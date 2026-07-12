"""Tests for tools/benchmark_corpus.py (weakness audit W1, 2026-07-12).

The benchmark harness is a public credibility artifact: anyone must be
able to reproduce the published numbers with one command. These tests
pin the three phases end-to-end on a TINY corpus (2 files x 400 rows)
so the whole file runs in well under a minute with no network:

* generation - manifest marker, determinism, schema shape
* run - every representative pipeline succeeds through the REAL engine
  (process_query_with_diagnostics), report structure is well-formed
* cleanup - the manifest guard refuses to delete a non-corpus dir
"""

import json
import os
import sys

import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools import benchmark_corpus as bc

TINY_FILES = 2
TINY_ROWS_PER_FILE = 400
TINY_SEED = 123


@pytest.fixture(scope="module")
def tiny_corpus(tmp_path_factory):
    """Generate a tiny corpus once for the whole module."""
    dest = str(tmp_path_factory.mktemp("bench") / "corpus")
    manifest = bc.generate_corpus(
        dest, files=TINY_FILES, rows_per_file=TINY_ROWS_PER_FILE,
        seed=TINY_SEED,
    )
    return dest, manifest


@pytest.fixture(scope="module")
def tiny_report(tiny_corpus):
    """Run the harness programmatically once for the whole module."""
    dest, _manifest = tiny_corpus
    return bc.run_benchmarks(dest, runs=2)


class TestGeneration:
    def test_manifest_marker_written(self, tiny_corpus):
        dest, manifest = tiny_corpus
        path = os.path.join(dest, bc.MANIFEST_NAME)
        assert os.path.isfile(path)
        with open(path, "r", encoding="utf-8") as fh:
            on_disk = json.load(fh)
        assert on_disk["generator"] == bc.GENERATOR_ID
        assert on_disk["base_seed"] == TINY_SEED
        assert on_disk["file_count"] == TINY_FILES
        assert on_disk["rows_per_file"] == TINY_ROWS_PER_FILE
        assert on_disk["rows_total"] == TINY_FILES * TINY_ROWS_PER_FILE
        assert on_disk == manifest

    def test_parquet_files_and_row_counts(self, tiny_corpus):
        dest, _ = tiny_corpus
        files = sorted(f for f in os.listdir(dest) if f.endswith(".parquet"))
        assert len(files) == TINY_FILES
        total = 0
        for f in files:
            df = pd.read_parquet(os.path.join(dest, f))
            total += len(df)
            assert list(df.columns) == bc.EXPECTED_COLUMNS
        assert total == TINY_FILES * TINY_ROWS_PER_FILE

    def test_epoch_column_int64_within_90_day_window(self, tiny_corpus):
        dest, _ = tiny_corpus
        f = sorted(f for f in os.listdir(dest) if f.endswith(".parquet"))[0]
        df = pd.read_parquet(os.path.join(dest, f))
        assert str(df["_epoch"].dtype) == "int64"
        assert int(df["_epoch"].min()) >= bc.START_EPOCH
        assert int(df["_epoch"].max()) <= bc.END_EPOCH

    def test_generation_is_deterministic_per_file_seed(self):
        a = bc.build_file_dataframe(50, seed=999)
        b = bc.build_file_dataframe(50, seed=999)
        pd.testing.assert_frame_equal(a, b)
        c = bc.build_file_dataframe(50, seed=1000)
        assert not a.equals(c)

    def test_refuses_to_generate_over_foreign_dir(self, tmp_path):
        foreign = tmp_path / "not_a_corpus"
        foreign.mkdir()
        (foreign / "precious.txt").write_text("user data")
        with pytest.raises(bc.BenchmarkCorpusError):
            bc.generate_corpus(str(foreign), files=1, rows_per_file=10)
        assert (foreign / "precious.txt").exists()

    def test_generate_requires_size_or_files(self, tmp_path):
        with pytest.raises(bc.BenchmarkCorpusError):
            bc.generate_corpus(str(tmp_path / "x"))


class TestRun:
    def test_every_pipeline_succeeds_with_rows(self, tiny_report):
        assert len(tiny_report["results"]) == 6
        for r in tiny_report["results"]:
            assert r["status"] == "success", (
                f"pipeline {r['name']} failed: {r['diagnostic']}"
            )
            assert r["rows"] > 0, f"pipeline {r['name']} returned zero rows"

    def test_expected_pipeline_names(self, tiny_report):
        names = [r["name"] for r in tiny_report["results"]]
        assert names == [
            "full_scan_head",
            "filtered_search_agg",
            "time_bounded_scan",
            "timechart_daily",
            "rex_extract_agg",
            "dedup_client_ip",
        ]

    def test_report_structure_well_formed(self, tiny_report):
        assert tiny_report["runs_per_pipeline"] == 2
        machine = tiny_report["machine"]
        for key in ("cpu_model", "cpu_count", "ram_total_gb",
                    "python_version", "platform"):
            assert key in machine
        corpus = tiny_report["corpus"]
        assert corpus["file_count"] == TINY_FILES
        assert corpus["row_count"] == TINY_FILES * TINY_ROWS_PER_FILE
        assert corpus["size_on_disk_bytes"] > 0
        assert corpus["epoch_min"] >= bc.START_EPOCH
        assert corpus["epoch_max"] <= bc.END_EPOCH
        for r in tiny_report["results"]:
            assert len(r["runs_seconds"]) == 2
            assert r["median_seconds"] >= 0
            assert r["spql"].startswith('index="')

    def test_report_is_json_serializable(self, tiny_report):
        round_tripped = json.loads(json.dumps(tiny_report))
        assert round_tripped["corpus"]["row_count"] == (
            TINY_FILES * TINY_ROWS_PER_FILE
        )

    def test_full_scan_head_caps_at_100_rows(self, tiny_report):
        by_name = {r["name"]: r for r in tiny_report["results"]}
        assert by_name["full_scan_head"]["rows"] == 100

    def test_markdown_render_contains_table_and_context(self, tiny_report):
        md = bc.render_markdown(tiny_report)
        assert "| Pipeline | SPQL | Median (s) | Rows | Status |" in md
        assert "|---|---|---:|---:|---|" in md
        for r in tiny_report["results"]:
            assert r["name"] in md
        assert "CPU:" in md
        assert "Runs per pipeline: 2" in md

    def test_time_bound_pipeline_hits_a_slice_not_everything(self, tiny_report):
        by_name = {r["name"]: r for r in tiny_report["results"]}
        spql = by_name["time_bounded_scan"]["spql"]
        assert 'earliest="' in spql and 'latest="' in spql


class TestCleanupGuard:
    def test_refuses_dir_without_manifest(self, tmp_path):
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "keep_me.parquet").write_bytes(b"not really parquet")
        with pytest.raises(bc.BenchmarkCorpusError):
            bc.cleanup_corpus(str(victim))
        assert (victim / "keep_me.parquet").exists()

    def test_refuses_foreign_manifest(self, tmp_path):
        victim = tmp_path / "victim2"
        victim.mkdir()
        (victim / bc.MANIFEST_NAME).write_text(
            json.dumps({"generator": "someone_else"})
        )
        with pytest.raises(bc.BenchmarkCorpusError):
            bc.cleanup_corpus(str(victim))
        assert victim.exists()

    def test_refuses_missing_dir(self, tmp_path):
        with pytest.raises(bc.BenchmarkCorpusError):
            bc.cleanup_corpus(str(tmp_path / "never_existed"))

    def test_cleanup_removes_real_corpus(self, tmp_path):
        dest = str(tmp_path / "small")
        bc.generate_corpus(dest, files=1, rows_per_file=10, seed=7)
        assert os.path.isdir(dest)
        bc.cleanup_corpus(dest)
        assert not os.path.exists(dest)


class TestCLI:
    def test_no_action_flags_is_an_error(self, capsys):
        assert bc.main([]) == 1
        assert "[x]" in capsys.readouterr().out

    def test_generate_run_cleanup_composed(self, tmp_path, capsys):
        dest = str(tmp_path / "cli_corpus")
        rc = bc.main([
            "--generate", "--files", "1", "--rows-per-file", "200",
            "--run", "--runs", "1", "--cleanup",
            "--dest", dest,
            "--json", str(tmp_path / "report.json"),
        ])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "| Pipeline | SPQL | Median (s) | Rows | Status |" in out
        assert not os.path.exists(dest)
        report = json.loads((tmp_path / "report.json").read_text())
        assert all(r["status"] == "success" for r in report["results"])

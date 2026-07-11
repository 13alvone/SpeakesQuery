"""
Tests for notebook_store.py + validation/NotebookValidation.py
(Phase 3 / Bet 4 slice 1).

The persistence layer is the foundation; later slices add the cell
engine (slice 2), the reactive cache (slice 3), and the Monaco-backed
SPA (slice 4+). Slice 1 ships:

  * `validation/NotebookValidation.py` - schema validators
  * `notebook_store.py` - YAML CRUD + seed-defaults pattern
  * Drift-guard wiring in 5 places (.gitignore, persistence.py,
    docker-compose.yml, install.sh, tests/test_persistence.py)

Test layout:
  * TestSchemaValidation - field validators + record validation +
    cross-cell uniqueness rules
  * TestStoreCrud - save / get / list / update / delete / install_default
  * TestSeedDefaults - missing-only copy from default_notebooks/
  * TestSingleton - get_store() / reset_for_tests() lifecycle
  * TestUserDataDriftGuards - the 5-place check (this is the load-bearing
    test; missing any one of the 5 silently wipes user data on container
    rebuild)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import notebook_store
from notebook_store import (
    DEFAULTS_DIR, NOTEBOOK_EXT, NOTEBOOKS_DIR,
    NotebookStore, get_store, reset_for_tests,
)
from validation.NotebookValidation import (
    ALLOWED_CELL_TYPES, NotebookValidation,
)


PROJECT_ROOT = Path(__file__).parent.parent


# ── Shared fixtures ─────────────────────────────────────────────────

@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Redirect both NOTEBOOKS_DIR and DEFAULTS_DIR to tmp_path so each
    test runs against a clean filesystem state. Mirrors the
    ``isolated_*_state`` pattern used elsewhere.
    """
    notebook_store.reset_for_tests()
    nb_dir = tmp_path / "notebooks"
    df_dir = tmp_path / "default_notebooks"
    nb_dir.mkdir()
    df_dir.mkdir()
    monkeypatch.setattr(notebook_store, "NOTEBOOKS_DIR", nb_dir)
    monkeypatch.setattr(notebook_store, "DEFAULTS_DIR", df_dir)
    yield nb_dir, df_dir
    notebook_store.reset_for_tests()


def _sample_record(notebook_id="my_brief"):
    return {
        "id": notebook_id,
        "name": "Daily news brief",
        "description": "Cost-cascade triage of last 24h news.",
        "default_max_cost_usd": 0.50,
        "cells": [
            {
                "id": "cell_1",
                "type": "spql",
                "source": (
                    'index="news/*.parquet" earliest=-24h '
                    '| nearest "fed pause" topk=50'
                ),
            },
            {
                "id": "rated",
                "type": "pipe",
                "source": (
                    "cell_1 | llm model=\"ollama-llama3-1-8b\" "
                    "prompt=\"rate 1-10 as JSON\""
                ),
            },
            {
                "id": "summary",
                "type": "markdown",
                "source": "## Top picks\n\nSee the rated cell above.",
            },
        ],
    }


# ═══════════════════════════════════════════════════════════════════
# 1. Schema validation
# ═══════════════════════════════════════════════════════════════════

class TestNotebookIdValidation:
    @pytest.mark.parametrize("nid", [
        "news_triage", "options_v2", "my-brief", "v1.0", "abc123", "a",
    ])
    def test_valid_ids(self, nid):
        assert NotebookValidation.validate_notebook_id(nid) == nid

    @pytest.mark.parametrize("nid", [
        "", " ", "Foo", "../etc/passwd", "with space", "with/slash",
        "tab\there", "with$char",
    ])
    def test_invalid_ids_raise(self, nid):
        with pytest.raises(ValueError):
            NotebookValidation.validate_notebook_id(nid)

    def test_id_max_length(self):
        # 64 chars OK
        ok = "a" * 64
        assert NotebookValidation.validate_notebook_id(ok) == ok
        # 65 fails
        with pytest.raises(ValueError, match="64"):
            NotebookValidation.validate_notebook_id("a" * 65)

    def test_id_non_string_raises(self):
        with pytest.raises(ValueError):
            NotebookValidation.validate_notebook_id(None)
        with pytest.raises(ValueError):
            NotebookValidation.validate_notebook_id(42)


class TestCellIdValidation:
    @pytest.mark.parametrize("cid", [
        "cell_1", "candidates", "rated_news", "x", "x_2",
    ])
    def test_valid_cell_ids(self, cid):
        assert NotebookValidation.validate_cell_id(cid) == cid

    @pytest.mark.parametrize("cid", [
        "", "1cell",       # leading digit
        "Cell_1",          # uppercase
        "with space",
        "with-dash",       # hyphens forbidden in cell ids (Python identifier)
        "a" * 33,          # over 32 char cap
    ])
    def test_invalid_cell_ids_raise(self, cid):
        with pytest.raises(ValueError):
            NotebookValidation.validate_cell_id(cid)


class TestCellTypeValidation:
    def test_all_documented_types_accepted(self):
        for t in ("spql", "python", "chart", "markdown", "param", "pipe"):
            assert NotebookValidation.validate_cell_type(t) == t

    def test_uppercase_normalised(self):
        assert NotebookValidation.validate_cell_type("SPQL") == "spql"
        assert NotebookValidation.validate_cell_type("Markdown") == "markdown"

    @pytest.mark.parametrize("t", [
        "", "javascript", "sql", "rust", "ipynb",
    ])
    def test_unknown_types_rejected(self, t):
        with pytest.raises(ValueError):
            NotebookValidation.validate_cell_type(t)

    def test_allowed_types_frozen_set_size(self):
        # Drift guard: if a future slice adds a new cell type, we want
        # to know about it explicitly (forces test + doc updates).
        # Slice 1 shipped 6; slice 9 added promote_to_alert_group → 7.
        assert ALLOWED_CELL_TYPES == frozenset({
            "spql", "python", "chart", "markdown", "param", "pipe",
            "promote_to_alert_group",
        })


class TestCellSourceValidation:
    def test_empty_source_ok(self):
        assert NotebookValidation.validate_cell_source(None) == ""
        assert NotebookValidation.validate_cell_source("") == ""

    def test_unicode_source_preserved(self):
        src = "title: 価格 (price)\n# émoji 🎉\n"
        assert NotebookValidation.validate_cell_source(src) == src

    def test_oversize_source_rejected(self):
        # 100KB cap; 100KB+1 byte should fail
        big = "x" * (NotebookValidation.MAX_CELL_SOURCE_BYTES + 1)
        with pytest.raises(ValueError, match="bytes"):
            NotebookValidation.validate_cell_source(big)

    def test_non_string_source_rejected(self):
        with pytest.raises(ValueError):
            NotebookValidation.validate_cell_source(42)


class TestNotebookRecordValidation:
    def test_minimal_record_round_trips(self):
        result = NotebookValidation.validate_record({"id": "foo"})
        assert result["id"] == "foo"
        assert result["cells"] == []
        assert result["name"] == ""
        assert result["description"] == ""
        assert result["default_max_cost_usd"] == 0.0
        assert result["schema_version"] == NotebookValidation.CURRENT_SCHEMA_VERSION

    def test_full_record_round_trips(self):
        record = _sample_record()
        out = NotebookValidation.validate_record(record)
        assert out["id"] == record["id"]
        assert len(out["cells"]) == 3
        assert out["cells"][0]["type"] == "spql"
        assert out["cells"][1]["type"] == "pipe"
        assert out["cells"][2]["type"] == "markdown"

    def test_duplicate_cell_ids_rejected(self):
        bad = {
            "id": "x",
            "cells": [
                {"id": "cell_1", "type": "spql", "source": "a"},
                {"id": "cell_1", "type": "markdown", "source": "b"},
            ],
        }
        with pytest.raises(ValueError, match="Duplicate cell id"):
            NotebookValidation.validate_record(bad)

    def test_too_many_cells_rejected(self):
        bad = {
            "id": "x",
            "cells": [
                {"id": f"cell_{i}", "type": "spql", "source": "x"}
                for i in range(NotebookValidation.MAX_CELLS_PER_NOTEBOOK + 1)
            ],
        }
        with pytest.raises(ValueError, match="cell count"):
            NotebookValidation.validate_record(bad)

    def test_cells_not_a_list_rejected(self):
        bad = {"id": "x", "cells": "not-a-list"}
        with pytest.raises(ValueError, match="cells"):
            NotebookValidation.validate_record(bad)

    def test_cell_with_metadata_round_trips(self):
        record = {
            "id": "x",
            "cells": [
                {
                    "id": "cell_1",
                    "type": "chart",
                    "source": "{}",
                    "metadata": {"width": 400, "height": 300},
                },
            ],
        }
        out = NotebookValidation.validate_record(record)
        assert out["cells"][0]["metadata"] == {"width": 400, "height": 300}

    def test_default_max_cost_usd_validators(self):
        # 0 ok, positive ok, negative rejected, > 1000 rejected
        rec = {"id": "x", "default_max_cost_usd": 0.0}
        assert NotebookValidation.validate_record(rec)["default_max_cost_usd"] == 0.0
        rec["default_max_cost_usd"] = 0.50
        assert NotebookValidation.validate_record(rec)["default_max_cost_usd"] == 0.50
        rec["default_max_cost_usd"] = -1.0
        with pytest.raises(ValueError, match="non-negative"):
            NotebookValidation.validate_record(rec)
        rec["default_max_cost_usd"] = 9999.0
        with pytest.raises(ValueError, match="ceiling"):
            NotebookValidation.validate_record(rec)

    def test_schema_version_must_be_int(self):
        bad = {"id": "x", "schema_version": "1"}
        with pytest.raises(ValueError, match="schema_version"):
            NotebookValidation.validate_record(bad)

    def test_schema_version_below_one_rejected(self):
        bad = {"id": "x", "schema_version": 0}
        with pytest.raises(ValueError, match=">= 1"):
            NotebookValidation.validate_record(bad)

    def test_higher_schema_version_tolerated_for_forward_compat(self):
        # If a newer version of the codebase wrote v2, an older reader
        # should still accept it (additive-only schema rule).
        rec = {"id": "x", "schema_version": 2, "cells": []}
        out = NotebookValidation.validate_record(rec)
        assert out["schema_version"] == 2

    def test_reactive_cache_fields_round_trip(self):
        # Slice 3 will populate _last_input_hash etc. Slice 1 must
        # tolerate these fields when present so the YAML round-trips.
        rec = {
            "id": "x",
            "cells": [
                {
                    "id": "cell_1", "type": "spql", "source": "...",
                    "_last_executed_at": "2026-05-08T20:00:00",
                    "_last_input_hash": "abc123",
                    "_last_output_hash": "def456",
                    "_last_runtime_ms": 42,
                },
            ],
        }
        out = NotebookValidation.validate_record(rec)
        assert out["cells"][0]["_last_input_hash"] == "abc123"
        assert out["cells"][0]["_last_output_hash"] == "def456"
        assert out["cells"][0]["_last_runtime_ms"] == 42


# ═══════════════════════════════════════════════════════════════════
# 2. Store CRUD
# ═══════════════════════════════════════════════════════════════════

class TestStoreCrud:
    def test_save_and_get_round_trip(self, isolated_store):
        store = get_store()
        record = _sample_record()
        saved = store.save_notebook(record)
        assert saved["id"] == "my_brief"
        assert "created_at" in saved
        assert "updated_at" in saved

        loaded = store.get_notebook("my_brief")
        assert loaded is not None
        assert loaded["id"] == "my_brief"
        assert len(loaded["cells"]) == 3

    def test_save_writes_atomically_and_idempotently(self, isolated_store):
        nb_dir, _ = isolated_store
        store = get_store()
        store.save_notebook(_sample_record())
        # File exists with the .spqnb extension
        path = nb_dir / f"my_brief{NOTEBOOK_EXT}"
        assert path.exists()
        assert path.read_text(encoding="utf-8").startswith("id: my_brief")

    def test_save_refuses_overwrite_by_default(self, isolated_store):
        store = get_store()
        store.save_notebook(_sample_record())
        with pytest.raises(FileExistsError):
            store.save_notebook(_sample_record())

    def test_save_overwrite_replaces(self, isolated_store):
        store = get_store()
        store.save_notebook(_sample_record())
        new = _sample_record()
        new["name"] = "Updated"
        result = store.save_notebook(new, overwrite=True)
        assert result["name"] == "Updated"

    def test_update_merges_patch(self, isolated_store):
        store = get_store()
        store.save_notebook(_sample_record())
        result = store.update_notebook(
            "my_brief", {"description": "now updated"},
        )
        assert result["description"] == "now updated"
        # cells preserved across update
        assert len(result["cells"]) == 3

    def test_update_replaces_cells_wholesale(self, isolated_store):
        store = get_store()
        store.save_notebook(_sample_record())
        new_cells = [
            {"id": "only", "type": "spql", "source": 'index="x"'},
        ]
        result = store.update_notebook("my_brief", {"cells": new_cells})
        assert len(result["cells"]) == 1
        assert result["cells"][0]["id"] == "only"

    def test_update_id_in_patch_ignored(self, isolated_store):
        # The id is the filename - it cannot change via update()
        store = get_store()
        store.save_notebook(_sample_record())
        result = store.update_notebook(
            "my_brief", {"id": "different_id", "description": "x"},
        )
        assert result["id"] == "my_brief"  # unchanged

    def test_update_missing_raises(self, isolated_store):
        store = get_store()
        with pytest.raises(FileNotFoundError):
            store.update_notebook("nonexistent", {"description": "x"})

    def test_get_missing_returns_none(self, isolated_store):
        store = get_store()
        assert store.get_notebook("nope") is None

    def test_get_invalid_id_returns_none(self, isolated_store):
        # `get_notebook("../etc/passwd")` should return None, NOT raise
        # - caller-friendly graceful failure on garbage input.
        store = get_store()
        assert store.get_notebook("../etc/passwd") is None
        assert store.get_notebook("with space") is None

    def test_list_returns_sorted_records(self, isolated_store):
        store = get_store()
        store.save_notebook({**_sample_record(), "id": "zoo"})
        store.save_notebook({**_sample_record(), "id": "abc"})
        store.save_notebook({**_sample_record(), "id": "mid"})
        ids = [n["id"] for n in store.list_notebooks()]
        assert ids == ["abc", "mid", "zoo"]

    def test_list_skips_invalid_yaml(self, isolated_store):
        # A malformed file should NOT poison the whole list - operator
        # can still see / edit the others.
        nb_dir, _ = isolated_store
        store = get_store()
        store.save_notebook(_sample_record())
        # Drop a malformed file alongside
        (nb_dir / "bad.spqnb").write_text("this is: : not valid yaml\nid:")
        ids = [n["id"] for n in store.list_notebooks()]
        assert "my_brief" in ids
        # bad.spqnb is skipped silently

    def test_list_notebook_ids_lighter_listing(self, isolated_store):
        store = get_store()
        store.save_notebook({**_sample_record(), "id": "alpha"})
        store.save_notebook({**_sample_record(), "id": "beta"})
        assert store.list_notebook_ids() == ["alpha", "beta"]

    def test_delete_removes_file(self, isolated_store):
        nb_dir, _ = isolated_store
        store = get_store()
        store.save_notebook(_sample_record())
        path = nb_dir / f"my_brief{NOTEBOOK_EXT}"
        assert path.exists()
        assert store.delete_notebook("my_brief") is True
        assert not path.exists()

    def test_delete_missing_returns_false(self, isolated_store):
        store = get_store()
        assert store.delete_notebook("nonexistent") is False

    def test_delete_invalid_id_returns_false(self, isolated_store):
        # Garbage id → False, not exception
        store = get_store()
        assert store.delete_notebook("../etc/passwd") is False


# ═══════════════════════════════════════════════════════════════════
# 3. Seed defaults
# ═══════════════════════════════════════════════════════════════════

class TestSeedDefaults:
    def test_no_defaults_dir_is_noop(self, tmp_path, monkeypatch):
        # If default_notebooks/ is missing entirely, init succeeds silently
        nb_dir = tmp_path / "notebooks"
        nb_dir.mkdir()
        # Don't create defaults dir
        notebook_store.reset_for_tests()
        monkeypatch.setattr(notebook_store, "NOTEBOOKS_DIR", nb_dir)
        monkeypatch.setattr(
            notebook_store, "DEFAULTS_DIR", tmp_path / "missing_defaults",
        )
        store = get_store()  # initialises
        # No crash; no notebooks
        assert store.list_notebook_ids() == []
        notebook_store.reset_for_tests()

    def test_empty_defaults_dir_is_noop(self, isolated_store):
        # default_notebooks/ exists but contains no .spqnb files
        store = get_store()
        assert store.list_notebook_ids() == []

    def test_seeds_missing_defaults_on_initialize(
        self, tmp_path, monkeypatch,
    ):
        nb_dir = tmp_path / "notebooks"
        df_dir = tmp_path / "default_notebooks"
        nb_dir.mkdir()
        df_dir.mkdir()
        # Drop a default that should be seeded
        seed_record = {**_sample_record(), "id": "shipped_template"}
        text = yaml.dump(seed_record, default_flow_style=False, sort_keys=False)
        (df_dir / f"shipped_template{NOTEBOOK_EXT}").write_text(text)
        notebook_store.reset_for_tests()
        monkeypatch.setattr(notebook_store, "NOTEBOOKS_DIR", nb_dir)
        monkeypatch.setattr(notebook_store, "DEFAULTS_DIR", df_dir)
        store = get_store()
        assert store.get_notebook("shipped_template") is not None
        notebook_store.reset_for_tests()

    def test_does_not_overwrite_existing_user_notebook(
        self, tmp_path, monkeypatch,
    ):
        nb_dir = tmp_path / "notebooks"
        df_dir = tmp_path / "default_notebooks"
        nb_dir.mkdir()
        df_dir.mkdir()
        # User has a notebook with the same id as a default
        user_record = {**_sample_record(), "id": "shipped",
                       "description": "USER VERSION"}
        (nb_dir / f"shipped{NOTEBOOK_EXT}").write_text(
            yaml.dump(user_record, default_flow_style=False, sort_keys=False),
        )
        # Default with the same id but different content
        default_record = {**_sample_record(), "id": "shipped",
                          "description": "DEFAULT VERSION"}
        (df_dir / f"shipped{NOTEBOOK_EXT}").write_text(
            yaml.dump(default_record, default_flow_style=False, sort_keys=False),
        )
        notebook_store.reset_for_tests()
        monkeypatch.setattr(notebook_store, "NOTEBOOKS_DIR", nb_dir)
        monkeypatch.setattr(notebook_store, "DEFAULTS_DIR", df_dir)
        store = get_store()
        loaded = store.get_notebook("shipped")
        assert loaded["description"] == "USER VERSION", (
            "_seed_defaults must NEVER overwrite a user-customised notebook."
        )
        notebook_store.reset_for_tests()

    def test_install_default_explicit_install(self, isolated_store):
        nb_dir, df_dir = isolated_store
        # Initialise the store FIRST (empty defaults → no-op seed).
        store = get_store()
        assert store.list_notebook_ids() == []
        # Now drop a default into the defaults dir AFTER init has run.
        seed = {**_sample_record(), "id": "after_init"}
        (df_dir / f"after_init{NOTEBOOK_EXT}").write_text(
            yaml.dump(seed, default_flow_style=False, sort_keys=False),
        )
        # install_default brings it in on demand without re-running seed
        assert store.install_default("after_init") is True
        assert store.get_notebook("after_init") is not None

    def test_install_default_refuses_overwrite_unless_flagged(
        self, isolated_store,
    ):
        nb_dir, df_dir = isolated_store
        # Pre-existing user notebook
        store = get_store()
        store.save_notebook({**_sample_record(), "id": "existing"})
        # Default with same id
        (df_dir / f"existing{NOTEBOOK_EXT}").write_text(
            yaml.dump(
                {**_sample_record(), "id": "existing", "description": "DEFAULT"},
                default_flow_style=False, sort_keys=False,
            ),
        )
        # Refuses without overwrite
        assert store.install_default("existing") is False
        assert store.get_notebook("existing")["description"] == \
               _sample_record()["description"]
        # With overwrite - replaces
        assert store.install_default("existing", overwrite=True) is True
        assert store.get_notebook("existing")["description"] == "DEFAULT"

    def test_list_default_ids(self, isolated_store):
        nb_dir, df_dir = isolated_store
        for nid in ("alpha", "beta", "gamma"):
            (df_dir / f"{nid}{NOTEBOOK_EXT}").write_text(
                yaml.dump({**_sample_record(), "id": nid}, sort_keys=False),
            )
        store = get_store()
        assert store.list_default_ids() == ["alpha", "beta", "gamma"]


# ═══════════════════════════════════════════════════════════════════
# 4. Singleton
# ═══════════════════════════════════════════════════════════════════

class TestSingleton:
    def test_get_store_returns_same_instance(self, isolated_store):
        a = get_store()
        b = get_store()
        assert a is b

    def test_reset_for_tests_clears_singleton(self, isolated_store):
        a = get_store()
        reset_for_tests()
        b = get_store()
        assert a is not b


# ═══════════════════════════════════════════════════════════════════
# 5. User-data drift guards (the load-bearing test)
# ═══════════════════════════════════════════════════════════════════

class TestUserDataDriftGuards:
    """Per CLAUDE.md "Do Not": adding a new user-data directory without
    ALSO updating (a) tools/persistence.py, (b) docker-compose.yml,
    (c) install.sh mkdir block, (d) .gitignore, (e) the persistence
    drift-guard tests will silently wipe user data on container rebuild.

    Slice 1 added `notebooks/` and `default_notebooks/` - verify all 5
    layers are wired.
    """

    def test_persistence_targets_includes_notebooks(self):
        from tools.persistence import DIR_TARGETS_HASHED
        assert "notebooks" in DIR_TARGETS_HASHED, (
            "tools/persistence.py::DIR_TARGETS_HASHED must include "
            "'notebooks'. Without this, `python -m tools.persistence "
            "backup` silently excludes user notebooks."
        )

    def test_persistence_targets_includes_default_notebooks(self):
        from tools.persistence import DIR_TARGETS_HASHED
        assert "default_notebooks" in DIR_TARGETS_HASHED, (
            "tools/persistence.py::DIR_TARGETS_HASHED should include "
            "'default_notebooks' so the diff catches drift between the "
            "user's tree and the templates the next install would seed."
        )

    def test_docker_compose_bind_mounts_notebooks(self):
        compose = (
            PROJECT_ROOT / "desktop_app" / "docker-compose.yml"
        ).read_text(encoding="utf-8")
        assert "../notebooks:/app/notebooks" in compose, (
            "desktop_app/docker-compose.yml must bind-mount "
            "../notebooks:/app/notebooks. Without this, every container "
            "rebuild wipes the user's notebooks."
        )

    def test_docker_compose_bind_mounts_default_notebooks_readonly(self):
        compose = (
            PROJECT_ROOT / "desktop_app" / "docker-compose.yml"
        ).read_text(encoding="utf-8")
        assert "../default_notebooks:/app/default_notebooks:ro" in compose, (
            "default_notebooks/ must be bind-mounted READ-ONLY so a "
            "runtime bug can never mutate the templates and propagate "
            "corruption back to git."
        )

    def test_install_sh_mkdir_includes_notebooks(self):
        install = (PROJECT_ROOT / "install.sh").read_text(encoding="utf-8")
        assert '"$PROJECT_ROOT/notebooks"' in install, (
            "install.sh `mkdir -p` block must create $PROJECT_ROOT/notebooks "
            "before docker compose runs, or Docker will create the dir "
            "with root ownership inside the volume."
        )
        assert '"$PROJECT_ROOT/default_notebooks"' in install, (
            "install.sh must mkdir default_notebooks/ as well."
        )

    def test_gitignore_excludes_user_notebooks(self):
        gi = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "/notebooks/*.spqnb" in gi, (
            ".gitignore must exclude /notebooks/*.spqnb - user-mutable "
            "notebooks are gitignored runtime data, mirroring "
            "/alert_groups/*.yaml and /models/*.yaml."
        )

    def test_default_notebooks_dir_is_tracked(self):
        # `default_notebooks/` itself (and at minimum a placeholder) is
        # tracked in git so a fresh clone has the directory available
        # before `install.sh` runs the mkdir.
        default_dir = PROJECT_ROOT / "default_notebooks"
        assert default_dir.is_dir(), (
            "default_notebooks/ directory must exist in the worktree."
        )
        # Either a .gitkeep or actual default notebooks must exist
        children = list(default_dir.iterdir())
        assert len(children) >= 1, (
            "default_notebooks/ must contain at least .gitkeep so git "
            "tracks the directory."
        )

    def test_user_data_yaml_actually_ignored_by_git(self):
        # Realistic check: drop a fake notebook into notebooks/ and ask
        # `git check-ignore` whether git would track it.
        sentinel = PROJECT_ROOT / "notebooks" / "_drift_guard_sentinel.spqnb"
        try:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("id: sentinel\ncells: []\n")
            try:
                proc = subprocess.run(
                    ["git", "check-ignore", str(sentinel)],
                    cwd=PROJECT_ROOT, capture_output=True, text=True,
                )
            except FileNotFoundError:
                pytest.skip("git CLI not available in this environment")
            # rc=0 means ignored; rc=1 means tracked. We want ignored.
            assert proc.returncode == 0, (
                f"`notebooks/*.spqnb` is NOT actually ignored by git. "
                f"`git check-ignore` returned rc={proc.returncode} "
                f"with stdout={proc.stdout!r} stderr={proc.stderr!r}."
            )
        finally:
            if sentinel.exists():
                sentinel.unlink()


# ═══════════════════════════════════════════════════════════════════
# 6. NOTEBOOK_EXT contract
# ═══════════════════════════════════════════════════════════════════

class TestExtensionContract:
    def test_extension_is_spqnb(self):
        # Drift guard: any change to the file extension breaks editor
        # association rules + the persistence audit's glob patterns.
        assert NOTEBOOK_EXT == ".spqnb"

    def test_save_writes_with_spqnb_extension(self, isolated_store):
        nb_dir, _ = isolated_store
        store = get_store()
        store.save_notebook(_sample_record())
        files = list(nb_dir.glob("*.spqnb"))
        assert len(files) == 1
        assert files[0].suffix == ".spqnb"

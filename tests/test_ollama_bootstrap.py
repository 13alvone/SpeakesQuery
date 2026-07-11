"""
Tests for tools/ollama_bootstrap.py - Phase 2 / Bet 3 slice 8.

The bootstrap helper has four layers - detect, list, pull, verify -
plus the orchestrating `bootstrap()` and the `main()` CLI wrapper.
We mock the HTTP layer (`requests.get` / `requests.post`) so tests
never reach a real daemon, then exercise:

  * Each helper's happy path, error path, and edge cases.
  * The orchestrator's seven decision branches (model resolve fail,
    daemon unreachable, model present, model missing + pull declined,
    model missing + pull fails, model missing + pull + verify fails,
    full success).
  * The CLI `--json` output shape + exit codes.
  * Drift guards: `_INSTALL_HINTS` covers the three platforms; the
    `_resolve_ollama_model` only accepts ollama-provider records.
"""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
import requests

from tools.ollama_bootstrap import (
    BootstrapReport, OllamaStatus,
    bootstrap, detect_ollama, list_local_models, main, pull_model,
    verify_inference, _build_argparser, _platform_install_hint,
    _resolve_ollama_model, _INSTALL_HINTS,
)


# ── Shared fixture for a clean model-store + history isolation ──────

@pytest.fixture
def isolated_model_store(tmp_path, monkeypatch):
    import model_store
    import analyzers.llm_history_store as hist
    model_store.reset_for_tests()
    hist.reset_for_tests()
    monkeypatch.setattr(model_store, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(
        hist, "DEFAULT_DB_PATH", tmp_path / "llm_call_history.sqlite",
    )
    model_store.get_store()  # seeds defaults
    yield
    model_store.reset_for_tests()
    hist.reset_for_tests()


def _mk_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json = MagicMock(return_value=json_data if json_data is not None else {})
    return resp


# ═════════════════════════════════════════════════════════════════════
# 1. detect_ollama
# ═════════════════════════════════════════════════════════════════════

class TestDetectOllama:
    def test_reachable_returns_version(self):
        with patch(
            "tools.ollama_bootstrap.requests.get",
            return_value=_mk_response(200, {"version": "0.3.0"}),
        ):
            status = detect_ollama("http://localhost:11434")
        assert status.reachable is True
        assert status.version == "0.3.0"

    def test_legacy_falls_back_to_tags(self):
        # /api/version returns 404; /api/tags returns 200 → reachable, "legacy"
        responses = [
            _mk_response(404, {}),
            _mk_response(200, {"models": []}),
        ]
        with patch(
            "tools.ollama_bootstrap.requests.get",
            side_effect=responses,
        ):
            status = detect_ollama("http://localhost:11434")
        assert status.reachable is True
        assert status.version == "legacy"

    def test_connection_refused_returns_unreachable(self):
        with patch(
            "tools.ollama_bootstrap.requests.get",
            side_effect=requests.ConnectionError("Connection refused"),
        ):
            status = detect_ollama("http://localhost:11434")
        assert status.reachable is False
        assert "ConnectionError" in (status.error or "")

    def test_timeout_returns_unreachable(self):
        with patch(
            "tools.ollama_bootstrap.requests.get",
            side_effect=requests.Timeout("timed out"),
        ):
            status = detect_ollama("http://localhost:11434", timeout=1.0)
        assert status.reachable is False
        assert "Timeout" in (status.error or "")

    def test_500_returns_unreachable_with_body(self):
        with patch(
            "tools.ollama_bootstrap.requests.get",
            return_value=_mk_response(500, {}, text="Internal Server Error"),
        ):
            status = detect_ollama("http://localhost:11434")
        assert status.reachable is False
        assert "500" in (status.error or "")

    def test_endpoint_trailing_slash_normalised(self):
        # Should not produce double-slash URLs
        captured = {}

        def fake_get(url, *a, **kw):
            captured["url"] = url
            return _mk_response(200, {"version": "0.3.0"})

        with patch("tools.ollama_bootstrap.requests.get", side_effect=fake_get):
            detect_ollama("http://localhost:11434/")
        assert captured["url"] == "http://localhost:11434/api/version"


# ═════════════════════════════════════════════════════════════════════
# 2. list_local_models
# ═════════════════════════════════════════════════════════════════════

class TestListLocalModels:
    def test_returns_model_names(self):
        with patch(
            "tools.ollama_bootstrap.requests.get",
            return_value=_mk_response(200, {
                "models": [
                    {"name": "llama3.1:8b"},
                    {"name": "qwen2:7b"},
                ],
            }),
        ):
            names = list_local_models("http://localhost:11434")
        assert names == ["llama3.1:8b", "qwen2:7b"]

    def test_empty_models_list(self):
        with patch(
            "tools.ollama_bootstrap.requests.get",
            return_value=_mk_response(200, {"models": []}),
        ):
            assert list_local_models("http://localhost:11434") == []

    def test_http_error_returns_empty(self):
        with patch(
            "tools.ollama_bootstrap.requests.get",
            return_value=_mk_response(503, {}, text="overloaded"),
        ):
            assert list_local_models("http://localhost:11434") == []

    def test_transport_error_returns_empty(self):
        with patch(
            "tools.ollama_bootstrap.requests.get",
            side_effect=requests.ConnectionError("nope"),
        ):
            assert list_local_models("http://localhost:11434") == []

    def test_filters_entries_without_name(self):
        with patch(
            "tools.ollama_bootstrap.requests.get",
            return_value=_mk_response(200, {
                "models": [{"name": "valid"}, {"size": 123}],  # second entry has no name
            }),
        ):
            assert list_local_models("http://localhost:11434") == ["valid"]


# ═════════════════════════════════════════════════════════════════════
# 3. pull_model
# ═════════════════════════════════════════════════════════════════════

class TestPullModel:
    def _streaming_response(self, lines, status_code=200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = ""
        resp.iter_lines = MagicMock(return_value=iter(lines))
        return resp

    def test_pull_success_streams_to_completion(self):
        lines = [
            json.dumps({"status": "pulling manifest"}).encode(),
            json.dumps({"status": "downloading", "completed": 50, "total": 100}).encode(),
            json.dumps({"status": "downloading", "completed": 100, "total": 100}).encode(),
            json.dumps({"status": "success"}).encode(),
        ]
        with patch(
            "tools.ollama_bootstrap.requests.post",
            return_value=self._streaming_response(lines),
        ):
            ok = pull_model("llama3.1:8b")
        assert ok is True

    def test_pull_records_progress_callback(self):
        progress = []
        lines = [
            json.dumps({"status": "pulling manifest"}).encode(),
            json.dumps({"status": "success"}).encode(),
        ]
        with patch(
            "tools.ollama_bootstrap.requests.post",
            return_value=self._streaming_response(lines),
        ):
            pull_model(
                "llama3.1:8b",
                progress_cb=progress.append,
            )
        assert "pulling manifest" in progress
        assert "success" in progress

    def test_pull_server_error_returns_false(self):
        lines = [
            json.dumps({"error": "model not found"}).encode(),
        ]
        with patch(
            "tools.ollama_bootstrap.requests.post",
            return_value=self._streaming_response(lines),
        ):
            ok = pull_model("nonexistent:latest")
        assert ok is False

    def test_pull_http_error_returns_false(self):
        with patch(
            "tools.ollama_bootstrap.requests.post",
            return_value=self._streaming_response([], status_code=500),
        ):
            ok = pull_model("llama3.1:8b")
        assert ok is False

    def test_pull_transport_error_returns_false(self):
        with patch(
            "tools.ollama_bootstrap.requests.post",
            side_effect=requests.ConnectionError("nope"),
        ):
            ok = pull_model("llama3.1:8b")
        assert ok is False

    def test_pull_skips_non_json_lines(self):
        # Mixed JSON + garbage; final success should still register
        lines = [
            b"not json",
            json.dumps({"status": "downloading"}).encode(),
            b"",  # empty line - skipped
            json.dumps({"status": "success"}).encode(),
        ]
        with patch(
            "tools.ollama_bootstrap.requests.post",
            return_value=self._streaming_response(lines),
        ):
            assert pull_model("llama3.1:8b") is True


# ═════════════════════════════════════════════════════════════════════
# 4. verify_inference
# ═════════════════════════════════════════════════════════════════════

class TestVerifyInference:
    def test_success_returns_text(self):
        with patch(
            "tools.ollama_bootstrap.requests.post",
            return_value=_mk_response(200, {
                "message": {"content": "yes."},
            }),
        ):
            ok, text = verify_inference("llama3.1:8b")
        assert ok is True
        assert text == "yes."

    def test_empty_content_returns_false(self):
        with patch(
            "tools.ollama_bootstrap.requests.post",
            return_value=_mk_response(200, {"message": {"content": ""}}),
        ):
            ok, text = verify_inference("llama3.1:8b")
        assert ok is False
        assert text == ""

    def test_http_error_returns_false(self):
        with patch(
            "tools.ollama_bootstrap.requests.post",
            return_value=_mk_response(404, {}, text="not found"),
        ):
            ok, text = verify_inference("llama3.1:8b")
        assert ok is False
        assert text is None

    def test_transport_error_returns_false(self):
        with patch(
            "tools.ollama_bootstrap.requests.post",
            side_effect=requests.ConnectionError("down"),
        ):
            ok, _ = verify_inference("llama3.1:8b")
        assert ok is False

    def test_uses_num_predict_one_for_speed(self):
        captured = {}

        def fake_post(url, json=None, *a, **kw):
            captured["json"] = json
            return _mk_response(200, {"message": {"content": "ok"}})

        with patch(
            "tools.ollama_bootstrap.requests.post", side_effect=fake_post,
        ):
            verify_inference("llama3.1:8b")
        assert captured["json"]["options"]["num_predict"] == 1
        assert captured["json"]["stream"] is False


# ═════════════════════════════════════════════════════════════════════
# 5. _resolve_ollama_model
# ═════════════════════════════════════════════════════════════════════

class TestResolveOllamaModel:
    def test_default_id_resolves_to_ollama(self, isolated_model_store):
        record, err = _resolve_ollama_model(None)
        assert record is not None
        assert err == ""
        assert record["provider"] == "ollama"
        # Default model in registry
        assert record["id"] == "ollama-llama3-1-8b"

    def test_explicit_id_resolves(self, isolated_model_store):
        record, err = _resolve_ollama_model("ollama-llama3-1-8b")
        assert record is not None
        assert record["model_name"] == "llama3.1:8b"

    def test_unknown_id_fails(self, isolated_model_store):
        record, err = _resolve_ollama_model("nonexistent-model")
        assert record is None
        assert "Unknown model_id" in err

    def test_non_ollama_provider_rejected(self, isolated_model_store):
        # Anthropic models exist in the default registry - they MUST
        # be rejected by the Ollama bootstrap helper.
        record, err = _resolve_ollama_model("claude-haiku-4-5-20251001")
        assert record is None
        assert "ollama" in err.lower()


# ═════════════════════════════════════════════════════════════════════
# 6. bootstrap() orchestrator - happy path + every branch
# ═════════════════════════════════════════════════════════════════════

class TestBootstrapOrchestrator:
    def test_unresolvable_model_id_exits_one(self, isolated_model_store):
        log: list = []
        report = bootstrap(model_id="nope", log_fn=log.append)
        assert report.exit_code == 1
        assert report.detected is False
        assert any("Unknown model_id" in m for m in report.messages)

    def test_daemon_unreachable_exits_one_with_install_hint(
        self, isolated_model_store,
    ):
        log: list = []
        with patch(
            "tools.ollama_bootstrap.detect_ollama",
            return_value=OllamaStatus(
                reachable=False, endpoint="http://localhost:11434",
                error="Connection refused",
            ),
        ):
            report = bootstrap(log_fn=log.append)
        assert report.exit_code == 1
        assert report.detected is False
        # The install hint should appear in the log output
        log_text = "\n".join(log)
        assert "ollama" in log_text.lower()  # install hint mentions "ollama"

    def test_model_already_local_no_pull_needed(self, isolated_model_store):
        log: list = []
        with patch(
            "tools.ollama_bootstrap.detect_ollama",
            return_value=OllamaStatus(
                reachable=True, endpoint="http://localhost:11434",
                version="0.3.0",
            ),
        ), patch(
            "tools.ollama_bootstrap.list_local_models",
            return_value=["llama3.1:8b", "qwen2:7b"],
        ), patch(
            "tools.ollama_bootstrap.verify_inference",
            return_value=(True, "ok."),
        ):
            report = bootstrap(log_fn=log.append)
        assert report.exit_code == 0
        assert report.detected is True
        assert report.pull_attempted is False
        assert report.inference_succeeded is True
        assert report.inference_text == "ok."

    def test_model_missing_no_pull_flag_exits_one(self, isolated_model_store):
        log: list = []
        with patch(
            "tools.ollama_bootstrap.detect_ollama",
            return_value=OllamaStatus(
                reachable=True, endpoint="http://localhost:11434",
                version="0.3.0",
            ),
        ), patch(
            "tools.ollama_bootstrap.list_local_models",
            return_value=[],  # nothing local
        ):
            report = bootstrap(no_pull=True, log_fn=log.append)
        assert report.exit_code == 1
        assert report.pull_attempted is False
        assert any("not local" in m.lower() for m in report.messages)

    def test_model_missing_pull_succeeds_then_verifies(
        self, isolated_model_store,
    ):
        log: list = []
        with patch(
            "tools.ollama_bootstrap.detect_ollama",
            return_value=OllamaStatus(
                reachable=True, endpoint="http://localhost:11434",
                version="0.3.0",
            ),
        ), patch(
            "tools.ollama_bootstrap.list_local_models",
            side_effect=[[], ["llama3.1:8b"]],  # before, then after
        ), patch(
            "tools.ollama_bootstrap.pull_model",
            return_value=True,
        ), patch(
            "tools.ollama_bootstrap.verify_inference",
            return_value=(True, "ok."),
        ):
            report = bootstrap(auto_yes=True, log_fn=log.append)
        assert report.exit_code == 0
        assert report.pull_attempted is True
        assert report.pull_succeeded is True
        assert report.inference_succeeded is True
        assert report.locally_available_after == ["llama3.1:8b"]

    def test_model_missing_pull_fails_exits_one(self, isolated_model_store):
        log: list = []
        with patch(
            "tools.ollama_bootstrap.detect_ollama",
            return_value=OllamaStatus(
                reachable=True, endpoint="http://localhost:11434",
                version="0.3.0",
            ),
        ), patch(
            "tools.ollama_bootstrap.list_local_models",
            return_value=[],
        ), patch(
            "tools.ollama_bootstrap.pull_model",
            return_value=False,  # pull fails
        ):
            report = bootstrap(auto_yes=True, log_fn=log.append)
        assert report.exit_code == 1
        assert report.pull_attempted is True
        assert report.pull_succeeded is False
        assert report.inference_succeeded is False

    def test_verify_inference_fails_after_successful_pull(
        self, isolated_model_store,
    ):
        log: list = []
        with patch(
            "tools.ollama_bootstrap.detect_ollama",
            return_value=OllamaStatus(
                reachable=True, endpoint="http://localhost:11434",
                version="0.3.0",
            ),
        ), patch(
            "tools.ollama_bootstrap.list_local_models",
            return_value=["llama3.1:8b"],  # already local
        ), patch(
            "tools.ollama_bootstrap.verify_inference",
            return_value=(False, None),  # but inference fails
        ):
            report = bootstrap(log_fn=log.append)
        assert report.exit_code == 1
        assert report.detected is True
        assert report.inference_succeeded is False
        assert any("inference" in m.lower() for m in report.messages)

    def test_pull_declined_interactively(self, isolated_model_store):
        log: list = []
        with patch(
            "tools.ollama_bootstrap.detect_ollama",
            return_value=OllamaStatus(
                reachable=True, endpoint="http://localhost:11434",
                version="0.3.0",
            ),
        ), patch(
            "tools.ollama_bootstrap.list_local_models",
            return_value=[],
        ), patch(
            "tools.ollama_bootstrap._prompt_yes_no",
            return_value=False,  # operator declines
        ):
            report = bootstrap(log_fn=log.append)
        assert report.exit_code == 1
        assert report.pull_attempted is False  # never attempted

    def test_json_mode_suppresses_human_output(self, isolated_model_store):
        log: list = []
        with patch(
            "tools.ollama_bootstrap.detect_ollama",
            return_value=OllamaStatus(
                reachable=True, endpoint="http://localhost:11434",
                version="0.3.0",
            ),
        ), patch(
            "tools.ollama_bootstrap.list_local_models",
            return_value=["llama3.1:8b"],
        ), patch(
            "tools.ollama_bootstrap.verify_inference",
            return_value=(True, "ok."),
        ):
            report = bootstrap(output_json=True, log_fn=log.append)
        assert report.exit_code == 0
        # In JSON mode, log_fn should NOT have been called for the
        # human-readable progress messages
        assert log == []


# ═════════════════════════════════════════════════════════════════════
# 7. CLI wrapper main()
# ═════════════════════════════════════════════════════════════════════

class TestCLIMain:
    def test_argparse_exposes_documented_flags(self):
        parser = _build_argparser()
        # All four flags should be present
        ns = parser.parse_args([])
        assert ns.model is None
        assert ns.no_pull is False
        assert ns.yes is False
        assert ns.json is False
        ns2 = parser.parse_args(
            ["--model", "x", "--no-pull", "--yes", "--json"],
        )
        assert ns2.model == "x"
        assert ns2.no_pull is True
        assert ns2.yes is True
        assert ns2.json is True

    def test_main_returns_zero_on_full_success(self, isolated_model_store, capsys):
        with patch(
            "tools.ollama_bootstrap.detect_ollama",
            return_value=OllamaStatus(
                reachable=True, endpoint="http://localhost:11434",
                version="0.3.0",
            ),
        ), patch(
            "tools.ollama_bootstrap.list_local_models",
            return_value=["llama3.1:8b"],
        ), patch(
            "tools.ollama_bootstrap.verify_inference",
            return_value=(True, "ok."),
        ):
            rc = main([])
        assert rc == 0

    def test_main_returns_one_on_unreachable(self, isolated_model_store, capsys):
        with patch(
            "tools.ollama_bootstrap.detect_ollama",
            return_value=OllamaStatus(
                reachable=False, endpoint="http://localhost:11434",
                error="Connection refused",
            ),
        ):
            rc = main([])
        assert rc == 1

    def test_main_json_mode_emits_valid_json(self, isolated_model_store, capsys):
        with patch(
            "tools.ollama_bootstrap.detect_ollama",
            return_value=OllamaStatus(
                reachable=True, endpoint="http://localhost:11434",
                version="0.3.0",
            ),
        ), patch(
            "tools.ollama_bootstrap.list_local_models",
            return_value=["llama3.1:8b"],
        ), patch(
            "tools.ollama_bootstrap.verify_inference",
            return_value=(True, "ok."),
        ):
            rc = main(["--json"])
        assert rc == 0
        captured = capsys.readouterr()
        # The whole stdout is the JSON dump
        parsed = json.loads(captured.out)
        assert parsed["exit_code"] == 0
        assert parsed["detected"] is True
        assert parsed["inference_succeeded"] is True


# ═════════════════════════════════════════════════════════════════════
# 8. Drift guards
# ═════════════════════════════════════════════════════════════════════

class TestDriftGuards:
    def test_install_hints_cover_three_platforms(self):
        # Drift guard: any new platform support should remember to add
        # an install-hint entry.
        assert set(_INSTALL_HINTS.keys()) == {"darwin", "linux", "win32"}

    def test_install_hint_resolver_returns_non_empty_for_known_platforms(self):
        for plat in ("darwin", "linux", "win32"):
            with patch("sys.platform", plat):
                hint = _platform_install_hint()
                assert hint and "ollama" in hint.lower()

    def test_install_hint_unknown_platform_falls_back(self):
        with patch("sys.platform", "haiku-os"):
            hint = _platform_install_hint()
        assert hint and "ollama.com" in hint.lower()

    def test_bootstrap_report_dict_round_trips(self):
        report = BootstrapReport(
            model_id="x", model_name="y", endpoint="z",
            detected=True, ollama_version="0.3.0",
            locally_available_before=["a"],
            pull_attempted=True, pull_succeeded=True,
            locally_available_after=["a", "b"],
            inference_succeeded=True, inference_text="hi",
            exit_code=0, messages=["ok"],
        )
        d = report.to_dict()
        assert d["model_id"] == "x"
        assert d["exit_code"] == 0
        assert d["messages"] == ["ok"]
        # Ensure all dataclass fields surface in the dict
        assert set(d.keys()) >= {
            "model_id", "model_name", "endpoint", "detected",
            "ollama_version", "locally_available_before",
            "pull_attempted", "pull_succeeded",
            "locally_available_after", "inference_succeeded",
            "inference_text", "exit_code", "messages",
        }

    def test_no_silent_install_of_ollama_itself(self):
        # Drift guard against scope creep: this tool MUST NOT call brew /
        # apt / curl / sh / subprocess to install Ollama on the user's
        # behalf. Sandbox boundary documented in the module docstring.
        # Forbidden: actual code that invokes a shell. The install-hint
        # strings ("brew install ollama" etc.) are fine because they're
        # printed for the operator to run, not executed.
        from pathlib import Path
        path = Path(__file__).parent.parent / "tools" / "ollama_bootstrap.py"
        text = path.read_text()
        for forbidden in (
            "import subprocess", "from subprocess",
            "subprocess.run", "subprocess.call", "subprocess.Popen",
            "os.system",
            "shell=True",
        ):
            assert forbidden not in text, (
                f"tools/ollama_bootstrap.py must not contain {forbidden!r} "
                " - installation is the operator's responsibility (sandbox "
                "boundary)."
            )

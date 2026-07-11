"""
Tests for the Wave 3 (2026-04-25) manual-return loop:
``POST /api/alert-groups/<name>/manual-return`` + the parse/write
refactor that lets it reuse ``AlertGroupDispatcher`` internals.

Coverage
--------
* The picks parser is now pure: ``_parse_picks_block(text, group)``
  returns a normalized list with no I/O.
* The writer is reusable: ``_log_picks(picks, group, run_request_id,
  source, model_used)`` writes one row per pick via ``log_ag_pick``.
* The live dispatcher backfills ``source="claude"`` + ``model_used``
  via the orchestrator (proving Wave 3 didn't break the existing path).
* The endpoint:
    - rejects empty raw_text + missing model_used with 400
    - returns picks_parsed but writes 0 in dry_run mode
    - generates a synthetic ``manual:<group>:<utc>`` run_request_id when
      dispatch_run_id is omitted
    - honors a caller-supplied dispatch_run_id verbatim
    - 422s when no picks parse (with the preview echoing back so the
      operator can see what got rejected)
    - 404s on unknown alert group
* ``log_ag_pick`` accepts the new ``source`` and ``model_used`` kwargs
  with sensible defaults - old callers don't break.
* Frontend contract: every AG row in the rendered table now exposes
  an "Upload Brief" button wired to ``openManualReturn(g.name)``;
  modal markup + JS handlers are present in ui.html.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent

# Stand-in valid JSON pick block - every required key, valid values.
_VALID_PICK = {
    "idea_id": "polymarket:abc-def-2026:yes",
    "instrument_type": "polymarket",
    "instrument_id": "abc-def-2026",
    "direction": "YES",
    "conviction_pct": 80,
    "expected_return_pct": 12.5,
    "position_size_tier": "MEDIUM",
    "entry_price": 0.45,
    "suggested_buy_epoch": 1_780_000_000,
    "suggested_sell_epoch": 1_780_086_400,
    "exit_catalyst": "Election day arrives",
    "thesis": "Polling has tightened; market overweighted prior leader.",
    "pick_tier": "TOP",
}


def _make_response_text(picks: list[dict]) -> str:
    """Build the canonical end-of-response fenced JSON block format."""
    body = json.dumps(picks, indent=2)
    return (
        "Here are the picks for today.\n\n"
        f"```json\n{body}\n```\n"
    )


@pytest.fixture
def mr_setup(tmp_path):
    """Flask test client + isolated AG store + log_writer pointed at
    tmp_path so the manual-return write stays out of real ag_picks/.
    """
    from scheduled_input_engine import start_engine
    start_engine()

    from desktop_app.server import app, _ag_store
    import functionality.log_writer as lw

    orig_ag_dir = _ag_store._dir
    orig_ag_db = _ag_store._db
    orig_ag_runs = _ag_store._runs_db

    _ag_store._dir = tmp_path / "ag"
    _ag_store._db = str(tmp_path / "lc.sqlite")
    _ag_store._runs_db = str(tmp_path / "runs.sqlite")
    _ag_store.initialize()

    # Redirect log writer to tmp so manual-return writes don't pollute
    # real indexes/logs/ag_picks/.
    orig_root = lw._LOG_ROOT if hasattr(lw, "_LOG_ROOT") else None
    log_root = tmp_path / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    if hasattr(lw, "_LOG_ROOT"):
        lw._LOG_ROOT = log_root

    app.config["TESTING"] = True
    client = app.test_client()

    try:
        yield {"client": client, "tmp_path": tmp_path}
    finally:
        _ag_store._dir = orig_ag_dir
        _ag_store._db = orig_ag_db
        _ag_store._runs_db = orig_ag_runs
        _ag_store.initialize()
        if hasattr(lw, "_LOG_ROOT") and orig_root is not None:
            lw._LOG_ROOT = orig_root


def _create_ag(client, name, search_names=("ghost_search",)):
    return client.post(
        "/api/alert-groups/create",
        data=json.dumps({
            "name": name,
            "search_names": list(search_names),
            "prompt_text": "Analyze",
        }),
        content_type="application/json",
    )


# ── Pure parser refactor ──────────────────────────────────────────────────
class TestParserIsPure:
    def test_parse_picks_block_returns_normalized_list(self):
        from alert_groups.dispatcher import AlertGroupDispatcher
        text = _make_response_text([_VALID_PICK])
        result = AlertGroupDispatcher._parse_picks_block(
            response_text=text, group_name="purity_test",
        )
        assert isinstance(result, list)
        assert len(result) == 1
        # Every key the writer expects is present
        for required in (
            "rank_in_brief", "idea_id", "instrument_type",
            "instrument_id", "direction", "conviction_pct",
            "expected_return_pct", "position_size_tier",
            "entry_price", "suggested_buy_epoch",
            "suggested_sell_epoch", "hold_hours",
            "exit_catalyst", "thesis",
        ):
            assert required in result[0], f"missing {required}"

    def test_parse_picks_block_handles_missing_block(self):
        from alert_groups.dispatcher import AlertGroupDispatcher
        result = AlertGroupDispatcher._parse_picks_block(
            response_text="No JSON block here, just prose.",
            group_name="missing_block_test",
        )
        assert result == []

    def test_parse_picks_block_skips_invalid_picks(self):
        from alert_groups.dispatcher import AlertGroupDispatcher
        bad = dict(_VALID_PICK)
        bad["conviction_pct"] = 999  # out of [0,100]
        text = _make_response_text([_VALID_PICK, bad])
        result = AlertGroupDispatcher._parse_picks_block(
            response_text=text, group_name="filter_test",
        )
        assert len(result) == 1, (
            f"Expected only the valid pick to survive; got {len(result)}"
        )
        assert result[0]["idea_id"] == _VALID_PICK["idea_id"]

    def test_parse_picks_block_tolerates_malformed_json(self):
        from alert_groups.dispatcher import AlertGroupDispatcher
        result = AlertGroupDispatcher._parse_picks_block(
            response_text="```json\n[ not actually json ]\n```",
            group_name="malformed_test",
        )
        # Returns [] without raising - never crash the dispatch
        assert result == []


# ── Endpoint behaviour ────────────────────────────────────────────────────
class TestManualReturnEndpoint:
    def test_404_on_unknown_group(self, mr_setup):
        resp = mr_setup["client"].post(
            "/api/alert-groups/nonexistent_xyz/manual-return",
            data=json.dumps({"raw_text": "x", "model_used": "gpt-4o"}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_400_on_empty_raw_text(self, mr_setup):
        _create_ag(mr_setup["client"], "mr_empty_test")
        resp = mr_setup["client"].post(
            "/api/alert-groups/mr_empty_test/manual-return",
            data=json.dumps({"raw_text": "", "model_used": "gpt-4o"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "raw_text" in json.loads(resp.data)["message"]

    def test_400_on_missing_model_used(self, mr_setup):
        _create_ag(mr_setup["client"], "mr_nomodel_test")
        resp = mr_setup["client"].post(
            "/api/alert-groups/mr_nomodel_test/manual-return",
            data=json.dumps({"raw_text": "blah"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "model_used" in json.loads(resp.data)["message"]

    def test_dry_run_returns_preview_without_writing(self, mr_setup):
        _create_ag(mr_setup["client"], "mr_dry_test")
        text = _make_response_text([_VALID_PICK])

        with patch(
            "alert_groups.dispatcher.AlertGroupDispatcher._log_picks"
        ) as write_mock:
            resp = mr_setup["client"].post(
                "/api/alert-groups/mr_dry_test/manual-return",
                data=json.dumps({
                    "raw_text": text,
                    "model_used": "gpt-4o",
                    "dry_run": True,
                }),
                content_type="application/json",
            )
            data = json.loads(resp.data)
            assert resp.status_code == 200
            assert data["status"] == "success"
            assert data["dry_run"] is True
            assert data["picks_parsed"] == 1
            assert data["picks_written"] == 0
            assert len(data["preview"]) == 1
            assert write_mock.call_count == 0, (
                "_log_picks must NOT be called in dry_run mode"
            )

    def test_commit_writes_picks_with_source_manual(self, mr_setup):
        _create_ag(mr_setup["client"], "mr_commit_test")
        text = _make_response_text([_VALID_PICK])

        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return len(kwargs.get("normalized_picks", []))

        with patch(
            "alert_groups.dispatcher.AlertGroupDispatcher._log_picks",
            side_effect=_capture,
        ):
            resp = mr_setup["client"].post(
                "/api/alert-groups/mr_commit_test/manual-return",
                data=json.dumps({
                    "raw_text": text,
                    "model_used": "gpt-4o",
                }),
                content_type="application/json",
            )
        data = json.loads(resp.data)
        assert resp.status_code == 200, data
        assert data["status"] == "success"
        assert data["picks_written"] == 1
        assert data["source"] == "manual"
        assert data["model_used"] == "gpt-4o"
        # Synthetic id when dispatch_run_id omitted
        assert data["run_request_id"].startswith("manual:mr_commit_test:")
        # Capture confirms backend passed the right provenance
        assert captured["source"] == "manual"
        assert captured["model_used"] == "gpt-4o"

    def test_caller_supplied_dispatch_run_id_is_used_verbatim(
        self, mr_setup
    ):
        _create_ag(mr_setup["client"], "mr_linkid_test")
        text = _make_response_text([_VALID_PICK])
        with patch(
            "alert_groups.dispatcher.AlertGroupDispatcher._log_picks",
            return_value=1,
        ):
            resp = mr_setup["client"].post(
                "/api/alert-groups/mr_linkid_test/manual-return",
                data=json.dumps({
                    "raw_text": text,
                    "model_used": "gemini-2.5-pro",
                    "dispatch_run_id": "req_abc123_legit_claude_id",
                }),
                content_type="application/json",
            )
        data = json.loads(resp.data)
        assert data["run_request_id"] == "req_abc123_legit_claude_id"

    def test_422_when_no_picks_parse(self, mr_setup):
        _create_ag(mr_setup["client"], "mr_nopicks_test")
        # Garbage input - no picks block, not even malformed JSON
        resp = mr_setup["client"].post(
            "/api/alert-groups/mr_nopicks_test/manual-return",
            data=json.dumps({
                "raw_text": "This is just prose. No picks. No JSON.",
                "model_used": "gpt-4o",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 422
        data = json.loads(resp.data)
        assert "preview" in data  # echoed back so operator sees what failed
        assert data["preview"] == []


# ── Backfill: live dispatcher path passes source=claude + model ─────────
class TestDispatcherBackfill:
    def test_extract_and_log_picks_passes_source_claude(self):
        """Live dispatcher's _extract_and_log_picks must pass
        source='claude' + model_used down to _log_picks so historical
        rows have provenance even before Wave 3 manual returns."""
        from alert_groups.dispatcher import AlertGroupDispatcher
        text = _make_response_text([_VALID_PICK])
        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return 1

        with patch.object(
            AlertGroupDispatcher, "_log_picks", side_effect=_capture,
        ):
            n = AlertGroupDispatcher._extract_and_log_picks(
                response_text=text,
                group_name="backfill_test",
                run_request_id="req_test",
                model_used="claude-opus-4-7",
            )
        assert n == 1
        assert captured["source"] == "claude"
        assert captured["model_used"] == "claude-opus-4-7"


# ── log_ag_pick signature compatibility ──────────────────────────────────
class TestLogAgPickSignature:
    def test_log_ag_pick_accepts_source_and_model_used(self):
        """The new kwargs must be accepted with sensible defaults so
        old call sites (any third-party scripts that call log_ag_pick
        directly) don't break."""
        from functionality.log_writer import log_ag_pick
        # Should not raise. Use minimal valid args.
        try:
            log_ag_pick(
                alert_group="sig_test",
                run_request_id="req_x",
                rank_in_brief=1,
                idea_id="equity:spy:long",
                instrument_type="equity",
                instrument_id="spy",
                direction="LONG",
                conviction_pct=80,
                expected_return_pct=5.0,
                position_size_tier="SMALL",
                entry_price=500.0,
                suggested_buy_epoch=1_780_000_000,
                suggested_sell_epoch=1_780_086_400,
                hold_hours=24,
                source="manual",
                model_used="gpt-4o",
            )
        except TypeError as exc:
            pytest.fail(f"log_ag_pick rejected new kwargs: {exc}")

    def test_log_ag_pick_old_callsite_still_works(self):
        """Old callsites that don't pass source/model_used still work."""
        from functionality.log_writer import log_ag_pick
        try:
            log_ag_pick(
                alert_group="sig_test_old",
                run_request_id="req_old",
                rank_in_brief=1,
                idea_id="equity:tsla:short",
                instrument_type="equity",
                instrument_id="tsla",
                direction="SHORT",
                conviction_pct=70,
                expected_return_pct=3.0,
                position_size_tier="SMALL",
                entry_price=200.0,
                suggested_buy_epoch=1_780_000_000,
                suggested_sell_epoch=1_780_086_400,
                hold_hours=24,
            )
        except TypeError as exc:
            pytest.fail(
                f"log_ag_pick broke old call site (no source/model): {exc}"
            )


# ── Frontend contract regressions ────────────────────────────────────────
class TestFrontendContract:
    def _ui(self) -> str:
        return (REPO_ROOT / "desktop_app" / "ui.html").read_text(
            encoding="utf-8"
        )

    def test_ag_row_has_upload_brief_button(self):
        ui = self._ui()
        assert "Upload Brief" in ui, (
            "Wave 3 AG row must expose an 'Upload Brief' button."
        )
        assert "openManualReturn(g.name)" in ui, (
            "AG row's Upload Brief button must call openManualReturn() "
            "with the group name."
        )

    def test_modal_markup_present(self):
        ui = self._ui()
        for required in (
            'id="mr-modal"',
            'id="mr-model-select"',
            'id="mr-raw-text"',
            'id="mr-preview-btn"',
            'id="mr-submit-btn"',
        ):
            assert required in ui, f"Wave 3 modal missing element: {required}"

    def test_preview_uses_dry_run_param(self):
        ui = self._ui()
        # The preview path must POST with dry_run:true so backend doesn't
        # write. This is the contract the modal relies on.
        assert "dryRun: true" in ui, (
            "mr-preview-btn must call _mrSubmit({dryRun:true}) so the "
            "preview pane never accidentally commits."
        )

    def test_submit_uses_dry_run_false(self):
        ui = self._ui()
        assert "dryRun: false" in ui, (
            "mr-submit-btn must call _mrSubmit({dryRun:false}) so the "
            "commit path actually writes."
        )

    def test_endpoint_path_matches_backend(self):
        ui = self._ui()
        assert "/manual-return" in ui, (
            "Wave 3 modal must POST to the /manual-return endpoint."
        )

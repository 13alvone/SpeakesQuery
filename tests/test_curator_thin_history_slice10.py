"""Curator thin-history aggressive discovery - slice 10 tests.

Phase 6 / Bet 5 slice 10 (2026-05-17, speaktube req #12). Three
concerns:

1. **Dial injection** - the dispatcher substitutes ``$GROWTH_DIAL_VALUE``
   and ``$THIN_HISTORY_ACTIVE`` in the prompt at AG-fire time. Before
   slice 10 the prompt had hard-coded "defaults to -0.7" text and the
   operator's slider had zero effect on composition (req #12.3 audit).
2. **Thin-history detection** - at dispatch time, sum
   ``watched_seconds`` from ``curator_telemetry`` for the trailing
   30 days. If below the threshold, thin-history is active.
3. **Dial boost** - when thin-history active, effective_dial =
   clamp(stored_dial + bias, -1.0, +1.0). The LLM sees the boosted
   value; the playlist parquet records it; the API surfaces both
   effective + stored + state.

The schema additivity guard for ``thin_history_active`` lives in
``tests/test_curator_speaktube_slice1.py::test_curator_playlist_schema_is_additive_only``
(extended in this slice).
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def isolated_immutable(tmp_path, monkeypatch):
    """Redirect immutable_dir() to a temp path + reset log writer."""
    from global_settings import get_settings
    from functionality.log_writer import LogWriter
    s = get_settings()
    s.set("immutable_root", str(tmp_path / "IMM"))
    LogWriter.reset_for_tests()
    yield tmp_path / "IMM"
    s.reset("immutable_root")
    LogWriter.reset_for_tests()


@pytest.fixture(autouse=True)
def _isolated_settings():
    """Reset slice-10 settings between tests so per-test sets don't
    leak across tests."""
    from global_settings import get_settings
    s = get_settings()
    yield
    for key in (
        "curator_growth_dial",
        "curator_thin_history_enabled",
        "curator_thin_history_threshold_seconds",
        "curator_thin_history_dial_bias",
    ):
        try:
            s.reset(key)
        except Exception:
            pass


# ── 1. Settings registration + 5-place drift ────────────────────────


def test_thin_history_settings_defaults():
    """Slice 10 default-value drift guard."""
    from global_settings import DEFAULTS
    assert DEFAULTS["curator_thin_history_enabled"] is True
    assert DEFAULTS["curator_thin_history_threshold_seconds"] == 18000
    assert DEFAULTS["curator_thin_history_dial_bias"] == pytest.approx(0.5)


def test_thin_history_validators_enforce_ranges():
    """All three settings + their validator ranges."""
    from global_settings import _validate_key, DEFAULTS

    # enabled (bool)
    assert _validate_key("curator_thin_history_enabled", True, DEFAULTS) is None
    assert _validate_key("curator_thin_history_enabled", False, DEFAULTS) is None
    assert _validate_key("curator_thin_history_enabled", 1, DEFAULTS) is not None
    assert _validate_key("curator_thin_history_enabled", "true", DEFAULTS) is not None

    # threshold_seconds (int)
    assert _validate_key("curator_thin_history_threshold_seconds", 0, DEFAULTS) is None
    assert _validate_key("curator_thin_history_threshold_seconds", 18000, DEFAULTS) is None
    assert _validate_key("curator_thin_history_threshold_seconds", 2_592_000, DEFAULTS) is None
    assert _validate_key("curator_thin_history_threshold_seconds", -1, DEFAULTS) is not None
    assert _validate_key("curator_thin_history_threshold_seconds", 2_592_001, DEFAULTS) is not None
    assert _validate_key("curator_thin_history_threshold_seconds", 1.5, DEFAULTS) is not None

    # dial_bias (float)
    assert _validate_key("curator_thin_history_dial_bias", 0.0, DEFAULTS) is None
    assert _validate_key("curator_thin_history_dial_bias", 0.5, DEFAULTS) is None
    assert _validate_key("curator_thin_history_dial_bias", 2.0, DEFAULTS) is None
    assert _validate_key("curator_thin_history_dial_bias", -0.1, DEFAULTS) is not None
    assert _validate_key("curator_thin_history_dial_bias", 2.1, DEFAULTS) is not None
    assert _validate_key("curator_thin_history_dial_bias", True, DEFAULTS) is not None


def test_thin_history_defaults_yaml_mirrors_in_code():
    """5-place drift: defaults.yaml mirrors in-code DEFAULTS."""
    import yaml
    root = Path(__file__).resolve().parent.parent
    with open(root / "global_settings.defaults.yaml") as f:
        yaml_defaults = yaml.safe_load(f)
    assert "curator_thin_history_enabled" in yaml_defaults
    assert "curator_thin_history_threshold_seconds" in yaml_defaults
    assert "curator_thin_history_dial_bias" in yaml_defaults


def test_thin_history_ui_registered():
    """5-place drift: UI inputs + JS settings-map entries."""
    root = Path(__file__).resolve().parent.parent
    ui = (root / "desktop_app" / "ui.html").read_text()
    # HTML input elements
    assert 'id="set-curator-thin-history-enabled"' in ui
    assert 'id="set-curator-thin-history-threshold-seconds"' in ui
    assert 'id="set-curator-thin-history-dial-bias"' in ui
    # JS settings-map entries
    assert "'curator_thin_history_enabled'" in ui
    assert "'curator_thin_history_threshold_seconds'" in ui
    assert "'curator_thin_history_dial_bias'" in ui


# ── 2. Schema additivity ────────────────────────────────────────────


def test_curator_playlist_schema_includes_thin_history_active():
    """Slice 10 schema addition."""
    from functionality.log_writer import SCHEMAS
    assert "thin_history_active" in SCHEMAS["curator_playlist"]


# ── 3. Effective-dial math ──────────────────────────────────────────


def test_effective_dial_no_boost_when_thin_history_inactive():
    from alert_groups.dispatcher import AlertGroupDispatcher
    from global_settings import get_settings
    get_settings().set("curator_thin_history_dial_bias", 0.5)
    val = AlertGroupDispatcher._compute_effective_growth_dial(
        stored_dial=-0.7, thin_history_active=False,
    )
    assert val == pytest.approx(-0.7)


def test_effective_dial_boost_when_thin_history_active():
    from alert_groups.dispatcher import AlertGroupDispatcher
    from global_settings import get_settings
    get_settings().set("curator_thin_history_dial_bias", 0.5)
    # -0.7 + 0.5 = -0.2
    val = AlertGroupDispatcher._compute_effective_growth_dial(
        stored_dial=-0.7, thin_history_active=True,
    )
    assert val == pytest.approx(-0.2)


def test_effective_dial_clamped_at_upper_bound():
    """Even with max bias + already-positive stored, never exceeds +1.0."""
    from alert_groups.dispatcher import AlertGroupDispatcher
    from global_settings import get_settings
    get_settings().set("curator_thin_history_dial_bias", 2.0)
    val = AlertGroupDispatcher._compute_effective_growth_dial(
        stored_dial=0.5, thin_history_active=True,
    )
    assert val == pytest.approx(1.0)


def test_effective_dial_clamped_at_lower_bound():
    """Stored dial at lower bound with zero bias stays at -1.0."""
    from alert_groups.dispatcher import AlertGroupDispatcher
    from global_settings import get_settings
    get_settings().set("curator_thin_history_dial_bias", 0.0)
    val = AlertGroupDispatcher._compute_effective_growth_dial(
        stored_dial=-1.0, thin_history_active=True,
    )
    assert val == pytest.approx(-1.0)


# ── 4. Thin-history detection (telemetry query) ─────────────────────


def test_detection_inactive_when_setting_disabled(isolated_immutable):
    from alert_groups.dispatcher import AlertGroupDispatcher
    from global_settings import get_settings
    get_settings().set("curator_thin_history_enabled", False)
    active, watched = AlertGroupDispatcher._compute_curator_thin_history()
    assert active is False
    assert watched == 0


def test_detection_active_when_no_telemetry_exists(isolated_immutable):
    """Fresh install - no telemetry parquet on disk → 0 watched → active.
    Aggressive discovery from day 1 for a new account is the desired
    behavior (lots of exploration until we have signal)."""
    from alert_groups.dispatcher import AlertGroupDispatcher
    from global_settings import get_settings
    get_settings().set("curator_thin_history_enabled", True)
    get_settings().set("curator_thin_history_threshold_seconds", 18000)
    active, watched = AlertGroupDispatcher._compute_curator_thin_history()
    assert active is True
    assert watched == 0


def test_detection_inactive_when_above_threshold(isolated_immutable):
    """Seed telemetry with > threshold seconds - thin-history off."""
    import time as _time
    from alert_groups.dispatcher import AlertGroupDispatcher
    from functionality.log_writer import log_curator_telemetry, flush_all
    from global_settings import get_settings

    get_settings().set("curator_thin_history_enabled", True)
    get_settings().set("curator_thin_history_threshold_seconds", 100)

    # 5 play_end events × 50s each = 250s total > 100s threshold
    for i in range(5):
        log_curator_telemetry(
            event_ts_iso=f"2026-05-{17 - i}T09:00:00+00:00",
            event_date=f"2026-05-{17 - i}",
            event_type="play_end",
            video_external_id=f"vid{i}",
            chosen_by="curator",
            watched_seconds=50,
            total_seconds=100,
        )
    flush_all()

    active, watched = AlertGroupDispatcher._compute_curator_thin_history()
    assert active is False, f"expected inactive (watched={watched}, threshold=100)"
    assert watched == 250


def test_detection_active_when_below_threshold(isolated_immutable):
    """Seed telemetry with < threshold - thin-history on."""
    from alert_groups.dispatcher import AlertGroupDispatcher
    from functionality.log_writer import log_curator_telemetry, flush_all
    from global_settings import get_settings

    get_settings().set("curator_thin_history_enabled", True)
    get_settings().set("curator_thin_history_threshold_seconds", 1000)

    # 2 events × 100s = 200s < 1000s threshold
    for i in range(2):
        log_curator_telemetry(
            event_ts_iso=f"2026-05-{17 - i}T09:00:00+00:00",
            event_date=f"2026-05-{17 - i}",
            event_type="play_end",
            video_external_id=f"vid{i}",
            chosen_by="curator",
            watched_seconds=100,
            total_seconds=100,
        )
    flush_all()

    active, watched = AlertGroupDispatcher._compute_curator_thin_history()
    assert active is True
    assert watched == 200


def test_detection_ignores_events_older_than_30_days(isolated_immutable):
    """Sum trailing 30 days only - events from 60 days ago don't count."""
    import time as _time
    from alert_groups.dispatcher import AlertGroupDispatcher
    from functionality.log_writer import log_curator_telemetry, flush_all
    from global_settings import get_settings
    from functionality.log_writer import LogWriter

    get_settings().set("curator_thin_history_enabled", True)
    get_settings().set("curator_thin_history_threshold_seconds", 1000)

    # Seed an OLD event (60 days ago) - should NOT count toward sum.
    # Use the LogWriter's _epoch directly since log_curator_telemetry
    # only auto-fills now-epoch; we mock by emitting via emit().
    from functionality.log_writer import emit
    old_epoch = int(_time.time()) - (60 * 86400)
    emit("curator_telemetry", {
        "_epoch": old_epoch,
        "event_ts_iso": "2026-03-17T09:00:00+00:00",
        "event_date": "2026-03-17",
        "event_type": "play_end",
        "video_external_id": "old_vid",
        "chosen_by": "curator",
        "watched_seconds": 5000,  # huge - would push over threshold IF counted
        "total_seconds": 5000,
    })
    flush_all()

    active, watched = AlertGroupDispatcher._compute_curator_thin_history()
    # Should be active (the 5000s of OLD watch doesn't count, we have 0 recent)
    assert active is True
    assert watched == 0


# ── 5. Dispatcher dial injection (prompt placeholder substitution) ──


class TestDialInjection:
    """The dispatcher MUST substitute $GROWTH_DIAL_VALUE +
    $THIN_HISTORY_ACTIVE placeholders BEFORE the prompt reaches Claude.

    Pre-slice-10 the prompt had hard-coded "defaults to -0.7" text and
    the LLM had no way to see the operator's actual slider setting.
    """

    def test_dispatcher_substitutes_placeholders_for_playlist_ags(self):
        """Read the source - the deep dispatch pipeline must contain
        the substitution branch under ``output_kind == "playlist"``.
        The injection lives in ``_run_inner`` (per the run /
        _run_locked / _run_inner audit refactor)."""
        import inspect
        from alert_groups.dispatcher import AlertGroupDispatcher
        # The injection logic lives in _run_inner (the deepest
        # method in the run → _run_locked → _run_inner chain).
        src = inspect.getsource(AlertGroupDispatcher._run_inner)
        # The substitution must be guarded by output_kind==playlist
        # AND reference both placeholders.
        assert "$GROWTH_DIAL_VALUE" in src, (
            "_run_inner() must substitute $GROWTH_DIAL_VALUE in prompt_text"
        )
        assert "$THIN_HISTORY_ACTIVE" in src, (
            "_run_inner() must substitute $THIN_HISTORY_ACTIVE in prompt_text"
        )
        assert "_compute_curator_thin_history" in src, (
            "_run_inner() must call _compute_curator_thin_history for playlist AGs"
        )
        assert "_compute_effective_growth_dial" in src, (
            "_run_inner() must call _compute_effective_growth_dial for playlist AGs"
        )

    def test_default_ag_prompt_uses_placeholders_not_hardcoded_default(self):
        """Drift guard: the shipped composer prompt must contain the
        $GROWTH_DIAL_VALUE placeholder (NOT a hard-coded "-0.7" or
        "defaults to" phrase that the dispatcher can't substitute)."""
        import yaml
        root = Path(__file__).resolve().parent.parent
        with open(root / "default_alert_groups" / "curator_playlist_composer.yaml") as f:
            ag = yaml.safe_load(f)
        prompt_text = ag.get("prompt_text", "")
        assert "$GROWTH_DIAL_VALUE" in prompt_text, (
            "Composer prompt missing $GROWTH_DIAL_VALUE placeholder"
        )
        assert "$THIN_HISTORY_ACTIVE" in prompt_text, (
            "Composer prompt missing $THIN_HISTORY_ACTIVE placeholder"
        )


# ── 6. Effective-dial OVERRIDE in parsed output ─────────────────────


def test_extract_and_log_overrides_llm_echoed_dial(isolated_immutable):
    """When the dispatcher passes effective_growth_dial, that value
    OVERRIDES whatever the LLM echoed back. The LLM's echo is for
    audit only; the dispatcher's truth is what's logged."""
    from alert_groups.dispatcher import AlertGroupDispatcher

    # LLM echoes 0.3 but dispatcher had injected -0.2 (boosted)
    llm_response = (
        "```json\n"
        "{\n"
        '  "run_date": "2026-05-17",\n'
        '  "growth_dial": 0.3,\n'
        '  "items": [\n'
        '    {"position": 1, "slot_kind": "main", "rationale": "r",\n'
        '     "video_external_id": "v1", "title": "T", "channel_name": "C"}\n'
        "  ]\n"
        "}\n"
        "```"
    )
    AlertGroupDispatcher._extract_and_log_playlist(
        response_text=llm_response,
        group_name="curator_playlist_composer",
        run_request_id="slice10-override",
        effective_growth_dial=-0.2,
        thin_history_active=True,
    )
    parquets = list((isolated_immutable / "curator_playlist").rglob("*.parquet"))
    df = pd.concat([pd.read_parquet(p) for p in parquets], ignore_index=True)
    assert float(df.iloc[0]["growth_dial"]) == pytest.approx(-0.2)


def test_extract_and_log_falls_back_to_llm_echo_when_effective_none(isolated_immutable):
    """Back-compat: when effective_growth_dial=None (the slice-6..9
    test call path), the LLM's echoed value is preserved."""
    from alert_groups.dispatcher import AlertGroupDispatcher

    llm_response = (
        "```json\n"
        "{\n"
        '  "run_date": "2026-05-17",\n'
        '  "growth_dial": 0.42,\n'
        '  "items": [\n'
        '    {"position": 1, "slot_kind": "main", "rationale": "r",\n'
        '     "video_external_id": "v1", "title": "T", "channel_name": "C"}\n'
        "  ]\n"
        "}\n"
        "```"
    )
    AlertGroupDispatcher._extract_and_log_playlist(
        response_text=llm_response,
        group_name="curator_playlist_composer",
        run_request_id="slice10-fallback",
        # effective_growth_dial intentionally omitted
    )
    parquets = list((isolated_immutable / "curator_playlist").rglob("*.parquet"))
    df = pd.concat([pd.read_parquet(p) for p in parquets], ignore_index=True)
    assert float(df.iloc[0]["growth_dial"]) == pytest.approx(0.42)


# ── 7. thin_history_active flows to log_writer + parquet ────────────


def test_thin_history_active_lands_in_parquet(isolated_immutable):
    """The dispatcher-passed thin_history_active flag reaches every
    playlist row in the parquet."""
    from alert_groups.dispatcher import AlertGroupDispatcher

    llm_response = (
        "```json\n"
        "{\n"
        '  "run_date": "2026-05-17",\n'
        '  "growth_dial": -0.2,\n'
        '  "items": [\n'
        '    {"position": 1, "slot_kind": "main", "rationale": "r1",\n'
        '     "video_external_id": "v1", "title": "T1", "channel_name": "C1"},\n'
        '    {"position": 2, "slot_kind": "surprise", "rationale": "r2",\n'
        '     "video_external_id": "v2", "title": "T2", "channel_name": "C2"}\n'
        "  ]\n"
        "}\n"
        "```"
    )
    AlertGroupDispatcher._extract_and_log_playlist(
        response_text=llm_response,
        group_name="curator_playlist_composer",
        run_request_id="slice10-flag",
        effective_growth_dial=-0.2,
        thin_history_active=True,
    )
    parquets = list((isolated_immutable / "curator_playlist").rglob("*.parquet"))
    df = pd.concat([pd.read_parquet(p) for p in parquets], ignore_index=True)
    # Every row carries thin_history_active=True
    assert "thin_history_active" in df.columns
    assert all(df["thin_history_active"] == True), (  # noqa: E712
        f"expected all True, got: {df['thin_history_active'].tolist()}"
    )


# ── 8. API contract - /api/playlist/today extended ──────────────────


def test_api_playlist_today_surfaces_thin_history_state(isolated_immutable, client):
    """/api/playlist/today response carries thin_history_active +
    growth_dial_stored alongside the existing growth_dial."""
    from functionality.log_writer import log_curator_playlist_item, flush_all
    from global_settings import get_settings

    # Operator's stored dial
    get_settings().set("curator_growth_dial", -0.5)

    # Write a composition with thin-history active + effective dial 0.0
    composed_at = "2026-05-17T05:00:00Z"
    log_curator_playlist_item(
        run_date="2026-05-17", composed_at_iso=composed_at,
        growth_dial=0.0,  # effective (boosted from -0.5 by +0.5)
        position=1, slot_kind="main", rationale="thin-pick",
        external_id="v1",
        url="https://www.youtube.com/watch?v=v1",
        title="t1", channel_name="c1",
        thin_history_active=True,
    )
    flush_all()

    resp = client.get("/api/playlist/today")
    assert resp.status_code == 200
    payload = resp.get_json()
    # Effective dial (from parquet)
    assert payload["growth_dial"] == pytest.approx(0.0)
    # Stored dial (from settings)
    assert payload["growth_dial_stored"] == pytest.approx(-0.5)
    # Thin-history state from parquet
    assert payload["thin_history_active"] is True


def test_api_playlist_today_thin_history_false_for_pre_slice10_rows(
    isolated_immutable, client,
):
    """Back-compat: parquet rows written before slice 10 (no
    thin_history_active column) → API returns False (not null) per
    the speaktube contract (always-present-bool)."""
    from functionality.log_writer import log_curator_playlist_item, flush_all

    log_curator_playlist_item(
        run_date="2026-05-17",
        composed_at_iso="2026-05-17T05:00:00Z",
        growth_dial=-0.7,
        position=1, slot_kind="main", rationale="r",
        external_id="v1",
        url="https://www.youtube.com/watch?v=v1",
        title="t1", channel_name="c1",
        # thin_history_active intentionally omitted (defaults to False)
    )
    flush_all()

    resp = client.get("/api/playlist/today")
    assert resp.status_code == 200
    assert resp.get_json()["thin_history_active"] is False

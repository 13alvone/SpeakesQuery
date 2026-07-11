"""
Timezone-aware scheduling - 2026-04-27
======================================
Pins the per-AG / per-saved-search ``timezone:`` field added 2026-04-27.

Three goals:

1. **Display correctness.** ``_get_next_run`` returns a TZ-aware ISO string
   (offset suffix like ``+00:00`` / ``-04:00``) so the SPA's
   ``new Date(iso)`` parser converts to browser-local time correctly.
   Pre-fix, the naive ISO was misparsed as browser-local - a 7-hour lie
   for a PT user against a UTC scheduler.
2. **Scheduler honors the field.** ``register_alert_group_jobs`` and
   ``QueryEngine.schedule_tasks`` pass ``timezone=ZoneInfo(tz)`` to
   ``CronTrigger.from_crontab`` so DST transitions are handled
   automatically.
3. **Backward compat.** Every AG / saved search written before the field
   existed loads cleanly with default timezone="UTC" - no migration
   required, no behavior change.

Plus a frontend-contract drift guard so the JS dropdown stays wired.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def ag_store(tmp_dir):
    """Isolated AlertGroupStore with empty defaults dir so initialize()'s
    _seed_defaults() is a no-op (added 2026-04-30 when AGs adopted the
    default-shipping pattern)."""
    from alert_group_store import AlertGroupStore
    empty_defaults = Path(tmp_dir) / "_empty_default_alert_groups"
    empty_defaults.mkdir()
    store = AlertGroupStore()
    store._dir = Path(tmp_dir) / "alert_groups"
    store._defaults_dir = empty_defaults
    store._db = str(Path(tmp_dir) / "last_chance.sqlite")
    store._runs_db = str(Path(tmp_dir) / "alert_group_runs.sqlite")
    store.initialize()
    return store


@pytest.fixture
def ss_store(tmp_dir):
    from saved_search_store import SavedSearchStore
    store = SavedSearchStore()
    store._dir = Path(tmp_dir) / "saved_searches"
    store._defaults_dir = Path(tmp_dir) / "default_saved_searches"
    store._db = str(Path(tmp_dir) / "last_chance.sqlite")
    return store


def _ag_payload(**overrides):
    """Minimal valid AG payload for save_group()."""
    base = {
        "name": "test_tz_ag",
        "description": "tz test",
        "search_names": ["search_a"],
        "prompt_text": "Test prompt body.",
        "schedule": "30 10 * * 1-5",
        "max_rows": 50,
        "email_address": "test@example.com",
    }
    base.update(overrides)
    return base


def _ss_payload(**overrides):
    """Minimal valid saved-search payload for save_search()."""
    base = {
        "name": "test_tz_ss",
        "description": "tz test",
        "query": "index=\"foo/*.parquet\" | head 1",
        "cron_schedule": "30 10 * * 1-5",
        "lookback": "-1h",
        "trigger": "once",
        "email_address": "noreply@speakesquery.local",
        "send_email": "no",
        "purpose": "alert_group_feeder",
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────
# Validators
# ─────────────────────────────────────────────────────────────────────

class TestTimezoneValidator:
    """Both AG and SavedSearch validators share the same vocabulary."""

    @pytest.mark.parametrize("validator_cls", [
        "validation.AlertGroupValidation.AlertGroupValidation",
        "validation.SavedSearchValidation.SavedSearchValidation",
    ])
    @pytest.mark.parametrize("good_zone", [
        "UTC", "America/New_York", "America/Los_Angeles",
        "Europe/London", "Europe/Berlin", "Asia/Tokyo",
        "Australia/Sydney", "Pacific/Auckland",
    ])
    def test_iana_zones_accepted(self, validator_cls, good_zone):
        module_name, cls_name = validator_cls.rsplit(".", 1)
        import importlib
        cls = getattr(importlib.import_module(module_name), cls_name)
        assert cls.validate_timezone(good_zone) == good_zone

    @pytest.mark.parametrize("validator_cls", [
        "validation.AlertGroupValidation.AlertGroupValidation",
        "validation.SavedSearchValidation.SavedSearchValidation",
    ])
    @pytest.mark.parametrize("missing", [None, "", "   "])
    def test_missing_defaults_to_utc(self, validator_cls, missing):
        module_name, cls_name = validator_cls.rsplit(".", 1)
        import importlib
        cls = getattr(importlib.import_module(module_name), cls_name)
        assert cls.validate_timezone(missing) == "UTC"

    @pytest.mark.parametrize("validator_cls", [
        "validation.AlertGroupValidation.AlertGroupValidation",
        "validation.SavedSearchValidation.SavedSearchValidation",
    ])
    @pytest.mark.parametrize("bad_zone", [
        "Mars/Olympus_Mons",
        "Not_A_Zone",
        "America/SpaceCity",
        # Bare offsets are rejected - APScheduler + croniter need a full
        # IANA zone to know about DST transitions.
        "-07:00",
        "+0530",
    ])
    def test_invalid_zones_rejected(self, validator_cls, bad_zone):
        module_name, cls_name = validator_cls.rsplit(".", 1)
        import importlib
        cls = getattr(importlib.import_module(module_name), cls_name)
        with pytest.raises(ValueError, match=r"(?i)invalid timezone|iana"):
            cls.validate_timezone(bad_zone)


# ─────────────────────────────────────────────────────────────────────
# AG store - round-trip + tz-aware ISO
# ─────────────────────────────────────────────────────────────────────

class TestAGStoreTimezone:

    def test_save_and_load_round_trip(self, ag_store):
        rec = ag_store.save_group(_ag_payload(timezone="America/New_York"))
        assert rec["timezone"] == "America/New_York"
        loaded = ag_store.get_group("test_tz_ag")
        assert loaded["timezone"] == "America/New_York"

    def test_missing_timezone_defaults_to_utc(self, ag_store):
        # Mimic a YAML written before the field existed: save without it.
        rec = ag_store.save_group(_ag_payload())
        assert rec["timezone"] == "UTC"
        loaded = ag_store.get_group("test_tz_ag")
        assert loaded["timezone"] == "UTC"

    def test_invalid_timezone_rejected(self, ag_store):
        with pytest.raises(ValueError, match=r"(?i)invalid timezone"):
            ag_store.save_group(_ag_payload(timezone="Mars/Olympus_Mons"))

    def test_update_changes_timezone(self, ag_store):
        ag_store.save_group(_ag_payload(timezone="UTC"))
        updated = ag_store.update_group(
            "test_tz_ag", {"timezone": "Europe/London"}
        )
        assert updated["timezone"] == "Europe/London"

    def test_next_run_iso_carries_offset(self, ag_store):
        """The display-bug fix: ISO must end with +HH:MM (or Z)."""
        ag_store.save_group(_ag_payload(timezone="America/New_York"))
        loaded = ag_store.get_group("test_tz_ag")
        nrt = loaded["next_run_time"]
        assert nrt, "next_run_time should be populated"
        # Tz-aware ISO: ends with +HH:MM, -HH:MM, or Z.
        assert re.search(r"([+-]\d{2}:\d{2}|Z)$", nrt), (
            f"next_run_time {nrt!r} is naive - JS will misparse as "
            f"browser-local"
        )

    def test_legacy_yaml_without_timezone_loads_with_utc_offset(
        self, ag_store, tmp_dir
    ):
        """A YAML written before the field existed must still produce a
        TZ-aware ISO. This is the back-compat path that silently fixes
        every existing AG without any migration."""
        legacy_yaml = (
            "name: legacy_ag\n"
            "description: pre-tz-field record\n"
            "search_names:\n  - foo\n"
            "prompt_text: |\n  legacy prompt\n"
            "schedule: '30 10 * * 1-5'\n"
            "max_rows: 50\n"
            "email_address: x@y.com\n"
            "disabled: false\n"
            "delivery_mode: api\n"
            "created_at: '2026-01-01T00:00:00'\n"
            "updated_at: '2026-01-01T00:00:00'\n"
        )
        path = ag_store._dir / "legacy_ag.yaml"
        path.write_text(legacy_yaml, encoding="utf-8")
        loaded = ag_store.get_group("legacy_ag")
        assert loaded.get("timezone", "UTC") == "UTC"
        assert re.search(r"\+00:00$", loaded["next_run_time"]), (
            "legacy YAML without timezone must still emit UTC-offset ISO"
        )


# ─────────────────────────────────────────────────────────────────────
# Saved-search store - same coverage
# ─────────────────────────────────────────────────────────────────────

class TestSavedSearchStoreTimezone:

    def test_save_and_load_round_trip(self, ss_store):
        rec = ss_store.save_search(_ss_payload(timezone="Europe/London"))
        assert rec["timezone"] == "Europe/London"
        loaded = ss_store.get_search("test_tz_ss")
        assert loaded["timezone"] == "Europe/London"

    def test_missing_timezone_defaults_to_utc(self, ss_store):
        rec = ss_store.save_search(_ss_payload())
        assert rec["timezone"] == "UTC"

    def test_invalid_timezone_rejected(self, ss_store):
        with pytest.raises(ValueError, match=r"(?i)invalid timezone"):
            ss_store.save_search(_ss_payload(timezone="Not_A_Zone"))

    def test_next_run_iso_carries_offset(self, ss_store):
        ss_store.save_search(_ss_payload(timezone="America/New_York"))
        loaded = ss_store.get_search("test_tz_ss")
        nrt = loaded["next_run_time"]
        assert nrt
        assert re.search(r"([+-]\d{2}:\d{2}|Z)$", nrt)


# ─────────────────────────────────────────────────────────────────────
# DST boundaries - the whole point of using IANA zones
# ─────────────────────────────────────────────────────────────────────

class TestDSTBoundaries:
    """The cron `30 10 * * 1-5` in America/New_York must fire at 10:30 ET
    every weekday - meaning 14:30 UTC in EDT (Mar–Nov) and 15:30 UTC in
    EST (Nov–Mar). A naïve UTC cron drifts an hour twice a year; an
    IANA-zoned cron does not."""

    def test_summer_edt_fires_at_1430_utc(self):
        from croniter import croniter
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
        # Anchor: Tue Apr 28 2026 09:00 EDT = 13:00 UTC
        anchor = datetime(2026, 4, 28, 13, 0, tzinfo=timezone.utc).astimezone(ny)
        nxt = croniter("30 10 * * 1-5", anchor).get_next(datetime)
        assert nxt.astimezone(timezone.utc).hour == 14
        assert nxt.astimezone(timezone.utc).minute == 30
        assert nxt.astimezone(ny).hour == 10
        assert nxt.astimezone(ny).minute == 30

    def test_winter_est_fires_at_1530_utc(self):
        from croniter import croniter
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
        # Anchor: Mon Dec 1 2025 09:00 EST = 14:00 UTC
        anchor = datetime(2025, 12, 1, 14, 0, tzinfo=timezone.utc).astimezone(ny)
        nxt = croniter("30 10 * * 1-5", anchor).get_next(datetime)
        # In EST the same wall-clock 10:30 ET is 15:30 UTC, NOT 14:30.
        assert nxt.astimezone(timezone.utc).hour == 15
        assert nxt.astimezone(timezone.utc).minute == 30
        # But the ET wall-clock is still 10:30 - DST transparency.
        assert nxt.astimezone(ny).hour == 10
        assert nxt.astimezone(ny).minute == 30

    def test_spring_forward_no_skip_no_dup(self):
        """The Sunday before spring-forward (Mar 8, 2026) is the trickiest
        case. A weekday cron should hit the next Monday at the same wall
        clock."""
        from croniter import croniter
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
        # Anchor: Sun Mar 8 2026 02:30 (during the lost hour) - pick a safe
        # Sunday-evening anchor instead.
        anchor = datetime(2026, 3, 8, 23, 0, tzinfo=ny)
        # Spring-forward already happened at 02:00 → 03:00 EST→EDT.
        nxt = croniter("30 10 * * 1-5", anchor).get_next(datetime)
        # Next is Mon Mar 9 10:30 EDT = 14:30 UTC.
        assert nxt.astimezone(ny).hour == 10
        assert nxt.astimezone(ny).minute == 30
        assert nxt.astimezone(timezone.utc).hour == 14

    def test_fall_back_no_skip_no_dup(self):
        from croniter import croniter
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
        # Anchor: Sun Nov 1 2026 23:00 ET (after fall-back).
        anchor = datetime(2026, 11, 1, 23, 0, tzinfo=ny)
        nxt = croniter("30 10 * * 1-5", anchor).get_next(datetime)
        # Mon Nov 2 10:30 EST = 15:30 UTC.
        assert nxt.astimezone(ny).hour == 10
        assert nxt.astimezone(ny).minute == 30
        assert nxt.astimezone(timezone.utc).hour == 15


# ─────────────────────────────────────────────────────────────────────
# Scheduler wiring - registration must use timezone=
# ─────────────────────────────────────────────────────────────────────

class TestSchedulerTimezoneWiring:
    """The dispatcher must pass ``timezone=ZoneInfo(tz)`` to
    ``CronTrigger.from_crontab``. Without this, the per-AG ``timezone:``
    field is dead config."""

    def test_ag_scheduler_passes_timezone(self, ag_store, monkeypatch):
        """Register an AG with timezone=Europe/London; assert the trigger
        we add to the scheduler carries that zone."""
        ag_store.save_group(_ag_payload(timezone="Europe/London"))

        from alert_groups.scheduler import register_alert_group_jobs

        # Substitute the AlertGroupStore singleton so the registrar reads
        # our fixture instead of the project's real store.
        import alert_group_store as ag_module
        monkeypatch.setattr(
            ag_module, "AlertGroupStore",
            lambda: ag_store,
        )

        captured = []

        class FakeScheduler:
            def add_job(self, func, trigger, **kwargs):
                captured.append((trigger, kwargs))

        register_alert_group_jobs(FakeScheduler())

        assert captured, "register_alert_group_jobs added zero jobs"
        trig, _ = captured[0]
        # APScheduler stores the timezone on the trigger; stringifying
        # gives the IANA zone name.
        assert "Europe/London" in str(trig.timezone), (
            f"Expected Europe/London on trigger, got {trig.timezone}"
        )

    def test_ag_scheduler_falls_back_to_utc_on_invalid_tz(
        self, ag_store, monkeypatch
    ):
        """Even though save_group rejects bad zones, a YAML hand-edited on
        disk could carry an invalid one. The registrar must NOT crash -
        it should warn and fall back to UTC."""
        # Write the bad zone directly (bypass validator).
        bad_yaml_path = ag_store._dir / "bad_tz.yaml"
        bad_yaml_path.write_text(
            "name: bad_tz\n"
            "description: hand-edited\n"
            "search_names:\n  - foo\n"
            "prompt_text: |\n  prompt\n"
            "schedule: '0 12 * * *'\n"
            "timezone: 'Mars/Olympus_Mons'\n"
            "max_rows: 50\n"
            "email_address: x@y.com\n"
            "disabled: false\n"
            "delivery_mode: api\n"
            "created_at: '2026-01-01T00:00:00'\n"
            "updated_at: '2026-01-01T00:00:00'\n",
            encoding="utf-8",
        )

        from alert_groups.scheduler import register_alert_group_jobs
        import alert_group_store as ag_module
        monkeypatch.setattr(
            ag_module, "AlertGroupStore",
            lambda: ag_store,
        )

        captured = []

        class FakeScheduler:
            def add_job(self, func, trigger, **kwargs):
                captured.append((trigger, kwargs))

        register_alert_group_jobs(FakeScheduler())

        assert captured, "should have registered with fallback UTC"
        trig, _ = captured[0]
        assert "UTC" in str(trig.timezone)


# ─────────────────────────────────────────────────────────────────────
# Migration of the two options AGs - the user-visible result
# ─────────────────────────────────────────────────────────────────────

class TestOptionsAGMigration:
    """The 2026-04-27 migration moved both options AGs to America/New_York
    so DST is handled automatically and the descriptions ("morning",
    "evening", "pre-close") line up with the user's actual experience."""

    def _load(self, name):
        path = PROJECT_ROOT / "alert_groups" / f"{name}.yaml"
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_options_edge_brief_uses_ny_timezone(self):
        ag = self._load("options_edge_brief")
        assert ag.get("timezone") == "America/New_York"
        # Cron must be the wall-clock-stable 10:30 + 15:30 ET pair.
        # 2026-05-02 cron audit (e3c5514): renamed numeric DOW "1-5" →
        # named "mon-fri" (numeric DOW silently misfires under
        # APScheduler's 0=Mon convention vs Linux 0=Sun).
        assert ag["schedule"] == "30 10,15 * * mon-fri"

    def test_options_performance_review_uses_ny_timezone(self):
        ag = self._load("options_performance_review")
        assert ag.get("timezone") == "America/New_York"
        # 6:30 PM ET Sunday - DST-stable, named DOW (post-audit form).
        # 2026-05-02 cron audit also moved 18:00 → 18:30 so the Sunday
        # post-market review fires cleanly after the cash close.
        assert ag["schedule"] == "30 18 * * sun"

    def test_oeb_fires_at_1030_and_1530_et_year_round(self):
        from croniter import croniter
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
        # Sample one anchor in EDT and one in EST; both must produce two
        # daily fires at 10:30 + 15:30 ET regardless of UTC offset.
        for anchor_iso in ("2026-04-28T09:00:00", "2025-12-01T09:00:00"):
            anchor = datetime.fromisoformat(anchor_iso).replace(tzinfo=ny)
            cron = croniter("30 10,15 * * 1-5", anchor)
            fires_et = []
            for _ in range(2):
                fires_et.append(cron.get_next(datetime).astimezone(ny))
            assert fires_et[0].hour == 10 and fires_et[0].minute == 30
            assert fires_et[1].hour == 15 and fires_et[1].minute == 30


# ─────────────────────────────────────────────────────────────────────
# Frontend-contract drift guards
# ─────────────────────────────────────────────────────────────────────

class TestFrontendContracts:
    """If the JS dropdown wiring is removed or renamed, the user can't
    set the timezone via the UI and our backend support is invisible."""

    def setup_method(self):
        self.ui = (PROJECT_ROOT / "desktop_app" / "ui.html").read_text(
            encoding="utf-8"
        )

    def test_ag_form_has_timezone_select(self):
        assert 'id="ag-timezone"' in self.ui, (
            "AG form must carry a #ag-timezone <select>"
        )

    def test_ss_form_has_timezone_select(self):
        assert 'id="ss-timezone"' in self.ui, (
            "Saved-search form must carry a #ss-timezone <select>"
        )

    def test_populate_timezone_select_helper_exists(self):
        assert "function populateTimezoneSelect" in self.ui
        assert "function readTimezoneSelect" in self.ui

    def test_timezone_options_includes_critical_zones(self):
        for needed in (
            "UTC", "America/New_York", "America/Los_Angeles",
            "Europe/London",
        ):
            assert needed in self.ui, (
                f"timezone dropdown must offer {needed}"
            )

    def test_ag_save_payload_includes_timezone(self):
        # The save fn destructures payload - pin the field is forwarded.
        assert "timezone" in self.ui  # cheap overall presence
        # Tighter: payload object literal references "timezone,"
        assert "timezone, max_rows" in self.ui or "timezone:" in self.ui

    def test_ss_save_payload_includes_timezone(self):
        # Saved-search payload uses "timezone:" key form.
        assert "timezone:      readTimezoneSelect" in self.ui

    def test_frozen_name_css_modifiers_present(self):
        # Both variants must exist - single-col for AG/SS, 2col for ingestion.
        assert ".data-table--frozen-name" in self.ui
        assert ".data-table--frozen-name-2col" in self.ui
        assert ".ft-title" in self.ui

    def test_ag_table_uses_frozen_name(self):
        # The AG table renderer applies the modifier class.
        assert "'data-table data-table--frozen-name'" in self.ui

    def test_ingestion_table_uses_frozen_name_2col(self):
        assert "'data-table data-table--frozen-name-2col'" in self.ui


# ─────────────────────────────────────────────────────────────────────
# Schedulers pinned to UTC explicitly
# ─────────────────────────────────────────────────────────────────────

class TestSchedulerUTCPin:
    """Both schedulers must default to timezone='UTC' so the failure mode
    of "I forgot to pass timezone= on the trigger" is consistent
    everywhere."""

    def test_background_scheduler_pinned_to_utc(self):
        engine_src = (
            PROJECT_ROOT / "scheduled_input_engine" / "engine.py"
        ).read_text(encoding="utf-8")
        # Must instantiate BackgroundScheduler with timezone="UTC".
        assert re.search(
            r'BackgroundScheduler\(\s*\n\s*timezone="UTC"',
            engine_src,
        ), "BackgroundScheduler must explicitly pin timezone='UTC'"

    def test_asyncio_scheduler_pinned_to_utc(self):
        qe_src = (
            PROJECT_ROOT / "query_engine" / "QueryEngine.py"
        ).read_text(encoding="utf-8")
        assert re.search(
            r'AsyncIOScheduler\(\s*\n\s*timezone="UTC"',
            qe_src,
        ), "AsyncIOScheduler must explicitly pin timezone='UTC'"

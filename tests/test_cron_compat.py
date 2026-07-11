"""Tests for functionality/cron_compat.py - the Linux→APScheduler day-of-week
translator that fixes the silent off-by-one in CronTrigger.from_crontab.

Caught 2026-05-02: options_edge_brief cron `30 10,15 * * 1-5` America/New_York
fired on Saturday - APScheduler interprets `1-5` as Tue-Sat (0=Mon convention),
not Mon-Fri (Linux convention). All Linux-style cron strings going to
CronTrigger.from_crontab must be translated first.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

from functionality.cron_compat import linux_dow_to_apscheduler


# ── Translation table tests ────────────────────────────────────────


@pytest.mark.parametrize(
    "linux_cron, expected",
    [
        # Single days
        ("0 9 * * 0", "0 9 * * 6"),         # Sun → APS 6
        ("0 9 * * 1", "0 9 * * 0"),         # Mon → APS 0
        ("0 9 * * 2", "0 9 * * 1"),         # Tue → APS 1
        ("0 9 * * 3", "0 9 * * 2"),         # Wed → APS 2
        ("0 9 * * 4", "0 9 * * 3"),         # Thu → APS 3
        ("0 9 * * 5", "0 9 * * 4"),         # Fri → APS 4
        ("0 9 * * 6", "0 9 * * 5"),         # Sat → APS 5
        # Linux accepts 7 as Sunday too - normalise to 0 → APS 6
        ("0 9 * * 7", "0 9 * * 6"),
        # The bug case
        ("30 10,15 * * 1-5", "30 10,15 * * 0-4"),  # Mon-Fri
        # Weekend list
        ("0 12 * * 0,6", "0 12 * * 6,5"),   # Sun,Sat → APS 6,5
        # MWF
        ("0 9 * * 1,3,5", "0 9 * * 0,2,4"),
        # Wildcard passes through
        ("0 9 * * *", "0 9 * * *"),
        # Named days pass through
        ("0 9 * * mon-fri", "0 9 * * mon-fri"),
        ("0 9 * * sat,sun", "0 9 * * sat,sun"),
        ("0 9 * * mon", "0 9 * * mon"),
        # Other fields untouched even if they contain digits matching DoW range
        ("1-5 9 * * 1-5", "1-5 9 * * 0-4"),
        ("0 9 1-5 * 1-5", "0 9 1-5 * 0-4"),
        # Step expressions in DoW pass through (not in Linux cron normally,
        # but APScheduler accepts them - let it handle)
        ("0 9 * * */2", "0 9 * * */2"),
    ],
)
def test_translation_table(linux_cron, expected):
    assert linux_dow_to_apscheduler(linux_cron) == expected


def test_passes_through_malformed_input():
    """Malformed crons are passed through so the downstream parser
    surfaces the real error, not a translator-eaten one."""
    for bad in ("", "* * *", "not a cron", None):
        assert linux_dow_to_apscheduler(bad) == bad


# ── Behavioral tests against APScheduler ───────────────────────────


def _next_fire(linux_cron: str, from_when: datetime, tz: ZoneInfo) -> datetime:
    """Translate then run through APScheduler."""
    translated = linux_dow_to_apscheduler(linux_cron)
    trigger = CronTrigger.from_crontab(translated, timezone=tz)
    return trigger.get_next_fire_time(None, from_when).astimezone(tz)


@pytest.mark.parametrize(
    "from_iso, expected_iso",
    [
        # On Sat 09:00 ET, next weekday Mon-Fri 10:30 fire is Mon 10:30 ET.
        # Pre-fix bug: APScheduler read `1-5` as Tue-Sat → would have
        # returned Sat 10:30 ET. Post-fix: returns Mon 10:30 ET.
        ("2026-05-02T13:00:00+00:00", "2026-05-04T10:30:00-04:00"),  # Sat 09 ET → Mon 10:30
        # On Fri 16:00 ET, next Mon-Fri 10:30 is Mon 10:30 ET (jumps over weekend).
        # Pre-fix bug: would have returned Sat 10:30.
        ("2026-05-01T20:00:00+00:00", "2026-05-04T10:30:00-04:00"),  # Fri 16 ET → Mon 10:30
        # On Mon 09:00 ET, next is Mon 10:30 ET (same day).
        # Pre-fix bug: APScheduler `1-5` = Tue-Sat would have returned Tue 10:30.
        ("2026-05-04T13:00:00+00:00", "2026-05-04T10:30:00-04:00"),  # Mon 09 ET → Mon 10:30
        # On Sun 12:00 ET, next is Mon 10:30 ET.
        ("2026-05-03T16:00:00+00:00", "2026-05-04T10:30:00-04:00"),
    ],
)
def test_oeb_weekday_cron_fires_only_on_weekdays(from_iso, expected_iso):
    """The exact bug pattern: Linux `30 10,15 * * 1-5` America/New_York must
    fire only on Mon-Fri after going through the translator + APScheduler."""
    et = ZoneInfo("America/New_York")
    from_when = datetime.fromisoformat(from_iso)
    expected = datetime.fromisoformat(expected_iso).astimezone(et)
    actual = _next_fire("30 10,15 * * 1-5", from_when, et)
    assert actual == expected, (
        f"Expected next fire {expected}, got {actual}. "
        f"Translator bug would return Sat 10:30 ET on weekend probes."
    )


@pytest.mark.parametrize(
    "from_iso, expected_iso",
    [
        # Sun 06:00 UTC → next Sun 18:00 UTC fire is Sun 18:00 UTC same day.
        ("2026-05-03T06:00:00+00:00", "2026-05-03T18:00:00+00:00"),
        # Mon 06:00 UTC → next Sun 18:00 UTC fire is next Sun (5/10).
        ("2026-05-04T06:00:00+00:00", "2026-05-10T18:00:00+00:00"),
    ],
)
def test_sunday_only_cron(from_iso, expected_iso):
    """`0 18 * * 0` means Sunday 18:00 in Linux convention. Pre-fix bug:
    APScheduler `0` = Monday → would have returned Mon 18:00."""
    utc = ZoneInfo("UTC")
    from_when = datetime.fromisoformat(from_iso)
    expected = datetime.fromisoformat(expected_iso).astimezone(utc)
    actual = _next_fire("0 18 * * 0", from_when, utc)
    assert actual == expected


@pytest.mark.parametrize(
    "from_iso, expected_dow_name",
    [
        # `0,6` Linux = Sat,Sun. Probe from Wed → next is Sat.
        ("2026-04-29T12:00:00+00:00", "Saturday"),
        # Probe from Sat 12:00 UTC → next is Sat 12:00 UTC same day (the cron is 12:00).
        # Wait - the cron we test is `0 9 * * 0,6`. Sat 12:00 UTC is past 09:00
        # so next fire is Sun 09:00 UTC.
        ("2026-05-02T12:00:00+00:00", "Sunday"),
    ],
)
def test_weekend_only_cron(from_iso, expected_dow_name):
    """`0 9 * * 0,6` means Sat+Sun in Linux convention."""
    utc = ZoneInfo("UTC")
    from_when = datetime.fromisoformat(from_iso)
    actual = _next_fire("0 9 * * 0,6", from_when, utc)
    assert actual.strftime("%A") == expected_dow_name, (
        f"Expected next fire on {expected_dow_name}, got {actual.strftime('%A')} ({actual})"
    )


def test_named_days_unchanged_by_translator():
    """Named-day crons must round-trip the translator unchanged so users
    who write `mon-fri` get exactly what they expect."""
    et = ZoneInfo("America/New_York")
    sat = datetime(2026, 5, 2, 13, 0, tzinfo=timezone.utc)  # Sat 09 ET
    actual = _next_fire("30 10,15 * * mon-fri", sat, et)
    expected = datetime(2026, 5, 4, 10, 30, tzinfo=et)
    assert actual == expected


# ── Drift guard: every from_crontab call site uses the translator ──


def test_all_from_crontab_callsites_use_translator():
    """Find every `CronTrigger.from_crontab` call in the production code
    and assert it's wrapped in `linux_dow_to_apscheduler`. Without this
    guard, a future commit could re-introduce the bug at a new call site.
    """
    from pathlib import Path
    import re

    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    PROD_DIRS = ["alert_groups", "scheduled_input_engine", "query_engine", "functionality"]

    bare_callsites = []
    for prod_dir in PROD_DIRS:
        for py in (PROJECT_ROOT / prod_dir).rglob("*.py"):
            if "__pycache__" in str(py):
                continue
            for line_no, line in enumerate(py.read_text().split("\n"), 1):
                if "CronTrigger.from_crontab(" not in line:
                    continue
                # Look back ~3 lines for the translator call. The pattern is:
                #   CronTrigger.from_crontab(linux_dow_to_apscheduler(schedule), ...)
                # OR sometimes the variable is pre-translated:
                #   sched = linux_dow_to_apscheduler(raw); ...; from_crontab(sched, ...)
                file_text = py.read_text()
                # Heuristic: if the file imports linux_dow_to_apscheduler and
                # the line itself or recent lines reference it, assume wired.
                wired = (
                    "linux_dow_to_apscheduler" in file_text
                    and "linux_dow_to_apscheduler" in line
                )
                if not wired:
                    bare_callsites.append(f"{py.relative_to(PROJECT_ROOT)}:{line_no}: {line.strip()}")

    # cron_compat.py itself has no from_crontab calls - sanity check
    # that we don't accidentally include it.
    bare_callsites = [s for s in bare_callsites if "cron_compat.py" not in s]

    assert not bare_callsites, (
        "These CronTrigger.from_crontab call sites do not use "
        "linux_dow_to_apscheduler. Wrap them: from_crontab(linux_dow_to_apscheduler(s), ...)\n"
        + "\n".join(bare_callsites)
    )


# ── Drift guard: NO numeric DOW in any user-mutable cron field ─────
# Caught 2026-05-04 when oeb_unusual_activity local YAML had `1-5`
# while live had been bumped to `mon-fri` by the cron audit. PUT-ing
# local→live silently regressed the cron back to numeric. The
# linux_dow_to_apscheduler translator at the scheduler call sites
# masks the runtime impact, but having different forms in different
# trees creates the same drift footgun every time someone PUTs an
# update. Solution: ALWAYS use named days in YAML/JSON config.


def _is_numeric_dow_cron(cron: str) -> bool:
    """Return True if the cron string's 5th field (day-of-week) is
    numeric (0-6 or 1-5 etc.) rather than named (sun/mon/tue...).
    Returns False for `*` (any day, no DOW restriction)."""
    import re

    if not cron:
        return False
    parts = cron.split()
    if len(parts) < 5:
        return False
    dow = parts[4]
    if dow == "*":
        return False
    return bool(re.match(r"^[0-9,\-]+$", dow))


def test_no_numeric_dow_in_any_user_mutable_cron_field():
    """Drift guard - every cron string in user-mutable config must use
    named day-of-week tokens (sun/mon/.../sat) rather than numeric (0-6
    or 1-5). Reasons:

    1. APScheduler interprets numeric DOW as 0=Mon while Linux convention
       is 0=Sun. The translator at the scheduler call sites masks the
       runtime impact, but the numeric form is bug-prone if the
       translator is ever bypassed.
    2. Mixing numeric (in templates / library JSON) with named (live
       deployment) creates a drift footgun: PUT-ing a local YAML to
       live silently regresses the cron form. Caught 2026-05-04 with
       oeb_unusual_activity.
    3. Named days are self-documenting: `* * * * mon-fri` is
       immediately readable without translation context.

    Scopes:
    - default_saved_searches/*.yaml + saved_searches/*.yaml
    - default_alert_groups/*.yaml + alert_groups/*.yaml
    - script_library/scripts/*.json (suggested_cron field)
    """
    from pathlib import Path
    import json
    import yaml

    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    violations: list[str] = []

    # SS and AG YAMLs
    for d in [
        "default_saved_searches",
        "saved_searches",
        "default_alert_groups",
        "alert_groups",
    ]:
        base = PROJECT_ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*.yaml")):
            spec = yaml.safe_load(p.read_text()) or {}
            cron = spec.get("cron_schedule", "") or ""
            if _is_numeric_dow_cron(cron):
                violations.append(f"{d}/{p.name}: cron_schedule={cron!r}")

    # Library script suggested_cron
    lib = PROJECT_ROOT / "script_library" / "scripts"
    if lib.is_dir():
        for p in sorted(lib.glob("*.json")):
            try:
                spec = json.loads(p.read_text())
            except Exception:
                continue
            cron = spec.get("suggested_cron", "") or ""
            if _is_numeric_dow_cron(cron):
                violations.append(f"script_library/scripts/{p.name}: suggested_cron={cron!r}")

    assert not violations, (
        f"Numeric day-of-week found in {len(violations)} config(s). "
        f"Use named days: 0/1-5/6 → sun/mon-fri/sat etc.\n"
        + "\n".join(violations)
    )

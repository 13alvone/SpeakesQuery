"""Cron compatibility shim for APScheduler day-of-week numbering.

APScheduler's ``CronTrigger.from_crontab(string)`` parses Linux-cron-style
strings BUT does NOT translate the day-of-week field. APScheduler internally
uses 0=Monday numbering, while Linux cron (and croniter, anacron, what users
type) uses 0=Sunday numbering. So ``* * * * 1-5`` (intended Mon-Fri) is
silently interpreted by APScheduler as Tue-Sat.

Caught 2026-05-02: ``options_edge_brief`` cron ``30 10,15 * * 1-5`` America/
New_York fired on Saturday and skipped Mondays for an unknown duration.

This module exposes :func:`linux_dow_to_apscheduler` which translates the
day-of-week field of a 5-field cron string from Linux convention to
APScheduler convention. All ``CronTrigger.from_crontab`` call sites in
SpeakesQuery should pass user-supplied cron strings through this function
first.

Named days (``mon``, ``tue``, ..., ``sun``) and wildcard (``*``) are passed
through unchanged - both conventions agree on these.
"""

from __future__ import annotations

import re

# Map: Linux cron DoW (0=Sun) → APScheduler DoW (0=Mon)
_LINUX_TO_APS_DOW = {
    0: 6,  # Sun
    1: 0,  # Mon
    2: 1,  # Tue
    3: 2,  # Wed
    4: 3,  # Thu
    5: 4,  # Fri
    6: 5,  # Sat
}


def linux_dow_to_apscheduler(cron_string: str) -> str:
    """Translate the day-of-week field of a cron string from Linux
    convention (0=Sun, 1=Mon, ..., 6=Sat) to APScheduler convention
    (0=Mon, 1=Tue, ..., 6=Sun).

    Operates on the 5th whitespace-delimited field only (DoW). Other
    fields are returned verbatim. Named days (``mon``, ``tue``, ...,
    ``sun``), wildcard (``*``), and step expressions (``*/2``) are
    passed through unchanged - they're either already unambiguous or
    behave identically across the two numbering conventions.

    Numeric ranges (e.g. ``1-5``) are translated token-by-token, so
    ``1-5`` (Linux Mon-Fri) becomes ``0-4`` (APScheduler Mon-Fri).

    Wraparound ranges that span the Sun boundary (e.g. ``6-1`` for
    Sat-Mon) translate token-by-token (→ ``5-0``) which APScheduler
    interprets as a wraparound. Use named days for these cases to
    stay readable.

    Examples:
      >>> linux_dow_to_apscheduler("30 9 * * 1-5")
      '30 9 * * 0-4'
      >>> linux_dow_to_apscheduler("0 12 * * 0,6")
      '0 12 * * 6,5'
      >>> linux_dow_to_apscheduler("0 9 * * mon-fri")
      '0 9 * * mon-fri'
      >>> linux_dow_to_apscheduler("0 9 * * *")
      '0 9 * * *'
    """
    if not cron_string or not isinstance(cron_string, str):
        return cron_string
    parts = cron_string.split()
    if len(parts) != 5:
        # Malformed - let downstream parser surface the error.
        return cron_string
    dow = parts[4]
    # If the field has any letters, slash, or hash, it's using named
    # days, step, or hash expression - pass through unchanged. Both
    # conventions agree on those forms.
    if re.search(r"[A-Za-z/#]", dow):
        return cron_string
    # Translate each numeric token. Handles "1-5", "1,3,5", "1", "*".
    # Wildcards survive the regex because they contain no digits.
    def _xlate(match: re.Match[str]) -> str:
        n = int(match.group(0))
        # Linux cron historically accepts 7 as Sunday too - normalise to 0.
        if n == 7:
            n = 0
        if n < 0 or n > 6:
            return match.group(0)  # Let downstream surface the error.
        return str(_LINUX_TO_APS_DOW[n])

    parts[4] = re.sub(r"\d+", _xlate, dow)
    return " ".join(parts)

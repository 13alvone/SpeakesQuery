#!/usr/bin/env python3
"""Simple SQLite cache for HTTP GET requests with per-execution resource budgets."""
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
import requests

logger = logging.getLogger(__name__)

# ── Per-execution budget tracking ────────────────────────────────
# Thread-local so each worker thread has independent counters.
_budget = threading.local()

# Default cache database path next to scheduled_inputs.db
CACHE_DB = Path(__file__).parent.parent / 'scheduled_inputs.db'


def _get_allowed_domains() -> set:
    """Return the set of allowed API domain regex patterns for the current thread.

    Resolution order:
      1. Per-execution thread-local set by ``reset_budget(allowed_domains=...)``.
      2. ``ALLOWED_API_DOMAINS`` environment variable (comma-separated).

    The thread-local takes precedence so engine-managed runs always honour the
    settings the engine was constructed with, even if the env var is unset or
    stale.
    """
    tl = getattr(_budget, "allowed_domains", None)
    if tl is not None:
        return set(tl)
    raw = os.environ.get("ALLOWED_API_DOMAINS", "")
    if not raw.strip():
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


# M-CE-10 (2026-04-22): catastrophic-backtrack guard on admin-supplied
# allowlist regex. Patterns over this length are rejected at use time
# (with a loud warning) rather than risking a stall on every HTTP
# request. A legitimate ``^sub\.domain\.example\.com$`` pattern sits
# well under this ceiling; anything over it is almost certainly a
# misconfiguration or a pasted payload like ``(a+)+$``.
_MAX_DOMAIN_PATTERN_LEN = 256


def _pattern_is_safe(pattern: str) -> bool:
    """Reject over-long patterns to prevent regex-DOS on every HTTP call."""
    return isinstance(pattern, str) and 0 < len(pattern) <= _MAX_DOMAIN_PATTERN_LEN


def is_allowed_api_url(api_url: str) -> bool:
    """Return True if api_url is http(s) and hostname matches an allowed regex pattern.

    Domains are resolved via :func:`_get_allowed_domains`.  An empty allowlist
    denies by default.
    """
    if not isinstance(api_url, str) or not api_url.strip():
        logger.warning("[!] is_allowed_api_url called with empty/non-string api_url")
        return False

    parsed = urlparse(api_url.strip())
    if parsed.scheme not in ("http", "https"):
        logger.warning("[!] Disallowed URL scheme: %s", parsed.scheme)
        return False

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        logger.warning("[!] URL missing hostname: %s", api_url)
        return False

    allowed_domains = _get_allowed_domains()
    if not allowed_domains:
        logger.warning("[!] allowed_api_domains is empty; denying by default")
        return False

    for pattern in allowed_domains:
        # M-CE-10: refuse to compile a pattern long enough to be a
        # potential ReDoS payload. A catastrophic regex like ``(a+)+$``
        # executed against every HTTP request stalls the ingestion
        # thread pool.
        if not _pattern_is_safe(pattern):
            logger.error(
                "[x] Rejected allowed_api_domains pattern - length %d "
                "exceeds %d-char ceiling (likely misconfiguration or "
                "ReDoS payload): %r",
                len(pattern) if isinstance(pattern, str) else -1,
                _MAX_DOMAIN_PATTERN_LEN,
                pattern[:64] if isinstance(pattern, str) else pattern,
            )
            continue
        try:
            if re.fullmatch(pattern, hostname):
                return True
        except re.error as rex:
            logger.error("[x] Invalid regex in allowed_api_domains (%s): %s", pattern, rex)

    return False


def reset_budget(
    max_requests: int = 50,
    max_response_mb: int = 10,
    allowed_domains: list | None = None,
) -> None:
    """Reset per-execution budget counters and domain allowlist.

    Call before each script run.  ``allowed_domains`` is a list of regex
    patterns matched against the URL hostname; if omitted, the previous
    thread-local list is cleared and validation falls back to the
    ``ALLOWED_API_DOMAINS`` environment variable.
    """
    _budget.max_requests = max_requests
    _budget.max_response_bytes = max_response_mb * 1024 * 1024
    _budget.request_count = 0
    _budget.allowed_domains = list(allowed_domains) if allowed_domains else None


def _check_budget() -> None:
    """Raise if the per-execution request budget is exhausted."""
    max_req = getattr(_budget, "max_requests", 50)
    count = getattr(_budget, "request_count", 0)
    if count >= max_req:
        raise RuntimeError(
            f"Request budget exhausted: {count}/{max_req} requests used. "
            f"Increase max_requests_per_execution in Settings if needed."
        )


def _check_response_size(data: bytes) -> None:
    """Raise if a single response exceeds the size budget."""
    max_bytes = getattr(_budget, "max_response_bytes", 10 * 1024 * 1024)
    if len(data) > max_bytes:
        max_mb = max_bytes / (1024 * 1024)
        actual_mb = len(data) / (1024 * 1024)
        raise RuntimeError(
            f"Response size {actual_mb:.1f} MB exceeds {max_mb:.0f} MB limit. "
            f"Increase max_response_size_mb in Settings if needed."
        )


def _extract_url(args, kwargs) -> str | None:
    """Pull the URL out of ``requests.<verb>(...)`` args or kwargs."""
    if "url" in kwargs:
        return kwargs["url"]
    if args:
        candidate = args[0]
        if isinstance(candidate, str):
            return candidate
    return None


class BudgetAwareRequests:
    """Drop-in proxy for the ``requests`` module that enforces per-execution budgets
    and the ``allowed_api_domains`` allowlist.

    Wraps ``get``, ``post``, ``put``, ``patch``, ``delete``, and ``head`` so
    every HTTP call counts against the request budget, every response body
    is checked against the response-size budget, and every target URL is
    validated against the allowlist before the request is dispatched.
    """

    def __init__(self):
        # Expose Session and other non-HTTP attributes transparently
        self._real = requests

    def __getattr__(self, name):
        """Proxy anything we don't explicitly wrap.

        L-MI-13 note (2026-04-22): accessing ``.Session``, ``.head``,
        ``.options``, etc. returns the real ``requests`` attribute,
        which means a ``Session()`` instance from a sandboxed script
        BYPASSES the budget + allowlist enforcement wired into
        ``.get``/``.post``/``.put``/``.patch``/``.delete`` below. This
        is a known design trap - the right remediation is a
        ``BudgetAwareSession`` wrapper. Until that ships, library
        scripts should stay on the top-level ``.get``/``.post`` verbs
        so every request hits the guards. Do NOT migrate paginated
        scripts to ``requests.Session()`` without also closing this
        loop.
        """
        return getattr(self._real, name)

    def _guarded_call(self, method, *args, **kwargs):
        url = _extract_url(args, kwargs)
        if url is None:
            raise ValueError(
                "BudgetAwareRequests requires a URL as the first positional "
                "argument or 'url' keyword argument."
            )
        if not is_allowed_api_url(url):
            raise ValueError(
                f"Domain not in allowed_api_domains for URL {url!r}. "
                f"Add the hostname pattern to Settings > Ingestion > "
                f"allowed_api_domains to permit this request."
            )
        _check_budget()
        _budget.request_count = getattr(_budget, "request_count", 0) + 1
        resp = method(*args, **kwargs)
        if resp.content:
            _check_response_size(resp.content)
        return resp

    def get(self, *args, **kwargs):
        return self._guarded_call(self._real.get, *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._guarded_call(self._real.post, *args, **kwargs)

    def put(self, *args, **kwargs):
        return self._guarded_call(self._real.put, *args, **kwargs)

    def patch(self, *args, **kwargs):
        return self._guarded_call(self._real.patch, *args, **kwargs)

    def delete(self, *args, **kwargs):
        return self._guarded_call(self._real.delete, *args, **kwargs)

    def head(self, *args, **kwargs):
        return self._guarded_call(self._real.head, *args, **kwargs)


def get_cached_or_fetch(url: str, ttl: int) -> bytes:
    """Return cached response content or fetch ``url`` if expired.

    Parameters
    ----------
    url : str
        The URL to request.
    ttl : int
        Time to live for the cached response in seconds.

    Returns
    -------
    bytes
        Response body.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if not is_allowed_api_url(url):
        logger.error("[x] Domain not allowed for URL %s", hostname)
        raise ValueError("Domain not allowed")

    now = time.time()
    with sqlite3.connect(CACHE_DB) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS api_cache (
                   key TEXT PRIMARY KEY,
                   data BLOB,
                   expires REAL
               )"""
        )
        cur = conn.execute(
            "SELECT data, expires FROM api_cache WHERE key=?", (url,)
        )
        row = cur.fetchone()
        if row and row[1] > now:
            logger.info("[i] Cache hit for %s", url)
            return row[0]
    # Budget: check request count before making the call
    _check_budget()
    _budget.request_count = getattr(_budget, "request_count", 0) + 1

    logger.info("[i] Fetching %s (request %d)", url, _budget.request_count)
    try:
        resp = requests.get(url, timeout=10, stream=True)
        resp.raise_for_status()
        data = resp.content
    except Exception as exc:
        logger.error("[x] Request failed: %s", exc)
        raise

    # Budget: check response size
    _check_response_size(data)
    expires = now + ttl
    with sqlite3.connect(CACHE_DB) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO api_cache (key, data, expires) VALUES (?, ?, ?)",
            (url, data, expires),
        )
        conn.commit()
    return data

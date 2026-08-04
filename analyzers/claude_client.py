"""
Claude API Client Wrapper
─────────────────────────
One place to issue ``client.messages.create(...)`` for the whole app.
Every caller - alert group dispatcher, scheduled analyzer, settings test
button, future features - routes through :func:`call_messages_create` so
that retry policy, hard timeouts, cost accounting, and durability of the
request/response record all live in a single audited path.

What this wrapper adds on top of the raw SDK:

* **Lazy import** - ``anthropic`` is imported only at call time so the app
  boots cleanly on systems without the SDK installed.
* **Bounded retry** - exponential backoff on transient network / rate /
  5xx errors. ``BadRequestError`` / ``AuthenticationError`` / 4xx other
  than 429 fail fast; retrying a malformed request just burns budget.
* **Hard request timeout** - configurable ceiling so a hung call cannot
  stall an alert group forever.
* **Dual logging** - each attempt writes one metadata row to
  ``indexes/logs/claude_api/`` (via ``log_writer``) and one full
  request+response record to ``claude_api_history.sqlite`` (via
  ``claude_history_store``). Both include a shared ``request_id`` so
  operators can cross-reference SPQL aggregates with the forensic audit.
* **Cost computation** - Anthropic's usage numbers turned into $USD using
  the pricing table in ``claude_analyzer._PRICING`` (kept DRY).

Callers get back the raw anthropic response object on success, and a
``ClaudeCallError`` on final failure - no silent ``None`` returns.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from analyzers.claude_history_store import ClaudeHistoryStore
from functionality.log_writer import log_claude_api_call

logger = logging.getLogger(__name__)


# ── Pricing (kept in sync with analyzers.claude_analyzer._PRICING) ────
# Fallback only - the canonical table is imported lazily.
_FALLBACK_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6":          {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001":  {"input": 1.00, "output": 5.00},
    # Opus-tier pricing dropped to $5/$25 with Opus 4.7+ (the old $15/$75
    # entry here was Opus 4.1-era and overstated cost 3x - corrected
    # 2026-08-04 when options_edge_brief moved to Opus 5).
    "claude-opus-4-7":            {"input": 5.00, "output": 25.00},
    "claude-opus-4-8":            {"input": 5.00, "output": 25.00},
    "claude-opus-5":              {"input": 5.00, "output": 25.00},
    "claude-sonnet-5":            {"input": 3.00, "output": 15.00},
}


def _pricing_for(model: str) -> tuple[float, float]:
    """Return (input_per_million, output_per_million) USD for *model*."""
    try:
        from analyzers.claude_analyzer import _PRICING  # type: ignore[attr-defined]
        if model in _PRICING:
            return _PRICING[model]["input"], _PRICING[model]["output"]
    except Exception:
        pass
    if model in _FALLBACK_PRICING:
        return _FALLBACK_PRICING[model]["input"], _FALLBACK_PRICING[model]["output"]
    return 0.0, 0.0


# ── Settings helpers ─────────────────────────────────────────────────

def _get_setting(key: str, default):
    try:
        from global_settings import get_settings
        value = get_settings().get(key)
        return value if value is not None else default
    except Exception:
        return default


class _MissingSDKError(RuntimeError):
    """Internal signal that ``import anthropic`` failed inside the factory."""


class ClaudeCallError(RuntimeError):
    """Raised when a Claude API call fails after all retries.

    ``request_id`` can be used to look up the full request/response in the
    history store for forensic analysis.
    """

    def __init__(
        self,
        message: str,
        *,
        request_id: str,
        error_class: str = "",
        attempts: int = 0,
        last_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.error_class = error_class
        self.attempts = attempts
        self.last_error = last_error


@dataclass
class ClaudeCallResult:
    """Successful response plus the metadata recorded for it."""
    response: Any
    request_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    latency_ms: int
    attempts: int
    # How the *successful* attempt was routed: "headroom" (via the
    # compression proxy), "direct" (straight to Anthropic), or
    # "direct-fallback" (proxy unreachable → failed open to direct).
    path: str = "direct"


# ── Retry policy ─────────────────────────────────────────────────────

def _is_retryable(exc: BaseException) -> bool:
    """Return True when *exc* is a transient error worth retrying.

    Deliberately does NOT include ``APITimeoutError``: the SDK raises it
    when the per-request ceiling expires, and retrying fires another
    attempt with the same timeout - just burning budget against the
    same wall. If 120s isn't enough for a web_search brief, 120s still
    isn't enough on attempt 2. Raise the ``claude_request_timeout_seconds``
    setting instead. Caught 2026-04-21 after a Daily Brief dispatch
    retried 4 × 120s = 8 minutes then failed; user never got output.
    """
    name = type(exc).__name__
    retryable_names = {
        "APIConnectionError",
        "RateLimitError",
        "InternalServerError", "APIError",
    }
    if name in retryable_names:
        return True
    # APIStatusError: only retry 5xx and 429
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status == 429 or 500 <= status < 600:
            return True
    return False


def _is_headroom_failover(exc: BaseException) -> bool:
    """Return True when a Headroom-routed call should fail open to direct.

    Fail-open is mandatory: Headroom must never be able to take down alert
    analysis. We fail over only on *connection-level* problems with the
    proxy - it's unreachable, the connection was refused/reset, the call
    timed out, or the proxy itself returned a 502/503/504. A genuine
    Anthropic 4xx (400/401/...) is a real error that would also fail
    direct, so we do NOT fail over on those - that just doubles the cost
    of a request that was always going to be rejected.

    Matched by exception class *name* so we don't have to import the
    ``anthropic`` SDK here (it's lazy-imported elsewhere); the SDK raises
    ``APIConnectionError`` / ``APITimeoutError`` for the connection cases.
    """
    name = type(exc).__name__
    if name in ("APIConnectionError", "APITimeoutError"):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in (502, 503, 504):
        return True
    return False


def _extract_usage(response: Any) -> dict:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        "cache_creation_tokens": int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        ),
    }


def _response_to_jsonable(response: Any) -> Any:
    """Convert an anthropic response (pydantic model) to plain dict."""
    if response is None:
        return None
    for attr in ("model_dump", "to_dict"):
        fn = getattr(response, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    # Fall back to vars() / string repr
    try:
        return vars(response)
    except Exception:
        return {"repr": repr(response)}


# ── Credential lookup ────────────────────────────────────────────────

_vault_lock = threading.Lock()
# Cached copy of the API key + the monotonic timestamp when it was
# fetched. Avoids opening the credential vault N times for a single
# retrying Claude call (3-4 retries × 10 feeders in an AG dispatch
# means ~30 vault opens per minute of dispatcher work). The TTL is
# short enough that a key rotation via the Settings UI takes effect
# quickly without any manual cache invalidation.
_API_KEY_TTL_S = 60.0
_api_key_cache: tuple[str, float] | None = None


def _get_api_key() -> str:
    """Retrieve ANTHROPIC_API_KEY from the credential vault, with a
    short-lived cache.

    Returns an empty string if the vault is unavailable or the key is not
    set - callers should check and raise ``ClaudeCallError`` before making
    a network call. Cache TTL is 60 seconds so a key rotation in the
    Settings UI propagates within a minute without manual invalidation.
    """
    global _api_key_cache
    now = time.monotonic()
    with _vault_lock:
        if _api_key_cache is not None:
            key, fetched_at = _api_key_cache
            if now - fetched_at < _API_KEY_TTL_S:
                return key

    try:
        from global_settings import get_settings
        from scheduled_input_engine.credentials import CredentialVault
        settings = get_settings()
        vault_db = _project_root() / "credentials.sqlite"
        with _vault_lock:
            vault = CredentialVault(
                str(vault_db), settings.get("credential_key_dir")
            )
            key = vault.retrieve(-1, "ANTHROPIC_API_KEY") or ""
            _api_key_cache = (key, now)
            return key
    except Exception as exc:
        logger.warning("[!] Could not retrieve ANTHROPIC_API_KEY: %s", exc)
        return ""


def _invalidate_api_key_cache() -> None:
    """Clear the cached API key.

    Exposed so the Settings endpoint can invalidate on key-save without
    waiting for the 60s TTL. Safe to call unconditionally.
    """
    global _api_key_cache
    with _vault_lock:
        _api_key_cache = None


def _project_root():
    from pathlib import Path
    return Path(__file__).parent.parent.resolve()


# ── Public API ───────────────────────────────────────────────────────

def call_messages_create(
    *,
    source: str,
    group_name: str | None = None,
    request_id: str | None = None,
    client_factory: Callable[..., Any] | None = None,
    api_key_override: str | None = None,
    use_headroom: bool | None = None,
    **create_kwargs,
) -> ClaudeCallResult:
    """Call ``client.messages.create(**create_kwargs)`` with robustness.

    Parameters
    ----------
    source : str
        Tag for the log row (``"alert_group"``, ``"analyzer"``,
        ``"settings_test"``, ``"batch_poll"``, ...).
    group_name : str, optional
        Associates the call with an alert group. Stored in both the
        Parquet log and the SQLite history for per-group cost queries.
    request_id : str, optional
        Pre-supply a UUID to thread through; by default one is generated.
    client_factory : callable, optional
        Test hook - returns an object with a ``.messages.create(...)``
        method. Called as ``client_factory(api_key, base_url)`` when it
        accepts a second positional parameter, else ``client_factory(api_key)``
        (so existing 1-arg test stubs keep working). ``base_url`` is the
        Headroom proxy URL on the headroom path and ``None`` on the direct
        path. Production code lets this default to ``anthropic.Anthropic``.
    api_key_override : str, optional
        Test hook - use this key instead of consulting the vault. Useful
        for the settings "Test Claude" endpoint which takes a candidate
        key that hasn't been saved yet.
    use_headroom : bool, optional
        Whether to route this call through the Headroom compression proxy
        (see :mod:`analyzers.headroom`). ``True`` builds the Anthropic
        client with ``base_url`` pointed at the proxy; on a connection /
        timeout / 502-504 failure it **fails open** to a direct Anthropic
        call (logged, ``path="direct-fallback"``). ``False`` / ``None``
        (the default) routes direct - callers that participate in the
        feature pass an already-resolved boolean from
        :func:`analyzers.headroom.resolve_use_headroom`. The
        ``HEADROOM_DISABLE`` env kill switch forces direct here regardless
        of what the caller passed (defense in depth).
    **create_kwargs :
        Forwarded directly to the SDK (``model``, ``max_tokens``,
        ``messages``, ``system``, ``tools``, ``metadata``, etc.).

    Raises
    ------
    ClaudeCallError
        If all retry attempts fail, or the API key is missing.
    """
    rid = request_id or str(uuid.uuid4())

    api_key = api_key_override or _get_api_key()
    if not api_key:
        err = "No Claude API key configured."
        _record_attempt(
            rid=rid, source=source, group_name=group_name,
            model=create_kwargs.get("model", ""), status="error",
            request_body=_redact_kwargs(create_kwargs),
            response_body=None, latency_ms=0,
            attempt_num=0, retried=False,
            error_class="MissingCredential",
            error_message=err, usage={}, cost_usd=0.0,
        )
        raise ClaudeCallError(
            err, request_id=rid, error_class="MissingCredential", attempts=0,
        )

    # Resolve robustness knobs
    max_attempts = max(1, int(_get_setting("claude_retry_attempts", 3)) + 1)
    initial_backoff = int(_get_setting("claude_retry_initial_backoff_seconds", 2))
    timeout_s = int(_get_setting("claude_request_timeout_seconds", 120))

    # ── Headroom routing decision ────────────────────────────────────
    # The caller passes an already-resolved boolean (or None=direct). The
    # HEADROOM_DISABLE env kill switch overrides it to direct here too, so
    # there is no path to the proxy when the operator flips that switch.
    from analyzers import headroom as _headroom
    use_hr = bool(use_headroom)
    if use_hr and _headroom.is_globally_disabled():
        logger.info(
            "[i] Claude call (%s): HEADROOM_DISABLE set - forcing direct route.",
            source,
        )
        use_hr = False
    headroom_url = _headroom.resolve_proxy_url() if use_hr else None

    if client_factory is None:
        def _default_factory(key: str, base_url: str | None = None):
            try:
                import anthropic
            except ImportError as exc:
                # The SDK is a hard dependency now (listed in requirements.txt)
                # but a host that predates that change - or a Docker image
                # that hasn't been rebuilt since - will still crash here.
                # Turn the raw ImportError into an actionable message the
                # Test Claude button can render inline so the user doesn't
                # have to grep server logs to figure out what to do.
                raise _MissingSDKError(str(exc)) from exc
            kwargs: dict[str, Any] = {"api_key": key, "timeout": float(timeout_s)}
            if base_url:
                kwargs["base_url"] = base_url
            return anthropic.Anthropic(**kwargs)
        client_factory = _default_factory

    # Support both 1-arg (legacy test stubs) and 2-arg factories.
    import inspect

    def _make_client(base_url: str | None):
        try:
            params = inspect.signature(client_factory).parameters
            accepts_base = len(params) >= 2 or any(
                p.kind == inspect.Parameter.VAR_POSITIONAL for p in params.values()
            )
        except (TypeError, ValueError):
            accepts_base = False
        if accepts_base:
            return client_factory(api_key, base_url)
        return client_factory(api_key)

    try:
        # The direct client is always built (it's the fail-open target).
        # The headroom client is built only when we're routing through it.
        direct_client = _make_client(None)
        active_client = _make_client(headroom_url) if use_hr else direct_client
    except _MissingSDKError as exc:
        err = (
            "The 'anthropic' Python SDK is not installed in this environment. "
            "Run `pip install 'anthropic>=0.91,<1.0'` and restart the server "
            "(or, if you're on Docker, rebuild the image with `./install.sh` "
            "now that the dependency has been added to requirements.txt)."
        )
        _record_attempt(
            rid=rid, source=source, group_name=group_name,
            model=create_kwargs.get("model", ""), status="error",
            request_body=_redact_kwargs(create_kwargs),
            response_body=None, latency_ms=0,
            attempt_num=0, retried=False,
            error_class="MissingSDK",
            error_message=err, usage={}, cost_usd=0.0,
            headroom_path="headroom" if use_hr else "direct",
        )
        raise ClaudeCallError(
            err, request_id=rid, error_class="MissingSDK", attempts=0,
            last_error=exc.__cause__,
        )

    # Routing state. ``path`` is the route in effect for the NEXT attempt;
    # ``failed_over`` ensures we only fail open once. ``total_attempts``
    # is monotonic across both routes (unique history keys); ``path_budget``
    # resets on failover so the direct route gets a full retry budget.
    client = active_client
    path = "headroom" if use_hr else "direct"
    failed_over = False
    logger.info(
        "[i] Claude call (%s): routing %s%s",
        source, path,
        f" (proxy={headroom_url})" if use_hr else "",
    )

    last_exc: BaseException | None = None
    total_attempts = 0
    path_budget = 0
    while True:
        total_attempts += 1
        path_budget += 1
        attempt = total_attempts
        started = time.monotonic()
        try:
            response = client.messages.create(**create_kwargs)
            latency_ms = int((time.monotonic() - started) * 1000)
            usage = _extract_usage(response)
            input_pm, output_pm = _pricing_for(create_kwargs.get("model", ""))
            cost = (
                usage.get("input_tokens", 0) / 1_000_000 * input_pm
                + usage.get("output_tokens", 0) / 1_000_000 * output_pm
            )
            cost = round(cost, 6)
            # L-AN-15 (2026-04-22): a mis-configured pricing table (negative
            # input_pm / output_pm) would silently CREDIT the ledger. Floor
            # at zero with a loud error so the bug surfaces in docker logs
            # rather than as a mysteriously-growing budget.
            if cost < 0:
                logger.error(
                    "[x] Negative cost computed for model=%s "
                    "(input_pm=%s, output_pm=%s, in_t=%d, out_t=%d). "
                    "Flooring at 0.0 - check the pricing table.",
                    create_kwargs.get("model", ""), input_pm, output_pm,
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                )
                cost = 0.0

            _record_attempt(
                rid=rid, source=source, group_name=group_name,
                model=create_kwargs.get("model", ""), status="success",
                request_body=_redact_kwargs(create_kwargs),
                response_body=_response_to_jsonable(response),
                latency_ms=latency_ms, attempt_num=attempt,
                retried=attempt > 1,
                stop_reason=getattr(response, "stop_reason", None),
                usage=usage, cost_usd=cost,
                headroom_path=path,
            )

            # H-AN-6 / X-2: unify daily-budget accounting so AG dispatcher
            # calls decrement the same counter ClaudeAnalyzer reads.
            _record_daily_budget_usd(
                create_kwargs.get("model", ""),
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                cost,
            )

            if path == "direct-fallback":
                logger.info(
                    "[i] Claude call (%s): succeeded via direct fallback "
                    "after Headroom proxy was unreachable.", source,
                )

            return ClaudeCallResult(
                response=response, request_id=rid,
                model=create_kwargs.get("model", ""),
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_tokens", 0),
                cost_usd=cost, latency_ms=latency_ms, attempts=attempt,
                path=path,
            )

        except BaseException as exc:  # noqa: BLE001
            latency_ms = int((time.monotonic() - started) * 1000)
            last_exc = exc
            status = "timeout" if type(exc).__name__ == "APITimeoutError" else "error"
            _record_attempt(
                rid=rid, source=source, group_name=group_name,
                model=create_kwargs.get("model", ""), status=status,
                request_body=_redact_kwargs(create_kwargs),
                response_body=None, latency_ms=latency_ms,
                attempt_num=attempt, retried=attempt > 1,
                error_class=type(exc).__name__,
                error_message=str(exc)[:2000],
                usage={}, cost_usd=0.0,
                headroom_path=path,
            )

            # ── Fail-open: Headroom unreachable → retry direct ──────────
            # Mandatory non-functional requirement: the proxy must never be
            # able to take down alert analysis. On a connection-level
            # failure (proxy down / refused / reset / timeout / 502-504)
            # we switch to the direct Anthropic client and retry
            # immediately - NOT counting it against the retry budget, so
            # even retry_attempts=0 still fails open. We only do this once.
            if path == "headroom" and not failed_over and _is_headroom_failover(exc):
                logger.warning(
                    "[!] Claude call (%s): Headroom proxy unreachable (%s); "
                    "failing open to direct Anthropic: %s",
                    source, type(exc).__name__, exc,
                )
                client = direct_client
                path = "direct-fallback"
                failed_over = True
                path_budget = 0  # fresh retry budget on the direct route
                continue

            if path_budget >= max_attempts or not _is_retryable(exc):
                break

            backoff = min(initial_backoff * (2 ** (path_budget - 1)), 60)
            logger.warning(
                "[!] Claude call (%s) attempt %d (%s, path=%s) failed (%s); "
                "retrying in %ds: %s",
                source, attempt, path_budget, path, type(exc).__name__,
                backoff, exc,
            )
            time.sleep(backoff)

    # Exhausted
    err_cls = type(last_exc).__name__ if last_exc else "Unknown"
    # Self-documenting error for the most common terminal failure: a
    # timeout on a web_search-enabled call. Tell the operator EXACTLY
    # which knob to raise rather than leaving them to grep the docs.
    hint = ""
    if err_cls == "APITimeoutError":
        hint = (
            " - the request exceeded claude_request_timeout_seconds "
            f"({timeout_s}s). web_search-enabled analyst briefs can take "
            "2-5 minutes; raise this setting (Settings → Claude Analyzer → "
            "Request timeout) rather than retrying, since another attempt "
            "will hit the same ceiling. Retries are already disabled for "
            "timeout errors to avoid wasting ~N*timeout seconds on the "
            "same wall."
        )
    raise ClaudeCallError(
        f"Claude API call failed after {max_attempts} attempt(s): "
        f"{last_exc}{hint}",
        request_id=rid,
        error_class=err_cls,
        attempts=max_attempts,
        last_error=last_exc,
    )


# ── Internal helpers ──────────────────────────────────────────────────

# Regex targeting obvious Anthropic API-key shapes that might have leaked
# into a user-authored prompt (e.g. pasted example, mis-filled macro).
# Matches ``sk-ant-api03-...`` and similar; applied to every string field
# in request_body + response_body before they land in claude_api_history.
#
# H-AN-7 (2026-04-21): helpers lifted into ``analyzers/_scrub.py`` so the
# batch-submit path in ``claude_analyzer.py::_call_batch_api`` can share
# the same boundary. Underscored aliases kept here for the existing
# ``_record_attempt`` call sites.
from ._scrub import (
    _SECRET_PATTERNS,  # noqa: F401 - re-exported for back-compat + tests
    scrub_secrets as _scrub_secrets,
    redact_kwargs as _redact_kwargs,
)


# H-AN-6 / X-2 (2026-04-22): unified daily-budget ledger.
#
# Before this change two independent cost trackers coexisted:
#
#   * ``ClaudeAnalyzer`` (scheduled-search path) wrote to
#     ``analyzer_budget`` via ``AnalyzerStorage.record_usage`` on every
#     ``_record_usage`` call, in CENTS.
#   * ``claude_client`` (live-path, used by the alert-group dispatcher,
#     the Test-Claude button, and the analyzer itself) only wrote the
#     per-call USD cost to ``claude_api_history.sqlite`` - NOT the
#     per-day ledger.
#
# Consequence: AG-dispatcher spend bypassed the daily budget counter
# that the ``budget_exceeded`` gate reads. A runaway AG schedule could
# burn through the daily budget without tripping the gate.
#
# Fix: every success path in ``call_messages_create`` now increments
# the ``analyzer_budget`` ledger. ``ClaudeAnalyzer`` no longer writes
# directly (it still keeps an in-memory telemetry counter for the UI)
# and its budget gate re-reads the ledger each call so cross-process
# writes are visible.

def _record_daily_budget_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> None:
    """Increment the shared daily-budget ledger for a successful Claude call.

    Writes to :class:`analyzers.storage.AnalyzerStorage`'s ``analyzer_budget``
    table. Cost is stored in cents to match the legacy
    ``ClaudeAnalyzer._record_usage`` convention. Failures are logged and
    swallowed - budget accounting must never break a Claude call.
    """
    # Skip degenerate calls (test stubs, cache-only hits).
    if input_tokens == 0 and output_tokens == 0 and cost_usd == 0.0:
        return
    try:
        from analyzers.storage import AnalyzerStorage
        import datetime as _dt
        storage = AnalyzerStorage()
        # M-AN-10 (2026-04-22): use UTC date so the budget window aligns
        # with the AG scheduler + email subject lines (all already UTC).
        storage.record_usage(
            _dt.datetime.now(_dt.timezone.utc).date().isoformat(),
            int(input_tokens),
            int(output_tokens),
            float(cost_usd) * 100.0,
        )
    except Exception as exc:
        logger.warning(
            "[!] Failed to record Claude call in daily budget ledger "
            "(model=%s, tokens=%d+%d, cost=$%.4f): %s",
            model, input_tokens, output_tokens, cost_usd, exc,
        )


def _record_attempt(
    *,
    rid: str,
    source: str,
    group_name: str | None,
    model: str,
    status: str,
    request_body: Any,
    response_body: Any,
    latency_ms: int,
    attempt_num: int,
    retried: bool,
    usage: dict,
    cost_usd: float,
    stop_reason: str | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
    headroom_path: str | None = None,
) -> None:
    """Write one Parquet log row + one SQLite history row per attempt."""
    try:
        log_claude_api_call(
            request_id=rid, source=source, group_name=group_name,
            model=model, status=status,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_tokens"),
            cache_creation_tokens=usage.get("cache_creation_tokens"),
            cost_usd=cost_usd, latency_ms=latency_ms,
            attempt_num=attempt_num, retried=retried,
            stop_reason=stop_reason,
            error_class=error_class, error_message=error_message,
            headroom_path=headroom_path,
        )
    except Exception as exc:
        logger.warning("[!] Could not emit claude_api log row: %s", exc)

    try:
        ClaudeHistoryStore.get_instance().record_call(
            request_id=f"{rid}:{attempt_num}" if attempt_num > 1 else rid,
            source=source, group_name=group_name, model=model, status=status,
            request_body=request_body, response_body=response_body,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_tokens"),
            cache_creation_tokens=usage.get("cache_creation_tokens"),
            cost_usd=cost_usd, latency_ms=latency_ms,
            attempt_num=attempt_num, retried=retried,
            stop_reason=stop_reason,
            error_class=error_class, error_message=error_message,
        )
    except Exception as exc:
        logger.warning("[!] Could not persist to claude_api_history: %s", exc)


def test_connectivity(api_key: str | None = None) -> dict:
    """Fire a minimal Claude call to verify credentials + network.

    Used by the ``/api/analyzer/test`` settings endpoint. Returns a dict
    the UI can render directly - success or structured error. Never
    raises; even exhausted retries turn into ``{"ok": False, ...}``.

    When ``api_key`` is provided we route through the wrapper with the
    override path, so the call still lands in the history store / log
    stream and the user can see that a pre-save key was tested.
    """
    model = _get_setting("claude_analyzer_model_triage", "claude-haiku-4-5-20251001")
    try:
        result = call_messages_create(
            source="settings_test",
            api_key_override=api_key,
            model=model,
            max_tokens=16,
            messages=[
                {"role": "user", "content": "Reply with the single word OK."}
            ],
        )
        return {
            "ok": True,
            "request_id": result.request_id,
            "model": result.model,
            "latency_ms": result.latency_ms,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
            "attempts": result.attempts,
        }
    except ClaudeCallError as exc:
        return {
            "ok": False,
            "request_id": exc.request_id,
            "error_class": exc.error_class,
            "error_message": str(exc),
            "attempts": exc.attempts,
        }

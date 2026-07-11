"""Phase 4 / Bet 4 slice 8a - failed-feeder patch drafter.

When an ingestion script fails, this module asks Claude to read the
script source + the error message and suggest a unified diff that
should fix the issue. The patch is RECORDED to the
``patch_suggestions`` Parquet log category for the operator to
review + apply manually. **It is never auto-applied.**

This module follows the slice-7 budget-gate contract:

* ``max_cost_usd=<F>`` hard ceiling per call (0 = uncapped, NOT
  recommended). Defaults to ``patch_drafter_max_cost_usd`` setting
  (default 0.10).
* ``dry_run=true`` returns a worst-case cost estimate without making
  a billable call - same money-leak canary contract that pins the
  ``| llm`` SPQL pipe.
* ``analyzers.claude_client.call_messages_create()`` is the ONLY
  Anthropic call site. Never imports ``anthropic`` directly. Per
  CLAUDE.md "Claude API calls" rule.

Slice 8a does NOT auto-trigger the drafter on failure - that wiring
lives in ``scheduled_input_engine/engine.py::_run_task`` and is
gated by ``patch_drafter_enabled`` (default False; opt-in). Slice 8b
will add GitHub PR creation; that's deliberately out of scope here.

Public surface:

* :func:`draft_patch_for_failed_task` - the synchronous entry point.
  Used by the engine's failure-path wiring AND by an on-demand
  endpoint (``/api/patch-drafter/suggest``) that operators can call
  manually from the UI.
* :func:`estimate_patch_cost_usd` - pre-call worst-case cost
  estimate, used by the dry-run path.
* :func:`compute_error_hash` - stable hash of an error message for
  dedup. Same error → same hash; identical hash on consecutive
  failures means we already drafted a patch and can skip the call.

Design notes:

* The drafter prompt is hardcoded inside this module (NOT operator-
  editable). Operator-editable prompts open a code-execution-via-
  prompt-injection attack surface that's not justified for an
  internal-use diff suggester. Hardcoded keeps the threat model
  simple and the audit trail clean.
* The diff output is opaque text - we don't parse it. The operator
  reads the diff in the patch_suggestions log + decides whether to
  apply. Slice 8b may add lightweight diff-shape validation; for
  now, trust Claude to emit a parseable unified diff.
* Cost shows in ``claude_api_history.sqlite`` (via ``call_messages_create``)
  AND in the ``patch_suggestions`` Parquet log (for SPQL queries).
  Two views, same source of truth.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Defaults - every value can be overridden via global settings or the
# function's keyword arguments.
# ─────────────────────────────────────────────────────────────────────

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_MAX_COST_USD = 0.10
_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_MAX_OUTPUT_TOKENS = 2048
_CHARS_PER_TOKEN = 4.0  # English-text industry rule of thumb

# Pricing fallback when the claude_client._pricing_for table doesn't
# have a row. Conservative - overestimates so the budget gate stays
# safe. Same shape used by claude_client._pricing_for.
_FALLBACK_INPUT_PER_M = 3.0    # $3 per 1M input tokens
_FALLBACK_OUTPUT_PER_M = 15.0  # $15 per 1M output tokens


# ─────────────────────────────────────────────────────────────────────
# The drafter prompt - hardcoded; not operator-editable.
# ─────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a software engineer reviewing a SpeakesQuery ingestion "
    "script that failed. SpeakesQuery scripts are Python that runs in "
    "a RestrictedPython sandbox by default; allowed modules are "
    "pandas, requests, json, datetime, time, re, math, hashlib, "
    "base64, collections, io, bs4, lxml. Scripts must produce a "
    "DataFrame with an `_epoch` column (Unix seconds) and call "
    "GENERATE_RESULTS(df) to emit output. Common failure causes: "
    "missing API credentials, rate limits, schema changes upstream, "
    "missing _epoch column, sandbox-disallowed imports, network "
    "failures, malformed responses.\n\n"
    "Given the script source and the error message, produce a "
    "unified diff (`diff --git a/script.py b/script.py` style) that "
    "you believe will fix the failure. Wrap the diff in a "
    "```diff fenced block. Then give a one-paragraph plain-English "
    "explanation of WHY the change should fix the issue.\n\n"
    "If you cannot suggest a confident fix from the information "
    "given (e.g. the error suggests an external service outage that "
    "the script cannot work around), say so explicitly in plain "
    "English instead of guessing - output the literal string "
    "NO_CONFIDENT_FIX followed by your reasoning. Do NOT emit a "
    "speculative diff just to fill the response."
)

_USER_PROMPT_TEMPLATE = (
    "<task>\n"
    "<script_title>{title}</script_title>\n"
    "<error_message>{error_message}</error_message>\n"
    "<script_source>\n"
    "{script_source}\n"
    "</script_source>\n"
    "</task>"
)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def compute_error_hash(error_message: str) -> str:
    """Stable hash of an error message for dedup.

    Same error → same hash. Used by the engine's failure-path wiring
    to avoid re-asking Claude for a patch when the SAME error fires
    repeatedly (e.g. a misconfigured script that fails every cron
    tick).

    The hash is sha256-truncated to 16 hex chars (64 bits). Collision
    probability is negligible for the dedup-cache use case (per-task
    cardinality ≤ a few dozen distinct errors).
    """
    if not isinstance(error_message, str):
        error_message = str(error_message or "")
    digest = hashlib.sha256(error_message.encode("utf-8")).hexdigest()
    return digest[:16]


def estimate_tokens_from_chars(text: str, *, chars_per_token: float = _CHARS_PER_TOKEN) -> int:
    """Conservative token estimate from char length. Round UP."""
    if not text:
        return 0
    n = len(text)
    return int((n + chars_per_token - 1) // chars_per_token) if n else 0


def _pricing(model: str) -> tuple[float, float]:
    """Per-million-token (input, output) USD pricing.

    Reuses ``analyzers.claude_client._pricing_for`` when available
    AND it returns a non-zero pair. ``_pricing_for`` returns
    ``(0.0, 0.0)`` for unknown models (a price-not-listed sentinel);
    the patch drafter MUST NOT trust that as "free" - a free price
    means the budget gate's worst-case estimate is also zero, which
    would silently let an unknown-model call slip past any
    ``max_cost_usd`` ceiling. Fall back to the conservative-
    overestimate pair instead so the gate stays safe.
    """
    try:
        from analyzers.claude_client import _pricing_for
        in_pm, out_pm = _pricing_for(model)
        if in_pm > 0 and out_pm > 0:
            return in_pm, out_pm
    except Exception:
        pass
    return (_FALLBACK_INPUT_PER_M, _FALLBACK_OUTPUT_PER_M)


def estimate_patch_cost_usd(
    *,
    script_source: str,
    error_message: str,
    title: str = "",
    model: str = _DEFAULT_MODEL,
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
) -> dict:
    """Worst-case pre-call cost estimate. Mirrors the slice-7 budget
    gate's conservative overestimate pattern: every call is assumed
    to hit ``max_output_tokens`` of output.

    Returns ``{cost_usd, input_tokens, output_tokens, model}``. Used
    by the ``dry_run=True`` path; never makes a network call.
    """
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        title=title or "(untitled)",
        error_message=error_message or "",
        script_source=script_source or "",
    )
    input_tokens = (
        estimate_tokens_from_chars(_SYSTEM_PROMPT)
        + estimate_tokens_from_chars(user_prompt)
    )
    input_pm, output_pm = _pricing(model)
    cost = (
        input_tokens / 1_000_000 * input_pm
        + max_output_tokens / 1_000_000 * output_pm
    )
    cost = round(max(cost, 0.0), 6)
    return {
        "cost_usd": cost,
        "input_tokens": int(input_tokens),
        "output_tokens": int(max_output_tokens),
        "model": model,
    }


# ─────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────

@dataclass
class PatchDraftResult:
    """Result envelope for ``draft_patch_for_failed_task``.

    ``status`` is one of:

    * ``"success"`` - Claude returned a response; ``patch`` and
      ``explanation`` are populated. Note: Claude may return
      ``NO_CONFIDENT_FIX`` text - we still call that "success"
      because the call completed; the operator-side parser can
      detect the sentinel and treat it as "no actionable patch."
    * ``"dry_run"`` - ``dry_run=True`` was set; ``cost_usd`` carries
      the worst-case estimate; no network call was made.
    * ``"skipped_budget"`` - the worst-case estimate exceeds
      ``max_cost_usd``; no call made. ``error_message`` carries
      the explanation.
    * ``"skipped_no_key"`` - no Anthropic API key configured; no
      call made.
    * ``"error"`` - the call failed (transient or terminal). The
      claude_client retry policy already kicked in; this is the
      final state. ``error_class`` and ``error_message`` populate.
    """

    status: str
    patch: str = ""
    explanation: str = ""
    response_text: str = ""
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str = ""
    request_id: str = ""
    error_class: str = ""
    error_message: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def _split_diff_and_explanation(text: str) -> tuple[str, str]:
    """Split Claude's response into ``(patch, explanation)``.

    The system prompt asks for a ```diff fenced block followed by a
    plain-English paragraph. We extract the fenced block as the
    patch; everything after it is the explanation. If no fenced
    block is found, ``patch`` is empty and ``explanation`` is the
    full text - typically the ``NO_CONFIDENT_FIX`` case.
    """
    if not text:
        return "", ""
    # Find ```diff or ``` fenced block
    import re
    fence_re = re.compile(r"```(?:diff|patch)?\n(.*?)```", re.DOTALL)
    m = fence_re.search(text)
    if not m:
        return "", text.strip()
    patch = m.group(1).strip()
    # Explanation = everything after the closing fence
    after = text[m.end():].strip()
    return patch, after


def _read_text_from_response(response: Any) -> str:
    """Pull the text content out of an Anthropic Messages response.

    The SDK shape is ``response.content[0].text``. We're defensive in
    case the response shape changes - fall back to ``str(response)``.
    """
    try:
        content = getattr(response, "content", None) or []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str) and text:
                return text
    except Exception:
        pass
    try:
        return str(response)
    except Exception:
        return ""


def draft_patch_for_failed_task(
    *,
    script_source: str,
    error_message: str,
    script_title: str = "",
    task_id: int | None = None,
    model: str | None = None,
    max_cost_usd: float | None = None,
    dry_run: bool = False,
    timeout_seconds: int | None = None,
    group_name: str | None = None,
    request_id: str | None = None,
) -> PatchDraftResult:
    """Ask Claude to suggest a unified-diff fix for a failed script.

    Synchronous; returns when Claude responds (or the timeout fires).
    Caller-side wiring (the engine's failure-path) is responsible for
    running this in a background thread when called from a worker.

    The slice-7 budget-gate contract:

    * ``max_cost_usd`` is a HARD ceiling. Calls whose worst-case
      estimate exceeds the cap return ``status="skipped_budget"``
      with no network call. ``0`` is treated as "uncapped" (NOT
      recommended for the patch drafter - see settings default).
    * ``dry_run=True`` returns the estimate without calling. The
      money-leak canary asserts zero ``call_messages_create``
      invocations under ``dry_run=True`` AND under any path that
      would cap-out, mirroring the ``| llm`` pipe canary class.

    Parameters
    ----------
    script_source : str
        The Python source of the failing script.
    error_message : str
        The error message from the failed run (already
        ``redact_credentials``-scrubbed by the engine before reaching
        here).
    script_title : str, optional
        Friendly task title for the prompt. Goes into the log row.
    task_id : int, optional
        Task id for the log row. Pure metadata; the drafter does not
        use it.
    model : str, optional
        Claude model id. Defaults to ``patch_drafter_model`` setting.
    max_cost_usd : float, optional
        Per-call ceiling. Defaults to ``patch_drafter_max_cost_usd``
        setting.
    dry_run : bool, default False
        If True, returns the worst-case estimate without calling.
    timeout_seconds : int, optional
        Per-request timeout. Defaults to ``patch_drafter_timeout_seconds``.
    group_name : str, optional
        For ``call_messages_create`` log routing. Pure metadata.
    request_id : str, optional
        Pre-supply a request id to thread through; otherwise
        ``call_messages_create`` generates one.
    """
    # Resolve defaults from settings (lazy import - settings module
    # may not be initialised in tests that exercise this in isolation).
    if model is None:
        model = _setting("patch_drafter_model", _DEFAULT_MODEL)
    if max_cost_usd is None:
        max_cost_usd = float(
            _setting("patch_drafter_max_cost_usd", _DEFAULT_MAX_COST_USD)
        )
    if timeout_seconds is None:
        timeout_seconds = int(
            _setting("patch_drafter_timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
        )

    # Estimate worst-case cost first - used by both dry_run + budget gate
    estimate = estimate_patch_cost_usd(
        script_source=script_source,
        error_message=error_message,
        title=script_title,
        model=model,
        max_output_tokens=_DEFAULT_MAX_OUTPUT_TOKENS,
    )
    estimated_cost = estimate["cost_usd"]

    if dry_run:
        return PatchDraftResult(
            status="dry_run",
            cost_usd=estimated_cost,
            latency_ms=0,
            model=model,
            input_tokens=estimate["input_tokens"],
            output_tokens=estimate["output_tokens"],
            request_id=request_id or "",
        )

    # Budget gate - slice-7 conservative-by-design contract.
    # 0 means "uncapped" (NOT recommended); ANY positive value is a
    # hard ceiling.
    if max_cost_usd is not None and max_cost_usd > 0 and estimated_cost > max_cost_usd:
        msg = (
            f"Patch drafter skipped: worst-case estimate "
            f"${estimated_cost:.4f} exceeds budget cap ${max_cost_usd:.4f}. "
            f"Raise patch_drafter_max_cost_usd to enable, or pass an "
            f"explicit max_cost_usd= override."
        )
        return PatchDraftResult(
            status="skipped_budget",
            cost_usd=0.0,
            latency_ms=0,
            model=model,
            input_tokens=estimate["input_tokens"],
            output_tokens=estimate["output_tokens"],
            error_class="BudgetCapExceeded",
            error_message=msg,
            request_id=request_id or "",
        )

    # Build the prompt + dispatch via the canonical Anthropic wrapper.
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        title=script_title or "(untitled)",
        error_message=error_message or "",
        script_source=script_source or "",
    )

    started = time.monotonic()
    try:
        from analyzers.claude_client import (
            call_messages_create, ClaudeCallError,
        )
    except ImportError as exc:
        return PatchDraftResult(
            status="error",
            error_class="MissingClaudeClient",
            error_message=str(exc),
            model=model,
            request_id=request_id or "",
        )

    try:
        result = call_messages_create(
            source="patch_drafter",
            group_name=group_name,
            request_id=request_id,
            model=model,
            max_tokens=_DEFAULT_MAX_OUTPUT_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except ClaudeCallError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        # Distinguish missing-credential from other failures
        status = "skipped_no_key" if exc.error_class == "MissingCredential" else "error"
        return PatchDraftResult(
            status=status,
            cost_usd=0.0,
            latency_ms=latency_ms,
            model=model,
            request_id=getattr(exc, "request_id", "") or (request_id or ""),
            error_class=exc.error_class or "ClaudeCallError",
            error_message=str(exc),
        )
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return PatchDraftResult(
            status="error",
            cost_usd=0.0,
            latency_ms=latency_ms,
            model=model,
            request_id=request_id or "",
            error_class=type(exc).__name__,
            error_message=str(exc),
        )

    response_text = _read_text_from_response(result.response)
    patch, explanation = _split_diff_and_explanation(response_text)
    return PatchDraftResult(
        status="success",
        patch=patch,
        explanation=explanation,
        response_text=response_text,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        model=result.model or model,
        request_id=result.request_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )


# ─────────────────────────────────────────────────────────────────────
# Internal: settings shim (allows tests to run without a settings file)
# ─────────────────────────────────────────────────────────────────────

def _setting(key: str, fallback):
    """Read a global setting with a fallback. Tolerates a missing /
    uninitialised settings module so tests can exercise the drafter
    without bootstrapping the full settings infrastructure.
    """
    try:
        from global_settings import get_settings
        s = get_settings()
        # GlobalSettings.get() takes a single positional argument
        # and returns the registered default for unknown keys.
        # Calling it with our own fallback would TypeError.
        v = s.get(key)
        if v is None:
            return fallback
        return v
    except Exception:
        return fallback


__all__ = [
    "PatchDraftResult",
    "compute_error_hash",
    "draft_patch_for_failed_task",
    "estimate_patch_cost_usd",
    "estimate_tokens_from_chars",
]

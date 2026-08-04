"""
Claude Analyzer
───────────────
Accepts SpeakesQuery result sets and returns structured analysis via the
Claude API.  All API interaction, cost control, and error handling is
internal.  This module never raises - it always returns an AnalysisResult.

Behavioural contract (from spec - do not change without documenting):
  - Gate logic ordering is authoritative (empty → budget → liquidity → route)
  - Budget kill switch must fire before any API call
  - Model routing uses spike_threshold_for_upgrade
  - Prompt caching via cache_control ephemeral on system message
  - Errors always convert to AnalysisResult(status="error"), never raise
  - The anthropic SDK is lazy-imported (never at module level)
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone


def _utc_today_iso() -> str:
    """UTC calendar date as ``YYYY-MM-DD``.

    M-AN-10 (2026-04-22): standardize on UTC so the daily budget window
    matches AG scheduler conventions and docker-container wall clocks
    regardless of host tz. The old ``date.today()`` rolled at midnight
    local time, which diverged from the AG scheduler's UTC cron and
    from subject-date headers on the email side.
    """
    return datetime.now(timezone.utc).date().isoformat()


def _utc_now_iso() -> str:
    """UTC timestamp as an ISO string, tz-aware (``…+00:00``)."""
    return datetime.now(timezone.utc).isoformat()
from typing import Dict, List, Optional

from analyzers.models import (
    ActionableMarket,
    AnalysisResult,
    AnalyzerConfig,
    UsageStats,
)

logger = logging.getLogger("analyzers.claude_analyzer")

# ── Token substitution regex (same pattern as Alert.py / MacroValidation) ──
_TOKEN_RE = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)\$")

# ── Pricing per million tokens (update if Anthropic changes pricing) ───────
_PRICING: Dict[str, Dict[str, float]] = {
    "claude-sonnet-4-6":       {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    # 2026-08-04: Opus 5 at 1.67x Sonnet 4.6 on both sides - within the
    # user's 3x ceiling for the options_edge_brief quality upgrade.
    "claude-opus-5":           {"input": 5.00, "output": 25.00},
    "claude-opus-4-8":         {"input": 5.00, "output": 25.00},
    "claude-opus-4-7":         {"input": 5.00, "output": 25.00},
    "claude-sonnet-5":         {"input": 3.00, "output": 15.00},
}

# ── Global token keys mapped to saved-search metadata field names ──────────
_GLOBAL_TOKEN_MAP = {
    "scheduled_search_name":        "name",
    "scheduled_search_description": "description",
    "scheduled_search_query":       "query",
    "scheduled_search_cron":        "cron_schedule",
    "scheduled_search_lookback":    "lookback",
    "scheduled_search_trigger":     "trigger",
    "scheduled_search_email":       "email_address",
    "scheduled_search_created_at":  "created_at",
}


# =====================================================================
# Token resolution helpers
# =====================================================================

def _truncate_multivalue(values: list, limit: int = 5) -> str:
    """Format a list of distinct values with truncation.

    Mirrors Alert.py ``_truncate_multivalue`` but with a default limit
    tuned for analyzer prompts (5 instead of 3).
    """
    if not values:
        return ""
    str_values = [str(v) for v in values]
    if len(str_values) <= limit:
        return ", ".join(f'"{v}"' for v in str_values)
    shown = ", ".join(f'"{v}"' for v in str_values[:limit])
    remaining = len(str_values) - limit
    return f'{shown}, ... [+] {remaining} TRUNCATED'


def resolve_analyzer_prompt(
    prompt_text: str,
    result_df,
    search_metadata: dict,
    execution_time: str,
    mv_truncate_limit: int = 5,
) -> str:
    """Resolve ``$token$`` placeholders in an analyzer prompt.

    Unlike ``Alert.render_email_body`` which substitutes per-row, this
    function aggregates across the entire DataFrame:

    - **Global tokens** (``$scheduled_search_name$``, ``$execution_time$``,
      etc.) resolve from saved-search metadata and runtime context.
    - **Column tokens** resolve to the distinct values in that column,
      truncated via ``_truncate_multivalue`` when they exceed the limit.
    - **Unresolved tokens** are left as-is so Claude can see them.

    Parameters
    ----------
    prompt_text : str
        Raw prompt with ``$token$`` placeholders.
    result_df : pandas.DataFrame
        Full query result set.
    search_metadata : dict
        The saved-search YAML record (name, description, query, etc.).
    execution_time : str
        ISO timestamp of when the query fired.
    mv_truncate_limit : int
        Max distinct values shown per column token before truncation.

    Returns
    -------
    str
        The prompt with all resolvable tokens substituted.
    """
    # 1. Build global token values
    global_values: Dict[str, str] = {}
    for token_name, meta_key in _GLOBAL_TOKEN_MAP.items():
        global_values[token_name] = str(search_metadata.get(meta_key, ""))

    global_values["execution_time"] = execution_time
    global_values["result_count"] = str(len(result_df))
    global_values["column_names"] = ", ".join(result_df.columns.tolist())

    # 2. Build column token values (distinct, truncated)
    column_values: Dict[str, str] = {}
    for col in result_df.columns:
        distinct = result_df[col].dropna().unique().tolist()
        column_values[col] = _truncate_multivalue(distinct, limit=mv_truncate_limit)

    # 3. Merge - globals take precedence over column names
    combined = {**column_values, **global_values}

    # 4. Substitute tokens
    def _replacer(match: re.Match) -> str:
        key = match.group(1)
        if key not in combined:
            return match.group(0)  # leave unresolved as-is
        return combined[key]

    return _TOKEN_RE.sub(_replacer, prompt_text)


def _result_df_to_json(result_df) -> str:
    """Serialize a DataFrame to compact JSON for the Claude user message.

    JSON is more token-efficient than CSV because column names appear once
    per record (no repeated header), and numeric values don't need quoting.
    """
    return result_df.to_json(orient="records", date_format="iso", default_handler=str)


# =====================================================================
# Cost helpers
# =====================================================================

def _compute_cost_cents(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return estimated cost in cents for a single API call."""
    rates = _PRICING.get(model, _PRICING.get("claude-haiku-4-5-20251001"))
    input_cost = (input_tokens / 1_000_000) * rates["input"]
    output_cost = (output_tokens / 1_000_000) * rates["output"]
    return round((input_cost + output_cost) * 100, 4)  # dollars → cents


# =====================================================================
# ClaudeAnalyzer
# =====================================================================

class ClaudeAnalyzer:
    """Accepts SpeakesQuery result sets and returns structured analysis.

    All API interaction, cost control, and error handling is internal.
    """

    def __init__(self, config: AnalyzerConfig, storage=None):
        """Initialize with config.  Validates API key presence.
        Does NOT make any API calls during init.

        The API key should be provided via ``config.api_key``, which the
        caller retrieves from the credential vault (script_id=-1,
        key_name="ANTHROPIC_API_KEY").

        Parameters
        ----------
        config : AnalyzerConfig
        storage : analyzers.storage.AnalyzerStorage, optional
            If provided, budget tracking is persisted to SQLite so it
            survives process restarts and is shared across instances.
        """
        self._config = config
        self._storage = storage
        self._lock = threading.Lock()

        # Budget tracking (resets daily) - seed from persistent store if available
        today = _utc_today_iso()
        if storage:
            persisted = storage.load_daily_budget(today)
            spent = persisted.get("total_cost_cents", 0.0)
            self._usage = UsageStats(
                total_input_tokens=persisted.get("total_input_tokens", 0),
                total_output_tokens=persisted.get("total_output_tokens", 0),
                total_calls=persisted.get("total_calls", 0),
                total_cost_cents=spent,
                budget_remaining_cents=float(config.daily_budget_cents) - spent,
                last_reset_date=today,
            )
        else:
            self._usage = UsageStats(
                budget_remaining_cents=float(config.daily_budget_cents),
                last_reset_date=today,
            )

        # API key comes from the credential vault via config - no env var lookup
        self._api_key = config.api_key

        if not self._api_key:
            logger.warning(
                "[!] Claude analyzer initialised without an API key. "
                "Store one via Settings → Claude Analyzer → API Key."
            )

    # ── Budget management ─────────────────────────────────────────

    def _maybe_reset_daily_budget(self):
        """Re-sync budget stats from the canonical ledger and reset on day rollover.

        H-AN-6 / X-2 (2026-04-22): every Claude call (analyzer, AG
        dispatcher, batch poller, Test-Claude button) writes to
        ``analyzer_budget`` via ``claude_client._record_daily_budget_usd``.
        The in-memory ``self._usage.budget_remaining_cents`` on any single
        ClaudeAnalyzer instance is therefore only a cache of the
        authoritative store. Re-reading on every budget check keeps the
        gate consistent with cross-caller writes. The SQLite read is a
        single-row lookup - cheap relative to a Claude call.
        """
        today = _utc_today_iso()
        day_rolled = today != self._usage.last_reset_date
        if day_rolled:
            logger.info("[i] Daily budget reset (new day: %s)", today)

        if self._storage:
            persisted = self._storage.load_daily_budget(today)
            spent = persisted.get("total_cost_cents", 0.0)
            self._usage = UsageStats(
                total_input_tokens=persisted.get("total_input_tokens", 0),
                total_output_tokens=persisted.get("total_output_tokens", 0),
                total_calls=persisted.get("total_calls", 0),
                total_cost_cents=spent,
                budget_remaining_cents=float(self._config.daily_budget_cents) - spent,
                last_reset_date=today,
            )
        elif day_rolled:
            # No persistence → purely in-memory rollover.
            self._usage = UsageStats(
                budget_remaining_cents=float(self._config.daily_budget_cents),
                last_reset_date=today,
            )

    def _record_usage(self, model: str, input_tokens: int, output_tokens: int):
        """Update cumulative usage after a successful API call.

        H-AN-6 / X-2 (2026-04-22): the SQLite persistence is now handled
        by ``claude_client._record_daily_budget_usd`` which runs on every
        success path (analyzer, AG dispatcher, batch_poller, Test-Claude
        button). This method now only updates the in-memory telemetry
        counter used by the /api/analyzer/stats UI - the authoritative
        budget number lives in ``analyzer_budget`` and is consulted by
        ``_maybe_reset_daily_budget``.
        """
        cost = _compute_cost_cents(model, input_tokens, output_tokens)
        with self._lock:
            self._usage.total_input_tokens += input_tokens
            self._usage.total_output_tokens += output_tokens
            self._usage.total_calls += 1
            self._usage.total_cost_cents += cost
            self._usage.budget_remaining_cents -= cost

            pct_used = (
                self._usage.total_cost_cents / self._config.daily_budget_cents * 100
                if self._config.daily_budget_cents > 0
                else 100.0
            )
            if pct_used >= 80:
                logger.warning(
                    "[!] Claude budget %.1f%% consumed (%.2f / %d cents)",
                    pct_used,
                    self._usage.total_cost_cents,
                    self._config.daily_budget_cents,
                )
        # NOTE: storage.record_usage intentionally NOT called here - see
        # docstring. claude_client owns the shared ledger write.

    def get_usage_stats(self) -> UsageStats:
        """Return cumulative token usage and cost for the current session."""
        with self._lock:
            self._maybe_reset_daily_budget()
            return UsageStats(
                total_input_tokens=self._usage.total_input_tokens,
                total_output_tokens=self._usage.total_output_tokens,
                total_calls=self._usage.total_calls,
                total_cost_cents=self._usage.total_cost_cents,
                budget_remaining_cents=self._usage.budget_remaining_cents,
                last_reset_date=self._usage.last_reset_date,
            )

    # ── Gate logic (pre-API checks) ──────────────────────────────

    def _gate_check(self, results: list | dict) -> Optional[str]:
        """Run pre-API gate checks in order.  Returns skip reason or None."""
        # Gate 1: API key present
        if not self._api_key:
            return "no_api_key"

        # Gate 2: Non-empty results
        if not results:
            return "empty_results"
        if isinstance(results, list) and len(results) == 0:
            return "empty_results"

        # Gate 3: Budget kill switch
        self._maybe_reset_daily_budget()
        if self._usage.budget_remaining_cents <= 0:
            return "budget_exceeded"

        # Gate 4: Min liquidity filter (skip if ALL rows below threshold)
        if isinstance(results, list) and all(isinstance(r, dict) for r in results):
            has_liquidity = any(
                r.get("liquidity", float("inf")) >= self._config.min_liquidity
                for r in results
            )
            if not has_liquidity and any("liquidity" in r for r in results):
                return "below_min_liquidity"

        return None  # All gates passed

    # ── Model routing ─────────────────────────────────────────────

    def _select_model(self, results: list | dict) -> str:
        """Route to primary (Sonnet) or triage (Haiku) based on data."""
        if isinstance(results, list):
            for row in results:
                if isinstance(row, dict):
                    spike = row.get("spike_multiple", 0)
                    try:
                        if float(spike) >= self._config.spike_threshold_for_upgrade:
                            return self._config.model_primary
                    except (TypeError, ValueError):
                        pass
        return self._config.model_triage

    # ── API call ──────────────────────────────────────────────────

    def _call_api(
        self,
        model: str,
        system_prompt: str,
        user_content: str,
    ) -> dict:
        """Make a single Claude API call.  Returns the raw API response.

        Routes through ``analyzers.claude_client.call_messages_create`` so
        retry policy, hard timeout, Parquet cost-log emission, and the
        SQLite request/response history all apply uniformly to every
        caller (this analyzer, the alert group dispatcher, the settings
        test button, etc.). The existing tests mock ``_call_api`` directly
        so they continue to bypass this wrapper entirely.
        """
        from analyzers.claude_client import call_messages_create
        # Headroom (2026-06-23): the per-scheduled-search analyzer is
        # alert analysis too, so it honors the global Headroom default.
        # No per-search override surface yet - resolve_use_headroom() with
        # no overrides returns the global default (and respects the
        # HEADROOM_DISABLE kill switch). Fails open to direct Anthropic.
        from analyzers.headroom import resolve_use_headroom

        system_block = (
            [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            if self._config.enable_cache
            else system_prompt
        )

        result = call_messages_create(
            source="analyzer",
            api_key_override=self._api_key,
            model=model,
            max_tokens=self._config.max_output_tokens,
            system=system_block,
            messages=[{"role": "user", "content": user_content}],
            use_headroom=resolve_use_headroom(),
        )
        return result.response

    def _call_batch_api(
        self,
        model: str,
        system_prompt: str,
        user_content: str,
        custom_id: str,
    ) -> str:
        """Submit a single analysis request via the Message Batches API.

        Returns the Anthropic batch_id.  The result will be retrieved
        asynchronously by the batch poller.

        Batch API pricing is 50% of standard rates.

        Batch calls don't fit the synchronous request/response mould of
        :func:`call_messages_create`, so this method still issues the raw
        SDK call - but records a ``batch_submit`` row to the Claude history
        store so every Claude-billable action is auditable.
        """
        import anthropic  # Lazy import per spec
        from analyzers.claude_history_store import ClaudeHistoryStore
        # H-AN-7 (2026-04-21): scrub Anthropic-key-shaped tokens out of the
        # payload before it lands in claude_api_history.sqlite. Previously
        # this path wrote request_body verbatim, exfiltrating any
        # ``sk-ant-*`` token an operator had accidentally pasted into the
        # system prompt or user_content. Shared with call_messages_create
        # via analyzers/_scrub.py.
        from analyzers._scrub import redact_kwargs, scrub_secrets

        client = anthropic.Anthropic(api_key=self._api_key)
        request_payload = {
            "model": model,
            "max_tokens": self._config.max_output_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
            "custom_id": custom_id,
        }
        safe_request_payload = redact_kwargs(request_payload)

        try:
            batch = client.messages.batches.create(
                requests=[
                    {
                        "custom_id": custom_id,
                        "params": {
                            "model": model,
                            "max_tokens": self._config.max_output_tokens,
                            "system": system_prompt,
                            "messages": [
                                {"role": "user", "content": user_content},
                            ],
                        },
                    }
                ]
            )
        except BaseException as exc:
            # M-AN-13 (2026-04-22): defend the history-record call itself.
            # Without this try/except, a DB-lock contention or disk-full
            # state inside ``record_call`` would replace the original
            # exception with a less-informative storage error, and the
            # forensic row would never land. The surrounding ``raise`` is
            # what actually drives retries / visibility at higher levels.
            try:
                ClaudeHistoryStore.get_instance().record_call(
                    source="batch_submit",
                    model=model,
                    status="error",
                    request_body=safe_request_payload,
                    response_body=None,
                    error_class=type(exc).__name__,
                    error_message=scrub_secrets(str(exc))[:2000],
                )
            except Exception as rec_exc:
                logger.warning(
                    "[!] Could not record batch-submit ERROR to history "
                    "(%s: %s); original exception follows.",
                    type(rec_exc).__name__, rec_exc,
                )
            raise

        logger.info(
            "[i] Batch request submitted: batch_id=%s, custom_id=%s",
            batch.id, custom_id,
        )

        try:
            ClaudeHistoryStore.get_instance().record_call(
                source="batch_submit",
                model=model,
                status="submitted",
                request_body=safe_request_payload,
                response_body={"batch_id": batch.id, "custom_id": custom_id},
                extra={"batch_id": batch.id, "custom_id": custom_id},
            )
        except Exception as exc:
            logger.warning("[!] Could not record batch submission: %s", exc)

        return batch.id

    @staticmethod
    def _extract_first_balanced_json_object(text: str) -> str | None:
        """Return the first ``{...}`` substring with balanced braces, or None.

        H-AN-3 (2026-04-21): Claude frequently emits prose before a JSON
        object without a markdown fence (e.g. ``"Here is the analysis: {...}"``).
        The old parse chain tried raw JSON, then a fenced block, then
        returned a generic error - never touching the common unfenced
        case. This helper scans character by character, tracks string
        literals + escape sequences, and returns the smallest balanced
        object starting at the first ``{``.
        """
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None

    @staticmethod
    def parse_response_text(raw_text: str, input_tokens: int = 0, output_tokens: int = 0) -> dict:
        """Parse raw Claude response text into a dict for AnalysisResult fields.

        This is a static version of ``_parse_response`` usable by both the
        synchronous analyzer and the batch poller (which doesn't have a
        full API response object).

        Parse strategy (H-AN-3, 2026-04-21):
          1. Try ``json.loads(raw_text)`` - Claude is well-behaved.
          2. Try a markdown fence match (```` ```json ... ``` ````), stripping
             whitespace around the captured block.
          3. Try a brace-balanced extract (prose-before-JSON case). This
             catches responses like ``"Here is the analysis: {...}"`` that
             the fence regex misses.
          4. Only then fall through to a generic ``Failed to parse`` error.
        """
        parsed = {
            "raw_response": raw_text,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

        # Sentinel distinguishes "haven't parsed anything yet" from "parsed
        # JSON null" - a real parse of ``"null"`` produces Python None and
        # must reach the non-dict guard below, not the fallback chain.
        _SENTINEL = object()
        data = _SENTINEL

        # Fast path: well-formed JSON from root.
        try:
            data = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError):
            data = _SENTINEL

        # Fallback 1: markdown fence. ``.strip()`` covers indented or padded
        # JSON inside the fence, which the old code passed to json.loads
        # with leading whitespace and a newline still attached.
        if data is _SENTINEL:
            json_match = re.search(
                r"```(?:json)?\s*\n?(.*?)\n?```",
                raw_text,
                re.DOTALL,
            )
            if json_match:
                try:
                    data = json.loads(json_match.group(1).strip())
                except (json.JSONDecodeError, TypeError):
                    data = _SENTINEL

        # Fallback 2: brace-balanced extract. Handles ``"Here is the JSON:\n{...}"``
        # and similar prose-preamble shapes that neither the root parse nor
        # the fence matcher catches.
        if data is _SENTINEL:
            candidate = ClaudeAnalyzer._extract_first_balanced_json_object(raw_text)
            if candidate is not None:
                try:
                    data = json.loads(candidate)
                except (json.JSONDecodeError, TypeError):
                    data = _SENTINEL

        if data is _SENTINEL:
            parsed["status"] = "error"
            parsed["error_message"] = "Failed to parse response as JSON"
            return parsed

        # Guard against non-dict JSON (list, scalar, null). Without this,
        # ``set(data.keys())`` raises AttributeError and propagates up into
        # the batch poller / analyze_search_result catch-all, where the
        # shape-mismatch cause is lost behind a generic "error handling
        # result" log. See tests/test_claude_analyzer.py::test_non_dict_json.
        if not isinstance(data, dict):
            parsed["status"] = "error"
            parsed["error_message"] = (
                f"Response JSON is not an object (got {type(data).__name__})"
            )
            return parsed

        expected_keys = {"alert_priority", "summary", "actionable_markets"}
        missing = expected_keys - set(data.keys())
        if missing:
            parsed["status"] = "error"
            parsed["error_message"] = f"Missing keys: {sorted(missing)}"
            return parsed

        parsed["status"] = "analyzed"
        parsed["alert_priority"] = data.get("alert_priority", "LOW")
        parsed["summary"] = data.get("summary", "")
        parsed["pattern_detected"] = data.get("pattern_detected", "")
        parsed["cross_reference_needed"] = data.get("cross_reference_needed", [])

        markets = []
        for m in data.get("actionable_markets", [])[:5]:
            if isinstance(m, dict):
                markets.append(ActionableMarket(
                    question=m.get("question", ""),
                    position=m.get("position", ""),
                    confidence=float(m.get("confidence", 0)),
                    reasoning=m.get("reasoning", ""),
                    estimated_roi=float(m.get("estimated_roi", 0)),
                ))
        parsed["actionable_markets"] = markets

        return parsed

    def _parse_response(self, response) -> dict:
        """Parse a Claude API response object into a dict for AnalysisResult fields."""
        raw_text = response.content[0].text if response.content else ""
        input_tokens = getattr(response.usage, "input_tokens", 0)
        output_tokens = getattr(response.usage, "output_tokens", 0)
        return self.parse_response_text(raw_text, input_tokens, output_tokens)

    # ── Main entry point ──────────────────────────────────────────

    def analyze(
        self,
        query_name: str,
        results: list | dict,
        result_df=None,
        system_prompt: str = "",
        boilerplate_prompt: str = "",
        search_metadata: Optional[dict] = None,
    ) -> AnalysisResult:
        """Main entry point.  Called by the pipeline after query execution.

        Args:
            query_name: The scheduled search identifier (e.g. "volume_spikes").
            results: List of dicts from the query result set.
            result_df: Optional full DataFrame (used for JSON attachment to Claude).
            system_prompt: The resolved analyzer prompt text (tokens already filled).
            boilerplate_prompt: Optional system-level prompt prepended to every
                analysis call (configured in Settings).
            search_metadata: The saved-search YAML record (for logging context).

        Returns:
            AnalysisResult dataclass.  Always returns, never raises.
            On failure or skip, returns AnalysisResult with status="skipped"
            or status="error" and empty analysis fields.
        """
        # ── Gate checks ──────────────────────────────────────────
        skip_reason = self._gate_check(results)
        if skip_reason:
            logger.info(
                "[i] Claude analysis skipped for '%s': %s", query_name, skip_reason
            )
            return AnalysisResult(status="skipped", skip_reason=skip_reason)

        # ── Truncate input rows per config ────────────────��──────
        if isinstance(results, list) and len(results) > self._config.max_input_rows:
            logger.info(
                "[i] Truncating results from %d to %d rows for Claude",
                len(results),
                self._config.max_input_rows,
            )
            results = results[: self._config.max_input_rows]

        # ── Model routing ────────────────────────────────────────
        model = self._select_model(results)

        # ── Build user message ───────────────────────────────────
        # Full JSON if DataFrame provided, otherwise serialized rows
        if result_df is not None:
            user_content = _result_df_to_json(result_df)
        else:
            user_content = json.dumps(results, indent=2, default=str)

        if not system_prompt:
            system_prompt = (
                "You are analyzing query results from a SpeakesQuery scheduled search. "
                "Respond with a JSON object containing: alert_priority (CRITICAL/HIGH/"
                "MODERATE/LOW/SKIP), summary (one sentence), actionable_markets (list of "
                "up to 5 objects with question, position, confidence, reasoning, "
                "estimated_roi), pattern_detected (string), cross_reference_needed "
                "(list of strings)."
            )

        # ── Prepend boilerplate prompt if configured ────────────
        if boilerplate_prompt and boilerplate_prompt.strip():
            system_prompt = boilerplate_prompt.strip() + "\n\n" + system_prompt

        # ── Batch mode: submit and return immediately ────────────
        if self._config.enable_batch:
            import uuid as _uuid
            custom_id = str(_uuid.uuid4())
            try:
                batch_id = self._call_batch_api(
                    model, system_prompt, user_content, custom_id,
                )
                return AnalysisResult(
                    status="batch_pending",
                    model_used=model,
                    batch_id=batch_id,
                    batch_custom_id=custom_id,
                )
            except Exception as exc:
                logger.warning(
                    "[!] Batch API submission failed for '%s', "
                    "falling back to synchronous: %s", query_name, exc,
                )
                # Fall through to the synchronous path below

        # ── API call with retry ──────────────────────────────────
        import time as _time

        last_error = None
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self._call_api(model, system_prompt, user_content)
                parsed = self._parse_response(response)

                # Record usage
                self._record_usage(
                    model,
                    parsed.get("input_tokens", 0),
                    parsed.get("output_tokens", 0),
                )

                cost = _compute_cost_cents(
                    model,
                    parsed.get("input_tokens", 0),
                    parsed.get("output_tokens", 0),
                )

                return AnalysisResult(
                    status=parsed.get("status", "error"),
                    alert_priority=parsed.get("alert_priority", "LOW"),
                    summary=parsed.get("summary", ""),
                    actionable_markets=parsed.get("actionable_markets", []),
                    pattern_detected=parsed.get("pattern_detected", ""),
                    cross_reference_needed=parsed.get("cross_reference_needed", []),
                    model_used=model,
                    input_tokens=parsed.get("input_tokens", 0),
                    output_tokens=parsed.get("output_tokens", 0),
                    cost_cents=cost,
                    error_message=parsed.get("error_message", ""),
                    raw_response=parsed.get("raw_response", ""),
                )

            except Exception as exc:
                last_error = exc
                exc_type = type(exc).__name__

                # Only retry on rate-limit (429) or server errors (5xx).
                # Never retry on 4xx auth/validation errors.
                is_retryable = False
                if hasattr(exc, "status_code"):
                    code = exc.status_code
                    is_retryable = code == 429 or (500 <= code < 600)
                elif "rate" in str(exc).lower() or "server" in str(exc).lower():
                    is_retryable = True

                if is_retryable and attempt < max_retries - 1:
                    backoff = 2 ** attempt  # 1s, 2s, 4s
                    logger.warning(
                        "[!] Claude API %s (attempt %d/%d), retrying in %ds: %s",
                        exc_type, attempt + 1, max_retries, backoff, exc,
                    )
                    _time.sleep(backoff)
                    continue

                # Non-retryable or final attempt
                logger.error(
                    "[x] Claude API error for '%s' (attempt %d/%d): %s: %s",
                    query_name, attempt + 1, max_retries, exc_type, exc,
                )
                break

        return AnalysisResult(
            status="error",
            model_used=model,
            error_message=f"{type(last_error).__name__}: {last_error}",
        )

    # ── Filter gate (post-analysis) ──────────────────────────────

    def evaluate_filter(
        self,
        analysis: AnalysisResult,
        filter_question: str,
    ) -> AnalysisResult:
        """Evaluate a boolean filter question against a completed analysis.

        Sends the analysis summary + actionable markets to Claude (always
        Haiku for cost) and asks the filter question.  Expects a YES or NO
        answer.  If YES → ``filter_passed=True`` (send alert).  If NO →
        ``filter_passed=False`` (suppress alert).

        On any failure the filter defaults to passed (send the alert) so
        that errors never silently suppress notifications.

        Mutates and returns the same AnalysisResult with ``filter_passed``
        and ``filter_answer`` populated.
        """
        if not filter_question or not filter_question.strip():
            analysis.filter_passed = True
            return analysis

        if analysis.status != "analyzed":
            analysis.filter_passed = True
            return analysis

        # Budget check - if budget exhausted, default to pass (send alert)
        self._maybe_reset_daily_budget()
        if self._usage.budget_remaining_cents <= 0:
            logger.warning(
                "[!] Filter gate skipped (budget exhausted); defaulting to pass."
            )
            analysis.filter_passed = True
            analysis.filter_answer = "BUDGET_EXHAUSTED"
            return analysis

        # Build context from the analysis
        context_parts = [f"Analysis summary: {analysis.summary}"]
        if analysis.alert_priority:
            context_parts.append(f"Alert priority: {analysis.alert_priority}")
        if analysis.pattern_detected:
            context_parts.append(f"Pattern detected: {analysis.pattern_detected}")
        for i, m in enumerate(analysis.actionable_markets[:5], 1):
            context_parts.append(
                f"Market {i}: {m.question} - position={m.position}, "
                f"confidence={m.confidence}, reasoning={m.reasoning}"
            )
        context = "\n".join(context_parts)

        system_prompt = (
            "You are a boolean filter for an alerting system. You will receive "
            "an analysis of query results and a yes/no question. Respond with "
            "EXACTLY one word: YES or NO. Do not explain, do not qualify, do "
            "not add any other text."
        )
        user_content = f"{context}\n\nQuestion: {filter_question.strip()}"

        try:
            # Always use triage model (Haiku) for filter - cheap and fast
            response = self._call_api(
                self._config.model_triage, system_prompt, user_content
            )

            raw = response.content[0].text.strip().upper() if response.content else ""
            input_tokens = getattr(response.usage, "input_tokens", 0)
            output_tokens = getattr(response.usage, "output_tokens", 0)

            self._record_usage(self._config.model_triage, input_tokens, output_tokens)

            filter_cost = _compute_cost_cents(
                self._config.model_triage, input_tokens, output_tokens
            )
            analysis.cost_cents += filter_cost
            analysis.input_tokens += input_tokens
            analysis.output_tokens += output_tokens

            # Parse the answer - look for standalone YES or NO as a word
            # Use word boundary matching to avoid false positives like
            # "NOT" containing "NO" or "YESTERDAY" containing "YES".
            has_yes = bool(re.search(r"\bYES\b", raw))
            has_no = bool(re.search(r"\bNO\b", raw))
            if has_yes and not has_no:
                analysis.filter_passed = True
                analysis.filter_answer = "YES"
            elif has_no and not has_yes:
                analysis.filter_passed = False
                analysis.filter_answer = "NO"
            else:
                # Ambiguous response - default to pass (send alert)
                logger.warning(
                    "[!] Ambiguous filter response: %r - defaulting to pass.", raw
                )
                analysis.filter_passed = True
                analysis.filter_answer = f"AMBIGUOUS: {raw[:100]}"

            logger.info(
                "[i] Filter gate result: %s (question: %s)",
                analysis.filter_answer, filter_question[:80],
            )

        except Exception as exc:
            logger.warning(
                "[!] Filter gate API call failed - defaulting to pass: %s", exc
            )
            analysis.filter_passed = True
            analysis.filter_answer = f"ERROR: {exc}"

        return analysis

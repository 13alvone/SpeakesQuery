"""
Batch Result Poller
───────────────────
Periodically checks pending Anthropic Message Batches for completion,
retrieves results, stores analysis outcomes, updates budget, and
triggers deferred email alerts.

Designed to run as an APScheduler interval job alongside the scheduled
search engine.
"""

import json
import logging
import re
from datetime import datetime, timezone


def _utc_today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
from typing import Optional

from analyzers.models import AnalysisResult
from analyzers.storage import AnalyzerStorage

logger = logging.getLogger("analyzers.batch_poller")


def poll_pending_batches(storage: Optional[AnalyzerStorage] = None) -> int:
    """Check all pending batch requests and process completed results.

    Returns the number of results processed.  Never raises - all errors
    are logged and skipped so the scheduler keeps running.
    """
    if storage is None:
        storage = AnalyzerStorage()

    pending_ids = storage.get_pending_batch_ids()
    if not pending_ids:
        return 0

    # Lazy import so the SDK is only needed when batch mode is active
    try:
        import anthropic
    except ImportError:
        logger.error("[x] anthropic SDK not installed; cannot poll batches.")
        return 0

    # Retrieve API key from credential vault
    api_key = _get_api_key()
    if not api_key:
        logger.warning("[!] No API key available for batch polling.")
        return 0

    client = anthropic.Anthropic(api_key=api_key)
    total_processed = 0

    for batch_id in pending_ids:
        try:
            total_processed += _process_batch(client, batch_id, storage)
        except Exception as exc:
            logger.error(
                "[x] Error processing batch %s: %s", batch_id, exc,
            )

    if total_processed:
        logger.info("[i] Batch poller processed %d result(s).", total_processed)

    return total_processed


def _get_api_key() -> str:
    """Retrieve the analyzer API key from the credential vault."""
    try:
        from global_settings import get_settings
        from scheduled_input_engine.credentials import CredentialVault
        import os

        settings = get_settings()
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        vault_db = os.path.join(project_root, "credentials.sqlite")
        vault = CredentialVault(vault_db, settings.get("credential_key_dir"))
        return vault.retrieve(-1, "ANTHROPIC_API_KEY")
    except Exception:
        return ""


# M-AN-9 (2026-04-22): transient-error retry classifier mirrors the
# live-call wrapper in claude_client. Batches API calls that fail with
# APIConnectionError / APITimeoutError / 5xx should be re-tried once;
# 401 / 404 / invalid-batch errors pass through.
_RETRYABLE_BATCH_ERRORS = frozenset({
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "ServiceUnavailableError",
    "GatewayTimeoutError",
})


def _retry_batch_retrieve(client, batch_id: str, attempts: int = 2):
    """Retrieve a batch with one retry on transient error. Returns the batch or None."""
    import time as _t
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return client.messages.batches.retrieve(batch_id)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            name = type(exc).__name__
            if attempt >= attempts or name not in _RETRYABLE_BATCH_ERRORS:
                logger.warning(
                    "[!] Batch retrieve failed (batch=%s, attempt=%d/%d, "
                    "err=%s): %s",
                    batch_id, attempt, attempts, name, exc,
                )
                return None
            backoff = min(2 ** (attempt - 1), 10)
            logger.warning(
                "[!] Transient batch retrieve error (batch=%s, attempt=%d, "
                "err=%s); retrying in %ds: %s",
                batch_id, attempt, name, backoff, exc,
            )
            _t.sleep(backoff)
    if last_exc is not None:
        logger.warning(
            "[!] Batch retrieve exhausted retries for %s: %s",
            batch_id, last_exc,
        )
    return None


def _process_batch(client, batch_id: str, storage: AnalyzerStorage) -> int:
    """Process a single batch. Returns number of results handled.

    M-AN-9 (2026-04-22): retrieve now retries once on transient errors
    rather than aborting the poll cycle and waiting for the next tick.
    Result iteration tracks a ``last_processed_index`` in storage so a
    mid-iteration failure doesn't re-process rows that already landed.
    """
    batch = _retry_batch_retrieve(client, batch_id)
    if batch is None:
        return 0

    if batch.processing_status == "in_progress":
        logger.debug("[i] Batch %s still in progress.", batch_id)
        return 0

    if batch.processing_status != "ended":
        logger.warning(
            "[!] Batch %s has unexpected status: %s",
            batch_id, batch.processing_status,
        )
        return 0

    # Resume from the checkpoint so an interrupted prior cycle doesn't
    # reprocess rows. ``get_batch_progress`` returns 0 when absent.
    start_index = storage.get_batch_progress(batch_id) or 0
    processed = 0
    try:
        for idx, result in enumerate(client.messages.batches.results(batch_id)):
            if idx < start_index:
                continue  # already handled in a prior cycle
            try:
                _handle_batch_result(result, storage)
                processed += 1
            except Exception as exc:
                logger.error(
                    "[x] Error handling result %s from batch %s: %s",
                    getattr(result, "custom_id", "?"), batch_id, exc,
                )
            # Record progress after each row (cheap: single UPDATE).
            storage.set_batch_progress(batch_id, idx + 1)
    except Exception as exc:
        logger.error(
            "[x] Iteration failed for batch %s (processed=%d so far; "
            "next cycle resumes from saved checkpoint): %s",
            batch_id, processed, exc,
        )
    return processed


def _handle_batch_result(result, storage: AnalyzerStorage) -> None:
    """Handle a single MessageBatchIndividualResponse."""
    from analyzers.claude_analyzer import ClaudeAnalyzer, _compute_cost_cents

    custom_id = result.custom_id
    request_info = storage.get_request(custom_id)

    if not request_info:
        logger.warning("[!] No stored request for custom_id=%s; skipping.", custom_id)
        return

    result_type = result.result.type

    if result_type == "succeeded":
        # Extract response text and usage
        message = result.result.message
        raw_text = message.content[0].text if message.content else ""
        input_tokens = getattr(message.usage, "input_tokens", 0)
        output_tokens = getattr(message.usage, "output_tokens", 0)

        # Parse the response using the shared static method
        parsed = ClaudeAnalyzer.parse_response_text(
            raw_text, input_tokens, output_tokens,
        )

        model = request_info.get("model", "")
        cost = _compute_cost_cents(model, input_tokens, output_tokens)
        # Batch API is 50% cheaper
        cost = cost * 0.5

        analysis = AnalysisResult(
            status=parsed.get("status", "error"),
            alert_priority=parsed.get("alert_priority", "LOW"),
            summary=parsed.get("summary", ""),
            actionable_markets=parsed.get("actionable_markets", []),
            pattern_detected=parsed.get("pattern_detected", ""),
            cross_reference_needed=parsed.get("cross_reference_needed", []),
            model_used=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cents=cost,
            error_message=parsed.get("error_message", ""),
            raw_response=raw_text,
            batch_id=request_info.get("batch_id", ""),
            batch_custom_id=custom_id,
        )

        # Run filter gate if enabled on the original request
        if request_info.get("filter_enabled") and analysis.status == "analyzed":
            filter_question = request_info.get("filter_question", "")
            if filter_question:
                _run_deferred_filter(analysis, filter_question, storage)

        # Record usage in persistent budget
        # M-AN-10 (2026-04-22): UTC so the ledger window aligns with the
        # AG scheduler + email subject dates.
        storage.record_usage(
            _utc_today_iso(), input_tokens, output_tokens, cost,
        )

        # Store the analysis result
        execution_time = request_info.get("created_at", _utc_now_iso())
        search_name = request_info.get("search_name", "")
        storage.store_result(search_name, execution_time, analysis)

        # Mark batch request as completed
        storage.mark_batch_completed(custom_id, "succeeded", raw_text)

        logger.info(
            "[i] Batch result for '%s': priority=%s, cost=%.2f cents",
            search_name, analysis.alert_priority, cost,
        )

    elif result_type == "errored":
        error_msg = str(getattr(result.result, "error", "Unknown error"))
        storage.mark_batch_completed(custom_id, "errored", error_msg)
        logger.warning(
            "[!] Batch result errored for custom_id=%s: %s",
            custom_id, error_msg,
        )

    elif result_type == "expired":
        storage.mark_batch_completed(custom_id, "expired")
        logger.warning(
            "[!] Batch result expired for custom_id=%s", custom_id,
        )

    elif result_type == "canceled":
        storage.mark_batch_completed(custom_id, "canceled")
        logger.info(
            "[i] Batch result canceled for custom_id=%s", custom_id,
        )


def _run_deferred_filter(
    analysis: AnalysisResult,
    filter_question: str,
    storage: AnalyzerStorage,
) -> None:
    """Run the boolean filter gate on a batch result.

    Uses the same fail-open principle: errors default to filter_passed=True.
    """
    try:
        from analyzers.claude_analyzer import ClaudeAnalyzer
        from analyzers.models import AnalyzerConfig
        from global_settings import get_settings

        settings = get_settings()
        api_key = _get_api_key()
        if not api_key:
            analysis.filter_passed = True
            analysis.filter_answer = "NO_API_KEY"
            return

        config = AnalyzerConfig(
            api_key=api_key,
            model_triage=settings.get("claude_analyzer_model_triage"),
            daily_budget_cents=settings.get("claude_analyzer_daily_budget_cents"),
        )
        analyzer = ClaudeAnalyzer(config, storage=storage)
        analyzer.evaluate_filter(analysis, filter_question)

    except Exception as exc:
        logger.warning(
            "[!] Deferred filter gate failed - defaulting to pass: %s", exc,
        )
        analysis.filter_passed = True
        analysis.filter_answer = f"ERROR: {exc}"

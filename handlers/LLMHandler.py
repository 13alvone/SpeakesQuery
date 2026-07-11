"""
LLMHandler - Phase 2 / Bet 3 slice 4 (the user-visible Phase 2 deliverable),
extended in slice 7 with a per-call budget gate + dry-run cost preview.

Implements the ``| llm`` SPQL pipe: per-row LLM application using the
slice-2 router + slice-3 cache. Each row's text columns become the
``<data>`` block appended to the operator's prompt; the model output
lands in a new ``_llm_output`` column alongside cost / latency /
status / error metadata.

Budget gate (slice 7) - ``max_cost_usd=N``
-------------------------------------------
Hard ceiling on cumulative cost. The handler tracks rolling actual cost
as it iterates; BEFORE each call it asks the conservative cost estimator
"would the next call push us past N?". If yes, processing stops, the
already-processed rows are returned, and a sentinel row with
``_llm_status="budget_exceeded"`` is appended so downstream pipes see
the boundary explicitly. ``max_cost_usd=0`` (or ``None``) means no cap.

Cache hits cost ``$0`` and never advance the cumulative actual cost,
but the estimator can't know in advance whether a call will hit cache
- so the gate is conservative-by-design (it might stop one call early
for what would have been a cache hit). That trade-off prevents
busting the cap on a string of cache misses.

Dry-run (slice 7) - ``dry_run=true``
-------------------------------------
Pre-flight cost preview. Builds every prompt that WOULD be sent, runs
the static estimator, and returns a single-row DataFrame with
``_dry_run=True`` plus ``_estimated_cost_usd``, ``_estimated_input_tokens``,
``_estimated_output_tokens``, ``_row_count``, ``_llm_status="dry_run"``.
**Zero provider calls. Zero cache lookups. Zero history capture.** The
sentinel column ``_dry_run`` lets downstream pipes branch (``| where
_dry_run = true``).

Wire shape per row::

    {prompt}

    <data>
    {col1}: {value1}
    {col2}: {value2}
    ...
    </data>

The ``<data>`` boundary tags follow the prompt-injection-mitigation
pattern from the ROADMAP risk register (Bet 3 / Phase 2 risk #1):
the system prompt + boundary tags signal to the model that the
``<data>`` content is operator-supplied data, not instructions.
Slice 8 will explicitly enforce + test this contract; slice 4 lays
down the format.

Per-row error capture
---------------------
A failure on one row (transient HTTP error, missing API key for a
particular provider, etc.) does NOT fail the whole pipe. The errored
row gets ``_llm_status="error"``, ``_llm_output=""``, the error class
+ message in ``_llm_error``, and ``_llm_cost_usd=0``. Downstream pipes
can ``| where _llm_status="success"`` to filter cleanly.

Cache behavior
--------------
Cache is opt-in via ``use_cache=true|false`` (default ``true``). When
enabled, cache hits return ``_llm_cost_usd=0.0`` and
``_llm_latency_ms=0`` - the cache-hit signature already established in
slice 3. Iterative prompt design becomes economical: re-running a
pipe with a tweaked downstream filter doesn't re-pay for the LLM
calls that produced unchanged inputs.
"""

from __future__ import annotations

import json
import logging
from typing import Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# Default cap on rows fed into a single | llm_batch prompt. Matches the
# convention from `claude_analyzer_max_input_rows` (which defaults to
# 20). Operators with longer-context models can override per-call.
_DEFAULT_BATCH_MAX_ROWS = 20


# ── Slice 7: budget gate + dry-run constants ────────────────────────

# Status sentinels for the budget gate. Distinct from "success" / "error"
# so downstream filters can target each case explicitly:
#   ``| where _llm_status="success"``        → only completed rows
#   ``| where _llm_status="budget_exceeded"`` → just the boundary marker
#   ``| where _llm_status="dry_run"``        → the cost preview row
_BUDGET_EXCEEDED_STATUS = "budget_exceeded"
_DRY_RUN_STATUS = "dry_run"


def _resolve_budget_cap(max_cost_usd: Optional[float]) -> Optional[float]:
    """Normalise the budget kwarg into ``None`` (uncapped) or positive float.

    Accepts ``None``, ``0``, ``0.0`` as "no cap" - matches the ``0 = unlimited``
    convention used by ``llm_default_max_cost_usd`` setting.
    """
    if max_cost_usd is None:
        return None
    try:
        cap = float(max_cost_usd)
    except (TypeError, ValueError) as exc:
        raise LLMPipeError(
            f"max_cost_usd must be a number, got {max_cost_usd!r}"
        ) from exc
    if cap <= 0:
        return None
    return cap


# ── Errors ──────────────────────────────────────────────────────────

class LLMPipeError(ValueError):
    """Raised on misuse of the ``| llm`` pipe (missing kwargs, bad
    field reference, etc.). Subclasses ``ValueError`` so SPQL's
    existing error-formatting paths surface a clean message rather
    than a stack trace.
    """


# ── Text-column discovery on pandas DataFrames ──────────────────────

# Column names that look text-y but should never be embedded into the
# per-row prompt. Mostly the slice-3 cache + slice-1 sidecar
# bookkeeping fields, plus this slice's own outputs (so re-running
# `| llm` doesn't recursively feed prior outputs as input). The
# ``_dry_run`` / ``_estimated_*`` columns from slice 7 are excluded too
# - re-running on a dry-run preview shouldn't feed the preview metadata
# as model input.
_EXCLUDED_TEXT_COLUMNS = frozenset({
    "_epoch", "_similarity", "_row_id", "_source_file",
    "_llm_output", "_llm_model", "_llm_provider",
    "_llm_cost_usd", "_llm_latency_ms",
    "_llm_status", "_llm_error", "_llm_request_id",
    "_llm_input_row_count",
    "_dry_run", "_estimated_cost_usd",
    "_estimated_input_tokens", "_estimated_output_tokens",
    "_row_count", "_max_tokens",
    # Slice-9 (Phase 4 / Bet 3 slice 1): | llm_route metadata.
    # Excluded so a re-run of | llm / | llm_batch / | llm_route on
    # the prior pipe's output doesn't feed cascade metadata back as
    # input text.
    "_llm_route_escalated", "_llm_route_stage_1_output",
    "_llm_route_confidence",
    # Phase 4 / Bet 3 slice 2: | llm_refine drafter/critic metadata.
    # Same excluded-from-feed-back rationale; drafts/critiques arrays
    # would otherwise become a JSON-shaped text input on re-run.
    "_llm_refine_rounds", "_llm_refine_drafts",
    "_llm_refine_critiques", "_llm_refine_converged",
    # Phase 4 / Bet 3 slice 3: | llm_ensemble multi-model voting metadata.
    # Per-model outputs + agreement metric; same exclude rationale.
    "_llm_ensemble_models", "_llm_ensemble_outputs",
    "_llm_ensemble_agreement", "_llm_ensemble_aggregator",
    # Phase 4 / Bet 3 slice 4: | llm_until convergence-loop metadata.
    # Per-iteration outputs + convergence telemetry; same exclude rationale.
    "_llm_until_iterations", "_llm_until_outputs",
    "_llm_until_converged", "_llm_until_convergence_reason",
})


def _is_text_dtype(series: pd.Series) -> bool:
    if series.dtype == "object":
        return True
    if pd.api.types.is_string_dtype(series):
        return True
    return False


def _df_text_columns(df: pd.DataFrame) -> list[str]:
    cols: list[str] = []
    for c in df.columns:
        if c in _EXCLUDED_TEXT_COLUMNS:
            continue
        if _is_text_dtype(df[c]):
            cols.append(c)
    return cols


def _resolve_columns(
    df: pd.DataFrame, field: Optional[str],
) -> list[str]:
    """Resolve which columns to feed as the per-row data block."""
    if field is not None:
        if field not in df.columns:
            raise LLMPipeError(
                f"field={field!r} does not exist in the input "
                f"(columns: {list(df.columns)})"
            )
        return [field]
    cols = _df_text_columns(df)
    if not cols:
        raise LLMPipeError(
            "No text columns to feed the LLM. Either the input has "
            "no string columns, or all of them are reserved "
            "(_epoch, _similarity, _row_id, _source_file, _llm_*). "
            "Use field=<column> to override, or pre-process with "
            "`| eval text=tostring(<col>)` to produce a text column."
        )
    return cols


def _format_row(
    row: pd.Series, columns: Sequence[str],
) -> str:
    """Produce the per-row ``<data>`` block text.

    Format: ``{column}: {value}\\n`` for each column. ``None`` / NaN
    cells become empty values; the column name is preserved so the
    model sees the schema even on missing data.
    """
    pieces: list[str] = []
    for c in columns:
        v = row.get(c)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            v = ""
        pieces.append(f"{c}: {v}")
    return "\n".join(pieces)


def build_full_prompt(
    user_prompt: str, row: pd.Series, columns: Sequence[str],
) -> str:
    """Public for tests + future composition. Wraps the row in
    ``<data>...</data>`` boundary tags per the prompt-injection-
    mitigation pattern.
    """
    row_text = _format_row(row, columns)
    return f"{user_prompt}\n\n<data>\n{row_text}\n</data>"


# ── Slice 7 helpers: dry-run preview + budget sentinel ──────────────

def _dry_run_preview(
    *,
    model: str,
    prompts: list[str],
    system: Optional[str],
    max_tokens: Optional[int],
    pipe_label: str,
) -> pd.DataFrame:
    """Build the well-shaped 1-row dry-run preview DataFrame.

    No provider call. No cache lookup. No history capture. Used by
    BOTH ``llm_pipe`` and ``llm_batch_pipe`` - the only thing that
    differs is ``len(prompts)`` (per-row mode passes one prompt per
    input row; batch mode passes the single packed prompt).

    On any estimator error (unknown model, invalid kwargs), we still
    return a single-row DataFrame but with ``_llm_status="error"`` and
    the error message in ``_llm_error`` so downstream pipes don't blow
    up. The cost surfaces remain zero.
    """
    from analyzers.llm_router import estimate_cost_usd, LLMRouterError

    try:
        est = estimate_cost_usd(
            model, prompts, system=system, max_tokens=max_tokens,
        )
    except LLMRouterError as exc:
        logger.warning(
            "[!] %s dry_run estimator failed: %s - %s",
            pipe_label, exc.error_class, exc,
        )
        return pd.DataFrame({
            "_dry_run": [True],
            "_estimated_cost_usd": [0.0],
            "_estimated_input_tokens": [0],
            "_estimated_output_tokens": [0],
            "_row_count": [len(prompts)],
            "_llm_model": [model],
            "_llm_provider": [getattr(exc, "provider", "") or ""],
            "_max_tokens": [int(max_tokens or 0)],
            "_llm_output": [""],
            "_llm_cost_usd": [0.0],
            "_llm_latency_ms": [0],
            "_llm_status": ["error"],
            "_llm_error": [f"{exc.error_class}: {exc}"[:1000]],
        })

    logger.info(
        "[i] %s dry_run: model=%s rows=%d est_cost_usd=%.6f "
        "est_in=%d est_out=%d max_tokens=%d (NO provider call made)",
        pipe_label, est["model_id"], est["n_calls"],
        est["cost_usd"], est["input_tokens"], est["output_tokens"],
        est["max_tokens"],
    )
    return pd.DataFrame({
        "_dry_run": [True],
        "_estimated_cost_usd": [float(est["cost_usd"])],
        "_estimated_input_tokens": [int(est["input_tokens"])],
        "_estimated_output_tokens": [int(est["output_tokens"])],
        "_row_count": [int(est["n_calls"])],
        "_llm_model": [est["model_id"]],
        "_llm_provider": [est["provider"]],
        "_max_tokens": [int(est["max_tokens"])],
        # Mirror the standard | llm output columns so dry-run rows
        # compose with downstream pipes that filter on _llm_status.
        "_llm_output": [""],
        "_llm_cost_usd": [0.0],
        "_llm_latency_ms": [0],
        "_llm_status": [_DRY_RUN_STATUS],
        "_llm_error": [""],
    })


def _budget_sentinel_row(
    *,
    model: str,
    rows_processed: int,
    rows_total: int,
    cumulative_cost: float,
    next_estimate: float,
    cap: float,
) -> dict:
    """One sentinel row appended after the partial result when the
    budget cap stops processing. Empty row content (input columns
    NaN) plus the diagnostic columns populated.

    Caller assembles this into the output DataFrame; the dict shape
    matches the ``_llm_*`` columns produced by the success path so
    downstream filters keep working (``| where _llm_status="success"``
    cleanly excludes both errors and budget-exceeded boundaries).
    """
    skipped = max(rows_total - rows_processed, 0)
    msg = (
        f"Budget cap ${cap:.6f} would be exceeded by next call "
        f"(cumulative=${cumulative_cost:.6f} + estimate=${next_estimate:.6f}). "
        f"{rows_processed}/{rows_total} rows processed; {skipped} skipped. "
        "Raise max_cost_usd or lower max_tokens to continue."
    )
    return {
        "_llm_output": "",
        "_llm_model": model,
        "_llm_provider": "",
        "_llm_cost_usd": 0.0,
        "_llm_latency_ms": 0,
        "_llm_status": _BUDGET_EXCEEDED_STATUS,
        "_llm_error": msg,
    }


# ── Public pipe implementation ──────────────────────────────────────

def llm_pipe(
    df: pd.DataFrame,
    *,
    model: str,
    prompt: str,
    system: Optional[str] = None,
    field: Optional[str] = None,
    use_cache: bool = True,
    max_tokens: Optional[int] = None,
    max_cost_usd: Optional[float] = None,
    dry_run: bool = False,
) -> pd.DataFrame:
    """Apply an LLM to each row of ``df``.

    Parameters
    ----------
    df :
        Input DataFrame.
    model :
        Registry id of the model to call (e.g.
        ``"claude-haiku-4-5-20251001"``, ``"lmstudio-remote"``).
        Looked up in :mod:`model_store`.
    prompt :
        Operator instructions. Each row's text columns are appended
        to this in a ``<data>...</data>`` block before being sent to
        the model.
    system :
        Optional system prompt threaded through to the provider.
    field :
        If supplied, embed only this single column. Default:
        concatenate all auto-detected text columns.
    use_cache :
        When True (default), reuse cached responses keyed by
        ``content_hash``. Cache hits cost ``$0`` and report
        ``_llm_latency_ms=0``.
    max_tokens :
        Override for the per-record ``max_output_tokens`` default
        from the registry.
    max_cost_usd :
        **Slice 7 budget gate.** Hard ceiling on cumulative actual
        cost (USD). Pre-call estimator checks each row before
        dispatch - if the next call would push cumulative + estimate
        past the cap, processing stops and a sentinel row is
        appended (``_llm_status="budget_exceeded"``). ``None`` /
        ``0`` / ``0.0`` means no cap. Cache hits don't advance the
        cumulative ($0 cost) but the estimate still applies - the
        gate is conservative-by-design.
    dry_run :
        **Slice 7 cost preview.** When True, returns a single-row
        preview DataFrame with the estimated cost - NO provider
        call, NO cache lookup, NO history capture. Useful before
        running a large pipe ("would this cost more than $X?").

    Returns
    -------
    DataFrame
        Per-row mode (default): input rows + 7 new columns
        (``_llm_output``, ``_llm_model``, ``_llm_provider``,
        ``_llm_cost_usd``, ``_llm_latency_ms``, ``_llm_status``,
        ``_llm_error``). May be cut short with a sentinel row when
        ``max_cost_usd`` triggers.

        Dry-run mode: single-row DataFrame with ``_dry_run=True``,
        ``_estimated_cost_usd``, ``_estimated_input_tokens``,
        ``_estimated_output_tokens``, ``_row_count``, plus the
        standard ``_llm_*`` columns (status="dry_run", cost=0).

    Raises
    ------
    LLMPipeError
        On bad inputs (missing prompt, missing field, no text columns,
        invalid ``max_cost_usd``).
    """
    if not isinstance(model, str) or not model.strip():
        raise LLMPipeError("llm requires a non-empty model id.")
    if not isinstance(prompt, str) or not prompt:
        raise LLMPipeError("llm requires a non-empty prompt string.")

    cap = _resolve_budget_cap(max_cost_usd)

    # Empty input → return well-shaped empty DataFrame with all output
    # columns present. Downstream pipes can compose without a special
    # case. Dry-run on empty input also returns the dry-run-shaped
    # preview row with row_count=0 - that's strictly more informative
    # than an empty DataFrame for a "what would this cost?" query.
    if df is None or len(df) == 0:
        if dry_run:
            return _dry_run_preview(
                model=model, prompts=[],
                system=system, max_tokens=max_tokens,
                pipe_label="llm",
            )
        out = df.copy() if df is not None else pd.DataFrame()
        out["_llm_output"] = pd.array([], dtype="object")
        out["_llm_model"] = pd.array([], dtype="object")
        out["_llm_provider"] = pd.array([], dtype="object")
        out["_llm_cost_usd"] = pd.array([], dtype="float64")
        out["_llm_latency_ms"] = pd.array([], dtype="int64")
        out["_llm_status"] = pd.array([], dtype="object")
        out["_llm_error"] = pd.array([], dtype="object")
        return out

    cols = _resolve_columns(df, field)

    # ── Dry-run path ───────────────────────────────────────────────
    # Build every prompt that WOULD be sent and hand them to the
    # estimator. Zero provider calls, zero cache lookups. The
    # money-leak canary tests pin this contract.
    if dry_run:
        prompts_per_row = [
            build_full_prompt(prompt, df.iloc[i], cols)
            for i in range(len(df))
        ]
        return _dry_run_preview(
            model=model, prompts=prompts_per_row,
            system=system, max_tokens=max_tokens,
            pipe_label="llm",
        )

    # Lazy-import the router to keep the handler import lightweight
    # for paths that don't actually touch LLMs.
    from analyzers.llm_router import (
        call_llm, estimate_cost_usd, LLMRouterError,
    )

    n = len(df)
    outputs: list[str] = []
    models: list[str] = []
    providers: list[str] = []
    costs: list[float] = []
    latencies: list[int] = []
    statuses: list[str] = []
    errors: list[str] = []

    cumulative_cost = 0.0
    sentinel: Optional[dict] = None
    rows_processed = 0

    for i in range(n):
        row = df.iloc[i]
        full_prompt = build_full_prompt(prompt, row, cols)

        # ── Slice 7 pre-call budget check ──────────────────────────
        # Estimate THIS row's cost; if cumulative + estimate would
        # exceed the cap, stop here and emit the sentinel boundary.
        # Conservative: cache hits we couldn't predict don't refund.
        if cap is not None:
            try:
                est = estimate_cost_usd(
                    model, [full_prompt],
                    system=system, max_tokens=max_tokens,
                )
                next_estimate = float(est["cost_usd"])
            except LLMRouterError as exc:
                # Estimator failed (unknown model, etc). Treat as a
                # hard error rather than silently bypassing the gate
                # - a money-leak audit must surface here, not later.
                logger.warning(
                    "[!] llm pipe budget estimator failed at row %d: %s",
                    i + 1, exc,
                )
                raise LLMPipeError(
                    f"Budget gate estimator failed: {exc}"
                ) from exc
            if cumulative_cost + next_estimate > cap:
                logger.info(
                    "[i] llm pipe: budget cap $%.6f reached at row %d/%d "
                    "(cumulative=$%.6f, next_est=$%.6f). Stopping.",
                    cap, i + 1, n, cumulative_cost, next_estimate,
                )
                sentinel = _budget_sentinel_row(
                    model=model, rows_processed=rows_processed,
                    rows_total=n, cumulative_cost=cumulative_cost,
                    next_estimate=next_estimate, cap=cap,
                )
                break

        try:
            response = call_llm(
                model,
                prompt=full_prompt,
                system=system,
                use_cache=use_cache,
                max_tokens=max_tokens,
                source="llm_pipe",
            )
            outputs.append(response.text)
            models.append(response.model_id)
            providers.append(response.provider)
            costs.append(float(response.cost_usd))
            latencies.append(int(response.latency_ms))
            statuses.append("success")
            errors.append("")
            cumulative_cost += float(response.cost_usd)
            rows_processed += 1
        except LLMRouterError as exc:
            outputs.append("")
            models.append(model)
            providers.append(getattr(exc, "provider", "") or "")
            costs.append(0.0)
            latencies.append(0)
            statuses.append("error")
            errors.append(f"{exc.error_class}: {exc}"[:1000])
            rows_processed += 1
            logger.warning(
                "[!] llm pipe row %d/%d failed: %s - %s",
                i + 1, n, exc.error_class, exc,
            )

    # Assemble the output. When the budget gate fires, only the
    # processed rows (slice 0..rows_processed) appear in the input
    # half, plus a sentinel row appended at the end.
    if sentinel is not None:
        head = df.iloc[:rows_processed].copy()
        head["_llm_output"] = outputs
        head["_llm_model"] = models
        head["_llm_provider"] = providers
        head["_llm_cost_usd"] = costs
        head["_llm_latency_ms"] = latencies
        head["_llm_status"] = statuses
        head["_llm_error"] = errors
        # Sentinel row: input columns NaN, _llm_* columns set per
        # ``_budget_sentinel_row``. We build it as a 1-row DataFrame
        # with the same columns as ``head`` and concat.
        sent_row: dict = {c: pd.NA for c in df.columns}
        sent_row.update(sentinel)
        sent_df = pd.DataFrame([sent_row], columns=list(head.columns))
        out = pd.concat([head, sent_df], ignore_index=True)
    else:
        out = df.copy()
        out["_llm_output"] = outputs
        out["_llm_model"] = models
        out["_llm_provider"] = providers
        out["_llm_cost_usd"] = costs
        out["_llm_latency_ms"] = latencies
        out["_llm_status"] = statuses
        out["_llm_error"] = errors

    # Aggregate telemetry - useful in `docker logs` for cost auditing
    # without forcing the operator to query the history store.
    n_success = sum(1 for s in statuses if s == "success")
    n_errors = sum(1 for s in statuses if s == "error")
    total_cost = float(sum(costs))
    logger.info(
        "[i] llm pipe: model=%s rows=%d processed=%d success=%d errors=%d "
        "cost_usd=%.6f cap=%s",
        model, n, rows_processed, n_success, n_errors, total_cost,
        f"${cap:.6f}" if cap is not None else "none",
    )
    return out


# ── Batch (whole-DataFrame as one prompt) ────────────────────────────

def _serialise_rows_for_batch(
    df: pd.DataFrame, columns: Sequence[str],
) -> str:
    """Serialise selected columns of ``df`` to a JSON array of objects.

    Format: ``[{col1: val1, col2: val2}, ...]`` indented for model
    readability. ``None`` / NaN cells become JSON ``null``. Non-string
    values (numbers, bools, timestamps) are coerced to strings via
    ``default=str`` so the model sees one stable format regardless of
    the underlying pandas dtype.
    """
    pieces: list[dict] = []
    for i in range(len(df)):
        row = df.iloc[i]
        record: dict = {}
        for c in columns:
            v = row.get(c)
            if v is None or (isinstance(v, float) and np.isnan(v)):
                record[c] = None
            else:
                record[c] = v
        pieces.append(record)
    return json.dumps(pieces, default=str, ensure_ascii=False, indent=2)


def build_batch_prompt(
    user_prompt: str, df: pd.DataFrame, columns: Sequence[str],
) -> str:
    """Public for tests + future composition. Wraps the JSON-serialised
    DataFrame in ``<data>...</data>`` boundary tags per the
    prompt-injection-mitigation pattern.
    """
    json_block = _serialise_rows_for_batch(df, columns)
    return f"{user_prompt}\n\n<data>\n{json_block}\n</data>"


def _empty_batch_result(
    *, model: str, status: str, error: str = "",
) -> pd.DataFrame:
    """Single-row well-shaped empty/error result. Output schema matches
    the success path's so downstream pipes compose without a special
    case.
    """
    return pd.DataFrame({
        "_llm_output": [""],
        "_llm_model": [model],
        "_llm_provider": [""],
        "_llm_cost_usd": [0.0],
        "_llm_latency_ms": [0],
        "_llm_status": [status],
        "_llm_error": [error],
        "_llm_input_row_count": [0],
    })


def llm_batch_pipe(
    df: pd.DataFrame,
    *,
    model: str,
    prompt: str,
    system: Optional[str] = None,
    field: Optional[str] = None,
    use_cache: bool = True,
    max_tokens: Optional[int] = None,
    max_rows: int = _DEFAULT_BATCH_MAX_ROWS,
    max_cost_usd: Optional[float] = None,
    dry_run: bool = False,
) -> pd.DataFrame:
    """Apply an LLM to the WHOLE DataFrame as one prompt.

    Differs from :func:`llm_pipe` (per-row) in that the entire input
    DataFrame is JSON-serialised and sent as ONE prompt to the model.
    Returns a **single-row** DataFrame containing the model's
    holistic response - the original rows are gone (use
    ``| append [llm_batch ...]`` if you need both).

    Parameters
    ----------
    df :
        Input DataFrame.
    model :
        Registry id (e.g. ``"claude-sonnet-4-6"``). Looked up in
        :mod:`model_store`.
    prompt :
        Operator instructions. The serialised DataFrame is appended
        below in a ``<data>...</data>`` block.
    system :
        Optional system prompt threaded through to the provider.
    field :
        If supplied, embed only this single column. Default:
        concatenate all auto-detected text columns.
    use_cache :
        When True (default), reuse cached responses keyed by
        ``content_hash`` (slice 3). Cache hits cost ``$0`` and report
        ``_llm_latency_ms=0``.
    max_tokens :
        Override the per-record output cap from the registry.
    max_rows :
        Cap on rows fed into the prompt. Default ``20`` (matches the
        existing ``claude_analyzer_max_input_rows``). Long-context
        models can override.
    max_cost_usd :
        **Slice 7 budget gate.** Hard ceiling on the cost of THIS
        single call (USD). The conservative pre-call estimator runs
        first; if its estimate exceeds the cap, no provider call is
        made and a single-row ``budget_exceeded`` result is returned
        instead. ``None`` / ``0`` means no cap. Note: unlike the
        per-row ``llm_pipe``, batch mode is one call total - the cap
        applies to that one call, not to a cumulative sum.
    dry_run :
        **Slice 7 cost preview.** When True, returns a 1-row preview
        with the estimated cost - NO provider call, NO cache lookup,
        NO history capture. Identical contract to ``llm_pipe``'s
        dry-run mode but the prompt is the packed-DataFrame form.

    Returns
    -------
    DataFrame
        ALWAYS exactly one row, with columns:
        ``_llm_output``, ``_llm_model``, ``_llm_provider``,
        ``_llm_cost_usd``, ``_llm_latency_ms``, ``_llm_status``,
        ``_llm_error``, ``_llm_input_row_count``. Dry-run mode
        additionally adds ``_dry_run``, ``_estimated_cost_usd``,
        ``_estimated_input_tokens``, ``_estimated_output_tokens``,
        ``_row_count``, ``_max_tokens``.

    Raises
    ------
    LLMPipeError
        On bad inputs (missing prompt, missing field, no text columns,
        invalid ``max_rows`` / ``max_cost_usd``).
    """
    if not isinstance(model, str) or not model.strip():
        raise LLMPipeError("llm_batch requires a non-empty model id.")
    if not isinstance(prompt, str) or not prompt:
        raise LLMPipeError("llm_batch requires a non-empty prompt string.")
    if not isinstance(max_rows, int) or max_rows <= 0:
        raise LLMPipeError(
            f"llm_batch max_rows must be a positive int, got {max_rows!r}"
        )

    cap = _resolve_budget_cap(max_cost_usd)

    # Empty input → return single-row "skipped_empty" result. Downstream
    # pipes still see a well-shaped DataFrame. Dry-run on empty input
    # returns the dry-run preview (cost=0, row_count=0) for symmetry
    # with llm_pipe.
    if df is None or len(df) == 0:
        if dry_run:
            return _dry_run_preview(
                model=model, prompts=[],
                system=system, max_tokens=max_tokens,
                pipe_label="llm_batch",
            )
        return _empty_batch_result(model=model, status="skipped_empty")

    cols = _resolve_columns(df, field)
    truncated = df.iloc[:max_rows]
    full_prompt = build_batch_prompt(prompt, truncated, cols)

    # ── Dry-run path ──────────────────────────────────────────────
    # Build the packed prompt and hand it to the estimator (one call,
    # so prompts=[full_prompt]). Zero provider calls.
    if dry_run:
        out = _dry_run_preview(
            model=model, prompts=[full_prompt],
            system=system, max_tokens=max_tokens,
            pipe_label="llm_batch",
        )
        # Carry the truncated row count through so the operator sees
        # what WOULD have been sent.
        out["_llm_input_row_count"] = [len(truncated)]
        return out

    from analyzers.llm_router import (
        call_llm, estimate_cost_usd, LLMRouterError,
    )

    # ── Slice 7 pre-call budget check ─────────────────────────────
    # Single-call mode: if estimator says we'd exceed the cap, skip
    # the dispatch entirely and return a budget_exceeded sentinel
    # row. No partial credit - batch is all-or-nothing by design.
    if cap is not None:
        try:
            est = estimate_cost_usd(
                model, [full_prompt],
                system=system, max_tokens=max_tokens,
            )
            est_cost = float(est["cost_usd"])
        except LLMRouterError as exc:
            raise LLMPipeError(
                f"Budget gate estimator failed: {exc}"
            ) from exc
        if est_cost > cap:
            logger.info(
                "[i] llm_batch: budget cap $%.6f exceeded by estimate "
                "$%.6f. Skipping dispatch.",
                cap, est_cost,
            )
            return pd.DataFrame({
                "_llm_output": [""],
                "_llm_model": [model],
                "_llm_provider": [est.get("provider", "") or ""],
                "_llm_cost_usd": [0.0],
                "_llm_latency_ms": [0],
                "_llm_status": [_BUDGET_EXCEEDED_STATUS],
                "_llm_error": [
                    f"Estimated cost ${est_cost:.6f} exceeds cap "
                    f"${cap:.6f}. Lower max_tokens, max_rows, or "
                    f"raise max_cost_usd."
                ],
                "_llm_input_row_count": [len(truncated)],
            })

    try:
        response = call_llm(
            model,
            prompt=full_prompt,
            system=system,
            use_cache=use_cache,
            max_tokens=max_tokens,
            source="llm_batch_pipe",
        )
    except LLMRouterError as exc:
        logger.warning(
            "[!] llm_batch failed: %s - %s", exc.error_class, exc,
        )
        return pd.DataFrame({
            "_llm_output": [""],
            "_llm_model": [model],
            "_llm_provider": [getattr(exc, "provider", "") or ""],
            "_llm_cost_usd": [0.0],
            "_llm_latency_ms": [0],
            "_llm_status": ["error"],
            "_llm_error": [f"{exc.error_class}: {exc}"[:1000]],
            "_llm_input_row_count": [len(truncated)],
        })

    logger.info(
        "[i] llm_batch: model=%s rows=%d (truncated from %d) "
        "cost_usd=%.6f latency_ms=%d cap=%s",
        model, len(truncated), len(df),
        response.cost_usd, response.latency_ms,
        f"${cap:.6f}" if cap is not None else "none",
    )
    return pd.DataFrame({
        "_llm_output": [response.text],
        "_llm_model": [response.model_id],
        "_llm_provider": [response.provider],
        "_llm_cost_usd": [float(response.cost_usd)],
        "_llm_latency_ms": [int(response.latency_ms)],
        "_llm_status": ["success"],
        "_llm_error": [""],
        "_llm_input_row_count": [len(truncated)],
    })


# ── Slice-9 (Phase 4 / Bet 3 slice 1): | llm_route - cost cascade ───
#
# 2-stage confidence-based escalation in a single pipe. Cheap model
# runs on every row; rows whose stage-1 output parses below the
# confidence threshold (or doesn't parse to a number, or errored)
# escalate to the expensive model. End-to-end cost approaches the
# cheap stage's per-row cost while fidelity stays close to the
# expensive stage's.
#
# Per-row output columns (same shape as | llm - composes with
# downstream | where _llm_status="success" / | switch / etc.):
#   _llm_output, _llm_model, _llm_provider, _llm_cost_usd,
#   _llm_latency_ms, _llm_status, _llm_error
# Plus three slice-9-specific columns:
#   _llm_route_escalated     bool - did this row escalate?
#   _llm_route_stage_1_output str - cheap model's output (preserved
#                                    for audit, even when escalated)
#   _llm_route_confidence    float - parsed confidence (NaN when
#                                    output didn't parse to a number)
#
# Slice-7 contract honoured: max_cost_usd= + dry_run= + money-leak
# canary test. Budget cap is checked PER STAGE - the cheap stage gets
# its own per-row pre-call estimate; the escalation stage gets its
# own per-row pre-call estimate (which is much higher per row, so the
# cap stops escalation first). Both stages contribute to cumulative
# cost; the sentinel marks WHICH stage hit the cap.


def _parse_confidence(text: Optional[str]) -> float:
    """Parse a confidence score from an LLM output string.

    Strategies, tried in order:
      1. The whole stripped text parses as float (model output =
         "0.85"). Fast path; covers prompt-engineered "output ONLY a
         number".
      2. JSON object containing a ``confidence`` key (e.g.
         ``{"confidence": 0.85, ...}``).
      3. First number in the text matched by regex (covers "I'm 85%
         confident" → 0.85, or "confidence: 0.7").

    Returns NaN if no number is recoverable. NaN triggers escalation
    (the threshold comparison against NaN is False).
    """
    import re as _re
    if text is None:
        return float("nan")
    s = str(text).strip()
    if not s:
        return float("nan")
    # Strategy 1: whole-string float
    try:
        return float(s)
    except ValueError:
        pass
    # Strategy 2: JSON
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and "confidence" in obj:
            v = obj["confidence"]
            if isinstance(v, (int, float)):
                return float(v)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    # Strategy 3: first number in text. Match optional %; convert to
    # decimal if percentage-shaped.
    m = _re.search(r"(\d+(?:\.\d+)?)\s*(%?)", s)
    if m:
        try:
            v = float(m.group(1))
            if m.group(2) == "%":
                return v / 100.0
            return v
        except ValueError:
            pass
    return float("nan")


def _llm_route_empty_result(
    df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """Well-shaped empty DataFrame for the empty-input case."""
    out = df.copy() if df is not None else pd.DataFrame()
    out["_llm_output"] = pd.array([], dtype="object")
    out["_llm_model"] = pd.array([], dtype="object")
    out["_llm_provider"] = pd.array([], dtype="object")
    out["_llm_cost_usd"] = pd.array([], dtype="float64")
    out["_llm_latency_ms"] = pd.array([], dtype="int64")
    out["_llm_status"] = pd.array([], dtype="object")
    out["_llm_error"] = pd.array([], dtype="object")
    out["_llm_route_escalated"] = pd.array([], dtype="bool")
    out["_llm_route_stage_1_output"] = pd.array([], dtype="object")
    out["_llm_route_confidence"] = pd.array([], dtype="float64")
    return out


def llm_route_pipe(
    df: pd.DataFrame,
    *,
    model: str,
    prompt: str,
    escalate_to: str,
    escalate_prompt: Optional[str] = None,
    confidence_threshold: float = 0.5,
    system: Optional[str] = None,
    field: Optional[str] = None,
    use_cache: bool = True,
    max_tokens: Optional[int] = None,
    max_cost_usd: Optional[float] = None,
    dry_run: bool = False,
) -> pd.DataFrame:
    """2-stage confidence-based cost cascade.

    Stage 1: the ``model`` (cheap) runs on every row. Its output is
    parsed for a confidence score (whole-string float → JSON
    ``confidence`` key → first number in text - see
    :func:`_parse_confidence`).

    Stage 2: rows whose stage-1 confidence is BELOW
    ``confidence_threshold`` (or NaN, or whose stage-1 errored)
    re-run with ``escalate_to``. ``escalate_prompt`` overrides the
    stage-1 prompt for the escalation call (defaults to the same
    prompt).

    Output preserves the input row order. The standard ``_llm_*``
    columns carry the FINAL output (whichever stage produced it);
    three new columns surface the cascade decision:

    * ``_llm_route_escalated`` - bool, True iff this row went through stage 2
    * ``_llm_route_stage_1_output`` - str, cheap model's output (always populated,
      even when escalated, for audit)
    * ``_llm_route_confidence`` - float, parsed confidence (NaN when the cheap
      output wasn't parseable)

    Slice-7 contract: ``max_cost_usd=N`` (per slice-7 budget gate;
    cumulative cost across BOTH stages) + ``dry_run=true`` (returns a
    1-row preview for the worst-case "every row escalates" scenario -
    upper bound on cost, conservative-by-design).
    """
    if not isinstance(model, str) or not model.strip():
        raise LLMPipeError("llm_route requires a non-empty model id.")
    if not isinstance(prompt, str) or not prompt:
        raise LLMPipeError("llm_route requires a non-empty prompt string.")
    if not isinstance(escalate_to, str) or not escalate_to.strip():
        raise LLMPipeError(
            "llm_route requires a non-empty escalate_to model id."
        )
    if not isinstance(confidence_threshold, (int, float)) or \
       isinstance(confidence_threshold, bool):
        raise LLMPipeError(
            "llm_route confidence_threshold must be a number."
        )

    cap = _resolve_budget_cap(max_cost_usd)
    effective_escalate_prompt = escalate_prompt or prompt

    if df is None or len(df) == 0:
        if dry_run:
            return _dry_run_preview(
                model=escalate_to, prompts=[],
                system=system, max_tokens=max_tokens,
                pipe_label="llm_route",
            )
        return _llm_route_empty_result(df)

    cols = _resolve_columns(df, field)

    # ── Dry-run path ──────────────────────────────────────────────────
    # Conservative-by-design: assume every row escalates (cheap stage
    # cost + escalation stage cost). This is the WORST CASE; actual
    # cost will be lower because some rows won't escalate. Operators
    # who want a "best case" (no escalations) can dry-run | llm
    # directly with the cheap model.
    if dry_run:
        from analyzers.llm_router import estimate_cost_usd, LLMRouterError
        cheap_prompts = [
            build_full_prompt(prompt, df.iloc[i], cols)
            for i in range(len(df))
        ]
        escalate_prompts = [
            build_full_prompt(effective_escalate_prompt, df.iloc[i], cols)
            for i in range(len(df))
        ]
        try:
            cheap_est = estimate_cost_usd(
                model, cheap_prompts, system=system, max_tokens=max_tokens,
            )
            escalate_est = estimate_cost_usd(
                escalate_to, escalate_prompts,
                system=system, max_tokens=max_tokens,
            )
        except LLMRouterError as exc:
            logger.warning(
                "[!] llm_route dry_run estimator failed: %s - %s",
                exc.error_class, exc,
            )
            return pd.DataFrame({
                "_dry_run": [True],
                "_estimated_cost_usd": [0.0],
                "_estimated_input_tokens": [0],
                "_estimated_output_tokens": [0],
                "_row_count": [len(df)],
                "_llm_model": [f"{model} → {escalate_to}"],
                "_llm_provider": [getattr(exc, "provider", "") or ""],
                "_max_tokens": [int(max_tokens or 0)],
                "_llm_output": [""],
                "_llm_cost_usd": [0.0],
                "_llm_latency_ms": [0],
                "_llm_status": ["error"],
                "_llm_error": [f"{exc.error_class}: {exc}"[:1000]],
            })
        worst_case = float(cheap_est["cost_usd"]) + float(escalate_est["cost_usd"])
        logger.info(
            "[i] llm_route dry_run: cheap=%s escalate=%s rows=%d "
            "worst_case_cost_usd=%.6f (NO provider call made)",
            model, escalate_to, len(df), worst_case,
        )
        return pd.DataFrame({
            "_dry_run": [True],
            "_estimated_cost_usd": [worst_case],
            "_estimated_input_tokens": [
                int(cheap_est["input_tokens"]) + int(escalate_est["input_tokens"])
            ],
            "_estimated_output_tokens": [
                int(cheap_est["output_tokens"]) + int(escalate_est["output_tokens"])
            ],
            "_row_count": [len(df)],
            "_llm_model": [f"{model} → {escalate_to}"],
            "_llm_provider": [
                f"{cheap_est['provider']} → {escalate_est['provider']}"
            ],
            "_max_tokens": [int(max_tokens or 0)],
            "_llm_output": [""],
            "_llm_cost_usd": [0.0],
            "_llm_latency_ms": [0],
            "_llm_status": [_DRY_RUN_STATUS],
            "_llm_error": [""],
        })

    # ── Live execution: 2-stage cascade ────────────────────────────────
    from analyzers.llm_router import (
        call_llm, estimate_cost_usd, LLMRouterError,
    )

    n = len(df)
    # Per-row output buckets - populated by stage 1, possibly
    # overridden by stage 2.
    outputs: list[str] = [""] * n
    models: list[str] = [""] * n
    providers: list[str] = [""] * n
    costs: list[float] = [0.0] * n
    latencies: list[int] = [0] * n
    statuses: list[str] = [""] * n
    errors: list[str] = [""] * n
    escalated: list[bool] = [False] * n
    stage_1_outputs: list[str] = [""] * n
    confidences: list[float] = [float("nan")] * n

    cumulative_cost = 0.0
    sentinel: Optional[dict] = None
    stage_1_processed = 0
    stage_2_processed = 0

    # ── Stage 1: cheap model on every row ─────────────────────────────
    for i in range(n):
        row = df.iloc[i]
        full_prompt = build_full_prompt(prompt, row, cols)

        if cap is not None:
            try:
                est = estimate_cost_usd(
                    model, [full_prompt],
                    system=system, max_tokens=max_tokens,
                )
                next_estimate = float(est["cost_usd"])
            except LLMRouterError as exc:
                logger.warning(
                    "[!] llm_route stage-1 estimator failed at row %d: %s",
                    i + 1, exc,
                )
                raise LLMPipeError(
                    f"Budget gate estimator failed at stage 1: {exc}"
                ) from exc
            if cumulative_cost + next_estimate > cap:
                logger.info(
                    "[i] llm_route: budget cap $%.6f reached during stage 1 "
                    "at row %d/%d (cumulative=$%.6f, next_est=$%.6f). "
                    "Stopping.",
                    cap, i + 1, n, cumulative_cost, next_estimate,
                )
                sentinel = _budget_sentinel_row(
                    model=model, rows_processed=stage_1_processed,
                    rows_total=n, cumulative_cost=cumulative_cost,
                    next_estimate=next_estimate, cap=cap,
                )
                break

        try:
            response = call_llm(
                model,
                prompt=full_prompt,
                system=system,
                use_cache=use_cache,
                max_tokens=max_tokens,
                source="llm_route_stage_1",
            )
            outputs[i] = response.text
            models[i] = response.model_id
            providers[i] = response.provider
            costs[i] = float(response.cost_usd)
            latencies[i] = int(response.latency_ms)
            statuses[i] = "success"
            errors[i] = ""
            stage_1_outputs[i] = response.text
            confidences[i] = _parse_confidence(response.text)
            cumulative_cost += float(response.cost_usd)
        except LLMRouterError as exc:
            outputs[i] = ""
            models[i] = model
            providers[i] = getattr(exc, "provider", "") or ""
            costs[i] = 0.0
            latencies[i] = 0
            statuses[i] = "error"
            errors[i] = f"{exc.error_class}: {exc}"[:1000]
            stage_1_outputs[i] = ""
            confidences[i] = float("nan")
            logger.warning(
                "[!] llm_route stage-1 row %d/%d failed: %s - %s",
                i + 1, n, exc.error_class, exc,
            )
        stage_1_processed += 1

    # ── Stage 2: escalation for low-confidence / errored stage-1 rows ─
    # Skip stage 2 entirely if stage 1 was cut short by the budget cap
    # - operator already saw the cap signal, escalating now would
    # blow further past the ceiling.
    if sentinel is None:
        for i in range(stage_1_processed):
            # Decide whether to escalate. Conditions:
            #   * stage-1 errored → escalate
            #   * stage-1 confidence parse failed (NaN) → escalate
            #   * stage-1 confidence < threshold → escalate
            should_escalate = (
                statuses[i] == "error"
                or np.isnan(confidences[i])
                or confidences[i] < confidence_threshold
            )
            if not should_escalate:
                continue

            row = df.iloc[i]
            full_prompt = build_full_prompt(
                effective_escalate_prompt, row, cols,
            )

            if cap is not None:
                try:
                    est = estimate_cost_usd(
                        escalate_to, [full_prompt],
                        system=system, max_tokens=max_tokens,
                    )
                    next_estimate = float(est["cost_usd"])
                except LLMRouterError as exc:
                    logger.warning(
                        "[!] llm_route stage-2 estimator failed at "
                        "row %d: %s", i + 1, exc,
                    )
                    raise LLMPipeError(
                        f"Budget gate estimator failed at stage 2: {exc}"
                    ) from exc
                if cumulative_cost + next_estimate > cap:
                    logger.info(
                        "[i] llm_route: budget cap $%.6f reached during "
                        "stage 2 at row %d (cumulative=$%.6f, "
                        "next_est=$%.6f). Stopping escalation.",
                        cap, i + 1, cumulative_cost, next_estimate,
                    )
                    sentinel = _budget_sentinel_row(
                        model=escalate_to,
                        rows_processed=stage_1_processed + stage_2_processed,
                        rows_total=n + stage_1_processed,  # cap-tracking total
                        cumulative_cost=cumulative_cost,
                        next_estimate=next_estimate, cap=cap,
                    )
                    break

            try:
                response = call_llm(
                    escalate_to,
                    prompt=full_prompt,
                    system=system,
                    use_cache=use_cache,
                    max_tokens=max_tokens,
                    source="llm_route_stage_2",
                )
                # Override stage-1 result; keep stage-1 output preserved.
                outputs[i] = response.text
                models[i] = response.model_id
                providers[i] = response.provider
                costs[i] = float(response.cost_usd)
                latencies[i] = int(response.latency_ms)
                statuses[i] = "success"
                errors[i] = ""
                escalated[i] = True
                cumulative_cost += float(response.cost_usd)
            except LLMRouterError as exc:
                # Escalation failed too. Keep stage-1 status if it was
                # success; otherwise mark the cumulative state.
                if statuses[i] == "success":
                    # Stage 1 was OK but its confidence was low; stage 2
                    # failed. Keep the stage-1 output but flag the
                    # escalation attempt's failure in the error column.
                    errors[i] = (
                        f"stage_2_escalation_failed: "
                        f"{exc.error_class}: {exc}"[:1000]
                    )
                else:
                    errors[i] = (
                        f"both_stages_failed: "
                        f"{statuses[i]}/{exc.error_class}: {exc}"[:1000]
                    )
                escalated[i] = True
                logger.warning(
                    "[!] llm_route stage-2 row %d failed: %s - %s",
                    i + 1, exc.error_class, exc,
                )
            stage_2_processed += 1

    # Build the result DataFrame. Truncate the per-row arrays to
    # stage_1_processed if the cap stopped stage 1; otherwise it's
    # full length.
    valid_rows = stage_1_processed
    out = df.iloc[:valid_rows].copy() if valid_rows > 0 else df.iloc[:0].copy()
    out["_llm_output"] = outputs[:valid_rows]
    out["_llm_model"] = models[:valid_rows]
    out["_llm_provider"] = providers[:valid_rows]
    out["_llm_cost_usd"] = costs[:valid_rows]
    out["_llm_latency_ms"] = latencies[:valid_rows]
    out["_llm_status"] = statuses[:valid_rows]
    out["_llm_error"] = errors[:valid_rows]
    out["_llm_route_escalated"] = escalated[:valid_rows]
    out["_llm_route_stage_1_output"] = stage_1_outputs[:valid_rows]
    out["_llm_route_confidence"] = confidences[:valid_rows]

    if sentinel is not None:
        # Append a sentinel row marking the budget boundary. Pad the
        # slice-9-specific columns with neutral values.
        sentinel_row = {**sentinel}
        sentinel_row["_llm_route_escalated"] = False
        sentinel_row["_llm_route_stage_1_output"] = ""
        sentinel_row["_llm_route_confidence"] = float("nan")
        # Pad the input columns with NaN
        for col in df.columns:
            if col not in sentinel_row:
                sentinel_row[col] = None
        out = pd.concat([out, pd.DataFrame([sentinel_row])], ignore_index=True)

    n_escalated = sum(escalated[:valid_rows])
    logger.info(
        "[i] llm_route: cheap=%s escalate=%s rows=%d "
        "stage1_processed=%d stage2_escalated=%d "
        "cumulative_cost_usd=%.6f cap=%s",
        model, escalate_to, n,
        stage_1_processed, n_escalated, cumulative_cost,
        f"${cap:.6f}" if cap is not None else "none",
    )
    return out


# ── Slice-2 (Phase 4 / Bet 3 slice 2): | llm_refine - drafter/critic loop ──
#
# Each row goes through up to ``max_rounds`` drafter→critic cycles.
# Round 1: drafter produces an initial draft against the row data.
# Round k>=2: critic from round k-1 evaluates the round-(k-1) draft;
#   drafter incorporates the critique into a new draft.
# Convergence: if the critic's output contains
# ``converge_when_critic_says`` (substring, case-insensitive), the
# loop exits early - saves cost when the model says "good enough".
#
# Per-row output columns (additive to the standard | llm columns):
#   _llm_refine_rounds       int - how many drafter rounds ran
#   _llm_refine_drafts       str (JSON array) - every draft for audit
#   _llm_refine_critiques    str (JSON array) - every critique for audit
#   _llm_refine_converged    bool - True iff converge_when_critic_says triggered

# Default revise-prompt template. ``{drafter_prompt}`` is the original
# operator instruction; ``{prev_draft}`` and ``{critique}`` are
# round-(k-1)'s draft + critic output. Operators can override via
# ``revise_prompt=`` if they want a different template (e.g. one that
# scores the draft, asks the model to identify what's missing, etc.).
_DEFAULT_REVISE_TEMPLATE = (
    "{drafter_prompt}\n\n"
    "<previous_draft>\n{prev_draft}\n</previous_draft>\n\n"
    "<critique>\n{critique}\n</critique>\n\n"
    "Incorporate the critique into a revised draft."
)

# Critic prompt assembly. ``{critic_prompt}`` is the operator's
# instruction; ``{draft}`` is the current draft being evaluated.
_CRITIC_TEMPLATE = (
    "{critic_prompt}\n\n"
    "<draft>\n{draft}\n</draft>"
)


def _check_converged(critique: str, signal: Optional[str]) -> bool:
    """Substring-search the critique for the convergence signal.
    Case-insensitive. Empty signal → never converge (loop runs full
    max_rounds).
    """
    if not signal or not critique:
        return False
    return signal.lower() in critique.lower()


def _llm_refine_empty_result(
    df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """Well-shaped empty DataFrame for the empty-input case."""
    out = df.copy() if df is not None else pd.DataFrame()
    out["_llm_output"] = pd.array([], dtype="object")
    out["_llm_model"] = pd.array([], dtype="object")
    out["_llm_provider"] = pd.array([], dtype="object")
    out["_llm_cost_usd"] = pd.array([], dtype="float64")
    out["_llm_latency_ms"] = pd.array([], dtype="int64")
    out["_llm_status"] = pd.array([], dtype="object")
    out["_llm_error"] = pd.array([], dtype="object")
    out["_llm_refine_rounds"] = pd.array([], dtype="int64")
    out["_llm_refine_drafts"] = pd.array([], dtype="object")
    out["_llm_refine_critiques"] = pd.array([], dtype="object")
    out["_llm_refine_converged"] = pd.array([], dtype="bool")
    return out


def llm_refine_pipe(
    df: pd.DataFrame,
    *,
    drafter_model: str,
    critic_model: str,
    drafter_prompt: str,
    critic_prompt: str,
    revise_prompt: Optional[str] = None,
    max_rounds: int = 3,
    converge_when_critic_says: Optional[str] = None,
    system: Optional[str] = None,
    field: Optional[str] = None,
    use_cache: bool = True,
    max_tokens: Optional[int] = None,
    max_cost_usd: Optional[float] = None,
    dry_run: bool = False,
) -> pd.DataFrame:
    """Drafter/critic refinement loop.

    Each row goes through:
      Round 1: drafter call (prompt = ``drafter_prompt`` + row data)
               → critic call (prompt = ``critic_prompt`` + draft)
      Round k≥2 (if not converged): drafter call (prompt = revise_template
               with prev draft + critique) → critic call

    The loop exits early when the critic's output contains
    ``converge_when_critic_says`` (substring, case-insensitive).
    Otherwise it runs full ``max_rounds``.

    Final row output is the LATEST draft. The complete drafts and
    critiques arrays are JSON-serialised in audit columns so an
    operator (or AI agent) can inspect the full refinement trajectory.

    Slice-7 contract honoured: per-call estimator + cumulative cost
    cap; dry-run returns worst-case estimate (every row runs full
    max_rounds with no convergence). Money-leak canary pinned in
    tests/test_llm_refine_pipe.py::TestMoneyLeakCanary.
    """
    if not isinstance(drafter_model, str) or not drafter_model.strip():
        raise LLMPipeError("llm_refine requires a non-empty drafter_model id.")
    if not isinstance(critic_model, str) or not critic_model.strip():
        raise LLMPipeError("llm_refine requires a non-empty critic_model id.")
    if not isinstance(drafter_prompt, str) or not drafter_prompt:
        raise LLMPipeError("llm_refine requires a non-empty drafter_prompt.")
    if not isinstance(critic_prompt, str) or not critic_prompt:
        raise LLMPipeError("llm_refine requires a non-empty critic_prompt.")
    if not isinstance(max_rounds, int) or isinstance(max_rounds, bool):
        raise LLMPipeError(
            f"llm_refine max_rounds must be an integer, got "
            f"{type(max_rounds).__name__}."
        )
    if max_rounds < 1:
        raise LLMPipeError(
            f"llm_refine max_rounds must be ≥ 1, got {max_rounds}."
        )

    cap = _resolve_budget_cap(max_cost_usd)
    revise_template = revise_prompt or _DEFAULT_REVISE_TEMPLATE

    if df is None or len(df) == 0:
        if dry_run:
            return _dry_run_preview(
                model=drafter_model, prompts=[],
                system=system, max_tokens=max_tokens,
                pipe_label="llm_refine",
            )
        return _llm_refine_empty_result(df)

    cols = _resolve_columns(df, field)

    # ── Dry-run path - worst-case (every row runs full max_rounds) ────
    # Per row: 1 initial drafter + max_rounds critic + (max_rounds-1)
    # revise drafter. Conservative: assumes no convergence at any round.
    if dry_run:
        from analyzers.llm_router import estimate_cost_usd, LLMRouterError
        # Build representative prompts. The revise stages need a
        # placeholder for prev_draft + critique; use a stub to size them.
        drafter_prompts: list[str] = []
        critic_prompts: list[str] = []
        for i in range(len(df)):
            row = df.iloc[i]
            # Initial drafter prompt
            drafter_prompts.append(
                build_full_prompt(drafter_prompt, row, cols)
            )
            # max_rounds critic prompts
            for _ in range(max_rounds):
                critic_prompts.append(
                    build_full_prompt(
                        _CRITIC_TEMPLATE.format(
                            critic_prompt=critic_prompt,
                            draft="(stub-draft for cost estimate)",
                        ),
                        row, cols,
                    )
                )
            # max_rounds-1 revise drafter prompts
            for _ in range(max_rounds - 1):
                drafter_prompts.append(
                    build_full_prompt(
                        revise_template.format(
                            drafter_prompt=drafter_prompt,
                            prev_draft="(stub-prev-draft)",
                            critique="(stub-critique)",
                        ),
                        row, cols,
                    )
                )
        try:
            drafter_est = estimate_cost_usd(
                drafter_model, drafter_prompts,
                system=system, max_tokens=max_tokens,
            )
            critic_est = estimate_cost_usd(
                critic_model, critic_prompts,
                system=system, max_tokens=max_tokens,
            )
        except LLMRouterError as exc:
            logger.warning(
                "[!] llm_refine dry_run estimator failed: %s - %s",
                exc.error_class, exc,
            )
            return pd.DataFrame({
                "_dry_run": [True],
                "_estimated_cost_usd": [0.0],
                "_estimated_input_tokens": [0],
                "_estimated_output_tokens": [0],
                "_row_count": [len(df)],
                "_llm_model": [f"{drafter_model} ⇄ {critic_model}"],
                "_llm_provider": [getattr(exc, "provider", "") or ""],
                "_max_tokens": [int(max_tokens or 0)],
                "_llm_output": [""],
                "_llm_cost_usd": [0.0],
                "_llm_latency_ms": [0],
                "_llm_status": ["error"],
                "_llm_error": [f"{exc.error_class}: {exc}"[:1000]],
            })
        worst_case = float(drafter_est["cost_usd"]) + float(critic_est["cost_usd"])
        logger.info(
            "[i] llm_refine dry_run: drafter=%s critic=%s rows=%d "
            "max_rounds=%d worst_case_cost_usd=%.6f (NO provider call made)",
            drafter_model, critic_model, len(df), max_rounds, worst_case,
        )
        return pd.DataFrame({
            "_dry_run": [True],
            "_estimated_cost_usd": [worst_case],
            "_estimated_input_tokens": [
                int(drafter_est["input_tokens"]) + int(critic_est["input_tokens"])
            ],
            "_estimated_output_tokens": [
                int(drafter_est["output_tokens"]) + int(critic_est["output_tokens"])
            ],
            "_row_count": [len(df)],
            "_llm_model": [f"{drafter_model} ⇄ {critic_model}"],
            "_llm_provider": [
                f"{drafter_est['provider']} ⇄ {critic_est['provider']}"
            ],
            "_max_tokens": [int(max_tokens or 0)],
            "_llm_output": [""],
            "_llm_cost_usd": [0.0],
            "_llm_latency_ms": [0],
            "_llm_status": [_DRY_RUN_STATUS],
            "_llm_error": [""],
        })

    # ── Live execution: per-row drafter/critic loop ────────────────────
    from analyzers.llm_router import (
        call_llm, estimate_cost_usd, LLMRouterError,
    )

    n = len(df)
    outputs: list[str] = [""] * n
    final_models: list[str] = [""] * n
    final_providers: list[str] = [""] * n
    per_row_costs: list[float] = [0.0] * n
    per_row_latencies: list[int] = [0] * n
    statuses: list[str] = [""] * n
    errors: list[str] = [""] * n
    rounds_count: list[int] = [0] * n
    drafts_json: list[str] = ["[]"] * n
    critiques_json: list[str] = ["[]"] * n
    converged_flags: list[bool] = [False] * n

    cumulative_cost = 0.0
    sentinel: Optional[dict] = None
    rows_processed = 0
    # Track per-row: did this row get at least one completed drafter
    # call? If the budget cap fires on round 1 before any drafter
    # call lands, we don't persist the row (its _llm_output would be
    # bogus "" with status="success" - misleading). Better: just
    # the sentinel, no partial row.
    persisted_row_indexes: list[int] = []

    for i in range(n):
        if sentinel is not None:
            break
        row = df.iloc[i]
        row_drafts: list[str] = []
        row_critiques: list[str] = []
        row_cost = 0.0
        row_latency = 0
        last_draft = ""
        last_status = "pending"  # only set to "success"/"error" after a drafter call attempt
        last_error = ""
        last_model = drafter_model
        last_provider = ""
        rounds_done = 0
        converged = False

        for round_idx in range(max_rounds):
            # ── Drafter call ──────────────────────────────────────────
            if round_idx == 0:
                drafter_full = build_full_prompt(drafter_prompt, row, cols)
            else:
                drafter_full = build_full_prompt(
                    revise_template.format(
                        drafter_prompt=drafter_prompt,
                        prev_draft=last_draft,
                        critique=row_critiques[-1],
                    ),
                    row, cols,
                )

            if cap is not None:
                try:
                    est = estimate_cost_usd(
                        drafter_model, [drafter_full],
                        system=system, max_tokens=max_tokens,
                    )
                    next_estimate = float(est["cost_usd"])
                except LLMRouterError as exc:
                    logger.warning(
                        "[!] llm_refine drafter estimator failed at row %d "
                        "round %d: %s", i + 1, round_idx + 1, exc,
                    )
                    raise LLMPipeError(
                        f"Budget gate estimator failed at drafter "
                        f"(row {i + 1}, round {round_idx + 1}): {exc}"
                    ) from exc
                if cumulative_cost + next_estimate > cap:
                    logger.info(
                        "[i] llm_refine: budget cap $%.6f reached at "
                        "row %d round %d drafter (cumulative=$%.6f, "
                        "next_est=$%.6f). Stopping.",
                        cap, i + 1, round_idx + 1,
                        cumulative_cost, next_estimate,
                    )
                    sentinel = _budget_sentinel_row(
                        model=drafter_model, rows_processed=rows_processed,
                        rows_total=n, cumulative_cost=cumulative_cost,
                        next_estimate=next_estimate, cap=cap,
                    )
                    break

            try:
                drafter_resp = call_llm(
                    drafter_model,
                    prompt=drafter_full,
                    system=system,
                    use_cache=use_cache,
                    max_tokens=max_tokens,
                    source=f"llm_refine_drafter_round_{round_idx + 1}",
                )
                last_draft = drafter_resp.text
                last_model = drafter_resp.model_id
                last_provider = drafter_resp.provider
                row_drafts.append(last_draft)
                row_cost += float(drafter_resp.cost_usd)
                row_latency += int(drafter_resp.latency_ms)
                cumulative_cost += float(drafter_resp.cost_usd)
                rounds_done = round_idx + 1
                last_status = "success"
                last_error = ""
            except LLMRouterError as exc:
                last_status = "error"
                last_error = (
                    f"drafter_round_{round_idx + 1}_failed: "
                    f"{exc.error_class}: {exc}"[:1000]
                )
                logger.warning(
                    "[!] llm_refine drafter row %d round %d failed: %s - %s",
                    i + 1, round_idx + 1, exc.error_class, exc,
                )
                # Stop this row's loop on drafter failure; keep what we have
                break

            # ── Critic call ───────────────────────────────────────────
            critic_full = build_full_prompt(
                _CRITIC_TEMPLATE.format(
                    critic_prompt=critic_prompt,
                    draft=last_draft,
                ),
                row, cols,
            )

            if cap is not None:
                try:
                    est = estimate_cost_usd(
                        critic_model, [critic_full],
                        system=system, max_tokens=max_tokens,
                    )
                    next_estimate = float(est["cost_usd"])
                except LLMRouterError as exc:
                    logger.warning(
                        "[!] llm_refine critic estimator failed at row %d "
                        "round %d: %s", i + 1, round_idx + 1, exc,
                    )
                    raise LLMPipeError(
                        f"Budget gate estimator failed at critic "
                        f"(row {i + 1}, round {round_idx + 1}): {exc}"
                    ) from exc
                if cumulative_cost + next_estimate > cap:
                    logger.info(
                        "[i] llm_refine: budget cap $%.6f reached at "
                        "row %d round %d critic (cumulative=$%.6f, "
                        "next_est=$%.6f). Stopping.",
                        cap, i + 1, round_idx + 1,
                        cumulative_cost, next_estimate,
                    )
                    sentinel = _budget_sentinel_row(
                        model=critic_model, rows_processed=rows_processed,
                        rows_total=n, cumulative_cost=cumulative_cost,
                        next_estimate=next_estimate, cap=cap,
                    )
                    break

            try:
                critic_resp = call_llm(
                    critic_model,
                    prompt=critic_full,
                    system=system,
                    use_cache=use_cache,
                    max_tokens=max_tokens,
                    source=f"llm_refine_critic_round_{round_idx + 1}",
                )
                row_critiques.append(critic_resp.text)
                row_cost += float(critic_resp.cost_usd)
                row_latency += int(critic_resp.latency_ms)
                cumulative_cost += float(critic_resp.cost_usd)
            except LLMRouterError as exc:
                # Critic failure: keep the round's draft; stop further
                # iteration. Don't mark the row as failed since we have
                # a usable draft. Note in error column.
                row_critiques.append("")
                last_error = (
                    f"critic_round_{round_idx + 1}_failed: "
                    f"{exc.error_class}: {exc}"[:1000]
                )
                logger.warning(
                    "[!] llm_refine critic row %d round %d failed: %s - %s",
                    i + 1, round_idx + 1, exc.error_class, exc,
                )
                break

            # Convergence check on the critic's output
            if _check_converged(critic_resp.text, converge_when_critic_says):
                converged = True
                break

        # Persist row results - but only if the row got at least one
        # drafter call attempt (status is no longer "pending"). When
        # the cap fires on round 1 before any drafter call completes,
        # the row has no usable draft and shouldn't be persisted with
        # bogus "" output.
        if last_status != "pending":
            outputs[i] = last_draft
            final_models[i] = last_model
            final_providers[i] = last_provider
            per_row_costs[i] = row_cost
            per_row_latencies[i] = row_latency
            statuses[i] = last_status
            errors[i] = last_error
            rounds_count[i] = rounds_done
            drafts_json[i] = json.dumps(row_drafts)
            critiques_json[i] = json.dumps(row_critiques)
            converged_flags[i] = converged
            persisted_row_indexes.append(i)
        rows_processed += 1

    # Build result DataFrame from PERSISTED rows only - rows that got
    # zero successful drafter calls are excluded entirely (their data
    # would be misleading "" outputs).
    if persisted_row_indexes:
        out = df.iloc[persisted_row_indexes].copy().reset_index(drop=True)
        out["_llm_output"] = [outputs[i] for i in persisted_row_indexes]
        out["_llm_model"] = [final_models[i] for i in persisted_row_indexes]
        out["_llm_provider"] = [final_providers[i] for i in persisted_row_indexes]
        out["_llm_cost_usd"] = [per_row_costs[i] for i in persisted_row_indexes]
        out["_llm_latency_ms"] = [per_row_latencies[i] for i in persisted_row_indexes]
        out["_llm_status"] = [statuses[i] for i in persisted_row_indexes]
        out["_llm_error"] = [errors[i] for i in persisted_row_indexes]
        out["_llm_refine_rounds"] = [rounds_count[i] for i in persisted_row_indexes]
        out["_llm_refine_drafts"] = [drafts_json[i] for i in persisted_row_indexes]
        out["_llm_refine_critiques"] = [critiques_json[i] for i in persisted_row_indexes]
        out["_llm_refine_converged"] = [converged_flags[i] for i in persisted_row_indexes]
    else:
        out = _llm_refine_empty_result(df.iloc[:0])

    if sentinel is not None:
        sentinel_row = {**sentinel}
        sentinel_row["_llm_refine_rounds"] = 0
        sentinel_row["_llm_refine_drafts"] = "[]"
        sentinel_row["_llm_refine_critiques"] = "[]"
        sentinel_row["_llm_refine_converged"] = False
        for col in df.columns:
            if col not in sentinel_row:
                sentinel_row[col] = None
        out = pd.concat([out, pd.DataFrame([sentinel_row])], ignore_index=True)

    persisted = len(persisted_row_indexes)
    n_converged = sum(converged_flags[i] for i in persisted_row_indexes) if persisted else 0
    avg_rounds = (
        sum(rounds_count[i] for i in persisted_row_indexes) / max(persisted, 1)
    )
    logger.info(
        "[i] llm_refine: drafter=%s critic=%s rows=%d "
        "rows_processed=%d converged=%d avg_rounds=%.2f "
        "cumulative_cost_usd=%.6f cap=%s",
        drafter_model, critic_model, n,
        rows_processed, n_converged, avg_rounds, cumulative_cost,
        f"${cap:.6f}" if cap is not None else "none",
    )
    return out


# ── Slice-3 (Phase 4 / Bet 3 slice 3): | llm_ensemble - multi-model voting ──
#
# Each row sends the SAME prompt to N models; outputs are aggregated
# by the chosen aggregator (majority / average / unanimous). The
# winning answer becomes ``_llm_output`` along with an agreement
# metric (0-1) and the per-model output array for audit.
#
# Cost economics: N× the per-row cost of a single | llm. Worth it
# when:
#   * Disagreement among models is itself a signal (high-variance
#     classifications need human review)
#   * High-stakes decisions where individual model bias might dominate
#   * Verifying a cheap model's classification with consensus
#
# Per-row output columns (additive to the standard | llm columns):
#   _llm_ensemble_models      str (JSON array) - model ids called
#   _llm_ensemble_outputs     str (JSON array) - per-model outputs
#   _llm_ensemble_agreement   float - fraction of models that agreed
#                                     with the winning answer (0-1)
#   _llm_ensemble_aggregator  str - which aggregator was used


_VALID_AGGREGATORS = ("majority", "average", "unanimous")


def _aggregate_majority(outputs: list[str]) -> tuple[str, float, str]:
    """Plurality vote over case-folded outputs. Returns (winner_text,
    agreement_fraction, status). Empty / all-empty inputs → no_consensus.
    """
    from collections import Counter
    cleaned = [(s.strip(), s.strip().lower()) for s in outputs if s and s.strip()]
    if not cleaned:
        return ("", 0.0, "no_consensus")
    counter = Counter(folded for _, folded in cleaned)
    winner_folded, count = counter.most_common(1)[0]
    agreement = count / len(cleaned)
    # Surface the original-cased form of the first matching output
    for original, folded in cleaned:
        if folded == winner_folded:
            return (original, agreement, "success")
    return (winner_folded, agreement, "success")


def _aggregate_average(outputs: list[str]) -> tuple[str, float, str]:
    """Mean of parsed numeric outputs (uses _parse_confidence).
    NaN-valued outputs are excluded. Agreement = fraction of inputs
    that parsed to a number. Returns string-formatted mean."""
    parsed = [_parse_confidence(o) for o in outputs]
    valid = [v for v in parsed if not np.isnan(v)]
    if not valid:
        return ("", 0.0, "no_consensus")
    avg = sum(valid) / len(valid)
    agreement = len(valid) / len(parsed)
    # Format with a clean precision so downstream parsing works
    return (f"{avg:.6f}".rstrip("0").rstrip("."), agreement, "success")


def _aggregate_unanimous(outputs: list[str]) -> tuple[str, float, str]:
    """All non-empty outputs must match (case-insensitive). Otherwise
    no_consensus. Empty outputs in the list count as missing → break
    unanimity."""
    if not outputs or any(not o or not o.strip() for o in outputs):
        return ("", 0.0, "no_consensus")
    folded = {s.strip().lower() for s in outputs}
    if len(folded) == 1:
        return (outputs[0].strip(), 1.0, "success")
    return ("", 0.0, "no_consensus")


def _llm_ensemble_empty_result(
    df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """Well-shaped empty DataFrame for the empty-input case."""
    out = df.copy() if df is not None else pd.DataFrame()
    out["_llm_output"] = pd.array([], dtype="object")
    out["_llm_model"] = pd.array([], dtype="object")
    out["_llm_provider"] = pd.array([], dtype="object")
    out["_llm_cost_usd"] = pd.array([], dtype="float64")
    out["_llm_latency_ms"] = pd.array([], dtype="int64")
    out["_llm_status"] = pd.array([], dtype="object")
    out["_llm_error"] = pd.array([], dtype="object")
    out["_llm_ensemble_models"] = pd.array([], dtype="object")
    out["_llm_ensemble_outputs"] = pd.array([], dtype="object")
    out["_llm_ensemble_agreement"] = pd.array([], dtype="float64")
    out["_llm_ensemble_aggregator"] = pd.array([], dtype="object")
    return out


def llm_ensemble_pipe(
    df: pd.DataFrame,
    *,
    models: list[str],
    prompt: str,
    aggregator: str = "majority",
    min_agreement: float = 0.0,
    system: Optional[str] = None,
    field: Optional[str] = None,
    use_cache: bool = True,
    max_tokens: Optional[int] = None,
    max_cost_usd: Optional[float] = None,
    dry_run: bool = False,
) -> pd.DataFrame:
    """Multi-model voting.

    Each row sends the SAME prompt to every model in ``models``; the
    per-model outputs are aggregated via ``aggregator``:

    * ``majority`` (default) - case-insensitive plurality vote on the
      output strings. Winner = most-common output. Agreement =
      fraction of models that agreed with the winner.
    * ``average`` - parse each output as a number (whole-string float
      → JSON ``confidence`` key → first number in text, same as
      slice-1's ``_parse_confidence``). Winner = mean of parseable
      values. Agreement = fraction of outputs that parsed.
    * ``unanimous`` - all outputs must match (case-insensitive). If
      any disagree or any error, status flips to ``no_consensus``.

    ``min_agreement`` (0.0 default) - when set, after aggregation if
    ``agreement < min_agreement`` the status flips to ``no_consensus``.
    Use this to require, e.g., ``min_agreement=0.66`` for "2 of 3
    models must agree".

    Output columns (per row): standard ``_llm_*`` columns carrying
    the AGGREGATED result + 4 ensemble-specific columns:

    * ``_llm_ensemble_models`` - JSON array of model ids called
    * ``_llm_ensemble_outputs`` - JSON array of per-model outputs
      (same order as models). Errored model entries become empty strings.
    * ``_llm_ensemble_agreement`` - float 0-1, fraction agreeing with winner
    * ``_llm_ensemble_aggregator`` - which aggregator was used

    Cost / latency are CUMULATIVE across all models for that row.

    Slice-7 contract: per-call estimator + cumulative cost cap;
    dry-run returns worst-case estimate (every row × every model).
    Money-leak canary pinned in
    tests/test_llm_ensemble_pipe.py::TestMoneyLeakCanary.
    """
    if not isinstance(models, list) or len(models) < 2:
        raise LLMPipeError(
            f"llm_ensemble requires a list of ≥ 2 model ids, got "
            f"{len(models) if isinstance(models, list) else type(models).__name__}."
        )
    for m in models:
        if not isinstance(m, str) or not m.strip():
            raise LLMPipeError(
                f"llm_ensemble model entries must be non-empty strings; "
                f"got {m!r} in {models}."
            )
    if not isinstance(prompt, str) or not prompt:
        raise LLMPipeError("llm_ensemble requires a non-empty prompt.")
    if aggregator not in _VALID_AGGREGATORS:
        raise LLMPipeError(
            f"llm_ensemble aggregator must be one of "
            f"{list(_VALID_AGGREGATORS)}, got {aggregator!r}."
        )
    if not isinstance(min_agreement, (int, float)) or isinstance(min_agreement, bool):
        raise LLMPipeError(
            "llm_ensemble min_agreement must be a number."
        )
    if min_agreement < 0 or min_agreement > 1:
        raise LLMPipeError(
            f"llm_ensemble min_agreement must be in [0, 1], got {min_agreement}."
        )

    cap = _resolve_budget_cap(max_cost_usd)
    aggregator_fn = {
        "majority": _aggregate_majority,
        "average": _aggregate_average,
        "unanimous": _aggregate_unanimous,
    }[aggregator]

    if df is None or len(df) == 0:
        if dry_run:
            return _dry_run_preview(
                model="+".join(models), prompts=[],
                system=system, max_tokens=max_tokens,
                pipe_label="llm_ensemble",
            )
        return _llm_ensemble_empty_result(df)

    cols = _resolve_columns(df, field)

    # ── Dry-run path - worst case (every row × every model) ───────────
    if dry_run:
        from analyzers.llm_router import estimate_cost_usd, LLMRouterError
        # Build per-row prompts (same prompt sent to each model)
        per_row_prompts = [
            build_full_prompt(prompt, df.iloc[i], cols)
            for i in range(len(df))
        ]
        per_model_estimates = []
        try:
            for m in models:
                est = estimate_cost_usd(
                    m, per_row_prompts,
                    system=system, max_tokens=max_tokens,
                )
                per_model_estimates.append(est)
        except LLMRouterError as exc:
            logger.warning(
                "[!] llm_ensemble dry_run estimator failed: %s - %s",
                exc.error_class, exc,
            )
            return pd.DataFrame({
                "_dry_run": [True],
                "_estimated_cost_usd": [0.0],
                "_estimated_input_tokens": [0],
                "_estimated_output_tokens": [0],
                "_row_count": [len(df)],
                "_llm_model": ["+".join(models)],
                "_llm_provider": [getattr(exc, "provider", "") or ""],
                "_max_tokens": [int(max_tokens or 0)],
                "_llm_output": [""],
                "_llm_cost_usd": [0.0],
                "_llm_latency_ms": [0],
                "_llm_status": ["error"],
                "_llm_error": [f"{exc.error_class}: {exc}"[:1000]],
            })
        worst_case = sum(float(e["cost_usd"]) for e in per_model_estimates)
        total_in = sum(int(e["input_tokens"]) for e in per_model_estimates)
        total_out = sum(int(e["output_tokens"]) for e in per_model_estimates)
        logger.info(
            "[i] llm_ensemble dry_run: models=%s rows=%d "
            "worst_case_cost_usd=%.6f (NO provider call made)",
            models, len(df), worst_case,
        )
        return pd.DataFrame({
            "_dry_run": [True],
            "_estimated_cost_usd": [worst_case],
            "_estimated_input_tokens": [total_in],
            "_estimated_output_tokens": [total_out],
            "_row_count": [len(df)],
            "_llm_model": ["+".join(models)],
            "_llm_provider": ["+".join(
                e["provider"] for e in per_model_estimates
            )],
            "_max_tokens": [int(max_tokens or 0)],
            "_llm_output": [""],
            "_llm_cost_usd": [0.0],
            "_llm_latency_ms": [0],
            "_llm_status": [_DRY_RUN_STATUS],
            "_llm_error": [""],
        })

    # ── Live execution: per-row × per-model voting ────────────────────
    from analyzers.llm_router import (
        call_llm, estimate_cost_usd, LLMRouterError,
    )

    n = len(df)
    final_outputs: list[str] = [""] * n
    final_models_used: list[str] = [""] * n
    final_providers: list[str] = [""] * n
    per_row_costs: list[float] = [0.0] * n
    per_row_latencies: list[int] = [0] * n
    statuses: list[str] = [""] * n
    errors: list[str] = [""] * n
    ensemble_models_json: list[str] = ["[]"] * n
    ensemble_outputs_json: list[str] = ["[]"] * n
    agreement_floats: list[float] = [0.0] * n
    aggregator_labels: list[str] = [""] * n

    cumulative_cost = 0.0
    sentinel: Optional[dict] = None
    persisted_row_indexes: list[int] = []

    for i in range(n):
        if sentinel is not None:
            break
        row = df.iloc[i]
        full_prompt = build_full_prompt(prompt, row, cols)

        per_model_outputs: list[str] = []
        per_model_errors: list[str] = []
        row_cost = 0.0
        row_latency = 0
        any_call_attempted = False

        for model_id in models:
            if cap is not None:
                try:
                    est = estimate_cost_usd(
                        model_id, [full_prompt],
                        system=system, max_tokens=max_tokens,
                    )
                    next_estimate = float(est["cost_usd"])
                except LLMRouterError as exc:
                    logger.warning(
                        "[!] llm_ensemble estimator failed at row %d "
                        "model %s: %s", i + 1, model_id, exc,
                    )
                    raise LLMPipeError(
                        f"Budget gate estimator failed at "
                        f"row {i + 1}, model {model_id!r}: {exc}"
                    ) from exc
                if cumulative_cost + next_estimate > cap:
                    logger.info(
                        "[i] llm_ensemble: budget cap $%.6f reached at "
                        "row %d model %s (cumulative=$%.6f, "
                        "next_est=$%.6f). Stopping.",
                        cap, i + 1, model_id,
                        cumulative_cost, next_estimate,
                    )
                    sentinel = _budget_sentinel_row(
                        model=model_id,
                        rows_processed=len(persisted_row_indexes),
                        rows_total=n,
                        cumulative_cost=cumulative_cost,
                        next_estimate=next_estimate, cap=cap,
                    )
                    break

            any_call_attempted = True
            try:
                resp = call_llm(
                    model_id,
                    prompt=full_prompt,
                    system=system,
                    use_cache=use_cache,
                    max_tokens=max_tokens,
                    source="llm_ensemble",
                )
                per_model_outputs.append(resp.text)
                per_model_errors.append("")
                row_cost += float(resp.cost_usd)
                row_latency += int(resp.latency_ms)
                cumulative_cost += float(resp.cost_usd)
            except LLMRouterError as exc:
                # Per-model error: empty output, error noted; ensemble
                # continues with remaining models. Voting handles the
                # gap (empty outputs are excluded from majority +
                # average; break unanimity).
                per_model_outputs.append("")
                per_model_errors.append(
                    f"{model_id}/{exc.error_class}: {exc}"[:200]
                )
                logger.warning(
                    "[!] llm_ensemble model %s failed at row %d: %s - %s",
                    model_id, i + 1, exc.error_class, exc,
                )

        if sentinel is not None:
            # The cap fired mid-row. If at least one model succeeded
            # for this row, persist the partial ensemble result;
            # otherwise drop the row entirely (no usable output).
            if any_call_attempted and any(per_model_outputs):
                # Aggregate what we have so far - partial-result is
                # still useful audit data.
                winner, agreement, ag_status = aggregator_fn(per_model_outputs)
                if min_agreement > 0 and agreement < min_agreement:
                    ag_status = "no_consensus"
                status = ag_status
                final_outputs[i] = winner
                final_models_used[i] = "+".join(models)
                final_providers[i] = "ensemble"
                per_row_costs[i] = row_cost
                per_row_latencies[i] = row_latency
                statuses[i] = status
                errors[i] = "; ".join(e for e in per_model_errors if e)
                ensemble_models_json[i] = json.dumps(models)
                ensemble_outputs_json[i] = json.dumps(per_model_outputs)
                agreement_floats[i] = agreement
                aggregator_labels[i] = aggregator
                persisted_row_indexes.append(i)
            break

        # Aggregate full per-row results
        if not any_call_attempted:
            # Defensive - shouldn't happen unless models list is empty,
            # which the validator rejected.
            continue

        winner, agreement, ag_status = aggregator_fn(per_model_outputs)
        if min_agreement > 0 and agreement < min_agreement:
            ag_status = "no_consensus"

        final_outputs[i] = winner
        final_models_used[i] = "+".join(models)
        final_providers[i] = "ensemble"
        per_row_costs[i] = row_cost
        per_row_latencies[i] = row_latency
        statuses[i] = ag_status
        errors[i] = "; ".join(e for e in per_model_errors if e)
        ensemble_models_json[i] = json.dumps(models)
        ensemble_outputs_json[i] = json.dumps(per_model_outputs)
        agreement_floats[i] = agreement
        aggregator_labels[i] = aggregator
        persisted_row_indexes.append(i)

    # Build result DataFrame from persisted rows only
    if persisted_row_indexes:
        out = df.iloc[persisted_row_indexes].copy().reset_index(drop=True)
        out["_llm_output"] = [final_outputs[i] for i in persisted_row_indexes]
        out["_llm_model"] = [final_models_used[i] for i in persisted_row_indexes]
        out["_llm_provider"] = [final_providers[i] for i in persisted_row_indexes]
        out["_llm_cost_usd"] = [per_row_costs[i] for i in persisted_row_indexes]
        out["_llm_latency_ms"] = [per_row_latencies[i] for i in persisted_row_indexes]
        out["_llm_status"] = [statuses[i] for i in persisted_row_indexes]
        out["_llm_error"] = [errors[i] for i in persisted_row_indexes]
        out["_llm_ensemble_models"] = [ensemble_models_json[i] for i in persisted_row_indexes]
        out["_llm_ensemble_outputs"] = [ensemble_outputs_json[i] for i in persisted_row_indexes]
        out["_llm_ensemble_agreement"] = [agreement_floats[i] for i in persisted_row_indexes]
        out["_llm_ensemble_aggregator"] = [aggregator_labels[i] for i in persisted_row_indexes]
    else:
        out = _llm_ensemble_empty_result(df.iloc[:0])

    if sentinel is not None:
        sentinel_row = {**sentinel}
        sentinel_row["_llm_ensemble_models"] = json.dumps(models)
        sentinel_row["_llm_ensemble_outputs"] = "[]"
        sentinel_row["_llm_ensemble_agreement"] = 0.0
        sentinel_row["_llm_ensemble_aggregator"] = aggregator
        for col in df.columns:
            if col not in sentinel_row:
                sentinel_row[col] = None
        out = pd.concat([out, pd.DataFrame([sentinel_row])], ignore_index=True)

    persisted = len(persisted_row_indexes)
    n_consensus = sum(
        1 for i in persisted_row_indexes
        if statuses[i] == "success"
    )
    avg_agreement = (
        sum(agreement_floats[i] for i in persisted_row_indexes) / max(persisted, 1)
    )
    logger.info(
        "[i] llm_ensemble: models=%s aggregator=%s rows=%d "
        "rows_processed=%d consensus=%d avg_agreement=%.2f "
        "cumulative_cost_usd=%.6f cap=%s",
        models, aggregator, n,
        persisted, n_consensus, avg_agreement, cumulative_cost,
        f"${cap:.6f}" if cap is not None else "none",
    )
    return out


# ── Slice-4 (Phase 4 / Bet 3 slice 4): | llm_until - convergence loop ──
#
# Each row runs up to ``max_iterations`` rounds of the same model.
# Round 1: model called with the operator's prompt + row data.
# Round k≥2: model called with the iterate_prompt template (default
# substitutes prev_output) + row data. Loop exits when ANY of:
#
#   * converge_when_output_contains substring matches the new output
#   * converge_when_output_unchanged is True AND output[k] == output[k-1]
#     (case-insensitive, stripped)
#   * converge_when_below_confidence is set AND _parse_confidence(output) < threshold
#   * max_iterations reached (the hard ceiling - always wins)
#
# Per-row output columns (additive to standard | llm columns):
#   _llm_until_iterations         int - how many model calls were made
#   _llm_until_outputs            str (JSON array) - every iteration's output
#   _llm_until_converged          bool - True iff a convergence sentinel fired
#                                        (False if max_iterations was hit)
#   _llm_until_convergence_reason str - "contains" | "unchanged" | "low_confidence" | "max_iterations"

# Default iterate-prompt template. Round k≥2 substitutes prev_output
# into this; row data is wrapped via build_full_prompt's <data> block.
_DEFAULT_ITERATE_TEMPLATE = (
    "{prompt}\n\n"
    "<previous_output>\n{prev_output}\n</previous_output>\n\n"
    "Continue from here."
)


def _outputs_match(a: str, b: str) -> bool:
    """Case-insensitive whitespace-stripped equality. Used for the
    converge_when_output_unchanged trigger.
    """
    return (a or "").strip().lower() == (b or "").strip().lower()


def _llm_until_empty_result(
    df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """Well-shaped empty DataFrame for the empty-input case."""
    out = df.copy() if df is not None else pd.DataFrame()
    out["_llm_output"] = pd.array([], dtype="object")
    out["_llm_model"] = pd.array([], dtype="object")
    out["_llm_provider"] = pd.array([], dtype="object")
    out["_llm_cost_usd"] = pd.array([], dtype="float64")
    out["_llm_latency_ms"] = pd.array([], dtype="int64")
    out["_llm_status"] = pd.array([], dtype="object")
    out["_llm_error"] = pd.array([], dtype="object")
    out["_llm_until_iterations"] = pd.array([], dtype="int64")
    out["_llm_until_outputs"] = pd.array([], dtype="object")
    out["_llm_until_converged"] = pd.array([], dtype="bool")
    out["_llm_until_convergence_reason"] = pd.array([], dtype="object")
    return out


def llm_until_pipe(
    df: pd.DataFrame,
    *,
    model: str,
    prompt: str,
    max_iterations: int,
    iterate_prompt: Optional[str] = None,
    converge_when_output_contains: Optional[str] = None,
    converge_when_output_unchanged: bool = False,
    converge_when_below_confidence: Optional[float] = None,
    system: Optional[str] = None,
    field: Optional[str] = None,
    use_cache: bool = True,
    max_tokens: Optional[int] = None,
    max_cost_usd: Optional[float] = None,
    dry_run: bool = False,
) -> pd.DataFrame:
    """Convergence loop with hard ceiling.

    Each row runs up to ``max_iterations`` rounds of the same model.
    Round 1 prompt = ``prompt`` + row data. Round k≥2 prompt =
    ``iterate_prompt`` template (default carries `prev_output`) + row data.

    Loop exits when ANY trigger fires:
      * ``converge_when_output_contains`` substring matches (case-insensitive)
      * ``converge_when_output_unchanged=True`` AND outputs[k] == outputs[k-1]
        (case-insensitive, whitespace-stripped)
      * ``converge_when_below_confidence`` set AND parsed confidence < threshold
      * ``max_iterations`` reached (always wins; no convergence implied)

    If NO convergence triggers are set, the loop ALWAYS runs to
    ``max_iterations`` (max_iterations is the only stop condition).
    Operators MUST supply ``max_iterations`` - it's the hard ceiling
    that prevents runaway loops; no default.

    Final row output is the LATEST iteration's output. The complete
    outputs array is JSON-serialised in
    ``_llm_until_outputs`` for audit. ``_llm_until_converged`` is
    True iff a convergence sentinel fired (False if max_iterations
    was the stop reason). ``_llm_until_convergence_reason`` names the
    specific trigger.

    Slice-7 contract honoured: per-call estimator + cumulative cost
    cap; dry-run returns worst-case (every row × max_iterations).
    Money-leak canary pinned in
    tests/test_llm_until_pipe.py::TestMoneyLeakCanary.
    Pending-status drift guard ensures that if the cap fires before
    any iteration's call lands for row 0, the result is exactly the
    sentinel - no partial bogus row (per
    reference_pending_status_for_iterative_pipes.md).
    """
    if not isinstance(model, str) or not model.strip():
        raise LLMPipeError("llm_until requires a non-empty model id.")
    if not isinstance(prompt, str) or not prompt:
        raise LLMPipeError("llm_until requires a non-empty prompt.")
    if not isinstance(max_iterations, int) or isinstance(max_iterations, bool):
        raise LLMPipeError(
            f"llm_until max_iterations must be an integer, got "
            f"{type(max_iterations).__name__}."
        )
    if max_iterations < 1:
        raise LLMPipeError(
            f"llm_until max_iterations must be ≥ 1, got {max_iterations}."
        )
    if converge_when_below_confidence is not None and (
        not isinstance(converge_when_below_confidence, (int, float))
        or isinstance(converge_when_below_confidence, bool)
    ):
        raise LLMPipeError(
            "llm_until converge_when_below_confidence must be a number."
        )

    cap = _resolve_budget_cap(max_cost_usd)
    iterate_template = iterate_prompt or _DEFAULT_ITERATE_TEMPLATE

    if df is None or len(df) == 0:
        if dry_run:
            return _dry_run_preview(
                model=model, prompts=[],
                system=system, max_tokens=max_tokens,
                pipe_label="llm_until",
            )
        return _llm_until_empty_result(df)

    cols = _resolve_columns(df, field)

    # ── Dry-run path - worst case (every row × max_iterations) ────────
    if dry_run:
        from analyzers.llm_router import estimate_cost_usd, LLMRouterError
        all_prompts: list[str] = []
        for i in range(len(df)):
            row = df.iloc[i]
            # Round 1 prompt
            all_prompts.append(build_full_prompt(prompt, row, cols))
            # max_iterations - 1 iterate-prompt placeholders
            for _ in range(max_iterations - 1):
                all_prompts.append(
                    build_full_prompt(
                        iterate_template.format(
                            prompt=prompt,
                            prev_output="(stub-prev-output for cost estimate)",
                        ),
                        row, cols,
                    )
                )
        try:
            est = estimate_cost_usd(
                model, all_prompts,
                system=system, max_tokens=max_tokens,
            )
        except LLMRouterError as exc:
            logger.warning(
                "[!] llm_until dry_run estimator failed: %s - %s",
                exc.error_class, exc,
            )
            return pd.DataFrame({
                "_dry_run": [True],
                "_estimated_cost_usd": [0.0],
                "_estimated_input_tokens": [0],
                "_estimated_output_tokens": [0],
                "_row_count": [len(df)],
                "_llm_model": [model],
                "_llm_provider": [getattr(exc, "provider", "") or ""],
                "_max_tokens": [int(max_tokens or 0)],
                "_llm_output": [""],
                "_llm_cost_usd": [0.0],
                "_llm_latency_ms": [0],
                "_llm_status": ["error"],
                "_llm_error": [f"{exc.error_class}: {exc}"[:1000]],
            })
        worst_case = float(est["cost_usd"])
        logger.info(
            "[i] llm_until dry_run: model=%s rows=%d max_iterations=%d "
            "worst_case_cost_usd=%.6f (NO provider call made)",
            model, len(df), max_iterations, worst_case,
        )
        return pd.DataFrame({
            "_dry_run": [True],
            "_estimated_cost_usd": [worst_case],
            "_estimated_input_tokens": [int(est["input_tokens"])],
            "_estimated_output_tokens": [int(est["output_tokens"])],
            "_row_count": [len(df)],
            "_llm_model": [est["model_id"]],
            "_llm_provider": [est["provider"]],
            "_max_tokens": [int(est["max_tokens"])],
            "_llm_output": [""],
            "_llm_cost_usd": [0.0],
            "_llm_latency_ms": [0],
            "_llm_status": [_DRY_RUN_STATUS],
            "_llm_error": [""],
        })

    # ── Live execution: per-row convergence loop ──────────────────────
    from analyzers.llm_router import (
        call_llm, estimate_cost_usd, LLMRouterError,
    )

    n = len(df)
    final_outputs: list[str] = [""] * n
    final_models: list[str] = [""] * n
    final_providers: list[str] = [""] * n
    per_row_costs: list[float] = [0.0] * n
    per_row_latencies: list[int] = [0] * n
    statuses: list[str] = [""] * n
    errors: list[str] = [""] * n
    iterations_count: list[int] = [0] * n
    outputs_json: list[str] = ["[]"] * n
    converged_flags: list[bool] = [False] * n
    convergence_reasons: list[str] = [""] * n

    cumulative_cost = 0.0
    sentinel: Optional[dict] = None
    persisted_row_indexes: list[int] = []

    for i in range(n):
        if sentinel is not None:
            break
        row = df.iloc[i]
        row_outputs: list[str] = []
        row_cost = 0.0
        row_latency = 0
        last_output = ""
        last_status = "pending"   # only flips after a real attempt
        last_error = ""
        last_model = model
        last_provider = ""
        iters_done = 0
        converged = False
        convergence_reason = ""

        for iter_idx in range(max_iterations):
            # Build the prompt for this iteration
            if iter_idx == 0:
                full = build_full_prompt(prompt, row, cols)
            else:
                full = build_full_prompt(
                    iterate_template.format(
                        prompt=prompt, prev_output=last_output,
                    ),
                    row, cols,
                )

            # Slice-7 budget gate
            if cap is not None:
                try:
                    est = estimate_cost_usd(
                        model, [full],
                        system=system, max_tokens=max_tokens,
                    )
                    next_estimate = float(est["cost_usd"])
                except LLMRouterError as exc:
                    logger.warning(
                        "[!] llm_until estimator failed at row %d "
                        "iter %d: %s", i + 1, iter_idx + 1, exc,
                    )
                    raise LLMPipeError(
                        f"Budget gate estimator failed at "
                        f"row {i + 1}, iter {iter_idx + 1}: {exc}"
                    ) from exc
                if cumulative_cost + next_estimate > cap:
                    logger.info(
                        "[i] llm_until: budget cap $%.6f reached at "
                        "row %d iter %d (cumulative=$%.6f, "
                        "next_est=$%.6f). Stopping.",
                        cap, i + 1, iter_idx + 1,
                        cumulative_cost, next_estimate,
                    )
                    sentinel = _budget_sentinel_row(
                        model=model,
                        rows_processed=len(persisted_row_indexes),
                        rows_total=n, cumulative_cost=cumulative_cost,
                        next_estimate=next_estimate, cap=cap,
                    )
                    break

            # Call the model
            try:
                resp = call_llm(
                    model,
                    prompt=full,
                    system=system,
                    use_cache=use_cache,
                    max_tokens=max_tokens,
                    source=f"llm_until_iter_{iter_idx + 1}",
                )
                prev_output_for_unchanged = last_output
                last_output = resp.text
                last_model = resp.model_id
                last_provider = resp.provider
                row_outputs.append(last_output)
                row_cost += float(resp.cost_usd)
                row_latency += int(resp.latency_ms)
                cumulative_cost += float(resp.cost_usd)
                iters_done = iter_idx + 1
                last_status = "success"
                last_error = ""
            except LLMRouterError as exc:
                last_status = "error"
                last_error = (
                    f"iter_{iter_idx + 1}_failed: "
                    f"{exc.error_class}: {exc}"[:1000]
                )
                logger.warning(
                    "[!] llm_until row %d iter %d failed: %s - %s",
                    i + 1, iter_idx + 1, exc.error_class, exc,
                )
                # Stop this row's loop on call failure
                break

            # ── Convergence checks (after at least one call lands) ────
            # Order matters only for the convergence_reason label;
            # any one trigger short-circuits the rest.
            if (
                converge_when_output_contains
                and converge_when_output_contains.lower() in last_output.lower()
            ):
                converged = True
                convergence_reason = "contains"
                break

            if (
                converge_when_output_unchanged
                and iter_idx >= 1
                and _outputs_match(last_output, prev_output_for_unchanged)
            ):
                converged = True
                convergence_reason = "unchanged"
                break

            if converge_when_below_confidence is not None:
                conf = _parse_confidence(last_output)
                # Below-threshold parsed confidence (NaN treated as
                # "couldn't decide" → does NOT converge here, since
                # callers using this trigger want a stable numeric).
                if not np.isnan(conf) and conf < converge_when_below_confidence:
                    converged = True
                    convergence_reason = "low_confidence"
                    break

        # Convergence reason fallback: if loop completed all iterations
        # without a sentinel firing, the reason is max_iterations.
        if last_status == "success" and not converged:
            convergence_reason = "max_iterations"

        # Persist row only if at least one call attempt landed
        if last_status != "pending":
            final_outputs[i] = last_output
            final_models[i] = last_model
            final_providers[i] = last_provider
            per_row_costs[i] = row_cost
            per_row_latencies[i] = row_latency
            statuses[i] = last_status
            errors[i] = last_error
            iterations_count[i] = iters_done
            outputs_json[i] = json.dumps(row_outputs)
            converged_flags[i] = converged
            convergence_reasons[i] = convergence_reason
            persisted_row_indexes.append(i)

    # Build result DataFrame from persisted rows only
    if persisted_row_indexes:
        out = df.iloc[persisted_row_indexes].copy().reset_index(drop=True)
        out["_llm_output"] = [final_outputs[i] for i in persisted_row_indexes]
        out["_llm_model"] = [final_models[i] for i in persisted_row_indexes]
        out["_llm_provider"] = [final_providers[i] for i in persisted_row_indexes]
        out["_llm_cost_usd"] = [per_row_costs[i] for i in persisted_row_indexes]
        out["_llm_latency_ms"] = [per_row_latencies[i] for i in persisted_row_indexes]
        out["_llm_status"] = [statuses[i] for i in persisted_row_indexes]
        out["_llm_error"] = [errors[i] for i in persisted_row_indexes]
        out["_llm_until_iterations"] = [iterations_count[i] for i in persisted_row_indexes]
        out["_llm_until_outputs"] = [outputs_json[i] for i in persisted_row_indexes]
        out["_llm_until_converged"] = [converged_flags[i] for i in persisted_row_indexes]
        out["_llm_until_convergence_reason"] = [convergence_reasons[i] for i in persisted_row_indexes]
    else:
        out = _llm_until_empty_result(df.iloc[:0])

    if sentinel is not None:
        sentinel_row = {**sentinel}
        sentinel_row["_llm_until_iterations"] = 0
        sentinel_row["_llm_until_outputs"] = "[]"
        sentinel_row["_llm_until_converged"] = False
        sentinel_row["_llm_until_convergence_reason"] = "budget_exceeded"
        for col in df.columns:
            if col not in sentinel_row:
                sentinel_row[col] = None
        out = pd.concat([out, pd.DataFrame([sentinel_row])], ignore_index=True)

    persisted = len(persisted_row_indexes)
    n_converged = sum(converged_flags[i] for i in persisted_row_indexes) if persisted else 0
    avg_iters = (
        sum(iterations_count[i] for i in persisted_row_indexes) / max(persisted, 1)
    )
    logger.info(
        "[i] llm_until: model=%s rows=%d rows_processed=%d "
        "converged=%d avg_iterations=%.2f max_iterations=%d "
        "cumulative_cost_usd=%.6f cap=%s",
        model, n, persisted, n_converged, avg_iters, max_iterations,
        cumulative_cost,
        f"${cap:.6f}" if cap is not None else "none",
    )
    return out


__all__ = [
    "LLMPipeError",
    "build_batch_prompt",
    "build_full_prompt",
    "llm_batch_pipe",
    "llm_ensemble_pipe",
    "llm_pipe",
    "llm_refine_pipe",
    "llm_route_pipe",
    "llm_until_pipe",
]

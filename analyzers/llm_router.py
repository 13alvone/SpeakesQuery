"""
LLM Router - Phase 2 / Bet 3 slice 2 (+ slice 2.5 OpenAI removal)
─────────────────────────────────────────────────────────────────
Single dispatcher for every Phase 2 LLM call. Looks up a model record
in :mod:`model_store` by ``model_id``, picks the provider-specific
transport, and returns a uniform :class:`LLMResponse` regardless of
whether the call went to Anthropic cloud, a self-hosted LM Studio
server, or a local Ollama daemon.

Design choices (locked in by user 2026-05-08):

* **Blocking** - caller waits for the full response. SPQL pipes pass
  complete DataFrames between stages; partial responses don't compose.
* **Sequential** - one in-flight call at a time. Concurrency layers
  on as its own slice if/when use cases demand it (the budget gate
  in slice 7 is the natural pairing).
* **Anthropic always routes through** :func:`analyzers.claude_client.call_messages_create`
  per the CLAUDE.md convention - the existing wrapper handles retry,
  daily-budget tracking, history capture, and secret scrubbing.
  This router just adapts the result into an :class:`LLMResponse`.
* **LM Studio uses Chat Completions HTTP** - the JSON wire shape used
  by self-hosted LLM servers including LM Studio, vLLM, and
  llama.cpp server. Endpoint comes from the registry record (no cloud
  default - every supported provider in this category is self-hosted).
  Future similar self-hosted backends drop in by adding their provider
  to ``ALLOWED_PROVIDERS`` and routing here.
* **Ollama uses its own** ``/api/chat`` endpoint (uses
  ``prompt_eval_count`` / ``eval_count`` for token usage).
* **Gemini** stub raises :class:`LLMRouterError` with a clear "ship in
  a future slice" message - no SDK dep added until there's demand.
* **OpenAI is deliberately omitted.** Per user direction 2026-05-08,
  SpeakesQuery does not interact with OpenAI's company or servers as a
  matter of principle. The Chat Completions wire protocol is
  industry-standard for self-hosted servers and that's what
  ``_call_chat_completions`` implements; LM Studio (and any future
  similar self-hosted backend) is its supported caller.

API key lookup follows the same convention as ``claude_client._get_api_key``:
``vault.retrieve(_GLOBAL_SCRIPT_ID, key_name)`` where
``_GLOBAL_SCRIPT_ID = -1``. Falls back to empty string on any error so
downstream provider-specific logic raises a helpful "set the key in the
vault" error.

The router does **not** implement caching - slice 3 ships
``llm_call_history.sqlite`` (generalising ``claude_api_history``) with
content-hash cache keys. For slice 2 every call goes through to the
provider; slice 3's cache layers on transparently.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────

# script_id sentinel for global / unscoped credentials (matches
# claude_client and the analyzer-key endpoint convention).
_GLOBAL_SCRIPT_ID = -1

# Provider → vault key name. Ollama isn't here because Ollama servers
# typically don't authenticate. LM Studio's key is OPTIONAL - if absent,
# the router sends no Authorization header (LM Studio defaults to no
# auth on a trusted LAN).
_PROVIDER_API_KEY_NAMES: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "lmstudio": "LMSTUDIO_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

_PROVIDERS_REQUIRING_API_KEY = frozenset({"anthropic", "gemini"})

# In-process cache for vault keys, identical TTL+lock contract as
# claude_client._api_key_cache (60s TTL).
_API_KEY_TTL_S = 60.0
_vault_lock = threading.Lock()
_api_key_cache: dict[str, tuple[str, float]] = {}


# ── Public dataclasses + errors ─────────────────────────────────────

@dataclass
class LLMResponse:
    """Uniform LLM response across every provider transport.

    Slice 3's call cache will hash this struct (less ``raw_response``)
    as the cache key. Slice 4+ ``| llm`` SPQL pipe surfaces the
    fields as ``_llm_output``, ``_llm_model``, ``_llm_cost_usd``, etc.
    """

    text: str
    model_id: str          # registry id (e.g. "claude-sonnet-4-6")
    provider: str          # "anthropic" | "openai" | "lmstudio" | "ollama"
    model_name: str        # provider-specific name (e.g. "claude-sonnet-4-6")
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    request_id: str
    raw_response: Optional[dict] = field(default=None, repr=False)


class LLMRouterError(RuntimeError):
    """Raised on routing failures.

    Catches: unknown model_id, missing required API key, transport
    failure (HTTP error, timeout, decode error), provider-specific
    error responses, deferred providers (gemini) that aren't wired yet.
    """

    def __init__(
        self,
        message: str,
        *,
        model_id: str = "",
        provider: str = "",
        error_class: str = "",
        request_id: str = "",
    ) -> None:
        super().__init__(message)
        self.model_id = model_id
        self.provider = provider
        self.error_class = error_class
        self.request_id = request_id


# ── Internal helpers ─────────────────────────────────────────────────

def _project_root():
    from pathlib import Path
    return Path(__file__).parent.parent.resolve()


def _get_provider_api_key(key_name: str) -> str:
    """Retrieve a global API key from the credential vault.

    Returns ``""`` if the vault is unavailable or the key is not set.
    Caller decides whether emptiness is fatal (cloud providers raise;
    LM Studio tolerates).

    Cache: 60s TTL, identical to ``claude_client._get_api_key``.
    """
    if not key_name:
        return ""
    now = time.monotonic()
    with _vault_lock:
        cached = _api_key_cache.get(key_name)
        if cached is not None:
            value, fetched_at = cached
            if now - fetched_at < _API_KEY_TTL_S:
                return value

    try:
        from global_settings import get_settings
        from scheduled_input_engine.credentials import CredentialVault
        settings = get_settings()
        vault_db = _project_root() / "credentials.sqlite"
        with _vault_lock:
            vault = CredentialVault(
                str(vault_db), settings.get("credential_key_dir"),
            )
            try:
                value = vault.retrieve(_GLOBAL_SCRIPT_ID, key_name) or ""
            except KeyError:
                value = ""
            _api_key_cache[key_name] = (value, now)
            return value
    except Exception as exc:
        logger.warning("[!] Could not retrieve %s from vault: %s", key_name, exc)
        return ""


def _invalidate_api_key_cache(key_name: Optional[str] = None) -> None:
    """Clear cached key(s). When ``key_name`` is None, clears all."""
    with _vault_lock:
        if key_name is None:
            _api_key_cache.clear()
        else:
            _api_key_cache.pop(key_name, None)


# Conservative chars-per-token ratio for the dry-run estimator. Real
# tokenizers vary by model + content, but ~4 chars/token is the
# industry-standard rule of thumb for English text. The estimator
# chooses to OVER-estimate (i.e. assume FEWER chars per token, hence
# MORE tokens) to make the budget gate err on the safe side. Operators
# with non-English / code-heavy prompts can lower this via
# ``llm_chars_per_token`` setting (slice 7).
_DEFAULT_CHARS_PER_TOKEN = 4.0


def estimate_tokens_from_chars(text: str, *, chars_per_token: float = _DEFAULT_CHARS_PER_TOKEN) -> int:
    """Approximate token count from character length.

    Conservative - round UP so a borderline prompt doesn't slip past
    the budget gate. ``chars_per_token`` defaults to 4.0 (English-text
    industry rule of thumb); lower for code/non-English.
    """
    if not text:
        return 0
    if chars_per_token <= 0:
        raise ValueError(
            f"chars_per_token must be positive, got {chars_per_token!r}"
        )
    # Ceil division so 1-4 chars → 1 token, 5-8 chars → 2 tokens, etc.
    n = len(text)
    return int((n + chars_per_token - 1) // chars_per_token) if n else 0


def estimate_cost_usd(
    model_id: str,
    prompts: list[str],
    *,
    system: Optional[str] = None,
    max_tokens: Optional[int] = None,
    chars_per_token: float = _DEFAULT_CHARS_PER_TOKEN,
) -> dict:
    """Estimate cost for one-call-per-prompt dispatch WITHOUT hitting any provider.

    Used by the slice-7 ``dry_run=true`` mode and the budget-gate's
    pre-call check. Returns a dict ``{cost_usd, input_tokens,
    output_tokens, model_id, provider, max_tokens}``.

    The estimate is intentionally CONSERVATIVE (overestimates) so the
    budget gate never silently lets a real call exceed the cap. We
    assume:

    * Every prompt costs the FULL ``max_tokens`` worth of output
      (worst-case - many calls return shorter responses).
    * The system prompt counts toward EVERY call's input (not amortised).
    * Tokenization is ``len(text) / chars_per_token`` (round-up).

    Cache hits don't show here - the estimator is a static cost, not a
    cache-aware projection. The budget gate still applies the estimate
    to every call; cache hits return ``cost_usd=0.0`` at runtime so
    they contribute nothing to the cumulative actual cost. The estimate
    just enforces "if every call missed cache, would we go over?".

    Raises
    ------
    LLMRouterError
        On unknown ``model_id`` or invalid ``chars_per_token`` /
        ``max_tokens``.
    """
    if chars_per_token <= 0:
        raise LLMRouterError(
            f"chars_per_token must be positive, got {chars_per_token!r}",
            model_id=model_id, error_class="InvalidEstimatorParam",
        )
    if not isinstance(prompts, (list, tuple)):
        raise LLMRouterError(
            f"prompts must be a list[str], got {type(prompts).__name__}",
            model_id=model_id, error_class="InvalidEstimatorParam",
        )

    from model_store import get_store
    record = get_store().get_model(model_id)
    if record is None:
        raise LLMRouterError(
            f"Unknown model_id: {model_id!r}. Cannot estimate cost.",
            model_id=model_id, error_class="UnknownModel",
        )

    effective_max_tokens = int(
        max_tokens if max_tokens else record.get("max_output_tokens", 4096)
    )
    if effective_max_tokens <= 0:
        raise LLMRouterError(
            f"max_tokens must be positive, got {effective_max_tokens}",
            model_id=model_id, error_class="InvalidEstimatorParam",
        )

    system_tokens = (
        estimate_tokens_from_chars(system or "", chars_per_token=chars_per_token)
        if system else 0
    )

    total_input_tokens = 0
    n_calls = len(prompts)
    for p in prompts:
        if not isinstance(p, str):
            raise LLMRouterError(
                f"every prompt must be a str, got {type(p).__name__}",
                model_id=model_id, error_class="InvalidEstimatorParam",
            )
        total_input_tokens += (
            estimate_tokens_from_chars(p, chars_per_token=chars_per_token)
            + system_tokens
        )

    # Worst-case output: every call hits the max_tokens ceiling.
    total_output_tokens = effective_max_tokens * n_calls

    cost = _compute_cost(record, total_input_tokens, total_output_tokens)
    return {
        "cost_usd": cost,
        "input_tokens": int(total_input_tokens),
        "output_tokens": int(total_output_tokens),
        "model_id": record["id"],
        "provider": record["provider"],
        "model_name": record["model_name"],
        "max_tokens": effective_max_tokens,
        "n_calls": n_calls,
    }


def _compute_cost(record: dict, input_tokens: int, output_tokens: int) -> float:
    """Cost in USD from registry pricing × token usage.

    Matches the ``claude_client._pricing_for`` convention: pricing in
    USD per million tokens. Floors at zero with a loud error if the
    record's pricing somehow went negative (would silently CREDIT a
    budget ledger otherwise).
    """
    in_pm = float(record.get("cost_per_input_million_usd", 0.0))
    out_pm = float(record.get("cost_per_output_million_usd", 0.0))
    cost = (input_tokens / 1_000_000.0 * in_pm
            + output_tokens / 1_000_000.0 * out_pm)
    if cost < 0:
        logger.error(
            "[x] Negative cost computed for model %s "
            "(input_pm=%s, output_pm=%s, in_t=%d, out_t=%d). "
            "Flooring at 0.0 - check the registry pricing.",
            record.get("id"), in_pm, out_pm, input_tokens, output_tokens,
        )
        return 0.0
    return round(cost, 6)


def _log_router_call(
    *,
    request_id: str,
    source: str,
    model_id: str,
    provider: str,
    status: str,
    latency_ms: int,
    cost_usd: float,
    input_tokens: int,
    output_tokens: int,
    error_class: str = "",
    error_message: str = "",
) -> None:
    """Emit a system_event row for non-Anthropic calls.

    Anthropic calls already log to ``claude_api_history.sqlite`` and
    ``indexes/logs/claude_api/`` via the ``call_messages_create``
    wrapper - duplicating here would double-count. Slice 3 generalises
    history capture to all providers via ``llm_call_history.sqlite``.
    """
    try:
        from functionality.log_writer import log_system_event
        msg = (
            f"request_id={request_id} model={model_id} provider={provider} "
            f"status={status} latency_ms={latency_ms} cost_usd={cost_usd:.6f} "
            f"in={input_tokens} out={output_tokens}"
        )
        if error_class:
            msg += f" error_class={error_class}"
        if error_message:
            msg += f" error_message={error_message[:200]}"
        log_system_event(
            component="llm_router",
            event=f"{source}_{status}",
            message=msg,
            level="error" if status == "error" else "info",
        )
    except Exception:
        # Logging must never break the dispatch path.
        pass


# ── Provider transports ──────────────────────────────────────────────

def _call_anthropic(
    record: dict,
    *,
    prompt: str,
    system: Optional[str],
    max_tokens: int,
    timeout_seconds: int,
    request_id: str,
    source: str,
) -> LLMResponse:
    """Delegate to ``analyzers.claude_client.call_messages_create``.

    The wrapper handles retry / timeout / cost-logging / history
    capture / daily-budget ledger / secret scrubbing - all preserved
    unchanged. We just adapt the ``ClaudeCallResult`` into an
    ``LLMResponse``.
    """
    from analyzers.claude_client import call_messages_create, ClaudeCallError

    create_kwargs: dict[str, Any] = {
        "model": record["model_name"],
        "max_tokens": int(max_tokens),
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        create_kwargs["system"] = system

    try:
        result = call_messages_create(
            source=source,
            request_id=request_id,
            **create_kwargs,
        )
    except ClaudeCallError as exc:
        raise LLMRouterError(
            f"Anthropic call failed: {exc}",
            model_id=record["id"],
            provider="anthropic",
            error_class=exc.error_class,
            request_id=exc.request_id,
        ) from exc

    # Extract text from the response. Anthropic returns content as a
    # list of blocks; the first text block is what we want.
    text_blocks = []
    for block in getattr(result.response, "content", []) or []:
        block_text = getattr(block, "text", None)
        if block_text:
            text_blocks.append(block_text)
    text = "\n".join(text_blocks)

    return LLMResponse(
        text=text,
        model_id=record["id"],
        provider="anthropic",
        model_name=record["model_name"],
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        request_id=result.request_id,
        raw_response=None,  # claude_client already audits the full payload
    )


def _call_chat_completions(
    record: dict,
    *,
    prompt: str,
    system: Optional[str],
    max_tokens: int,
    timeout_seconds: int,
    request_id: str,
    source: str,
    api_key_name: str,
    api_key_required: bool,
) -> LLMResponse:
    """Chat Completions HTTP transport - used by ``lmstudio`` (and future
    similar self-hosted backends like vLLM, llama.cpp server).

    Wire shape: ``POST <endpoint>/chat/completions`` with
    ``{model, messages, max_tokens, ...}``; response is
    ``{choices: [{message: {content: ...}}], usage: {...}}``. This is
    the de-facto JSON shape across the self-hosted-LLM ecosystem.

    Endpoint comes from the registry record - there is no cloud
    fallback because no supported provider in this category is cloud-
    hosted. Slice 1.5's ``PROVIDERS_REQUIRING_ENDPOINT`` validation
    catches a missing endpoint at save-time.
    """
    endpoint = record.get("endpoint", "")
    if not endpoint:
        raise LLMRouterError(
            f"{record['provider']} requires an endpoint URL in the registry.",
            model_id=record["id"],
            provider=record["provider"],
            error_class="MissingEndpoint",
            request_id=request_id,
        )
    url = endpoint.rstrip("/") + "/chat/completions"

    api_key = _get_provider_api_key(api_key_name) if api_key_name else ""
    if api_key_required and not api_key:
        raise LLMRouterError(
            f"No {api_key_name} configured. Store one in the credential "
            f"vault (script_id={_GLOBAL_SCRIPT_ID}) before calling "
            f"provider={record['provider']}.",
            model_id=record["id"],
            provider=record["provider"],
            error_class="MissingCredential",
            request_id=request_id,
        )

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": record["model_name"],
        "messages": messages,
        "max_tokens": int(max_tokens),
    }
    # Optional per-record sampler overrides (validated allowlist in
    # ModelValidation.validate_sampling). Forwarded verbatim. Primary
    # use: pin a reasoning model's recommended sampling - notably
    # presence_penalty - so its <think> trace self-terminates instead of
    # looping past max_tokens and returning empty content (the
    # Qwen3.5-122B-A10B failure mode, 2026-06-07). Absent/empty → server
    # defaults apply, unchanged from before this field existed.
    sampling = record.get("sampling") or {}
    if isinstance(sampling, dict) and sampling:
        payload.update(sampling)

    started = time.monotonic()
    status = "success"
    error_class = ""
    error_message = ""
    response_json: Optional[dict] = None
    try:
        # nosec B113 - timeout IS supplied via the explicit timeout=
        # kwarg below (bandit's static matcher doesn't track through
        # float() coercion). Per-record timeout default is 120s for
        # OpenAI / 300s for LM Studio (validated 1-3600s in slice 1).
        resp = requests.post(  # nosec B113
            url, json=payload, headers=headers,
            timeout=float(timeout_seconds),
        )
    except requests.RequestException as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        status = "error"
        error_class = type(exc).__name__
        error_message = str(exc)[:500]
        _log_router_call(
            request_id=request_id, source=source,
            model_id=record["id"], provider=record["provider"],
            status=status, latency_ms=latency_ms, cost_usd=0.0,
            input_tokens=0, output_tokens=0,
            error_class=error_class, error_message=error_message,
        )
        raise LLMRouterError(
            f"{record['provider']} HTTP transport failed: {exc}",
            model_id=record["id"],
            provider=record["provider"],
            error_class=error_class,
            request_id=request_id,
        ) from exc

    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code >= 400:
        status = "error"
        error_class = f"HTTP{resp.status_code}"
        error_message = resp.text[:500]
        _log_router_call(
            request_id=request_id, source=source,
            model_id=record["id"], provider=record["provider"],
            status=status, latency_ms=latency_ms, cost_usd=0.0,
            input_tokens=0, output_tokens=0,
            error_class=error_class, error_message=error_message,
        )
        raise LLMRouterError(
            f"{record['provider']} returned HTTP {resp.status_code}: "
            f"{resp.text[:300]}",
            model_id=record["id"],
            provider=record["provider"],
            error_class=error_class,
            request_id=request_id,
        )

    try:
        response_json = resp.json()
    except ValueError as exc:
        raise LLMRouterError(
            f"{record['provider']} returned non-JSON: {resp.text[:300]}",
            model_id=record["id"],
            provider=record["provider"],
            error_class="DecodeError",
            request_id=request_id,
        ) from exc

    # Extract Chat Completions response shape.
    choices = response_json.get("choices") or []
    if not choices:
        raise LLMRouterError(
            f"{record['provider']} response had no choices: {response_json}",
            model_id=record["id"],
            provider=record["provider"],
            error_class="EmptyResponse",
            request_id=request_id,
        )
    text = (choices[0].get("message") or {}).get("content", "") or ""
    usage = response_json.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("completion_tokens", 0) or 0)
    cost = _compute_cost(record, input_tokens, output_tokens)

    _log_router_call(
        request_id=request_id, source=source,
        model_id=record["id"], provider=record["provider"],
        status=status, latency_ms=latency_ms, cost_usd=cost,
        input_tokens=input_tokens, output_tokens=output_tokens,
    )

    return LLMResponse(
        text=text,
        model_id=record["id"],
        provider=record["provider"],
        model_name=record["model_name"],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        request_id=request_id,
        raw_response=response_json,
    )


def _call_ollama(
    record: dict,
    *,
    prompt: str,
    system: Optional[str],
    max_tokens: int,
    timeout_seconds: int,
    request_id: str,
    source: str,
) -> LLMResponse:
    """Dispatch to an Ollama daemon's ``/api/chat`` endpoint.

    Ollama's protocol is similar to OpenAI's Chat Completions but
    distinct enough to warrant its own transport - token usage is
    reported as ``prompt_eval_count`` / ``eval_count`` rather than
    ``prompt_tokens`` / ``completion_tokens``. We don't pass a
    streaming flag (default is non-streaming for Ollama's /api/chat).
    """
    endpoint = (record.get("endpoint") or "").rstrip("/")
    if not endpoint:
        raise LLMRouterError(
            "Ollama requires an endpoint URL in the registry.",
            model_id=record["id"],
            provider="ollama",
            error_class="MissingEndpoint",
            request_id=request_id,
        )
    url = endpoint + "/api/chat"

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": record["model_name"],
        "messages": messages,
        "stream": False,
        "options": {"num_predict": int(max_tokens)},
    }

    started = time.monotonic()
    try:
        # nosec B113 - timeout IS supplied (see same comment in
        # _call_openai_compatible). Default 120s per Ollama record.
        resp = requests.post(url, json=payload, timeout=float(timeout_seconds))  # nosec B113
    except requests.RequestException as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        _log_router_call(
            request_id=request_id, source=source,
            model_id=record["id"], provider="ollama",
            status="error", latency_ms=latency_ms, cost_usd=0.0,
            input_tokens=0, output_tokens=0,
            error_class=type(exc).__name__,
            error_message=str(exc)[:500],
        )
        raise LLMRouterError(
            f"Ollama HTTP transport failed: {exc}",
            model_id=record["id"], provider="ollama",
            error_class=type(exc).__name__,
            request_id=request_id,
        ) from exc

    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code >= 400:
        _log_router_call(
            request_id=request_id, source=source,
            model_id=record["id"], provider="ollama",
            status="error", latency_ms=latency_ms, cost_usd=0.0,
            input_tokens=0, output_tokens=0,
            error_class=f"HTTP{resp.status_code}",
            error_message=resp.text[:500],
        )
        raise LLMRouterError(
            f"Ollama returned HTTP {resp.status_code}: {resp.text[:300]}",
            model_id=record["id"], provider="ollama",
            error_class=f"HTTP{resp.status_code}",
            request_id=request_id,
        )

    try:
        response_json = resp.json()
    except ValueError as exc:
        raise LLMRouterError(
            f"Ollama returned non-JSON: {resp.text[:300]}",
            model_id=record["id"], provider="ollama",
            error_class="DecodeError",
            request_id=request_id,
        ) from exc

    text = (response_json.get("message") or {}).get("content", "") or ""
    input_tokens = int(response_json.get("prompt_eval_count", 0) or 0)
    output_tokens = int(response_json.get("eval_count", 0) or 0)
    cost = _compute_cost(record, input_tokens, output_tokens)  # typically 0

    _log_router_call(
        request_id=request_id, source=source,
        model_id=record["id"], provider="ollama",
        status="success", latency_ms=latency_ms, cost_usd=cost,
        input_tokens=input_tokens, output_tokens=output_tokens,
    )

    return LLMResponse(
        text=text,
        model_id=record["id"],
        provider="ollama",
        model_name=record["model_name"],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        request_id=request_id,
        raw_response=response_json,
    )


def _call_gemini(
    record: dict,
    *,
    prompt: str,
    system: Optional[str],
    max_tokens: int,
    timeout_seconds: int,
    request_id: str,
    source: str,
) -> LLMResponse:
    """Stub - Gemini support deferred to a future small slice.

    The ``google-generativeai`` SDK adds ~100 MB of dependency footprint
    for an integration with no near-term user demand. When demand
    surfaces, this stub becomes the implementation entry point - the
    registry already accepts ``provider: gemini`` records.
    """
    raise LLMRouterError(
        "Gemini provider is in the registry enum but not yet implemented "
        "in the router. Requires the `google-generativeai` SDK and a "
        "Gemini API key in the credential vault. Ship in a future small "
        "slice when needed.",
        model_id=record["id"],
        provider="gemini",
        error_class="ProviderNotImplemented",
        request_id=request_id,
    )


# ── Public entry point ───────────────────────────────────────────────

def call_llm(
    model_id: str,
    *,
    prompt: str,
    system: Optional[str] = None,
    max_tokens: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
    request_id: Optional[str] = None,
    source: str = "llm_router",
    use_cache: bool = True,
    cache_max_age_seconds: Optional[int] = None,
) -> LLMResponse:
    """Dispatch a single-prompt LLM call by registry ``model_id``.

    Parameters
    ----------
    model_id :
        Registry id of the model to call (e.g. ``"claude-sonnet-4-6"``,
        ``"lmstudio-remote"``).
    prompt :
        User-message content. Required.
    system :
        Optional system prompt. Provider-specific construction handles
        the right wire-format (Anthropic top-level ``system``,
        Chat Completions message with role=system, etc.).
    max_tokens :
        Override for the per-record ``max_output_tokens`` default.
    timeout_seconds :
        Override for the per-record ``default_timeout_seconds`` default.
    request_id :
        Optional UUID for cross-system correlation. One is generated
        when omitted.
    source :
        Tag for the log row (``"llm_router"`` by default; the slice 4+
        SPQL pipes will pass ``"llm_pipe"``).
    use_cache :
        When True (default), check ``llm_call_history`` for a cached
        successful response with a matching ``content_hash`` before
        dispatching. Pass ``False`` for "always-fresh" callers
        (settings-test buttons, evaluation runs).
    cache_max_age_seconds :
        Optional TTL for cache hits. ``None`` (default) means unlimited
        - same prompt + same model + same kwargs → cached response
        forever, until the registry record changes (which changes
        ``model_name`` and therefore the content hash).

    Returns
    -------
    LLMResponse
        Uniform response shape across every provider. Cache hits
        return an ``LLMResponse`` reconstructed from the historical
        row with ``cost_usd=0.0`` and ``latency_ms=0`` (since the
        cache hit avoided the actual call).

    Raises
    ------
    LLMRouterError
        On any failure: unknown model_id, missing API key, transport
        failure, deferred provider (gemini).
    """
    rid = request_id or str(uuid.uuid4())

    if not isinstance(prompt, str) or not prompt:
        raise LLMRouterError(
            "call_llm requires a non-empty prompt string.",
            model_id=model_id, error_class="InvalidPrompt", request_id=rid,
        )

    # Look up the model record.
    from model_store import get_store
    record = get_store().get_model(model_id)
    if record is None:
        raise LLMRouterError(
            f"Unknown model_id: {model_id!r}. Add a model YAML under "
            "models/<id>.yaml or check the spelling.",
            model_id=model_id, error_class="UnknownModel", request_id=rid,
        )

    # Resolve effective per-call settings.
    effective_max_tokens = int(
        max_tokens if max_tokens else record.get("max_output_tokens", 4096)
    )
    effective_timeout = int(
        timeout_seconds if timeout_seconds
        else record.get("default_timeout_seconds", 120)
    )

    provider = record["provider"]

    # Compute content hash up-front so both the cache lookup AND the
    # post-dispatch history.record_call use the same key.
    from analyzers.llm_history_store import (
        compute_content_hash, get_store as _hist_store,
    )
    content_hash = compute_content_hash(
        model_id=record["id"],
        model_name=record["model_name"],
        provider=provider,
        prompt=prompt,
        system=system,
        max_tokens=effective_max_tokens,
    )

    # Cache lookup (opt-in via use_cache, default on). Only successful
    # historical rows are eligible - errored calls never serve cache.
    if use_cache:
        try:
            cached = _hist_store().get_cached_response(
                content_hash, max_age_seconds=cache_max_age_seconds,
            )
        except Exception as exc:
            # Cache lookup failure must NEVER break the dispatch path.
            logger.warning(
                "[!] llm_router: cache lookup failed (%s); "
                "falling through to live dispatch", exc,
            )
            cached = None
        if cached is not None:
            logger.info(
                "[i] llm_router: cache HIT for %s (content_hash=%s...)",
                record["id"], content_hash[:12],
            )
            return LLMResponse(
                text=cached.get("response_text") or "",
                model_id=record["id"],
                provider=provider,
                model_name=record["model_name"],
                input_tokens=int(cached.get("input_tokens") or 0),
                output_tokens=int(cached.get("output_tokens") or 0),
                cost_usd=0.0,        # cache hit avoided the call
                latency_ms=0,
                request_id=cached.get("request_id") or rid,
                raw_response=cached.get("raw_response"),
            )

    # Cache miss → live dispatch.
    response: Optional[LLMResponse] = None
    error_class = ""
    error_message = ""
    error_latency_ms = 0
    try:
        if provider == "anthropic":
            response = _call_anthropic(
                record, prompt=prompt, system=system,
                max_tokens=effective_max_tokens,
                timeout_seconds=effective_timeout,
                request_id=rid, source=source,
            )
        elif provider == "lmstudio":
            api_key_name = _PROVIDER_API_KEY_NAMES.get(provider, "")
            api_key_required = provider in _PROVIDERS_REQUIRING_API_KEY
            response = _call_chat_completions(
                record, prompt=prompt, system=system,
                max_tokens=effective_max_tokens,
                timeout_seconds=effective_timeout,
                request_id=rid, source=source,
                api_key_name=api_key_name,
                api_key_required=api_key_required,
            )
        elif provider == "ollama":
            response = _call_ollama(
                record, prompt=prompt, system=system,
                max_tokens=effective_max_tokens,
                timeout_seconds=effective_timeout,
                request_id=rid, source=source,
            )
        elif provider == "gemini":
            response = _call_gemini(
                record, prompt=prompt, system=system,
                max_tokens=effective_max_tokens,
                timeout_seconds=effective_timeout,
                request_id=rid, source=source,
            )
        else:
            # Unknown provider - registry validation should catch this
            # earlier, but defense in depth.
            raise LLMRouterError(
                f"No router transport for provider={provider!r}",
                model_id=model_id, provider=provider,
                error_class="UnknownProvider", request_id=rid,
            )
    except LLMRouterError as exc:
        error_class = exc.error_class or type(exc).__name__
        error_message = str(exc)[:1000]
        # Capture failure to history before re-raising so audit is
        # complete even on errors.
        try:
            _hist_store().record_call(
                request_id=rid,
                content_hash=content_hash,
                model_id=record["id"],
                provider=provider,
                model_name=record["model_name"],
                source=source,
                status="error",
                prompt=prompt,
                system=system,
                response_text=None,
                raw_response=None,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                latency_ms=error_latency_ms,
                max_tokens=effective_max_tokens,
                error_class=error_class,
                error_message=error_message,
            )
        except Exception as record_exc:
            logger.warning(
                "[!] llm_router: failed to record error to history: %s",
                record_exc,
            )
        raise

    # Successful dispatch → record to history before returning. The
    # request_id from the response wins (Anthropic preserves the one
    # claude_client minted; other providers use the rid we passed in).
    try:
        _hist_store().record_call(
            request_id=response.request_id or rid,
            content_hash=content_hash,
            model_id=record["id"],
            provider=provider,
            model_name=record["model_name"],
            source=source,
            status="success",
            prompt=prompt,
            system=system,
            response_text=response.text,
            raw_response=response.raw_response,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
            max_tokens=effective_max_tokens,
        )
    except Exception as exc:
        # History capture failure must NEVER break a successful call.
        logger.warning(
            "[!] llm_router: failed to record success to history: %s", exc,
        )
    return response


__all__ = [
    "LLMResponse",
    "LLMRouterError",
    "call_llm",
    "estimate_cost_usd",
    "estimate_tokens_from_chars",
]

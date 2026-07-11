"""LAN / local LLM smoke test.

Exercises every self-hosted model in the registry (``models/*.yaml``
with provider ``lmstudio`` or ``ollama``) end-to-end: raw HTTP health
checks against each distinct endpoint, then the production
`analyzers.llm_router.call_llm` path against every model. Reports
latency stats, cache behaviour, and error handling.

Hosts and budgets are discovered from the model registry - nothing is
hardcoded. Per-model token/timeout budgets come from each record's
``max_output_tokens`` / ``default_timeout_seconds``. Reasoning models
that think before answering (long timeout records) automatically get a
trimmed reliability sweep so the run stays tractable. Anti-loop
sampling knobs (e.g. presence_penalty) ride in on the registry record's
``sampling`` block through the router - no per-call override needed.

Run from project root:
  python -m tools.smoke_test_lan_llms
  python -m tools.smoke_test_lan_llms --models my-model-id,other-id
  python -m tools.smoke_test_lan_llms --concurrent my-model-id

Concurrency is opt-in per model (``--concurrent``): LM Studio is
effectively single-stream for chat completions (concurrent dispatch
returns HTTP 500 by design), while llama.cpp servers handle parallel
requests fine - only you know which is behind each endpoint.

Exit 0 if every check passes, 1 if any fail.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

# Providers whose records point at a self-hosted HTTP endpoint.
LOCAL_PROVIDERS = ("lmstudio", "ollama")

SIMPLE_PROMPT = "Reply with only the single word: OK"
LONGER_PROMPT = (
    "Classify the sentiment of this paragraph as positive, negative, or "
    "neutral, and reply with that single word only. Paragraph: "
    "The sun was setting over the quiet valley. Birds began their evening "
    "chorus. A gentle breeze rustled the leaves of the old oak tree. "
    "Everything felt still and at peace, the kind of moment one wishes "
    "could last forever."
)
RELIABILITY_N = 5

# Records with a long default timeout are reasoning-class models that can
# take minutes per call - trim their reliability sweep so the run stays
# tractable.
SLOW_MODEL_TIMEOUT_THRESHOLD_S = 600
SLOW_MODEL_RELIABILITY_N = 2

# Cap the per-call smoke budget: health probes don't need a brief-sized
# token allowance, but reasoning models need >= 4096 so the think trace
# doesn't starve the answer.
SMOKE_MAX_TOKENS_CAP = 4096
SMOKE_TIMEOUT_CAP_S = 600


def load_local_models() -> list[dict]:
    """Return registry records for every self-hosted (endpoint-backed) model."""
    from model_store import get_store

    store = get_store()
    records = []
    for rec in store.list_models():
        if rec.get("provider") in LOCAL_PROVIDERS and (rec.get("endpoint") or "").strip():
            records.append(rec)
    return records


def health_url_for(rec: dict) -> str:
    """Derive the models-listing health URL from a registry record's endpoint."""
    endpoint = rec["endpoint"].rstrip("/")
    if rec["provider"] == "ollama":
        return endpoint + "/api/tags"
    # lmstudio / llama.cpp / vLLM - OpenAI-compatible Chat Completions shape.
    return endpoint + "/models"


def budget_for(rec: dict) -> tuple[int, int]:
    max_tokens = min(int(rec.get("max_output_tokens") or 500), SMOKE_MAX_TOKENS_CAP)
    timeout_s = min(int(rec.get("default_timeout_seconds") or 180), SMOKE_TIMEOUT_CAP_S)
    return max_tokens, timeout_s


def reliability_for(rec: dict) -> int:
    if int(rec.get("default_timeout_seconds") or 0) >= SLOW_MODEL_TIMEOUT_THRESHOLD_S:
        return SLOW_MODEL_RELIABILITY_N
    return RELIABILITY_N


@dataclass
class CallResult:
    label: str
    ok: bool
    latency_s: float
    text_excerpt: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""


def fmt_ms(seconds: float) -> str:
    return f"{seconds * 1000:>6.0f}ms"


def latency_stats(latencies: list[float]) -> str:
    if not latencies:
        return "no data"
    s = sorted(latencies)
    n = len(s)
    p50 = s[n // 2]
    p95 = s[min(int(n * 0.95), n - 1)]
    return f"n={n} min={fmt_ms(min(s)).strip()} p50={fmt_ms(p50).strip()} p95={fmt_ms(p95).strip()} max={fmt_ms(max(s)).strip()}"


def hr(title: str) -> None:
    print(f"\n════ {title} ════")


def print_results(results: list[CallResult]) -> None:
    for r in results:
        status = "OK " if r.ok else "FAIL"
        cost_str = f" ${r.cost_usd:.4f}" if r.cost_usd else ""
        tok_str = (
            f" {r.input_tokens}->{r.output_tokens}tok"
            if r.input_tokens or r.output_tokens
            else ""
        )
        excerpt = (r.text_excerpt or r.error or "").replace("\n", " ")
        if len(excerpt) > 140:
            excerpt = excerpt[:137] + "..."
        print(f"  [{status}] {r.label}: {fmt_ms(r.latency_s)}{cost_str}{tok_str}  {excerpt}")


def test_endpoint_health(label: str, url: str, iterations: int) -> list[CallResult]:
    out: list[CallResult] = []
    for i in range(iterations):
        start = time.perf_counter()
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode("utf-8")
                data = json.loads(body)
                models = data.get("data") or data.get("models") or []
            latency = time.perf_counter() - start
            out.append(
                CallResult(
                    label=f"{label} GET #{i + 1}",
                    ok=True,
                    latency_s=latency,
                    text_excerpt=f"{len(models)} model(s)",
                )
            )
        except Exception as exc:
            out.append(
                CallResult(
                    label=f"{label} GET #{i + 1}",
                    ok=False,
                    latency_s=time.perf_counter() - start,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return out


def call_via_router(
    rec: dict,
    prompt: str,
    *,
    use_cache: bool,
    label_suffix: str,
    iteration: int,
) -> CallResult:
    from analyzers.llm_router import call_llm

    model_id = rec["id"]
    max_tokens, timeout_s = budget_for(rec)
    start = time.perf_counter()
    try:
        r = call_llm(
            model_id=model_id,
            prompt=prompt,
            max_tokens=max_tokens,
            timeout_seconds=timeout_s,
            use_cache=use_cache,
            source=f"smoke_test{label_suffix}",
        )
        latency = time.perf_counter() - start
        text = (r.text or "").strip().replace("\n", " ")
        excerpt = text[:90] + "..." if len(text) > 90 else text
        return CallResult(
            label=f"{model_id} #{iteration}{label_suffix}",
            ok=True,
            latency_s=latency,
            text_excerpt=excerpt,
            cost_usd=r.cost_usd or 0.0,
            input_tokens=r.input_tokens or 0,
            output_tokens=r.output_tokens or 0,
        )
    except Exception as exc:
        return CallResult(
            label=f"{model_id} #{iteration}{label_suffix}",
            ok=False,
            latency_s=time.perf_counter() - start,
            error=f"{type(exc).__name__}: {exc}",
        )


def test_cache_round_trip(rec: dict) -> tuple[CallResult, CallResult]:
    """Same prompt twice with cache enabled - the second must be a cache hit."""
    from analyzers.llm_router import call_llm

    model_id = rec["id"]
    max_tokens, timeout_s = budget_for(rec)
    nonce = time.time_ns()
    prompt = f"Probe-{nonce}: respond with the single word 'pong'."

    start = time.perf_counter()
    try:
        r1 = call_llm(
            model_id=model_id,
            prompt=prompt,
            max_tokens=max_tokens,
            timeout_seconds=timeout_s,
            use_cache=True,
            source="smoke_test_cache_miss",
        )
    except Exception as exc:
        l1 = time.perf_counter() - start
        miss = CallResult(
            label=f"{model_id} cache miss (warmed)",
            ok=False,
            latency_s=l1,
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        skip = CallResult(
            label=f"{model_id} cache HIT skipped",
            ok=False,
            latency_s=0.0,
            error="skipped - miss phase failed",
        )
        return miss, skip
    l1 = time.perf_counter() - start

    start = time.perf_counter()
    try:
        r2 = call_llm(
            model_id=model_id,
            prompt=prompt,
            max_tokens=max_tokens,
            timeout_seconds=timeout_s,
            use_cache=True,
            source="smoke_test_cache_hit",
        )
    except Exception as exc:
        l2 = time.perf_counter() - start
        return (
            CallResult(
                label=f"{model_id} cache miss (warmed)",
                ok=True,
                latency_s=l1,
                text_excerpt=(r1.text or "").strip()[:60],
            ),
            CallResult(
                label=f"{model_id} cache HIT",
                ok=False,
                latency_s=l2,
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            ),
        )
    l2 = time.perf_counter() - start

    cache_hit = l2 < 0.05 and r1.text == r2.text
    return (
        CallResult(
            label=f"{model_id} cache miss (warmed)",
            ok=True,
            latency_s=l1,
            text_excerpt=(r1.text or "").strip()[:60],
        ),
        CallResult(
            label=f"{model_id} cache HIT" if cache_hit else f"{model_id} cache HIT FAILED",
            ok=cache_hit,
            latency_s=l2,
            text_excerpt=(r2.text or "").strip()[:60],
            error="" if cache_hit else "second call did not hit cache",
        ),
    )


def test_unknown_model() -> CallResult:
    from analyzers.llm_router import call_llm, LLMRouterError

    start = time.perf_counter()
    try:
        call_llm(
            model_id="nonexistent-model-xyz-42",
            prompt="test",
            max_tokens=10,
            use_cache=False,
        )
        return CallResult(
            label="unknown_model rejection",
            ok=False,
            latency_s=time.perf_counter() - start,
            error="no exception raised - should have been LLMRouterError",
        )
    except LLMRouterError as exc:
        return CallResult(
            label="unknown_model rejection",
            ok=True,
            latency_s=time.perf_counter() - start,
            text_excerpt=f"correctly raised LLMRouterError: {str(exc)[:60]}",
        )
    except Exception as exc:
        return CallResult(
            label="unknown_model rejection",
            ok=False,
            latency_s=time.perf_counter() - start,
            error=f"wrong exception type: {type(exc).__name__}: {exc}",
        )


def test_concurrent_dispatch(records: list[dict], n_per_model: int) -> list[CallResult]:
    def one(idx: int, rec: dict) -> CallResult:
        return call_via_router(
            rec,
            prompt=f"Concurrent probe {idx}: respond 'ack'.",
            use_cache=False,
            label_suffix=" concurrent",
            iteration=idx,
        )

    jobs = [(i, rec) for rec in records for i in range(n_per_model)]
    out: list[CallResult] = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futures = [ex.submit(one, idx, rec) for idx, rec in jobs]
        for f in as_completed(futures):
            out.append(f.result())
    return out


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke-test every self-hosted model in the registry."
    )
    parser.add_argument(
        "--models",
        default="",
        help="Comma-separated model ids to test (default: every lmstudio/ollama record with an endpoint).",
    )
    parser.add_argument(
        "--concurrent",
        default="",
        help=(
            "Comma-separated model ids that are safe to hit concurrently "
            "(llama.cpp: yes; LM Studio: no - it is single-stream by design). "
            "Default: skip the concurrency phase."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    overall_start = time.perf_counter()
    all_calls: list[CallResult] = []

    records = load_local_models()
    if args.models:
        wanted = {m.strip() for m in args.models.split(",") if m.strip()}
        records = [r for r in records if r["id"] in wanted]
        missing = wanted - {r["id"] for r in records}
        if missing:
            print(f"[x] Not in registry (or not a local-provider record): {', '.join(sorted(missing))}")
            return 1
    if not records:
        print(
            "[x] No self-hosted models found in the registry. Add a model with "
            "provider lmstudio or ollama (Settings -> Models, or models/<id>.yaml) "
            "and point its endpoint at your server."
        )
        return 1

    concurrent_ids = {m.strip() for m in args.concurrent.split(",") if m.strip()}

    print("Models under test:")
    for rec in records:
        print(f"  - {rec['id']} ({rec['provider']}) -> {rec['endpoint']}")

    hr("Phase 1: Endpoint health (5 GETs each)")
    seen_urls = {}
    for rec in records:
        url = health_url_for(rec)
        if url in seen_urls:
            continue
        seen_urls[url] = rec["id"]
        label = url
        results = test_endpoint_health(label, url, iterations=5)
        all_calls.extend(results)
        print(f"\n[{label}]")
        print_results(results)
        oks = [r.latency_s for r in results if r.ok]
        print(f"  -> {latency_stats(oks)}")

    hr("Phase 2: Registry resolution (1 simple call per model)")
    for rec in records:
        r = call_via_router(rec, SIMPLE_PROMPT, use_cache=False, label_suffix=" warmup", iteration=1)
        all_calls.append(r)
        print_results([r])

    hr(f"Phase 3: Reliability (up to N={RELIABILITY_N} per model, cache off)")
    for rec in records:
        run = [
            call_via_router(rec, SIMPLE_PROMPT, use_cache=False, label_suffix=" reliability", iteration=i + 1)
            for i in range(reliability_for(rec))
        ]
        all_calls.extend(run)
        print(f"\n[{rec['id']}]")
        print_results(run)
        oks = [r.latency_s for r in run if r.ok]
        print(f"  -> {latency_stats(oks)}")

    hr("Phase 4: Longer prompt (per model)")
    for rec in records:
        r = call_via_router(rec, LONGER_PROMPT, use_cache=False, label_suffix=" longer", iteration=1)
        all_calls.append(r)
        print_results([r])

    hr("Phase 5: Cache miss -> cache hit verification (per model)")
    for rec in records:
        miss, hit = test_cache_round_trip(rec)
        all_calls.extend([miss, hit])
        print_results([miss, hit])

    hr("Phase 6: Error handling - unknown model must raise")
    r = test_unknown_model()
    all_calls.append(r)
    print_results([r])

    hr("Phase 7: Concurrent dispatch (opt-in via --concurrent)")
    concurrent_records = [rec for rec in records if rec["id"] in concurrent_ids]
    skipped = [rec["id"] for rec in records if rec["id"] not in concurrent_ids]
    if skipped:
        print(f"  Skipping (not opted in via --concurrent): {', '.join(skipped)}")
    if not concurrent_records:
        print("  No models opted in - skipping phase.")
        results = []
    else:
        results = test_concurrent_dispatch(concurrent_records, n_per_model=3)
    all_calls.extend(results)
    print_results(results)
    oks = [r.latency_s for r in results if r.ok]
    if oks:
        print(f"  -> {latency_stats(oks)}")

    hr("SUMMARY")
    total = len(all_calls)
    passed = sum(1 for r in all_calls if r.ok)
    failed = total - passed
    print(f"Total checks:     {total}")
    print(f"Passed:           {passed}")
    print(f"Failed:           {failed}")
    print(f"Success rate:     {passed * 100 / total:.1f}%")
    print(f"Total runtime:    {time.perf_counter() - overall_start:.1f}s")
    if failed:
        print("\nFailures:")
        for r in all_calls:
            if not r.ok:
                print(f"  - {r.label}: {r.error}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

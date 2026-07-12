# SpeakesQuery - Developer Guide

## What This Is

Local-first search and ingestion engine (v1.0.0-rc1 - see VERSION). Custom query language (SPQL) over Parquet/SQLite via DuckDB. Flask + PyWebView desktop app. Zero cloud dependency, no telemetry.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.14 (3.12 - 3.14 supported for local dev; Docker image is python:3.14-slim) |
| Query Parser | ANTLR4 (`lexers/speakesQuery.g4` -> generated `lexers/antlr4_active/`) |
| Data | pandas, pyarrow, DuckDB (Parquet predicate/projection pushdown) |
| Storage | Parquet (gzip-compressed) + SQLite |
| Web | Flask 3.1, Bulma 0.9.4 SPA (`desktop_app/ui.html`) |
| Desktop | PyWebView (`desktop_app/main.py`) |
| Scheduling | APScheduler 3.x, croniter |
| Sandbox | RestrictedPython (allowlisted modules only) |
| Encryption | Fernet (cryptography) for credential vault |
| Email | aiosmtplib + STARTTLS |
| AI (optional) | Anthropic Claude API (lazy-imported, budget-gated) |
| Testing | pytest, Playwright (Chromium), flake8, bandit |

## Project Layout

```
desktop_app/          Flask server (server.py) + SPA (ui.html) + PyWebView (main.py)
  vendor/                 Vendored third-party frontend assets (marked, CodeMirror, Monaco, Vega) served at /vendor/ - NEVER load a frontend lib from a CDN (W10 2026-07-12; pinned versions + SHA-256 in vendor/MANIFEST.md; drift-guarded by tests/test_vendored_assets.py; notebook HTML exports inline the Vega bundles so standalone files render offline)
  access_gate.py          W11b access-token gate (2026-07-12, the Jupyter model). Auto-active when the bind is non-loopback (= every Docker install since compose sets HOST=0.0.0.0); SPEAKESQUERY_AUTH=on/off overrides. Token at ~/.speakes-query/access_token (0600); install.sh generates it and prints the ?token= URL. GET /healthz is the ONLY exempt path (Dockerfile HEALTHCHECK + install.sh readiness probe target it - probing / would 401). Tests: tests/test_access_gate.py
query_engine/         Scheduled search execution, alerts, history
  CmdExecutionBackend.py   Query parser + ANTLR4 listener (entry point)
  QueryEngine.py           APScheduler cron executor
  Alert.py                 Async email delivery
handlers/             Pipe directive handlers (eval, stats, search, rex, etc.)
lexers/               ANTLR4 grammar + generated parser/lexer
  speakesQuery.g4            Grammar definition (source of truth)
  antlr4_active/           Generated code (do not hand-edit)
  grammar_vocab.py         Extracts commands/functions/keywords from speakesQuery.g4 for the console autocomplete + `/api/grammar/vocab`
scheduled_input_engine/   Data ingestion pipeline
  engine.py               BackgroundScheduler (4-worker ThreadPool)
  executor.py             RestrictedPython sandbox + code execution
  parquet_writer.py       Atomic gzip-compressed writes
  credentials.py          Fernet-encrypted credential vault
  store.py                SQLite CRUD for ingestion tasks
script_library/       131 premade ingestion scripts (JSON metadata + code, 21 are unrestricted trust tier; notable additions: the OEB Wave 1/2 suite incl. oeb_pick_tracker_pro; github_trending_repos (Slice B, 2026-06-23) - scrapes github.com/trending for the hot-repos AG, first script feeding a local-model alert group; ai_papers_github_lists (Slice C1) + ai_papers_huggingface (Slice C2) - AI-paper tracking cross-deduped by arxiv id via the ai_papers/*/* feeder glob)
  scripts/                One JSON file per script; `_pro` suffix = unrestricted trust tier
analyzers/            Claude API wrapper + post-processor
  claude_client.py        Single retry/timeout/cost-logging wrapper around `anthropic.messages.create` - ALL Claude calls route through `call_messages_create()` (alert groups, analyzer, settings-test button, batch submissions). `use_headroom=` selects the Anthropic `base_url` (Headroom proxy vs direct) per call and FAILS OPEN to direct on a connection/timeout/502-504 error; route recorded in `ClaudeCallResult.path` + the `headroom_path` log column
  headroom.py             Headroom proxy routing decision (2026-06-23). `resolve_use_headroom(alert_override, group_override)` = tri-state precedence (per-alert → per-AG → `global_use_headroom_default`, default False) with the `HEADROOM_DISABLE` env kill switch winning over all. `resolve_proxy_url()` = env `HEADROOM_PROXY_URL` → `headroom_proxy_url` setting → `http://localhost:8787`. Headroom is a drop-in Anthropic Messages-API compression proxy (passthrough today); only the Claude path uses it (a `model_id`-set local AG doesn't). See docs/lang/12_alert_groups.md.
  claude_history_store.py Dedicated SQLite (`claude_api_history.sqlite` at project root) capturing full gzipped request + response for every Claude call; lives OUTSIDE `indexes/` so cleanup never touches it
  claude_analyzer.py      Optional per-scheduled-search Claude post-processor (routes through claude_client)
  batch_poller.py         Background poller for Claude batch API results
  llm_history_store.py    Phase 2 / Bet 3 slice 3 (2026-05-08): provider-agnostic SQLite history + content-hash cache for every `analyzers.llm_router.call_llm()` invocation. Lives at `<project_root>/llm_call_history.sqlite` (NOT under `indexes/` - cleanup-budget eviction never touches it; cache hits cost real money). Schema captures `(request_id, content_hash, model_id, provider, model_name, source, status, prompt_gz, system_gz, response_text_gz, raw_response_gz, input_tokens, output_tokens, cost_usd, latency_ms, error_class, error_message, triggered_at_epoch)`. `compute_content_hash(model_id, model_name, provider, prompt, system, max_tokens)` keys cache lookups; including `model_name` means a registry edit that swaps the underlying model invalidates the cache automatically. `get_cached_response(content_hash, max_age_seconds=None)` returns most-recent SUCCESS row only - errored calls never serve. Coexists with `claude_history_store.py` at a different abstraction layer (this is application-uniform; that's Anthropic-SDK-detail).
  llm_router.py           Phase 2 / Bet 3 slice 2 + 2.5 + 3 (2026-05-08): single dispatcher for every LLM call. Looks up `model_id` in `model_store`, picks the provider transport, returns a uniform `LLMResponse`. Anthropic delegates to existing `claude_client.call_messages_create()` (preserves retry / daily-budget / history capture / secret scrubbing). LM Studio uses Chat Completions HTTP transport via `_call_chat_completions` - endpoint always required from registry; no cloud fallback (slice 2.5 removed OpenAI per user direction). Future similar self-hosted backends (vLLM, llama.cpp server) reuse the same transport. Ollama uses its own `/api/chat` endpoint with `prompt_eval_count` / `eval_count` token accounting. Gemini stub raises `LLMRouterError(ProviderNotImplemented)` until SDK demand surfaces. Blocking + sequential per Phase 2 design choice. API keys for non-Anthropic providers live in the credential vault under `script_id=-1` (matching the analyzer-key convention). LM Studio API key OPTIONAL - sends Authorization header only when the vault has a `LMSTUDIO_API_KEY`. **Slice 3** added automatic history capture (every dispatch records to `llm_call_history.sqlite` via `llm_history_store`) + opt-in cache (`call_llm(use_cache=True, cache_max_age_seconds=None)` checks for a matching content_hash before dispatching; cache hits return `cost_usd=0.0` and `latency_ms=0`). Slice 4+ wires this into `| llm` SPQL pipes - idempotent re-runs become free, the structural unlock for cost-cascade economics.
  embedder.py             Phase 1 semantic primitive (Bet 2): lazy-loaded sentence-transformers wrapper. Singleton `get_embedder()` returns a thread-safe instance whose `.encode()` / `.encode_batch()` emit L2-normalized float32 vectors; helpers `cosine_similarity()` and `cosine_similarity_matrix()` power the planned `| nearest` and `| dedup_semantic` SPQL pipes. Default model `sentence-transformers/all-MiniLM-L6-v2` (384 dims, ~80 MB, MIT). Sentence-transformers is a hard dep but lazy-imported so the app boots cleanly on hosts that haven't rebuilt yet - `MissingEmbeddingSDKError` carries an actionable install message.
alert_groups/         Multi-search Claude API dispatch
  serializer.py           Result loading, row capping, token estimation
  builder.py              Prompt template injection + block rendering
  dispatcher.py           Orchestrates: serialize → Claude (or the local LLM router when the AG sets `model_id`, Slice A 2026-06-23) → email → log; emits failure-alert email on error; `delivery_mode: prompt_only` forks to email-the-prompt path (no Claude call, $0 cost) for budget-friendly runs
  scheduler.py            APScheduler job registration
  feeder_status.py        Per-feeder health resolver: saved search → index → library script → scheduled task → creds → data
functionality/        Shared infra
  log_writer.py           Thread-safe buffered Parquet log emitter for `indexes/logs/<category>/` (schemas in SCHEMAS dict)
  atomic_write.py         Atomic text/bytes file writes (POSIX rename, EBUSY fallback)
  ParquetEpochAdder.py    Backfill `_epoch` for legacy Parquet
  embedding_sidecar.py    Phase 1 slice 2: per-source-parquet sidecar storage (`<source>.embeddings.parquet`). Schema `(_row_id INT64, _epoch INT64, embedding FIXED_SIZE_LIST<float, dim>)` plus parquet key-value metadata (`model_name`, `dim`, `created_epoch`). Atomic write via `.<name>.tmp` → `os.replace`. `read_sidecar()`, `write_sidecar()`, `is_stale()`, `sidecar_path_for()` are the public surface. `dim` is parameterized per write so a future model swap (MiniLM 384 → BGE-base 768) doesn't require schema migration. The slice 3 sweeper drives population; the slice 4 `| nearest` pipe will read these via DuckDB VSS.
  embedding_sweeper.py    Phase 1 slice 3: standalone-callable background pass that walks the indexes tree, finds source parquets whose sidecar is missing or stale, and embeds them in batch. `EmbeddingSweeper(indexes_root).sweep_once()` returns a `SweepReport` with per-source telemetry. Excludes `IMMUTABLE/` + `logs/` subtrees by default. Default text extractor concatenates all string-typed columns. Failures in one source don't stop the sweep - bad files land in `report.failures`. **Slice 5 (2026-05-08)** wires this into the engine: `_schedule_embedding_sweep` registers it on an `IntervalTrigger(minutes=embedding_sweep_interval_minutes)` when `embeddings_enabled=True`, and `tools.embed_backfill` provides a one-shot CLI for initial bootstrap. **Slice 6 (2026-05-08)** adds `handlers/SemanticHandler.py::_try_sidecar_lookup` - a conservative fast path that reuses the sweeper's precomputed embeddings when the input DataFrame's rows align 1:1 with sidecar entries (most often the case immediately after `index=` with no upstream filter), skipping `encode_batch()` entirely. Falls back to embed-on-the-fly on ANY uncertainty (missing sidecar, model swap, row-count mismatch, stale sidecar, `field=` pinned). Slower-but-correct over faster-but-wrong every time.
  duckdb_index_call.py    DuckDB index loading
validation/           Input validators (cron, email, macros, prompts, alert groups)
handlers/             SPQL pipe command implementations
indexes/              Parquet + SQLite data files (user data lives here)
  logs/                 SPQL-queryable Parquet log stream with its own `max_logs_size_gb` budget (default 5 GB, independent of the main indexes budget). Subdirs: config, search_runs, alert_groups, claude_api, ingestion, system. See docs/lang/14_logging.md
claude_api_history.sqlite  Dedicated forensic audit of every Claude call (project root, NOT inside indexes/ - never auto-cleaned, user-managed retention)
lookups/              Reference data for lookup command (CSV/JSON/Parquet/TSV)
macros/               User-defined SPQL macros (YAML)
saved_searches/       Scheduled search configs (YAML, gitignored - user data)
default_saved_searches/   Project-shipped feeder templates for default alert groups (tracked in git; seeded into saved_searches/ on first-run and installable on-demand via Feeder Health)
alert_groups/         Alert group configs (YAML, managed by alert_group_store.py)
boilerplate_prompts/  Prompt templates for alert groups (YAML)
tests/                Test suite (see Testing section)
  _live_harness.py         Helpers for live integration runs: secrets parser + feeder registry
  _live_runner.py          CLI runner for live feeder end-to-end audit (`python -m tests._live_runner`)
  test_live_integration.py Gated `live_integration` pytest: every default feeder + Claude + Gmail
  test_feeder_fixes.py     Regression tests for glob/sandbox/decimal/empty-epoch/SEC-guard fixes
  test_smtp_diagnose.py    Tests for tools/smtp_diagnose.py + /api/email/diagnose
  test_log_writer.py       Tests for log_writer.py (buffer/flush/categories) + cleanup_logs
  test_claude_history.py   Tests for claude_history_store + claude_client wrapper (retry, timeout, logging)
  test_claude_api_endpoints.py  /api/analyzer/test + /api/claude-history endpoint tests
  test_alert_group_robustness.py  Row-cap regression + failure-email gating
  test_sec_edgar_fallback.py    SEC EDGAR scripts tested without creds (default UA) and with user-supplied creds
tools/                Operational utilities shipped *in* the Docker image (unlike tests/)
  smtp_diagnose.py         SMTP diagnostic - `python -m tools.smtp_diagnose --send-to <addr>` or POST `/api/email/diagnose` (same logic, shared code)
  embed_backfill.py        Phase 1 slice 5: one-shot bootstrap to populate `*.embeddings.parquet` sidecars for an existing corpus without waiting for the (default 15-min) sweeper cadence. `python -m tools.embed_backfill [--root <path>] [--cleanup] [--json]`. Same code path as the engine-scheduled sweeper; exit 1 if any source failed, exit 0 on clean sweep.
  persistence.py           User-data snapshot/backup/restore/diff. Stdlib-only. Wired into `./update.sh` (pre-update tarball + post-update regression diff). Subcommands: `snapshot`, `backup`, `restore`, `diff`. See `docs/lang/13_backup_recovery.md`.
  diagnose_alert_group.py  Per-feeder pipeline walk for an AG (deploy/cred/ingest/filter triage)
  schedule_pdf.py          Schedule Operations Report PDF (`/api/schedule/pdf` + `python -m tools.schedule_pdf`) - WeasyPrint render of heatmaps, per-AG feeder health, anomalies (incl. the 2026-07-01 FAILING bucket + ON-DEMAND feeder resolution)
  smoke_test_lan_llms.py   Live smoke test of every self-hosted model in the registry (lmstudio/ollama providers) through the project's own router stack
  ollama_bootstrap.py      Bootstrap helper for an Ollama host (pull models, verify serve)
  rotate_vault_key.py      Rotate the Fernet credential-vault key (re-encrypts every stored credential)
  audit_deployed_task_drift.py  Compare deployed ingestion tasks against their library scripts (drift triage)
secrets.txt           Gitignored; developer-supplied live credentials for `live_integration` tests
global_settings.py    Thread-safe YAML-backed singleton config
saved_search_store.py YAML CRUD + 30-day soft-delete recovery
macro_store.py        Macro CRUD
job_store.py          Query result ring buffer (last 10) + named snapshots
alert_group_store.py  Alert group YAML CRUD + run audit trail
boilerplate_prompt_store.py  Boilerplate prompt YAML CRUD
email_group_store.py  Email group / mailing-list YAML CRUD + `@group_name` resolver hooked into all email-send paths
model_store.py        Phase 2 / Bet 3 slice 1 (2026-05-08): LLM model registry. YAML CRUD over `models/<id>.yaml` records `(id, provider, model_name, endpoint, costs, max_output_tokens, default_timeout_seconds, sampling)`. `sampling` (added 2026-06-07) is an optional allowlisted sampler block (`temperature, top_p, top_k, min_p, presence_penalty, frequency_penalty, repeat_penalty, seed`) forwarded verbatim into the Chat Completions payload by `_call_chat_completions` - pins a reasoning model's recommended sampling so its `<think>` trace self-terminates instead of looping past `max_output_tokens` and returning empty `content` (the Qwen3.5-122B-A10B failure mode; the `llamacpp-qwen35-122b-a10b` default ships `presence_penalty: 1.5`). Validated by `ModelValidation.validate_sampling` (unknown keys + non-numbers rejected at save-time; pinned by `tests/test_model_store.py::TestModelValidation` sampling tests + `test_shipped_122b_default_pins_anti_loop_sampling`). Default templates ship in `default_models/` (tracked in git, RO-mounted in Docker), seeded missing-only into `models/` (gitignored, RW) on first `initialize()` via `_seed_defaults()` - never overwrites user edits. Provider enum: `anthropic | ollama | gemini | lmstudio` (slice 1.5 added `lmstudio` for self-hosted LLM servers; slice 2.5 removed `openai` per user direction - SpeakesQuery does not interact with OpenAI's company or servers as a matter of principle). Future similar self-hosted backends (vLLM, llama.cpp server) are one-line additions to ALLOWED_PROVIDERS. `PROVIDERS_REQUIRING_ENDPOINT={ollama, lmstudio}` enforces non-empty endpoint at save-time so config errors surface immediately. Slice 2 builds `analyzers/llm_router.py` on top to dispatch by id; slices 4+ wire it into `| llm` / `| llm_batch` SPQL pipes.
notebook_to_alert_group.py  Phase 3 / Bet 4 slice 9 (2026-05-09): notebook → AG converter for the `promote_to_alert_group` headliner cell. Three pure functions + one round-trip helper: `extract_ag_payload(notebook, cell_id)` (pure transform, no I/O), `build_promote_preview(notebook, cell_id)` (engine-side dry-run; reads AG + saved-search stores; never writes), `promote_cell_to_ag(notebook, cell_id, *, overwrite_existing=True)` (the ONLY function in this module that mutates AG state - calls `AlertGroupStore.save_group` / `update_group`), and `alert_group_to_notebook(ag)` (round-trip the other direction; pure function). The notebook engine handler `_execute_promote_to_alert_group` ALWAYS calls `build_promote_preview` (dry-run); actual deploy goes through the explicit `POST /api/notebooks/<id>/promote/<cell_id>` endpoint which calls `promote_cell_to_ag`. The split is the **config-leak canary** boundary - pinned by `tests/test_notebook_slice9_promote.py::TestConfigLeakCanary` (patches both AG mutating methods with `AssertionError("CONFIG LEAK")` and runs a notebook with a promote cell; both must stay zero on the engine path).
schedule_visualization.py  Cron expansion + log-history aggregator that powers the Schedule page heatmap (`/api/schedule/heatmap`)
email_groups/         Email-group YAML configs (gitignored - user data)
```

## Query Language (SPQL)

Pipe-delimited commands: `index="path" | search field="val" | stats count by category | sort -count | head 10`

**Supported commands:** search, where, eval, stats, eventstats, streamstats, timechart, fields, table, rename, sort, reverse, head, limit, dedup, dedup_semantic, llm, llm_batch, llm_route, llm_refine, llm_ensemble, llm_until, nearest, rex, regex, sql, join, append, appendpipe, switch, lookup, outputlookup, outputnew, coalesce, mvexpand, mvreverse, mvcombine, mvdedup, mvappend, mvfilter, mvcount, mvindex, mvzip, mvjoin, mvdc, mvfind, makeresults, addinfo, spath, base64, bin, multisearch, maketable, fieldsummary, fillnull, loadjob, inputlookup

**Built-in functions:** round, floor, ceil, min, max, avg, sum, abs, sqrt, median, mode, range, random, concat, replace, upper, lower, capitalize, substr, trim, ltrim, rtrim, len, match, split, tonumber, tostring, urlencode, urldecode, defang, fang, type, base64_encode, base64_decode, isnull, isnotnull, coalesce, if_, case, randomize, now, relative_time, strftime, strptime, mvdedup, mvsort, mvcount, mvreverse, mvjoin, mvfind, mvindex, mvdc, mvappend, mvzip

**Grammar parity:** `lexers/speakesQuery.g4` is the source of truth for SPQL syntax. Every function in this list has a corresponding rule in the grammar; `lexers/grammar_vocab.py` parses the `.g4` file and exposes `/api/grammar/vocab` for the console autocomplete. Keep grammar + handlers + this list in sync.

## Script Library

Each script is a JSON file in `script_library/scripts/` with this schema:

```json
{
  "title": "Human-readable name",
  "description": "What it does",
  "category": "Category Name",
  "api_url": "https://api.example.com/endpoint",
  "requires_credentials": [],
  "credential_kinds": {},
  "suggested_cron": "*/30 * * * *",
  "suggested_subdirectory": "category/subcategory",
  "suggested_overwrite": false,
  "trust_level": "sandboxed",
  "support_tier": "core",
  "tags": ["free", "no-auth", "category"],
  "code": "import pandas as pd\nimport requests\n..."
}
```

**Support tiers** (the `support_tier` field - REQUIRED explicit on every script; loader fail-safes a missing value to `example`):
- `core` - documented stable API, maintained with the project (126 of 131)
- `example` - unofficial/fragile endpoint (HTML scrape, undocumented API), use-at-your-own-risk badge in UI. The classification is FROZEN in `tests/test_support_tier.py::EXPECTED_EXAMPLE_TIER` - tier moves update that set + the README "(N core)" count in the same commit. See docs/lang/09_ingestion_etiquette.md "Support Tiers".

**Credential kinds** (optional `credential_kinds` field, maps credential name → kind):
- `api_key` - secret API key (default if field omitted, for back-compat)
- `secret` - non-api-key secret (tokens, passwords)
- `contact` - non-secret contact string required by fair-access policies (e.g. `SEC_EDGAR_CONTACT`)
- `identifier` - non-secret public identifier used as a parameter (e.g. `POLYMARKET_USER_ADDRESS`)

The UI renders different pills, help text, and deploy notifications based on the worst-severity kind, so users don't hunt for an "API key portal" that doesn't exist.

**Trust levels** (the `trust_level` field - default `"sandboxed"`):
- `sandboxed` - RestrictedPython + module allowlist. Use this by default.
- `unrestricted` - plain `compile()`, full `__builtins__`, no import filter. Opt-in per script. Output subdirectory and filename get a `_pro` suffix. See [Ingestion Etiquette: Trust Tiers](docs/lang/09_ingestion_etiquette.md#trust-tiers-sandboxed-vs-unrestricted).

**Requirements for script code (sandboxed tier):**
- Must produce a DataFrame with an `_epoch` column (Unix seconds)
- Must call `GENERATE_RESULTS(df)` to emit output
- Only allowlisted modules: pandas, requests, json, datetime, time, re, math, hashlib, base64, collections, io, bs4, lxml
- No `_`-prefixed names, no tuple unpacking in `for` loops, helpers cannot call other helpers (RestrictedPython quirks)
- No filesystem access, no subprocess, no network beyond allowlisted domains
- Resource budgets auto-scale with cron interval (tighter for frequent runs)

**Requirements for script code (unrestricted / `_pro` tier):**
- Same `_epoch` / `GENERATE_RESULTS` / Parquet contract
- Full Python standard library + `scipy` + `scikit-learn` + `rapidfuzz` + `numpy` + `duckdb` available
- Must emit a **superset** of the sandboxed variant's columns (never drop or rename base columns)
- HTTP count, timeout, and output-row cap are still enforced at the engine layer

## Testing

### Framework & Tiers

All tests run via `pytest -vv` from the virtualenv. YAML-driven parametrized tests organized by tier:

| Tier | Directory | Tests |
|------|-----------|-------|
| 1 | `tests/yaml/tier1_commands/` | Individual SPQL commands |
| 2 | `tests/yaml/tier2_functions/` | Built-in functions |
| 3 | `tests/yaml/tier3_complex/` | Multi-pipe, joins, nested ops |
| 4 | `tests/yaml/tier4_negative/` | Error cases, invalid input |
| 5 | `tests/yaml/tier5_api/` | REST API endpoints (Flask test client) |
| 6 | `tests/yaml/tier6_ui/` | Playwright browser automation |

### YAML Test Format

```yaml
- id: command_001
  title: "descriptive test name"
  query: 'index="indexes/default_test/test0.parquet" | stats count by level'
  expect:
    row_count: 5
    columns: [level, count]
    values:
      - row: 0
        column: level
        value: CRITICAL
    sorted_by: { column: count, order: desc }
```

### Key Test Files

- `test_spql.py` - YAML-driven query execution (tiers 1-4)
- `test_api.py` - REST endpoint tests (tier 5)
- `test_ui.py` / `test_ui_crud.py` - Playwright UI tests (tier 6)
- `test_script_library.py` - Script validation + mock execution (all 131 scripts; two registries - `SCRIPT_REGISTRY` + `CREDENTIALED_SCRIPT_REGISTRY` - plus the dedicated metaculus auth-sentinel class)
- `test_duckdb_index_call.py` - DuckDB predicate pushdown
- `test_relative_time.py` - Time parsing with ±5s tolerance
- `test_claude_analyzer.py` / `test_batch_api.py` / `test_analyzer_storage.py` - Analyzer unit tests

### Fixtures (`tests/conftest.py`)

- `run_query` - Session-scoped query executor returning (DataFrame, job_id)
- `client` - Flask test client (starts scheduled input engine)
- `ui_server` - In-process Flask on port 5199
- `browser_instance` / `page` / `shared_page` - Playwright Chromium fixtures

### Running Tests

```bash
source env/bin/activate
pytest -vv                          # All tests
pytest tests/test_spql.py -vv       # Query tests only
pytest tests/yaml/tier1_commands/   # Specific tier
pytest -m smoke                     # Live API smoke tests (needs network)
pytest -m live_integration          # Live feeder + Claude + SMTP end-to-end (needs secrets.txt)
pytest -m browser                   # Playwright browser tier only (auto-marked from the browser_instance fixture; needs: playwright install chromium)
HEADED=1 pytest tests/test_ui_crud.py  # Visual UI debugging
flake8                              # Lint (.flake8 handles excludes; a CLI --exclude OVERRIDES the config and lints generated code)
bandit -r .                         # Security scan
```

**`secrets.txt` format** for `live_integration` tests (project root, gitignored):

```
[gmail]
your.address@gmail.com
16-char-app-password

[claude]
sk-ant-api03-...

[FRED API Key]
<fred key>
```

Additional sections (`[sec]`, `[tradier]`, etc.) are picked up by the same
loader - the `[FRED API Key]` form is aliased to `fred`, and
`SEC_EDGAR_CONTACT` falls back to the `[gmail]` address if `[sec]` is
absent (SEC accepts any email contact).

### Script Library Tests

Every script in `script_library/scripts/` must have a corresponding entry in the `SCRIPT_REGISTRY` dict in `test_script_library.py`. Tests validate:
- JSON schema (required keys, types, tags)
- Code execution in RestrictedPython sandbox with mock HTTP responses
- Output DataFrame structure (`_epoch` column, expected columns, min row counts)
- Custom assertion callbacks where needed

Mock data factories (`make_gamma_market()`, `make_fred_observations()`, etc.) and a URL router provide deterministic responses without network access.

## Adding a New Feature

Every feature touches code, tests, and documentation. All three are required - a feature without docs is incomplete.

### New SPQL Command

1. Add grammar rule to `lexers/speakesQuery.g4`
2. Regenerate parser: `cd lexers && antlr4 -v 4.13.2 -Dlanguage=Python3 speakesQuery.g4 -o antlr4_active` (antlr4-tools is in requirements-dev.txt; the `-v` pin must match the antlr4-python3-runtime version)
3. Add handler method to appropriate file in `handlers/`
4. Register in `speakesQueryListener._command_map`
5. Add YAML tests in appropriate tier (`tier1_commands/` or `tier2_functions/`)
6. Run `pytest tests/test_spql.py -vv`
7. **Docs:** Add command to `docs/lang/02_commands.md`. Add examples to `docs/lang/05_cookbook.md` if non-trivial. Update the "Supported commands" list in this file's SPQL section.

### New Built-in Function

1. Implement in the appropriate handler file in `handlers/`
2. Add YAML tests in `tests/yaml/tier2_functions/`
3. Run `pytest tests/test_spql.py -vv`
4. **Docs:** Add to `docs/lang/03_functions.md`. Update the "Built-in functions" list in this file's SPQL section.

### New Script Library Entry

1. Create `script_library/scripts/your_script.json` following the schema above
2. Add entry to `SCRIPT_REGISTRY` in `tests/test_script_library.py` with mock data factory and URL router
3. Run `pytest tests/test_script_library.py -vv`
4. **Docs:** Update the script count in this file's Project Layout section if it has drifted.

### New API Endpoint

1. Add route in `desktop_app/server.py`
2. Add YAML test case in `tests/yaml/tier5_api/`
3. Add UI integration in `desktop_app/ui.html` if user-facing
4. Run `pytest tests/test_api.py -vv`
5. **Docs:** Add to `docs/lang/10_api_reference.md`.

### New UI Feature

1. Add HTML/CSS/JS to `desktop_app/ui.html` (single-file SPA)
2. Add Playwright test in `tests/yaml/tier6_ui/` or `tests/test_ui_crud.py`
3. Run `HEADED=1 pytest tests/test_ui_crud.py -vv` to verify visually
4. **Docs:** Update `docs/lang/06_application_guide.md` with usage instructions. Add to `README.md` Features section if it's a major capability.

### New Ingestion/Analyzer/Settings Capability

1. Implement the feature in the relevant module
2. Add appropriate tests
3. **Docs:** Update the relevant doc (`docs/lang/09_ingestion_etiquette.md`, `docs/lang/11_claude_analyzer.md`, or `docs/lang/06_application_guide.md`). Update `README.md` if it changes the project's feature surface.

## Conventions

- **Logging prefixes (stdout):** `[i]` info, `[x]` error, `[!]` warning
- **Structured logs (Parquet):** emit via `functionality.log_writer` helpers - `log_config_change`, `log_search_run`, `log_alert_group_event`, `log_claude_api_call`, `log_ingestion_run`, `log_system_event`. Columns are schema-validated against `SCHEMAS` in `log_writer.py`; unknown columns drop, missing columns land null. See [14_logging.md](docs/lang/14_logging.md).
- **Claude API calls:** every call goes through `analyzers.claude_client.call_messages_create(...)` - never import `anthropic` directly in feature code. The wrapper handles retry, timeout, Parquet cost-log emission, and SQLite history recording in one place.
- **Long-running dispatchers must self-narrate.** Any operation that can block the request thread for > ~5 seconds (alert group runs, Claude calls, multi-feeder loops) emits `[i]` log lines at every phase boundary: phase start, per-item `[N/total]`, phase done (with elapsed ms), pre-external-call (with the knobs - model, timeout, retry count), post-external-call (with latency + tokens + stop reason). Rule: an operator tailing `docker logs -f` must never see a silent gap > 30s on the happy path. Caught on 2026-04-21 when a UI hang at "Dispatching to Claude" had no log line distinguishing "thinking" from "wedged". Pinned by `tests/test_no_jpype_and_dispatch_logging.py::TestDispatcherPhaseLogging`.
- **SPQL pipe handlers must tolerate empty input.** Every handler (`where`, `table`, `sort`, `head`, `stats`, etc.) treats an empty DataFrame as a valid state and returns an empty well-shaped output - never raises. An ingestion legitimately producing zero rows (e.g. no arb opportunities today) writes a Parquet with only `_epoch`; downstream `| where col >= 5` would otherwise raise `UndefinedVariableError`. Companion rule for ingestion scripts: pass explicit `columns=EXPECTED_COLUMNS` to `pd.DataFrame(rows, columns=…)` so empty-day output still carries the schema. Pinned by `tests/test_ag_dispatch_functional_2026_04_21.py::TestEmptyDataFrameShortCircuit`.
- **Don't retry timeouts.** `APITimeoutError` is deliberately NOT in `analyzers.claude_client._is_retryable`: a retry just fires another attempt against the same timeout ceiling. Raise `claude_request_timeout_seconds` (default 600s, ceiling 3600s) instead. Retries are correct for 429 / 5xx / connection errors only.
- **Use `process_query_with_diagnostics()` for callers that need the error reason.** `process_query()` swallows exceptions and returns `(None, None)`; the diagnostic variant returns `(df, job_id, diagnostic_or_none)` where `diagnostic` names the exception class + message or flags `empty:…`. The alert group dispatcher uses this so feeder failures log with the feeder name instead of a misleading "No cached result" from the fallback path. The scheduled-search executor (`query_engine/QueryEngine.py::execute_query`) uses it too (since 2026-07-01) so a quiet day logs `search_runs` `status="empty"` while a real failure logs `status="error"` with the actual diagnostic - the legacy collapse made every always-empty feeder show " - " avg rows in the schedule report, indistinguishable from real breakage (the ESPN payload drift hid in that same bucket for weeks). Pinned by `tests/test_query_engine_run_logging.py`.
- **Log DataFrames by shape, not content.** `logging.info(f"[i] ... {df}")` stringifies the entire DataFrame - fine for dev, catastrophic in production for million-row results (hundreds of MB of heap + log noise). Log the shape (`f"{len(df.index)} rows × {len(df.columns)} cols"`) or a `.head()` sample instead. Caught in the 2026-04-21 audit.
- **Downgrade hot-path INFO to DEBUG.** A log emitted once per pipe per feeder will flood production INFO streams (10 feeders × 5 pipes × 2 messages = 100 lines/dispatch). Emit intermediate diagnostics at DEBUG so they're still accessible via `--log-level=DEBUG` but don't dominate the day-to-day operator view. Keep INFO for phase-boundary events that actually help diagnose stuck dispatches.
- **Share stateful singletons across hot loops.** The AG dispatcher's feeder loop reuses one `SavedSearchStore` instance (class-level `_ss_store_shared`) for all 10 feeders rather than re-initialising per feeder. Tests that inject fakes call `AlertGroupDispatcher._reset_ss_store_cache()` to drop the cache before patching.
- **Never swallow audit-log failures silently.** `except Exception: pass` on a credential-vault mutation log or a config-change log is an anti-pattern - an attacker who gains code execution could exfiltrate credentials and leave no audit trace. Always `logger.warning(...)` so the failure is visible even if the primary operation (the mutation) succeeded.
- **Regex-redact `sk-ant-*` tokens in anything logged verbatim.** `analyzers.claude_client._scrub_secrets` runs on every `messages.create` request/response body before it lands in `claude_api_history.sqlite`, to catch the rare case where an operator pastes a real API key into a prompt. Extend the `_SECRET_PATTERNS` tuple when adding support for new secret-shaped credentials.
- **No em dashes, anywhere, ever.** Not in code comments, docs, YAML descriptions, prompts, log strings, commit messages, or generated PDFs. Use a plain hyphen with spaces (" - "), a comma, a colon, or restructure the sentence. This is a hard authorial-voice rule (user direction 2026-07-11); a repo-wide purge removed every em dash character, its backslash-u-2014 escape form in string literals, and every mdash HTML entity. Do not reintroduce any of them.
- **Naming:** snake_case functions/variables, CamelCase classes, UPPER_CASE constants
- **Config stores:** `*_store.py` for persistence, YAML-backed with CRUD pattern
- **Singletons:** `_instance` + `get_*()` factory (thread-safe where needed)
- **Error responses:** JSON `{"status": "error", "message": "..."}`, never expose tracebacks
- **Atomic writes:** all persistent state files (YAML stores, JSON indexes, settings, Parquet) stage to a sibling `.tmp` and `os.replace` into place. For text/bytes use `from functionality.atomic_write import write_text_atomic, write_bytes_atomic`; Parquet uses `ParquetWriter.write_atomic()`. Never `open(path, "w")` directly for files that survive a crash.
- **Security:** RestrictedPython sandbox, Fernet credential vault, input validation at boundaries, `allowed_api_domains` regex allowlist
- **Threading:** Lock-based synchronization, daemon threads for background work
- **Frontend:** Vanilla JS, no framework. `escHtml()`/`escAttr()` for XSS prevention. CSS custom properties for 4-theme support (Light, Dark, Night, Cyber)

## Configuration

- `global_settings.yaml` - User overrides (gitignored)
- `global_settings.defaults.yaml` - Reference defaults
- `.env` - Environment variables (SMTP, API keys)
- `~/.speakes-query/` - Credential encryption keys (outside repo)

## Documentation

### Structure

```
README.md                         Project overview, quick start, features, Docker, high-level architecture
CLAUDE.md                         This file - developer guide, conventions, checklists (see below)
CHANGELOG.md                      Version history (update with every release)
ROADMAP.md                        Strategic priorities + phased implementation plan (~24 months) - authoritative
docs/lang/
  01_fundamentals.md              SPQL basics, index clause, pipes, data types
  02_commands.md                  All SPQL commands with syntax and examples
  03_functions.md                 All built-in functions with signatures and examples
  04_advanced.md                  Complex patterns, joins, multi-value fields
  05_cookbook.md                   Practical recipes and worked examples
  06_application_guide.md         UI walkthrough, page-by-page usage
  07_email_setup.md               SMTP configuration, Gmail App Passwords
  08_macros.md                    Macro definition, parameters, expansion
  09_ingestion_etiquette.md       Script writing rules, sandbox constraints, best practices
  10_api_reference.md             REST endpoint catalog
  11_claude_analyzer.md           Analyzer setup, prompts, budget, batch mode, Test Claude button, history store
  12_alert_groups.md              Alert groups: multi-search dispatch, boilerplate prompts, failure-alert emails
  13_backup_recovery.md           What to back up, restore recipes, recovery scenarios, hygiene
  14_logging.md                   Logs index (indexes/logs/*) schemas, budget, SPQL recipes, cost alerting
  15_options_edge_brief.md        Options Edge Brief: signal classes, three-tier learner format, pick journal, wave roadmap
  16_immutable_data_namespace.md  indexes/IMMUTABLE/* protected tree - never garbage-collected, schema-additive forever
  17_semantic_search.md           `| nearest` + `| dedup_semantic` SPQL pipes (Phase 1 / Bet 2 slice 4): user-facing reference for semantic search backed by all-MiniLM-L6-v2
  18_llm_pipes.md                 `| llm` SPQL pipe (Phase 2 / Bet 3 slice 4): user-facing reference covering registry-driven dispatch, cost-cascade examples, cache semantics, error handling, model selection
  19_notebooks.md                 Notebook mode (Phase 3 / Bet 4): cell types, reactive cache, `promote_to_alert_group` headliner cell + dev→prod deploy loop, round-trip API, schema reference
  20_visual_builder.md            Visual Builder (Phase 4 / Bet 4.1): drag-drop SPQL canvas, palette, text↔visual round-trip, starter templates
```

### When to Update Docs

| Change | Update |
|--------|--------|
| New/modified SPQL command | `02_commands.md`, `05_cookbook.md` if non-trivial |
| New/modified function | `03_functions.md` |
| New API endpoint | `10_api_reference.md` |
| UI change | `06_application_guide.md` |
| New ingestion capability | `09_ingestion_etiquette.md` |
| Analyzer change | `11_claude_analyzer.md` |
| Alert group change | `12_alert_groups.md` |
| Email/SMTP change | `07_email_setup.md` |
| Macro system change | `08_macros.md` |
| New persistent state file (DB/YAML) or change to install/Docker volume mounts | `13_backup_recovery.md` |
| Major new feature | `README.md` Features section |
| Release | `CHANGELOG.md`, `VERSION` |
| Strategic pivot, phase completion, or checkpoint outcome | `ROADMAP.md` (retrospective section, risk register, checkpoint result) |

### README.md

The README is the public face of the project. Update it when:
- A new user-visible capability is added (new section or bullet under Features)
- The quick start flow changes (install steps, prerequisites)
- Docker or deployment instructions change
- The tech stack changes materially (new major dependency)

Do not bloat the README with internal details - keep it oriented toward someone evaluating or installing the project for the first time.

### Keeping Docs in Sync

Docs are served in-app via the Help page (`/api/docs/<filename>`), so stale docs directly hurt users. Treat a doc update as part of the definition of done - code + tests + docs = complete.

If you notice a doc is stale while working on an unrelated task, fix it in the same commit or flag it. Don't let drift accumulate.

## Maintaining This File (CLAUDE.md)

This file is the authoritative developer reference. It must stay accurate as the project evolves. Update it when:

- **Commands or functions are added/removed** - update the SPQL section's command and function lists
- **Project layout changes** - new top-level directories, renamed modules, moved files
- **Tech stack changes** - new major dependency added or removed from `requirements.txt`
- **Testing patterns change** - new tier added, new fixture, changed test conventions
- **Script library schema changes** - new required/optional JSON keys
- **Conventions change** - new naming pattern, new logging prefix, new error handling approach
- **New feature workflow emerges** - add a checklist under "Adding a New Feature"
- **A "Do Not" is discovered** - add it to prevent repeat mistakes

When updating, keep it concise. This file should remain under 300 lines. If a section grows too large, move the detail to the appropriate `docs/lang/` file and reference it here.

**Do not duplicate** content that belongs in `docs/lang/` - this file is a map and checklist, not the full reference. Point to the right doc instead of reproducing it.

## Do Not

- Remove, bypass, or opt new test files out of the `preserve_user_state` session guard in `tests/conftest.py`. The suite exercises real endpoints against the real stores, so on a developer/user machine a test can clobber LIVE config - caught 2026-07-11 when the UI settings-reset test wiped the operator's `global_settings.yaml` to `{}` during the post-3.14 verification pass (recovered only because the config log stream happened to capture every key's old value). The guard snapshots user-state files (settings yaml, sqlite stores, `alert_groups/` / `saved_searches/` / `lookups/` / `macros/` / `models/` / `notebooks/` / prompt dirs) before the session and restores them after, deleting test-created strays. When adding a NEW user-data file or directory, add it to `_USER_STATE_FILES` / `_USER_STATE_DIRS` in the same commit (same rule as the persistence/docker/install triple). Prefer `tmp_path`-isolated stores for new tests regardless - the guard is a safety net, not an isolation mechanism.
- Hand-edit files in `lexers/antlr4_active/` (generated from grammar)
- Add `openai` back to `validation/ModelValidation.py::ALLOWED_PROVIDERS` or to any router transport. SpeakesQuery does not interact with OpenAI's company or servers as a matter of principle (user direction 2026-05-08, slice 2.5). The `_call_chat_completions` HTTP transport stays - LM Studio (and any future independent self-hosted backend like vLLM, llama.cpp server) uses the same JSON wire shape, which is industry-standard among self-hosted LLM servers. But no provider entry, default template, API-key slot, code path, test, or doc reference may point at OpenAI's cloud. Pinned by `tests/test_model_store.py::TestModelValidation::test_openai_provider_is_rejected`.
- Store secrets in config files (use credential vault or .env)
- Add external network calls without `allowed_api_domains` validation
- Skip the test gate for ingestion scripts (RestrictedPython compilation + `_epoch` check)
- Break the atomic write pattern for Parquet output
- Ship a feature without updating the relevant `docs/lang/` file
- Let `README.md` fall out of sync with major capability changes
- Add implementation detail to this file that belongs in `docs/lang/` - link instead
- Add a new user-data directory or root-level state file without ALSO adding it to (a) `tools/persistence.py`'s `DIR_TARGETS_HASHED` / `FILE_TARGETS`, (b) the bind-mount list in `desktop_app/docker-compose.yml`, and (c) the `mkdir -p` block in `install.sh`. The drift-guard tests in `tests/test_persistence.py` enforce all three. Without all three, every container rebuild silently wipes that data - caught 2026-04-25 for `email_groups/` and `analyzer_prompts/`.
- Rename or remove `data-si-task-id` on the ingestion table rows or the matching `tr[data-si-task-id]` selector in `navigateToIngestionTask()` without updating both sides. The Pipeline Check "Go to ingestion task →" cross-tab nav (Wave 2, 2026-04-25) silently fails to find the row otherwise. Pinned by `tests/test_alert_group_deploy_run_chain.py::TestNavigationContract`.
- Re-couple parsing and writing in `AlertGroupDispatcher._extract_and_log_picks` - the Wave 3 manual-return endpoint depends on `_parse_picks_block` being pure (no I/O, no logging side effects beyond warnings) so it can drive the dry-run preview pane. Always preserve the parse → log split.
- Rename or remove the Wave 4 (2026-04-25) row data attributes (`data-search-name`, `data-ag-row-name`) or the matching `tr[...]` selectors in `navigateToSavedSearch` / `navigateToAlertGroup` without updating both sides. The cross-link badge click-through silently fails to find the row otherwise. Pinned by `tests/test_wave4_cross_linking.py::TestFrontendContracts`.
- Drop a tab from the top nav without also removing it from `EXPECTED_PAGES` in `tests/test_wave4_cross_linking.py::TestTabBarReorder` in the same commit. The drift-guard otherwise fails loud (correctly) at every CI run after the unrelated change.
- Route an alert-group failure / diagnostic email to the customer-facing `email_address` field. Wave 5 (2026-04-26) added per-AG `admin_error_email` precisely so paid mailing lists never receive operational error notices. Recipient priority is per-AG admin → global `alert_group_failure_email_to` → `smtp_from` → `smtp_user`, and **never** falls through to `email_address`. Pinned by `tests/test_wave5_admin_error_email.py::TestAGFailureRouting::test_per_ag_admin_email_wins_over_global`.
- Add a runtime chart-library dependency (Chart.js / D3 / Recharts / etc.) for the Wave 6 (2026-04-26) Schedule volume charts. They're inline SVG by design - keep it that way. Pinned by `tests/test_wave6_schedule_volume.py::TestFrontendContracts::test_renderer_uses_inline_svg_no_runtime_deps`.
- Re-flatten the 2026-04-27 dropdown nav back into 14 sibling `.nav-tab` buttons, OR remove the `data-page` / `data-group` attributes from leaf `.nav-tab` buttons inside `.nav-dropdown` panels. The cross-tab navigation contract (`document.querySelector('.nav-tab[data-page="X"]').click()`, used in ~15 callsites including welcome doc cards, alert-group badges, and ingestion-task cross-links) depends on those leaf buttons existing in the DOM with their original attributes. The 5-group dropdown is the new shape - to add a tab, nest a new `<button class="nav-tab" data-page="..." data-group="...">` inside the matching `.nav-dropdown`. Pinned by `tests/test_nav_dropdown_menus.py`.
- Drop or rename any of the 8 options-specific columns added to the `ag_picks` schema 2026-04-26 (`option_structure`, `option_legs_json`, `option_max_loss_usd`, `option_max_profit_usd`, `option_net_debit_credit`, `option_dte_days`, `option_difficulty_tier`, `account_size_floor_usd`) without ALSO updating (a) `functionality/log_writer.py::SCHEMAS`, (b) `log_ag_pick` kwargs, (c) `alert_groups/dispatcher.py::_validate_and_normalize_pick` extraction, (d) `_log_picks` forwarding, (e) the `options_edge_brief.yaml` prompt-text JSON-tail spec, and (f) `tests/test_options_edge_brief.py`. Options Edge Brief Wave 1 (2026-04-26) needs this column set existing end-to-end so Wave 2 mark-to-market attribution can read structured leg data per pick. Renaming a column without updating all six places silently breaks pick journaling - caught at brief-dispatch time, hours to days later. Pinned by `tests/test_options_edge_brief.py::test_ag_picks_schema_has_options_columns` + `test_oeb_prompt_documents_options_fields`.
- Remove a column from any IMMUTABLE-bound schema (`ag_picks`, `ag_picks_closures`, `ag_picks_review_observations`) once shipped. Wave 2 of Options Edge Brief (2026-04-27) introduced `indexes/IMMUTABLE/<subdir>/*.parquet` as the protected, never-garbage-collected tree for the user's decade-horizon trading record. The design horizon is a decade of compounding; historical SPQL queries that referenced any column must keep working forever. The log_writer projects-with-NULL on missing columns when reading, so ADDING columns is fully backward-compatible - but REMOVING a column breaks every historical query touching it. Frozen column snapshots in `tests/test_oeb_wave2.py::test_immutable_schema_is_additive_only` fail loud if any of these schemas lose a column. If you genuinely need to migrate a column (last resort), do it as a one-time data migration that rewrites every existing parquet AND keep the old column NULL'd in the schema for backward compat.
- Move any IMMUTABLE-bound subdirectory back under `indexes/logs/` or anywhere else inside `indexes/` outside of `IMMUTABLE/`. The cleanup `skip_subdirs` mechanism only protects the top-level `IMMUTABLE/` segment; if you nest IMMUTABLE data inside a non-protected tree, mtime-based cleanup will silently delete the trading record. Always write to `settings.immutable_subdir(name)`, never construct the path manually. Pinned by `tests/test_oeb_wave2.py::test_engine_skip_subdirs_includes_both_logs_and_immutable`.
- Remove `indexes/IMMUTABLE` from `tools/persistence.py::DIR_TARGETS_HASHED`. It MUST be in the default-backup set (not gated behind `--include-indexes`) - otherwise a routine `python -m tools.persistence backup` silently excludes the OEB pick journal. Pre-2026-05-06 IMMUTABLE was implicitly covered by `DIR_TARGETS_SUMMARIZED["indexes"]` but that entry is opt-in only - meaning the user's decade-horizon trading record was excluded from default backups. The de-dup logic in `cmd_backup` (excludes IMMUTABLE from the bulk indexes/ add when `--include-indexes` is also passed) prevents tar-duplication. Pinned by `tests/test_persistence.py::TestImmutableBackupCoverage` (4 tests: default-includes-immutable, include-indexes-no-dup, round-trip-bit-identical, dir-targets-hashed-membership) + `test_immutable_is_covered_by_parent_indexes_mount`.
- Re-evaluate already-closed picks in the performance-review AG with hindsight ("the analyst should have closed earlier", "this would have won if held longer"). Wave 2 (2026-04-27) is built on marker/examiner separation - the deterministic `oeb_pick_tracker_pro` script grades picks against the rules-as-they-existed-at-entry; the Claude review only AGGREGATES those outcomes. If the review prompt loses the explicit "no hindsight" guidance, the metric becomes an opinion-of-opinions and the user's go-live decision loses its measurable foundation. Pinned by `tests/test_oeb_wave2.py::test_perf_review_prompt_documents_marker_examiner_separation`.
- Use naive `datetime.now().isoformat()` (or naive `croniter().get_next(datetime).isoformat()`) on any wire ISO sent to the SPA. JavaScript's `new Date(iso)` parses time-only-no-offset strings as **browser-local time**, not UTC - a 7-hour display lie for a PT user against a UTC scheduler (caught 2026-04-27 in `_get_next_run`). Always pass a tz-aware anchor to `croniter` and return TZ-aware ISO with explicit `+HH:MM` offset. Pinned by `tests/test_timezone_aware_scheduling.py::TestAGStoreTimezone::test_next_run_iso_carries_offset`.
- Drop the `timezone:` field from `AlertGroupStore` / `SavedSearchStore` schemas, OR remove the `timezone=` kwarg from `CronTrigger.from_crontab` calls in `alert_groups/scheduler.py` / `query_engine/QueryEngine.py`. Per-AG / per-search timezone (added 2026-04-27) is the documented escape hatch from the UTC-pinned scheduler - it lets the OEB cron `30 10,15 * * 1-5` in `America/New_York` fire at 10:30 + 15:30 ET year-round without manual DST adjustments. Removing the field without removing the cron interpretation breaks every TZ-aware AG silently at the next DST boundary. Pinned by `tests/test_timezone_aware_scheduling.py::TestSchedulerTimezoneWiring`.
- Re-introduce a silent-zero fallback in any `earliest=`/`latest=` parsing path. The 2026-04-29 audit caught the legacy `_parse_date_to_epoch` returning `0` on parse failure, which silently became `WHERE _epoch >= 0` and made the bound indistinguishable from "no bound applied" in the result set. The user-query path now goes through `parse_date_to_epoch` (no leading underscore) which raises :class:`TimeBoundParseError`, and the extractor re-raises with per-keyword context. The legacy `_parse_date_to_epoch` shim is preserved ONLY for `ParquetEpochAdder` per-row backfill - never call it from query-execution code. Pinned by `tests/test_time_bounds.py::TestDiagnosticsSurface::test_garbage_does_not_silently_return_unfiltered`.
- Add a new accepted `earliest=`/`latest=` value form without ALSO updating (a) the strptime/fromisoformat dispatch in `functionality/duckdb_index_call.py::parse_date_to_epoch`, (b) `tests/test_time_bounds.py::TestStrictParserAcceptedForms`, and (c) the table in `docs/lang/01_fundamentals.md` "Time Ranges → Accepted value forms". The user expects every documented form to round-trip through `execute_query` end-to-end - and the integration cliff that hid the original bug (every prior test bypassed the ANTLR pathway by hand-building tokens) is exactly what `tests/test_time_bounds.py::TestEndToEndExecuteQuery` now closes. Skip the integration test and the silent-failure regression ships immediately.
- Re-track `alert_groups/*.yaml` files in git, OR add a new "shipped default" config tree without the `default_<thing>/` + `_seed_defaults()` no-overwrite pattern. The 2026-04-30 audit caught all 13 AG yamls being tracked, which meant any `git pull` that updated a default could clobber a user's UI customisation (the user's verbatim complaint: "ALL alert group settings ARE ALWAYS LOST WHENEVER UPDATE.SH IS RUN"). Fix shipped: yamls moved to `default_alert_groups/` (tracked), `/alert_groups/*.yaml` added to `.gitignore`, `AlertGroupStore._seed_defaults()` copies missing-only on init, with `install_default()` for on-demand re-install via Feeder Health. Pinned by `tests/test_alert_group_persistence_and_button.py::TestAlertGroupsYamlGitignored::test_check_ignore_actually_ignores_a_test_yaml`. Same pattern for any future user-mutable config tree: ship defaults under `default_<thing>/` (tracked), gitignore `<thing>/*.yaml`, seed missing-only on init.
- Render an action-oriented button (Enable/Disable, Start/Stop, Show/Hide, etc.) using the CURRENT-STATE label instead of the NEXT-ACTION label. The 2026-04-30 fix to the AG Enable/Disable button caught the buggy form `isDisabled ? 'Disabled' : 'Enabled'` (current state) - every click reloaded the row but the label didn't change so the user thought clicking did nothing. Always use the next-action form: `isDisabled ? 'Enable' : 'Disable'`. Pinned by `tests/test_alert_group_persistence_and_button.py::TestEnableDisableButtonContract::test_button_label_is_action_oriented`.
- Render a binary state toggle WITHOUT an explicit state pill alongside the action button. The user must be able to read the CURRENT state (ON/OFF, ENABLED/DISABLED, ACTIVE/INACTIVE) at a glance from a single visual element - separate from any action button. The 2026-04-30 money-leak audit added `.ag-state-pill` precisely so the user can never confuse "Enable" (action button text when AG is currently DISABLED) for "currently enabled". Pill and button must use DIFFERENT vocabularies (pill: ON/OFF; button: Enable/Disable). For any binary state where a wrong reading could cost money (Claude billing, cron firing, billing tier toggles), the pill is REQUIRED, not optional. Pinned by `tests/test_ag_disabled_money_leak_audit.py::TestStatePillContract`.
- Add a binary state field (disabled, paused, enabled, active) gated by ONE layer of defense. Defense in depth means at least TWO independent gates: (a) the scheduler must not register/keep the job, AND (b) the dispatcher/runner must check at execute time. Pre-2026-04-30 `register_alert_group_jobs` only SKIPPED registration for disabled AGs but never REMOVED previously-registered jobs - leaving the dispatcher gate as the sole load-bearing check. Caught when the user explicitly worried about money leaking. Always: when adding a "disabled" / "paused" / "off" gate, write a money-leak canary test (patch the billable client, raise AssertionError on invocation, run the disabled path, assert zero invocations). Pinned by `tests/test_ag_disabled_money_leak_audit.py::TestDispatcherDisabledGate::test_dispatcher_skips_disabled_group`.
- Route an alert group's analysis call so a `model_id`-set (local-model) AG can fall through to `analyzers.claude_client.call_messages_create` (the billable Claude path). Slice A (2026-06-23) made AG dispatch provider-agnostic: when an AG sets `model_id`, `alert_groups/dispatcher.py` routes through `_call_router_llm` → `analyzers.llm_router.call_llm` (a $0 local LAN model like `llamacpp-qwen35-122b-a10b`) with NO `web_search` tool (a single-shot completion can't use Anthropic's server-side tool - and an MCP server wouldn't help, there's no agentic loop), the model's per-record timeout (not `claude_request_timeout_seconds`), and the per-AG dollar budget short-circuited to $0. The Claude path is byte-for-byte unchanged when `model_id` is empty. NON-OBVIOUS gotcha: the dispatcher binds `call_messages_create` at module load (`from analyzers.claude_client import call_messages_create`), so a money-leak canary MUST patch `alert_groups.dispatcher.call_messages_create` - patching `analyzers.claude_client.call_messages_create` does NOT intercept the dispatcher's binding. (The router patch target is `analyzers.llm_router.call_llm`, which works because `_call_router_llm` lazy-imports `call_llm` at call time.) Pinned by `tests/test_ag_local_model_slice_a.py::TestLocalModelMoneyLeakCanary` (Claude never called when `model_id` set; router never called when unset) + budget-short-circuit + empty-response-fails-loud canaries.
- Remove the Headroom **fail-open** path or let a Headroom-routed call hard-fail when the proxy is unreachable. Headroom (2026-06-23) is an OPTIONAL compression proxy in front of Anthropic; it must NEVER be able to take down alert analysis. A connection-level failure (proxy down/refused/reset/timeout/HTTP 502-504) on the headroom route MUST retry the same call against direct Anthropic (`path="direct-fallback"`, warning logged) - and this failover MUST NOT consume the retry budget (so even `claude_retry_attempts=0` fails open). A genuine Anthropic 4xx must NOT fail over (it would also fail direct - retrying just doubles the cost of a doomed request). The routing decision lives ONLY in `analyzers/headroom.py::resolve_use_headroom` (per-alert → per-AG → `global_use_headroom_default`); the `HEADROOM_DISABLE` env kill switch is enforced both there AND inside `call_messages_create` (defense in depth). Do NOT route the `| llm` pipes, patch drafter, batch submissions, or settings-test button through Headroom - the feature is scoped to alert analysis (AG dispatcher + the per-search analyzer). The `headroom_path` log column is append-only/additive like every other claude_api column. Pinned by `tests/test_headroom_integration.py`.
- Use **numeric day-of-week** in any cron schedule (e.g. `* * * * 1-5`, `0 12 * * 0`). APScheduler's `CronTrigger.from_crontab` interprets `0=Mon` while Linux/operator convention is `0=Sun`, so `1-5` (intended Mon-Fri) silently fires Tue-Sat with Mondays SKIPPED. Caught 2026-05-02 - `options_edge_brief` had been firing Saturdays + skipping Mondays for an unknown duration. The `functionality/cron_compat.py::linux_dow_to_apscheduler` translator at all 6 `from_crontab` call sites masks this for any user-typed cron, but **always use named days** in YAML defaults, library `suggested_cron`, and any cron string: `mon-fri`, `sun`, `sat,sun`, `tue,thu`. Self-documenting AND survives any future translator regression. Pinned by `tests/test_cron_compat.py::test_all_from_crontab_callsites_use_translator` (drift guard scans all production `.py` for unwrapped `from_crontab`) + 30 translation/behavioral tests. Same rule applies to `default_alert_groups/*.yaml`, `default_saved_searches/*.yaml`, and `script_library/scripts/*.json::suggested_cron`.
- Rename a saved search / alert group / ingestion script without DELETE-ing the old name in the same commit. The old YAML/record stays on disk, keeps firing on its cron, and writes cached results no consumer reads - pure wasted execution. Caught 2026-05-03: 22 `ag_*`-prefixed orphan SS still firing 10 days after the 2026-04-23 dob_/gmrb_ rename. Always: when renaming, `DELETE /api/ss/<old>` (soft-delete, recoverable for 30 days) OR set `disabled: true` on the old record in the same commit. Detection signature for orphans: cron set + zero AG references + no email recipient + identical query/cron to a renamed counterpart. See `reference_orphan_from_rename_pattern.md`.
- Ship a new `| llm`-shaped SPQL pipe (any pipe that dispatches a billable LLM call per row or per DataFrame) without ALSO supporting `max_cost_usd=<F>` and `dry_run=<bool>` kwargs. The slice-7 contract (2026-05-08) is: every billable pipe routes through `analyzers.llm_router.estimate_cost_usd()` BEFORE its dispatch loop and short-circuits to a `_llm_status="dry_run"` preview row when `dry_run=true`. Future Phase 2/3 pipes (e.g. `| llm_streaming`, `| classify`, `| embed_and_llm`, agentic `| react` loops, Phase 3 reactive notebook cells with implicit LLM steps) MUST honour these kwargs. Pinned by `tests/test_llm_pipe_slice7.py::TestMoneyLeakCanary::test_dry_run_makes_zero_call_llm_invocations` + the companion budget-gate canary. A decade-horizon compounding mission means a silent path that bypasses the budget gate is an existential constraint, not a UX nice-to-have. When in doubt, write a money-leak canary test for the new pipe FIRST (patch `analyzers.llm_router.call_llm` to raise `AssertionError("MONEY LEAK")`, run the dry-run path, assert zero invocations) - same pattern as `tests/test_ag_disabled_money_leak_audit.py`.
- Invoke `AlertGroupStore.save_group` / `update_group` (or any future AG-mutating method) from a notebook-cell-execution code path. Phase 3 / Bet 4 slice 9 (2026-05-09) cemented the **config-leak canary** boundary: `promote_to_alert_group` cells render an in-cell DRY-RUN preview at execution time; the actual deploy happens via the explicit `POST /api/notebooks/<id>/promote/<cell_id>` endpoint which is the ONLY path allowed to mutate AG state from the notebook surface. A re-run / cache-miss / cell re-render must NEVER silently create or update an AG - that would convert a 10-second iteration loop into "did I just overwrite production?" anxiety. Pinned by `tests/test_notebook_slice9_promote.py::TestConfigLeakCanary` (patches both AG mutating methods with `AssertionError("CONFIG LEAK")` and runs a notebook with a promote cell; both must stay zero). Generalises to any future notebook cell type that mutates persistent state (broker order placement, credential vault writes, scheduled task creation in Phase 4+ - all need the same dry-run-by-default + explicit-endpoint pattern + canary test). The user's verbatim 2026-05-08 mission *"shy away from restricted python usage literally ANYWHERE else"* + dual-audience principle frames this: notebook execution is operator-driven exploration; mutating production is operator-driven deploy. Don't conflate the two.
- Use the module-level `duckdb.sql(...)` helper to read IMMUTABLE parquets from a Flask request handler, APScheduler job, or any code that can run on a worker thread. The `duckdb.sql()` helper shares a SINGLE module-level default connection across all callers, and DuckDB connections are NOT thread-safe for concurrent execution. Two near-simultaneous request handlers can leave the global connection in a bad state, producing the misleading `InvalidInputException: Attempting to execute an unsuccessful or closed pending query result` on the second caller's `.fetchall()`/`.df()`. Caught 2026-05-18 on prod when the UI fired two IMMUTABLE-reading endpoints back-to-back. Always use a per-call connection: `con = duckdb.connect(database=":memory:"); con.execute("PRAGMA threads=1"); ...; con.close()` in a try/finally.
- Read a multi-file IMMUTABLE-tree parquet glob (any `read_parquet([files...])` or `read_parquet('<glob>')` against `indexes/IMMUTABLE/ag_picks*` or any future IMMUTABLE schema) WITHOUT `union_by_name=true`. Two unrelated production 500s on 2026-05-18 traced to the SAME root cause: DuckDB picks the FIRST-sorted file's schema for the whole glob, and IMMUTABLE trees naturally have schema-heterogeneous files. Two distinct failure modes: (a) an ingestion script that writes a parquet PER FIRE even when zero rows were fetched - pyarrow infers all-None string columns as the Null logical type, and any `WHERE col = ?` / `col IN (...)` against a Null-typed column fails with `Conversion Error: Unimplemented type for cast (VARCHAR -> "NULL")`; (b) additive schema growth means older parquets lack newer columns, and a `COALESCE(new_col, '')` against a missing column trips the misleading binder error `Column "new_col" referenced that exists in the SELECT clause - but this column cannot be referenced before it is defined`. The fix is `union_by_name=true` - it promotes Null-typed columns to VARCHAR (using the wider type across files) AND fills missing columns with NULL so binder resolution always succeeds. Applies to EVERY multi-file IMMUTABLE read (e.g. the generic `_probe_index_freshness` in `desktop_app/server.py`, since OEB IMMUTABLE schemas grow too). When introducing a new IMMUTABLE-tree read site, always include `union_by_name=true` in the SQL.

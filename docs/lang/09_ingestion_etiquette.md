# Efficient Python Ingestion Etiquette

SpeakesQuery gives you full control over how data enters the system. That power comes with a responsibility: **the quality of your ingestion scripts determines the quality of everything downstream** - query speed, storage costs, alert reliability, and overall system health.

This guide covers the principles and practical patterns that separate a clean, efficient deployment from one that wastes resources and produces unreliable results.

> **Not every dataset needs a script.** For one-off imports - CSV exports, database dumps, static log archives - use the **Import** tab instead. It converts your file to a queryable index in seconds with no code required. Scripted ingestion is for **recurring data sources** that need to pull fresh data on a schedule.

---

## The Operator's Responsibility

SpeakesQuery does not make assumptions about your data. It will faithfully ingest whatever your scripts produce - duplicates included. There is no built-in magic that deduplicates, validates, or rate-limits on your behalf. That is by design: you know your data sources better than any generic engine could.

This means:

- **If your script produces duplicates, your index will contain duplicates.**
- **If your script pulls too much data, your storage will reflect it.**
- **If your script hammers an API, you will get rate-limited or banned.**

A well-written ingestion script is the single most impactful thing you can do for the health of your deployment.

---

## Avoiding Data Duplication

Duplicate data is the most common problem in ingestion pipelines. It inflates storage, skews aggregations (`stats count` returns inflated numbers), and makes dashboards unreliable.

### Use `_epoch` as Your Dedup Anchor

Every record should carry an `_epoch` timestamp (Unix seconds). When fetching data that may overlap with previous runs, use `_epoch` to determine what you already have:

```python
import time

# Only emit records newer than the last known timestamp
cutoff = time.time() - lookback_seconds
for record in api_results:
    if record["timestamp"] >= cutoff:
        output.append(record)
```

### Idempotent Scripts

An idempotent script produces the same result whether it runs once or ten times for the same time window. To achieve this:

1. **Filter at the source.** Use API parameters (`since`, `after`, `updated_after`) to request only new data rather than fetching everything and filtering locally.
2. **Use unique identifiers.** If the source provides an ID (event ID, record ID, commit SHA), include it in your output. This makes downstream dedup queries trivial: `| dedup event_id`.
3. **Avoid appending blindly.** If your script re-fetches the last hour of data every run, you will get overlapping records unless the source guarantees idempotent responses.

---

## Lookback Windows vs. Cron Periods

The relationship between your **lookback window** (how far back your script looks for data) and your **cron period** (how often it runs) is critical to get right.

### The Golden Rule

> **Your lookback window should always be slightly longer than your cron period.**

If your script runs every 15 minutes, its lookback should cover at least the last 16–20 minutes. This overlap ensures no data falls through the cracks if a run is delayed, slow, or briefly fails.

### What Happens When the Lookback Is Too Short

```
Cron: every 15 min    Lookback: 10 min

Timeline:  |----run1----|         |----run2----|
           00:00    00:10        00:15    00:25

Gap: data from 00:10–00:15 is NEVER fetched.
```

Events that arrive in the gap between the lookback end and the next run start are **permanently lost**. You will not know they are missing unless you go looking.

### What Happens When the Lookback Is Too Long

```
Cron: every 15 min    Lookback: 60 min

Timeline:  |------------run1------------|
           00:00                    01:00
                    |------------run2------------|
                    00:15                    01:15
```

Every run re-fetches 45 minutes of data you already have. This means:

- **4x the API calls** needed (most responses are redundant)
- **Duplicate records** in your index unless your script deduplicates
- **Wasted compute and storage** - you are paying (in time and resources) to process data you already ingested

### Recommended Overlap

| Cron Period | Lookback Window | Overlap |
|---|---|---|
| 5 min | 6–7 min | ~20–40% |
| 15 min | 18–20 min | ~20–33% |
| 1 hour | 70–75 min | ~15–25% |
| 1 day | 25–26 hours | ~4–8% |

The overlap percentage can decrease as the period gets longer - a 1-day job is unlikely to be delayed by an hour, but a 5-minute job can easily slip by a minute or two.

### Handling the Overlap

Since the overlap intentionally re-fetches some data, your script must handle duplicates. The two cleanest approaches:

1. **Dedup at write time.** Include a unique ID in each record and let your query pipeline handle it: `| dedup event_id sortby -_epoch`.
2. **Dedup in-script.** Track the last-seen ID or timestamp from the previous run (using a checkpoint file or the API's cursor) and skip records you have already emitted.

---

## Efficient API Consumption

### Paginate Correctly

Most APIs return results in pages. A common mistake is fetching only the first page and silently dropping the rest:

```python
# BAD - only gets first page
response = requests.get(url, params={"limit": 100})
data = response.json()["results"]

# GOOD - follows pagination to completion
results = []
next_url = url
while next_url:
    response = requests.get(next_url, params={"limit": 100})
    page = response.json()
    results.extend(page["results"])
    next_url = page.get("next")  # or however the API signals more pages
```

### Respect Rate Limits

If an API returns a `429 Too Many Requests` or a `Retry-After` header, honor it. SpeakesQuery's `Max Retries` setting will retry on transient failures, but a script that aggressively re-hits a rate-limited API will just burn through retries and fail.

```python
import time

response = requests.get(url)
if response.status_code == 429:
    wait = int(response.headers.get("Retry-After", 60))
    time.sleep(wait)
    response = requests.get(url)  # single retry after backoff
```

### Use Incremental Fetching

Instead of pulling the entire dataset every run, use API features to fetch only what changed:

- **Cursors / pagination tokens** - resume from where the last run stopped
- **`since` / `updated_after` parameters** - let the API filter server-side
- **ETags / `If-Modified-Since` headers** - skip the fetch entirely if nothing changed

Incremental fetching reduces API calls, network traffic, and processing time dramatically.

### Surviving an API V2 schema migration

Public APIs change. The most painful kind of change is a **silent schema migration** - fields get renamed, nested under a new object, or moved to a sibling endpoint, but the HTTP request continues to return `200 OK` with a familiar-looking JSON envelope. Your script keeps running. Every row it produces is mostly empty. No error surfaces until someone reads the output.

Two real cases caught in the SpeakesQuery 2026-Q1 → Q2 ingestion audit:

- **Kalshi V2** - Field names gained suffixes (`yes_bid` → `yes_bid_fp`, prices changed type from int cents to string-typed dollars). The `/markets` endpoint also began emitting auto-generated permutation rows (4000+ `KXMVE*` markets per query). Fix: walk `/events` first then per-event `/markets` to skip the permutation flood; add multi-path defensive reads (`m.get('yes_bid_fp') or m.get('yes_bid')`).
- **Metaculus V2** - Question data was hoisted into a nested `question` object, several fields were renamed (`prediction_count` → `forecasts_count`, `created_time` → `created_at`, `publish_time` → `published_at`, `resolve_time` → `scheduled_resolve_time`), and categories moved to `q.projects.category[]`. Public API access also gained an auth requirement.

The handling pattern that makes a script survive both forms:

1. **Detection** - Spot the silent migration by watching for runs where most fields populate but a specific subset is consistently empty across all rows. That's the fingerprint of a renamed field.
2. **Multi-path defensive reads** - Read the V2 path first, then fall back to the legacy path, then a sentinel default. Never index into a nested object without first checking the parent type:
   ```python
   question_data = q.get('question', {}) or {}
   qtype = (
       question_data.get('type', '')   # V2 path
       or q.get('possibilities', {}).get('type', '')   # legacy
       or q.get('type', '')             # older legacy
       or 'binary'                      # safe default
   )
   ```
3. **Sentinel rows for total failure** - If the API returns 401/403 (auth required), 429 (rate-limited), or an unparseable payload, emit a single sentinel row with `question_type='INFO'` (or similar) carrying instructions to the operator instead of writing zero rows. Zero rows look identical to "empty quiet day" - sentinel rows surface the actual failure.
4. **Test fixtures for both shapes** - When you write the test mock, include both a V2-shaped fixture AND a legacy-shaped fixture as separate rows so the mock router exercises both code paths in the same run.

The reusable primitive is documented in `reference_api_v2_schema_migration_playbook.md` in the project memory.

---

## Resource-Conscious Design

### Mind Your Memory Footprint

Every row your script collects lives in memory until it is written to disk. If you are fetching millions of records, consider:

- **Streaming writes** - process and emit records in batches rather than accumulating everything into a single list
- **Generator patterns** - use `yield` to process one page at a time rather than loading all pages into memory
- **The `Max Output Rows` setting** - this is your safety net. If your script accidentally produces 50 million rows, this cap truncates the output before it crashes your system. But don't rely on it - write scripts that produce the right amount of data in the first place.

### Be Aware of Response Size Limits

The `Max Response Size (MB)` setting caps individual HTTP responses. If an API returns a 200 MB JSON blob, your script will be blocked. Solutions:

- Request smaller pages (reduce `limit` parameter)
- Use compressed responses (`Accept-Encoding: gzip`)
- Filter server-side to reduce payload size

### Set Reasonable Timeouts

The `Script Timeout` and `HTTP Timeout` settings exist to prevent runaway scripts. If your script legitimately needs more than the default timeout:

1. Increase the timeout in Settings
2. But also ask yourself: **why does this script take so long?** Long runtimes often signal an inefficient approach (fetching too much data, not paginating, waiting on a slow endpoint without a timeout).

---

## Testing and Validation

### Start Small

Before scheduling a script to run every 5 minutes in production:

1. **Run it once manually** from the Ingestion Scripts page. Check the output - are the columns right? Is `_epoch` populated? Are there duplicates?
2. **Run it twice** with overlapping time windows. Did you get duplicates? If so, fix your dedup logic before scheduling.
3. **Check the row count.** If a single run produces 100,000 rows but you expected 500, something is wrong. If it produces 10 million, something is very wrong.

### Validate Before Scheduling

Use the Query page to inspect your ingested data:

```
index="your_index/*" | stats count
index="your_index/*" | stats count by _epoch | sort -_epoch | head 20
index="your_index/*" | dedup your_unique_id | stats count
```

The third query tells you how many truly unique records you have. If it is significantly less than the first query, you have a duplication problem.

### Monitor Over Time

After scheduling, periodically check:

- **Row growth rate** - is it consistent with expectations, or accelerating (sign of duplication)?
- **Storage size** - is your index growing faster than it should?
- **Alert reliability** - are alerts firing when they should? Missing events mean your lookback may be too short.

---

## Preserving Schema on Empty Days

Any ingestion that can legitimately produce zero rows on some runs (no cross-platform arbitrage today, no unusual-options anomalies, an API returning an empty list) **must** emit an empty DataFrame that still carries the expected schema. Otherwise downstream SPQL queries like `| where divergence_pct >= 5.0` will crash with `UndefinedVariableError: name 'divergence_pct' is not defined` - pandas' `pd.DataFrame([])` produces a DataFrame with **zero columns**, and `df.query(...)` cannot resolve a column reference against an empty schema.

**Wrong** (pandas infers zero columns on empty input):

```python
rows = []
for item in api_response:
    if passes_filter(item):
        rows.append({...})

df = pd.DataFrame(rows)    # ← zero columns when rows=[]
GENERATE_RESULTS(df)
```

**Right** (schema preserved on empty days):

```python
EXPECTED_COLUMNS = [
    "ticker", "price", "volume",
    "divergence_pct", "opportunity_strength",
    "_epoch",
]
rows = []
for item in api_response:
    if passes_filter(item):
        rows.append({...})

df = pd.DataFrame(rows, columns=EXPECTED_COLUMNS)  # ← schema preserved
GENERATE_RESULTS(df)
```

Reference: `script_library/scripts/kalshi_polymarket_arbitrage_pro.json` and its sandboxed sibling both use this pattern (fixed 2026-04-21 after a Daily Opportunity Brief dispatch silently skipped the feeder with a misleading "No cached result" error).

An alternative is the "sentinel error row" pattern used by `options_unusual_activity_pro.json` - emit a single row with `alert_level='UNKNOWN'` and an `error_detail` string when the script can't produce real data, so downstream consumers always see a well-shaped Parquet and the filter pipe naturally drops the sentinel. Pick whichever pattern reads best for your script; what matters is the final Parquet has the full column set.

The SPQL engine itself short-circuits `where` / `table` / `sort` on empty DataFrames as a defense-in-depth - so if an existing third-party script produces a zero-column empty Parquet, the query no longer crashes, just returns empty. But new scripts should still adopt the schema-preserving pattern above.

---

## Summary

| Principle | Why It Matters |
|---|---|
| Lookback > cron period | Prevents data gaps from delayed or slow runs |
| Lookback not too long | Avoids redundant API calls and duplicate records |
| Dedup by unique ID | Keeps aggregations accurate and storage lean |
| Paginate fully | Ensures you do not silently drop data |
| Respect rate limits | Prevents bans and wasted retries |
| Fetch incrementally | Reduces load on both the API and your system |
| Test before scheduling | Catches problems before they compound over days |
| Preserve schema on empty | Downstream SPQL survives zero-row days without crashing |

Your deployment's efficiency is a direct reflection of your ingestion scripts. A few minutes spent writing a careful, incremental, dedup-aware script will save hours of troubleshooting and gigabytes of wasted storage down the road.

---

## Trust Tiers: `sandboxed` vs `unrestricted`

Every ingestion script declares a `trust_level` that controls how it is compiled and what Python it can access. The default is `sandboxed` - what all scripts shipped before 2026-04-16 use. A new `unrestricted` tier was added to give expert authors access to libraries like `scipy`, `scikit-learn`, `rapidfuzz`, and anything else on `sys.path`.

### The two tiers

| Tier | Compilation | Imports | Builtins | When to use |
|---|---|---|---|---|
| `sandboxed` (default) | RestrictedPython (`compile_restricted`) | Allowlist: `pandas, requests, json, datetime, time, re, math, hashlib, base64, collections, io, bs4, lxml` | `safe_builtins` + hand-picked extras | Everything, unless you need a library outside the allowlist or scipy-level scientific computation |
| `unrestricted` | Plain `compile()` | Full `sys.path` | Full `__builtins__` | You genuinely need `scipy`, `sklearn`, `rapidfuzz`, or equivalents. If the sandboxed version would be functionally identical, **keep it sandboxed** |

### What stays the same for both tiers

- The `GENERATE_RESULTS(df)` convention and `_epoch` enforcement
- Per-execution **HTTP request budget** (capped via `BudgetAwareRequests`)
- Per-execution **wall-clock timeout** (default 600 s - raised from 120 s on 2026-05-04 as a uniform floor across all schedule cadences; tighter intervals can still scale lower via the budget rules below, but no script gets less than 600 s headroom unless explicitly overridden)
- Per-execution **max output rows** cap
- **`allowed_api_domains` enforcement** - every `requests.get/post/put/patch/delete/head` call from a sandboxed script is checked against the regex allowlist in `Settings > Ingestion > allowed_api_domains` *before* the request leaves the process. A non-matching hostname raises `ValueError` immediately; the request is never sent and does not consume budget. Unrestricted scripts can bypass the wrapper entirely (see threat model below) - for them the allowlist is advisory.
- Credential vault injection - scripts never see raw secrets
- Atomic Parquet writes with subdirectory validation

### The `_pro` naming convention

When you create an unrestricted variant of a sandboxed script, name it `<base>_pro.json`, write its output to a sibling subdirectory `<base_subdir>_pro/`, and declare `trust_level: "unrestricted"` in the JSON. This keeps both versions runnable side-by-side and makes it obvious to authors which tier they are looking at.

### Deploying a `_pro` script - the trust-level contract

The **Script Library → Deploy** button reads the script's `trust_level` and sets the matching value on the **Trust Level** dropdown in the Create Ingestion form. Both the **Test** button and the **Save** button forward the trust level to the backend, so the test runs under the same mode the saved task will run under.

If you hand-type a pro-tier script into the form (instead of using Deploy), set **Trust Level** to **Unrestricted** yourself. Symptoms of forgetting:

- `NameError: name '_unpack_sequence_' is not defined` - your code uses tuple-unpack in an assignment (`a, b = pair`); RestrictedPython rejects it
- `NameError: name '_iter_unpack_sequence_' is not defined` - your code has `for a, b in items:`; same reason
- Any import error for `scipy`, `sklearn`, `rapidfuzz`, `numpy.*` - those aren't on the sandboxed allowlist

### Title restrictions

Library script titles must use only letters, digits, space, underscore, period, and hyphen. Colons, parentheses, ampersands, and similar special characters are disallowed - they break downstream display and filename sanitization. The `test_title_has_no_special_characters` test in `test_script_library.py` enforces this at CI time.

### Credential kinds - `api_key` vs `contact` vs `identifier`

Not every entry in `requires_credentials` is an API key. SpeakesQuery supports four credential kinds so the UI can render appropriate pills, hints, and deploy notifications instead of labelling everything "API Key Required":

| Kind | Meaning | Example |
|---|---|---|
| `api_key` | Secret key issued by a provider portal (default if `credential_kinds` omitted) | `FRED_API_KEY` |
| `secret` | Non-api-key secret - token, password | rare; most providers issue "keys" |
| `contact` | **Not a secret.** A contact identifier (usually email) required by fair-access policies so the upstream API can reach you if your requests misbehave. No portal, no key to generate. | `SEC_EDGAR_CONTACT` - any plausible contact string like `your.name@example.com` or `Jane Doe jane@example.com` |
| `identifier` | **Not a secret.** A public identifier the script needs to parameterize its query. | `POLYMARKET_USER_ADDRESS` (public blockchain address), `POLYMARKET_SEARCH_TERM` (literal search term) |

Declare kinds in the script JSON:

```json
"requires_credentials": ["SEC_EDGAR_CONTACT"],
"credential_kinds": {"SEC_EDGAR_CONTACT": "contact"}
```

**UI behavior when a script is deployed:**
- Script Library card shows the worst-severity pill: red **API Key Required** if any secret is required, amber **Contact String** if only contact/identifier entries, green **No Auth** otherwise.
- The filter chip bar gets a **Contact String** filter that shows every script whose strictest requirement is a non-secret.
- The Create Ingestion credentials sidebar shows a per-credential hint banner with kind-appropriate copy - e.g. a contact entry explains *"Not an API key - this is the contact identifier… any plausible contact string works."*

For back-compat, scripts without a `credential_kinds` field are treated as if every entry is `api_key`. Adding the field is additive - no migration required.

### Output-schema rule (important)

A `_pro` variant MUST emit a **superset** of its sandboxed counterpart's columns: every column the base emits, plus any additional computed columns. This way, saved searches that currently `table` a list of base columns keep working when the `index=` path is swapped to the `_pro` subdirectory - new columns are opt-in, not breaking.

### Sandboxed vs Pro side-by-side

`coingecko_volume_anomaly_detector.json` (sandboxed) - hand-rolled median heuristic:

```python
median_ratio = ratios[len(ratios) // 2] if ratios else 0.05
# ... filter where vol_mcap / median_ratio >= 2.0
```

`coingecko_volume_anomaly_detector_pro.json` (unrestricted) - statistical outlier detection:

```python
from scipy import stats
import numpy as np

ratios_arr = np.array(ratios)
mad = stats.median_abs_deviation(ratios_arr, scale='normal')
robust_z = (vol_mcap - np.median(ratios_arr)) / mad
# ... flag outliers where abs(robust_z) >= 3.0
# ... also compute percentile_rank within the population
```

Both scripts hit the same endpoint, both write `_epoch`-tagged rows, both respect the HTTP budget. The pro variant simply produces richer output columns (`z_score`, `robust_z_score`, `percentile_rank`, `is_statistical_outlier`, `anomaly_strength`) that downstream SPQL can filter on.

### Threat model when authoring unrestricted scripts

With full `__builtins__` you can:

- Import anything on `sys.path` (including `os`, `subprocess`, `socket`)
- Access the filesystem directly
- Bypass the `BudgetAwareRequests` wrapper by importing `urllib`, `http.client`, or raw sockets

The per-execution timeout, HTTP count, and row cap still apply - they are enforced by the engine layer that wraps every script run. But there is no protection against intentional misuse. Treat `trust_level: "unrestricted"` like you treat any code you paste from the internet: **read it first**.

When to refuse to escalate a script to `unrestricted`:

- You are installing it from a source you do not trust
- The sandboxed version works fine - you just prefer nicer syntax
- You cannot explain exactly which library function made the upgrade worthwhile

### Criteria for choosing `unrestricted`

Use `unrestricted` if you need:

- **Statistical computation** that scipy expresses correctly but pandas cannot (robust z-scores, curve fits, proper distributions, optimization routines)
- **Machine-learning primitives** (sklearn TF-IDF, k-means clustering, PCA, feature extraction)
- **String matching** via `rapidfuzz` that the sandbox does not expose
- **Vector math** cleaner in numpy than pandas (PCA, SVD, Nelson-Siegel decomposition)

Do NOT use `unrestricted` if:

- You only need pandas / requests / json / re - sandboxed handles those perfectly
- You want to avoid the sandbox's naming quirks (`_`-prefixed names, no tuple unpacking in `for` loops, helpers can't call other helpers) - that's not a good-enough reason
- The script will be installed by end users who shouldn't be handing over full filesystem access blindly

### Test both tiers

The test harness in `tests/test_script_library.py` reads each script's `trust_level` field and routes it through the matching executor path. Every `_pro` script must have a registry entry with expected columns that include the new scientific columns - otherwise the test suite will catch the regression at CI time.


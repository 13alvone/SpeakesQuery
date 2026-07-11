# SpeakesQuery Advanced Features

This document covers the more powerful capabilities of SpeakesQuery: subsearches, macros, joins and enrichment patterns, scheduled/saved searches, field extraction with rex, timechart workflows, lookup management, and the credential vault.

---

## Subsearches

A subsearch is a complete query enclosed in square brackets `[ ]` that executes independently. Subsearches are the mechanism for dynamic filtering, enrichment, and combining data from multiple sources.

### How subsearches execute

1. The subsearch runs **first**, producing its own result set
2. That result set is passed to the outer query as input (for filtering, joining, or appending)
3. The outer query continues processing with the subsearch results integrated

### Subsearch for dynamic filtering

Use a subsearch to narrow down the outer query based on values from another index:

```spl
index="web/access.parquet"
    [index="alerts/critical.parquet" earliest="-1d"
     | stats count by src_ip
     | fields src_ip]
| stats count by src_ip, endpoint
```

How it works:
- The subsearch queries the alerts index for the last day and returns a list of `src_ip` values
- Those values filter the outer web access query - only rows matching those IPs pass through
- The outer query then aggregates the filtered data

### Subsearch with join

Subsearches are required as the right-hand side of `join`:

```spl
index="orders/transactions.parquet"
| join type=left customer_id
    [index="customers/profiles.parquet"
     | table customer_id, customer_name, region]
```

### Subsearch with append

Subsearches provide the rows to append:

```spl
index="logs/web.parquet" | stats count as web_events by host
| append
    [index="logs/api.parquet" | stats count as api_events by host]
```

### Generating commands in subsearches

Subsearches can begin with a **generating command** like `makeresults` instead of an index call. This is useful for injecting synthetic rows:

```spl
| makeresults | eval label="real_data", value=42
| append [ | makeresults | eval label="synthetic", value=0 ]
| append [ | makeresults | eval label="baseline", value=100 ]
```

When a subsearch starts with `makeresults`, it generates a **fresh** `_epoch` independently - it does not inherit `_epoch` from the outer pipeline.

### Transforming-only subsearches

A subsearch that starts with only a transforming command (e.g. `| eval`, `| stats`) - no generating command or index call - runs against a **copy of the current result set**. All existing field values, including `_epoch`, are inherited:

```spl
| makeresults | eval status="original"
| append [ | eval status="copy" ]
```

Here, the appended row inherits `_epoch` from the outer result because `| eval` is a transforming command, not a generating command.

### Subsearch with appendpipe

`appendpipe` is different - its subsearch runs **against the current result set**, not a separate index. This is useful for adding summary rows:

```spl
index="logs/app.parquet"
| stats count by host
| appendpipe [stats sum(count) as count | eval host="TOTAL"]
```

Result: the original per-host counts plus a summary "TOTAL" row appended at the bottom.

### Nesting subsearches

Subsearches can be nested - a subsearch can contain its own subsearch:

```spl
index="web/access.parquet"
    [index="alerts/active.parquet"
        [index="config/monitored_hosts.parquet" | fields host]
     | fields src_ip]
| stats count by src_ip
```

### Subsearch caveats

- Subsearches in filtering position return fields that are used to match against the outer query's rows
- The `| fields` command at the end of a filtering subsearch is important - it defines which field(s) are used for the match
- Subsearches run with their own independent pipeline - they have no access to the outer query's data (except for `appendpipe`)

---

## Joins and Enrichment Patterns

SpeakesQuery provides several ways to combine data from multiple sources. Choosing the right one depends on your use case.

### join - merge on shared fields

Use `join` when you want to add columns from another dataset matched on a key field.

```spl
index="orders/transactions.parquet"
| join customer_id
    [index="customers/profiles.parquet" | table customer_id, customer_name]
```

**Join types**:

| Type | Behavior |
|------|----------|
| `center` (default) | Inner join - only keep rows that match in both |
| `left` | Keep all rows from the main query; add matching data from subsearch |
| `right` | Keep all rows from the subsearch; add matching data from main query |

```spl
| join type=left user_id [index="users/profiles.parquet" | table user_id, department]
```

**Multiple join fields**: Join on more than one field by listing them comma-separated:

```spl
| join host, timestamp [index="metrics/cpu.parquet" | table host, timestamp, cpu_pct]
```

### lookup - enrich from a file

Use `lookup` for simple enrichment from a reference file in the `lookups/` directory. It performs a left join internally.

```spl
index="logs/app.parquet" | lookup geoip.csv src_ip OUTPUT country, city, isp
```

Key differences from `join`:
- `lookup` always reads from a file in the lookups directory (not a subsearch)
- It's always a left join - all original rows are preserved
- It's simpler syntax for the common case of enriching with reference data

### append - stack results vertically

Use `append` when you want to add rows (not columns) from another search:

```spl
index="logs/web.parquet" | stats count as web_count
| append [index="logs/api.parquet" | stats count as api_count]
```

The result has rows from both queries stacked. Columns are unioned - if the two result sets have different columns, missing values are filled with null.

### multisearch - combine multiple independent searches

Use `multisearch` when you need to run several independent searches and combine all results:

```spl
| multisearch
    [index="logs/web.parquet" | stats count as events | eval source="web"]
    [index="logs/api.parquet" | stats count as events | eval source="api"]
    [index="logs/batch.parquet" | stats count as events | eval source="batch"]
```

All subsearches run independently and their results are concatenated (like multiple `append` calls).

### When to use which

| Goal | Command |
|------|---------|
| Add columns from another source, matched on a key | `join` or `lookup` |
| Add rows from another search | `append` |
| Add a summary row from the current data | `appendpipe` |
| Combine results from many independent searches | `multisearch` |
| Filter dynamically based on another dataset | Filtering subsearch `[ ]` |

---

## Macros

Macros are reusable query fragments stored as named definitions and invoked with backtick syntax. They use **pure text substitution** - the expansion engine replaces each backtick call with the macro's definition text before the query is parsed.

> For a comprehensive guide covering creation, parameterisation, nested macros, standardisation recipes, and best practices, see the dedicated **[Macros - Practical Guide](08_macros.md)**.

### Quick reference

**Invoke a parameterless macro:**

```spl
index="logs/app.parquet" | `exclude_noise`
```

**Invoke a parameterised macro:**

```spl
index="logs/app.parquet" | `threshold_filter(response_time, 500)`
```

Arguments are positional and comma-separated. In the macro definition, parameters are referenced with `$param$` placeholders:

```
search $field$ > $limit$
```

**Nested macros** are supported - a macro definition can contain calls to other macros. The engine expands level by level with cycle detection and a configurable depth limit.

**Managing macros:** Macros are created, edited, tested, and deleted through the **Macros** tab in the application UI. See the [Application Guide](06_application_guide.md) for step-by-step instructions.

**Expanding macros in the Query page:** The **Expand Macros** button lets you preview the fully expanded query with inline annotation comments before running it. Set the **Depth** control to expand specific nesting levels (0 = all).

---

## Field Extraction with Rex

The `rex` command is one of the most powerful tools for parsing unstructured data. It operates in two modes.

### Regex mode (default)

Extract new fields from an existing field using named capture groups:

```spl
index="logs/app.parquet"
| rex field=message "user=(?<username>\w+)\s+action=(?<action>\w+)"
```

This creates two new columns (`username` and `action`) by matching the pattern against each row's `message` field.

**Named group syntax**: Use `(?<name>pattern)` - SpeakesQuery converts this to Python's `(?P<name>pattern)` internally.

**Multiple matches**: By default, only the first match per row is extracted. Use `max_match` to extract more:

```spl
| rex field=message max_match=0 "ip=(?<ip>\d+\.\d+\.\d+\.\d+)"
```

`max_match=0` means unlimited matches. When multiple matches are found, the extracted field becomes a multi-value (list) field.

**Collision handling**: If the extracted field name already exists as a column, the new field is suffixed with `_rex` to avoid overwriting.

**Case-insensitive**: Rex matching is case-insensitive by default.

**MV-aware**: If the source field contains a list (multi-value), rex searches across all elements.

### Sed mode

Perform regex-based find-and-replace on a field's values:

```spl
index="logs/app.parquet"
| rex field=message mode=sed "s/\d{3}-\d{2}-\d{4}/XXX-XX-XXXX/"
```

**Sed syntax**: `s/<search_pattern>/<replacement>/<flags>`

- The search pattern is a regular expression
- The replacement supports backreferences (`\1`, `\2`, etc.)
- Sed mode modifies the field in place

**MV-aware**: In sed mode, if the field contains a list, the replacement is applied to each element individually.

### Practical rex patterns

```spl
# Extract key-value pairs from log lines
| rex field=_raw "(?<key>\w+)=(?<value>[^\s]+)"

# Extract IP addresses
| rex field=message "(?<ip>\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"

# Parse structured log format: [LEVEL] timestamp - message
| rex field=_raw "\[(?<level>\w+)\]\s+(?<ts>[^\-]+)\-\s+(?<msg>.*)"

# Mask sensitive data
| rex field=email mode=sed "s/(.{2}).*@/\1***@/"
```

---

## Timechart Deep Dive

`timechart` is the primary command for time-series analysis. It bins events by time and applies aggregations.

### Basic usage

```spl
index="logs/app.parquet" | timechart span=1h count
```

This counts events per hour.

### Span selection

The `span` parameter controls the time bucket size:

```spl
| timechart span=5m count          # 5-minute buckets
| timechart span=1h avg(duration)  # hourly average
| timechart span=1d sum(bytes)     # daily totals
| timechart span=1w dc(user)       # weekly unique users
```

If `span` is omitted, it defaults to `1h`.

### Splitting by a field

Use `by` to create separate series:

```spl
index="logs/app.parquet" | timechart span=1h count by status
```

This produces one count series per unique `status` value, all bucketed by hour.

### Multiple aggregations

```spl
index="metrics/system.parquet"
| timechart span=5m avg(cpu) as avg_cpu, max(cpu) as peak_cpu, avg(memory) as avg_mem
```

### How timechart works internally

1. The `_time` field is binned (floored) to the specified span using the `bin` operation
2. A `stats` aggregation is run with `_time` as a group-by field (plus any user-specified `by` field)
3. The result is a table with `_time` as the first column and aggregated metrics as additional columns

**Requirement**: The data must contain a `_time` column with datetime-parseable values.

### Timechart + eval patterns

```spl
# Calculate error rate per hour
index="logs/app.parquet"
| eval is_error=if_(status>=400, 1, 0)
| timechart span=1h sum(is_error) as errors, count as total
| eval error_rate=round(errors/total*100, 2)
```

---

## Saved Searches

Saved searches are persistent, schedulable queries stored as YAML files. They run automatically on a cron schedule and can send email alerts when results are returned.

### Saved search structure

Each saved search is a YAML file in the `saved_searches/` directory:

```yaml
name: high_error_rate
description: Alert when error rate exceeds threshold
query: |
  index="logs/app.parquet" earliest="-1h"
  | stats count as total, count(eval(status>=400)) as errors by host
  | eval error_rate=round(errors/total*100, 2)
  | search error_rate > 5
cron_schedule: '*/15 * * * *'
lookback: -4h
trigger: once
email_address: oncall@example.com
send_email: 'yes'
disabled: false
created_at: '2026-03-20T18:28:52.263171'
updated_at: '2026-03-20T20:01:12.051764'
```

### Fields explained

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier. Alphanumeric, spaces, hyphens, underscores, periods |
| `description` | No | Human-readable description |
| `query` | Yes | The SpeakesQuery query to execute |
| `cron_schedule` | Yes | Standard cron expression (e.g., `*/15 * * * *` = every 15 minutes) |
| `lookback` | Yes | How far back to search. Format: `-Ns`, `-Nm`, `-Nh`, `-Nd`, `-Nw` |
| `trigger` | No | `once` (default) - alert once per execution. `per result` - alert per row |
| `email_address` | Yes | Recipient email for alerts |
| `send_email` | No | `yes` (default) or `no` |
| `analyzer_prompt` | No | Name of a [Claude Analyzer prompt](11_claude_analyzer.md) to run against results. Empty = no analysis. |
| `analyzer_filter_enabled` | No | `true` to enable the boolean filter gate after analysis. Default `false`. |
| `analyzer_filter_question` | No | A yes/no question evaluated against the analysis. If NO, the alert is suppressed. |
| `disabled` | No | `true` to pause the schedule, `false` to keep it active |

### Cron schedule examples

| Cron Expression | Meaning |
|----------------|---------|
| `*/5 * * * *` | Every 5 minutes |
| `*/15 * * * *` | Every 15 minutes |
| `0 * * * *` | Every hour on the hour |
| `0 */6 * * *` | Every 6 hours |
| `0 9 * * 1-5` | 9 AM on weekdays |
| `0 0 * * *` | Midnight daily |
| `0 0 * * 0` | Midnight every Sunday |

### Managing saved searches

Saved searches are managed through the UI's Saved Searches panel or through the REST API:

| Action | API Endpoint | Method |
|--------|-------------|--------|
| List all | `/api/ss/` | GET |
| Create | `/api/ss/` | POST |
| Get one | `/api/ss/<name>` | GET |
| Update | `/api/ss/<name>` | PUT |
| Delete | `/api/ss/<name>` | DELETE |
| View YAML | `/api/ss/<name>/yaml` | GET |

### Soft delete and recovery

Deleted saved searches are not permanently removed - they are archived in `last_chance.sqlite` for 30 days. This provides a safety net for accidental deletions.

### Creating a saved search from a query

The UI supports a "schedule from query" flow: after running a query, you can save it as a scheduled search by providing the name, cron schedule, lookback period, and email address directly from the query page.

### Claude Analyzer integration

Saved searches can optionally route their results through the Claude Analyzer for AI-powered interpretation before alerting. This adds a structured analysis (priority, summary, actionable items, pattern detection) and can optionally filter whether an alert should be sent at all. See the [Claude Analyzer guide](11_claude_analyzer.md) for full details.

---

## Scheduled Inputs (Ingestion Scripts)

Scheduled inputs are Python scripts that run on a cron schedule to ingest data into SpeakesQuery indexes. They are the mechanism for pulling data from external sources (APIs, databases, files) on a recurring basis.

### How scheduled inputs work

1. You write a Python ingestion script that fetches data and writes it to the `indexes/` directory
2. You register it with a title, the script code, and a cron schedule
3. SpeakesQuery's scheduled input engine runs the script at each cron interval
4. The script's output becomes queryable as an index

### Available libraries

Scripts run in a RestrictedPython sandbox with access to:

`pandas`, `requests`, `json`, `datetime`, `time`, `re`, `math`, `hashlib`, `base64`, `collections`, `io`, `bs4` (BeautifulSoup4), `lxml`

This means scripts can fetch data from REST APIs **and** scrape HTML pages using BeautifulSoup with the lxml parser.

### Per-execution resource budgets

Every script run is governed by configurable resource limits (see **Settings > Ingestion**):

| Budget | Default | Description |
|--------|---------|-------------|
| **Script Timeout** | 120 sec | Wall-clock time limit. Script is killed if exceeded. |
| **Max Output Rows** | 500,000 | DataFrame rows are truncated before the parquet write. |
| **Max Requests / Execution** | 50 | Total HTTP requests (both `requests.*` and `get_cached_or_fetch()`). |
| **Max Response Size** | 10 MB | Per-response body cap. Prevents downloading unexpectedly large payloads. |

These budgets apply uniformly regardless of cron schedule. They protect against runaway scripts, memory exhaustion, and accidental crawl explosions.

### Scheduled input lifecycle

| Action | API Endpoint | Method |
|--------|-------------|--------|
| List all | `/api/scheduled-inputs/` | GET |
| Create | `/api/scheduled-inputs/` | POST |
| Get one | `/api/scheduled-inputs/<id>` | GET |
| Update | `/api/scheduled-inputs/<id>` | PUT |
| Delete | `/api/scheduled-inputs/<id>` | DELETE |
| Test run | `/api/scheduled-inputs/<id>/test` | POST |
| Enable/Disable | `/api/scheduled-inputs/<id>/toggle` | POST |
| Engine status | `/api/scheduled-inputs/status` | GET |

### Code editor features

The Python code editor on the Create Ingestion page provides:

- **Python syntax highlighting** and bracket matching via CodeMirror
- **Autocomplete** (`Ctrl`+`Space` or keystroke-triggered) for all sandbox modules, common library methods, builtins, and the SpeakesQuery API (`GENERATE_RESULTS`, `CREDENTIALS`, `get_cached_or_fetch`)
- **Live syntax linting** - a debounced server-side lint (`POST /api/si/lint`) runs `compile()` on your code and marks syntax errors in the gutter within 500ms of your last keystroke
- **Comment toggle** (`Ctrl`+`/`) and auto-indent/dedent (`Tab`/`Shift-Tab`)

### Test gate

Before a scheduled input can be saved or enabled, its code **must** pass a test execution. This prevents broken scripts from being registered.

Test runs are subject to the same resource budgets as production runs (script timeout, request count, response size). The test result includes targeted error messages for common failure modes - syntax errors (with line numbers), import restrictions, missing `GENERATE_RESULTS()` calls, credential/HTTP failures, timeout/budget exceeded, and data quality issues (missing timestamps, empty DataFrames, duplicate columns).

### Overwrite vs. Append

| Mode | Behavior | Use case |
|------|----------|----------|
| **Append** (default) | Each run writes a new uniquely-named parquet file. Over time, the compaction job merges small files into larger ones for query efficiency. All historical data is preserved. | Most ingestion tasks - API polling, log collection, event tracking. |
| **Overwrite** | Each run atomically replaces the single output file. Previous data is permanently deleted. | Point-in-time snapshots, dashboard data, status boards - where only the latest state matters. |

A warning is displayed in the UI when overwrite mode is selected. Both modes use atomic writes (write to `.tmp`, then `os.rename`) so readers never see partial data.

---

## Credential Vault

The credential vault provides encrypted storage for API keys, tokens, and secrets that ingestion scripts need to access external services.

### How it works

- Credentials are stored in a Fernet-encrypted SQLite database (`credentials.sqlite`)
- Each credential is associated with a specific ingestion script (by script ID)
- The API only exposes credential **key names** - values are never returned through the API
- Ingestion scripts can access their credentials at runtime

### API

| Action | Endpoint | Method |
|--------|----------|--------|
| List keys (names only) | `/api/credentials/<script_id>` | GET |
| Store credential | `/api/credentials/<script_id>` | POST |
| Delete credential | `/api/credentials/<script_id>/<key>` | DELETE |

### Security model

- Credential values are encrypted at rest using Fernet symmetric encryption
- The API never returns credential values - only key names are listed
- Credentials are scoped to individual scripts (no cross-script access)

---

## Lookup Management

Lookups are reference data files stored in the `lookups/` directory. They can be used for enrichment (`| lookup`) or as data sources (`| inputlookup`).

### Supported formats

| Format | Extensions |
|--------|-----------|
| CSV | `.csv` |
| TSV | `.tsv` |
| JSON | `.json` |
| YAML | `.yaml`, `.yml` |
| Parquet | `.parquet` |
| XML | `.xml` |
| SQLite | `.sqlite`, `.db` |

### Loading a lookup as a data source

```spl
| inputlookup reference_data.csv
| search region="us-east-1"
| stats count by category
```

### Enriching results from a lookup

```spl
index="logs/app.parquet"
| lookup asset_inventory.csv hostname OUTPUT owner, department, location
```

### Writing results to a lookup

```spl
index="logs/app.parquet"
| stats count by host
| outputlookup host_activity.csv
```

Options:
- `overwrite` - replace the file if it exists
- `overwrite_if_empty` - delete the file if the result set is empty
- `create_empty` - create the file even if empty

### Writing to a new file (fail if exists)

```spl
| outputnew snapshot_2024Q1.csv
```

`outputnew` will raise an error if the target file already exists, preventing accidental overwrites.

### Lookup management via UI

The UI provides a Lookup Manager panel for:
- Uploading new lookup files
- Previewing lookup contents
- Downloading lookups
- Deleting lookups

---

## The spath Command

`spath` extracts values from JSON or nested structured data using dot-notation paths.

### Syntax

```spl
| spath <source_field> output=<new_field>
```

### Example

Given a `payload` field containing `{"user": {"id": 123, "name": "Alice"}}`:

```spl
index="api/requests.parquet" | spath user.id output=user_id
```

This creates a `user_id` column with value `123`.

### Navigating nested paths

Dot notation traverses nested objects:

```spl
| spath response.headers.content_type output=content_type
| spath metadata.tags.priority output=priority
```

If any level in the path doesn't exist or the field isn't valid JSON, the result is `null`.

---

## Case Sensitivity

SpeakesQuery enforces **strict case sensitivity** for all field (column) name references. You must use the exact casing as it exists in the stored data.

### Why strict case sensitivity?

This design upholds **data immutability**: the column names your ingestion scripts produce are the authoritative names. There is no hidden transformation or guessing. What you see in the UI result headers is exactly what you type in your queries.

### Practical impact

If your data has a column named `actor`:

```spl
| stats count by actor                # Correct
| stats count by ACTOR                # WRONG - "ACTOR" does not exist
| fields actor                        # Correct
| fields Actor                        # WRONG - "Actor" does not exist
```

### What IS case-insensitive

**Command keywords** are case-insensitive - `STATS`, `Stats`, and `stats` are all valid. Only field/column names require exact casing.

```spl
| STATS count by actor                # OK - "STATS" is a command keyword
| Stats count BY actor                # OK - "Stats" and "BY" are keywords
```

### Tips

- The UI displays column headers exactly as stored - you can copy them directly into queries.
- If you're unsure of a column's casing, run a query without any transformation to see the raw headers.
- When writing ingestion scripts, choose a consistent naming convention (e.g., `snake_case`) and stick to it.

### Single-quoted field names

When a field name conflicts with a reserved word, use single quotes:

```spl
| eval 'type'=upper('type')
| stats count by 'value'
```

---

## The ! (NOT) Shorthand

SpeakesQuery supports `!` as a shorthand for `NOT` in eval expressions:

```spl
| eval is_active=if_(!isnull(last_seen), true, false)
| search !match(host, "test-.*")
```

The `!` is internally converted to `not` before evaluation. This works in `eval` expressions and `search`/`where` filters.

---

## What's Next

- **[Claude Analyzer](11_claude_analyzer.md)** - AI-powered analysis and filtering for scheduled search alerts
- **[Cookbook: "I want to..."](05_cookbook.md)** - Task-oriented recipes for common queries

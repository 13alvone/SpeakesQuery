# SpeakesQuery Commands Reference

This document covers every pipeline command available in SpeakesQuery, grouped by category. Each entry shows the syntax, describes behavior, and includes examples.

> **Convention**: In syntax blocks, `< >` denotes required arguments, `[ ]` denotes optional arguments, and `...` means the pattern repeats.

---

## Filtering Commands

### search / where

Filter events using expressions. `search` and `where` are interchangeable.

**Syntax**:
```
| search <expression> [AND|OR <expression> ...]
| where <expression>
```

**Expressions support**: comparison operators (`=` or `==`, `!=`, `>`, `<`, `>=`, `<=`), logical operators (`AND`, `OR`, `NOT`), parentheses for grouping, and the `IN` operator. Function calls like `match(field, pattern)` are also valid as a clause - useful for regex filtering inline.

**Examples**:
```spl
index="logs/app.parquet" | search status=200 AND method="GET"
index="logs/app.parquet" | where status!=404
index="logs/app.parquet" | where status == 200 AND method == "GET"
index="logs/app.parquet" | where match(host, "^web-\d+")
index="logs/app.parquet" | where match(message, "(?i)error|fail") OR severity > 3
index="logs/app.parquet" | search status IN (200, 201, 204)
index="logs/app.parquet" | search NOT (level="DEBUG" OR level="INFO")
```

**Notes**:
- Adjacent conditions without an explicit operator are implicitly ANDed.
- Field names are **case-sensitive** - use the exact casing from your data.
- String values containing spaces must be double-quoted.
- **Equality quirk**: inside a `where` clause, both `=` and `==` work as equality. Inside `if_()` and `case()` *function arguments*, equality is **always** `==` (single `=` is a syntax error there). Pick the form that's most readable for the surrounding expression - most users default to `=` in `where` and `==` in functions, but the engine accepts both forms in `where` since 2026-05-05.

---

### regex

Filter rows where a field matches (or doesn't match) a regular expression.

**Syntax**:
```
| regex <field>=<pattern>
| regex <field>!=<pattern>
```

**Examples**:
```spl
index="logs/app.parquet" | regex host="^web-\d+"
index="logs/app.parquet" | regex message!="(?i)debug"
```

**Notes**:
- The pattern is applied using `re.search` semantics - it matches anywhere in the string, not just from the start.
- Use `!=` to exclude matching rows.

---

## Column Operations

### fields

Select or exclude specific columns from the result set.

**Syntax**:
```
| fields [+|-] <field1> [, <field2>, ...]
```

**Examples**:
```spl
index="logs/app.parquet" | fields host, status, message
index="logs/app.parquet" | fields + host, status
index="logs/app.parquet" | fields - _raw, _time
```

**Notes**:
- `+` (default) keeps only the listed columns.
- `-` removes the listed columns, keeping everything else.
- Commas between field names are optional.

---

### table

Keep only the specified columns. Functionally identical to `fields +`.

**Syntax**:
```
| table <field1> [, <field2>, ...]
```

**Example**:
```spl
index="logs/app.parquet" | table host, status, duration
```

---

### maketable

Create an empty result set with the specified column names. Useful as a starting structure for `append` or pipeline testing.

**Syntax**:
```
| maketable <field1>, <field2> [, ...]
```

**Example**:
```spl
| maketable name, email, role
```

---

### rename

Rename one or more columns.

**Syntax**:
```
| rename <old_name> as <new_name> [, <old_name2> as <new_name2>, ...]
```

**Examples**:
```spl
index="logs/app.parquet" | rename src_ip as source_address
index="logs/app.parquet" | rename src_ip as source, dst_ip as destination
```

**Notes**:
- The new name can be a double-quoted string if it contains spaces: `rename src as "Source IP"`.
- Renaming to an already existing column name will proceed but may cause conflicts.

---

## Aggregation Commands

### stats

Compute aggregate statistics, optionally grouped by one or more fields. This is the most common transformation command.

**Syntax**:
```
| stats <agg_func>(<field>) [as <alias>] [, ...] [by <group_field1> [, <group_field2>, ...]]
```

**Aggregation functions**:

| Function | Description |
|----------|-------------|
| `count` or `count(<field>)` | Count all rows, or non-null values in a field |
| `sum(<field>)` | Sum of values |
| `avg(<field>)` | Mean of values |
| `min(<field>)` | Minimum value |
| `max(<field>)` | Maximum value |
| `median(<field>)` | Median value |
| `mode(<field>)` | Most frequent value |
| `dc(<field>)` | Distinct count of unique values |
| `range(<field>)` | Difference between max and min |
| `values(<field>)` | Collect all distinct values into a list |
| `first(<field>)` | First value encountered |
| `last(<field>)` | Last value encountered |
| `earliest(<field>)` | First value (alias for `first`) |
| `latest(<field>)` | Last value (alias for `last`) |

**Examples**:
```spl
index="logs/app.parquet" | stats count by status
index="logs/app.parquet" | stats avg(duration) as avg_duration, max(duration) as peak by endpoint
index="logs/app.parquet" | stats dc(user) as unique_users, count
index="logs/app.parquet" | stats values(host) as hosts by region
```

**Notes**:
- `count` without a field counts all rows. `count(field)` counts non-null values.
- Wildcard `*` as a field expands to all non-group columns.
- The `as` keyword assigns an alias to the result column; without it, the default name is `func(field)` (e.g., `avg(duration)`).
- Multiple aggregations can be comma-separated.

---

### eventstats

Compute the same aggregations as `stats`, but **append the result to each row** instead of collapsing the table. The original row count is preserved.

**Syntax**:
```
| eventstats <agg_func>(<field>) [as <alias>] [, ...] [by <group_field1>, ...]
```

**Example**:
```spl
index="logs/app.parquet"
| eventstats avg(duration) as avg_duration by endpoint
| eval deviation=duration - avg_duration
```

This adds an `avg_duration` column to every row, calculated per `endpoint` group.

---

### streamstats

Compute **cumulative** (running) statistics. For each row, the aggregation includes only that row and all preceding rows.

**Syntax**:
```
| streamstats <agg_func>(<field>) [as <alias>] [, ...] [by <group_field1>, ...]
```

**Supported functions**: `count`, `sum`, `avg`, `min`, `max`, `median`, `mode`, `dc`, `values`, `first`/`earliest`, `last`/`latest`.

**Examples**:
```spl
index="logs/app.parquet" | streamstats count as row_number
index="logs/app.parquet" | streamstats sum(bytes) as running_total by host
```

**Notes**:
- `streamstats count` without a field produces a simple row counter (1, 2, 3, ...).
- When grouped with `by`, the cumulative window resets per group.

---

### timechart

Aggregate data into time buckets for time-series analysis. Automatically bins the `_time` field.

**Syntax**:
```
| timechart [span=<timespan>] <agg_func>(<field>) [, ...] [by <split_field>]
```

**Examples**:
```spl
index="logs/app.parquet" | timechart span=1h count
index="logs/app.parquet" | timechart span=5m avg(response_time) by status
index="logs/app.parquet" | timechart span=1d sum(bytes) as total_bytes
```

**Notes**:
- Default span is `1h` if not specified.
- The `by` field splits data into separate series within each time bucket.
- Timespan units: `s` (seconds), `m`/`min` (minutes), `h` (hours), `d` (days), `w` (weeks), `y` (years). Both short and long forms work (`span=1hours` = `span=1h`).
- Requires a `_time` column in the data.

---

## Sorting & Limiting

### sort

Sort results by one or more fields.

**Syntax**:
```
| sort <direction> <field1> [, <field2>, ...]
```

Where `<direction>` is:
- `+` - ascending (A-Z, 0-9)
- `-` - descending (Z-A, 9-0)

**Examples**:
```spl
index="logs/app.parquet" | sort -count
index="logs/app.parquet" | sort +timestamp
index="logs/app.parquet" | stats count by host | sort -count
```

**Notes**:
- The direction prefix applies to **all** fields in the sort. There is no per-field direction.
- The direction is required (unlike some query languages where it's optional).

---

### head / limit

Return the first N rows. `head` and `limit` are interchangeable.

**Syntax**:
```
| head <N>
| limit <N>
```

**Examples**:
```spl
index="logs/app.parquet" | head 10
index="logs/app.parquet" | stats count by host | sort -count | limit 5
```

**Notes**:
- Default is 5 rows if N is omitted (though the grammar requires a number).
- If N exceeds the total row count, all rows are returned.

---

### reverse

Reverse the order of all rows.

**Syntax**:
```
| reverse
```

**Example**:
```spl
index="logs/app.parquet" | sort +_time | reverse
```

---

## Evaluation & Transformation

### eval

Create new fields or transform existing ones using expressions.

**Syntax**:
```
| eval <field>=<expression> [, <field2>=<expression2>, ...]
```

**Examples**:
```spl
index="logs/app.parquet" | eval duration_sec=duration/1000
index="logs/app.parquet" | eval full_name=concat(first_name, " ", last_name)
index="logs/app.parquet" | eval status_group=if_(status>=400, "error", "ok")
index="logs/app.parquet" | eval tag=upper(category), score=round(raw_score, 2)
```

**Notes**:
- Multiple assignments can be comma-separated in a single `eval`.
- The full set of eval functions is documented in [Eval & Functions Reference](03_functions.md).
- Field names that are reserved words must be single-quoted: `| eval 'type'=upper('type')`.
- Eval expressions support arithmetic (`+`, `-`, `*`, `/`), string functions, conditional logic, and nested function calls.

---

### bin

Bucket a time field into uniform intervals. Commonly used before `stats` to group by time windows.

**Syntax**:
```
| bin <field> span=<timespan>
```

**Example**:
```spl
index="logs/app.parquet" | bin _time span=1h | stats count by _time
```

**Notes**:
- The field value is floored to the nearest span boundary.
- If `span` is omitted, defaults to `1h`.
- The field must contain datetime-parseable values.

---

## Data Enrichment

### lookup

Enrich results by joining with an external lookup file (CSV, JSON, Parquet, or TSV) stored in the `lookups/` directory.

**Syntax**:
```
| lookup <filename> <key_field> [OUTPUT <output_field1>, <output_field2>, ...]
```

**Example**:
```spl
index="logs/app.parquet" | lookup geoip.csv src_ip OUTPUT country, city
```

**Notes**:
- Performs a left join on the `key_field` - all original rows are preserved.
- If `OUTPUT` is omitted, all columns from the lookup file are added.
- The lookup file must exist in the `lookups/` directory.

---

### join

Join the current result set with the output of a subsearch on one or more shared fields.

**Syntax**:
```
| join [type=<left|center|right>] <field1> [, <field2>] [<subsearch>]
```

**Join types**:
- `left` - keep all rows from the main result, add matching subsearch columns
- `center` (default / `inner`) - keep only rows that match in both
- `right` - keep all rows from the subsearch, add matching main columns

**Example**:
```spl
index="orders/transactions.parquet"
| join type=left customer_id [index="customers/profiles.parquet" | table customer_id, customer_name]
```

---

### append

Append the results of a subsearch to the bottom of the current result set. Columns are unioned.

**Syntax**:
```
| append [<subsearch>]
```

**Examples**:
```spl
# Append results from a different index
index="logs/web.parquet" | stats count as web_count
| append [index="logs/api.parquet" | stats count as api_count]

# Append with a generating command inside the subsearch
| makeresults | eval label="first"
| append [ | makeresults | eval label="second" ]

# Append with a transforming-only subsearch (inherits current data)
| makeresults | eval label="original"
| append [ | eval label="copy_with_inherited_epoch" ]
```

**Subsearch behaviour**:
- Subsearches can contain **generating commands** (`makeresults`, index calls) that produce their own data independently.
- Subsearches that start with only **transforming commands** (e.g. `| eval`, `| stats`) run against a copy of the current result set - all existing field values, including `_epoch`, are inherited.
- Subsearches support full pipelines with multiple `|` stages (e.g. `[ | makeresults | eval ... | head 5 ]`).

---

### appendpipe

Run a subsearch **against the current result set** (not a separate index), then append those results back.

**Syntax**:
```
| appendpipe [<subsearch>]
```

**Example**:
```spl
index="logs/app.parquet" | stats count by host
| appendpipe [stats sum(count) as count | eval host="TOTAL"]
```

This appends a summary row to the existing stats output.

---

### multisearch

Execute multiple independent searches and combine their results into a single table.

**Syntax**:
```
| multisearch [<search1>] [<search2>] [...]
```

**Example**:
```spl
| multisearch
    [index="logs/web.parquet" | stats count as web_events]
    [index="logs/api.parquet" | stats count as api_events]
```

---

## Deduplication & Data Quality

### dedup

Remove duplicate rows based on one or more fields.

**Syntax**:
```
| dedup [<N>] [consecutive=<true|false>] <field1> [, <field2>, ...]
```

**Arguments**:
- `N` (optional) - keep the first N occurrences of each unique combination (default: 1)
- `consecutive=true` - only remove duplicates that are adjacent (consecutive) in the data
- Fields - one or more column names that define uniqueness

**Examples**:
```spl
index="logs/app.parquet" | dedup host
index="logs/app.parquet" | dedup 3 host, status
index="logs/app.parquet" | dedup consecutive=true host
```

---

### switch

Conditional pipe-level branching. For each row, the value of `<column>` selects which case's sub-pipeline processes it. The `case "*"` catchall handles values not matched by an explicit case; rows with no match and no catchall are silently dropped (logged at INFO).

The natural pairing for `| llm` classifications: classify rows, then route each class through a different sub-pipeline.

**Syntax**:
```
| switch <column>
   case "value1" [ <subpipe_for_value1> ]
   case "value2" [ <subpipe_for_value2> ]
   case "*"      [ <catchall_subpipe> ]
```

Each case's subpipe receives only the matching rows as its input. Outputs are concatenated (column union, NaN-fill for missing columns) into the switch's final output. Within each case the input order is preserved; across cases output is grouped by case order in the directive.

**Examples**:
```spl
# Classify with | llm, route each class through its own analysis path
index="news/*.parquet" earliest=-2h
| llm model="claude-haiku-4-5-20251001" prompt="classify as urgent|routine|drop"
| switch _llm_output
   case "urgent" [ llm_batch model="claude-sonnet-4-6" prompt="deep brief" ]
   case "routine" [ stats count by source ]
   case "drop"   [ head 0 ]

# A/B routing on a feature flag
index="users/*.parquet"
| switch experiment_arm
   case "A" [ table user_id score_a ]
   case "B" [ table user_id score_b ]
   case "*" [ table user_id ]

# Different aggregations per status
index="orders/*.parquet"
| switch status
   case "completed" [ stats sum(amount) ]
   case "pending"   [ stats count ]
```

**Limitation**: subpipe text cannot contain `]` literals (same constraint as `| multisearch`). Pre-process with `| eval` if needed.

See [`docs/lang/18_llm_pipes.md`](18_llm_pipes.md) for the `| llm` + `| switch` cost-cascade composition pattern.

---

### llm_batch

Apply an LLM to the **whole DataFrame as one prompt** (vs `| llm` per-row). Serialises the input rows as a JSON array, sends one call, returns a **single-row** DataFrame containing the model's holistic response. The original input rows are gone from the output - use `| append [llm_batch ...]` if you need both.

Use this for "summarize these articles" / "rank in order" / "find the common theme" - anything where the model needs to see the whole set to do its job.

**Syntax**:
```
| llm_batch model="<registry_id>" prompt="<instruction>"
            [system="<system_prompt>"]
            [field=<column>]
            [max_rows=<N>]
            [use_cache=<true|false>]
            [max_tokens=<N>]
            [max_cost_usd=<F>]
            [dry_run=<true|false>]
```

**Arguments** (all match `| llm` except `max_rows`):
- `model`, `prompt` - required
- `system`, `field`, `use_cache`, `max_tokens`, `max_cost_usd`, `dry_run` - same semantics as `| llm`
- `max_rows` *(default 20)* - cap on rows fed into the prompt. Long-context models can override.
- `max_cost_usd` *(slice 7, optional)* - hard ceiling on the cost of THIS single call. The pre-call estimator runs first; if its estimate exceeds the cap, no provider call is made and a `budget_exceeded` row is returned instead. `0` = no cap. Unlike `| llm` (per-row cumulative), batch is one call total - the cap applies to that one call.
- `dry_run` *(slice 7, optional)* - when `true`, returns a 1-row cost preview WITHOUT making any provider call.

**Output columns**: `_llm_output`, `_llm_model`, `_llm_provider`, `_llm_cost_usd`, `_llm_latency_ms`, `_llm_status`, `_llm_error`, plus `_llm_input_row_count` (tracks truncation honestly - useful for "did we feed the model the full set or did we hit the cap?"). Dry-run mode adds `_dry_run`, `_estimated_cost_usd`, `_estimated_input_tokens`, `_estimated_output_tokens`, `_row_count`, `_max_tokens`. Budget-exceeded mode sets `_llm_status="budget_exceeded"` with the reason in `_llm_error`.

**Examples**:
```spl
# Summarise today's news
index="news/*.parquet" earliest=-24h
| nearest "fed pause" topk=20
| llm_batch model="claude-sonnet-4-6" prompt="summarize the key themes in three bullets"

# Rank candidates and explain
index="kalshi/*.parquet" | head 10
| llm_batch model="claude-haiku-4-5-20251001" prompt="rank these markets by liquidity"

# Use only one column as the input set
index="news/*.parquet" earliest=-2h
| llm_batch model="claude-sonnet-4-6" prompt="cluster these headlines" field=title

# Long-context model can take more rows
| llm_batch model="claude-opus-4-7" prompt="..." max_rows=200
```

See [`docs/lang/18_llm_pipes.md`](18_llm_pipes.md) for the full reference.

---

### llm

Apply a Large Language Model to each row of the input DataFrame. Backed by the local registry (`models/<id>.yaml`) and the slice-2 router; cache-on-by-default via the slice-3 content-hash cache (re-runs of the same prompt + model are free).

Each row's text columns are appended to your prompt inside a `<data>...</data>` boundary block before being sent to the model. The model output lands in a new `_llm_output` column alongside cost / latency / status / error metadata.

**Syntax**:
```
| llm model="<registry_id>" prompt="<instruction>"
      [system="<system_prompt>"]
      [field=<column>]
      [use_cache=<true|false>]
      [max_tokens=<N>]
      [max_cost_usd=<F>]
      [dry_run=<true|false>]
```

**Arguments**:
- `model` *(required)* - registry id of the model to call (e.g. `claude-haiku-4-5-20251001`, `lmstudio-remote`)
- `prompt` *(required)* - operator instructions; row content is appended below in a `<data>` block
- `system` *(optional)* - system prompt threaded through to the provider
- `field` *(optional)* - embed only this column. Default: concatenate all auto-detected text columns
- `use_cache` *(optional, default `true`)* - reuse cached responses keyed by content hash. Cache hits return `_llm_cost_usd=0.0` and `_llm_latency_ms=0`
- `max_tokens` *(optional)* - override the per-record output cap from the registry
- `max_cost_usd` *(slice 7, optional)* - hard ceiling on cumulative cost (USD). The pre-call estimator checks each row before dispatch - if the next call would push cumulative + estimate past the cap, processing stops and a sentinel row with `_llm_status="budget_exceeded"` is appended. `0` = no cap (unlimited). Cache hits don't advance the cumulative ($0 cost) but the gate is conservative-by-design.
- `dry_run` *(slice 7, optional, default `false`)* - when `true`, returns a 1-row cost preview WITHOUT calling any provider. Use this before running a large pipe.

**Output columns added**: `_llm_output`, `_llm_model`, `_llm_provider`, `_llm_cost_usd`, `_llm_latency_ms`, `_llm_status`, `_llm_error`. Per-row error capture: a failure on one row does NOT fail the whole pipe; the errored row gets `_llm_status="error"` and downstream pipes can `| where _llm_status="success"` to filter. Dry-run mode adds `_dry_run`, `_estimated_cost_usd`, `_estimated_input_tokens`, `_estimated_output_tokens`, `_row_count`, `_max_tokens`. Budget-exceeded boundary rows have `_llm_status="budget_exceeded"` and the reason in `_llm_error`.

**Examples**:
```spl
# Score each headline with a cheap local-cascade model
index="news/*.parquet" earliest=-2h
| nearest "geopolitical risk" topk=50
| llm model="ollama-llama3-1-8b" prompt="rate 1-10 as JSON: {score, reason}"

# Use only one column as input
index="news/*.parquet"
| llm model="claude-haiku-4-5-20251001" prompt="extract entities" field=title

# Force a fresh call (no cache)
index="kalshi/*.parquet"
| llm model="claude-sonnet-4-6" prompt="..." use_cache=false

# Total spend on a brief
index="news/*.parquet" earliest=-2h
| llm model="claude-haiku-4-5-20251001" prompt="..."
| stats sum(_llm_cost_usd) as total_cost_usd

# Pre-flight: how much would this cost? (no provider calls)
index="news/*.parquet" earliest=-24h
| llm model="claude-sonnet-4-6" prompt="rate 1-10" dry_run=true

# Hard ceiling: stop after $0.50 cumulative
index="news/*.parquet" earliest=-24h
| llm model="claude-haiku-4-5-20251001" prompt="..." max_cost_usd=0.50
```

See [`docs/lang/18_llm_pipes.md`](18_llm_pipes.md) for the full reference (boundary-tag pattern, cost-cascade examples, cache semantics, budget gate, dry-run).

---

### llm_route

Confidence-based two-stage cost cascade in one pipe: a cheap model runs on every row; rows whose parsed confidence falls below the threshold (or that errored, or didn't parse to a number) escalate to an expensive model. The standard `_llm_*` columns carry whichever stage's output was final.

**Syntax**:
```
| llm_route model="<cheap_id>" prompt="<instruction>" escalate_to="<expensive_id>"
      [escalate_prompt="<override>"] [confidence_threshold=<F>]
      [system="..."] [field=<column>] [use_cache=<true|false>] [max_tokens=<N>]
      [max_cost_usd=<F>] [dry_run=<true|false>]
```

**Key arguments**:
- `model` / `prompt` *(required)* - stage-1 (cheap) model + prompt. Prompt-engineer for a numeric 0-1 confidence ("Output ONLY a number")
- `escalate_to` *(required)* - stage-2 (expensive) model id; `escalate_prompt` overrides the prompt for that stage (default: same prompt)
- `confidence_threshold` *(default `0.5`)* - stage-1 confidence below this escalates
- `max_cost_usd` / `dry_run` - the mandatory slice-7 cost gate: hard cumulative budget ceiling (spans BOTH stages) + zero-call worst-case cost preview

**Output columns added**: the standard `_llm_*` set plus `_llm_route_escalated` (bool), `_llm_route_stage_1_output` (audit copy of the cheap output), `_llm_route_confidence` (float, NaN if unparseable).

**Example**:
```spl
index="news/*.parquet" earliest=-1d
| llm_route model="ollama-llama3-1-8b"
    prompt="Score how much this is a Fed-rate news event 0-1. Output ONLY a number."
    escalate_to="claude-sonnet-4-6" confidence_threshold=0.6 max_cost_usd=0.50
| where _llm_route_confidence >= 0.7
```

See [`docs/lang/18_llm_pipes.md`](18_llm_pipes.md) for confidence-parsing strategies, escalation triggers, and the cost-economics worked example.

---

### llm_refine

Drafter/critic refinement loop: per row, a drafter model writes a draft, a critic model critiques it, and the drafter revises - up to `max_rounds` cycles, with optional early stop when the critique contains a convergence phrase. Costs MORE than one-shot; use when output quality is worth N× the spend.

**Syntax**:
```
| llm_refine drafter_model="<id>" critic_model="<id>"
      drafter_prompt="<instruction>" critic_prompt="<instruction>"
      [revise_prompt="<template>"] [max_rounds=<N>] [converge_when_critic_says="<str>"]
      [system="..."] [field=<column>] [use_cache=<true|false>] [max_tokens=<N>]
      [max_cost_usd=<F>] [dry_run=<true|false>]
```

**Key arguments**:
- `drafter_model` / `critic_model` *(required)* - may be the same registry id
- `drafter_prompt` / `critic_prompt` *(required)* - initial-draft and critique instructions; `revise_prompt` overrides the round-2+ "incorporate the critique" template
- `max_rounds` *(default `3`)* - cap on drafter→critic cycles per row
- `converge_when_critic_says` *(optional)* - case-insensitive substring (e.g. `"APPROVED"`) that short-circuits the loop
- `max_cost_usd` / `dry_run` - the mandatory slice-7 cost gate, cumulative across all rows AND all rounds; dry-run previews the worst case (full max_rounds everywhere) with zero provider calls

**Output columns added**: the standard `_llm_*` set (final revision; cost/latency cumulative) plus `_llm_refine_rounds`, `_llm_refine_drafts` (JSON array), `_llm_refine_critiques` (JSON array), `_llm_refine_converged` (bool).

**Example**:
```spl
index="news/*.parquet" earliest=-1d
| llm_refine drafter_model="claude-haiku-4-5-20251001" critic_model="claude-sonnet-4-6"
    drafter_prompt="Write a 2-sentence summary for a financial daily brief."
    critic_prompt="Accurate, concise, jargon-free? Reply APPROVED if so, otherwise specify ONE edit."
    max_rounds=3 converge_when_critic_says="APPROVED" max_cost_usd=0.25
| where _llm_refine_converged = true
```

See [`docs/lang/18_llm_pipes.md`](18_llm_pipes.md) for the default revise template, per-round error handling, and cost economics.

---

### llm_ensemble

Multi-model voting: sends the SAME prompt to N registered models per row and aggregates by majority vote, numeric average, or unanimous-required. Cost is linear in the model count - but disagreement becomes a structural signal (`_llm_ensemble_agreement`).

**Syntax**:
```
| llm_ensemble models="<id1>,<id2>[,...]" prompt="<instruction>"
      [aggregator="majority|average|unanimous"] [min_agreement=<F>]
      [system="..."] [field=<column>] [use_cache=<true|false>] [max_tokens=<N>]
      [max_cost_usd=<F>] [dry_run=<true|false>]
```

**Key arguments**:
- `models` *(required)* - comma-separated registry ids, at least 2
- `aggregator` *(default `"majority"`)* - `majority` (plurality, case-insensitive), `average` (mean of parsed numeric outputs), `unanimous` (any dissent or errored model → `no_consensus`)
- `min_agreement` *(default `0.0`)* - require at least this fraction of agreement, else `_llm_status="no_consensus"`
- `max_cost_usd` / `dry_run` - the mandatory slice-7 cost gate, cumulative across rows × models; dry-run previews every-row-×-every-model with zero provider calls

**Output columns added**: the standard `_llm_*` set (aggregated winner; `_llm_provider="ensemble"`) plus `_llm_ensemble_models`, `_llm_ensemble_outputs` (JSON arrays), `_llm_ensemble_agreement` (0-1), `_llm_ensemble_aggregator`.

**Example**:
```spl
index="news/*.parquet" earliest=-1d
| llm_ensemble models="ollama-llama3-1-8b,claude-haiku-4-5-20251001,claude-sonnet-4-6"
    prompt="Is this market-moving news? Reply YES or NO."
    aggregator="unanimous" max_cost_usd=0.20
| where _llm_status = "success" | where _llm_output = "YES"
```

See [`docs/lang/18_llm_pipes.md`](18_llm_pipes.md) for the three aggregators, `min_agreement` recipes, and per-model error isolation.

---

### llm_until

Convergence loop with a hard ceiling: calls the same model up to `max_iterations` times per row, feeding each round's output back into the next, and stops early when any configured convergence trigger fires. `max_iterations` is **required with no default** - runaway loops are the failure mode this cap exists to prevent.

**Syntax**:
```
| llm_until model="<registry_id>" prompt="<instruction>" max_iterations=<N>
      [iterate_prompt="<template>"] [converge_when_output_contains="<str>"]
      [converge_when_output_unchanged=<true|false>] [converge_when_below_confidence=<F>]
      [system="..."] [field=<column>] [use_cache=<true|false>] [max_tokens=<N>]
      [max_cost_usd=<F>] [dry_run=<true|false>]
```

**Key arguments**:
- `max_iterations` *(REQUIRED, no default)* - hard per-row ceiling
- Convergence triggers (any one fires → stop): `converge_when_output_contains` (case-insensitive substring sentinel like `"DONE"`), `converge_when_output_unchanged=true` (output stabilised vs the prior round), `converge_when_below_confidence` (parsed confidence dropped below a float threshold). With none set, the loop always runs to `max_iterations`
- `iterate_prompt` *(optional)* - overrides the round-2+ continuation template (placeholders `{prompt}`, `{prev_output}`)
- `max_cost_usd` / `dry_run` - the mandatory slice-7 cost gate, cumulative across rows × iterations; dry-run previews full-max_iterations worst case with zero provider calls

**Output columns added**: the standard `_llm_*` set (latest iteration; cost/latency cumulative) plus `_llm_until_iterations`, `_llm_until_outputs` (JSON array), `_llm_until_converged` (bool), `_llm_until_convergence_reason` (`contains` / `unchanged` / `low_confidence` / `max_iterations` / `budget_exceeded`).

**Example**:
```spl
index="news/*.parquet" earliest=-1d
| llm_until model="claude-sonnet-4-6"
    prompt="Write a 2-sentence summary. If already optimal, output exactly 'OPTIMAL: <summary>'."
    max_iterations=4 converge_when_output_contains="OPTIMAL" max_cost_usd=1.00
| where _llm_until_converged = true
```

See [`docs/lang/18_llm_pipes.md`](18_llm_pipes.md) for the default iterate template, trigger semantics, and how `| llm_until` differs from `| llm_refine`.

---

### dedup_semantic

Drop near-duplicate rows by semantic similarity (cosine of the row's text-column embedding). Keeps the first occurrence in each cluster; subsequent rows whose similarity to any kept row meets or exceeds the threshold are dropped.

Use this *before* an LLM stage that fans out across rows - it's a token-cost reduction lever for any pipeline whose inputs include duplicate or paraphrased content (multiple news outlets covering the same story, multiple prediction markets framing the same event, etc.).

**Syntax**:
```
| dedup_semantic [threshold=<F>] [field=<column>]
```

**Arguments**:
- `threshold` (optional, default `0.85`) - cosine cutoff in the range `[-1.0, 1.0]`; pairs at or above this are duplicates
- `field` (optional) - embed only this column. Default: concatenate all text columns

**Examples**:
```spl
index="news/*.parquet" earliest=-2h | dedup_semantic threshold=0.85
index="kalshi/*.parquet" | dedup_semantic threshold=0.90 field=question
```

See [17_semantic_search.md](17_semantic_search.md) for the full design rationale, threshold tuning, and cost model.

---

### nearest

Rank rows by cosine similarity to a free-text query. Adds a `_similarity` column, sorts the result descending, and (optionally) trims to the top *K* and/or drops rows below a threshold. Powered by a local sentence-transformer model - no cloud calls.

This is the SPQL primitive for *semantic* matching: it catches paraphrases, synonyms, and conceptual neighbours that no keyword `OR`-list captures cleanly.

**Syntax**:
```
| nearest "<query string>" [topk=<N>] [threshold=<F>] [field=<column>]
```

**Arguments**:
- The first argument is the query string in double quotes (required)
- `topk` (optional, default `10`) - keep the top *N* rows by similarity. `topk=0` returns all rows sorted
- `threshold` (optional) - drop rows below this cosine similarity (range `[-1.0, 1.0]`)
- `field` (optional) - embed only this column. Default: concatenate all text columns

**Examples**:
```spl
index="news/*.parquet" earliest=-24h | nearest "fed pause" topk=10
index="news/*.parquet" | nearest "geopolitical risk" threshold=0.4 field=headline
index="kalshi/*.parquet" | nearest "fed rate decision may 2026" topk=5
```

The query "fed pause" finds rows about "FOMC holds steady", "Powell skips a hike", "central bank pauses tightening" - variants no `OR`-list catches cleanly. See [17_semantic_search.md](17_semantic_search.md) for the full reference.

---

### fieldsummary

Generate summary statistics for every field in the current result set.

**Syntax**:
```
| fieldsummary
```

**Output columns**: `field`, `count`, `distinct_count`, `is_exact`, `max`, `min`, `mean`, `stdev`, `numeric_count`, `values` (top 10 values with counts).

**Example**:
```spl
index="logs/app.parquet" | fieldsummary
```

---

### fillnull

Replace null/missing values with a specified default.

**Syntax**:
```
| fillnull value=<default> [<field1> <field2> ...]
```

**Examples**:
```spl
index="logs/app.parquet" | fillnull value="N/A" region
index="logs/app.parquet" | fillnull value=0
```

**Notes**:
- If no fields are listed, all columns are filled.
- The fill value is always treated as a string unless the field is numeric.

---

### coalesce

Return the first non-null, non-empty value from a list of fields.

**Syntax**:
```
| coalesce(<field1>, <field2> [, ...])
```

**Example**:
```spl
index="logs/app.parquet" | coalesce(preferred_email, backup_email, username)
```

**Notes**:
- Creates a new column named `coalesce` with the result.
- Also available as an eval function: `| eval contact=coalesce(email, phone)`.

---

## Field Extraction

### rex

Extract new fields from an existing field using named capture groups in a regular expression, or perform sed-mode substitution.

**Syntax (regex mode)**:
```
| rex field=<source_field> [max_match=<N>] "<regex_with_named_groups>"
```

**Syntax (sed mode)**:
```
| rex field=<source_field> mode=sed "s/<search>/<replace>/<flags>"
```

**Examples**:
```spl
# Extract error codes from log messages
index="logs/app.parquet" | rex field=message "error=(?<error_code>\d+)"

# Extract multiple fields
index="logs/app.parquet" | rex field=_raw "host=(?<host>\w+)\s+status=(?<status>\d+)"

# Sed mode: replace sensitive data
index="logs/app.parquet" | rex field=message mode=sed "s/\d{4}-\d{4}/XXXX-XXXX/"

# Extract up to 5 matches per row
index="logs/app.parquet" | rex field=message max_match=5 "ip=(?<ip>\d+\.\d+\.\d+\.\d+)"
```

**Notes**:
- Named groups use `(?<name>pattern)` syntax (traditional query language style) - these are converted to Python's `(?P<name>pattern)` internally.
- `max_match=0` means unlimited matches.
- If an extracted field name already exists, the new field is suffixed with `_rex`.
- Rex is MV-aware: if the source field contains a list, it searches across all elements.
- Regex matching is case-insensitive by default.

---

## Output Commands

### outputlookup

Write the current result set to a file in the `lookups/` directory.

**Syntax**:
```
| outputlookup <filename> [overwrite] [overwrite_if_empty] [create_empty]
```

**Supported formats**: CSV, TSV, JSON, YAML, SQLite (determined by file extension).

**Examples**:
```spl
index="logs/app.parquet" | stats count by host | outputlookup host_counts.csv
index="logs/app.parquet" | stats count by host | outputlookup results.json overwrite
```

**Notes**:
- `overwrite` and `append` are mutually exclusive.
- `overwrite_if_empty` (default behavior) removes the file if the result set is empty.

---

### outputnew

Write results to a new file. **Fails if the file already exists**.

**Syntax**:
```
| outputnew <filename>
```

**Example**:
```spl
index="logs/app.parquet" | table host, status | outputnew snapshot_20240101.csv
```

---

### base64

Encode or decode fields using Base64.

**Syntax**:
```
| base64 <encode|decode> <field1> [, <field2>, ...]
```

**Examples**:
```spl
index="logs/app.parquet" | base64 encode payload
index="logs/app.parquet" | base64 decode encoded_message
```

---

## Multi-Value (MV) Commands

Multi-value fields are cells that contain a list of values (e.g., from `values()` aggregation or `split()`). These commands operate on those lists.

### mvexpand

Expand a multi-value field into separate rows - one row per value. All other fields are duplicated.

**Syntax**:
```
| mvexpand <field>
```

**Example**:
```spl
index="logs/app.parquet" | stats values(status) as statuses by host | mvexpand statuses
```

---

### mvjoin

Join multi-value elements into a single string using a delimiter.

**Syntax**:
```
| mvjoin("<delimiter>", <field>)
```

**Example**:
```spl
index="logs/app.parquet" | stats values(host) as hosts by region | mvjoin(", ", hosts)
```

---

### mvindex

Extract a specific element from a multi-value field by its index (0-based).

**Syntax**:
```
| mvindex(<field>, <index>)
```

**Example**:
```spl
| mvindex(hosts, 0)
```

Negative indices work: `-1` returns the last element.

---

### mvfind

Find the index of the first element matching a regex pattern in a multi-value field. Returns `-1` if no match.

**Syntax**:
```
| mvfind(<field>, <pattern>)
```

**Example**:
```spl
| mvfind(hosts, "web-.*")
```

Creates a new column named `mvfind` with the index result.

---

### mvdedup

Remove duplicate values within each multi-value field cell, preserving order.

**Syntax**:
```
| mvdedup(<field>)
```

**Example**:
```spl
| mvdedup(tags)
```

---

### mvappend

Concatenate values from multiple fields into a single multi-value field.

**Syntax**:
```
| mvappend(<field1>, <field2> [, ...])
```

**Example**:
```spl
| mvappend(src_ip, dst_ip)
```

The result is stored in the first field name.

---

### mvfilter

Filter elements within a multi-value field, keeping only those that match a condition.

**Syntax**:
```
| mvfilter(<expression>)
```

**Example**:
```spl
| eval codes=split(status_codes, ",") | mvfilter(codes="200")
```

Also available as an eval function for predicate-based filtering. See [Eval & Functions Reference](03_functions.md).

---

### mvcombine

Combine multi-value list elements into a single delimited string (similar to `mvjoin`).

**Syntax**:
```
| mvcombine(<field>, "<delimiter>")
```

**Example**:
```spl
| mvcombine(tags, ";")
```

---

### mvcount

Count the number of elements in a multi-value field. Adds a new column named `<field>_count`.

**Syntax**:
```
| mvcount(<field>)
```

**Example**:
```spl
| mvcount(hosts)
```

---

### mvdc

Count the number of **distinct** elements in a multi-value field. Adds a column named `<field>_dc`.

**Syntax**:
```
| mvdc(<field>)
```

**Example**:
```spl
| mvdc(tags)
```

---

### mvreverse

Reverse the order of elements in a multi-value field.

**Syntax**:
```
| mvreverse(<field>)
```

---

### mvzip

Zip two multi-value fields element-wise, joining corresponding elements with a delimiter.

**Syntax**:
```
| mvzip(<field1>, <field2>, "<delimiter>")
```

**Example**:
```spl
| mvzip(keys, values, "=")
```

Creates a new column named `mvzip` with results like `["key1=val1", "key2=val2"]`.

---

## Special Commands

### spath

Extract a nested path from a JSON or structured field into a new column.

**Syntax**:
```
| spath <source_field> output=<new_field>
```

**Example**:
```spl
index="logs/app.parquet" | spath payload output=user_id
```

The path is specified as the source field name using dot notation: if `payload` contains `{"user": {"id": 123}}`, then `| spath user.id output=uid` extracts `123`.

---

### inputlookup

Load an entire lookup file as the starting data source for a query.

**Syntax**:
```
| inputlookup <filename>
```

**Supported formats**: CSV, JSON, Parquet, TSV.

**Example**:
```spl
| inputlookup hosts.csv | search region="us-east-1"
```

---

### makeresults

Generate a synthetic result set. This is a **generating command** - it produces its own data rather than transforming upstream results. Typically used for testing, prototyping queries, and building logical SPQL.

**Syntax**:
```
| makeresults [count=<N>] [annotate=<true|false>]
```

**Parameters**:

| Parameter   | Default | Description                                              |
|-------------|---------|----------------------------------------------------------|
| `count`     | `1`     | Number of rows to generate                               |
| `annotate`  | `false` | When `true`, adds `_raw` (empty string) and `server` (`local`) columns |

**Output**: Each row contains a single `_epoch` field set to the current Unix epoch at generation time.

**Examples**:
```spl
# Generate a single row and add a field
| makeresults
| eval message="Hello, World!"

# Generate 5 rows with sequential IDs
| makeresults count=5
| streamstats count as id

# Build test data with append
| makeresults | eval status="OK", code=200
| append [ | makeresults | eval status="ERROR", code=500 ]
| append [ | makeresults | eval status="WARN", code=429 ]
```

**Subsearch behaviour**:

When `makeresults` appears inside a subsearch (e.g. `| append [ | makeresults | eval ... ]`), it generates a **fresh** `_epoch` value independently - it does not copy `_epoch` from the outer pipeline.

Conversely, a bare transforming subsearch without a generating command (e.g. `| append [ | eval field="value" ]`) runs against a **copy** of the current result set and inherits all existing field values including `_epoch`. This is consistent with standard pipe-delimited query language behaviour.

---

### addinfo

Add informational metadata fields about the current search to every row. This is a **transforming command** - it enriches existing results.

**Syntax**:
```
| addinfo
```

**Output fields**:

| Field              | Description                                                    |
|--------------------|----------------------------------------------------------------|
| `info_min_time`    | Earliest `_epoch` value in the current result set (or `0` if no `_epoch` column) |
| `info_max_time`    | Latest `_epoch` value in the current result set (or `0` if no `_epoch` column)   |
| `info_sid`         | Synthetic search ID derived from the query                     |
| `info_search_time` | Unix epoch at the moment `| addinfo` was executed              |

**Examples**:
```spl
# Add search metadata to indexed results
index="logs/app.parquet" | addinfo

# Combine with makeresults to inspect metadata
| makeresults count=3 | addinfo | table _epoch, info_min_time, info_max_time, info_search_time
```

**Notes**:
- If the result set does not contain an `_epoch` column, `info_min_time` and `info_max_time` default to `0`.
- `info_sid` is generated uniquely per search execution.

---

### loadjob

Reload results from a previously executed query using its job ID or custom name.

**Syntax**:
```
| loadjob "<job_id>"
| loadjob "<custom_name>"
```

**Metadata fields**: When results are loaded, two columns are automatically appended:

| Field                 | Description                                                         |
|-----------------------|---------------------------------------------------------------------|
| `_loadjob_time`       | Raw epoch value (float) extracted from the job ID                   |
| `_loadjob_time_human` | Human-readable UTC timestamp (e.g. `2026-03-21 20:12:26 UTC`)      |

These fields are derived from the epoch prefix encoded in every job ID (`<epoch>_<uuid>` format). When loading by custom name, the system looks up the underlying job ID to extract the epoch.

**Examples**:
```spl
| loadjob "1710000000.123456_abc-uuid-here"
| loadjob "my_saved_search"
| loadjob "1710000000.123456_abc-uuid-here" | table _loadjob_time_human, status, count
```

---

### Macros (backtick syntax)

Invoke a saved macro (reusable query fragment) with named arguments.

**Syntax**:
```
| `macro_name(arg1="value1", arg2="value2")`
```

**Example**:
```spl
index="logs/app.parquet" | `normalize_timestamps(field="event_time")`
```

Macros are defined as YAML files in the project's macro directory. Arguments are substituted into the macro's query template.

---

## Command Quick Reference

| Command | Category | Purpose |
|---------|----------|---------|
| `search` / `where` | Filtering | Filter rows by expression |
| `regex` | Filtering | Filter rows by regex |
| `fields` | Columns | Select or exclude columns |
| `table` | Columns | Keep only listed columns |
| `maketable` | Columns | Create empty table with headers |
| `rename` | Columns | Rename columns |
| `stats` | Aggregation | Aggregate with grouping |
| `eventstats` | Aggregation | Aggregate and attach to every row |
| `streamstats` | Aggregation | Cumulative/running aggregation |
| `timechart` | Aggregation | Time-bucketed aggregation |
| `sort` | Sorting | Sort by columns |
| `head` / `limit` | Sorting | First N rows |
| `reverse` | Sorting | Reverse row order |
| `eval` | Transform | Create/transform fields |
| `bin` | Transform | Bucket time values |
| `lookup` | Enrichment | Join with lookup file |
| `join` | Enrichment | Join with subsearch |
| `append` | Enrichment | Append subsearch results |
| `appendpipe` | Enrichment | Append piped subsearch |
| `multisearch` | Enrichment | Combine multiple searches |
| `dedup` | Data Quality | Remove duplicates |
| `fieldsummary` | Data Quality | Field statistics summary |
| `fillnull` | Data Quality | Replace nulls |
| `coalesce` | Data Quality | First non-null from fields |
| `llm_route` | AI | Confidence-based cost-cascade routing (cheap model, escalate low-confidence rows) |
| `llm_refine` | AI | Drafter/critic refinement rounds |
| `llm_ensemble` | AI | Multi-model voting |
| `llm_until` | AI | Convergence loop with cost ceiling |
| `rex` | Extraction | Regex field extraction / sed |
| `spath` | Extraction | JSON path extraction |
| `outputlookup` | Output | Write to lookup file |
| `outputnew` | Output | Write to new file |
| `base64` | Output | Encode/decode Base64 |
| `mvexpand` | Multi-Value | Expand MV to rows |
| `mvjoin` | Multi-Value | Join MV to string |
| `mvindex` | Multi-Value | Get MV element by index |
| `mvfind` | Multi-Value | Find index of matching element |
| `mvdedup` | Multi-Value | Deduplicate MV elements |
| `mvappend` | Multi-Value | Concatenate fields into MV |
| `mvfilter` | Multi-Value | Filter MV elements |
| `mvcombine` | Multi-Value | MV to delimited string |
| `mvcount` | Multi-Value | Count MV elements |
| `mvdc` | Multi-Value | Distinct count of MV elements |
| `mvreverse` | Multi-Value | Reverse MV order |
| `mvzip` | Multi-Value | Zip two MV fields |
| `inputlookup` | Data Source | Load lookup as data source |
| `loadjob` | Data Source | Reload previous job results |
| `makeresults` | Generating | Generate synthetic result set |
| `addinfo` | Enrichment | Add search metadata to every row |

---

## What's Next

- **[Eval & Functions Reference](03_functions.md)** - All eval functions, stats aggregations, and MV functions in detail
- **[Advanced Features](04_advanced.md)** - Subsearches, macros, joins, scheduled searches
- **[Cookbook: "I want to..."](05_cookbook.md)** - Task-oriented recipes

# SpeakesQuery Language Fundamentals

## Overview

SpeakesQuery is a pipe-delimited query language for searching, filtering, and transforming data stored in local indexes (Parquet, SQLite, CSV, and log files). If you've used pipe-delimited query languages before, you'll be immediately at home - SpeakesQuery follows the same operational model with a few quality-of-life additions.

This document covers the foundational building blocks of every query: how queries are structured, how data flows, and the syntax primitives you'll use in every search.

---

## Query Structure

Every SpeakesQuery query follows a pipeline model. You start with a **data source**, then pipe (`|`) the results through one or more **commands** that filter, transform, or aggregate the data.

```
<data source> | <command 1> | <command 2> | ... | <command N>
```

Each pipe passes the full result set from the previous step into the next. Commands execute left to right, top to bottom.

### Minimal query

```spl
index="logs/app.parquet"
```

This returns all rows from the specified index with no filtering or transformation.

### Typical query

```spl
index="logs/app.parquet" status="error" | stats count by host | sort -count
```

This:
1. Loads the index and filters to rows where `status` equals `"error"` (inline filter)
2. Counts events grouped by `host`
3. Sorts by count descending

### Comments and whitespace

- Lines whose first non-whitespace character is `#` are **comments** and are stripped before parsing. Use them to disable a pipe segment while iterating:

  ```spl
  index="logs/app.parquet"
  # | where severity >= 4
  | stats count by host
  ```

- Leading and trailing whitespace on the whole query is trimmed automatically.
- Hash characters inside double-quoted strings (`"…"`) are preserved - only a `#` at the start of a line is treated as a comment.

---

## Data Sources

There are three ways to start a query:

### 1. Index expression (most common)

```spl
index="path/to/file"
```

The path is relative to your configured indexes directory. Supported formats include Parquet, SQLite, CSV, TSV, and log files.

You can include **inline filters** directly in the index expression - these are applied at load time:

```spl
index="web/access.parquet" status=200 method="GET"
```

Multiple conditions in the initial expression are implicitly ANDed together.

### 2. inputlookup

Load data directly from a lookup file (stored in the `lookups/` directory):

```spl
| inputlookup hosts.csv
```

### 3. loadjob

Reload results from a previously executed query by its job ID or custom name. Automatically adds `_loadjob_time` (epoch) and `_loadjob_time_human` (UTC timestamp) metadata columns:

```spl
| loadjob "1710000000.abc123_uuid-here"
| loadjob "my_saved_search"
```

### 4. makeresults

Generate a synthetic result set - typically used for testing and building logical SPQL. Creates rows with `_epoch` set to the current Unix epoch:

```spl
| makeresults
| makeresults count=5
| makeresults count=3 | eval status="OK", code=200
```

---

## Time Ranges

Time ranges restrict which events are returned from an index. They are specified in the initial expression using `earliest` and `latest`. Both must be in the **initial sequence** alongside `index=` - placing them inside a piped `| search ...` clause has no time-bound effect.

### Always quote the value

The grammar requires the value to be a quoted string or a bare integer. Unquoted forms like `earliest=-1d` or `earliest=2024-06-01` will not parse as a time bound and the whole clause is silently ignored. Always wrap the value in double quotes:

```spl
earliest="-1d"      # ✓ correct
earliest=-1d        # ✗ silently dropped
```

### Accepted value forms

| Form                          | Example                              | Notes |
|-------------------------------|--------------------------------------|-------|
| Epoch seconds (UTC)           | `earliest="1717200000"`              | Always interpreted as UTC. |
| Splunk relative time          | `earliest="-7d"`, `latest="now"`     | Anchored at "now". |
| Relative with @-snap          | `earliest="-1d@d"`, `latest="@h"`    | Snaps to start of day/hour/week. |
| ISO 8601 with explicit offset | `earliest="2024-01-01T10:00:00Z"`    | `Z` or `±HH:MM` honoured. |
| ISO 8601 tz-naive             | `earliest="2024-01-01"`              | Interpreted as **UTC** by default. |
| Inline tz suffix              | `earliest="2024-01-01/America/New_York"` | Override the default UTC interpretation. |

### Absolute times

```spl
earliest="2024-01-01" latest="2024-12-31" index="logs/app.parquet"
```

### Relative times

Relative times use a minus sign and a unit suffix:

| Suffix | Meaning |
|--------|---------|
| `s`    | Seconds |
| `m`    | Minutes |
| `h`    | Hours   |
| `d`    | Days    |
| `w`    | Weeks   |
| `M`    | Months (30d) |
| `y`    | Years (365d) |

```spl
earliest="-7d" latest="now" index="logs/app.parquet"
```

This returns events from the last 7 days.

### Snap-to-period (`@`)

Append `@<unit>` to snap the result down to the start of the period:

```spl
earliest="-1d@d"     # 1 day ago, snapped to start of day
earliest="-1h@h"     # 1 hour ago, snapped to start of hour
earliest="-7d@w"     # 7 days ago, snapped to start of week (Monday 00:00)
```

The snap is performed in the configured timezone (UTC by default; see "Timezones" below).

### Timezones

Tz-naive forms like `earliest="2024-01-01"` are interpreted as **UTC** by default. To anchor a tz-naive value (or a `@`-snap) in a specific timezone, append `/<IANA-name>`:

```spl
earliest="2024-01-01/America/New_York"      # midnight ET
earliest="-1d@d/America/New_York"           # yesterday 00:00 ET
latest="2024-06-01T09:30:00/America/New_York"   # 9:30 AM ET market open
```

The IANA timezone name (e.g. `America/New_York`, `Europe/London`, `Asia/Tokyo`) is validated against the system's `zoneinfo` database - invalid names raise an error rather than being silently ignored. Inline `/<tz>` suffixes always override any per-search timezone configured elsewhere.

ISO 8601 values that include an explicit `Z` suffix or `±HH:MM` offset (e.g. `"2024-06-01T13:30:00-04:00"`) are unambiguous and honoured as-is - no inline tz suffix needed.

### Bad input is loud

Any value that cannot be parsed as one of the forms above raises a `TimeBoundParseError` at query time and is surfaced through `process_query_with_diagnostics` as a structured diagnostic. The query returns no rows AND the operator sees the parse error - there is no "silent zero" fallback that would let the bound be dropped while pretending the query succeeded.

```spl
earliest="garbge"   # → TimeBoundParseError: earliest='garbge': Could not parse...
```

### Order doesn't matter

`earliest` and `latest` can appear in any order, and can appear before or after the `index` clause:

```spl
index="logs/app.parquet" earliest="-1d"
earliest="-30d" latest="-7d" index="logs/app.parquet"
```

Both are valid.

---

## Operators

### Comparison operators

| Operator | Meaning                  | Example              |
|----------|--------------------------|----------------------|
| `=`      | Equals                   | `status=200`         |
| `!=`     | Not equals               | `status!=404`        |
| `>`      | Greater than             | `bytes>1024`         |
| `<`      | Less than                | `duration<100`       |
| `>=`     | Greater than or equal to | `retries>=3`         |
| `<=`     | Less than or equal to    | `score<=50`          |

### Logical operators

| Operator     | Meaning    | Example                              |
|--------------|------------|--------------------------------------|
| `AND` / `and`| Logical AND | `status=200 AND method="GET"`       |
| `OR` / `or`  | Logical OR  | `status=404 OR status=500`          |
| `NOT` / `not`| Negation    | `NOT status=200`                    |
| `!`          | Negation (shorthand) | `!status=200`               |

**Precedence** (highest to lowest):
1. `NOT` / `!`
2. `AND` (implicit when conditions are adjacent with no operator)
3. `OR`

Use parentheses to override precedence:

```spl
index="logs/app.parquet" (status=404 OR status=500) AND method="POST"
```

**Implicit AND**: When two conditions are placed side by side without a logical operator, they are ANDed:

```spl
index="logs/app.parquet" status=200 method="GET"
```

is equivalent to:

```spl
index="logs/app.parquet" status=200 AND method="GET"
```

### Arithmetic operators

Used in `eval` expressions and calculations:

| Operator | Meaning        | Example                       |
|----------|----------------|-------------------------------|
| `+`      | Addition       | `eval total=price+tax`        |
| `-`      | Subtraction    | `eval diff=end-start`         |
| `*`      | Multiplication | `eval area=width*height`      |
| `/`      | Division       | `eval rate=bytes/seconds`     |

---

## The IN Operator

Test whether a field's value matches any value in a set:

```spl
index="logs/app.parquet" | search status IN (200, 201, 204)
```

Negate with `NOT`:

```spl
index="logs/app.parquet" | search NOT status IN (400, 401, 403, 404)
```

`IN` works with both numbers and strings:

```spl
| search region IN ("us-east-1", "us-west-2", "eu-west-1")
```

---

## Field References

Fields (columns) are referenced by name. SpeakesQuery enforces **strict case sensitivity** for field names - you must reference fields exactly as they are stored in the data.

```spl
| search status=200       # Correct - matches the stored column "status"
| search Status=200       # WRONG - "Status" does not exist, "status" does
| search STATUS=200       # WRONG - "STATUS" does not exist, "status" does
```

This design enforces data immutability: the column names created by your ingestion scripts are the exact names you use in queries. The UI displays column headers as-is, so you can always copy them directly into your queries.

### Quoting field names

- **Unquoted**: Simple alphanumeric names with underscores and dots: `host`, `user_name`, `src.ip`
- **Single-quoted**: Use when field names conflict with reserved words or contain special characters: `'value'`, `'type'`, `'my field'`

```spl
| eval 'type'=upper('type')
```

---

## Literals and Values

### Strings

Strings are enclosed in double quotes:

```spl
| search message="connection timeout"
```

Double quotes are required when the value contains spaces or special characters. For simple values without spaces, quotes are optional in some contexts (e.g., `status=200`), but using them is always safe.

### Numbers

Integers and decimals are written directly:

```spl
| search count>100
| eval ratio=3.14
```

Negative numbers use a leading minus: `-42`, `-3.14`

### Booleans

```spl
| search active=true
| eval is_valid=false
```

Accepted forms: `true`, `True`, `TRUE`, `false`, `False`, `FALSE`

### Null

The `null()` function represents a null/missing value:

```spl
| eval placeholder=null()
```

Use `isnull()` and `isnotnull()` to test for null values:

```spl
| search isnotnull(email)
| eval has_phone=if_(isnotnull(phone), "yes", "no")
```

---

## Comments

Lines starting with `#` are treated as comments and ignored by the parser:

```spl
# This query finds error events from the last week
earliest="-7d" index="logs/app.parquet" status="error"
| stats count by host
```

---

## Multiline Queries

Queries can span multiple lines. Line breaks are allowed between pipe segments for readability:

```spl
earliest="-7d" index="logs/app.parquet" status="error"
| stats count by host
| sort -count
| head 10
```

This is identical to writing it on one line. Whitespace (spaces, tabs, newlines) between tokens is ignored.

---

## Timespans

Several commands (`timechart`, `bin`) accept a `span` parameter that specifies a time bucket size. The syntax is a number followed by a unit:

| Syntax     | Meaning   | Example          |
|------------|-----------|------------------|
| `Ns` / `N seconds` | N seconds | `span=30s`       |
| `Nm` / `N minutes` | N minutes | `span=5m`        |
| `Nh` / `N hours`   | N hours   | `span=1h`        |
| `Nd` / `N days`    | N days    | `span=1d`        |
| `Nw` / `N weeks`   | N weeks   | `span=1w`        |
| `Ny` / `N years`   | N years   | `span=1y`        |

Both abbreviated and full unit names are accepted: `span=1hours` and `span=1h` are equivalent, as are `span=7days` and `span=7d`.

```spl
index="metrics/cpu.parquet" | timechart span=1h avg(cpu_percent) by host
index="metrics/cpu.parquet" | bin _time span=1d
```

---

## Subsearches

A subsearch is a query enclosed in square brackets `[ ]` that executes independently and feeds its results into the outer query. This is commonly used for dynamic filtering:

```spl
index="web/access.parquet"
    [index="alerts/critical.parquet" | stats count by src_ip | fields src_ip]
| stats count by src_ip
```

How it works:
1. The subsearch runs first: it queries the alerts index and returns a list of `src_ip` values
2. Those values are used to filter the outer query's results
3. The outer query then aggregates the filtered data

Subsearches can appear in `join`, `append`, and `appendpipe` commands as well:

```spl
index="orders" | join customer_id [index="customers" | table customer_id, customer_name]
```

---

## Macros

Macros are reusable query fragments defined externally and invoked using backtick syntax:

```spl
index="logs/app.parquet" | `normalize_timestamps(field="event_time")`
```

Macros accept named arguments passed as `key="value"` pairs. The macro definition (stored as a YAML file in the `macros/` directory) contains the query template with parameter placeholders.

---

## Pipeline Execution Model

Understanding how the pipeline executes helps you write efficient queries:

1. **Data source** loads the initial dataset (index call, inputlookup, or loadjob)
2. **Inline filters** in the index expression are applied at load time (most efficient)
3. Each **pipe command** receives the full result set from the previous step
4. Commands execute **sequentially** - order matters
5. The final result set is returned to the UI

### Performance tip

Filter early, aggregate late. Putting `search` or `where` commands early in the pipeline reduces the row count for subsequent operations:

```spl
# Good: filter first, then aggregate
index="logs/app.parquet" | search status!=200 | stats count by endpoint

# Less efficient: aggregate everything, then filter
index="logs/app.parquet" | stats count by endpoint, status | search status!=200
```

---

## Quick Reference: Reserved Words

The following words have special meaning in SpeakesQuery and cannot be used as unquoted field names. Use single quotes if your data has columns with these names.

`index`, `earliest`, `latest`, `search`, `where`, `eval`, `stats`, `eventstats`, `streamstats`, `timechart`, `sort`, `head`, `limit`, `fields`, `table`, `rename`, `dedup`, `reverse`, `rex`, `regex`, `bin`, `join`, `append`, `appendpipe`, `lookup`, `inputlookup`, `outputlookup`, `fillnull`, `fieldsummary`, `base64`, `coalesce`, `spath`, `multisearch`, `maketable`, `mvexpand`, `mvjoin`, `mvindex`, `mvfind`, `mvdedup`, `mvappend`, `mvfilter`, `mvcombine`, `mvcount`, `mvdc`, `mvzip`, `mvreverse`, `outputnew`, `by`, `as`, `and`, `or`, `not`, `in`, `true`, `false`, `null`, `span`, `field`, `value`, `type`, `mode`

To reference a column named `type`:

```spl
| eval 'type'=upper('type')
```

---

## What's Next

- **[Commands Reference](02_commands.md)** - Full reference for all pipeline commands
- **[Eval & Functions Reference](03_functions.md)** - All eval functions, stats aggregations, and MV functions
- **[Advanced Features](04_advanced.md)** - Subsearches, joins, timechart, rex, and scheduled searches
- **[Cookbook: "I want to..."](05_cookbook.md)** - Task-oriented recipes for common queries
- **[Application Guide](06_application_guide.md)** - UI walkthrough for every tab
- **[Macros - Practical Guide](08_macros.md)** - Creating, parameterising, nesting, and standardising reusable query fragments

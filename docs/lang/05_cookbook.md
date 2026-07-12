# SpeakesQuery Cookbook: "I want to..."

This document is organized by task. Find what you want to do, and get a ready-to-use query pattern.

---

## Filtering & Searching

### I want to filter events by a field value

```spl
index="logs/app.parquet" | search status=404
```

Multiple conditions (implicit AND):

```spl
index="logs/app.parquet" | search status=404 method="GET"
```

### I want to filter with OR logic

```spl
index="logs/app.parquet" | search status=404 OR status=500
```

Group with parentheses for complex logic:

```spl
index="logs/app.parquet" | search (status=404 OR status=500) AND method="POST"
```

### I want to check if a field matches one of several values

```spl
index="logs/app.parquet" | search status IN (200, 201, 204)
```

Negate with NOT:

```spl
index="logs/app.parquet" | search NOT status IN (400, 401, 403, 404, 500)
```

### I want to filter by time range

Absolute:

```spl
earliest="2024-01-01" latest="2024-03-31" index="logs/app.parquet"
```

Relative (last 7 days):

```spl
earliest="-7d" latest="now" index="logs/app.parquet"
```

Last 24 hours:

```spl
earliest="-24h" index="logs/app.parquet"
```

### I want to filter using a regular expression

```spl
index="logs/app.parquet" | regex message="error|fail|exception"
```

Exclude matches:

```spl
index="logs/app.parquet" | regex host!="^test-"
```

### I want to filter out null/empty values

```spl
index="logs/app.parquet" | search isnotnull(email)
```

Keep only rows where a field IS null:

```spl
index="logs/app.parquet" | search isnull(response_code)
```

### I want to negate a condition

```spl
index="logs/app.parquet" | search NOT status=200
index="logs/app.parquet" | search NOT match(host, "test-.*")
```

---

## Counting & Aggregating

### I want to count events

Total count:

```spl
index="logs/app.parquet" | stats count
```

Count by a field:

```spl
index="logs/app.parquet" | stats count by status
```

Count by multiple fields:

```spl
index="logs/app.parquet" | stats count by host, status
```

### I want to count unique values

```spl
index="logs/app.parquet" | stats dc(user) as unique_users
```

Unique values per group:

```spl
index="logs/app.parquet" | stats dc(user) as unique_users by region
```

### I want to find the top N results

```spl
index="logs/app.parquet" | stats count by endpoint | sort -count | head 10
```

### I want to find the bottom N results

```spl
index="logs/app.parquet" | stats count by endpoint | sort +count | head 10
```

### I want to calculate averages, min, max

```spl
index="metrics/response.parquet"
| stats avg(duration) as avg_ms, min(duration) as min_ms, max(duration) as max_ms, median(duration) as p50
```

Per group:

```spl
index="metrics/response.parquet"
| stats avg(duration) as avg_ms, max(duration) as peak by endpoint
```

### I want to sum values

```spl
index="logs/app.parquet" | stats sum(bytes) as total_bytes by host
```

### I want to collect all distinct values of a field

```spl
index="logs/app.parquet" | stats values(status) as all_statuses by host
```

This creates a multi-value (list) field containing every unique status per host.

### I want multiple aggregations at once

```spl
index="logs/app.parquet"
| stats count, dc(user) as unique_users, avg(duration) as avg_dur, values(status) as statuses by endpoint
```

---

## Transforming Data

### I want to create a new calculated field

```spl
index="logs/app.parquet" | eval duration_sec=duration / 1000
```

### I want to combine two fields into one

```spl
index="logs/app.parquet" | eval full_name=concat(first_name, " ", last_name)
```

### I want to categorize data with if/else logic

Two categories:

```spl
index="logs/app.parquet" | eval status_group=if_(status>=400, "error", "success")
```

Multiple categories (nested if_):

```spl
index="logs/app.parquet"
| eval severity=if_(status>=500, "critical", if_(status>=400, "warning", "ok"))
```

### I want to categorize data with multiple conditions (case)

```spl
index="logs/app.parquet"
| eval tier=case(
    response_time>5000, "critical",
    response_time>1000, "slow",
    response_time>200, "normal",
    "fast"
)
```

### I want to convert a field to uppercase/lowercase

```spl
index="logs/app.parquet" | eval host_lower=lower(hostname)
index="logs/app.parquet" | eval method_upper=upper(method)
```

### I want to extract a substring

```spl
index="logs/app.parquet" | eval prefix=substr(hostname, 0, 3)
```

### I want to replace text in a field

Plain text replacement (using regex):

```spl
index="logs/app.parquet" | eval clean=replace(message, "password=\S+", "password=***")
```

### I want to fill in missing values

Fill specific fields:

```spl
index="logs/app.parquet" | fillnull value="unknown" region, department
```

Fill all fields:

```spl
index="logs/app.parquet" | fillnull value="N/A"
```

### I want to use the first non-null value from several fields

```spl
index="logs/app.parquet" | eval contact=coalesce(email, phone, "no contact")
```

### I want to convert types

String to number:

```spl
index="logs/app.parquet" | eval port_num=tonumber(port)
```

Anything to string:

```spl
index="logs/app.parquet" | eval code_str=tostring(status)
```

### I want to round numbers

```spl
index="metrics/system.parquet" | eval cpu_pct=round(cpu_usage, 1)
```

Round to whole number:

```spl
| eval whole=round(price)
```

---

## Time-Series Analysis

### I want to count events over time

```spl
index="logs/app.parquet" | timechart span=1h count
```

### I want to see a metric trend over time

```spl
index="metrics/system.parquet" | timechart span=5m avg(cpu) as avg_cpu
```

### I want to compare categories over time

```spl
index="logs/app.parquet" | timechart span=1h count by status
```

### I want to calculate error rate over time

```spl
index="logs/app.parquet"
| eval is_error=if_(status>=400, 1, 0)
| timechart span=1h sum(is_error) as errors, count as total
| eval error_rate=round(errors / total * 100, 2)
```

### I want to see daily unique users

```spl
index="logs/app.parquet" | timechart span=1d dc(user_id) as unique_users
```

### I want to bucket a time field manually

```spl
index="logs/app.parquet" | bin _time span=1h | stats count by _time
```

---

## Column Management

### I want to keep only specific columns

```spl
index="logs/app.parquet" | table host, status, message
```

Or equivalently:

```spl
index="logs/app.parquet" | fields host, status, message
```

### I want to remove specific columns

```spl
index="logs/app.parquet" | fields - _raw, _time, internal_id
```

### I want to rename a column

```spl
index="logs/app.parquet" | rename src_ip as source_address
```

Multiple renames:

```spl
| rename src_ip as source, dst_ip as destination, ts as timestamp
```

### I want to see what fields are available

```spl
index="logs/app.parquet" | fieldsummary
```

This returns one row per field with count, distinct count, min, max, mean, stdev, and top values.

---

## Sorting & Limiting

### I want the first N rows

```spl
index="logs/app.parquet" | head 20
```

### I want to sort ascending

```spl
index="logs/app.parquet" | sort +timestamp
```

### I want to sort descending

```spl
index="logs/app.parquet" | sort -count
```

### I want to reverse the result order

```spl
index="logs/app.parquet" | sort +_time | reverse
```

---

## Deduplication

### I want to remove duplicate rows

Keep the first occurrence of each unique value:

```spl
index="logs/app.parquet" | dedup host
```

Deduplicate on multiple fields:

```spl
index="logs/app.parquet" | dedup host, status
```

### I want to keep the first N duplicates

```spl
index="logs/app.parquet" | dedup 3 host
```

Keeps up to 3 rows per unique `host`.

### I want to remove only consecutive duplicates

```spl
index="logs/app.parquet" | dedup consecutive=true host
```

This only removes duplicates that appear in adjacent rows - non-adjacent duplicates are kept.

---

## Field Extraction & Parsing

### I want to extract fields from log messages using regex

```spl
index="logs/app.parquet"
| rex field=message "status=(?<status_code>\d+)\s+duration=(?<dur>\d+)ms"
```

### I want to extract IP addresses

```spl
index="logs/app.parquet"
| rex field=_raw "(?<ip_addr>\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"
```

### I want to extract all matches (not just the first)

```spl
index="logs/app.parquet"
| rex field=message max_match=0 "user=(?<user>\w+)"
```

The `user` field becomes multi-value when multiple matches are found.

### I want to redact sensitive data

```spl
index="logs/app.parquet"
| rex field=message mode=sed "s/\b\d{3}-\d{2}-\d{4}\b/XXX-XX-XXXX/"
```

### I want to extract a value from a JSON field

```spl
index="api/events.parquet" | spath payload output=event_type
```

Nested path:

```spl
| spath response.data.user_id output=uid
```

### I want to split a delimited string into a list

```spl
index="logs/app.parquet" | eval tags=split(tag_string, ",")
```

After splitting, `tags` is a multi-value field you can further manipulate.

---

## Multi-Value Fields

### I want to expand a list into separate rows

```spl
index="logs/app.parquet"
| stats values(status) as statuses by host
| mvexpand statuses
```

Each status value becomes its own row (other columns are duplicated).

### I want to join list elements into a string

```spl
| eval tag_string=mvjoin(tags, ", ")
```

### I want to count elements in a list

```spl
| eval num_tags=mvcount(tags)
```

### I want to get a specific element from a list

First element:

```spl
| eval first=mvindex(items, 0)
```

Last element:

```spl
| eval last=mvindex(items, -1)
```

### I want to remove duplicates within a list

```spl
| eval unique_tags=mvdedup(tags)
```

### I want to sort elements within a list

```spl
| eval sorted=mvsort(items)
```

### I want to combine multiple fields into one list

```spl
| eval all_ips=mvappend(src_ip, dst_ip, nat_ip)
```

### I want to zip two lists together

```spl
| eval pairs=mvzip(keys, values, "=")
```

If `keys=["a","b"]` and `values=["1","2"]`, result is `["a=1", "b=2"]`.

### I want to filter elements within a list

Keep only elements matching a pattern:

```spl
| eval critical=mvfilter(match(events, "CRITICAL"))
```

Keep only numeric values above a threshold:

```spl
| eval high_scores=mvfilter(scores > 90)
```

### I want to find which index an element is at

```spl
| eval pos=mvfind(hosts, "web-prod-.*")
```

Returns the 0-based index of the first matching element, or `-1`.

### I want to reverse a list

```spl
| eval reversed=mvreverse(items)
```

### I want to count distinct values in a list

```spl
| mvdc(tags)
```

Creates a `tags_dc` column with the distinct count.

---

## Enrichment & Joining

### I want to enrich results with data from a CSV file

```spl
index="logs/app.parquet"
| lookup geoip.csv src_ip OUTPUT country, city
```

### I want to join two indexes on a shared field

```spl
index="orders/transactions.parquet"
| join type=left customer_id
    [index="customers/profiles.parquet" | table customer_id, customer_name, region]
```

### I want to combine results from multiple searches

```spl
| multisearch
    [index="logs/web.parquet" | stats count as events | eval source="web"]
    [index="logs/api.parquet" | stats count as events | eval source="api"]
```

### I want to append a summary row

```spl
index="logs/app.parquet"
| stats count by host
| appendpipe [stats sum(count) as count | eval host="TOTAL"]
```

### I want to dynamically filter based on another index

```spl
index="web/access.parquet"
    [index="blocklist/ips.parquet" | fields src_ip]
| stats count by src_ip
```

---

## Output & Export

### I want to save results as a CSV

```spl
index="logs/app.parquet" | stats count by host | outputlookup host_counts.csv
```

### I want to save results as JSON

```spl
index="logs/app.parquet" | stats count by host | outputlookup results.json
```

### I want to save to a new file (fail if exists)

```spl
index="logs/app.parquet" | stats count by host | outputnew snapshot.csv
```

### I want to overwrite an existing lookup

```spl
| outputlookup host_counts.csv overwrite
```

---

## Encoding & Security

### I want to Base64 encode/decode a field

```spl
index="logs/app.parquet" | eval encoded=base64_encode(payload)
index="logs/app.parquet" | eval decoded=base64_decode(encoded_data)
```

Or using the command form (modifies in place):

```spl
index="logs/app.parquet" | base64 encode payload
index="logs/app.parquet" | base64 decode encoded_payload
```

### I want to defang URLs/IPs for safe sharing

```spl
index="logs/app.parquet" | eval safe_url=defang(url)
```

`http://evil.com` becomes `http[:]//evil[.]com`

### I want to re-fang defanged indicators

```spl
| eval real_url=fang(defanged_url)
```

### I want to URL-encode/decode values

```spl
| eval encoded=urlencode(query_param)
| eval decoded=urldecode(encoded_param)
```

---

## Data Investigation

### I want a quick overview of what's in my data

```spl
index="logs/app.parquet" | fieldsummary
```

### I want to see the first few rows

```spl
index="logs/app.parquet" | head 5
```

### I want to see unique values of a field

```spl
index="logs/app.parquet" | stats values(status) as all_statuses
```

Or with counts:

```spl
index="logs/app.parquet" | stats count by status | sort -count
```

### I want to find the most recent events

```spl
index="logs/app.parquet" | sort -_time | head 10
```

### I want to find the earliest and latest timestamps

```spl
index="logs/app.parquet" | stats earliest(_time) as first_event, latest(_time) as last_event
```

### I want to see the spread/range of a numeric field

```spl
index="metrics/system.parquet"
| stats min(cpu) as low, max(cpu) as high, range(cpu) as spread, avg(cpu) as mean
```

---

## Running Totals & Row Numbers

### I want a running row count

```spl
index="logs/app.parquet" | streamstats count as row_number
```

### I want a running total

```spl
index="logs/app.parquet" | streamstats sum(bytes) as cumulative_bytes
```

### I want a running average

```spl
index="metrics/system.parquet" | streamstats avg(cpu) as running_avg_cpu
```

### I want running stats per group

```spl
index="logs/app.parquet" | streamstats count as group_row_num by host
```

---

## Attaching Aggregate Context to Every Row

### I want each row to know its group's total

```spl
index="logs/app.parquet"
| eventstats count as group_total by host
```

Every row gets a `group_total` column showing the total count for its `host`.

### I want to calculate each row's percentage of the total

```spl
index="logs/app.parquet"
| stats count by endpoint
| eventstats sum(count) as grand_total
| eval pct=round(count / grand_total * 100, 2)
```

### I want to find outliers compared to group average

```spl
index="metrics/response.parquet"
| eventstats avg(duration) as avg_dur by endpoint
| eval deviation=duration - avg_dur
| search deviation > 1000
```

---

## Working with Lookups

### I want to load and query a lookup file directly

```spl
| inputlookup reference_data.csv | search region="us-east-1" | stats count by category
```

### I want to reload results from a previous query

```spl
| loadjob "1710000000.abc123_uuid-here"
| loadjob "my_saved_search"
```

The loaded results automatically include `_loadjob_time` (epoch) and `_loadjob_time_human` (UTC timestamp) metadata.

### I want to generate test data

```spl
| makeresults count=3
| eval status="test", code=200
```

Build multi-row test data with `append`:

```spl
| makeresults | eval env="prod", status="healthy"
| append [ | makeresults | eval env="staging", status="degraded" ]
| append [ | makeresults | eval env="dev", status="healthy" ]
```

### I want to see search metadata on my results

```spl
index="logs/app.parquet" | addinfo
| table _epoch, info_min_time, info_max_time, info_sid, info_search_time
```

### I want to create an empty table structure

```spl
| maketable name, email, department, start_date
```

---

## Scheduled Searches & Alerts

### I want to create a search that runs every 15 minutes

Create a YAML file in `saved_searches/`:

```yaml
name: error_monitor
description: Check for high error rates
query: |
  earliest="-15m" index="logs/app.parquet"
  | stats count as errors by host
  | search errors > 100
cron_schedule: '*/15 * * * *'
lookback: -1h
trigger: once
email_address: oncall@example.com
send_email: 'yes'
disabled: false
```

Or use the UI's "Schedule from Query" feature after running the query.

### I want a daily summary report

```yaml
name: daily_summary
query: |
  earliest="-24h" index="logs/app.parquet"
  | stats count as total, dc(host) as hosts, dc(user) as users
  | eval report_date=now()
cron_schedule: '0 8 * * *'
lookback: -24h
trigger: once
email_address: team@example.com
send_email: 'yes'
disabled: false
```

---

## Common Patterns & Recipes

### Count events and show percentage breakdown

```spl
index="logs/app.parquet"
| stats count by status
| eventstats sum(count) as total
| eval pct=round(count / total * 100, 1)
| sort -count
| table status, count, pct
```

### Find first and last occurrence of each value

```spl
index="logs/app.parquet"
| stats earliest(_time) as first_seen, latest(_time) as last_seen, count by host
```

### Compare this week to last week

```spl
earliest="-7d" index="logs/app.parquet" | stats count as this_week
| append [earliest="-14d" latest="-7d" index="logs/app.parquet" | stats count as last_week]
```

### Create a lookup from query results for reuse

```spl
index="logs/app.parquet"
| stats dc(user) as unique_users, count as total_events by host
| outputlookup host_activity.csv
```

Then use it later:

```spl
| inputlookup host_activity.csv | sort -unique_users | head 10
```

### Chain multiple transformations

```spl
index="logs/app.parquet" earliest="-24h"
| rex field=message "endpoint=(?<endpoint>/[^\s]+)"
| eval endpoint=lower(trim(endpoint))
| stats count, avg(duration) as avg_ms by endpoint
| eval avg_ms=round(avg_ms, 1)
| sort -count
| head 20
| rename count as requests, avg_ms as "Avg Response (ms)", endpoint as Endpoint
```

### Identify anomalous hosts (more than 2x average)

```spl
index="logs/app.parquet"
| stats count by host
| eventstats avg(count) as avg_count
| eval ratio=round(count / avg_count, 2)
| search ratio > 2
| sort -ratio
| table host, count, avg_count, ratio
```

### Build a summary dashboard table

```spl
index="logs/app.parquet" earliest="-1d"
| stats count as events,
        dc(user) as users,
        dc(host) as hosts,
        avg(duration) as avg_duration,
        max(duration) as peak_duration
        by endpoint
| eval avg_duration=round(avg_duration, 0)
| sort -events
| head 25
```

## Options Edge Brief - performance attribution dashboard (Wave 2)

These queries read the IMMUTABLE pick journal (`indexes/IMMUTABLE/ag_picks/`) and the deterministic closure stream (`indexes/IMMUTABLE/ag_picks_closures/`) populated by the `oeb_pick_tracker_pro` ingestion script. Use them to grade the brief over weeks of paper-trading before funding the real-money account.

### Past 30 days hit rate (overall AND account-fit)

```spl
index="indexes/IMMUTABLE/ag_picks_closures/*.parquet"
| where alert_group="options_edge_brief"
| eval ago_30d = now() - 2592000
| where _epoch >= ago_30d
| eval is_win = if_(outcome="won", 1, 0)
| eval is_account_fit = if_(fits_account_at_entry=true, 1, 0)
| eval is_account_fit_win = if_(outcome="won" AND fits_account_at_entry=true, 1, 0)
| stats count as total,
        sum(is_win) as wins,
        sum(is_account_fit) as account_fit_total,
        sum(is_account_fit_win) as account_fit_wins
| eval hit_rate_overall = round(wins / total * 100, 2)
| eval hit_rate_account_fit = round(account_fit_wins / account_fit_total * 100, 2)
| table total, wins, hit_rate_overall, account_fit_total, account_fit_wins, hit_rate_account_fit
```

### P&L distribution per signal class (last 30 days)

```spl
index="indexes/IMMUTABLE/ag_picks_closures/*.parquet"
| where alert_group="options_edge_brief"
| eval ago_30d = now() - 2592000
| where _epoch >= ago_30d
| join idea_id [
    index="indexes/IMMUTABLE/ag_picks/*.parquet"
    | where alert_group="options_edge_brief"
    | table idea_id, source_signals, option_structure
  ]
| stats count as picks,
        avg(pnl_per_contract_usd) as avg_pnl,
        sum(pnl_per_contract_usd) as total_pnl
        by source_signals
| eval avg_pnl = round(avg_pnl, 2)
| sort -total_pnl
| head 20
```

### Hit rate by option structure (long_call vs vertical_debit_spread vs iron_condor etc.)

```spl
index="indexes/IMMUTABLE/ag_picks_closures/*.parquet"
| where alert_group="options_edge_brief"
| join idea_id [
    index="indexes/IMMUTABLE/ag_picks/*.parquet"
    | table idea_id, option_structure
  ]
| eval is_win = if_(outcome="won", 1, 0)
| stats count as total, sum(is_win) as wins by option_structure
| eval hit_rate_pct = round(wins / total * 100, 2)
| sort -hit_rate_pct
| table option_structure, total, wins, hit_rate_pct
```

### Currently open positions (no matching closure yet)

```spl
index="indexes/IMMUTABLE/ag_picks/*.parquet"
| where alert_group="options_edge_brief"
| eval ago_60d = now() - 5184000
| where _epoch >= ago_60d
| join type=left idea_id [
    index="indexes/IMMUTABLE/ag_picks_closures/*.parquet"
    | rename outcome as closure_outcome
    | table idea_id, closure_outcome
  ]
| where isnull(closure_outcome)
| table idea_id, instrument_id, direction, entry_price, take_profit_price, stop_loss_price, suggested_sell_epoch, option_structure, account_size_floor_usd
| sort -_epoch
| head 50
```

### Days-held distribution by trigger rule

```spl
index="indexes/IMMUTABLE/ag_picks_closures/*.parquet"
| where alert_group="options_edge_brief"
| stats count as closures,
        avg(days_held) as avg_days,
        min(days_held) as min_days,
        max(days_held) as max_days
        by trigger_rule
| eval avg_days = round(avg_days, 1)
| sort -closures
| table trigger_rule, closures, avg_days, min_days, max_days
```

### Closure quality audit (filter to clean fills only when grading)

```spl
index="indexes/IMMUTABLE/ag_picks_closures/*.parquet"
| where alert_group="options_edge_brief"
| stats count by closure_quality
| sort -count
```

A clean grade rejects illiquid / gap_through_stop fills:

```spl
index="indexes/IMMUTABLE/ag_picks_closures/*.parquet"
| where alert_group="options_edge_brief" AND closure_quality="clean"
| eval is_win = if_(outcome="won", 1, 0)
| stats count as total, sum(is_win) as wins
| eval hit_rate_clean_only_pct = round(wins / total * 100, 2)
| table total, wins, hit_rate_clean_only_pct
```

### Weekly review history - hit-rate trend over time

```spl
index="indexes/IMMUTABLE/ag_picks_review_observations/*.parquet"
| where alert_group="options_performance_review" AND row_kind="summary"
| sort -_epoch
| table review_period_end, hit_rate_overall, hit_rate_account_fit, n_picks_overall, best_signal_class, worst_signal_class
| head 26
```

### Latest rule-tweak recommendations from the weekly review

```spl
index="indexes/IMMUTABLE/ag_picks_review_observations/*.parquet"
| where alert_group="options_performance_review" AND row_kind="summary" AND rule_tweak_recommendation_text != ""
| sort -_epoch
| table review_period_end, rule_tweak_recommendation_text, rule_tweak_rationale, rule_tweak_expected_impact
| head 8
```

### Total dollar P&L assuming 1 contract per pick (paper-trade scoreboard)

```spl
index="indexes/IMMUTABLE/ag_picks_closures/*.parquet"
| where alert_group="options_edge_brief"
| eval ago_90d = now() - 7776000
| where _epoch >= ago_90d
| stats count as closures,
        sum(pnl_per_contract_usd) as total_pnl_per_contract,
        avg(pnl_per_contract_usd) as avg_pnl_per_contract
| eval total_pnl_per_contract = round(total_pnl_per_contract, 2)
| eval avg_pnl_per_contract = round(avg_pnl_per_contract, 2)
| table closures, total_pnl_per_contract, avg_pnl_per_contract
```

> **Reading the metric:** P&L is journaled per 1 contract (price math, not position math), so the unit stays stable as the account scales. Multiply by your actual position size at query time to get dollar P&L.

## Dropping into plain SQL mid-pipeline

Prefer SQL for a step? `| sql` hands the current pipeline to DuckDB as the view `pipeline` and the statement's result becomes the new pipeline. Filesystem access is disabled inside the statement (see [02_commands.md](02_commands.md#sql)) - data enters through `index=` only.

Keep the whole statement on one line (SPQL string literals do not span lines); chain multiple `| sql` pipes for multi-step SQL.

**Window functions (no SPQL equivalent):**

```spl
index="indexes/sample/app_logs/*"
| sql "SELECT service, path, response_ms, rank() OVER (PARTITION BY service ORDER BY response_ms DESC) AS slowest_rank FROM pipeline"
| where slowest_rank <= 3
| sort service, slowest_rank
```

**A whole analysis in one statement, SPQL for the trimmings:**

```spl
index="indexes/sample/app_logs/*"
| sql "SELECT service, count(*) AS requests, round(avg(response_ms), 1) AS avg_ms, round(quantile_cont(response_ms, 0.95), 1) AS p95_ms, sum(CASE WHEN level = 'ERROR' THEN 1 ELSE 0 END) AS errors FROM pipeline GROUP BY service"
| eval error_rate=round(errors / requests * 100, 2)
| sort -p95_ms
```

**Remember:** your indexes are plain gzip Parquet on disk - DuckDB, pandas, or any Parquet reader can also query them entirely outside SpeakesQuery. Your data is never captive to SPQL.

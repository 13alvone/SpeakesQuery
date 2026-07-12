# SpeakesQuery Eval & Functions Reference

This document covers every function available in SpeakesQuery: eval functions (used inside `| eval`), stats aggregation functions (used inside `| stats`, `| eventstats`, `| streamstats`), and multi-value functions.

> **MV-aware**: Many string and numeric functions are "multi-value aware" - when a cell contains a list (e.g., from `values()` or `split()`), the function is automatically applied to each element in the list. This is noted per function.

---

## Using Eval Functions

Eval functions are used inside the `eval` command to create or transform fields:

```spl
| eval <new_field>=<function>(<arguments>)
```

Multiple assignments can be comma-separated:

```spl
| eval clean_name=trim(lower(name)), age_group=if_(age>=18, "adult", "minor")
```

Functions can be nested - inner functions evaluate first:

```spl
| eval result=upper(trim(substr(message, 0, 50)))
```

---

## String Functions

All string functions are **MV-aware**: when applied to a multi-value (list) cell, the function is applied to each element individually, returning a list of results.

### lower(value)

Convert a string to lowercase.

```spl
| eval host_lc=lower(hostname)
```

### upper(value)

Convert a string to uppercase.

```spl
| eval code=upper(status_text)
```

### capitalize(value)

Capitalize the first character of a string.

```spl
| eval name=capitalize(raw_name)
```

### trim(value)

Remove leading and trailing whitespace.

```spl
| eval clean=trim(user_input)
```

### ltrim(value)

Remove leading (left) whitespace only.

```spl
| eval clean=ltrim(padded_text)
```

### rtrim(value)

Remove trailing (right) whitespace only.

```spl
| eval clean=rtrim(padded_text)
```

### len(value)

Return the length of a string (character count).

```spl
| eval msg_length=len(message)
```

### tostring(value)

Convert any value to its string representation.

```spl
| eval status_str=tostring(status_code)
```

### substr(value, start, length)

Extract a substring. `start` is 0-based, `length` is the number of characters.

```spl
| eval prefix=substr(hostname, 0, 3)
| eval middle=substr(message, 5, 10)
```

### replace(value, pattern, replacement)

Replace occurrences of a regex pattern with a replacement string.

```spl
| eval cleaned=replace(message, "\d{4}-\d{4}", "XXXX-XXXX")
| eval no_spaces=replace(name, " ", "_")
```

**Note**: The pattern is a regular expression (Python `re.sub` syntax), not a plain string. To match literal special characters, escape them: `replace(url, "\.", "[.]")`.

### concat(value1, value2, ...)

Concatenate multiple values into a single string. Accepts any mix of field references, string literals, and numbers.

```spl
| eval full_name=concat(first_name, " ", last_name)
| eval log_entry=concat("[", timestamp, "] ", message)
```

**Note**: `concat` is vectorized - it operates across all rows simultaneously and handles Series, scalars, and nested function calls as arguments.

### match(field, pattern)

Test whether a field matches a regular expression. Returns `true` or `false`. Works in both `eval` (write a boolean column) and directly inside a `where` / `search` clause (filter rows).

```spl
| eval is_ip=match(src, "^\d+\.\d+\.\d+\.\d+$")
| eval has_error=match(message, "(?i)error|fail|exception")
```

Inline filtering (no eval column needed):

```spl
| where match(host, "^web-\d+")
| where match(message, "(?i)error|fail") OR severity > 3
| search NOT match(host, "test-.*")
```

The pattern is applied with `re.search` semantics - it matches anywhere in the string, not just from the start. Use `^` / `$` anchors when you need a full-string match. Patterns are Python regex syntax.

### split(value, delimiter)

Split a string into a multi-value (list) field.

```spl
| eval tags=split(tag_string, ",")
| eval parts=split(path, "/")
```

After splitting, use MV functions (`mvcount`, `mvindex`, `mvjoin`, etc.) to work with the resulting list.

### urlencode(value)

URL-encode a string (percent-encoding).

```spl
| eval encoded=urlencode(query_param)
```

`"hello world"` becomes `"hello%20world"`.

### urldecode(value)

Decode a URL-encoded string.

```spl
| eval decoded=urldecode(encoded_param)
```

### defang(value)

Defang indicators (IOCs) by replacing `.` with `[.]` and `:` with `[:]`. Useful for safely sharing URLs and IPs.

```spl
| eval safe_url=defang(url)
```

`"http://evil.com"` becomes `"http[:]//evil[.]com"`.

### fang(value)

Reverse of `defang` - restore defanged indicators to their original form.

```spl
| eval real_url=fang(defanged_url)
```

---

## Numeric Functions

Numeric functions are also **MV-aware** where noted.

### round(value [, precision])

Round a number to the specified number of decimal places. Default precision is 0.

```spl
| eval score=round(raw_score, 2)
| eval whole=round(price)
```

**MV-aware**: Works element-wise on lists.

### abs(value)

Return the absolute value.

```spl
| eval magnitude=abs(delta)
```

**MV-aware**.

### sqrt(value)

Return the square root.

```spl
| eval root=sqrt(variance)
```

**MV-aware**.

### floor(value)

Return the largest integer less than or equal to *value* (i.e. round toward `-∞`). Use this for time-bucketing (`floor(epoch / 86400)` = days-since-Unix-epoch) and any other "truncate down" arithmetic.

```spl
| eval days_since_epoch=floor(_epoch / 86400)
| eval whole_dollars=floor(price)
```

**MV-aware**: works element-wise on lists.

Added 2026-05-16 after a live-API validation pass surfaced that `floor` was a documented expectation but missing from the eval allowlist. The fix landed alongside the [SPQL function drift-guard test](../../tests/test_spql_function_drift_guard.py).

### ceil(value)

Return the smallest integer greater than or equal to *value* (i.e. round toward `+∞`). Companion to `floor`. Operators reach for `ceil` when bucketing things like "days remaining until expiry" or "minimum pages needed to hold N rows."

```spl
| eval pages_needed=ceil(row_count / 100)
| eval whole_dollars_up=ceil(price)
```

**MV-aware**: works element-wise on lists.

### random([min, max])

Generate a random number. Without arguments, returns a random float. With arguments, returns a value in the given range.

```spl
| eval dice=random(1, 6)
| eval noise=random()
```

### tonumber(value)

Convert a string to a numeric value.

```spl
| eval port_num=tonumber(port)
```

**MV-aware**. Returns `NaN` if the conversion fails.

### avg(a, b)

Return the average of two values. (For column-wide averages, use `stats avg(field)`.)

```spl
| eval midpoint=avg(start, end)
```

---

## Conditional Functions

### if_(condition, true_value, false_value)

Ternary conditional - return one value if the condition is true, another if false.

```spl
| eval status_label=if_(status>=400, "error", "ok")
| eval priority=if_(severity>7, "high", if_(severity>3, "medium", "low"))
```

**Note**: The function name is `if_` (with trailing underscore) - not `if`.

Conditions can use comparison operators and other functions:

```spl
| eval has_data=if_(isnotnull(payload), "yes", "no")
| eval tagged=if_(match(message, "CRITICAL"), "alert", "normal")
```

### case(condition1, value1, condition2, value2, ...[, default])

Multi-branch conditional. Evaluates conditions in order and returns the value for the first true condition. An optional final argument serves as the default.

```spl
| eval tier=case(revenue>1000000, "enterprise", revenue>100000, "business", revenue>0, "starter", "unknown")
```

**Note**: If the number of arguments is odd, the last argument is the default value. If even, rows matching no condition get `null`.

### coalesce(value1, value2, ...)

Return the first non-null, non-empty value from the argument list.

```spl
| eval contact=coalesce(email, phone, "no contact info")
```

Works with any mix of field references and literal values.

### isnull(field)

Return `true` if the field value is null/NaN/missing.

```spl
| eval missing=isnull(email)
| search isnull(phone)
```

### isnotnull(field)

Return `true` if the field value is not null.

```spl
| eval has_email=isnotnull(email)
| search isnotnull(response_code)
```

---

## Aggregation Functions (eval-side)

These return per-row values across multiple inputs - distinct from the
`stats` aggregators (which collapse rows).

### min(value1, value2, ...)

Element-wise minimum across two or more inputs. Mixes columns and literals
freely; scalars are broadcast to the column length.

```spl
| eval cheaper=min(price_a, price_b)
| eval capped=min(price, 100)
| eval winner=min(score_a, score_b, score_c)
```

### max(value1, value2, ...)

Element-wise maximum across two or more inputs. Same broadcast rules as
`min()`.

```spl
| eval winner=max(bid, offer)
| eval floored=max(value, 0)
```

---

## Time / Datetime Functions

All time helpers operate in **UTC**. Epochs are floats (seconds since the
Unix epoch); microsecond-precision input formats keep the fractional part.

### now()

Return the current UTC epoch as a float. Broadcasts naturally in
arithmetic with column values.

```spl
| eval current=now()
| eval age_seconds=now() - created_at_epoch
```

### relative_time(spec)

Resolve a Splunk-style relative-time string to a UTC epoch integer. Accepts
the same syntax as the `earliest=` / `latest=` time-range tokens:

| Spec | Meaning |
|---|---|
| `now` | Current epoch |
| `-30m` | 30 minutes ago |
| `+1h` | 1 hour from now |
| `-1d@d` | 1 day ago, snapped to start of day |
| `-7d@w` | 7 days ago, snapped to start of week (Monday 00:00 UTC) |
| `-1h@h` | 1 hour ago, snapped to start of hour |

Snap units: `s`, `m`, `h`, `d`, `w`, `M` (month), `y` (year).

```spl
| eval one_hour_ago=relative_time("-1h")
| eval start_of_yesterday=relative_time("-1d@d")
```

### strptime(date_str [, format])

Parse a date string into a UTC epoch float.

* **Auto-detect mode** (no `format` argument): tries each entry in the
  28-format whitelist defined in `functionality/datetime_parser.py`,
  returning the first match. Numeric strings (e.g. `"1705329000"`) pass
  through as-is on the assumption they're already epochs. When given a
  Series, takes a column-homogeneous fast path: detects the format from
  the first non-null value, then bulk-applies `pd.to_datetime` for
  performance, falling back per-row only for stragglers.
* **Explicit-format mode**: forces a single format using Python's standard
  `strftime` directives. Faster than auto-detect for known data.

Auto-detected formats include ISO 8601 (`%Y-%m-%dT%H:%M:%S`), space-separated
(`%Y-%m-%d %H:%M:%S` and microsecond variants), US slash (`%m/%d/%Y`),
European dash (`%d-%m-%Y`), month-name (`%B %d, %Y`), 12-hour (`%I:%M:%S %p`),
compact (`%Y%m%d%H%M%S`), and ISO week (`%Y-W%W-%w`). See `DATE_FORMATS` in
`functionality/datetime_parser.py` for the full ordered list.

```spl
| eval epoch=strptime(timestamp_str)
| eval epoch=strptime(date_field, "%m/%d/%Y %H:%M:%S")
```

### strftime(epoch, format)

Format a UTC epoch as a string using Python's standard `strftime` directives.

```spl
| eval iso_date=strftime(_epoch, "%Y-%m-%d")
| eval pretty=strftime(created_at, "%B %d, %Y at %I:%M %p")
| eval round_trip=strftime(strptime(d, "%m/%d/%Y"), "%Y-%m-%d")
```

---

## Encoding Functions

### base64_encode(value)

Encode a string to Base64.

```spl
| eval encoded=base64_encode(payload)
```

**MV-aware**.

### base64_decode(value)

Decode a Base64 string.

```spl
| eval decoded=base64_decode(encoded_data)
```

**MV-aware**.

---

## Multi-Value Eval Functions

These functions operate on multi-value (list) cells within an `eval` expression. Scalar values are automatically promoted to single-element lists before processing.

### mvdedup(field)

Remove duplicate values from a multi-value field, preserving order.

```spl
| eval unique_tags=mvdedup(tags)
```

### mvsort(field)

Sort the elements of a multi-value field alphabetically.

```spl
| eval sorted_hosts=mvsort(hosts)
```

### mvcount(field)

Return the number of elements in a multi-value field.

```spl
| eval tag_count=mvcount(tags)
```

### mvreverse(field)

Reverse the order of elements in a multi-value field.

```spl
| eval reversed=mvreverse(path_segments)
```

### mvjoin(field, delimiter)

Join multi-value elements into a single string using a delimiter.

```spl
| eval host_list=mvjoin(hosts, ", ")
| eval csv_line=mvjoin(values, ",")
```

### mvfind(field, pattern)

Return the index (0-based) of the first element matching a regex pattern. Returns `-1` if no match.

```spl
| eval pos=mvfind(tags, "critical")
```

### mvdc(field)

Multi-value distinct count - return the number of unique elements in a multi-value field. Like `mvcount` but de-duplicates before counting.

```spl
| eval unique_tag_count=mvdc(tags)
| eval distinct_users=mvdc(viewers)
```

### mvindex(field, index)

Return the element at the specified index (0-based). Negative indices count from the end.

```spl
| eval first_tag=mvindex(tags, 0)
| eval last_tag=mvindex(tags, -1)
```

### mvappend(value1, value2, ...)

Concatenate values and/or lists into a single multi-value field.

```spl
| eval all_ips=mvappend(src_ip, dst_ip)
| eval combined=mvappend(field_a, field_b, "literal_value")
```

### mvzip(list1, list2, delimiter)

Zip two multi-value fields element-wise, joining corresponding elements with a delimiter.

```spl
| eval pairs=mvzip(keys, values, "=")
```

If `keys=["a","b"]` and `values=["1","2"]`, the result is `["a=1", "b=2"]`.

### mvfilter(expression)

Filter elements within a multi-value field per-row, keeping only those for which the boolean expression is true.

```spl
| eval critical_events=mvfilter(match(events, "CRITICAL"))
| eval high_scores=mvfilter(scores > 90)
```

**How it works**: For each row, `mvfilter` iterates over every element in the multi-value field referenced by the expression. The expression is evaluated with the field bound to each individual element. Only elements where the expression evaluates to true are kept.

**Supported inside mvfilter expressions**: comparison operators (`=`, `!=`, `>`, `<`, `>=`, `<=`), `match()`, `isnull()`, `isnotnull()`, `lower()`, `upper()`, `len()`, `like()`, and boolean operators (`and`, `or`, `not`).

```spl
# Keep only error-level log entries from a multi-value field
| eval errors=mvfilter(match(log_entries, "(?i)error|critical"))

# Keep only values greater than 100
| eval big_values=mvfilter(metrics > 100)
```

---

## Stats Aggregation Functions

These functions are used inside `stats`, `eventstats`, `streamstats`, and `timechart`.

| Function | Description | Example |
|----------|-------------|---------|
| `count` | Count all rows | `stats count` |
| `count(field)` | Count non-null values | `stats count(email) as has_email` |
| `sum(field)` | Sum of values | `stats sum(bytes) as total_bytes` |
| `avg(field)` | Arithmetic mean | `stats avg(duration) as avg_dur` |
| `min(field)` | Minimum value | `stats min(latency)` |
| `max(field)` | Maximum value | `stats max(latency)` |
| `median(field)` | Median (50th percentile) | `stats median(response_time)` |
| `mode(field)` | Most frequent value | `stats mode(status)` |
| `dc(field)` | Distinct count | `stats dc(user) as unique_users` |
| `range(field)` | Max minus min | `stats range(temperature)` |
| `values(field)` | Collect distinct values into a list | `stats values(host) as hosts` |
| `first(field)` | First value (by row order) | `stats first(timestamp)` |
| `last(field)` | Last value (by row order) | `stats last(timestamp)` |
| `earliest(field)` | Alias for `first` | `stats earliest(_time)` |
| `latest(field)` | Alias for `last` | `stats latest(_time)` |

### Using aliases

Use `as` to name the result column:

```spl
| stats count as total_events, dc(host) as unique_hosts by region
```

Without `as`, the default column name is the function expression itself (e.g., `count(host)`).

### Wildcard fields

Use `*` to apply an aggregation across all non-group columns:

```spl
| stats sum(*) by category
```

### Grouping with BY

The `by` clause splits aggregations by one or more fields:

```spl
| stats avg(cpu) as avg_cpu, max(cpu) as peak_cpu by host, datacenter
```

---

## Streamstats-Specific Behavior

When used in `streamstats`, aggregation functions compute **cumulatively** up to and including the current row:

| Function | Streamstats Behavior |
|----------|---------------------|
| `count` | Running row counter (1, 2, 3, ...) |
| `sum` | Cumulative sum |
| `avg` | Expanding mean |
| `min` | Running minimum (never increases) |
| `max` | Running maximum (never decreases) |
| `median` | Expanding median |
| `mode` | Expanding mode |
| `dc` | Running distinct count |
| `values` | Accumulating list of unique values |
| `first`/`earliest` | Always the first row's value |
| `last`/`latest` | Always the current row's value |

```spl
| streamstats sum(bytes) as running_total, count as row_num by host
```

---

## Special / Utility Functions

### type(value)

Return the Python type-name of a value as a string. MV-aware - on a list cell, returns a list of type-names per element.

```spl
| eval kind=type(amount)
```

### randomize(value)

Replace a numeric or list value with a randomised variant (useful for redaction / jitter scenarios). Returns the same shape as the input.

```spl
| eval jittered=randomize(response_time)
```

---

## Function Quick Reference

### By Category

**String**: `lower`, `upper`, `capitalize`, `trim`, `ltrim`, `rtrim`, `len`, `tostring`, `substr`, `replace`, `concat`, `match`, `split`, `urlencode`, `urldecode`, `defang`, `fang`

**Numeric**: `round`, `floor`, `ceil`, `abs`, `sqrt`, `random`, `randomize`, `tonumber`, `avg`, `min`, `max`, `sum`, `median`, `mode`, `range`

**Conditional**: `if_`, `case`, `coalesce`, `isnull`, `isnotnull`

**Time / Datetime**: `now`, `relative_time`, `strftime`, `strptime`

**Encoding**: `base64_encode`, `base64_decode`

**Type Introspection**: `type`

**Multi-Value**: `mvdedup`, `mvsort`, `mvcount`, `mvreverse`, `mvjoin`, `mvfind`, `mvindex`, `mvappend`, `mvzip`, `mvfilter`, `mvcombine`, `mvdc`

**Stats Aggregation**: `count`, `sum`, `avg`, `min`, `max`, `median`, `mode`, `dc`, `range`, `values`, `first`, `last`, `earliest`, `latest`

### MV-Awareness Summary

Functions that automatically apply **per-element** when the input is a list:

`lower`, `upper`, `capitalize`, `trim`, `ltrim`, `rtrim`, `len`, `tostring`, `tonumber`, `urlencode`, `urldecode`, `base64_encode`, `base64_decode`, `abs`, `sqrt`, `round`, `substr`, `replace`, `match`, `split`

Functions that operate on the **list itself**:

`mvdedup`, `mvsort`, `mvcount`, `mvreverse`, `mvjoin`, `mvfind`, `mvindex`, `mvappend`, `mvzip`, `mvfilter`

---

## What's Next

- **[Advanced Features](04_advanced.md)** - Subsearches, macros, joins, scheduled searches
- **[Cookbook: "I want to..."](05_cookbook.md)** - Task-oriented recipes for common queries

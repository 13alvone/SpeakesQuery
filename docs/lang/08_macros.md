# Macros - Practical Guide

Macros are reusable, parameterised query fragments that you define once and invoke anywhere with backtick syntax. They are one of the most powerful tools SpeakesQuery offers for building maintainable, consistent, and auditable queries - and you should use them liberally.

---

## Why Use Macros?

### Eliminate repetition

If you find yourself typing the same filter, eval chain, or regex in multiple queries, that's a macro waiting to happen. Define it once; invoke it everywhere.

```spl
# Before: same filter copy-pasted across dozens of queries
index="web/access.parquet" | search status!=200 AND method!="OPTIONS" AND NOT src_ip IN ("10.0.0.1", "10.0.0.2")

# After: one macro, reused everywhere
index="web/access.parquet" | search `exclude_noise`
```

### Enforce standardisation

Macros let your team agree on a single, canonical way to perform common operations. When the definition changes - say a new internal IP is added to the exclusion list - every query that uses the macro picks up the change automatically. No find-and-replace across dozens of saved searches.

### Simplify complex queries

Long queries become readable when you replace dense sub-expressions with named macros:

```spl
index="orders/transactions.parquet"
| `enrich_customer_data`
| `flag_high_value(threshold=10000)`
| `exclude_test_accounts`
| stats sum(amount) as revenue by region
```

Anyone reading this query can understand the intent without parsing three pages of inline logic.

### Build a shared vocabulary

Over time, your macro library becomes a shared vocabulary for your organisation's data operations. New team members can browse the Macros tab, read descriptions, and immediately understand what primitives are available - instead of reverse-engineering existing queries.

---

## How Macros Work

Macros are **pure text substitution**. Before the SpeakesQuery parser ever sees your query, every backtick-delimited macro call is replaced with its stored definition text. This happens pre-parse, so macros can contain any valid SpeakesQuery syntax - pipeline segments, field references, eval expressions, even other macro calls.

### The expansion lifecycle

1. You write a query containing `` `my_macro` `` or `` `my_macro(arg1, arg2)` ``
2. The expansion engine finds all backtick-delimited calls
3. Each call is looked up in the macro store
4. Parameter placeholders (`$param$`) in the definition are replaced with the supplied arguments
5. The expanded text replaces the backtick call in the query string
6. Steps 2–5 repeat for any nested macro calls introduced by the expansion
7. The fully expanded query is passed to the ANTLR4 parser for execution

Because expansion is pure text substitution, macros are transparent - you can always see exactly what a macro produces by using the **Expand Macros** button on the Query page.

---

## Macro Syntax

### Invoking a parameterless macro

```spl
index="logs/app.parquet" | `exclude_noise`
```

### Invoking a parameterised macro

```spl
index="logs/app.parquet" | `threshold_filter(field_name, 100)`
```

Arguments are positional and comma-separated. String arguments containing commas must be quoted:

```spl
| `tag_regions("us-east-1, us-west-2", "production")`
```

### Parameter placeholders in definitions

When you define a macro, you declare parameter names. In the definition body, each parameter is referenced with `$param_name$` (dollar signs on both sides):

**Macro name:** `threshold_filter`
**Parameters:** `field`, `limit`
**Definition:**
```
search $field$ > $limit$
```

When called as `` `threshold_filter(response_time, 500)` ``, the expansion produces:
```
search response_time > 500
```

---

## Creating Macros

Macros are created and managed through the **Macros** tab in the application. See the [Application Guide](06_application_guide.md) for step-by-step instructions on using the UI.

### Naming conventions

- Macro names must be alphanumeric with underscores only (no spaces, no hyphens)
- Use `snake_case` for consistency
- Choose descriptive names that communicate intent: `exclude_internal_traffic` is better than `filter1`

### Writing good definitions

A macro definition is any valid SpeakesQuery fragment. It can be:

- A **filter expression**: `search status >= 400 AND status < 500`
- A **pipeline segment**: `| eval duration_ms = duration * 1000 | search duration_ms > 100`
- A **multi-command pipeline**: `| stats count by host | sort -count | head 10`
- A **partial expression** used inline: `status IN (200, 201, 204)`

**Important:** Definitions are inserted exactly where the backtick call appears. Make sure your definition makes syntactic sense in context:

```spl
# If your macro definition is: search status >= 400
# Good - macro produces a valid pipeline segment:
index="logs/app.parquet" | `errors_only`

# If your macro definition is: status >= 400
# Good - macro produces a valid inline filter:
index="logs/app.parquet" `errors_only`
```

### Parameter design tips

- Keep parameter counts low (1–3). If you need more, consider breaking the macro into smaller pieces.
- Use descriptive parameter names: `$field_name$` is clearer than `$f$`.
- Document parameters in the macro's description field so other users know what to pass.

---

## Nested Macros

Macro definitions can contain calls to other macros. The expansion engine handles this automatically - it expands level by level until no more macro calls remain.

### Example: composing macros

**Macro `valid_country_regex`:**
```
^(US|UK|CA|AU|DE|FR|JP)$
```

**Macro `exclude_invalid_countries`:**
```
| regex country="`valid_country_regex`"
```

**Macro `standard_web_pipeline`:**
```
| `exclude_invalid_countries` | `exclude_noise` | eval request_time_ms=request_time*1000
```

When you write:
```spl
index="web/access.parquet" | `standard_web_pipeline` | stats avg(request_time_ms) by endpoint
```

The engine expands `standard_web_pipeline` → which contains `exclude_invalid_countries` → which contains `valid_country_regex`. All three levels are resolved automatically.

### Cycle detection

The engine detects circular references. If macro `A` calls `B` and macro `B` calls `A`, expansion fails immediately with a clear error message showing the full chain: `A -> B -> A`.

### Depth limits

Expansion is capped at a configurable maximum depth (default 100 levels) to prevent runaway recursion. This limit is far higher than any practical use case - if you're nesting more than 3–4 levels deep, consider simplifying.

---

## Expanding Macros in the Query Page

The **Expand Macros** button on the Query page lets you preview exactly what a macro produces before running the query. This is invaluable for debugging, learning, and auditing.

### How it works

1. Write a query containing macro calls in the query box
2. Set the **Depth** control next to the button:
   - **0** - expand all levels (every nested macro is resolved)
   - **1** - expand only top-level macros (nested calls remain as backticks)
   - **2** - expand two levels deep
   - **N** - expand N levels deep
3. Click **Expand Macros**
4. The query box is updated with the expanded text, wrapped in annotation comments

### Annotation comments

Each expanded macro is wrapped in triple-backtick annotation lines that mark where the expansion starts and ends:

```
```[+] Expanded: exclude_noise```
search status!=200 AND method!="OPTIONS" AND NOT src_ip IN ("10.0.0.1", "10.0.0.2")
```exclude_noise END```
```

These annotations are **purely visual** - they help you see which parts of the expanded query came from which macro. When you click **Run Query**, annotation lines are automatically stripped before execution. You never need to remove them manually.

### Depth control in practice

Suppose you have a query with a top-level macro that itself contains a nested macro:

```spl
index="web/access.parquet" | `standard_web_pipeline` | stats count by endpoint
```

- **Depth 1** expands `standard_web_pipeline` but leaves `exclude_invalid_countries` and `exclude_noise` as backtick calls - so you can see the first layer
- **Depth 2** also expands `exclude_invalid_countries` and `exclude_noise`, but leaves `valid_country_regex` as a backtick call
- **Depth 0** expands everything - no backtick calls remain

This granular control lets you inspect one layer at a time when debugging complex macro chains.

---

## Standardisation Recipes

The following patterns show how to use macros to standardise common operations across your organisation.

### Standard exclusion filters

Define exclusion lists in one place so every query applies the same rules:

**`exclude_internal_ips`:**
```
NOT src_ip IN ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
```

**`exclude_health_checks`:**
```
NOT (endpoint="/health" OR endpoint="/ping" OR endpoint="/ready")
```

**`production_only`:**
```
environment="production"
```

### Standard enrichment chains

Wrap common eval + lookup sequences that every query should apply:

**`enrich_geo(ip_field)`:**
```
| lookup geoip.csv $ip_field$ OUTPUT country, city, latitude, longitude
```

**`classify_status`:**
```
| eval status_class=case(status<200, "informational", status<300, "success", status<400, "redirect", status<500, "client_error", true(), "server_error")
```

### Standard aggregation patterns

Create named macros for frequently-used aggregation logic:

**`error_rate`:**
```
| eval is_error=if_(status>=400, 1, 0) | stats sum(is_error) as errors, count as total | eval error_rate=round(errors/total*100, 2)
```

**`top_n(field, n)`:**
```
| stats count by $field$ | sort -count | head $n$
```

### Regex libraries

Store validated regex patterns as macros so they're tested once and reused everywhere:

**`ip_v4_pattern`:**
```
\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}
```

**`email_pattern`:**
```
[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}
```

Use them in rex or regex commands:

```spl
index="logs/app.parquet" | rex field=message "from=(?<sender>`email_pattern`)"
```

---

## Best Practices

### Name descriptively

A macro name should communicate what it does without needing to read its definition. Prefer `exclude_internal_traffic` over `filter1` or `my_macro`.

### Keep macros focused

Each macro should do one thing well. If a macro definition spans more than 3–4 pipeline segments, consider breaking it into smaller composable pieces.

### Document everything

Always fill in the description field when creating a macro. Include:
- What the macro does
- What each parameter represents
- Example usage

### Version through naming

If you need to introduce a breaking change to a widely-used macro, create a new version (`error_rate_v2`) rather than modifying the original. Retire the old one once all queries have migrated.

### Test before deploying

Use the **Test** panel on the Macros tab to run your macro against real data before using it in production queries or saved searches.

### Use the Expand button for debugging

When a query produces unexpected results, expand its macros to see the raw query. The annotation comments pinpoint exactly which macro contributed each part of the pipeline.

---

## What's Next

- **[Application Guide](06_application_guide.md)** - Step-by-step UI instructions for creating, editing, testing, and expanding macros
- **[Advanced Features](04_advanced.md)** - Subsearches, joins, timechart, rex, and other advanced capabilities
- **[Cookbook: "I want to..."](05_cookbook.md)** - Task-oriented recipes for common queries

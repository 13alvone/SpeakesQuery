# API Reference

SpeakesQuery exposes a REST API on `http://<host>:<port>/api/` for headless, programmatic interaction. Every endpoint accepts and returns JSON. The default bind address is `127.0.0.1:5111` (bare metal) - configurable via `HOST` and `PORT` environment variables. Docker binds `0.0.0.0` inside the container while the host-side port mapping stays on `127.0.0.1` unless `BIND_ADDR` is set.

> **Authentication (access-token gate, 2026-07-12):** whenever the server binds beyond loopback (every Docker install; any `HOST` override) or `SPEAKESQUERY_AUTH=on`, all endpoints except `GET /healthz` require the access token generated at install (`~/.speakes-query/access_token`). Present it one of three ways:
>
> ```bash
> curl -H "X-SpeakesQuery-Token: <token>" http://localhost:5111/api/tree
> curl -H "Authorization: Bearer <token>"  http://localhost:5111/api/tree
> curl "http://localhost:5111/api/tree?token=<token>"
> ```
>
> Browsers authenticate once via the `?token=` URL install.sh prints; the server promotes it to an HttpOnly session cookie. Requests without a valid token get `401 {"status": "error", "message": "access token required"}`. `SPEAKESQUERY_AUTH=off` disables the gate for deployments behind a reverse proxy that enforces its own auth. This is a single-operator gate, not a multi-user permission model - see the security discussion in [06_application_guide.md](06_application_guide.md#network-exposure-and-lan-access).

---

## Conventions

### Content type

All request bodies must be `application/json`. All responses are `application/json` unless the endpoint returns a file download. File upload endpoints (`/api/lookups/upload`, `/api/indexes/import`, `/api/indexes/import/sqlite-tables`) use `multipart/form-data` instead.

### Response envelope

Every response includes a `status` field:

```json
{"status": "success", ...}
{"status": "error", "message": "Human-readable error description."}
{"status": "partial", "errors": [...], ...}
```

- `"success"` - operation completed normally.
- `"error"` - operation failed; check `message` for details. HTTP status codes are used where appropriate (400, 404, 500).
- `"partial"` - some operations succeeded, some failed (used by bulk settings updates).

### Defaults and limits

| Parameter | Default | Range / Notes |
|-----------|---------|---------------|
| Server port | `5111` | Set via `PORT` env var |
| Server host | `127.0.0.1` | Set via `HOST` env var; non-loopback binds activate the access-token gate |
| Lookup upload limit | 200 MB | Hard ceiling per file |
| Index import limit | 200 MB | Hard ceiling per file (CSV, Parquet, SQLite) |
| Lookup preview rows | 200 | Override with `?limit=N`, max 5000 |
| Job history | 10 | Ring buffer; oldest evicted automatically |
| Execution history | 50 | Override with `?limit=N` |
| Script timeout | Configured in Settings | `default_script_timeout_seconds` |

---

## Core Endpoints

### Liveness

#### `GET /healthz`

Liveness probe. Always exempt from the access-token gate (it leaks nothing but liveness). Used by the Docker `HEALTHCHECK` and the install.sh readiness loop.

```json
{"status": "ok"}
```

### Query Execution

#### `POST /api/query`

Execute an SPQL query and return results as JSON.

**Request:**

```json
{
  "query": "index=\"indexes/github/public_events/*\" | stats count by type"
}
```

**Response (success):**

```json
{
  "status": "success",
  "results": [
    {"type": "PushEvent", "count": 42},
    {"type": "WatchEvent", "count": 17}
  ],
  "column_names": ["type", "count"],
  "job_id": "auto_1711234567_abc123"
}
```

**Response (no data):**

```json
{"status": "error", "message": "No data returned from query."}
```

**curl example:**

```bash
curl -X POST http://localhost:5111/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "index=\"indexes/github/public_events/*\" | head 5"}'
```

**Notes:**
- The `job_id` can be used with `/api/jobs/<job_id>` to retrieve cached results.
- Multi-value fields (lists) are preserved in the JSON output with `null` values stripped.
- Queries follow SPQL pipe syntax - see the language reference docs for the full command set.

---

### Ingestion Scripts (`/api/si/*`)

Manage scheduled data ingestion tasks that run Python scripts on cron schedules.

#### `GET /api/si/list`

List all ingestion scripts.

**Response:**

```json
{
  "status": "success",
  "tasks": [
    {
      "id": 1,
      "title": "GitHub Public Events",
      "description": "Fetches latest public events from GitHub Events API.",
      "cron_schedule": "*/30 * * * *",
      "subdirectory": "github/public_events",
      "overwrite": "false",
      "disabled": false,
      "api_url": "https://api.github.com/events",
      "code": "import pandas as pd\n..."
    }
  ]
}
```

#### `POST /api/si/add`

Create a new ingestion script.

**Request:**

```json
{
  "title": "My Ingestion Script",
  "code": "import pandas as pd\nimport requests\n...",
  "cron_schedule": "0 */6 * * *",
  "description": "Optional description",
  "overwrite": "false",
  "subdirectory": "my_data/source",
  "api_url": "https://api.example.com/data"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `title` | Yes | Unique name. Letters, digits, spaces, hyphens, underscores. |
| `code` | Yes | Python 3 script. Must call `GENERATE_RESULTS(df)` with a pandas DataFrame. |
| `cron_schedule` | Yes | 5-field cron expression (minute hour day month weekday). |
| `description` | No | Human-readable description. |
| `overwrite` | No | `"true"` replaces data each run; `"false"` (default) appends new Parquet files. |
| `subdirectory` | No | Output path under `indexes/`. Supports nesting with `/`. Defaults to title if blank. |
| `api_url` | No | Reference URL for documentation purposes. |

**Response:**

```json
{
  "status": "success",
  "task": { "id": 5, "title": "My Ingestion Script", ... }
}
```

**curl example:**

```bash
curl -X POST http://localhost:5111/api/si/add \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Weather Data",
    "code": "import pandas as pd\nimport requests\nresp = requests.get(\"https://api.example.com/weather\")\ndf = pd.DataFrame(resp.json())\nGENERATE_RESULTS(df)",
    "cron_schedule": "0 */3 * * *",
    "subdirectory": "weather/current"
  }'
```

#### `POST /api/si/<id>/run`

Trigger an immediate, synchronous ingestion run for a task, bypassing its cron schedule. Same code path as the scheduled trigger. Blocks until the run completes (subject to the task's execution timeout) and returns the resulting execution-history row.

**Response:**

```json
{
  "status": "success",
  "run": {"task_id": 3, "status": "success", "runtime": 4.2, "error_message": null}
}
```

#### `GET /api/si/<id>`

Retrieve a single ingestion script by numeric ID.

**Response:**

```json
{
  "status": "success",
  "task": { "id": 1, "title": "...", "code": "...", ... }
}
```

#### `PUT /api/si/<id>`

Update an existing ingestion script. Send only the fields you want to change.

**Request:**

```json
{
  "cron_schedule": "0 */12 * * *",
  "description": "Updated description"
}
```

#### `DELETE /api/si/<id>`

Delete an ingestion script.

**Response:**

```json
{"status": "success"}
```

#### `POST /api/si/<id>/test`

Test-run a saved ingestion script in the sandbox. Returns structured pass/fail results including row count, columns, data types, and any errors.

**Response:**

```json
{
  "status": "success",
  "summary": {
    "passed": true,
    "rows": 30,
    "columns": ["event_id", "type", "actor", "_epoch"],
    "dtypes": {"event_id": "object", "type": "object", "actor": "object", "_epoch": "float64"},
    "preview": [{"event_id": "123", "type": "PushEvent", ...}]
  }
}
```

#### `POST /api/si/test-code`

Test arbitrary code before saving. Uses staging credentials (`script_id=0`).

**Request:**

```json
{
  "code": "import pandas as pd\ndf = pd.DataFrame({'x': [1,2,3]})\nGENERATE_RESULTS(df)",
  "task_id": null
}
```

#### `POST /api/si/<id>/toggle`

Enable or disable a scheduled script.

**Request:**

```json
{"enabled": false}
```

#### `POST /api/si/lint`

Syntax-check Python code without executing it.

**Request:**

```json
{"code": "import pandas as pd\ndf = pd.DataFrame()"}
```

**Response:**

```json
{"status": "ok", "errors": []}
```

**Response (syntax error):**

```json
{
  "status": "ok",
  "errors": [
    {"line": 3, "col": 12, "message": "unexpected EOF while parsing", "text": "df = pd.DataFrame("}
  ]
}
```

#### `GET /api/si/status`

Return scheduler status with next run times for all active jobs.

#### `GET /api/si/history`

Return execution history. Optional query params: `?task_id=1&limit=20`.

**Response:**

```json
{
  "status": "success",
  "history": [
    {
      "task_id": 1,
      "started_at": "2026-03-22T12:00:00",
      "duration_seconds": 4.2,
      "rows_written": 30,
      "status": "success"
    }
  ]
}
```

#### `GET /api/si/check-subdirectory`

Validate a subdirectory path before saving.

**Query params:** `?path=github/public_events`

**Response:**

```json
{
  "status": "success",
  "exists": true,
  "has_files": true,
  "valid": true
}
```

---

### Saved Searches (`/api/ss/*`)

Scheduled queries that run on cron and send email alerts when results are found.

#### `GET /api/ss/list`

List all saved searches with their next scheduled run time.

**Response:**

```json
{
  "status": "success",
  "searches": [
    {
      "name": "github_force_push_alert",
      "query": "index=\"indexes/github/public_events/*\" | search type=\"PushEvent\"",
      "cron_schedule": "*/15 * * * *",
      "lookback": "24h",
      "email_address": "alerts@example.com",
      "email_body": "Force push detected by $actor$ on $repo$",
      "trigger": "per result",
      "disabled": false
    }
  ]
}
```

#### `POST /api/ss/create`

Create a new saved search.

**Request:**

```json
{
  "name": "high_error_rate",
  "query": "index=\"indexes/app_logs/*\" | stats count by level | search level=\"ERROR\"",
  "cron_schedule": "*/5 * * * *",
  "lookback": "1h",
  "email_address": "oncall@example.com",
  "email_body": "Error count: $count$",
  "trigger": "per result"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier (alphanumeric, hyphens, underscores). |
| `query` | Yes | SPQL query to execute on schedule. |
| `cron_schedule` | Yes | 5-field cron expression. |
| `lookback` | Yes | Time window for data (e.g. `1h`, `24h`, `7d`). |
| `email_address` | Yes | Recipient for alert emails. |
| `email_body` | No | Email body template. Use `$field_name$` tokens for value substitution. |
| `trigger` | No | `"per result"` or `"once"`. |
| `overwrite` | No | Set `true` to overwrite an existing search with the same name. |

#### `GET /api/ss/<name>`

Retrieve a single saved search by name.

#### `PUT /api/ss/<name>`

Update an existing saved search. Send only the fields you want to change.

#### `DELETE /api/ss/<name>`

Soft-delete a saved search (archived for 30 days, recoverable).

#### `GET /api/ss/<name>/yaml`

Return the raw YAML configuration for a saved search.

**Response:**

```json
{
  "status": "success",
  "yaml": "name: high_error_rate\nquery: ..."
}
```

#### `POST /api/ss/validate-tokens`

Validate that `$token$` variables in the email body are populated in query results.

**Request:**

```json
{
  "query": "index=\"indexes/app_logs/*\" | head 100",
  "tokens": ["actor", "repo", "type"],
  "validation_days": 30
}
```

**Response (all valid):**

```json
{"status": "success", "message": "All tokens validated.", "days_checked": 30}
```

**Response (warnings):**

```json
{
  "status": "warning",
  "null_tokens": [
    {"token": "actor", "null_count": 5, "total_rows": 100, "reason": "null_values"}
  ],
  "days_checked": 30
}
```

---

### Lookups (`/api/lookups/*`)

Manage reference data files (CSV, JSON, TSV, Parquet) used with the `| lookup` and `| inputlookup` commands.

#### `GET /api/lookups`

List all lookup files with metadata.

**Response:**

```json
{
  "status": "success",
  "files": [
    {
      "name": "geo_ips.csv",
      "type": "csv",
      "size_bytes": 524288,
      "created": 1711234567.0,
      "modified": 1711234567.0,
      "accessed": 1711234567.0
    }
  ]
}
```

#### `GET /api/lookups/preview`

Preview the first N rows of a lookup file.

**Query params:** `?file=geo_ips.csv&limit=50`

**Response:**

```json
{
  "status": "success",
  "file": "geo_ips.csv",
  "total_rows": 10000,
  "preview_rows": 50,
  "columns": ["ip", "country", "city"],
  "rows": [{"ip": "1.2.3.4", "country": "US", "city": "New York"}, ...]
}
```

#### `POST /api/lookups/upload`

Upload a lookup file. Send as `multipart/form-data` with a `file` field.

**Constraints:**
- Allowed extensions: `.csv`, `.json`, `.tsv`, `.parquet`
- Maximum size: 200 MB
- Content is validated (must parse as the declared format)
- Filenames must match `[a-zA-Z0-9_\-. ]+`

**curl example:**

```bash
curl -X POST http://localhost:5111/api/lookups/upload \
  -F "file=@/path/to/geo_ips.csv"
```

#### `POST /api/lookups/delete`

Delete a lookup file.

**Request:**

```json
{"file": "old_data.csv"}
```

#### `GET /api/lookups/download`

Download a lookup file.

**Query params:** `?file=geo_ips.csv`

Returns the raw file with `Content-Disposition: attachment`.

---

### Index Import (`/api/indexes/import`)

Import CSV, Parquet, or SQLite files directly as queryable indexes. Unlike lookups (reference tables), imported files become full indexes that you query with `index="name"`.

#### `POST /api/indexes/import`

Import a file into a target index subdirectory. Uses `multipart/form-data`.

**Form fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `file` | Yes | The file to import (CSV, Parquet, or SQLite). |
| `index_name` | Yes | Target subdirectory under `indexes/`. Supports nesting with `/` (e.g. `firewall/2024`). |
| `date_field` | No | Column name to convert to `_epoch`. If omitted and no `_epoch` column exists, import time is used. |
| `table` | No | SQLite only - import a single table. If omitted, all tables are imported (each as a separate Parquet file). |

**Constraints:**
- Allowed extensions: `.csv`, `.parquet`, `.sqlite`, `.sqlite3`, `.db`
- Maximum size: 200 MB
- Content is validated (must parse as the declared format)
- Index name is validated against path traversal, invalid characters, and depth limits

**Response (success):**

```json
{
  "status": "success",
  "message": "Imported 1,500 rows into index=firewall/2024 (1 file(s)).",
  "files_written": 1,
  "total_rows": 1500
}
```

**Response (SQLite, multiple tables):**

```json
{
  "status": "success",
  "message": "Imported 3,200 rows into index=app_db (2 file(s)).",
  "files_written": 2,
  "total_rows": 3200,
  "tables": [
    {"name": "users", "rows": 1200},
    {"name": "events", "rows": 2000}
  ]
}
```

**curl examples:**

```bash
# Import a CSV with a date column
curl -X POST http://localhost:5111/api/indexes/import \
  -F "file=@/path/to/firewall_logs.csv" \
  -F "index_name=firewall/2024" \
  -F "date_field=timestamp"

# Import a Parquet file (already has _epoch)
curl -X POST http://localhost:5111/api/indexes/import \
  -F "file=@/path/to/metrics.parquet" \
  -F "index_name=metrics/prod"

# Import a specific table from a SQLite database
curl -X POST http://localhost:5111/api/indexes/import \
  -F "file=@/path/to/app.sqlite" \
  -F "index_name=app_data" \
  -F "table=events"
```

**Notes:**
- Every imported file gets an `_epoch` column (required for time-based queries like `earliest=` / `latest=`).
- If your file already has `_epoch`, it is used as-is.
- If you specify `date_field`, that column is converted to Unix epoch seconds via `pd.to_datetime()`.
- If neither exists, all rows are stamped with the current time.
- After import, query your data with `index="index_name" | head 10`.

#### `POST /api/indexes/import/sqlite-tables`

List the table names in an uploaded SQLite file. Use this to discover available tables before importing.

**Request:** `multipart/form-data` with a `file` field (SQLite file only).

**Response:**

```json
{
  "status": "success",
  "tables": ["users", "events", "sessions"]
}
```

---

### Macros (`/api/macros/*`)

Reusable query fragments invoked with backtick syntax in SPQL (e.g. `` `my_macro` `` or `` `my_macro(arg1, arg2)` ``).

#### `GET /api/macros/list`

List all macros.

**Response:**

```json
{
  "status": "success",
  "macros": [
    {
      "name": "exclude_noise",
      "definition": "search NOT type=\"HeartbeatEvent\"",
      "args": [],
      "description": "Filter out noise events"
    }
  ]
}
```

#### `POST /api/macros/create`

Create a new macro.

**Request:**

```json
{
  "name": "top_by_field",
  "definition": "stats count by $field$ | sort - count | head $limit$",
  "args": ["field", "limit"],
  "description": "Top N values by any field"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Macro name (used in backtick calls). |
| `definition` | Yes | SPQL fragment. Use `$arg$` for parameters. |
| `args` | No | List of argument names. |
| `description` | No | Human-readable description. |
| `overwrite` | No | Set `true` to replace an existing macro. |

#### `GET /api/macros/<name>`

Retrieve a single macro.

#### `PUT /api/macros/<name>`

Update an existing macro.

#### `DELETE /api/macros/<name>`

Delete a macro.

#### `POST /api/macros/expand`

Expand macro calls in a query without executing it.

**Request:**

```json
{"query": "index=\"logs\" | `exclude_noise` | head 10"}
```

**Response:**

```json
{
  "status": "success",
  "original": "index=\"logs\" | `exclude_noise` | head 10",
  "expanded": "index=\"logs\" | search NOT type=\"HeartbeatEvent\" | head 10"
}
```

#### `POST /api/macros/expand-annotated`

Expand macros with inline annotation comments showing which macro produced each expansion.

**Request:**

```json
{
  "query": "index=\"logs\" | `exclude_noise`",
  "depth": 0,
  "max_depth": 100
}
```

#### `POST /api/macros/test`

Expand macros in a query, then execute it and return results.

**Request:**

```json
{"query": "index=\"logs\" | `exclude_noise` | stats count"}
```

**Response:**

```json
{
  "status": "success",
  "expanded": "index=\"logs\" | search NOT type=\"HeartbeatEvent\" | stats count",
  "columns": ["count"],
  "rows": [{"count": 42}],
  "total": 1
}
```

---

### Analyzer Prompts (`/api/analyzer-prompts/*`)

Reusable prompt templates for the [Claude Analyzer](11_claude_analyzer.md). Each prompt is a YAML file in `analyzer_prompts/` with `$token$` placeholders.

#### `GET /api/analyzer-prompts/list`

List all analyzer prompts.

**Response:**

```json
{
  "status": "success",
  "prompts": [
    {
      "name": "polymarket_volume_spike",
      "description": "Analyze Polymarket volume spikes",
      "prompt_text": "You are analyzing results from \"$scheduled_search_name$\"...",
      "created_at": "2026-04-07T12:00:00",
      "updated_at": "2026-04-07T12:00:00"
    }
  ]
}
```

#### `POST /api/analyzer-prompts/create`

Create a new analyzer prompt.

**Request:**

```json
{
  "name": "market_spike_analysis",
  "description": "Analyze market volume spikes",
  "prompt_text": "You are analyzing results from \"$scheduled_search_name$\".\nMarkets: $question$\nSpike: $spike_multiple$\n\nRespond with JSON: {alert_priority, summary, actionable_markets, pattern_detected, cross_reference_needed}"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique name. Letters, digits, spaces, hyphens, underscores, periods. |
| `prompt_text` | Yes | Template text with `$token$` placeholders. |
| `description` | No | Human-readable description. |
| `overwrite` | No | Set `true` to replace an existing prompt. |

#### `GET /api/analyzer-prompts/<name>`

Retrieve a single analyzer prompt.

#### `PUT /api/analyzer-prompts/<name>`

Update an existing analyzer prompt. Updatable fields: `description`, `prompt_text`.

#### `DELETE /api/analyzer-prompts/<name>`

Soft-delete an analyzer prompt (archived in `last_chance.sqlite` for 30 days).

#### `GET /api/analyzer-prompts/<name>/yaml`

Return the raw YAML text for display.

#### `POST /api/analyzer-prompts/validate-tokens`

Validate that `$token$` placeholders in a prompt resolve against a query's output columns and the set of known global tokens.

**Request:**

```json
{
  "prompt_text": "$scheduled_search_name$ found $question$ with $nonexistent$",
  "query": "index=\"polymarket\" | head 5"
}
```

**Response:**

```json
{
  "status": "success",
  "valid": false,
  "global_tokens": ["scheduled_search_name"],
  "column_tokens": ["question"],
  "unresolved": ["nonexistent"],
  "all_tokens": ["nonexistent", "question", "scheduled_search_name"]
}
```

**Notes:**

- If `query` is provided, it is executed to discover available column names. If omitted, only global tokens are validated.
- Unresolved tokens don't cause errors - they are left as literal `$token$` text in the resolved prompt.

---

### Boilerplate Prompts (`/api/boilerplate-prompts/*`)

Reusable prompt templates for [Alert Groups](12_alert_groups.md). Each prompt is a YAML file in `boilerplate_prompts/` whose `template` text is injected into the alert group's dispatch prompt.

#### `GET /api/boilerplate-prompts/list`

List all boilerplate prompts.

#### `POST /api/boilerplate-prompts/create`

Create a new boilerplate prompt.

**Request:**

```json
{
  "name": "daily_brief_header",
  "template": "You are composing a daily brief...",
  "description": "Standard brief framing"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique name. |
| `template` | Yes | Prompt template text. |
| `description` | No | Human-readable description. |
| `overwrite` | No | Set `true` to replace an existing prompt (otherwise returns `status: "exists"`). |

#### `GET /api/boilerplate-prompts/<name>`

Retrieve a single boilerplate prompt.

#### `PUT /api/boilerplate-prompts/<name>`

Update an existing boilerplate prompt.

#### `DELETE /api/boilerplate-prompts/<name>`

Soft-delete a boilerplate prompt (archived in `last_chance.sqlite` for 30 days).

#### `GET /api/boilerplate-prompts/<name>/yaml`

Return the raw YAML text for display.

---

### Analyzer API Key (`/api/settings/analyzer-key`)

Manage the Claude analyzer API key. The key is stored in the Fernet-encrypted credential vault (same vault used for ingestion script credentials) with a reserved `script_id=-1` for system-level credentials.

#### `GET /api/settings/analyzer-key`

Check whether an API key is stored. **Never returns the key value.**

**Response:**

```json
{
  "status": "success",
  "has_key": true
}
```

#### `POST /api/settings/analyzer-key`

Store (or replace) the analyzer API key.

**Body:**

```json
{
  "value": "sk-ant-..."
}
```

**Response:**

```json
{
  "status": "success",
  "message": "Analyzer API key stored securely."
}
```

#### `DELETE /api/settings/analyzer-key`

Remove the stored analyzer API key.

**Response:**

```json
{
  "status": "success",
  "message": "Analyzer API key deleted."
}
```

---

### Claude API Diagnostics and History (`/api/analyzer/test`, `/api/claude-history/*`)

#### `POST /api/analyzer/test`

Fire a minimal Claude API call (16-token probe against Haiku) to verify the stored key works end-to-end. Accepts an optional `value` in the body to test a key that has not yet been saved.

**Request:**

```json
{"value": "sk-ant-..."}
```

Or `{}` to test the currently-stored key.

**Success response (HTTP 200):**

```json
{
  "status": "success", "ok": true,
  "request_id": "rid-1",
  "model": "claude-haiku-4-5-20251001",
  "latency_ms": 420,
  "input_tokens": 7, "output_tokens": 2,
  "cost_usd": 0.000047,
  "attempts": 1
}
```

**Failure response (HTTP 400):**

```json
{
  "status": "error", "ok": false,
  "error_class": "AuthenticationError",
  "error_message": "invalid x-api-key",
  "attempts": 1
}
```

Every test call is persisted to `claude_api_history.sqlite` and the `indexes/logs/claude_api/*.parquet` log stream - treat it like any other billable call.

#### `GET /api/claude-history`

Paginated list of every Claude API call (alert groups, analyzer, settings test, batch submissions). Query params:

| Param | Default | Purpose |
|-------|---------|---------|
| `limit` | 50 | Max 500 rows. |
| `offset` | 0 | Pagination offset. |
| `since` | - | Unix epoch seconds lower bound. |
| `until` | - | Unix epoch seconds upper bound. |
| `source` | - | `alert_group` / `analyzer` / `settings_test` / `batch_submit`. |
| `group_name` | - | Only rows for this alert group. |
| `status` | - | `success` / `error` / `timeout`. |
| `payloads` | `0` | Set to `1` to include decoded request + response bodies. |

#### `GET /api/claude-history/<request_id>`

Fetch one call with fully-decoded request and response payloads. Returns 404 if the ID is unknown.

#### `GET /api/claude-history/stats`

Aggregate tokens, cost, success/error counts, and DB size across the selected range. Same filters as the list endpoint.

```json
{
  "status": "success",
  "stats": {
    "calls": 42, "success_count": 40, "error_count": 2,
    "input_tokens": 12345, "output_tokens": 6789,
    "cache_read_tokens": 100, "cache_creation_tokens": 0,
    "cost_usd": 0.135, "db_size_bytes": 204800
  }
}
```

#### `POST /api/claude-history/vacuum`

Optional `older_than_epoch` deletes rows before the cutoff, then `VACUUM` reclaims disk space. Always back up the DB file before calling - prunes are one-way.

**Request:**

```json
{"older_than_epoch": 1776000000}
```

**Response:**

```json
{"status": "success", "removed": 128, "db_size_bytes": 65536}
```

---

### Credentials Vault (`/api/credentials/*`)

Store and manage encrypted API keys for ingestion scripts. Values are Fernet-encrypted at rest; only key names are ever returned via the API.

#### `GET /api/credentials/<script_id>`

List credential key names for a script. Values are never exposed. The default `keys` field is the merged per-task + global list - what the script will actually resolve at runtime. Pass `?split=true` to also get `per_script` and `global` arrays separately.

**Response:**

```json
{
  "status": "success",
  "keys": ["api_key", "webhook_secret"]
}
```

**Note:** Use `script_id=0` for staging credentials (pre-save testing).

#### `POST /api/credentials/<script_id>`

Store an encrypted credential.

**Request:**

```json
{
  "key_name": "api_key",
  "value": "sk-..."
}
```

**Response:**

```json
{"status": "success", "message": "Credential 'api_key' stored."}
```

**curl example:**

```bash
curl -X POST http://localhost:5111/api/credentials/1 \
  -H "Content-Type: application/json" \
  -d '{"key_name": "api_key", "value": "your-secret-key"}'
```

#### `DELETE /api/credentials/<script_id>/<key_name>`

Delete a single credential.

#### `POST /api/credentials/<script_id>/<key_name>/promote-to-global`

Promote a per-script credential to the global vault. The value is decrypted server-side, re-encrypted as a global, and the per-script entry is removed - plaintext never leaves the server. After the promote, every script declaring `key_name` in `requires_credentials` resolves the value automatically. Returns 404 if the per-script credential doesn't exist.

#### `GET /api/credentials/global`

List global credential key names (never values).

#### `POST /api/credentials/global`

Store (or update) a global credential.

**Request:**

```json
{"key_name": "FRED_API_KEY", "value": "..."}
```

#### `DELETE /api/credentials/global/<key_name>`

Delete a single global credential. Per-task entries with the same name are preserved (they continue to resolve via the per-task layer).

---

### Jobs (`/api/jobs/*`)

Query results are stored in a ring buffer (last 10 by default). Jobs can be promoted to saved jobs with configurable TTL.

#### `GET /api/jobs`

List all non-expired jobs, newest first.

**Response:**

```json
{
  "status": "success",
  "jobs": [
    {
      "job_id": "auto_1711234567_abc123",
      "query": "index=\"logs\" | head 10",
      "created": "2026-03-22T12:00:00",
      "row_count": 10,
      "saved": false
    }
  ]
}
```

#### `GET /api/jobs/<job_id>`

Retrieve metadata for a specific job.

#### `POST /api/jobs/<job_id>/save`

Promote a job to a saved job with extended retention.

**Request:**

```json
{
  "name": "my_analysis",
  "ttl_days": 10,
  "save_to_lookups": false
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | No | Custom display name. |
| `ttl_days` | No | Days to retain (default: 10). |
| `save_to_lookups` | No | If `true`, also saves results as a lookup file. |

#### `DELETE /api/jobs/<job_id>`

Delete a job.

---

### Settings (`/api/settings/*`)

Manage global configuration - storage limits, maintenance intervals, ingestion defaults, security, and email (SMTP).

#### `GET /api/settings`

Retrieve all current settings.

**Response:**

```json
{
  "status": "success",
  "settings": {
    "indexes_root": "indexes",
    "max_total_size_gb": 50,
    "max_subdirectory_size_gb": 10,
    "max_parquet_file_mb": 128,
    "cleanup_interval_hours": 6,
    "default_script_timeout_seconds": 120,
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_starttls": true,
    "smtp_from": ""
  }
}
```

#### `POST /api/settings`

Update one or more settings. Send only the fields you want to change.

**Request:**

```json
{
  "max_total_size_gb": 100,
  "default_script_timeout_seconds": 300
}
```

**Response (partial failure):**

```json
{
  "status": "partial",
  "errors": ["max_total_size_gb must be between 1 and 10000"],
  "settings": { ... }
}
```

#### `POST /api/settings/reset`

Reset all settings to their defaults.

---

### Grammar Vocabulary (`/api/grammar/vocab`)

Exposes the SPQL vocabulary derived from `lexers/speakesQuery.g4`. Used by the console's client-side autocomplete, and available to external tools (linters, editor plugins) that need the canonical list of commands, functions, keywords, and operators.

#### `GET /api/grammar/vocab`

**Response:**

```json
{
  "status": "success",
  "vocab": {
    "version": 1,
    "commands":  [{"name": "search",   "kind": "directive"}, ...],
    "functions": [{"name": "round",    "kind": "numeric"},   ...],
    "keywords":  ["AND", "OR", "NOT", "BY", "AS", "IN"],
    "operators": ["=", "!=", "<", ">", "<=", ">="],
    "booleans":  ["true", "false"],
    "time_units": ["second", "minute", "hour", "day", "week", "year"]
  }
}
```

Bump the top-level `version` field if the vocab shape changes; clients should treat the shape as stable within a version.

---

### System Clock (`/api/system/clock`)

#### `GET /api/system/clock`

Return the server's current time plus the scheduler timezone. The UI top bar polls this so the operator can sanity-check "what time does SpeakesQuery think it is?" when writing cron expressions. The scheduler TZ is the authoritative timezone for every cron field on every alert group and saved search - SpeakesQuery forces UTC on boot so behavior is independent of the Docker host's system TZ.

**Response:**

```json
{
  "server_time_utc": "2026-07-02 14:30:00",
  "server_time_iso": "2026-07-02T14:30:00+00:00",
  "scheduler_timezone": "UTC",
  "system_timezone": "UTC",
  "epoch": 1782052200,
  "note": "All cron expressions ... are interpreted in scheduler_timezone. ..."
}
```

---

### Visual Builder Parse (`/api/visual-builder/parse`)

#### `POST /api/visual-builder/parse`

Parse an SPQL string into `{index_clause, stages}` for the Visual Builder canvas. The SPA's "Load" button POSTs operator-pasted SPQL here and uses the returned structure to populate stage cards. The parser is grammar-version-stable - a flat split-on-pipe-outside-quotes, not ANTLR.

**Request:**

```json
{"spql": "index=\"indexes/app_logs/*\" | stats count by level | head 10"}
```

**Response:**

```json
{
  "status": "success",
  "index_clause": "index=\"indexes/app_logs/*\"",
  "stages": [
    {"command": "stats", "kwargs": "count by level"},
    {"command": "head", "kwargs": "10"}
  ]
}
```

Returns 400 on missing / non-string `spql`.

---

### Topology (`/api/topology`) - Wave 4 (2026-04-25)

#### `GET /api/topology`

Returns the canonical adjacency graph that powers the cross-link badges in the Searches / Ingestion Scripts / Alert Groups tabs. One fetch per page-load; the SPA caches client-side and joins by name.

Edge model:

| From | To | Computed via |
|---|---|---|
| Saved search | Index path(s) | `index="..."` clauses extracted from the SPQL query |
| Index path | Ingestion task | Task `subdirectory` matches the normalized path |
| Index path | Library script | Script `suggested_subdirectory` matches |
| Saved search | Alert group | AG `search_names` list |

Each edge is materialized in both directions so a single lookup by name surfaces every relationship.

**Response:**

```json
{
  "status": "success",
  "searches": [
    {
      "name": "dob_macro_regime",
      "indexes": ["indexes/fred/dxy_regime/*.parquet"],
      "subdirs": ["fred/dxy_regime"],
      "tasks": [
        {"id": 12, "title": "FRED DXY Regime",
         "library_script_id": "fred_dxy_regime",
         "subdirectory": "fred/dxy_regime", "disabled": false}
      ],
      "alert_groups": ["daily_opportunity_brief"]
    }
  ],
  "tasks": [
    {
      "id": 12, "title": "FRED DXY Regime",
      "subdirectory": "fred/dxy_regime",
      "library_script_id": "fred_dxy_regime", "disabled": false,
      "feeds_searches": ["dob_macro_regime"],
      "feeds_alert_groups": ["daily_opportunity_brief"]
    }
  ],
  "alert_groups": [
    {
      "name": "daily_opportunity_brief",
      "search_names": ["dob_macro_regime", "..."],
      "feeders": [
        {"search_name": "dob_macro_regime",
         "indexes": ["indexes/fred/dxy_regime/*.parquet"],
         "subdirs": ["fred/dxy_regime"],
         "tasks": [{"id": 12, "...": "..."}]}
      ]
    }
  ],
  "scripts": [
    {"id": "fred_dxy_regime",
     "suggested_subdirectory": "fred/dxy_regime",
     "deployed_as_tasks": [12]}
  ]
}
```

**Reverse-link invariants** (pinned by `tests/test_wave4_cross_linking.py`):

- AG references search → search's `alert_groups` array contains AG name
- Search targets subdir → task with that subdir lists search in `feeds_searches`

These let any consumer trust either direction of an edge without recomputing.

---

### Schedule Volume (`/api/schedule/volume`) - Wave 6 (2026-04-26)

#### `GET /api/schedule/volume`

Per-day activity buckets powering the Recent Activity bar + line charts on the Schedule page. Aggregates from `indexes/logs/{ingestion,search_runs,alert_groups}/*.parquet`.

**Query params:**

| Name | Type | Default | Notes |
|---|---|---|---|
| `days` | int | 14 | Window size, clamped to `[1, 365]`. |

**Response:**

```json
{
  "status": "success",
  "days": 14,
  "buckets": [
    {
      "date": "2026-04-12",
      "ingestion_runs": 35,
      "search_runs": 200,
      "ag_dispatches": 11,
      "rows_ingested": 1542
    },
    /* ... one bucket per UTC day, oldest → newest ... */
  ]
}
```

Empty days are pre-zeroed so the chart x-axis is uniform; missing log directories yield zero buckets, never errors.

---

### Schedule Operations Report PDF (`/api/schedule/pdf`) - 2026-05-01

#### `GET /api/schedule/pdf`

Generate a polished, branded multi-page PDF report of the entire scheduled-job landscape. Includes a cover page, executive summary with stat tiles + headline paragraph, both heatmaps (firing count + expected data volume) as inline SVG, the recent-activity bar+line charts, per-AG feeder health blocks (each AG paired with its feeders and OK/EMPTY/NEVER-RAN/MISSING pill), highlights & anomalies (never-ran, empty-output, latency outliers, disabled), and an all-jobs appendix table. Backed by [tools/schedule_pdf.py](tools/schedule_pdf.py).

Renderer: WeasyPrint (HTML+CSS → PDF). The Dockerfile installs the required system libs (libpango, libcairo, libgdk-pixbuf, libffi, shared-mime-info). On macOS local dev, install via `brew install pango cairo gdk-pixbuf libffi`.

**Query params:**

| Name | Type | Default | Notes |
|---|---|---|---|
| `lookahead_days` | int | 7 | Cron expansion window for the heatmaps, clamped to `[1, 30]`. |
| `history_runs` | int | 5 | Recent runs averaged per job for `avg_row_count` / `avg_duration_ms`, clamped to `[1, 50]`. |
| `history_days` | int | 30 | Log lookback window for run history, clamped to `[1, 180]`. |
| `activity_days` | int | 14 | Bar/line chart window for Recent Activity, clamped to `[1, 365]`. |
| `include_disabled` | bool | false | Count disabled jobs in the heatmap totals; they always appear in the appendix. |

**Response:**

`Content-Type: application/pdf`. Body is the PDF bytes. Filename suggested via `Content-Disposition: attachment; filename="speakesquery-schedule-report-YYYYMMDD-HHMM.pdf"`.

**Errors:**

* `503 Service Unavailable` - WeasyPrint isn't installed. Body includes a `hint` field with the install command.
* `500 Internal Server Error` - render failed. Body has `message`.

**CLI alternative:**

```bash
python -m tools.schedule_pdf --output schedule-report-$(date +%Y%m%d).pdf \
  --lookahead-days 7 --activity-days 14 --history-runs 5
```

Useful for cron-driven weekly archives. Same options as the HTTP endpoint.

---

### Email Test

#### `POST /api/email/test`

Send a test email to verify SMTP configuration.

**Request:**

```json
{"to": "you@example.com"}
```

**Response:**

```json
{"status": "success", "message": "Test email sent to you@example.com."}
```

---

## Quick Reference

Additional utility endpoints:

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/indexes/import` | Import CSV/Parquet/SQLite file as a queryable index (`multipart/form-data`). |
| `POST` | `/api/indexes/import/sqlite-tables` | List table names in a SQLite file before importing. |
| `GET` | `/api/version` | Returns `{"version": "0.9.0-beta"}` |
| `GET` | `/api/grammar/vocab` | SPQL vocabulary (commands, functions, keywords) parsed from `speakesQuery.g4`; backs console autocomplete. |
| `POST` | `/api/email/diagnose` | Step-by-step SMTP diagnostic (TCP → STARTTLS → AUTH → optional send). Body: `{"send_to": "you@example.com", "strip_password": false}`. Same logic as `python -m tools.smtp_diagnose`. |
| `GET` | `/api/tree` | Directory tree of `.parquet` files under `indexes/`. Optional `?path=` to switch root. |
| `POST` | `/api/save` | Export results as downloadable CSV or JSON. Body: `{"results": [...], "columns": [...], "format": "csv"}` |
| `GET` | `/api/library/list` | List all premade ingestion scripts from the script library. |
| `GET` | `/api/library/<script_id>` | Get full details (including code) for a library script. |
| `GET` | `/api/docs/` | List all documentation files with titles. |
| `GET` | `/api/docs/<filename>` | Return raw Markdown content of a documentation file. |
| `GET` | `/api/system/clock` | Server time + scheduler timezone (see [System Clock](#system-clock-apisystemclock)). |
| `GET` | `/api/persistence/audit` | Persistence inventory for the UI banner: per-target `{path, kind, exists, size}` plus `total`/`healthy`/`issues` so a mis-configured Docker bind-mount surfaces as a warning. Cheap (stat only, no hashing). |

---

## Configuration and Best Practices

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Bind address. Set to `127.0.0.1` for localhost-only access. |
| `PORT` | `5111` | Server port. |
| `SMTP_SERVER` | `smtp.gmail.com` | SMTP relay host (can also be set via Settings UI). |
| `SMTP_PORT` | `587` | SMTP port. |
| `SMTP_USER` | - | SMTP username. |
| `SMTP_PASSWORD` | - | SMTP password or app password. |
| `SMTP_STARTTLS` | `true` | Enable STARTTLS. |
| `SMTP_FROM` | - | Sender address for alert emails. |

### Reverse proxy deployment

For production or LAN-facing deployments, place SpeakesQuery behind a reverse proxy:

```nginx
# nginx example
server {
    listen 443 ssl;
    server_name speakesquery.internal;

    ssl_certificate     /etc/ssl/certs/speakesquery.pem;
    ssl_certificate_key /etc/ssl/private/speakesquery.key;

    location / {
        proxy_pass http://127.0.0.1:5111;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Set `HOST=127.0.0.1` when using a reverse proxy so that the Flask server only accepts connections from the proxy, not directly from the network.

### Scripting patterns

**Run a query and save results:**

```bash
# Execute query
RESULT=$(curl -s -X POST http://localhost:5111/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "index=\"indexes/app_logs/*\" | stats count by level"}')

# Check status
echo "$RESULT" | jq '.status'

# Extract results
echo "$RESULT" | jq '.results'
```

**Create an ingestion script and test it:**

```bash
# Create
curl -s -X POST http://localhost:5111/api/si/add \
  -H "Content-Type: application/json" \
  -d @my_script.json | jq '.task.id'

# Test (using the returned ID)
curl -s -X POST http://localhost:5111/api/si/3/test | jq '.summary'
```

**Programmatic saved search setup:**

```bash
# Create a saved search
curl -s -X POST http://localhost:5111/api/ss/create \
  -H "Content-Type: application/json" \
  -d '{
    "name": "error_spike",
    "query": "index=\"indexes/app_logs/*\" | stats count by level | where count > 100",
    "cron_schedule": "*/5 * * * *",
    "lookback": "1h",
    "email_address": "oncall@example.com",
    "email_body": "Error spike detected: $count$ errors at level $level$"
  }'
```

### Error handling

All error responses follow the same envelope:

```json
{"status": "error", "message": "Descriptive error message."}
```

HTTP status codes:
- `200` - Success (even for query errors that return `status: "error"` in the body).
- `400` - Bad request (missing fields, invalid input).
- `404` - Resource not found.
- `500` - Internal server error.

**Important:** Query execution errors (`POST /api/query`) return HTTP 200 with `"status": "error"` in the JSON body. Always check the `status` field rather than relying solely on HTTP status codes.

---

## Alert Groups (`/api/alert-groups/*`)

Multi-search Claude (or local-model) dispatch. For concepts, configuration fields, feeder health states, and worked examples, see the [Alert Groups Guide](12_alert_groups.md).

### `GET /api/alert-groups/list`
Return all alert groups with their next scheduled run time.

### `POST /api/alert-groups/create`
Create a new alert group. Required fields: `name`, `search_names` (non-empty list), `prompt_text`. Optional `overwrite` (bool) replaces an existing group; otherwise a name collision returns `{status: "exists"}`. The cron job is registered with the scheduler immediately - no restart needed.

### `GET /api/alert-groups/<name>`
Return a single alert group by name.

### `PUT /api/alert-groups/<name>`
Update an existing alert group. Re-registers the APScheduler cron job after the save so schedule/disabled edits take effect immediately.

### `DELETE /api/alert-groups/<name>`
Soft-delete an alert group (archived in `last_chance.sqlite` for 30 days). The cron job is removed immediately.

### `GET /api/alert-groups/<name>/yaml`
Return the raw YAML text for an alert group.

### `POST /api/alert-groups/<name>/run`
Manually trigger a dispatch (bypasses the schedule). Query params:

- `dry_run=true` - runs everything up to the messages build but skips the Claude API call and the email send; the response still carries the full payload that would have been sent.
- `force=true` - bypasses the per-AG rate limit (`max_dispatches_per_day` / `min_interval_between_runs_hours`) and the circuit breaker. Budget + freshness checks still run.

### `GET /api/alert-groups/<name>/dispatch-progress`
Return live progress of an in-flight dispatch. The UI polls this (1–2 s cadence) during a manual Run so the operator sees phase-by-phase progress (`feeder_loop`, `calling_claude`, `sending_email`, `done_success`, …) with per-feeder `[N/total]` counters and elapsed times. Entries are kept for 120 s after completion so a late poll can still read the terminal status.

### `POST /api/alert-groups/<name>/enable`
Enable an alert group.

### `POST /api/alert-groups/<name>/disable`
Disable an alert group.

### `GET /api/alert-groups/runs`
Return recent run history across all groups. Query params: `?group_name=...&limit=50`.

### `POST /api/alert-groups/<name>/debug-report`
Run every saved search referenced by the AG and return a structured debug report (`searches` list + pasteable `report_text`) for iterative query-quality refinement. Does NOT call Claude, consume budget, or send email - pure diagnostic.

### `GET /api/alert-groups/<name>/metrics`
Aggregate success rate, cost, latency, and error streaks for an AG from `alert_group_runs.sqlite` + `claude_api_history.sqlite`. Window selectable via `?hours=24` (max 2160).

```json
{
  "status": "success",
  "metrics": {
    "window_hours": 24, "total_runs": 4,
    "success": 4, "error": 0, "skipped": 0, "success_rate": 1.0,
    "total_cost_usd": 0.135, "avg_cost_usd": 0.034, "max_cost_usd": 0.041,
    "total_tokens": 48200, "avg_tokens": 12050,
    "consecutive_errors": 0,
    "claude_call_count": 4, "claude_total_cost_usd": 0.135
  }
}
```

### `POST /api/alert-groups/<name>/reset-circuit-breaker`
Clear a tripped circuit breaker so the AG can dispatch again.

### `GET /api/alert-groups/<name>/feeder-status`
Report health of every saved search referenced by the group: whether each feeder maps to a library script, whether that script is deployed, whether required credentials are set, and whether data has landed in the expected index directory. See [Feeder Health](12_alert_groups.md#feeder-health) for the state vocabulary.

### `GET /api/alert-groups/<name>/pipeline-health`
Deep health check: in addition to deployment/credential state, actually runs each feeder's SPQL against the live indexes and reports row count, any query error, and returned columns. Catches silent breakage the basic feeder-status check misses (e.g. an upstream API format change where the parquet still lands but the SPQL raises).

### `POST /api/alert-groups/<name>/deploy-feeders`
Bulk-deploy every library script referenced by the group's feeders that isn't already scheduled. Query params: `run_after_deploy=true` (default) also triggers an immediate run for newly-deployed and existing-but-empty tasks; `max_run_workers=4` bounds the run-now thread pool (cap 8). Returns a per-feeder action summary (installed / deployed / ran / skipped / failed).

### `POST /api/alert-groups/<name>/install-default-feeder/<search_name>`
Install a single missing default-template saved search (copies `default_saved_searches/<search_name>.yaml` into `saved_searches/`). Query param `overwrite=true` force-replaces an already-installed YAML with the current template (for syncing stale copies after a template bug-fix).

### `POST /api/alert-groups/<name>/manual-return`
Accept an operator-pasted brief returned from an external LLM (typical with `delivery_mode: "prompt_only"`, where the user runs the prompt in a chat UI and wants the resulting picks captured into `indexes/logs/ag_picks/`). Body: `raw_text` (required - full LLM response, fenced JSON block included), `model_used` (required), optional `dispatch_run_id` to join picks to the originating dispatch.

### `GET /api/alert-groups/<ag_name>/as-notebook`
Return a synthetic notebook record built from an existing AG (round-trip path: AG → notebook; save the result via `POST /api/notebooks` to spawn an editable copy). Pure read - no side effects on the source AG. 404 if the AG doesn't exist.

---

## Email Groups (`/api/email-groups/*`)

Reusable mailing lists. Reference a group from any saved search or alert group's `email_address` field as `@group_name` and the recipients are expanded at send time. Groups can reference other groups (nested mailing lists); cycles are detected and broken safely; unknown group references log a WARNING and are silently skipped so a typo never blocks a send that has at least some valid literal recipients.

### `GET /api/email-groups/list`
Return all email groups.

### `POST /api/email-groups/create`
Create a new group. Body fields: `name` (snake_case ASCII), `email_addresses` (list of literals or `@group_name` refs), optional `description`, optional `overwrite` (bool). Returns `{status: "success" | "exists" | "error"}`.

### `GET /api/email-groups/<name>`
Return a single group plus a `resolved_recipients` preview (`@group_name` refs expanded into literal addresses).

### `PUT /api/email-groups/<name>`
Update an existing group's `description` and/or `email_addresses`. The `name` is immutable.

### `DELETE /api/email-groups/<name>`
Hard-delete the YAML.

### `POST /api/email-groups/preview`
Resolve a raw recipients string (or list) into the literal list that would be sent. Useful for preview-before-save when a saved search or alert group references `@group_name`. Body: `{"recipients": "alice@x.com, @ops_team, @sales"}`.

---

## Schedule Visualization (`/api/schedule/*`)

Aggregate view of every scheduled job (ingestion tasks, saved searches, alert groups). Used by the Schedule page heatmap to identify overloaded UTC hours when planning new schedules.

### `GET /api/schedule/heatmap`

Returns: jobs + per-(day-of-week, hour) firing counts + per-cell expected data volume + per-job recent-run averages.

Query params (all optional):

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `lookahead_days` | `7` | 1–30 | Cron expansion window |
| `history_runs` | `5` | 1–50 | Recent runs to average per job |
| `history_days` | `30` | 1–180 | Log-lookback window for history reads |
| `include_disabled` | `false` | bool | Include disabled jobs in counts |

Response shape (truncated):

```json
{
  "status": "success",
  "generated_at_epoch": 1745594000,
  "lookahead_days": 7,
  "jobs": [
    {
      "name": "fred_dxy_regime",
      "kind": "ingestion",
      "cron": "30 */6 * * *",
      "next_firing_iso": "2026-04-25T14:30:00+00:00",
      "firings_in_lookahead": 28,
      "run_count": 5,
      "avg_row_count": 3.0,
      "avg_duration_ms": 1850.0
    }
  ],
  "hour_distribution": {
    "by_dow_hour": {"0": [24 ints], ..., "6": [...]},
    "by_hour_total": [24 ints],
    "total_firings": 142
  },
  "data_distribution": {
    "by_dow_hour": {...},
    "by_dow_hour_has_data": {...},
    "by_hour_total": [24 floats]
  },
  "summary": {
    "total_jobs": 35,
    "by_kind": {"ingestion": 18, "saved_search": 14, "alert_group": 3},
    "busiest_hour_utc": 14,
    "busiest_hour_count": 18,
    "biggest_data_hour_utc": 11,
    "biggest_data_hour_total": 4250.0
  }
}
```

`by_dow_hour` keys: Monday=0 … Sunday=6 (Python `datetime.weekday()` convention). Each row has 24 integer cells (hour 00–23 UTC).

The `hour_distribution` is COUNT of firings; the `data_distribution` estimates row volume = sum of (firings × avg_row_count) per cell. `by_dow_hour_has_data` distinguishes "no data yet" from "literal zero rows".

---

## Notebooks (`/api/notebooks/*`)

Reactive SPQL notebooks (cells, cache, exports, promote-to-alert-group). For cell types, the cache model, and the dev→prod deploy loop, see the [Notebooks Guide](19_notebooks.md).

### `GET /api/notebooks`
List all notebooks (lightweight summary: id + name + description + cell_count + timestamps). Full records come from `GET /api/notebooks/<id>`.

### `POST /api/notebooks`
Create a new notebook (or overwrite an existing one when `overwrite=true`).

### `GET /api/notebooks/<id>`
Return the full notebook record, including all cell sources.

### `PUT /api/notebooks/<id>`
Update an existing notebook. Cells are replaced wholesale.

### `DELETE /api/notebooks/<id>`
Delete a notebook and cascade-invalidate its cache entries.

### `POST /api/notebooks/<id>/execute`
Execute a notebook top-to-bottom and return the run result. Body fields (all optional):

| Field | Default | Description |
|-------|---------|-------------|
| `use_cache` | `true` | Reactive cache control - pass `false` to force full re-execution. |
| `namespace_overrides` | `{}` | Param-form value overrides, keyed by param-cell id. Param cells bypass the cache. |
| `stop_at_cell_id` | `null` | Per-cell Run: execute cells `[0..N]` only, where N is the cell with this id. |

### `GET /api/notebooks/_cache/stats`
Return cache statistics (entries, size, hits).

### `POST /api/notebooks/_cache/clear`
Drop EVERY cache entry. Admin-grade - the API applies no confirmation prompt; the UI confirms before posting.

### `POST /api/notebooks/_install_default/<id>`
Re-install a project-shipped default notebook (no-op if already present unless `overwrite=true`).

### `POST /api/notebooks/<id>/export/html`
Export a notebook as a self-contained HTML page. Optional body field `run_first` (bool, default `false`) executes the notebook first so cell outputs appear in the export. Returns `text/html` with a JSON sidecar (`<script type="application/json" id="notebook-data">`) so agents can ingest the export without HTML scraping.

### `POST /api/notebooks/<id>/export/pdf`
Export a notebook as a PDF via WeasyPrint. Same body shape as `/export/html`. Limitation: WeasyPrint is a static renderer - Vega-Lite chart cells appear as their JSON spec text; use the HTML export when you want rendered charts.

### `GET /api/notebooks/<id>/promote/<cell_id>/preview`
Return the dry-run preview for a `promote_to_alert_group` cell - the same structured payload the cell engine emits at execution time. Never mutates alert-group state. 404 if the notebook or cell doesn't exist; 200 with `decision: "blocked"` and an `errors` list when the cell metadata is malformed.

### `POST /api/notebooks/<id>/promote/<cell_id>`
Deploy a `promote_to_alert_group` cell - the ONLY notebook-surface path that actually creates/updates the alert group YAML. Optional body field `overwrite_existing` (bool, default `true`); pass `false` to make an existing AG name a hard error. Returns `{status, ag, deploy_record}` on success, 400 with a structured `error_class` on validation failure, 404 if the notebook or cell doesn't exist.

---

## Models Registry (`/api/models`)

### `GET /api/models`

Return the LLM model registry (`models/<id>.yaml` via `model_store`). Used by the notebook SPA's pipe-cell model picker and any surface that needs to enumerate available models for `| llm` / alert-group `model_id` dispatch.

**Response:**

```json
{
  "status": "success",
  "models": [
    {
      "id": "llamacpp-qwen35-122b-a10b",
      "provider": "lmstudio",
      "model_name": "Qwen3.5-122B-A10B",
      "description": "...",
      "endpoint": "http://192.168.1.50:8085/v1",
      "cost_per_input_million_usd": 0.0,
      "cost_per_output_million_usd": 0.0,
      "max_output_tokens": 8192,
      "default_timeout_seconds": 600
    }
  ]
}
```

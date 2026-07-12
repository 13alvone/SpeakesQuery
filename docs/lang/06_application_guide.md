# Application Guide

This guide walks through every tab in the SpeakesQuery desktop application - what each screen does, how to fill out its forms, and the key things to remember when using it.

> **Tip:** The language reference (syntax, commands, functions) lives in the other documentation sections. This guide focuses on *using the application itself*.

## Top navigation (2026-04-26 redesign)

The top bar collapses into **six group dropdowns** instead of fourteen flat tabs:

| Group | Tabs |
|---|---|
| **Data** | Query · Lookups · Import |
| **Search** | Create Search · Searches · Macros |
| **Ingestion** | Create Ingestion · Ingestion Scripts · Script Library |
| **Alerts** | Alert Groups · Email Groups · Schedule |
| **Develop** | Notebooks · Visual Builder |
| **Help** | Settings · Docs |

**How to open a dropdown.** Hover the group name (Data, Search, etc.) to expand its panel; click the group name to keep it open until you select a tab; click outside the panel or press `Esc` to close it. Touchscreen / keyboard users can `Tab` to a group button and press `Enter` (or `Space`) to toggle.

**Where you are.** Whichever group contains the currently active tab is underlined in the accent color, so the parent dropdown stays visually selected even when its panel is closed. Inside an open panel, the active leaf is highlighted with a subtle accent bar on the left edge.

The bar wraps onto a second row on narrow viewports. The `data-page` routing is unchanged, so every existing deep link, cross-tab navigation badge, and JS callsite (`document.querySelector('.nav-tab[data-page="..."]').click()`) keeps working without modification.

---

## Query

The Query tab is where you write and run SpeakesQuery queries against your ingested data.

### Layout

| Area | Purpose |
|------|---------|
| **Query textarea** | Write your SpeakesQuery expression here. Autocomplete and auto-format apply automatically. |
| **Run** button | Execute the query. |
| **Auto-format** toggle | On by default. Reformats the query on run: each pipe on its own line, whitespace tidied, comments preserved. Press `Ctrl`/`Cmd`+`Shift`+`F` to format without running. |
| **Export (CSV / JSON)** | Download the current result set - enabled after a query returns results. |
| **Schedule Search** | Pre-fill the Create Search form with the current query. |
| **Expand Macros** button | Expand backtick macro calls in the query box with inline annotation comments. |
| **Depth** input | Controls how many nesting levels the Expand Macros button resolves (0 = all). |
| **File browser** (sidebar) | Browse indexes on disk. Click a file or folder to insert its `index="…"` path into the query. |
| **Sample queries card** | Shown under the query box on new installs. Five one-click starter queries against the bundled sample dataset (`indexes/sample/app_logs/` - 30 days of app logs, present on every install). Click a chip to load the query, then Run. Dismissible; regenerate the dataset with `python -m tools.generate_sample_data`. |

### How to use it

1. Type a query - for example `index="firewall" | stats count by src_ip`.
2. Press **Enter** (or click **Run**) to execute.
3. Results appear in a paginated table (100 rows per page).
4. Use the sidebar file browser to discover available indexes. The search box at the top filters the tree.

### Autocomplete

As you type, a dropdown surfaces matching commands and functions. The vocabulary is derived from `lexers/speakesQuery.g4` at server start and served by `GET /api/grammar/vocab`, so it's always in sync with what the parser actually accepts.

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move through suggestions |
| `Tab` or `Enter` | Accept the highlighted suggestion |
| `Esc` | Dismiss the dropdown |

The dropdown favours commands right after a pipe (`| …`) and functions inside `eval`/`where`/`stats` bodies.

### Auto-format and comments

- **Auto-format** is on by default. Each run reflows the query so every pipe directive lands on its own line, surplus whitespace is collapsed, and quoted strings / macros / regexes are preserved verbatim.
- Turn off the checkbox next to **Run Query** to leave your exact text untouched.
- Press `Ctrl`/`Cmd`+`Shift`+`F` to format without running.
- Full-line `#` comments are supported - the engine strips them before parsing, so you can disable a pipe segment by prefixing the line with `#` while iterating.
- Leading and trailing whitespace on the whole query is also trimmed, so pasted snippets run as expected.

### Expanding macros

If your query contains backtick macro calls (e.g. `` `exclude_noise` ``), you can preview the expanded form before running:

1. Set the **Depth** input next to the button - `0` expands all levels, `1` expands only top-level macros, `2` expands two levels, etc.
2. Click **Expand Macros**.
3. The query box is updated with the expanded text. Each expansion is wrapped in triple-backtick annotation comments showing which macro was expanded and where the expansion ends.
4. Review the expanded query, then click **Run Query** as usual - annotation comments are automatically stripped before execution.

This is especially useful for debugging complex queries with nested macros, or for auditing what a macro actually produces.

### Key things to remember

- **Enter runs the query**; use **Shift+Enter** to insert a newline.
- The file browser reflects the path set in **Settings → Browse Path**.
- Multi-value cells display each value on its own line inside the cell.
- Your last query is saved automatically and restored when you reopen the app.
- Annotation comments from macro expansion are stripped automatically when you run a query - you never need to remove them by hand.

---

## Lookups

Lookups lets you upload, preview, download, and delete reference files that your queries can join against with the `lookup` command.

### Supported formats

CSV, JSON, TSV, and Parquet.

### How to use it

1. Click **Upload** and select a file (`.csv`, `.json`, `.tsv`, or `.parquet`).
2. The file appears in the table with its name, type, size, and timestamps.
3. Click **Preview** to see the first 200 rows without downloading.
4. Click **Download** to save a copy locally.
5. Click **Delete** to permanently remove the file (confirmation required).

### Key things to remember

- Lookup files are stored in the application's `lookups/` directory.
- Reference a lookup in a query with `| lookup myfile.csv field_name` (see the Commands documentation for full syntax).
- Large lookups are fine to upload - preview caps at 200 rows, but the query engine reads the full file.

---

## Import

The Import tab lets you bring CSV, Parquet, or SQLite files directly into SpeakesQuery as queryable indexes - no ingestion script required. This is ideal for one-off data loads, database exports, log file archives, or any static dataset you want to explore with SPQL.

### Supported formats

| Format | Extensions | Notes |
|--------|-----------|-------|
| CSV | `.csv` | Parsed with pandas. |
| Parquet | `.parquet` | Validated for correct Parquet magic bytes and schema. |
| SQLite | `.sqlite`, `.sqlite3`, `.db` | Each table becomes a separate Parquet file in the target index. |

### Form fields

| Field | Required | Description |
|-------|----------|-------------|
| **File** | Yes | The file to import (up to 200 MB). |
| **Index Name** | Yes | Target subdirectory under `indexes/`. Supports nesting with `/` (e.g. `firewall/2024`). |
| **Date Field** | No | Column to convert to `_epoch` for time-based queries. If blank and no `_epoch` column exists, all rows are stamped with the current time. |
| **SQLite Table** | No | For SQLite files only - pick a specific table or import all tables. |

### How to use it

1. Click **Choose File** and select a CSV, Parquet, or SQLite file.
2. Enter an **Index Name** - this is the path you will use in queries (e.g. `index="firewall/2024"`).
3. (Optional) Enter a **Date Field** if your data has a timestamp column you want to use for time-range queries (`earliest=` / `latest=`).
4. For SQLite files, a table selector appears automatically. Choose a specific table or leave it on "All Tables."
5. Click **Import**.
6. On success, navigate to the **Query** tab and query your data: `index="your_index_name" | head 10`.

### Key things to remember

- **Every imported file gets an `_epoch` column.** This is required for time-based queries. If your file already has one, it is used as-is.
- **SQLite imports produce multiple files** - one Parquet file per table, all in the same index directory.
- **This is for static data.** For recurring data sources that need to pull fresh data on a schedule, use **Create Ingestion** instead.
- The maximum file size is **200 MB**.
- The imported data appears immediately in the Query tab's file browser after import.

---

## Create Search

Create Search defines a scheduled (saved) search - a query that runs automatically on a cron schedule and optionally emails results.

### Form fields

| Field | Required | Description |
|-------|----------|-------------|
| **Name** | Yes | Unique identifier for this search (e.g. `high_error_rate`). Cannot be changed after creation. |
| **Query** | Yes | The SpeakesQuery expression to run. Must include an `index=` source. |
| **Cron Schedule** | Yes | Standard 5-field cron expression (minute, hour, day-of-month, month, day-of-week). Example: `30 * * * *` = every hour at :30. |
| **Lookback** | Yes | How far back to scan. Use a relative time string like `-4h`, `-1d`, or `-30m`. |
| **Trigger** | Yes | `once` - alert fires once if any results match. `per result` - alert fires once per matching row. |
| **Email** | Yes | Notification recipient. |
| **Description** | No | Free-text notes about this search. |

### How to use it

1. Fill out all required fields.
2. Click **Save**. The server validates the cron expression, lookback, and trigger.
3. If a search with the same name already exists, you will be prompted to overwrite it.
4. On success the form clears and you are taken to the Searches tab.

### Key things to remember

- Use the **Import from Query** button to copy whatever is currently in the Query tab's textarea - handy when you've just prototyped a query.
- The **Schedule Search** button on the Query tab does the same thing in reverse: it pre-fills this form.
- Cron validation happens server-side; an invalid expression produces an immediate error.
- Lookback must start with `-` and end with a unit (`s`, `m`, `h`, `d`).

---

## Searches

The Searches tab lists all saved / scheduled searches and lets you manage them.

### Table columns

| Column | Meaning |
|--------|---------|
| **Name** | The search identifier. |
| **Cron Schedule** | When the search runs. |
| **Lookback** | Time window each run covers. |
| **Email** | Alert recipient. |
| **Trigger** | `once` or `per result`. |
| **Next Run** | Calculated from the cron expression. Shows red "N/A" if the cron is unparseable. |

### Actions

- **Edit** - loads the search into the Create Search form for modification.
- **View** - opens a modal with the raw YAML configuration.
- **Delete** - soft-deletes the search (recoverable for 30 days; the YAML is archived with a `.deleted` suffix).

### Cross-link badges (Wave 4, 2026-04-25)

Each saved-search row now carries small chips below the name showing the topology graph for that search:

| Chip | Meaning | Click action |
|---|---|---|
| 📂 *subdir* | An index path the SPQL query reads | (informational, no nav) |
| ⚙ *task #N* | An ingestion task whose output feeds the index | Switch to **Ingestion Scripts** + scroll-highlight that row |
| 🚨 *ag_name* | An alert group that includes this search as a feeder | Switch to **Alert Groups** + scroll-highlight that row |

The same chip set appears (mirrored) on Ingestion Scripts and Alert Groups rows, so you can navigate the index ↔ script ↔ search ↔ AG graph in any direction with a single click. Backed by `GET /api/topology` (one fetch per page-load, cached client-side). See [10_api_reference.md § Topology](10_api_reference.md#topology-apitopology--wave-4-2026-04-25).

### Key things to remember

- Deleted searches can be recovered within 30 days - the confirmation dialog tells you this.
- Editing a search does **not** change its name; the name field is locked during edits.
- Click **Refresh** to pick up changes made outside the UI.

---

## Create Ingestion

This is where you write, test, and save Python ingestion scripts that pull data from external APIs or sources into SpeakesQuery indexes.

### Form fields

| Field | Required | Description |
|-------|----------|-------------|
| **Title** | Yes | Unique name for this script (e.g. `github_issues_ingest`). Locked during edits. |
| **Description** | No | What this script does. |
| **Python Code** | Yes | The ingestion script. Must call `GENERATE_RESULTS(df)` with a Pandas DataFrame. |
| **Cron Schedule** | Yes | 5-field cron expression controlling how often the script runs. |
| **Subdirectory** | No | Output path under `indexes/`. Supports nesting with `/` (e.g. `github/issues`). Defaults to the title if blank. |
| **Overwrite Mode** | Yes | `Append` (default) - each run creates a new file; compaction merges small files over time. `Overwrite` - each run atomically replaces the previous file; all prior data is deleted. Use overwrite only for dashboards or point-in-time snapshots where history is not needed. |
| **API URL** | No | Reference URL for the source API (informational only). |
| **Trust Level** | Yes | `Sandboxed` (default) - RestrictedPython; only allowlisted imports. `Unrestricted` - plain `exec`; full `sys.path`. Required for any script using `scipy`, `sklearn`, `rapidfuzz`, tuple-unpack in loops, or underscore-prefixed names. Script Library's Deploy button sets this automatically based on the library script's declared tier; see [Ingestion Etiquette: Trust Tiers](09_ingestion_etiquette.md#trust-tiers-sandboxed-vs-unrestricted). |

### Sidebar - API Credentials

The credentials panel is **always available**, even before a script is saved. This lets you store API keys that your script needs *before* testing it.

| Field | Description |
|-------|-------------|
| **Key Name** | The credential name your script references (e.g. `api_key`). Accessed in code via `CREDENTIALS["api_key"]`. |
| **Value** | The secret value. Encrypted at rest with Fernet; never displayed after saving. |

**Validation rules for credentials:**

- ASCII characters only - no Unicode.
- No spaces or whitespace of any kind.
- No percent-encoded sequences (e.g. `%20`).
- No shell metacharacters (`` ` $ \ ; | & < > ( ) { } ! ``).

### Code editor

The Python code field uses a full-featured code editor with:

- **Syntax highlighting** - Python keywords, strings, numbers, comments, and operators are color-coded.
- **Line numbers** - displayed in a gutter on the left.
- **Auto-indent** - pressing Enter after a colon (`:`) automatically indents the next line.
- **Bracket matching** - matching parentheses, brackets, and braces are highlighted.
- **Auto-close brackets** - typing `(`, `[`, `{`, or `"` automatically inserts the closing counterpart.
- **Autocomplete** - press `Ctrl`+`Space` (or just start typing) to see suggestions for sandbox modules, common pandas/requests/bs4 methods, builtins, and the SpeakesQuery API (`GENERATE_RESULTS`, `CREDENTIALS`, `get_cached_or_fetch`).
- **Toggle comments** - `Ctrl`+`/` (or `Cmd`+`/` on Mac) toggles line comments.
- **Live syntax checking** - as you type, a server-side linter checks for Python syntax errors and marks the offending line with a red dot in the gutter. Hover over the dot to see the error message.

### How to use it

1. **Add credentials first** (if needed) - enter the key name and value in the sidebar and click **Store Credential**. These are saved in a staging area until the script is saved.
2. **Write your code.** Your script receives:
   - `CREDENTIALS` - a dict of your stored secrets.
   - `GENERATE_RESULTS(df)` - call this with a Pandas DataFrame to produce output.
   - Standard libraries: `pandas`, `requests`, `json`, `datetime`, `time`, `re`, `math`, `hashlib`, `base64`, `collections`, `io`, `bs4` (BeautifulSoup), `lxml`.
3. **Test your code** by clicking **Test Code**. The test runs in a sandbox with your stored credentials injected, subject to the same resource budgets as production runs (timeout, request count, response size). The result panel shows:
   - Pass/Fail badge
   - Row count, column count, epoch source, duration
   - Column data types
   - A preview of the first few rows
   - Clear, actionable error messages when something goes wrong (see below)
4. **Save the script** once it passes. The Save button is disabled until the test passes.

### Common test errors and what they mean

| Error | What to do |
|-------|-----------|
| **Syntax error (line N)** | Fix the Python syntax. The lint gutter also shows which line has the error. |
| **Import of 'X' is not allowed** | You tried to import a module outside the sandbox. Only the listed libraries are available. |
| **name 'X' is not defined** | Typo in a variable or function name. Check spelling and capitalization. |
| **No DataFrame passed to GENERATE_RESULTS()** | Your script must call `GENERATE_RESULTS(df)` exactly once with a pandas DataFrame. |
| **No _epoch column and no parseable timestamp** | Your DataFrame needs a timestamp column (named `_epoch`, `TIMESTAMP`, `DATE`, or `CREATED_AT`) for time-based queries. |
| **Request budget exhausted** | Your script made too many HTTP calls. Reduce the number of requests or increase the limit in Settings. |
| **Response size exceeds limit** | A single HTTP response was too large. The page you're fetching may contain embedded resources; try targeting a specific API endpoint instead. |
| **Script exceeded Ns timeout** | The script ran too long. Check for infinite loops or slow API calls. Increase the timeout in Settings if needed. |
| **Connection failed** | The URL is unreachable or the domain is not in Settings > Allowed API Domains. |
| **HTTP error (401/403)** | Authentication failed. Check credentials in the sidebar. |
| **DataFrame is empty (0 rows)** | The API returned no data or your parsing logic filtered everything out. |

### Subdirectory validation

As you type a subdirectory, real-time validation checks:

- **Red** - invalid path (contains `..`, starts with `/` or `~`).
- **Amber** - path exists and already has data (will append or overwrite depending on mode).
- **Blue** - path exists but is empty.
- **Green** - new path that will be created on first run.

### Key things to remember

- **Credentials can be added before saving or testing.** You no longer need to save the script first. For new scripts, credentials are stored in a staging area and automatically attached when you save.
- The test must pass before saving - this is intentional. It prevents broken scripts from being scheduled.
- **Tab/Shift-Tab** indent and dedent selected lines. The code editor behaves like a real IDE.
- Any change to the code resets the test - you'll need to re-test before saving again.
- **Overwrite mode** permanently deletes previous data on every run. A warning appears when you select it. Use Append (the default) unless you specifically want a rotating snapshot.

---

## Ingestion Scripts

This tab lists all saved ingestion scripts with their run status and controls.

### Search

A search bar at the top filters the list by title, subdirectory, description, and API URL. The matcher normalizes underscores, hyphens, and whitespace into a single space - so you can paste an identifier like `coingecko_volume_anomaly_detector_pro` directly from a Feeder Health row and find the matching task without retyping it. Multi-token searches are ANDed (every token must match).

### Table columns

| Column | Meaning |
|--------|---------|
| **Enabled** | Toggle switch - enables or disables the scheduled job. |
| **Title** | Script name. |
| **Cron** | When the script runs. |
| **Subdirectory** | Where output is written under `indexes/`. |
| **Mode** | Overwrite or Append. |
| **Created** | When the script was first saved. |
| **Next Run** | Calculated from cron; blank if disabled. |

### Actions

- **Edit** - loads the script into the Create Ingestion form.
- **Test** - runs the script immediately (one-off) and shows a pass/fail notification.
- **History** - opens a modal with the last 50 execution records (status, start time, runtime, attempt number, error details).
- **Delete** - permanently removes the script and its schedule (confirmation required).

### Key things to remember

- Disabling a script stops its cron schedule but does **not** delete its data or credentials.
- The History modal is the first place to look when diagnosing failed ingestions.
- **New Script** button clears the Create Ingestion form and navigates to it.

---

## Script Library

The Script Library is a curated collection of ready-to-use ingestion scripts for common data sources (GitHub, APIs, etc.).

### Card display

Each library script shows:

- **Category** - the type of integration (e.g. "GitHub", "Monitoring"). Click to add it as a filter.
- **Title** and **Description**.
- **Tags** - includes "API Key Required" if the script needs credentials, "Pro Tier" if it's an unrestricted-tier `_pro` variant, plus any author-supplied tags. Every tag is clickable.

### Filtering and search

A toolbar above the grid lets you narrow the catalog:

- **Search box** - case-insensitive word search across title, description, category, and tags. Space-separated tokens are ANDed together (all tokens must match).
- **Filter chips** - one per distinct tag/category, each showing its script count. Click a chip (or press Enter/Space when focused) to toggle it. Multiple active chips are ANDed: a script must carry *every* active tag to remain visible. Click a chip on a card to add it to the active filters.
- **Clear filters** - appears whenever any filter or search term is active; resets to the full catalog.
- The script count in the toolbar reflects the current filtered / total view (e.g. `12 of 92 scripts`).

### Actions

- **Preview** - opens a modal with full details: category, suggested cron, subdirectory, mode, required credentials, and the complete Python code.
- **Deploy** - copies the script into the Create Ingestion form so you can customize and save it.

### How to deploy a library script

1. Click **Deploy** on the card (or in the preview modal).
2. The Create Ingestion form is populated with the library script's defaults.
3. If the script requires credentials, a notification tells you which keys to add. **Add them in the sidebar before testing.**
4. Test and save as usual.

### Key things to remember

- Deploying does **not** auto-save - you still need to test and save.
- You can edit any field after deploying (title, cron, code, etc.).
- Credentials listed under "API Key Required" must match exactly - the code references them by name (e.g. `CREDENTIALS["github_token"]`).

---

## Docs

The Docs tab contains the full SpeakesQuery language reference - fundamentals, commands, functions, advanced topics, and a cookbook of recipes.

### How to use it

1. Click a topic in the sidebar to load it.
2. Use the **search box** to filter across all documentation. Search matches by title and content; matching terms are highlighted.
3. Internal cross-references (e.g. "see Commands") are clickable links that navigate within the docs tab.

### Key things to remember

- All documentation is cached after the first visit - subsequent loads are instant.
- Search works across all docs simultaneously, even if you haven't opened them yet.

---

## Settings

Global application settings that control storage, maintenance, ingestion, and security behavior.

### Setting groups

#### Storage

| Setting | Description | Default |
|---------|-------------|---------|
| **Indexes Root** | Filesystem path where ingested data is stored. | `<app>/indexes` |
| **Browse Path** | Root path shown in the Query tab's file browser. | `/app/indexes` |
| **Max Total Size (GB)** | Storage cap across all indexes. | Varies |
| **Max Subdirectory Size (GB)** | Per-index size cap. | Varies |
| **Target Parquet File (MB)** | Parquet file size target for compaction (16–1024 MB). | 128 |

#### Maintenance

| Setting | Description | Default |
|---------|-------------|---------|
| **Cleanup Interval (hours)** | How often the compaction + cleanup job runs (1–168 hours). | 6 |

#### Subdirectory

| Setting | Description | Default |
|---------|-------------|---------|
| **Max Nesting Depth** | Maximum folder depth under indexes (5–20). | Varies |

#### Ingestion

| Setting | Description | Default |
|---------|-------------|---------|
| **Script Timeout (seconds)** | Max wall-clock time per script execution (10–600). Enforced on both Python and subprocess scripts. | 120 |
| **Max Retries** | How many times a failed script is retried (0–10). | 3 |
| **HTTP Request Timeout (seconds)** | Timeout for outbound HTTP calls in scripts (5–300). | 30 |
| **Max Output Rows** | Per-execution row cap (1K–10M). Excess rows are silently truncated before the parquet write. | 500,000 |
| **Max Requests / Execution** | Maximum HTTP requests a single script run can make (1–500). Applies to both `requests.*` calls and `get_cached_or_fetch()`. | 50 |
| **Max Response Size (MB)** | Maximum size of any single HTTP response body (1–100 MB). Prevents scripts from downloading unexpectedly large payloads. | 10 |

#### Email (SMTP)

| Setting | Description | Default |
|---------|-------------|---------|
| **SMTP Server** | Hostname of the outbound mail relay. | `smtp.gmail.com` |
| **Port** | SMTP port (587 for STARTTLS, 465 for implicit SSL). | `587` |
| **Username** | Your email address (e.g. `you@gmail.com`). | *(empty)* |
| **Password / App Password** | Password or App Password for authentication. | *(empty)* |
| **From Address** | Sender address (defaults to username if blank). | *(empty)* |
| **Use STARTTLS** | Enable STARTTLS encryption (required for port 587). | Checked |

Click **Send Test Email** to verify your configuration works - the button automatically saves your SMTP settings before sending, so there is no need to click Save first. For full setup instructions - including how to create a Gmail App Password - see the [Email Setup Guide](07_email_setup.md).

#### Security

| Setting | Description | Default |
|---------|-------------|---------|
| **Credential Key Directory** | Filesystem path for the Fernet master key. | `~/.speakes-query` |
| **Allowed API Domains** | Newline-separated regex patterns. Only matching domains can be contacted by ingestion scripts. | Varies |

### How to use it

1. Adjust values as needed.
2. Click **Save Settings**. The server validates and normalizes all values.
3. If any setting is invalid, a partial-success response shows which fields had errors.
4. **Reset to Defaults** restores factory settings (confirmation required - cannot be undone).

### Key things to remember

- Changing **Browse Path** immediately refreshes the Query tab's file browser.
- **Allowed API Domains** uses regex - `.*` matches everything, `github\\.com` matches only GitHub.
- The Fernet master key (in Credential Key Directory) should have `0600` permissions. The app warns if permissions are too open.
- Settings are persisted server-side, not in the browser. They survive browser cache clears.

---

## Macros

The Macros tab lets you create, edit, test, and delete reusable query fragments. Macros are pure text-substitution - they replace backtick-delimited calls with stored definition text before the query is parsed.

> For the full language guide covering syntax, nesting, standardisation recipes, and best practices, see the **[Macros - Practical Guide](08_macros.md)**.

### Layout

The Macros tab has three sections:

| Section | Purpose |
|---------|---------|
| **Macro List** | Table of all saved macros with name, description, parameter count, and action buttons. |
| **Create / Edit Form** | Form for defining or modifying a macro. |
| **Test Panel** | Query box for testing macro expansion and execution against live data. |

### How to create a macro

1. Click **+ New Macro** (or navigate to the Macros tab).
2. Fill in the form:

| Field | Required | Description |
|-------|----------|-------------|
| **Name** | Yes | Unique identifier - alphanumeric and underscores only, no spaces. Use `snake_case`. |
| **Definition** | Yes | The SpeakesQuery fragment this macro expands to. Can contain any valid syntax: filters, pipeline segments, eval expressions, and even calls to other macros. |
| **Parameters** | No | Comma-separated list of parameter names. Each parameter is referenced in the definition as `$param_name$`. |
| **Description** | No | Free-text notes - what the macro does, what each parameter means, example usage. |

3. **Auto-extracted parameters:** As you type the definition, the form automatically detects `$param$` tokens and populates the Parameters field. You can also edit the parameters manually.
4. Click **Save Macro**.

### Example: creating a threshold filter macro

1. **Name:** `threshold_filter`
2. **Definition:** `search $field$ > $limit$`
3. **Parameters:** `field, limit` (auto-detected from the definition)
4. **Description:** `Filter rows where a given field exceeds a numeric threshold. Usage: \`threshold_filter(response_time, 500)\``
5. Click **Save Macro**.

Now use it in a query:
```spl
index="web/access.parquet" | `threshold_filter(response_time, 500)` | stats count by endpoint
```

### How to edit a macro

1. Find the macro in the list and click **Edit**.
2. The form is populated with the macro's current values. The name field is locked - macro names cannot be changed after creation.
3. Modify the definition, parameters, or description.
4. Click **Save Macro**.

### How to test a macro

The Test Panel at the bottom of the Macros tab lets you verify a macro works before using it in production queries.

1. **Expand Only** - enter a query containing your macro call in the test query box and click **Expand Only**. The panel shows the expanded text without executing it. Use this to verify parameter substitution and nesting are correct.
2. **Expand & Run** - click **Expand & Run** to jump straight to the **Query** page with the expanded query pre-populated. The macro call that was expanded is included as a `#` comment header at the top (so you can see what was expanded from what), and the query runs immediately. From there you have all the normal Query-page tools (saving to a job, exporting, field filtering, etc.).

### How to delete a macro

1. Find the macro in the list and click **Delete**.
2. Confirm the deletion. **This is permanent** - macros are hard-deleted, not soft-deleted.

> **Warning:** Before deleting a macro, check whether any saved searches or other macros reference it. Deleting a macro that is still in use will cause those queries to fail.

### Key things to remember

- Macro names are permanent - choose carefully.
- Definitions are pure text substitution, not evaluated code. The `$param$` placeholders are replaced literally with the supplied arguments.
- Parameter count must match between the definition's `$param$` placeholders and the call site's arguments - a mismatch produces a clear error.
- Nested macros are expanded automatically. Circular references are detected and rejected.
- Use the **Expand Macros** button on the Query page to preview expansion with depth control before running.

---

## Analyzer Prompts

The Analyzer Prompts tab lets you create, edit, and delete prompt templates for the [Claude Analyzer](11_claude_analyzer.md). These prompts tell Claude what to look for when analyzing scheduled search results.

> For the full guide covering token placeholders, cost controls, filter gates, and end-to-end examples, see the **[Claude Analyzer guide](11_claude_analyzer.md)**.

### Layout

| Section | Purpose |
|---------|---------|
| **Prompt List** | Table of all saved prompts with name, description, and action buttons. |
| **Create / Edit Form** | Form for defining or modifying a prompt. |

### How to create an analyzer prompt

1. Navigate to the **Analyzer Prompts** tab.
2. Fill in the form:

| Field | Required | Description |
|-------|----------|-------------|
| **Name** | Yes | Unique identifier - letters, digits, spaces, hyphens, underscores, periods. |
| **Description** | No | Free-text notes - what the prompt analyses, which searches use it. |
| **Prompt Text** | Yes | The prompt template sent to Claude. Use `$token$` placeholders for dynamic values (e.g., `$scheduled_search_name$`, `$question$`, `$result_count$`). |

3. Click **Save Prompt**.

### How to assign a prompt to a saved search

1. Go to **Create Search** (or edit an existing saved search).
2. In the **Analyzer Prompt** dropdown, select the prompt you created.
3. Optionally enable the **Filter Gate** and enter a yes/no question.
4. Click **Save**.

When the saved search fires, the analyzer runs automatically if enabled in Settings.

### Key things to remember

- Prompts are optional. A saved search with no `analyzer_prompt` set runs exactly as before.
- `$token$` placeholders map to either global tokens (search metadata) or column names from query results.
- Column tokens are aggregated across all rows and truncated when there are many distinct values.
- The full result CSV is always sent alongside the resolved prompt - Claude gets both the overview and the raw data.
- Use the **Validate Tokens** endpoint to check that your tokens resolve against a query's output before deploying.

---

## Alert Groups & Feeder Health

The **Alert Groups** tab lists every configured alert group (multi-search Claude dispatch), its last run outcome, and a per-AG **Feeder Health** pill that summarises the state of every saved search the AG depends on. Click the pill (or the `Feeder Health` button) to open the modal.

### Feeder Health modal

The modal shows one row per feeder. Each row carries a state pill, the feeder's library-script id (if matched), its index subdirectory, and a contextual action button. Possible states (best → worst):

| State | Meaning | Action |
|-------|---------|--------|
| `live` | Data landed recently, query runs cleanly | (none) |
| `pending` | Scheduled but no data yet | wait |
| `disabled` | Ingestion task manually disabled | Re-enable in Ingestion Scripts |
| `needs_creds` | Task is live but its credential(s) aren't in the vault | Jump to Ingestion Scripts → task #N |
| `needs_deploy` | Library script matched but no scheduled task yet | Click **Deploy** |
| `no_library_script` | Index path is user-managed (no matching shipped script) | Informational - may still work |
| `missing_search` | AG references a saved search that doesn't exist | Click **Install default** (if template available) |
| `unknown_index` | Query has no `index=` path | Edit the saved search |

**Dead-feeder detection** (2026-04-20+). Even a `live` feeder gets a red ⚠️ warning if its most recent saved-search execution is older than `alert_group_max_feeder_staleness_hours` (default 48h). The underlying Parquet files may be ancient even if the ingestion schedule is nominally enabled.

**Template-drift detection** (2026-04-21+). Feeders whose installed `saved_searches/<name>.yaml` differs from the git-tracked `default_saved_searches/<name>.yaml` template (on the `query` field) get a yellow **Sync Template** button. Click it to overwrite the installed YAML with the current template. A confirmation dialog warns that any manual edits to that feeder's YAML will be lost. Use this after an operator rebuild when the Docker volume was seeded before a bug-fix commit.

**Fix Missing Feeders chained run** (Wave 2, 2026-04-25). The button now does Install → Deploy → **Run** in one flow: every newly-deployed task (and every existing task in `pending` state with no parquet yet) is run-now'd in parallel (max 4 concurrent) before the modal returns. Per-feeder rows render the ingestion outcome inline (`Ingestion ran: N row(s) in X.XXs` or `Ingestion failed: …`), and Pipeline Check auto-refreshes so the operator sees the post-run SPQL row counts without an extra click. The whole flow takes 1-2 minutes for a typical 10-feeder AG; the message bar sets that expectation up front so the modal doesn't look hung. Pass `?run_after_deploy=false` to the API for the historical deploy-only behaviour.

**Pipeline Check zero-row classification** (Wave 2, 2026-04-25). Feeders that returned 0 rows now carry a colored tag distinguishing the two distinct failure modes:

- **Likely sparse** (yellow): parquet has rows but the saved-search query filtered everything out - common on quiet days. Action: **Go to ingestion task →** (the filter is the suspect, not the ingestion).
- **Likely broken** (red): no parquet under the feeder's subdirectory yet - ingestion never produced output. Actions: **Run ingestion now** (synchronous; auto-re-runs Pipeline Check on success) **+ Go to ingestion task →**.

**Go to ingestion task →** closes the Feeder Health modal, switches to the Ingestion Scripts tab, and scroll-highlights the matching task row in yellow for ~2.5s.

Programmatic equivalents:

```bash
# Install a missing default feeder
curl -X POST http://localhost:5111/api/alert-groups/<ag>/install-default-feeder/<search>

# Force-sync an existing (drifted) feeder with the current template
curl -X POST "http://localhost:5111/api/alert-groups/<ag>/install-default-feeder/<search>?overwrite=true"

# Bulk Install + Deploy + Run (default - Wave 2)
curl -X POST http://localhost:5111/api/alert-groups/<ag>/deploy-feeders

# Deploy without the chained run (historical behaviour)
curl -X POST "http://localhost:5111/api/alert-groups/<ag>/deploy-feeders?run_after_deploy=false"

# Run a single ingestion task immediately
curl -X POST http://localhost:5111/api/si/<task_id>/run
```

### Upload Brief - manual return loop (Wave 3, 2026-04-25)

The **Upload Brief** button on each AG row opens a modal for pasting a brief returned from an **external** LLM (Claude.ai / ChatGPT / Gemini / etc.). The typical use case: the AG runs in `delivery_mode: prompt_only` (zero Claude API cost - the prompt is emailed to you), you paste it into your LLM of choice, and you want the resulting picks captured into `ag_picks` so historical-performance queries see manual + Claude-pipeline picks through the same surface.

The modal:

1. **Pick the model** that generated the brief (dropdown of common models + "other" for custom). Stored as `model_used` so cross-model performance comparisons stay possible.
2. **Optional: link to a past dispatch** - paste the original Claude `request_id` (from the prompt-only email's banner). Leave blank to auto-generate `manual:<group>:<UTC>`.
3. **Paste the full LLM response** including the trailing fenced ```` ```json [ ... ] ``` ```` block.
4. **Preview parsed picks** - runs the same parser the live dispatcher uses, in `dry_run=true` mode (zero writes). Lets you verify what would land before committing.
5. **Commit to ag_picks** - writes one row per pick with `source="manual"` + your chosen `model_used`.

Dedup: identical pastes (SHA-256 of `alert_group + raw_text`) within 7 days return HTTP 409 with the prior `run_request_id`. Deliberately back-fill-friendly - paste a brief days late for a missed dispatch and it still captures.

Once committed, manual returns are queryable through the same SPQL surface as Claude-pipeline picks (`source` + `model_used` are now first-class filter columns). See [12_alert_groups.md § Pick Capture](12_alert_groups.md#wave-3-2026-04-25-manual-return-loop-for-prompt-only-deliveries) for queries.

### Manual Run

The **Run** button on each AG card triggers a dispatch immediately. The row shows a progress message ("Running alert group…") and the dispatch runs synchronously:

1. Every feeder's query runs on-demand against live indexes.
2. Results serialized + token-estimated + budget-gated.
3. Claude API called with the configured prompt + `web_search` tool.
4. Response rendered as branded HTML email (if `email_address` is set) + included in the run result.

A full analyst brief typically takes 1–8 minutes. **Tail `docker logs -f <container>` during a long run** - the dispatcher emits `[i]` log lines at every phase boundary so you can see exactly where time is being spent. See [12_alert_groups.md § Troubleshooting](12_alert_groups.md#troubleshooting) for the expected log cadence.

When rate-limited or circuit-breaker-tripped, the result modal offers a **Force Run** prompt that bypasses the per-AG rate limit + breaker for a single dispatch. Budget + freshness checks still apply.

---

## Schedule

The Schedule tab gives you four ways to look at the cron-driven activity in your install:

1. **Summary cards** - total job counts split by ingestion / saved-search / alert-group, plus the busiest UTC hour and the biggest-data UTC hour.
2. **Firing-count heatmap** - day-of-week × hour grid colored by how many jobs are scheduled to fire in each cell. Spot overloaded slots visually before scheduling a new job.
3. **Expected-data-volume heatmap** - same grid, colored by `firings × avg row count` from recent history. Shows where the heavy-data hours land.
4. **Recent Activity charts** (Wave 6, 2026-04-26) - bar chart of executions per UTC day stacked by kind (ingestion / saved-search / AG) plus a line chart of rows ingested per day. Default 14-day window, configurable via the per-chart `Window` selector (7 / 14 / 30 / 60 / 90 days). Backed by `GET /api/schedule/volume?days=N`. Inline SVG - no runtime chart-library dependency. The bar chart shows *what's running*; the line chart shows *what's landing*.
5. **All Scheduled Jobs table** - sortable list of every job with kind, name, cron, next firing, recent average runtime, and recent average row count.

The **Lookahead**, **History runs**, and **Include disabled** controls re-render the heatmaps + summary; the **Window** control on the Recent Activity box re-renders only the volume charts. **Refresh** re-runs everything.

### Schedule Operations Report (PDF)

The **Download PDF** button (also `python -m tools.schedule_pdf`) produces
a standalone report: executive summary, heatmaps, recent activity,
per-alert-group feeder health, anomalies, and a full job appendix.

Per-AG feeder health pills (2026-07-01 vocabulary):

| Pill | Meaning | Action |
|------|---------|--------|
| `OK` | Last runs averaged > 0 rows | None |
| `EMPTY` | Ran cleanly, averaged 0 rows | Check whether the source is legitimately dry or the filter is too tight |
| `FAILING` | Every recent run logged `status=error` | Read `error_message` in `indexes/logs/search_runs` |
| `NEVER RAN` | Cron-scheduled but no history in the lookback window | Check scheduler / deploy state |
| `ON-DEMAND` | Installed dispatch-time feeder (empty cron; the AG dispatcher runs it at dispatch) that hasn't fired yet | None - it will populate after the AG's first dispatch |
| `PLACEHOLDER` | Manual-return slot (`*_reserved_picks`) | None - intentional |
| `MISSING` | The saved search is not installed at all | Install it from Feeder Health |

Dispatch-time feeders (`purpose: alert_group_feeder`, empty cron - e.g.
`github_hot_repos_today`, `ai_papers_new_today`) are resolved against the
saved-search store and their dispatcher-logged run history, so they are
never falsely reported MISSING just because they aren't cron-scheduled.

The **Highlights & Anomalies** section includes a **Failing runs** card:
any enabled job with `status=error` rows among its recent runs, ranked by
error count. A job failing on every run produces no rows and no data -
before 2026-07-01 it rendered as " - " and escaped every anomaly bucket.

---

## Notebooks

The Notebooks tab (Develop group) is a cell-stream analysis surface with a reactive cache - iterating on a query or prompt is free until you change something upstream.

### Layout

- **List view** - every saved notebook with a **+ New Notebook** button, **Refresh**, a live `cache: …` stats readout, and a **Clear Cache** button (drops every cached cell output; cannot be undone). First-run installs ship a `getting_started` notebook - a five-minute walkthrough of every cell type - highlighted by a welcome banner until you open something else.
- **Editor view** - opens when you click a notebook. Header controls: **← Back**, a **Use cache** checkbox, **Run All**, **Save**, **+ Cell**, **Export HTML**, **Export PDF**, and **Delete**. Cells render below with per-cell run buttons.

### Cell types

`spql` (standard pipe query → DataFrame), `pipe` (SPQL with `| llm` / `| llm_batch`, with a model-picker affordance), `python` (full Python, Jupyter-style last-expression output - an admin tool, **not** RestrictedPython), `markdown`, `chart` (Vega-Lite JSON spec), `param` (YAML form spec whose value binds to the cell id), and `promote_to_alert_group` (see below). Each cell's output is bound at its cell id, so a downstream Python cell can reference an upstream SPQL cell named `news` as `news.head()`.

### Reactive cache

Every cell's content hash combines its own source with the output hashes of all prior cells - editing cell 5 invalidates cells 5+ but leaves 1–4 cached. Uncheck **Use cache** on a run to force full re-execution. The cache lives in `notebook_cache/` (default budget 1 GB, `max_notebook_cache_gb` in Settings).

### Exports

**Export HTML** produces a self-contained page (charts render via Vega-Lite; a JSON sidecar at `#notebook-data` lets AI agents ingest the export programmatically). **Export PDF** renders via WeasyPrint - a static renderer, so chart cells appear as their JSON spec text; prefer the HTML export when you want rendered charts.

### Promote to alert group (dev → prod)

A `promote_to_alert_group` cell carries AG metadata as YAML in its source. Running the notebook **always dry-runs** the cell - it renders a structured preview (`create` / `update` / `no_change` / `blocked`, changed fields, per-feeder status, validation errors) and never mutates alert-group state. The actual deploy is a separate explicit click: the **↑ Deploy to Alert Group** button on the cell's preview pane POSTs to `/api/notebooks/<id>/promote/<cell_id>`, saves the AG, and re-registers it with the live scheduler - no restart needed.

### Key things to remember

- Re-running a notebook never silently creates or overwrites an alert group - deploy is always an explicit button click.
- Python cells run **unrestricted** - notebooks are an operator/admin surface, not a sandbox.
- Clear Cache is global (all notebooks) and irreversible.

Full reference - schema, execute API, param overrides, worked examples: [Notebooks](19_notebooks.md).

---

## Visual Builder

The Visual Builder tab (Develop group) lets you compose an SPQL query by dragging stage cards onto a canvas instead of typing pipes. Generated SPQL updates live; **▶ Run** executes it through the normal query engine (`/api/query`) - the builder is a different way to *compose* SPQL, not a different way to execute it.

### Layout

Three columns:

1. **Stages palette (left)** - every SPQL command, grouped by category (Filter, Aggregate, Reshape, Multi-value, Joins/Append, Semantic, LLM, …). Drag a command onto the canvas to add a stage.
2. **Canvas (center)** - an `index="…"` input plus the ordered stage cards. Each card has a command badge, kwargs editing, a drag handle (⋮⋮) for reordering, and a remove (×) button. **▶ Run** and **Clear** sit in the toolbar.
3. **Preview (right)** - the live-generated SPQL string and the result of the most recent Run (table capped at 50 rows × 30 columns for skimming).

### Stage card editing

Cards render in **form mode** (structured widgets per kwarg, including budget-gate fields like `max_cost_usd` / `dry_run` on every `| llm*` command) when a template exists and the kwargs parse cleanly, or **raw mode** (free-text kwargs, full grammar control) otherwise. A toggle button (⚙ / ✎) switches modes; raw → form refuses rather than silently rewriting unparseable text.

### Templates, round-trip, and tour

- **Start from a template** - a disclosure listing 12 preset pipelines (top-N, time-bucketed aggregate, lookback filter, semantic search/dedup, LLM cost-cascade, ensemble, convergence loop, …). Clicking one loads the full pipeline onto the canvas.
- **Load existing SPQL into canvas (round-trip)** - paste any SPQL, click **↓ Load**, and the canvas reconstructs the index clause + one stage card per pipe segment (via `POST /api/visual-builder/parse`). The round-trip is lossless modulo whitespace.
- **Take the tour** - a 10-step guided walkthrough launched from the page header (never auto-launched).

### Key things to remember

- Running a query here is identical to running it in the Query tab - same engine, same job history.
- Raw-mode kwargs accept anything the grammar does; form mode is a convenience, not a constraint.
- Reordering stages reorders the generated SPQL immediately - check the preview pane before Run.

Full reference - parser contract, form/raw lossless guarantee, template catalog: [Visual Builder](20_visual_builder.md).

---

## Common Workflows

### "I want to ingest data from an API that requires an API key"

1. Go to **Create Ingestion**.
2. Enter a title and write your Python code using `CREDENTIALS["my_key"]`.
3. In the **sidebar**, add a credential: Key Name = `my_key`, Value = your secret.
4. Click **Test Code** - the test injects your stored credential.
5. Once the test passes, click **Save Script**.

### "I want to deploy a library script"

1. Go to **Script Library**.
2. Find the script and click **Deploy**.
3. The Create Ingestion form is populated. Add any required credentials in the sidebar.
4. **Test**, then **Save**.

### "I want to create an alert for a query"

1. Go to **Query** and run your query to confirm it returns the results you expect.
2. Click **Schedule Search** (or go to **Create Search** and click **Import from Query**).
3. Fill in the cron schedule, lookback window, trigger mode, and email address.
4. Click **Save**.

### "I want to investigate why an ingestion script failed"

1. Go to **Ingestion Scripts**.
2. Find the script and click **History**.
3. Check the error column for the most recent failed run.
4. Click **Edit** to load the script, fix the issue, re-test, and save.

### "I have a CSV or SQLite file I want to query"

1. Go to **Import**.
2. Click **Choose File** and select your file.
3. Enter an index name (e.g. `my_data`).
4. If your file has a date column, enter its name in **Date Field**.
5. Click **Import**.
6. Go to **Query** and run `index="my_data" | head 10`.

### "I want to query data I just ingested"

1. Go to **Query**.
2. Use the file browser sidebar to find your new index folder.
3. Click the folder - its path is inserted into the query box.
4. Add pipe commands and press **Enter**.

### "I want to create a reusable macro"

1. Go to **Macros** and click **+ New Macro**.
2. Enter a name (e.g. `exclude_noise`), write the definition, and optionally add a description.
3. Click **Save Macro**.
4. Use it in any query: `` `exclude_noise` ``.

### "I want to debug a query that uses macros"

1. Go to **Query** and type (or paste) your query.
2. Set the **Depth** input to `1` to expand just the first level.
3. Click **Expand Macros** to see annotation comments showing what each macro produced.
4. Increase the depth or set it to `0` to expand all levels.
5. Click **Run Query** - annotations are stripped automatically.

### "I want to add AI analysis to a saved search"

1. Get an API key from [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) (create an account if needed - the key starts with `sk-ant-`). Then go to **Settings** → **Claude Analyzer** section, paste the key and click **Save Key**, toggle **Enable Claude Analyzer** on, and click **Save Settings**.
2. Go to **Analyzer Prompts** and create a prompt with `$token$` placeholders for the data your search returns.
3. Go to **Searches**, find your saved search, and click **Edit**.
4. Set the **Analyzer Prompt** field to the prompt name you created.
5. Optionally enable the **Filter Gate** and enter a boolean yes/no question to control whether alerts are sent.
6. Click **Save**. The next time the search fires, Claude will analyse the results.

## Network Exposure and LAN Access

SpeakesQuery is a single-operator, desktop-class application. Out of the box it is reachable only from the machine it runs on, and any bind beyond loopback is token-gated:

- **Bare metal:** the Flask server binds to `127.0.0.1` by default (`HOST` in the environment overrides it).
- **Docker:** the container's internal server binds to `0.0.0.0` so Docker's port mapping works, but the HOST-side mapping binds to `127.0.0.1` by default (`BIND_ADDR` in `.env` or the environment overrides it). A default `./install.sh` run is not reachable from other devices.
- **Access-token gate (the Jupyter model):** whenever the server binds beyond loopback - which includes every Docker install - all requests must carry the access token generated at install. `install.sh` prints the ready-to-open `?token=` URL; your browser authenticates once and gets a session cookie. The token lives at `~/.speakes-query/access_token` (0600, outside the repo). Scripts use the `X-SpeakesQuery-Token` or `Authorization: Bearer` header - see [10_api_reference.md](10_api_reference.md). `GET /healthz` is the single ungated path (liveness only).

### Exposing beyond localhost

Read this before setting `BIND_ADDR=0.0.0.0` (or a LAN interface IP):

1. **Understand what you are exposing.** The app includes a credential vault UI, a settings page, and an ingestion system with an opt-in unrestricted script tier that executes arbitrary Python. Anyone who can reach the port AND presents the access token can use all of it. The token gate keeps drive-by LAN traffic out, but there is no multi-user permission model: one token, full control. Never disable the gate (`SPEAKESQUERY_AUTH=off`) on an exposed bind unless a reverse proxy in front enforces its own authentication.
2. **Never expose it to the public internet.** Not directly, not via port forwarding. If you need remote access, use a VPN (WireGuard, Tailscale) so the app itself stays on a private network.
3. **Prefer a reverse proxy with its own auth** (basic auth, forward auth) if you must share it on a trusted LAN, and terminate TLS at the proxy. Traffic between the app and browsers is plain HTTP.
4. **Set the opt-in explicitly:**

```bash
# .env (or the shell environment used by docker compose)
BIND_ADDR=0.0.0.0        # or a specific interface, e.g. 192.168.1.10
```

Then restart: `docker compose -f desktop_app/docker-compose.yml up -d`.

### Container health

The Docker image ships a `HEALTHCHECK` (an HTTP probe against the app root every 5 seconds). `docker ps` shows the container as `healthy`/`unhealthy`, `install.sh` uses it to detect readiness, and `restart: unless-stopped` in compose revives the container after a crash or host reboot. Note that stock Docker does not auto-restart a running-but-unhealthy container; if you want that behavior, pair the healthcheck with a watchdog such as `willfarrell/autoheal`.

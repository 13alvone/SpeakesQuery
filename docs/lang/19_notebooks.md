# Notebooks

> Phase 3 / Bet 4. Cell-stream notebooks with reactive content-hash
> caching, full-Python admin cells, and one-cell promotion to live
> alert groups. The dev → production gap collapses to one button.

A SpeakesQuery notebook is a `.spqnb` YAML file containing an ordered
list of cells. Each cell's output is the typed input to the next.
Editing cell `N` invalidates downstream cells but leaves cells
`< N` cached - iterating on the analysis becomes economically free
until the moment you hit Deploy.

Open the SPA's **Develop → Notebooks** tab. The shipped
`getting_started` notebook walks every cell type in eight cells.

## Why a notebook

The platform's previous workflow looked like:

1. Hand-edit a saved-search YAML.
2. Hand-edit an alert-group YAML.
3. Wait for the cron to fire to see if your prompt actually works.
4. Pay Claude tokens for every iteration.
5. Repeat.

Notebooks collapse all five steps into a single editor. Your prompt
runs immediately against live data, the output renders in-line, the
content-hash cache makes re-iteration free, and a single
`promote_to_alert_group` cell deploys the result as a recurring AG.

## Cell types

| Type | Source format | Output | Notes |
|------|---------------|--------|-------|
| `spql` | SPQL pipe expression | `pandas.DataFrame` | Routes through the standard query engine |
| `pipe` | SPQL with `\| llm` / `\| llm_batch` | DataFrame | UI surfaces a model-picker affordance |
| `python` | Full Python | Last-expression value (Jupyter style) | **Not** RestrictedPython - admin tool |
| `markdown` | Markdown | Rendered HTML | Cell-id NOT exposed in namespace |
| `chart` | Vega-Lite JSON spec | Rendered chart | Lazy-loads vega-embed from CDN |
| `param` | YAML param spec | The `default` value bound to the cell id | Bypasses cache (runtime overrides) |
| `promote_to_alert_group` | YAML AG-config | Structured deploy preview | **Slice 9 - the headliner** |

The cell-id binding rule: whatever a cell produces is bound at
`namespace[cell.id]` so subsequent Python / SPQL cells can reference
it. A cell named `news` whose query returns 100 rows lets the next
Python cell write `news.head()`.

## Reactive cache

Every cell's content hash combines its source + the output hashes of
all prior cells. Editing cell 5's prompt invalidates cells 5+ but
leaves cells 1-4 cached. Combined with Phase 2's content-hash LLM
cache, **iterating on a prompt is free until you change something**.

The cache is an LRU-evicted SQLite + on-disk pickle store at
`notebook_cache/` next to the project root. Budget defaults to 1 GB
(`max_notebook_cache_gb`); Settings → Notebook Cache controls it.

## `promote_to_alert_group` - the headliner

A `promote_to_alert_group` cell carries the AG metadata in its
`source` field as YAML. The notebook engine ALWAYS dry-runs the cell
- it never mutates AG state on its own. Actual deploy is a separate
explicit operator action via the **↑ Deploy to Alert Group** button
on the cell preview pane.

### Anatomy of a promote cell

```yaml
# AG metadata for the promote_to_alert_group cell's `source` field
name: news_triage_v2                     # required, AG name
description: Daily news triage brief.    # optional
schedule: "0 6 * * mon-fri"              # required, cron (named-day form preferred)
timezone: America/New_York               # optional, IANA zone, default UTC
email_address: ops@example.com           # required, AG recipient
admin_error_email: alerts@example.com    # optional, separate failure-alert recipient
error_email_disabled: false              # optional, default false
delivery_mode: api                       # optional, "api" | "prompt_only"
max_rows: 200                            # optional, rows-per-feeder cap
search_names:                            # required, saved_search feeders
  - news_pulled
  - news_dedup
prompt_cell: build_prompt                # required, cell_id whose source IS the AG prompt_text
disabled: false                          # optional, default false
# Optional production-hardening pass-through (per-AG cost / staleness gates):
# max_cost_usd_per_run, max_cost_usd_per_day, max_dispatches_per_day,
# min_interval_between_runs_hours, max_output_tokens,
# max_feeder_staleness_hours, fail_on_stale_feeder, email_template_override
```

### Engine behaviour (dry-run only)

When the notebook executes (Run All, per-cell ▶ Run, or via the
`/api/notebooks/<id>/execute` endpoint), the cell handler computes a
structured **preview** dict and returns it as `output_preview`. The
preview is `{kind: "promote_to_alert_group_preview"}` with these
fields:

| Field | Meaning |
|-------|---------|
| `decision` | `create` / `update` / `no_change` / `blocked` |
| `target_payload` | The AG dict that WOULD be saved |
| `current_ag` | The current AG record (if any) for diff |
| `changed_fields` | When `decision=update`: list of `{field, old, new}` |
| `feeder_status` | List of `{name, exists, cron_schedule, last_run_at, error}` |
| `validation` | `{errors: [...], warnings: [...]}` |
| `deploy_endpoint` | Convenience hint at the deploy URL |

The engine NEVER calls `AlertGroupStore.save_group` /
`update_group` from this path. Pinned by
`tests/test_notebook_slice9_promote.py::TestConfigLeakCanary` (the
**config-leak canary** - patches both methods with
`AssertionError("CONFIG LEAK")` and runs a notebook with a promote
cell; both must stay zero).

### Deploy

Click **↑ Deploy to Alert Group** on the cell preview pane. The SPA
confirms (modal), then POSTs to
`/api/notebooks/<id>/promote/<cell_id>`. The endpoint:

1. Re-validates the cell metadata.
2. Calls `AlertGroupStore.save_group` (create) or `update_group`
   (update-in-place).
3. Re-registers the AG with the live scheduler so the next cron tick
   picks it up - no server restart needed.
4. Emits an `auto_toggle_to_feeder` config-log row tying the AG back
   to its source notebook + cell so you can trace where any AG came
   from months later.
5. Returns the saved AG record + a deploy_record summary.

### Round-trip: AG → notebook

`GET /api/alert-groups/<name>/as-notebook` returns a synthetic
notebook record built from an existing AG. The notebook contains:

* a `markdown` intro describing the source AG
* one `spql` cell per saved-search feeder, query body loaded from
  the saved-search store
* a `pipe` cell carrying the AG's `prompt_text`
* a `promote_to_alert_group` cell with all metadata pre-filled

The endpoint does NOT save the notebook - the caller decides
whether to persist it via `POST /api/notebooks`. Useful for
migrating an existing hand-written AG into the notebook
iteration loop, or for quickly cloning a known-good AG.

## API reference

All notebook endpoints live under `/api/notebooks/`:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/notebooks` | List notebooks (lightweight) |
| `GET` | `/api/notebooks/<id>` | Full notebook record |
| `POST` | `/api/notebooks` | Create (or overwrite via `overwrite=true`) |
| `PUT` | `/api/notebooks/<id>` | Update an existing notebook |
| `DELETE` | `/api/notebooks/<id>` | Delete + cascade-invalidate cache |
| `POST` | `/api/notebooks/<id>/execute` | Run top-to-bottom |
| `POST` | `/api/notebooks/<id>/export/html` | Self-contained HTML export |
| `POST` | `/api/notebooks/<id>/export/pdf` | PDF via WeasyPrint |
| `GET` | `/api/notebooks/_cache/stats` | Cache statistics |
| `POST` | `/api/notebooks/_cache/clear` | Drop every cache entry |
| `GET` | `/api/notebooks/<id>/promote/<cell>/preview` | Slice 9 - dry-run preview |
| `POST` | `/api/notebooks/<id>/promote/<cell>` | Slice 9 - actual deploy |
| `GET` | `/api/alert-groups/<name>/as-notebook` | Slice 9 - round-trip the other direction |

## Schema

The `.spqnb` YAML schema (frozen v1, additive-only):

```yaml
id: my_notebook                     # filename-safe; lowercase + digits + ._-
schema_version: 1
name: "My notebook"                 # optional display name
description: |                       # optional, max 4000 chars
  ...
default_max_cost_usd: 0.50          # optional implicit budget cap (mirrors Phase 2 setting)
cells:                              # ordered list, max 200 entries
  - id: cell_1                      # ^[a-z][a-z0-9_]*$ - Python-identifier-like
    type: spql                      # closed enum (see table above)
    source: |                       # max 100 KB per cell
      ...
    metadata: {}                    # cell-type-specific config dict
    # Optional cache-tracking fields (populated by slice 3+):
    _last_executed_at: ...
    _last_input_hash: ...
    _last_output_hash: ...
    _last_runtime_ms: ...
```

Notebook-level cap: 5 MB serialised; 200 cells per notebook.

## Storage

User-edited notebooks live in `notebooks/` (gitignored, RW). Shipped
default templates live in `default_notebooks/` (tracked in git, RO
mounted in Docker) and seed missing-only into `notebooks/` on first
init. Same pattern as alert groups + saved searches.

The reactive cache lives at `notebook_cache/`. Clear it via
**Settings → Notebook Cache → Clear Cache** or `POST
/api/notebooks/_cache/clear`. Cache eviction is LRU under the
`max_notebook_cache_gb` budget.

## Worked example: building OEB itself in a notebook

The Options Edge Brief AG can be reproduced in a single notebook:

1. `intro` (markdown) - describe the brief.
2. `feeder_putcalls` (spql) - load the put/call ratios from the
   shipped feeder.
3. `feeder_skew` (spql) - load the IV skew snapshot.
4. `consolidate` (python) - merge the two DataFrames.
5. `prompt` (pipe) - `consolidate | llm model="claude-sonnet" prompt="..."`
   to draft the brief.
6. `chart` (chart) - visualise the put/call distribution.
7. `deploy` (promote_to_alert_group) - promote with
   `name: oeb_v3`, `schedule: "30 10,15 * * mon-fri"`,
   `timezone: America/New_York`.

Iterate on `prompt` for free (cells 1-4 are cached). When the
output looks right, click **Deploy** - the AG is live before the
next cron tick.

## Limitations + design choices

* **No reactive re-execution on cell edit.** Editing a cell does
  NOT auto-trigger downstream re-runs. The Reactive cache is
  invalidation-only; the operator clicks Run All / per-cell ▶ Run
  to re-execute. This was a deliberate slice-3 choice - automatic
  re-execution would multiply LLM spend per keystroke.
* **`python` cells are full Python, not RestrictedPython.** Notebooks
  are an admin tool; the audience is VS-Code-class developers on a
  trusted-local machine. The RestrictedPython sandbox stays scoped
  to the script library's data-feeder use case (where untrusted
  community-contributed code runs).
* **`promote_to_alert_group` cells bypass the cache by design.** The
  preview embeds CURRENT AG state for the diff pane; serving a stale
  "no_change" decision after the operator edited the AG outside the
  notebook would erode dev → prod trust. The cell re-runs every time
  (cheap - read AG YAML + saved-search YAMLs).
* **PDF export doesn't render charts.** WeasyPrint is a static HTML
  renderer (no JavaScript). Chart cells appear as their JSON spec
  text in the PDF. Use HTML export for charts.

## See also

* [`02_commands.md`](02_commands.md) - full SPQL command reference (used in `spql` / `pipe` cells)
* [`03_functions.md`](03_functions.md) - built-in functions
* [`12_alert_groups.md`](12_alert_groups.md) - AG schema + dispatcher behaviour
* [`17_semantic_search.md`](17_semantic_search.md) - `| nearest` / `| dedup_semantic` (often used in Pipe cells)
* [`18_llm_pipes.md`](18_llm_pipes.md) - `| llm` / `| llm_batch` (the Pipe-cell building blocks)

# Visual Builder

> Phase 4 / Bet 4.1. Drag-drop pipeline canvas backed by the SPQL grammar.
> Round-trip lossless to the text editor (slice 6); per-command forms +
> starter templates + onboarding tour (slice 7).

The Visual Builder is the SPQL on-ramp for users who don't write the language fluently. Drag stages from a palette onto a canvas, configure their kwargs, see the generated SPQL update live, click Run to execute against your indexes.

Open via the **Develop → Visual Builder** tab.

## Why a visual builder

The text-first surface (the SPQL editor) is what power users live in. The visual builder is for everyone else - analysts, ops folks, anyone who needs to ship one query without learning the full grammar first. It's also the better surface for *teaching*: every stage card is a labelled, configurable, removable atom. Mistakes show up immediately as wrong-shaped output.

## Slice 5 - what's shipped

### Page layout

Three columns:

1. **Palette (left)** - list of every SPQL command available, grouped by category (Filter, Aggregate, Reshape, Multi-value, Joins/Append, Semantic, LLM, Misc). Categories come from a hand-curated map in the SPA; any grammar command not in a named group falls into "More" so future grammar additions never silently disappear.
2. **Canvas (center)** - the drop target. Stage cards land here when dragged from the palette. Each card has the command type as a badge, a free-text kwargs input, and a remove (×) button. An optional `index="..."` clause sits above the canvas (the initial-clause placement that real SPQL queries need).
3. **Preview (right)** - the generated SPQL string (live-rendered as you build) plus the result of the most recent Run.

### How execution works

The Run button assembles the generated SPQL string from the index clause + the ordered stages, POSTs it to the existing `/api/query` endpoint, and renders the resulting DataFrame as a small HTML table (capped at 50 rows × 30 columns for skim - the full payload is in the API response).

There is no new backend endpoint. Slice 5 is pure SPA; the visual builder is a different way to *compose* SPQL, not a different way to *execute* it.

### Stage card kwargs

Slice 5 ships a single free-text input per card. You type whatever the command's grammar accepts (e.g. `count by host` for `| stats`, `model="..." prompt="..."` for `| llm`). Per-command form templates (model picker for `| llm`, by-clause builder for `| stats`, etc.) land in **slice 6** alongside the round-trip parser.

This keeps slice 5 small + safe: the visual builder works for every grammar command on day one, even if the kwargs UX is "type it" rather than "click it" for now.

## Slice 6 - round-trip + reorder

### Round-trip text → visual (the headliner)

Paste an SPQL string into the canvas; reconstruct stage cards. Click the **"Load existing SPQL into canvas (round-trip)"** disclosure on the canvas toolbar, paste your SPQL, click **↓ Load**. Canvas resets, the parsed `index="..."` clause lands in the index input, and one stage card appears per pipe segment.

Implementation:

* Server-side parser: `lexers/spql_pipeline_split.py::split_spql_pipeline(text) -> {index_clause, stages}`. Splits on `|` outside double-quoted strings (so `regex msg "(a|b|c)"` survives). Initial-clause detection: first segment treated as `index_clause` iff it starts with `index=` (case insensitive). Each non-initial segment splits into `{command, kwargs}` on the first whitespace boundary; internal whitespace runs collapse to single spaces for round-trip stability.
* Endpoint: `POST /api/visual-builder/parse` - body `{spql: "..."}`, response `{status, index_clause, stages}`. New endpoint justified by new BEHAVIOUR (parsing SPQL → stage list); see `reference_reuse_existing_endpoint_for_ui_surface.md`.
* SPA: `_vbLoad()` POSTs to the endpoint and replaces `_vbStages` + index input with the parsed structure.

### Lossless guarantee

`join_spql_pipeline(split_spql_pipeline(s))` produces a string that re-parses to the same `{index_clause, stages}` (modulo whitespace normalisation). Pinned by `tests/test_spql_pipeline_split.py::TestRoundTripLossless` against a hand-curated 100-query corpus covering every Phase 1-4 pipe, common SPQL patterns, and the load-bearing `pipe-inside-quoted-string` edge case. ROADMAP exit criterion: ≥100 queries serialize visual ↔ text identically. ✓

### Drag-to-reorder

Each stage card has a drag handle (⋮⋮). Drag a handle and drop on another stage card to reorder. Implementation: HTML5 native `draggable=true` on the handle, `dragstart`/`dragover`/`drop` event handlers on the card. The drop handler re-orders the `_vbStages` array and re-renders. Idempotent wiring via `dataset` markers - `_vbWireStageReorder()` runs after every canvas re-render without double-binding.

The canvas drop zone (for new stages from the palette) and the stage-card drop zones (for reorder) are kept distinct: the stage-card drop only fires when `_vbDragStageId` is set, which only happens via the stage-handle dragstart. Palette-to-canvas drops don't trigger the reorder path.

## Slice 7 - per-command forms + starter templates + onboarding tour

### Per-command form templates

Each stage card now renders in one of two modes:

* **Form** - structured widgets (input, textarea, number, select) labelled per kwarg. The card builds the kwargs string via a per-template `serialize()` function. Default mode when the kwargs parse cleanly into the template's fields.
* **Raw** - the slice-5 free-text input, full grammar control. Default mode when no template exists for the command, or when the current kwargs can't be cleanly represented in the form.

A toggle button (⚙ for form, ✎ for raw) on each card switches modes. The toggle from raw → form refuses + shows a hint message if the current kwargs string isn't parseable into the form view ("kwargs cannot be parsed; edit the raw kwargs first"). It never silently overwrites operator-typed text.

Templates ship for: `head`, `limit`, `sort`, `stats`, `eventstats`, `streamstats`, `eval`, `where`, `search`, `fields`, `table`, `rename`, `nearest`, `dedup_semantic`, `llm`, `llm_batch`, `llm_route`, `llm_refine`, `llm_ensemble`, `llm_until`. Other commands keep the slice-5 free-text kwargs input.

Every `| llm*` form template surfaces the slice-7 budget-gate kwargs (`max_cost_usd`, `dry_run`, `timeout_seconds`, `use_cache`) as form fields - pinned by `tests/test_visual_builder_slice7.py::TestFormTemplateRegistry::test_llm_common_fields_include_budget_gate`.

The form/raw bidirectional contract: `serialize(parse(text))` produces a kwargs string that re-parses to the same `{key: value}` object (modulo whitespace normalisation). Pinned by `tests/test_visual_builder_slice7.py::TestFormModePreservesLossless` against the slice-6 corpus.

### Starter templates

A new **"Start from a template"** disclosure on the canvas toolbar lists 12 preset pipelines covering common patterns:

| ID | Category | Description |
|----|----------|-------------|
| top_n_by_field | Starter | Group + count + sort + head - the SPQL "top N" pattern |
| time_bucketed_aggregate | Starter | Bucket _epoch into hourly bins + count per bin |
| multivalue_expand_dedup | Starter | Flatten a multi-value column + dedup |
| lookback_filter | Starter | earliest=-1d + where + sort - typical alert-search pattern |
| rename_then_table | Reshape | rename + table for a clean export shape |
| semantic_search | Semantic | Phase 1 / `\| nearest` query string lookup |
| semantic_dedup | Semantic | Phase 1 / `\| dedup_semantic` on a text column |
| cost_cascade_route | LLM | Phase 4 / `\| llm_route` cheap → expensive escalation |
| editor_grade_summary | LLM | Phase 4 / `\| llm_refine` drafter+critic with APPROVED convention |
| high_stakes_ensemble | LLM | Phase 4 / `\| llm_ensemble unanimous` - disagreement = no_consensus |
| convergence_loop | LLM | Phase 4 / `\| llm_until ... converge_when_output_contains="DONE"` |
| cross_source_volume_top | Pattern | Filter, table, sort by volume - cross-source analysis frame |

Templates ship as a JS const map embedded in the SPA - NOT a new YAML store. No new persistence layer; clicking a template card calls `_vbApplyTemplate(id)` which routes through `_vbLoadFromString(spql)` → `POST /api/visual-builder/parse` → reconstructed stage cards via `_vbAddStage`. The slice-6 round-trip lossless contract is the regression bar - every starter SPQL is pinned by `tests/test_visual_builder_slice7.py::TestStarterTemplatesRoundTrip`.

### Onboarding tour

A new **"Take the tour"** button in the page header launches a 10-step guided walkthrough of the Visual Builder. The tour reuses the existing tour engine (`startTour`, `TOURS`, `tour-tooltip` etc. from the Docs page) - no new infrastructure. The new TOURS entry is `visual_builder_intro`.

Step structure: welcome (centered modal) → palette → canvas → index input → form vs raw kwargs (auto-seeds two demo stages if canvas is empty; non-destructive) → templates section → load section → SPQL preview → run button → completion.

The tour is operator-driven (button click) - not auto-launched. Per the `feedback_user_visible_slices_end_with_manual_test_handoff.md` principle: surprising users with a forced tour on first visit is worse UX than offering it visibly.

### Worked example: build a cost-cascade with one click

1. Visual Builder → expand **"Start from a template"** → click **"Cost-cascade route"**.
2. Canvas reconstructs with stage cards: `nearest`, `llm_route`, `where`, `sort`. Index input shows `index="indexes/news/*.parquet" earliest=-1d`.
3. Each card defaults to **form mode**: structured widgets pre-populated from the template's kwargs.
4. Edit `llm_route`'s `confidence_threshold` widget from `0.5` to `0.7` - the live SPQL preview updates immediately. The kwargs string under the hood becomes `... confidence_threshold=0.7 ...`.
5. Click **▶ Run** - POSTs the assembled SPQL to `/api/query` (same endpoint as the SPQL editor) - result table appears.

Total time: ~10 seconds. The non-SPQL user has built and run a Phase 4 cost-cascade pipeline without typing a single SPQL keyword.

## Slice 7 - what's deferred

* **Self-healing scripts** (slice 8 candidate, may be deferred to Phase 6) - failed-feeder → AG drafts patch → GitHub PR. Two-part track: 8a = patch generation (Claude prompt + diff), 8b = GitHub integration. Splittable.
* **Phase 4 close + cross-cutting audit** (slice 9) - mirrors the Phase 2 slice-8 / Phase 3 slice-10 audit pattern: one test class per ROADMAP cross-cutting principle, ROADMAP retrospective, CHANGELOG.

## Schema additions

None. Slices 5 + 6 + 7 are all UI-only. The Visual Builder reuses `/api/grammar/vocab`, `/api/query`, and the slice-6 `/api/visual-builder/parse` endpoint. No new server-side routes. No schema migration.

## Limitations + design choices

* **No new backend endpoints** - the visual builder is a SPQL composer, not a separate query engine. This keeps the bug surface small (every visual-builder query is *also* a valid SPQL string).
* **Free-text kwargs in slice 5** - punted on per-command form templates to keep the slice small. Trade-off: visual builder works for every command immediately, at the cost of "click-to-fill" UX for the per-command kwargs.
* **No stage reordering yet** - slice 5 supports add + remove only. Reordering is a slice 6 deliverable (alongside round-trip).
* **Result rendering is capped** - slice 5 shows the first 50 rows × 30 columns of the result. The full payload is in the `/api/query` response if the operator needs it.

## Worked example: news classification cascade

In slice 5 the workflow is:

1. Type `index="news/*.parquet" earliest=-1d` in the index input.
2. Drag `nearest` from the **Semantic (Phase 1)** group. Type `"fed pause" topk=20` in the kwargs input.
3. Drag `llm_route` from the **LLM (Phase 2-4)** group. Type `model="ollama-llama3-1-8b" prompt="Classify urgency 0-1" escalate_to="claude-haiku-4-5-20251001" max_cost_usd=0.20` in the kwargs input.
4. Drag `where` from the **Filter** group. Type `_llm_output >= 0.7` in the kwargs input.
5. Drag `sort` from the **Reshape** group. Type `- _llm_output` in the kwargs input.

Generated SPQL (live in the preview pane):

```spql
index="news/*.parquet" earliest=-1d
| nearest "fed pause" topk=20
| llm_route model="ollama-llama3-1-8b" prompt="Classify urgency 0-1" escalate_to="claude-haiku-4-5-20251001" max_cost_usd=0.20
| where _llm_output >= 0.7
| sort - _llm_output
```

Click Run → the result table appears in the right pane.

## See also

* [`02_commands.md`](02_commands.md) - full SPQL command reference (covers every stage in the palette)
* [`03_functions.md`](03_functions.md) - built-in functions
* [`17_semantic_search.md`](17_semantic_search.md) - `| nearest` / `| dedup_semantic`
* [`18_llm_pipes.md`](18_llm_pipes.md) - `| llm` / `| llm_batch` / `| llm_route` / `| llm_refine` / `| llm_ensemble` / `| llm_until`
* [`19_notebooks.md`](19_notebooks.md) - the power-user iteration surface (compare/contrast)

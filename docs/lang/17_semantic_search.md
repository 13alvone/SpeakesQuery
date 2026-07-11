# Semantic Search in SpeakesQuery

> **Status**: Phase 1 (Semantic Foundation) - **all 6 slices shipped.** The `| nearest` and `| dedup_semantic` pipes are first-class SPQL stages backed by a local sentence-transformer model, the background sweeper auto-populates per-source embedding sidecars, Settings exposes all the operational knobs, and a sidecar fast path skips re-embedding when the input rows align 1:1 with on-disk sidecar entries. No cloud calls; everything runs on your machine.

## What this is

SPQL has always been excellent at lexical matching - `search`, `regex`, `match()`, glob patterns. It was *blind* to semantics: there's no `OR`-list of keywords that reliably catches every paraphrase of "the Fed paused rate hikes" across news outlets, prediction markets, and Reddit posts.

The semantic-search slice fixes this. Two new pipes, both backed by the same primitive:

| Pipe | Purpose |
|------|---------|
| `\| nearest "<query>"` | rank rows by cosine similarity to a free-text query |
| `\| dedup_semantic` | drop near-duplicate rows (paraphrases of the same story / event) |

Under the hood, both pipes embed the input rows + the query (for `nearest`) into 384-dimensional vectors using the **all-MiniLM-L6-v2** sentence-transformer (~80 MB, MIT-licensed). Vectors are L2-normalized, so cosine similarity is a simple dot product. Embedding runs on CPU at ~5–20 ms/row on M-series; for 1000 rows expect ~5–10 s.

## Quick start

```spl
# Find news about the Fed paushing rates - catches FOMC paraphrases too
index="news/*.parquet" earliest=-24h
| nearest "fed pause" topk=10
```

```spl
# Collapse near-duplicate stories before sending to Claude (saves tokens)
index="news/*.parquet" earliest=-2h
| dedup_semantic threshold=0.85
| llm_batch model=claude-haiku prompt="summarise"
```

```spl
# Embed only one specific column rather than all text fields
index="kalshi/*.parquet"
| nearest "fed rate decision may 2026" topk=5 field=question
```

## `| nearest` reference

Rank rows by cosine similarity to a query string. Adds a `_similarity` column, sorts descending, and optionally trims to the top *K* and/or drops rows below a threshold.

### Syntax

```
| nearest "<query string>" [topk=<N>] [threshold=<F>] [field=<column>]
```

### Arguments

- **Query string** *(required, first positional)* - the text to compare every row against. Always double-quoted.
- **`topk`** *(optional, default `10`)* - keep the top *N* rows by similarity. `topk=0` keeps all rows (just sorted).
- **`threshold`** *(optional)* - drop rows below this cosine similarity. Valid range `[-1.0, 1.0]`. Typical values: `0.3` (loose), `0.5` (moderate), `0.7` (strict).
- **`field`** *(optional)* - embed only the named column. Default: concatenate all string columns (excluding `_epoch`, `_similarity`, `_row_id`) with newlines.

### Output

The input DataFrame plus a new `_similarity` column, sorted descending. Threshold and `topk` apply *after* sorting.

### What "similarity" means in practice

| Cosine | Interpretation |
|--------|----------------|
| `≥ 0.95` | near-identical (literal duplicates, copy-pasted text) |
| `0.7 – 0.95` | strong semantic match (paraphrases of the same fact) |
| `0.4 – 0.7` | thematic match (same topic, different framing) |
| `0.2 – 0.4` | loose conceptual link |
| `< 0.2` | unrelated |

These bands depend on your domain - for news headlines, even paraphrases tend to score 0.4–0.5 because the Mini-LM model is small. For longer documents (paragraphs, articles) you'll see tighter clustering.

### Examples

**Catch paraphrases of "the Fed paused":**

```spl
index="news/*.parquet" earliest=-24h
| nearest "federal reserve paused interest rate hikes" topk=20
```

Returns rows about "FOMC holds steady", "Powell skips a hike", "central bank pauses tightening" - none of which match a `search` for `"fed pause"` literally.

**Surface the most-relevant rows for a brief, dropping the long tail:**

```spl
index="news/*.parquet" earliest=-2h
| nearest "geopolitical risk to oil supply" topk=5 threshold=0.35
```

Returns up to 5 rows, but only those with similarity ≥ 0.35. Lets you avoid sending obvious unrelated noise to a downstream LLM stage.

**Restrict the embedding to one column** (e.g. when other columns contain noisy auto-generated metadata):

```spl
index="reddit/*.parquet"
| nearest "ai chip demand" topk=10 field=title
```

The `body` and other columns are ignored; only `title` text drives the ranking.

## `| dedup_semantic` reference

Drop near-duplicate rows. Walks rows in order; keeps a row only if its cosine similarity to every previously-kept row is **below** the threshold. The first row in the input is always kept (no priors to compare against).

### Syntax

```
| dedup_semantic [threshold=<F>] [field=<column>]
```

### Arguments

- **`threshold`** *(optional, default `0.85`)* - cosine cutoff. Pairs at or above this are duplicates. Valid range `[-1.0, 1.0]`.
- **`field`** *(optional)* - dedup using only the named column. Default: concatenate all text columns.

### Output

A filtered DataFrame with near-duplicates removed. Preserves the input's order for surviving rows.

### Choosing a threshold

- **`0.95+`** - only catches literal duplicates (identical or near-identical strings)
- **`0.85`** *(default)* - catches obvious paraphrases ("Fed pauses" / "Fed holds steady")
- **`0.7`** - collapses topical clusters aggressively (multiple outlets covering the same story)
- **`0.5–0.6`** - collapses to one row per *theme*, not per story
- **`< 0.4`** - likely too aggressive; you'll lose distinct stories that share vocabulary

Start with `0.85`; tune downward only if you're seeing too many duplicates pass through.

### Why this matters: token-cost cascading

The classic application is right before an LLM stage that fans out per row. Five news outlets covering the same Fed announcement become one Claude prompt instead of five - direct dollar savings, identical analysis quality.

```spl
# Without dedup_semantic: 50 articles × Claude Haiku = $$$
# With dedup_semantic: ~15 unique stories × Claude Haiku = ~30% the cost
index="news/*.parquet" earliest=-2h
| dedup_semantic threshold=0.80
| llm_batch model=claude-haiku prompt="rate 1-10 for market relevance"
```

## Settings

The Settings page exposes four knobs under **Semantic Search**:

| Setting | Default | What it does |
|---------|---------|--------------|
| `embeddings_enabled` | `false` | Master switch for the background sweeper. When off, `\| nearest` and `\| dedup_semantic` still work - they just embed every row on each call. When on, the sweeper runs every `embedding_sweep_interval_minutes` and populates sidecars. |
| `embedding_model_name` | `sentence-transformers/all-MiniLM-L6-v2` | HuggingFace model identifier. Higher-recall alternatives: `BAAI/bge-base-en-v1.5` (768 dims, ~440 MB), `nomic-ai/nomic-embed-text-v1.5` (768 dims, ~270 MB). Restart required after change. |
| `max_embeddings_size_gb` | `5` | Total cap on `*.embeddings.parquet` sidecars. Independent of `max_total_size_gb` so a runaway sweeper never evicts indexed data. ~3M rows fit at 384 dims/float32. |
| `embedding_batch_size` | `32` | Encoder batch size. CPU-friendly default; bump to 128+ on a beefy GPU. |
| `embedding_sweep_interval_minutes` | `15` | Sweeper cadence. Floor 1, ceiling 1440. |

All five settable via the Settings UI **and** by editing `global_settings.yaml` directly.

## Bootstrap CLI

For an existing corpus, you can backfill all sidecars in one shot rather than waiting for the sweeper to chip away at it:

```bash
# Default - backfills indexes/ from settings.indexes_dir()
python -m tools.embed_backfill

# Custom root
python -m tools.embed_backfill --root /tmp/news

# Sweep + budget eviction in one pass
python -m tools.embed_backfill --cleanup

# Machine-readable JSON output (pipe to jq)
python -m tools.embed_backfill --json
```

Same code path as the engine-scheduled sweeper; per-source telemetry shows which sources got embedded, skipped (fresh / empty / no-text), or failed. Exit code is `0` on a clean sweep, `1` if any source failed.

## How it works under the hood

1. **Embedder** (`analyzers/embedder.py`) - lazy-loaded sentence-transformers wrapper. Singleton process-wide; first call downloads ~80 MB; subsequent calls are instant. L2-normalizes vectors so cosine == dot product.
2. **Sidecar parquets** (`functionality/embedding_sidecar.py`) - per-source `<source>.embeddings.parquet` files holding `(_row_id, _epoch, embedding FixedSizeList<float, dim>)`. Atomic write, parquet key-value metadata captures `model_name` + `dim` for drift detection.
3. **Background sweeper** (`functionality/embedding_sweeper.py`) - registered in the scheduled-input engine when `embeddings_enabled=True`. Walks `indexes/` every `embedding_sweep_interval_minutes`, populating sidecars for sources without them.
4. **Sidecar fast path (slice 6)** - both pipes try a sidecar lookup *first*. When the input DataFrame's rows align 1:1 with on-disk sidecar entries (most often the case immediately after `index="..."` with no upstream filter), the precomputed embeddings are reused and `encode_batch()` is skipped entirely. Only the query string still needs encoding.
5. **Embed-on-the-fly fallback** - when the fast path doesn't apply (no `_source_file`, missing/stale sidecar, model swap not yet swept, upstream filter dropped rows, `field=` pinned to a specific column), both pipes fall back to embedding every row from scratch. Slower but always correct.

The fast path's detection is **conservative by design**: any uncertainty about whether sidecar entries align with the current df rows triggers the slow path. Wrong embeddings would silently produce wrong rankings - slower-but-correct is preferred to faster-but-wrong every time.

For the full architecture and forward roadmap see [`ROADMAP.md`](../../ROADMAP.md) under "Bet 2: Semantic Depth Across Feeders".

## Cost model

**Slow path (no sidecar / sidecar misaligned):** every row in the input gets embedded once per pipe call. On Apple Silicon CPU with the default model:

| Rows | Slow-path latency |
|-----:|-------------------|
| 50    | < 0.5 s |
| 500   | 2–5 s |
| 1 000 | 5–10 s |
| 10 000+ | mostly the encode cost; consider keeping sidecars hot |

**Fast path (sidecars align with input rows):** only the query string is encoded; row vectors are read from sidecar parquet via pyarrow. Latency is dominated by the parquet read and is roughly:

| Rows | Fast-path latency |
|-----:|-------------------|
| 50      | ~ 50 ms (single parquet seek) |
| 1 000   | ~ 100 ms |
| 10 000  | ~ 200–400 ms |

Memory overhead: ~250 MB resident with the model loaded. The model loads once per process and stays cached. Fast-path memory peaks at the (N, dim) numpy array size - 1.5 KB per row at default dims.

## Limitations

- **Small model.** all-MiniLM-L6-v2 (22 M params) is fast and cheap but not as accurate as larger models. Slice 5 will expose `embedding_model_name` so you can swap to BGE-base (768 dims) or Nomic-embed for higher recall at higher cost.
- **English-centric.** The default model handles other languages but with reduced quality. Multilingual variants are available via the same plumbing.
- **No HNSW index yet.** Both pipes still scan all input rows once they have the embeddings (whether from the fast path or freshly encoded). For corpora > 100 000 rows a future slice could layer DuckDB VSS HNSW indexing on top of the sidecars to push the scan down to logarithmic; for the current SpeakesQuery scale (typical AG outputs are 100–1000 rows) this hasn't been needed.
- **Fast path needs an unbroken `_source_file` provenance chain.** Once a query passes through `where`, `head`, `eval`-with-row-removal, or any other filter, the row-position-to-sidecar-row-id mapping breaks and the slow path takes over. That's correct behaviour - wrong embeddings would silently produce wrong rankings - but operators trying to preserve fast-path eligibility should put `| nearest` immediately after `index="..."`.

## Future direction

Phase 1 is **complete**. Bet 3 (Phase 2) layers `| llm` pipes on top - the semantic prefilter then becomes the unlock for cost-cascading: cheap local LLM filters survivors → expensive cloud LLM only sees the top of the funnel. A possible Phase 1.5 could add DuckDB VSS HNSW indexing on top of the existing sidecars when corpus sizes warrant it, but for current SpeakesQuery scale (typical AG outputs in the 100–1000 row range) the cache fast path delivers most of the benefit without the complexity.

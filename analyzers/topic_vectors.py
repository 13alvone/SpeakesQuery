"""
Curator Topic Vectors - Phase 6 / Bet 5 slice 3
─────────────────────────────────────────────────
Extracts topic-level representations from the user's watch history so
the curator composer can score candidates by *topical* similarity
instead of "channels I already watch by frequency."

The slice-1.5 candidate ingestion pulled RSS from existing YouTube
subscriptions, ranked by raw watch_count. The slice-2 composer scored
candidates by ``log(watch_count) / max_log``. Both layers were
bootstrap-locked to the user's existing YouTube curation - which is
exactly the bias the curator is meant to escape (memory:
``project_curator_vision_2026_05_16``).

Slice 3 breaks the lock at the *scoring* layer:

1. :func:`compute_topic_snapshot` - embed watch-history titles via the
   Phase 1 embedder, recency-weight them, KMeans into K clusters,
   write a structured snapshot record carrying centroids + exemplar
   titles per cluster.
2. :func:`score_candidates_against_snapshot` - per-candidate
   ``interest_score = max(cosine(title_emb, centroid_k) for k in K)``
   plus the matched ``topic_cluster_id``. Pure function over
   precomputed embeddings (slice-1.5 candidates already get sidecars
   via Phase 1 slice-3's :class:`functionality.embedding_sweeper`).
3. :func:`label_clusters_with_llm` - optional pass that asks a
   local/cheap LLM (Phase 2 cost cascade, default registry id
   ``llamacpp-qwen35-122b-a10b``, a self-hosted llama.cpp server) to
   render a 3–5 word human label per cluster. Money-leak gate: ``dry_run=True``
   returns placeholder labels at $0 (canary-pinned in tests).

Snapshots land at
``indexes/IMMUTABLE/curator_topic_snapshots/<epoch>_<uuid>.parquet``
(IMMUTABLE because *topic evolution over time* is forever-interesting
data - "what did the user care about 6 months ago vs today?" is a
load-bearing query for the life-project horizon). Schema is
additive-only per CLAUDE.md "Do Not drop a column from any curator_*
IMMUTABLE-bound schema" rule.

Design choices
--------------
* **Discrete clusters, not continuous centroid.** Interpretable; the
  LLM composer can reason about "your top 5 topic clusters labeled X,
  Y, Z" instead of a 384-dim opaque vector.
* **Recency decay** weights each watch by
  ``exp(-(now - watch_epoch) / decay_lambda_seconds)`` so newer
  watches dominate cluster definition. Configurable; default
  ``curator_topic_decay_lambda_days = 180`` (≈6-month half-life)
  matches the life-project framing.
* **K is configurable; default 10.** Empirically yields ~5–12 useful
  topic groups for personal histories of ~1–10k watches.
* **LLM labeling is optional.** Scoring works on cluster ids alone;
  labels are pure UX. The labeling pass has its own dry-run + cost
  cap so an operator can preview cluster centroids before spending.
* **Stateless functions.** All state is in arguments; no module-level
  caches. Callers serialize themselves (one snapshot-refresh background
  job; AG dispatcher scoring step runs single-threaded).

Generalisation
--------------
The shape - embed + cluster + label + score-against-cluster-centroids
- is genus-level. The same primitive will serve any future
"user-style" surface SpeakesQuery grows: Phase 7 article reader, Phase
8 chat assistant style mirror, etc. Keep the public API platform-
agnostic (no "video" / "youtube" in signatures).
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd

from analyzers.embedder import (
    cosine_similarity_matrix,
    get_embedder,
)

logger = logging.getLogger(__name__)


# ── Constants / settings keys ────────────────────────────────────────

_DEFAULT_N_CLUSTERS = 10
_DEFAULT_DECAY_LAMBDA_DAYS = 180.0
_DEFAULT_N_EXEMPLARS = 5
_DEFAULT_LABEL_MODEL_ID = "llamacpp-qwen35-122b-a10b"
_MIN_HISTORY_ROWS_FOR_CLUSTERING = 3

# Strips <think>...</think> sections that reasoning models like Qwen3
# emit before the actual response. Greedy across newlines; absent in
# non-reasoning model outputs (no-op).
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


# ── Errors ───────────────────────────────────────────────────────────

class TopicVectorsError(RuntimeError):
    """Base class for topic-vector failures (clustering, scoring, labeling).

    Carries an ``error_class`` tag so callers (and the eventual
    UI surface) can branch on the failure mode without parsing the
    message.
    """

    def __init__(self, message: str, *, error_class: str = "TopicVectorsError"):
        super().__init__(message)
        self.error_class = error_class


# ── Dataclasses ──────────────────────────────────────────────────────

@dataclass
class TopicCluster:
    """One cluster in a topic snapshot.

    ``centroid`` is L2-normalised float32 (matching the embedder
    convention) so downstream cosine similarity is a dot product.
    ``weight`` is the sum of recency-decay weights of cluster members;
    ``n_members`` is the raw count. The two diverge when the operator
    has watched something a lot many years ago - high count, low
    weight - which is exactly what the cluster-importance signal
    should reflect.
    """

    cluster_id: int
    centroid: np.ndarray
    weight: float
    n_members: int
    exemplar_titles: list[str]
    label: str = ""


@dataclass
class TopicSnapshot:
    """A single point-in-time clustering of the user's watch history.

    Persisted to ``indexes/IMMUTABLE/curator_topic_snapshots/``; the
    additive-only schema rule means new fields can be appended in
    future slices but existing fields are forever.
    """

    snapshot_epoch: int
    snapshot_id: str
    model_name: str
    dim: int
    n_clusters: int
    n_history_rows: int
    decay_lambda_days: float
    clusters: list[TopicCluster] = field(default_factory=list)


# ── Settings shim (mirrors embedder.py's pattern) ───────────────────

def _get_setting(key: str, default: Any) -> Any:
    try:
        from global_settings import get_settings
        value = get_settings().get(key)
        return value if value is not None else default
    except Exception:
        return default


# ── Public: compute snapshot ────────────────────────────────────────

def compute_topic_snapshot(
    history_df: pd.DataFrame,
    *,
    title_col: str = "title",
    epoch_col: str = "_epoch",
    n_clusters: Optional[int] = None,
    decay_lambda_days: Optional[float] = None,
    n_exemplars: int = _DEFAULT_N_EXEMPLARS,
    now_epoch: Optional[int] = None,
    random_state: int = 0,
) -> TopicSnapshot:
    """Embed history titles, recency-weight, KMeans cluster, return snapshot.

    Parameters
    ----------
    history_df :
        DataFrame with at least ``title_col`` (string) and ``epoch_col``
        (int Unix seconds). Other columns ignored.
    title_col :
        Column name carrying the text to embed. Defaults to ``"title"``.
    epoch_col :
        Column name carrying the Unix-second timestamp for recency
        weighting. Defaults to ``"_epoch"``.
    n_clusters :
        Target number of clusters. ``None`` reads the
        ``curator_topic_n_clusters`` global setting (default 10).
        Capped at ``len(history_df)`` so KMeans never raises on
        small inputs.
    decay_lambda_days :
        Half-life parameter for recency weighting (in days). Older
        watches' contributions to clustering and weighting fall
        exponentially. ``None`` reads ``curator_topic_decay_lambda_days``
        (default 180 ≈ 6-month half-life).
    n_exemplars :
        How many of the cluster-closest titles to retain per cluster
        for downstream labeling / UI display.
    now_epoch :
        Override for "now" in recency calculations (testing).
        Defaults to wall-clock Unix seconds.
    random_state :
        Deterministic KMeans seed for reproducible snapshots in tests.

    Returns
    -------
    TopicSnapshot
        Fully populated snapshot. ``clusters[i].label`` is empty -
        call :func:`label_clusters_with_llm` afterwards if a label
        pass is wanted.

    Raises
    ------
    TopicVectorsError
        On empty history, missing columns, or import failure for the
        sklearn KMeans dependency.
    """
    if history_df is None or len(history_df.index) == 0:
        raise TopicVectorsError(
            "compute_topic_snapshot: history_df is empty. "
            "Curator topic vectors need at least a few watched titles.",
            error_class="EmptyHistory",
        )
    for col in (title_col, epoch_col):
        if col not in history_df.columns:
            raise TopicVectorsError(
                f"compute_topic_snapshot: history_df is missing column "
                f"{col!r}. Got: {list(history_df.columns)}",
                error_class="MissingColumn",
            )

    target_k = (
        int(n_clusters)
        if n_clusters is not None
        else int(_get_setting("curator_topic_n_clusters", _DEFAULT_N_CLUSTERS))
    )
    lambda_days = (
        float(decay_lambda_days)
        if decay_lambda_days is not None
        else float(_get_setting(
            "curator_topic_decay_lambda_days", _DEFAULT_DECAY_LAMBDA_DAYS,
        ))
    )
    if lambda_days <= 0.0:
        raise TopicVectorsError(
            f"decay_lambda_days must be > 0, got {lambda_days}",
            error_class="InvalidDecay",
        )

    titles = [
        str(t) if t is not None else "" for t in history_df[title_col].tolist()
    ]
    epochs = (
        pd.to_numeric(history_df[epoch_col], errors="coerce")
        .fillna(0.0)
        .astype(float)
        .tolist()
    )

    if len(titles) < _MIN_HISTORY_ROWS_FOR_CLUSTERING:
        raise TopicVectorsError(
            f"compute_topic_snapshot: need at least "
            f"{_MIN_HISTORY_ROWS_FOR_CLUSTERING} history rows to cluster, "
            f"got {len(titles)}.",
            error_class="InsufficientHistory",
        )

    # Cap K so KMeans never blows up on small histories (an operator
    # bootstrapping with a partial Takeout import will hit this).
    effective_k = max(1, min(target_k, len(titles)))

    # Recency weights - exp(-age_days / lambda_days). Watches in the
    # future (clock skew) get weight 1.0 by clamping age at 0.
    now_s = int(now_epoch) if now_epoch is not None else int(time.time())
    lambda_seconds = max(1.0, lambda_days * 86400.0)
    weights = np.array(
        [
            math.exp(-max(0.0, (now_s - e)) / lambda_seconds)
            for e in epochs
        ],
        dtype=np.float64,
    )

    embedder = get_embedder()
    embeddings = embedder.encode_batch(titles)
    if embeddings.shape[0] != len(titles):
        # Defensive - should never happen but a mismatch silently
        # breaks every downstream array index.
        raise TopicVectorsError(
            f"Embedder returned {embeddings.shape[0]} vectors for "
            f"{len(titles)} titles. Embedder bug.",
            error_class="EmbedderRowMismatch",
        )

    try:
        from sklearn.cluster import KMeans
    except ImportError as exc:
        raise TopicVectorsError(
            "compute_topic_snapshot needs scikit-learn. "
            "Install via `pip install scikit-learn` and restart, or "
            "rebuild the Docker image with the dep declared in "
            "requirements.txt.",
            error_class="MissingDependency",
        ) from exc

    kmeans = KMeans(
        n_clusters=effective_k,
        random_state=random_state,
        n_init=10,
    )
    cluster_ids = kmeans.fit_predict(embeddings, sample_weight=weights)
    centroids = np.asarray(kmeans.cluster_centers_, dtype=np.float32)

    # Renormalise centroids so cosine-similarity downstream is a clean
    # dot product. KMeans centroids are arithmetic means of normalised
    # inputs - typically near-but-not-exactly unit norm.
    norms = np.linalg.norm(centroids, axis=1, keepdims=True) + 1e-12
    centroids = centroids / norms

    clusters: list[TopicCluster] = []
    titles_arr = np.asarray(titles)
    for cid in range(effective_k):
        member_mask = cluster_ids == cid
        if not member_mask.any():
            # KMeans occasionally produces empty clusters on tiny
            # inputs. Skip rather than emit a zero-weight cluster
            # that confuses the composer.
            continue
        member_weights = weights[member_mask]
        member_embeddings = embeddings[member_mask]
        # Exemplars = titles closest to centroid by cosine, descending.
        sims = cosine_similarity_matrix(centroids[cid], member_embeddings)
        order = np.argsort(-sims)[:n_exemplars]
        member_titles = titles_arr[member_mask][order].tolist()

        clusters.append(TopicCluster(
            cluster_id=cid,
            centroid=centroids[cid].astype(np.float32),
            weight=float(member_weights.sum()),
            n_members=int(member_mask.sum()),
            exemplar_titles=[str(t) for t in member_titles],
            label="",
        ))

    snapshot = TopicSnapshot(
        snapshot_epoch=now_s,
        snapshot_id=str(uuid.uuid4()),
        model_name=embedder.model_name,
        dim=int(embedder.dim),
        n_clusters=len(clusters),
        n_history_rows=int(len(titles)),
        decay_lambda_days=float(lambda_days),
        clusters=clusters,
    )
    logger.info(
        "[i] compute_topic_snapshot: built %d clusters from %d titles "
        "(model=%s, lambda=%.1fd)",
        snapshot.n_clusters, snapshot.n_history_rows,
        snapshot.model_name, snapshot.decay_lambda_days,
    )
    return snapshot


# ── Public: score candidates ────────────────────────────────────────

def score_candidates_against_snapshot(
    candidates_df: pd.DataFrame,
    snapshot: TopicSnapshot,
    *,
    title_col: str = "title",
    interest_col: str = "interest_score",
    cluster_id_col: str = "topic_cluster_id",
    label_col: str = "topic_label",
    similarity_col: str = "topic_similarity",
) -> pd.DataFrame:
    """Add ``interest_score`` + ``topic_cluster_id`` + ``topic_label`` columns.

    Computes ``max(cosine(candidate_title_emb, centroid_k) for k in K)``
    per candidate. The matched cluster id is recorded; downstream the
    composer can balance picks across clusters (the diversity layer).

    Empty inputs are returned unchanged with empty score columns so
    composer pipelines never need to special-case "no candidates today"
    (memory: SPQL pipe handlers must tolerate empty input).

    Embeddings for candidates are computed on the fly via the singleton
    embedder. If candidate parquets carry the Phase 1 sidecar
    embeddings, callers can pre-attach them under a ``_title_embedding``
    column to skip the recompute - but the function works without that.
    """
    out = candidates_df.copy()
    if title_col not in out.columns:
        raise TopicVectorsError(
            f"score_candidates_against_snapshot: candidates_df missing "
            f"column {title_col!r}. Got: {list(out.columns)}",
            error_class="MissingColumn",
        )
    if len(out.index) == 0 or not snapshot.clusters:
        out[interest_col] = pd.Series([], dtype=float)
        out[cluster_id_col] = pd.Series([], dtype="Int64")
        out[label_col] = pd.Series([], dtype=str)
        out[similarity_col] = pd.Series([], dtype=float)
        return out

    titles = [str(t) if t is not None else "" for t in out[title_col].tolist()]
    embedder = get_embedder()
    embeddings = embedder.encode_batch(titles)

    centroids = np.stack(
        [c.centroid for c in snapshot.clusters], axis=0,
    ).astype(np.float32)
    # (K, dim) @ (N, dim).T → (K, N)
    sim_matrix = cosine_similarity_matrix(centroids, embeddings)
    best_k = np.argmax(sim_matrix, axis=0)  # (N,)
    best_sim = sim_matrix[best_k, np.arange(sim_matrix.shape[1])]  # (N,)

    cluster_id_to_label = {
        c.cluster_id: c.label for c in snapshot.clusters
    }
    snapshot_cluster_ids = [c.cluster_id for c in snapshot.clusters]

    out[interest_col] = best_sim.astype(float)
    out[similarity_col] = best_sim.astype(float)
    out[cluster_id_col] = pd.Series(
        [snapshot_cluster_ids[int(k)] for k in best_k], dtype="Int64",
    )
    out[label_col] = pd.Series(
        [cluster_id_to_label.get(snapshot_cluster_ids[int(k)], "") for k in best_k],
        dtype=str,
    )
    return out


# ── Public: label clusters via LLM ──────────────────────────────────

_LABEL_SYSTEM_PROMPT = (
    "You are a topic labeler. Given a small set of video titles that "
    "have been clustered together, output ONE short label (3-5 words, "
    "Title Case) that captures the topical theme of the cluster. "
    "Output ONLY the label text - no quotation marks, no extra "
    "commentary, no surrounding lines. If the cluster lacks a clear "
    "theme, output \"Mixed Topics\"."
)


def _format_label_user_prompt(exemplars: Sequence[str]) -> str:
    body = "\n".join(f"{i+1}. {t}" for i, t in enumerate(exemplars))
    return (
        "Cluster exemplar titles:\n"
        f"{body}\n\n"
        "Label:"
    )


def _clean_label(raw: str) -> str:
    """Strip reasoning-model think blocks, quotes, whitespace.

    Qwen3 emits ``<think>...</think>`` before its answer; older
    reasoning models use ``[REASONING]...[/REASONING]`` etc. We
    only strip the documented Qwen3 form here; other shapes can be
    layered on as new model families surface.
    """
    text = _THINK_BLOCK_RE.sub("", raw or "").strip()
    # Take only the first non-empty line - the prompt asks for one
    # line but some models add a trailing explanation.
    for line in text.splitlines():
        line = line.strip().strip("\"'`").strip()
        if line:
            return line[:80]
    return ""


def label_clusters_with_llm(
    snapshot: TopicSnapshot,
    *,
    model_id: Optional[str] = None,
    dry_run: bool = False,
    max_cost_usd: Optional[float] = None,
    request_id_prefix: Optional[str] = None,
) -> TopicSnapshot:
    """Populate ``cluster.label`` for every cluster via an LLM call.

    Mutates the passed snapshot in-place AND returns it. Dispatches
    one ``llm_router.call_llm`` per cluster (sequential, cheap with
    Qwen3 batches of 5–10 exemplars).

    Money-leak gate
    ---------------
    Per the slice-7 budget-pipe convention (CLAUDE.md "Do Not ship a
    new | llm-shaped pipe without max_cost_usd + dry_run"), this
    function honours both kwargs. ``dry_run=True`` short-circuits the
    LLM call entirely and writes a placeholder label (``"Cluster <id>
    (dry-run)"``). ``max_cost_usd`` caps the cumulative cost; once
    exceeded, remaining clusters get ``"Cluster <id> (budget capped)"``.

    Pinned by the slice-3 money-leak canary test.

    Parameters
    ----------
    snapshot :
        Snapshot to label. Mutated in-place.
    model_id :
        Registry id of the model to use. ``None`` reads
        ``curator_topic_label_model_id`` setting; default
        ``llamacpp-qwen35-122b-a10b`` (self-hosted llama.cpp server).
    dry_run :
        When True, no LLM calls are made and placeholder labels are
        written. Cost gate: useful for previewing snapshots before
        spending.
    max_cost_usd :
        Optional cumulative-cost ceiling. Set to a small positive
        number (e.g. 0.05) for safe automated runs; None for
        unbounded (but ``llamacpp-qwen35-122b-a10b`` and any other
        local model is $0 anyway).
    request_id_prefix :
        Optional prefix for the per-cluster request_id (eases log
        correlation with the snapshot-refresh job).

    Returns
    -------
    TopicSnapshot
        The same snapshot instance, with labels populated.
    """
    chosen_model = (
        model_id
        or _get_setting("curator_topic_label_model_id", _DEFAULT_LABEL_MODEL_ID)
    )

    if dry_run:
        for c in snapshot.clusters:
            c.label = f"Cluster {c.cluster_id} (dry-run)"
        logger.info(
            "[i] label_clusters_with_llm dry-run: stamped %d placeholders "
            "(model=%s, zero cost)", len(snapshot.clusters), chosen_model,
        )
        return snapshot

    # Lazy import - keeps test cases that monkeypatch llm_router stable.
    from analyzers.llm_router import call_llm, LLMRouterError

    cumulative_cost = 0.0
    prefix = request_id_prefix or f"curator-label-{snapshot.snapshot_id[:8]}"
    for cluster in snapshot.clusters:
        if max_cost_usd is not None and cumulative_cost >= float(max_cost_usd):
            cluster.label = f"Cluster {cluster.cluster_id} (budget capped)"
            continue
        try:
            rid = f"{prefix}-c{cluster.cluster_id}"
            resp = call_llm(
                chosen_model,
                prompt=_format_label_user_prompt(cluster.exemplar_titles),
                system=_LABEL_SYSTEM_PROMPT,
                request_id=rid,
                source="curator_topic_label",
                use_cache=True,
            )
            cleaned = _clean_label(resp.text)
            cluster.label = cleaned or f"Cluster {cluster.cluster_id}"
            cumulative_cost += float(resp.cost_usd or 0.0)
        except LLMRouterError as exc:
            logger.warning(
                "[!] label_clusters_with_llm: cluster %d label failed: %s - "
                "falling back to placeholder",
                cluster.cluster_id, exc,
            )
            cluster.label = f"Cluster {cluster.cluster_id}"

    logger.info(
        "[i] label_clusters_with_llm: labeled %d clusters via %s "
        "(cumulative cost $%.4f)",
        len(snapshot.clusters), chosen_model, cumulative_cost,
    )
    return snapshot


# ── Public: serialization ────────────────────────────────────────────

def snapshot_to_records(snapshot: TopicSnapshot) -> list[dict]:
    """Flatten a snapshot to one row dict per cluster for Parquet write.

    The IMMUTABLE log schema is one row per cluster (not one row per
    snapshot) so SPQL queries can ``stats avg(weight) by cluster_id``
    naturally and so an additive future schema can append per-cluster
    fields without re-keying.

    Centroid is stored as a JSON-encoded list-of-floats; SPQL today
    doesn't natively query vector columns, so JSON is the lowest-
    friction format. A future slice can add a Phase-1-style sidecar
    if vector-aware queries become useful.
    """
    return [
        {
            "snapshot_epoch": int(snapshot.snapshot_epoch),
            "snapshot_id": str(snapshot.snapshot_id),
            "model_name": str(snapshot.model_name),
            "dim": int(snapshot.dim),
            "n_clusters": int(snapshot.n_clusters),
            "n_history_rows": int(snapshot.n_history_rows),
            "decay_lambda_days": float(snapshot.decay_lambda_days),
            "cluster_id": int(c.cluster_id),
            "centroid_json": json.dumps(c.centroid.tolist()),
            "weight": float(c.weight),
            "n_members": int(c.n_members),
            "exemplar_titles_json": json.dumps(list(c.exemplar_titles)),
            "label": str(c.label),
        }
        for c in snapshot.clusters
    ]


def records_to_snapshot(records: Sequence[dict]) -> TopicSnapshot:
    """Inverse of :func:`snapshot_to_records`.

    Used by callers that read a snapshot from Parquet for downstream
    scoring. If multiple snapshots' rows are mixed in ``records``,
    only the most-recent ``snapshot_epoch`` is honoured (older rows
    are dropped) - caller should typically pre-filter.
    """
    if not records:
        raise TopicVectorsError(
            "records_to_snapshot: no rows.",
            error_class="EmptyRecords",
        )
    # Sort descending so we land on the latest snapshot regardless of
    # input order; group by snapshot_id to be tolerant of mixed input.
    latest_epoch = max(int(r["snapshot_epoch"]) for r in records)
    latest_id = next(
        r["snapshot_id"] for r in records
        if int(r["snapshot_epoch"]) == latest_epoch
    )
    rows = [r for r in records if r["snapshot_id"] == latest_id]
    rows.sort(key=lambda r: int(r["cluster_id"]))

    head = rows[0]
    clusters = [
        TopicCluster(
            cluster_id=int(r["cluster_id"]),
            centroid=np.asarray(
                json.loads(r["centroid_json"]), dtype=np.float32,
            ),
            weight=float(r.get("weight") or 0.0),
            n_members=int(r.get("n_members") or 0),
            exemplar_titles=list(json.loads(r.get("exemplar_titles_json") or "[]")),
            label=str(r.get("label") or ""),
        )
        for r in rows
    ]
    return TopicSnapshot(
        snapshot_epoch=int(head["snapshot_epoch"]),
        snapshot_id=str(head["snapshot_id"]),
        model_name=str(head["model_name"]),
        dim=int(head["dim"]),
        n_clusters=int(head["n_clusters"]),
        n_history_rows=int(head["n_history_rows"]),
        decay_lambda_days=float(head["decay_lambda_days"]),
        clusters=clusters,
    )


# ── Public: load latest snapshot from IMMUTABLE tree ────────────────

def load_latest_snapshot(
    indexes_root: Optional[Any] = None,
) -> Optional[TopicSnapshot]:
    """Return the most-recent topic snapshot from the IMMUTABLE tree, or None.

    Used by the composer dispatcher's post-feeder hook (slice 3) to
    score the candidate pool against the live snapshot at AG-fire time.

    The function reads every parquet under
    ``<indexes_root>/IMMUTABLE/curator_topic_snapshots/``, picks the
    rows whose ``snapshot_epoch`` is largest, and reconstructs the
    snapshot object via :func:`records_to_snapshot`. Returns ``None``
    (not raise) when no snapshot has been persisted yet - callers
    decide whether absence is a soft skip (composer falls back to
    legacy scoring) or a hard error.

    Parameters
    ----------
    indexes_root :
        Override for the indexes root path. ``None`` reads
        ``settings.indexes_dir()`` (or falls back to the project's
        ``indexes/`` directory).
    """
    from pathlib import Path

    if indexes_root is None:
        try:
            from global_settings import get_settings
            root = get_settings().indexes_dir()
        except Exception:
            root = Path(__file__).resolve().parent.parent / "indexes"
    else:
        root = Path(indexes_root)

    snapshots_dir = Path(root) / "IMMUTABLE" / "curator_topic_snapshots"
    if not snapshots_dir.is_dir():
        return None

    parquets = sorted(snapshots_dir.glob("*.parquet"))
    if not parquets:
        return None

    try:
        frames = [pd.read_parquet(p) for p in parquets]
    except Exception as exc:
        logger.warning(
            "[!] load_latest_snapshot: read failed under %s: %s",
            snapshots_dir, exc,
        )
        return None
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        return None
    if "snapshot_epoch" not in df.columns:
        return None

    # Pick rows belonging to the latest snapshot only. Compare via
    # snapshot_id (string equality) to be robust to multiple snapshots
    # sharing the same epoch (unlikely but possible on tests).
    max_epoch = int(df["snapshot_epoch"].max())
    latest_id = df.loc[df["snapshot_epoch"] == max_epoch, "snapshot_id"].iloc[0]
    rows = df.loc[df["snapshot_id"] == latest_id].to_dict("records")
    if not rows:
        return None
    return records_to_snapshot(rows)


__all__ = [
    "TopicCluster",
    "TopicSnapshot",
    "TopicVectorsError",
    "compute_topic_snapshot",
    "score_candidates_against_snapshot",
    "label_clusters_with_llm",
    "snapshot_to_records",
    "records_to_snapshot",
    "load_latest_snapshot",
]

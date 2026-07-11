"""
Local Embedder
──────────────
Lazy-loaded sentence-transformers wrapper. Single entry point used by
the Phase 1 semantic search layer (``| nearest``, ``| dedup_semantic``)
and any future code that needs to compare text by meaning.

Pattern mirrors :mod:`analyzers.claude_client`:

* **Lazy import** - ``sentence_transformers`` is imported only on first
  use, so apps boot cleanly on hosts that haven't been rebuilt with the
  new requirement yet. The :class:`MissingEmbeddingSDKError` carries an
  actionable install message that endpoints (e.g. a future "Test
  embedder" button) can render verbatim.
* **Thread-safe singleton** - the model load is expensive (~5 s plus an
  80 MB download on first call); concurrent ``encode()`` callers share
  one instance and never trigger a duplicate load.
* **Settings-driven** - ``embedding_model_name`` and
  ``embedding_batch_size`` are read from :mod:`global_settings`, so a
  user can swap the default ``all-MiniLM-L6-v2`` for a larger BGE/Nomic
  variant without code changes.
* **Normalized embeddings by default** - vectors are L2-normalized so
  cosine similarity is a plain dot product downstream (the ``| nearest``
  query layer pays for one matmul, not a full norm-divide).

Thread safety
-------------
The model is loaded under ``_model_lock``; subsequent encodes do not
take the lock (PyTorch handles concurrent inference internally). The
:func:`reset_for_tests` hook clears the singleton so tests can swap
models without leaking state between cases.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

logger = logging.getLogger(__name__)


# ── Errors ────────────────────────────────────────────────────────────

class EmbeddingError(RuntimeError):
    """Base class for embedder failures."""


class MissingEmbeddingSDKError(EmbeddingError):
    """Raised when the ``sentence_transformers`` package is not installed.

    The message is intentionally actionable so the UI can render it
    verbatim - mirrors :class:`analyzers.claude_client.ClaudeCallError`'s
    handling of ``MissingSDK``.
    """


# ── Settings helpers (lazy import to avoid circulars in tests) ───────

_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_DEFAULT_DIM = 384
_DEFAULT_BATCH_SIZE = 32


def _get_setting(key: str, default: Any) -> Any:
    try:
        from global_settings import get_settings
        value = get_settings().get(key)
        return value if value is not None else default
    except Exception:
        return default


# ── Singleton state ──────────────────────────────────────────────────

_model_lock = threading.Lock()
_embedder_singleton: "Embedder | None" = None


@dataclass
class EmbeddingResult:
    """Typed return value from :meth:`Embedder.encode_batch`.

    The bulk path through ``| nearest`` operates on the raw ndarray
    only, but downstream callers (sweeper telemetry, future audit logs)
    benefit from the matched ``model_name`` and ``dim`` metadata for
    drift detection.
    """

    vectors: np.ndarray
    model_name: str
    dim: int


# ── Embedder ─────────────────────────────────────────────────────────

class Embedder:
    """Wraps a single ``SentenceTransformer`` instance with a stable API.

    Construction loads the model - call :func:`get_embedder` instead of
    instantiating directly so a single load is shared process-wide.

    Encoded vectors are always L2-normalized ``float32``; cosine
    similarity is therefore equivalent to the dot product, which the
    ``| nearest`` pipe and the DuckDB VSS extension both exploit.
    """

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or _get_setting(
            "embedding_model_name", _DEFAULT_MODEL,
        )
        self._dim: int | None = None
        self._model = self._load_model(self._model_name)

    @staticmethod
    def _load_model(name: str) -> Any:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise MissingEmbeddingSDKError(
                "The 'sentence-transformers' Python package is not "
                "installed in this environment. Run "
                "`pip install 'sentence-transformers>=3.0,<5.0'` and "
                "restart the server (or, on Docker, rebuild the image "
                "with `./install.sh` now that the dependency has been "
                "added to requirements.txt)."
            ) from exc
        try:
            model = SentenceTransformer(name)
        except Exception as exc:
            raise EmbeddingError(
                f"Failed to load embedding model '{name}': {exc}. "
                "Check the model identifier (Hugging Face hub name or a "
                "local path) and that the host has network access for "
                "the first-use download."
            ) from exc
        try:
            model.eval()  # disable dropout etc. for deterministic inference
        except AttributeError:
            pass
        return model

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dim(self) -> int:
        """Embedding dimension; lazily computed on first access."""
        if self._dim is None:
            try:
                self._dim = int(self._model.get_sentence_embedding_dimension())
            except Exception:
                # Fall back to encoding a tiny probe.
                probe = self.encode("probe")
                self._dim = int(probe.shape[-1])
        return self._dim

    def encode(self, text: str) -> np.ndarray:
        """Encode a single string to a 1-D float32 vector.

        Empty / whitespace-only input still returns a normalized vector
        (sentence-transformers handles this internally; we don't second
        guess it). Input is coerced to ``str`` so a ``None`` slip
        through fails loudly rather than producing a misleading vector.
        """
        if text is None:
            raise TypeError("encode() requires a string, got None")
        out = self._model.encode(
            str(text),
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return self._coerce_float32(out, ndim=1)

    def encode_batch(
        self,
        texts: Sequence[str],
        batch_size: int | None = None,
        show_progress: bool = False,
    ) -> np.ndarray:
        """Encode a sequence of strings to a 2-D ``(N, dim)`` matrix.

        Empty input returns a zero-row matrix with the right column
        count, so downstream code can ``vstack`` without a special case.
        """
        if texts is None:
            raise TypeError("encode_batch() requires a sequence, got None")
        materialised = [str(t) if t is not None else "" for t in texts]
        if not materialised:
            return np.zeros((0, self.dim), dtype=np.float32)
        bs = int(batch_size) if batch_size else int(
            _get_setting("embedding_batch_size", _DEFAULT_BATCH_SIZE)
        )
        out = self._model.encode(
            materialised,
            batch_size=max(1, bs),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=bool(show_progress),
        )
        return self._coerce_float32(out, ndim=2)

    @staticmethod
    def _coerce_float32(arr: Any, *, ndim: int) -> np.ndarray:
        """Force the model output into a contiguous float32 ndarray."""
        np_arr = np.asarray(arr, dtype=np.float32)
        if np_arr.ndim != ndim:
            np_arr = np_arr.reshape(-1) if ndim == 1 else np_arr.reshape(
                (-1, np_arr.shape[-1])
            )
        return np.ascontiguousarray(np_arr)


def get_embedder(model_name: str | None = None) -> Embedder:
    """Return the process-wide :class:`Embedder` singleton.

    Loads the model on first call (slow - up to ~5 s plus an ~80 MB
    HuggingFace download). Subsequent calls return the cached instance
    in O(1). If ``model_name`` is supplied AND differs from the cached
    instance's, the cached instance is replaced so tests / settings
    changes can opt into a different model without a process restart.
    """
    global _embedder_singleton
    desired = model_name or _get_setting("embedding_model_name", _DEFAULT_MODEL)
    with _model_lock:
        existing = _embedder_singleton
        if existing is not None and existing.model_name == desired:
            return existing
        new_inst = Embedder(model_name=desired)
        _embedder_singleton = new_inst
        return new_inst


def reset_for_tests() -> None:
    """Clear the cached embedder.

    Tests should call this in fixture teardown so a model swap or a
    monkeypatched settings value applies cleanly. Production code never
    calls this - the singleton is meant to live for the process.
    """
    global _embedder_singleton
    with _model_lock:
        _embedder_singleton = None


# ── Similarity helpers ───────────────────────────────────────────────

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Return the cosine similarity of two 1-D vectors.

    Accepts un-normalized inputs (computes the norms inline). Returns
    ``0.0`` for either-side zero-norm so downstream sorts don't blow up
    on degenerate rows. Output is clamped to ``[-1.0, 1.0]`` to absorb
    floating-point drift past the theoretical bound.
    """
    av = np.asarray(a, dtype=np.float32).reshape(-1)
    bv = np.asarray(b, dtype=np.float32).reshape(-1)
    if av.shape != bv.shape:
        raise ValueError(
            f"cosine_similarity shape mismatch: {av.shape} vs {bv.shape}"
        )
    na = float(np.linalg.norm(av))
    nb = float(np.linalg.norm(bv))
    if na == 0.0 or nb == 0.0:
        return 0.0
    sim = float(np.dot(av, bv) / (na * nb))
    if sim > 1.0:
        return 1.0
    if sim < -1.0:
        return -1.0
    return sim


def cosine_similarity_matrix(query: np.ndarray, corpus: np.ndarray) -> np.ndarray:
    """Return the ``(M,)`` similarity vector between ``query`` and each row of
    ``corpus``.

    ``query`` may be 1-D (single embedding) or 2-D (``(K, dim)``); the
    return shape is ``(M,)`` for 1-D and ``(K, M)`` for 2-D. Inputs are
    normalized inline so the function works on either raw or pre-
    normalized embeddings - a one-row probe + a stored corpus matmul is
    the hot path for ``| nearest``.
    """
    q = np.asarray(query, dtype=np.float32)
    c = np.asarray(corpus, dtype=np.float32)
    if c.ndim != 2:
        raise ValueError(
            f"corpus must be 2-D (rows × dim), got shape {c.shape}"
        )
    if q.ndim == 1:
        if q.shape[0] != c.shape[1]:
            raise ValueError(
                f"query dim {q.shape[0]} != corpus dim {c.shape[1]}"
            )
        qn = q / (np.linalg.norm(q) or 1.0)
        cn = c / (np.linalg.norm(c, axis=1, keepdims=True) + 1e-12)
        return np.clip(cn @ qn, -1.0, 1.0).astype(np.float32)
    if q.ndim == 2:
        if q.shape[1] != c.shape[1]:
            raise ValueError(
                f"query dim {q.shape[1]} != corpus dim {c.shape[1]}"
            )
        qn = q / (np.linalg.norm(q, axis=1, keepdims=True) + 1e-12)
        cn = c / (np.linalg.norm(c, axis=1, keepdims=True) + 1e-12)
        return np.clip(qn @ cn.T, -1.0, 1.0).astype(np.float32)
    raise ValueError(f"query must be 1-D or 2-D, got shape {q.shape}")


__all__ = [
    "Embedder",
    "EmbeddingError",
    "EmbeddingResult",
    "MissingEmbeddingSDKError",
    "cosine_similarity",
    "cosine_similarity_matrix",
    "get_embedder",
    "reset_for_tests",
]

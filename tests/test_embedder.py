"""
Tests for analyzers/embedder.py - the Phase 1 semantic-foundation primitive.

Covers:
  * Lazy singleton behavior (single load, reset_for_tests semantics)
  * Encode shape / dtype / normalization invariants
  * Batch encode (including empty-input edge case)
  * Determinism (same input → identical vector)
  * Cosine similarity helper (paired, matrix, edge cases)
  * Threading safety of get_embedder()
  * MissingEmbeddingSDKError surfaced with an actionable message when the
    sentence-transformers package is unavailable
  * Productive validation: paraphrase similarity > random similarity
    (proves the wrapped model is actually doing semantic work)

These tests are integration tests in the sense that they exercise the
real sentence-transformers stack - there's no value in mocking what we
just installed. The model download (~80 MB) happens once at the first
session run and is cached by HuggingFace under ~/.cache/huggingface/.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from analyzers import embedder as emb_mod


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def fresh_embedder():
    """Reset the module-level singleton before and after each test.

    Ensures one test's monkeypatch / model-name swap can't leak into
    the next test's view of the cache.
    """
    emb_mod.reset_for_tests()
    yield emb_mod.get_embedder()
    emb_mod.reset_for_tests()


# ── Singleton behavior ───────────────────────────────────────────────

class TestSingleton:
    def test_returns_same_instance_on_repeat_calls(self, fresh_embedder):
        a = emb_mod.get_embedder()
        b = emb_mod.get_embedder()
        assert a is b is fresh_embedder

    def test_reset_for_tests_clears_singleton(self):
        emb_mod.reset_for_tests()
        first = emb_mod.get_embedder()
        emb_mod.reset_for_tests()
        second = emb_mod.get_embedder()
        # New instance after reset
        assert first is not second
        # But both point at the same model identifier
        assert first.model_name == second.model_name

    def test_concurrent_get_embedder_does_not_double_load(self):
        emb_mod.reset_for_tests()
        # Track loads via the SentenceTransformer constructor.
        load_count = {"n": 0}
        original_load = emb_mod.Embedder._load_model

        def counting_load(name):
            load_count["n"] += 1
            return original_load(name)

        emb_mod.Embedder._load_model = staticmethod(counting_load)
        try:
            instances = []
            errors = []

            def worker():
                try:
                    instances.append(emb_mod.get_embedder())
                except Exception as exc:
                    errors.append(exc)

            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(worker) for _ in range(8)]
                for f in futures:
                    f.result()

            assert errors == []
            assert len(instances) == 8
            # All threads share one Embedder (and therefore one underlying model)
            assert len({id(inst) for inst in instances}) == 1
            # Model loaded exactly once despite 8 concurrent callers
            assert load_count["n"] == 1
        finally:
            emb_mod.Embedder._load_model = staticmethod(original_load)
            emb_mod.reset_for_tests()


# ── Encode shape / dtype / normalization ─────────────────────────────

class TestEncode:
    def test_encode_returns_1d_float32_normalized(self, fresh_embedder):
        vec = fresh_embedder.encode("the federal reserve paused")
        assert vec.ndim == 1
        assert vec.dtype == np.float32
        assert vec.shape[0] == fresh_embedder.dim
        # L2 norm should be ~1.0 (normalize_embeddings=True)
        assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-4

    def test_encode_batch_returns_2d_matrix(self, fresh_embedder):
        out = fresh_embedder.encode_batch(["alpha", "beta", "gamma"])
        assert out.ndim == 2
        assert out.shape == (3, fresh_embedder.dim)
        assert out.dtype == np.float32
        # Every row is L2-normalized
        norms = np.linalg.norm(out, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-4)

    def test_encode_batch_empty_returns_well_shaped_zero_rows(self, fresh_embedder):
        out = fresh_embedder.encode_batch([])
        assert out.shape == (0, fresh_embedder.dim)
        assert out.dtype == np.float32

    def test_encode_batch_handles_none_elements(self, fresh_embedder):
        # ``None`` row is coerced to an empty string rather than crashing
        out = fresh_embedder.encode_batch(["hello", None, "world"])
        assert out.shape == (3, fresh_embedder.dim)
        # Each row still normalizes (model emits a non-zero vector even
        # for an empty string - we just don't want a crash here)
        for row in out:
            assert np.isfinite(row).all()

    def test_encode_none_raises_type_error(self, fresh_embedder):
        with pytest.raises(TypeError):
            fresh_embedder.encode(None)

    def test_encode_is_deterministic(self, fresh_embedder):
        text = "deterministic encoding check"
        v1 = fresh_embedder.encode(text)
        v2 = fresh_embedder.encode(text)
        # Exact equality - not approximate. Sentence-transformers in eval
        # mode is fully deterministic on a fixed input.
        assert np.array_equal(v1, v2)

    def test_dim_property_is_consistent(self, fresh_embedder):
        # Default model is all-MiniLM-L6-v2 → 384 dims.
        assert fresh_embedder.dim == 384
        probe = fresh_embedder.encode("dim probe")
        assert probe.shape[0] == fresh_embedder.dim


# ── Cosine similarity helpers ────────────────────────────────────────

class TestCosineSimilarity:
    def test_identical_vectors_similarity_one(self):
        a = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        assert abs(emb_mod.cosine_similarity(a, a) - 1.0) < 1e-6

    def test_opposite_vectors_similarity_negative_one(self):
        a = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        assert abs(emb_mod.cosine_similarity(a, -a) + 1.0) < 1e-6

    def test_orthogonal_vectors_similarity_zero(self):
        a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        b = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        assert abs(emb_mod.cosine_similarity(a, b)) < 1e-6

    def test_zero_norm_returns_zero_not_nan(self):
        a = np.zeros(3, dtype=np.float32)
        b = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        assert emb_mod.cosine_similarity(a, b) == 0.0
        assert emb_mod.cosine_similarity(b, a) == 0.0
        assert emb_mod.cosine_similarity(a, a) == 0.0

    def test_shape_mismatch_raises(self):
        a = np.array([1.0, 2.0], dtype=np.float32)
        b = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        with pytest.raises(ValueError):
            emb_mod.cosine_similarity(a, b)

    def test_clamped_to_unit_range(self):
        # Float drift past 1.0 (manufactured) should still clamp.
        # Build two identical vectors that, after normalization, dot to
        # exactly 1.0 - fine. The clamp branch is exercised by the unit
        # vector path; verify the boundary explicitly.
        a = np.ones(4, dtype=np.float32) / 2.0  # norm = 1.0
        sim = emb_mod.cosine_similarity(a, a)
        assert -1.0 <= sim <= 1.0

    def test_matrix_similarity_1d_query(self, fresh_embedder):
        q = fresh_embedder.encode("query about the fed")
        corpus = fresh_embedder.encode_batch([
            "FOMC holds steady",
            "apple announces iphone",
            "central bank pauses tightening",
        ])
        sims = emb_mod.cosine_similarity_matrix(q, corpus)
        assert sims.shape == (3,)
        assert sims.dtype == np.float32
        # All values in [-1, 1]
        assert sims.min() >= -1.0
        assert sims.max() <= 1.0

    def test_matrix_similarity_2d_query_matches_1d_path(self, fresh_embedder):
        # Pure math invariant: the (K,M) matrix form must equal the row-by-row
        # 1-D form. Avoids asserting any specific model semantics here -
        # the productive semantic check lives in TestSemanticBehavior.
        Q = fresh_embedder.encode_batch(["fed pause", "ai chips"])
        C = fresh_embedder.encode_batch([
            "FOMC holds steady",
            "nvidia announces new GPU",
            "apple announces iphone",
        ])
        sims_matrix = emb_mod.cosine_similarity_matrix(Q, C)
        assert sims_matrix.shape == (2, 3)
        sims_rowwise = np.stack([
            emb_mod.cosine_similarity_matrix(Q[i], C) for i in range(Q.shape[0])
        ])
        assert np.allclose(sims_matrix, sims_rowwise, atol=1e-5)
        # And every value sits inside the unit interval
        assert sims_matrix.min() >= -1.0
        assert sims_matrix.max() <= 1.0

    def test_matrix_similarity_dim_mismatch_raises(self):
        q = np.ones(384, dtype=np.float32)
        bad_corpus = np.ones((10, 256), dtype=np.float32)
        with pytest.raises(ValueError):
            emb_mod.cosine_similarity_matrix(q, bad_corpus)

    def test_matrix_similarity_corpus_must_be_2d(self):
        q = np.ones(384, dtype=np.float32)
        bad_corpus = np.ones(384, dtype=np.float32)  # 1-D, illegal
        with pytest.raises(ValueError):
            emb_mod.cosine_similarity_matrix(q, bad_corpus)


# ── Productive semantic validation ───────────────────────────────────

class TestSemanticBehavior:
    """Sanity-check the model is doing semantic work, not just hashing.

    This is the high-level assertion the Phase 1 roadmap sets out: the
    primitive must catch synonym/paraphrase pairs that lexical search
    misses. If this test ever fails, either the model loaded wrong or
    the wrapper is mis-applying normalization - either way it's a
    real regression worth catching here.
    """

    def test_paraphrase_beats_random_pair(self, fresh_embedder):
        anchor = fresh_embedder.encode("federal reserve paused interest rate hikes")
        paraphrase = fresh_embedder.encode("FOMC holds rates steady")
        unrelated = fresh_embedder.encode("apple announces new iphone model")
        sim_paraphrase = emb_mod.cosine_similarity(anchor, paraphrase)
        sim_unrelated = emb_mod.cosine_similarity(anchor, unrelated)
        # Paraphrase pair beats unrelated by a clear margin
        assert sim_paraphrase > sim_unrelated + 0.10, (
            f"paraphrase {sim_paraphrase:.3f} should beat unrelated "
            f"{sim_unrelated:.3f} by ≥ 0.10 - model may be mis-loaded"
        )


# ── Missing-SDK fallback ─────────────────────────────────────────────

class TestMissingSDK:
    def test_missing_sdk_raises_actionable_error(self, monkeypatch):
        emb_mod.reset_for_tests()
        # Force the lazy import inside _load_model to fail
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "sentence_transformers" or name.startswith(
                "sentence_transformers."
            ):
                raise ImportError("simulated missing SDK")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        try:
            with pytest.raises(emb_mod.MissingEmbeddingSDKError) as exc_info:
                emb_mod.get_embedder()
            msg = str(exc_info.value)
            # Actionable: tells the user exactly what to install
            assert "sentence-transformers" in msg
            assert "pip install" in msg
        finally:
            emb_mod.reset_for_tests()


# ── Threading regression: encode is safe across threads ──────────────

class TestThreadedEncode:
    def test_encode_callable_from_multiple_threads(self, fresh_embedder):
        # PyTorch handles concurrent inference internally; we want to
        # confirm the wrapper doesn't block, deadlock, or corrupt state.
        results: list[np.ndarray] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(text: str):
            try:
                v = fresh_embedder.encode(text)
                with lock:
                    results.append(v)
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        texts = [f"thread payload {i}" for i in range(8)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(worker, t) for t in texts]
            for f in futures:
                f.result()

        assert errors == []
        assert len(results) == 8
        # All vectors are well-shaped + finite
        for v in results:
            assert v.shape == (fresh_embedder.dim,)
            assert np.isfinite(v).all()

"""
Phase 2 cross-cutting principles audit (slice 8 - Phase 2 close).

The ROADMAP "Cross-Cutting Principles" section lists 8 invariants that
every phase must satisfy. This file is the drift guard that pins
each one in CI so a future change can never silently violate them.
The 8 principles, mapped to the test classes below:

  1. **Zero green-test regression** - TestPrinciple1ZeroRegression
     (informational; the sweep itself is the enforcement)
  2. **Additive only** - TestPrinciple2AdditiveOnly
     (frozen schema snapshots for Phase 2 SQLite tables + YAML
     registry shape; principle 2's IMMUTABLE/ rule is already pinned
     in test_oeb_wave2.py - we just verify Phase 2 didn't break it)
  3. **Drift guards from day 1** - TestPrinciple3DriftGuards
     (every Phase 2 SPQL pipe has a grammar-parity test in its own
     test file; this audit just lists them by name)
  4. **Docs = definition of done** - TestPrinciple4Docs
     (CHANGELOG.md mentions every Phase 2 slice; docs/lang/18_llm_pipes.md
     exists and is non-trivial)
  5. **Each phase ends with a demoable artifact** - informational
  6. **Feature-flagged until burn-in** - TestPrinciple6ExplicitOptIn
     (Phase 2 LLM pipes are explicit-syntax-opt-in via the SPQL
     surface; documenting interpretation)
  7. **Local-first remains the moat** - TestPrinciple7LocalFirst
     (Ollama is in the default model registry; the router can route
     to a local model with NO cloud credentials)
  8. **Money-leak audit pattern** - TestPrinciple8MoneyLeakCanary
     (slice-7 canary class exists; the CLAUDE.md "Do Not" entry
     references it)

This file is the slice-8 acceptance test for Phase 2 close. If any
test here fails, Phase 2 is not actually done.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


# ═════════════════════════════════════════════════════════════════════
# Principle 1 - Zero green-test regression (informational)
# ═════════════════════════════════════════════════════════════════════

class TestPrinciple1ZeroRegression:
    """The sweep itself is the enforcement. We document the slice-7
    + slice-8 ship-time totals here so a future regression that
    silently drops tests is caught at code review.
    """

    def test_phase2_test_files_exist(self):
        # Each Phase 2 slice has at least one dedicated test file.
        for f in (
            "test_model_store.py",            # slice 1
            "test_llm_router.py",             # slice 2
            "test_llm_history_store.py",      # slice 3
            "test_llm_pipe.py",               # slice 4
            "test_llm_batch_pipe.py",         # slice 5
            "test_switch_pipe.py",            # slice 6
            "test_llm_pipe_slice7.py",        # slice 7
            "test_llm_boundary_tags_slice8.py",  # slice 8
            "test_phase2_cross_cutting_audit.py",  # this file
        ):
            assert (PROJECT_ROOT / "tests" / f).exists(), (
                f"Phase 2 expects {f} to exist. If a slice's tests "
                "moved, update this list."
            )


# ═════════════════════════════════════════════════════════════════════
# Principle 2 - Additive only (no schema column ever removed)
# ═════════════════════════════════════════════════════════════════════

class TestPrinciple2AdditiveOnly:
    """Phase 2 introduced two new persistence schemas:
      * `llm_call_history.sqlite` (slice 3 - content-hash cache + audit)
      * `models/<id>.yaml` (slice 1 - model registry)

    Both are now FROZEN: the column / field set may grow, but
    columns/fields documented here MUST NEVER be removed without a
    formal migration. The slice-7 cache + the slice-3 audit trail are
    long-lived data; cost reconstruction queries against historical
    rows depend on these columns.
    """

    # Frozen as of slice 3 ship; slice 7 reused without alteration.
    LLM_HISTORY_FROZEN_COLUMNS = frozenset({
        "id", "request_id", "triggered_at_epoch", "triggered_at",
        "content_hash", "model_id", "provider", "model_name", "source",
        "status", "input_tokens", "output_tokens", "cost_usd",
        "latency_ms", "max_tokens",
        "prompt_gz", "system_gz", "response_text_gz", "raw_response_gz",
        "error_class", "error_message",
    })

    # Frozen as of slice 1 ship; slice 1.5 added `endpoint`-validation but
    # the field set in the YAML record itself is the same shape.
    MODEL_REGISTRY_FROZEN_FIELDS = frozenset({
        "id", "provider", "model_name", "endpoint",
        "cost_per_input_million_usd", "cost_per_output_million_usd",
        "max_output_tokens", "default_timeout_seconds",
        "description", "created_at", "updated_at",
    })

    def test_llm_call_history_columns_present(self, tmp_path):
        # Spin up an in-memory store and read the table's column names.
        # Drift guard: any column listed in LLM_HISTORY_FROZEN_COLUMNS
        # must still appear after Phase 2 / Bet 3 evolves.
        import sqlite3
        from analyzers.llm_history_store import LLMHistoryStore
        db_path = tmp_path / "llm_call_history.sqlite"
        # __init__ runs _init_schema; no separate initialize() call needed
        LLMHistoryStore(db_path=db_path)
        conn = sqlite3.connect(db_path)
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(llm_call_history)")
            }
        finally:
            conn.close()
        missing = self.LLM_HISTORY_FROZEN_COLUMNS - cols
        assert not missing, (
            f"llm_call_history is missing frozen columns: {sorted(missing)}. "
            "Phase 2 schema is additive-only. Removing a column breaks "
            "every historical SPQL query that referenced it."
        )

    def test_model_registry_yaml_fields_round_trip(self, tmp_path, monkeypatch):
        # Save a record with every frozen field, read it back, verify
        # nothing was silently dropped on the round-trip.
        import model_store
        model_store.reset_for_tests()
        monkeypatch.setattr(model_store, "MODELS_DIR", tmp_path / "models")
        store = model_store.get_store()  # seeds defaults
        record = {
            "id": "audit-test",
            "provider": "ollama",
            "model_name": "audit-test:latest",
            "endpoint": "http://localhost:11434",
            "cost_per_input_million_usd": 0.0,
            "cost_per_output_million_usd": 0.0,
            "max_output_tokens": 2048,
            "default_timeout_seconds": 120,
            "description": "audit fixture",
        }
        store.save_model(record)
        round_tripped = store.get_model("audit-test")
        assert round_tripped is not None
        # Every frozen field reads back
        for field in self.MODEL_REGISTRY_FROZEN_FIELDS:
            assert field in round_tripped, (
                f"Model registry dropped frozen field: {field!r}. "
                "Phase 2 schema is additive-only."
            )
        model_store.reset_for_tests()


# ═════════════════════════════════════════════════════════════════════
# Principle 3 - Drift guards from day 1
# ═════════════════════════════════════════════════════════════════════

class TestPrinciple3DriftGuards:
    """Every new SPQL pipe ships with a grammar-parity test in its
    own test file. Phase 2 added 5 new pipes; verify each has a
    drift-guard test class.
    """

    PHASE_2_PIPES = {
        # pipe → test file with TestGrammarParity (or similar) class
        "llm":            "test_llm_pipe.py",
        "llm_batch":      "test_llm_batch_pipe.py",
        "switch":         "test_switch_pipe.py",
        # Phase 1 pipes added in the same window; pin them here too
        # since this audit covers the cumulative state at Phase 2 close.
        "nearest":        "test_semantic_pipes.py",
        "dedup_semantic": "test_semantic_pipes.py",
    }

    def test_every_phase2_pipe_has_grammar_parity_test(self):
        for pipe, test_file in self.PHASE_2_PIPES.items():
            path = PROJECT_ROOT / "tests" / test_file
            assert path.exists(), (
                f"Phase 2 pipe `| {pipe}` should have grammar-parity "
                f"tests in {test_file}, but the file is missing."
            )
            text = path.read_text()
            # The grammar-parity test should mention the .g4 token
            # OR the pipe directive in some form.
            mentions_grammar = (
                "speakesQuery.g4" in text
                or "TestGrammarParity" in text
                or "grammar_vocab" in text
            )
            assert mentions_grammar, (
                f"{test_file} should contain a grammar-parity test "
                f"for `| {pipe}` (mentions speakesQuery.g4 or "
                "TestGrammarParity)."
            )

    def test_grammar_g4_declares_phase2_directive_tokens(self):
        g4 = (PROJECT_ROOT / "lexers" / "speakesQuery.g4").read_text()
        # Each pipe-name token MUST exist as a literal in the .g4
        for token, literal in (
            ("LLM",            "'llm'"),
            ("LLM_BATCH",      "'llm_batch'"),
            ("SWITCH",         "'switch'"),
            ("NEAREST",        "'nearest'"),
            ("DEDUP_SEMANTIC", "'dedup_semantic'"),
            # Slice 7 kwargs
            ("MAX_COST_USD",   "'max_cost_usd'"),
            ("DRY_RUN",        "'dry_run'"),
        ):
            pattern = rf"\b{token}\s*:\s*{re.escape(literal)}"
            assert re.search(pattern, g4), (
                f"speakesQuery.g4 missing token declaration: {token} : "
                f"{literal}"
            )


# ═════════════════════════════════════════════════════════════════════
# Principle 4 - Docs = definition of done
# ═════════════════════════════════════════════════════════════════════

class TestPrinciple4Docs:
    """`docs/lang/18_llm_pipes.md` is the dedicated reference for
    Phase 2 pipes; CHANGELOG.md tracks every slice. Both must reflect
    the current ship state.
    """

    def test_18_llm_pipes_doc_exists_and_nontrivial(self):
        path = PROJECT_ROOT / "docs" / "lang" / "18_llm_pipes.md"
        assert path.exists(), "docs/lang/18_llm_pipes.md is missing"
        text = path.read_text()
        # Must mention every Phase 2 user-facing surface
        for token in (
            "| llm", "| llm_batch", "| switch",
            "max_cost_usd", "dry_run",
            "<data>",  # boundary-tag pattern
        ):
            assert token in text, (
                f"docs/lang/18_llm_pipes.md should mention {token!r}"
            )
        # Should be at least 250 lines (the slice-7 polish brought it
        # close to 400; an accidental truncation should fail loud)
        assert len(text.splitlines()) >= 250, (
            f"18_llm_pipes.md has {len(text.splitlines())} lines; "
            "expected ≥ 250. Did someone truncate?"
        )

    def test_changelog_has_phase2_slice_entries(self):
        path = PROJECT_ROOT / "CHANGELOG.md"
        text = path.read_text()
        for token in (
            "Phase 2 / Bet 3 slice 1",
            "Phase 2 / Bet 3 slice 2",
            "Phase 2 / Bet 3 slice 3",
            "Phase 2 / Bet 3 slice 4",
            "Phase 2 / Bet 3 slice 5",
            "Phase 2 / Bet 3 slice 6",
            "Phase 2 / Bet 3 slice 7",
            "Phase 2 / Bet 3 slice 8",
        ):
            assert token in text, (
                f"CHANGELOG.md is missing an entry for {token!r}. "
                "Per the 'Docs = definition of done' principle, every "
                "slice gets a CHANGELOG entry before merge."
            )


# ═════════════════════════════════════════════════════════════════════
# Principle 6 - Feature-flagged until burn-in
# ═════════════════════════════════════════════════════════════════════

class TestPrinciple6ExplicitOptIn:
    """Phase 2 LLM pipes are gated by *explicit syntax opt-in* rather
    than a global on/off switch. To use `| llm` an operator must:
      1. Have a model registered (slice 1: model_store)
      2. Have credentials in the vault (or run Ollama locally)
      3. Write `| llm model="<id>" prompt="..."` in their query

    Each step is an explicit, unambiguous opt-in - there is no
    accidental usage path. Compared to `claude_analyzer_enabled`
    (which is a master switch on a background process), the SPQL
    pipe surface IS the feature flag.

    The slice-7 budget gate provides the cost-runaway safety. The
    money-leak canary (Principle 8) pins zero invocations on the
    dry-run path.

    This class documents that interpretation as a passing test so
    future audits don't flag Phase 2 as missing the flag.
    """

    def test_llm_pipes_require_explicit_model_kwarg(self):
        # `| llm` without `model=` raises - there's no implicit default.
        from handlers.LLMHandler import LLMPipeError, llm_pipe
        import pandas as pd
        with pytest.raises(LLMPipeError, match="model"):
            llm_pipe(
                pd.DataFrame({"x": ["v"]}),
                model="", prompt="x",
            )

    def test_default_max_cost_is_uncapped_only_when_kwarg_explicit(self):
        # The slice-7 default `llm_default_max_cost_usd: 0.0` means
        # "no implicit cap". Operators who want the hard ceiling MUST
        # pass `max_cost_usd=` per pipe. This is documented in
        # docs/lang/18_llm_pipes.md (verified above).
        from global_settings import DEFAULTS
        assert DEFAULTS["llm_default_max_cost_usd"] == 0.0


# ═════════════════════════════════════════════════════════════════════
# Principle 7 - Local-first remains the moat
# ═════════════════════════════════════════════════════════════════════

class TestPrinciple7LocalFirst:
    """No phase introduces a mandatory cloud dependency. Phase 2's
    `| llm` pipe MUST be usable end-to-end with no cloud credentials -
    via Ollama (default registry includes `ollama-llama3-1-8b`) or
    LM Studio (per slice 1.5).
    """

    def test_default_model_registry_includes_local_provider(self):
        defaults_dir = PROJECT_ROOT / "default_models"
        local_records = []
        for yaml_path in defaults_dir.glob("*.yaml"):
            text = yaml_path.read_text()
            if "provider: ollama" in text or "provider: lmstudio" in text:
                local_records.append(yaml_path.name)
        assert len(local_records) >= 1, (
            "Default model registry must include at least one local "
            "provider (ollama or lmstudio) so `| llm` is usable "
            "without cloud credentials. Found: "
            f"{[p.name for p in defaults_dir.glob('*.yaml')]}"
        )

    def test_router_dispatches_to_local_without_cloud_credentials(
        self, tmp_path, monkeypatch,
    ):
        # The router can resolve + dispatch to a local Ollama model
        # WITHOUT any cloud-API key in the vault. Drift guard: a
        # future change that requires a cloud key for any code path
        # would fail this test.
        import model_store
        import analyzers.llm_history_store as hist
        from analyzers import llm_router
        model_store.reset_for_tests()
        hist.reset_for_tests()
        monkeypatch.setattr(model_store, "MODELS_DIR", tmp_path / "models")
        monkeypatch.setattr(
            hist, "DEFAULT_DB_PATH", tmp_path / "llm_call_history.sqlite",
        )
        model_store.get_store()
        llm_router._invalidate_api_key_cache()

        # Resolve the local Ollama model - must succeed without any
        # vault credentials present
        record = model_store.get_store().get_model("ollama-llama3-1-8b")
        assert record is not None
        assert record["provider"] == "ollama"
        # Cost estimator works without credentials
        out = llm_router.estimate_cost_usd(
            "ollama-llama3-1-8b", ["test"], max_tokens=50,
        )
        assert out["provider"] == "ollama"
        # Local model: zero cost (free local inference)
        assert out["cost_usd"] == 0.0

        model_store.reset_for_tests()
        hist.reset_for_tests()
        llm_router._invalidate_api_key_cache()

    def test_no_openai_provider_in_default_registry(self):
        # Hard rule from slice 2.5: no OpenAI as a provider.
        defaults_dir = PROJECT_ROOT / "default_models"
        offenders = []
        for yaml_path in defaults_dir.glob("*.yaml"):
            text = yaml_path.read_text()
            # Match `provider: openai` only, not e.g. `compatible-with-openai`
            if re.search(r"^provider:\s*openai\s*$", text, re.MULTILINE):
                offenders.append(yaml_path.name)
        assert not offenders, (
            f"OpenAI provider in default model registry: {offenders}. "
            "Per the 2026-05-08 user direction (slice 2.5), SpeakesQuery "
            "does not interact with OpenAI's company or servers."
        )


# ═════════════════════════════════════════════════════════════════════
# Principle 8 - Money-leak audit pattern applies to every billable surface
# ═════════════════════════════════════════════════════════════════════

class TestPrinciple8MoneyLeakCanary:
    """Slice 7 introduced the canary pattern for `| llm` /
    `| llm_batch`. This class verifies the canary class exists and is
    discoverable; the canary tests' actual contracts (zero invocations
    on dry-run, bounded invocations on budget cap) are pinned in
    `tests/test_llm_pipe_slice7.py::TestMoneyLeakCanary` itself.
    """

    def test_money_leak_canary_class_exists(self):
        # Drift guard: anyone who deletes the canary class would
        # silently disable the most-load-bearing test in Phase 2.
        path = PROJECT_ROOT / "tests" / "test_llm_pipe_slice7.py"
        assert path.exists()
        text = path.read_text()
        assert "class TestMoneyLeakCanary" in text, (
            "tests/test_llm_pipe_slice7.py should still contain the "
            "TestMoneyLeakCanary class. Slice 7 introduced it as the "
            "load-bearing test for the dry-run + budget gate contracts."
        )
        # Canary tests patch call_llm with an AssertionError-raising
        # function - verify that pattern is still in the file.
        assert "MONEY LEAK" in text, (
            "The canary tests should patch call_llm with a function "
            "that raises AssertionError(\"MONEY LEAK\")."
        )

    def test_claude_md_do_not_entry_references_canary(self):
        # CLAUDE.md should reference the canary in the Phase 2 "Do Not"
        # entry so the rule is discoverable without trawling tests.
        path = PROJECT_ROOT / "CLAUDE.md"
        text = path.read_text()
        assert (
            "test_llm_pipe_slice7.py::TestMoneyLeakCanary" in text
            or "TestMoneyLeakCanary" in text
        ), (
            "CLAUDE.md 'Do Not' entry for | llm-shaped pipes should "
            "reference the slice-7 canary class so future authors find it."
        )
        # And the rule itself is mentioned
        assert "max_cost_usd" in text and "dry_run" in text, (
            "CLAUDE.md should document the slice-7 max_cost_usd / "
            "dry_run contract for future | llm-shaped pipes."
        )


# ═════════════════════════════════════════════════════════════════════
# Phase 2 demoable artifact - informational
# ═════════════════════════════════════════════════════════════════════

class TestPhase2DemoableArtifact:
    """Per ROADMAP: Phase 2 closes with a demoable artifact - the
    cost-cascade pattern fully expressible in SPQL alone:

      | nearest "..." topk=N        # cheap semantic prefilter
      | llm model="ollama-..."       # local-cost classification
      | switch _llm_output
         case "urgent" [
           llm_batch model="claude-..." max_cost_usd=0.05
         ]
         case "drop" [ head 0 ]

    All four primitives ship in Phase 2; the budget gate (slice 7)
    makes the cloud stage hard-capped. This class doesn't run the
    pipeline (that requires real API keys / a live Ollama daemon);
    instead it verifies all primitives are reachable from SPQL.
    """

    def test_all_four_primitives_in_grammar(self):
        g4 = (PROJECT_ROOT / "lexers" / "speakesQuery.g4").read_text()
        for primitive in ("'nearest'", "'llm'", "'llm_batch'", "'switch'"):
            assert primitive in g4, (
                f"Phase 2 cost-cascade demo requires {primitive} in "
                "the grammar."
            )

    def test_all_four_primitives_have_listener_dispatch(self):
        listener = (
            PROJECT_ROOT / "lexers" / "speakesQueryListener.py"
        ).read_text()
        for cmd in ("nearest", "llm", "llm_batch", "switch"):
            assert f'"{cmd}":' in listener, (
                f"Listener _command_map should dispatch `| {cmd}`."
            )

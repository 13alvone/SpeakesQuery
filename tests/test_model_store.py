"""
Tests for model_store.py + validation/ModelValidation.py - Phase 2 / Bet 3 slice 1.

Covers:
  * Validation: id format, provider enum, costs ≥ 0, positive ints
  * CRUD: save / get / update / delete / list (sorted)
  * Seed defaults: missing-only, never overwrites user edits
  * install_default: single-id install, refuses overwrite by default
  * list_default_ids: matches what's shipped under default_models/
  * Singleton: get_store() returns the same instance, reset_for_tests
    clears it
  * Default YAML schema: every shipped default validates cleanly
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from validation.ModelValidation import ModelValidation
from model_store import (
    DEFAULTS_DIR,
    ModelStore,
    get_store,
    reset_for_tests,
)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def isolated_store(tmp_path):
    """Fresh ModelStore pointed at a tmp dir; defaults dir = real one
    (so seed tests exercise the actual shipped templates).
    """
    store = ModelStore()
    store._dir = tmp_path / "models"
    store.initialize()
    return store


@pytest.fixture
def empty_store(tmp_path):
    """Fresh ModelStore with NO defaults seeded (uses an empty defaults
    dir). For tests that want a blank slate.
    """
    store = ModelStore()
    store._dir = tmp_path / "models"
    store._defaults_dir = tmp_path / "default_models_empty"
    (tmp_path / "default_models_empty").mkdir()
    store.initialize()
    return store


# ── Validation ───────────────────────────────────────────────────────

class TestModelValidation:
    def test_validate_id_accepts_canonical(self):
        assert ModelValidation.validate_id("claude-sonnet-4-6") == "claude-sonnet-4-6"
        assert ModelValidation.validate_id("ollama-llama3-1-8b") == "ollama-llama3-1-8b"
        # Dot-segment allowed for version markers like model-name.v2
        assert ModelValidation.validate_id("lmstudio-llama3.v2") == "lmstudio-llama3.v2"

    def test_validate_id_rejects_uppercase(self):
        with pytest.raises(ValueError, match="lowercase"):
            ModelValidation.validate_id("Claude-Sonnet-4-6")

    def test_validate_id_rejects_spaces(self):
        with pytest.raises(ValueError):
            ModelValidation.validate_id("claude sonnet 4 6")

    def test_validate_id_rejects_empty(self):
        with pytest.raises(ValueError):
            ModelValidation.validate_id("")
        with pytest.raises(ValueError):
            ModelValidation.validate_id("   ")

    def test_validate_provider_enum(self):
        # All four Phase 2 providers must be accepted (slice 2.5 removed
        # `openai` per user direction - SpeakesQuery does not interact
        # with OpenAI's company or servers).
        for ok in ("anthropic", "ollama", "gemini", "lmstudio"):
            assert ModelValidation.validate_provider(ok) == ok
        # Case is normalised to lower
        assert ModelValidation.validate_provider("Anthropic") == "anthropic"
        assert ModelValidation.validate_provider("LMStudio") == "lmstudio"

    def test_openai_provider_is_rejected(self):
        # Drift guard for slice 2.5: SpeakesQuery deliberately does not
        # support OpenAI as a provider. If a future change accidentally
        # re-introduces `openai` to ALLOWED_PROVIDERS, this test fails.
        with pytest.raises(ValueError, match="Unknown provider"):
            ModelValidation.validate_provider("openai")

    def test_validate_provider_rejects_unknown(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            ModelValidation.validate_provider("grok")

    def test_validate_endpoint_optional(self):
        assert ModelValidation.validate_endpoint(None) == ""
        assert ModelValidation.validate_endpoint("") == ""
        assert ModelValidation.validate_endpoint("http://localhost:11434") == "http://localhost:11434"

    def test_validate_endpoint_must_be_http(self):
        with pytest.raises(ValueError, match="http"):
            ModelValidation.validate_endpoint("localhost:11434")

    def test_validate_cost_non_negative(self):
        assert ModelValidation.validate_cost(0.0, name="cost_in") == 0.0
        assert ModelValidation.validate_cost(3.5, name="cost_in") == 3.5
        with pytest.raises(ValueError, match="non-negative"):
            ModelValidation.validate_cost(-0.1, name="cost_in")

    def test_validate_cost_rejects_bool(self):
        # bool is subclass of int - must be rejected explicitly
        with pytest.raises(ValueError):
            ModelValidation.validate_cost(True, name="cost_in")

    def test_validate_record_normalises_with_defaults(self):
        rec = ModelValidation.validate_record({
            "id": "x",
            "provider": "anthropic",
            "model_name": "y",
        })
        assert rec["max_output_tokens"] == 4096
        assert rec["default_timeout_seconds"] == 120
        assert rec["cost_per_input_million_usd"] == 0.0
        assert rec["endpoint"] == ""

    def test_validate_record_rejects_missing_required(self):
        with pytest.raises(ValueError):
            ModelValidation.validate_record({"id": "x", "provider": "anthropic"})  # no model_name


# ── Slice 1.5: self-hosted-server endpoint requirement ──────────────

class TestEndpointRequirement:
    """``ollama`` and ``lmstudio`` providers REQUIRE a non-empty endpoint
    - there's no SDK default for a self-hosted server. Cloud providers
    (anthropic / openai / gemini) accept an empty endpoint and use their
    SDK default. Caught at save-time so the operator sees the error
    before they try to use the model.
    """

    def test_lmstudio_requires_endpoint(self):
        with pytest.raises(ValueError, match="non-empty endpoint"):
            ModelValidation.validate_record({
                "id": "lms-broken", "provider": "lmstudio",
                "model_name": "local-model",
                # endpoint omitted → should fail
            })

    def test_lmstudio_with_endpoint_validates(self):
        rec = ModelValidation.validate_record({
            "id": "lms-ok", "provider": "lmstudio",
            "model_name": "local-model",
            "endpoint": "http://192.168.1.50:1234/v1",
        })
        assert rec["provider"] == "lmstudio"
        assert rec["endpoint"] == "http://192.168.1.50:1234/v1"

    def test_ollama_requires_endpoint(self):
        with pytest.raises(ValueError, match="non-empty endpoint"):
            ModelValidation.validate_record({
                "id": "ollama-broken", "provider": "ollama",
                "model_name": "llama3.1:8b",
                # endpoint omitted → should fail
            })

    def test_anthropic_endpoint_optional(self):
        # No endpoint set - fine, SDK default applies
        rec = ModelValidation.validate_record({
            "id": "claude-x", "provider": "anthropic",
            "model_name": "claude-haiku-4-5-20251001",
        })
        assert rec["endpoint"] == ""

    def test_gemini_endpoint_optional(self):
        # Cloud provider, SDK has its own default endpoint resolution.
        rec = ModelValidation.validate_record({
            "id": "gem-x", "provider": "gemini",
            "model_name": "gemini-1.5-pro",
        })
        assert rec["endpoint"] == ""

    def test_providers_requiring_endpoint_constant_is_correct(self):
        # Drift guard - if a future provider gets added that ALSO needs
        # an endpoint, this test forces the maintainer to think about it.
        assert ModelValidation.PROVIDERS_REQUIRING_ENDPOINT == frozenset({
            "ollama", "lmstudio",
        })

    # ── sampling block (2026-06-07: Qwen3.5-122B repoint) ──────────────

    def test_validate_sampling_none_is_empty(self):
        # Absent field → {} so every existing model keeps server-default
        # sampling, unchanged from before the field existed.
        assert ModelValidation.validate_sampling(None) == {}

    def test_validate_sampling_passes_allowed_keys(self):
        block = {"temperature": 1.0, "top_p": 0.95, "top_k": 20,
                 "min_p": 0, "presence_penalty": 1.5}
        assert ModelValidation.validate_sampling(block) == block

    def test_validate_sampling_rejects_unknown_key(self):
        # Typos must fail loud at save-time, not be silently dropped.
        with pytest.raises(ValueError, match="Unknown sampling key"):
            ModelValidation.validate_sampling({"presnce_penalty": 1.5})

    def test_validate_sampling_rejects_bool(self):
        with pytest.raises(ValueError, match="must be a number"):
            ModelValidation.validate_sampling({"temperature": True})

    def test_validate_sampling_rejects_non_numeric(self):
        with pytest.raises(ValueError, match="must be a number"):
            ModelValidation.validate_sampling({"temperature": "hot"})

    def test_validate_sampling_rejects_non_dict(self):
        with pytest.raises(ValueError, match="must be a dict"):
            ModelValidation.validate_sampling([1, 2, 3])

    def test_validate_record_includes_sampling(self):
        rec = ModelValidation.validate_record({
            "id": "x", "provider": "lmstudio", "model_name": "m",
            "endpoint": "http://h:8085/v1",
            "sampling": {"presence_penalty": 1.5},
        })
        assert rec["sampling"] == {"presence_penalty": 1.5}

    def test_validate_record_sampling_defaults_empty(self):
        rec = ModelValidation.validate_record({
            "id": "x", "provider": "anthropic", "model_name": "m",
        })
        assert rec["sampling"] == {}


# ── CRUD ─────────────────────────────────────────────────────────────

class TestCRUD:
    def test_save_and_get_round_trip(self, empty_store):
        rec = empty_store.save_model({
            "id": "test-model",
            "provider": "gemini",
            "model_name": "gemini-1.5-pro",
            "cost_per_input_million_usd": 2.5,
            "cost_per_output_million_usd": 10.0,
        })
        assert rec["id"] == "test-model"
        assert rec["created_at"] == rec["updated_at"]
        loaded = empty_store.get_model("test-model")
        assert loaded["id"] == "test-model"
        assert loaded["cost_per_input_million_usd"] == 2.5

    def test_sampling_block_round_trips_through_save(self, empty_store):
        # An operator-pinned sampling block must survive the normalising
        # save path (validate_record) - otherwise editing a model would
        # silently strip the presence_penalty that keeps a reasoning
        # model's <think> trace from looping (Qwen3.5-122B, 2026-06-07).
        empty_store.save_model({
            "id": "big-local", "provider": "lmstudio",
            "model_name": "Qwen3.5-122B-A10B",
            "endpoint": "http://llama-host:8085/v1",
            "sampling": {"presence_penalty": 1.5, "temperature": 1.0},
        })
        loaded = empty_store.get_model("big-local")
        assert loaded["sampling"] == {
            "presence_penalty": 1.5, "temperature": 1.0,
        }

    def test_save_refuses_overwrite_by_default(self, empty_store):
        empty_store.save_model({
            "id": "dup", "provider": "anthropic", "model_name": "x",
        })
        with pytest.raises(FileExistsError):
            empty_store.save_model({
                "id": "dup", "provider": "anthropic", "model_name": "y",
            })

    def test_save_overwrite_true_replaces(self, empty_store):
        empty_store.save_model({
            "id": "dup", "provider": "anthropic", "model_name": "x",
        })
        empty_store.save_model({
            "id": "dup", "provider": "anthropic", "model_name": "y",
        }, overwrite=True)
        loaded = empty_store.get_model("dup")
        assert loaded["model_name"] == "y"

    def test_update_merges_partial(self, empty_store):
        empty_store.save_model({
            "id": "u", "provider": "anthropic", "model_name": "x",
            "cost_per_input_million_usd": 1.0,
        })
        updated = empty_store.update_model("u", {"description": "new desc"})
        assert updated["description"] == "new desc"
        # Untouched fields preserved
        assert updated["cost_per_input_million_usd"] == 1.0
        assert updated["model_name"] == "x"

    def test_update_cannot_change_id(self, empty_store):
        empty_store.save_model({
            "id": "original", "provider": "anthropic", "model_name": "x",
        })
        # Patch tries to change id; merge path strips it. The on-disk
        # file's id stays the same.
        updated = empty_store.update_model(
            "original", {"id": "renamed", "description": "x"},
        )
        assert updated["id"] == "original"
        assert empty_store.get_model("renamed") is None

    def test_update_missing_raises(self, empty_store):
        with pytest.raises(FileNotFoundError):
            empty_store.update_model("nope", {"description": "x"})

    def test_get_returns_none_for_unknown(self, empty_store):
        assert empty_store.get_model("does-not-exist") is None

    def test_get_returns_none_for_invalid_id_chars(self, empty_store):
        # Filename traversal / illegal chars never reach disk
        assert empty_store.get_model("../etc/passwd") is None
        assert empty_store.get_model("ID WITH SPACES") is None

    def test_delete_returns_true_on_success(self, empty_store):
        empty_store.save_model({
            "id": "del-me", "provider": "anthropic", "model_name": "x",
        })
        assert empty_store.delete_model("del-me") is True
        assert empty_store.get_model("del-me") is None

    def test_delete_returns_false_for_unknown(self, empty_store):
        assert empty_store.delete_model("ghost") is False

    def test_list_models_sorted_by_id(self, empty_store):
        for mid in ("zebra", "apple", "mango"):
            empty_store.save_model({
                "id": mid, "provider": "anthropic", "model_name": "x",
            })
        ids = [r["id"] for r in empty_store.list_models()]
        assert ids == ["apple", "mango", "zebra"]


# ── Seeding ──────────────────────────────────────────────────────────

class TestSeedDefaults:
    def test_seeds_all_defaults_on_first_init(self, isolated_store):
        # The fixture's initialize() seeds. Defaults should be present.
        ids = {r["id"] for r in isolated_store.list_models()}
        # Every shipped default should land in models/
        for default_id in isolated_store.list_default_ids():
            assert default_id in ids, f"missing seeded default: {default_id}"

    def test_seed_never_overwrites_user_edits(self, isolated_store):
        # Modify a seeded model
        isolated_store.update_model(
            "claude-sonnet-4-6", {"description": "USER MODIFIED"},
        )
        # Re-run seeding (idempotent)
        isolated_store._seed_defaults()
        rec = isolated_store.get_model("claude-sonnet-4-6")
        assert rec["description"] == "USER MODIFIED"

    def test_install_default_writes_only_when_missing(self, isolated_store):
        # First delete a seeded model
        isolated_store.delete_model("claude-sonnet-4-6")
        # Re-install
        assert isolated_store.install_default("claude-sonnet-4-6") is True
        # Second call should NOT overwrite
        isolated_store.update_model(
            "claude-sonnet-4-6", {"description": "DON'T OVERWRITE"},
        )
        assert isolated_store.install_default("claude-sonnet-4-6") is False
        rec = isolated_store.get_model("claude-sonnet-4-6")
        assert rec["description"] == "DON'T OVERWRITE"

    def test_install_default_with_overwrite_replaces(self, isolated_store):
        isolated_store.update_model(
            "claude-sonnet-4-6", {"description": "USER"},
        )
        assert isolated_store.install_default(
            "claude-sonnet-4-6", overwrite=True,
        ) is True
        rec = isolated_store.get_model("claude-sonnet-4-6")
        # Description from default YAML, not user's edit
        assert "USER" not in rec["description"]

    def test_install_default_unknown_returns_false(self, isolated_store):
        assert isolated_store.install_default("no-such-model") is False

    def test_list_default_ids_matches_shipped(self, isolated_store):
        defaults = set(isolated_store.list_default_ids())
        # Shipped defaults: 4 Phase 2 slice 1 + LM Studio template
        # (slice 1.5) + the two llama.cpp Qwen3 local registry records
        # (32B retained as rollback target + 122B new default, 2026-06-07).
        assert "claude-sonnet-4-6" in defaults
        assert "claude-haiku-4-5-20251001" in defaults
        assert "claude-opus-4-7" in defaults
        assert "ollama-llama3-1-8b" in defaults
        assert "lmstudio-remote" in defaults
        assert "llamacpp-qwen3-32b-q4km" in defaults
        assert "llamacpp-qwen35-122b-a10b" in defaults

    def test_shipped_122b_default_pins_anti_loop_sampling(self, isolated_store):
        # Drift guard for the 2026-06-07 repoint fix: the new big-local
        # default MUST ship presence_penalty so its reasoning trace
        # self-terminates instead of looping past max_output_tokens and
        # returning empty content. Removing this silently reintroduces
        # the empty-label failure mode (caught only minutes-into-a-call).
        rec = isolated_store.get_model("llamacpp-qwen35-122b-a10b")
        assert rec is not None, "122B default not seeded"
        assert rec.get("sampling", {}).get("presence_penalty") == 1.5
        # And the budget/timeout that pair with thinking-on (>=4096 /
        # >=300s) per the LAN_AI guide's "Calling the 122B safely" note.
        assert int(rec["max_output_tokens"]) >= 4096
        assert int(rec["default_timeout_seconds"]) >= 300


# ── Default YAML files validate cleanly ──────────────────────────────

class TestShippedDefaults:
    """Drift guard - every YAML in default_models/ must round-trip
    through ModelValidation. Catches a broken default at PR time
    rather than at first-run-init time on the operator's machine.
    """

    def test_every_default_yaml_is_valid(self):
        for path in sorted(DEFAULTS_DIR.glob("*.yaml")):
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            try:
                ModelValidation.validate_record(data)
            except ValueError as exc:
                pytest.fail(f"{path.name}: validation failed: {exc}")

    def test_every_default_id_matches_filename(self):
        for path in sorted(DEFAULTS_DIR.glob("*.yaml")):
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            assert data["id"] == path.stem, (
                f"{path.name}: id={data['id']!r} doesn't match filename stem"
            )


# ── Singleton ────────────────────────────────────────────────────────

class TestSingleton:
    def test_get_store_returns_same_instance(self, tmp_path, monkeypatch):
        # Patch the module-level dir so we don't touch real state
        import model_store
        reset_for_tests()
        monkeypatch.setattr(model_store, "MODELS_DIR", tmp_path / "m")
        a = get_store()
        b = get_store()
        assert a is b
        reset_for_tests()

    def test_reset_clears_singleton(self, tmp_path, monkeypatch):
        import model_store
        reset_for_tests()
        monkeypatch.setattr(model_store, "MODELS_DIR", tmp_path / "m")
        a = get_store()
        reset_for_tests()
        b = get_store()
        assert a is not b
        reset_for_tests()

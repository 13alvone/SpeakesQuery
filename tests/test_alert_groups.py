#!/usr/bin/env python3
"""
Unit tests for the Alert Groups feature.

Covers:
  - BoilerplatePromptStore CRUD
  - AlertGroupStore CRUD and run logging
  - ResultSerializer (token estimation, row capping, error cases)
  - PayloadBuilder (template injection, block rendering)
  - AlertGroupDispatcher (skip/error/success flows)
  - AlertGroupScheduler (job registration)
  - BoilerplatePromptValidation
  - AlertGroupValidation
  - REST API endpoints (via Flask test client)

All tests run without an API key, network access, or real search data.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pandas as pd

from alert_groups.models import AlertGroupRunResult, SerializedResult
from alert_groups.builder import PayloadBuilder
from alert_groups.serializer import (
    EmptyResultError,
    ResultSerializer,
    SearchNotFoundError,
)
from validation.AlertGroupValidation import AlertGroupValidation
from validation.BoilerplatePromptValidation import BoilerplatePromptValidation


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def sample_prompt():
    return {
        "name": "analyst_brief",
        "template": "Group: {group_name}\nTimestamp: {run_timestamp}\nSearches: {search_count}\n{search_blocks}",
    }


@pytest.fixture
def sample_group():
    return {
        "name": "polymarket_daily",
        "description": "Daily prediction market summary",
        "search_names": ["hormuz_volume_spike", "iran_ceasefire_odds"],
        "prompt_text": "Analyze the following prediction market data and identify the 5 highest-conviction opportunities.",
        "schedule": "0 6 * * *",
        "max_rows": 200,
        "email_address": "test@example.com",
        "disabled": False,
    }


@pytest.fixture
def sample_result_a():
    return SerializedResult(
        search_name="hormuz_volume_spike",
        row_count=50,
        estimated_tokens=420,
        format="json",
        content='[{"market": "Hormuz closure", "yes_price": 0.34, "volume_24h": 182000}]',
    )


@pytest.fixture
def sample_result_b():
    return SerializedResult(
        search_name="iran_ceasefire_odds",
        row_count=30,
        estimated_tokens=280,
        format="json",
        content='[{"market": "Iran ceasefire by June", "yes_price": 0.21, "volume_24h": 94000}]',
    )


@pytest.fixture
def tmp_dir():
    """Create a temporary directory that is cleaned up after the test."""
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def bp_store(tmp_dir):
    """BoilerplatePromptStore with isolated temp directories."""
    from boilerplate_prompt_store import BoilerplatePromptStore
    store = BoilerplatePromptStore()
    store._dir = Path(tmp_dir) / "boilerplate_prompts"
    store._db = str(Path(tmp_dir) / "last_chance.sqlite")
    store.initialize()
    return store


@pytest.fixture
def ag_store(tmp_dir):
    """AlertGroupStore with isolated temp directories.

    NOTE (2026-04-30): ``initialize()`` now calls ``_seed_defaults()`` which
    copies every yaml from `default_alert_groups/` into the store dir. For
    tests that want a CLEAN empty store, we point ``_defaults_dir`` at an
    empty temp subdir so the seed runs but is a no-op - keeping the prior
    "store starts empty" assumption intact. Tests that need real defaults
    can either point ``_defaults_dir`` at the project's `default_alert_groups/`
    or use a different fixture that does so.
    """
    from alert_group_store import AlertGroupStore
    empty_defaults = Path(tmp_dir) / "_empty_default_alert_groups"
    empty_defaults.mkdir()

    store = AlertGroupStore()
    store._dir = Path(tmp_dir) / "alert_groups"
    store._defaults_dir = empty_defaults
    store._db = str(Path(tmp_dir) / "last_chance.sqlite")
    store._runs_db = str(Path(tmp_dir) / "alert_group_runs.sqlite")
    store.initialize()
    return store


# =====================================================================
# BoilerplatePromptValidation
# =====================================================================

class TestBoilerplatePromptValidation:

    def test_valid_name(self):
        assert BoilerplatePromptValidation.validate_name("my_prompt") == "my_prompt"

    def test_empty_name_raises(self):
        with pytest.raises(ValueError):
            BoilerplatePromptValidation.validate_name("")

    def test_invalid_name_raises(self):
        with pytest.raises(ValueError):
            BoilerplatePromptValidation.validate_name("my/prompt")

    def test_valid_template(self):
        assert BoilerplatePromptValidation.validate_template("Hello {group_name}") == "Hello {group_name}"

    def test_empty_template_raises(self):
        with pytest.raises(ValueError):
            BoilerplatePromptValidation.validate_template("")

    def test_none_template_raises(self):
        with pytest.raises(ValueError):
            BoilerplatePromptValidation.validate_template(None)


# =====================================================================
# AlertGroupValidation
# =====================================================================

class TestAlertGroupValidation:

    def test_valid_name(self):
        assert AlertGroupValidation.validate_name("my group") == "my group"

    def test_empty_name_raises(self):
        with pytest.raises(ValueError):
            AlertGroupValidation.validate_name("")

    def test_valid_search_names(self):
        names = ["search1", "search2"]
        assert AlertGroupValidation.validate_search_names(names) == names

    def test_empty_search_names_raises(self):
        with pytest.raises(ValueError):
            AlertGroupValidation.validate_search_names([])

    def test_too_many_search_names_raises(self):
        # Default cap is 10; 11 items must raise against the default.
        with pytest.raises(ValueError):
            AlertGroupValidation.validate_search_names(
                [f"s{i}" for i in range(11)], max_feeders=10
            )

    def test_non_list_raises(self):
        with pytest.raises(ValueError):
            AlertGroupValidation.validate_search_names("not_a_list")

    def test_respects_explicit_max_feeders_override(self):
        # When the operator raises alert_group_max_feeders to 25, a list of
        # 11 names is now valid but 26 still fails.
        names_11 = [f"s{i}" for i in range(11)]
        assert (
            AlertGroupValidation.validate_search_names(names_11, max_feeders=25)
            == names_11
        )
        with pytest.raises(ValueError) as exc:
            AlertGroupValidation.validate_search_names(
                [f"s{i}" for i in range(26)], max_feeders=25
            )
        assert "25" in str(exc.value)

    def test_respects_min_cap_of_2(self):
        # The setting itself is bounded at floor=2 - confirm the validator
        # passes the error message through verbatim.
        with pytest.raises(ValueError) as exc:
            AlertGroupValidation.validate_search_names(
                ["a", "b", "c"], max_feeders=2
            )
        assert "2" in str(exc.value)

    def test_respects_max_cap_of_100(self):
        names_100 = [f"s{i}" for i in range(100)]
        assert (
            AlertGroupValidation.validate_search_names(names_100, max_feeders=100)
            == names_100
        )
        with pytest.raises(ValueError):
            AlertGroupValidation.validate_search_names(
                [f"s{i}" for i in range(101)], max_feeders=100
            )

    def test_validator_reads_live_setting_when_override_omitted(self):
        """When no explicit ``max_feeders`` is passed, the validator must
        resolve the cap from ``global_settings.alert_group_max_feeders``.
        """
        from global_settings import get_settings

        settings = get_settings()
        original = settings.get("alert_group_max_feeders") or 10
        try:
            # Bump the cap to 25 and verify a 15-item list now passes.
            settings.set("alert_group_max_feeders", 25)
            names_15 = [f"s{i}" for i in range(15)]
            assert AlertGroupValidation.validate_search_names(names_15) == names_15
            # And 26 items still fails with the new cap quoted verbatim.
            with pytest.raises(ValueError) as exc:
                AlertGroupValidation.validate_search_names(
                    [f"s{i}" for i in range(26)]
                )
            assert "25" in str(exc.value)
        finally:
            settings.set("alert_group_max_feeders", original)


class TestAlertGroupMaxFeedersSetting:
    """The ``alert_group_max_feeders`` setting itself is int 2..100."""

    def test_default_is_10(self):
        from global_settings import DEFAULTS
        assert DEFAULTS["alert_group_max_feeders"] == 10

    def test_set_below_floor_rejected(self, tmp_path, monkeypatch):
        from global_settings import GlobalSettings
        monkeypatch.chdir(tmp_path)
        s = GlobalSettings(tmp_path)
        with pytest.raises(ValueError) as exc:
            s.set("alert_group_max_feeders", 1)
        assert "minimum" in str(exc.value).lower()

    def test_set_above_ceiling_rejected(self, tmp_path, monkeypatch):
        from global_settings import GlobalSettings
        monkeypatch.chdir(tmp_path)
        s = GlobalSettings(tmp_path)
        with pytest.raises(ValueError) as exc:
            s.set("alert_group_max_feeders", 101)
        assert "maximum" in str(exc.value).lower()

    def test_set_valid_values_accepted(self, tmp_path, monkeypatch):
        from global_settings import GlobalSettings
        monkeypatch.chdir(tmp_path)
        s = GlobalSettings(tmp_path)
        for v in (2, 10, 50, 100):
            s.set("alert_group_max_feeders", v)
            assert s.get("alert_group_max_feeders") == v

    def test_valid_schedule(self):
        assert AlertGroupValidation.validate_schedule("0 6 * * *") == "0 6 * * *"

    def test_empty_schedule_is_valid(self):
        assert AlertGroupValidation.validate_schedule("") == ""

    def test_invalid_schedule_raises(self):
        with pytest.raises(ValueError):
            AlertGroupValidation.validate_schedule("not a cron")

    def test_valid_max_rows(self):
        assert AlertGroupValidation.validate_max_rows(100) == 100

    def test_max_rows_out_of_range_raises(self):
        with pytest.raises(ValueError):
            AlertGroupValidation.validate_max_rows(0)

    def test_max_rows_string_converted(self):
        assert AlertGroupValidation.validate_max_rows("50") == 50

    def test_valid_email(self):
        assert AlertGroupValidation.validate_email("test@example.com") == "test@example.com"

    def test_multi_email_comma(self):
        result = AlertGroupValidation.validate_email("a@b.com, c@d.com")
        assert result == "a@b.com, c@d.com"

    def test_multi_email_semicolon(self):
        result = AlertGroupValidation.validate_email("a@b.com; c@d.com")
        assert result == "a@b.com; c@d.com"

    def test_invalid_email_raises(self):
        with pytest.raises(ValueError):
            AlertGroupValidation.validate_email("not_an_email")

    def test_multi_email_one_invalid_raises(self):
        with pytest.raises(ValueError):
            AlertGroupValidation.validate_email("good@example.com, bad_address")

    def test_valid_prompt_text(self):
        assert AlertGroupValidation.validate_prompt_text("Analyze these results") == "Analyze these results"

    def test_empty_prompt_text_raises(self):
        with pytest.raises(ValueError):
            AlertGroupValidation.validate_prompt_text("")

    # -- delivery_mode (budget-friendly prompt-only mode, 2026-04-22) --

    def test_delivery_mode_api(self):
        assert AlertGroupValidation.validate_delivery_mode("api") == "api"

    def test_delivery_mode_prompt_only(self):
        assert AlertGroupValidation.validate_delivery_mode("prompt_only") == "prompt_only"

    def test_delivery_mode_case_insensitive(self):
        assert AlertGroupValidation.validate_delivery_mode("PROMPT_ONLY") == "prompt_only"
        assert AlertGroupValidation.validate_delivery_mode("Api") == "api"

    def test_delivery_mode_empty_defaults_to_api(self):
        """Back-compat: pre-existing AG YAMLs without delivery_mode must load."""
        assert AlertGroupValidation.validate_delivery_mode("") == "api"
        assert AlertGroupValidation.validate_delivery_mode(None) == "api"
        assert AlertGroupValidation.validate_delivery_mode("   ") == "api"

    def test_delivery_mode_invalid_raises(self):
        with pytest.raises(ValueError) as excinfo:
            AlertGroupValidation.validate_delivery_mode("email_only")
        assert "delivery_mode" in str(excinfo.value)
        assert "api" in str(excinfo.value) and "prompt_only" in str(excinfo.value)

    def test_delivery_mode_non_string_raises(self):
        with pytest.raises(ValueError):
            AlertGroupValidation.validate_delivery_mode(42)


# =====================================================================
# BoilerplatePromptStore
# =====================================================================

class TestBoilerplatePromptStore:

    def test_save_and_get(self, bp_store):
        result = bp_store.save_prompt({
            "name": "test_prompt",
            "template": "Hello {group_name}",
        })
        assert result["name"] == "test_prompt"
        assert result["template"] == "Hello {group_name}"

        fetched = bp_store.get_prompt("test_prompt")
        assert fetched["name"] == "test_prompt"

    def test_list_prompts(self, bp_store):
        bp_store.save_prompt({"name": "prompt_a", "template": "A"})
        bp_store.save_prompt({"name": "prompt_b", "template": "B"})
        prompts = bp_store.list_prompts()
        # +1 for the seeded default
        assert len(prompts) >= 2
        names = [p["name"] for p in prompts]
        assert "prompt_a" in names
        assert "prompt_b" in names

    def test_duplicate_raises(self, bp_store):
        bp_store.save_prompt({"name": "dup", "template": "X"})
        with pytest.raises(FileExistsError):
            bp_store.save_prompt({"name": "dup", "template": "Y"})

    def test_overwrite(self, bp_store):
        bp_store.save_prompt({"name": "ow", "template": "old"})
        result = bp_store.save_prompt({"name": "ow", "template": "new"}, overwrite=True)
        assert result["template"] == "new"

    def test_update(self, bp_store):
        bp_store.save_prompt({"name": "up", "template": "original"})
        updated = bp_store.update_prompt("up", {"template": "modified"})
        assert updated["template"] == "modified"

    def test_delete(self, bp_store):
        bp_store.save_prompt({"name": "del_me", "template": "bye"})
        bp_store.delete_prompt("del_me")
        with pytest.raises(FileNotFoundError):
            bp_store.get_prompt("del_me")

    def test_delete_nonexistent_raises(self, bp_store):
        with pytest.raises(FileNotFoundError):
            bp_store.delete_prompt("nope")

    def test_get_yaml(self, bp_store):
        bp_store.save_prompt({"name": "yaml_test", "template": "raw"})
        raw = bp_store.get_prompt_yaml("yaml_test")
        assert "yaml_test" in raw

    def test_default_seed(self, bp_store):
        """Default analyst_brief prompt should be seeded on init."""
        prompts = bp_store.list_prompts()
        names = [p["name"] for p in prompts]
        assert "analyst_brief" in names


# =====================================================================
# AlertGroupStore
# =====================================================================

class TestAlertGroupStore:

    def test_save_and_get(self, ag_store, sample_group):
        result = ag_store.save_group(sample_group)
        assert result["name"] == "polymarket_daily"
        assert result["search_names"] == ["hormuz_volume_spike", "iran_ceasefire_odds"]

        fetched = ag_store.get_group("polymarket_daily")
        assert "highest-conviction" in fetched["prompt_text"]

    def test_list_groups(self, ag_store, sample_group):
        ag_store.save_group(sample_group)
        groups = ag_store.list_groups()
        assert len(groups) >= 1
        assert groups[0]["name"] == "polymarket_daily"

    def test_duplicate_raises(self, ag_store, sample_group):
        ag_store.save_group(sample_group)
        with pytest.raises(FileExistsError):
            ag_store.save_group(sample_group)

    def test_update(self, ag_store, sample_group):
        ag_store.save_group(sample_group)
        updated = ag_store.update_group("polymarket_daily", {"max_rows": 100})
        assert updated["max_rows"] == 100

    def test_delete(self, ag_store, sample_group):
        ag_store.save_group(sample_group)
        ag_store.delete_group("polymarket_daily")
        with pytest.raises(FileNotFoundError):
            ag_store.get_group("polymarket_daily")

    def test_next_run_time(self, ag_store, sample_group):
        result = ag_store.save_group(sample_group)
        assert result.get("next_run_time")
        assert result["next_run_time"] != ""

    def test_no_schedule_no_next_run(self, ag_store, sample_group):
        sample_group["schedule"] = ""
        result = ag_store.save_group(sample_group)
        assert result["next_run_time"] == ""

    def test_get_yaml(self, ag_store, sample_group):
        ag_store.save_group(sample_group)
        raw = ag_store.get_group_yaml("polymarket_daily")
        assert "polymarket_daily" in raw

    def test_log_and_list_runs(self, ag_store):
        run_id = ag_store.log_run(
            group_name="test_group",
            status="success",
            searches_used=["s1", "s2"],
            estimated_tokens=500,
            actual_tokens=400,
            cost_usd=0.002,
        )
        assert run_id > 0
        runs = ag_store.list_runs("test_group")
        assert len(runs) == 1
        assert runs[0]["status"] == "success"

    def test_list_runs_all(self, ag_store):
        ag_store.log_run("group_a", "success")
        ag_store.log_run("group_b", "error", error_message="test error")
        runs = ag_store.list_runs()
        assert len(runs) == 2

    # -- delivery_mode round-trip (2026-04-22) --

    def test_delivery_mode_defaults_to_api(self, ag_store, sample_group):
        """AGs saved without an explicit delivery_mode default to 'api'."""
        sample_group.pop("delivery_mode", None)
        result = ag_store.save_group(sample_group)
        assert result["delivery_mode"] == "api"
        # Round-trip through YAML to prove persistence.
        fetched = ag_store.get_group(sample_group["name"])
        assert fetched["delivery_mode"] == "api"

    def test_delivery_mode_prompt_only_round_trip(self, ag_store, sample_group):
        """prompt_only persists through save → YAML → load → update."""
        sample_group["delivery_mode"] = "prompt_only"
        saved = ag_store.save_group(sample_group)
        assert saved["delivery_mode"] == "prompt_only"
        fetched = ag_store.get_group(sample_group["name"])
        assert fetched["delivery_mode"] == "prompt_only"
        # Update back to api
        updated = ag_store.update_group(sample_group["name"], {"delivery_mode": "api"})
        assert updated["delivery_mode"] == "api"

    def test_delivery_mode_prompt_only_requires_email(self, ag_store, sample_group):
        """prompt_only with empty email_address is rejected at save time -
        the prompt is delivered by email, so no recipient = nowhere to go."""
        sample_group["delivery_mode"] = "prompt_only"
        sample_group["email_address"] = ""
        with pytest.raises(ValueError) as excinfo:
            ag_store.save_group(sample_group)
        assert "prompt_only" in str(excinfo.value)
        assert "email_address" in str(excinfo.value)

    def test_delivery_mode_invalid_rejected_at_save(self, ag_store, sample_group):
        sample_group["delivery_mode"] = "telepathy"
        with pytest.raises(ValueError):
            ag_store.save_group(sample_group)

    def test_delivery_mode_update_to_prompt_only_without_email_rejected(
        self, ag_store, sample_group,
    ):
        """Updating an existing AG to prompt_only must still require email."""
        # Save without email is allowed for the default 'api' mode (historically
        # we treat no-email as cache-only), but flipping to prompt_only after
        # the fact must catch the missing recipient.
        sample_group["email_address"] = ""
        ag_store.save_group(sample_group)
        with pytest.raises(ValueError) as excinfo:
            ag_store.update_group(
                sample_group["name"], {"delivery_mode": "prompt_only"},
            )
        assert "prompt_only" in str(excinfo.value)


# =====================================================================
# ResultSerializer
# =====================================================================

class TestResultSerializer:

    def test_token_estimation_basic(self):
        s = ResultSerializer()
        # 350 chars / 3.5 = 100 tokens
        assert s.estimate_tokens("x" * 350) == 100

    def test_token_estimation_minimum_one(self):
        s = ResultSerializer()
        assert s.estimate_tokens("") == 1

    def test_max_rows_applied(self):
        """Serializer must not return more rows than max_rows."""
        df = pd.DataFrame({"col": range(500)})
        s = ResultSerializer(max_rows=100)
        with patch.object(s, "_load_last_result", return_value=df):
            result = s.serialize(search_name="test_search")
            assert result.row_count <= 100

    def test_empty_result_raises(self):
        s = ResultSerializer()
        with patch.object(s, "_load_last_result", return_value=pd.DataFrame()):
            with pytest.raises(EmptyResultError):
                s.serialize(search_name="empty_search")

    def test_missing_search_raises(self):
        s = ResultSerializer()
        with patch.object(s, "_load_last_result", side_effect=SearchNotFoundError("not found")):
            with pytest.raises(SearchNotFoundError):
                s.serialize(search_name="missing")

    def test_json_output_is_valid(self):
        df = pd.DataFrame([{"a": 1, "b": "two"}])
        s = ResultSerializer(fmt="json")
        with patch.object(s, "_load_last_result", return_value=df):
            result = s.serialize(search_name="test")
            parsed = json.loads(result.content)
            assert isinstance(parsed, list)
            assert parsed[0]["a"] == 1

    def test_csv_output_has_header(self):
        df = pd.DataFrame([{"col_a": 1, "col_b": "x"}])
        s = ResultSerializer(fmt="csv")
        with patch.object(s, "_load_last_result", return_value=df):
            result = s.serialize(search_name="test")
            lines = result.content.strip().splitlines()
            assert "col_a" in lines[0]
            assert "col_b" in lines[0]

    def test_serialized_result_fields(self):
        df = pd.DataFrame([{"x": 1}])
        s = ResultSerializer()
        with patch.object(s, "_load_last_result", return_value=df):
            result = s.serialize(search_name="my_search")
            assert result.search_name == "my_search"
            assert result.row_count == 1
            assert result.format == "json"
            assert result.estimated_tokens > 0


# =====================================================================
# PayloadBuilder
# =====================================================================

class TestPayloadBuilder:

    def test_returns_list_with_user_role(self, sample_result_a):
        b = PayloadBuilder()
        messages = b.build("test_group", [sample_result_a], "Analyze this data")
        assert isinstance(messages, list)
        assert messages[0]["role"] == "user"

    def test_prompt_text_at_start(self, sample_result_a):
        b = PayloadBuilder()
        messages = b.build("g", [sample_result_a], "My custom instruction")
        assert messages[0]["content"].startswith("My custom instruction")

    def test_group_name_injected(self, sample_result_a):
        b = PayloadBuilder()
        messages = b.build("polymarket_daily", [sample_result_a], "Analyze")
        assert "polymarket_daily" in messages[0]["content"]

    def test_all_search_names_present(self, sample_result_a, sample_result_b):
        b = PayloadBuilder()
        messages = b.build("g", [sample_result_a, sample_result_b], "Analyze")
        content = messages[0]["content"]
        assert sample_result_a.search_name in content
        assert sample_result_b.search_name in content

    def test_data_content_embedded(self, sample_result_a):
        b = PayloadBuilder()
        messages = b.build("g", [sample_result_a], "Analyze")
        assert "Hormuz closure" in messages[0]["content"]

    def test_empty_results_raises(self):
        b = PayloadBuilder()
        with pytest.raises(ValueError, match="at least one"):
            b.build("g", [], "template")

    def test_search_count_injected(self, sample_result_a, sample_result_b):
        b = PayloadBuilder()
        messages = b.build("g", [sample_result_a, sample_result_b], "Analyze")
        assert "Searches included:** 2" in messages[0]["content"]

    def test_render_blocks_has_headers(self, sample_result_a):
        b = PayloadBuilder()
        blocks = b._render_blocks([sample_result_a])
        assert "## Search: hormuz_volume_spike" in blocks
        assert "(50 rows, JSON)" in blocks

    def test_render_blocks_code_fenced(self, sample_result_a):
        b = PayloadBuilder()
        blocks = b._render_blocks([sample_result_a])
        assert "```json" in blocks
        assert "```" in blocks


# =====================================================================
# AlertGroupDispatcher
# =====================================================================

class TestAlertGroupDispatcher:

    def _make_dispatcher(self):
        from alert_groups.dispatcher import AlertGroupDispatcher
        d = AlertGroupDispatcher()
        d.serializer = MagicMock(spec=ResultSerializer)
        return d

    def test_skips_disabled_group(self, sample_group):
        sample_group["disabled"] = True
        d = self._make_dispatcher()
        with patch.object(type(d), "_log_run"):
            run = d.run(group=sample_group)
        assert run.status == "skipped"

    def test_skips_empty_searches_gracefully(self, sample_group):
        d = self._make_dispatcher()
        d.serializer.serialize.side_effect = EmptyResultError("empty")
        with patch.object(type(d), "_log_run"):
            run = d.run(group=sample_group)
        assert run.status == "error"
        assert "No results" in run.error_message

    def test_missing_prompt_text_returns_error(self, sample_group, sample_result_a):
        sample_group["prompt_text"] = ""
        d = self._make_dispatcher()
        d.serializer.serialize.return_value = sample_result_a
        with patch.object(type(d), "_log_run"):
            with patch.object(type(d), "_get_budget_gate", return_value=None):
                run = d.run(group=sample_group)
        assert run.status == "error"
        assert "prompt text" in run.error_message.lower()

    @staticmethod
    def _fake_claude_result(text: str = "analyst brief text",
                             in_tokens: int = 100, out_tokens: int = 200):
        """Return a ClaudeCallResult-shaped mock matching the wrapper's contract.

        The dispatcher was refactored on 2026-04-19 to route all Claude calls
        through ``analyzers.claude_client.call_messages_create`` - see
        ``reference_claude_api_call_wrapper.md``. The old private
        ``_call_claude`` method was removed. Patch the module-level
        ``call_messages_create`` symbol instead (imported from
        analyzers.claude_client at the top of dispatcher.py).
        """
        raw = MagicMock()
        raw.content = [MagicMock(text=text)]
        raw.usage = MagicMock(input_tokens=in_tokens, output_tokens=out_tokens)
        result = MagicMock()
        result.response = raw
        result.request_id = "rid-test"
        result.model = "claude-sonnet-4-6"
        result.input_tokens = in_tokens
        result.output_tokens = out_tokens
        result.cache_read_tokens = 0
        result.cache_creation_tokens = 0
        result.cost_usd = round(in_tokens / 1e6 * 3.0 + out_tokens / 1e6 * 15.0, 6)
        result.latency_ms = 50
        result.attempts = 1
        return result

    def test_successful_run_calls_email(self, sample_group, sample_result_a):
        d = self._make_dispatcher()
        d.serializer.serialize.return_value = sample_result_a

        result = self._fake_claude_result()

        with patch("alert_groups.dispatcher.call_messages_create", return_value=result):
            with patch.object(type(d), "_send_html_email") as mock_email:
                with patch.object(type(d), "_log_run"):
                    with patch.object(type(d), "_get_budget_gate", return_value=None):
                        run = d.run(group=sample_group)

        assert run.status == "success"
        mock_email.assert_called_once()
        # Subject format changed 2026-04-20 per user feedback Issue #6:
        # "[SpeakesQuery REPORT] <group_name> - <YYYY-MM-DD>" (no more
        # "Analyst Brief" suffix). TRUNCATED suffix appears only when
        # Claude hit max_tokens.
        subject = mock_email.call_args.kwargs.get(
            "subject",
            mock_email.call_args[1].get("subject", ""),
        )
        assert subject.startswith("[SpeakesQuery REPORT] polymarket_daily")
        assert " - " in subject   # em-dash separator
        assert "TRUNCATED" not in subject  # fake response has no stop_reason='max_tokens'

    def test_api_failure_returns_error(self, sample_group, sample_result_a):
        d = self._make_dispatcher()
        d.serializer.serialize.return_value = sample_result_a

        from analyzers.claude_client import ClaudeCallError
        err = ClaudeCallError(
            "API down", request_id="rid", error_class="APIConnectionError",
            attempts=3,
        )

        with patch("alert_groups.dispatcher.call_messages_create", side_effect=err):
            with patch.object(type(d), "_log_run"):
                with patch.object(type(d), "_get_budget_gate", return_value=None):
                    with patch.object(type(d), "_maybe_send_failure_email"):
                        run = d.run(group=sample_group)

        assert run.status == "error"
        assert "API" in run.error_message

    def test_no_email_when_address_empty(self, sample_group, sample_result_a):
        sample_group["email_address"] = ""
        d = self._make_dispatcher()
        d.serializer.serialize.return_value = sample_result_a

        result = self._fake_claude_result(text="brief", in_tokens=50, out_tokens=100)

        with patch("alert_groups.dispatcher.call_messages_create", return_value=result):
            with patch.object(type(d), "_send_html_email") as mock_email:
                with patch.object(type(d), "_log_run"):
                    with patch.object(type(d), "_get_budget_gate", return_value=None):
                        run = d.run(group=sample_group)

        assert run.status == "success"
        mock_email.assert_not_called()

    # ─── Prompt-only delivery mode (budget-friendly, 2026-04-22) ───────

    def test_prompt_only_skips_claude_and_emails_prompt(
        self, sample_group, sample_result_a,
    ):
        """Core contract: prompt_only mode must not call Claude, must send
        an email whose subject is [SpeakesQuery PROMPT] (not REPORT), and
        whose plain body contains the user's prompt_text + the serialized
        feeder data (the exact string the API path would have sent)."""
        sample_group["delivery_mode"] = "prompt_only"
        d = self._make_dispatcher()
        d.serializer.serialize.return_value = sample_result_a

        with patch("alert_groups.dispatcher.call_messages_create") as mock_claude:
            with patch.object(type(d), "_send_html_email") as mock_email:
                with patch.object(type(d), "_log_run"):
                    with patch.object(type(d), "_get_budget_gate", return_value=None):
                        run = d.run(group=sample_group)

        mock_claude.assert_not_called()
        mock_email.assert_called_once()

        assert run.status == "prompt_only"
        assert run.actual_tokens == 0
        assert run.cost_usd == 0.0
        # estimated_tokens should still be populated (prompt was built)
        assert run.estimated_tokens > 0
        # response_text holds the built prompt so the UI can display it
        assert run.response_text
        assert sample_group["prompt_text"] in run.response_text

        # Subject uses the PROMPT prefix and em-dash separator
        kwargs = mock_email.call_args.kwargs
        subject = kwargs.get("subject", "")
        assert subject.startswith("[SpeakesQuery PROMPT] polymarket_daily")
        assert " - " in subject

        # plain body = the built prompt, not a Claude response
        assert sample_group["prompt_text"] in kwargs.get("plain_body", "")
        # meta flags the email renderer to show the prompt-only banner
        assert kwargs.get("meta", {}).get("prompt_only") is True
        assert kwargs.get("meta", {}).get("cost_usd") == 0.0
        # attach_markdown stays True so the recipient gets a .md copy to paste
        assert kwargs.get("attach_markdown") is True

    def test_prompt_only_email_failure_surfaces_as_error(
        self, sample_group, sample_result_a,
    ):
        """An SMTP failure in prompt-only mode must still route through the
        error path (failure email + circuit-breaker tick), not swallow."""
        sample_group["delivery_mode"] = "prompt_only"
        d = self._make_dispatcher()
        d.serializer.serialize.return_value = sample_result_a

        with patch("alert_groups.dispatcher.call_messages_create") as mock_claude:
            with patch.object(type(d), "_send_html_email", side_effect=RuntimeError("smtp boom")):
                with patch.object(type(d), "_log_run"):
                    with patch.object(type(d), "_get_budget_gate", return_value=None):
                        with patch.object(type(d), "_maybe_send_failure_email") as mock_fail:
                            with patch.object(type(d), "_maybe_trip_circuit_breaker"):
                                run = d.run(group=sample_group)

        mock_claude.assert_not_called()
        assert run.status == "error"
        assert "smtp boom" in run.error_message
        mock_fail.assert_called_once()

    def test_prompt_only_without_email_is_error(self, sample_group, sample_result_a):
        """Defense-in-depth: validator should catch this at save time, but a
        hand-edited YAML can still reach the dispatcher. Fail with an
        actionable message instead of a silent success."""
        sample_group["delivery_mode"] = "prompt_only"
        sample_group["email_address"] = ""
        d = self._make_dispatcher()
        d.serializer.serialize.return_value = sample_result_a

        with patch("alert_groups.dispatcher.call_messages_create") as mock_claude:
            with patch.object(type(d), "_send_html_email") as mock_email:
                with patch.object(type(d), "_log_run"):
                    with patch.object(type(d), "_get_budget_gate", return_value=None):
                        with patch.object(type(d), "_maybe_send_failure_email"):
                            with patch.object(type(d), "_maybe_trip_circuit_breaker"):
                                run = d.run(group=sample_group)

        mock_claude.assert_not_called()
        mock_email.assert_not_called()
        assert run.status == "error"
        assert "email_address" in run.error_message
        assert "prompt_only" in run.error_message

    def test_prompt_only_respects_disabled_gate(self, sample_group):
        """prompt_only mode must still honor the disabled gate - flipping
        delivery_mode is not an escape hatch for a disabled AG."""
        sample_group["delivery_mode"] = "prompt_only"
        sample_group["disabled"] = True
        d = self._make_dispatcher()
        with patch.object(type(d), "_log_run"):
            run = d.run(group=sample_group)
        assert run.status == "skipped"

    def test_prompt_only_still_requires_prompt_text(self, sample_group, sample_result_a):
        """prompt_only with empty prompt_text is as bad as api mode with
        empty prompt_text - empty email body is a user-facing silent fail."""
        sample_group["delivery_mode"] = "prompt_only"
        sample_group["prompt_text"] = ""
        d = self._make_dispatcher()
        d.serializer.serialize.return_value = sample_result_a

        with patch("alert_groups.dispatcher.call_messages_create") as mock_claude:
            with patch.object(type(d), "_send_html_email") as mock_email:
                with patch.object(type(d), "_log_run"):
                    with patch.object(type(d), "_get_budget_gate", return_value=None):
                        with patch.object(type(d), "_maybe_send_failure_email"):
                            with patch.object(type(d), "_maybe_trip_circuit_breaker"):
                                run = d.run(group=sample_group)

        mock_claude.assert_not_called()
        mock_email.assert_not_called()
        assert run.status == "error"
        assert "prompt text" in run.error_message.lower()

    def test_prompt_only_html_renders_banner_and_preserves_prompt(self):
        """build_html_email with meta.prompt_only=True must render the blue
        prompt-only banner, swap the subtitle, and embed the raw prompt
        inside a <pre> block so fenced code / tables survive for copy-paste.
        Regression guard for future template edits."""
        from alert_groups.dispatcher import build_html_email
        html = build_html_email(
            "demo_group",
            'Prompt body\n\n## Search: foo (3 rows)\n\n```json\n[{"x":1}]\n```',
            {
                "searches_used": ["foo"],
                "estimated_tokens": 1234,
                "actual_tokens": 0,
                "cost_usd": 0.0,
                "prompt_only": True,
            },
        )
        assert "Prompt-only delivery" in html
        assert "Prompt-Only Delivery" in html
        assert "claude.ai" in html
        assert "no Claude API call" in html or "no API call" in html
        # Prompt rendered in a <pre> block (preserves structure)
        assert "<pre" in html
        # Mode-indicator line in the meta bar
        assert "prompt-only" in html.lower() and "$0.00" in html

    def test_api_mode_html_does_not_show_prompt_only_banner(self):
        """Regression guard: normal analyst-brief emails must NOT show the
        prompt-only banner or subtitle."""
        from alert_groups.dispatcher import build_html_email
        html = build_html_email(
            "demo_group",
            "analyst brief body",
            {
                "searches_used": ["foo"],
                "estimated_tokens": 1234,
                "actual_tokens": 500,
                "cost_usd": 0.0015,
            },
        )
        assert "Prompt-only delivery" not in html
        assert "Prompt-Only Delivery" not in html
        assert "Analyst Brief" in html

    def test_payload_builder_build_user_content_matches_build(
        self, sample_result_a, sample_result_b,
    ):
        """Contract: the string emailed in prompt_only mode is EXACTLY the
        string the API path sends as messages[0].content. Drift between
        the two would mean the manual-paste copy differs from what the
        scheduled API fire would have sent."""
        from alert_groups.builder import PayloadBuilder
        pb = PayloadBuilder()
        results = [sample_result_a, sample_result_b]
        msgs = pb.build("grp", results, "Analyze")
        content = pb.build_user_content("grp", results, "Analyze")
        assert msgs[0]["content"] == content

    def test_api_mode_is_default_when_delivery_mode_absent(
        self, sample_group, sample_result_a,
    ):
        """Back-compat: AGs without a delivery_mode key continue to hit the
        Claude API + analyst-brief email path unchanged."""
        sample_group.pop("delivery_mode", None)
        d = self._make_dispatcher()
        d.serializer.serialize.return_value = sample_result_a

        result = self._fake_claude_result(text="brief")
        with patch("alert_groups.dispatcher.call_messages_create", return_value=result) as mock_claude:
            with patch.object(type(d), "_send_html_email") as mock_email:
                with patch.object(type(d), "_log_run"):
                    with patch.object(type(d), "_get_budget_gate", return_value=None):
                        run = d.run(group=sample_group)

        mock_claude.assert_called_once()
        mock_email.assert_called_once()
        assert run.status == "success"
        # Classic REPORT subject (not PROMPT)
        kwargs = mock_email.call_args.kwargs
        assert kwargs.get("subject", "").startswith("[SpeakesQuery REPORT] polymarket_daily")


# =====================================================================
# AlertGroupScheduler
# =====================================================================

class TestAlertGroupScheduler:

    def test_enabled_groups_with_schedule_are_registered(self, sample_group):
        mock_scheduler = MagicMock()
        with patch("alert_group_store.AlertGroupStore") as MockStore:
            store_instance = MockStore.return_value
            store_instance.list_groups.return_value = [sample_group]
            store_instance.initialize.return_value = None

            from alert_groups.scheduler import register_alert_group_jobs
            register_alert_group_jobs(mock_scheduler)

        mock_scheduler.add_job.assert_called_once()

    def test_groups_without_schedule_not_registered(self, sample_group):
        sample_group["schedule"] = ""
        mock_scheduler = MagicMock()
        with patch("alert_group_store.AlertGroupStore") as MockStore:
            store_instance = MockStore.return_value
            store_instance.list_groups.return_value = [sample_group]
            store_instance.initialize.return_value = None

            from alert_groups.scheduler import register_alert_group_jobs
            register_alert_group_jobs(mock_scheduler)

        mock_scheduler.add_job.assert_not_called()

    def test_disabled_groups_not_registered(self, sample_group):
        sample_group["disabled"] = True
        mock_scheduler = MagicMock()
        with patch("alert_group_store.AlertGroupStore") as MockStore:
            store_instance = MockStore.return_value
            store_instance.list_groups.return_value = [sample_group]
            store_instance.initialize.return_value = None

            from alert_groups.scheduler import register_alert_group_jobs
            register_alert_group_jobs(mock_scheduler)

        mock_scheduler.add_job.assert_not_called()


# =====================================================================
# REST API Endpoints (via Flask test client)
# =====================================================================

class TestAlertGroupAPI:

    @pytest.fixture(autouse=True)
    def setup_client(self, tmp_dir):
        """Set up Flask test client with isolated stores.

        Saves and restores the original singleton paths so subsequent tests
        in the session (e.g. tier5 YAML alert-group tests) don't inherit a
        pointer to this fixture's torn-down ``tmp_dir``.
        """
        # The feeder-status endpoint touches the scheduled input engine, so
        # the engine must be running even when the rest of the AG-API tests
        # don't need it.  ``start_engine`` is idempotent.
        from scheduled_input_engine import start_engine
        start_engine()

        from desktop_app.server import app, _bp_store, _ag_store
        original = {
            "bp_dir": _bp_store._dir,
            "bp_db": _bp_store._db,
            "ag_dir": _ag_store._dir,
            "ag_db": _ag_store._db,
            "ag_runs_db": _ag_store._runs_db,
        }

        # Redirect stores to temp directories
        _bp_store._dir = Path(tmp_dir) / "bp"
        _bp_store._db = str(Path(tmp_dir) / "lc.sqlite")
        _bp_store.initialize()

        _ag_store._dir = Path(tmp_dir) / "ag"
        _ag_store._db = str(Path(tmp_dir) / "lc.sqlite")
        _ag_store._runs_db = str(Path(tmp_dir) / "runs.sqlite")
        _ag_store.initialize()

        app.config["TESTING"] = True
        self.client = app.test_client()

        try:
            yield
        finally:
            _bp_store._dir = original["bp_dir"]
            _bp_store._db = original["bp_db"]
            _bp_store.initialize()
            _ag_store._dir = original["ag_dir"]
            _ag_store._db = original["ag_db"]
            _ag_store._runs_db = original["ag_runs_db"]
            _ag_store.initialize()

    # -- Boilerplate Prompts --

    def test_bp_list(self):
        resp = self.client.get("/api/boilerplate-prompts/list")
        data = json.loads(resp.data)
        assert data["status"] == "success"
        assert isinstance(data["prompts"], list)

    def test_bp_create_and_get(self):
        resp = self.client.post(
            "/api/boilerplate-prompts/create",
            data=json.dumps({"name": "api_test", "template": "Hello {group_name}"}),
            content_type="application/json",
        )
        data = json.loads(resp.data)
        assert data["status"] == "success"
        assert data["prompt"]["name"] == "api_test"

        resp = self.client.get("/api/boilerplate-prompts/api_test")
        data = json.loads(resp.data)
        assert data["status"] == "success"
        assert data["prompt"]["template"] == "Hello {group_name}"

    def test_bp_create_missing_fields(self):
        resp = self.client.post(
            "/api/boilerplate-prompts/create",
            data=json.dumps({"name": "incomplete"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_bp_update(self):
        self.client.post(
            "/api/boilerplate-prompts/create",
            data=json.dumps({"name": "upd", "template": "old"}),
            content_type="application/json",
        )
        resp = self.client.put(
            "/api/boilerplate-prompts/upd",
            data=json.dumps({"template": "new"}),
            content_type="application/json",
        )
        data = json.loads(resp.data)
        assert data["prompt"]["template"] == "new"

    def test_bp_delete(self):
        self.client.post(
            "/api/boilerplate-prompts/create",
            data=json.dumps({"name": "del_bp", "template": "x"}),
            content_type="application/json",
        )
        resp = self.client.delete("/api/boilerplate-prompts/del_bp")
        data = json.loads(resp.data)
        assert data["status"] == "success"

        resp = self.client.get("/api/boilerplate-prompts/del_bp")
        assert resp.status_code == 404

    def test_bp_get_yaml(self):
        self.client.post(
            "/api/boilerplate-prompts/create",
            data=json.dumps({"name": "yaml_bp", "template": "raw text"}),
            content_type="application/json",
        )
        resp = self.client.get("/api/boilerplate-prompts/yaml_bp/yaml")
        data = json.loads(resp.data)
        assert data["status"] == "success"
        assert "yaml_bp" in data["yaml"]

    # -- Alert Groups --

    def test_ag_list(self):
        resp = self.client.get("/api/alert-groups/list")
        data = json.loads(resp.data)
        assert data["status"] == "success"
        assert isinstance(data["groups"], list)

    def test_ag_create_and_get(self):
        resp = self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({
                "name": "api_group",
                "search_names": ["s1", "s2"],
                "prompt_text": "Analyze these results",
                "email_address": "test@example.com",
            }),
            content_type="application/json",
        )
        data = json.loads(resp.data)
        assert data["status"] == "success"
        assert data["group"]["name"] == "api_group"

        resp = self.client.get("/api/alert-groups/api_group")
        data = json.loads(resp.data)
        assert data["status"] == "success"

    def test_ag_create_missing_fields(self):
        resp = self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({"name": "incomplete"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_ag_update(self):
        self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({
                "name": "upd_group",
                "search_names": ["s1"],
                "prompt_text": "Analyze these results",
            }),
            content_type="application/json",
        )
        resp = self.client.put(
            "/api/alert-groups/upd_group",
            data=json.dumps({"max_rows": 50}),
            content_type="application/json",
        )
        data = json.loads(resp.data)
        assert data["group"]["max_rows"] == 50

    def test_ag_delete(self):
        self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({
                "name": "del_group",
                "search_names": ["s1"],
                "prompt_text": "Analyze these results",
            }),
            content_type="application/json",
        )
        resp = self.client.delete("/api/alert-groups/del_group")
        data = json.loads(resp.data)
        assert data["status"] == "success"

    def test_ag_enable_disable(self):
        self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({
                "name": "toggle_group",
                "search_names": ["s1"],
                "prompt_text": "Analyze these results",
            }),
            content_type="application/json",
        )
        resp = self.client.post("/api/alert-groups/toggle_group/disable")
        data = json.loads(resp.data)
        assert data["group"]["disabled"] is True

        resp = self.client.post("/api/alert-groups/toggle_group/enable")
        data = json.loads(resp.data)
        assert data["group"]["disabled"] is False

    def test_ag_get_yaml(self):
        self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({
                "name": "yaml_group",
                "search_names": ["s1"],
                "prompt_text": "Analyze these results",
            }),
            content_type="application/json",
        )
        resp = self.client.get("/api/alert-groups/yaml_group/yaml")
        data = json.loads(resp.data)
        assert data["status"] == "success"
        assert "yaml_group" in data["yaml"]

    def test_ag_runs_empty(self):
        resp = self.client.get("/api/alert-groups/runs")
        data = json.loads(resp.data)
        assert data["status"] == "success"
        assert isinstance(data["runs"], list)

    def test_ag_get_nonexistent(self):
        resp = self.client.get("/api/alert-groups/nonexistent")
        assert resp.status_code == 404

    # -- Feeder status endpoint --

    def test_ag_feeder_status_missing_search(self):
        """
        Happy-path smoke test: create an alert group that references a
        saved search which doesn't exist. The feeder-status endpoint
        should resolve without raising and report state=missing_search
        for that feeder.
        """
        create_resp = self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({
                "name": "feeder_status_smoke",
                "search_names": ["ghost_search_xyz"],
                "prompt_text": "Analyze these results",
            }),
            content_type="application/json",
        )
        assert json.loads(create_resp.data)["status"] == "success"

        resp = self.client.get(
            "/api/alert-groups/feeder_status_smoke/feeder-status"
        )
        data = json.loads(resp.data)
        assert resp.status_code == 200
        assert data["status"] == "success"
        assert data["group_name"] == "feeder_status_smoke"
        assert len(data["feeders"]) == 1
        assert data["feeders"][0]["state"] == "missing_search"
        assert data["summary"]["overall"] == "missing_search"

    def test_ag_deploy_feeders_only_skips_when_nothing_to_do(self):
        """
        deploy-feeders against an AG whose feeders all have missing
        saved searches should return status=success with an empty
        `deployed` list and every feeder recorded under `skipped`.
        """
        self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({
                "name": "deploy_smoke",
                "search_names": ["ghost_search_xyz"],
                "prompt_text": "Analyze these results",
            }),
            content_type="application/json",
        )
        resp = self.client.post(
            "/api/alert-groups/deploy_smoke/deploy-feeders"
        )
        data = json.loads(resp.data)
        assert resp.status_code == 200
        assert data["status"] == "success"
        assert data["deployed"] == []
        assert data["installed"] == []
        assert len(data["skipped"]) == 1
        assert data["skipped"][0]["reason"] == "missing_search"

    def test_ag_deploy_feeders_uses_ag_aligned_cron(self, tmp_dir):
        """
        When an AG has a schedulable cron, newly-deployed ingestion tasks
        should fire 60 minutes before the AG so data is fresh at dispatch.
        Verify cron_source='ag_schedule_minus_60min' on the deployed entry.
        """
        import yaml as _yaml
        from pathlib import Path
        from unittest.mock import patch
        from desktop_app.server import _ss_store

        # Fully isolate _ss_store to tmp so we don't pollute real
        # saved_searches/ (not redirected by the class fixture).
        orig_dir = _ss_store._dir
        orig_defaults = _ss_store._defaults_dir
        _ss_store._dir = Path(tmp_dir) / "ss_dir"
        _ss_store._defaults_dir = Path(tmp_dir) / "ss_defaults"
        _ss_store._dir.mkdir(parents=True, exist_ok=True)
        _ss_store._defaults_dir.mkdir(parents=True, exist_ok=True)

        feed_name = "ag_test_feed_cron"
        template = {
            "name": feed_name,
            "query": 'index="indexes/testcron/feed_xyz/*.parquet" | head 1',
            "cron_schedule": "0 5,11 * * *",
            "lookback": "-1h", "trigger": "once",
            "email_address": "noreply@speakesquery.local", "send_email": "no",
            "disabled": False,
            "created_at": "2026-04-16T00:00:00",
            "updated_at": "2026-04-16T00:00:00",
        }
        (_ss_store._defaults_dir / f"{feed_name}.yaml").write_text(
            _yaml.safe_dump(template)
        )

        stub_script = {
            "id": "testcron_feed_xyz",
            "title": "Test cron feed",
            "description": "",
            "suggested_cron": "*/30 * * * *",
            "suggested_subdirectory": "testcron/feed_xyz",
            "suggested_overwrite": False,
            "api_url": "https://example.com/test",
            "code": "import pandas as pd\nGENERATE_RESULTS(pd.DataFrame({'_epoch':[0]}))",
            "requires_credentials": [],
            "trust_level": "sandboxed",
            "tags": [],
        }

        try:
            self.client.post(
                "/api/alert-groups/create",
                data=json.dumps({
                    "name": "cron_align_smoke",
                    "search_names": [feed_name],
                    "prompt_text": "Analyze",
                    "schedule": "0 6,12 * * *",
                }),
                content_type="application/json",
            )
            with patch("desktop_app.server._list_library_scripts",
                       return_value=[stub_script]), \
                 patch("desktop_app.server._get_library_script",
                       return_value=stub_script):
                resp = self.client.post(
                    "/api/alert-groups/cron_align_smoke/deploy-feeders"
                )
            data = json.loads(resp.data)
            assert data["status"] == "success"
            assert len(data["installed"]) == 1, data
            assert len(data["deployed"]) == 1, data
            d = data["deployed"][0]
            assert d["cron_source"] == "ag_schedule_minus_60min"
            assert d["cron_schedule"] == "0 5,11 * * *"
            assert d["ag_schedule"] == "0 6,12 * * *"
        finally:
            # Restore store + clean up any scheduled task we created
            _ss_store._dir = orig_dir
            _ss_store._defaults_dir = orig_defaults
            from scheduled_input_engine import get_engine
            try:
                for t in get_engine().store.list_scheduled_inputs():
                    if t.get("subdirectory") == "testcron/feed_xyz":
                        get_engine().delete_task(t["id"])
            except Exception:
                pass

    def test_ag_install_default_feeder_not_found(self):
        """install-default-feeder on a group+search combo with no default."""
        self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({
                "name": "inst_smoke",
                "search_names": ["not_a_default"],
                "prompt_text": "Analyze",
            }),
            content_type="application/json",
        )
        resp = self.client.post(
            "/api/alert-groups/inst_smoke/install-default-feeder/not_a_default"
        )
        assert resp.status_code == 404
        assert json.loads(resp.data)["status"] == "error"

    def test_ag_install_default_feeder_missing_group(self):
        resp = self.client.post(
            "/api/alert-groups/nonexistent_abc/install-default-feeder/x"
        )
        assert resp.status_code == 404

    # -- Pipeline-health endpoint --

    def test_ag_pipeline_health_returns_query_fields(self):
        """
        /pipeline-health is /feeder-status plus per-feeder query execution.
        With an AG that references a missing saved search, every feeder
        should carry the extra fields (query_row_count, query_error,
        query_columns, fresh_row_count) - even if they're null/empty
        because the query couldn't be run.
        """
        self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({
                "name": "pipeline_health_smoke",
                "search_names": ["ghost_search_abc"],
                "prompt_text": "Analyze",
            }),
            content_type="application/json",
        )
        resp = self.client.get(
            "/api/alert-groups/pipeline_health_smoke/pipeline-health"
        )
        data = json.loads(resp.data)
        assert resp.status_code == 200
        assert data["status"] == "success"
        assert len(data["feeders"]) == 1
        f = data["feeders"][0]
        # Extra fields must be present in the shape even if query wasn't run
        for key in ("query_row_count", "query_error",
                    "query_columns", "fresh_row_count"):
            assert key in f, f"missing expected field {key}"
        # missing_search feeder shouldn't have query attempted
        assert f["state"] == "missing_search"
        assert f["query_row_count"] is None

    # -- Dispatch dry-run --

    def test_ag_run_dry_run_builds_preview_without_claude(self):
        """
        POST /run?dry_run=true should return status=success with a
        preview payload, even without an ANTHROPIC_API_KEY.  No
        Claude call, no email.
        """
        from unittest.mock import patch
        from alert_groups.models import SerializedResult

        self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({
                "name": "dry_run_smoke",
                "search_names": ["any_search"],
                "prompt_text": "Analyze the data",
            }),
            content_type="application/json",
        )

        # Stub the serializer so we don't need a real saved search result
        fake_sr = SerializedResult(
            search_name="any_search",
            row_count=3,
            estimated_tokens=42,
            format="json",
            content='[{"x": 1}, {"x": 2}, {"x": 3}]',
        )
        with patch(
            "alert_groups.serializer.ResultSerializer.serialize",
            return_value=fake_sr,
        ):
            resp = self.client.post(
                "/api/alert-groups/dry_run_smoke/run?dry_run=true"
            )
        data = json.loads(resp.data)
        assert resp.status_code == 200
        assert data["status"] == "success"
        assert data["dry_run"] is True
        assert data["run"]["status"] == "dry_run"
        assert data["run"]["actual_tokens"] == 0
        assert data["run"]["cost_usd"] == 0.0
        # Preview must carry the message payload that WOULD have been sent
        assert data["preview"] is not None
        messages = data["preview"]["messages"]
        assert isinstance(messages, list) and len(messages) == 1
        assert messages[0]["role"] == "user"
        assert "Analyze the data" in messages[0]["content"]
        assert data["preview"]["searches_used"] == ["any_search"]

    # -- Trust level plumbing on /api/si/test-code --

    def test_si_test_code_forwards_trust_level(self):
        """
        Regression for the silent-sandboxing bug: when the UI sends
        trust_level=unrestricted, the handler must forward it to
        engine.test_task so the script runs with plain exec().  This is
        what lets pro-tier scripts (tuple-unpack, underscore names,
        scipy/sklearn imports) pass the mandatory test gate.
        """
        from unittest.mock import patch
        # Code uses a `for a, b in pairs:` tuple-unpack which fails under
        # RestrictedPython's _iter_unpack_sequence_ rule but works under
        # unrestricted.  A passing result under trust_level=unrestricted
        # proves the handler forwarded the kwarg.
        code = (
            "import pandas as pd\n"
            "pairs = [(1, 'a'), (2, 'b'), (3, 'c')]\n"
            "rows = []\n"
            "for x, y in pairs:\n"
            "    rows.append({'_epoch': x, 'label': y})\n"
            "df = pd.DataFrame(rows)\n"
            "GENERATE_RESULTS(df)\n"
        )

        # Capture the kwargs passed to engine.test_task
        captured = {}

        def fake_test_task(c, task_id=0, **kw):
            captured.update(kw)
            return {"status": "pass", "errors": [], "row_count": 3,
                    "columns": ["_epoch", "label"], "has_epoch": True}

        from desktop_app.server import _get_engine
        engine = _get_engine()
        with patch.object(engine, "test_task", side_effect=fake_test_task):
            resp = self.client.post(
                "/api/si/test-code",
                data=json.dumps({"code": code,
                                 "trust_level": "unrestricted"}),
                content_type="application/json",
            )
        data = json.loads(resp.data)
        assert resp.status_code == 200
        assert data["status"] == "success"
        assert captured.get("trust_level") == "unrestricted", (
            f"trust_level was not forwarded to engine.test_task; "
            f"captured kwargs: {captured}"
        )

    def test_si_test_code_defaults_when_trust_level_omitted(self):
        """When the caller omits trust_level, the kwarg must NOT be forced -
        engine.test_task's existing 'sandboxed' default should apply."""
        from unittest.mock import patch
        captured = {}

        def fake_test_task(c, task_id=0, **kw):
            captured.update(kw)
            return {"status": "pass", "errors": [], "row_count": 0,
                    "columns": [], "has_epoch": True}

        from desktop_app.server import _get_engine
        engine = _get_engine()
        with patch.object(engine, "test_task", side_effect=fake_test_task):
            resp = self.client.post(
                "/api/si/test-code",
                data=json.dumps({"code": "GENERATE_RESULTS(1)"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        assert "trust_level" not in captured, (
            "handler should not pass trust_level when client omits it"
        )

    def test_ag_run_without_dry_run_flag_is_unchanged(self):
        """
        Without dry_run=true, the endpoint should behave as before -
        attempt dispatch, fail gracefully on missing searches.
        """
        self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({
                "name": "normal_run_smoke",
                "search_names": ["ghost"],
                "prompt_text": "Analyze",
            }),
            content_type="application/json",
        )
        resp = self.client.post("/api/alert-groups/normal_run_smoke/run")
        data = json.loads(resp.data)
        assert data["dry_run"] is False
        assert data["run"]["status"] == "error"  # no results available

    # ─── delivery_mode REST round-trip (2026-04-22) ───────────────────

    def test_ag_create_with_prompt_only_mode_persists(self):
        """Creating an AG with delivery_mode=prompt_only persists the field
        and surfaces it in subsequent GETs. The Settings UI's delivery-mode
        badge on the list page depends on this field being on the wire."""
        resp = self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({
                "name": "budget_brief",
                "search_names": ["s1"],
                "prompt_text": "Analyze me",
                "email_address": "budget@example.com",
                "delivery_mode": "prompt_only",
            }),
            content_type="application/json",
        )
        data = json.loads(resp.data)
        assert data["status"] == "success"
        assert data["group"]["delivery_mode"] == "prompt_only"

        # GET round-trip
        resp = self.client.get("/api/alert-groups/budget_brief")
        data = json.loads(resp.data)
        assert data["group"]["delivery_mode"] == "prompt_only"

        # list includes the field so the UI badge can render
        resp = self.client.get("/api/alert-groups/list")
        data = json.loads(resp.data)
        matched = [g for g in data["groups"] if g["name"] == "budget_brief"]
        assert matched and matched[0]["delivery_mode"] == "prompt_only"

    def test_ag_create_prompt_only_without_email_returns_400(self):
        resp = self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({
                "name": "budget_no_email",
                "search_names": ["s1"],
                "prompt_text": "Analyze",
                "delivery_mode": "prompt_only",
                # email_address deliberately omitted
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["status"] == "error"
        assert "email_address" in data["message"]

    def test_ag_create_invalid_delivery_mode_returns_400(self):
        resp = self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({
                "name": "bad_mode",
                "search_names": ["s1"],
                "prompt_text": "Analyze",
                "email_address": "x@y.com",
                "delivery_mode": "telepathy",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_ag_update_delivery_mode_api_to_prompt_only(self):
        """Mode switching via PUT works when the AG already has an email."""
        self.client.post(
            "/api/alert-groups/create",
            data=json.dumps({
                "name": "mode_swap",
                "search_names": ["s1"],
                "prompt_text": "Analyze",
                "email_address": "test@example.com",
                "delivery_mode": "api",
            }),
            content_type="application/json",
        )
        resp = self.client.put(
            "/api/alert-groups/mode_swap",
            data=json.dumps({"delivery_mode": "prompt_only"}),
            content_type="application/json",
        )
        data = json.loads(resp.data)
        assert data["status"] == "success"
        assert data["group"]["delivery_mode"] == "prompt_only"


# ─────────────────────────────────────────────────────────────────────────────
# SavedSearchStore - default feeder seeding / install
# ─────────────────────────────────────────────────────────────────────────────

class TestSavedSearchStoreDefaults:
    """
    Exercises the project-shipped `default_saved_searches/` seeding and
    on-demand install paths added for the Feeder Health feature.
    """

    def _make_store(self, tmp_path):
        from saved_search_store import SavedSearchStore
        store = SavedSearchStore()
        store._dir = tmp_path / "saved_searches"
        store._defaults_dir = tmp_path / "default_saved_searches"
        store._db = str(tmp_path / "lc.sqlite")
        return store

    def _write_default(self, store, name, **overrides):
        store._defaults_dir.mkdir(parents=True, exist_ok=True)
        body = {
            "name": name,
            "description": "test default",
            "query": f'index="indexes/test/{name}/*.parquet" | head 1',
            "cron_schedule": "*/30 * * * *",
            "lookback": "-1h",
            "trigger": "once",
            "email_address": "noreply@speakesquery.local",
            "send_email": "no",
            "disabled": False,
            "created_at": "2026-04-16T00:00:00",
            "updated_at": "2026-04-16T00:00:00",
        }
        body.update(overrides)
        import yaml
        (store._defaults_dir / f"{name}.yaml").write_text(yaml.safe_dump(body))

    def test_list_defaults_empty_when_no_dir(self, tmp_path):
        store = self._make_store(tmp_path)
        assert store.list_defaults() == []
        assert store.has_default("anything") is False

    def test_list_defaults_returns_sorted_names(self, tmp_path):
        store = self._make_store(tmp_path)
        self._write_default(store, "ag_z_last")
        self._write_default(store, "ag_a_first")
        assert store.list_defaults() == ["ag_a_first", "ag_z_last"]
        assert store.has_default("ag_a_first") is True

    def test_seed_defaults_copies_missing_on_initialize(self, tmp_path):
        store = self._make_store(tmp_path)
        self._write_default(store, "dob_poly_high_prob")
        self._write_default(store, "dob_crypto_anomalies")
        store.initialize()

        names = sorted(s["name"] for s in store.list_searches())
        assert names == ["dob_crypto_anomalies", "dob_poly_high_prob"]

    def test_seed_defaults_is_idempotent(self, tmp_path):
        store = self._make_store(tmp_path)
        self._write_default(store, "dob_poly_high_prob")
        store.initialize()
        first_mtime = (store._dir / "dob_poly_high_prob.yaml").stat().st_mtime
        # Calling again should NOT re-copy and clobber user edits
        store.initialize()
        assert (store._dir / "dob_poly_high_prob.yaml").stat().st_mtime == first_mtime

    def test_seed_defaults_respects_user_edits(self, tmp_path):
        store = self._make_store(tmp_path)
        self._write_default(store, "dob_poly_high_prob")
        store.initialize()
        # Simulate user editing the seeded copy
        user_path = store._dir / "dob_poly_high_prob.yaml"
        user_path.write_text("name: dob_poly_high_prob\nquery: 'user edited'\n")
        store.initialize()  # Re-seed should NOT overwrite
        assert "user edited" in user_path.read_text()

    def test_install_default_copies_template(self, tmp_path):
        store = self._make_store(tmp_path)
        self._write_default(store, "dob_kalshi_poly_arb")
        # Fresh init with defaults - seed already copies; delete user copy
        # so install_default has work to do.
        store.initialize()
        (store._dir / "dob_kalshi_poly_arb.yaml").unlink()

        result = store.install_default("dob_kalshi_poly_arb")
        assert result["name"] == "dob_kalshi_poly_arb"
        assert (store._dir / "dob_kalshi_poly_arb.yaml").exists()

    def test_install_default_raises_when_missing(self, tmp_path):
        store = self._make_store(tmp_path)
        store.initialize()
        with pytest.raises(FileNotFoundError):
            store.install_default("no_such_default")

    def test_install_default_raises_when_already_exists(self, tmp_path):
        store = self._make_store(tmp_path)
        self._write_default(store, "dob_macro_regime")
        store.initialize()  # Seeds the file
        with pytest.raises(FileExistsError):
            store.install_default("dob_macro_regime")


# ─────────────────────────────────────────────────────────────────────────────
# FeederStatus resolver
# ─────────────────────────────────────────────────────────────────────────────

class TestFeederStatusResolver:
    """
    Unit tests for alert_groups.feeder_status - pure-function resolver with
    injected loaders, so no Flask / filesystem / vault fixtures required
    beyond a tmp_path for parquet data checks.
    """

    @staticmethod
    def _lib_script(
        script_id="fred_fear_gauges_pro",
        subdir="macro/fred_fear_gauges_pro",
        requires=None,
    ):
        return {
            "id": script_id,
            "title": "Mock",
            "suggested_subdirectory": subdir,
            "requires_credentials": list(requires or []),
        }

    @staticmethod
    def _task(task_id=42, subdir="macro/fred_fear_gauges_pro", disabled=False):
        return {"id": task_id, "subdirectory": subdir, "disabled": disabled}

    @staticmethod
    def _saved_search(name, query):
        return lambda n: (
            {"name": name, "query": query} if n == name
            else (_ for _ in ()).throw(FileNotFoundError(n))
        )

    # ── derive_pre_cron ────────────────────────────────────────

    def test_derive_pre_cron_twice_daily(self):
        """AG fires at 06:00 and 12:00 → ingestion fires at 05:00 and 11:00."""
        from alert_groups.feeder_status import derive_pre_cron
        assert derive_pre_cron("0 6,12 * * *", 60) == "0 5,11 * * *"

    def test_derive_pre_cron_single_hour(self):
        from alert_groups.feeder_status import derive_pre_cron
        assert derive_pre_cron("30 8 * * *", 60) == "30 7 * * *"

    def test_derive_pre_cron_offset_90(self):
        """90-min offset rolls minute + decrements hour by 2."""
        from alert_groups.feeder_status import derive_pre_cron
        assert derive_pre_cron("0 6 * * *", 90) == "30 4 * * *"

    def test_derive_pre_cron_midnight_returns_none(self):
        """Cron at 00:00 can't shift back without crossing day boundary."""
        from alert_groups.feeder_status import derive_pre_cron
        assert derive_pre_cron("0 0 * * *", 60) is None

    def test_derive_pre_cron_every_30min_returns_none(self):
        from alert_groups.feeder_status import derive_pre_cron
        assert derive_pre_cron("*/30 * * * *", 60) is None

    def test_derive_pre_cron_hour_wildcard_returns_none(self):
        from alert_groups.feeder_status import derive_pre_cron
        assert derive_pre_cron("30 * * * *", 60) is None

    def test_derive_pre_cron_range_returns_none(self):
        from alert_groups.feeder_status import derive_pre_cron
        assert derive_pre_cron("0 6-8 * * *", 60) is None

    def test_derive_pre_cron_empty_returns_none(self):
        from alert_groups.feeder_status import derive_pre_cron
        assert derive_pre_cron("", 60) is None
        assert derive_pre_cron(None, 60) is None

    def test_derive_pre_cron_malformed_returns_none(self):
        from alert_groups.feeder_status import derive_pre_cron
        assert derive_pre_cron("0 6 *", 60) is None  # wrong field count
        assert derive_pre_cron("abc def * * *", 60) is None  # non-numeric

    def test_derive_pre_cron_preserves_dom_dow(self):
        from alert_groups.feeder_status import derive_pre_cron
        # Weekday-only AG cron
        assert derive_pre_cron("0 9 * * 1-5", 60) == "0 8 * * 1-5"

    def test_extract_index_paths_single(self):
        from alert_groups.feeder_status import extract_index_paths
        q = 'index="indexes/polymarket/high_probability_pro/*.parquet" | head 5'
        assert extract_index_paths(q) == [
            "indexes/polymarket/high_probability_pro/*.parquet"
        ]

    def test_extract_index_paths_multiple(self):
        from alert_groups.feeder_status import extract_index_paths
        q = (
            'multisearch '
            '[index="indexes/a/b/*.parquet" | head 5] '
            '[index="indexes/c/d/*" | head 5]'
        )
        assert extract_index_paths(q) == [
            "indexes/a/b/*.parquet",
            "indexes/c/d/*",
        ]

    def test_extract_index_paths_empty(self):
        from alert_groups.feeder_status import extract_index_paths
        assert extract_index_paths("") == []
        assert extract_index_paths("| head 5") == []

    def test_normalize_subdirectory_variants(self):
        from alert_groups.feeder_status import _normalize_subdirectory
        assert _normalize_subdirectory(
            "indexes/polymarket/high_probability_pro/*.parquet"
        ) == "polymarket/high_probability_pro"
        assert _normalize_subdirectory("indexes/sec/major_filings/*") == "sec/major_filings"
        assert _normalize_subdirectory("indexes/github/public_events") == "github/public_events"
        assert _normalize_subdirectory("github/public_events/") == "github/public_events"

    def test_resolve_missing_search(self):
        from alert_groups.feeder_status import resolve_feeder

        def loader(_):
            raise FileNotFoundError("nope")

        fs = resolve_feeder(
            "ghost",
            saved_search_loader=loader,
            library_scripts=[],
            scheduled_tasks=[],
            credentials_lister=lambda _: [],
            indexes_root="/tmp/does-not-exist",
        )
        assert fs.state == "missing_search"
        assert fs.installable is False

    def test_resolve_missing_search_installable(self):
        """Missing search + default template available -> installable=True."""
        from alert_groups.feeder_status import resolve_feeder

        def loader(_):
            raise FileNotFoundError("nope")

        fs = resolve_feeder(
            "dob_poly_high_prob",
            saved_search_loader=loader,
            library_scripts=[],
            scheduled_tasks=[],
            credentials_lister=lambda _: [],
            indexes_root="/tmp/does-not-exist",
            default_search_names=["dob_poly_high_prob", "ag_other"],
        )
        assert fs.state == "missing_search"
        assert fs.installable is True
        assert "Install" in fs.message

    def test_resolve_unknown_index(self, tmp_path):
        from alert_groups.feeder_status import resolve_feeder

        loader = self._saved_search("noidx", "| head 1")
        fs = resolve_feeder(
            "noidx",
            saved_search_loader=loader,
            library_scripts=[],
            scheduled_tasks=[],
            credentials_lister=lambda _: [],
            indexes_root=tmp_path,
        )
        assert fs.state == "unknown_index"
        assert fs.index_paths == []

    def test_resolve_no_library_script_user_managed_live(self, tmp_path):
        """User-managed index (no library match) with data → live with note."""
        from alert_groups.feeder_status import resolve_feeder

        subdir = tmp_path / "custom" / "mydata"
        subdir.mkdir(parents=True)
        (subdir / "a.parquet").write_bytes(b"x")

        loader = self._saved_search(
            "custom_feed", 'index="indexes/custom/mydata/*.parquet" | head 1'
        )
        fs = resolve_feeder(
            "custom_feed",
            saved_search_loader=loader,
            library_scripts=[],
            scheduled_tasks=[],
            credentials_lister=lambda _: [],
            indexes_root=tmp_path,
        )
        assert fs.state == "live"
        assert fs.data_file_count == 1

    def test_resolve_no_library_script_user_managed_empty(self, tmp_path):
        from alert_groups.feeder_status import resolve_feeder

        loader = self._saved_search(
            "custom_feed", 'index="indexes/custom/mydata/*.parquet" | head 1'
        )
        fs = resolve_feeder(
            "custom_feed",
            saved_search_loader=loader,
            library_scripts=[],
            scheduled_tasks=[],
            credentials_lister=lambda _: [],
            indexes_root=tmp_path,
        )
        assert fs.state == "no_library_script"

    def test_resolve_needs_deploy(self, tmp_path):
        from alert_groups.feeder_status import resolve_feeder

        loader = self._saved_search(
            "fred_feed",
            'index="indexes/macro/fred_fear_gauges_pro/*.parquet" | head 1',
        )
        fs = resolve_feeder(
            "fred_feed",
            saved_search_loader=loader,
            library_scripts=[self._lib_script(requires=["FRED_API_KEY"])],
            scheduled_tasks=[],  # not deployed
            credentials_lister=lambda _: [],
            indexes_root=tmp_path,
        )
        assert fs.state == "needs_deploy"
        assert fs.library_script_id == "fred_fear_gauges_pro"
        assert fs.required_credentials == ["FRED_API_KEY"]

    def test_resolve_disabled(self, tmp_path):
        from alert_groups.feeder_status import resolve_feeder

        loader = self._saved_search(
            "fred_feed",
            'index="indexes/macro/fred_fear_gauges_pro/*.parquet" | head 1',
        )
        fs = resolve_feeder(
            "fred_feed",
            saved_search_loader=loader,
            library_scripts=[self._lib_script()],
            scheduled_tasks=[self._task(disabled=True)],
            credentials_lister=lambda _: [],
            indexes_root=tmp_path,
        )
        assert fs.state == "disabled"
        assert fs.task_id == 42

    def test_resolve_needs_creds(self, tmp_path):
        from alert_groups.feeder_status import resolve_feeder

        loader = self._saved_search(
            "fred_feed",
            'index="indexes/macro/fred_fear_gauges_pro/*.parquet" | head 1',
        )
        fs = resolve_feeder(
            "fred_feed",
            saved_search_loader=loader,
            library_scripts=[self._lib_script(requires=["FRED_API_KEY"])],
            scheduled_tasks=[self._task()],
            credentials_lister=lambda _: [],  # no creds stored
            indexes_root=tmp_path,
        )
        assert fs.state == "needs_creds"
        assert fs.missing_credentials == ["FRED_API_KEY"]

    def test_resolve_pending(self, tmp_path):
        from alert_groups.feeder_status import resolve_feeder

        # No parquet files under tmp_path → pending
        loader = self._saved_search(
            "feed",
            'index="indexes/macro/fred_fear_gauges_pro/*.parquet" | head 1',
        )
        fs = resolve_feeder(
            "feed",
            saved_search_loader=loader,
            library_scripts=[self._lib_script(requires=["FRED_API_KEY"])],
            scheduled_tasks=[self._task()],
            credentials_lister=lambda _: ["FRED_API_KEY"],
            indexes_root=tmp_path,
        )
        assert fs.state == "pending"

    def test_resolve_live(self, tmp_path):
        from alert_groups.feeder_status import resolve_feeder

        # Seed a parquet file under the expected index path
        sub = tmp_path / "macro" / "fred_fear_gauges_pro"
        sub.mkdir(parents=True)
        (sub / "part-000.parquet").write_bytes(b"data")

        loader = self._saved_search(
            "feed",
            'index="indexes/macro/fred_fear_gauges_pro/*.parquet" | head 1',
        )
        fs = resolve_feeder(
            "feed",
            saved_search_loader=loader,
            library_scripts=[self._lib_script(requires=["FRED_API_KEY"])],
            scheduled_tasks=[self._task()],
            credentials_lister=lambda _: ["FRED_API_KEY"],
            indexes_root=tmp_path,
        )
        assert fs.state == "live"
        assert fs.data_file_count == 1
        assert fs.last_data_epoch is not None

    def test_summarize_picks_worst_state(self):
        from alert_groups.feeder_status import summarize, FeederStatus
        feeders = [
            FeederStatus(search_name="a", state="live"),
            FeederStatus(search_name="b", state="needs_creds"),
            FeederStatus(search_name="c", state="pending"),
        ]
        s = summarize(feeders)
        assert s["overall"] == "needs_creds"
        assert s["counts"]["live"] == 1
        assert s["total"] == 3

    def test_resolve_alert_group_end_to_end(self, tmp_path):
        from alert_groups.feeder_status import resolve_alert_group

        # Seed one live feeder, declare one undeployed feeder
        sub = tmp_path / "polymarket" / "high_probability_pro"
        sub.mkdir(parents=True)
        (sub / "p.parquet").write_bytes(b"d")

        searches = {
            "ag_poly_hi": {
                "name": "ag_poly_hi",
                "query": 'index="indexes/polymarket/high_probability_pro/*.parquet" | head 1',
            },
            "ag_fred": {
                "name": "ag_fred",
                "query": 'index="indexes/macro/fred_fear_gauges_pro/*.parquet" | head 1',
            },
        }

        def loader(name):
            if name not in searches:
                raise FileNotFoundError(name)
            return searches[name]

        library_scripts = [
            self._lib_script(
                script_id="polymarket_high_probability_pro",
                subdir="polymarket/high_probability_pro",
            ),
            self._lib_script(
                script_id="fred_fear_gauges_pro",
                subdir="macro/fred_fear_gauges_pro",
                requires=["FRED_API_KEY"],
            ),
        ]
        # Only the polymarket one is deployed
        tasks = [self._task(task_id=1, subdir="polymarket/high_probability_pro")]

        result = resolve_alert_group(
            {"name": "test_ag", "search_names": ["ag_poly_hi", "ag_fred"]},
            saved_search_loader=loader,
            library_scripts=library_scripts,
            scheduled_tasks=tasks,
            credentials_lister=lambda _: [],
            indexes_root=tmp_path,
        )
        states = [f["state"] for f in result["feeders"]]
        assert states == ["live", "needs_deploy"]
        assert result["summary"]["overall"] == "needs_deploy"
        assert result["summary"]["counts"]["live"] == 1
        assert result["summary"]["counts"]["needs_deploy"] == 1

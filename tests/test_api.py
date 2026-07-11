#!/usr/bin/env python3
"""
API Test Framework - parametrized YAML-driven test runner for REST endpoints.

Discovers all YAML test definitions under tests/yaml/tier5_api/ and executes
them against the Flask test client, asserting expected HTTP responses.

CRUD lifecycle tests (ingestion, saved searches, macros) use dedicated
ordered test functions to guarantee create → read → update → delete sequencing.
"""

import json
import pytest
from tests.conftest import collect_all_yaml_tests, make_test_id


# ---------------------------------------------------------------------------
# Collect parametrized (stateless) API test cases
# ---------------------------------------------------------------------------

ALL_API_TESTS = collect_all_yaml_tests(subdir="tier5_api")


# ---------------------------------------------------------------------------
# Request dispatcher
# ---------------------------------------------------------------------------

def dispatch_request(client, tc):
    """Send an HTTP request based on the test case definition.

    Returns the Flask test response object.
    """
    method = tc.get("method", "GET").upper()
    path = tc["path"]
    body = tc.get("body")
    query_params = tc.get("query_params")
    headers = tc.get("headers", {})
    content_type = tc.get("content_type", "application/json")

    # Build query string
    query_string = query_params if query_params else None

    if method == "GET":
        return client.get(path, query_string=query_string, headers=headers)
    elif method == "POST":
        if content_type == "application/json":
            return client.post(
                path,
                data=json.dumps(body) if body else "{}",
                content_type="application/json",
                query_string=query_string,
                headers=headers,
            )
        else:
            return client.post(
                path,
                data=body,
                content_type=content_type,
                query_string=query_string,
                headers=headers,
            )
    elif method == "PUT":
        return client.put(
            path,
            data=json.dumps(body) if body else "{}",
            content_type="application/json",
            query_string=query_string,
            headers=headers,
        )
    elif method == "DELETE":
        return client.delete(
            path,
            data=json.dumps(body) if body else None,
            content_type="application/json" if body else None,
            query_string=query_string,
            headers=headers,
        )
    else:
        raise ValueError(f"Unsupported HTTP method: {method}")


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

def assert_status_code(resp, expect):
    """Assert the HTTP status code matches."""
    if "status_code" in expect:
        assert resp.status_code == expect["status_code"], (
            f"Expected HTTP {expect['status_code']}, got {resp.status_code}.\n"
            f"Body: {resp.get_data(as_text=True)[:500]}"
        )


def assert_json_status(data, expect):
    """Assert the JSON envelope status field."""
    if "json_status" in expect:
        actual = data.get("status")
        assert actual == expect["json_status"], (
            f"Expected json.status={expect['json_status']!r}, got {actual!r}"
        )


def assert_has_keys(data, expect):
    """Assert that specific keys exist in the JSON response."""
    if "has_keys" in expect:
        missing = [k for k in expect["has_keys"] if k not in data]
        assert not missing, (
            f"Missing expected keys: {missing}\n"
            f"Available keys: {list(data.keys())}"
        )


def assert_missing_keys(data, expect):
    """Assert that specific keys do NOT exist in the JSON response."""
    if "missing_keys" in expect:
        present = [k for k in expect["missing_keys"] if k in data]
        assert not present, f"Keys should not be present: {present}"


def assert_results(data, expect):
    """Assert result set size constraints."""
    if "min_results" in expect:
        key = _results_key(data)
        results = data.get(key, [])
        assert len(results) >= expect["min_results"], (
            f"Expected >= {expect['min_results']} results in '{key}', "
            f"got {len(results)}"
        )

    if "result_count" in expect:
        key = _results_key(data)
        results = data.get(key, [])
        assert len(results) == expect["result_count"], (
            f"Expected exactly {expect['result_count']} results in '{key}', "
            f"got {len(results)}"
        )


def assert_json_values(data, expect):
    """Assert specific key/value pairs in the JSON response."""
    if "json_values" not in expect:
        return
    for check in expect["json_values"]:
        key = check["key"]
        expected_val = check["value"]
        actual_val = data.get(key)
        assert actual_val == expected_val, (
            f"json['{key}']: expected {expected_val!r}, got {actual_val!r}"
        )


def assert_body_contains(resp, expect):
    """Assert the raw response body contains a substring."""
    if "body_contains" in expect:
        body = resp.get_data(as_text=True)
        assert expect["body_contains"] in body, (
            f"Response body does not contain {expect['body_contains']!r}.\n"
            f"Body: {body[:500]}"
        )


def assert_list_length(data, expect):
    """Assert the length of a named list in the response."""
    if "list_length" not in expect:
        return
    for check in expect["list_length"]:
        key = check["key"]
        assert key in data, f"Key '{key}' not in response"
        actual_len = len(data[key])
        if "min" in check:
            assert actual_len >= check["min"], (
                f"len(json['{key}']) = {actual_len}, expected >= {check['min']}"
            )
        if "exact" in check:
            assert actual_len == check["exact"], (
                f"len(json['{key}']) = {actual_len}, expected {check['exact']}"
            )


def _results_key(data):
    """Find the results list key in the response (results, tasks, searches, etc.)."""
    for key in ("results", "tasks", "searches", "macros", "files", "jobs",
                "keys", "history", "scripts"):
        if key in data and isinstance(data[key], list):
            return key
    return "results"


# ---------------------------------------------------------------------------
# Parametrized test - stateless endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tc",
    ALL_API_TESTS,
    ids=[make_test_id(tc) for tc in ALL_API_TESTS],
)
def test_api(client, tc):
    """Execute a single API test case defined in YAML."""
    resp = dispatch_request(client, tc)
    expect = tc.get("expect", {})

    assert_status_code(resp, expect)
    assert_body_contains(resp, expect)

    # Parse JSON for structured assertions
    if any(k in expect for k in (
        "json_status", "has_keys", "missing_keys", "min_results",
        "result_count", "json_values", "list_length",
    )):
        data = resp.get_json()
        assert data is not None, (
            f"Expected JSON response but got content-type "
            f"{resp.content_type}.\nBody: {resp.get_data(as_text=True)[:300]}"
        )
        assert_json_status(data, expect)
        assert_has_keys(data, expect)
        assert_missing_keys(data, expect)
        assert_results(data, expect)
        assert_json_values(data, expect)
        assert_list_length(data, expect)


# ---------------------------------------------------------------------------
# CRUD lifecycle tests - ordered sequences for stateful endpoints
# ---------------------------------------------------------------------------

class TestIngestionCRUD:
    """Create → read → update → toggle → delete lifecycle for ingestion scripts."""

    _task_id = None

    def test_create(self, client):
        resp = client.post("/api/si/add", json={
            "title": "pytest_api_test_script",
            "code": (
                "import pandas as pd\n"
                "df = pd.DataFrame({'x': [1, 2, 3], '_epoch': [1.0, 2.0, 3.0]})\n"
                "GENERATE_RESULTS(df)"
            ),
            "cron_schedule": "0 0 1 1 *",
            "description": "Automated API test - safe to delete",
            "subdirectory": "pytest_api_test",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert "task" in data
        TestIngestionCRUD._task_id = data["task"]["id"]

    def test_read(self, client):
        assert self._task_id is not None, "Create must run first"
        resp = client.get(f"/api/si/{self._task_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["task"]["title"] == "pytest_api_test_script"

    def test_update(self, client):
        assert self._task_id is not None
        resp = client.put(f"/api/si/{self._task_id}", json={
            "description": "Updated by pytest",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["task"]["description"] == "Updated by pytest"

    def test_toggle_disable(self, client):
        assert self._task_id is not None
        resp = client.post(f"/api/si/{self._task_id}/toggle", json={
            "enabled": False,
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_toggle_enable(self, client):
        assert self._task_id is not None
        resp = client.post(f"/api/si/{self._task_id}/toggle", json={
            "enabled": True,
        })
        assert resp.status_code == 200

    def test_test_run(self, client):
        assert self._task_id is not None
        resp = client.post(f"/api/si/{self._task_id}/test")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert "summary" in data

    def test_delete(self, client):
        assert self._task_id is not None
        resp = client.delete(f"/api/si/{self._task_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_read_after_delete(self, client):
        assert self._task_id is not None
        resp = client.get(f"/api/si/{self._task_id}")
        assert resp.status_code == 404


class TestSavedSearchCRUD:
    """Create → read → update → delete lifecycle for saved searches."""

    _name = "pytest_api_test_search"

    def test_create(self, client):
        resp = client.post("/api/ss/create", json={
            "name": self._name,
            "query": 'index="indexes/default_test/output_parquets/test0.parquet" | head 5',
            "cron_schedule": "0 0 1 1 *",
            "lookback": "-24h",
            "email_address": "test@example.com",
            "email_body": "Test alert: $level$",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_read(self, client):
        resp = client.get(f"/api/ss/{self._name}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["search"]["name"] == self._name

    def test_get_yaml(self, client):
        resp = client.get(f"/api/ss/{self._name}/yaml")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert "yaml" in data

    def test_update(self, client):
        resp = client.put(f"/api/ss/{self._name}", json={
            "lookback": "-48h",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_delete(self, client):
        resp = client.delete(f"/api/ss/{self._name}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_read_after_delete(self, client):
        resp = client.get(f"/api/ss/{self._name}")
        assert resp.status_code == 404


class TestMacroCRUD:
    """Create → read → expand → update → delete lifecycle for macros."""

    _name = "pytest_api_test_macro"

    def test_create(self, client):
        resp = client.post("/api/macros/create", json={
            "name": self._name,
            "definition": "search level=\"ERROR\"",
            "description": "Automated API test macro - safe to delete",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_read(self, client):
        resp = client.get(f"/api/macros/{self._name}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["macro"]["name"] == self._name

    def test_expand(self, client):
        resp = client.post("/api/macros/expand", json={
            "query": f'index="logs" | `{self._name}`',
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert "expanded" in data
        assert "search level" in data["expanded"]

    def test_update(self, client):
        resp = client.put(f"/api/macros/{self._name}", json={
            "description": "Updated by pytest",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_delete(self, client):
        resp = client.delete(f"/api/macros/{self._name}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_read_after_delete(self, client):
        resp = client.get(f"/api/macros/{self._name}")
        assert resp.status_code == 404


class TestCredentialsCRUD:
    """Store → list → delete lifecycle for credentials vault."""

    _script_id = 99999  # unlikely to collide with real scripts

    def test_store(self, client):
        resp = client.post(f"/api/credentials/{self._script_id}", json={
            "key_name": "pytest_test_key",
            "value": "pytest_test_value_secret",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_list(self, client):
        resp = client.get(f"/api/credentials/{self._script_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert "pytest_test_key" in data["keys"]

    def test_value_not_exposed(self, client):
        """Credential values must never appear in the list response."""
        resp = client.get(f"/api/credentials/{self._script_id}")
        body = resp.get_data(as_text=True)
        assert "pytest_test_value_secret" not in body

    def test_delete(self, client):
        resp = client.delete(f"/api/credentials/{self._script_id}/pytest_test_key")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_list_after_delete(self, client):
        resp = client.get(f"/api/credentials/{self._script_id}")
        data = resp.get_json()
        assert "pytest_test_key" not in data.get("keys", [])


class TestSettingsCRUD:
    """Read → update → reset lifecycle for global settings."""

    _original = None

    def test_read(self, client):
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert "settings" in data
        TestSettingsCRUD._original = data["settings"]

    def test_update(self, client):
        resp = client.post("/api/settings", json={
            "default_script_timeout_seconds": 300,
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_read_after_update(self, client):
        resp = client.get("/api/settings")
        data = resp.get_json()
        assert data["settings"]["default_script_timeout_seconds"] == 300

    def test_reset(self, client):
        resp = client.post("/api/settings/reset")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"


# ---------------------------------------------------------------------------
# Grammar Vocab API - backs the console autocomplete UI
# ---------------------------------------------------------------------------
#
# The vocab is derived from lexers/speakesQuery.g4 at server start. These
# tests verify the endpoint's shape so a grammar regen (or vocab extractor
# bug) fails loudly rather than silently breaking autocomplete.


class TestGrammarVocabAPI:
    def test_endpoint_returns_success(self, client):
        resp = client.get("/api/grammar/vocab")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert "vocab" in data

    def test_vocab_shape(self, client):
        resp = client.get("/api/grammar/vocab")
        vocab = resp.get_json()["vocab"]
        assert "version" in vocab
        for key in ("commands", "functions", "keywords", "operators",
                    "booleans", "time_units"):
            assert key in vocab, f"missing key {key}"

    def test_core_commands_present(self, client):
        resp = client.get("/api/grammar/vocab")
        vocab = resp.get_json()["vocab"]
        names = {c["name"] for c in vocab["commands"]}
        for core in ("search", "where", "eval", "stats", "head", "limit",
                     "sort", "table", "fields"):
            assert core in names, f"core command {core!r} missing from vocab"

    def test_core_functions_present(self, client):
        resp = client.get("/api/grammar/vocab")
        vocab = resp.get_json()["vocab"]
        names = {f["name"] for f in vocab["functions"]}
        for core in ("count", "values", "round", "concat", "match", "if_"):
            assert core in names, f"core function {core!r} missing from vocab"

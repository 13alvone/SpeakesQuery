"""Tests for ``lexers.grammar_vocab`` - the grammar-derived vocabulary
consumed by the console autocomplete UI and any future linter.

The vocab is a function of ``lexers/speakesQuery.g4``. These tests pin down
the contract so that if the grammar changes the vocab drifts noisily rather
than silently. Concretely:

* Every command listed in CLAUDE.md's "Supported commands" section must be
  present in ``vocab["commands"]``. If a new command is added to the
  grammar but not mentioned in CLAUDE.md (or vice versa) this catches the
  drift.
* Functions, keywords, and operators surface the expected canonical names.
* The extractor is idempotent - repeated calls return the same dict.
"""
from __future__ import annotations

from lexers.grammar_vocab import VOCAB_VERSION, get_vocab


# Commands documented in CLAUDE.md "Supported commands:" plus the three
# initial-clause commands that are listed separately. If the grammar or the
# docs drift, this test fails - update both to match the source of truth.
EXPECTED_COMMANDS = {
    # directives
    "search", "where", "eval", "stats", "eventstats", "streamstats",
    "timechart", "fields", "table", "rename", "sort", "reverse", "head",
    "limit", "dedup", "rex", "regex", "join", "append", "appendpipe",
    "lookup", "outputlookup", "outputnew", "coalesce", "mvexpand",
    "mvreverse", "mvcombine", "mvdedup", "mvappend", "mvfilter", "mvcount",
    "mvindex", "mvzip", "mvjoin", "spath", "base64", "bin", "multisearch",
    "maketable", "fieldsummary", "fillnull", "makeresults", "addinfo",
    "mvdc", "mvfind",
    # Phase 1 / Bet 2 slice 4 (2026-05-08): semantic-search pipes
    "nearest", "dedup_semantic",
    # Phase 2 / Bet 3 slice 4 (2026-05-08): | llm SPQL pipe
    "llm",
    # Phase 2 / Bet 3 slice 5 (2026-05-08): | llm_batch SPQL pipe
    "llm_batch",
    # Phase 2 / Bet 3 slice 6 (2026-05-08): | switch ... case conditional pipe
    "switch",
    # Phase 4 / Bet 3 slice 1 (2026-05-09): | llm_route 2-stage cost cascade
    "llm_route",
    # Phase 4 / Bet 3 slice 2 (2026-05-09): | llm_refine drafter/critic loop
    "llm_refine",
    # Phase 4 / Bet 3 slice 3 (2026-05-09): | llm_ensemble multi-model voting
    "llm_ensemble",
    # Phase 4 / Bet 3 slice 4 (2026-05-09): | llm_until convergence loop
    "llm_until",
    # W14 (2026-07-12): | sql DuckDB passthrough pipe
    "sql",
    # initial-clause
    "inputlookup", "loadjob",
}

EXPECTED_FUNCTIONS_SAMPLE = {
    "round", "min", "max", "avg", "sum", "abs", "sqrt",
    "concat", "replace", "upper", "lower", "capitalize", "substr", "trim",
    "ltrim", "rtrim", "len", "match", "tonumber", "tostring",
    "urlencode", "urldecode", "defang", "fang", "if_", "case", "coalesce",
    "isnull", "isnotnull",
    "count", "values", "latest", "earliest", "first", "last", "dc",
    # Added 2026-04-21 lexer review - these must now be in the grammar.
    "now", "relative_time", "strftime", "strptime",
    "split", "type", "base64_encode", "base64_decode",
    "mvsort", "randomize",
}


class TestVocabShape:
    def test_has_version(self):
        v = get_vocab(reload=True)
        assert v["version"] == VOCAB_VERSION

    def test_shape_matches_public_contract(self):
        v = get_vocab(reload=True)
        assert set(v.keys()) >= {
            "version", "commands", "functions", "keywords",
            "operators", "booleans", "time_units",
        }
        for c in v["commands"]:
            assert "name" in c and "kind" in c
        for f in v["functions"]:
            assert "name" in f and "kind" in f


class TestCommandExtraction:
    def test_every_documented_command_present(self):
        v = get_vocab(reload=True)
        names = {c["name"] for c in v["commands"]}
        missing = EXPECTED_COMMANDS - names
        assert not missing, f"grammar vocab is missing: {sorted(missing)}"

    def test_no_unexpected_commands_leaked(self):
        # Guard against a future grammar tweak that accidentally classifies
        # a non-command token as a command. If a real new command is added,
        # extend EXPECTED_COMMANDS.
        v = get_vocab(reload=True)
        names = {c["name"] for c in v["commands"]}
        unexpected = names - EXPECTED_COMMANDS
        assert not unexpected, f"unexpected commands surfaced: {sorted(unexpected)}"

    def test_initial_commands_tagged(self):
        v = get_vocab(reload=True)
        kinds = {c["name"]: c["kind"] for c in v["commands"]}
        assert kinds.get("inputlookup") == "initial"
        assert kinds.get("loadjob") == "initial"
        assert kinds.get("search") == "directive"


class TestFunctionExtraction:
    def test_sample_functions_present(self):
        v = get_vocab(reload=True)
        names = {f["name"] for f in v["functions"]}
        missing = EXPECTED_FUNCTIONS_SAMPLE - names
        assert not missing, f"grammar vocab is missing functions: {sorted(missing)}"

    def test_function_kinds_are_known(self):
        v = get_vocab(reload=True)
        kinds = {f["kind"] for f in v["functions"]}
        assert kinds <= {"numeric", "string", "specific", "stats"}


class TestKeywordsAndOperators:
    def test_control_flow_keywords(self):
        v = get_vocab(reload=True)
        assert set(v["keywords"]) == {"AND", "OR", "NOT", "BY", "AS", "IN"}

    def test_comparison_operators(self):
        v = get_vocab(reload=True)
        assert set(v["operators"]) == {"=", "!=", "<", ">", "<=", ">="}

    def test_booleans(self):
        v = get_vocab(reload=True)
        assert set(v["booleans"]) == {"true", "false"}

    def test_time_units(self):
        v = get_vocab(reload=True)
        assert set(v["time_units"]) == {
            "second", "minute", "hour", "day", "week", "year",
        }


class TestIdempotent:
    def test_repeated_calls_return_same_cached_dict(self):
        a = get_vocab(reload=True)
        b = get_vocab()
        assert a is b

    def test_reload_returns_equivalent_dict(self):
        a = get_vocab(reload=True)
        b = get_vocab(reload=True)
        assert a == b

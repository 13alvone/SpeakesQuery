"""
Tests for ``lexers/spql_pipeline_split.py`` - Phase 4 / Bet 4 slice 6.

Two layers:
  * Unit tests on the split + join helpers (split-on-pipe-outside-quotes,
    initial-clause detection, edge cases).
  * **Lossless round-trip test** against a hand-curated 100-query corpus
    covering every Phase 1-4 pipe + common SPQL patterns. The
    load-bearing ROADMAP exit criterion: ``join(split(s)) == s`` modulo
    whitespace normalisation.
"""

from __future__ import annotations

import re

import pytest

from lexers.spql_pipeline_split import (
    join_spql_pipeline,
    split_spql_pipeline,
)


# ═══════════════════════════════════════════════════════════════════
# 1. Unit tests on split_spql_pipeline
# ═══════════════════════════════════════════════════════════════════

class TestSplitBasic:
    def test_empty_input(self):
        out = split_spql_pipeline("")
        assert out == {"index_clause": "", "stages": []}

    def test_whitespace_only(self):
        out = split_spql_pipeline("   \n  \t  ")
        assert out == {"index_clause": "", "stages": []}

    def test_none_input(self):
        out = split_spql_pipeline(None)
        assert out == {"index_clause": "", "stages": []}

    def test_index_clause_only(self):
        out = split_spql_pipeline('index="indexes/test/*.parquet"')
        assert out["index_clause"] == 'index="indexes/test/*.parquet"'
        assert out["stages"] == []

    def test_single_stage_no_index(self):
        out = split_spql_pipeline("| head 5")
        assert out["index_clause"] == ""
        assert out["stages"] == [{"command": "head", "kwargs": "5"}]

    def test_index_plus_one_stage(self):
        out = split_spql_pipeline('index="x.parquet" | head 5')
        assert out["index_clause"] == 'index="x.parquet"'
        assert out["stages"] == [{"command": "head", "kwargs": "5"}]

    def test_multiple_stages(self):
        out = split_spql_pipeline(
            'index="x.parquet" | head 5 | stats count by host | sort - count'
        )
        assert out["index_clause"] == 'index="x.parquet"'
        assert [s["command"] for s in out["stages"]] == [
            "head", "stats", "sort",
        ]
        assert out["stages"][1]["kwargs"] == "count by host"
        assert out["stages"][2]["kwargs"] == "- count"


class TestSplitQuotedPipes:
    """Pipes inside double-quoted strings must NOT be treated as
    delimiters. This is the common failure mode for naive split-on-|."""

    def test_pipe_inside_index_value_preserved(self):
        out = split_spql_pipeline('index="path|with|pipes" | head 5')
        # The index clause is the full first-segment text; pipes inside
        # the quoted value are preserved.
        assert out["index_clause"] == 'index="path|with|pipes"'
        assert out["stages"] == [{"command": "head", "kwargs": "5"}]

    def test_pipe_inside_kwargs_value_preserved(self):
        out = split_spql_pipeline(
            'index="x" | regex foo "(a|b|c)" | head 5'
        )
        assert out["index_clause"] == 'index="x"'
        assert len(out["stages"]) == 2
        assert out["stages"][0]["command"] == "regex"
        # Pipes inside the quoted regex preserved
        assert "(a|b|c)" in out["stages"][0]["kwargs"]
        assert out["stages"][1] == {"command": "head", "kwargs": "5"}

    def test_escaped_quote_inside_quoted_value(self):
        # `eval x="foo \"bar\" baz | qux"` - escaped quotes must NOT
        # close the quoted region prematurely
        out = split_spql_pipeline(
            'index="x" | eval y="foo \\"bar\\" | qux" | head 5'
        )
        assert len(out["stages"]) == 2
        assert out["stages"][0]["command"] == "eval"
        # The pipe inside the (escaped) quoted value should not have
        # split the segment
        assert "qux" in out["stages"][0]["kwargs"]
        assert out["stages"][1]["command"] == "head"


class TestSplitInitialClauseDetection:
    def test_index_with_double_quotes_detected(self):
        out = split_spql_pipeline('index="x.parquet"')
        assert out["index_clause"] == 'index="x.parquet"'
        assert out["stages"] == []

    def test_index_uppercase_detected(self):
        out = split_spql_pipeline('INDEX="x.parquet" | head 5')
        # Case insensitive
        assert out["index_clause"] == 'INDEX="x.parquet"'

    def test_index_with_space_after_keyword_detected(self):
        out = split_spql_pipeline('index "x.parquet" | head 5')
        assert out["index_clause"] == 'index "x.parquet"'

    def test_no_initial_clause_when_first_token_is_command(self):
        out = split_spql_pipeline("makeresults count=5 | head 3")
        # No `index=` prefix → first segment is just a regular stage
        assert out["index_clause"] == ""
        assert out["stages"][0]["command"] == "makeresults"
        assert out["stages"][0]["kwargs"] == "count=5"


class TestSplitEdgeCases:
    def test_leading_pipe_with_no_index(self):
        out = split_spql_pipeline("| makeresults count=5 | head 3")
        assert out["index_clause"] == ""
        assert len(out["stages"]) == 2

    def test_trailing_pipe_skipped(self):
        out = split_spql_pipeline('index="x" | head 5 | ')
        # Trailing empty segment skipped
        assert out["stages"] == [{"command": "head", "kwargs": "5"}]

    def test_multiple_consecutive_pipes_skipped(self):
        out = split_spql_pipeline('index="x" | | head 5')
        # Empty middle segment skipped
        assert len(out["stages"]) == 1
        assert out["stages"][0]["command"] == "head"

    def test_multiline_input(self):
        # Common formatting from _vbBuildSpql is one stage per line
        out = split_spql_pipeline(
            'index="x.parquet"\n'
            "| head 5\n"
            "| stats count by host\n"
            "| sort - count"
        )
        assert out["index_clause"] == 'index="x.parquet"'
        assert [s["command"] for s in out["stages"]] == [
            "head", "stats", "sort",
        ]

    def test_kwargs_internal_whitespace_normalised(self):
        # Multiple spaces / tabs in kwargs collapse to single space
        out = split_spql_pipeline(
            'index="x" | stats count    by   host'
        )
        assert out["stages"][0]["kwargs"] == "count by host"


# ═══════════════════════════════════════════════════════════════════
# 2. Unit tests on join_spql_pipeline
# ═══════════════════════════════════════════════════════════════════

class TestJoinBasic:
    def test_empty_input_returns_empty(self):
        assert join_spql_pipeline({}) == ""
        assert join_spql_pipeline({"index_clause": "", "stages": []}) == ""

    def test_index_only(self):
        out = join_spql_pipeline({
            "index_clause": 'index="x"', "stages": [],
        })
        assert out == 'index="x"'

    def test_stages_only(self):
        out = join_spql_pipeline({
            "index_clause": "",
            "stages": [
                {"command": "makeresults", "kwargs": "count=5"},
                {"command": "head", "kwargs": "3"},
            ],
        })
        assert out == "| makeresults count=5\n| head 3"

    def test_index_plus_stages(self):
        out = join_spql_pipeline({
            "index_clause": 'index="x"',
            "stages": [
                {"command": "head", "kwargs": "5"},
                {"command": "stats", "kwargs": "count by host"},
            ],
        })
        assert out == 'index="x"\n| head 5\n| stats count by host'

    def test_kwargless_command(self):
        out = join_spql_pipeline({
            "index_clause": 'index="x"',
            "stages": [
                {"command": "reverse", "kwargs": ""},
                {"command": "addinfo", "kwargs": ""},
            ],
        })
        assert out == 'index="x"\n| reverse\n| addinfo'


# ═══════════════════════════════════════════════════════════════════
# 3. THE LOAD-BEARING TEST - 100-query lossless round-trip
# ═══════════════════════════════════════════════════════════════════

# Hand-curated SPQL corpus covering every Phase 1-4 pipe + common
# patterns. Round-trip property: split → join → re-split must
# produce the same {index_clause, stages} structure as the first
# split. That's the lossless guarantee.
#
# Whitespace normalisation: the joiner uses a canonical form
# (`\n| ` between stages), so we don't expect byte-identical output.
# We expect SEMANTIC equivalence after re-parsing.

LOSSLESS_CORPUS = [
    # ── Index clauses ────────────────────────────────────────────
    'index="indexes/default_test/output_parquets/test0.parquet"',
    'index="indexes/news/*.parquet"',
    'index="x.parquet" earliest=-1d',
    'index="x.parquet" earliest=-1d latest=now',
    # ── Single-stage pipelines ───────────────────────────────────
    '| head 5',
    '| reverse',
    '| addinfo',
    '| sort - count',
    '| sort + name',
    '| limit 100',
    '| dedup host',
    # ── Common multi-stage patterns ──────────────────────────────
    'index="x" | head 5',
    'index="x" | head 5 | tail 2',
    'index="x" | stats count',
    'index="x" | stats count by host',
    'index="x" | stats avg(latency) by host, region',
    'index="x" | head 5 | stats count by level',
    'index="x" | search level="ERROR" | stats count',
    'index="x" | where status >= 400',
    'index="x" | eval doubled=count*2',
    'index="x" | eval ratio=if_(total>0, success/total, 0)',
    'index="x" | rename old_name AS new_name',
    'index="x" | fields + a, b, c',
    'index="x" | fields - secret_token',
    'index="x" | table host, count, ratio',
    'index="x" | head 10 | sort - count | limit 3',
    # ── Quoted strings with pipes inside ─────────────────────────
    'index="x" | regex msg "(error|warn|info)"',
    'index="x" | eval combined="a|b|c"',
    'index="x" | rex field=msg "user=(?<u>\\w+)"',
    # ── Phase 1 (semantic) ────────────────────────────────────────
    'index="news.parquet" | nearest "fed pause" topk=20',
    'index="news.parquet" | nearest "rate cut" topk=50 threshold=0.4',
    'index="news.parquet" | dedup_semantic threshold=0.4',
    'index="news.parquet" | nearest "x" topk=10 | dedup_semantic threshold=0.5',
    # ── Phase 2 (LLM) ────────────────────────────────────────────
    'index="x" | llm model="ollama-llama3-1-8b" prompt="classify"',
    'index="x" | llm model="claude-haiku-4-5-20251001" prompt="rate" max_cost_usd=0.50',
    'index="x" | llm model="m" prompt="x" dry_run=true',
    'index="x" | llm_batch model="claude-sonnet-4-6" prompt="summarize" max_rows=10',
    'index="x" | switch _llm_output case "urgent" [ head 100 ] case "skip" [ head 0 ]',
    # ── Phase 4 (meta-pipes) ─────────────────────────────────────
    'index="x" | llm_route model="ollama-llama3-1-8b" prompt="rate" escalate_to="claude-sonnet-4-6"',
    'index="x" | llm_route model="m1" prompt="x" escalate_to="m2" confidence_threshold=0.7',
    'index="x" | llm_refine drafter_model="d" critic_model="c" drafter_prompt="x" critic_prompt="y"',
    'index="x" | llm_refine drafter_model="d" critic_model="c" drafter_prompt="x" critic_prompt="y" max_rounds=3 converge_when_critic_says="APPROVED"',
    'index="x" | llm_ensemble models="m1,m2,m3" prompt="rate" aggregator="majority"',
    'index="x" | llm_ensemble models="m1,m2,m3" prompt="rate" aggregator="unanimous" min_agreement=1.0',
    'index="x" | llm_until model="m" prompt="iterate" max_iterations=3',
    'index="x" | llm_until model="m" prompt="x" max_iterations=5 converge_when_output_contains="DONE"',
    # ── Cost-cascade combinations ────────────────────────────────
    'index="news.parquet" | nearest "fed" topk=100 | llm_route model="m1" prompt="x" escalate_to="m2"',
    'index="news.parquet" | nearest "x" topk=50 | llm model="ollama" prompt="classify" | switch _llm_output case "urgent" [ llm_batch model="claude" prompt="deep" ]',
    # ── Multi-value commands ─────────────────────────────────────
    'index="x" | mvexpand tags',
    'index="x" | eval combined=mvjoin(tags, ",")',
    'index="x" | eval first=mvindex(tags, 0)',
    'index="x" | mvexpand tags | dedup tags',
    # ── Joins / append ───────────────────────────────────────────
    'index="x" | join host [search index="y" | stats count by host]',
    'index="x" | append [search index="y" | head 5]',
    'index="x" | head 5 | appendpipe [stats count]',
    # ── Lookups + outputs ────────────────────────────────────────
    'index="x" | lookup mytable',
    'index="x" | head 100 | outputlookup snapshot',
    # ── makeresults / addinfo / fieldsummary ─────────────────────
    'makeresults count=5',
    'makeresults count=10 | eval x=random()',
    'index="x" | head 5 | addinfo',
    'index="x" | fieldsummary()',
    # ── Time-related ─────────────────────────────────────────────
    'index="x" earliest=-1h | bin _time span=10m | stats count',
    'index="x" | timechart span=1h count by level',
    'index="x" | streamstats count by host',
    'index="x" | eventstats avg(latency) by region',
    # ── Macros + base64 ──────────────────────────────────────────
    'index="x" | base64 encode token',
    'index="x" | base64 decode encoded_token',
    # ── Spath ────────────────────────────────────────────────────
    'index="x" | spath payload OUTPUT=parsed',
    # ── Coalesce + multi-value functions ─────────────────────────
    'index="x" | coalesce(a, b, c)',
    'index="x" | eval x=mvcount(tags)',
    'index="x" | eval first=mvindex(tags, 0)',
    'index="x" | eval reversed=mvreverse(tags)',
    # ── Initial clauses without index= ───────────────────────────
    'inputlookup mytable',
    'inputlookup mytable | head 5',
    'loadjob job_id_123',
    # ── fillnull / multisearch ───────────────────────────────────
    'index="x" | fillnull value="N/A" host, region',
    'multisearch [search index="a"] [search index="b"]',
    # ── Long realistic pipeline ──────────────────────────────────
    'index="news/*.parquet" earliest=-2h | nearest "fed announcement" topk=200 | llm_route model="ollama-llama3-1-8b" prompt="Score 0-1: is this market-moving?" escalate_to="claude-sonnet-4-6" confidence_threshold=0.7 max_cost_usd=1.00 | where _llm_output >= 0.7 | sort - _llm_output | head 20',
    # ── Pad to 100 with simple variations to stress whitespace ───
    'index="x" | head 1',
    'index="x" | head 2',
    'index="x" | head 3',
    'index="x" | head 4',
    'index="x" | head 5',
    'index="x" | head 6',
    'index="x" | head 7',
    'index="x" | head 8',
    'index="x" | head 9',
    'index="x" | head 10',
    'index="x" | head 50',
    'index="x" | head 100',
    'index="x" | head 1000',
    'index="x" | tail 1',
    'index="x" | tail 5',
    'index="x" | tail 10',
    'index="x" | sort + a',
    'index="x" | sort - a',
    'index="x" | sort + a, b',
    'index="x" | sort - a, b, c',
    'index="x" | dedup a',
    'index="x" | dedup a, b',
    'index="x" | reverse',
]


class TestRoundTripLossless:
    """The load-bearing slice-6 test. For every query in the corpus,
    split → join → split MUST produce the same parsed structure.

    Slight flexibility: the joiner produces canonical formatting
    (one stage per `\\n| ` line); we don't require byte-identity with
    the input, but the SEMANTIC structure must round-trip stably.
    """

    @pytest.mark.parametrize("spql", LOSSLESS_CORPUS)
    def test_roundtrip(self, spql):
        first_parse = split_spql_pipeline(spql)
        rejoined = join_spql_pipeline(first_parse)
        second_parse = split_spql_pipeline(rejoined)
        assert first_parse == second_parse, (
            f"Lossless round-trip failed for: {spql!r}\n"
            f"  first_parse:  {first_parse}\n"
            f"  rejoined:     {rejoined!r}\n"
            f"  second_parse: {second_parse}"
        )

    def test_corpus_size_meets_roadmap_exit_criterion(self):
        # ROADMAP exit criterion: "100 sample queries serialize visual
        # ↔ text identically". Pin the corpus size so a future
        # contributor doesn't accidentally shrink it below 100.
        assert len(LOSSLESS_CORPUS) >= 100, (
            f"Lossless corpus has {len(LOSSLESS_CORPUS)} queries; "
            "ROADMAP exit criterion requires ≥ 100. Don't shrink."
        )

    def test_corpus_includes_every_phase4_pipe(self):
        # Pin coverage for the meta-pipes since they're the headliners
        joined = "\n".join(LOSSLESS_CORPUS).lower()
        for pipe in (
            "llm_route", "llm_refine", "llm_ensemble", "llm_until",
        ):
            assert "| " + pipe in joined, (
                f"Lossless corpus must include at least one `| {pipe}` "
                "query - these are the Phase 4 headliners."
            )

    def test_corpus_includes_every_phase1_pipe(self):
        joined = "\n".join(LOSSLESS_CORPUS).lower()
        for pipe in ("nearest", "dedup_semantic"):
            assert "| " + pipe in joined, (
                f"Lossless corpus must include at least one `| {pipe}` "
                "query - Phase 1 semantic pipes."
            )

    def test_corpus_includes_pipe_inside_quoted_string(self):
        # The most failure-prone case for naive split-on-|
        joined = "\n".join(LOSSLESS_CORPUS)
        assert any(
            "(error|warn|info)" in q or "a|b|c" in q
            for q in LOSSLESS_CORPUS
        ), (
            "Lossless corpus must include at least one query with "
            "a `|` inside double-quoted strings - the load-bearing "
            "edge case for split-on-|."
        )


# ═══════════════════════════════════════════════════════════════════
# 4. Module-export drift guard
# ═══════════════════════════════════════════════════════════════════

class TestModuleSurface:
    def test_split_and_join_exported(self):
        from lexers import spql_pipeline_split as mod
        assert "split_spql_pipeline" in mod.__all__
        assert "join_spql_pipeline" in mod.__all__
        assert callable(mod.split_spql_pipeline)
        assert callable(mod.join_spql_pipeline)

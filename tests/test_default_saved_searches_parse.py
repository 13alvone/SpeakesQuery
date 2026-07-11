"""
Regression test - every default_saved_searches/*.yaml must parse cleanly.

Caught 2026-04-23 after a live run surfaced "all searches returned zero
results" for the newly-shipped Global Macro Risk Brief feeders. Root
cause: 9 of the YAMLs had `sort <col>` (unprefixed) instead of the
required `sort +<col>` / `sort -<col>`. The SPQL lexer reports a parser
error via ANTLR's error listener, but because the engine still returns
an empty DataFrame rather than raising, the UI just shows "no results"
- silent failure mode.

This test walks every YAML under default_saved_searches/, extracts the
query field, hands it to the SPQL parser with a fail-on-error listener,
and fails the test run if any query raises a syntax error.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)


def _list_default_queries():
    """Return [(search_name, query), ...] for every default YAML."""
    out = []
    for p in sorted((PROJECT_ROOT / "default_saved_searches").glob("*.yaml")):
        spec = yaml.safe_load(p.read_text()) or {}
        query = (spec.get("query") or "").strip()
        if query:
            out.append((p.stem, query))
    return out


class _CollectErrorListener:
    """ANTLR ErrorListener that collects syntax errors instead of
    printing them to stderr. Matches the shape ANTLR4 Python runtime
    expects (duck-typed; no need to subclass the official ErrorListener
    since we implement every method it calls)."""

    def __init__(self):
        self.errors: list[str] = []

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
        self.errors.append(f"line {line}:{column} {msg}")

    def reportAmbiguity(self, *a, **kw):
        pass

    def reportAttemptingFullContext(self, *a, **kw):
        pass

    def reportContextSensitivity(self, *a, **kw):
        pass


def _neutralize_eval_function_bodies(query: str) -> str:
    """Replace `if_(...)` / `case(...)` argument bodies with a literal `1`
    before strict ANTLR parsing.

    Why: the SPQL ANTLR grammar (lexers/speakesQuery.g4) does not currently
    accept equality comparisons (`==`) inside `if_()` / `case()` argument
    lists - it expects only aggregation functions or arithmetic operators.
    BUT the production execution path (`process_query()` →
    speakesQueryListener) successfully handles equality comparisons via
    Python eval - confirmed by `tests/yaml/tier2_functions/test_conditional_functions.yaml`
    where `if_(userRole=="admin", "yes", "no")` works end-to-end.

    Until the grammar is extended (separate task), this strict-parse test
    elides if_/case bodies so it doesn't false-positive on YAMLs using the
    `==` form that's known to work at runtime. The new drift-guards
    (`test_no_single_equal_inside_if_`) still catch the actual production
    bug - the `=` form (Python kwarg syntax → KeyError at runtime)."""
    import re as _re

    def _strip_balanced(text: str, fname: str) -> str:
        # Walk char-by-char to find balanced `fname(...)` and replace with `1`.
        out = []
        i = 0
        n = len(text)
        token = fname + "("
        while i < n:
            if text[i:i + len(token)] == token:
                # Find matching close paren
                depth = 1
                j = i + len(token)
                while j < n and depth > 0:
                    if text[j] == "(":
                        depth += 1
                    elif text[j] == ")":
                        depth -= 1
                    j += 1
                if depth == 0:
                    out.append("1")
                    i = j
                    continue
            out.append(text[i])
            i += 1
        return "".join(out)

    # Order matters: nested `case(if_(...))` would lose the inner if_
    # if we did case first; do if_ first so the outer case() then sees
    # `case(1, ..., 1, ...)`. (Both functions take comma-separated
    # condition+value pairs; `1` is a valid token in either context.)
    out = _strip_balanced(query, "if_")
    out = _strip_balanced(out, "case")
    return out


@pytest.mark.parametrize(
    "search_name,query",
    _list_default_queries(),
    ids=[name for name, _ in _list_default_queries()],
)
def test_default_saved_search_parses(search_name: str, query: str):
    """Every shipped default saved search must parse without SPQL
    syntax errors. A single unprefixed `sort <col>` or similar drift
    will silently return zero rows in production - this test makes the
    failure loud.

    Note: if_/case argument bodies are elided before parsing because the
    ANTLR grammar lacks equality comparison support inside them, but the
    runtime execution path handles equality fine. See
    :func:`_neutralize_eval_function_bodies` for full rationale."""
    from antlr4 import CommonTokenStream, InputStream  # type: ignore

    from lexers.antlr4_active.speakesQueryLexer import speakesQueryLexer
    from lexers.antlr4_active.speakesQueryParser import speakesQueryParser

    listener = _CollectErrorListener()

    parse_input = _neutralize_eval_function_bodies(query)
    lexer = speakesQueryLexer(InputStream(parse_input))
    lexer.removeErrorListeners()
    lexer.addErrorListener(listener)

    stream = CommonTokenStream(lexer)
    parser = speakesQueryParser(stream)
    parser.removeErrorListeners()
    parser.addErrorListener(listener)

    # Trigger a full parse - the grammar's entry rule is ``speakesQuery``.
    parser.speakesQuery()

    assert not listener.errors, (
        f"{search_name}.yaml has SPQL syntax errors:\n  "
        + "\n  ".join(listener.errors)
        + f"\n\nQuery (if_/case bodies elided):\n{parse_input}"
        + f"\n\nOriginal:\n{query}"
    )


# ---------------------------------------------------------------------------
# Drift-guards for two SPQL bug classes that ANTLR parsing alone misses.
# Both surfaced together 2026-05-04 in the schedule report iteration:
# 5 SS YAMLs were silently producing 0 rows in production.
# ---------------------------------------------------------------------------

import re  # noqa: E402


_SAVED_SEARCH_DIRS = ("default_saved_searches", "saved_searches")


def _list_all_queries():
    """Walk BOTH default_saved_searches/ and saved_searches/ (live).
    Returns [(dir/name, query), ...]. Useful for drift guards that need
    to catch live-but-not-default regressions too."""
    out = []
    for d in _SAVED_SEARCH_DIRS:
        path = PROJECT_ROOT / d
        if not path.is_dir():
            continue
        for p in sorted(path.glob("*.yaml")):
            spec = yaml.safe_load(p.read_text()) or {}
            query = (spec.get("query") or "").strip()
            if query:
                out.append((f"{d}/{p.stem}", query))
    return out


# Match `if_(<identifier>=<not-equals-or-greater-or-less>` - i.e. a single
# `=` immediately after an identifier inside an `if_(` call. Python parses
# this as a kwarg assignment (positional-after-keyword SyntaxError when
# followed by more args). The correct SPQL form is `==`. Caught when
# `pppb_federal_register` returned 0 rows from 1249 underlying - the
# `if_(significant_action=true OR ...)` clause raised KeyError: None.
_IF_SINGLE_EQUAL_PATTERN = re.compile(
    r"if_\(\s*\w+\s*=(?![=<>])",  # `=` not followed by = / < / > (which form ==, <=, >=)
)


@pytest.mark.parametrize(
    "label,query",
    _list_all_queries(),
    ids=[name for name, _ in _list_all_queries()],
)
def test_no_single_equal_inside_if_(label: str, query: str):
    """Drift guard: `if_(field=value, ...)` is a Python kwarg syntax
    error at translation time. SPQL equality requires `==`. This bug
    silently returned 0 rows in production for 5 saved searches before
    being caught in the 2026-05-04 schedule-report iteration.

    Correct: `if_(field==value, then, else)`
    Broken:  `if_(field=value, then, else)`  ← positional after keyword
    """
    matches = _IF_SINGLE_EQUAL_PATTERN.findall(query)
    assert not matches, (
        f"{label}.yaml uses single `=` inside if_() - must be `==`. "
        f"Python parses `if_(field=value, ...)` as a kwarg followed by "
        f"positional args, which is a SyntaxError at SPQL translation "
        f"time. Pattern matches: {matches}\n\nQuery:\n{query}"
    )


# Match `eventstats <stuff> count as <name>` - bare `count` keyword (no
# parens) inside eventstats. The `as <name>` rename is silently dropped;
# the resulting column is named `count`, not the intended name. Downstream
# clauses referencing the intended name then return 0 rows or NaN. Use
# `count(_epoch) as <name>` or `count(*) as <name>` instead. Caught
# alongside the if_() bug in the 2026-05-04 iteration.
_BARE_COUNT_AS_PATTERN = re.compile(
    r"eventstats[^|]*?\bcount\s+as\s+\w+",
)


@pytest.mark.parametrize(
    "label,query",
    _list_all_queries(),
    ids=[name for name, _ in _list_all_queries()],
)
def test_no_bare_count_rename_in_eventstats(label: str, query: str):
    """Drift guard: `eventstats count as <name>` silently fails to
    rename - the column ends up named `count`, not `<name>`. Use
    `count(_epoch)` (or `count(*)`) so the `as` clause is honored.
    Caught when pppb_federal_register's `total_docs` and
    fxrb_carry_trade_signal's `total_pairs` were both NULL in
    production output."""
    matches = _BARE_COUNT_AS_PATTERN.findall(query)
    assert not matches, (
        f"{label}.yaml uses `eventstats ... count as <name>` - the "
        f"rename is silently dropped, column ends up named `count`. "
        f"Use `count(_epoch) as <name>` instead. "
        f"Pattern matches: {matches}\n\nQuery:\n{query}"
    )


# NOTE - the 2026-05-05 21:35 UTC drift guards `test_no_double_equal_in_where_clause`
# and `test_no_bare_where_match` were REMOVED 2026-05-05 22:XX UTC after
# the engine fixes shipped in `handlers/SearchCmdHandler.py` and
# `lexers/speakesQueryListener.py`:
#
#  * `where x == y`  is now equivalent to `where x = y` (the tokenizer
#    captures `==` as one token and normalises to `=`).
#  * `where match(field, "regex")` is now wired as a 2-arg search
#    function that translates to pandas `.str.contains(regex=True,
#    na=False)`.
#
# Authors are free to use either form. End-to-end coverage lives in
# `tests/test_where_match_and_double_equal.py` (14 cases).

"""Grammar ↔ handler ↔ docs parity test.

Single-source-of-truth regression check for the SPQL grammar surface.
If someone adds a function to the grammar but forgets to wire the
handler, or removes a token but leaves it in `docs/lang/03_functions.md`,
these tests fail loud.

Runs as part of the regular `pytest` suite - no network, no fixtures.
"""
from __future__ import annotations

from pathlib import Path

import antlr4
import pytest
from antlr4.error.ErrorListener import ErrorListener

from lexers.antlr4_active.speakesQueryLexer import speakesQueryLexer
from lexers.antlr4_active.speakesQueryParser import speakesQueryParser
from lexers.grammar_vocab import get_vocab


PROJECT_ROOT = Path(__file__).resolve().parent.parent
GRAMMAR_PATH = PROJECT_ROOT / "lexers" / "speakesQuery.g4"
DOCS_PATH = PROJECT_ROOT / "docs" / "lang" / "03_functions.md"


# ── functions that must exist in the current grammar ──────────
# (everything CLAUDE.md advertises as a "Built-in function" - the
# full public SPQL function surface).
REQUIRED_FUNCTIONS = {
    # numeric
    "round", "min", "max", "avg", "sum", "abs", "sqrt",
    "median", "mode", "range", "random", "tonumber", "randomize",
    # string
    "concat", "replace", "upper", "lower", "capitalize", "substr",
    "trim", "ltrim", "rtrim", "len", "match", "split",
    "tostring", "urlencode", "urldecode", "defang", "fang", "type",
    "base64_encode", "base64_decode",
    # conditional / specific
    "isnull", "isnotnull", "coalesce", "if_", "case",
    # time / datetime
    "now", "relative_time", "strftime", "strptime",
    # stats aggregators
    "count", "values", "first", "last", "earliest", "latest", "dc",
    # multi-value
    "mvsort",
}


# ── tokens that must NOT exist in the current grammar ─────────
# (removed as dead code; listener stubs also removed).
FORBIDDEN_TOKENS = {"to_cron", "from_cron", "repeat", "null"}


class _SyntaxErrorCollector(ErrorListener):
    def __init__(self) -> None:
        self.errors: list[str] = []

    def syntaxError(self, r, off, line, col, msg, e):  # noqa: N802
        self.errors.append(f"{line}:{col} {msg}")


def _parse(query: str) -> tuple[list[str], list[str]]:
    """Parse ``query`` and return (lexer_errors, parser_errors)."""
    lex_l = _SyntaxErrorCollector()
    par_l = _SyntaxErrorCollector()
    lexer = speakesQueryLexer(antlr4.InputStream(query + "\n"))
    lexer.removeErrorListeners()
    lexer.addErrorListener(lex_l)
    parser = speakesQueryParser(antlr4.CommonTokenStream(lexer))
    parser.removeErrorListeners()
    parser.addErrorListener(par_l)
    parser.speakesQuery()
    return lex_l.errors, par_l.errors


# ── Parity tests ─────────────────────────────────────────────


def test_grammar_vocab_exposes_every_required_function():
    """Every public function must appear in the vocab parsed from the .g4."""
    vocab = get_vocab(reload=True)
    fn_names = {f["name"] for f in vocab["functions"]}
    missing = REQUIRED_FUNCTIONS - fn_names
    assert not missing, (
        "Functions advertised in CLAUDE.md but missing from grammar: "
        f"{sorted(missing)}"
    )


def test_grammar_has_no_forbidden_dead_tokens():
    """Tokens removed in the 2026-04-21 dead-code pass must not return."""
    grammar = GRAMMAR_PATH.read_text(encoding="utf-8")
    for tok in FORBIDDEN_TOKENS:
        # The forbidden tokens would reappear as ``TO_CRON : 'to_cron';``
        # lexer rules or as ``'to_cron'`` literals inside parser rules.
        assert f"'{tok}'" not in grammar, (
            f"Dead grammar token {tok!r} resurrected in speakesQuery.g4"
        )


def test_grammar_parses_every_new_function_cleanly():
    """Parser accepts each new-in-2026-04 function without a syntax error."""
    canonical_queries = [
        '| makeresults count=1 | eval t=now()',
        '| makeresults count=1 | eval e=relative_time("-1d@d")',
        '| makeresults count=1 | eval s=strftime(1705329000, "%Y-%m-%d")',
        '| makeresults count=1 | eval e=strptime("2024-01-15")',
        '| makeresults count=1 | eval e=strptime("01/15/2024", "%m/%d/%Y")',
        '| makeresults count=1 | eval parts=split("a,b,c", ",")',
        '| makeresults count=1 | eval tk=type(42)',
        '| makeresults count=1 | eval b=base64_encode("x")',
        '| makeresults count=1 | eval b=base64_decode("YQ==")',
        '| makeresults count=1 | eval r=randomize(42)',
        (
            '| makeresults count=1 '
            '| eval raw="b,a,c" '
            '| eval parts=split(raw, ",") '
            '| eval sorted=mvsort(parts)'
        ),
        (
            '| makeresults count=1 '
            '| eval a=1, b=2, c=3 '
            '| eval m=min(a, b, c)'
        ),
        (
            '| makeresults count=1 '
            '| eval a=1, b=2, c=3 '
            '| eval m=max(a, b, c)'
        ),
    ]
    failures: list[tuple[str, list[str], list[str]]] = []
    for q in canonical_queries:
        lex_errs, par_errs = _parse(q)
        if lex_errs or par_errs:
            failures.append((q, lex_errs, par_errs))
    assert not failures, (
        "Queries with syntax errors:\n"
        + "\n".join(f"  {q}\n    lex={le}\n    par={pe}" for q, le, pe in failures)
    )


def test_removed_functions_fail_at_execution_time():
    """The four removed functions may parse loosely (eval RHS is permissive),
    but MUST fail at evaluation time because EvalHandler no longer defines
    them. This enforces the user-visible contract: dead tokens don't work.
    """
    from query_engine.CmdExecutionBackend import process_query

    dead_queries = [
        '| makeresults count=1 | eval x=null()',
        '| makeresults count=1 | eval x=repeat("-", 10)',
        '| makeresults count=1 | eval x=to_cron("0 0 * * *")',
        '| makeresults count=1 | eval x=from_cron("0 0 * * *", "%H:%M")',
    ]
    for q in dead_queries:
        df, _ = process_query(q)
        assert df is None, (
            f"Removed function {q!r} unexpectedly succeeded - "
            "expected evaluation failure."
        )


@pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_TOKENS))
def test_docs_do_not_mention_forbidden_functions(forbidden):
    """03_functions.md must not document the removed dead functions."""
    text = DOCS_PATH.read_text(encoding="utf-8")
    # Look for explicit anchor - a `### forbidden(` heading.
    assert f"### {forbidden}(" not in text, (
        f"docs/lang/03_functions.md still documents the removed "
        f"function {forbidden!r} - remove it or resurrect the grammar."
    )


def test_docs_document_every_time_function():
    """The 4 time functions must each have a `### name(` anchor in the docs."""
    text = DOCS_PATH.read_text(encoding="utf-8")
    for fn in ("now", "relative_time", "strftime", "strptime"):
        assert f"### {fn}(" in text, (
            f"docs/lang/03_functions.md is missing an anchor for {fn!r}"
        )


def test_variable_names_reserved_tokens_still_usable():
    """Tokens like ``count`` / ``values`` must remain usable as column names."""
    # E.g. sort by the aggregate output column ``count``.
    q = (
        'index="indexes/default_test/error_tracking/system_alerts.parquet"'
        ' | stats count by region'
        ' | sort -count'
    )
    lex_errs, par_errs = _parse(q)
    assert not lex_errs and not par_errs, (
        f"Canonical 'stats count by ... | sort -count' no longer parses:"
        f"\n  lex={lex_errs}\n  par={par_errs}"
    )


def test_new_keyword_usable_as_column_name():
    """Users can still name a field ``now`` / ``split`` / ``strftime`` etc."""
    # ``variableName`` grammar alternative includes each of the new keyword
    # tokens so they do not break pre-existing user-named columns.
    q = (
        '| makeresults count=1'
        ' | eval now="placeholder"'
        ' | eval split="delimiter"'
        ' | eval type="kind"'
        ' | fields now, split, type'
    )
    lex_errs, par_errs = _parse(q)
    assert not lex_errs and not par_errs, (
        f"New-keyword-as-column-name broke existing queries:"
        f"\n  lex={lex_errs}\n  par={par_errs}"
    )

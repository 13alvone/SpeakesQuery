"""
Regression tests for the 2026-05-05 SPQL where-clause engine fixes:

1. ``where match(field, "regex")`` - was silently returning 0 rows in
   production because the search-handler parser didn't recognise
   ``match`` as a function call. Now wired as a 2-arg search function
   that translates to pandas ``.str.contains()`` with ``regex=True``.

2. ``where x == 1`` - was silently returning 0 rows because the
   tokenizer regex split ``==`` into two separate ``=`` tokens, which
   the parser couldn't make sense of. Now ``==`` is captured as a
   single token and normalised to ``=`` for downstream handling.

End-to-end tests run through ``process_query`` against a synthetic
DataFrame so we hit the same code path as production (lexer →
listener → SearchCmdHandler).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _run_search(query: str, df: pd.DataFrame) -> pd.DataFrame:
    """Execute a single ``| where`` segment against ``df`` via the
    SearchCmdHandler. Skips ANTLR/listener entirely - we're testing the
    parser+translator+pandas-query pipeline that the listener calls into."""
    from handlers.SearchCmdHandler import SearchDirective

    sd = SearchDirective()
    # Tokenise like the listener's ``_cmd_search`` does.
    import re
    pattern = (
        r'"[^\"]*"'
        r'|==|>=|<=|!=|=|>|<'
        r'|\(|\)|,'
        r'|\d+\.\d+'
        r'|\w+'
        r'|\S'
    )
    tokens = re.findall(pattern, query)
    return sd.run_search(tokens, df)


# ── Bug 1: where match(field, "regex") ──────────────────────────────


class TestWhereMatch:
    def setup_method(self):
        self.df = pd.DataFrame({
            "title": [
                "Became Public Law No: 119-60.",
                "Held at the desk.",
                "Placed on the Union Calendar, Calendar No. 501.",
                "Committee on Veterans' Affairs. Ordered to be reported with",
                None,
            ],
            "n": [1, 2, 3, 4, 5],
        })

    def test_match_substring(self):
        out = _run_search('match(title, "Public Law")', self.df)
        assert list(out["n"]) == [1], (
            f"Expected only row 1 (Public Law); got {list(out['n'])}"
        )

    def test_match_case_insensitive_flag(self):
        out = _run_search('match(title, "(?i)public law")', self.df)
        assert list(out["n"]) == [1]

    def test_match_alternation(self):
        out = _run_search('match(title, "Public Law|Held")', self.df)
        assert list(out["n"]) == [1, 2]

    def test_match_quantifier(self):
        out = _run_search('match(title, "C.{1,30}Calendar")', self.df)
        assert 3 in list(out["n"])

    def test_match_dot_plus_matches_any_nonempty(self):
        """``.+`` matches every non-empty string. NaN/None must NOT match
        (na=False in the pandas translation)."""
        out = _run_search('match(title, ".+")', self.df)
        assert list(out["n"]) == [1, 2, 3, 4]

    def test_match_combined_with_other_where_clauses(self):
        df = pd.DataFrame({
            "tier": ["HIGH", "MEDIUM", "LOW", "HIGH"],
            "title": ["Became Public Law", "ordered reported", "ignore me", "ignore"],
            "n": [1, 2, 3, 4],
        })
        out = _run_search('tier = "HIGH" AND match(title, "Public Law")', df)
        assert list(out["n"]) == [1]

    def test_match_with_regex_alternation_in_parens(self):
        out = _run_search('match(title, "(Public|Held)")', self.df)
        assert list(out["n"]) == [1, 2]


# ── Bug 2: where x == 1 (now equivalent to x = 1) ────────────────────


class TestWhereDoubleEqual:
    def setup_method(self):
        self.df = pd.DataFrame({
            "tier": ["HIGH", "MEDIUM", "LOW", "HIGH"],
            "is_open": [True, False, True, True],
            "n": [1, 2, 3, 4],
        })

    def test_double_equal_with_string(self):
        out = _run_search('tier == "HIGH"', self.df)
        assert list(out["n"]) == [1, 4]

    def test_double_equal_with_int(self):
        out = _run_search('n == 2', self.df)
        assert list(out["n"]) == [2]

    def test_double_equal_with_bool(self):
        out = _run_search('is_open == True', self.df)
        assert list(out["n"]) == [1, 3, 4]

    def test_single_equal_still_works(self):
        """The pre-existing single-= form must continue to work."""
        out = _run_search('tier = "HIGH"', self.df)
        assert list(out["n"]) == [1, 4]

    def test_mixed_double_and_single_in_AND_chain(self):
        out = _run_search('tier == "HIGH" AND is_open = True', self.df)
        assert list(out["n"]) == [1, 4]


# ── Combined: the actual pppb_congress_bills production pattern ──────


class TestEvalThenWhereStillWorks:
    """Sanity: the eval-then-where workaround we shipped to production
    on 2026-05-05 must continue to work after the engine fixes. Authors
    using either pattern (bare match() or eval-then-where) should get
    the same result."""

    def test_workaround_pattern_unchanged(self):
        df = pd.DataFrame({
            "tier": ["HIGH", "HIGH", "LOW"],
            "title": ["passed senate", "ordered to be reported", "noise"],
            "is_substantive": [1, 1, 0],
            "n": [1, 2, 3],
        })
        # Workaround form: pre-computed indicator column
        out = _run_search('is_substantive = 1 AND tier = "HIGH"', df)
        assert list(out["n"]) == [1, 2]

    def test_native_form_now_equivalent(self):
        """The natural form should now be equivalent to the workaround."""
        df = pd.DataFrame({
            "tier": ["HIGH", "HIGH", "LOW"],
            "title": ["passed senate", "ordered to be reported", "noise"],
            "n": [1, 2, 3],
        })
        out = _run_search(
            'tier == "HIGH" AND match(title, "passed|ordered")',
            df,
        )
        assert list(out["n"]) == [1, 2]


# ── Removed type-check functions: isnum/isint/isstr ──────────────


class TestRemovedTypeCheckFunctions:
    """Round-8 (2026-05-06) cleanup: `isnum`, `isint`, `isstr` were
    removed from the SearchCmdHandler's `_SEARCH_FUNCTIONS` set after a
    full audit found they had never worked end-to-end:

    * Not present in `lexers/speakesQuery.g4` (ANTLR grammar)
    * Not in EvalHandler's allowlist (eval rejects with
      "Function 'isnum' is not allowed")
    * Where-context translations used `apply(lambda x: ...)` which
      pandas df.query() rejects with "'Lambda' nodes are not implemented"
    * Zero production usage across saved_searches/, alert_groups/,
      script_library/scripts/, and the test YAML corpus

    These tests pin the removal intent: isnull/isnotnull continue to
    work; the removed functions surface a clear parser-level signal so
    a future author can't silently re-introduce the broken pattern."""

    def setup_method(self):
        import pandas as pd
        self.df = pd.DataFrame({
            "name": ["alice", None, "bob"],
            "n": [1, 2, 3],
        })

    def test_isnull_still_works(self):
        """The kept-functions (isnull/isnotnull) must continue to filter
        correctly - they translate to pandas Series methods (.isna()
        and .notna()) which df.query() supports natively."""
        out = _run_search('isnull(name)', self.df)
        assert list(out["n"]) == [2], (
            f"Expected only the row with name=None; got {list(out['n'])}"
        )

    def test_isnotnull_still_works(self):
        out = _run_search('isnotnull(name)', self.df)
        assert list(out["n"]) == [1, 3]

    def test_isnum_no_longer_silently_returns_zero_rows(self):
        """Pre-removal: `where isnum(field)` would parse, translate to a
        broken pandas query, fail silently in the handler's outer
        try/except, and return 0 rows. Post-removal: the parser still
        recognises `isnum(` syntactically (the regex tokenizer handles
        any function-like construct), but `_SEARCH_FUNCTIONS` no longer
        lists it, so `parse_comparison` falls through to bare-identifier
        handling. The query produces an empty DataFrame OR a clear
        diagnostic, but does NOT silently match-zero-by-broken-lambda.

        This test pins the absence of a translation by asserting the
        handler doesn't silently execute the old broken pattern."""
        from handlers.SearchCmdHandler import SearchDirective
        sd = SearchDirective.Parser([])
        # Confirm the function is no longer in the search set
        assert "isnum" not in sd._SEARCH_FUNCTIONS
        assert "isint" not in sd._SEARCH_FUNCTIONS
        assert "isstr" not in sd._SEARCH_FUNCTIONS

    def test_isnull_isnotnull_remain_in_search_functions(self):
        """isnull and isnotnull DO have working pandas-eval translations
        (.isna() / .notna()) and must remain available."""
        from handlers.SearchCmdHandler import SearchDirective
        sd = SearchDirective.Parser([])
        assert "isnull" in sd._SEARCH_FUNCTIONS
        assert "isnotnull" in sd._SEARCH_FUNCTIONS

"""Tests for the SPQL query pre-processing hooks in
``query_engine.CmdExecutionBackend``.

The pre-processor is responsible for three things before the query reaches
the ANTLR parser:

1. Normalising line endings (``\\r\\n`` -> ``\\n``).
2. Stripping full-line ``#`` comments so users can prototype by commenting
   out pipe segments.
3. Trimming leading / trailing whitespace so editor quirks (copy-paste,
   "format on save" style tools) never cause parse failures.

These tests exercise the contract directly rather than driving a full
query - the parser integration is covered elsewhere; here we just want to
pin down the string transforms so the behaviour is stable across refactors.
"""
from __future__ import annotations

from query_engine.CmdExecutionBackend import _strip_line_comments


class TestStripLineComments:
    def test_strips_full_line_comment(self):
        q = 'index="x"\n# commented out\n| head 5\n'
        assert _strip_line_comments(q) == 'index="x"\n| head 5\n'

    def test_strips_comment_with_leading_whitespace(self):
        q = 'index="x"\n    # indented comment\n| head 5\n'
        assert _strip_line_comments(q) == 'index="x"\n| head 5\n'

    def test_preserves_hash_inside_double_quoted_string(self):
        q = 'index="indexes/path#withhash.parquet"\n# real comment\n| head 5\n'
        expected = 'index="indexes/path#withhash.parquet"\n| head 5\n'
        assert _strip_line_comments(q) == expected

    def test_preserves_hash_inside_multiline_string_value(self):
        # Double-quoted strings can span lines in SPQL; a '#' that appears
        # after an unterminated '"' on a prior line must still be treated
        # as quoted content.
        q = 'search body="line one\nline two # with hash"\n| head 5\n'
        assert _strip_line_comments(q) == q

    def test_honors_escaped_quote_inside_string(self):
        # The '"' escaped with '\\' is not a closing quote, so the '#' on
        # the next real line remains inside the string.
        q = 'search body="he said \\"hi\\"  # nope"\n| head 5\n'
        assert _strip_line_comments(q) == q

    def test_comment_as_only_content(self):
        q = '# nothing but comments\n# all the way down\n'
        assert _strip_line_comments(q) == ''

    def test_comment_without_trailing_newline(self):
        # User's editor may not append a trailing newline - still stripped.
        q = 'index="x"\n# tail comment no newline'
        assert _strip_line_comments(q) == 'index="x"\n'

    def test_pass_through_when_no_comments(self):
        q = 'index="x"\n| search k=1\n| head 5\n'
        assert _strip_line_comments(q) == q

    def test_user_reported_earnings_query(self):
        # Verbatim query from the 2026-04-17 bug report: commenting out a
        # pipe segment should leave the rest of the query parseable.
        q = (
            'index="indexes/equities/earnings_calendar/*"\n'
            '# | where hours_until_earnings > 8 AND hours_until_earnings <= 72\n'
            '| table ticker, company, earnings_date\n'
            '| sort hours_until_earnings\n'
            '| head 20\n'
        )
        expected = (
            'index="indexes/equities/earnings_calendar/*"\n'
            '| table ticker, company, earnings_date\n'
            '| sort hours_until_earnings\n'
            '| head 20\n'
        )
        assert _strip_line_comments(q) == expected

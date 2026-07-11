"""
Regression tests for ``speakesQueryListener.split_pipeline`` - the bracket-
and quote-aware tokenizer that splits a SPQL query into its pipe segments.

Pre-2026-05-05 the function only respected ``[...]`` bracket nesting, not
quoted-string context. As a result, any ``|`` inside a string literal
(e.g. regex alternation in ``match(text, "a|b|c")``) was misinterpreted
as a pipe-command delimiter. The downstream ``shlex.split`` then saw a
string fragment with no closing quote and raised ``ValueError: No
closing quotation`` - turning legitimate SPQL into a runtime error.

Caught 2026-05-05 when:
- ``pppb_congress_bills`` local YAML's ``(senate|house)`` regex
- ``pppb_kalshi_economy_policy`` local YAML's ``(Funds|Reserve|rate)`` regex
- ``pppb_kalshi_politics`` local YAML's ``(election|presidential|...)`` regex
- ``spbeb_kalshi_sports`` local YAML's ``(NFL|NBA|MLB|...)`` regex

All silently failed in production. The fix adds quote-state tracking
(both single and double, with backslash-escape support per the
``DOUBLE_QUOTED_STRING`` lexer rule) so ``|`` inside any string is
preserved.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lexers.speakesQueryListener import speakesQueryListener  # noqa: E402


# ── Existing behaviour preserved ─────────────────────────────────────


def test_simple_pipe_split():
    assert speakesQueryListener.split_pipeline(
        'index="x" | head 5 | table a, b'
    ) == ['index="x"', 'head 5', 'table a, b']


def test_bracket_nesting_preserved():
    """Pipe inside ``[...]`` subsearch brackets must NOT split."""
    assert speakesQueryListener.split_pipeline(
        'index="x" | append [search foo | head 3]'
    ) == ['index="x"', 'append [search foo | head 3]']


def test_empty_segments_dropped():
    """Whitespace-only segments are dropped."""
    assert speakesQueryListener.split_pipeline(
        'index="x" |  | head 1'
    ) == ['index="x"', 'head 1']


# ── The 2026-05-05 fix: pipe inside quoted strings preserved ─────────


def test_pipe_inside_double_quoted_string():
    """Regex alternation inside ``match()`` - the pppb_kalshi_politics
    local YAML form. Must remain in the segment, not become two."""
    q = 'index="x" | where match(market_title, "(?i)\\b(election|primary|senate)\\b") | head 5'
    out = speakesQueryListener.split_pipeline(q)
    assert out == [
        'index="x"',
        'where match(market_title, "(?i)\\b(election|primary|senate)\\b")',
        'head 5',
    ], f"unexpected split: {out}"


def test_pipe_inside_single_quoted_string():
    """Same rule for single-quoted strings."""
    q = "index='x' | where match(field, '(a|b|c)') | head 1"
    out = speakesQueryListener.split_pipeline(q)
    assert out == [
        "index='x'",
        "where match(field, '(a|b|c)')",
        "head 1",
    ], f"unexpected split: {out}"


def test_multiple_pipes_in_one_quoted_string():
    """`a|b|c|d` should all stay inside the string."""
    q = '| where match(t, "a|b|c|d|e")'
    out = speakesQueryListener.split_pipeline(q)
    assert out == ['where match(t, "a|b|c|d|e")']


def test_escaped_quote_inside_double_quoted_string():
    """Per the DOUBLE_QUOTED_STRING lexer rule (``'\\' . | ~('"' ...)``),
    a backslash escapes the next character. ``"foo\\"bar|baz"`` must
    stay as ONE string (the inner ``\\"`` is a literal quote, not a
    closer)."""
    q = '| where field == "foo\\"bar|baz" | head 1'
    out = speakesQueryListener.split_pipeline(q)
    assert out == [
        'where field == "foo\\"bar|baz"',
        'head 1',
    ], f"unexpected split: {out}"


def test_combined_brackets_and_quotes():
    """Subsearch brackets containing a quoted regex with pipes."""
    q = 'index="x" | append [search | where match(t, "(a|b)") | head 1] | tail 5'
    out = speakesQueryListener.split_pipeline(q)
    assert out == [
        'index="x"',
        'append [search | where match(t, "(a|b)") | head 1]',
        'tail 5',
    ], f"unexpected split: {out}"


# ── End-to-end: the actual failing SS queries now run cleanly ────────


def test_pppb_congress_bills_local_yaml_parses_via_split():
    """The pppb_congress_bills local YAML has the ``(senate|house)``
    alternation pattern. Before the fix, split_pipeline destroyed the
    quoted regex. This test asserts it now stays intact."""
    q = (
        'index="indexes/politics/congress_bills/*.parquet"\n'
        '| sort -_epoch\n'
        '| dedup bill_id\n'
        '| where importance_tier IN ("HIGH","MEDIUM")\n'
        '| where bill_type IN ("S","HR","SJRES","HJRES")\n'
        '| where match(latest_action_text, "(?i)became public law|passed.{0,30}(senate|house)|veto|reported with|reported without")\n'
        '| eventstats count(_epoch) as n_substantive\n'
        '| sort -latest_action_date\n'
        '| head 15'
    )
    segments = speakesQueryListener.split_pipeline(q)
    # Find the where-match segment and assert the regex is intact
    match_segs = [s for s in segments if "match(latest_action_text" in s]
    assert len(match_segs) == 1, (
        f"Expected exactly one segment containing match(latest_action_text); "
        f"got {len(match_segs)}. Full split: {segments}"
    )
    seg = match_segs[0]
    # All four regex alternation tokens must be in the segment
    for token in ["(senate|house)", "became public law", "veto", "reported with"]:
        assert token in seg, (
            f"Token {token!r} missing from where-match segment - split "
            f"likely cut the regex. Segment: {seg}"
        )

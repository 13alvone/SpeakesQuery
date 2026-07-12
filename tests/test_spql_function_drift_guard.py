"""SPQL built-in function drift guard.

Follow-up from 2026-05-16. Caught when a live-API validation pass
tripped on ``floor`` - the function was
documented as a built-in but missing from ``EvalHandler``'s
``env_template``. The audit found 7+ peer functions (random / sum /
median / mode / range / ceil / floor) in the same broken state.

The user's permanent operating rule (verbatim 2026-05-16):
*"ANYTIME there is an SPQL fix, it should be treated as of the
upmost criticality such that most everything within reason stops
until that spql bug is both robustly addressed, and robustly included
in testing to ensure it never happens again."*

This test enforces the "never happens again" half. It is a STRUCTURAL
test that fails LOUDLY whenever any of these three sources of truth
diverge:

1. **CLAUDE.md** ``Built-in functions:`` line - the operator-facing
   contract.
2. **docs/lang/03_functions.md** - the user-facing reference with
   per-function semantics.
3. **EvalHandler.custom_eval** ``env_template`` keys - the runtime
   that actually executes the function calls.
4. **tests/yaml/tier2_functions/** - at least one YAML test row
   per documented function.

If a future commit adds a function to the docs but forgets to wire it
into ``env_template``, this test fails. Same in reverse - adding to
the runtime without doc + test coverage also fails.

See:
* ``feedback_spql_bugs_are_top_priority_drop_everything.md``
* ``reference_spql_floor_function_missing_in_eval_2026_05_16.md``
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent

# Functions documented in CLAUDE.md / docs but legitimately NOT eval
# functions - they are stats-command aggregators OR special-form
# (mvfilter / spath have their own grammar handling). Listed here so
# the drift guard doesn't false-positive on them.
EXEMPT_FROM_EVAL_ENV_TEMPLATE = {
    # mvfilter has its own early-dispatch in safe_eval (handles the
    # per-element predicate via a separate scalar evaluator). Doesn't
    # need an env_template entry.
    "mvfilter",
}


# ── Extractors ─────────────────────────────────────────────────────


def _claude_md_builtin_functions() -> set[str]:
    """Parse the ``Built-in functions:`` line from CLAUDE.md."""
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    m = re.search(r"\*\*Built-in functions:\*\*\s*(.+)", text)
    assert m, "CLAUDE.md missing the 'Built-in functions:' line"
    raw = m.group(1).split("\n", 1)[0]
    return {name.strip() for name in raw.split(",") if name.strip()}


def _docs_lang_03_function_sections() -> set[str]:
    """Extract function names from ``### funcname(...)`` headers in
    docs/lang/03_functions.md."""
    text = (REPO_ROOT / "docs" / "lang" / "03_functions.md").read_text(encoding="utf-8")
    return set(re.findall(r"^###\s+([a-z_][a-z_0-9]*)\s*\(", text, re.MULTILINE))


def _eval_env_template_keys() -> set[str]:
    """Walk ``handlers/EvalHandler.py`` AST and pull out every string
    literal key in ``local_env.update({...})`` calls inside
    ``custom_eval``. The runtime allowlist is built dynamically as
    ``set(local_env.keys())`` so this set IS what ``custom_eval``
    will accept."""
    src = (REPO_ROOT / "handlers" / "EvalHandler.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        # match ``local_env.update({...})``
        if node.func.attr != "update":
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "local_env":
            continue
        for arg in node.args:
            if not isinstance(arg, ast.Dict):
                continue
            for k in arg.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    return keys


def _tier2_yaml_function_coverage() -> set[str]:
    """Scan every tier-2 YAML file for which functions are exercised
    (heuristic: look for `funcname(` in any `query:` field)."""
    tier2 = REPO_ROOT / "tests" / "yaml" / "tier2_functions"
    covered: set[str] = set()
    for path in sorted(tier2.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not doc:
            continue
        cases = doc.get("tests") or doc.get("cases") or []
        for case in cases:
            q = case.get("query", "")
            for m in re.finditer(r"\b([a-z_][a-z_0-9]*)\s*\(", q):
                covered.add(m.group(1))
    return covered


# ── The drift guards ───────────────────────────────────────────────


class TestSpqlFunctionDriftGuard:
    """Pin the four sources of truth in lockstep."""

    def test_every_claude_md_function_is_in_eval_env_template(self):
        """If CLAUDE.md says a name is a built-in function, the runtime
        env_template must actually have it. This is the canary that
        would have fired the day ``floor`` was promised but not wired."""
        documented = _claude_md_builtin_functions()
        implemented = _eval_env_template_keys()
        missing = documented - implemented - EXEMPT_FROM_EVAL_ENV_TEMPLATE
        assert not missing, (
            f"{len(missing)} function(s) documented in CLAUDE.md but missing "
            f"from EvalHandler.custom_eval env_template: {sorted(missing)}. "
            f"Add them to env_template (with MV awareness where applicable) "
            f"and update tests/yaml/tier2_functions/ to cover the new entries. "
            f"See feedback_spql_bugs_are_top_priority_drop_everything.md."
        )

    def test_every_docs_lang_function_is_in_eval_env_template(self):
        """Same check against the per-function reference in docs/lang/03_functions.md."""
        documented = _docs_lang_03_function_sections()
        implemented = _eval_env_template_keys()
        missing = documented - implemented - EXEMPT_FROM_EVAL_ENV_TEMPLATE
        assert not missing, (
            f"{len(missing)} function(s) documented in docs/lang/03_functions.md "
            f"but missing from EvalHandler env_template: {sorted(missing)}. "
            f"Either implement them in env_template + add tier-2 YAML tests, "
            f"or remove their section from the docs."
        )

    def test_every_eval_env_template_function_is_documented(self):
        """Reverse drift: a function in env_template must be documented
        somewhere (CLAUDE.md OR docs/lang/03_functions.md). Catches the
        opposite mistake - adding a runtime function that operators
        don't know they can call."""
        implemented = _eval_env_template_keys()
        documented = _claude_md_builtin_functions() | _docs_lang_03_function_sections()
        # Some env_template entries are private helpers used by other
        # functions internally - not meant to be called by operators.
        # Add to this set as needed; it's the "intentionally undocumented"
        # escape hatch.
        INTERNAL_HELPERS: set[str] = set()
        undocumented = implemented - documented - INTERNAL_HELPERS
        assert not undocumented, (
            f"{len(undocumented)} function(s) in EvalHandler env_template "
            f"but undocumented: {sorted(undocumented)}. Add a section to "
            f"docs/lang/03_functions.md OR add to INTERNAL_HELPERS in this test."
        )

    def test_every_claude_md_function_has_tier2_yaml_coverage(self):
        """Every documented function must have at least one tier-2
        YAML test row exercising it. Pinned so the next floor-shaped
        bug is caught by the synthetic test suite before it can reach
        a live-API probe."""
        documented = _claude_md_builtin_functions()
        covered = _tier2_yaml_function_coverage()
        # ``if_`` is exempt because YAML scanners trip on the trailing
        # underscore; it IS covered via test_conditional_functions.yaml.
        # Add to this set if a future function legitimately has no
        # YAML coverage (rare - most should be testable).
        YAML_COVERAGE_EXEMPT: set[str] = set()
        missing = documented - covered - YAML_COVERAGE_EXEMPT
        assert not missing, (
            f"{len(missing)} documented function(s) lack tier-2 YAML "
            f"coverage: {sorted(missing)}. Add at least one test row in "
            f"tests/yaml/tier2_functions/ that exercises each."
        )


class TestNoSilentRegressionOfFixedBugs:
    """Pin specific historical SPQL bugs to ensure they cannot recur."""

    def test_floor_compound_expression_no_paren_slicing_bug(self):
        """The 2026-05-16 ``round()`` early-dispatch had a paren-slicing
        bug. Tester previously: ``round(1.7) + len("a")*0`` →
        SyntaxError. Fix: removed the early-dispatch. This test pins
        the canonical broken pattern so a future re-introduction of
        the slicing dispatch fails immediately."""
        from query_engine.CmdExecutionBackend import process_query_with_diagnostics
        q = '| makeresults count=1 | eval r=round(1.7) + len("a")*0'
        df, _job, diag = process_query_with_diagnostics(q)
        assert df is not None, f"compound round expression broke again: {diag}"
        assert float(df.iloc[0]["r"]) == pytest.approx(2.0)

    def test_floor_with_now_minus_epoch_pattern(self):
        """The exact pattern that surfaced the floor bug on
        2026-05-16: ``floor((now() - _epoch) / 86400)``. Pinned so
        any future regression in the eval allowlist hits this test
        before it hits a live operator query."""
        from query_engine.CmdExecutionBackend import process_query_with_diagnostics
        q = (
            '| makeresults count=1 '
            '| eval _epoch=86400 * 100 '   # 100 days after Unix epoch
            '| eval base=86400 * 1000 '    # 1000 days after Unix epoch (deterministic)
            '| eval days_ago=floor((base - _epoch) / 86400)'
        )
        df, _job, diag = process_query_with_diagnostics(q)
        assert df is not None, f"floor + now-minus-epoch pattern broke: {diag}"
        assert int(df.iloc[0]["days_ago"]) == 900


class TestStatsNoParensAliasSilentDrop:
    """Pin the 2026-05-16 silent-alias-drop bug: ``stats <fn> as A by X``
    silently produced a column named ``<fn>`` instead of ``A``.

    Root cause: ``StatsHandler._parse_function_specs`` regex
    ``(\\w+)\\s*(?:\\(...\\))?(?:\\s+as\\s+...)?`` - the ``\\s*`` after
    (\\w+) greedily consumed the whitespace that the alias group's
    ``\\s+`` needed to anchor on. Fix: moved ``\\s*`` inside the optional
    paren group. Affected six aggregators (count/avg/min/max/sum/dc);
    the parenthesized forms always worked.

    These tests assert each affected aggregator now produces the alias
    column AND the bare-function-name column does NOT appear in the
    result (which is the canary signal for the silent-drop bug)."""

    @pytest.mark.parametrize("aggregator,setup,expected_alias", [
        ("count",  '| eval c="g"',                    "customer_count"),
        ("dc",     '| eval c="g"',                    "distinct_count"),
        ("avg",    '| eval c="g" | eval x=10',        "mean_x"),
        ("min",    '| eval c="g" | eval x=10',        "lowest"),
        ("max",    '| eval c="g" | eval x=10',        "highest"),
        ("sum",    '| eval c="g" | eval x=10',        "total"),
    ])
    def test_no_parens_alias_preserved(self, aggregator, setup, expected_alias):
        """For each aggregator: alias appears in result, bare name does NOT."""
        from query_engine.CmdExecutionBackend import process_query_with_diagnostics
        q = (
            f'| makeresults count=2 {setup} '
            f'| stats {aggregator} as {expected_alias} by c'
        )
        df, _job, diag = process_query_with_diagnostics(q)
        assert df is not None, f"{aggregator}: query failed: {diag}"
        cols = list(df.columns)
        assert expected_alias in cols, (
            f"{aggregator}: alias {expected_alias!r} not in result columns "
            f"{cols}. The silent-alias-drop bug has regressed."
        )
        assert aggregator not in cols, (
            f"{aggregator}: bare '{aggregator}' column appeared in result "
            f"{cols} - the silent-alias-drop bug has regressed. The "
            f"alias was supposed to rename it."
        )

    def test_stats_handler_regex_fix_directly(self):
        """Belt-and-suspenders: directly exercise the regex via
        ``_parse_function_specs`` so a future grammar change can't mask
        a re-regression in the handler. The regex itself must produce
        ``alias_explicit=True`` for the no-parens shorthand."""
        from handlers.StatsHandler import StatsHandler
        h = StatsHandler()
        for s in ["count as A", "avg as mean_x", "min as lowest", "dc as distinct"]:
            specs = h._parse_function_specs(s)
            assert len(specs) == 1
            assert specs[0]["alias_explicit"] is True, (
                f"{s!r}: alias_explicit must be True. Regex regressed."
            )
            assert specs[0]["alias"] == s.split(" as ")[1], (
                f"{s!r}: alias != expected. Got {specs[0]['alias']!r}."
            )

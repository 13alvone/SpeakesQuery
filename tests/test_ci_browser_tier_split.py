"""CI browser-tier split drift guards (2026-07-12).

The very first CI run failed because the fast tests job excluded the
Playwright tier by FILE (--ignore=tests/test_ui.py --ignore=tests/test_ui_crud.py)
while a third browser-fixture file (tests/test_redesign_2026_04_26.py)
existed outside that list - its 10 browser tests errored at fixture
setup on a runner with no Chromium installed.

The fix is marker-based: conftest auto-applies @pytest.mark.browser to
any test whose fixture closure includes browser_instance, and both CI
jobs select by that marker. These guards pin every piece of that
contract so it cannot silently regress:

1. pytest.ini registers the browser marker.
2. conftest.py auto-marks from fixture usage (source scan).
3. ci.yml selects by marker in both jobs, never by file ignore list.
4. Behavioral: a real collect-only run proves the auto-mark selects
   and deselects browser tests correctly.
"""

import configparser
import os
import re
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI_YML = os.path.join(PROJECT_ROOT, ".github", "workflows", "ci.yml")
CONFTEST = os.path.join(PROJECT_ROOT, "tests", "conftest.py")
PYTEST_INI = os.path.join(PROJECT_ROOT, "pytest.ini")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestMarkerRegistration:
    def test_pytest_ini_registers_browser_marker(self):
        parser = configparser.ConfigParser()
        parser.read(PYTEST_INI, encoding="utf-8")
        markers = parser.get("pytest", "markers")
        assert re.search(r"^\s*browser\s*:", markers, re.MULTILINE), (
            "pytest.ini must register the 'browser' marker - without it "
            "the auto-mark in conftest raises PytestUnknownMarkWarning "
            "and CI's -m selection silently matches nothing."
        )


class TestConftestAutoMark:
    def test_conftest_auto_marks_from_browser_instance_fixture(self):
        src = _read(CONFTEST)
        assert "def pytest_collection_modifyitems(" in src, (
            "conftest.py must define pytest_collection_modifyitems - it "
            "is what derives the browser marker from fixture usage."
        )
        hook_src = src.split("def pytest_collection_modifyitems(", 1)[1]
        hook_src = hook_src.split("\n@", 1)[0]
        assert "browser_instance" in hook_src and "add_marker" in hook_src, (
            "The collection hook must add the browser marker to every "
            "item whose fixture closure includes browser_instance. "
            "Hand-applied markers rot; fixture-derived markers cannot."
        )


class TestCiWorkflowSelection:
    def test_fast_job_excludes_browser_and_network_tiers(self):
        src = _read(CI_YML)
        assert '-m "not smoke and not live_integration and not browser"' in src, (
            "The fast tests job must exclude browser + smoke + "
            "live_integration via one explicit -m expression. An "
            "explicit -m OVERRIDES the pytest.ini addopts marker "
            "expression, so dropping any clause re-enables that tier."
        )

    def test_no_job_excludes_browser_tier_by_file_ignore(self):
        src = _read(CI_YML)
        assert "--ignore=tests/test_ui" not in src, (
            "CI must not exclude the browser tier by file ignore list - "
            "that is exactly what broke the first CI run when a browser "
            "test landed outside tests/test_ui*.py."
        )

    def test_ui_job_selects_by_marker_after_browser_install(self):
        src = _read(CI_YML)
        assert "playwright install chromium" in src
        assert re.search(r"pytest\s+-q\s+-m\s+browser\b", src), (
            "The ui-tests job must select by '-m browser' so it picks "
            "up browser tests wherever they live."
        )


class TestAutoMarkBehavior:
    """Real collect-only runs proving the marker actually selects."""

    def _collect(self, marker_expr, target):
        proc = subprocess.run(
            [
                sys.executable, "-m", "pytest", "--collect-only", "-q",
                "-m", marker_expr, target,
            ],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=120,
        )
        m = re.search(r"(\d+)(?:/\d+)? tests? collected", proc.stdout)
        if m is None:
            assert "no tests collected" in proc.stdout or "no tests ran" in proc.stdout, (
                f"Unexpected collect-only output:\n{proc.stdout}\n{proc.stderr}"
            )
            return 0
        return int(m.group(1))

    def test_browser_marker_selects_ui_tests(self):
        n = self._collect("browser", "tests/test_ui.py")
        assert n > 0, (
            "-m browser selected nothing from tests/test_ui.py - the "
            "conftest auto-mark is broken, so the nightly ui-tests job "
            "would silently run zero browser tests."
        )

    def test_not_browser_deselects_every_ui_test(self):
        n = self._collect("not browser", "tests/test_ui.py")
        assert n == 0, (
            f"'-m not browser' still selected {n} test(s) from "
            "tests/test_ui.py - browser tests would run (and error) in "
            "the fast CI job that has no Chromium installed."
        )

    def test_redesign_file_splits_across_both_tiers(self):
        target = "tests/test_redesign_2026_04_26.py"
        n_browser = self._collect("browser", target)
        n_fast = self._collect("not browser", target)
        assert n_browser > 0, (
            "test_redesign_2026_04_26.py has browser-fixture tests; "
            "-m browser must select them."
        )
        assert n_fast > 0, (
            "test_redesign_2026_04_26.py also has source-scan tests "
            "that must keep running in the fast job."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])

"""MEDIUMs batch 1 - M-CE-6, M-CE-7, M-CE-8 regressions.

Three fixes from the 2026-04-21 production review:

  * **M-CE-6** - dead ``finally: if attempt == max_retries or True: pass``
    block removed from ``scheduled_input_engine/engine.py::_run_task``.
  * **M-CE-7** - CREDENTIALS dict is popped out of the ``extra`` globals
    immediately after the ThreadPoolExecutor call returns or raises.
  * **M-CE-8** - ``result_df.fillna('')`` replaced with a dtype-aware
    fill so numeric columns stay numeric after SPQL query execution
    (previously downstream ``stats sum`` coerced empty strings to 0).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ======================================================================
# M-CE-6: no `or True` tautology remains in _run_task
# ======================================================================

class TestNoTautologyInEngine:
    """Source-invariant regression: the dead finally-block is gone."""

    ENGINE = _PROJECT_ROOT / "scheduled_input_engine" / "engine.py"

    def test_no_or_true_tautology_in_engine(self):
        """Match ``if <expr> or True:`` in executable code only (comments / docstrings stripped)."""
        import re

        # Strip line comments so a migration-note comment like
        # ``# if x or True:`` doesn't trip the regex.
        cleaned_lines = []
        for line in self.ENGINE.read_text().splitlines():
            # Find the first '#' that isn't inside a string literal. A
            # full-fidelity tokenizer would be overkill; the naive strip
            # is fine for engine.py (no strings-with-hash outside comments).
            stripped = line.split("#", 1)[0]
            cleaned_lines.append(stripped)
        cleaned = "\n".join(cleaned_lines)

        bad = re.findall(r"if\s+[^:]*\bor\s+True\s*:", cleaned)
        assert bad == [], (
            f"Dead `or True` tautology reintroduced in engine.py: {bad}"
        )


# ======================================================================
# M-CE-7: CREDENTIALS dict is popped after script execution
# ======================================================================

class TestCredentialsDictPoppedAfterExec:
    """The ``extra`` dict passed to CodeExecutor must not retain CREDENTIALS after execute()."""

    def _make_engine_with_fake_vault(self, creds: dict):
        from scheduled_input_engine.engine import ScheduledInputEngine

        engine = ScheduledInputEngine()

        class _FakeVault:
            def decrypt_for_script(self, _task_id):
                return creds

        engine._vault = _FakeVault()
        return engine

    def test_credentials_popped_after_success(self, monkeypatch):
        """After a successful script run, the CREDENTIALS key must be gone from the globals dict."""
        from scheduled_input_engine.engine import ScheduledInputEngine
        from scheduled_input_engine import engine as engine_mod

        captured = {}

        def fake_execute(self, extra_globals=None):
            # Capture the dict reference so we can inspect it AFTER the
            # run_task flow completes.
            captured["extra"] = extra_globals
            # Return a valid DataFrame with _epoch so _run_task proceeds.
            import pandas as _pd
            df = _pd.DataFrame({"x": [1], "_epoch": [1]})
            return df

        monkeypatch.setattr(
            engine_mod.CodeExecutor, "execute", fake_execute,
        )
        # Suppress the atomic Parquet write so the fake DataFrame doesn't
        # need to land on disk.
        monkeypatch.setattr(
            engine_mod.ParquetWriter, "write_atomic",
            lambda self, *a, **kw: Path("/tmp/fake.parquet"),
        )

        engine = self._make_engine_with_fake_vault({"API_KEY": "sk-fake-1234"})
        try:
            task = {
                "id": 111,
                "title": "mce7_test",
                "trust_level": "sandboxed",
                "code": (
                    "import pandas as pd\n"
                    "df = pd.DataFrame({'_epoch': [1]})\n"
                    "GENERATE_RESULTS(df, 'x.system4.system4.parquet')\n"
                ),
                "cron_schedule": "* * * * *",
                "overwrite": False,
                "subdirectory": "_test_mce7",
            }
            engine._run_task(task)
        finally:
            try:
                engine._scheduler.shutdown(wait=False)
            except Exception:
                pass

        # The executor's globals dict must have had CREDENTIALS during the
        # call but NOT after - the finally in _run_task pops it.
        extra = captured["extra"]
        assert "CREDENTIALS" not in extra, (
            f"CREDENTIALS lingered in extra globals after script run; "
            f"keys={list(extra.keys())!r}"
        )

    def test_credentials_popped_after_exception(self, monkeypatch):
        """Exception mid-execution also pops CREDENTIALS before the retry/error branches."""
        from scheduled_input_engine.engine import ScheduledInputEngine
        from scheduled_input_engine import engine as engine_mod

        captured = {}

        def fake_execute(self, extra_globals=None):
            captured["extra"] = extra_globals
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(engine_mod.CodeExecutor, "execute", fake_execute)
        # Force max_retries=0 so we hit the final-fail path immediately.
        monkeypatch.setattr(
            engine_mod, "MAX_RETRIES", 0,
        )

        engine = self._make_engine_with_fake_vault({"API_KEY": "sk-exc-case"})
        engine._setting = lambda key, default=None: (
            0 if key == "max_retries" else default
        )
        try:
            task = {
                "id": 112,
                "title": "mce7_exc",
                "trust_level": "sandboxed",
                "code": (
                    "import pandas as pd\n"
                    "df = pd.DataFrame({'_epoch': [1]})\n"
                    "GENERATE_RESULTS(df, 'x.system4.system4.parquet')\n"
                ),
                "cron_schedule": "* * * * *",
                "overwrite": False,
                "subdirectory": "_test_mce7_exc",
            }
            engine._run_task(task)
        finally:
            try:
                engine._scheduler.shutdown(wait=False)
            except Exception:
                pass

        assert "CREDENTIALS" not in captured["extra"], (
            "CREDENTIALS lingered after an exception - the try/finally in "
            "_run_task must pop on every exit path."
        )


# ======================================================================
# M-CE-8: dtype-aware fillna preserves numeric types
# ======================================================================

class TestDtypeAwareFillna:
    """``_fillna_dtype_aware`` keeps numeric / datetime / bool columns typed."""

    def test_numeric_nan_filled_with_zero(self):
        from query_engine.CmdExecutionBackend import _fillna_dtype_aware

        df = pd.DataFrame({
            "level": ["INFO", None, "ERROR"],
            "count": [1.0, float("nan"), 3.0],
            "_epoch": [100, None, 102],
        })
        out = _fillna_dtype_aware(df)

        # String column gets "" for NaN.
        assert out["level"].tolist() == ["INFO", "", "ERROR"]
        # Numeric column stays numeric.
        assert pd.api.types.is_numeric_dtype(out["count"]), (
            f"count lost numeric dtype: {out['count'].dtype}"
        )
        assert out["count"].tolist() == [1.0, 0.0, 3.0]
        # Sum/avg now work correctly - the downstream SPQL stats pipeline.
        assert out["count"].sum() == 4.0

    def test_bool_nan_filled_with_false(self):
        from query_engine.CmdExecutionBackend import _fillna_dtype_aware

        df = pd.DataFrame({
            "flag": pd.Series([True, None, False], dtype="object"),
        })
        # Cast to a real bool column with NaN - use nullable bool.
        df["flag"] = df["flag"].astype("boolean")

        out = _fillna_dtype_aware(df)
        assert out["flag"].tolist() == [True, False, False]

    def test_datetime_nan_stays_datetime(self):
        from query_engine.CmdExecutionBackend import _fillna_dtype_aware

        df = pd.DataFrame({
            "ts": pd.to_datetime(
                ["2026-01-01", None, "2026-01-03"], utc=True,
            ),
        })
        out = _fillna_dtype_aware(df)
        assert pd.api.types.is_datetime64_any_dtype(out["ts"])
        # Middle value is NaT, the datetime-aware null.
        assert pd.isna(out["ts"].iloc[1])
        assert out["ts"].iloc[0] == pd.Timestamp("2026-01-01", tz="UTC")

    def test_string_nan_still_filled_with_empty_string(self):
        from query_engine.CmdExecutionBackend import _fillna_dtype_aware

        df = pd.DataFrame({"msg": ["hi", None, "bye"]})
        out = _fillna_dtype_aware(df)
        assert out["msg"].tolist() == ["hi", "", "bye"]

    def test_integrates_via_process_query_preserves_numeric(self, tmp_path):
        """End-to-end: a query with NaN-bearing numeric column still sums correctly."""
        # Write a tiny parquet with a NaN in a float column and run a stats
        # pipe through the real engine.
        p = _PROJECT_ROOT / "indexes" / "_test_mce8" / "fill.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            df = pd.DataFrame({
                "category": ["a", "b", "a"],
                "amount": [10.0, float("nan"), 5.0],
                "_epoch": [1, 2, 3],
            })
            df.to_parquet(p, index=False, compression="gzip")

            from query_engine.CmdExecutionBackend import process_query
            rel = p.relative_to(_PROJECT_ROOT)
            query = (
                f'index="{rel}" | stats sum(amount) as total'
            )
            out_df, _job = process_query(query)
            assert out_df is not None and not out_df.empty
            # Old behavior: fillna('') made amount object-dtype, and
            # stats sum either errored or coerced to 0. With the fix,
            # the NaN is treated as 0 numerically and the total is 15.
            total = out_df["total"].iloc[0]
            assert float(total) == 15.0, (
                f"Expected total=15.0 after dtype-aware fillna; got {total!r}"
            )
        finally:
            try:
                p.unlink()
                p.parent.rmdir()
            except Exception:
                pass

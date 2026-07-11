"""
Code Executor
─────────────
AST processing, trust-tiered execution, and Parquet output.

Trust tiers
  * ``sandboxed`` (default) - RestrictedPython with a module allowlist:
    pandas, requests, json, datetime, time, re, math, hashlib, base64,
    collections, io, bs4 (BeautifulSoup), lxml.  Everything else is denied.
  * ``unrestricted`` - plain ``compile()`` with full ``__builtins__`` and
    no import filter.  Opt-in per script via the ``trust_level`` JSON field
    (or the ``trust_level`` column on ``scheduled_inputs``).  Resource
    budgets (HTTP count, response size, wall-clock timeout, output rows)
    still apply in both tiers via ``engine.py``.

Mandatory test gate:
  Scripts must pass ``execute_test()`` before they can be saved.  The test
  validates column hygiene, _epoch presence, and returns a structured result.
"""

import ast
import base64
import collections
import datetime
import hashlib
import io
import json
import logging
import math
import os
import re
import time
import uuid
from pathlib import Path
from types import MappingProxyType

import pandas as pd
import requests
from RestrictedPython import compile_restricted
from RestrictedPython.Guards import safe_builtins
from RestrictedPython import utility_builtins

try:
    import bs4
except ImportError:
    bs4 = None

try:
    import lxml
except ImportError:
    lxml = None

logger = logging.getLogger(__name__)

INDEXES_DIR = Path(__file__).parent.parent / "indexes"

# ── Sandbox allowlist ─────────────────────────────────────────────

ALLOWED_MODULES: dict = {
    "pd": pd,
    "pandas": pd,
    "requests": requests,
    "json": json,
    "datetime": datetime,
    "time": time,
    "re": re,
    "math": math,
    "hashlib": hashlib,
    "base64": base64,
    "collections": collections,
    "io": io,
}

# Optional scraping libraries - available when installed
if bs4 is not None:
    ALLOWED_MODULES["bs4"] = bs4
if lxml is not None:
    ALLOWED_MODULES["lxml"] = lxml

# Attributes that sandbox code must never reach
_BLOCKED_ATTRS = frozenset({
    "__subclasses__", "__globals__", "__code__", "__closure__",
    "__bases__", "__mro__", "__class__", "__dict__",
    "__delattr__", "__setattr__", "__reduce__", "__reduce_ex__",
    "__import__",
})


def _safe_getattr(obj, name, default=None):
    """RestrictedPython _getattr_ guard - blocks dunder escape vectors."""
    if isinstance(name, str) and name in _BLOCKED_ATTRS:
        raise AttributeError(f"Access to '{name}' is denied")
    return getattr(obj, name, default)


def _safe_hasattr(obj, name) -> bool:
    """Sandbox-safe ``hasattr`` that honours ``_BLOCKED_ATTRS``.

    M-CE-9 (2026-04-22): the stock ``hasattr`` bypasses ``_safe_getattr``
    and returns ``True`` for dunder attributes that are otherwise blocked
    for actual access. A sandboxed script could use it to probe the
    subclass graph (``hasattr(cls, '__subclasses__')`` → ``True``) even
    though subsequent ``getattr(cls, '__subclasses__')`` is denied. The
    block list is the same set used by ``_safe_getattr`` so existence
    checks and access checks agree.
    """
    if isinstance(name, str) and name in _BLOCKED_ATTRS:
        return False
    return hasattr(obj, name)


_IMPORTABLE_NAMES = {
    "pandas", "requests", "json", "datetime", "time",
    "re", "math", "hashlib", "base64", "collections", "io",
    "bs4", "lxml", "lxml.html",
}


def _safe_import(name, *args, **kwargs):
    """Restricted __import__ - only allow modules in the allowlist."""
    if name in ALLOWED_MODULES or name in _IMPORTABLE_NAMES:
        # Give a helpful message if bs4/lxml are allowed but not installed
        if name in ("bs4", "lxml", "lxml.html"):
            pkg = "beautifulsoup4" if name == "bs4" else "lxml"
            try:
                return __import__(name, *args, **kwargs)
            except ImportError:
                raise ImportError(
                    f"'{name}' is allowed but not installed. "
                    f"Run: pip install {pkg}"
                )
        return __import__(name, *args, **kwargs)
    raise ImportError(f"Import of '{name}' is not allowed in ingestion scripts")


# ── Trust tier constants ──────────────────────────────────────────

TRUST_SANDBOXED = "sandboxed"
TRUST_UNRESTRICTED = "unrestricted"
VALID_TRUST_LEVELS = frozenset({TRUST_SANDBOXED, TRUST_UNRESTRICTED})


def _normalise_trust_level(value: str | None) -> str:
    """Return a validated trust level, defaulting to sandboxed."""
    if not value:
        return TRUST_SANDBOXED
    v = str(value).strip().lower()
    if v not in VALID_TRUST_LEVELS:
        raise ValueError(
            f"Invalid trust_level '{value}'. Must be one of "
            f"{sorted(VALID_TRUST_LEVELS)}."
        )
    return v


# ── Shared sandbox globals builder ────────────────────────────────

def _build_sandbox_globals(extra_globals: dict | None = None) -> dict:
    """Construct the restricted globals dict for both execute() and execute_test()."""
    restricted = dict(safe_builtins)
    restricted.update(utility_builtins)

    # Standard builtins that RestrictedPython's safe_builtins omits
    restricted["_getiter_"] = iter
    restricted["_getitem_"] = lambda obj, idx: obj[idx]
    restricted["_print_"] = print
    restricted["_getattr_"] = _safe_getattr
    restricted["_write_"] = lambda obj: obj
    restricted["__import__"] = _safe_import
    restricted["sorted"] = sorted
    restricted["len"] = len
    restricted["range"] = range
    restricted["enumerate"] = enumerate
    restricted["min"] = min
    restricted["max"] = max
    restricted["zip"] = zip
    restricted["map"] = map
    restricted["filter"] = filter
    restricted["list"] = list
    restricted["dict"] = dict
    restricted["set"] = set
    restricted["tuple"] = tuple
    restricted["str"] = str
    restricted["int"] = int
    restricted["float"] = float
    restricted["bool"] = bool
    restricted["isinstance"] = isinstance
    restricted["type"] = type
    restricted["round"] = round
    restricted["abs"] = abs
    restricted["sum"] = sum
    restricted["any"] = any
    restricted["all"] = all
    restricted["reversed"] = reversed
    restricted["hasattr"] = _safe_hasattr

    # Allowed modules
    restricted.update(ALLOWED_MODULES)

    # Caller-provided extras (credentials, cache helper, etc.)
    if extra_globals:
        restricted.update(extra_globals)

    return restricted


def _build_unrestricted_globals(extra_globals: dict | None = None) -> dict:
    """Globals dict for trust_level='unrestricted' - full __builtins__, no import filter.

    Resource budgets (HTTP count, timeout, output rows) are still enforced by the
    engine layer via ``BudgetAwareRequests`` injection and ``ThreadPoolExecutor``
    timeouts - those wrap this globals dict, they don't live inside it.
    """
    import builtins

    unrestricted: dict = {"__builtins__": builtins}

    # Caller-provided extras (credentials, cache helper, BudgetAwareRequests, etc.)
    if extra_globals:
        unrestricted.update(extra_globals)

    return unrestricted


class CodeExecutor:
    """Parse, compile, and execute user-provided ingestion code.

    Execution follows the script's declared ``trust_level``:

    * ``sandboxed`` (default) - RestrictedPython with the module allowlist.
    * ``unrestricted`` - plain ``compile()`` with full ``__builtins__``.
      Caller is responsible for the decision; the engine forwards the value
      from the script record.
    """

    def __init__(
        self,
        code: str,
        test_mode: bool = False,
        timestamp_fields: list | None = None,
        trust_level: str = TRUST_SANDBOXED,
    ):
        self.code = code
        self.test_mode = test_mode
        self.timestamp_fields = timestamp_fields or ["TIMESTAMP", "DATE", "CREATED_AT", "timestamp", "date", "created_at"]
        self.trust_level = _normalise_trust_level(trust_level)
        self.df_variable: str | None = None
        self.output_path: str | None = None
        self._compiled = self._process_code()

    # ------------------------------------------------------------------
    # AST processing + compilation
    # ------------------------------------------------------------------

    def _process_code(self):
        """Parse user code, extract GENERATE_RESULTS, compile with RestrictedPython."""
        tree = ast.parse(self.code, mode="exec")

        if not self.test_mode:
            transformer = _GenerateResultsTransformer()
            transformer.visit(tree)
            if not transformer.found:
                raise ValueError("Code must contain GENERATE_RESULTS(<df_variable>).")
            self.df_variable = transformer.df_variable
            self.output_path = transformer.output_path
            if not isinstance(self.output_path, str) or not self.output_path.endswith(
                ".system4.system4.parquet"
            ):
                raise ValueError(
                    "Output path must be a string ending with '.system4.system4.parquet'."
                )
        else:
            extractor = _GenerateResultsExtractor()
            extractor.visit(tree)
            if not extractor.found:
                raise ValueError("Code must contain GENERATE_RESULTS(<df_variable>).")
            self.df_variable = extractor.df_variable

        fixed_tree = ast.fix_missing_locations(tree)
        if self.trust_level == TRUST_UNRESTRICTED:
            return compile(fixed_tree, "<user_code>", "exec")
        return compile_restricted(fixed_tree, "<user_code>", "exec")

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _build_globals(self, extra_globals: dict | None) -> dict:
        """Return globals dict appropriate for the script's trust level."""
        if self.trust_level == TRUST_UNRESTRICTED:
            return _build_unrestricted_globals(extra_globals)
        return _build_sandbox_globals(extra_globals)

    def execute(self, extra_globals: dict | None = None) -> pd.DataFrame:
        """Run compiled code in the configured trust tier. Returns a DataFrame with _epoch."""
        run_globals = self._build_globals(extra_globals)

        # Use a single dict for both globals and locals in every trust tier.
        # ``exec(code, globals, locals)`` with separate dicts binds top-level
        # ``from X import Y`` into the *locals* dict - but function bodies
        # only resolve names through their ``__globals__``. That means a
        # helper function that uses ``datetime`` (imported with ``from
        # datetime import datetime`` at the top of the script) silently
        # falls back to the pre-populated sandbox ``datetime`` module
        # instead of the class, producing ``'NoneType' object is not
        # callable`` at the first method call. Merging globals+locals fixes
        # this for both sandboxed and unrestricted mode.
        exec(self._compiled, run_globals)  # nosec B102
        result_df = run_globals.get(self.df_variable)

        if not isinstance(result_df, pd.DataFrame):
            raise ValueError(f"'{self.df_variable}' is not a pandas DataFrame.")

        self._ensure_epoch(result_df)
        return result_df

    def execute_test(self, extra_globals: dict | None = None) -> dict:
        """Mandatory test gate.  Executes the script and returns a structured result.

        The result includes pass/fail status, column info, row preview, _epoch
        validation, and any errors encountered.  A script must pass this test
        before it can be saved.
        """
        start_ms = time.monotonic()
        errors: list[str] = []
        result: dict = {
            "status": "fail",
            "columns": [],
            "row_count": 0,
            "head": [],
            "dtypes": {},
            "has_epoch": False,
            "epoch_source": None,
            "errors": errors,
            "duration_ms": 0,
        }

        try:
            run_globals = self._build_globals(extra_globals)

            captured_df: pd.DataFrame | None = None

            def mock_generate(df, *_args):
                nonlocal captured_df
                captured_df = df

            run_globals["GENERATE_RESULTS"] = mock_generate

            # Single-dict exec for the same reason as ``execute`` - see the
            # comment there. ``execute_test`` mirrors production semantics.
            exec(self._compiled, run_globals)  # nosec B102

            if not isinstance(captured_df, pd.DataFrame):
                errors.append("No DataFrame passed to GENERATE_RESULTS().")
                return result

            df = captured_df

            # ── Column hygiene ────────────────────────────────
            if df.empty:
                errors.append("DataFrame is empty (0 rows).")

            empty_cols = [c for c in df.columns if not str(c).strip()]
            if empty_cols:
                errors.append(f"Found {len(empty_cols)} empty/whitespace column name(s).")

            dup_cols = [c for c in df.columns if list(df.columns).count(c) > 1]
            if dup_cols:
                errors.append(f"Duplicate column names: {sorted(set(dup_cols))}")

            # ── Epoch validation ──────────────────────────────
            epoch_source = None
            has_epoch = "_epoch" in df.columns
            if has_epoch:
                epoch_source = "_epoch"
            else:
                for field in self.timestamp_fields:
                    if field in df.columns:
                        epoch_source = field
                        break
                if epoch_source is None:
                    errors.append(
                        f"No _epoch column and no parseable timestamp field found "
                        f"among {self.timestamp_fields}. Ingestion requires a "
                        f"timestamp for _epoch derivation."
                    )

            result.update({
                "columns": df.columns.tolist(),
                "row_count": len(df),
                "head": df.head(5).fillna("").to_dict(orient="records"),
                "dtypes": {k: str(v) for k, v in df.dtypes.items()},
                "has_epoch": has_epoch or epoch_source is not None,
                "epoch_source": epoch_source,
            })

            if not errors:
                result["status"] = "pass"

        except SyntaxError as exc:
            line_info = f" (line {exc.lineno})" if exc.lineno else ""
            errors.append(f"Syntax error{line_info}: {exc.msg or exc}")
            if exc.text:
                errors.append(f"  {exc.text.rstrip()}")

        except ImportError as exc:
            errors.append(
                f"{exc}. Only these modules are available in the sandbox: "
                f"pandas, requests, json, datetime, time, re, math, hashlib, "
                f"base64, collections, io, bs4, lxml."
            )

        except NameError as exc:
            hint = str(exc)
            # Suggest sandbox globals for common typos
            sandbox_names = [
                "pd", "pandas", "requests", "json", "datetime", "time",
                "re", "math", "hashlib", "base64", "collections", "io",
                "bs4", "lxml", "CREDENTIALS", "GENERATE_RESULTS",
                "get_cached_or_fetch", "BeautifulSoup",
            ]
            for name in sandbox_names:
                if name.lower() in hint.lower():
                    hint += f" (Hint: did you mean '{name}'?)"
                    break
            errors.append(hint)

        except TypeError as exc:
            msg = str(exc)
            if "GENERATE_RESULTS" in msg:
                errors.append(
                    f"{msg}. GENERATE_RESULTS() takes one argument: "
                    f"the DataFrame to output. Example: GENERATE_RESULTS(df)"
                )
            else:
                errors.append(f"Type error: {msg}")

        except ValueError as exc:
            errors.append(str(exc))

        except RuntimeError as exc:
            msg = str(exc)
            if "budget" in msg.lower() or "timeout" in msg.lower():
                errors.append(msg)
            else:
                errors.append(f"Runtime error: {msg}")

        except Exception as exc:
            # Connection / HTTP errors with guidance
            exc_type = type(exc).__name__
            msg = str(exc)
            if "ConnectionError" in exc_type or "ConnectTimeout" in exc_type:
                errors.append(
                    f"Connection failed: {msg}. Check the URL and ensure "
                    f"the domain is in Settings > Allowed API Domains."
                )
            elif "HTTPError" in exc_type:
                errors.append(
                    f"HTTP error: {msg}. If this is a 401/403, check your "
                    f"API credentials in the Credentials sidebar."
                )
            else:
                errors.append(f"{exc_type}: {msg}")

        result["duration_ms"] = int((time.monotonic() - start_ms) * 1000)
        return result

    # ------------------------------------------------------------------
    # Epoch column
    # ------------------------------------------------------------------

    def _ensure_epoch(self, df: pd.DataFrame) -> None:
        """Add _epoch column to a single DataFrame if missing. No disk scanning.

        Empty DataFrames are tolerated: scripts that legitimately find no
        rows (e.g. an arbitrage scanner on a quiet day) still need to write
        *something* so downstream schedulers can distinguish "ran, found
        nothing" from "failed to run". We stamp an empty ``_epoch`` column
        onto the zero-row frame so it writes cleanly to Parquet.
        """
        if "_epoch" in df.columns:
            return
        if df.empty:
            df["_epoch"] = pd.Series([], dtype="int64")
            logger.info("[i] Added empty _epoch column to zero-row DataFrame.")
            return
        for field in self.timestamp_fields:
            if field in df.columns:
                df["_epoch"] = (
                    pd.to_datetime(df[field], errors="coerce").astype("int64") // 10**9
                )
                logger.info("[i] Added _epoch column from '%s'", field)
                return
        raise ValueError(
            f"No parseable timestamp field found among {self.timestamp_fields}"
        )


# ------------------------------------------------------------------
# One-time migration utility
# ------------------------------------------------------------------

def backfill_epoch_column(indexes_dir=None, timestamp_fields=None):
    """Walk indexes/ and add _epoch to any parquet files missing it.

    This is a one-time migration - NOT called during normal execution.
    Run manually: python -c "from scheduled_input_engine.executor import backfill_epoch_column; backfill_epoch_column()"
    """
    root = Path(indexes_dir) if indexes_dir else INDEXES_DIR
    fields = timestamp_fields or ["TIMESTAMP", "DATE", "CREATED_AT"]
    updated = 0

    for parquet_file in root.rglob("*.system4.system4.parquet"):
        try:
            df = pd.read_parquet(parquet_file)
            if "_epoch" in df.columns:
                continue
            for field in fields:
                if field in df.columns:
                    df["_epoch"] = (
                        pd.to_datetime(df[field], errors="coerce").astype("int64")
                        // 10**9
                    )
                    df.to_parquet(parquet_file, index=False, compression="gzip")
                    updated += 1
                    logger.info("[i] Backfilled _epoch in %s", parquet_file)
                    break
        except Exception as exc:
            logger.error("[x] Failed to backfill %s: %s", parquet_file, exc)

    logger.info("[i] Backfill complete: %d files updated", updated)
    return updated


# ------------------------------------------------------------------
# AST visitors
# ------------------------------------------------------------------

class _GenerateResultsTransformer(ast.NodeTransformer):
    """Remove GENERATE_RESULTS() call from AST and extract its arguments."""

    def __init__(self):
        self.found = False
        self.df_variable = None
        self.output_path = None

    def visit_Expr(self, node):
        if isinstance(node.value, ast.Call):
            call = node.value
            if isinstance(call.func, ast.Name) and call.func.id == "GENERATE_RESULTS":
                self.found = True

                if len(call.args) == 1:
                    if not isinstance(call.args[0], ast.Name):
                        raise ValueError(
                            "GENERATE_RESULTS argument must be a variable name."
                        )
                    self.df_variable = call.args[0].id
                    self.output_path = (
                        f"{time.time()}_{uuid.uuid4()}.system4.system4.parquet"
                    )
                elif len(call.args) == 2:
                    if not isinstance(call.args[0], ast.Name):
                        raise ValueError(
                            "First argument to GENERATE_RESULTS must be a variable name."
                        )
                    self.df_variable = call.args[0].id
                    if isinstance(call.args[1], ast.Constant) and isinstance(
                        call.args[1].value, str
                    ):
                        self.output_path = call.args[1].value
                    else:
                        raise ValueError(
                            "Second argument to GENERATE_RESULTS must be a string."
                        )
                else:
                    raise ValueError(
                        "GENERATE_RESULTS takes one or two arguments."
                    )
                return None  # Remove the node from the AST
        return self.generic_visit(node)


class _GenerateResultsExtractor(ast.NodeVisitor):
    """Read-only visitor that extracts df_variable from GENERATE_RESULTS (test mode)."""

    def __init__(self):
        self.found = False
        self.df_variable = None

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "GENERATE_RESULTS":
            self.found = True
            if len(node.args) >= 1 and isinstance(node.args[0], ast.Name):
                self.df_variable = node.args[0].id
            else:
                raise ValueError(
                    "GENERATE_RESULTS argument must be a variable name."
                )
        self.generic_visit(node)

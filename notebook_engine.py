"""
Notebook Engine - Phase 3 / Bet 4 slice 2
─────────────────────────────────────────
Cell-stream execution engine for ``.spqnb`` notebooks. Slice 2 ships
top-to-bottom execution with cell-type dispatch and a shared per-run
namespace. Reactive caching layers on top in slice 3 (this engine
just runs cells in order).

Cell type → executor mapping
─────────────────────────────

* **``spql``** - full SPQL query routed through
  :func:`query_engine.CmdExecutionBackend.process_query_with_diagnostics`.
  Output is the resulting :class:`pandas.DataFrame`; cell's id gets
  bound in the shared namespace so downstream cells can reference it.

* **``pipe``** - same execution path as ``spql``. Distinguished only
  for UI rendering (LLM-aware affordances arrive in slice 5+).

* **``python``** - full Python via :func:`exec` in the shared
  namespace. **NOT RestrictedPython.** Per user direction 2026-05-08
  this is an admin tool; the audience is VS-Code-class developers on a
  trusted-local machine. Standard ``__builtins__`` are available; any
  variables the cell assigns become available to subsequent cells.
  IPython-style: if the cell's last statement is an expression, its
  value is captured as the cell's "output" (analogous to a Jupyter
  cell's display). ``stdout`` / ``stderr`` are captured per cell.

* **``markdown``** - pass-through. ``source`` IS the output. No
  namespace exposure (markdown is documentation, not data).

* **``chart``** - pass-through. ``source`` IS the spec (Vega-Lite by
  default; finalised in slice 7). Renderer is slice 7.

* **``param``** - YAML-parses the source as a parameter spec; exposes
  the ``default`` value at ``namespace[cell.id]``. Form rendering +
  user-supplied values arrive in slice 5/6.

Namespace contract
──────────────────

A notebook execution holds ONE shared namespace dict for the duration
of the run. After each cell, the engine binds the cell's output at
``namespace[cell.id]`` (where applicable - markdown / chart cells
don't bind). Python cells can also assign arbitrary names to the
namespace via standard assignment statements; those become available
to all subsequent cells. Re-running the notebook starts with a fresh
namespace by default; callers can opt in to a persistent namespace by
passing ``namespace=``.

Error handling
──────────────

A failure in one cell does NOT stop subsequent cells from running.
The errored cell gets ``status="error"``, a stack-class + message,
and is omitted from the namespace; subsequent cells that reference
the missing name will themselves fail naturally with ``NameError``.
This mirrors Jupyter's behaviour and lets the operator see the full
chain of failures in one run rather than fixing them one at a time.

Slice 2 deliberately does NOT:
  * Persist execution state back to the YAML (slice 3's reactive cache
    handles that - ``_last_input_hash`` / ``_last_output_hash`` etc.).
  * Enforce per-cell timeouts or memory caps (admin tool; user is
    expected to ctrl-C runaway cells from the UI in slice 4+).
  * Pre-import any libraries (callers do their own ``import pandas as pd``).
"""

from __future__ import annotations

import ast
import io
import logging
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


# ── Public status / cell-type constants ────────────────────────────

STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

# Per-cell capture caps. These are belt-and-braces protections against
# a Python cell printing megabytes to stdout - UI display + audit
# storage assume bounded results.
_MAX_STDOUT_CHARS = 10000
_MAX_STDERR_CHARS = 10000
_MAX_REPR_CHARS = 1000
_MAX_ERROR_MESSAGE_CHARS = 2000


# ── Result dataclasses ─────────────────────────────────────────────

@dataclass
class CellResult:
    """Result of executing a single cell. Dual-audience contract:
    every renderable field has BOTH a human-skim form and a structured
    machine-readable form so AI agents can reason about cell outputs
    without HTML scraping (per
    ``feedback_dual_audience_ai_and_human``, 2026-05-09).

    Field overview:

    * ``output`` - the raw produced value (DataFrame / str / param value).
      Heavy; not in ``to_dict()``.
    * ``output_repr`` - short human-skim string.
    * ``output_preview`` *(slice 5, dual-audience)* - structured dict for
      DataFrame outputs (spql/pipe). Schema: ``{kind, total_rows,
      total_cols, columns: [{name, dtype}], head_rows: [{...}],
      head_truncated, schema_version}``. The UI renders this as an HTML
      table; an AI agent reads ``columns`` + ``head_rows`` directly.
    * ``output_html`` *(slice 5)* - rendered markdown HTML. AI agents
      get the original markdown via ``output`` (which IS the source);
      humans get the rendered HTML for the SPA.
    * ``param_spec`` *(slice 5)* - parsed YAML param spec dict for param
      cells (``{type, default, options?, label?, ...}``). The UI uses
      ``type`` to pick form input; AI agents introspect ``options`` /
      ``default`` directly.
    * ``stdout`` / ``stderr`` - captured per cell (10K cap each).
    * ``error_class`` / ``error_message`` - structured error fields.
    * ``runtime_ms`` / ``executed_at`` / ``exposed_names`` - telemetry.
    * ``cache_hit`` - slice-3 reactive cache flag.
    """
    cell_id: str
    cell_type: str
    status: str
    output: Any = None
    output_repr: str = ""
    output_preview: Optional[dict] = None
    output_html: str = ""
    param_spec: Optional[dict] = None
    stdout: str = ""
    stderr: str = ""
    error_class: str = ""
    error_message: str = ""
    runtime_ms: int = 0
    executed_at: str = ""
    exposed_names: list[str] = field(default_factory=list)
    cache_hit: bool = False

    def to_dict(self) -> dict:
        """Serialisable dict for the API response. Drops the heavy
        ``output`` payload (often a DataFrame); the structured
        ``output_preview`` carries enough for UI rendering AND AI
        consumption.
        """
        return {
            "cell_id": self.cell_id,
            "cell_type": self.cell_type,
            "status": self.status,
            "output_repr": self.output_repr,
            "output_preview": self.output_preview,
            "output_html": self.output_html,
            "param_spec": self.param_spec,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error_class": self.error_class,
            "error_message": self.error_message,
            "runtime_ms": self.runtime_ms,
            "executed_at": self.executed_at,
            "exposed_names": list(self.exposed_names),
            "cache_hit": self.cache_hit,
        }


@dataclass
class NotebookRunResult:
    """Aggregated result of a full top-to-bottom notebook run."""
    notebook_id: str
    cells: list[CellResult] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    total_runtime_ms: int = 0
    success_count: int = 0
    error_count: int = 0
    skipped_count: int = 0
    cache_hits: int = 0   # slice-3: how many cells served from cache

    def to_dict(self) -> dict:
        return {
            "notebook_id": self.notebook_id,
            "cells": [c.to_dict() for c in self.cells],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_runtime_ms": self.total_runtime_ms,
            "success_count": self.success_count,
            "error_count": self.error_count,
            "skipped_count": self.skipped_count,
            "cache_hits": self.cache_hits,
        }


# ── Internal helpers ───────────────────────────────────────────────

def _now_iso() -> str:
    """Return the current time as a tz-aware ISO 8601 string with
    explicit offset. Mirrors the alert-group / saved-search convention
    so JS ``new Date(iso)`` parses unambiguously.
    """
    return datetime.now(timezone.utc).astimezone().isoformat()


def _truncate(s: str, limit: int) -> str:
    if not s:
        return ""
    if len(s) <= limit:
        return s
    return s[:limit] + f"…(+{len(s) - limit} chars truncated)"


# ── Slice 5: rich-rendering helpers ────────────────────────────────

# How many head rows to include in the structured preview. Bounded so
# even a 1M-row DataFrame produces a small JSON-serialisable preview.
_PREVIEW_HEAD_ROWS = 10
# Per-cell truncation cap for stringified cell values in the head_rows
# preview. Long strings get truncated with an ellipsis suffix; full
# values stay on the underlying DataFrame.
_PREVIEW_VALUE_CHARS = 200


def _coerce_cell_value_for_preview(v) -> Any:
    """Convert a single DataFrame cell value into something JSON-safe.

    Pandas / numpy types (numpy.int64, numpy.float64, Timestamp, etc.)
    don't pass ``isinstance(v, int)`` checks but ARE convertible to
    Python primitives via the ``.item()`` method or ``int()`` /
    ``float()``. Strings get truncated past the cap; other complex
    objects (lists, dicts, custom classes) get ``str()``-ified.
    """
    import pandas as _pd
    import numpy as _np
    if v is None:
        return None
    try:
        if _pd.isna(v):
            return None
    except (TypeError, ValueError):
        # Some types raise inside pd.isna (e.g. lists). Fall through.
        pass
    # Numpy scalars expose .item() to produce a Python primitive.
    if isinstance(v, _np.generic):
        try:
            v = v.item()
        except Exception:
            v = str(v)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        if len(v) > _PREVIEW_VALUE_CHARS:
            return (
                v[:_PREVIEW_VALUE_CHARS]
                + f"…(+{len(v) - _PREVIEW_VALUE_CHARS} chars)"
            )
        return v
    # Timestamps, lists, dicts, custom objects - coerce to str.
    s = str(v)
    if len(s) > _PREVIEW_VALUE_CHARS:
        s = (
            s[:_PREVIEW_VALUE_CHARS]
            + f"…(+{len(s) - _PREVIEW_VALUE_CHARS} chars)"
        )
    return s


def _build_dataframe_preview(df) -> Optional[dict]:
    """Build a dual-audience preview for a DataFrame output.

    Returns a JSON-serialisable dict the UI can render as an HTML
    table AND an AI agent can introspect for schema/sample data
    without scraping HTML. Schema version is pinned so future
    additions stay additive (per ``reference_forward_declare_future_slice_fields``).
    """
    if df is None or not hasattr(df, "columns"):
        return None
    total_rows = int(len(df.index))
    total_cols = int(len(df.columns))
    columns = [
        {"name": str(c), "dtype": str(df[c].dtype)}
        for c in df.columns
    ]
    head_n = min(total_rows, _PREVIEW_HEAD_ROWS)
    head_rows = []
    if head_n > 0:
        head_df = df.head(head_n)
        for _idx, row in head_df.iterrows():
            row_dict = {
                str(c): _coerce_cell_value_for_preview(row[c])
                for c in df.columns
            }
            head_rows.append(row_dict)
    return {
        "schema_version": 1,
        "kind": "dataframe",
        "total_rows": total_rows,
        "total_cols": total_cols,
        "columns": columns,
        "head_rows": head_rows,
        "head_truncated": total_rows > head_n,
    }


def _render_markdown_html(source: str) -> str:
    """Render markdown source to HTML.

    Uses the ``markdown`` library if available; falls back to a
    ``<pre>``-wrapped escaped source if the library isn't installed.
    The fallback keeps the page functional during the
    add-dependency-then-rebuild deploy window. The structured
    audience (AI agents) can always read the original ``source``
    directly - this function exists for the human renderer only.
    """
    if not source:
        return ""
    try:
        import markdown as _md  # noqa: F401 - optional
    except ImportError:
        # Graceful fallback: HTML-escape + wrap in <pre>. The page
        # stays functional; full markdown rendering arrives after
        # `pip install -r requirements.txt`.
        escaped = (
            source.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return f"<pre class=\"nb-markdown-fallback\">{escaped}</pre>"
    try:
        # Standard extensions the operator is likely to use:
        # - fenced_code: ```block``` syntax
        # - tables: pipe tables
        # - sane_lists: paragraph-aware lists
        return _md.markdown(
            source,
            extensions=["fenced_code", "tables", "sane_lists"],
            output_format="html5",
        )
    except Exception as exc:
        logger.warning(
            "[!] notebook_engine: markdown render failed (%s); "
            "falling back to plain pre-tag", exc,
        )
        escaped = (
            source.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return f"<pre class=\"nb-markdown-fallback\">{escaped}</pre>"


# ── Engine ─────────────────────────────────────────────────────────

class NotebookEngine:
    """Cell-stream execution engine.

    Stateless across notebooks - each call to :meth:`execute_notebook`
    starts with a fresh namespace (or a caller-supplied one). The
    engine itself can be reused; per-notebook state lives in the
    namespace + result.
    """

    # ------------------------------------------------------------------
    # Per-cell dispatch
    # ------------------------------------------------------------------

    def execute_cell(
        self, cell: dict, namespace: dict,
        *, notebook: Optional[dict] = None,
    ) -> CellResult:
        """Execute a single cell against a shared namespace.

        The namespace is mutated in-place: the cell's output (where
        applicable) is bound at ``namespace[cell.id]``, and any
        Python-cell assignments add to the dict. Returns a
        :class:`CellResult` with status + telemetry; never raises (any
        exception inside the cell is caught and reported via
        ``status="error"``).

        ``notebook`` (optional) is the full enclosing notebook record.
        Required ONLY for ``promote_to_alert_group`` cells, which need
        cross-cell context to resolve their ``prompt_cell`` reference.
        Other cell types ignore it. The engine's whole-notebook path
        always supplies it; ad-hoc per-cell callers may omit it (the
        promote handler will fail with a clear error if a notebook is
        needed but absent).
        """
        cell_type = cell.get("type", "")
        cell_id = cell.get("id", "<unknown>")
        started = time.monotonic()
        executed_at = _now_iso()

        try:
            if cell_type in ("spql", "pipe"):
                return self._execute_spql_like(
                    cell, namespace, cell_id, cell_type,
                    started, executed_at,
                )
            if cell_type == "python":
                return self._execute_python(
                    cell, namespace, cell_id, started, executed_at,
                )
            if cell_type == "markdown":
                return self._execute_passthrough(
                    cell, cell_id, "markdown", started, executed_at,
                )
            if cell_type == "chart":
                return self._execute_passthrough(
                    cell, cell_id, "chart", started, executed_at,
                )
            if cell_type == "param":
                return self._execute_param(
                    cell, namespace, cell_id, started, executed_at,
                )
            if cell_type == "promote_to_alert_group":
                return self._execute_promote_to_alert_group(
                    cell, cell_id, notebook, started, executed_at,
                )
            # Defense-in-depth - schema validation should already have
            # rejected unknown types, but if a malformed YAML slipped
            # through, surface a clear error rather than crashing.
            runtime_ms = int((time.monotonic() - started) * 1000)
            return CellResult(
                cell_id=cell_id, cell_type=str(cell_type),
                status=STATUS_ERROR,
                error_class="UnknownCellType",
                error_message=f"Unknown cell type: {cell_type!r}",
                runtime_ms=runtime_ms, executed_at=executed_at,
            )
        except Exception as exc:
            # Last-resort catch - any executor's own machinery raising
            # (NOT the user's cell code, which is caught lower) lands here.
            runtime_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                "[!] notebook_engine: dispatch failed for cell %s (%s): %s",
                cell_id, cell_type, exc,
            )
            return CellResult(
                cell_id=cell_id, cell_type=str(cell_type),
                status=STATUS_ERROR,
                error_class=type(exc).__name__,
                error_message=_truncate(str(exc), _MAX_ERROR_MESSAGE_CHARS),
                runtime_ms=runtime_ms, executed_at=executed_at,
            )

    # ------------------------------------------------------------------
    # Cell-type executors
    # ------------------------------------------------------------------

    def _execute_spql_like(
        self, cell: dict, namespace: dict, cell_id: str, cell_type: str,
        started: float, executed_at: str,
    ) -> CellResult:
        """Execute ``spql`` and ``pipe`` cells - same path."""
        from query_engine.CmdExecutionBackend import (
            process_query_with_diagnostics,
        )
        source = cell.get("source", "")
        df, _job_id, diagnostic = process_query_with_diagnostics(source)
        runtime_ms = int((time.monotonic() - started) * 1000)

        if df is None:
            # Distinguish empty (filter dropped everything) from error
            # using the diagnostic prefix convention from the AG dispatcher.
            if diagnostic and diagnostic.startswith("empty:"):
                error_class = "EmptyResult"
                message = diagnostic
            else:
                error_class = "QueryError"
                if diagnostic and ":" in diagnostic:
                    error_class = diagnostic.split(":", 1)[0].strip()
                message = diagnostic or "Query returned no DataFrame"
            return CellResult(
                cell_id=cell_id, cell_type=cell_type,
                status=STATUS_ERROR,
                error_class=error_class,
                error_message=_truncate(message, _MAX_ERROR_MESSAGE_CHARS),
                runtime_ms=runtime_ms, executed_at=executed_at,
            )

        # Success - bind to namespace + return
        namespace[cell_id] = df
        # Slice-5: build the dual-audience preview. Cheap (head N rows
        # + dtype scan); bounded JSON size; works for any DataFrame.
        preview = None
        try:
            preview = _build_dataframe_preview(df)
        except Exception as exc:
            logger.warning(
                "[!] notebook_engine: preview build failed for %s/%s: %s",
                cell_type, cell_id, exc,
            )
        return CellResult(
            cell_id=cell_id, cell_type=cell_type,
            status=STATUS_SUCCESS,
            output=df,
            output_repr=(
                f"DataFrame ({len(df)} rows × {len(df.columns)} cols)"
            ),
            output_preview=preview,
            runtime_ms=runtime_ms, executed_at=executed_at,
            exposed_names=[cell_id],
        )

    def _execute_python(
        self, cell: dict, namespace: dict, cell_id: str,
        started: float, executed_at: str,
    ) -> CellResult:
        """Execute a ``python`` cell via :func:`exec`.

        Uses IPython-style last-expression capture: if the cell's
        final statement is an expression, evaluate it separately and
        return its value as ``output``. Otherwise ``output`` is None.
        Stdout / stderr are captured per cell.

        Full Python - no RestrictedPython. Per user direction
        2026-05-08, the ingestion-script sandbox is the ONLY place
        RestrictedPython is appropriate; admin tools default to full
        Python (see
        ``feedback_no_restricted_python_outside_ingestion`` memory).
        """
        source = cell.get("source", "")
        prior_keys = set(namespace.keys())
        result_value = None
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        try:
            tree = ast.parse(source, filename=f"<cell:{cell_id}>", mode="exec")
        except SyntaxError as exc:
            runtime_ms = int((time.monotonic() - started) * 1000)
            return CellResult(
                cell_id=cell_id, cell_type="python",
                status=STATUS_ERROR,
                error_class="SyntaxError",
                error_message=_truncate(
                    f"line {exc.lineno}: {exc.msg}",
                    _MAX_ERROR_MESSAGE_CHARS,
                ),
                runtime_ms=runtime_ms, executed_at=executed_at,
            )

        # IPython-style: if the last statement is an expression, split
        # it off and eval it separately so we can capture its value.
        eval_code = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            exec_module = ast.Module(body=tree.body[:-1], type_ignores=[])
            eval_expression = ast.Expression(body=tree.body[-1].value)
            try:
                exec_code = compile(
                    exec_module, f"<cell:{cell_id}>", "exec",
                )
                eval_code = compile(
                    eval_expression, f"<cell:{cell_id}>", "eval",
                )
            except SyntaxError as exc:
                # Should not happen - ast.parse already succeeded - but
                # defense in depth.
                runtime_ms = int((time.monotonic() - started) * 1000)
                return CellResult(
                    cell_id=cell_id, cell_type="python",
                    status=STATUS_ERROR,
                    error_class="CompileError",
                    error_message=_truncate(str(exc), _MAX_ERROR_MESSAGE_CHARS),
                    runtime_ms=runtime_ms, executed_at=executed_at,
                )
        else:
            try:
                exec_code = compile(tree, f"<cell:{cell_id}>", "exec")
            except SyntaxError as exc:
                runtime_ms = int((time.monotonic() - started) * 1000)
                return CellResult(
                    cell_id=cell_id, cell_type="python",
                    status=STATUS_ERROR,
                    error_class="CompileError",
                    error_message=_truncate(str(exc), _MAX_ERROR_MESSAGE_CHARS),
                    runtime_ms=runtime_ms, executed_at=executed_at,
                )

        # Run with stdout / stderr captured. Full Python; no sandbox.
        # The cell can `import os; os.system(...)` - that's intentional
        # for the admin-tool threat model.
        try:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                exec(exec_code, namespace)  # nosec B102 - admin tool, full Python by design
                if eval_code is not None:
                    result_value = eval(eval_code, namespace)  # nosec B307
        except Exception as exc:
            runtime_ms = int((time.monotonic() - started) * 1000)
            return CellResult(
                cell_id=cell_id, cell_type="python",
                status=STATUS_ERROR,
                error_class=type(exc).__name__,
                error_message=_truncate(str(exc), _MAX_ERROR_MESSAGE_CHARS),
                runtime_ms=runtime_ms, executed_at=executed_at,
                stdout=_truncate(stdout_buf.getvalue(), _MAX_STDOUT_CHARS),
                stderr=_truncate(stderr_buf.getvalue(), _MAX_STDERR_CHARS),
            )

        runtime_ms = int((time.monotonic() - started) * 1000)
        # Python's exec injects `__builtins__` if absent - exclude that
        # from the "newly-defined names" view so it doesn't appear as a
        # spurious assignment from the cell.
        new_keys = sorted(
            set(namespace.keys()) - prior_keys - {"__builtins__"}
        )

        # Slice-6: if the last-expression value is a DataFrame, build
        # a structured preview so the SPA renders it as a table -
        # same pattern as spql/pipe cells. If there's no terminal
        # expression but the cell bound a DataFrame to a name (e.g.
        # ``df = pd.read_csv(...)``), pick the LAST such binding for
        # the preview. Picking the last matches Jupyter convention
        # (the most recent assignment is the "result"); skipping
        # module-typed bindings prevents ``import pandas as pd`` from
        # clobbering the DataFrame ``df`` declared on the next line.
        df_for_preview = None
        try:
            import pandas as _pd
            import types as _types
            if isinstance(result_value, _pd.DataFrame):
                df_for_preview = result_value
            elif result_value is None and new_keys:
                for name in reversed(new_keys):
                    candidate = namespace.get(name)
                    if isinstance(candidate, _types.ModuleType):
                        continue
                    if isinstance(candidate, _pd.DataFrame):
                        df_for_preview = candidate
                        break
        except ImportError:
            pass  # pandas not available; nothing to preview
        output_preview = None
        if df_for_preview is not None:
            try:
                output_preview = _build_dataframe_preview(df_for_preview)
            except Exception as exc:
                logger.warning(
                    "[!] notebook_engine: python DataFrame preview build "
                    "failed for %s: %s", cell_id, exc,
                )

        # output_repr - prefer the last-expression value (Jupyter style),
        # else summarise the new bindings, else empty.
        if df_for_preview is not None:
            output_repr = (
                f"DataFrame ({len(df_for_preview)} rows × "
                f"{len(df_for_preview.columns)} cols)"
            )
        elif result_value is not None:
            try:
                output_repr = _truncate(repr(result_value), _MAX_REPR_CHARS)
            except Exception:
                # repr() can raise for pathological objects; fall back
                # to a stable description.
                output_repr = (
                    f"<{type(result_value).__name__} (repr raised)>"
                )
        elif new_keys:
            output_repr = f"defined: {', '.join(new_keys)}"
        else:
            output_repr = ""

        return CellResult(
            cell_id=cell_id, cell_type="python",
            status=STATUS_SUCCESS,
            output=result_value,
            output_repr=output_repr,
            output_preview=output_preview,
            runtime_ms=runtime_ms, executed_at=executed_at,
            stdout=_truncate(stdout_buf.getvalue(), _MAX_STDOUT_CHARS),
            stderr=_truncate(stderr_buf.getvalue(), _MAX_STDERR_CHARS),
            exposed_names=new_keys,
        )

    def _execute_passthrough(
        self, cell: dict, cell_id: str, cell_type: str,
        started: float, executed_at: str,
    ) -> CellResult:
        """Markdown + chart cells: the source IS the output. No
        namespace exposure.

        Slice-5 enrichment: markdown cells get their source rendered
        through the markdown library and exposed as ``output_html``
        for the human-facing renderer. The raw ``source`` is preserved
        on the cell record so AI agents can introspect / refactor the
        markdown without re-parsing the HTML.

        Chart cells stay passthrough - the spec is the spec; rendering
        is the SPA's job (Vega-Lite / matplotlib in slice 6).
        """
        source = cell.get("source", "")
        output_html = ""
        if cell_type == "markdown":
            output_html = _render_markdown_html(source)
        runtime_ms = int((time.monotonic() - started) * 1000)
        return CellResult(
            cell_id=cell_id, cell_type=cell_type,
            status=STATUS_SUCCESS,
            output=source,
            output_repr=f"{cell_type} ({len(source)} chars)",
            output_html=output_html,
            runtime_ms=runtime_ms, executed_at=executed_at,
        )

    def _execute_param(
        self, cell: dict, namespace: dict, cell_id: str,
        started: float, executed_at: str,
    ) -> CellResult:
        """Param cells: YAML-parse the source as a parameter spec;
        bind ``namespace[cell_id]`` to the spec's ``default`` value
        (or ``None`` if no default supplied).

        Slice 2 just persists + exposes the default. Slice 5/6 wires
        the form rendering and lets the operator override the default
        with a user-supplied value at run time.
        """
        source = cell.get("source", "")
        try:
            spec = yaml.safe_load(source) or {}
        except yaml.YAMLError as exc:
            runtime_ms = int((time.monotonic() - started) * 1000)
            return CellResult(
                cell_id=cell_id, cell_type="param",
                status=STATUS_ERROR,
                error_class="YAMLParseError",
                error_message=_truncate(str(exc), _MAX_ERROR_MESSAGE_CHARS),
                runtime_ms=runtime_ms, executed_at=executed_at,
            )
        if not isinstance(spec, dict):
            runtime_ms = int((time.monotonic() - started) * 1000)
            return CellResult(
                cell_id=cell_id, cell_type="param",
                status=STATUS_ERROR,
                error_class="InvalidParamSpec",
                error_message=(
                    f"Param spec must be a YAML mapping, got "
                    f"{type(spec).__name__}."
                ),
                runtime_ms=runtime_ms, executed_at=executed_at,
            )

        # Slice-5 dual-audience: the parsed spec ITSELF is the AI-side
        # contract (an AI agent reads spec.type / spec.options /
        # spec.default to reason about the parameter). The UI renders
        # a form input keyed off spec.type. Both consume the same dict.
        # If the operator (or an AI agent) seeded namespace[cell_id]
        # before the run (e.g. via the slice-4 execute namespace= seed),
        # we honour that override - that's how form-supplied values
        # flow back into the run.
        if cell_id in namespace:
            value = namespace[cell_id]
        else:
            value = spec.get("default")
            namespace[cell_id] = value
        runtime_ms = int((time.monotonic() - started) * 1000)
        return CellResult(
            cell_id=cell_id, cell_type="param",
            status=STATUS_SUCCESS,
            output=value,
            output_repr=(
                f"param[{spec.get('type', 'value')}] = {value!r}"
            ),
            param_spec=spec,
            runtime_ms=runtime_ms, executed_at=executed_at,
            exposed_names=[cell_id],
        )

    def _execute_promote_to_alert_group(
        self, cell: dict, cell_id: str, notebook: Optional[dict],
        started: float, executed_at: str,
    ) -> CellResult:
        """Execute a ``promote_to_alert_group`` cell.

        ALWAYS DRY-RUN. The handler builds a structured preview via
        :func:`notebook_to_alert_group.build_promote_preview` and
        returns it as ``output_preview``. It NEVER calls
        ``AlertGroupStore.save_group`` / ``update_group`` from this
        path - that's the explicit
        ``POST /api/notebooks/<id>/promote/<cell_id>`` endpoint's job.

        This is the **config-leak canary** boundary: the
        ``test_engine_path_does_not_invoke_save_group`` drift guard
        patches both AG-mutating methods to raise
        ``AssertionError("CONFIG LEAK")`` and asserts neither fires
        when a notebook with a promote cell runs. Same shape as the
        slice-7 ``| llm`` money-leak canary.
        """
        if notebook is None:
            runtime_ms = int((time.monotonic() - started) * 1000)
            return CellResult(
                cell_id=cell_id, cell_type="promote_to_alert_group",
                status=STATUS_ERROR,
                error_class="MissingNotebookContext",
                error_message=(
                    "promote_to_alert_group cells require the enclosing "
                    "notebook record to resolve prompt_cell. Call "
                    "execute_cell(..., notebook=nb) or use the "
                    "execute_notebook entry point."
                ),
                runtime_ms=runtime_ms, executed_at=executed_at,
            )

        from notebook_to_alert_group import build_promote_preview
        try:
            preview = build_promote_preview(notebook, cell_id)
        except Exception as exc:
            # build_promote_preview is supposed to surface bad config as
            # a structured "blocked" decision rather than raise, but if
            # it does raise (e.g. AG store import failure) we surface a
            # cell-level error instead of crashing the whole notebook.
            runtime_ms = int((time.monotonic() - started) * 1000)
            return CellResult(
                cell_id=cell_id, cell_type="promote_to_alert_group",
                status=STATUS_ERROR,
                error_class=type(exc).__name__,
                error_message=_truncate(str(exc), _MAX_ERROR_MESSAGE_CHARS),
                runtime_ms=runtime_ms, executed_at=executed_at,
            )

        runtime_ms = int((time.monotonic() - started) * 1000)
        decision = preview.get("decision", "blocked")
        target = preview.get("target_payload") or {}
        ag_name = target.get("name") or "<unknown>"

        if decision == "create":
            output_repr = f"would CREATE alert group {ag_name!r}"
        elif decision == "update":
            n = len(preview.get("changed_fields") or [])
            output_repr = (
                f"would UPDATE alert group {ag_name!r} ({n} field"
                f"{'' if n == 1 else 's'} changed)"
            )
        elif decision == "no_change":
            output_repr = f"alert group {ag_name!r} already up to date"
        else:
            errs = preview.get("validation", {}).get("errors") or []
            first = errs[0] if errs else "unknown"
            output_repr = f"BLOCKED: {first}"

        # Status is SUCCESS even when blocked - the cell ran, the
        # preview was produced; "blocked" is a state of the WOULD-be
        # AG, not a failure of cell execution. The SPA + AI consumers
        # branch on ``output_preview.decision`` to see what's next.
        return CellResult(
            cell_id=cell_id, cell_type="promote_to_alert_group",
            status=STATUS_SUCCESS,
            output=preview,
            output_repr=output_repr,
            output_preview=preview,
            runtime_ms=runtime_ms, executed_at=executed_at,
        )

    # ------------------------------------------------------------------
    # Whole-notebook execution
    # ------------------------------------------------------------------

    def execute_notebook(
        self, notebook: dict, *,
        namespace: Optional[dict] = None,
        use_cache: bool = True,
        cache_store: Optional[Any] = None,
        stop_at_cell_id: Optional[str] = None,
    ) -> NotebookRunResult:
        """Execute every cell in order against a shared namespace.

        ``namespace`` defaults to a fresh empty dict; callers can pass
        their own to seed initial values.

        Slice-3 cache (``use_cache=True``, default): each cell is
        keyed by ``content_hash = SHA-256(type + source +
        prior_output_hashes)``. On cache hit, the cell's
        ``namespace_delta`` is restored (and ``output`` returned) WITHOUT
        re-executing - the cell's downstream consumers see the cached
        upstream state, and the cell-result's ``cache_hit=True`` flag
        is set. On cache miss, the cell executes normally + the result
        is written to the cache for future runs.

        Pass ``use_cache=False`` to force every cell to re-execute
        AND disable cache writes (full slow-path execution).
        ``cache_store=None`` (default) resolves to the process-wide
        singleton; pass an explicit store to override.

        Slice-6 per-cell run: pass ``stop_at_cell_id="cell_n"`` to run
        cells ``[0..n]`` only (where ``n`` is the index of the cell with
        that id). Upstream cells normally hit cache (cheap); the target
        cell is the focus of the run. Cells beyond the target are
        SKIPPED - not in the result. Used by the SPA's per-cell ▶ Run
        button so operators can iterate on cell N without paying for
        cells N+1..end. Raises ``LookupError`` if ``stop_at_cell_id``
        doesn't match any cell in the notebook (caller should validate).

        A failure in one cell does NOT stop subsequent cells. Errored
        cells are NOT cached. Downstream cells that reference the
        missing name fail naturally with NameError (those failures are
        also not cached - re-running with a fixed upstream cell
        re-executes downstreams normally).
        """
        notebook_id = notebook.get("id", "<unknown>")
        if namespace is None:
            namespace = {}
        result = NotebookRunResult(notebook_id=notebook_id)
        result.started_at = _now_iso()
        run_started = time.monotonic()

        # Resolve the cache store: explicit > singleton > disabled.
        active_cache = None
        if cache_store is not None:
            active_cache = cache_store
        elif use_cache:
            try:
                from notebook_cache_store import get_store as _get_cache
                active_cache = _get_cache()
            except Exception as exc:
                logger.warning(
                    "[!] notebook_cache: lazy-load failed (%s); cache disabled "
                    "for this run", exc,
                )
                active_cache = None

        # Slice 6: optional stop-at-cell. Slice the cell list before
        # iterating so prior_output_hashes still propagate correctly
        # for the cells we DO run. Cells past stop_at_cell_id are
        # silently skipped (not added to the result) - the operator
        # iterating on cell N doesn't want N+1..end either re-run or
        # surfaced as untouched.
        all_cells = notebook.get("cells", []) or []
        if stop_at_cell_id is not None:
            stop_idx = None
            for i, cell in enumerate(all_cells):
                if cell.get("id") == stop_at_cell_id:
                    stop_idx = i
                    break
            if stop_idx is None:
                raise LookupError(
                    f"stop_at_cell_id={stop_at_cell_id!r} not found in "
                    f"notebook {notebook_id!r}"
                )
            cells_to_run = all_cells[: stop_idx + 1]
        else:
            cells_to_run = all_cells

        # DAG hash propagation: each cell's content_hash includes the
        # output_hashes of every prior cell. Edits propagate downstream
        # naturally - see ``compute_content_hash`` for the contract.
        prior_output_hashes: list[str] = []

        for cell in cells_to_run:
            cell_result, output_hash = self._execute_cell_with_cache(
                cell, namespace, prior_output_hashes,
                active_cache, use_cache, notebook_id,
                notebook=notebook,
            )
            result.cells.append(cell_result)
            prior_output_hashes.append(output_hash)
            if cell_result.status == STATUS_SUCCESS:
                result.success_count += 1
                if cell_result.cache_hit:
                    result.cache_hits += 1
            elif cell_result.status == STATUS_ERROR:
                result.error_count += 1
            else:
                result.skipped_count += 1

        result.finished_at = _now_iso()
        result.total_runtime_ms = int((time.monotonic() - run_started) * 1000)
        logger.info(
            "[i] Notebook %s executed: success=%d errors=%d skipped=%d "
            "cache_hits=%d total_ms=%d (cells_run=%d)",
            notebook_id, result.success_count, result.error_count,
            result.skipped_count, result.cache_hits, result.total_runtime_ms,
            len(cells_to_run),
        )
        return result

    def _execute_cell_with_cache(
        self, cell: dict, namespace: dict,
        prior_output_hashes: list[str],
        cache_store: Optional[Any], use_cache: bool,
        notebook_id: str,
        *, notebook: Optional[dict] = None,
    ) -> tuple[CellResult, str]:
        """Execute one cell with cache lookup + write-back wrapping.

        Returns ``(CellResult, output_hash)``. The output_hash is what
        gets propagated to downstream cells' content_hash computations
        - empty string for errored / skipped cells (so downstream
        invalidation is forced when an upstream re-runs).
        """
        from notebook_cache_store import (
            compute_content_hash, compute_output_hash,
        )

        content_hash = compute_content_hash(cell, prior_output_hashes)
        cell_id = cell.get("id", "<unknown>")
        cell_type = cell.get("type", "")

        # ── promote_to_alert_group cells bypass the cache (slice 9) ──
        # The cell's preview embeds the CURRENT AG state (for diff
        # rendering) and live feeder pre-flight. Caching the preview
        # by content_hash would serve a stale "no_change" decision
        # after the operator edits the AG outside the notebook (via
        # the AGs page) - exactly the failure mode that erodes the
        # operator's trust in the dev → prod loop. Re-execute every
        # time - cheap (read AG YAML + saved-search YAMLs). The cell
        # exposes nothing to the namespace, so downstream cells aren't
        # affected by the bypass.
        if cell_type == "promote_to_alert_group":
            cell_result = self.execute_cell(
                cell, namespace, notebook=notebook,
            )
            # Empty output_hash: no downstream cells depend on this
            # cell's output (it doesn't bind to namespace), so there's
            # nothing for prior_output_hashes propagation to chain.
            return cell_result, ""

        # ── Param cells bypass the cache (slice 5) ────────────────
        # Param-cell output depends on the runtime namespace override
        # supplied via execute_notebook(namespace=...). Caching by
        # content_hash alone would serve a stale value when the
        # operator changes the form input. Re-execute every time -
        # cheap (parse YAML + dict lookup). Downstream cells still get
        # an output_hash for their content_hash propagation, so their
        # caches stay correct (different override → different param
        # output_hash → downstream cache miss → re-execute).
        if cell_type == "param":
            cell_result = self.execute_cell(cell, namespace)
            if cell_result.status == STATUS_SUCCESS:
                namespace_delta = {
                    name: namespace[name]
                    for name in cell_result.exposed_names
                    if name in namespace
                }
                output_hash_payload = {
                    "namespace_delta": namespace_delta,
                    "output": cell_result.output,
                    "output_repr": cell_result.output_repr,
                    "stdout": cell_result.stdout,
                    "stderr": cell_result.stderr,
                    "exposed_names": list(cell_result.exposed_names),
                    "output_preview": cell_result.output_preview,
                    "output_html": cell_result.output_html,
                    "param_spec": cell_result.param_spec,
                }
                try:
                    output_hash = compute_output_hash(output_hash_payload)
                except Exception:
                    output_hash = content_hash
                return cell_result, output_hash
            return cell_result, ""

        # ── Cache lookup ───────────────────────────────────────────
        if use_cache and cache_store is not None:
            try:
                cached = cache_store.get(content_hash)
            except Exception as exc:
                logger.warning(
                    "[!] notebook_cache: get(%s) failed: %s - falling through "
                    "to live execution", content_hash[:12], exc,
                )
                cached = None
            if cached is not None:
                # Restore namespace delta - every name the original
                # execution exposed is rebound on the shared namespace.
                for name, value in cached.namespace_delta.items():
                    namespace[name] = value
                # Slice-5 rich-rendering fields: the cache may have
                # them (newly-cached entries) or not (entries cached
                # before slice 5 ship). Default to None / "" so old
                # cache entries stay valid; new entries surface the
                # full structured output.
                cached_payload = cached.namespace_delta.get(
                    "__nb_slice5_payload__", {}
                ) if isinstance(cached.namespace_delta, dict) else {}
                # Extract slice-5 fields from cached.* if present,
                # else use safe defaults. The cache_store reconstructs
                # CachedEntry from the pickle payload; the payload
                # carries the same keys we wrote on slice-3 + the new
                # slice-5 keys. See ``_payload_for_cache`` below.
                output_preview = getattr(cached, "output_preview", None)
                output_html = getattr(cached, "output_html", "") or ""
                param_spec = getattr(cached, "param_spec", None)
                cell_result = CellResult(
                    cell_id=cell_id,
                    cell_type=cell_type,
                    status=STATUS_SUCCESS,
                    output=cached.output,
                    output_repr=cached.output_repr,
                    output_preview=output_preview,
                    output_html=output_html,
                    param_spec=param_spec,
                    stdout=cached.stdout,
                    stderr=cached.stderr,
                    runtime_ms=0,
                    executed_at=_now_iso(),
                    exposed_names=list(cached.exposed_names),
                    cache_hit=True,
                )
                logger.info(
                    "[i] notebook_cache HIT for %s/%s (content_hash=%s...)",
                    notebook_id, cell_id, content_hash[:12],
                )
                return cell_result, cached.output_hash

        # ── Cache miss → live execution ────────────────────────────
        cell_result = self.execute_cell(cell, namespace, notebook=notebook)

        # Only successful cells get cached. Errors are intentionally
        # not memoised - re-running with a fixed upstream re-attempts
        # downstream cells.
        if (
            cell_result.status == STATUS_SUCCESS
            and use_cache
            and cache_store is not None
        ):
            # Slice-6 fix: filter module bindings out of the cached
            # namespace_delta. Modules can't be pickled (cannot
            # pickle 'module' object); without this filter, ANY
            # Python cell that does `import x` failed to cache -
            # the operator silently lost iteration economics for
            # the most common Jupyter pattern. Trade-off documented:
            # cache hits do NOT restore module bindings; downstream
            # cells that need a module should import it themselves
            # (the convention in Jupyter / Marimo too). sys.modules
            # makes repeat imports cheap (~µs).
            import types as _types
            namespace_delta = {
                name: namespace[name]
                for name in cell_result.exposed_names
                if name in namespace
                and not isinstance(namespace[name], _types.ModuleType)
            }
            payload = {
                "namespace_delta": namespace_delta,
                "output": cell_result.output,
                "output_repr": cell_result.output_repr,
                "stdout": cell_result.stdout,
                "stderr": cell_result.stderr,
                "exposed_names": list(cell_result.exposed_names),
                # Slice-5 dual-audience fields (additive - old cache
                # entries omit these; new entries write them).
                "output_preview": cell_result.output_preview,
                "output_html": cell_result.output_html,
                "param_spec": cell_result.param_spec,
            }
            try:
                output_hash = compute_output_hash(payload)
                cache_store.put(
                    content_hash=content_hash,
                    output_hash=output_hash,
                    notebook_id=notebook_id,
                    cell_id=cell_id,
                    cell_type=cell_type,
                    payload=payload,
                    runtime_ms=cell_result.runtime_ms,
                    executed_at=cell_result.executed_at,
                )
                return cell_result, output_hash
            except Exception as exc:
                # Cache write failure must never break execution. Log
                # + return a synthetic output_hash derived from the
                # content_hash so downstream cells get a stable
                # propagation value.
                logger.warning(
                    "[!] notebook_cache: put(%s) failed: %s - "
                    "downstream cache validity may be reduced",
                    content_hash[:12], exc,
                )
                return cell_result, content_hash

        # No-cache path (or errored cell): return empty output_hash
        # so downstream cells re-compute their content_hash without
        # any stale state being implied.
        return cell_result, ""


__all__ = [
    "STATUS_SUCCESS",
    "STATUS_ERROR",
    "STATUS_SKIPPED",
    "CellResult",
    "NotebookRunResult",
    "NotebookEngine",
]

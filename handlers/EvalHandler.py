#!/usr/bin/env python3
"""
EvalHandler.py
Purpose: Implements evaluation of SPQL eval commands. This module supports functions
         such as if_/case/tonumber/concat/round and uses a custom evaluation environment
         to support nested and complex expressions.
"""

import logging
import math
import random as _random
import re as _re
import time as _time
import datetime as _dt
import numpy as np
import pandas as pd
import base64
import ast
import operator
from urllib.parse import quote as _url_quote, unquote as _url_unquote

from handlers.MathematicOperations import MathHandler
from handlers.StringHandler import StringHandler
from handlers.GeneralHandler import GeneralHandler
from functionality.datetime_parser import (
    parse_to_epoch as _parse_to_epoch,
    parse_series_to_epoch as _parse_series_to_epoch,
    parse_relative_time as _parse_relative_time,
)

logger = logging.getLogger(__name__)


class EvalHandler:
    def __init__(self):
        self.math_handler = MathHandler()
        self.string_handler = StringHandler()
        self.general_handler = GeneralHandler()

    def run_eval(self, eval_tokens, df):
        """
        Expects eval_tokens in the form:
          ['eval', <assignment>, <assignment>, ... ]
        Each assignment must follow the format: field = expression.
        The expressions are evaluated via safe_eval and the results assigned to new
        columns in df.
        """
        # Empty-DataFrame / None short-circuit - per the
        # ``reference_empty_df_pipe_handler_contract.md`` rule, every pipe
        # handler must tolerate an empty upstream state and return an
        # empty well-shaped frame rather than raise. Caught 2026-04-23
        # when a day-1 dispatch on a freshly-deployed AG (zero parquet
        # files under the index subdir) produced
        # ``'NoneType' object has no attribute 'columns'`` from pandas.eval
        # because upstream returned None.
        if df is None:
            return pd.DataFrame()
        if isinstance(df, pd.DataFrame) and df.empty:
            # Still emit the target columns so downstream ``where`` /
            # ``table`` clauses that reference them produce a typed
            # empty frame rather than a KeyError.
            if isinstance(eval_tokens, list):
                _astr = " ".join(t.strip() for t in eval_tokens[1:])
            else:
                _astr = str(eval_tokens)
            for _assignment in self.split_arguments(_astr):
                _parts = _assignment.split("=", 1)
                if len(_parts) == 2:
                    _field = _parts[0].strip()
                    if _field and _field not in df.columns:
                        df[_field] = pd.Series(dtype="object")
            return df

        # If eval_tokens is a list, join tokens starting at index 1 (skip the "eval" directive)
        if isinstance(eval_tokens, list):
            assignments_str = " ".join(token.strip() for token in eval_tokens[1:])
        else:
            assignments_str = eval_tokens

        # Split assignments at the top level (by commas that are not nested)
        assignment_list = self.split_arguments(assignments_str)
        for assignment in assignment_list:
            parts = assignment.split("=", 1)
            if len(parts) != 2:
                raise ValueError("Invalid assignment format: " + assignment)
            field = parts[0].strip()
            expr = parts[1].strip()
            result = self.safe_eval(expr, df)
            df[field] = result
        return df

    def safe_eval(self, expr, df):
        """
        Evaluates an expression string in the context of DataFrame df.
        It performs minimal normalization by stripping the entire expression and
        ensuring that function names are immediately followed by '('.
        Dedicated branches handle functions such as concat, round, and mvfilter;
        if any special keyword is detected the expression is delegated to
        custom_eval.  Otherwise, it falls back to pandas.eval.
        """
        import re
        expr = expr.strip()
        # Normalise SPQL "!" (NOT) operator to Python "not " for AST parsing.
        expr = re.sub(r'!(?=\s*[a-zA-Z_])', 'not ', expr)
        # Collapse spaces between an identifier and an opening parenthesis.
        expr = re.sub(r'([a-zA-Z_][a-zA-Z_0-9]*)\s*\(', r'\1(', expr)
        try:
            if expr.startswith("mvfilter("):
                return self._eval_mvfilter(expr, df)

            if expr.startswith("concat("):
                inner = expr[expr.find("(") + 1:expr.rfind(")")]
                args = [arg.strip() for arg in self.split_arguments(inner)]
                length = len(df)
                evaluated_args = []
                for arg in args:
                    if arg.startswith("\"") and arg.endswith("\""):
                        value = arg.strip("\"")
                    elif arg.startswith("tostring(") or arg.startswith("lower(") or arg.startswith("upper("):
                        # Delegate nested function calls through safe_eval for
                        # MV-awareness.
                        value = self.safe_eval(arg, df)
                    else:
                        try:
                            value = pd.to_numeric(arg)
                        except Exception:
                            key = arg.replace(" ", "")
                            value = df[key] if key in df.columns else arg
                    evaluated_args.append(self.ensure_series(value, length))
                result = self.vectorized_concat(*evaluated_args)
                return result

            # NB: the previous ``round(`` early-dispatch was REMOVED 2026-05-16
            # because its naive paren-slicing
            # (``expr[expr.find("(") + 1:expr.rfind(")")]``) corrupted any
            # expression where ``round`` appeared alongside another function
            # call - e.g. ``round(1.7) + len("a")*0`` got sliced into
            # ``1.7) + len("a"`` and threw a SyntaxError. The general path
            # (custom_eval with ``round`` in env_template, wired to
            # ``MathematicOperations.complex_round`` for MV awareness)
            # handles every case correctly without the bespoke slicing.
            # See ``reference_spql_floor_function_missing_in_eval_2026_05_16.md``.

            special_keywords = [
                "if_", "case", "tonumber(",
                "lower(", "upper(", "capitalize(", "trim(", "ltrim(", "rtrim(",
                "len(", "tostring(", "substr(", "replace(", "match(",
                "urlencode(", "urldecode(", "base64_encode(", "base64_decode(",
                "randomize(", "avg(", "min(", "max(", "sum(", "median(", "mode(",
                "sqrt(", "abs(", "random(", "floor(", "ceil(", "round(",
                "not ",
                "mvdedup(", "mvsort(", "mvcount(", "mvreverse(",
                "mvjoin(", "mvfind(", "mvindex(", "mvappend(", "mvzip(",
                "split(",
            ]
            if any(keyword in expr for keyword in special_keywords):
                return self.custom_eval(expr, df)
            else:
                # Try pandas.eval first (fast path for simple arithmetic),
                # fall back to custom_eval for special function support.
                try:
                    return df.eval(expr)
                except Exception:
                    return self.custom_eval(expr, df)
        except Exception as e:
            logging.error(f"[x] Error in safe_eval for expression '{expr}': {e}")
            raise

    # ── Multi-value eval function helpers ─────────────────────────────
    # These operate on the *list itself* (not per-element like _mv_apply).
    # Scalar cells are promoted to single-element lists before calling fn.

    @staticmethod
    def _mv_eval_unary(fn):
        """Wrap fn(list) -> value for use as an eval function on a Series."""
        def _is_empty(v):
            if v is None or v == '':
                return True
            if isinstance(v, float) and v != v:  # NaN check
                return True
            return False

        def _wrapped(x):
            if isinstance(x, pd.Series):
                return x.apply(
                    lambda v: fn(v) if isinstance(v, list)
                    else fn([v]) if not _is_empty(v) else v
                )
            if isinstance(x, list):
                return fn(x)
            return fn([x]) if not _is_empty(x) else x
        return _wrapped

    @staticmethod
    def _mv_eval_binary(fn):
        """Wrap fn(list, arg2) -> value for use as an eval function."""
        def _wrapped(x, arg2):
            if isinstance(x, pd.Series):
                return x.apply(
                    lambda v: fn(v, arg2) if isinstance(v, list)
                    else fn([v], arg2) if v is not None and v != '' else v
                )
            if isinstance(x, list):
                return fn(x, arg2)
            return fn([x], arg2) if x is not None and x != '' else x
        return _wrapped

    @staticmethod
    def _mv_eval_ternary(fn):
        """Wrap fn(list, arg2, arg3) -> value for use as an eval function."""
        def _wrapped(x, arg2, arg3):
            if isinstance(x, pd.Series):
                return x.apply(
                    lambda v: fn(v, arg2, arg3) if isinstance(v, list)
                    else fn([v], arg2, arg3) if v is not None and v != '' else v
                )
            if isinstance(x, list):
                return fn(x, arg2, arg3)
            return fn([x], arg2, arg3) if x is not None and x != '' else x
        return _wrapped

    @staticmethod
    def _eval_mvappend(*args, df):
        """mvappend(val1, val2, ...) - concatenate values/lists into a single MV field."""
        length = len(df)
        result = [[] for _ in range(length)]
        for arg in args:
            if isinstance(arg, pd.Series):
                for i, v in enumerate(arg):
                    if isinstance(v, list):
                        result[i].extend(v)
                    elif v is not None and v != '':
                        result[i].append(v)
            elif isinstance(arg, list):
                for i in range(length):
                    result[i].extend(arg)
            elif arg is not None and arg != '':
                for i in range(length):
                    result[i].append(arg)
        return pd.Series(result, index=df.index)

    # ── Per-element function helpers (for scalar transforms on MV cells) ──

    @staticmethod
    def _mv_apply(fn):
        """
        Wrap a scalar function *fn(value) -> value* so it transparently
        handles pd.Series whose cells may contain Python lists (multi-value
        fields from ``values()`` aggregation).

        * Scalar cell → fn(cell)
        * List cell   → [fn(element) for element in cell]

        The wrapper preserves the Series index and dtype flexibility.
        """
        def _wrapped(x):
            if isinstance(x, pd.Series):
                return x.apply(
                    lambda v: [fn(e) for e in v] if isinstance(v, list) else fn(v)
                )
            if isinstance(x, list):
                return [fn(e) for e in x]
            return fn(x)
        return _wrapped

    @staticmethod
    def _mv_apply2(fn):
        """Like _mv_apply but for two-arg functions fn(value, arg2)."""
        def _wrapped(x, arg2):
            if isinstance(x, pd.Series):
                return x.apply(
                    lambda v: [fn(e, arg2) for e in v] if isinstance(v, list) else fn(v, arg2)
                )
            if isinstance(x, list):
                return [fn(e, arg2) for e in x]
            return fn(x, arg2)
        return _wrapped

    @staticmethod
    def _mv_apply3(fn):
        """Like _mv_apply but for three-arg functions fn(value, a2, a3)."""
        def _wrapped(x, a2, a3):
            if isinstance(x, pd.Series):
                return x.apply(
                    lambda v: [fn(e, a2, a3) for e in v] if isinstance(v, list) else fn(v, a2, a3)
                )
            if isinstance(x, list):
                return [fn(e, a2, a3) for e in x]
            return fn(x, a2, a3)
        return _wrapped

    @staticmethod
    def _eval_strptime(value, fmt=None):
        """Eval-side wrapper for ``strptime(date_str[, fmt])``.

        Routes Series input to the column-homogeneous fast path when no
        explicit format is supplied (auto-detection across the 28-format
        whitelist), and falls back to per-row parsing otherwise.
        """
        if isinstance(value, pd.Series):
            if fmt is None:
                return _parse_series_to_epoch(value)
            return value.apply(
                lambda v: _parse_to_epoch(v, str(fmt)) if pd.notna(v) else None
            )
        return _parse_to_epoch(value, str(fmt) if fmt is not None else None)

    def custom_eval(self, expr, df):
        """
        Evaluates the expression using a custom local environment that defines our
        special functions, including nested support.
        All string/transform functions are MV-aware: when a cell contains a list
        (multi-value field), the function is applied to each element.
        """
        expr = expr.strip()
        local_env = {col: df[col] for col in df.columns}
        # Add trimmed keys for columns with spaces (e.g. "my field" -> "myfield").
        local_env.update({col.replace(" ", ""): df[col] for col in df.columns if " " in col})

        # MV-aware wrappers for scalar functions
        _s = self._mv_apply    # unary
        _s2 = self._mv_apply2  # binary
        _s3 = self._mv_apply3  # ternary

        local_env.update({
            "if_": lambda cond, true_val, false_val: pd.Series(
                        np.where(
                            cond,
                            true_val if isinstance(true_val, pd.Series) else np.repeat(true_val, len(df)),
                            false_val if isinstance(false_val, pd.Series) else np.repeat(false_val, len(df))
                        ), index=df.index),
            # ── String functions (MV-aware) ──────────────────────────────
            "lower":      _s(lambda v: str(v).lower()),
            "upper":      _s(lambda v: str(v).upper()),
            "capitalize": _s(lambda v: str(v).capitalize()),
            "trim":       _s(lambda v: str(v).strip()),
            "ltrim":      _s(lambda v: str(v).lstrip()),
            "rtrim":      _s(lambda v: str(v).rstrip()),
            "len":        _s(lambda v: len(str(v))),
            "tostring":   _s(lambda v: str(v)),
            "tonumber":   _s(lambda v: pd.to_numeric(v)),
            "urlencode":  _s(lambda v: _url_quote(str(v), safe="")),
            "urldecode":  _s(lambda v: _url_unquote(str(v))),
            "defang":     _s(lambda v: str(v).replace('.', '[.]').replace(':', '[:]')),
            "fang":       _s(lambda v: str(v).replace('[.]', '.').replace('[:]', ':')),
            "type":       _s(lambda v: type(v).__name__),
            "base64_encode": _s(lambda v: base64.b64encode(str(v).encode()).decode()),
            "base64_decode": _s(lambda v: base64.b64decode(str(v)).decode()),
            # ── Numeric functions (MV-aware) ─────────────────────────────
            # ``round`` routes through ``complex_round`` so list-typed
            # cells (MV fields) round element-wise. Accepts 1 or 2 args
            # (value, optional precision) - same shape as Python's
            # built-in. Replaces the prior early-dispatch path that had
            # a paren-slicing bug for compound expressions (caught
            # 2026-05-16, see CLAUDE.md "Do Not" pin).
            "round":      lambda *args: self.math_handler.complex_round(
                              args[0],
                              int(args[1]) if len(args) > 1 else 0,
                          ),
            "abs":        _s(lambda v: abs(v)),
            "sqrt":       _s(lambda v: v ** 0.5),
            # ``floor`` and ``ceil`` were the original bug surfaced via
            # live-API validation on 2026-05-16. Standard math builtins;
            # operators reach for them when bucketing time
            # (``floor(epoch / 86400)`` = days-since-epoch). MV-aware so
            # ``floor(my_mv_field)`` floors each element.
            "floor":      _s(lambda v: math.floor(float(v))),
            "ceil":       _s(lambda v: math.ceil(float(v))),
            # ``random()`` returns a single 0.0-1.0 uniform random; with
            # 2 args returns ``random.uniform(min, max)``. Documented in
            # CLAUDE.md + docs/lang/03_functions.md but missing from the
            # env_template until 2026-05-16. Scalar result; pandas will
            # broadcast to all rows in the parent eval expression.
            "random":     lambda *args: (
                              _random.uniform(float(args[0]), float(args[1]))
                              if len(args) >= 2 else _random.random()
                          ),
            # ``sum`` / ``median`` / ``mode`` / ``range`` as eval
            # functions (NOT the stats command versions). Multi-arg
            # variants: ``sum(a, b, c)`` element-wise. They join the
            # ``min`` / ``max`` / ``avg`` family already present.
            # Documented in CLAUDE.md; missing from env_template until
            # 2026-05-16.
            "sum":        lambda *args: pd.concat(
                              [self.ensure_series(a, len(df)) for a in args], axis=1
                          ).sum(axis=1),
            "median":     lambda *args: pd.concat(
                              [self.ensure_series(a, len(df)) for a in args], axis=1
                          ).median(axis=1),
            "mode":       lambda *args: pd.concat(
                              [self.ensure_series(a, len(df)) for a in args], axis=1
                          ).mode(axis=1)[0],
            "range":      lambda *args: pd.concat(
                              [self.ensure_series(a, len(df)) for a in args], axis=1
                          ).agg(lambda r: r.max() - r.min(), axis=1),
            # ── Two-arg string functions (MV-aware) ──────────────────────
            "split":      _s2(lambda v, delim: str(v).split(str(delim))),
            "match":      _s2(lambda v, pat: bool(_re.search(pat, str(v)))),
            "substr":     _s3(lambda v, start, length: str(v)[int(start):int(start) + int(length)]),
            "replace":    _s3(lambda v, old, new: _re.sub(old, new, str(v))),
            # ── Aggregation / conditional (NOT MV-mapped) ────────────────
            "concat":     lambda *args: self.vectorized_concat(*[self.ensure_series(arg, len(df)) for arg in args]),
            "case":       lambda *args: self.case_func(*args, df=df),
            "randomize":  lambda x: self.math_handler.complex_randomize(x) if isinstance(x, (pd.Series, list))
                                     else self.math_handler.complex_randomize(float(x)),
            "avg":        lambda a, b: (a + b) / 2,
            "coalesce":   lambda *args: pd.Series(
                GeneralHandler.coalesce_lists([self.ensure_series(a, len(df)).tolist() for a in args])
            ),
            "isnull":     lambda x: x.isnull() if isinstance(x, pd.Series) else pd.isna(x),
            "isnotnull":  lambda x: x.notnull() if isinstance(x, pd.Series) else not pd.isna(x),
            # ── min/max - n-arg element-wise across columns/scalars ──────
            # Python's builtin ``min``/``max`` are shadowed in this scope so
            # users can write ``eval cheaper = min(price_a, price_b)`` and
            # get a Series back.  Scalars are broadcast to a column-length
            # Series via ``ensure_series`` first; then per-row min/max
            # across the resulting wide DataFrame.
            "min":        lambda *args: pd.concat(
                [self.ensure_series(a, len(df)) for a in args], axis=1
            ).min(axis=1),
            "max":        lambda *args: pd.concat(
                [self.ensure_series(a, len(df)) for a in args], axis=1
            ).max(axis=1),
            # ── Time / datetime functions ────────────────────────────────
            # ``now()`` returns a float epoch scalar; pandas broadcasts it
            # naturally in arithmetic (``eval delta = now() - created_at``).
            "now":          lambda: _time.time(),
            # ``relative_time("-1h@h")`` returns an int epoch using the
            # same Splunk-style syntax as ``earliest=`` / ``latest=``.
            # MV-aware so a Series of relative-time strings round-trips.
            "relative_time": _s(lambda v: _parse_relative_time(str(v))),
            # ``strftime(epoch, fmt)`` formats a UTC epoch as a string.
            "strftime":   _s2(lambda epoch, fmt: _dt.datetime.fromtimestamp(
                float(epoch), tz=_dt.timezone.utc
            ).strftime(str(fmt))),
            # ``strptime(date_str)`` auto-detects across the 28-format
            # whitelist; ``strptime(date_str, fmt)`` forces a single format.
            # When given a Series and no format, takes the column-homogeneous
            # fast path (detect-once + bulk pd.to_datetime).
            "strptime":   self._eval_strptime,
            # ── Multi-value eval functions ─────────────────────────────────
            # These operate on list-typed cells within a Series.
            "mvdedup":    self._mv_eval_unary(lambda lst: list(dict.fromkeys(lst))),
            "mvsort":     self._mv_eval_unary(lambda lst: sorted(lst, key=str)),
            "mvcount":    self._mv_eval_unary(lambda lst: len(lst)),
            "mvreverse":  self._mv_eval_unary(lambda lst: list(reversed(lst))),
            "mvjoin":     self._mv_eval_binary(lambda lst, d: str(d).join(str(v) for v in lst)),
            "mvfind":     self._mv_eval_binary(lambda lst, pat: next(
                              (i for i, v in enumerate(lst) if _re.search(pat, str(v))), -1)),
            "mvindex":    self._mv_eval_binary(lambda lst, idx: lst[int(idx)]
                              if -len(lst) <= int(idx) < len(lst) else None),
            "mvdc":       self._mv_eval_unary(lambda lst: len(set(lst))),
            "mvappend":   lambda *args: self._eval_mvappend(*args, df=df),
            "mvzip":      self._mv_eval_ternary(lambda a, b, d: [
                              str(x) + str(d) + str(y) for x, y in
                              zip(a if isinstance(a, list) else [a],
                                  b if isinstance(b, list) else [b])]),
        })
        try:
            # Parse the expression into an AST
            tree = ast.parse(expr, mode="eval")

            allowed_funcs = set(local_env.keys())

            class SafeEvaluator(ast.NodeVisitor):
                allowed_operators = {
                    ast.Add: operator.add,
                    ast.Sub: operator.sub,
                    ast.Mult: operator.mul,
                    ast.Div: operator.truediv,
                    ast.Mod: operator.mod,
                    ast.Pow: operator.pow,
                    ast.FloorDiv: operator.floordiv,
                }

                allowed_unary = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}

                allowed_bool = {ast.And: lambda a, b: a & b, ast.Or: lambda a, b: a | b}

                allowed_compare = {
                    ast.Eq: operator.eq,
                    ast.NotEq: operator.ne,
                    ast.Lt: operator.lt,
                    ast.LtE: operator.le,
                    ast.Gt: operator.gt,
                    ast.GtE: operator.ge,
                }

                def __init__(self, env):
                    self.env = env

                def visit(self, node):
                    return super().visit(node)

                def generic_visit(self, node):
                    raise ValueError(f"Unsupported expression: {ast.dump(node)}")

                def visit_Expression(self, node):
                    return self.visit(node.body)

                def visit_Name(self, node):
                    if node.id in self.env:
                        return self.env[node.id]
                    raise ValueError(f"Use of name '{node.id}' is not allowed")

                def visit_Constant(self, node):
                    return node.value

                # Python <3.8 compatibility
                def visit_Num(self, node):
                    return node.n

                def visit_BinOp(self, node):
                    if type(node.op) not in self.allowed_operators:
                        raise ValueError("Operator not allowed")
                    left = self.visit(node.left)
                    right = self.visit(node.right)
                    return self.allowed_operators[type(node.op)](left, right)

                def visit_UnaryOp(self, node):
                    if type(node.op) not in self.allowed_unary:
                        raise ValueError("Unary operator not allowed")
                    operand = self.visit(node.operand)
                    return self.allowed_unary[type(node.op)](operand)

                def visit_BoolOp(self, node):
                    if type(node.op) not in self.allowed_bool:
                        raise ValueError("Boolean operator not allowed")
                    values = [self.visit(v) for v in node.values]
                    result = values[0]
                    for v in values[1:]:
                        result = self.allowed_bool[type(node.op)](result, v)
                    return result

                def visit_Compare(self, node):
                    left = self.visit(node.left)
                    results = []
                    for op, comp in zip(node.ops, node.comparators):
                        if type(op) not in self.allowed_compare:
                            raise ValueError("Comparison operator not allowed")
                        right = self.visit(comp)
                        results.append(self.allowed_compare[type(op)](left, right))
                        left = right
                    result = results[0]
                    for r in results[1:]:
                        result = result & r
                    return result

                def visit_Call(self, node):
                    if not isinstance(node.func, ast.Name):
                        raise ValueError("Only direct function calls allowed")
                    func_name = node.func.id
                    if func_name not in allowed_funcs or not callable(self.env.get(func_name)):
                        raise ValueError(f"Function '{func_name}' is not allowed")
                    args = [self.visit(a) for a in node.args]
                    kwargs = {kw.arg: self.visit(kw.value) for kw in node.keywords}
                    return self.env[func_name](*args, **kwargs)

            evaluator = SafeEvaluator(local_env)
            result = evaluator.visit(tree)
            return result
        except Exception as e:
            logging.error(f"[x] Error in custom_eval for expression '{expr}': {e}")
            raise

    # ── mvfilter: per-element predicate filtering on multi-value fields ──

    def _eval_mvfilter(self, expr, df):
        """
        Evaluate ``mvfilter(<boolean_expr>)`` per SPQL semantics:

        For each row, identify any multi-value (list) column referenced in the
        expression.  Iterate over the individual values, evaluate the boolean
        expression with the MV field bound to the scalar element, and keep only
        those elements for which the expression is truthy.

        Returns a pd.Series of filtered lists (or scalars/nulls for non-list
        rows).
        """
        import re

        # Extract the inner boolean expression from mvfilter(...)
        inner = expr[len("mvfilter("):]
        if inner.endswith(")"):
            inner = inner[:-1]
        inner = inner.strip()

        # Identify which DataFrame column(s) the expression references.
        # We look for bare identifiers that match actual columns (exact case).
        col_set = set(df.columns)
        ident_pattern = re.compile(r'\b([a-zA-Z_]\w*)\b')
        reserved = {
            "match", "not", "True", "False", "None", "and", "or",
            "isnull", "isnotnull", "lower", "upper", "len", "like",
            "true", "false", "null", "if_", "case",
        }
        mv_field = None
        mv_ident = None  # the identifier as written in the expression
        for m in ident_pattern.finditer(inner):
            ident = m.group(1)
            if ident in col_set and ident not in reserved:
                mv_field = ident
                mv_ident = ident
                break

        if mv_field is None:
            raise ValueError(
                f"mvfilter: could not identify a multi-value field in expression: {inner}"
            )

        # Build a mini-evaluator that can evaluate the inner expression with
        # the MV field bound to a single scalar value.
        def _make_predicate(inner_expr, field_name):
            """Return a callable(scalar_value) -> bool."""
            # Normalise the field reference in the expression to a placeholder
            # that won't collide with function names.
            placeholder = "_mv_val_"
            # Replace the field name (exact case, whole-word) with placeholder
            normalised = re.sub(
                r'\b' + re.escape(field_name) + r'\b',
                placeholder,
                inner_expr,
            )

            env_template = {
                "match": lambda val, pattern: bool(_re.search(pattern, str(val))),
                "isnull": lambda x: x is None or (isinstance(x, float) and pd.isna(x)),
                "isnotnull": lambda x: x is not None and not (isinstance(x, float) and pd.isna(x)),
                "lower": lambda x: str(x).lower(),
                "upper": lambda x: str(x).upper(),
                "len": lambda x: len(str(x)),
                "like": lambda val, pattern: bool(_re.search(
                    pattern.replace("%", ".*").replace("_", "."),
                    str(val),
                )),
            }

            tree = ast.parse(normalised, mode="eval")

            allowed_funcs = set(env_template.keys())

            class ScalarEvaluator(ast.NodeVisitor):
                """Evaluate an AST expression with a single scalar binding."""
                allowed_operators = {
                    ast.Add: operator.add, ast.Sub: operator.sub,
                    ast.Mult: operator.mul, ast.Div: operator.truediv,
                    ast.Mod: operator.mod,
                }
                allowed_unary = {
                    ast.UAdd: operator.pos, ast.USub: operator.neg,
                    ast.Not: operator.not_,
                }
                allowed_compare = {
                    ast.Eq: operator.eq, ast.NotEq: operator.ne,
                    ast.Lt: operator.lt, ast.LtE: operator.le,
                    ast.Gt: operator.gt, ast.GtE: operator.ge,
                }
                allowed_bool = {
                    ast.And: lambda a, b: a and b,
                    ast.Or: lambda a, b: a or b,
                }

                def __init__(self, env):
                    self.env = env

                def visit_Expression(self, node):
                    return self.visit(node.body)

                def visit_Name(self, node):
                    if node.id in self.env:
                        return self.env[node.id]
                    raise ValueError(f"Unknown name '{node.id}' in mvfilter expression")

                def visit_Constant(self, node):
                    return node.value

                def visit_Num(self, node):
                    return node.n

                def visit_Str(self, node):
                    return node.s

                def visit_UnaryOp(self, node):
                    if type(node.op) not in self.allowed_unary:
                        raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
                    return self.allowed_unary[type(node.op)](self.visit(node.operand))

                def visit_BoolOp(self, node):
                    if type(node.op) not in self.allowed_bool:
                        raise ValueError("Unsupported boolean operator")
                    values = [self.visit(v) for v in node.values]
                    result = values[0]
                    for v in values[1:]:
                        result = self.allowed_bool[type(node.op)](result, v)
                    return result

                def visit_BinOp(self, node):
                    if type(node.op) not in self.allowed_operators:
                        raise ValueError("Operator not allowed")
                    return self.allowed_operators[type(node.op)](
                        self.visit(node.left), self.visit(node.right)
                    )

                def visit_Compare(self, node):
                    left = self.visit(node.left)
                    for op_node, comp in zip(node.ops, node.comparators):
                        if type(op_node) not in self.allowed_compare:
                            raise ValueError("Comparison not allowed")
                        right = self.visit(comp)
                        if not self.allowed_compare[type(op_node)](left, right):
                            return False
                        left = right
                    return True

                def visit_Call(self, node):
                    if not isinstance(node.func, ast.Name):
                        raise ValueError("Only direct function calls allowed in mvfilter")
                    fname = node.func.id
                    if fname not in allowed_funcs or not callable(self.env.get(fname)):
                        raise ValueError(f"Function '{fname}' not allowed in mvfilter")
                    args = [self.visit(a) for a in node.args]
                    return self.env[fname](*args)

                def generic_visit(self, node):
                    raise ValueError(f"Unsupported expression node: {ast.dump(node)}")

            def predicate(scalar_val):
                env = dict(env_template)
                env[placeholder] = scalar_val
                try:
                    return bool(ScalarEvaluator(env).visit(tree))
                except Exception:
                    return False

            return predicate

        pred = _make_predicate(inner, mv_ident)

        def _filter_row(cell):
            if isinstance(cell, list):
                filtered = [v for v in cell if pred(v)]
                return filtered if filtered else None
            # Scalar: keep if predicate passes, else null
            return cell if pred(cell) else None

        return df[mv_field].apply(_filter_row)

    def case_func(self, *args, df):
        """
        Evaluates a case statement in a vectorized manner.
        Arguments must be provided as condition, result pairs, with an optional default.
        """
        try:
            length = len(df)
            series_args = [self.ensure_series(arg, length) for arg in args]
            if len(series_args) % 2 == 1:
                default = series_args[-1]
                pairs = series_args[:-1]
            else:
                default = pd.Series([None] * length, index=df.index)
                pairs = series_args
            result = default
            # Process pairs in reverse order
            for i in range(len(pairs) - 2, -1, -2):
                condition = pairs[i]
                value = pairs[i+1]
                result = pd.Series(np.where(condition, value, result), index=df.index)
            return result
        except Exception as e:
            logging.error(f"[x] Error in case_func: {e}")
            raise

    def ensure_series(self, arg, length):
        """Ensures that arg is a pandas Series of the given length."""
        if isinstance(arg, pd.Series):
            if len(arg) != length:
                return pd.Series(np.resize(arg.values, length), index=range(length))
            return arg
        elif isinstance(arg, (int, float, str)):
            return pd.Series([arg] * length)
        else:
            try:
                s = pd.Series(arg)
                if len(s) != length:
                    s = pd.Series(np.resize(s.values, length))
                return s
            except Exception as e:
                logging.error(f"[x] Error converting argument to series: {e}")
                raise

    def vectorized_concat(self, *args):
        """
        Concatenates a list of Series (or scalars converted to Series) elementwise.
        """
        try:
            target_length = None
            for arg in args:
                if isinstance(arg, pd.Series):
                    target_length = len(arg)
                    break
            if target_length is None:
                target_length = len(pd.Series(args[0]))
            series_list = [self.ensure_series(arg, target_length) for arg in args]
            result = series_list[0].astype(str)
            for s in series_list[1:]:
                result = result + s.astype(str)
            return result
        except Exception as e:
            logging.error(f"[x] Error in vectorized_concat: {e}")
            raise

    def split_arguments(self, arg_str):
        """
        Splits a comma-separated string into arguments, taking nesting and quoted strings into account.
        Commas inside quotes or parentheses are not treated as delimiters.
        """
        args = []
        current = ""
        depth = 0
        in_quote = False
        quote_char = None

        for char in arg_str:
            # If the character is a quote and we're not already inside a quote:
            if char in ('"', "'"):
                if not in_quote:
                    in_quote = True
                    quote_char = char
                elif char == quote_char:
                    in_quote = False
                    quote_char = None
                current += char
            # If we're inside a quote, add the character verbatim.
            elif in_quote:
                current += char
            # When not in a quote, manage parentheses depth.
            else:
                if char == "(":
                    depth += 1
                    current += char
                elif char == ")":
                    depth -= 1
                    current += char
                # Only split on commas if at top-level (depth 0) and not in a quote.
                elif char == "," and depth == 0:
                    args.append(current.strip())
                    current = ""
                else:
                    current += char
        if current.strip():
            args.append(current.strip())
        return args

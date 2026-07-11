#!/usr/bin/env python3
"""
StatsHandler.py
Purpose: Implements SPQL stats, eventstats, and streamstats commands.
         Supports functions: count, sum, avg/mean, min, max, median, mode,
         dc (distinct count), values, earliest/first, latest/last, range.
         Uses pandas for aggregation and streaming operations.
"""
import logging
import re
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class StatsHandler:
    def __init__(self):
        """Initialize handler state if needed."""
        pass

    def run_stats(self, stats_tokens, df):
        """
        Entry point for stats/eventstats/streamstats.
        stats_tokens: list of tokens, e.g., ['stats','sum(x)','as','total','by','group']
        df: pandas DataFrame
        """
        if not isinstance(df, pd.DataFrame):
            logging.error(f"[x] Input is not a DataFrame: {type(df)}")
            raise TypeError("df must be a pandas DataFrame")

        directive = stats_tokens[0].lower()
        clause = " ".join(stats_tokens[1:]).strip()
        logging.info(f"[i] Running {directive} with clause: {clause}")

        # Parse BY clause and function specs
        parts = re.split(r"\s+by\s+", clause, flags=re.IGNORECASE, maxsplit=1)
        funcs_str = parts[0].strip()
        group_fields = []
        if len(parts) == 2 and parts[1].strip():
            group_fields = [f for f in re.split(r"[,\s]+", parts[1].strip()) if f]
        logging.debug(f"[DEBUG] Parsed group fields: {group_fields}")

        specs = self._parse_function_specs(funcs_str)

        # Ensure unique aliases
        aliases = [s['alias'] for s in specs]
        if len(set(aliases)) != len(aliases):
            logging.error(f"[x] Duplicate aliases detected: {aliases}")
            raise ValueError(f"Duplicate alias names in stats specs: {aliases}")

        if directive == 'stats':
            return self._do_stats(df, specs, group_fields)
        elif directive == 'eventstats':
            return self._do_eventstats(df, specs, group_fields)
        elif directive == 'streamstats':
            return self._do_streamstats(df, specs, group_fields)
        else:
            logging.error(f"[x] Unsupported directive: {directive}")
            raise ValueError(f"Unsupported directive '{directive}'")

    def _parse_function_specs(self, funcs_str):
        """
        Parses comma- or space-separated specs like 'count()', 'sum(x) as total'.
        Returns list of dicts: {'func':..., 'field':..., 'alias':..., 'alias_explicit': bool}

        2026-05-16 - fixed a silent-alias-drop bug. The previous pattern was
        ``(\\w+)\\s*(?:\\(...\\))?(?:\\s+as\\s+...)?`` where the ``\\s*`` after
        the function name greedily consumed the whitespace that the alias
        group's ``\\s+`` needed to anchor on. The no-parens shorthand
        ``count as customers`` matched ``count `` (with trailing space) and
        the alias group came up empty, so the alias was silently dropped
        and downstream pipes referencing ``customers`` exploded. Moving the
        ``\\s*`` INSIDE the optional paren group preserves the whitespace
        for the alias group. Pinned by
        ``tests/yaml/tier1_commands/test_stats_no_parens_alias.yaml`` and
        the regex-bug regression test in
        ``tests/test_spql_function_drift_guard.py``.
        """
        specs = []
        tokens = self.split_arguments(funcs_str)
        for tok in tokens:
            # Allow '*' as a field or alias for later expansion
            m = re.match(r"(\w+)(?:\s*\(\s*([\w\.\*]+)?\s*\))?(?:\s+as\s+([\w\*]+))?", tok, flags=re.IGNORECASE)
            if not m:
                logging.error(f"[x] Invalid function spec: '{tok}'")
                raise ValueError(f"Invalid function spec: '{tok}'")
            func = m.group(1).lower()
            field = m.group(2)
            explicit = m.group(3) is not None
            alias = m.group(3) or (f"{func}({field})" if field else func)
            specs.append({"func": func, "field": field, "alias": alias, "alias_explicit": explicit})
            logging.debug(f"[DEBUG] Spec parsed func={func}, field={field}, alias={alias}, explicit={explicit}")
        return specs

    def _do_stats(self, df, specs, group_fields):
        """
        Compute stats and return aggregated DataFrame.
        """
        # Expand wildcard fields and aliases before aggregation
        expanded = []
        for s in specs:
            if s['field'] == '*':
                fields = [c for c in df.columns if c not in group_fields]
                for col in fields:
                    alias = col if s['alias'] == '*' else s['alias']
                    expanded.append({'func': s['func'], 'field': col, 'alias': alias})
            else:
                alias = s['field'] if s['alias'] == '*' and s['field'] else s['alias']
                expanded.append({'func': s['func'], 'field': s['field'], 'alias': alias})
        specs = expanded

        if group_fields:
            missing = [c for c in group_fields if c not in df.columns]
            if missing:
                logging.error(f"[x] Missing group fields: {missing}")
                raise KeyError(f"Group fields not in DataFrame: {missing}")
            grouped = df.groupby(group_fields)
            agg_dict = {}
            for s in specs:
                agg_dict[s['alias']] = pd.NamedAgg(
                    column=s['field'] if s['field'] else df.columns[0],
                    aggfunc=self._agg_map(s['func'], s['field'])
                )
            result = grouped.agg(**agg_dict).reset_index()
        else:
            data = {}
            for s in specs:
                func = self._agg_map(s['func'], s['field'])
                try:
                    val = func(df[s['field']]) if s['field'] else func(df)
                except Exception as e:
                    logging.error(f"[x] Error in aggregate {s}: {e}")
                    raise
                if isinstance(val, (np.ndarray, pd.Series)):
                    val = val.tolist()
                data[s['alias']] = [val]
            result = pd.DataFrame(data)
        logging.info(f"[i] stats result shape: {result.shape}")
        return result

    def _do_eventstats(self, df, specs, group_fields):
        """
        Compute eventstats and attach results back to each row via transform for total counts.
        """
        out = df.copy()
        for s in specs:
            alias, func, fld = s['alias'], s['func'], s['field']
            if func == 'count' and fld is None:
                if group_fields:
                    first_col = df.columns[0]
                    out[alias] = df.groupby(group_fields)[first_col].transform('count')
                else:
                    out[alias] = len(df)
                logging.debug(f"[DEBUG] eventstats count() as {alias}")
            else:
                aggfunc = self._agg_map(func, fld)
                if group_fields:
                    out[alias] = df.groupby(group_fields)[fld].transform(aggfunc)
                else:
                    out[alias] = aggfunc(df[fld])
                logging.debug(f"[DEBUG] eventstats '{alias}' computed")
        logging.info(f"[i] eventstats result shape: {out.shape}")
        return out

    def _do_streamstats(self, df, specs, group_fields):
        """
        Compute cumulative (stream) statistics per group or globally.
        """
        out = df.copy()
        # handle count() without field
        for s in specs:
            if s['func'] == 'count' and s['field'] is None:
                if group_fields:
                    out[s['alias']] = df.groupby(group_fields).cumcount() + 1
                else:
                    out[s['alias']] = np.arange(1, len(df) + 1)
                logging.debug(f"[DEBUG] streamstats count() as {s['alias']}")
        # other funcs
        for s in specs:
            func, fld, alias = s['func'], s['field'], s['alias']
            if func == 'count' and fld is None:
                continue
            if fld not in df.columns:
                logging.error(f"[x] Field not in DataFrame for streamstats: {fld}")
                raise KeyError(f"Field '{fld}' not found for streamstats")
            if func == 'sum':
                cumfunc = lambda x: x.cumsum()
            elif func in ('avg', 'mean'):
                cumfunc = lambda x: x.expanding().mean()
            elif func == 'min':
                cumfunc = lambda x: x.cummin()
            elif func == 'max':
                cumfunc = lambda x: x.cummax()
            elif func == 'median':
                cumfunc = lambda x: x.expanding().median()
            elif func == 'mode':
                cumfunc = lambda x: x.expanding().apply(
                    lambda s: s.mode().iloc[0] if not s.mode().empty else np.nan,
                    raw=False)
            elif func in ('dc', 'distinctcount'):
                def cumfunc(x):
                    seen, counts = set(), []
                    for v in x:
                        seen.add(v)
                        counts.append(len(seen))
                    return pd.Series(counts, index=x.index)
            elif func == 'values':
                def cumfunc(x):
                    seen, result = [], []
                    for v in x:
                        if v not in seen:
                            seen.append(v)
                        result.append(seen.copy())
                    return pd.Series(result, index=x.index)
            elif func in ('earliest', 'first'):
                cumfunc = lambda x: x.expanding().apply(lambda s: s.iloc[0], raw=False)
            elif func in ('latest', 'last'):
                cumfunc = lambda x: x.expanding().apply(lambda s: s.iloc[-1], raw=False)
            else:
                logging.error(f"[x] Unsupported streamstats func: {func}")
                raise ValueError(f"Unsupported streamstats func '{func}'")
            if group_fields:
                out[alias] = out.groupby(group_fields)[fld].transform(cumfunc)
            else:
                out[alias] = cumfunc(out[fld])
            logging.debug(f"[DEBUG] Computed streamstats '{alias}'")
        logging.info(f"[i] streamstats result shape: {out.shape}")
        return out

    def _agg_map(self, func, field):
        """Return aggregation callable for stats/eventstats."""
        mapping = {
            'count': (lambda df: len(df)) if field is None else (lambda s: s.count()),
            'sum': lambda s: s.sum(),
            'avg': lambda s: s.mean(),
            'mean': lambda s: s.mean(),
            'min': lambda s: s.min(),
            'max': lambda s: s.max(),
            'median': lambda s: s.median(),
            'mode': lambda s: s.mode().iloc[0] if not s.mode().empty else np.nan,
            'dc': lambda s: s.nunique(),
            'distinctcount': lambda s: s.nunique(),
            'values': lambda s: list(s.dropna().unique()),
            'earliest': lambda s: s.iloc[0] if len(s) > 0 else np.nan,
            'first': lambda s: s.iloc[0] if len(s) > 0 else np.nan,
            'latest': lambda s: s.iloc[-1] if len(s) > 0 else np.nan,
            'last': lambda s: s.iloc[-1] if len(s) > 0 else np.nan,
            'range': lambda s: s.max() - s.min(),
        }
        if func not in mapping:
            logging.error(f"[x] Unsupported stats func: {func}")
            raise ValueError(f"Unsupported stats function '{func}'")
        return mapping[func]

    def split_arguments(self, arg_str):
        """
        Splits comma-separated arguments respecting nested parentheses and quotes.
        """
        args, cur, depth, in_q, qc = [], '', 0, False, None
        for ch in arg_str:
            if ch in ('"', "'"):
                if not in_q:
                    in_q, qc = True, ch
                elif ch == qc:
                    in_q = False
                cur += ch
            elif in_q:
                cur += ch
            else:
                if ch == '(':
                    depth += 1; cur += ch
                elif ch == ')':
                    depth -= 1; cur += ch
                elif ch == ',' and depth == 0:
                    args.append(cur.strip()); cur = ''
                else:
                    cur += ch
        if cur.strip():
            args.append(cur.strip())
        return args

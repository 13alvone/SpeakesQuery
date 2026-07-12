#!/usr/bin/env python3
"""
Module: speakesQueryListener.py
Purpose: Implements the parse tree listener for speakesQueryParser.
         Index loading and Parquet filtering are handled by the DuckDB-based
         module in functionality/duckdb_index_call.py.
"""

import logging
import inspect
import shlex
import sys
import os
import re
from pathlib import Path

from antlr4.tree.Tree import TerminalNodeImpl
from antlr4 import ParseTreeListener

# import pyarrow.dataset as ds
# import pyarrow.compute as pc
# import pyarrow as pa
# from pyarrow.dataset import Expression

from handlers.GeneralHandler import GeneralHandler
from handlers.LookupHandler import LookupHandler
from handlers.SearchCmdHandler import SearchDirective
from handlers.StatsHandler import StatsHandler
from handlers.MultiSearchHandler import MultiSearchHandler
from utils.tree_helpers import (
    ctx_flatten,
    flatten_list,
    flatten_with_parens,
)

# Import speakesQueryParser (support for relative or absolute import)
if "." in __name__:
    from .antlr4_active.speakesQueryParser import speakesQueryParser
else:
    from antlr4_active.speakesQueryParser import speakesQueryParser

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------------------------------
# Function to dynamically find project root by climbing up from current file
# ------------------------------------------------------------------------------------------------
def find_project_root(start_path: str, marker_files=("global_settings.defaults.yaml", "requirements.txt", ".git")):
    logging.debug("[DEBUG] Attempting to find project root...")
    current = os.path.abspath(start_path)
    while current != os.path.dirname(current):  # while not root
        if any(
            os.path.exists(os.path.join(current, marker)) for marker in marker_files
        ):
            logging.debug(f"[DEBUG] Project root identified: {current}")
            return current
        current = os.path.dirname(current)
    raise RuntimeError("Project root not found")


# Compute project root (do not change cwd)
project_root = find_project_root(__file__)

logging.info(f"[i] Project root: {project_root}")

# DuckDB-based index loader - replaces the former C++ cpp_index_call extension.
from functionality.duckdb_index_call import process_index_calls
logging.info("[i] Successfully loaded DuckDB-based index call module.")


# ------------------------------------------------------------------------------------------------
# speakesQueryListener Class Definition
# ------------------------------------------------------------------------------------------------
class speakesQueryListener(ParseTreeListener):
    """Listener used by ``speakesQueryParser`` to execute pipeline commands.

    The initial index clause is parsed by ANTLR, but the remainder of the
    pipeline is tokenised manually.  Each segment is looked up in
    ``_command_map`` and the corresponding ``_cmd_*`` method is invoked to
    transform the running ``pandas.DataFrame``.
    """
    def __init__(self, cleaned_query):
        self.current_search_cmd_tokens = None
        # Use the project root discovered at import time
        self.project_root = project_root
        self.lookup_root = f"{self.project_root}/lookups"
        # loadjob now resolves via JobStore - no filesystem root needed
        self.loadjob_root = None  # retained for reference; unused
        self.current_inputlookup_path = None
        self.current_inputlookup_call = None
        self.current_inputlookup_filename = None
        self.current_loadjob_path = None
        self.current_loadjob_call = None
        self.current_loadjob_filename = None
        self.main_df = None
        self.root_ctx = None
        self.earliest_clause = None
        self.earliest_time = None
        self.latest_clause = None
        self.latest_time = None
        self.target_index = None
        self.initial_sequence_enabled = False
        self.original_query = cleaned_query.strip()
        # Strip all whitespace from the first segment (index clause)
        first_seg = self.original_query.split("|")[0]
        # Remove inline comments (# ... to end of line) before comparing,
        # because the ANTLR lexer skips COMMENT tokens entirely.
        first_seg = re.sub(r"#[^\n]*", "", first_seg)
        self.original_index_call = re.sub(r"\s+", "", first_seg)
        self.general_handler = GeneralHandler()
        self.lookup_handler = LookupHandler()
        self.search_cmd_handler = SearchDirective()
        self.stats_handler = StatsHandler()
        self.multisearch_handler = MultiSearchHandler()

        # Map command keywords to handler functions for cleaner dispatch
        self._command_map = {
            "search": self._cmd_search,
            "where": self._cmd_search,
            "stats": self._cmd_stats,
            "eventstats": self._cmd_stats,
            "streamstats": self._cmd_stats,
            "timechart": self._cmd_timechart,
            "eval": self._cmd_eval,
            "head": self._cmd_head,
            "limit": self._cmd_head,
            "sort": self._cmd_sort,
            "reverse": self._cmd_reverse,
            "rex": self._cmd_rex,
            "regex": self._cmd_regex,
            "sql": self._cmd_sql,
            "fields": self._cmd_fields,
            "rename": self._cmd_rename,
            "fieldsummary": self._cmd_fieldsummary,
            "fillnull": self._cmd_fillnull,
            "table": self._cmd_table,
            "maketable": self._cmd_maketable,
            "base64": self._cmd_base64,
            "bin": self._cmd_bin,
            "dedup": self._cmd_dedup,
            "nearest": self._cmd_nearest,
            "dedup_semantic": self._cmd_dedup_semantic,
            "llm": self._cmd_llm,
            "llm_batch": self._cmd_llm_batch,
            "llm_route": self._cmd_llm_route,
            "llm_refine": self._cmd_llm_refine,
            "llm_ensemble": self._cmd_llm_ensemble,
            "llm_until": self._cmd_llm_until,
            "switch": self._cmd_switch,
            "join": self._cmd_join,
            "append": self._cmd_append,
            "appendpipe": self._cmd_appendpipe,
            "multisearch": self._cmd_multisearch,
            "lookup": self._cmd_lookup,
            "outputlookup": self._cmd_outputlookup,
            "outputnew": self._cmd_outputnew,
            "coalesce": self._cmd_coalesce,
            "mvexpand": self._cmd_mvexpand,
            "mvreverse": self._cmd_mvreverse,
            "mvcombine": self._cmd_mvcombine,
            "mvdedup": self._cmd_mvdedup,
            "mvappend": self._cmd_mvappend,
            "mvfilter": self._cmd_mvfilter,
            "mvcount": self._cmd_mvcount,
            "mvdc": self._cmd_mvdc,
            "mvfind": self._cmd_mvfind,
            "mvzip": self._cmd_mvzip,
            "mvjoin": self._cmd_mvjoin,
            "mvindex": self._cmd_mvindex,
            "spath": self._cmd_spath,
            "makeresults": self._cmd_makeresults,
            "addinfo": self._cmd_addinfo,
        }

    def enterEveryRule(self, ctx):
        """Capture the root context for later processing."""
        if isinstance(ctx, speakesQueryParser.SpeakesQueryContext) and self.root_ctx is None:
            self.root_ctx = ctx

    def exitEveryRule(self, ctx):
        """Manual backup trigger for ``exitSpeakesQuery``.

        The generated parser at ``lexers/antlr4_active/speakesQueryParser.py``
        already dispatches ``exitSpeakesQuery`` natively via ``hasattr``
        (see generated line ``if hasattr(listener, "exitSpeakesQuery")``),
        so this hook is a belt-and-suspenders backup. Without the
        idempotency guard inside ``exitSpeakesQuery`` itself, this hook
        caused the whole pipeline (including every Parquet index read)
        to run twice per query - triple-read in production when
        combined with ``exitExpression``'s independent load. Fixed
        2026-04-21.
        """
        if isinstance(ctx, speakesQueryParser.SpeakesQueryContext):
            self.exitSpeakesQuery(ctx)

    # Exit a parse tree produced by speakesQueryParser#speakesQuery.
    def exitSpeakesQuery(self, ctx: speakesQueryParser.SpeakesQueryContext):
        """Top level exit hook used by the parser.

        The initial index expression is parsed via ANTLR.  All subsequent
        pipeline commands (``| stats ...``, ``| eval ...`` etc.) are currently
        tokenised manually.  The grammar does define rules for these
        directives but the listener does not yet utilise those contexts for
        execution.  ``exitSpeakesQuery`` therefore re-tokenises the raw query and
        dispatches the recognised commands itself.  The list of commands handled
        this way is documented in ``_apply_command``.

        Returns the resulting ``pandas.DataFrame`` or ``None``.
        """

        # Idempotency guard - ANTLR's ParseTreeWalker invokes
        # ``exitSpeakesQuery`` directly (via the generated parser's
        # ``hasattr``-based dispatch) AND ``exitEveryRule`` above
        # re-triggers it for SpeakesQueryContext nodes. Without this
        # guard the full pipeline (Parquet reads, where, table, sort,
        # head, stats) ran twice per query; combined with the
        # ``exitExpression`` branch below the net was a triple-read
        # of every Parquet file (visible in production docker logs
        # on 2026-04-21 as 3x ``process_index_calls`` per feeder).
        # Measured latency reduction: ~65% on a 10-feeder AG dispatch.
        if getattr(self, "_exit_speakesquery_ran", False):
            return self.main_df
        self._exit_speakesquery_ran = True

        # Save the root context on first invocation
        if self.root_ctx is None:
            self.root_ctx = ctx

        # Validate any SEARCH directives early
        self.validate_exceptions(ctx)

        # Flatten to get tokens for index vs. transformations
        flattened = ctx_flatten(self.root_ctx, self.extract_screenshot_of_ctx)
        tokens = [t for t in flattened if t != "<EOF>"]

        # Skip a leading pipe if present
        if tokens and tokens[0] == "|":
            tokens = tokens[1:]

        # --- inputlookup handling ---
        if tokens and tokens[0].lower() == "inputlookup":
            filename = tokens[1].strip('"').strip()
            path = os.path.join(self.lookup_root, filename)
            logging.info(f"[i] Running inputlookup on file: {filename}")
            self.main_df = self.lookup_handler.load_data(path)

            # Force any further transforms through eval
            follow = tokens[2:]
            if follow and follow[0].lower() != "eval":
                follow.insert(0, "eval")
            from handlers.EvalHandler import EvalHandler

            eval_handler = EvalHandler()
            try:
                self.main_df = eval_handler.run_eval(follow, self.main_df)
            except Exception as e:
                logging.error(f"[x] EvalHandler failure on inputlookup: {e}")
                raise
            return self.main_df

        # --- loadjob handling ---
        if tokens and tokens[0].lower() == "loadjob":
            job_id = tokens[1].strip("'").strip().strip('"')
            logging.info(f"[i] Running loadjob on ID: {job_id}")
            self.main_df = self.general_handler.load_job(job_id)
            self.main_df = self.general_handler.add_loadjob_metadata(self.main_df, job_id)

            follow = tokens[2:]
            if follow and follow[0].lower() != "eval":
                follow.insert(0, "eval")
            from handlers.EvalHandler import EvalHandler

            eval_handler = EvalHandler()
            try:
                self.main_df = eval_handler.run_eval(follow, self.main_df)
            except Exception as e:
                logging.error(f"[x] EvalHandler failure on loadjob: {e}")
                raise
            return self.main_df

        # --- makeresults handling ---
        if tokens and tokens[0].lower() == "makeresults":
            count = 1
            annotate = False
            i = 1
            while i < len(tokens):
                if tokens[i] == "|":
                    break
                tok = tokens[i].lower()
                if tok == "count" and i + 2 < len(tokens) and tokens[i + 1] == "=":
                    count = int(tokens[i + 2])
                    i += 3
                elif tok == "annotate" and i + 2 < len(tokens) and tokens[i + 1] == "=":
                    annotate = tokens[i + 2].lower() in ("true", "1")
                    i += 3
                else:
                    i += 1
            logging.info(f"[i] Running makeresults (count={count}, annotate={annotate})")
            self.main_df = self.general_handler.make_results(count=count, annotate=annotate)

            # Process subsequent pipeline segments (everything after the
            # first '|' that introduced makeresults itself).
            # Use bracket-aware splitter so subsearches in [ ] stay intact.
            all_segments = self.split_pipeline(self.original_query)
            # First segment is makeresults - remaining are follow-up pipes
            follow_segments = all_segments[1:]

            valid_lines = ctx.validLine()
            for j, seg_str in enumerate(follow_segments):
                try:
                    seg_tokens = shlex.split(seg_str)
                except ValueError as e:
                    logging.error(f"[x] Failed to tokenize segment '{seg_str}': {e}")
                    raise
                cmd = seg_tokens[0].split("(")[0].lower()
                logging.info(f"[i] Processing pipeline segment: {cmd}")

                if cmd in ("stats", "eventstats", "streamstats"):
                    try:
                        dctx = valid_lines[j].directive()
                        seg_tokens = ctx_flatten(dctx, self.extract_screenshot_of_ctx)
                        seg_tokens = speakesQueryListener.normalize_tokens(seg_tokens)
                    except Exception as e:
                        logging.error(f"[x] Failed to parse directive via context: {e}")
                        raise

                try:
                    self.main_df = self._apply_command(cmd, seg_tokens, seg_str)
                except Exception as e:
                    logging.error(f"[x] Failure processing '{seg_str}': {e}")
                    raise

            return self.main_df

        # --- index call only? ---
        try:
            first_pipe = tokens.index("|")
        except ValueError:
            # no pipe = pure index call
            combined = "".join(tokens).replace(" ", "")
            if combined == self.original_index_call:
                logging.info("[i] Executing index call only.")
                self.main_df = process_index_calls(tokens)
            else:
                logging.warning("[!] Tokens did not match original index call.")
            return self.main_df

        # --- execute index call portion ---
        index_tokens = tokens[:first_pipe]
        combined_index = "".join(index_tokens).replace(" ", "")
        if combined_index == self.original_index_call:
            # NOTE: ``exitExpression`` has already loaded main_df via its
            # own process_index_calls call (with expression-context
            # tokens). This second load replaces it with the
            # root-ctx-flattened token version. Keeping this replacement
            # is load-bearing - some tier-3 queries produce different
            # token flattening between the expression-ctx and root-ctx
            # and the root-ctx version is the one that matches the
            # downstream pipe loop's expectations. This is one redundant
            # read per query (down from 3 pre-2026-04-21, now 2 max),
            # preserved to avoid the regression described in
            # tests/test_spql.py::tier3_complex evaluation-order cases.
            logging.info("[i] Executing index call portion.")
            self.main_df = process_index_calls(index_tokens)
        else:
            logging.warning("[!] Index call tokens mismatch expected index call.")

        # --- transformations: re-tokenize raw query so we capture all BY fields ---
        # Use bracket-aware splitter, then drop the first segment (index call).
        all_segments = self.split_pipeline(self.original_query)
        segment_strs = all_segments[1:]  # everything after the index clause

        valid_lines = ctx.validLine()
        for i, seg_str in enumerate(segment_strs):
            # Tokenize segment, preserving quoted literals
            try:
                seg_tokens = shlex.split(seg_str)
            except ValueError as e:
                logging.error(f"[x] Failed to tokenize segment '{seg_str}': {e}")
                raise

            cmd = seg_tokens[0].split("(")[0].lower()
            logging.info(f"[i] Processing pipeline segment: {cmd}")

            if cmd in ("stats", "eventstats", "streamstats"):
                try:
                    dctx = valid_lines[i].directive()
                    seg_tokens = ctx_flatten(dctx, self.extract_screenshot_of_ctx)
                    seg_tokens = speakesQueryListener.normalize_tokens(seg_tokens)
                except Exception as e:
                    logging.error(f"[x] Failed to parse directive via context: {e}")
                    raise

            try:
                self.main_df = self._apply_command(cmd, seg_tokens, seg_str)
            except Exception as e:
                logging.error(f"[x] Failure processing '{seg_str}': {e}")
                raise

        return self.main_df

    def _apply_command(self, cmd, seg_tokens, seg_str):
        """Dispatch transformation commands parsed manually."""
        handler = self._command_map.get(cmd)
        if handler:
            return handler(seg_tokens, seg_str)
        if cmd in ("if_", "case", "tonumber"):
            from handlers.EvalHandler import EvalHandler

            return EvalHandler().run_eval(seg_tokens, self.main_df)
        # NOTE: Backtick macro expansion is now handled pre-parse in
        # CmdExecutionBackend.execute_query() before ANTLR4 ever sees the
        # query.  Any backtick expressions reaching here are unrecognised.
        logging.warning(f"[!] Unhandled transformation '{cmd}', ignoring")
        return self.main_df

    # Individual command handlers
    def _cmd_search(self, seg_tokens, seg_str):
        """Run a search or where clause.
        seg_tokens is a list like ['search', 'status', '=', '200']; seg_str is the raw segment. Returns a DataFrame.
        """
        expr = seg_str[len(seg_tokens[0].split("(")[0]):].strip()
        # Order matters: match comparison operators and numeric literals
        # BEFORE bare word chars so `leading_price >= 0.75` tokenises as
        # [leading_price, >=, 0.75] instead of [0, ., 75].
        # `==` MUST come before `=` so `x == 1` doesn't tokenise as two
        # separate `=` operators (caught 2026-05-05 - `where x == 1`
        # silently returned 0 rows because the parser couldn't make sense
        # of `[x, =, =, 1]`). The handler treats `==` and `=` identically.
        pattern = (
            r'"[^\"]*"'          # double-quoted string literal
            r'|==|>=|<=|!=|=|>|<'  # comparison operators (== before =)
            r'|\(|\)|,'          # structural punctuation
            r'|\d+\.\d+'         # decimal number (leading digit required)
            r'|\w+'              # identifier / integer literal
            r'|\S'               # any remaining non-whitespace char
        )
        tokens = re.findall(pattern, expr)
        return self.search_cmd_handler.run_search(tokens, self.main_df)

    def _cmd_stats(self, seg_tokens, _):
        """Process stats/eventstats/streamstats.
        seg_tokens lists the command and its arguments, e.g. ["stats", "count", "by", "host"].
        Returns the transformed DataFrame.
        """
        return self.stats_handler.run_stats(seg_tokens, self.main_df)

    def _cmd_timechart(self, seg_tokens, _):
        """Generate a timechart. seg_tokens example: ["timechart", "count", "by", "hour"].
        Returns a DataFrame ready for charting.
        """
        from handlers.ChartHandler import ChartHandler
        return ChartHandler().run_timechart(seg_tokens, self.main_df)

    def _cmd_eval(self, seg_tokens, seg_str):
        """Evaluate expressions on the DataFrame.
        Uses the raw segment string to preserve quotes, parens, and operators
        that shlex or ANTLR would otherwise mangle.
        """
        from handlers.EvalHandler import EvalHandler
        # Extract everything after "eval " from the raw string to preserve
        # special characters (!,  quotes, nested parens) verbatim.
        raw_expr = seg_str[len("eval"):].strip() if seg_str.lower().startswith("eval") else " ".join(seg_tokens[1:])
        return EvalHandler().run_eval(["eval", raw_expr], self.main_df)

    def _cmd_head(self, seg_tokens, _):
        """Return the first n rows. seg_tokens example: ["head", "10"]. Default is 5.
        Returns the truncated DataFrame.
        """
        count = int(seg_tokens[1]) if len(seg_tokens) > 1 else 5
        return self.general_handler.head_call(self.main_df, count, "head")

    def _cmd_sort(self, seg_tokens, _):
        """Sort the DataFrame by columns.
        Supports: sort -field, sort - count, sort - 0 count, sort 0 -count, etc.
        An optional numeric limit (0 = unlimited) may appear after the direction
        or before the field list. Direction determined by +/- prefix or standalone
        +/- token. Returns sorted DataFrame.
        """
        args = seg_tokens[1:]  # everything after "sort"

        # Determine direction and strip standalone +/- token
        direction = "-"  # default descending (SPL convention)
        if args and args[0] in ("+", "-"):
            direction = args.pop(0)
        elif args and (args[0].startswith("+") or args[0].startswith("-")):
            direction = "+" if args[0].startswith("+") else "-"

        # Strip optional numeric limit (e.g. "0" meaning unlimited)
        limit = None
        if args and args[0].lstrip("-+").isdigit() and args[0].lstrip("+-").strip(",") not in getattr(self.main_df, 'columns', []):
            tok = args.pop(0)
            tok_clean = tok.lstrip("+-").strip(",")
            if tok_clean.isdigit():
                limit = int(tok_clean)

        # Remaining args are column names
        cols = [c.lstrip("+-").strip(",") for c in args]
        cols = [c for c in cols if c]  # drop empties

        if not cols:
            raise RuntimeError("sort requires at least one field name.")

        result = self.general_handler.sort_df_by_columns(self.main_df, cols, direction)

        # Apply row limit if specified (0 means no limit)
        if limit is not None and limit > 0:
            result = result.head(limit)

        return result

    def _cmd_reverse(self, seg_tokens, _):
        """Reverse row order of the DataFrame. seg_tokens only contains the command name.
        Returns the reversed DataFrame.
        """
        return self.general_handler.reverse_df_rows(self.main_df)

    def _cmd_rex(self, seg_tokens, _):
        """Apply a rex extraction. seg_tokens contains command arguments followed by the regex.
        Returns the DataFrame with extracted fields.
        """
        args = []
        for tok in seg_tokens[1:-1]:
            if "=" in tok:
                key, val = tok.split("=", 1)
                args.extend([key, "=", val])
            else:
                args.append(tok)
        if len(seg_tokens) > 1:
            args.append(seg_tokens[-1])
        return self.general_handler.execute_rex(self.main_df, args)

    def _cmd_regex(self, seg_tokens, _):
        """Filter rows using a regular expression. seg_tokens like ["regex", "field=expr"].
        Returns the filtered DataFrame.
        """
        field, regex = seg_tokens[1].split("=", 1)
        return self.general_handler.filter_df_by_regex(self.main_df, field, regex)

    def _cmd_sql(self, seg_tokens, _):
        """Run one DuckDB SQL statement against the pipeline DataFrame
        (registered as the view ``pipeline``). seg_tokens like
        ["sql", "SELECT * FROM pipeline"] - shlex has already dequoted
        the statement. Sandboxed per-call connection with
        enable_external_access=false; see handlers/SqlHandler.py.
        """
        from handlers.SqlHandler import SqlHandler
        sql_text = " ".join(seg_tokens[1:])
        return SqlHandler().execute_sql(self.main_df, sql_text)

    def _cmd_fields(self, seg_tokens, _):
        """Select or drop columns. seg_tokens like ["fields", "-foo", "bar"].
        Returns DataFrame with columns filtered.
        """
        mode = "+"
        cols = []
        for tok in seg_tokens[1:]:
            if tok.startswith("-"):
                mode = "-"
                cols.append(tok[1:].strip(","))
            else:
                cols.append(tok.strip(","))
        return self.general_handler.filter_df_columns(self.main_df, cols, mode)

    def _cmd_rename(self, seg_tokens, _):
        """Rename columns. seg_tokens example: ["rename", "old as new"].
        Returns the DataFrame with updated column names.
        """
        pairs = [p.strip() for p in " ".join(seg_tokens[1:]).split(",")]
        for pair in pairs:
            if "as" in pair:
                old, new = [s.strip() for s in pair.split("as")]
                self.main_df = self.general_handler.rename_column(self.main_df, old, new)
        return self.main_df

    def _cmd_fieldsummary(self, seg_tokens, _):
        """Summarise field statistics. seg_tokens only includes the command.
        Returns a DataFrame containing the summary.
        """
        return self.general_handler.execute_fieldsummary(self.main_df)

    def _cmd_fillnull(self, seg_tokens, _):
        """Replace null values. seg_tokens after the command list default replacements.
        Returns the DataFrame with nulls filled.
        """
        return self.general_handler.execute_fillnull(self.main_df, seg_tokens[1:])

    def _cmd_table(self, seg_tokens, _):
        """Keep only specified columns. seg_tokens like ["table", "foo", "bar"].
        Returns the reduced DataFrame.
        """
        cols = [c.strip(",") for c in seg_tokens[1:]]
        return self.general_handler.filter_df_columns(self.main_df, cols, "+")

    def _cmd_maketable(self, seg_tokens, _):
        """Create an empty DataFrame with the specified columns.
        seg_tokens example: ["maketable", "foo", "bar"]. Returns the new DataFrame.
        """
        cols = [c.strip(",") for c in seg_tokens[1:]]
        return self.general_handler.create_empty_dataframe(cols)

    def _cmd_base64(self, seg_tokens, _):
        """Decode base64 fields. seg_tokens contains the command and arguments.
        Returns the DataFrame with decoded columns.
        """
        return self.general_handler.handle_base64(self.main_df, seg_tokens)

    def _cmd_bin(self, seg_tokens, _):
        """Round time values into bins. seg_tokens example: ["bin", "_time", "span", "=", "1h"].
        Returns the DataFrame with an added binned column.
        """
        field = seg_tokens[1]
        span = seg_tokens[seg_tokens.index("span") + 2] if "span" in seg_tokens else "1h"
        return self.general_handler.execute_bin(self.main_df, field, span)

    def _cmd_dedup(self, seg_tokens, _):
        """Remove duplicate rows. seg_tokens may include a count and options.
        Returns the deduplicated DataFrame.
        """
        args = []
        tokens = seg_tokens[1:]
        if tokens and re.fullmatch(r"\d+", tokens[0]):
            args.append(int(tokens[0]))
            tokens = tokens[1:]
        consec_val = None
        consec_idx = None
        for i, t in enumerate(tokens):
            if t == "consecutive":
                if i + 2 < len(tokens) and tokens[i + 1] == "=":
                    consec_val = tokens[i + 2]
                    consec_idx = i
                    break
                if i + 1 < len(tokens):
                    val_tok = tokens[i + 1]
                    consec_val = val_tok.split("=", 1)[1] if "=" in val_tok else val_tok
                    consec_idx = i
                    break
            elif t.startswith("consecutive="):
                consec_val = t.split("=", 1)[1]
                consec_idx = i
                break
        if consec_idx is not None:
            if tokens[consec_idx] == "consecutive":
                if consec_idx + 2 < len(tokens) and tokens[consec_idx + 1] == "=":
                    del tokens[consec_idx : consec_idx + 3]
                else:
                    del tokens[consec_idx : consec_idx + 2]
            else:
                tokens.pop(consec_idx)
            args.extend(["consecutive", "=", consec_val])
        fields = []
        for tok in tokens:
            parts = [p for p in tok.split(",") if p]
            fields.extend(parts)
        for i, f in enumerate(fields):
            args.append(f)
            if i < len(fields) - 1:
                args.append(",")
        return self.general_handler.execute_dedup(self.main_df, args)

    def _cmd_nearest(self, seg_tokens, _):
        """Rank rows by cosine similarity to a query string.

        Token shape (post shlex.split):
          ["nearest", "<query string>", "topk=N", "threshold=F", "field=col"]

        kwargs are optional and order-insensitive. The query string is
        always the first positional argument.
        """
        from handlers.SemanticHandler import nearest as _nearest, SemanticPipeError

        args = seg_tokens[1:]
        if not args:
            raise RuntimeError("nearest requires a query string.")
        query = args[0]
        kwargs: dict = {}
        for tok in args[1:]:
            if "=" not in tok:
                raise RuntimeError(
                    f"nearest unexpected token {tok!r}; expected key=value"
                )
            k, v = tok.split("=", 1)
            kwargs[k.strip().lower()] = v.strip()
        topk = int(kwargs["topk"]) if "topk" in kwargs else 10
        threshold = float(kwargs["threshold"]) if "threshold" in kwargs else None
        field = kwargs.get("field")
        try:
            return _nearest(
                self.main_df, query,
                topk=topk, threshold=threshold, field=field,
            )
        except SemanticPipeError as exc:
            # Surface as RuntimeError so SPQL's existing error formatter
            # treats it consistently with other pipe failures.
            raise RuntimeError(f"nearest: {exc}") from exc

    def _cmd_llm(self, seg_tokens, _):
        """Apply an LLM to each row.

        Token shape (post shlex.split):
          ["llm", "model=<id>", "prompt=<...>", "system=<...>",
           "field=<col>", "use_cache=<bool>", "max_tokens=<N>",
           "max_cost_usd=<F>", "dry_run=<bool>"]

        Required: model + prompt. Other kwargs are optional and
        order-insensitive.

        Slice 7 added ``max_cost_usd`` (hard ceiling on cumulative
        cost; ``0`` = no cap) and ``dry_run`` (returns a 1-row cost
        preview without calling any provider).
        """
        from handlers.LLMHandler import llm_pipe, LLMPipeError

        kwargs: dict = {}
        for tok in seg_tokens[1:]:
            if "=" not in tok:
                raise RuntimeError(
                    f"llm unexpected token {tok!r}; expected key=value"
                )
            k, v = tok.split("=", 1)
            kwargs[k.strip().lower()] = v.strip()

        model = kwargs.get("model")
        if not model:
            raise RuntimeError(
                "llm requires model=<registry_id>; e.g. "
                "`| llm model=\"claude-haiku-4-5-20251001\" prompt=\"...\"`"
            )
        prompt = kwargs.get("prompt")
        if not prompt:
            raise RuntimeError("llm requires prompt=<string>")

        system = kwargs.get("system") or None
        field = kwargs.get("field") or None

        # use_cache: default True. Accept the BOOLEAN literals shlex
        # passes through (TRUE / true / FALSE / false).
        if "use_cache" in kwargs:
            v = kwargs["use_cache"].strip().lower()
            if v in ("true", "1", "yes"):
                use_cache = True
            elif v in ("false", "0", "no"):
                use_cache = False
            else:
                raise RuntimeError(
                    f"llm use_cache must be true|false, got {v!r}"
                )
        else:
            use_cache = True

        max_tokens = None
        if "max_tokens" in kwargs:
            try:
                max_tokens = int(kwargs["max_tokens"])
            except ValueError:
                raise RuntimeError(
                    f"llm max_tokens must be an integer, got "
                    f"{kwargs['max_tokens']!r}"
                )

        # ── Slice 7 kwargs ─────────────────────────────────────────
        max_cost_usd = self._resolve_max_cost_kwarg(kwargs, pipe_label="llm")
        dry_run = self._resolve_dry_run_kwarg(kwargs, pipe_label="llm")

        try:
            return llm_pipe(
                self.main_df,
                model=model, prompt=prompt,
                system=system, field=field,
                use_cache=use_cache, max_tokens=max_tokens,
                max_cost_usd=max_cost_usd, dry_run=dry_run,
            )
        except LLMPipeError as exc:
            raise RuntimeError(f"llm: {exc}") from exc

    def _cmd_llm_batch(self, seg_tokens, _):
        """Apply an LLM to the WHOLE DataFrame as one prompt.

        Token shape (post shlex.split):
          ["llm_batch", "model=<id>", "prompt=<...>", "system=<...>",
           "field=<col>", "use_cache=<bool>", "max_tokens=<N>",
           "max_rows=<N>"]

        Required: model + prompt. Other kwargs are optional and
        order-insensitive. Default max_rows=20 caps the input fed to
        the model.
        """
        from handlers.LLMHandler import llm_batch_pipe, LLMPipeError

        kwargs: dict = {}
        for tok in seg_tokens[1:]:
            if "=" not in tok:
                raise RuntimeError(
                    f"llm_batch unexpected token {tok!r}; expected key=value"
                )
            k, v = tok.split("=", 1)
            kwargs[k.strip().lower()] = v.strip()

        model = kwargs.get("model")
        if not model:
            raise RuntimeError(
                "llm_batch requires model=<registry_id>; e.g. "
                "`| llm_batch model=\"claude-sonnet-4-6\" prompt=\"...\"`"
            )
        prompt = kwargs.get("prompt")
        if not prompt:
            raise RuntimeError("llm_batch requires prompt=<string>")

        system = kwargs.get("system") or None
        field = kwargs.get("field") or None

        if "use_cache" in kwargs:
            v = kwargs["use_cache"].strip().lower()
            if v in ("true", "1", "yes"):
                use_cache = True
            elif v in ("false", "0", "no"):
                use_cache = False
            else:
                raise RuntimeError(
                    f"llm_batch use_cache must be true|false, got {v!r}"
                )
        else:
            use_cache = True

        max_tokens = None
        if "max_tokens" in kwargs:
            try:
                max_tokens = int(kwargs["max_tokens"])
            except ValueError:
                raise RuntimeError(
                    f"llm_batch max_tokens must be an integer, got "
                    f"{kwargs['max_tokens']!r}"
                )

        max_rows = 20  # matches _DEFAULT_BATCH_MAX_ROWS in handler
        if "max_rows" in kwargs:
            try:
                max_rows = int(kwargs["max_rows"])
            except ValueError:
                raise RuntimeError(
                    f"llm_batch max_rows must be an integer, got "
                    f"{kwargs['max_rows']!r}"
                )

        # ── Slice 7 kwargs ─────────────────────────────────────────
        max_cost_usd = self._resolve_max_cost_kwarg(
            kwargs, pipe_label="llm_batch",
        )
        dry_run = self._resolve_dry_run_kwarg(
            kwargs, pipe_label="llm_batch",
        )

        try:
            return llm_batch_pipe(
                self.main_df,
                model=model, prompt=prompt,
                system=system, field=field,
                use_cache=use_cache, max_tokens=max_tokens,
                max_rows=max_rows,
                max_cost_usd=max_cost_usd, dry_run=dry_run,
            )
        except LLMPipeError as exc:
            raise RuntimeError(f"llm_batch: {exc}") from exc

    def _cmd_llm_route(self, seg_tokens, _):
        """Confidence-based 2-stage cost cascade - Phase 4 / Bet 3 slice 1.

        Token shape (post shlex.split):
          ["llm_route", "model=<cheap-id>", "prompt=<...>",
           "escalate_to=<expensive-id>",
           "escalate_prompt=<...>"?, "confidence_threshold=<float>"?,
           "system=<...>"?, "field=<col>"?, "use_cache=<bool>"?,
           "max_tokens=<N>"?, "max_cost_usd=<F>"?, "dry_run=<bool>"?]

        Required: model + prompt + escalate_to. The cheap model runs on
        every row; rows whose stage-1 output parses to a number BELOW
        ``confidence_threshold`` (default 0.5) - OR doesn't parse to a
        number at all, OR errored - escalate to ``escalate_to``.

        Cost-cascade economics: 80% of rows handled by the cheap stage,
        20% escalate. End-to-end cost approaches the cheap-stage's per-
        row cost while fidelity stays close to the expensive stage's.
        """
        from handlers.LLMHandler import llm_route_pipe, LLMPipeError

        kwargs: dict = {}
        for tok in seg_tokens[1:]:
            if "=" not in tok:
                raise RuntimeError(
                    f"llm_route unexpected token {tok!r}; expected key=value"
                )
            k, v = tok.split("=", 1)
            kwargs[k.strip().lower()] = v.strip()

        model = kwargs.get("model")
        if not model:
            raise RuntimeError(
                "llm_route requires model=<cheap-registry-id>; e.g. "
                '`| llm_route model="ollama-llama3-1-8b" '
                'prompt="..." escalate_to="claude-sonnet-4-6"`'
            )
        prompt = kwargs.get("prompt")
        if not prompt:
            raise RuntimeError("llm_route requires prompt=<string>")
        escalate_to = kwargs.get("escalate_to")
        if not escalate_to:
            raise RuntimeError(
                "llm_route requires escalate_to=<expensive-registry-id>"
            )

        escalate_prompt = kwargs.get("escalate_prompt") or None
        system = kwargs.get("system") or None
        field = kwargs.get("field") or None

        confidence_threshold = 0.5
        if "confidence_threshold" in kwargs:
            try:
                confidence_threshold = float(kwargs["confidence_threshold"])
            except ValueError:
                raise RuntimeError(
                    f"llm_route confidence_threshold must be a number, "
                    f"got {kwargs['confidence_threshold']!r}"
                )

        if "use_cache" in kwargs:
            v = kwargs["use_cache"].strip().lower()
            if v in ("true", "1", "yes"):
                use_cache = True
            elif v in ("false", "0", "no"):
                use_cache = False
            else:
                raise RuntimeError(
                    f"llm_route use_cache must be true|false, got {v!r}"
                )
        else:
            use_cache = True

        max_tokens = None
        if "max_tokens" in kwargs:
            try:
                max_tokens = int(kwargs["max_tokens"])
            except ValueError:
                raise RuntimeError(
                    f"llm_route max_tokens must be an integer, got "
                    f"{kwargs['max_tokens']!r}"
                )

        # ── Slice 7 contract - every billable pipe MUST honour ───────
        max_cost_usd = self._resolve_max_cost_kwarg(
            kwargs, pipe_label="llm_route",
        )
        dry_run = self._resolve_dry_run_kwarg(
            kwargs, pipe_label="llm_route",
        )

        try:
            return llm_route_pipe(
                self.main_df,
                model=model, prompt=prompt,
                escalate_to=escalate_to,
                escalate_prompt=escalate_prompt,
                confidence_threshold=confidence_threshold,
                system=system, field=field,
                use_cache=use_cache, max_tokens=max_tokens,
                max_cost_usd=max_cost_usd, dry_run=dry_run,
            )
        except LLMPipeError as exc:
            raise RuntimeError(f"llm_route: {exc}") from exc

    def _cmd_llm_refine(self, seg_tokens, _):
        """Drafter/critic refinement loop - Phase 4 / Bet 3 slice 2.

        Token shape (post shlex.split):
          ["llm_refine", "drafter_model=<id>", "critic_model=<id>",
           "drafter_prompt=<...>", "critic_prompt=<...>",
           "revise_prompt=<...>"?, "max_rounds=<N>"?,
           "converge_when_critic_says=<str>"?, "system=<...>"?,
           "field=<col>"?, "use_cache=<bool>"?, "max_tokens=<N>"?,
           "max_cost_usd=<F>"?, "dry_run=<bool>"?]

        Required: drafter_model + critic_model + drafter_prompt +
        critic_prompt. Each row goes through up to ``max_rounds``
        (default 3) drafter→critic cycles, exiting early when the
        critic's output contains ``converge_when_critic_says``.
        """
        from handlers.LLMHandler import llm_refine_pipe, LLMPipeError

        kwargs: dict = {}
        for tok in seg_tokens[1:]:
            if "=" not in tok:
                raise RuntimeError(
                    f"llm_refine unexpected token {tok!r}; expected key=value"
                )
            k, v = tok.split("=", 1)
            kwargs[k.strip().lower()] = v.strip()

        drafter_model = kwargs.get("drafter_model")
        if not drafter_model:
            raise RuntimeError(
                "llm_refine requires drafter_model=<registry-id>"
            )
        critic_model = kwargs.get("critic_model")
        if not critic_model:
            raise RuntimeError(
                "llm_refine requires critic_model=<registry-id>"
            )
        drafter_prompt = kwargs.get("drafter_prompt")
        if not drafter_prompt:
            raise RuntimeError(
                "llm_refine requires drafter_prompt=<string>"
            )
        critic_prompt = kwargs.get("critic_prompt")
        if not critic_prompt:
            raise RuntimeError(
                "llm_refine requires critic_prompt=<string>"
            )

        revise_prompt = kwargs.get("revise_prompt") or None
        converge_when_critic_says = (
            kwargs.get("converge_when_critic_says") or None
        )
        system = kwargs.get("system") or None
        field = kwargs.get("field") or None

        max_rounds = 3
        if "max_rounds" in kwargs:
            try:
                max_rounds = int(kwargs["max_rounds"])
            except ValueError:
                raise RuntimeError(
                    f"llm_refine max_rounds must be an integer, got "
                    f"{kwargs['max_rounds']!r}"
                )

        if "use_cache" in kwargs:
            v = kwargs["use_cache"].strip().lower()
            if v in ("true", "1", "yes"):
                use_cache = True
            elif v in ("false", "0", "no"):
                use_cache = False
            else:
                raise RuntimeError(
                    f"llm_refine use_cache must be true|false, got {v!r}"
                )
        else:
            use_cache = True

        max_tokens = None
        if "max_tokens" in kwargs:
            try:
                max_tokens = int(kwargs["max_tokens"])
            except ValueError:
                raise RuntimeError(
                    f"llm_refine max_tokens must be an integer, got "
                    f"{kwargs['max_tokens']!r}"
                )

        # ── Slice 7 contract - every billable pipe MUST honour ───────
        max_cost_usd = self._resolve_max_cost_kwarg(
            kwargs, pipe_label="llm_refine",
        )
        dry_run = self._resolve_dry_run_kwarg(
            kwargs, pipe_label="llm_refine",
        )

        try:
            return llm_refine_pipe(
                self.main_df,
                drafter_model=drafter_model,
                critic_model=critic_model,
                drafter_prompt=drafter_prompt,
                critic_prompt=critic_prompt,
                revise_prompt=revise_prompt,
                max_rounds=max_rounds,
                converge_when_critic_says=converge_when_critic_says,
                system=system, field=field,
                use_cache=use_cache, max_tokens=max_tokens,
                max_cost_usd=max_cost_usd, dry_run=dry_run,
            )
        except LLMPipeError as exc:
            raise RuntimeError(f"llm_refine: {exc}") from exc

    def _cmd_llm_ensemble(self, seg_tokens, _):
        """Multi-model voting - Phase 4 / Bet 3 slice 3.

        Token shape (post shlex.split):
          ["llm_ensemble", "models=<id1,id2,id3>", "prompt=<...>",
           "aggregator=<majority|average|unanimous>"?,
           "min_agreement=<float>"?, "system=<...>"?, "field=<col>"?,
           "use_cache=<bool>"?, "max_tokens=<N>"?,
           "max_cost_usd=<F>"?, "dry_run=<bool>"?]

        Required: models + prompt. ``models`` is a comma-separated list
        of registered model ids (whitespace around commas tolerated).
        Each row sends the same prompt to every model; outputs are
        aggregated by the chosen ``aggregator``.
        """
        from handlers.LLMHandler import llm_ensemble_pipe, LLMPipeError

        kwargs: dict = {}
        for tok in seg_tokens[1:]:
            if "=" not in tok:
                raise RuntimeError(
                    f"llm_ensemble unexpected token {tok!r}; "
                    "expected key=value"
                )
            k, v = tok.split("=", 1)
            kwargs[k.strip().lower()] = v.strip()

        models_raw = kwargs.get("models")
        if not models_raw:
            raise RuntimeError(
                "llm_ensemble requires models=<comma-separated registry-ids>; "
                'e.g. `| llm_ensemble models="ollama-llama3-1-8b,'
                'claude-haiku-4-5-20251001" prompt="..."`'
            )
        # Split + strip; reject empty entries
        models = [m.strip() for m in models_raw.split(",") if m.strip()]
        if len(models) < 2:
            raise RuntimeError(
                f"llm_ensemble requires at least 2 models for voting, "
                f"got {len(models)}: {models}. Use | llm for single-model."
            )

        prompt = kwargs.get("prompt")
        if not prompt:
            raise RuntimeError("llm_ensemble requires prompt=<string>")

        aggregator = (kwargs.get("aggregator") or "majority").strip().lower()
        if aggregator not in ("majority", "average", "unanimous"):
            raise RuntimeError(
                f"llm_ensemble aggregator must be one of "
                f"majority|average|unanimous, got {aggregator!r}"
            )

        min_agreement = 0.0
        if "min_agreement" in kwargs:
            try:
                min_agreement = float(kwargs["min_agreement"])
            except ValueError:
                raise RuntimeError(
                    f"llm_ensemble min_agreement must be a number, got "
                    f"{kwargs['min_agreement']!r}"
                )

        system = kwargs.get("system") or None
        field = kwargs.get("field") or None

        if "use_cache" in kwargs:
            v = kwargs["use_cache"].strip().lower()
            if v in ("true", "1", "yes"):
                use_cache = True
            elif v in ("false", "0", "no"):
                use_cache = False
            else:
                raise RuntimeError(
                    f"llm_ensemble use_cache must be true|false, got {v!r}"
                )
        else:
            use_cache = True

        max_tokens = None
        if "max_tokens" in kwargs:
            try:
                max_tokens = int(kwargs["max_tokens"])
            except ValueError:
                raise RuntimeError(
                    f"llm_ensemble max_tokens must be an integer, got "
                    f"{kwargs['max_tokens']!r}"
                )

        # ── Slice 7 contract - every billable pipe MUST honour ───────
        max_cost_usd = self._resolve_max_cost_kwarg(
            kwargs, pipe_label="llm_ensemble",
        )
        dry_run = self._resolve_dry_run_kwarg(
            kwargs, pipe_label="llm_ensemble",
        )

        try:
            return llm_ensemble_pipe(
                self.main_df,
                models=models, prompt=prompt,
                aggregator=aggregator, min_agreement=min_agreement,
                system=system, field=field,
                use_cache=use_cache, max_tokens=max_tokens,
                max_cost_usd=max_cost_usd, dry_run=dry_run,
            )
        except LLMPipeError as exc:
            raise RuntimeError(f"llm_ensemble: {exc}") from exc

    def _cmd_llm_until(self, seg_tokens, _):
        """Convergence loop with hard ceiling - Phase 4 / Bet 3 slice 4.

        Token shape (post shlex.split):
          ["llm_until", "model=<id>", "prompt=<...>",
           "max_iterations=<N>",
           "iterate_prompt=<...>"?, "converge_when_output_contains=<str>"?,
           "converge_when_output_unchanged=<bool>"?,
           "converge_when_below_confidence=<float>"?,
           "system=<...>"?, "field=<col>"?, "use_cache=<bool>"?,
           "max_tokens=<N>"?, "max_cost_usd=<F>"?, "dry_run=<bool>"?]

        Required: model + prompt + max_iterations. Each row runs up to
        max_iterations rounds of the same model; loop exits when any
        convergence condition fires OR max_iterations is hit.
        """
        from handlers.LLMHandler import llm_until_pipe, LLMPipeError

        kwargs: dict = {}
        for tok in seg_tokens[1:]:
            if "=" not in tok:
                raise RuntimeError(
                    f"llm_until unexpected token {tok!r}; expected key=value"
                )
            k, v = tok.split("=", 1)
            kwargs[k.strip().lower()] = v.strip()

        model = kwargs.get("model")
        if not model:
            raise RuntimeError(
                "llm_until requires model=<registry-id>"
            )
        prompt = kwargs.get("prompt")
        if not prompt:
            raise RuntimeError("llm_until requires prompt=<string>")

        max_iterations_raw = kwargs.get("max_iterations")
        if max_iterations_raw is None:
            raise RuntimeError(
                "llm_until requires max_iterations=<N> - operators MUST "
                "set the hard ceiling explicitly (no default)."
            )
        try:
            max_iterations = int(max_iterations_raw)
        except ValueError:
            raise RuntimeError(
                f"llm_until max_iterations must be an integer, got "
                f"{max_iterations_raw!r}"
            )

        iterate_prompt = kwargs.get("iterate_prompt") or None
        converge_when_output_contains = (
            kwargs.get("converge_when_output_contains") or None
        )

        converge_when_output_unchanged = False
        if "converge_when_output_unchanged" in kwargs:
            v = kwargs["converge_when_output_unchanged"].strip().lower()
            if v in ("true", "1", "yes"):
                converge_when_output_unchanged = True
            elif v in ("false", "0", "no"):
                converge_when_output_unchanged = False
            else:
                raise RuntimeError(
                    f"llm_until converge_when_output_unchanged must be "
                    f"true|false, got {v!r}"
                )

        converge_when_below_confidence = None
        if "converge_when_below_confidence" in kwargs:
            try:
                converge_when_below_confidence = float(
                    kwargs["converge_when_below_confidence"]
                )
            except ValueError:
                raise RuntimeError(
                    f"llm_until converge_when_below_confidence must be a "
                    f"number, got {kwargs['converge_when_below_confidence']!r}"
                )

        system = kwargs.get("system") or None
        field = kwargs.get("field") or None

        if "use_cache" in kwargs:
            v = kwargs["use_cache"].strip().lower()
            if v in ("true", "1", "yes"):
                use_cache = True
            elif v in ("false", "0", "no"):
                use_cache = False
            else:
                raise RuntimeError(
                    f"llm_until use_cache must be true|false, got {v!r}"
                )
        else:
            use_cache = True

        max_tokens = None
        if "max_tokens" in kwargs:
            try:
                max_tokens = int(kwargs["max_tokens"])
            except ValueError:
                raise RuntimeError(
                    f"llm_until max_tokens must be an integer, got "
                    f"{kwargs['max_tokens']!r}"
                )

        # ── Slice 7 contract - every billable pipe MUST honour ───────
        max_cost_usd = self._resolve_max_cost_kwarg(
            kwargs, pipe_label="llm_until",
        )
        dry_run = self._resolve_dry_run_kwarg(
            kwargs, pipe_label="llm_until",
        )

        try:
            return llm_until_pipe(
                self.main_df,
                model=model, prompt=prompt,
                max_iterations=max_iterations,
                iterate_prompt=iterate_prompt,
                converge_when_output_contains=converge_when_output_contains,
                converge_when_output_unchanged=converge_when_output_unchanged,
                converge_when_below_confidence=converge_when_below_confidence,
                system=system, field=field,
                use_cache=use_cache, max_tokens=max_tokens,
                max_cost_usd=max_cost_usd, dry_run=dry_run,
            )
        except LLMPipeError as exc:
            raise RuntimeError(f"llm_until: {exc}") from exc

    @staticmethod
    def _resolve_max_cost_kwarg(kwargs: dict, *, pipe_label: str):
        """Parse ``max_cost_usd=<float>`` from a flat kwargs dict.

        Returns ``None`` (uncapped) when the key is absent OR the
        value parses to a non-positive number (so ``max_cost_usd=0``
        is the documented "disable the cap" form). The handler also
        normalises this same way; doing it here keeps the listener +
        handler interpretations identical at the audit boundary.
        """
        if "max_cost_usd" not in kwargs:
            return None
        raw = kwargs["max_cost_usd"]
        try:
            v = float(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{pipe_label} max_cost_usd must be a number, got {raw!r}"
            ) from exc
        if v <= 0:
            return None
        return v

    @staticmethod
    def _resolve_dry_run_kwarg(kwargs: dict, *, pipe_label: str) -> bool:
        """Parse ``dry_run=<bool>`` from a flat kwargs dict. Default False."""
        if "dry_run" not in kwargs:
            return False
        v = kwargs["dry_run"].strip().lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no"):
            return False
        raise RuntimeError(
            f"{pipe_label} dry_run must be true|false, got {v!r}"
        )

    def _cmd_switch(self, seg_tokens, seg_str):
        """Conditional pipe-level branching - Phase 2 / Bet 3 slice 6.

        Routes each row through a sub-pipeline keyed by a column value.

        Syntax::

            | switch <column>
                case "value1" [ <subpipe1> ]
                case "value2" [ <subpipe2> ]
                case "*"      [ <catchall_subpipe> ]

        For each input row, the value of ``<column>`` selects which
        case's subpipe processes it. ``case "*"`` matches any value
        not explicitly cased. Rows whose value matches no case AND no
        catchall are dropped (with a log warning at INFO).

        Each case's subpipe receives only the matching rows as its
        input DataFrame. Outputs from all cases are concatenated
        (column union, NaN-filled for missing columns) into the
        switch's final output. Within each case the input order is
        preserved; across cases output is grouped by case order in
        the directive.
        """
        import re as _re_mod
        import pandas as _pd

        if len(seg_tokens) < 2:
            raise RuntimeError("switch requires a column name.")
        column = seg_tokens[1]
        if column not in self.main_df.columns:
            raise RuntimeError(
                f"switch: column {column!r} does not exist "
                f"(have: {list(self.main_df.columns)})"
            )

        # Extract all `case "VALUE" [SUBPIPE]` triples from the raw
        # directive string. Same constraint as | multisearch: subpipe
        # text cannot contain `]` literally - operators using literal
        # `]` inside a subpipe should escape via the SPQL-level
        # mechanism (or pre-process via | eval).
        pattern = re.compile(
            r'case\s+"([^"]+)"\s*\[([^\]]+)\]', re.DOTALL,
        )
        cases: list[tuple[str, str]] = pattern.findall(seg_str)
        if not cases:
            raise RuntimeError(
                "switch requires at least one `case \"value\" [subpipe]`"
            )

        # Bucket rows by which case they match. The wildcard "*" case
        # is a catchall for unmatched values. Rows that match no case
        # AND no catchall are dropped.
        case_values = [c for c, _ in cases]
        case_subpipes = {c: sub.strip() for c, sub in cases}

        # First pass: pick a case index for each row. -1 means dropped.
        col_values = self.main_df[column].astype(str).tolist()
        case_index_for_row: list[int] = []
        catchall_idx = case_values.index("*") if "*" in case_values else -1
        for v in col_values:
            try:
                case_index_for_row.append(case_values.index(v))
            except ValueError:
                case_index_for_row.append(catchall_idx)

        n_dropped = sum(1 for i in case_index_for_row if i == -1)
        if n_dropped:
            logger.info(
                "[i] switch: dropped %d/%d rows (no case + no catchall)",
                n_dropped, len(self.main_df),
            )

        # Second pass: dispatch each non-empty bucket through its subpipe.
        outputs: list = []
        for ci, value in enumerate(case_values):
            row_mask = [idx == ci for idx in case_index_for_row]
            bucket = self.main_df[row_mask]
            if len(bucket) == 0:
                continue
            saved = self.main_df
            self.main_df = bucket.reset_index(drop=True)
            try:
                sub_df = self._run_subsearch_pipeline(
                    case_subpipes[value],
                )
                if sub_df is not None and len(sub_df) > 0:
                    outputs.append(sub_df)
                logger.info(
                    "[i] switch: case %r processed %d rows",
                    value, len(bucket),
                )
            finally:
                self.main_df = saved

        if not outputs:
            return _pd.DataFrame()
        return _pd.concat(outputs, ignore_index=True, sort=False)

    def _cmd_dedup_semantic(self, seg_tokens, _):
        """Drop near-duplicate rows by semantic similarity.

        Token shape (post shlex.split):
          ["dedup_semantic", "threshold=F", "field=col"]

        Both kwargs are optional. Default threshold is 0.85.
        """
        from handlers.SemanticHandler import (
            dedup_semantic as _dedup_semantic, SemanticPipeError,
        )

        args = seg_tokens[1:]
        kwargs: dict = {}
        for tok in args:
            if "=" not in tok:
                raise RuntimeError(
                    f"dedup_semantic unexpected token {tok!r}; expected key=value"
                )
            k, v = tok.split("=", 1)
            kwargs[k.strip().lower()] = v.strip()
        threshold = float(kwargs.get("threshold", 0.85))
        field = kwargs.get("field")
        try:
            return _dedup_semantic(
                self.main_df, threshold=threshold, field=field,
            )
        except SemanticPipeError as exc:
            raise RuntimeError(f"dedup_semantic: {exc}") from exc

    def _cmd_join(self, seg_tokens, seg_str):
        """Join with a subsearch. seg_str includes join type and fields followed by [subsearch].
        Returns the joined DataFrame.
        """
        join_type = "inner"
        fields = []
        # Extract fields and join type from tokens before the bracket
        for tok in seg_tokens[1:]:
            if tok.startswith("[") or tok.endswith("]"):
                break
            if tok.startswith("type="):
                join_type = tok.split("=", 1)[1]
            else:
                fields.extend([x.strip(",") for x in tok.split(",") if x])
        # Extract and run subsearch from raw string
        raw_sub = self._extract_subsearch_raw(seg_str)
        if raw_sub is not None:
            sub_df = self._run_subsearch_pipeline(raw_sub)
            if sub_df is not None:
                return self.general_handler.execute_join(self.main_df, sub_df, fields, join_type)
        return self.main_df

    def _cmd_append(self, seg_tokens, seg_str):
        """Append the results of a subsearch to the main DataFrame.
        seg_tokens contains [subsearch] brackets. Returns the extended DataFrame.
        """
        # Extract raw subsearch content between [ and ]
        raw_sub = self._extract_subsearch_raw(seg_str)
        if raw_sub is None:
            return self.main_df
        add_df = self._run_subsearch_pipeline(raw_sub)
        if add_df is not None:
            return self.general_handler.execute_append(self.main_df, add_df)
        return self.main_df

    def _extract_subsearch_raw(self, seg_str: str):
        """Return the raw string between the first ``[`` and last ``]``."""
        start = seg_str.find("[")
        end = seg_str.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return None
        return seg_str[start + 1 : end].strip()

    def _run_subsearch_pipeline(self, raw_sub: str):
        """Execute a raw subsearch string as a full pipeline.

        Handles both generating commands (``makeresults``) and index calls
        followed by any number of transforming commands.
        """
        segments = self.split_pipeline(raw_sub)
        if not segments:
            return None

        first_seg = segments[0]
        try:
            first_tokens = shlex.split(first_seg)
        except ValueError:
            first_tokens = first_seg.split()
        first_cmd = first_tokens[0].split("(")[0].lower()

        # 2026-05-16: detect `search index="..."` as an INDEX CALL, not a
        # filter. Without this branch, the subsearch ``[search index="X"
        # | stats count by Y]`` falls through to the ``first_cmd in
        # _command_map`` arm below (because ``search`` IS in the map),
        # which sub-runs against a copy of the OUTER main_df instead of
        # loading the index. Result: subsearch returns empty rows, join
        # returns "No data returned" silently. Caught 2026-05-16 while
        # prototyping curator slice 2 against real Takeout data. Pinned
        # by the test in tests/test_subsearch_index_call.py.
        has_index_clause = any(
            isinstance(t, str) and t.lower().startswith("index=")
            for t in first_tokens
        )

        # Determine the initial DataFrame and which segments still need processing.
        if first_cmd == "makeresults":
            count = 1
            annotate = False
            for tok in first_tokens[1:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    if k.lower() == "count":
                        count = int(v)
                    elif k.lower() == "annotate":
                        annotate = v.lower() in ("true", "1")
            sub_df = self.general_handler.make_results(count=count, annotate=annotate)
            remaining = segments[1:]  # first segment consumed above
        elif first_cmd == "search" and has_index_clause:
            # ``search index="X" ...`` - strip the leading ``search``
            # token and treat the rest as an index call (matches the
            # top-level pipeline's implicit-search convention where
            # ``index="X"`` alone loads the index).
            sub_df = process_index_calls(first_tokens[1:])
            remaining = segments[1:]
        elif has_index_clause:
            # Bare ``index="X" ...`` form (Splunk-like). Treat as index call.
            sub_df = process_index_calls(first_tokens)
            remaining = segments[1:]
        elif first_cmd in self._command_map or first_cmd in ("if_", "case", "tonumber"):
            # Transforming command with no generating source - run against
            # a copy of main_df, and process ALL segments including this one.
            sub_df = self.main_df.copy()
            remaining = segments  # include the first segment
        else:
            # Assume index call
            sub_df = process_index_calls(first_tokens)
            remaining = segments[1:]  # first segment consumed above

        # Process pipeline segments
        saved_df = self.main_df
        self.main_df = sub_df
        for seg_raw in remaining:
            try:
                seg_toks = shlex.split(seg_raw)
            except ValueError:
                seg_toks = seg_raw.split()
            cmd = seg_toks[0].split("(")[0].lower()
            self.main_df = self._apply_command(cmd, seg_toks, seg_raw)
        sub_df = self.main_df
        self.main_df = saved_df
        return sub_df

    def _cmd_appendpipe(self, seg_tokens, seg_str):
        """Run a subsearch and append the results to ``self.main_df``.

        ``seg_str`` must contain the query in ``[subsearch]`` brackets.
        Returns the DataFrame with rows from the subsearch appended.
        """
        raw_sub = self._extract_subsearch_raw(seg_str)
        if raw_sub is None:
            return self.main_df
        add_df = self._run_subsearch_pipeline(raw_sub)
        if add_df is not None:
            return self.general_handler.execute_append(self.main_df, add_df)
        return self.main_df

    def _cmd_multisearch(self, seg_tokens, seg_str):
        """Execute multiple subsearches and combine results.
        seg_str contains one or more [subsearch] groups. Returns the combined DataFrame.
        """
        # Extract each [...] subsearch from the raw string
        import re as _re_mod
        subsearch_strs = _re_mod.findall(r'\[([^\]]+)\]', seg_str)
        dfs = []
        for sub_raw in subsearch_strs:
            sub_df = self._run_subsearch_pipeline(sub_raw.strip())
            if sub_df is not None and len(sub_df) > 0:
                dfs.append(sub_df)
        if not dfs:
            import pandas as _pd
            return _pd.DataFrame()
        import pandas as _pd
        return _pd.concat(dfs, ignore_index=True)

    def _cmd_lookup(self, seg_tokens, _):
        """Perform a lookup against a file. seg_tokens format: ["lookup", "file.csv", "key", "OUTPUT", ...].
        Returns the joined DataFrame.
        """
        filename = seg_tokens[1]
        key = seg_tokens[2]
        output_fields = [t.strip(",") for t in seg_tokens[4:]] if "OUTPUT" in seg_tokens else []

        root = Path(self.lookup_root).resolve()
        lookup_path = (root / filename).resolve()
        try:
            lookup_path.relative_to(root)
        except ValueError:
            logger.error(f"[x] Invalid lookup filename: {filename}")
            return self.main_df

        lookup_df = self.lookup_handler.load_data(str(lookup_path))
        if lookup_df is not None:
            self.main_df = self.general_handler.execute_join(self.main_df, lookup_df, [key], "left")
            if output_fields:
                self.main_df = self.general_handler.filter_df_columns(self.main_df, self.main_df.columns.tolist(), "+")
        return self.main_df

    def _cmd_outputlookup(self, seg_tokens, _):
        """Write the DataFrame to a lookup file. seg_tokens hold options and filename.
        Returns the original DataFrame.
        """
        args = self.general_handler.parse_outputlookup_args(seg_tokens[1:])
        root = Path(self.lookup_root).resolve()
        if isinstance(args, str):
            filename = args
        else:
            filename = args.get("filename", "output.csv")

        output_path = (root / filename).resolve()
        try:
            output_path.relative_to(root)
        except ValueError:
            logger.error(f"[x] Invalid lookup filename: {filename}")
            return self.main_df

        if isinstance(args, str):
            kwargs = {"filename": str(output_path)}
        else:
            args["filename"] = str(output_path)
            kwargs = args

        self.general_handler.execute_outputlookup(self.main_df, **kwargs)
        return self.main_df

    def _cmd_outputnew(self, seg_tokens, _):
        """Write output to a new lookup file. seg_tokens example: ["outputnew", "file.csv"].
        Returns the original DataFrame.
        """
        filename = seg_tokens[1].strip('"').strip("'")
        root = Path(self.lookup_root).resolve()
        output_path = (root / filename).resolve()
        try:
            output_path.relative_to(root)
        except ValueError:
            logger.error(f"[x] Invalid lookup filename: {filename}")
            return self.main_df

        self.general_handler.execute_outputnew(self.main_df, str(output_path))
        return self.main_df

    def _cmd_coalesce(self, seg_tokens, seg_str):
        """Coalesce multiple fields into the first non-null value.
        seg_tokens lists the fields or parentheses may hold them in seg_str. Returns the DataFrame with a new column.
        """
        if "(" in seg_str and ")" in seg_str:
            inside = seg_str[seg_str.find("(") + 1 : seg_str.rfind(")")]
            fields = [f.strip().strip(",") for f in inside.split(",")]
        else:
            fields = [t.strip(",") for t in seg_tokens[1:]]
        return self.general_handler.execute_coalesce(self.main_df, fields)

    def _cmd_mvexpand(self, seg_tokens, _):
        """Expand a multivalue field into multiple events. seg_tokens example: ["mvexpand", "field"].
        Returns the expanded DataFrame.
        """
        field = seg_tokens[1]
        return self.general_handler.execute_mvexpand(self.main_df, field)

    def _cmd_mvreverse(self, seg_tokens, _):
        """Reverse the order of values in a multivalue field. seg_tokens example: ["mvreverse", "field"].
        Returns the modified DataFrame.
        """
        field = seg_tokens[1]
        return self.general_handler.execute_mvreverse(self.main_df, field)

    def _cmd_mvcombine(self, seg_tokens, _):
        """Combine multivalue entries into a single delimited string.
        seg_tokens example: ["mvcombine", "field", "delim=,"]
        Returns the updated DataFrame.
        """
        field = None
        delim = " "
        for t in seg_tokens[1:]:
            if t.startswith("delim="):
                delim = t.split("=", 1)[1].strip('"')
            else:
                field = t.strip(",")
        if field:
            return self.general_handler.execute_mvcombine(self.main_df, field, delim)
        return self.main_df

    def _cmd_mvdedup(self, seg_tokens, _):
        """Remove duplicate values from a multivalue field. seg_tokens example: ["mvdedup", "field"].
        Returns the DataFrame with unique lists.
        """
        field = seg_tokens[1]
        return self.general_handler.execute_mvdedup(self.main_df, field)

    def _cmd_mvappend(self, seg_tokens, _):
        """Append multiple fields into a single multivalue field. seg_tokens example: ["mvappend", "foo", "bar"].
        Returns the DataFrame with appended list.
        """
        fields = [t.strip(",") for t in seg_tokens[1:]]
        return self.general_handler.execute_mvappend(self.main_df, fields, fields[0])

    def _cmd_mvfilter(self, seg_tokens, _):
        """Filter multivalue field values by expression. seg_tokens like ["mvfilter", "field", "value=foo"].
        Returns the filtered DataFrame.
        """
        field = seg_tokens[1]
        value = seg_tokens[2].split("=")[1] if "=" in seg_tokens[2] else seg_tokens[2]
        return self.general_handler.execute_mvfilter(self.main_df, field, value)

    def _cmd_mvcount(self, seg_tokens, _):
        """Count elements in a multivalue field. seg_tokens example: ["mvcount", "field"].
        Returns DataFrame with an added count column.
        """
        field = seg_tokens[1]
        return self.general_handler.execute_mvcount(self.main_df, field, f"{field}_count")

    def _cmd_mvdc(self, seg_tokens, _):
        """Count distinct elements in a multivalue field. seg_tokens like ["mvdc", "field"].
        Returns DataFrame with a distinct count column.
        """
        field = seg_tokens[1]
        return self.general_handler.execute_mvdc(self.main_df, field, f"{field}_dc")

    def _cmd_mvfind(self, seg_tokens, _):
        """Search within multivalue fields for a pattern. seg_tokens example: ["mvfind", "field", "foo"].
        Returns the DataFrame with matches extracted.
        """
        field = seg_tokens[1]
        pattern = seg_tokens[2] if len(seg_tokens) > 2 else ""
        return self.general_handler.execute_mvfind(self.main_df, field, pattern)

    def _cmd_mvzip(self, seg_tokens, _):
        """Zip two multivalue fields element-wise. seg_tokens example: ["mvzip", "f1", "f2", "_"]
        Returns DataFrame with a new zipped field.
        """
        field1 = seg_tokens[1].rstrip(",")
        field2 = seg_tokens[2].rstrip(",")
        delim = seg_tokens[3].strip('"') if len(seg_tokens) > 3 else "_"
        return self.general_handler.execute_mvzip(self.main_df, field1, field2, delim, "mvzip")

    def _cmd_mvjoin(self, seg_tokens, _):
        """Join multivalue elements with a delimiter. seg_tokens like ["mvjoin", "field", "delim=;"]
        Returns the DataFrame with joined values.
        """
        field = seg_tokens[1]
        delim = seg_tokens[2].split("=")[1] if len(seg_tokens) > 2 else " "
        return self.general_handler.execute_mvjoin(self.main_df, field, delim)

    def _cmd_mvindex(self, seg_tokens, _):
        """Extract specific indices from a multivalue field. seg_tokens example: ["mvindex", "field", "0", "1"].
        Returns DataFrame with indexed values.
        """
        field = seg_tokens[1]
        idxs = [int(i.strip(",")) for i in seg_tokens[2:]]
        return self.general_handler.execute_mvindex(self.main_df, field, idxs, "mvindex")

    def _cmd_spath(self, seg_tokens, _):
        """Extract JSON values using a spath expression. seg_tokens include key=value pairs such as input=field.
        Returns DataFrame with the extracted output column.
        """
        params = {}
        for tok in seg_tokens[1:]:
            if "=" in tok:
                key, val = tok.split("=", 1)
                params[key.lower()] = val
        input_col = params.get("input")
        output_col = params.get("output")
        json_path = params.get("path")
        if input_col and output_col and json_path:
            return self.general_handler.execute_spath(self.main_df, input_col, output_col, json_path)
        return self.main_df

    def _cmd_makeresults(self, seg_tokens, _):
        """Generate a result set. seg_tokens like ["makeresults"] or ["makeresults", "count=5", "annotate=true"].
        Returns a new DataFrame with _time column.
        """
        count = 1
        annotate = False
        for tok in seg_tokens[1:]:
            if "=" in tok:
                key, val = tok.split("=", 1)
                if key.lower() == "count":
                    count = int(val)
                elif key.lower() == "annotate":
                    annotate = val.lower() in ("true", "1")
        return self.general_handler.make_results(count=count, annotate=annotate)

    def _cmd_addinfo(self, seg_tokens, _):
        """Add informational fields about the search to each event.
        Returns the DataFrame with info_* columns added.
        """
        return self.general_handler.add_info(self.main_df, self.original_query)

    # Enter a parse tree produced by speakesQueryParser#initialSequence.
    def enterInitialSequence(self, ctx: speakesQueryParser.InitialSequenceContext):
        self.initial_sequence_enabled = True
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#initialSequence.
    def exitInitialSequence(self, ctx: speakesQueryParser.InitialSequenceContext):
        self.initial_sequence_enabled = False
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#expression.
    def exitExpression(self, ctx: speakesQueryParser.ExpressionContext):
        current_parsed_index_call = ctx_flatten(
            ctx, self.extract_screenshot_of_ctx
        )
        current_index_call = "".join(current_parsed_index_call).replace(" ", "")
        if current_index_call == self.original_index_call:
            # Use the dynamically loaded process_index_calls function.
            # NOTE: This is one of two index-load call sites (the other
            # is ``exitSpeakesQuery``). The idempotency flag on
            # ``exitSpeakesQuery`` collapses its own duplicate, but this
            # site remains - some complex queries (eval-before-search,
            # eventstats-then-where, search→eval→search) rely on
            # exitExpression producing the canonical base DataFrame via
            # its own expression-context token flatten. Do NOT gate this
            # on ``main_df is None`` - that regresses 3 tier-3 tests.
            self.main_df = process_index_calls(current_parsed_index_call)
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#conjunction.
    def exitConjunction(self, ctx: speakesQueryParser.ConjunctionContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#comparison.
    def exitComparison(self, ctx: speakesQueryParser.ComparisonContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#additiveExpr.
    def exitAdditiveExpr(self, ctx: speakesQueryParser.AdditiveExprContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#multiplicativeExpr.
    def exitMultiplicativeExpr(self, ctx: speakesQueryParser.MultiplicativeExprContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#unaryExpr.
    def exitUnaryExpr(self, ctx: speakesQueryParser.UnaryExprContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#primary.
    def exitPrimary(self, ctx: speakesQueryParser.PrimaryContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#timeClause.
    def exitTimeClause(self, ctx: speakesQueryParser.TimeClauseContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#earliestClause.
    def exitEarliestClause(self, ctx: speakesQueryParser.EarliestClauseContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#latestClause.
    def exitLatestClause(self, ctx: speakesQueryParser.LatestClauseContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#indexClause.
    def exitIndexClause(self, ctx: speakesQueryParser.IndexClauseContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#comparisonOperator.
    def exitComparisonOperator(self, ctx: speakesQueryParser.ComparisonOperatorContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#inExpression.
    def exitInExpression(self, ctx: speakesQueryParser.InExpressionContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#inputlookupInit.
    def exitInputlookupInit(self, ctx: speakesQueryParser.InputlookupInitContext):
        self.current_inputlookup_call = ctx_flatten(
            ctx, self.extract_screenshot_of_ctx
        )
        self.current_inputlookup_filename = (
            self.current_inputlookup_call[-1].strip().strip('"')
        )
        self.current_inputlookup_path = (
            f"{self.lookup_root}/{self.current_inputlookup_filename}"
        )
        self.main_df = self.lookup_handler.load_data(f"{self.current_inputlookup_path}")
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#loadjobInit.
    def exitLoadjobInit(self, ctx: speakesQueryParser.LoadjobInitContext):
        self.current_loadjob_call = ctx_flatten(
            ctx, self.extract_screenshot_of_ctx
        )
        self.current_loadjob_filename = (
            self.current_loadjob_call[-1].strip("'").strip().strip('"')
        )
        self.main_df = self.general_handler.load_job(
            self.current_loadjob_filename
        )
        self.main_df = self.general_handler.add_loadjob_metadata(
            self.main_df, self.current_loadjob_filename
        )
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#makeresultsInit.
    def exitMakeresultsInit(self, ctx: speakesQueryParser.MakeresultsInitContext):
        tokens = ctx_flatten(ctx, self.extract_screenshot_of_ctx)
        # Parse count= and annotate= from tokens
        count = 1
        annotate = False
        i = 0
        while i < len(tokens):
            tok = tokens[i].lower()
            if tok == "count" and i + 2 < len(tokens) and tokens[i + 1] == "=":
                count = int(tokens[i + 2])
                i += 3
            elif tok == "annotate" and i + 2 < len(tokens) and tokens[i + 1] == "=":
                annotate = tokens[i + 2].lower() in ("true", "1")
                i += 3
            else:
                i += 1
        self.main_df = self.general_handler.make_results(count=count, annotate=annotate)
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#validLine.
    def exitValidLine(self, ctx: speakesQueryParser.ValidLineContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#directive.
    def exitDirective(self, ctx: speakesQueryParser.DirectiveContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#macro.
    def exitMacro(self, ctx: speakesQueryParser.MacroContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#statsAgg.
    def exitStatsAgg(self, ctx: speakesQueryParser.StatsAggContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#variableList.
    def exitVariableList(self, ctx: speakesQueryParser.VariableListContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#subsearch.
    def exitSubsearch(self, ctx: speakesQueryParser.SubsearchContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#functionCall.
    def exitFunctionCall(self, ctx: speakesQueryParser.FunctionCallContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#numericFunctionCall.
    def exitNumericFunctionCall(self, ctx: speakesQueryParser.NumericFunctionCallContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#stringFunctionCall.
    def exitStringFunctionCall(self, ctx: speakesQueryParser.StringFunctionCallContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#specificFunctionCall.
    def exitSpecificFunctionCall(
        self, ctx: speakesQueryParser.SpecificFunctionCallContext
    ):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#statsFunctionCall.
    def exitStatsFunctionCall(self, ctx: speakesQueryParser.StatsFunctionCallContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#regexTarget.
    def exitRegexTarget(self, ctx: speakesQueryParser.RegexTargetContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#mvfindObject.
    def exitMvfindObject(self, ctx: speakesQueryParser.MvfindObjectContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#mvindexIndex.
    def exitMvindexIndex(self, ctx: speakesQueryParser.MvindexIndexContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#mvDelim.
    def exitMvDelim(self, ctx: speakesQueryParser.MvDelimContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#timespan.
    def exitTimespan(self, ctx: speakesQueryParser.TimespanContext):
        self.validate_exceptions(ctx)

    # Exit a parse tree produced by speakesQueryParser#variableName.
    def exitVariableName(self, ctx: speakesQueryParser.VariableNameContext):
        self.validate_exceptions(ctx)

    # **************************************************************************************************************
    # Custom Functions
    # **************************************************************************************************************
    # CRITICAL COMPONENT
    @staticmethod
    def generic_processing_exit(obj_failure, err_msg):
        """
        Logs an error message and raises a RuntimeError to allow for graceful error handling.

        Args:
            obj_failure (str): Identifier for where the failure occurred.
            err_msg (str): Detailed error message.

        Raises:
            RuntimeError: With the provided error message.
        """
        logging.error(f'[x] Failure at "{obj_failure}". {err_msg}')
        raise RuntimeError(f'Failure at "{obj_failure}". {err_msg}')


    def run_subsearch(self, tokens, df):
        """Execute a list of tokens as a pipeline against df."""
        segments = []
        current = []
        for tok in tokens:
            if tok == "|":
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(tok)
        if current:
            segments.append(current)

        result_df = df.copy()
        for seg in segments:
            if not seg:
                continue
            cmd = seg[0].split("(")[0].lower()
            if cmd in ("stats", "eventstats", "streamstats"):
                result_df = self.stats_handler.run_stats(seg, result_df)
            elif cmd == "eval":
                from handlers.EvalHandler import EvalHandler

                eval_handler = EvalHandler()
                result_df = eval_handler.run_eval(seg, result_df)
            elif cmd in ("head", "limit"):
                count = int(seg[1]) if len(seg) > 1 else 5
                result_df = self.general_handler.head_call(result_df, count, "head")
            elif cmd == "fields":
                mode = "+"
                cols = []
                for t in seg[1:]:
                    if t.startswith("-"):
                        mode = "-"
                        cols.append(t[1:].strip(","))
                    else:
                        cols.append(t.strip(","))
                result_df = self.general_handler.filter_df_columns(
                    result_df, cols, mode
                )
        return result_df

    # CRITICAL COMPONENT
    def extract_screenshot_of_ctx(self, ctx):
        """
        Recursively processes the context tree and generates a list representing
        all terminal nodes, without handling parentheses nesting.
        """
        # tokens_to_skip = {'\n', '\r', '\t', ' ', '', ','}  # Removing original for now, but if errors, I will return.
        tokens_to_skip = {"\n", "\r", "\t", " ", ""}

        if ctx is None:
            return None

        # Base case: If the context is a terminal node, return its text.
        if isinstance(ctx, TerminalNodeImpl):
            text = ctx.getText()
            if text.strip() in tokens_to_skip:
                return None  # Skip empty or unwanted tokens.
            else:
                return text

        children_results = []  # List to hold the final results.
        if hasattr(ctx, "children") and ctx.children:
            idx = 0
            while idx < len(ctx.children):
                child = ctx.children[idx]
                child_result = self.extract_screenshot_of_ctx(
                    child
                )  # Process each child recursively.
                if child_result is not None:
                    children_results.append(child_result)
                idx += 1

            # Remove None values and flatten the results.
            children_results = [
                child for child in children_results if child is not None
            ]
            return flatten_list(children_results)
        else:
            return None

    # CRITICAL COMPONENT
    def validate_exceptions(self, ctx_obj):
        """
        Validates exceptions by checking the current context and handling errors gracefully.
        """
        obj_identifier = inspect.currentframe().f_back.f_code.co_name
        if not obj_identifier or not isinstance(obj_identifier, str):
            self.generic_processing_exit(
                "validate_exceptions", "General Syntax Failure."
            )

        if obj_identifier == "exitDirective":
            directive = str(ctx_obj.children[0]).lower()
            if directive in ("search", "where"):
                self.current_search_cmd_tokens = ctx_flatten(
                    ctx_obj, self.extract_screenshot_of_ctx
                )[1:]
                self.main_df = self.search_cmd_handler.run_search(
                    self.current_search_cmd_tokens, self.main_df
                )

    @staticmethod
    def split_pipeline(raw: str) -> list:
        """Split a raw pipeline string on ``|`` while respecting ``[...]``
        bracket nesting AND quoted strings.

        Pipes inside subsearch brackets (``[ ... | ... ]``) are kept intact
        so commands like ``append`` and ``join`` receive the full subsearch
        text. Pipes inside double-quoted (``"..."``) or single-quoted
        (``'...'``) strings are also kept intact - required for ``match()``
        regex alternation like ``match(text, "a|b|c")`` and for any string
        literal that legitimately contains a ``|``. Caught 2026-05-05 when
        4 SS YAMLs (pppb_kalshi_*, spbeb_kalshi_sports, pppb_congress_bills)
        had local edits with regex alternation that silently failed at
        runtime with ``ValueError: No closing quotation`` because the
        quote-unaware splitter cut the string in half before
        ``shlex.split`` ever saw it.

        Backslash inside ``"..."`` escapes the next character (per the
        ANTLR ``DOUBLE_QUOTED_STRING`` rule). Single-quoted strings have no
        escape - the rule is ``~['\\r\\n]*``.

        Returns a list of stripped, non-empty segment strings.
        """
        segments = []
        current: list[str] = []
        depth = 0
        in_double = False
        in_single = False
        escape_next = False
        for ch in raw:
            if escape_next:
                # Inside a "..." string, a backslash consumed the next char
                # as a literal. Append it and clear the flag.
                current.append(ch)
                escape_next = False
                continue
            if in_double:
                current.append(ch)
                if ch == "\\":
                    escape_next = True
                elif ch == '"':
                    in_double = False
                continue
            if in_single:
                current.append(ch)
                if ch == "'":
                    in_single = False
                continue
            # Outside any string
            if ch == '"':
                in_double = True
                current.append(ch)
            elif ch == "'":
                in_single = True
                current.append(ch)
            elif ch == "[":
                depth += 1
                current.append(ch)
            elif ch == "]":
                depth -= 1
                current.append(ch)
            elif ch == "|" and depth == 0:
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
            else:
                current.append(ch)
        # Flush last segment
        seg = "".join(current).strip()
        if seg:
            segments.append(seg)
        return segments

    @staticmethod
    def normalize_tokens(token_list):
        """Return a cleaned token list with collapsed parentheses.

        Tokens are first stripped of surrounding whitespace and any spaces
        between an identifier and the opening parenthesis are removed.  Adjacent
        tokens representing a function call (e.g. ``['values', '(', 'a', ')']``)
        are then collapsed into a single token (``'values(a)'``).
        """

        # Initial whitespace/spacing cleanup
        cleaned = []
        for tok in token_list:
            tok = tok.strip()
            tok = re.sub(r"([a-zA-Z_][a-zA-Z_0-9]*)\s*\(", r"\1(", tok)
            cleaned.append(tok)

        normalized = []
        i = 0
        while i < len(cleaned):
            tok = cleaned[i]
            # Detect pattern identifier '(' ... ')' and collapse
            if (i + 1 < len(cleaned)) and cleaned[i + 1] == "(":
                depth = 1
                j = i + 2
                inner = []
                while j < len(cleaned) and depth > 0:
                    t = cleaned[j]
                    if t == "(":
                        depth += 1
                    elif t == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    inner.append(t)
                    j += 1
                if depth == 0 and j < len(cleaned) and cleaned[j] == ")":
                    normalized.append(f"{tok}({''.join(inner)})")
                    i = j + 1
                    continue
            normalized.append(tok)
            i += 1

        return normalized


# Retain parser reference for isinstance checks

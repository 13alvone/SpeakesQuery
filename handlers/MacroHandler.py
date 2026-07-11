#!/usr/bin/env python3
"""
MacroHandler.py
───────────────
SPQL macro expansion engine.

Macros are pure text substitution: backtick-delimited macro calls in the raw
query string are replaced with their definitions before ANTLR4 parsing.

Supports:
  - Parameterised macros: `my_macro(arg1, "arg 2")`
  - Parameterless macros: `my_shortcut`
  - Nested macros (macro definitions can invoke other macros)
  - Cycle detection and max-depth limits
"""

import logging
import re
import csv
import io
from typing import List, Optional

logger = logging.getLogger(__name__)

# Regex to find backtick-delimited macro calls in query text
# Matches: `macro_name` or `macro_name(arg1, arg2, "arg 3")`
_MACRO_CALL_RE = re.compile(r'`(\w+)(?:\(([^`]*)\))?`')

# Regex to match triple-backtick annotation comment lines inserted by
# expand_annotated().  These are purely visual - they must be stripped
# before query execution.
_ANNOTATION_LINE_RE = re.compile(r'^\s*```[^`]*```\s*$', re.MULTILINE)


class MacroHandler:
    """
    Pure text-substitution macro expansion engine.

    Takes a raw query string containing backtick-delimited macro calls and
    returns the expanded query string with all macro calls replaced by their
    definitions. Parameters in definitions are substituted using $param$
    placeholders.

    This handler does NOT operate on DataFrames. It sits upstream of the
    parser: expand first, then parse.
    """

    def __init__(self, macro_store, max_depth: int = 10):
        """
        Args:
            macro_store: A MacroStore instance for looking up macro definitions.
                         Must implement ``get_macro(name)`` which returns a dict
                         with keys ``"definition"`` (str) and optionally
                         ``"parameters"`` (list of str), or raises
                         ``FileNotFoundError`` if the macro does not exist.
            max_depth:   Maximum recursion depth for nested macro expansion.
        """
        self._store = macro_store
        self._max_depth = max_depth

    # ── public API ──────────────────────────────────────────────────────

    @staticmethod
    def strip_annotations(query: str) -> str:
        """
        Remove triple-backtick annotation comment lines from a query.

        These lines are inserted by :meth:`expand_annotated` for visual
        feedback in the UI.  They must be stripped before the query is
        passed to the parser/execution engine.

        Returns:
            The query with annotation lines removed and excess blank lines
            collapsed.
        """
        stripped = _ANNOTATION_LINE_RE.sub('', query)
        # Collapse runs of blank lines left behind by removal
        stripped = re.sub(r'\n{3,}', '\n\n', stripped)
        return stripped.strip()

    def expand(self, query: str) -> str:
        """
        Expand all macro calls in the query string.

        Repeatedly scans for backtick-delimited macro calls and replaces them
        with their definitions (with parameters substituted).  Continues until
        no more macro calls are found or max_depth is reached.

        Args:
            query: The raw query string potentially containing macro calls.

        Returns:
            The fully expanded query string.

        Raises:
            ValueError:    If a circular macro reference is detected.
            RecursionError: If max_depth is exceeded.
        """
        return self._expand_recursive(query, depth=0, chain=set())

    # ── internal recursion ──────────────────────────────────────────────

    def _expand_recursive(self, query: str, depth: int, chain: set) -> str:
        """
        Inner recursive expansion with cycle detection.

        Args:
            query: Current query text to scan for macro calls.
            depth: Current recursion depth (0-based).
            chain: Set of macro names currently being expanded in the call
                   stack.  Used to detect circular references.

        Returns:
            The query string with one level of macro calls expanded.
        """
        if depth > self._max_depth:
            raise RecursionError(
                f"Macro expansion exceeded maximum depth of {self._max_depth}. "
                f"Expansion chain: {' -> '.join(chain)}"
            )

        # Find all macro calls in current query
        matches = list(_MACRO_CALL_RE.finditer(query))
        if not matches:
            return query  # No more macros to expand

        # Process matches in reverse order to preserve string positions
        expanded = query
        for match in reversed(matches):
            name = match.group(1)
            arg_str = match.group(2)  # None if no parens

            # Look up macro definition
            try:
                macro = self._store.get_macro(name)
            except FileNotFoundError:
                # Not a known macro -- leave the backtick expression as-is
                # (it might be handled elsewhere or be intentional)
                logger.debug("Macro '%s' not found in store, skipping", name)
                continue

            # Cycle detection: if this macro is already being expanded
            # somewhere up the call stack, we have a circular reference.
            if name in chain:
                raise ValueError(
                    f"Circular macro reference detected: "
                    f"{' -> '.join(chain)} -> {name}"
                )

            # Parse arguments from the call site
            args = self._parse_args(arg_str) if arg_str is not None else []

            # Validate argument count against the macro's declared parameters
            expected_params = macro.get("parameters", [])
            if len(args) != len(expected_params):
                raise ValueError(
                    f"Macro '{name}' expects {len(expected_params)} argument(s) "
                    f"({', '.join(expected_params)}) but received {len(args)}: "
                    f"{args}"
                )

            # Substitute $param$ placeholders in the definition body
            definition = macro["definition"]
            for param, value in zip(expected_params, args):
                definition = definition.replace(f"${param}$", value)

            logger.info(
                "Expanded macro '%s': %s -> %s",
                name, match.group(0), definition,
            )

            # Splice the expanded definition into the query, replacing the
            # backtick-delimited call.
            expanded = expanded[:match.start()] + definition + expanded[match.end():]

        # If any substitutions were made, recurse to handle nested macros
        # that may have been introduced by the expansion.
        if expanded != query:
            # Add every macro we actually expanded at this level to the chain
            # so that the next level can detect cycles.
            new_chain = chain | {
                m.group(1)
                for m in matches
                if self._macro_exists(m.group(1))
            }
            return self._expand_recursive(expanded, depth + 1, new_chain)

        return expanded

    # ── annotated expansion ─────────────────────────────────────────────

    def expand_annotated(self, query: str, target_depth: int = 0,
                         max_depth: int = 100) -> str:
        """
        Expand macros with inline annotation comments.

        Each expanded macro call is wrapped in triple-backtick comment
        lines that identify which macro was expanded and where the
        expansion ends::

            ```[+] Expanded: my_macro```
            <expanded definition here>
            ```my_macro END```

        These annotation lines are purely visual - they are stripped
        automatically before execution by :meth:`strip_annotations`.

        Args:
            query:        The raw query string with macro calls.
            target_depth: How many levels of nesting to expand.
                          ``0`` means expand ALL levels (up to *max_depth*).
                          ``1`` expands only top-level macros.
                          ``N`` expands N levels deep.
            max_depth:    Hard ceiling on expansion depth (default 100).

        Returns:
            The query with macros expanded and wrapped in annotation
            comments.  Unexpanded macro calls (beyond *target_depth*)
            remain as backtick expressions.
        """
        effective_limit = max_depth if target_depth == 0 else min(target_depth, max_depth)
        result = self._expand_annotated_recursive(
            query, depth=0, target=effective_limit, chain=set(),
        )
        # Clean up leading/trailing whitespace introduced by newline
        # padding around annotation blocks.
        return result.strip()

    def _expand_annotated_recursive(self, query: str, depth: int,
                                     target: int, chain: set) -> str:
        """
        Inner recursive annotated expansion with cycle detection.

        Works like :meth:`_expand_recursive` but wraps each substitution
        in triple-backtick annotation comment lines.
        """
        if depth >= target:
            return query  # Reached the requested expansion ceiling

        matches = list(_MACRO_CALL_RE.finditer(query))
        if not matches:
            return query  # No more macros to expand

        expanded = query
        for match in reversed(matches):
            name = match.group(1)
            arg_str = match.group(2)  # None if no parens
            call_text = match.group(0)  # Full backtick expression

            # Look up macro definition
            try:
                macro = self._store.get_macro(name)
            except FileNotFoundError:
                logger.debug("Macro '%s' not found in store, skipping", name)
                continue

            # Cycle detection
            if name in chain:
                raise ValueError(
                    f"Circular macro reference detected: "
                    f"{' -> '.join(chain)} -> {name}"
                )

            # Parse and validate arguments
            args = self._parse_args(arg_str) if arg_str is not None else []
            expected_params = macro.get("parameters", [])
            if len(args) != len(expected_params):
                raise ValueError(
                    f"Macro '{name}' expects {len(expected_params)} argument(s) "
                    f"({', '.join(expected_params)}) but received {len(args)}: "
                    f"{args}"
                )

            # Substitute $param$ placeholders
            definition = macro["definition"]
            for param, value in zip(expected_params, args):
                definition = definition.replace(f"${param}$", value)

            # Build the label (macro call without outer backticks)
            label = call_text[1:-1]

            # Wrap the expanded definition in annotation comments.
            # Each annotation line is on its own line to ensure
            # strip_annotations() can reliably remove them later.
            annotated = (
                f"\n```[+] Expanded: {label}```\n"
                f"{definition}\n"
                f"```{label} END```\n"
            )

            logger.info(
                "Annotated expansion of macro '%s' at depth %d",
                name, depth + 1,
            )

            expanded = expanded[:match.start()] + annotated + expanded[match.end():]

        # Recurse for nested macros introduced by this expansion
        if expanded != query:
            new_chain = chain | {
                m.group(1)
                for m in matches
                if self._macro_exists(m.group(1))
            }
            return self._expand_annotated_recursive(
                expanded, depth + 1, target, new_chain,
            )

        return expanded

    # ── helpers ──────────────────────────────────────────────────────────

    def _macro_exists(self, name: str) -> bool:
        """Return True if *name* is a known macro in the store."""
        try:
            self._store.get_macro(name)
            return True
        except FileNotFoundError:
            return False

    @staticmethod
    def _parse_args(arg_str: str) -> List[str]:
        """
        Parse a comma-separated argument string, respecting quoted values.

        Uses :mod:`csv` to correctly handle quoted strings that contain commas.

        Examples::

            "400, 500"            -> ["400", "500"]
            '"hello world", 42'   -> ["hello world", "42"]
            'foo, "bar,baz"'      -> ["foo", "bar,baz"]
            ""                    -> []

        Args:
            arg_str: The raw argument text between parentheses.

        Returns:
            A list of stripped argument values.
        """
        if not arg_str or not arg_str.strip():
            return []

        # csv.reader handles quoting, escaping, and embedded commas correctly.
        reader = csv.reader(io.StringIO(arg_str.strip()), skipinitialspace=True)
        try:
            row = next(reader)
            return [v.strip() for v in row]
        except StopIteration:
            return []

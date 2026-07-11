#!/usr/bin/env python3

"""
search_directive.py

A class-based Python module that mirrors the logical and comparison
filtering from the C++ index call code, but applies it to an already-loaded
Pandas DataFrame. The filtering is guided by a list of tokens
(e.g., ['status', '=', '"success"', 'x', '>', '5', 'earliest', '=', '"2024-01-07"']).

Usage in your app.py or similar:
    from search_directive import SearchDirective
    import pandas as pd

    sd = SearchDirective()
    df = pd.read_csv("some_data.csv")  # or however you load your data
    tokens = ['level', '=', '"ERROR"', ...]  # e.g., from a query
    filtered_df = sd.run_search(tokens, df)

    # filtered_df is now your reduced DataFrame
"""

import logging
import re
import pandas as pd
from typing import List, Optional

logger = logging.getLogger(__name__)

# Integer or decimal literal (optional leading minus for explicit negatives).
# The tokenizer regex in speakesQueryListener._cmd_search only emits unsigned
# forms - but we keep the minus-case here for defence in depth in case a
# caller pre-tokenises with signs.
_NUMBER_LITERAL_RE = re.compile(r'-?\d+(?:\.\d+)?')


class SearchDirective:
    """
    Encapsulates logic for tokenizing, parsing, and applying a "search" directive
    against an existing Pandas DataFrame.
    """

    class TokenType:
        IDENTIFIER = "IDENTIFIER"
        STRING_LITERAL = "STRING_LITERAL"
        NUMBER_LITERAL = "NUMBER_LITERAL"
        OPERATOR = "OPERATOR"
        PARENTHESIS = "PARENTHESIS"
        COMMA = "COMMA"
        LITERAL = "LITERAL"

    class ASTNodeType:
        COMPARISON = "COMPARISON"
        LOGICAL_OP = "LOGICAL_OP"
        IN_CLAUSE = "IN_CLAUSE"
        IDENTIFIER = "IDENTIFIER"
        FUNCTION_CALL = "FUNCTION_CALL"
        LITERAL = "LITERAL"

    class Token:
        def __init__(self, token_type: str, value: str):
            self.type = token_type
            self.value = value

    class ASTNode:
        def __init__(self, node_type: str):
            self.node_type = node_type
            self.operator_: Optional[str] = None
            self.identifier: Optional[str] = None
            self.values: List[str] = []
            self.left: Optional['SearchDirective.ASTNode'] = None
            self.right: Optional['SearchDirective.ASTNode'] = None
            self.literal_or_ident: Optional[str] = None

    # ------------------------------------------------------------------
    # Public method to call for applying the "search" directive
    # ------------------------------------------------------------------
    def run_search(self, search_tokens: List[str], df: pd.DataFrame) -> pd.DataFrame:
        """
        Given a list of token strings (e.g. ['status', '=', '"success"', 'x', '>', '5']),
        build an AST, convert it to a Pandas query, and apply it to 'df'.

        Returns a filtered DataFrame or empty DataFrame if errors occur or if nothing matches.
        """

        if not search_tokens:
            logging.info("[i] No search tokens provided. Returning the original DataFrame.")
            return df

        logging.info(f"[i] Received search tokens: {search_tokens}")

        # Short-circuit on empty input: an empty DataFrame filtered by
        # anything is still empty, and evaluating the query string against
        # missing columns (common when an ingestion legitimately produced
        # zero rows and its parquet landed with only ``_epoch``) raises
        # ``UndefinedVariableError``/``NameError``. Caught 2026-04-21 when
        # ``ag_kalshi_poly_arb`` emitted an empty DataFrame (no arb
        # opportunities today) and every downstream ``| where`` crashed.
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            logging.info("[i] where: input DataFrame is empty; returning it unchanged.")
            return df if df is not None else pd.DataFrame()

        try:
            # 1) Convert raw string tokens -> Token objects
            token_list = self.tokenize_query_tokens(search_tokens)
            if not token_list:
                logging.info("[i] Could not produce any tokens. Returning original DataFrame.")
                return df

            # 2) Parse tokens -> AST
            parser = self.Parser(token_list)
            ast_root = parser.parse_expression()

            # 3) Convert AST -> Pandas query string
            pandas_query_str = self.ast_to_query(ast_root)
            if not pandas_query_str:
                pandas_query_str = "True"

            # Intermediate pipe diagnostics - DEBUG level so they don't
            # flood INFO logs during AG dispatches (10 feeders × 5 pipes
            # × 2 messages/pipe = 100 INFO lines per dispatch otherwise).
            # Visible with `--log-level=DEBUG` when troubleshooting.
            logging.debug(f"[DEBUG] Generated Pandas query: {pandas_query_str}")

            # 4) Apply the filter
            if pandas_query_str == "True":
                logging.debug("[DEBUG] Pandas query is 'True'; no filtering needed.")
                return df

            try:
                filtered_df = df.query(pandas_query_str)
                logging.debug(
                    f"[DEBUG] DataFrame filtered. Rows before: {len(df.index)}; after: {len(filtered_df.index)}.",
                )
                return filtered_df
            except Exception as ex:
                # NOT necessarily an error: a `where` on an eval- or
                # eventstats-derived column lands here on the engine's
                # FIRST pass (the column doesn't exist yet); the second
                # pass - after the deriving pipe has run - applies the
                # filter correctly and the final result is unaffected.
                # A real failure (typo'd column on a populated frame)
                # takes the same path, so keep it visible at WARNING -
                # but the old ERROR level made every AG feeder using a
                # derived-column filter look broken in production logs.
                logging.warning(
                    f"[!] where/search filter not applicable on this pass "
                    f"({ex}); returning empty for this pass. If the column "
                    f"is defined by a later eval/eventstats pipe, the "
                    f"final result is unaffected."
                )
                return pd.DataFrame()

        except Exception as e:
            logging.error(f"[x] Exception in run_search: {str(e)}")
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # Tokenizer
    # ------------------------------------------------------------------
    def tokenize_query_tokens(self, raw_tokens: List[str]) -> List['SearchDirective.Token']:
        """
        Convert raw token strings into Token objects. This parallels the
        C++ 'tokenize_query_tokens' function.

        The `==` form is normalised to `=` here so the rest of the parser
        and the AST→pandas-query mapping only deal with one equality token.
        Caught 2026-05-05: `where x == 1` silently returned 0 rows because
        the upstream tokenizer split `==` into two `=` tokens that the
        parser couldn't make sense of. See `reference_spql_where_clause_quirks.md`.
        """
        tokens: List[SearchDirective.Token] = []
        for rt in raw_tokens:
            if rt in ["(", ")"]:
                tokens.append(self.Token(self.TokenType.PARENTHESIS, rt))
            elif rt == "==":
                # Normalise `==` to `=` so SPQL `where x = 1` and
                # `where x == 1` both work. The convention in `where`
                # clauses is single `=`, but the alternative form is
                # idiomatic for many SQL/Python authors.
                tokens.append(self.Token(self.TokenType.OPERATOR, "="))
            elif rt.upper() in ["=", "!=", "<", ">", "<=", ">=", "AND", "OR", "IN"]:
                # Convert operators to uppercase
                tokens.append(self.Token(self.TokenType.OPERATOR, rt.upper()))
            elif rt == ",":
                tokens.append(self.Token(self.TokenType.COMMA, rt))
            elif rt in ["True", "False"]:
                tokens.append(self.Token(self.TokenType.LITERAL, rt))
            else:
                # Distinguish between string-literal, number-literal, and identifier
                if len(rt) >= 2 and rt.startswith('"') and rt.endswith('"'):
                    # It's a quoted string literal
                    trimmed = rt[1:-1]
                    tokens.append(self.Token(self.TokenType.STRING_LITERAL, trimmed))
                elif _NUMBER_LITERAL_RE.fullmatch(rt):
                    # numeric literal - int (``200``) or decimal (``0.75``)
                    tokens.append(self.Token(self.TokenType.NUMBER_LITERAL, rt))
                else:
                    # otherwise treat as identifier
                    tokens.append(self.Token(self.TokenType.IDENTIFIER, rt))

        return tokens

    # ------------------------------------------------------------------
    # Parser (similar to the C++ Parser)
    # ------------------------------------------------------------------
    class Parser:
        def __init__(self, tokens: List['SearchDirective.Token']):
            self.tokens = tokens
            self.index = 0

        def parse_expression(self) -> 'SearchDirective.ASTNode':
            return self.parse_or()

        def parse_or(self) -> 'SearchDirective.ASTNode':
            left_node = self.parse_and()
            while self.peek_operator("OR"):
                self.get_next_token()  # consume "OR"
                right_node = self.parse_and()
                new_node = SearchDirective.ASTNode(SearchDirective.ASTNodeType.LOGICAL_OP)
                new_node.operator_ = "|"
                new_node.left = left_node
                new_node.right = right_node
                left_node = new_node
            return left_node

        def parse_and(self) -> 'SearchDirective.ASTNode':
            left_node = self.parse_comparison()
            while True:
                if self.peek_operator("AND"):
                    self.get_next_token()  # consume "AND"
                    right_node = self.parse_comparison()
                    new_node = SearchDirective.ASTNode(SearchDirective.ASTNodeType.LOGICAL_OP)
                    new_node.operator_ = "&"
                    new_node.left = left_node
                    new_node.right = right_node
                    left_node = new_node
                elif self.peek_implicit_and():
                    right_node = self.parse_comparison()
                    new_node = SearchDirective.ASTNode(SearchDirective.ASTNodeType.LOGICAL_OP)
                    new_node.operator_ = "&"
                    new_node.left = left_node
                    new_node.right = right_node
                    left_node = new_node
                else:
                    break
            return left_node

        # 1-arg function names recognised in search/where context.
        # NOTE - `isnum`, `isint`, `isstr` were removed 2026-05-06 after a
        # full audit found they had never worked end-to-end:
        #   * Not present in the ANTLR grammar (lexers/speakesQuery.g4)
        #   * Not in the EvalHandler's allowlist (raises
        #     "Function 'isnum' is not allowed")
        #   * The where-context translations here used `apply(lambda x: ...)`
        #     which pandas df.query() rejects with
        #     "'Lambda' nodes are not implemented"
        #   * Zero production usage across saved_searches/, alert_groups/,
        #     script_library/scripts/, and the test YAML corpus
        # Re-introducing them requires a real Python-level filter (not a
        # pandas-eval-friendly translation) - see
        # `reference_spql_where_clause_quirks.md` for context on lambda's
        # incompatibility with df.query().
        _SEARCH_FUNCTIONS = {"isnull", "isnotnull"}
        # 2-arg function names - must be parsed with comma + second operand.
        # `match(field, "regex")` was added 2026-05-05 after the production
        # bug where `where match(...)` silently returned 0 rows because the
        # parser fell through to treating `match` as a bare identifier.
        # The function works in eval context; this fix makes it work in
        # where context too. See `reference_spql_where_clause_quirks.md`.
        _SEARCH_FUNCTIONS_2ARG = {"match"}

        def parse_comparison(self) -> 'SearchDirective.ASTNode':
            # Function call: isnull(field), isnotnull(field), etc.
            if (self.has_next()
                    and self.current_token().type == SearchDirective.TokenType.IDENTIFIER
                    and self.current_token().value.lower() in self._SEARCH_FUNCTIONS
                    and self._peek_ahead_parenthesis("(")):
                func_tok = self.current_token()
                self.get_next_token()  # consume function name
                self.get_next_token()  # consume '('
                arg_node = self.parse_operand()
                if not self.peek_parenthesis(")"):
                    raise ValueError(f"[x] Expected ')' after {func_tok.value}(...).")
                self.get_next_token()  # consume ')'
                node = SearchDirective.ASTNode(SearchDirective.ASTNodeType.FUNCTION_CALL)
                node.operator_ = func_tok.value.lower()
                node.left = arg_node
                return node

            # 2-arg function call: match(field, "regex"), etc.
            if (self.has_next()
                    and self.current_token().type == SearchDirective.TokenType.IDENTIFIER
                    and self.current_token().value.lower() in self._SEARCH_FUNCTIONS_2ARG
                    and self._peek_ahead_parenthesis("(")):
                func_tok = self.current_token()
                self.get_next_token()  # consume function name
                self.get_next_token()  # consume '('
                arg1_node = self.parse_operand()  # field identifier
                if not self.has_next() or self.current_token().type != SearchDirective.TokenType.COMMA:
                    raise ValueError(
                        f"[x] Expected ',' between args in {func_tok.value}(...). "
                        f"Form is `{func_tok.value}(field, \"regex\")`."
                    )
                self.get_next_token()  # consume ','
                arg2_node = self.parse_operand()  # regex string literal
                if not self.peek_parenthesis(")"):
                    raise ValueError(f"[x] Expected ')' after {func_tok.value}(...).")
                self.get_next_token()  # consume ')'
                node = SearchDirective.ASTNode(SearchDirective.ASTNodeType.FUNCTION_CALL)
                node.operator_ = func_tok.value.lower()
                node.left = arg1_node
                node.right = arg2_node
                return node

            # Parenthesized sub-expression
            if self.peek_parenthesis("("):
                self.get_next_token()  # consume '('
                node = self.parse_expression()
                if not self.peek_parenthesis(")"):
                    raise ValueError("[x] Expected ')' after sub-expression.")
                self.get_next_token()  # consume ')'
                return node

            left_operand = self.parse_operand()
            if self.has_next() and self.current_token().type == SearchDirective.TokenType.OPERATOR:
                op = self.current_token().value
                self.get_next_token()  # consume operator
                if op == "IN":
                    if left_operand.node_type != SearchDirective.ASTNodeType.IDENTIFIER:
                        raise ValueError("[x] Expected identifier before IN operator.")
                    if not self.peek_parenthesis("("):
                        raise ValueError("[x] Expected '(' after IN operator.")
                    self.get_next_token()  # consume '('

                    values: List[str] = []
                    while True:
                        if not self.has_next():
                            raise ValueError("[x] Unexpected end of IN clause.")
                        if self.peek_parenthesis(")"):
                            self.get_next_token()  # consume ')'
                            break
                        elif self.current_token().type == SearchDirective.TokenType.COMMA:
                            self.get_next_token()  # skip comma
                            continue
                        elif self.current_token().type in [
                            SearchDirective.TokenType.STRING_LITERAL,
                            SearchDirective.TokenType.NUMBER_LITERAL,
                            SearchDirective.TokenType.IDENTIFIER,
                            SearchDirective.TokenType.LITERAL
                        ]:
                            val_token = self.current_token()
                            values.append(self.normalize_literal(val_token))
                            self.get_next_token()
                        else:
                            raise ValueError("[x] Unexpected token in IN clause.")

                    in_node = SearchDirective.ASTNode(SearchDirective.ASTNodeType.IN_CLAUSE)
                    in_node.identifier = left_operand.literal_or_ident
                    in_node.values = values
                    return in_node
                else:
                    # standard comparison operator (=, !=, <, >, <=, >=)
                    right_operand = self.parse_operand()
                    cmp_node = SearchDirective.ASTNode(SearchDirective.ASTNodeType.COMPARISON)
                    cmp_node.operator_ = op
                    cmp_node.left = left_operand
                    cmp_node.right = right_operand
                    return cmp_node
            return left_operand

        def parse_operand(self) -> 'SearchDirective.ASTNode':
            if not self.has_next():
                raise ValueError("[x] Unexpected end of expression while parsing operand.")

            tok = self.current_token()
            self.get_next_token()  # consume

            if tok.type == SearchDirective.TokenType.IDENTIFIER:
                # Validate a suitable identifier
                if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', tok.value):
                    raise ValueError(f"[x] Invalid identifier: '{tok.value}'")

                # Function call form: ``identifier(args)``. Caught
                # 2026-05-16 while prototyping curator slice 2 - ``where
                # _epoch >= relative_time("-7d")`` silently returned 0
                # rows because the parser treated ``relative_time`` as a
                # bare column identifier and the orphan ``("-2d")``
                # tokens crashed downstream (caught by the run_search
                # except-handler, which returned an empty DataFrame -
                # silent failure). The fix: when an identifier is
                # followed by ``(``, delegate to EvalHandler.safe_eval
                # to evaluate the function at parse-time against a
                # 1-row dummy DataFrame, then substitute the scalar
                # result as a LITERAL operand. Auto-extends to every
                # eval function (relative_time, now, floor, ceil,
                # round, abs, len, etc.) without per-function wiring.
                # Pinned by tests/yaml/tier1_commands/test_where_func_call.yaml
                # + tests/test_spql_function_drift_guard.py.
                if self.has_next() and self.peek_parenthesis("("):
                    return self._parse_call_and_evaluate(tok.value)

                node = SearchDirective.ASTNode(SearchDirective.ASTNodeType.IDENTIFIER)
                node.literal_or_ident = tok.value
                return node

            elif tok.type in [
                SearchDirective.TokenType.STRING_LITERAL,
                SearchDirective.TokenType.NUMBER_LITERAL,
                SearchDirective.TokenType.LITERAL
            ]:
                node = SearchDirective.ASTNode(SearchDirective.ASTNodeType.LITERAL)
                node.literal_or_ident = self.normalize_literal(tok)
                return node

            else:
                raise ValueError(f"[x] Unexpected token type: {tok.type} = {tok.value}")

        def _parse_call_and_evaluate(self, func_name: str) -> 'SearchDirective.ASTNode':
            """Consume ``func_name(args)`` from the token stream, evaluate
            via EvalHandler, and return a LITERAL operand carrying the
            scalar result.

            Functions already handled directly by the where parser
            (``isnull``/``isnotnull``/``match``) take their own paths
            higher up - those branches fire before we get here, so this
            helper only sees functions the where parser doesn't natively
            implement (relative_time, now, floor, ceil, round, abs, len,
            etc. - i.e. the whole eval function library).
            """
            # The current token is ``(``. Walk forward collecting tokens
            # until the matching ``)``, tracking nesting depth for
            # function calls inside function calls (``floor(now()/86400)``).
            self.get_next_token()  # consume '('
            depth = 1
            arg_tokens: List['SearchDirective.Token'] = []
            while self.has_next() and depth > 0:
                tok = self.current_token()
                if tok.type == SearchDirective.TokenType.PARENTHESIS:
                    if tok.value == "(":
                        depth += 1
                    elif tok.value == ")":
                        depth -= 1
                        if depth == 0:
                            self.get_next_token()  # consume final ')'
                            break
                arg_tokens.append(tok)
                self.get_next_token()
            if depth != 0:
                raise ValueError(f"[x] Unbalanced parens in {func_name}(...)")

            # Reconstruct an evaluable expression string from the tokens.
            # String literals need to be re-quoted (the tokenizer stripped
            # the surrounding quotes off STRING_LITERAL tokens).
            parts: List[str] = [f"{func_name}("]
            for i, t in enumerate(arg_tokens):
                if t.type == SearchDirective.TokenType.STRING_LITERAL:
                    escaped = t.value.replace('"', '\\"')
                    parts.append(f'"{escaped}"')
                elif t.type == SearchDirective.TokenType.COMMA:
                    parts.append(",")
                else:
                    parts.append(t.value)
            parts.append(")")
            expr = "".join(parts)

            # Evaluate via EvalHandler against a 1-row dummy frame. The
            # eval handler supports every documented function (now /
            # relative_time / floor / ceil / round / abs / len /
            # strftime / strptime / etc.) - auto-extends here without
            # per-function wiring.
            from handlers.EvalHandler import EvalHandler
            import pandas as _pd
            try:
                result = EvalHandler().safe_eval(expr, _pd.DataFrame({"_dummy_where": [0]}))
            except Exception as exc:
                # Surface the failure with the original expression so
                # operators can see what they wrote. Returning empty
                # silently would re-introduce the bug class this fix
                # exists to eliminate.
                raise ValueError(
                    f"[x] where: could not evaluate {expr!r}: {exc}"
                ) from exc

            # Collapse a 1-row Series result to its scalar value.
            if hasattr(result, "iloc"):
                result = result.iloc[0] if len(result) > 0 else None

            node = SearchDirective.ASTNode(SearchDirective.ASTNodeType.LITERAL)
            # Quote string results; pass numbers as-is.
            if isinstance(result, str):
                escaped = result.replace("'", "\\'")
                node.literal_or_ident = f"'{escaped}'"
            elif result is None:
                node.literal_or_ident = "None"
            else:
                node.literal_or_ident = str(result)
            return node

        @staticmethod
        def normalize_literal(tok: 'SearchDirective.Token') -> str:
            """
            Convert a token into a string suitable for Pandas query. For example:
            - Strings get single-quoted
            - True/False remain unquoted booleans
            - Numbers remain as-is
            """
            if tok.type == SearchDirective.TokenType.STRING_LITERAL:
                escaped = tok.value.replace("'", "\\'")
                return f"'{escaped}'"
            elif tok.type == SearchDirective.TokenType.NUMBER_LITERAL:
                return tok.value
            elif tok.type == SearchDirective.TokenType.LITERAL:
                if tok.value in ["True", "False"]:
                    return tok.value
                else:
                    return f"'{tok.value}'"
            elif tok.type == SearchDirective.TokenType.IDENTIFIER:
                return tok.value
            else:
                raise ValueError("[x] Could not normalize token.")

        def current_token(self) -> 'SearchDirective.Token':
            return self.tokens[self.index]

        def has_next(self) -> bool:
            return self.index < len(self.tokens)

        def get_next_token(self) -> 'SearchDirective.Token':
            tok = self.tokens[self.index]
            self.index += 1
            return tok

        def peek_operator(self, op: str) -> bool:
            if self.has_next():
                nt = self.tokens[self.index]
                return nt.type == SearchDirective.TokenType.OPERATOR and nt.value == op
            return False

        def peek_parenthesis(self, paren: str) -> bool:
            if self.has_next():
                nt = self.tokens[self.index]
                return nt.type == SearchDirective.TokenType.PARENTHESIS and nt.value == paren
            return False

        def _peek_ahead_parenthesis(self, paren: str) -> bool:
            """Look one token ahead (past current) for a parenthesis."""
            next_idx = self.index + 1
            if next_idx < len(self.tokens):
                nt = self.tokens[next_idx]
                return nt.type == SearchDirective.TokenType.PARENTHESIS and nt.value == paren
            return False

        def peek_implicit_and(self) -> bool:
            if not self.has_next():
                return False
            nt = self.tokens[self.index]
            if nt.type in [
                SearchDirective.TokenType.IDENTIFIER,
                SearchDirective.TokenType.STRING_LITERAL,
                SearchDirective.TokenType.NUMBER_LITERAL,
                SearchDirective.TokenType.LITERAL
            ]:
                return True
            if nt.type == SearchDirective.TokenType.PARENTHESIS and nt.value == "(":
                return True
            return False

    # ------------------------------------------------------------------
    # AST -> Pandas Query
    # ------------------------------------------------------------------
    def ast_to_query(self, node: 'ASTNode') -> str:
        if node.node_type == self.ASTNodeType.COMPARISON:
            op_map = {
                "=": "==",
                "!=": "!=",
                "<": "<",
                ">": ">",
                "<=": "<=",
                ">=": ">="
            }
            left_str = self.ast_to_query(node.left)
            right_str = self.ast_to_query(node.right)
            op = op_map.get(node.operator_)
            if not op:
                raise ValueError(f"[x] Unknown operator: {node.operator_}")
            return f"({left_str} {op} {right_str})"

        elif node.node_type == self.ASTNodeType.LOGICAL_OP:
            left_str = self.ast_to_query(node.left)
            right_str = self.ast_to_query(node.right)
            return f"({left_str} {node.operator_} {right_str})"

        elif node.node_type == self.ASTNodeType.IN_CLAUSE:
            vals = ", ".join(node.values)
            return f"({node.identifier} in [{vals}])"

        elif node.node_type == self.ASTNodeType.FUNCTION_CALL:
            field = self.ast_to_query(node.left)
            func = node.operator_
            if func == "isnotnull":
                return f"({field}.notna())"
            elif func == "isnull":
                return f"({field}.isna())"
            elif func == "match":
                # match(field, "regex") → pandas .str.contains() with regex=True.
                # ``na=False`` ensures NaN values don't pass the filter.
                # The caller is responsible for applying match() to a
                # string-compatible column - calling it on a numeric column
                # will raise ``AttributeError: Can only use .str accessor
                # with string values!`` at filter time, which is the right
                # behaviour for misuse (match() is a text function).
                # Caught 2026-05-05: `where match(...)` silently returned 0
                # rows because the parser didn't recognise it as a function
                # call. This translation completes the fix.
                if node.right is None:
                    raise ValueError(
                        "[x] match() requires two args: match(field, \"regex\")"
                    )
                regex = self.ast_to_query(node.right)
                return f"({field}.str.contains({regex}, regex=True, na=False))"
            else:
                raise ValueError(f"[x] Unknown search function: {func}")

        elif node.node_type == self.ASTNodeType.IDENTIFIER:
            return node.literal_or_ident

        elif node.node_type == self.ASTNodeType.LITERAL:
            return node.literal_or_ident

        else:
            raise ValueError("[x] Unknown node type in ast_to_query.")


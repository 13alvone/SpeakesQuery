"""Grammar-derived vocabulary for SpeakesQuery autocomplete / linting.

Parses ``lexers/speakesQuery.g4`` - the single source of truth for SPQL syntax
- and returns a structured vocab dict used by the console autocomplete UI,
the autoformatter, and any future linter or doc-generator.

Design goals
------------
* **No duplication.** The grammar file is the only place SPQL keywords are
  enumerated. This module discovers them at import time.
* **Cheap at runtime.** Parsing the grammar is pure regex + string split,
  well under 10 ms. The result is cached in a module-level singleton.
* **Stable shape.** The returned dict is documented below and versioned so
  the UI can detect grammar drift.

Returned shape::

    {
      "version": 1,
      "commands": [                # top-level pipe directives and init
        {"name": "search", "kind": "directive"},
        {"name": "inputlookup", "kind": "initial"},
        ...
      ],
      "functions": [               # eval / where / stats builtins
        {"name": "round", "kind": "numeric"},
        {"name": "concat", "kind": "string"},
        {"name": "count", "kind": "stats"},
        ...
      ],
      "keywords": ["AND", "OR", ...],
      "operators": ["=", "!=", "<", ">", "<=", ">="],
      "booleans": ["true", "false"],
      "time_units": ["second", "minute", ...]
    }
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

VOCAB_VERSION = 1

_GRAMMAR_PATH = Path(__file__).parent / "speakesQuery.g4"

_LEXER_RULE_RE = re.compile(
    r"^([A-Z][A-Z0-9_]*)\s*:\s*(.+?)\s*;\s*$",
    re.MULTILINE,
)

_LITERAL_RE = re.compile(r"'([^']+)'")

# Tokens that appear inside parser-rule alternatives but are structural,
# not user-visible commands. Skip them when picking the "lead" token of an
# alternative.
_SKIP_LEAD_TOKENS = frozenset(
    {
        "NEWLINE",
        "WS",
        "NOT",          # optional on SEARCH/WHERE etc.
        "PIPE",         # structural
        "LPAREN",
        "RPAREN",
        "LBRACK",
        "RBRACK",
        "COMMA",
        "BACKTICK",
    }
)


# ── Low-level parsing helpers ──────────────────────────────────────


def _load_grammar_text() -> str:
    return _GRAMMAR_PATH.read_text(encoding="utf-8")


def _extract_first_literal(body: str) -> Optional[str]:
    """Return the first lowercase alphabetic literal in a lexer rule body.

    ``WHERE : ('WHERE' | 'where') ;`` -> ``"where"``
    ``IF_   : 'if_' ;``                -> ``"if_"``
    """
    candidates = _LITERAL_RE.findall(body)
    # Prefer lowercase word (SPQL is lowercase by convention).
    for lit in candidates:
        if re.fullmatch(r"[a-z_][a-z_0-9]*", lit):
            return lit
    for lit in candidates:
        if lit:
            return lit.lower()
    return None


def _extract_lexer_map(grammar: str) -> Dict[str, str]:
    """Return ``{TOKEN_NAME: preferred_literal}`` for every lexer rule."""
    out: Dict[str, str] = {}
    for m in _LEXER_RULE_RE.finditer(grammar):
        name, body = m.group(1), m.group(2)
        lit = _extract_first_literal(body)
        if lit is not None:
            out[name] = lit
    return out


def _extract_parser_rule_body(grammar: str, rule: str) -> str:
    """Return the body text of a named parser rule.

    Parser rules always end in ``;`` on its own line (by convention). We
    greedy-match from the rule name to the first ``^\\s*;`` on its own line.
    """
    pattern = re.compile(
        rf"^{re.escape(rule)}\s*:(.*?)^\s*;",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(grammar)
    return m.group(1) if m else ""


def _split_top_level_alternatives(body: str) -> List[str]:
    """Split a parser rule body into its top-level ``|``-separated alts.

    Alternatives inside ``( ... )`` or ``[ ... ]`` groups are not split; the
    parser only considers ``|`` at depth 0.
    """
    alts: List[str] = []
    depth = 0
    buf: List[str] = []
    for ch in body:
        if ch in "([":
            depth += 1
            buf.append(ch)
        elif ch in ")]":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "|" and depth == 0:
            alts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        alts.append(tail)
    return [a for a in alts if a]


def _first_token(alt: str) -> Optional[str]:
    """Return the first uppercase token in *alt* that is not a skip-token."""
    for m in re.finditer(r"\b([A-Z][A-Z0-9_]+)\b", alt):
        tok = m.group(1)
        if tok in _SKIP_LEAD_TOKENS:
            continue
        return tok
    return None


_LEADING_GROUP_RE = re.compile(r"^\s*\(([^()]+)\)")


def _leading_tokens(alt: str) -> List[str]:
    """Return the leading command tokens of an alternative.

    Most alts look like ``SEARCH expr`` - a single token. A few are grouped,
    e.g. ``(HEAD | LIMIT) NUMBER``; in that case every token inside the
    leading group is a command. We also skip leading structural groups like
    ``(NEWLINE | WS)*`` and ``(NOT? ...)`` by rejecting groups containing
    ``?`` or ``*`` quantifiers.
    """
    stripped = alt.lstrip()
    # Skip past leading (NEWLINE | WS)* and (NOT? ...) noise so grouped
    # commands like (HEAD | LIMIT) are recognised even when preceded by
    # optional whitespace groups.
    while True:
        m = _LEADING_GROUP_RE.match(stripped)
        if not m:
            break
        group = m.group(1)
        rest_start = m.end()
        rest = stripped[rest_start: rest_start + 1]
        if rest in ("?", "*", "+"):
            # Optional / repeated - not the command lead itself. Skip it.
            stripped = stripped[rest_start + 1 :].lstrip()
            continue
        tokens = [
            t
            for t in re.findall(r"\b([A-Z][A-Z0-9_]+)\b", group)
            if t not in _SKIP_LEAD_TOKENS
        ]
        if tokens:
            return tokens
        stripped = stripped[rest_start:].lstrip()

    solo = _first_token(stripped)
    return [solo] if solo else []


# ── Category extractors ────────────────────────────────────────────


def _commands(grammar: str, lex: Dict[str, str]) -> List[Dict[str, str]]:
    """Extract top-level pipe directives and initial-clause commands.

    Directives: the ``directive`` parser rule is a flat list of alternatives,
    each starting with the command keyword. Initial-clause commands live in
    their own ``*Init`` rules but are equivalent from the user's point of
    view (they are pipe-prefixed commands too).
    """
    commands: List[Dict[str, str]] = []
    seen: set[str] = set()

    # Directive alternatives - each leads with the command token.
    directive_body = _extract_parser_rule_body(grammar, "directive")
    for alt in _split_top_level_alternatives(directive_body):
        for tok in _leading_tokens(alt):
            literal = lex.get(tok)
            if not literal or literal in seen:
                continue
            seen.add(literal)
            commands.append({"name": literal, "kind": "directive"})

    # Initial-clause commands are named after the init rule (inputlookupInit
    # -> inputlookup, etc.). We resolve via the token referenced inside.
    for init_rule in ("inputlookupInit", "loadjobInit", "makeresultsInit"):
        body = _extract_parser_rule_body(grammar, init_rule)
        for m in re.finditer(r"\b([A-Z][A-Z0-9_]+)\b", body):
            tok = m.group(1)
            if tok in _SKIP_LEAD_TOKENS:
                continue
            literal = lex.get(tok)
            if literal and literal not in seen:
                seen.add(literal)
                commands.append({"name": literal, "kind": "initial"})
                break

    return commands


def _functions(grammar: str, lex: Dict[str, str]) -> List[Dict[str, str]]:
    """Extract function names grouped by category."""
    out: List[Dict[str, str]] = []
    seen: set[str] = set()

    categories = (
        ("numericFunctionCall", "numeric"),
        ("stringFunctionCall", "string"),
        ("specificFunctionCall", "specific"),
        ("statsFunctionCall", "stats"),
    )
    for rule, kind in categories:
        body = _extract_parser_rule_body(grammar, rule)
        for alt in _split_top_level_alternatives(body):
            tok = _first_token(alt)
            if not tok:
                continue
            # statsFunctionCall includes "| numericFunctionCall" as an alt -
            # that's a rule reference, not a token. Skip rule names.
            if tok[0].islower():
                continue
            literal = lex.get(tok)
            if not literal or literal in seen:
                continue
            seen.add(literal)
            out.append({"name": literal, "kind": kind})

    return out


def _keywords(lex: Dict[str, str]) -> List[str]:
    """Return control-flow keywords (AND/OR/NOT/BY/AS/IN)."""
    wanted = ("AND", "OR", "NOT", "BY", "AS", "IN")
    return [lex[k].upper() for k in wanted if k in lex]


def _operators() -> List[str]:
    # Hard-coded because operators are punctuation; there's no value in
    # parsing them out of lexer rules when they never change.
    return ["=", "!=", "<", ">", "<=", ">="]


def _booleans(lex: Dict[str, str]) -> List[str]:
    # BOOLEAN lexer rule lists all variants; we return canonical lowercase.
    return ["true", "false"]


def _time_units(lex: Dict[str, str]) -> List[str]:
    wanted = ("SECONDS", "MINUTES", "HOURS", "DAYS", "WEEKS", "YEARS")
    units: List[str] = []
    for key in wanted:
        if key in lex:
            units.append(lex[key].rstrip("s"))
    return units


# ── Public API ─────────────────────────────────────────────────────


_lock = threading.Lock()
_cache: Optional[dict] = None


def get_vocab(*, reload: bool = False) -> dict:
    """Return the cached grammar vocabulary dict.

    Pass ``reload=True`` to force a re-parse (useful for tests and after
    grammar regeneration during development).
    """
    global _cache
    if _cache is not None and not reload:
        return _cache
    with _lock:
        if _cache is not None and not reload:
            return _cache
        grammar = _load_grammar_text()
        lex = _extract_lexer_map(grammar)
        _cache = {
            "version": VOCAB_VERSION,
            "commands": _commands(grammar, lex),
            "functions": _functions(grammar, lex),
            "keywords": _keywords(lex),
            "operators": _operators(),
            "booleans": _booleans(lex),
            "time_units": _time_units(lex),
        }
    return _cache

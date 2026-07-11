"""
SPQL pipeline split / join helpers - Phase 4 / Bet 4 slice 6.

The Visual Builder needs to round-trip text ↔ visual losslessly:

* **Split** an SPQL string into ``{index_clause, stages}`` so the SPA
  can populate stage cards from operator-pasted text.
* **Join** a ``{index_clause, stages}`` back into an SPQL string for
  display + execution (mirrors the SPA's ``_vbBuildSpql`` helper).

The split function is delimited by the ``|`` pipe character with one
constraint: a ``|`` inside double-quoted strings is NOT a delimiter.
This handles the common case of regex / SPQL operators that use
``|`` as a literal (``regex foo "(a|b)"``, ``eval x="a|b|c"``).

**Slice 6 scope:**
* Initial clause detection: the first segment is treated as the
  ``index_clause`` if and only if it starts with ``index=`` (case
  insensitive). Otherwise it's just a regular stage starting at
  position 0.
* Each non-initial segment splits into ``{command, kwargs}`` on the
  first whitespace boundary.
* Subsearches (``[ ... ]``) are NOT recursed - any stage whose kwargs
  contain a ``[`` keeps its kwargs verbatim. The visual builder
  surfaces the subsearch as opaque text in slice 6; deeper nesting
  arrives in a later slice.

**Lossless guarantee (the load-bearing property):**
For every well-formed SPQL string ``s``, the round trip
``join_spql_pipeline(split_spql_pipeline(s))`` must produce a string
that re-parses to the same ``{index_clause, stages}`` (modulo
whitespace normalisation). Pinned by
``tests/test_spql_pipeline_split.py::TestRoundTripLossless`` against
a hand-curated 100-query corpus.

This module is intentionally separate from the ANTLR-generated
parser. The Visual Builder doesn't need a full parse tree; it needs
a flat stage list. Reusing ANTLR would be heavyweight + would tie
the visual builder's behaviour to grammar evolution. The flat split
is grammar-version-stable.
"""

from __future__ import annotations


def split_spql_pipeline(spql_text: str) -> dict:
    """Split an SPQL string into an index clause + ordered stages.

    Returns:
        ``{
            "index_clause": str,        # may be empty
            "stages": [
                {"command": str, "kwargs": str},
                ...
            ],
        }``

    Lossless contract: ``join_spql_pipeline(split_spql_pipeline(s))``
    must equal ``s`` modulo whitespace. Pinned by
    ``tests/test_spql_pipeline_split.py``.

    Empty / whitespace-only input returns
    ``{"index_clause": "", "stages": []}``.
    """
    if not isinstance(spql_text, str):
        return {"index_clause": "", "stages": []}
    text = spql_text.strip()
    if not text:
        return {"index_clause": "", "stages": []}

    segments = _split_on_pipe_outside_quotes(text)
    if not segments:
        return {"index_clause": "", "stages": []}

    # First segment: if it starts with `index=` (case insensitive),
    # treat as the initial clause; otherwise treat as a regular stage.
    first = segments[0].strip()
    index_clause = ""
    pipe_segments: list[str] = []
    if _looks_like_initial_clause(first):
        index_clause = first
        pipe_segments = segments[1:]
    else:
        pipe_segments = segments

    stages: list[dict] = []
    for seg in pipe_segments:
        seg_stripped = seg.strip()
        if not seg_stripped:
            # Skip empty stages - the operator may have typed
            # `| | head 5` which is a syntax error in SPQL but we
            # don't want to crash here. Skip silently.
            continue
        command, kwargs = _split_first_token(seg_stripped)
        stages.append({"command": command, "kwargs": kwargs})

    return {"index_clause": index_clause, "stages": stages}


def join_spql_pipeline(parsed: dict) -> str:
    """Inverse of :func:`split_spql_pipeline`.

    Mirrors the SPA's ``_vbBuildSpql`` helper exactly so server-side
    parsing + client-side joining produce identical SPQL strings.
    """
    if not isinstance(parsed, dict):
        return ""
    index_clause = (parsed.get("index_clause") or "").strip()
    stages = parsed.get("stages") or []
    pieces: list[str] = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        cmd = (stage.get("command") or "").strip()
        if not cmd:
            continue
        kw = (stage.get("kwargs") or "").strip()
        pieces.append(f"{cmd} {kw}".rstrip() if kw else cmd)
    if not index_clause and not pieces:
        return ""
    if not index_clause:
        return "| " + "\n| ".join(pieces)
    if not pieces:
        return index_clause
    return index_clause + "\n| " + "\n| ".join(pieces)


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────

def _split_on_pipe_outside_quotes(text: str) -> list[str]:
    """Split ``text`` on ``|`` characters that are NOT inside double-
    quoted strings. Handles backslash-escaped quotes (``\\"``) inside
    quoted regions. Single quotes are NOT a quote character in SPQL -
    the grammar uses double quotes only.
    """
    segments: list[str] = []
    current: list[str] = []
    in_quotes = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and in_quotes:
            # Preserve backslash-escape sequence verbatim
            current.append(ch)
            current.append(text[i + 1])
            i += 2
            continue
        if ch == '"':
            in_quotes = not in_quotes
            current.append(ch)
            i += 1
            continue
        if ch == "|" and not in_quotes:
            segments.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    segments.append("".join(current))
    return segments


def _looks_like_initial_clause(segment: str) -> bool:
    """Return True if the segment looks like an SPQL initial clause
    (an ``index=...`` prefix, possibly with surrounding whitespace).
    Case insensitive.
    """
    s = segment.lstrip().lower()
    return s.startswith("index=") or s.startswith("index ")


def _split_first_token(segment: str) -> tuple[str, str]:
    """Split the segment into its first whitespace-delimited token
    (the command name) and the rest (the kwargs string).

    Returns ``(command, kwargs)`` where ``kwargs`` may be empty.

    Whitespace is collapsed to single spaces in the kwargs to keep
    the round-trip stable. Newlines inside kwargs (e.g. multi-line
    eval expressions) become single spaces.
    """
    stripped = segment.strip()
    if not stripped:
        return "", ""
    # Find the first whitespace character (space, tab, newline)
    for i, ch in enumerate(stripped):
        if ch in " \t\n\r":
            command = stripped[:i]
            kwargs = stripped[i + 1:].strip()
            # Normalise internal whitespace runs to single spaces so
            # the round-trip is stable.
            kwargs = " ".join(kwargs.split())
            return command, kwargs
    return stripped, ""


__all__ = [
    "split_spql_pipeline",
    "join_spql_pipeline",
]

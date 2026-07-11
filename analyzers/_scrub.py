"""Shared secret-scrubbing helpers for every Claude API call path.

Originally lived inside ``analyzers/claude_client.py`` - the live request
path. H-AN-7 (2026-04-21 production review) found that the batch-submit
path in ``analyzers/claude_analyzer.py::_call_batch_api`` recorded the full
request payload to ``claude_api_history.sqlite`` without routing through
the same scrubber, leaving operator-pasted ``sk-ant-*`` tokens plaintext in
the history store.

Both paths now import from this module so every write to the history store
- sync, batch-submit, batch-poller - shares a single redaction boundary.

**Anti-goal:** full DLP. We do not scrub arbitrary PII, tokens we don't
recognise, or credentials the user stores intentionally. The scope is
narrow: strings that look like Anthropic API keys, because those are the
one class of secret that should never reach a forensic SQLite outside of
the credential vault.
"""
from __future__ import annotations

import re as _re

# ``sk-ant-api03-...`` and similar Anthropic keys; the lookahead requires
# >= 20 additional chars so we don't clobber user text mentioning ``sk-``
# in isolation. Second pattern is a broader safety net for any ``sk-``
# prefix with a 30+ char alphanumeric tail.
_SECRET_PATTERNS = (
    _re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    _re.compile(r"\bsk-[A-Za-z0-9_\-]{30,}\b"),
)


def scrub_secrets(value):
    """Walk a JSON-shaped structure and replace Anthropic-key-looking substrings.

    Idempotent; accepts any type (str / list / dict / scalar). Non-string
    scalars pass through untouched so numeric fields like token counts
    survive.
    """
    if isinstance(value, str):
        out = value
        for pat in _SECRET_PATTERNS:
            out = pat.sub("[REDACTED]", out)
        return out
    if isinstance(value, list):
        return [scrub_secrets(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub_secrets(v) for k, v in value.items()}
    return value


def redact_kwargs(kwargs: dict) -> dict:
    """Return a log-safe copy of a Claude ``messages.create`` kwargs / payload.

    * Callables are stripped (can't be pickled meaningfully, and they
      usually represent ``tool_use_function`` handles).
    * Every string is routed through :func:`scrub_secrets` - including
      deeply nested strings inside ``messages[].content`` blocks, tool
      definitions, etc.

    The ``api_key`` is never in kwargs (it's bound to the ``Anthropic()``
    client), but a malformed prompt could include one. That's what this
    exists to catch.
    """
    safe: dict = {}
    for k, v in kwargs.items():
        if callable(v):
            safe[k] = f"<callable:{getattr(v, '__name__', type(v).__name__)}>"
            continue
        safe[k] = scrub_secrets(v)
    return safe

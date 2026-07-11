"""Credential-value redaction helpers shared across ingestion-engine paths.

Exception messages and subprocess stderr frequently embed credential values
verbatim - e.g. ``KeyError({'GITHUB_TOKEN': 'ghp_real...'})`` from a script
that mis-reads the CREDENTIALS dict, or ``echo $SPEAKESQUERY_CRED_X >&2`` from
a repo script. Those strings then land in Parquet telemetry logs and the
SQLite execution history, exposing the secret.

Two redaction points live here:

  * :func:`redact_credentials` - substitutes every literal credential VALUE
    in *msg* with ``[REDACTED:KEY]``. Used on:
      - ``engine._run_task`` ``str(exc)`` on error paths (H-SV-2)
      - ``engine._run_repo_script`` ``result.stderr`` (H-SV-3)

  * :func:`redact_subprocess_output` - applies :func:`redact_credentials`
    AND regex-scrubs ``SPEAKESQUERY_CRED_<KEY>=<value>`` environment-variable
    dumps (common when a script runs ``env``, ``printenv``, or logs
    ``os.environ``). The regex catches cases where the raw value is
    concatenated or re-encoded so the literal-value match misses it.

The helper uses plain ``str.replace`` for literal values so credential
values containing regex metacharacters (``.``, ``+``, ``?``) are handled
correctly. Only the env-dump scrub uses a regex - and that regex targets a
fixed shape (``SPEAKESQUERY_CRED_[A-Z0-9_]+=<non-whitespace>``).
"""
from __future__ import annotations

import re
from typing import Mapping

# Below this length, a value is too short to uniquely identify a secret and
# redacting it would be collateral damage (e.g. "1" or "ok"). The length
# threshold matches what standard token formats use: GitHub ``ghp_`` prefix
# is already 4 chars; API keys, JWTs, UUIDs are all much longer.
_MIN_REDACT_LEN = 4


def redact_credentials(
    msg: object,
    cred_dict: Mapping[str, object] | None,
) -> str:
    """Return *msg* with every credential value replaced by ``[REDACTED:KEY]``.

    Parameters
    ----------
    msg : any
        String-like message to scrub. Coerced with :func:`str` so exception
        objects, dicts, etc. can be passed directly.
    cred_dict : mapping or None
        ``{credential_name: credential_value, ...}``. May be ``None`` or
        empty, in which case *msg* is returned coerced to ``str`` with no
        substitution.

    Notes
    -----
    * Values shorter than ``_MIN_REDACT_LEN`` are ignored.
    * Non-string values are stringified before replacement.
    * Credential names are upper-cased in the sentinel so the operator can
      identify which credential was involved without seeing the value.
    * Longest values are substituted first so that a credential whose value
      is a prefix of another doesn't leak the longer one.
    """
    text = msg if isinstance(msg, str) else str(msg)
    if not cred_dict:
        return text

    # Build a list of (key, value_str) pairs for values worth redacting.
    pairs: list[tuple[str, str]] = []
    for key, raw_val in cred_dict.items():
        if raw_val is None:
            continue
        val_str = raw_val if isinstance(raw_val, str) else str(raw_val)
        if len(val_str) < _MIN_REDACT_LEN:
            continue
        pairs.append((str(key), val_str))

    # Substitute longest first to avoid shadow-substitution of prefix values.
    pairs.sort(key=lambda kv: len(kv[1]), reverse=True)
    for key, val_str in pairs:
        sentinel = f"[REDACTED:{key.upper()}]"
        text = text.replace(val_str, sentinel)

    return text


# Matches ``SPEAKESQUERY_CRED_<KEY>=<value>`` where value is non-whitespace.
# Crafted to grab the value terminator at the next whitespace, ``:``, quote,
# or end-of-string - whichever comes first. We err on the side of
# aggressive matching because stray chars after the value are less harmful
# than leaking the prefix of a token.
_ENV_ASSIGNMENT_RE = re.compile(
    r"(SPEAKESQUERY_CRED_[A-Z0-9_]+)=([^\s'\"`,;]+)"
)


def redact_subprocess_output(
    output: object,
    cred_dict: Mapping[str, object] | None,
) -> str:
    """Scrub a subprocess stderr/stdout payload of credential material.

    Applies two passes:

    1. ``SPEAKESQUERY_CRED_<KEY>=<value>`` env-dump substitution. Catches the
       case where a repo script ran ``env | grep SPEAKESQUERY`` or logged
       ``os.environ`` - the raw key=value assignment is replaced with
       ``SPEAKESQUERY_CRED_<KEY>=[REDACTED]``. This pass fires even when
       *cred_dict* is empty (the dispatcher may have failed before
       populating it).

    2. Literal-value substitution via :func:`redact_credentials`. Catches
       the case where the script echoed ``$SPEAKESQUERY_CRED_X`` directly
       (expanded by the shell) - the literal value appears in the output
       without the ``KEY=`` prefix.
    """
    text = output if isinstance(output, str) else str(output)
    text = _ENV_ASSIGNMENT_RE.sub(r"\1=[REDACTED]", text)
    return redact_credentials(text, cred_dict)

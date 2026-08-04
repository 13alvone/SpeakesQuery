"""
Alert Group Dispatcher
──────────────────────
Orchestrates a single alert group run: serialize results, build prompt,
call Claude API, send email, and log the run.

Uses the existing Claude API integration and email sender - does not
duplicate either.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from alert_groups.builder import PayloadBuilder
from alert_groups.models import AlertGroupRunResult, SerializedResult
from alert_groups.serializer import (
    EmptyResultError,
    ResultSerializer,
    SearchNotFoundError,
)
from analyzers.claude_client import (
    ClaudeCallError,
    ClaudeCallResult,
    call_messages_create,
)
from functionality.log_writer import log_ag_pick, log_alert_group_event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cross-process dispatch lock (M-AN-11, 2026-04-22)
# ---------------------------------------------------------------------------
# APScheduler's ``max_instances=1`` prevents a cron fire from overlapping
# ITSELF, but cannot prevent a manual "Run Now" click via
# ``/api/alert-groups/<id>/run`` from landing mid-cron-fire of the same
# AG. Both paths then mutate circuit-breaker state in ``AlertGroupStore``,
# where same-file YAML/SQLite reads + writes race.
#
# A filesystem exclusive-create lock is cross-platform (POSIX + Windows
# both honour ``O_CREAT | O_EXCL``), requires no new dependency, and
# leaves an audit artefact under ``.locks/`` if a crash ever leaves a
# lock behind. We include the PID + monotonic clock in the file so an
# operator can tell whether it's stale.

_DISPATCH_LOCK_DIR = Path(__file__).resolve().parent.parent / ".locks"
# If a lock file is older than this, treat it as stale (crashed holder).
# Tune this higher than the longest legitimate dispatch (~10 minutes on
# web_search + retry).
_DISPATCH_LOCK_STALE_AFTER_SECONDS = 30 * 60


def _sanitize_group_name_for_filename(name: str) -> str:
    """Return a filesystem-safe rendering of an AG name for the lock file."""
    out = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(name))[:80]
    return out or "unnamed"


@contextmanager
def _acquire_dispatch_lock(group_name: str):
    """Context manager that guards against concurrent dispatch of the same AG.

    Yields True on acquisition, False when another dispatch is already
    running. Callers must check the yielded flag and skip work when it's
    False - raising would be the wrong semantics (the second caller isn't
    an error, it just shouldn't proceed).
    """
    try:
        _DISPATCH_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        # Can't create the lock dir - fall through without a lock rather
        # than blocking dispatches on a filesystem issue.
        logger.warning(
            "[!] AG dispatch lock unavailable (mkdir failed for %s): %s. "
            "Falling back to no-lock semantics for this run.",
            _DISPATCH_LOCK_DIR, exc,
        )
        yield True
        return

    lock_path = _DISPATCH_LOCK_DIR / (
        f"ag_{_sanitize_group_name_for_filename(group_name)}.lock"
    )

    # Stale-lock sweep: if the file is older than the threshold, the
    # prior holder almost certainly crashed - remove and proceed.
    try:
        if lock_path.exists():
            age = time.time() - lock_path.stat().st_mtime
            if age > _DISPATCH_LOCK_STALE_AFTER_SECONDS:
                logger.warning(
                    "[!] AG '%s': removing stale dispatch lock (age=%ds, "
                    "threshold=%ds). Prior holder likely crashed.",
                    group_name, int(age),
                    _DISPATCH_LOCK_STALE_AFTER_SECONDS,
                )
                lock_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning(
            "[!] AG '%s': stale-lock check failed (%s); proceeding.",
            group_name, exc,
        )

    fd = None
    try:
        fd = os.open(
            str(lock_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
    except FileExistsError:
        logger.info(
            "[i] AG '%s': another dispatch is already in progress "
            "(lock=%s). Skipping this invocation.",
            group_name, lock_path,
        )
        yield False
        return
    except Exception as exc:
        # Unexpected filesystem error - log and fall through without a
        # lock rather than blocking dispatches.
        logger.warning(
            "[!] AG '%s': could not acquire dispatch lock (%s: %s); "
            "proceeding without lock.",
            group_name, type(exc).__name__, exc,
        )
        yield True
        return

    try:
        # Record holder metadata so a forensic readout of stale lock
        # files shows what was running.
        os.write(
            fd, f"pid={os.getpid()} started={time.time():.3f}\n".encode(),
        )
    except Exception:
        pass
    finally:
        try:
            os.close(fd)
        except Exception:
            pass

    try:
        yield True
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning(
                "[!] AG '%s': failed to release dispatch lock %s: %s",
                group_name, lock_path, exc,
            )

# ---------------------------------------------------------------------------
# SpeakesQuery logo SVG (light theme) for HTML emails - base64 encoded
# Sourced from logos/speakesQuery_logo_svgs_REV6/speakesquery_light.svg at import
# time. Falls back to a compact inline SVG if the file is unavailable in the
# running image (e.g. stale Docker build that predates 2026-04-20).
# ---------------------------------------------------------------------------
def _load_logo_b64() -> str:
    import base64 as _b64
    try:
        from pathlib import Path as _Path
        logo_path = (
            _Path(__file__).resolve().parent.parent
            / "logos" / "speakesQuery_logo_svgs_REV6" / "speakesquery_light.svg"
        )
        if logo_path.exists():
            return _b64.b64encode(logo_path.read_bytes()).decode("ascii")
    except Exception as exc:
        logger.warning("[!] Could not load email logo %s: %s", "speakesquery_light.svg", exc)
    return _FALLBACK_LOGO_B64


_FALLBACK_LOGO_B64 = (
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA4NDAgMjEwIiB3"
    "aWR0aD0iNDIwIiBoZWlnaHQ9IjEwNSI+CiAgPHJlY3QgeD0iMTYiIHk9IjE2IiB3aWR0aD0iMTcwIiBoZW"
    "lnaHQ9IjE3OCIgcng9IjIyIiBmaWxsPSIjRjBGNkZDIi8+CiAgPHJlY3QgeD0iMTYiIHk9IjE2IiB3aWR0"
    "aD0iMTcwIiBoZWlnaHQ9IjI4IiByeD0iMjIiIGZpbGw9IiNFNEVERjYiLz4KICA8cmVjdCB4PSIxNiIgeT"
    "0iMzIiIHdpZHRoPSIxNzAiIGhlaWdodD0iMTIiIGZpbGw9IiNFNEVERjYiLz4KICA8Y2lyY2xlIGN4PSI0"
    "MiIgY3k9IjMwIiByPSIzLjUiIGZpbGw9IiNGRjVGNTciIG9wYWNpdHk9IjAuOCIvPgogIDxjaXJjbGUgY3"
    "g9IjU2IiBjeT0iMzAiIHI9IjMuNSIgZmlsbD0iI0ZFQkMyRSIgb3BhY2l0eT0iMC44Ii8+CiAgPGNpcmNs"
    "ZSBjeD0iNzAiIGN5PSIzMCIgcj0iMy41IiBmaWxsPSIjMjhDODQwIiBvcGFjaXR5PSIwLjgiLz4KICA8cmVj"
    "dCB4PSIzOCIgeT0iNTYiIHdpZHRoPSI4OCIgaGVpZ2h0PSI3IiByeD0iMyIgZmlsbD0iI0E4RDRGMCIgb3Bh"
    "Y2l0eT0iMC41Ii8+CiAgPHJlY3QgeD0iMzgiIHk9IjcxIiB3aWR0aD0iMTI0IiBoZWlnaHQ9IjciIHJ4PS"
    "IzIiBmaWxsPSIjMUE1QTk2IiBvcGFjaXR5PSIwLjMiLz4KICA8cmVjdCB4PSIzOCIgeT0iODYiIHdpZHRo"
    "PSI2NiIgaGVpZ2h0PSI3IiByeD0iMyIgZmlsbD0iI0E4RDRGMCIgb3BhY2l0eT0iMC42Ii8+CiAgPHJlY3"
    "QgeD0iMzgiIHk9IjEwMSIgd2lkdGg9IjEwOCIgaGVpZ2h0PSI3IiByeD0iMyIgZmlsbD0iIzFBNUE5NiIg"
    "b3BhY2l0eT0iMC40Ii8+CiAgPHJlY3QgeD0iMzgiIHk9IjExNiIgd2lkdGg9Ijc4IiBoZWlnaHQ9IjciIH"
    "J4PSIzIiBmaWxsPSIjQThENEYwIiBvcGFjaXR5PSIwLjciLz4KICA8cmVjdCB4PSIzOCIgeT0iMTMxIiB3aW"
    "R0aD0iMTMwIiBoZWlnaHQ9IjciIHJ4PSIzIiBmaWxsPSIjMUE1QTk2IiBvcGFjaXR5PSIwLjU1Ii8+CiAgPH"
    "JlY3QgeD0iMzgiIHk9IjE0NiIgd2lkdGg9Ijk2IiBoZWlnaHQ9IjciIHJ4PSIzIiBmaWxsPSIjQThENEYw"
    "IiBvcGFjaXR5PSIwLjg1Ii8+CiAgPHRleHQgeD0iMzgiIHk9IjE3NCIgZm9udC1mYW1pbHk9IidTRiBNb25v"
    "JywnQ2FzY2FkaWEgQ29kZScsJ0ZpcmEgQ29kZScsbW9ub3NwYWNlIiBmb250LXNpemU9IjEzIiBmb250LX"
    "dlaWdodD0iNjAwIiBmaWxsPSIjMUE1QTk2IiBvcGFjaXR5PSIwLjUiPiZndDtfPC90ZXh0PgogIDx0ZXh0IH"
    "g9IjIxNiIgeT0iMTI0IiBmb250LWZhbWlseT0iJ1NlZ29lIFVJJywnU0YgUHJvIERpc3BsYXknLCdIZWx2"
    "ZXRpY2EgTmV1ZScsc2Fucy1zZXJpZiIgZm9udC1zaXplPSI5MCIgZm9udC13ZWlnaHQ9IjcwMCIgbGV0dG"
    "VyLXNwYWNpbmc9Ii0yIiBmaWxsPSIjODhDNEU4Ij5TcGVhazwvdGV4dD4KICA8dGV4dCB4PSI1MjAiIHk9"
    "IjEyNCIgZm9udC1mYW1pbHk9IidTZWdvZSBVSScsJ1NGIFBybyBEaXNwbGF5JywnSGVsdmV0aWNhIE5ldW"
    "UnLHNhbnMtc2VyaWYiIGZvbnQtc2l6ZT0iOTAiIGZvbnQtd2VpZ2h0PSI3MDAiIGxldHRlci1zcGFjaW5n"
    "PSIxIiBmaWxsPSIjMUE1QTk2Ij5RdWVyeTwvdGV4dD4KPC9zdmc+"
)

# Loaded on first email build. A long-running process rebuilds this once
# and hits the cached value on subsequent sends.
_LOGO_SVG_B64 = _load_logo_b64()


def _convert_md_tables(text: str) -> str:
    """Convert GitHub-style markdown tables to inline-styled HTML tables.

    Operates on already-HTML-escaped text (pipes survive escaping).
    A table is a ``| ... |`` header line followed by a ``|---|---|``
    separator; rows continue until the first non-pipe line. Anything
    that doesn't match passes through untouched. Added 2026-08-04:
    analyst briefs regularly emit small tables that previously rendered
    as raw pipe characters in the email.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    sep_re = re.compile(r"^\s*\|[\s:|\-]+\|\s*$")
    while i < len(lines):
        line = lines[i]
        if (line.lstrip().startswith("|") and i + 1 < len(lines)
                and sep_re.match(lines[i + 1])):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(
                    [c.strip() for c in lines[i].strip().strip("|").split("|")]
                )
                i += 1
            th = "".join(
                f'<th style="padding:6px 10px; border:1px solid #dde3ea; '
                f'background:#f0f6fc; text-align:left; font-size:13px; '
                f'color:#1A5A96;">{c}</th>' for c in header
            )
            trs = "".join(
                "<tr>" + "".join(
                    f'<td style="padding:6px 10px; border:1px solid #dde3ea; '
                    f'font-size:13px;">{c}</td>' for c in row
                ) + "</tr>" for row in rows
            )
            out.append(
                '<table cellpadding="0" cellspacing="0" '
                'style="border-collapse:collapse; margin:12px 0; '
                'width:100%;">'
                f"<tr>{th}</tr>{trs}</table>"
            )
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _markdown_to_html(text: str) -> str:
    """Minimal markdown-to-HTML conversion for Claude's response text."""
    import html as html_mod

    text = html_mod.escape(text)

    # Tables first (before bold/italic so cell content still gets
    # inline formatting applied afterwards).
    text = _convert_md_tables(text)

    # Headers: ### h3, ## h2, # h1
    text = re.sub(
        r"^### (.+)$", r'<h3 style="color:#1A5A96; margin:18px 0 8px;">\1</h3>', text, flags=re.MULTILINE
    )
    text = re.sub(
        r"^## (.+)$", r'<h2 style="color:#1A5A96; margin:20px 0 10px;">\1</h2>', text, flags=re.MULTILINE
    )
    text = re.sub(
        r"^# (.+)$", r'<h1 style="color:#1A5A96; margin:24px 0 12px;">\1</h1>', text, flags=re.MULTILINE
    )

    # Bold and italic
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)

    # Inline code
    text = re.sub(
        r"`([^`]+)`",
        r'<code style="background:#f0f4f8; padding:2px 6px; border-radius:3px; font-size:13px;">\1</code>',
        text,
    )

    # Horizontal rules
    text = re.sub(r"^---+$", '<hr style="border:none; border-top:1px solid #dde3ea; margin:20px 0;">', text, flags=re.MULTILINE)

    # Bullet lists
    text = re.sub(
        r"^[•\-\*] (.+)$",
        r'<li style="margin:4px 0;">\1</li>',
        text,
        flags=re.MULTILINE,
    )
    # Wrap consecutive <li> in <ul>
    text = re.sub(
        r"((?:<li[^>]*>.*?</li>\n?)+)",
        r'<ul style="margin:10px 0; padding-left:24px;">\1</ul>',
        text,
    )

    # Numbered lists
    text = re.sub(
        r"^\d+\.\s+(.+)$",
        r'<li style="margin:4px 0;">\1</li>',
        text,
        flags=re.MULTILINE,
    )

    # Paragraphs: convert double newlines
    text = re.sub(r"\n\n+", "</p><p>", text)
    text = f"<p>{text}</p>"
    # Clean up empty paragraphs
    text = re.sub(r"<p>\s*</p>", "", text)

    return text


def build_html_email(
    group_name: str, response_text: str, meta: dict,
    template_override: str | None = None,
) -> str:
    """Wrap Claude's response in a branded HTML email template.

    When ``template_override`` is non-empty, use that HTML verbatim with
    token substitution applied. Supported tokens (all string-replaced):

    - ``{{group_name}}`` - alert group name
    - ``{{body_html}}`` - Claude's response rendered to HTML
    - ``{{body_text}}`` - Claude's response as plain text
    - ``{{meta_bar}}`` - the standard "searches / tokens / cost" mini bar
    - ``{{searches_used}}`` - comma-joined list of searches
    - ``{{estimated_tokens}}`` / ``{{actual_tokens}}`` / ``{{cost_usd}}``

    Absent those tokens, the default branded template is used.
    """
    # Prompt-only mode: render the built prompt verbatim inside a <pre>
    # block so fenced code blocks, tables, and markdown structure survive
    # unmolested for manual copy-paste into Claude.ai. For the API path we
    # still run the light markdown-to-HTML conversion that formats
    # headings, lists, and bolding for the analyst brief.
    if meta.get("prompt_only"):
        import html as _html
        body_html = (
            '<pre style="margin:0; padding:14px 18px; '
            'background:#f6f8fa; border:1px solid #d0d7de; '
            'border-radius:6px; font-family:\'SFMono-Regular\',Menlo,'
            'Consolas,monospace; font-size:12px; line-height:1.5; '
            'color:#24292f; white-space:pre-wrap; word-break:break-word;">'
            f'{_html.escape(response_text)}'
            '</pre>'
        )
    else:
        body_html = _markdown_to_html(response_text)

    searches_used = meta.get("searches_used", [])
    estimated_tokens = meta.get("estimated_tokens", 0)
    actual_tokens = meta.get("actual_tokens", 0)
    cost_usd = meta.get("cost_usd", 0.0)

    meta_items = []
    if searches_used:
        meta_items.append(f"Searches: {len(searches_used)}")
    if estimated_tokens:
        meta_items.append(f"Est. tokens: {estimated_tokens:,}")
    if actual_tokens:
        meta_items.append(f"Actual tokens: {actual_tokens:,}")
    if cost_usd:
        meta_items.append(f"Cost: ${cost_usd:.4f}")
    # Prompt-only runs have zero cost + zero actual tokens - surface that
    # fact explicitly rather than omitting the line, so the recipient sees
    # the savings every time.
    if meta.get("prompt_only"):
        meta_items.append("Mode: prompt-only (no API call, $0.00)")
    if meta.get("digest"):
        meta_items.append(
            f"Summarized by {meta.get('digest_model') or 'local model'} "
            f"(full brief attached)"
        )
    meta_bar = " &middot; ".join(meta_items) if meta_items else ""

    # Self-documenting truncation banner - inline HTML says plainly that
    # Claude ran out of output tokens and points to the attached .md for
    # anything beyond opportunity #N. Never silent.
    truncation_banner = ""
    if meta.get("truncated"):
        truncation_banner = (
            '<tr><td style="padding:12px 32px 0;">'
            '<div style="padding:12px 16px; background:#FFF3CD; '
            'border:1px solid #FFE69C; border-radius:6px; font-size:13px; '
            'color:#664D03;">'
            '<strong>\u26A0 Analyst brief was truncated</strong> - '
            'Claude hit the <code>max_tokens</code> output cap before '
            'finishing. The attached <code>.md</code> file contains the '
            'complete response that was generated (check that first). '
            'Raise <code>max_output_tokens</code> on this alert group '
            '(or globally in Settings) to avoid truncation on the next run.'
            '</div></td></tr>'
        )

    # Prompt-only banner - budget-friendly mode emits the built prompt so
    # the operator can paste it into Claude.ai manually instead of paying
    # for an API call. The email body is the prompt itself; this banner
    # flags the delivery mode so the recipient doesn't mistake the payload
    # for an analyst brief.
    prompt_only_banner = ""
    if meta.get("salvage"):
        import html as _html_mod
        reason = _html_mod.escape(str(meta.get("failure_reason") or "")[:400])
        prompt_only_banner = (
            '<tr><td style="padding:12px 32px 0;">'
            '<div style="padding:12px 16px; background:#FFF3CD; '
            'border:1px solid #FFE69C; border-radius:6px; font-size:13px; '
            'color:#664D03;">'
            '<strong>⚠ AI analysis failed - data preserved</strong> - '
            'the LLM call for this brief failed after all retry attempts '
            f'(<code>{reason}</code>). So today\'s collected data is not '
            'lost, the fully built prompt is below (and attached as '
            '<code>.md</code>). Paste it into '
            '<a href="https://claude.ai" style="color:#664D03;">Claude.ai</a> '
            'or any LLM to get the analysis manually. The system will '
            'retry automatically on the next scheduled run.'
            '</div></td></tr>'
        )
    elif meta.get("prompt_only"):
        prompt_only_banner = (
            '<tr><td style="padding:12px 32px 0;">'
            '<div style="padding:12px 16px; background:#E0F2FE; '
            'border:1px solid #7DD3FC; border-radius:6px; font-size:13px; '
            'color:#075985;">'
            '<strong>\U0001F4DD Prompt-only delivery</strong> - '
            'no Claude API call was made for this alert group run (cost '
            '$0.00). The text below is the <em>built prompt</em> that '
            'would have been sent to Claude. Paste it into '
            '<a href="https://claude.ai" style="color:#075985;">Claude.ai</a> '
            '(or any LLM of your choice) to finish the analysis manually. '
            'The attached <code>.md</code> file contains the same prompt '
            'in a copy-paste-friendly format. Toggle <code>delivery_mode</code> '
            'back to <code>api</code> on this alert group to resume '
            'automated Claude dispatch.'
            '</div></td></tr>'
        )

    if template_override and template_override.strip():
        rendered = template_override
        # M-AN-8 (2026-04-22): the legacy one-pass ``str.replace`` allowed
        # ``{{...}}`` embedded in a substituted value to survive and be
        # interpreted on a hypothetical second pass - a small template-
        # injection vector if user data (e.g. ``group_name``) ever
        # contained delimiter syntax. Text-shaped values are now HTML-
        # escaped AND stripped of stray ``{{`` / ``}}``. The two HTML-
        # shaped values (``{{body_html}}``, ``{{meta_bar}}``) are already
        # rendered HTML and stay trusted - they're generated upstream by
        # us, not by the user.
        import html as _html

        def _sanitize_text_value(v) -> str:
            """Escape HTML and strip ``{{``/``}}`` from a plain-text substitution value."""
            s = "" if v is None else str(v)
            s = s.replace("{{", "").replace("}}", "")
            return _html.escape(s, quote=True)

        text_subs = {
            "{{group_name}}": _sanitize_text_value(group_name),
            "{{body_text}}": _sanitize_text_value(response_text),
            "{{searches_used}}": _sanitize_text_value(
                ", ".join(searches_used or []),
            ),
            "{{estimated_tokens}}": _sanitize_text_value(
                str(estimated_tokens or 0),
            ),
            "{{actual_tokens}}": _sanitize_text_value(
                str(actual_tokens or 0),
            ),
            "{{cost_usd}}": _sanitize_text_value(
                f"{cost_usd:.4f}" if cost_usd else "0.0000",
            ),
        }
        # HTML-shaped values: trusted (built upstream by us, contain
        # legitimate tags). NOT html.escape'd. We still defensively log
        # if they somehow contain ``{{`` so a future bug upstream is
        # visible.
        html_subs = {
            "{{body_html}}": body_html,
            "{{meta_bar}}": meta_bar,
        }
        for tok, val in html_subs.items():
            if val and ("{{" in val or "}}" in val):
                logger.warning(
                    "[!] AG email template value %s contained template "
                    "delimiters; stripping to prevent pass-2 re-interpretation.",
                    tok,
                )
                val = val.replace("{{", "").replace("}}", "")
            html_subs[tok] = val or ""

        for tok, val in {**html_subs, **text_subs}.items():
            rendered = rendered.replace(tok, val)
        return rendered

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0; padding:0; background:#f4f6f9; font-family:'Segoe UI','Helvetica Neue',Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;">
<tr><td align="center" style="padding:24px 16px;">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px; width:100%; background:#ffffff; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.06);">

  <!-- Logo header -->
  <tr>
    <td style="padding:28px 32px 16px; text-align:center; border-bottom:2px solid #e8eef4;">
      <img src="data:image/svg+xml;base64,{_LOGO_SVG_B64}" alt="SpeakesQuery" width="280" style="max-width:280px; height:auto;" />
    </td>
  </tr>

  <!-- Alert group name banner -->
  <tr>
    <td style="padding:20px 32px 8px;">
      <h1 style="margin:0; font-size:22px; font-weight:700; color:#1A5A96; letter-spacing:-0.5px;">
        {group_name}
      </h1>
      <p style="margin:6px 0 0; font-size:12px; color:#8899aa;">
        {"Alert Group - Prompt-Only Delivery" if meta.get("prompt_only") else "Alert Group - Analyst Brief"}
      </p>
    </td>
  </tr>

  <!-- Meta bar -->
  {f'''<tr>
    <td style="padding:8px 32px 0;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="padding:8px 14px; background:#f0f6fc; border-radius:6px; font-size:12px; color:#5a7a96; letter-spacing:0.2px;">
            {meta_bar}
          </td>
        </tr>
      </table>
    </td>
  </tr>''' if meta_bar else ''}

  <!-- Truncation banner (only rendered when Claude hit max_tokens) -->
  {truncation_banner}

  <!-- Prompt-only banner (only rendered when delivery_mode=prompt_only) -->
  {prompt_only_banner}

  <!-- Response body -->
  <tr>
    <td style="padding:20px 32px 28px; font-size:14px; line-height:1.7; color:#2c3e50;">
      {body_html}
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="padding:16px 32px; background:#f8fafc; border-top:1px solid #e8eef4; border-radius:0 0 12px 12px; text-align:center;">
      <p style="margin:0; font-size:11px; color:#a0aab4;">
        Generated by SpeakesQuery Alert Groups &middot; Claude AI Analysis
      </p>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────
# Async-email invocation helper
# ─────────────────────────────────────────────────────────────────────
# The email helpers below use aiosmtplib (async) but the dispatcher
# runs synchronously in a Flask request thread. ``asyncio.run(coro)``
# is the clean pattern, BUT it raises ``RuntimeError: asyncio.run()
# cannot be called from a running event loop`` if the caller is
# already inside one (e.g. pywebview's main loop, future async Flask
# contexts). Guard against that case by detecting a running loop and
# falling back to ``loop.run_until_complete`` on a fresh loop bound
# to the current thread. Audit catch 2026-04-21.
def _run_coroutine_from_sync_context(coro) -> None:
    import asyncio
    try:
        # If this works, we're NOT inside a running event loop in this
        # thread and asyncio.run is the standard path.
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop - normal case. asyncio.run spins up a fresh
        # one, runs the coroutine, and cleans up.
        asyncio.run(coro)
        return
    # Already inside an event loop → schedule via a new thread so we
    # don't deadlock. Rare path (only when Flask is hosted inside
    # pywebview's loop), but non-crashing matters.
    import threading
    result_holder = {"exc": None}

    def _runner():
        try:
            asyncio.run(coro)
        except BaseException as e:
            result_holder["exc"] = e

    t = threading.Thread(target=_runner, daemon=False)
    t.start()
    t.join()
    if result_holder["exc"] is not None:
        raise result_holder["exc"]


# ─────────────────────────────────────────────────────────────────────
# Dispatch-progress tracker (2026-04-21)
# ─────────────────────────────────────────────────────────────────────
# Module-level in-memory map keyed by group_name. Updated by the
# dispatcher at every phase boundary; queried by the UI poller at
# /api/alert-groups/<name>/dispatch-progress so a manual Run click can
# show live progress ("Feeder [4/10] ag_sec_catalysts..." / "Calling
# Claude (waited 47s)...") instead of a static "Dispatching to
# Claude..." label for the whole 1-8 minute dispatch.
#
# Each entry is a dict with:
#   phase - short machine-readable tag (e.g. "feeder_loop",
#                   "calling_claude", "email", "done")
#   phase_label - human-readable message for direct UI display
#   phase_started - monotonic seconds when the phase began (elapsed
#                   is derived by the UI, not stored)
#   run_started - monotonic seconds when the whole dispatch began
#   updated_epoch - unix seconds of the last phase update (the UI uses
#                   this to drop stale entries from dead runs)
#   feeder_idx - 1-based current feeder (during feeder_loop)
#   feeder_total - total feeders in this dispatch
#   feeder_name - name of the feeder currently executing
#   result_status - once done: "success" / "error" / "rate_limited" / ...
#   error_message - populated on terminal-error phases
#
# Entries for completed dispatches are kept for 120s after ``done`` so a
# late UI poll can still read the terminal state (useful if the poll
# interval and the dispatch completion race).
_DISPATCH_PROGRESS: dict = {}
_DISPATCH_PROGRESS_LOCK = threading.Lock()
_DISPATCH_PROGRESS_TTL_SECONDS = 120


def _dispatch_progress_set(
    group_name: str,
    phase: str,
    phase_label: str,
    **extra,
) -> None:
    """Update the shared progress record for *group_name*. Thread-safe.

    Dispatcher callers pass the current phase tag + user-facing label.
    Extra kwargs (feeder_idx, feeder_total, result_status, etc.) are
    merged into the record atomically.
    """
    import time as _time
    with _DISPATCH_PROGRESS_LOCK:
        entry = _DISPATCH_PROGRESS.get(group_name)
        if entry is None:
            entry = {
                "run_started": _time.monotonic(),
                "phase_started": _time.monotonic(),
            }
            _DISPATCH_PROGRESS[group_name] = entry
        # New phase → reset the phase timer
        if entry.get("phase") != phase:
            entry["phase_started"] = _time.monotonic()
        entry["phase"] = phase
        entry["phase_label"] = phase_label
        entry["updated_epoch"] = int(_time.time())
        for k, v in extra.items():
            entry[k] = v


def _dispatch_progress_clear_stale() -> None:
    """Drop entries that haven't been updated in ``_DISPATCH_PROGRESS_TTL_SECONDS``.

    Called lazily on each snapshot read so a crashed dispatch doesn't
    leak a stale entry forever. Two-phase: old ``done`` entries drop
    after TTL; genuinely-abandoned entries (no phase transition in the
    TTL) also drop.
    """
    import time as _time
    now = int(_time.time())
    with _DISPATCH_PROGRESS_LOCK:
        to_drop = [
            name for name, entry in _DISPATCH_PROGRESS.items()
            if now - int(entry.get("updated_epoch", 0)) > _DISPATCH_PROGRESS_TTL_SECONDS
        ]
        for name in to_drop:
            _DISPATCH_PROGRESS.pop(name, None)


def dispatch_progress_snapshot(group_name: str) -> dict | None:
    """Public reader for the Flask endpoint.

    Returns the current progress dict for ``group_name`` OR None if no
    dispatch is in-flight / finished within the TTL window. The returned
    dict is a copy so callers can't mutate the live state.
    """
    import time as _time
    _dispatch_progress_clear_stale()
    with _DISPATCH_PROGRESS_LOCK:
        entry = _DISPATCH_PROGRESS.get(group_name)
        if entry is None:
            return None
        out = dict(entry)
    # Compute elapsed times fresh on every snapshot so the UI shows
    # accurate wall-clock progress.
    now_mono = _time.monotonic()
    out["phase_elapsed_s"] = max(0, int(now_mono - out.get("phase_started", now_mono)))
    out["run_elapsed_s"] = max(0, int(now_mono - out.get("run_started", now_mono)))
    return out


class AlertGroupDispatcher:
    """Execute a single alert group dispatch."""

    def __init__(self):
        self.serializer: Optional[ResultSerializer] = None
        self.payload_builder = PayloadBuilder()

    def run(
        self,
        group: dict,
        dry_run: bool = False,
        *,
        force: bool = False,
    ) -> AlertGroupRunResult:
        """Run the full dispatch pipeline for one alert group.

        Contract: **never raises**. Every exit path - success, error,
        skipped, dry-run, AND any uncaught runtime exception - produces
        an ``AlertGroupRunResult`` AND emits an ``alert_groups`` log row.

        ``force=True`` bypasses the per-AG rate limit
        (``max_dispatches_per_day`` / ``min_interval_between_runs_hours``)
        and the circuit breaker. Intended only for manual "force run"
        clicks from the UI where the operator has seen the limit fire and
        explicitly wants to override it. Budget + freshness checks still
        run so the operator can't accidentally burn through their daily
        cost cap.
        """
        run_started = time.monotonic()
        group_name = (group or {}).get("name", "unknown")
        result = AlertGroupRunResult(group_name=group_name)

        # Per-AG dry-run gate (2026-05-16, Phase 6 / Bet 5 slice 2).
        # Operators set ``dry_run: true`` on an AG YAML to fire the
        # full feeder loop + prompt build but SHORT-CIRCUIT the LLM
        # call. The dispatcher's existing dry_run path (line ~1022)
        # already supports this; we just OR-in the YAML field so the
        # operator can flip it via the UI without code changes. The
        # parameter `dry_run=True` (passed from /api/alert-groups/.../run
        # with `?dry_run=1`) wins on equal footing - either source
        # turning dry_run on suffices. Honors the "money-leak canary"
        # rule pattern: writing dry_run=true must produce ZERO billable
        # LLM calls.
        yaml_dry_run = bool((group or {}).get("dry_run", False))
        if yaml_dry_run and not dry_run:
            dry_run = True
            logger.info(
                "[i] AG '%s': dry_run=true on the AG YAML - short-circuiting LLM call.",
                group_name,
            )

        # M-AN-11 (2026-04-22): acquire a cross-process file lock so a
        # manual "Run Now" can't race an APScheduler cron fire on the
        # same AG. If another dispatch is already holding the lock, we
        # return status='skipped' with a descriptive error_message and
        # emit the log + audit row so it's observable in the UI history.
        with _acquire_dispatch_lock(group_name) as acquired:
            if not acquired:
                result.status = "skipped"
                result.error_message = (
                    "Another dispatch for this alert group is already in "
                    "progress (cross-process lock). Skipping this invocation."
                )
                _dispatch_progress_set(
                    group_name, phase="done_skipped",
                    phase_label="Skipped - already running",
                    run_started_epoch=int(time.time()),
                    dry_run=dry_run, force=force,
                )
                try:
                    self._log_run(result)
                    self._emit_log(result, run_started, dry_run=dry_run)
                except Exception:
                    logger.warning(
                        "[!] AG '%s': failed to log skipped-due-to-lock run.",
                        group_name,
                    )
                return result

            # Initialise progress tracking AFTER lock acquisition so the
            # UI reflects the real dispatch start, not a would-be one.
            _dispatch_progress_set(
                group_name, phase="starting",
                phase_label="Preparing dispatch…",
                run_started_epoch=int(time.time()),
                dry_run=dry_run, force=force,
            )
            return self._run_locked(group, dry_run, result, run_started, force=force)

    def _run_locked(self, group, dry_run, result, run_started, *, force):
        """Inner body of :meth:`run`, executed under the AG dispatch lock."""
        group_name = result.group_name
        try:
            final = self._run_inner(group, dry_run, result, run_started,
                                    force=force)
        except BaseException as exc:
            # Defense in depth. The inner method is already structured to
            # emit on every explicit exit, but a malformed prompt, a bad
            # serializer output, or any unexpected KeyError will reach
            # here - we guarantee the log row + audit row + failure email
            # regardless.
            result.status = "error"
            result.error_message = (
                f"Uncaught dispatcher exception: {type(exc).__name__}: {exc}"
            )
            logger.error(
                "[x] Alert group '%s' dispatcher uncaught exception: %s",
                group_name, exc,
            )
            try:
                self._log_run(result)
            except Exception:
                pass
            try:
                self._emit_log(result, run_started, dry_run=dry_run)
            except Exception:
                pass
            try:
                self._maybe_send_failure_email(result)
            except Exception:
                pass
            final = result
        # Record the terminal phase so the UI poller sees the final
        # status even if the dispatch finished between polls. Mapped
        # to one of: "done_success" / "done_error" / "done_skipped" /
        # "done_rate_limited" / "done_dry_run" / "done_prompt_only"
        # so the UI can style accordingly.
        terminal_status = final.status or "error"
        _dispatch_progress_set(
            group_name,
            phase=f"done_{terminal_status}",
            phase_label=f"Dispatch complete ({terminal_status}).",
            result_status=terminal_status,
            error_message=final.error_message or "",
            searches_used=list(final.searches_used or []),
            estimated_tokens=int(final.estimated_tokens or 0),
            actual_tokens=int(final.actual_tokens or 0),
            cost_usd=float(final.cost_usd or 0.0),
        )
        return final

    def _run_inner(
        self,
        group: dict,
        dry_run: bool,
        result: AlertGroupRunResult,
        run_started: float,
        *,
        force: bool = False,
    ) -> AlertGroupRunResult:
        group_name = result.group_name

        # ── Rate limit: max_dispatches_per_day / min_interval_between_runs_hours
        # Applied BEFORE the circuit breaker so a user who accidentally
        # scheduled a twice-daily cron but wants daily-only output sees a
        # specific ``rate_limited`` status rather than eating budget.
        # Both fields are optional per-AG YAML overrides; leave unset for
        # unlimited (the default historical behaviour).
        # ``force=True`` (manual-run override) bypasses this check.
        if not force:
            rate_err = self._check_rate_limit(group, group_name)
            if rate_err:
                result.status = "rate_limited"
                # Self-documenting error: tell the user EXACTLY where to
                # change the limit, since this is a per-AG field, not a
                # global setting. Caught 2026-04-20: user saw the error
                # and went to global Settings looking for it.
                result.error_message = (
                    f"{rate_err}. This is a per-group setting configured "
                    f"on the Alert Group (click Edit on '{group_name}' → "
                    f"Advanced section). Call the Run endpoint with "
                    f"force=true to bypass the limit for a single manual "
                    f"dispatch."
                )
                logger.info(
                    "[i] Alert group '%s' skipped (rate_limited): %s",
                    group_name, rate_err,
                )
                self._log_run(result)
                self._emit_log(result, run_started, dry_run=dry_run)
                # Rate-limiting is NORMAL operation, not an error - don't fire
                # the failure email or trip the circuit breaker.
                return result

        # ── Circuit breaker (half-open since 2026-08-04): a tripped
        # breaker no longer blocks forever. During the cooldown window
        # (``alert_group_circuit_breaker_cooldown_hours``, default 20h)
        # the dispatch is skipped CLEANLY - status "skipped", no failure
        # email (the trip itself already sent one), no error-streak
        # growth. Once the cooldown elapses, the next dispatch proceeds
        # as a half-open probe: the tripped_at timestamp is refreshed
        # immediately (so a failing probe waits a full cooldown before
        # the next attempt) and a succeeding run closes the breaker at
        # the success exit. Manual reset via
        # POST /api/alert-groups/<name>/reset-circuit-breaker still
        # works, and ``force=True`` still bypasses the breaker entirely.
        # Pre-2026-08-04 behaviour (skip forever + daily failure email
        # until manual reset) turned one bad week into a silent outage:
        # daily_opportunity_brief sat tripped for 6 days straight.
        if not force and group.get("circuit_breaker_tripped"):
            probe_ok, wait_msg = self._circuit_breaker_probe_state(group)
            if not probe_ok:
                result.status = "skipped"
                result.error_message = wait_msg
                logger.warning(
                    "[!] Alert group '%s' skipped (circuit breaker cooling "
                    "down): %s", group_name, wait_msg,
                )
                self._log_run(result)
                self._emit_log(result, run_started, dry_run=dry_run)
                return result
            logger.warning(
                "[!] Alert group '%s': circuit breaker HALF-OPEN probe - "
                "cooldown elapsed, attempting a real dispatch. Success "
                "closes the breaker; failure restarts the cooldown.",
                group_name,
            )
            self._touch_circuit_breaker_timestamp(group_name)

        # Gate: disabled group
        if group.get("disabled", False):
            result.status = "skipped"
            result.error_message = "Group is disabled."
            logger.info("[i] Alert group '%s' skipped (disabled).", group_name)
            self._log_run(result)
            self._emit_log(result, run_started, dry_run=dry_run)
            return result

        # Initialise serializer with per-group max_rows. This is the PER-SEARCH
        # row cap: every search output is truncated to this many rows before
        # being serialized for Claude (serializer.py applies df.head(max_rows)
        # during serialize()). Tested by tests/test_alert_group_row_cap.py.
        max_rows = int(group.get("max_rows", 200))
        if self.serializer is None:
            self.serializer = ResultSerializer(max_rows=max_rows)
        else:
            self.serializer.max_rows = max_rows

        # Serialize each search result. Strategy: run each saved search's
        # query ON DEMAND against the live indexes - no dependency on the
        # saved-search cron having fired recently. Falls back to the
        # cached result in saved_search_history.db only if the on-demand
        # execution fails for some reason.
        search_names = group.get("search_names", [])
        serialized: list[SerializedResult] = []

        # Phase-boundary logging so ``docker logs -f`` reveals exactly
        # where a long dispatch is - the 2026-04-21 "stuck at Dispatching
        # to Claude" incident was a visibility gap: the UI shows a static
        # "Dispatching to Claude..." string and the backend went silent
        # between "last feeder executed" and "dispatch complete" for the
        # duration of the web_search-enabled Claude call (can be minutes).
        feeder_loop_started = time.monotonic()
        total_feeders = len(search_names)
        logger.info(
            "[i] AG '%s': feeder loop start (%d feeders)",
            group_name, total_feeders,
        )
        _dispatch_progress_set(
            group_name,
            phase="feeder_loop",
            phase_label=f"Running feeders (0/{total_feeders})…",
            feeder_total=total_feeders,
            feeder_idx=0,
        )

        for idx, name in enumerate(search_names, start=1):
            _dispatch_progress_set(
                group_name,
                phase="feeder_loop",
                phase_label=(
                    f"Feeder [{idx}/{total_feeders}] '{name}' running…"
                ),
                feeder_total=total_feeders,
                feeder_idx=idx,
                feeder_name=name,
            )
            logger.info(
                "[i] AG '%s': feeder [%d/%d] '%s' running...",
                group_name, idx, total_feeders, name,
            )
            df = self._execute_feeder_query_now(name, group_name=group_name)
            try:
                if df is not None:
                    sr = self.serializer.serialize_df(name, df)
                else:
                    # ``_execute_feeder_query_now`` returned None - either
                    # the query errored (already logged with [!] above) OR
                    # the query ran cleanly and produced zero rows today.
                    # We still try the serializer's cache fallback so a
                    # stale-but-present cached result is better than
                    # nothing - but if both live + cached miss, skip the
                    # feeder rather than crashing the whole dispatch.
                    sr = self.serializer.serialize(name)
                serialized.append(sr)
                result.searches_used.append(name)
            except (SearchNotFoundError, EmptyResultError) as exc:
                # Emit a more helpful log than the previous generic "No
                # cached result found" - if the earlier on-demand
                # execution errored the operator has already seen the
                # real reason one line above; this line just says the
                # fallback also missed.
                logger.warning(
                    "[!] AG '%s': skipping feeder '%s' - live query "
                    "produced no data AND saved-search cache also missed "
                    "(%s). Check the feeder's query, ingestion output, or "
                    "index path.",
                    group_name, name, exc,
                )

        feeder_loop_ms = int((time.monotonic() - feeder_loop_started) * 1000)
        result.feeder_loop_ms = feeder_loop_ms
        total_rows = sum(r.row_count for r in serialized)
        logger.info(
            "[i] AG '%s': feeder loop done (%d/%d feeders produced data, "
            "%d rows total, %dms)",
            group_name, len(serialized), total_feeders, total_rows,
            feeder_loop_ms,
        )

        if not serialized:
            if group.get("skip_on_empty"):
                # Slice C1 (2026-06-23): diff-style AGs ("what changed in
                # these lists today") legitimately have nothing to report on
                # a quiet day. Treat empty feeders as a CLEAN skip - no
                # error, no failure email, no circuit-breaker tick - so a
                # quiet stretch never trips the breaker and disables the AG.
                result.status = "skipped"
                result.error_message = "No new data today (skip_on_empty)."
                logger.info(
                    "[i] Alert group '%s': no new data; skipping cleanly "
                    "(skip_on_empty).", group_name,
                )
                self._log_run(result)
                self._emit_log(result, run_started, dry_run=dry_run)
                return result
            result.status = "error"
            result.error_message = "No results available for any search in group."
            logger.warning("[!] Alert group '%s': no results available.", group_name)
            self._log_run(result)
            self._emit_log(result, run_started, dry_run=dry_run)
            self._maybe_send_failure_email(result)
            self._maybe_trip_circuit_breaker(group_name)
            return result

        # ── Prompt text gate (moved early, 2026-04-20) ────────────
        # Check before the freshness/budget gates so an empty prompt text
        # produces a specific actionable error instead of getting masked by
        # a freshness warning prepend. Regression test:
        # tests/test_alert_groups.py::test_missing_prompt_text_returns_error.
        if not (group.get("prompt_text") or "").strip():
            result.status = "error"
            result.error_message = "No prompt text configured for this alert group."
            self._log_run(result)
            self._emit_log(result, run_started, dry_run=dry_run)
            self._maybe_send_failure_email(result)
            # Trip the breaker on repeated config errors too - if the
            # operator has deleted the prompt_text and left the AG
            # enabled, the breaker prevents infinite daily failure
            # emails (audit 2026-04-21).
            self._maybe_trip_circuit_breaker(group_name)
            return result

        # NOTE: the saved-search-cache freshness check that used to live
        # here is now vestigial - ``_execute_feeder_query_now`` runs each
        # query fresh against current indexed data, so the dispatcher no
        # longer depends on the history-DB cache. ``_check_feeder_freshness``
        # is preserved (still unit-tested) for callers that explicitly want
        # to audit the cache; see
        # ``tests/test_alert_group_hardening.py::TestFeederFreshness``. A
        # future "raw data age" check based on ``indexes/<subdir>/`` mtimes
        # can replace it.

        # Prompt text has already been validated above; use the (possibly
        # freshness-annotated) version from the group dict. We need it now
        # so the budget estimator below can render the built prompt, which
        # matches what actually flies to Claude.
        prompt_text = (group.get("prompt_text") or "").strip()

        # Closure for the trim-to-budget loop AND the pre-trim estimate.
        # Renders the full user_content via PayloadBuilder (minus the
        # message-envelope overhead, which is a flat ~20 tokens shared by
        # every request).
        def _build_prompt_text(_results):
            msgs = self.payload_builder.build(group_name, _results, prompt_text)
            return msgs[0]["content"] if msgs else ""

        # H-AN-5 (2026-04-21): budget gate uses the built-prompt estimate.
        # Previously we summed ``estimated_tokens`` per-block, missing the
        # builder's wrapper markdown (headers, fences, metadata lines,
        # separators) - easily several hundred tokens on a 10-feeder AG.
        built_initial = _build_prompt_text(serialized)
        total_estimated = ResultSerializer.estimate_prompt_tokens(built_initial)
        result.estimated_tokens = total_estimated

        budget_gate = self._get_budget_gate()
        if budget_gate and total_estimated > budget_gate:
            serialized = self._trim_to_budget(
                serialized, budget_gate, build_prompt_fn=_build_prompt_text,
            )
            if not serialized:
                result.status = "error"
                result.error_message = "All results trimmed - nothing to send."
                self._log_run(result)
                self._emit_log(result, run_started, dry_run=dry_run)
                self._maybe_send_failure_email(result)
                return result
            result.estimated_tokens = ResultSerializer.estimate_prompt_tokens(
                _build_prompt_text(serialized),
            )

        # Build the messages payload
        messages = self.payload_builder.build(group_name, serialized, prompt_text)

        if dry_run:
            import json as _json
            result.status = "dry_run"
            result.response_text = _json.dumps(
                {"messages": messages, "searches_used": result.searches_used,
                 "estimated_tokens": result.estimated_tokens},
                default=str,
            )
            logger.info(
                "[i] Alert group '%s' dry-run complete (no Claude call, no email).",
                group_name,
            )
            self._log_run(result)
            self._emit_log(result, run_started, dry_run=True)
            return result

        # ── Budget-friendly prompt-only mode (2026-04-22) ─────────
        # Skip the Claude API call entirely and email the built prompt so
        # the operator can paste it into Claude.ai manually. Cost stays
        # $0.00; the feeder loop, freshness gates, prompt-text gate, and
        # token-trim loop still ran above - so the recipient gets exactly
        # what the API path would have sent. The per-AG cost-budget gate
        # is intentionally bypassed (no cost to gate against), but the
        # rate-limit + circuit-breaker gates upstream still apply.
        delivery_mode = (group.get("delivery_mode") or "api").strip().lower()
        if delivery_mode == "prompt_only":
            return self._deliver_prompt_only(
                group=group,
                group_name=group_name,
                result=result,
                serialized=serialized,
                prompt_text=prompt_text,
                run_started=run_started,
                dry_run=dry_run,
            )

        # ── Per-AG cost budget (pre-flight) ──────────────────────
        budget_err = self._check_per_ag_budget(group, group_name, total_estimated)
        if budget_err:
            result.status = "error"
            result.error_message = budget_err
            logger.warning(
                "[!] Alert group '%s' skipped per-AG budget: %s",
                group_name, budget_err,
            )
            self._log_run(result)
            self._emit_log(result, run_started, dry_run=dry_run)
            self._maybe_send_failure_email(result)
            return result

        # ── LLM dispatch (Slice A, 2026-06-23) ────────────────────
        # Claude API by default, or the provider-agnostic LLM router when
        # this AG pins a registry ``model_id`` (e.g. a local LAN model like
        # llamacpp-qwen35-122b-a10b, $0/token). Both branches converge on
        # ``(call, response_text, response_meta)`` so every downstream
        # surface - empty-text guard, pick extraction, email,
        # logging - stays provider-agnostic.
        max_tokens = self._max_tokens(group)
        local_model_id = (group.get("model_id") or "").strip()
        claude_started = time.monotonic()

        if local_model_id:
            # Local / router path. Single-shot completion: NO web_search
            # tool (Anthropic-only - a local model can't use it), and the
            # per-record timeout (e.g. 600s for the 122B) applies rather
            # than the Claude knob. A local model costs $0.
            model = local_model_id
            logger.info(
                "[i] AG '%s': calling local model via LLM router "
                "(model_id=%s, max_tokens=%d, est_input_tokens=%d, "
                "no web_search tool). Local inference can take 1-5 minutes.",
                group_name, model, max_tokens, result.estimated_tokens,
            )
            _dispatch_progress_set(
                group_name,
                phase="calling_local_llm",
                phase_label=(
                    f"Calling local model ({model}, ≤{max_tokens} output, "
                    f"est. {result.estimated_tokens:,} input tokens). "
                    f"Local inference typically takes 1-5 minutes."
                ),
                claude_model=model,
                claude_max_tokens=max_tokens,
                claude_est_input_tokens=int(result.estimated_tokens),
            )
            try:
                call, response_text, response_meta = (
                    self._call_router_llm_with_retry(
                        group_name=group_name,
                        model_id=model,
                        user_content=(
                            messages[0]["content"] if messages else ""
                        ),
                        max_tokens=max_tokens,
                    )
                )
            except Exception as exc:
                claude_ms = int((time.monotonic() - claude_started) * 1000)
                result.claude_call_ms = claude_ms
                result.status = "error"
                result.error_message = f"Local LLM call failed: {exc}"
                logger.error(
                    "[x] AG '%s': local LLM error after %dms: %s",
                    group_name, claude_ms, exc,
                )
                self._log_run(result)
                self._emit_log(result, run_started, dry_run=dry_run)
                self._maybe_send_failure_email(result)
                self._maybe_send_salvage_prompt_email(
                    group=group, group_name=group_name,
                    serialized=serialized, prompt_text=prompt_text,
                    failure_reason=str(exc),
                )
                self._maybe_trip_circuit_breaker(group_name)
                return result
        else:
            # Call Claude API via the shared wrapper - retries, timeout, and
            # both log surfaces (Parquet + SQLite history) are handled there.
            model = self._model_choice(group)
            timeout_s = int(self._get_setting("claude_request_timeout_seconds", 120))
            retry_attempts = int(self._get_setting("claude_retry_attempts", 3))
            # Headroom (2026-06-23): resolve whether this AG's call routes
            # through the compression proxy. Per-AG ``use_headroom``
            # tri-state beats the global default; the wrapper fails open to
            # direct Anthropic if the proxy is unreachable.
            from analyzers.headroom import resolve_use_headroom
            use_headroom = resolve_use_headroom(
                group_override=group.get("use_headroom"),
            )
            # Pre-flight log - this is the single most important visibility
            # beacon on a stuck dispatch. A web_search-enabled Claude call can
            # legitimately run for 2-10 minutes; without this line operators
            # cannot tell the difference between "Claude is thinking" and
            # "something is wedged". See tests/test_alert_group_dispatch_logging.py.
            logger.info(
                "[i] AG '%s': calling Claude (model=%s, max_tokens=%d, "
                "est_input_tokens=%d, timeout=%ds, retry_attempts=%d, "
                "tools=web_search, route=%s)",
                group_name, model, max_tokens, result.estimated_tokens,
                timeout_s, retry_attempts,
                "headroom" if use_headroom else "direct",
            )
            _dispatch_progress_set(
                group_name,
                phase="calling_claude",
                phase_label=(
                    f"Calling Claude ({model}, ≤{max_tokens} output, "
                    f"est. {result.estimated_tokens:,} input tokens, "
                    f"timeout {timeout_s}s, web_search enabled). This "
                    f"typically takes 2-5 minutes."
                ),
                claude_model=model,
                claude_max_tokens=max_tokens,
                claude_est_input_tokens=int(result.estimated_tokens),
                claude_timeout_s=timeout_s,
            )
            # Prompt caching (2026-08-04): mark the built prompt as a
            # cache breakpoint so the SERVER-SIDE web_search loop re-reads
            # the growing prefix at ~0.1x instead of re-billing it at full
            # price every iteration. The first uncapped Opus 5 run paid
            # full freight on 716k input tokens for a 38k prompt - the
            # search loop re-processed the same context ~19x uncached.
            # Block-level cache_control is supported by every SDK version
            # in play (top-level auto-caching is not).
            cached_messages = messages
            if messages and isinstance(messages[0].get("content"), str):
                cached_messages = [{
                    "role": messages[0].get("role", "user"),
                    "content": [{
                        "type": "text",
                        "text": messages[0]["content"],
                        "cache_control": {"type": "ephemeral"},
                    }],
                }] + list(messages[1:])
            try:
                call: ClaudeCallResult = call_messages_create(
                    source="alert_group",
                    group_name=group_name,
                    model=model,
                    max_tokens=max_tokens,
                    messages=cached_messages,
                    tools=[self._web_search_tool_for(model)],
                    use_headroom=use_headroom,
                )
            except ClaudeCallError as exc:
                claude_ms = int((time.monotonic() - claude_started) * 1000)
                result.claude_call_ms = claude_ms
                result.status = "error"
                result.error_message = f"Claude API call failed: {exc}"
                logger.error(
                    "[x] AG '%s': Claude API error after %dms: %s",
                    group_name, claude_ms, exc,
                )
                self._log_run(result)
                self._emit_log(result, run_started, dry_run=dry_run)
                self._maybe_send_failure_email(result)
                self._maybe_send_salvage_prompt_email(
                    group=group, group_name=group_name,
                    serialized=serialized, prompt_text=prompt_text,
                    failure_reason=str(exc),
                )
                self._maybe_trip_circuit_breaker(group_name)
                return result

            # M-AN-12 (2026-04-22): capture structured response meta so the
            # empty-text fail-fast branch below can cite concrete diagnostics
            # (block types, block count) in the audit row, not just log them.
            response_text, response_meta = self._extract_response_meta(call.response)
        result.response_text = response_text
        result.actual_tokens = call.input_tokens + call.output_tokens
        result.cost_usd = call.cost_usd

        claude_ms = int((time.monotonic() - claude_started) * 1000)
        result.claude_call_ms = claude_ms
        _stop_reason = response_meta.get("stop_reason")

        # H-AN-2 (2026-04-21): an empty response_text means Claude returned
        # content blocks but none contained text (tool-only turn, refusal
        # with no prose, or an incomplete max_tokens cutoff). Without this
        # guard the dispatcher continues to pick extraction + email
        # delivery, producing a blank analyst brief with status='success'.
        # Fail fast so the operator gets a failure-email + circuit-breaker
        # tick instead of a silent dud. Mirror the Claude-exception path
        # above so every exit routes through the same telemetry.
        if not response_text.strip():
            result.status = "error"
            result.error_message = (
                f"LLM response contained no text "
                f"(stop_reason={_stop_reason}, "
                f"in={call.input_tokens}, out={call.output_tokens}, "
                f"blocks={response_meta.get('block_count', 0)}, "
                f"block_types={response_meta.get('block_types', [])}). "
                "No brief to email."
            )
            logger.error(
                "[x] AG '%s': %s", group_name, result.error_message,
            )
            self._log_run(result)
            self._emit_log(result, run_started, dry_run=dry_run)
            self._maybe_send_failure_email(result)
            self._maybe_send_salvage_prompt_email(
                group=group, group_name=group_name,
                serialized=serialized, prompt_text=prompt_text,
                failure_reason=result.error_message,
            )
            self._maybe_trip_circuit_breaker(group_name)
            return result
        logger.info(
            "[i] AG '%s': Claude returned (in=%d, out=%d, stop=%s, "
            "cost=$%.4f, latency=%dms, attempts=%d, route=%s)",
            group_name, call.input_tokens, call.output_tokens,
            _stop_reason, call.cost_usd, claude_ms, call.attempts,
            getattr(call, "path", "direct"),
        )
        _dispatch_progress_set(
            group_name,
            phase="claude_returned",
            phase_label=(
                f"Claude returned ({call.input_tokens:,} in + "
                f"{call.output_tokens:,} out, ${call.cost_usd:.4f}, "
                f"{claude_ms // 1000}s). Extracting picks…"
            ),
            claude_input_tokens=call.input_tokens,
            claude_output_tokens=call.output_tokens,
            claude_cost_usd=float(call.cost_usd),
            claude_latency_ms=claude_ms,
            stop_reason=_stop_reason or "",
        )

        # Extract structured picks from the trailing fenced JSON block
        # in Claude's response. Writes one row per pick to
        # indexes/IMMUTABLE/ag_picks/*.parquet for backtesting, alerting, and
        # the next dispatch's "reserved picks" feeder. Failures here are
        # non-fatal - the brief still ships, we just lose capture for
        # this one run and log a warning. Truncated briefs (``stop_reason
        # == "max_tokens"``) may legitimately not contain a JSON block.
        pick_count: int | None = None
        try:
            pick_count = self._extract_and_log_picks(
                response_text=response_text,
                group_name=group_name,
                run_request_id=call.request_id,
                model_used=model,
            )
            if pick_count > 0:
                logger.info(
                    "[i] AG '%s': captured %d pick(s) into indexes/IMMUTABLE/ag_picks/",
                    group_name, pick_count,
                )
        except Exception as exc:
            logger.warning(
                "[!] AG '%s': pick extraction failed (dispatch continues): %s",
                group_name, exc,
            )

        # Wave 2 of OEB (2026-04-26): if this dispatch was the weekly
        # performance review AG, parse the structured observations
        # object (an OBJECT, not a picks ARRAY) and persist to
        # indexes/IMMUTABLE/ag_picks_review_observations/. Picks
        # extraction above will return zero for this AG (the JSON tail
        # is shaped differently); this parallel path handles it.
        if group_name == "options_performance_review":
            try:
                obs_count = self._extract_and_log_review_observations(
                    response_text=response_text,
                    group_name=group_name,
                    run_request_id=call.request_id,
                )
                if obs_count > 0:
                    logger.info(
                        "[i] AG '%s': captured %d review row(s) into "
                        "indexes/IMMUTABLE/ag_picks_review_observations/",
                        group_name, obs_count,
                    )
            except Exception as exc:
                logger.warning(
                    "[!] AG '%s': review extraction failed (dispatch continues): %s",
                    group_name, exc,
                )

        # ── Email body selection (2026-08-04) ─────────────────────
        # When the AG sets ``email_digest_model_id``, the raw analyst
        # output is distilled into a BLUF-first readable report by a
        # local $0 model before emailing. The RAW response always
        # survives in full: pick extraction above already ran on it,
        # and it ships as the .md attachment. Digest failure falls back
        # to the raw text minus the machine JSON tail - never a lost
        # brief.
        email_body = response_text
        digest_used = False
        if (group.get("email_digest_model_id") or "").strip():
            digest = self._build_digest_email_body(
                group=group, group_name=group_name,
                response_text=response_text,
            )
            if digest:
                email_body = digest
                digest_used = True
            else:
                email_body = self._strip_json_tail(response_text)

        # Send email
        email_address = group.get("email_address", "").strip()
        if email_address:
            logger.info(
                "[i] AG '%s': sending email to %s",
                group_name, email_address,
            )
            _dispatch_progress_set(
                group_name,
                phase="sending_email",
                phase_label=f"Sending email to {email_address}…",
                email_to=email_address,
            )
            email_started = time.monotonic()
            try:
                import datetime as _dt
                subject_date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
                # Warn loudly when Claude hit its output cap - operator
                # needs to know the analyst brief got truncated even if the
                # email looks fine at a glance.
                stop_reason = getattr(call.response, "stop_reason", None)
                truncated = stop_reason == "max_tokens"
                subject_suffix = " - TRUNCATED" if truncated else ""
                # BLUF at subject level: when the digest path ran and the
                # pick journaler parsed a count, put it in the subject so
                # the reader knows whether to open the email at all.
                if digest_used and pick_count is not None:
                    plural = "" if pick_count == 1 else "s"
                    subject_suffix = (
                        f" - {pick_count} pick{plural}{subject_suffix}"
                    )
                self._send_html_email(
                    subject=(
                        f"[SpeakesQuery REPORT] {group_name} - {subject_date}"
                        f"{subject_suffix}"
                    ),
                    plain_body=email_body,
                    group_name=group_name,
                    to_addrs=email_address,
                    meta={
                        "searches_used": result.searches_used,
                        "estimated_tokens": result.estimated_tokens,
                        "actual_tokens": result.actual_tokens,
                        "cost_usd": result.cost_usd,
                        "truncated": truncated,
                        "stop_reason": stop_reason,
                        "digest": digest_used,
                        "digest_model": (
                            (group.get("email_digest_model_id") or "").strip()
                            if digest_used else ""
                        ),
                    },
                    template_override=(group.get("email_template_override") or ""),
                    attach_markdown=True,
                    attachment_text=response_text,
                )
            except Exception as exc:
                email_ms = int((time.monotonic() - email_started) * 1000)
                result.email_send_ms = email_ms
                logger.error(
                    "[x] AG '%s': email send failed after %dms: %s",
                    group_name, email_ms, exc,
                )
                result.status = "error"
                result.error_message = f"Email send failed: {exc}"
                self._log_run(result)
                self._emit_log(result, run_started, dry_run=dry_run)
                self._maybe_send_failure_email(result)
                self._maybe_trip_circuit_breaker(group_name)
                return result
            email_ms = int((time.monotonic() - email_started) * 1000)
            result.email_send_ms = email_ms
            logger.info(
                "[i] AG '%s': email sent (%dms)", group_name, email_ms,
            )

        result.status = "success"
        # A successful run resets the consecutive-failure counter (the
        # circuit breaker only trips on a streak).
        self._reset_consecutive_failure_count(group_name)
        total_ms = int((time.monotonic() - run_started) * 1000)
        logger.info(
            "[i] AG '%s': dispatch complete (%d searches, %d est. tokens, "
            "total %dms).",
            group_name, len(serialized), result.estimated_tokens, total_ms,
        )
        self._log_run(result)
        self._emit_log(result, run_started, dry_run=dry_run)
        return result

    # ------------------------------------------------------------------
    # Prompt-only delivery (budget-friendly mode, 2026-04-22)
    # ------------------------------------------------------------------

    def _deliver_prompt_only(
        self,
        *,
        group: dict,
        group_name: str,
        result: AlertGroupRunResult,
        serialized: list[SerializedResult],
        prompt_text: str,
        run_started: float,
        dry_run: bool,
    ) -> AlertGroupRunResult:
        """Email the built Claude prompt instead of calling the API.

        Cost stays $0.00. The feeder loop, freshness gates, prompt-text
        gate, and token-trim loop have already run in ``_run_inner``, so
        ``serialized`` and ``prompt_text`` are already trim-to-budget and
        ready to embed. We re-use ``PayloadBuilder.build_user_content``
        to produce the EXACT string the API path would have sent - no
        drift between the two modes.

        Side effects mirror the success path: audit row, Parquet log row,
        failure-reset on the consecutive-error counter. Status is set to
        ``prompt_only`` so operators can distinguish prompt-only deliveries
        from full API runs in ``alert_group_runs.sqlite``.
        """
        email_address = (group.get("email_address") or "").strip()
        # Validator already requires email_address when delivery_mode is
        # prompt_only, but defend against a hand-edited YAML sneaking past
        # the API. A prompt with nowhere to go is a silent failure.
        if not email_address:
            result.status = "error"
            result.error_message = (
                "delivery_mode='prompt_only' requires email_address. "
                "Edit the alert group and set a recipient, or switch "
                "delivery_mode back to 'api'."
            )
            logger.error(
                "[x] AG '%s': prompt_only mode with empty email_address.",
                group_name,
            )
            self._log_run(result)
            self._emit_log(result, run_started, dry_run=dry_run)
            self._maybe_send_failure_email(result)
            self._maybe_trip_circuit_breaker(group_name)
            return result

        _dispatch_progress_set(
            group_name,
            phase="building_prompt_email",
            phase_label=(
                f"Prompt-only mode - building email to {email_address} "
                f"(no Claude API call; cost $0.00)."
            ),
            email_to=email_address,
        )

        # Exact same string the API path ships in messages[0].content -
        # the recipient pastes this verbatim into Claude.ai and gets the
        # same inputs Claude would have seen.
        prompt_body = self.payload_builder.build_user_content(
            group_name, serialized, prompt_text,
        )

        import datetime as _dt
        subject_date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        subject = f"[SpeakesQuery PROMPT] {group_name} - {subject_date}"

        _dispatch_progress_set(
            group_name,
            phase="sending_email",
            phase_label=f"Sending prompt email to {email_address}…",
            email_to=email_address,
        )
        email_started = time.monotonic()
        try:
            self._send_html_email(
                subject=subject,
                plain_body=prompt_body,
                group_name=group_name,
                to_addrs=email_address,
                meta={
                    "searches_used": result.searches_used,
                    "estimated_tokens": result.estimated_tokens,
                    "actual_tokens": 0,
                    "cost_usd": 0.0,
                    "prompt_only": True,
                },
                template_override=(group.get("email_template_override") or ""),
                attach_markdown=True,
            )
        except Exception as exc:
            email_ms = int((time.monotonic() - email_started) * 1000)
            result.email_send_ms = email_ms
            logger.error(
                "[x] AG '%s': prompt-only email send failed after %dms: %s",
                group_name, email_ms, exc,
            )
            result.status = "error"
            result.error_message = f"Email send failed: {exc}"
            self._log_run(result)
            self._emit_log(result, run_started, dry_run=dry_run)
            self._maybe_send_failure_email(result)
            self._maybe_trip_circuit_breaker(group_name)
            return result

        email_ms = int((time.monotonic() - email_started) * 1000)
        result.email_send_ms = email_ms
        result.status = "prompt_only"
        result.actual_tokens = 0
        result.cost_usd = 0.0
        result.response_text = prompt_body
        # A successful delivery (even one that skipped Claude) counts as
        # a healthy run for circuit-breaker purposes.
        self._reset_consecutive_failure_count(group_name)
        total_ms = int((time.monotonic() - run_started) * 1000)
        logger.info(
            "[i] AG '%s': prompt-only dispatch complete "
            "(%d searches, %d est. tokens, email %dms, total %dms).",
            group_name, len(serialized), result.estimated_tokens,
            email_ms, total_ms,
        )
        self._log_run(result)
        self._emit_log(result, run_started, dry_run=dry_run)
        return result

    def _maybe_send_salvage_prompt_email(
        self,
        *,
        group: dict,
        group_name: str,
        serialized: list,
        prompt_text: str,
        failure_reason: str,
    ) -> None:
        """Best-effort data-preservation email after a terminal LLM failure.

        Added 2026-08-04. By the time the LLM call fails, every feeder has
        already run and the prompt is fully built - throwing that away
        means the day's data is simply lost (feeders like the trending-
        repos dedup only surface a repo ONCE). When the LLM call fails
        after all retries, email the built prompt to the AG's normal
        recipient (prompt_only-style) so the reader can still get the
        analysis manually. Gated by
        ``alert_group_llm_failure_prompt_fallback`` (default True).

        Never raises; the run keeps status='error' either way so the
        circuit breaker and failure telemetry are unaffected.
        """
        try:
            if not bool(self._get_setting(
                    "alert_group_llm_failure_prompt_fallback", True)):
                return
            email_address = (group.get("email_address") or "").strip()
            if not email_address or not serialized:
                return
            prompt_body = self.payload_builder.build_user_content(
                group_name, serialized, prompt_text,
            )
            import datetime as _dt
            subject_date = _dt.datetime.now(
                _dt.timezone.utc,
            ).strftime("%Y-%m-%d")
            self._send_html_email(
                subject=(
                    f"[SpeakesQuery SALVAGE] {group_name} - {subject_date} "
                    f"- LLM failed, data preserved"
                ),
                plain_body=prompt_body,
                group_name=group_name,
                to_addrs=email_address,
                meta={
                    "searches_used": [
                        getattr(s, "search_name", "") for s in serialized
                    ],
                    "actual_tokens": 0,
                    "cost_usd": 0.0,
                    "prompt_only": True,
                    "salvage": True,
                    "failure_reason": failure_reason,
                },
                template_override=(group.get("email_template_override") or ""),
                attach_markdown=True,
            )
            logger.info(
                "[i] AG '%s': salvage prompt email sent to %s (LLM failure: "
                "%s)", group_name, email_address, failure_reason,
            )
        except Exception as exc:
            logger.warning(
                "[!] AG '%s': salvage prompt email failed (data only in "
                "logs now): %s", group_name, exc,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_budget_gate() -> Optional[int]:
        """Read the token budget gate from global settings."""
        try:
            from global_settings import get_settings
            settings = get_settings()
            budget = settings.get("claude_analyzer_daily_budget_cents") or 50
            return int(budget / 0.3 * 1000)
        except Exception:
            return None

    @staticmethod
    def _trim_to_budget(results: list, budget: int, *, build_prompt_fn=None) -> list:
        """Iteratively halve row counts on the largest results until under budget.

        H-AN-4 (2026-04-21): the old JSON branch assumed a top-level list and
        silently no-op'd on any other shape (``data = data[:new_rows]`` raised
        TypeError for dict/scalar, which the bare except swallowed). The loop
        then iterated 10× without shrinking content, leaving prompts that
        could blow the per-AG token budget. Now each shape is handled
        explicitly; an untrimmable shape breaks the outer loop with a
        WARNING so the operator sees the unreached budget instead of a
        silent exceed.

        H-AN-5 (2026-04-21): budget gate now measured against the
        fully-built prompt (wrappers + headers + metadata included) when
        ``build_prompt_fn`` is provided. The caller (``run``) supplies a
        lambda that invokes ``PayloadBuilder.build`` with the current
        trimmed list and returns the rendered user_content string. For
        backward compat + isolation tests, absence of the callable falls
        back to ``sum(r.estimated_tokens)`` (old behaviour).
        """
        from alert_groups.serializer import ResultSerializer

        def _total(rows):
            if build_prompt_fn is not None:
                try:
                    built = build_prompt_fn(rows)
                    return ResultSerializer.estimate_prompt_tokens(built)
                except Exception as exc:
                    # If the builder chokes on some intermediate trim, log
                    # and fall back to the per-block sum rather than
                    # aborting. Better to ship a slightly-over-budget
                    # prompt than no prompt at all.
                    logger.warning(
                        "[!] Budget trim: build_prompt_fn failed (%s); "
                        "falling back to per-block sum for this iteration.",
                        exc,
                    )
            return sum(r.estimated_tokens for r in rows)

        trimmed = list(results)
        for _ in range(10):
            total = _total(trimmed)
            if total <= budget:
                return trimmed
            largest = max(trimmed, key=lambda r: r.estimated_tokens)
            idx = trimmed.index(largest)
            new_rows = max(1, largest.row_count // 2)
            new_content = largest.content
            untrimmable = False
            if largest.format == "json":
                import json
                try:
                    data = json.loads(new_content)
                except Exception:
                    # Malformed JSON in a serialized result is itself a bug;
                    # don't loop uselessly trying to trim it.
                    logger.warning(
                        "[!] Budget trim: cannot parse JSON content for "
                        "feeder %r - aborting trim loop.",
                        getattr(largest, "search_name", "<unknown>"),
                    )
                    break

                if isinstance(data, list):
                    data = data[:new_rows]
                elif isinstance(data, dict) and isinstance(data.get("records"), list):
                    data["records"] = data["records"][:new_rows]
                elif isinstance(data, dict) and isinstance(data.get("rows"), list):
                    data["rows"] = data["rows"][:new_rows]
                else:
                    logger.warning(
                        "[!] Budget trim: cannot trim JSON of shape %s for "
                        "feeder %r; exiting trim loop at total=%d tokens, "
                        "budget=%d.",
                        type(data).__name__,
                        getattr(largest, "search_name", "<unknown>"),
                        total, budget,
                    )
                    untrimmable = True

                if untrimmable:
                    break

                try:
                    new_content = json.dumps(data, default=str)
                except Exception as exc:
                    logger.warning(
                        "[!] Budget trim: json.dumps failed for feeder %r: %s",
                        getattr(largest, "search_name", "<unknown>"), exc,
                    )
                    break
            else:
                lines = new_content.splitlines()
                new_content = "\n".join(lines[:new_rows + 1])

            trimmed[idx] = SerializedResult(
                search_name=largest.search_name,
                row_count=new_rows,
                estimated_tokens=ResultSerializer.estimate_tokens(new_content),
                format=largest.format,
                content=new_content,
            )
        # L-AN-16 (2026-04-22): loop-iteration cap reached without
        # converging under budget. Emit a visible warning so the operator
        # sees the overshoot in docker logs rather than silently shipping
        # an over-budget prompt.
        final_total = _total(trimmed)
        if final_total > budget:
            logger.warning(
                "[!] Budget trim: exhausted %d iterations without "
                "reaching the cap; final=%d tokens, budget=%d tokens. "
                "The prompt will be sent over budget - consider raising "
                "the per-AG max_tokens or shrinking the feeder set.",
                10, final_total, budget,
            )
        return trimmed

    # ------------------------------------------------------------------
    # On-demand feeder execution (manual-run fix, 2026-04-20)
    # ------------------------------------------------------------------

    # Class-level shared SavedSearchStore - reused across all feeders of a
    # single dispatch AND across multiple dispatches. Previously the
    # dispatcher re-instantiated + re-initialised the store for every
    # feeder (10× disk YAML reads + 10× log lines per AG run, per the
    # 2026-04-21 audit). The store is thread-safe (internal RLock), so a
    # class-level singleton is safe even with concurrent AG runs.
    _ss_store_shared = None

    @classmethod
    def _get_ss_store(cls):
        if cls._ss_store_shared is None:
            from saved_search_store import SavedSearchStore
            store = SavedSearchStore()
            store.initialize()
            cls._ss_store_shared = store
        return cls._ss_store_shared

    @classmethod
    def _reset_ss_store_cache(cls):
        """Drop the shared SavedSearchStore singleton.

        Exposed for tests that patch ``saved_search_store.SavedSearchStore``
        - without this, the class-level cache holds a reference to the
        unpatched store created by a prior test and ignores the patch.
        Production code should never need to call this.
        """
        cls._ss_store_shared = None

    @classmethod
    def _execute_feeder_query_now(
        cls, search_name: str, *, group_name: str = "",
    ) -> Any:
        """Run the saved search's query NOW and return the DataFrame.

        Bypasses ``saved_search_history.db`` entirely - the dispatcher no
        longer depends on the saved-search cron having fired recently. As
        long as the underlying ingestion has landed data under
        ``indexes/<subdir>/``, this call produces a fresh result set. Used
        for both manual Run clicks and scheduled AG cron fires, so the two
        behave identically.

        Returns ``None`` when the saved search does not exist, has an
        empty query, or ``process_query()`` raised - the caller then falls
        back to the history-DB cache. Emits a ``search_runs`` log row so
        the user can SPQL-see every feeder execution, whether it came
        from a saved-search cron or an AG dispatch.
        """
        import time as _time

        start = _time.monotonic()
        try:
            ss_store = cls._get_ss_store()
            search = ss_store.get_search(search_name)
        except FileNotFoundError:
            logger.warning(
                "[!] AG '%s': feeder '%s' not found in SavedSearchStore",
                group_name, search_name,
            )
            return None
        except Exception as exc:
            logger.warning(
                "[!] AG '%s': could not load feeder '%s': %s",
                group_name, search_name, exc,
            )
            return None

        query = (search.get("query") or "").strip()
        if not query:
            logger.warning(
                "[!] AG '%s': feeder '%s' has empty query - skipping",
                group_name, search_name,
            )
            return None

        try:
            from query_engine.CmdExecutionBackend import process_query_with_diagnostics
        except Exception as exc:
            logger.warning(
                "[!] AG '%s': cannot import process_query_with_diagnostics: %s",
                group_name, exc,
            )
            return None

        try:
            df, _job_id, diagnostic = process_query_with_diagnostics(query)
        except Exception as exc:
            # process_query_with_diagnostics is designed not to raise, but
            # defensively handle any leak so we still emit a search_runs
            # log row and log a clearly-labelled feeder failure.
            duration_ms = int((_time.monotonic() - start) * 1000)
            logger.warning(
                "[!] AG '%s': feeder '%s' query raised unexpectedly after "
                "%dms: %s", group_name, search_name, duration_ms, exc,
            )
            try:
                from functionality.log_writer import log_search_run
                log_search_run(
                    search_name=search_name, status="error",
                    duration_ms=duration_ms,
                    error_message=str(exc)[:500],
                    triggered_by=f"alert_group:{group_name}",
                )
            except Exception:
                pass
            return None

        duration_ms = int((_time.monotonic() - start) * 1000)
        rows = len(df) if df is not None else 0

        # Distinguish three outcomes for the operator: success (rows > 0),
        # empty-legitimate (ingestion ran, zero matches today), and errored
        # (query-side failure hidden by process_query's catch-all). When
        # ``diagnostic`` is non-None the query did NOT run cleanly - log
        # it with the feeder name so the dispatcher's own logs are
        # self-sufficient. Previously this error was swallowed and the
        # operator only saw a misleading "No cached result" from the
        # cache-fallback path.
        if diagnostic:
            # Fold "empty" diagnostics down to info; only real errors
            # (UndefinedVariableError, KeyError etc.) warrant the [!]
            # warning prefix and the search_runs 'error' status.
            status_word = "empty" if diagnostic.startswith("empty:") else "error"
            log_level = logger.info if status_word == "empty" else logger.warning
            prefix = "[i]" if status_word == "empty" else "[!]"
            log_level(
                "%s AG '%s': feeder '%s' %s after %dms - %s",
                prefix, group_name, search_name, status_word, duration_ms,
                diagnostic,
            )
            try:
                from functionality.log_writer import log_search_run
                log_search_run(
                    search_name=search_name, status=status_word,
                    row_count=0, duration_ms=duration_ms,
                    error_message=(
                        diagnostic[:500] if status_word == "error" else None
                    ),
                    triggered_by=f"alert_group:{group_name}",
                )
            except Exception:
                pass
            return None

        try:
            from functionality.log_writer import log_search_run
            log_search_run(
                search_name=search_name,
                status="success" if rows > 0 else "empty",
                row_count=rows,
                duration_ms=duration_ms,
                triggered_by=f"alert_group:{group_name}",
            )
        except Exception:
            pass
        logger.info(
            "[i] AG '%s': feeder '%s' executed on-demand (%d rows, %dms)",
            group_name, search_name, rows, duration_ms,
        )
        return df

    # ------------------------------------------------------------------
    # Production-hardening helpers (freshness, per-AG budget, breaker)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_setting(key: str, default):
        try:
            from global_settings import get_settings
            value = get_settings().get(key)
            return value if value is not None else default
        except Exception:
            return default

    @classmethod
    def _check_rate_limit(cls, group: dict, group_name: str) -> Optional[str]:
        """Return an error message when this dispatch would violate the
        per-AG rate limit, else None.

        Two optional per-AG YAML fields compose the limit:
          * ``max_dispatches_per_day`` - absolute cap on ``success`` runs
            within the rolling 24h window. Prevents accidental twice-a-day
            cron schedules from producing two analyst briefs when the user
            wanted one.
          * ``min_interval_between_runs_hours`` - minimum wall-clock spacing
            between successful dispatches. Use 12 to mean "at most every
            12h", 24 for "once a day", 168 for "weekly".

        Reads ``alert_group_runs.sqlite``. Failed runs do NOT count against
        the limit (so a failure + retry doesn't burn the daily quota).
        Dry-runs don't count either.
        """
        max_per_day = group.get("max_dispatches_per_day")
        min_interval = group.get("min_interval_between_runs_hours")
        if max_per_day is None and min_interval is None:
            return None

        try:
            from alert_group_store import AlertGroupStore
            store = AlertGroupStore()
            store.initialize()
            # Pull enough history to cover the rate-limit window plus
            # headroom. `limit=200` was too narrow for AGs with
            # high-frequency failed attempts (each failure counts as a
            # row even though it doesn't count as a success). 2000 rows
            # covers a year of daily dispatches comfortably.
            runs = store.list_runs(group_name=group_name, limit=2000)
        except Exception as exc:
            # Fail OPEN so rate-limit infra failure doesn't block a
            # legitimate dispatch, but WARN loudly - silent fail-open
            # on DB errors can leak into runaway dispatching. Caught
            # 2026-04-21 audit.
            logger.warning(
                "[!] AG '%s' rate-limit check failed; proceeding "
                "without rate limiting (fail-open). Investigate: %s",
                group_name, exc,
            )
            return None

        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)

        def _parse(ts: str) -> _dt.datetime | None:
            if not ts:
                return None
            try:
                return _dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=_dt.timezone.utc,
                )
            except (TypeError, ValueError):
                return None

        successful_runs = [
            r for r in runs if r.get("status") == "success"
        ]

        if max_per_day is not None:
            try:
                cap = int(max_per_day)
            except (TypeError, ValueError):
                cap = 0
            if cap > 0:
                # Calendar-day semantics (2026-08-04): the cap counts
                # successes since MIDNIGHT in the AG's timezone, not a
                # rolling 24h window. Run rows are stamped at COMPLETION,
                # so a rolling window rejected the next day's cron
                # whenever yesterday's run took minutes (three daily AGs
                # alternated success/rate_limited every other day for
                # weeks), and a late manual recovery run slid the clock
                # so the next scheduled day skipped too. "Per day" now
                # means per day; use min_interval_between_runs_hours for
                # wall-clock spacing (e.g. to prevent a 23:50 + 00:10
                # double-send across midnight).
                tz_name = (group.get("timezone") or "UTC").strip() or "UTC"
                try:
                    from zoneinfo import ZoneInfo
                    tz = ZoneInfo(tz_name)
                except Exception:
                    tz = _dt.timezone.utc
                day_start_local = now.astimezone(tz).replace(
                    hour=0, minute=0, second=0, microsecond=0,
                )
                window_start = day_start_local.astimezone(_dt.timezone.utc)
                count_today = sum(
                    1 for r in successful_runs
                    if (t := _parse(r.get("triggered_at") or ""))
                    and t >= window_start
                )
                if count_today >= cap:
                    return (
                        f"already dispatched {count_today} time(s) today "
                        f"({tz_name} calendar day) "
                        f"(max_dispatches_per_day={cap})"
                    )

        if min_interval is not None:
            try:
                hrs = float(min_interval)
            except (TypeError, ValueError):
                hrs = 0
            if hrs > 0 and successful_runs:
                last_ts = _parse(successful_runs[0].get("triggered_at") or "")
                if last_ts is not None:
                    delta_h = (now - last_ts).total_seconds() / 3600.0
                    if delta_h < hrs:
                        return (
                            f"last success was {delta_h:.1f}h ago "
                            f"(min_interval_between_runs_hours={hrs})"
                        )

        return None

    @classmethod
    def _check_feeder_freshness(
        cls, group: dict, search_names: list,
    ) -> list[tuple[str, float]]:
        """Return ``[(name, age_hours), ...]`` for any feeder whose most
        recent cached result is older than the staleness threshold.

        Threshold resolution: per-AG ``max_feeder_staleness_hours`` wins,
        otherwise the global ``alert_group_max_feeder_staleness_hours``
        (default 48h). A feeder we can't locate (e.g. deleted parquet) is
        reported as infinitely stale so the user is told.
        """
        import sqlite3
        from pathlib import Path
        import time as _time

        threshold_hours = float(
            group.get("max_feeder_staleness_hours")
            or cls._get_setting("alert_group_max_feeder_staleness_hours", 48)
        )
        if threshold_hours <= 0:
            return []

        from alert_groups.serializer import HISTORY_DB
        if not HISTORY_DB.exists():
            return [(name, float("inf")) for name in search_names or []]

        stale: list[tuple[str, float]] = []
        now = _time.time()
        try:
            with sqlite3.connect(str(HISTORY_DB)) as conn:
                for name in search_names or []:
                    row = conn.execute(
                        "SELECT saved_search_path FROM execution_history "
                        "WHERE query_name = ? "
                        "ORDER BY execution_start_time DESC LIMIT 1",
                        (name,),
                    ).fetchone()
                    if row is None:
                        stale.append((name, float("inf")))
                        continue
                    path = Path(row[0])
                    if not path.exists():
                        stale.append((name, float("inf")))
                        continue
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        stale.append((name, float("inf")))
                        continue
                    age_hr = (now - mtime) / 3600.0
                    if age_hr > threshold_hours:
                        stale.append((name, age_hr))
        except sqlite3.OperationalError as exc:
            logger.warning("[!] Freshness check DB error: %s", exc)
        return stale

    @classmethod
    def _check_per_ag_budget(
        cls, group: dict, group_name: str, estimated_tokens: int,
    ) -> Optional[str]:
        """Return an error message if the pre-flight cost estimate would
        exceed this AG's configured budget (per-run or per-day), else None.

        ``max_cost_usd_per_run`` caps a single dispatch.
        ``max_cost_usd_per_day`` caps the sum over the last 24 hours as
        recorded in ``claude_api_history.sqlite`` for this group_name.

        Token→cost conversion uses the current analyzer primary model's
        rate - conservative, since a mixed-tool call may cost less.
        """
        # Slice A (2026-06-23): a registry model_id routes this AG through
        # the provider-agnostic LLM router to a $0-cost local model (e.g.
        # llamacpp-qwen35-122b-a10b on the LAN). There's no per-token cost
        # to gate against, so the dollar budget is moot - returning None
        # keeps a free local AG from being blocked by a Claude-priced
        # estimate it will never incur.
        if (group.get("model_id") or "").strip():
            return None
        model = cls._model_choice(group)
        try:
            from analyzers.claude_client import _pricing_for
            in_pm, out_pm = _pricing_for(model)
        except Exception:
            in_pm, out_pm = 3.0, 15.0
        # Rough estimate: assume 20% output tokens vs input
        est_input = estimated_tokens
        est_output = max(int(estimated_tokens * 0.2),
                         int(cls._max_tokens(group) * 0.5))
        est_cost = round(
            est_input / 1_000_000 * in_pm + est_output / 1_000_000 * out_pm,
            6,
        )

        per_run_cap = group.get("max_cost_usd_per_run")
        if per_run_cap is not None:
            try:
                cap = float(per_run_cap)
                if cap > 0 and est_cost > cap:
                    return (
                        f"estimated ${est_cost:.4f} exceeds per-run cap "
                        f"${cap:.4f}"
                    )
            except (TypeError, ValueError):
                logger.warning(
                    "[!] Ignoring invalid max_cost_usd_per_run=%r for '%s'",
                    per_run_cap, group_name,
                )

        per_day_cap = group.get("max_cost_usd_per_day")
        if per_day_cap is not None:
            try:
                cap = float(per_day_cap)
            except (TypeError, ValueError):
                cap = 0
            if cap > 0:
                import time as _time
                from analyzers.claude_history_store import ClaudeHistoryStore
                try:
                    stats = ClaudeHistoryStore.get_instance().stats(
                        since_epoch=int(_time.time()) - 86400,
                        group_name=group_name,
                    )
                    spent_today = float(stats.get("cost_usd") or 0)
                except Exception:
                    spent_today = 0.0
                if spent_today + est_cost > cap:
                    return (
                        f"24h spend ${spent_today:.4f} + estimated "
                        f"${est_cost:.4f} exceeds per-day cap ${cap:.4f}"
                    )
        return None

    @classmethod
    def _consecutive_error_count(cls, group_name: str) -> int:
        """Count the latest consecutive error runs for a group.

        Reads ``alert_group_runs.sqlite`` newest-first; stops at the first
        non-error status. Used by the circuit breaker.
        """
        try:
            from alert_group_store import AlertGroupStore
            store = AlertGroupStore()
            store.initialize()
            runs = store.list_runs(group_name=group_name, limit=50)
        except Exception:
            return 0
        streak = 0
        for r in runs:
            if r.get("status") == "error":
                streak += 1
            else:
                break
        return streak

    @classmethod
    def _maybe_trip_circuit_breaker(cls, group_name: str) -> None:
        """If consecutive errors hit the threshold, mark the AG tripped.

        Tripped AGs refuse to dispatch (see the breaker check at the top
        of ``_run_inner``) until reset via the reset endpoint. Gated by
        ``alert_group_circuit_breaker_auto_disable`` (default True).
        """
        if not cls._get_setting("alert_group_circuit_breaker_auto_disable", True):
            return
        threshold = int(cls._get_setting(
            "alert_group_circuit_breaker_consecutive_failures", 5,
        ))
        streak = cls._consecutive_error_count(group_name)
        # The run that just failed is about to be logged (caller does it
        # right after this method); include it by using streak + 1.
        if streak + 1 < threshold:
            return
        try:
            import datetime as _dt
            from alert_group_store import AlertGroupStore
            store = AlertGroupStore()
            store.initialize()
            store.update_group(group_name, {
                "circuit_breaker_tripped": True,
                "circuit_breaker_tripped_at": _dt.datetime.now(
                    _dt.timezone.utc,
                ).isoformat(),
            })
            logger.error(
                "[x] Alert group '%s' circuit breaker TRIPPED after %d "
                "consecutive failures.", group_name, streak + 1,
            )
            try:
                from functionality.log_writer import log_system_event
                log_system_event(
                    component="alert_groups",
                    event="circuit_breaker_tripped",
                    level="error",
                    message=(
                        f"{group_name} tripped after {streak + 1} consecutive "
                        f"failures (threshold={threshold})"
                    ),
                )
            except Exception:
                pass
        except Exception as exc:
            logger.warning(
                "[!] Could not trip circuit breaker for '%s': %s",
                group_name, exc,
            )

    @classmethod
    def _reset_consecutive_failure_count(cls, group_name: str) -> None:
        """Close the circuit breaker after a healthy run.

        The consecutive-error counter itself resets naturally (success
        runs land in the audit DB, so the streak reads 0 on next query),
        but since 2026-08-04 a successful half-open probe must ALSO clear
        ``circuit_breaker_tripped`` so the AG returns to normal service
        without a manual reset.
        """
        try:
            from alert_group_store import AlertGroupStore
            store = AlertGroupStore()
            store.initialize()
            g = store.get_group(group_name)
            if g.get("circuit_breaker_tripped"):
                store.update_group(group_name, {
                    "circuit_breaker_tripped": False,
                    "circuit_breaker_tripped_at": "",
                })
                logger.info(
                    "[i] Alert group '%s': circuit breaker CLOSED after "
                    "successful run.", group_name,
                )
        except FileNotFoundError:
            # Group dict was passed directly (tests / ad-hoc dispatch)
            # without a backing YAML - nothing to clear.
            return
        except Exception as exc:
            logger.warning(
                "[!] Alert group '%s': could not close circuit breaker "
                "after success: %s", group_name, exc,
            )

    @classmethod
    def _circuit_breaker_probe_state(cls, group: dict) -> tuple[bool, str]:
        """Return ``(probe_ok, wait_message)`` for a tripped breaker.

        ``probe_ok=True`` means the cooldown has elapsed (or the trip
        predates the ``circuit_breaker_tripped_at`` field) and the caller
        should attempt a half-open probe dispatch. ``probe_ok=False``
        means the breaker is still cooling down; ``wait_message``
        explains when the next automatic probe will happen.
        """
        import datetime as _dt
        cooldown_h = float(cls._get_setting(
            "alert_group_circuit_breaker_cooldown_hours", 20,
        ))
        raw = (group.get("circuit_breaker_tripped_at") or "").strip()
        if not raw:
            # Legacy trip (field predates 2026-08-04) - probe immediately
            # so long-stuck AGs self-heal on their next scheduled fire.
            return True, ""
        try:
            tripped_at = _dt.datetime.fromisoformat(raw)
            if tripped_at.tzinfo is None:
                tripped_at = tripped_at.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            return True, ""
        elapsed_h = (
            _dt.datetime.now(_dt.timezone.utc) - tripped_at
        ).total_seconds() / 3600.0
        if elapsed_h >= cooldown_h:
            return True, ""
        remaining_h = cooldown_h - elapsed_h
        return False, (
            f"Circuit breaker cooling down - tripped {elapsed_h:.1f}h ago; "
            f"next automatic half-open probe in {remaining_h:.1f}h "
            f"(alert_group_circuit_breaker_cooldown_hours={cooldown_h:g}). "
            f"Reset now via POST /api/alert-groups/<name>/"
            f"reset-circuit-breaker or run with force=true."
        )

    @classmethod
    def _touch_circuit_breaker_timestamp(cls, group_name: str) -> None:
        """Refresh ``circuit_breaker_tripped_at`` to now (probe start).

        Called when a half-open probe begins so a failing probe waits a
        full cooldown before the next attempt instead of probing on
        every scheduled fire.
        """
        try:
            import datetime as _dt
            from alert_group_store import AlertGroupStore
            store = AlertGroupStore()
            store.initialize()
            store.update_group(group_name, {
                "circuit_breaker_tripped": True,
                "circuit_breaker_tripped_at": _dt.datetime.now(
                    _dt.timezone.utc,
                ).isoformat(),
            })
        except Exception as exc:
            logger.warning(
                "[!] Alert group '%s': could not refresh circuit breaker "
                "timestamp: %s", group_name, exc,
            )

    def _call_router_llm(
        self, *, group_name, model_id, user_content, max_tokens,
    ):
        """Dispatch an AG analysis call through the provider-agnostic LLM
        router (Slice A, 2026-06-23) and normalise the result into the same
        ``(call, response_text, response_meta)`` shape the Claude path
        produces - so the downstream pick/email/logging code stays
        branch-agnostic.

        ``call`` is a lightweight stand-in exposing exactly the attributes
        the converge + email blocks read (``response`` carrying
        ``stop_reason``, ``input_tokens``, ``output_tokens``, ``cost_usd``,
        ``request_id``, ``attempts``) - NOT a ``ClaudeCallResult``; this
        path sets ``response_text`` directly rather than parsing Anthropic
        content blocks.

        Routes through the registry: a local model (provider ``lmstudio`` /
        ``ollama``) costs $0 and uses its per-record timeout (e.g. 600s for
        the 122B). NO web_search tool - a single-shot completion can't use
        Anthropic's server-side tool. Cache is OFF: a scheduled brief must
        reflect today's feeders, never a prior run's cached text.

        Raises whatever ``call_llm`` raises (``LLMRouterError``) on failure;
        the caller's ``except`` turns it into a failure-email + circuit
        breaker tick, mirroring the ClaudeCallError path. The 122B's
        thinking-loop "empty content" failure mode is NOT raised here - it
        returns empty text and is caught by the shared empty-text guard.
        """
        from types import SimpleNamespace
        from analyzers.llm_router import call_llm

        resp = call_llm(
            model_id,
            prompt=(user_content or ""),
            system=None,
            max_tokens=max_tokens,
            source="alert_group",
            use_cache=False,
        )
        response_text = resp.text or ""
        # Pull the Chat Completions finish_reason when present so the meta
        # mirrors _extract_response_meta's shape; default "stop".
        finish_reason = "stop"
        try:
            raw = resp.raw_response or {}
            choices = raw.get("choices") or []
            if choices and isinstance(choices[0], dict):
                finish_reason = choices[0].get("finish_reason") or "stop"
        except Exception:
            finish_reason = "stop"
        response_meta = {
            "stop_reason": finish_reason,
            "block_types": ["text"] if response_text else [],
            "block_count": 1 if response_text else 0,
            "text_block_count": 1 if response_text else 0,
        }
        call = SimpleNamespace(
            response=SimpleNamespace(stop_reason=finish_reason),
            input_tokens=int(getattr(resp, "input_tokens", 0) or 0),
            output_tokens=int(getattr(resp, "output_tokens", 0) or 0),
            cost_usd=float(getattr(resp, "cost_usd", 0.0) or 0.0),
            request_id=getattr(resp, "request_id", "") or "",
            attempts=1,
        )
        logger.info(
            "[i] AG '%s': local model returned (model_id=%s, in=%d, out=%d, "
            "finish=%s, cost=$%.4f)",
            group_name, model_id, call.input_tokens, call.output_tokens,
            finish_reason, call.cost_usd,
        )
        return call, response_text, response_meta

    @staticmethod
    def _is_transient_llm_error(exc: Exception) -> bool:
        """Classify a router/transport failure as transient (retryable).

        Transient: connection-level failures, timeouts, HTTP 429, and
        HTTP 5xx (including a gateway's 502/503/504). NOT transient:
        other HTTP 4xx (401 bad token, 400 bad request, 404) - retrying
        a config error just delays the failure email.
        """
        error_class = getattr(exc, "error_class", "") or type(exc).__name__
        if error_class.startswith("HTTP"):
            try:
                code = int(error_class[4:])
            except ValueError:
                return False
            return code == 429 or code >= 500
        transient_markers = ("Timeout", "Connection", "ChunkedEncoding",
                             "Protocol")
        return any(m in error_class for m in transient_markers)

    def _call_router_llm_with_retry(
        self, *, group_name, model_id, user_content, max_tokens,
    ):
        """Graduated-retry wrapper around :meth:`_call_router_llm`.

        Added 2026-08-04: an AG dispatch aggregates the work of many
        feeders; losing the whole run to one transient LLM hiccup throws
        that data away. Local calls cost $0, so retrying is cheap.

        Behaviour:
          * up to ``local_llm_retry_attempts`` (default 3) total attempts
          * graduated backoff: base delay (default 30s) tripling each
            retry, capped at 10 minutes (30s, 90s, 270s, ...)
          * only TRANSIENT failures retry (see
            :meth:`_is_transient_llm_error`); a 401/400 config error
            raises immediately
          * an empty-text response (the reasoning-trace-starvation
            failure mode) also retries - it is transient in practice and
            each retry is free on a local model

        Note this deliberately diverges from the Claude-path "don't
        retry timeouts" rule: that rule exists because a cloud retry
        burns real dollars against the same timeout ceiling. A LAN model
        retry costs nothing but wall-clock, and the observed gateway
        timeouts (504 at a proxy's ceiling) often clear on a quieter
        second attempt.
        """
        attempts = max(1, int(self._get_setting("local_llm_retry_attempts", 3)))
        base_delay = max(1, int(self._get_setting(
            "local_llm_retry_base_delay_seconds", 30,
        )))
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                call, response_text, response_meta = self._call_router_llm(
                    group_name=group_name,
                    model_id=model_id,
                    user_content=user_content,
                    max_tokens=max_tokens,
                )
                if response_text.strip():
                    call.attempts = attempt
                    return call, response_text, response_meta
                # Empty text: retryable on a local model. Keep the last
                # normalized result so the final failure surfaces through
                # the shared empty-text guard with full diagnostics.
                last_exc = None
                last_empty = (call, response_text, response_meta)
                if attempt == attempts:
                    call.attempts = attempt
                    return last_empty
                logger.warning(
                    "[!] AG '%s': local LLM attempt %d/%d returned EMPTY "
                    "text (finish=%s). Retrying in %ds...",
                    group_name, attempt, attempts,
                    response_meta.get("stop_reason"),
                    min(base_delay * (3 ** (attempt - 1)), 600),
                )
            except Exception as exc:
                if not self._is_transient_llm_error(exc) or attempt == attempts:
                    raise
                last_exc = exc
                logger.warning(
                    "[!] AG '%s': local LLM attempt %d/%d failed "
                    "(transient: %s). Retrying in %ds...",
                    group_name, attempt, attempts, exc,
                    min(base_delay * (3 ** (attempt - 1)), 600),
                )
            delay = min(base_delay * (3 ** (attempt - 1)), 600)
            _dispatch_progress_set(
                group_name,
                phase="retrying_local_llm",
                phase_label=(
                    f"Local LLM attempt {attempt}/{attempts} failed - "
                    f"retrying in {delay}s (graduated backoff)."
                ),
            )
            time.sleep(delay)
        # Unreachable: the loop always returns or raises on the final
        # attempt. Defensive re-raise keeps static analysis honest.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("local LLM retry loop exited without a result")

    # ------------------------------------------------------------------
    # Email digest (2026-08-04): distill the raw analyst output into a
    # BLUF-first readable report via a local $0 model before emailing.
    # ------------------------------------------------------------------

    _DIGEST_PROMPT_TEMPLATE = (
        "You are the email editor for the \"{group_name}\" report. Below "
        "is today's RAW analyst output. Rewrite it as a clean, skimmable "
        "email report with EXACTLY this structure (markdown):\n"
        "\n"
        "# {group_name} - Daily Report\n"
        "\n"
        "## BLUF\n"
        "2-4 sentences maximum, bottom line up front: what action (if "
        "any) the reader should take today, how many picks/items there "
        "are, the overall stance, and the single most important fact. If "
        "there are zero picks, the FIRST sentence says so plainly and "
        "the second gives the one-clause reason.\n"
        "\n"
        "## Today's Picks\n"
        "(Omit this section entirely when there are zero picks.) One "
        "compact block per pick: **instrument and direction** on the "
        "first line, then short bullets for structure/entry, max "
        "loss/max profit, conviction, and a one-sentence thesis.\n"
        "\n"
        "## Key Context\n"
        "3-6 short bullets: trailing performance, market regime, and the "
        "most notable candidates that were REJECTED and why (one line "
        "each).\n"
        "\n"
        "## Watch Next\n"
        "1-3 bullets: the conditions that would change the call or "
        "produce picks on a future day.\n"
        "\n"
        "HARD RULES\n"
        "- Preserve every number EXACTLY as written in the source. Never "
        "invent, recompute, or extrapolate a figure.\n"
        "- Drop all internal analysis notes, step-by-step reasoning, "
        "tool-call narration, and any fenced JSON block completely.\n"
        "- Plain markdown only: headers, bold, bullets. No tables. No "
        "JSON. No preamble before the # header and nothing after the "
        "final bullet.\n"
        "- Keep the whole report under 500 words. The complete raw "
        "analyst output is attached to the email separately - you are "
        "writing the readable summary, not the archive.\n"
        "\n"
        "RAW ANALYST OUTPUT\n"
        "------------------\n"
        "{raw}\n"
    )

    def _build_digest_email_body(
        self, *, group: dict, group_name: str, response_text: str,
    ) -> str | None:
        """Distill ``response_text`` via the AG's ``email_digest_model_id``.

        Returns the digest markdown, or None when the field is unset or
        the digest attempt failed (caller falls back to the raw text -
        an ugly brief always beats a lost one). The digest model is
        expected to be a $0 local registry model (e.g. the 122B qwen);
        the raw response is preserved verbatim in the email's .md
        attachment and in the pick journal regardless.
        """
        digest_model = (group.get("email_digest_model_id") or "").strip()
        if not digest_model or not response_text.strip():
            return None
        # Default 8192, not less: a thinking model's reasoning trace
        # counts against max_tokens (the 122B's trace alone can run ~6k
        # tokens), and a starved trace returns EMPTY content.
        max_tokens = int(self._get_setting(
            "alert_group_digest_max_tokens", 8192,
        ))
        logger.info(
            "[i] AG '%s': building email digest via %s (raw %d chars).",
            group_name, digest_model, len(response_text),
        )
        _dispatch_progress_set(
            group_name,
            phase="building_digest",
            phase_label=(
                f"Distilling brief into a BLUF-first email via "
                f"{digest_model} (local, $0)..."
            ),
        )
        try:
            _call, text, _meta = self._call_router_llm_with_retry(
                group_name=group_name,
                model_id=digest_model,
                user_content=self._DIGEST_PROMPT_TEMPLATE.format(
                    group_name=group_name, raw=response_text,
                ),
                max_tokens=max_tokens,
            )
        except Exception as exc:
            logger.warning(
                "[!] AG '%s': email digest via %s failed (%s) - falling "
                "back to the raw brief.", group_name, digest_model, exc,
            )
            return None
        text = (text or "").strip()
        if not text:
            logger.warning(
                "[!] AG '%s': email digest returned empty text - falling "
                "back to the raw brief.", group_name,
            )
            return None
        return text

    @staticmethod
    def _strip_json_tail(text: str) -> str:
        """Remove a trailing fenced ```json block from an email body.

        The machine-readable tail exists for the pick journaler, not the
        human reader; it survives untouched in the .md attachment and in
        the pick extraction (which runs on the raw response). Only a
        TRAILING fence is stripped - a JSON example mid-brief is left
        alone.
        """
        stripped = (text or "").rstrip()
        fence_start = stripped.rfind("```json")
        if fence_start == -1:
            return text
        tail = stripped[fence_start:]
        # Only treat it as the machine tail when the block closes at the
        # very end of the text (allowing trailing whitespace).
        if not tail.rstrip().endswith("```"):
            return text
        return stripped[:fence_start].rstrip()

    @staticmethod
    def _web_search_tool_for(model: str) -> dict:
        """Return the newest web_search server-tool variant *model* supports.

        The ``web_search_20260209`` variant (dynamic filtering - results
        are code-filtered server-side before hitting the context window)
        requires Opus 4.6+/Sonnet 4.6+/Opus 5/Sonnet 5/Fable 5; older
        models keep the basic ``web_search_20250305`` variant. Added
        2026-08-04 with the options_edge_brief Opus 5 upgrade.
        """
        modern_prefixes = (
            "claude-opus-5", "claude-sonnet-5", "claude-fable-5",
            "claude-mythos", "claude-opus-4-6", "claude-opus-4-7",
            "claude-opus-4-8", "claude-sonnet-4-6",
        )
        # Cap the server-side search loop. The first uncapped Opus 5 run
        # (2026-08-04) issued enough searches to balloon input to 716k
        # tokens ($4.02/run, 6.5x the Sonnet baseline) - each server-side
        # iteration re-bills the growing context. 8 matches the observed
        # search count of healthy Sonnet runs with room to spare.
        max_uses = int(AlertGroupDispatcher._get_setting(
            "alert_group_web_search_max_uses", 8,
        ))
        if any(model.startswith(p) for p in modern_prefixes):
            return {"type": "web_search_20260209", "name": "web_search",
                    "max_uses": max_uses}
        return {"type": "web_search_20250305", "name": "web_search",
                "max_uses": max_uses}

    @staticmethod
    def _model_choice(group: dict | None = None) -> str:
        """Resolve the Claude model for a dispatch.

        Priority:
          1. Per-AG override on the YAML (``claude_analyzer_model_primary``
             key on the alert-group dict). Lets one AG run on Sonnet while
             another runs on Haiku for cost reasons.
          2. Global setting ``claude_analyzer_model_primary``.
          3. Hard fallback ``claude-sonnet-4-6``.

        Wave-2 of OEB exposed the bug that the per-AG override was being
        ignored - the YAML field was effectively dead because this method
        only checked the global. Fixed 2026-04-27 to check the group
        first when one is supplied. ``group=None`` callers still get the
        global behaviour (used by the cost-cap estimator before the AG
        dict is loaded).
        """
        if group is not None:
            override = (group.get("claude_analyzer_model_primary") or "").strip()
            if override:
                return override
        try:
            from global_settings import get_settings
            return (
                get_settings().get("claude_analyzer_model_primary")
                or "claude-sonnet-4-6"
            )
        except Exception:
            return "claude-sonnet-4-6"

    @staticmethod
    def _max_tokens(group: dict | None = None) -> int:
        """Return max_output_tokens for a dispatch.

        Per-AG YAML override via ``max_output_tokens`` wins over the
        global ``claude_analyzer_max_output_tokens`` setting. A long
        multi-opportunity analyst brief typically needs 8192+ to avoid
        ``stop_reason=max_tokens`` truncation - the default (8192) is
        sized for that; per-search analyzer calls can override lower.
        """
        if group is not None:
            override = group.get("max_output_tokens")
            if override is not None:
                try:
                    v = int(override)
                    if v > 0:
                        return v
                except (TypeError, ValueError):
                    pass
        try:
            from global_settings import get_settings
            return int(
                get_settings().get("claude_analyzer_max_output_tokens") or 8192
            )
        except Exception:
            return 8192

    @staticmethod
    def _extract_response_text(response) -> str:
        """Extract text content from a Claude API response.

        H-AN-2 (2026-04-21): emit a ``[!]`` warning when the response
        carries no text blocks - e.g. a tool-only turn (``tool_use`` blocks
        with no accompanying ``text``). Previously this returned ``""``
        silently and the dispatcher happily emailed an empty brief with
        status='success'. The warning gives operators a visible signal
        paired with stop_reason + block-type list so the root cause is
        obvious in ``docker logs``.

        M-AN-12 (2026-04-22): callers that need structured metadata (stop
        reason, block type list, block count) should call
        :meth:`_extract_response_meta` instead. This method stays single-
        valued for back-compat with the 6+ existing call sites.
        """
        text, _meta = AlertGroupDispatcher._extract_response_meta(response)
        return text

    @staticmethod
    def _extract_response_meta(response):
        """Return ``(text, meta)`` - text plus a diagnostic dict.

        ``meta`` shape::

            {
                "stop_reason": str | None,
                "block_types": list[str],    # class name of each content block
                "block_count": int,
                "text_block_count": int,     # blocks that contributed text
            }

        M-AN-12 (2026-04-22): when the dispatcher fails fast on an empty
        response, having ``stop_reason`` + ``block_types`` in the
        structured error_message makes docker-logs triage concrete instead
        of "look up the request_id". Previously only a log line carried
        the context; now the audit row does too.
        """
        meta = {
            "stop_reason": getattr(response, "stop_reason", None),
            "block_types": [],
            "block_count": 0,
            "text_block_count": 0,
        }
        if not response or not response.content:
            logger.warning(
                "[!] Claude response is empty (response=%r, content=%r); "
                "downstream dispatch will fail fast with an error status.",
                bool(response),
                getattr(response, "content", None),
            )
            return "", meta

        meta["block_types"] = [type(b).__name__ for b in response.content]
        meta["block_count"] = len(response.content)
        parts = []
        for block in response.content:
            if hasattr(block, "text") and getattr(block, "text", None):
                parts.append(block.text)
        meta["text_block_count"] = len(parts)
        if not parts:
            logger.warning(
                "[!] Claude response contained no text blocks "
                "(stop_reason=%s, content_types=%s). Dispatcher will "
                "treat this as a failure - the brief cannot be emailed.",
                meta["stop_reason"], meta["block_types"],
            )
        return "\n".join(parts), meta

    # ------------------------------------------------------------------
    # Pick capture (2026-04-21)
    # ------------------------------------------------------------------

    # Regex for a trailing fenced ```json``` block. Claude sometimes uses
    # ```json ... ``` or just ``` ... ``` with JSON content. We try both.
    # DOTALL so the content can span newlines; we anchor to the LAST block
    # via greedy + negative-lookahead to avoid picking up in-prose examples.
    _PICK_BLOCK_RE = __import__("re").compile(
        r"```(?:json)?\s*(\[[\s\S]*?\])\s*```(?!.*```)",
        flags=__import__("re").DOTALL,
    )

    # idea_id format check - loose but catches obvious drift (e.g. spaces,
    # missing component). Claude produces this verbatim; we lowercase it
    # on the way in as the one "verify" step on top of trust.
    _IDEA_ID_RE = __import__("re").compile(
        r"^[a-z0-9][a-z0-9_\-]*:[a-z0-9][a-z0-9_\-\.]*:[a-z0-9][a-z0-9_\-]*$",
    )

    _VALID_INSTRUMENT_TYPES = frozenset({
        "polymarket", "kalshi", "equity", "crypto", "option",
        "commodity", "forex", "etf",
    })
    _VALID_DIRECTIONS = frozenset({
        "YES", "NO", "LONG", "SHORT", "BUY", "SELL",
    })
    _VALID_POSITION_TIERS = frozenset({"SMALL", "MEDIUM", "LARGE"})

    # Required keys - every pick must carry these. Others (``take_profit_price``,
    # ``stop_loss_price``) are optional; ``None`` is allowed.
    _REQUIRED_PICK_KEYS = (
        "idea_id", "instrument_type", "instrument_id", "direction",
        "conviction_pct", "expected_return_pct", "position_size_tier",
        "entry_price", "suggested_buy_epoch", "suggested_sell_epoch",
        "exit_catalyst", "thesis",
    )

    @staticmethod
    def _json_loads_lenient(raw: str, *, max_repairs: int = 20):
        """``json.loads`` tolerating the two malformations LLMs actually
        emit in fenced blocks: a missing comma between members/elements
        and a trailing comma before a closing bracket. Caught 2026-07-10
        when the local 122B dropped one comma in the daily_opportunity_brief
        JSON tail and six well-formed picks went unjournaled.

        Strict parse runs first - well-formed input never touches the
        repair path. On failure, the decoder's reported error position
        drives a targeted one-character fix and the parse retries, up to
        ``max_repairs`` times. Position-driven repair can't corrupt
        string content the way blind regex substitution can. Anything
        the loop can't fix re-raises the ORIGINAL strict-parse error so
        caller warnings describe the real malformation.

        Returns ``(obj, repairs_applied)``. Pure - no I/O, no logging.
        """
        import json

        try:
            return json.loads(raw), 0
        except json.JSONDecodeError as exc:
            original, last = exc, exc

        repaired = raw
        for repairs in range(1, max_repairs + 1):
            if "Expecting ',' delimiter" in last.msg:
                fixed = repaired[:last.pos] + "," + repaired[last.pos:]
            elif last.msg.startswith("Illegal trailing comma"):
                # Python 3.14+ names the trailing comma directly and
                # reports ``pos`` AT the comma - drop that character.
                fixed = repaired[:last.pos] + repaired[last.pos + 1:]
            elif last.msg.startswith(
                ("Expecting value", "Expecting property name"),
            ) and repaired[:last.pos].rstrip().endswith(","):
                # Python <= 3.13 reports a trailing comma as a generic
                # expectation failure with ``pos`` AFTER the comma
                # (pointing at ``}`` / ``]``) - drop the comma.
                cut = len(repaired[:last.pos].rstrip()) - 1
                fixed = repaired[:cut] + repaired[cut + 1:]
            else:
                raise original
            repaired = fixed
            try:
                return json.loads(repaired), repairs
            except json.JSONDecodeError as exc:
                last = exc
        raise original

    @classmethod
    def _parse_picks_block(
        cls,
        *,
        response_text: str,
        group_name: str,
    ) -> list[dict]:
        """Parse the trailing fenced JSON picks block into a list of
        normalized pick dicts. Pure - no I/O, no logging side effects
        beyond warnings. Reused by both the live dispatch path and the
        Wave 3 manual-return endpoint (which also drives the preview
        pane in the UI before the operator commits).

        Returns ``[]`` when the block is missing, malformed, or every
        candidate pick fails validation. Never raises.
        """
        import json

        if not response_text:
            return []

        match = cls._PICK_BLOCK_RE.search(response_text)
        if not match:
            logger.warning(
                "[!] AG '%s': no fenced JSON picks block found in response "
                "(truncated? format drift?). Check the most recent row in "
                "claude_api_history.sqlite for the full response.",
                group_name,
            )
            return []

        raw = match.group(1)
        try:
            picks, repairs = cls._json_loads_lenient(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "[!] AG '%s': picks JSON block failed to parse (%s). "
                "Block starts with: %r",
                group_name, exc, raw[:200],
            )
            return []
        if repairs:
            logger.warning(
                "[!] AG '%s': picks JSON block was malformed but "
                "auto-repaired (%d comma fix(es) applied) - check the "
                "emitting model's JSON discipline if this recurs.",
                group_name, repairs,
            )

        if not isinstance(picks, list):
            logger.warning(
                "[!] AG '%s': picks JSON is not a list (got %s)",
                group_name, type(picks).__name__,
            )
            return []

        normalized: list[dict] = []
        for i, raw_pick in enumerate(picks, start=1):
            if not isinstance(raw_pick, dict):
                logger.warning(
                    "[!] AG '%s': pick #%d is not a dict (skipping): %r",
                    group_name, i, raw_pick,
                )
                continue
            n = cls._validate_and_normalize_pick(
                raw_pick, rank=i, group_name=group_name,
            )
            if n is None:
                continue
            normalized.append(n)
        return normalized

    @classmethod
    def _log_picks(
        cls,
        *,
        normalized_picks: list[dict],
        group_name: str,
        run_request_id: str,
        source: str = "claude",
        model_used: str = "",
    ) -> int:
        """Write each already-normalized pick to ``indexes/IMMUTABLE/ag_picks/``.
        Returns the number of rows successfully written. Per-row failures
        log a warning and skip that row - the rest still land.

        ``source`` and ``model_used`` (Wave 3, 2026-04-25) carry pick
        provenance. Defaults preserve the historical behaviour for the
        live dispatcher (Claude pipeline). The manual-return endpoint
        passes ``source="manual"`` and the operator-chosen model id.
        """
        written = 0
        for normalized in normalized_picks:
            try:
                log_ag_pick(
                    alert_group=group_name,
                    run_request_id=run_request_id,
                    rank_in_brief=normalized["rank_in_brief"],
                    pick_tier=normalized.get("pick_tier", "TOP"),
                    idea_id=normalized["idea_id"],
                    instrument_type=normalized["instrument_type"],
                    instrument_id=normalized["instrument_id"],
                    direction=normalized["direction"],
                    conviction_pct=normalized["conviction_pct"],
                    expected_return_pct=normalized["expected_return_pct"],
                    position_size_tier=normalized["position_size_tier"],
                    entry_price=normalized["entry_price"],
                    suggested_buy_epoch=normalized["suggested_buy_epoch"],
                    suggested_sell_epoch=normalized["suggested_sell_epoch"],
                    hold_hours=normalized["hold_hours"],
                    take_profit_price=normalized.get("take_profit_price"),
                    stop_loss_price=normalized.get("stop_loss_price"),
                    exit_catalyst=normalized["exit_catalyst"],
                    thesis=normalized["thesis"],
                    source_signals=normalized.get("source_signals", ""),
                    correlation_cluster=normalized.get("correlation_cluster", ""),
                    short_squeeze_risk_json=normalized.get("short_squeeze_risk_json", ""),
                    status="open",
                    source=source,
                    model_used=model_used,
                    option_structure=normalized.get("option_structure"),
                    option_legs_json=normalized.get("option_legs_json"),
                    option_max_loss_usd=normalized.get("option_max_loss_usd"),
                    option_max_profit_usd=normalized.get("option_max_profit_usd"),
                    option_net_debit_credit=normalized.get("option_net_debit_credit"),
                    option_dte_days=normalized.get("option_dte_days"),
                    option_difficulty_tier=normalized.get("option_difficulty_tier"),
                    account_size_floor_usd=normalized.get("account_size_floor_usd"),
                )
                written += 1
            except Exception as exc:
                logger.warning(
                    "[!] AG '%s': pick row write failed: %s",
                    group_name, exc,
                )
        return written

    @classmethod
    def _extract_and_log_picks(
        cls,
        *,
        response_text: str,
        group_name: str,
        run_request_id: str,
        model_used: str = "",
    ) -> int:
        """Live-dispatch entry point: parse the fenced JSON block AND
        write each pick. Source defaults to ``claude``. Returns the
        number of rows successfully written. Never raises.

        Contract with Claude (enforced by the prompt): the response ends
        with ```json [ {...}, {...}, ... ] ``` containing 1..5 picks.
        Each pick carries the keys listed in ``_REQUIRED_PICK_KEYS``
        plus optional ``take_profit_price`` / ``stop_loss_price`` /
        ``source_signals``. The Wave 3 manual-return endpoint reuses
        the ``_parse_picks_block`` and ``_log_picks`` halves separately.
        """
        normalized = cls._parse_picks_block(
            response_text=response_text, group_name=group_name,
        )
        if not normalized:
            return 0
        return cls._log_picks(
            normalized_picks=normalized,
            group_name=group_name,
            run_request_id=run_request_id,
            source="claude",
            model_used=model_used,
        )

    @classmethod
    def _extract_and_log_review_observations(
        cls,
        *,
        response_text: str,
        group_name: str,
        run_request_id: str,
    ) -> int:
        """Parse the trailing fenced JSON OBJECT (not array) emitted by
        the ``options_performance_review`` alert group and persist its
        summary + observation rows.

        Wave 2 of Options Edge Brief (2026-04-26). The review AG's
        prompt instructs Claude to emit a structured object with hit
        rates, signal-class winners/losers, a ``rule_tweak`` sub-object,
        and an ``observations`` array. This method:

          1. Locates the LAST fenced JSON block in the response (same
             regex semantics as ``_parse_picks_block`` so a brief that
             accidentally embeds an example doesn't trip the parser).
          2. Parses as a dict (not a list - that's the picks shape).
          3. Writes one ``row_kind="summary"`` row carrying the rule
             tweak + headline metrics, then one ``row_kind="observation"``
             row per entry in the ``observations`` array.

        Returns the number of rows written. Never raises - extraction
        failures log a warning and return 0; the brief still emails.
        """
        import json
        from functionality.log_writer import log_ag_review_observation

        if not response_text:
            return 0
        match = cls._PICK_BLOCK_OBJECT_RE.search(response_text)
        if not match:
            logger.warning(
                "[!] AG '%s': no fenced JSON OBJECT (expected for review): "
                "format drift or truncated brief?",
                group_name,
            )
            return 0
        raw = match.group(1)
        try:
            obj, repairs = cls._json_loads_lenient(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "[!] AG '%s': review JSON failed to parse (%s)",
                group_name, exc,
            )
            return 0
        if repairs:
            logger.warning(
                "[!] AG '%s': review JSON was malformed but auto-repaired "
                "(%d comma fix(es) applied).",
                group_name, repairs,
            )
        if not isinstance(obj, dict):
            logger.warning(
                "[!] AG '%s': review JSON is not an object (got %s)",
                group_name, type(obj).__name__,
            )
            return 0

        def _safe_str(v, max_len: int = 2000) -> str:
            if v is None:
                return ""
            return str(v)[:max_len]

        def _safe_int(v) -> int:
            try:
                return int(v) if v is not None else 0
            except (TypeError, ValueError):
                return 0

        def _safe_float(v) -> float:
            try:
                f = float(v) if v is not None else 0.0
                return f if f == f else 0.0
            except (TypeError, ValueError):
                return 0.0

        period_start = _safe_str(obj.get("review_period_start"), 32)
        period_end = _safe_str(obj.get("review_period_end"), 32)
        period_days = _safe_int(obj.get("review_period_days"))
        n_overall = _safe_int(obj.get("n_picks_overall"))
        n_account_fit = _safe_int(obj.get("n_picks_account_fit"))
        hit_overall = _safe_float(obj.get("hit_rate_overall"))
        hit_account_fit = _safe_float(obj.get("hit_rate_account_fit"))
        best_class = _safe_str(obj.get("best_signal_class"), 80)
        worst_class = _safe_str(obj.get("worst_signal_class"), 80)

        rule_tweak = obj.get("rule_tweak") or {}
        if not isinstance(rule_tweak, dict):
            rule_tweak = {}
        tweak_text = _safe_str(rule_tweak.get("recommendation"), 1000)
        tweak_rationale = _safe_str(rule_tweak.get("rationale"), 1500)
        tweak_impact = _safe_str(rule_tweak.get("expected_impact"), 1500)

        # Calibration verdict (added 2026-05-06). Optional in the JSON
        # tail - review runs that pre-date the calibration prompt edit
        # won't carry these keys, and the prompt explicitly skips
        # rendering when total closures < 10. Defaults: "" / 0 mean
        # "not computed" - distinguishable from a real verdict for the
        # SPQL trend queries the user will eventually run.
        cal_status_raw = _safe_str(obj.get("calibration_status"), 32).lower()
        valid_cal_statuses = {
            "well_calibrated", "overconfident",
            "underconfident", "insufficient_data",
        }
        cal_status = cal_status_raw if cal_status_raw in valid_cal_statuses else ""
        cal_n_closures = _safe_int(obj.get("calibration_n_closures"))

        rows_written = 0
        # Summary row
        try:
            log_ag_review_observation(
                alert_group=group_name,
                run_request_id=run_request_id,
                review_period_start=period_start,
                review_period_end=period_end,
                review_period_days=period_days,
                n_picks_overall=n_overall,
                n_picks_account_fit=n_account_fit,
                hit_rate_overall=hit_overall,
                hit_rate_account_fit=hit_account_fit,
                best_signal_class=best_class,
                worst_signal_class=worst_class,
                rule_tweak_recommendation_text=tweak_text,
                rule_tweak_rationale=tweak_rationale,
                rule_tweak_expected_impact=tweak_impact,
                row_kind="summary",
                calibration_status=cal_status,
                calibration_n_closures=cal_n_closures,
            )
            rows_written += 1
        except Exception as exc:
            logger.warning(
                "[!] AG '%s': summary row write failed: %s", group_name, exc,
            )

        # Observation rows
        observations = obj.get("observations") or []
        if not isinstance(observations, list):
            observations = []
        for obs in observations:
            if not isinstance(obs, dict):
                continue
            try:
                log_ag_review_observation(
                    alert_group=group_name,
                    run_request_id=run_request_id,
                    review_period_start=period_start,
                    review_period_end=period_end,
                    review_period_days=period_days,
                    n_picks_overall=n_overall,
                    n_picks_account_fit=n_account_fit,
                    hit_rate_overall=hit_overall,
                    hit_rate_account_fit=hit_account_fit,
                    best_signal_class=best_class,
                    worst_signal_class=worst_class,
                    observation_text=_safe_str(obs.get("text"), 2000),
                    observation_evidence=_safe_str(obs.get("evidence"), 2000),
                    observation_actionable=bool(obs.get("actionable")),
                    row_kind="observation",
                    calibration_status=cal_status,
                    calibration_n_closures=cal_n_closures,
                )
                rows_written += 1
            except Exception as exc:
                logger.warning(
                    "[!] AG '%s': observation row write failed: %s",
                    group_name, exc,
                )
        return rows_written

    # Regex matches the LAST fenced JSON block whose contents look like
    # an object (starts with `{`). Mirrors _PICK_BLOCK_RE which matches
    # arrays. Used by _extract_and_log_review_observations.
    _PICK_BLOCK_OBJECT_RE = __import__("re").compile(
        r"```(?:json)?\s*(\{[\s\S]*?\})\s*```(?!.*```)", __import__("re").DOTALL,
    )

    @classmethod
    def _validate_and_normalize_pick(
        cls, pick: dict, *, rank: int, group_name: str,
    ) -> dict | None:
        """Return a normalised pick dict, or None if invalid.

        Validation rules (trust Claude, verify the obvious):
          * All ``_REQUIRED_PICK_KEYS`` present.
          * ``idea_id`` matches ``type:id:direction`` format (lowercased
            on the way in).
          * ``instrument_type`` is one of the accepted values.
          * ``direction`` is one of the accepted values.
          * ``position_size_tier`` is one of the accepted values.
          * ``conviction_pct`` ∈ [0, 100]; ``expected_return_pct`` finite.
          * Epochs are positive ints; ``sell >= buy``.
          * Price fields are finite floats where present.

        Computed fields:
          * ``hold_hours = (sell - buy) // 3600`` if not provided.
          * ``source_signals`` - if list/tuple, semicolon-join.
        """
        missing = [k for k in cls._REQUIRED_PICK_KEYS if k not in pick]
        if missing:
            logger.warning(
                "[!] AG '%s': pick #%d missing required keys %s (skipping)",
                group_name, rank, missing,
            )
            return None

        idea_id = str(pick["idea_id"]).strip().lower()
        if not cls._IDEA_ID_RE.match(idea_id):
            logger.warning(
                "[!] AG '%s': pick #%d idea_id %r does not match "
                "'{type}:{id}:{direction}' format (skipping)",
                group_name, rank, idea_id,
            )
            return None

        instrument_type = str(pick["instrument_type"]).strip().lower()
        if instrument_type not in cls._VALID_INSTRUMENT_TYPES:
            logger.warning(
                "[!] AG '%s': pick #%d unknown instrument_type %r "
                "(expected one of %s, skipping)",
                group_name, rank, instrument_type,
                sorted(cls._VALID_INSTRUMENT_TYPES),
            )
            return None

        direction = str(pick["direction"]).strip().upper()
        if direction not in cls._VALID_DIRECTIONS:
            logger.warning(
                "[!] AG '%s': pick #%d unknown direction %r (skipping)",
                group_name, rank, direction,
            )
            return None

        position_size_tier = str(pick["position_size_tier"]).strip().upper()
        if position_size_tier not in cls._VALID_POSITION_TIERS:
            logger.warning(
                "[!] AG '%s': pick #%d unknown position_size_tier %r (skipping)",
                group_name, rank, position_size_tier,
            )
            return None

        try:
            conviction_pct = int(pick["conviction_pct"])
            expected_return_pct = float(pick["expected_return_pct"])
            entry_price = float(pick["entry_price"])
            suggested_buy_epoch = int(pick["suggested_buy_epoch"])
            suggested_sell_epoch = int(pick["suggested_sell_epoch"])
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[!] AG '%s': pick #%d numeric field coercion failed: %s",
                group_name, rank, exc,
            )
            return None

        if not (0 <= conviction_pct <= 100):
            logger.warning(
                "[!] AG '%s': pick #%d conviction_pct %d out of [0,100]",
                group_name, rank, conviction_pct,
            )
            return None
        if suggested_buy_epoch <= 0 or suggested_sell_epoch <= 0:
            logger.warning(
                "[!] AG '%s': pick #%d non-positive epoch(s) buy=%d sell=%d",
                group_name, rank, suggested_buy_epoch, suggested_sell_epoch,
            )
            return None
        if suggested_sell_epoch < suggested_buy_epoch:
            logger.warning(
                "[!] AG '%s': pick #%d sell epoch (%d) before buy epoch (%d)",
                group_name, rank, suggested_sell_epoch, suggested_buy_epoch,
            )
            return None

        # Optional prices - may be None
        def _maybe_float(key):
            v = pick.get(key)
            if v is None or v == "":
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        take_profit_price = _maybe_float("take_profit_price")
        stop_loss_price = _maybe_float("stop_loss_price")

        # hold_hours: prefer Claude's value if provided and consistent;
        # else compute from the epochs.
        hold_hours_raw = pick.get("hold_hours")
        if hold_hours_raw is not None:
            try:
                hold_hours = int(hold_hours_raw)
                if hold_hours < 0:
                    hold_hours = (suggested_sell_epoch - suggested_buy_epoch) // 3600
            except (TypeError, ValueError):
                hold_hours = (suggested_sell_epoch - suggested_buy_epoch) // 3600
        else:
            hold_hours = (suggested_sell_epoch - suggested_buy_epoch) // 3600

        # source_signals may come as a list or a semicolon-delimited string
        src = pick.get("source_signals", "")
        if isinstance(src, (list, tuple)):
            source_signals = "; ".join(str(s) for s in src)
        else:
            source_signals = str(src or "")

        # Strip / bound long free-text fields (Parquet tolerates large
        # strings, but we don't want unbounded log rows).
        thesis = str(pick["thesis"])[:2000]
        exit_catalyst = str(pick["exit_catalyst"])[:1000]
        instrument_id = str(pick["instrument_id"]).strip().lower()

        # New fields added 2026-04-23 with the tightened prompt template.
        # All optional - the parser accepts old-shape JSON too.
        # pick_rank (preferred key from new prompt) → rank_in_brief column.
        # pick_tier: "TOP" (default) | "HONORABLE_MENTION".
        pick_rank_raw = pick.get("pick_rank")
        if pick_rank_raw is not None:
            try:
                pick_rank_claude = int(pick_rank_raw)
                if pick_rank_claude > 0:
                    rank = pick_rank_claude
            except (TypeError, ValueError):
                pass
        pick_tier = str(pick.get("pick_tier", "TOP")).strip().upper()
        if pick_tier not in ("TOP", "HONORABLE_MENTION"):
            pick_tier = "TOP"
        correlation_cluster = str(pick.get("correlation_cluster", ""))[:80].strip().lower()
        squeeze = pick.get("short_squeeze_risk")
        if isinstance(squeeze, dict):
            import json as _json
            short_squeeze_risk_json = _json.dumps(squeeze, separators=(",", ":"))[:500]
        else:
            short_squeeze_risk_json = ""

        # Options-specific optional fields (Options Edge Brief, 2026-04-26).
        # Every field is None-safe; OEB picks populate them, other AGs leave them out.
        option_structure_raw = pick.get("option_structure")
        option_structure = (
            str(option_structure_raw)[:60].strip().lower()
            if option_structure_raw is not None and option_structure_raw != ""
            else None
        )

        option_legs = pick.get("option_legs")
        if isinstance(option_legs, list) and option_legs:
            import json as _json
            try:
                option_legs_json = _json.dumps(option_legs, separators=(",", ":"))[:4000]
            except (TypeError, ValueError):
                option_legs_json = None
        else:
            option_legs_json = None

        def _maybe_float_optional(key):
            v = pick.get(key)
            if v is None or v == "":
                return None
            try:
                f = float(v)
                if f != f:  # NaN check
                    return None
                return f
            except (TypeError, ValueError):
                return None

        def _maybe_int_optional(key):
            v = pick.get(key)
            if v is None or v == "":
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        option_max_loss_usd = _maybe_float_optional("option_max_loss_usd")
        option_max_profit_usd = _maybe_float_optional("option_max_profit_usd")
        option_net_debit_credit = _maybe_float_optional("option_net_debit_credit")
        option_dte_days = _maybe_int_optional("option_dte_days")
        account_size_floor_usd = _maybe_float_optional("account_size_floor_usd")

        difficulty_raw = pick.get("option_difficulty_tier")
        if difficulty_raw is None or difficulty_raw == "":
            option_difficulty_tier = None
        else:
            difficulty_norm = str(difficulty_raw).strip().upper()
            option_difficulty_tier = (
                difficulty_norm
                if difficulty_norm in ("BEGINNER", "INTERMEDIATE", "ADVANCED")
                else None
            )

        return {
            "rank_in_brief": rank,
            "pick_tier": pick_tier,
            "idea_id": idea_id,
            "instrument_type": instrument_type,
            "instrument_id": instrument_id,
            "direction": direction,
            "conviction_pct": conviction_pct,
            "expected_return_pct": expected_return_pct,
            "position_size_tier": position_size_tier,
            "entry_price": entry_price,
            "suggested_buy_epoch": suggested_buy_epoch,
            "suggested_sell_epoch": suggested_sell_epoch,
            "hold_hours": hold_hours,
            "take_profit_price": take_profit_price,
            "stop_loss_price": stop_loss_price,
            "exit_catalyst": exit_catalyst,
            "thesis": thesis,
            "source_signals": source_signals,
            "correlation_cluster": correlation_cluster,
            "short_squeeze_risk_json": short_squeeze_risk_json,
            "option_structure": option_structure,
            "option_legs_json": option_legs_json,
            "option_max_loss_usd": option_max_loss_usd,
            "option_max_profit_usd": option_max_profit_usd,
            "option_net_debit_credit": option_net_debit_credit,
            "option_dte_days": option_dte_days,
            "option_difficulty_tier": option_difficulty_tier,
            "account_size_floor_usd": account_size_floor_usd,
        }


    @staticmethod
    def _send_html_email(subject: str, plain_body: str, group_name: str,
                         to_addrs: str, meta: dict,
                         template_override: str | None = None,
                         attach_markdown: bool = False,
                         attachment_text: str | None = None):
        """Send a branded HTML email with plain-text fallback.

        When ``template_override`` is provided, it is used verbatim (with
        token substitution) instead of the default branded SpeakesQuery
        layout. See ``build_html_email`` for the supported tokens.

        When ``attach_markdown=True``, the full plain-text response is
        attached as ``<group>_<date>.md``. This guarantees the recipient
        has the complete Claude response even if the HTML body is
        truncated by the email client, clipped by Gmail's 102 KB preview
        threshold, or malformed by a template override. Belt-and-suspenders
        against the 2026-04-20 first-brief truncation: operator saw the
        full story in the attachment while the inline HTML showed only
        opportunity #1.

        ``attachment_text`` (2026-08-04) overrides what lands in the .md
        attachment. The digest path emails a distilled body but must
        attach the COMPLETE raw analyst output - defaults to
        ``plain_body`` when omitted.
        """
        from query_engine.Alert import (
            load_smtp_config_from_env,
            resolve_and_normalize_recipients,
        )
        import asyncio
        import ssl
        from email.message import EmailMessage

        # Resolve `@group_name` references + split comma/semicolon
        # delimited addresses + de-dupe + validate. Single choke point
        # so the AG path matches the saved-search path.
        recipients = resolve_and_normalize_recipients(to_addrs)
        if not recipients:
            raise ValueError("No valid recipient addresses provided.")

        cfg = load_smtp_config_from_env()

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg.from_addr
        msg["To"] = ", ".join(recipients)

        # Plain text fallback
        msg.set_content(plain_body)

        # HTML version
        html_body = build_html_email(
            group_name, plain_body, meta,
            template_override=template_override,
        )
        msg.add_alternative(html_body, subtype="html")

        # Attach the complete plain-text response as a .md file so nothing
        # the user paid for is lost to inline-HTML truncation or email-client
        # oddities. This is a content-safety net, not a primary delivery path.
        if attach_markdown:
            import datetime as _dt
            safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", group_name).strip("_")
            fname = (
                f"{safe_name or 'alert_group'}_"
                f"{_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%d_%H%M%S')}.md"
            )
            try:
                msg.add_attachment(
                    (attachment_text if attachment_text is not None
                     else (plain_body or "")).encode("utf-8"),
                    maintype="text",
                    subtype="markdown",
                    filename=fname,
                )
            except Exception as exc:
                logger.warning(
                    "[!] Could not attach markdown copy (%s) - inline-only: %s",
                    fname, exc,
                )

        # Send
        import aiosmtplib

        async def _send():
            try:
                import certifi
                tls_context = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                tls_context = ssl.create_default_context()

            await aiosmtplib.send(
                msg,
                hostname=cfg.server,
                port=cfg.port,
                username=cfg.user,
                password=cfg.password,
                start_tls=cfg.start_tls,
                tls_context=tls_context if cfg.start_tls else None,
                timeout=30,
            )

        _run_coroutine_from_sync_context(_send())
        logger.info("[i] HTML email sent to %s", ", ".join(recipients))

    @staticmethod
    def _log_run(result: AlertGroupRunResult):
        """Persist run to the alert_group_runs SQLite table."""
        try:
            from alert_group_store import AlertGroupStore
            store = AlertGroupStore()
            store.initialize()
            store.log_run(
                group_name=result.group_name,
                status=result.status,
                searches_used=result.searches_used or None,
                estimated_tokens=result.estimated_tokens or None,
                actual_tokens=result.actual_tokens or None,
                cost_usd=result.cost_usd or None,
                error_message=result.error_message or None,
            )
        except Exception as exc:
            logger.warning("[!] Failed to log alert group run: %s", exc)

    @staticmethod
    def _emit_log(
        result: AlertGroupRunResult,
        started: float,
        *,
        dry_run: bool,
    ) -> None:
        """Write the alert_groups log row for this dispatch attempt."""
        try:
            duration_ms = int((time.monotonic() - started) * 1000)
            log_alert_group_event(
                group_name=result.group_name,
                status=result.status,
                searches_used=list(result.searches_used or []),
                estimated_tokens=result.estimated_tokens or None,
                actual_tokens=result.actual_tokens or None,
                cost_usd=result.cost_usd or None,
                error_message=result.error_message or None,
                duration_ms=duration_ms,
                dry_run=dry_run,
                # Per-phase timings for SPQL bottleneck aggregation.
                feeder_loop_ms=result.feeder_loop_ms,
                claude_call_ms=result.claude_call_ms,
                email_send_ms=result.email_send_ms,
            )
        except Exception as exc:
            logger.warning("[!] Failed to emit alert_groups log row: %s", exc)

    @classmethod
    def _maybe_send_failure_email(cls, result: AlertGroupRunResult) -> None:
        """Send a notification to the admin when a run ends in ``error``.

        Gated by the ``alert_group_failure_email_enabled`` setting (default
        ``True``). Recipient priority (Wave 5, 2026-04-26):
          1. The AG's per-group ``admin_error_email`` field (lets each
             alert group route its errors to a different admin in
             multi-tenant / customer-recipient deployments).
          2. The global ``alert_group_failure_email_to`` setting.
          3. ``smtp_from`` (whoever Gmail is sending from).

        The recipient choice is the central design point of Wave 5: the
        customer-facing ``email_address`` (often a paid mailing list) must
        NEVER receive failure / diagnostic notices, only the analyst brief.
        Errors go to the operator. The email is plain-text - no Claude
        involvement - so this path works even when the Claude outage is
        the reason for the failure.
        """
        if result.status != "error":
            return
        try:
            from global_settings import get_settings
            settings = get_settings()
            if not settings.get("alert_group_failure_email_enabled"):
                return

            # Wave 5 (2026-04-26): per-AG admin override. The dispatcher
            # has the AG name; load its YAML to read the optional
            # ``admin_error_email`` field AND the 2026-04-27
            # ``error_email_disabled`` opt-out. We deliberately re-load
            # rather than caching so an operator can change either
            # field in the UI between runs without restarting.
            per_ag_admin = ""
            opted_out = False
            try:
                from alert_group_store import AlertGroupStore
                _store = AlertGroupStore()
                _g = _store.get_group(result.group_name)
                per_ag_admin = (_g.get("admin_error_email") or "").strip()
                opted_out = bool(_g.get("error_email_disabled", False))
            except Exception:
                per_ag_admin = ""
                opted_out = False

            # Per-AG opt-out short-circuits BEFORE consulting fallbacks.
            # Logged at INFO so the operator can see why no email went
            # out (rather than silently dropping the notification).
            if opted_out:
                logger.info(
                    "[i] Alert group failure email skipped - '%s' has "
                    "error_email_disabled=true.",
                    result.group_name,
                )
                return

            to_addr = (
                per_ag_admin
                or (settings.get("alert_group_failure_email_to") or "").strip()
                or (settings.get("smtp_from") or "").strip()
                or (settings.get("smtp_user") or "").strip()
            )
            if not to_addr or "@" not in to_addr:
                logger.warning(
                    "[!] Alert group failure email skipped - no recipient configured "
                    "(per-AG admin_error_email, alert_group_failure_email_to, "
                    "smtp_from, smtp_user all empty)."
                )
                return

            subject = (
                f"[SpeakesQuery] Alert group '{result.group_name}' failed"
            )
            body = (
                f"Alert group: {result.group_name}\n"
                f"Status: {result.status}\n"
                f"Error: {result.error_message}\n\n"
                f"Searches attempted: {', '.join(result.searches_used) or '(none)'}\n"
                f"Estimated tokens: {result.estimated_tokens or 0}\n"
                f"\n"
                f"This notification was sent because the alert group did not "
                f"complete successfully. Disable with "
                f"`alert_group_failure_email_enabled: false` in global settings."
            )
            cls._send_plain_email(subject, body, to_addr)
        except Exception as exc:
            logger.warning("[!] Alert group failure email dispatch failed: %s", exc)

    @staticmethod
    def _send_plain_email(subject: str, body: str, to_addr: str) -> None:
        """Plain-text email helper for operational notifications."""
        from query_engine.Alert import load_smtp_config_from_env
        import asyncio
        import ssl
        from email.message import EmailMessage

        cfg = load_smtp_config_from_env()
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg.from_addr
        msg["To"] = to_addr
        msg.set_content(body)

        import aiosmtplib

        async def _send():
            try:
                import certifi
                tls_context = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                tls_context = ssl.create_default_context()
            await aiosmtplib.send(
                msg,
                hostname=cfg.server,
                port=cfg.port,
                username=cfg.user,
                password=cfg.password,
                start_tls=cfg.start_tls,
                tls_context=tls_context if cfg.start_tls else None,
                timeout=30,
            )

        _run_coroutine_from_sync_context(_send())
        logger.info("[i] Plain failure notice emailed to %s", to_addr)

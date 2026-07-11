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


def _markdown_to_html(text: str) -> str:
    """Minimal markdown-to-HTML conversion for Claude's response text."""
    import html as html_mod

    text = html_mod.escape(text)

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
    if meta.get("prompt_only"):
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
        # LLM calls (pinned by tests/test_curator_composer_slice2.py).
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

        # ── Circuit breaker: if a prior guard tripped it, refuse to dispatch
        # until the user manually resets. Prevents burning Claude tokens on
        # a persistently-failing group. Reset via
        # POST /api/alert-groups/<name>/reset-circuit-breaker.
        # ``force=True`` also bypasses the breaker so an operator can
        # manually retry once they've fixed whatever caused the trip.
        if not force and group.get("circuit_breaker_tripped"):
            result.status = "error"
            result.error_message = (
                "Circuit breaker tripped - skipping. Reset via "
                "POST /api/alert-groups/<name>/reset-circuit-breaker "
                "after investigating the failure reason."
            )
            logger.warning(
                "[!] Alert group '%s' skipped (circuit breaker tripped).",
                group_name,
            )
            self._log_run(result)
            self._emit_log(result, run_started, dry_run=dry_run)
            self._maybe_send_failure_email(result)
            return result

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
        # Slice 6 (2026-05-17): captured for the playlist composer's
        # hybrid expansion path - _extract_and_log_playlist reads from
        # the first feeder DataFrame to bulk-fill positions beyond the
        # LLM-composed top. Empty dict (or `output_kind != "playlist"`)
        # means the hybrid path no-ops; only the playlist AG actually
        # consumes this.
        feeder_dfs: dict[str, "pd.DataFrame"] = {}
        # Slice 11 (2026-05-17 - speaktube req #10): the keyword pool
        # the dispatcher injected into prompt + boost. Empty when no
        # active pool (or feature disabled). Captured here so the
        # $KEYWORD_POOL prompt placeholder substitution has a value
        # even when the feeder loop runs 0 times.
        runtime_keyword_pool: list[str] = []

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
            # Phase 6 / Bet 5 slice 3 hook (2026-05-16): when the AG
            # opts in via ``apply_topic_scoring: true``, augment the
            # feeder DataFrame with topic-similarity columns BEFORE
            # serialisation. Failures degrade gracefully - log a
            # warning and leave the DataFrame unchanged so the
            # composer always has SOMETHING to render. The scoring
            # logic + snapshot loader live in
            # :mod:`analyzers.topic_vectors`; this dispatcher hook is
            # the thin AG-aware wire.
            df = self._maybe_apply_topic_scoring(df, group, group_name=group_name)
            # Slice 11 (2026-05-17 - speaktube req #10): boost
            # interest_score on title matches against the active
            # keyword pool. Runs AFTER topic-scoring so keyword boost
            # stacks on top of topic-similarity. Returns the captured
            # keyword list so the dispatcher can also inject it into
            # the composer prompt as $KEYWORD_POOL.
            df, runtime_keyword_pool = self._maybe_apply_keyword_boost(
                df, group, group_name=group_name,
            )
            # Slice 6 (2026-05-17): capture post-scoring DataFrame so
            # the playlist composer's hybrid expansion can bulk-fill
            # from the same rows the LLM saw in the prompt.
            if df is not None:
                feeder_dfs[name] = df
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

        # Slice 10 (2026-05-17, speaktube req #12): for playlist AGs,
        # substitute the runtime growth_dial value AND thin-history
        # state into the prompt BEFORE the builder runs (so the budget
        # estimate matches what actually flies to Claude). The
        # previous prompt had hard-coded "defaults to -0.7" text that
        # the LLM read as the dial value - the operator's slider had
        # zero effect on composition. The composer prompt template now
        # uses ``$GROWTH_DIAL_VALUE`` + ``$THIN_HISTORY_ACTIVE``
        # placeholders that the dispatcher replaces with live values.
        #
        # Thin-history detection: when the user has watched fewer than
        # `curator_thin_history_threshold_seconds` of video in the
        # trailing 30 days, the effective dial is boosted by
        # `curator_thin_history_dial_bias` (default +0.5, clamped to
        # [-1, +1]).
        #
        # The runtime values are also captured here so the post-LLM
        # write paths (_log_playlist_items + _log_bulk_playlist_extras)
        # can record the EFFECTIVE dial + thin-history state in each
        # playlist row.
        runtime_curator_growth_dial = None
        runtime_thin_history_active = False
        if (group.get("output_kind") or "").strip().lower() == "playlist":
            try:
                stored_dial = float(
                    self._get_setting("curator_growth_dial", -0.7)
                )
            except (TypeError, ValueError):
                stored_dial = -0.7
            thin_active, watched_30d = self._compute_curator_thin_history()
            effective_dial = self._compute_effective_growth_dial(
                stored_dial=stored_dial,
                thin_history_active=thin_active,
            )
            runtime_curator_growth_dial = effective_dial
            runtime_thin_history_active = thin_active
            logger.info(
                "[i] AG '%s': curator runtime - stored_dial=%.2f, "
                "thin_history=%s (watched_30d=%ds), effective_dial=%.2f",
                group_name, stored_dial, thin_active, watched_30d,
                effective_dial,
            )
            prompt_text = prompt_text.replace(
                "$GROWTH_DIAL_VALUE", f"{effective_dial:.2f}",
            ).replace(
                "$THIN_HISTORY_ACTIVE",
                "true" if thin_active else "false",
            )
            # Slice 11 (2026-05-17 - speaktube req #10): inject the
            # active keyword pool so the LLM sees the operator's
            # recent keywords explicitly (in addition to the boosted
            # interest_scores on matching candidates). Empty pool
            # renders as "(none)" so the LLM doesn't waste tokens
            # interpreting an empty list.
            if runtime_keyword_pool:
                pool_str = ", ".join(runtime_keyword_pool)
            else:
                pool_str = "(none)"
            prompt_text = prompt_text.replace("$KEYWORD_POOL", pool_str)
            logger.info(
                "[i] AG '%s': curator runtime - keyword pool size=%d "
                "(%s)",
                group_name, len(runtime_keyword_pool), pool_str[:100],
            )

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
        # surface - empty-text guard, pick/playlist extraction, email,
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
                call, response_text, response_meta = self._call_router_llm(
                    group_name=group_name,
                    model_id=model,
                    user_content=(messages[0]["content"] if messages else ""),
                    max_tokens=max_tokens,
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
            try:
                call: ClaudeCallResult = call_messages_create(
                    source="alert_group",
                    group_name=group_name,
                    model=model,
                    max_tokens=max_tokens,
                    messages=messages,
                    tools=[{"type": "web_search_20250305", "name": "web_search"}],
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

        # Phase 6 / Bet 5 slice 2 (2026-05-16): curator playlist composer.
        # When the AG declares ``output_kind: playlist``, the LLM's
        # response carries a {run_date, growth_dial, theme, items[...]}
        # object (NOT a picks array). The standard pick extraction above
        # returns zero for this AG; this parallel path parses the
        # playlist object and writes one row per item to
        # indexes/IMMUTABLE/curator_playlist/ via log_curator_playlist_item.
        # GET /api/playlist/today then serves the most-recent composition
        # to the speaktube player. See docs/lang/21_curator_speaktube.md.
        output_kind = (group.get("output_kind") or "").strip().lower()
        if output_kind == "playlist":
            try:
                item_count = self._extract_and_log_playlist(
                    response_text=response_text,
                    group_name=group_name,
                    run_request_id=call.request_id,
                    model_used=model,
                    feeder_dfs=feeder_dfs,
                    effective_growth_dial=runtime_curator_growth_dial,
                    thin_history_active=runtime_thin_history_active,
                )
                if item_count > 0:
                    logger.info(
                        "[i] AG '%s': captured %d playlist item(s) into "
                        "indexes/IMMUTABLE/curator_playlist/",
                        group_name, item_count,
                    )
            except Exception as exc:
                logger.warning(
                    "[!] AG '%s': playlist extraction failed (dispatch continues): %s",
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
                self._send_html_email(
                    subject=(
                        f"[SpeakesQuery REPORT] {group_name} - {subject_date}"
                        f"{subject_suffix}"
                    ),
                    plain_body=response_text,
                    group_name=group_name,
                    to_addrs=email_address,
                    meta={
                        "searches_used": result.searches_used,
                        "estimated_tokens": result.estimated_tokens,
                        "actual_tokens": result.actual_tokens,
                        "cost_usd": result.cost_usd,
                        "truncated": truncated,
                        "stop_reason": stop_reason,
                    },
                    template_override=(group.get("email_template_override") or ""),
                    attach_markdown=True,
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
    def _maybe_apply_topic_scoring(
        cls,
        df: Any,
        group: dict,
        *,
        group_name: str = "",
    ) -> Any:
        """Augment a feeder DataFrame with topic-similarity columns.

        Hook for Phase 6 / Bet 5 slice 3 (2026-05-16): when the AG
        opts in via ``apply_topic_scoring: true`` in its YAML, this
        method loads the latest topic snapshot from
        ``indexes/IMMUTABLE/curator_topic_snapshots/`` and re-scores
        every row by topical similarity to the user's cluster
        centroids, replacing the bootstrap-locked watch-count
        ``interest_score`` from slice 1.5.

        Failure modes (snapshot missing, embedder unavailable,
        column-shape surprise) DEGRADE GRACEFULLY: the original
        DataFrame is returned unchanged and a warning is logged.
        The composer's prompt should describe the score's intended
        meaning so the LLM can still produce a sensible playlist
        even when scoring fell back to the legacy column.

        Behaviour matrix:
            apply_topic_scoring=False / missing → df unchanged
            df is None                          → df unchanged
            no snapshot persisted yet           → df unchanged + warn
            score helper raises                 → df unchanged + warn
            success                             → df + new score cols
        """
        if df is None:
            return df
        if not bool(group.get("apply_topic_scoring", False)):
            return df

        try:
            from analyzers.topic_vectors import (
                load_latest_snapshot,
                score_candidates_against_snapshot,
            )
        except Exception as exc:
            logger.warning(
                "[!] AG '%s': apply_topic_scoring imports failed "
                "(slice-3 deps missing?): %s - leaving feeder rows "
                "unscored", group_name, exc,
            )
            return df

        try:
            snapshot = load_latest_snapshot()
        except Exception as exc:
            logger.warning(
                "[!] AG '%s': load_latest_snapshot raised: %s - "
                "leaving feeder rows unscored", group_name, exc,
            )
            return df
        if snapshot is None or not snapshot.clusters:
            logger.warning(
                "[!] AG '%s': no curator topic snapshot persisted yet - "
                "leaving feeder rows unscored. Run "
                "'python -m tools.curator_topic_snapshot_refresh' to "
                "bootstrap.", group_name,
            )
            return df

        # The candidate DataFrame's title column is conventionally
        # ``title`` (slice 1.5 canonical schema), but allow per-AG
        # override for forward compatibility with non-video composers.
        title_col = str(group.get("topic_scoring_title_col", "title"))
        try:
            scored = score_candidates_against_snapshot(
                df, snapshot, title_col=title_col,
            )
        except Exception as exc:
            logger.warning(
                "[!] AG '%s': score_candidates_against_snapshot raised "
                "(title_col=%s, snapshot_id=%s): %s - leaving feeder "
                "rows unscored",
                group_name, title_col, snapshot.snapshot_id, exc,
            )
            return df

        logger.info(
            "[i] AG '%s': topic-scoring applied (snapshot_id=%s, %d "
            "clusters, %d rows)",
            group_name, snapshot.snapshot_id, len(snapshot.clusters),
            len(scored.index),
        )
        return scored

    @classmethod
    def _maybe_apply_keyword_boost(
        cls,
        df: Any,
        group: dict,
        *,
        group_name: str = "",
    ) -> tuple[Any, list[str]]:
        """Slice 11 (2026-05-17 - speaktube req #10): boost
        ``interest_score`` on candidates whose ``title`` contains an
        active-pool keyword (case-insensitive substring).

        Returns ``(df, active_keywords)``. The active keyword list is
        also returned so the dispatcher can inject ``$KEYWORD_POOL``
        into the composer prompt (the LLM gets to know about the
        operator's recent keywords explicitly, AND see boosted scores
        on matching items - defense in depth).

        Behaviour matrix:
            output_kind != "playlist"                  → df unchanged, []
            curator_keyword_boost_enabled is False     → df unchanged, []
            active pool is empty                       → df unchanged, []
            df is None / missing 'title' column        → df unchanged, []
            success                                    → df with boosted
                                                         interest_score
                                                         on matching
                                                         rows, plus the
                                                         keyword list

        Boost stacks on top of any prior interest_score (from feeder
        or from topic-scoring). Each row gets at most +boost_amount
        once even if it matches multiple keywords - the boost is
        about "was matched", not "how many times matched". Result is
        clamped to [0.0, 1.0] (interest_score's canonical range).

        Failure modes degrade gracefully - log warning, return df
        unchanged.
        """
        # Only playlist AGs participate; other AGs leave their feeder
        # DataFrames alone.
        if (group.get("output_kind") or "").strip().lower() != "playlist":
            return df, []
        if df is None:
            return df, []
        try:
            enabled = bool(cls._get_setting(
                "curator_keyword_boost_enabled", True,
            ))
        except Exception:
            enabled = True
        if not enabled:
            return df, []

        try:
            fallback = int(cls._get_setting(
                "curator_keyword_pool_fallback_seconds", 86400,
            ))
        except (TypeError, ValueError):
            fallback = 86400

        try:
            from functionality.log_writer import read_active_curator_keyword_pool
            keywords = read_active_curator_keyword_pool(
                fallback_seconds=fallback,
            )
        except Exception as exc:
            logger.warning(
                "[!] AG '%s': keyword-pool read failed: %s - leaving "
                "feeder rows un-boosted",
                group_name, exc,
            )
            return df, []

        if not keywords:
            return df, []

        if "title" not in df.columns:
            logger.warning(
                "[!] AG '%s': feeder DF has no 'title' column - "
                "keyword boost skipped (active_keywords=%d)",
                group_name, len(keywords),
            )
            return df, keywords

        try:
            boost = float(cls._get_setting(
                "curator_keyword_boost_amount", 0.2,
            ))
        except (TypeError, ValueError):
            boost = 0.2
        # Clamp to canonical interest_score range. Boost <=0 is a
        # no-op (operator effectively disabled via setting).
        boost = max(0.0, min(1.0, boost))
        if boost <= 0.0:
            return df, keywords

        # Build a regex alternation for case-insensitive substring match.
        # Each keyword is escaped (literal substring, not regex).
        import re
        try:
            pattern = "|".join(re.escape(kw) for kw in keywords if kw)
        except Exception as exc:
            logger.warning(
                "[!] AG '%s': keyword regex build failed: %s",
                group_name, exc,
            )
            return df, keywords
        if not pattern:
            return df, keywords

        try:
            # pandas.Series.str.contains with case=False, regex=True.
            # NaN titles get fill=False (no boost).
            df_out = df.copy()
            matches_mask = df_out["title"].astype(str).str.contains(
                pattern, case=False, regex=True, na=False,
            )
            n_matches = int(matches_mask.sum())
            if "interest_score" in df_out.columns:
                # Boost matching rows; clamp to [0, 1]. Non-matches
                # unchanged.
                current = df_out["interest_score"].astype(float).fillna(0.0)
                boosted = current + (matches_mask.astype(float) * boost)
                df_out["interest_score"] = boosted.clip(lower=0.0, upper=1.0)
            else:
                # No interest_score yet - create one (matches get
                # boost, non-matches get 0.0). This is unusual: the
                # feeder normally provides interest_score.
                df_out["interest_score"] = matches_mask.astype(float) * boost
            logger.info(
                "[i] AG '%s': keyword boost applied "
                "(active_keywords=%d, boost=+%.2f, matched_rows=%d/%d)",
                group_name, len(keywords), boost, n_matches,
                len(df_out.index),
            )
            return df_out, keywords
        except Exception as exc:
            logger.warning(
                "[!] AG '%s': keyword-boost apply failed: %s - leaving "
                "feeder rows un-boosted",
                group_name, exc,
            )
            return df, keywords

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
                window_start = now - _dt.timedelta(hours=24)
                count_24h = sum(
                    1 for r in successful_runs
                    if (t := _parse(r.get("triggered_at") or ""))
                    and t >= window_start
                )
                if count_24h >= cap:
                    return (
                        f"already dispatched {count_24h} time(s) in last 24h "
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
            from alert_group_store import AlertGroupStore
            store = AlertGroupStore()
            store.initialize()
            store.update_group(group_name, {"circuit_breaker_tripped": True})
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
        """No-op marker - success runs are naturally recorded in the audit
        DB, so the consecutive-error counter reads 0 on next query. The
        method exists so callers signal intent at the success exit."""
        # Intentionally empty - see docstring. Preserved so the success
        # exit of _run_inner stays explicit rather than silent.
        return

    def _call_router_llm(
        self, *, group_name, model_id, user_content, max_tokens,
    ):
        """Dispatch an AG analysis call through the provider-agnostic LLM
        router (Slice A, 2026-06-23) and normalise the result into the same
        ``(call, response_text, response_meta)`` shape the Claude path
        produces - so the downstream pick/playlist/email/logging code stays
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
            elif last.msg.startswith(
                ("Expecting value", "Expecting property name"),
            ) and repaired[:last.pos].rstrip().endswith(","):
                # Trailing comma before ``}`` / ``]`` - drop the comma.
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

    # ── Curator playlist composer (Phase 6 / Bet 5 slice 2) ──────
    # When an AG declares ``output_kind: playlist``, the LLM emits a
    # fenced JSON object (same shape as _PICK_BLOCK_OBJECT_RE matches
    # for the OEB review AG). The dispatcher delegates to the three
    # methods below: ``_parse_playlist_block`` extracts + validates
    # the JSON, ``_log_playlist_items`` writes one row per item via
    # log_curator_playlist_item, ``_extract_and_log_playlist`` is
    # the orchestrator (mirrors the picks / review-obs split).
    #
    # The required item fields are checked at parse time; missing
    # fields produce a warning + skip that item. Extra fields are
    # ignored. Per the slice-2 design, the playlist row schema is
    # additive-only (see CLAUDE.md "Do Not" - curator_playlist is
    # one of three IMMUTABLE forever-data streams).

    @classmethod
    def _compute_curator_thin_history(cls) -> tuple[bool, int]:
        """Slice 10 (2026-05-17, speaktube req #12): detect "thin history"
        mode by summing ``watched_seconds`` from ``curator_telemetry`` for
        the trailing 30 days. Returns ``(active, watched_seconds_30d)``.

        Active iff ``curator_thin_history_enabled`` is True AND the sum
        is below ``curator_thin_history_threshold_seconds`` (default
        18000s = 5 hours). Telemetry parquet is read via DuckDB against
        ``indexes/IMMUTABLE/curator_telemetry/*.parquet``. Missing
        index (fresh install before any telemetry ingest) → 0
        watched-seconds → thin-history active (which is the desired
        behavior for a new account: be exploratory until we have
        signal).

        Never raises - telemetry query failures fall through to
        ``(False, 0)`` with a logged warning so the AG dispatch still
        completes even when the telemetry tree is unreadable.
        """
        enabled = bool(cls._get_setting("curator_thin_history_enabled", True))
        if not enabled:
            return (False, 0)

        try:
            threshold = int(cls._get_setting(
                "curator_thin_history_threshold_seconds", 18000,
            ))
        except (TypeError, ValueError):
            threshold = 18000

        watched_seconds = 0
        try:
            import duckdb
            from global_settings import get_settings

            # Use the settings.immutable_subdir() helper so test
            # fixtures that point immutable_root at a tmp_path
            # resolve correctly - mirrors `_curator_immutable_glob`
            # in desktop_app/server.py.
            root = get_settings().immutable_subdir("curator_telemetry")
            if root.exists():
                files = sorted(str(p) for p in root.rglob("*.parquet"))
                if files:
                    quoted = ", ".join(f"'{f}'" for f in files)
                    cutoff_epoch = int(time.time()) - (30 * 86400)
                    # Per-call connection - see the equivalent comment in
                    # functionality/log_writer.py::get_active_keyword_pool
                    # for the concurrency rationale. APScheduler runs the
                    # dispatcher on a worker thread that overlaps with
                    # request threads, so the module-level
                    # ``duckdb.sql()`` global connection is unsafe.
                    # union_by_name=true tolerates the curator_telemetry
                    # ingestion script writing empty parquets (pyarrow
                    # infers all-None columns as Null logical type) and
                    # additive schema drift. Drift-guarded by
                    # tests/test_curator_immutable_read_robustness.py.
                    con = duckdb.connect(database=":memory:")
                    try:
                        con.execute("PRAGMA threads=1")
                        row = con.execute(
                            f"SELECT SUM(watched_seconds) AS total "
                            f"FROM read_parquet([{quoted}], union_by_name=true) "
                            f"WHERE _epoch >= {cutoff_epoch} "
                            f"AND watched_seconds IS NOT NULL"
                        ).fetchone()
                    finally:
                        con.close()
                    if row and row[0] is not None:
                        watched_seconds = int(row[0])
        except Exception as exc:
            logger.warning(
                "[!] thin-history detection failed (treating as not "
                "thin): %s", exc,
            )
            return (False, 0)

        active = watched_seconds < threshold
        return (active, watched_seconds)

    @classmethod
    def _compute_effective_growth_dial(
        cls,
        stored_dial: float,
        thin_history_active: bool,
    ) -> float:
        """Slice 10 (2026-05-17): when thin-history is active, the
        operator's stored ``curator_growth_dial`` value gets boosted by
        ``curator_thin_history_dial_bias`` (default +0.5) - the
        composer composes more exploratory picks for users with thin
        watched-seconds telemetry. The result is clamped to the dial's
        canonical [-1.0, +1.0] range. When thin-history is inactive,
        returns the stored value unchanged.
        """
        if not thin_history_active:
            return float(stored_dial)
        try:
            bias = float(cls._get_setting(
                "curator_thin_history_dial_bias", 0.5,
            ))
        except (TypeError, ValueError):
            bias = 0.5
        boosted = float(stored_dial) + bias
        # Clamp to canonical bipolar range (slice 8)
        return max(-1.0, min(1.0, boosted))

    _REQUIRED_PLAYLIST_ITEM_KEYS = (
        "position", "slot_kind", "rationale",
        "video_external_id", "title", "channel_name",
    )

    @classmethod
    def _parse_playlist_block(
        cls,
        *,
        response_text: str,
        group_name: str,
    ) -> dict | None:
        """Parse the LLM's response_text and return ``{run_date, growth_dial,
        theme, items: [...]}`` with normalized items. Returns ``None`` if
        no parseable JSON block is found OR the structure is invalid.
        Per-item validation: items missing any required key are dropped
        with a logged warning; the rest still land.

        Pure function - no I/O, no side effects beyond logger warnings.
        Mirrors the parse / log split established by _parse_picks_block
        so a future "manual return" path for playlists can reuse just
        this half. See the config-leak canary pattern (CLAUDE.md).
        """
        import json
        if not response_text:
            return None
        match = cls._PICK_BLOCK_OBJECT_RE.search(response_text)
        if not match:
            logger.warning(
                "[!] AG '%s': no fenced JSON OBJECT for playlist composer - "
                "format drift or truncated response?",
                group_name,
            )
            return None
        raw = match.group(1)
        try:
            obj, repairs = cls._json_loads_lenient(raw)
        except Exception as exc:
            logger.warning(
                "[!] AG '%s': playlist JSON parse failed: %s - raw[:200]=%r",
                group_name, exc, raw[:200],
            )
            return None
        if repairs:
            logger.warning(
                "[!] AG '%s': playlist JSON was malformed but auto-repaired "
                "(%d comma fix(es) applied).",
                group_name, repairs,
            )
        if not isinstance(obj, dict):
            logger.warning(
                "[!] AG '%s': playlist JSON parsed but isn't an object (got %s)",
                group_name, type(obj).__name__,
            )
            return None

        run_date = str(obj.get("run_date") or "").strip()
        if not run_date:
            # Fall back to today UTC so a missing run_date doesn't sink
            # the whole composition. The schema does carry composed_at_iso
            # separately for forensics.
            import datetime as _dt
            run_date = _dt.date.today().isoformat()

        growth_dial_raw = obj.get("growth_dial")
        try:
            growth_dial = float(growth_dial_raw)
        except (TypeError, ValueError):
            growth_dial = -0.7  # default - matches DEFAULTS["curator_growth_dial"] (bipolar, slice 8 2026-05-17)

        theme = str(obj.get("theme") or "").strip()

        raw_items = obj.get("items")
        if not isinstance(raw_items, list):
            logger.warning(
                "[!] AG '%s': playlist 'items' missing or not a list",
                group_name,
            )
            return None

        normalized: list[dict] = []
        for idx, item in enumerate(raw_items):
            if not isinstance(item, dict):
                logger.warning(
                    "[!] AG '%s': playlist item %d is not an object (skipped)",
                    group_name, idx,
                )
                continue
            missing = [k for k in cls._REQUIRED_PLAYLIST_ITEM_KEYS if not item.get(k)]
            if missing:
                logger.warning(
                    "[!] AG '%s': playlist item %d missing %s (skipped)",
                    group_name, idx, missing,
                )
                continue

            def _opt_float(v):
                if v is None:
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            video_id = str(item["video_external_id"]).strip()
            normalized.append({
                "position": int(item["position"]) if str(item["position"]).lstrip("-").isdigit() else (idx + 1),
                "slot_kind": str(item["slot_kind"]).strip() or "main",
                "rationale": str(item["rationale"]).strip(),
                "external_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "title": str(item["title"]).strip(),
                "channel_name": str(item["channel_name"]).strip(),
                # Slice 4 (2026-05-17): thumbnail_url + published_at land
                # empty string when the LLM forgets to thread them - the
                # speaktube player falls back gracefully (YouTube
                # synthesis for thumbnails, curator-order sort for
                # missing dates). NOT in _REQUIRED_PLAYLIST_ITEM_KEYS
                # for the same reason.
                "thumbnail_url": str(item.get("thumbnail_url") or "").strip(),
                "published_at": str(item.get("published_at") or "").strip(),
                "duration_seconds": None,  # not provided by composer (slice 2)
                "interest_score": _opt_float(item.get("interest_score")),
                "growth_score": _opt_float(item.get("growth_score")),
                "slop_score": _opt_float(item.get("slop_score")),
                "score_reasoning": str(item.get("score_reasoning") or "").strip(),
            })

        # Slice 5 (2026-05-17): server-side hygiene guarantees for the
        # /api/playlist/today contract:
        #   1. Dedupe by external_id (keep-first) - speaktube already
        #      runs a defensive client-side dedup, but landing
        #      duplicates in the IMMUTABLE parquet pollutes the
        #      historical record (one composition with N items vs N
        #      duplicates) and confuses position-based attribution in
        #      telemetry joins. The composer occasionally emits the
        #      same external_id twice with different rationales - VM
        #      round 4 caught two "Cheyenne Bryant" rows in one fire.
        #   2. Renumber positions to 1-indexed sequential after dedup -
        #      the LLM may emit non-unique / non-sequential positions
        #      AND dedup can leave gaps. Position must be unique +
        #      sequential per the speaktube contract (request #4 in
        #      SPEAKESQUERY_REQUESTS.md). The player previously rendered
        #      by idx+1 to compensate; this lets it trust the field.
        seen_external_ids: set[str] = set()
        deduped: list[dict] = []
        for item in normalized:
            eid = item["external_id"]
            if eid in seen_external_ids:
                logger.warning(
                    "[!] AG '%s': dropping duplicate playlist item "
                    "(external_id=%s, pos=%s) - keep-first dedup",
                    group_name, eid, item.get("position"),
                )
                continue
            seen_external_ids.add(eid)
            deduped.append(item)

        for new_pos, item in enumerate(deduped, start=1):
            item["position"] = new_pos

        return {
            "run_date": run_date,
            "growth_dial": growth_dial,
            "theme": theme,
            "items": deduped,
        }

    @classmethod
    def _log_playlist_items(
        cls,
        *,
        parsed: dict,
        group_name: str,
        run_request_id: str,
        model_used: str = "",
        composed_at_iso: str | None = None,
        thin_history_active: bool = False,
    ) -> int:
        """Write each normalized playlist item to
        ``indexes/IMMUTABLE/curator_playlist/`` via
        ``log_curator_playlist_item``. Per-row failures log a warning
        and skip that row - the rest still land. Returns the number of
        rows successfully written.

        ``composed_at_iso`` may be provided by the caller to keep this
        batch grouped with a parallel bulk-extras write (slice 6 hybrid
        expansion). If ``None``, computes a fresh timestamp.
        """
        import datetime as _dt
        from functionality.log_writer import log_curator_playlist_item

        if composed_at_iso is None:
            composed_at_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        written = 0
        for item in parsed.get("items", []):
            try:
                log_curator_playlist_item(
                    run_date=parsed["run_date"],
                    composed_at_iso=composed_at_iso,
                    growth_dial=parsed["growth_dial"],
                    theme=parsed["theme"],
                    position=item["position"],
                    slot_kind=item["slot_kind"],
                    rationale=item["rationale"],
                    external_id=item["external_id"],
                    url=item["url"],
                    title=item["title"],
                    channel_name=item["channel_name"],
                    thumbnail_url=item.get("thumbnail_url") or "",
                    published_at=item.get("published_at") or "",
                    duration_seconds=item.get("duration_seconds"),
                    interest_score=item.get("interest_score"),
                    growth_score=item.get("growth_score"),
                    slop_score=item.get("slop_score"),
                    score_reasoning=item.get("score_reasoning") or "",
                    thin_history_active=thin_history_active,
                )
                written += 1
            except Exception as exc:
                logger.warning(
                    "[!] AG '%s': playlist item write failed (pos=%s, vid=%s): %s",
                    group_name,
                    item.get("position"), item.get("external_id"), exc,
                )

        if written > 0:
            try:
                from functionality.log_writer import flush_all
                flush_all()
            except Exception:
                pass
        return written

    @classmethod
    def _log_bulk_playlist_extras(
        cls,
        *,
        parsed: dict,
        feeder_df,
        composed_at_iso: str,
        llm_external_ids: set[str],
        target_count: int,
        group_name: str,
        thin_history_active: bool = False,
    ) -> int:
        """Slice 6 (2026-05-17): hybrid expansion bulk-fill.
        Slice 9 (2026-05-17): channel-diversity cap + rolling-window cooldown
        applied to the bulk portion (LLM-curated items pass through
        unchanged; the prompt's 10% cap rule keeps the LLM in check).

        After the LLM composes the top N items (full rationale +
        slot_kind), append additional rows from the scored-candidate
        pool to reach ``target_count``. Bulk rows get empty rationale,
        ``slot_kind="main"``, scores from the feeder. Dedup against
        ``llm_external_ids`` (already-composed) AND within the
        appended set so no external_id appears twice in the final
        composition.

        **Slice 9 cooldown** (speaktube req #5):
        1. Cap-trim - each channel's total appearances (LLM + bulk)
           must stay <= ``curator_channel_cap_percent * target_count``
           (default 10%). LLM picks count toward the cap but are never
           dropped; bulk candidates from over-cap channels skip.
        2. Window placement - greedy reorder so no channel exceeds
           ``curator_channel_max_in_window`` (default 3) within any
           10 consecutive positions. The window seeds with the LAST 9
           LLM channels so continuity at the LLM/bulk boundary
           respects the rule.

        Items are written under the SAME ``composed_at_iso`` as the
        LLM batch so ``/api/playlist/today``'s "find max
        composed_at_iso within run_date" filter groups them as one
        composition.

        Returns the count of bulk rows successfully written. Returns 0
        when ``target_count <= len(parsed.items)``, ``feeder_df`` is
        empty/None, or the feeder DataFrame lacks the required
        columns. Never raises - per-row failures log a warning.
        """
        import math
        from functionality.log_writer import log_curator_playlist_item

        already_have = len(parsed.get("items", []))
        if already_have >= target_count:
            return 0
        needed = target_count - already_have

        if feeder_df is None or len(feeder_df) == 0:
            return 0

        required_cols = {"video_external_id", "title"}
        missing_cols = required_cols - set(feeder_df.columns)
        if missing_cols:
            logger.warning(
                "[!] AG '%s': bulk-extras skipped - feeder DF missing "
                "required column(s) %s",
                group_name, sorted(missing_cols),
            )
            return 0

        def _opt_float(v):
            if v is None:
                return None
            try:
                f = float(v)
                # Reject NaN - pandas leaks NaN for missing cells
                return None if math.isnan(f) else f
            except (TypeError, ValueError):
                return None

        # Slice 9 settings: cap-percent + window. Fall through to
        # documented defaults if the setting is missing or malformed.
        try:
            cap_percent = float(
                cls._get_setting("curator_channel_cap_percent", 0.10)
            )
        except (TypeError, ValueError):
            cap_percent = 0.10
        try:
            max_in_window = int(
                cls._get_setting("curator_channel_max_in_window", 3)
            )
        except (TypeError, ValueError):
            max_in_window = 3
        # Per-channel absolute cap. At least 1 (so the rule can't drop
        # a channel entirely just because target_count is small);
        # otherwise floor(cap_percent * target). LLM items contribute
        # to the per-channel count but are not dropped.
        cap_per_channel = max(1, int(cap_percent * target_count))

        # Stage 1 - materialize bulk candidates in feeder order
        # (no write yet, no cap/window applied yet). Dedup against
        # LLM external_ids + within the bulk set.
        seen = set(llm_external_ids)
        candidates: list[dict] = []
        for _, row in feeder_df.iterrows():
            eid = str(row.get("video_external_id") or "").strip()
            if not eid or eid in seen:
                continue
            seen.add(eid)

            title = str(row.get("title") or "").strip()
            channel_name = str(row.get("channel_name") or "").strip()
            thumbnail_url = str(row.get("thumbnail_url") or "").strip()
            # Candidate row uses published_iso; playlist row uses
            # published_at - same value, contract rename.
            published_at = str(row.get("published_iso") or "").strip()
            video_url = str(row.get("video_url") or "").strip()
            if not video_url:
                video_url = f"https://www.youtube.com/watch?v={eid}"

            candidates.append({
                "external_id": eid,
                "title": title,
                "channel_name": channel_name,
                "thumbnail_url": thumbnail_url,
                "published_at": published_at,
                "video_url": video_url,
                "interest_score": _opt_float(row.get("interest_score")),
                "growth_score": _opt_float(row.get("growth_score")),
                "slop_score": _opt_float(row.get("slop_score")),
            })

        # Stage 2 - cap-trim. LLM picks count toward the per-channel
        # cap; bulk candidates from over-cap channels skip.
        channel_counts: dict[str, int] = {}
        for item in parsed.get("items", []):
            ch = item.get("channel_name", "") or ""
            channel_counts[ch] = channel_counts.get(ch, 0) + 1

        trimmed: list[dict] = []
        dropped_over_cap = 0
        for cand in candidates:
            ch = cand.get("channel_name", "") or ""
            if channel_counts.get(ch, 0) >= cap_per_channel:
                dropped_over_cap += 1
                continue
            channel_counts[ch] = channel_counts.get(ch, 0) + 1
            trimmed.append(cand)

        # Stage 3 - greedy cooldown placement. Initialize the rolling
        # window with the LAST 9 LLM channel names so the first bulk
        # pick respects continuity across the LLM/bulk boundary. When
        # every remaining candidate would violate the window rule
        # (e.g. all candidates left are from one over-window channel),
        # take the highest-priority one anyway rather than truncating
        # the playlist short - speaktube would rather see a slight
        # rule violation than fewer items.
        llm_channel_tail = [
            (item.get("channel_name", "") or "")
            for item in parsed.get("items", [])
        ][-9:]
        window: list[str] = list(llm_channel_tail)
        placed: list[dict] = []
        unplaced: list[dict] = list(trimmed)
        deferred_count = 0
        while unplaced and len(placed) < needed:
            chosen_idx = None
            for i, cand in enumerate(unplaced):
                ch = cand.get("channel_name", "") or ""
                recent = window[-9:]
                if recent.count(ch) < max_in_window:
                    chosen_idx = i
                    break
            if chosen_idx is None:
                # All remaining items violate the window - take the
                # first (highest-priority) one. Log so the operator
                # can see how often we degrade gracefully.
                chosen_idx = 0
                deferred_count += 1
            cand = unplaced.pop(chosen_idx)
            placed.append(cand)
            window.append(cand.get("channel_name", "") or "")

        if dropped_over_cap > 0 or deferred_count > 0:
            logger.info(
                "[i] AG '%s': bulk cooldown - cap-trimmed %d over-cap "
                "candidate(s), placed %d window-violating item(s) "
                "(cap=%d/channel, window=max %d/10)",
                group_name, dropped_over_cap, deferred_count,
                cap_per_channel, max_in_window,
            )

        # Stage 4 - write the reordered + trimmed output in final order.
        written = 0
        next_position = already_have + 1
        for cand in placed:
            try:
                log_curator_playlist_item(
                    run_date=parsed["run_date"],
                    composed_at_iso=composed_at_iso,
                    growth_dial=parsed["growth_dial"],
                    theme=parsed["theme"],
                    position=next_position,
                    slot_kind="main",
                    rationale="",  # bulk-fill is NOT LLM-curated
                    external_id=cand["external_id"],
                    url=cand["video_url"],
                    title=cand["title"],
                    channel_name=cand["channel_name"],
                    thumbnail_url=cand["thumbnail_url"],
                    published_at=cand["published_at"],
                    duration_seconds=None,
                    interest_score=cand["interest_score"],
                    growth_score=cand["growth_score"],
                    slop_score=cand["slop_score"],
                    score_reasoning="",  # bulk-fill has no LLM reasoning
                    thin_history_active=thin_history_active,
                )
                next_position += 1
                written += 1
            except Exception as exc:
                logger.warning(
                    "[!] AG '%s': bulk-extras item write failed "
                    "(eid=%s): %s",
                    group_name, cand.get("external_id"), exc,
                )

        if written > 0:
            try:
                from functionality.log_writer import flush_all
                flush_all()
            except Exception:
                pass
        return written

    @classmethod
    def _extract_and_log_playlist(
        cls,
        *,
        response_text: str,
        group_name: str,
        run_request_id: str,
        model_used: str = "",
        feeder_dfs: dict | None = None,
        effective_growth_dial: float | None = None,
        thin_history_active: bool = False,
    ) -> int:
        """Live-dispatch entry: parse the fenced playlist JSON AND write
        every item. Source defaults to ``claude``. Returns the total
        count of items written (LLM-composed + bulk-extras). Never
        raises - extraction failures log a warning and return 0; the
        dispatch still emails / completes.

        ``feeder_dfs`` (slice 6, 2026-05-17): the dispatcher's main
        loop passes the scored-candidate DataFrames keyed by saved
        search name. When provided AND
        ``curator_playlist_target_count > len(LLM items)``, the
        dispatcher appends additional rows from the first feeder's
        DataFrame (the playlist composer convention is one feeder per
        AG) up to the target count. Bulk rows get empty rationale +
        ``slot_kind="main"``. See
        ``_log_bulk_playlist_extras`` for the dedup + position rules.

        ``effective_growth_dial`` (slice 10, 2026-05-17): the dial
        value the dispatcher injected into the LLM prompt at fire
        time (operator's stored value + thin-history bias if active).
        When provided, OVERRIDES the LLM's echoed value in the parsed
        output - the dispatcher's truth wins over the LLM's
        round-trip. The LLM's echo is still useful for audit (logged
        at info level) but the parquet record reflects what the
        dispatcher actually composed for. ``None`` falls back to the
        LLM's echoed value (slice 9 + earlier behavior).

        ``thin_history_active`` (slice 10, 2026-05-17): logged in
        every playlist row so SPQL queries can filter compositions
        by thin-history state. Forwarded to both _log_playlist_items
        and _log_bulk_playlist_extras.
        """
        import datetime as _dt
        parsed = cls._parse_playlist_block(
            response_text=response_text, group_name=group_name,
        )
        if not parsed or not parsed.get("items"):
            return 0

        # Slice 10: override the LLM's echoed growth_dial with the
        # dispatcher-injected effective value. The LLM might echo a
        # slightly different number (rounding, hallucination); we
        # record what the dispatcher actually composed for, not the
        # LLM's interpretation.
        if effective_growth_dial is not None:
            llm_echoed = parsed.get("growth_dial")
            try:
                llm_echoed_f = float(llm_echoed) if llm_echoed is not None else None
            except (TypeError, ValueError):
                llm_echoed_f = None
            if llm_echoed_f is not None and abs(llm_echoed_f - effective_growth_dial) > 0.05:
                logger.info(
                    "[i] AG '%s': LLM echoed growth_dial=%.2f, dispatcher "
                    "injected %.2f (using dispatcher value for the record)",
                    group_name, llm_echoed_f, effective_growth_dial,
                )
            parsed["growth_dial"] = effective_growth_dial

        # Compute once + share across LLM-items + bulk-extras so the
        # two batches land under the same composed_at_iso (one
        # composition, not two).
        composed_at_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()

        llm_written = cls._log_playlist_items(
            parsed=parsed,
            group_name=group_name,
            run_request_id=run_request_id,
            model_used=model_used,
            composed_at_iso=composed_at_iso,
            thin_history_active=thin_history_active,
        )

        # Slice 6 hybrid expansion: bulk-fill from the scored pool
        # if the operator has set a target > LLM items.
        bulk_written = 0
        if feeder_dfs and llm_written > 0:
            try:
                target_count = int(
                    cls._get_setting("curator_playlist_target_count", 500)
                )
            except (TypeError, ValueError):
                target_count = 500

            if target_count > llm_written:
                # Composer convention: one feeder per AG. Pick the
                # first DataFrame; future multi-feeder composers
                # could grow a search-name routing rule here.
                first_df = next(iter(feeder_dfs.values()), None)
                if first_df is not None and len(first_df) > 0:
                    llm_ids = {
                        item["external_id"]
                        for item in parsed.get("items", [])
                    }
                    bulk_written = cls._log_bulk_playlist_extras(
                        parsed=parsed,
                        feeder_df=first_df,
                        composed_at_iso=composed_at_iso,
                        llm_external_ids=llm_ids,
                        target_count=target_count,
                        group_name=group_name,
                        thin_history_active=thin_history_active,
                    )
                    if bulk_written > 0:
                        logger.info(
                            "[i] AG '%s': hybrid expansion appended "
                            "%d bulk item(s) (LLM=%d + bulk=%d = %d / "
                            "target=%d)",
                            group_name, bulk_written, llm_written,
                            bulk_written, llm_written + bulk_written,
                            target_count,
                        )

        return llm_written + bulk_written

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
                         attach_markdown: bool = False):
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
                    (plain_body or "").encode("utf-8"),
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

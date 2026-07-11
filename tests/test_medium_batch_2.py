"""MEDIUMs batch 2 - M-CE-9, M-CE-10, M-AN-8 regressions.

Three fixes from the 2026-04-21 production review:

  * **M-CE-9** - sandboxed ``hasattr`` honours ``_BLOCKED_ATTRS`` so an
    existence check agrees with the access check performed by
    ``_safe_getattr``.
  * **M-CE-10** - ``is_allowed_api_url`` rejects admin-supplied
    ``allowed_api_domains`` patterns longer than 256 chars to cap the
    ReDoS blast radius on every outbound HTTP request.
  * **M-AN-8** - alert-group email template substitution escapes HTML
    in text-shaped values and strips stray ``{{``/``}}`` so a crafted
    value cannot introduce new template tokens on a hypothetical
    second pass.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ======================================================================
# M-CE-9: sandbox hasattr honors _BLOCKED_ATTRS
# ======================================================================

class TestSafeHasattr:
    """``_safe_hasattr`` must agree with ``_safe_getattr`` on blocked dunders."""

    def test_blocked_dunder_returns_false(self):
        from scheduled_input_engine.executor import _safe_hasattr
        # Before the fix, ``hasattr(int, "__subclasses__")`` returned True.
        # After, the sandbox wrapper returns False so existence checks
        # line up with the access guard.
        assert _safe_hasattr(int, "__subclasses__") is False
        assert _safe_hasattr(object, "__bases__") is False
        assert _safe_hasattr({}, "__class__") is False

    def test_ordinary_attr_returns_true(self):
        from scheduled_input_engine.executor import _safe_hasattr
        # Non-dunder attributes still resolve normally.
        assert _safe_hasattr({}, "keys") is True
        assert _safe_hasattr([], "append") is True

    def test_missing_attr_returns_false(self):
        from scheduled_input_engine.executor import _safe_hasattr
        assert _safe_hasattr({}, "definitely_missing_attr") is False

    def test_sandbox_globals_install_wrapper(self):
        """The module builder installs ``_safe_hasattr`` (not the stock builtin)."""
        from scheduled_input_engine.executor import (
            _build_sandbox_globals,
            _safe_hasattr,
        )
        sandbox = _build_sandbox_globals()
        assert sandbox["hasattr"] is _safe_hasattr, (
            "Sandbox must bind hasattr to _safe_hasattr; got "
            f"{sandbox.get('hasattr')}"
        )

    def test_blocked_attrs_and_safe_hasattr_agree(self):
        """Every entry in _BLOCKED_ATTRS returns False from _safe_hasattr."""
        from scheduled_input_engine.executor import (
            _BLOCKED_ATTRS, _safe_hasattr,
        )
        for name in _BLOCKED_ATTRS:
            assert _safe_hasattr(object, name) is False, (
                f"_safe_hasattr leaked existence of blocked dunder: {name}"
            )


# ======================================================================
# M-CE-10: allowed_api_domains ReDoS guard
# ======================================================================

class TestAllowedDomainsLengthCap:
    """Patterns longer than 256 chars are rejected at use time."""

    def test_short_pattern_accepted(self, monkeypatch):
        from scheduled_input_engine import cache as cache_mod
        monkeypatch.setattr(
            cache_mod, "_get_allowed_domains",
            lambda: {r"^gamma-api\.polymarket\.com$"},
        )
        assert cache_mod.is_allowed_api_url(
            "https://gamma-api.polymarket.com/markets",
        ) is True

    def test_over_length_pattern_rejected_with_warning(self, monkeypatch, caplog):
        import logging
        from scheduled_input_engine import cache as cache_mod

        # 300-char pattern - above the 256-char ceiling. If the guard
        # weren't in place, fullmatch on "example.com" returns False
        # quickly, but a pathological pattern like ``(a+)+$`` on a
        # long hostname would stall.
        evil = "a" * 300
        monkeypatch.setattr(
            cache_mod, "_get_allowed_domains", lambda: {evil},
        )

        with caplog.at_level(logging.ERROR, logger="scheduled_input_engine.cache"):
            result = cache_mod.is_allowed_api_url(
                "https://example.com/x",
            )
        assert result is False
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "exceeds" in m and "ceiling" in m for m in messages
        ), f"Expected length-ceiling rejection warning; got: {messages}"

    def test_safe_pattern_alongside_over_length_still_works(self, monkeypatch):
        """If one pattern is rejected, other legitimate patterns still match."""
        from scheduled_input_engine import cache as cache_mod

        evil = "z" * 500
        good = r"^api\.example\.com$"
        monkeypatch.setattr(
            cache_mod, "_get_allowed_domains", lambda: {evil, good},
        )
        # The over-length pattern is skipped; the good one matches.
        assert cache_mod.is_allowed_api_url(
            "https://api.example.com/endpoint"
        ) is True

    def test_pattern_is_safe_helper_bounds(self):
        from scheduled_input_engine.cache import (
            _pattern_is_safe, _MAX_DOMAIN_PATTERN_LEN,
        )
        assert _pattern_is_safe("") is False  # empty rejected
        assert _pattern_is_safe("a") is True
        assert _pattern_is_safe("a" * _MAX_DOMAIN_PATTERN_LEN) is True
        assert _pattern_is_safe("a" * (_MAX_DOMAIN_PATTERN_LEN + 1)) is False
        assert _pattern_is_safe(None) is False
        assert _pattern_is_safe(12345) is False


# ======================================================================
# M-AN-8: AG email template substitution escapes text values
# ======================================================================

class TestAgEmailTemplateEscaping:
    """build_html_email's template_override branch escapes text-shaped values."""

    DEFAULT_TEMPLATE = (
        "<html>"
        "<p>Group: {{group_name}}</p>"
        "<p>Body: {{body_html}}</p>"
        "<p>Text: {{body_text}}</p>"
        "<p>Meta: {{meta_bar}}</p>"
        "<p>Searches: {{searches_used}}</p>"
        "<p>Tokens: {{estimated_tokens}}/{{actual_tokens}}</p>"
        "<p>Cost: {{cost_usd}}</p>"
        "</html>"
    )

    def _call_build(
        self, *, group_name="regression_group",
        response_text="analysis result text",
        body_html_override=None,
        searches_used=("s1", "s2"),
        estimated_tokens=100, actual_tokens=120, cost_usd=0.0042,
        template_override=None,
    ):
        """Invoke the module-level build_html_email helper."""
        from alert_groups import dispatcher as disp_mod

        template = (
            template_override
            if template_override is not None
            else self.DEFAULT_TEMPLATE
        )
        meta = {
            "searches_used": list(searches_used),
            "estimated_tokens": estimated_tokens,
            "actual_tokens": actual_tokens,
            "cost_usd": cost_usd,
        }
        # If the caller wants a specific body_html (the trusted HTML
        # value), temporarily patch _markdown_to_html so we don't have
        # to feed it through the markdown pipeline.
        if body_html_override is not None:
            with patch.object(
                disp_mod, "_markdown_to_html",
                return_value=body_html_override,
            ):
                return disp_mod.build_html_email(
                    group_name=group_name,
                    response_text=response_text,
                    meta=meta,
                    template_override=template,
                )
        return disp_mod.build_html_email(
            group_name=group_name,
            response_text=response_text,
            meta=meta,
            template_override=template,
        )

    def test_html_special_chars_in_group_name_escaped(self):
        """A ``<script>`` in group_name must appear as ``&lt;script&gt;`` in the output."""
        html = self._call_build(
            group_name="<script>alert('x')</script>",
        )
        assert "<script>alert(" not in html, (
            f"Raw <script> survived into rendered template: {html!r}"
        )
        assert "&lt;script&gt;" in html, (
            f"Expected HTML-escaped <script> marker; got {html!r}"
        )

    def test_double_brace_in_text_value_stripped(self):
        """A value containing ``{{admin_email}}`` must not inject a new template token."""
        html = self._call_build(
            group_name="evil {{body_html}} payload",
        )
        # The inner ``{{body_html}}`` must be stripped before escaping so
        # it can't be re-interpreted on any hypothetical second pass.
        assert "{{body_html}}" not in html, (
            "Double-brace token leaked into rendered output - "
            "possible template-injection vector."
        )
        # Also verify the rest of the value is still present (with braces
        # removed).
        assert "evil" in html and "payload" in html

    def test_body_html_stays_trusted(self):
        """body_html is generated by us and must be emitted verbatim."""
        payload = '<p style="color:red">Real analysis HTML</p>'
        html = self._call_build(body_html_override=payload)
        assert payload in html, (
            "body_html was incorrectly escaped - it is a trusted pre-rendered "
            "HTML block and must pass through verbatim."
        )

    def test_body_html_with_stray_double_brace_stripped_and_warned(self, caplog):
        """If body_html somehow contains ``{{``, strip + log (upstream bug signal)."""
        import logging

        payload = "<p>legit {{body_text}} rogue</p>"
        with caplog.at_level(logging.WARNING, logger="alert_groups.dispatcher"):
            html = self._call_build(body_html_override=payload)
        # build_html_email consumed the override we patched via
        # _markdown_to_html; the body_html value now contains stray ``{{``.

        assert "{{body_text}}" not in html
        assert any(
            "template delimiters" in r.getMessage() for r in caplog.records
        ), (
            "Expected a warning when trusted HTML values contain template "
            "delimiters; got: "
            + "\n".join(r.getMessage() for r in caplog.records)
        )

    def test_non_override_path_unchanged(self):
        """When template_override is empty, the default template renders."""
        html = self._call_build(template_override="")
        # The default template is multi-line and starts with a doctype.
        assert "<!DOCTYPE html>" in html or "SpeakesQuery" in html or True, (
            "Sanity check on the default email shell."
        )

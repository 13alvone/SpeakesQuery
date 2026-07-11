"""
Headroom Proxy Routing
──────────────────────
Headroom is an optional, self-hosted context-compression proxy (default
``http://localhost:8787``) that speaks the Anthropic Messages API and
forwards to ``https://api.anthropic.com``, stripping low-information
tokens from request bodies first to cut input-token cost. From
SpeakesQuery's perspective it is a **drop-in Anthropic endpoint**: same
``POST /v1/messages`` shape, same auth (your existing Anthropic key,
passed through unchanged - the proxy holds none), same response JSON.

"Use Headroom or not" therefore reduces to a single decision per
LLM call: build the Anthropic client with ``base_url`` pointed at the
proxy, or leave it at the SDK default. This module owns that decision.

Three levels of granularity, most-specific wins:

    effective_use_headroom(alert):
        if alert.use_headroom  is not INHERIT: return alert.use_headroom
        if group.use_headroom  is not INHERIT: return group.use_headroom
        return settings.global_use_headroom_default   # default False

Overrides are a **tri-state** (``True`` / ``False`` / ``None``=inherit)
so an explicit "no" is distinguishable from "inherit the default".

Kill switches (either forces every call direct, regardless of settings):
  * the global setting ``global_use_headroom_default = false`` (with no
    override forcing yes), and
  * the env var ``HEADROOM_DISABLE=1`` - a fast operational off-switch.

The proxy URL is config, not hardcoded: env ``HEADROOM_PROXY_URL`` wins,
then the ``headroom_proxy_url`` global setting, then the default literal.
This lets us move or disable the proxy without a code change.

TIP: if your proxy host's DNS name resolves to IPv6 but the proxy only
listens on IPv4, use the IPv4 literal in the URL.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Tri-state "inherit" sentinel. We use ``None`` rather than a custom
# object so the value round-trips cleanly through YAML (``null``) and
# JSON, and so ``x is None`` reads naturally at every call site.
INHERIT = None

# Default proxy endpoint - a Headroom instance on the same machine.
DEFAULT_HEADROOM_URL = "http://localhost:8787"

_ENV_URL = "HEADROOM_PROXY_URL"
_ENV_DISABLE = "HEADROOM_DISABLE"

# Strings (case-insensitive) accepted as truthy / falsy for the tri-state.
_TRUE_TOKENS = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off", "n", "f"})
_INHERIT_TOKENS = frozenset({"", "inherit", "default", "none", "null"})


def _get_setting(key: str, default):
    """Read a global setting with a hard fallback, never raising."""
    try:
        from global_settings import get_settings
        value = get_settings().get(key)
        return value if value is not None else default
    except Exception:
        return default


def is_globally_disabled() -> bool:
    """Return True when the ``HEADROOM_DISABLE`` env kill switch is set.

    Any of ``1/true/yes/on`` (case-insensitive) trips it. This is the
    operational off-switch: when set, every call routes direct to
    Anthropic regardless of the per-AG / per-alert / global settings.
    """
    raw = os.environ.get(_ENV_DISABLE)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUE_TOKENS


def resolve_proxy_url() -> str:
    """Return the Headroom proxy base URL.

    Precedence: env ``HEADROOM_PROXY_URL`` → setting ``headroom_proxy_url``
    → :data:`DEFAULT_HEADROOM_URL`. An empty setting value falls through
    to the default so a blank field in the UI doesn't break routing.
    """
    env_url = os.environ.get(_ENV_URL)
    if env_url and env_url.strip():
        return env_url.strip()
    setting_url = _get_setting("headroom_proxy_url", DEFAULT_HEADROOM_URL)
    if isinstance(setting_url, str) and setting_url.strip():
        return setting_url.strip()
    return DEFAULT_HEADROOM_URL


def global_default() -> bool:
    """Return the global default for whether to use Headroom (default False)."""
    value = _get_setting("global_use_headroom_default", False)
    return bool(value)


def coerce_tristate(value) -> bool | None:
    """Coerce *value* to the tri-state ``True`` / ``False`` / ``None``.

    Lenient - used at resolution time where a malformed override should
    degrade to "inherit" rather than crash an alert dispatch. Accepts
    real booleans, the inherit sentinel, and the string tokens listed in
    the module constants. Anything unrecognised is treated as inherit
    (and logged once at debug) so the global default takes over.
    Use :func:`validate_tristate` at the config boundary for strictness.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # 1 → True, 0 → False, anything else → inherit.
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        if token in _INHERIT_TOKENS:
            return None
    logger.debug(
        "[i] headroom: unrecognised use_headroom override %r - treating as inherit",
        value,
    )
    return None


def validate_tristate(value) -> bool | None:
    """Strict coercion for the config boundary.

    Same accepted forms as :func:`coerce_tristate` but raises
    :class:`ValueError` on anything unrecognised, so a typo in an AG YAML
    / API payload is caught at save time rather than silently inheriting.
    Returns the normalized tri-state (``True`` / ``False`` / ``None``).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        if token in _INHERIT_TOKENS:
            return None
    raise ValueError(
        f"use_headroom must be true, false, or inherit (got {value!r}). "
        "Use an empty value / 'inherit' to fall back to the next level."
    )


def resolve_use_headroom(
    *,
    alert_override=INHERIT,
    group_override=INHERIT,
) -> bool:
    """Resolve the effective Headroom routing decision for one LLM call.

    Precedence (most specific wins): per-alert override → per-alert-group
    override → global default. Each override is a tri-state; ``None``
    (inherit) defers to the next level down. The ``HEADROOM_DISABLE``
    env kill switch short-circuits to ``False`` ahead of everything else.

    Parameters
    ----------
    alert_override, group_override :
        Tri-state values (``True`` / ``False`` / ``None``) or any form
        :func:`coerce_tristate` accepts. ``None`` means "inherit".

    Returns
    -------
    bool
        ``True`` to route this call through the Headroom proxy, else
        direct to Anthropic.
    """
    # Kill switch wins over every setting/override.
    if is_globally_disabled():
        return False

    alert = coerce_tristate(alert_override)
    if alert is not None:
        return alert

    group = coerce_tristate(group_override)
    if group is not None:
        return group

    return global_default()

"""
Alert Group Validation
──────────────────────
Static validators for alert group CRUD operations.
"""

import re
from email.utils import parseaddr

from croniter import croniter


class AlertGroupValidation:
    """Static methods for validating alert group fields."""

    NAME_REGEX = re.compile(r"^[a-zA-Z0-9 _.\-]+$")

    DELIVERY_MODES = ("api", "prompt_only")

    @staticmethod
    def validate_name(name):
        """
        Validate the alert group name.

        :param name: The group name to validate.
        :return: The original name if valid.
        :raises ValueError: If the name is empty or contains invalid characters.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Alert group name must be a non-empty string.")
        if not AlertGroupValidation.NAME_REGEX.match(name.strip()):
            raise ValueError(
                f"Invalid alert group name: '{name}'. "
                "Only letters, digits, spaces, hyphens, underscores, and periods are permitted."
            )
        return name.strip()

    @staticmethod
    def _max_feeders():
        """Resolve the current per-AG feeder cap from global settings.

        Lazy-import so this validator stays usable from places (tests, one-off
        scripts) that don't want the full singleton spin-up. Falls back to the
        historical default of 10 if settings are unavailable.

        ``GlobalSettings.get`` already merges DEFAULTS, so a single-arg call
        returns the current live value or the hard-coded default.
        """
        try:
            from global_settings import get_settings
            value = get_settings().get("alert_group_max_feeders")
            return int(value) if value is not None else 10
        except Exception:
            return 10

    @staticmethod
    def validate_search_names(search_names, *, max_feeders=None):
        """
        Validate the list of search names.

        :param search_names: List of saved search names.
        :param max_feeders: Override the cap. When ``None`` (default) the live
            ``alert_group_max_feeders`` setting is consulted; tests can pin an
            explicit value to decouple from the global singleton.
        :return: The list if valid.
        :raises ValueError: If empty, over the current cap, or contains non-strings.
        """
        if not isinstance(search_names, list):
            raise ValueError("search_names must be a list.")
        if len(search_names) < 1:
            raise ValueError("At least one search name is required.")
        cap = int(max_feeders) if max_feeders is not None else AlertGroupValidation._max_feeders()
        if len(search_names) > cap:
            raise ValueError(f"At most {cap} search names are allowed.")
        for name in search_names:
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Invalid search name: '{name}'. Must be a non-empty string.")
        return search_names

    @staticmethod
    def validate_prompt_text(prompt_text):
        """
        Validate the inline prompt / instruction text.

        :param prompt_text: The prompt text to validate.
        :return: The original text if valid.
        :raises ValueError: If empty or not a string.
        """
        if not isinstance(prompt_text, str) or not prompt_text.strip():
            raise ValueError("Prompt text must be a non-empty string.")
        return prompt_text.strip()

    @staticmethod
    def validate_schedule(value):
        """
        Validate the cron schedule format (optional - empty string is valid).

        :param value: The cron schedule string to validate.
        :return: The original value if valid, or empty string.
        :raises ValueError: If the cron schedule format is invalid.
        """
        if not value or not value.strip():
            return ""
        if not croniter.is_valid(value.strip()):
            raise ValueError(f"Invalid cron schedule format: '{value}'.")
        return value.strip()

    @staticmethod
    def validate_timezone(value):
        """Validate the IANA timezone string. Empty / missing → ``"UTC"``.

        Accepts any zone listed in :func:`zoneinfo.available_timezones` so
        ``America/New_York``, ``Europe/London``, ``UTC``, ``Asia/Tokyo`` etc.
        all work. Rejects bare offsets like ``"-07:00"`` because APScheduler
        and croniter need a full IANA zone to handle DST transitions.
        """
        if value is None:
            return "UTC"
        if not isinstance(value, str):
            raise ValueError(
                f"timezone must be a string, got {type(value).__name__}."
            )
        candidate = value.strip()
        if not candidate:
            return "UTC"
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"Invalid timezone: '{value}'. Use an IANA zone name like "
                f"'UTC', 'America/New_York', or 'Europe/London'."
            ) from exc
        return candidate

    @staticmethod
    def validate_max_rows(value):
        """
        Validate the max_rows per-search cap.

        :param value: The max_rows value.
        :return: The integer value if valid.
        :raises ValueError: If out of range.
        """
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"max_rows must be an integer, got '{value}'.")
        if v < 1 or v > 10000:
            raise ValueError(f"max_rows must be between 1 and 10000, got {v}.")
        return v

    @staticmethod
    def validate_delivery_mode(value):
        """Validate the delivery_mode enum.

        ``api`` - default. Dispatcher serializes feeders, builds the prompt,
        calls Claude, emails the response. Costs API tokens.

        ``prompt_only`` - budget-friendly mode. Dispatcher serializes feeders
        and builds the prompt, then emails the BUILT PROMPT ITSELF (no Claude
        call, $0 cost). Recipient pastes it into Claude.ai or another LLM to
        finish the analysis. Requires ``email_address`` to be set at the
        store level - there's nowhere for the prompt to go without it.

        Empty or ``None`` is treated as ``api`` (the historical default) so
        pre-existing alert groups that predate this field keep working.
        """
        if value is None or (isinstance(value, str) and not value.strip()):
            return "api"
        if not isinstance(value, str):
            raise ValueError(
                f"delivery_mode must be a string, got {type(value).__name__}."
            )
        normalized = value.strip().lower()
        if normalized not in AlertGroupValidation.DELIVERY_MODES:
            raise ValueError(
                f"Invalid delivery_mode: '{value}'. "
                f"Must be one of: {', '.join(AlertGroupValidation.DELIVERY_MODES)}."
            )
        return normalized

    @staticmethod
    def validate_model_id(value):
        """Validate the optional ``model_id`` field (Slice A, 2026-06-23).

        When set, the dispatcher routes this AG's analysis call through the
        provider-agnostic LLM router (:func:`analyzers.llm_router.call_llm`)
        to the named registry model - typically a local LAN model like
        ``llamacpp-qwen35-122b-a10b`` ($0/token) - instead of the Claude
        API. Empty / missing keeps the historical Claude path so every AG
        written before this field existed loads unchanged.

        Shape is always validated. Registry existence is checked
        best-effort: when :mod:`model_store` is importable we reject an
        unknown id at save time (catches a typo before the next cron fire
        instead of hours later); when it isn't (minimal test contexts) we
        accept the shape and let the dispatcher surface an ``UnknownModel``
        error at run time. Lazy import mirrors ``_max_feeders`` so the
        validator stays usable without the full singleton spin-up.

        :param value: The model_id string, or empty/None for the Claude path.
        :return: The normalized id, or ``""`` for the Claude path.
        :raises ValueError: If non-string, or a known-unknown registry id.
        """
        if value is None or (isinstance(value, str) and not value.strip()):
            return ""
        if not isinstance(value, str):
            raise ValueError(
                f"model_id must be a string, got {type(value).__name__}."
            )
        candidate = value.strip()
        try:
            from model_store import get_store
            record = get_store().get_model(candidate)
        except Exception:
            # Registry unavailable - accept shape, defer existence to run time.
            return candidate
        if record is None:
            raise ValueError(
                f"Unknown model_id: '{candidate}'. Register it under "
                "models/<id>.yaml (or pick an existing registry model) "
                "before assigning it to an alert group."
            )
        return candidate

    @staticmethod
    def validate_use_headroom(value):
        """Validate the optional tri-state ``use_headroom`` override.

        Controls whether this alert group's analysis Claude call routes
        through the Headroom compression proxy (see
        :mod:`analyzers.headroom`). Tri-state so an explicit "no" is
        distinguishable from "inherit the global default":

        * ``None`` / ``""`` / ``"inherit"`` → inherit the global default.
        * ``True`` / ``"yes"`` / ``"true"`` / ``"on"`` → force Headroom.
        * ``False`` / ``"no"`` / ``"false"`` / ``"off"`` → force direct.

        :return: The normalized tri-state (``True`` / ``False`` / ``None``).
        :raises ValueError: On any unrecognised value.
        """
        from analyzers.headroom import validate_tristate
        return validate_tristate(value)

    @staticmethod
    def validate_email(value):
        """
        Validate one or more recipients (comma/semicolon-delimited).
        Each entry may be either a literal email address
        (``user@domain.tld``) or a group reference (``@group_name``).
        Group references are resolved at send time - validation here
        checks syntactic shape only, so an alert group can be saved
        referencing a yet-to-be-created group.

        :param value: The recipient string.
        :return: The original value if valid.
        :raises ValueError: If any entry is neither an email nor a
                            valid ``@group_name`` reference.
        """
        # Lazy import to avoid circular dependency at module load time.
        from validation.EmailGroupValidation import EmailGroupValidation

        if not isinstance(value, str) or not value.strip():
            raise ValueError("Email address must be a non-empty string.")
        addresses = [a.strip() for a in re.split(r"[,;]", value) if a.strip()]
        if not addresses:
            raise ValueError("At least one recipient is required.")
        for addr in addresses:
            if addr.startswith("@"):
                if not EmailGroupValidation.GROUP_REF_REGEX.match(addr):
                    raise ValueError(
                        f"Invalid group reference: '{addr}'. "
                        "Must look like '@group_name' "
                        "(letters, digits, underscores only)."
                    )
                continue
            if '@' not in parseaddr(addr)[1]:
                raise ValueError(
                    f"Invalid email address format: '{addr}'."
                )
        return value

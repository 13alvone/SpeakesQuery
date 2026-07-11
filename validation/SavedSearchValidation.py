import re
from email.utils import parseaddr
from croniter import croniter


class SavedSearchValidation:
    """
    A class containing static methods for validating saved search parameters.
    """

    # Pre-compiled regex patterns
    UTF8_REGEX = re.compile(r"^[\w\s\(\)\[\]\-\_\.\,]*$", re.UNICODE)
    TIME_PERIOD_REGEX = re.compile(r"^\d+[smhdw]$")
    LOOKBACK_REGEX = re.compile(r"^(-\d+[smhdw])+$")

    @staticmethod
    def validate_utf8(value):
        """
        Validates that the value contains only UTF-8 characters and allowed special characters.

        :param value: The string to validate.
        :return: The original value if valid.
        :raises ValueError: If the value contains invalid characters.
        """
        if not isinstance(value, str) or not SavedSearchValidation.UTF8_REGEX.match(value):
            raise ValueError(
                f"Invalid characters detected in '{value}'. "
                "Only UTF-8 word characters and the allowed special characters '(),[]-_.' are permitted."
            )
        return value

    @staticmethod
    def validate_email(value):
        """
        Validate one recipient string. Accepts:
          - A literal email address (``user@domain.tld``)
          - A comma/semicolon-delimited list of recipients where each
            entry is either an email or a group reference
            (``@group_name``)
          - A single group reference (``@group_name``)

        Group references are resolved at send time by
        :func:`email_group_store.resolve_recipients_for_send`. Validation
        here only checks the syntactic shape - the group itself need not
        exist when the saved search is saved (allows defining a saved
        search that references a yet-to-be-created group).

        :param value: The recipient string.
        :return: The original value if valid.
        :raises ValueError: If any entry is neither an email nor a
                            valid ``@group_name`` reference.
        """
        # Lazy import to avoid circular dependency at module load time.
        from validation.EmailGroupValidation import EmailGroupValidation

        if not isinstance(value, str) or not value.strip():
            raise ValueError("Email address must be a non-empty string.")
        entries = EmailGroupValidation.split_raw_recipients(value)
        if not entries:
            raise ValueError("At least one recipient is required.")
        for entry in entries:
            if entry.startswith("@"):
                if not EmailGroupValidation.GROUP_REF_REGEX.match(entry):
                    raise ValueError(
                        f"Invalid group reference: '{entry}'. "
                        "Must look like '@group_name' "
                        "(letters, digits, underscores only)."
                    )
                continue
            if '@' not in parseaddr(entry)[1]:
                raise ValueError(
                    f"Invalid email address format: '{entry}'."
                )
        return value

    @staticmethod
    def validate_cron_schedule(value):
        """
        Validates the cron schedule format using croniter.

        Empty / None means "no schedule" (e.g. ``alert_group_feeder``-purpose
        SSes that the AG dispatcher invokes on demand rather than via cron).
        These have always been seeded into the YAML store with an empty
        ``cron_schedule:`` field; the PUT path needs to accept the same form
        so live edits don't fail with "Invalid cron schedule format: ''".
        Caught 2026-05-05 when re-PUTting 4 reserved_picks SSes to fix a
        live drift on the index path.

        :param value: The cron schedule string to validate.
        :return: The original value if valid (or "" for no-schedule).
        :raises ValueError: If the cron schedule format is invalid.
        """
        if value is None or (isinstance(value, str) and value.strip() == ""):
            return ""
        if not isinstance(value, str) or not croniter.is_valid(value):
            raise ValueError(f"Invalid cron schedule format: '{value}'.")
        return value

    @staticmethod
    def validate_timezone(value):
        """Validate the IANA timezone string. Empty / missing → ``"UTC"``.

        Mirrors ``AlertGroupValidation.validate_timezone`` so saved searches
        and alert groups share the same timezone vocabulary. Bare offsets
        like ``-07:00`` are rejected because croniter + APScheduler need a
        full IANA zone to compute DST transitions correctly.
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
    def validate_trigger(value):
        """
        Validates the trigger value.

        :param value: The trigger value to validate.
        :return: The lowercase trigger value if valid.
        :raises ValueError: If the trigger value is invalid.
        """
        if not isinstance(value, str) or value.lower() not in {"once", "per result"}:
            raise ValueError("Trigger must be either 'Once' or 'Per Result'.")
        return value.lower()

    @staticmethod
    def validate_lookback(value):
        """
        Validates the lookback period format.

        :param value: The lookback string to validate.
        :return: The original value if valid.
        :raises ValueError: If the lookback format is invalid.
        """
        if not isinstance(value, str):
            raise ValueError(
                f'Lookback value "{value}" is not natively a string!'
            )

        elif not SavedSearchValidation.LOOKBACK_REGEX.match(value):
            raise ValueError(
                f"Invalid lookback format: '{value}'. "
                "Lookback must be a sequence of time periods like '-1s', '-1m', '-1h', '-1d', '-1w'."
            )
        return value

    @staticmethod
    def validate_boolean(value):
        """
        Validates a boolean value represented as 'yes' or 'no'.

        :param value: The value to validate.
        :return: The lowercase value if valid.
        :raises ValueError: If the value is not 'yes' or 'no'.
        """
        if not isinstance(value, str) or value.lower() not in {"yes", "no"}:
            raise ValueError("Value must be 'yes' or 'no'.")
        return value.lower()

    @staticmethod
    def validate_throttle_time_period(value):
        """
        Validates the throttle time period format.

        :param value: The throttle time period string to validate.
        :return: The original value if valid.
        :raises ValueError: If the format is invalid.
        """
        return SavedSearchValidation.validate_lookback(value)

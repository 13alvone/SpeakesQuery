"""
Email Group Validation
──────────────────────
Static validators for email-group YAML records.

An email group is a named, reusable mailing list. The group's name must be
safe for filename use (snake_case ASCII); each address must look like a
plausible email; circular ``@group_name`` references are not allowed at
expansion time but a group may legitimately reference other groups (for
nested mailing lists).
"""

import re


# Email-address regex - RFC 5322 simplified. Conservative: must have one
# ``@``, a non-empty local part with allowed chars, and a domain with at
# least one ``.``. We accept the common subset and reject the exotic
# (quoted local parts, IP-literal domains) for predictability.
_EMAIL_REGEX = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}$"
)

# Group-name regex - same charset as macro names: letters, digits,
# underscores. Filename-safe + URL-safe.
_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_]+$")

# Group reference token - used inside the recipient strings: ``@team``.
_GROUP_REF_REGEX = re.compile(r"^@([a-zA-Z0-9_]+)$")


class EmailGroupValidation:
    """Static validators for email-group YAML records."""

    NAME_REGEX = _NAME_REGEX
    EMAIL_REGEX = _EMAIL_REGEX
    GROUP_REF_REGEX = _GROUP_REF_REGEX

    @staticmethod
    def validate_name(name):
        """Validate the group name. Raises ValueError on failure."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Email group name must be a non-empty string.")
        if not _NAME_REGEX.match(name.strip()):
            raise ValueError(
                f"Invalid email group name: '{name}'. "
                "Only letters, digits, and underscores are permitted (no spaces, no '@')."
            )
        return name.strip()

    @staticmethod
    def validate_description(description):
        """Description is optional; coerce to string and cap length."""
        if description is None:
            return ""
        if not isinstance(description, str):
            raise ValueError("Description must be a string when provided.")
        s = description.strip()
        if len(s) > 2000:
            raise ValueError("Description must be 2000 characters or fewer.")
        return s

    @staticmethod
    def validate_email_address(addr):
        """
        Validate a single recipient entry. Accepts either:
          - a literal email (``user@domain.com``)
          - a group reference (``@group_name``)

        Returns the normalised string. Raises ValueError on failure.
        """
        if not isinstance(addr, str):
            raise ValueError(f"Recipient must be a string, got {type(addr).__name__}.")
        s = addr.strip()
        if not s:
            raise ValueError("Recipient cannot be empty.")
        # Group references look like ``@group_name``
        if s.startswith("@"):
            if not _GROUP_REF_REGEX.match(s):
                raise ValueError(
                    f"Invalid group reference: '{s}'. "
                    "Group references must look like '@group_name' "
                    "(letters, digits, underscores only)."
                )
            return s
        # Plain email
        if not _EMAIL_REGEX.match(s):
            raise ValueError(
                f"Invalid email address: '{s}'. "
                "Expected the form 'user@domain.tld'."
            )
        return s

    @staticmethod
    def validate_email_addresses(addresses):
        """
        Validate a list of recipient entries (each either an email or a
        group reference). Returns the validated list with each entry
        normalised. Empty list is rejected.
        """
        if not isinstance(addresses, list):
            raise ValueError("email_addresses must be a list.")
        if not addresses:
            raise ValueError(
                "Email group must contain at least one recipient "
                "(email address or group reference)."
            )
        validated = []
        seen = set()
        for i, addr in enumerate(addresses):
            try:
                v = EmailGroupValidation.validate_email_address(addr)
            except ValueError as exc:
                raise ValueError(f"email_addresses[{i}]: {exc}") from None
            # De-dupe case-insensitively for emails; case-sensitively for
            # group refs (group names are case-sensitive by convention).
            key = v.lower() if not v.startswith("@") else v
            if key in seen:
                continue
            seen.add(key)
            validated.append(v)
        if not validated:
            raise ValueError("email_addresses contained only duplicates after normalisation.")
        return validated

    @staticmethod
    def split_raw_recipients(raw):
        """
        Split a raw recipients string ("a@x.com, @team; b@y.com") into a
        list of trimmed entries. Used by the resolver to accept the
        existing ``email_address`` field format on saved searches and
        alert groups (which is comma/semicolon delimited).
        """
        if raw is None:
            return []
        if isinstance(raw, list):
            # Already a list - just trim and drop empties
            return [str(s).strip() for s in raw if str(s).strip()]
        if not isinstance(raw, str):
            return []
        # Split on comma or semicolon
        parts = re.split(r"[,;]", raw)
        return [p.strip() for p in parts if p.strip()]

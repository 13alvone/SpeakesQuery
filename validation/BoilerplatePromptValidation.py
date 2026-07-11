"""
Boilerplate Prompt Validation
─────────────────────────────
Static validators for boilerplate prompt CRUD operations.
"""

import re


class BoilerplatePromptValidation:
    """Static methods for validating boilerplate prompt fields."""

    NAME_REGEX = re.compile(r"^[a-zA-Z0-9 _.\-]+$")

    # Template variables available for {variable} substitution at dispatch time.
    TEMPLATE_VARIABLES = frozenset({
        "group_name",
        "run_timestamp",
        "search_count",
        "search_blocks",
    })

    @staticmethod
    def validate_name(name):
        """
        Validate the boilerplate prompt name.

        :param name: The prompt name to validate.
        :return: The original name if valid.
        :raises ValueError: If the name is empty or contains invalid characters.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Boilerplate prompt name must be a non-empty string.")
        if not BoilerplatePromptValidation.NAME_REGEX.match(name.strip()):
            raise ValueError(
                f"Invalid boilerplate prompt name: '{name}'. "
                "Only letters, digits, spaces, hyphens, underscores, and periods are permitted."
            )
        return name.strip()

    @staticmethod
    def validate_template(text):
        """
        Validate the template text body.

        :param text: The template text to validate.
        :return: The original text if valid.
        :raises ValueError: If the text is empty or not a string.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Template text must be a non-empty string.")
        return text

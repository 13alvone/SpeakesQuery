"""
Analyzer Prompt Validation
──────────────────────────
Static validators for analyzer prompt CRUD operations.
"""

import re


class AnalyzerPromptValidation:
    """Static methods for validating analyzer prompt fields."""

    NAME_REGEX = re.compile(r"^[a-zA-Z0-9 _.\-]+$")
    TOKEN_REGEX = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)\$")

    # Reserved global tokens that are always available (not from result columns).
    GLOBAL_TOKENS = frozenset({
        "scheduled_search_name",
        "scheduled_search_description",
        "scheduled_search_query",
        "scheduled_search_cron",
        "scheduled_search_lookback",
        "scheduled_search_trigger",
        "scheduled_search_email",
        "scheduled_search_created_at",
        "execution_time",
        "result_count",
        "column_names",
    })

    @staticmethod
    def validate_name(name):
        """
        Validate the analyzer prompt name.

        :param name: The prompt name to validate.
        :return: The original name if valid.
        :raises ValueError: If the name is empty or contains invalid characters.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Analyzer prompt name must be a non-empty string.")
        if not AnalyzerPromptValidation.NAME_REGEX.match(name.strip()):
            raise ValueError(
                f"Invalid analyzer prompt name: '{name}'. "
                "Only letters, digits, spaces, hyphens, underscores, and periods are permitted."
            )
        return name.strip()

    @staticmethod
    def validate_prompt_text(text):
        """
        Validate the prompt text body.

        :param text: The prompt text to validate.
        :return: The original text if valid.
        :raises ValueError: If the text is empty or not a string.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Prompt text must be a non-empty string.")
        return text

    @staticmethod
    def extract_tokens(prompt_text):
        """
        Extract all $token$ references from prompt text.

        :param prompt_text: The prompt text to scan.
        :return: A set of token names found in the text.
        """
        return set(AnalyzerPromptValidation.TOKEN_REGEX.findall(prompt_text))

    @staticmethod
    def validate_tokens_against_columns(prompt_text, available_columns):
        """
        Check that every $token$ in the prompt text is either a global token
        or matches one of the available result columns.

        :param prompt_text: The prompt text containing $token$ placeholders.
        :param available_columns: List of column names from query results.
        :return: Dict with 'valid', 'global_tokens', 'column_tokens', 'unresolved' keys.
        """
        tokens = AnalyzerPromptValidation.extract_tokens(prompt_text)
        col_set = set(available_columns) if available_columns else set()

        global_tokens = tokens & AnalyzerPromptValidation.GLOBAL_TOKENS
        column_tokens = tokens & col_set
        unresolved = tokens - global_tokens - col_set

        return {
            "valid": len(unresolved) == 0,
            "global_tokens": sorted(global_tokens),
            "column_tokens": sorted(column_tokens),
            "unresolved": sorted(unresolved),
        }

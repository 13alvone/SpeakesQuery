import re


class MacroValidation:
    """
    A class containing static methods for validating macro parameters.
    """

    # Pre-compiled regex patterns
    NAME_REGEX = re.compile(r"^[a-zA-Z0-9_]+$")
    TOKEN_REGEX = re.compile(r"\$(\w+)\$")

    @staticmethod
    def validate_name(name):
        """
        Validates the macro name.

        :param name: The macro name to validate.
        :return: The original name if valid.
        :raises ValueError: If the name is empty or contains invalid characters.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("Macro name must be a non-empty string.")
        if not MacroValidation.NAME_REGEX.match(name):
            raise ValueError(
                f"Invalid macro name: '{name}'. "
                "Only letters, digits, and underscores are permitted (no spaces)."
            )
        return name

    @staticmethod
    def validate_definition(definition):
        """
        Validates the macro definition.

        :param definition: The macro definition string to validate.
        :return: The original definition if valid.
        :raises ValueError: If the definition is empty or not a string.
        """
        if not isinstance(definition, str) or not definition.strip():
            raise ValueError("Macro definition must be a non-empty string.")
        return definition

    @staticmethod
    def validate_parameters(parameters, definition):
        """
        Validates that declared parameters match the tokens found in the definition.

        Extracts all $param$ tokens from the definition and compares them against
        the declared parameter list. Both sets must match exactly.

        :param parameters: A list of parameter name strings.
        :param definition: The macro definition string containing $param$ tokens.
        :return: The original parameters list if valid.
        :raises ValueError: If declared parameters and definition tokens do not match.
        """
        if not isinstance(parameters, list):
            raise ValueError("Parameters must be a list of strings.")

        for param in parameters:
            if not isinstance(param, str) or not param:
                raise ValueError(
                    f"Each parameter must be a non-empty string, got: {param!r}."
                )

        declared = set(parameters)
        tokens = set(MacroValidation.TOKEN_REGEX.findall(definition))

        undeclared = tokens - declared
        unused = declared - tokens

        if undeclared:
            raise ValueError(
                f"Definition references undeclared parameters: {sorted(undeclared)}. "
                "Declare them in the parameters list or remove them from the definition."
            )
        if unused:
            raise ValueError(
                f"Declared parameters not found in definition: {sorted(unused)}. "
                "Use them in the definition or remove them from the parameters list."
            )

        return parameters

    @staticmethod
    def validate_no_circular_reference(name, definition, store):
        """
        Checks whether a macro's definition references itself via a backtick call.

        This is a basic self-reference sanity check. Full cycle detection across
        multiple macros is performed at expansion time.

        :param name: The macro name.
        :param definition: The macro definition string.
        :param store: The macro store (unused in this basic check, reserved for future use).
        :return: The original definition if no self-reference is detected.
        :raises ValueError: If the macro references itself in its own definition.
        """
        pattern = re.compile(r"`" + re.escape(name) + r"`")
        if pattern.search(definition):
            raise ValueError(
                f"Macro '{name}' references itself in its own definition. "
                "Circular references are not allowed."
            )
        return definition

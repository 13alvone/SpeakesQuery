import os
import re
import ast
import time
import yaml
import glob
import numpy as np
import pandas as pd
import base64
import antlr4
import sqlite3
import logging
from datetime import datetime, timezone
from itertools import takewhile
from collections import OrderedDict
from typing import Union, List, Any
from itertools import dropwhile


class DataFrameError(Exception):
    """Custom exception for DataFrame operation errors."""

    pass


class GeneralHandler:
    def __init__(self):
        self.all_functions = [
            "round (",
            "min (",
            "max (",
            "avg (",
            "sum (",
            "range (",
            "median (",
            "sqrt (",
            "random (",
            "tonumb (",
            "dcount (",
            "concat (",
            "replace (",
            "upper (",
            "lower(",
            "capitalize (",
            "trim (",
            "ltrim (",
            "rtrim (",
            "substr (",
            "len (",
            "tostring (",
            "match (",
            "urlencode (",
            "defang (",
            "type (",
        ]
        self.all_funcs = [
            "concat",
            "replace",
            "upper",
            "lower",
            "capitalize",
            "trim",
            "rtrim",
            "ltrim",
            "substr",
            "len",
            "tostring",
            "match",
            "urlencode",
            "urldecode",
            "defang",
            "type",
        ]

    @staticmethod
    def is_valid_expression(expr):
        """Check if the expression is a valid numeric/mathematics expression."""
        allowed_nodes = {
            ast.Expression,
            ast.BinOp,
            ast.UnaryOp,
            ast.Num,
            ast.Constant,
            ast.Add,
            ast.Sub,
            ast.Mult,
            ast.Div,
            ast.Pow,
            ast.Mod,
            ast.USub,
        }

        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as e:
            logging.error(f"[x] Invalid syntax: {e}")
            return False

        for node in ast.walk(tree):
            if type(node) not in allowed_nodes:
                logging.warning(f"[!] Disallowed node: {type(node).__name__}")
                return False

        return True

    def contains_known_sub_function(self, _string):
        for known_function_name in self.all_functions:
            if known_function_name in _string:
                return True
        return False

    @staticmethod
    def examine_string_args(input_str: str, replacement: str = "REPLACEMENT") -> list:
        # Enhanced patterns to identify list-like structures, considering nested lists encoded as strings
        list_pattern = r"\[(?:[^\[\]]*?)\]"
        replacements = (
            {}
        )  # Dictionary to store original structures with their placeholders

        def replacer(match):  # Function to replace with unique placeholders
            _original_text = match.group(0)
            _placeholder = f"{replacement}{len(replacements)}"
            replacements[_placeholder] = _original_text
            return _placeholder

        # Replace complex list-like structures and quoted strings with placeholders
        modified_str = re.sub(list_pattern, replacer, input_str)
        parts = modified_str.split(
            ","
        )  # Split the modified string by commas not within any complex structures
        restored_parts = []  # Initialize restored parts

        for part in parts:
            for placeholder, original_text in replacements.items():
                part = part.replace(placeholder, original_text)
            restored_parts.append(part.strip())

        literal_parts = []  # Convert restored parts into literal values when possible
        for part in restored_parts:
            try:
                # Enhanced to handle nested list-like strings correctly
                if part.startswith('"[') and part.endswith(']"'):
                    part = ast.literal_eval(part)
                literal_parts.append(ast.literal_eval(part))
            except (ValueError, SyntaxError) as e:
                logging.warning(f"[!] Literal eval failed for {part}: {e}")
                literal_parts.append(part)

        logging.info("[i] Split and replacement process completed successfully.")
        return literal_parts

    def split_and_preserve(
        self, all_vars: dict, input_str: str, replacement: str = "REPLACEMENT"
    ) -> list:
        # Enhanced patterns to identify list-like structures, considering nested lists encoded as strings
        list_pattern = r"\[(?:[^\[\]]*|\[(?:[^\[\]]*|\[[^\[\]]*\])*\])*\]"
        quote_pattern = r'"[^"]*"'
        nested_list_pattern = r'"\[.*?\]"'
        combined_pattern = f"({list_pattern})|({quote_pattern})|({nested_list_pattern})"
        replacements = (
            {}
        )  # Dictionary to store original structures with their placeholders

        def replacer(match):  # Function to replace with unique placeholders
            _original_text = match.group(0)
            _placeholder = f"{replacement}{len(replacements)}"
            replacements[_placeholder] = _original_text
            return _placeholder

        # Replace complex list-like structures and quoted strings with placeholders
        modified_str = re.sub(combined_pattern, replacer, input_str)
        parts = modified_str.split(
            ","
        )  # Split the modified string by commas not within any complex structures
        restored_parts = []  # Initialize restored parts

        for part in parts:
            for placeholder, original_text in replacements.items():
                part = part.replace(placeholder, original_text)
            restored_parts.append(part.strip())

        literal_parts = []  # Convert restored parts into literal values when possible
        for part in restored_parts:
            try:
                # Check and correctly handle nested lists encoded as strings
                part = part.strip()
                if part.startswith('"') and part.endswith('"'):
                    if not part.strip('"').strip().strip('"') in all_vars:
                        part = ast.literal_eval(part)
                    else:
                        part = part.strip()
                else:
                    part = ast.literal_eval(part)
                literal_parts.append(part)
            except (ValueError, SyntaxError) as e:
                logging.warning(f"[!] Literal eval failed for {part}: {e}")
                literal_parts.append(part)

        logging.info("[i] Split and replacement process completed successfully.")
        return literal_parts

    @staticmethod
    def validate_ast(_obj):
        try:
            return ast.literal_eval(_obj)
        except Exception as exc:
            logging.error("[x] AST validation failed: %s", exc)
            return _obj

    def ast_args(self, args):
        for index, arg in enumerate(args):
            arg = self.validate_ast(str(arg))
            if isinstance(arg, list):
                for _index, entry in enumerate(arg):
                    entry = self.validate_ast(str(entry))
                    if isinstance(entry, list):
                        logging.warning(
                            "[!] This depth hasn't been built yet. See GeneralHandler.ast_args()."
                        )
            args[index] = arg
        return args

    def clean_args(self, all_variables, _unchanged_params):
        _unchanged_args = self.split_and_preserve(
            all_variables, _unchanged_params.strip()
        )
        if len(_unchanged_args) == 1 and "[" in _unchanged_args[0]:
            _unchanged_args = self.ast_args(_unchanged_args[0])
        _args = []
        rerun_split_and_preserve = False
        for arg in _unchanged_args:
            orig_arg = arg
            if isinstance(arg, str):
                arg = arg.strip()
            if isinstance(arg, list):
                _args.append(arg)
                continue
            if arg in all_variables:
                rerun_split_and_preserve = True
                _args.append(all_variables[arg])
            else:
                _args.append(orig_arg)
        if rerun_split_and_preserve:
            _args = self.split_and_preserve(
                all_variables, str(_args).replace('"[', "[").replace(']"', "]")
            )[0]
        return _args

    @staticmethod
    def process_concat_args(*args):
        """
        Processes input arguments, converting string representations of lists into lists,
        and handles up to two levels of nesting for lists.

        Args:
            *args: Variable length argument list, can be a mix of strings, lists, and nested lists.

        Returns:
            A list containing processed arguments.
        """

        def evaluate_argument(arg):
            """Evaluates a single argument, converting strings to lists if applicable."""
            try:
                # Attempt to evaluate argument as a Python expression
                evaluated_arg = ast.literal_eval(arg)
                # If the result is a list, further process each element (if they are strings)
                if isinstance(evaluated_arg, list):
                    return [
                        (
                            ast.literal_eval(element)
                            if isinstance(element, str)
                            else element
                        )
                        for element in evaluated_arg
                    ]
                return evaluated_arg
            except (ValueError, SyntaxError):
                # Return the argument as is if it's not a string representation of a list
                return arg

        logging.info("[i] Processed input arguments successfully.")
        return [evaluate_argument(arg) for arg in args]

    def contains_known_function(
        self, input_string, known_functions, position=0, is_root=False
    ):
        if not input_string:
            return None, None, 0
        expression = []
        has_nested_function = False
        while position < len(input_string):
            char = input_string[position]

            if char.isalpha() and not is_root:  # Potential function call
                start = position
                while position < len(input_string) and input_string[position].isalpha():
                    position += 1
                func_name = input_string[start:position]

                # Skip optional spaces between function name and '('
                while position < len(input_string) and input_string[position] == " ":
                    position += 1

                if func_name in known_functions and input_string[position] == "(":
                    expression.append(func_name)
                    position += 1  # Skip '('
                    nested_expression, nested_has_nested_function, position = (
                        self.contains_known_function(
                            input_string, known_functions, position
                        )
                    )
                    expression.append(nested_expression)
                    has_nested_function = (
                        True  # Any found function is considered nested
                    )
                    continue

            if char == "(":
                position += 1  # Skip '('
                nested_expression, nested_has_nested_function, position = (
                    self.contains_known_function(
                        input_string, known_functions, position
                    )
                )
                expression.append("(" + nested_expression + ")")
                if nested_has_nested_function:
                    has_nested_function = True
            elif char == ")":
                position += 1  # Skip ')'
                break  # End of current expression
            else:
                expression_start = position
                while (
                    position < len(input_string) and input_string[position] not in ",()"
                ):
                    position += 1
                expression.append(input_string[expression_start:position])

            # Skip delimiters
            if position < len(input_string) and input_string[position] in ", ":
                position += 1

        return " ".join(expression), has_nested_function, position

    @staticmethod
    def extract_outermost_parentheses(expression_list):
        """
        Extracts elements from the outermost parentheses, handling nested parentheses.
        Args:
            expression_list (list): The list of strings representing the expression.
        Returns:
            list: The elements within the outermost parentheses, or the original list if no such elements are found.
        """
        start = None
        depth = 0

        for i, element in enumerate(expression_list):
            if element == "(":
                if depth == 0:
                    start = i
                depth += 1
            elif element == ")":
                depth -= 1
                if depth == 0 and start is not None:
                    return expression_list[
                        start + 1 : i
                    ]  # Exclude the parentheses themselves

        return expression_list  # No matching closing parenthesis found

    @staticmethod
    def process_ctx_children(ctx_children):
        # Concatenate with '###', then strip in the same order as the original
        concatenated = "###".join([str(obj) for obj in ctx_children])
        stripped = concatenated.strip("\n").strip(" ").strip("\n").strip("###")
        # Split based on '###', which now reflects the original manipulation more closely
        split = stripped.split("###")
        # Convert to string again as takewhile checks against the string "\n", not the newline character itself
        return [obj for obj in takewhile(lambda x: str(x) != "\n", split)]

    @staticmethod
    def extract_function_name(function_call: str) -> str:
        try:
            match = re.match(r"^(\w+)\s*\(", function_call)
            if match:
                return match.group(
                    1
                )  # Return the first captured group, which is the function name
            else:
                logging.warning("[!] No function name found in the input.")
                return ""
        except Exception as e:
            logging.error(f"[x] Error extracting function name: {e}")
            return ""

    @staticmethod
    def extract_db_names(input_string):
        try:
            match = re.search(r'file\s*=\s*"([^"]+)"', input_string)
            if match:
                # Splitting the matched group by commas and stripping any whitespace
                db_names = [db.strip() for db in match.group(1).split(",")]
                logging.info("[i] Database names extracted successfully.")
                return db_names
            else:
                # If the pattern is not found in the input string
                logging.error(
                    "[x] No database names could be extracted from the input string."
                )
                raise ValueError(
                    "No database names could be extracted from the input string."
                )
        except Exception as e:
            logging.error(f"[x] An error occurred while extracting database names: {e}")
            raise

    def extract_function_name_and_params(self, _input_string):
        """
        Extracts the name of the function and its parameters from the provided text.
        Args:
            _input_string: The string to search within.
        Returns:
            tuple: A tuple containing the name of the first matched function and its parameters as a string,
                   or (None, None) if no match is found.
        """
        # Prepare the function names for regex search (escape special characters and remove spaces)
        func_names = [re.escape(func.strip()) for func in self.all_funcs]

        # Join the function names with '|' to create a regex pattern that matches any of them
        pattern = r"((" + "|".join(func_names) + r")\s*\((.*?)\))"

        # Use non-greedy matching to capture parameters
        match = re.search(pattern, _input_string, re.DOTALL)

        if match:
            return match.group(2), match.group(1)
        else:
            return None, None

    @staticmethod
    def remove_outer_newlines(string_list):
        temp_list = list(
            dropwhile(lambda x: x == "\n", string_list)
        )  # Remove leading "\n" entries
        result_list = list(
            dropwhile(lambda x: x == "\n", reversed(temp_list))
        )  # Remove trailing "\n" entries
        return list(reversed(result_list))

    @staticmethod
    def flatten_outer_lists(nested_list):
        """
        Recursively flattens a list by removing nested outer lists if they are unnecessary.

        Parameters:
        nested_list (list): The nested list to flatten.

        Returns:
        The flattened list or the original element.
        """
        try:
            while isinstance(nested_list, list) and len(nested_list) == 1:
                nested_list = nested_list[0]
            return nested_list
        except TypeError as e:
            logging.error(f"[x] TypeError encountered in flatten_outer_lists: {e}")
            return nested_list
        except Exception as e:
            logging.error(
                f"[x] Unexpected error encountered in flatten_outer_lists: {e}"
            )
            return nested_list

    @staticmethod
    def resolve_terminal_nodes(antlr_objs):
        """
        Accepts a list of ANTLR4 objects and resolves each object of type
        antlr4.tree.Tree.TerminalNodeImpl to its ast.literal_eval value.

        Parameters:
        antlr_objs (list): The list of ANTLR4 objects to resolve.

        Returns:
        list: A list of resolved values.
        """
        resolved_values = []
        for obj in antlr_objs:
            # Check if the object is an instance of TerminalNodeImpl
            if isinstance(obj, antlr4.tree.Tree.TerminalNodeImpl):
                try:
                    value = ast.literal_eval(
                        obj.getText()
                    )  # Attempt to resolve the terminal node's text content
                    resolved_values.append(value)
                except (ValueError, SyntaxError):
                    try:
                        resolved_values.append(float(obj.getText()))
                    except Exception:
                        resolved_values.append(str(obj.getText()))
            else:
                logging.debug(
                    f"[DEBUG] Object {obj} is not of type TerminalNodeImpl and was skipped."
                )
        return resolved_values

    @staticmethod
    def filter_df_with_dates(main_df, date_dict):
        try:
            # Ensure that self.main_df is a pandas DataFrame
            if not isinstance(main_df, pd.DataFrame):
                logging.error("[x] The main_df attribute is not a pandas DataFrame.")
                return main_df

            # Extract keys from the date_dict which represent the date strings to filter by
            filter_dates = list(date_dict.keys())

            # Filter the DataFrame where the TIMESTAMP column matches any of the keys in date_dict
            filtered_df = main_df[main_df["TIMESTAMP"].isin(filter_dates)]
            logging.info(
                f"[i] Filtered DataFrame with {len(filtered_df)} rows based on TIMESTAMP matching."
            )

            return filtered_df
        except Exception as e:
            logging.error(
                f"[!] Failed to filter dataframe for the following Timestamp(s):\n{date_dict}.\n\t{e}"
            )
            return main_df

    @staticmethod
    def filter_dates(time_handler, start_time, end_time, target_field):
        try:
            start = time_handler.parse_input_date(start_time.strip().strip('"'))
            finish = time_handler.parse_input_date(end_time.strip().strip('"'))
            if not isinstance(target_field, list):
                logging.error(
                    "[!] Timerange was called but the third parameter was NOT a list as expected."
                )
                return {}

            # Use a dictionary comprehension with a single call to parse_input_date per item
            results = {
                x: epoch_time
                for x in target_field
                if (epoch_time := time_handler.parse_input_date(x)) >= start
                and epoch_time <= finish
            }

            # Logging the filtered dates with their epoch values
            logging.info(f"[i] Filtered dates and their epoch values: {results}")
            return results

        except Exception as e:
            logging.error(f"[x] An error occurred: {e}")
            return {}

    @staticmethod
    def prep_variables(all_variables):
        for variable_name, var_value in all_variables.items():
            if not var_value:
                all_variables[variable_name] = ""
                continue
            try:
                literal_value = ast.literal_eval(var_value)
                if type(literal_value) == list:
                    all_variables[variable_name] = literal_value
            except ValueError:
                try:
                    all_variables[variable_name] = float(var_value)
                except Exception as e:
                    logging.error(
                        f"[!] Issue converting {var_value} for {variable_name}.\n[!] {e}"
                    )

    @staticmethod
    def parse_comments(comments, _original_query):
        all_lines = [x.strip() for x in _original_query.split("\n")]
        output_query_without_comments = ""
        for index, line in enumerate(all_lines):
            if line.startswith("#"):
                comments[index] = f"{line}\n"
            else:
                output_query_without_comments += f"{line}\n"
        return output_query_without_comments

    @staticmethod
    def add_variables_as_columns(all_variables, main_df):
        """
        Adds variables from self.all_variables to self.main_df as new columns.
        Each key-value pair in self.all_variables is added as a column, with the key being the column name.
        Single values are repeated across all rows, and lists are added directly as column values.
        """
        for variable, value in all_variables.items():
            try:
                if isinstance(value, list):
                    if len(value) != len(main_df):
                        logging.warning(
                            f"[!] List length for '{variable}' doesn't match DataFrame length. Skipping."
                        )
                        continue
                    main_df[variable] = value
                else:
                    main_df[variable] = np.repeat(value, len(main_df))
            except Exception as e:
                logging.error(f"[x] Failed to add '{variable}' as a column: {e}")

    @staticmethod
    def get_main_df_overview(main_df):
        """
        Returns the headers and the first 2 entries of each column in the DataFrame.
        Additionally, logs the total count of rows and columns.
        This provides a quick overview of the fields available in the DataFrame, ensuring
        efficiency and robustness through error handling.

        :param main_df: The main DataFrame from which to generate the overview.
        :return: A pandas DataFrame containing the first 2 entries of each column.
        """
        try:
            if main_df.empty:  # Ensure the DataFrame is not empty
                logging.warning("[!] The DataFrame is empty.")
                return None

            # Log the total count of rows and columns
            total_rows, total_columns = main_df.shape
            logging.info(
                f"[+] FIELD SUMMARY:\n[i] Total Rows: {total_rows}, Total Columns: {total_columns}"
            )
            overview_df = main_df.head(
                2
            ).transpose()  # Select the first 2 entries of each column
            return overview_df

        except Exception as e:
            logging.error(f"[x] Error generating overview: {e}")
            return None

    @staticmethod
    def update_dataframe_column(main_df, field_name, field_values):
        """
        Update the specified column in the pandas DataFrame with the given list of values.

        Parameters:
        df (pd.DataFrame): The DataFrame to be updated.
        field_name (str): The name of the column in the DataFrame to update.
        field_values (list): A list of values to update the column with.

        Returns:
        pd.DataFrame: The updated DataFrame.
        """
        try:
            # Check if the field_name exists in the DataFrame
            if field_name not in main_df.columns:
                logging.error(
                    f"[x] The field '{field_name}' does not exist in the DataFrame."
                )
                return main_df

            # Check if the length of field_values matches the number of rows in the DataFrame
            if len(field_values) != len(main_df):
                logging.error(
                    "[x] The length of field_values does not match the DataFrame's length."
                )
                return main_df

            # Update the column with the new values
            main_df[field_name] = field_values
            logging.info("[i] DataFrame column updated successfully.")

        except Exception as e:
            logging.error(f"[x] An error occurred: {e}")

        return main_df

    @staticmethod
    def coalesce_lists(list_of_lists: List[List]) -> List:
        """
        Coalesces values across each entry in member lists, returning the first non-empty and non-None entry found.
        :param list_of_lists: List of lists with equal length, containing any type of values.
        :return: A single list with coalesced values.
        """
        try:
            if not list_of_lists:
                raise ValueError("Input list is empty")

            list_length = len(
                list_of_lists[0]
            )  # Check all lists are of the same length
            if not all(len(lst) == list_length for lst in list_of_lists):
                raise ValueError("Not all lists are of the same length")

            result = []  # Coalesce values
            for i in range(
                list_length
            ):  # Find the first non-empty, non-None value in each 'column'
                for lst in list_of_lists:
                    value = lst[i]
                    if value not in ["", None]:
                        result.append(value)
                        break
                else:  # If all values are "" or None, append a default value ("" or None)
                    result.append("")

            logging.info("[i] Coalesced list generated successfully.")
            return result

        except Exception as e:
            logging.error(f"[x] Error in coalescing lists: {e}")
            return []

    @staticmethod
    def remove_unnecessary_nesting(nested_list: Any) -> Any:
        """
        Removes unnecessary list encapsulations down to the least possible root without modifying
        the contents or the structure of the original nested list.

        :param nested_list: The nested list to be simplified.
        :return: The simplified list with minimal necessary nesting.
        """

        def is_single_element_list(item):
            return isinstance(item, list) and len(item) == 1

        # Recursive function to simplify the list
        def simplify_list(_nested_list):
            # If it's not a list or a single-element list, return it as is
            if not isinstance(_nested_list, list) or (
                is_single_element_list(_nested_list)
                and not isinstance(_nested_list[0], list)
            ):
                return _nested_list
            # If the list contains exactly one list element, dive deeper without losing its structure
            if is_single_element_list(_nested_list):
                return simplify_list(_nested_list[0])
            # Otherwise, iterate through the list and simplify each element
            simplified_list = []
            for item in _nested_list:
                simplified_item = simplify_list(item)
                simplified_list.append(simplified_item)
            return simplified_list

        # Start the simplification process
        return simplify_list(nested_list)

    # CRITICAL COMPONENT
    @staticmethod
    def are_all_terminal_instances(_obj):
        """
        Recursively checks if all items in the provided list or nested lists are instances of allowed types
        or if a single object is an instance of the allowed types.

        Args:
            _obj (any): The object, list, or nested list to check.

        Returns:
            bool: True if all items are instances of the allowed types or nested lists contain only allowed types.
        """
        allowed_types = (antlr4.tree.Tree.TerminalNodeImpl, str, int, float)

        # Direct instance check
        if isinstance(_obj, allowed_types):
            return True
        # Recursive list checking
        elif isinstance(_obj, list):
            for entry in _obj:
                # Recursively check each entry, immediately return False if any entry fails the check
                if not GeneralHandler.are_all_terminal_instances(entry):
                    return False
            # All entries in the list passed the check
            return True
        else:
            # Object is neither an allowed type nor a list
            return False

    @staticmethod
    def return_equal_components_from_list(custom_objects):
        """
        Finds the first occurrence of an object containing "=" in its string representation.
        Returns a tuple: the object just before it (left part), and a list of objects after it (right parts).

        Parameters:
        - custom_objects: List of custom objects.

        Returns:
        - A tuple where the first element is the object before the one with "=", and the second
          element is a list of objects after the one with "=".
        - If no "=" is found, returns (None, []).
        """
        for i, obj in enumerate(custom_objects):
            if "=" in str(obj):
                left_part = custom_objects[i - 1] if i > 0 else None
                right_parts = custom_objects[i + 1 :]
                return left_part, right_parts
        return None, []

    @staticmethod
    def replace_keys_with_values(_original_string, replace_dict):
        for key, value in replace_dict.items():
            _original_string = _original_string.replace(key, str(value))
        return _original_string

    @staticmethod
    def trim_ordered_dict_bottom_up(ordered_dict, key):
        """
        Return all items from an OrderedDict up to and including the specified key,
        matching the type of the key as well.

        Args:
        - ordered_dict (OrderedDict): The OrderedDict to search through.
        - key: The key up to which (inclusive) to return items, with type matching.

        Returns:
        - OrderedDict: A new OrderedDict containing all items up to and including the specified key.
        """
        new_dict = OrderedDict()
        key_found = False

        for k, v in ordered_dict.items():
            if type(k) == type(key) and k == key:
                new_dict[k] = v
                key_found = True
                break
            new_dict[k] = v

        if not key_found:
            logging.warning(
                "[!] Key not found in the OrderedDict or type mismatch. Returning partial OrderedDict."
            )
            return new_dict  # Return what was accumulated up to the point of failure.

        return new_dict

    @staticmethod
    def trim_to_last_sublist(mixed_list):
        """
        Trims the input list up to the last identified sublist.

        Parameters:
        mixed_list (list): A list containing a mixture of lists and single values (str, int, float, antlr4.tree.Tree.TerminalNodeImpl).

        Returns:
        list: Trimmed list up to the last identified sublist.
        """
        last_sublist_index = None
        for i, item in enumerate(mixed_list):
            if isinstance(item, list):
                last_sublist_index = i  # Update the index whenever a sublist is found

        # If no sublist is found, return the original list
        if last_sublist_index is None:
            return mixed_list

        # Return the list up to (and including) the last sublist
        return mixed_list[: last_sublist_index + 1]

    @staticmethod
    def preprocess_string_values(df, column_names):
        """
        Preprocess DataFrame string values in specified columns to strip surrounding quotes.

        Parameters:
        df (pd.DataFrame): The DataFrame to preprocess.
        column_names (list): List of column names to preprocess.
        """
        for col in column_names:
            if col in df.columns:
                df[col] = df[col].str.strip(
                    '"'
                )  # Strip double quotes from start and end of each string

    @staticmethod
    def join_values(values: List[Union[Any, List[Any]]], separator: str = " ") -> str:
        """
        Recursively concatenates all single values from a nested list or a list of single values
        into one single string, separated by a specified separator.

        Args:
        - values: A list of single value objects or a list of lists of single value objects.
        - separator: A string separator used to concatenate the values. Default is a space.

        Returns:
        - A string containing all single values concatenated with the specified separator.
        """

        def is_single_value(item) -> bool:
            """
            Check if the item is not a list and is an instance of allowed single value types.
            """
            allowed_types = (str, int, float, antlr4.tree.Tree.TerminalNodeImpl)
            return not isinstance(item, list) and isinstance(item, allowed_types)

        def recurse_items(items) -> str:
            """
            Recursively process each item in the list, concatenating single values
            and unpacking nested lists.
            """
            result = []
            for item in items:
                if is_single_value(item):
                    result.append(str(item))  # Convert all single values to strings
                elif isinstance(item, list):
                    result.append(
                        recurse_items(item)
                    )  # Recursive call for nested lists
                else:
                    logging.warning(f"[!] Unsupported item type: {type(item)}")
            return separator.join(result)

        if not isinstance(values, list):
            logging.error(
                f"[x] Invalid input type: {type(values)}. Expected a list or a list of lists."
            )
            return ""

        return recurse_items(values)

    @staticmethod
    def antlr_obj_to_str(obj):
        """Converts an antlr4 object to its string representation."""
        if isinstance(obj, antlr4.tree.Tree.TerminalNodeImpl):
            return obj.getText()
        return obj

    def list_to_query_part(self, expr_list):
        """Converts a list from the expression to a part of the query string."""
        parts = []
        for item in expr_list:
            if isinstance(item, list):
                part = self.list_to_query_part(item)
                parts.append(part)
            elif isinstance(item, antlr4.tree.Tree.TerminalNodeImpl):
                parts.append(self.antlr_obj_to_str(item))
            elif isinstance(item, str):
                if item == "AND":
                    parts.append("and")  # Correct logical operator
                elif item == "OR":
                    parts.append("or")  # Correct logical operator
                elif item in ["(", ")"]:
                    parts.append(item)  # Keep parentheses
                else:
                    # Assume string literals are column names or operators; literals should be handled outside
                    parts.append(item)
            else:
                parts.append(str(item))
        return " ".join(parts)

    def print_final_command(self, __last_val):
        for index, entry in enumerate(self.convert_nested_list(__last_val)):

            query_line_formatted = ""
            for _index, sub_entry in enumerate(entry):
                if sub_entry in ("\n", "<EOF>", "[", "]"):
                    pass
                else:
                    query_line_formatted += f" {sub_entry}"

            space_buffer = (4 - len(str(index))) * " "
            logging.info(f"[i] {index}{space_buffer}{query_line_formatted}")

    def convert_to_query_string(self, expression):
        """Converts the structured logical expression into a query string."""
        return (
            self.list_to_query_part(expression)
            .replace("AND", "and")
            .replace("OR", "or")
            .replace('"', "'")
        )

    def filter_df_with_logical_expression(self, main_df, expression):
        """Converts a complex logical expression into a pandas query string and filters the DataFrame."""
        # Convert your logical expression structure to a query string here
        # The following is a placeholder; you'll need to implement the conversion based on your actual logic
        query_string = self.convert_to_query_string(expression)
        return main_df.query(query_string)

    @staticmethod
    def rename_column(df, old_name, new_name):
        """
        Renames a column in a pandas DataFrame.

        :param df: The pandas DataFrame to modify.
        :param old_name: The name of the column to rename.
        :param new_name: The new name for the column.
        """
        if old_name not in df.columns:
            logging.error(
                f"[x] The column '{old_name}' does not exist in the DataFrame."
            )
            return df
        if new_name in df.columns:
            logging.warning(
                f"[!] A column named '{new_name}' already exists. Proceeding with rename might cause issues."
            )

        df.rename(columns={old_name: new_name}, inplace=True)
        logging.info(f"[i] Column '{old_name}' renamed to '{new_name}'.")
        return df

    @staticmethod
    def create_empty_dataframe(columns):
        """Create an empty pandas DataFrame with the specified columns."""
        try:
            if not isinstance(columns, list) or not all(
                isinstance(c, str) for c in columns
            ):
                raise DataFrameError("Columns must be a list of strings.")
            df = pd.DataFrame(columns=columns)
            logging.info(f"[i] Created empty DataFrame with columns: {columns}")
            return df
        except Exception as e:
            logging.error(f"[x] Failed to create empty DataFrame: {e}")
            return pd.DataFrame()

    @staticmethod  # FIELD Call Handling
    def filter_df_columns(df, columns, mode):
        """
        Filters a DataFrame based on the specified columns and mode, raising exceptions for errors.

        :param df: The original pandas DataFrame.
        :param columns: A list of column headers to include or exclude.
        :param mode: Specify 'include' to keep the columns, or 'exclude' to remove them.
        :return: A new DataFrame after applying the filter based on the mode.
        :raises DataFrameError: If any input validations fail.
        """
        # Validate input
        if not isinstance(df, pd.DataFrame):
            raise DataFrameError("The provided df is not a pandas DataFrame.")

        if not isinstance(columns, list) or not all(
            isinstance(col, str) for col in columns
        ):
            raise DataFrameError("The columns parameter must be a list of strings.")

        if mode not in ["-", "+"]:
            raise DataFrameError(
                "The mode parameter must be either '-' or '+', but defaults to '+'."
            )

        # Handle 'include' mode
        filtered_df = None
        if mode == "+":
            missing_columns = [col for col in columns if col not in df.columns]
            if missing_columns:
                logging.warning(
                    f"[!] The following specified columns do not exist in the DataFrame: {missing_columns}"
                )

            existing_columns = [col for col in columns if col in df.columns]
            if not existing_columns:
                # On an EMPTY DataFrame (e.g. ingestion legitimately
                # produced zero rows today and its parquet landed with
                # only _epoch) it's correct to return an empty DataFrame
                # with the requested schema rather than crashing: the
                # downstream pipeline (e.g. ``sort``, ``head``,
                # serializer) then sees a well-formed 0-row table with
                # the expected columns. Caught 2026-04-21 for
                # ag_kalshi_poly_arb + ag_poly_volume_spikes.
                if df.empty:
                    logging.info(
                        "[i] table: input DataFrame is empty; returning "
                        "a zero-row DataFrame with the requested schema %s",
                        columns,
                    )
                    return pd.DataFrame(columns=columns)
                raise DataFrameError(
                    "None of the specified columns exist in the DataFrame."
                )

            filtered_df = df[existing_columns].copy()
            logging.info(
                f"[i] DataFrame filtered to include specified columns. Columns retained: {existing_columns}"
            )

        # Handle 'exclude' mode
        elif mode == "-":
            filtered_df = df.drop(columns=columns, errors="ignore").copy()
            logging.info(
                f"[i] DataFrame filtered to exclude specified columns. Columns removed: {columns}"
            )

        return filtered_df

    @staticmethod  # HEAD call Handling
    def head_call(df, n, _mode):
        """
        Returns the first n rows of a DataFrame.

        :param df:      The pandas DataFrame to operate on.
        :param n:       The number of rows to return.
        :param _mode:   'head' or 'tail'
        :return:        A new DataFrame containing the first n rows.
        :raises         DataFrameError: If the input DataFrame is not valid or n is not a positive integer.
        """

        if not isinstance(df, pd.DataFrame):  # Validate inputs
            raise DataFrameError("The provided df is not a pandas DataFrame.")

        if not isinstance(n, int) or n < 1:  # Validate inputs
            raise DataFrameError(
                "The number of rows to return must be a positive integer."
            )

        if n > len(df):  # Check if n is larger than the DataFrame
            logging.warning(
                f"[!] HEAD CALL: Requested number of rows ({n}) exceeds DataFrame row count ({len(df)}). "
                "Returning all rows."
            )

        if str(_mode).lower() == "head":
            return df.head(n)
        elif str(_mode).lower() == "tail":
            return df.tail(n)
        else:
            raise DataFrameError('Error: Mode must be "head" or "tail".')

    @staticmethod  # SORT Call handling
    def sort_df_by_columns(df, columns, is_ascending="+"):
        """
        Sorts a DataFrame by the given column names, entirely in ascending or descending order.

        :param df: The pandas DataFrame to be sorted.
        :param columns: A list of column names to sort by, in order.
        :param is_ascending: Boolean indicating the sort order for all columns. True for ascending, False for descending.
                          Defaults to True.
        :return: A new DataFrame sorted based on the given columns and the specified sort order.
        :raises DataFrameError: If inputs are not valid or if any column name is not found.
        """

        # Upstream may return None when the index subdirectory does not
        # exist yet (day-1 of a freshly-deployed feeder). Treat the None
        # path the same as "empty DataFrame" - return an empty frame
        # rather than raise, so the dispatcher's per-feeder serializer
        # produces a well-shaped empty block instead of aborting.
        # Caught 2026-04-23 via tools/diagnose_alert_group during the
        # Global Macro Risk Brief pre-deploy validation.
        if df is None:
            return pd.DataFrame()
        if not isinstance(df, pd.DataFrame):  # Validate input DataFrame
            raise DataFrameError("The provided object is not a pandas DataFrame.")
        if not isinstance(columns, list) or not all(
            isinstance(col, str) for col in columns
        ):  # Validate columns list
            raise DataFrameError(
                "The columns parameter must be a list of string column names."
            )

        # Empty DataFrame: nothing to sort, never raise - the downstream
        # pipeline may still care about the shape (columns) which we
        # preserve. Prevents ``sort`` from aborting the whole query
        # when the prior pipe legitimately returned zero rows.
        if df.empty:
            logging.info(
                "[i] sort: input DataFrame is empty; returning it unchanged."
            )
            return df

        # Check for existence of all columns in DataFrame
        missing_columns = [col for col in columns if col not in df.columns]
        if missing_columns:
            raise DataFrameError(
                f"The DataFrame does not contain the specified columns: {missing_columns}"
            )
        if is_ascending == "+":
            return df.sort_values(by=columns, ascending=True)  # Perform sorting
        elif is_ascending == "-":
            return df.sort_values(by=columns, ascending=False)  # Perform sorting
        else:
            raise DataFrameError("")

    @staticmethod
    def reverse_df_rows(df):
        """
        Returns a new DataFrame with the order of rows reversed compared to the original DataFrame.

        :param df: The pandas DataFrame to be reversed.
        :return: A new DataFrame with reversed row order.
        :raises ValueError: If the input is not a pandas DataFrame.
        """

        if not isinstance(df, pd.DataFrame):  # Validate input DataFrame
            raise ValueError("The provided object is not a pandas DataFrame.")
        return df.iloc[::-1].reset_index(drop=True)  # Reverse the DataFrame rows

    def contains_sublist(self, lst: List[Any]) -> bool:
        """
        Checks if the provided list contains any sub lists.

        :param lst: List to be checked for sub lists.
        :return: True if there is at least one sublist, False otherwise.
        """
        for item in lst:
            if isinstance(item, (list, np.ndarray)):
                logging.info(
                    "[i] Sublist or numpy array found: %s",
                    self.convert_nested_list(item),
                )
                return True
        logging.info("[i] No sublist or numpy array found in the provided list.")
        return False

    def is_any_substring_present(strings_list, target_string):
        """
        Check if any string in the list is a substring of the target string.

        Parameters:
        strings_list (list of str): List of strings to check.
        target_string (str): The target string to search within.

        Returns:
        bool: True if any string in the list is found in the target string, False otherwise.
        """
        for substring in strings_list:
            if substring in target_string:
                return True
        return False

    def contains_numpy_ndarray(self, lst: List[Any]) -> bool:
        """
        Checks if the provided list contains any sub lists.

        :param lst: List to be checked for sub lists.
        :return: True if there is at least one sublist, False otherwise.
        """
        for item in lst:
            if isinstance(item, (list, np.ndarray)):
                logging.info(
                    "[i] Sublist or numpy array found: %s",
                    self.convert_nested_list(item),
                )
                return True
        logging.info("[i] No sublist or numpy array found in the provided list.")
        return False

    @staticmethod
    def filter_df_by_regex(
        dataframe: pd.DataFrame, column_header: str, regex_pattern_str: str
    ) -> pd.DataFrame:
        """
        Filters a pandas DataFrame based on a regular expression pattern applied to the specified column.

        :param dataframe: The pandas DataFrame to be filtered.
        :param column_header: The header name of the column to apply the regex to.
        :param regex_pattern_str: The regular expression pattern in string form to match against the column's values.
        :return: A new pandas DataFrame containing only the rows where the column's value matches the regex pattern.
        """
        try:
            # Verify if the column header exists in the DataFrame
            if column_header not in dataframe.columns:
                logging.warning(
                    f"[!] The column header '{column_header}' does not exist in the DataFrame."
                )
                return pd.DataFrame()

            # Compile the regex with the unescaped pattern
            compiled_regex = re.compile(regex_pattern_str.strip(" ").strip('"'))

            # Apply the compiled regex to the specified column and filter rows
            # matched_rows = dataframe[dataframe[column_header].astype(str).str.match(compiled_regex)]
            matched_rows = dataframe[
                dataframe[column_header].astype(str).str.contains(compiled_regex)
            ]

            # Log the result
            if matched_rows.empty:
                logging.info("[i] No rows match the provided regular expression.")
            else:
                logging.info(
                    f"[i] Found {len(matched_rows)} rows that match the regular expression."
                )

            return matched_rows

        except Exception as e:
            logging.error(f"[x] An error occurred while filtering the DataFrame: {e}")
            raise

    @staticmethod
    def convert_terminal_node(value):
        """
        Converts a TerminalNodeImpl to its Python equivalent: float for numbers and str for strings.
        """
        try:
            # Attempt to convert to float
            return float(value)
        except ValueError:
            # If conversion to float fails, return as string
            return str(value)

    def convert_nested_list(self, nested_list):
        """
        Recursively converts a nested list containing TerminalNodeImpl instances and other lists
        into a Python-native structure, with strings and floats.
        """
        if isinstance(
            nested_list, antlr4.tree.Tree.TerminalNodeImpl
        ):  # Directly convert terminal nodes
            return self.convert_terminal_node(str(nested_list))
        elif isinstance(nested_list, list):  # Recursively handle lists
            return [self.convert_nested_list(item) for item in nested_list]
        else:
            # logging.warning("[!] Unexpected type encountered: {}".format(type(nested_list)))
            return None

    @staticmethod
    def check_empty(value):
        """Check if the input is None, an empty list, or an empty numpy ndarray."""
        if value is None:
            logging.info("[i] The value is None, returning.")
            return True
        elif isinstance(value, list) and len(value) == 0:
            logging.info("[i] The list is empty, returning.")
            return True
        elif isinstance(value, np.ndarray) and value.size == 0:
            logging.info("[i] The numpy array is empty, returning.")
            return True
        else:
            return False

    @staticmethod
    def execute_dedup(main_df, args):
        """
        Remove duplicate or consecutive duplicate rows based on specified fields.

        Args:
            main_df (pd.DataFrame): Dataframe to perform deduplication against.
            args (list): Arguments list consisting of fields and possibly options.

        Returns:
            pd.DataFrame: DataFrame with duplicates removed as per the criteria.
        """
        field_list = []
        keep_first_n = 1
        consecutive = False

        # Parse arguments
        i = 0
        while i < len(args):
            if isinstance(args[i], (int, float)) and not isinstance(
                args[i], bool
            ):  # Check if the argument is a number
                keep_first_n = int(args[i])
                i += 1
            elif args[i] == "consecutive":
                consecutive = args[i + 2].lower() == "true"
                i += 3  # Skip 'consecutive', '=', 'TRUE/FALSE'
            elif args[i] == ",":
                i += 1  # Skip commas
            else:
                field_list.append(str(args[i]))
                i += 1

        # Tolerate empty input - an upstream `index` step with zero matching
        # files emits a 0-row DataFrame with no columns, and `dedup ticker`
        # would otherwise return None on a freshly-deployed feeder before its
        # first cron fire (then downstream eventstats crashes with TypeError).
        # An empty input round-trips as empty output regardless of fields.
        if main_df is None or main_df.empty:
            return main_df if main_df is not None else pd.DataFrame()

        # Ensure all specified fields exist in the DataFrame
        missing_fields = [field for field in field_list if field not in main_df.columns]
        if missing_fields:
            logging.error(f"[x] Missing fields in DataFrame: {missing_fields}")
            return None

        try:
            if consecutive:
                # Apply back-fill and check for consecutive duplicates
                main_df["temp_shifted"] = (
                    main_df[field_list]
                    .shift(1)
                    .bfill()
                    .eq(main_df[field_list])
                    .all(axis=1)
                )
                main_df["temp_group"] = main_df["temp_shifted"].cumsum()
                filtered_df = (
                    main_df.groupby("temp_group")
                    .head(keep_first_n)
                    .drop(columns=["temp_shifted", "temp_group"])
                )
            else:
                # Drop duplicates based on the specified fields
                filtered_df = main_df.drop_duplicates(subset=field_list, keep="first")
                if keep_first_n > 1:
                    main_df["temp_group"] = main_df[field_list].apply(
                        lambda row: "_".join(row.values.astype(str)), axis=1
                    )
                    main_df["temp_rank"] = main_df.groupby("temp_group").cumcount() + 1
                    filtered_df = main_df[main_df["temp_rank"] <= keep_first_n].drop(
                        columns=["temp_group", "temp_rank"]
                    )

            logging.info("[i] Duplicates removed successfully.")
            return filtered_df
        except Exception as e:
            logging.error(f"[x] Error during deduplication: {str(e)}")
            return None

    @staticmethod
    def execute_fieldsummary(df, maxvals=10):
        """
        Generate summary statistics for each field in the DataFrame.

        Args:
            df (pd.DataFrame): The DataFrame to summarize.
            maxvals (int): Maximum number of unique values to return in the summary.

        Returns:
            pd.DataFrame: A DataFrame containing summary statistics for each column.
        """
        summary = []

        for column in df.columns:
            stats = {
                "field": column,
                "count": df[column].count(),
                "distinct_count": df[column].nunique(),
                "is_exact": 1,  # Assuming count is always exact in this context
            }

            # Handle non-numeric data gracefully
            if pd.api.types.is_numeric_dtype(df[column]):
                stats["max"] = df[column].max()
                stats["min"] = df[column].min()
                stats["mean"] = df[column].mean()
                stats["stdev"] = df[column].std()
                stats["numeric_count"] = df[column].dropna().shape[0]
            else:
                stats["max"] = df[column].dropna().max()
                stats["min"] = df[column].dropna().min()
                stats["mean"] = None
                stats["stdev"] = None
                stats["numeric_count"] = (
                    0  # Non-numeric columns do not contribute to numeric counts
                )

            # Calculate top values and their counts
            distinct_values = df[column].dropna().value_counts().reset_index()
            distinct_values.columns = ["value", "count"]
            stats["values"] = distinct_values.head(maxvals).to_dict(orient="records")

            summary.append(stats)

        return pd.DataFrame(summary)

    @staticmethod
    def execute_rex(df, args):
        field = None
        mode = "regex"  # Default to regex mode
        max_match = 1
        sed_expression = None

        # Parse arguments
        if "field" in args:
            field_index = args.index("field") + 2
            field = args[field_index]
        if "mode" in args:
            mode_index = args.index("mode") + 2
            mode = args[mode_index]
            sed_expression = args[-1].strip('"')

        # Mode Fork: Regex or SED?
        if mode == "regex":
            regex = args[-1].strip('"')
            if "max_match" in args:
                max_match_index = args.index("max_match") + 2
                max_match = int(args[max_match_index])

            # Compile regex pattern with appropriate flags
            regex = re.sub(r"\?<([^>]+)>", r"?P<\1>", regex)
            pattern = re.compile(regex, flags=re.IGNORECASE | re.DOTALL)

            # max_match=0 means unlimited; convert to float('inf')
            effective_limit = max_match if max_match > 0 else float('inf')

            def extract_matches(cell):
                """Extract named groups from a scalar or MV field cell."""
                if isinstance(cell, list):
                    strings = [str(v) for v in cell]
                else:
                    strings = [str(cell)]
                results = {}
                match_count = 0
                for s in strings:
                    for m in pattern.finditer(s):
                        if match_count >= effective_limit:
                            break
                        for name, value in m.groupdict().items():
                            results.setdefault(name, []).append(value)
                        match_count += 1
                    if match_count >= effective_limit:
                        break
                return results

            extracted_df = df[field].apply(extract_matches).apply(pd.Series)
            for column in extracted_df.columns:
                # Replace NaN (no-match rows) with None, flatten single-element
                # lists to scalars for cleaner output.
                extracted_df[column] = extracted_df[column].apply(
                    lambda v: None if (isinstance(v, float) and v != v)
                    else v[0] if isinstance(v, list) and len(v) == 1
                    else v
                )
                if column in df.columns:
                    df[column + "_rex"] = extracted_df[column]
                else:
                    df[column] = extracted_df[column]

        elif mode == "sed" and sed_expression:
            parts = sed_expression.split("/")
            if len(parts) == 4 and parts[0] == "s":
                search_re, replacement, flags = parts[1], parts[2], parts[3]
                # MV-aware sed: apply replacement to each element in a list
                def _sed_replace(cell):
                    if isinstance(cell, list):
                        return [re.sub(search_re, replacement, str(v)) for v in cell]
                    return re.sub(search_re, replacement, str(cell))
                df[field] = df[field].apply(_sed_replace)

        return df

    @staticmethod
    def handle_base64(df, args):
        if len(args) < 3:
            raise ValueError("Insufficient arguments for BASE64 operation.")

        operation = args[1]  # "encode" or "decode"
        columns = args[2:]  # list of columns to apply base64 operation

        for column in columns:
            if column not in df.columns:
                raise ValueError(f"Column '{column}' does not exist in the DataFrame.")

            if operation == "encode":
                df[column] = df[column].apply(
                    lambda x: base64.b64encode(x.encode()).decode()
                )
            elif operation == "decode":
                df[column] = df[column].apply(lambda x: base64.b64decode(x).decode())
            else:
                raise ValueError(
                    f"Unsupported operation '{operation}' in BASE64 command."
                )

        return df

    @staticmethod
    def execute_fillnull(df, args):
        # Minimum: ['value', '=', '<fill_value>'] - 3 tokens
        if len(args) < 3 or "value" not in args:
            raise ValueError("Incorrect arguments for fillnull operation.")

        # Extract the value to fill NaNs with, which comes right after 'value', '='
        value_index = args.index("value") + 2
        fill_value = args[value_index].strip('"')

        # Anything after the fill value is a field name; if none, fill all columns
        remaining = args[value_index + 1:]
        fields = remaining if remaining else list(df.columns)

        for field in fields:
            if field not in df.columns:
                raise ValueError(f"Column '{field}' does not exist in the DataFrame.")
            df[field] = df[field].fillna(fill_value)

        return df

    @staticmethod
    def parse_outputlookup_args(args):
        """
        Parses a list of arguments into a dictionary.

        :param args: List of command parameters in the format ['param', '=', 'value', ...]
        :return: Dictionary of parameters
        """
        if len(args) == 1:
            return str(args[0])

        arg_dict = {}
        i = 0
        while i < len(args):
            if args[i] == "=":
                key = args[i - 1].lower()
                value = args[i + 1]
                if value.lower() in [
                    "true",
                    "false",
                ]:  # Convert the value to a boolean if necessary
                    value = value.lower() == "true"
                arg_dict[key] = value
                i += 2  # skip the next position as it's the value we just processed
            else:
                i += 1
        return arg_dict

    @staticmethod
    def execute_outputlookup(df, **kwargs):
        filename = kwargs.get("filename")
        append = kwargs.get("append", False)
        override_if_empty = kwargs.get("override_if_empty", True)
        overwrite = kwargs.get("overwrite", False)

        logging.info("[i] Starting the data output process.")

        # Exit if append and overwrite are added in the same, as these are mutually exclusive.
        if append:
            if overwrite:
                raise SyntaxError(
                    f'EXECUTE_LOOKUP() options "append" and "overwrite" are mutually exclusive.'
                )

        # Determine the output format from the file extension
        if "." in filename:
            _format = filename.split(".")[-1]
        else:
            logging.error("[x] Invalid filename. Please include a file extension.")
            raise ValueError(
                "Filename must include a valid extension (.csv, .tsv, .json, .yaml, .sqlite)"
            )

        # Check if file exists and handle append, overwrite, and override_if_empty logic
        file_exists = os.path.exists(filename)
        if file_exists:
            if not append and not overwrite:
                if override_if_empty:
                    if df.empty:
                        logging.info(
                            "[i] No data to output and override_if_empty is set. Deleting the file."
                        )
                        os.remove(filename)
                        return
                else:
                    logging.info(
                        "[i] No operation performed as no overwrite or append is specified."
                    )
                    return

        # Handling data output based on the format
        if _format in ["csv", "tsv"]:
            sep = "," if _format == "csv" else "\t"
            if append and file_exists:
                df_existing = pd.read_csv(filename, sep=sep)
                df = pd.concat([df_existing, df], ignore_index=True)
            df.to_csv(filename, sep=sep, index=False)
            logging.info(f"[i] Data written to {filename} in {_format.upper()} format.")
        elif _format == "json":
            df.to_json(filename, orient="records", lines=True)
            logging.info(f"[i] Data written to {filename} in JSON format.")
        elif _format == "yaml":
            # Atomic write per project convention (crash-safe rename).
            from functionality.atomic_write import write_text_atomic
            yaml_text = yaml.dump(df.to_dict(orient="records"), allow_unicode=True)
            write_text_atomic(filename, yaml_text)
            logging.info(f"[i] Data written to {filename} in YAML format.")
        elif _format == "sqlite":
            with sqlite3.connect(filename) as conn:
                df.to_sql(
                    name="data",
                    con=conn,
                    if_exists="replace" if overwrite else "append",
                    index=False,
                )
            logging.info(f"[i] Data written to {filename} in SQLite database.")
        else:
            logging.error("[x] Unsupported file format.")
            raise ValueError(
                "Unsupported file format. Supported formats: csv, tsv, json, yaml, sqlite"
            )

    @staticmethod
    def execute_outputnew(df: pd.DataFrame, filename: str) -> None:
        """Write a DataFrame to a new file.

        This method fails if the target file already exists. The output format
        is determined by the file extension and supports CSV, TSV, JSON, YAML
        and SQLite.

        Parameters
        ----------
        df : pandas.DataFrame
            Data to write.
        filename : str
            Destination file path including extension.

        Raises
        ------
        FileExistsError
            If ``filename`` already exists.
        ValueError
            If ``filename`` has an unsupported extension.
        """

        if os.path.exists(filename):
            logging.error(f"[x] File '{filename}' already exists.")
            raise FileExistsError(f"File '{filename}' already exists")

        if "." not in filename:
            raise ValueError(
                "Filename must include a valid extension (.csv, .tsv, .json, .yaml, .sqlite)"
            )

        ext = filename.split(".")[-1].lower()

        if ext in {"csv", "tsv"}:
            sep = "," if ext == "csv" else "\t"
            df.to_csv(filename, sep=sep, index=False)
        elif ext == "json":
            df.to_json(filename, orient="records", lines=True)
        elif ext == "yaml":
            # Atomic write per project convention (crash-safe rename).
            from functionality.atomic_write import write_text_atomic
            yaml_text = yaml.dump(df.to_dict(orient="records"), allow_unicode=True)
            write_text_atomic(filename, yaml_text)
        elif ext == "sqlite":
            with sqlite3.connect(filename) as conn:
                df.to_sql(name="data", con=conn, if_exists="fail", index=False)
        else:
            raise ValueError(
                "Unsupported file format. Supported formats: csv, tsv, json, yaml, sqlite"
            )

        logging.info(f"[i] Data written to new file {filename}.")

    @staticmethod
    def replace_variable_in_self_dot_values(
        stack: OrderedDict, var_name: str, replacement: List[Any]
    ) -> None:
        """
        Recursively replace occurrences of a variable name with a given list in an OrderedDict.

        :param stack: OrderedDict representing the custom stack.
        :param var_name: The variable name to replace.
        :param replacement: The list of values to replace the variable name with.
        """

        def recursive_replace(data: Any) -> Any:
            if isinstance(data, list):
                return [recursive_replace(item) for item in data]
            elif isinstance(data, dict):
                return {k: recursive_replace(v) for k, v in data.items()}
            elif isinstance(data, str) and data == var_name:
                logging.debug(
                    f"[DEBUG] Replacing variable '{var_name}' with {replacement}"
                )
                return replacement

        for key, value in stack.items():
            logging.debug(f"[DEBUG] Processing key: {key}")
            stack[key] = recursive_replace(value)

    @staticmethod
    def compare_two_lists(list1, list2):
        """
        Check if any of the string entries in list1 exist in any of the elements in list2.

        Args:
            list1 (list): List of strings to check within.
            list2 (list): List of elements (strings, floats, etc.) to check against.

        Returns:
            bool: True if any string from list1 is found in list2, otherwise False.
        """
        try:
            set1 = set(str(item) for item in list1)
            set2 = set(str(item) for item in list2)

            result = any(substring in string for string in set2 for substring in set1)
            return result
        except Exception as e:
            logging.error("[x] An error occurred while checking substrings: %s", e)
            return False

    # *********************************************************************************
    # CRITICAL COMPONENTS - NEW STUFF 11/02/2024
    # *********************************************************************************
    @staticmethod
    def expand_file_paths(path_pattern, file_extension=".parquet"):
        """
        Expands a directory path or file path pattern that may include wildcards
        and returns a list of matching file paths, recursively including all files
        with the specified extension in subdirectories.

        Args:
            path_pattern (str): A file path or directory that may include wildcards.
            file_extension (str): The file extension to filter by (e.g., '.parquet').

        Returns:
            list: A sorted list of unique matching file paths.
        """
        if not path_pattern:
            return []

        # Expand user tilde (~) and environment variables
        path_pattern = os.path.expandvars(os.path.expanduser(path_pattern))

        # If the path_pattern is a directory, append the recursive wildcard pattern
        if os.path.isdir(path_pattern):
            search_pattern = os.path.join(path_pattern, "**", f"*{file_extension}")
        else:
            # If the path_pattern already includes wildcards, use it as is
            # Ensure it includes the file extension
            if not path_pattern.endswith(file_extension):
                # Replace any trailing wildcard with the file extension
                if path_pattern.endswith("*"):
                    path_pattern = path_pattern.rstrip("*")
                path_pattern += f"*{file_extension}"

            # Ensure recursive search
            if "**" not in path_pattern:
                # Insert '**/' before the last component
                path_parts = os.path.split(path_pattern)
                search_pattern = os.path.join(path_parts[0], "**", path_parts[1])
            else:
                search_pattern = path_pattern

        # Use glob.glob to expand the pattern (supports wildcards and recursive)
        expanded_paths = glob.glob(search_pattern, recursive=True)

        # Filter out directories (in case any directories match the pattern)
        expanded_paths = [path for path in expanded_paths if os.path.isfile(path)]

        # Normalize paths to eliminate redundant separators and up-level references
        expanded_paths = [os.path.normpath(path) for path in expanded_paths]

        # Remove duplicates by converting to a set, then back to a list
        unique_paths = list(set(expanded_paths))

        # Sort the list for consistency
        unique_paths.sort()

        return unique_paths

    @staticmethod
    def load_job(job_id: str):
        """Load a saved query result by job ID from the JobStore.

        Supports lookup by exact ``job_id`` or by custom name.  Parquet-based
        storage - no pickle deserialization risk.

        Args:
            job_id: The job identifier (epoch_uuid) or custom name.

        Returns:
            A ``pandas.DataFrame`` with the saved results.

        Raises:
            FileNotFoundError: If the job does not exist or has expired.
        """
        from job_store import get_default_job_store
        return get_default_job_store().load(job_id)

    @staticmethod
    def make_results(count: int = 1, annotate: bool = False) -> pd.DataFrame:
        """Generate a result set with ``_epoch`` set to the current epoch.

        Generates synthetic result rows for the ``| makeresults`` command.

        Args:
            count:    Number of rows to generate (default 1).
            annotate: If True, add a ``_raw`` and ``server`` column.

        Returns:
            A new ``pandas.DataFrame``.
        """
        now = int(time.time())
        data = {"_epoch": [now] * count}
        if annotate:
            data["_raw"] = [""] * count
            data["server"] = ["local"] * count
        df = pd.DataFrame(data)
        logging.info(f"[i] makeresults: generated {count} row(s).")
        return df

    @staticmethod
    def add_loadjob_metadata(df: pd.DataFrame, job_id: str) -> pd.DataFrame:
        """Append metadata columns derived from the job ID.

        The job ID format is ``<epoch>.<fraction>_<uuid>``.  This method
        extracts the epoch portion and adds two fields:

        * ``_loadjob_time`` - the raw epoch value (float)
        * ``_loadjob_time_human`` - human-readable UTC timestamp

        Args:
            df:     The DataFrame returned by ``load_job()``.
            job_id: The job identifier used in the ``| loadjob`` call.

        Returns:
            The same DataFrame with the two extra columns.
        """
        epoch_val = None
        try:
            # job_id format: "1742000445.123456_<uuid>"
            epoch_str = job_id.split("_", 1)[0]
            epoch_val = float(epoch_str)
        except (ValueError, IndexError):
            # If the user passed a custom name, look up the real job_id
            try:
                from job_store import get_default_job_store
                meta = get_default_job_store().get_job_meta(job_id)
                if meta:
                    epoch_str = meta["job_id"].split("_", 1)[0]
                    epoch_val = float(epoch_str)
            except Exception:
                pass

        if epoch_val is not None:
            human = datetime.fromtimestamp(epoch_val, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
            df["_loadjob_time"] = epoch_val
            df["_loadjob_time_human"] = human
            logging.info(f"[i] loadjob metadata: epoch={epoch_val}, human={human}")
        else:
            logging.warning("[!] Could not extract epoch from job ID; skipping metadata.")

        return df

    @staticmethod
    def add_info(df: pd.DataFrame, query: str = "") -> pd.DataFrame:
        """Add informational fields about the search to every row.

        Adds search metadata fields to every row.  Adds:

        * ``info_min_time`` - earliest ``_epoch`` value in the results (or 0)
        * ``info_max_time`` - latest ``_epoch`` value in the results (or 0)
        * ``info_sid`` - a synthetic search ID
        * ``info_search_time`` - epoch when addinfo was executed

        Args:
            df:    The current results DataFrame.
            query: The original query string (used to build info_sid).

        Returns:
            The DataFrame with info columns appended.
        """
        now = time.time()

        # Derive min/max from _epoch column if present
        if "_epoch" in df.columns:
            try:
                numeric_times = pd.to_numeric(df["_epoch"], errors="coerce")
                info_min = float(numeric_times.min()) if not numeric_times.isna().all() else 0.0
                info_max = float(numeric_times.max()) if not numeric_times.isna().all() else 0.0
            except Exception:
                info_min = 0.0
                info_max = 0.0
        else:
            info_min = 0.0
            info_max = 0.0

        import uuid as _uuid
        sid = f"scheduler__{_uuid.uuid4().hex[:16]}_{query[:32]}" if query else f"scheduler__{_uuid.uuid4().hex[:16]}"

        df["info_min_time"] = info_min
        df["info_max_time"] = info_max
        df["info_sid"] = sid
        df["info_search_time"] = now

        logging.info("[i] addinfo: added info_min_time, info_max_time, info_sid, info_search_time.")
        return df

    # ------------------------------------------------------------------
    # New helper functions for directive processing
    # ------------------------------------------------------------------

    @staticmethod
    def execute_join(
        main_df: pd.DataFrame,
        sub_df: pd.DataFrame,
        fields: list,
        join_type: str = "inner",
    ) -> pd.DataFrame:
        """Perform a pandas merge using the specified join type and fields."""
        try:
            return main_df.merge(sub_df, on=fields, how=join_type)
        except Exception as e:
            logging.error(f"[x] JOIN failed: {e}")
            return main_df

    @staticmethod
    def execute_append(main_df: pd.DataFrame, add_df: pd.DataFrame) -> pd.DataFrame:
        """Append rows from add_df to main_df."""
        try:
            return pd.concat([main_df, add_df], ignore_index=True)
        except Exception as e:
            logging.error(f"[x] APPEND failed: {e}")
            return main_df

    @staticmethod
    def execute_spath(
        df: pd.DataFrame, input_col: str, output_col: str, json_path: str
    ) -> pd.DataFrame:
        """Extract JSON path from a column into a new column."""
        try:
            import json

            def extract(obj):
                try:
                    data = obj if isinstance(obj, dict) else json.loads(str(obj))
                    for part in json_path.split("."):
                        if isinstance(data, dict):
                            data = data.get(part)
                        else:
                            return None
                    return data
                except Exception:
                    return None

            df[output_col] = df[input_col].apply(extract)
        except Exception as e:
            logging.error(f"[x] SPATH failed: {e}")
        return df

    @staticmethod
    def execute_bin(df: pd.DataFrame, field: str, span: str) -> pd.DataFrame:
        """Bin a field into the provided span using pandas floor."""
        try:
            df[field] = pd.to_datetime(df[field]).dt.floor(span)
        except Exception as e:
            logging.error(f"[x] BIN failed: {e}")
        return df

    @staticmethod
    def execute_mvexpand(df: pd.DataFrame, field: str) -> pd.DataFrame:
        if field in df.columns:
            return df.explode(field).reset_index(drop=True)
        return df

    @staticmethod
    def execute_mvreverse(df: pd.DataFrame, field: str) -> pd.DataFrame:
        if field in df.columns:
            df[field] = df[field].apply(
                lambda x: list(reversed(x)) if isinstance(x, list) else x
            )
        return df

    @staticmethod
    def execute_mvcombine(
        df: pd.DataFrame, field: str, delim: str = " "
    ) -> pd.DataFrame:
        if field in df.columns:
            df[field] = df[field].apply(
                lambda x: delim.join(map(str, x)) if isinstance(x, list) else x
            )
        return df

    @staticmethod
    def execute_mvdedup(df: pd.DataFrame, field: str) -> pd.DataFrame:
        if field in df.columns:
            df[field] = df[field].apply(
                lambda x: list(dict.fromkeys(x)) if isinstance(x, list) else x
            )
        return df

    @staticmethod
    def execute_mvappend(df: pd.DataFrame, fields: list, target: str) -> pd.DataFrame:
        if not fields:
            return df
        try:
            df[target] = df.apply(
                lambda row: [
                    item
                    for f in fields
                    for item in (row[f] if isinstance(row[f], list) else [row[f]])
                ],
                axis=1,
            )
        except Exception as e:
            logging.error(f"[x] MVAPPEND failed: {e}")
        return df

    @staticmethod
    def execute_mvfilter(df: pd.DataFrame, field: str, value) -> pd.DataFrame:
        if field in df.columns:
            df[field] = df[field].apply(
                lambda x: (
                    [v for v in x if str(v) == str(value)] if isinstance(x, list) else x
                )
            )
        return df

    @staticmethod
    def execute_mvcount(
        df: pd.DataFrame, field: str, result_field: str
    ) -> pd.DataFrame:
        if field in df.columns:
            df[result_field] = df[field].apply(
                lambda x: len(x) if isinstance(x, list) else 0
            )
        return df

    @staticmethod
    def execute_mvdc(df: pd.DataFrame, field: str, result_field: str) -> pd.DataFrame:
        if field in df.columns:
            df[result_field] = df[field].apply(
                lambda x: len(set(x)) if isinstance(x, list) else 0
            )
        return df

    @staticmethod
    def execute_mvzip(
        df: pd.DataFrame, field1: str, field2: str, delim: str, result_field: str
    ) -> pd.DataFrame:
        try:
            df[result_field] = df.apply(
                lambda r: [
                    f"{a}{delim}{b}"
                    for a, b in zip(
                        r[field1] if isinstance(r[field1], list) else [r[field1]],
                        r[field2] if isinstance(r[field2], list) else [r[field2]],
                    )
                ],
                axis=1,
            )
        except Exception as e:
            logging.error(f"[x] MVZIP failed: {e}")
        return df

    @staticmethod
    def execute_mvjoin(df: pd.DataFrame, field: str, delim: str) -> pd.DataFrame:
        if field in df.columns:
            df[field] = df[field].apply(
                lambda x: delim.join(map(str, x)) if isinstance(x, list) else x
            )
        return df

    @staticmethod
    def execute_mvindex(
        df: pd.DataFrame, field: str, indexes: list, result_field: str
    ) -> pd.DataFrame:
        def pick(lst):
            if not isinstance(lst, list):
                return None
            res = []
            for i in indexes:
                if -len(lst) <= i < len(lst):
                    res.append(lst[i])
            return res[0] if len(indexes) == 1 else res

        if field in df.columns:
            df[result_field] = df[field].apply(pick)
        return df

    @staticmethod
    def execute_mvfind(df: pd.DataFrame, field: str, pattern: str) -> pd.DataFrame:
        """Search each list in ``field`` for ``pattern`` and record index."""
        if field not in df.columns:
            return df

        regex = re.compile(pattern)

        def find_index(value):
            if isinstance(value, list):
                for idx, item in enumerate(value):
                    if regex.search(str(item)):
                        return idx
                return -1
            if pd.isna(value):
                return -1
            return 0 if regex.search(str(value)) else -1

        df["mvfind"] = df[field].apply(find_index)
        return df

    @staticmethod
    def execute_coalesce(
        df: pd.DataFrame, fields: list, result_field: str = "coalesce"
    ) -> pd.DataFrame:
        """Return the first non-null/empty value across the provided fields."""
        missing = [f for f in fields if f not in df.columns]
        if missing:
            logging.error(f"[x] COALESCE failed, missing columns: {missing}")
            return df

        def pick(row):
            for f in fields:
                val = row[f]
                if pd.notna(val) and val != "":
                    return val
            return None

        df[result_field] = df.apply(pick, axis=1)
        return df

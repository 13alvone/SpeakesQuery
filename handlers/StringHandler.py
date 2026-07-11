#!/usr/bin/env python3
import ast
import logging
import re
import urllib.parse
from itertools import zip_longest
from typing import List

import numpy as np
import pandas as pd

all_funcs = [
    "concat", "replace", "upper", "lower", "capitalize", "trim", "rtrim", "ltrim", "substr", "len", "tostring",
    "match", "urlencode", "urldecode", "defang", "type"
]

class StringHandler:
    def __init__(self):
        self.all_functions = [
            'round (', 'min (', 'max (', 'avg (', 'sum (', 'range (', 'median (', 'sqrt (', 'random (', 'tonumb (',
            'dcount (', 'concat (', 'replace (', 'upper (', 'lowerm(', 'capitalize (', 'trim (', 'ltrim (', 'rtrim (',
            'substr (', 'len (', 'tostring (', 'match (', 'urlencode (', 'defang (', 'type ('
        ]
        self.all_funcs = [
            'round', 'min', 'max', 'avg', 'sum', 'range', 'median', 'sqrt', 'random', 'tonumb', 'dcount', 'concat',
            'replace', 'upper', 'lower', 'capitalize', 'trim', 'ltrim', 'rtrim', 'substr', 'len', 'tostring', 'match',
            'urlencode', 'defang', 'type'
        ]
        self.all_numeric_funcs = [
            'round', 'min', 'max', 'avg', 'sum', 'range', 'median', 'sqrt', 'random', 'tonumb', 'dcount', 'len'
        ]
        self.all_string_funcs = [
            "concat", "replace", "upper", "lower", "capitalize", "trim", "rtrim", "ltrim", "substr", "len",
            "tostring", "match", "urlencode", "urldecode", "defang", "type"
        ]

    # CRITICAL COMPONENT
    def vectorized_concat(self, *args):
        """
        Concatenates multiple arguments elementwise.
        Each argument can be either a scalar or a pandas Series.
        Scalars are broadcast to the length of the first Series encountered.
        The result is a pandas Series with elementwise concatenation.
        """
        logging.debug("[DEBUG] Starting vectorized_concat with arguments: %s", args)
        series_list = []
        ref_index = None
        for arg in args:
            if isinstance(arg, pd.Series):
                s = arg.astype(str)
                series_list.append(s)
                if ref_index is None:
                    ref_index = s.index
            else:
                # Assume scalar value.
                if ref_index is None:
                    raise ValueError("Cannot broadcast scalar without a reference Series.")
                series_list.append(pd.Series([str(arg)] * len(ref_index), index=ref_index))
        # Use the first series as the initial value.
        result = series_list[0]
        for s in series_list[1:]:
            result = result.str.cat(s, sep="")
        logging.debug("[DEBUG] vectorized_concat result: %s", result.head())
        return result

    # The rest of your functions remain as before.
    def transform_strings(self, data, operation):
        def apply_single_operation(value, op):
            if op == 'upper':
                return value.upper() if isinstance(value, str) else value
            elif op == 'lower':
                return value.lower() if isinstance(value, str) else value
            elif op == 'tostring':
                return f'{value}' if isinstance(value, str) else value
            elif op == 'capitalize':
                return value.capitalize() if isinstance(value, str) else value
            elif op == 'len':
                return len(value) if hasattr(value, '__len__') else value
            elif op == 'defang':
                return value.replace('.', '[.]').replace(':', '[:]') if isinstance(value, str) else value
            elif op == 'fang':
                return value.replace('[.]', '.').replace('[:]', ':') if isinstance(value, str) else value
            elif op == 'urlencode':
                return urllib.parse.quote(value) if isinstance(value, str) else value
            elif op == 'urldecode':
                return urllib.parse.unquote(value) if isinstance(value, str) else value
            else:
                raise ValueError("Unsupported operation.")
        def transform_list_string(list_string, op):
            try:
                parsed_list = ast.literal_eval(list_string)
                if isinstance(parsed_list, list):
                    transformed_list = [apply_single_operation(item, op) for item in parsed_list]
                    return str(transformed_list).replace("'", '"')
                else:
                    return apply_single_operation(list_string, op)
            except (ValueError, SyntaxError):
                return apply_single_operation(list_string, op)
        def recursive_apply(value, op):
            if isinstance(value, str):
                return transform_list_string(value, op)
            elif isinstance(value, list):
                return [recursive_apply(v, op) for v in value]
            elif isinstance(value, np.ndarray):
                return np.array([recursive_apply(v, op) for v in value])
            else:
                raise TypeError("Unsupported data type: {}".format(type(value)))
        data = self.try_ast_conversion(data)
        return recursive_apply(data, operation)

    def complex_replace(self, target, to_replace, replacement):
        def apply_replace(s, old, new):
            return s.replace(old, new)
        def recursive_replace(value, old, new):
            if isinstance(value, str):
                return apply_replace(value, old, new)
            elif isinstance(value, list):
                return [recursive_replace(item, old, new) for item in value]
            elif isinstance(value, np.ndarray):
                return np.array([recursive_replace(item, old, new) for item in value])
            else:
                raise TypeError("Unsupported data type: {}".format(type(value)))
        self.try_ast_conversion(to_replace)
        if not isinstance(to_replace, list):
            to_replace = [to_replace.strip(' ').strip('"')]
        if not isinstance(replacement, list):
            replacement = [replacement.strip(' ').strip('"')] * len(to_replace)
        if len(to_replace) != len(replacement):
            raise ValueError("to_replace and replacement lists must be of the same length.")
        for old, new in zip(to_replace, replacement):
            target = recursive_replace(target, old, new)
        return target

    @staticmethod
    def trim_strings(input_value, characters=" ", operation="trim"):
        def apply_trim(s, chars, op):
            if op == "ltrim":
                if s.startswith(chars):
                    return s[len(chars):]
                return s
            elif op == "rtrim":
                if s.endswith(chars):
                    return s[:-len(chars)]
                return s
            else:
                return apply_trim(apply_trim(s, chars, "ltrim"), chars, "rtrim")
        def recursive_trim(value, chars, op):
            if isinstance(value, list):
                return [recursive_trim(v, chars, op) for v in value]
            elif isinstance(value, np.ndarray):
                return np.array([recursive_trim(v, chars, op) for v in value.flat]).reshape(value.shape)
            else:
                return apply_trim(str(value), chars, op)
        return recursive_trim(input_value, characters, operation)

    @staticmethod
    def are_all_strings(lst: list):
        return all(isinstance(item, str) for item in lst)

    @staticmethod
    def convert_to_list(s):
        try:
            return ast.literal_eval(s)
        except (ValueError, SyntaxError):
            return s

    def try_ast_conversion(self, _obj):
        try:
            _obj = ast.literal_eval(_obj)
        except (ValueError, SyntaxError):
            return _obj
        input_value = []
        for entry in _obj:
            try:
                input_value.append(ast.literal_eval(entry))
            except ValueError:
                if isinstance(entry, np.ndarray):
                    input_value.append(entry)
                elif isinstance(entry, str):
                    input_value.append(entry.strip().strip('"'))
                elif isinstance(entry, (float, int)):
                    input_value.append(str(entry))
                else:
                    input_value.append(entry)
            except Exception as e:
                if isinstance(entry, str):
                    input_value.append(entry.strip().strip('"'))
        return input_value

    def trim_operation(self, args, operation="trim"):
        if not args or not args[0].any():
            raise ValueError("Input value is required.")
        input_value = self.try_ast_conversion(args[0])
        trim_characters = args[1].strip('"') if len(args) > 1 else " "
        modified_input = self.trim_strings(input_value, trim_characters, operation)
        return modified_input


#!/usr/bin/env python3
"""
MathematicOperations.py
Provides math-related helper functions.
"""

import math
import random
import decimal
import logging
import statistics
import ast
from typing import Union, List
import numpy as np

# Configure logger
logger = logging.getLogger(__name__)

class MathHandler:
    def __init__(self):
        pass

    @staticmethod
    def complex_randomize(_input):
        """
        Randomizes the input value by producing a random number between -value and value.
        This function works recursively if the input is a list or a NumPy array.
        It determines the number of decimal places by attempting to convert the input 
        to a Decimal. If that conversion fails (due to an invalid operation), it defaults 
        to 0 decimal places.
        """
        def get_decimal_places(value):
            try:
                dec = decimal.Decimal(str(value))
                return -dec.as_tuple().exponent if dec.as_tuple().exponent < 0 else 0
            except (decimal.InvalidOperation, ValueError):
                # Default to 0 decimal places if conversion fails.
                return 0

        def randomize_value(value):
            decimal_places = get_decimal_places(value)
            # Although 'step' is computed, it is not used for now.
            step = 10 ** -decimal_places
            return round(random.uniform(-value, value), decimal_places)

        def recursive_randomize(value):
            if isinstance(value, list):
                return [recursive_randomize(v) for v in value]
            elif isinstance(value, np.ndarray):
                return np.array([recursive_randomize(v) for v in value])
            else:
                return randomize_value(value)

        return recursive_randomize(_input)

    @staticmethod
    def complex_round(target, precision):
        """
        Rounds the target value (or each element of a list/array) to the specified precision.
        """
        def recursive_round(value, prec):
            if isinstance(value, list):
                return [recursive_round(v, prec) for v in value]
            elif isinstance(value, np.ndarray):
                return np.array([recursive_round(v, prec) for v in value])
            else:
                return round(value, prec)
        return recursive_round(target, precision)

    @staticmethod
    def column_operation(operation, _input):
        """
        Applies a column operation (min, max, avg, range, median, mode, dcount)
        to the input data, either on a list or recursively on nested lists.
        """
        def calculate_median(data):
            return statistics.median(data) if data else None

        def calculate_mode(data):
            try:
                return statistics.mode(data)
            except statistics.StatisticsError:
                return None

        def recursive_operation(value, op):
            if isinstance(value, list):
                if any(isinstance(el, list) for el in value):
                    return [recursive_operation(sublist, op) if isinstance(sublist, list) else sublist for sublist in value]
                else:
                    if op == 'min':
                        return min(value)
                    elif op == 'max':
                        return max(value)
                    elif op == 'avg':
                        return sum(value) / len(value) if value else None
                    elif op == 'range':
                        return max(value) - min(value) if value else None
                    elif op == 'median':
                        return calculate_median(value)
                    elif op == 'mode':
                        return calculate_mode(value)
                    elif op == 'dcount':
                        return len(set(value))
            else:
                if op in ['min', 'max', 'avg', 'range', 'median', 'mode', 'dcount']:
                    return value
                else:
                    raise ValueError("Unsupported operation for single value.")
        return recursive_operation(_input, operation)

    @staticmethod
    def apply_unary_operation(operation_array_list):
        """
        Applies a unary operation (++, --, or ~) to a NumPy array. The input list must contain exactly
        two elements: the operator (as a string) and the NumPy array.
        """
        if len(operation_array_list) != 2:
            logging.error("[x] The input list must contain exactly two elements.")
            raise ValueError("The input list must contain exactly two elements.")
        unary_ops = {'++', '--', '~'}
        operator = None
        array = None
        for item in operation_array_list:
            if isinstance(item, str) and item in unary_ops:
                operator = item
            elif isinstance(item, np.ndarray):
                array = item
        if operator is None or array is None:
            logging.error("[x] The input list must contain one unary operator and one np array.")
            raise ValueError("The input list must contain one unary operator and one np array.")
        try:
            if operator == '++':
                result = array + 1
                logging.info("[i] Applied '++' operation successfully.")
            elif operator == '--':
                result = array - 1
                logging.info("[i] Applied '--' operation successfully.")
            elif operator == '~':
                result = ~array
                logging.info("[i] Applied '~' operation successfully.")
            else:
                logging.error("[x] Unsupported unary operator.")
                raise ValueError("Unsupported unary operator.")
        except Exception as e:
            logging.error(f"[x] An error occurred while applying the unary operation: {str(e)}")
            raise
        return result

    @staticmethod
    def process_input(value: Union[List[Union[int, float, str]], int, float, str]) -> List[float]:
        """
        Converts a single value or a list of values into a list of floats.
        """
        if isinstance(value, (int, float, str)):
            value = [value]
        return [float(v) for v in value]

    @staticmethod
    def is_numeric(entry):
        """
        Checks whether the given entry (or the first element of a list) is numeric.
        """
        if isinstance(entry, list):
            if entry:
                return all(isinstance(item, (int, float)) for item in entry[0])
            else:
                return []
        else:
            return isinstance(entry, (int, float))

    @staticmethod
    def validate_ast(_obj):
        """
        Attempts to convert _obj to a Python literal using ast.literal_eval. 
        If conversion fails, returns the original object.
        """
        try:
            return ast.literal_eval(str(_obj))
        except Exception as e:
            logging.info(f'[i] Literal ast translation failed for {str(_obj)}: {e}. Returning and moving on...')
            return _obj

    @staticmethod
    def validate_numeric_asts(_obj):
        """
        Recursively applies validate_ast to elements if _obj is a list.
        """
        if isinstance(_obj, list):
            return [MathHandler.validate_numeric_asts(entry) for entry in _obj]
        else:
            return MathHandler.validate_ast(_obj)


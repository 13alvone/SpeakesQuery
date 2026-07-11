#!/usr/bin/env python3
"""Helper utilities for working with ANTLR parse trees."""
from __future__ import annotations

from typing import Any, Callable, List
import re


def flatten_list(result: Any) -> Any:
    """Flatten one level of nested lists.

    Lists with a single element are collapsed to that element to avoid
    unnecessary nesting.
    """
    if isinstance(result, list):
        flat_result: List[Any] = []
        for item in result:
            if isinstance(item, list):
                flat_result.extend(item)
            else:
                flat_result.append(item)
        if len(flat_result) == 1:
            return flat_result[0]
        return flat_result
    return result


def flatten_with_parens(input_list: Any) -> List[str]:
    """Recursively flatten ``input_list`` while keeping parentheses tokens."""

    def flatten_recursive(element: Any) -> List[str]:
        if isinstance(element, list):
            if not element:
                return []
            result: List[str] = []
            for item in element:
                if item == "(":
                    result.append("(")
                elif item == ")":
                    result.append(")")
                else:
                    result.extend(flatten_recursive(item))
            return result
        if isinstance(element, str):
            return [element]
        return []

    return flatten_recursive(input_list)


def ctx_flatten(ctx: Any, extractor: Callable[[Any], Any]) -> List[str]:
    """Return a normalized list of tokens from ``ctx``.

    ``extractor`` should return a nested list representation of ``ctx``.
    Whitespace around tokens and between identifiers and ``("`` is stripped.
    """

    flattened = flatten_with_parens(extractor(ctx))

    normalized: List[str] = []
    for token in flattened:
        token = token.strip()
        token = re.sub(r"([a-zA-Z_][a-zA-Z_0-9]*)\s*\(", r"\1(", token)
        normalized.append(token)

    return normalized


__all__ = [
    "flatten_list",
    "flatten_with_parens",
    "ctx_flatten",
]

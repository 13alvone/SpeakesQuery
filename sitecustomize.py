"""Site customisation hook enabling PyCharm debugger attachment.

The Python interpreter imports :mod:`sitecustomize` automatically when the
module is discoverable on :data:`sys.path`.  By placing this file at the project
root the debug attach logic runs for every SpeakesQuery Python process without
requiring changes to the application entry points.
"""
from __future__ import annotations

import logging

_LOGGER = logging.getLogger("speakesquery.sitecustomize")
_LOGGER.addHandler(logging.NullHandler())

try:
    from devtools import debug_attach
except Exception as exc:  # pragma: no cover - defensive logging
    _LOGGER.warning(
        "[!] sitecustomize failed to import devtools.debug_attach: %s", exc, exc_info=True
    )
else:
    try:
        debug_attach.auto_attach_if_enabled()
    except Exception as exc:  # pragma: no cover - defensive logging
        _LOGGER.warning(
            "[!] sitecustomize encountered an error during PyCharm attach: %s",
            exc,
            exc_info=True,
        )

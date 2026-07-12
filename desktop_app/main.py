#!/usr/bin/env python3
"""
SpeakesQuery Desktop
------------------
A macOS/Linux desktop wrapper that runs the full SpeakesQuery Flask server
inside a native window via pywebview.

Requires pywebview >= 5.0:
    pip install pywebview

Launch:
    python desktop_app/main.py
"""

import sys
import os
import logging
import threading
import time
import socket

# ---------------------------------------------------------------------------
# Ensure the project root (parent of this file's directory) is on sys.path
# so the existing speakesquery modules import cleanly.
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import webview

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _wait_for_server(host: str, port: int, timeout: float = 10.0) -> bool:
    """Block until the Flask server is accepting connections (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> None:
    host = "127.0.0.1"
    port = int(os.environ.get("PORT", 5111))

    # Import the Flask app from the server module (registers all routes).
    from server import app as flask_app

    # Start Flask in a daemon thread so it shuts down when the window closes.
    flask_thread = threading.Thread(
        target=lambda: flask_app.run(
            host=host, port=port, debug=False, use_reloader=False,
        ),
        daemon=True,
    )
    flask_thread.start()

    if not _wait_for_server(host, port):
        sys.exit("[x] Flask server did not start within 10 seconds.")

    # Start the scheduled input engine (server.py only starts it in __main__).
    try:
        from scheduled_input_engine import start_engine
        start_engine()
    except Exception as exc:
        logger.error("[x] Failed to start scheduled input engine: %s", exc)

    url = f"http://{host}:{port}"
    # The desktop app binds loopback, so the W11b access-token gate is
    # normally inactive. If the operator forced it on (SPEAKESQUERY_AUTH=on),
    # inject the token so the embedded window authenticates transparently.
    if flask_app.config.get("SPQ_AUTH_REQUIRED"):
        token = flask_app.config.get("SPQ_ACCESS_TOKEN") or ""
        if token:
            url = f"{url}/?token={token}"
    logger.info("[i] Server ready at %s", url.split("?")[0])

    window = webview.create_window(
        title="SpeakesQuery",
        url=url,
        width=1440,
        height=900,
        resizable=True,
        min_size=(900, 600),
        background_color="#161616",
    )

    webview.start(debug=False)

    # Clean shutdown when the window closes.
    try:
        from scheduled_input_engine import shutdown_engine
        shutdown_engine()
    except Exception:
        pass


if __name__ == "__main__":
    main()

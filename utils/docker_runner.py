#!/usr/bin/env python3
"""Run a Python script inside an isolated Docker container."""

import argparse
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from utils.env_crypto import decrypt_env


logger = logging.getLogger(__name__)


def _contains_fetch_api_call(script_path: Path) -> bool:
    """Return True if the script references FETCH_API_DATA."""
    try:
        text = script_path.read_text()
    except Exception as exc:  # pragma: no cover - file access should rarely fail
        logger.error("[x] Unable to read %s: %s", script_path, exc)
        return False
    return "FETCH_API_DATA" in text


def run_script_in_container(
    script: Path,
    env_enc: Path | None = None,
    memory: str = "512m",
    cpus: str = "1",
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Execute *script* inside a Docker container and return the result."""
    repo_root = Path(__file__).resolve().parents[1]
    script_path = (repo_root / script).resolve()
    if not script_path.is_file():
        raise FileNotFoundError(f"Script not found: {script_path}")

    allow_net = _contains_fetch_api_call(script_path)

    temp_env_file = None
    try:
        cmd = [
            "docker",
            "run",
            "--rm",
            "--memory",
            memory,
            "--cpus",
            cpus,
            "-v",
            f"{repo_root}:/app:ro",
            "-w",
            "/app",
        ]
        if env_enc:
            decrypted = decrypt_env(str(env_enc))
            tf = tempfile.NamedTemporaryFile("w", delete=False)
            tf.write(decrypted)
            tf.close()
            temp_env_file = tf.name
            cmd += ["--env-file", temp_env_file]
        if not allow_net:
            cmd += ["--network", "none"]
        cmd += ["python:3.11-slim", "python", str(script_path.relative_to(repo_root))]
        logger.info("[i] Running %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result
    finally:
        if temp_env_file and os.path.exists(temp_env_file):
            os.remove(temp_env_file)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("script", help="Path to Python script to run, relative to repo root")
    parser.add_argument("--env-enc", help="Path to encrypted .env file", default=None)
    parser.add_argument("--memory", default="512m", help="Memory limit, e.g. 512m")
    parser.add_argument("--cpus", default="1", help="CPU quota")
    parser.add_argument("--timeout", type=int, default=60, help="Execution timeout in seconds")

    args = parser.parse_args()
    try:
        result = run_script_in_container(
            Path(args.script),
            Path(args.env_enc) if args.env_enc else None,
            memory=args.memory,
            cpus=args.cpus,
            timeout=args.timeout,
        )
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    except Exception as exc:  # pragma: no cover - CLI error paths
        logger.error("[x] %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()

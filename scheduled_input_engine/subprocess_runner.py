#!/usr/bin/env python3
"""Run repo scripts in a restricted subprocess."""
import asyncio
from dataclasses import dataclass
from pathlib import Path
import logging
import os
import sys

logger = logging.getLogger(__name__)

@dataclass
class SubprocessResult:
    """Result of a subprocess execution."""
    stdout: str
    stderr: str
    returncode: int


async def run_in_subprocess(
    script_path: Path, timeout: int = 60, env: dict | None = None
) -> SubprocessResult:
    """Execute a script in a subprocess with a timeout.

    Parameters
    ----------
    script_path : Path
        Path to the script to execute.
    timeout : int
        Seconds before the process is killed.
    Returns
    -------
    SubprocessResult
        Captured output and return code.
    """
    script_path = script_path.resolve()
    if not script_path.is_file():
        raise FileNotFoundError(script_path)

    cwd = script_path.parent
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "PYTHONUNBUFFERED": "1",
    }
    if env:
        base_env.update(env)

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        cwd=str(cwd),
        env=base_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.error("[x] Timeout running %s", script_path.name)
        return SubprocessResult("", f"Timeout after {timeout}s", -1)

    stdout = stdout_b.decode("utf-8", "replace")
    stderr = stderr_b.decode("utf-8", "replace")

    if stdout:
        logger.info("[i] %s stdout:\n%s", script_path.name, stdout)
    if stderr:
        logger.warning("[!] %s stderr:\n%s", script_path.name, stderr)
    if proc.returncode != 0:
        logger.error("[x] %s exited with %s", script_path.name, proc.returncode)

    return SubprocessResult(stdout, stderr, proc.returncode)

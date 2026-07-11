"""Ollama bootstrap helper - Phase 2 / Bet 3 slice 8.

One-shot CLI that takes an operator from "no Ollama" to "working `| llm`
dispatch against a local model" in one command. Ships with the install
flow as the optional final step:

    python -m tools.ollama_bootstrap                 # default ollama-llama3-1-8b
    python -m tools.ollama_bootstrap --model <id>    # different registry id
    python -m tools.ollama_bootstrap --no-pull       # don't auto-pull missing
    python -m tools.ollama_bootstrap --yes           # non-interactive auto-pull
    python -m tools.ollama_bootstrap --json          # machine-readable output

The tool:

1. **Resolves** the registered Ollama model from `model_store` (default
   ``ollama-llama3-1-8b``). Bails with a clear message if the registry
   has no Ollama-provider entries.

2. **Detects** the daemon at the registry's `endpoint` (default
   ``http://localhost:11434``). On unreachable, prints OS-specific
   install guidance and exits 1.

3. **Lists** locally-available models. If the registered ``model_name``
   is missing, offers to pull it (auto with ``--yes``, prompted
   interactively otherwise; ``--no-pull`` bails instead).

4. **Verifies** end-to-end with a 1-token test inference against
   ``/api/chat``. Success → exit 0.

**No automated install of Ollama itself.** Sandbox boundary: this tool
detects + nudges + pulls models, but never runs `brew install` or
`curl | sh` on the operator's behalf. The install hint is printed; the
operator runs it.

The detection / list / pull / verify functions are factored as pure
helpers (`detect_ollama`, `list_local_models`, `pull_model`,
`verify_inference`) so the test suite can mock the HTTP layer cleanly.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
_DEFAULT_MODEL_ID = "ollama-llama3-1-8b"
_DEFAULT_ENDPOINT = "http://localhost:11434"
_DEFAULT_TIMEOUT_S = 30.0
_PULL_TIMEOUT_S = 600.0  # model pulls can take minutes on first run

_INSTALL_HINTS = {
    "darwin": (
        "Install Ollama for macOS:\n"
        "    brew install ollama\n"
        "  or download from https://ollama.com/download\n"
        "Then start the daemon (most installs auto-start):\n"
        "    ollama serve"
    ),
    "linux": (
        "Install Ollama for Linux:\n"
        "    curl -fsSL https://ollama.com/install.sh | sh\n"
        "Start the daemon (systemd is auto-enabled by the installer):\n"
        "    systemctl --user start ollama  # or `ollama serve`"
    ),
    "win32": (
        "Install Ollama for Windows:\n"
        "    Download from https://ollama.com/download/OllamaSetup.exe\n"
        "    Run the installer; the daemon starts automatically."
    ),
}


# ── Result types ────────────────────────────────────────────────────

@dataclass
class OllamaStatus:
    """Result of probing the Ollama daemon at a given endpoint."""
    reachable: bool
    endpoint: str
    version: Optional[str] = None
    error: Optional[str] = None


@dataclass
class BootstrapReport:
    """Aggregated outcome for the full bootstrap run. Used as the
    ``--json`` output shape and as the test-side assertion target.
    """
    model_id: str
    model_name: str
    endpoint: str
    detected: bool = False
    ollama_version: Optional[str] = None
    locally_available_before: list[str] = field(default_factory=list)
    pull_attempted: bool = False
    pull_succeeded: bool = False
    locally_available_after: list[str] = field(default_factory=list)
    inference_succeeded: bool = False
    inference_text: Optional[str] = None
    exit_code: int = 0
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "model_name": self.model_name,
            "endpoint": self.endpoint,
            "detected": self.detected,
            "ollama_version": self.ollama_version,
            "locally_available_before": self.locally_available_before,
            "pull_attempted": self.pull_attempted,
            "pull_succeeded": self.pull_succeeded,
            "locally_available_after": self.locally_available_after,
            "inference_succeeded": self.inference_succeeded,
            "inference_text": self.inference_text,
            "exit_code": self.exit_code,
            "messages": self.messages,
        }


# ── Pure helpers (HTTP-mockable) ────────────────────────────────────

def detect_ollama(
    endpoint: str = _DEFAULT_ENDPOINT,
    *,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> OllamaStatus:
    """Probe the daemon at ``endpoint``.

    Tries ``GET /api/version`` first; falls back to ``GET /api/tags`` if
    the version endpoint is missing on older builds. Either responding
    with HTTP 200 + JSON counts as "reachable".
    """
    base = endpoint.rstrip("/")
    try:
        # nosec B113 - timeout is supplied
        resp = requests.get(f"{base}/api/version", timeout=float(timeout))  # nosec B113
    except requests.RequestException as exc:
        return OllamaStatus(
            reachable=False, endpoint=endpoint,
            error=f"{type(exc).__name__}: {exc}",
        )
    if resp.status_code == 200:
        try:
            payload = resp.json()
            version = payload.get("version") or "unknown"
        except ValueError:
            version = "unknown"
        return OllamaStatus(reachable=True, endpoint=endpoint, version=version)
    # Some older Ollama builds don't expose /api/version - try /api/tags
    if resp.status_code == 404:
        try:
            # nosec B113 - timeout is supplied
            tags_resp = requests.get(f"{base}/api/tags", timeout=float(timeout))  # nosec B113
        except requests.RequestException as exc:
            return OllamaStatus(
                reachable=False, endpoint=endpoint,
                error=f"{type(exc).__name__}: {exc}",
            )
        if tags_resp.status_code == 200:
            return OllamaStatus(
                reachable=True, endpoint=endpoint, version="legacy",
            )
    return OllamaStatus(
        reachable=False, endpoint=endpoint,
        error=f"HTTP {resp.status_code}: {resp.text[:200]}",
    )


def list_local_models(
    endpoint: str = _DEFAULT_ENDPOINT,
    *,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> list[str]:
    """Return ``model_name`` strings known to the local daemon.

    Empty list on any error or missing endpoint.
    """
    base = endpoint.rstrip("/")
    try:
        # nosec B113 - timeout is supplied
        resp = requests.get(f"{base}/api/tags", timeout=float(timeout))  # nosec B113
    except requests.RequestException as exc:
        logger.warning(
            "[!] list_local_models: %s - %s", type(exc).__name__, exc,
        )
        return []
    if resp.status_code != 200:
        return []
    try:
        payload = resp.json()
    except ValueError:
        return []
    return [
        m.get("name") for m in payload.get("models") or [] if m.get("name")
    ]


def pull_model(
    model_name: str,
    *,
    endpoint: str = _DEFAULT_ENDPOINT,
    timeout: float = _PULL_TIMEOUT_S,
    progress_cb=None,
) -> bool:
    """Pull ``model_name`` via ``POST /api/pull`` (streaming).

    Returns True on success (final ``status: "success"`` message) and
    False on any failure path. ``progress_cb(message: str)`` is invoked
    for each progress line if supplied - the CLI uses it to print dots.
    """
    base = endpoint.rstrip("/")
    try:
        # nosec B113 - timeout is supplied
        resp = requests.post(  # nosec B113
            f"{base}/api/pull",
            json={"name": model_name},
            stream=True,
            timeout=float(timeout),
        )
    except requests.RequestException as exc:
        logger.warning(
            "[!] pull_model: HTTP transport failed: %s", exc,
        )
        if progress_cb:
            progress_cb(f"error: {type(exc).__name__}: {exc}")
        return False
    if resp.status_code != 200:
        logger.warning(
            "[!] pull_model: HTTP %d: %s", resp.status_code, resp.text[:200],
        )
        if progress_cb:
            progress_cb(f"error: HTTP {resp.status_code}")
        return False

    final_status = ""
    for raw_line in resp.iter_lines():
        if not raw_line:
            continue
        try:
            message = json.loads(raw_line)
        except ValueError:
            continue
        # Ollama emits {status: "...", completed: N, total: N} or {status: "success"}
        status = message.get("status") or ""
        if progress_cb:
            progress_cb(status)
        final_status = status
        if message.get("error"):
            logger.warning("[!] pull_model: server error: %s", message["error"])
            return False
    return final_status.lower() == "success"


def verify_inference(
    model_name: str,
    *,
    endpoint: str = _DEFAULT_ENDPOINT,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> tuple[bool, Optional[str]]:
    """Send a 1-token chat request to confirm the model is dispatchable.

    Returns ``(success, response_text_or_None)``. Uses the same
    ``/api/chat`` endpoint as the production router so this is a true
    end-to-end smoke test.
    """
    base = endpoint.rstrip("/")
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ok?"}],
        "stream": False,
        "options": {"num_predict": 1},
    }
    try:
        # nosec B113 - timeout is supplied
        resp = requests.post(  # nosec B113
            f"{base}/api/chat", json=payload, timeout=float(timeout),
        )
    except requests.RequestException as exc:
        logger.warning(
            "[!] verify_inference: HTTP transport failed: %s", exc,
        )
        return False, None
    if resp.status_code != 200:
        logger.warning(
            "[!] verify_inference: HTTP %d: %s",
            resp.status_code, resp.text[:200],
        )
        return False, None
    try:
        body = resp.json()
    except ValueError:
        return False, None
    content = (body.get("message") or {}).get("content")
    return bool(content), content


# ── Bootstrap orchestration ──────────────────────────────────────────

def _platform_install_hint() -> str:
    """Pick the closest install-hint string for ``sys.platform``."""
    if sys.platform.startswith("darwin"):
        return _INSTALL_HINTS["darwin"]
    if sys.platform.startswith("linux"):
        return _INSTALL_HINTS["linux"]
    if sys.platform.startswith("win"):
        return _INSTALL_HINTS["win32"]
    # Fallback for unknown platforms
    return (
        "Install Ollama from https://ollama.com/download for your "
        "platform; then start the daemon."
    )


def _resolve_ollama_model(model_id: Optional[str]) -> tuple[Optional[dict], str]:
    """Look up the model record from `model_store`.

    Returns ``(record, error_message)``. Either ``record`` is a dict with
    ``provider == "ollama"`` and a non-empty ``endpoint``, or
    ``record`` is None and ``error_message`` describes why.
    """
    from model_store import get_store
    target_id = model_id or _DEFAULT_MODEL_ID
    record = get_store().get_model(target_id)
    if record is None:
        return None, (
            f"Unknown model_id: {target_id!r}. Use --model to pick a "
            "different registered model, or run `python -c \"import "
            "model_store; print([m['id'] for m in "
            "model_store.get_store().list_models()])\"` to list available."
        )
    if record.get("provider") != "ollama":
        return None, (
            f"Model {target_id!r} has provider={record.get('provider')!r}, "
            "not 'ollama'. The bootstrap helper only sets up Ollama. "
            "Use --model to pick an Ollama-provider entry."
        )
    if not record.get("endpoint"):
        return None, (
            f"Model {target_id!r} has no endpoint. Edit the registry "
            f"YAML or pass a different --model."
        )
    return record, ""


def _prompt_yes_no(
    question: str, *, default_yes: bool = False, auto_yes: bool = False,
) -> bool:
    """Tiny y/n prompt with default. ``auto_yes`` short-circuits to True."""
    if auto_yes:
        return True
    suffix = " [Y/n] " if default_yes else " [y/N] "
    try:
        answer = input(question + suffix).strip().lower()
    except EOFError:
        return default_yes
    if not answer:
        return default_yes
    return answer in ("y", "yes")


def bootstrap(
    model_id: Optional[str] = None,
    *,
    no_pull: bool = False,
    auto_yes: bool = False,
    timeout: float = _DEFAULT_TIMEOUT_S,
    output_json: bool = False,
    log_fn=print,
) -> BootstrapReport:
    """Run the full bootstrap flow. Returns a `BootstrapReport`.

    The CLI wraps this; callers can also drive it programmatically
    (e.g. from an install-script subshell).
    """
    record, err = _resolve_ollama_model(model_id)
    if record is None:
        report = BootstrapReport(
            model_id=model_id or _DEFAULT_MODEL_ID, model_name="",
            endpoint="", detected=False,
            messages=[f"resolve failed: {err}"], exit_code=1,
        )
        if not output_json:
            log_fn(f"[x] {err}")
        return report

    report = BootstrapReport(
        model_id=record["id"],
        model_name=record["model_name"],
        endpoint=record["endpoint"],
    )

    # 1. Detect daemon
    if not output_json:
        log_fn(f"[i] Probing Ollama daemon at {report.endpoint} ...")
    status = detect_ollama(report.endpoint, timeout=timeout)
    report.detected = status.reachable
    report.ollama_version = status.version
    if not status.reachable:
        msg = (
            f"Ollama daemon not reachable at {report.endpoint} "
            f"({status.error or 'unknown error'})."
        )
        report.messages.append(msg)
        report.exit_code = 1
        if not output_json:
            log_fn(f"[x] {msg}")
            log_fn("")
            log_fn(_platform_install_hint())
            log_fn("")
            log_fn(
                "Once Ollama is running, re-run "
                "`python -m tools.ollama_bootstrap`."
            )
        return report
    if not output_json:
        log_fn(f"[i] Ollama daemon reachable (version: {status.version})")

    # 2. List local models
    locals_now = list_local_models(report.endpoint, timeout=timeout)
    report.locally_available_before = list(locals_now)
    if not output_json:
        log_fn(
            f"[i] {len(locals_now)} local model(s) currently available: "
            f"{locals_now if locals_now else '(none)'}"
        )

    # 3. Pull if missing
    if report.model_name not in locals_now:
        if no_pull:
            msg = (
                f"Model {report.model_name!r} is not local and --no-pull "
                "was specified. Skipping pull."
            )
            report.messages.append(msg)
            report.exit_code = 1
            if not output_json:
                log_fn(f"[!] {msg}")
            return report
        wants_pull = _prompt_yes_no(
            f"Model {report.model_name!r} is not local. Pull it now?",
            default_yes=True, auto_yes=auto_yes,
        )
        if not wants_pull:
            msg = "Pull declined by operator. Bootstrap cannot continue."
            report.messages.append(msg)
            report.exit_code = 1
            if not output_json:
                log_fn(f"[!] {msg}")
            return report
        report.pull_attempted = True
        if not output_json:
            log_fn(f"[i] Pulling {report.model_name!r} (this may take minutes)...")

        last_progress = ""

        def _on_progress(s: str) -> None:
            nonlocal last_progress
            if s != last_progress and not output_json:
                log_fn(f"    ↳ {s}")
                last_progress = s

        ok = pull_model(
            report.model_name, endpoint=report.endpoint,
            timeout=_PULL_TIMEOUT_S, progress_cb=_on_progress,
        )
        report.pull_succeeded = ok
        if not ok:
            msg = (
                f"Pull of {report.model_name!r} failed. Check the "
                "Ollama daemon logs and try `ollama pull "
                f"{report.model_name}` manually."
            )
            report.messages.append(msg)
            report.exit_code = 1
            if not output_json:
                log_fn(f"[x] {msg}")
            return report
        if not output_json:
            log_fn(f"[i] Pull complete.")

        # Re-list after pull
        locals_now = list_local_models(report.endpoint, timeout=timeout)
    report.locally_available_after = list(locals_now)

    # 4. Verify with 1-token inference
    if not output_json:
        log_fn(
            f"[i] Verifying inference dispatch against {report.model_name!r}..."
        )
    ok, text = verify_inference(
        report.model_name, endpoint=report.endpoint, timeout=timeout,
    )
    report.inference_succeeded = ok
    report.inference_text = text
    if not ok:
        msg = (
            f"Test inference against {report.model_name!r} failed. "
            "The daemon is reachable and the model is local but "
            "/api/chat did not return a usable response."
        )
        report.messages.append(msg)
        report.exit_code = 1
        if not output_json:
            log_fn(f"[x] {msg}")
        return report

    msg = (
        f"OK - Ollama is reachable, {report.model_name!r} is local, "
        f"and inference round-trips. `| llm model=\"{report.model_id}\" "
        "prompt=\"...\"` is now usable."
    )
    report.messages.append(msg)
    report.exit_code = 0
    if not output_json:
        log_fn(f"[i] {msg}")
    return report


# ── CLI ──────────────────────────────────────────────────────────────

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Bootstrap a local Ollama installation against a registered "
            "Ollama-provider model in SpeakesQuery's model registry. "
            "Detects the daemon, pulls the model if absent, and verifies "
            "end-to-end with a 1-token test inference."
        ),
    )
    p.add_argument(
        "--model", default=None,
        help=(
            f"Registered model id to bootstrap. Default: "
            f"{_DEFAULT_MODEL_ID!r}."
        ),
    )
    p.add_argument(
        "--no-pull", action="store_true",
        help=(
            "Don't pull the model if it's missing locally. Exit 1 instead."
        ),
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="Non-interactive: auto-confirm any prompts.",
    )
    p.add_argument(
        "--timeout", type=float, default=_DEFAULT_TIMEOUT_S,
        help=(
            f"HTTP timeout (seconds) for non-pull operations. Pulls use "
            f"a longer fixed timeout ({_PULL_TIMEOUT_S}s). "
            f"Default: {_DEFAULT_TIMEOUT_S}s."
        ),
    )
    p.add_argument(
        "--json", action="store_true",
        help=(
            "Emit the BootstrapReport as JSON on stdout, suppressing "
            "human-readable output."
        ),
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)
    report = bootstrap(
        model_id=args.model,
        no_pull=args.no_pull,
        auto_yes=args.yes,
        timeout=args.timeout,
        output_json=args.json,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())

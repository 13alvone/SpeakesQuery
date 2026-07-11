"""
Email Group Store
─────────────────
YAML-based CRUD for reusable email distribution lists ("email groups" /
"mailing lists"). Each group is one ``email_groups/<name>.yaml`` file
with a name, optional description, and a list of recipient entries.
Recipient entries may be literal email addresses (``user@domain.com``)
or references to OTHER groups (``@group_name``) for nested mailing
lists.

Authoritative resolution helper :func:`resolve_recipients_for_send`
expands ``@group_name`` references at email-send time. Cycles are
detected and broken with a one-shot warning; unknown group references
are silently skipped (logged at WARNING) so a typo never blocks a send
that has at least some valid literal recipients.
"""

import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import yaml

from functionality.atomic_write import write_text_atomic
from validation.EmailGroupValidation import EmailGroupValidation

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.resolve()
EMAIL_GROUPS_DIR = _PROJECT_ROOT / "email_groups"

# Maximum nesting depth for ``@group_name`` references. Resolution stops
# at this depth to avoid pathological cycles or nested-group chains
# blowing up. Effectively unlimited for any sensible use case.
_MAX_RESOLUTION_DEPTH = 16


class EmailGroupStore:
    """Manages email-group YAML files."""

    def __init__(self):
        self._dir = EMAIL_GROUPS_DIR
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self):
        """Create the email_groups directory if it does not exist."""
        os.makedirs(self._dir, exist_ok=True)
        logger.info("[i] EmailGroupStore initialised (dir=%s)", self._dir)

    # ------------------------------------------------------------------
    # YAML helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Convert a group name to a safe filename (no extension)."""
        safe = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip())
        safe = re.sub(r"_+", "_", safe).strip("_")
        if not safe:
            raise ValueError("Group name produces an empty filename after sanitisation.")
        return safe

    def _yaml_path(self, name: str) -> Path:
        return self._dir / f"{self._sanitize_filename(name)}.yaml"

    @staticmethod
    def _read_yaml(path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def _write_yaml(path: Path, data: dict):
        text = yaml.dump(
            data, default_flow_style=False, allow_unicode=True, sort_keys=False,
        )
        write_text_atomic(path, text, encoding="utf-8")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate(data: dict) -> dict:
        """Validate all required fields. Returns the normalised data."""
        name = EmailGroupValidation.validate_name(data.get("name", ""))
        description = EmailGroupValidation.validate_description(
            data.get("description", "")
        )
        addresses = EmailGroupValidation.validate_email_addresses(
            data.get("email_addresses", [])
        )
        return {
            "name": name,
            "description": description,
            "email_addresses": addresses,
        }

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save_group(self, data: dict, *, overwrite: bool = False) -> dict:
        """Create a new email group YAML. Returns the saved record."""
        validated = self._validate(data)
        now = datetime.now().isoformat()
        record = {
            "name": validated["name"],
            "description": validated["description"],
            "email_addresses": validated["email_addresses"],
            "created_at": now,
            "updated_at": now,
        }

        path = self._yaml_path(record["name"])

        with self._lock:
            if path.exists() and not overwrite:
                raise FileExistsError(
                    f'An email group named "{record["name"]}" already exists.'
                )
            if path.exists() and overwrite:
                try:
                    existing = self._read_yaml(path)
                    record["created_at"] = existing.get("created_at", now)
                except Exception:
                    pass
            self._write_yaml(path, record)

        logger.info("[+] Email group written: %s", path.name)
        _emit_config_event(record["name"], "create", None, record)
        return record

    def list_groups(self) -> list:
        """Return all email groups sorted by name."""
        results = []
        if not self._dir.exists():
            return results
        for path in sorted(self._dir.glob("*.yaml")):
            try:
                data = self._read_yaml(path)
                results.append(data)
            except Exception as exc:
                logger.warning("[!] Failed to read %s: %s", path.name, exc)
        results.sort(key=lambda g: g.get("name", ""))
        return results

    def get_group(self, name: str) -> dict:
        """Return a single email group by name."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Email group "{name}" not found.')
        return self._read_yaml(path)

    def update_group(self, name: str, data: dict) -> dict:
        """Update an existing email group. Returns the updated record."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Email group "{name}" not found.')

        before = self._read_yaml(path)
        merged = dict(before)
        # Updatable fields - name is immutable (filename is keyed on it)
        for key in ("description", "email_addresses"):
            if key in data:
                merged[key] = data[key]
        # Re-validate the merged record (raises if invalid)
        validated = self._validate({
            "name": before.get("name", name),
            "description": merged.get("description", ""),
            "email_addresses": merged.get("email_addresses", []),
        })
        record = {
            "name": validated["name"],
            "description": validated["description"],
            "email_addresses": validated["email_addresses"],
            "created_at": before.get("created_at", datetime.now().isoformat()),
            "updated_at": datetime.now().isoformat(),
        }

        with self._lock:
            self._write_yaml(path, record)

        logger.info("[~] Email group updated: %s", path.name)
        _emit_config_event(name, "update", before, record)
        return record

    def delete_group(self, name: str):
        """Hard-delete: remove the YAML file."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Email group "{name}" not found.')
        try:
            data = self._read_yaml(path)
        except Exception:
            data = {"name": name}
        with self._lock:
            path.unlink()
        logger.info("[x] Email group deleted: %s", name)
        _emit_config_event(name, "delete", data, None)

    # ------------------------------------------------------------------
    # Resolution - used by every email-send code path
    # ------------------------------------------------------------------

    def resolve_recipients(
        self,
        raw,
        *,
        _depth: int = 0,
        _seen: set | None = None,
    ) -> list:
        """
        Expand ``@group_name`` references in a raw recipient list / string
        and return a flat list of literal email addresses.

        Inputs accepted:
          - ``"alice@x.com, @sales_team, bob@y.com"`` (string, comma/semi
            delimited)
          - ``["alice@x.com", "@sales_team", "bob@y.com"]`` (list)

        Behaviour:
          - Literal addresses pass through unchanged.
          - ``@group_name`` references are resolved by reading the YAML
            and recursively expanding any group references inside.
          - Cycles are detected via ``_seen`` and broken: the offending
            group is skipped, a single WARNING is emitted.
          - Unknown group references are silently skipped (WARNING
            logged) - the rest of the list still resolves so a typo never
            blocks a send.
          - Output is de-duplicated case-insensitively while preserving
            first-seen order.

        This function is the single resolution choke-point - call sites
        in :mod:`query_engine.Alert` and :mod:`alert_groups.dispatcher`
        invoke this before passing to any SMTP layer.
        """
        if _depth > _MAX_RESOLUTION_DEPTH:
            logger.warning(
                "[!] Email-group resolution exceeded max depth (%d); "
                "remaining references skipped.", _MAX_RESOLUTION_DEPTH,
            )
            return []
        if _seen is None:
            _seen = set()

        entries = EmailGroupValidation.split_raw_recipients(raw)
        out = []
        seen_emails = set()

        for entry in entries:
            ref = EmailGroupValidation.GROUP_REF_REGEX.match(entry)
            if ref:
                group_name = ref.group(1)
                if group_name in _seen:
                    logger.warning(
                        "[!] Email-group cycle detected at '%s' - skipping to avoid loop.",
                        group_name,
                    )
                    continue
                try:
                    group = self.get_group(group_name)
                except FileNotFoundError:
                    logger.warning(
                        "[!] Email-group reference '@%s' not found - skipping.",
                        group_name,
                    )
                    continue
                except Exception as exc:
                    logger.warning(
                        "[!] Failed to read email group '%s': %s - skipping.",
                        group_name, exc,
                    )
                    continue
                nested_seen = set(_seen)
                nested_seen.add(group_name)
                resolved = self.resolve_recipients(
                    group.get("email_addresses", []),
                    _depth=_depth + 1,
                    _seen=nested_seen,
                )
                for addr in resolved:
                    key = addr.lower()
                    if key not in seen_emails:
                        seen_emails.add(key)
                        out.append(addr)
                continue

            # Literal email - silently drop anything that doesn't match
            # the email regex (rather than raise) so one bad entry never
            # blocks a send. Bad entries are logged at WARNING.
            if not EmailGroupValidation.EMAIL_REGEX.match(entry):
                logger.warning(
                    "[!] Skipping invalid recipient entry '%s' (not an email or '@group_name').",
                    entry,
                )
                continue
            key = entry.lower()
            if key not in seen_emails:
                seen_emails.add(key)
                out.append(entry)

        return out


# ── Module-level shared instance + functional resolver ──────────────
#
# The dispatcher and Alert paths import :func:`resolve_recipients_for_send`
# directly (no constructor noise). The shared store instance is lazily
# initialised on first use so test code can stub it without paying the
# directory-creation cost.

_shared_store: EmailGroupStore | None = None
_shared_lock = threading.Lock()


def get_shared_store() -> EmailGroupStore:
    """Lazy-init the module-level shared :class:`EmailGroupStore`."""
    global _shared_store
    with _shared_lock:
        if _shared_store is None:
            store = EmailGroupStore()
            store.initialize()
            _shared_store = store
        return _shared_store


def resolve_recipients_for_send(raw, *, store: EmailGroupStore | None = None) -> list:
    """Public resolver - expand ``@group_name`` refs in a raw recipient field.

    Returns a flat, de-duplicated list of literal email addresses ready
    to hand to SMTP. Never raises - bad entries log WARNINGS and are
    skipped so partial-recipient sends still proceed.
    """
    s = store if store is not None else get_shared_store()
    try:
        return s.resolve_recipients(raw)
    except Exception as exc:
        logger.warning(
            "[!] Email-group resolution failed: %s - falling back to literal split.",
            exc,
        )
        # Fallback: best-effort literal split + filter for valid emails
        entries = EmailGroupValidation.split_raw_recipients(raw)
        return [
            e for e in entries
            if not e.startswith("@")
            and EmailGroupValidation.EMAIL_REGEX.match(e)
        ]


def _reset_shared_store_for_tests() -> None:
    """Test hook - drop the shared instance so a fresh dir can be wired."""
    global _shared_store
    with _shared_lock:
        _shared_store = None


def _emit_config_event(
    name: str, action: str,
    old_value: dict | None, new_value: dict | None,
) -> None:
    """Record an email-group CRUD event to the config log stream - never raises."""
    try:
        from functionality.log_writer import log_config_change
        log_config_change(
            subject=name,
            action=action,
            subject_type="email_group",
            old_value=old_value,
            new_value=new_value,
            actor="api",
            source="email_group_store",
        )
    except Exception as exc:
        logger.warning(
            "[!] Email-group audit log failed for '%s' action=%r: %s",
            name, action, exc,
        )

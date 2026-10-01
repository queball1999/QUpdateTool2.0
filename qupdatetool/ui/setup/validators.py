"""
Read-only checks the wizard's Validate page runs against the developer's own
machine and the configured release backend.

Nothing here writes to disk, stops a process, or downloads anything. The
network check builds a real `Updater` and calls `.check()` - the same
read-only call `--check-only` makes - so "does this config work" is answered
by the real code path instead of a wizard-only approximation that could
drift from it.

No PySide6 import: this module is exercised directly by tests/test_setup_wizard.py
without the GUI extra installed, and the Qt thread wrapper around
test_connection() lives in workers.py instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ... import openpgp
from ... import process as process_module
from ...errors import UpdaterError
from ...updater import Updater


@dataclass
class CheckResult:
    """One pass/fail line for the Validate page."""

    ok: bool
    message: str
    detail: str = ""


@dataclass
class ConnectionResult:
    """The outcome of a live check against the configured release backend."""

    ok: bool
    message: str
    latest_version: str = ""
    asset_name: str = ""
    notes_preview: str = ""
    fingerprints: list = field(default_factory=list)


def check_path(path: str, kind: str = "file") -> CheckResult:
    """Existence check for app.executable / app.install_dir / a key file."""
    if not path:
        return CheckResult(True, "Not set (optional)")

    candidate = Path(path).expanduser()
    exists = candidate.is_file() if kind == "file" else candidate.is_dir()

    if exists:
        return CheckResult(True, f"Found: {candidate}")
    return CheckResult(False, f"Not found: {candidate}")


def check_regex(pattern: str) -> CheckResult:
    """Same compile check Config.validate() runs on assets.pattern etc."""
    if not pattern:
        return CheckResult(True, "Empty (matches everything)")

    try:
        re.compile(pattern)
    except re.error as exc:
        return CheckResult(False, f"Invalid regex: {exc}")

    return CheckResult(True, "Compiles")


def check_process_names(names) -> CheckResult:
    """Report which of the configured process names are running right now."""
    wanted = tuple(name for name in (names or ()) if name)
    if not wanted:
        return CheckResult(True, "No process names configured")

    try:
        found = process_module.find_processes(names=wanted)
    except UpdaterError as exc:
        return CheckResult(False, str(exc))

    if not found:
        return CheckResult(True, "None of these are running right now (that's fine)")

    live_names = set()
    for proc in found:
        try:
            live_names.add(proc.name())
        except Exception:  # noqa: BLE001, S112 - a process that vanished mid-scan is not an error
            continue

    return CheckResult(
        True,
        f"Currently running: {', '.join(sorted(live_names)) or f'{len(found)} process(es)'}",
    )


def check_public_key_file(path: str) -> CheckResult:
    """Parse a public key file and surface the real fingerprints it contains."""
    if not path:
        return CheckResult(True, "Not set (optional)")

    candidate = Path(path).expanduser()
    if not candidate.is_file():
        return CheckResult(False, f"Not found: {candidate}")

    try:
        data = candidate.read_bytes()
        keys = openpgp.parse_public_keys(data)
    except UpdaterError as exc:
        return CheckResult(False, f"Could not parse key file: {exc}")
    except OSError as exc:
        return CheckResult(False, f"Could not read key file: {exc}")

    fingerprints = [key.fingerprint_hex for key in keys if not key.is_subkey] or [
        key.fingerprint_hex for key in keys
    ]
    return CheckResult(
        True,
        f"{len(keys)} key(s) found",
        detail=", ".join(fingerprints),
    )


def test_connection_sync(config) -> ConnectionResult:
    """
    Run a real, read-only check against the configured release backend.

    Builds `Updater(config)` directly rather than going through
    `build_updater()`, which requires a fully valid config including
    `app.name` - the wizard wants to test connectivity while the developer is
    still filling in other sections.
    """
    try:
        updater = Updater(config)
        check = updater.check(fetch_notes=True)
    except UpdaterError as exc:
        return ConnectionResult(False, str(exc))
    except Exception as exc:  # noqa: BLE001 - surface anything unexpected as a failed check, not a crash
        return ConnectionResult(False, f"Unexpected error: {exc}")

    notes_preview = ""
    if check.notes and not check.notes.is_empty:
        notes_preview = check.notes.truncated(400)

    return ConnectionResult(
        ok=True,
        message=check.reason,
        latest_version=check.latest_version,
        asset_name=check.asset.name if check.asset else "",
        notes_preview=notes_preview,
    )

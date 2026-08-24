"""
Installer interface shared by every platform.

An Installer takes a verified artifact on disk and applies it. Each subclass
handles one family of artifact kinds and declares which it supports, so the
registry can pick one without any platform conditionals at the call site.

Two install modes exist throughout:

  silent       - unattended; no windows, no prompts, no user interaction
  interactive  - the real installer UI runs and the user clicks through it

Both are first-class. Silent is the default because the updater is usually
invoked by an application that has already asked the user, but a publisher
who wants users to see licence terms or component choices sets interactive
and gets exactly the vendor installer experience.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import InstallError


@dataclass
class InstallResult:
    """The outcome of applying one artifact."""

    success: bool
    method: str = ""                # human-readable description of what ran
    exit_code: int = 0
    output: str = ""
    installed_path: str = ""
    requires_restart: bool = False
    notes: list = field(default_factory=list)


class Installer:
    """Base class for artifact installers."""

    name = "base"
    supported_kinds = ()

    def __init__(self, config, host_platform, log=None):
        self.config = config
        self.host = host_platform
        self.log = log or (lambda message: None)

    def supports(self, kind: str) -> bool:
        return kind in self.supported_kinds

    def install(self, artifact: Path, kind: str) -> InstallResult:
        """Apply the artifact. Implemented by each platform installer."""
        raise NotImplementedError

    # --- helpers shared by subclasses ---

    @property
    def interactive(self) -> bool:
        """True when the user should walk through the vendor installer."""
        return (self.config.get("install.mode") or "silent").lower() == "interactive"

    def installer_args(self, kind: str) -> list:
        """
        Return the argument list for a given artifact kind and install mode.

        A publisher can override these per kind in config, which matters
        because installer flags are vendor-specific: Inno Setup, NSIS, and
        WiX all spell "be quiet" differently.
        """
        args = self.config.get("install.installer_args", {}) or {}

        key = f"{kind}_interactive" if self.interactive else kind
        value = args.get(key)

        if value is None and self.interactive:
            # No interactive override configured: run the installer bare, which
            # is what shows its normal UI.
            return []

        return list(value or [])

    def run(self, command: list, timeout: int = 3600, cwd: str = "") -> tuple:
        """
        Run an installer command, returning (exit_code, combined_output).

        Installers can legitimately take minutes, so the timeout is generous;
        it exists to stop a hung installer wedging the updater forever, not
        to bound normal operation.
        """
        self.log(f"Running: {' '.join(str(part) for part in command)}")

        try:
            result = subprocess.run(
                [str(part) for part in command],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd or None,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise InstallError(
                "The installer did not finish in time",
                f"timed out after {timeout} seconds",
            ) from exc
        except OSError as exc:
            raise InstallError(f"Could not run the installer: {command[0]}", str(exc)) from exc

        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return result.returncode, output

    def fail(self, message: str, exit_code: int, output: str) -> InstallResult:
        """Build a failed InstallResult and raise it as an InstallError."""
        detail = output[-600:] if output else f"exit code {exit_code}"
        raise InstallError(message, detail)

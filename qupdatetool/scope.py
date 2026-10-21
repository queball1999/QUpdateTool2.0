"""
Install scope: was the application installed for one user or for the machine?

The answer decides two things. Elevation: a per-user install updates with no
prompt, a machine-wide install needs administrator rights. And, for an
installer that can do either (Inno Setup's /CURRENTUSER and /ALLUSERS), which
mode to run it in. Getting the second wrong is worse than failing: a
per-user run over a machine-wide install leaves the old copy in place and
quietly creates a second one, which the application then never starts.

The scope is read, most reliable first, from:

  1. install.scope in config, or --install-scope (the application knows)
  2. the Windows uninstall entry (install.uninstall_key): HKLM means machine,
     HKCU means user, matched on InstallLocation when both exist
  3. the install folder: under Program Files means machine
  4. whether the install folder is writable without elevation
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

USER = "user"
MACHINE = "machine"
AUTO = "auto"
SCOPES = (AUTO, USER, MACHINE)


def uninstall_key(config) -> str:
    """The configured uninstall key; patches.windows_uninstall_key is the v0.0.3 name."""
    return (config.get("install.uninstall_key", "")
            or config.get("patches.windows_uninstall_key", "") or "")


def same_folder(left, right) -> bool:
    if not left or not right:
        return False
    try:
        return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))
    except OSError:
        return False


def read_uninstall_entries(key: str) -> dict:
    """Return {scope: InstallLocation} for each hive that holds the uninstall key (Windows only)."""
    if not key or not sys.platform.startswith("win"):
        return {}
    try:
        import winreg
    except ImportError:
        return {}

    found = {}
    for scope, hive in ((USER, winreg.HKEY_CURRENT_USER), (MACHINE, winreg.HKEY_LOCAL_MACHINE)):
        try:
            with winreg.OpenKey(hive, key) as handle:
                try:
                    location, _ = winreg.QueryValueEx(handle, "InstallLocation")
                except OSError:
                    location = ""
                found[scope] = str(location or "")
        except OSError:
            continue
    return found


def program_files_dirs() -> list:
    names = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")
    return [Path(os.environ[name]) for name in names if os.environ.get(name)]


def under(path: Path, parent: Path) -> bool:
    try:
        return os.path.normcase(str(path.resolve())).startswith(
            os.path.normcase(str(parent.resolve())).rstrip("\\/") + os.sep
        )
    except OSError:
        return False


def detect(config, os_family: str = "") -> str:
    """Return USER or MACHINE for the install the updater is about to change."""
    configured = (config.get("install.scope") or AUTO).lower()
    if configured in (USER, MACHINE):
        return configured

    install_dir = config.get("install.target_dir", "") or config.install_dir
    os_family = os_family or ("windows" if sys.platform.startswith("win") else "")

    if os_family == "windows":
        entries = read_uninstall_entries(uninstall_key(config))
        for scope, location in entries.items():
            if install_dir and same_folder(location, install_dir):
                return scope
        if len(entries) == 1 and not install_dir:
            return next(iter(entries))
        if install_dir and any(under(Path(install_dir), parent) for parent in program_files_dirs()):
            return MACHINE

    if install_dir:
        from .elevate import can_write, is_elevated

        # Elevated, everything is writable; that says nothing about the install.
        if not is_elevated() and not can_write(install_dir):
            return MACHINE

    return USER

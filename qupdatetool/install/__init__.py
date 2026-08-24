"""
Installer registry.

Picks the installer for the running platform and confirms it can handle the
artifact kind that was selected. Keeping the mapping here means the update
flow never branches on sys.platform.
"""

from __future__ import annotations

from ..errors import InstallError
from .base import InstallResult, Installer
from .linux import LinuxInstaller
from .macos import MacOSInstaller
from .windows import WindowsInstaller

INSTALLERS = {
    "windows": WindowsInstaller,
    "linux": LinuxInstaller,
    "macos": MacOSInstaller,
}

__all__ = [
    "Installer",
    "InstallResult",
    "WindowsInstaller",
    "LinuxInstaller",
    "MacOSInstaller",
    "INSTALLERS",
    "get_installer",
]


def get_installer(config, host_platform, log=None) -> Installer:
    """Return the installer for the detected platform."""
    installer_class = INSTALLERS.get(host_platform.os_family)

    if installer_class is None:
        raise InstallError(
            f"No installer available for {host_platform.os_family}",
            "Supported platforms are Windows, Linux, and macOS",
        )

    return installer_class(config, host_platform, log=log)

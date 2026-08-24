"""
Host platform detection and release-artifact preference ordering.

The updater has to work out, at runtime on the target machine, which artifact
from a release it should actually download. That answer depends on three
things: the OS family, the CPU architecture, and - on Linux especially - how
the application was installed in the first place.

A Debian box wants the .deb. A Fedora box wants the .rpm. A machine running
the app from an AppImage wants the new AppImage, regardless of distro. If a
release only ships one of those, the updater falls back down the preference
list rather than failing, so an application that ships only .deb today keeps
working unchanged when it starts shipping .rpm and .AppImage tomorrow.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Artifact kinds the updater knows how to install. Keep these stable: brand
# files and config.yaml reference them by name in `asset_preference`.
KIND_EXE_INSTALLER = "exe"
KIND_MSI = "msi"
KIND_DEB = "deb"
KIND_RPM = "rpm"
KIND_APPIMAGE = "appimage"
KIND_PKG = "pkg"
KIND_DMG = "dmg"
KIND_ZIP = "zip"
KIND_TARBALL = "tar"

# Filename suffixes that identify each kind. The longest matching suffix wins,
# so ".tar.gz" is not mistaken for a bare archive.
KIND_SUFFIXES = {
    KIND_MSI: (".msi",),
    KIND_DEB: (".deb",),
    KIND_RPM: (".rpm",),
    KIND_APPIMAGE: (".appimage",),
    KIND_PKG: (".pkg",),
    KIND_DMG: (".dmg",),
    KIND_EXE_INSTALLER: (".exe",),
    KIND_ZIP: (".zip",),
    KIND_TARBALL: (".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar.zst", ".tar"),
}

# Architecture aliases seen in release asset filenames, normalised to a
# canonical name so "amd64", "x64" and "x86_64" all match one host.
ARCH_ALIASES = {
    "x86_64": ("x86_64", "x86-64", "amd64", "x64", "win64", "64bit"),
    "aarch64": ("aarch64", "arm64", "armv8", "apple-silicon", "silicon"),
    "armv7l": ("armv7l", "armv7", "armhf", "arm32"),
    "i386": ("i386", "i686", "win32", "32bit"),
}

# Words in an asset filename that identify the OS it targets.
OS_ALIASES = {
    "windows": ("windows", "win32", "win64", "win", "msi", "exe"),
    "linux": ("linux", "deb", "rpm", "appimage", "ubuntu", "debian", "fedora"),
    "macos": ("macos", "mac", "osx", "darwin", "dmg", "apple"),
}

# Characters stripped from os-release values, built without literal quote
# characters so the module stays easy to embed in generated build scripts.
QUOTE_CHARS = chr(34) + chr(39)


@dataclass
class HostPlatform:
    """A description of the machine the updater is running on."""

    os_family: str                      # "windows" | "linux" | "macos"
    arch: str                           # canonical architecture name
    distro_id: str = ""                 # Linux only: "ubuntu", "fedora", ...
    distro_like: tuple = ()             # Linux only: ID_LIKE families
    package_manager: str = ""           # "apt" | "dnf" | "zypper" | "pacman"
    running_as_appimage: bool = False
    running_frozen: bool = False
    preference: list = field(default_factory=list)

    def describe(self) -> str:
        parts = [self.os_family, self.arch]
        if self.distro_id:
            parts.append(self.distro_id)
        if self.running_as_appimage:
            parts.append("appimage")
        return "/".join(parts)


def detect(app_path: str | os.PathLike | None = None) -> HostPlatform:
    """
    Inspect the running machine and return a HostPlatform.

    `app_path` is the install location of the parent application when known;
    on Linux it is used to notice an AppImage-style deployment even when the
    APPIMAGE environment variable is not inherited by the updater.
    """
    system = sys.platform
    if system.startswith("win"):
        return detect_windows()
    if system == "darwin":
        return detect_macos(app_path)
    return detect_linux(app_path)


def normalise_arch(text: str) -> str:
    """Map a raw architecture string onto a canonical name."""
    lowered = (text or "").lower()
    for canonical, aliases in ARCH_ALIASES.items():
        if lowered in aliases:
            return canonical
    return lowered or "x86_64"


def host_arch() -> str:
    """Return the canonical architecture name for this machine."""
    return normalise_arch(platform.machine())


def detect_windows() -> HostPlatform:
    """Windows prefers a real installer so uninstall bookkeeping stays intact."""
    return HostPlatform(
        os_family="windows",
        arch=host_arch(),
        running_frozen=getattr(sys, "frozen", False),
        preference=[KIND_EXE_INSTALLER, KIND_MSI, KIND_ZIP],
    )


def detect_macos(app_path=None) -> HostPlatform:
    """
    macOS prefers a disk image, then a signed package, then a zipped bundle.

    A .app running from /Applications updates cleanly from any of the three;
    the dmg leads because it is what most projects sign and notarize.
    """
    return HostPlatform(
        os_family="macos",
        arch=host_arch(),
        running_frozen=getattr(sys, "frozen", False),
        preference=[KIND_DMG, KIND_PKG, KIND_ZIP, KIND_TARBALL],
    )


def detect_linux(app_path=None) -> HostPlatform:
    """
    Work out which Linux packaging this machine should be fed.

    An AppImage deployment wins outright - swapping the AppImage in place is
    correct no matter what the host distro is. Otherwise the native package
    format for the detected distro family leads, with AppImage and a plain
    tarball behind it so a release that has not started shipping .rpm yet
    still resolves to something installable.
    """
    distro_id, distro_like = read_os_release()
    package_manager = detect_package_manager()
    as_appimage = running_as_appimage(app_path)

    if as_appimage:
        preference = [KIND_APPIMAGE, KIND_TARBALL, KIND_ZIP]
    elif package_manager == "apt" or in_family(distro_id, distro_like, ("debian", "ubuntu")):
        preference = [KIND_DEB, KIND_APPIMAGE, KIND_TARBALL, KIND_ZIP]
    elif package_manager in ("dnf", "yum", "zypper") or in_family(
        distro_id, distro_like, ("fedora", "rhel", "centos", "suse", "opensuse")
    ):
        preference = [KIND_RPM, KIND_APPIMAGE, KIND_TARBALL, KIND_ZIP]
    else:
        # Arch, NixOS, Gentoo, unknown: no native format we can install
        # unattended, so a self-contained AppImage or tarball is the safe pick.
        preference = [KIND_APPIMAGE, KIND_TARBALL, KIND_ZIP, KIND_DEB, KIND_RPM]

    return HostPlatform(
        os_family="linux",
        arch=host_arch(),
        distro_id=distro_id,
        distro_like=distro_like,
        package_manager=package_manager,
        running_as_appimage=as_appimage,
        running_frozen=getattr(sys, "frozen", False),
        preference=preference,
    )


def in_family(distro_id: str, distro_like: tuple, names: tuple) -> bool:
    """Return True when a distro ID or one of its ID_LIKE families is in `names`."""
    if distro_id in names:
        return True
    return any(like in names for like in distro_like)


def read_os_release() -> tuple:
    """
    Parse /etc/os-release for the distro ID and its ID_LIKE families.

    Returns ("", ()) on any machine where the file is missing or unreadable,
    which the caller treats as an unknown distro.
    """
    for candidate in ("/etc/os-release", "/usr/lib/os-release"):
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            values = {}
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "=" not in line or line.startswith("#"):
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip(QUOTE_CHARS)

            distro_id = values.get("ID", "").lower()
            like_text = values.get("ID_LIKE", "").lower()
            distro_like = tuple(part for part in like_text.split() if part)
            return distro_id, distro_like
        except OSError:
            continue

    return "", ()


def detect_package_manager() -> str:
    """Return the first system package manager found on PATH."""
    for name in ("apt-get", "dnf", "yum", "zypper", "pacman"):
        if shutil.which(name):
            return "apt" if name == "apt-get" else name
    return ""


def running_as_appimage(app_path=None) -> bool:
    """
    Detect an AppImage deployment.

    AppImage sets APPIMAGE/APPDIR for the process it launches. Those may not
    reach the updater if it is spawned differently, so the path of the parent
    application is checked as a second signal.
    """
    if os.environ.get("APPIMAGE") or os.environ.get("APPDIR"):
        return True
    if app_path:
        return str(app_path).lower().endswith(".appimage")
    return False


def classify_asset(filename: str) -> str:
    """
    Return the artifact kind for an asset filename, or "" if unrecognised.

    The longest matching suffix wins, so ".tar.gz" beats ".tar" and an
    ".AppImage" is never swallowed by a more generic rule.
    """
    lowered = (filename or "").lower()

    matches = []
    for kind, suffixes in KIND_SUFFIXES.items():
        for suffix in suffixes:
            if lowered.endswith(suffix):
                matches.append((len(suffix), kind))

    if not matches:
        return ""

    matches.sort(reverse=True)
    return matches[0][1]


def asset_matches_os(filename: str, os_family: str) -> bool:
    """
    Return True when a filename does not clearly target a different OS.

    This is deliberately permissive: an asset with no OS word in its name
    (common for single-platform projects) is treated as a match, and only an
    explicit mention of a different OS rules it out.
    """
    lowered = (filename or "").lower()
    own_words = OS_ALIASES.get(os_family, ())

    if any(word in lowered for word in own_words):
        return True

    for other_family, words in OS_ALIASES.items():
        if other_family == os_family:
            continue
        if any(word in lowered for word in words):
            return False

    return True


def asset_matches_arch(filename: str, arch: str) -> bool:
    """
    Return True when a filename does not clearly target a different arch.

    Same permissive rule as the OS check - an unqualified filename matches.
    """
    lowered = (filename or "").lower()
    own_aliases = ARCH_ALIASES.get(arch, (arch,))

    if any(alias in lowered for alias in own_aliases):
        return True

    for other_arch, aliases in ARCH_ALIASES.items():
        if other_arch == arch:
            continue
        if any(alias in lowered for alias in aliases):
            return False

    return True

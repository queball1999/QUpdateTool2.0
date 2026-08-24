"""
Platform detection and artifact selection tests.

These cover the behaviour the cross-platform requirement depends on: that a
single release carrying Windows, Debian, Fedora, AppImage, and macOS
artifacts resolves to the right one on each host, and keeps working when a
project starts publishing formats it did not publish before.
"""

from __future__ import annotations

import pytest

from qupdatetool import platforms

# The asset list from a release that ships everything.
FULL_RELEASE = [
    "MyApp-1.2.3-windows-installer.exe",
    "MyApp-1.2.3-windows-portable.zip",
    "MyApp-1.2.3-win64.msi",
    "myapp_1.2.3_amd64.deb",
    "MyApp-1.2.3.x86_64.rpm",
    "MyApp-1.2.3-x86_64.AppImage",
    "MyApp-1.2.3-macos.dmg",
    "MyApp-1.2.3-macos.pkg",
    "MyApp-1.2.3-linux-x86_64.tar.gz",
    "SHA256SUMS.txt",
]


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("MyApp-1.2.3-windows-installer.exe", platforms.KIND_EXE_INSTALLER),
        ("MyApp-1.2.3-win64.msi", platforms.KIND_MSI),
        ("myapp_1.2.3_amd64.deb", platforms.KIND_DEB),
        ("MyApp-1.2.3.x86_64.rpm", platforms.KIND_RPM),
        ("MyApp-1.2.3-x86_64.AppImage", platforms.KIND_APPIMAGE),
        ("MyApp-1.2.3-macos.dmg", platforms.KIND_DMG),
        ("MyApp-1.2.3-macos.pkg", platforms.KIND_PKG),
        ("MyApp-1.2.3-windows-portable.zip", platforms.KIND_ZIP),
        ("MyApp-1.2.3-linux-x86_64.tar.gz", platforms.KIND_TARBALL),
        ("MyApp-1.2.3.tar.xz", platforms.KIND_TARBALL),
        ("SHA256SUMS.txt", ""),
    ],
)
def test_classify_asset(filename, expected):
    """Each artifact filename is classified into the right kind."""
    assert platforms.classify_asset(filename) == expected


def test_longest_suffix_wins():
    """A .tar.gz is a tarball, not confused with a bare archive."""
    assert platforms.classify_asset("app.tar.gz") == platforms.KIND_TARBALL
    assert platforms.classify_asset("app.tar") == platforms.KIND_TARBALL


@pytest.mark.parametrize(
    "filename,family,expected",
    [
        ("MyApp-windows-installer.exe", "windows", True),
        ("MyApp-windows-installer.exe", "linux", False),
        ("myapp_1.0_amd64.deb", "linux", True),
        ("myapp_1.0_amd64.deb", "windows", False),
        ("MyApp-macos.dmg", "macos", True),
        ("MyApp-macos.dmg", "linux", False),
        # An unqualified name matches every platform: many projects ship one.
        ("MyApp-1.2.3.zip", "windows", True),
        ("MyApp-1.2.3.zip", "linux", True),
    ],
)
def test_asset_matches_os(filename, family, expected):
    """OS matching is permissive for unqualified names, strict otherwise."""
    assert platforms.asset_matches_os(filename, family) is expected


def test_asset_matches_arch():
    """Architecture matching handles the common aliases."""
    assert platforms.asset_matches_arch("app-x86_64.AppImage", "x86_64")
    assert platforms.asset_matches_arch("app-amd64.deb", "x86_64")
    assert platforms.asset_matches_arch("app-arm64.dmg", "aarch64")

    assert not platforms.asset_matches_arch("app-arm64.dmg", "x86_64")
    assert not platforms.asset_matches_arch("app-x86_64.deb", "aarch64")

    # Unqualified names match anything.
    assert platforms.asset_matches_arch("app.deb", "x86_64")
    assert platforms.asset_matches_arch("app.deb", "aarch64")


def test_windows_prefers_installer():
    """Windows takes the installer over the portable zip."""
    host = platforms.detect_windows()
    assert host.preference[0] == platforms.KIND_EXE_INSTALLER
    assert platforms.KIND_ZIP in host.preference


def test_macos_prefers_dmg():
    """macOS takes the disk image first."""
    host = platforms.detect_macos()
    assert host.preference[0] == platforms.KIND_DMG
    assert platforms.KIND_PKG in host.preference


def test_debian_prefers_deb(monkeypatch):
    """A Debian-family host takes the .deb."""
    monkeypatch.setattr(platforms, "read_os_release", lambda: ("ubuntu", ("debian",)))
    monkeypatch.setattr(platforms, "detect_package_manager", lambda: "apt")
    monkeypatch.setattr(platforms, "running_as_appimage", lambda path=None: False)

    host = platforms.detect_linux()

    assert host.preference[0] == platforms.KIND_DEB
    # AppImage remains a fallback for when no .deb is published.
    assert platforms.KIND_APPIMAGE in host.preference


def test_fedora_prefers_rpm(monkeypatch):
    """A Fedora-family host takes the .rpm from the very same release."""
    monkeypatch.setattr(platforms, "read_os_release", lambda: ("fedora", ()))
    monkeypatch.setattr(platforms, "detect_package_manager", lambda: "dnf")
    monkeypatch.setattr(platforms, "running_as_appimage", lambda path=None: False)

    host = platforms.detect_linux()

    assert host.preference[0] == platforms.KIND_RPM


def test_suse_prefers_rpm(monkeypatch):
    """openSUSE is recognised through ID_LIKE."""
    monkeypatch.setattr(platforms, "read_os_release", lambda: ("opensuse-leap", ("suse",)))
    monkeypatch.setattr(platforms, "detect_package_manager", lambda: "zypper")
    monkeypatch.setattr(platforms, "running_as_appimage", lambda path=None: False)

    host = platforms.detect_linux()

    assert host.preference[0] == platforms.KIND_RPM


def test_appimage_deployment_wins_over_distro(monkeypatch):
    """Running from an AppImage takes the AppImage, whatever the distro is."""
    monkeypatch.setattr(platforms, "read_os_release", lambda: ("ubuntu", ("debian",)))
    monkeypatch.setattr(platforms, "detect_package_manager", lambda: "apt")
    monkeypatch.setattr(platforms, "running_as_appimage", lambda path=None: True)

    host = platforms.detect_linux()

    assert host.preference[0] == platforms.KIND_APPIMAGE


def test_unknown_distro_prefers_self_contained(monkeypatch):
    """Arch or NixOS gets a self-contained format rather than a native package."""
    monkeypatch.setattr(platforms, "read_os_release", lambda: ("arch", ()))
    monkeypatch.setattr(platforms, "detect_package_manager", lambda: "pacman")
    monkeypatch.setattr(platforms, "running_as_appimage", lambda path=None: False)

    host = platforms.detect_linux()

    assert host.preference[0] == platforms.KIND_APPIMAGE
    assert host.preference[1] == platforms.KIND_TARBALL


def test_appimage_detected_from_executable_path():
    """An AppImage deployment is spotted from the parent executable path."""
    assert platforms.running_as_appimage("/opt/MyApp-1.2.3-x86_64.AppImage")
    assert not platforms.running_as_appimage("/usr/bin/myapp")


def test_release_resolves_per_platform():
    """
    One release with every artifact resolves correctly on each host.

    This is the end-to-end expression of the cross-platform requirement: the
    same asset list, filtered by each host, yields exactly one right answer.
    """
    expectations = {
        "windows": ("MyApp-1.2.3-windows-installer.exe", platforms.KIND_EXE_INSTALLER),
        "linux": ("myapp_1.2.3_amd64.deb", platforms.KIND_DEB),
        "macos": ("MyApp-1.2.3-macos.dmg", platforms.KIND_DMG),
    }

    preferences = {
        "windows": platforms.detect_windows().preference,
        "linux": [platforms.KIND_DEB, platforms.KIND_APPIMAGE, platforms.KIND_TARBALL],
        "macos": platforms.detect_macos().preference,
    }

    for family, (expected_name, expected_kind) in expectations.items():
        candidates = [
            name for name in FULL_RELEASE
            if platforms.classify_asset(name)
            and platforms.asset_matches_os(name, family)
            and platforms.asset_matches_arch(name, "x86_64")
        ]

        ranked = sorted(
            (preferences[family].index(platforms.classify_asset(name)), name)
            for name in candidates
            if platforms.classify_asset(name) in preferences[family]
        )

        assert ranked, f"nothing resolved for {family}"
        assert ranked[0][1] == expected_name
        assert platforms.classify_asset(ranked[0][1]) == expected_kind

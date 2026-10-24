"""
Architecture matching tests for Raspberry Pi and 64-bit Linux releases.

A Pi Zero W reports armv6l and must take an armv6 asset; a Pi Zero 2 W on a
32-bit OS reports armv7l and may fall back to an armv6 asset when the release
ships no armv7 one. The 64-bit names (arm64, amd64) must keep matching.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from qupdatetool import config as config_module
from qupdatetool import platforms
from qupdatetool.updater import Updater

ASSETS = [
    "pistation-v1.2.3-linux-armv6.tar.gz",
    "pistation-v1.2.3-linux-arm64.tar.gz",
    "pistation-v1.2.3-linux-amd64.tar.gz",
]


@pytest.mark.parametrize(
    "machine,expected",
    [
        ("armv6l", "armv6l"),
        ("armv6", "armv6l"),
        ("armel", "armv6l"),
        ("armv6hf", "armv6l"),
        ("armv7l", "armv7l"),
        ("armhf", "armv7l"),
        ("aarch64", "aarch64"),
        ("arm64", "aarch64"),
        ("x86_64", "x86_64"),
        ("amd64", "x86_64"),
    ],
)
def test_normalise_arch(machine, expected):
    """Raw machine strings map onto one canonical name each."""
    assert platforms.normalise_arch(machine) == expected


@pytest.mark.parametrize(
    "asset,arch,expected",
    [
        ("pistation-v1.2.3-linux-armv6.tar.gz", "armv6l", True),
        ("pistation-v1.2.3-linux-armv6.tar.gz", "armv7l", False),
        ("pistation-v1.2.3-linux-armv6.tar.gz", "aarch64", False),
        ("pistation-v1.2.3-linux-armv6.tar.gz", "x86_64", False),
        ("pistation-v1.2.3-linux-armv7.tar.gz", "armv6l", False),
        ("pistation-v1.2.3-linux-armv7.tar.gz", "armv7l", True),
        ("pistation-v1.2.3-linux-arm64.tar.gz", "aarch64", True),
        ("pistation-v1.2.3-linux-arm64.tar.gz", "armv6l", False),
        ("pistation-v1.2.3-linux-arm64.tar.gz", "x86_64", False),
        ("pistation-v1.2.3-linux-amd64.tar.gz", "x86_64", True),
        ("pistation-v1.2.3-linux-amd64.tar.gz", "aarch64", False),
        ("pistation-v1.2.3-linux-amd64.tar.gz", "armv6l", False),
        ("pistation-v1.2.3-linux-aarch64.tar.gz", "aarch64", True),
        ("pistation-v1.2.3-linux-x86_64.tar.gz", "x86_64", True),
    ],
)
def test_asset_matches_arch_table(asset, arch, expected):
    """The asset-name versus host-architecture matching table."""
    assert platforms.asset_matches_arch(asset, arch) is expected


def test_pistation_asset_is_a_linux_tarball():
    """The PiStation asset name classifies as a Linux tarball."""
    name = "pistation-v1.2.3-linux-armv6.tar.gz"
    assert platforms.classify_asset(name) == platforms.KIND_TARBALL
    assert platforms.asset_matches_os(name, "linux")
    assert not platforms.asset_matches_os(name, "windows")


def test_arch_chain_falls_back_only_from_armv7_to_armv6():
    """armv7l may run armv6 builds; nothing else falls back."""
    assert platforms.arch_chain("armv7l") == ["armv7l", "armv6l"]
    assert platforms.arch_chain("armv6l") == ["armv6l"]
    assert platforms.arch_chain("aarch64") == ["aarch64"]


def select(arch: str, names: list) -> str:
    """Run Updater.select_asset for a fake Linux host and return the asset name."""
    updater = Updater.__new__(Updater)
    updater.config = config_module.Config(config_module.deep_merge(config_module.DEFAULTS, {}))
    updater.host = SimpleNamespace(
        os_family="linux", arch=arch,
        preference=[platforms.KIND_DEB, platforms.KIND_TARBALL],
        describe=lambda: f"linux/{arch}",
    )
    release = SimpleNamespace(
        tag="v1.2.3",
        assets=[SimpleNamespace(name=name, size=1, url="") for name in names],
    )
    asset, _ = updater.select_asset(release)
    return asset.name


def test_zero_w_selects_armv6():
    """A Pi Zero W (armv6l) takes the armv6 asset from a mixed release."""
    assert select("armv6l", ASSETS) == "pistation-v1.2.3-linux-armv6.tar.gz"


def test_zero_2_w_falls_back_to_armv6_when_no_armv7_asset():
    """A 32-bit OS on a Zero 2 W (armv7l) falls back to the armv6 asset."""
    assert select("armv7l", ASSETS) == "pistation-v1.2.3-linux-armv6.tar.gz"


def test_zero_2_w_prefers_armv7_when_published():
    """The armv7 asset wins over the armv6 fallback when it exists."""
    names = [*ASSETS, "pistation-v1.2.3-linux-armv7.tar.gz"]
    assert select("armv7l", names) == "pistation-v1.2.3-linux-armv7.tar.gz"


def test_armv6_host_never_takes_armv7_asset():
    """An ARMv7 build does not run on ARMv6, so it is never offered there."""
    names = ["pistation-v1.2.3-linux-armv7.tar.gz", "pistation-v1.2.3-linux-armv6.tar.gz"]
    assert select("armv6l", names) == "pistation-v1.2.3-linux-armv6.tar.gz"


@pytest.mark.parametrize(
    "arch,expected",
    [
        ("aarch64", "pistation-v1.2.3-linux-arm64.tar.gz"),
        ("x86_64", "pistation-v1.2.3-linux-amd64.tar.gz"),
    ],
)
def test_64_bit_hosts_select_their_asset(arch, expected):
    """arm64 and amd64 hosts pick the matching asset names."""
    assert select(arch, ASSETS) == expected

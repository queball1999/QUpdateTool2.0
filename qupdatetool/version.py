"""
Version parsing and comparison.

Release tags in the wild are messy: "v1.2.3", "1.2.3-windows", "2.0.0-rc1",
"0.0.8-dev". This module normalises them into a comparable tuple so the
updater can decide whether a release is actually newer than what is running.

Comparison follows semantic versioning precedence rules, including the rule
that a pre-release sorts before its own release ("1.0.0-rc1" < "1.0.0").
Build metadata after "+" is ignored for precedence, as SemVer requires.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Tag prefixes and platform suffixes that carry no version meaning. These are
# stripped before parsing so "v1.2.3-windows" compares equal to "1.2.3".
PLATFORM_SUFFIXES = (
    "windows", "win", "win32", "win64",
    "linux", "deb", "rpm", "appimage",
    "macos", "mac", "osx", "darwin",
    "release", "stable",
    "x86_64", "amd64", "arm64", "aarch64", "x64", "i386",
)

VERSION_RE = re.compile(
    r"^(?P<major>\d+)"
    r"(?:\.(?P<minor>\d+))?"
    r"(?:\.(?P<patch>\d+))?"
    r"(?:[-.](?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


class Version(NamedTuple):
    """A parsed version. `raw` keeps the original string for display."""

    major: int
    minor: int
    patch: int
    prerelease: tuple
    raw: str

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def __str__(self) -> str:
        return self.raw


def strip_platform_suffix(tag: str) -> str:
    """
    Remove leading "v" and trailing platform/channel words from a release tag.

    "v0.0.8-release" becomes "0.0.8". A suffix is only dropped when the whole
    dash-separated segment is a known platform word, so a real pre-release
    label like "rc1" survives.
    """
    text = tag.strip()
    if text[:1].lower() == "v" and text[1:2].isdigit():
        text = text[1:]

    parts = text.split("-")
    while len(parts) > 1 and parts[-1].lower() in PLATFORM_SUFFIXES:
        parts.pop()

    return "-".join(parts)


def parse(text: str) -> Version | None:
    """
    Parse a version string into a Version, or return None if unparseable.

    Missing minor/patch components default to 0, so "2" parses as 2.0.0.
    """
    if not text:
        return None

    raw = text.strip()
    cleaned = strip_platform_suffix(raw)
    match = VERSION_RE.match(cleaned)
    if not match:
        return None

    prerelease_text = match.group("prerelease") or ""
    prerelease = tuple(
        _prerelease_part(part) for part in prerelease_text.split(".") if part
    )

    return Version(
        major=int(match.group("major")),
        minor=int(match.group("minor") or 0),
        patch=int(match.group("patch") or 0),
        prerelease=prerelease,
        raw=raw,
    )


def _prerelease_part(part: str):
    """
    Convert one dot-separated pre-release identifier into a sortable value.

    SemVer sorts numeric identifiers below alphanumeric ones, so numbers are
    tagged with 0 and strings with 1 to force that ordering in tuple compare.

    One deliberate deviation from strict SemVer: a trailing number on an
    alphanumeric identifier is split off and compared numerically, so "rc10"
    sorts after "rc9". Strict SemVer compares those as ASCII strings and
    concludes rc10 is the older of the two, which for an updater means
    silently failing to offer a release that is plainly newer. Projects
    tagging "rc9" and "rc10" are common enough that being right about them
    matters more than matching the letter of a spec they were not following.

    The tuple is (kind, prefix, number): the alphabetic prefix is compared
    before the trailing number, so "beta2" still sorts below "rc1" rather
    than being lifted above it by the larger digit.
    """
    if part.isdigit():
        return (0, "", int(part))

    match = re.match(r"^(.*?)(\d+)$", part)
    if match:
        prefix, digits = match.groups()
        return (1, prefix, int(digits))

    return (1, part, 0)


def compare(left: str, right: str) -> int:
    """
    Compare two version strings. Returns -1, 0, or 1.

    Unparseable versions fall back to a case-insensitive string comparison so
    the tool degrades predictably rather than crashing on an odd tag.
    """
    left_version = parse(left)
    right_version = parse(right)

    if left_version is None or right_version is None:
        left_text = (left or "").lower()
        right_text = (right or "").lower()
        return (left_text > right_text) - (left_text < right_text)

    left_core = (left_version.major, left_version.minor, left_version.patch)
    right_core = (right_version.major, right_version.minor, right_version.patch)
    if left_core != right_core:
        return 1 if left_core > right_core else -1

    # Equal cores: a version with no pre-release outranks one that has it.
    if not left_version.prerelease and right_version.prerelease:
        return 1
    if left_version.prerelease and not right_version.prerelease:
        return -1
    if left_version.prerelease == right_version.prerelease:
        return 0

    return 1 if left_version.prerelease > right_version.prerelease else -1


def is_newer(candidate: str, current: str) -> bool:
    """Return True when `candidate` is a strictly newer version than `current`."""
    return compare(candidate, current) > 0

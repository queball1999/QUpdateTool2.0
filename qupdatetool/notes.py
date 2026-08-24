"""
Release notes: working out what to show the user as "what is new".

Four sources are tried, in this order, and the first that produces anything
wins:

  notices    a per-version YAML file in a notices/ folder in the repository,
             which many projects already publish and is the richest option
             because it is written for end users
  changelog  the section of CHANGELOG.md matching the new version
  release    the release body as published on the git host
  none       skip notes entirely

"auto" tries notices, then changelog, then the release body. That ordering
means a project gets good notes for free if it already keeps them in the
repository, and still gets something useful if it does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import version as version_module

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is a hard dependency
    yaml = None

# Matches a markdown heading that introduces a version section, e.g.
# "## [1.2.3] - 2026-08-01" or "## v1.2.3".
CHANGELOG_HEADING = re.compile(r"^(#{1,4})\s*\[?v?(\d+\.\d+(?:\.\d+)?[^\]\s]*)\]?", re.MULTILINE)


@dataclass
class ReleaseNotes:
    """Notes for one release, ready to display."""

    title: str = ""
    body: str = ""
    source: str = ""            # which mechanism produced these notes
    url: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.body or "").strip()

    def truncated(self, limit: int = 20000) -> str:
        """Return the body, cut to a maximum length at a line boundary."""
        body = (self.body or "").strip()
        if len(body) <= limit:
            return body

        clipped = body[:limit]
        last_newline = clipped.rfind("\n")
        if last_newline > limit // 2:
            clipped = clipped[:last_newline]

        return clipped.rstrip() + "\n\n..."


def fetch(config, backend, release, log=None) -> ReleaseNotes:
    """
    Fetch release notes for `release` using the configured source.

    Never raises: notes are informational, and an update must not fail
    because a CHANGELOG could not be read. Failures return empty notes and
    are logged at debug level.
    """
    source = (config.get("notes.source") or "auto").lower()

    if source == "none":
        return ReleaseNotes()

    def note(message):
        if log:
            log(message)

    attempts = {
        "notices": [fetch_from_notices],
        "changelog": [fetch_from_changelog],
        "release": [fetch_from_release_body],
        "auto": [fetch_from_notices, fetch_from_changelog, fetch_from_release_body],
    }.get(source, [fetch_from_release_body])

    for attempt in attempts:
        try:
            notes = attempt(config, backend, release)
        except Exception as exc:  # notes are never worth failing an update over
            note(f"Could not read release notes ({attempt.__name__}): {exc}")
            continue

        if notes and not notes.is_empty:
            note(f"Release notes loaded from {notes.source}")
            limit = int(config.get("notes.max_length", 20000))
            notes.body = notes.truncated(limit)
            return notes

    note("No release notes found")
    return ReleaseNotes(title=release.name or release.tag, url=release.html_url)


def fetch_from_notices(config, backend, release) -> ReleaseNotes | None:
    """
    Read a notice YAML file for this version out of the repository.

    The format looks like this:

        id: "v0.0.8"
        title: "MyApp 0.0.8"
        message: |
          ### Section
          - bullet

    Both the active notices/ folder and its history/ subfolder are searched,
    because a release moves from one to the other over time.
    """
    if yaml is None:
        return None

    directory = config.get("notes.notices_dir", "notices")
    ref = config.get("notes.branch", "") or release.tag

    target = version_module.parse(release.tag)

    for search_dir in (directory, f"{directory.rstrip('/')}/history"):
        filenames = backend.list_directory(search_dir, ref=ref)

        if not filenames:
            continue

        for filename in filenames:
            if not filename.lower().endswith((".yaml", ".yml")):
                continue

            # Match the notice whose version matches this release, so a
            # folder holding every past notice still resolves to one file.
            stem = filename.rsplit(".", 1)[0]
            candidate = version_module.parse(stem.replace("-notice", ""))

            if target and candidate and version_module.compare(
                candidate.raw, target.raw
            ) != 0:
                continue
            if not target and release.tag.lower() not in filename.lower():
                continue

            text = backend.fetch_text_file(f"{search_dir}/{filename}", ref=ref)
            if not text:
                continue

            data = yaml.safe_load(text) or {}
            if not isinstance(data, dict):
                continue

            body = str(data.get("message", "") or "")
            if not body.strip():
                continue

            return ReleaseNotes(
                title=str(data.get("title", "") or release.name or release.tag),
                body=body,
                source=f"{search_dir}/{filename}",
                url=release.html_url,
            )

    return None


def fetch_from_changelog(config, backend, release) -> ReleaseNotes | None:
    """
    Extract the section of a CHANGELOG that describes this release.

    Only the matching version section is returned, not the whole file, since
    showing a user every release since 1.0 is not "what is new".
    """
    ref = config.get("notes.branch", "") or release.tag
    paths = config.get("notes.changelog_paths", []) or ["CHANGELOG.md"]

    for path in paths:
        text = backend.fetch_text_file(path, ref=ref)
        if not text:
            continue

        section = extract_changelog_section(text, release.tag)
        if section:
            return ReleaseNotes(
                title=release.name or release.tag,
                body=section,
                source=path,
                url=release.html_url,
            )

    return None


def extract_changelog_section(text: str, tag: str) -> str:
    """
    Return the body of the changelog section matching `tag`.

    Falls back to the first section in the file when no heading matches the
    tag exactly, which covers changelogs whose newest section is headed
    "Unreleased" at the moment a release is cut.
    """
    target = version_module.parse(tag)
    headings = list(CHANGELOG_HEADING.finditer(text))

    if not headings:
        return ""

    chosen = None
    for index, match in enumerate(headings):
        heading_version = version_module.parse(match.group(2))
        if target and heading_version and version_module.compare(
            heading_version.raw, target.raw
        ) == 0:
            chosen = index
            break

    if chosen is None:
        chosen = 0

    start = headings[chosen].end()
    end = headings[chosen + 1].start() if chosen + 1 < len(headings) else len(text)

    return text[start:end].strip()


def fetch_from_release_body(config, backend, release) -> ReleaseNotes | None:
    """Use the release description as published on the git host."""
    body = (release.body or "").strip()
    if not body:
        return None

    return ReleaseNotes(
        title=release.name or release.tag,
        body=body,
        source="release description",
        url=release.html_url,
    )

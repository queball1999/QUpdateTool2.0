"""
Generic release backend for hosts that are not a supported git platform.

Two shapes are supported. A JSON manifest published anywhere reachable over
HTTPS gives full metadata, and is the recommended option for a self-hosted
download server. A bare download URL covers the simplest case, where the
publisher just drops a file at a stable location.

Manifest format:

    {
      "version": "1.2.3",
      "notes": "What changed in this release",
      "notes_url": "https://example.com/notes/1.2.3.md",
      "published_at": "2026-08-23T12:00:00Z",
      "prerelease": false,
      "assets": [
        {
          "name": "MyApp-1.2.3-windows-installer.exe",
          "url": "https://example.com/dl/MyApp-1.2.3-windows-installer.exe",
          "size": 48210944,
          "sha256": "..."
        }
      ]
    }
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse

from ..errors import BackendError, ConfigError
from .base import Asset, Release, ReleaseBackend


class GenericBackend(ReleaseBackend):
    """Release source backed by a static JSON manifest or a single URL."""

    name = "Generic"

    def __init__(self, config):
        super().__init__(config)
        self.manifest_url = config.get("source.manifest_url", "")
        self.download_url = config.get("source.download_url", "")

        if not self.manifest_url and not self.download_url:
            raise ConfigError(
                "The generic provider needs a manifest URL or a download URL"
            )

    def fetch_releases(self, limit: int = 30) -> list:
        """Return the single release described by the manifest or download URL."""
        if self.manifest_url:
            return self.fetch_from_manifest()
        return self.fetch_from_download_url()

    def fetch_from_manifest(self) -> list:
        """Parse a JSON manifest into one or more releases."""
        payload = self.request_json(self.manifest_url)

        # A manifest may be a single release object or a list of them.
        if isinstance(payload, dict) and isinstance(payload.get("releases"), list):
            items = payload["releases"]
        elif isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = [payload]
        else:
            raise BackendError("Manifest was not a JSON object or array")

        releases = [self.parse_manifest_release(item) for item in items]

        # Newest first, so latest_release() picks correctly even when the
        # publisher lists them oldest-first.
        from .. import version as version_module

        releases.sort(
            key=lambda release: version_module.parse(release.tag)
            or version_module.Version(0, 0, 0, (), release.tag),
            reverse=True,
        )
        return releases

    def parse_manifest_release(self, item: dict) -> Release:
        """Convert one manifest entry into a Release."""
        if not isinstance(item, dict):
            raise BackendError("Manifest entry was not an object")

        tag = str(item.get("version") or item.get("tag") or "")
        if not tag:
            raise BackendError("Manifest entry has no version")

        assets = []
        for raw in item.get("assets") or []:
            name = raw.get("name") or self.filename_from_url(raw.get("url", ""))
            assets.append(
                Asset(
                    name=name,
                    url=raw.get("url", ""),
                    size=int(raw.get("size") or 0),
                    digest=raw.get("sha256", "") or raw.get("digest", "") or "",
                )
            )

        return Release(
            tag=tag,
            name=item.get("name", "") or tag,
            body=item.get("notes", "") or item.get("body", "") or "",
            prerelease=bool(item.get("prerelease")),
            draft=bool(item.get("draft")),
            published_at=item.get("published_at", "") or "",
            html_url=item.get("url", "") or item.get("notes_url", "") or "",
            assets=assets,
        )

    def fetch_from_download_url(self) -> list:
        """
        Build a synthetic release around a single download URL.

        With no version metadata available, the configured version is echoed
        back so the update always looks newer; this mode is for publishers who
        want the updater to fetch and install unconditionally.
        """
        name = self.filename_from_url(self.download_url)
        tag = self.config.get("source.channel_version", "") or "0.0.0+direct"

        asset = Asset(name=name, url=self.download_url)

        return [
            Release(
                tag=tag,
                name=name,
                body="",
                prerelease=False,
                draft=False,
                assets=[asset],
            )
        ]

    def latest_release(self):
        """
        Return the newest release, bypassing channel filters in direct-URL mode.

        A bare download URL carries no channel information, so applying the
        stable/prerelease filter to it would reject the only release there is.
        """
        if self.download_url and not self.manifest_url:
            releases = self.fetch_releases()
            return releases[0] if releases else None
        return super().latest_release()

    @staticmethod
    def filename_from_url(url: str) -> str:
        """Extract a filename from a URL path, falling back to a generic name."""
        if not url:
            return "download"
        path = urlparse(url).path
        name = unquote(path.rsplit("/", 1)[-1])
        return name or "download"

    def fetch_text_file(self, path: str, ref: str = "") -> str:
        """A generic host exposes no repository tree, so notes come from the manifest."""
        return ""

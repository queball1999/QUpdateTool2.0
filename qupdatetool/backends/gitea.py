"""
Gitea and Forgejo release backend.

Gitea deliberately mirrors the GitHub API shape, and Forgejo is a Gitea fork,
so one implementation covers both. The differences that matter are the API
root path, the token authorization scheme, and the fact that Gitea serves
assets from browser_download_url rather than an API asset URL.
"""

from __future__ import annotations

import base64

from ..errors import BackendError, ConfigError
from .base import Asset, Release, ReleaseBackend


class GiteaBackend(ReleaseBackend):
    """Release source backed by the Gitea or Forgejo API."""

    name = "Gitea"
    default_host = ""

    def __init__(self, config):
        super().__init__(config)
        if not self.host:
            raise ConfigError(
                "Gitea requires an explicit host",
                "Pass --host https://git.example.com or set source.host",
            )

    def api_root(self) -> str:
        """
        Return the API root, tolerating a host given with or without /api/v1.

        Operators reasonably write either form, and guessing wrong produces a
        confusing 404 several steps later, so both are normalised here.
        """
        host = self.host.rstrip("/")
        if host.endswith("/api/v1"):
            return host
        return f"{host}/api/v1"

    def auth_headers(self) -> dict:
        """Gitea expects the "token <value>" scheme rather than a bearer token."""
        token = self.config.token
        if not token:
            return {}
        return {"Authorization": f"token {token}"}

    def fetch_releases(self, limit: int = 30) -> list:
        """Fetch releases newest-first from the Gitea API."""
        url = f"{self.api_root()}/repos/{self.repo}/releases"
        payload = self.request_json(url, params={"limit": limit})

        if not isinstance(payload, list):
            raise BackendError("Unexpected releases payload from Gitea")

        return [self.parse_release(item) for item in payload]

    def parse_release(self, item: dict) -> Release:
        """Convert one Gitea release object into our normalised Release."""
        assets = []

        for raw in item.get("assets") or []:
            assets.append(
                Asset(
                    name=raw.get("name", ""),
                    url=raw.get("browser_download_url", "") or raw.get("url", ""),
                    size=int(raw.get("size") or 0),
                    content_type=raw.get("type", "") or "",
                )
            )

        return Release(
            tag=item.get("tag_name", ""),
            name=item.get("name", "") or item.get("tag_name", ""),
            body=item.get("body", "") or "",
            prerelease=bool(item.get("prerelease")),
            draft=bool(item.get("draft")),
            published_at=item.get("published_at", "") or "",
            html_url=item.get("html_url", "") or "",
            assets=assets,
        )

    def fetch_text_file(self, path: str, ref: str = "") -> str:
        """Read a repository file through the Gitea contents API."""
        url = f"{self.api_root()}/repos/{self.repo}/contents/{path.lstrip('/')}"
        params = {"ref": ref} if ref else None

        try:
            payload = self.request_json(url, params=params)
        except BackendError:
            return ""

        if isinstance(payload, dict) and payload.get("encoding") == "base64":
            try:
                return base64.b64decode(payload.get("content", "")).decode(
                    "utf-8", errors="replace"
                )
            except (ValueError, TypeError):
                return ""

        return ""

    def list_directory(self, path: str, ref: str = "") -> list:
        """List filenames in a repository directory."""
        url = f"{self.api_root()}/repos/{self.repo}/contents/{path.lstrip('/')}"
        params = {"ref": ref} if ref else None

        try:
            payload = self.request_json(url, params=params)
        except BackendError:
            return []

        if not isinstance(payload, list):
            return []

        return [entry.get("name", "") for entry in payload if entry.get("type") == "file"]


class ForgejoBackend(GiteaBackend):
    """Forgejo speaks the Gitea API; this exists so --provider forgejo reads clearly."""

    name = "Forgejo"

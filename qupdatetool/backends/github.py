"""
GitHub and GitHub Enterprise release backend.

Works against github.com and any GitHub Enterprise Server install by pointing
source.host at the enterprise API root. Private repositories are supported
through a personal access token or a fine-grained token with Contents: read.
"""

from __future__ import annotations

import base64

from ..errors import BackendError
from .base import Asset, Release, ReleaseBackend


class GitHubBackend(ReleaseBackend):
    """Release source backed by the GitHub REST API."""

    name = "GitHub"
    default_host = "https://api.github.com"

    def auth_headers(self) -> dict:
        """
        GitHub accepts a bearer token for both classic and fine-grained PATs.

        The API version header pins response shapes so a future default change
        on the server side cannot silently alter what we parse.
        """
        headers = {"X-GitHub-Api-Version": "2022-11-28"}
        token = self.config.token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def releases_url(self) -> str:
        return f"{self.host}/repos/{self.repo}/releases"

    def fetch_releases(self, limit: int = 30) -> list:
        """Fetch releases newest-first from the GitHub API."""
        payload = self.request_json(self.releases_url(), params={"per_page": limit})

        if not isinstance(payload, list):
            raise BackendError("Unexpected releases payload from GitHub")

        return [self.parse_release(item) for item in payload]

    def parse_release(self, item: dict) -> Release:
        """Convert one GitHub release object into our normalised Release."""
        assets = []

        for raw in item.get("assets") or []:
            # The API asset URL (not browser_download_url) is what works for
            # private repositories, given an octet-stream Accept header.
            url = raw.get("url") or raw.get("browser_download_url", "")
            assets.append(
                Asset(
                    name=raw.get("name", ""),
                    url=url,
                    size=int(raw.get("size") or 0),
                    content_type=raw.get("content_type", ""),
                    digest=raw.get("digest", "") or "",
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
        """Read a repository file through the contents API."""
        url = f"{self.host}/repos/{self.repo}/contents/{path.lstrip('/')}"
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
        url = f"{self.host}/repos/{self.repo}/contents/{path.lstrip('/')}"
        params = {"ref": ref} if ref else None

        try:
            payload = self.request_json(url, params=params)
        except BackendError:
            return []

        if not isinstance(payload, list):
            return []

        return [entry.get("name", "") for entry in payload if entry.get("type") == "file"]

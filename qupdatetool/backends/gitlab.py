"""
GitLab release backend, covering gitlab.com and self-managed instances.

GitLab differs from the GitHub-shaped APIs in three ways that matter here:
projects are addressed by a URL-encoded path or numeric ID, release files are
"asset links" rather than uploaded assets, and authentication uses the
PRIVATE-TOKEN header for personal access tokens.
"""

from __future__ import annotations

import base64
from urllib.parse import quote

from ..errors import BackendError
from .base import Asset, Release, ReleaseBackend


class GitLabBackend(ReleaseBackend):
    """Release source backed by the GitLab REST API."""

    name = "GitLab"
    default_host = "https://gitlab.com"

    def api_root(self) -> str:
        host = self.host.rstrip("/")
        if host.endswith("/api/v4"):
            return host
        return f"{host}/api/v4"

    def project_id(self) -> str:
        """
        Return the URL-encoded project path.

        GitLab requires "group/project" to arrive as "group%2Fproject", and a
        numeric project ID is passed through untouched.
        """
        if self.repo.isdigit():
            return self.repo
        return quote(self.repo, safe="")

    def auth_headers(self) -> dict:
        """
        GitLab accepts several token types under different headers.

        A personal or project access token goes in PRIVATE-TOKEN; a CI job
        token or OAuth token uses the bearer scheme. PRIVATE-TOKEN is sent by
        default because it covers the common case, with the bearer header
        added alongside so an OAuth token also works without extra config.
        """
        token = self.config.token
        if not token:
            return {}
        return {"PRIVATE-TOKEN": token, "Authorization": f"Bearer {token}"}

    def fetch_releases(self, limit: int = 30) -> list:
        """Fetch releases newest-first from the GitLab API."""
        url = f"{self.api_root()}/projects/{self.project_id()}/releases"
        payload = self.request_json(url, params={"per_page": limit})

        if not isinstance(payload, list):
            raise BackendError("Unexpected releases payload from GitLab")

        return [self.parse_release(item) for item in payload]

    def parse_release(self, item: dict) -> Release:
        """
        Convert one GitLab release object into our normalised Release.

        Both explicit asset links and the auto-generated source archives are
        surfaced, so a project that publishes only source tarballs still has
        something the updater can resolve.
        """
        assets = []
        asset_block = item.get("assets") or {}

        for link in asset_block.get("links") or []:
            assets.append(
                Asset(
                    name=link.get("name", "") or link.get("filename", ""),
                    url=link.get("direct_asset_url", "") or link.get("url", ""),
                    content_type=link.get("link_type", "") or "",
                )
            )

        for source in asset_block.get("sources") or []:
            fmt = source.get("format", "")
            assets.append(
                Asset(
                    name=f"{item.get('tag_name', 'source')}.{fmt}",
                    url=source.get("url", ""),
                    content_type=fmt,
                )
            )

        # GitLab has no draft flag on releases; an unreleased future date is
        # the closest equivalent and is left to the channel filter instead.
        return Release(
            tag=item.get("tag_name", ""),
            name=item.get("name", "") or item.get("tag_name", ""),
            body=item.get("description", "") or "",
            prerelease=bool(item.get("upcoming_release")),
            draft=False,
            published_at=item.get("released_at", "") or "",
            html_url=(item.get("_links") or {}).get("self", "") or "",
            assets=assets,
        )

    def fetch_text_file(self, path: str, ref: str = "") -> str:
        """Read a repository file through the GitLab files API."""
        encoded_path = quote(path.lstrip("/"), safe="")
        url = f"{self.api_root()}/projects/{self.project_id()}/repository/files/{encoded_path}"
        params = {"ref": ref or "HEAD"}

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
        """List filenames in a repository directory through the tree API."""
        url = f"{self.api_root()}/projects/{self.project_id()}/repository/tree"
        params = {"path": path.lstrip("/"), "per_page": 100}
        if ref:
            params["ref"] = ref

        try:
            payload = self.request_json(url, params=params)
        except BackendError:
            return []

        if not isinstance(payload, list):
            return []

        return [entry.get("name", "") for entry in payload if entry.get("type") == "blob"]

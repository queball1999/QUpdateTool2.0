"""
Release backend interface and the shared HTTP session behind it.

A backend answers one question: given a repository and a channel, what is the
newest release and what files does it contain? Everything platform-specific
about picking and installing those files lives elsewhere, so adding support
for a new git host means implementing two methods here and nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..errors import BackendError, NetworkError

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - requests is a hard dependency
    requests = None


@dataclass
class Asset:
    """One downloadable file attached to a release."""

    name: str
    url: str                        # URL the updater should download from
    size: int = 0
    content_type: str = ""
    digest: str = ""                # server-reported digest, when available
    headers: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"<Asset {self.name} ({self.size} bytes)>"


@dataclass
class Release:
    """A release, normalised across every backend."""

    tag: str
    name: str = ""
    version: str = ""               # tag with prefixes/suffixes stripped
    body: str = ""                  # release notes as published on the host
    prerelease: bool = False
    draft: bool = False
    published_at: str = ""
    html_url: str = ""
    assets: list = field(default_factory=list)

    def find_asset(self, name: str) -> Asset | None:
        """Find an asset by exact filename, case-insensitively."""
        lowered = name.lower()
        for asset in self.assets:
            if asset.name.lower() == lowered:
                return asset
        return None

    def find_signature(self, asset_name: str, suffixes: tuple) -> Asset | None:
        """
        Find the detached signature that belongs to `asset_name`.

        Both conventions are accepted: a sibling file named "<asset><suffix>"
        and one where the suffix replaces the original extension.
        """
        candidates = [f"{asset_name}{suffix}" for suffix in suffixes]

        stem = asset_name.rsplit(".", 1)[0]
        candidates += [f"{stem}{suffix}" for suffix in suffixes]

        for candidate in candidates:
            found = self.find_asset(candidate)
            if found:
                return found

        return None


class ReleaseBackend:
    """Base class for release sources. Subclasses implement fetch_releases()."""

    name = "base"
    default_host = ""

    def __init__(self, config):
        self.config = config
        self.host = (config.get("source.host") or self.default_host).rstrip("/")
        self.repo = config.get("source.repo", "")
        self.timeout = int(config.get("source.timeout", 30))
        self.verify_tls = bool(config.get("source.verify_tls", True))
        self.session = self.build_session()

    # --- HTTP plumbing ---

    def build_session(self):
        """Create a requests session with retry and backoff already wired in."""
        if requests is None:
            raise NetworkError("The requests library is not installed")

        session = requests.Session()
        retry = Retry(
            total=int(self.config.get("source.retries", 3)),
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            backoff_factor=1.5,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({
            "User-Agent": self.config.get("source.user_agent", "QUpdateTool/0.0.1"),
            "Accept": "application/json",
        })
        return session

    def auth_headers(self) -> dict:
        """Authorization headers for this backend. Subclasses override the scheme."""
        token = self.config.token
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    def request_json(self, url: str, params: dict | None = None):
        """GET a URL and parse the JSON body, translating failures into our errors."""
        try:
            response = self.session.get(
                url,
                headers=self.auth_headers(),
                params=params,
                timeout=self.timeout,
                verify=self.verify_tls,
            )
        except requests.exceptions.SSLError as exc:
            raise NetworkError("TLS verification failed", str(exc)) from exc
        except requests.exceptions.RequestException as exc:
            raise NetworkError(f"Could not reach {self.name}", str(exc)) from exc

        if response.status_code == 401:
            raise BackendError(
                "Authentication failed",
                "The access token is missing, expired, or lacks read access",
            )
        if response.status_code == 403:
            detail = "Rate limited or forbidden"
            if "rate limit" in (response.text or "").lower():
                detail = "API rate limit exceeded; supply a token to raise the limit"
            raise BackendError("Access denied", detail)
        if response.status_code == 404:
            raise BackendError(
                "Repository or release not found",
                f"{url} returned 404; check source.repo and source.host",
            )
        if response.status_code >= 400:
            raise BackendError(
                f"{self.name} returned HTTP {response.status_code}",
                (response.text or "")[:400],
            )

        try:
            return response.json()
        except ValueError as exc:
            raise BackendError("Response was not valid JSON", str(exc)) from exc

    def download_headers(self) -> dict:
        """
        Headers to use when downloading an asset rather than reading metadata.

        Private repositories generally need the auth header repeated on the
        asset request, and an explicit octet-stream Accept so the API returns
        the file itself instead of a JSON description of it.
        """
        headers = dict(self.auth_headers())
        headers["Accept"] = "application/octet-stream"
        headers["User-Agent"] = self.config.get("source.user_agent", "QUpdateTool/0.0.1")
        return headers

    # --- interface ---

    def fetch_releases(self, limit: int = 30) -> list:
        """Return releases newest-first. Implemented by each backend."""
        raise NotImplementedError

    def latest_release(self) -> Release | None:
        """
        Return the newest release matching the configured channel and filters.

        Drafts are always skipped. Pre-releases are included only when the
        channel asks for them, so a stable-channel user is never offered an
        rc build by accident.
        """
        channel = (self.config.get("source.channel") or "stable").lower()
        tag_pattern = self.config.get("source.tag_pattern", "")
        matcher = re.compile(tag_pattern) if tag_pattern else None

        for release in self.fetch_releases():
            if release.draft:
                continue
            if channel == "stable" and release.prerelease:
                continue
            if channel == "prerelease" and not release.prerelease:
                continue
            if matcher and not matcher.search(release.tag):
                continue
            return release

        return None

    def fetch_text_file(self, path: str, ref: str = "") -> str:
        """
        Fetch a text file from the repository at a given ref.

        Used to read release notes out of a notices folder or a CHANGELOG.
        Backends that cannot do this return an empty string rather than
        raising, because notes are a nice-to-have, not a hard requirement.
        """
        return ""

    def list_directory(self, path: str, ref: str = "") -> list:
        """Return filenames in a repository directory, or [] if unsupported."""
        return []

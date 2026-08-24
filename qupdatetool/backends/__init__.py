"""
Backend registry.

Adding support for a new git host means writing a ReleaseBackend subclass and
registering it here; nothing else in the updater needs to change.
"""

from __future__ import annotations

from ..errors import ConfigError
from .base import Asset, Release, ReleaseBackend
from .generic import GenericBackend
from .gitea import ForgejoBackend, GiteaBackend
from .github import GitHubBackend
from .gitlab import GitLabBackend

BACKENDS = {
    "github": GitHubBackend,
    "gitea": GiteaBackend,
    "forgejo": ForgejoBackend,
    "gitlab": GitLabBackend,
    "generic": GenericBackend,
}

__all__ = [
    "BACKENDS",
    "Asset",
    "ForgejoBackend",
    "GenericBackend",
    "GitHubBackend",
    "GitLabBackend",
    "GiteaBackend",
    "Release",
    "ReleaseBackend",
    "get_backend",
]


def get_backend(config) -> ReleaseBackend:
    """Instantiate the backend named by source.provider."""
    provider = (config.get("source.provider") or "github").lower()

    backend_class = BACKENDS.get(provider)
    if backend_class is None:
        raise ConfigError(
            f"Unknown provider: {provider}",
            f"Expected one of: {', '.join(sorted(BACKENDS))}",
        )

    return backend_class(config)

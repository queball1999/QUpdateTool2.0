"""
Configuration model, defaults, and layered merging.

The updater can be driven three ways, and real deployments mix all three: a
brand file baked into the binary at build time, a config.yaml sitting beside
it, and command-line flags. They are merged in that order, lowest priority
first, so a branded updater.exe works with no arguments at all while an
operator can still override any single value on the command line.

    built-in defaults
      <- brand data compiled into the binary
        <- config.yaml (--config, or auto-discovered next to the binary)
          <- environment variables
            <- command-line flags

Values live in a nested dict rather than a rigid dataclass tree because the
merge is recursive and every layer is partial. `Config` wraps that dict with
dotted-path access and the small amount of derived logic the rest of the tool
needs.
"""

from __future__ import annotations

import copy
import os
import re
import sys
from pathlib import Path

from .errors import ConfigError

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is a hard dependency
    yaml = None

# Environment variables consulted for values that should not appear in a
# config file or a process listing. Each maps onto a dotted config path.
ENV_OVERRIDES = {
    "QUPDATE_TOKEN": "source.token",
    "QUPDATE_REPO": "source.repo",
    "QUPDATE_HOST": "source.host",
    "QUPDATE_PROVIDER": "source.provider",
    "QUPDATE_CHANNEL": "source.channel",
    "QUPDATE_CONFIG": "meta.config_path",
    "QUPDATE_LOG_LEVEL": "logging.level",
}

# Filenames searched for when --config is not given.
CONFIG_FILENAMES = ("qupdate.yaml", "qupdate.yml", "config.yaml", "config.yml", "update.yaml")

DEFAULTS = {
    "app": {
        "name": "",                     # display name, used in GUI and logs
        "publisher": "",
        "current_version": "",          # version the parent app is running
        "executable": "",               # path to the parent binary
        "install_dir": "",              # derived from executable when blank
        "process_names": [],            # extra process names to stop
        "support_url": "",
        "icon": "",                     # filename only, for display/debug
        "icon_data": "",                # base64-embedded icon bytes (branded builds)
        "icon_format": "",              # "ico" | "icns" | "png", matches icon_data
    },
    "source": {
        "provider": "github",           # github | gitea | gitlab | generic
        "host": "",                     # API base; defaults per provider
        "repo": "",                     # "owner/name"
        "channel": "stable",            # stable | prerelease | any
        "tag_pattern": "",              # regex a release tag must match
        "token": "",
        "token_env": "",                # name of an env var holding the token
        "token_file": "",
        "manifest_url": "",             # generic provider: static JSON manifest
        "download_url": "",             # generic provider: direct artifact URL
        "timeout": 30,
        "retries": 3,
        "verify_tls": True,
        "user_agent": "QUpdateTool/0.0.1",
    },
    "assets": {
        "preference": [],               # override the platform default order
        "pattern": "",                  # regex the asset filename must match
        "exclude_pattern": "",
        "match_arch": True,
        "windows": {"pattern": "", "preference": []},
        "linux": {"pattern": "", "preference": []},
        "macos": {"pattern": "", "preference": []},
    },
    "security": {
        "require_signature": True,
        "require_checksum": True,
        "signature_suffixes": [".sig", ".asc"],
        "checksum_file": "SHA256SUMS.txt",
        "public_key": "",               # inline ASCII-armored key block
        "public_key_file": "",
        "allowed_fingerprints": [],      # if set, the signing key must be one
        "allow_gpg_fallback": True,      # use the gpg binary if present
    },
    "install": {
        "mode": "silent",               # silent | interactive
        "elevate": "auto",              # auto | always | never
        "installer_args": {
            "exe": ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"],
            "exe_interactive": [],
            "msi": ["/quiet", "/norestart"],
            "msi_interactive": ["/passive"],
        },
        "target_dir": "",               # zip/tar/appimage: where to unpack
        "keep_download": False,
        "download_dir": "",
    },
    "process": {
        "calling_pid": 0,
        "stop_parent": True,
        "stop_timeout": 30,
        "force_after_timeout": True,
        "relaunch": True,
        "relaunch_command": [],         # defaults to app.executable
        "relaunch_args": [],
        "wait_before_relaunch": 2,
    },
    "notes": {
        "source": "auto",               # auto | notices | changelog | release | none
        "notices_dir": "notices",
        "changelog_paths": ["CHANGELOG.md", "CHANGELOG", "docs/CHANGELOG.md"],
        "branch": "",
        "max_length": 20000,
    },
    "ui": {
        "gui": False,
        "confirm": False,               # ask before downloading
        "window_title": "",
        "accent_color": "",
        "auto_close_seconds": 3,
    },
    "logging": {
        "level": "INFO",
        "file": "",
        "json": False,
    },
    "meta": {
        "config_path": "",
        "check_only": False,
        "dry_run": False,
        "yes": False,
    },
}


def deep_merge(base: dict, overlay: dict) -> dict:
    """
    Recursively merge `overlay` onto a copy of `base`.

    Only dicts are merged; lists and scalars replace wholesale, which is what
    you want for things like `installer_args` where an override should mean
    "use exactly these", not "append to the defaults".
    """
    result = copy.deepcopy(base)

    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        elif value is not None:
            result[key] = copy.deepcopy(value)

    return result


class Config:
    """Merged updater configuration with dotted-path access."""

    def __init__(self, data: dict):
        self.data = data

    # --- access ---

    def get(self, path: str, default=None):
        """Read a value by dotted path, e.g. get("source.repo")."""
        node = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default

    def set(self, path: str, value) -> None:
        """Write a value by dotted path, creating intermediate dicts."""
        parts = path.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ConfigError(f"Cannot set {path}: {part} is not a section")
        node[parts[-1]] = value

    def require(self, path: str, hint: str = ""):
        """Read a value that must be present, raising ConfigError if it is not."""
        value = self.get(path)
        if value in (None, "", [], {}):
            raise ConfigError(f"Missing required setting: {path}", hint)
        return value

    # --- derived values ---

    @property
    def app_name(self) -> str:
        return self.get("app.name") or "application"

    @property
    def token(self) -> str:
        """
        Resolve the access token from its inline value, env var, or file.

        Checked in that order so a CI secret injected into the environment
        beats a stale value committed to a config file.
        """
        inline = self.get("source.token", "")
        if inline:
            return str(inline).strip()

        env_name = self.get("source.token_env", "")
        if env_name and os.environ.get(env_name):
            return os.environ[env_name].strip()

        token_file = self.get("source.token_file", "")
        if token_file:
            path = Path(token_file).expanduser()
            if not path.is_file():
                raise ConfigError(f"Token file not found: {path}")
            return path.read_text(encoding="utf-8").strip()

        return ""

    @property
    def public_key_data(self) -> bytes:
        """
        Resolve the trusted public key from its inline value or a file.

        Returns empty bytes when no key is configured; the caller decides
        whether that is fatal based on security.require_signature.
        """
        inline = self.get("security.public_key", "")
        if inline:
            return inline.encode("utf-8") if isinstance(inline, str) else bytes(inline)

        key_file = self.get("security.public_key_file", "")
        if key_file:
            path = Path(key_file).expanduser()
            if not path.is_file():
                raise ConfigError(f"Public key file not found: {path}")
            return path.read_bytes()

        return b""

    @property
    def executable_path(self) -> Path | None:
        """Absolute path to the parent application binary, if configured."""
        raw = self.get("app.executable", "")
        if not raw:
            return None
        return Path(raw).expanduser().resolve()

    @property
    def install_dir(self) -> Path | None:
        """
        Directory the parent application is installed into.

        Falls back to the directory containing the executable, which is right
        for every layout the updater supports except a macOS .app bundle,
        where the bundle root is used instead.
        """
        raw = self.get("app.install_dir", "")
        if raw:
            return Path(raw).expanduser().resolve()

        executable = self.executable_path
        if not executable:
            return None

        # Walk up out of Contents/MacOS inside a .app bundle.
        for parent in [executable] + list(executable.parents):
            if parent.suffix == ".app":
                return parent

        return executable.parent

    def asset_pattern(self, os_family: str) -> str:
        """Return the asset filename regex for a platform, falling back to the global one."""
        specific = self.get(f"assets.{os_family}.pattern", "")
        return specific or self.get("assets.pattern", "")

    def asset_preference(self, os_family: str, platform_default: list) -> list:
        """Return the artifact-kind preference order for a platform."""
        specific = self.get(f"assets.{os_family}.preference", [])
        if specific:
            return list(specific)
        general = self.get("assets.preference", [])
        if general:
            return list(general)
        return list(platform_default)

    def validate(self) -> None:
        """
        Check the merged configuration for contradictions before doing any work.

        Failing here produces a clear message about a missing flag, rather
        than an obscure HTTP error several steps later.
        """
        provider = (self.get("source.provider") or "").lower()
        if provider not in ("github", "gitea", "forgejo", "gitlab", "generic"):
            raise ConfigError(
                f"Unknown provider: {provider}",
                "Expected one of: github, gitea, forgejo, gitlab, generic",
            )

        if provider == "generic":
            if not (self.get("source.manifest_url") or self.get("source.download_url")):
                raise ConfigError(
                    "The generic provider needs source.manifest_url or source.download_url",
                    "Set one in config.yaml or pass --manifest-url / --download-url",
                )
        else:
            repo = self.get("source.repo", "")
            if not repo:
                raise ConfigError(
                    "No repository configured",
                    "Pass --repo owner/name or set source.repo in config.yaml",
                )
            if provider in ("github", "gitea", "forgejo") and "/" not in repo:
                raise ConfigError(
                    f"Repository must be in owner/name form: {repo}",
                )

        channel = (self.get("source.channel") or "").lower()
        if channel not in ("stable", "prerelease", "any"):
            raise ConfigError(
                f"Unknown channel: {channel}",
                "Expected one of: stable, prerelease, any",
            )

        mode = (self.get("install.mode") or "").lower()
        if mode not in ("silent", "interactive"):
            raise ConfigError(
                f"Unknown install mode: {mode}",
                "Expected one of: silent, interactive",
            )

        elevate = (self.get("install.elevate") or "").lower()
        if elevate not in ("auto", "always", "never"):
            raise ConfigError(
                f"Unknown elevation mode: {elevate}",
                "Expected one of: auto, always, never",
            )

        for field in ("assets.pattern", "assets.exclude_pattern", "source.tag_pattern"):
            pattern = self.get(field, "")
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ConfigError(f"Invalid regex in {field}", str(exc)) from exc

        if self.get("security.require_signature") and not self.public_key_data:
            if not self.get("security.allow_gpg_fallback"):
                raise ConfigError(
                    "Signature verification is required but no public key is configured",
                    "Bake a key into the brand, set security.public_key_file, "
                    "or pass --no-require-signature to accept unsigned releases",
                )


def load_yaml_file(path: str | os.PathLike) -> dict:
    """Read one YAML config file into a dict."""
    if yaml is None:
        raise ConfigError("PyYAML is not installed; cannot read config files")

    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise ConfigError(f"Config file not found: {file_path}")

    try:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse config file: {file_path}", str(exc)) from exc

    if not isinstance(data, dict):
        raise ConfigError(f"Config file must contain a mapping: {file_path}")

    return data


def discover_config(explicit: str = "") -> Path | None:
    """
    Locate a config file: the explicit path, or a known name beside the binary.

    Searching next to the binary is what lets a branded updater.exe ship with
    a config.yaml that end users never have to know about.
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        return path

    search_dirs = []
    if getattr(sys, "frozen", False):
        search_dirs.append(Path(sys.executable).parent)
    search_dirs.append(Path(__file__).resolve().parent.parent)
    search_dirs.append(Path.cwd())

    for directory in search_dirs:
        for name in CONFIG_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate

    return None


def apply_env_overrides(data: dict) -> dict:
    """Overlay values supplied through environment variables."""
    overlay = {}

    for env_name, path in ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value is None or value == "":
            continue
        node = overlay
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    return deep_merge(data, overlay)


def build(brand: dict | None = None, config_path: str = "", overrides: dict | None = None) -> Config:
    """
    Build the merged configuration from every layer.

    `overrides` carries the command-line flags as a partial nested dict, so
    the CLI does not need to know anything about the merge order.

    Brand-locked fields are reapplied last, after the config file, the
    environment, and the flags. That ordering is the trust boundary: it means
    a config.yaml dropped next to a branded binary cannot repoint it at
    another release host or swap the trusted signing key. See brand.py for
    why that matters and what it does not protect against.
    """
    from .brand import locked_fields, locked_values

    data = copy.deepcopy(DEFAULTS)
    brand = brand or {}

    if brand:
        # The lock/unlock lists are build-time metadata, not settings.
        brand_settings = {
            key: value for key, value in brand.items() if key not in ("lock", "unlock")
        }
        data = deep_merge(data, brand_settings)

    discovered = discover_config(config_path or os.environ.get("QUPDATE_CONFIG", ""))
    if discovered:
        file_data = load_yaml_file(discovered)

        # `lock` and `unlock` are build-time metadata and are honoured only
        # from a compiled-in brand. Stripping them here means a project can
        # keep one YAML file that serves as both its brand source and its
        # runtime config without the runtime
        # copy being able to assert authority it does not have - in
        # particular, an edited config cannot `unlock` a field the binary
        # froze at build time.
        file_data = {
            key: value for key, value in file_data.items()
            if key not in ("lock", "unlock")
        }

        data = deep_merge(data, file_data)
        data.setdefault("meta", {})["config_path"] = str(discovered)

    data = apply_env_overrides(data)

    if overrides:
        data = deep_merge(data, overrides)

    config = Config(data)

    for path, value in locked_values(brand, DEFAULTS).items():
        config.set(path, value)

    config.set("meta.locked_fields", list(locked_fields(brand)))

    return config

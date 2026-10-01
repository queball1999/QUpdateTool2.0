"""
QUpdateTool's own settings: how the tool itself logs and looks.

This is not an update config. An update config.yaml (examples/config.yaml)
describes one application's releases and lives next to that application's
updater; this file belongs to QUpdateTool and lives in the per-user settings
folder:

    Windows  %LOCALAPPDATA%\\QUpdateTool\\config.yaml
    macOS    ~/Library/Application Support/QUpdateTool/config.yaml
    Linux    $XDG_CONFIG_HOME/QUpdateTool/config.yaml (~/.config/...)

It follows the QSnippet pattern: written from a template on first run and
merged with that template on every load, so a key added in a new version
appears with its default and a key that was removed is pruned.

Only `--setup` creates the file. A branded updater running on an end user's
machine reads it if it happens to exist (for the window theme) but never
writes one, so updating an application doesn't leave QUpdateTool settings
behind on that machine. An update run's own logging keeps coming from its
update config (`logging.*`), which belongs to the application.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml

from .logging_utils import LOG_LEVELS, get_logger

logger = get_logger("settings")

APP_DIR_NAME = "QUpdateTool"
SETTINGS_FILE = "config.yaml"

THEMES = ("system", "dark", "light")

DEFAULTS = {
    "log_level": "INFO",
    "log_file": "",
    "theme": "system",
    "accent_color": "",
}

TEMPLATE = """\
# QUpdateTool settings - how QUpdateTool itself logs and looks.
#
# Not an update config: that's the config.yaml next to an app's updater
# (see examples/config.yaml). Keys missing here are filled in with their
# defaults on the next run; unknown keys are removed.

# ERROR | WARNING | INFO | DEBUG. Applies to the setup wizard (--setup).
log_level: {log_level}

# Where the setup wizard writes its log: a file, or a folder to write
# setup.log into. Blank = the logs folder next to this file. "none" turns
# file logging off.
log_file: {log_file}

# system | dark | light. "system" follows the OS light/dark setting.
# Applies to the setup wizard and the update window.
theme: {theme}

# A hex colour like "#4C8BF5", or blank to use the system accent colour.
# An update config's ui.accent_color still wins in the update window.
accent_color: {accent_color}
"""


def settings_dir() -> Path:
    """The per-user folder holding this file and the setup wizard's logs."""
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_DIR_NAME


def settings_path() -> Path:
    return settings_dir() / SETTINGS_FILE


def merge(raw) -> dict:
    """
    The template's keys with values from raw where they're usable.

    A value of the wrong type or outside its allowed set falls back to the
    default, because a typo in this file must never stop the tool starting.
    """
    raw = raw if isinstance(raw, dict) else {}
    merged = dict(DEFAULTS)

    for key, default in DEFAULTS.items():
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, type(default)):
            logger.warning("Settings: ignoring %s=%r (expected %s)", key, value, type(default).__name__)
            continue
        merged[key] = value.strip() if isinstance(value, str) else value

    if str(merged["log_level"]).upper() not in LOG_LEVELS:
        logger.warning("Settings: unknown log_level %r, using INFO", merged["log_level"])
        merged["log_level"] = DEFAULTS["log_level"]
    merged["log_level"] = merged["log_level"].upper()

    if merged["theme"] not in THEMES:
        logger.warning("Settings: unknown theme %r, using system", merged["theme"])
        merged["theme"] = DEFAULTS["theme"]

    return merged


def render(settings: dict) -> str:
    # JSON strings are valid YAML scalars, and quote anything that needs it.
    return TEMPLATE.format(**{key: json.dumps(settings[key]) for key in DEFAULTS})


def load(create: bool = False, path: Path | None = None) -> dict:
    """
    Read the settings, merged over the defaults.

    With create=True the file is written when it's missing, and rewritten
    when it differs from the merged result (a new key to add, an unknown key
    to prune, an invalid value replaced). A failed write is logged, never
    raised.
    """
    path = path or settings_path()
    raw = None
    text = ""

    try:
        text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
    except FileNotFoundError:
        pass
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Could not read %s, using defaults: %s", path, exc)

    settings = merge(raw)

    if create and render(settings) != text:
        # A file that was unreadable as YAML is left alone rather than
        # overwritten, so a hand edit with a typo isn't silently lost.
        if text and raw is None:
            return settings
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render(settings), encoding="utf-8")
            logger.debug("Wrote settings to %s", path)
        except OSError as exc:
            logger.warning("Could not write %s: %s", path, exc)

    return settings


def setup_log_path(settings: dict) -> str:
    """
    Where the setup wizard logs, in the form logging_utils.configure takes.

    A configured folder gets setup.log inside it, so the wizard's log never
    lands in an updater.log next to it.
    """
    configured = str(settings.get("log_file", "")).strip()
    if configured.lower() in ("none", "off", "false", "-"):
        return "none"
    if configured:
        path = Path(configured).expanduser()
        if path.is_dir() or not path.suffix:
            return str(path / "setup.log")
        return str(path)
    return str(settings_dir() / "logs" / "setup.log")

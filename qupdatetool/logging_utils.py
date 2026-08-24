"""
Logging setup for the updater.

This deliberately mirrors the logging a typical parent application uses, down
to the format string and the compressed rotation behaviour, so that an
updater log dropped into a support bundle reads identically to an application
log and the two can be interleaved by timestamp when diagnosing a failed
update.

Three things are configurable, in the usual layer order (config file, then
environment, then flags):

  level     ERROR / WARNING / INFO / DEBUG, a common severity ladder
  file      where to write; defaults per-OS, but is meant to be pointed at
            the parent application own log directory so everything lands in
            one place
  console   whether to also write to stderr, which is on by default for a
            CLI run and off when the updater is driven by a parent process

The default location is used only when nothing overrides it. A parent
application should pass --log-file pointing at its own log directory so the
two logs stay together.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_LEVELS = ("ERROR", "WARNING", "INFO", "DEBUG")

# Matches a typical parent application's format so the two logs interleave cleanly.
LOG_FORMAT = "[%(levelname)s] %(asctime)s [%(name)s]: %(message)s"
CONSOLE_FORMAT = "%(message)s"

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3

logger = logging.getLogger("qupdatetool")


class CompressedRotatingFileHandler(RotatingFileHandler):
    """
    A rotating file handler that zips each rotated log.

    Produces a "<name>.log.1.zip" backup layout so a support bundle looks
    consistent alongside the parent application's own logs.
    """

    def doRollover(self) -> None:
        """
        Perform log file rollover with compression.

        Closes the current stream, shifts existing zipped backups along,
        compresses the current log into a new .1.zip, and reopens the stream.
        """
        if self.stream:
            self.stream.close()
            self.stream = None

        if self.backupCount > 0:
            oldest = f"{self.baseFilename}.{self.backupCount}.zip"
            if os.path.exists(oldest):
                os.remove(oldest)

            for index in range(self.backupCount - 1, 0, -1):
                source = f"{self.baseFilename}.{index}.zip"
                destination = f"{self.baseFilename}.{index + 1}.zip"
                if os.path.exists(source):
                    os.rename(source, destination)

            destination = f"{self.baseFilename}.1.zip"
            try:
                self.compress_log_file(self.baseFilename, destination)
            except OSError:
                # Losing a rotated log is not worth failing an update over.
                pass

        self.mode = "w"
        self.stream = self._open()

    def compress_log_file(self, source: str, dest_zip: str) -> None:
        """Compress a log file into a zip archive and remove the original."""
        with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(source, arcname=os.path.basename(source))
        os.remove(source)


class JsonFormatter(logging.Formatter):
    """
    Emits one JSON object per line.

    Used when the updater runs under a parent process that wants to parse the
    log rather than display it.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def resolve_level(value) -> int:
    """
    Convert a level name or number into a logging level.

    Unknown names fall back to INFO rather than raising, because a typo in a
    config file should not prevent an update from running.
    """
    if isinstance(value, int):
        return value

    name = str(value or "INFO").strip().upper()
    return getattr(logging, name, logging.INFO)


def default_log_dir(app_name: str = "QUpdateTool") -> Path:
    """
    Return the conventional per-user log directory for this platform.

    Used only when no log file is configured. A parent application should
    override this with --log-file so the updater writes alongside its own
    logs instead of into a second location the user has to go find.
    """
    safe_name = "".join(
        character for character in (app_name or "QUpdateTool")
        if character.isalnum() or character in (" ", "-", "_")
    ).strip() or "QUpdateTool"

    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / safe_name / "logs"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / safe_name

    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / safe_name / "logs"

    return Path.home() / ".local" / "state" / safe_name / "logs"


def resolve_log_path(configured: str = "", app_name: str = "QUpdateTool") -> Path | None:
    """
    Work out where the log file should go.

    A configured value wins and may be either a full file path or a
    directory, because a parent application usually knows its log *directory*
    and should not have to invent a filename. Returns None when file logging
    is explicitly disabled with "none".
    """
    if configured:
        text = str(configured).strip()
        if text.lower() in ("none", "off", "false", "-"):
            return None

        path = Path(text).expanduser()

        # An existing directory, or a path that looks like one, gets a
        # filename appended rather than being treated as the log file.
        if path.is_dir() or not path.suffix:
            return path / "updater.log"

        return path

    return default_log_dir(app_name) / "updater.log"


def configure(level="INFO", log_file: str = "", app_name: str = "QUpdateTool",
              console: bool = True, json_format: bool = False,
              max_bytes: int = DEFAULT_MAX_BYTES,
              backup_count: int = DEFAULT_BACKUP_COUNT) -> logging.Logger:
    """
    Configure the updater logger and return it.

    Console output goes to stderr so that stdout stays clean for machine
    readable output, which is what lets a parent application parse `--json`
    while still capturing the human log.

    File logging failing is never fatal. An updater that refuses to run
    because it could not open a log file would be worse than one that runs
    without a log, so the failure is reported to the console and the update
    proceeds.
    """
    resolved_level = resolve_level(level)

    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)

    logger.setLevel(resolved_level)

    # Records are handled here, not by the root logger, so that configuring
    # the updater never disturbs logging in a process that embeds it.
    logger.propagate = False

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(resolved_level)
        console_handler.setFormatter(
            JsonFormatter() if json_format else logging.Formatter(CONSOLE_FORMAT)
        )
        logger.addHandler(console_handler)

    log_path = resolve_log_path(log_file, app_name)

    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)

            file_handler = CompressedRotatingFileHandler(
                str(log_path),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(resolved_level)
            file_handler.setFormatter(
                JsonFormatter() if json_format else logging.Formatter(LOG_FORMAT)
            )
            logger.addHandler(file_handler)

            logger.debug("Logging to %s at level %s", log_path, logging.getLevelName(resolved_level))

        except OSError as exc:
            logger.warning("Could not open log file %s: %s", log_path, exc)

    return logger


def set_level(level) -> None:
    """Change the log level at runtime, on the logger and all its handlers."""
    resolved = resolve_level(level)
    logger.setLevel(resolved)
    for handler in logger.handlers:
        handler.setLevel(resolved)


def current_log_path() -> str:
    """Return the active log file path, or "" when only console logging is on."""
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            return handler.baseFilename
    return ""


def get_logger(name: str = "") -> logging.Logger:
    """Return a child logger under the updater namespace."""
    return logger.getChild(name) if name else logger

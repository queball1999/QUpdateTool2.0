"""
Resolves the window icon from the branded config.

The icon is embedded as base64 bytes in the brand (see tools/build_branded.py),
not referenced by the build-time file path: that path points into the
consuming project's source tree and does not exist on an end user's machine,
and PyInstaller's own --icon flag only sets the .exe's file-explorer icon
resource - it does not make the source file available to the running program.
Without this, QIcon(app.icon) silently resolves nothing and the window falls
back to Qt's generic default icon.

QIcon needs an actual file on disk to parse a multi-resolution .ico properly
(loading from raw bytes via QPixmap.loadFromData only grabs a single frame),
so the embedded bytes are written to a temporary file once per process and
loaded from there.
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

from ..logging_utils import get_logger

logger = get_logger("ui")

_cached_icon = None
_cached_temp_path = None


def resolve_icon(config):
    """
    Return a QIcon for this branded build, or None if no icon is available.

    Cached for the life of the process: every dialog and the QApplication
    itself can call this without re-decoding the base64 payload or writing a
    second temp file.
    """
    global _cached_icon, _cached_temp_path

    if _cached_icon is not None:
        return _cached_icon

    from PySide6.QtGui import QIcon

    icon_data = config.get("app.icon_data", "")

    if icon_data:
        icon_format = config.get("app.icon_format", "") or "ico"

        try:
            raw = base64.b64decode(icon_data)
        except (ValueError, TypeError) as exc:
            logger.warning("Could not decode the embedded icon: %s", exc)
            raw = b""

        if raw:
            try:
                handle = tempfile.NamedTemporaryFile(
                    suffix=f".{icon_format}", delete=False
                )
                handle.write(raw)
                handle.close()
                _cached_temp_path = handle.name

                icon = QIcon(_cached_temp_path)
                if not icon.isNull():
                    _cached_icon = icon
                    return _cached_icon

                logger.warning("Embedded icon data decoded but produced a null QIcon")
            except OSError as exc:
                logger.warning("Could not write the embedded icon to a temp file: %s", exc)

    # Fall back to a literal path, for an unbranded/dev run where a config
    # file points app.icon at a real file on this machine.
    literal_path = config.get("app.icon", "")
    if literal_path and Path(literal_path).is_file():
        icon = QIcon(literal_path)
        if not icon.isNull():
            _cached_icon = icon
            return _cached_icon

    return None

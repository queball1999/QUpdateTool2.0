"""
GUI bootstrap.

Keeping the Qt import inside this module is what allows the updater to run on
a machine with no GUI toolkit at all: nothing here is imported unless --gui
was actually passed. If PySide6 turns out to be missing or a display is
unavailable, the updater says so and falls back to the console flow rather
than crashing with an import traceback.
"""

from __future__ import annotations

import json
import os
import sys

from ..errors import ExitCode
from ..logging_utils import get_logger

logger = get_logger("ui")


def gui_available() -> tuple:
    """
    Report whether a GUI can actually be shown.

    Returns (available, reason). A missing PySide6 and a headless session are
    both common and both need a clear explanation rather than a traceback.
    """
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return False, "PySide6 is not installed in this build"

    if sys.platform.startswith("linux"):
        has_display = bool(
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        )
        if not has_display:
            return False, "No display is available (DISPLAY and WAYLAND_DISPLAY are unset)"

    return True, ""


def run_gui(config, args) -> int:
    """
    Run the update with a progress window.

    Falls back to the console flow when a GUI cannot be created, so passing
    --gui on a headless server degrades to a working headless update instead
    of failing outright.
    """
    available, reason = gui_available()

    if not available:
        logger.warning("Falling back to console mode: %s", reason)
        print(f"Cannot show the update window: {reason}", file=sys.stderr)
        print("Continuing without a GUI.", file=sys.stderr)

        from ..cli import run_console

        return run_console(config, args)

    from PySide6.QtWidgets import QApplication

    from .icon import resolve_icon
    from .window import UpdaterWindow

    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName(config.app_name)
    application.setApplicationDisplayName(config.app_name)

    if config.get("app.publisher"):
        application.setOrganizationName(config.get("app.publisher"))

    # Set at the application level too, not just on the window: on Linux this
    # is what makes the taskbar/dock icon correct, since some window managers
    # read QApplication's icon rather than the individual QWidget's.
    icon = resolve_icon(config)
    if icon is not None:
        application.setWindowIcon(icon)

    window = UpdaterWindow(config, check_only=bool(args.check_only))
    window.show()

    application.exec()

    if args.json and window.result_payload is not None:
        payload = window.result_payload
        print(json.dumps(payload.to_dict(), indent=2, default=str))

    return window.exit_code if window.exit_code else ExitCode.SUCCESS

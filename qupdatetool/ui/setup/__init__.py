"""
Entry point for `updater --setup`.

Mirrors ui/app.py's run_gui(): checks gui_available() before importing
PySide6 or anything Qt-dependent, so `--setup` fails with a clear message
instead of an import traceback when the GUI extra isn't installed. Nothing
at this module's top level imports PySide6, so importing qupdatetool.ui.setup
(e.g. from cli.py, or from tests exercising state/validators/generator) never
requires the GUI extra either.
"""

from __future__ import annotations

import sys

from ...errors import ExitCode
from ...logging_utils import get_logger
from ..app import gui_available

logger = get_logger("setup")


def run_setup_wizard(config_path: str = "", settings: dict | None = None) -> int:
    """
    Open the wizard. settings is QUpdateTool's own settings (app_settings),
    already used by cli.py to configure logging; here it picks the theme.
    """
    available, reason = gui_available()
    if not available:
        logger.error("Cannot open the setup wizard: %s", reason)
        print(f"Cannot open the setup wizard: {reason}", file=sys.stderr)
        print("Install GUI dependencies with: pip install -r requirements-gui.txt", file=sys.stderr)
        return ExitCode.ERROR

    from PySide6.QtWidgets import QApplication, QDialog

    from ..theme import apply_theme
    from .state import WizardState
    from .wizard import SetupWizard

    settings = settings or {}
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("QUpdateTool setup")
    apply_theme(application, settings.get("theme", "system"), settings.get("accent_color", ""))

    # An exception inside a Qt slot doesn't reach main(); log it instead of
    # letting it vanish into stderr, and keep the wizard running.
    def log_uncaught(kind, value, traceback):
        logger.error("Unhandled error in the setup wizard", exc_info=(kind, value, traceback))

    previous_hook = sys.excepthook
    sys.excepthook = log_uncaught

    try:
        initial_state = WizardState.from_config_file(config_path) if config_path else WizardState()
    except Exception as exc:  # noqa: BLE001 - a bad --config with --setup should not crash the wizard
        logger.warning("Could not load %s: %s", config_path, exc)
        print(f"Could not load {config_path}: {exc}", file=sys.stderr)
        initial_state = WizardState()

    logger.info("Setup wizard opened%s", f" with {config_path}" if config_path else "")
    try:
        wizard = SetupWizard(initial_state)
        result = wizard.exec()
    finally:
        sys.excepthook = previous_hook

    return ExitCode.SUCCESS if result == QDialog.Accepted else ExitCode.CANCELLED

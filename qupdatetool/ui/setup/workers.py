"""
Qt thread wrapper around the network check in validators.py.

Kept separate from validators.py so that module stays importable (and
tested) without PySide6 installed. This is the only piece of glue needed:
the check itself is plain, blocking, synchronous Python, run off the UI
thread so the Validate page never freezes on a slow or unreachable backend.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from . import validators


class ConnectionTestWorker(QThread):
    """Runs validators.test_connection_sync() off the UI thread."""

    finished_check = Signal(object)  # ConnectionResult

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config

    def run(self) -> None:
        result = validators.test_connection_sync(self.config)
        self.finished_check.emit(result)

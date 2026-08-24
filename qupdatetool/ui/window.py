"""
The Qt progress window shown when the updater runs with --gui.

The window is intentionally plain: an icon, a heading, a status line, a
progress bar, and a cancel button. It is branded through the icon and accent
colour supplied at build time, so the same code renders as any application
updater without carrying per-application UI.

All real work happens on a worker thread. The GUI never blocks, so cancel
stays responsive even mid-download, and Qt is never touched from the worker.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
)

from ..errors import CancelledError, UpdaterError
from ..updater import build_updater


class UpdateWorker(QThread):
    """Runs the update on a background thread, reporting back through signals."""

    status = Signal(str)
    progress = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str, int)

    def __init__(self, config, check_only: bool = False, parent=None):
        super().__init__(parent)
        self.config = config
        self.check_only = check_only
        self.updater = None
        self.check_result = None

    def run(self) -> None:
        """Execute the update flow, translating every failure into a signal."""
        try:
            self.updater = build_updater(
                self.config,
                status_callback=self.status.emit,
                progress_callback=self.progress.emit,
            )

            check = self.updater.check()
            self.check_result = check

            if self.check_only or not check.available:
                self.finished_ok.emit(check)
                return

            outcome = self.updater.apply(check)
            self.finished_ok.emit(outcome)

        except CancelledError as exc:
            self.failed.emit(str(exc), exc.exit_code)
        except UpdaterError as exc:
            self.failed.emit(str(exc), exc.exit_code)
        except Exception as exc:  # noqa: BLE001 - never let a worker exception kill the GUI
            from ..errors import ExitCode
            from ..logging_utils import get_logger

            get_logger("ui").exception("Unhandled error in the update worker")
            # ERROR rather than a borrowed category code; see cli.run_console.
            self.failed.emit(f"Unexpected error: {exc}", ExitCode.ERROR)

    def cancel(self) -> None:
        if self.updater:
            self.updater.cancel()


class UpdaterWindow(QDialog):
    """The progress window."""

    def __init__(self, config, check_only: bool = False, parent=None):
        super().__init__(parent)
        self.config = config
        self.check_only = check_only
        self.result_payload = None
        self.exit_code = 0
        self.finished_cleanly = False

        app_name = config.app_name
        title = config.get("ui.window_title", "") or f"{app_name} Updater"

        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        from .icon import resolve_icon

        icon = resolve_icon(config)
        if icon is not None:
            self.setWindowIcon(icon)

        self.build_widgets(app_name, icon)

        self.worker = UpdateWorker(config, check_only=check_only, parent=self)
        self.worker.status.connect(self.on_status)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)

        # Start after the window is on screen so the first status line is not
        # emitted into a widget that has not been shown yet.
        QTimer.singleShot(0, self.worker.start)

    def build_widgets(self, app_name: str, icon) -> None:
        """Lay out the window."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(14)

        if icon is not None:
            icon_label = QLabel()
            # QIcon.pixmap() picks the best-matching frame out of a
            # multi-resolution .ico for this size, rather than loading one
            # fixed frame and rescaling it, which is what the old
            # QPixmap(icon_path) + manual .scaled() call did.
            icon_label.setPixmap(icon.pixmap(48, 48))
            icon_label.setFixedSize(48, 48)
            header.addWidget(icon_label, 0, Qt.AlignTop)

        heading_box = QVBoxLayout()
        heading_box.setSpacing(2)

        self.heading = QLabel(f"Updating {app_name}")
        font = self.heading.font()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        self.heading.setFont(font)

        self.status_label = QLabel("Starting...")
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

        heading_box.addWidget(self.heading)
        heading_box.addWidget(self.status_label)
        header.addLayout(heading_box, 1)

        layout.addLayout(header)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("")

        accent = self.config.get("ui.accent_color", "")
        if accent and QColor(accent).isValid():
            self.progress_bar.setStyleSheet(
                f"QProgressBar::chunk {{ background-color: {accent}; }}"
            )

        layout.addWidget(self.progress_bar)

        self.notes_view = QTextBrowser()
        self.notes_view.setOpenExternalLinks(True)
        self.notes_view.setMaximumHeight(180)
        self.notes_view.hide()
        layout.addWidget(self.notes_view)

        buttons = QHBoxLayout()
        buttons.addStretch(1)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.on_cancel)
        buttons.addWidget(self.cancel_button)

        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.accept)
        self.close_button.hide()
        buttons.addWidget(self.close_button)

        layout.addLayout(buttons)

    # --- worker signals ---

    def on_status(self, message: str) -> None:
        self.status_label.setText(message)

    def on_progress(self, snapshot) -> None:
        """Update the progress bar from a download progress snapshot."""
        if snapshot.total > 0:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(int(snapshot.percent))
            self.progress_bar.setFormat(snapshot.describe())
        else:
            # Unknown total: show an indeterminate bar rather than a wrong one.
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat(snapshot.describe())

    def on_finished(self, payload) -> None:
        """Handle a successful run, whether that was a check or a full update."""
        self.result_payload = payload
        self.finished_cleanly = True
        self.exit_code = 0

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.cancel_button.hide()
        self.close_button.show()
        self.close_button.setDefault(True)

        notes = getattr(payload, "notes", None)
        if notes is not None and not notes.is_empty:
            self.show_notes(notes)

        if hasattr(payload, "available"):
            from ..errors import ExitCode

            if payload.available:
                self.heading.setText(f"{self.config.app_name} {payload.latest_version} available")
                self.status_label.setText(payload.reason)
                self.exit_code = ExitCode.UPDATE_AVAILABLE
            else:
                self.heading.setText("Up to date")
                self.status_label.setText(payload.reason)
                self.exit_code = ExitCode.UP_TO_DATE
        else:
            self.heading.setText("Update complete")
            self.status_label.setText(
                f"{self.config.app_name} was updated to {payload.installed_version}"
            )

            if payload.requires_restart:
                self.status_label.setText(
                    self.status_label.text() + "\nA system restart is required."
                )
            elif payload.relaunched:
                # Nothing left to look at once the app is back; close shortly.
                delay = int(self.config.get("ui.auto_close_seconds", 3))
                if delay > 0:
                    QTimer.singleShot(delay * 1000, self.accept)

    def on_failed(self, message: str, exit_code: int) -> None:
        """Show a failure and leave the window open so the user can read it."""
        self.finished_cleanly = False
        self.exit_code = exit_code

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.heading.setText("Update failed")
        self.status_label.setText(message)

        self.cancel_button.hide()
        self.close_button.show()
        self.close_button.setDefault(True)

        support = self.config.get("app.support_url", "")
        detail = f"{message}\n\nIf this keeps happening, see {support}" if support else message

        QMessageBox.critical(self, "Update failed", detail)

    def show_notes(self, notes) -> None:
        """Render release notes in the window."""
        self.notes_view.setMarkdown(notes.body)
        self.notes_view.show()
        self.adjustSize()

    # --- interaction ---

    def on_cancel(self) -> None:
        """Cancel the update, confirming first if it is already installing."""
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Cancelling...")
        self.worker.cancel()

        from ..errors import ExitCode

        self.exit_code = ExitCode.CANCELLED

    def closeEvent(self, event) -> None:
        """Make sure the worker thread is finished before the window goes away."""
        if self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(5000)
        super().closeEvent(event)

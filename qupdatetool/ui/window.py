"""
The Qt progress window shown when the updater runs with --gui.

The window is intentionally plain: an icon, a heading, a status line, a
slim progress bar, a "What's new" card, and a cancel button. The card shows a
pulsing skeleton while the check runs and the release notes once it's done.
It is branded through the icon and accent colour supplied at build time, so
the same code renders as any application updater without carrying
per-application UI. Colours come from the shared theme (ui/theme.py).

All real work happens on a worker thread. The GUI never blocks, so cancel
stays responsive even mid-download, and Qt is never touched from the worker.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
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
from ..logging_utils import get_logger
from ..updater import build_updater
from .widgets import Skeleton

logger = get_logger("ui")

NOTES_MAX_HEIGHT = 200


class UpdateWorker(QThread):
    """Runs the update on a background thread, reporting back through signals."""

    status = Signal(str)
    progress = Signal(object)
    # The UpdateCheck, as soon as the check is done and before any download,
    # so the window can show the release notes while the update runs.
    checked = Signal(object)
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
            self.checked.emit(check)

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
    """
    The progress window.

    `worker_class` defaults to UpdateWorker and exists so the setup wizard's
    Demo page (ui/setup/demo.py) can drive this exact window with a scripted
    fake worker instead - same signals, same rendering, no network call.
    """

    def __init__(self, config, check_only: bool = False, parent=None, worker_class=None):
        super().__init__(parent)
        self.config = config
        self.check_only = check_only
        self.result_payload = None
        self.exit_code = 0
        self.finished_cleanly = False

        app_name = config.app_name
        title = config.get("ui.window_title", "") or f"{app_name} Updater"

        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        from .icon import resolve_icon

        icon = resolve_icon(config)
        if icon is not None:
            self.setWindowIcon(icon)

        self.build_widgets(app_name, icon)

        worker_cls = worker_class or UpdateWorker
        self.worker = worker_cls(config, check_only=check_only, parent=self)
        self.worker.status.connect(self.on_status)
        self.worker.progress.connect(self.on_progress)
        # A custom worker_class may predate the checked signal; the notes
        # then arrive with finished_ok instead.
        if hasattr(self.worker, "checked"):
            self.worker.checked.connect(self.on_checked)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)

        # Start after the window is on screen so the first status line is not
        # emitted into a widget that has not been shown yet.
        QTimer.singleShot(0, self.worker.start)

    def build_widgets(self, app_name: str, icon) -> None:
        """Lay out the window."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(16)

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
        heading_box.setSpacing(4)

        self.heading = QLabel(f"Updating {app_name}")
        self.heading.setObjectName("Heading")

        self.status_label = QLabel("Starting...")
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

        heading_box.addWidget(self.heading)
        heading_box.addWidget(self.status_label)
        header.addLayout(heading_box, 1)

        layout.addLayout(header)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("UpdateProgress")
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        # Busy until there's a real figure to show.
        self.progress_bar.setRange(0, 0)

        accent = self.config.get("ui.accent_color", "")
        if accent and QColor(accent).isValid():
            self.progress_bar.setStyleSheet(
                f"QProgressBar#UpdateProgress::chunk {{ background-color: {accent}; }}"
            )

        layout.addWidget(self.progress_bar)

        # The bar is too slim to carry text, so the figures sit under it.
        self.progress_text = QLabel("")
        self.progress_text.setObjectName("Muted")
        self.progress_text.hide()
        layout.addWidget(self.progress_text)

        self.notes_card = QFrame()
        self.notes_card.setObjectName("Card")
        notes_layout = QVBoxLayout(self.notes_card)
        notes_layout.setContentsMargins(16, 12, 16, 12)
        notes_layout.setSpacing(8)

        self.notes_title = QLabel("What's new")
        self.notes_title.setObjectName("CardTitle")
        notes_layout.addWidget(self.notes_title)

        self.notes_skeleton = Skeleton()
        notes_layout.addWidget(self.notes_skeleton)

        self.notes_view = QTextBrowser()
        self.notes_view.setObjectName("Notes")
        self.notes_view.setOpenExternalLinks(True)
        self.notes_view.hide()
        notes_layout.addWidget(self.notes_view)

        layout.addWidget(self.notes_card)

        buttons = QHBoxLayout()
        buttons.addStretch(1)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setMinimumWidth(96)
        self.cancel_button.clicked.connect(self.on_cancel)
        buttons.addWidget(self.cancel_button)

        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("Primary")
        self.close_button.setMinimumWidth(96)
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
        else:
            # Unknown total: show an indeterminate bar rather than a wrong one.
            self.progress_bar.setRange(0, 0)
        self.progress_text.setText(snapshot.describe())
        self.progress_text.show()

    def on_checked(self, check) -> None:
        """The check is done: swap the skeleton for the release notes, if any."""
        notes = getattr(check, "notes", None)
        if notes is not None and not notes.is_empty:
            self.show_notes(notes)
        else:
            self.hide_notes()

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
        elif self.notes_view.isHidden():
            # No notes arrived at any point; don't leave a skeleton pulsing.
            self.hide_notes()

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
        self.progress_text.hide()
        if self.notes_view.isHidden():
            self.hide_notes()
        logger.warning("Update window: %s (exit %s)", message, exit_code)

        self.cancel_button.hide()
        self.close_button.show()
        self.close_button.setDefault(True)

        support = self.config.get("app.support_url", "")
        detail = f"{message}\n\nIf this keeps happening, see {support}" if support else message

        QMessageBox.critical(self, "Update failed", detail)

    def show_notes(self, notes) -> None:
        """Render release notes in the window."""
        if notes.title:
            self.notes_title.setText(f"What's new in {notes.title}")
        self.notes_view.setMarkdown(notes.body)
        self.notes_skeleton.hide()
        self.notes_view.show()
        self.notes_card.show()

        # As tall as the notes need, up to NOTES_MAX_HEIGHT, then it scrolls.
        # Measured against the card, which is already laid out; the view was
        # hidden until now, so its own width isn't meaningful yet.
        margins = self.notes_card.layout().contentsMargins()
        width = self.notes_card.width() - margins.left() - margins.right()
        document = self.notes_view.document()
        document.setTextWidth(max(width, 200))
        self.notes_view.setFixedHeight(min(NOTES_MAX_HEIGHT, int(document.size().height()) + 6))
        self.fit_to_contents()

    def hide_notes(self) -> None:
        self.notes_card.hide()
        self.fit_to_contents()

    def fit_to_contents(self) -> None:
        """Shrink or grow the window to what's showing now."""
        # Deferred: widgets just shown or hidden post their layout changes as
        # events, so an immediate adjustSize() still sees the old size hint
        # and the window keeps its old height.
        QTimer.singleShot(0, self.adjustSize)

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

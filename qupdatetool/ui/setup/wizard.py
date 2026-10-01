"""
SetupWizard: a sidebar of steps, the current page, and a Back/Next bar.

Laid out like QSnippet's settings dialog rather than a stock QWizard: the
steps are listed on the left and any step already reached can be clicked to
jump back to it. Owns the shared WizardState; see state.py for what's being
edited and generator.py for what comes out the other end.

Pages keep the QWizardPage-style contract (initializePage, validatePage,
isComplete, completeChanged) - see pages.SectionPage - so leaving a page
saves it, and entering one reloads it from the state.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...logging_utils import current_log_path, get_logger
from . import pages
from .state import WizardState

logger = get_logger("setup")

# A bit larger than the platform default throughout the wizard - this is
# meant to be comfortably readable for someone new to config files, not
# dense like a settings dialog.
BASE_FONT_POINT_INCREASE = 1

# (page class, short label for the sidebar)
PAGES = [
    (pages.WelcomePage, "Welcome"),
    (pages.ApplicationPage, "Your app"),
    (pages.SourcePage, "Release source"),
    (pages.AssetsPage, "Which file"),
    (pages.SecurityPage, "Security"),
    (pages.InstallPage, "Installing"),
    (pages.ProcessPage, "Close & reopen"),
    (pages.NotesPage, "What's new text"),
    (pages.UiLoggingPage, "Look & logs"),
    (pages.DistributionPage, "Branded build"),
    (pages.ValidatePage, "Check it works"),
    (pages.DemoPage, "See it in action"),
    (pages.ExportPage, "Save your files"),
]
PAGE_CLASSES = [cls for cls, _ in PAGES]


class SetupWizard(QDialog):
    def __init__(self, initial_state: WizardState | None = None, parent=None):
        super().__init__(parent)
        self.state = initial_state or WizardState()
        self.current = -1
        self.furthest = 0

        self.setWindowTitle("QUpdateTool setup")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.resize(1040, 740)
        self.setMinimumSize(820, 600)

        font = self.font()
        font.setPointSize(font.pointSize() + BASE_FONT_POINT_INCREASE)
        self.setFont(font)

        self.pages = [cls(self) for cls in PAGE_CLASSES]
        self.build_layout()

        for page in self.pages:
            page.completeChanged.connect(self.update_buttons)

        self.go_to(0)

    # --- layout ---

    def build_layout(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(230)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 16, 12, 12)
        side.setSpacing(4)

        title = QLabel("QUpdateTool setup")
        title.setObjectName("SidebarTitle")
        side.addWidget(title)

        self.step_list = QListWidget()
        self.step_list.setObjectName("StepList")
        self.step_list.setFocusPolicy(Qt.NoFocus)
        for _, label in PAGES:
            self.step_list.addItem(QListWidgetItem(label))
        self.step_list.currentRowChanged.connect(self.on_step_clicked)
        side.addWidget(self.step_list, 1)

        log_button = QPushButton("Open log folder")
        log_button.setObjectName("Link")
        log_button.setCursor(Qt.PointingHandCursor)
        log_button.setToolTip(current_log_path() or "File logging is off")
        log_button.setEnabled(bool(current_log_path()))
        log_button.clicked.connect(self.open_log_folder)
        side.addWidget(log_button)

        root.addWidget(sidebar)

        main = QWidget()
        body = QVBoxLayout(main)
        body.setContentsMargins(32, 26, 32, 18)
        body.setSpacing(2)

        self.page_title = QLabel()
        self.page_title.setObjectName("PageTitle")
        self.page_subtitle = QLabel()
        self.page_subtitle.setObjectName("PageSubtitle")
        self.page_subtitle.setWordWrap(True)
        body.addWidget(self.page_title)
        body.addWidget(self.page_subtitle)

        self.stack = QStackedWidget()
        for page in self.pages:
            self.stack.addWidget(page)
        body.addWidget(self.stack, 1)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 14, 0, 0)
        self.step_counter = QLabel()
        self.step_counter.setObjectName("StepCounter")
        bar.addWidget(self.step_counter)
        bar.addStretch(1)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self.back)
        self.next_button = QPushButton("Next")
        self.next_button.setObjectName("Primary")
        self.next_button.setDefault(True)
        self.next_button.clicked.connect(self.next)
        for button in (self.cancel_button, self.back_button, self.next_button):
            button.setMinimumWidth(96)
            bar.addWidget(button)
        body.addLayout(bar)

        root.addWidget(main, 1)

    # --- navigation ---

    def current_page(self):
        return self.pages[self.current]

    def go_to(self, index: int) -> None:
        self.current = index
        self.furthest = max(self.furthest, index)
        page = self.pages[index]

        page.initializePage()
        self.stack.setCurrentWidget(page)
        self.page_title.setText(page.title())
        self.page_subtitle.setText(page.subTitle())
        self.page_subtitle.setVisible(bool(page.subTitle()))

        self.step_list.blockSignals(True)
        self.step_list.setCurrentRow(index)
        self.step_list.blockSignals(False)

        logger.info("Step %d/%d: %s", index + 1, len(self.pages), PAGES[index][1])
        self.update_buttons()

    def leave_current(self) -> bool:
        """
        Save the current page before moving off it, in either direction.

        A stock QWizard drops edits when you go Back; here leaving a page by
        any route saves it, so a sidebar jump never loses what was typed.
        """
        if not self.current_page().validatePage():
            logger.info("Stayed on %s: the page didn't validate", PAGES[self.current][1])
            return False
        return True

    def next(self) -> None:
        if not self.current_page().isComplete() or not self.leave_current():
            return
        if self.current == len(self.pages) - 1:
            logger.info("Setup finished")
            self.accept()
            return
        self.go_to(self.current + 1)

    def back(self) -> None:
        if self.current > 0 and self.leave_current():
            self.go_to(self.current - 1)

    def on_step_clicked(self, row: int) -> None:
        allowed = row <= self.furthest and (row < self.current or self.current_page().isComplete())
        if row == self.current or not allowed or not self.leave_current():
            self.step_list.blockSignals(True)
            self.step_list.setCurrentRow(self.current)
            self.step_list.blockSignals(False)
            return
        self.go_to(row)

    def update_buttons(self) -> None:
        last = self.current == len(self.pages) - 1
        self.back_button.setEnabled(self.current > 0)
        self.next_button.setText("Finish" if last else "Next")
        self.next_button.setEnabled(self.current_page().isComplete())
        self.step_counter.setText(f"Step {self.current + 1} of {len(self.pages)}")

        for row in range(self.step_list.count()):
            item = self.step_list.item(row)
            flags = item.flags()
            if row <= self.furthest:
                item.setFlags(flags | Qt.ItemIsEnabled)
            else:
                item.setFlags(flags & ~Qt.ItemIsEnabled)

    def reject(self) -> None:
        logger.info("Setup closed on step %d (%s) without finishing", self.current + 1, PAGES[self.current][1])
        super().reject()

    def open_log_folder(self) -> None:
        from pathlib import Path

        path = current_log_path()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))

    # --- state ---

    def load_config_file(self, path: str) -> None:
        """Replace the working config with one loaded from an existing config.yaml."""
        new_state = WizardState.from_config_file(path)
        # Keep any Distribution/branding answers already given; only the
        # config.yaml-shaped half of the state is what "load" means here.
        new_state.wizard = self.state.wizard
        self.state = new_state
        logger.info("Loaded %s", path)

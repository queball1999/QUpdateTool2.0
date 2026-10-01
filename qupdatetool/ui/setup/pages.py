"""
One wizard page per config.yaml section.

Pages read the shared WizardState in initializePage() and write back to it
in validatePage(), the QWizardPage contract (see SectionPage), rather than a
field-binding mini-DSL - that keeps cross-field validation (e.g. "repo is
required unless the provider is generic") straightforward Python.

Every page is a thin view over qupdatetool.config.DEFAULTS' shape; the
generic provider/regex/process-name rules mirror qupdatetool/config.py's own
Config.validate(), because a wizard that validated differently than the
runtime would be worse than no validation at all.

Every field is laid out the same way - a card with a plain-language title,
a short plain-English explanation under it, and the input below (or an on/off
switch on the right). This is meant to be usable by someone who has never
touched config.yaml before.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...logging_utils import get_logger
from ..widgets import Skeleton, ToggleSwitch
from . import generator, validators
from .demo import DemoUpdateWorker
from .workers import ConnectionTestWorker

logger = get_logger("setup")

PROVIDERS = ["github", "gitea", "forgejo", "gitlab", "generic"]
CHANNELS = ["stable", "prerelease", "any"]
INSTALL_MODES = ["silent", "interactive"]
ELEVATE_MODES = ["auto", "always", "never"]
NOTES_SOURCES = ["auto", "notices", "changelog", "release", "none"]
LOG_LEVELS = ["ERROR", "WARNING", "INFO", "DEBUG"]
ASSET_KINDS = "exe, msi, deb, rpm, appimage, pkg, dmg, zip, tar"
PLATFORM_TARGETS = ["windows-latest", "ubuntu-latest", "macos-latest"]


# --- small widget helpers --------------------------------------------------


class Card(QFrame):
    """A rounded panel; one per setting. Clicking it toggles its switch, if it has one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.switch = None

    def mouseReleaseEvent(self, event) -> None:
        if self.switch is not None and self.switch.isEnabled() and event.button() == Qt.LeftButton:
            self.switch.toggle()
        super().mouseReleaseEvent(event)


def card_text(title: str, description: str, required: bool = False) -> QVBoxLayout:
    """A card's bold title (with a red * when required) over its muted description."""
    text = QVBoxLayout()
    text.setSpacing(2)

    header = QHBoxLayout()
    header.setSpacing(4)
    title_label = QLabel(title)
    title_label.setObjectName("CardTitle")
    header.addWidget(title_label)
    if required:
        star = QLabel("*")
        star.setObjectName("Required")
        star.setToolTip("Required")
        header.addWidget(star)
    header.addStretch(1)
    text.addLayout(header)

    if description:
        desc = QLabel(description)
        desc.setObjectName("CardDescription")
        desc.setWordWrap(True)
        text.addWidget(desc)
    return text


def wrap(card: QFrame) -> QVBoxLayout:
    """Cards are handed to pages as layouts, so pages add every field the same way."""
    layout = QVBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(card)
    return layout


def field(title: str, description: str, widget, required: bool = False) -> QVBoxLayout:
    """
    A setting card, laid out the same way everywhere in this wizard:

        Title *
        What this is, in plain English
        [ entry ]

    `widget` may be a single widget (a QLineEdit, QComboBox, ...) or a
    QLayout (e.g. browse_row's line-edit-plus-button row). `required` marks
    the title with a red asterisk - use it only for fields the wizard
    actually refuses to move past when empty (see each page's isComplete()).
    """
    card = Card()
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 12, 16, 14)
    layout.setSpacing(8)
    layout.addLayout(card_text(title, description, required))

    if isinstance(widget, QLayout):
        layout.addLayout(widget)
    elif widget is not None:
        layout.addWidget(widget)
    return wrap(card)


def checkbox_field(checkbox: QCheckBox, description: str) -> QVBoxLayout:
    """
    An on/off setting card: the checkbox's text becomes the card title, with
    the switch on the right. Clicking anywhere on the card flips it.
    """
    card = Card()
    row = QHBoxLayout(card)
    row.setContentsMargins(16, 12, 16, 12)
    row.setSpacing(16)
    row.addLayout(card_text(checkbox.text(), description), 1)

    if isinstance(checkbox, ToggleSwitch):
        card.switch = checkbox
    else:
        checkbox.setText("")
    row.addWidget(checkbox, 0, Qt.AlignVCenter)
    return wrap(card)


def browse_row(line_edit: QLineEdit, *, directory: bool = False, name_filter: str = "All files (*)") -> QHBoxLayout:
    """A text box plus a Browse... button, for picking a file or folder."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(line_edit)

    button = QPushButton("Browse...")

    def on_browse():
        if directory:
            chosen = QFileDialog.getExistingDirectory(line_edit, "Choose a folder", line_edit.text())
        else:
            chosen, _ = QFileDialog.getOpenFileName(line_edit, "Choose a file", line_edit.text(), name_filter)
        if chosen:
            line_edit.setText(chosen)

    button.clicked.connect(on_browse)
    row.addWidget(button)
    return row


def button_row(*buttons: QPushButton) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(0, 4, 0, 4)
    for button in buttons:
        row.addWidget(button)
    row.addStretch(1)
    return row


def intro_label(text: str) -> QLabel:
    """A word-wrapped paragraph at the top of a page."""
    label = QLabel(text)
    label.setObjectName("Intro")
    label.setWordWrap(True)
    return label


def status_label() -> QLabel:
    label = QLabel("")
    label.setObjectName("StatusText")
    label.setWordWrap(True)
    label.hide()
    return label


def set_status(label: QLabel, text: str, state: str = "ok") -> None:
    """Show a status line coloured by state ("ok", "error", "pending"); empty text hides it."""
    label.setText(text)
    label.setProperty("state", state)
    label.style().unpolish(label)
    label.style().polish(label)
    label.setVisible(bool(text))


def csv_to_list(text: str) -> list:
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def list_to_csv(items) -> str:
    return ", ".join(items or [])


class SectionPage(QWidget):
    """
    One wizard step. Keeps QWizardPage's contract so each page reads the same
    as before: setTitle/setSubTitle, initializePage (load from state),
    validatePage (save to state, or refuse), isComplete, completeChanged.
    """

    completeChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # Kept here because the wizard's QStackedWidget reparents the page.
        self._wizard = parent
        self._title = ""
        self._subtitle = ""

    def wizard(self):
        return self._wizard

    @property
    def state(self):
        return self._wizard.state

    def setTitle(self, text: str) -> None:
        self._title = text

    def title(self) -> str:
        return self._title

    def setSubTitle(self, text: str) -> None:
        self._subtitle = text

    def subTitle(self) -> str:
        return self._subtitle

    def initializePage(self) -> None:
        pass

    def validatePage(self) -> bool:
        return True

    def isComplete(self) -> bool:
        return True

    def body(self) -> QVBoxLayout:
        """A scrolling vertical layout for this page, one card per row."""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setObjectName("PageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QWidget()
        container.setObjectName("PageBody")
        layout = QVBoxLayout(container)
        # Right margin keeps cards clear of the scrollbar.
        layout.setContentsMargins(0, 4, 12, 8)
        layout.setSpacing(8)

        scroll.setWidget(container)
        outer.addWidget(scroll)
        return layout


# --- Welcome ---------------------------------------------------------------


class WelcomePage(SectionPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Welcome!")
        self.setSubTitle("Let's get your app's updates set up.")

        layout = self.body()
        layout.addWidget(intro_label(
            "This wizard asks a few simple questions about your app, one step "
            "at a time. Every setting has a short explanation right under its "
            "name, and the steps on the left show where you are."
        ))
        layout.addLayout(field(
            "Near the end",
            "We'll test your settings for real (without downloading or "
            "installing anything) and show you what the update window looks "
            "like. Then you save your files and you're done.",
            None,
        ))
        layout.addLayout(field(
            "Required fields",
            "A red * marks a field the wizard needs before it can move on. "
            "Everything else is optional.",
            None,
        ))

        load_button = QPushButton("Load config.yaml...")
        load_button.clicked.connect(self.on_load)
        self.loaded_label = status_label()
        layout.addLayout(field(
            "Already have a config.yaml?",
            "Load it to start from your existing settings instead of the defaults.",
            button_row(load_button),
        ))
        layout.addWidget(self.loaded_label)
        layout.addStretch(1)

    def on_load(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load config.yaml", "", "YAML files (*.yaml *.yml)")
        if not path:
            return
        try:
            self.wizard().load_config_file(path)
        except Exception as exc:  # noqa: BLE001 - show the developer exactly what went wrong
            logger.warning("Could not load %s: %s", path, exc)
            QMessageBox.critical(self, "Could not load that file", str(exc))
            return
        set_status(self.loaded_label, f"Loaded {path} - look through the next steps to see what it set.")


# --- Application -------------------------------------------------------


class ApplicationPage(SectionPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Your app")
        self.setSubTitle("A few basics about the app being updated.")

        self.name = QLineEdit()
        self.name.setPlaceholderText("My App")
        self.publisher = QLineEdit()
        self.current_version = QLineEdit()
        self.current_version.setPlaceholderText("1.2.3")
        self.executable = QLineEdit()
        self.install_dir = QLineEdit()
        self.process_names = QLineEdit()
        self.process_names.setPlaceholderText("MyApp.exe, MyAppHelper.exe")
        self.support_url = QLineEdit()

        layout = self.body()
        layout.addLayout(field(
            "App name",
            "Required. The name people see, like \"My App\". Shows up in "
            "the update window and in log files.",
            self.name,
            required=True,
        ))
        layout.addLayout(field(
            "Publisher",
            "Your name or your company's name. Not required.",
            self.publisher,
        ))
        layout.addLayout(field(
            "Current version",
            "The version this build is running right now, like 1.2.3. Most "
            "apps pass this in automatically each time they call the updater, "
            "so you can usually leave this blank here.",
            self.current_version,
        ))
        layout.addLayout(field(
            "App executable",
            "The program file that gets closed and reopened during an "
            "update. Click Browse to find it.",
            browse_row(self.executable, name_filter="Executables (*.exe);;All files (*)"),
        ))
        layout.addLayout(field(
            "Install folder",
            "Where your app is installed. You can leave this blank - we'll "
            "work it out from the app executable above.",
            browse_row(self.install_dir, directory=True),
        ))
        layout.addLayout(field(
            "Process names to close",
            "The name of the program (or programs) to close before "
            "installing, separated by commas. For example: MyApp.exe, "
            "MyAppHelper.exe.",
            self.process_names,
        ))
        layout.addLayout(field(
            "Support link",
            "A web page people can visit if an update fails. Not required.",
            self.support_url,
        ))
        layout.addStretch(1)

        self.name.textChanged.connect(self.completeChanged)

    def initializePage(self):
        s = self.state
        self.name.setText(s.get("app.name", ""))
        self.publisher.setText(s.get("app.publisher", ""))
        self.current_version.setText(s.get("app.current_version", ""))
        self.executable.setText(s.get("app.executable", ""))
        self.install_dir.setText(s.get("app.install_dir", ""))
        self.process_names.setText(list_to_csv(s.get("app.process_names", [])))
        self.support_url.setText(s.get("app.support_url", ""))

    def validatePage(self):
        s = self.state
        s.set("app.name", self.name.text().strip())
        s.set("app.publisher", self.publisher.text().strip())
        s.set("app.current_version", self.current_version.text().strip())
        s.set("app.executable", self.executable.text().strip())
        s.set("app.install_dir", self.install_dir.text().strip())
        s.set("app.process_names", csv_to_list(self.process_names.text()))
        s.set("app.support_url", self.support_url.text().strip())
        return True

    def isComplete(self):
        return bool(self.name.text().strip())


# --- Release source ------------------------------------------------------


class SourcePage(SectionPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Where updates come from")
        self.setSubTitle("Tell us where your releases get published.")

        self.provider = QComboBox()
        self.provider.addItems(PROVIDERS)
        self.host = QLineEdit()
        self.host.setPlaceholderText("blank = github.com, or the provider's usual address")
        self.repo = QLineEdit()
        self.repo.setPlaceholderText("owner/name")
        self.channel = QComboBox()
        self.channel.addItems(CHANNELS)
        self.tag_pattern = QLineEdit()
        self.manifest_url = QLineEdit()
        self.download_url = QLineEdit()
        self.token_env = QLineEdit()
        self.token_env.setPlaceholderText("e.g. MYAPP_UPDATE_TOKEN")
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 600)
        self.retries = QSpinBox()
        self.retries.setRange(0, 10)
        self.verify_tls = ToggleSwitch("Verify TLS certificates")

        layout = self.body()
        layout.addLayout(field(
            "Release host",
            "Where your releases live. Most projects should pick \"github\".",
            self.provider,
        ))
        layout.addLayout(field(
            "Custom server address",
            "Only needed if you're self-hosting, like your own GitLab or "
            "Gitea server. Leave this blank for github.com.",
            self.host,
        ))
        layout.addLayout(field(
            "Repository",
            "Required, unless you're using the \"generic\" release host "
            "below. Your repository, written as owner/name - for example "
            "octocat/MyApp.",
            self.repo,
            required=True,
        ))
        layout.addLayout(field(
            "Which releases count",
            "\"Stable\" skips beta releases. Pick \"prerelease\" for a beta "
            "channel, or \"any\" to always take whatever is newest.",
            self.channel,
        ))
        layout.addLayout(field(
            "Only match tags like...",
            "Not required. Only consider release tags matching this "
            "pattern. Leave this blank unless one repository publishes more "
            "than one product.",
            self.tag_pattern,
        ))
        layout.addLayout(field(
            "Manifest URL (generic host only)",
            "Only used when \"Release host\" above is set to \"generic\": a "
            "JSON file listing your releases. You need this or the Download "
            "URL below, not both.",
            self.manifest_url,
        ))
        layout.addLayout(field(
            "Download URL (generic host only)",
            "Only used with the \"generic\" release host: a direct link to "
            "the file to download. You need this or the Manifest URL above, "
            "not both.",
            self.download_url,
        ))
        layout.addLayout(field(
            "Access token variable (private repos only)",
            "If your repository is private, put the name of an environment "
            "variable that holds an access token here. Never put the token "
            "itself in this file.",
            self.token_env,
        ))
        layout.addLayout(field(
            "Network timeout (seconds)",
            "How long to wait for the release server to respond before "
            "giving up.",
            self.timeout,
        ))
        layout.addLayout(field(
            "Retries",
            "How many times to retry a failed network request.",
            self.retries,
        ))
        layout.addLayout(checkbox_field(
            self.verify_tls,
            "Keep this on. It makes sure the connection to your release "
            "server is secure and hasn't been tampered with.",
        ))

        self.tag_status = status_label()
        layout.addWidget(self.tag_status)
        layout.addStretch(1)

        self.provider.currentTextChanged.connect(self.completeChanged)
        self.repo.textChanged.connect(self.completeChanged)
        self.manifest_url.textChanged.connect(self.completeChanged)
        self.download_url.textChanged.connect(self.completeChanged)
        self.tag_pattern.textChanged.connect(self.on_tag_pattern_changed)

    def on_tag_pattern_changed(self, text):
        result = validators.check_regex(text)
        set_status(self.tag_status, result.message if text.strip() else "", "ok" if result.ok else "error")

    def initializePage(self):
        s = self.state
        self.provider.setCurrentText(s.get("source.provider", "github"))
        self.host.setText(s.get("source.host", ""))
        self.repo.setText(s.get("source.repo", ""))
        self.channel.setCurrentText(s.get("source.channel", "stable"))
        self.tag_pattern.setText(s.get("source.tag_pattern", ""))
        self.manifest_url.setText(s.get("source.manifest_url", ""))
        self.download_url.setText(s.get("source.download_url", ""))
        self.token_env.setText(s.get("source.token_env", ""))
        self.timeout.setValue(int(s.get("source.timeout", 30)))
        self.retries.setValue(int(s.get("source.retries", 3)))
        self.verify_tls.setChecked(bool(s.get("source.verify_tls", True)))

    def validatePage(self):
        result = validators.check_regex(self.tag_pattern.text().strip())
        if not result.ok:
            QMessageBox.warning(self, "That tag pattern isn't valid", result.message)
            return False

        s = self.state
        s.set("source.provider", self.provider.currentText())
        s.set("source.host", self.host.text().strip())
        s.set("source.repo", self.repo.text().strip())
        s.set("source.channel", self.channel.currentText())
        s.set("source.tag_pattern", self.tag_pattern.text().strip())
        s.set("source.manifest_url", self.manifest_url.text().strip())
        s.set("source.download_url", self.download_url.text().strip())
        s.set("source.token_env", self.token_env.text().strip())
        s.set("source.timeout", self.timeout.value())
        s.set("source.retries", self.retries.value())
        s.set("source.verify_tls", self.verify_tls.isChecked())
        return True

    def isComplete(self):
        if self.provider.currentText() == "generic":
            return bool(self.manifest_url.text().strip() or self.download_url.text().strip())
        return bool(self.repo.text().strip())


# --- Assets ----------------------------------------------------------------


class AssetsPage(SectionPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Which file to install")
        self.setSubTitle("You can usually skip this page - we'll pick the right file automatically.")

        self.pattern = QLineEdit()
        self.exclude_pattern = QLineEdit()
        self.preference = QLineEdit()
        self.preference.setPlaceholderText(f"e.g. deb,appimage,tar  (choices: {ASSET_KINDS})")
        self.match_arch = ToggleSwitch("Skip files built for a different type of processor")

        layout = self.body()
        layout.addLayout(field(
            "Only match files like...",
            "Not required. Only consider release files whose name matches "
            "this pattern. Leave blank to let us pick automatically.",
            self.pattern,
        ))
        layout.addLayout(field(
            "Never match files like...",
            "Not required. Skip any release file whose name matches this.",
            self.exclude_pattern,
        ))
        layout.addLayout(field(
            "Preferred file types",
            "Not required. List file types in the order you'd like them "
            "tried, like deb, appimage, tar.",
            self.preference,
        ))
        layout.addLayout(checkbox_field(
            self.match_arch,
            "Recommended. Avoids installing, say, an ARM build on an Intel "
            "machine.",
        ))

        self.status = status_label()
        layout.addWidget(self.status)
        layout.addStretch(1)

        self.pattern.textChanged.connect(self.on_changed)
        self.exclude_pattern.textChanged.connect(self.on_changed)

    def on_changed(self, _text=""):
        for line_edit in (self.pattern, self.exclude_pattern):
            result = validators.check_regex(line_edit.text().strip())
            if not result.ok:
                set_status(self.status, f"{line_edit.text()!r}: {result.message}", "error")
                return
        set_status(self.status, "")

    def initializePage(self):
        s = self.state
        self.pattern.setText(s.get("assets.pattern", ""))
        self.exclude_pattern.setText(s.get("assets.exclude_pattern", ""))
        self.preference.setText(list_to_csv(s.get("assets.preference", [])))
        self.match_arch.setChecked(bool(s.get("assets.match_arch", True)))
        self.on_changed()

    def validatePage(self):
        for line_edit, label in ((self.pattern, "\"only match\" pattern"), (self.exclude_pattern, "\"never match\" pattern")):
            result = validators.check_regex(line_edit.text().strip())
            if not result.ok:
                QMessageBox.warning(self, f"That {label} isn't valid", result.message)
                return False

        s = self.state
        s.set("assets.pattern", self.pattern.text().strip())
        s.set("assets.exclude_pattern", self.exclude_pattern.text().strip())
        s.set("assets.preference", csv_to_list(self.preference.text()))
        s.set("assets.match_arch", self.match_arch.isChecked())
        return True


# --- Security ----------------------------------------------------------


class SecurityPage(SectionPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Keeping updates safe")
        self.setSubTitle("This makes sure nobody can slip a fake update to your users.")

        self.require_signature = ToggleSwitch("Require a valid signature on every release")
        self.require_checksum = ToggleSwitch("Require a checksum match")
        self.checksum_file = QLineEdit()
        self.public_key_file = QLineEdit()
        self.allowed_fingerprints = QLineEdit()
        self.allow_gpg_fallback = ToggleSwitch("Allow falling back to the gpg program for unusual key types")

        layout = self.body()
        layout.addLayout(checkbox_field(
            self.require_signature,
            "Strongly recommended, and on by default. Refuses to install "
            "anything that isn't signed with your key.",
        ))
        layout.addLayout(checkbox_field(
            self.require_checksum,
            "Also recommended. Double checks the downloaded file matches "
            "what was published, on top of the signature check.",
        ))
        layout.addLayout(field(
            "Checksum file name",
            "The name of the checksum file published alongside your "
            "release. Most projects can leave this as the default.",
            self.checksum_file,
        ))
        detect_button = QPushButton("Check this key file")
        detect_button.clicked.connect(self.on_detect)
        self.key_status = status_label()
        key_box = QVBoxLayout()
        key_box.setSpacing(6)
        key_box.addLayout(browse_row(self.public_key_file, name_filter="Public keys (*.asc *.pgp *.gpg);;All files (*)"))
        key_box.addLayout(button_row(detect_button))
        key_box.addWidget(self.key_status)
        layout.addLayout(field(
            "Your public signing key",
            "The public key file (ending in .asc) used to check that a "
            "release really came from you. Checking it fills in the "
            "fingerprints below.",
            key_box,
        ))

        layout.addLayout(field(
            "Allowed key fingerprints",
            "Not required, but an extra safety net: only trust signatures "
            "from these exact keys, separated by commas.",
            self.allowed_fingerprints,
        ))
        layout.addLayout(checkbox_field(
            self.allow_gpg_fallback,
            "Leave this on unless you have a reason not to. It helps with "
            "a few uncommon key types our built-in checker can't read.",
        ))
        layout.addStretch(1)

    def on_detect(self):
        result = validators.check_public_key_file(self.public_key_file.text().strip())
        message = result.message + (f"\nFingerprints: {result.detail}" if result.detail else "")
        set_status(self.key_status, message, "ok" if result.ok else "error")
        logger.info("Key file check: %s", message.replace("\n", " - "))

        if result.ok and result.detail:
            existing = set(csv_to_list(self.allowed_fingerprints.text()))
            existing.update(part.strip() for part in result.detail.split(",") if part.strip())
            self.allowed_fingerprints.setText(list_to_csv(sorted(existing)))

    def initializePage(self):
        s = self.state
        self.require_signature.setChecked(bool(s.get("security.require_signature", True)))
        self.require_checksum.setChecked(bool(s.get("security.require_checksum", True)))
        self.checksum_file.setText(s.get("security.checksum_file", "SHA256SUMS.txt"))
        self.public_key_file.setText(s.get("security.public_key_file", ""))
        self.allowed_fingerprints.setText(list_to_csv(s.get("security.allowed_fingerprints", [])))
        self.allow_gpg_fallback.setChecked(bool(s.get("security.allow_gpg_fallback", True)))
        set_status(self.key_status, "")

    def validatePage(self):
        s = self.state
        s.set("security.require_signature", self.require_signature.isChecked())
        s.set("security.require_checksum", self.require_checksum.isChecked())
        s.set("security.checksum_file", self.checksum_file.text().strip())
        s.set("security.public_key_file", self.public_key_file.text().strip())
        s.set("security.allowed_fingerprints", csv_to_list(self.allowed_fingerprints.text()))
        s.set("security.allow_gpg_fallback", self.allow_gpg_fallback.isChecked())
        return True


# --- Install ---------------------------------------------------------------


class InstallPage(SectionPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Installing the update")
        self.setSubTitle("How the downloaded file gets applied.")

        self.mode = QComboBox()
        self.mode.addItems(INSTALL_MODES)
        self.elevate = QComboBox()
        self.elevate.addItems(ELEVATE_MODES)
        self.target_dir = QLineEdit()
        self.download_dir = QLineEdit()
        self.keep_download = ToggleSwitch("Keep the downloaded file after installing")

        layout = self.body()
        layout.addLayout(field(
            "How the installer runs",
            "\"Silent\" installs with no prompts - best for most apps. "
            "\"Interactive\" shows your normal installer screens instead.",
            self.mode,
        ))
        layout.addLayout(field(
            "Admin permission",
            "\"Auto\" only asks for admin rights if the install folder "
            "actually needs them. That's the right choice for almost "
            "everyone.",
            self.elevate,
        ))
        layout.addLayout(field(
            "Unpack folder",
            "Only used for .zip or AppImage releases. Leave this blank to "
            "use the app's own folder.",
            browse_row(self.target_dir, directory=True),
        ))
        layout.addLayout(field(
            "Download folder",
            "Where the update file is saved while it downloads. Leave "
            "blank to use the default temporary location.",
            browse_row(self.download_dir, directory=True),
        ))
        layout.addLayout(checkbox_field(
            self.keep_download,
            "Mostly useful for debugging. Most apps should leave this off "
            "so the download gets cleaned up automatically.",
        ))
        layout.addStretch(1)

    def initializePage(self):
        s = self.state
        self.mode.setCurrentText(s.get("install.mode", "silent"))
        self.elevate.setCurrentText(s.get("install.elevate", "auto"))
        self.target_dir.setText(s.get("install.target_dir", ""))
        self.download_dir.setText(s.get("install.download_dir", ""))
        self.keep_download.setChecked(bool(s.get("install.keep_download", False)))

    def validatePage(self):
        s = self.state
        s.set("install.mode", self.mode.currentText())
        s.set("install.elevate", self.elevate.currentText())
        s.set("install.target_dir", self.target_dir.text().strip())
        s.set("install.download_dir", self.download_dir.text().strip())
        s.set("install.keep_download", self.keep_download.isChecked())
        return True


# --- Process -----------------------------------------------------------


class ProcessPage(SectionPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Closing and reopening your app")
        self.setSubTitle("Your app usually needs to be closed while it's being updated.")

        self.stop_parent = ToggleSwitch("Close the running app before installing")
        self.stop_timeout = QSpinBox()
        self.stop_timeout.setRange(1, 600)
        self.relaunch = ToggleSwitch("Reopen the app after updating")
        self.wait_before_relaunch = QSpinBox()
        self.wait_before_relaunch.setRange(0, 60)

        layout = self.body()
        layout.addLayout(checkbox_field(
            self.stop_parent,
            "Recommended. Installing over a running app usually fails or "
            "corrupts the install, so this should normally stay on.",
        ))
        layout.addLayout(field(
            "How long to wait (seconds)",
            "How long to give the app a chance to close on its own before "
            "we force it.",
            self.stop_timeout,
        ))
        layout.addLayout(checkbox_field(
            self.relaunch,
            "Turn this off only if you'd rather the user reopen the app "
            "themselves.",
        ))
        layout.addLayout(field(
            "Pause before reopening (seconds)",
            "A short pause after installing, before the app starts back "
            "up. Gives the filesystem a moment to settle.",
            self.wait_before_relaunch,
        ))

        check_button = QPushButton("Check now")
        check_button.clicked.connect(self.on_check)
        self.status = status_label()
        check_box = QVBoxLayout()
        check_box.setSpacing(6)
        check_box.addLayout(button_row(check_button))
        check_box.addWidget(self.status)
        layout.addLayout(field(
            "Is the app running right now?",
            "Looks for the process names from the \"Your app\" step on this "
            "computer. Nothing is closed.",
            check_box,
        ))
        layout.addStretch(1)

    def on_check(self):
        result = validators.check_process_names(self.state.get("app.process_names", []))
        set_status(self.status, result.message, "ok" if result.ok else "error")
        logger.info("Process check: %s", result.message)

    def initializePage(self):
        s = self.state
        self.stop_parent.setChecked(bool(s.get("process.stop_parent", True)))
        self.stop_timeout.setValue(int(s.get("process.stop_timeout", 30)))
        self.relaunch.setChecked(bool(s.get("process.relaunch", True)))
        self.wait_before_relaunch.setValue(int(s.get("process.wait_before_relaunch", 2)))
        set_status(self.status, "")

    def validatePage(self):
        s = self.state
        s.set("process.stop_parent", self.stop_parent.isChecked())
        s.set("process.stop_timeout", self.stop_timeout.value())
        s.set("process.relaunch", self.relaunch.isChecked())
        s.set("process.wait_before_relaunch", self.wait_before_relaunch.value())
        return True


# --- Notes -------------------------------------------------------------


class NotesPage(SectionPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("What's new text")
        self.setSubTitle("Where the \"what's new\" message in the update window comes from.")

        self.source = QComboBox()
        self.source.addItems(NOTES_SOURCES)
        self.notices_dir = QLineEdit()

        layout = self.body()
        layout.addLayout(field(
            "Where to find it",
            "\"Auto\" tries a notices folder first, then your CHANGELOG, "
            "then the release description on your git host - whichever it "
            "finds first. That's the right choice for most apps.",
            self.source,
        ))
        layout.addLayout(field(
            "Notices folder",
            "Only used with \"notices\" above: a folder in your "
            "repository with one small text file per release.",
            self.notices_dir,
        ))
        layout.addStretch(1)

    def initializePage(self):
        s = self.state
        self.source.setCurrentText(s.get("notes.source", "auto"))
        self.notices_dir.setText(s.get("notes.notices_dir", "notices"))

    def validatePage(self):
        s = self.state
        s.set("notes.source", self.source.currentText())
        s.set("notes.notices_dir", self.notices_dir.text().strip())
        return True


# --- UI & logging --------------------------------------------------------


class UiLoggingPage(SectionPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Window look & logs")
        self.setSubTitle("Cosmetics for the update window, and where log files go.")

        self.window_title = QLineEdit()
        self.accent_color = QLineEdit()
        self.accent_color.setPlaceholderText("#4C8BF5")
        self.auto_close_seconds = QSpinBox()
        self.auto_close_seconds.setRange(0, 60)
        self.gui_default = ToggleSwitch("Show the update window by default")
        self.log_level = QComboBox()
        self.log_level.addItems(LOG_LEVELS)
        self.log_file = QLineEdit()
        self.log_file.setPlaceholderText("blank = default location, 'none' to turn off")

        layout = self.body()
        layout.addLayout(field(
            "Window title",
            "The title bar text of the update window. Leave blank to use "
            "\"<Your app name> Updater\".",
            self.window_title,
        ))
        layout.addLayout(field(
            "Accent color",
            "A hex color code, like #4C8BF5, used for the progress bar. "
            "Not required.",
            self.accent_color,
        ))
        layout.addLayout(field(
            "Auto-close after success (seconds)",
            "How long the window stays open after a successful update "
            "before closing itself. Use 0 to make the user close it by "
            "hand.",
            self.auto_close_seconds,
        ))
        layout.addLayout(checkbox_field(
            self.gui_default,
            "Most apps leave this off and pass --gui explicitly only when "
            "the user asks to check for updates.",
        ))
        layout.addLayout(field(
            "Log detail level",
            "How much detail gets written to the log file. \"INFO\" is a "
            "good default; use \"DEBUG\" when tracking down a problem.",
            self.log_level,
        ))
        layout.addLayout(field(
            "Log file or folder",
            "Where log files are written. Point this at your app's own "
            "log folder so everything ends up in one place.",
            self.log_file,
        ))
        layout.addStretch(1)

    def initializePage(self):
        s = self.state
        self.window_title.setText(s.get("ui.window_title", ""))
        self.accent_color.setText(s.get("ui.accent_color", ""))
        self.auto_close_seconds.setValue(int(s.get("ui.auto_close_seconds", 3)))
        self.gui_default.setChecked(bool(s.get("ui.gui", False)))
        self.log_level.setCurrentText(s.get("logging.level", "INFO"))
        self.log_file.setText(s.get("logging.file", ""))

    def validatePage(self):
        s = self.state
        s.set("ui.window_title", self.window_title.text().strip())
        s.set("ui.accent_color", self.accent_color.text().strip())
        s.set("ui.auto_close_seconds", self.auto_close_seconds.value())
        s.set("ui.gui", self.gui_default.isChecked())
        s.set("logging.level", self.log_level.currentText())
        s.set("logging.file", self.log_file.text().strip())
        return True


# --- Distribution / branded CI build --------------------------------------


class DistributionPage(SectionPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Building a ready-to-ship updater")
        self.setSubTitle("Optional - skip this page if you just want a config.yaml for now.")

        self.enabled = ToggleSwitch("Build a signed updater.exe automatically in CI")
        self.enabled.toggled.connect(self.on_toggled)

        self.icon_path = QLineEdit()
        self.icon_path.setPlaceholderText("e.g. assets/MyApp.ico")
        self.qupdatetool_repo = QLineEdit()
        self.qupdatetool_ref = QLineEdit()
        self.key_secret_name = QLineEdit()
        self.sign_command = QLineEdit()
        self.sign_command.setPlaceholderText("optional, e.g. azuresigntool sign ... {file}")
        self.build_gui = ToggleSwitch("Include the update window (needs a bit more space)")

        self.platform_boxes = {name: QCheckBox(name) for name in PLATFORM_TARGETS}
        platforms_row = QHBoxLayout()
        platforms_row.setContentsMargins(0, 0, 0, 0)
        for box in self.platform_boxes.values():
            platforms_row.addWidget(box)
        platforms_row.addStretch(1)

        self.lock_channel = ToggleSwitch("Always use the release channel above, no exceptions")
        self.lock_tag_pattern = ToggleSwitch("Always use the tag pattern above, no exceptions")
        self.lock_elevate = ToggleSwitch("Always use the admin-permission setting above, no exceptions")

        # Every card below greys out while the switch above is off.
        self.group = QWidget()
        group_layout = QVBoxLayout(self.group)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setSpacing(8)
        group_layout.addLayout(field(
            "Icon file",
            "The path to your app's icon inside your own repository, like "
            "assets/MyApp.ico. Used for the updater's icon.",
            self.icon_path,
        ))
        group_layout.addLayout(field(
            "QUpdateTool repository to use",
            "Almost always the default. Only change this if you're using "
            "your own fork of QUpdateTool.",
            self.qupdatetool_repo,
        ))
        group_layout.addLayout(field(
            "QUpdateTool version (branch or tag)",
            "Which version of QUpdateTool to build with. \"main\" is the "
            "latest.",
            self.qupdatetool_ref,
        ))
        group_layout.addLayout(field(
            "Signing-key secret name",
            "The name of the CI secret holding your public signing key. "
            "You'll add this secret in your repository settings.",
            self.key_secret_name,
        ))
        group_layout.addLayout(field(
            "Code-signing command",
            "Not required. A command that signs the finished .exe, if your "
            "organization code-signs binaries.",
            self.sign_command,
        ))
        group_layout.addLayout(checkbox_field(
            self.build_gui,
            "Turn this on if you want to show a progress window to users. "
            "Leave it off for a smaller, background-only updater.",
        ))
        group_layout.addLayout(field(
            "Which computers to build for",
            "Pick every operating system you ship your app on. Each one "
            "needs its own build.",
            platforms_row,
        ))
        group_layout.addLayout(checkbox_field(
            self.lock_channel,
            "Stops a stray config.yaml file from quietly moving users onto "
            "a beta channel.",
        ))
        group_layout.addLayout(checkbox_field(self.lock_tag_pattern, "Same idea, for the tag pattern."))
        group_layout.addLayout(checkbox_field(self.lock_elevate, "Same idea, for the admin-permission setting."))

        layout = self.body()
        layout.addLayout(checkbox_field(
            self.enabled,
            "Generates a GitHub Actions workflow and a brand file that build "
            "an updater with your app's name, icon and signing key baked in.",
        ))
        layout.addWidget(self.group)
        layout.addStretch(1)

    def on_toggled(self, checked):
        self.group.setEnabled(checked)

    def initializePage(self):
        w = self.state.wizard
        self.enabled.setChecked(bool(w.get("branding_enabled", False)))
        self.icon_path.setText(w.get("icon_path", ""))
        self.qupdatetool_repo.setText(w.get("qupdatetool_repo", "queball1999/QUpdateTool2.0"))
        self.qupdatetool_ref.setText(w.get("qupdatetool_ref", "main"))
        self.key_secret_name.setText(w.get("key_secret_name", "RELEASE_PUBLIC_KEY"))
        self.sign_command.setText(w.get("sign_command", ""))
        self.build_gui.setChecked(bool(w.get("build_gui", False)))

        targets = set(w.get("target_platforms", ["windows-latest"]))
        for name, box in self.platform_boxes.items():
            box.setChecked(name in targets)

        locks = set(w.get("extra_lock_fields", []))
        self.lock_channel.setChecked("source.channel" in locks)
        self.lock_tag_pattern.setChecked("source.tag_pattern" in locks)
        self.lock_elevate.setChecked("install.elevate" in locks)

        self.group.setEnabled(self.enabled.isChecked())

    def validatePage(self):
        w = self.state.wizard
        w["branding_enabled"] = self.enabled.isChecked()
        w["icon_path"] = self.icon_path.text().strip()
        w["qupdatetool_repo"] = self.qupdatetool_repo.text().strip() or "queball1999/QUpdateTool2.0"
        w["qupdatetool_ref"] = self.qupdatetool_ref.text().strip() or "main"
        w["key_secret_name"] = self.key_secret_name.text().strip() or "RELEASE_PUBLIC_KEY"
        w["sign_command"] = self.sign_command.text().strip()
        w["build_gui"] = self.build_gui.isChecked()
        w["target_platforms"] = [name for name, box in self.platform_boxes.items() if box.isChecked()] or ["windows-latest"]

        locks = []
        if self.lock_channel.isChecked():
            locks.append("source.channel")
        if self.lock_tag_pattern.isChecked():
            locks.append("source.tag_pattern")
        if self.lock_elevate.isChecked():
            locks.append("install.elevate")
        w["extra_lock_fields"] = locks
        return True


# --- Validate ------------------------------------------------------------


RESULT_MARKS = {"ok": "✓", "error": "✕", "pending": "•"}


class ResultRow(Card):
    """One check on the Validate page: a status mark, its name, and what it found."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 16, 10)
        row.setSpacing(12)

        self.icon = QLabel()
        self.icon.setObjectName("ResultIcon")
        self.icon.setFixedWidth(18)
        self.icon.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        row.addWidget(self.icon, 0, Qt.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(4)
        self.title = QLabel(title)
        self.title.setObjectName("CardTitle")
        self.message = QLabel()
        self.message.setObjectName("CardDescription")
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.skeleton = Skeleton(bars=((0.55, 10),), gap=0)
        text.addWidget(self.title)
        text.addWidget(self.message)
        text.addWidget(self.skeleton)
        row.addLayout(text, 1)

        self.set_pending("")

    def set_mark(self, state: str) -> None:
        self.icon.setText(RESULT_MARKS[state])
        self.icon.setProperty("state", state)
        self.icon.style().unpolish(self.icon)
        self.icon.style().polish(self.icon)

    def set_pending(self, message: str) -> None:
        self.set_mark("pending")
        self.message.setText(message)
        self.message.setVisible(bool(message))
        self.skeleton.show()

    def set_result(self, ok: bool, message: str) -> None:
        self.set_mark("ok" if ok else "error")
        self.message.setText(message)
        self.message.show()
        self.skeleton.hide()


class ValidatePage(SectionPage):
    # (key, title) for each check, in the order they're shown.
    CHECKS = (
        ("executable", "App executable"),
        ("install_dir", "Install folder"),
        ("processes", "Process names"),
        ("asset_pattern", "Asset pattern"),
        ("exclude_pattern", "Exclude pattern"),
        ("tag_pattern", "Tag pattern"),
        ("public_key", "Public key file"),
        ("source", "Release source"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Let's check everything works")
        self.setSubTitle("We'll test your settings for real - nothing gets downloaded or installed.")

        layout = self.body()
        layout.addWidget(intro_label(
            "This checks your file paths, whether the app is currently "
            "running, and makes one real, read-only connection to your "
            "release source to see what it finds. It runs as soon as you "
            "open this step."
        ))

        self.run_button = QPushButton("Run again")
        self.run_button.clicked.connect(self.on_run)
        layout.addLayout(button_row(self.run_button))

        self.rows = {}
        for key, title in self.CHECKS:
            self.rows[key] = ResultRow(title)
            layout.addWidget(self.rows[key])
        layout.addStretch(1)

        self._has_run = False
        self._worker = None

    def initializePage(self):
        # Run once the page is on screen, so the skeletons are seen pulsing.
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, self.on_run)

    def report(self, key, result, detail_in_message: bool = True):
        message = result.message
        if detail_in_message and getattr(result, "detail", ""):
            message += f" ({result.detail})"
        self.rows[key].set_result(result.ok, message)
        log = logger.info if result.ok else logger.warning
        log("Check %s: %s - %s", dict(self.CHECKS)[key], "ok" if result.ok else "problem", message)

    def on_run(self):
        if self._worker is not None and self._worker.isRunning():
            return

        s = self.state
        logger.info("Running checks")
        self.report("executable", validators.check_path(s.get("app.executable", ""), "file"))
        self.report("install_dir", validators.check_path(s.get("app.install_dir", ""), "dir"))
        self.report("processes", validators.check_process_names(s.get("app.process_names", [])))
        self.report("asset_pattern", validators.check_regex(s.get("assets.pattern", "")))
        self.report("exclude_pattern", validators.check_regex(s.get("assets.exclude_pattern", "")))
        self.report("tag_pattern", validators.check_regex(s.get("source.tag_pattern", "")))
        self.report("public_key", validators.check_public_key_file(s.get("security.public_key_file", "")))

        self.rows["source"].set_pending("Checking (read-only, nothing is downloaded)...")
        self.run_button.setEnabled(False)

        self._worker = ConnectionTestWorker(s.to_config(), parent=self)
        self._worker.finished_check.connect(self.on_connection_result)
        self._worker.start()

        self._has_run = True
        self.completeChanged.emit()

    def on_connection_result(self, result):
        if result.ok and result.latest_version:
            result.message += f" (latest {result.latest_version}, file: {result.asset_name or 'n/a'})"
        self.report("source", result, detail_in_message=not result.ok)
        self.run_button.setEnabled(True)

    def isComplete(self):
        return self._has_run


# --- Demo ------------------------------------------------------------------


class DemoPage(SectionPage):
    """
    Shows the real update dialogs without a real update happening.

    "Live preview" runs the actual UpdateWorker in --check-only mode, which
    never downloads or installs anything - it's just the real check() call.
    The other four buttons drive the same UpdaterWindow with the scripted
    DemoUpdateWorker instead (demo.py), entirely offline.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("See it in action")
        self.setSubTitle("This is what your users will see - try each one.")

        layout = self.body()

        live = QPushButton("Check for real updates")
        live.setObjectName("Primary")
        live.clicked.connect(lambda: self.launch(check_only=True, live=True))
        layout.addLayout(field(
            "A real check (safe)",
            "Makes one read-only check against the release source you set "
            "up and shows the result in the real update window. It never "
            "downloads or installs anything.",
            button_row(live),
        ))

        pretend = QVBoxLayout()
        pretend.setSpacing(6)
        for text, scenario in (
            ("An update is found and installs fine", "success"),
            ("Already up to date", "up_to_date"),
            ("The update fails to verify", "failed"),
            ("The user cancels mid-download", "cancelled"),
        ):
            button = QPushButton(text)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.clicked.connect(lambda _checked=False, s=scenario: self.launch(scenario=s))
            pretend.addWidget(button)
        layout.addLayout(field(
            "Pretend runs",
            "Scripted runs of the same window, so you can see every outcome "
            "even before your first real release. Nothing touches the network.",
            pretend,
        ))
        layout.addStretch(1)

    def launch(self, scenario: str = "success", check_only: bool = False, live: bool = False):
        from ..window import UpdaterWindow

        config = self.state.to_config()
        if not live:
            config.set("meta.demo_scenario", scenario)

        logger.info("Demo: %s", "live check" if live else f"pretend run '{scenario}'")
        window = UpdaterWindow(
            config,
            check_only=check_only,
            parent=self,
            worker_class=None if live else DemoUpdateWorker,
        )
        window.exec()
        logger.info("Demo window closed with exit code %s", window.exit_code)


# --- Export ------------------------------------------------------------


class ExportPage(SectionPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Save your files")
        self.setSubTitle("Here's what we put together. Take a look, then save it.")

        self.file_picker = QComboBox()
        self.file_picker.currentTextChanged.connect(self.on_preview_changed)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = self.preview.font()
        font.setFamily("Consolas")
        self.preview.setFont(font)
        self.preview.setMinimumHeight(300)
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        zip_button = QPushButton("Save as ZIP...")
        zip_button.setObjectName("Primary")
        zip_button.clicked.connect(self.on_save_zip)
        folder_button = QPushButton("Save to a folder...")
        folder_button.clicked.connect(self.on_save_folder)

        preview_box = QVBoxLayout()
        preview_box.setSpacing(8)
        preview_box.addWidget(self.file_picker)
        preview_box.addWidget(self.preview, 1)

        layout = self.body()
        layout.addLayout(field(
            "Preview",
            "Pick a file to see exactly what will be saved.",
            preview_box,
        ))
        layout.addLayout(button_row(zip_button, folder_button))

        self._rendered = {}

    def initializePage(self):
        s = self.state
        self._rendered = {"config.yaml": generator.render_config_yaml(s)}
        if s.wizard.get("branding_enabled"):
            self._rendered["updater-brand.yaml"] = generator.render_brand_yaml(s)
            self._rendered[".github/workflows/build-updater.yml"] = generator.render_ci_yaml(s)
        self._rendered["README.md"] = generator.render_readme(s)

        self.file_picker.blockSignals(True)
        self.file_picker.clear()
        self.file_picker.addItems(list(self._rendered))
        self.file_picker.blockSignals(False)
        self.on_preview_changed(self.file_picker.currentText())

    def on_preview_changed(self, name):
        self.preview.setPlainText(self._rendered.get(name, ""))

    def on_save_zip(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save your files", "qupdatetool-setup.zip", "Zip files (*.zip)")
        if not path:
            return
        try:
            members = generator.build_export_bundle(self.state, path)
        except OSError as exc:
            logger.error("Could not write %s: %s", path, exc)
            QMessageBox.critical(self, "Could not save", f"{path}\n\n{exc}")
            return
        logger.info("Saved %s (%s)", path, ", ".join(members))
        QMessageBox.information(self, "Saved", f"Wrote {path}\n\nIt contains:\n" + "\n".join(members))

    def on_save_folder(self):
        directory = QFileDialog.getExistingDirectory(self, "Save your files to")
        if not directory:
            return

        from pathlib import Path

        written = []
        try:
            for name, content in self._rendered.items():
                target = Path(directory) / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                written.append(str(target))
        except OSError as exc:
            logger.error("Could not write to %s: %s", directory, exc)
            QMessageBox.critical(self, "Could not save", f"{directory}\n\n{exc}")
            return

        logger.info("Saved %s", ", ".join(written))
        QMessageBox.information(self, "Saved", "Wrote:\n" + "\n".join(written))

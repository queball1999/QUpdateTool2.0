"""
Windows 11 style dark/light theme with the system accent colour, shared by
the setup wizard and the update window.

Ported from the syncro-customer-vars-editor theme (itself from QSnippet's
theme_manager): same palette keys and QSS structure, trimmed to the widgets
QUpdateTool uses.

Nothing at module level imports PySide6, so the palette and build_qss() can
be tested on a machine without the GUI extra; apply_theme() imports Qt when
it's called.
"""

from __future__ import annotations

from ..logging_utils import get_logger

logger = get_logger("ui")

THEMES = {
    "dark": {
        "window": "#202020",
        "panel": "#2b2b2b",
        "sidebar": "#1b1b1b",
        "card": "rgba(255, 255, 255, 0.05)",
        "card_hover": "rgba(255, 255, 255, 0.08)",
        "input": "rgba(255, 255, 255, 0.06)",
        "input_solid": "#2d2d2d",
        "input_focus": "rgba(255, 255, 255, 0.11)",
        "hover": "#393939",
        "text": "#ffffff",
        "text_muted": "rgba(255, 255, 255, 0.6)",
        "text_muted_solid": "#9e9e9e",
        "accent": "#60cdff",
        "border": "rgba(255, 255, 255, 0.10)",
        "selected": "#3a3a3a",
        "scrollbar": "rgba(255, 255, 255, 0.18)",
        "scrollbar_hover": "rgba(255, 255, 255, 0.38)",
        "success": "#6cc46c",
        "warning": "#ffc107",
        "danger": "#fc4f4f",
        "on_accent": "#000000",  # recomputed from the accent in colours_for
        "switch_off": "#8a8a8a",
    },
    "light": {
        "window": "#f3f3f3",
        "panel": "#ffffff",
        "sidebar": "#ebebeb",
        "card": "rgba(0, 0, 0, 0.04)",
        "card_hover": "rgba(0, 0, 0, 0.07)",
        "input": "#ffffff",
        "input_solid": "#ffffff",
        "input_focus": "#f5f5f5",
        "hover": "#e5e5e5",
        "text": "#1c1c1c",
        "text_muted": "rgba(0, 0, 0, 0.55)",
        "text_muted_solid": "#6e6e6e",
        "accent": "#0067c0",
        "border": "rgba(0, 0, 0, 0.12)",
        "selected": "#dcdcdc",
        "scrollbar": "rgba(0, 0, 0, 0.2)",
        "scrollbar_hover": "rgba(0, 0, 0, 0.38)",
        "success": "#157347",
        "warning": "#9a6b00",
        "danger": "#c42b1c",
        "on_accent": "#ffffff",
        "switch_off": "#7a7a7a",
    },
}

# The colours of the theme last applied, for widgets that paint themselves
# (the toggle switch). Starts as dark so a widget built before apply_theme
# still draws something sensible.
active = dict(THEMES["dark"])


def is_hex_colour(value: str) -> bool:
    text = (value or "").strip()
    return len(text) == 7 and text.startswith("#") and all(
        ch in "0123456789abcdefABCDEF" for ch in text[1:]
    )


def colours_for(name: str, accent: str = "") -> dict:
    """The palette for "dark" or "light", with accent swapped in when it's a hex colour."""
    colours = dict(THEMES.get(name, THEMES["dark"]))
    if is_hex_colour(accent):
        colours["accent"] = accent.strip()
    colours["on_accent"] = text_on(colours["accent"])
    # Hover feedback for accent buttons is a shade of the fill, never an
    # outline: no rounded highlight rings anywhere in this theme.
    colours["accent_hover"] = shade(colours["accent"], 0.15 if name == "dark" else -0.12)
    return colours


def shade(colour: str, amount: float) -> str:
    """Mix a hex colour toward white (amount > 0) or black (amount < 0)."""
    channels = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    target = 255 if amount > 0 else 0
    mixed = [round(value + (target - value) * abs(amount)) for value in channels]
    return "#" + "".join(f"{value:02x}" for value in mixed)


def text_on(background: str) -> str:
    """Black or white, whichever reads better on a hex background colour."""
    r, g, b = (int(background[i:i + 2], 16) / 255 for i in (1, 3, 5))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#000000" if luminance > 0.5 else "#ffffff"


CHEVRON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
    '<path d="{path}" fill="none" stroke="{colour}" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
CHEVRONS = {"down": "M2.5 4.5 6 8l3.5-3.5", "up": "M2.5 7.5 6 4l3.5 3.5"}


def write_chevrons(name: str, colour: str) -> dict:
    """
    Write down/up chevron SVGs in colour, returning {"down": path, "up": path}.

    A stylesheet can only draw an arrow from an image file, and the colour
    has to follow the theme, so they're written to the temp folder. Returns
    {} if that fails, and build_qss then leaves the style's own arrows.
    """
    import tempfile
    from pathlib import Path

    folder = Path(tempfile.gettempdir()) / "qupdatetool-theme"
    paths = {}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for direction, path in CHEVRONS.items():
            target = folder / f"chevron-{direction}-{name}.svg"
            target.write_text(CHEVRON_SVG.format(path=path, colour=colour), encoding="utf-8")
            paths[direction] = target.as_posix()
    except OSError as exc:
        logger.debug("Could not write theme arrows: %s", exc)
        return {}
    return paths


def build_arrow_qss(arrows: dict) -> str:
    if not arrows:
        return ""
    return f"""
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url({arrows['down']}); width: 12px; height: 12px; }}
QSpinBox {{ padding-right: 24px; }}
QSpinBox::up-button, QSpinBox::down-button {{
    border: none;
    background: transparent;
    width: 22px;
}}
QSpinBox::up-arrow {{ image: url({arrows['up']}); width: 10px; height: 10px; }}
QSpinBox::down-arrow {{ image: url({arrows['down']}); width: 10px; height: 10px; }}
"""


def build_qss(c: dict, arrows: dict | None = None) -> str:
    return build_arrow_qss(arrows or {}) + f"""
QWidget {{
    background-color: {c['window']};
    color: {c['text']};
    selection-background-color: {c['accent']};
    selection-color: {c['on_accent']};
}}
QWidget:disabled {{ color: {c['text_muted']}; }}
QDialog {{ background-color: {c['window']}; }}
QLabel, QCheckBox {{ background: transparent; }}
QToolTip {{
    background-color: {c['panel']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 4px;
    padding: 4px 6px;
}}

/* --- wizard shell --- */
QFrame#Sidebar {{ background-color: {c['sidebar']}; border-right: 1px solid {c['border']}; }}
QFrame#Sidebar QLabel, QFrame#Sidebar QListWidget {{ background: transparent; }}
QLabel#SidebarTitle {{ font-size: 15px; font-weight: 700; padding: 6px 8px 10px 8px; }}
QListWidget#StepList {{ border: none; outline: none; }}
QListWidget#StepList::item {{ padding: 9px 10px; border-radius: 6px; margin: 1px 0; }}
QListWidget#StepList::item:selected {{ background-color: {c['selected']}; color: {c['text']}; }}
QListWidget#StepList::item:hover:!selected {{ background-color: {c['hover']}; }}
QListWidget#StepList::item:disabled {{ color: {c['text_muted']}; }}
QLabel#PageTitle {{ font-size: 22px; font-weight: 700; }}
QLabel#PageSubtitle {{ color: {c['text_muted']}; padding-bottom: 8px; }}
QLabel#StepCounter, QLabel#Muted {{ color: {c['text_muted']}; }}
QScrollArea#PageScroll, QWidget#PageBody {{ background: transparent; border: none; }}
QLabel#Intro {{ padding: 2px 2px 6px 2px; }}

/* --- cards --- */
QFrame#Card {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-radius: 8px;
}}
QFrame#Card:hover {{ background-color: {c['card_hover']}; }}
QFrame#Card QLabel {{ background: transparent; }}
QFrame#Card QWidget#CardRow {{ background: transparent; }}
QLabel#CardTitle {{ font-weight: 600; }}
QLabel#CardDescription {{ color: {c['text_muted']}; font-size: 12px; }}
QLabel#Required {{ color: {c['danger']}; font-weight: 700; }}
QLabel#StatusText[state="ok"], QLabel#ResultIcon[state="ok"] {{ color: {c['success']}; }}
QLabel#StatusText[state="error"], QLabel#ResultIcon[state="error"] {{ color: {c['danger']}; }}
QLabel#StatusText[state="pending"] {{ color: {c['text_muted']}; }}
QLabel#ResultIcon {{ font-size: 15px; font-weight: 700; }}

/* --- inputs --- */
QLineEdit, QComboBox, QSpinBox {{
    background-color: {c['input']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 6px 8px;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ background-color: {c['input_focus']}; }}
QLineEdit:read-only {{ color: {c['text_muted']}; }}
QComboBox QAbstractItemView {{
    background-color: {c['panel']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 4px;
    outline: none;
    selection-background-color: {c['selected']};
    selection-color: {c['text']};
}}
QPlainTextEdit, QTextBrowser {{
    background-color: {c['input']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 4px;
}}
QTextBrowser#Notes {{ background: transparent; border: none; padding: 0; }}

/* --- buttons --- */
QPushButton {{
    background-color: {c['input']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 18px;
}}
QPushButton:hover {{ background-color: {c['hover']}; }}
QPushButton:pressed {{ background-color: {c['selected']}; }}
QPushButton:disabled {{ color: {c['text_muted']}; }}
QPushButton#Primary {{
    background-color: {c['accent']};
    border-color: {c['accent']};
    color: {c['on_accent']};
    font-weight: 600;
}}
QPushButton#Primary:hover {{ background-color: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
QPushButton#Primary:disabled {{
    background-color: {c['input']};
    border-color: {c['border']};
    color: {c['text_muted']};
}}
QPushButton#Link {{
    background: transparent;
    border: none;
    color: {c['text_muted']};
    text-align: left;
    padding: 6px 8px;
}}
QPushButton#Link:hover {{ color: {c['accent']}; }}

QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {c['text_muted']};
    background-color: {c['input']};
    border-radius: 3px;
}}
QCheckBox::indicator:checked {{ background-color: {c['accent']}; border-color: {c['accent']}; }}

/* --- update window --- */
QLabel#Heading {{ font-size: 18px; font-weight: 700; }}
QProgressBar#UpdateProgress {{
    background-color: {c['card']};
    border: none;
    border-radius: 3px;
}}
QProgressBar#UpdateProgress::chunk {{ background-color: {c['accent']}; border-radius: 3px; }}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {c['scrollbar']}; border-radius: 4px; min-height: 20px; }}
QScrollBar::handle:vertical:hover {{ background: {c['scrollbar_hover']}; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {c['scrollbar']}; border-radius: 4px; min-width: 20px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


def detect_system_theme() -> str:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    try:
        scheme = QGuiApplication.styleHints().colorScheme()
        return "light" if scheme == Qt.ColorScheme.Light else "dark"
    except Exception:  # noqa: BLE001 - platform dependent; dark is the safe default
        return "dark"


def system_accent() -> str:
    """The OS accent colour, or "" when it can't be read."""
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM")
        value, _ = winreg.QueryValueEx(key, "AccentColor")
        r, g, b = value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception as exc:  # noqa: BLE001 - not Windows, or no accent set
        logger.debug("No Windows accent colour: %s", exc)

    try:
        from PySide6.QtGui import QGuiApplication

        colour = QGuiApplication.palette().highlight().color()
        if colour.saturation() > 20:
            return colour.name()
    except Exception as exc:  # noqa: BLE001
        logger.debug("No palette accent colour: %s", exc)
    return ""


def apply_theme(app, theme: str = "system", accent: str = "") -> dict:
    """
    Style the whole QApplication and return the colours used.

    theme is "system", "dark" or "light"; accent is a hex colour or "" for
    the system accent. Uses the Fusion style underneath so stylesheet rules
    render the same on every platform, with a matching palette for the bits
    a stylesheet doesn't reach (combo and spin box arrows). With "system",
    the theme follows the OS when it switches between light and dark.
    """
    from PySide6.QtGui import QColor, QPalette

    name = detect_system_theme() if theme == "system" else theme
    colours = colours_for(name, accent if is_hex_colour(accent) else system_accent())

    app.setStyle("Fusion")

    palette = QPalette()
    solid = {
        QPalette.Window: colours["window"],
        QPalette.WindowText: colours["text"],
        QPalette.Base: colours["input_solid"],
        QPalette.AlternateBase: colours["panel"],
        QPalette.Text: colours["text"],
        QPalette.Button: colours["panel"],
        QPalette.ButtonText: colours["text"],
        QPalette.ToolTipBase: colours["panel"],
        QPalette.ToolTipText: colours["text"],
        QPalette.PlaceholderText: colours["text_muted_solid"],
        QPalette.Highlight: colours["accent"],
        QPalette.HighlightedText: colours["on_accent"],
        QPalette.Link: colours["accent"],
    }
    for role, value in solid.items():
        palette.setColor(role, QColor(value))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(colours["text_muted_solid"]))
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(colours["text_muted_solid"]))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(colours["text_muted_solid"]))
    app.setPalette(palette)
    app.setStyleSheet(build_qss(colours, write_chevrons(name, colours["text_muted_solid"])))

    active.clear()
    active.update(colours)

    if theme == "system" and not getattr(app, "_qupdate_theme_follows_system", False):
        app._qupdate_theme_follows_system = True
        try:
            app.styleHints().colorSchemeChanged.connect(
                lambda _scheme: apply_theme(app, "system", accent)
            )
        except Exception as exc:  # noqa: BLE001 - older Qt without the signal
            logger.debug("Can't follow system theme changes: %s", exc)

    logger.debug("Theme applied: %s, accent %s", name, colours["accent"])
    return colours

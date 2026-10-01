"""
QUpdateTool's own settings file (app_settings), the theme's Qt-free half,
and --setup logging from those settings.

Like test_setup_wizard.py, nothing here imports PySide6, so it runs on a
machine without the GUI extra.
"""

from __future__ import annotations

import logging

import pytest
import yaml

from qupdatetool import app_settings, cli, logging_utils
from qupdatetool.ui import theme


@pytest.fixture
def settings_home(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "settings_dir", lambda: tmp_path / "QUpdateTool")
    yield tmp_path / "QUpdateTool"
    # cli --setup reconfigures the shared updater logger; close its file
    # handler so tmp_path can be removed and later tests start clean.
    for handler in logging_utils.logger.handlers[:]:
        handler.close()
        logging_utils.logger.removeHandler(handler)


def test_first_load_writes_template_with_defaults(settings_home):
    settings = app_settings.load(create=True)

    assert settings == app_settings.DEFAULTS
    written = yaml.safe_load(app_settings.settings_path().read_text(encoding="utf-8"))
    assert written == app_settings.DEFAULTS


def test_load_without_create_never_writes(settings_home):
    app_settings.load(create=False)

    assert not app_settings.settings_path().exists()


def test_merge_keeps_values_adds_missing_and_prunes_unknown(settings_home):
    path = app_settings.settings_path()
    path.parent.mkdir(parents=True)
    path.write_text("log_level: debug\ntheme: light\nold_key: 1\n", encoding="utf-8")

    settings = app_settings.load(create=True)

    assert settings["log_level"] == "DEBUG"
    assert settings["theme"] == "light"
    assert settings["accent_color"] == ""
    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "old_key" not in written
    assert written["accent_color"] == ""


@pytest.mark.parametrize("raw", [
    {"log_level": "LOUD"},
    {"theme": "purple"},
    {"log_level": 5},
    "not a mapping",
])
def test_bad_values_fall_back_to_defaults(raw):
    merged = app_settings.merge(raw)

    assert merged["log_level"] == "INFO"
    assert merged["theme"] == "system"


def test_unreadable_yaml_is_not_overwritten(settings_home):
    path = app_settings.settings_path()
    path.parent.mkdir(parents=True)
    path.write_text("log_level: [unclosed\n", encoding="utf-8")

    settings = app_settings.load(create=True)

    assert settings == app_settings.DEFAULTS
    assert path.read_text(encoding="utf-8") == "log_level: [unclosed\n"


def test_setup_log_path(settings_home, tmp_path):
    assert app_settings.setup_log_path({"log_file": ""}) == str(settings_home / "logs" / "setup.log")
    assert app_settings.setup_log_path({"log_file": "none"}) == "none"
    assert app_settings.setup_log_path({"log_file": str(tmp_path / "logs")}) == str(tmp_path / "logs" / "setup.log")
    assert app_settings.setup_log_path({"log_file": str(tmp_path / "x.log")}) == str(tmp_path / "x.log")


def test_setup_logs_to_its_own_file(settings_home, monkeypatch):
    """--setup configures logging from the settings file before the wizard opens."""
    import qupdatetool.ui.setup as setup_module

    seen = {}

    def fake_wizard(config_path, settings):
        logging_utils.get_logger("setup").info("wizard ran")
        seen["settings"] = settings
        return 0

    monkeypatch.setattr(setup_module, "run_setup_wizard", fake_wizard)

    assert cli.main(["--setup", "--quiet", "--log-level", "DEBUG"]) == 0

    log = (settings_home / "logs" / "setup.log").read_text(encoding="utf-8")
    assert "wizard ran" in log
    assert "settings" in log
    assert logging_utils.logger.level == logging.DEBUG
    assert seen["settings"]["theme"] == "system"
    assert app_settings.settings_path().exists()


@pytest.mark.parametrize("name", ["dark", "light"])
def test_theme_palettes_have_every_key(name):
    assert set(theme.THEMES[name]) == set(theme.THEMES["dark"])
    qss = theme.build_qss(theme.colours_for(name))
    # Every placeholder filled: a missed key would raise KeyError, a
    # half-escaped brace would leave one in the output.
    assert "{" in qss and "{c[" not in qss


def test_accent_override_and_contrast():
    colours = theme.colours_for("dark", "#0078d4")
    assert colours["accent"] == "#0078d4"
    assert colours["on_accent"] == "#ffffff"

    assert theme.colours_for("dark", "#60cdff")["on_accent"] == "#000000"
    assert theme.colours_for("dark", "not a colour")["accent"] == theme.THEMES["dark"]["accent"]


def test_arrow_rules_only_with_arrow_files():
    colours = theme.colours_for("dark")
    assert "down-arrow" not in theme.build_qss(colours)
    assert "url(/tmp/d.svg)" in theme.build_qss(colours, {"down": "/tmp/d.svg", "up": "/tmp/u.svg"})


@pytest.mark.parametrize("name", ["dark", "light"])
def test_no_accent_outlines(name):
    """Hard rule: no rounded highlight rings. Accent is a fill, never a border."""
    import re

    colours = theme.colours_for(name)
    qss = theme.build_qss(colours)
    for rule in re.findall(r"[^{}]+\{[^{}]*\}", qss):
        selector, body = rule.split("{", 1)
        if selector.strip() in ("QPushButton#Primary", "QCheckBox::indicator:checked"):
            continue  # border matches the solid fill, so it reads as one shape
        for colour in (colours["accent"], colours["text"]):
            assert not re.search(rf"border[\w-]*:[^;]*{re.escape(colour)}", body), selector.strip()

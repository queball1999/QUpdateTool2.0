"""
Headless-device tests: elevation disabled, no GUI imports, and the
cryptography surface the OpenPGP verifier relies on.

A Raspberry Pi runs the updater as root from a systemd unit. It must never
try to raise a privilege prompt, never import Qt, and must work with the
older cryptography that Debian bookworm ships.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from qupdatetool import cli, elevate
from qupdatetool import config as config_module
from qupdatetool.errors import ConfigError, ExitCode

ROOT = Path(__file__).resolve().parent.parent


def make_config(**sections) -> config_module.Config:
    """Build a Config from the defaults with the given sections merged on top."""
    return config_module.Config(config_module.deep_merge(config_module.DEFAULTS, sections))


def parse(argv: list):
    """Parse argv and return (args, overrides)."""
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    return args, cli.collect_overrides(args, parser)


def test_no_elevate_flag_sets_never():
    """--no-elevate is shorthand for install.elevate never."""
    _, overrides = parse(["--no-elevate"])
    assert overrides["install"]["elevate"] == "never"


def test_no_elevate_wins_over_elevate_always():
    """If both are passed, the safe reading (never elevate) wins."""
    _, overrides = parse(["--elevate", "always", "--no-elevate"])
    assert overrides["install"]["elevate"] == "never"


def test_never_does_not_need_elevation(tmp_path, monkeypatch):
    """needs_elevation is False for never, even when the folder is read-only."""
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    monkeypatch.setattr(elevate, "can_write", lambda path: False)
    config = make_config(install={"elevate": "never"})

    assert elevate.needs_elevation(config, tmp_path, "machine") is False


def test_require_writable_raises_usage_error(tmp_path, monkeypatch):
    """An unwritable target with elevation off fails with exit 2 and says to use root."""
    monkeypatch.setattr(elevate, "can_write", lambda path: False)
    config = make_config(install={"elevate": "never"})

    with pytest.raises(ConfigError) as caught:
        elevate.require_writable_without_elevation(config, tmp_path)

    assert caught.value.exit_code == ExitCode.USAGE
    assert "as root" in str(caught.value)
    assert "systemd oneshot" in str(caught.value)


def test_require_writable_passes_when_writable(tmp_path):
    """A writable target is fine."""
    elevate.require_writable_without_elevation(
        make_config(install={"elevate": "never"}), tmp_path
    )


def test_require_writable_ignored_for_other_modes(tmp_path, monkeypatch):
    """Only never triggers the early failure."""
    monkeypatch.setattr(elevate, "can_write", lambda path: False)
    elevate.require_writable_without_elevation(make_config(install={"elevate": "auto"}), tmp_path)


def test_elevate_first_never_calls_a_launcher(tmp_path, monkeypatch, capsys):
    """With --no-elevate and a read-only target, no pkexec/sudo/UAC is launched."""
    def forbidden(*args, **kwargs):
        raise AssertionError("elevation must not be attempted")

    monkeypatch.setattr(elevate, "relaunch_elevated", forbidden)
    monkeypatch.setattr(elevate, "posix_relaunch_elevated", forbidden)
    monkeypatch.setattr(elevate, "windows_relaunch_elevated", forbidden)
    monkeypatch.setattr(elevate.subprocess, "run", forbidden)
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    monkeypatch.setattr(elevate, "can_write", lambda path: False)

    config = make_config(install={"elevate": "never", "target_dir": str(tmp_path)})
    args, _ = parse(["--no-elevate"])

    assert cli.elevate_first(config, args) == ExitCode.USAGE
    assert "as root" in capsys.readouterr().err


def test_elevate_first_never_proceeds_when_writable(tmp_path):
    """A writable target with elevation off carries on in this process."""
    config = make_config(install={"elevate": "never", "target_dir": str(tmp_path)})
    args, _ = parse(["--no-elevate"])

    assert cli.elevate_first(config, args) is None


def test_importing_the_cli_does_not_load_qt():
    """Importing the headless entry point must not pull in PySide6."""
    code = (
        "import sys; import qupdatetool.cli, qupdatetool.updater, qupdatetool.process; "
        "bad = [m for m in sys.modules if m.startswith('PySide6') or m == 'qupdatetool.ui']; "
        "sys.exit(1 if bad else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="DISPLAY check is Linux only")
def test_headless_session_detection(monkeypatch):
    """No DISPLAY and no WAYLAND_DISPLAY means headless on Linux."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert cli.headless_session() is True

    monkeypatch.setenv("DISPLAY", ":0")
    assert cli.headless_session() is False


def test_gui_request_is_downgraded_when_headless(monkeypatch):
    """ui.gui true on a headless Linux box runs the console flow, never Qt."""
    calls = []
    monkeypatch.setattr(cli, "headless_session", lambda: True)
    monkeypatch.setattr(cli, "run_console", lambda config, args: calls.append("console") or 0)
    monkeypatch.setattr(cli, "elevate_first", lambda config, args: None)

    # A None entry makes "from .ui.app import run_gui" raise ImportError, so
    # the test fails (exit 1) if the GUI import is ever attempted.
    monkeypatch.setitem(sys.modules, "qupdatetool.ui.app", None)
    monkeypatch.setattr(cli.logging_utils, "configure", lambda **kwargs: None)

    assert cli.main(["--gui", "--app-name", "X", "--repo", "a/b", "--log-file", "none"]) == 0
    assert calls == ["console"]


def test_cryptography_surface_used_by_openpgp():
    """
    Every cryptography call the OpenPGP verifier makes exists on the oldest
    supported version (38.0.4 on Debian bookworm): these are the same names
    imported and called in qupdatetool/openpgp.py.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding
    from cryptography.hazmat.primitives.asymmetric import utils as asym_utils
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

    assert hasattr(RSAPublicNumbers, "public_key")
    assert hasattr(ec.EllipticCurvePublicKey, "from_encoded_point")
    assert hasattr(ed25519.Ed25519PublicKey, "from_public_bytes")
    assert hasattr(asym_utils, "Prehashed")
    assert hasattr(asym_utils, "encode_dss_signature")
    assert hasattr(padding, "PKCS1v15")
    for name in ("SHA1", "SHA224", "SHA256", "SHA384", "SHA512", "SHA3_256", "SHA3_512"):
        assert hasattr(hashes, name)
    for name in ("SECP256R1", "SECP384R1", "SECP521R1"):
        assert hasattr(ec, name)


def test_requirements_allow_bookworm_cryptography():
    """requirements.txt must not exclude Debian bookworm's cryptography 38."""
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "cryptography>=38" in text

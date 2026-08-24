"""
Exit-code contract tests.

Exit codes are the interface between the updater and the application that
launches it, and that application turns each code into a sentence a user
reads. A code that means two different things produces a wrong message, so
these tests pin both the numbers and which exception maps to which.
"""

from __future__ import annotations

import pytest

from qupdatetool.cli import main
from qupdatetool.errors import (
    BackendError,
    CancelledError,
    ConfigError,
    ExitCode,
    InstallError,
    NetworkError,
    NoReleaseError,
    ProcessError,
    UpdaterError,
    VerificationError,
)


def test_exit_code_numbers_are_stable():
    """
    The numbers are a published contract; changing one silently breaks every
    caller that already maps it to a message.
    """
    assert ExitCode.SUCCESS == 0
    assert ExitCode.ERROR == 1
    assert ExitCode.USAGE == 2
    assert ExitCode.NETWORK == 3
    assert ExitCode.VERIFICATION == 4
    assert ExitCode.INSTALL == 5
    assert ExitCode.CANCELLED == 6
    assert ExitCode.PROCESS == 7
    assert ExitCode.NO_RELEASE == 8
    assert ExitCode.UPDATE_AVAILABLE == 10
    assert ExitCode.UP_TO_DATE == 11


def test_every_exit_code_is_distinct():
    """No two codes share a number, or one failure would masquerade as another."""
    codes = [
        value for name, value in vars(ExitCode).items()
        if not name.startswith("_") and isinstance(value, int)
    ]

    assert len(codes) == len(set(codes))


@pytest.mark.parametrize(
    "exception,expected",
    [
        (ConfigError("x"), ExitCode.USAGE),
        (NetworkError("x"), ExitCode.NETWORK),
        (BackendError("x"), ExitCode.NETWORK),
        (NoReleaseError("x"), ExitCode.NO_RELEASE),
        (VerificationError("x"), ExitCode.VERIFICATION),
        (InstallError("x"), ExitCode.INSTALL),
        (ProcessError("x"), ExitCode.PROCESS),
        (CancelledError("x"), ExitCode.CANCELLED),
    ],
)
def test_exceptions_map_to_their_own_code(exception, expected):
    assert exception.exit_code == expected


def test_base_error_degrades_to_unclassified():
    """
    A bare UpdaterError reports ERROR, not USAGE.

    If the base class claimed a specific category, a subclass that forgot to
    declare its own exit_code would silently misreport itself as that
    category rather than as an unclassified failure.
    """
    assert UpdaterError("x").exit_code == ExitCode.ERROR


def test_no_release_is_not_a_network_error():
    """
    A reachable backend with nothing to offer is not a connection failure.

    These were the same code once, which meant a project that had not cut a
    stable release told users their network was broken.
    """
    assert NoReleaseError("x").exit_code != NetworkError("x").exit_code


def test_unexpected_exception_does_not_report_install_failure(monkeypatch, tmp_path):
    """
    An unexpected error anywhere returns ERROR, never INSTALL.

    Regression test: this used to return INSTALL, so a crash during the
    *check* phase - before anything was downloaded - told the user their
    install had failed when nothing was ever installed.
    """
    import qupdatetool.cli as cli

    def explode(*args, **kwargs):
        raise RuntimeError("something unforeseen")

    monkeypatch.setattr(cli, "build_updater", explode)

    code = main([
        "--provider", "github",
        "--repo", "owner/app",
        "--app-name", "TestApp",
        "--current-version", "1.0.0",
        "--no-require-signature",
        "--log-file", "none",
        "--quiet",
    ])

    assert code == ExitCode.ERROR
    assert code != ExitCode.INSTALL


def test_usage_error_reports_usage_code(tmp_path):
    """A bad flag value is a usage error, not a generic one."""
    code = main([
        "--provider", "github",
        "--repo", "owner/app",
        "--app-name", "TestApp",
        "--asset-pattern", "[unclosed",
        "--no-require-signature",
        "--log-file", "none",
        "--quiet",
    ])

    assert code == ExitCode.USAGE


def test_version_and_self_check_succeed():
    """Informational flags exit 0 and never touch the network."""
    assert main(["--version"]) == ExitCode.SUCCESS
    assert main(["--self-check"]) == ExitCode.SUCCESS


def test_no_gui_overrides_a_brand_that_defaults_gui_true(tmp_path, monkeypatch):
    """
    --no-gui wins even when the brand/config bakes in ui.gui: true.

    Regression test: a brand with ui.gui defaulted to true meant every
    invocation - including a caller's captured, non-interactive
    "--check-only --json --quiet" call - tried to open a real window and hung
    forever waiting for someone to close it, since the caller wasn't watching
    for one. --no-gui is the explicit escape hatch for exactly that case.
    """
    import argparse

    from qupdatetool import cli as cli_module

    parser = cli_module.build_parser()
    args = parser.parse_args(["--check-only", "--no-gui"])
    overrides = cli_module.collect_overrides(args, parser)

    config = config_module_for_test(overrides)
    assert config.get("ui.gui") is False


def config_module_for_test(overrides):
    from qupdatetool import config as config_module

    return config_module.build(
        brand={"ui": {"gui": True}},
        overrides=overrides,
    )

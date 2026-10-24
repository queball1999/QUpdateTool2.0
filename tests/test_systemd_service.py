"""
systemd service mode tests.

With app.service_manager set to systemd the updater stops and starts the
application with systemctl instead of by PID. subprocess is mocked, so these
run anywhere; they pin the exact command lists, the unit-name validation, and
that every failure maps to exit code 7 (or 2 for a bad name).
"""

from __future__ import annotations

import subprocess

import pytest

from qupdatetool import cli, process
from qupdatetool import config as config_module
from qupdatetool.errors import ConfigError, ExitCode, ProcessError


def make_config(**sections) -> config_module.Config:
    """Build a Config from the defaults with the given sections merged on top."""
    return config_module.Config(config_module.deep_merge(config_module.DEFAULTS, sections))


def systemd_config(**process_section) -> config_module.Config:
    """Build a Config in systemd mode for pistation.service."""
    return make_config(
        app={"service_manager": "systemd", "service_unit": "pistation.service"},
        process=process_section,
    )


class FakeRun:
    """A stand-in for subprocess.run that records commands."""

    def __init__(self, returncode=0, stdout="", stderr="", exc=None):
        self.calls = []
        self.kwargs = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.exc = exc

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        self.kwargs.append(kwargs)
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


def install_fake(monkeypatch, runner) -> None:
    """Route process.py's subprocess.run and systemctl lookup to fakes."""
    monkeypatch.setattr(process.subprocess, "run", runner)
    monkeypatch.setattr(process.shutil, "which", lambda name: "/usr/bin/systemctl")


@pytest.fixture
def fake_run(monkeypatch):
    runner = FakeRun()
    install_fake(monkeypatch, runner)
    return runner


def test_stop_uses_systemctl_stop(fake_run):
    """stop_parent runs systemctl stop on the unit, as a list, with no shell."""
    stopped = process.stop_parent(systemd_config())

    assert stopped == ["pistation.service"]
    assert fake_run.calls == [
        ["/usr/bin/systemctl", "--no-ask-password", "stop", "pistation.service"]
    ]
    assert "shell" not in fake_run.kwargs[0]


def test_relaunch_uses_systemctl_start(fake_run):
    """relaunch runs systemctl start on the unit instead of spawning the app."""
    assert process.relaunch(systemd_config()) is True
    assert fake_run.calls == [
        ["/usr/bin/systemctl", "--no-ask-password", "start", "pistation.service"]
    ]


def test_no_relaunch_and_no_stop_flags_are_honoured(fake_run):
    """process.relaunch and process.stop_parent still switch the calls off."""
    config = systemd_config(relaunch=False, stop_parent=False)

    assert process.stop_parent(config) == []
    assert process.relaunch(config) is False
    assert fake_run.calls == []


def test_stop_does_not_use_pid_lookup(fake_run, monkeypatch):
    """Service mode never looks processes up by PID or name."""
    def fail(*args, **kwargs):
        raise AssertionError("find_processes must not be used in systemd mode")

    monkeypatch.setattr(process, "find_processes", fail)
    config = systemd_config()
    config.set("process.calling_pid", 1234)

    process.stop_parent(config)


def test_timeout_maps_to_process_error(monkeypatch):
    """A systemctl that does not finish in time is a ProcessError (exit 7)."""
    install_fake(monkeypatch, FakeRun(exc=subprocess.TimeoutExpired(cmd="systemctl", timeout=30)))

    with pytest.raises(ProcessError) as caught:
        process.stop_parent(systemd_config(stop_timeout=30))

    assert caught.value.exit_code == ExitCode.PROCESS
    assert "did not finish in time" in str(caught.value)


def test_nonzero_exit_maps_to_process_error(monkeypatch):
    """A failing systemctl reports its output and exits 7."""
    install_fake(monkeypatch, FakeRun(returncode=5, stderr="Unit pistation.service not loaded"))

    with pytest.raises(ProcessError) as caught:
        process.relaunch(systemd_config())

    assert caught.value.exit_code == ExitCode.PROCESS
    assert "exit 5" in str(caught.value)
    assert "not loaded" in str(caught.value)


def test_missing_systemctl_maps_to_process_error(monkeypatch):
    """No systemctl on the machine is a clear ProcessError, not a crash."""
    monkeypatch.setattr(process.shutil, "which", lambda name: None)

    with pytest.raises(ProcessError) as caught:
        process.stop_parent(systemd_config())

    assert "systemctl was not found" in str(caught.value)


def test_oserror_maps_to_process_error(monkeypatch):
    """An OSError launching systemctl is a ProcessError."""
    install_fake(monkeypatch, FakeRun(exc=PermissionError("denied")))

    with pytest.raises(ProcessError):
        process.stop_parent(systemd_config())


@pytest.mark.parametrize(
    "unit",
    [
        "pistation.service",
        "pistation-updater.service",
        "my_app@instance.service",
        "App1.service",
        "dev-sda1:x.service",
    ],
)
def test_valid_unit_names(unit):
    """Ordinary unit names pass validation."""
    assert config_module.validate_unit_name(unit) == unit


@pytest.mark.parametrize(
    "unit",
    [
        "",
        "pistation",
        "pistation.timer",
        "-pistation.service",
        "--now.service",
        "pistation.service; reboot",
        "pistation service.service",
        "pistation.service\n",
        "$(reboot).service",
        "../etc/x.service",
        "a/b.service",
        "pi`id`.service",
        ".service",
        "x" * 300 + ".service",
    ],
)
def test_invalid_unit_names_are_refused(unit):
    """Anything that is not a plain unit name is refused with exit code 2."""
    with pytest.raises(ConfigError) as caught:
        config_module.validate_unit_name(unit)

    assert caught.value.exit_code == ExitCode.USAGE


def test_run_systemctl_refuses_bad_unit_before_running(monkeypatch):
    """A bad name never reaches subprocess."""
    runner = FakeRun()
    monkeypatch.setattr(process.subprocess, "run", runner)

    with pytest.raises(ConfigError):
        process.run_systemctl("stop", "x; reboot")

    assert runner.calls == []


def test_validate_requires_a_unit_for_systemd():
    """service_manager systemd without a unit is a configuration error."""
    config = make_config(app={"service_manager": "systemd"})

    with pytest.raises(ConfigError, match="no unit"):
        config.validate_service()


def test_validate_rejects_unknown_manager():
    """Only none and systemd are accepted."""
    with pytest.raises(ConfigError, match="Unknown service manager"):
        make_config(app={"service_manager": "runit"}).validate_service()


def test_flags_map_to_config():
    """--service-manager and --service-unit land in app.*."""
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--service-manager", "systemd", "--service-unit", "pistation.service"]
    )
    overrides = cli.collect_overrides(args, parser)

    assert overrides["app"]["service_manager"] == "systemd"
    assert overrides["app"]["service_unit"] == "pistation.service"

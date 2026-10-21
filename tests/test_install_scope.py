"""
Install scope, elevation and installer-mode tests.

The failure these guard against: updating a machine-wide install as if it
were a per-user one, which leaves the old copy running and creates a second
install nobody launches; and an elevated updater starting the application as
the administrator.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from qupdatetool import cli, elevate
from qupdatetool import config as config_module
from qupdatetool import scope as scope_module
from qupdatetool.errors import ExitCode, InstallError
from qupdatetool.install import windows


def make_config(**sections) -> config_module.Config:
    return config_module.Config(config_module.deep_merge(config_module.DEFAULTS, sections))


# --- scope detection ---


@pytest.mark.parametrize("scope", ["user", "machine"])
def test_configured_scope_wins(scope, tmp_path):
    config = make_config(install={"scope": scope}, app={"install_dir": str(tmp_path)})
    assert scope_module.detect(config, "windows") == scope


def test_program_files_means_machine(tmp_path, monkeypatch):
    program_files = tmp_path / "Program Files"
    install = program_files / "App"
    install.mkdir(parents=True)
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.setattr(scope_module, "read_uninstall_entries", lambda key: {})
    config = make_config(app={"install_dir": str(install)})
    assert scope_module.detect(config, "windows") == "machine"


def test_writable_folder_outside_program_files_means_user(tmp_path, monkeypatch):
    for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(scope_module, "read_uninstall_entries", lambda key: {})
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    config = make_config(app={"install_dir": str(tmp_path)})
    assert scope_module.detect(config, "windows") == "user"


def test_read_only_folder_means_machine(tmp_path, monkeypatch):
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    monkeypatch.setattr(elevate, "can_write", lambda path: False)
    config = make_config(app={"install_dir": str(tmp_path)})
    assert scope_module.detect(config, "linux") == "machine"


def test_uninstall_entry_matching_the_folder_decides(tmp_path, monkeypatch):
    """Both hives have the key (a user and a machine install): the one at this folder wins."""
    other = tmp_path / "other"
    monkeypatch.setattr(scope_module, "read_uninstall_entries",
                        lambda key: {"user": str(other), "machine": str(tmp_path) + "\\"})
    config = make_config(app={"install_dir": str(tmp_path)}, install={"uninstall_key": "K"})
    assert scope_module.detect(config, "windows") == "machine"


def test_old_patches_key_name_is_still_read():
    config = make_config(patches={"windows_uninstall_key": "Old"})
    assert scope_module.uninstall_key(config) == "Old"


def test_invalid_scope_is_a_config_error():
    config = make_config(source={"repo": "o/r"}, install={"scope": "everyone"})
    with pytest.raises(Exception, match="install scope"):
        config.validate()


# --- elevation ---


def test_machine_scope_needs_elevation_even_if_writable(tmp_path, monkeypatch):
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    config = make_config()
    assert elevate.needs_elevation(config, tmp_path, scope="machine") is True
    assert elevate.needs_elevation(config, tmp_path, scope="user") is False


def test_never_means_never(tmp_path, monkeypatch):
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    config = make_config(install={"elevate": "never"})
    assert elevate.needs_elevation(config, tmp_path, scope="machine") is False


class Args:
    check_only = False
    already_elevated = False


def elevate_config(tmp_path):
    return make_config(app={"install_dir": str(tmp_path), "process_names": ["App.exe"]})


def test_elevate_first_runs_elevated_without_relaunch_then_relaunches_as_user(tmp_path, monkeypatch):
    config = elevate_config(tmp_path)
    calls = {}
    monkeypatch.setattr(scope_module, "detect", lambda config, os_family="": "machine")
    monkeypatch.setattr(elevate, "needs_elevation", lambda *a, **k: True)
    monkeypatch.setattr(elevate, "relaunch_elevated",
                        lambda extra_args=(), log=None: calls.setdefault("args", extra_args) and 0)
    monkeypatch.setattr("qupdatetool.process.find_processes", lambda **k: [])
    monkeypatch.setattr("qupdatetool.process.relaunch",
                        lambda config, log=None: calls.setdefault("relaunched", True))

    assert cli.elevate_first(config, Args()) == ExitCode.SUCCESS
    assert "--no-relaunch" in calls["args"]
    assert calls["args"][calls["args"].index("--install-scope") + 1] == "machine"
    assert calls["relaunched"] is True


def test_elevate_first_does_not_start_a_second_copy(tmp_path, monkeypatch):
    config = elevate_config(tmp_path)
    monkeypatch.setattr(elevate, "needs_elevation", lambda *a, **k: True)
    monkeypatch.setattr(elevate, "relaunch_elevated", lambda extra_args=(), log=None: 0)
    monkeypatch.setattr("qupdatetool.process.find_processes", lambda **k: ["running"])
    monkeypatch.setattr("qupdatetool.process.relaunch",
                        lambda config, log=None: pytest.fail("relaunched while running"))

    assert cli.elevate_first(config, Args()) == ExitCode.SUCCESS


def test_declined_elevation_reports_install_failure(tmp_path, monkeypatch):
    config = elevate_config(tmp_path)
    monkeypatch.setattr(elevate, "needs_elevation", lambda *a, **k: True)

    def declined(extra_args=(), log=None):
        raise InstallError("Administrator privileges were declined")

    monkeypatch.setattr(elevate, "relaunch_elevated", declined)
    assert cli.elevate_first(config, Args()) == ExitCode.INSTALL


@pytest.mark.parametrize("flag", ["check_only", "already_elevated"])
def test_no_elevation_for_checks_or_inside_the_elevated_copy(tmp_path, monkeypatch, flag):
    args = Args()
    setattr(args, flag, True)
    monkeypatch.setattr(elevate, "needs_elevation", lambda *a, **k: pytest.fail("asked"))
    assert cli.elevate_first(elevate_config(tmp_path), args) is None


# --- installer mode ---


def fake_installer(tmp_path, scope, monkeypatch, **installer_args):
    config = make_config(app={"install_dir": str(tmp_path)},
                         install={"installer_args": installer_args})
    config.set("meta.install_scope", scope)
    installer = windows.WindowsInstaller(config, host_platform=None)
    commands = []

    def run(command, timeout=3600, cwd=""):
        commands.append([str(part) for part in command])
        return 0, ""

    monkeypatch.setattr(installer, "run", run)
    return installer, commands


@pytest.mark.parametrize("scope, flag", [("user", "/CURRENTUSER"), ("machine", "/ALLUSERS")])
def test_scope_flags_are_added_to_the_installer(tmp_path, monkeypatch, scope, flag):
    installer, commands = fake_installer(
        tmp_path, scope, monkeypatch,
        exe=["/VERYSILENT"], exe_user=["/CURRENTUSER"], exe_machine=["/ALLUSERS"],
    )
    result = installer.install(tmp_path / "setup.exe", "exe")

    assert commands[0][1:] == ["/VERYSILENT", flag]
    assert scope in result.method


# --- the updater moving itself aside ---


def test_running_updater_is_moved_aside_and_the_new_copy_kept(tmp_path):
    install = tmp_path / "App"
    install.mkdir()
    exe = install / "updater.exe"
    exe.write_text("old updater")

    with windows.own_executable_aside(install, executable=str(exe)):
        assert not exe.exists()
        exe.write_text("new updater")   # what the installer does

    assert exe.read_text() == "new updater"
    asides = list(install.glob(f"*{windows.SELF_ASIDE_SUFFIX}"))
    assert [p.read_text() for p in asides] == ["old updater"]


def test_original_comes_back_when_the_installer_writes_none(tmp_path):
    install = tmp_path / "App"
    install.mkdir()
    exe = install / "updater.exe"
    exe.write_text("old updater")

    with pytest.raises(RuntimeError), windows.own_executable_aside(install, executable=str(exe)):
        raise RuntimeError("installer failed")

    assert exe.read_text() == "old updater"
    assert not list(install.glob(f"*{windows.SELF_ASIDE_SUFFIX}"))


def test_updater_outside_the_install_is_left_alone(tmp_path):
    exe = tmp_path / "elsewhere" / "updater.exe"
    exe.parent.mkdir()
    exe.write_text("updater")
    (tmp_path / "App").mkdir()

    with windows.own_executable_aside(tmp_path / "App", executable=str(exe)):
        assert exe.exists()


def test_stale_copies_are_removed(tmp_path):
    stale = tmp_path / f"updater.exe.123{windows.SELF_ASIDE_SUFFIX}"
    stale.write_text("x")
    windows.remove_stale_self_copies(tmp_path)
    assert not stale.exists()


# --- failures bring the app back ---


def test_failed_install_relaunches_the_previous_version(tmp_path, monkeypatch):
    from qupdatetool.updater import UpdateCheck, Updater

    config = make_config(
        app={"name": "App", "install_dir": str(tmp_path)},
        source={"provider": "generic", "download_url": "https://example.invalid/a.exe"},
        process={"stop_parent": False},
    )
    updater = Updater(config)
    relaunched = []
    monkeypatch.setattr(updater, "relaunch", lambda messages: relaunched.append(True) or True)

    class Broken:
        def install(self, artifact, kind):
            raise InstallError("installer exited 2")

    monkeypatch.setattr("qupdatetool.updater.get_installer", lambda *a, **k: Broken())

    with pytest.raises(InstallError):
        updater.install(UpdateCheck(available=True, latest_version="2"), Path("a.exe"), None)

    assert relaunched == [True]


@pytest.mark.parametrize("kind", ["msi", "deb", "rpm", "appimage"])
def test_package_formats_are_never_patched(tmp_path, monkeypatch, kind):
    from qupdatetool.updater import UpdateCheck, Updater

    config = make_config(
        app={"name": "App", "install_dir": str(tmp_path), "current_version": "1.0.0"},
        source={"provider": "generic", "download_url": "https://example.invalid/a.exe"},
        patches={"enabled": True},
    )
    updater = Updater(config)
    updater.host.running_as_appimage = False
    monkeypatch.setattr("qupdatetool.patch.plan_chain", lambda *a, **k: pytest.fail("planned"))
    check = UpdateCheck(available=True, current_version="1.0.0", asset_kind=kind)

    assert updater.plan_patches(check) is None


def test_elevated_child_command_carries_its_flags(monkeypatch):
    monkeypatch.setattr("sys.argv", ["updater", "--gui"])
    monkeypatch.setattr("sys.frozen", True, raising=False)
    _executable, arguments = elevate.current_command(("--no-relaunch", "--install-scope", "machine"))
    assert arguments == ["--gui", "--no-relaunch", "--install-scope", "machine", "--already-elevated"]
    assert subprocess.list2cmdline(arguments)

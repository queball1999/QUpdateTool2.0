"""
Post-install health check and rollback tests.

Success is HTTP 200 from health.url. When it never answers 200 within the
configured attempts, every file is restored from its .old backup, the service
is restarted, and the updater exits 5. Without health.url nothing changes.
The HTTP side uses a real http.server on a loopback port.
"""

from __future__ import annotations

import http.server
import io
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from qupdatetool import cli, health, process
from qupdatetool import config as config_module
from qupdatetool import updater as updater_module
from qupdatetool.errors import ConfigError, ExitCode, InstallError, ProcessError
from qupdatetool.updater import Updater


class HealthServer:
    """A loopback HTTP server whose /healthz status code can be changed."""

    def __init__(self, status: int = 200):
        self.status = status
        self.hits = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                self.send_response(outer.status)
                if outer.status in (301, 302):
                    self.send_header("Location", "/elsewhere")
                self.end_headers()

            def log_message(self, format, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/healthz"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc_info):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def no_sleep(seconds):
    """Skip waiting between attempts."""


def test_probe_true_only_for_200():
    """200 is healthy; 500, 404 and redirects are not."""
    with HealthServer(200) as server:
        assert health.probe(server.url, 2) is True
        server.status = 500
        assert health.probe(server.url, 2) is False
        server.status = 404
        assert health.probe(server.url, 2) is False
        server.status = 302
        assert health.probe(server.url, 2) is False


def test_probe_connection_refused_is_unhealthy():
    """A refused connection returns False and does not raise."""
    with HealthServer() as server:
        url = server.url
    assert health.probe(url, 1) is False


def test_wait_succeeds_on_first_200():
    """Polling stops at the first healthy answer."""
    with HealthServer(200) as server:
        assert health.wait_until_healthy(server.url, 3, 30, sleep=no_sleep) is True
        assert server.hits == 1


def test_wait_succeeds_after_a_failure():
    """A service that comes up on the second attempt passes."""
    with HealthServer(503) as server:
        def flip(seconds):
            server.status = 200

        assert health.wait_until_healthy(server.url, 3, 30, sleep=flip) is True
        assert server.hits == 2


def test_wait_fails_after_all_attempts():
    """The configured number of attempts is made, then the check fails."""
    with HealthServer(500) as server:
        assert health.wait_until_healthy(server.url, 3, 30, sleep=no_sleep) is False
        assert server.hits == 3


def test_attempts_are_spread_over_the_window():
    """Sleeps between attempts add up to less than the window."""
    sleeps = []
    ticks = iter(range(1000))
    with HealthServer(500) as server:
        health.wait_until_healthy(
            server.url, 3, 60, sleep=sleeps.append, clock=lambda: next(ticks) * 0.0
        )
    assert len(sleeps) == 2
    assert sum(sleeps) <= 60


def test_settings_defaults_and_validation():
    """Defaults are 3 attempts in 60 seconds, and blank url disables the check."""
    config = config_module.Config(config_module.deep_merge(config_module.DEFAULTS, {}))
    assert health.settings(config) == ("", 3, 60)

    bad = config_module.Config(
        config_module.deep_merge(config_module.DEFAULTS, {"health": {"attempts": 0}})
    )
    with pytest.raises(ConfigError):
        health.settings(bad)
    with pytest.raises(ConfigError):
        bad.validate_health()


def test_health_url_must_be_http():
    """A file: or other scheme is refused at validation."""
    config = config_module.Config(
        config_module.deep_merge(config_module.DEFAULTS, {"health": {"url": "file:///etc/passwd"}})
    )
    with pytest.raises(ConfigError, match="http or https"):
        config.validate_health()


def test_health_flags_map_to_config():
    """--health-url, --health-attempts and --health-window-seconds land in health.*."""
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--health-url", "http://127.0.0.1/healthz", "--health-attempts", "5",
         "--health-window-seconds", "90"]
    )
    overrides = cli.collect_overrides(args, parser)

    assert overrides["health"] == {
        "url": "http://127.0.0.1/healthz", "attempts": 5, "window_seconds": 90,
    }


# --- full install flow with rollback ---


def make_tarball(path: Path, data: bytes) -> Path:
    """Write a tarball holding one executable `pistation` file."""
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("pistation")
        info.size = len(data)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(data))
    return path


class Scenario:
    """An Updater wired to a temp bin dir, a fake systemctl, and a health URL."""

    def __init__(self, tmp_path, monkeypatch, url: str = "", start_fails: bool = False):
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        (self.bindir / "pistation").write_bytes(b"old")
        (self.bindir / "other").write_bytes(b"other")
        self.tarball = make_tarball(tmp_path / "new.tar.gz", b"new")
        self.systemctl_calls = []
        self.start_fails = start_fails
        self.seen_binary = []

        sections = {
            "app": {"name": "PiStation", "service_manager": "systemd",
                    "service_unit": "pistation.service"},
            "install": {"target_dir": str(self.bindir)},
            "health": {"url": url, "attempts": 2, "window_seconds": 2},
        }
        config = config_module.Config(config_module.deep_merge(config_module.DEFAULTS, sections))

        def fake_systemctl(action, unit, timeout=30, log=None):
            self.systemctl_calls.append(action)
            self.seen_binary.append((action, (self.bindir / "pistation").read_bytes()))
            if action == "start" and self.start_fails and len(self.systemctl_calls) == 2:
                raise ProcessError("systemctl start pistation.service failed (exit 1)")

        monkeypatch.setattr(process, "run_systemctl", fake_systemctl)

        original = health.wait_until_healthy
        monkeypatch.setattr(
            updater_module.health_module, "wait_until_healthy",
            lambda url, attempts, window, log=None: original(
                url, attempts, window, log=log, sleep=no_sleep
            ),
        )

        self.updater = Updater.__new__(Updater)
        self.updater.config = config
        self.updater.host = SimpleNamespace(os_family="linux", arch="armv6l", describe=lambda: "linux")
        self.updater.status_callback = lambda message: None
        self.check = SimpleNamespace(asset_kind="tar", latest_version="2.0.0")
        self.report = SimpleNamespace(describe=lambda: "verified")

    def run(self):
        return self.updater.install(self.check, self.tarball, self.report)


def test_healthy_update_keeps_new_binary_and_backup(tmp_path, monkeypatch):
    """HTTP 200 after start keeps the new binary and leaves .old in place."""
    with HealthServer(200) as server:
        scenario = Scenario(tmp_path, monkeypatch, url=server.url)

        outcome = scenario.run()

    assert outcome.success
    assert (scenario.bindir / "pistation").read_bytes() == b"new"
    assert (scenario.bindir / "pistation.old").read_bytes() == b"old"
    assert scenario.systemctl_calls == ["stop", "start"]


def test_unhealthy_update_rolls_back_and_exits_5(tmp_path, monkeypatch):
    """No 200 within the attempts restores the old binary, restarts, and exits 5."""
    with HealthServer(500) as server:
        scenario = Scenario(tmp_path, monkeypatch, url=server.url)

        with pytest.raises(InstallError) as caught:
            scenario.run()

        assert server.hits == 2

    assert caught.value.exit_code == ExitCode.INSTALL
    assert "rolled back" in str(caught.value)
    assert (scenario.bindir / "pistation").read_bytes() == b"old"
    assert not (scenario.bindir / "pistation.old").exists()
    assert (scenario.bindir / "other").read_bytes() == b"other"
    # stop, start (new), then stop, start again around the restore.
    assert scenario.systemctl_calls == ["stop", "start", "stop", "start"]
    # The restart after rollback ran against the restored binary.
    assert scenario.seen_binary[-1] == ("start", b"old")


def test_start_failure_with_health_rolls_back_and_exits_7(tmp_path, monkeypatch):
    """A unit that will not start rolls back but still reports exit 7."""
    with HealthServer(200) as server:
        scenario = Scenario(tmp_path, monkeypatch, url=server.url, start_fails=True)

        with pytest.raises(ProcessError) as caught:
            scenario.run()

    assert caught.value.exit_code == ExitCode.PROCESS
    assert (scenario.bindir / "pistation").read_bytes() == b"old"


def test_start_failure_without_health_is_exit_7_and_keeps_new(tmp_path, monkeypatch):
    """With no health.url nothing is rolled back; the start failure is exit 7."""
    scenario = Scenario(tmp_path, monkeypatch, url="", start_fails=True)

    with pytest.raises(ProcessError) as caught:
        scenario.run()

    assert caught.value.exit_code == ExitCode.PROCESS
    assert (scenario.bindir / "pistation").read_bytes() == b"new"


def test_no_health_url_means_no_probe(tmp_path, monkeypatch):
    """Behaviour is unchanged when health.url is not set."""
    def forbidden(*args, **kwargs):
        raise AssertionError("no health probe expected")

    monkeypatch.setattr(health, "probe", forbidden)
    scenario = Scenario(tmp_path, monkeypatch, url="")

    outcome = scenario.run()

    assert outcome.success
    assert (scenario.bindir / "pistation").read_bytes() == b"new"
    assert scenario.systemctl_calls == ["stop", "start"]


def test_rollback_without_backups_is_reported(tmp_path, monkeypatch):
    """An install type with no .old files cannot be rolled back; exit 5 says so."""
    scenario = Scenario(tmp_path, monkeypatch, url="http://127.0.0.1:9/healthz")
    result = SimpleNamespace(backups=[])

    with pytest.raises(InstallError, match="cannot be rolled back"):
        scenario.updater.roll_back(result, [], "unhealthy")

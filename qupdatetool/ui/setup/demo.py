"""
A fake update run for the wizard's Demo page.

Mirrors the public surface of ui/window.py's real UpdateWorker exactly
(status/progress/finished_ok/failed signals, a cancel() method, and the same
constructor shape) so UpdaterWindow can run it completely unmodified - see
the `worker_class` parameter added to UpdaterWindow.__init__. Nothing here
touches the network, the filesystem, or the parent process; it just replays
a scripted timeline using the same Progress/ReleaseNotes/UpdateCheck/
UpdateOutcome shapes the real worker produces, so the window renders
identically to a real run.

The scenario comes from config.get("meta.demo_scenario"), set by the Demo
page on a throwaway copy of the wizard's Config before it opens the window.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QThread, Signal

from ...download import Progress
from ...errors import ExitCode
from ...notes import ReleaseNotes
from ...updater import UpdateCheck, UpdateOutcome

DEMO_NOTES_BODY = (
    "- This is a **simulated** release, shown so you can see the update "
    "window and release-notes rendering before a real release exists.\n"
    "- Nothing was downloaded or installed.\n"
)

FAKE_CURRENT_VERSION = "1.0.0"
FAKE_LATEST_VERSION = "2.0.0"
FAKE_ASSET_NAME = "demo-installer.exe"
FAKE_TOTAL_BYTES = 12_400_000


class DemoUpdateWorker(QThread):
    """Scripted stand-in for UpdateWorker; never touches the network."""

    status = Signal(str)
    progress = Signal(object)
    checked = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str, int)

    def __init__(self, config, check_only: bool = False, parent=None):
        super().__init__(parent)
        self.config = config
        self.check_only = check_only
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            self._run_scenario()
        except Exception as exc:  # noqa: BLE001 - a demo must never crash the wizard
            self.failed.emit(f"Demo error: {exc}", ExitCode.ERROR)

    def _run_scenario(self) -> None:
        scenario = self.config.get("meta.demo_scenario", "success")
        app_name = self.config.app_name

        self.status.emit(f"Checking {app_name} for updates (demo)")
        time.sleep(0.6)

        if scenario == "up_to_date":
            check = UpdateCheck(
                available=False,
                current_version=FAKE_LATEST_VERSION,
                latest_version=FAKE_LATEST_VERSION,
                reason=f"{FAKE_LATEST_VERSION} is already up to date",
            )
            self.checked.emit(check)
            self.finished_ok.emit(check)
            return

        notes = ReleaseNotes(title=FAKE_LATEST_VERSION, body=DEMO_NOTES_BODY, source="demo")
        check = UpdateCheck(
            available=True,
            current_version=FAKE_CURRENT_VERSION,
            latest_version=FAKE_LATEST_VERSION,
            reason=f"{FAKE_LATEST_VERSION} is newer than {FAKE_CURRENT_VERSION}",
            notes=notes,
        )
        self.checked.emit(check)

        if self.check_only:
            self.finished_ok.emit(check)
            return

        if scenario == "cancelled":
            self.status.emit(f"Downloading {FAKE_ASSET_NAME}")
            time.sleep(0.4)
            self.failed.emit("Update cancelled", ExitCode.CANCELLED)
            return

        self.status.emit(f"Downloading {FAKE_ASSET_NAME}")
        for percent in (10, 30, 55, 80, 100):
            if self._cancelled:
                self.failed.emit("Update cancelled", ExitCode.CANCELLED)
                return
            time.sleep(0.25)
            self.progress.emit(Progress(
                downloaded=int(FAKE_TOTAL_BYTES * percent / 100),
                total=FAKE_TOTAL_BYTES,
                filename=FAKE_ASSET_NAME,
                speed=4_500_000,
            ))

        if scenario == "failed":
            time.sleep(0.3)
            self.failed.emit(
                "Signature verification failed: no key matched the release signature",
                ExitCode.VERIFICATION,
            )
            return

        self.status.emit("Verifying signature")
        time.sleep(0.4)
        self.status.emit(f"Installing {FAKE_LATEST_VERSION}")
        time.sleep(0.5)
        self.status.emit(f"Restarted {app_name}")

        outcome = UpdateOutcome(
            success=True,
            installed_version=FAKE_LATEST_VERSION,
            method="demo (nothing was actually installed)",
            verification="demo signature (not real)",
            relaunched=True,
            requires_restart=False,
            messages=["This was a simulated update - nothing was downloaded or installed."],
        )
        self.finished_ok.emit(outcome)

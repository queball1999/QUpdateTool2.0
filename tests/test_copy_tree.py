"""
copy_tree tests: archive installs (Windows portable zip, Linux tarball/zip)
replace an install all or nothing.

A file that can't be written halfway through must leave the previous version
exactly as it was, not some files from each version.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from qupdatetool.errors import InstallError
from qupdatetool.install import windows
from qupdatetool.install.windows import BACKUP_PREFIX, RESTORE_PREFIX, copy_tree


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def snapshot(root: Path) -> dict:
    """Every file under root, relative path -> contents."""
    return {
        p.relative_to(root).as_posix(): p.read_text()
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture
def trees(tmp_path):
    """An installed old version with user data beside it, and a new release."""
    install = tmp_path / "install"
    write(install / "app.exe", "old app")
    write(install / "lib" / "core.dll", "old core")
    write(install / "portable.txt", "marker")
    write(install / "data" / "app.db", "user data")

    release = tmp_path / "release"
    write(release / "app.exe", "new app")
    write(release / "lib" / "core.dll", "new core")
    write(release / "lib" / "extra.dll", "new extra")
    write(release / "plugins" / "one" / "p.dll", "new plugin")
    return install, release


def test_copy_replaces_files_and_keeps_user_data(trees):
    install, release = trees

    copied = copy_tree(release, install)

    assert copied == 4
    assert snapshot(install) == {
        "app.exe": "new app",
        "lib/core.dll": "new core",
        "lib/extra.dll": "new extra",
        "plugins/one/p.dll": "new plugin",
        "portable.txt": "marker",
        "data/app.db": "user data",
    }
    assert not list(install.glob(BACKUP_PREFIX + "*"))


def test_failed_copy_restores_previous_version(trees, monkeypatch):
    install, release = trees
    before = snapshot(install)
    real_copy = shutil.copy2

    # Fail on the last file, after the others (including new folders and
    # replaced files) have already been written.
    def copy2(src, dst, *args, **kwargs):
        if Path(dst).name == "p.dll":
            Path(dst).write_text("half written")
            raise PermissionError(13, "in use", str(dst))
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(windows.shutil, "copy2", copy2)

    with pytest.raises(InstallError) as caught:
        copy_tree(release, install)

    assert "p.dll" in caught.value.message
    assert "previous version is intact" in caught.value.detail
    assert snapshot(install) == before
    assert not (install / "plugins").exists()
    assert not list(install.glob(BACKUP_PREFIX + "*"))


def test_unrestorable_rollback_keeps_originals(trees, monkeypatch):
    install, release = trees
    real_copy = shutil.copy2
    real_replace = os.replace

    def copy2(src, dst, *args, **kwargs):
        if Path(dst).name == "p.dll":
            raise PermissionError(13, "in use", str(dst))
        return real_copy(src, dst, *args, **kwargs)

    # Moving the original app.exe back fails.
    def replace(src, dst):
        if Path(src).name == "app.exe" and BACKUP_PREFIX in str(src):
            raise PermissionError(13, "in use", str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(windows.shutil, "copy2", copy2)
    monkeypatch.setattr(windows.os, "replace", replace)

    with pytest.raises(InstallError) as caught:
        copy_tree(release, install)

    assert "could not be fully restored" in caught.value.message
    kept = list(install.glob(RESTORE_PREFIX + "*"))
    assert len(kept) == 1
    assert (kept[0] / "app.exe").read_text() == "old app"
    assert str(kept[0]) in caught.value.detail

    # The next update's cleanup leaves the only copy of the originals alone.
    monkeypatch.setattr(windows.shutil, "copy2", real_copy)
    copy_tree(release, install)
    assert (kept[0] / "app.exe").read_text() == "old app"


def test_stale_backups_are_removed(trees):
    install, release = trees
    write(install / (BACKUP_PREFIX + "old") / "app.exe", "older app")

    copy_tree(release, install)

    assert not list(install.glob(BACKUP_PREFIX + "*"))

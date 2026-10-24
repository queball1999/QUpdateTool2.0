"""
Single-binary tarball install tests.

A release tarball holding only the `pistation` binary, installed into a
shared directory such as /usr/local/bin, must replace just that file
atomically, keep the previous one as `<name>.old`, keep the executable bit,
and leave every other file in the directory alone.
"""

from __future__ import annotations

import io
import os
import stat
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from qupdatetool import config as config_module
from qupdatetool.errors import InstallError
from qupdatetool.install import linux
from qupdatetool.install.linux import LinuxInstaller, replace_file_atomic

POSIX_ONLY = pytest.mark.skipif(sys.platform.startswith("win"), reason="needs POSIX file modes")


def make_tarball(path: Path, members: dict, mode: int = 0o755) -> Path:
    """Write a .tar.gz containing the given name -> bytes members."""
    with tarfile.open(path, "w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            archive.addfile(info, io.BytesIO(data))
    return path


def make_installer(target_dir: Path) -> LinuxInstaller:
    """Build a LinuxInstaller whose install.target_dir is `target_dir`."""
    config = config_module.Config(
        config_module.deep_merge(config_module.DEFAULTS, {"install": {"target_dir": str(target_dir)}})
    )
    return LinuxInstaller(config, SimpleNamespace(os_family="linux"))


@pytest.fixture
def bindir(tmp_path):
    """A shared bin directory with an old pistation and unrelated files."""
    directory = tmp_path / "bin"
    directory.mkdir()
    (directory / "pistation").write_bytes(b"old binary")
    (directory / "pistation").chmod(0o755)
    (directory / "other-tool").write_bytes(b"other")
    (directory / "pistation.conf").write_bytes(b"conf")
    return directory


def test_single_binary_replaced_with_backup(bindir, tmp_path):
    """The binary is replaced, the old one kept as .old, others untouched."""
    tarball = make_tarball(tmp_path / "pistation-v2-linux-armv6.tar.gz", {"pistation": b"new binary"})

    result = make_installer(bindir).install(tarball, "tar")

    assert result.success
    assert (bindir / "pistation").read_bytes() == b"new binary"
    assert (bindir / "pistation.old").read_bytes() == b"old binary"
    assert (bindir / "other-tool").read_bytes() == b"other"
    assert (bindir / "pistation.conf").read_bytes() == b"conf"
    assert result.backups == [(bindir / "pistation", bindir / "pistation.old")]


def test_no_leftover_files(bindir, tmp_path):
    """No temp files or backup folders are left in the target directory."""
    tarball = make_tarball(tmp_path / "a.tar.gz", {"pistation": b"new"})

    make_installer(bindir).install(tarball, "tar")

    assert sorted(p.name for p in bindir.iterdir()) == [
        "other-tool", "pistation", "pistation.conf", "pistation.old",
    ]


def test_first_install_has_no_backup(tmp_path):
    """With no previous binary there is nothing to back up."""
    target = tmp_path / "bin"
    target.mkdir()
    tarball = make_tarball(tmp_path / "a.tar.gz", {"pistation": b"new"})

    result = make_installer(target).install(tarball, "tar")

    assert (target / "pistation").read_bytes() == b"new"
    assert not (target / "pistation.old").exists()
    assert result.backups == []


def test_second_update_replaces_old_backup(bindir, tmp_path):
    """.old always holds the version just before the latest update."""
    installer = make_installer(bindir)
    installer.install(make_tarball(tmp_path / "1.tar.gz", {"pistation": b"v2"}), "tar")
    installer.install(make_tarball(tmp_path / "2.tar.gz", {"pistation": b"v3"}), "tar")

    assert (bindir / "pistation").read_bytes() == b"v3"
    assert (bindir / "pistation.old").read_bytes() == b"v2"


def test_top_level_folder_in_tarball_is_unwrapped(bindir, tmp_path):
    """pistation-v2/pistation inside the tarball still replaces one file."""
    tarball = make_tarball(tmp_path / "a.tar.gz", {"pistation-v2/pistation": b"new"})

    make_installer(bindir).install(tarball, "tar")

    assert (bindir / "pistation").read_bytes() == b"new"
    assert (bindir / "other-tool").exists()


def test_multi_file_archive_still_uses_copy_tree(bindir, tmp_path):
    """More than one file keeps the existing all-or-nothing tree copy."""
    tarball = make_tarball(
        tmp_path / "a.tar.gz", {"pistation": b"new", "README": b"docs"}
    )

    result = make_installer(bindir).install(tarball, "tar")

    assert result.backups == []
    assert (bindir / "README").read_bytes() == b"docs"
    assert not (bindir / "pistation.old").exists()


@POSIX_ONLY
def test_executable_bit_preserved(bindir, tmp_path):
    """The new binary is executable."""
    tarball = make_tarball(tmp_path / "a.tar.gz", {"pistation": b"new"}, mode=0o755)

    make_installer(bindir).install(tarball, "tar")

    mode = stat.S_IMODE((bindir / "pistation").stat().st_mode)
    assert mode & 0o111 == 0o111
    assert mode == 0o755


@POSIX_ONLY
def test_executable_bit_kept_when_archive_drops_it(bindir, tmp_path):
    """A source without exec bits (zip) still leaves an executable file."""
    source = tmp_path / "new"
    source.write_bytes(b"new")
    source.chmod(0o644)

    replace_file_atomic(source, bindir / "pistation")

    assert os.access(bindir / "pistation", os.X_OK)


def test_write_failure_leaves_previous_binary(bindir, tmp_path, monkeypatch):
    """If the final rename fails, the old binary is intact and no temp file remains."""
    source = tmp_path / "new"
    source.write_bytes(b"new")
    real_replace = os.replace

    def failing_replace(src, dst):
        if Path(dst).name == "pistation":
            raise PermissionError("denied")
        return real_replace(src, dst)

    monkeypatch.setattr(linux.os, "replace", failing_replace)

    with pytest.raises(InstallError) as caught:
        replace_file_atomic(source, bindir / "pistation")

    assert "previous file was left in place" in str(caught.value)
    assert (bindir / "pistation").read_bytes() == b"old binary"
    assert sorted(p.name for p in bindir.iterdir() if p.name.startswith(".qupdate")) == []


def test_atomic_replace_never_writes_to_destination_directly(bindir, tmp_path, monkeypatch):
    """The new content is staged in the same directory, then renamed."""
    source = tmp_path / "new"
    source.write_bytes(b"new")
    renames = []
    real_replace = os.replace

    def spy(src, dst):
        renames.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(linux.os, "replace", spy)

    replace_file_atomic(source, bindir / "pistation")

    final = renames[-1]
    assert final[1] == bindir / "pistation"
    assert final[0].parent == bindir
    assert final[0].name.startswith(".qupdate-new-")

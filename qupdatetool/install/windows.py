"""
Windows installers: Inno Setup and NSIS executables, MSI packages, and zips.

The default silent flags target Inno Setup, because that is what most small
Windows applications ship, but the installer family is detected from the file
itself where possible so an NSIS installer gets NSIS flags rather than Inno
ones. A publisher can always override the flags in config.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from ..errors import InstallError
from ..platforms import KIND_EXE_INSTALLER, KIND_MSI, KIND_ZIP
from .base import Installer, InstallResult

# Silent-install flags by installer family. Inno and NSIS both accept /S but
# Inno prefers /VERYSILENT, which also suppresses the progress window.
INNO_SILENT = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]
NSIS_SILENT = ["/S"]
MSI_SILENT = ["/quiet", "/norestart"]

# Exit codes an MSI or installer can return that are successes, not failures.
# 3010 means "success, a reboot is required", which is not an error.
SUCCESS_EXIT_CODES = {0, 3010, 1641}

# Backup folders copy_tree creates inside an install while it replaces files.
BACKUP_PREFIX = ".qupdate-backup-"
# A backup that a failed rollback could not fully put back. Never removed
# automatically: it holds the only copy of the files it lists.
RESTORE_PREFIX = ".qupdate-restore-"


class WindowsInstaller(Installer):
    """Applies Windows release artifacts."""

    name = "windows"
    supported_kinds = (KIND_EXE_INSTALLER, KIND_MSI, KIND_ZIP)

    def install(self, artifact: Path, kind: str) -> InstallResult:
        if kind == KIND_MSI:
            return self.install_msi(artifact)
        if kind == KIND_ZIP:
            return self.install_zip(artifact)
        return self.install_exe(artifact)

    def detect_installer_family(self, artifact: Path) -> str:
        """
        Guess whether an installer .exe is Inno Setup, NSIS, or unknown.

        Both toolchains leave recognisable strings in the binary. Reading the
        first megabyte is enough to find them and cheap enough not to matter.
        Getting this wrong is not fatal - it only changes which silent flags
        are tried first - so an unknown result falls back to Inno flags.
        """
        try:
            with open(artifact, "rb") as handle:
                head = handle.read(1024 * 1024)
        except OSError:
            return "unknown"

        lowered = head.lower()

        if b"inno setup" in lowered or b"jr.inno.setup" in lowered:
            return "inno"
        if b"nullsoft" in lowered or b"nsis" in lowered:
            return "nsis"

        return "unknown"

    def install_exe(self, artifact: Path) -> InstallResult:
        """
        Run an installer executable, silently or interactively.

        In interactive mode the installer runs with no flags at all, so the
        user sees and drives the vendor wizard exactly as they would from a
        manual download.
        """
        configured = self.installer_args(KIND_EXE_INSTALLER)

        if self.interactive:
            args = configured  # usually empty: show the real wizard
            mode = "interactive"
        elif configured:
            args = configured
            mode = "silent (configured flags)"
        else:
            family = self.detect_installer_family(artifact)
            if family == "nsis":
                args = list(NSIS_SILENT)
            else:
                args = list(INNO_SILENT)
            mode = f"silent ({family} flags)"

        exit_code, output = self.run([artifact] + args)

        if exit_code not in SUCCESS_EXIT_CODES:
            self.fail(f"The installer failed (exit code {exit_code})", exit_code, output)

        return InstallResult(
            success=True,
            method=f"Windows installer, {mode}",
            exit_code=exit_code,
            output=output,
            requires_restart=exit_code in (3010, 1641),
        )

    def install_msi(self, artifact: Path) -> InstallResult:
        """Install an MSI package through msiexec."""
        configured = self.installer_args(KIND_MSI)

        if self.interactive:
            args = configured or []
            mode = "interactive"
        else:
            args = configured or list(MSI_SILENT)
            mode = "silent"

        command = ["msiexec", "/i", str(artifact)] + args

        exit_code, output = self.run(command)

        if exit_code not in SUCCESS_EXIT_CODES:
            self.fail(f"msiexec failed (exit code {exit_code})", exit_code, output)

        return InstallResult(
            success=True,
            method=f"MSI package, {mode}",
            exit_code=exit_code,
            output=output,
            requires_restart=exit_code in (3010, 1641),
        )

    def install_zip(self, artifact: Path) -> InstallResult:
        """
        Unpack a portable zip over the existing install.

        The archive is extracted to a scratch directory first and only copied
        over the install once extraction has fully succeeded, so a corrupt or
        truncated archive cannot leave the application half-replaced. A
        backup of the previous install is kept until the copy completes.
        """
        target = self.config.get("install.target_dir", "") or self.config.install_dir
        if not target:
            raise InstallError(
                "Cannot install a portable archive without a target directory",
                "Set install.target_dir or app.executable",
            )

        target_dir = Path(target)
        target_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="qupdate-zip-") as scratch:
            staging = Path(scratch) / "extracted"
            staging.mkdir(parents=True)

            try:
                with zipfile.ZipFile(artifact) as archive:
                    check_archive_members(archive.namelist())
                    archive.extractall(staging)
            except zipfile.BadZipFile as exc:
                raise InstallError("The downloaded archive is corrupt", str(exc)) from exc

            # A zip that contains a single top-level folder is unwrapped, which
            # is the usual layout for a portable release.
            entries = list(staging.iterdir())
            source = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging

            copied = copy_tree(source, target_dir, log=self.log)

        return InstallResult(
            success=True,
            method="portable archive extracted in place",
            installed_path=str(target_dir),
            notes=[f"{copied} files updated"],
        )


def check_archive_members(names: list) -> None:
    """
    Reject archives containing path traversal or absolute paths.

    An archive is attacker-controlled input right up until its signature has
    been verified, and even a verified one can be malformed. Extracting
    "../../Windows/System32/..." would be catastrophic, so it is refused
    outright rather than sanitised.
    """
    for name in names:
        normalised = name.replace("\\", "/")

        if normalised.startswith("/") or (len(normalised) > 1 and normalised[1] == ":"):
            raise InstallError(
                "The archive contains an absolute path",
                f"refusing to extract {name}",
            )

        parts = [part for part in normalised.split("/") if part not in ("", ".")]
        depth = 0
        for part in parts:
            depth += -1 if part == ".." else 1
            if depth < 0:
                raise InstallError(
                    "The archive contains a path traversal entry",
                    f"refusing to extract {name}",
                )


def copy_tree(source: Path, target: Path, log=None) -> int:
    """
    Copy a directory tree over another, returning the number of files written.

    Files are replaced individually rather than the directory being wiped
    first, so user data living alongside the application survives an update.

    The copy is all or nothing. Each file about to be overwritten is first
    moved into a backup folder inside the target, and if any later file
    cannot be written (locked, no permission, disk full) every change is
    undone: new files and folders are removed and the originals moved back.
    Without this, a file locked halfway through would leave some files from
    the new version beside some from the old one, which is an install that
    may not start at all.

    The backup lives inside the target so moving a file there is a rename on
    the same volume. That also means a running executable can be replaced:
    Windows refuses to overwrite one, but allows renaming it.
    """
    remove_stale_backups(target, log)
    backup_root = Path(tempfile.mkdtemp(prefix=BACKUP_PREFIX, dir=target))

    replaced = []        # (destination, backup) for each file moved aside
    created_files = []   # files that did not exist before
    created_dirs = []    # folders that did not exist before, parents first
    count = 0

    try:
        for root, _, filenames in os.walk(source):
            root_path = Path(root)
            relative = root_path.relative_to(source)
            destination_dir = target / relative
            make_dirs(destination_dir, target, created_dirs)

            for filename in filenames:
                source_file = root_path / filename
                destination_file = destination_dir / filename

                if destination_file.exists() or destination_file.is_symlink():
                    backup = backup_root / relative / filename
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination_file, backup)
                    replaced.append((destination_file, backup))
                else:
                    # Recorded before the copy, so a partly written file is
                    # removed on rollback too.
                    created_files.append(destination_file)

                shutil.copy2(source_file, destination_file)
                count += 1
    except OSError as exc:
        name = Path(exc.filename).name if exc.filename else "a file"
        unrestored = roll_back(replaced, created_files, created_dirs)

        if unrestored:
            # Renamed out of BACKUP_PREFIX so the next update's stale-backup
            # sweep doesn't delete the only copy of those originals.
            kept = backup_root.with_name(
                RESTORE_PREFIX + backup_root.name[len(BACKUP_PREFIX):]
            )
            try:
                os.replace(backup_root, kept)
                backup_root = kept
            except OSError:
                pass
            raise InstallError(
                f"Could not replace {name}, and the previous version could "
                "not be fully restored",
                f"{exc}. Originals of {len(unrestored)} files are still in "
                f"{backup_root}; copy them back by hand",
            ) from exc

        shutil.rmtree(backup_root, ignore_errors=True)
        raise InstallError(
            f"Could not replace {name}",
            "The file is in use or the updater lacks permission. Nothing was "
            f"changed; the previous version is intact. {exc}",
        ) from exc

    # A renamed executable that is still running cannot be deleted yet; the
    # next update removes whatever is left.
    shutil.rmtree(backup_root, ignore_errors=True)

    if log:
        log(f"Copied {count} files into {target}")

    return count



def make_dirs(directory: Path, root: Path, created: list) -> None:
    """
    Create directory and any missing parents below root, recording each
    folder created so a rollback can remove it again.
    """
    missing = []
    current = directory
    while current != root and not current.is_dir():
        missing.append(current)
        current = current.parent

    for folder in reversed(missing):
        folder.mkdir()
        created.append(folder)


def roll_back(replaced: list, created_files: list, created_dirs: list) -> list:
    """
    Undo a partial copy_tree: remove the files and folders it added, then move
    each original back. Returns the originals that could not be restored.
    """
    for path in reversed(created_files):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    unrestored = []
    for destination, backup in reversed(replaced):
        try:
            os.replace(backup, destination)
        except OSError:
            unrestored.append(destination)

    # Deepest first; a folder that still holds something is left alone.
    for folder in reversed(created_dirs):
        try:
            folder.rmdir()
        except OSError:
            pass

    return unrestored


def remove_stale_backups(target: Path, log=None) -> None:
    """
    Delete backup folders left by an earlier update, typically because they
    held the old copy of an executable that was still running at the time.
    """
    for entry in target.glob(BACKUP_PREFIX + "*"):
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
            if log and entry.exists():
                log(f"Could not remove old backup {entry}")

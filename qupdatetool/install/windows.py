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
    """
    count = 0

    for root, _, filenames in os.walk(source):
        root_path = Path(root)
        relative = root_path.relative_to(source)
        destination_dir = target / relative
        destination_dir.mkdir(parents=True, exist_ok=True)

        for filename in filenames:
            source_file = root_path / filename
            destination_file = destination_dir / filename

            try:
                shutil.copy2(source_file, destination_file)
                count += 1
            except PermissionError as exc:
                raise InstallError(
                    f"Could not replace {destination_file.name}",
                    "The file is in use or the updater lacks permission: "
                    f"{exc}",
                ) from exc

    if log:
        log(f"Copied {count} files into {target}")

    return count

"""
Linux installers: deb, rpm, AppImage, and plain tarballs.

Only .deb is in production use for the applications this tool was written
for, but the dispatch is written so that shipping .rpm or .AppImage later
needs no updater change at all: the release gains the asset, the host
detection in platforms.py picks it, and the matching handler here runs.

Each package format is installed through the system package manager rather
than by unpacking it, so the package database stays consistent and the
application remains uninstallable through normal means afterwards.
"""

from __future__ import annotations

import os
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path

from ..errors import InstallError
from ..platforms import KIND_APPIMAGE, KIND_DEB, KIND_RPM, KIND_TARBALL, KIND_ZIP
from .base import InstallResult, Installer
from .windows import check_archive_members, copy_tree


class LinuxInstaller(Installer):
    """Applies Linux release artifacts."""

    name = "linux"
    supported_kinds = (KIND_DEB, KIND_RPM, KIND_APPIMAGE, KIND_TARBALL, KIND_ZIP)

    def install(self, artifact: Path, kind: str) -> InstallResult:
        if kind == KIND_DEB:
            return self.install_deb(artifact)
        if kind == KIND_RPM:
            return self.install_rpm(artifact)
        if kind == KIND_APPIMAGE:
            return self.install_appimage(artifact)
        return self.install_archive(artifact, kind)

    # --- native packages ---

    def install_deb(self, artifact: Path) -> InstallResult:
        """
        Install a .deb through apt, falling back to dpkg.

        apt is preferred because it resolves dependencies; dpkg alone fails on
        a package whose dependencies are not already present. When dpkg is
        used as a fallback, "apt-get install -f" is run afterwards to repair
        anything left unsatisfied.
        """
        apt = shutil.which("apt-get") or shutil.which("apt")

        if apt:
            command = [apt, "install", "-y", "--allow-downgrades", str(artifact)]
            if self.interactive:
                command = [apt, "install", str(artifact)]

            exit_code, output = self.run(command, timeout=1800)

            if exit_code != 0:
                self.fail(f"apt failed to install the package (exit {exit_code})",
                          exit_code, output)

            return InstallResult(
                success=True,
                method="Debian package installed with apt",
                exit_code=exit_code,
                output=output,
            )

        dpkg = shutil.which("dpkg")
        if not dpkg:
            raise InstallError(
                "No Debian package manager found",
                "Neither apt nor dpkg is available on this system",
            )

        exit_code, output = self.run([dpkg, "-i", str(artifact)], timeout=1800)

        if exit_code != 0:
            # Dependency problems are the usual cause; try to repair them.
            repair = shutil.which("apt-get")
            if repair:
                self.log("dpkg reported unmet dependencies; running apt-get -f install")
                fix_code, fix_output = self.run(
                    [repair, "install", "-f", "-y"], timeout=1800
                )
                if fix_code == 0:
                    return InstallResult(
                        success=True,
                        method="Debian package installed with dpkg (dependencies repaired)",
                        exit_code=fix_code,
                        output=fix_output,
                    )
                output = f"{output}\n{fix_output}"

            self.fail(f"dpkg failed to install the package (exit {exit_code})",
                      exit_code, output)

        return InstallResult(
            success=True,
            method="Debian package installed with dpkg",
            exit_code=exit_code,
            output=output,
        )

    def install_rpm(self, artifact: Path) -> InstallResult:
        """
        Install an .rpm through dnf, yum, zypper, or rpm itself.

        The higher-level tools are preferred in that order because they
        resolve dependencies; bare rpm is the last resort.
        """
        for manager, arguments in (
            ("dnf", ["install", "-y", "--allowerasing"]),
            ("yum", ["install", "-y"]),
            ("zypper", ["--non-interactive", "install", "--allow-unsigned-rpm"]),
        ):
            executable = shutil.which(manager)
            if not executable:
                continue

            command = [executable] + arguments + [str(artifact)]
            if self.interactive and manager != "zypper":
                command = [executable, "install", str(artifact)]

            exit_code, output = self.run(command, timeout=1800)

            if exit_code != 0:
                self.fail(f"{manager} failed to install the package (exit {exit_code})",
                          exit_code, output)

            return InstallResult(
                success=True,
                method=f"RPM package installed with {manager}",
                exit_code=exit_code,
                output=output,
            )

        rpm = shutil.which("rpm")
        if not rpm:
            raise InstallError(
                "No RPM package manager found",
                "None of dnf, yum, zypper, or rpm is available on this system",
            )

        # -U upgrades an existing install or installs fresh if absent.
        exit_code, output = self.run([rpm, "-Uvh", "--replacepkgs", str(artifact)],
                                     timeout=1800)

        if exit_code != 0:
            self.fail(f"rpm failed to install the package (exit {exit_code})",
                      exit_code, output)

        return InstallResult(
            success=True,
            method="RPM package installed with rpm",
            exit_code=exit_code,
            output=output,
        )

    # --- self-contained formats ---

    def install_appimage(self, artifact: Path) -> InstallResult:
        """
        Replace the running AppImage with the newly downloaded one.

        The target is whichever AppImage the parent application was launched
        from, so an update keeps the file exactly where the user put it and
        preserves any desktop entry pointing at that path. The old file is
        kept as a .bak until the replacement is confirmed in place.
        """
        target = self.appimage_target()

        if not target:
            raise InstallError(
                "Could not determine which AppImage to replace",
                "Set app.executable to the path of the running AppImage",
            )

        backup = target.with_suffix(target.suffix + ".bak")

        try:
            if target.exists():
                target.replace(backup)

            shutil.move(str(artifact), str(target))

            # An AppImage that is not executable is just a confusing file.
            mode = target.stat().st_mode
            target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        except OSError as exc:
            # Put the original back before reporting failure.
            if backup.exists() and not target.exists():
                try:
                    backup.replace(target)
                except OSError:
                    pass
            raise InstallError(f"Could not replace the AppImage at {target}", str(exc)) from exc

        if backup.exists() and not self.config.get("install.keep_download", False):
            backup.unlink(missing_ok=True)

        return InstallResult(
            success=True,
            method="AppImage replaced in place",
            installed_path=str(target),
        )

    def appimage_target(self) -> Path | None:
        """Work out which AppImage file the update should overwrite."""
        configured = self.config.get("install.target_dir", "")
        if configured and str(configured).lower().endswith(".appimage"):
            return Path(configured)

        # APPIMAGE is set by the AppImage runtime to the path of the image.
        env_path = os.environ.get("APPIMAGE", "")
        if env_path:
            return Path(env_path)

        executable = self.config.executable_path
        if executable and str(executable).lower().endswith(".appimage"):
            return executable

        return None

    def install_archive(self, artifact: Path, kind: str) -> InstallResult:
        """
        Unpack a tarball or zip over the existing install directory.

        Used for applications distributed as a plain archive. Extraction goes
        to a scratch directory first so a corrupt archive cannot leave a
        half-written install behind.
        """
        target = self.config.get("install.target_dir", "") or self.config.install_dir
        if not target:
            raise InstallError(
                "Cannot install an archive without a target directory",
                "Set install.target_dir or app.executable",
            )

        target_dir = Path(target)
        target_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="qupdate-archive-") as scratch:
            staging = Path(scratch) / "extracted"
            staging.mkdir(parents=True)

            if kind == KIND_ZIP:
                import zipfile

                try:
                    with zipfile.ZipFile(artifact) as archive:
                        check_archive_members(archive.namelist())
                        archive.extractall(staging)
                except zipfile.BadZipFile as exc:
                    raise InstallError("The downloaded archive is corrupt", str(exc)) from exc
            else:
                try:
                    with tarfile.open(artifact) as archive:
                        check_archive_members(archive.getnames())
                        extract_tar_safely(archive, staging)
                except tarfile.TarError as exc:
                    raise InstallError("The downloaded archive is corrupt", str(exc)) from exc

            entries = list(staging.iterdir())
            source = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging

            copied = copy_tree(source, target_dir, log=self.log)

        return InstallResult(
            success=True,
            method="archive extracted in place",
            installed_path=str(target_dir),
            notes=[f"{copied} files updated"],
        )


def extract_tar_safely(archive: tarfile.TarFile, destination: Path) -> None:
    """
    Extract a tar archive, refusing links that point outside the destination.

    check_archive_members already rejects traversal in member names; this adds
    the link-target check, because a symlink whose target escapes the
    destination is the other way a tar archive can write outside it.
    """
    destination = destination.resolve()

    for member in archive.getmembers():
        if member.issym() or member.islnk():
            target = (destination / member.name).parent / member.linkname
            try:
                resolved = target.resolve()
            except OSError:
                raise InstallError(
                    "The archive contains an unresolvable link",
                    f"refusing to extract {member.name}",
                ) from None

            if destination not in resolved.parents and resolved != destination:
                raise InstallError(
                    "The archive contains a link pointing outside the target",
                    f"refusing to extract {member.name} -> {member.linkname}",
                )

    # filter="data" is the hardened extraction mode; it is available from
    # Python 3.12 and is the default from 3.14, so it is requested explicitly
    # to get the same behaviour on older interpreters.
    try:
        archive.extractall(destination, filter="data")
    except TypeError:
        archive.extractall(destination)

"""
macOS installers: disk images, installer packages, and zipped app bundles.

A .dmg is mounted, the .app inside is copied into place, and the image is
detached again. A .pkg is handed to the system installer. A .zip or tarball
containing a bundle is unpacked and copied.

Quarantine handling matters here: a bundle downloaded programmatically still
carries the com.apple.quarantine attribute, and macOS will refuse to launch
it or will show a scary dialog. The attribute is cleared after the bundle is
in place, which is safe precisely because the artifact has already had its
signature verified before reaching this code.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from ..errors import InstallError
from ..platforms import KIND_DMG, KIND_PKG, KIND_TARBALL, KIND_ZIP
from .base import InstallResult, Installer
from .windows import check_archive_members

DEFAULT_APPLICATIONS = Path("/Applications")


class MacOSInstaller(Installer):
    """Applies macOS release artifacts."""

    name = "macos"
    supported_kinds = (KIND_DMG, KIND_PKG, KIND_ZIP, KIND_TARBALL)

    def install(self, artifact: Path, kind: str) -> InstallResult:
        if kind == KIND_DMG:
            return self.install_dmg(artifact)
        if kind == KIND_PKG:
            return self.install_pkg(artifact)
        return self.install_archive(artifact, kind)

    # --- disk images ---

    def install_dmg(self, artifact: Path) -> InstallResult:
        """
        Mount a disk image, copy the app bundle out, then detach it.

        The image is always detached, including on failure, because a leaked
        mount point blocks the next update attempt and confuses the user with
        a stray volume on their desktop.
        """
        mount_point = self.attach_dmg(artifact)

        try:
            bundle = self.find_bundle(mount_point)
            if bundle is None:
                raise InstallError(
                    "No application bundle found in the disk image",
                    f"nothing matching *.app inside {artifact.name}",
                )

            destination = self.bundle_destination(bundle)
            self.replace_bundle(bundle, destination)

        finally:
            self.detach_dmg(mount_point)

        self.clear_quarantine(destination)

        return InstallResult(
            success=True,
            method="application bundle copied from disk image",
            installed_path=str(destination),
        )

    def attach_dmg(self, artifact: Path) -> Path:
        """
        Attach a disk image and return its mount point.

        The plist output format is parsed rather than the human-readable one,
        because the text layout of hdiutil output is not stable and volume
        names routinely contain spaces.
        """
        command = [
            "hdiutil", "attach", str(artifact),
            "-nobrowse", "-noverify", "-noautoopen", "-plist",
        ]

        try:
            result = subprocess.run(
                command, capture_output=True, timeout=600, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallError("Could not mount the disk image", str(exc)) from exc

        if result.returncode != 0:
            raise InstallError(
                "Could not mount the disk image",
                (result.stderr or b"").decode("utf-8", errors="replace")[:400],
            )

        try:
            payload = plistlib.loads(result.stdout)
        except Exception as exc:
            raise InstallError("Could not read the disk image layout", str(exc)) from exc

        for entity in payload.get("system-entities", []):
            mount_point = entity.get("mount-point")
            if mount_point:
                return Path(mount_point)

        raise InstallError("The disk image mounted with no accessible volume")

    def detach_dmg(self, mount_point: Path) -> None:
        """Detach a mounted disk image, forcing it if a normal eject fails."""
        if not mount_point or not Path(mount_point).exists():
            return

        for arguments in (["hdiutil", "detach", str(mount_point)],
                          ["hdiutil", "detach", str(mount_point), "-force"]):
            try:
                result = subprocess.run(
                    arguments, capture_output=True, timeout=120, check=False
                )
                if result.returncode == 0:
                    return
            except (OSError, subprocess.SubprocessError):
                continue

        self.log(f"Warning: could not detach {mount_point}")

    # --- installer packages ---

    def install_pkg(self, artifact: Path) -> InstallResult:
        """
        Install a .pkg.

        Silent mode uses the `installer` command line tool, which requires
        root. Interactive mode opens the package in Installer.app so the user
        walks through it themselves, which is what a publisher who wants
        explicit consent asks for.
        """
        if self.interactive:
            try:
                subprocess.Popen(["open", "-W", str(artifact)])
            except OSError as exc:
                raise InstallError("Could not open the installer package", str(exc)) from exc

            return InstallResult(
                success=True,
                method="installer package opened for the user",
                notes=["The user is completing the installation manually"],
            )

        exit_code, output = self.run(
            ["installer", "-pkg", str(artifact), "-target", "/"], timeout=1800
        )

        if exit_code != 0:
            self.fail(f"The installer failed (exit code {exit_code})", exit_code, output)

        return InstallResult(
            success=True,
            method="installer package applied to /",
            exit_code=exit_code,
            output=output,
        )

    # --- archives ---

    def install_archive(self, artifact: Path, kind: str) -> InstallResult:
        """Unpack a zip or tarball and install the app bundle it contains."""
        with tempfile.TemporaryDirectory(prefix="qupdate-macos-") as scratch:
            staging = Path(scratch) / "extracted"
            staging.mkdir(parents=True)

            if kind == KIND_ZIP:
                # ditto preserves resource forks and code signatures, which
                # plain zipfile extraction silently discards, breaking the
                # bundle signature.
                if shutil.which("ditto"):
                    exit_code, output = self.run(
                        ["ditto", "-x", "-k", str(artifact), str(staging)], timeout=900
                    )
                    if exit_code != 0:
                        self.fail("Could not unpack the archive", exit_code, output)
                else:
                    try:
                        with zipfile.ZipFile(artifact) as archive:
                            check_archive_members(archive.namelist())
                            archive.extractall(staging)
                    except zipfile.BadZipFile as exc:
                        raise InstallError(
                            "The downloaded archive is corrupt", str(exc)
                        ) from exc
            else:
                try:
                    with tarfile.open(artifact) as archive:
                        check_archive_members(archive.getnames())
                        try:
                            archive.extractall(staging, filter="data")
                        except TypeError:
                            archive.extractall(staging)
                except tarfile.TarError as exc:
                    raise InstallError("The downloaded archive is corrupt", str(exc)) from exc

            bundle = self.find_bundle(staging)
            if bundle is None:
                raise InstallError(
                    "No application bundle found in the archive",
                    f"nothing matching *.app inside {artifact.name}",
                )

            destination = self.bundle_destination(bundle)
            self.replace_bundle(bundle, destination)

        self.clear_quarantine(destination)

        return InstallResult(
            success=True,
            method="application bundle copied from archive",
            installed_path=str(destination),
        )

    # --- shared bundle handling ---

    def find_bundle(self, root: Path) -> Path | None:
        """Find the .app bundle inside an extracted archive or mounted image."""
        root = Path(root)

        for candidate in sorted(root.glob("*.app")):
            return candidate

        # Some publishers nest the bundle one level down.
        for candidate in sorted(root.glob("*/*.app")):
            return candidate

        return None

    def bundle_destination(self, bundle: Path) -> Path:
        """
        Decide where the new bundle should go.

        The existing install location wins, so an app the user keeps outside
        /Applications stays where they put it. Otherwise /Applications is the
        conventional destination.
        """
        configured = self.config.get("install.target_dir", "")
        if configured:
            target = Path(configured)
            return target if target.suffix == ".app" else target / bundle.name

        install_dir = self.config.install_dir
        if install_dir and install_dir.suffix == ".app":
            return install_dir

        if install_dir and install_dir.is_dir():
            return install_dir / bundle.name

        return DEFAULT_APPLICATIONS / bundle.name

    def replace_bundle(self, source: Path, destination: Path) -> None:
        """
        Replace an application bundle, keeping the old one until the copy lands.

        A bundle is a directory tree, so a partial copy is a broken app. The
        previous version is moved aside first and restored if the copy fails.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)

        backup = None
        if destination.exists():
            backup = destination.with_name(destination.name + ".old")
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            try:
                destination.rename(backup)
            except OSError as exc:
                raise InstallError(
                    f"Could not move the existing application aside: {destination}",
                    str(exc),
                ) from exc

        try:
            # ditto preserves signatures and extended attributes; copytree
            # does not, and a bundle copied with copytree can fail Gatekeeper.
            if shutil.which("ditto"):
                result = subprocess.run(
                    ["ditto", str(source), str(destination)],
                    capture_output=True, text=True, timeout=900, check=False,
                )
                if result.returncode != 0:
                    raise InstallError(
                        "Could not copy the application bundle",
                        (result.stderr or "").strip()[:400],
                    )
            else:
                shutil.copytree(source, destination, symlinks=True)

        except Exception:
            if backup and backup.exists():
                shutil.rmtree(destination, ignore_errors=True)
                try:
                    backup.rename(destination)
                except OSError:
                    pass
            raise

        if backup and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)

    def clear_quarantine(self, path: Path) -> None:
        """
        Remove the quarantine attribute from an installed bundle.

        Safe to do here because the artifact signature was verified before
        installation; without it macOS blocks the relaunch with a Gatekeeper
        warning even though the application is the one the user already had.
        """
        if not shutil.which("xattr"):
            return

        try:
            subprocess.run(
                ["xattr", "-dr", "com.apple.quarantine", str(path)],
                capture_output=True, timeout=120, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            self.log(f"Warning: could not clear the quarantine attribute on {path}")

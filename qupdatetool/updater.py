"""
The update flow itself.

This module owns the sequence and nothing else: no argument parsing, no
window management, no printing. Progress and status are reported through
callbacks, which is what lets the same code drive a silent CLI run and a Qt
progress window without duplicating the logic.

The order below is deliberate and each step guards the next:

    check      ask the backend what the newest release is, compare versions
    select     pick the artifact matching this platform and architecture
    download   stream it to a temporary location
    verify     checksum and OpenPGP signature; abort and delete on failure
    stop       terminate the parent application, wait for file locks to clear
    install    hand the artifact to the platform installer
    relaunch   start the parent application again

When patches are enabled and the releases between the installed version and
the latest publish them, download and verify fetch every patch in the chain
instead, and install applies them one at a time, each checked against the
hashes it carries before the next begins. A patch that cannot be applied
leaves the last good version in place and, by default, falls back to the full
installer. See patch.py.

Verification sits before anything destructive happens. The application is not
stopped, and nothing is written to the install directory, until the
downloaded bytes have been proven to come from the holder of the pinned
signing key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import notes as notes_module
from . import patch as patch_module
from . import platforms
from . import version as version_module
from .backends import get_backend
from .download import Downloader, default_download_dir
from .errors import CancelledError, ConfigError, NoReleaseError, UpdaterError
from .install import get_installer
from .logging_utils import get_logger
from .verify import Verifier

logger = get_logger("updater")


@dataclass
class UpdateCheck:
    """The result of asking whether an update exists."""

    available: bool
    current_version: str = ""
    latest_version: str = ""
    release = None
    asset = None
    asset_kind: str = ""
    notes: notes_module.ReleaseNotes = None
    platform_description: str = ""
    reason: str = ""
    patch_plan = None

    def to_dict(self) -> dict:
        """Render as JSON-friendly data for a parent application to parse."""
        plan = self.patch_plan
        return {
            "update_available": self.available,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "tag": self.release.tag if self.release else "",
            "published_at": self.release.published_at if self.release else "",
            "url": self.release.html_url if self.release else "",
            "asset": self.asset.name if self.asset else "",
            "asset_kind": self.asset_kind,
            "asset_size": self.asset.size if self.asset else 0,
            "platform": self.platform_description,
            "notes_title": self.notes.title if self.notes else "",
            "notes": self.notes.body if self.notes else "",
            "notes_source": self.notes.source if self.notes else "",
            "reason": self.reason,
            "update_method": "patch" if plan else "full",
            "download_size": plan.size if plan else (self.asset.size if self.asset else 0),
            "patches": [step.to_dict() for step in plan.steps] if plan else [],
        }


@dataclass
class UpdateOutcome:
    """The result of applying an update."""

    success: bool
    installed_version: str = ""
    method: str = ""
    verification: str = ""
    relaunched: bool = False
    requires_restart: bool = False
    messages: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "installed_version": self.installed_version,
            "method": self.method,
            "verification": self.verification,
            "relaunched": self.relaunched,
            "requires_restart": self.requires_restart,
            "messages": self.messages,
        }


class Updater:
    """Runs the update sequence for one configuration."""

    def __init__(self, config, status_callback=None, progress_callback=None):
        self.config = config
        self.status_callback = status_callback or (lambda message: None)
        self.progress_callback = progress_callback
        self.cancelled = False

        self.host = platforms.detect(config.get("app.executable", ""))
        self.backend = get_backend(config)
        self.downloader = Downloader(
            session=self.backend.session,
            headers=self.backend.download_headers(),
            timeout=int(config.get("source.timeout", 30)),
            verify_tls=bool(config.get("source.verify_tls", True)),
        )
        self.verifier = Verifier(config, self.backend, self.downloader)

    # --- reporting ---

    def status(self, message: str) -> None:
        """Report a step to the caller and the log at the same time."""
        logger.info(message)
        self.status_callback(message)

    def detail(self, message: str) -> None:
        """Report a lower-level detail; logged, but not necessarily displayed."""
        logger.debug(message)

    def cancel(self) -> None:
        """Request cancellation. Takes effect at the next checkpoint."""
        self.cancelled = True
        self.downloader.cancel()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise CancelledError("Update cancelled")

    # --- check ---

    def check(self, fetch_notes: bool = True) -> UpdateCheck:
        """
        Ask whether a newer release exists, without downloading anything.

        This is what the preflight check on application startup calls, and
        what `--check-only` exposes, so it must stay cheap: one or two API
        calls and no writes to disk.
        """
        current = str(self.config.get("app.current_version", "") or "")

        self.status(f"Checking {self.backend.name} for updates")
        self.detail(f"Platform detected as {self.host.describe()}")

        release = self.backend.latest_release()

        if release is None:
            raise NoReleaseError(
                "No matching release was found",
                f"channel={self.config.get('source.channel')} on {self.backend.name}",
            )

        latest = version_module.strip_platform_suffix(release.tag)

        if not current:
            # With no current version to compare against, treat the newest
            # release as available rather than guessing; the caller decides.
            available = True
            reason = "No current version was supplied, so the latest release is offered"
        else:
            available = version_module.is_newer(release.tag, current)
            reason = (
                f"{latest} is newer than {current}" if available
                else f"{current} is already up to date (latest is {latest})"
            )

        self.status(reason)

        check = UpdateCheck(
            available=available,
            current_version=current,
            latest_version=latest,
            platform_description=self.host.describe(),
            reason=reason,
        )
        check.release = release

        if available:
            asset, kind = self.select_asset(release)
            check.asset = asset
            check.asset_kind = kind
            check.patch_plan = self.plan_patches(check)

            if fetch_notes:
                check.notes = notes_module.fetch(
                    self.config, self.backend, release, log=self.detail
                )

        return check

    # --- asset selection ---

    def select_asset(self, release):
        """
        Choose the artifact this machine should install.

        Selection runs in three passes. Explicit configuration wins: a
        publisher who sets an asset pattern gets exactly what they asked for.
        Otherwise assets are filtered to those plausibly for this OS and
        architecture, then ranked by the platform preference order, so a
        Debian host takes the .deb and a Fedora host takes the .rpm from the
        very same release.
        """
        candidates = [asset for asset in release.assets if asset.name]

        if not candidates:
            raise NoReleaseError(
                f"Release {release.tag} has no downloadable assets"
            )

        # Signature and checksum files, and patches, are never install candidates.
        auxiliary_suffixes = tuple(
            self.config.get("security.signature_suffixes", [".sig", ".asc"])
        ) + (".sha256", ".sha512", ".md5", ".txt", patch_module.PATCH_SUFFIX)
        candidates = [
            asset for asset in candidates
            if not asset.name.lower().endswith(auxiliary_suffixes)
        ]

        exclude = self.config.get("assets.exclude_pattern", "")
        if exclude:
            import re

            matcher = re.compile(exclude)
            candidates = [
                asset for asset in candidates if not matcher.search(asset.name)
            ]

        pattern = self.config.asset_pattern(self.host.os_family)
        if pattern:
            import re

            matcher = re.compile(pattern)
            matched = [asset for asset in candidates if matcher.search(asset.name)]

            if not matched:
                raise NoReleaseError(
                    f"No asset in {release.tag} matched the configured pattern",
                    f"pattern={pattern}; available: "
                    + ", ".join(asset.name for asset in candidates[:10]),
                )

            asset = matched[0]
            return asset, platforms.classify_asset(asset.name)

        # Filter to assets that do not clearly belong to another platform.
        plausible = [
            asset for asset in candidates
            if platforms.asset_matches_os(asset.name, self.host.os_family)
        ]

        if self.config.get("assets.match_arch", True):
            arch_matched = [
                asset for asset in plausible
                if platforms.asset_matches_arch(asset.name, self.host.arch)
            ]
            # Only apply the architecture filter if it leaves something; a
            # release with no arch in its filenames must not filter to empty.
            if arch_matched:
                plausible = arch_matched

        if not plausible:
            raise NoReleaseError(
                f"No asset in {release.tag} is suitable for {self.host.describe()}",
                "available: " + ", ".join(asset.name for asset in candidates[:10]),
            )

        preference = self.config.asset_preference(
            self.host.os_family, self.host.preference
        )

        ranked = []
        for asset in plausible:
            kind = platforms.classify_asset(asset.name)
            if kind in preference:
                ranked.append((preference.index(kind), asset, kind))

        if ranked:
            ranked.sort(key=lambda entry: entry[0])
            _, asset, kind = ranked[0]
            self.detail(f"Selected {asset.name} (kind={kind}) for {self.host.describe()}")
            return asset, kind

        # Nothing matched a known kind; fall back to the single plausible
        # asset if there is exactly one, rather than guessing between several.
        if len(plausible) == 1:
            asset = plausible[0]
            return asset, platforms.classify_asset(asset.name)

        raise NoReleaseError(
            f"Could not decide which asset to install for {self.host.describe()}",
            "candidates: " + ", ".join(asset.name for asset in plausible[:10])
            + ". Set assets.pattern to choose explicitly.",
        )

    # --- patches ---

    def plan_patches(self, check: UpdateCheck):
        """
        Work out whether this update can be applied as patches.

        Returns a PatchPlan, or None for a full install. Never raises:
        planning reads release metadata only, and a problem with it must not
        turn a working update check into a failed one.
        """
        if not self.config.get("patches.enabled", False):
            return None
        if not check.current_version:
            self.detail("Patches skipped: no current version to patch from")
            return None
        if (self.config.get("install.mode") or "silent").lower() == "interactive":
            self.detail("Patches skipped: interactive installs run the full installer")
            return None
        if self.host.running_as_appimage:
            self.detail("Patches skipped: an AppImage is replaced whole")
            return None

        try:
            plan = patch_module.plan_chain(
                self.backend.candidate_releases(),
                check.current_version,
                check.release,
                self.host.os_family,
                self.host.arch,
                pattern=self.config.get("patches.pattern", ""),
                max_steps=int(self.config.get("patches.max_chain", 10)),
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning("Could not plan a patch update: %s", exc)
            return None

        if plan is None:
            self.detail(
                f"No patch route from {check.current_version} to {check.release.tag}; "
                "the full installer will be used"
            )
            return None

        full_size = check.asset.size if check.asset else 0
        ratio = float(self.config.get("patches.max_size_ratio", 0.9))
        if full_size and plan.size >= full_size * ratio:
            self.detail(
                f"Patches total {plan.size} bytes against {full_size} for the full "
                "installer; the full installer will be used"
            )
            return None

        self.detail(
            f"Patch route: {check.current_version} -> "
            + " -> ".join(step.to_version for step in plan.steps)
            + f" ({plan.size} bytes)"
        )
        return plan

    def patch_target(self) -> Path | None:
        """The folder patches apply to: the same one an archive install unpacks into."""
        target = self.config.get("install.target_dir", "") or self.config.install_dir
        return Path(target) if target else None

    def apply_patches(self, check: UpdateCheck) -> UpdateOutcome | None:
        """
        Download, verify and apply the planned patches in order.

        Every patch is downloaded and its signature verified before anything
        is stopped or changed, so a network or signature failure costs
        nothing. Then each patch is applied and the install verified before
        the next one starts. Returns None when the update should continue as
        a full install instead.
        """
        from .elevate import can_write

        plan = check.patch_plan
        count = len(plan.steps)
        fallback = bool(self.config.get("patches.fallback_to_full", True))
        target = self.patch_target()
        downloaded = []
        report = None

        try:
            if target is None:
                raise patch_module.PatchError(
                    "Cannot patch: the install folder is unknown",
                    "Pass --executable or set app.install_dir",
                )
            if not target.is_dir() or not can_write(target):
                raise patch_module.PatchError(f"Cannot patch: {target} is not writable")

            for index, step in enumerate(plan.steps, 1):
                self.raise_if_cancelled()
                downloaded.append(self.download_asset(step.asset, f"patch {index} of {count}"))
                self.raise_if_cancelled()
                self.status(f"Verifying patch {index} of {count}")
                report = self.verifier.verify_release_artifact(
                    step.release, downloaded[-1], step.asset, log=self.detail
                )
        except CancelledError:
            self.cleanup_all(downloaded)
            raise
        except UpdaterError as exc:
            self.cleanup_all(downloaded)
            self.patch_fallback(exc, fallback)
            return None

        if self.config.get("meta.dry_run", False):
            return UpdateOutcome(
                success=True,
                installed_version=check.latest_version,
                method=f"dry run: {count} patch(es) downloaded and verified, nothing installed",
                verification=report.describe(),
                messages=[f"Patch left at {path}" for path in downloaded],
            )

        from . import process as process_module

        messages = []
        stopped = process_module.stop_parent(self.config, log=self.detail)
        if stopped:
            self.status(f"Stopped {', '.join(stopped)}")
            messages.append(f"Stopped {', '.join(stopped)}")

        current = check.current_version
        try:
            for index, (step, path) in enumerate(zip(plan.steps, downloaded), 1):
                self.status(f"Applying patch {index} of {count} ({step.to_version})")
                result = patch_module.apply_patch(
                    path,
                    target,
                    expected_from=current,
                    expected_to=step.to_version,
                    verify=self.config.get("patches.verify", patch_module.VERIFY_FULL),
                    log=self.detail,
                )
                current = step.to_version
                self.status(f"Verified {current}: {result.verified} files match")
                messages.append(
                    f"Patched to {current}: {result.written} files written, "
                    f"{result.deleted} removed"
                )
                self.record_installed_version(current)
        except patch_module.PatchError as exc:
            if current != check.current_version:
                logger.warning("Patching stopped at %s", current)
            if not fallback:
                # Whatever version the chain reached is complete and
                # verified, so the application can run while the error is
                # reported.
                self.relaunch(messages)
            self.patch_fallback(exc, fallback)
            return None
        finally:
            self.cleanup_all(downloaded)

        relaunched = self.relaunch(messages)

        return UpdateOutcome(
            success=True,
            installed_version=check.latest_version,
            method=f"patched in place ({count} patch{'es' if count != 1 else ''}, "
                   f"{plan.size / (1024 * 1024):.1f} MB)",
            verification=report.describe(),
            relaunched=relaunched,
            messages=messages,
        )

    def patch_fallback(self, exc: UpdaterError, fallback: bool) -> None:
        """Report a fall back to the full installer, or re-raise when fallback is off."""
        if not fallback:
            raise exc
        logger.warning("Patch update failed, using the full installer: %s", exc)
        self.status("Patching was not possible; installing the full update instead")

    def record_installed_version(self, version: str) -> None:
        """
        Keep the Windows uninstall entry's version in step with a patch.

        A full installer rewrites DisplayVersion itself; a patch does not run
        the installer, so without this Apps & Features would keep showing the
        version the patches started from.
        """
        key = self.config.get("patches.windows_uninstall_key", "")
        if not key or self.host.os_family != "windows":
            return
        try:
            import winreg
        except ImportError:
            return

        display = version_module.strip_platform_suffix(version)
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_SET_VALUE) as handle:
                    winreg.SetValueEx(handle, "DisplayVersion", 0, winreg.REG_SZ, display)
                self.detail(f"Uninstall entry now shows {display}")
                return
            except OSError:
                continue
        self.detail(f"Uninstall entry {key} not found; its version was not updated")

    # --- apply ---

    def apply(self, check: UpdateCheck | None = None) -> UpdateOutcome:
        """
        Download, verify, and install the update, then relaunch the parent.

        Accepts an UpdateCheck to avoid a second round of API calls when the
        caller has already run check().
        """
        if check is None:
            check = self.check()

        if not check.available:
            return UpdateOutcome(
                success=True,
                installed_version=check.current_version,
                method="no update required",
                messages=[check.reason],
            )

        if check.asset is None:
            raise NoReleaseError("No installable asset was selected")

        self.raise_if_cancelled()

        if check.patch_plan:
            outcome = self.apply_patches(check)
            if outcome is not None:
                return outcome

        artifact = self.download(check)

        self.raise_if_cancelled()

        report = self.verify(check, artifact)

        self.raise_if_cancelled()

        if self.config.get("meta.dry_run", False):
            return UpdateOutcome(
                success=True,
                installed_version=check.latest_version,
                method="dry run: downloaded and verified, nothing installed",
                verification=report.describe(),
                messages=[f"Artifact left at {artifact}"],
            )

        outcome = self.install(check, artifact, report)

        self.cleanup(artifact)

        return outcome

    def download(self, check: UpdateCheck) -> Path:
        """Download the selected artifact to the staging directory."""
        return self.download_asset(check.asset)

    def download_asset(self, asset, label: str = "") -> Path:
        """Download one release asset to the staging directory."""
        directory = self.config.get("install.download_dir", "")
        target_dir = (
            Path(directory).expanduser()
            if directory
            else default_download_dir(self.config.app_name)
        )
        target_dir.mkdir(parents=True, exist_ok=True)

        target = target_dir / asset.name

        size_text = f" ({asset.size / (1024 * 1024):.1f} MB)" if asset.size else ""
        prefix = f"Downloading {label}: " if label else "Downloading "
        self.status(f"{prefix}{asset.name}{size_text}")

        return self.downloader.fetch_file(
            asset.url,
            target,
            expected_size=asset.size,
            progress_callback=self.progress_callback,
        )

    def verify(self, check: UpdateCheck, artifact: Path):
        """Verify the downloaded artifact, deleting it on any failure."""
        self.status("Verifying signature")

        report = self.verifier.verify_release_artifact(
            check.release, artifact, check.asset, log=self.detail
        )

        self.status(f"Verified: {report.describe()}")
        return report

    def install(self, check: UpdateCheck, artifact: Path, report) -> UpdateOutcome:
        """Stop the parent, install the artifact, and relaunch."""
        from . import process as process_module

        messages = []

        stopped = process_module.stop_parent(self.config, log=self.detail)
        if stopped:
            self.status(f"Stopped {', '.join(stopped)}")
            messages.append(f"Stopped {', '.join(stopped)}")

        mode = (self.config.get("install.mode") or "silent").lower()
        self.status(
            "Running the installer"
            if mode == "interactive"
            else f"Installing {check.latest_version}"
        )

        installer = get_installer(self.config, self.host, log=self.detail)
        result = installer.install(artifact, check.asset_kind)

        self.status(f"Installed via {result.method}")
        messages.extend(result.notes)

        relaunched = self.relaunch(messages) if result.success else False

        return UpdateOutcome(
            success=result.success,
            installed_version=check.latest_version,
            method=result.method,
            verification=report.describe(),
            relaunched=relaunched,
            requires_restart=result.requires_restart,
            messages=messages,
        )

    def relaunch(self, messages: list) -> bool:
        """Start the parent application again, noting a failure in `messages`."""
        from . import process as process_module

        try:
            relaunched = process_module.relaunch(self.config, log=self.detail)
        except UpdaterError as exc:
            # The update itself succeeded; failing to relaunch is worth
            # reporting but must not be reported as a failed update.
            logger.warning("Could not relaunch: %s", exc)
            messages.append(f"Could not relaunch automatically: {exc}")
            return False
        if relaunched:
            self.status(f"Restarted {self.config.app_name}")
        return relaunched

    def cleanup_all(self, artifacts: list) -> None:
        for artifact in artifacts:
            self.cleanup(artifact)

    def cleanup(self, artifact: Path) -> None:
        """Remove the downloaded artifact unless the config says to keep it."""
        if self.config.get("install.keep_download", False):
            self.detail(f"Keeping downloaded artifact at {artifact}")
            return

        try:
            Path(artifact).unlink(missing_ok=True)
            self.detail(f"Removed {artifact}")
        except OSError as exc:
            self.detail(f"Could not remove {artifact}: {exc}")


def build_updater(config, status_callback=None, progress_callback=None) -> Updater:
    """Validate configuration and construct an Updater."""
    config.validate()

    if not config.get("app.name"):
        raise ConfigError(
            "No application name configured",
            "Pass --app-name or set app.name in config.yaml",
        )

    return Updater(
        config,
        status_callback=status_callback,
        progress_callback=progress_callback,
    )

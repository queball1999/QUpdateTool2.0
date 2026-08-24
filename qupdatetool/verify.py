"""
Release artifact verification: checksums first, then OpenPGP signatures.

The verification chain the updater runs, in order:

  1. Download SHA256SUMS.txt and its detached signature.
  2. Verify that signature against the pinned public key. This is the step
     that actually establishes trust; everything after it is a consequence.
  3. Find the downloaded artifact in the now-trusted checksum file and
     compare hashes.

Verifying the checksum manifest once and then checking artifacts against it
means a release can ship dozens of files with a single signature, which is a
common release-pipeline layout. When no checksum manifest exists, the updater
falls back to verifying a per-artifact detached signature directly, which is
the other common convention.

A failure anywhere in the chain deletes the download and aborts. There is no
"warn and continue" path, because an update mechanism that installs
unverified code on a warning is an update mechanism with no verification.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import openpgp
from .errors import VerificationError
from .integrity import file_digest

# "<hex>  <path>" or "<hex> *<path>", the shasum/sha256sum output formats.
CHECKSUM_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+[*]?(.+)$")


@dataclass
class VerificationReport:
    """What was checked, and how it turned out."""

    checksum_verified: bool = False
    signature_verified: bool = False
    signer: str = ""
    fingerprint: str = ""
    method: str = ""            # "pgp" | "gpg-binary" | "none"
    digest: str = ""
    notes: list = None

    def __post_init__(self):
        if self.notes is None:
            self.notes = []

    def describe(self) -> str:
        parts = []
        if self.checksum_verified:
            parts.append(f"SHA-256 matched ({self.digest[:16]}...)")
        if self.signature_verified:
            parts.append(f"signed by {self.signer}")
        return "; ".join(parts) if parts else "not verified"


def parse_checksums(text: str) -> dict:
    """
    Parse a SHA256SUMS file into {filename: digest}.

    Paths are reduced to their basename because release pipelines commonly
    generate the manifest with directory prefixes ("./windows-build/App.exe")
    that do not survive into the published asset names.
    """
    digests = {}

    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        match = CHECKSUM_LINE.match(line)
        if not match:
            continue

        digest, path = match.groups()
        name = path.replace("\\", "/").rsplit("/", 1)[-1].strip()
        digests[name] = digest.lower()

    return digests


def verify_checksum(path: Path, expected: str) -> str:
    """
    Compare a file against an expected SHA-256, returning the actual digest.

    Raises VerificationError on mismatch; the caller is responsible for
    deleting the artifact.
    """
    actual = file_digest(path, "sha256").lower()
    expected_clean = (expected or "").strip().lower()

    if actual != expected_clean:
        raise VerificationError(
            f"Checksum mismatch for {path.name}",
            f"expected {expected_clean}, got {actual}",
        )

    return actual


def verify_signature(message: bytes, signature_data: bytes, key_data: bytes,
                     allowed_fingerprints: tuple = ()) -> tuple:
    """
    Verify a detached OpenPGP signature, returning (signer, fingerprint).

    When `allowed_fingerprints` is set, the signing key must be one of them.
    That is a second lock beyond the pinned key block: it stops a key block
    that has picked up an extra subkey from silently widening what counts as
    a trusted signature.
    """
    result = openpgp.verify_detached(message, signature_data, key_data)

    fingerprint = result.key.fingerprint_hex
    signer = result.key.user_ids[0] if result.key.user_ids else fingerprint

    if allowed_fingerprints:
        normalised = {
            value.replace(" ", "").upper() for value in allowed_fingerprints if value
        }
        # Accept a full fingerprint or a long key ID suffix.
        if not any(
            fingerprint == candidate or fingerprint.endswith(candidate)
            for candidate in normalised
        ):
            raise VerificationError(
                "Release was signed by an untrusted key",
                f"{fingerprint} is not in the allowed fingerprint list",
            )

    return signer, fingerprint


def verify_with_gpg_binary(artifact: Path, signature: Path, key_data: bytes) -> tuple:
    """
    Fall back to the gpg binary when the built-in verifier cannot cope.

    This exists for key or signature types outside the supported set, such as
    a DSA key on an older project. The pinned key is imported into a scratch
    keyring so the verification cannot be satisfied by whatever happens to be
    in the user keyring already.
    """
    gpg = shutil.which("gpg") or shutil.which("gpg2")
    if not gpg:
        raise VerificationError("gpg is not installed and the built-in verifier failed")

    with tempfile.TemporaryDirectory(prefix="qupdate-gpg-") as scratch:
        home = Path(scratch)
        key_file = home / "trusted.asc"
        key_file.write_bytes(key_data)

        base = [gpg, "--homedir", str(home), "--batch", "--quiet", "--no-tty"]

        try:
            imported = subprocess.run(
                base + ["--import", str(key_file)],
                capture_output=True, text=True, timeout=60, check=False,
            )
            if imported.returncode != 0:
                raise VerificationError(
                    "Could not import the pinned key into gpg",
                    (imported.stderr or "").strip()[:300],
                )

            result = subprocess.run(
                base + ["--status-fd", "1", "--verify", str(signature), str(artifact)],
                capture_output=True, text=True, timeout=120, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise VerificationError("gpg verification could not run", str(exc)) from exc

    status = result.stdout or ""

    if "GOODSIG" not in status and result.returncode != 0:
        raise VerificationError(
            "gpg rejected the release signature",
            (result.stderr or status).strip()[:300],
        )

    signer = ""
    fingerprint = ""
    for line in status.splitlines():
        if line.startswith("[GNUPG:] GOODSIG"):
            parts = line.split(None, 3)
            if len(parts) >= 4:
                signer = parts[3]
        elif line.startswith("[GNUPG:] VALIDSIG"):
            parts = line.split()
            if len(parts) >= 3:
                fingerprint = parts[2]

    return signer or "unknown signer", fingerprint


class Verifier:
    """Runs the verification chain for one release."""

    def __init__(self, config, backend, downloader):
        self.config = config
        self.backend = backend
        self.downloader = downloader

        self.require_signature = bool(config.get("security.require_signature", True))
        self.require_checksum = bool(config.get("security.require_checksum", True))
        self.signature_suffixes = tuple(
            config.get("security.signature_suffixes", [".sig", ".asc"])
        )
        self.checksum_filename = config.get("security.checksum_file", "SHA256SUMS.txt")
        self.allowed_fingerprints = tuple(
            config.get("security.allowed_fingerprints", []) or ()
        )
        self.allow_gpg_fallback = bool(config.get("security.allow_gpg_fallback", True))
        self.key_data = config.public_key_data

    def verify_release_artifact(self, release, artifact_path: Path, asset,
                                log=None) -> VerificationReport:
        """
        Verify one downloaded artifact against the release metadata.

        Returns a VerificationReport on success. On any failure the artifact
        is deleted before the error propagates, so a rejected download can
        never be picked up and run by something else later.
        """
        report = VerificationReport()

        try:
            self.run_chain(release, artifact_path, asset, report, log)
        except VerificationError:
            # A file that failed verification must not survive on disk.
            try:
                artifact_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        return report

    def run_chain(self, release, artifact_path: Path, asset, report, log=None) -> None:
        """Execute checksum and signature verification in the documented order."""
        def note(message):
            report.notes.append(message)
            if log:
                log(message)

        if not self.key_data and self.require_signature:
            raise VerificationError(
                "Signature verification is required but no trusted key is configured"
            )

        checksums, checksum_asset = self.load_checksums(release, report, note)

        if checksums:
            expected = checksums.get(asset.name)
            if expected is None:
                if self.require_checksum:
                    raise VerificationError(
                        f"{asset.name} is not listed in {self.checksum_filename}",
                        "The release manifest does not cover this artifact",
                    )
                note(f"{asset.name} not listed in the checksum manifest")
            else:
                report.digest = verify_checksum(artifact_path, expected)
                report.checksum_verified = True
                note(f"SHA-256 verified against {self.checksum_filename}")

        # If the checksum manifest was signed, trust already flows to the
        # artifact through it, and a per-artifact signature is optional.
        if report.signature_verified and report.checksum_verified:
            return

        self.verify_artifact_signature(release, artifact_path, asset, report, note)

        if self.require_signature and not report.signature_verified:
            raise VerificationError(
                f"No valid signature found for {asset.name}",
                "Publish a detached signature, or run with --no-require-signature "
                "to accept unsigned releases",
            )

        if self.require_checksum and not report.checksum_verified:
            # A verified per-artifact signature is a stronger guarantee than a
            # checksum, so it satisfies the integrity requirement on its own.
            if not report.signature_verified:
                raise VerificationError(
                    f"No checksum available for {asset.name}"
                )
            report.digest = file_digest(artifact_path, "sha256")

    def load_checksums(self, release, report, note) -> tuple:
        """
        Download and verify the release checksum manifest.

        Returns ({filename: digest}, asset) or ({}, None) when the release
        publishes no manifest.
        """
        checksum_asset = release.find_asset(self.checksum_filename)
        if checksum_asset is None:
            note(f"No {self.checksum_filename} published with this release")
            return {}, None

        try:
            manifest = self.downloader.fetch_bytes(checksum_asset.url)
        except Exception as exc:
            raise VerificationError(
                f"Could not download {self.checksum_filename}", str(exc)
            ) from exc

        signature_asset = release.find_signature(
            checksum_asset.name, self.signature_suffixes
        )

        if signature_asset is None:
            if self.require_signature:
                raise VerificationError(
                    f"{self.checksum_filename} is not signed",
                    "The checksum manifest carries no detached signature, so its "
                    "contents cannot be trusted",
                )
            note(f"{self.checksum_filename} is unsigned")
        else:
            signature_data = self.downloader.fetch_bytes(signature_asset.url)
            signer, fingerprint = self.verify_or_fallback(
                manifest, signature_data, artifact=None, signature_path=None
            )
            report.signature_verified = True
            report.signer = signer
            report.fingerprint = fingerprint
            report.method = "pgp"
            note(f"{self.checksum_filename} signature verified ({signer})")

        return parse_checksums(manifest.decode("utf-8", errors="replace")), checksum_asset

    def verify_artifact_signature(self, release, artifact_path: Path, asset,
                                  report, note) -> None:
        """Verify a detached signature published alongside the artifact itself."""
        signature_asset = release.find_signature(asset.name, self.signature_suffixes)

        if signature_asset is None:
            note(f"No detached signature published for {asset.name}")
            return

        signature_data = self.downloader.fetch_bytes(signature_asset.url)

        signature_path = artifact_path.with_name(signature_asset.name)
        signature_path.write_bytes(signature_data)

        try:
            signer, fingerprint = self.verify_or_fallback(
                artifact_path.read_bytes(),
                signature_data,
                artifact=artifact_path,
                signature_path=signature_path,
            )
        finally:
            signature_path.unlink(missing_ok=True)

        report.signature_verified = True
        report.signer = signer
        report.fingerprint = fingerprint
        report.method = report.method or "pgp"
        note(f"{asset.name} signature verified ({signer})")

    def verify_or_fallback(self, message: bytes, signature_data: bytes,
                           artifact: Path | None, signature_path: Path | None) -> tuple:
        """
        Verify with the built-in verifier, falling back to gpg only if allowed.

        The fallback is for unsupported key algorithms, not for verification
        failures: a signature that is genuinely wrong fails both paths, and a
        mismatch is never downgraded into a pass.
        """
        try:
            return verify_signature(
                message, signature_data, self.key_data, self.allowed_fingerprints
            )
        except VerificationError as exc:
            unsupported = "unsupported" in str(exc).lower()

            if not (unsupported and self.allow_gpg_fallback and artifact and signature_path):
                raise

            signer, fingerprint = verify_with_gpg_binary(
                artifact, signature_path, self.key_data
            )

            if self.allowed_fingerprints:
                normalised = {
                    value.replace(" ", "").upper()
                    for value in self.allowed_fingerprints if value
                }
                if not any(
                    fingerprint.upper().endswith(candidate) for candidate in normalised
                ):
                    raise VerificationError(
                        "Release was signed by an untrusted key",
                        f"{fingerprint} is not in the allowed fingerprint list",
                    ) from exc

            return signer, fingerprint

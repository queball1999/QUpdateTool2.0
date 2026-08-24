"""
Integrity checking for the updater binary itself.

## The honest version of the threat model

An updater is a high-value target: it downloads code and installs it, often
with elevated privileges. So "what if someone tampers with updater.exe?" is
the right question to ask. The uncomfortable answer is that **a program
cannot meaningfully verify its own integrity**. Any check the binary performs
on itself can be removed by whoever modified the binary. Embedding a hash of
updater.exe inside updater.exe and comparing at startup stops nobody who is
actually trying; they recompute the constant, or delete the comparison.

What does work is verification by a *different* party:

1. **The parent application checks the updater before launching it.**
   The parent application knows the SHA-256 of the updater it shipped with,
   stamped into its build_info at release time. Before spawning updater.exe it
   hashes the file and refuses to run a mismatch. This is not circular: an
   attacker who can only write to updater.exe is caught, and one who can also
   rewrite the application's own binary did not need the updater in the first
   place.
   `verify_file_hash()` and `expected_updater_hash()` support this.

2. **The operating system checks a code signature.** Authenticode on Windows
   and notarization on macOS are enforced outside the binary, and a modified
   file fails them. `authenticode_status()` surfaces that state so the parent
   app can log it; the build script has a hook for signing.

3. **File permissions.** A binary in Program Files needs admin rights to
   replace. `check_install_permissions()` warns when the updater is sitting
   somewhere a normal user can overwrite, which is the condition that makes
   the whole attack cheap.

The self-hash reported by `self_digest()` is therefore documented as
tamper-*evident*, not tamper-*proof*: it is for support diagnostics, for
letting a parent app pin a known-good build, and for catching accidental
corruption. It is deliberately not used as a runtime gate that pretends to
provide a security guarantee it cannot deliver.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
import subprocess
import sys
from pathlib import Path

from .brand import brand_info

CHUNK_SIZE = 1024 * 1024


def file_digest(path: str | os.PathLike, algorithm: str = "sha256") -> str:
    """Return the hex digest of a file, read in chunks so large files are fine."""
    digest = hashlib.new(algorithm)

    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)

    return digest.hexdigest()


def verify_file_hash(path: str | os.PathLike, expected: str, algorithm: str = "sha256") -> bool:
    """
    Compare a file against an expected hex digest in constant time.

    This is the function a parent application calls on updater.exe before
    launching it. The comparison uses compare_digest so the check does not
    leak the expected value through timing, and a missing file returns False
    rather than raising, because "not there" and "wrong" both mean do not run.
    """
    if not expected:
        return False

    file_path = Path(path)
    if not file_path.is_file():
        return False

    try:
        actual = file_digest(file_path, algorithm)
    except OSError:
        return False

    return hmac.compare_digest(actual.lower(), expected.strip().lower())


def self_path() -> Path:
    """Return the path of the running updater binary or entry script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(sys.argv[0]).resolve()


def self_digest() -> str:
    """
    Hash the running updater binary.

    Tamper-evident only: useful for support output and for a parent app to
    pin, but not a security boundary, since a modified binary controls this
    code path too. See the module docstring.
    """
    try:
        return file_digest(self_path())
    except OSError:
        return ""


def expected_updater_hash(app_install_dir: str | os.PathLike, name: str = "updater") -> str:
    """
    Read the expected updater hash a parent application recorded at build time.

    Looks for a sidecar written by the build pipeline next to the updater:
    "<name>.sha256", in the standard "<hex>  <filename>" shasum format. A
    parent app that stamps the hash into its own build_info should prefer that
    over this file, since a sidecar next to the binary is only as trustworthy
    as the directory holding it.
    """
    directory = Path(app_install_dir)

    for candidate in (directory / f"{name}.sha256", directory / f"{name}.exe.sha256"):
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        return text.split()[0]

    return ""


def check_install_permissions(path: str | os.PathLike) -> tuple:
    """
    Report whether the updater sits somewhere an unprivileged user can rewrite.

    Returns (is_safe, explanation). A writable location is not proof of
    compromise; it is a warning that the cheapest attack on this mechanism is
    available, and that a code signature or a parent-side hash check is
    carrying the actual weight.
    """
    target = Path(path)
    directory = target.parent if target.is_file() else target

    if not directory.exists():
        return False, f"Directory does not exist: {directory}"

    if sys.platform.startswith("win"):
        # Windows ACLs are not readable through os.stat in a useful way, so
        # this reports the standard protected locations rather than guessing.
        protected_roots = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("SystemRoot", r"C:\Windows"),
        ]
        resolved = str(directory.resolve()).lower()
        for root in protected_roots:
            if root and resolved.startswith(str(Path(root).resolve()).lower()):
                return True, f"Installed under a protected location: {directory}"
        return False, (
            f"Installed in a user-writable location: {directory}. "
            "Rely on the parent-side hash check or a code signature."
        )

    try:
        mode = directory.stat().st_mode
    except OSError as exc:
        return False, f"Could not stat {directory}: {exc}"

    # World-writable without the sticky bit is the clearly dangerous case.
    if mode & stat.S_IWOTH and not mode & stat.S_ISVTX:
        return False, f"Directory is world-writable: {directory}"

    if os.access(directory, os.W_OK) and os.geteuid() != 0:
        return False, (
            f"Directory is writable by the current user: {directory}. "
            "Rely on the parent-side hash check or a package-managed install."
        )

    return True, f"Directory is not user-writable: {directory}"


def authenticode_status(path: str | os.PathLike) -> tuple:
    """
    Report the OS-level code signature state of a file.

    Returns (status, detail) where status is one of "valid", "invalid",
    "unsigned", or "unknown". This is the one integrity signal in this module
    that a modified binary cannot forge, because the verdict comes from the
    operating system rather than from code inside the file.
    """
    target = Path(path)
    if not target.is_file():
        return "unknown", f"File not found: {target}"

    if sys.platform.startswith("win"):
        return windows_signature_status(target)
    if sys.platform == "darwin":
        return macos_signature_status(target)

    return "unknown", "Code signature checking is not available on this platform"


def windows_signature_status(target: Path) -> tuple:
    """Query Authenticode status through PowerShell Get-AuthenticodeSignature."""
    command = [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        f"(Get-AuthenticodeSignature -LiteralPath '{target}').Status",
    ]

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "unknown", f"Could not query signature: {exc}"

    status = (result.stdout or "").strip()

    if status == "Valid":
        return "valid", "Authenticode signature is valid"
    if status == "NotSigned":
        return "unsigned", "File is not code signed"
    if status:
        return "invalid", f"Authenticode status: {status}"

    return "unknown", (result.stderr or "").strip() or "No signature information"


def macos_signature_status(target: Path) -> tuple:
    """Query the code signature through codesign."""
    try:
        result = subprocess.run(
            ["codesign", "--verify", "--strict", str(target)],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "unknown", f"Could not query signature: {exc}"

    if result.returncode == 0:
        return "valid", "Code signature is valid"

    message = (result.stderr or "").strip()
    if "not signed" in message.lower():
        return "unsigned", "File is not code signed"

    return "invalid", message or "Code signature verification failed"


def self_check_report() -> dict:
    """
    Assemble everything known about the integrity of this binary.

    Surfaced by `updater --self-check`, so a support request can include the
    build stamp, the file hash, the code signature verdict, and whether the
    install location is writable, without the user running four commands.
    """
    path = self_path()
    info = brand_info()

    signature_status, signature_detail = authenticode_status(path)
    permissions_ok, permissions_detail = check_install_permissions(path)

    return {
        "path": str(path),
        "frozen": bool(getattr(sys, "frozen", False)),
        "sha256": self_digest(),
        "brand": info,
        "signature_status": signature_status,
        "signature_detail": signature_detail,
        "install_location_protected": permissions_ok,
        "install_location_detail": permissions_detail,
    }

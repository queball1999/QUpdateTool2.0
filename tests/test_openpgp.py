"""
OpenPGP verifier tests.

These generate real keys with the real gpg binary and sign real data with it,
rather than testing against fixtures produced by the same code under test.
That matters for a verifier: a self-consistent implementation that disagrees
with GnuPG would pass a round-trip test and fail in production.

Skipped when gpg is not installed.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap

import pytest

from qupdatetool import openpgp
from qupdatetool.errors import VerificationError


def find_gpg() -> str | None:
    """
    Locate a gpg that understands this platform native paths.

    On Windows the gpg bundled with Git for Windows is an MSYS build that
    rejects native paths such as "C:\\Users\\...", so the native GnuPG
    install is preferred when it is present.
    """
    import os
    import sys

    if sys.platform.startswith("win"):
        for candidate in (
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                         "GnuPG", "bin", "gpg.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                         "GnuPG", "bin", "gpg.exe"),
        ):
            if os.path.isfile(candidate):
                return candidate

    found = shutil.which("gpg") or shutil.which("gpg2")

    # An MSYS gpg cannot be handed native Windows paths; skip rather than
    # produce confusing failures that look like verifier bugs.
    if found and sys.platform.startswith("win") and "Git" in found:
        return None

    return found


GPG = find_gpg()

pytestmark = pytest.mark.skipif(GPG is None, reason="a native gpg is not installed")

# A payload shaped like the checksum manifest a release pipeline publishes.
SHA256SUMS = textwrap.dedent(
    """\
    0d1f8a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7  ./MyApp-1.0-windows-installer.exe
    1e2f3a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f607  ./myapp_1.0_amd64.deb
    """
).encode()

KEY_PARAMS = {
    "rsa": "Key-Type: RSA\nKey-Length: 3072\nKey-Usage: sign",
    "ed25519": "Key-Type: eddsa\nKey-Curve: ed25519\nKey-Usage: sign",
    "nistp256": "Key-Type: ecdsa\nKey-Curve: nistp256\nKey-Usage: sign",
}


@pytest.fixture(scope="module")
def keyring(tmp_path_factory):
    """Generate one signing key of each supported algorithm in a scratch keyring."""
    home = tmp_path_factory.mktemp("gnupg")

    for name, params in KEY_PARAMS.items():
        script = home / f"{name}.params"
        script.write_text(
            f"%no-protection\n{params}\n"
            f"Name-Real: Test {name}\n"
            f"Name-Email: {name}@example.invalid\n"
            "Expire-Date: 0\n%commit\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [GPG, "--homedir", str(home), "--batch", "--quiet",
             "--gen-key", str(script)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"Could not generate a {name} key: {result.stderr}")

    return home


def export_key(home, name: str) -> bytes:
    """Export one public key from the scratch keyring."""
    result = subprocess.run(
        [GPG, "--homedir", str(home), "--batch", "--quiet",
         "--armor", "--export", f"{name}@example.invalid"],
        capture_output=True, check=True,
    )
    return result.stdout


def sign(home, name: str, data: bytes, path, armor: bool = False,
         digest: str = "") -> bytes:
    """Produce a detached signature over `data` with the named key."""
    payload = path / "payload.bin"
    payload.write_bytes(data)

    signature = path / ("signature.asc" if armor else "signature.sig")

    command = [
        GPG, "--homedir", str(home), "--batch", "--yes", "--quiet",
        "--local-user", f"{name}@example.invalid",
    ]
    if digest:
        command += ["--digest-algo", digest]
    if armor:
        command.append("--armor")
    command += ["--detach-sign", "--output", str(signature), str(payload)]

    subprocess.run(command, capture_output=True, check=True)
    return signature.read_bytes()


@pytest.mark.parametrize("algorithm", list(KEY_PARAMS))
def test_verifies_binary_signature(keyring, tmp_path, algorithm):
    """A binary detached signature from gpg verifies."""
    key = export_key(keyring, algorithm)
    signature = sign(keyring, algorithm, SHA256SUMS, tmp_path)

    result = openpgp.verify_detached(SHA256SUMS, signature, key)

    assert algorithm in result.key.user_ids[0]
    assert result.key.fingerprint_hex


@pytest.mark.parametrize("algorithm", list(KEY_PARAMS))
def test_verifies_armored_signature(keyring, tmp_path, algorithm):
    """An ASCII-armored .asc signature verifies just like a binary .sig."""
    key = export_key(keyring, algorithm)
    signature = sign(keyring, algorithm, SHA256SUMS, tmp_path, armor=True)

    result = openpgp.verify_detached(SHA256SUMS, signature, key)

    assert result.key.fingerprint_hex


@pytest.mark.parametrize("digest", ["SHA256", "SHA384", "SHA512"])
def test_verifies_each_hash_algorithm(keyring, tmp_path, digest):
    """Every accepted digest algorithm verifies."""
    key = export_key(keyring, "rsa")
    signature = sign(keyring, "rsa", SHA256SUMS, tmp_path, digest=digest)

    result = openpgp.verify_detached(SHA256SUMS, signature, key)

    assert result.hash_name == digest.lower()


def test_verifies_large_binary_payload(keyring, tmp_path):
    """Signing works over a payload larger than one read buffer."""
    import os

    payload = os.urandom(512 * 1024)
    key = export_key(keyring, "ed25519")
    signature = sign(keyring, "ed25519", payload, tmp_path)

    assert openpgp.verify_detached(payload, signature, key)


# --- rejection cases -------------------------------------------------------


@pytest.mark.parametrize("algorithm", list(KEY_PARAMS))
def test_rejects_tampered_payload(keyring, tmp_path, algorithm):
    """A single flipped byte in the payload fails verification."""
    key = export_key(keyring, algorithm)
    signature = sign(keyring, algorithm, SHA256SUMS, tmp_path)

    tampered = SHA256SUMS.replace(b"0d1f8a2b", b"0d1f8a2c")

    with pytest.raises(VerificationError):
        openpgp.verify_detached(tampered, signature, key)


def test_rejects_truncated_payload(keyring, tmp_path):
    """A truncated payload fails verification."""
    key = export_key(keyring, "rsa")
    signature = sign(keyring, "rsa", SHA256SUMS, tmp_path)

    with pytest.raises(VerificationError):
        openpgp.verify_detached(SHA256SUMS[:-20], signature, key)


def test_rejects_signature_from_another_key(keyring, tmp_path):
    """A signature made by a different key is rejected."""
    signature = sign(keyring, "rsa", SHA256SUMS, tmp_path)
    other_key = export_key(keyring, "ed25519")

    with pytest.raises(VerificationError):
        openpgp.verify_detached(SHA256SUMS, signature, other_key)


def test_rejects_corrupted_signature(keyring, tmp_path):
    """Flipping bits inside the signature is rejected."""
    key = export_key(keyring, "rsa")
    signature = bytearray(sign(keyring, "rsa", SHA256SUMS, tmp_path))
    signature[-1] ^= 0xFF

    with pytest.raises(VerificationError):
        openpgp.verify_detached(SHA256SUMS, bytes(signature), key)


def test_rejects_bad_armor_checksum(keyring, tmp_path):
    """A corrupted armor CRC24 is caught before any crypto runs."""
    key = export_key(keyring, "rsa")
    armored = sign(keyring, "rsa", SHA256SUMS, tmp_path, armor=True).decode()

    lines = armored.strip().splitlines()
    for index, line in enumerate(lines):
        if line.startswith("="):
            lines[index] = "=AAAA"

    with pytest.raises(VerificationError, match="checksum"):
        openpgp.verify_detached(SHA256SUMS, "\n".join(lines).encode(), key)


def test_rejects_weak_sha1_signature(keyring, tmp_path):
    """A SHA-1 signature is refused even though gpg will happily make one."""
    key = export_key(keyring, "rsa")

    try:
        signature = sign(keyring, "rsa", SHA256SUMS, tmp_path, digest="SHA1")
    except subprocess.CalledProcessError:
        pytest.skip("This gpg build refuses to sign with SHA-1")

    with pytest.raises(VerificationError, match="weak hash"):
        openpgp.verify_detached(SHA256SUMS, signature, key)


def test_rejects_empty_input():
    """Empty data produces a clear error rather than an obscure crash."""
    with pytest.raises(VerificationError):
        openpgp.verify_detached(b"data", b"", b"")


def test_rejects_non_openpgp_data():
    """Random bytes are rejected as unparseable rather than misread."""
    with pytest.raises(VerificationError):
        openpgp.verify_detached(b"data", b"this is not a signature", b"nor is this")


# --- key parsing -----------------------------------------------------------


def test_parses_primary_key_and_subkeys(keyring):
    """Both the primary key and any signing subkeys are returned."""
    key = export_key(keyring, "rsa")
    keys = openpgp.parse_public_keys(key)

    assert keys
    assert any(not entry.is_subkey for entry in keys)
    assert all(len(entry.fingerprint) in (20, 32) for entry in keys)


def test_fingerprint_matches_gpg(keyring):
    """The computed fingerprint agrees with what gpg reports for the same key."""
    result = subprocess.run(
        [GPG, "--homedir", str(keyring), "--batch", "--with-colons",
         "--fingerprint", "rsa@example.invalid"],
        capture_output=True, text=True, check=True,
    )

    expected = ""
    for line in result.stdout.splitlines():
        if line.startswith("fpr:"):
            expected = line.split(":")[9]
            break

    keys = openpgp.parse_public_keys(export_key(keyring, "rsa"))
    primary = next(entry for entry in keys if not entry.is_subkey)

    assert primary.fingerprint_hex == expected

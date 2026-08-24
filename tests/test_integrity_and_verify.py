"""
Integrity checking and checksum-manifest parsing tests.

verify_file_hash is the function a parent application calls on the updater
binary before launching it, so a bug here silently disables the main defence
against a tampered updater. It gets direct coverage.
"""

from __future__ import annotations

import hashlib

import pytest

from qupdatetool import integrity
from qupdatetool.errors import VerificationError
from qupdatetool.verify import parse_checksums, verify_checksum

# A realistic manifest, including the "./subdir/" prefixes that a CI pipeline
# produces when it runs sha256sum over a downloaded-artifacts tree.
MANIFEST = """\
18c0dfab94e02c28ee7e0e4e4b23c4bb0d2cc5b32f9d0a4d5d6e7f8091a2b3c4  ./windows-build/app-0.0.7-windows-installer.exe
b6d43d4cc129e61f0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f6071  ./windows-build/app-0.0.7-windows-portable.zip
9699715124d131160718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e  ./linux-build/app_0.0.7_amd64.deb
"""


@pytest.fixture
def sample_file(tmp_path):
    """A file with known content and a known digest."""
    path = tmp_path / "updater.exe"
    path.write_bytes(b"pretend this is a compiled updater binary" * 500)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_file_digest_matches_hashlib(sample_file):
    """Chunked hashing agrees with hashing the whole file at once."""
    path, expected = sample_file
    assert integrity.file_digest(path) == expected


def test_verify_file_hash_accepts_the_genuine_file(sample_file):
    path, expected = sample_file
    assert integrity.verify_file_hash(path, expected) is True


def test_verify_file_hash_is_case_insensitive(sample_file):
    """A recorded hash in upper case still matches."""
    path, expected = sample_file
    assert integrity.verify_file_hash(path, expected.upper()) is True


def test_verify_file_hash_rejects_a_tampered_file(sample_file):
    """
    Appending a single byte is caught.

    This is the concrete attack the check exists to stop: an attacker
    replacing the updater binary with one that downloads from their own host.
    """
    path, expected = sample_file
    path.write_bytes(path.read_bytes() + b"x")

    assert integrity.verify_file_hash(path, expected) is False


def test_verify_file_hash_rejects_a_missing_file(tmp_path):
    """A missing file is not trusted; absent and wrong both mean do not run."""
    assert integrity.verify_file_hash(tmp_path / "nope.exe", "a" * 64) is False


def test_verify_file_hash_rejects_an_empty_expected_value(sample_file):
    """No recorded hash means no assertion of trust, so the check fails."""
    path, _ = sample_file
    assert integrity.verify_file_hash(path, "") is False


def test_verify_file_hash_tolerates_surrounding_whitespace(sample_file):
    """A hash read from a sidecar file may carry a trailing newline."""
    path, expected = sample_file
    assert integrity.verify_file_hash(path, f"  {expected}\n") is True


def test_expected_updater_hash_reads_a_sidecar(tmp_path):
    """The .sha256 sidecar written by the build script is readable."""
    digest = "a" * 64
    (tmp_path / "updater.sha256").write_text(f"{digest}  updater\n", encoding="utf-8")

    assert integrity.expected_updater_hash(tmp_path) == digest


def test_expected_updater_hash_returns_empty_when_absent(tmp_path):
    assert integrity.expected_updater_hash(tmp_path) == ""


def test_self_check_report_has_the_expected_shape():
    """The self-check report carries every field the CLI prints."""
    report = integrity.self_check_report()

    for key in (
        "path", "frozen", "sha256", "brand",
        "signature_status", "install_location_protected",
    ):
        assert key in report


# --- checksum manifests ----------------------------------------------------


def test_parse_checksums_strips_directory_prefixes():
    """
    Manifest paths are reduced to basenames.

    Release pipelines generate the manifest over a directory tree, so entries
    carry prefixes that never appear in the published asset names.
    """
    digests = parse_checksums(MANIFEST)

    assert "app-0.0.7-windows-installer.exe" in digests
    assert "app_0.0.7_amd64.deb" in digests
    assert len(digests) == 3


def test_parse_checksums_handles_binary_marker():
    """The "*filename" binary-mode marker from sha256sum is handled."""
    digests = parse_checksums(f"{'a' * 64} *MyApp.exe")

    assert digests["MyApp.exe"] == "a" * 64


def test_parse_checksums_ignores_comments_and_blanks():
    text = f"# a comment\n\n{'b' * 64}  MyApp.exe\n"

    assert parse_checksums(text) == {"MyApp.exe": "b" * 64}


def test_parse_checksums_ignores_malformed_lines():
    """A truncated hash is skipped rather than parsed into a false match."""
    assert parse_checksums("deadbeef  MyApp.exe") == {}


def test_verify_checksum_accepts_a_match(tmp_path):
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"release payload")
    digest = hashlib.sha256(b"release payload").hexdigest()

    assert verify_checksum(path, digest) == digest


def test_verify_checksum_rejects_a_mismatch(tmp_path):
    """A checksum mismatch raises rather than returning a falsy value."""
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"tampered payload")

    with pytest.raises(VerificationError, match="Checksum mismatch"):
        verify_checksum(path, hashlib.sha256(b"release payload").hexdigest())

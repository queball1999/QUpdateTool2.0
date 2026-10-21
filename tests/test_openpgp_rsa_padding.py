"""
RSA signatures whose first byte is zero.

An OpenPGP MPI drops leading zero bytes, so about one RSA signature in 256 is
stored shorter than the key modulus. The gpg-driven tests in test_openpgp.py
only hit that case by chance, which made them fail intermittently in CI. This
builds such a signature on purpose so the case is covered every run.

Needs no gpg: the OpenPGP digest is stubbed so the RSA step is tested alone.
"""

from __future__ import annotations

import hashlib

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric import utils as asym_utils

from qupdatetool import openpgp
from qupdatetool.errors import VerificationError

SHA256_ID = 8


def leading_zero_signature(private_key) -> tuple[bytes, bytes]:
    """Sign digests until one signature starts with 0x00; return (digest, signature)."""
    for counter in range(10_000):
        digest = hashlib.sha256(str(counter).encode()).digest()
        signature = private_key.sign(
            digest, padding.PKCS1v15(), asym_utils.Prehashed(hashes.SHA256())
        )
        if signature[0] == 0:
            return digest, signature
    raise AssertionError("no leading-zero signature found in 10,000 tries")


def make_key_and_signature(private_key, mpi_bytes: bytes):
    numbers = private_key.public_key().public_numbers()
    key = openpgp.PublicKey(
        version=4,
        algorithm=openpgp.ALGO_RSA_ENCRYPT_SIGN,
        created=0,
        fingerprint=b"\x00" * 20,
        key_id=b"\x00" * 8,
        key_material={"n": numbers.n, "e": numbers.e},
    )
    signature = openpgp.Signature(
        version=4,
        sig_type=0,
        pubkey_algorithm=openpgp.ALGO_RSA_ENCRYPT_SIGN,
        hash_algorithm=SHA256_ID,
        hashed_subpackets=b"",
        issuer_key_id=b"\x00" * 8,
        issuer_fingerprint=b"",
        created=0,
        expires=0,
        signature_material={"s": mpi_bytes},
    )
    return key, signature


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_verifies_signature_with_leading_zero(private_key, monkeypatch):
    """A signature stored without its leading zero byte still verifies."""
    digest, full_signature = leading_zero_signature(private_key)
    key, signature = make_key_and_signature(private_key, full_signature.lstrip(b"\x00"))
    monkeypatch.setattr(openpgp, "signature_digest", lambda sig, message: digest)

    openpgp.verify_with_key(key, signature, b"unused")


def test_rejects_wrong_digest_with_leading_zero(private_key, monkeypatch):
    """Padding the signature must not make a mismatched digest pass."""
    digest, full_signature = leading_zero_signature(private_key)
    key, signature = make_key_and_signature(private_key, full_signature.lstrip(b"\x00"))
    wrong_digest = hashlib.sha256(digest).digest()
    monkeypatch.setattr(openpgp, "signature_digest", lambda sig, message: wrong_digest)

    with pytest.raises(VerificationError):
        openpgp.verify_with_key(key, signature, b"unused")

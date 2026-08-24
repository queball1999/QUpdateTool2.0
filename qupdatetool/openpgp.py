"""
A minimal OpenPGP detached-signature verifier built on `cryptography`.

Why this exists: the updater ships as a single binary to machines that will
not have GnuPG installed, and an update mechanism that silently skips
signature checking when `gpg` is missing is worse than no signature checking
at all, because it advertises a guarantee it does not deliver. So the parsing
and verification needed for detached release signatures is implemented here
directly, on top of the primitives in `cryptography`.

Scope is deliberately narrow. This verifies detached signatures over release
artifacts against a known, pinned public key. It does NOT implement the web
of trust, key expiry chains beyond the basic checks, revocation lookup,
encryption, or signing. It is a verifier for one specific job.

Supported: RFC 4880 v4 signatures, RFC 9580 v6 signatures, RSA (PKCS#1 v1.5),
Ed25519 (both the EdDSA-legacy and the modern Ed25519 algorithm IDs), and
ECDSA over NIST P-256/P-384/P-521. DSA and ElGamal are not supported; keys
using them are reported as unsupported rather than silently accepted.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, utils as asym_utils
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

from .errors import VerificationError

# --- OpenPGP constants -----------------------------------------------------

TAG_SIGNATURE = 2
TAG_PUBLIC_KEY = 6
TAG_USER_ID = 13
TAG_PUBLIC_SUBKEY = 14

# Public-key algorithm IDs (RFC 4880 section 9.1, RFC 9580 section 9.1).
ALGO_RSA_ENCRYPT_SIGN = 1
ALGO_RSA_SIGN_ONLY = 3
ALGO_DSA = 17
ALGO_ECDSA = 19
ALGO_EDDSA_LEGACY = 22
ALGO_ED25519 = 27

RSA_ALGOS = (ALGO_RSA_ENCRYPT_SIGN, ALGO_RSA_SIGN_ONLY)

# Hash algorithm IDs mapped onto hashlib constructors.
HASH_ALGORITHMS = {
    2: ("sha1", hashes.SHA1),
    8: ("sha256", hashes.SHA256),
    9: ("sha384", hashes.SHA384),
    10: ("sha512", hashes.SHA512),
    11: ("sha224", hashes.SHA224),
    12: ("sha3-256", hashes.SHA3_256),
    14: ("sha3-512", hashes.SHA3_512),
}

# Hashes too weak to accept on a software update signature. SHA-1 collision
# attacks are practical, so a SHA-1 signature is rejected outright.
FORBIDDEN_HASHES = {1, 2, 3}  # MD5, SHA-1, RIPEMD-160

# Curve OIDs, encoded as they appear in an OpenPGP key packet.
CURVE_OIDS = {
    "2a8648ce3d030107": ("nistp256", ec.SECP256R1),
    "2b81040022": ("nistp384", ec.SECP384R1),
    "2b81040023": ("nistp521", ec.SECP521R1),
    "2b06010401da470f01": ("ed25519-legacy", None),
    "2b656e": ("cv25519", None),
}

# Signature types. Only binary and canonical-text document signatures are
# meaningful for a detached signature over a release artifact.
SIG_TYPE_BINARY = 0x00
SIG_TYPE_TEXT = 0x01

# Signature subpacket types we read.
SUBPACKET_CREATION_TIME = 2
SUBPACKET_EXPIRY_TIME = 3
SUBPACKET_ISSUER_KEY_ID = 16
SUBPACKET_KEY_FLAGS = 27
SUBPACKET_ISSUER_FINGERPRINT = 33

ARMOR_RE = re.compile(
    r"-----BEGIN PGP (?P<kind>[A-Z ]+)-----(?P<body>.*?)-----END PGP (?P=kind)-----",
    re.DOTALL,
)


# --- Data structures -------------------------------------------------------


@dataclass
class PublicKey:
    """One public key or subkey parsed out of a key block."""

    version: int
    algorithm: int
    created: int
    fingerprint: bytes
    key_id: bytes
    key_material: dict = field(default_factory=dict)
    is_subkey: bool = False
    user_ids: list = field(default_factory=list)

    @property
    def fingerprint_hex(self) -> str:
        return self.fingerprint.hex().upper()

    @property
    def key_id_hex(self) -> str:
        return self.key_id.hex().upper()


@dataclass
class Signature:
    """A parsed OpenPGP signature packet."""

    version: int
    sig_type: int
    pubkey_algorithm: int
    hash_algorithm: int
    hashed_subpackets: bytes
    issuer_key_id: bytes
    issuer_fingerprint: bytes
    created: int
    expires: int
    signature_material: dict
    salt: bytes = b""

    @property
    def issuer_hex(self) -> str:
        if self.issuer_fingerprint:
            return self.issuer_fingerprint.hex().upper()
        return self.issuer_key_id.hex().upper()


@dataclass
class VerifyResult:
    """The outcome of a successful verification."""

    key: PublicKey
    signature: Signature
    hash_name: str

    def describe(self) -> str:
        uid = self.key.user_ids[0] if self.key.user_ids else "unknown identity"
        return f"{uid} (key {self.key.fingerprint_hex[-16:]}, {self.hash_name})"


# --- Armor and packet parsing ----------------------------------------------


def dearmor(data: bytes) -> bytes:
    """
    Strip ASCII armor from a PGP block, returning the raw packet bytes.

    Binary input is returned unchanged, so callers can hand this either a
    `.asc` or a `.sig` file without checking which they have. The armor CRC24
    checksum is validated when present.
    """
    if not data:
        raise VerificationError("Empty OpenPGP data")

    # A binary packet always has the high bit of its first byte set.
    if data[0] & 0x80:
        return data

    try:
        text = data.decode("utf-8", errors="replace")
    except Exception as exc:
        raise VerificationError("Could not decode OpenPGP armor", str(exc)) from exc

    match = ARMOR_RE.search(text)
    if not match:
        raise VerificationError("No OpenPGP armor block found")

    body = match.group("body")

    # Drop the armor headers: everything up to the first blank line.
    lines = body.strip().splitlines()
    payload_lines = []
    seen_blank = False
    for line in lines:
        stripped = line.strip()
        if not seen_blank:
            if not stripped:
                seen_blank = True
                continue
            if ":" in stripped:
                continue
            # No headers at all; treat this line as payload.
            seen_blank = True
        payload_lines.append(stripped)

    checksum_text = ""
    if payload_lines and payload_lines[-1].startswith("="):
        checksum_text = payload_lines.pop()[1:]

    try:
        packets = base64.b64decode("".join(payload_lines))
    except (binascii.Error, ValueError) as exc:
        raise VerificationError("Malformed OpenPGP armor payload", str(exc)) from exc

    if checksum_text:
        try:
            expected = base64.b64decode(checksum_text)
        except (binascii.Error, ValueError) as exc:
            raise VerificationError("Malformed OpenPGP armor checksum", str(exc)) from exc
        actual = crc24(packets)
        if len(expected) == 3 and expected != actual:
            raise VerificationError("OpenPGP armor checksum mismatch")

    return packets


def crc24(data: bytes) -> bytes:
    """Compute the 3-byte CRC24 used by OpenPGP ASCII armor."""
    crc = 0x00B704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x01000000:
                crc ^= 0x01864CFB
    return (crc & 0xFFFFFF).to_bytes(3, "big")


def iter_packets(data: bytes):
    """
    Yield (tag, body) for each OpenPGP packet in `data`.

    Handles both the old-format and new-format packet headers, including
    new-format partial body lengths, which are concatenated into one body.
    """
    offset = 0
    total = len(data)

    while offset < total:
        header = data[offset]
        if not header & 0x80:
            raise VerificationError("Invalid OpenPGP packet header")
        offset += 1

        if header & 0x40:
            # New format: tag is the low 6 bits, length is self-describing.
            tag = header & 0x3F
            body, offset = read_new_format_body(data, offset)
        else:
            # Old format: tag is bits 2-5, length type is the low 2 bits.
            tag = (header >> 2) & 0x0F
            length_type = header & 0x03
            if length_type == 0:
                length = data[offset]
                offset += 1
            elif length_type == 1:
                length = struct.unpack(">H", data[offset:offset + 2])[0]
                offset += 2
            elif length_type == 2:
                length = struct.unpack(">I", data[offset:offset + 4])[0]
                offset += 4
            else:
                # Indeterminate length: the packet runs to the end of the data.
                length = total - offset
            body = data[offset:offset + length]
            offset += length

        yield tag, body


def read_new_format_body(data: bytes, offset: int) -> tuple:
    """Read a new-format packet body, concatenating any partial-length chunks."""
    chunks = []

    while True:
        first = data[offset]
        offset += 1

        if first < 192:
            length = first
            partial = False
        elif first < 224:
            length = ((first - 192) << 8) + data[offset] + 192
            offset += 1
            partial = False
        elif first < 255:
            length = 1 << (first & 0x1F)
            partial = True
        else:
            length = struct.unpack(">I", data[offset:offset + 4])[0]
            offset += 4
            partial = False

        chunks.append(data[offset:offset + length])
        offset += length

        if not partial:
            break

    return b"".join(chunks), offset


def read_mpi(data: bytes, offset: int) -> tuple:
    """Read an OpenPGP multiprecision integer. Returns (value_bytes, new_offset)."""
    if offset + 2 > len(data):
        raise VerificationError("Truncated OpenPGP MPI")
    bit_length = struct.unpack(">H", data[offset:offset + 2])[0]
    offset += 2
    byte_length = (bit_length + 7) // 8
    value = data[offset:offset + byte_length]
    if len(value) != byte_length:
        raise VerificationError("Truncated OpenPGP MPI body")
    return value, offset + byte_length


# --- Key parsing -----------------------------------------------------------


def parse_public_keys(data: bytes) -> list:
    """
    Parse a public key block into a list of PublicKey objects.

    Both the primary key and its subkeys are returned, because release signing
    is very often done by a signing subkey rather than the primary key.
    """
    packets = dearmor(data)
    keys = []
    pending_user_ids = []

    for tag, body in iter_packets(packets):
        if tag in (TAG_PUBLIC_KEY, TAG_PUBLIC_SUBKEY):
            key = parse_key_packet(body, is_subkey=(tag == TAG_PUBLIC_SUBKEY))
            keys.append(key)
            if tag == TAG_PUBLIC_KEY:
                pending_user_ids = key.user_ids
        elif tag == TAG_USER_ID:
            uid = body.decode("utf-8", errors="replace")
            pending_user_ids.append(uid)
            # Subkeys inherit the identity of the primary key for display.
            for key in keys:
                if key.is_subkey and not key.user_ids:
                    key.user_ids.append(uid)

    if not keys:
        raise VerificationError("No public keys found in key block")

    # Backfill identities onto subkeys parsed before the User ID packet.
    primary_uids = next((key.user_ids for key in keys if not key.is_subkey), [])
    for key in keys:
        if not key.user_ids:
            key.user_ids = list(primary_uids)

    return keys


def parse_key_packet(body: bytes, is_subkey: bool) -> PublicKey:
    """Parse a public-key or public-subkey packet body."""
    if not body:
        raise VerificationError("Empty OpenPGP key packet")

    version = body[0]
    if version == 4:
        created = struct.unpack(">I", body[1:5])[0]
        algorithm = body[5]
        material_offset = 6
    elif version == 6:
        created = struct.unpack(">I", body[1:5])[0]
        algorithm = body[5]
        # v6 inserts a 4-byte count of the key material before the material.
        material_offset = 10
    else:
        raise VerificationError(f"Unsupported OpenPGP key version: {version}")

    material = parse_key_material(body, material_offset, algorithm)
    fingerprint = compute_fingerprint(body, version)

    if version == 4:
        key_id = fingerprint[-8:]
    else:
        key_id = fingerprint[:8]

    return PublicKey(
        version=version,
        algorithm=algorithm,
        created=created,
        fingerprint=fingerprint,
        key_id=key_id,
        key_material=material,
        is_subkey=is_subkey,
    )


def parse_key_material(body: bytes, offset: int, algorithm: int) -> dict:
    """Extract the algorithm-specific public key material from a key packet."""
    if algorithm in RSA_ALGOS:
        modulus, offset = read_mpi(body, offset)
        exponent, offset = read_mpi(body, offset)
        return {
            "n": int.from_bytes(modulus, "big"),
            "e": int.from_bytes(exponent, "big"),
        }

    if algorithm in (ALGO_ECDSA, ALGO_EDDSA_LEGACY):
        oid_length = body[offset]
        offset += 1
        oid = body[offset:offset + oid_length]
        offset += oid_length
        point, offset = read_mpi(body, offset)
        curve_name, curve_class = CURVE_OIDS.get(oid.hex(), ("", None))
        return {"curve": curve_name, "curve_class": curve_class, "point": point}

    if algorithm == ALGO_ED25519:
        # RFC 9580: a fixed 32-byte native public key, no MPI framing.
        return {"curve": "ed25519", "curve_class": None, "point": body[offset:offset + 32]}

    if algorithm == ALGO_DSA:
        raise VerificationError(
            "DSA signing keys are not supported",
            "Re-sign releases with an RSA or Ed25519 key",
        )

    raise VerificationError(f"Unsupported OpenPGP public key algorithm: {algorithm}")


def compute_fingerprint(body: bytes, version: int) -> bytes:
    """
    Compute the key fingerprint from a raw key packet body.

    v4 keys hash 0x99 plus a 2-byte length prefix with SHA-1; v6 keys hash
    0x9B plus a 4-byte length prefix with SHA-256. SHA-1 here is prescribed by
    the format for identity, not used as a security signature check.
    """
    if version == 4:
        prefix = b"\x99" + struct.pack(">H", len(body))
        return hashlib.sha1(prefix + body).digest()  # noqa: S324 - format-mandated
    if version == 6:
        prefix = b"\x9b" + struct.pack(">I", len(body))
        return hashlib.sha256(prefix + body).digest()
    raise VerificationError(f"Cannot fingerprint key version {version}")


# --- Signature parsing -----------------------------------------------------


def parse_signatures(data: bytes) -> list:
    """Parse a detached signature file into a list of Signature objects."""
    packets = dearmor(data)
    signatures = []

    for tag, body in iter_packets(packets):
        if tag == TAG_SIGNATURE:
            signatures.append(parse_signature_packet(body))

    if not signatures:
        raise VerificationError("No signature packets found in signature file")

    return signatures


def parse_signature_packet(body: bytes) -> Signature:
    """Parse a v4 or v6 signature packet body."""
    if not body:
        raise VerificationError("Empty OpenPGP signature packet")

    version = body[0]
    if version not in (4, 6):
        raise VerificationError(
            f"Unsupported OpenPGP signature version: {version}",
            "Only v4 and v6 signatures are supported",
        )

    sig_type = body[1]
    pubkey_algorithm = body[2]
    hash_algorithm = body[3]
    offset = 4

    # Subpacket area lengths are 2 bytes in v4 and 4 bytes in v6.
    length_size = 2 if version == 4 else 4
    length_format = ">H" if version == 4 else ">I"

    hashed_length = struct.unpack(length_format, body[offset:offset + length_size])[0]
    offset += length_size
    hashed_subpackets = body[offset:offset + hashed_length]
    offset += hashed_length

    unhashed_length = struct.unpack(length_format, body[offset:offset + length_size])[0]
    offset += length_size
    unhashed_subpackets = body[offset:offset + unhashed_length]
    offset += unhashed_length

    # Two-octet quick check on the digest, skipped: we recompute it in full.
    offset += 2

    salt = b""
    if version == 6:
        salt_length = body[offset]
        offset += 1
        salt = body[offset:offset + salt_length]
        offset += salt_length

    material = parse_signature_material(body, offset, pubkey_algorithm, version)

    hashed_values = read_subpackets(hashed_subpackets)
    unhashed_values = read_subpackets(unhashed_subpackets)

    # Issuer identity is trusted only as a hint for key selection; the actual
    # cryptographic check is what decides whether the key is the right one.
    fingerprint = hashed_values.get(SUBPACKET_ISSUER_FINGERPRINT) or unhashed_values.get(
        SUBPACKET_ISSUER_FINGERPRINT, b""
    )
    if fingerprint and len(fingerprint) > 1:
        fingerprint = fingerprint[1:]  # strip the leading key-version octet

    key_id = hashed_values.get(SUBPACKET_ISSUER_KEY_ID) or unhashed_values.get(
        SUBPACKET_ISSUER_KEY_ID, b""
    )

    created_raw = hashed_values.get(SUBPACKET_CREATION_TIME, b"")
    created = struct.unpack(">I", created_raw)[0] if len(created_raw) == 4 else 0

    expires_raw = hashed_values.get(SUBPACKET_EXPIRY_TIME, b"")
    expires = struct.unpack(">I", expires_raw)[0] if len(expires_raw) == 4 else 0

    return Signature(
        version=version,
        sig_type=sig_type,
        pubkey_algorithm=pubkey_algorithm,
        hash_algorithm=hash_algorithm,
        hashed_subpackets=hashed_subpackets,
        issuer_key_id=key_id,
        issuer_fingerprint=fingerprint,
        created=created,
        expires=expires,
        signature_material=material,
        salt=salt,
    )


def parse_signature_material(body: bytes, offset: int, algorithm: int, version: int) -> dict:
    """Extract the algorithm-specific signature values from a signature packet."""
    if algorithm in RSA_ALGOS:
        value, offset = read_mpi(body, offset)
        return {"s": value}

    if algorithm == ALGO_ECDSA:
        r, offset = read_mpi(body, offset)
        s, offset = read_mpi(body, offset)
        return {"r": r, "s": s}

    if algorithm == ALGO_EDDSA_LEGACY:
        r, offset = read_mpi(body, offset)
        s, offset = read_mpi(body, offset)
        # Ed25519 wants a fixed 64-byte R||S; MPIs drop leading zero bytes.
        return {"raw": r.rjust(32, b"\x00") + s.rjust(32, b"\x00")}

    if algorithm == ALGO_ED25519:
        # RFC 9580: a fixed 64-byte native signature, no MPI framing.
        return {"raw": body[offset:offset + 64]}

    if algorithm == ALGO_DSA:
        raise VerificationError("DSA signatures are not supported")

    raise VerificationError(f"Unsupported OpenPGP signature algorithm: {algorithm}")


def read_subpackets(data: bytes) -> dict:
    """
    Parse a signature subpacket area into {type: value}.

    The critical bit in the subpacket type is masked off; this verifier reads
    only the subpackets it understands and does not enforce unknown critical
    subpackets, which is acceptable for the narrow job of checking a release
    signature against a pinned key.
    """
    values = {}
    offset = 0
    total = len(data)

    while offset < total:
        first = data[offset]
        offset += 1

        if first < 192:
            length = first
        elif first < 255:
            length = ((first - 192) << 8) + data[offset] + 192
            offset += 1
        else:
            length = struct.unpack(">I", data[offset:offset + 4])[0]
            offset += 4

        if length == 0:
            continue

        subpacket_type = data[offset] & 0x7F
        value = data[offset + 1:offset + length]
        offset += length

        values.setdefault(subpacket_type, value)

    return values


# --- Verification ----------------------------------------------------------


def signature_digest(signature: Signature, message: bytes) -> bytes:
    """
    Compute the digest that a detached signature actually covers.

    The signed data is the message followed by a trailer built from the
    signature packet itself, which is what stops an attacker lifting a valid
    signature from one context and replaying it in another.
    """
    hash_name, _ = HASH_ALGORITHMS.get(signature.hash_algorithm, (None, None))
    if hash_name is None:
        raise VerificationError(
            f"Unsupported OpenPGP hash algorithm: {signature.hash_algorithm}"
        )
    if signature.hash_algorithm in FORBIDDEN_HASHES:
        raise VerificationError(
            f"Signature uses a weak hash ({hash_name})",
            "Re-sign releases with SHA-256 or stronger",
        )

    digest = hashlib.new(hash_name)

    # v6 signatures prepend a random salt to the hash context.
    if signature.version == 6 and signature.salt:
        digest.update(signature.salt)

    if signature.sig_type == SIG_TYPE_TEXT:
        digest.update(canonicalise_text(message))
    else:
        digest.update(message)

    # v4 counts the hashed subpacket area in 2 bytes, v6 in 4.
    length_format = ">H" if signature.version == 4 else ">I"

    preamble = (
        bytes([signature.version, signature.sig_type, signature.pubkey_algorithm,
               signature.hash_algorithm])
        + struct.pack(length_format, len(signature.hashed_subpackets))
        + signature.hashed_subpackets
    )
    digest.update(preamble)

    # Trailer: version, 0xFF, then the byte count of everything above that
    # came from the signature packet.
    trailer = bytes([signature.version, 0xFF]) + struct.pack(">I", len(preamble))
    digest.update(trailer)

    return digest.digest()


def canonicalise_text(message: bytes) -> bytes:
    """Normalise line endings to CRLF for a canonical-text-mode signature."""
    return message.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")


def select_key(keys: list, signature: Signature) -> PublicKey | None:
    """
    Find the key in `keys` that the signature claims to come from.

    When the signature names no issuer, every key in the block is a candidate
    and the caller tries each in turn, so an unsigned issuer hint cannot be
    used to steer verification toward the wrong key.
    """
    if signature.issuer_fingerprint:
        for key in keys:
            if key.fingerprint == signature.issuer_fingerprint:
                return key

    if signature.issuer_key_id:
        for key in keys:
            if key.key_id == signature.issuer_key_id:
                return key

    return None


def verify_with_key(key: PublicKey, signature: Signature, message: bytes) -> None:
    """
    Verify one signature against one key, raising VerificationError on failure.

    The digest is computed here rather than delegated, because OpenPGP hashes
    a trailer over the signature packet that no generic verify() call knows
    about; the primitives are therefore driven in prehashed mode.
    """
    if key.algorithm != signature.pubkey_algorithm:
        # Ed25519 has two algorithm IDs for the same maths; treat them as one.
        legacy_pair = {ALGO_EDDSA_LEGACY, ALGO_ED25519}
        if not ({key.algorithm, signature.pubkey_algorithm} <= legacy_pair):
            raise VerificationError("Signature algorithm does not match the key")

    digest = signature_digest(signature, message)
    _, hash_class = HASH_ALGORITHMS[signature.hash_algorithm]

    try:
        if key.algorithm in RSA_ALGOS:
            public_key = RSAPublicNumbers(
                e=key.key_material["e"], n=key.key_material["n"]
            ).public_key()
            public_key.verify(
                signature.signature_material["s"],
                digest,
                padding.PKCS1v15(),
                asym_utils.Prehashed(hash_class()),
            )

        elif key.algorithm in (ALGO_EDDSA_LEGACY, ALGO_ED25519):
            point = key.key_material["point"]
            # The legacy encoding prefixes the 32-byte point with 0x40.
            if len(point) == 33 and point[0] == 0x40:
                point = point[1:]
            if len(point) != 32:
                raise VerificationError("Malformed Ed25519 public key")
            public_key = ed25519.Ed25519PublicKey.from_public_bytes(point)
            # Ed25519 signs the digest itself here, matching how OpenPGP
            # applies EdDSA over an already-computed OpenPGP hash.
            public_key.verify(signature.signature_material["raw"], digest)

        elif key.algorithm == ALGO_ECDSA:
            curve_class = key.key_material.get("curve_class")
            if curve_class is None:
                raise VerificationError(
                    f"Unsupported ECDSA curve: {key.key_material.get('curve') or 'unknown'}"
                )
            point = key.key_material["point"]
            public_key = ec.EllipticCurvePublicKey.from_encoded_point(curve_class(), point)
            der = asym_utils.encode_dss_signature(
                int.from_bytes(signature.signature_material["r"], "big"),
                int.from_bytes(signature.signature_material["s"], "big"),
            )
            public_key.verify(der, digest, ec.ECDSA(asym_utils.Prehashed(hash_class())))

        else:
            raise VerificationError(f"Unsupported key algorithm: {key.algorithm}")

    except InvalidSignature as exc:
        raise VerificationError(
            "Signature does not match the release key",
            f"key {key.fingerprint_hex[-16:]}",
        ) from exc
    except VerificationError:
        raise
    except Exception as exc:
        raise VerificationError("Signature verification failed", str(exc)) from exc


def verify_detached(message: bytes, signature_data: bytes, key_data: bytes) -> VerifyResult:
    """
    Verify a detached OpenPGP signature over `message` against a public key block.

    Returns a VerifyResult on success and raises VerificationError on any
    failure. A signature file containing several signatures passes as soon as
    one of them verifies, which is how multi-key signing arrangements behave.
    """
    keys = parse_public_keys(key_data)
    signatures = parse_signatures(signature_data)

    last_error = None

    for signature in signatures:
        if signature.sig_type not in (SIG_TYPE_BINARY, SIG_TYPE_TEXT):
            last_error = VerificationError(
                f"Signature is not a document signature (type 0x{signature.sig_type:02x})"
            )
            continue

        named_key = select_key(keys, signature)
        candidates = [named_key] if named_key else list(keys)

        for key in candidates:
            try:
                verify_with_key(key, signature, message)
            except VerificationError as exc:
                last_error = exc
                continue

            hash_name, _ = HASH_ALGORITHMS[signature.hash_algorithm]
            return VerifyResult(key=key, signature=signature, hash_name=hash_name)

    if last_error:
        raise last_error
    raise VerificationError("No usable signature found")

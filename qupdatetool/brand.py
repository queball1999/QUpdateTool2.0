"""
Build-time branding, and the trust boundary around it.

A branded updater is built once per application: the build script bakes a
brand payload into the binary carrying the app name, icon, release source,
and - critically - the trusted OpenPGP public key. That payload is what makes
one generic tool behave as MyApp updater.exe, OtherApp updater.exe, and so on.

## Why some brand fields are locked

The updater merges configuration from several layers, and a config.yaml
sitting next to the binary is one of them. On a normal Windows install that
file lives in Program Files and needs admin rights to modify, but plenty of
deployments put the application somewhere user-writable. If a config file
could override `security.public_key` or `source.repo`, then anyone who can
drop a YAML file beside the binary could point the updater at their own
release host, sign their own payload with their own key, and have a trusted,
possibly elevated process install it.

So the brand marks fields as locked. A locked field is applied after every
other layer and cannot be overridden by a config file, an environment
variable, or a command-line flag. The security-critical fields are locked by
default; a brand can add more.

## What this does and does not protect against

Locking defends against an attacker who can write files *next to* the binary.
It does not defend against one who can rewrite the binary itself - at that
point they can strip the check entirely, which is why no amount of
self-verification inside a program is worth much. Real defenses against a
modified binary have to come from outside it:

  - the parent application verifies the updater hash before launching it
    (see integrity.py and the UPDATER_SHA256 build stamp)
  - the OS verifies a code signature (Authenticode, notarization)
  - the installer writes the binary somewhere unprivileged users cannot touch

The self-check here is honest about its scope: it detects accidental
corruption and casual tampering, and it makes the build reproducible enough
to audit. It is not a substitute for the two mechanisms above.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

# Fields that a config file, environment variable, or CLI flag must never be
# able to change once a brand has set them. These are the ones that decide
# where code comes from and which key is trusted to sign it.
SECURITY_CRITICAL_FIELDS = (
    "security.public_key",
    "security.public_key_file",
    "security.require_signature",
    "security.allowed_fingerprints",
    "source.provider",
    "source.host",
    "source.repo",
    "source.manifest_url",
    "source.download_url",
    "source.verify_tls",
)

# Brand payload compiled into the binary by tools/build_branded.py. The
# generated module is absent in a source checkout, which is why the import is
# guarded rather than assumed.
try:
    from . import _brand_data  # type: ignore
except ImportError:
    _brand_data = None


def is_branded() -> bool:
    """Return True when this binary was built with a brand payload."""
    return _brand_data is not None


def brand_info() -> dict:
    """
    Return the build stamp describing this updater binary.

    Mirrors the shape of the BUILD_INFO stamp used by the applications this
    tool updates, so both can be reported together in a support bundle.
    """
    if _brand_data is None:
        return {
            "branded": False,
            "brand_name": "",
            "build_version": "0.0.1",
            "build_date": "",
            "build_commit": "",
            "brand_sha256": "",
        }

    return {
        "branded": True,
        "brand_name": getattr(_brand_data, "BRAND_NAME", ""),
        "build_version": getattr(_brand_data, "BUILD_VERSION", "0.0.1"),
        "build_date": getattr(_brand_data, "BUILD_DATE", ""),
        "build_commit": getattr(_brand_data, "BUILD_COMMIT", ""),
        "brand_sha256": getattr(_brand_data, "BRAND_SHA256", ""),
    }


def load_brand() -> dict:
    """
    Return the brand configuration payload compiled into this binary.

    Returns an empty dict for an unbranded build, which is the normal case
    when running from source or driving the tool entirely by flags.
    """
    if _brand_data is None:
        return {}

    payload = getattr(_brand_data, "BRAND", None)
    if not isinstance(payload, dict):
        return {}

    return copy.deepcopy(payload)


def locked_fields(brand: dict) -> tuple:
    """
    Return the dotted paths this brand refuses to let other layers override.

    Locking is all-or-nothing across the security-critical set. As soon as a
    brand pins *any* of them, the whole set is frozen - including the fields
    the brand left at their defaults.

    That rule exists because partial locking leaves exploitable gaps. A brand
    that pins `source.repo` and `source.provider` for github.com has no reason
    to set `source.host`, since the default API root is correct. But if
    `source.host` stayed unlocked, a config.yaml dropped next to the binary
    could set it to an attacker-controlled server and every API call, release
    listing, and asset download would follow it there. The same argument
    applies to `source.download_url` and the rest: a field left unset is not
    a field that is safe to let someone else set.

    A brand can still opt a specific path out through `unlock`, for the rare
    internal build that needs repointing at a staging host without a rebuild.
    """
    if not brand:
        return ()

    unlocked = set(brand.get("unlock") or ())

    pins_any_critical = any(
        brand_has_value(brand, path) for path in SECURITY_CRITICAL_FIELDS
    )

    locked = set()

    if pins_any_critical:
        locked.update(path for path in SECURITY_CRITICAL_FIELDS if path not in unlocked)

    for path in brand.get("lock") or ():
        if path not in unlocked:
            locked.add(path)

    return tuple(sorted(locked))


def locked_values(brand: dict, defaults: dict) -> dict:
    """
    Return {path: value} for every field this brand locks.

    A locked field the brand does not set falls back to the built-in default
    rather than being left alone, which is what makes the all-or-nothing rule
    in locked_fields() actually bite: an unset `source.host` is pinned to the
    default empty value instead of remaining writable.
    """
    values = {}

    for path in locked_fields(brand):
        value = brand_value(brand, path)

        if value is None:
            value = dotted_get(defaults, path)

        if value is not None:
            values[path] = value

    return values


def dotted_get(data: dict, path: str):
    """Read a value from a nested dict by dotted path."""
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def brand_has_value(brand: dict, path: str) -> bool:
    """Return True when the brand sets a non-empty value at a dotted path."""
    node = brand
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]

    return node not in (None, "", [], {})


def brand_value(brand: dict, path: str):
    """Read a value from the brand payload by dotted path."""
    node = brand
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def compute_brand_hash(brand: dict) -> str:
    """
    Hash a brand payload deterministically.

    Used by the build script to stamp BRAND_SHA256, and by `--self-check` to
    report which brand a binary carries. Keys are sorted so the same brand
    file always produces the same hash regardless of dict ordering.
    """
    canonical = json.dumps(brand, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_brand_file(path: str | Path) -> dict:
    """
    Read a brand YAML file from disk.

    Used by the build script; the runtime reads the compiled payload instead,
    so a brand file shipped alongside a binary is never trusted.
    """
    import yaml

    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise FileNotFoundError(f"Brand file not found: {file_path}")

    data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise TypeError(f"Brand file must contain a mapping: {file_path}")

    return data

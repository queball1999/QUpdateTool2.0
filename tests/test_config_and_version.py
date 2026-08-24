"""
Configuration merging, brand locking, and version comparison tests.

The brand-locking tests are security tests: they assert that a config file or
a command-line flag cannot repoint a branded updater at a different release
source or a different signing key.
"""

from __future__ import annotations

import pytest

from qupdatetool import brand as brand_module
from qupdatetool import config as config_module
from qupdatetool import version
from qupdatetool.errors import ConfigError

# --- version comparison ----------------------------------------------------


@pytest.mark.parametrize(
    "candidate,current,expected",
    [
        ("1.2.4", "1.2.3", True),
        ("1.3.0", "1.2.9", True),
        ("2.0.0", "1.9.9", True),
        ("1.2.3", "1.2.3", False),
        ("1.2.2", "1.2.3", False),
        # Tag prefixes and platform suffixes carry no version meaning.
        ("v0.0.8-release", "0.0.7-dev", True),
        ("v1.2.3-windows", "1.2.3", False),
        ("v2", "1.9.9", True),
        # A release outranks its own prerelease.
        ("1.0.0", "1.0.0-rc1", True),
        ("1.0.0-rc1", "1.0.0", False),
        ("1.0.0-rc2", "1.0.0-rc1", True),
        # Numeric prerelease identifiers sort numerically, not lexically.
        ("1.0.0-rc10", "1.0.0-rc9", True),
    ],
)
def test_is_newer(candidate, current, expected):
    assert version.is_newer(candidate, current) is expected


def test_strip_platform_suffix():
    """Platform words are stripped; real prerelease labels survive."""
    assert version.strip_platform_suffix("v0.0.8-release") == "0.0.8"
    assert version.strip_platform_suffix("v1.2.3-windows") == "1.2.3"
    assert version.strip_platform_suffix("1.0.0-rc1") == "1.0.0-rc1"


def test_unparseable_versions_do_not_crash():
    """A tag that is not a version compares without raising."""
    assert version.compare("nightly", "nightly") == 0
    assert version.parse("not-a-version") is None


# --- config merging --------------------------------------------------------


def test_deep_merge_replaces_lists_wholesale():
    """A list override means exactly these, not append to the defaults."""
    base = {"install": {"installer_args": {"exe": ["/A", "/B"]}}}
    overlay = {"install": {"installer_args": {"exe": ["/C"]}}}

    merged = config_module.deep_merge(base, overlay)

    assert merged["install"]["installer_args"]["exe"] == ["/C"]


def test_deep_merge_ignores_none():
    """An unset flag never clobbers a configured value."""
    base = {"source": {"repo": "owner/app"}}
    merged = config_module.deep_merge(base, {"source": {"repo": None}})

    assert merged["source"]["repo"] == "owner/app"


def test_dotted_access():
    config = config_module.Config({"source": {"repo": "owner/app"}})

    assert config.get("source.repo") == "owner/app"
    assert config.get("source.missing", "fallback") == "fallback"

    config.set("source.host", "https://example.com")
    assert config.get("source.host") == "https://example.com"


def test_flags_override_config_file(tmp_path, monkeypatch):
    """A command-line flag beats a config file for an unlocked field."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("source:\n  channel: prerelease\n", encoding="utf-8")

    config = config_module.build(
        brand={},
        config_path=str(config_file),
        overrides={"source": {"channel": "stable"}},
    )

    assert config.get("source.channel") == "stable"


# --- brand locking (security) ----------------------------------------------

BRAND = {
    "app": {"name": "MyApp"},
    "source": {"provider": "github", "repo": "trusted/app", "host": ""},
    "security": {
        "public_key": "-----BEGIN PGP PUBLIC KEY BLOCK-----\ntrusted\n",
        "require_signature": True,
    },
    "lock": ["source.channel"],
}


def test_security_critical_fields_are_locked_automatically():
    """A brand that sets a key and a repo locks them without being asked."""
    locked = brand_module.locked_fields(BRAND)

    assert "security.public_key" in locked
    assert "security.require_signature" in locked
    assert "source.repo" in locked
    assert "source.provider" in locked


def test_config_file_cannot_repoint_the_repo(tmp_path):
    """
    A config.yaml dropped beside a branded binary cannot change the source.

    This is the core trust-boundary test: without it, anyone able to write a
    file next to the updater could redirect it at their own release host.
    """
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "source:\n  repo: attacker/evil\n  host: https://evil.example\n",
        encoding="utf-8",
    )

    config = config_module.build(brand=BRAND, config_path=str(config_file))

    assert config.get("source.repo") == "trusted/app"
    assert config.get("source.host") == ""


def test_config_file_cannot_swap_the_signing_key(tmp_path):
    """A config file cannot replace the pinned public key."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "security:\n  public_key: |\n    attacker key\n", encoding="utf-8"
    )

    config = config_module.build(brand=BRAND, config_path=str(config_file))

    assert "trusted" in config.get("security.public_key")
    assert "attacker" not in config.get("security.public_key")


def test_flags_cannot_disable_signature_verification():
    """--no-require-signature is ignored against a brand that locked it on."""
    config = config_module.build(
        brand=BRAND,
        overrides={"security": {"require_signature": False}},
    )

    assert config.get("security.require_signature") is True


def test_explicitly_locked_field_is_enforced(tmp_path):
    """A field named in the brand lock list cannot be overridden either."""
    branded = dict(BRAND)
    branded["source"] = dict(BRAND["source"], channel="stable")

    config_file = tmp_path / "config.yaml"
    config_file.write_text("source:\n  channel: prerelease\n", encoding="utf-8")

    config = config_module.build(brand=branded, config_path=str(config_file))

    assert config.get("source.channel") == "stable"


def test_unlocked_fields_remain_overridable(tmp_path):
    """Locking is targeted: ordinary settings still respond to config."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("logging:\n  level: DEBUG\n", encoding="utf-8")

    config = config_module.build(brand=BRAND, config_path=str(config_file))

    assert config.get("logging.level") == "DEBUG"


def test_unbranded_build_locks_nothing():
    """Running unbranded, every field is configurable as normal."""
    config = config_module.build(
        brand={},
        overrides={"source": {"repo": "any/repo"}, "security": {"require_signature": False}},
    )

    assert config.get("source.repo") == "any/repo"
    assert config.get("security.require_signature") is False


def test_brand_hash_is_stable():
    """The brand hash does not depend on dict ordering."""
    first = brand_module.compute_brand_hash({"a": 1, "b": {"c": 2, "d": 3}})
    second = brand_module.compute_brand_hash({"b": {"d": 3, "c": 2}, "a": 1})

    assert first == second


# --- validation ------------------------------------------------------------


def test_validate_rejects_unknown_provider():
    config = config_module.build(overrides={
        "app": {"name": "X"}, "source": {"provider": "bitbucket", "repo": "a/b"},
    })

    with pytest.raises(ConfigError, match="Unknown provider"):
        config.validate()


def test_validate_requires_repo_for_git_providers():
    config = config_module.build(overrides={
        "app": {"name": "X"}, "source": {"provider": "github", "repo": ""},
    })

    with pytest.raises(ConfigError, match="No repository"):
        config.validate()


def test_validate_rejects_bad_regex():
    config = config_module.build(overrides={
        "app": {"name": "X"},
        "source": {"provider": "github", "repo": "a/b"},
        "assets": {"pattern": "[unclosed"},
        "security": {"require_signature": False},
    })

    with pytest.raises(ConfigError, match="Invalid regex"):
        config.validate()


def test_validate_requires_a_key_when_signatures_are_required():
    config = config_module.build(overrides={
        "app": {"name": "X"},
        "source": {"provider": "github", "repo": "a/b"},
        "security": {"require_signature": True, "allow_gpg_fallback": False},
    })

    with pytest.raises(ConfigError, match="no public key"):
        config.validate()


def test_token_resolves_from_environment(monkeypatch):
    """A token named by env var is read at runtime, not stored in config."""
    monkeypatch.setenv("MY_TOKEN", "secret-value")

    config = config_module.build(overrides={"source": {"token_env": "MY_TOKEN"}})

    assert config.token == "secret-value"


def test_inline_token_beats_environment(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "from-env")

    config = config_module.build(
        overrides={"source": {"token": "from-flag", "token_env": "MY_TOKEN"}}
    )

    assert config.token == "from-flag"


def test_config_file_cannot_unlock_a_locked_field(tmp_path):
    """
    A config file cannot lift a lock the brand applied.

    This matters because a project may keep ONE yaml file that serves as both
    the build-time brand and the shipped runtime config (an app like app.exe
    might do this). The runtime copy carries a `lock:` list, and it must be
    inert when read as config; otherwise an edited copy could `unlock` the
    signing key.
    """
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "unlock:\n"
        "  - security.public_key\n"
        "  - source.repo\n"
        "security:\n"
        "  public_key: attacker key\n"
        "source:\n"
        "  repo: attacker/evil\n",
        encoding="utf-8",
    )

    config = config_module.build(brand=BRAND, config_path=str(config_file))

    assert "trusted" in config.get("security.public_key")
    assert config.get("source.repo") == "trusted/app"


def test_config_file_lock_list_is_not_applied(tmp_path):
    """A `lock:` list in a config file does not freeze anything."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "lock:\n  - logging.level\nlogging:\n  level: WARNING\n", encoding="utf-8"
    )

    config = config_module.build(
        brand={},
        config_path=str(config_file),
        overrides={"logging": {"level": "DEBUG"}},
    )

    # The flag still wins; the config file lock was ignored.
    assert config.get("logging.level") == "DEBUG"
    assert config.get("meta.locked_fields") == []

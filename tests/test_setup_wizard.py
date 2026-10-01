"""
Tests for the `--setup` wizard's non-GUI half: state.py, validators.py, and
generator.py. Deliberately does not import anything from qupdatetool.ui.setup
that touches PySide6 (pages.py, wizard.py, demo.py, workers.py) - matching
the fact that this repository has no Qt tests today, and proving the split
that lets `--setup`-adjacent logic stay covered without the GUI extra
installed (see requirements-dev.txt / requirements-gui.txt).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile

import pytest
import yaml

from qupdatetool import config as config_module
from qupdatetool.backends.base import Asset, Release
from qupdatetool.errors import NetworkError
from qupdatetool.notes import ReleaseNotes
from qupdatetool.ui.setup import generator, validators
from qupdatetool.ui.setup.state import WizardState
from qupdatetool.updater import UpdateCheck

# --- WizardState -----------------------------------------------------------


def test_wizard_state_defaults_match_config_shape():
    """A fresh WizardState carries every config.DEFAULTS section, unmodified."""
    state = WizardState()
    assert state.get("source.provider") == "github"
    assert state.get("install.mode") == "silent"
    assert state.wizard["branding_enabled"] is False


def test_wizard_state_get_set_dotted_path():
    state = WizardState()
    state.set("app.name", "MyApp")
    state.set("app.process_names", ["MyApp.exe"])

    assert state.get("app.name") == "MyApp"
    assert state.get("app.process_names") == ["MyApp.exe"]
    assert state.get("app.missing", "fallback") == "fallback"


def test_wizard_state_from_config_file_round_trips(tmp_path):
    """`--setup --config existing.yaml` should prefill from that file."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n  name: MyApp\n  process_names: [MyApp.exe]\n"
        "source:\n  repo: exampleltd/myapp\n  channel: prerelease\n",
        encoding="utf-8",
    )

    state = WizardState.from_config_file(str(config_file))

    assert state.get("app.name") == "MyApp"
    assert state.get("app.process_names") == ["MyApp.exe"]
    assert state.get("source.repo") == "exampleltd/myapp"
    assert state.get("source.channel") == "prerelease"


def test_wizard_state_from_config_file_strips_lock_metadata(tmp_path):
    """lock/unlock are brand build-time metadata, not runtime config - see config.py."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n  name: MyApp\nlock: [source.repo]\nunlock: [source.host]\n",
        encoding="utf-8",
    )

    state = WizardState.from_config_file(str(config_file))

    assert "lock" not in state.data
    assert "unlock" not in state.data


def test_wizard_state_to_config_is_a_real_config():
    state = WizardState()
    state.set("app.name", "MyApp")

    config = state.to_config()

    assert isinstance(config, config_module.Config)
    assert config.app_name == "MyApp"


# --- validators.py -----------------------------------------------------


def test_check_path_missing_file(tmp_path):
    result = validators.check_path(str(tmp_path / "nope.exe"), "file")
    assert result.ok is False


def test_check_path_existing_directory(tmp_path):
    result = validators.check_path(str(tmp_path), "dir")
    assert result.ok is True


def test_check_path_empty_is_treated_as_optional():
    result = validators.check_path("", "file")
    assert result.ok is True


def test_check_regex_invalid():
    result = validators.check_regex("(unterminated")
    assert result.ok is False
    assert "regex" in result.message.lower()


def test_check_regex_valid():
    assert validators.check_regex(r"\.deb$").ok is True
    assert validators.check_regex("").ok is True  # empty matches everything


def test_check_process_names_none_running():
    result = validators.check_process_names(["DefinitelyNotARealProcess12345"])
    assert result.ok is True
    assert "not running" in result.message.lower() or "none" in result.message.lower()


def test_check_process_names_empty_list():
    result = validators.check_process_names([])
    assert result.ok is True


def test_check_public_key_file_missing():
    result = validators.check_public_key_file("no-such-file.asc")
    assert result.ok is False


def test_check_public_key_file_garbage(tmp_path):
    garbage = tmp_path / "bad.asc"
    garbage.write_text("this is not a PGP key", encoding="utf-8")

    result = validators.check_public_key_file(str(garbage))

    assert result.ok is False


def find_gpg() -> str | None:
    """Minimal, test-local copy of test_openpgp.find_gpg's platform handling."""
    if sys.platform.startswith("win"):
        import os

        candidate = os.path.join(
            os.environ.get("ProgramFiles", r"C:\Program Files"), "GnuPG", "bin", "gpg.exe"
        )
        if os.path.isfile(candidate):
            return candidate

    found = shutil.which("gpg") or shutil.which("gpg2")
    if found and sys.platform.startswith("win") and "Git" in found:
        return None
    return found


GPG = find_gpg()


@pytest.mark.skipif(GPG is None, reason="a native gpg is not installed")
def test_check_public_key_file_valid_key(tmp_path):
    """A real ASCII-armored key parses and its fingerprint is surfaced."""
    home = tmp_path / "gnupg"
    home.mkdir()
    params = home / "key.params"
    params.write_text(
        "%no-protection\nKey-Type: eddsa\nKey-Curve: ed25519\nKey-Usage: sign\n"
        "Name-Real: Setup Wizard Test\nName-Email: wizard@example.invalid\n"
        "Expire-Date: 0\n%commit\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [GPG, "--homedir", str(home), "--batch", "--quiet", "--gen-key", str(params)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"Could not generate a test key: {result.stderr}")

    export = subprocess.run(
        [GPG, "--homedir", str(home), "--batch", "--quiet", "--armor",
         "--export", "wizard@example.invalid"],
        capture_output=True, check=True,
    )
    key_file = tmp_path / "key.asc"
    key_file.write_bytes(export.stdout)

    result = validators.check_public_key_file(str(key_file))

    assert result.ok is True
    assert result.detail  # a fingerprint was surfaced


def test_connection_sync_success(monkeypatch):
    """A working backend surfaces the resolved release/asset without downloading."""
    asset = Asset(name="MyApp-1.0-windows-installer.exe", url="https://example.invalid/asset")
    release = Release(tag="v1.0.0", assets=[asset])
    check = UpdateCheck(
        available=True,
        current_version="0.9.0",
        latest_version="1.0.0",
        reason="1.0.0 is newer than 0.9.0",
        notes=ReleaseNotes(body="### Changes\n- Fixed things"),
    )
    check.release = release
    check.asset = asset

    class FakeUpdater:
        def __init__(self, config):
            pass

        def check(self, fetch_notes=True):
            return check

    monkeypatch.setattr(validators, "Updater", FakeUpdater)

    config = WizardState().to_config()
    result = validators.test_connection_sync(config)

    assert result.ok is True
    assert result.latest_version == "1.0.0"
    assert result.asset_name == "MyApp-1.0-windows-installer.exe"
    assert "Fixed things" in result.notes_preview


def test_connection_sync_wraps_updater_errors(monkeypatch):
    """A NetworkError (or any UpdaterError) becomes a friendly failed result, not a raise."""

    class FailingUpdater:
        def __init__(self, config):
            pass

        def check(self, fetch_notes=True):
            raise NetworkError("Could not reach github", "connection refused")

    monkeypatch.setattr(validators, "Updater", FailingUpdater)

    config = WizardState().to_config()
    result = validators.test_connection_sync(config)

    assert result.ok is False
    assert "github" in result.message.lower()


# --- generator.py --------------------------------------------------------


def _sample_state():
    state = WizardState()
    state.set("app.name", "MyApp")
    state.set("app.publisher", "Example Ltd")
    state.set("app.process_names", ["MyApp.exe", "MyAppHelper.exe"])
    state.set("source.repo", "exampleltd/myapp")
    state.set("source.tag_pattern", "-release$")
    state.set("security.allowed_fingerprints", ["0E33B487E4193A20AB494B29F70C9E44B78E7B7C"])
    return state


def test_render_config_yaml_is_valid_and_round_trips():
    state = _sample_state()

    text = generator.render_config_yaml(state)
    parsed = yaml.safe_load(text)

    assert parsed["app"]["name"] == "MyApp"
    assert parsed["app"]["process_names"] == ["MyApp.exe", "MyAppHelper.exe"]
    assert parsed["source"]["repo"] == "exampleltd/myapp"
    assert parsed["source"]["tag_pattern"] == "-release$"


def test_render_config_yaml_loads_through_load_yaml_file(tmp_path):
    """The generated file must actually work with `updater --config`."""
    state = _sample_state()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(generator.render_config_yaml(state), encoding="utf-8")

    data = config_module.load_yaml_file(config_file)

    assert data["app"]["name"] == "MyApp"


def test_render_brand_yaml_includes_lock_list_only_when_set():
    state = _sample_state()

    without_locks = yaml.safe_load(generator.render_brand_yaml(state))
    assert "lock" not in without_locks

    state.wizard["extra_lock_fields"] = ["source.channel", "install.elevate"]
    with_locks = yaml.safe_load(generator.render_brand_yaml(state))
    assert with_locks["lock"] == ["source.channel", "install.elevate"]

    # The key is never inlined - it's injected at build time via --key.
    assert with_locks["security"]["public_key_file"] == ""


def test_render_ci_yaml_matrixes_selected_platforms():
    state = _sample_state()
    state.wizard["target_platforms"] = ["windows-latest", "macos-latest"]
    state.wizard["qupdatetool_repo"] = "example-org/QUpdateTool2.0"
    state.wizard["key_secret_name"] = "MY_SECRET"

    ci = yaml.safe_load(generator.render_ci_yaml(state))

    matrix = ci["jobs"]["build-updater"]["strategy"]["matrix"]["os"]
    assert matrix == ["windows-latest", "macos-latest"]
    assert ci["jobs"]["build-updater"]["steps"][1]["with"]["repository"] == "example-org/QUpdateTool2.0"


def test_render_readme_mentions_branding_steps_only_when_enabled():
    state = _sample_state()

    unbranded = generator.render_readme(state)
    assert "updater-brand.yaml" not in unbranded

    state.wizard["branding_enabled"] = True
    branded = generator.render_readme(state)
    assert "updater-brand.yaml" in branded
    assert "RELEASE_PUBLIC_KEY" in branded


def test_build_export_bundle_unbranded(tmp_path):
    state = _sample_state()

    members = generator.build_export_bundle(state, tmp_path / "bundle.zip")

    assert set(members) == {"config.yaml", "README.md"}
    with zipfile.ZipFile(tmp_path / "bundle.zip") as archive:
        assert set(archive.namelist()) == set(members)
        assert archive.testzip() is None


def test_build_export_bundle_branded(tmp_path):
    state = _sample_state()
    state.wizard["branding_enabled"] = True

    members = generator.build_export_bundle(state, tmp_path / "bundle.zip")

    assert set(members) == {
        "config.yaml",
        "updater-brand.yaml",
        ".github/workflows/build-updater.yml",
        "README.md",
    }


def test_build_export_bundle_includes_key_file_when_present(tmp_path):
    state = _sample_state()
    state.wizard["branding_enabled"] = True

    key_file = tmp_path / "release-key.asc"
    key_file.write_text("-----BEGIN PGP PUBLIC KEY BLOCK-----\nfake\n", encoding="utf-8")
    state.set("security.public_key_file", str(key_file))

    members = generator.build_export_bundle(state, tmp_path / "bundle.zip")

    assert "release-key.asc" in members
    with zipfile.ZipFile(tmp_path / "bundle.zip") as archive:
        assert archive.read("release-key.asc").decode() == key_file.read_text(encoding="utf-8")

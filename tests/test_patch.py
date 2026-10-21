"""
Patch update tests: building a .qpatch, applying it all or nothing, planning
a chain of patches across releases, and the updater falling back to the full
installer when a patch cannot be used.

The property that matters most: whatever goes wrong, the install is left as
a complete version - the one before the patch, or the one after it - never a
mix of the two.
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest

from qupdatetool import config as config_module
from qupdatetool import patch
from qupdatetool.backends.base import Asset, Release
from qupdatetool.patch import PatchError
from qupdatetool.updater import Updater


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def snapshot(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): p.read_text()
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture
def versions(tmp_path):
    """Version 1 and version 2 of an app, and an install of version 1 with user data in it."""
    old = tmp_path / "v1"
    write(old / "app.exe", "app 1")
    write(old / "lib" / "core.dll", "core 1")
    write(old / "lib" / "same.dll", "unchanged")
    write(old / "lib" / "gone.dll", "removed in 2")

    new = tmp_path / "v2"
    write(new / "app.exe", "app 2")
    write(new / "lib" / "core.dll", "core 2")
    write(new / "lib" / "same.dll", "unchanged")
    write(new / "plugins" / "extra" / "p.dll", "added in 2")

    install = tmp_path / "parent" / "install"
    shutil.copytree(old, install)
    write(install / "user.cfg", "user data")

    return old, new, install


def make(old, new, out_dir, from_version="v1.0.0", to_version="v2.0.0") -> Path:
    out = out_dir / patch.patch_asset_name(f"App-{to_version}-windows-x64", from_version)
    patch.create_patch(old, new, out, app="App", from_version=from_version,
                       to_version=to_version, os_family="windows", arch="x64")
    return out


# --- building and applying ---


def test_patch_carries_only_changed_files(versions, tmp_path):
    old, new, _ = versions
    out = make(old, new, tmp_path)

    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read(patch.MANIFEST_NAME))

    assert names == {
        patch.MANIFEST_NAME,
        "files/app.exe",
        "files/lib/core.dll",
        "files/plugins/extra/p.dll",
    }
    ops = {op["path"]: op["op"] for op in manifest["ops"]}
    assert ops == {
        "app.exe": "replace",
        "lib/core.dll": "replace",
        "lib/gone.dll": "delete",
        "plugins/extra/p.dll": "add",
    }
    assert set(manifest["target"]) == {"app.exe", "lib/core.dll", "lib/same.dll",
                                       "plugins/extra/p.dll"}


def test_apply_turns_old_into_new_and_keeps_user_data(versions, tmp_path):
    old, new, install = versions
    out = make(old, new, tmp_path)

    result = patch.apply_patch(out, install, expected_from="v1.0.0", expected_to="v2.0.0")

    assert result.written == 3 and result.deleted == 1 and result.verified == 4
    expected = snapshot(new)
    expected["user.cfg"] = "user data"
    assert snapshot(install) == expected


def test_scratch_folder_stays_out_of_the_install_and_is_removed(versions, tmp_path):
    old, new, install = versions
    patch.apply_patch(make(old, new, tmp_path), install)

    leftovers = [p.name for p in install.parent.iterdir() if p.name.startswith(patch.WORK_PREFIX)]
    assert leftovers == []
    assert not any(p.name.startswith(patch.WORK_PREFIX) for p in install.rglob("*"))


def test_zip_inputs_with_a_top_folder_are_unwrapped(versions, tmp_path):
    old, new, install = versions
    zips = []
    for tree in (old, new):
        archive = tmp_path / f"{tree.name}.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            for path in tree.rglob("*"):
                if path.is_file():
                    handle.write(path, "App/" + path.relative_to(tree).as_posix())
        zips.append(archive)

    with patch.open_tree(zips[0]) as old_root, patch.open_tree(zips[1]) as new_root:
        out = make(old_root, new_root, tmp_path)

    patch.apply_patch(out, install)
    assert snapshot(install)["plugins/extra/p.dll"] == "added in 2"


def test_wrong_base_version_is_refused_and_nothing_changes(versions, tmp_path):
    old, new, install = versions
    out = make(old, new, tmp_path)
    write(install / "lib" / "core.dll", "someone else's core")
    before = snapshot(install)

    with pytest.raises(PatchError, match="do not match"):
        patch.apply_patch(out, install)

    assert snapshot(install) == before


def test_patch_for_another_version_is_refused(versions, tmp_path):
    old, new, install = versions
    out = make(old, new, tmp_path)

    with pytest.raises(PatchError, match="different version"):
        patch.apply_patch(out, install, expected_from="v0.9.0")


def test_corrupt_file_inside_the_patch_is_refused(versions, tmp_path):
    old, new, install = versions
    out = make(old, new, tmp_path)
    tampered = tmp_path / "tampered.qpatch"
    with zipfile.ZipFile(out) as source, zipfile.ZipFile(tampered, "w") as sink:
        for item in source.infolist():
            data = source.read(item)
            sink.writestr(item, b"evil" if item.filename == "files/app.exe" else data)
    before = snapshot(install)

    with pytest.raises(PatchError, match="corrupt"):
        patch.apply_patch(tampered, install)

    assert snapshot(install) == before


def test_failure_part_way_rolls_back(versions, tmp_path, monkeypatch):
    old, new, install = versions
    out = make(old, new, tmp_path)
    before = snapshot(install)

    real_replace = os.replace

    def flaky_replace(source, destination):
        if Path(destination).name == "p.dll":
            raise PermissionError(13, "in use", str(destination))
        return real_replace(source, destination)

    monkeypatch.setattr(patch.os, "replace", flaky_replace)

    with pytest.raises(PatchError, match="previous version is intact"):
        patch.apply_patch(out, install)

    monkeypatch.setattr(patch.os, "replace", real_replace)
    assert snapshot(install) == before
    assert not (install / "plugins").exists()


def test_full_verification_catches_a_damaged_untouched_file(versions, tmp_path):
    """A file the patch does not touch but which is wrong fails the patch, and it is undone."""
    old, new, install = versions
    out = make(old, new, tmp_path)
    write(install / "lib" / "same.dll", "damaged")
    before = snapshot(install)

    with pytest.raises(PatchError, match="same.dll"):
        patch.apply_patch(out, install, verify=patch.VERIFY_FULL)

    assert snapshot(install) == before


def test_changed_scope_only_checks_what_the_patch_wrote(versions, tmp_path):
    old, new, install = versions
    out = make(old, new, tmp_path)
    write(install / "lib" / "same.dll", "damaged")

    result = patch.apply_patch(out, install, verify=patch.VERIFY_CHANGED)

    assert result.verified == 3


def test_path_traversal_in_the_manifest_is_refused(versions, tmp_path):
    _, _, install = versions
    evil = tmp_path / "evil.qpatch"
    manifest = {"format": 1, "from_version": "1", "to_version": "2",
                "ops": [{"op": "add", "path": "../outside.dll", "sha256": "0" * 64}],
                "target": {}}
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr(patch.MANIFEST_NAME, json.dumps(manifest))

    with pytest.raises(PatchError, match="traversal"):
        patch.apply_patch(evil, install)

    assert not (install.parent / "outside.dll").exists()


def test_unknown_format_is_refused(versions, tmp_path):
    _, _, install = versions
    future = tmp_path / "future.qpatch"
    with zipfile.ZipFile(future, "w") as archive:
        archive.writestr(patch.MANIFEST_NAME, json.dumps({"format": 99}))

    with pytest.raises(PatchError, match="Unsupported patch format"):
        patch.apply_patch(future, install)


# --- naming ---


def test_patch_names_round_trip():
    name = patch.patch_asset_name("App-v2.0.0-release-windows-x64", "v1.0.0-release")
    assert name == "App-v2.0.0-release-windows-x64.from-v1.0.0-release.qpatch"
    assert patch.parse_patch_name(name) == ("App-v2.0.0-release-windows-x64", "v1.0.0-release")
    assert patch.parse_patch_name("App-2.0.0-windows-installer.exe") is None


def test_version_key_keeps_dev_and_release_builds_apart():
    assert patch.version_key("v1.2.0-release") == patch.version_key("1.2.0")
    assert patch.version_key("v1.2.0-dev") != patch.version_key("v1.2.0-release")


# --- planning ---


def asset(name: str, size: int = 100) -> Asset:
    return Asset(name=name, url=f"https://example.invalid/{name}", size=size)


def release(tag: str, *patch_from: str, size: int = 100, extra=()) -> Release:
    assets = [asset(f"App-{tag}-windows-installer.exe", 10_000)]
    assets += [asset(patch.patch_asset_name(f"App-{tag}-windows-x64", source), size)
               for source in patch_from]
    assets += list(extra)
    return Release(tag=tag, assets=assets)


def plan(releases, current, latest=None):
    latest = latest or releases[0]
    return patch.plan_chain(releases, current, latest, "windows", "x86_64")


def test_single_patch_to_the_latest():
    releases = [release("v1.1.0", "v1.0.0")]
    result = plan(releases, "v1.0.0")
    assert [step.to_version for step in result.steps] == ["v1.1.0"]


def test_chain_across_several_releases():
    releases = [
        release("v1.3.0", "v1.2.0"),
        release("v1.2.0", "v1.1.0"),
        release("v1.1.0", "v1.0.0"),
    ]
    result = plan(releases, "v1.0.0")
    assert [(s.from_version, s.to_version) for s in result.steps] == [
        ("v1.0.0", "v1.1.0"), ("v1.1.0", "v1.2.0"), ("v1.2.0", "v1.3.0"),
    ]


def test_fewest_patches_wins_then_smallest_download():
    releases = [
        release("v1.2.0", "v1.1.0", "v1.0.0"),
        release("v1.1.0", "v1.0.0"),
    ]
    result = plan(releases, "v1.0.0")
    assert [s.to_version for s in result.steps] == ["v1.2.0"]


def test_gap_in_the_chain_means_no_plan():
    releases = [
        release("v1.3.0", "v1.2.0"),
        release("v1.2.0"),
        release("v1.1.0", "v1.0.0"),
    ]
    assert plan(releases, "v1.0.0") is None


def test_already_latest_means_no_plan():
    releases = [release("v1.1.0", "v1.0.0")]
    assert plan(releases, "v1.1.0") is None


def test_patches_for_another_os_are_ignored():
    linux_patch = asset(patch.patch_asset_name("App-v1.1.0-linux-x64", "v1.0.0"))
    releases = [release("v1.1.0", extra=[linux_patch])]
    assert plan(releases, "v1.0.0") is None


def test_chain_never_goes_past_the_latest_release():
    releases = [
        release("v1.2.0", "v1.1.0"),
        release("v1.1.0", "v1.0.0"),
    ]
    result = plan(releases, "v1.0.0", latest=releases[1])
    assert [s.to_version for s in result.steps] == ["v1.1.0"]


def test_release_suffix_matches_a_bare_current_version():
    releases = [release("v1.1.0-release", "v1.0.0-release")]
    assert plan(releases, "1.0.0") is not None


def test_max_chain_is_respected():
    releases = [
        release("v1.2.0", "v1.1.0"),
        release("v1.1.0", "v1.0.0"),
    ]
    assert patch.plan_chain(releases, "v1.0.0", releases[0], "windows", "x86_64",
                            max_steps=1) is None


# --- the updater ---


class FakeBackend:
    name = "fake"
    session = None

    def __init__(self, releases, files):
        self.releases = releases
        self.files = files

    def candidate_releases(self):
        return self.releases

    def latest_release(self):
        return self.releases[0]

    def download_headers(self):
        return {}


class FakeReport:
    def describe(self):
        return "verified (test)"


def build_updater(tmp_path, install, releases, files, monkeypatch, **patches):
    config = config_module.Config(config_module.deep_merge(config_module.DEFAULTS, {
        "app": {"name": "App", "current_version": "v1.0.0",
                "install_dir": str(install)},
        "source": {"provider": "generic", "download_url": "https://example.invalid/app.exe"},
        "install": {"download_dir": str(tmp_path / "downloads")},
        "process": {"stop_parent": False, "relaunch": False},
        "notes": {"source": "none"},
        "patches": {"enabled": True, **patches},
    }))

    backend = FakeBackend(releases, files)
    monkeypatch.setattr("qupdatetool.updater.get_backend", lambda config: backend)
    updater = Updater(config)
    updater.host.os_family = "windows"
    updater.host.arch = "x86_64"
    updater.host.running_as_appimage = False

    def fetch_file(url, target, expected_size=0, progress_callback=None):
        shutil.copy(files[Path(url).name], target)
        return Path(target)

    monkeypatch.setattr(updater.downloader, "fetch_file", fetch_file)
    monkeypatch.setattr(updater.verifier, "verify_release_artifact",
                        lambda release, path, asset, log=None: FakeReport())
    return updater


@pytest.fixture
def published(versions, tmp_path):
    """A release v2.0.0 with a full installer and a patch from v1.0.0."""
    old, new, install = versions
    out = make(old, new, tmp_path / "published")
    rel = Release(tag="v2.0.0", assets=[
        asset("App-v2.0.0-windows-installer.exe", 50_000_000),
        Asset(name=out.name, url=f"https://example.invalid/{out.name}", size=out.stat().st_size),
    ])
    return new, install, rel, {out.name: out}


def test_check_reports_a_patch_update(published, tmp_path, monkeypatch):
    _, install, rel, files = published
    updater = build_updater(tmp_path, install, [rel], files, monkeypatch)

    check = updater.check(fetch_notes=False)
    data = check.to_dict()

    assert check.asset.name == "App-v2.0.0-windows-installer.exe"
    assert data["update_method"] == "patch"
    assert data["download_size"] == check.patch_plan.size
    assert [p["to"] for p in data["patches"]] == ["v2.0.0"]


def test_disabled_patches_report_a_full_update(published, tmp_path, monkeypatch):
    _, install, rel, files = published
    updater = build_updater(tmp_path, install, [rel], files, monkeypatch, enabled=False)

    data = updater.check(fetch_notes=False).to_dict()

    assert data["update_method"] == "full"
    assert data["patches"] == []


def test_patch_bigger_than_the_ratio_uses_the_full_installer(published, tmp_path, monkeypatch):
    _, install, rel, files = published
    updater = build_updater(tmp_path, install, [rel], files, monkeypatch,
                            max_size_ratio=0.000001)

    assert updater.check(fetch_notes=False).patch_plan is None


def test_apply_patches_in_place(published, tmp_path, monkeypatch):
    new, install, rel, files = published
    updater = build_updater(tmp_path, install, [rel], files, monkeypatch)
    monkeypatch.setattr(updater, "download", lambda check: pytest.fail("full download used"))

    outcome = updater.apply(updater.check(fetch_notes=False))

    assert outcome.success
    assert outcome.method.startswith("patched in place (1 patch")
    expected = snapshot(new)
    expected["user.cfg"] = "user data"
    assert snapshot(install) == expected
    assert not any((tmp_path / "downloads").iterdir())


def test_a_patch_that_does_not_fit_falls_back_to_the_full_installer(published, tmp_path,
                                                                    monkeypatch):
    _, install, rel, files = published
    write(install / "app.exe", "modified locally")
    updater = build_updater(tmp_path, install, [rel], files, monkeypatch)

    class FullInstall(Exception):
        pass

    def full_download(check):
        raise FullInstall

    monkeypatch.setattr(updater, "download", full_download)

    with pytest.raises(FullInstall):
        updater.apply(updater.check(fetch_notes=False))

    assert snapshot(install)["app.exe"] == "modified locally"


def test_without_fallback_the_patch_error_is_raised(published, tmp_path, monkeypatch):
    _, install, rel, files = published
    write(install / "app.exe", "modified locally")
    updater = build_updater(tmp_path, install, [rel], files, monkeypatch,
                            fallback_to_full=False)

    with pytest.raises(PatchError):
        updater.apply(updater.check(fetch_notes=False))


def test_dry_run_downloads_and_verifies_but_changes_nothing(published, tmp_path, monkeypatch):
    _, install, rel, files = published
    updater = build_updater(tmp_path, install, [rel], files, monkeypatch)
    updater.config.set("meta.dry_run", True)
    before = snapshot(install)

    outcome = updater.apply(updater.check(fetch_notes=False))

    assert outcome.method.startswith("dry run: 1 patch")
    assert snapshot(install) == before


def test_patches_are_never_chosen_as_the_full_install(published, tmp_path, monkeypatch):
    _, install, rel, files = published
    rel.assets = [a for a in rel.assets if a.name.endswith(".qpatch")] + [
        asset("App-v2.0.0-windows.zip", 50_000_000)
    ]
    updater = build_updater(tmp_path, install, [rel], files, monkeypatch)

    asset_, _ = updater.select_asset(rel)

    assert asset_.name == "App-v2.0.0-windows.zip"


def test_cli_flags_toggle_patches():
    from qupdatetool.cli import build_parser, collect_overrides

    parser = build_parser()
    on = collect_overrides(parser.parse_args(["--patches"]), parser)
    both = collect_overrides(parser.parse_args(["--patches", "--no-patches"]), parser)

    assert on["patches"]["enabled"] is True
    assert both["patches"]["enabled"] is False


def test_make_patch_tool_writes_the_conventional_name(versions, tmp_path):
    import importlib.util

    old, new, _ = versions
    spec = importlib.util.spec_from_file_location(
        "make_patch", Path(__file__).resolve().parent.parent / "tools" / "make_patch.py"
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    code = tool.main(["--old", str(old), "--new", str(new), "--app", "App",
                      "--from", "v1.0.0", "--to", "v2.0.0", "--os", "windows",
                      "--arch", "x64", "--out", str(tmp_path / "out")])

    assert code == 0
    assert (tmp_path / "out" / "App-v2.0.0-windows-x64.from-v1.0.0.qpatch").is_file()

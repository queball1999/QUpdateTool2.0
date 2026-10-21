"""
Patch updates: replace only the files that changed between two releases.

A full update downloads the whole installer even when a release changed a
handful of files. A patch carries just those files, so an application whose
bundle is mostly unchanging runtime libraries downloads a fraction of the
size. Patches are optional: a release that publishes none, or a machine a
patch cannot be applied to, gets the full installer exactly as before.

A patch is a zip with a `.qpatch` suffix, published as an ordinary release
asset and therefore covered by the same signed SHA256SUMS.txt as everything
else. Its name says which version it applies to:

    MyApp-1.2.0-windows-x64.from-v1.1.0.qpatch

Inside it:

    patch.json          what changes, with SHA-256 hashes for every file
    files/<path>        the new contents of each added or replaced file

patch.json lists one operation per changed file - add, replace or delete -
with the hash the file must have before (so a patch is never applied over
the wrong version) and after (so the result is checked), plus the hash of
every file in the new version, so the whole install can be verified once the
patch is in.

Applying one patch is all or nothing, like an archive install: each file is
moved aside before it is replaced, and any failure, including a hash that
does not match afterwards, moves every original back. A chain of patches
(1.0 -> 1.1 -> 1.2) is applied one at a time, each verified before the next,
so a failure part way leaves a complete, consistent earlier version.

This module uses the standard library only, so the release pipeline can run
it to build patches without installing the updater's dependencies.
"""

from __future__ import annotations

import contextlib
import hashlib
import heapq
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import platforms
from . import version as version_module
from .errors import ExitCode, UpdaterError

PATCH_SUFFIX = ".qpatch"
MANIFEST_NAME = "patch.json"
FILES_DIR = "files/"
FORMAT = 1

OP_ADD = "add"
OP_REPLACE = "replace"
OP_DELETE = "delete"
OPS = (OP_ADD, OP_REPLACE, OP_DELETE)

VERIFY_FULL = "full"
VERIFY_CHANGED = "changed"

# "<stem>.from-<version>.qpatch". The version is the release tag the patch
# applies to, so it is read from the name without downloading the patch.
NAME_RE = re.compile(r"^(?P<stem>.+)\.from-(?P<from>[^/\\]+)\.qpatch$", re.IGNORECASE)

# Scratch folders created beside (or, failing that, inside) the install while
# a patch is applied. Removed when the patch finishes; a leftover one is
# swept at the start of the next patch.
WORK_PREFIX = ".qupdate-patch-"

CHUNK = 1024 * 1024


class PatchError(UpdaterError):
    """A patch could not be applied. The install is left as it was."""

    exit_code = ExitCode.INSTALL


# --- naming ---


def patch_asset_name(stem: str, from_version: str) -> str:
    """Build the asset name for a patch from `from_version` to the release named by `stem`."""
    return f"{stem}.from-{from_version}{PATCH_SUFFIX}"


def parse_patch_name(name: str) -> tuple | None:
    """Return (stem, from_version) for a patch asset name, or None for anything else."""
    match = NAME_RE.match(name or "")
    if not match:
        return None
    return match.group("stem"), match.group("from")


def is_patch_name(name: str) -> bool:
    return (name or "").lower().endswith(PATCH_SUFFIX)


def version_key(text: str) -> str:
    """
    Reduce a version or tag to the form used to match patches to releases.

    "v1.2.0-release" and "1.2.0" are the same build; "1.2.0-dev" is not, so
    only the "v" prefix and platform/channel words are dropped.
    """
    return version_module.strip_platform_suffix(str(text or "")).lower()


# --- hashing and trees ---


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_tree(root: Path, exclude: tuple = ()) -> dict:
    """Return {relative posix path: (sha256, size, mode)} for every file under root."""
    root = Path(root)
    files = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(Path(rel).match(pattern) for pattern in exclude):
            continue
        info = path.stat()
        files[rel] = (sha256_file(path), info.st_size, stat.S_IMODE(info.st_mode))
    return files


@contextlib.contextmanager
def open_tree(source: str | os.PathLike):
    """
    Yield a directory holding one version's files.

    `source` is a folder, or a zip of one (a portable release). A zip with a
    single top-level folder is unwrapped, the same as an archive install.
    """
    source = Path(source)
    if source.is_dir():
        yield source
        return

    if not zipfile.is_zipfile(source):
        raise PatchError(f"{source} is neither a folder nor a zip archive")

    with tempfile.TemporaryDirectory(prefix="qupdate-tree-") as scratch:
        root = Path(scratch)
        with zipfile.ZipFile(source) as archive:
            check_member_paths(archive.namelist())
            archive.extractall(root)
        entries = list(root.iterdir())
        yield entries[0] if len(entries) == 1 and entries[0].is_dir() else root


def check_member_paths(names) -> None:
    """Refuse absolute paths and ".." entries; a patch must stay inside the install."""
    for name in names:
        normalised = str(name).replace("\\", "/")
        if normalised.startswith("/") or (len(normalised) > 1 and normalised[1] == ":"):
            raise PatchError("The patch contains an absolute path", f"refusing {name}")
        if any(part == ".." for part in normalised.split("/")):
            raise PatchError("The patch contains a path traversal entry", f"refusing {name}")


# --- creating ---


@dataclass
class PatchSummary:
    """What create_patch wrote."""

    path: Path
    added: int = 0
    replaced: int = 0
    deleted: int = 0
    unchanged: int = 0
    size: int = 0

    def describe(self) -> str:
        return (
            f"{self.path.name}: {self.added} added, {self.replaced} replaced, "
            f"{self.deleted} deleted, {self.unchanged} unchanged, {self.size} bytes"
        )


def create_patch(old_root: Path, new_root: Path, out_path: Path, *, app: str,
                 from_version: str, to_version: str, os_family: str = "",
                 arch: str = "", exclude: tuple = ()) -> PatchSummary:
    """Write a patch that turns the files in `old_root` into those in `new_root`."""
    old_files = hash_tree(old_root, exclude)
    new_files = hash_tree(new_root, exclude)

    ops = []
    summary = PatchSummary(path=Path(out_path))

    for rel, (digest, size, mode) in new_files.items():
        previous = old_files.get(rel)
        if previous is None:
            ops.append({"op": OP_ADD, "path": rel, "sha256": digest, "size": size, "mode": mode})
            summary.added += 1
        elif previous[0] != digest:
            ops.append({"op": OP_REPLACE, "path": rel, "sha256": digest, "size": size,
                        "mode": mode, "old_sha256": previous[0]})
            summary.replaced += 1
        else:
            summary.unchanged += 1

    for rel, (digest, _size, _mode) in old_files.items():
        if rel not in new_files:
            ops.append({"op": OP_DELETE, "path": rel, "old_sha256": digest})
            summary.deleted += 1

    manifest = {
        "format": FORMAT,
        "app": app,
        "from_version": from_version,
        "to_version": to_version,
        "os": os_family,
        "arch": arch,
        "ops": ops,
        "target": {rel: digest for rel, (digest, _size, _mode) in new_files.items()},
    }

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_name(out_path.name + ".partial")

    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(manifest, indent=1, sort_keys=True))
        for op in ops:
            if op["op"] != OP_DELETE:
                archive.write(Path(new_root) / op["path"], FILES_DIR + op["path"])

    os.replace(partial, out_path)
    summary.size = out_path.stat().st_size
    return summary


# --- reading ---


@dataclass
class PatchManifest:
    app: str
    from_version: str
    to_version: str
    os_family: str
    arch: str
    ops: list
    target: dict


def read_manifest(archive: zipfile.ZipFile) -> PatchManifest:
    """Read and validate patch.json from an open patch archive."""
    check_member_paths(archive.namelist())

    try:
        data = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
    except KeyError as exc:
        raise PatchError(f"The patch has no {MANIFEST_NAME}") from exc
    except ValueError as exc:
        raise PatchError(f"{MANIFEST_NAME} is not valid JSON", str(exc)) from exc

    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise PatchError(
            "Unsupported patch format",
            f"format={data.get('format') if isinstance(data, dict) else '?'}; "
            f"this updater reads format {FORMAT}",
        )

    ops = data.get("ops")
    target = data.get("target")
    if not isinstance(ops, list) or not isinstance(target, dict):
        raise PatchError(f"{MANIFEST_NAME} is missing ops or target")

    for op in ops:
        if not isinstance(op, dict) or op.get("op") not in OPS or not op.get("path"):
            raise PatchError(f"{MANIFEST_NAME} has an invalid operation", repr(op)[:200])
    check_member_paths(op["path"] for op in ops)
    check_member_paths(target)

    return PatchManifest(
        app=str(data.get("app", "")),
        from_version=str(data.get("from_version", "")),
        to_version=str(data.get("to_version", "")),
        os_family=str(data.get("os", "")),
        arch=str(data.get("arch", "")),
        ops=ops,
        target=target,
    )


# --- applying ---


@dataclass
class ApplyResult:
    """The outcome of one applied patch."""

    from_version: str
    to_version: str
    written: int = 0
    deleted: int = 0
    verified: int = 0


def make_work_dir(target: Path) -> Path:
    """
    Create the scratch folder for one patch.

    Beside the install where possible: staged files must be on the same
    volume as the install so they can be renamed into place, and keeping them
    out of the install means nothing extra is ever visible inside it - an
    application that checks its own folder for planted files would otherwise
    find the moved-aside copy of a running executable there. Inside the
    install is the fallback when its parent is not writable.
    """
    for parent in (target.parent, target):
        try:
            return Path(tempfile.mkdtemp(prefix=f"{WORK_PREFIX}{target.name}-", dir=parent))
        except OSError:
            continue
    raise PatchError(f"Cannot write next to or inside {target}")


def remove_stale_work_dirs(target: Path, log=None) -> None:
    """Delete scratch folders an earlier patch left, typically holding a then-running executable."""
    patterns = ((target.parent, f"{WORK_PREFIX}{target.name}-*"), (target, f"{WORK_PREFIX}*"))
    for directory, pattern in patterns:
        try:
            entries = list(directory.glob(pattern))
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
                if log and entry.exists():
                    log(f"Could not remove old patch folder {entry}")


class Transaction:
    """
    File changes to an install that can be undone as a unit.

    Every file about to be replaced or deleted is first moved into a backup
    folder (a rename, so even a running executable can be moved on Windows),
    and every file and folder created is recorded. rollback() puts it all
    back; commit() discards the backups.
    """

    def __init__(self, target: Path, work_dir: Path):
        self.target = target
        self.backup_root = work_dir / "backup"
        self.replaced = []        # (destination, backup)
        self.created_files = []
        self.created_dirs = []

    def _move_aside(self, destination: Path, rel: str) -> None:
        backup = self.backup_root / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.replace(destination, backup)
        self.replaced.append((destination, backup))

    def _make_dirs(self, directory: Path) -> None:
        missing = []
        current = directory
        while current != self.target and not current.is_dir():
            missing.append(current)
            current = current.parent
        for folder in reversed(missing):
            folder.mkdir()
            self.created_dirs.append(folder)

    def put(self, rel: str, staged: Path) -> None:
        destination = self.target / rel
        self._make_dirs(destination.parent)
        if destination.exists() or destination.is_symlink():
            self._move_aside(destination, rel)
        else:
            self.created_files.append(destination)
        os.replace(staged, destination)

    def remove(self, rel: str) -> None:
        destination = self.target / rel
        if destination.exists() or destination.is_symlink():
            self._move_aside(destination, rel)

    def rollback(self) -> list:
        """Undo every change; return the originals that could not be put back."""
        for path in reversed(self.created_files):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)

        unrestored = []
        for destination, backup in reversed(self.replaced):
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, destination)
            except OSError:
                unrestored.append(destination)

        for folder in reversed(self.created_dirs):
            with contextlib.suppress(OSError):
                folder.rmdir()

        return unrestored


def check_before(target: Path, manifest: PatchManifest) -> None:
    """
    Confirm the install is the version this patch was made from.

    Only the files the patch touches are checked here; the full install is
    verified against the target list after the patch is in.
    """
    for op in manifest.ops:
        expected = op.get("old_sha256")
        if not expected:
            continue
        path = target / op["path"]
        if not path.is_file():
            if op["op"] == OP_DELETE:
                continue
            raise PatchError(
                f"The installed files do not match {manifest.from_version}",
                f"missing: {op['path']}",
            )
        if sha256_file(path) != expected:
            raise PatchError(
                f"The installed files do not match {manifest.from_version}",
                f"changed: {op['path']}",
            )


def stage_files(archive: zipfile.ZipFile, manifest: PatchManifest, stage_dir: Path) -> dict:
    """Extract every new file to the stage folder, checking each hash. Returns {rel: staged path}."""
    staged = {}
    for index, op in enumerate(manifest.ops):
        if op["op"] == OP_DELETE:
            continue
        member = FILES_DIR + op["path"]
        destination = stage_dir / str(index)
        digest = hashlib.sha256()
        try:
            with archive.open(member) as source, open(destination, "wb") as sink:
                for chunk in iter(lambda: source.read(CHUNK), b""):
                    digest.update(chunk)
                    sink.write(chunk)
        except KeyError as exc:
            raise PatchError("The patch is incomplete", f"missing {member}") from exc
        if digest.hexdigest() != op.get("sha256"):
            raise PatchError("A file in the patch is corrupt", op["path"])
        mode = op.get("mode")
        if mode and os.name != "nt":
            os.chmod(destination, int(mode))
        staged[op["path"]] = destination
    return staged


def check_after(target: Path, manifest: PatchManifest, scope: str) -> int:
    """Hash the patched install against the patch's target list. Returns the number checked."""
    if scope == VERIFY_CHANGED:
        expected = {op["path"]: op["sha256"] for op in manifest.ops if op["op"] != OP_DELETE}
    else:
        expected = manifest.target

    for rel, digest in expected.items():
        path = target / rel
        if not path.is_file():
            raise PatchError(f"Verification after patching failed: missing {rel}")
        if sha256_file(path) != digest:
            raise PatchError(f"Verification after patching failed: {rel} does not match")

    if scope != VERIFY_CHANGED:
        for op in manifest.ops:
            if op["op"] == OP_DELETE and (target / op["path"]).exists():
                raise PatchError(f"Verification after patching failed: {op['path']} was not removed")

    return len(expected)


def apply_patch(patch_path: Path, target: Path, *, expected_from: str = "",
                expected_to: str = "", verify: str = VERIFY_FULL, log=None) -> ApplyResult:
    """
    Apply one verified patch to the install in `target`, all or nothing.

    The sequence: read and validate patch.json, confirm the install is the
    version the patch was made from, extract and hash every new file, swap
    them in, then hash the result. A failure at any step leaves the install
    exactly as it was and raises PatchError.
    """
    log = log or (lambda message: None)
    target = Path(target)

    if not target.is_dir():
        raise PatchError(f"Install folder not found: {target}")

    try:
        archive = zipfile.ZipFile(patch_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PatchError("The patch is not a valid archive", str(exc)) from exc

    with archive:
        manifest = read_manifest(archive)

        if expected_from and version_key(manifest.from_version) != version_key(expected_from):
            raise PatchError(
                "The patch is for a different version",
                f"it applies to {manifest.from_version}, the install is {expected_from}",
            )
        if expected_to and version_key(manifest.to_version) != version_key(expected_to):
            raise PatchError(
                "The patch does not match its release",
                f"it produces {manifest.to_version}, expected {expected_to}",
            )

        check_before(target, manifest)
        log(f"Install matches {manifest.from_version}")

        remove_stale_work_dirs(target, log)
        work_dir = make_work_dir(target)
        stage_dir = work_dir / "stage"
        stage_dir.mkdir()

        try:
            staged = stage_files(archive, manifest, stage_dir)
            transaction = Transaction(target, work_dir)
            result = ApplyResult(manifest.from_version, manifest.to_version)

            try:
                for op in manifest.ops:
                    if op["op"] == OP_DELETE:
                        transaction.remove(op["path"])
                        result.deleted += 1
                    else:
                        transaction.put(op["path"], staged[op["path"]])
                        result.written += 1
                result.verified = check_after(target, manifest, verify)
            except (OSError, PatchError) as exc:
                unrestored = transaction.rollback()
                if unrestored:
                    kept = work_dir.with_name(work_dir.name + "-restore")
                    with contextlib.suppress(OSError):
                        os.replace(work_dir, kept)
                        work_dir = kept
                    raise PatchError(
                        "The patch failed and the previous version could not be fully restored",
                        f"{exc}. Originals of {len(unrestored)} files are in {work_dir / 'backup'}",
                    ) from exc
                if isinstance(exc, PatchError):
                    raise PatchError(exc.message, f"{exc.detail} The previous version is intact.".strip()) from exc
                name = Path(exc.filename).name if getattr(exc, "filename", None) else "a file"
                raise PatchError(
                    f"Could not replace {name}",
                    f"{exc}. Nothing was changed; the previous version is intact.",
                ) from exc
        except BaseException:
            if not work_dir.name.endswith("-restore"):
                shutil.rmtree(work_dir, ignore_errors=True)
            raise

    # A moved-aside executable that is still running cannot be deleted yet;
    # the next patch sweeps whatever is left.
    shutil.rmtree(work_dir, ignore_errors=True)
    log(
        f"Patched {manifest.from_version} -> {manifest.to_version}: "
        f"{result.written} written, {result.deleted} deleted, {result.verified} verified"
    )
    return result


# --- planning ---


@dataclass
class PatchStep:
    """One patch in a chain: the release it belongs to and the asset to download."""

    release: object
    asset: object
    from_version: str
    to_version: str

    def to_dict(self) -> dict:
        return {
            "from": self.from_version,
            "to": self.to_version,
            "asset": self.asset.name,
            "size": self.asset.size,
        }


@dataclass
class PatchPlan:
    steps: list = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(step.asset.size or 0 for step in self.steps)


def patch_assets(release, os_family: str, arch: str, pattern: str = "") -> dict:
    """Return {version_key(from): asset} for the patches in a release that suit this host."""
    matcher = re.compile(pattern) if pattern else None
    found = {}
    for asset in release.assets:
        parsed = parse_patch_name(asset.name)
        if not parsed:
            continue
        stem, from_version = parsed
        if matcher and not matcher.search(asset.name):
            continue
        if not platforms.asset_matches_os(stem, os_family):
            continue
        if not platforms.asset_matches_arch(stem, arch):
            continue
        found.setdefault(version_key(from_version), asset)
    return found


def plan_chain(releases, current: str, latest, os_family: str, arch: str, *,
               pattern: str = "", max_steps: int = 10) -> PatchPlan | None:
    """
    Find the patches that take `current` to `latest`, or None if there is no way.

    `releases` are the releases this machine may install (channel and tag
    filters already applied). Each patch is an edge from the version it was
    made from to the release it belongs to; the route chosen has the fewest
    patches, then the smallest download. Only releases between the current
    version and the latest are used, so a chain never passes through a build
    this machine would not otherwise be offered.
    """
    start = version_key(current)
    goal = version_key(latest.tag)
    if not start or start == goal:
        return None

    by_key = {}
    for release in releases:
        if getattr(release, "draft", False):
            continue
        if not version_module.is_newer(release.tag, current):
            continue
        if version_module.is_newer(release.tag, latest.tag):
            continue
        by_key.setdefault(version_key(release.tag), release)
    by_key.setdefault(goal, latest)

    edges = {}  # from key -> [(to key, release, asset)]
    for key, release in by_key.items():
        for from_key, asset in patch_assets(release, os_family, arch, pattern).items():
            edges.setdefault(from_key, []).append((key, release, asset))

    # Dijkstra over (number of patches, bytes). The counter breaks ties so
    # the heap never has to compare two paths.
    counter = 0
    queue = [(0, 0, counter, start, [])]
    seen = set()
    while queue:
        steps, size, _, node, path = heapq.heappop(queue)
        if node == goal:
            return PatchPlan(steps=path)
        if node in seen or steps >= max_steps:
            continue
        seen.add(node)
        for to_key, release, asset in edges.get(node, ()):
            if to_key in seen:
                continue
            from_label = path[-1].to_version if path else current
            step = PatchStep(release, asset, from_label, release.tag)
            counter += 1
            heapq.heappush(
                queue, (steps + 1, size + (asset.size or 0), counter, to_key, path + [step])
            )

    return None

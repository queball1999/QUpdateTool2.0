#!/usr/bin/env python3
"""
Build a branded updater binary for one application.

This is the script a consuming project calls from CI. It takes a brand YAML
file and an icon, compiles the brand into the package as a generated module,
runs PyInstaller, and writes the resulting binary plus a .sha256 sidecar.

    python tools/build_branded.py \
        --brand ../MyApp/updater-brand.yaml \
        --icon  ../MyApp/assets/icons/MyApp.ico \
        --key   ../MyApp/gpg-public.asc \
        --out   ../MyApp/output/windows/updater.exe

The .sha256 sidecar matters for the security model: the consuming project
records that hash and verifies it before launching the updater, which is the
only non-circular way to detect a tampered updater binary. See
qupdatetool/integrity.py for why a self-check cannot do this job.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from qupdatetool.brand import (
    compute_brand_hash,
    load_brand_file,
    locked_fields,
)

GENERATED_MODULE = REPO_ROOT / "qupdatetool" / "_brand_data.py"

GENERATED_HEADER = '''"""
Brand payload compiled into this binary at build time.

GENERATED FILE - DO NOT EDIT AND DO NOT COMMIT.
Written by tools/build_branded.py; recreated on every branded build.
"""

'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a branded QUpdateTool binary for one application",
    )

    parser.add_argument("--brand", required=True, metavar="PATH",
                        help="Brand YAML file describing the application")
    parser.add_argument("--icon", metavar="PATH",
                        help="Icon for the binary (.ico on Windows, .icns on macOS)")
    parser.add_argument("--key", metavar="PATH",
                        help="ASCII-armored OpenPGP public key to pin into the build")
    parser.add_argument("--out", metavar="PATH",
                        help="Where to write the finished binary")
    parser.add_argument("--name", metavar="NAME", default="updater",
                        help="Base name of the produced binary")
    parser.add_argument("--version", metavar="VERSION", default="",
                        help="Build version stamp (defaults to the package version)")
    parser.add_argument("--commit", metavar="SHA", default="",
                        help="Source commit stamp (defaults to git rev-parse)")
    parser.add_argument("--gui", action="store_true",
                        help="Bundle PySide6 so the binary supports --gui")
    parser.add_argument("--console", dest="console", action="store_true", default=None,
                        help="Force a console-subsystem build. Default: on for a "
                             "headless (--gui not passed) build, off for a --gui build.")
    parser.add_argument("--no-console", dest="console", action="store_false",
                        help="Force a windowed-subsystem build (no console at all)")
    parser.add_argument("--onefile", action="store_true", default=True,
                        help="Produce a single-file binary (default)")
    parser.add_argument("--sign-command", metavar="CMD", default="",
                        help="Command to code sign the binary; {file} is substituted. "
                             "An OS-level signature is the strongest tamper defence.")
    parser.add_argument("--clean", action="store_true",
                        help="Remove build artifacts before building")
    parser.add_argument("--keep-generated", action="store_true",
                        help="Leave the generated brand module in place afterwards")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate the brand module and print the plan, but do not build")

    args = parser.parse_args()

    if args.console is None:
        # A console-subsystem binary always gets a console window from
        # Windows on some launch paths no matter what creation flags the
        # caller passes: ShellExecuteExW's "runas" verb (used for UAC
        # elevation) offers no equivalent of DETACHED_PROCESS or
        # CREATE_NO_WINDOW, so an elevated update always flashes a console
        # unless the binary itself carries no console subsystem at all. A
        # --gui build's whole point is to show a Qt window instead, so it
        # defaults to windowed; a headless build keeps its console so a
        # developer running it interactively from a terminal still sees
        # status output without redirecting it.
        args.console = not args.gui

    return args


def git_commit() -> str:
    """Return the short commit hash of this checkout, or "" if unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=30, check=False,
        )
        return (result.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def load_public_key(brand: dict, key_path: str) -> str:
    """
    Resolve the public key to pin into the build.

    An explicit --key wins over anything in the brand file, because CI
    normally holds the canonical key alongside the application source.
    """
    if key_path:
        path = Path(key_path).expanduser()
        if not path.is_file():
            raise SystemExit(f"Public key file not found: {path}")
        return path.read_text(encoding="utf-8")

    inline = (brand.get("security") or {}).get("public_key", "")
    if inline:
        return inline

    key_file = (brand.get("security") or {}).get("public_key_file", "")
    if key_file:
        path = (Path(key_file) if Path(key_file).is_absolute()
                else Path(key_path or ".").parent / key_file)
        if path.is_file():
            return path.read_text(encoding="utf-8")

    return ""


def validate_brand(brand: dict, key_supplied: bool = False) -> list:
    """
    Check a brand for the mistakes that produce a broken or unsafe updater.

    Returns a list of warnings. Anything that would make the build actively
    unsafe raises instead, because shipping it would be worse than failing CI.

    `key_supplied` reflects whether --key was passed, since a brand file that
    carries no inline key is perfectly correct when CI supplies one.
    """
    warnings = []

    app = brand.get("app") or {}
    source = brand.get("source") or {}
    security = brand.get("security") or {}

    if not app.get("name"):
        raise SystemExit("Brand error: app.name is required")

    provider = (source.get("provider") or "").lower()
    if not provider:
        warnings.append("source.provider is not set; the updater will default to github")

    if provider != "generic" and not source.get("repo"):
        raise SystemExit("Brand error: source.repo is required for a git provider")

    if provider in ("gitea", "forgejo") and not source.get("host"):
        raise SystemExit(f"Brand error: source.host is required for the {provider} provider")

    require_signature = security.get("require_signature", True)
    has_key = bool(
        key_supplied or security.get("public_key") or security.get("public_key_file")
    )

    if require_signature and not has_key:
        warnings.append(
            "security.require_signature is on but the brand carries no key; "
            "pass --key so the binary pins one"
        )

    if not require_signature:
        warnings.append(
            "security.require_signature is DISABLED in this brand - the updater "
            "will install unsigned releases"
        )

    if source.get("token"):
        raise SystemExit(
            "Brand error: source.token must not be baked into a binary. "
            "Use source.token_env and supply the token at runtime."
        )

    if source.get("verify_tls") is False:
        raise SystemExit("Brand error: source.verify_tls must not be disabled in a brand")

    return warnings


def write_brand_module(brand: dict, public_key: str, args) -> str:
    """
    Write the generated module carrying the brand payload.

    The payload is emitted as a literal dict rather than a serialised blob so
    that anyone auditing a build can read exactly what was compiled in.
    """
    payload = json.loads(json.dumps(brand, default=str))  # deep copy, plain types

    if public_key:
        payload.setdefault("security", {})["public_key"] = public_key
        # A pinned inline key makes a file reference meaningless and would
        # otherwise point at a path that does not exist on the target machine.
        payload["security"].pop("public_key_file", None)

    brand_hash = compute_brand_hash(payload)
    version = args.version or read_package_version()
    commit = args.commit or git_commit()
    built = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        GENERATED_HEADER,
        f"BRAND_NAME = {json.dumps(payload.get('app', {}).get('name', ''))}\n",
        f"BUILD_VERSION = {json.dumps(version)}\n",
        f"BUILD_DATE = {json.dumps(built)}\n",
        f"BUILD_COMMIT = {json.dumps(commit)}\n",
        f"BRAND_SHA256 = {json.dumps(brand_hash)}\n",
        "\n",
        f"BRAND = {format_payload(payload)}\n",
    ]

    GENERATED_MODULE.write_text("".join(lines), encoding="utf-8")

    print(f"Generated {GENERATED_MODULE.relative_to(REPO_ROOT)}")
    print(f"  brand name : {payload.get('app', {}).get('name', '')}")
    print(f"  brand hash : {brand_hash}")
    print(f"  build      : {version} ({commit}) {built}")
    print(f"  pinned key : {'yes' if public_key else 'NO'}")
    locked = locked_fields(payload)
    print(f"  locked     : {len(locked)} fields frozen against config/flag override")
    for path in locked:
        print(f"               - {path}")

    return brand_hash


def format_payload(payload: dict) -> str:
    """Render the brand dict as readable, auditable Python source."""
    import pprint

    return pprint.pformat(payload, indent=4, width=100, sort_dicts=True)


def read_package_version() -> str:
    """Read __version__ out of the package without importing it."""
    init_file = REPO_ROOT / "qupdatetool" / "__init__.py"

    for line in init_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")

    return "0.0.1"


def build_pyinstaller_command(args, icon: Path | None) -> list:
    """Assemble the PyInstaller command line."""
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", args.name,
        "--distpath", str(REPO_ROOT / "dist"),
        "--workpath", str(REPO_ROOT / "build"),
        "--specpath", str(REPO_ROOT / "build"),
    ]

    if args.onefile:
        command.append("--onefile")

    if args.console:
        command.append("--console")
    else:
        command.append("--windowed")

    if icon and icon.is_file():
        command += ["--icon", str(icon)]

    # requests probes for a charset detector at import time and warns loudly
    # when PyInstaller has not collected one; certifi carries the CA bundle
    # that TLS verification depends on.
    # collect-all rather than hidden-import: charset_normalizer ships
    # compiled mypyc submodules that a plain hidden-import misses, leaving
    # requests to warn about a missing charset detector on every run.
    command += ["--collect-all", "charset_normalizer",
                "--collect-data", "certifi",
                "--hidden-import", "certifi"]

    # PySide6 is enormous; excluding it keeps a headless updater small, which
    # matters when the binary ships inside every application release.
    if args.gui:
        command += ["--hidden-import", "PySide6.QtCore",
                    "--hidden-import", "PySide6.QtGui",
                    "--hidden-import", "PySide6.QtWidgets"]
    else:
        command += ["--exclude-module", "PySide6",
                    "--exclude-module", "shiboken6",
                    "--exclude-module", "tkinter",
                    "--exclude-module", "matplotlib",
                    "--exclude-module", "numpy"]

    # The entry script must live outside the package: PyInstaller loads it
    # as a top-level module, and __main__.py relative imports would break.
    command += ["--paths", str(REPO_ROOT)]
    command.append(str(REPO_ROOT / "updater_main.py"))

    return command


def sign_binary(path: Path, sign_command: str) -> None:
    """
    Run the configured code-signing command against the built binary.

    Code signing is the one integrity mechanism an attacker who rewrites the
    binary cannot forge, so this is the most valuable optional step here.
    """
    command = sign_command.replace("{file}", str(path))

    print(f"Signing: {command}")

    result = subprocess.run(command, shell=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"Code signing failed with exit code {result.returncode}")

    print("Signed successfully")


def write_sidecar(binary: Path) -> str:
    """
    Write "<binary>.sha256" and return the digest.

    The consuming application records this value and checks it before
    launching the updater.
    """
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    sidecar = binary.with_name(binary.name + ".sha256")
    sidecar.write_text(f"{digest}  {binary.name}\n", encoding="utf-8")

    print(f"SHA-256: {digest}")
    print(f"Wrote {sidecar}")

    return digest


def main() -> int:
    args = parse_args()

    brand = load_brand_file(args.brand)

    warnings = validate_brand(brand, key_supplied=bool(args.key))
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)

    icon = Path(args.icon).expanduser() if args.icon else None
    if icon and not icon.is_file():
        print(f"WARNING: icon not found: {icon}", file=sys.stderr)
        icon = None

    if icon:
        # The bytes are embedded inline, not just the filename or the
        # build-time path. --icon points at a file in the CONSUMING project's
        # source tree (e.g. ../MyApp/assets/icons/MyApp.ico), which does
        # not exist on an end user's machine, and PyInstaller's own --icon
        # flag only sets the .exe's file-explorer icon resource - it does not
        # make the source file available to the running program. Embedding
        # the bytes, the same way the public key is embedded rather than
        # referenced by path, is what lets the window actually show the icon
        # instead of falling back to Qt's generic default.
        app_section = brand.setdefault("app", {})
        app_section["icon"] = icon.name
        app_section["icon_format"] = icon.suffix.lstrip(".").lower() or "ico"
        app_section["icon_data"] = base64.b64encode(icon.read_bytes()).decode("ascii")

    public_key = load_public_key(brand, args.key or "")

    brand_hash = write_brand_module(brand, public_key, args)

    if args.dry_run:
        print("\nDry run: brand module generated, build skipped.")
        return 0

    if args.clean:
        for directory in (REPO_ROOT / "build", REPO_ROOT / "dist"):
            shutil.rmtree(directory, ignore_errors=True)

    command = build_pyinstaller_command(args, icon)

    print("\nBuilding:")
    print("  " + " ".join(command))

    try:
        result = subprocess.run(command, cwd=REPO_ROOT, check=False)
    except OSError as exc:
        raise SystemExit(f"Could not run PyInstaller: {exc}") from exc

    if result.returncode != 0:
        raise SystemExit(f"PyInstaller failed with exit code {result.returncode}")

    suffix = ".exe" if sys.platform.startswith("win") else ""
    built = REPO_ROOT / "dist" / f"{args.name}{suffix}"

    if not built.is_file():
        raise SystemExit(f"Expected binary was not produced: {built}")

    if args.sign_command:
        sign_binary(built, args.sign_command)

    if args.out:
        destination = Path(args.out).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built, destination)
        print(f"Copied to {destination}")
        built = destination

    digest = write_sidecar(built)

    if not args.keep_generated:
        # The generated module holds one application brand; leaving it behind
        # would silently brand the next build from this checkout.
        GENERATED_MODULE.unlink(missing_ok=True)
        print(f"Removed {GENERATED_MODULE.name}")

    print("\nBuild complete")
    print(f"  binary     : {built}")
    print(f"  sha256     : {digest}")
    print(f"  brand hash : {brand_hash}")
    print("\nRecord the sha256 above in the application build info and verify it")
    print("before launching the updater. See docs/SECURITY.md.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

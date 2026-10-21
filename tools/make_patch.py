#!/usr/bin/env python3
"""
Build a .qpatch that turns one release's files into the next one's.

    python tools/make_patch.py \
        --old MyApp-1.1.0-windows-portable.zip \
        --new MyApp-1.2.0-windows-portable.zip \
        --app MyApp --from v1.1.0 --to v1.2.0 \
        --os windows --arch x64 \
        --out dist/

--old and --new are folders or zips of the installed files (a portable zip is
exactly that). --out is a file, or a folder to write the conventionally named
patch into:

    <stem>.from-<from>.qpatch     stem defaults to <app>-<to>-<os>-<arch>

Publish the patch as a release asset beside the full installer and list it
in the signed SHA256SUMS.txt like any other asset; the updater refuses a
patch the manifest does not cover. --from must be the exact tag (or version)
the updater is told is installed, since that is how it finds the patch.

Standard library only, so a release job can run it from a plain checkout.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qupdatetool import patch


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--old", required=True, help="Folder or zip of the version patched from")
    parser.add_argument("--new", required=True, help="Folder or zip of the version patched to")
    parser.add_argument("--app", required=True, help="Application name, recorded in the patch")
    parser.add_argument("--from", dest="from_version", required=True,
                        help="Tag or version of --old, exactly as the updater sees it")
    parser.add_argument("--to", dest="to_version", required=True,
                        help="Tag of the release the patch is published on")
    parser.add_argument("--os", dest="os_family", default="",
                        help="windows, linux or macos; used in the name and recorded")
    parser.add_argument("--arch", default="", help="e.g. x64; used in the name and recorded")
    parser.add_argument("--stem", default="",
                        help="Name before .from-<from>.qpatch (default <app>-<to>-<os>-<arch>)")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="Relative path pattern to leave out of the patch; may be repeated")
    parser.add_argument("--out", required=True, help="Output file, or folder for the default name")
    args = parser.parse_args(argv)

    out = Path(args.out)
    if out.suffix.lower() != patch.PATCH_SUFFIX:
        stem = args.stem or "-".join(
            part for part in (args.app, args.to_version, args.os_family, args.arch) if part
        )
        out = out / patch.patch_asset_name(stem, args.from_version)

    try:
        with patch.open_tree(args.old) as old_root, patch.open_tree(args.new) as new_root:
            summary = patch.create_patch(
                old_root, new_root, out,
                app=args.app,
                from_version=args.from_version,
                to_version=args.to_version,
                os_family=args.os_family,
                arch=args.arch,
                exclude=tuple(args.exclude),
            )
    except patch.PatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(summary.describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# QUpdateTool

A universal updater for desktop applications. One tool, branded per
application at build time, that checks a git release backend for a newer
version, verifies its OpenPGP signature, installs it using the right
mechanism for the host platform, and restarts the application.

Built to replace the per-application updaters in QUpdateTool 1.x and
`QVMS/updater_ui.py` with a single reusable component.

Full docs: **[QUpdateTool wiki](https://github.com/queball1999/QUpdateTool2.0/wiki)**.

## What it does

- **Runs headless by default**, with a [Qt progress window](https://github.com/queball1999/QUpdateTool2.0/wiki/GUI) when `--gui` is passed
- **Silent or interactive installs** - unattended, or the user walks through the real vendor installer
- **[Multiple git backends](https://github.com/queball1999/QUpdateTool2.0/wiki/BACKENDS)** - GitHub, GitHub Enterprise, Gitea, Forgejo, GitLab, or a plain JSON manifest
- **Public and private repositories** via access tokens
- **Enforced OpenPGP signature verification** with no GnuPG dependency (see [SECURITY.md](SECURITY.md))
- **[Cross-platform installs](https://github.com/queball1999/QUpdateTool2.0/wiki/PLATFORMS)** - Windows installer/MSI/zip, Linux deb/rpm/AppImage/tarball, macOS dmg/pkg/zip
- **Stops and restarts the parent application** cleanly, including waiting out Windows file locks
- **[Release notes](https://github.com/queball1999/QUpdateTool2.0/wiki/RELEASE_NOTES)** pulled from a `notices/` folder, a CHANGELOG, or the release body
- **Configured by flags, a `config.yaml`, or a baked-in brand** - or any mix
- **Graphical setup wizard** (`--setup`) - builds a `config.yaml` and CI workflow interactively, with a live connection test and a demo of the update dialogs

## Quick start

```bash
pip install -r requirements.txt

# Is there an update? (exit 10 = yes, 11 = no)
python -m qupdatetool --check-only \
    --provider github --repo owner/app \
    --app-name MyApp --current-version 1.2.3 \
    --pubkey-file release-key.asc

# Update, stopping and restarting the running app
python -m qupdatetool \
    --provider github --repo owner/app \
    --app-name MyApp --current-version 1.2.3 \
    --executable "C:/Program Files/MyApp/MyApp.exe" \
    --calling-pid 12345 \
    --pubkey-file release-key.asc

# Same thing, driven entirely by a config file
python -m qupdatetool --config config.yaml
```

## Configuration file

Anything you can pass as a flag can live in a `config.yaml` instead. Drop it
next to the updater binary and it is picked up automatically, or point at it
with `--config`. Flags override the file; the file overrides the brand -
except for brand-locked fields, which nothing can override.

See [`examples/config.yaml`](examples/config.yaml) for every available
setting, fully commented.

See [DEV_GUIDE](https://github.com/queball1999/QUpdateTool2.0/wiki/DEV_GUIDE)
for a full walkthrough of integrating this into an app, including CI/CD.

## Setup wizard

`updater --setup` opens a graphical wizard that walks through every
`config.yaml` section, validates it against your machine (paths, running
processes, and a real read-only connection check against your release
source), and lets you see the actual update dialogs fire - including
success, up-to-date, and failure states - without downloading or installing
anything. At the end it exports `config.yaml`, and optionally a brand file
plus a GitHub Actions workflow that builds a branded `updater.exe`, bundled
with a generated `README.md` covering the remaining manual steps.

```bash
pip install -r requirements-gui.txt   # needs PySide6, same as --gui
python -m qupdatetool --setup

# Edit an existing config instead of starting from scratch
python -m qupdatetool --setup --config config.yaml
```

It's a dev-time tool only - it has no effect on `--check-only`/`--gui`
runtime behaviour and writes nothing until you explicitly save from its
Export page.

The wizard keeps QUpdateTool's own settings (log level, log file, theme,
accent colour) in `%LOCALAPPDATA%\QUpdateTool\config.yaml`
(`~/.config/QUpdateTool/config.yaml` on Linux), created on first run, and
logs every step, check and save to `logs/setup.log` beside it. `--log-level`
and `--log-file` override both for one run. The update window follows the
same theme setting but never creates that file.

## How an application uses it

The intended shape: your application ships a branded `updater.exe` in its
install root and shells out to it.

```
1. App startup       updater --check-only --json    ->  "an update is available"
2. Help > Check      updater --gui                  ->  progress window
3. Updater           stops the app, installs, relaunches it
```

Exit codes are the contract:

| Code | Meaning |
|-----:|---------|
| 0 | Success, or already up to date |
| 1 | Unexpected, unclassified error |
| 2 | Usage or configuration error |
| 3 | Network error (backend unreachable) |
| 4 | **Verification failed** |
| 5 | Install failed |
| 6 | Cancelled by the user |
| 7 | Could not stop or relaunch the app |
| 8 | No usable release (backend reachable, nothing to install) |
| 10 | `--check-only`: an update is available |
| 11 | `--check-only`: already up to date |

Each code means exactly one thing, because the calling application turns it
into a message a user reads. Code 1 exists so an unexpected failure never has
to borrow a specific code and claim, for example, that an install broke when
nothing was installed. Codes 3 and 8 are separate for the same reason: a
project that has simply not cut a stable release yet should not be reported
as a broken network connection.

## Branding

A branded build bakes the application name, icon, release source, and trusted
signing key into the binary, so `updater.exe` needs no arguments and no config
file to do the right thing.

```bash
python tools/build_branded.py \
    --brand ../MyApp/updater-brand.yaml \
    --icon  ../MyApp/assets/MyApp.ico \
    --key   ../MyApp/release-key.asc \
    --out   ../MyApp/output/updater.exe
```

This writes `updater.exe` and `updater.exe.sha256`. **Record that hash in your
application and verify it before launching the updater** - see
[SECURITY.md](SECURITY.md) for why that matters.

See [`examples/qsnippet-brand.yaml`](examples/qsnippet-brand.yaml) for a
complete brand file, and [`examples/config.yaml`](examples/config.yaml) for
every available setting.

## Platform support

Artifact selection is decided at runtime on the user machine, from the OS, the
CPU architecture, and how the application was installed. A single release can
carry artifacts for every platform and each host picks the right one.

| Platform | Preferred order |
|----------|-----------------|
| Windows | installer `.exe`, `.msi`, `.zip` |
| Debian / Ubuntu | `.deb`, AppImage, tarball |
| Fedora / RHEL / SUSE | `.rpm`, AppImage, tarball |
| Running as an AppImage | AppImage, tarball |
| Other Linux | AppImage, tarball, then native packages |
| macOS | `.dmg`, `.pkg`, `.zip`, tarball |
| Raspberry Pi OS (Debian) | `.deb`, AppImage, tarball; `armv6` for armv6l, `armv7` then `armv6` for armv7l |

If a release does not yet publish a format, selection falls through to the
next one. An application shipping only `.deb` today keeps working unchanged
when it starts publishing `.rpm` and `.AppImage` tomorrow - no updater change
is needed, because the host detection already looks for them.

## Updating a systemd service on Linux (e.g. Raspberry Pi)

For a headless service such as PiStation on a Raspberry Pi Zero W, run the
updater from source as root (for example from a systemd oneshot unit). It
needs only Debian packages: `sudo apt install python3-requests python3-yaml
python3-psutil python3-cryptography` (cryptography 38 on bookworm is enough).
PySide6 is never imported without a display.

```yaml
app:
  name: PiStation
  current_version: "1.2.2"          # or pass --current-version
  service_manager: systemd          # stop/start with systemctl, not by PID
  service_unit: pistation.service
source:
  provider: github
  repo: owner/pistation
security:
  public_key_file: /etc/pistation/release-key.asc
install:
  elevate: never                    # never call pkexec/sudo; run as root instead
  target_dir: /usr/local/bin        # a tarball holding only `pistation` replaces that file
health:
  url: http://127.0.0.1/healthz     # HTTP 200 means the new version is healthy
  attempts: 3
  window_seconds: 60
```

```bash
sudo python3 -m qupdatetool --config /etc/pistation/updater.yaml
# or with flags only
sudo python3 -m qupdatetool --service-manager systemd --service-unit pistation.service     --no-elevate --target-dir /usr/local/bin --health-url http://127.0.0.1/healthz
```

What happens:

1. The release asset is chosen by architecture: `armv6` for a Zero W, `armv7`
   (or `armv6` if there is no armv7 asset) for a Zero 2 W on a 32-bit OS,
   `arm64` and `amd64` elsewhere, e.g. `pistation-v1.2.3-linux-armv6.tar.gz`.
2. It is downloaded and signature-verified before anything is touched.
3. `systemctl stop pistation.service`, then the tarball's single file is
   written to a temporary name in the same directory, fsynced, and renamed
   over `pistation`. The executable bit is kept, the previous binary stays as
   `pistation.old`, and no other file in `target_dir` is touched.
4. `systemctl start pistation.service`, then `health.url` is polled
   (`attempts` tries spread over `window_seconds`). HTTP 200 is success.
5. If it never answers 200, `pistation.old` is moved back, the service is
   restarted, the rollback is logged, and the updater exits 5. Rollback only
   works for single-file installs; archives with several files keep no backup.

With `install.elevate: never` an unwritable `target_dir` fails immediately with
exit code 2 and a message to run as root. A `systemctl` failure or timeout
exits 7. Unit names are validated (letters, digits, `: _ . @ -`, ending in
`.service`) and `systemctl` is run as a list, never through a shell.

`--check-only --json` prints these keys on stdout (exit 10 = update available,
11 = up to date): `update_available`, `current_version`, `latest_version`,
`tag`, `published_at`, `url`, `asset`, `asset_kind`, `asset_size`, `platform`,
`notes_title`, `notes`, `notes_source`, `reason`, `update_method`,
`download_size`, `patches`. Status and log lines go to stderr.

## Patch updates

Optional, per application (`patches.enabled` or `--patches`). Instead of the
full installer, the updater downloads only the files that changed, the way
Discord and Squirrel-based apps update.

1. **Publish** a patch with each release, made from the previous release:

   ```bash
   python tools/make_patch.py --old MyApp-1.1.0-portable.zip --new MyApp-1.2.0-portable.zip \
       --app MyApp --from v1.1.0 --to v1.2.0 --os windows --arch x64 --out dist/
   # -> dist/MyApp-v1.2.0-windows-x64.from-v1.1.0.qpatch
   ```

   Upload it beside the installer and include it in the signed `SHA256SUMS.txt`.
2. **Check**: `--check-only --json` reports `update_method` (`patch` or `full`),
   `download_size` and the `patches` chain.
3. **Update**: every patch in the chain is downloaded and signature-verified
   first. Then, one at a time: confirm the installed files match the patch's
   starting version, swap the changed files in, and hash the result. Each patch
   is all or nothing, so a failure leaves the last complete version.
4. **Fallback**: no patch route, a modified install, a read-only install
   folder, interactive mode or an AppImage all use the full installer instead
   (`patches.fallback_to_full: false` turns that into an error).

Patches replace whole files; no binary diffing. For a typical bundled app the
runtime libraries never change between releases, so the patch is mostly
the executable.

## Security

Signature verification is **enforced by default** and implemented in pure
Python on top of `cryptography`, so it works on machines with no GnuPG
installed. An updater that silently skips verification when `gpg` is missing
is worse than one with no verification at all, because it advertises a
guarantee it does not deliver.

The verification chain:

1. Download `SHA256SUMS.txt` and its detached signature
2. Verify that signature against the pinned public key
3. Match the downloaded artifact against the now-trusted checksum manifest

Any failure deletes the download and aborts. There is no warn-and-continue
path.

Supported: RFC 4880 v4 and RFC 9580 v6 signatures; RSA, Ed25519, and ECDSA
over P-256/P-384/P-521; SHA-256 and stronger. SHA-1 signatures are rejected.

Read [SECURITY.md](SECURITY.md) for the threat model, including an honest
account of what a binary can and cannot prove about its own integrity.

## Logging

Logging mirrors the applications this tool updates: the same level ladder
(`ERROR` / `WARNING` / `INFO` / `DEBUG`), the same format string, and the same
compressed log rotation, so an updater log and an application log interleave
cleanly by timestamp in a support bundle.

```bash
# Default per-OS location
python -m qupdatetool --log-level DEBUG

# Write into the application own log directory instead
python -m qupdatetool --log-file "C:/Users/me/AppData/Local/MyApp/logs"

# Console only
python -m qupdatetool --log-file none
```

`--log-file` accepts a file or a directory; given a directory it writes
`updater.log` inside it.

## Development

```bash
make venv       # create .venv and install dependencies
make test       # run the test suite
make lint       # ruff
make check      # a live --check-only run against a configured repo
make build      # build an unbranded binary
make clean
```

## Layout

```
qupdatetool/
  cli.py            flags, config merge, entry point
  updater.py        the update sequence
  config.py         layered configuration
  brand.py          build-time branding and locked fields
  integrity.py      binary hashing and code-signature checks
  openpgp.py        pure-Python OpenPGP verifier
  verify.py         the checksum + signature chain
  patch.py          .qpatch format: build, plan a chain, apply all or nothing
  download.py       streaming downloads with progress
  process.py        stopping and relaunching the parent app (PID or systemd)
  health.py         post-install HTTP health check
  elevate.py        UAC / pkexec / sudo elevation
  platforms.py      host detection and artifact preference
  notes.py          release notes from notices / changelog / release body
  logging_utils.py  logging, matching the host application setup
  backends/         github, gitea, forgejo, gitlab, generic
  install/          windows, linux, macos installers
  ui/               optional Qt progress window
  ui/setup/         the --setup wizard (config.yaml/brand/CI generator, demo mode)
tools/
  build_branded.py  branded binary builder
  make_patch.py     builds a .qpatch between two releases (stdlib only)
```

## Licence

See `LICENSE.txt`.

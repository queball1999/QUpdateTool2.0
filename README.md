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

If a release does not yet publish a format, selection falls through to the
next one. An application shipping only `.deb` today keeps working unchanged
when it starts publishing `.rpm` and `.AppImage` tomorrow - no updater change
is needed, because the host detection already looks for them.

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
  download.py       streaming downloads with progress
  process.py        stopping and relaunching the parent app
  elevate.py        UAC / pkexec / sudo elevation
  platforms.py      host detection and artifact preference
  notes.py          release notes from notices / changelog / release body
  logging_utils.py  logging, matching the host application setup
  backends/         github, gitea, forgejo, gitlab, generic
  install/          windows, linux, macos installers
  ui/               optional Qt progress window
tools/
  build_branded.py  branded binary builder
```

## Licence

See `LICENSE.txt`.

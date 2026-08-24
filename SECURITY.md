# Security model

An updater is an attractive target for attackers. If someone can trick it into
installing their code instead of the real update, they get to run whatever
they want on the user's machine, often with admin rights. This document sets
out what QUpdateTool defends against, what it does not, and why.

## The short version

| Threat | Defence | Strength |
|--------|---------|----------|
| Malicious or compromised release server | Pinned OpenPGP key; enforced signature verification | **Strong** |
| Man-in-the-middle on the download | TLS, plus signature verification that does not trust the transport | **Strong** |
| Tampered artifact on a legitimate host | Signed checksum manifest, verified before install | **Strong** |
| Attacker drops a `config.yaml` beside the binary | Brand-locked fields cannot be overridden | **Strong** |
| Attacker rewrites `updater.exe` itself | Parent-side hash check, code signature, install permissions | **Moderate**, see below |
| Attacker already has admin on the machine | None | **None** |

## 1. Release verification

This is the core defence, and the one that matters most.

Signature verification is **on by default** and implemented in pure Python on
top of `cryptography`. It does not shell out to GnuPG and does not require it
to be installed. That choice is deliberate: an updater that silently skips
verification when `gpg` is missing is worse than one with no verification at
all.

The chain:

1. Download `SHA256SUMS.txt` and its detached signature from the release.
2. Verify the signature against the public key **pinned into the binary at
   build time**.
3. Look the downloaded artifact up in the now-trusted manifest and compare
   SHA-256 hashes.

If a release publishes no checksum manifest, the updater falls back to
verifying a detached signature over the artifact itself.

**Any failure deletes the download and aborts.** There is no warn-and-continue
path, and a verification failure is never downgraded into a pass. The optional
`gpg` fallback exists only for key algorithms the built-in verifier does not
support (DSA); it cannot turn a bad signature into a good one.

Supported: RFC 4880 v4 and RFC 9580 v6 signatures; RSA (PKCS#1 v1.5), Ed25519
in both the legacy EdDSA and modern algorithm IDs, and ECDSA over NIST
P-256/P-384/P-521. SHA-256 and above.

Deliberately rejected:

- **SHA-1 and MD5 signatures.** Collision attacks against SHA-1 are practical.
- **DSA keys**, which are not implemented; the fallback path handles them.
- **Signatures that are not document signatures**, so a key certification
  cannot be replayed as if it were a release signature.

`allowed_fingerprints` adds a second lock: even if the pinned key block picks
up an extra subkey, only the listed fingerprints are accepted as signers.

## 2. The configuration trust boundary

The updater merges configuration from several layers:

```
built-in defaults
  <- brand baked into the binary
    <- config.yaml next to the binary
      <- environment variables
        <- command-line flags
          <- BRAND-LOCKED FIELDS reapplied last
```

That last step is the trust boundary, and the ordering is the point.

Without it, anyone able to write a `config.yaml` next to the binary could
repoint `source.repo` at their own release host and swap
`security.public_key` for their own key. The updater would then verify
perfectly against the attacker key and install their payload, possibly with
elevated privileges. On a per-user install, dropping a file next to the binary
needs no privileges at all.

So these fields are **locked** whenever a brand sets them, and reapplied after
every other layer:

```
security.public_key            source.provider
security.public_key_file       source.host
security.require_signature     source.repo
security.allowed_fingerprints  source.manifest_url
source.verify_tls              source.download_url
```

A brand can lock more fields with a `lock:` list, for example `source.channel`,
so a dropped-in config cannot quietly move users onto a prerelease channel.

The build script refuses to produce a brand that bakes in an access token or
disables TLS verification.

## 3. Integrity of the updater binary itself

This is where honesty matters more than reassurance.

**A program cannot meaningfully verify its own integrity.** Any check a binary
performs on itself can be removed by whoever modified that binary. Embedding a
hash of `updater.exe` inside `updater.exe` and comparing them at startup stops
nobody who is actually trying: they recompute the constant, or delete the
comparison. Self-verification of this kind is security theatre.

What works is verification by a **different** party. Three mechanisms, in
descending order of strength:

### 3.1 The parent application verifies the updater before launching it

This is the important one, and it is not circular. Your application knows the
SHA-256 of the updater it shipped with, recorded at release time in its own
build info. Before spawning `updater.exe` it hashes the file and refuses to run
a mismatch.

An attacker who can only write to `updater.exe` is caught. One who can also
rewrite the application binary did not need to bother with the updater in the
first place - they already control the process.

`tools/build_branded.py` writes `updater.exe.sha256` for exactly this purpose.
Record it in your application at build time:

```python
# build_info.py, written by CI
UPDATER_SHA256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
```

```python
from pathlib import Path
from qupdatetool.integrity import verify_file_hash

if not verify_file_hash(updater_path, UPDATER_SHA256):
    logger.error("Updater binary failed its integrity check; refusing to run it")
    return
```

Prefer the value compiled into your application over the `.sha256` sidecar
file. A sidecar sitting next to the binary is only as trustworthy as the
directory holding it - an attacker who can replace one can replace both.

### 3.2 Code signing

An Authenticode signature on Windows, or notarization on macOS, is verified by
the operating system rather than by anything inside the file. It is the only
integrity signal a modified binary cannot forge. `build_branded.py` takes a
`--sign-command` for this, and `integrity.authenticode_status()` reports the
verdict so an application can log it.

If you do one thing beyond the parent-side hash check, do this.

### 3.3 Install location permissions

The cheapest version of this attack needs the attacker to be able to overwrite
the binary. Installing to a location that requires administrative rights
removes that. `integrity.check_install_permissions()` reports when the updater
is sitting somewhere a normal user can rewrite.

`updater --self-check` prints all three signals together, along with an
explicit note that the self-reported hash proves nothing on its own.

## 4. Privilege handling

Elevation is never requested speculatively. The updater checks whether the
install directory is actually writable by the current user and elevates only
when it is not, so a per-user install updates with no prompt and a Program
Files install prompts once, at the point of need.

Elevation happens **before** the download rather than after, so the user is not
asked to authorise something after a long wait.

On Windows this is `ShellExecuteExW` with the `runas` verb, waiting on the
child process handle so the exit code propagates and the update runs exactly
once. On Linux it is `pkexec` in a desktop session or `sudo` on a terminal;
with neither available the updater reports that clearly rather than hanging on
a password prompt nobody can answer.

## 5. Archive extraction

Archives are attacker-controlled input until their signature has been
verified, and a verified archive can still be malformed. Before extraction the
updater rejects:

- absolute paths (`/etc/passwd`, `C:\Windows\...`)
- path traversal (`../../..`)
- symlinks and hard links resolving outside the destination

Tar extraction uses the hardened `filter="data"` mode. Extraction always goes
to a scratch directory first and is copied into place only after it completes,
so a corrupt archive cannot leave a half-replaced application.

## 6. What is deliberately not defended

- **An attacker with administrative access to the machine.** Nothing here
  helps, and nothing could. They can replace the application, the updater, and
  the trust store.
- **A compromised signing key.** The pinned key is the root of trust. If it
  leaks, rotate it and ship a new branded build.
- **Downgrade attacks to a signed older release.** The updater installs only
  strictly newer versions, but a signed old release remains legitimately
  signed. Yank compromised releases at the host.
- **Malicious release content that is correctly signed.** Verification proves
  provenance, not intent.

## 7. Reporting

Report vulnerabilities privately to the maintainer rather than opening a public
issue.

## Checklist for a new consuming application

- [ ] Pin the release signing key into the brand with `--key`
- [ ] Set `allowed_fingerprints` to the specific signing key(s)
- [ ] Leave `require_signature: true`
- [ ] Publish a signed `SHA256SUMS.txt` with every release
- [ ] Record `updater.exe.sha256` in the application build info
- [ ] Verify that hash before launching the updater
- [ ] Code sign the updater binary
- [ ] Install to a location that needs privileges to modify
- [ ] Never put an access token in a brand or a shipped config file

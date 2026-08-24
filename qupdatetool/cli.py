"""
Command-line interface and process entry point.

Every flag maps onto a dotted configuration path, and only flags the user
actually passed are collected into the override layer. That distinction
matters: a flag left unset must not overwrite a value coming from the brand
or config.yaml with an argparse default, which is why defaults are set to
None here and the real defaults live in config.DEFAULTS.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import brand as brand_module
from . import config as config_module
from . import integrity, logging_utils
from .errors import CancelledError, ExitCode, UpdaterError
from .updater import build_updater

# Flags that map straight onto a config path. Anything needing interpretation
# (booleans with an inverse, lists, counters) is handled explicitly below.
SIMPLE_FLAG_PATHS = {
    "app_name": "app.name",
    "publisher": "app.publisher",
    "current_version": "app.current_version",
    "executable": "app.executable",
    "install_dir": "app.install_dir",
    "support_url": "app.support_url",
    "provider": "source.provider",
    "host": "source.host",
    "repo": "source.repo",
    "channel": "source.channel",
    "tag_pattern": "source.tag_pattern",
    "token": "source.token",
    "token_env": "source.token_env",
    "token_file": "source.token_file",
    "manifest_url": "source.manifest_url",
    "download_url": "source.download_url",
    "timeout": "source.timeout",
    "retries": "source.retries",
    "asset_pattern": "assets.pattern",
    "exclude_pattern": "assets.exclude_pattern",
    "checksum_file": "security.checksum_file",
    "pubkey_file": "security.public_key_file",
    "install_mode": "install.mode",
    "elevate": "install.elevate",
    "target_dir": "install.target_dir",
    "download_dir": "install.download_dir",
    "calling_pid": "process.calling_pid",
    "stop_timeout": "process.stop_timeout",
    "notes_source": "notes.source",
    "notices_dir": "notes.notices_dir",
    "log_level": "logging.level",
    "log_file": "logging.file",
}


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="updater",
        description=(
            "QUpdateTool - a universal updater for desktop applications. "
            "Checks a git release backend for a newer version, verifies its "
            "signature, installs it, and restarts the application."
        ),
        epilog=(
            "Exit codes: 0 success, 1 unexpected error, 2 usage error, "
            "3 network error, 4 verification failed, 5 install failed, "
            "6 cancelled, 7 process error, 8 no usable release, "
            "10 update available (--check-only), 11 up to date (--check-only)."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("--config", metavar="PATH",
                        help="Path to a YAML config file. Replaces the need for most flags.")
    parser.add_argument("--version", action="store_true",
                        help="Print the updater version and exit")
    parser.add_argument("--self-check", action="store_true",
                        help="Report this binary hash, brand, and code signature, then exit")

    application = parser.add_argument_group("application")
    application.add_argument("--app-name", metavar="NAME",
                             help="Display name of the application being updated")
    application.add_argument("--publisher", metavar="NAME", help="Publisher name")
    application.add_argument("--current-version", metavar="VERSION",
                             help="Version currently installed, used for the comparison")
    application.add_argument("--executable", metavar="PATH",
                             help="Path to the application binary, used to stop and relaunch it")
    application.add_argument("--install-dir", metavar="PATH",
                             help="Install directory (defaults to the executable directory)")
    application.add_argument("--process-name", action="append", metavar="NAME",
                             dest="process_names",
                             help="Process name to stop; may be repeated")
    application.add_argument("--support-url", metavar="URL", help="Support URL shown on failure")

    source = parser.add_argument_group("release source")
    source.add_argument("--provider", choices=["github", "gitea", "forgejo", "gitlab", "generic"],
                        help="Release backend to query")
    source.add_argument("--host", metavar="URL",
                        help="API host, e.g. https://git.example.com for a self-hosted Gitea")
    source.add_argument("--repo", metavar="OWNER/NAME", help="Repository to read releases from")
    source.add_argument("--channel", choices=["stable", "prerelease", "any"],
                        help="Which releases to consider")
    source.add_argument("--tag-pattern", metavar="REGEX",
                        help="Only consider release tags matching this regex")
    source.add_argument("--token", metavar="TOKEN",
                        help="Access token for a private repository. Prefer --token-env.")
    source.add_argument("--token-env", metavar="VAR",
                        help="Environment variable holding the access token")
    source.add_argument("--token-file", metavar="PATH",
                        help="File containing the access token")
    source.add_argument("--manifest-url", metavar="URL",
                        help="generic provider: URL of a JSON release manifest")
    source.add_argument("--download-url", metavar="URL",
                        help="generic provider: direct URL of the artifact")
    source.add_argument("--timeout", type=int, metavar="SECONDS", help="HTTP timeout")
    source.add_argument("--retries", type=int, metavar="N", help="HTTP retry attempts")
    source.add_argument("--no-verify-tls", action="store_true",
                        help="Disable TLS certificate verification (strongly discouraged)")

    assets = parser.add_argument_group("asset selection")
    assets.add_argument("--asset-pattern", metavar="REGEX",
                        help="Regex the release asset filename must match")
    assets.add_argument("--exclude-pattern", metavar="REGEX",
                        help="Regex of asset filenames to ignore")
    assets.add_argument("--asset-preference", metavar="KINDS",
                        help="Comma-separated artifact kinds in priority order, "
                             "e.g. deb,appimage,tar")
    assets.add_argument("--ignore-arch", action="store_true",
                        help="Do not filter assets by CPU architecture")

    security = parser.add_argument_group("security")
    security.add_argument("--pubkey-file", metavar="PATH",
                          help="ASCII-armored OpenPGP public key to verify releases against")
    security.add_argument("--fingerprint", action="append", metavar="FPR",
                          dest="fingerprints",
                          help="Require the signing key to be this fingerprint; may be repeated")
    security.add_argument("--checksum-file", metavar="NAME",
                          help="Name of the checksum manifest asset")
    security.add_argument("--require-signature", action="store_true",
                          help="Refuse to install an unsigned release (default)")
    security.add_argument("--no-require-signature", action="store_true",
                          help="Allow installing a release with no valid signature")
    security.add_argument("--no-require-checksum", action="store_true",
                          help="Allow installing without a checksum match")

    install_group = parser.add_argument_group("installation")
    install_group.add_argument("--install-mode", choices=["silent", "interactive"],
                               help="Run the installer unattended, or let the user walk through it")
    install_group.add_argument("--interactive", action="store_true",
                               help="Shorthand for --install-mode interactive")
    install_group.add_argument("--elevate", choices=["auto", "always", "never"],
                               help="When to request administrator privileges")
    install_group.add_argument("--target-dir", metavar="PATH",
                               help="Where to unpack archive and AppImage releases")
    install_group.add_argument("--download-dir", metavar="PATH",
                               help="Where to stage the download")
    install_group.add_argument("--keep-download", action="store_true",
                               help="Do not delete the artifact after installing")
    install_group.add_argument("--already-elevated", action="store_true",
                               help=argparse.SUPPRESS)

    process_group = parser.add_argument_group("parent process")
    process_group.add_argument("--calling-pid", type=int, metavar="PID",
                               help="PID of the application to stop before installing")
    process_group.add_argument("--stop-timeout", type=int, metavar="SECONDS",
                               help="How long to wait for the application to exit")
    process_group.add_argument("--no-stop", action="store_true",
                               help="Do not stop the parent application")
    process_group.add_argument("--no-relaunch", action="store_true",
                               help="Do not restart the application after updating")
    process_group.add_argument("--relaunch-command", metavar="CMD",
                               help="Command to relaunch instead of the executable")
    process_group.add_argument("--relaunch-arg", action="append", metavar="ARG",
                               dest="relaunch_args",
                               help="Argument to pass on relaunch; may be repeated")

    notes_group = parser.add_argument_group("release notes")
    notes_group.add_argument("--notes-source",
                             choices=["auto", "notices", "changelog", "release", "none"],
                             help="Where to read the what-is-new text from")
    notes_group.add_argument("--notices-dir", metavar="PATH",
                             help="Repository folder holding notice YAML files")

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument("--check-only", action="store_true",
                           help="Report whether an update exists and exit without installing")
    behaviour.add_argument("--dry-run", action="store_true",
                           help="Download and verify, but do not install")
    behaviour.add_argument("--gui", action="store_true",
                           help="Show a graphical progress window")
    behaviour.add_argument("--no-gui", action="store_true",
                           help="Force headless output even if a brand or config file "
                                "defaults ui.gui to true. Use for a scripted or "
                                "background invocation that must never open a window.")
    behaviour.add_argument("--confirm", action="store_true",
                           help="Ask before downloading")
    behaviour.add_argument("--yes", "-y", action="store_true",
                           help="Assume yes for every prompt")
    behaviour.add_argument("--json", action="store_true",
                           help="Print machine-readable JSON on stdout")
    behaviour.add_argument("--quiet", "-q", action="store_true",
                           help="Suppress status output")

    logging_group = parser.add_argument_group("logging")
    logging_group.add_argument("--log-level", choices=list(logging_utils.LOG_LEVELS),
                               help="Log verbosity")
    logging_group.add_argument("--log-file", metavar="PATH",
                               help="Log file or directory. Point this at the application "
                                    "own log directory to keep everything together. "
                                    "Use 'none' to disable file logging.")
    logging_group.add_argument("--log-json", action="store_true",
                               help="Write logs as JSON lines")
    logging_group.add_argument("--no-log-console", action="store_true",
                               help="Do not write log output to the console")

    return parser


def collect_overrides(args, parser) -> dict:
    """
    Turn parsed arguments into a partial config dict.

    Only values the user actually supplied are included, so an unset flag
    never clobbers a branded or configured value with an argparse default.
    """
    overrides: dict = {}

    def assign(path: str, value) -> None:
        node = overrides
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    for attribute, path in SIMPLE_FLAG_PATHS.items():
        value = getattr(args, attribute, None)
        if value is not None:
            assign(path, value)

    if args.process_names:
        assign("app.process_names", list(args.process_names))

    if args.asset_preference:
        kinds = [part.strip() for part in args.asset_preference.split(",") if part.strip()]
        assign("assets.preference", kinds)

    if args.ignore_arch:
        assign("assets.match_arch", False)

    if args.fingerprints:
        assign("security.allowed_fingerprints", list(args.fingerprints))

    if args.no_verify_tls:
        assign("source.verify_tls", False)

    # An explicit --require-signature beats --no-require-signature if both
    # are somehow passed, because the safe reading of a contradiction wins.
    if args.no_require_signature:
        assign("security.require_signature", False)
    if args.require_signature:
        assign("security.require_signature", True)

    if args.no_require_checksum:
        assign("security.require_checksum", False)

    if args.interactive:
        assign("install.mode", "interactive")

    if args.keep_download:
        assign("install.keep_download", True)

    if args.no_stop:
        assign("process.stop_parent", False)

    if args.no_relaunch:
        assign("process.relaunch", False)

    if args.relaunch_command:
        assign("process.relaunch_command", [args.relaunch_command])

    if args.relaunch_args:
        assign("process.relaunch_args", list(args.relaunch_args))

    if args.gui:
        assign("ui.gui", True)

    # Applied after --gui so a contradiction resolves to the safe reading:
    # a background/scripted caller that explicitly asks for no window wins
    # over a brand or config file that defaults ui.gui to true.
    if args.no_gui:
        assign("ui.gui", False)

    if args.confirm:
        assign("ui.confirm", True)

    if args.log_json:
        assign("logging.json", True)

    if args.check_only:
        assign("meta.check_only", True)

    if args.dry_run:
        assign("meta.dry_run", True)

    if args.yes:
        assign("meta.yes", True)

    return overrides


def print_version() -> int:
    """Print version and build information."""
    info = brand_module.brand_info()

    print(f"QUpdateTool {info['build_version']}")
    if info["branded"]:
        print(f"Branded for: {info['brand_name']}")
        print(f"Build date:  {info['build_date']}")
        print(f"Build commit:{info['build_commit']}")
        print(f"Brand hash:  {info['brand_sha256'][:16]}")

    return ExitCode.SUCCESS


def print_self_check(as_json: bool = False) -> int:
    """
    Print everything known about the integrity of this binary.

    Deliberately honest about what a self-check can prove: the hash is
    reported so an external party can compare it against a known-good value,
    not as evidence that this binary is unmodified.
    """
    report = integrity.self_check_report()

    if as_json:
        print(json.dumps(report, indent=2, default=str))
        return ExitCode.SUCCESS

    print(f"Path:            {report['path']}")
    print(f"Frozen binary:   {report['frozen']}")
    print(f"SHA-256:         {report['sha256']}")
    print(f"Branded:         {report['brand']['branded']}")

    if report["brand"]["branded"]:
        print(f"Brand:           {report['brand']['brand_name']}")
        print(f"Brand hash:      {report['brand']['brand_sha256']}")
        print(f"Build:           {report['brand']['build_version']} "
              f"({report['brand']['build_commit']}) {report['brand']['build_date']}")

    print(f"Code signature:  {report['signature_status']} - {report['signature_detail']}")
    print(f"Install location:{report['install_location_detail']}")
    print()
    print("Note: a binary cannot prove its own integrity. Compare the SHA-256")
    print("above against the value recorded by the application that shipped it,")
    print("and rely on the code signature for tamper detection.")

    return ExitCode.SUCCESS


def main(argv: list | None = None) -> int:
    """Entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        return print_version()

    if args.self_check:
        return print_self_check(as_json=args.json)

    try:
        overrides = collect_overrides(args, parser)
        config = config_module.build(
            brand=brand_module.load_brand(),
            config_path=args.config or "",
            overrides=overrides,
        )
    except UpdaterError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return exc.exit_code

    logging_utils.configure(
        level=config.get("logging.level", "INFO"),
        log_file=config.get("logging.file", ""),
        app_name=config.get("app.name") or "QUpdateTool",
        console=not args.no_log_console and not args.quiet,
        json_format=bool(config.get("logging.json", False)),
    )

    # A failure outside run_console - importing Qt, for instance - would
    # otherwise escape as a raw traceback and exit 1 by accident rather than
    # by decision. Catching it here makes the exit code deliberate and gives
    # the user a sentence instead of a stack trace.
    try:
        if config.get("ui.gui", False):
            from .ui.app import run_gui

            return run_gui(config, args)

        return run_console(config, args)

    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return ExitCode.CANCELLED

    except Exception as exc:
        logging_utils.get_logger().exception("Unhandled error")
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return ExitCode.ERROR


def run_console(config, args) -> int:
    """Run the update flow with console output."""
    from .console import ConsoleReporter, print_banner

    reporter = ConsoleReporter(quiet=args.quiet)

    if not args.quiet and not args.json:
        print_banner(config.get("app.name", ""))

    try:
        updater = build_updater(
            config,
            status_callback=reporter.status,
            progress_callback=reporter.progress,
        )

        check = updater.check()

        if args.check_only:
            reporter.finish_progress()
            if args.json:
                print(json.dumps(check.to_dict(), indent=2, default=str))
            else:
                reporter.notes(check.notes)
            return ExitCode.UPDATE_AVAILABLE if check.available else ExitCode.UP_TO_DATE

        if not check.available:
            reporter.success(check.reason)
            if args.json:
                print(json.dumps(check.to_dict(), indent=2, default=str))
            return ExitCode.SUCCESS

        reporter.notes(check.notes)

        should_confirm = config.get("ui.confirm", False) and not config.get("meta.yes", False)
        if should_confirm:
            question = f"Install {config.app_name} {check.latest_version} now?"
            if not reporter.confirm(question, default=True):
                reporter.warn("Update declined")
                return ExitCode.CANCELLED

        outcome = maybe_elevate_and_apply(config, updater, check, reporter, args)

        reporter.finish_progress()

        if args.json:
            print(json.dumps(outcome.to_dict(), indent=2, default=str))
        elif outcome.success:
            reporter.success(
                f"{config.app_name} updated to {outcome.installed_version}"
            )
            if outcome.requires_restart:
                reporter.warn("A system restart is required to finish the update")

        return ExitCode.SUCCESS if outcome.success else ExitCode.INSTALL

    except CancelledError as exc:
        reporter.warn(str(exc))
        return exc.exit_code

    except UpdaterError as exc:
        reporter.error(str(exc))

        support = config.get("app.support_url", "")
        if support and not args.quiet:
            reporter.warn(f"If this keeps happening, see {support}")

        if args.json:
            print(json.dumps({"success": False, "error": str(exc)}, indent=2))

        return exc.exit_code

    except KeyboardInterrupt:
        reporter.warn("Interrupted")
        return ExitCode.CANCELLED

    except Exception as exc:  # unexpected: log it fully, report it briefly
        # ERROR, not INSTALL. An unexpected exception can come from anywhere,
        # including the check phase before anything has been downloaded, and
        # reporting it as an install failure would have the parent tell the
        # user their install broke when nothing was ever installed.
        logging_utils.get_logger().exception("Unhandled error during update")
        reporter.error(f"Unexpected error: {exc}")

        if args.json:
            print(json.dumps({"success": False, "error": str(exc)}, indent=2))

        return ExitCode.ERROR


def maybe_elevate_and_apply(config, updater, check, reporter, args):
    """
    Apply the update, re-running elevated first if the install needs it.

    Elevation is decided before the download rather than after, so the user
    sees the UAC prompt up front instead of after waiting for a long download
    that then cannot be installed.
    """
    from . import elevate

    already = getattr(args, "already_elevated", False)
    install_dir = config.install_dir

    if not already and elevate.needs_elevation(config, install_dir):
        reporter.status("This update needs administrator privileges")

        exit_code = elevate.relaunch_elevated(log=reporter.status)

        # The elevated instance did the work; mirror its result.
        from .updater import UpdateOutcome

        return UpdateOutcome(
            success=exit_code == ExitCode.SUCCESS,
            installed_version=check.latest_version if exit_code == 0 else "",
            method="applied by an elevated instance",
            messages=[f"Elevated updater exited with code {exit_code}"],
        )

    return updater.apply(check)


def run() -> None:
    """Console-script entry point."""
    sys.exit(main())


if __name__ == "__main__":
    run()

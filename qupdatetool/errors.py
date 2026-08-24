"""
Exception hierarchy and process exit codes for QUpdateTool.

Every failure path in the tool raises an UpdaterError subclass so the CLI can
map it onto a stable exit code. Parent applications rely on those codes to
decide what to do next, so they are part of the public contract and must not
be renumbered.
"""


class ExitCode:
    """
    Process exit codes returned by the updater.

    These are a contract with the calling application, which maps them onto
    user-facing messages, so each code must mean exactly one thing. Code 1 is
    reserved for genuinely unclassified failures: without it, an unexpected
    exception has to borrow some other code, and borrowing INSTALL would tell
    a user their install failed when nothing was ever installed.
    """

    SUCCESS = 0             # Update applied, or nothing needed doing
    ERROR = 1               # Unexpected, unclassified failure
    USAGE = 2               # Bad flags, bad config, missing required values
    NETWORK = 3             # Could not reach the release backend
    VERIFICATION = 4        # Checksum or signature verification failed
    INSTALL = 5             # Installer or package manager reported failure
    CANCELLED = 6           # User cancelled from the GUI or a prompt
    PROCESS = 7             # Could not stop or relaunch the parent application
    NO_RELEASE = 8          # Backend reachable, but published no usable release
    UPDATE_AVAILABLE = 10   # --check-only: a newer release exists
    UP_TO_DATE = 11         # --check-only: already on the newest release


class UpdaterError(Exception):
    """
    Base class for every error the updater raises deliberately.

    The base maps to ERROR rather than to any specific category, so a
    subclass that forgets to declare an exit_code degrades to "something went
    wrong" instead of silently misreporting itself as a usage error.
    """

    exit_code = ExitCode.ERROR

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message}: {self.detail}"
        return self.message


class ConfigError(UpdaterError):
    """Invalid, missing, or contradictory configuration."""

    exit_code = ExitCode.USAGE


class NetworkError(UpdaterError):
    """The release backend was unreachable or returned an unusable response."""

    exit_code = ExitCode.NETWORK


class BackendError(NetworkError):
    """The backend responded, but with an error or an unexpected payload."""

    exit_code = ExitCode.NETWORK


class NoReleaseError(UpdaterError):
    """
    The backend has no release matching the requested channel or filters.

    Distinct from NETWORK: the server was reached and answered, it simply had
    nothing installable to offer. Reporting this as a network failure would
    tell a user their connection is broken when a project has merely not cut
    a stable release yet, or has published no asset for their platform.
    """

    exit_code = ExitCode.NO_RELEASE


class VerificationError(UpdaterError):
    """A checksum or OpenPGP signature did not match."""

    exit_code = ExitCode.VERIFICATION


class InstallError(UpdaterError):
    """The platform installer failed or exited non-zero."""

    exit_code = ExitCode.INSTALL


class ProcessError(UpdaterError):
    """The parent application could not be stopped or relaunched."""

    exit_code = ExitCode.PROCESS


class CancelledError(UpdaterError):
    """The user cancelled the update."""

    exit_code = ExitCode.CANCELLED

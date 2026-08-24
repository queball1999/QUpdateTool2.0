"""
Privilege elevation, and deciding when it is actually needed.

The rule the updater follows: never ask for elevation speculatively. Check
whether the install location is writable as the current user, and only
re-launch elevated when it is not. A per-user install updates with no prompt
at all; a Program Files install prompts once, at the moment it is needed.

On Windows elevation means re-running the updater through ShellExecuteW with
the "runas" verb, which is what raises the UAC dialog. On Linux and macOS it
means pkexec or sudo, and in a headless session there may be no way to prompt
at all, which is reported as such rather than hanging on a password prompt.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .errors import InstallError


def is_elevated() -> bool:
    """Return True when the current process already has administrative rights."""
    if sys.platform.startswith("win"):
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False

    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def can_write(path: str | os.PathLike) -> bool:
    """
    Test whether the current user can actually write to a directory.

    os.access lies often enough on Windows that an empty temp file is created
    instead; an actual write is the only trustworthy answer.
    """
    directory = Path(path)
    if not directory.is_dir():
        directory = directory.parent
    if not directory.is_dir():
        return False

    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".qupdate-probe-"):
            return True
    except (OSError, PermissionError):
        return False


def needs_elevation(config, install_dir: str | os.PathLike | None) -> bool:
    """
    Decide whether this update requires elevated privileges.

    "always" and "never" are honoured verbatim; "auto" checks whether the
    install directory is writable and elevates only if it is not.
    """
    mode = (config.get("install.elevate") or "auto").lower()

    if mode == "never":
        return False
    if mode == "always":
        return not is_elevated()

    if is_elevated():
        return False

    if not install_dir:
        return False

    return not can_write(install_dir)


def relaunch_elevated(extra_args: tuple = (), log=None) -> int:
    """
    Re-run this updater with elevated privileges and wait for it to finish.

    Returns the exit code of the elevated instance. The current process should
    exit with that code immediately afterwards, so the update runs exactly
    once rather than continuing unprivileged in parallel.
    """
    if sys.platform.startswith("win"):
        return windows_relaunch_elevated(extra_args, log)
    return posix_relaunch_elevated(extra_args, log)


def current_command(extra_args: tuple = ()) -> tuple:
    """
    Rebuild the command line that launched this updater.

    A frozen binary re-runs itself directly; running from source re-runs the
    interpreter with the same script and arguments. A marker flag is appended
    so the elevated instance knows not to try elevating again.
    """
    arguments = list(sys.argv[1:]) + [argument for argument in extra_args]

    if "--already-elevated" not in arguments:
        arguments.append("--already-elevated")

    if getattr(sys, "frozen", False):
        return sys.executable, arguments

    return sys.executable, [os.path.abspath(sys.argv[0])] + arguments


def quote_windows(arguments: list) -> str:
    """Join arguments into a Windows command-line string, quoting as needed."""
    parts = []
    for argument in arguments:
        text = str(argument)
        if not text:
            parts.append('""')
        elif any(character in text for character in ' \t"'):
            escaped = text.replace('"', '\\"')
            parts.append(f'"{escaped}"')
        else:
            parts.append(text)
    return " ".join(parts)


def windows_relaunch_elevated(extra_args: tuple = (), log=None) -> int:
    """
    Raise a UAC prompt through ShellExecuteExW and wait for the child to exit.

    ShellExecuteExW is used rather than the simpler ShellExecuteW because it
    returns a process handle, which is what makes it possible to wait for the
    elevated instance and propagate its exit code instead of returning
    immediately and leaving the parent guessing.
    """
    import ctypes.wintypes

    executable, arguments = current_command(extra_args)
    parameters = quote_windows(arguments)

    if log:
        log("Requesting administrator privileges")

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", ctypes.wintypes.HANDLE),
            ("lpVerb", ctypes.wintypes.LPCWSTR),
            ("lpFile", ctypes.wintypes.LPCWSTR),
            ("lpParameters", ctypes.wintypes.LPCWSTR),
            ("lpDirectory", ctypes.wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", ctypes.wintypes.LPCWSTR),
            ("hkeyClass", ctypes.wintypes.HKEY),
            ("dwHotKey", ctypes.wintypes.DWORD),
            ("hIconOrMonitor", ctypes.wintypes.HANDLE),
            ("hProcess", ctypes.wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NOASYNC = 0x00000100
    SW_SHOWNORMAL = 1
    INFINITE = 0xFFFFFFFF

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.hwnd = None
    info.lpVerb = "runas"
    info.lpFile = executable
    info.lpParameters = parameters
    info.lpDirectory = None
    info.nShow = SW_SHOWNORMAL

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        # 1223 is ERROR_CANCELLED: the user declined the UAC prompt.
        if error == 1223:
            raise InstallError(
                "Administrator privileges were declined",
                "The update needs elevation to write to the install directory",
            )
        raise InstallError("Could not request elevation", f"Windows error {error}")

    if not info.hProcess:
        raise InstallError("Elevation did not return a process handle")

    ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, INFINITE)

    exit_code = ctypes.wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code))
    ctypes.windll.kernel32.CloseHandle(info.hProcess)

    return int(exit_code.value)


def posix_relaunch_elevated(extra_args: tuple = (), log=None) -> int:
    """
    Re-run the updater through pkexec or sudo.

    pkexec is preferred in a desktop session because it shows a graphical
    prompt. sudo is used otherwise, but only when a terminal is attached;
    launching sudo with no tty would block forever waiting for a password
    nobody can type, so that case fails with a clear message instead.
    """
    executable, arguments = current_command(extra_args)
    command = [executable] + arguments

    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    pkexec = shutil.which("pkexec")
    sudo = shutil.which("sudo")

    if pkexec and has_display:
        launcher = [pkexec]
    elif sudo and sys.stdin and sys.stdin.isatty():
        launcher = [sudo]
    elif pkexec:
        launcher = [pkexec]
    else:
        raise InstallError(
            "Elevated privileges are required but cannot be requested",
            "Run the updater with sudo, or install pkexec for a graphical prompt",
        )

    if log:
        log(f"Requesting elevated privileges through {Path(launcher[0]).name}")

    try:
        result = subprocess.run(launcher + command, check=False)
    except OSError as exc:
        raise InstallError("Could not request elevation", str(exc)) from exc

    # pkexec reports 126 for a dismissed dialog and 127 for authorisation failure.
    if result.returncode in (126, 127):
        raise InstallError(
            "Elevated privileges were declined",
            "The update needs elevation to write to the install directory",
        )

    return result.returncode

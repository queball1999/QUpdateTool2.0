"""
Stopping and relaunching the parent application.

Getting this right is most of what separates a working updater from one that
corrupts an install. The sequence is always: ask politely, wait, escalate,
confirm the file locks are gone, install, then relaunch. Skipping the "wait
until the executable is actually unlocked" step is the classic Windows bug
where the installer fails because the old binary is still mapped.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .errors import ProcessError

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a hard dependency
    psutil = None


def find_processes(pid: int = 0, names: tuple = (), executable: str = "") -> list:
    """
    Find the processes belonging to the parent application.

    Three signals are combined because none is reliable alone: the PID the
    parent handed us (exact, but the parent may have restarted), the process
    name (catches sibling instances), and the executable path (catches a
    renamed binary). The updater must never match itself, so its own PID and
    the whole process tree above it are excluded.
    """
    if psutil is None:
        raise ProcessError("psutil is not installed")

    own_pid = os.getpid()
    matches = {}

    wanted_names = {name.lower() for name in names if name}
    for name in list(wanted_names):
        # Match "App" and "App.exe" interchangeably.
        if name.endswith(".exe"):
            wanted_names.add(name[:-4])
        else:
            wanted_names.add(f"{name}.exe")

    wanted_exe = str(Path(executable).resolve()).lower() if executable else ""

    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            info = process.info
            process_pid = info.get("pid")

            if process_pid in (own_pid, 0):
                continue

            process_name = (info.get("name") or "").lower()
            process_exe = (info.get("exe") or "")

            matched = False
            if (pid and process_pid == pid) or (wanted_names and process_name in wanted_names):
                matched = True
            elif wanted_exe and process_exe:
                try:
                    if str(Path(process_exe).resolve()).lower() == wanted_exe:
                        matched = True
                except (OSError, ValueError):
                    pass

            if matched:
                matches[process_pid] = process

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return list(matches.values())


def stop_processes(processes: list, timeout: int = 30, force: bool = True,
                   log=None) -> list:
    """
    Terminate the given processes, escalating to a kill if they do not exit.

    Returns the list of processes that were still alive at the end, which the
    caller treats as a hard failure: installing over a running application is
    how you get a half-updated install.
    """
    if not processes:
        return []

    def note(message):
        if log:
            log(message)

    for process in processes:
        try:
            note(f"Asking {process.name()} (pid {process.pid}) to exit")
            process.terminate()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise ProcessError(
                f"Not permitted to stop pid {process.pid}",
                "The updater may need to run with the same privileges as the "
                f"application: {exc}",
            ) from exc

    gone, alive = psutil.wait_procs(processes, timeout=timeout)

    for process in gone:
        note(f"pid {process.pid} exited")

    if alive and force:
        for process in alive:
            try:
                note(f"pid {process.pid} did not exit; killing it")
                process.kill()
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                continue

        _, alive = psutil.wait_procs(alive, timeout=10)

    return alive


def wait_for_unlock(path: str | os.PathLike, timeout: int = 30, log=None) -> bool:
    """
    Wait until a file can be opened for writing.

    On Windows a terminated process can hold its image locked briefly after
    it disappears from the process list. Installing during that window fails
    in confusing ways, so the updater waits for the lock to clear rather than
    trusting that process exit means the file is free.
    """
    target = Path(path)
    if not target.exists():
        return True

    deadline = time.monotonic() + timeout
    reported = False

    while time.monotonic() < deadline:
        try:
            with open(target, "ab"):
                return True
        except PermissionError:
            if not reported and log:
                log(f"Waiting for {target.name} to be released")
                reported = True
            time.sleep(0.5)
        except OSError:
            # Not a locking problem; nothing to wait for.
            return True

    return False


def stop_parent(config, log=None) -> list:
    """
    Stop the parent application described by the configuration.

    Returns the list of process names that were stopped, so the caller can
    report accurately and decide what to relaunch.
    """
    if not config.get("process.stop_parent", True):
        return []

    pid = int(config.get("process.calling_pid", 0) or 0)
    names = tuple(config.get("app.process_names", []) or ())
    executable = config.get("app.executable", "")

    if not (pid or names or executable):
        return []

    processes = find_processes(pid=pid, names=names, executable=executable)
    if not processes:
        if log:
            log("Parent application is not running")
        return []

    stopped = []
    for process in processes:
        try:
            stopped.append(process.name())
        except psutil.Error:
            stopped.append(f"pid {process.pid}")

    timeout = int(config.get("process.stop_timeout", 30))
    force = bool(config.get("process.force_after_timeout", True))

    alive = stop_processes(processes, timeout=timeout, force=force, log=log)

    if alive:
        names_alive = ", ".join(str(process.pid) for process in alive)
        raise ProcessError(
            "Could not stop the running application",
            f"still running: {names_alive}. Close it manually and retry.",
        )

    if executable:
        wait_for_unlock(executable, timeout=timeout, log=log)

    return stopped


def relaunch(config, log=None) -> bool:
    """
    Start the parent application again after a successful update.

    The new process is fully detached so it does not die when the updater
    exits, and its working directory is set to the install directory so
    relative resource paths resolve the way they do on a normal launch.
    """
    if not config.get("process.relaunch", True):
        return False

    command = list(config.get("process.relaunch_command", []) or [])

    if not command:
        executable = config.get("app.executable", "")
        if not executable:
            if log:
                log("Nothing to relaunch: no executable configured")
            return False
        command = [executable]

    command += list(config.get("process.relaunch_args", []) or [])

    target = Path(command[0])
    if not target.exists():
        # The installer may have moved the binary; this is worth reporting
        # but not worth failing the whole update over, since the update
        # itself already succeeded.
        if log:
            log(f"Cannot relaunch: {target} does not exist")
        return False

    delay = int(config.get("process.wait_before_relaunch", 2))
    if delay > 0:
        time.sleep(delay)

    working_dir = config.install_dir
    kwargs = {
        "cwd": str(working_dir) if working_dir and working_dir.is_dir() else None,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }

    if sys.platform.startswith("win"):
        # DETACHED_PROCESS plus a new group means the relaunched app survives
        # the updater exiting and does not inherit its console.
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    try:
        if log:
            log(f"Relaunching {' '.join(command)}")
        subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise ProcessError(f"Could not relaunch {command[0]}", str(exc)) from exc

    return True


def is_running(pid: int) -> bool:
    """Return True when a PID belongs to a live process."""
    if psutil is None or not pid:
        return False
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except psutil.Error:
        return False


def send_graceful_signal(pid: int) -> bool:
    """
    Ask a process to shut down cleanly, before any terminate/kill escalation.

    On POSIX this is SIGTERM. On Windows there is no portable equivalent that
    reaches a GUI application, so this is a no-op and psutil.terminate handles
    it; the function exists so callers can express the intent uniformly.
    """
    if not pid or psutil is None:
        return False

    if sys.platform.startswith("win"):
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, ProcessLookupError):
        return False

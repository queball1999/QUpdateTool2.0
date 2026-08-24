"""
Headless console output.

This is the default front end: the updater runs without a GUI unless --gui is
passed, so everything the user sees in normal operation comes from here.

stdout is reserved for machine-readable output (--json) and the final result.
Status lines and the progress bar go to stderr, so a parent application can
parse stdout cleanly while a human watching a terminal still sees progress.
"""

from __future__ import annotations

import shutil
import sys
import time

from . import __version__
from .download import format_size

BAR_WIDTH = 32


class ConsoleReporter:
    """Prints status lines and a progress bar to stderr."""

    def __init__(self, quiet: bool = False, use_colour: bool | None = None):
        self.quiet = quiet
        self.last_progress = 0.0
        self.progress_active = False
        self.colour = self.detect_colour() if use_colour is None else use_colour

    @staticmethod
    def detect_colour() -> bool:
        """
        Decide whether to emit ANSI colour.

        Honours the NO_COLOR convention and only colours a real terminal, so
        redirected output stays clean.
        """
        import os

        if os.environ.get("NO_COLOR"):
            return False
        if not sys.stderr.isatty():
            return False
        if sys.platform.startswith("win"):
            # Windows Terminal and modern conhost support ANSI; older ones do
            # not, and WT_SESSION is the reliable signal for the former.
            return bool(os.environ.get("WT_SESSION") or os.environ.get("ANSICON"))
        return True

    def paint(self, text: str, code: str) -> str:
        if not self.colour:
            return text
        return f"\033[{code}m{text}\033[0m"

    def status(self, message: str) -> None:
        """Print one status line."""
        if self.quiet:
            return

        self.finish_progress()
        print(self.paint("->", "36") + f" {message}", file=sys.stderr, flush=True)

    def warn(self, message: str) -> None:
        self.finish_progress()
        print(self.paint("!", "33") + f" {message}", file=sys.stderr, flush=True)

    def error(self, message: str) -> None:
        self.finish_progress()
        print(self.paint("x", "31") + f" {message}", file=sys.stderr, flush=True)

    def success(self, message: str) -> None:
        self.finish_progress()
        print(self.paint("ok", "32") + f" {message}", file=sys.stderr, flush=True)

    def progress(self, snapshot) -> None:
        """
        Render a download progress bar, redrawn in place.

        Updates are throttled to whole percentage points so a fast download
        does not spend more time writing to the terminal than to disk.
        """
        if self.quiet or not sys.stderr.isatty():
            return

        percent = snapshot.percent

        if self.progress_active and abs(percent - self.last_progress) < 1.0 and percent < 100:
            return

        self.last_progress = percent
        self.progress_active = True

        if snapshot.total > 0:
            filled = int(BAR_WIDTH * percent / 100)
            bar = "#" * filled + "-" * (BAR_WIDTH - filled)
            eta = snapshot.eta_seconds
            eta_text = f" ETA {int(eta // 60):d}:{int(eta % 60):02d}" if eta > 0 else ""
            line = (
                f"   [{bar}] {percent:5.1f}%  "
                f"{format_size(snapshot.downloaded)}/{format_size(snapshot.total)}  "
                f"{format_size(snapshot.speed)}/s{eta_text}"
            )
        else:
            line = (
                f"   {format_size(snapshot.downloaded)} downloaded  "
                f"{format_size(snapshot.speed)}/s"
            )

        width = shutil.get_terminal_size((100, 24)).columns
        sys.stderr.write("\r" + line[: width - 1].ljust(width - 1))
        sys.stderr.flush()

    def finish_progress(self) -> None:
        """Clear the progress bar line so the next message starts clean."""
        if not self.progress_active:
            return

        self.progress_active = False
        if sys.stderr.isatty():
            width = shutil.get_terminal_size((100, 24)).columns
            sys.stderr.write("\r" + " " * (width - 1) + "\r")
            sys.stderr.flush()

    def confirm(self, question: str, default: bool = True) -> bool:
        """
        Ask a yes/no question on the terminal.

        With no terminal attached there is nobody to answer, so the default is
        taken rather than blocking forever on a read that will never return.
        """
        self.finish_progress()

        if not sys.stdin or not sys.stdin.isatty():
            return default

        suffix = "[Y/n]" if default else "[y/N]"

        try:
            answer = input(f"{question} {suffix} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return False

        if not answer:
            return default

        return answer in ("y", "yes")

    def notes(self, release_notes) -> None:
        """Print release notes in a readable block."""
        if self.quiet or release_notes is None or release_notes.is_empty:
            return

        self.finish_progress()

        title = release_notes.title or "What is new"
        print(file=sys.stderr)
        print(self.paint(title, "1"), file=sys.stderr)
        print(self.paint("-" * min(len(title), 60), "2"), file=sys.stderr)

        for line in release_notes.body.splitlines():
            print(f"  {line}", file=sys.stderr)

        print(file=sys.stderr)


def print_banner(app_name: str = "") -> None:
    """Print the tool banner; suppressed whenever output is not a terminal."""
    if not sys.stderr.isatty():
        return

    target = f" - updating {app_name}" if app_name else ""
    print(f"QUpdateTool {__version__}{target}", file=sys.stderr)


def wait_and_close(seconds: int) -> None:
    """Pause before exiting, so a console window launched by a GUI is readable."""
    if seconds > 0 and sys.stderr.isatty():
        time.sleep(seconds)

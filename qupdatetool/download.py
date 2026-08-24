"""
Streaming downloader with progress reporting and resume support.

Downloads run on the calling thread and report progress through a callback,
which keeps this module free of any GUI dependency. The Qt window and the
console progress bar are both just callbacks.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import NetworkError

try:
    import requests
except ImportError:  # pragma: no cover - requests is a hard dependency
    requests = None

CHUNK_SIZE = 64 * 1024


@dataclass
class Progress:
    """A progress snapshot handed to the progress callback."""

    downloaded: int
    total: int
    filename: str
    speed: float = 0.0          # bytes per second, averaged over the transfer
    elapsed: float = 0.0

    @property
    def percent(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(100.0, (self.downloaded / self.total) * 100.0)

    @property
    def eta_seconds(self) -> float:
        """Estimated seconds remaining, or 0 when it cannot be known."""
        if self.speed <= 0 or self.total <= 0:
            return 0.0
        remaining = max(0, self.total - self.downloaded)
        return remaining / self.speed

    def describe(self) -> str:
        if self.total > 0:
            return (
                f"{self.percent:.1f}% "
                f"({format_size(self.downloaded)} / {format_size(self.total)}) "
                f"at {format_size(self.speed)}/s"
            )
        return f"{format_size(self.downloaded)} at {format_size(self.speed)}/s"


def format_size(value: float) -> str:
    """Render a byte count in human units."""
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            precision = 0 if unit == "B" else 1
            return f"{size:.{precision}f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class Downloader:
    """Fetches release assets to disk, with progress and cancellation."""

    def __init__(self, session=None, headers: dict | None = None, timeout: int = 30,
                 verify_tls: bool = True):
        if requests is None:
            raise NetworkError("The requests library is not installed")

        self.session = session or requests.Session()
        self.headers = headers or {}
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.cancelled = False

    def cancel(self) -> None:
        """Ask an in-flight download to stop at the next chunk boundary."""
        self.cancelled = True

    def fetch_bytes(self, url: str, max_bytes: int = 8 * 1024 * 1024) -> bytes:
        """
        Download a small file straight into memory.

        Used for signature and checksum files, which are a few hundred bytes.
        The cap stops a hostile or misconfigured server from making the
        updater allocate without limit for what should be a tiny file.
        """
        try:
            response = self.session.get(
                url,
                headers=self.headers,
                timeout=self.timeout,
                verify=self.verify_tls,
                stream=True,
            )
        except requests.exceptions.RequestException as exc:
            raise NetworkError(f"Could not download {url}", str(exc)) from exc

        if response.status_code >= 400:
            raise NetworkError(
                f"Download failed with HTTP {response.status_code}",
                url,
            )

        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise NetworkError(
                    "Auxiliary file was larger than expected",
                    f"{url} exceeded {format_size(max_bytes)}",
                )
            chunks.append(chunk)

        return b"".join(chunks)

    def fetch_file(self, url: str, target: str | os.PathLike, expected_size: int = 0,
                   progress_callback=None, resume: bool = True) -> Path:
        """
        Download `url` to `target`, reporting progress along the way.

        The download lands in a temporary file in the destination directory
        and is moved into place only once complete, so an interrupted update
        can never leave a truncated installer that looks ready to run.
        """
        target_path = Path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        partial_path = target_path.with_suffix(target_path.suffix + ".part")
        existing = partial_path.stat().st_size if (resume and partial_path.is_file()) else 0

        headers = dict(self.headers)
        if existing:
            headers["Range"] = f"bytes={existing}-"

        try:
            response = self.session.get(
                url,
                headers=headers,
                timeout=self.timeout,
                verify=self.verify_tls,
                stream=True,
                allow_redirects=True,
            )
        except requests.exceptions.SSLError as exc:
            raise NetworkError("TLS verification failed during download", str(exc)) from exc
        except requests.exceptions.RequestException as exc:
            raise NetworkError(f"Could not download {url}", str(exc)) from exc

        # A server that ignores the Range header restarts the file, so the
        # partial data has to be discarded rather than appended to.
        if existing and response.status_code != 206:
            existing = 0
            partial_path.unlink(missing_ok=True)

        if response.status_code >= 400:
            raise NetworkError(
                f"Download failed with HTTP {response.status_code}",
                (response.text or "")[:300] or url,
            )

        total = int(response.headers.get("content-length") or 0) + existing
        if expected_size and not total:
            total = expected_size

        started = time.monotonic()
        downloaded = existing
        mode = "ab" if existing else "wb"

        try:
            with open(partial_path, mode) as handle:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if self.cancelled:
                        raise DownloadCancelled(str(partial_path))
                    if not chunk:
                        continue

                    handle.write(chunk)
                    downloaded += len(chunk)

                    if progress_callback:
                        elapsed = max(1e-6, time.monotonic() - started)
                        speed = (downloaded - existing) / elapsed
                        progress_callback(
                            Progress(
                                downloaded=downloaded,
                                total=total,
                                filename=target_path.name,
                                speed=speed,
                                elapsed=elapsed,
                            )
                        )
        except requests.exceptions.RequestException as exc:
            raise NetworkError("Download interrupted", str(exc)) from exc

        if expected_size and downloaded != expected_size:
            partial_path.unlink(missing_ok=True)
            raise NetworkError(
                "Downloaded file was the wrong size",
                f"expected {expected_size} bytes, got {downloaded}",
            )

        # Move into place only now that the file is known to be complete.
        target_path.unlink(missing_ok=True)
        shutil.move(str(partial_path), str(target_path))

        if progress_callback:
            progress_callback(
                Progress(
                    downloaded=downloaded,
                    total=total or downloaded,
                    filename=target_path.name,
                    speed=(downloaded - existing) / max(1e-6, time.monotonic() - started),
                    elapsed=time.monotonic() - started,
                )
            )

        return target_path


class DownloadCancelled(NetworkError):
    """Raised when a download is cancelled through Downloader.cancel()."""

    def __init__(self, partial_path: str = ""):
        super().__init__("Download cancelled", partial_path)
        self.partial_path = partial_path


def default_download_dir(app_name: str = "QUpdateTool") -> Path:
    """
    Pick a writable directory to stage downloads in.

    A per-run temporary directory under the system temp location is used, so
    a failed update leaves nothing behind in the user Downloads folder and
    two updaters running at once cannot collide.
    """
    base = Path(tempfile.gettempdir()) / f"{app_name}-update"
    base.mkdir(parents=True, exist_ok=True)
    return base

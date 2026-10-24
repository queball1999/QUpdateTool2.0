"""
Post-install health check.

After the updated application has been started, the updater can poll a URL
(for example http://127.0.0.1/healthz) to learn whether the new version came
up. Success is an HTTP 200 response. If the URL never answers 200 within the
configured number of attempts, the caller restores the previous files and
restarts the application (see Updater.roll_back).

With no health.url configured nothing here runs, so behaviour is unchanged.
"""

from __future__ import annotations

import time

import requests

from .errors import ConfigError

# Longest a single request may take, so one hung attempt cannot eat the whole
# window by itself.
MAX_REQUEST_TIMEOUT = 10.0


def settings(config) -> tuple:
    """
    Read the health settings from a config as (url, attempts, window_seconds).

    An empty url means the check is disabled. Raises ConfigError when the
    numbers are not usable.
    """
    url = str(config.get("health.url", "") or "").strip()
    try:
        attempts = int(config.get("health.attempts", 3))
        window = int(config.get("health.window_seconds", 60))
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "health.attempts and health.window_seconds must be whole numbers"
        ) from exc
    if attempts < 1 or window < 1:
        raise ConfigError("health.attempts and health.window_seconds must be at least 1")
    return url, attempts, window


def probe(url: str, timeout: float, log=None) -> bool:
    """
    Make one GET request and return True only for an HTTP 200 response.

    Redirects are not followed: a health endpoint that redirects is not
    healthy. Connection errors, timeouts and any other status are logged and
    reported as False, never raised, because a refused connection is the
    expected answer while the application is still starting.
    """
    try:
        response = requests.get(url, timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        if log:
            log(f"Health probe of {url} failed: {type(exc).__name__}: {exc}")
        return False

    healthy = response.status_code == 200
    if log:
        log(f"Health probe of {url} returned HTTP {response.status_code}")
    return healthy


def wait_until_healthy(url: str, attempts: int, window_seconds: int, log=None,
                       sleep=time.sleep, clock=time.monotonic) -> bool:
    """
    Poll a URL until it answers HTTP 200 or the attempts are used up.

    The window is split into `attempts` equal slots and one request is made
    at the start of each slot, so all attempts finish inside the window.
    Returns True as soon as a request succeeds, False after the last failure.
    `sleep` and `clock` are injectable so tests do not have to wait.
    """
    interval = window_seconds / attempts
    timeout = min(interval, MAX_REQUEST_TIMEOUT)

    for attempt in range(1, attempts + 1):
        started = clock()
        if log:
            log(f"Health check attempt {attempt} of {attempts}")

        if probe(url, timeout, log=log):
            return True

        if attempt < attempts:
            remaining = interval - (clock() - started)
            if remaining > 0:
                sleep(remaining)

    return False

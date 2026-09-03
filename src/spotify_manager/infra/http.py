"""The single shared, rate-limited HTTP session every API call goes through.

One session governs all requests so that the whole tool has exactly one place that
throttles, honours `Retry-After`, backs off on server errors, and -- in verbose mode
-- prints what it is about to do.

Policy, in order of precedence:

* Self-throttle. Requests are spaced at least `min_interval` apart, a client-side
  ceiling deliberately set below Spotify's (undocumented, quota-dependent) threshold
  so that ordinary runs never get throttled in the first place.
* 429. Spotify's own `Retry-After` header is authoritative: we sleep exactly that
  many seconds (plus a small epsilon for clock rounding) and retry, rather than
  guessing a delay. If the header is absent we fall back to a conservative constant.
* 5xx. Transient; retried with exponential backoff plus jitter, up to a cap.
* Other 4xx. Not retryable (bad scope, malformed request, revoked token) -- raised
  immediately as an `ApiError` carrying Spotify's own message.
"""

from __future__ import annotations

import random
import sys
import time
from typing import Any
from urllib.parse import urlencode

import requests

from ..errors import ApiError

API_BASE = "https://api.spotify.com/v1"

#: Client-side ceiling: at most this many requests per second, across the whole run.
DEFAULT_REQUESTS_PER_SECOND = 3.0
#: Seconds added to a `Retry-After` wait to survive clock and rounding skew.
RETRY_AFTER_EPSILON = 0.1
#: Used only when a 429 arrives without the header Spotify normally sends.
RETRY_AFTER_FALLBACK_SECONDS = 5.0
DEFAULT_MAX_429_RETRIES = 10
DEFAULT_MAX_5XX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 15.0


def backoff_delay(
    attempt: int,
    *,
    base: float = BACKOFF_BASE_SECONDS,
    cap: float = BACKOFF_CAP_SECONDS,
    jitter: float = 0.0,
) -> float:
    """Delay before retry number `attempt` (1-based) of a server error.

    Doubles each attempt from `base`, clamped at `cap`, then adds up to `jitter`
    times the delay as random spread. Pure, so the schedule can be asserted on.
    """
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    delay = min(base * (2 ** (attempt - 1)), cap)
    if jitter:
        delay += random.uniform(0.0, delay * jitter)
    return delay


def parse_retry_after(value: str | None) -> float:
    """Seconds to wait per a `Retry-After` header, or the fallback if unusable."""
    if value is None:
        return RETRY_AFTER_FALLBACK_SECONDS
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return RETRY_AFTER_FALLBACK_SECONDS
    if seconds < 0:
        return RETRY_AFTER_FALLBACK_SECONDS
    return seconds


class RequestStats:
    """Counters for the run summary."""

    def __init__(self) -> None:
        self.requests = 0
        self.retries = 0
        self.throttled = 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"RequestStats(requests={self.requests}, retries={self.retries}, "
            f"throttled={self.throttled})"
        )


class RateLimitedSession:
    """A `requests.Session` wrapper implementing the policy described above."""

    def __init__(
        self,
        token_provider: Any,
        *,
        verbose: bool = False,
        requests_per_second: float = DEFAULT_REQUESTS_PER_SECOND,
        max_5xx_retries: int = DEFAULT_MAX_5XX_RETRIES,
        max_429_retries: int = DEFAULT_MAX_429_RETRIES,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> None:
        self._token_provider = token_provider
        self._verbose = verbose
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._max_5xx_retries = max_5xx_retries
        self._max_429_retries = max_429_retries
        self._timeout = timeout
        self._sleep = sleep
        self._monotonic = monotonic
        self._session = requests.Session()
        self._last_request_at: float | None = None
        self.stats = RequestStats()

    # -- public verb helpers -------------------------------------------------

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.request("GET", url, params=params)
        return response.json() if response.content else {}

    def put_ids(self, path: str, ids: list[str]) -> None:
        """PUT album ids in the JSON body (the body form allows 50 per call)."""
        self.request("PUT", path, json={"ids": ids}, note=f"{len(ids)} ids")

    def delete_ids(self, path: str, ids: list[str]) -> None:
        """DELETE album ids in the JSON body (the body form allows 50 per call)."""
        self.request("DELETE", path, json={"ids": ids}, note=f"{len(ids)} ids")

    # -- the one code path ---------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        note: str | None = None,
    ) -> requests.Response:
        full_url = url if url.startswith("http") else f"{API_BASE}{url}"
        throttle_attempts = 0
        server_error_attempts = 0

        while True:
            self._respect_rate_cap()
            token = self._token_provider.access_token()
            self._log_request(method, full_url, params, note)

            response = self._session.request(
                method,
                full_url,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
            )
            self._last_request_at = self._monotonic()
            self.stats.requests += 1

            if response.status_code == 429:
                throttle_attempts += 1
                self.stats.throttled += 1
                if throttle_attempts > self._max_429_retries:
                    raise ApiError(
                        "Spotify is still rate limiting this app after "
                        f"{self._max_429_retries} waits. Try again later."
                    )
                wait = parse_retry_after(response.headers.get("Retry-After"))
                self._log(
                    f"429 rate limited; waiting {wait:g}s "
                    f"({'Retry-After' if 'Retry-After' in response.headers else 'no Retry-After header, using fallback'})"
                )
                self.stats.retries += 1
                self._sleep(wait + RETRY_AFTER_EPSILON)
                continue

            if 500 <= response.status_code < 600:
                server_error_attempts += 1
                if server_error_attempts > self._max_5xx_retries:
                    raise ApiError(
                        f"Spotify returned {response.status_code} for "
                        f"{method} {full_url} after {self._max_5xx_retries} retries. "
                        "This is a problem on Spotify's side; try again later."
                    )
                wait = backoff_delay(server_error_attempts, jitter=0.25)
                self._log(
                    f"{response.status_code} server error; retry "
                    f"{server_error_attempts}/{self._max_5xx_retries} in {wait:.1f}s"
                )
                self.stats.retries += 1
                self._sleep(wait)
                continue

            if response.status_code >= 400:
                raise ApiError(_client_error_message(method, full_url, response))

            return response

    # -- internals -----------------------------------------------------------

    def _respect_rate_cap(self) -> None:
        if self._last_request_at is None or self._min_interval <= 0:
            return
        elapsed = self._monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            self._sleep(self._min_interval - elapsed)

    def _log_request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        note: str | None,
    ) -> None:
        if not self._verbose:
            return
        line = f"{method} {url}"
        if params:
            line += "?" + urlencode(params)
        if note:
            line += f" ({note})"
        self._log(line)

    def _log(self, message: str) -> None:
        if self._verbose:
            print(f"[api] {message}", file=sys.stderr, flush=True)


def _client_error_message(method: str, url: str, response: requests.Response) -> str:
    detail = ""
    try:
        payload = response.json()
        detail = payload.get("error", {}).get("message", "")
    except ValueError:
        detail = response.text[:200]

    if response.status_code == 401:
        return (
            "Spotify rejected the access token (401). Delete the token cache in "
            ".spotifymanager/token_cache.json and run the command again to log in."
        )
    if response.status_code == 403:
        return (
            "Spotify refused the request (403). The cached login is probably missing "
            "a required permission. Delete .spotifymanager/token_cache.json and run "
            f"the command again to re-authorise. Spotify said: {detail}"
        )
    return f"Spotify returned {response.status_code} for {method} {url}: {detail}"

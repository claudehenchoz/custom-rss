"""Polite, retrying HTTP GET with no shared mutable state."""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USER_AGENT = "custom-rss/1.0 (+https://github.com/claudehenchoz/custom-rss)"

log = logging.getLogger("custom_rss.http")


class FetchError(Exception):
    """A request failed after all retries, or returned something unusable."""


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        timeout: float = 30,
        max_attempts: int = 4,
        backoff_base: float = 2.0,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base

    def get_bytes(self, url: str, *, accept: str) -> bytes:
        return self._request(url, accept=accept, parse=None)

    def get_json(self, url: str) -> Any:
        return self._request(url, accept="application/json", parse=json.loads)

    def _request(self, url: str, *, accept: str, parse: Any) -> Any:
        request = Request(url, headers={"Accept": accept, "User-Agent": self.user_agent})
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = response.read()
                return parse(payload) if parse is not None else payload
            except HTTPError as error:
                last_error = error
                retryable = error.code == 429 or error.code >= 500
                if not retryable:
                    raise FetchError(f"GET {url} -> HTTP {error.code} {error.reason}") from error
                delay = self._retry_delay(attempt, error.headers.get("Retry-After") if error.headers else None)
            except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
                last_error = error
                delay = self._retry_delay(attempt, None)

            if attempt < self.max_attempts:
                log.warning(
                    "Attempt %d/%d for %s failed (%s); retrying in %.1fs",
                    attempt,
                    self.max_attempts,
                    url,
                    last_error,
                    delay,
                )
                time.sleep(delay)

        raise FetchError(f"GET {url} failed after {self.max_attempts} attempts: {last_error}") from last_error

    def _retry_delay(self, attempt: int, retry_after_header: str | None) -> float:
        if retry_after_header and retry_after_header.strip().isdigit():
            return float(retry_after_header.strip())
        return self.backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 1)

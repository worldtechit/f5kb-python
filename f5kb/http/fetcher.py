"""HTTP fetching with retry/backoff and per-request timeout via httpx."""

from __future__ import annotations

import re
import time
from typing import Callable

import httpx

from f5kb.lib.logger import Logger, NULL_LOGGER
from f5kb.version import USER_AGENT

REQUEST_TIMEOUT_S = 60.0
MAX_RETRIES = 5


def _default_sleep(s: float) -> None:
    time.sleep(s)


class HttpClient:
    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        logger: Logger = NULL_LOGGER,
        sleep: Callable[[float], None] = _default_sleep,
        timeout_s: float = REQUEST_TIMEOUT_S,
        user_agent: str = USER_AGENT,
    ) -> None:
        self._client = client or httpx.Client(follow_redirects=True, timeout=timeout_s)
        self._logger = logger
        self._sleep = sleep
        self._timeout_s = timeout_s
        self._user_agent = user_agent

    def fetch_text(self, url: str, attempt: int = 0) -> str:
        """GET url, return body text. Retries 5xx/429; terminal on 404/403/410."""
        headers = {"User-Agent": self._user_agent, "Accept": "text/html"}
        try:
            res = self._client.get(url, headers=headers)
            if not res.is_success:
                if (res.status_code >= 500 or res.status_code == 429) and attempt < MAX_RETRIES:
                    self._sleep(0.75 * (2 ** attempt))
                    return self.fetch_text(url, attempt + 1)
                raise RuntimeError(f"HTTP {res.status_code}")
            return res.text
        except httpx.HTTPStatusError:
            raise
        except (httpx.RequestError, RuntimeError) as e:
            msg = str(e)
            if attempt < MAX_RETRIES and not re.match(r"^HTTP \d", msg):
                self._sleep(0.75 * (2 ** attempt))
                return self.fetch_text(url, attempt + 1)
            raise

    def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
        """Low-level GET with caller-supplied headers. No retry or UA injection."""
        return self._client.get(url, headers=headers)

    def fetch_doc(self, url: str, attempt: int = 0) -> tuple[str, str]:
        """GET url following redirects. Returns (html, final_url)."""
        headers = {"User-Agent": self._user_agent, "Accept": "text/html"}
        try:
            res = self._client.get(url, headers=headers)
            if not res.is_success:
                if (res.status_code >= 500 or res.status_code == 429) and attempt < MAX_RETRIES:
                    self._sleep(0.75 * (2 ** attempt))
                    return self.fetch_doc(url, attempt + 1)
                raise RuntimeError(f"HTTP {res.status_code}")
            return res.text, str(res.url)
        except httpx.HTTPStatusError:
            raise
        except (httpx.RequestError, RuntimeError) as e:
            msg = str(e)
            if attempt < MAX_RETRIES and not re.match(r"^HTTP \d", msg):
                self._sleep(0.75 * (2 ** attempt))
                return self.fetch_doc(url, attempt + 1)
            raise

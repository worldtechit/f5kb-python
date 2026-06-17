"""GitHub REST/raw access for the F5_GitHub enricher."""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from f5kb.http.fetcher import HttpClient
from f5kb.version import USER_AGENT

MAX_RETRIES = 5


class GhTarget:
    def __init__(
        self,
        kind: str,
        api_path: str | None = None,
        raw_url: str | None = None,
    ) -> None:
        self.kind = kind  # "issue" | "pull" | "readme" | "file"
        self.api_path = api_path
        self.raw_url = raw_url


def parse_github_url(raw_url: str) -> GhTarget:
    u = urlparse(raw_url)
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"unrecognized GitHub URL: {raw_url}")
    owner, repo = parts[0], parts[1]
    rest = parts[2:]

    if not rest:
        return GhTarget("readme", api_path=f"/repos/{owner}/{repo}/readme")
    kind = rest[0]
    tail = rest[1:]

    if kind == "issues" and tail:
        return GhTarget("issue", api_path=f"/repos/{owner}/{repo}/issues/{tail[0]}")
    if kind == "pull" and tail:
        return GhTarget("pull", api_path=f"/repos/{owner}/{repo}/pulls/{tail[0]}")
    if kind == "blob" and len(tail) >= 2:
        ref = tail[0]
        path = "/".join(tail[1:])
        return GhTarget(
            "file",
            raw_url=f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}",
        )
    raise ValueError(f"unsupported GitHub URL shape: {raw_url}")


def github_headers(token: str | None, json: bool) -> dict[str, str]:
    h: dict[str, str] = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json" if json else "application/vnd.github.raw",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def github_api(
    path: str,
    token: str | None,
    http: HttpClient | None = None,
    attempt: int = 0,
) -> dict[str, Any]:
    """GET GitHub REST API, return parsed JSON. Retries 5xx/429."""
    url = f"https://api.github.com{path}"
    headers = github_headers(token, json=True)

    if http is not None:
        res = http.get(url, headers=headers)
    else:
        res = httpx.get(url, headers=headers, timeout=60.0)

    if res.is_success:
        return res.json()

    remaining = res.headers.get("x-ratelimit-remaining")
    if res.status_code == 403 and remaining == "0":
        if token:
            raise RuntimeError(
                "GitHub API rate limit exhausted (token present) — re-run later"
            )
        raise RuntimeError(
            "GitHub API rate limit hit (60/hr) — set GITHUB_TOKEN to raise to 5000/hr"
        )

    if (res.status_code >= 500 or res.status_code == 429) and attempt < MAX_RETRIES:
        time.sleep(0.75 * (2 ** attempt))
        return github_api(path, token, http, attempt + 1)

    raise RuntimeError(f"HTTP {res.status_code}")

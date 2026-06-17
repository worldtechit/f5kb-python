"""Per-type body enrichers — each receives an HttpClient via DI."""

from __future__ import annotations

import base64
import re
import sys
from typing import Callable, Any
from urllib.parse import urlparse

from f5kb.http.fetcher import HttpClient
from f5kb.http.github import github_api, parse_github_url
from f5kb.html.bugtracker import bug_tracker_url, parse_bug_content
from f5kb.html.docpage import HOST_RULES, extract_doc_body
from f5kb.html.nextdata import extract_next_data_body

# Keys written by enrich — cleared on re-enrich so no stale values remain.
STALE_KEYS = {"sections", "body_text", "bodyError", "bodySource", "fetchedAt"}


def has_body(content: dict | None) -> bool:
    bt = (content or {}).get("body_text")
    if isinstance(bt, str) and bt.strip():
        return True
    return isinstance((content or {}).get("bodyError"), str)


def enrich_bug_tracker(article: dict, now_iso: str, http: HttpClient, **_) -> dict:
    url = bug_tracker_url(article)
    html = http.fetch_text(url)
    sections = parse_bug_content(html)
    if not sections:
        raise ValueError("no body sections extracted")
    body_text = "\n\n".join(f"## {title}\n\n{text}" for title, text in sections.items())
    return {"sections": sections, "body_text": body_text, "bodySource": url, "fetchedAt": now_iso}


def enrich_github(article: dict, now_iso: str, http: HttpClient, github_token: str | None = None, **_) -> dict:
    url = article.get("link") or ""
    if not url:
        raise ValueError("no link to derive GitHub target")
    target = parse_github_url(url)
    if target.kind == "file":
        body = http.fetch_text(target.raw_url or "")
    elif target.kind == "readme":
        data = github_api(target.api_path or "", github_token, http)
        b64 = (data.get("content") or "").replace("\n", "")
        body = base64.b64decode(b64).decode("utf-8") if data.get("encoding") == "base64" else (data.get("content") or "")
    else:
        data = github_api(target.api_path or "", github_token, http)
        body = data.get("body") or ""

    body = body.strip()
    if not body:
        return {
            "bodySource": url,
            "fetchedAt": now_iso,
            "bodyError": f"empty GitHub {target.kind} body (no description)",
        }
    return {"sections": {target.kind: body}, "body_text": body, "bodySource": url, "fetchedAt": now_iso}


def enrich_doc_page(article: dict, now_iso: str, http: HttpClient, **_) -> dict:
    url = article.get("link") or ""
    if not url:
        raise ValueError("no link to fetch")
    host = urlparse(url).hostname or ""
    rule = HOST_RULES.get(host)
    if rule and getattr(rule, "js_rendered", False):
        return {
            "bodySource": url,
            "fetchedAt": now_iso,
            "bodyError": f"JS-rendered host {host}: body not in fetched HTML (needs headless browser)",
        }
    if not rule:
        print(f"  [doc] unmapped host: {host} (using generic fallback) — {url}", file=sys.stderr)

    html, final_url = http.fetch_doc(url)
    final_host = urlparse(final_url).hostname or ""

    if final_host == "my.f5.com" and host != "my.f5.com":
        km = re.search(r"/article/(K\d+)", final_url)
        return {
            "bodySource": final_url,
            "fetchedAt": now_iso,
            "bodyError": f"redirected into F5 KB {km.group(1) if km else final_url}; body captured under its Salesforce type",
        }

    req_path = urlparse(url).path
    fin_path = urlparse(final_url).path

    def _seg1(p: str) -> str:
        parts = [x for x in p.split("/") if x]
        return parts[0] if parts else ""

    if (
        final_url != url
        and fin_path.endswith("/")
        and (re.search(r"/[^/]+\.[a-z0-9]+$", req_path, re.I) or _seg1(req_path) != _seg1(fin_path))
    ):
        return {
            "bodySource": final_url,
            "fetchedAt": now_iso,
            "bodyError": f"redirected to landing page {final_url} (original page moved/removed)",
        }

    body_text = ""
    if rule and getattr(rule, "next_data", False):
        body_text = extract_next_data_body(html)

    if len(body_text) < 40:
        try:
            body_text = extract_doc_body(html, final_url, rule)
        except Exception as e:
            if not (rule and getattr(rule, "next_data", False)):
                raise

    if len(body_text) < 40:
        raise ValueError(f"extracted body too short ({len(body_text)} chars)")

    if re.match(r"^#{0,3}\s*404 - Page Not Found", body_text) or \
       re.search(r"the page you are looking for does not exist", body_text[:400], re.I):
        return {
            "bodySource": final_url,
            "fetchedAt": now_iso,
            "bodyError": "soft 404 (HTTP 200 'Page Not Found')",
        }

    return {"body_text": body_text, "bodySource": final_url, "fetchedAt": now_iso}


# Registry: type key -> enricher callable(article, now_iso, http, *, github_token=None)
TYPE_ENRICHERS: dict[str, Callable] = {
    "Bug_Tracker": enrich_bug_tracker,
    "F5_GitHub": enrich_github,
    "Manual": enrich_doc_page,
    "Release_Note": enrich_doc_page,
    "Supplemental_Document": enrich_doc_page,
}

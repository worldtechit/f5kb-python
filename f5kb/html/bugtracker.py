"""Bug Tracker body extraction."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from f5kb.html.serialize import is_hidden, node_to_markdown


def bug_tracker_url(article: dict[str, Any]) -> str:
    meta = article.get("metadata") or {}
    bug_id = meta.get("f5_bug_id")
    if isinstance(bug_id, str) and bug_id:
        return f"https://cdn.f5.com/product/bugtracker/ID{bug_id}.html"
    link = article.get("link")
    if link:
        return link
    raise ValueError("no f5_bug_id and no link to derive bug URL")


def parse_bug_content(html: str) -> dict[str, str]:
    doc = BeautifulSoup(html, "lxml")
    container = doc.select_one("div.bug-content")
    if container:
        return _parse_standard(container)
    mid = doc.select_one("div.middlecontent")
    if not mid:
        raise ValueError("bug-content container not found")
    fields = parse_labeled_fields(mid)
    return {
        label: value
        for label, value in fields.items()
        if re.search(r"CVE|Related Article|Vulnerability Severity", label, re.I)
        and value
    }


def _parse_standard(container: Tag) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if current is not None:
            text = "".join(buf).replace("\n\n\n", "\n\n").strip()
            if text:
                sections[current] = text

    for node in container.children:
        if isinstance(node, Tag) and is_hidden(node):
            continue
        if isinstance(node, Tag) and node.name and node.name.lower() == "h4":
            flush()
            buf.clear()
            current = (node.get_text() or "").strip()
            continue
        buf.append(node_to_markdown(node))

    flush()
    return sections


def parse_labeled_fields(root: Tag) -> dict[str, str]:
    """Parse '<span class=standard-field>Label:</span> value …' pairs."""
    fields: dict[str, str] = {}
    label: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if label is not None:
            v = " ".join("".join(buf).split()).strip()
            if v:
                fields[label] = v
        buf.clear()

    def walk(node: object) -> None:
        nonlocal label
        if isinstance(node, NavigableString):
            buf.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        if is_hidden(node):
            return
        cls = " ".join(node.get("class") or [])
        if "standard-field" in cls:
            flush()
            label = re.sub(r":\s*$", "", (node.get_text() or "")).strip()
            return
        if node.name and node.name.lower() == "a":
            href = (node.get("href") or "").strip()
            text = (node.get_text() or "").strip()
            buf.append(f"[{text}]({href})" if href else text)
            return
        for child in node.children:
            walk(child)

    walk(root)
    flush()
    return fields

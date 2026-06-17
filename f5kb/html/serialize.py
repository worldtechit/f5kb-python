"""HTML -> markdown serialization. Byte-for-byte equivalent of serialize.ts."""

from __future__ import annotations

import re
from typing import Callable
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag


def is_hidden(el: Tag) -> bool:
    style = (el.get("style") or "").replace(" ", "")
    return bool(re.search(r"display:none", style, re.I))


def resolve_url(href: str, base: str | None = None) -> str:
    if not base:
        return href
    try:
        return urljoin(base, href)
    except Exception:
        return href


def make_serializer(base_url: str | None = None) -> Callable[[object], str]:
    """Build a serializer that converts a BS4 node subtree to compact markdown."""

    def serialize(node: object) -> str:
        if isinstance(node, NavigableString):
            return str(node).replace("\n", " ").replace("\t", " ")
            # collapse multiple spaces but preserve single space
            # (TS does .replace(/\s+/g, " ") — collapse all whitespace to one space)
        if not isinstance(node, Tag):
            return ""
        el: Tag = node
        if is_hidden(el):
            return ""
        tag = el.name
        if tag is None:
            return ""
        tag = tag.lower()

        def inner() -> str:
            return "".join(serialize(c) for c in el.children)

        if tag in ("script", "style", "noscript"):
            return ""
        if tag == "br":
            return "\n"
        if tag == "hr":
            return "\n---\n\n"
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            t = " ".join(inner().split()).strip()
            return f"\n{'#' * level} {t}\n\n" if t else ""
        if tag == "a":
            href = (el.get("href") or "").strip()
            text = inner().strip()
            if not text:
                return ""
            return f"[{text}]({resolve_url(href, base_url)})" if href else text
        if tag == "img":
            alt = (el.get("alt") or "").strip()
            src = (el.get("src") or "").strip()
            return f"![{alt}]({resolve_url(src, base_url)})" if src else ""
        if tag in ("b", "strong"):
            return f"**{inner().strip()}**"
        if tag in ("i", "em"):
            return f"*{inner().strip()}*"
        if tag == "code":
            return f"`{inner().strip()}`"
        if tag == "pre":
            code = (el.get_text() or "").rstrip("\n")
            return f"\n```\n{code}\n```\n\n" if code.strip() else ""
        if tag == "blockquote":
            return f"> {inner().strip().replace(chr(10), chr(10) + '> ')}\n\n"
        if tag == "li":
            return f"- {' '.join(inner().split()).strip()}\n"
        if tag in ("ul", "ol"):
            return f"{inner()}\n"
        if tag == "tr":
            cells = [serialize(c).strip() for c in el.children if isinstance(c, Tag)]
            return " | ".join(cells) + "\n"
        if tag in ("th", "td"):
            return " ".join(inner().split()).strip()
        if tag in ("table", "thead", "tbody"):
            return f"{inner()}\n"
        if tag in ("p", "div", "section"):
            return f"{inner().strip()}\n\n"
        return inner()

    return serialize


# Back-compat alias (bug tracker uses absolute links, no base needed)
node_to_markdown = make_serializer()

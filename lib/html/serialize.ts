// HTML -> markdown serialization for inline/block body content. Moved VERBATIM
// from enrich_bodies.ts — this governs the body_text output, so whitespace and
// tag handling are intentionally byte-for-byte identical to the original.

import { type Element, type Node } from "@b-fuze/deno-dom";

// deno-dom node type constants
export const ELEMENT_NODE = 1;
export const TEXT_NODE = 3;

export function isElement(n: Node): n is Element {
  return n.nodeType === ELEMENT_NODE;
}

export function isHidden(el: Element): boolean {
  const style = (el.getAttribute("style") ?? "").replace(/\s+/g, "");
  return /display:none/i.test(style);
}

// Resolve a possibly-relative URL against the page it came from.
export function resolveUrl(href: string, base?: string): string {
  if (!base) return href;
  try {
    return new URL(href, base).href;
  } catch {
    return href;
  }
}

// Build a serializer that turns a node subtree into compact markdown, preserving
// structure (headings, lists, code blocks, links, images) while dropping pure
// presentation. `baseUrl` resolves relative links/images when given.
export function makeSerializer(baseUrl?: string): (node: Node) => string {
  const serialize = (node: Node): string => {
    if (node.nodeType === TEXT_NODE) {
      return (node.textContent ?? "").replace(/\s+/g, " ");
    }
    if (!isElement(node)) return "";
    const el = node;
    if (isHidden(el)) return "";
    const tag = el.tagName.toLowerCase();
    const inner = () => Array.from(el.childNodes).map(serialize).join("");
    switch (tag) {
      case "script":
      case "style":
      case "noscript":
        return "";
      case "br":
        return "\n";
      case "hr":
        return "\n---\n\n";
      case "h1":
      case "h2":
      case "h3":
      case "h4":
      case "h5":
      case "h6": {
        const t = inner().replace(/\s+/g, " ").trim();
        return t ? `\n${"#".repeat(+tag[1])} ${t}\n\n` : "";
      }
      case "a": {
        const href = el.getAttribute("href") ?? "";
        const text = inner().trim();
        if (!text) return "";
        return href ? `[${text}](${resolveUrl(href, baseUrl)})` : text;
      }
      case "img": {
        const alt = (el.getAttribute("alt") ?? "").trim();
        const src = el.getAttribute("src") ?? "";
        return src ? `![${alt}](${resolveUrl(src, baseUrl)})` : "";
      }
      case "b":
      case "strong":
        return `**${inner().trim()}**`;
      case "i":
      case "em":
        return `*${inner().trim()}*`;
      case "code":
        // Inline code only; <pre> handles block code below.
        return `\`${inner().trim()}\``;
      case "pre": {
        const code = (el.textContent ?? "").replace(/\n+$/, "");
        return code.trim() ? `\n\`\`\`\n${code}\n\`\`\`\n\n` : "";
      }
      case "blockquote":
        return `> ${inner().trim().replace(/\n/g, "\n> ")}\n\n`;
      case "li":
        return `- ${inner().replace(/\s+/g, " ").trim()}\n`;
      case "ul":
      case "ol":
        return `${inner()}\n`;
      case "tr":
        return `${Array.from(el.children).map((c) => serialize(c).trim()).join(" | ")}\n`;
      case "th":
      case "td":
        return inner().replace(/\s+/g, " ").trim();
      case "table":
      case "thead":
      case "tbody":
        return `${inner()}\n`;
      case "p":
      case "div":
      case "section":
        return `${inner().trim()}\n\n`;
      default:
        return inner();
    }
  };
  return serialize;
}

// Back-compat alias for the Bug Tracker extractor (links there are absolute).
export const nodeToMarkdown = makeSerializer();

// Bug Tracker body extraction. Moved VERBATIM from enrich_bodies.ts.
//
// The body is the labelled sections inside <div class="bug-content">
// (Symptoms / Conditions / Impact / Workaround / Fix Information / Behavior
// Change / Guides & references). Everything above it (Affected Product(s),
// Known Affected Versions, Opened, Severity, Last Modified) duplicates the
// metadata we already have, and the site header/footer are outside it — so we
// extract ONLY this container.

import { DOMParser, type Element, type Node } from "@b-fuze/deno-dom";
import { isElement, isHidden, nodeToMarkdown, TEXT_NODE } from "./serialize.ts";

interface BugArticle {
  link?: string;
  metadata?: Record<string, unknown>;
}

export function bugTrackerUrl(a: BugArticle): string {
  const bugId = a.metadata?.["f5_bug_id"];
  if (typeof bugId === "string" && bugId) {
    return `https://cdn.f5.com/product/bugtracker/ID${bugId}.html`;
  }
  if (a.link) return a.link;
  throw new Error("no f5_bug_id and no link to derive bug URL");
}

export function parseBugContent(html: string): Record<string, string> {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const container = doc?.querySelector("div.bug-content");
  if (container) {
    // Standard template: <h4> section + content (Symptoms/Impact/...).
    const sections: Record<string, string> = {};
    let current: string | null = null;
    let buf = "";
    const flush = () => {
      if (current) {
        const text = buf.replace(/\n{3,}/g, "\n\n").trim();
        if (text) sections[current] = text;
      }
      buf = "";
    };
    for (const node of Array.from(container.childNodes)) {
      if (isElement(node) && isHidden(node)) continue; // hidden Behavior Change etc.
      if (isElement(node) && node.tagName.toLowerCase() === "h4") {
        flush();
        current = (node.textContent ?? "").trim();
        continue;
      }
      buf += nodeToMarkdown(node);
    }
    flush();
    return sections;
  }

  // Vulnerability/CVE template: no bug-content div; the body is labelled fields
  // in <div class="middlecontent">. Keep only the ones that aren't already in
  // the dump metadata (CVE list, Related Article, Vulnerability Severity); skip
  // Affected Product(s)/Opened/Last Modified, which duplicate metadata.
  const mid = doc?.querySelector("div.middlecontent");
  if (!mid) throw new Error("bug-content container not found");
  const fields = parseLabeledFields(mid);
  const sections: Record<string, string> = {};
  for (const [label, value] of Object.entries(fields)) {
    if (/CVE|Related Article|Vulnerability Severity/i.test(label) && value) {
      sections[label] = value;
    }
  }
  return sections;
}

// Parse "<span class=standard-field>Label:</span> value …" pairs from a subtree.
// Each standard-field span starts a new field; following text/links (until the
// next such span) are its value (links preserved as markdown).
export function parseLabeledFields(root: Element): Record<string, string> {
  const fields: Record<string, string> = {};
  let label: string | null = null;
  let buf = "";
  const flush = () => {
    if (label) {
      const v = buf.replace(/\s+/g, " ").trim();
      if (v) fields[label] = v;
    }
    buf = "";
  };
  const walk = (node: Node) => {
    if (node.nodeType === TEXT_NODE) {
      buf += node.textContent ?? "";
      return;
    }
    if (!isElement(node)) return;
    const el = node;
    if (isHidden(el)) return;
    const cls = el.getAttribute("class") ?? "";
    if (cls.includes("standard-field")) {
      flush();
      label = (el.textContent ?? "").replace(/:\s*$/, "").trim();
      return; // label text consumed; its value follows
    }
    if (el.tagName.toLowerCase() === "a") {
      const href = el.getAttribute("href") ?? "";
      const text = (el.textContent ?? "").trim();
      buf += href ? `[${text}](${href})` : text;
      return;
    }
    for (const c of Array.from(el.childNodes)) walk(c);
  };
  walk(root);
  flush();
  return fields;
}

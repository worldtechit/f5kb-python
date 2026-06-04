// Doc-page body extraction (Manual / Release Note / Supplemental Document).
// Moved VERBATIM from enrich_bodies.ts.
//
// These types have no body in the search index; the body lives on the rendered
// doc page each `link` points to. Different doc-site generators wrap the body in
// a different container, so we map host -> content selector and extract ONLY
// that container, stripping in-page nav/sidebar/footer. Site header/footer live
// outside the container and the article title/dates are already in metadata.

import { DOMParser, type Element } from "@b-fuze/deno-dom";
import { makeSerializer } from "./serialize.ts";

export interface HostRule {
  /** CSS selector(s) for the main content container, tried in order. */
  selectors: string[];
  /** True when the body is client-rendered (not in the fetched HTML). */
  jsRendered?: boolean;
  /**
   * True for Next.js sites that embed the body in a <script id="__NEXT_DATA__">
   * JSON blob rather than (only) the rendered DOM. We parse that JSON instead of
   * scraping elements — no headless browser needed.
   */
  nextData?: boolean;
}

// Hosts observed across Manual/Release_Note/Supplemental_Document. Add new hosts
// here as the corpus widens (run with logging to surface unmapped ones).
export const HOST_RULES: Record<string, HostRule> = {
  "clouddocs.f5.com": { selectors: ["[role=main]", "article.docs-container"] },
  "techdocs.f5.com": { selectors: ["div.pageContent", "div.manual-chapter", "main"] },
  "docs.nginx.com": { selectors: ["[data-testid=content]", "main.content", "article"] },
  // nginx.org (open-source NGINX docs) and unit.nginx.org (NGINX Unit, Sphinx)
  // both keep the body in #content.
  "nginx.org": { selectors: ["#content", "#main"] },
  "unit.nginx.org": { selectors: ["#content", "div.body", "#main"] },
  // docs.cloud.f5.com (Next.js) renders the article body client-side, so it is
  // NOT in the rendered DOM of an API page. But the body IS embedded in the
  // page's <script id="__NEXT_DATA__"> JSON (docData.compiledSource for prose
  // pages, docData.swaggerFile for API pages) — so a plain fetch + JSON parse
  // recovers it without a headless browser. selectors are a DOM fallback.
  "docs.cloud.f5.com": { selectors: ["main"], nextData: true },
};

// Generic fallback for unmapped hosts.
export const GENERIC_SELECTORS = ["main", "article", "[role=main]", "#main-content", "div.content"];

// Descendants to remove from the chosen container before serializing: in-page
// navigation, sidebars, breadcrumbs, prev/next, edit/feedback widgets, etc.
export const STRIP_SELECTORS = [
  "nav",
  "header",
  "footer",
  "aside",
  "script",
  "style",
  "noscript",
  "form",
  ".next-prev-btn-row",
  ".document-navigation",
  ".doc-nav",
  ".site-breadcrumb-nav",
  "[class*=breadcrumb]",
  "[class*=pagination]",
  "[class*=edit-on]",
  "[class*=feedback]",
  "[aria-label*=breadcrumb]",
  "[aria-label*=pagination]",
  "button",
  ".headerlink",
  "a.headerlink", // Sphinx ¶ heading permalinks (clouddocs)
];

export function selectContainer(
  doc: ReturnType<DOMParser["parseFromString"]>,
  rule: HostRule | undefined,
): Element | null {
  const selectors = rule && rule.selectors.length ? rule.selectors : GENERIC_SELECTORS;
  for (const sel of selectors) {
    let el: Element | null = null;
    try {
      el = doc?.querySelector(sel) ?? null;
    } catch { /* bad selector */ }
    if (el && (el.textContent ?? "").trim().length > 0) return el;
  }
  return null;
}

export function extractDocBody(html: string, finalUrl: string, rule: HostRule | undefined): string {
  const doc = new DOMParser().parseFromString(html, "text/html");
  let container = selectContainer(doc, rule);
  if (!container) {
    // Last resort for plain/odd pages with no standard container (e.g. nginx.org
    // changelog text files and directory listings): a substantial <pre>, else the
    // whole <body>. STRIP_SELECTORS below removes any nav/header/footer chrome.
    const pre = doc?.querySelector("pre");
    container = (pre && (pre.textContent ?? "").trim().length > 200)
      ? pre
      : (doc?.querySelector("body") ?? null);
  }
  if (!container) throw new Error("content container not found");
  // Remove in-page chrome from a clone-free container (deno-dom mutates in place,
  // which is fine — we discard the doc after).
  for (const sel of STRIP_SELECTORS) {
    let nodes;
    try {
      nodes = container.querySelectorAll(sel);
    } catch {
      continue;
    }
    for (const n of nodes as unknown as Element[]) n.remove();
  }
  const md = makeSerializer(finalUrl)(container);
  return md.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}

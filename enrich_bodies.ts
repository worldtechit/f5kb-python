/**
 * Enriches an existing article dump with article BODIES for the document types
 * whose body is not present in the Coveo search index (so dump_articles.ts left
 * their `content` object empty).
 *
 * The real body lives on each article's public rendered page. This post-processor
 * walks a dump directory, fetches those pages, extracts ONLY the article body
 * (no site header/footer/nav, and nothing that merely repeats the metadata the
 * dump already has), and writes it back into each article JSON's `content`.
 *
 * Status: Bug Tracker (deterministic static page on cdn.f5.com) and F5 GitHub
 * (GitHub REST API) are implemented. The remaining empty-body types (Manual,
 * Release Note, Supplemental Document) are stubbed in TYPE_ENRICHERS and not yet
 * wired up — see TODO.txt.
 *
 * For F5 GitHub, set GITHUB_TOKEN (and pass --allow-env) to raise the GitHub API
 * limit from 60 to 5,000 requests/hour; without it the run still works but is
 * rate-limited.
 *
 * Usage:
 *   deno run --allow-net --allow-read --allow-write --allow-env enrich_bodies.ts \
 *       --dump=outputs/dump [--types="Bug_Tracker,F5_GitHub"] [--concurrency=4] \
 *       [--limit=N] [--refetch]
 *
 * Options:
 *   --dump=DIR       Dump directory produced by dump_articles.ts
 *                    (default: outputs/dump).
 *   --types="A,B"    Subset of type keys to enrich (default: all implemented).
 *   --concurrency=N  Parallel fetches (default: 4). Be polite.
 *   --delay-ms=N     Min delay between requests per worker (default: 200).
 *   --limit=N        Cap articles processed per type (default: no cap).
 *   --refetch        Re-fetch even if the article already has a body / error.
 *
 * Resumability: by default an article is skipped if its content already has a
 * non-empty `body_text` or a recorded `bodyError`, so a re-run only fills gaps.
 */

import { DOMParser, type Element, type Node } from "jsr:@b-fuze/deno-dom";

const USER_AGENT =
  "f5-articles-indexer/1.0 (personal KB archival; contact ryanparas@yahoo.com)";

// ---------------------------------------------------------------------------
// CLI args
// ---------------------------------------------------------------------------
interface Args {
  dump: string;
  types: string[] | null;
  concurrency: number;
  delayMs: number;
  limit: number | null;
  refetch: boolean;
}

function parseArgs(): Args {
  const a: Args = {
    dump: "outputs/dump",
    types: null,
    concurrency: 4,
    delayMs: 200,
    limit: null,
    refetch: false,
  };
  for (const arg of Deno.args) {
    const [k, v] = arg.startsWith("--") ? arg.slice(2).split("=") : ["", ""];
    switch (k) {
      case "dump":
        a.dump = v;
        break;
      case "types":
        a.types = v.split(",").map((s) => s.trim()).filter(Boolean);
        break;
      case "concurrency":
        a.concurrency = Math.max(1, parseInt(v, 10) || 4);
        break;
      case "delay-ms":
        a.delayMs = Math.max(0, parseInt(v, 10) || 0);
        break;
      case "limit":
        a.limit = parseInt(v, 10) || null;
        break;
      case "refetch":
        a.refetch = true;
        break;
      default:
        if (arg.startsWith("--")) {
          console.error(`Unknown flag: ${arg}`);
          Deno.exit(2);
        }
    }
  }
  return a;
}

// ---------------------------------------------------------------------------
// Article JSON shape (only the fields we touch)
// ---------------------------------------------------------------------------
interface Article {
  id: string;
  documentType: string;
  title?: string;
  link?: string;
  metadata?: Record<string, unknown>;
  content?: Record<string, unknown>;
  [k: string]: unknown;
}

interface EnrichResult {
  /** Section title -> body markdown, in document order. */
  sections?: Record<string, string>;
  /** Full readable body (sections joined). */
  body_text?: string;
  /** Where the body came from. */
  bodySource: string;
  /** ISO time of the fetch attempt. */
  fetchedAt: string;
  /** Set instead of body when the page could not be fetched/parsed. */
  bodyError?: string;
}

// An enricher turns one article into a body (or throws → recorded as bodyError).
type Enricher = (article: Article, nowIso: string) => Promise<EnrichResult>;

// ---------------------------------------------------------------------------
// HTTP with retry/backoff (mirrors dump_articles.ts coveoPost behavior)
// ---------------------------------------------------------------------------
async function fetchText(url: string, attempt = 0): Promise<string> {
  const MAX_RETRIES = 5;
  try {
    const res = await fetch(url, {
      headers: { "User-Agent": USER_AGENT, Accept: "text/html" },
    });
    if (!res.ok) {
      // 404/403/410 are terminal (page gone / restricted) — don't retry.
      if ((res.status >= 500 || res.status === 429) && attempt < MAX_RETRIES) {
        await sleep(750 * 2 ** attempt);
        return fetchText(url, attempt + 1);
      }
      // Drain body to free the connection before throwing.
      await res.body?.cancel();
      throw new Error(`HTTP ${res.status}`);
    }
    return await res.text();
  } catch (e) {
    const msg = (e as Error).message ?? "";
    // Retry network-level failures, not HTTP errors we already classified.
    if (attempt < MAX_RETRIES && !/^HTTP \d/.test(msg)) {
      await sleep(750 * 2 ** attempt);
      return fetchText(url, attempt + 1);
    }
    throw e;
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// GitHub token (optional) — set in main() from env. Raises the API limit from
// 60 to 5,000 req/hr.
let GITHUB_TOKEN: string | undefined;

function githubHeaders(json: boolean): HeadersInit {
  const h: Record<string, string> = {
    "User-Agent": USER_AGENT,
    Accept: json ? "application/vnd.github+json" : "application/vnd.github.raw",
  };
  if (GITHUB_TOKEN) h.Authorization = `Bearer ${GITHUB_TOKEN}`;
  return h;
}

// GET the GitHub REST API and return parsed JSON. Retries 5xx/429/secondary
// rate limits; surfaces a clear message on primary rate-limit exhaustion.
async function githubApi(
  path: string,
  attempt = 0,
): Promise<Record<string, unknown>> {
  const MAX_RETRIES = 5;
  const res = await fetch(`https://api.github.com${path}`, {
    headers: githubHeaders(true),
  });
  if (res.ok) return await res.json();
  const remaining = res.headers.get("x-ratelimit-remaining");
  await res.body?.cancel();
  // Primary rate limit hit and we'd have to wait an hour — fail loudly so the
  // user knows to set GITHUB_TOKEN. (Resumable: re-run later fills the rest.)
  if (res.status === 403 && remaining === "0") {
    throw new Error(
      GITHUB_TOKEN
        ? "GitHub API rate limit exhausted (token present) — re-run later"
        : "GitHub API rate limit hit (60/hr) — set GITHUB_TOKEN to raise to 5000/hr",
    );
  }
  if ((res.status >= 500 || res.status === 429) && attempt < MAX_RETRIES) {
    await sleep(750 * 2 ** attempt);
    return githubApi(path, attempt + 1);
  }
  throw new Error(`HTTP ${res.status}`);
}

// ---------------------------------------------------------------------------
// HTML -> markdown for inline/block body content
// ---------------------------------------------------------------------------
// deno-dom node type constants
const ELEMENT_NODE = 1;
const TEXT_NODE = 3;

function isElement(n: Node): n is Element {
  return n.nodeType === ELEMENT_NODE;
}

function isHidden(el: Element): boolean {
  const style = (el.getAttribute("style") ?? "").replace(/\s+/g, "");
  return /display:none/i.test(style);
}

// Resolve a possibly-relative URL against the page it came from.
function resolveUrl(href: string, base?: string): string {
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
function makeSerializer(baseUrl?: string): (node: Node) => string {
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
        return `${
          Array.from(el.children).map((c) => serialize(c).trim()).join(" | ")
        }\n`;
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
const nodeToMarkdown = makeSerializer();

// ---------------------------------------------------------------------------
// Bug Tracker enricher
// ---------------------------------------------------------------------------
// The body is the labelled sections inside <div class="bug-content">
// (Symptoms / Conditions / Impact / Workaround / Fix Information / Behavior
// Change / Guides & references). Everything above it (Affected Product(s),
// Known Affected Versions, Opened, Severity, Last Modified) duplicates the
// metadata we already have, and the site header/footer are outside it — so we
// extract ONLY this container.
function bugTrackerUrl(a: Article): string {
  const bugId = a.metadata?.["f5_bug_id"];
  if (typeof bugId === "string" && bugId) {
    return `https://cdn.f5.com/product/bugtracker/ID${bugId}.html`;
  }
  if (a.link) return a.link;
  throw new Error("no f5_bug_id and no link to derive bug URL");
}

function parseBugContent(html: string): Record<string, string> {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const container = doc?.querySelector("div.bug-content");
  if (!container) throw new Error("bug-content container not found");

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

const enrichBugTracker: Enricher = (article, nowIso) => {
  const url = bugTrackerUrl(article);
  return fetchText(url).then((html) => {
    const sections = parseBugContent(html);
    if (Object.keys(sections).length === 0) {
      throw new Error("no body sections extracted");
    }
    const body_text = Object.entries(sections)
      .map(([title, text]) => `## ${title}\n\n${text}`)
      .join("\n\n");
    return { sections, body_text, bodySource: url, fetchedAt: nowIso };
  });
};

// ---------------------------------------------------------------------------
// F5 GitHub enricher
// ---------------------------------------------------------------------------
// The article body is the GitHub object's own description: the issue/PR body
// markdown, a repo's README, or a referenced file's contents. The title and
// author already live in the dump metadata, so we extract ONLY the body.
interface GhTarget {
  kind: "issue" | "pull" | "readme" | "file";
  apiPath?: string; // for issue/pull/readme (JSON API)
  rawUrl?: string; // for file (raw.githubusercontent.com)
}

function parseGithubUrl(rawUrl: string): GhTarget {
  const u = new URL(rawUrl);
  const parts = u.pathname.split("/").filter(Boolean);
  const [owner, repo, kind, ...rest] = parts;
  if (!owner || !repo) throw new Error(`unrecognized GitHub URL: ${rawUrl}`);
  if (kind === "issues" && rest[0]) {
    return { kind: "issue", apiPath: `/repos/${owner}/${repo}/issues/${rest[0]}` };
  }
  if (kind === "pull" && rest[0]) {
    return { kind: "pull", apiPath: `/repos/${owner}/${repo}/pulls/${rest[0]}` };
  }
  if (kind === "blob" && rest.length >= 2) {
    const ref = rest[0];
    const path = rest.slice(1).join("/");
    return {
      kind: "file",
      rawUrl: `https://raw.githubusercontent.com/${owner}/${repo}/${ref}/${path}`,
    };
  }
  if (!kind) {
    return { kind: "readme", apiPath: `/repos/${owner}/${repo}/readme` };
  }
  throw new Error(`unsupported GitHub URL shape: ${rawUrl}`);
}

const enrichGithub: Enricher = async (article, nowIso) => {
  const url = article.link;
  if (!url) throw new Error("no link to derive GitHub target");
  const target = parseGithubUrl(url);

  let body: string;
  if (target.kind === "file") {
    body = await fetchText(target.rawUrl!);
  } else if (target.kind === "readme") {
    const data = await githubApi(target.apiPath!);
    const b64 = (data.content as string ?? "").replace(/\n/g, "");
    body = data.encoding === "base64" ? atob(b64) : (data.content as string ?? "");
  } else {
    const data = await githubApi(target.apiPath!);
    body = (data.body as string) ?? "";
  }

  body = body.trim();
  if (!body) {
    // A real but empty description (some PRs have none). Record it as a benign
    // marker so a re-run skips it instead of refetching forever.
    return {
      bodySource: url,
      fetchedAt: nowIso,
      bodyError: `empty GitHub ${target.kind} body (no description)`,
    };
  }
  return {
    sections: { [target.kind]: body },
    body_text: body,
    bodySource: url,
    fetchedAt: nowIso,
  };
};

// ---------------------------------------------------------------------------
// Doc-page enricher (Manual / Release Note / Supplemental Document)
// ---------------------------------------------------------------------------
// These types have no body in the search index; the body lives on the rendered
// doc page each `link` points to. Different doc-site generators wrap the body in
// a different container, so we map host -> content selector and extract ONLY
// that container, stripping in-page nav/sidebar/footer. Site header/footer live
// outside the container and the article title/dates are already in metadata.
interface HostRule {
  /** CSS selector(s) for the main content container, tried in order. */
  selectors: string[];
  /** True when the body is client-rendered (not in the fetched HTML). */
  jsRendered?: boolean;
}

// Hosts observed across Manual/Release_Note/Supplemental_Document. Add new hosts
// here as the corpus widens (run with logging to surface unmapped ones).
const HOST_RULES: Record<string, HostRule> = {
  "clouddocs.f5.com": { selectors: ["[role=main]", "article.docs-container"] },
  "techdocs.f5.com": { selectors: ["div.pageContent", "div.manual-chapter", "main"] },
  "docs.nginx.com": { selectors: ["[data-testid=content]", "main.content", "article"] },
  // docs.cloud.f5.com server-renders only its nav menu; the article body is
  // injected client-side, so a plain fetch can't see it (needs a headless
  // browser — out of scope for this scraper).
  "docs.cloud.f5.com": { selectors: [], jsRendered: true },
};

// Generic fallback for unmapped hosts.
const GENERIC_SELECTORS = ["main", "article", "[role=main]", "#main-content", "div.content"];

// Descendants to remove from the chosen container before serializing: in-page
// navigation, sidebars, breadcrumbs, prev/next, edit/feedback widgets, etc.
const STRIP_SELECTORS = [
  "nav", "header", "footer", "aside", "script", "style", "noscript", "form",
  ".next-prev-btn-row", ".document-navigation", ".doc-nav", ".site-breadcrumb-nav",
  "[class*=breadcrumb]", "[class*=pagination]", "[class*=edit-on]", "[class*=feedback]",
  "[aria-label*=breadcrumb]", "[aria-label*=pagination]", "button",
  ".headerlink", "a.headerlink", // Sphinx ¶ heading permalinks (clouddocs)
];

// Fetch a page following redirects, returning the body and the final URL.
async function fetchDoc(
  url: string,
  attempt = 0,
): Promise<{ html: string; finalUrl: string }> {
  const MAX_RETRIES = 5;
  try {
    const res = await fetch(url, {
      headers: { "User-Agent": USER_AGENT, Accept: "text/html" },
      redirect: "follow",
    });
    if (!res.ok) {
      if ((res.status >= 500 || res.status === 429) && attempt < MAX_RETRIES) {
        await res.body?.cancel();
        await sleep(750 * 2 ** attempt);
        return fetchDoc(url, attempt + 1);
      }
      await res.body?.cancel();
      throw new Error(`HTTP ${res.status}`);
    }
    return { html: await res.text(), finalUrl: res.url || url };
  } catch (e) {
    const msg = (e as Error).message ?? "";
    if (attempt < MAX_RETRIES && !/^HTTP \d/.test(msg)) {
      await sleep(750 * 2 ** attempt);
      return fetchDoc(url, attempt + 1);
    }
    throw e;
  }
}

function selectContainer(doc: ReturnType<DOMParser["parseFromString"]>, rule: HostRule | undefined): Element | null {
  const selectors = rule && rule.selectors.length ? rule.selectors : GENERIC_SELECTORS;
  for (const sel of selectors) {
    let el: Element | null = null;
    try { el = doc?.querySelector(sel) ?? null; } catch { /* bad selector */ }
    if (el && (el.textContent ?? "").trim().length > 0) return el;
  }
  return null;
}

function extractDocBody(html: string, finalUrl: string, rule: HostRule | undefined): string {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const container = selectContainer(doc, rule);
  if (!container) throw new Error("content container not found");
  // Remove in-page chrome from a clone-free container (deno-dom mutates in place,
  // which is fine — we discard the doc after).
  for (const sel of STRIP_SELECTORS) {
    let nodes; try { nodes = container.querySelectorAll(sel); } catch { continue; }
    for (const n of nodes as unknown as Element[]) n.remove();
  }
  const md = makeSerializer(finalUrl)(container);
  return md.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}

const enrichDocPage: Enricher = async (article, nowIso) => {
  const url = article.link;
  if (!url) throw new Error("no link to fetch");
  const host = new URL(url).hostname;
  const rule = HOST_RULES[host];
  if (rule?.jsRendered) {
    return {
      bodySource: url,
      fetchedAt: nowIso,
      bodyError: `JS-rendered host ${host}: body not in fetched HTML (needs headless browser)`,
    };
  }
  if (!rule) console.warn(`  [doc] unmapped host: ${host} (using generic fallback) — ${url}`);

  const { html, finalUrl } = await fetchDoc(url);
  const body_text = extractDocBody(html, finalUrl, rule);
  if (body_text.length < 40) {
    throw new Error(`extracted body too short (${body_text.length} chars)`);
  }
  return { body_text, bodySource: finalUrl, fetchedAt: nowIso };
};

// ---------------------------------------------------------------------------
// Registry: type key (dump subdir name) -> enricher
// ---------------------------------------------------------------------------
const TYPE_ENRICHERS: Record<string, Enricher> = {
  Bug_Tracker: enrichBugTracker,
  F5_GitHub: enrichGithub,
  Manual: enrichDocPage,
  Release_Note: enrichDocPage,
  Supplemental_Document: enrichDocPage,
};

// ---------------------------------------------------------------------------
// Driver
// ---------------------------------------------------------------------------
function hasBody(content: Record<string, unknown> | undefined): boolean {
  if (!content) return false;
  const bt = content["body_text"];
  return (typeof bt === "string" && bt.trim().length > 0) ||
    typeof content["bodyError"] === "string";
}

async function listArticleFiles(dir: string): Promise<string[]> {
  const files: string[] = [];
  for await (const entry of Deno.readDir(dir)) {
    if (!entry.isFile || !entry.name.endsWith(".json")) continue;
    if (entry.name.startsWith("_")) continue; // _catalogue.json, _index.json
    files.push(`${dir}/${entry.name}`);
  }
  files.sort();
  return files;
}

// Run async tasks with a fixed concurrency, returning when all are done.
async function runPool<T>(
  items: T[],
  concurrency: number,
  worker: (item: T) => Promise<void>,
): Promise<void> {
  let idx = 0;
  const runners = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
    while (idx < items.length) {
      const i = idx++;
      await worker(items[i]);
    }
  });
  await Promise.all(runners);
}

async function enrichType(typeKey: string, args: Args) {
  const enricher = TYPE_ENRICHERS[typeKey];
  const dir = `${args.dump}/${typeKey}`;
  let files: string[];
  try {
    files = await listArticleFiles(dir);
  } catch {
    console.log(`  [${typeKey}] no directory ${dir} — skipping`);
    return;
  }
  if (args.limit) files = files.slice(0, args.limit);

  let done = 0, skipped = 0, ok = 0, failed = 0;
  const nowIso = new Date().toISOString();

  await runPool(files, args.concurrency, async (file) => {
    const article: Article = JSON.parse(await Deno.readFile(file).then((b) =>
      new TextDecoder().decode(b)
    ));
    if (!args.refetch && hasBody(article.content)) {
      skipped++;
      return;
    }
    let result: EnrichResult;
    try {
      result = await enricher(article, nowIso);
      ok++;
    } catch (e) {
      result = {
        bodySource: article.link ?? "",
        fetchedAt: nowIso,
        bodyError: (e as Error).message,
      };
      failed++;
    }
    article.content = { ...(article.content ?? {}), ...result };
    await Deno.writeTextFile(file, JSON.stringify(article, null, 2) + "\n");
    done++;
    if ((done + skipped) % 25 === 0) {
      console.log(
        `  [${typeKey}] ${done + skipped}/${files.length} (ok=${ok} fail=${failed} skip=${skipped})`,
      );
    }
    if (args.delayMs) await sleep(args.delayMs);
  });

  console.log(
    `  [${typeKey}] DONE: ${files.length} files — enriched=${ok} failed=${failed} skipped=${skipped}`,
  );
}

async function main() {
  const args = parseArgs();
  try {
    GITHUB_TOKEN = Deno.env.get("GITHUB_TOKEN") || undefined;
  } catch {
    // --allow-env not granted; GitHub enrichment will run unauthenticated.
    GITHUB_TOKEN = undefined;
  }
  const requested = args.types ?? Object.keys(TYPE_ENRICHERS);
  const toRun = requested.filter((t) => {
    if (!TYPE_ENRICHERS[t]) {
      console.error(`  [${t}] no enricher implemented — skipping`);
      return false;
    }
    return true;
  });
  if (toRun.length === 0) {
    console.error("Nothing to do. Implemented types: " + Object.keys(TYPE_ENRICHERS).join(", "));
    Deno.exit(1);
  }
  console.log(`Enriching bodies in ${args.dump} for: ${toRun.join(", ")}`);
  console.log(`(concurrency=${args.concurrency}, delay=${args.delayMs}ms, refetch=${args.refetch})`);
  if (toRun.includes("F5_GitHub")) {
    console.log(`GitHub auth: ${GITHUB_TOKEN ? "token present (5000/hr)" : "UNAUTHENTICATED (60/hr) — set GITHUB_TOKEN to raise"}`);
  }
  for (const t of toRun) await enrichType(t, args);
  console.log("All done.");
}

if (import.meta.main) await main();

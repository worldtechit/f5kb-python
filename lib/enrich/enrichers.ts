// Per-type body enrichers. Logic moved VERBATIM from enrich_bodies.ts; the only
// change is dependency-injection — each enricher receives an HttpClient (and the
// GitHub enricher the token) instead of reaching for module-level globals, so
// they can be exercised offline in unit tests. The extracted markdown/body
// output is byte-identical to the original code's output.

import { HttpClient } from "../http/fetcher.ts";
import { githubApi, parseGithubUrl } from "../http/github.ts";
import { bugTrackerUrl, parseBugContent } from "../html/bugtracker.ts";
import { extractDocBody, HOST_RULES } from "../html/docpage.ts";
import { extractNextDataBody } from "../html/nextdata.ts";

// ---------------------------------------------------------------------------
// Article JSON shape (only the fields we touch)
// ---------------------------------------------------------------------------
export interface Article {
  id: string;
  documentType: string;
  title?: string;
  link?: string;
  metadata?: Record<string, unknown>;
  content?: Record<string, unknown>;
  [k: string]: unknown;
}

export interface EnrichResult {
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

// Dependencies injected into every enricher.
export interface EnricherDeps {
  http: HttpClient;
  /** GitHub token (optional) — passed in by the cmd layer, never read here. */
  githubToken?: string;
}

// An enricher turns one article into a body (or throws → recorded as bodyError).
export type Enricher = (
  article: Article,
  nowIso: string,
  deps: EnricherDeps,
) => Promise<EnrichResult>;

export function hasBody(content: Record<string, unknown> | undefined): boolean {
  if (!content) return false;
  const bt = content["body_text"];
  return (typeof bt === "string" && bt.trim().length > 0) ||
    typeof content["bodyError"] === "string";
}

// ---------------------------------------------------------------------------
// Bug Tracker enricher
// ---------------------------------------------------------------------------
export const enrichBugTracker: Enricher = (article, nowIso, deps) => {
  const url = bugTrackerUrl(article);
  return deps.http.fetchText(url).then((html) => {
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
export const enrichGithub: Enricher = async (article, nowIso, deps) => {
  const url = article.link;
  if (!url) throw new Error("no link to derive GitHub target");
  const target = parseGithubUrl(url);

  let body: string;
  if (target.kind === "file") {
    body = await deps.http.fetchText(target.rawUrl!);
  } else if (target.kind === "readme") {
    const data = await githubApi(target.apiPath!, deps.githubToken, deps.http);
    const b64 = (data.content as string ?? "").replace(/\n/g, "");
    body = data.encoding === "base64" ? atob(b64) : (data.content as string ?? "");
  } else {
    const data = await githubApi(target.apiPath!, deps.githubToken, deps.http);
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
export const enrichDocPage: Enricher = async (article, nowIso, deps) => {
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

  const { html, finalUrl } = await deps.http.fetchDoc(url);
  // Some doc URLs (notably docs.nginx.com pages being migrated) now redirect into
  // the F5 KB. Those articles are Salesforce-Knowledge records whose body we
  // already capture under their own type (Support_Solution/Knowledge/...). Record
  // a cross-reference rather than scraping the JS-heavy my.f5.com SPA.
  const finalHost = new URL(finalUrl).hostname;
  if (finalHost === "my.f5.com" && host !== "my.f5.com") {
    const km = finalUrl.match(/\/article\/(K\d+)/);
    return {
      bodySource: finalUrl,
      fetchedAt: nowIso,
      bodyError: `redirected into F5 KB ${
        km?.[1] ?? finalUrl
      }; body captured under its Salesforce type`,
    };
  }
  // A specific page (X.html) that redirects to a directory/landing root means the
  // original article was moved/removed; the landing page is not the article, so
  // capturing its body would be wrong content (don't).
  const reqPath = new URL(url).pathname;
  const finPath = new URL(finalUrl).pathname;
  // Redirected onto a directory/landing root (ends in "/") AND either the request
  // was a specific file (basename has an extension) or it landed under a different
  // top-level section than requested => the original page moved/removed and the
  // landing page is not the article. (A bare trailing-slash normalization keeps
  // the same first segment with no extension, so it is not flagged.)
  const seg1 = (p: string) => p.split("/").filter(Boolean)[0] ?? "";
  if (
    finalUrl !== url && finPath.endsWith("/") &&
    (/\/[^/]+\.[a-z0-9]+$/i.test(reqPath) || seg1(reqPath) !== seg1(finPath))
  ) {
    return {
      bodySource: finalUrl,
      fetchedAt: nowIso,
      bodyError: `redirected to landing page ${finalUrl} (original page moved/removed)`,
    };
  }
  let body_text = "";
  if (rule?.nextData) {
    body_text = extractNextDataBody(html);
  }
  // DOM scrape for non-nextData hosts, or as a fallback if __NEXT_DATA__ was
  // missing/too short (some content pages also server-render into the DOM).
  if (body_text.length < 40) {
    try {
      body_text = extractDocBody(html, finalUrl, rule);
    } catch (e) {
      if (!rule?.nextData) throw e; // for nextData hosts the JSON was the primary path
    }
  }
  if (body_text.length < 40) {
    throw new Error(`extracted body too short (${body_text.length} chars)`);
  }
  // Some hosts (techdocs) serve a "Page Not Found" page with HTTP 200 — its body
  // would otherwise be captured as the article. Reject it.
  if (
    /^#{0,3}\s*404 - Page Not Found/.test(body_text) ||
    /the page you are looking for does not exist/i.test(body_text.slice(0, 400))
  ) {
    return {
      bodySource: finalUrl,
      fetchedAt: nowIso,
      bodyError: "soft 404 (HTTP 200 'Page Not Found')",
    };
  }
  return { body_text, bodySource: finalUrl, fetchedAt: nowIso };
};

// ---------------------------------------------------------------------------
// Registry: type key (dump subdir name) -> enricher
// ---------------------------------------------------------------------------
export const TYPE_ENRICHERS: Record<string, Enricher> = {
  Bug_Tracker: enrichBugTracker,
  F5_GitHub: enrichGithub,
  Manual: enrichDocPage,
  Release_Note: enrichDocPage,
  Supplemental_Document: enrichDocPage,
};

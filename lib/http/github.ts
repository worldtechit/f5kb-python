// GitHub REST/raw access for the F5_GitHub enricher. Moved verbatim from
// enrich_bodies.ts; the only changes are dependency-injection: the GitHub token
// is passed in as a parameter (not read from env here) and the HTTP layer is
// injected (an HttpClient, or a bare FetchFn) so it is unit-testable offline.

import { type FetchFn, HttpClient } from "./fetcher.ts";
import { USER_AGENT } from "../version.ts";

// The article body is the GitHub object's own description: the issue/PR body
// markdown, a repo's README, or a referenced file's contents. The title and
// author already live in the dump metadata, so we extract ONLY the body.
export interface GhTarget {
  kind: "issue" | "pull" | "readme" | "file";
  apiPath?: string; // for issue/pull/readme (JSON API)
  rawUrl?: string; // for file (raw.githubusercontent.com)
}

export function parseGithubUrl(rawUrl: string): GhTarget {
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

export function githubHeaders(token: string | undefined, json: boolean): HeadersInit {
  const h: Record<string, string> = {
    "User-Agent": USER_AGENT,
    Accept: json ? "application/vnd.github+json" : "application/vnd.github.raw",
  };
  if (token) h.Authorization = `Bearer ${token}`;
  return h;
}

// GET the GitHub REST API and return parsed JSON. Retries 5xx/429/secondary
// rate limits; surfaces a clear message on primary rate-limit exhaustion.
export async function githubApi(
  path: string,
  token: string | undefined,
  http: HttpClient | FetchFn,
  attempt = 0,
): Promise<Record<string, unknown>> {
  const MAX_RETRIES = 5;
  const url = `https://api.github.com${path}`;
  const init: RequestInit = { headers: githubHeaders(token, true) };
  const res = http instanceof HttpClient
    ? await http.fetchWithTimeout(url, init)
    : await http(url, init);
  if (res.ok) return await res.json();
  const remaining = res.headers.get("x-ratelimit-remaining");
  await res.body?.cancel();
  // Primary rate limit hit and we'd have to wait an hour — fail loudly so the
  // user knows to set GITHUB_TOKEN. (Resumable: re-run later fills the rest.)
  if (res.status === 403 && remaining === "0") {
    throw new Error(
      token
        ? "GitHub API rate limit exhausted (token present) — re-run later"
        : "GitHub API rate limit hit (60/hr) — set GITHUB_TOKEN to raise to 5000/hr",
    );
  }
  if ((res.status >= 500 || res.status === 429) && attempt < MAX_RETRIES) {
    // Backoff handled by HttpClient elsewhere; mirror the original direct sleep.
    await new Promise<void>((r) => setTimeout(r, 750 * 2 ** attempt));
    return githubApi(path, token, http, attempt + 1);
  }
  throw new Error(`HTTP ${res.status}`);
}

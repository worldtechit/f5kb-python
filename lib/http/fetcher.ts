// HTTP fetching with retry/backoff and a per-request wall-clock timeout. The
// logic is moved verbatim from enrich_bodies.ts; the only change is
// dependency-injection (an injectable fetch, logger, sleep, timeout, and
// User-Agent) so the enrichers can be exercised offline in unit tests.

import { type Logger, NULL_LOGGER } from "../logger.ts";
import { USER_AGENT } from "../version.ts";

export type FetchFn = (input: string | URL, init?: RequestInit) => Promise<Response>;

// Per-request wall-clock timeout. fetch() has no default timeout, so a socket
// that goes dead (machine sleeps / connectivity drops mid-request) would hang
// forever; aborting turns it into a rejection the retry logic handles.
export const REQUEST_TIMEOUT_MS = 60_000;

const defaultSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export interface HttpClientDeps {
  fetch?: FetchFn;
  logger?: Logger;
  sleep?: (ms: number) => Promise<void>;
  timeoutMs?: number;
  userAgent?: string;
}

export class HttpClient {
  private readonly fetchFn: FetchFn;
  private readonly logger: Logger;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly timeoutMs: number;
  private readonly userAgent: string;

  constructor(deps: HttpClientDeps = {}) {
    this.fetchFn = deps.fetch ?? globalThis.fetch;
    this.logger = deps.logger ?? NULL_LOGGER;
    this.sleep = deps.sleep ?? defaultSleep;
    this.timeoutMs = deps.timeoutMs ?? REQUEST_TIMEOUT_MS;
    this.userAgent = deps.userAgent ?? USER_AGENT;
  }

  async fetchWithTimeout(url: string | URL, init: RequestInit = {}): Promise<Response> {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(new Error("request timeout")), this.timeoutMs);
    try {
      return await this.fetchFn(url, { ...init, signal: ctrl.signal });
    } finally {
      clearTimeout(timer);
    }
  }

  // HTTP with retry/backoff (mirrors dump_articles.ts coveoPost behavior).
  async fetchText(url: string, attempt = 0): Promise<string> {
    const MAX_RETRIES = 5;
    try {
      const res = await this.fetchWithTimeout(url, {
        headers: { "User-Agent": this.userAgent, Accept: "text/html" },
      });
      if (!res.ok) {
        // 404/403/410 are terminal (page gone / restricted) — don't retry.
        if ((res.status >= 500 || res.status === 429) && attempt < MAX_RETRIES) {
          await this.sleep(750 * 2 ** attempt);
          return this.fetchText(url, attempt + 1);
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
        await this.sleep(750 * 2 ** attempt);
        return this.fetchText(url, attempt + 1);
      }
      throw e;
    }
  }

  // Fetch a page following redirects, returning the body and the final URL.
  async fetchDoc(
    url: string,
    attempt = 0,
  ): Promise<{ html: string; finalUrl: string }> {
    const MAX_RETRIES = 5;
    try {
      const res = await this.fetchWithTimeout(url, {
        headers: { "User-Agent": this.userAgent, Accept: "text/html" },
        redirect: "follow",
      });
      if (!res.ok) {
        if ((res.status >= 500 || res.status === 429) && attempt < MAX_RETRIES) {
          await res.body?.cancel();
          await this.sleep(750 * 2 ** attempt);
          return this.fetchDoc(url, attempt + 1);
        }
        await res.body?.cancel();
        throw new Error(`HTTP ${res.status}`);
      }
      return { html: await res.text(), finalUrl: res.url || url };
    } catch (e) {
      const msg = (e as Error).message ?? "";
      if (attempt < MAX_RETRIES && !/^HTTP \d/.test(msg)) {
        await this.sleep(750 * 2 ** attempt);
        return this.fetchDoc(url, attempt + 1);
      }
      throw e;
    }
  }
}

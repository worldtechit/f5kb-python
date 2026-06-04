// Coveo search client. Wraps the hardened coveoPost (timeout + token-refresh +
// backoff retry), getCount, and listFacetValues from the original scripts. Moved
// verbatim from dump_articles.ts (post/getCount) and fetch_f5_articles_flex.ts
// (listFacetValues); the only change is dependency-injection (config, fetch,
// logger, sleep, refresh, timeout) so retries/backoff/refresh are testable
// offline. console.warn lines became logger.trace calls per the task spec.

import { Logger, NULL_LOGGER } from "../logger.ts";
import { CoveoConfig, FetchFn, refreshConfig } from "./aura.ts";

export type CoveoResult = Record<string, unknown>;

interface CoveoClientDeps {
  fetch?: FetchFn;
  logger?: Logger;
  sleep?: (ms: number) => Promise<void>;
  refresh?: (c: CoveoConfig) => Promise<void>;
  timeoutMs?: number;
}

export class CoveoClient {
  readonly config: CoveoConfig;
  private readonly fetchFn: FetchFn;
  private readonly logger: Logger;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly refresh: (c: CoveoConfig) => Promise<void>;
  private readonly timeoutMs: number;

  constructor(config: CoveoConfig, deps: CoveoClientDeps = {}) {
    this.config = config;
    this.fetchFn = deps.fetch ?? globalThis.fetch;
    this.logger = deps.logger ?? NULL_LOGGER;
    this.sleep = deps.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
    this.refresh = deps.refresh ?? ((c) => refreshConfig(c, this.fetchFn));
    this.timeoutMs = deps.timeoutMs ?? 60_000;
  }

  // Per-request wall-clock timeout. fetch() has no default timeout, so a socket
  // that goes dead (e.g. the machine sleeps / loses connectivity mid-request)
  // would otherwise hang forever. Aborting turns it into a rejection that the
  // retry/backoff below handles.
  async post(
    body: Record<string, unknown>,
    attempt = 0,
  ): Promise<Record<string, unknown>> {
    const config = this.config;
    const MAX_RETRIES = 5;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(new Error("request timeout")), this.timeoutMs);
    try {
      this.logger.trace("coveo post", {
        aq: body.aq,
        firstResult: body.firstResult,
        numberOfResults: body.numberOfResults,
        url: `${config.platformUrl}/rest/search/v2?organizationId=${config.organizationId}`,
        attempt,
      });
      const res = await this.fetchFn(
        `${config.platformUrl}/rest/search/v2?organizationId=${config.organizationId}`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${config.accessToken}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(body),
          signal: ctrl.signal,
        },
      );
      if (!res.ok) {
        const text = await res.text();
        // Expired/invalid guest token: refresh it in place and retry. The Coveo
        // JWT is ~24h, but a long full-corpus dump can outlive it.
        if ((res.status === 401 || res.status === 419) && attempt < MAX_RETRIES) {
          this.logger.trace(`token rejected ${res.status} — refreshing Coveo token`, { attempt });
          await this.refresh(config);
          await this.sleep(250);
          return this.post(body, attempt + 1);
        }
        // Retry transient server-side statuses; surface everything else (incl.
        // the 400 response-size error, which fetchPaged handles by shrinking).
        if ((res.status >= 500 || res.status === 429) && attempt < MAX_RETRIES) {
          this.logger.trace("coveo retry (transient status)", { status: res.status, attempt });
          await this.sleep(750 * 2 ** attempt);
          return this.post(body, attempt + 1);
        }
        throw new Error(`Coveo API error ${res.status}: ${text.slice(0, 300)}`);
      }
      return await res.json();
    } catch (e) {
      // Network-level failure (timeout, connection reset): retry with backoff.
      // Don't re-retry an HTTP error we already classified above.
      const msg = (e as Error).message ?? "";
      if (attempt < MAX_RETRIES && !/Coveo API error/.test(msg)) {
        this.logger.trace("coveo retry (network error)", { msg, attempt });
        await this.sleep(750 * 2 ** attempt);
        return this.post(body, attempt + 1);
      }
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }

  async getCount(aq: string): Promise<number> {
    const data = await this.post({
      q: "",
      aq: aq || undefined,
      numberOfResults: 0,
      searchHub: "myF5",
    });
    return ((data.totalCountFiltered ?? data.totalCount) as number) ?? 0;
  }

  async listFacetValues(
    field: string,
    filterAq?: string,
  ): Promise<Array<{ value: string; count: number }>> {
    const data = await this.post({
      q: "",
      ...(filterAq ? { aq: filterAq } : {}),
      numberOfResults: 0,
      searchHub: "myF5",
      // 5000 is needed for f5_version: the field has ~2,769 values total, most of
      // which are versioned hierarchy entries (e.g. "BIG-IP LTM|16|16.1|16.1.0").
      // 500 fills up with versioned entries before all top-level product names are
      // returned, so less-common products silently disappear from --list-products.
      facets: [{ field, numberOfValues: 5000, type: "specific" }],
    });

    const facets = (data.facets as Array<Record<string, unknown>>) ?? [];
    const facet = facets.find((f) => f.field === field);
    if (!facet) return [];

    return ((facet.values as Array<Record<string, unknown>>) ?? []).map((v) => ({
      value: v.value as string,
      count: v.numberOfResults as number,
    }));
  }
}

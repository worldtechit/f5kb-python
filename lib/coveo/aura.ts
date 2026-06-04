// Coveo guest-token acquisition via the F5 Salesforce Aura endpoint. Moved
// verbatim from dump_articles.ts; the only change is dependency-injection of the
// fetch function so the Aura request can be exercised offline in unit tests.

export const AURA_URL = "https://my.f5.com/manage/s/sfsites/aura?r=7";
export const AURA_CONTEXT = JSON.stringify({
  mode: "PROD",
  fwuid:
    "ZkJhOVpLN2NZQkJrd2NWd3pMcnFOdzJEa1N5enhOU3R5QWl2VzNveFZTbGcxMy4tMjE0NzQ4MzY0OC4xMzEwNzIwMA",
  app: "siteforce:communityApp",
  loaded: {
    "APPLICATION@markup://siteforce:communityApp": "1547_6p-2GBd9IQWZ4UXs1Im3BQ",
  },
  dn: [],
  globals: {},
  uad: false,
});

export interface CoveoConfig {
  platformUrl: string;
  accessToken: string;
  organizationId: string;
}

export type FetchFn = (input: string | URL, init?: RequestInit) => Promise<Response>;

export async function fetchCoveoConfig(
  fetchFn: FetchFn = globalThis.fetch,
): Promise<CoveoConfig> {
  const body = new URLSearchParams({
    message: JSON.stringify({
      actions: [
        {
          id: "1",
          descriptor: "aura://ApexActionController/ACTION$execute",
          callingDescriptor: "UNKNOWN",
          params: {
            classname: "HeadlessController",
            method: "getHeadlessConfiguration",
            params: {},
            cacheable: false,
            isContinuation: false,
          },
        },
      ],
    }),
    "aura.context": AURA_CONTEXT,
    "aura.pageURI": "/manage/s/global-search/%40uri",
    "aura.token": "null",
  });

  const res = await fetchFn(AURA_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });

  const text = await res.text();
  let jsonText = text;
  const wrapped = text.match(/^\*\/(.+?)\/\*(?:ERROR\*\/)?$/s);
  if (wrapped) jsonText = wrapped[1];
  const data = JSON.parse(jsonText);

  if (data.actions[0].state !== "SUCCESS") {
    throw new Error(`Aura action failed: ${JSON.stringify(data.actions[0].error)}`);
  }

  return JSON.parse(data.actions[0].returnValue.returnValue) as CoveoConfig;
}

// Refresh an expired guest token in place (mutates the shared config object so
// every subsequent coveoPost uses the new token).
export async function refreshConfig(
  config: CoveoConfig,
  fetchFn: FetchFn = globalThis.fetch,
): Promise<void> {
  const fresh = await fetchCoveoConfig(fetchFn);
  config.accessToken = fresh.accessToken;
  config.platformUrl = fresh.platformUrl;
  config.organizationId = fresh.organizationId;
}

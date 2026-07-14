// api.js — thin fetch layer for the f5kb console API.
"use strict";

async function req(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try {
      const body = await r.json();
      if (body && body.detail) detail = body.detail;
    } catch (_) { /* non-JSON error body */ }
    const err = new Error(detail);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

export const api = {
  get: (path) => req(path),
  post: (path, body) => req(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  }),

  config: () => req("/api/config"),
  overview: () => req("/api/overview"),
  runs: (limit = 20) => req(`/api/runs?limit=${limit}`),
  runDetail: (date) => req(`/api/runs/${encodeURIComponent(date)}`),
  corpus: (refresh = false) => req(`/api/corpus${refresh ? "?refresh=true" : ""}`),
  articles: (type, q, page, size) =>
    req(`/api/articles/${encodeURIComponent(type)}?q=${encodeURIComponent(q || "")}&page=${page || 1}&size=${size || 25}`),
  article: (type, id) =>
    req(`/api/article/${encodeURIComponent(type)}/${encodeURIComponent(id)}`),
  diff: (type, id, archiveKey) =>
    req(`/api/article/${encodeURIComponent(type)}/${encodeURIComponent(id)}/diff` +
        (archiveKey ? `?archive_key=${encodeURIComponent(archiveKey)}` : "")),
  find: (id) => req(`/api/find?id=${encodeURIComponent(id)}`),
  pending: (cap = 2000) => req(`/api/pending?cap=${cap}`),
  changelog: (month, limit = 500) =>
    req(`/api/changelog?month=${encodeURIComponent(month || "")}&limit=${limit}`),
  decisions: (month, limit = 500) =>
    req(`/api/decisions?month=${encodeURIComponent(month || "")}&limit=${limit}`),
  object: (key) => req(`/api/object?key=${encodeURIComponent(key)}`),
  keys: (prefix, limit = 500) =>
    req(`/api/keys?prefix=${encodeURIComponent(prefix)}&limit=${limit}`),
  dlqs: () => req("/api/dlqs"),
  dlqMessages: (queue) => req(`/api/dlq/${encodeURIComponent(queue)}/messages`),
  errors: (minutes = 1440) => req(`/api/errors?minutes=${minutes}`),
  logs: (fn = "all", minutes = 180, level = "all", q = "", limit = 300) =>
    req(`/api/logs?fn=${encodeURIComponent(fn)}&minutes=${minutes}` +
        `&level=${encodeURIComponent(level)}&q=${encodeURIComponent(q)}&limit=${limit}`),
  pipeline: () => req("/api/pipeline"),
  integrations: () => req("/api/integrations"),
  health: () => req("/api/health"),
  costs: (minutes = 1440) => req(`/api/costs?minutes=${minutes}`),
  targets: () => req("/api/targets"),
  docs: () => req("/api/docs"),
  doc: (name) => req(`/api/docs/${encodeURIComponent(name)}`),

  trigger: (mode) => api.post("/api/actions/trigger", { mode }),
  approve: (payload) => api.post("/api/actions/approve", payload),
  backfill: (payload) => api.post("/api/actions/backfill", payload),
  restore: (payload) => api.post("/api/actions/restore", payload),
  saveArticle: (payload) => api.post("/api/actions/save-article", payload),
  pipelineAction: (action) => api.post("/api/actions/pipeline", { action }),
  deleteRun: (payload) => api.post("/api/actions/delete-run", payload),
  pendingAction: (payload) => api.post("/api/actions/pending", payload),
  redrive: (payload) => api.post("/api/actions/redrive", payload),
  switchTarget: (target) => api.post("/api/actions/switch-target", { target }),
};

// app.js — shell, router, polling, global search, theme.
"use strict";

import { api } from "/api.js";
import { el, esc, html, modal, toast } from "/ui.js";
import {
  articlePage, corpusPage, docsPage, historyPage, integrationsPage, opsPage,
  overviewPage, reviewPage, runsPage,
} from "/pages.js";

const ICONS = {
  overview: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
  runs: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 9 11 13 15 20 7"/><polyline points="14 7 20 7 20 13"/></svg>',
  corpus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>',
  review: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3 8-8"/><path d="M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9"/></svg>',
  history: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/></svg>',
  ops: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="8" x2="20" y2="8"/><line x1="4" y1="16" x2="20" y2="16"/><circle cx="9" cy="8" r="2" fill="var(--bg-raised)"/><circle cx="15" cy="16" r="2" fill="var(--bg-raised)"/></svg>',
  docs: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V4H6.5A2.5 2.5 0 0 0 4 6.5v13z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-2.5"/></svg>',
  integrations: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="5" cy="12" r="2.5"/><circle cx="19" cy="6" r="2.5"/><circle cx="19" cy="18" r="2.5"/><line x1="7.2" y1="11" x2="16.8" y2="7"/><line x1="7.2" y1="13" x2="16.8" y2="17"/></svg>',
};

const NAV = [
  { hash: "#/overview", label: "Overview", icon: "overview", group: "Pipeline" },
  { hash: "#/runs", label: "Runs", icon: "runs" },
  { hash: "#/review", label: "Review", icon: "review" },
  { hash: "#/corpus", label: "Corpus", icon: "corpus", group: "Data" },
  { hash: "#/history", label: "History", icon: "history" },
  { hash: "#/ops", label: "Operations", icon: "ops", group: "Operate" },
  { hash: "#/integrations", label: "Integrations", icon: "integrations" },
  { hash: "#/docs", label: "Playbook & Docs", icon: "docs" },
];

const ROUTES = [
  { re: /^#\/overview$/, page: overviewPage, crumb: "Overview", nav: "#/overview" },
  { re: /^#\/runs(?:\/([^/]+))?$/, page: runsPage, crumb: "Runs", nav: "#/runs" },
  { re: /^#\/review$/, page: reviewPage, crumb: "Review", nav: "#/review" },
  { re: /^#\/corpus(?:\/([^/]+))?$/, page: corpusPage, crumb: "Corpus", nav: "#/corpus" },
  { re: /^#\/article\/([^/]+)\/([^/]+)$/, page: articlePage, crumb: "Corpus", nav: "#/corpus" },
  { re: /^#\/history(?:\/([^/]+))?$/, page: historyPage, crumb: "History", nav: "#/history" },
  { re: /^#\/ops$/, page: opsPage, crumb: "Operations", nav: "#/ops" },
  { re: /^#\/integrations$/, page: integrationsPage, crumb: "Integrations", nav: "#/integrations" },
  { re: /^#\/docs(?:\/([^/]+))?$/, page: docsPage, crumb: "Playbook & Docs", nav: "#/docs" },
];

const state = { cfg: { writable: false, mode: "?" }, refreshTimer: null };

function setRefresh(fn, ms) {
  clearInterval(state.refreshTimer);
  if (!fn) return;
  state.refreshTimer = setInterval(() => {
    if (!document.hidden) {
      fn();
      const note = document.getElementById("refresh-note");
      note.textContent = "updated " + new Date().toLocaleTimeString();
    }
  }, ms || 15000);
}

function navigate(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

async function route() {
  clearInterval(state.refreshTimer);
  const hash = location.hash || "#/overview";
  const match = ROUTES.map((r) => ({ r, m: hash.match(r.re) })).find((x) => x.m);
  const view = document.getElementById("view");
  view.innerHTML = "";
  document.querySelectorAll("#nav a").forEach((a) =>
    a.classList.toggle("active", a.getAttribute("href") === (match ? match.r.nav : "")));
  if (!match) {
    navigate("#/overview");
    return;
  }
  document.getElementById("crumb").textContent = match.r.crumb;
  try {
    await match.r.page(view, match.m.slice(1).filter((x) => x !== undefined).map(decodeURIComponent),
      { cfg: state.cfg, navigate, setRefresh });
  } catch (e) {
    view.innerHTML = "";
    view.append(el("div", "alert error", e.message));
  }
}

function buildNav() {
  const nav = document.getElementById("nav");
  for (const item of NAV) {
    if (item.group) nav.append(el("div", "sep", item.group));
    const a = html("a", null, `${ICONS[item.icon] || ""}<span>${esc(item.label)}</span>`);
    a.href = item.hash;
    nav.append(a);
  }
}

function applyBadge() {
  const cfg = state.cfg;
  const badge = document.getElementById("target-badge");
  badge.classList.toggle("rw", !!cfg.writable);
  const bits = [String(cfg.mode || "?").toUpperCase()];
  if (cfg.target && cfg.target !== cfg.mode) bits.push(cfg.target);
  if (cfg.layout === "cli") bits.push("cli outputs");
  bits.push(cfg.writable ? "READ-WRITE" : "read-only");
  document.getElementById("badge-text").textContent = bits.join(" · ");

  const foot = document.getElementById("side-foot");
  foot.innerHTML = "";
  if (cfg.bucket) {
    foot.append(html("div", "kv", `<span>bucket</span>`));
    foot.append(html("div", "mono small", esc(cfg.bucket)));
    foot.append(html("div", "kv", `<span>region</span><span>${esc(cfg.region || "")}</span>`));
  } else if (cfg.root) {
    foot.append(html("div", "kv", `<span>root</span>`));
    foot.append(html("div", "mono small", esc(cfg.root)));
  }
}

function setupSearch() {
  const inp = document.getElementById("global-search");
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "/" && document.activeElement !== inp &&
        !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
      ev.preventDefault();
      inp.focus();
      inp.select();
    }
  });
  inp.addEventListener("keydown", async (ev) => {
    if (ev.key !== "Enter") return;
    const id = inp.value.trim();
    if (!id) return;
    inp.disabled = true;
    try {
      const r = await api.find(id);
      const m = r.matches || [];
      if (!m.length) toast(`no article "${id}" in live or pending`, true);
      else if (m.length === 1) navigate(`#/article/${m[0].type_key}/${m[0].id}`);
      else {
        const body = el("div");
        for (const hit of m) {
          const a = html("a", "btn sm mb",
            `<span class="mono">${esc(hit.type_key)}/${esc(hit.id)}</span>&nbsp;` +
            `<span class="tag ${hit.where === "live" ? "ok" : "warn"}">${esc(hit.where)}</span>`);
          a.href = `#/article/${hit.type_key}/${hit.id}`;
          a.style.display = "inline-flex";
          a.style.marginRight = "8px";
          body.append(a);
        }
        modal({ title: `"${id}" found in ${m.length} types`, body });
      }
    } catch (e) {
      toast(e.message, true);
    }
    inp.disabled = false;
  });
}

function setupTheme() {
  const saved = localStorage.getItem("f5kb-theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
  document.getElementById("theme-toggle").onclick = () => {
    const cur = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    if (cur === "light") document.documentElement.setAttribute("data-theme", "light");
    else document.documentElement.removeAttribute("data-theme");
    localStorage.setItem("f5kb-theme", cur === "light" ? "light" : "");
  };
}

async function setupTargetSwitcher() {
  let info;
  try { info = await api.targets(); } catch (_) { return; }
  if (!(info.targets || []).length) return;
  const sel = el("select", "inp");
  sel.id = "target-select";
  sel.title = "Switch target. Only the target the server was started against " +
              "keeps write access — switched-to targets are read-only.";
  sel.style.width = "auto";
  for (const t of info.targets) sel.append(new Option(t, t, false, t === info.current));
  sel.onchange = async () => {
    sel.disabled = true;
    try {
      const r = await api.switchTarget(sel.value);
      state.cfg = await api.config();
      applyBadge();
      toast(`switched to ${r.target}` +
            (r.forced_read_only ? " (read-only — writes stay on the startup target)" : ""));
      route();
    } catch (e) {
      toast(`switch failed: ${e.message}`, true);
      const cur = (await api.targets().catch(() => null)) || {};
      if (cur.current) sel.value = cur.current;
    }
    sel.disabled = false;
  };
  const badge = document.getElementById("target-badge");
  badge.parentElement.insertBefore(sel, badge);
}

async function init() {
  buildNav();
  setupTheme();
  setupSearch();
  try {
    state.cfg = await api.config();
  } catch (e) {
    toast("cannot reach the console API: " + e.message, true);
  }
  applyBadge();
  setupTargetSwitcher();
  window.addEventListener("hashchange", route);
  await route();
}

init();

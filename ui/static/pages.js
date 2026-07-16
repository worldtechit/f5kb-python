// pages.js — every page of the f5kb console. Each page gets (view, params, ctx)
// where ctx = { cfg, navigate, setRefresh } and returns nothing.
"use strict";

import { api } from "/api.js";
import {
  confirmModal, diffView, el, empty, esc, fmtNum, fmtTime, html, infoDot,
  jsonBlock, modal, pager, progressBar, skeleton, stat, stepper, table, tag,
  toast,
} from "/ui.js";

const RISK_HARD = new Set(["body-dropped", "body-error"]);

function pageHead(view, title, subText, rightNodes) {
  const head = el("div", "page-head");
  head.append(el("h1", null, title));
  if (subText) head.append(el("div", "sub", subText));
  if (rightNodes && rightNodes.length) {
    const right = el("div", "right");
    rightNodes.forEach((n) => right.append(n));
    head.append(right);
  }
  view.append(head);
  return head;
}

function riskChips(risks) {
  const box = el("span");
  for (const r of risks || []) {
    box.append(el("span", "risk-chip" + (RISK_HARD.has(r) ? " hard" : ""), r));
    box.append(document.createTextNode(" "));
  }
  return box;
}

function canDrive(ctx) {
  return ctx.cfg.writable && ctx.cfg.mode === "aws";
}

// ── dismissible alerts (per-browser; the underlying condition is unaffected) ──
const DISMISSED_ALERTS_KEY = "f5kb-dismissed-alerts";

function dismissedAlerts() {
  try { return new Set(JSON.parse(localStorage.getItem(DISMISSED_ALERTS_KEY) || "[]")); }
  catch (_) { return new Set(); }
}

function rememberDismissed(key) {
  const all = [...dismissedAlerts(), key].slice(-200);
  localStorage.setItem(DISMISSED_ALERTS_KEY, JSON.stringify(all));
}

function alertRow(a, scope) {
  const key = `${scope}|${a.level}|${a.msg}`;
  if (dismissedAlerts().has(key)) return null;
  const div = el("div", "alert " + a.level);
  div.append(html("span", null, esc(a.msg)));
  const x = el("button", "alert-x", "✕");
  x.title = "dismiss — hides this message in this browser only; the underlying " +
            "condition (DLQ message, held articles…) stays until actually resolved";
  x.onclick = () => { rememberDismissed(key); div.remove(); };
  div.append(x);
  return div;
}

// ═════════════════════════════════════════════════════════════════════════════
//  Overview
// ═════════════════════════════════════════════════════════════════════════════
export async function overviewPage(view, params, ctx) {
  view.append(skeleton(6));
  const o = await api.overview();
  view.innerHTML = "";

  const right = [];
  if (canDrive(ctx)) {
    const inc = el("button", "btn primary sm", "▶ Run incremental");
    const full = el("button", "btn sm", "▶ Run full");
    inc.onclick = () => confirmModal("Trigger incremental run?",
      "Invokes the orchestrator with mode=incremental.", async () => {
        await api.trigger("incremental"); toast("incremental run triggered");
      });
    full.onclick = () => confirmModal("Trigger FULL run?",
      "A full run re-lists the entire corpus — heavier and slower.", async () => {
        await api.trigger("full"); toast("full run triggered");
      }, { danger: true });
    right.push(inc, full);
  }
  pageHead(view, "Overview", null, right);

  const latest = o.latest;
  const tiles = el("div", "grid cols-4 mb");
  tiles.append(stat("Live corpus", fmtNum(o.corpus_total), {
    hint: `${Object.keys(o.corpus || {}).length} types`,
  }));
  tiles.append(stat("Latest run", latest ? latest.run_date : "—", {
    cls: "sm", hint: latest ? `${latest.mode || ""} · ${latest.phase}` : "no runs found",
  }));
  tiles.append(stat("Pending staged", fmtNum(o.pending_total), {
    cls: o.pending_total ? "warn" : "",
    hint: o.pending_total ? "awaiting approval" : "queue clear",
  }));
  const held = latest ? latest.held || 0 : 0;
  tiles.append(stat("Held for review", fmtNum(held), {
    cls: held ? "bad" : "ok", hint: held ? "needs a human decision" : "nothing held",
  }));
  view.append(tiles);

  const cols = el("div", "grid cols-2");

  // Recent runs
  const runsCard = el("div", "card");
  runsCard.append(el("h3", null, "Recent runs"));
  if (!(o.runs || []).length) {
    runsCard.append(empty(ctx.cfg.layout === "cli"
      ? "local CLI outputs — pipeline runs live in the S3 stage"
      : "no runs recorded yet"));
  } else {
    for (const r of o.runs) {
      const row = el("div", "run-row");
      row.append(el("span", "dot " + (r.closed ? "done" : r.phase === "failed" ? "failed" : "running")));
      const meta = el("div");
      meta.append(el("div", "run-date", r.run_date));
      meta.append(el("div", "dim small", `${r.mode || "?"} · ${r.phase}`));
      row.append(meta);
      row.onclick = () => ctx.navigate(`#/runs/${r.run_date}`);
      runsCard.append(row);
    }
  }
  cols.append(runsCard);

  // Health / infra
  const health = el("div", "card");
  health.append(el("h3", null, "Health"));
  const dlqs = o.dlqs || {};
  if (!Object.keys(dlqs).length) {
    health.append(html("div", "dim small", "local mode — no live queues"));
  } else {
    for (const [q, n] of Object.entries(dlqs)) {
      const short = q.replace(/^f5kb-/, "").replace(/-(staging|prod)$/, "");
      const bad = q.includes("dlq") && n > 0;
      health.append(html("div", "kv",
        `<span class="mono">${esc(short)}</span>` +
        `<span class="v ${bad ? "bad" : n === 0 ? "good" : ""}">${n < 0 ? "?" : n}</span>`));
    }
  }
  const hi = o.hash_index || {};
  health.append(html("div", "kv",
    `<span>hash-index</span><span class="v ${hi.present ? "good" : ""}">` +
    `${hi.present ? fmtNum(hi.entries) + (hi.entries ? " entries" : " present") : "absent"}</span>`));
  cols.append(health);
  view.append(cols);

  // Corpus breakdown
  const corpusCard = el("div", "card mt");
  corpusCard.append(el("h3", null, "Live corpus by type"));
  const entries = Object.entries(o.corpus || {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) corpusCard.append(empty("corpus is empty for this target"));
  const max = entries.length ? entries[0][1] : 1;
  for (const [t, n] of entries) {
    const row = el("div", "kv");
    row.style.cursor = "pointer";
    row.onclick = () => ctx.navigate(`#/corpus/${t}`);
    row.append(html("span", null, `<span class="mono">${esc(t)}</span>`));
    const v = el("span", "v");
    v.append(document.createTextNode(fmtNum(n) + " "));
    const bar = progressBar(n, max, true);
    bar.style.width = "120px";
    bar.style.display = "inline-block";
    v.append(bar);
    row.append(v);
    corpusCard.append(row);
  }
  view.append(corpusCard);

  ctx.setRefresh(() => refreshInPlace(view, params, ctx, overviewPage), 15000);
}

function clearView(view) { view.innerHTML = ""; return view; }

// Re-render a page's data WITHOUT the blank-out/skeleton flash: build the new
// DOM off-screen, then swap it in atomically. Skips a cycle when the user is
// mid-interaction (focused field, checked selection boxes, or an open modal)
// and preserves <details> open/closed toggles across the swap.
async function refreshInPlace(view, params, ctx, pageFn) {
  if (document.querySelector("#modal-root .scrim")) return;
  const ae = document.activeElement;
  if (ae && view.contains(ae) && /INPUT|TEXTAREA|SELECT/.test(ae.tagName)) return;
  if (view.querySelector("input[type=checkbox]:checked")) return;
  const open = new Set(
    [...view.querySelectorAll("details[open] > summary")].map((s) => s.textContent));
  const hadDetails = view.querySelector("details") !== null;
  const tmp = el("div");
  try { await pageFn(tmp, params, { ...ctx, setRefresh: () => {} }); }
  catch (_) { return; /* keep the last good render on a transient error */ }
  if (hadDetails) {
    tmp.querySelectorAll("details > summary").forEach((s) => {
      s.parentElement.open = open.has(s.textContent);
    });
  }
  view.replaceChildren(...tmp.children);
}

// ═════════════════════════════════════════════════════════════════════════════
//  Runs
// ═════════════════════════════════════════════════════════════════════════════
export async function runsPage(view, params, ctx) {
  const selected = params[0] || null;
  view.append(skeleton(5));
  const runs = await api.runs(30);
  view.innerHTML = "";
  pageHead(view, "Runs", "pipeline executions, live progress, held approvals");

  if (!runs.length) {
    view.append(empty(ctx.cfg.layout === "cli"
      ? "This is a local CLI outputs tree — runs exist only in the deployed S3 stages."
      : "No runs found."));
    return;
  }
  const date = selected || runs[0].run_date;

  const cols = el("div", "two-col");
  const list = el("div", "card");
  list.append(el("h3", null, "history"));
  for (const r of runs) {
    const row = el("div", "run-row" + (r.run_date === date ? " sel" : ""));
    row.append(el("span", "dot " + (r.closed ? "done" : r.phase === "failed" ? "failed" : "running")));
    const meta = el("div");
    meta.append(el("div", "run-date", r.run_date));
    meta.append(el("div", "dim small", `${r.mode || "?"} · ${r.phase}`));
    row.append(meta);
    row.onclick = () => ctx.navigate(`#/runs/${r.run_date}`);
    list.append(row);
  }
  cols.append(list);

  const detail = el("div");
  cols.append(detail);
  view.append(cols);
  await renderRunDetail(detail, date, ctx);

  ctx.setRefresh(async () => {
    let d2;
    try { d2 = await api.runDetail(date); }
    catch (_) { return; }
    patchRunDetail(detail, d2);
  }, 8000);
}

// Drill-down: open a paginated list of the articles behind one of a run's
// counts (staged / track class / auto-approved / held). Each row links to the
// article view. Backed by the per-run JSONL manifests the pipeline already writes.
function openChangesModal(ctx, date, { kind, type, op, risk, title }) {
  const cols = [{ k: "id", label: "article id" }, { k: "type", label: "type" }];
  if (kind !== "staged") cols.push({ k: "op", label: "op" });
  if (kind === "auto" || kind === "holds") cols.push({ k: "changed", label: "changed" });
  if (kind === "track") cols.push({ k: "risk", label: "risk" });

  const cell = (c, r) => {
    if (c.k === "id") return { html: `<span class="mono">${esc(r.id ?? "?")}</span>` };
    if (c.k === "type") return { text: r.type_key || "—", cls: "dim" };
    if (c.k === "op") return { text: r.op || "—" };
    if (c.k === "changed") return { text: (r.changed || []).join(", ") || "—", cls: "dim" };
    if (c.k === "risk") {
      return { el: (r.risk || []).length
        ? el("span", null, (r.risk || []).map((x) => tag(x, "bad"))) : el("span", "dim", "—") };
    }
    return { text: "" };
  };

  const info = el("div", "dim small mb");
  const tblWrap = el("div");
  const pgWrap = el("div", "mt");
  const bodyBox = el("div", null, [info, tblWrap, pgWrap]);
  const size = 50;
  let m;

  async function load(page) {
    tblWrap.innerHTML = "<div class='dim small'>loading…</div>";
    let res;
    try { res = await api.runChanges(date, { kind, type, op, risk, page, size }); }
    catch (e) { tblWrap.innerHTML = ""; tblWrap.append(empty(e.message)); return; }
    info.textContent = `${fmtNum(res.total)} article(s)` +
      (res.truncated ? ` — capped at ${fmtNum(res.rows.length ? size * res.pages : 0)} for display` : "") +
      (res.total ? " · click a row to open the article" : "");
    tblWrap.innerHTML = "";
    if (!res.rows.length) {
      tblWrap.append(empty("No articles recorded for this count in the run manifests."));
    } else {
      const rows = res.rows.map((r) => ({ _r: r, cells: cols.map((c) => cell(c, r)) }));
      tblWrap.append(table(cols.map((c) => c.label), rows, {
        onRow: (row) => {
          if (!row._r.id || !row._r.type_key) return;
          m.close();
          ctx.navigate(`#/article/${encodeURIComponent(row._r.type_key)}/${encodeURIComponent(row._r.id)}`);
        },
      }));
    }
    pgWrap.innerHTML = "";
    if (res.pages > 1) pgWrap.append(pager(res.page, res.pages, load));
  }

  m = modal({ title: title || "Articles", sub: `run ${date}`, body: bodyBox, wide: true });
  load(1);
}

async function renderRunDetail(box, date, ctx) {
  box.append(skeleton(6));
  let d;
  try { d = await api.runDetail(date); }
  catch (e) { box.innerHTML = ""; box.append(empty(e.message)); return; }
  box.innerHTML = "";

  const head = el("div", "page-head");
  head.append(el("h1", null, date));
  head.append(el("div", "sub", `${d.mode || "?"} run` +
    (d.mode_source ? ` (${d.mode_source})` : "") +
    (d.started_at ? ` · started ${fmtTime(d.started_at)}` : "")));
  if (canDrive(ctx)) {
    const right = el("div", "right");
    right.append(await runControls(d, ctx));
    head.append(right);
  }
  box.append(head);
  const stepRow = el("div", "toolbar");
  stepRow.append(stepper(d.phases || ["scrape", "track", "approve", "done"], d.phase));
  stepRow.append(infoDot("Run phases",
    "A run advances scrape → track → approve → done.\n\n" +
    "scrape: every type is dumped from Coveo into pending/ (and, for the 4 enrichable " +
    "types, article bodies are fetched over HTTP). The last type to finish writes the " +
    "scrape/_done gate file, which starts track.\n\n" +
    "track: each staged article is diffed against the live corpus and risk-flagged " +
    "(body dropped / fetch error / body shrank).\n\n" +
    "approve: safe articles are promoted to live/ automatically and announced to P2 " +
    "over SNS; risky ones are held for a human decision. The run closes (done) when " +
    "every hold is resolved."));
  box.append(stepRow);

  const alertsBox = el("div"); alertsBox.dataset.anchor = "alerts";
  for (const a of d.alerts || []) {
    const row = alertRow(a, date);
    if (row) alertsBox.append(row);
  }
  box.append(alertsBox);

  // per-type progress
  const rows = (d.per_type || []).map((t) => {
    let dumped = fmtNum(t.dump_count ?? 0);
    let dumpCell;
    if (t.server_total && !t.dump_done) {
      dumpCell = el("span");
      dumpCell.append(document.createTextNode(`${fmtNum(t.dump_count)}/${fmtNum(t.server_total)}`));
      dumpCell.append(progressBar(t.dump_count, t.server_total));
    } else dumpCell = el("span", null, dumped);
    let enrCell = el("span", "dim", "—");
    if (t.enrichable) {
      const done = (t.enriched ?? 0) + (t.enrich_failed ?? 0);
      if (t.dump_done && !t.enrich_done && t.dump_count) {
        enrCell = el("span");
        enrCell.append(document.createTextNode(`${fmtNum(done)}/${fmtNum(t.dump_count)}`));
        enrCell.append(progressBar(done, t.dump_count));
      } else enrCell = el("span", null, fmtNum(t.enriched ?? 0));
    }
    const stateKind = t.terminal ? "ok" : /dumping|enriching/.test(t.state || "") ? "info" : "dim";
    return {
      _type: t.type_key,
      cells: [
        { html: `<span class="mono">${esc(t.type_key)}</span>` },
        { el: t.enrichable ? tag("enrich", "info") : el("span") },
        { el: dumpCell, cls: "num", anchor: `dump-${t.type_key}` },
        { el: enrCell, cls: "num", anchor: `enr-${t.type_key}` },
        { html: t.enrichable
            ? `<span class="${t.enrich_failed ? "bad-c" : "dim"}">${t.enrich_failed ?? 0}</span>` : "—",
          cls: "num", anchor: `fail-${t.type_key}` },
        { el: tag(t.terminal ? "done" : t.state || "…", stateKind), anchor: `state-${t.type_key}` },
      ],
    };
  });
  const typeHead = el("div", "toolbar mt");
  typeHead.append(html("span", "dim small", "per-type progress"));
  typeHead.append(infoDot("Per-type progress columns",
    "staged — articles written to pending/ this run; while a type is still dumping " +
    "it shows x/y against the server-side total reported by Coveo.\n\n" +
    "enriched — bodies fetched for enrichable types, x/y over the staged count. " +
    "failed — enrichment failures; if a live version of the article exists these " +
    "become body-error holds at approve.\n\n" +
    "state — queued: the type's SQS message is waiting, delayed by a retry backoff, " +
    "or held in-flight by SQS; dumping/enriching: a Lambda is actively working; " +
    "(resumed): the Lambda hit its 15-minute limit and continued from its saved " +
    "cursor — completely normal for large types; done: this type is finished for " +
    "the run.\n\n" +
    "Tip: the staged count (and the track/approve counts below) are clickable — " +
    "they open the exact list of articles behind the number, each linking to the " +
    "article."));
  box.append(typeHead);
  const typeTable = table(["type", "", { label: "staged", cls: "num" },
    { label: "enriched", cls: "num" }, { label: "failed", cls: "num" }, "state"], rows);
  typeTable.dataset.anchor = "typetable";
  // tag each td so patchRunDetail can find cells without re-rendering
  typeTable.querySelectorAll("tbody tr").forEach((tr, i) => {
    const r = rows[i]; if (!r) return;
    tr.dataset.type = r._type || "";
    const tds = tr.querySelectorAll("td");
    ["", "", `dump-${r._type}`, `enr-${r._type}`, `fail-${r._type}`, `state-${r._type}`]
      .forEach((a, j) => { if (a && tds[j]) tds[j].dataset.anchor = a; });
    // Make the STAGED cell a drill-down into that type's staged manifest.
    const pt = (d.per_type || [])[i];
    const stagedTd = tds[2];
    if (stagedTd && pt && (pt.dump_count || 0) > 0) {
      stagedTd.classList.add("drill");
      stagedTd.dataset.drill = "staged";
      stagedTd.dataset.drillType = r._type;
      stagedTd.dataset.drillTitle = `Staged this run — ${r._type}`;
    }
  });
  box.append(typeTable);

  // track + approve summary
  const grid = el("div", "grid cols-2 mt"); grid.dataset.anchor = "summary";
  const t = d.track || {};
  const trackCard = el("div", "card");
  const trackH = el("h3", null, "track — change detection");
  trackH.append(infoDot("Track — change detection",
    "Every staged article is compared with the live corpus by metadata hash: " +
    "new (no live copy), changed, or unchanged.\n\n" +
    "The body rows are risk flags: body shrank (pending body is much smaller than " +
    "live — informational only, always auto-approves), body dropped (live had a " +
    "body, pending has none), body error (the enrichment fetch failed). Only " +
    "dropped and error force a human hold, and only when a live version exists."));
  trackCard.append(trackH);
  // kv row; pass `drill` to make it a clickable drill-down when its count > 0.
  const kv = (k, v, cls, anchor, drill) => {
    const row = html("div", "kv",
      `<span>${esc(k)}</span><span class="v ${cls || ""}" ${anchor ? `data-kv="${anchor}"` : ""}>${esc(String(v))}</span>`);
    if (drill && (drill.count || 0) > 0) {
      row.classList.add("drill");
      row.dataset.drill = drill.kind;
      if (drill.op) row.dataset.drillOp = drill.op;
      if (drill.risk) row.dataset.drillRisk = drill.risk;
      row.dataset.drillTitle = drill.title || "Articles";
    }
    return row;
  };
  trackCard.append(kv("new", fmtNum(t.new), "", "t-new",
    { kind: "track", op: "new", title: "New articles", count: t.new }));
  trackCard.append(kv("changed", fmtNum(t.changed), "", "t-changed",
    { kind: "track", op: "changed", title: "Changed articles", count: t.changed }));
  trackCard.append(kv("unchanged", fmtNum(t.unchanged), "", "t-unchanged",
    { kind: "track", op: "unchanged", title: "Unchanged articles", count: t.unchanged }));
  trackCard.append(kv("body shrank", fmtNum(t.body_shrank), "", "t-shrank",
    { kind: "track", risk: "body-shrank", title: "Body shrank", count: t.body_shrank }));
  trackCard.append(kv("body dropped", fmtNum(t.body_dropped), t.body_dropped ? "bad" : "", "t-dropped",
    { kind: "track", risk: "body-dropped", title: "Body dropped", count: t.body_dropped }));
  trackCard.append(kv("body error", fmtNum(t.body_error), t.body_error ? "bad" : "", "t-error",
    { kind: "track", risk: "body-error", title: "Body error", count: t.body_error }));
  grid.append(trackCard);

  const apprCard = el("div", "card");
  const apprH = el("h3", null, "approve — handoff to P2");
  apprH.append(infoDot("Approve — handoff to P2",
    "auto-approved articles are already promoted to live/ and announced to the P2 " +
    "team over SNS — they never wait on holds. holds-approved counts held articles " +
    "a human (or the escalation watchdog) later approved.\n\n" +
    "The three markers are the run's gate files in S3: scrape/_done starts track, " +
    "track/_done starts approve, approve/_done closes the run. A phase that seems " +
    "stuck usually means its gate file has not appeared yet."));
  apprCard.append(apprH);
  apprCard.append(kv("auto-approved", fmtNum(d.approve.auto), "good", "a-auto",
    { kind: "auto", title: "Auto-approved → live + P2", count: d.approve.auto }));
  apprCard.append(kv("holds-approved", fmtNum(d.approve.holds), d.approve.holds ? "good" : "", "a-holds",
    { kind: "holds", title: "Holds approved", count: d.approve.holds }));
  apprCard.append(kv("scrape/_done", d.markers.scrape_done ? "✓" : "·",
    d.markers.scrape_done ? "good" : "", "m-scrape"));
  apprCard.append(kv("track/_done", d.markers.track_done ? "✓" : "·",
    d.markers.track_done ? "good" : "", "m-track"));
  apprCard.append(kv("approve/_done", d.markers.approve_done ? "✓" : "·",
    d.markers.approve_done ? "good" : "", "m-approve"));
  grid.append(apprCard);
  box.append(grid);

  if ((d.held || []).length) box.append(heldSection(d, ctx));

  // One delegated handler for every drill-down cell/row (survives live cell
  // patching, which only rewrites cell contents, not these container elements).
  box.onclick = (ev) => {
    const t = ev.target.closest("[data-drill]");
    if (!t || !box.contains(t)) return;
    openChangesModal(ctx, date, {
      kind: t.dataset.drill,
      type: t.dataset.drillType || null,
      op: t.dataset.drillOp || null,
      risk: t.dataset.drillRisk || null,
      title: t.dataset.drillTitle || "Articles",
    });
  };
}

// ── run controls: pause / stop / delete ──────────────────────────────────────
async function runControls(d, ctx) {
  const box = el("div", "btn-row");
  let ps = null;
  try { ps = await api.pipeline(); } catch (_) { /* non-AWS target */ }
  let paused = !!(ps && ps.paused);

  const pauseBtn = el("button", "btn sm");
  const syncPause = () => {
    pauseBtn.textContent = paused ? "▶ Resume pipeline" : "⏸ Pause pipeline";
  };
  syncPause();
  pauseBtn.onclick = () => confirmModal(
    paused ? "Resume the pipeline?" : "Pause the pipeline?",
    paused
      ? "Re-enables the dump + enrich SQS triggers. Queued messages start processing again."
      : "Disables the dump + enrich SQS triggers. NOTHING is deleted — queued messages " +
        "simply wait (and in-flight Lambdas finish, up to 15 min) until you resume.",
    async () => {
      const r = await api.pipelineAction(paused ? "resume" : "pause");
      paused = r.status === "paused";
      syncPause();
      toast(`pipeline ${r.status} (${(r.functions || []).length} triggers)`);
    });

  const stopBtn = el("button", "btn danger sm", "⏹ Stop run");
  stopBtn.onclick = () => confirmModal("Stop the in-flight run?",
    "Pauses the SQS triggers AND purges the dump/enrich WORK queues, killing the " +
    "self-requeue chain. Queued (unprocessed) messages are deleted; DLQ messages and " +
    "all run data in S3 are untouched. In-flight Lambdas finish their current 15-min " +
    "slice but cannot continue. Resume the pipeline afterwards for the next run.",
    async () => {
      await api.pipelineAction("pause");
      const r = await api.pipelineAction("purge_queues");
      const errs = Object.values(r).filter((v) => String(v).startsWith("error"));
      toast(errs.length ? `stopped, but: ${errs[0]}` : "run stopped — triggers paused, work queues purged", !!errs.length);
    }, { danger: true });

  const delBtn = el("button", "btn danger sm", "🗑 Delete run…");
  delBtn.onclick = () => deleteRunModal(d.run_date, ctx);

  box.append(pauseBtn, stopBtn, delBtn);
  return box;
}

function deleteRunModal(runDate, ctx) {
  const body = el("div");
  body.append(el("div", "alert warn",
    "Deletes this run's tracking data: runs/" + runDate + "/ and lambda/state/" + runDate + "/. " +
    "The live corpus, archives, audit trail, and hash-index are NEVER touched. " +
    "Already-approved articles stay live."));
  const cbRow = el("label", "lbl");
  const cb = el("input");
  cb.type = "checkbox";
  cbRow.style.display = "flex";
  cbRow.style.gap = "8px";
  cbRow.style.alignItems = "center";
  cbRow.append(cb, document.createTextNode(
    "also delete the pending/ articles this run staged (they re-stage on the next run)"));
  body.append(cbRow);
  const out = el("div", "mt");
  body.append(out);

  async function preview() {
    out.innerHTML = "";
    out.append(el("div", "skel"));
    try {
      const r = await api.deleteRun({ run_date: runDate, include_pending: cb.checked, dry_run: true });
      out.innerHTML = "";
      const c = r.counts || {};
      out.append(html("div", "kv", `<span>runs/${esc(runDate)}/ keys</span><span class="v">${fmtNum(c.runs || 0)}</span>`));
      out.append(html("div", "kv", `<span>lambda/state/${esc(runDate)}/ keys</span><span class="v">${fmtNum(c.state || 0)}</span>`));
      out.append(html("div", "kv", `<span>pending/ articles</span><span class="v ${c.pending ? "bad" : ""}">${fmtNum(c.pending || 0)}</span>`));
      if (typeof r.dlq_messages === "number" && r.dlq_messages >= 0) {
        out.append(html("div", "kv", `<span>DLQ messages for this run</span>` +
          `<span class="v ${r.dlq_messages ? "bad" : ""}">${fmtNum(r.dlq_messages)}` +
          `${r.dlq_messages ? ' <span class="dim small">(deleted with the run)</span>' : ""}</span>`));
      }
      out.append(html("div", "kv", `<span><b>total to delete</b></span><span class="v"><b>${fmtNum(r.total || 0)}</b></span>`));
    } catch (e) { out.innerHTML = ""; out.append(empty(e.message)); }
  }
  cb.onchange = preview;
  preview();

  modal({
    title: `Delete run ${runDate}`,
    body,
    actions: [
      { label: "Cancel", cls: "ghost" },
      {
        label: "Delete permanently", cls: "danger",
        onClick: async () => {
          const r = await api.deleteRun({ run_date: runDate, include_pending: cb.checked,
            dry_run: false, actor: "console" });
          toast(`deleted ${fmtNum(r.total || 0)} keys for run ${runDate}`);
          ctx.navigate("#/runs");
        },
      },
    ],
  });
}

function patchRunDetail(box, d) {
  // Update alerts (respecting per-browser dismissals)
  const ab = box.querySelector("[data-anchor=alerts]");
  if (ab) {
    ab.innerHTML = "";
    for (const a of d.alerts || []) {
      const row = alertRow(a, d.run_date);
      if (row) ab.append(row);
    }
  }
  // Update phase stepper (replace only the stepper node)
  const stepOld = box.querySelector(".stepper");
  if (stepOld) stepOld.replaceWith(stepper(d.phases || ["scrape", "track", "approve", "done"], d.phase));
  // Update per-type cells in place
  for (const t of d.per_type || []) {
    const dumpEl = box.querySelector(`[data-anchor="dump-${t.type_key}"]`);
    const enrEl  = box.querySelector(`[data-anchor="enr-${t.type_key}"]`);
    const failEl = box.querySelector(`[data-anchor="fail-${t.type_key}"]`);
    const stateEl = box.querySelector(`[data-anchor="state-${t.type_key}"]`);
    if (dumpEl) {
      if (t.server_total && !t.dump_done) {
        dumpEl.innerHTML = "";
        dumpEl.append(document.createTextNode(`${fmtNum(t.dump_count)}/${fmtNum(t.server_total)}`));
        dumpEl.append(progressBar(t.dump_count, t.server_total));
      } else {
        dumpEl.innerHTML = `<span>${fmtNum(t.dump_count ?? 0)}</span>`;
      }
    }
    if (enrEl && t.enrichable) {
      const done = (t.enriched ?? 0) + (t.enrich_failed ?? 0);
      if (t.dump_done && !t.enrich_done && t.dump_count) {
        enrEl.innerHTML = "";
        enrEl.append(document.createTextNode(`${fmtNum(done)}/${fmtNum(t.dump_count)}`));
        enrEl.append(progressBar(done, t.dump_count));
      } else {
        enrEl.innerHTML = `<span>${fmtNum(t.enriched ?? 0)}</span>`;
      }
    }
    if (failEl && t.enrichable) {
      failEl.innerHTML = `<span class="${t.enrich_failed ? "bad-c" : "dim"}">${t.enrich_failed ?? 0}</span>`;
    }
    if (stateEl) {
      const stateKind = t.terminal ? "ok" : /dumping|enriching/.test(t.state || "") ? "info" : "dim";
      stateEl.innerHTML = "";
      stateEl.append(tag(t.terminal ? "done" : t.state || "…", stateKind));
    }
  }
  // Update kv summary numbers
  const kvPatch = (anchor, val, cls) => {
    const el2 = box.querySelector(`[data-kv="${anchor}"]`);
    if (!el2) return;
    el2.textContent = String(val);
    el2.className = `v ${cls || ""}`;
  };
  const tr = d.track || {};
  kvPatch("t-new",     fmtNum(tr.new));
  kvPatch("t-changed", fmtNum(tr.changed));
  kvPatch("t-unchanged", fmtNum(tr.unchanged));
  kvPatch("t-shrank",  fmtNum(tr.body_shrank));
  kvPatch("t-dropped", fmtNum(tr.body_dropped), tr.body_dropped ? "bad" : "");
  kvPatch("t-error",   fmtNum(tr.body_error),   tr.body_error   ? "bad" : "");
  const ap = d.approve || {};
  const mk = d.markers || {};
  kvPatch("a-auto",   fmtNum(ap.auto),  "good");
  kvPatch("a-holds",  fmtNum(ap.holds), ap.holds ? "good" : "");
  kvPatch("m-scrape",  mk.scrape_done  ? "✓" : "·", mk.scrape_done  ? "good" : "");
  kvPatch("m-track",   mk.track_done   ? "✓" : "·", mk.track_done   ? "good" : "");
  kvPatch("m-approve", mk.approve_done ? "✓" : "·", mk.approve_done ? "good" : "");
}

function heldSection(d, ctx) {
  const card = el("div", "card mt");
  const h3 = el("h3", null, `held for review (${d.held.length})`);
  h3.append(infoDot("Held for review",
    "Held articles are staged edits where the body was dropped or the fetch " +
    "errored AND a live version already exists. (A shrunken body is flagged for " +
    "information but always auto-approves.)\n\n" +
    "Approve promotes pending → live (the old live copy is archived first) and " +
    "hands the article to P2. Reject deletes the pending copy; live stays exactly " +
    "as it was. Decisions are sent to the Approve Lambda asynchronously, so counts " +
    "update on the next refresh.\n\n" +
    "The run only closes once every hold is resolved."));
  card.append(h3);
  const chosen = new Map();
  if (canDrive(ctx)) {
    const bar = el("div", "btn-row mb");
    const bA = el("button", "btn ok sm", "✓ Approve all");
    const bR = el("button", "btn danger sm", "✕ Reject all");
    const bSA = el("button", "btn ok sm", "Approve selected");
    const bSR = el("button", "btn danger sm", "Reject selected");
    const syncSel = () => {
      bSA.textContent = `Approve selected (${chosen.size})`;
      bSR.textContent = `Reject selected (${chosen.size})`;
      bSA.disabled = bSR.disabled = !chosen.size;
    };
    const actSelected = (action) => {
      const items = [...chosen.values()];
      confirmModal(
        `${action === "approve" ? "Approve" : "Reject"} ${items.length} selected article(s)?`,
        action === "approve"
          ? "Each is promoted to live/ and handed to P2."
          : "Pending versions are dropped; live stays untouched.",
        async () => {
          for (const h of items) {
            await api.approve({ action, run_date: d.run_date,
              type_key: h.type_key, id: h.id, actor: "console" });
          }
          chosen.clear();
          card.querySelectorAll("input.sel-cb").forEach((cb) => { cb.checked = false; });
          syncSel();
          toast(`${items.length} ${action} action(s) sent — they apply as the Approve Lambda processes them`);
        }, { danger: action === "reject" });
    };
    bSA.onclick = () => actSelected("approve");
    bSR.onclick = () => actSelected("reject");
    bA.onclick = () => confirmModal(`Approve ALL ${d.held.length} held articles?`,
      "They will be promoted to live/ and handed to P2.", async () => {
        await api.approve({ action: "approve_all", run_date: d.run_date, actor: "console" });
        toast("approve_all sent");
      });
    bR.onclick = () => confirmModal(`Reject ALL ${d.held.length} held articles?`,
      "Pending versions are dropped; live stays untouched.", async () => {
        await api.approve({ action: "reject_all", run_date: d.run_date, actor: "console" });
        toast("reject_all sent");
      }, { danger: true });
    bar.append(bA, bR, bSA, bSR);
    syncSel();
    card.append(bar);
  }
  for (const h of d.held) {
    const row = el("div", "card");
    row.style.marginTop = "8px";
    const top = el("div", "toolbar");
    if (canDrive(ctx)) {
      const cb = el("input", "sel-cb");
      cb.type = "checkbox";
      const key = `${h.type_key}/${h.id}`;
      cb.onchange = () => {
        if (cb.checked) chosen.set(key, h); else chosen.delete(key);
        const n = chosen.size;
        card.querySelectorAll(".btn-row .btn").forEach((b) => {
          if (b.textContent.startsWith("Approve selected")) { b.textContent = `Approve selected (${n})`; b.disabled = !n; }
          if (b.textContent.startsWith("Reject selected"))  { b.textContent = `Reject selected (${n})`;  b.disabled = !n; }
        });
      };
      top.append(cb);
    }
    const idLink = html("span", null,
      `<a class="mono" href="#/article/${esc(h.type_key)}/${esc(h.id)}"><b>${esc(h.id)}</b></a>` +
      ` <span class="dim">· ${esc(h.type_key)} · ${esc(h.op || "")}</span>`);
    top.append(idLink);
    top.append(riskChips(h.risk));
    const right = el("span", "right dim small",
      h.live_chars != null ? `live ${fmtNum(h.live_chars)} → pending ${fmtNum(h.pending_chars)} chars` : "");
    top.append(right);
    row.append(top);
    if (h.error_msg) row.append(el("div", "alert error", h.error_msg));
    if (h.live_excerpt) row.append(el("div", "dim small", `live excerpt: ${h.live_excerpt}`));
    const act = el("div", "btn-row mt");
    const diffBtn = el("button", "btn sm", "View diff");
    diffBtn.onclick = async () => {
      try {
        const diff = await api.diff(h.type_key, h.id);
        modal({ title: `${h.id} — live vs pending`, wide: true, body: diffView(diff) });
      } catch (e) { toast(e.message, true); }
    };
    act.append(diffBtn);
    if (canDrive(ctx)) {
      const bA = el("button", "btn ok sm", "Approve");
      const bR = el("button", "btn danger sm", "Reject");
      bA.onclick = () => confirmModal(`Approve ${h.id}?`, "Promotes pending → live, hands to P2.",
        async () => {
          await api.approve({ action: "approve", run_date: d.run_date,
            type_key: h.type_key, id: h.id, actor: "console" });
          toast(`${h.id} approve sent`);
        });
      bR.onclick = () => confirmModal(`Reject ${h.id}?`, "Drops the pending version.",
        async () => {
          await api.approve({ action: "reject", run_date: d.run_date,
            type_key: h.type_key, id: h.id, actor: "console" });
          toast(`${h.id} reject sent`);
        }, { danger: true });
      act.append(bA, bR);
    }
    if (h.article_url) {
      const a = el("a", "btn sm ghost", "Open on my.f5.com ↗");
      a.href = h.article_url; a.target = "_blank";
      act.append(a);
    }
    row.append(act);
    card.append(row);
  }
  return card;
}

// ═════════════════════════════════════════════════════════════════════════════
//  Corpus browser
// ═════════════════════════════════════════════════════════════════════════════
export async function corpusPage(view, params, ctx) {
  const typeKey = params[0] || null;
  if (typeKey) return articlesPage(view, typeKey, ctx);

  view.append(skeleton(5));
  const c = await api.corpus();
  view.innerHTML = "";
  const refreshBtn = el("button", "btn sm ghost", "↻ recount");
  refreshBtn.onclick = async () => {
    refreshBtn.disabled = true;
    await api.corpus(true);
    corpusPage(clearView(view), params, ctx);
  };
  pageHead(view, "Corpus", `${fmtNum(c.total)} live articles`, [refreshBtn]);

  const entries = Object.entries(c.counts || {}).sort((a, b) => b[1] - a[1]);
  const known = new Set(entries.map(([t]) => t));
  for (const t of c.types || []) if (!known.has(t)) entries.push([t, 0]);
  if (!entries.length) { view.append(empty("no live corpus on this target")); return; }

  const rows = entries.map(([t, n]) => ({
    type_key: t,
    cells: [
      { html: `<span class="mono">${esc(t)}</span>` },
      { text: fmtNum(n), cls: "num" },
      { el: n ? tag("browse ›", "info") : tag("empty", "dim") },
    ],
  }));
  view.append(table(["type", { label: "articles", cls: "num" }, ""], rows,
    { onRow: (r) => ctx.navigate(`#/corpus/${r.type_key}`) }));
}

async function articlesPage(view, typeKey, ctx) {
  view.innerHTML = "";
  pageHead(view, typeKey, "live corpus", [
    Object.assign(el("a", "btn sm ghost", "‹ all types"), { href: "#/corpus" }),
  ]);

  const bar = el("div", "toolbar");
  const q = el("input", "inp grow");
  q.placeholder = "filter by article id…";
  bar.append(q);
  view.append(bar);
  const listBox = el("div");
  view.append(listBox);

  let page = 1;
  let query = "";
  async function load() {
    listBox.innerHTML = "";
    listBox.append(skeleton(6));
    let data;
    try { data = await api.articles(typeKey, query, page, 25); }
    catch (e) { listBox.innerHTML = ""; listBox.append(empty(e.message)); return; }
    listBox.innerHTML = "";
    if (!data.rows.length) { listBox.append(empty("no articles match")); return; }
    const rows = data.rows.map((a) => ({
      id: a.id,
      cells: [
        { html: `<span class="mono nowrap">${esc(a.id)}</span>` },
        { text: a.title || "(no title)" },
        { html: a.has_pending ? '<span class="tag warn">pending</span>' : "" },
        { text: fmtNum(a.body_chars), cls: "num" },
        { text: fmtTime(a.updated), cls: "mono nowrap" },
      ],
    }));
    listBox.append(table(["id", "title", "", { label: "body chars", cls: "num" }, "updated"],
      rows, { onRow: (r) => ctx.navigate(`#/article/${typeKey}/${r.id}`) }));
    listBox.append(pager(data.page, data.pages, (p) => { page = p; load(); }));
    listBox.append(el("div", "dim small mt", `${fmtNum(data.total)} articles`));
  }
  let deb;
  q.oninput = () => { clearTimeout(deb); deb = setTimeout(() => { query = q.value.trim(); page = 1; load(); }, 300); };
  await load();
}

// ═════════════════════════════════════════════════════════════════════════════
//  Article detail — view / edit / diff / history / restore
// ═════════════════════════════════════════════════════════════════════════════
export async function articlePage(view, params, ctx) {
  const [typeKey, artId] = params;
  view.append(skeleton(8));
  let a;
  try { a = await api.article(typeKey, artId); }
  catch (e) { view.innerHTML = ""; view.append(empty(e.message)); return; }
  view.innerHTML = "";

  const art = a.live || a.pending;
  const meta = (art || {}).metadata || {};
  const title = meta.title || art.title || "(no title)";
  const url = meta.url || art.link || null;

  const right = [];
  if (url) {
    const open = el("a", "btn sm ghost", "my.f5.com ↗");
    open.href = url; open.target = "_blank";
    right.push(open);
  }
  if (ctx.cfg.writable) {
    const edit = el("button", "btn sm", "✎ Edit JSON");
    edit.onclick = () => editArticleModal(typeKey, artId, a.live || a.pending, ctx, view, params);
    right.push(edit);
  }
  pageHead(view, artId, `${typeKey} — ${title}`, right);

  const badges = el("div", "toolbar");
  badges.append(tag(a.live ? "live" : "not live", a.live ? "ok" : "dim"));
  if (a.pending) badges.append(tag("pending staged", "warn"));
  if ((a.archive || []).length) badges.append(tag(`${a.archive.length} archived versions`, "info"));
  view.append(badges);

  // tabs: body | metadata | raw json | pending diff | history | versions
  const tabs = [
    ["body", "Body"],
    ["metadata", "Metadata"],
    ["json", "Raw JSON"],
  ];
  if (a.pending) tabs.push(["diff", "Pending diff"]);
  if ((a.archive || []).length) tabs.push(["versions", `Versions (${a.archive.length})`]);
  if ((a.history || []).length) tabs.push(["history", `History (${a.history.length})`]);

  const seg = el("div", "seg mb");
  const content = el("div", "mt");
  const render = {};

  render.body = () => {
    const c = (art || {}).content || {};
    const body = c.body_text || c.bodyText || "";
    if (!body) return empty("no body text on this article" + (c.bodyError ? ` — bodyError: ${c.bodyError}` : ""));
    return el("div", "article-body", body);
  };
  render.metadata = () => {
    const card = el("div", "card");
    for (const [k, v] of Object.entries(meta)) {
      card.append(html("div", "kv", `<span class="mono">${esc(k)}</span>` +
        `<span class="v" style="max-width:70%;text-align:right">${esc(
          typeof v === "string" ? v : JSON.stringify(v))}</span>`));
    }
    return card;
  };
  render.json = () => jsonBlock(art);
  render.diff = () => {
    const box = el("div");
    box.append(el("div", "skel"));
    api.diff(typeKey, artId).then((d) => { box.innerHTML = ""; box.append(diffView(d)); })
      .catch((e) => { box.innerHTML = ""; box.append(empty(e.message)); });
    return box;
  };
  render.versions = () => {
    const box = el("div");
    const rows = (a.archive || []).map((v) => ({
      key: v.key,
      cells: [
        { html: `<span class="mono">${esc(v.ts)}</span>` },
        { html: `<span class="mono dim small">${esc(v.key)}</span>` },
      ],
    }));
    box.append(table(["archived at", "key"], rows, {
      onRow: (r) => archiveVersionModal(typeKey, artId, r.key, ctx, view, params),
    }));
    box.append(el("div", "dim small mt",
      "each entry is the pre-overwrite copy taken when live was replaced (approve / restore / edit)"));
    return box;
  };
  render.history = () => {
    const rows = (a.history || []).map((h) => ({
      cells: [
        { el: tag(h.op || "?", h.op === "restored" ? "warn" : h.op === "edited" ? "info" : "ok") },
        { text: fmtTime(h.ts), cls: "mono nowrap" },
        { text: h.approved_by || h.actor || "" },
        { html: `<span class="dim small">${esc(h.run_date || "")}${h.restored_from ? " · from " + esc(h.restored_from) : ""}</span>` },
      ],
    }));
    return table(["op", "when", "by", "detail"], rows);
  };

  let active = null;
  const btns = {};
  function show(name) {
    active = name;
    for (const [k, b] of Object.entries(btns)) b.classList.toggle("on", k === name);
    content.innerHTML = "";
    content.append(render[name]());
  }
  for (const [name, label] of tabs) {
    const b = el("button", null, label);
    b.onclick = () => show(name);
    btns[name] = b;
    seg.append(b);
  }
  view.append(seg, content);
  show(tabs[0][0]);
}

function archiveVersionModal(typeKey, artId, archiveKey, ctx, view, params) {
  const body = el("div");
  body.append(el("div", "skel"));
  const actions = [{ label: "Close", cls: "ghost" }];
  if (ctx.cfg.writable) {
    actions.unshift({
      label: "Restore this version", cls: "danger",
      onClick: () => new Promise((resolve) => {
        confirmModal(`Restore ${artId} from ${archiveKey.split("/").pop()}?`,
          "Current live is archived first; hash-index and audit logs are updated. " +
          (ctx.cfg.mode === "aws" ? "Runs via the Restore Lambda." : "Applied to the local tree."),
          async () => {
            const r = await api.restore({ type_key: typeKey, art_id: artId,
              archive_key: archiveKey, actor: "console" });
            if (r.status === "error" || r.status === "refused") {
              toast(r.error || r.reason || "restore refused", true);
            } else {
              toast(`restored ${artId}`);
              articlePage(clearView(view), params, ctx);
            }
            resolve(true);
          }, { danger: true });
        resolve(false);
      }),
    });
  }
  const m = modal({ title: `${artId} @ ${archiveKey.split("/").pop()}`, wide: true, body, actions });
  Promise.all([api.object(archiveKey), api.diff(typeKey, artId, archiveKey).catch(() => null)])
    .then(([obj, d]) => {
      body.innerHTML = "";
      if (d) {
        body.append(el("h3", null, "diff vs current live"));
        body.append(diffView(d));
        body.append(el("h3", "mt", "archived envelope"));
      }
      body.append(jsonBlock(obj));
    })
    .catch((e) => { body.innerHTML = ""; body.append(empty(e.message)); });
  return m;
}

function editArticleModal(typeKey, artId, article, ctx, view, params) {
  const body = el("div");
  body.append(el("div", "alert warn",
    "Direct edit: saving archives the current live version, rewrites live, refreshes " +
    "the hash-index, and appends to the audit trail. The envelope hashes are recomputed server-side."));
  const ta = el("textarea", "inp");
  ta.rows = 24;
  ta.value = JSON.stringify(article, null, 2);
  body.append(ta);
  modal({
    title: `Edit ${typeKey}/${artId}`,
    wide: true,
    body,
    actions: [
      { label: "Cancel", cls: "ghost" },
      {
        label: "Save to live", cls: "primary",
        onClick: async () => {
          let parsed;
          try { parsed = JSON.parse(ta.value); }
          catch (e) { toast(`invalid JSON: ${e.message}`, true); return false; }
          const r = await api.saveArticle({ type_key: typeKey, id: artId,
            article: parsed, actor: "console" });
          toast(r.note ? `saved — ${r.note}` : "saved to live");
          articlePage(clearView(view), params, ctx);
        },
      },
    ],
  });
}

// ═════════════════════════════════════════════════════════════════════════════
//  Review — pending staged articles across the corpus
// ═════════════════════════════════════════════════════════════════════════════
export async function reviewPage(view, params, ctx) {
  view.append(skeleton(5));
  const [pend, runs] = await Promise.all([api.pending(2000), api.runs(5)]);
  view.innerHTML = "";
  pageHead(view, "Review", "staged edits awaiting approval — nothing here touches live until approved");

  // Held articles of the most recent open run get first-class treatment.
  const openRun = (runs || []).find((r) => !r.closed);
  if (openRun) {
    const d = await api.runDetail(openRun.run_date);
    if ((d.held || []).length) view.append(heldSection(d, ctx));
    else view.append(el("div", "alert info",
      `run ${openRun.run_date} is ${d.phase} — no articles held so far`));
  }

  const byType = {};
  for (const e of pend.entries || []) (byType[e.type_key] ||= []).push(e);
  const types = Object.keys(byType).sort();
  const card = el("div", "card mt");
  const pendH = el("h3", null,
    `pending staged (${fmtNum(pend.total)}${pend.capped ? ", showing first " + pend.entries.length : ""})`);
  pendH.append(infoDot("Pending staged",
    "Everything a run scrapes lands in pending/ first — nothing touches the live " +
    "corpus until the approve phase promotes it.\n\n" +
    "Safe articles are promoted automatically at approve; only risk-flagged ones " +
    "wait for a human (see the held section above when a run has holds). A large " +
    "pending count during an open run is normal — it drains when the run reaches " +
    "approve."));
  card.append(pendH);
  if (!types.length) {
    card.append(empty("pending queue is empty — live corpus and upstream are in sync"));
  }

  // Bulk approve/reject on the pending list (console-side; held articles above
  // still go through the Approve Lambda).
  const chosen = new Map(); // "type/id" -> {type_key, id}
  let bulkBar = null;
  const syncBulk = () => {
    if (!bulkBar) return;
    bulkBar.querySelectorAll("button").forEach((b) => {
      const verb = b.dataset.verb;
      if (!verb) return;
      b.textContent = `${verb === "approve" ? "✓ Approve" : "✕ Reject"} selected (${chosen.size})`;
      b.disabled = !chosen.size;
    });
  };
  const bulkAct = (action) => {
    const items = [...chosen.values()];
    confirmModal(
      `${action === "approve" ? "Approve" : "Reject"} ${items.length} pending article(s)?`,
      action === "approve"
        ? "Each is promoted to live/ with the full protocol (archive-before-overwrite, " +
          "hash-index, audit). NOTE: no P2 handoff message is published — run a backfill " +
          "afterwards if P2 must receive these."
        : "The pending copies are deleted and a rejected audit record is written; " +
          "live stays exactly as it is.",
      async () => {
        let done = 0, errors = 0;
        for (let i = 0; i < items.length; i += 500) {
          const r = await api.pendingAction({ action, items: items.slice(i, i + 500), actor: "console" });
          done += r.done || 0; errors += (r.errors || 0) + (r.missing || 0);
        }
        chosen.clear();
        toast(`${done} ${action}${action === "approve" ? "d" : "ed"}` +
              (errors ? ` · ${errors} skipped/failed` : ""), !!errors);
        reviewPage(clearView(view), params, ctx);
      }, { danger: action === "reject" });
  };
  if (canDrive(ctx) && types.length) {
    bulkBar = el("div", "btn-row mb");
    const bA = el("button", "btn ok sm"); bA.dataset.verb = "approve";
    const bR = el("button", "btn danger sm"); bR.dataset.verb = "reject";
    bA.onclick = () => bulkAct("approve");
    bR.onclick = () => bulkAct("reject");
    bulkBar.append(bA, bR);
    card.append(bulkBar);
    syncBulk();
  }

  for (const t of types) {
    const group = el("details");
    group.open = types.length <= 3;
    const sum = el("summary");
    sum.style.cursor = "pointer";
    sum.style.padding = "6px 0";
    sum.append(html("span", null, `<b class="mono">${esc(t)}</b> <span class="dim">· ${byType[t].length} staged</span>`));
    if (canDrive(ctx)) {
      const selAll = el("button", "btn sm ghost", "select all");
      selAll.style.marginLeft = "10px";
      selAll.onclick = (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        const boxes = group.querySelectorAll("input.psel");
        const allOn = [...boxes].every((cb) => cb.checked);
        boxes.forEach((cb) => {
          cb.checked = !allOn;
          cb.dispatchEvent(new Event("change"));
        });
      };
      sum.append(selAll);

      const typeAct = (action) => (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        confirmModal(
          `${action === "approve" ? "Approve" : "Reject"} EVERY pending ${t} article?`,
          (action === "approve"
            ? "Acts on every pending/ object of this type (not just the rows shown — " +
              "the list is capped). Each is promoted to live/ with archive-before-" +
              "overwrite + hash-index + audit. No P2 handoff is published — backfill " +
              "afterwards if P2 needs these. Large types take a while; leave the tab open."
            : "Deletes every pending/ object of this type (not just the rows shown). " +
              "Live stays untouched; a rejected_type audit record is written."),
          async () => {
            const r = await api.pendingAction({ action, type_key: t, actor: "console" });
            toast(`${t}: ${r.done} ${action}${action === "approve" ? "d" : "ed"} of ${r.total}` +
                  (r.errors ? ` · ${r.errors} failed` : ""), !!r.errors);
            reviewPage(clearView(view), params, ctx);
          }, { danger: true });
      };
      const tA = el("button", "btn ok sm", "✓ type");
      tA.title = `approve every pending ${t} article`;
      tA.onclick = typeAct("approve");
      const tR = el("button", "btn danger sm", "✕ type");
      tR.title = `reject every pending ${t} article`;
      tR.onclick = typeAct("reject");
      tA.style.marginLeft = "8px";
      tR.style.marginLeft = "4px";
      sum.append(tA, tR);
    }
    group.append(sum);
    const rows = byType[t].map((e) => {
      const cells = [];
      if (canDrive(ctx)) {
        const cb = el("input", "psel");
        cb.type = "checkbox";
        cb.onclick = (ev) => ev.stopPropagation();
        cb.onchange = () => {
          const key = `${e.type_key}/${e.id}`;
          if (cb.checked) chosen.set(key, { type_key: e.type_key, id: e.id });
          else chosen.delete(key);
          syncBulk();
        };
        cells.push({ el: cb });
      }
      cells.push(
        { html: `<span class="mono">${esc(e.id)}</span>` },
        { html: `<span class="dim small mono">${esc(e.key)}</span>` },
        { el: tag("view ›", "info") },
      );
      return { e, cells };
    });
    const heads = canDrive(ctx) ? ["", "id", "key", ""] : ["id", "key", ""];
    group.append(table(heads, rows,
      { onRow: (r) => ctx.navigate(`#/article/${r.e.type_key}/${r.e.id}`) }));
    card.append(group);
  }
  view.append(card);
  ctx.setRefresh(() => refreshInPlace(view, params, ctx, reviewPage), 20000);
}

// ═════════════════════════════════════════════════════════════════════════════
//  History — changelog + decisions
// ═════════════════════════════════════════════════════════════════════════════
export async function historyPage(view, params, ctx) {
  view.append(skeleton(5));
  let mode = params[0] === "decisions" ? "decisions" : "changelog";
  const data = mode === "decisions" ? await api.decisions("") : await api.changelog("");
  view.innerHTML = "";
  pageHead(view, "History", "the audit trail — every applied change and every decision");

  const bar = el("div", "toolbar");
  const seg = el("div", "seg");
  const bC = el("button", mode === "changelog" ? "on" : null, "Changes");
  const bD = el("button", mode === "decisions" ? "on" : null, "Decisions");
  bC.onclick = () => ctx.navigate("#/history");
  bD.onclick = () => ctx.navigate("#/history/decisions");
  seg.append(bC, bD);
  bar.append(seg);

  const monthSel = el("select", "inp");
  monthSel.append(new Option("latest month", ""));
  for (const m of data.months || []) monthSel.append(new Option(m, m));
  bar.append(monthSel);

  const filter = el("input", "inp grow");
  filter.placeholder = "filter by id / type / actor…";
  bar.append(filter);
  view.append(bar);

  const listBox = el("div");
  view.append(listBox);

  let rows = data.rows || [];
  async function reload() {
    const d2 = mode === "decisions"
      ? await api.decisions(monthSel.value)
      : await api.changelog(monthSel.value);
    rows = d2.rows || [];
    draw();
  }
  function draw() {
    listBox.innerHTML = "";
    const q = filter.value.trim().toLowerCase();
    const shown = q
      ? rows.filter((r) => JSON.stringify(r).toLowerCase().includes(q))
      : rows;
    if (!shown.length) { listBox.append(empty("no entries")); return; }
    const trows = shown.slice(0, 400).map((r) => {
      const op = r.op || "?";
      const kind = op.includes("reject") ? "bad"
        : op === "restored" ? "warn" : op === "edited" ? "info" : "ok";
      const id = r.id || "";
      const t = r.type || r.type_key || r.documentType || "";
      return {
        r,
        cells: [
          { el: tag(op, kind) },
          { html: `<a class="mono" href="#/article/${esc(t)}/${esc(id)}">${esc(id)}</a>` },
          { html: `<span class="mono small">${esc(t)}</span>` },
          { text: r.approved_by || r.actor || r.source || "" },
          { text: fmtTime(r.ts), cls: "mono nowrap" },
          { html: `<span class="dim small">${esc(r.run_date || "")}` +
                  `${r.changed ? " · " + esc(String(r.changed)) : ""}` +
                  `${r.restored_from ? " · from " + esc(r.restored_from) : ""}</span>` },
        ],
      };
    });
    listBox.append(table(["op", "id", "type", "by", "when", "detail"], trows));
    if (shown.length > 400) listBox.append(el("div", "dim small mt",
      `showing 400 of ${fmtNum(shown.length)} — narrow the filter`));
  }
  monthSel.onchange = reload;
  let deb;
  filter.oninput = () => { clearTimeout(deb); deb = setTimeout(draw, 250); };
  draw();
}

// ═════════════════════════════════════════════════════════════════════════════
//  Ops — infra health, errors, backfill, restore, raw browser
// ═════════════════════════════════════════════════════════════════════════════
function dlqMessagesModal(queue, ctx) {
  const body = el("div");
  body.append(skeleton(4));
  modal({ title: queue, wide: true,
    sub: "up to 10 messages, peeked without consuming — nothing is deleted", body });
  const doRedrive = (messageId, label) => confirmModal(
    `Redrive ${label}?`,
    "Sends the message body back to the matching WORK queue and deletes it from " +
    "the DLQ — the type resumes from its saved cursor. Fix the underlying cause " +
    "first: an unfixed message returns here after 3 more failed deliveries.",
    async () => {
      const r = await api.redrive({ queue, message_id: messageId });
      toast(r.status === "not_found"
        ? "message not received in the polling window — retry"
        : `${r.moved} message(s) redriven to ${r.to}`, r.status === "not_found");
    });
  api.dlqMessages(queue).then((msgs) => {
    body.innerHTML = "";
    if (!msgs.length) {
      body.append(empty("DLQ is empty — nothing to inspect"));
      return;
    }
    if (ctx && canDrive(ctx) && msgs.length > 1) {
      const all = el("button", "btn primary sm mb", `⟳ Redrive all (${msgs.length})`);
      all.onclick = () => doRedrive(null, `ALL ${msgs.length} messages`);
      body.append(el("div", "btn-row mb", [all]));
    }
    for (const m of msgs) {
      const box = el("div", "card mb");
      const top = el("div", "toolbar");
      top.append(html("span", "mono small", esc(m.message_id || "")));
      const meta = [];
      if (m.sent_at) meta.push(`sent ${fmtTime(m.sent_at)}`);
      if (m.receive_count) meta.push(`${m.receive_count} delivery attempt(s)`);
      top.append(el("span", "right dim small", meta.join(" · ")));
      box.append(top);
      box.append(jsonBlock(m.body));
      if (ctx && canDrive(ctx)) {
        const rd = el("button", "btn ok sm", "⟳ Redrive this message");
        rd.onclick = () => doRedrive(m.message_id, m.message_id);
        box.append(el("div", "btn-row mt", [rd]));
      }
      body.append(box);
    }
    if (!(ctx && canDrive(ctx))) {
      body.append(el("div", "alert info",
        "Redrive buttons need --allow-writes. Manual path: send the message body to " +
        "the matching work queue (aws sqs send-message), then delete it here."));
    }
  }).catch((e) => { body.innerHTML = ""; body.append(empty(e.message)); });
}

export async function opsPage(view, params, ctx) {
  view.innerHTML = "";
  pageHead(view, "Operations", "queues, errors, and the manual levers");

  const isAws = ctx.cfg.mode === "aws";
  if (isAws) view.append(healthCard());
  const grid = el("div", "grid cols-2");

  // queues
  const qCard = el("div", "card");
  const qH = el("h3", null, "queues");
  qH.append(infoDot("Queues",
    "The dump and enrich queues drive the pipeline Lambdas — a non-zero depth " +
    "while a run is working is normal. The count only shows VISIBLE messages: a " +
    "message being processed (in flight, up to 90 min) or waiting out a retry " +
    "backoff (delayed) is hidden, so a type can be busy while its queue reads 0.\n\n" +
    "A message in a DLQ (dead-letter queue) means that type failed all its " +
    "delivery attempts and was NOT processed — click the DLQ row to read the " +
    "messages, then see the playbook's incident section for the re-queue steps."));
  qCard.append(qH);
  if (!isAws) qCard.append(html("div", "dim small", "local mode — no live queues"));
  else {
    qCard.append(el("div", "skel"));
    api.dlqs().then((dlqs) => {
      qCard.innerHTML = "";
      qCard.append(qH);
      for (const [q, n] of Object.entries(dlqs)) {
        const isDlq = q.includes("dlq");
        const bad = isDlq && n > 0;
        const row = html("div", "kv", `<span class="mono">${esc(q)}</span>` +
          `<span class="v ${bad ? "bad" : n === 0 ? "good" : ""}">${n < 0 ? "?" : fmtNum(n)}` +
          `${isDlq ? ' <span class="dim small">view ›</span>' : ""}</span>`);
        if (isDlq) {
          row.style.cursor = "pointer";
          row.onclick = () => dlqMessagesModal(q, ctx);
        }
        qCard.append(row);
      }
      if (Object.values(dlqs).some((n) => typeof n === "number" && n > 0)) {
        qCard.append(el("div", "alert warn mt",
          "messages in a DLQ mean a type failed 3 retries — click the DLQ row to inspect them"));
      }
    }).catch((e) => { qCard.append(el("div", "dim small", e.message)); });
  }
  grid.append(qCard);

  // actions
  const aCard = el("div", "card");
  aCard.append(el("h3", null, "manual levers"));
  if (!ctx.cfg.writable) {
    aCard.append(el("div", "alert info", "read-only session — restart the server with --allow-writes to enable actions"));
  }
  if (isAws && ctx.cfg.writable) {
    const row1 = el("div", "btn-row mb");
    const inc = el("button", "btn primary sm", "▶ incremental run");
    const full = el("button", "btn sm", "▶ full run");
    inc.onclick = () => confirmModal("Trigger incremental run?", "Invokes the orchestrator.",
      async () => { await api.trigger("incremental"); toast("triggered"); });
    full.onclick = () => confirmModal("Trigger FULL run?", "Re-lists the whole corpus.",
      async () => { await api.trigger("full"); toast("triggered"); }, { danger: true });
    row1.append(inc, full);
    aCard.append(row1);

    const bf = el("button", "btn sm", "Publish backfill to P2…");
    bf.onclick = () => backfillModal(ctx);
    aCard.append(el("div", "btn-row mb", [bf]));

    // pipeline triggers: pause / resume / purge
    const pRow = el("div", "btn-row mb");
    aCard.append(pRow);
    api.pipeline().then((ps) => {
      let paused = !!ps.paused;
      const pb = el("button", "btn sm");
      const syncPb = () => {
        pb.textContent = paused ? "▶ Resume pipeline" : "⏸ Pause pipeline";
      };
      syncPb();
      pb.onclick = () => confirmModal(
        paused ? "Resume the pipeline?" : "Pause the pipeline?",
        paused ? "Re-enables the dump + enrich SQS triggers."
               : "Disables the dump + enrich SQS triggers. Nothing is deleted — messages wait.",
        async () => {
          const r = await api.pipelineAction(paused ? "resume" : "pause");
          paused = r.status === "paused";
          syncPb();
          toast(`pipeline ${r.status}`);
        });
      const purge = el("button", "btn danger sm", "Purge work queues");
      purge.onclick = () => confirmModal("Purge the dump + enrich WORK queues?",
        "Queued (unprocessed) messages are permanently deleted. DLQs are untouched. " +
        "Use to kill a runaway self-requeue chain — pause the pipeline first.",
        async () => {
          const r = await api.pipelineAction("purge_queues");
          toast(Object.entries(r).map(([q, v]) => `${q.split("-").slice(1, 3).join("-")}: ${v}`).join(" · "));
        }, { danger: true });
      pRow.append(pb, purge);
      for (const t of ps.triggers || []) {
        pRow.append(tag(`${t.function.replace(/^f5kb-/, "").replace(/-(staging|prod)$/, "")}: ${t.state}`,
          t.state === "Enabled" ? "ok" : "warn"));
      }
    }).catch(() => {});
  }
  const rs = el("button", "btn sm", "Restore an article…");
  rs.onclick = () => restoreModal(ctx);
  if (ctx.cfg.writable) aCard.append(el("div", "btn-row", [rs]));
  grid.append(aCard);
  view.append(grid);

  // raw key browser
  const bCard = el("div", "card mt");
  bCard.append(el("h3", null, "raw key browser"));
  const bBar = el("div", "toolbar");
  const prefixInp = el("input", "inp grow");
  prefixInp.placeholder = "prefix, e.g. runs/2026-07-08/ or lambda/state/";
  const go = el("button", "btn sm", "List");
  bBar.append(prefixInp, go);
  bCard.append(bBar);
  const bOut = el("div");
  bCard.append(bOut);
  go.onclick = async () => {
    bOut.innerHTML = "";
    bOut.append(el("div", "skel"));
    try {
      const r = await api.keys(prefixInp.value.trim(), 500);
      bOut.innerHTML = "";
      if (!r.keys.length) { bOut.append(empty("no keys")); return; }
      const rows = r.keys.map((k) => ({ k, cells: [{ html: `<span class="mono small">${esc(k)}</span>` }] }));
      bOut.append(table(["key"], rows, {
        onRow: async (row) => {
          try {
            const obj = await api.object(row.k);
            modal({ title: row.k, wide: true,
              body: obj && obj._raw !== undefined
                ? el("pre", "json", obj._raw) : jsonBlock(obj) });
          } catch (e) { toast(e.message, true); }
        },
      }));
      bOut.append(el("div", "dim small mt",
        `${fmtNum(r.total)} keys${r.capped ? " (showing 500)" : ""}`));
    } catch (e) { bOut.innerHTML = ""; bOut.append(empty(e.message)); }
  };
  view.append(bCard);

  // recent errors
  if (isAws) {
    const eCard = el("div", "card mt");
    eCard.append(el("h3", null, "recent lambda errors (24h)"));
    eCard.append(el("div", "skel"));
    api.errors(1440).then((errs) => {
      eCard.innerHTML = "";
      eCard.append(el("h3", null, "recent lambda errors (24h)"));
      if (!errs.length) { eCard.append(html("div", "good small", "✓ no errors in the last 24h")); return; }
      const rows = errs.slice(0, 60).map((e2) => ({
        cells: [
          { html: `<span class="tag bad">${esc(e2._lambda || "?")}</span>` },
          { text: fmtTime(e2.ts), cls: "mono nowrap" },
          { html: `<span class="small">${esc((e2.action || "") + " " + (e2.err_msg || e2.msg || ""))}</span>` },
        ],
      }));
      eCard.append(table(["lambda", "when", "message"], rows));
    }).catch((e2) => { eCard.append(el("div", "dim small", e2.message)); });
    view.append(eCard);

    view.append(logViewerCard());
    view.append(costCard());
  }
}

// ── health checks ─────────────────────────────────────────────────────────────
function healthCard() {
  const card = el("div", "card mb");
  const h = el("h3", null, "health checks");
  h.append(infoDot("Health checks",
    "End-to-end tests of everything the pipeline depends on, run from this " +
    "machine: fetch a Coveo guest token (Aura endpoint), run one live search, " +
    "read the S3 bucket (hash index), reach all four SQS queues, and confirm " +
    "every Lambda function is deployed.\n\n" +
    "First stop when a run won't start or a stage looks dead — a failing row " +
    "names the broken dependency and what to check."));
  card.append(h);
  const go = el("button", "btn primary sm", "▶ Run checks");
  card.append(el("div", "btn-row mb", [go]));
  const out = el("div");
  card.append(out);
  go.onclick = async () => {
    go.disabled = true;
    out.innerHTML = "";
    out.append(skeleton(5));
    try {
      const checks = await api.health();
      out.innerHTML = "";
      if (!checks.length) { out.append(empty("no checks on this target")); return; }
      for (const c of checks) {
        const row = el("div", "kv");
        row.append(html("span", null,
          `${c.ok ? '<span class="good">✓</span>' : '<span class="bad-c">✗</span>'} ` +
          `<span class="mono">${esc(c.name)}</span>`));
        row.append(html("span", "v " + (c.ok ? "good" : "bad"),
          `${esc(c.detail || "")} <span class="dim small">· ${c.ms}ms</span>`));
        out.append(row);
        if (!c.ok && c.hint) out.append(el("div", "alert warn", c.hint));
      }
    } catch (e) { out.innerHTML = ""; out.append(empty(e.message)); }
    finally { go.disabled = false; }
  };
  return card;
}

// ── cost + duration (from Lambda REPORT lines) ────────────────────────────────
function costCard() {
  const card = el("div", "card mt");
  const h = el("h3", null, "compute cost + duration");
  h.append(infoDot("Cost + duration",
    "Parsed from the Lambda REPORT log lines in the selected window: invocation " +
    "count, billed GB-seconds, peak duration, peak memory, and an estimated " +
    "dollar figure (x86 us-east-2 rates, before any free tier).\n\n" +
    "Compute only — S3/SQS/CloudWatch request costs are excluded (typically " +
    "cents). A type that resumes many times shows up as many invocations."));
  card.append(h);
  const bar = el("div", "toolbar");
  const winSel = el("select", "inp");
  for (const [v, l] of [[60, "1 h"], [1440, "24 h"], [10080, "7 d"]]) {
    winSel.append(new Option(l, String(v)));
  }
  winSel.value = "1440";
  const go = el("button", "btn primary sm", "Load");
  bar.append(winSel, go);
  card.append(bar);
  const out = el("div", "mt");
  card.append(out);
  async function load() {
    go.disabled = true;
    out.innerHTML = "";
    out.append(skeleton(4));
    try {
      const c = await api.costs(parseInt(winSel.value, 10));
      out.innerHTML = "";
      if (!(c.lambdas || []).length) {
        out.append(empty("no invocations in this window"));
        return;
      }
      const rows = c.lambdas.map((r) => ({
        cells: [
          { html: `<span class="mono">${esc(r.lambda)}</span>` },
          { text: fmtNum(r.invocations), cls: "num" },
          { text: fmtNum(r.gb_seconds), cls: "num" },
          { text: `${fmtNum(Math.round(r.max_duration_ms / 1000))}s`, cls: "num" },
          { text: `${r.max_memory_mb}/${r.memory_mb} MB`, cls: "num" },
          { text: `$${r.est_usd.toFixed(4)}`, cls: "num" },
        ],
      }));
      out.append(table(["lambda", { label: "invocations", cls: "num" },
        { label: "GB-s", cls: "num" }, { label: "max duration", cls: "num" },
        { label: "peak/alloc mem", cls: "num" }, { label: "est cost", cls: "num" }], rows));
      const t = c.totals || {};
      out.append(html("div", "kv mt",
        `<span><b>total</b></span><span class="v"><b>${fmtNum(t.invocations)} invocations · ` +
        `${fmtNum(t.gb_seconds)} GB-s · $${(t.est_usd || 0).toFixed(4)}</b></span>`));
      out.append(el("div", "dim small mt", c.note || ""));
    } catch (e) { out.innerHTML = ""; out.append(empty(e.message)); }
    finally { go.disabled = false; }
  }
  go.onclick = load;
  return card;
}

// ── log viewer: every level, every lambda, filterable ────────────────────────
const LOG_LAMBDAS = ["orchestrator", "dump", "enrich", "track", "approve",
  "restore", "watchdog", "slack-ack"];

function logViewerCard() {
  const card = el("div", "card mt");
  const h = el("h3", null, "logs — all lambdas, all levels");
  h.append(infoDot("Log viewer",
    "Reads CloudWatch Logs directly: INFO lines (article_staged, requeued_self, " +
    "article_skipped…), ERROR lines, and PLATFORM lines (START/END/REPORT — REPORT " +
    "shows duration + memory per invocation).\n\n" +
    "Filter by lambda, level, time window, and free text (matches the whole JSON " +
    "record — try an article id, a type key, or a run date). Click a row for the " +
    "full structured record."));
  card.append(h);

  const bar = el("div", "toolbar");
  const fnSel = el("select", "inp");
  fnSel.append(new Option("all lambdas", "all"));
  for (const f of LOG_LAMBDAS) fnSel.append(new Option(f, f));
  const lvlSel = el("select", "inp");
  for (const [v, l] of [["all", "all levels"], ["info", "INFO"], ["error", "ERROR"]]) {
    lvlSel.append(new Option(l, v));
  }
  const winSel = el("select", "inp");
  for (const [v, l] of [[15, "15 min"], [60, "1 h"], [180, "3 h"], [1440, "24 h"], [10080, "7 d"]]) {
    winSel.append(new Option(l, String(v)));
  }
  winSel.value = "180";
  const q = el("input", "inp grow");
  q.placeholder = "text filter — article id, type key, run date…";
  const sizeSel = el("select", "inp");
  for (const n of [200, 400, 1000]) sizeSel.append(new Option(`fetch ${n}`, String(n)));
  sizeSel.value = "400";
  const go = el("button", "btn primary sm", "Load");
  const tail = el("button", "btn sm", "▶ tail");
  tail.title = "auto-reload every 5s (live view during a run); pagination resets to page 1";
  bar.append(fnSel, lvlSel, winSel, q, sizeSel, go, tail);
  card.append(bar);

  const out = el("div", "mt");
  card.append(out);
  out.append(el("div", "dim small", "pick filters, press Load"));

  const LEVEL_KIND = { ERROR: "bad", INFO: "info", PLATFORM: "dim", RAW: "dim" };
  const PAGE = 50;
  let rows = [];
  let page = 1;

  function draw() {
    out.innerHTML = "";
    if (!rows.length) { out.append(empty("no log events match")); return; }
    const pages = Math.max(1, Math.ceil(rows.length / PAGE));
    page = Math.max(1, Math.min(page, pages));
    const window_ = rows.slice((page - 1) * PAGE, page * PAGE);
    const trows = window_.map((r) => ({
      r,
      cells: [
        { el: tag(r.lambda || "?", "info") },
        { el: tag(r.level || "?", LEVEL_KIND[r.level] || "dim") },
        { text: fmtTime(r.ts), cls: "mono nowrap" },
        { html: `<span class="small">${esc(r.msg || "")}</span>` },
      ],
    }));
    out.append(table(["lambda", "level", "when", "message"], trows, {
      onRow: (row) => modal({
        title: `${row.r.lambda} · ${row.r.level} · ${fmtTime(row.r.ts)}`,
        wide: true,
        body: jsonBlock(row.r.record || { message: row.r.msg }),
      }),
    }));
    out.append(pager(page, pages, (p) => { page = p; draw(); }));
    out.append(el("div", "dim small mt",
      `${fmtNum(rows.length)} events fetched (newest first) · page ${page}/${pages}`));
  }

  async function load() {
    out.innerHTML = "";
    out.append(skeleton(5));
    go.disabled = true;
    try {
      rows = await api.logs(fnSel.value, parseInt(winSel.value, 10),
        lvlSel.value, q.value.trim(), parseInt(sizeSel.value, 10));
      page = 1;
      draw();
    } catch (e) {
      out.innerHTML = "";
      out.append(empty(e.message));
    } finally {
      go.disabled = false;
    }
  }
  go.onclick = load;
  q.onkeydown = (ev) => { if (ev.key === "Enter") load(); };

  let tailTimer = null;
  const stopTail = () => {
    clearInterval(tailTimer);
    tailTimer = null;
    tail.textContent = "▶ tail";
    tail.classList.remove("primary");
  };
  tail.onclick = () => {
    if (tailTimer) { stopTail(); return; }
    tail.textContent = "⏸ tail";
    tail.classList.add("primary");
    load();
    tailTimer = setInterval(() => {
      // self-clean if the user navigated away and the card left the DOM
      if (!document.contains(card)) { stopTail(); return; }
      if (document.hidden || go.disabled) return;
      load();
    }, 5000);
  };
  return card;
}

// ═════════════════════════════════════════════════════════════════════════════
//  Integrations — SNS topics + subscriber queues (downstream ingestion status)
// ═════════════════════════════════════════════════════════════════════════════
export async function integrationsPage(view, params, ctx) {
  view.append(skeleton(5));
  let data = {};
  try { data = await api.integrations(); } catch (_) { /* local target */ }
  view.innerHTML = "";
  pageHead(view, "Integrations",
    "SNS topics, who subscribes to them, and each subscriber queue's backlog");

  if (!(data.topics || []).length) {
    view.append(empty(ctx.cfg.mode === "aws"
      ? "no f5kb SNS topics found for this stage"
      : "local mode — integrations live in the deployed AWS stage"));
    return;
  }

  const banner = el("div", "toolbar mb");
  banner.append(tag(data.last_handoff_run
    ? `last completed handoff: ${data.last_handoff_run}` : "no completed handoff yet",
    data.last_handoff_run ? "ok" : "dim"));
  banner.append(infoDot("Reading this page",
    "The handoff topic gets ONE message per completed run (after Gate 1); every " +
    "subscribed queue receives a copy.\n\n" +
    "A subscriber queue's visible + in-flight counts are its ingestion backlog: " +
    "0/0 means the consumer has drained everything it was sent; growing visible " +
    "means the consumer is behind or down. Queues in other AWS accounts can't be " +
    "read from here — they show as 'no access'."));
  view.append(banner);

  for (const t of data.topics || []) {
    const card = el("div", "card mb");
    const h = el("h3", null, t.name);
    if (t.is_handoff) h.append(document.createTextNode(" ")), h.append(tag("P2 handoff", "ok"));
    card.append(h);
    card.append(el("div", "dim small mono mb", t.arn));
    if (!(t.subscriptions || []).length) {
      card.append(empty("no subscribers"));
    } else {
      const rows = t.subscriptions.map((s) => {
        const qi = s.queue;
        let statusCell;
        if (!qi) statusCell = { html: `<span class="dim">—</span>` };
        else if (!qi.accessible) statusCell = { el: tag("no access", "dim") };
        else {
          const backlog = (qi.visible || 0) + (qi.in_flight || 0);
          statusCell = { html:
            `<span class="${backlog ? "" : "good"}">${fmtNum(qi.visible)} visible` +
            ` · ${fmtNum(qi.in_flight)} in-flight` +
            (qi.delayed ? ` · ${fmtNum(qi.delayed)} delayed` : "") + `</span>` };
        }
        return {
          cells: [
            { el: tag(s.protocol || "?", s.protocol === "sqs" ? "info" : "dim") },
            { html: `<span class="mono small">${esc(s.endpoint || "")}</span>` },
            statusCell,
            { el: qi && qi.accessible
                ? tag(((qi.visible || 0) + (qi.in_flight || 0)) ? "ingesting / backlog" : "drained",
                      ((qi.visible || 0) + (qi.in_flight || 0)) ? "warn" : "ok")
                : el("span") },
          ],
        };
      });
      card.append(table(["protocol", "endpoint", "queue backlog", "status"], rows));
    }
    view.append(card);
  }
  ctx.setRefresh(() => refreshInPlace(view, params, ctx, integrationsPage), 15000);
}

function backfillModal(ctx) {
  const body = el("div");
  body.append(el("div", "modal-sub",
    "Re-publishes a past run's manifest to the P2 handoff topic (batch=backfill). " +
    "All subscribers re-receive it — consumers must upsert idempotently."));
  const dateInp = el("input", "inp");
  dateInp.placeholder = "run date, e.g. 2026-07-01";
  const manifestInp = el("input", "inp");
  manifestInp.placeholder = "manifest key, e.g. runs/2026-07-01/approve/changed_ids.jsonl";
  manifestInp.style.width = "100%";
  const countInp = el("input", "inp");
  countInp.placeholder = "article count (from the manifest)";
  body.append(el("label", "lbl", "run date"), dateInp);
  body.append(el("label", "lbl", "manifest key"), manifestInp);
  body.append(el("label", "lbl", "article count"), countInp);
  dateInp.onchange = () => {
    if (dateInp.value && !manifestInp.value) {
      manifestInp.value = `runs/${dateInp.value.trim()}/approve/changed_ids.jsonl`;
    }
  };
  modal({
    title: "Publish backfill",
    body,
    actions: [
      { label: "Cancel", cls: "ghost" },
      {
        label: "Publish to SNS", cls: "danger",
        onClick: async () => {
          const run_date = dateInp.value.trim();
          const manifest_key = manifestInp.value.trim();
          if (!run_date || !manifest_key) { toast("run date + manifest key required", true); return false; }
          await api.backfill({ run_date, manifest_key,
            article_count: parseInt(countInp.value, 10) || 0 });
          toast("backfill published");
        },
      },
    ],
  });
}

function restoreModal(ctx) {
  const body = el("div");
  body.append(el("div", "modal-sub",
    "Pick the article, list its archived versions, then restore. Live is archived " +
    "before being overwritten, and the hash-index + audit trail are updated."));
  const typeInp = el("input", "inp");
  typeInp.placeholder = "type key, e.g. Support_Solution";
  const idInp = el("input", "inp");
  idInp.placeholder = "article id, e.g. K000135868";
  const go = el("button", "btn sm", "Find versions");
  const out = el("div", "mt");
  body.append(el("label", "lbl", "type key"), typeInp);
  body.append(el("label", "lbl", "article id"), idInp);
  body.append(el("div", "btn-row mt", [go]), out);
  go.onclick = async () => {
    out.innerHTML = "";
    try {
      const a = await api.article(typeInp.value.trim(), idInp.value.trim());
      if (!(a.archive || []).length) { out.append(empty("no archived versions for this article")); return; }
      for (const v of a.archive) {
        const row = el("div", "btn-row mb");
        row.append(html("span", "mono small", esc(v.ts)));
        const btn = el("button", "btn danger sm", "Restore");
        btn.onclick = () => confirmModal(`Restore ${a.id} @ ${v.ts}?`,
          "The current live version is archived first.", async () => {
            const r = await api.restore({ type_key: a.type_key, art_id: a.id,
              archive_key: v.key, actor: "console" });
            if (r.status === "error" || r.status === "refused") {
              toast(r.error || r.reason || "restore refused", true);
            } else toast(`restored ${a.id} from ${v.ts}`);
          }, { danger: true });
        row.append(btn);
        out.append(row);
      }
    } catch (e) { out.append(empty(e.message)); }
  };
  modal({ title: "Restore an article", body, actions: [{ label: "Close", cls: "ghost" }] });
}

// ═════════════════════════════════════════════════════════════════════════════
//  Docs / playbook
// ═════════════════════════════════════════════════════════════════════════════
export async function docsPage(view, params, ctx) {
  const name = params[0] || "playbook";
  view.append(skeleton(4));
  const docs = await api.docs();
  view.innerHTML = "";

  const layout = el("div", "docs-layout");
  const nav = el("div", "docs-nav card");
  const groups = {};
  for (const d of docs) (groups[d.group] ||= []).push(d);
  for (const [g, items] of Object.entries(groups)) {
    nav.append(el("div", "grp", g));
    for (const d of items) {
      const a = el("a", d.name === name ? "active" : null, d.title);
      a.href = `#/docs/${d.name}`;
      nav.append(a);
    }
  }
  layout.append(nav);

  const bodyBox = el("div", "docs-body");
  bodyBox.append(skeleton(10));
  layout.append(bodyBox);
  view.append(layout);

  try {
    const doc = await api.doc(name);
    bodyBox.innerHTML = "";
    bodyBox.append(html("div", "md", doc.html));
    // Rewrite internal .md links to console doc routes where possible.
    bodyBox.querySelectorAll("a[href]").forEach((a) => {
      const href = a.getAttribute("href") || "";
      if (/^https?:/.test(href)) { a.target = "_blank"; return; }
      const m = docs.find((d2) => href.toLowerCase().includes(d2.name) ||
        href.replace(/\.md$/i, "").toLowerCase() === d2.name);
      if (m) a.setAttribute("href", `#/docs/${m.name}`);
    });
  } catch (e) {
    bodyBox.innerHTML = "";
    bodyBox.append(empty(e.message));
  }
}

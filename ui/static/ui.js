// ui.js — DOM helpers + shared components (toast, modal, tables, stepper, diff).
"use strict";

export function el(tag, cls, content) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (content !== undefined) {
    if (typeof content === "string") e.textContent = content;
    else if (Array.isArray(content)) content.forEach((c) => c && e.append(c));
    else if (content) e.append(content);
  }
  return e;
}

export function html(tag, cls, markup) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  e.innerHTML = markup;
  return e;
}

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function fmtNum(n) {
  return (n ?? 0).toLocaleString("en-US");
}

export function fmtTime(iso) {
  if (!iso) return "—";
  const s = String(iso);
  return s.length > 19 ? s.slice(0, 19).replace("T", " ") + "Z" : s.replace("T", " ");
}

export function toast(msg, isErr) {
  const root = document.getElementById("toast-root");
  const t = el("div", "toast" + (isErr ? " err" : ""), msg);
  root.append(t);
  setTimeout(() => t.remove(), isErr ? 6000 : 3500);
}

// ── Modal ────────────────────────────────────────────────────────────────────
export function modal({ title, sub, body, wide, actions }) {
  const root = document.getElementById("modal-root");
  root.innerHTML = "";
  const scrim = el("div", "scrim");
  const box = el("div", "modal" + (wide ? " wide" : ""));
  box.append(el("h2", null, title));
  if (sub) box.append(el("div", "modal-sub", sub));
  if (body) box.append(body);
  const close = () => { root.innerHTML = ""; document.removeEventListener("keydown", onKey); };
  const bar = el("div", "modal-actions");
  for (const a of actions || []) {
    const b = el("button", "btn " + (a.cls || ""), a.label);
    b.onclick = async () => {
      if (a.onClick) {
        b.disabled = true;
        try { if ((await a.onClick()) !== false) close(); }
        catch (e) { toast(e.message, true); }
        b.disabled = false;
      } else close();
    };
    bar.append(b);
  }
  if (!(actions || []).length) bar.append(Object.assign(el("button", "btn", "Close"), { onclick: close }));
  box.append(bar);
  scrim.append(box);
  scrim.onclick = (ev) => { if (ev.target === scrim) close(); };
  const onKey = (ev) => { if (ev.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  root.append(scrim);
  return { close };
}

export function confirmModal(title, sub, onConfirm, { danger } = {}) {
  modal({
    title, sub,
    actions: [
      { label: "Cancel", cls: "ghost" },
      { label: "Confirm", cls: danger ? "danger" : "primary", onClick: onConfirm },
    ],
  });
}

// ── Tables ───────────────────────────────────────────────────────────────────
export function table(headers, rows, { onRow } = {}) {
  const wrap = el("div", "tbl-wrap");
  const t = el("table", "tbl");
  const thead = el("thead");
  const htr = el("tr");
  for (const h of headers) {
    const th = el("th", h.cls || null, h.label ?? h);
    htr.append(th);
  }
  thead.append(htr);
  t.append(thead);
  const tbody = el("tbody");
  for (const r of rows) {
    const tr = el("tr", onRow ? "rowlink" : null);
    for (const cell of r.cells) {
      const td = typeof cell === "object" && cell.el
        ? (() => { const d = el("td", cell.cls || null); d.append(cell.el); return d; })()
        : typeof cell === "object" && cell.html !== undefined
          ? html("td", cell.cls || null, cell.html)
          : el("td", (typeof cell === "object" && cell.cls) || null,
               typeof cell === "object" ? cell.text : String(cell ?? ""));
      tr.append(td);
    }
    if (onRow) tr.onclick = () => onRow(r);
    tbody.append(tr);
  }
  t.append(tbody);
  wrap.append(t);
  return wrap;
}

// ── Stat tile / stepper / tags ───────────────────────────────────────────────
export function stat(label, value, { cls, hint } = {}) {
  const c = el("div", "card stat");
  c.append(el("div", "label", label));
  c.append(el("div", "value " + (cls || ""), String(value)));
  if (hint) c.append(el("div", "hint", hint));
  return c;
}

export function stepper(phases, current) {
  const box = el("div", "stepper");
  const idx = phases.indexOf(current);
  phases.forEach((p, i) => {
    if (i) box.append(el("span", "step-line" + (idx >= 0 && i <= idx ? " past" : "")));
    const cls = i === idx ? " active" : idx >= 0 && i < idx ? " past" : "";
    const s = el("span", "step" + cls);
    s.append(el("span", "bubble", idx >= 0 && i < idx ? "✓" : String(i + 1)));
    s.append(el("span", "lbl", p));
    box.append(s);
  });
  if (idx < 0 && current) box.append(el("span", "tag dim", current));
  return box;
}

export function tag(text, kind) {
  return el("span", "tag " + (kind || "dim"), text);
}

// ── Info dot — a small ⓘ that opens a plain-language explanation ─────────────
export function infoDot(title, text) {
  const b = el("button", "info-dot", "i");
  b.type = "button";
  b.title = title;
  b.setAttribute("aria-label", `about: ${title}`);
  b.onclick = (ev) => {
    ev.stopPropagation();
    ev.preventDefault();
    const body = el("div");
    for (const para of String(text).split("\n\n")) body.append(el("p", "info-p", para));
    modal({ title, body });
  };
  return b;
}

export function progressBar(done, total, ok) {
  const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return html("div", "bar", `<div class="fill${ok ? " ok" : ""}" style="width:${pct}%"></div>`);
}

export function empty(msg) {
  return el("div", "empty", msg);
}

export function pager(page, pages, onGo) {
  const box = el("div", "pager");
  const prev = el("button", "btn sm ghost", "‹ prev");
  const next = el("button", "btn sm ghost", "next ›");
  prev.disabled = page <= 1;
  next.disabled = page >= pages;
  prev.onclick = () => onGo(page - 1);
  next.onclick = () => onGo(page + 1);
  box.append(prev, el("span", null, `page ${page} / ${pages}`), next);
  return box;
}

// ── Diff renderer (consumes /api/…/diff structured hunks) ──────────────────
export function diffView(d) {
  const box = el("div");
  if (d.metadata && d.metadata.length) {
    const card = el("div", "card mb");
    card.append(el("h3", null, `metadata changes (${d.metadata.length})`));
    for (const m of d.metadata) {
      card.append(html("div", "kv",
        `<span class="mono">${esc(m.field)}</span>` +
        `<span class="v"><span class="bad-c">${esc(m.old ?? "∅")}</span> → ` +
        `<span class="good">${esc(m.new ?? "∅")}</span></span>`));
    }
    box.append(card);
  }
  const hdr = el("div", "dim small mb",
    `body: ${fmtNum(d.old_chars)} chars → ${fmtNum(d.new_chars)} chars`);
  box.append(hdr);
  const dv = el("div", "diff");
  const addLine = (cls, gut, text) => {
    const line = el("div", "dline " + cls);
    line.append(el("span", "gut", gut));
    line.append(el("span", "txt", text));
    dv.append(line);
  };
  for (const h of d.body || []) {
    if (h.tag === "skip") addLine("hunk", "⋯", ` ${h.count} unchanged lines`);
    else for (const ln of h.lines || []) {
      if (h.tag === "add") addLine("add", "+", ln);
      else if (h.tag === "del") addLine("del", "−", ln);
      else addLine("", "", ln);
    }
  }
  if (!(d.body || []).length) dv.append(el("div", "dline", " (no body changes)"));
  box.append(dv);
  return box;
}

export function jsonBlock(obj) {
  return el("pre", "json", JSON.stringify(obj, null, 2));
}

export function skeleton(lines = 4) {
  const box = el("div");
  for (let i = 0; i < lines; i++) {
    const s = el("div", "skel mb");
    s.style.height = "18px";
    s.style.width = `${60 + Math.round(35 * ((i * 7919) % 100) / 100)}%`;
    box.append(s);
  }
  return box;
}

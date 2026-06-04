// Field flattening, metadata/content splitting, and field-catalogue building.
// Moved verbatim from dump_articles.ts — no algorithm or output-format change.
// (writeCatalogue still writes _catalogue.json/_catalogue.md byte-for-byte as
// before, via Deno.writeTextFile with no trailing newline.)

import { TypeConfig } from "../config/types.ts";

type CoveoResult = Record<string, unknown>;

// A flat view of one article's fields: top-level keys (except `raw`) plus every
// raw.* key, each tagged by source. Used for both output splitting and the
// catalogue. Bare field name is the key; top-level wins on a clash.
export function flattenFields(
  r: CoveoResult,
): Map<string, { source: "top" | "raw"; value: unknown }> {
  const fields = new Map<string, { source: "top" | "raw"; value: unknown }>();
  const raw = (r.raw as CoveoResult) ?? {};
  for (const [k, v] of Object.entries(raw)) fields.set(k, { source: "raw", value: v });
  for (const [k, v] of Object.entries(r)) {
    if (k === "raw") continue;
    fields.set(k, { source: "top", value: v }); // top-level overrides raw on clash
  }
  return fields;
}

// flattenFields is defined above; this guarded wrapper keeps a bad result from
// aborting the whole run.
export function flattenFieldsSafe(r: CoveoResult) {
  try {
    return flattenFields(r);
  } catch {
    return new Map<string, { source: "top" | "raw"; value: unknown }>();
  }
}

export function selects(sel: "*" | string[], name: string): boolean {
  return sel === "*" || sel.includes(name);
}

// Split an article's fields into { metadata, content } per the type config.
// "content" takes precedence: a field named in content never also appears in
// metadata (even when metadata is "*").
export function splitEntry(
  fields: Map<string, { source: "top" | "raw"; value: unknown }>,
  cfg: TypeConfig,
): { metadata: Record<string, unknown>; content: Record<string, unknown> } {
  const metadata: Record<string, unknown> = {};
  const content: Record<string, unknown> = {};
  const contentSel = cfg.content;
  for (const [name, { value }] of fields) {
    const isContent = selects(contentSel, name);
    if (isContent) {
      content[name] = value;
    } else if (selects(cfg.metadata, name)) {
      metadata[name] = value;
    }
  }
  return { metadata, content };
}

export interface CatalogueEntry {
  field: string;
  source: "top" | "raw";
  types: Set<string>;
  occurrences: number;
  sample: string;
  description: string;
}

export function jsType(v: unknown): string {
  if (v === null) return "null";
  if (Array.isArray(v)) return "list";
  return typeof v;
}

export function sampleOf(v: unknown): string {
  if (v === null || v === undefined) return "";
  let s: string;
  if (typeof v === "string") s = v;
  else if (Array.isArray(v)) s = JSON.stringify(v);
  else if (typeof v === "object") s = JSON.stringify(v);
  else s = String(v);
  s = s.replace(/\s+/g, " ").trim();
  return s.length > 200 ? s.slice(0, 200) + "…" : s;
}

export function updateCatalogue(
  cat: Map<string, CatalogueEntry>,
  fields: Map<string, { source: "top" | "raw"; value: unknown }>,
  descriptions: Record<string, string>,
): void {
  for (const [name, { source, value }] of fields) {
    let e = cat.get(name);
    if (!e) {
      e = {
        field: name,
        source,
        types: new Set(),
        occurrences: 0,
        sample: "",
        description: descriptions[name] ?? "",
      };
      cat.set(name, e);
    }
    e.occurrences++;
    e.types.add(jsType(value));
    // Keep the first non-empty sample we encounter.
    if (!e.sample) {
      const s = sampleOf(value);
      if (s) e.sample = s;
    }
  }
}

export function writeCatalogue(
  dir: string,
  typeKey: string,
  documentType: string,
  cat: Map<string, CatalogueEntry>,
  totalEntries: number,
  cfg: TypeConfig,
): Promise<void[]> {
  const rows = [...cat.values()]
    .map((e) => ({
      field: e.field,
      source: e.source,
      section: selects(cfg.content, e.field)
        ? "content"
        : (selects(cfg.metadata, e.field) ? "metadata" : "unselected"),
      types: [...e.types].sort(),
      occurrences: e.occurrences,
      coverage: totalEntries ? +(e.occurrences / totalEntries).toFixed(3) : 0,
      description: e.description,
      sample: e.sample,
    }))
    .sort((a, b) => a.field.localeCompare(b.field));

  const json = {
    typeKey,
    documentType,
    totalEntries,
    fieldCount: rows.length,
    note: "Every field returned by the API across the dumped entries. 'section' " +
      'reflects the current config. Replace metadata: "*" in the config with ' +
      "an explicit list of the field names you want to keep.",
    fields: rows,
  };

  // Human-readable companion.
  const md: string[] = [
    `# Field catalogue — ${documentType} (${typeKey})`,
    "",
    `Entries surveyed: ${totalEntries}  •  Fields seen: ${rows.length}`,
    "",
    "| field | source | section | type(s) | coverage | description | sample |",
    "|-------|--------|---------|---------|----------|-------------|--------|",
  ];
  const esc = (s: string) => s.replace(/\|/g, "\\|").replace(/\n/g, " ");
  for (const r of rows) {
    md.push(
      `| \`${r.field}\` | ${r.source} | ${r.section} | ${r.types.join(", ")} | ` +
        `${(r.coverage * 100).toFixed(0)}% | ${esc(r.description)} | ${esc(r.sample)} |`,
    );
  }
  md.push("");

  return Promise.all([
    Deno.writeTextFile(`${dir}/_catalogue.json`, JSON.stringify(json, null, 2)),
    Deno.writeTextFile(`${dir}/_catalogue.md`, md.join("\n")),
  ]);
}

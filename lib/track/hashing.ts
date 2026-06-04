// Hashing / canonicalization + article->record mapping for change tracking.
//
// EXTRACTED VERBATIM from track_articles.ts. The hashing scheme and the record
// shape MUST stay byte-identical so the existing outputs/articles.db remains
// valid across runs (content_hash / metadata_hash must reproduce exactly).

// ---------------------------------------------------------------------------
// Hashing / canonicalization
// ---------------------------------------------------------------------------
// Deterministic JSON: sort object keys recursively so logically-equal objects
// hash identically regardless of key order.
export function canonical(v: unknown): unknown {
  if (Array.isArray(v)) return v.map(canonical);
  if (v && typeof v === "object") {
    const out: Record<string, unknown> = {};
    for (const k of Object.keys(v as Record<string, unknown>).sort()) {
      out[k] = canonical((v as Record<string, unknown>)[k]);
    }
    return out;
  }
  return v;
}

export async function sha256(obj: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify(canonical(obj)));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------------------------------------------------------------------------
// Article -> tracked record
// ---------------------------------------------------------------------------
export interface Article {
  id?: string;
  documentType?: string;
  title?: string;
  link?: string;
  modifiedMs?: number | null;
  capturedAt?: string;
  metadata?: Record<string, unknown>;
  content?: Record<string, unknown>;
}

// content keys that are bookkeeping, not body — excluded from the content hash
// and the has-body test so a re-fetch timestamp never looks like a change.
export const VOLATILE_CONTENT_KEYS = new Set(["bodySource", "fetchedAt"]);

export function numMeta(meta: Record<string, unknown> | undefined, key: string): number | null {
  const v = meta?.[key];
  return typeof v === "number" ? v : null;
}

export function contentForHash(
  content: Record<string, unknown> | undefined,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(content ?? {})) {
    if (!VOLATILE_CONTENT_KEYS.has(k)) out[k] = v;
  }
  return out;
}

export function hasBody(content: Record<string, unknown> | undefined): boolean {
  for (const [k, v] of Object.entries(content ?? {})) {
    if (VOLATILE_CONTENT_KEYS.has(k) || k === "bodyError") continue;
    if (typeof v === "string" ? v.trim().length > 0 : v != null) return true;
  }
  return false;
}

export interface Record_ {
  id: string;
  document_type: string;
  title: string;
  link: string;
  created_ms: number | null;
  original_published_ms: number | null;
  updated_published_ms: number | null;
  modified_ms: number | null;
  captured_at: string;
  metadata_hash: string;
  content_hash: string;
  has_body: number;
  body_error: string | null;
}

export async function toRecord(a: Article): Promise<Record_> {
  return {
    id: a.id ?? "",
    document_type: a.documentType ?? "",
    title: a.title ?? "",
    link: a.link ?? "",
    created_ms: numMeta(a.metadata, "f5_created_date"),
    original_published_ms: numMeta(a.metadata, "f5_original_published_date"),
    updated_published_ms: numMeta(a.metadata, "f5_updated_published_date"),
    modified_ms: typeof a.modifiedMs === "number" ? a.modifiedMs : null,
    captured_at: a.capturedAt ?? "",
    metadata_hash: await sha256(a.metadata ?? {}),
    content_hash: await sha256(contentForHash(a.content)),
    has_body: hasBody(a.content) ? 1 : 0,
    body_error: typeof a.content?.bodyError === "string" ? a.content.bodyError : null,
  };
}

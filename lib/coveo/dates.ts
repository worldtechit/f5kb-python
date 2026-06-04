// Date helpers shared across Coveo queries. Moved verbatim from the original
// scripts (dump_articles.ts / fetch_recent_by_type.ts) — behavior unchanged.

// Coveo date filter format: YYYY/MM/DD@HH:MM:SS (UTC).
export function toCoveoDate(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => n.toString().padStart(2, "0");
  return [
    `${d.getUTCFullYear()}/${pad(d.getUTCMonth() + 1)}/${pad(d.getUTCDate())}`,
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`,
  ].join("@");
}

// Build an `aq` date-range fragment: `@date>=START @date<END` (either bound optional).
export function dateAq(startMs?: number, endMs?: number): string {
  const parts: string[] = [];
  if (startMs !== undefined) parts.push(`@date>=${toCoveoDate(startMs)}`);
  if (endMs !== undefined) parts.push(`@date<${toCoveoDate(endMs)}`);
  return parts.join(" ");
}

// Most specific available modification timestamp (ms) on a Coveo `raw` bag.
export function modMsOf(raw: Record<string, unknown> | undefined): number | undefined {
  return (raw?.f5_updated_published_date as number) ??
    (raw?.sflastmodifieddate as number) ??
    (raw?.date as number);
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// ms epoch -> "MMM D, YYYY" (UTC). Used by the flat fetch/recent exports.
export function formatDate(ms?: number | null): string {
  if (ms == null) return "";
  const d = new Date(ms);
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`;
}

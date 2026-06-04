// Filesystem + id helpers shared across subcommands. sanitizeName/idOf are moved
// verbatim from dump_articles.ts (behavior unchanged — same per-article filenames).

export function sanitizeName(s: string): string {
  return s.replace(/[^a-zA-Z0-9_-]/g, "_").replace(/_+/g, "_").replace(/^_|_$/g, "");
}

// Stable, human-friendly id for a per-article filename.
export function idOf(r: Record<string, unknown>): string {
  const raw = (r.raw as Record<string, unknown>) ?? {};
  const candidate = (raw.f5_kb_id as string) ||
    (raw.permanentid as string) ||
    (r.uniqueId as string) ||
    (r.title as string) ||
    "article";
  return sanitizeName(candidate).slice(0, 120);
}

export async function readJson<T = unknown>(path: string): Promise<T> {
  return JSON.parse(await Deno.readTextFile(path)) as T;
}

// Pretty-printed JSON with a trailing newline (matches existing output files).
export async function writeJson(path: string, data: unknown): Promise<void> {
  await Deno.writeTextFile(path, JSON.stringify(data, null, 2) + "\n");
}

// Yield each article JSON file in a type dir, skipping `_catalogue.json`/`_index.json`.
export async function* walkArticleFiles(typeDir: string): AsyncGenerator<string> {
  for await (const entry of Deno.readDir(typeDir)) {
    if (entry.isFile && entry.name.endsWith(".json") && !entry.name.startsWith("_")) {
      yield `${typeDir}/${entry.name}`;
    }
  }
}

export async function listTypeDirs(dumpDir: string): Promise<string[]> {
  const dirs: string[] = [];
  for await (const e of Deno.readDir(dumpDir)) {
    if (e.isDirectory) dirs.push(e.name);
  }
  dirs.sort();
  return dirs;
}

export async function exists(path: string): Promise<boolean> {
  try {
    await Deno.stat(path);
    return true;
  } catch {
    return false;
  }
}

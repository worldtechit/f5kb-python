// deno-lint-ignore-file no-explicit-any
// Next.js __NEXT_DATA__ extraction (docs.cloud.f5.com). Moved VERBATIM from
// enrich_bodies.ts. The swagger walker keeps the original loosely-typed `any`
// shapes (arbitrary OpenAPI JSON) to preserve byte-identical output.
//
// The body is embedded as JSON in the page, not (reliably) in the rendered DOM.

export function parseNextData(html: string): Record<string, unknown> | null {
  const m = html.match(
    /<script id="__NEXT_DATA__" type="application\/json">([\s\S]*?)<\/script>/,
  );
  if (!m) return null;
  try {
    return JSON.parse(m[1]) as Record<string, unknown>;
  } catch {
    return null;
  }
}

// next-mdx-remote keeps the authored MDX as /* ... */ comment blocks interleaved
// with the compiled JS. Recover them and join → the original markdown body.
export function mdxFromCompiledSource(compiledSource: string): string {
  const blocks = [...compiledSource.matchAll(/\/\*([\s\S]*?)\*\//g)]
    .map((b) => b[1].trim())
    .filter(Boolean)
    // Drop MDX import/export plumbing lines that aren't body content.
    .filter((b) => !/^(import|export)\s/.test(b));
  return blocks.join("\n\n").replace(/\n{3,}/g, "\n\n").trim();
}

// Render an OpenAPI/Swagger spec to a concise markdown body (API doc pages).
export function swaggerToMarkdown(sw: Record<string, any>): string {
  const out: string[] = [];
  const info = sw.info ?? {};
  if (info.title) out.push(`# ${info.title}`);
  if (info.description) out.push(info.description);
  const paths = sw.paths ?? {};
  for (const [path, ops] of Object.entries<Record<string, any>>(paths)) {
    for (const [method, op] of Object.entries<any>(ops)) {
      if (!["get", "post", "put", "delete", "patch"].includes(method)) continue;
      out.push(`## ${method.toUpperCase()} ${path}`);
      const summary = op.summary ?? op["x-displayname"];
      if (summary) out.push(`**${summary}**`);
      if (op.description) out.push(op.description);
    }
  }
  return out.join("\n\n").trim();
}

// Extract a body from docs.cloud.f5.com via __NEXT_DATA__. Returns "" if the
// JSON isn't present/usable so the caller can fall back to DOM scraping.
export function extractNextDataBody(html: string): string {
  const data = parseNextData(html);
  const pageProps = (data?.props as any)?.pageProps;
  const docData = pageProps?.docData;
  if (!docData) return "";
  if (typeof docData.compiledSource === "string") {
    return mdxFromCompiledSource(docData.compiledSource);
  }
  if (docData.swaggerFile) {
    return swaggerToMarkdown(docData.swaggerFile);
  }
  return "";
}

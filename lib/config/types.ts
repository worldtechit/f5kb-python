// Shared config types. `config.yaml` has three top-level sections:
//   types:              per-document-type field keep-lists (was dump_config.yaml)
//   field_descriptions: field-name -> description (was field_descriptions.yaml)
//   products:           read-only product snapshot (was supplemental_products.json)

export interface TypeConfig {
  documentType: string;
  metadata: "*" | string[];
  content: "*" | string[];
}

export interface ProductEntry {
  product: string;
  count: number;
  source: string;
  hiddenFromGlobalFacet?: boolean;
  discoveredViaTypes?: string[];
}

export interface AppConfig {
  types: Record<string, TypeConfig>;
  fieldDescriptions: Record<string, string>;
  products: { generatedAt?: string; entries: ProductEntry[] };
}

// Default content to [] and metadata to "*" (matches dump_articles.ts `normalize`).
export function normalizeType(c: Partial<TypeConfig>): TypeConfig {
  return {
    documentType: c.documentType ?? "",
    metadata: c.metadata ?? "*",
    content: c.content ?? [],
  };
}

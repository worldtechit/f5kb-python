// Loads the merged config.yaml into a typed AppConfig. A missing section yields
// a sensible empty default (e.g. no products: section -> { entries: [] }).

import { parse as parseYaml } from "@std/yaml";
import type { AppConfig } from "./types.ts";

interface RawConfig {
  types?: AppConfig["types"];
  field_descriptions?: Record<string, string>;
  products?: AppConfig["products"];
}

export async function loadConfig(path = "config.yaml"): Promise<AppConfig> {
  const doc = (parseYaml(await Deno.readTextFile(path)) as RawConfig | null) ?? {};
  return {
    types: doc.types ?? {},
    fieldDescriptions: doc.field_descriptions ?? {},
    products: doc.products ?? { entries: [] },
  };
}

// Optional override file for field descriptions (the deprecated --fields-doc flag).
// Accepts either { descriptions: {...} } (legacy field_descriptions.yaml) or a
// bare map. Returns {} if absent/unparseable.
export async function loadFieldDescriptionsFile(path: string): Promise<Record<string, string>> {
  try {
    const doc = parseYaml(await Deno.readTextFile(path)) as
      | { descriptions?: Record<string, string> }
      | Record<string, string>
      | null;
    if (!doc || typeof doc !== "object") return {};
    const maybe = (doc as { descriptions?: Record<string, string> }).descriptions;
    return (maybe && typeof maybe === "object" ? maybe : (doc as Record<string, string>)) ?? {};
  } catch {
    return {};
  }
}

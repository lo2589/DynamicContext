/**
 * dc/config.ts — reads the project's runtime.yaml and nothing else.
 *
 * The extension is config-driven the same way the Python runtime is: the
 * life_cycle table, context.roles, context.patch_end, recall.* and
 * compact.* all come from the same YAML the runtime reads. Search order:
 * --dc-config flag / DC_CONFIG env, then <cwd>/config/standard/runtime.yaml,
 * then built-in defaults (which add the pi-only toolResult element).
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { parse } from "yaml";

export interface PatchEndConfig {
  enabled: boolean;
  template: string;
}

export interface RecallConfig {
  type: string; // "none" | "grep"
  searchFields: string[];
  topK: number;
  minScore: number;
  trigger: string; // "always" | "never" | "manual" | "pattern"
  triggerParams: Record<string, unknown>;
}

export interface CompactConfig {
  fields: string[];
  overloadThresholdBytes: number;
  prompt: string;
  intervalTurns: number;
  keepRecentTurns: number;
  retention: string; // "latest_only" | "keep_all" | "last_k" | "equidistant"
  retentionParams: Record<string, unknown>;
}

export interface DcConfig {
  lifeCycle: Record<string, [unknown, unknown]>;
  roles: Record<string, string>;
  patchEnd: PatchEndConfig;
  recall: RecallConfig;
  compact: CompactConfig | null;
  sourcePath: string | null;
}

export const DEFAULT_LIFE_CYCLE: Record<string, [unknown, unknown]> = {
  user: ["born", null],
  assistant: ["born", null],
  // The one element the Python runtime never had: pi's tool results. Born
  // with a short fuse so tool noise retires itself unless configured away.
  toolResult: ["born", "born+5"],
  // Recall evidence is born and dies within the turn that asked for it.
  recall: ["born", "born"],
};

function readLifeCycle(raw: unknown): Record<string, [unknown, unknown]> {
  const table: Record<string, [unknown, unknown]> = { ...DEFAULT_LIFE_CYCLE };
  if (raw === null || raw === undefined) return table;
  if (typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("life_cycle 必须是 mapping");
  }
  for (const [element, bound] of Object.entries(raw as Record<string, unknown>)) {
    if (!Array.isArray(bound) || bound.length !== 2) {
      throw new Error(`life_cycle.${element} 必须是 [start, end]`);
    }
    table[element] = [bound[0], bound[1]];
  }
  return table;
}

function readPatchEnd(raw: unknown): PatchEndConfig {
  if (raw === undefined || raw === null || raw === false) return { enabled: false, template: "" };
  if (raw === true) {
    throw new Error("context.patch_end 为 true 时必须写成 mapping 并声明 template");
  }
  if (typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("context.patch_end必须是bool或mapping");
  }
  const section = raw as Record<string, unknown>;
  const enabled = section.enabled !== false;
  const template = typeof section.template === "string" ? section.template.trim() : "";
  if (enabled && !template) {
    throw new Error("启用 context.patch_end 时必须声明 template：结束语是内容，属于配置");
  }
  return { enabled, template };
}

function readRecall(raw: unknown): RecallConfig {
  const section = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>;
  const type = typeof section.type === "string" ? section.type : "none";
  const trigger = section.trigger;
  let triggerName = "always";
  let triggerParams: Record<string, unknown> = {};
  if (typeof trigger === "string") triggerName = trigger;
  else if (typeof trigger === "object" && trigger !== null) {
    const t = trigger as Record<string, unknown>;
    triggerName = typeof t.type === "string" ? t.type : "always";
    const { type: _drop, ...rest } = t;
    triggerParams = rest;
  }
  const searchFields = Array.isArray(section.search_fields)
    ? section.search_fields.map(String).filter(Boolean)
    : [];
  if (type !== "none" && searchFields.length === 0) {
    throw new Error(
      "启用 recall 时必须声明 recall.search_fields：回忆只搜已声明的证据字段",
    );
  }
  return {
    type,
    searchFields,
    topK: section.top_k != null ? Number(section.top_k) : 3,
    minScore: section.min_score != null ? Number(section.min_score) : 1,
    trigger: triggerName,
    triggerParams,
  };
}

function readCompact(raw: unknown): CompactConfig | null {
  // `compact: none` (or an absent section) turns the whole mechanism off.
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const section = raw as Record<string, unknown>;
  const compressors = (typeof section.compressors === "object" && section.compressors !== null
    ? section.compressors
    : {}) as Record<string, unknown>;
  const summary = (typeof compressors.summary === "object" && compressors.summary !== null
    ? compressors.summary
    : {}) as Record<string, unknown>;
  const prompt = typeof summary.prompt === "string" ? summary.prompt : "";
  if (!prompt) {
    throw new Error(
      "启用 compact 时必须声明 compact.compressors.summary.prompt：提示词是内容，属于配置",
    );
  }
  const fields = Array.isArray(section.fields) ? section.fields.map(String).filter(Boolean) : [];
  if (fields.length === 0) {
    throw new Error("启用 compact 时必须声明 compact.fields：哪些元素可被折叠是配置");
  }
  const periodic = (typeof summary.periodic === "object" && summary.periodic !== null
    ? summary.periodic
    : {}) as Record<string, unknown>;
  const retention = summary.retention;
  let retentionName = "latest_only";
  let retentionParams: Record<string, unknown> = {};
  if (typeof retention === "string") retentionName = retention;
  else if (typeof retention === "object" && retention !== null) {
    const r = retention as Record<string, unknown>;
    retentionName = typeof r.type === "string" ? r.type : "latest_only";
    const { type: _drop, ...rest } = r;
    retentionParams = rest;
  }
  return {
    fields,
    overloadThresholdBytes:
      section.overload_threshold_bytes != null ? Number(section.overload_threshold_bytes) : 32000,
    prompt,
    intervalTurns: periodic.interval_turns != null ? Number(periodic.interval_turns) : 20,
    keepRecentTurns: periodic.keep_recent_turns != null ? Number(periodic.keep_recent_turns) : 10,
    retention: retentionName,
    retentionParams,
  };
}

export function defaultConfig(): DcConfig {
  return {
    lifeCycle: { ...DEFAULT_LIFE_CYCLE },
    roles: {},
    patchEnd: { enabled: false, template: "" },
    recall: {
      type: "none",
      searchFields: [],
      topK: 3,
      minScore: 1,
      trigger: "always",
      triggerParams: {},
    },
    compact: null,
    sourcePath: null,
  };
}

export function loadDcConfig(cwd: string, explicitPath?: string): DcConfig {
  const candidates = [
    explicitPath,
    process.env.DC_CONFIG,
    join(cwd, "config", "standard", "runtime.yaml"),
  ].filter((p): p is string => typeof p === "string" && p.length > 0);
  const sourcePath = candidates.find((p) => existsSync(p)) ?? null;
  const cfg = defaultConfig();
  if (!sourcePath) return cfg;
  const doc = (parse(readFileSync(sourcePath, "utf8")) ?? {}) as Record<string, unknown>;
  cfg.sourcePath = sourcePath;
  cfg.lifeCycle = readLifeCycle(doc.life_cycle);
  const context = (typeof doc.context === "object" && doc.context !== null
    ? doc.context
    : {}) as Record<string, unknown>;
  if (typeof context.roles === "object" && context.roles !== null) {
    for (const [element, role] of Object.entries(context.roles as Record<string, unknown>)) {
      cfg.roles[element] = String(role);
    }
  }
  cfg.patchEnd = readPatchEnd(context.patch_end);
  cfg.recall = readRecall(doc.recall);
  cfg.compact = readCompact(doc.compact);
  return cfg;
}

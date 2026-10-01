/**
 * dc/compact.ts — port of compact/processor.py (triggers), summery.py
 * (compressor + retention), and the decision half of compact/pipeline.py.
 *
 * Two policy axes, both config:
 *
 *   the compressor — how a window of raw turns becomes one summary
 *                    (one model call, prompt from compact.compressors.summary.prompt)
 *   retention      — which already-written summaries stay visible afterwards
 *                    (never recomputes anything; only moves visibility)
 *
 * A summary is computed once, for the stretch it covers, and never fed back
 * through the model: the transcript window contains raw turns only, so no
 * summary is ever a summary of summaries.
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const SUMMARY_SUFFIX = "_summary";

// ---------------------------------------------------------------------------
// Triggers
// ---------------------------------------------------------------------------

/** Fixed-interval trigger: every `step` turns after `firstRunTurn`. */
export function isDuePeriodic(
  currentTurn: number,
  lastRunTurn: number | null,
  step: number,
  firstRunTurn: number,
): boolean {
  if (lastRunTurn === null) return currentTurn >= firstRunTurn;
  return currentTurn >= lastRunTurn + step;
}

/** Resource trigger: the assembled context has grown past a byte budget. */
export function isDueOverload(contextSizeBytes: number, thresholdBytes: number): boolean {
  if (thresholdBytes <= 0) throw new Error("threshold_bytes 必须是正数");
  return contextSizeBytes > thresholdBytes;
}

/** UTF-8 bytes of the context as it would go over the wire. */
export function contextByteSize(messages: unknown): number {
  return Buffer.byteLength(JSON.stringify(messages), "utf8");
}

// ---------------------------------------------------------------------------
// Retention — which already-written summaries stay visible.
// ---------------------------------------------------------------------------

export type RetentionFn = (anchors: number[], currentTurn: number, params: Record<string, unknown>) => Set<number>;

export const retentionStrategies: Record<string, RetentionFn> = {
  /** Only the newest summary stays visible; each one supersedes the last. */
  latest_only: (anchors) => (anchors.length ? new Set([Math.max(...anchors)]) : new Set()),
  /** Every summary ever written stays visible. */
  keep_all: (anchors) => new Set(anchors),
  /** The k most recent summaries stay visible. */
  last_k: (anchors, _turn, params) => {
    const k = Number(params.k ?? 3);
    if (!Number.isInteger(k) || k < 1) throw new Error("retain.last_k 的 k 必须是正整数");
    return new Set([...anchors].sort((a, b) => a - b).slice(-k));
  },
  /**
   * Keep k summaries spread evenly over the compressed span — distant
   * history thins out instead of vanishing. Ties prefer the later anchor;
   * the newest summary is always kept.
   */
  equidistant: (anchors, currentTurn, params) => {
    const k = Number(params.k ?? 3);
    if (!Number.isInteger(k) || k < 1) throw new Error("retain.equidistant 的 k 必须是正整数");
    if (anchors.length === 0) return new Set();
    const kept = new Set<number>();
    for (let index = 1; index <= k; index++) {
      const target = (currentTurn * index) / k;
      let best = anchors[0];
      for (const anchor of anchors) {
        const d = Math.abs(anchor - target);
        const bestD = Math.abs(best - target);
        if (d < bestD || (d === bestD && anchor > best)) best = anchor;
      }
      kept.add(best);
    }
    kept.add(Math.max(...anchors));
    return kept;
  },
};

// ---------------------------------------------------------------------------
// Window + transcript
// ---------------------------------------------------------------------------

export function summaryElementName(anchorTurn: number): string {
  return `${anchorTurn}${SUMMARY_SUFFIX}`;
}

/** Render a window of raw turns into plain text for the prompt. */
export function windowTranscript(
  turns: { turn: number; element: string; text: string }[],
  fields: string[],
): string {
  const lines: string[] = [];
  for (const { turn, element, text } of turns) {
    if (!fields.includes(element) || !text) continue;
    lines.push(`[第${turn}轮 ${element}] ${text}`);
  }
  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// The compressor: one model call through the pi-configured provider.
// ---------------------------------------------------------------------------

interface ProviderSpec {
  baseUrl: string;
  apiKey: string;
  model: string;
}

/**
 * Read the provider the pi session is actually using, from pi's own
 * models.json — the summary call speaks OpenAI-compatible chat completions,
 * which is what pi's ollama/local providers already expose.
 */
export function resolveSummaryProvider(preferredModel?: string): ProviderSpec {
  const path = process.env.DC_MODELS_JSON ?? join(homedir(), ".pi", "agent", "models.json");
  const doc = JSON.parse(readFileSync(path, "utf8")) as {
    providers?: Record<string, { baseUrl?: string; apiKey?: string; models?: { id: string }[] }>;
  };
  for (const provider of Object.values(doc.providers ?? {})) {
    if (!provider.baseUrl || !provider.models?.length) continue;
    const model =
      (preferredModel && provider.models.find((m) => preferredModel.endsWith(m.id))?.id) ??
      provider.models[0].id;
    return { baseUrl: provider.baseUrl.replace(/\/+$/, ""), apiKey: provider.apiKey ?? "none", model };
  }
  throw new Error("models.json 里没有可用 provider，无法生成压缩摘要");
}

/** Call the model once to fold a raw-turn window into a short summary. */
export async function computeSummary(
  transcript: string,
  promptTemplate: string,
  preferredModel?: string,
): Promise<string> {
  const provider = resolveSummaryProvider(preferredModel);
  const prompt = promptTemplate.replace("{content}", transcript);
  const response = await fetch(`${provider.baseUrl}/chat/completions`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${provider.apiKey}`,
    },
    body: JSON.stringify({
      model: provider.model,
      messages: [{ role: "user", content: prompt }],
      stream: false,
    }),
  });
  if (!response.ok) {
    throw new Error(`摘要模型调用失败: HTTP ${response.status}`);
  }
  const data = (await response.json()) as {
    choices?: { message?: { content?: string } }[];
  };
  return String(data.choices?.[0]?.message?.content ?? "").trim();
}

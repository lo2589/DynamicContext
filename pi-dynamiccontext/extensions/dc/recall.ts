/**
 * dc/recall.ts — port of recall/grep_recall.py + recall/trigger.py.
 *
 * Recall is an act of connecting, not a cache lookup: it searches the whole
 * declared evidence range, including slots that are still active — being
 * present in the context is not the same as being recognized as relevant
 * to what was just asked. What it may search is declared (search_fields);
 * how far back is the whole ledger. No embeddings, no external store.
 *
 * `recall.type` picks how to look things up; `recall.trigger` picks when to
 * bother at all — an axis of its own.
 */

const ASCII_WORD = /[a-z0-9]+/g;
// eslint-disable-next-line no-misleading-character-class
const CJK_CHAR = /[㐀-鿿]/g;

/**
 * Tokens for overlap scoring, in a way that works for both scripts: ASCII
 * gets words; CJK has no word boundaries, so it gets character bigrams —
 * enough to make 花生 match 花生酱 without pulling in every sentence that
 * merely shares one common character.
 */
export function lexicalTokens(text: string): Set<string> {
  const source = String(text ?? "").toLowerCase();
  const tokens = new Set<string>(source.match(ASCII_WORD) ?? []);
  const cjkRun = (source.match(CJK_CHAR) ?? []).join("");
  for (let i = 0; i + 1 < cjkRun.length; i++) tokens.add(cjkRun.slice(i, i + 2));
  return tokens;
}

export interface RecallHit {
  turn: number;
  element: string;
  text: string;
  score: number;
  retired: boolean;
}

export interface RecallSlotView {
  turn: number;
  element: string;
  text: string;
  retired: boolean;
}

/**
 * Pull back the best-matching slots for this turn's query. The current turn
 * is already in context verbatim, so it is never a hit. Highest score
 * first; ties break toward recency.
 */
export function grepRecall(
  slots: RecallSlotView[],
  currentTurn: number,
  query: string,
  opts: { searchFields: string[]; topK: number; minScore: number },
): RecallHit[] {
  if (opts.searchFields.length === 0 || opts.topK < 1) return [];
  const searchable = new Set(opts.searchFields);
  const queryTokens = lexicalTokens(query);
  if (queryTokens.size === 0) return [];
  const scored: RecallHit[] = [];
  for (const slot of slots) {
    if (slot.turn >= currentTurn) continue;
    if (!searchable.has(slot.element)) continue;
    const score = intersectionSize(queryTokens, lexicalTokens(slot.text));
    if (score >= opts.minScore) scored.push({ ...slot, score });
  }
  scored.sort((a, b) => b.score - a.score || b.turn - a.turn);
  return scored.slice(0, opts.topK);
}

function intersectionSize(a: Set<string>, b: Set<string>): number {
  let n = 0;
  for (const item of a) if (b.has(item)) n++;
  return n;
}

/** Render hits the way the runtime writes them into the ledger. */
export function formatRecallLines(hits: RecallHit[]): string[] {
  return hits.map(
    (h) => `${h.retired ? "[retired] " : ""}第${h.turn}轮 ${h.element}：${h.text}`,
  );
}

// ---------------------------------------------------------------------------
// Triggers — when recall fires, separate from how it looks things up.
// ---------------------------------------------------------------------------

export type RecallTrigger = (query: string) => boolean;

export function makeTrigger(name: string, params: Record<string, unknown>): RecallTrigger {
  switch (name) {
    case "always":
      return () => true;
    case "never":
    case "manual":
      // Reserved for an explicit user command; never fires on its own.
      return () => false;
    case "pattern": {
      const pattern = typeof params.pattern === "string" ? params.pattern : "";
      if (!pattern) {
        throw new Error(
          "recall.trigger 选用 pattern 时必须声明 pattern：触发词表依赖语言和任务，属于配置",
        );
      }
      let regex: RegExp;
      try {
        regex = new RegExp(pattern);
      } catch (exc) {
        throw new Error(`recall.trigger.pattern 不是合法正则: ${exc}`);
      }
      return (query) => (query ? regex.test(query) : false);
    }
    default:
      throw new Error(`未知 recall.trigger: ${name}(可用: always, never, manual, pattern)`);
  }
}

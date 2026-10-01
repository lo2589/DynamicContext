/**
 * dc/patches.ts — port of patch/state_patch.py + the patch_end layer of
 * patch/config.py and runtime.patch_endings_for_turn.
 *
 * Two kinds of patches:
 *
 *   ContentPatch — "/goal 翻译文本 remain 5": a new content slot.
 *     remain  lands once, lives `turns` turns.
 *     roll    re-materializes as a fresh one-turn cell on every covered turn.
 *     refresh ends whatever is active on arrival (like roll) but the new
 *             cell never expires on its own (like remain, turns=INF).
 *
 *   RowPatch — "/dc-rule goal born born+5 force_refresh": a rule change.
 *     forward          old slots keep their promise, new rule for new slots.
 *     force_refresh    re-derive every existing slot's end from its born turn.
 *     reset_from_now   end every existing slot now, new rule for new slots.
 *     add_row          declare a new element; not a rule change at all.
 *
 * patch_end: when a patch element's last cell closes, one durable ending
 * message ("以上goal已经结束…") is born with remain=1, and the turns the
 * element governed fold away in place (goal collapse — the element's own
 * content stands in as the summary, no model call).
 */

import { OPEN, resolveBound, validateRemain, type EndTurn } from "./lifecycle.js";
import type { Ledger, SlotMeta } from "./ledger.js";

// ---------------------------------------------------------------------------
// ContentPatch
// ---------------------------------------------------------------------------

export type ContentPatchMode = "remain" | "roll" | "refresh";

export interface ContentPatch {
  creator: string;
  element: string;
  content: string;
  mode: ContentPatchMode;
  turns: number; // positive int or Infinity
  createdTurn: number | null;
}

const PATCH_INPUT_PATTERN =
  /^\/(?<element>\S+)\s+(?<content>.+)\s+(?<mode>remain|remian|roll|refresh)\s+(?<turns>[1-9]\d*|none)$/s;
const INLINE_PATCH_INPUT_PATTERN =
  /^(?:(?<user>.+?)\s+)?\/(?<element>\S+)\s+(?<content>.+)\s+(?<mode>remain|remian|roll|refresh)\s+(?<turns>[1-9]\d*|none)$/s;
const PATCH_FIRST_INPUT_PATTERN =
  /^\/(?<element>\S+)\s+(?<content>.+?)\s+(?<mode>remain|remian|roll|refresh)\s+(?<turns>[1-9]\d*|none)(?:\s+(?<user>.+))?$/s;

function patchFromMatch(match: RegExpMatchArray, creator: string): ContentPatch {
  const groups = match.groups!;
  const mode = (groups.mode === "remian" ? "remain" : groups.mode) as ContentPatchMode;
  const turns = groups.turns === "none" ? Infinity : Number(groups.turns);
  return createContentPatch({
    creator,
    element: groups.element,
    content: groups.content,
    mode,
    turns,
  });
}

export function createContentPatch(opts: {
  creator: string;
  element: string;
  content: string;
  mode: ContentPatchMode;
  turns: number;
  createdTurn?: number | null;
}): ContentPatch {
  const creator = opts.creator.trim();
  const element = opts.element.trim().replace(/^\/+/, "");
  const content = opts.content.trim();
  if (!creator) throw new Error("patch creator不能为空");
  if (!element) throw new Error("patch element不能为空");
  if (!content) throw new Error("patch content不能为空");
  try {
    validateRemain(opts.turns);
  } catch {
    throw new Error("patch turns必须是正整数或none(无限期)");
  }
  if (
    opts.createdTurn !== undefined &&
    opts.createdTurn !== null &&
    (!Number.isInteger(opts.createdTurn) || opts.createdTurn < 1)
  ) {
    throw new Error("patch created_turn必须是正整数");
  }
  return {
    creator,
    element,
    content,
    mode: opts.mode,
    turns: opts.turns,
    createdTurn: opts.createdTurn ?? null,
  };
}

/** Parse "/goal xxx remain 5" occupying the whole line; null when not a patch. */
export function parsePatchInput(userInput: string, creator = "user"): ContentPatch | null {
  const line = userInput.trim();
  if (!line.startsWith("/")) return null;
  const match = PATCH_INPUT_PATTERN.exec(line);
  if (!match) {
    throw new Error(
      "patch格式必须是 /元素 内容 remain|roll 正整数，或 /元素 内容 refresh none（内容在模式前面，不是后面）",
    );
  }
  return patchFromMatch(match, creator);
}

/**
 * Extract one optional patch from a user input line, wherever it sits.
 * Returns [remainingUserText, patch].
 */
export function extractPatchInput(
  userInput: string,
  creator = "user",
): [string | null, ContentPatch | null] {
  const line = userInput.trim();
  let match = INLINE_PATCH_INPUT_PATTERN.exec(line) ?? PATCH_FIRST_INPUT_PATTERN.exec(line);
  if (!match) {
    if (line.startsWith("/")) return [null, parsePatchInput(line, creator)];
    return [line, null];
  }
  const user = match.groups!.user;
  return [user && user.trim() ? user.trim() : null, patchFromMatch(match, creator)];
}

/** Materialize one patch occurrence for a concrete turn (roll re-fires). */
export function contentPatchForTurn(patch: ContentPatch, turn: number): ContentPatch | null {
  if (patch.createdTurn === null) throw new Error("ContentPatch缺少created_turn");
  const born = patch.createdTurn;
  if (patch.mode === "remain" || patch.mode === "refresh") {
    return turn === born ? patch : null;
  }
  if (born <= turn && turn < born + patch.turns) {
    // A rolling value is a new one-turn cell on every covered turn.
    return { ...patch, createdTurn: turn, turns: 1 };
  }
  return null;
}

/** Whether this source patch still has an occurrence after `turn`. */
export function contentPatchPendingAfter(patch: ContentPatch, turn: number): boolean {
  if (patch.createdTurn === null) throw new Error("ContentPatch缺少created_turn");
  const lastTurn =
    patch.mode === "remain" || patch.mode === "refresh"
      ? patch.createdTurn
      : patch.createdTurn + patch.turns - 1;
  return turn < lastTurn;
}

/**
 * Ordered patch queue. A newer instruction for the same not-yet-happened
 * slot replaces the older one ("I changed my mind"), never piles up.
 */
export class PatchQueue {
  queued: ContentPatch[] = [];
  sources: ContentPatch[] = [];

  queue(patch: ContentPatch): void {
    if (patch.createdTurn === null) throw new Error("ContentPatch缺少created_turn");
    const notSuperseded = (item: ContentPatch) =>
      !(item.element === patch.element && item.createdTurn === patch.createdTurn);
    this.queued = this.queued.filter(notSuperseded);
    this.sources = this.sources.filter(notSuperseded);
    this.queued.push(patch);
    this.queued.sort((a, b) => (a.createdTurn ?? 0) - (b.createdTurn ?? 0));
    this.sources.push(patch);
    this.sources.sort((a, b) => (a.createdTurn ?? 0) - (b.createdTurn ?? 0));
  }

  /** Occurrences due at `turn`; for a shared element the newest source wins. */
  forTurn(turn: number): ContentPatch[] {
    const latest = new Map<string, ContentPatch>();
    for (const item of this.queued) {
      const occurrence = contentPatchForTurn(item, turn);
      if (occurrence) latest.set(occurrence.element, occurrence);
    }
    return [...latest.values()];
  }

  consume(turn: number): void {
    this.queued = this.queued.filter((item) => contentPatchPendingAfter(item, turn));
  }
}

// ---------------------------------------------------------------------------
// patch_end endings
// ---------------------------------------------------------------------------

function rangeEnd(slot: SlotMeta): EndTurn {
  const ends: number[] = [];
  for (const seg of slot.ranges) {
    if (seg.end === OPEN) return OPEN;
    ends.push(seg.end);
  }
  return ends.length ? Math.max(...ends) : OPEN;
}

function sourceOwnsOccurrence(source: ContentPatch, turn: number): boolean {
  if (source.createdTurn === null) return false;
  if (source.mode === "remain" || source.mode === "refresh") {
    return turn === source.createdTurn;
  }
  return source.createdTurn <= turn && turn < source.createdTurn + source.turns;
}

/**
 * Build one durable ending per element when its last cell closes — fired on
 * exactly the turn after the close (end == turn - 1), never regenerated
 * forever, and never while another occurrence of the element is still up.
 */
export function patchEndingsForTurn(
  turn: number,
  ledger: Ledger,
  sources: ContentPatch[],
  template: string,
): Map<string, string> {
  const endings = new Map<string, string>();
  if (!template) return endings;
  const candidates = new Map<string, { end: number; source: ContentPatch }[]>();
  const activeElements = new Set<string>();
  for (const source of sources) {
    for (const [id, slot] of ledger.entriesOf(source.element)) {
      if (!sourceOwnsOccurrence(source, slot.bornTurn)) continue;
      if (ledger.visibleAt(id, turn)) {
        activeElements.add(source.element);
        continue;
      }
      const end = rangeEnd(slot);
      if (end !== OPEN && end === turn - 1) {
        const list = candidates.get(source.element) ?? [];
        list.push({ end, source });
        candidates.set(source.element, list);
      }
    }
  }
  for (const [element, values] of candidates) {
    if (activeElements.has(element)) continue;
    const endingElement = `${element}_end`;
    const endingActive = ledger
      .entriesOf(endingElement)
      .some(([id]) => ledger.visibleAt(id, turn));
    if (endingActive) continue;
    const latestEnd = Math.max(...values.map((v) => v.end));
    const messages = values
      .filter((v) => v.end === latestEnd)
      .map((v) =>
        template
          .replace("{element}", v.source.element)
          .replace("{content}", v.source.content),
      );
    endings.set(element, [...new Set(messages)].join("\n"));
  }
  return endings;
}

// ---------------------------------------------------------------------------
// RowPatch
// ---------------------------------------------------------------------------

export type RowPatchMode = "forward" | "force_refresh" | "reset_from_now" | "add_row";

/**
 * Apply a rule change to the ledger. Returns a human-readable summary.
 * `bound` is the [start, end] pair the element carries from now on.
 */
export function applyRowPatch(
  ledger: Ledger,
  element: string,
  bound: [unknown, unknown],
  mode: RowPatchMode,
  turn: number,
): string {
  if (mode === "add_row") {
    if (ledger.rules[element]) throw new Error(`row ${element} 已存在`);
    ledger.rules[element] = bound;
    return `${element}: 新增槽位类型 [${String(bound[0])}, ${String(bound[1])}]`;
  }
  if (!ledger.rules[element]) throw new Error(`row_id=${element} 不存在（/dc-addrow 可以新增）`);
  ledger.rules[element] = bound;
  if (mode === "forward") {
    return `${element}: [${String(bound[0])}, ${String(bound[1])}] (forward — 已有槽位保持出生时的承诺)`;
  }
  const affected = ledger.entriesOf(element);
  if (mode === "reset_from_now") {
    let n = 0;
    for (const [id] of affected) if (ledger.endCell(id, turn)) n++;
    return `${element}: 本轮结束 ${n} 个旧槽位，新规则只管之后的新槽位`;
  }
  // force_refresh: re-derive each slot's end from its own born turn.
  let recalculated = 0;
  let closed = 0;
  for (const [id, slot] of affected) {
    slot.rule = bound;
    const newEnd = resolveBound(bound[1], slot.bornTurn);
    for (const seg of slot.ranges) {
      if (seg.end === OPEN || seg.end >= turn) {
        seg.end = newEnd === OPEN ? OPEN : newEnd;
        recalculated++;
      }
    }
    if (newEnd !== OPEN && newEnd < turn) {
      if (ledger.endCell(id, turn)) closed++;
    }
  }
  return `${element}: 按新规则重算 ${recalculated} 段区间（${closed} 个槽位立即结束）`;
}

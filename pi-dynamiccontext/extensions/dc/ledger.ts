/**
 * dc/ledger.ts — port of manager/engine.py's FixedTableLifecycle.
 *
 * The ledger is a set of slots keyed by pi entry id. Each slot belongs to
 * the turn it was written at and speaks over one or more closed-interval
 * range segments [[start, end], ...] (end null = open). Visibility is a
 * pure function of ranges and turn; retirement in pi is expressed by
 * diffing that function against the context_edit entries already on the
 * branch, so raw history is never touched and a crash mid-turn cannot
 * desync the ledger — the branch itself is the other source of truth.
 */

import { OPEN, configuredRange, resolveBound, validateRemain, type EndTurn } from "./lifecycle.js";

export interface RangeSeg {
  start: number;
  end: EndTurn; // inclusive; null = open
}

export interface SlotMeta {
  element: string;
  /** The turn the slot belongs to (for a summary: the turn it covers up to). */
  bornTurn: number;
  ranges: RangeSeg[];
  /** The life_cycle [start, end] pair the slot was born under (forward semantics). */
  rule: [unknown, unknown];
  /** Exclusive turn from which the slot is dead regardless of ranges (cancelpin). */
  dead: number | null;
  /** Summaries record the span they compacted, reaching back through superseded ones. */
  compactRange?: [number, number];
}

export const DC_PREFIX = "dc:";

/** Which ledger element a pi entry carries, or null when it stays pi-managed. */
export function elementOfEntry(entry: any): string | null {
  if (entry?.type === "message") {
    const role = entry.message?.role;
    if (role === "user" || role === "assistant" || role === "toolResult") return role;
    return null; // system messages stay pi-managed
  }
  if (entry?.type === "custom_message") {
    const customType = String(entry.customType ?? "");
    if (customType.startsWith(DC_PREFIX)) return customType.slice(DC_PREFIX.length);
    return "custom_message";
  }
  return null;
}

export function textOfEntry(entry: any): string {
  const content = entry?.message?.content ?? entry?.content ?? "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .filter((b: any) => b?.type === "text")
      .map((b: any) => String(b.text ?? ""))
      .join("\n");
  }
  return "";
}

export class Ledger {
  rules: Record<string, [unknown, unknown]>;
  slots = new Map<string, SlotMeta>();
  /** Visibility overrides that outvote ranges: keeps force open, dead forces closed. */
  keeps = new Set<string>();

  constructor(rules: Record<string, [unknown, unknown]>) {
    this.rules = { ...rules };
  }

  ruleFor(element: string): [unknown, unknown] {
    return this.rules[element] ?? ["born", null];
  }

  has(id: string): boolean {
    return this.slots.has(id);
  }

  get(id: string): SlotMeta | undefined {
    return this.slots.get(id);
  }

  /**
   * Register a slot born at `turn` under the element's current rule.
   * `remain` overrides the rule with an explicit lifespan (ContentPatch);
   * `visibleFrom` separates where a slot sits from when it starts speaking.
   */
  addSlot(
    id: string,
    element: string,
    turn: number,
    opts: { remain?: number; visibleFrom?: number; compactRange?: [number, number] } = {},
  ): SlotMeta {
    if (this.slots.has(id)) throw new Error(`槽位已存在，不能覆盖: ${id}`);
    let range: { start: number; end: EndTurn };
    let rule: [unknown, unknown];
    if (opts.remain !== undefined) {
      validateRemain(opts.remain);
      const start = opts.visibleFrom ?? turn;
      rule = [start, opts.remain === Infinity ? null : start + opts.remain - 1];
      range = configuredRange(rule, turn, opts.visibleFrom);
    } else {
      rule = this.ruleFor(element);
      range = configuredRange(rule, turn, opts.visibleFrom);
    }
    const slot: SlotMeta = {
      element,
      bornTurn: turn,
      ranges: [{ start: range.start, end: range.end }],
      rule,
      dead: null,
      ...(opts.compactRange ? { compactRange: opts.compactRange } : {}),
    };
    this.slots.set(id, slot);
    return slot;
  }

  /** The closed-interval end the slot's birth rule declares, or OPEN. */
  declaredEnd(slot: SlotMeta): EndTurn {
    return resolveBound(slot.rule[1], slot.bornTurn);
  }

  /** On stage at `turn`? Ranges and dead say where; keeps outvote both. */
  visibleAt(id: string, turn: number): boolean {
    if (this.keeps.has(id)) return true;
    const slot = this.slots.get(id);
    if (!slot) return false;
    if (slot.dead !== null && turn >= slot.dead) return false;
    return slot.ranges.some(
      (seg) => seg.start <= turn && (seg.end === OPEN || turn <= seg.end),
    );
  }

  /** Close the covering segment of exactly one slot at `turn`. */
  endCell(id: string, turn: number): boolean {
    const slot = this.slots.get(id);
    if (!slot) return false;
    let closed = false;
    for (const seg of slot.ranges) {
      if (seg.start <= turn && (seg.end === OPEN || seg.end >= turn)) {
        seg.end = turn - 1;
        closed = true;
      }
    }
    return closed;
  }

  /** Close every currently visible slot of an element (roll/refresh replace). */
  endActiveElement(element: string, turn: number): string[] {
    const closed: string[] = [];
    for (const [id, slot] of this.slots) {
      if (slot.element !== element) continue;
      if (this.visibleAt(id, turn) && this.endCell(id, turn)) closed.push(id);
    }
    return closed;
  }

  /**
   * Make an invisible slot visible again by appending a fresh segment
   * starting at `turn`. Coming back does not hand a slot a longer life
   * than its birth rule declared.
   */
  reopenCell(id: string, turn: number): boolean {
    const slot = this.slots.get(id);
    if (!slot) throw new Error(`槽位不存在，无法重新捞起: ${id}`);
    if (this.visibleAt(id, turn)) return false;
    const end = this.declaredEnd(slot);
    if (end !== OPEN && end < turn) {
      throw new Error(
        `life_cycle.${slot.element} 声明的有效期已在第 ${end} 轮结束，无法在第 ${turn} 轮捞起`,
      );
    }
    slot.ranges.push({ start: turn, end });
    return true;
  }

  /** A slot's element row: every slot id carrying that element name. */
  entriesOf(element: string): [string, SlotMeta][] {
    return [...this.slots.entries()].filter(([, s]) => s.element === element);
  }
}

/**
 * dc/lifecycle.ts — port of code/lifecycle/rules.py + span.py.
 *
 * The lifespan vocabulary: a declaration is [startBound, endBound] per
 * element, every bound resolves to a closed-interval turn number or OPEN
 * (null). "born" means the slot's own birth turn wherever it is written;
 * "born+n" is punctuation the reader splits off; a bare number is the turn
 * it says; null/permanent means no end. Dynamic rules (until_*) resolve to
 * OPEN at birth and get their real end written back by whatever detects
 * the event (cancelpin, a newer patch).
 *
 * cycle/labelled are deliberately not ported: they answer from the eval
 * dataset's per-turn labels (linked_trap/turn_type), and a pi session has
 * no dataset to ask.
 */

/** No end in sight — the open right edge of a range. */
export const OPEN: null = null;

export type EndTurn = number | null;

/** A lifespan is a positive whole number of turns, or Infinity. */
export function validateRemain(remain: number): void {
  if (typeof remain !== "number" || Number.isNaN(remain)) {
    throw new Error("remain 必须是正整数或 INF");
  }
  if (remain === Infinity) return;
  if (!Number.isInteger(remain) || remain < 1) {
    throw new Error("remain 必须是正整数或 INF");
  }
}

/**
 * resolveBound — port of rules.resolve_bound.
 *
 * Returns the inclusive turn the bound names, or OPEN. Throws on anything
 * a declaration may not write, naming what was available.
 */
export function resolveBound(bound: unknown, bornTurn: number): EndTurn {
  if (bound === null || bound === undefined) return OPEN; // permanent
  if (typeof bound === "boolean") throw new Error("life_cycle 边界不能是 bool");
  if (typeof bound === "number") {
    if (!Number.isInteger(bound)) throw new Error("life_cycle 边界必须是整数轮次");
    return bound;
  }
  if (typeof bound !== "string") throw new Error(`life_cycle 边界无法识别: ${String(bound)}`);
  const name = bound.trim();
  if (!name) throw new Error("life_cycle 边界不能是空字符串");
  if (name.includes("+")) {
    const [base, , argument] = [name.slice(0, name.indexOf("+")), "+", name.slice(name.indexOf("+") + 1)];
    if (base.trim() !== "born") throw new Error(`未知生命周期规则: ${name}(可用: born, born+n, permanent, until_cancelled, until_goal_end)`);
    const n = Number(argument);
    if (!Number.isInteger(n) || n < 0) throw new Error("born+n 的 n 必须是非负整数");
    return bornTurn + n;
  }
  switch (name) {
    case "born":
      return bornTurn;
    case "permanent":
      return OPEN;
    case "until_cancelled":
    case "until_goal_end":
      return OPEN; // closed later by the event that owns it
    default:
      throw new Error(`未知生命周期规则: ${name}(可用: born, born+n, permanent, until_cancelled, until_goal_end)`);
  }
}

/**
 * configuredRange — port of engine._configured_range. A slot sits at the
 * turn it belongs to; when it starts speaking can be later (visibleFrom —
 * a summary belongs to the turn it covers but speaks from the turn the
 * compaction ran). End is the closed-interval last turn still shown, so
 * [born, born] means visible for exactly the turn it is born in.
 */
export function configuredRange(
  rule: [unknown, unknown],
  turn: number,
  visibleFrom?: number,
): { start: number; end: EndTurn } {
  const [rawStart, rawEnd] = rule;
  const start0 = resolveBound(rawStart, turn);
  const end = resolveBound(rawEnd, turn);
  if (start0 === OPEN) throw new Error("life_cycle 的起始轮不能为空");
  let start = start0;
  if (visibleFrom !== undefined) {
    if (visibleFrom < turn) throw new Error("life_cycle 的起效轮不能早于落位轮");
    start = visibleFrom;
  }
  if (end !== OPEN && end < start) throw new Error("life_cycle 的结束轮早于起效轮");
  return { start, end };
}

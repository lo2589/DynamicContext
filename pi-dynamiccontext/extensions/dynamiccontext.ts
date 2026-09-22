/**
 * DynamicContext — declarative lifecycle rules over pi's append-only session.
 *
 * pi already keeps an append-only raw history and a derived model context.
 * What it does not have is the layer in between: rules. This extension adds
 * them. Every entry that enters the session is registered as a slot with a
 * lifecycle rule snapshotted at birth (forward semantics — changing a rule
 * never rewrites what existing slots already promised). Once per turn, at the
 * turn_end boundary, the slots are settled against their rules and the diff
 * is expressed as pi-native context_edit entries:
 *
 *   closing a slot  -> { type: "context_edit", targetId, replacement: null }
 *   reopening a slot -> { type: "context_edit", targetId, replacement: <original> }
 *
 * Raw history is never touched; retirement is a visibility change, not a
 * deletion, which is why dc_recall can search retired slots and why /unpin
 * can bring anything back.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

type Entry = any;

interface SlotMeta {
  element: string;
  bornTurn: number;
  rule: string;
}

interface Snapshot {
  rules: Record<string, string>;
  slots: Record<string, SlotMeta>;
  pins: string[];
  turn: number;
}

const SNAPSHOT_TYPE = "dynamiccontext";

const DEFAULT_RULES: Record<string, string> = {
  user: "permanent",
  assistant: "permanent",
  toolResult: "born+5",
  custom_message: "permanent",
};

/** Where does this range end — the only question a rule answers. */
function resolveEnd(rule: string, bornTurn: number): number | null {
  const r = rule.trim();
  if (r === "born") return bornTurn;
  if (r === "permanent" || r === "null") return null;
  const m = /^born\+(\d+)$/.exec(r);
  if (m) return bornTurn + parseInt(m[1], 10);
  throw new Error(
    `unknown lifecycle rule: ${rule} (available: born, born+n, permanent)`,
  );
}

function elementOf(entry: Entry): string | null {
  if (entry.type === "message") {
    const role = entry.message?.role;
    if (role === "user" || role === "assistant" || role === "toolResult")
      return role;
    return null; // system messages stay pi-managed
  }
  if (entry.type === "custom_message") return "custom_message";
  return null;
}

function textOf(content: any): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content))
    return content
      .filter((b: any) => b?.type === "text")
      .map((b: any) => b.text)
      .join("\n");
  return "";
}

export default function (pi: ExtensionAPI) {
  let rules: Record<string, string> = { ...DEFAULT_RULES };
  let slots = new Map<string, SlotMeta>();
  let pins = new Set<string>();
  let currentTurn = 0;

  function snapshotData(): Snapshot {
    return {
      rules,
      slots: Object.fromEntries(slots),
      pins: [...pins],
      turn: currentTurn,
    };
  }

  /** Latest context_edit per target on the active branch: null replacement = omitted. */
  function appliedEdits(branch: Entry[]): Map<string, any> {
    const latest = new Map<string, any>();
    for (const e of branch) {
      if (e?.type === "context_edit" && e.targetId)
        latest.set(e.targetId, e.replacement ?? null);
    }
    return latest;
  }

  function isVisible(meta: SlotMeta, entryId: string, turn: number): boolean {
    if (pins.has(entryId)) return true;
    const end = resolveEnd(meta.rule, meta.bornTurn);
    return end === null || turn <= end;
  }

  /**
   * Register every unregistered slot-bearing entry on the branch, then diff
   * desired visibility against the edits already applied and return the
   * drafts that close the gap. Fully stateless about what pi has applied:
   * the branch itself is the source of truth, so /tree navigation or a
   * crash mid-turn cannot desync it. Entries that predate the extension are
   * grandfathered in as born at the turn they are first seen.
   */
  function settle(ctx: any): { drafts: Entry[]; changed: boolean } {
    const branch: Entry[] = ctx.sessionManager.getBranch();
    let changed = false;
    for (const entry of branch) {
      const element = elementOf(entry);
      if (!element || !entry.id || slots.has(entry.id)) continue;
      // The rule is snapshotted at birth: changing defaults later is
      // forward-only and never retroactively re-promises an existing slot.
      slots.set(entry.id, {
        element,
        bornTurn: currentTurn,
        rule: rules[element] ?? "permanent",
      });
      changed = true;
    }
    const edits = appliedEdits(branch);
    const drafts: Entry[] = [];
    for (const entry of branch) {
      const meta = slots.get(entry?.id ?? "");
      if (!meta) continue;
      const visible = isVisible(meta, entry.id, currentTurn);
      const omitted = edits.has(entry.id) && edits.get(entry.id) === null;
      if (!visible && !omitted) {
        drafts.push({
          type: "context_edit",
          targetId: entry.id,
          replacement: null,
        });
      } else if (visible && omitted) {
        const original = entry.message?.content ?? entry.content ?? "";
        drafts.push({
          type: "context_edit",
          targetId: entry.id,
          replacement: { content: original },
        });
      }
    }
    return { drafts, changed };
  }

  pi.on("session_start", async (_event, ctx) => {
    for (const entry of ctx.sessionManager.getEntries()) {
      if (entry.type === "custom" && entry.customType === SNAPSHOT_TYPE) {
        const s = entry.data as Snapshot;
        rules = { ...DEFAULT_RULES, ...s.rules };
        slots = new Map(Object.entries(s.slots));
        pins = new Set(s.pins);
        currentTurn = s.turn;
      }
    }
  });

  pi.on("turn_end", async (event, ctx) => {
    // pi's turnIndex restarts per process/run; the ledger needs a counter
    // that survives -c continuations, so we keep our own and increment it
    // once per settled turn (restored from the snapshot on session_start).
    currentTurn += 1;
    const { drafts, changed } = settle(ctx);
    const extra: Entry[] = [];
    if (changed || drafts.length > 0)
      extra.push({
        type: "custom",
        customType: SNAPSHOT_TYPE,
        data: snapshotData(),
      });
    extra.push(...drafts);
    if (extra.length === 0) return undefined;
    return { entries: [...event.entries, ...extra] };
  });

  pi.registerCommand("dc-status", {
    description: "Slot ledger: rules, open/retired counts, pins",
    handler: async (_args, ctx) => {
      const branch: Entry[] = ctx.sessionManager.getBranch();
      const edits = appliedEdits(branch);
      let open = 0;
      let retired = 0;
      for (const [id, meta] of slots) {
        const omitted = edits.has(id) && edits.get(id) === null;
        const visible = isVisible(meta, id, currentTurn) && !omitted;
        if (visible) open++;
        else retired++;
      }
      const ruleLines = Object.entries(rules)
        .map(([el, r]) => `  ${el}: ${r}`)
        .join("\n");
      ctx.ui.notify(
        `turn ${currentTurn} · slots ${slots.size} (open ${open} / retired ${retired}) · pinned ${pins.size}\nrules:\n${ruleLines}`,
        "info",
      );
    },
  });

  pi.registerCommand("dc-rule", {
    description: "Set the birth rule for future slots: /dc-rule toolResult born+8",
    handler: async (args, ctx) => {
      const [element, rule] = (args ?? "").trim().split(/\s+/);
      if (!element || !rule) {
        ctx.ui.notify("usage: /dc-rule <element> <born|born+n|permanent>", "warning");
        return;
      }
      try {
        resolveEnd(rule, 0);
      } catch (err: any) {
        ctx.ui.notify(err.message, "error");
        return;
      }
      rules[element] = rule;
      pi.appendEntry(SNAPSHOT_TYPE, snapshotData());
      ctx.ui.notify(
        `${element}: ${rule} (applies to slots born from now on; existing slots keep the rule they were born with)`,
        "info",
      );
    },
  });

  function lastMessageEntryId(ctx: any): string | null {
    const branch: Entry[] = ctx.sessionManager.getBranch();
    for (let i = branch.length - 1; i >= 0; i--) {
      if (elementOf(branch[i])) return branch[i].id;
    }
    return null;
  }

  pi.registerCommand("pin", {
    description: "Pin an entry so it never retires: /pin [entryId]",
    handler: async (args, ctx) => {
      const id = (args ?? "").trim() || lastMessageEntryId(ctx);
      if (!id) {
        ctx.ui.notify("nothing to pin", "warning");
        return;
      }
      pins.add(id);
      pi.appendEntry(SNAPSHOT_TYPE, snapshotData());
      ctx.ui.notify(`pinned ${id}`, "info");
    },
  });

  pi.registerCommand("unpin", {
    description: "Unpin: /unpin <entryId|all>",
    handler: async (args, ctx) => {
      const id = (args ?? "").trim();
      if (id === "all") pins.clear();
      else pins.delete(id);
      pi.appendEntry(SNAPSHOT_TYPE, snapshotData());
      ctx.ui.notify("unpinned (takes effect at next turn settle)", "info");
    },
  });

  pi.registerTool({
    name: "dc_recall",
    label: "Recall retired slots",
    description:
      "Search the full session ledger, including slots that have retired from the model-visible context. Retirement is a visibility change, not a deletion — everything ever said stays searchable here.",
    parameters: Type.Object({
      query: Type.String({ description: "Text to search for" }),
    }),
    async execute(_id, params, _signal, _onUpdate, ctx) {
      const q = params.query.toLowerCase();
      const hits: string[] = [];
      for (const entry of (ctx as any).sessionManager.getBranch()) {
        const meta = slots.get(entry?.id ?? "");
        if (!meta) continue;
        const text = textOf(entry.message?.content ?? entry.content);
        if (!text.toLowerCase().includes(q)) continue;
        const retired = !isVisible(meta, entry.id, currentTurn);
        hits.push(
          `[${retired ? "retired" : "open"}] turn ${meta.bornTurn} ${meta.element}: ${text.slice(0, 300)}`,
        );
        if (hits.length >= 5) break;
      }
      return {
        content: [
          {
            type: "text",
            text: hits.length ? hits.join("\n") : `no ledger hits for "${params.query}"`,
          },
        ],
        details: {},
      };
    },
  });
}

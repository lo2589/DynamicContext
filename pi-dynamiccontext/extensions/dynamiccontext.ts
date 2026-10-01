/**
 * DynamicContext — the full runtime semantics, ported onto pi's session.
 *
 * pi keeps an append-only raw history and derives the model context from
 * it. What it does not have is the layer in between: declared lifecycles.
 * This extension is that layer — the same system as the Python runtime,
 * reading the same runtime.yaml:
 *
 *   life_cycle    [start, end] bounds per element, closed intervals,
 *                 multi-segment ranges, reopen never recomputes content
 *   patches       /goal xxx remain|roll|refresh n — content patches, plus
 *                 rule patches (forward / force_refresh / reset_from_now /
 *                 add_row) and patch_end ending messages with goal collapse
 *   recall        lexical grep over the whole ledger, trigger-gated,
 *                 written back as a slot that obeys its own life_cycle
 *   compact       periodic / overload / manual summary compaction with
 *                 retention strategies; summaries are slots with anchors
 *                 and compact_range, never fed back through the model
 *   pin           permanent notes attached to the ledger; cancelpin closes
 *                 them from here on without rewriting the past
 *
 * Two surfaces carry the projection:
 *   context hook      — the live π(history, life_cycle): every LLM call
 *                       sees exactly the slots visible at the turn being
 *                       answered
 *   context_edit      — the persisted record of the same truth, written at
 *                       each turn boundary, so the transcript UI and a
 *                       extension-less reload stay honest
 *
 * Raw history is never touched. Retirement is a visibility change, not a
 * deletion, which is why recall can search retired slots and cancelpin
 * does not erase that a pin was ever shown.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { loadDcConfig, type DcConfig } from "./dc/config.js";
import { OPEN } from "./dc/lifecycle.js";
import { DC_PREFIX, Ledger, elementOfEntry, textOfEntry } from "./dc/ledger.js";
import {
  PatchQueue,
  applyRowPatch,
  extractPatchInput,
  patchEndingsForTurn,
  type ContentPatch,
  type RowPatchMode,
} from "./dc/patches.js";
import { formatRecallLines, grepRecall, makeTrigger } from "./dc/recall.js";
import {
  computeSummary,
  contextByteSize,
  isDueOverload,
  isDuePeriodic,
  retentionStrategies,
  summaryElementName,
  windowTranscript,
  SUMMARY_SUFFIX,
} from "./dc/compact.js";
import { SNAPSHOT_TYPE, deserializeState, serializeState, deserializePatch } from "./dc/state.js";

type Entry = any;

const PIN_ELEMENT_PATTERN = /^pin_(?:model|user)_p(\d+)$/;

export default function (pi: ExtensionAPI) {
  pi.registerFlag("dc-config", {
    description: "Path to the runtime.yaml DynamicContext reads",
    type: "string",
  });
  let cfg: DcConfig = loadDcConfig(process.cwd(), pi.getFlag("dc-config") as string | undefined);
  let ledger = new Ledger(cfg.lifeCycle);
  let patchQueue = new PatchQueue();
  let currentTurn = 0;
  let lastUserEntryId: string | null = null;
  let manualCompactRequested = false;
  /** Slot metadata for entries we create ourselves, keyed by customType+content. */
  const pendingMeta = new Map<string, { bornTurn: number; remain?: number; visibleFrom?: number; compactRange?: [number, number] }>();
  /**
   * Occurrences already materialized, keyed `element@turn`. A patch-only
   * submission does not advance the turn counter, so the next real input
   * lands on the same turn number — without this set its patch would
   * materialize twice.
   */
  const materializedOccurrences = new Set<string>();

  function metaKey(customType: string, content: string): string {
    return `${customType}${content}`;
  }

  // -------------------------------------------------------------------------
  // Slots we create: patch occurrences, endings, pins, recall, summaries.
  // Content enters the model context as a user-role custom message; elements
  // that are not protocol roles keep their name as an [element] prefix, the
  // same fallback the runtime's context projection uses.
  // -------------------------------------------------------------------------

  function slotContent(element: string, content: string): string {
    if (element === "user" || element === "assistant" || element === "system") return content;
    return `[${element}]\n${content}`;
  }

  function spawnSlot(
    element: string,
    content: string,
    meta: { bornTurn: number; remain?: number; visibleFrom?: number; compactRange?: [number, number] },
  ): void {
    const customType = `${DC_PREFIX}${element}`;
    const text = slotContent(element, content);
    pendingMeta.set(metaKey(customType, text), meta);
    pi.sendMessage({ customType, content: text, display: true });
  }

  // -------------------------------------------------------------------------
  // Registration: every unregistered slot-bearing entry on the branch joins
  // the ledger, with the rule its element promises right now (forward
  // semantics — a later rule change never rewrites what a slot was promised
  // at birth). Entries that predate the extension are grandfathered in as
  // born at the turn they are first seen.
  // -------------------------------------------------------------------------

  function registerNewEntries(branch: Entry[]): boolean {
    let changed = false;
    for (const entry of branch) {
      const element = elementOfEntry(entry);
      if (!element || !entry.id || ledger.has(entry.id)) continue;
      const customType = String(entry.customType ?? "");
      const key = metaKey(customType, textOfEntry(entry));
      const meta = pendingMeta.get(key);
      if (meta) pendingMeta.delete(key);
      ledger.addSlot(entry.id, element, meta?.bornTurn ?? currentTurn, {
        remain: meta?.remain,
        visibleFrom: meta?.visibleFrom,
        compactRange: meta?.compactRange,
      });
      changed = true;
    }
    return changed;
  }

  /** Latest context_edit per target on the active branch: null = omitted. */
  function appliedEdits(branch: Entry[]): Map<string, any> {
    const latest = new Map<string, any>();
    for (const e of branch) {
      if (e?.type === "context_edit" && e.targetId) latest.set(e.targetId, e.replacement ?? null);
    }
    return latest;
  }

  function entryById(branch: Entry[], id: string): Entry | undefined {
    return branch.find((e) => e?.id === id);
  }

  /**
   * Diff desired visibility at `perspectiveTurn` against the context_edit
   * records already on the branch; return the drafts that close the gap.
   */
  function visibilityDrafts(branch: Entry[], perspectiveTurn: number): Entry[] {
    const edits = appliedEdits(branch);
    const drafts: Entry[] = [];
    for (const [id] of ledger.slots) {
      const entry = entryById(branch, id);
      if (!entry) continue; // not on the active branch (tree navigation)
      const visible = ledger.visibleAt(id, perspectiveTurn);
      const omitted = edits.has(id) && edits.get(id) === null;
      if (!visible && !omitted) {
        drafts.push({ type: "context_edit", targetId: id, replacement: null });
      } else if (visible && omitted) {
        const original = entry.message?.content ?? entry.content ?? "";
        drafts.push({ type: "context_edit", targetId: id, replacement: { content: original } });
      }
    }
    return drafts;
  }

  function snapshotDraft(): Entry {
    return {
      type: "custom",
      customType: SNAPSHOT_TYPE,
      data: serializeState({
        rules: ledger.rules,
        slots: ledger.slots,
        keeps: ledger.keeps,
        queuedPatches: patchQueue.queued,
        sourcePatches: patchQueue.sources,
        currentTurn,
        lastUserEntryId,
        manualCompactRequested,
      }),
    };
  }

  // -------------------------------------------------------------------------
  // Pre-turn materialization (TurnTransaction.prepare_input): patches due
  // this turn replace or add slots, patch_end endings announce closed goals,
  // recall connects the query to retired evidence. All of it lands as real
  // entries before the model is called — nothing bypasses the ledger.
  // -------------------------------------------------------------------------

  function materializeTurn(turn: number, userText: string | null, ctx: any): void {
    // roll/refresh end whatever is active; a new value also retires an
    // earlier ending message, which no longer applies.
    const due = patchQueue
      .forTurn(turn)
      .filter((p) => !materializedOccurrences.has(`${p.element}@${turn}`));
    for (const patch of due) {
      materializedOccurrences.add(`${patch.element}@${turn}`);
      if (patch.mode === "roll" || patch.mode === "refresh") {
        ledger.endActiveElement(patch.element, turn);
      }
      ledger.endActiveElement(`${patch.element}_end`, turn);
    }
    for (const patch of due) {
      spawnSlot(patch.element, patch.content, { bornTurn: turn, remain: patch.turns });
    }
    // patch_end: one durable ending per element whose last cell just closed.
    if (cfg.patchEnd.enabled) {
      const endings = patchEndingsForTurn(turn, ledger, patchQueue.sources, cfg.patchEnd.template);
      for (const [element, message] of endings) {
        ledger.endActiveElement(`${element}_end`, turn);
        spawnSlot(`${element}_end`, message, { bornTurn: turn, remain: 1 });
        // Goal collapse: the turns the element governed fold away; the
        // element's own content stands in as their summary. No model call.
        const collapseFields = cfg.compact?.fields ?? [];
        if (collapseFields.includes(element)) {
          throw new Error("goal_element 不能出现在 compact.fields 里，否则摘要本身也被折叠了");
        }
        const bornTurns = ledger
          .entriesOf(element)
          .map(([, s]) => s.bornTurn)
          .filter((t) => t < turn);
        if (bornTurns.length) {
          const goalBorn = Math.min(...bornTurns);
          for (const field of collapseFields) {
            for (const [id, slot] of ledger.entriesOf(field)) {
              if (slot.bornTurn >= goalBorn && slot.bornTurn < turn) {
                ledger.endCell(id, turn);
              }
            }
          }
        }
      }
    }
    // recall: the trigger decides whether to look; the lookup never searches
    // anything search_fields did not declare.
    if (cfg.recall.type !== "none" && userText) {
      const trigger = makeTrigger(cfg.recall.trigger, cfg.recall.triggerParams);
      if (trigger(userText)) {
        const hits = grepRecall(slotView(), turn, userText, {
          searchFields: cfg.recall.searchFields,
          topK: cfg.recall.topK,
          minScore: cfg.recall.minScore,
        });
        if (hits.length) {
          spawnSlot("recall", formatRecallLines(hits).join("\n"), { bornTurn: turn });
          ctx.ui.notify(`[recall hit ${hits.length}]`, "info");
        }
      }
    }
  }

  /** The ledger as a flat list of (turn, element, text, retired) for recall. */
  function slotView(): { turn: number; element: string; text: string; retired: boolean }[] {
    const branch: Entry[] = lastBranch;
    const view: { turn: number; element: string; text: string; retired: boolean }[] = [];
    for (const [id, slot] of ledger.slots) {
      const entry = entryById(branch, id);
      if (!entry) continue;
      const text = textOfEntry(entry);
      if (!text) continue;
      view.push({
        turn: slot.bornTurn,
        element: slot.element,
        text,
        retired: !ledger.visibleAt(id, currentTurn + 1),
      });
    }
    return view;
  }

  let lastBranch: Entry[] = [];

  // -------------------------------------------------------------------------
  // Compact: periodic / overload / manual summary compression.
  // ---------------------------------------------------------------------------

  function summaryAnchors(): number[] {
    const anchors: number[] = [];
    for (const [, slot] of ledger.slots) {
      if (slot.element.endsWith(SUMMARY_SUFFIX)) anchors.push(slot.bornTurn);
    }
    return [...new Set(anchors)].sort((a, b) => a - b);
  }

  /**
   * Run one compaction when due. Mutates the ledger (covered cells and
   * dropped anchors close their segments; revived anchors append one) and
   * returns the summary as a custom_message draft — the boundary that
   * called us commits it. Never throws into the turn that already
   * committed: a failed summary call just skips the compression.
   */
  async function maybeCompact(manual: boolean): Promise<Entry[]> {
    const compactCfg = cfg.compact;
    if (!compactCfg || currentTurn < 1) return [];
    const anchors = summaryAnchors();
    const lastAnchor = anchors.length ? anchors[anchors.length - 1] : null;
    const visibleMessages = slotView()
      .filter((s) => !s.retired)
      .map((s) => ({ role: "user", content: s.text }));
    const due =
      manual ||
      isDuePeriodic(currentTurn, lastAnchor, compactCfg.intervalTurns, compactCfg.intervalTurns) ||
      isDueOverload(contextByteSize(visibleMessages), compactCfg.overloadThresholdBytes);
    if (process.env.DC_DEBUG) {
      console.error(
        `[dc] compact check: turn=${currentTurn} due=${due} bytes=${contextByteSize(visibleMessages)} threshold=${compactCfg.overloadThresholdBytes} anchors=${anchors}`,
      );
    }
    if (!due) return [];

    const endTurn = currentTurn - compactCfg.keepRecentTurns;
    if (endTurn < 1) return [];
    // Smallest turn <= endTurn still carrying an active raw slot — read out
    // of real activity, so a gap (a crash mid-run, anything) self-heals on
    // the very next compression instead of staying skipped forever.
    const activeCandidates: number[] = [];
    for (const [id, slot] of ledger.slots) {
      if (!compactCfg.fields.includes(slot.element)) continue;
      if (slot.element.endsWith(SUMMARY_SUFFIX) || slot.bornTurn > endTurn) continue;
      if (ledger.visibleAt(id, currentTurn)) activeCandidates.push(slot.bornTurn);
    }
    if (!activeCandidates.length) return [];
    const startTurn = Math.min(...activeCandidates);

    const windowTurns: number[] = [];
    for (let t = startTurn; t <= endTurn; t++) windowTurns.push(t);
    const windowSlots = slotView().filter(
      (s) => windowTurns.includes(s.turn) && compactCfg.fields.includes(s.element),
    );
    const transcript = windowTranscript(windowSlots, compactCfg.fields);
    if (!transcript.trim()) return [];

    let summaryText: string;
    try {
      summaryText = await computeSummary(transcript, compactCfg.prompt);
    } catch (exc) {
      if (process.env.DC_DEBUG) console.error(`[dc] compact summary failed: ${exc}`);
      return []; // compression must never crash a turn that already committed
    }
    if (!summaryText) return [];
    if (process.env.DC_DEBUG) console.error(`[dc] compact summary ok: ${summaryText.length} chars, anchor=${endTurn}`);

    // Retention picks from every summary ever written; dropped ones close a
    // segment, revived ones append one. No summary is recomputed.
    const allAnchors = [...new Set([...anchors, endTurn])].sort((a, b) => a - b);
    const visibleAnchors = new Set(
      anchors.filter((a) => {
        const id = summarySlotId(a);
        return id !== null && ledger.visibleAt(id, currentTurn + 1);
      }),
    );
    const retain = retentionStrategies[compactCfg.retention];
    if (!retain) throw new Error(`未知 retention 策略: ${compactCfg.retention}`);
    const keepAnchors = retain(allAnchors, currentTurn, compactCfg.retentionParams);
    let coveredFrom = startTurn;
    for (const oldAnchor of anchors) {
      if (!keepAnchors.has(oldAnchor) && visibleAnchors.has(oldAnchor)) {
        const id = summarySlotId(oldAnchor);
        const slot = id ? ledger.get(id) : undefined;
        if (slot?.compactRange) coveredFrom = Math.min(coveredFrom, slot.compactRange[0]);
        if (id) ledger.endCell(id, currentTurn);
      }
    }
    for (const oldAnchor of anchors) {
      if (keepAnchors.has(oldAnchor) && !visibleAnchors.has(oldAnchor) && oldAnchor !== endTurn) {
        const id = summarySlotId(oldAnchor);
        if (id) {
          try {
            ledger.reopenCell(id, currentTurn);
          } catch {
            // A summary whose declared end already passed stays down.
          }
        }
      }
    }
    for (const [, slot] of ledger.slots) {
      if (
        compactCfg.fields.includes(slot.element) &&
        windowTurns.includes(slot.bornTurn) &&
        !slot.element.endsWith(SUMMARY_SUFFIX)
      ) {
        const id = [...ledger.slots.entries()].find(([, s]) => s === slot)![0];
        ledger.endCell(id, currentTurn);
      }
    }
    // The summary lands at its anchor turn (the position it replaces) but
    // starts speaking at the turn after this compaction ran.
    const element = summaryElementName(endTurn);
    const text = slotContent(element, summaryText);
    pendingMeta.set(metaKey(`${DC_PREFIX}${element}`, text), {
      bornTurn: endTurn,
      visibleFrom: currentTurn + 1,
      compactRange: [coveredFrom, endTurn],
    });
    return [
      { type: "custom_message", customType: `${DC_PREFIX}${element}`, content: text, display: true },
    ];
  }

  function summarySlotId(anchor: number): string | null {
    for (const [id, slot] of ledger.slots) {
      if (slot.element === summaryElementName(anchor) && slot.bornTurn === anchor) return id;
    }
    return null;
  }

  // -------------------------------------------------------------------------
  // Events
  // -------------------------------------------------------------------------

  pi.on("session_start", async (_event, ctx) => {
    cfg = loadDcConfig(ctx.cwd, pi.getFlag("dc-config") as string | undefined);
    ledger = new Ledger(cfg.lifeCycle);
    patchQueue = new PatchQueue();
    currentTurn = 0;
    lastUserEntryId = null;
    for (const entry of ctx.sessionManager.getEntries()) {
      if (entry.type === "custom" && entry.customType === SNAPSHOT_TYPE) {
        const s = deserializeState(entry.data);
        if (!s) continue; // superseded schema versions are abandoned, not migrated
        ledger = new Ledger({ ...cfg.lifeCycle, ...s.rules });
        ledger.slots = new Map(Object.entries(s.slots));
        ledger.keeps = new Set(s.keeps);
        patchQueue = new PatchQueue();
        patchQueue.queued = s.queuedPatches.map(deserializePatch);
        patchQueue.sources = s.sourcePatches.map(deserializePatch);
        currentTurn = s.currentTurn;
        lastUserEntryId = s.lastUserEntryId;
        manualCompactRequested = s.manualCompactRequested;
      }
    }
    lastBranch = ctx.sessionManager.getBranch();
    registerNewEntries(lastBranch);
  });

  /**
   * User input is intercepted before agent processing: patch syntax is
   * extracted (and a patch-only line is consumed without a turn), due
   * patches materialize, recall fires. This is prepare_input — everything
   * the model is about to see is decided here.
   */
  pi.on("input", (event, _ctx) => {
    if (event.source === "extension") return undefined;
    const text = event.text ?? "";
    const turn = currentTurn + 1;
    let userText: string | null = text;
    let patch: ContentPatch | null = null;
    const trimmed = text.trim();
    if (!isDcCommand(trimmed)) {
      try {
        // Plain text returns unchanged with no patch; a slash line that is
        // not a patch throws (it may be a foreign command) and passes through.
        [userText, patch] = extractPatchInput(trimmed, "user");
      } catch {
        userText = text;
        patch = null;
      }
    }
    if (patch) {
      patch.createdTurn = turn;
      patchQueue.queue(patch);
    }
    if (patch && !userText) {
      // A bare patch submission is its own event in the ledger, not a
      // passenger on whatever conversation comes next — but it is not a
      // conversational turn, so the model is not woken for it.
      materializeTurn(turn, null, _ctx);
      _ctx.ui.notify(
        `patch queued: ${patch.element} ${patch.mode} ${patch.turns === Infinity ? "none" : patch.turns}`,
        "info",
      );
      lastBranch = _ctx.sessionManager.getBranch();
      registerNewEntries(lastBranch);
      pi.appendEntry(SNAPSHOT_TYPE, snapshotDraft().data);
      return { action: "handled" as const };
    }
    materializeTurn(turn, userText, _ctx);
    if (patch && userText) return { action: "transform" as const, text: userText };
    return undefined;
  });

  const DC_COMMANDS = new Set([
    "/dc-status", "/dc-rule", "/dc-addrow", "/pin", "/cancelpin",
    "/dc-keep", "/dc-unkeep", "/dc-compact", "/dc-recall",
  ]);

  function isDcCommand(line: string): boolean {
    const first = line.split(/\s+/, 1)[0];
    return DC_COMMANDS.has(first);
  }

  /**
   * The live projection. pi's own projection already honors persisted
   * context_edits; this hook closes the gap for everything the ledger has
   * decided since the last boundary — a roll replacement, a just-ended
   * goal's collapse — so every LLM call sees exactly π(history, life_cycle)
   * at the turn being answered.
   */
  /**
   * The live projection. pi's own projection already honors persisted
   * context_edits; this hook closes the gap for everything the ledger has
   * decided since the last boundary — a roll replacement, a refresh that
   * ended a value seconds ago — so every LLM call sees exactly
   * π(history, life_cycle) at the turn being answered.
   *
   * event.messages and the extension's own buildSessionProjection() call
   * produce different object instances, so identity mapping is useless.
   * Instead the branch is walked in order and each event message is matched
   * to the next slot-bearing entry whose projected text equals it — the
   * projection is deterministic and order-preserving, so two pointers
   * align. Untagged messages (compaction summaries, branch summaries,
   * bash executions) are pi-managed and always kept.
   */
  pi.on("context", (event, ctx) => {
    const branch: Entry[] = ctx.sessionManager.getBranch();
    const edits = appliedEdits(branch);
    const expected: { id: string; text: string }[] = [];
    for (const entry of branch) {
      const element = elementOfEntry(entry);
      if (!element || !entry.id || !ledger.has(entry.id)) continue;
      if (edits.has(entry.id) && edits.get(entry.id) === null) continue; // pi already hides it
      const replacement = edits.get(entry.id);
      const text = textOfEntry(replacement ? { content: replacement.content } : entry);
      expected.push({ id: entry.id, text });
    }
    // The turn being answered: one past the settled turn while a fresh user
    // message sits on the branch, otherwise the settled turn itself (tool
    // loop continuations re-fire this hook inside the same turn).
    let newestUser: string | null = null;
    for (const entry of branch) {
      if (entry?.type === "message" && entry.message?.role === "user") newestUser = entry.id;
    }
    const answeringTurn =
      newestUser && newestUser !== lastUserEntryId ? currentTurn + 1 : currentTurn;
    const drop = new Set<any>();
    let i = 0;
    for (const message of event.messages) {
      const text = textOfEntry({ content: (message as any).content });
      let tagged: string | null = null;
      for (let j = i; j < expected.length; j++) {
        if (expected[j].text === text) {
          tagged = expected[j].id;
          i = j + 1;
          break;
        }
      }
      if (tagged && !ledger.visibleAt(tagged, answeringTurn)) drop.add(message);
    }
    if (process.env.DC_DEBUG) {
      console.error(`[dc] context hook: turn=${answeringTurn} drop=${drop.size}/${event.messages.length}`);
    }
    if (drop.size === 0) return undefined;
    return { messages: event.messages.filter((m: any) => !drop.has(m)) };
  });

  /**
   * turn_end is the commit boundary: the turn counter advances when a new
   * user message landed (a tool loop is one turn, not five), new entries
   * join the ledger, patches are consumed, compaction runs when due, and
   * the diff against the branch's context_edits is written as new records.
   */
  pi.on("turn_end", async (event, ctx) => {
    const branch: Entry[] = ctx.sessionManager.getBranch();
    lastBranch = branch;
    let newestUser: string | null = null;
    for (const entry of branch) {
      if (entry?.type === "message" && entry.message?.role === "user") newestUser = entry.id;
    }
    if (newestUser && newestUser !== lastUserEntryId) {
      currentTurn += 1;
      lastUserEntryId = newestUser;
    }
    registerNewEntries(branch);
    patchQueue.consume(currentTurn);
    let compactDrafts: Entry[] = [];
    try {
      compactDrafts = await maybeCompact(manualCompactRequested);
    } catch (exc) {
      if (process.env.DC_DEBUG) console.error(`[dc] compact aborted: ${exc}`);
      compactDrafts = []; // compression is best-effort; the turn already committed
    }
    manualCompactRequested = false;
    const drafts = visibilityDrafts(lastBranch, currentTurn + 1);
    return { entries: [...event.entries, ...compactDrafts, ...drafts, snapshotDraft()] };
  });

  /**
   * When pi's own threshold/overflow compaction fires, our pipeline answers
   * it with the project's compressor instead of pi's default summarizer —
   * same wire, our content. Falls back to pi's built-in when ours has
   * nothing new to fold.
   */
  pi.on("session_before_compact", async (event) => {
    if (!cfg.compact) return undefined;
    try {
      const branch: Entry[] = (event as any).branchEntries ?? [];
      const fields = cfg.compact.fields;
      const lines: string[] = [];
      for (const entry of branch) {
        const element = elementOfEntry(entry);
        if (!element || !fields.includes(element)) continue;
        const text = textOfEntry(entry);
        if (text) lines.push(`[${element}] ${text}`);
      }
      if (!lines.length) return undefined;
      const summary = await computeSummary(lines.join("\n"), cfg.compact.prompt);
      if (!summary) return undefined;
      return {
        compaction: {
          summary,
          firstKeptEntryId: (event as any).preparation?.firstKeptEntryId ?? undefined,
          tokensBefore: (event as any).preparation?.tokensBefore ?? 0,
        },
      };
    } catch {
      return undefined; // pi's built-in compaction is the fallback
    }
  });

  // -------------------------------------------------------------------------
  // Commands
  // -------------------------------------------------------------------------

  function parseBoundText(raw: string): unknown {
    if (raw === "null" || raw === "none" || raw === "permanent") return null;
    if (/^\d+$/.test(raw)) return Number(raw);
    return raw; // born / born+n / until_* — resolveBound validates
  }

  pi.registerCommand("dc-status", {
    description: "Slot ledger: ranges, rules, keeps, patches, summaries",
    handler: async (_args, ctx) => {
      const branch: Entry[] = ctx.sessionManager.getBranch();
      lastBranch = branch;
      registerNewEntries(branch);
      const perspective = currentTurn + 1;
      const lines: string[] = [];
      for (const [id, slot] of ledger.slots) {
        const ranges = slot.ranges
          .map((r) => `[${r.start}, ${r.end === OPEN ? "∞" : r.end}]`)
          .join(" ");
        const visible = ledger.visibleAt(id, perspective);
        const dead = slot.dead !== null ? ` dead@${slot.dead}` : "";
        const kept = ledger.keeps.has(id) ? " pinned" : "";
        const cr = slot.compactRange ? ` covers[${slot.compactRange[0]},${slot.compactRange[1]}]` : "";
        lines.push(
          `${visible ? "open  " : "retired"}  t${slot.bornTurn}  ${slot.element}  ${ranges}${dead}${kept}${cr}  ${id.slice(0, 8)}`,
        );
      }
      const rules = Object.entries(ledger.rules)
        .map(([el, r]) => `  ${el}: [${String(r[0])}, ${r[1] === null ? "null" : String(r[1])}]`)
        .join("\n");
      const patches = patchQueue.queued
        .map((p) => `  ${p.element} ${p.mode} ${p.turns === Infinity ? "none" : p.turns} @t${p.createdTurn}`)
        .join("\n");
      ctx.ui.notify(
        `turn ${currentTurn} · slots ${ledger.slots.size} · config ${cfg.sourcePath ?? "built-in"}\nrules:\n${rules}\npatches:${patches ? `\n${patches}` : " (none)"}\nslots:\n${lines.join("\n") || "  (empty)"}`,
        "info",
      );
    },
  });

  pi.registerCommand("dc-rule", {
    description: "Change an element's life_cycle: /dc-rule toolResult born born+8 [forward|force_refresh|reset_from_now]",
    handler: async (args, ctx) => {
      const parts = (args ?? "").trim().split(/\s+/).filter(Boolean);
      if (parts.length < 2 || parts.length > 4) {
        ctx.ui.notify("usage: /dc-rule <element> <start> [end] [forward|force_refresh|reset_from_now]", "warning");
        return;
      }
      const [element, startRaw] = parts;
      let endRaw: string = "born";
      let mode: RowPatchMode = "forward";
      if (parts.length === 3) {
        if (isRowPatchMode(parts[2])) mode = parts[2];
        else endRaw = parts[2];
      } else if (parts.length === 4) {
        endRaw = parts[2];
        if (!isRowPatchMode(parts[3])) {
          ctx.ui.notify(`未知模式: ${parts[3]}`, "error");
          return;
        }
        mode = parts[3];
      }
      try {
        const message = applyRowPatch(
          ledger,
          element,
          [parseBoundText(startRaw), parseBoundText(endRaw)],
          mode,
          currentTurn,
        );
        pi.appendEntry(SNAPSHOT_TYPE, snapshotDraft().data);
        ctx.ui.notify(message, "info");
      } catch (exc: any) {
        ctx.ui.notify(String(exc?.message ?? exc), "error");
      }
    },
  });

  function isRowPatchMode(raw: string): raw is RowPatchMode {
    return raw === "forward" || raw === "force_refresh" || raw === "reset_from_now";
  }

  pi.registerCommand("dc-addrow", {
    description: "Declare a new element: /dc-addrow goal born null",
    handler: async (args, ctx) => {
      const parts = (args ?? "").trim().split(/\s+/).filter(Boolean);
      if (parts.length < 2 || parts.length > 3) {
        ctx.ui.notify("usage: /dc-addrow <element> <start> [end]", "warning");
        return;
      }
      try {
        const message = applyRowPatch(
          ledger,
          parts[0],
          [parseBoundText(parts[1]), parseBoundText(parts[2] ?? "null")],
          "add_row",
          currentTurn,
        );
        pi.appendEntry(SNAPSHOT_TYPE, snapshotDraft().data);
        ctx.ui.notify(message, "info");
      } catch (exc: any) {
        ctx.ui.notify(String(exc?.message ?? exc), "error");
      }
    },
  });

  /**
   * /pin attaches a permanent note to the ledger — a new slot, not a
   * flag on an old one. Ids are derived from the highest pin id ever seen,
   * never recycled after /cancelpin.
   */
  pi.registerCommand("pin", {
    description: "Attach a permanent note: /pin 孩子对花生过敏",
    handler: async (args, ctx) => {
      const content = (args ?? "").trim();
      if (!content) {
        ctx.ui.notify("usage: /pin <内容>", "warning");
        return;
      }
      if (currentTurn < 1) {
        ctx.ui.notify("[pin skipped: no committed turn to attach to yet]", "warning");
        return;
      }
      lastBranch = ctx.sessionManager.getBranch();
      registerNewEntries(lastBranch);
      const numbers = [...ledger.slots.values()]
        .map((s) => PIN_ELEMENT_PATTERN.exec(s.element))
        .filter((m): m is RegExpExecArray => m !== null)
        .map((m) => Number(m[1]));
      const pinId = `p${String(numbers.length ? Math.max(...numbers) + 1 : 1).padStart(2, "0")}`;
      spawnSlot(`pin_user_${pinId}`, content, { bornTurn: currentTurn });
      pi.appendEntry(SNAPSHOT_TYPE, snapshotDraft().data);
      ctx.ui.notify(`[pin created: pin_user_${pinId}] ${content}`, "info");
    },
  });

  pi.registerCommand("cancelpin", {
    description: "End a pin from here on: /cancelpin p01",
    handler: async (args, ctx) => {
      const pinId = (args ?? "").trim();
      lastBranch = ctx.sessionManager.getBranch();
      registerNewEntries(lastBranch);
      const found = [...ledger.slots.entries()].find(([, s]) => {
        const m = PIN_ELEMENT_PATTERN.exec(s.element);
        return m && `p${String(Number(m[1])).padStart(2, "0")}` === pinId;
      });
      if (!found) {
        ctx.ui.notify(`[cancelpin: no such pin ${pinId}]`, "warning");
        return;
      }
      // The pin was genuinely shown through the turns that already happened;
      // cancelling only stops it from here on.
      found[1].dead = currentTurn + 1;
      pi.appendEntry(SNAPSHOT_TYPE, snapshotDraft().data);
      ctx.ui.notify(`[pin cancelled: ${found[1].element}]`, "info");
    },
  });

  pi.registerCommand("dc-keep", {
    description: "Protect an entry from retirement: /dc-keep [entryId] (default: latest)",
    handler: async (args, ctx) => {
      const branch: Entry[] = ctx.sessionManager.getBranch();
      lastBranch = branch;
      registerNewEntries(branch);
      let id = (args ?? "").trim();
      if (!id) {
        for (let i = branch.length - 1; i >= 0; i--) {
          if (elementOfEntry(branch[i])) {
            id = branch[i].id;
            break;
          }
        }
      }
      if (!id || !ledger.has(id)) {
        ctx.ui.notify("nothing to keep", "warning");
        return;
      }
      ledger.keeps.add(id);
      pi.appendEntry(SNAPSHOT_TYPE, snapshotDraft().data);
      ctx.ui.notify(`kept ${id}`, "info");
    },
  });

  pi.registerCommand("dc-unkeep", {
    description: "Drop retirement protection: /dc-unkeep <entryId|all>",
    handler: async (args, ctx) => {
      const id = (args ?? "").trim();
      if (id === "all") ledger.keeps.clear();
      else ledger.keeps.delete(id);
      pi.appendEntry(SNAPSHOT_TYPE, snapshotDraft().data);
      ctx.ui.notify("unkept (takes effect at next turn settle)", "info");
    },
  });

  pi.registerCommand("dc-compact", {
    description: "Run summary compaction at the end of this turn",
    handler: async (_args, ctx) => {
      if (!cfg.compact) {
        ctx.ui.notify("compact 未启用（配置里没有 compact 段）", "warning");
        return;
      }
      manualCompactRequested = true;
      ctx.ui.notify("compact requested — runs at this turn's settle", "info");
    },
  });

  pi.registerCommand("dc-recall", {
    description: "Manual recall preview: /dc-recall 花生",
    handler: async (args, ctx) => {
      const query = (args ?? "").trim();
      if (!query) {
        ctx.ui.notify("usage: /dc-recall <query>", "warning");
        return;
      }
      lastBranch = ctx.sessionManager.getBranch();
      const hits = grepRecall(slotView(), currentTurn + 1, query, {
        searchFields: cfg.recall.searchFields.length ? cfg.recall.searchFields : [...new Set([...ledger.slots.values()].map((s) => s.element))],
        topK: cfg.recall.topK,
        minScore: cfg.recall.minScore,
      });
      ctx.ui.notify(
        hits.length ? formatRecallLines(hits).join("\n") : `no ledger hits for "${query}"`,
        "info",
      );
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
      lastBranch = (ctx as any).sessionManager.getBranch();
      const hits = grepRecall(slotView(), currentTurn + 1, params.query, {
        searchFields: cfg.recall.searchFields.length
          ? cfg.recall.searchFields
          : [...new Set([...ledger.slots.values()].map((s) => s.element))],
        topK: cfg.recall.topK,
        minScore: cfg.recall.minScore,
      });
      return {
        content: [
          {
            type: "text",
            text: hits.length
              ? formatRecallLines(hits).join("\n")
              : `no ledger hits for "${params.query}"`,
          },
        ],
        details: {},
      };
    },
  });
}

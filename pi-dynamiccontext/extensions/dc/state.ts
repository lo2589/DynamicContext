/**
 * dc/state.ts — snapshot schema v2 and its (de)serialization.
 *
 * The whole ledger state travels in one pi custom entry per settle, so a
 * fresh process replays the latest snapshot and continues. Infinity (a
 * patch's turns=none) and open range ends (null) survive the round trip
 * explicitly rather than by JSON accident.
 */

import type { SlotMeta } from "./ledger.js";
import type { ContentPatch } from "./patches.js";

export const SNAPSHOT_TYPE = "dynamiccontext";
export const SNAPSHOT_VERSION = 2;

export interface SnapshotV2 {
  v: number;
  rules: Record<string, [unknown, unknown]>;
  slots: Record<string, SlotMeta>;
  keeps: string[];
  queuedPatches: SerializedPatch[];
  sourcePatches: SerializedPatch[];
  currentTurn: number;
  lastUserEntryId: string | null;
  manualCompactRequested: boolean;
}

type SerializedPatch = Omit<ContentPatch, "turns"> & { turns: number | "INF" };

function serializePatch(patch: ContentPatch): SerializedPatch {
  return { ...patch, turns: patch.turns === Infinity ? "INF" : patch.turns };
}

export function deserializePatch(patch: SerializedPatch): ContentPatch {
  return { ...patch, turns: patch.turns === "INF" ? Infinity : patch.turns };
}

export function serializeState(state: {
  rules: Record<string, [unknown, unknown]>;
  slots: Map<string, SlotMeta>;
  keeps: Set<string>;
  queuedPatches: ContentPatch[];
  sourcePatches: ContentPatch[];
  currentTurn: number;
  lastUserEntryId: string | null;
  manualCompactRequested: boolean;
}): SnapshotV2 {
  return {
    v: SNAPSHOT_VERSION,
    rules: state.rules,
    slots: Object.fromEntries(state.slots),
    keeps: [...state.keeps],
    queuedPatches: state.queuedPatches.map(serializePatch),
    sourcePatches: state.sourcePatches.map(serializePatch),
    currentTurn: state.currentTurn,
    lastUserEntryId: state.lastUserEntryId,
    manualCompactRequested: state.manualCompactRequested,
  };
}

export function deserializeState(raw: unknown): SnapshotV2 | null {
  if (typeof raw !== "object" || raw === null) return null;
  const data = raw as Record<string, unknown>;
  if (data.v !== SNAPSHOT_VERSION) return null; // v1 snapshots are superseded, not migrated
  return {
    v: SNAPSHOT_VERSION,
    rules: (data.rules ?? {}) as Record<string, [unknown, unknown]>,
    slots: (data.slots ?? {}) as Record<string, SlotMeta>,
    keeps: (data.keeps ?? []) as string[],
    queuedPatches: (data.queuedPatches ?? []) as SerializedPatch[],
    sourcePatches: (data.sourcePatches ?? []) as SerializedPatch[],
    currentTurn: Number(data.currentTurn ?? 0),
    lastUserEntryId: (data.lastUserEntryId as string | null) ?? null,
    manualCompactRequested: Boolean(data.manualCompactRequested),
  };
}

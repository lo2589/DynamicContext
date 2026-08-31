"""The turn-by-turn compaction pipeline: read compact.* config, decide
whether a summary is due, render the window, run the configured compressor,
and fold the result back into the ledger through FixedTableLifecycle.

This is the runtime-integration layer over the compressor/retention
strategies in summery.py and the due-checks in processor.py — those are
pure policy, this is what actually calls them each turn and writes the
outcome back to history/state/context.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from ..manager.engine import FixedTableLifecycle
from ..registry import compact, manager, saver
from .processor import (
    ProcessorRuntime,
    context_byte_size,
    is_due_manual,
    is_due_overload,
    is_due_periodic,
)
from .summery import DEFAULT_RETENTION

if TYPE_CHECKING:
    from ..manager.runtime import RuntimeComponents

    History = dict[str, dict[str, dict[str, Any]]]
    State = dict[str, dict[str, int]]

SUMMARY_SUFFIX = "_summary"

# A summary slot carries its own compact_range — the span of turns it
# covers, reaching back through any earlier summary it supersedes — recorded
# outright rather than left to be inferred from anchor turns and retention.
COMPACT_RANGE_KEY = "compact_range"


def _compact_enabled(cfg: Any) -> bool:
    """``compact: none`` (or an absent section) turns the whole mechanism off,
    the same convention chat.tools/chat.search/recall.type already use."""

    return isinstance(cfg.to_dict().get("compact"), dict)


def _compact_settings(cfg: Any) -> tuple[int, str, int, int, tuple[str, ...]]:
    """Read compact.* once; every field falls back if the section is absent."""

    compact_cfg = cfg.to_dict().get("compact")
    compact_cfg = compact_cfg if isinstance(compact_cfg, dict) else {}
    overload_threshold = int(compact_cfg.get("overload_threshold_bytes") or 32000)
    compressors = compact_cfg.get("compressors")
    compressors = compressors if isinstance(compressors, dict) else {}
    summary_cfg = compressors.get("summary")
    summary_cfg = summary_cfg if isinstance(summary_cfg, dict) else {}
    prompt = summary_cfg.get("prompt")
    if not prompt:
        raise ValueError(
            "启用 compact 时必须声明 compact.compressors.summary.prompt："
            "提示词是内容，属于配置，不由通用代码代写"
        )
    periodic = summary_cfg.get("periodic")
    periodic = periodic if isinstance(periodic, dict) else {}
    interval_turns = int(periodic.get("interval_turns") or 20)
    keep_recent_turns = int(periodic.get("keep_recent_turns") or 10)
    raw_fields = compact_cfg.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ValueError(
            "启用 compact 时必须声明 compact.fields："
            "哪些元素可被折叠是配置，通用代码不预设槽位名"
        )
    fields = tuple(str(item) for item in raw_fields)
    return overload_threshold, prompt, interval_turns, keep_recent_turns, fields


def _last_summary_turn(history: "History") -> int | None:
    turns = [
        int(turn_id)
        for turn_id, content in history.items()
        if any(element.endswith(SUMMARY_SUFFIX) for element in content)
    ]
    return max(turns) if turns else None


def _summary_element_name(anchor_turn: int) -> str:
    """The summary anchored at end_turn replaces that turn's own position —
    same naming convention as the reference Processor's derived column_id
    (f"{end_turn}_{name}"), just placed at an existing turn key instead of a
    synthetic new one. No new number ever enters the sequence."""
    return f"{anchor_turn}{SUMMARY_SUFFIX}"


def _first_uncompressed_turn(
    history: "History", state: "State", end_turn: int, fields: tuple[str, ...]
) -> int | None:
    """Smallest turn <= end_turn still carrying an active raw slot.

    Deliberately not "the turn after the last summary was written" — that
    turn number is current_turn at write time, not the end_turn the previous
    window actually covered, and the two differ by keep_recent_turns. Reading
    real activity out of state instead of trusting a turn-number formula also
    means a prior gap (a bug, a crash mid-run, anything) self-heals on the
    very next compression instead of staying skipped forever.
    """
    candidates = [
        int(turn_id)
        for turn_id, content in history.items()
        if int(turn_id) <= end_turn
        and any(
            element in content and state.get(turn_id, {}).get(element) == 1
            for element in fields
        )
    ]
    return min(candidates) if candidates else None


def _window_transcript(
    history: "History", window: tuple[int, ...], fields: tuple[str, ...]
) -> str:
    """Render a window of raw turns into plain text for the prompt.

    Raw turns only: summaries are never fed back in, so a summary always
    describes original exchanges rather than other summaries. Which elements
    appear is compact.fields — the same declaration that says what may be
    folded, so a config can never fold something the summarizer never saw.
    """

    lines: list[str] = []
    for entry in window:
        content = history.get(str(entry), {})
        for element in fields:
            slot = content.get(element)
            if slot and slot.get("content"):
                lines.append(f"[第{entry}轮 {element}] {slot['content']}")
    return "\n".join(lines)


def _all_summary_anchors(history: "History") -> list[int]:
    """Every summary ever written, visible or not, oldest first.

    A summary is computed once, at the compaction turn that produced it, and
    then kept in history forever. Retention never recomputes one — it only
    decides which of these already-written summaries are visible for the
    coming stretch, so the whole set is always the candidate pool.
    """

    return sorted(
        int(turn_id)
        for turn_id, content in history.items()
        for element in content
        if element == _summary_element_name(int(turn_id))
    )


def _active_summary_anchors(history: "History", state: "State") -> list[int]:
    """Every summary visible right now, oldest first."""

    return sorted(
        int(turn_id)
        for turn_id, content in history.items()
        for element in content
        if element == _summary_element_name(int(turn_id))
        and state.get(turn_id, {}).get(element) == 1
    )


def _retention_settings(cfg: Any) -> tuple[str, dict[str, Any]]:
    """Read compact.compressors.summary.retention; default keeps only the
    newest summary, which is what compaction did before this was a choice."""

    compact_cfg = cfg.to_dict().get("compact")
    compact_cfg = compact_cfg if isinstance(compact_cfg, dict) else {}
    compressors = compact_cfg.get("compressors")
    compressors = compressors if isinstance(compressors, dict) else {}
    summary_cfg = compressors.get("summary")
    summary_cfg = summary_cfg if isinstance(summary_cfg, dict) else {}
    retention = summary_cfg.get("retention")
    if isinstance(retention, str):
        return retention, {}
    if not isinstance(retention, dict):
        return DEFAULT_RETENTION, {}
    name = str(retention.get("type") or DEFAULT_RETENTION)
    params = {
        key: value for key, value in retention.items() if key != "type"
    }
    return name, params


def _apply_summary_result(
    lifecycle: FixedTableLifecycle,
    *,
    anchor_turn: int,
    retire_turn: int,
    summary_text: str,
    window: tuple[int, ...],
    fields: tuple[str, ...],
    retire_anchors: tuple[int, ...] = (),
    reopen_anchors: tuple[int, ...] = (),
    covered_turns: tuple[int, ...] = (),
) -> None:
    """Insert the summary at the position it replaces and retire every source
    cell — always through add_slot/end_cell, which keep the persisted history
    in sync (unlike the reference Engine's add_derived_column, which only
    touches its own internal tables and would leave the real history/context
    blind to it).

    anchor_turn is where the summary lives (end_turn — an existing turn, so
    no new number enters the sequence). retire_turn is when the retirement
    takes effect (current_turn — when this compression decision was made).
    retire_anchors are summaries the retention strategy dropped for the
    coming stretch; reopen_anchors are ones it picked back up. Neither is
    recomputed — retiring closes a range segment, reopening appends a new
    one, and the content written at that anchor's own compaction turn stands
    unchanged the whole time.
    """

    # Two facts about a summary, kept apart on purpose:
    #   where it sits   — anchor_turn, the last turn it stands for
    #   when it speaks  — retire_turn, the turn the compaction actually ran
    # and one fact recorded outright rather than left to be inferred:
    #   compact_range   — the span of turns this summary compacted, reaching
    #                     back through any earlier summary it supersedes
    covered_from = min(covered_turns) if covered_turns else anchor_turn
    for old_anchor in retire_anchors:
        old = lifecycle.history.get(str(old_anchor), {}).get(
            _summary_element_name(old_anchor)
        ) or {}
        span = old.get(COMPACT_RANGE_KEY)
        if isinstance(span, list) and span:
            covered_from = min(covered_from, int(span[0]))
    lifecycle.add_slot(
        _summary_element_name(anchor_turn),
        summary_text,
        turn=anchor_turn,
        visible_from=retire_turn,
        extra={COMPACT_RANGE_KEY: [covered_from, anchor_turn]},
    )
    for old_anchor in retire_anchors:
        lifecycle.end_cell(
            _summary_element_name(old_anchor), old_anchor, turn=retire_turn
        )
    for old_anchor in reopen_anchors:
        lifecycle.reopen_cell(
            _summary_element_name(old_anchor), old_anchor, turn=retire_turn
        )
    for entry in window:
        content = lifecycle.history.get(str(entry), {})
        for element in fields:
            if element in content:
                lifecycle.end_cell(element, entry, turn=retire_turn)


def _maybe_compress(runtime: "RuntimeComponents", *, manual_requested: bool = False) -> None:
    """Best-effort periodic/overload/manual summary compression.

    Runs as its own small transaction after a turn (or a bare /compat
    request) commits: recompute from the just-saved tables, mutate, validate,
    save again — the same pattern _restore_committed_patches already uses.
    A failure here (e.g. the provider call) is non-fatal; it must never lose
    the conversational turn that already committed successfully.
    """

    from ..manager.runtime import _last_turn  # cycle guard: runtime imports us at module level

    if not _compact_enabled(runtime.cfg):
        return
    current_turn = _last_turn(runtime.tables.history)
    if current_turn < 1:
        return
    overload_threshold, prompt, interval_turns, keep_recent_turns, fields = _compact_settings(
        runtime.cfg
    )
    last_summary_turn = _last_summary_turn(runtime.tables.history)
    periodic_runtime = ProcessorRuntime(last_run_turn=last_summary_turn)
    context_size = context_byte_size(runtime.tables.context)
    due = (
        is_due_periodic(
            current_turn, periodic_runtime, step=interval_turns, first_run_turn=interval_turns
        )
        or is_due_overload(context_size, threshold_bytes=overload_threshold)
        or is_due_manual(manual_requested)
    )
    if not due:
        return

    end_turn = current_turn - keep_recent_turns
    if end_turn < 1:
        return
    start_turn = _first_uncompressed_turn(
        runtime.tables.history, runtime.tables.state, end_turn, fields
    )
    if start_turn is None:
        return  # nothing new has accumulated past the keep-recent window yet

    # Only raw turns are summarized. A summary is produced once, for the
    # stretch of history it covers, and never fed back through the model —
    # so nothing is ever re-compressed and no summary is a summary of
    # summaries.
    window: tuple[int, ...] = tuple(range(start_turn, end_turn + 1))
    transcript = _window_transcript(runtime.tables.history, window, fields)
    if not transcript.strip():
        return
    try:
        summary_text = compact["summary"](
            runtime, transcript, prompt, turn_id=current_turn
        )
    except Exception as exc:  # noqa: BLE001 - compression must never crash a turn
        print(f"[compress skipped: {exc}]")
        return
    if not summary_text:
        return

    # Retention picks from every summary ever written — including ones that
    # went invisible several compactions ago, since they are all still in
    # history. Whatever it picks becomes visible until the next compaction:
    # dropped ones get their current range segment closed, revived ones get
    # a fresh segment appended. No summary is recomputed either way.
    all_anchors = sorted({*_all_summary_anchors(runtime.tables.history), end_turn})
    visible_anchors = set(
        _active_summary_anchors(runtime.tables.history, runtime.tables.state)
    )
    keep_anchors = runtime.retain_summaries(
        anchors=all_anchors,
        current_turn=current_turn,
        **runtime.retention_params,
    )
    retire_anchors = tuple(sorted(visible_anchors - set(keep_anchors)))
    reopen_anchors = tuple(
        sorted(set(keep_anchors) - visible_anchors - {end_turn})
    )

    history = copy.deepcopy(runtime.tables.history)
    lifecycle: FixedTableLifecycle = manager["lifecycle.fixed"](
        history, runtime.tables.life_cycle, current_turn=current_turn
    )
    _apply_summary_result(
        lifecycle,
        anchor_turn=end_turn,
        retire_turn=current_turn,
        summary_text=summary_text,
        window=window,
        fields=fields,
        retire_anchors=retire_anchors,
        reopen_anchors=reopen_anchors,
        covered_turns=window,
    )
    lifecycle.validate()
    state = lifecycle.state_snapshot()
    context = runtime.select_context(
        history, state, through_turn=current_turn, roles=runtime.element_roles
    )
    saver["history.save"](runtime.tables, history)
    saver["state.save"](runtime.tables, state)
    saver["context.save"](runtime.tables, context)
    print(f"[compressed turns {start_turn}-{end_turn} into {end_turn}_summary]")

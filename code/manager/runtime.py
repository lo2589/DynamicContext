"""Fixed-table conversational runtime assembled from decorated components."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .context import select_context_without_extra_filter
from .engine import FixedTableLifecycle
from ..compact import DEFAULT_RETENTION, retain_latest_only
from ..compact.processor import (
    ProcessorRuntime,
    context_byte_size,
    is_due_manual,
    is_due_overload,
    is_due_periodic,
)
from ..dataset.input_data import (
    MANUAL_CHECK_COMMAND,
    MANUAL_COMPRESS_COMMAND,
    CancelPinCommand,
    DatasetInputProcessor,
    DialogueInput,
    PinCommand,
)
from ..dataset.resume_context import DatasetContext
from ..dataset.tables import DatasetTables
from ..lifecycle.process import process
from ..lifecycle.rules import end_functions
from ..patch.state_patch import ContentPatch, ContentPatchMode
from ..provider import ParsedAnswer, build_provider_from_cfg
from ..recall import recall as recall_registry
from ..registry import compact, dataset, manager, patch, provider as provider_registry, saver
from ..saver import table_printer as _table_printer  # Register console printers.
from ..saver import live_view as _live_view  # Register viewer.serve entry.
from ..saver import session_registry as _session_registry  # Register session.*/hub.serve.


History = dict[str, dict[str, dict[str, Any]]]
State = dict[str, dict[str, int]]
Context = list[dict[str, str]]
SUMMARY_SUFFIX = "_summary"
# Recorded on the summary slot: the span of turns it compacted. Written
# rather than inferred, because the interval on a record says when a slot
# is visible, never what it replaced.
COMPACT_RANGE_KEY = "compact_range"
# Fallback only — the real answer is always compact.fields in YAML (read by
# _compact_settings). Which elements compaction may fold is data, not a
# code constant; a config that adds new elements (e.g. pin) declares its own
# field list instead of inheriting whatever happens to be hardcoded here.
# The slot the input-echo channel writes. A channel naming its own output is
# not a name-based branch — it is that channel's identity — but nothing else
# in the runtime may assume it.
INPUT_ELEMENT = "user"
DEFAULT_RECALL_TRIGGER = "always"

# pin_{origin}_{id} — origin (model/user) and id are both visible straight in
# the element name, the same way role is visible in "user"/"assistant"
# without a separate metadata table. Ids never repeat and are never
# recycled after a /cancelpin: they're derived from the highest id ever
# seen in history, not a remembered counter — same self-healing reasoning
# as the compaction gap fix (see _first_uncompressed_turn).
PIN_ELEMENT_PATTERN = re.compile(r"^pin_(?:model|user)_p(\d+)$")

# The recall channel's own slot name — one slot per turn holding everything
# that turn pulled back. A single fixed name (rather than recall_1..recall_n)
# keeps it declarable in life_cycle exactly like user/think/assistant, so how
# long recalled evidence stays visible is a YAML decision: [born, born] for
# evidence that serves only the turn that asked for it, [born, null] to make
# it stick.
RECALL_ELEMENT = "recall"


def _next_pin_id(history: History) -> str:
    numbers = [
        int(match.group(1))
        for content in history.values()
        for element in content
        if (match := PIN_ELEMENT_PATTERN.match(element))
    ]
    return f"p{(max(numbers) + 1) if numbers else 1:02d}"


def _pin_element_name(origin: str, pin_id: str) -> str:
    return f"pin_{origin}_{pin_id}"


def _find_pin_element(history: History, pin_id: str) -> tuple[str, int] | None:
    """Locate the (element_name, born_turn) for a bare id like "p01" —
    /cancelpin doesn't know (or need to know) which origin created it."""

    for turn_id, content in history.items():
        for element in content:
            match = PIN_ELEMENT_PATTERN.match(element)
            if match and f"p{int(match.group(1)):02d}" == pin_id:
                return element, int(turn_id)
    return None


def _last_turn(history: History) -> int:
    return max((int(turn_id) for turn_id in history), default=0)


# Dead code, kept as reference only — predates FixedTableLifecycle and is
# never called from the live turn path anymore (add_slot/end_active/end_cell
# in engine.py replaced it). Left commented rather than deleted because
# _configured_range below still hardcoded think-by-name, which is exactly the
# anti-pattern this codebase spent a whole session eliminating elsewhere —
# it must never be uncommented as-is.
#
# def _slot_is_active(slot: dict[str, Any], turn: int) -> bool:
#     ranges = slot.get("range")
#     if not isinstance(ranges, list):
#         return False
#     for entry in ranges:
#         try:
#             end = entry[0][1]
#         except (IndexError, TypeError):
#             continue
#         if end is None or int(end) > turn:
#             return True
#     return False
#
#
# def _rebuild_state(history: History, turn: int) -> State:
#     return {
#         turn_id: {
#             element: int(_slot_is_active(slot, turn))
#             for element, slot in content.items()
#         }
#         for turn_id, content in history.items()
#     }
#
#
# def _configured_range(
#     life_cycle: dict[str, Any],
#     element: str,
#     turn: int,
# ) -> list[list[Any]]:
#     """Build a slot range exclusively from the persisted life-cycle table."""
#     start, end = life_cycle.get(element, ["born", None])
#     start = turn if start == "born" else int(start)
#     end = turn if end == "born" else (None if end is None else int(end))
#     density = 0.0 if element == "think" else 1.0
#     compressor = "strip" if element == "think" else "none"
#     return [[[start, end], density, compressor]]
#
#
# def _patch_range(content_patch: ContentPatch) -> list[list[Any]]:
#     assert content_patch.created_turn is not None
#     return [
#         [
#             [content_patch.created_turn, content_patch.created_turn + content_patch.turns],
#             1.0,
#             "none",
#         ]
#     ]
#
#
# def _end_active_element(
#     history: History,
#     state: State,
#     *,
#     element: str,
#     turn: int,
# ) -> None:
#     for turn_id, content in history.items():
#         if int(turn_id) >= turn or element not in content:
#             continue
#         if state.get(turn_id, {}).get(element, 0) != 1:
#             continue
#         slot = content[element]
#         for entry in slot.get("range") or []:
#             try:
#                 end = entry[0][1]
#             except (IndexError, TypeError):
#                 continue
#             if end is None or int(end) > turn:
#                 entry[0][1] = turn
#         state.setdefault(turn_id, {})[element] = 0
#
#
# def _append_content_patch(
#     history: History,
#     state: State,
#     content_patch: ContentPatch,
# ) -> None:
#     if content_patch.created_turn is None:
#         raise ValueError("ContentPatch缺少created_turn")
#     turn_id = str(content_patch.created_turn)
#     history.setdefault(turn_id, {})[content_patch.element] = {
#         "content": content_patch.content,
#         "range": _patch_range(content_patch),
#     }
#     state.setdefault(turn_id, {})[content_patch.element] = 1


def _configured_patches(cfg: Any) -> list[ContentPatch]:
    values = ((cfg.to_dict().get("context") or {}).get("patches") or {})
    if not isinstance(values, dict):
        raise ValueError("context.patches必须是mapping")
    patches: list[ContentPatch] = []
    for patch_id, value in values.items():
        if not isinstance(value, dict):
            raise ValueError(f"context.patches.{patch_id}必须是mapping")
        patches.append(
            patch["create"](
                creator=value.get("creator", "user"),
                element=value["element"],
                content=value["content"],
                mode=value["mode"],
                turns=value["turns"],
                created_turn=value["created_turn"],
            )
        )
    return sorted(patches, key=lambda item: int(item.created_turn or 0))


def _patch_end_settings(cfg: Any) -> tuple[bool, str]:
    """Read the optional ending-message switch without making it mandatory."""

    context = (cfg.to_dict().get("context") or {})
    if not isinstance(context, dict):
        return False, ""
    raw = context.get("patch_end", False)
    if isinstance(raw, bool):
        if raw:
            raise ValueError(
                "context.patch_end 为 true 时必须写成 mapping 并声明 template"
            )
        return False, ""
    if not isinstance(raw, dict):
        raise ValueError("context.patch_end必须是bool或mapping")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("context.patch_end.enabled必须是bool")
    template = raw.get("template")
    if not enabled:
        return False, str(template or "")
    if not isinstance(template, str) or not template.strip():
        raise ValueError(
            "启用 context.patch_end 时必须声明 template：结束语是内容，属于配置"
        )
    try:
        template.format(element="goal", content="example")
    except (KeyError, ValueError) as exc:
        raise ValueError(
            "context.patch_end.template只支持{element}和{content}"
        ) from exc
    return enabled, template.strip()


def _range_end(slot: dict[str, Any]) -> int | None:
    ends: list[int] = []
    for entry in slot.get("range") or []:
        try:
            end = entry[0][1]
        except (IndexError, TypeError):
            continue
        if end is None:
            return None
        ends.append(int(end))
    return max(ends) if ends else None


def _range_active_at(slot: dict[str, Any], turn: int) -> bool:
    for entry in slot.get("range") or []:
        try:
            start, end = entry[0]
        except (IndexError, TypeError, ValueError):
            continue
        if int(start) <= turn and (end is None or int(end) >= turn):
            return True
    return False


def _source_owns_occurrence(source: ContentPatch, turn: int) -> bool:
    if source.created_turn is None:
        return False
    if source.mode == ContentPatchMode.REMAIN:
        return turn == source.created_turn
    return source.created_turn <= turn < source.created_turn + source.turns


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


def _last_summary_turn(history: History) -> int | None:
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
    history: History, state: State, end_turn: int, fields: tuple[str, ...]
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
    history: History, window: tuple[int, ...], fields: tuple[str, ...]
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


def _all_summary_anchors(history: History) -> list[int]:
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


def _active_summary_anchors(history: History, state: State) -> list[int]:
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


def _create_pin(runtime: "RuntimeComponents", content: str, *, origin: str) -> None:
    """/pin: the same small standalone-transaction pattern _maybe_compress
    uses — recompute from the just-saved tables, mutate, validate, save
    again. Attaches to the last committed turn rather than starting a new
    one; a pin is a note about what already happened, not a new exchange."""

    current_turn = _last_turn(runtime.tables.history)
    if current_turn < 1:
        print("[pin skipped: no committed turn to attach to yet]")
        return
    history = copy.deepcopy(runtime.tables.history)
    lifecycle: FixedTableLifecycle = manager["lifecycle.fixed"](
        history, runtime.tables.life_cycle, current_turn=current_turn
    )
    element = _pin_element_name(origin, _next_pin_id(history))
    lifecycle.add_slot(element, content, turn=current_turn)
    lifecycle.validate()
    state = lifecycle.state_snapshot()
    context = runtime.select_context(
        history, state, through_turn=current_turn, roles=runtime.element_roles
    )
    saver["history.save"](runtime.tables, history)
    saver["state.save"](runtime.tables, state)
    saver["context.save"](runtime.tables, context)
    print(f"[pin created: {element}] {content}")


def _cancel_pin(runtime: "RuntimeComponents", pin_id: str) -> None:
    current_turn = _last_turn(runtime.tables.history)
    found = _find_pin_element(runtime.tables.history, pin_id)
    if found is None:
        print(f"[cancelpin: no such pin {pin_id}]")
        return
    element, born_turn = found
    history = copy.deepcopy(runtime.tables.history)
    lifecycle: FixedTableLifecycle = manager["lifecycle.fixed"](
        history, runtime.tables.life_cycle, current_turn=current_turn
    )
    # turn=current_turn + 1: the pin was genuinely active through the turn
    # that already happened (and was shown then) — cancelling doesn't
    # rewrite that, it only stops the pin from here on.
    lifecycle.end_cell(element, born_turn, turn=current_turn + 1)
    lifecycle.validate()
    state = lifecycle.state_snapshot()
    context = runtime.select_context(
        history, state, through_turn=current_turn, roles=runtime.element_roles
    )
    saver["history.save"](runtime.tables, history)
    saver["state.save"](runtime.tables, state)
    saver["context.save"](runtime.tables, context)
    print(f"[pin cancelled: {element}]")


INSTRUCTION_SUFFIX = "_instruction"


def _output_instructions(cfg: Any) -> dict[str, str]:
    """Declared instructions telling the model how to produce an answer output.

    An output the parser can read is useless until the model knows it may
    emit it. The parser side and the instruction side are declared together
    under ``answer.outputs.<name>``, so an output never exists half-wired:
    ``role`` says how it is rendered back, ``instruction`` says how to ask
    for it in the first place.
    """

    data = cfg.to_dict()
    found: dict[str, str] = {}

    # Outputs the answer parser reads back out of the model's reply.
    answer = data.get("answer")
    outputs = (answer or {}).get("outputs") if isinstance(answer, dict) else None
    if isinstance(outputs, dict):
        for name, spec in outputs.items():
            if isinstance(spec, dict) and str(spec.get("instruction") or "").strip():
                found[str(name)] = str(spec["instruction"]).strip()

    # Channels that write a slot the model reads. Retrieved evidence needs
    # framing as much as a requested output does: without it the model has
    # no way to know a recalled line is a candidate rather than a fact, and
    # will bend an answer to fit whatever surfaced.
    recall_cfg = data.get("recall")
    if isinstance(recall_cfg, dict) and str(recall_cfg.get("instruction") or "").strip():
        found[RECALL_ELEMENT] = str(recall_cfg["instruction"]).strip()
    return found


def _restore_output_instructions(cfg: Any, tables: DatasetTables) -> None:
    """Materialize each declared output instruction as its own turn-0 slot.

    Kept out of ``runtime.system.content`` deliberately: the system prompt is
    the task author's own text, whereas these are the runtime's standing
    instructions about its own output channels. Separate slots keep both
    independently editable, separately visible in history, and each governed
    by its own life_cycle entry.
    """

    instructions = _output_instructions(cfg)
    if not instructions:
        return
    history = copy.deepcopy(tables.history)
    turn_zero = history.setdefault("0", {})
    pending = {
        f"{name}{INSTRUCTION_SUFFIX}": text
        for name, text in instructions.items()
        if turn_zero.get(f"{name}{INSTRUCTION_SUFFIX}", {}).get("content") != text
    }
    if not pending:
        return

    for element in pending:
        turn_zero.pop(element, None)
    lifecycle: FixedTableLifecycle = manager["lifecycle.fixed"](
        history, tables.life_cycle, current_turn=_last_turn(history)
    )
    for element, text in pending.items():
        lifecycle.add_slot(element, text, turn=0)
    lifecycle.validate()
    state = lifecycle.state_snapshot()
    saver["history.save"](tables, history)
    saver["state.save"](tables, state)


def _restore_committed_patches(cfg: Any, tables: DatasetTables) -> None:
    """Materialize YAML patches that belong to already committed turns."""

    latest = _last_turn(tables.history)
    history = copy.deepcopy(tables.history)
    scheduled: list[tuple[int, int, ContentPatch]] = []
    for source_index, source_patch in enumerate(_configured_patches(cfg)):
        born = int(source_patch.created_turn or 0)
        for turn in range(born, latest + 1):
            occurrence = patch["content.for_turn"](source_patch, turn)
            if occurrence is not None:
                scheduled.append((turn, source_index, occurrence))

    for _, _, content_patch in sorted(scheduled):
        patch_turn = int(content_patch.created_turn or 0)

        expected = {
            "content": content_patch.content,
            "range": [
                [[patch_turn, patch_turn + content_patch.turns - 1], 1.0, "none"]
            ],
        }
        existing = history.setdefault(str(patch_turn), {}).get(
            content_patch.element
        )
        if existing != expected:
            history[str(patch_turn)].pop(content_patch.element, None)

        lifecycle: FixedTableLifecycle = manager["lifecycle.fixed"](
            history,
            tables.life_cycle,
            current_turn=max(0, patch_turn - 1),
        )
        if content_patch.mode == ContentPatchMode.ROLL:
            lifecycle.end_active(content_patch.element, turn=patch_turn)
        if existing != expected:
            lifecycle.add_slot(
                content_patch.element,
                content_patch.content,
                turn=patch_turn,
                remain=content_patch.turns,
            )

    lifecycle = manager["lifecycle.fixed"](
        history,
        tables.life_cycle,
        current_turn=latest,
    )
    expected_state = lifecycle.state_snapshot()
    expected_context = select_context_without_extra_filter(
        history, expected_state, roles=_element_roles(cfg)
    )
    if history != tables.history:
        saver["history.save"](tables, history)
    if expected_state != tables.state:
        saver["state.save"](tables, expected_state)
    if expected_context != tables.context:
        saver["context.save"](tables, expected_context)


@manager("tables.initialize")
def initialize_tables(cfg: Any) -> DatasetTables:
    """Initialize or resume the four fixed tables, then repair their projection."""

    resumed: DatasetContext = dataset["context.resume"](cfg.source_path)
    tables = resumed.tables
    tables.history = dataset["history.initialize"](
        tables.paths.history,
        cfg.runtime.system.to_dict(),
    )
    if not tables.life_cycle:
        saver["life_cycle.save"](tables, cfg.life_cycle.to_dict())
    _restore_output_instructions(cfg, tables)
    _restore_committed_patches(cfg, tables)
    return tables


@dataclass
class TurnTransaction:
    runtime: "RuntimeComponents"
    turn_id: int
    user_input: str
    patches: list[ContentPatch]
    history: History
    lifecycle: FixedTableLifecycle
    state: State
    context: Context = field(default_factory=list)

    @classmethod
    def begin(
        cls,
        runtime: "RuntimeComponents",
        user_input: str,
    ) -> "TurnTransaction":
        turn_id = _last_turn(runtime.tables.history) + 1
        patches = runtime.patches_for_turn(turn_id)
        history = copy.deepcopy(runtime.tables.history)
        lifecycle: FixedTableLifecycle = manager["lifecycle.fixed"](
            history,
            runtime.tables.life_cycle,
            current_turn=turn_id - 1,
        )
        return cls(
            runtime=runtime,
            turn_id=turn_id,
            user_input=user_input,
            patches=patches,
            history=history,
            lifecycle=lifecycle,
            state=lifecycle.state_snapshot(),
        )

    def prepare_input(self) -> None:
        # Historical effects happen before expiration so a patch can change the
        # cells that would otherwise expire on this turn.
        for content_patch in self.patches:
            if content_patch.mode == ContentPatchMode.ROLL:
                self.lifecycle.end_active(content_patch.element, turn=self.turn_id)
            # A new value reopens this element, so an earlier ending no longer
            # applies even when the previous value used remain mode.
            self.lifecycle.end_active(
                f"{content_patch.element}_end",
                turn=self.turn_id,
            )

        endings = self.runtime.patch_endings_for_turn(
            self.turn_id,
            self.history,
        )

        self.lifecycle.expire_turn(self.turn_id)

        # ContentPatch creates a new cell and therefore belongs after expiry.
        for content_patch in self.patches:
            self.lifecycle.add_slot(
                content_patch.element,
                content_patch.content,
                turn=self.turn_id,
                remain=content_patch.turns,
            )

        replacement_elements = {item.element for item in self.patches}
        for element, content in endings.items():
            ending_element = f"{element}_end"
            if element in replacement_elements or ending_element in replacement_elements:
                continue
            self.lifecycle.end_active(ending_element, turn=self.turn_id)
            self.lifecycle.add_slot(
                ending_element,
                content,
                turn=self.turn_id,
                remain=1,
            )
            # In-place compression: everything this element governed folds
            # away now that it has ended; the element's own content (already
            # in history) stands in as the summary. A registered compressor,
            # like any other — not a bespoke call.
            born_turns = [
                int(turn_id) for turn_id, turn_content in self.history.items()
                if element in turn_content
            ]
            if born_turns:
                compact["goal_collapse"](
                    self.lifecycle,
                    goal_element=element,
                    goal_born_turn=min(born_turns),
                    goal_end_turn=self.turn_id,
                    collapse_elements=_compact_settings(self.runtime.cfg)[4],
                )

        self._append_element(INPUT_ELEMENT, self.user_input)
        self.state = self.lifecycle.state_snapshot()
        self.context = self.runtime.select_context(
            self.history,
            self.state,
            through_turn=self.turn_id,
            roles=self.runtime.element_roles,
        )
        # Whether to look anything up is its own decision, separate from how
        # the lookup works — a turn that merely continues the topic need not
        # pay for a search, while one that reaches backwards should.
        triggered = self.runtime.recall_trigger(
            query=self.user_input,
            turn_id=self.turn_id,
            history=self.history,
            state=self.state,
            **self.runtime.recall_trigger_params,
        )
        recalled = (
            self.runtime.recall(
                context=self.context,
                history=self.history,
                state=self.state,
                turn_id=self.turn_id,
                cfg=self.runtime.cfg,
                query_element=INPUT_ELEMENT,
                search_fields=self.runtime.recall_search_fields,
            )
            if triggered
            else None
        )
        # A lookup that runs invisibly cannot be judged. Report both halves —
        # whether the trigger fired and what the search actually returned —
        # the same way compaction and pinning announce themselves.
        if triggered:
            if recalled:
                print(f"[recall hit {len(recalled)}]")
                for line in recalled:
                    print(f"  · {line[:100]}")
            else:
                print("[recall fired, nothing matched]")
        # Recall is a writing channel like any other, not an injection that
        # bypasses the ledger: what it pulls back becomes a slot in history,
        # so it is auditable, it obeys a declared life_cycle, and the saved
        # context stays a faithful projection instead of quietly omitting
        # evidence the model actually saw.
        if recalled:
            self._append_element(RECALL_ELEMENT, "\n".join(recalled))
            self.context = self.runtime.select_context(
                self.history,
                self.state,
                through_turn=self.turn_id,
                roles=self.runtime.element_roles,
            )

    def call_and_parse(
        self,
        *,
        on_chunk: Callable[[str], Any] | None = None,
    ) -> ParsedAnswer:
        raw_answer = self.runtime.chat(
            self.runtime.provider,
            self.context,
            turn_id=self.turn_id,
            on_chunk=on_chunk,
            tools=self.runtime.tools,
            search=self.runtime.search,
        )
        # Before anything is split off: the parser's input is the one thing the
        # element table cannot hold, and the one thing a different parser would need.
        self.runtime.tables.append_raw(self.turn_id, raw_answer)
        parsed = self.runtime.answer_parser(raw_answer)
        for element, content in parsed.elements().items():
            self._append_element(element, content)
        for pin_text in parsed.pins:
            # Recomputed per pin, not once up front — add_slot mutates
            # self.history in place, so two pins in the same turn still get
            # distinct, sequential ids.
            pin_id = _next_pin_id(self.history)
            self._append_element(_pin_element_name("model", pin_id), pin_text)
        self.context = self.runtime.select_context(
            self.history,
            self.state,
            through_turn=self.turn_id,
            roles=self.runtime.element_roles,
        )
        return parsed

    def validate(self) -> None:
        self.lifecycle.validate()
        if self.state != self.lifecycle.state_snapshot():
            raise AssertionError("state_latest 与生命周期 CURRENT 不一致")
        if str(self.turn_id) not in self.history:
            raise AssertionError("当前轮没有写入history")
        turn_content = self.history[str(self.turn_id)]
        if not turn_content:
            raise AssertionError("当前轮没有写入任何槽位")
        # Which slots a complete turn must contain is declared, not assumed:
        # a config that renames or drops one of them is a different shape of
        # conversation, not a broken one.
        for element in self.runtime.required_turn_elements:
            if element not in turn_content:
                raise AssertionError(f"当前轮缺少{element}槽位")
        for turn_id, content in self.history.items():
            for element, slot in content.items():
                if not isinstance(slot.get("content"), str):
                    raise AssertionError(f"history[{turn_id}][{element}].content不是str")
                if not isinstance(slot.get("range"), list):
                    raise AssertionError(f"history[{turn_id}][{element}].range不是list")
                value = self.state.get(turn_id, {}).get(element)
                if value not in {0, 1}:
                    raise AssertionError(f"state[{turn_id}][{element}]不是0/1")
        for index, message in enumerate(self.context):
            if not isinstance(message.get("role"), str) or not isinstance(
                message.get("content"), str
            ):
                raise AssertionError(f"context[{index}]不是OpenAI消息")

    def commit(self) -> None:
        """Commit only after chat (including partial chat), parse, and validation."""

        saver["history.save"](self.runtime.tables, self.history)
        saver["state.save"](self.runtime.tables, self.state)
        saver["context.save"](self.runtime.tables, self.context)
        self.runtime.consume_patches(self.turn_id)

    # Dead code, kept as reference only — never called; superseded by
    # lifecycle.expire_turn()/state_snapshot() in engine.py.
    #
    # def _expire_current_turn(self) -> None:
    #     for turn_id, content in self.history.items():
    #         for element, slot in content.items():
    #             if self.state.get(turn_id, {}).get(element, 0) != 1:
    #                 continue
    #             if not _slot_is_active(slot, self.turn_id):
    #                 self.state.setdefault(turn_id, {})[element] = 0

    def _append_element(self, element: str, content: str) -> None:
        self.lifecycle.add_slot(
            element,
            content,
            turn=self.turn_id,
        )
        self.state = self.lifecycle.state_snapshot()


@dataclass
class RuntimeComponents:
    cfg: Any
    tables: DatasetTables
    input_data: DatasetInputProcessor
    provider: Any
    chat: Callable[..., str]
    answer_parser: Callable[[str], ParsedAnswer]
    recall: Callable[..., Any]
    select_context: Callable[..., Context]
    tools: Callable[..., Any]
    search: Callable[..., Any]
    printer: Callable[..., None]
    retain_summaries: Callable[..., set[int]] = retain_latest_only
    retention_params: dict[str, Any] = field(default_factory=dict)
    element_roles: dict[str, str] = field(default_factory=dict)
    required_turn_elements: tuple[str, ...] = ()
    recall_search_fields: tuple[str, ...] = ()
    recall_trigger: Callable[..., bool] = recall_registry["trigger.always"]
    recall_trigger_params: dict[str, Any] = field(default_factory=dict)
    queued_patches: list[ContentPatch] = field(default_factory=list)
    source_patches: list[ContentPatch] = field(default_factory=list)
    patch_end_enabled: bool = False
    patch_end_template: str = ""
    # Why the last turn produced nothing, for a UI that cannot see the console.
    last_error: str | None = None
    # Text generated so far this turn, and whether generation is still running
    # — the browser's equivalent of watching the stream scroll in a terminal.
    streaming: str = ""
    streaming_active: bool = False

    def queue_patch(self, content_patch: ContentPatch) -> None:
        if content_patch.created_turn is None:
            raise ValueError("ContentPatch缺少created_turn")
        self.queued_patches.append(content_patch)
        self.queued_patches.sort(key=lambda item: int(item.created_turn or 0))
        if content_patch not in self.source_patches:
            self.source_patches.append(content_patch)
            self.source_patches.sort(key=lambda item: int(item.created_turn or 0))

    def patch_endings_for_turn(
        self,
        turn_id: int,
        history: History,
    ) -> dict[str, str]:
        """Build one durable ending per element when its last cell closes."""

        if not self.patch_end_enabled:
            return {}
        candidates: dict[str, list[tuple[int, ContentPatch]]] = {}
        active_elements: set[str] = set()
        for source in self.source_patches:
            for occurrence_turn, turn_content in history.items():
                concrete_turn = int(occurrence_turn)
                if not _source_owns_occurrence(source, concrete_turn):
                    continue
                slot = turn_content.get(source.element)
                if (
                    slot is None
                    or str(slot.get("content") or "") != source.content
                ):
                    continue
                if _range_active_at(slot, turn_id):
                    active_elements.add(source.element)
                    continue
                end = _range_end(slot)
                # end is the closed-interval last-active turn, so the turn
                # right after it is exactly the closing turn — not "any turn
                # since it closed", otherwise, now that the ending message
                # itself expires after one turn (remain=1), the very next
                # turn would see it as no longer active and regenerate it
                # forever.
                if end is not None and end == turn_id - 1:
                    candidates.setdefault(source.element, []).append((end, source))

        endings: dict[str, str] = {}
        for element, values in candidates.items():
            if element in active_elements:
                continue
            ending_element = f"{element}_end"
            if any(
                ending_element in content
                and _range_active_at(content[ending_element], turn_id)
                for content in history.values()
            ):
                continue
            latest_end = max(end for end, _ in values)
            messages = [
                self.patch_end_template.format(
                    element=source.element,
                    content=source.content,
                )
                for end, source in values
                if end == latest_end
            ]
            endings[element] = "\n".join(dict.fromkeys(messages))
        return endings

    def patches_for_turn(self, turn_id: int) -> list[ContentPatch]:
        materialized = [
            patch["content.for_turn"](item, turn_id)
            for item in self.queued_patches
        ]
        return [item for item in materialized if item is not None]

    def consume_patches(self, turn_id: int) -> None:
        self.queued_patches = [
            item for item in self.queued_patches
            if patch["content.pending_after"](item, turn_id)
        ]

    def process_turn(
        self,
        user_input: str,
        *,
        on_chunk: Callable[[str], Any] | None = None,
    ) -> ParsedAnswer:
        # A range whose end could not be known at birth is settled before the
        # turn is built, by the same function the declaration named. Which
        # elements those are is in the declaration, not here.
        grouped = end_functions(self.cfg.to_dict().get("life_cycle") or {})
        if grouped:
            current_turn = _last_turn(self.tables.history)
            lifecycle: FixedTableLifecycle = manager["lifecycle.fixed"](
                self.tables.history, self.tables.life_cycle, current_turn=current_turn
            )
            for range_end_func, elements in grouped.items():
                process(
                    self.tables.history,
                    lifecycle,
                    range_end_func=range_end_func,
                    elements=elements,
                    turn=current_turn + 1,
                    source=self.input_data,
                )
        transaction = TurnTransaction.begin(self, user_input)
        transaction.prepare_input()
        parsed = transaction.call_and_parse(on_chunk=on_chunk)
        transaction.validate()
        transaction.commit()
        _maybe_compress(self)
        return parsed


def _choice(cfg: Any, section: str, key: str, default: str) -> str:
    section_value = cfg.to_dict().get(section) or {}
    if not isinstance(section_value, dict):
        return default
    return str(section_value.get(key) or default)


def _print_type(cfg: Any) -> str:
    runtime_cfg = cfg.to_dict().get("runtime") or {}
    if not isinstance(runtime_cfg, dict):
        return "none"
    print_cfg = runtime_cfg.get("print") or {}
    if isinstance(print_cfg, str):
        return print_cfg
    if not isinstance(print_cfg, dict):
        return "none"
    return str(print_cfg.get("type") or "none")


def _required_turn_elements(cfg: Any) -> tuple[str, ...]:
    """Which slots every committed turn must carry, from context.required_elements.

    Empty by default: a turn that wrote nothing at all is still rejected, but
    beyond that the runtime does not assume what a turn is made of."""

    context = cfg.to_dict().get("context")
    context = context if isinstance(context, dict) else {}
    declared = context.get("required_elements")
    if not isinstance(declared, list):
        return ()
    return tuple(str(item) for item in declared if item)


def _element_roles(cfg: Any) -> dict[str, str]:
    """Which chat role each element speaks in, gathered from config.

    Two declaration sites, because two kinds of element exist: whatever the
    answer parser produces declares its role next to itself under
    ``answer.outputs``, and everything else (input echo, patch elements,
    derived slots) is declared under ``context.roles``. context.roles wins
    on a clash, being the more specific, whole-table statement.
    """

    data = cfg.to_dict()
    roles: dict[str, str] = {}

    answer = data.get("answer")
    outputs = (answer or {}).get("outputs") if isinstance(answer, dict) else None
    if isinstance(outputs, dict):
        for element, spec in outputs.items():
            if isinstance(spec, dict) and spec.get("role"):
                roles[str(element)] = str(spec["role"])
            elif isinstance(spec, str):
                roles[str(element)] = spec

    context = data.get("context")
    declared = (context or {}).get("roles") if isinstance(context, dict) else None
    if isinstance(declared, dict):
        for element, role in declared.items():
            if role:
                roles[str(element)] = str(role)
    return roles


def _recall_search_fields(cfg: Any) -> tuple[str, ...]:
    """Which elements recall is allowed to search, from recall.search_fields.

    Required whenever recall is on, and for a concrete reason: recall only
    considers retired slots, so in a config where the conversational slots
    never retire, the only thing left to find is whatever expires every turn
    — the model's own discarded reasoning. Naming the searchable evidence is
    what keeps a lookup pointed at evidence."""

    section = cfg.to_dict().get("recall")
    section = section if isinstance(section, dict) else {}
    declared = section.get("search_fields")
    if not isinstance(declared, list) or not declared:
        raise ValueError(
            "启用 recall 时必须声明 recall.search_fields："
            "回忆只搜已退役的槽位，不指明搜哪些元素就只会捞到每轮自动退役的东西"
        )
    return tuple(str(item) for item in declared if item)


def _recall_trigger_settings(cfg: Any) -> tuple[str, dict[str, Any]]:
    """Read recall.trigger. Accepts a bare name ("always") or a mapping
    carrying the trigger's own parameters ({type: pattern, pattern: ...})."""

    section = cfg.to_dict().get("recall")
    section = section if isinstance(section, dict) else {}
    trigger = section.get("trigger")
    if isinstance(trigger, str):
        return trigger, {}
    if not isinstance(trigger, dict):
        return DEFAULT_RECALL_TRIGGER, {}
    name = str(trigger.get("type") or DEFAULT_RECALL_TRIGGER)
    return name, {key: value for key, value in trigger.items() if key != "type"}


def _require_recall_life_cycle(cfg: Any) -> None:
    """An undeclared element defaults to permanent, which is right for most
    slots and quietly wrong for recall: every turn's pulled-back evidence
    would stay visible forever and pile up. Rather than hardcode a different
    default for one element name, refuse to guess and make the config say
    how long recalled evidence lives."""

    if _choice(cfg, "recall", "type", "none") == "none":
        return
    life_cycle = cfg.to_dict().get("life_cycle")
    life_cycle = life_cycle if isinstance(life_cycle, dict) else {}
    _recall_search_fields(cfg)
    if RECALL_ELEMENT not in life_cycle:
        raise ValueError(
            f"启用 recall 时必须在 life_cycle 里声明 {RECALL_ELEMENT} 的生命周期，"
            f"例如 {RECALL_ELEMENT}: [born, born]（只在提问那一轮可见）"
        )


@manager("runtime.build")
def build_runtime(
    cfg: Any,
    *,
    tables: DatasetTables | None = None,
    input_data: DatasetInputProcessor | None = None,
    provider_instance: Any = None,
) -> RuntimeComponents:
    """Build every configured slot through the existing decorator registries."""

    tables = tables or manager["tables.initialize"](cfg)
    _require_recall_life_cycle(cfg)
    input_data = input_data or dataset["input.build"](cfg, tables=tables)
    provider_instance = provider_instance or build_provider_from_cfg(cfg)
    latest = _last_turn(tables.history)
    source_patches = _configured_patches(cfg)
    queued_patches = [
        item for item in source_patches
        if patch["content.pending_after"](item, latest)
    ]
    patch_end_enabled, patch_end_template = _patch_end_settings(cfg)
    retention_name, retention_params = _retention_settings(cfg)
    recall_trigger_name, recall_trigger_params = _recall_trigger_settings(cfg)
    return RuntimeComponents(
        cfg=cfg,
        tables=tables,
        input_data=input_data,
        provider=provider_instance,
        chat=provider_registry[f"chat.{_choice(cfg, 'chat', 'type', 'normal')}"],
        answer_parser=provider_registry[
            f"answer.parse.{_choice(cfg, 'answer', 'parser', 'normal')}"
        ],
        recall=recall_registry[_choice(cfg, "recall", "type", "none")],
        select_context=manager[
            f"context.select.{_choice(cfg, 'context', 'selector', 'none')}"
        ],
        tools=provider_registry[f"tools.{_choice(cfg, 'chat', 'tools', 'none')}"],
        search=provider_registry[f"search.{_choice(cfg, 'chat', 'search', 'none')}"],
        printer=saver[f"print.{_print_type(cfg)}"],
        retain_summaries=compact[f"retain.{retention_name}"],
        retention_params=retention_params,
        element_roles=_element_roles(cfg),
        required_turn_elements=_required_turn_elements(cfg),
        recall_search_fields=(
            _recall_search_fields(cfg)
            if _choice(cfg, "recall", "type", "none") != "none"
            else ()
        ),
        recall_trigger=recall_registry[f"trigger.{recall_trigger_name}"],
        recall_trigger_params=recall_trigger_params,
        queued_patches=queued_patches,
        source_patches=source_patches,
        patch_end_enabled=patch_end_enabled,
        patch_end_template=patch_end_template,
    )


def _viewer_settings(cfg: Any) -> tuple[bool, int, float, bool]:
    """Read runtime.viewer: a bare bool, or a mapping with port/interval/open.

    Off by default — a run that never asked to be watched must not bind a
    port or open a browser tab on its own.
    """

    runtime_cfg = cfg.to_dict().get("runtime") or {}
    if not isinstance(runtime_cfg, dict):
        return False, 8777, 1.0, True
    raw = runtime_cfg.get("viewer", False)
    if isinstance(raw, bool):
        return raw, 8777, 1.0, True
    if not isinstance(raw, dict):
        return False, 8777, 1.0, True
    enabled = bool(raw.get("enabled", True))
    port = int(raw.get("port") or 8777)
    interval = float(raw.get("interval") or 1.0)
    open_browser = bool(raw.get("open", True))
    return enabled, port, interval, open_browser


def _snapshot_every_turns(cfg: Any) -> int:
    runtime_cfg = cfg.to_dict().get("runtime") or {}
    if not isinstance(runtime_cfg, dict):
        return 0
    value = runtime_cfg.get("snapshot_every_turns") or 0
    return int(value) if isinstance(value, (int, float)) and value > 0 else 0


def _snapshot_tables(tables: DatasetTables, turn_id: int) -> None:
    """Copy the four table files into a numbered, never-overwritten subdir."""

    import shutil

    snapshot_dir = tables.paths.root / "snapshots" / f"turn_{turn_id:04d}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for source in (
        tables.paths.history,
        tables.paths.state,
        tables.paths.context,
        tables.paths.life_cycle,
    ):
        if source.exists():
            shutil.copy2(source, snapshot_dir / source.name)
    print(f"[snapshot saved: {snapshot_dir}]")


def _model_status(runtime: "RuntimeComponents") -> Callable[[], dict]:
    """What model is answering right now, and what else could.

    ``vendors`` is PROVIDER_DEFAULTS — the vendors the provider registry can
    actually build, with their default model/base_url — so the page offers
    exactly what `python3 -m code.provider init` would, no separate list to
    keep in sync. ``saved`` is the config-provider files already on disk, so
    a key entered once never has to be typed again.
    """

    def status() -> dict:
        from ..provider.provider import CONFIG_DIR, PROVIDER_DEFAULTS

        config = getattr(runtime.provider, "config", None)
        saved = (
            sorted(path.name for path in CONFIG_DIR.glob("*.json"))
            if CONFIG_DIR.is_dir()
            else []
        )
        return {
            "current": {
                "provider": getattr(config, "provider", ""),
                "model": getattr(config, "model", ""),
                "base_url": getattr(config, "base_url", ""),
                "has_api_key": bool(getattr(config, "api_key", "")),
            },
            "vendors": PROVIDER_DEFAULTS,
            "saved": saved,
            # Which models are actually installed, so the picker offers real
            # choices instead of one free-text box. Only Ollama can answer
            # this without credentials, so only Ollama is enumerated; the
            # hosted vendors keep their default as a starting point and stay
            # type-in, since their catalogue needs a key to list.
            "installed": _installed_ollama_models(
                getattr(config, "base_url", "") or PROVIDER_DEFAULTS["ollama"]["base_url"]
            ),
        }

    return status


def _write_provider_to_yaml(cfg: Any, *, vendor: str, config_name: str) -> str:
    """Point the task YAML's ``provider`` block at the newly chosen model.

    This project's rule (README: "终端输入会先写回实验 yaml……运行期间只有
    一份生效配置") is that anything which changes configuration lands in the
    YAML, not only in memory — otherwise the file on disk quietly disagrees
    with what is running, and a restart silently reverts the change. A model
    picked in the browser is exactly such a change, so it is written back the
    same way terminal overrides and ContentPatches already are
    (_append_terminal_input, patch["yaml.write"]).

    Only the two scalars under ``provider:`` are rewritten, in place —
    comments, anchors and every other block keep their exact text, which a
    parse-and-redump would destroy.
    """

    from pathlib import Path

    path = Path(cfg.source_path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    inside = False
    wrote_type = wrote_config = False
    for line in lines:
        stripped = line.strip()
        if not inside:
            if re.match(r"^provider\s*:\s*$", line.rstrip("\n")):
                inside = True
            out.append(line)
            continue
        # The block ends at the first line that is neither indented nor blank.
        if stripped and not line[:1].isspace():
            if not wrote_type:
                out.append(f"  type: {vendor}\n")
            if not wrote_config:
                out.append(f"  config: {config_name}\n")
            inside = False
            out.append(line)
            continue
        if re.match(r"^\s+type\s*:", line):
            out.append(f"  type: {vendor}\n")
            wrote_type = True
            continue
        if re.match(r"^\s+config\s*:", line):
            out.append(f"  config: {config_name}\n")
            wrote_config = True
            continue
        out.append(line)
    if inside:  # provider block ran to end of file
        if not wrote_type:
            out.append(f"  type: {vendor}\n")
        if not wrote_config:
            out.append(f"  config: {config_name}\n")
    path.write_text("".join(out), encoding="utf-8")
    return str(path)


def _installed_ollama_models(base_url: str) -> list[str]:
    """Ask a local Ollama what it has pulled. Best-effort: an unreachable or
    slow daemon yields an empty list rather than blocking the page."""

    import json as _json
    import urllib.error
    import urllib.request

    from ..provider.provider import PROVIDER_DEFAULTS, _urlopen

    root = (base_url or PROVIDER_DEFAULTS["ollama"]["base_url"]).rstrip("/")
    if not root:
        return []
    try:
        request = urllib.request.Request(f"{root}/api/tags", method="GET")
        with _urlopen(request, timeout=2) as response:
            payload = _json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, RuntimeError):
        return []
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    return sorted(
        str(entry.get("name"))
        for entry in models
        if isinstance(entry, dict) and entry.get("name")
    )


def _model_switch(runtime: "RuntimeComponents") -> Callable[[dict], dict]:
    """Swap the answering model mid-session, optionally saving the config.

    Only ``runtime.provider`` changes — the ledger, the life_cycle rules and
    every committed turn stay exactly as they are, so a conversation can
    continue across a model change rather than starting over. That attribute
    was already swappable (runtime.py's own self-test replaces it to simulate
    a failing provider); this just exposes it, with a real construction and a
    key check up front so a bad switch fails here instead of mid-turn.

    ``save_as`` (a bare filename) additionally persists the config into
    config-provider/ through the same save_provider_config the CLI uses —
    that, plus a first switch on a machine with no config at all, is what
    makes this the first-run setup path too.
    """

    def switch(payload: dict) -> dict:
        from ..provider.provider import (
            PROVIDER_DEFAULTS,
            ProviderConfig,
            build_provider,
            load_provider_config,
            save_provider_config,
        )

        # Reusing a saved config: name it and nothing else is required.
        saved_name = str(payload.get("use_saved") or "").strip()
        if saved_name:
            config = load_provider_config(saved_name)
        else:
            vendor = str(payload.get("provider") or "").strip().lower()
            if vendor not in PROVIDER_DEFAULTS:
                raise ValueError(
                    f"unsupported provider {vendor!r}；可选：{', '.join(PROVIDER_DEFAULTS)}"
                )
            defaults = PROVIDER_DEFAULTS[vendor]
            config = ProviderConfig.from_dict(
                {
                    "provider": vendor,
                    "model": str(payload.get("model") or "").strip() or defaults["model"],
                    "base_url": str(payload.get("base_url") or "").strip() or defaults["base_url"],
                    "api_key": str(payload.get("api_key") or ""),
                    "timeout": payload.get("timeout") or 300,
                }
            )

        instance = build_provider(config)
        runtime.provider = instance

        # Persist, then point the YAML at what was persisted. A switch that
        # only lived in memory would make the running session disagree with
        # its own config file and revert on restart — see
        # _write_provider_to_yaml for why that is not acceptable here.
        config_name = saved_name or str(payload.get("save_as") or "").strip()
        if not config_name:
            # B-01: name it after this task, not the vendor. A per-vendor file
            # is shared by every task using that vendor, so switching a model
            # here would rewrite what another running session is pointing at.
            config_name = f"{runtime.tables.paths.root.name}.json"
        if not config_name.endswith(".json"):
            config_name += ".json"
        saved_to = str(save_provider_config(config, config_name))
        yaml_path = _write_provider_to_yaml(
            runtime.cfg, vendor=config.provider, config_name=config_name
        )
        print(f"[model switched: {config.provider} / {config.model} → {config_name}]")
        return {
            "ok": True,
            "provider": config.provider,
            "model": config.model,
            "base_url": config.base_url,
            "saved_to": saved_to,
            "config_name": config_name,
            "yaml": yaml_path,
        }

    return switch


def _recall_preview(runtime: "RuntimeComponents") -> Callable[[str], list[str]]:
    """IF-14: recall["grep"] reads its query from a history slot, not a bare
    string (see code/recall/grep_recall.py) — so an ad-hoc preview query is
    written into a scratch copy of history, one turn past the real last turn,
    never touching runtime.tables or persisting anything. Bypasses the
    trigger entirely, on purpose: this previews what recall *would* find, not
    whether it would have fired."""

    def preview(query: str) -> list[str]:
        current_turn = _last_turn(runtime.tables.history)
        preview_turn = current_turn + 1
        scratch_history = dict(runtime.tables.history)
        scratch_history[str(preview_turn)] = {INPUT_ELEMENT: {"content": query}}
        return recall_registry["grep"](
            history=scratch_history,
            state=runtime.tables.state,
            turn_id=preview_turn,
            cfg=runtime.cfg,
            query_element=INPUT_ELEMENT,
            search_fields=runtime.recall_search_fields,
        )

    return preview


def _compress_status(runtime: "RuntimeComponents") -> Callable[[], dict]:
    """IF-15: thin read-only wrapper around the same due-checks and window
    lookups `_maybe_compress` already uses — never triggers anything."""

    def status() -> dict:
        if not _compact_enabled(runtime.cfg):
            return {"enabled": False}
        current_turn = _last_turn(runtime.tables.history)
        overload_threshold, _prompt, interval_turns, keep_recent_turns, fields = _compact_settings(
            runtime.cfg
        )
        last_summary_turn = _last_summary_turn(runtime.tables.history)
        periodic_runtime = ProcessorRuntime(last_run_turn=last_summary_turn)
        due_periodic = is_due_periodic(
            current_turn, periodic_runtime, step=interval_turns, first_run_turn=interval_turns
        )
        context_size = context_byte_size(runtime.tables.context)
        due_overload = (
            is_due_overload(context_size, threshold_bytes=overload_threshold)
            if overload_threshold > 0
            else False
        )
        end_turn = current_turn - keep_recent_turns
        pending_from_turn = (
            _first_uncompressed_turn(runtime.tables.history, runtime.tables.state, end_turn, fields)
            if end_turn >= 1
            else None
        )
        return {
            "enabled": True,
            "current_turn": current_turn,
            "last_summary_turn": last_summary_turn,
            "interval_turns": interval_turns,
            "keep_recent_turns": keep_recent_turns,
            "due_periodic": due_periodic,
            "due_overload": due_overload,
            "context_bytes": context_size,
            "overload_threshold_bytes": overload_threshold,
            "pending_from_turn": pending_from_turn,
            "active_summary_anchors": _active_summary_anchors(runtime.tables.history, runtime.tables.state),
            "all_summary_anchors": _all_summary_anchors(runtime.tables.history),
        }

    return status


@manager("runtime.run")
def run_runtime(runtime: RuntimeComponents) -> None:
    """Run configured input until EOF/close; provider generation is always streamed."""

    show_stream = bool(runtime.cfg.runtime.stream)
    snapshot_every_turns = _snapshot_every_turns(runtime.cfg)
    viewer_enabled, viewer_port, viewer_interval, viewer_open = _viewer_settings(runtime.cfg)
    if viewer_enabled:
        # push_user only feeds a turn loop that is actually reading from that
        # queue — the terminal interface never looks at it. Handing the page
        # a live send box when nothing would ever consume what it sends is
        # worse than not offering one, so it stays read-only until the task
        # says its turns come from there.
        interface = (runtime.cfg.to_dict().get("input_data") or {}).get("interface")
        if interface == "gui":
            viewer_push = runtime.input_data.push_user
        else:
            viewer_push = None
            print(
                "[viewer] input_data.interface 不是 gui，网页里的输入框不会生效——"
                "要用网页发消息，把 input_data.interface 改成 gui"
            )
        session_id: str | None = None
        try:
            saver["viewer.serve"](
                runtime.tables.paths.root,
                # The directory, not cfg.name: two tasks copied from the
                # same template share a `name:` field, and the registry then
                # lists both under it. The directory is what actually
                # distinguishes one run's ledger from another's.
                label=runtime.tables.paths.root.name,
                port=viewer_port,
                interval=viewer_interval,
                open_browser=viewer_open,
                push=viewer_push,
                recall_preview=_recall_preview(runtime),
                compress_status=_compress_status(runtime),
                model_status=_model_status(runtime),
                model_switch=_model_switch(runtime),
                last_error=lambda: runtime.last_error,
                streaming=lambda: {
                    "text": runtime.streaming,
                    "active": runtime.streaming_active,
                },
            )
        except Exception as exc:  # noqa: BLE001 - a busy port must never stop a run
            print(f"[viewer skipped: {exc}]")
        else:
            # IF-17: register this session so the hub (IF-19) can list it.
            # Best-effort like the viewer itself — a registry write failure
            # must not stop a run that can already serve its own viewer fine.
            try:
                session_id = saver["session.register"](
                    task=runtime.tables.paths.root.name,
                    task_dir=runtime.tables.paths.root,
                    port=viewer_port,
                    label=runtime.tables.paths.root.name,
                )
                saver["hub.serve"]()
            except Exception as exc:  # noqa: BLE001
                print(f"[session registry skipped: {exc}]")
    else:
        session_id = None
    try:
        while True:
            try:
                item = runtime.input_data.next()
            except KeyboardInterrupt:
                print()
                break
            except ValueError as exc:
                print(f"[input error] {exc}")
                continue
            if item is None:
                break
            if item == MANUAL_COMPRESS_COMMAND:
                _maybe_compress(runtime, manual_requested=True)
                continue
            if item == MANUAL_CHECK_COMMAND:
                # One-off status dump on request, independent of the configured
                # print type — normal conversation stays quiet by default; this
                # is the escape hatch instead of switching the whole session to
                # per-turn debug printing.
                saver["print.tables"](
                    history=runtime.tables.history,
                    state=runtime.tables.state,
                    context=runtime.tables.context,
                    turn_id=_last_turn(runtime.tables.history),
                )
                continue
            if isinstance(item, PinCommand):
                _create_pin(runtime, item.content, origin="user")
                continue
            if isinstance(item, CancelPinCommand):
                _cancel_pin(runtime, item.pin_id)
                continue
            if isinstance(item, DialogueInput):
                for content_patch in item.patches:
                    runtime.queue_patch(content_patch)
                    print(
                        f"[patch queued: turn={content_patch.created_turn} "
                        f"element={content_patch.element} "
                        f"mode={content_patch.mode.value}]"
                    )
                item = item.user
            if isinstance(item, ContentPatch):
                runtime.queue_patch(item)
                print(
                    f"[patch queued: turn={item.created_turn} "
                    f"element={item.element} mode={item.mode.value}]"
                )
                continue

            # The terminal has always watched generation arrive chunk by
            # chunk; the browser saw nothing at all until the turn committed,
            # which on a local model is tens of seconds of a page that looks
            # frozen. Same callback, one more consumer: accumulate into a
            # buffer the page polls (GET /streaming).
            runtime.streaming = ""
            runtime.streaming_active = True

            def on_chunk(chunk: str) -> None:
                runtime.streaming += chunk
                if show_stream:
                    print(chunk, end="", flush=True)

            try:
                parsed = runtime.process_turn(item, on_chunk=on_chunk)
            except Exception as exc:  # noqa: BLE001
                # A provider that refuses (bad key, unreachable endpoint, a
                # model switched to something that isn't running) must not
                # take the whole session down: the turn is already guaranteed
                # uncommitted, so the ledger is intact and the next input can
                # be answered by a working provider. Before the web UI this
                # killed the process, which a terminal user could see and
                # restart — a browser user just gets a dead page instead.
                runtime.last_error = f"{type(exc).__name__}: {exc}"
                runtime.streaming_active = False
                print(f"\n[turn failed, not committed] {runtime.last_error}")
                continue
            runtime.last_error = None
            runtime.streaming_active = False
            if show_stream:
                print()
            else:
                print(parsed.assistant)
            current_turn = _last_turn(runtime.tables.history)
            runtime.printer(
                history=runtime.tables.history,
                state=runtime.tables.state,
                context=runtime.tables.context,
                turn_id=current_turn,
            )
            if snapshot_every_turns and current_turn % snapshot_every_turns == 0:
                _snapshot_tables(runtime.tables, current_turn)
    finally:
        # IF-17: every exit path (EOF, Ctrl-C, an uncaught exception) drops
        # this session from the registry so the hub never shows a ghost.
        if session_id is not None:
            try:
                saver["session.deregister"](session_id)
            except Exception:  # noqa: BLE001 - shutdown must never fail on this
                pass


def _self_test() -> None:
    import json
    from pathlib import Path
    import tempfile

    from ..dataset.load_config import DEFAULT_CONFIG_PATH, load_config

    class StubProvider:
        """Deterministic offline answer, for tests only.

        This used to be a registered vendor ("dry-run") any YAML could
        select, which meant a real session could silently be answering with
        fabricated text. It is gone from the registry; what the tests
        actually need is not a vendor but a predictable reply, so the stub
        lives here and is injected through build_runtime(provider_instance=…)
        — no config can reach it, and the suite still runs offline and fast
        instead of depending on a model being pulled.
        """

        def chat(self, context: Any, *, turn_id: int | str, **_: Any) -> str:
            return f"<think>{turn_id}</think> id：{turn_id}"

        def chat_stream(self, context: Any, *, turn_id: int | str, **_: Any):
            yield self.chat(context, turn_id=turn_id)

    def build_stub_runtime(cfg: Any, **kwargs: Any) -> "RuntimeComponents":
        kwargs.setdefault("provider_instance", StubProvider())
        return build_runtime(cfg, **kwargs)

    class InterruptedProvider:
        def chat_stream(self, *_args: Any, **_kwargs: Any):
            yield "<think>partial"
            raise KeyboardInterrupt

    class FailedProvider:
        def chat_stream(self, *_args: Any, **_kwargs: Any):
            raise RuntimeError("provider failed")
            yield  # pragma: no cover - keep this function a generator

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config_path = root / "runtime-test.yaml"
        config_path.write_text(
            DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
            .replace("path: ../../task/standard", "path: task", 1)
            .replace("history: history.jsonl", "history: configured-history.jsonl", 1)
            .replace("interface: input", "interface: gui", 1),
            encoding="utf-8",
        )
        cfg = load_config(config_path)
        runtime = build_stub_runtime(cfg)
        assert runtime.tables.paths.history.name == "configured-history.jsonl"
        assert runtime.tables.paths.history.is_file()
        assert not (runtime.tables.paths.root / "history.jsonl").exists()
        assert runtime.tables.state == {"0": {"system": 1}}

        runtime.input_data.push_user("/goal ship runtime remain 5")
        content_patch = runtime.input_data.next()
        assert isinstance(content_patch, ContentPatch)
        runtime.queue_patch(content_patch)
        stub_provider = runtime.provider
        runtime.provider = FailedProvider()
        try:
            runtime.process_turn("not committed")
        except RuntimeError:
            pass
        else:
            raise AssertionError("provider失败后本轮没有回滚")
        assert "1" not in runtime.tables.history
        assert runtime.patches_for_turn(1) == [content_patch]
        runtime.provider = stub_provider
        parsed = runtime.process_turn("hello")
        assert parsed.think == "1"
        assert parsed.assistant == "id：1"
        assert list(runtime.tables.history["1"]) == [
            "goal", "user", "think", "assistant"
        ]
        assert runtime.tables.state["1"] == {
            "goal": 1,
            "user": 1,
            "think": 1,
            "assistant": 1,
        }
        assert runtime.tables.context[-1] == {
            "role": "assistant",
            "content": "id：1",
        }

        resumed_cfg = load_config(config_path)
        resumed = build_stub_runtime(resumed_cfg)
        assert resumed.tables.history == runtime.tables.history
        assert resumed.tables.state == runtime.tables.state
        resumed.input_data.push_user("/goal replace runtime roll 2")
        roll_patch = resumed.input_data.next()
        assert isinstance(roll_patch, ContentPatch)
        resumed.queue_patch(roll_patch)
        resumed.process_turn("second")
        assert resumed.tables.history["1"]["goal"]["range"][0][0][1] == 1
        assert resumed.tables.history["2"]["goal"]["content"] == "replace runtime"
        assert resumed.tables.history["2"]["goal"]["range"][0][0][1] == 2
        resumed.process_turn("third")
        assert resumed.tables.state["1"]["goal"] == 0
        assert resumed.tables.state["2"]["goal"] == 0
        assert resumed.tables.history["3"]["goal"]["content"] == "replace runtime"
        assert resumed.tables.history["3"]["goal"]["range"][0][0][1] == 3
        assert resumed.tables.state["3"]["goal"] == 1

        resumed = build_stub_runtime(load_config(config_path))
        assert resumed.tables.history["1"]["goal"]["range"][0][0][1] == 1
        assert resumed.tables.history["2"]["goal"]["content"] == "replace runtime"
        assert resumed.tables.history["3"]["goal"]["content"] == "replace runtime"

        resumed.provider = InterruptedProvider()
        interrupted_turn = resumed.process_turn("stop now")
        assert interrupted_turn.think == "partial"
        assert interrupted_turn.assistant == ""
        assert resumed.tables.history["4"]["think"]["content"] == "partial"
        assert resumed.tables.history["4"]["assistant"]["content"] == ""
        assert resumed.tables.state["4"]["think"] == 1
        assert resumed.tables.history["4"]["goal_end"]["content"] == (
            "以上goal已经结束，不再继续执行：replace runtime。"
            "请按当前用户请求正常回答。"
        )
        assert resumed.tables.state["4"]["goal_end"] == 1
        assert {
            "role": "system",
            "content": (
                "[goal_end]\n以上goal已经结束，不再继续执行："
                "replace runtime。请按当前用户请求正常回答。"
            ),
        } in resumed.tables.context

        # Regression: goal_end now expires after one turn (remain=1). Before
        # the end==turn_id fix, patch_endings_for_turn kept treating the
        # closed goal as a fresh candidate every turn after that, since "no
        # longer active" looked identical to "never announced" — recreating
        # goal_end forever. Two more turns must not add a second one.
        resumed.provider = stub_provider
        resumed.process_turn("still going")
        assert "goal_end" not in resumed.tables.history["5"]
        assert resumed.tables.state["4"]["goal_end"] == 0  # itself expired
        resumed.process_turn("and again")
        assert "goal_end" not in resumed.tables.history["6"]

    # Periodic + manual summary compression: history-synced insert+retire.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config_path = root / "compress-test.yaml"
        config_path.write_text(
            DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
            .replace("path: ../../task/standard", "path: task", 1)
            .replace("history: history.jsonl", "history: configured-history.jsonl", 1)
            .replace("interface: input", "interface: gui", 1)
            .replace("interval_turns: 20", "interval_turns: 4", 1)
            .replace("keep_recent_turns: 10", "keep_recent_turns: 1", 1),
            encoding="utf-8",
        )
        cfg = load_config(config_path)
        runtime = build_stub_runtime(cfg)

        runtime.process_turn("turn one")
        runtime.process_turn("turn two")
        runtime.process_turn("turn three")
        assert _last_summary_turn(runtime.tables.history) is None
        # current_turn=4: periodic due (step=4); end_turn=4-1=3, window=1..3.
        # The summary replaces turn 3's own position — no new turn number.
        runtime.process_turn("turn four")
        assert _last_summary_turn(runtime.tables.history) == 3
        assert runtime.tables.state["1"]["user"] == 0  # folded into the summary
        assert runtime.tables.state["1"]["assistant"] == 0
        assert runtime.tables.state["2"]["user"] == 0
        assert runtime.tables.state["3"]["user"] == 0  # end_turn itself is folded too
        assert runtime.tables.state["3"]["3_summary"] == 1
        assert runtime.tables.state["4"]["user"] == 1  # keep_recent_turns=1
        assert runtime.tables.history["3"]["3_summary"]["content"]

        runtime.process_turn("turn five")
        runtime.process_turn("turn six")
        assert _last_summary_turn(runtime.tables.history) == 3  # not due yet (next due at 3+4=7)
        _maybe_compress(runtime, manual_requested=True)
        assert _last_summary_turn(runtime.tables.history) == 5
        assert runtime.tables.state["3"]["3_summary"] == 0  # superseded, cascades
        assert runtime.tables.state["4"]["user"] == 0  # newly folded in
        assert runtime.tables.state["5"]["user"] == 0  # end_turn itself is folded too
        assert runtime.tables.state["5"]["5_summary"] == 1
        assert runtime.tables.state["6"]["user"] == 1  # current keep-recent turn, untouched

    # compact.fields in YAML, not a code constant: an element left out of
    # fields is never folded, even though it would otherwise qualify (same
    # element, same turns) — this is how pin stays untouched without any
    # pin-specific exemption code, and how any future element type opts
    # in/out purely through config.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config_path = root / "compress-fields-test.yaml"
        config_path.write_text(
            DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
            .replace("path: ../../task/standard", "path: task", 1)
            .replace("history: history.jsonl", "history: configured-history.jsonl", 1)
            .replace("interface: input", "interface: gui", 1)
            .replace("interval_turns: 20", "interval_turns: 4", 1)
            .replace("keep_recent_turns: 10", "keep_recent_turns: 1", 1)
            .replace(
                "  fields: [user, assistant, think]",
                "  fields: [assistant]",
                1,
            ),
            encoding="utf-8",
        )
        cfg = load_config(config_path)
        assert cfg.to_dict()["compact"]["fields"] == ["assistant"]
        runtime = build_stub_runtime(cfg)
        for index in range(4):
            runtime.process_turn(f"turn {index}")
        assert _last_summary_turn(runtime.tables.history) == 3
        assert runtime.tables.state["1"]["assistant"] == 0  # folded — in fields
        # user has permanent [born, null] life_cycle, so unlike think (which
        # expires on its own after one turn regardless of compaction) this
        # can only stay active because fields left it out of compaction.
        assert runtime.tables.state["1"]["user"] == 1

    # compact: none (or an absent section) turns the whole mechanism off —
    # same convention as chat.tools/chat.search/recall.type. No summary ever
    # gets written, even with keep_recent_turns effectively 0 turns of slack
    # and a manual /compat request.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config_path = root / "compress-disabled-test.yaml"
        default_text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
        start = default_text.index("compact:\n")
        end = default_text.index("\ndataset:\n", start) + 1
        compact_block = default_text[start:end]
        assert "compressors:" in compact_block
        config_path.write_text(
            default_text.replace(compact_block, "compact: none\n", 1)
            .replace("path: ../../task/standard", "path: task", 1)
            .replace("history: history.jsonl", "history: configured-history.jsonl", 1)
            .replace("interface: input", "interface: gui", 1),
            encoding="utf-8",
        )
        cfg = load_config(config_path)
        runtime = build_stub_runtime(cfg)
        for index in range(6):
            runtime.process_turn(f"turn {index}")
        assert _last_summary_turn(runtime.tables.history) is None
        _maybe_compress(runtime, manual_requested=True)
        assert _last_summary_turn(runtime.tables.history) is None

    # In-place goal collapse: everything the goal governed folds away when it
    # ends; the goal's own content is untouched (it is the summary).
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config_path = root / "collapse-test.yaml"
        config_path.write_text(
            DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
            .replace("path: ../../task/standard", "path: task", 1)
            .replace("history: history.jsonl", "history: configured-history.jsonl", 1)
            .replace("interface: input", "interface: gui", 1),
            encoding="utf-8",
        )
        cfg = load_config(config_path)
        runtime = build_stub_runtime(cfg)
        runtime.input_data.push_user("/goal ship feature remain 2")
        goal_patch = runtime.input_data.next()
        assert isinstance(goal_patch, ContentPatch)
        runtime.queue_patch(goal_patch)

        runtime.process_turn("first under goal")
        runtime.process_turn("second under goal")
        assert runtime.tables.state["1"]["user"] == 1  # still governed, still active
        runtime.process_turn("after goal ends")  # goal's [1,3) expires this turn
        assert runtime.tables.history["3"]["goal_end"]["content"]
        assert runtime.tables.state["1"]["user"] == 0
        assert runtime.tables.state["1"]["assistant"] == 0
        assert runtime.tables.state["2"]["user"] == 0
        assert runtime.tables.state["2"]["assistant"] == 0
        assert runtime.tables.state["3"]["user"] == 1  # this turn's own, untouched
        assert runtime.tables.history["1"]["goal"]["content"] == "ship feature"

    # /check: an on-demand status dump, independent of the configured print
    # type — normal conversation (print type absent -> "none") stays quiet
    # every turn; /check prints once, on request, without switching the
    # whole session into per-turn debug printing.
    with tempfile.TemporaryDirectory() as temporary:
        import contextlib
        import io

        root = Path(temporary)
        config_path = root / "check-test.yaml"
        config_path.write_text(
            DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
            .replace("path: ../../task/standard", "path: task", 1)
            .replace("history: history.jsonl", "history: configured-history.jsonl", 1)
            .replace("interface: input", "interface: gui", 1)
            .replace("type: tables", "type: none", 1),  # normal, quiet conversation
            encoding="utf-8",
        )
        cfg = load_config(config_path)
        runtime = build_stub_runtime(cfg)
        runtime.input_data.push_user("hello")
        runtime.input_data.push_user("/check")
        runtime.input_data.close()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            run_runtime(runtime)
        rendered = output.getvalue()
        assert rendered.count("· CONTEXT") == 1
        assert "TURN 1 · CONTEXT" in rendered

    # /pin and /cancelpin: user-authored pins attach to the last committed
    # turn (not a new one), get the next global id regardless of origin,
    # and /cancelpin only needs the bare id — no need to know it was
    # user- vs model-authored.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config_path = root / "user-pin-test.yaml"
        config_path.write_text(
            DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
            .replace("path: ../../task/standard", "path: task", 1)
            .replace("history: history.jsonl", "history: configured-history.jsonl", 1)
            .replace("interface: input", "interface: gui", 1),
            encoding="utf-8",
        )
        cfg = load_config(config_path)
        runtime = build_stub_runtime(cfg)
        runtime.input_data.push_user("/pin 现在还没有轮次")
        no_turn_pin = runtime.input_data.next()
        assert isinstance(no_turn_pin, PinCommand)
        _create_pin(runtime, no_turn_pin.content, origin="user")  # no committed turn yet: no-op
        assert not any("pin_" in element for content in runtime.tables.history.values() for element in content)

        runtime.process_turn("first turn")
        runtime.input_data.push_user("/pin 示例事实一")
        pin_item = runtime.input_data.next()
        assert isinstance(pin_item, PinCommand)
        _create_pin(runtime, pin_item.content, origin="user")
        assert runtime.tables.history["1"]["pin_user_p01"]["content"] == "示例事实一"
        assert runtime.tables.state["1"]["pin_user_p01"] == 1

        runtime.input_data.push_user("/cancelpin p01")
        cancel_item = runtime.input_data.next()
        assert isinstance(cancel_item, CancelPinCommand)
        _cancel_pin(runtime, cancel_item.pin_id)
        assert runtime.tables.state["1"]["pin_user_p01"] == 0

        _cancel_pin(runtime, "p01")  # already cancelled: harmless no-op
        assert runtime.tables.state["1"]["pin_user_p01"] == 0

        _create_pin(runtime, "第二次手动pin", origin="user")
        assert runtime.tables.history["1"]["pin_user_p02"]["content"] == "第二次手动pin"

    # Model-origin pins: parsed like think, but each gets its own permanent
    # element (pin_model_pNN) instead of overwriting a fixed-name slot, ids
    # never repeat, and pins survive compaction untouched because they were
    # never in compact.fields to begin with — no pin-specific exemption.
    class PinningProvider:
        def __init__(self, replies: list[str]) -> None:
            self._replies = list(replies)

        def chat_stream(self, *_args: Any, **_kwargs: Any):
            # Falls back to a plain reply once the queue drains — compaction
            # makes its own chat() call for the summary, which would
            # otherwise silently consume a reply meant for a real turn.
            yield self._replies.pop(0) if self._replies else "no pins"

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config_path = root / "pin-test.yaml"
        config_path.write_text(
            DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
            .replace("path: ../../task/standard", "path: task", 1)
            .replace("history: history.jsonl", "history: configured-history.jsonl", 1)
            .replace("interface: input", "interface: gui", 1)
            .replace("interval_turns: 20", "interval_turns: 2", 1)
            .replace("keep_recent_turns: 10", "keep_recent_turns: 1", 1),
            encoding="utf-8",
        )
        cfg = load_config(config_path)
        runtime = build_stub_runtime(cfg)
        runtime.provider = PinningProvider(
            [
                "<think>noting</think>"
                "<pin>2026-07-20 家里 妈妈说示例事实一</pin>"
                "好的，记住了。"
                "<pin>2026-07-20 家里 晚饭吃了面条</pin>",
                "no pins this turn",
                "no pins this turn either",
            ]
        )
        parsed_one = runtime.process_turn("first turn")
        assert parsed_one.pins == (
            "2026-07-20 家里 妈妈说示例事实一",
            "2026-07-20 家里 晚饭吃了面条",
        )
        assert runtime.tables.history["1"]["pin_model_p01"]["content"] == (
            "2026-07-20 家里 妈妈说示例事实一"
        )
        assert runtime.tables.history["1"]["pin_model_p02"]["content"] == (
            "2026-07-20 家里 晚饭吃了面条"
        )
        assert runtime.tables.state["1"]["pin_model_p01"] == 1
        assert runtime.tables.state["1"]["pin_model_p02"] == 1

        runtime.process_turn("second turn")
        runtime.process_turn("third turn")  # interval_turns=2 -> compaction fires
        assert _last_summary_turn(runtime.tables.history) is not None
        assert runtime.tables.state["1"]["user"] == 0  # folded, ordinary field
        # pins were never in compact.fields, so compaction never touched them.
        assert runtime.tables.state["1"]["pin_model_p01"] == 1
        assert runtime.tables.state["1"]["pin_model_p02"] == 1
        assert any(
            "示例事实一" in message.get("content", "") for message in runtime.tables.context
        )

        runtime.provider = PinningProvider(["<pin>2026-07-21 学校 老师说下周春游</pin>好的"])
        runtime.process_turn("fourth turn")
        assert runtime.tables.history["4"]["pin_model_p03"]["content"] == (
            "2026-07-21 学校 老师说下周春游"
        )

    partial = provider_registry["chat.normal"](
        InterruptedProvider(), [], turn_id=1
    )
    assert partial == "<think>partial"
    parsed_partial = provider_registry["answer.parse.normal"](partial)
    assert parsed_partial.think == "partial"
    assert parsed_partial.assistant == ""


if __name__ == "__main__":
    _self_test()
    print("manager.runtime: ok")

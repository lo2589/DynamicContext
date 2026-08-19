"""Hide four input sources behind one ``next() -> user`` interface."""

from __future__ import annotations

import json
import re
import tempfile
import threading
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from .load_config import Config, ConfigFieldError
from .table_manager import RowId
from ..patch.state_patch import ContentPatch, Patch
from ..registry import dataset, patch as patch_registry


InputValues = Union[Sequence[Any], Mapping[RowId, Any]]

MANUAL_COMPRESS_COMMAND = "/compat"
MANUAL_CHECK_COMMAND = "/check"

# /pin and /cancelpin carry a payload, so unlike /compat and /check they
# can't be bare string sentinels — but they still can't go through the
# "/element content mode turns" ContentPatch grammar (no mode/turns), so
# they need their own pattern and return types.
PIN_COMMAND_PATTERN = re.compile(r"^/pin\s+(?P<content>.+)$")
CANCEL_PIN_COMMAND_PATTERN = re.compile(r"^/cancelpin\s+(?P<pin_id>\S+)$")


@dataclass(frozen=True)
class InputCell:
    passed: bool
    value: Any = None


@dataclass(frozen=True)
class TurnInput:
    values: InputValues
    patches: Sequence[Patch] = field(default_factory=tuple)


StreamItem = Union[InputValues, TurnInput]


@dataclass(frozen=True)
class DialogueInput:
    user: str
    patches: tuple[ContentPatch, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PinCommand:
    content: str


@dataclass(frozen=True)
class CancelPinCommand:
    pin_id: str


def parse_pin_input(text: str) -> Optional[Union[PinCommand, CancelPinCommand]]:
    """Recognize /pin and /cancelpin as their own command shape — neither
    fits the "/element content mode turns" ContentPatch grammar (no
    mode/turns), so they get their own standalone parser instead of an
    inline special case in next()."""

    stripped = text.strip()
    pin_match = PIN_COMMAND_PATTERN.match(stripped)
    if pin_match:
        return PinCommand(content=pin_match.group("content").strip())
    cancel_match = CANCEL_PIN_COMMAND_PATTERN.match(stripped)
    if cancel_match:
        return CancelPinCommand(pin_id=cancel_match.group("pin_id").strip())
    return None


class InputDataError(ValueError):
    """Invalid JSON record or live user input."""


class DatasetInputProcessor:
    """Return the next user without exposing the configured input source."""

    USER_ONLY_JSON = "user_only_json"
    USER_ANSWER_JSON = "user_answer_json"
    REAL_USER = "real_user"
    JSON_THEN_USER = "json_then_user"

    def __init__(
        self,
        cfg: Config,
        *,
        input_type: Optional[str] = None,
        json_path: Optional[Union[str, Path]] = None,
        interface: Optional[str] = None,
        tables: Any = None,
    ) -> None:
        self.cfg = cfg
        self.input_type = (
            input_type
            if input_type is not None
            else cfg.input_data.type
        )
        self.json_path = (
            json_path
            if json_path is not None
            else cfg.input_data.path
        )
        self.interface = interface if interface is not None else cfg.input_data.interface
        self.tables = tables
        self._json_records: Optional[list[Any]] = None
        self._json_index = 0
        self._json_index_restored = False
        self._user_answer_done = False
        self._live_users: deque[str] = deque()
        self._live_closed = False
        self._live_condition = threading.Condition()
        try:
            interface_function = dataset[f"input.interface.{self.interface}"]
            self._user_input: Callable[[], Optional[str]] = interface_function.__get__(
                self, type(self)
            )
        except KeyError as exc:
            raise ConfigFieldError(
                self.cfg.source_path,
                "input_data.interface",
                f"未知用户输入接口 {self.interface!r}",
            ) from exc
        try:
            next_function = dataset[f"input.type.{self.input_type}"]
            self._next_user: Callable[[], Optional[str]] = next_function.__get__(
                self, type(self)
            )
        except KeyError as exc:
            raise ConfigFieldError(
                self.cfg.source_path,
                "input_data.type",
                f"未知输入类型 {self.input_type!r}",
            ) from exc

    def push_user(self, user_input: str) -> None:
        """Put one UI/CLI user message into the live-input queue."""
        with self._live_condition:
            self._live_users.append(self._required_user_input(user_input))
            self._live_condition.notify()

    def close(self) -> None:
        """Close a live-input interface and release a blocked ``next()``."""
        with self._live_condition:
            self._live_closed = True
            self._live_condition.notify_all()

    def next(
        self,
    ) -> Optional[Union[str, ContentPatch, DialogueInput, PinCommand, CancelPinCommand]]:
        """Return user text, a patch command, or one combined dialogue turn."""
        user_input = self._next_user()
        if user_input is None:
            return None
        if user_input.strip() == MANUAL_COMPRESS_COMMAND:
            # A bare compression request, not a "/element content mode turns"
            # patch — must not go through patch parsing, which would reject it.
            return MANUAL_COMPRESS_COMMAND
        if user_input.strip() == MANUAL_CHECK_COMMAND:
            # Same reasoning as /compat: a bare status-table request, not a
            # patch command.
            return MANUAL_CHECK_COMMAND
        pin_command = parse_pin_input(user_input)
        if pin_command is not None:
            return pin_command
        user_text, content_patch = patch_registry["input.extract"](user_input)
        if content_patch is None:
            return user_text
        if self.tables is None:
            stored_patch = content_patch
        else:
            turns = [int(turn_id) for turn_id in self.tables.history]
            stored_patch = patch_registry["yaml.write"](
                self.cfg,
                content_patch,
                created_turn=max(turns, default=0) + 1,
            )
        if user_text is None:
            return stored_patch
        return DialogueInput(user=user_text, patches=(stored_patch,))

    @dataset("input.type.user_only_json")
    def _next_user_only_json(self) -> Optional[str]:
        records = self._records()
        if not self._json_index_restored:
            self._json_index = self._resume_user_only_index(records)
            self._json_index_restored = True
        if self._json_index >= len(records):
            return None
        index = self._json_index
        self._json_index += 1
        return self._user_only_text(records[index], index)

    @dataset("input.type.user_answer_json")
    def _next_user_answer_json(self) -> Optional[str]:
        if self._user_answer_done:
            return None
        self._user_answer_done = True
        return self._resume_user_from_history(self._records())

    @dataset("input.type.real_user")
    def _next_real_user(self) -> Optional[str]:
        return self._user_input()

    @dataset("input.type.json_then_user")
    def _next_json_then_user(self) -> Optional[str]:
        json_user = self._next_user_only_json()
        if json_user is not None:
            return json_user
        return self._next_real_user()

    def _records(self) -> list[Any]:
        if self._json_records is None:
            self._json_records = self._read_json_array()
        return self._json_records

    def _read_json_array(self) -> list[Any]:
        """Read a prompt set from either a JSON array or a JSONL file.

        JSONL is how every other ledger in this project is stored
        (history.jsonl, patches.jsonl, raw_history.jsonl), so a prompt set
        exported from one of them arrives one-record-per-line and used to be
        rejected for not having a top-level array. Both shapes describe the
        same list of records, and which one a file happens to use is not a
        difference the input types should care about; the suffix decides.
        """

        path = self._resolved_json_path()
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".jsonl":
            records: list[Any] = []
            for number, line in enumerate(text.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise InputDataError(f"JSONL 格式错误: {path}:{number}") from exc
            return records
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InputDataError(f"JSON 格式错误: {path}:{exc.lineno}") from exc
        if not isinstance(data, list):
            raise InputDataError(f"JSON 顶层必须是数组: {path}")
        return data

    def _resolved_json_path(self) -> Path:
        if not isinstance(self.json_path, (str, Path)) or not str(self.json_path).strip():
            raise ConfigFieldError(
                self.cfg.source_path,
                "input_data.path",
                "JSON 输入类型必须提供文件路径",
            )
        configured = Path(self.json_path).expanduser()
        path = (
            configured.resolve()
            if configured.is_absolute()
            else (self.cfg.source_path.parent / configured).resolve()
        )
        if not path.is_file():
            raise ConfigFieldError(
                self.cfg.source_path,
                "input_data.path",
                f"文件不存在: {path}",
            )
        return path

    @staticmethod
    def _user_only_text(record: Any, index: int) -> str:
        if isinstance(record, str):
            text = record.strip()
        elif isinstance(record, dict) and isinstance(record.get("user"), str):
            text = record["user"].strip()
        else:
            raise InputDataError(f"JSON[{index}] 必须是字符串或含 user 的对象")
        if not text:
            raise InputDataError(f"JSON[{index}].user 不能为空")
        return text

    @dataset("input.linked_trap")
    def linked_trap(self, turn: int) -> Optional[int]:
        """The earlier turn this one points back at, or None if it points at
        nothing.

        A record carries whatever fields its dataset wants; the two this
        runtime reads are ``user`` and ``linked_trap``. What the link means
        is the dataset's business — the runtime only learns that answering
        this turn concerns that one.
        """

        records = self._records()
        index = turn - 1
        if index < 0 or index >= len(records):
            return None
        record = records[index]
        if not isinstance(record, dict):
            return None
        linked = record.get("linked_trap")
        if linked is None:
            return None
        if isinstance(linked, bool) or not isinstance(linked, int):
            raise InputDataError(f"JSON[{index}].linked_trap 必须是整数轮号")
        return linked

    @dataset("input.turn_type")
    def turn_type(self, turn: int) -> Optional[str]:
        """What the record for ``turn`` calls itself, or None if it says nothing.

        A dataset may label its records however it likes; the runtime never
        learns what a label means, only that two turns carry different ones.
        A rule can then treat one label differently from another without any
        element name being hardcoded anywhere.
        """

        records = self._records()
        index = turn - 1
        if index < 0 or index >= len(records):
            return None
        record = records[index]
        if not isinstance(record, dict):
            return None
        declared = record.get("type")
        if declared is None:
            return None
        if not isinstance(declared, str):
            raise InputDataError(f"JSON[{index}].type 必须是字符串")
        return declared

    @staticmethod
    def _required_text(record: dict[str, Any], key: str, index: int) -> str:
        value = record.get(key)
        if not isinstance(value, str) or not value.strip():
            raise InputDataError(f"JSON[{index}].{key} 必须是非空字符串")
        return value.strip()

    def _resume_user_from_history(self, records: list[Any]) -> Optional[str]:
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise InputDataError(f"JSON[{index}] 必须是 user/answer 对象")
            user = self._required_text(record, "user", index)
            answer = record.get("answer")
            answer_is_empty = answer is None or (
                isinstance(answer, str) and not answer.strip()
            )
            if answer_is_empty:
                return user
            self._required_text(record, "answer", index)
        return None

    def _resume_user_only_index(self, records: list[Any]) -> int:
        """Return the first dataset prompt not already consumed in history.

        History is append-only and may contain a partial repeated pass after a
        process restart. Match prompt text instead of using ``len(history)`` so
        a history such as ``u1, u2, u3, u1, u2`` still resumes at ``u4``.
        Duplicate prompts in the source remain safe because Counter preserves
        multiplicity.
        """

        if self.tables is None:
            return 0
        history = getattr(self.tables, "history", None)
        if not isinstance(history, Mapping):
            return 0

        consumed: Counter[str] = Counter()
        for turn in history.values():
            if not isinstance(turn, Mapping):
                continue
            user = turn.get("user")
            if not isinstance(user, Mapping):
                continue
            content = user.get("content")
            if isinstance(content, str) and content.strip():
                consumed[content.strip()] += 1

        index = 0
        for source_index, record in enumerate(records):
            text = self._user_only_text(record, source_index)
            if consumed[text] <= 0:
                break
            consumed[text] -= 1
            index += 1
        return index

    @staticmethod
    def _required_user_input(user_input: Optional[str]) -> str:
        if not isinstance(user_input, str) or not user_input.strip():
            raise InputDataError("真实用户输入不能为空")
        return user_input.strip()

    @dataset("input.interface.input")
    def _input_user(self) -> Optional[str]:
        try:
            return self._required_user_input(input("你> "))
        except EOFError:
            return None

    @dataset("input.interface.gui")
    def _gui_user(self) -> Optional[str]:
        with self._live_condition:
            while not self._live_users and not self._live_closed:
                self._live_condition.wait()
            if self._live_users:
                return self._live_users.popleft()
            return None


@dataset("input.build")
def build_input_data(cfg: Config, *, tables: Any = None) -> DatasetInputProcessor:
    """Decorated construction entry retained even when the source is simple."""

    return DatasetInputProcessor(cfg, tables=tables)


def _self_test() -> None:
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        user_only_path = root / "user_only.json"
        user_only_path.write_text(
            json.dumps(["u1", {"user": "u2"}], ensure_ascii=False),
            encoding="utf-8",
        )
        resume_path = root / "resume_user_only.json"
        resume_path.write_text(
            json.dumps(["u1", "u2", "u3", "u4"], ensure_ascii=False),
            encoding="utf-8",
        )
        duplicate_path = root / "duplicate_user_only.json"
        duplicate_path.write_text(
            json.dumps(["same", "same", "last"], ensure_ascii=False),
            encoding="utf-8",
        )
        jsonl_path = root / "prompts.jsonl"
        jsonl_path.write_text(
            '{"user": "j1"}\n\n{"user": "j2"}\n', encoding="utf-8"
        )
        user_answer_path = root / "user_answer.json"
        user_answer_path.write_text(
            json.dumps(
                [
                    {"user": "u1", "answer": "a1"},
                    {"user": "u2", "answer": "a2"},
                    {"user": "last question", "answer": ""},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        cfg = Config.from_mapping(
            {
                "input_data": {
                    "type": "real_user",
                    "path": None,
                    "interface": "gui",
                },
            },
            source_path=root / "task.yaml",
        )

        user_only = DatasetInputProcessor(
            cfg,
            input_type="user_only_json",
            json_path=user_only_path,
        )
        assert user_only.next() == "u1"
        assert user_only.next() == "u2"
        assert user_only.next() is None

        repeated_history = SimpleNamespace(
            history={
                "0": {"system": {"content": "system"}},
                "1": {"user": {"content": "u1"}},
                "2": {"user": {"content": "u2"}},
                "3": {"user": {"content": "u3"}},
                "4": {"user": {"content": "u1"}},
                "5": {"user": {"content": "u2"}},
            }
        )
        resumed_user_only = DatasetInputProcessor(
            cfg,
            input_type="user_only_json",
            json_path=resume_path,
            tables=repeated_history,
        )
        assert resumed_user_only.next() == "u4"
        assert resumed_user_only.next() is None

        gap_history = SimpleNamespace(
            history={
                "1": {"user": {"content": "u1"}},
                "2": {"user": {"content": "u3"}},
            }
        )
        gap_resume = DatasetInputProcessor(
            cfg,
            input_type="user_only_json",
            json_path=resume_path,
            tables=gap_history,
        )
        assert gap_resume.next() == "u2"

        duplicate_history = SimpleNamespace(
            history={"1": {"user": {"content": "same"}}}
        )
        duplicate_resume = DatasetInputProcessor(
            cfg,
            input_type="user_only_json",
            json_path=duplicate_path,
            tables=duplicate_history,
        )
        assert duplicate_resume.next() == "same"
        assert duplicate_resume.next() == "last"
        assert duplicate_resume.next() is None

        jsonl_only = DatasetInputProcessor(
            cfg, input_type="user_only_json", json_path=jsonl_path
        )
        assert jsonl_only.next() == "j1"
        assert jsonl_only.next() == "j2"
        assert jsonl_only.next() is None

        user_answer = DatasetInputProcessor(
            cfg,
            input_type="user_answer_json",
            json_path=user_answer_path,
        )
        assert user_answer.next() == "last question"
        assert user_answer.next() is None

        real_user = DatasetInputProcessor(cfg)
        real_user.push_user("live")
        assert real_user.next() == "live"
        real_user.push_user("/goal write essay roll 5")
        patch_item = real_user.next()
        assert isinstance(patch_item, ContentPatch)
        assert patch_item.element == "goal"
        assert patch_item.turns == 5
        real_user.push_user("今天天气不好 /goal 翻译文本 remain 5")
        inline_item = real_user.next()
        assert isinstance(inline_item, DialogueInput)
        assert inline_item.user == "今天天气不好"
        assert inline_item.patches[0].content == "翻译文本"
        real_user.push_user("/compat")
        assert real_user.next() == MANUAL_COMPRESS_COMMAND
        real_user.push_user(" /compat ")  # surrounding whitespace still matches
        assert real_user.next() == MANUAL_COMPRESS_COMMAND
        real_user.push_user("/check")
        assert real_user.next() == MANUAL_CHECK_COMMAND
        real_user.push_user(" /check ")
        assert real_user.next() == MANUAL_CHECK_COMMAND
        real_user.push_user("/pin 示例事实一")
        pinned = real_user.next()
        assert isinstance(pinned, PinCommand)
        assert pinned.content == "示例事实一"
        real_user.push_user("/cancelpin p01")
        cancelled = real_user.next()
        assert isinstance(cancelled, CancelPinCommand)
        assert cancelled.pin_id == "p01"
        real_user.close()
        assert real_user.next() is None

        mixed = DatasetInputProcessor(
            cfg,
            input_type="json_then_user",
            json_path=user_only_path,
            interface="gui",
        )
        mixed.push_user("live")
        assert mixed.next() == "u1"
        assert mixed.next() == "u2"
        assert mixed.next() == "live"
        mixed.close()
        assert mixed.next() is None


if __name__ == "__main__":
    _self_test()
    print("dataset.input_data: ok")

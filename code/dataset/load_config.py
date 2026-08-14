# load_cfg：必须给 YAML 或输出目录；无 YAML 就复制默认 YAML；终端输入先追加到 YAML 尾部，再从该 YAML 启动。
"""Load the task YAML without deciding any runtime behavior."""

from __future__ import annotations

import ast
import argparse
import copy
import json
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

from ..registry import dataset


DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "standard"
    / "runtime.yaml"
)

class ConfigError(ValueError):
    """YAML syntax or config field error."""


class ConfigFieldError(ConfigError):
    """A config error that points back to one YAML field."""

    def __init__(self, source_path: Path, field_path: str, reason: str) -> None:
        self.source_path = source_path
        self.field_path = field_path
        self.reason = reason
        super().__init__(f"{source_path}: {field_path}: {reason}")


class ConfigNode:
    """Read-only config object with recursive ``cfg.section.field`` access."""

    def __init__(
        self,
        data: Mapping[str, Any],
        *,
        source_path: Path,
        field_path: str = "",
    ) -> None:
        self._data = dict(data)
        self._source_path = source_path
        self._field_path = field_path

    @property
    def source_path(self) -> Path:
        return self._source_path

    @property
    def field_path(self) -> str:
        return self._field_path

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._data:
            raise ConfigFieldError(
                self.source_path,
                self._child_path(name),
                "字段不存在",
            )
        return self._wrap(self._data[name], self._child_path(name))

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def _child_path(self, key: str) -> str:
        return f"{self.field_path}.{key}" if self.field_path else key

    def _wrap(self, value: Any, path: str) -> Any:
        if isinstance(value, Mapping):
            return ConfigNode(value, source_path=self.source_path, field_path=path)
        if isinstance(value, list):
            return [
                self._wrap(item, f"{path}[{index}]")
                for index, item in enumerate(value)
            ]
        return value


class Config(ConfigNode):
    """Root YAML config object."""

    def __init__(self, data: Mapping[str, Any], source_path: Union[str, Path]) -> None:
        super().__init__(data, source_path=Path(source_path).expanduser().resolve())

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        source_path: Union[str, Path],
    ) -> "Config":
        return cls(data, source_path)


def parse(argv: Any = None) -> argparse.Namespace:
    """Require one YAML source or one output directory for a copied YAML."""

    parser = argparse.ArgumentParser(description="Load simple_chat_runtime YAML.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--config",
        default=None,
        help="existing experiment YAML",
    )
    source.add_argument(
        "--output-dir",
        default=None,
        help="create/reuse an experiment directory containing the default YAML",
    )
    parser.add_argument(
        "--input-type",
        choices=(
            "user_only_json",
            "user_answer_json",
            "real_user",
            "json_then_user",
        ),
        default=None,
        help="override cfg.input_data.type",
    )
    parser.add_argument(
        "--input-path",
        default=None,
        help="override cfg.input_data.path",
    )
    parser.add_argument(
        "--dataset-path",
        default=None,
        help="override cfg.dataset.kwargs.path",
    )
    return parser.parse_args(argv)


@dataset("config.load_runtime")
def load_cfg(argv: Any = None) -> Config:
    """Materialize terminal input in YAML, then load that YAML as the sole config."""

    terminal = parse(argv)
    yaml_path, copied_default = _resolve_yaml_path(terminal)
    yaml_cfg = load_config(yaml_path)
    terminal_overrides = _terminal_overrides(terminal, copied_default)
    effective = _merge_mapping(yaml_cfg.to_dict(), terminal_overrides)
    _append_terminal_input(
        yaml_path,
        terminal,
        effective,
        rewrites_dataset=bool(terminal_overrides),
        copied_default=copied_default,
    )
    return load_config(yaml_path)


def _resolve_yaml_path(terminal: argparse.Namespace) -> tuple[Path, bool]:
    if terminal.config is not None:
        return Path(terminal.config).expanduser().resolve(), False

    output_dir = Path(terminal.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = output_dir / DEFAULT_CONFIG_PATH.name
    if yaml_path.exists():
        return yaml_path, False
    shutil.copy2(DEFAULT_CONFIG_PATH, yaml_path)
    return yaml_path, True


def _terminal_overrides(
    terminal: argparse.Namespace,
    copied_default: bool,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if terminal.input_type is not None:
        _set_path(overrides, ("input_data", "type"), terminal.input_type)
    if terminal.input_path is not None:
        _set_path(overrides, ("input_data", "path"), terminal.input_path)
    if terminal.dataset_path is not None:
        _set_path(overrides, ("dataset", "kwargs", "path"), terminal.dataset_path)
    elif copied_default:
        _set_path(overrides, ("dataset", "kwargs", "path"), ".")
    return overrides


def _append_terminal_input(
    yaml_path: Path,
    terminal: argparse.Namespace,
    effective: Mapping[str, Any],
    *,
    rewrites_dataset: bool,
    copied_default: bool,
) -> None:
    lines = ["", "# terminalinput"]
    if terminal.config is not None:
        lines.append(f"# --config 是 {terminal.config!r}")
    if terminal.output_dir is not None:
        lines.append(f"# --output-dir 是 {terminal.output_dir!r}")
    if terminal.input_type is not None:
        lines.append(f"# --input-type 是 {terminal.input_type!r}")
    if terminal.input_path is not None:
        lines.append(f"# --input-path 是 {terminal.input_path!r}")
    if terminal.dataset_path is not None:
        lines.append(f"# --dataset-path 是 {terminal.dataset_path!r}")
    if copied_default and terminal.dataset_path is None:
        lines.append("# 新实验默认在输出目录写数据，因此 cfg.dataset.kwargs.path 是 '.'")

    if rewrites_dataset:
        lines.append("# 上方配置是默认设置；terminalinput 生效后，以以下配置为准。")
        input_data = effective.get("input_data")
        if not isinstance(input_data, Mapping):
            raise ConfigError("cfg.input_data 必须是 mapping")
        dataset = effective.get("dataset")
        if not isinstance(dataset, Mapping):
            raise ConfigError("cfg.dataset 必须是 mapping")
        lines.extend(_dump_yaml_mapping({"input_data": input_data, "dataset": dataset}))

    with yaml_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")


def _dump_yaml_mapping(value: Mapping[str, Any], indent: int = 0) -> list[str]:
    lines: list[str] = []
    prefix = " " * indent
    for key, item in value.items():
        if isinstance(item, Mapping):
            lines.append(f"{prefix}{key}:")
            lines.extend(_dump_yaml_mapping(item, indent + 2))
        else:
            lines.append(f"{prefix}{key}: {_yaml_scalar(item)}")
    return lines


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_yaml_scalar(item) for item in value) + "]"
    raise ConfigError(f"无法写入 YAML 的值: {value!r}")


def _set_path(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = target
    for key in path[:-1]:
        child = current.setdefault(key, {})
        if not isinstance(child, dict):
            raise ConfigError(f"terminal override path conflict: {'.'.join(path)}")
        current = child
    current[path[-1]] = value


def _merge_mapping(
    lower_authority: Mapping[str, Any],
    higher_authority: Mapping[str, Any],
) -> dict[str, Any]:
    merged = copy.deepcopy(dict(lower_authority))
    for key, value in higher_authority.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, Mapping)
        ):
            merged[key] = _merge_mapping(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


@dataclass(frozen=True)
class _Token:
    indent: int
    text: str
    line: int


@dataset("config.load")
def load_config(path: Union[str, Path]) -> Config:
    """Load one YAML config and return one dot-accessible config object.

    PyYAML is used when installed.  The standard-library fallback supports the
    subset used by the standard config: indented maps/lists, flow lists,
    anchors, aliases, and merge keys.
    """

    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"config does not exist: {config_path}")
    text = config_path.read_text(encoding="utf-8")

    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        try:
            data = _parse_yaml_subset(text)
        except ConfigError as exc:
            raise ConfigError(f"{config_path}: {exc}") from exc
    else:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"{config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping: {config_path}")
    _resolve_name_placeholder(data, config_path)
    name = data.get("name")
    if isinstance(name, str):
        data = _substitute_name_template(data, name)
    return Config(data, config_path)


NAME_PLACEHOLDER = "<YamlName>"


def _resolve_name_placeholder(data: dict[str, Any], config_path: Path) -> None:
    """A YAML that names itself NAME_PLACEHOLDER gets its own filename (no
    extension) as `name`, so a copied task config defaults to a dataset path
    that follows the file instead of silently pointing at the original's."""

    if data.get("name") == NAME_PLACEHOLDER:
        data["name"] = config_path.stem


def _substitute_name_template(value: Any, name: str) -> Any:
    if isinstance(value, str):
        return value.replace("${name}", name)
    if isinstance(value, dict):
        return {key: _substitute_name_template(item, name) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute_name_template(item, name) for item in value]
    return value


def _parse_yaml_subset(text: str) -> dict[str, Any]:
    tokens = _tokenize(text)
    if not tokens:
        return {}
    anchors: dict[str, Any] = {}
    value, index = _parse_block(tokens, 0, tokens[0].indent, anchors)
    if index != len(tokens):
        token = tokens[index]
        raise ConfigError(f"line {token.line}: unexpected indentation")
    if not isinstance(value, dict):
        raise ConfigError("config root must be a mapping")
    return value


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ConfigError(f"line {line_number}: tabs are not valid indentation")
        clean = _strip_comment(raw).rstrip()
        if not clean.strip():
            continue
        indent = len(clean) - len(clean.lstrip(" "))
        tokens.append(_Token(indent, clean.strip(), line_number))
    return tokens


def _strip_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    depth = 0
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quote and char == "\\":
            escaped = True
            continue
        if char in {"'", '"'}:
            quote = None if quote == char else (char if quote is None else quote)
            continue
        if quote is None:
            if char in "[{(":
                depth += 1
            elif char in "]})":
                depth -= 1
            elif char == "#" and depth == 0:
                return line[:index]
    return line


def _parse_block(
    tokens: list[_Token],
    index: int,
    indent: int,
    anchors: dict[str, Any],
) -> tuple[Any, int]:
    if tokens[index].indent != indent:
        raise ConfigError(f"line {tokens[index].line}: unexpected indentation")
    if tokens[index].text.startswith("- ") or tokens[index].text == "-":
        return _parse_list(tokens, index, indent, anchors)
    return _parse_mapping(tokens, index, indent, anchors)


def _parse_mapping(
    tokens: list[_Token],
    index: int,
    indent: int,
    anchors: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    while index < len(tokens):
        token = tokens[index]
        if token.indent < indent:
            break
        if token.indent != indent or token.text.startswith("-"):
            break
        key, raw_value = _split_pair(token)
        index += 1
        value, index, anchor = _parse_entry_value(
            tokens, index, indent, raw_value, anchors, token.line
        )
        if key == "<<":
            if not isinstance(value, dict):
                raise ConfigError(f"line {token.line}: merge value must be a mapping")
            out.update(copy.deepcopy(value))
        else:
            out[key] = value
        if anchor:
            anchors[anchor] = copy.deepcopy(value)
    return out, index


def _parse_list(
    tokens: list[_Token],
    index: int,
    indent: int,
    anchors: dict[str, Any],
) -> tuple[list[Any], int]:
    out: list[Any] = []
    while index < len(tokens):
        token = tokens[index]
        if token.indent < indent:
            break
        if token.indent != indent or not token.text.startswith("-"):
            break
        body = token.text[1:].strip()
        index += 1
        if not body:
            if index >= len(tokens) or tokens[index].indent <= indent:
                out.append(None)
            else:
                value, index = _parse_block(tokens, index, tokens[index].indent, anchors)
                out.append(value)
            continue
        if _has_top_level_colon(body):
            synthetic = _Token(indent + 2, body, token.line)
            segment = [synthetic]
            end = index
            while end < len(tokens) and tokens[end].indent > indent:
                segment.append(tokens[end])
                end += 1
            value, consumed = _parse_mapping(segment, 0, indent + 2, anchors)
            if consumed != len(segment):
                bad = segment[consumed]
                raise ConfigError(f"line {bad.line}: invalid list mapping")
            out.append(value)
            index = end
        else:
            out.append(_parse_scalar(body, anchors, token.line))
    return out, index


def _parse_entry_value(
    tokens: list[_Token],
    index: int,
    parent_indent: int,
    raw_value: str,
    anchors: dict[str, Any],
    line: int,
) -> tuple[Any, int, str | None]:
    anchor = None
    raw_value = raw_value.strip()
    if raw_value.startswith("&"):
        parts = raw_value.split(maxsplit=1)
        anchor = parts[0][1:]
        raw_value = parts[1] if len(parts) == 2 else ""
    if raw_value:
        return _parse_scalar(raw_value, anchors, line), index, anchor
    if index < len(tokens) and tokens[index].indent > parent_indent:
        value, index = _parse_block(tokens, index, tokens[index].indent, anchors)
        return value, index, anchor
    return {}, index, anchor


def _split_pair(token: _Token) -> tuple[str, str]:
    index = _top_level_colon(token.text)
    if index < 0:
        raise ConfigError(f"line {token.line}: expected 'key: value'")
    key = token.text[:index].strip()
    if not key:
        raise ConfigError(f"line {token.line}: empty mapping key")
    return key, token.text[index + 1 :].strip()


def _has_top_level_colon(text: str) -> bool:
    return _top_level_colon(text) >= 0


def _top_level_colon(text: str) -> int:
    quote: str | None = None
    depth = 0
    for index, char in enumerate(text):
        if char in {"'", '"'}:
            quote = None if quote == char else (char if quote is None else quote)
        elif quote is None:
            if char in "[({":
                depth += 1
            elif char in "]) }".replace(" ", ""):
                depth -= 1
            elif char == ":" and depth == 0:
                return index
    return -1


def _parse_scalar(text: str, anchors: dict[str, Any], line: int) -> Any:
    text = text.strip()
    if text.startswith("*"):
        name = text[1:]
        if name not in anchors:
            raise ConfigError(f"line {line}: unknown alias '*{name}'")
        return copy.deepcopy(anchors[name])
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part, anchors, line) for part in _split_flow(inner)]
    if text == "{}":
        return {}
    if text[:1] in {"'", '"'}:
        try:
            return ast.literal_eval(text)
        except (SyntaxError, ValueError) as exc:
            raise ConfigError(f"line {line}: invalid quoted string") from exc
    lowered = text.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered in {"true", "false"}:
        return lowered == "true"
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:e[-+]?\d+)?", text, re.I):
        return float(text)
    return text


def _split_flow(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    quote: str | None = None
    depth = 0
    for index, char in enumerate(text):
        if char in {"'", '"'}:
            quote = None if quote == char else (char if quote is None else quote)
        elif quote is None:
            if char in "[({":
                depth += 1
            elif char in "]) }".replace(" ", ""):
                depth -= 1
            elif char == "," and depth == 0:
                parts.append(text[start:index].strip())
                start = index + 1
    parts.append(text[start:].strip())
    return parts


def _self_test() -> None:
    config_path = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "standard"
        / "runtime.yaml"
    )
    cfg = load_config(config_path)
    assert cfg.name == "standard"
    assert cfg.dataset.type == "local"
    assert cfg.input_data.type == "real_user"
    assert cfg.input_data.interface == "input"
    assert cfg.dataset.kwargs.tables.history == "history.jsonl"
    assert cfg.context.compat.m == 20
    try:
        _ = cfg.dataset.kwargs.missing_field
    except ConfigFieldError as exc:
        assert exc.field_path == "dataset.kwargs.missing_field"
        assert str(config_path.resolve()) in str(exc)
    else:
        raise AssertionError("缺失字段没有定位到 YAML")

    import contextlib
    import io
    import tempfile

    # 1. YAML 和输出目录都没有：parse 直接拒绝。
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            load_cfg([])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("无 YAML、无输出目录时仍然进入了程序")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)

        # 2. 没有 YAML、只有输出目录：复制默认 YAML，再从副本加载。
        output_dir = root / "new_experiment"
        copied_cfg = load_cfg(["--output-dir", str(output_dir)])
        copied_yaml = output_dir / DEFAULT_CONFIG_PATH.name
        assert copied_yaml.is_file()
        assert copied_cfg.source_path == copied_yaml.resolve()
        assert copied_cfg.dataset.kwargs.path == "."
        copied_text = copied_yaml.read_text(encoding="utf-8")
        assert "# terminalinput" in copied_text
        assert "--output-dir 是" in copied_text

        # 3. 有 YAML：从指定 YAML 启动，并把终端来源注释写入末尾。
        custom_path = root / "custom.yaml"
        custom_text = config_path.read_text(encoding="utf-8").replace(
            "  type: real_user\n",
            "  type: user_answer_json\n",
            1,
        )
        custom_path.write_text(custom_text, encoding="utf-8")
        yaml_cfg = load_cfg(["--config", str(custom_path)])
        assert yaml_cfg.input_data.type == "user_answer_json"
        assert yaml_cfg.runtime.system.to_dict() == cfg.runtime.system.to_dict()
        assert "--config 是" in custom_path.read_text(encoding="utf-8")

        # 4. YAML + 终端补充：先追加完整生效段，再重新读取 YAML。
        supplemented = load_cfg(
            [
                "--config",
                str(custom_path),
                "--input-type",
                "json_then_user",
                "--input-path",
                "dialog.json",
                "--dataset-path",
                "run/terminal",
            ]
        )
        assert supplemented.input_data.type == "json_then_user"
        assert supplemented.input_data.path == "dialog.json"
        assert supplemented.dataset.kwargs.path == "run/terminal"
        assert supplemented.runtime.system.to_dict() == yaml_cfg.runtime.system.to_dict()
        supplemented_text = custom_path.read_text(encoding="utf-8")
        assert "--input-type 是 'json_then_user'" in supplemented_text
        assert "以下配置为准" in supplemented_text

        # 5. 明确指定的 YAML 不存在：不复制、不静默回退。
        missing_path = root / "missing.yaml"
        try:
            load_cfg(["--config", str(missing_path)])
        except FileNotFoundError as exc:
            assert str(missing_path) in str(exc)
        else:
            raise AssertionError("不存在的 YAML 被静默忽略")

        # 6. name: <YamlName> 占位符自动取文件名；${name} 在别处原样替换。
        # 复制一份任务模板当新任务用时，默认数据目录跟着新文件名走，不用
        # 手动同步——这正是这次真实撞上的那个 bug（复制了 path 忘记改）。
        placeholder_path = root / "my_new_task.yaml"
        placeholder_path.write_text(
            "name: <YamlName>\n"
            "dataset:\n"
            "  type: local\n"
            "  kwargs:\n"
            "    path: ../../task/${name}\n"
            "    tables:\n"
            "      history: history.jsonl\n"
            "      state: state_latest.json\n"
            "      context: context_latest.json\n"
            "      life_cycle: life_cycle.json\n"
            "      patches: patches.jsonl\n"
            "      compression: compression\n"
            "      raw: raw_history.jsonl\n",
            encoding="utf-8",
        )
        placeholder_cfg = load_config(placeholder_path)
        assert placeholder_cfg.name == "my_new_task"
        assert placeholder_cfg.dataset.kwargs.path == "../../task/my_new_task"

        # An explicit name still wins; ${name} follows whatever that is.
        custom_name_path = root / "another_file.yaml"
        custom_name_path.write_text(
            "name: chosen_on_purpose\n"
            "dataset:\n"
            "  kwargs:\n"
            "    path: ../../task/${name}\n",
            encoding="utf-8",
        )
        custom_name_cfg = load_config(custom_name_path)
        assert custom_name_cfg.name == "chosen_on_purpose"
        assert custom_name_cfg.dataset.kwargs.path == "../../task/chosen_on_purpose"


if __name__ == "__main__":
    _self_test()
    print("dataset.load_config: ok")

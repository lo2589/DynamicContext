"""Start panel: choose a model and a shape of run, then launch it.

Everything this runtime does is decided by one YAML (README: "YAML 必须成为
cfg.xx 的唯一运行配置入口"), which is excellent for reproducibility and
unhelpful the very first time — a new user has no provider config, so
`main.py` cannot even reach the point of serving its viewer, and hand-editing
a 120-line YAML is the only way in.

So this panel does not replace the YAML; it *writes* one. Pick a few things,
press start, and it produces `task/<name>/runtime.yaml` plus the provider
config it references, then launches `main.py --config` against exactly that
file. What runs afterwards is an ordinary YAML-driven session — inspectable,
editable, re-runnable by hand — which is the point: the panel is a way to
author the config, never a second source of truth.

Every field has a working default, so "start" with nothing touched is a valid
run. It binds its own port and needs no RuntimeComponents, which is what lets
it work before any model has been configured.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..registry import saver

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "config" / "standard" / "runtime.yaml"
TASKS_DIR = REPO_ROOT / "task"
LAUNCHER_PORT = 8775


def _default_task_name() -> str:
    return time.strftime("run_%m%d_%H%M")


def _free_port(preferred: int = 8777) -> int:
    """First viewer port not already claimed by a live session."""
    import socket

    for port in range(preferred, preferred + 40):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return preferred


def _options() -> dict[str, Any]:
    """Everything the panel offers, with the default already chosen."""
    from ..manager.runtime import _installed_ollama_models
    from ..provider.provider import CONFIG_DIR, PROVIDER_DEFAULTS

    installed = _installed_ollama_models(PROVIDER_DEFAULTS["ollama"]["base_url"])
    saved = sorted(p.name for p in CONFIG_DIR.glob("*.json")) if CONFIG_DIR.is_dir() else []
    runs = _existing_runs()
    return {
        "task": _default_task_name(),
        "existing_tasks": [run["task"] for run in runs],
        "runs": runs,
        "vendors": PROVIDER_DEFAULTS,
        "installed": installed,
        "saved": saved,
        # Prefer a real chat model over whatever happens to be first: the
        # shipped default is a translation model, which answers by translating
        # the question and makes a fresh install look broken.
        "default_model": _preferred_model(installed),
        "port": _free_port(),
        "system": "You are a concise assistant.",
        "yaml_sources": _yaml_sources(),
        "lifecycle_rules": LIFECYCLE_RULES,
        "default_slots": DEFAULT_SLOTS,
        "input_types": [
            {"id": "real_user", "label": "我自己打字", "needs_path": False},
            {"id": "user_only_json", "label": "数据集：每条一个提问", "needs_path": True},
            {"id": "user_answer_json", "label": "数据集：提问+答案，从第一条没答案的续", "needs_path": True},
            {"id": "json_then_user", "label": "数据集跑完再接手打字", "needs_path": True},
        ],
        "datasets": _datasets(),
        "histories": _histories(),
    }


def _histories() -> list[dict[str, Any]]:
    """Existing ledgers that a new run could be seeded from.

    Continuing someone else's conversation needs nothing but their
    history.jsonl: state and context are projections, so they are recomputed
    from the copied ledger at startup rather than carried along
    (README: 状态文件可以删除后重算). The YAML is the other half of the pair
    and is supplied by whatever you pick here — that is the choice being made.
    """

    found: list[dict[str, Any]] = []
    for base in (REPO_ROOT / "task", REPO_ROOT / "data"):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.jsonl")):
            if path.name != "history.jsonl" or "snapshots" in path.parts:
                continue
            if any(part.startswith(".") for part in path.relative_to(REPO_ROOT).parts):
                continue
            turns = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
            found.append(
                {
                    "path": str(path.relative_to(REPO_ROOT)),
                    "turns": max(0, turns - 1),
                }
            )
    return found


def _datasets() -> list[str]:
    """Prompt sets that could drive a run, repo-relative.

    Both .json (a top-level array) and .jsonl (one record per line) qualify —
    the reader accepts either, and JSONL is what anything exported from this
    project's own ledgers looks like.
    """

    found: list[str] = []
    for base in (REPO_ROOT / "data", REPO_ROOT / "dataset", REPO_ROOT / "task"):
        if not base.is_dir():
            continue
        candidates = sorted(base.rglob("*.json")) + sorted(base.rglob("*.jsonl"))
        for path in candidates:
            # A run's own ledgers are outputs, not prompt sets.
            if path.name in {"history.jsonl", "patches.jsonl", "raw_history.jsonl"}:
                continue
            if path.name in {"state_latest.json", "context_latest.json", "life_cycle.json"}:
                continue
            if any(part.startswith(".") for part in path.relative_to(REPO_ROOT).parts):
                continue
            if "snapshots" in path.parts:
                continue
            found.append(str(path.relative_to(REPO_ROOT)))
    return found[:60]


# Y-08. The declarations this runtime is actually about: which slots are on
# stage, for how long. Every entry is a registered end-function
# (code/lifecycle/rules.py), described in the terms a user picking one needs —
# what it does to the timetable, and what it costs. Kept here rather than read
# off the registry because the registry knows the functions, not what choosing
# one means for the picture the viewer draws.
LIFECYCLE_RULES = [
    {
        "id": "permanent",
        "label": "一直在场",
        "note": "写下就再也不退场；上下文只增不减",
        "yaml": "null",
    },
    {
        "id": "born",
        "label": "只在出生那轮",
        "note": "想完就扔，下一轮就看不见了（think 的默认）",
        "yaml": "born",
    },
    {
        "id": "born+n",
        "label": "出生后再留 n 轮",
        "note": "滑动窗口：n 轮后自动退场",
        "yaml": "born+n",
        "needs_n": True,
    },
    {
        "id": "until_cancelled",
        "label": "钉住，直到手动取消",
        "note": "唯一不随距离衰减的机制；/cancelpin 才会撤下",
        "yaml": "until_cancelled",
    },
    {
        "id": "cycle",
        "label": "被指回时缺席",
        "note": "输入指回这一轮时它让位；需要数据集提供 linked_trap",
        "yaml": "cycle",
        "needs_dataset": True,
    },
    {
        "id": "labelled",
        "label": "按数据集标记决定",
        "note": "打了标的按 born 结束，没打的永久；需要数据集提供 type",
        "yaml": "labelled",
        "needs_dataset": True,
    },
    {
        "id": "until_goal_end",
        "label": "直到所属 goal 结束",
        "note": "goal 一收尾，它管辖的内容一起折叠",
        "yaml": "until_goal_end",
    },
]

# The slots a fresh run starts with. system is turn 0 and is declared as a
# literal range, not an end-function, so it is not offered as a row.
DEFAULT_SLOTS = [
    {"element": "user", "rule": "permanent", "n": 3},
    {"element": "think", "rule": "born", "n": 3},
    {"element": "assistant", "rule": "permanent", "n": 3},
]


def _yaml_sources() -> list[dict[str, str]]:
    """YAMLs that could be copied next to an orphaned history.

    Adopting one is a deliberate statement — "these turns were produced under
    rules like these" — so the choice is the user's and it is made explicit,
    rather than the panel inventing a config behind their back.
    """

    sources = [{"id": "__template__", "label": "默认模板 (config/standard/runtime.yaml)"}]
    if TASKS_DIR.is_dir():
        for directory in sorted(TASKS_DIR.iterdir()):
            candidate = directory / "runtime.yaml"
            if candidate.is_file():
                sources.append({"id": directory.name, "label": f"task/{directory.name}/runtime.yaml"})
    return sources


def _existing_runs() -> list[dict[str, Any]]:
    """Past conversations on disk, newest first, each marked live or not.

    A run's ledger outlives the process that wrote it — that is the whole
    point of `history.jsonl` — so the panel has to offer them back. Without
    this the only way to return to yesterday's conversation is to remember
    its directory name and type it exactly.
    """

    from .session_registry import list_sessions

    if not TASKS_DIR.is_dir():
        return []
    live = {entry["task"]: entry for entry in list_sessions().values()}
    runs: list[dict[str, Any]] = []
    for directory in TASKS_DIR.iterdir():
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        history = directory / "history.jsonl"
        if not history.is_file():
            continue
        turns = sum(1 for line in history.read_text(encoding="utf-8").splitlines() if line.strip())
        entry = live.get(directory.name)
        runs.append(
            {
                "task": directory.name,
                # Turn 0 holds the system prompt, so it is not an exchange.
                "turns": max(0, turns - 1),
                "modified": time.strftime("%m-%d %H:%M", time.localtime(history.stat().st_mtime)),
                "live_url": f"http://127.0.0.1:{entry['port']}/" if entry else None,
                **_yaml_summary(directory / "runtime.yaml"),
            }
        )
    runs.sort(key=lambda run: run["modified"], reverse=True)
    return runs


def _yaml_summary(yaml_path: Path) -> dict[str, Any]:
    """What a run's own YAML says about it — the other half of the pair.

    A run is a (yaml, history) pair: the ledger only means anything read
    through the declarations that produced it. So the list shows what each
    stored YAML actually configures, and a history with no YAML beside it is
    reported as unstartable rather than quietly handed a freshly generated
    config — that would be answering one config's history with another's
    rules, which is precisely what this pairing exists to prevent.
    """

    if not yaml_path.is_file():
        return {"has_yaml": False, "summary": None, "model": None}
    try:
        from ..dataset.load_config import load_config

        cfg = load_config(yaml_path)
        data = cfg.to_dict()
    except Exception:  # noqa: BLE001 - an unreadable YAML is still worth listing
        return {"has_yaml": True, "summary": "yaml 读不出来", "model": None}

    provider = data.get("provider") or {}
    model = None
    try:
        from ..provider.provider import load_provider_config

        model = load_provider_config(provider.get("config")).model
    except Exception:  # noqa: BLE001 - the referenced provider file may be gone
        model = provider.get("config")

    life = data.get("life_cycle") or {}
    bits = [f"{provider.get('type')} / {model}"]
    bits.append("压缩" if isinstance(data.get("compact"), dict) else "不压缩")
    if life.get("think") == ["born", None]:
        bits.append("留 think")
    interface = (data.get("input_data") or {}).get("interface")
    bits.append("网页输入" if interface == "gui" else "终端输入")
    return {"has_yaml": True, "summary": " · ".join(str(b) for b in bits), "model": model}


CHAT_MODEL_HINTS = ("qwen", "llama", "glm", "gemma", "mistral", "phi", "deepseek")


def _preferred_model(installed: list[str]) -> str:
    for hint in CHAT_MODEL_HINTS:
        for name in installed:
            if hint in name.lower():
                return name
    return installed[0] if installed else ""


def _rule_to_yaml(rule: str, n: int) -> str:
    """A picked rule as the YAML bound the loader expects.

    `permanent` is written as the empty value `null`, which is what an absent
    end already means (code/lifecycle/rules.py: "Writing nothing is not naming
    a function"). `born+n` carries its parameter in the name, which
    resolve_bound splits back out — the parameter never becomes part of the
    registered function's identity.
    """

    if rule == "permanent":
        return "null"
    if rule == "born+n":
        return f"born+{max(1, int(n))}"
    return rule


def build_yaml(
    *,
    task: str,
    vendor: str,
    config_name: str,
    system: str,
    interface: str,
    port: int,
    compact_on: bool,
    slots: list[dict] | None = None,
    input_type: str = "real_user",
    input_path: str = "",
    compact_interval: int = 20,
    compact_keep_recent: int = 10,
    compact_threshold: int = 32000,
    compact_retention: str = "latest_only",
    compact_prompt: str = "",
    recall_on: bool = False,
    recall_trigger: str = "always",
    recall_top_k: int = 3,
) -> str:
    """Render the shipped template with the panel's answers substituted.

    Text substitution rather than parse-and-redump, for the same reason the
    live model switch rewrites in place: the template's comments and anchors
    are documentation for whoever opens this YAML next, and a redump loses
    every one of them.
    """

    text = TEMPLATE.read_text(encoding="utf-8")
    text = re.sub(r"^name: .*$", f"name: {task}", text, count=1, flags=re.M)
    text = re.sub(
        r"(provider:\n)  type: .*\n  config: .*\n",
        lambda m: f"{m.group(1)}  type: {vendor}\n  config: {config_name}\n",
        text,
        count=1,
    )
    text = re.sub(
        r"^    content: .*$",
        f"    content: {json.dumps(system, ensure_ascii=False)}",
        text,
        count=1,
        flags=re.M,
    )
    text = re.sub(r"^  interface: .*$", f"  interface: {interface}", text, count=1, flags=re.M)
    text = re.sub(r"^    path: \.\./\.\./task/standard$", "    path: .", text, count=1, flags=re.M)
    text = re.sub(r"^    enabled: false.*$", "    enabled: true", text, count=1, flags=re.M)
    text = re.sub(r"^    port: 8777$", f"    port: {port}", text, count=1, flags=re.M)
    # Y-05/Y-06: where the turns come from.
    text = re.sub(r"^  type: real_user$", f"  type: {input_type}", text, count=1, flags=re.M)
    if input_path:
        # input_data.path is resolved against the YAML's own directory
        # (_resolved_json_path), and this YAML lives in the task dir — so the
        # repo-relative path picked in the panel is rewritten relative to that
        # directory. Never absolute: a YAML carrying a machine's own paths
        # only runs on that machine, which contradicts this project's claim
        # that the same input reproduces the same context anywhere.
        relative = os.path.relpath(REPO_ROOT / input_path, TASKS_DIR / task)
        text = re.sub(
            r"^  path: null$",
            f"  path: {json.dumps(relative, ensure_ascii=False)}",
            text,
            count=1,
            flags=re.M,
        )

    # Y-08: the whole life_cycle block, rewritten from the picked rules. system
    # keeps its literal [1, null]: it is turn 0's slot, declared as a range
    # rather than by an end-function.
    if slots:
        lines = ["life_cycle:", "  system: [1, null]"]
        for slot in slots:
            element = str(slot.get("element") or "").strip()
            if not element or element == "system":
                continue
            bound = _rule_to_yaml(str(slot.get("rule") or "permanent"), slot.get("n") or 3)
            lines.append(f"  {element}: [born, {bound}]")
        if recall_on and not any(str(s.get("element")) == "recall" for s in slots):
            # Enabling recall without declaring how long recalled evidence
            # lives is refused at build_runtime; [born, born] (visible only for
            # the turn that asked) is the documented example.
            lines.append("  recall: [born, born]")
        text = re.sub(
            r"^life_cycle:\n(?:  .*\n)+",
            "\n".join(lines) + "\n",
            text,
            count=1,
            flags=re.M,
        )

    if not compact_on:
        # `compact: none` is the documented off-switch, same convention as
        # chat.tools / recall.type.
        text = re.sub(r"^compact:\n(?:[ ].*\n|\n)*?(?=^\S)", "compact: none\n\n", text, count=1, flags=re.M)
    else:
        # Y-11~15
        text = re.sub(
            r"^  overload_threshold_bytes: \d+$",
            f"  overload_threshold_bytes: {int(compact_threshold)}",
            text, count=1, flags=re.M,
        )
        text = re.sub(
            r"^        interval_turns: \d+$",
            f"        interval_turns: {int(compact_interval)}",
            text, count=1, flags=re.M,
        )
        text = re.sub(
            r"^        keep_recent_turns: \d+$",
            f"        keep_recent_turns: {int(compact_keep_recent)}",
            text, count=1, flags=re.M,
        )
        if compact_prompt.strip():
            text = re.sub(
                r"^      prompt: .*$",
                f"      prompt: {json.dumps(compact_prompt, ensure_ascii=False)}",
                text, count=1, flags=re.M,
            )
        if compact_retention and compact_retention != "latest_only":
            text = re.sub(
                r"^      periodic:$",
                f"      retention: {compact_retention}\n      periodic:",
                text, count=1, flags=re.M,
            )

    # Y-16~19: recall is off in the template; turning it on also requires the
    # searchable fields to be named, which build_runtime enforces.
    if recall_on:
        searchable = [
            str(s.get("element"))
            for s in (slots or [])
            if str(s.get("element")) not in {"", "system", "think", "recall"}
        ] or ["user", "assistant"]
        text = re.sub(
            r"^recall: &recall\n(?:  .*\n)+",
            "recall: &recall\n"
            "  type: grep\n"
            f"  trigger: {recall_trigger}\n"
            f"  search_fields: [{', '.join(searchable)}]\n"
            f"  top_k: {int(recall_top_k)}\n"
            "  build: none\n"
            "  recall: none\n"
            "  update: none\n",
            text, count=1, flags=re.M,
        )
    return text


@saver("launcher.create")
def create_run(payload: dict) -> dict:
    """Write the provider config and the task YAML, then start the session."""
    from ..provider.provider import PROVIDER_DEFAULTS, ProviderConfig, save_provider_config

    task = str(payload.get("task") or "").strip() or _default_task_name()
    if not re.fullmatch(r"[\w.-]+", task):
        raise ValueError("任务名只能用字母、数字、下划线、点、减号")

    saved_name = str(payload.get("use_saved") or "").strip()
    if saved_name:
        config_name = saved_name
        vendor = json.loads((_provider_dir() / saved_name).read_text(encoding="utf-8")).get(
            "provider", "ollama"
        )
    else:
        vendor = str(payload.get("provider") or "ollama").strip().lower()
        if vendor not in PROVIDER_DEFAULTS:
            raise ValueError(f"unsupported provider {vendor!r}")
        defaults = PROVIDER_DEFAULTS[vendor]
        config = ProviderConfig.from_dict(
            {
                "provider": vendor,
                "model": str(payload.get("model") or "").strip() or defaults["model"],
                "base_url": str(payload.get("base_url") or "").strip() or defaults["base_url"],
                "api_key": str(payload.get("api_key") or ""),
            }
        )
        # B-01: one file per task, not per vendor. A shared "{vendor}.json"
        # means two tasks on the same vendor point at the same file, so
        # switching the model in one session silently rewrites the other's —
        # a run's config has to belong to that run.
        config_name = f"{task}.json"
        save_provider_config(config, config_name)

    port = int(payload.get("port") or _free_port())
    task_dir = TASKS_DIR / task
    yaml_path = task_dir / "runtime.yaml"
    history_path = task_dir / "history.jsonl"

    # A run is a (yaml, history) pair. A ledger with no YAML beside it cannot
    # be continued here: whatever declarations produced those turns are gone,
    # and generating a fresh config would answer that history under different
    # rules — different life_cycle, different compaction, silently. Point the
    # user at the file instead of guessing on their behalf.
    adopt = str(payload.get("adopt_yaml") or "").strip()
    if history_path.is_file() and not yaml_path.is_file():
        if not adopt:
            raise ValueError(
                f"task/{task}/ 有 history 但没有配套的 runtime.yaml。"
                f"选一份 yaml 复制进来配对，或换个新任务名重开一局。"
            )
        source = (
            TEMPLATE if adopt == "__template__" else TASKS_DIR / adopt / "runtime.yaml"
        )
        if not source.is_file():
            raise ValueError(f"找不到要复制的 yaml：{source}")
        task_dir.mkdir(parents=True, exist_ok=True)
        # Retarget the copy at this task: its name, its own directory, and a
        # port that is actually free. Everything else — life_cycle, compaction,
        # provider — is inherited verbatim, which is the point of choosing it.
        text = source.read_text(encoding="utf-8")
        text = re.sub(r"^name: .*$", f"name: {task}", text, count=1, flags=re.M)
        text = re.sub(r"^    path: .*$", "    path: .", text, count=1, flags=re.M)
        text = re.sub(r"^    enabled: false.*$", "    enabled: true", text, count=1, flags=re.M)
        text = re.sub(r"^    port: \d+$", f"    port: {port}", text, count=1, flags=re.M)
        # Adopted from the panel means it will be driven from the panel: a
        # source yaml written for terminal input would come up with a dead
        # input box and no explanation.
        text = re.sub(r"^  interface: .*$", "  interface: gui", text, count=1, flags=re.M)
        yaml_path.write_text(text, encoding="utf-8")
        print(f"[adopted yaml] {source} -> {yaml_path}")

    seed = str(payload.get("seed_history") or "").strip()
    if seed and not history_path.is_file():
        source_history = (REPO_ROOT / seed).resolve()
        if not source_history.is_file() or REPO_ROOT not in source_history.parents:
            raise ValueError(f"找不到这份 history.jsonl：{seed}")
        task_dir.mkdir(parents=True, exist_ok=True)
        # Only the ledger is copied. state/context/life_cycle are projections
        # and are rebuilt from it on startup, so carrying them over would just
        # risk pairing this ledger with someone else's stale snapshot.
        history_path.write_text(source_history.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[seeded] {source_history} -> {history_path}")

    task_dir.mkdir(parents=True, exist_ok=True)
    resuming = yaml_path.exists()
    if not resuming:
        yaml_path.write_text(
            build_yaml(
                task=task,
                vendor=vendor,
                config_name=config_name,
                system=str(payload.get("system") or "You are a concise assistant."),
                interface="gui" if payload.get("web_input", True) else "input",
                port=port,
                compact_on=bool(payload.get("compact", True)),
                slots=payload.get("slots") or DEFAULT_SLOTS,
                input_type=str(payload.get("input_type") or "real_user"),
                input_path=str(payload.get("input_path") or ""),
                compact_interval=int(payload.get("compact_interval") or 20),
                compact_keep_recent=int(payload.get("compact_keep_recent") or 10),
                compact_threshold=int(payload.get("compact_threshold") or 32000),
                compact_retention=str(payload.get("compact_retention") or "latest_only"),
                compact_prompt=str(payload.get("compact_prompt") or ""),
                recall_on=bool(payload.get("recall")),
                recall_trigger=str(payload.get("recall_trigger") or "always"),
                recall_top_k=int(payload.get("recall_top_k") or 3),
            ),
            encoding="utf-8",
        )
    else:
        # An existing task keeps its own YAML — that file is the record of how
        # this conversation has been running, and silently rewriting it would
        # make the turns already in its ledger unreproducible. Only the port
        # is refreshed, since the old one may now be taken.
        text = yaml_path.read_text(encoding="utf-8")
        yaml_path.write_text(
            re.sub(r"^    port: \d+$", f"    port: {port}", text, count=1, flags=re.M),
            encoding="utf-8",
        )

    _preflight(yaml_path)

    process = subprocess.Popen(
        [sys.executable, "-u", "main.py", "--config", str(yaml_path)],
        cwd=str(REPO_ROOT),
        stdout=(task_dir / "run.log").open("a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    return {
        "ok": True,
        "task": task,
        "yaml": str(yaml_path),
        "url": f"http://127.0.0.1:{port}/",
        "pid": process.pid,
        "resumed": resuming,
    }


def _preflight(yaml_path: Path) -> None:
    """Fail here, with a sentence, rather than in a process nobody is watching.

    The panel's last act is to spawn `main.py` and redirect the browser at a
    port it expects to appear. If that process dies on startup — a YAML that
    names a provider config which is not on this machine is the easy way —
    the user sees a connection error and no reason for it. So load the config
    the same way the runtime will, and repair or report before spawning.

    A missing provider file is repaired when the answer is unambiguous: an
    adopted YAML brings its author's filename with it, and pointing it at the
    only config for that vendor here is what the user meant by adopting it.
    """

    from ..dataset.load_config import load_config
    from ..provider.provider import CONFIG_DIR, load_provider_config

    cfg = load_config(yaml_path)
    referenced = cfg.to_dict().get("provider", {}).get("config")
    try:
        load_provider_config(referenced)
        return
    except FileNotFoundError:
        pass

    vendor = cfg.to_dict().get("provider", {}).get("type")
    candidates = []
    for candidate in sorted(CONFIG_DIR.glob("*.json")) if CONFIG_DIR.is_dir() else []:
        try:
            if load_provider_config(candidate.name).provider == vendor:
                candidates.append(candidate.name)
        except (ValueError, FileNotFoundError, json.JSONDecodeError):
            continue
    if not candidates:
        raise ValueError(
            f"这份 yaml 指向 config-provider/{referenced}，但本机没有这个文件，"
            f"也没有任何 {vendor} 的配置可用。先在上面「开新的」里配一个 {vendor} 模型。"
        )
    chosen = candidates[0]
    text = yaml_path.read_text(encoding="utf-8")
    yaml_path.write_text(
        re.sub(r"^  config: .*$", f"  config: {chosen}", text, count=1, flags=re.M),
        encoding="utf-8",
    )
    print(f"[preflight] {referenced} 不存在，改指向 {chosen}")


def _provider_dir() -> Path:
    from ..provider.provider import CONFIG_DIR

    return CONFIG_DIR


PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>New run</title>
<style>
:root{--paper:#EDF1F2;--panel:#F7FAFA;--ink:#101A1E;--ink-2:#546A70;
  --rule:rgba(16,26,30,.14);--live:#0D7D6C;--shadow:0 1px 2px rgba(16,26,30,.05),0 12px 32px -18px rgba(16,26,30,.3)}
@media (prefers-color-scheme:dark){:root{--paper:#0C1214;--panel:#151D20;--ink:#DAE5E7;--ink-2:#88A0A7;
  --rule:rgba(218,229,231,.16);--live:#3FBFA8;--shadow:0 1px 2px rgba(0,0,0,.45),0 12px 32px -18px rgba(0,0,0,.8)}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);min-height:100vh;
  font:14px/1.6 ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  display:flex;align-items:center;justify-content:center;padding:28px}
.card{width:100%;max-width:35rem;background:var(--panel);border:1px solid var(--rule);
  border-radius:12px;box-shadow:var(--shadow);padding:22px 24px 20px}
.eyebrow{font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--ink-2)}
h1{font-size:20px;margin:6px 0 4px;font-family:ui-sans-serif,system-ui,sans-serif}
.sub{color:var(--ink-2);font-size:12px;margin:0 0 18px;
  font-family:ui-sans-serif,system-ui,sans-serif}
label{display:block;margin:13px 0 5px;font-size:11px;letter-spacing:.07em;
  text-transform:uppercase;color:var(--ink-2)}
input,select,textarea{width:100%;padding:8px 10px;border:1px solid var(--rule);border-radius:7px;
  background:var(--paper);color:var(--ink);font:inherit;font-size:13px}
textarea{resize:vertical;font-family:ui-sans-serif,system-ui,sans-serif;line-height:1.5}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.checks{display:flex;gap:16px;flex-wrap:wrap;margin-top:12px}
.checks label{display:flex;align-items:center;gap:6px;margin:0;text-transform:none;
  letter-spacing:0;font-size:12px;color:var(--ink);cursor:pointer}
.checks input{width:auto}
details{margin-top:14px;border-top:1px solid var(--rule);padding-top:10px}
summary{cursor:pointer;color:var(--ink-2);font-size:12px;
  font-family:ui-sans-serif,system-ui,sans-serif}
button.go{margin-top:20px;width:100%;padding:11px;border:0;border-radius:8px;
  background:var(--live);color:#fff;font:inherit;font-size:14px;font-weight:600;cursor:pointer}
button.go:disabled{opacity:.55;cursor:default}
.msg{margin-top:11px;font-size:12px;color:var(--ink-2);min-height:1.3em;overflow-wrap:anywhere}
.hint{font-size:11px;color:var(--ink-2);margin-top:4px;
  font-family:ui-sans-serif,system-ui,sans-serif}
/* Y-08 is the point of this runtime, so it gets the room and the emphasis:
   one row per slot, the rule spelled out in words, and a line saying what the
   choice does to the timetable the viewer draws. */
.lc{border:1px solid var(--live);border-radius:10px;padding:13px 14px 11px;margin-top:16px;
  background:color-mix(in srgb, var(--live) 5%, transparent)}
.lc h3{margin:0 0 3px;font-size:13px;font-family:ui-sans-serif,system-ui,sans-serif}
.lc .why{color:var(--ink-2);font-size:11.5px;margin:0 0 11px;
  font-family:ui-sans-serif,system-ui,sans-serif;line-height:1.55}
.lc-row{display:grid;grid-template-columns:7.5rem 1fr auto;gap:8px;align-items:center;
  margin-bottom:7px}
.lc-row .el{font-size:12.5px;font-weight:600}
.lc-row select{font-size:12px;padding:6px 8px}
.lc-row .n{width:4.2rem;font-size:12px;padding:6px 8px}
.lc-row .n[hidden]{display:none}
.lc-note{grid-column:1/-1;color:var(--ink-2);font-size:10.5px;margin:-3px 0 4px;
  font-family:ui-sans-serif,system-ui,sans-serif}
.lc-add{display:flex;gap:7px;margin-top:9px}
.lc-add input{flex:1;font-size:12px;padding:6px 8px}
.lc-add button,.lc-row .del{font-size:11px;padding:5px 9px;border:1px solid var(--rule);
  border-radius:6px;background:transparent;color:var(--ink-2);cursor:pointer}
.lc-add button:hover,.lc-row .del:hover{border-color:var(--live);color:var(--live)}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
#runs{display:flex;flex-direction:column;gap:6px;margin-bottom:20px}
#runs:empty{display:none}
.run{display:flex;align-items:center;gap:10px;padding:8px 11px;border:1px solid var(--rule);
  border-radius:8px;background:var(--paper);cursor:pointer;text-align:left;width:100%;
  color:var(--ink);font:inherit;font-size:12.5px}
.run:hover{border-color:var(--live)}
.run .nm{font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.run .meta{color:var(--ink-2);font-size:11px;white-space:nowrap}
.run{flex-wrap:wrap}
.run .cfg{flex-basis:100%;color:var(--ink-2);font-size:10.5px;margin-top:3px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.run.orphan{border-style:dashed}
.run .warn{flex-basis:100%;color:var(--ink);font-size:10.5px;margin-top:4px}
.adopt{display:flex;gap:6px;margin-top:6px;flex-basis:100%}
.adopt select{flex:1;font-size:11px;padding:4px 6px}
.adopt button{font-size:11px;padding:4px 9px;border:1px solid var(--live);border-radius:6px;
  background:transparent;color:var(--live);cursor:pointer}
.run .dot{width:7px;height:7px;border-radius:50%;background:var(--live);flex:none}
.sec{font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--ink-2);
  margin:0 0 8px}
</style></head>
<body>
<div class="card">
  <div class="eyebrow">simple chat runtime</div>
  <h1>开一局新的</h1>
  <p class="sub">每项都有默认值，直接点开始就能跑。面板不替代 yaml——它写出一份 yaml，
  然后用它启动，之后你随时可以自己改那份文件重跑。</p>

  <div class="sec" id="runs-title" hidden>接着之前的</div>
  <div id="runs"></div>

  <div class="sec">开新的</div>
  <label for="task">任务名</label>
  <input id="task" autocomplete="off">
  <div class="hint" id="task-hint"></div>

  <div class="two">
    <div>
      <label for="vendor">厂商</label>
      <select id="vendor"></select>
    </div>
    <div>
      <label for="model">模型</label>
      <select id="model"></select>
    </div>
  </div>
  <input id="model-manual" placeholder="模型名字" autocomplete="off" hidden style="margin-top:8px">

  <label for="system">系统提示词</label>
  <textarea id="system" rows="2"></textarea>

  <label for="seed">从一份 history.jsonl 接着聊（可选）</label>
  <select id="seed"><option value="">— 从空白开始 —</option></select>
  <div class="hint">只复制账本；state / context 是投影，启动时按这份 yaml 的声明重算。</div>

  <div class="two">
    <div>
      <label for="input-type">对话从哪来</label>
      <select id="input-type"></select>
    </div>
    <div>
      <label for="dataset">数据集文件</label>
      <select id="dataset"><option value="">—</option></select>
    </div>
  </div>

  <div class="lc">
    <h3>生命周期 · 每个槽位在场多久</h3>
    <p class="why">这是这个 runtime 的主张，也是右边格子图唯一的变量：同一段对话，
    换一组声明就是完全不同的一张表。历史一个字不会变——变的只是谁还在上下文里。</p>
    <div id="lc-rows"></div>
    <div class="lc-add">
      <input id="lc-new" placeholder="新槽位名字（例如 goal、note）" autocomplete="off">
      <button type="button" id="lc-add-btn">＋ 加一行</button>
    </div>
  </div>

  <div class="checks">
    <label><input type="checkbox" id="web-input" checked> 在网页里聊天</label>
    <label><input type="checkbox" id="compact" checked> 自动压缩</label>
    <label><input type="checkbox" id="recall"> 开启回捞</label>
  </div>

  <details>
    <summary>压缩参数（默认要聊满 30 轮才触发一次）</summary>
    <div class="grid3">
      <div><label for="c-interval">每隔几轮</label><input id="c-interval" value="20"></div>
      <div><label for="c-keep">保留最近几轮</label><input id="c-keep" value="10"></div>
      <div><label for="c-threshold">超过多少字节</label><input id="c-threshold" value="32000"></div>
    </div>
    <label for="c-retention">摘要保留策略</label>
    <select id="c-retention">
      <option value="latest_only">只留最新一份</option>
      <option value="keep_all">全部保留</option>
      <option value="last_k">保留最近 k 份</option>
      <option value="equidistant">等距抽 k 份</option>
    </select>
    <label for="c-prompt">摘要提示词（留空用默认）</label>
    <textarea id="c-prompt" rows="2" placeholder="留空则用模板里的默认提示词"></textarea>
  </details>

  <details>
    <summary>回捞参数（勾了「开启回捞」才生效）</summary>
    <div class="two">
      <div>
        <label for="r-trigger">什么时候捞</label>
        <select id="r-trigger">
          <option value="always">每轮都捞</option>
          <option value="pattern">匹配到关键词才捞</option>
          <option value="never">从不</option>
        </select>
      </div>
      <div><label for="r-topk">最多捞回几条</label><input id="r-topk" value="3"></div>
    </div>
  </details>

  <details>
    <summary>更多（已保存的配置 / 密钥 / 地址 / 端口）</summary>
    <label for="saved">用已保存的配置</label>
    <select id="saved"><option value="">— 新建 —</option></select>
    <label for="base-url">base_url</label>
    <input id="base-url" autocomplete="off">
    <label for="api-key">api_key（只写到本机 config-provider/）</label>
    <input id="api-key" type="password" autocomplete="off">
    <label for="port">viewer 端口</label>
    <input id="port" autocomplete="off">
  </details>

  <button class="go" id="go">开始</button>
  <div class="msg" id="msg"></div>
</div>
<script>
var $ = function (id) { return document.getElementById(id) }
var MANUAL = "__manual__"
var opts = {}

fetch("/options").then(function (r) { return r.json() }).then(function (d) {
  opts = d
  $("task").value = d.task
  $("system").value = d.system
  $("port").value = d.port
  Object.keys(d.vendors).forEach(function (v) {
    var o = document.createElement("option"); o.value = v; o.textContent = v
    $("vendor").appendChild(o)
  })
  $("vendor").value = d.installed.length ? "ollama" : Object.keys(d.vendors)[0]
  ;(d.saved || []).forEach(function (f) {
    var o = document.createElement("option"); o.value = f; o.textContent = f
    $("saved").appendChild(o)
  })
  syncVendor()
  checkTask()
  renderRuns(d.runs || [])

  // Y-05/06
  ;(d.input_types || []).forEach(function (it) {
    var o = document.createElement("option"); o.value = it.id; o.textContent = it.label
    $("input-type").appendChild(o)
  })
  ;(d.histories || []).forEach(function (h) {
    var o = document.createElement("option")
    o.value = h.path; o.textContent = h.path + "（" + h.turns + " 轮）"
    $("seed").appendChild(o)
  })
  ;(d.datasets || []).forEach(function (f) {
    var o = document.createElement("option"); o.value = f; o.textContent = f
    $("dataset").appendChild(o)
  })
  syncInputType()
  // Y-08
  slots = (d.default_slots || []).map(function (s) { return {element: s.element, rule: s.rule, n: s.n || 3} })
  renderSlots()
})

// ---- Y-05/06: where turns come from ----
function syncInputType() {
  var chosen = (opts.input_types || []).filter(function (it) { return it.id === $("input-type").value })[0]
  var needs = chosen && chosen.needs_path
  $("dataset").disabled = !needs
  $("dataset").style.opacity = needs ? 1 : .45
}

// ---- Y-08: life_cycle rows ----
var slots = []

function renderSlots() {
  var host = $("lc-rows")
  host.innerHTML = ""
  slots.forEach(function (slot, index) {
    var row = document.createElement("div")
    row.className = "lc-row"
    var rule = ruleById(slot.rule)
    row.innerHTML =
      '<span class="el">' + slot.element + "</span>" +
      "<select>" + (opts.lifecycle_rules || []).map(function (r) {
        return '<option value="' + r.id + '"' + (r.id === slot.rule ? " selected" : "") +
          ">" + r.label + "</option>"
      }).join("") + "</select>" +
      '<input class="n" type="number" min="1" value="' + (slot.n || 3) + '"' +
        (rule && rule.needs_n ? "" : " hidden") + ">" +
      '<span class="lc-note">' + (rule ? rule.note : "") + "</span>"
    var sel = row.querySelector("select")
    sel.onchange = function () {
      slots[index].rule = sel.value
      renderSlots()
    }
    var n = row.querySelector(".n")
    n.oninput = function () { slots[index].n = parseInt(n.value || "1", 10) }
    // system is fixed; everything else the user added can go away again.
    if (["user", "think", "assistant"].indexOf(slot.element) === -1) {
      var del = document.createElement("button")
      del.type = "button"; del.className = "del"; del.textContent = "删"
      del.onclick = function () { slots.splice(index, 1); renderSlots() }
      row.appendChild(del)
    }
    host.appendChild(row)
  })
}

function ruleById(id) {
  return (opts.lifecycle_rules || []).filter(function (r) { return r.id === id })[0]
}

function renderRuns(runs) {
  var host = $("runs")
  host.innerHTML = ""
  if (!runs.length) return
  $("runs-title").hidden = false
  runs.forEach(function (r) {
    var b = document.createElement(r.has_yaml ? "button" : "div")
    b.className = "run" + (r.has_yaml ? "" : " orphan")
    var html = (r.live_url ? '<span class="dot"></span>' : "") +
      '<span class="nm">' + r.task + "</span>" +
      '<span class="meta">' + r.turns + " 轮 · " + r.modified +
      (r.live_url ? " · 运行中" : "") + "</span>"
    if (r.has_yaml) {
      // What this run's own yaml declares — resuming means resuming under
      // these rules, so they are worth seeing before clicking.
      html += '<span class="cfg">' + (r.summary || "") + "</span>"
    } else {
      html += '<span class="warn">只有 history，没有配套 yaml —— 选一份复制进来才能接着跑</span>' +
        '<span class="adopt"><select class="adopt-src">' +
        (opts.yaml_sources || []).map(function (s) {
          return '<option value="' + s.id + '">' + s.label + "</option>"
        }).join("") +
        '</select><button type="button">复制并开始</button></span>'
    }
    b.innerHTML = html
    if (r.has_yaml) {
      b.onclick = function () { resume(r) }
    } else {
      b.querySelector(".adopt button").onclick = function (e) {
        e.stopPropagation()
        resume(r, b.querySelector(".adopt-src").value)
      }
    }
    host.appendChild(b)
  })
}

function resume(r, adoptYaml) {
  // Already running: just go to it — starting a second process on the same
  // ledger would have two writers appending to one history.jsonl.
  if (r.live_url) { location.href = r.live_url; return }
  $("msg").textContent = "正在拉起 " + r.task + "…"
  fetch("/create", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task: r.task, port: $("port").value, adopt_yaml: adoptYaml || "" })
  })
    .then(function (x) { return x.json().then(function (d) { if (!x.ok) throw new Error(d.error || x.statusText); return d }) })
    .then(function (d) {
      $("msg").textContent = "接着跑 " + d.task + " —— 正在打开…"
      setTimeout(function () { location.href = d.url }, 2500)
    })
    .catch(function (e) { $("msg").textContent = "失败：" + e.message })
}

function syncVendor() {
  var v = $("vendor").value
  var d = opts.vendors[v] || {}
  $("base-url").value = d.base_url || ""
  var list = v === "ollama" ? (opts.installed || []).slice() : []
  if (d.model && list.indexOf(d.model) === -1) list.push(d.model)
  $("model").innerHTML = list.map(function (m) {
    return '<option value="' + m + '">' + m + "</option>"
  }).join("") + '<option value="' + MANUAL + '">手动输入…</option>'
  var preferred = v === "ollama" ? opts.default_model : d.model
  $("model").value = list.indexOf(preferred) !== -1 ? preferred : (list[0] || MANUAL)
  syncModel()
}
function syncModel() { $("model-manual").hidden = $("model").value !== MANUAL }
function checkTask() {
  var exists = (opts.existing_tasks || []).indexOf($("task").value.trim()) !== -1
  $("task-hint").textContent = exists
    ? "这个任务已经存在 —— 会接着它的历史继续，并沿用它自己的 yaml"
    : "会新建 task/" + ($("task").value.trim() || "?") + "/"
}
$("vendor").onchange = syncVendor
$("input-type").onchange = syncInputType
$("lc-add-btn").onclick = function () {
  var name = $("lc-new").value.trim()
  if (!name) return
  if (slots.some(function (s) { return s.element === name })) return
  slots.push({element: name, rule: "permanent", n: 3})
  $("lc-new").value = ""
  renderSlots()
}
$("model").onchange = syncModel
$("task").oninput = checkTask
$("saved").onchange = function () {
  var using = !!$("saved").value
  ;["vendor", "model", "model-manual", "base-url", "api-key"].forEach(function (i) {
    $(i).disabled = using
  })
}

$("go").onclick = function () {
  $("go").disabled = true
  $("msg").textContent = "正在写 yaml、拉起进程…"
  var model = $("model").value === MANUAL ? $("model-manual").value.trim() : $("model").value
  fetch("/create", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      task: $("task").value.trim(),
      use_saved: $("saved").value,
      provider: $("vendor").value,
      model: model,
      base_url: $("base-url").value.trim(),
      api_key: $("api-key").value,
      system: $("system").value,
      web_input: $("web-input").checked,
      compact: $("compact").checked,
      port: $("port").value,
      slots: slots,
      seed_history: $("seed").value,
      input_type: $("input-type").value,
      input_path: $("dataset").disabled ? "" : $("dataset").value,
      compact_interval: $("c-interval").value,
      compact_keep_recent: $("c-keep").value,
      compact_threshold: $("c-threshold").value,
      compact_retention: $("c-retention").value,
      compact_prompt: $("c-prompt").value,
      recall: $("recall").checked,
      recall_trigger: $("r-trigger").value,
      recall_top_k: $("r-topk").value
    })
  })
    .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || r.statusText); return d }) })
    .then(function (d) {
      $("msg").textContent = (d.resumed ? "接着跑 " : "已创建 ") + d.yaml + " —— 正在打开…"
      // The run needs a moment to bind its viewer port before we send the
      // browser there; going too early lands on a connection error.
      setTimeout(function () { location.href = d.url }, 2500)
    })
    .catch(function (e) { $("msg").textContent = "失败：" + e.message; $("go").disabled = false })
}
</script>
</body></html>
"""


class LauncherHandler(BaseHTTPRequestHandler):
    def _send(self, payload: bytes, mime: str, *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, value: dict, *, status: int = 200) -> None:
        self._send(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json", status=status)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/options":
            self._json(_options())
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/create":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": "invalid JSON body"}, status=400)
            return
        try:
            self._json(create_run(payload))
        except (ValueError, FileNotFoundError, OSError) as exc:
            self._json({"error": str(exc)}, status=400)

    def log_message(self, *_args) -> None:
        """Quiet; the panel is a single page, not a service worth logging."""


@saver("launcher.ensure")
def ensure_launcher(*, port: int = LAUNCHER_PORT) -> str:
    """Return a URL for the start panel, starting it if nobody is serving it.

    "New conversation" has to work from inside a session, and a session has
    no reason to know whether the user happened to launch `start.py` first.
    Rather than hand the page a link that may be dead, the session brings the
    panel up on demand — as its own process, so closing either one leaves the
    other alone.
    """

    import socket

    with socket.socket() as probe:
        probe.settimeout(0.4)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return f"http://127.0.0.1:{port}/"  # already serving

    subprocess.Popen(
        [sys.executable, "-u", "start.py", "--no-open", "--port", str(port)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Give it a moment to bind, so the page is not redirected to a refused
    # connection; the panel itself is tiny and comes up fast.
    for _ in range(20):
        time.sleep(0.15)
        with socket.socket() as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                break
    return f"http://127.0.0.1:{port}/"


@saver("launcher.serve")
def start_launcher(*, port: int = LAUNCHER_PORT, open_browser: bool = True) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), LauncherHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"[start] {url}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    return server


def _self_test() -> None:
    import tempfile
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # The rendered YAML must be loadable by the real loader, with every
    # panel answer actually taking effect.
    from ..dataset.load_config import load_config

    rendered = build_yaml(
        task="panel_test",
        vendor="ollama",
        config_name="ollama.json",
        system="你是测试助手",
        interface="gui",
        port=8999,
        compact_on=True,
        slots=[
            {"element": "user", "rule": "permanent"},
            {"element": "think", "rule": "born+n", "n": 2},
            {"element": "assistant", "rule": "until_cancelled"},
        ],
        input_type="user_only_json",
        input_path="data/sample_prompts.jsonl",
        compact_interval=5,
        compact_keep_recent=2,
        compact_threshold=12000,
        compact_retention="keep_all",
        recall_on=True,
        recall_trigger="pattern",
        recall_top_k=7,
    )
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "runtime.yaml"
        path.write_text(rendered, encoding="utf-8")
        cfg = load_config(path)
        assert cfg.name == "panel_test"
        assert cfg.provider.type == "ollama"
        assert cfg.provider.config == "ollama.json"
        assert cfg.runtime.system.content == "你是测试助手"
        assert cfg.input_data.interface == "gui"
        assert cfg.dataset.kwargs.path == "."
        assert cfg.runtime.viewer.enabled is True
        assert cfg.runtime.viewer.port == 8999
        # Y-08: every picked rule reaches the loader in the form it expects.
        life = cfg.life_cycle.to_dict()
        assert life["system"] == [1, None]
        assert life["user"] == ["born", None]          # permanent -> empty end
        assert life["think"] == ["born", "born+2"]     # parameter kept in the name
        assert life["assistant"] == ["born", "until_cancelled"]
        assert life["recall"] == ["born", "born"]      # required once recall is on
        # Y-05/Y-06
        assert cfg.input_data.type == "user_only_json"
        # Stored absolute on purpose: relative would resolve against the task
        # directory the YAML lives in, not the repo.
        # Relative to the YAML's own directory, never absolute: a config
        # carrying this machine's paths would not run anywhere else.
        assert cfg.input_data.path == "../../data/sample_prompts.jsonl"
        assert "/Users/" not in rendered
        # Y-11~15
        assert cfg.compact.overload_threshold_bytes == 12000
        assert cfg.compact.compressors.summary.periodic.interval_turns == 5
        assert cfg.compact.compressors.summary.periodic.keep_recent_turns == 2
        assert cfg.compact.compressors.summary.retention == "keep_all"
        # Y-16~19
        assert cfg.recall.type == "grep"
        assert cfg.recall.trigger == "pattern"
        assert cfg.recall.top_k == 7
        assert "user" in cfg.recall.search_fields

        # compact off is the documented "none", and must still parse.
        off = build_yaml(
            task="panel_off", vendor="ollama", config_name="ollama.json",
            system="s", interface="input", port=8998, compact_on=False,
        )
        off_path = Path(temporary) / "off.yaml"
        off_path.write_text(off, encoding="utf-8")
        off_cfg = load_config(off_path)
        assert off_cfg.to_dict()["compact"] == "none"
        assert off_cfg.life_cycle.to_dict()["think"] == ["born", "born"]
        assert off_cfg.recall.type == "none"
        assert off_cfg.input_data.interface == "input"

    # The panel itself serves without any runtime present.
    server = ThreadingHTTPServer(("127.0.0.1", 0), LauncherHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with opener.open(f"http://127.0.0.1:{port}/") as response:
            assert "开一局新的".encode("utf-8") in response.read()
        with opener.open(f"http://127.0.0.1:{port}/options") as response:
            options = json.loads(response.read())
        assert options["task"]
        assert "ollama" in options["vendors"]
        assert isinstance(options["installed"], list)
        # A bad task name is refused rather than writing junk directories.
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/create",
            data=json.dumps({"task": "../escape"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            opener.open(request)
            raise AssertionError("非法任务名应该被拒绝")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    _self_test()
    print("saver.launcher: ok")

"""The runtime-facing provider interface and its fixed config loader."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from ..registry import provider as provider_registry


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = RUNTIME_ROOT / "config" / "config-provider"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "provider.json"
DEFAULT_OLLAMA_MODEL = "RogerBen/HY-MT2-1.8B:latest"

PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "dry-run": {"base_url": "", "model": "dry-run"},
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "GLM-4.5-Air",
    },
    "minimax": {
        "base_url": "https://api.minimaxi.com/v1",
        "model": "MiniMax-M2.5",
    },
    "ollama": {
        "base_url": "http://127.0.0.1:11434",
        "model": DEFAULT_OLLAMA_MODEL,
    },
}


def _load_api_key_file(name: str | Path) -> str:
    """Read a key from an existing provider-settings JSON without copying it."""

    path = Path(name).expanduser()
    if not path.is_absolute():
        path = (RUNTIME_ROOT / path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"provider api_key_file does not exist: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"provider api_key_file root must be an object: {path}")
    api_key = str(value.get("api_key") or "")
    if not api_key:
        raise ValueError(f"provider api_key_file has no api_key: {path}")
    return api_key


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    base_url: str = ""
    api_key: str = ""
    timeout: int = 300
    thinking: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProviderConfig":
        provider = str(value.get("provider") or "").strip().lower()
        if provider not in PROVIDER_DEFAULTS:
            raise ValueError(f"unsupported provider: {provider!r}")
        defaults = PROVIDER_DEFAULTS[provider]
        model = str(value.get("model") or defaults["model"]).strip()
        base_url = str(value.get("base_url") or defaults["base_url"]).strip().rstrip("/")
        if provider == "ollama" and base_url and "://" not in base_url:
            base_url = f"http://{base_url}"
        api_key_env = str(value.get("api_key_env") or "").strip()
        api_key_file = str(value.get("api_key_file") or "").strip()
        api_key = str(value.get("api_key") or "")
        if not api_key and api_key_env:
            api_key = str(os.environ.get(api_key_env) or "")
        if not api_key and api_key_file:
            api_key = _load_api_key_file(api_key_file)
        timeout = int(value.get("timeout") or 300)
        raw_thinking = value.get("thinking")
        if raw_thinking is None:
            thinking = None
        elif isinstance(raw_thinking, Mapping):
            thinking = dict(raw_thinking)
        else:
            raise ValueError("provider thinking must be an object")
        if not model:
            raise ValueError("provider model cannot be empty")
        if provider != "dry-run" and not base_url:
            raise ValueError("provider base_url cannot be empty")
        if provider in {"deepseek", "glm", "minimax"} and not api_key:
            sources = []
            if api_key_env:
                sources.append(f"env {api_key_env}")
            if api_key_file:
                sources.append(f"file {api_key_file}")
            suffix = f" ({' or '.join(sources)})" if sources else ""
            raise ValueError(f"{provider} api_key{suffix} cannot be empty")
        return cls(provider, model, base_url, api_key, timeout, thinking)


def _config_path(name: str | Path | None = None) -> Path:
    if name is None:
        return DEFAULT_CONFIG_PATH
    candidate = Path(name)
    if candidate.is_absolute() or candidate.parent != Path("."):
        raise ValueError("provider config name must be a filename inside config-provider")
    path = (CONFIG_DIR / candidate).resolve()
    if path.parent != CONFIG_DIR.resolve():
        raise ValueError("provider config must stay inside config-provider")
    return path


def load_provider_config(name: str | Path | None = None) -> ProviderConfig:
    path = _config_path(name)
    if not path.is_file():
        raise FileNotFoundError(f"provider config does not exist: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"provider config root must be an object: {path}")
    return ProviderConfig.from_dict(value)


def save_provider_config(
    config: ProviderConfig,
    name: str | Path | None = None,
) -> Path:
    path = _config_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


def _messages(context: Any) -> list[dict[str, str]]:
    if not isinstance(context, list):
        raise TypeError("provider context must be a list")
    messages: list[dict[str, str]] = []
    for index, item in enumerate(context):
        if not isinstance(item, dict):
            raise TypeError(f"provider context[{index}] must be an object")
        role = str(item.get("role") or "").strip()
        content = item.get("content")
        if not role or not isinstance(content, str):
            raise ValueError(f"provider context[{index}] requires role and string content")
        messages.append({"role": role, "content": content})
    return messages


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
# A proxy has no business intercepting a request to the machine we are already
# on. urllib honours HTTP_PROXY for every host unless no_proxy happens to be
# set, so a developer with a system proxy configured (Privoxy, Clash, a
# corporate PAC) cannot reach their own Ollama at all — the request is
# forwarded to the proxy, which cannot route back to the caller's loopback.
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _urlopen(request: urllib.request.Request, *, timeout: int):
    host = urllib.parse.urlsplit(request.full_url).hostname or ""
    if host in _LOOPBACK_HOSTS:
        return _DIRECT_OPENER.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def _request_json(
    url: str,
    body: dict[str, Any],
    *,
    api_key: str,
    timeout: int,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with _urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"provider HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"provider connection failed: {url}: {exc.reason}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("provider response must be an object")
    return value


def _stream_response_objects(
    url: str,
    body: dict[str, Any],
    *,
    api_key: str,
    timeout: int,
) -> Iterator[dict[str, Any]]:
    """Reuse the previous provider bridge's SSE/JSONL streaming protocol."""

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with _urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        break
                else:
                    payload = line
                try:
                    value = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value
                if value.get("done") is True:
                    break
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"provider HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"provider connection failed: {url}: {exc.reason}") from exc


def _stream_canonical_text(
    objects: Iterator[dict[str, Any]],
) -> Iterator[str]:
    """Render separate reasoning/content deltas into the existing think format."""

    think_open = False
    for value in objects:
        if "message" in value:
            message = value.get("message") or {}
        else:
            choices = value.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                continue
            message = choices[0].get("delta") or choices[0].get("message") or {}
        if not isinstance(message, dict):
            continue
        reasoning = str(
            message.get("reasoning_content")
            or message.get("reasoning")
            or message.get("thinking")
            or ""
        )
        content = str(message.get("content") or "")
        if reasoning:
            if not think_open:
                think_open = True
                yield "<think>"
            yield reasoning
        if content:
            if think_open:
                think_open = False
                yield "</think>"
            yield content
    if think_open:
        yield "</think>"


def _render_message(message: dict[str, Any]) -> str:
    content = str(message.get("content") or "")
    think = str(
        message.get("reasoning_content")
        or message.get("reasoning")
        or message.get("thinking")
        or ""
    )
    return f"<think>{think}</think>{content}" if think else content


@provider_registry("client.dry-run")
class DryRunProvider:
    def __init__(self, config: ProviderConfig | None = None):
        del config

    def chat(self, context: Any, *, turn_id: int | str, **_: Any) -> str:
        _messages(context)
        return f"<think>{turn_id}</think> id：{turn_id}"

    def chat_stream(self, context: Any, *, turn_id: int | str, **_: Any) -> Iterator[str]:
        yield self.chat(context, turn_id=turn_id)


@provider_registry("client.deepseek")
@provider_registry("client.glm")
@provider_registry("client.minimax")
class OpenAICompatibleProvider:
    def __init__(self, config: ProviderConfig):
        self.config = config

    def chat(self, context: Any, *, turn_id: int | str, **kwargs: Any) -> str:
        del turn_id
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": _messages(context),
            "stream": False,
        }
        if self.config.thinking is not None:
            body["thinking"] = dict(self.config.thinking)
        body.update(kwargs)
        base = self.config.base_url
        url = (
            base
            if base.endswith("/chat/completions")
            else f"{base}/chat/completions"
        )
        value = _request_json(
            url,
            body,
            api_key=self.config.api_key,
            timeout=self.config.timeout,
        )
        choices = value.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise RuntimeError("provider response has no choices[0]")
        return _render_message(choices[0].get("message") or {})

    def chat_stream(
        self,
        context: Any,
        *,
        turn_id: int | str,
        **kwargs: Any,
    ) -> Iterator[str]:
        del turn_id
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": _messages(context),
            "stream": True,
        }
        if self.config.thinking is not None:
            body["thinking"] = dict(self.config.thinking)
        body.update(kwargs)
        body["stream"] = True
        base = self.config.base_url
        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        yield from _stream_canonical_text(
            _stream_response_objects(
                url,
                body,
                api_key=self.config.api_key,
                timeout=self.config.timeout,
            )
        )


@provider_registry("client.ollama")
class OllamaProvider:
    def __init__(self, config: ProviderConfig):
        self.config = config

    def chat(self, context: Any, *, turn_id: int | str, **kwargs: Any) -> str:
        del turn_id
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": _messages(context),
            "stream": False,
        }
        body.update(kwargs)
        value = _request_json(
            f"{self.config.base_url}/api/chat",
            body,
            api_key=self.config.api_key,
            timeout=self.config.timeout,
        )
        message = value.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("Ollama response has no message")
        return _render_message(message)

    def chat_stream(
        self,
        context: Any,
        *,
        turn_id: int | str,
        **kwargs: Any,
    ) -> Iterator[str]:
        del turn_id
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": _messages(context),
            "stream": True,
        }
        body.update(kwargs)
        body["stream"] = True
        yield from _stream_canonical_text(
            _stream_response_objects(
                f"{self.config.base_url}/api/chat",
                body,
                api_key=self.config.api_key,
                timeout=self.config.timeout,
            )
        )


def build_provider(config: ProviderConfig):
    provider_class = provider_registry[f"client.{config.provider}"]
    return provider_class(config)


def load_provider(name: str | Path | None = None):
    return build_provider(load_provider_config(name))


@provider_registry("cfg.dry-run")
def _build_dry_run_from_cfg(cfg: Any):
    del cfg
    return DryRunProvider()


@provider_registry("cfg.deepseek")
@provider_registry("cfg.glm")
@provider_registry("cfg.minimax")
@provider_registry("cfg.ollama")
def _build_saved_provider_from_cfg(cfg: Any):
    config = load_provider_config(cfg.provider.config)
    if config.provider != cfg.provider.type:
        raise ValueError(
            f"cfg.provider.type={cfg.provider.type!r} 与"
            f" config-provider中的provider={config.provider!r}不一致"
        )
    return build_provider(config)


def build_provider_from_cfg(cfg: Any):
    """Use cfg.provider.type to select one provider builder function."""
    builder = provider_registry[f"cfg.{cfg.provider.type}"]
    return builder(cfg)


@provider_registry("chat")
@provider_registry("chat.normal")
def chat(
    provider_instance: Any,
    context: Any,
    *,
    turn_id: int | str,
    on_chunk: Callable[[str], Any] | None = None,
    tools: Any = None,
    search: Any = None,
    **kwargs: Any,
) -> str:
    """Stream every provider and turn a user interrupt into a partial answer."""

    del tools, search  # Reserved for later ReAct executors.
    chunks: list[str] = []
    stream = provider_instance.chat_stream(context, turn_id=turn_id, **kwargs)
    try:
        for chunk in stream:
            text = str(chunk or "")
            if not text:
                continue
            chunks.append(text)
            if on_chunk is not None:
                on_chunk(text)
    except KeyboardInterrupt:
        # A user stop is a successful partial generation, not a failed turn.
        pass
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    return "".join(chunks)


@provider_registry("chat.no_stream")
def chat_no_stream(
    provider_instance: Any,
    context: Any,
    *,
    turn_id: int | str,
    on_chunk: Callable[[str], Any] | None = None,
    tools: Any = None,
    search: Any = None,
    **kwargs: Any,
) -> str:
    """Call the provider's non-streaming path.

    Some providers only assemble reasoning and answer correctly when the whole
    message comes back in one response; MiniMax's own issue tracker documents
    its streaming reasoning parser losing track of where thinking ends and the
    answer begins, sending the same fragment more than once or leaving the
    literal ``<think>`` tags sitting in the answer text. The non-streaming path
    calls the same endpoint without that failure mode, at the cost of the whole
    turn arriving as one piece instead of as it is generated.
    """

    del tools, search  # Reserved for later ReAct executors.
    text = str(provider_instance.chat(context, turn_id=turn_id, **kwargs) or "")
    if text and on_chunk is not None:
        on_chunk(text)
    return text


@provider_registry("tools.none")
def no_tools(*_args: Any, **_kwargs: Any) -> None:
    return None


@provider_registry("search.none")
def no_search(*_args: Any, **_kwargs: Any) -> None:
    return None

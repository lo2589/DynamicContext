"""Initialize provider config once, then load it for later calls."""

from __future__ import annotations

import argparse
import getpass
import json
import subprocess
from typing import Any

from .provider import (
    DEFAULT_OLLAMA_MODEL,
    PROVIDER_DEFAULTS,
    ProviderConfig,
    build_provider,
    load_provider,
    load_provider_config,
    save_provider_config,
)


def parse(argv: Any = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m code.provider")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create config/config-provider/provider.json")
    init.add_argument("--vendor", choices=tuple(PROVIDER_DEFAULTS), default=None)
    init.add_argument("--api-key", default=None)
    init.add_argument("--model", default=None)
    init.add_argument("--base-url", default=None)
    init.add_argument("--config-name", default="provider.json")
    init.add_argument("--no-pull", action="store_true")

    show = commands.add_parser("show", help="load and display the saved config")
    show.add_argument("--config-name", default="provider.json")

    dry = commands.add_parser("dry-run", help="return deterministic turn id output")
    dry.add_argument("--turn-id", required=True)

    chat = commands.add_parser("chat", help="load saved config and send one user message")
    chat.add_argument("message")
    chat.add_argument("--turn-id", required=True)
    chat.add_argument("--config-name", default="provider.json")
    return parser.parse_args(argv)


def _ask(value: str | None, prompt: str, default: str = "") -> str:
    if value is not None:
        return value
    suffix = f" [{default}]" if default else ""
    entered = input(f"{prompt}{suffix}: ").strip()
    return entered or default


def _init(args: argparse.Namespace) -> None:
    vendor = _ask(args.vendor, "vendor", "ollama").lower()
    if vendor not in PROVIDER_DEFAULTS:
        raise ValueError(f"unsupported provider: {vendor}")
    defaults = PROVIDER_DEFAULTS[vendor]
    model = _ask(args.model, "model", defaults["model"])
    base_url = _ask(args.base_url, "base_url", defaults["base_url"])
    if args.api_key is None and vendor in {"glm", "minimax"}:
        api_key = getpass.getpass("api_key: ")
    else:
        api_key = args.api_key or ""
    config = ProviderConfig.from_dict(
        {
            "provider": vendor,
            "api_key": api_key,
            "model": model,
            "base_url": base_url,
        }
    )
    path = save_provider_config(config, args.config_name)
    if vendor == "ollama" and not args.no_pull:
        subprocess.run(["ollama", "pull", model], check=True)
    print(path)


def main(argv: Any = None) -> None:
    args = parse(argv)
    if args.command == "init":
        _init(args)
        return
    if args.command == "show":
        config = load_provider_config(args.config_name)
        visible = {
            "provider": config.provider,
            "model": config.model,
            "base_url": config.base_url,
            "has_api_key": bool(config.api_key),
            "timeout": config.timeout,
        }
        print(json.dumps(visible, ensure_ascii=False, indent=2))
        return
    if args.command == "dry-run":
        provider = build_provider(
            ProviderConfig("dry-run", "dry-run")
        )
        print(provider.chat([], turn_id=args.turn_id))
        return
    if args.command == "chat":
        provider = load_provider(args.config_name)
        print(
            provider.chat(
                [{"role": "user", "content": args.message}],
                turn_id=args.turn_id,
            )
        )
        return
    raise AssertionError(args.command)


if __name__ == "__main__":
    main()

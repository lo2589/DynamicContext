"""Folder-named decorators: decorate once, then resolve and call directly."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional, TypeVar, Union


Registered = TypeVar("Registered")


class FolderRegistry:
    def __init__(self, folder: str) -> None:
        self.folder = folder
        self._items: dict[str, Any] = {}

    def __call__(
        self,
        name: Optional[Union[str, Registered]] = None,
    ) -> Union[Registered, Callable[[Registered], Registered]]:
        if callable(name):
            return self._register(name.__name__, name)

        def decorator(item: Registered) -> Registered:
            item_name = name or getattr(item, "__name__", None)
            if not item_name:
                raise ValueError(f"{self.folder}注册项必须提供名称")
            return self._register(str(item_name), item)

        return decorator

    def _register(self, name: str, item: Registered) -> Registered:
        key = name.strip()
        if not key:
            raise ValueError(f"{self.folder}注册名不能为空")
        existing = self._items.get(key)
        if existing is not None and existing is not item:
            raise ValueError(f"{self.folder}.{key}重复注册")
        self._items[key] = item
        return item

    def get(self, name: str) -> Any:
        try:
            return self._items[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._items)) or "<empty>"
            raise KeyError(
                f"{self.folder}.{name}未注册；已有：{available}"
            ) from exc

    def __getitem__(self, name: str) -> Any:
        return self.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))


compact = FolderRegistry("compact")
dataset = FolderRegistry("dataset")
lifecycle = FolderRegistry("lifecycle")
manager = FolderRegistry("manager")
patch = FolderRegistry("patch")
provider = FolderRegistry("provider")
recall = FolderRegistry("recall")
saver = FolderRegistry("saver")


FOLDERS = {
    registry.folder: registry
    for registry in (
        compact,
        dataset,
        lifecycle,
        manager,
        patch,
        provider,
        recall,
        saver,
    )
}


def resolve(path: str) -> Any:
    """Resolve ``folder.name`` to the decorated item."""
    folder, separator, name = path.partition(".")
    if not separator or not name:
        raise ValueError("注册路径必须是folder.name")
    try:
        registry = FOLDERS[folder]
    except KeyError as exc:
        raise KeyError(f"未知注册目录{folder!r}") from exc
    return registry.get(name)


def _self_test() -> None:
    temporary = FolderRegistry("temporary")

    @temporary("echo")
    def echo(value: str) -> str:
        return value

    assert temporary["echo"]("ok") == "ok"
    assert temporary.names() == ("echo",)


if __name__ == "__main__":
    _self_test()
    print("registry: ok")

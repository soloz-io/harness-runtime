"""Shared type aliases for the harness runtime."""

from typing import Any as _Any

Namespace = tuple[str, ...]


class Event:
    """A single v3 protocol event from LangGraph stream_events."""

    __slots__ = ("method", "namespace", "data")

    def __init__(
        self,
        method: str,
        namespace: Namespace,
        data: dict[str, _Any],
    ) -> None:
        self.method = method
        self.namespace = namespace
        self.data = data

    @classmethod
    def from_raw(cls, raw: dict[str, _Any]) -> "Event":
        params = raw.get("params", {})
        ns_list = params.get("namespace", [])
        ns: Namespace = tuple(ns_list) if isinstance(ns_list, list) else ()
        return cls(
            method=raw.get("method", ""),
            namespace=ns,
            data=params.get("data", {}),
        )

    def __repr__(self) -> str:
        return f"Event(method={self.method!r}, ns={self.namespace!r})"

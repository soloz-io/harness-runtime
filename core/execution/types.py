"""Shared type aliases for the harness runtime."""

from typing import Any as _Any

Namespace = tuple[str, ...]


class Event:
    """A single v3 protocol event from LangGraph stream_events."""

    __slots__ = ("method", "namespace", "data", "interrupts")

    def __init__(
        self,
        method: str,
        namespace: Namespace,
        data: dict[str, _Any],
        interrupts: tuple[_Any, ...] = (),
    ) -> None:
        self.method = method
        self.namespace = namespace
        self.data = data
        self.interrupts = interrupts

    @classmethod
    def from_raw(cls, raw: dict[str, _Any]) -> "Event":
        params = raw.get("params", {})
        ns_list = params.get("namespace", [])
        ns: Namespace = tuple(ns_list) if isinstance(ns_list, list) else ()
        # `interrupts` is a SIBLING of `data` in the v3 protocol, not a key
        # inside it:
        #
        #   {"method": "values",
        #    "params": {"namespace": [], "data": {...}, "interrupts": (Interrupt(...),)}}
        #
        # Dropping it here is why an `ask_user` question never reached the UI.
        # The handler looked for a `__interrupt__` key inside `data` — that is
        # the CHECKPOINT channel's name, not the stream's, so the lookup could
        # never match and the turn ended silently with the graph still parked.
        # The interrupt must survive this boundary to be reachable at all.
        raw_interrupts = params.get("interrupts") or ()
        interrupts: tuple[_Any, ...] = (
            tuple(raw_interrupts)
            if isinstance(raw_interrupts, (list, tuple))
            else (raw_interrupts,)
        )
        return cls(
            method=raw.get("method", ""),
            namespace=ns,
            data=params.get("data", {}),
            interrupts=interrupts,
        )

    def __repr__(self) -> str:
        return (
            f"Event(method={self.method!r}, ns={self.namespace!r}, "
            f"interrupts={len(self.interrupts)})"
        )

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any


EventHandler = Callable[..., Any | Awaitable[Any]]


class EventEmitter:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def on(
        self,
        event: str,
        handler: EventHandler | None = None,
    ) -> EventHandler | Callable[[EventHandler], EventHandler]:
        def register(callback: EventHandler) -> EventHandler:
            self._handlers[str(event)].append(callback)
            return callback

        if handler is None:
            return register

        return register(handler)

    def off(self, event: str, handler: EventHandler) -> None:
        self._handlers[str(event)].remove(handler)

    async def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        print("[EMIT]", event, args, kwargs)
        for handler in list(self._handlers[str(event)]):
            result = handler(*args, **kwargs)
            if asyncio.iscoroutine(result):
                await result

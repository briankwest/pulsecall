"""In-process pub/sub for SSE fan-out.

Topics:
    call:{call_id}        — per-call events (answer, dnc, ended)
    campaign:{campaign_id} — per-campaign progress events
"""
import asyncio
from typing import Any, Optional

_subscribers: dict[str, set[asyncio.Queue]] = {}
_loop: Optional[asyncio.AbstractEventLoop] = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Record the server's event loop so sync code (SWAIG tools) can publish safely."""
    global _loop
    _loop = loop


async def subscribe(topic: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(topic, set()).add(q)
    return q


async def unsubscribe(topic: str, q: asyncio.Queue) -> None:
    subs = _subscribers.get(topic)
    if subs is None:
        return
    subs.discard(q)
    if not subs:
        _subscribers.pop(topic, None)


def publish(topic: str, event_type: str, data: Any) -> None:
    """Thread-safe publish callable from sync code (SWAIG tool handlers run in a thread)."""
    payload = {"type": event_type, "data": data}
    subs = _subscribers.get(topic)
    if not subs:
        return
    if _loop is None or not _loop.is_running():
        # Loop not started yet — drop (no subscribers anyway)
        return
    for q in list(subs):
        _loop.call_soon_threadsafe(q.put_nowait, payload)


def has_subscribers(topic: str) -> bool:
    return bool(_subscribers.get(topic))

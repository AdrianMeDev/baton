"""Wait until at least one agent of a route leaves its cooldown."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import TextIO

from baton.limits import LimitStore
from baton.routed import say

CHUNK_SECONDS = 300.0


def wait_for_reset(
    agents: Iterable[str],
    store: LimitStore,
    *,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    stop_requested: Callable[[], bool],
    out: TextIO,
    deadline: datetime | None = None,
) -> str:
    """Sleep in chunks of at most five minutes until an agent is free or a stop is requested.

    The store is re-read every chunk, so a cleared cooldown (``baton recheck``) ends the wait early.
    """
    names = set(agents)
    announced = None
    while True:
        if stop_requested():
            return "stopped"
        current = now()
        if deadline is not None and current >= deadline:
            return "budget reached: run deadline (--max-hours)"
        cooling = {n: d for n, d in store.active(now=current).items() if n in names}
        if len(cooling) < len(names):
            return "ready"
        agent, reset_at = min(cooling.items(), key=lambda item: item[1])
        if announced != reset_at:
            announced = reset_at
            say(
                out,
                f"{agent} in cooldown, sign in with another account or wait until "
                f"{reset_at.astimezone():%H:%M}",
            )
        seconds = min(CHUNK_SECONDS, max((reset_at - current).total_seconds(), 1.0))
        if deadline is not None:
            seconds = min(seconds, (deadline - current).total_seconds())
        sleep(seconds)

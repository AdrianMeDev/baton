"""Wait until at least one agent of a route leaves its cooldown."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Literal, TextIO

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
) -> Literal["ready", "stopped"]:
    """Sleep in chunks of at most five minutes until an agent is free or a stop is requested.

    The store is re-read every chunk, so a cleared cooldown (``baton recheck``) ends the wait early.
    """
    names = set(agents)
    announced = None
    while True:
        if stop_requested():
            return "stopped"
        current = now()
        cooling = {n: d for n, d in store.active(now=current).items() if n in names}
        if len(cooling) < len(names):
            return "ready"
        agent, deadline = min(cooling.items(), key=lambda item: item[1])
        if announced != deadline:
            announced = deadline
            say(
                out,
                f"{agent} in cooldown, sign in with another account or wait until "
                f"{deadline.astimezone():%H:%M}",
            )
        sleep(min(CHUNK_SECONDS, max((deadline - current).total_seconds(), 1.0)))

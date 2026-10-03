"""Small, deterministic policy functions used by the local Compose scaler."""

from dataclasses import dataclass


SCALE_UP_AFTER_SECONDS = 30
SCALE_DOWN_AFTER_SECONDS = 60
SCALE_COOLDOWN_SECONDS = 120


@dataclass
class QueueTimers:
    backlog_since: float | None = None
    idle_since: float | None = None
    last_scaled_at: float | None = None


def next_replica_count(
    *,
    enabled: bool,
    current: int,
    minimum: int,
    maximum: int,
    queued: int,
    busy: bool,
    demand: bool | None = None,
    now: float,
    timers: QueueTimers,
) -> int:
    """Return a one-step desired replica count and update demand timers."""
    demand = queued > 0 if demand is None else demand
    if not enabled:
        timers.backlog_since = None
        timers.idle_since = None
        return current

    if current < minimum:
        timers.backlog_since = None
        timers.idle_since = None
        timers.last_scaled_at = now
        return minimum

    if demand:
        timers.idle_since = None
        timers.backlog_since = now if timers.backlog_since is None else timers.backlog_since
        if current < maximum and now - timers.backlog_since >= SCALE_UP_AFTER_SECONDS:
            timers.last_scaled_at = now
            timers.backlog_since = now
            return current + 1
        return current

    timers.backlog_since = None
    if busy:
        timers.idle_since = None
        return current

    timers.idle_since = now if timers.idle_since is None else timers.idle_since
    cooled_down = timers.last_scaled_at is None or now - timers.last_scaled_at >= SCALE_COOLDOWN_SECONDS
    if current > minimum and now - timers.idle_since >= SCALE_DOWN_AFTER_SECONDS and cooled_down:
        timers.last_scaled_at = now
        timers.idle_since = now
        return current - 1
    return current

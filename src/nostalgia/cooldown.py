"""Cooldown tracking during a build.

Cooldowns have to advance *as items are placed*, not once at the start of the day.
A pool of twelve commercials in a 96-slot day would otherwise put the same spot in
every break, because nothing tells the selector that it just used it two minutes
ago.

Two rules, both applied:

* **Windowed cooldown.** ``cooldown_ms`` from config, measured against the item's
  own air time.
* **Hard separation.** The last ``min_gap`` placements are always excluded, which
  guarantees a break never literally repeats back-to-back even when the whole pool
  is inside its cooldown window. When that would leave nothing playable, the hard
  separation is dropped before the windowed one: repeating after four other spots
  beats repeating immediately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class CooldownTracker:
    cooldown_ms: int = 0
    min_gap: int = 3
    """How many intervening placements before an asset is eligible again."""

    last_placed_ms: dict[str, int] = field(default_factory=dict)
    recent: list[str] = field(default_factory=list)

    @classmethod
    def from_history(
        cls,
        history: dict[str, int],
        *,
        cooldown_ms: int,
        min_gap: int = 3,
        disabled: bool = False,
    ) -> "CooldownTracker":
        """Seed from the catalog's recorded airings (previous days)."""
        tracker = cls(cooldown_ms=0 if disabled else cooldown_ms, min_gap=0 if disabled else min_gap)
        tracker.last_placed_ms = dict(history)
        return tracker

    def cooling(self, candidates: Iterable[str], now_ms: int) -> set[str]:
        """Keys that must not air at *now_ms*."""
        if self.cooldown_ms <= 0 and self.min_gap <= 0:
            return set()

        blocked: set[str] = set()
        if self.min_gap > 0:
            blocked.update(self.recent[-self.min_gap :])
        if self.cooldown_ms > 0:
            blocked.update(
                key
                for key, last in self.last_placed_ms.items()
                if now_ms - last < self.cooldown_ms
            )
        return blocked

    def relaxed(self, now_ms: int) -> set[str]:
        """Blocked set with the windowed cooldown dropped (hard separation kept)."""
        if self.min_gap <= 0:
            return set()
        return set(self.recent[-self.min_gap :])

    def record(self, key: str, start_ms: int, *, gap: int = 1) -> None:
        self.last_placed_ms[key] = start_ms
        self.recent.append(key)
        if len(self.recent) > 64:
            del self.recent[: len(self.recent) - 64]
        for _ in range(max(0, gap - 1)):
            self.recent.append(key)

    def reset_history(self) -> None:
        self.last_placed_ms.clear()
        self.recent.clear()


__all__ = ["CooldownTracker"]

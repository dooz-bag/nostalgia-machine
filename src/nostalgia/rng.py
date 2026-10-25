"""Deterministic randomness.

The channel must produce the same playout for a given date no matter how many
times we build it, which rules out :mod:`random`'s global state. Every random
choice therefore draws from a PRNG seeded by a stable hash of its *purpose*.

Changing ``SEED_VERSION`` is a deliberate act: it reshuffles every generated
day, so it is versioned and covered by the golden-file test.
"""

from __future__ import annotations

import hashlib
import random
from typing import Iterable, Sequence, TypeVar

SEED_VERSION = 1

T = TypeVar("T")


def stable_seed(*parts: object) -> int:
    """Hash arbitrary parts into a stable 64-bit seed.

    ``hash()`` is randomised per process, so it is never used here; SHA-256 of
    the UTF-8 join is stable across processes, platforms and Python versions.
    """
    payload = "\x1f".join([str(SEED_VERSION), *(str(part) for part in parts)])
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def rng_for(*parts: object) -> random.Random:
    return random.Random(stable_seed(*parts))


def weighted_choice(rng: random.Random, items: Sequence[T], weights: Sequence[float]) -> T:
    if len(items) != len(weights):
        raise ValueError("items and weights must be the same length")
    if not items:
        raise ValueError("cannot choose from an empty sequence")
    total = sum(weights)
    if total <= 0:
        return items[rng.randrange(len(items))]
    point = rng.random() * total
    upto = 0.0
    for item, weight in zip(items, weights):
        upto += weight
        if point < upto:
            return item
    return items[-1]


def shuffled(rng: random.Random, items: Iterable[T]) -> list[T]:
    pool = list(items)
    rng.shuffle(pool)
    return pool

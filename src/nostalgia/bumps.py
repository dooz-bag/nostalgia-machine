"""Bump matching -- the reason this project exists.

ErsatzTV, Tunarr, DizqueTV and every other streamer treat filler as anonymous.
A 2003 [adult swim] break is not anonymous: the card that says *"next up: Futurama"*
belongs immediately before *Futurama*, and the one that says *"we now return to
Aqua Teen"* belongs immediately after *Aqua Teen*. Only the scheduler knows the
adjacency, so only the scheduler can choose the bump.

Scoring is deliberately **lenient but never wrong-on-purpose**: an asset naming a
*different* show can never beat a neutral bump. That is what makes "semi-strict"
behave the way you'd hope instead of the way it sounds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .assets import NEUTRAL, Asset
from .rng import rng_for, weighted_choice

POSITION_EXACT = 3.0
POSITION_ADJACENT = 1.0
POSITION_UNKNOWN = 0.5

SUBJECT_EXACT = 5.0
SUBJECT_FAMILY = 2.0
SUBJECT_NEUTRAL = 1.0
SUBJECT_WRONG = -100.0

ERA_MATCH = 1.0
ERA_NEAR = 0.25
FAMILY_ONLY = 0.5
COOLDOWN_PENALTY = 2.0

#: Positions considered "the same kind of moment" as another.
ADJACENT_POSITIONS: dict[str, set[str]] = {
    "INTO": {"NEXT", "ANY"},
    "OUT": {"AFTER", "ANY"},
    "NEXT": {"INTO", "ANY"},
    "AFTER": {"OUT", "ANY"},
    "ANY": {"INTO", "OUT", "NEXT", "AFTER"},
}


@dataclass(frozen=True, slots=True)
class BumpRequest:
    """One slot in a break, described by *intent* rather than by a file."""

    subject: str = "this"
    position: str = "ANY"
    max_seconds: float | None = None
    this_show: str | None = None
    next_show: str | None = None
    prev_show: str | None = None
    year: int | None = None
    family: str | None = None
    era_drift: int = 1

    def resolved_subject(self) -> str:
        if self.subject == "this":
            return self.this_show or NEUTRAL
        if self.subject == "next":
            return self.next_show or NEUTRAL
        if self.subject == "prev":
            return self.prev_show or NEUTRAL
        if self.subject.startswith("specific:"):
            return self.subject.split(":", 1)[1]
        return self.subject


@dataclass
class Scored:
    asset: Asset
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class BumpChoice:
    asset: Asset | None
    score: float
    fallback: bool
    """True when no asset could name the right show and a neutral one was used."""

    reason: str


def score_asset(asset: Asset, request: BumpRequest, *, cool: bool) -> tuple[float, list[str]]:
    """Score *asset* against *request*. Returns ``(score, reasons)``.

    A score of 0 or less means "ineligible", not "last choice".
    """
    reasons: list[str] = []
    score = 0.0

    wanted = request.resolved_subject()
    if wanted and wanted != NEUTRAL:
        if asset.subject == wanted:
            score += SUBJECT_EXACT
            reasons.append(f"subject={wanted}")
        elif asset.subject is None or asset.subject == NEUTRAL:
            score += SUBJECT_NEUTRAL
            reasons.append("subject=neutral")
        elif request.family and asset.family == request.family:
            score += SUBJECT_FAMILY
            reasons.append(f"family={request.family}")
        else:
            return SUBJECT_WRONG, [f"wrong subject {asset.subject!r}"]
    else:
        if asset.is_neutral_bump:
            score += SUBJECT_NEUTRAL
            reasons.append("neutral slot")
        else:
            reasons.append("neutral slot, show-specific bump")

    if asset.position:
        if asset.position == request.position:
            score += POSITION_EXACT
            reasons.append(f"position={request.position}")
        elif asset.position in ADJACENT_POSITIONS.get(request.position, set()):
            score += POSITION_ADJACENT
            reasons.append(f"position~{asset.position}")
        else:
            score += POSITION_UNKNOWN
            reasons.append(f"position?{asset.position}")
    else:
        score += POSITION_UNKNOWN

    if request.year is not None:
        if asset.era_covers(request.year, request.era_drift):
            score += ERA_MATCH
            reasons.append(f"era={request.year}")
        elif asset.era_from is None:
            score += ERA_NEAR
        else:
            nearest = request.year - (asset.era_to or asset.era_from or request.year)
            if abs(nearest) <= 2:
                score += ERA_NEAR
                reasons.append(f"era~{asset.era_from}")
            else:
                score -= ERA_NEAR

    if request.family and asset.family == request.family:
        score += FAMILY_ONLY

    if cool:
        score -= COOLDOWN_PENALTY
        reasons.append("cooldown")

    return score, reasons


def rank(
    pool: Iterable[Asset],
    request: BumpRequest,
    *,
    on_cooldown: set[str] | None = None,
) -> list[Scored]:
    """Score and sort a bump pool, best first."""
    cooling = on_cooldown or set()
    scored: list[Scored] = []
    for asset in pool:
        score, reasons = score_asset(asset, request, cool=asset.key in cooling)
        if score <= 0:
            continue
        scored.append(Scored(asset=asset, score=score, reasons=reasons))
    scored.sort(key=lambda item: (-item.score, item.asset.key))
    return scored


def choose(
    pool: Iterable[Asset],
    request: BumpRequest,
    *,
    min_score: float,
    strictness: str = "semi",
    on_cooldown: set[str] | None = None,
    seed_parts: Sequence[object] = (),
    generated: Iterable[Asset] = (),
) -> BumpChoice:
    """Pick the best bump for *request*.

    Real assets are tried first. Generated bumps are only considered when nothing
    real clears *min_score*, which keeps the synthetic share honest.
    """
    pool = list(pool)
    cooling = on_cooldown or set()
    ranked = rank(pool, request, on_cooldown=cooling)

    # If every bump that names the right show is cooling down, prefer a repeat
    # over a neutral card: the card naming the right show is still more accurate,
    # and a recycled bump is what a real channel would do too.
    if not [item for item in ranked if item.score >= min_score]:
        thawed_candidates = [
            item
            for item in rank(pool, request, on_cooldown=set())
            if item.score >= min_score
        ]
        if thawed_candidates:
            picked = _rotate(thawed_candidates, seed_parts)
            return BumpChoice(
                picked.asset,
                picked.score,
                fallback=False,
                reason="all-cooling," + ",".join(picked.reasons),
            )

    viable = [item for item in ranked if item.score >= min_score]

    if viable:
        top = viable[0].score
        # within a hair of the best score, rotate so consecutive breaks differ
        contenders = [item for item in viable if item.score >= top - 1.0]
        picked = _rotate(contenders, seed_parts)
        return BumpChoice(picked.asset, picked.score, fallback=False, reason=",".join(picked.reasons))

    if strictness == "off" and ranked:
        picked = _rotate(ranked[: max(1, len(ranked) // 2)], seed_parts)
        return BumpChoice(
            picked.asset, picked.score, fallback=False, reason="strictness=off," + ",".join(picked.reasons)
        )

    # A generated card that names the right show beats a neutral real card: the
    # whole point of the generator is to say the specific thing the archive
    # cannot. It still ranks below any *real* asset that names the show, which is
    # what keeps generated cards out of the way when the library is complete.
    generated = list(generated)
    if generated:
        gen_ranked = [item for item in rank(generated, request, on_cooldown=on_cooldown) if item.score >= min_score]
        if gen_ranked:
            picked = _rotate(gen_ranked, seed_parts)
            return BumpChoice(
                picked.asset, picked.score, fallback=False, reason="generated," + ",".join(picked.reasons)
            )

    neutral = [asset for asset in pool if asset.is_neutral_bump]
    neutral_ranked = rank(
        neutral, BumpRequest(position=request.position, year=request.year), on_cooldown=on_cooldown
    )
    if neutral_ranked:
        picked = _rotate(neutral_ranked[:3], seed_parts)
        return BumpChoice(picked.asset, picked.score, fallback=True, reason="neutral-fallback")

    return BumpChoice(None, 0.0, fallback=True, reason="no-eligible-bump")


def _rotate(candidates: Sequence[Scored], seed_parts: Sequence[object]) -> Scored:
    if len(candidates) == 1:
        return candidates[0]
    rng = rng_for("bump-rotate", *seed_parts)
    weights = [1.0 / (index + 1) for index in range(len(candidates))]
    return weighted_choice(rng, list(candidates), weights)


def fits(asset: Asset, max_seconds: float | None) -> bool:
    if max_seconds is None:
        return True
    return asset.duration_ms <= max_seconds * 1000 + 0.5


def apply_cooldowns(
    pool: Iterable[Asset],
    last_aired_ms: dict[str, int],
    *,
    now_ms: int,
    cooldown_ms: int,
) -> set[str]:
    """Keys still inside their cooldown window at *now_ms*."""
    if cooldown_ms <= 0:
        return set()
    return {
        asset.key
        for asset in pool
        if asset.key in last_aired_ms and now_ms - last_aired_ms[asset.key] < cooldown_ms
    }


__all__ = [
    "BumpChoice",
    "BumpRequest",
    "Scored",
    "apply_cooldowns",
    "choose",
    "fits",
    "rank",
    "score_asset",
]

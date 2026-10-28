"""Bump matching -- the reason this project exists.

ErsatzTV, Tunarr, DizqueTV and every other streamer treat filler as anonymous.
A 2003 [adult swim] break is not anonymous: the card that says *"next up: Futurama"*
belongs immediately before *Futurama*, and the one that says *"we now return to
Aqua Teen"* belongs immediately after *Aqua Teen*. Only the scheduler knows the
adjacency, so only the scheduler can choose the bump.

Scoring is deliberately **lenient but never wrong-on-purpose**. The distinction
that carries the whole design is not *which* show a card names but whether it
makes a **claim** about the running order:

* "Deathklok returns with new episodes" playing during Aqua Teen is a promo. It
  is a real 2003 card, it is topical rather than positional, and a real channel
  would absolutely have aired it there. Allowed.
* "Next up: Futurama" in front of Aqua Teen is a *lie*. It contradicts the thing
  the viewer can immediately check. Never allowed, however thin the pool.

That is the rule "semi-strict" means here, and it is why the positional tags in
``config/bumps.yaml`` matter more than the subject tags.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .assets import NEUTRAL, Asset
from .rng import rng_for, weighted_choice

POSITION_EXACT = 3.0
POSITION_ADJACENT = 1.0
POSITION_UNKNOWN = 0.5

#: The subject ladder. Every rung is worth at least one position grade (1.0), so
#: a better *subject* can never be outbid by a better-fitting position. That
#: monotonicity is what lets :func:`choose` take a band around the best score
#: without ever handing the slot to a card about the wrong show.
SUBJECT_EXACT = 5.0

SUBJECT_FAMILY = 3.5
"""Another show in the same family -- the nearest thing to the right card."""

SUBJECT_OTHER = 2.0
"""A promo for an unrelated show.

Above a general card on purpose: a real card from the archive is the thing being
recreated, and a promo for a neighbouring show is both more authentic and more
informative than a card that says nothing. The general pool still airs -- via
``promo_share`` while named cards exist, and as the fallback when none does.
"""

SUBJECT_NEUTRAL = 1.0
"""A general card."""

SUBJECT_WRONG = -100.0
"""A card that would contradict the running order. Never eligible."""

SUBJECT_WEIGHT: dict[str, float] = {
    "exact": SUBJECT_EXACT,
    "family": SUBJECT_FAMILY,
    "promo": SUBJECT_OTHER,
    "neutral": SUBJECT_NEUTRAL,
}
"""What each tier contributes, so it can be scaled back out for comparison."""

ERA_MATCH = 1.0
ERA_NEAR = 0.25
FAMILY_ONLY = 0.5
COOLDOWN_PENALTY = 2.0

#: Positions whose *fit* is close enough to earn the partial position bonus.
#:
#: A scoring hint only. It says "a card from the other side of this moment still
#: fits better than nothing", which is true of ``INTO``/``NEXT`` and of
#: ``OUT``/``AFTER``. It does **not** grant permission: whether the printed phrase
#: is true here is :data:`PERMITTED_MOMENTS`, which is strictly narrower. Keeping
#: the two apart is what stops a "WE NOW RETURN TO" card being handed to an
#: ``AFTER`` slot because the two scored as adjacent.
ADJACENT_POSITIONS: dict[str, set[str]] = {
    "INTO": {"NEXT", "ANY"},
    "OUT": {"AFTER", "ANY"},
    "NEXT": {"INTO", "ANY"},
    "AFTER": {"OUT", "ANY"},
    "ANY": {"INTO", "OUT", "NEXT", "AFTER"},
}

#: Positions that assert something about the *sequence* rather than the schedule.
#:
#: "Up next", "we now return to", "you just watched" all make a claim the viewer
#: can check against the next thing that starts. This is what
#: :data:`SUBJECT_WRONG` is reserved for -- the wrong show is merely unhelpful,
#: but the wrong show *plus* a positional claim is incorrect.
POSITIONAL_CLAIMS: frozenset[str] = frozenset({"NEXT", "AFTER", "INTO", "OUT"})

#: The slots a card carrying *position* may truthfully occupy.
#:
#: This is the correctness table, and it is deliberately narrower than
#: :data:`ADJACENT_POSITIONS`: that one is a scoring hint ("these moments are
#: similar enough that a good-fit bonus is fair"), while this one decides whether a
#: printed sentence is TRUE. Conflating the two is what let a "WE NOW RETURN TO
#: King of the Hill" card air immediately after King of the Hill had finished --
#: ``OUT`` and ``AFTER`` were adjacent for scoring, so an ``OUT`` card was handed
#: to an ``AFTER`` slot, and the phrase is false there by definition.
#:
#: Each phrase names exactly one moment:
#:
#: * ``STAY TUNED FOR <show>`` -- the show is about to *start*. ``INTO`` only. At
#:   a resume ``INTO`` the episode is already under way, and before the next slot
#:   the thing starting is that slot's show, not this one.
#: * ``WE NOW RETURN TO <show>`` -- the show is coming *back*. ``OUT``, and also
#:   the resume ``INTO`` immediately after it (granted by
#:   :func:`_contradicts` when :attr:`BumpRequest.resumes`, since that ``INTO`` is
#:   the return). Never ``AFTER``: there the episode has ended and nothing returns.
#: * ``YOU JUST WATCHED <show>`` -- the episode finished. ``AFTER`` only.
#: * ``NEXT: <show>`` -- what plays after this *episode*. ``NEXT``, and anywhere
#:   after the episode has ended. Never ``INTO`` or ``OUT``, including a resume
#:   ``INTO``: what resumes there is the current episode, not another show.
#:
#: A card with no position names nothing about the running order, so it is absent
#: here and unconstrained everywhere.
PERMITTED_MOMENTS: dict[str, frozenset[str]] = {
    "INTO": frozenset({"INTO"}),
    "OUT": frozenset({"OUT"}),
    "AFTER": frozenset({"AFTER"}),
    "NEXT": frozenset({"NEXT", "AFTER"}),
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
    allow_other_shows: bool = True
    """Whether a promo for another show may fill this slot (see ``bumps.yaml``).

    Carried on the request rather than on :func:`score_asset` because it is a
    property of the *slot being filled*, not of the card being considered.
    """

    resumes: bool = False
    """True for the `INTO` that returns from a mid-episode break.

    Two `INTO` cards exist in a break and they are not the same moment: the one
    before the episode announces what is about to *start*, while the one after a
    mid-roll pod says the episode is continuing. "Stay tuned for <show>" and "we
    now return to <show>" are statements about opposite directions in time, so the
    distinction has to reach the matcher rather than being averaged away.
    """

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
    tier: str = "neutral"
    """``exact`` | ``family`` | ``promo`` | ``neutral``. Never ``wrong``: those
    score below zero and :func:`rank` drops them."""


@dataclass
class BumpChoice:
    asset: Asset | None
    score: float
    fallback: bool
    """True when no asset could name the right show and a neutral one was used."""

    reason: str


def subject_tier(asset: Asset, request: BumpRequest) -> str:
    """How *asset* relates to the show *request* asked for.

    Kept separate from the score so the planner can reason about *tiers* -- "is
    there a card for this show at all?" -- instead of comparing floats against a
    single threshold. Those are different questions, and conflating them is what
    made a promo for a neighbouring show permanently unusable.
    """
    wanted = request.resolved_subject()
    if asset.is_neutral_bump:
        # A general card names nothing, so it can never make a false claim --
        # including in the up-next slot, where saying nothing is always true.
        return "neutral"
    if not wanted or wanted == NEUTRAL:
        return "promo"
    if asset.subject == wanted:
        # Naming the right show is not enough if the card claims the wrong *side*
        # of it: "we now return to Aqua Teen" in front of Aqua Teen says its
        # episode has already finished.
        return "wrong" if _contradicts(asset, request) else "exact"
    if request.family and asset.family == request.family:
        return "family"
    if _contradicts(asset, request):
        return "wrong"
    if not request.allow_other_shows:
        # Still *ineligible*, not neutral: the card names a show, so airing it
        # here presents it as a general card and keeps the very claim the setting
        # was meant to suppress.
        return "wrong"
    return "promo"


def _meets_bar(item: Scored, min_score: float) -> bool:
    """Whether *item* fits its *moment* well enough to air.

    ``min_score`` was written against a perfect subject match, so it cannot be
    compared with a promo's raw score: a flawless promo tops out around 2.0, and
    demanding 6.0 of it would reject every one -- which is exactly the bug that
    emptied half the break slots. The subject's own weight is scaled out first, so
    every tier is judged on position and era, which is what "fits this moment"
    actually means. A cooldown still counts against it.
    """
    residual = item.score - SUBJECT_WEIGHT.get(item.tier, 0.0)
    return residual >= min_score - SUBJECT_EXACT


def score_asset(asset: Asset, request: BumpRequest, *, cool: bool) -> tuple[float, list[str]]:
    """Score *asset* against *request*. Returns ``(score, reasons)``.

    A score of 0 or less means "ineligible", not "last choice".
    """
    reasons: list[str] = []
    score = 0.0

    wanted = request.resolved_subject()
    specific_slot = bool(wanted) and wanted != NEUTRAL
    tier = subject_tier(asset, request)

    if tier == "wrong":
        # Naming another show is only a problem if the card *also* claims a place
        # in the running order. "Next up: Futurama" before Aqua Teen is wrong in a
        # way the viewer can see; a Deathklok promo airing during Aqua Teen is
        # just a promo, and refusing those was costing the channel ~40% of its
        # break cards.
        return SUBJECT_WRONG, [f"{asset.subject!r} claims {asset.position} out of place"]
    score += SUBJECT_WEIGHT.get(tier, 0.0)
    if tier == "exact":
        reasons.append(f"subject={wanted}")
    elif tier == "family":
        reasons.append(f"family={request.family}")
    elif tier == "promo":
        reasons.append(f"promo for {asset.subject}" if specific_slot else "neutral slot")
    elif tier == "neutral":
        reasons.append("subject=neutral")

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

    # Reached only for a *generic* slot (no show named): "up next" still promises
    # a specific show, so a card naming one is a claim the planner cannot check
    # and therefore must not make. A general card claims nothing and is fine.
    if not specific_slot and asset.subject not in (None, "", NEUTRAL):
        if request.position in POSITIONAL_CLAIMS:
            return SUBJECT_WRONG, [f"'{request.position}' needs a known subject"]

    if request.family and asset.family == request.family:
        score += FAMILY_ONLY

    if cool:
        score -= COOLDOWN_PENALTY
        reasons.append("cooldown")

    return score, reasons


def _contradicts(asset: Asset, request: BumpRequest) -> bool:
    """Whether *asset* claims a place in the running order it cannot honour.

    A general card claims nothing, so it can never contradict anything. For the
    rest, a claim is true when it names the right show *for that moment*:

    * **Before the episode** (`INTO`/`NEXT`) the truthful subject is the one
      about to start -- the next show, or the current one at an `INTO`.
    * **After the episode** (`OUT`/`AFTER`) it is the one that just ended.
    * **Wrong side.** "We now return to Aqua Teen" in front of Aqua Teen says its
      episode has already finished. That is how an `AFTER` card once opened a
      slot, and the only salvage is a card naming the show that truly comes next,
      where "up next: Brak" is accurate from anywhere in the break.
    * **Wrong moment.** A card's phrase names one moment -- "we now return to"
      only where the show comes back, "you just watched" only once it has ended --
      so it may not sit in a slot that is a different moment, even when it names
      exactly the right show. See :data:`PERMITTED_MOMENTS`.
    """
    if asset.subject is None or asset.subject == NEUTRAL:
        return False
    if asset.position not in POSITIONAL_CLAIMS:
        # An untagged promo says nothing about the schedule, so there is nothing
        # it can get wrong. This is the common case and the relaxation itself.
        return False

    # The closing card *is* the "up next" announcement, so only a card labelled
    # NEXT belongs there: anything else describes a moment this slot has already
    # left behind. That is how an INTO card for Aqua Teen ended up promising more
    # Aqua Teen at the end of a slot that lead into Sealab.
    if request.position == "NEXT" and asset.position != "NEXT":
        return True

    # Whether the *phrase* is sayable here at all. A "WE NOW RETURN TO King of the
    # Hill" card is false the moment King of the Hill has finished, so it may not
    # be handed to an `AFTER` slot however thin the pool is.
    permitted = PERMITTED_MOMENTS.get(asset.position)
    if permitted is not None:
        allowed = set(permitted)
        if request.resumes:
            # The `INTO` that returns from a mid-roll pod is the one place the two
            # phrases meet: "we now return to <show>" belongs there, and "stay
            # tuned for <show>" is merely odd rather than false. Allowing both is
            # what keeps the most recognisable moment of the break schedulable.
            if asset.position == "OUT":
                allowed.add("INTO")
        if request.position not in allowed:
            return True

    # A card's *own* label says which moment it describes, which fixes which show
    # it must name -- regardless of which slot it sits in. "Up next: The Brak
    # Show" is accurate from anywhere before Brak; "we now return to Aqua Teen" is
    # only accurate when Aqua Teen is what this slot is about.
    required = request.next_show if asset.position == "NEXT" else request.this_show
    return asset.subject != required


def _position_agrees(asset: Asset, position: str) -> bool:
    """Whether *asset* fits the *moment* as well as a card that says nothing."""
    if asset.position is None:
        return True
    return asset.position == position or asset.position in ADJACENT_POSITIONS.get(position, {"ANY"})


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
        scored.append(
            Scored(
                asset=asset,
                score=score,
                reasons=reasons,
                tier=subject_tier(asset, request),
            )
        )
    scored.sort(key=lambda item: (-item.score, item.asset.key))
    return scored


#: Tiers that mean "this card names the show the slot asked for".
NAMED_TIERS: frozenset[str] = frozenset({"exact", "family", "promo"})


def choose(
    pool: Iterable[Asset],
    request: BumpRequest,
    *,
    min_score: float,
    strictness: str = "semi",
    on_cooldown: set[str] | None = None,
    seed_parts: Sequence[object] = (),
    generated: Iterable[Asset] = (),
    promo_share: float = 0.0,
) -> BumpChoice:
    """Pick the best bump for *request*.

    The ladder, in order:

    1. A real card that **names** the target show -- exactly, by family, or as a
       promo for a neighbouring show. A share of picks goes to a *general* card
       instead, so the general pool keeps airing rather than being crowded out.
    2. The show's own card again, if a cooldown is the only thing excluding it.
    3. ``strictness: off`` -- the whole ranked pool.
    4. A **generated** card that names the show, if the budget allows.
    5. A **neutral** card, which is the honest thing to air.

    *min_score* gates how well a card fits the *moment* (position and era). It
    deliberately no longer decides whether a promo may air -- that is a question
    about the subject, and a single global threshold was answering it wrongly.
    """
    pool = list(pool)
    cooling = on_cooldown or set()
    # Cooldown is applied *after* scoring everywhere below, so a card that the
    # board excludes on other grounds can never sneak back in via the cooldown
    # pass. This used to re-rank the raw pool, which re-admitted promos the
    # request had ruled out and put them at the end of a slot.
    ranked = rank(pool, request, on_cooldown=cooling)
    thawed_all = rank(pool, request, on_cooldown=set())

    viable = [item for item in ranked if _meets_bar(item, min_score)]

    # A recycled correct bump beats a fresh generic one, as it always did. The
    # cooling card has to be compared *by tier* against what is available warm:
    # once promos became eligible, "nothing viable" stopped happening and this
    # rescue never ran, so a single cooldown demoted the show's own card below a
    # promo for a different show.
    if viable and not any(item.tier == "exact" for item in viable):
        better = _cooled_upgrade(thawed_all, viable[0], min_score)
        if better is not None:
            picked = _rotate(_best_band(better), seed_parts)
            return BumpChoice(
                picked.asset,
                picked.score,
                fallback=False,
                reason="all-cooling," + ",".join(picked.reasons),
            )

    named = [item for item in viable if item.tier in NAMED_TIERS]
    general = [item for item in viable if item.tier == "neutral"]

    # A share of picks deliberately goes to a *general* card even when a named one
    # is available. [adult swim] never ran a card for the show you were watching
    # at every single break, and a channel that does sounds like a DVD menu. This
    # is the knob that keeps the general pool in rotation.
    #
    # Gated on the show having *something* of its own, exact or not: when it has
    # nothing, the general card is already the outcome, so choosing it "by share"
    # would relabel the gap the owner should be fixing.
    if named and general and promo_share > 0:
        if rng_for("bump-kind", *seed_parts).random() < promo_share:
            picked = _rotate(_best_band(general), seed_parts)
            return BumpChoice(
                picked.asset,
                picked.score,
                fallback=False,
                reason="general-by-share," + ",".join(picked.reasons),
            )

    if named:
        picked = _rotate(_best_band(named), seed_parts)
        return BumpChoice(
            picked.asset,
            picked.score,
            fallback=False,
            reason=",".join(picked.reasons),
        )

    if strictness == "off" and ranked:
        picked = _rotate(ranked[: max(1, len(ranked) // 2)], seed_parts)
        return BumpChoice(
            picked.asset,
            picked.score,
            fallback=False,
            reason="strictness=off," + ",".join(picked.reasons),
        )

    # A generated card that names the right show beats a neutral real card: the
    # whole point of the generator is to say the specific thing the archive
    # cannot. It still ranks below any *real* asset that names the show, which is
    # what keeps generated cards out of the way when the library is complete.
    generated = list(generated)
    if generated:
        gen_ranked = [
            item
            for item in rank(generated, request, on_cooldown=on_cooldown)
            if item.tier in NAMED_TIERS
        ]
        if gen_ranked:
            picked = _rotate(gen_ranked, seed_parts)
            return BumpChoice(
                picked.asset, picked.score, fallback=False, reason="generated," + ",".join(picked.reasons)
            )

    # Last resort: reuse the general cards from the main ranking rather than
    # re-deriving them. The previous version re-ranked a `neutral`-only pool with
    # a subject-free request, which silently decided which cards counted as
    # neutral a second, different way.
    neutral = [item for item in ranked if item.tier == "neutral"]
    if not neutral:
        neutral = rank(
            [asset for asset in pool if asset.is_neutral_bump],
            BumpRequest(
                position=request.position,
                year=request.year,
                allow_other_shows=request.allow_other_shows,
            ),
            on_cooldown=on_cooldown,
        )
    if neutral:
        picked = _rotate(_best_band(neutral)[:3], seed_parts)
        # Always a fallback by definition: reaching here means a general card
        # stood in for one naming the show, which is the gap to report. Earlier
        # branches never return a neutral card, so there is no other case.
        return BumpChoice(picked.asset, picked.score, fallback=True, reason="neutral-fallback")

    return BumpChoice(None, 0.0, fallback=True, reason="no-eligible-bump")


def _cooled_upgrade(
    thawed: Sequence[Scored],
    warm_best: Scored,
    min_score: float,
) -> list[Scored] | None:
    """Exact cards excluded only by cooldown, if they beat what would air instead.

    A recycled correct bump beats a fresh generic one, which is what a real
    channel does too. *warm_best* is the best card that would otherwise air, so
    the comparison is against that -- not against a threshold, which is why this
    can only ever improve a decision.
    """
    cooled = [
        item
        for item in thawed
        if item.tier == "exact" and _meets_bar(item, min_score)
    ]
    if not cooled:
        return None
    if cooled[0].score - COOLDOWN_PENALTY < warm_best.score:
        return None
    return cooled


def _best_band(candidates: Sequence[Scored]) -> list[Scored]:
    """The candidates shareable with the best one, so breaks do not repeat.

    A *band*, not a rate: rotating across the whole viable list would let a promo
    for a neighbouring show appear as often as the show's own card, and a card
    naming the wrong show must never outrank the right one for the same moment.
    """
    if not candidates:
        return []
    best = candidates[0]
    # Tie-break on *fit*, not on the tier's own weight: the weight is what makes
    # tiers dominate each other, so including it here would leave a single card
    # in every band and nothing to rotate through.
    best_fit = best.score - SUBJECT_WEIGHT.get(best.tier, 0.0)
    return [
        item
        for item in candidates
        if item.tier == best.tier
        and item.score - SUBJECT_WEIGHT.get(item.tier, 0.0) >= best_fit - 1.0
    ]


def _rotate(candidates: Sequence[Scored], seed_parts: Sequence[object]) -> Scored:
    """Weighted pick, favouring the best-ranked candidates."""
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

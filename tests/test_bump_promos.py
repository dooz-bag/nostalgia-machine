"""Cross-show promos: the rule the archive actually contains.

A complete [adult swim] break ran *promos for other shows*, not cards about the
one currently airing. The old matcher treated every wrong subject identically and
rejected all of them, so roughly half the channel's break slots fell back to a
generic card -- accuracy bought at the cost of the thing being recreated.

The distinction that matters is not *which* show a card names but whether it
makes a **positional claim**:

    "Deathklok returns with new episodes"  ->  a promo. Fine anywhere.
    "Next up: Deathklok"                   ->  a lie unless Deathklok is next.

These tests pin that boundary in both directions, because the failure mode of
getting it wrong is a channel that either lies to the viewer or quietly stops
being [adult swim].
"""

from __future__ import annotations

import pytest

from nostalgia.assets import NEUTRAL, Asset
from nostalgia.bumps import (
    BumpRequest,
    choose,
    rank,
    score_asset,
    subject_tier,
)


def _bump(
    key: str,
    subject: str | None,
    position: str | None = None,
    *,
    era: int | None = 2003,
    duration: int = 4_200,
) -> Asset:
    return Asset(
        key=key,
        path=f"/media/bumps/{key}.mkv",
        kind="bump",
        duration_ms=duration,
        subject=subject,
        position=position,
        era_from=era,
        era_to=era,
    )


def _request(position: str = "INTO", **kwargs) -> BumpRequest:
    """A break *during* Aqua Teen, with Deathklok on next."""
    return BumpRequest(
        position=position,
        this_show="aqua-teen-hunger-force",
        next_show="the-brak-show",
        year=2003,
        **kwargs,
    )


# -- the two halves of the rule -------------------------------------------


def test_a_promo_without_a_positional_claim_is_allowed() -> None:
    promo = _bump("deathklok-promo", "the-venture-bros", None)
    score, reasons = score_asset(promo, _request(), cool=False)
    assert score > 0, reasons
    assert any("promo" in reason for reason in reasons)


def test_the_same_promo_claiming_next_up_is_refused() -> None:
    """"Next up: Venture Bros" during Aqua Teen is the lie we must never tell."""
    promo = _bump("deathklok-next", "the-venture-bros", "NEXT")
    score, reasons = score_asset(promo, _request(), cool=False)
    assert score < 0, reasons


def test_a_card_claiming_the_sequence_is_refused_in_any_claimed_slot() -> None:
    for slot_position in ("INTO", "OUT", "NEXT", "AFTER"):
        promo = _bump("other-claims", "the-venture-bros", "OUT")
        score, _ = score_asset(promo, _request(position=slot_position), cool=False)
        assert score < 0, f"a positional claim survived a {slot_position} slot"


def test_a_card_for_the_show_that_actually_follows_is_fine_anywhere() -> None:
    """It says "up next" and the next show *is* it, so the claim is always true.

    Including at the end of the break -- that is precisely where an "up next" card
    belongs, and rejecting it there would have made the most recognisable moment
    on the channel impossible to schedule.
    """
    upcoming = _bump("brak-next", "the-brak-show", "NEXT")
    for slot_position in ("INTO", "OUT", "NEXT", "AFTER", "ANY"):
        score, reasons = score_asset(upcoming, _request(position=slot_position), cool=False)
        assert score > 0, (slot_position, reasons)


def test_the_closing_card_must_be_labelled_as_an_up_next() -> None:
    """Only an "up next" card can close a slot: the others describe a past moment.

    An `INTO` card for the show that just played would promise more of it right
    before a *different* show starts, which is how one used to end up there.
    """
    request = _request(position="NEXT")
    into_card = _bump("athf-into", "aqua-teen-hunger-force", "INTO")
    next_card = _bump("brak-next", "the-brak-show", "NEXT")
    assert score_asset(into_card, request, cool=False)[0] < 0
    assert score_asset(next_card, request, cool=False)[0] > 0


def test_a_card_about_this_show_at_this_spot_is_fine() -> None:
    into = _bump("athf-into", "aqua-teen-hunger-force", "INTO")
    score, reasons = score_asset(into, _request(position="INTO"), cool=False)
    assert score > 0, reasons


def test_a_card_for_this_show_may_open_the_slot_it_belongs_to() -> None:
    into = _bump("athf-into", "aqua-teen-hunger-force", "INTO")
    score, reasons = score_asset(into, _request(position="INTO"), cool=False)
    assert score > 0, reasons


def test_a_general_card_is_never_a_contradiction() -> None:
    for slot_position in ("INTO", "OUT", "NEXT", "AFTER", "ANY"):
        general = _bump("general", NEUTRAL, "NEXT")
        score, reasons = score_asset(general, _request(position=slot_position), cool=False)
        assert score > 0, (slot_position, reasons)


# -- ordering: a promo may air, but must not win against the right card ---


def test_the_right_shows_card_still_outranks_a_promo() -> None:
    pool = [
        _bump("athf-into", "aqua-teen-hunger-force", "INTO"),
        _bump("promo", "the-venture-bros", None),
    ]
    ranked = rank(pool, _request())
    assert ranked[0].asset.subject == "aqua-teen-hunger-force"


def test_a_real_archive_promo_outranks_a_general_card() -> None:
    """A card that says *something* beats one that says nothing.

    2003 breaks were largely promos for other programmes, so a topical card is
    both more authentic and more informative than a generic one. The general pool
    still airs -- it is what ``promo_share`` reserves and what the fallback uses.
    """
    pool = [
        _bump("promo", "the-venture-bros", None),
        _bump("general", NEUTRAL, None),
    ]
    ranked = rank(pool, _request())
    assert ranked[0].asset.subject == "the-venture-bros"


def test_a_promo_is_used_when_the_show_has_no_cards_of_its_own() -> None:
    """This is the whole point: the archive has no Sealab cards, but has promos."""
    pool = [
        _bump("brak-promo", "the-brak-show", None),
        _bump("general", NEUTRAL, None),
    ]
    choice = choose(pool, _request(), min_score=6.0)
    assert choice.asset is not None
    assert choice.asset.subject == "the-brak-show"
    # A promo is intended behaviour, so it is not reported as a library gap.
    assert not choice.fallback


def test_the_promo_can_be_switched_off_entirely() -> None:
    pool = [
        _bump("brak-promo", "the-brak-show", None),
        _bump("general", NEUTRAL, None),
    ]
    choice = choose(pool, _request(allow_other_shows=False), min_score=6.0)
    assert choice.asset is not None
    assert choice.asset.is_neutral_bump


# -- the general pool must keep airing -----------------------------------


def test_general_cards_still_get_a_share_when_the_show_has_its_own() -> None:
    """A channel where every break names the show sounds like a DVD menu.

    Determinism means some seeds land on the exact card and some on the general
    one; what matters is that *both* outcomes occur across seeds.
    """
    pool = [
        _bump("athf-into", "aqua-teen-hunger-force", "INTO"),
        _bump("general-1", NEUTRAL, None),
        _bump("general-2", NEUTRAL, None),
    ]
    picks = {
        choose(pool, _request(), min_score=6.0, promo_share=0.3, seed_parts=("d", slot, 0)).asset.key
        for slot in range(200)
    }
    assert "athf-into" in picks, "the exact card never aired"
    assert picks & {"general-1", "general-2"}, "no general card ever aired"


def test_the_general_share_is_a_rate_not_a_guarantee() -> None:
    """The share is honoured as configured, and defaults to none at all."""
    pool = [
        _bump("athf-into", "aqua-teen-hunger-force", "INTO"),
        _bump("general-1", NEUTRAL, None),
    ]
    seeds = [("d", slot, 0) for slot in range(200)]
    never = sum(
        choose(pool, _request(), min_score=6.0, seed_parts=seed).asset.key == "general-1"
        for seed in seeds
    )
    always = sum(
        choose(pool, _request(), min_score=6.0, promo_share=1.0, seed_parts=seed).asset.key
        == "general-1"
        for seed in seeds
    )
    assert never == 0, "the default must prefer the show's own card"
    assert always == len(seeds)


def test_a_share_of_zero_always_prefers_the_exact_card() -> None:
    pool = [
        _bump("athf-into", "aqua-teen-hunger-force", "INTO"),
        _bump("general-1", NEUTRAL, None),
    ]
    for slot in range(20):
        choice = choose(pool, _request(), min_score=6.0, promo_share=0.0, seed_parts=("d", slot))
        assert choice.asset.key == "athf-into"


def test_a_share_of_one_still_only_airs_general_cards() -> None:
    pool = [
        _bump("athf-into", "aqua-teen-hunger-force", "INTO"),
        _bump("general-1", NEUTRAL, None),
    ]
    for slot in range(20):
        choice = choose(pool, _request(), min_score=6.0, promo_share=1.0, seed_parts=("d", slot))
        assert choice.asset.key == "general-1"


# -- the fallback signal must stay meaningful ----------------------------


def test_a_promo_is_not_reported_as_a_missing_card() -> None:
    """Fallbacks are the signal for "this show needs cards". A promo is not that."""
    pool = [_bump("brak-promo", "the-brak-show", None)]
    choice = choose(pool, _request(), min_score=6.0)
    assert not choice.fallback


def test_a_general_card_standing_in_for_a_missing_show_is_reported() -> None:
    pool = [_bump("general", NEUTRAL, None)]
    choice = choose(pool, _request(), min_score=6.0)
    assert choice.fallback
    assert "neutral-fallback" in choice.reason


# -- tiers are the stable vocabulary the planner reasons about ------------


def test_subject_tiers_describe_each_relationship() -> None:
    request = _request()
    assert subject_tier(_bump("a", "aqua-teen-hunger-force", "INTO"), request) == "exact"
    assert subject_tier(_bump("b", NEUTRAL, None), request) == "neutral"
    assert subject_tier(_bump("c", "the-venture-bros", None), request) == "promo"
    assert subject_tier(_bump("d", "the-venture-bros", "NEXT"), request) == "wrong"


def test_tier_ordering_survives_repeated_ranking() -> None:
    """Ranking is pure: scoring the same pool twice must not drift."""
    pool = [
        _bump("athf-into", "aqua-teen-hunger-force", "INTO"),
        _bump("promo", "the-venture-bros", None),
        _bump("general", NEUTRAL, None),
    ]
    first = [(item.asset.key, item.score) for item in rank(pool, _request())]
    second = [(item.asset.key, item.score) for item in rank(pool, _request())]
    assert first == second


@pytest.mark.parametrize("seed", [("d", 0, 0), ("d", 5, 2), ("d", 71, 4)])
def test_choice_is_repeatable_for_the_same_seed(seed) -> None:
    pool = [
        _bump("athf-into", "aqua-teen-hunger-force", "INTO"),
        _bump("promo", "the-venture-bros", None),
        _bump("general", NEUTRAL, None),
    ]
    first = choose(pool, _request(), min_score=6.0, seed_parts=seed)
    second = choose(pool, _request(), min_score=6.0, seed_parts=seed)
    assert first.asset.key == second.asset.key

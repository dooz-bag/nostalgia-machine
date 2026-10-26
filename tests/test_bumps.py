"""Bump matching: the accuracy rule that motivates the whole project."""

from __future__ import annotations

import pytest

from nostalgia.assets import NEUTRAL, Asset
from nostalgia.bumps import BumpRequest, choose, rank, score_asset


def _bump(
    key: str,
    subject: str | None,
    position: str | None = None,
    *,
    era: int | None = None,
    family: str | None = None,
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
        family=family,
    )


@pytest.fixture()
def pool() -> list[Asset]:
    return [
        _bump("athf-into", "athf", "INTO", era=2003, family="comedy"),
        _bump("athf-after", "athf", "AFTER", era=2003, family="comedy"),
        _bump("futurama-next", "futurama", "NEXT", era=2003, family="sitcom"),
        _bump("futurama-into", "futurama", "INTO", era=2003, family="sitcom"),
        _bump("neutral-1", NEUTRAL, "ANY", era=2003),
        _bump("neutral-2", None, None, era=2003),
    ]


def _request(subject: str = "this", position: str = "INTO", **kwargs) -> BumpRequest:
    return BumpRequest(
        subject=subject,
        position=position,
        this_show="athf",
        next_show="futurama",
        year=2003,
        family="comedy",
        **kwargs,
    )


def test_right_show_beats_neutral(pool: list[Asset]) -> None:
    choice = choose(pool, _request(), min_score=6.0)
    assert choice.asset is not None
    assert choice.asset.subject == "athf"
    assert not choice.fallback


def test_wrong_show_is_never_chosen(pool: list[Asset]) -> None:
    """A card that would make a false positional claim is never chosen."""
    request = _request()  # Aqua Teen playing, Futurama up next
    choice = choose(pool, request, min_score=6.0)
    # futurama-into claims "we now return to Futurama" while Aqua Teen is on;
    # futurama-next claims it is up next, which is false too.
    assert choice.asset.key not in {"futurama-into", "futurama-next"}


def test_next_bump_matches_the_following_show(pool: list[Asset]) -> None:
    request = BumpRequest(
        subject="next",
        position="NEXT",
        this_show="athf",
        next_show="futurama",
        year=2003,
        family="comedy",
    )
    choice = choose(pool, request, min_score=6.0)
    assert choice.asset.subject == "futurama"


def test_position_affects_ranking(pool: list[Asset]) -> None:
    into = rank(pool, _request(position="INTO"))[0]
    after = rank(pool, _request(position="AFTER"))[0]
    assert into.asset.position == "INTO"
    assert after.asset.position == "AFTER"


def test_missing_show_uses_a_promo_before_a_neutral_card(pool: list[Asset]) -> None:
    """The archive has no Sealab cards, but it has promos for other shows.

    Those are what a real 2003 break was made of, so they are the *first* choice
    when a show is uncovered -- not a last resort.
    """
    request = BumpRequest(
        subject="this",
        position="INTO",
        this_show="sealab",  # no sealab bumps in the pool
        next_show="futurama",
        year=2003,
    )
    choice = choose(pool, request, min_score=6.0)
    assert not choice.fallback
    assert choice.asset is not None
    assert choice.asset.subject != "sealab"


def test_missing_show_falls_back_to_neutral_when_promos_are_off(pool: list[Asset]) -> None:
    request = BumpRequest(
        subject="this",
        position="INTO",
        this_show="sealab",
        next_show="futurama",
        year=2003,
        allow_other_shows=False,
    )
    choice = choose(pool, request, min_score=6.0)
    assert choice.fallback
    assert choice.asset is not None
    assert choice.asset.is_neutral_bump


def test_no_bumps_at_all_returns_none() -> None:
    choice = choose([], _request(), min_score=6.0)
    assert choice.asset is None
    assert choice.fallback


def test_family_breaks_ties_between_wrong_shows(pool: list[Asset]) -> None:
    request = BumpRequest(
        subject="this",
        position="INTO",
        this_show="home-movies",
        year=2003,
        family="comedy",
    )
    ranked = rank(pool, request)
    # a comedy bump may outrank a sitcom bump for a comedy show
    assert ranked[0].asset.family == "comedy"


def test_cooldown_lowers_the_score(pool: list[Asset]) -> None:
    request = _request()
    warm, _ = score_asset(pool[0], request, cool=False)
    cooling, _ = score_asset(pool[0], request, cool=True)
    assert cooling < warm


def test_cooling_bump_beats_neutral(pool: list[Asset]) -> None:
    cooling = {asset.key for asset in pool if asset.subject == "athf"}
    choice = choose(pool, _request(), min_score=6.0, on_cooldown=cooling)
    assert choice.asset.subject == "athf"
    assert not choice.fallback
    assert "cooldown" in choice.reason


def test_thawed_choice_when_cooldown_drops_it_below_the_bar() -> None:
    # A high enough min_score means a cooling bump no longer clears it, but a
    # repeat naming the right show is still better than a neutral card.
    pool = [
        _bump("athf-into", "athf", "INTO", era=2003),
        _bump("neutral-1", NEUTRAL, "INTO", era=2003),
    ]
    request = _request()
    warm, _ = score_asset(pool[0], request, cool=False)
    choice = choose(
        pool,
        request,
        min_score=warm - 1.0,  # only reachable when not cooling
        on_cooldown={"athf-into"},
    )
    assert not choice.fallback
    assert choice.asset.key == "athf-into"
    assert "all-cooling" in choice.reason


def test_strictness_off_widens_a_thin_pool(pool: list[Asset]) -> None:
    """`off` relaxes *fit*, never the truth: a contradiction stays ineligible."""
    request = BumpRequest(
        subject="this",
        position="INTO",
        this_show="sealab",
        year=2003,
        allow_other_shows=False,
    )
    off = choose(pool, request, min_score=6.0, strictness="off")
    assert not off.fallback
    # The only Sealab cards are absent, so it must have reached past the
    # preferred set -- but never to a card that lies about the running order.
    assert off.asset.key not in {"futurama-next", "athf-after"}
    assert off.score > 0


def test_era_mismatch_penalises() -> None:
    request = BumpRequest(subject="this", position="INTO", this_show="athf", year=2003, era_drift=0)
    close = _bump("close", "athf", "INTO", era=2003)
    far = _bump("far", "athf", "INTO", era=2015)
    close_score, _ = score_asset(close, request, cool=False)
    far_score, _ = score_asset(far, request, cool=False)
    assert close_score > far_score


def test_choice_is_repeatable_for_the_same_seed(pool: list[Asset]) -> None:
    first = choose(pool, _request(), min_score=6.0, seed_parts=("d", 1, 0))
    second = choose(pool, _request(), min_score=6.0, seed_parts=("d", 1, 0))
    assert first.asset.key == second.asset.key

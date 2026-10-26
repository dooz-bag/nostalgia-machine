"""The corner bug: it has to be legible, the right size, and never doubled.

Three separate faults drove this file.

The mark rendered as ``adult swim]`` with no opening bracket. The filter chain
ended with ``crop=iw*0.96:ih*0.96``, which crops *symmetrically about the centre*
while the text is drawn at ``x=0`` -- so it removed ~31px from the left and ate
the ``[``. The same crop also left the card far wider than the ink, which is why
the mark came out at roughly half the size ``width_percent`` asked for.

And an episode that already carried the network's own logo got a second one
stamped on top, because overlay mode forced ``skip_when_burned_in=False``.
"""

from __future__ import annotations

import pytest

from nostalgia.assets import Asset
from nostalgia.config import WatermarkConfig
from nostalgia.watermark import (
    DEFAULT_OPACITY_PERCENT,
    DEFAULT_WIDTH_PERCENT,
    WatermarkLook,
    WatermarkRule,
    card_size,
    look_from_config,
    mark_filter,
    rule_from_config,
)


# -- the card is all ink, and starts at the ink ----------------------------


def test_the_card_is_trimmed_to_its_ink() -> None:
    """``bbox`` finds the glyphs; a fixed fraction crop cannot."""
    graph = mark_filter(WatermarkLook(), font_path=None)
    assert "bbox=" in graph
    # The symmetric crop is what removed the opening bracket.
    assert "crop=iw*0.96" not in graph


def test_the_condense_still_happens_before_the_trim() -> None:
    """Squeezing after the trim would change the card's aspect, not the letterforms."""
    graph = mark_filter(WatermarkLook(condense=0.84), font_path=None)
    assert graph.index("scale=iw*0.840") < graph.index("bbox=")


def test_a_condensed_face_is_not_squeezed_again() -> None:
    from pathlib import Path

    condensed = Path("/fonts/HelveticaNeue-CondensedBold.ttf")
    graph = mark_filter(WatermarkLook(), font_path=condensed)
    assert "scale=iw*" not in graph


def test_the_card_canvas_holds_the_whole_wordmark() -> None:
    """A canvas narrower than the text is what clipped it in the first place."""
    look = WatermarkLook()
    width, height = card_size(look, width=1280, height=720)
    assert width > height * 3, "the canvas must be much wider than tall"


# -- size and opacity -----------------------------------------------------


def test_the_default_mark_is_larger_and_softer_than_it_was() -> None:
    """The asked-for change, pinned so a later tweak is a deliberate act."""
    look = WatermarkLook()
    assert look.font_size == pytest.approx(0.033)
    assert DEFAULT_WIDTH_PERCENT == pytest.approx(14.0)
    assert DEFAULT_OPACITY_PERCENT == pytest.approx(72.0)
    assert look.opacity_percent() == pytest.approx(72.0)


def test_the_config_defaults_match_the_look_defaults() -> None:
    """Two copies of the same number is how they drift apart."""
    config = WatermarkConfig()
    look = look_from_config(config)
    default = WatermarkLook()
    assert look.font_size == pytest.approx(default.font_size)
    assert look.opacity == pytest.approx(default.opacity)
    assert look.width_percent == pytest.approx(default.width_percent)


# -- never two bugs -------------------------------------------------------


def test_a_show_that_already_has_a_bug_is_skipped() -> None:
    rule = WatermarkRule(except_shows=("harvey-birdman", "the-brak-show"))
    assert not rule.applies_to(kind="episode", show_id="harvey-birdman", era_year=2003)
    assert not rule.applies_to(kind="episode", show_id="the-brak-show", era_year=2003)
    assert rule.applies_to(kind="episode", show_id="sealab-2021", era_year=2003)


def test_except_shows_survive_the_config_round_trip() -> None:
    config = WatermarkConfig(mode="overlay", except_shows=["harvey-birdman"])
    rule = rule_from_config(config)
    assert not rule.applies_to(kind="episode", show_id="harvey-birdman", era_year=2003)


def test_only_episodes_are_ever_marked() -> None:
    """[adult swim] did not bug its commercials or its bumpers."""
    rule = WatermarkRule()
    for kind in ("bump", "ad", "ident"):
        assert not rule.applies_to(kind=kind, show_id="sealab-2021", era_year=2003)


def test_the_allow_list_still_wins_over_the_skip_list() -> None:
    """An explicit allow-list is a narrower statement and takes precedence."""
    rule = WatermarkRule(shows=("futurama",), except_shows=("harvey-birdman",))
    assert rule.applies_to(kind="episode", show_id="futurama", era_year=2003)
    assert not rule.applies_to(kind="episode", show_id="sealab-2021", era_year=2003)


def test_an_unexamined_episode_is_still_marked() -> None:
    """`None` means "not examined", which must not switch the bug off channel-wide.

    Only a definite absence of a mark -- or a show on the skip list -- may stop it.
    """
    from nostalgia.plan import _wants_mark

    class _Root:
        era_year = 2003

    episode = Asset(key="x", path="/x", kind="episode", duration_ms=1000)
    assert _wants_mark(WatermarkRule(), "sealab-2021", _Root(), episode) is True


def test_an_episode_known_to_carry_its_own_bug_is_left_alone() -> None:
    from dataclasses import replace

    from nostalgia.plan import _wants_mark
    from nostalgia.rotation import EpisodeRef

    class _Root:
        era_year = 2003

    marked = EpisodeRef(
        key="x:s01e01",
        show_id="sealab-2021",
        season=1,
        episode=1,
        path="/x",
        duration_ms=1000,
        carries_bug=True,
    )
    assert _wants_mark(WatermarkRule(), "sealab-2021", _Root(), marked) is False

    clean = replace(marked, carries_bug=False)
    assert _wants_mark(WatermarkRule(), "sealab-2021", _Root(), clean) is True

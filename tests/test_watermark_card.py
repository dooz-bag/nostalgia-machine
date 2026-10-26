"""The corner bug: it has to be legible, the right size, and never doubled.

Four separate faults drove this file, and the size one took three attempts to
find because two "fixes" were no-ops.

The mark rendered as ``adult swim]`` with no opening bracket: the chain ended with
``crop=iw*0.96:ih*0.96``, cropping *symmetrically about the centre* while the text
is drawn at ``x=0``, so it ate ~31px from the left.

Fixing that with ``bbox`` looked right and did nothing. ``bbox`` **measures** the
ink and prints the box; it passes the frame through untouched (verified: an 800x200
input comes out 800x200). So the card stayed as wide as its canvas -- 1681px for
616px of ink -- and the streamer's ``width_percent`` scaled mostly empty space. The
mark was always about a third of the requested size, which is why turning the
setting up barely moved it.

The real trim is :func:`_ink_extent`, and ``scale`` also needed ``setsar=1``:
compressing the width made ffmpeg set a compensating sample aspect ratio
(2002:1681), which stretched the card ~19% when the streamer scaled by width alone.

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


def test_the_filter_chain_does_not_pretend_to_trim() -> None:
    """Trimming is a second pass, because no filter does it in one.

    ``bbox`` must not be in the chain: it is a *measurement* filter that leaves the
    frame at full canvas size, so using it here silently did nothing and the card
    stayed mostly empty space.
    """
    graph = mark_filter(WatermarkLook(), font_path=None)
    assert "bbox=" not in graph
    # The symmetric crop is what removed the opening bracket.
    assert "crop=iw*0.96" not in graph


def test_the_scale_restores_a_square_pixel_aspect() -> None:
    """Without ``setsar=1`` the squeeze leaves a 2002:1681 card and it stretches."""
    graph = mark_filter(WatermarkLook(condense=0.84), font_path=None)
    assert "setsar=1" in graph


def test_the_condense_squeezes_the_letterforms_not_the_card() -> None:
    """The squeeze is what turns a Helvetica-metric bold into the condensed cut."""
    graph = mark_filter(WatermarkLook(condense=0.84), font_path=None)
    assert "scale=iw*0.840" in graph


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


def test_the_default_mark_is_near_double_what_it_was_and_more_opaque() -> None:
    """The asked-for change, pinned so a later tweak is a deliberate act.

    ``width_percent`` only started meaning what it says once the card was actually
    trimmed to its ink, so 11% *after* the trim is already bigger than 14% ever
    looked *before* it. 20% is the near-doubling that was asked for.
    """
    look = WatermarkLook()
    assert look.font_size == pytest.approx(0.033)
    assert DEFAULT_WIDTH_PERCENT == pytest.approx(20.0)
    assert DEFAULT_OPACITY_PERCENT == pytest.approx(92.0)
    assert look.opacity_percent() == pytest.approx(92.0)


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


# -- trimming the card to its ink -----------------------------------------


def _write_png(path, width: int, height: int, box) -> None:
    """A minimal RGBA PNG: opaque ink inside *box*, transparent elsewhere.

    Written by hand rather than with a library because the container has none, and
    the point of the test is exactly the thing a library would hide.
    """
    import struct
    import zlib

    left, top, right, bottom = box
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter: none
        for x in range(width):
            inside = left <= x <= right and top <= y <= bottom
            rows += bytes((255, 255, 255, 255) if inside else (0, 0, 0, 0))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def test_the_ink_extent_finds_the_drawn_pixels(tmp_path) -> None:
    from nostalgia.watermark import _ink_extent

    target = tmp_path / "card.png"
    _write_png(target, 200, 60, (40, 10, 139, 29))
    assert _ink_extent(target) == (40, 10, 100, 20)


def test_the_ink_extent_ignores_fully_transparent_cards(tmp_path) -> None:
    from nostalgia.watermark import _ink_extent

    target = tmp_path / "empty.png"
    _write_png(target, 40, 20, (0, 0, -1, -1))
    assert _ink_extent(target) is None


def test_the_ink_extent_refuses_something_that_is_not_a_png(tmp_path) -> None:
    from nostalgia.watermark import _ink_extent

    target = tmp_path / "junk.png"
    target.write_bytes(b"not a png at all")
    assert _ink_extent(target) is None

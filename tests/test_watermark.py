"""The corner bug.

[adult swim] ran a persistent ``[adult swim]`` mark bottom-right of every
programme. Two things have to be true for this to be worth having:

* it must look like the real wordmark -- Helvetica Neue Condensed Bold -- which
  means the font chain has to degrade gracefully rather than fail, because the
  real font ships with nothing;
* it must never be applied twice, and never to a commercial.

The rendered artwork is checked separately (a card is a PNG, which cannot be
asserted on meaningfully in a unit test); what is pinned here is the wiring:
which items get asked for a mark, and that overlay and burn cannot both fire.
"""

from __future__ import annotations

import pytest

from nostalgia.config import WatermarkConfig
from nostalgia.watermark import (
    CONDENSED_HINTS,
    FONT_CHAIN,
    WANTED_FONT,
    WatermarkLook,
    WatermarkRule,
    build_filtergraph,
    card_size,
    graphics_layer,
    look_from_config,
    mark_filter,
    resolve_mark_font,
    rule_from_config,
)


# -- which items get the mark ---------------------------------------------


def test_a_rule_with_no_shows_marks_every_episode() -> None:
    rule = WatermarkRule(enabled=True)
    assert rule.applies_to(kind="episode", show_id="futurama", era_year=2003)


def test_commercials_and_bumpers_are_never_marked() -> None:
    """[adult swim] did not bug its own commercials."""
    rule = WatermarkRule(enabled=True)
    for kind in ("ad", "bump", "ident", "filler"):
        assert not rule.applies_to(kind=kind, show_id="futurama", era_year=2003)


def test_a_show_list_restricts_the_mark() -> None:
    rule = WatermarkRule(enabled=True, shows=("futurama",))
    assert rule.applies_to(kind="episode", show_id="futurama", era_year=2003)
    assert not rule.applies_to(kind="episode", show_id="sealab-2021", era_year=2003)


def test_the_era_window_bounds_the_mark() -> None:
    rule = WatermarkRule(enabled=True, era_from=2001, era_to=2008)
    assert rule.applies_to(kind="episode", show_id="x", era_year=2003)
    assert not rule.applies_to(kind="episode", show_id="x", era_year=1998)


def test_a_disabled_rule_marks_nothing() -> None:
    assert not WatermarkRule(enabled=False).applies_to(
        kind="episode", show_id="futurama", era_year=2003
    )


# -- config -----------------------------------------------------------------


def test_the_default_mode_is_off() -> None:
    assert WatermarkConfig().mode == "none"
    assert not WatermarkConfig().enabled


@pytest.mark.parametrize("value", ["none", "overlay", "burn"])
def test_valid_modes(value) -> None:
    assert WatermarkConfig(mode=value).mode == value


@pytest.mark.parametrize("value", [False, None])
def test_unquoted_yaml_off_is_forgiven(value) -> None:
    """`mode: off` arrives as Python False, and the error would not hint why."""
    assert WatermarkConfig(mode=value).mode == "none"


def test_look_from_config_converts_percentages_to_fractions() -> None:
    look = look_from_config(
        WatermarkConfig(margin_x_percent=2.0, margin_y_percent=4.0, opacity=0.5)
    )
    assert look.margin_x == pytest.approx(0.02)
    assert look.margin_y == pytest.approx(0.04)
    assert look.opacity == pytest.approx(0.5)


def test_burn_rule_is_only_enabled_in_burn_mode() -> None:
    assert rule_from_config(WatermarkConfig(mode="burn")).enabled
    assert not rule_from_config(WatermarkConfig(mode="overlay")).enabled
    assert not rule_from_config(WatermarkConfig(mode="none")).enabled


# -- the mark's look --------------------------------------------------------


def test_the_requested_typeface_is_helvetica_neue_condensed_bold() -> None:
    assert WANTED_FONT == "Helvetica Neue Condensed Bold"


def test_the_default_config_requests_helvetica_neue_condensed_bold() -> None:
    assert WatermarkConfig().font == WANTED_FONT
    assert WatermarkConfig().condense == pytest.approx(0.84)


def test_the_font_chain_ends_somewhere_a_container_has() -> None:
    """The chain must not depend on the host's font collection.

    A Debian container ships Liberation Sans; if the chain cannot reach it, the
    overlay silently stops rendering in Docker while working on Windows.
    """
    flat = [name.lower() for group in FONT_CHAIN for name in group]
    assert any("liberationsans" in name for name in flat)
    assert any("dejavu" in name for name in flat)


def test_resolve_mark_font_always_finds_something_on_this_machine() -> None:
    assert resolve_mark_font(WANTED_FONT) is not None


def test_a_plain_bold_face_is_condensed_to_match() -> None:
    """Arial bold is not condensed, so the look applies the squeeze."""
    look = WatermarkLook(font="Arial")
    graph = mark_filter(look, font_path=None)
    assert f"scale=iw*{look.condense:.3f}:ih" in graph


def test_an_already_condensed_face_is_not_squeezed_again() -> None:
    from pathlib import Path

    condensed = Path("/usr/share/fonts/truetype/DejaVuSansCondensed-Bold.ttf")
    assert any(hint in condensed.name.lower() for hint in CONDENSED_HINTS)
    graph = mark_filter(WatermarkLook(), font_path=condensed)
    assert "scale=iw*" not in graph


def test_the_card_keeps_alpha() -> None:
    """Without colorkey the card is an opaque black box over the picture.

    Neither `color=c=black@0.0` nor a `geq` alpha channel survives ffmpeg: both
    came back with alpha 255 when measured, which is why this is pinned.
    """
    graph = mark_filter(WatermarkLook(), font_path=None)
    assert "colorkey" in graph


def test_the_card_scales_with_the_output_resolution() -> None:
    look = WatermarkLook()
    small = card_size(look, width=1280, height=720)
    large = card_size(look, width=1920, height=1080)
    assert large[0] > small[0]
    assert large[1] > small[1]


def test_look_values_are_clamped_into_a_visible_range() -> None:
    """A typo must not silently move the mark off screen."""
    wild = WatermarkLook(font_size=9.0, margin_x=5.0, opacity=8.0, condense=0.0)
    normal = wild.normalized()
    assert normal.font_size <= 0.12
    assert normal.margin_x <= 0.4
    assert normal.opacity <= 1.0
    assert normal.condense >= 0.5


# -- the playout layer ------------------------------------------------------


def test_the_overlay_layer_anchors_bottom_right() -> None:
    layer = graphics_layer(WatermarkLook(), path="/app/media/derived/card.png")
    assert layer["location"] == "bottom_right"
    assert layer["source"] == {
        "source_type": "local",
        "path": "/app/media/derived/card.png",
    }


def test_the_overlay_layer_keeps_the_mark_inside_letterbox_bars() -> None:
    """`within_source_content` is the whole reason to prefer this over pixels."""
    layer = graphics_layer(WatermarkLook(), path="/x.png")
    assert layer["within_source_content"] is True


def test_the_overlay_layer_carries_margins_and_opacity() -> None:
    look = WatermarkLook(margin_x=0.03, margin_y=0.05, opacity=0.75, width_percent=12)
    layer = graphics_layer(look, path="/x.png")
    assert layer["horizontal_margin_percent"] == pytest.approx(3.0)
    assert layer["vertical_margin_percent"] == pytest.approx(5.0)
    assert layer["opacity_percent"] == pytest.approx(75.0)
    assert layer["width_percent"] == pytest.approx(12.0)


def test_the_placement_filter_is_an_overlay_bottom_right() -> None:
    graph = build_filtergraph(WatermarkLook(), width=1280, height=720)
    assert graph.startswith("overlay=")
    assert "W-w-" in graph and "H-h-" in graph


# -- can the overlay actually work? ----------------------------------------


def test_the_binary_markers_are_the_schema_field_names() -> None:
    """These are serde field names; a rename upstream makes the layer a no-op."""
    from nostalgia.pipeline import OVERLAY_BINARY_MARKERS

    assert "within_source_content" in OVERLAY_BINARY_MARKERS
    assert "bottom_right" in OVERLAY_BINARY_MARKERS


def test_overlay_support_is_unknown_without_a_binary() -> None:
    """Unknown must not read as success: the caller warns instead."""
    from nostalgia.pipeline import streamer_supports_overlay

    assert streamer_supports_overlay(binary=None, container=None) is None


def test_overlay_support_detects_a_binary_missing_the_fields(tmp_path) -> None:
    from nostalgia.pipeline import streamer_supports_overlay

    stub = tmp_path / "ersatztv"
    stub.write_bytes(b"an old build with no graphics layer support")
    assert streamer_supports_overlay(binary=stub) is False


def test_overlay_support_detects_a_binary_carrying_the_fields(tmp_path) -> None:
    from nostalgia.pipeline import OVERLAY_BINARY_MARKERS, streamer_supports_overlay

    stub = tmp_path / "ersatztv"
    stub.write_bytes(" ".join(OVERLAY_BINARY_MARKERS).encode())
    assert streamer_supports_overlay(binary=stub) is True


def test_none_mode_reports_nothing_about_watermark(config, catalog, paths) -> None:
    from nostalgia.pipeline import _watermark_findings

    payload = config.model_dump()
    payload["watermark"] = {**payload["watermark"], "mode": "none"}
    quiet = type(config).model_validate(payload)
    assert _watermark_findings(quiet, paths) == []


def test_overlay_mode_warns_when_it_cannot_confirm(config, catalog, paths) -> None:
    """No host binary and no container name: say so rather than claim success."""
    from nostalgia.pipeline import _watermark_findings

    payload = config.model_dump()
    payload["watermark"] = {**payload["watermark"], "mode": "overlay"}
    overlay = type(config).model_validate(payload)
    messages = [message for _l, _a, message in _watermark_findings(overlay, paths)]
    assert any("cannot inspect" in message for message in messages), messages


def test_burn_mode_prompts_for_the_composite_step(config, catalog, paths) -> None:
    from nostalgia.pipeline import _watermark_findings

    payload = config.model_dump()
    payload["watermark"] = {**payload["watermark"], "mode": "burn"}
    burn = type(config).model_validate(payload)
    messages = [message for _l, _a, message in _watermark_findings(burn, paths)]
    assert any("watermark --apply" in message for message in messages), messages


def test_the_fallback_font_is_reported_so_a_boxy_mark_is_explicable(
    config, catalog, paths
) -> None:
    payload = config.model_dump()
    payload["watermark"] = {**payload["watermark"], "mode": "overlay"}
    overlay = type(config).model_validate(payload)
    from nostalgia.pipeline import _watermark_findings

    messages = [message for _l, _a, message in _watermark_findings(overlay, paths)]
    assert any("condensed by" in message or WANTED_FONT in message for message in messages)

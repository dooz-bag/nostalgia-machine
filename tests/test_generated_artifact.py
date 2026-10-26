"""Generated bump cards must announce that they were generated.

A generated card sits in the same pool as several hundred archive bumps. If it is
indistinguishable, the channel quietly rewrites history: you can no longer tell
what actually aired in 2003 from what the renderer invented last week.

The mark has to do two opposite things at once:

* **be part of the picture**, not a player-side overlay, so it survives any
  transcode and shows up in a screenshot -- otherwise it is invisible on exactly
  the surface you would check it on;
* **be quiet**, because it also has to be watchable. Hence low opacity, small,
  and in the top-right corner, which real [adult swim] cards never used.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nostalgia.bumpgen import StylePack, _artifact_filters, load_era_packs

ERA_FILE = Path(__file__).resolve().parents[1] / "config" / "bumps-era.yaml"


def test_the_shipped_config_marks_generated_cards() -> None:
    packs = load_era_packs(ERA_FILE)
    assert packs.styles
    for key, style in packs.styles.items():
        assert style.artifact, f"style {key} would render an unmarked card"


def test_the_artifact_declared_once_is_inherited_by_every_style(tmp_path) -> None:
    """The mark is a channel-wide policy, so it should not be repeated per era."""
    path = tmp_path / "era.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "artifact": True,
                "artifact_text": "ZZ",
                "styles": {
                    "2001": {"name": "A"},
                    "2007": {"name": "B"},
                },
            }
        ),
        encoding="utf-8",
    )
    packs = load_era_packs(path)
    assert {style.artifact_text for style in packs.styles.values()} == {"ZZ"}


def test_a_style_may_override_the_shared_artifact(tmp_path) -> None:
    path = tmp_path / "era.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "artifact": True,
                "artifact_text": "ZZ",
                "styles": {
                    "2001": {"name": "A"},
                    "2007": {"name": "B", "artifact_text": "QQ"},
                },
            }
        ),
        encoding="utf-8",
    )
    packs = load_era_packs(path)
    assert packs.styles["2007"].artifact_text == "QQ"
    assert packs.styles["2001"].artifact_text == "ZZ"


def test_the_mark_is_drawn_into_the_picture() -> None:
    """A drawtext filter, not a watermark the client adds."""
    filters = _artifact_filters(StylePack(key="2003", name="x"), width=1280, height=720)
    joined = ",".join(filters)
    assert "drawtext" in joined
    assert any("drawbox" in entry for entry in filters)


def test_the_mark_sits_top_right_where_real_cards_never_did() -> None:
    style = StylePack(key="2003", name="x", artifact_text="SG")
    filters = _artifact_filters(style, width=1280, height=720)
    drawtext = next(entry for entry in filters if "drawtext" in entry)
    x = int(drawtext.split("x=")[1].split(":")[0])
    y = int(drawtext.split("y=")[1].split(":")[0])
    # Right half, top third: the bottom-right belongs to the network bug.
    assert x > 1280 * 0.7, x
    assert y < 720 * 0.35, y


def test_the_mark_is_translucent_not_a_caption() -> None:
    style = StylePack(key="2003", name="x", artifact_opacity=0.32)
    joined = ",".join(_artifact_filters(style, width=1280, height=720))
    assert "alpha=0.32" in joined


def test_the_mark_can_be_switched_off_per_style() -> None:
    style = StylePack(key="2003", name="x", artifact=False)
    assert _artifact_filters(style, width=1280, height=720) == []


def test_an_empty_artifact_text_draws_nothing() -> None:
    style = StylePack(key="2003", name="x", artifact_text="")
    assert _artifact_filters(style, width=1280, height=720) == []


def test_the_mark_scales_with_the_output_resolution() -> None:
    small = _artifact_filters(StylePack(key="a", name="x"), width=1280, height=720)[-1]
    large = _artifact_filters(StylePack(key="a", name="x"), width=1920, height=1080)[-1]
    assert "fontsize=22" in small  # 720 * 0.030
    assert "fontsize=32" in large  # 1080 * 0.030

"""Conforming: one picture shape, one loudness, exact frames.

The properties worth pinning are the ones a viewer would notice if they broke: the
picture width changing at a cut, a fade appearing where a bump should butt-cut, a
silent junk file being boosted into hiss, and a conformed copy replacing the
original *without* the original losing its tags.

Everything here is pure logic or catalog state. The ffmpeg recipe itself is covered
by building the command and checking what it asks for; the bytes it produces were
verified against real footage separately (stream signatures identical across
differently-shaped sources, loudness within 0.5 LU of target, a 12-file stream-copy
join decoding with no errors).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nostalgia import conform as C
from nostalgia.assets import Asset
from nostalgia.catalog import Catalog

PARAMS = C.ConformParams()


def _info(**overrides) -> C.SourceInfo:
    base = dict(width=640, height=480, dar=4 / 3, interlaced=False, duration_s=30.03, has_audio=True)
    base.update(overrides)
    return C.SourceInfo(**base)


def _command(kind="ad", info=None, loudness=None, params=PARAMS) -> list[str]:
    info = info or _info()
    geometry = C.geometry_for(info.dar, params.width, params.height)
    return C.build_command(
        "ffmpeg", Path("in.mp4"), Path("out.mkv"), info, geometry, params,
        loudness=loudness, fade=(kind == "ad"),
    )


def _arg(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


LOUDNESS = {
    "input_i": -22.0, "input_tp": -3.0, "input_lra": 5.0,
    "input_thresh": -32.0, "target_offset": 0.5,
}


# -- geometry: two canvases, not seven -----------------------------------------


@pytest.mark.parametrize(
    "dar",
    [1.25, 1.333, 1.37, 1.5, 1.55],
    ids=["5:4", "4:3", "41:30", "3:2", "odd"],
)
def test_every_near_4x3_shape_lands_on_the_same_960_wide_canvas(dar):
    """Nearly every "5:4" or "3:2" file in a recorded-TV library is 4:3 material in
    an odd container, so they must all land on one width -- that is what stops the
    black bars changing at the cut."""
    geometry = C.geometry_for(dar)
    assert geometry.canvas == "standard"
    assert (geometry.scale_w, geometry.scale_h) == (960, 720)
    assert geometry.pad_x == 160


@pytest.mark.parametrize("dar", [1.78, 1.7778, 1.85])
def test_widescreen_fills_the_frame(dar):
    geometry = C.geometry_for(dar)
    assert geometry.canvas == "wide"
    assert geometry.scale_w == 1280
    assert geometry.pad_x == 0


def test_a_cinemascope_trailer_keeps_its_letterbox():
    """Stretching 2.35:1 to fill the frame would distort it; fit and bar instead."""
    geometry = C.geometry_for(2.35)
    assert geometry.scale_w == 1280
    assert geometry.scale_h < 720
    assert geometry.pad_y == (720 - geometry.scale_h) // 2


def test_the_whole_variety_of_a_library_collapses_to_two_picture_widths():
    seen = {
        C.geometry_for(dar).scale_w
        for dar in (1.25, 1.333, 1.37, 1.5, 1.7778, 1.78, 1.85)
    }
    assert seen == {960, 1280}


def test_geometry_follows_the_configured_frame_not_a_hardcoded_720p():
    geometry = C.geometry_for(4 / 3, width=1920, height=1080)
    assert (geometry.scale_w, geometry.scale_h, geometry.pad_x) == (1440, 1080, 240)


def test_pixel_aspect_reveals_a_4x3_capture_stored_as_720x480():
    """720x480 with 8:9 pixels is 4:3, even though its stored shape is 3:2."""
    dar = C.display_aspect(720, 480, sar="8:9", dar=None)
    assert dar == pytest.approx(4 / 3, abs=0.01)
    assert C.geometry_for(dar).canvas == "standard"


def test_a_declared_display_aspect_wins_over_the_stored_size():
    assert C.display_aspect(854, 480, sar="1:1", dar="427:240") == pytest.approx(1.779, abs=0.01)
    assert C.display_aspect(640, 480, sar="N/A", dar="N/A") == pytest.approx(4 / 3, abs=0.01)


# -- frames and the recipe signature -------------------------------------------


def test_durations_become_whole_frames_at_29_97():
    assert C.frame_count(15.0) == 449
    assert C.frame_count(30.03) == 900
    assert C.frame_count(0.001) == 1  # never an empty file
    # round trip: a frame count always maps back to no more than the source length
    for seconds in (4.0, 15.015, 29.93, 59.09):
        assert C.frames_to_seconds(C.frame_count(seconds)) <= seconds + 1e-9


def test_the_signature_changes_with_anything_that_changes_the_output():
    base = C.ConformParams().signature()
    assert C.ConformParams().signature() == base  # deterministic
    for changed in (
        C.ConformParams(dip_ms=0),
        C.ConformParams(width=1920, height=1080),
        C.ConformParams(target_lufs=-23.0),
        C.ConformParams(crf=24),
    ):
        assert changed.signature() != base


# -- the ffmpeg recipe ---------------------------------------------------------


def test_a_commercial_dips_to_black_and_silence_at_both_ends():
    command = _command("ad", loudness=LOUDNESS)
    video, audio = _arg(command, "-vf"), _arg(command, "-af")
    assert video.count("fade=t=") == 2 and "fade=t=in" in video and "fade=t=out" in video
    assert audio.count("afade=t=") == 2


def test_a_bump_is_butt_cut_with_no_fade():
    """A real bump cut straight in; dipping it would be a different channel."""
    command = _command("bump", loudness=LOUDNESS)
    assert "fade" not in _arg(command, "-vf")
    assert "afade" not in _arg(command, "-af")


def test_a_zero_dip_is_a_hard_cut_even_for_a_commercial():
    command = _command("ad", loudness=LOUDNESS, params=C.ConformParams(dip_ms=0))
    assert "fade" not in _arg(command, "-vf")


def test_the_dip_can_never_swallow_a_very_short_spot():
    """A 0.4 s clip with a 100 ms dip each end would be mostly black; the dip is
    capped at a quarter of the clip."""
    import re

    def dip(seconds: float) -> float:
        command = _command("ad", info=_info(duration_s=seconds), loudness=LOUDNESS)
        return float(re.search(r"fade=t=in:st=0:d=([\d.]+)", _arg(command, "-vf")).group(1))

    # Measured against the frame-quantised length, which is what actually plays.
    for seconds in (0.4, 0.2, 0.1):
        length = C.frames_to_seconds(C.frame_count(seconds))
        assert dip(seconds) <= length / 4 + 1e-3
    assert dip(0.4) < 0.1, "a short clip must get a shorter dip than the default"
    assert dip(30.0) == pytest.approx(0.1), "a normal spot gets the configured dip"


def test_loudness_is_applied_with_the_measured_values_in_linear_mode():
    audio = _arg(_command(loudness=LOUDNESS), "-af")
    assert "loudnorm=I=-16.0" in audio
    assert "measured_I=-22.0" in audio and "linear=true" in audio


def test_no_measurement_means_no_gain_is_applied():
    """A silent bump has nothing to normalise; boosting it would only make noise."""
    assert "loudnorm" not in _arg(_command("bump", loudness=None), "-af")


def test_interlaced_footage_is_deinterlaced_and_progressive_is_left_alone():
    assert "yadif" in _arg(_command(info=_info(interlaced=True)), "-vf")
    assert "yadif" not in _arg(_command(info=_info(interlaced=False)), "-vf")


def test_every_output_is_cfr_29_97_with_a_closed_gop_and_no_bframes():
    """What lets a run of conformed files be joined by stream copy: identical
    parameters, a keyframe at the start of each, and no reordering at the join."""
    command = _command(loudness=LOUDNESS)
    assert _arg(command, "-r") == "30000/1001"
    params = _arg(command, "-x264-params")
    assert "bframes=0" in params and "open-gop=0" in params and "keyint=30" in params
    assert _arg(command, "-ar") == "48000" and _arg(command, "-ac") == "2"
    assert _arg(command, "-frames:v") == str(C.frame_count(30.03))


def test_a_source_with_no_audio_gets_silence_rather_than_a_missing_track():
    """Stream-copy joins need every file to have the same streams."""
    command = _command(info=_info(has_audio=False))
    assert "anullsrc" in " ".join(command)
    assert _arg(command, "-map") == "0:v:0"


def test_output_paths_are_stable_unique_and_filesystem_safe():
    first = C.output_path(Path("/d"), "ad", "ad:2003/some spot (v2).mp4")
    assert first == C.output_path(Path("/d"), "ad", "ad:2003/some spot (v2).mp4")
    assert first != C.output_path(Path("/d"), "ad", "ad:2004/some spot (v2).mp4")
    assert first.suffix == ".mkv" and first.parent == Path("/d/ad")
    assert not set(' ()/:') & set(first.name)


# -- the catalog: the copy airs, the original keeps its identity -------------------


def _ad(catalog: Catalog):
    return next(iter(catalog.assets(("ad",))))


def test_an_unconformed_asset_airs_as_itself(catalog: Catalog):
    asset = _ad(catalog)
    assert asset.air_path is None and not asset.is_conformed
    assert asset.playable_path == asset.path


def test_a_conformed_copy_airs_in_place_of_the_original(catalog: Catalog, tmp_path: Path):
    original = _ad(catalog)
    copy = tmp_path / "copy.mkv"
    copy.write_bytes(b"x")
    assert catalog.set_conformed(original.key, copy, 29_970, "sig1")

    asset = next(a for a in catalog.assets(("ad",)) if a.key == original.key)
    assert asset.playable_path == str(copy) and asset.is_conformed
    assert asset.duration_ms == 29_970  # the planner must sum what actually airs


def test_the_original_keeps_its_own_path_for_tags_and_history(catalog: Catalog, tmp_path: Path):
    """Tags are read from beside the original file, so the asset's `path` must not
    move to the derived folder or every tag would silently disappear."""
    original = _ad(catalog)
    copy = tmp_path / "copy.mkv"
    copy.write_bytes(b"x")
    catalog.set_conformed(original.key, copy, 29_970, "sig1")
    asset = next(a for a in catalog.assets(("ad",)) if a.key == original.key)
    assert asset.path == original.path and asset.key == original.key


def test_a_deleted_copy_falls_back_to_the_original(catalog: Catalog, tmp_path: Path):
    """Wiping media/derived/ must degrade to the old behaviour, not point the
    streamer at a file that is not there."""
    original = _ad(catalog)
    copy = tmp_path / "copy.mkv"
    copy.write_bytes(b"x")
    catalog.set_conformed(original.key, copy, 29_970, "sig1")
    copy.unlink()

    asset = next(a for a in catalog.assets(("ad",)) if a.key == original.key)
    assert not asset.is_conformed
    assert asset.duration_ms == original.duration_ms


def test_a_rescan_does_not_forget_what_was_conformed(catalog, config, paths, tmp_path: Path):
    from nostalgia.pipeline import scan

    original = _ad(catalog)
    copy = tmp_path / "copy.mkv"
    copy.write_bytes(b"x")
    catalog.set_conformed(original.key, copy, 29_970, "sig1")
    scan(config, catalog, paths)
    asset = next(a for a in catalog.assets(("ad",)) if a.key == original.key)
    assert asset.is_conformed


def test_the_queue_skips_up_to_date_files_and_requeues_stale_or_missing(
    catalog: Catalog, tmp_path: Path
):
    keys = [a.key for a in catalog.assets(("ad",))]
    first, second = keys[0], keys[1]
    done = tmp_path / "done.mkv"
    done.write_bytes(b"x")
    catalog.set_conformed(first, done, 1000, "current")
    catalog.set_conformed(second, tmp_path / "gone.mkv", 1000, "current")  # never written

    queued = {row["key"] for row in catalog.conform_queue(("ad",), "current")}
    assert first not in queued, "an up-to-date file must not be redone"
    assert second in queued, "a missing copy must be redone"
    # a different recipe (e.g. the dip changed) makes everything stale
    assert first in {row["key"] for row in catalog.conform_queue(("ad",), "other")}


def test_a_disabled_file_is_not_queued(catalog: Catalog):
    victim = _ad(catalog)
    catalog.set_disabled(victim.key, True)
    assert victim.key not in {row["key"] for row in catalog.conform_queue(("ad",), "x")}


def test_stats_count_what_is_done_and_what_is_stale(catalog: Catalog, tmp_path: Path):
    keys = [a.key for a in catalog.assets(("ad",))]
    copy = tmp_path / "c.mkv"
    copy.write_bytes(b"x")
    catalog.set_conformed(keys[0], copy, 1000, "old")
    catalog.set_conformed(keys[1], copy, 1000, "new")
    ads = catalog.conform_stats("new")["ad"]
    assert ads["total"] == len(keys)
    assert ads["conformed"] == 2 and ads["stale"] == 1


def test_clearing_a_copy_restores_the_original(catalog: Catalog, tmp_path: Path):
    original = _ad(catalog)
    copy = tmp_path / "copy.mkv"
    copy.write_bytes(b"x")
    catalog.set_conformed(original.key, copy, 29_970, "sig")
    catalog.clear_conformed(original.key)
    assert not next(a for a in catalog.assets(("ad",)) if a.key == original.key).is_conformed


def test_an_asset_defaults_to_playing_itself():
    asset = Asset(key="k", path="/a.mp4", kind="ad", duration_ms=1000)
    assert asset.playable_path == "/a.mp4" and not asset.is_conformed

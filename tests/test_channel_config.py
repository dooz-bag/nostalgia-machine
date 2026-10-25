"""The channel config must be loadable by the ErsatzTV build we actually ship.

A version mismatch or one bad enum value does not raise anywhere in Python: the
streamer logs "unsupported schema version" (or "unknown variant"), exits the
channel session, and every request to `channel/N.m3u8` returns 503. These tests
pin the values that caused exactly that, so it cannot regress silently.
"""

from __future__ import annotations

import json
from pathlib import Path

from nostalgia.emit import (
    CHANNEL_VERSION,
    PLAYOUT_VERSION,
    EmitPaths,
    write_ersatztv_config,
)


def _schema_compatible(version: str) -> tuple[int, int]:
    """ErsatzTV version URIs are ``0.<breaking>.<compatible>``."""
    tail = version.rsplit("/", 1)[-1]
    _, breaking, compatible = tail.split(".")
    return int(breaking), int(compatible)


def _write(paths, tmp_path: Path) -> dict:
    _, channel = write_ersatztv_config(
        EmitPaths(paths.playout_dir, paths.xmltv_dir),
        channel_number="1",
        channel_name="Test",
        tvg_id="test.1",
        width=1280,
        height=720,
        bitrate_kbps=2500,
        audio_bitrate_kbps=160,
        config_dir=tmp_path,
    )
    return json.loads(channel.read_text(encoding="utf-8"))


def test_channel_version_is_not_newer_than_the_shipped_streamer(paths, tmp_path) -> None:
    """0.2.3 accepts compatible <= 2. 0.1.3 shipped a 503, so the pin must stay."""
    breaking, compatible = _schema_compatible(CHANNEL_VERSION)
    assert breaking == 1
    assert compatible <= 2, (
        f"{CHANNEL_VERSION} is newer than the shipped image supports; "
        "every channel request will 503"
    )


def test_subtitle_mode_is_a_real_variant(paths, tmp_path) -> None:
    """Only 'burn' and 'convert' exist. 'none' is a deserialisation error."""
    payload = _write(paths, tmp_path)
    assert payload["normalization"]["subtitle"]["mode"] in ("burn", "convert")


def test_video_accel_is_omitted_rather_than_null(paths, tmp_path) -> None:
    payload = _write(paths, tmp_path)
    video = payload["normalization"]["video"]
    # null would be a type error for the enum; absence means software.
    assert "accel" not in video or isinstance(video["accel"], str)
    assert video["mode"] in ("transcode", "copy")
    assert video["format"] in ("h264", "hevc", "mpeg2video")


def test_audio_mode_and_format_are_real_variants(paths, tmp_path) -> None:
    payload = _write(paths, tmp_path)
    audio = payload["normalization"]["audio"]
    assert audio["mode"] in ("transcode", "copy")
    assert audio["format"] in ("aac", "ac3")


def test_now_required_keys_are_present(paths, tmp_path) -> None:
    payload = _write(paths, tmp_path)
    # PlayoutConfig.folder, FfmpegConfig and NormalizationConfig are not
    # optional in the schema; a missing one is a load failure.
    assert payload["playout"]["folder"]
    assert "disabled_filters" in payload["ffmpeg"]
    assert "audio" in payload["normalization"]
    assert "video" in payload["normalization"]


def test_playout_version_is_a_parsable_schema_uri() -> None:
    """Playout and channel are versioned independently, but both must parse.

    They are not required to match: playout is at 0.0.7 while channel is at 0.1.2,
    and the loader checks each against its own supported number.
    """
    breaking, _compatible = _schema_compatible(PLAYOUT_VERSION)
    assert breaking == 0
    assert PLAYOUT_VERSION.startswith("https://ersatztv.org/playout/version/")


def test_ersatztv_config_lands_in_its_own_directory(paths, tmp_path) -> None:
    """The streamer gets one mountable folder, not a view of the whole project."""
    write_ersatztv_config(
        EmitPaths(paths.playout_dir, paths.xmltv_dir),
        channel_number="1",
        channel_name="Test",
        tvg_id="test.1",
        width=1280,
        height=720,
        bitrate_kbps=2500,
        audio_bitrate_kbps=160,
        config_dir=tmp_path,
    )
    assert (tmp_path / "lineup.json").is_file()
    assert (tmp_path / "channels" / "1" / "channel.json").is_file()
    lineup = json.loads((tmp_path / "lineup.json").read_text(encoding="utf-8"))
    # A relative path keeps the folder relocatable into a container mount.
    assert lineup["channels"][0]["config"] == "./channels/1/channel.json"


def test_container_path_mapping_rewrites_emitted_paths(tmp_path) -> None:
    from nostalgia.emit import set_path_mapping, _seen_by_streamer

    # A real absolute host path: the pipeline passes Paths.root, and the host side
    # is resolved while the container side is kept verbatim.
    host = tmp_path / "project"
    (host / "media").mkdir(parents=True)
    try:
        set_path_mapping(host, "/app")
        assert _seen_by_streamer(host / "media" / "a.mkv") == "/app/media/a.mkv"
        assert _seen_by_streamer(host / "playout") == "/app/playout"
        # Paths outside the root are left alone rather than mangled.
        assert _seen_by_streamer(tmp_path / "elsewhere" / "b.mkv").endswith("b.mkv")
        # A sibling that merely shares a prefix is not treated as a child.
        sibling = host.with_name("project-other") / "c.mkv"
        assert _seen_by_streamer(sibling).endswith("project-other/c.mkv")
    finally:
        set_path_mapping(None, None)

    # Unset mapping is identity, which is the native-run case.
    assert _seen_by_streamer(host / "media" / "a.mkv").endswith("media/a.mkv")

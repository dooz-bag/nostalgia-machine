"""The sound under a generated card, and the card that carries the words.

The properties that matter: a card sounds the same every time it is rendered, a bed
that is silent is never laid under a card (hiss turned up is worse than no bed),
only real text-on-black bumps are harvested (never a character's dialogue under a
card about a different show), and the renderer degrades to the old plain card when
there is no bank and no music, so adding either can only ever add.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from nostalgia import bumpgen as G
from nostalgia import bumplines as bl
from nostalgia import music as M

REPO = Path(__file__).resolve().parents[1]


# -- choosing a bed ------------------------------------------------------------------


def _library(*beds: M.Bed) -> M.MusicLibrary:
    return M.MusicLibrary(root=Path("/m"), beds=list(beds))


def _bed(name: str, seconds: float = 12.0, era: int | None = None) -> M.Bed:
    return M.Bed(path=Path("/m") / name, duration_s=seconds, era=era)


def test_an_empty_library_picks_nothing():
    assert not _library()
    assert _library().pick(year=2003) is None


def test_a_card_always_draws_the_same_bed():
    library = _library(*(_bed(f"{i}.m4a") for i in range(8)))
    first = library.pick(year=2003, seed_parts=("athf", "NEXT", "a line"))
    assert first == library.pick(year=2003, seed_parts=("athf", "NEXT", "a line"))
    picks = {library.pick(year=2003, seed_parts=("x", i)).path for i in range(40)}
    assert len(picks) > 3, "different cards must not all share one track"


def test_a_bed_too_short_to_cover_a_card_is_never_chosen():
    library = _library(_bed("blip.m4a", 1.0), _bed("ok.m4a", 10.0))
    assert {library.pick(year=None, seed_parts=(i,)).path.name for i in range(20)} == {"ok.m4a"}
    assert _library(_bed("blip.m4a", 1.0)).pick(year=None) is None


def test_a_bed_from_the_right_era_is_preferred_and_an_untagged_one_fits_anywhere():
    library = _library(_bed("old.m4a", era=1995), _bed("near.m4a", era=2003), _bed("any.m4a", era=None))
    chosen = {library.pick(year=2003, seed_parts=(i,)).path.name for i in range(60)}
    assert chosen == {"near.m4a", "any.m4a"}, "a 1995 bed does not belong on a 2003 card"


def test_with_nothing_near_in_time_any_bed_beats_silence():
    library = _library(_bed("far.m4a", era=1980))
    assert library.pick(year=2003).path.name == "far.m4a"


def test_the_start_offset_stays_inside_the_bed_and_is_stable():
    library = _library()
    bed = _bed("b.m4a", 12.0)
    offsets = {library.offset_for(bed, 4.2, (i,)) for i in range(30)}
    assert all(0 <= o <= 12.0 - 4.2 for o in offsets)
    assert len(offsets) > 5, "two cards on one track should not match"
    assert library.offset_for(bed, 4.2, (7,)) == library.offset_for(bed, 4.2, (7,))
    assert library.offset_for(_bed("short.m4a", 4.0), 4.2) == 0.0


# -- loading the library ---------------------------------------------------------------


def test_a_missing_music_folder_is_empty_not_an_error(tmp_path: Path):
    assert not M.load_library(tmp_path / "nope")
    assert not M.load_library(None)


def test_only_audio_files_are_beds_and_durations_are_cached(tmp_path: Path, monkeypatch):
    (tmp_path / "a.mp3").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.WAV").write_bytes(b"xx")
    (tmp_path / "notes.txt").write_text("not music")
    (tmp_path / "clip.mp4").write_bytes(b"video, not a bed")

    calls = []
    monkeypatch.setattr(M, "_duration", lambda path, ffprobe: calls.append(path) or 9.0)
    first = M.load_library(tmp_path, ffprobe="ffprobe")
    assert sorted(b.path.name for b in first.beds) == ["a.mp3", "b.WAV"]
    assert len(calls) == 2

    again = M.load_library(tmp_path, ffprobe="ffprobe")
    assert len(again.beds) == 2 and len(calls) == 2, "the second load must not re-probe"


def test_beds_come_from_your_folder_and_from_the_harvest_folder(tmp_path: Path, monkeypatch):
    """The media mount is read-only except `derived/`, so harvests cannot live beside your files."""
    own, harvested = tmp_path / "music", tmp_path / "derived" / "music" / "harvested"
    own.mkdir()
    harvested.mkdir(parents=True)
    (own / "mine.mp3").write_bytes(b"x")
    (harvested / "taken.m4a").write_bytes(b"xx")
    monkeypatch.setattr(M, "_duration", lambda path, ffprobe: 9.0)

    roots = M.music_roots(tmp_path)
    assert roots == [tmp_path / "music", tmp_path / "derived" / "music"]
    library = M.load_library(roots, ffprobe="ffprobe")
    assert sorted(b.path.name for b in library.beds) == ["mine.mp3", "taken.m4a"]
    assert M.load_library([tmp_path / "nope", own], ffprobe="ffprobe").beds, "one missing root is not fatal"


def test_a_read_only_folder_still_loads(tmp_path: Path, monkeypatch):
    (tmp_path / "a.mp3").write_bytes(b"x")
    monkeypatch.setattr(M, "_duration", lambda path, ffprobe: 9.0)

    def refuse(root, index):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(M, "_write_index", refuse)
    assert len(M.load_library(tmp_path, ffprobe="ffprobe").beds) == 1


def test_an_unreadable_file_is_skipped_not_fatal(tmp_path: Path, monkeypatch):
    (tmp_path / "broken.mp3").write_bytes(b"x")
    monkeypatch.setattr(M, "_duration", lambda path, ffprobe: 0.0)
    assert not M.load_library(tmp_path, ffprobe="ffprobe")


# -- laying a bed under a card -----------------------------------------------------------

LOUD = {"input_i": -20.0, "input_tp": -4.0, "input_lra": 3.0, "input_thresh": -30.0, "target_offset": 0.2}


def test_a_bed_is_levelled_on_the_stretch_that_is_heard_and_faded(monkeypatch):
    seen = {}

    def measure(path, ffmpeg, params, *, start=None, duration=None):
        seen.update(start=start, duration=duration, target=params.target_lufs)
        return LOUD

    monkeypatch.setattr(M, "measure_loudness", measure)
    chain = M.segment_filter(Path("/m/b.m4a"), 3.5, 4.2, "ffmpeg")
    assert seen == {"start": 3.5, "duration": 4.2, "target": M.BED_LUFS}, "measure the segment, not the track"
    assert "loudnorm" in chain and "measured_I=-20.0" in chain and "linear=true" in chain
    assert chain.count("afade=t=") == 2, "in and out, so it does not start or stop on a click"


def test_a_silent_bed_is_dropped_rather_than_boosted(monkeypatch):
    """Normalising a near-silent file to -18 LUFS turns tape hiss into the music."""
    quiet = {**LOUD, "input_i": -61.0}
    monkeypatch.setattr(M, "measure_loudness", lambda *a, **k: quiet)
    assert M.segment_filter(Path("/m/b.m4a"), 0, 4, "ffmpeg") is None
    monkeypatch.setattr(M, "measure_loudness", lambda *a, **k: None)
    assert M.segment_filter(Path("/m/b.m4a"), 0, 4, "ffmpeg") is None


def test_the_fade_out_never_exceeds_half_the_card(monkeypatch):
    monkeypatch.setattr(M, "measure_loudness", lambda *a, **k: LOUD)
    chain = M.segment_filter(Path("/m/b.m4a"), 0, 1.0, "ffmpeg")
    assert "afade=t=out:st=0.500:d=0.500" in chain


# -- harvesting from real bumps ------------------------------------------------------------


def test_only_dark_cards_with_a_real_bed_qualify():
    assert M.CardAnalysis(0.9, -20.0).is_text_card_with_bed
    assert not M.CardAnalysis(0.2, -20.0).is_text_card_with_bed, "show footage: its audio is dialogue"
    assert not M.CardAnalysis(0.9, None).is_text_card_with_bed, "a black card with no audio"
    assert not M.CardAnalysis(0.9, -70.0).is_text_card_with_bed, "silent"
    assert M.CardAnalysis(M.DARK_SHARE, M.BED_FLOOR_LUFS + 1).is_text_card_with_bed


class _Asset:
    def __init__(self, path: Path, key: str, era=2003, generated=False):
        self.path, self.key, self.era_from, self.is_generated = str(path), key, era, generated


def test_harvest_takes_text_cards_and_leaves_show_footage_alone(tmp_path: Path, monkeypatch):
    card, footage = tmp_path / "card.mp4", tmp_path / "footage.mp4"
    card.write_bytes(b"x")
    footage.write_bytes(b"x")
    monkeypatch.setattr(
        M, "analyse_bump",
        lambda path, ffmpeg: M.CardAnalysis(0.95, -22.0) if path.name == "card.mp4" else M.CardAnalysis(0.1, -22.0),
    )
    monkeypatch.setattr(M, "measure_loudness", lambda *a, **k: LOUD)
    monkeypatch.setattr(M, "_duration", lambda path, ffprobe: 15.0)

    def encode(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"audio")
        import subprocess as sp

        return sp.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(M.subprocess, "run", encode)

    out = tmp_path / "music" / "harvested"
    report = M.harvest(
        [_Asset(card, "bump:card", era=2003), _Asset(footage, "bump:footage")], out, ffmpeg="ffmpeg"
    )
    assert report.taken == ["bump:card"] and report.skipped == 1
    assert [p.name for p in out.glob("*.m4a")] == ["card.m4a"]

    library = M.load_library(out.parent, ffprobe="ffprobe")
    (bed,) = library.beds
    assert bed.era == 2003, "the bed remembers the era of the bump it came from"
    assert bed.source.startswith("harvested:")


def test_harvest_is_resumable_and_ignores_generated_cards(tmp_path: Path, monkeypatch):
    card = tmp_path / "card.mp4"
    card.write_bytes(b"x")
    monkeypatch.setattr(M, "analyse_bump", lambda path, ffmpeg: M.CardAnalysis(0.95, -22.0))
    monkeypatch.setattr(M, "measure_loudness", lambda *a, **k: LOUD)
    monkeypatch.setattr(M, "_duration", lambda path, ffprobe: 15.0)
    runs = []

    def encode(cmd, **kwargs):
        runs.append(cmd)
        Path(cmd[-1]).write_bytes(b"audio")
        import subprocess as sp

        return sp.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(M.subprocess, "run", encode)
    out = tmp_path / "music" / "harvested"
    assets = [_Asset(card, "bump:card"), _Asset(card, "bump:gen", generated=True)]

    first = M.harvest(assets, out, ffmpeg="ffmpeg")
    assert first.taken == ["bump:card"], "a generated card has no original sound to take"
    second = M.harvest(assets, out, ffmpeg="ffmpeg")
    assert second.taken == [] and len(runs) == 1, "an existing bed is not extracted twice"


def test_a_failed_extraction_is_reported_and_leaves_no_partial_file(tmp_path: Path, monkeypatch):
    card = tmp_path / "card.mp4"
    card.write_bytes(b"x")
    monkeypatch.setattr(M, "analyse_bump", lambda path, ffmpeg: M.CardAnalysis(0.95, -22.0))
    monkeypatch.setattr(M, "measure_loudness", lambda *a, **k: LOUD)

    def boom(cmd, **kwargs):
        import subprocess as sp

        return sp.CompletedProcess(cmd, 1, "", "no audio stream")

    monkeypatch.setattr(M.subprocess, "run", boom)
    out = tmp_path / "music" / "harvested"
    report = M.harvest([_Asset(card, "bump:card")], out, ffmpeg="ffmpeg")
    assert report.failed and not report.taken
    assert not list(out.glob("*"))


# -- the card renderer --------------------------------------------------------------------


@pytest.fixture(scope="module")
def pack() -> G.EraPack:
    return G.load_era_packs(REPO / "config" / "bumps-era.yaml")


def _bank() -> bl.LineBank:
    return bl.LineBank(
        lines={
            "NEXT": tuple(f"next: {{show}}. variant {i}." for i in range(6)) + ("next: more of this.",),
            "AFTER": ("that was {show}. over.",),
            "ANY": ("nothing is wrong.", "we are still here."),
        }
    )


TITLES = {"athf": "Aqua Teen Hunger Force", "futurama": "Futurama"}


def test_with_no_bank_the_cards_are_exactly_the_old_plain_ones():
    specs = G.specs_for_schedule(shows=["athf", "futurama"], titles=TITLES, year=2003)
    assert len(specs) == 6 and all(s.text is None and not s.music_path for s in specs)


def test_a_bank_gives_each_show_and_position_several_sentences():
    specs = G.specs_for_schedule(
        shows=["athf"], titles=TITLES, year=2003, positions=("NEXT",), bank=_bank(), variants=3
    )
    own = [s for s in specs if s.subject == "athf"]
    assert len(own) == 3 and len({s.text for s in own}) == 3
    assert all("Aqua Teen Hunger Force" in s.text and "{show}" not in s.text for s in own)


def test_general_lines_become_show_less_cards_rendered_once():
    specs = G.specs_for_schedule(
        shows=["athf", "futurama"], titles=TITLES, year=2003, positions=("NEXT",), bank=_bank()
    )
    general = [s for s in specs if s.subject == "neutral" and s.position == "NEXT"]
    assert [s.text for s in general] == ["next: more of this."], "one card, not one per show"
    any_cards = [s for s in specs if s.position == "ANY"]
    assert {s.text for s in any_cards} == {"nothing is wrong.", "we are still here."}
    assert all(s.subject == "neutral" for s in any_cards)


def test_a_position_the_bank_has_nothing_for_still_gets_a_plain_card():
    """Adding lines may only add variety; it must never leave a slot with nothing."""
    specs = G.specs_for_schedule(
        shows=["athf"], titles=TITLES, year=2003, positions=("INTO",), bank=_bank()
    )
    (plain,) = [s for s in specs if s.subject == "athf" and s.position == "INTO"]
    assert plain.text is None


def test_spec_choice_is_deterministic():
    one = G.specs_for_schedule(shows=["athf"], titles=TITLES, year=2003, positions=("NEXT",), bank=_bank())
    two = G.specs_for_schedule(shows=["athf"], titles=TITLES, year=2003, positions=("NEXT",), bank=_bank())
    assert one == two


def test_music_is_attached_deterministically_when_there_is_a_library():
    library = _library(*(_bed(f"{i}.m4a") for i in range(5)))
    kwargs = dict(shows=["athf"], titles=TITLES, year=2003, positions=("NEXT",), bank=_bank(), music=library)
    one, two = G.specs_for_schedule(**kwargs), G.specs_for_schedule(**kwargs)
    assert one == two
    assert all(s.music_path and s.music_id for s in one)
    assert len({s.music_path for s in one}) > 1


def test_the_cache_key_changes_with_the_sentence_and_the_bed(pack: G.EraPack):
    style = pack.style_for(2003)
    base = G.BumpSpec(subject="athf", position="NEXT", year=2003, show_title="ATHF")
    key = lambda s: G.cache_key(s, style, 1280, 720)  # noqa: E731
    said = dataclasses.replace(base, text="next: ATHF.")
    assert key(base) != key(said)
    assert key(said) != key(dataclasses.replace(said, text="next: ATHF. again."))
    assert key(said) != key(dataclasses.replace(said, music_id="a.m4a@1"))
    assert key(said) == key(dataclasses.replace(said))


def _command(pack, spec, audio_filter=None):
    style = pack.style_for(2003)
    return G._build_command(spec, style, pack, width=1280, height=720, duration_ms=4200, audio_filter=audio_filter)


def test_a_card_with_no_music_still_carries_a_silent_audio_track(pack):
    """A card with no audio stream at all cannot be joined to its neighbours."""
    command = _command(pack, G.BumpSpec(subject="athf", position="NEXT", year=2003, show_title="ATHF"))
    joined = " ".join(command)
    assert "anullsrc" in joined and "-an" not in command
    assert command[command.index("-map") + 1] == "0:v:0" and "1:a:0" in command


def test_a_card_with_music_lays_the_bed_at_its_offset(pack):
    spec = G.BumpSpec(subject="athf", position="NEXT", year=2003, show_title="ATHF",
                      music_path="/m/b.m4a", music_offset=3.25, music_id="b@3.25")
    command = _command(pack, spec, audio_filter="loudnorm=I=-18")
    assert "/m/b.m4a" in command and command[command.index("-ss") + 1] == "3.250"
    assert "loudnorm=I=-18" in command and "anullsrc" not in " ".join(command)


def test_a_silent_bed_falls_back_to_a_silent_card(pack):
    """`segment_filter` returned None: no bed beats hiss."""
    spec = G.BumpSpec(subject="athf", position="NEXT", year=2003, music_path="/m/b.m4a", music_id="x")
    assert "anullsrc" in " ".join(_command(pack, spec, audio_filter=None))


def test_a_sentence_is_wrapped_over_several_lines_and_the_apostrophe_is_safe(pack):
    spec = G.BumpSpec(
        subject="athf", position="AFTER", year=2003,
        text="you've just watched aqua teen hunger force. no one is going to ask how you feel",
    )
    command = _command(pack, spec)
    vf = command[command.index("-vf") + 1] if "-vf" in command else command[command.index("-filter_complex") + 1]
    values = re.findall(r"text='((?:[^'\\]|\\.)*)'", vf)
    assert len(values) >= 3, "a sentence wraps; it is not one overflowing line"
    assert not any("'" in v.replace("\\'", "") for v in values), "a straight apostrophe would end the string"
    assert any("\u2019" in v for v in values)


def test_a_plain_card_still_prints_the_position_word_and_title(pack):
    vf = _command(pack, G.BumpSpec(subject="athf", position="NEXT", year=2003, show_title="Futurama"))
    text = " ".join(vf)
    assert "FUTURAMA" in text.upper()

"""Bump placement: how many cards, and exactly where.

The capability already existed in ``continuity.yaml``, but only as a flat list of
interleaved bump / pod / episode segments. Answering "two cards before the show,
none after" meant inserting YAML list items in the right places, which is how a
break template gets silently broken. These tests pin the translation in both
directions.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from nostalgia.web.continuity_edit import (
    SLOT_FAMILIES,
    ContinuityEditError,
    MidBreak,
    apply_form,
    parse_form,
    pod_shares,
    read_all,
    read_form,
    template_for,
)

CONFIG = Path(__file__).resolve().parents[1] / "config"

#: A copy of ``continuity.yaml`` as it ships, taken from the last commit that
#: touched it. Tests that describe *the shipped defaults* must not read the live
#: config: that file is the one the schedule page rewrites, so the moment anyone
#: changes the card counts in the UI, every assertion about "one card at each
#: position" starts failing for a reason that has nothing to do with the code.
#: Editing a template is a supported feature, not a broken build.
SHIPPED = Path(__file__).resolve().parent / "fixtures" / "continuity-shipped.yaml"


@pytest.fixture()
def document():
    reader = YAML()
    reader.preserve_quotes = True
    return reader.load((SHIPPED).read_text(encoding="utf-8"))


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(CONFIG, target)
    return target


def dump(document) -> str:
    reader = YAML()
    reader.preserve_quotes = True
    reader.width = 4096
    buffer = io.StringIO()
    reader.dump(document, buffer)
    return buffer.getvalue()


# -- reading the shipped templates ----------------------------------------


def test_every_slot_family_has_a_template(document) -> None:
    for family in SLOT_FAMILIES:
        assert template_for(document, family) is not None, family


def test_the_shipped_templates_read_as_one_card_at_each_standard_point(document) -> None:
    for family in SLOT_FAMILIES:
        form = read_form(document, family)
        assert (form.into, form.after, form.next) == (1, 1, 1)


def test_the_live_config_is_never_assumed_to_hold_the_shipped_counts() -> None:
    """Editing a template must not be able to fail the build.

    The card counts are a supported setting on the schedule page, so the live
    ``config/continuity.yaml`` is expected to drift from what ships. Every test
    above reads the shipped snapshot instead; this one states the invariant that
    makes that necessary, so nobody "fixes" the fixture back to the live file.
    """
    reader = YAML()
    reader.preserve_quotes = True
    live = reader.load((CONFIG / "continuity.yaml").read_text(encoding="utf-8"))
    shipped = reader.load(SHIPPED.read_text(encoding="utf-8"))

    for family in SLOT_FAMILIES:
        # Both files must be structurally valid, whatever counts they carry.
        assert read_form(live, family) is not None, family
        assert read_form(shipped, family) is not None, family


def test_mid_breaks_are_read_with_their_card_counts(document) -> None:
    short = read_form(document, "short")
    half = read_form(document, "half")
    hour = read_form(document, "hour")
    assert short.mid == [], "the short template has no mid-break"
    assert len(half.mid) == 1 and (half.mid[0].out, half.mid[0].into) == (1, 1)
    assert len(hour.mid) == 2


def test_the_card_cap_is_read_from_the_template(document) -> None:
    """16s, because the classic cards run 15.0-15.4s and a lower cap drops them."""
    assert read_form(document, "half").max_seconds == 16.0


def test_pod_shares_come_from_the_template_itself(document) -> None:
    """A hand-tuned balance must not be reset just because a count changed."""
    assert pod_shares(template_for(document, "short")) == (0.34, 0.62)
    assert pod_shares(template_for(document, "half")) == (0.30, 0.66)


def test_pod_shares_have_a_sane_default_for_a_missing_template() -> None:
    assert pod_shares(None) == (0.30, 0.66)


# -- writing ---------------------------------------------------------


def test_counts_become_segments_in_slot_order(document) -> None:
    form = parse_form("half", {"half.into": "2", "half.after": "0", "half.next": "2"})
    template = template_for(document, "half")
    apply_form(template, form, pod_shares=pod_shares(template))

    kinds = [(s["kind"], s.get("position")) for s in template["segments"]]
    assert kinds == [
        ("ad_pod", None),
        ("bump", "INTO"),
        ("bump", "INTO"),
        ("episode", None),
        ("ad_pod", None),
        ("bump", "NEXT"),
        ("bump", "NEXT"),
    ]


def test_the_episode_segment_is_never_lost(document) -> None:
    """A template with no episode would have nothing to air."""
    form = parse_form("half", {"half.into": "0", "half.after": "0", "half.next": "0"})
    template = template_for(document, "half")
    apply_form(template, form, pod_shares=pod_shares(template))
    assert any(s["kind"] == "episode" for s in template["segments"])


def test_a_mid_break_is_written_as_cards_around_a_pod(document) -> None:
    form = parse_form(
        "half",
        {
            "half.into": "1",
            "half.after": "1",
            "half.next": "1",
            "half.mid0.out": "1",
            "half.mid0.into": "1",
            "half.mid0.percent": "40",
            "half.mid0.pod": "90",
        },
    )
    template = template_for(document, "half")
    apply_form(template, form, pod_shares=pod_shares(template))

    mid = template["mid"][0]
    assert mid["after_percent"] == 40
    kinds = [(s["kind"], s.get("position")) for s in mid["segments"]]
    assert kinds == [("bump", "OUT"), ("ad_pod", None), ("bump", "INTO")]
    assert mid["segments"][1]["seconds"] == 90


def test_a_counts_only_edit_leaves_the_mid_break_alone(document) -> None:
    """Changing "before/after" must not quietly delete a hand-written mid-break.

    The form submits every mid field it renders, so a submission that mentions no
    mid at all is a caller editing counts only -- treating the silence as "zero"
    removed a real break (and took the comments attached to it) with it.
    """
    form = parse_form("half", {"half.into": "1", "half.after": "1", "half.next": "1"})
    template = template_for(document, "half")
    apply_form(template, form, pod_shares=pod_shares(template))
    assert [m.percent for m in read_form(document, "half").mid] == [45.0]


def test_clearing_both_mid_counts_removes_the_key(document) -> None:
    """An empty `mid:` says nothing that an absent `mid:` does not."""
    form = parse_form("half", {"half.mid0.out": "0", "half.mid0.into": "0"})
    template = template_for(document, "half")
    apply_form(template, form, pod_shares=pod_shares(template))
    assert "mid" not in template


def test_a_round_trip_is_stable(document) -> None:
    """Reading, writing and re-reading must not drift."""
    template = template_for(document, "hour")
    before = read_form(document, "hour")
    apply_form(template, before, pod_shares=pod_shares(template))
    after = read_form(document, "hour")
    assert before == after


def test_other_templates_and_comments_survive(config_dir: Path) -> None:
    """The whole file is rewritten, so untouched parts must be preserved."""
    from nostalgia.web.continuity_edit import read_all

    text_before = (config_dir / "continuity.yaml").read_text(encoding="utf-8")
    reader = YAML()
    reader.preserve_quotes = True
    document = reader.load(text_before)

    form = parse_form("half", {"half.into": "3", "half.after": "0", "half.next": "1"})
    template = template_for(document, "half")
    apply_form(template, form, pod_shares=pod_shares(template))
    text = dump(document)

    assert "# Continuity: how a slot is broken up." in text
    assert "share: 0.34" in text, "the short template was disturbed"
    assert read_form(document, "short") == read_all(document)[0]


# -- validation ---------------------------------------------------------


def test_a_wild_count_is_clamped_not_written(document) -> None:
    form = parse_form("half", {"half.into": "99", "half.after": "-3", "half.next": "1"})
    assert form.into == 4
    assert form.after == 0


def test_a_non_numeric_count_is_refused() -> None:
    with pytest.raises(ContinuityEditError):
        parse_form("half", {"half.into": "lots"})


def test_the_card_cap_is_bounded(document) -> None:
    assert parse_form("half", {"half.max_seconds": "999"}).max_seconds == 60.0
    assert parse_form("half", {"half.max_seconds": "0"}).max_seconds == 2.0


def test_a_mid_break_with_no_cards_is_dropped() -> None:
    """Zero on both sides is "no mid-break", not an empty pod."""
    form = parse_form("half", {"half.mid0.out": "0", "half.mid0.into": "0"})
    assert form.mid == []


def test_an_unknown_family_is_not_editable(document) -> None:
    assert template_for(document, "movie") is None


def test_total_cards_counts_every_point() -> None:
    form = parse_form(
        "half",
        {"half.into": "2", "half.after": "1", "half.next": "1", "half.mid0.out": "1", "half.mid0.into": "1"},
    )
    assert form.total_cards() == 6


# -- comments survive a rebuild ------------------------------------------

# `continuity.yaml` is mostly prose that explains why each break is shaped the way
# it is, and the heading above a template is stored at the *end of the previous
# template*. Rebuilding the middle of the file therefore used to eat the next
# section's heading -- twice, in two different ruamel slots.


def _round_trip(document, family: str, submitted: dict[str, str]) -> str:
    form = parse_form(family, submitted)
    template = template_for(document, family)
    apply_form(template, form, pod_shares=pod_shares(template))
    buffer = io.StringIO()
    writer = YAML()
    writer.preserve_quotes = True
    writer.width = 4096
    writer.indent(mapping=2, sequence=4, offset=2)
    writer.dump(document, buffer)
    return buffer.getvalue()


def _comment_count(text: str) -> int:
    return text.count("#")


@pytest.mark.parametrize(
    "submitted",
    [
        # counts only: the mid-break list is rebuilt but keeps its length
        {"half.into": "2", "half.after": "1", "half.next": "1"},
        # the mid-break is rebuilt with a different length
        {
            "half.into": "2",
            "half.after": "1",
            "half.next": "1",
            "half.mid0.out": "1",
            "half.mid0.into": "1",
            "half.mid0.percent": "45",
        },
        # the mid-break is removed entirely, so `mid:` disappears as a key
        {"half.into": "1", "half.after": "1", "half.next": "1", "half.mid0.out": "0", "half.mid0.into": "0"},
    ],
    ids=["counts-only", "mid-rebuilt", "mid-cleared"],
)
def test_every_comment_survives_and_the_next_heading_is_not_lost(document, submitted) -> None:
    source = (CONFIG / "continuity.yaml").read_text(encoding="utf-8")
    text = _round_trip(document, "half", submitted)
    assert _comment_count(text) == _comment_count(source)
    assert "# 60-minute block" in text
    assert document["continuity"]["default-hour"]["name"] == "Hour block"


def test_a_rebuilt_mid_keeps_its_own_percentage(document) -> None:
    """A hand-tuned 45% must not silently become the 50% default."""
    _round_trip(
        document,
        "half",
        {"half.mid0.out": "1", "half.mid0.into": "1", "half.mid0.percent": "45"},
    )
    assert [m.percent for m in read_form(document, "half").mid] == [45.0]
    assert [m.pod_seconds for m in read_form(document, "half").mid] == [60]


def test_hand_written_scalars_keep_their_spelling(document) -> None:
    """`share: 0.30` must not come back as `0.3` -- it is not part of this edit."""
    text = _round_trip(document, "half", {"half.into": "2", "half.after": "1", "half.next": "1"})
    assert "share: 0.30" in text
    assert "share: 0.66" in text

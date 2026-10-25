"""Confirming a bump's tags must never write a value the scanner will reject."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nostalgia.sidecars import POSITIONS, SIDECAR_SUFFIX, read
from nostalgia.web.view import apply_tag


def _first_bump_key(catalog) -> str:
    bumps = catalog.assets(("bump",))
    assert bumps
    return bumps[0].key


def _media_path(sidecar) -> Path:
    name = sidecar.name
    assert name.endswith(SIDECAR_SUFFIX)
    return sidecar.with_name(name[: -len(SIDECAR_SUFFIX)])


def test_position_is_written_verbatim(catalog) -> None:
    key = _first_bump_key(catalog)
    sidecar = apply_tag(catalog, key, subject="futurama", position="next")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["position"] == "NEXT"
    assert payload["subject"] == "futurama"
    # The scanner's own reader must accept what we just wrote: a sidecar it cannot
    # parse is a warning on every subsequent scan.
    assert read(_media_path(sidecar)).position == "NEXT"


def test_a_placeholder_position_is_rejected_not_stored(catalog) -> None:
    """Regression: the review form's blank option submitted the text 'position?'.

    An <option> with no value attribute submits its own label, so the placeholder
    became a real position and every scan warned about it.
    """
    key = _first_bump_key(catalog)
    with pytest.raises(ValueError, match="position must be one of"):
        apply_tag(catalog, key, subject="futurama", position="position?")


def test_a_blank_position_means_unspecified(catalog) -> None:
    key = _first_bump_key(catalog)
    sidecar = apply_tag(catalog, key, subject="futurama", position="   ")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert "position" not in payload


@pytest.mark.parametrize("bad", ["Futurama", "futu rama", "futu_rama", "-bad", "bad-"])
def test_invalid_subjects_are_rejected(catalog, bad: str) -> None:
    key = _first_bump_key(catalog)
    with pytest.raises(ValueError, match="not a valid show id"):
        apply_tag(catalog, key, subject=bad)


def test_every_valid_position_is_accepted(catalog) -> None:
    key = _first_bump_key(catalog)
    for position in POSITIONS:
        sidecar = apply_tag(catalog, key, subject="futurama", position=position)
        assert json.loads(sidecar.read_text(encoding="utf-8"))["position"] == position

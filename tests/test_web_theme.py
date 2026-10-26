"""The web UI wears a broadcast test card, and the channel runs on JST.

Two things worth pinning, because both are easy to break silently:

* the test pattern is markup + CSS with no build step, so nothing fails loudly if
  a strip disappears -- it just renders subtly wrong;
* the timezone is declared in *two* places (the schedule and the container
  clock), and they must agree or the guide and the clock drift apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nostalgia.config import RootConfig
from nostalgia.web.app import _card, _page, _test_card

NTSC = "ntsc"
PAL = "pal"


# -- the test card ---------------------------------------------------------


def test_the_page_wears_the_crt_class() -> None:
    html = _page("x", "body")
    assert 'class="crt"' in html
    assert "scanlines" in html or "repeating-linear-gradient" in html


def test_pal_adds_its_own_class() -> None:
    html = _page("x", "body", card=PAL)
    assert 'class="crt pal"' in html


def test_ntsc_has_all_three_strips() -> None:
    card = _test_card(NTSC)
    assert 'class="bars"' in card
    assert 'class="castellations"' in card
    assert 'class="pluge"' in card


def test_pal_drops_the_castellation_strip() -> None:
    """PAL has no castellations; keeping them would be NTSC wearing a PAL label."""
    assert 'class="castellations"' not in _test_card(PAL)


def test_both_patterns_keep_seven_colour_bars() -> None:
    import re

    for card in (NTSC, PAL):
        markup = _test_card(card)
        bars = re.search(r'<div class="bars"[^>]*>(.*?)</div>', markup)
        assert bars is not None, card
        assert bars.group(1).count("<span></span>") == 7, card


def test_the_footer_names_the_right_standard() -> None:
    assert "SMPTE ECR 1-1978 NTSC" in _page("x", "body", card=NTSC)
    assert "EBU PAL" in _page("x", "body", card=PAL)


def test_the_pattern_is_hidden_from_assistive_tech() -> None:
    """It is decoration; a screen reader should not announce 19 empty spans."""
    card = _test_card(NTSC)
    assert card.count('aria-hidden="true"') == 3


# -- which pattern is selected ---------------------------------------------


def test_the_project_config_wears_ntsc(project_config: RootConfig) -> None:
    """[adult swim] was a US NTSC channel, and Japan used NTSC-J."""
    assert project_config.channel.test_card == NTSC
    assert _card(project_config) == NTSC


def test_pal_is_opt_in(project_config: RootConfig) -> None:
    payload = project_config.model_dump()
    payload["channel"] = {**payload["channel"], "test_card": "pal"}
    assert _card(RootConfig.model_validate(payload)) == PAL


def test_a_nonsense_card_value_is_rejected(config: RootConfig) -> None:
    payload = config.model_dump()
    payload["channel"] = {**payload["channel"], "test_card": "secam"}
    with pytest.raises(Exception):
        RootConfig.model_validate(payload)


# -- timezone --------------------------------------------------------------


@pytest.fixture()
def project_config() -> RootConfig:
    """The *project's* config, not the test fixture's.

    The fixture builds its own minimal library with its own timezone; these tests
    are about what the shipped channel is set to, so they read the real file.
    """
    from nostalgia.config import load_config

    root = Path(__file__).resolve().parents[1]
    return load_config(root / "config")


def test_the_channel_runs_on_japan_standard_time(project_config: RootConfig) -> None:
    assert project_config.channel.grid.timezone == "Asia/Tokyo"


def test_japan_standard_time_has_no_dst(project_config: RootConfig) -> None:
    """JST is UTC+9 all year, so every day is exactly 24h and the grid's DST
    branch never has to fire. Worth pinning: a wrong zone id would resolve to
    something with DST and quietly produce 23/25-hour days twice a year."""
    from datetime import datetime, timedelta

    from nostalgia.timeutil import tz

    zone = tz(project_config.channel.grid.timezone)
    offsets = {
        datetime(2026, month, 15, 12, tzinfo=zone).utcoffset()
        for month in range(1, 13)
    }
    assert len(offsets) == 1, offsets
    assert offsets.pop() == timedelta(hours=9)


def test_every_day_is_24_hours_in_japan(project_config: RootConfig) -> None:
    """No DST means no 23- or 25-hour days, so slot counts never shift."""
    from datetime import date, timedelta

    from nostalgia.grid import day_bounds_ms

    grid = project_config.channel.grid
    day = date(2026, 1, 1)
    while day.year == 2026:
        start, stop = day_bounds_ms(day, grid)
        assert stop - start == 24 * 3_600_000, day
        day += timedelta(days=1)


def test_the_config_default_is_still_ntsc(config: RootConfig) -> None:
    """The *model* default, independent of what any file says."""
    assert RootConfig.model_fields["channel"].annotation is not None
    from nostalgia.config import ChannelConfig

    assert ChannelConfig().test_card == NTSC


def test_the_container_clock_matches_the_schedule() -> None:
    """TZ configures the container; grid.timezone configures the schedule. If
    they disagree, log timestamps and the guide drift apart."""
    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    channel = (root / "config" / "channel.yaml").read_text(encoding="utf-8")
    assert "Asia/Tokyo" in compose
    assert 'timezone: "Asia/Tokyo"' in channel
    assert "America/New_York" not in compose.split("environment:")[1]

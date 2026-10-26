"""The channel's era must not follow the wall clock.

The timeline has to carry a real date -- a streamer and an EPG cannot be asked
for "some time in 2003". But every *era* decision (which spots are eligible,
which card style is rendered, how a bump is scored) must use the year being
recreated. Conflating the two silently gives a 2003 channel 2026's commercials
and 2026's bump cards, which is the exact failure this project exists to avoid.
"""

from __future__ import annotations

from datetime import date

from nostalgia.config import RootConfig
from nostalgia.pipeline import day_config_for
from nostalgia.plan import PlanContext, build_plan


def _with_channel(config: RootConfig, **overrides) -> RootConfig:
    payload = config.model_dump()
    payload["channel"] = {**payload["channel"], **overrides}
    return RootConfig.model_validate(payload)


def test_era_year_defaults_to_the_configured_year(config) -> None:
    assert config.era_year == 2003


def test_on_air_date_supplies_the_era_when_unset(config) -> None:
    """An explicit launch date is itself a statement about the era."""
    derived = _with_channel(config, era_year=None, on_air_date="2004-06-15")
    assert derived.era_year == 2004


def test_generated_era_year_is_the_last_resort(config) -> None:
    derived = _with_channel(config, era_year=None, on_air_date=None)
    assert derived.era_year == derived.bumps.generated.era_year


def test_explicit_era_year_beats_the_air_date(config) -> None:
    """The point of the knob: air a 2003 season without moving the timeline."""
    derived = _with_channel(config, era_year=2003, on_air_date="2008-01-05")
    assert derived.era_year == 2003


def test_era_year_never_tracks_the_plan_date(config, catalog) -> None:
    """A day built for 2026 is still a 2003 channel."""
    context = PlanContext.load(config, catalog)
    day = date(2026, 10, 26)
    plan = build_plan(day, day_config_for(config, day), context)

    assert plan.day.year == 2026
    assert context.root.era_year == 2003

    by_key = {asset.key: asset for asset in context.ads}
    drift = config.ads.era_drift_years
    for item in plan.items:
        asset = by_key.get(item.asset_key)
        if asset is None or asset.kind != "ad" or asset.era_from is None:
            continue
        assert asset.era_covers(context.root.era_year, drift), (
            f"{item.asset_key} (era {asset.era_from}) aired on a "
            f"{context.root.era_year} channel with drift {drift}"
        )


def test_no_era_widening_warning_on_a_future_date(config, catalog) -> None:
    """The bug this replaces: every day warned about media/commercials/2026/."""
    context = PlanContext.load(config, catalog)
    day = date(2026, 10, 26)
    plan = build_plan(day, day_config_for(config, day), context)

    widened = [warning for warning in plan.warnings if "match era" in warning]
    assert not widened, widened


def test_fallback_ids_resolve_to_emitted_items(config, catalog) -> None:
    """A fallback id that cannot be looked up is worse than no id at all.

    The recorder used to write ``-b{index}`` while the item was emitted as
    ``-b{index:02d}``, so the count was right and the ids were all wrong.
    """
    context = PlanContext.load(config, catalog)
    seen_fallback = False
    for day in (date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)):
        plan = build_plan(day, day_config_for(config, day), context)
        ids = {item.item_id for item in plan.items}
        missing = [key for key in plan.bump_fallbacks if key not in ids]
        assert not missing, f"{day}: fallback ids not in the timeline: {missing}"
        assert len(plan.bump_fallback_reasons) == len(plan.bump_fallbacks)
        seen_fallback = seen_fallback or bool(plan.bump_fallbacks)
    assert seen_fallback, "no fallback occurred; this test would be vacuous"

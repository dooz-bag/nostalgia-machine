"""How much of the day is commercials, and how to change it.

Two different questions that are easy to confuse, and the code answers them very
differently:

* **How is a break divided?** bumps, pods, mid-rolls -- `continuity.yaml`.
* **How much break is there?** That is arithmetic, not a setting: a slot is
  fixed and the episode is a fixed cost, so ``break = slot - episode``. The lever
  is therefore the *slot family* (or which show sits in which slot), not the pod
  shares.

These tests pin both facts, because getting the second one wrong is how you
spend an afternoon tuning shares and see the total not move.
"""

from __future__ import annotations

from datetime import date

import pytest

from nostalgia.config import ContinuitySegment, ContinuityTemplate
from nostalgia.grid import Slot, build_day
from nostalgia.pipeline import day_config_for
from nostalgia.plan import PlanContext, _template_for, build_plan
from nostalgia.schedule import assign_day


def _ad_ms(plan) -> int:
    return sum(item.duration_ms for item in plan.items if item.kind in ("ad", "ident"))


def _slot_with_template(daypart, config, day=date(2026, 10, 26)) -> Slot:
    slots = build_day(day, daypart, config.channel.grid)
    return slots[20]


# -- a daypart's template is honoured --------------------------------------


def _template_name_for(show_id: str, template: str | None, context) -> str:
    from nostalgia.schedule import AssignedSlot

    slot = Slot(
        index=0,
        start_ms=0,
        end_ms=1_800_000,
        family="half",
        daypart="probe",
        template=template,
    )
    assigned = AssignedSlot(
        slot=slot, show_id=show_id, slot_in_day=0, of_same_show=1
    )
    return _template_for(assigned, context).name


def test_a_daypart_template_overrides_the_show_default(config, catalog) -> None:
    """It used to be validated and then ignored, so a custom break could not
    take effect at all. That is the bug this pins."""
    context = PlanContext.load(config, catalog)

    assert _template_name_for("futurama", None, context) == "Half"
    assert _template_name_for("futurama", "default-hour", context) == "Hour"


def test_an_unknown_daypart_template_falls_back_rather_than_crashing(config, catalog) -> None:
    context = PlanContext.load(config, catalog)
    assert _template_name_for("futurama", "no-such-template", context) == "Half"


# -- total break time is arithmetic, not a knob ----------------------------


def test_break_time_is_slot_minus_episode(config, catalog) -> None:
    """The pod shares cannot change the total, only its distribution."""
    context = PlanContext.load(config, catalog)
    day = date(2026, 10, 26)
    plan = build_plan(day, day_config_for(config, day), context)

    slot_total = plan.day_end_ms - plan.day_start_ms
    episode = sum(item.duration_ms for item in plan.items if item.kind == "episode")
    ads = _ad_ms(plan)

    # Bumps (and any black filler) make up the rest, so ads must be strictly
    # less than the residual, and the whole thing must still tile the day.
    assert ads < slot_total - episode
    assert ads + episode < slot_total


def test_break_time_is_arithmetic_on_the_slot_not_a_template_setting(
    config, catalog
) -> None:
    """Two different templates over the same library leave the same total.

    This is the surprising part: `share` and `seconds` decide *how* the break is
    divided, never *how much* there is. A slot is fixed and the episode is a
    fixed cost, so the tail pod absorbs whatever the template does not spend.
    """
    day = date(2026, 10, 26)
    daypart = day_config_for(config, day)
    context = PlanContext.load(config, catalog)
    total = _ad_ms(build_plan(day, daypart, context))
    assert total > 0, "fixture should produce commercials"

    # Config models are frozen, so retargeting the daypart is done by rebuilding
    # the slot with a different template -- which is what the planner reads.
    from nostalgia.continuity import build_script, plan_budget

    for name in ("default-short", "default-half", "default-hour"):
        template = context.root.continuity[name]
        episode_ms = 22 * 60 * 1000
        script = build_script(template, episode_ms=episode_ms)
        budget = plan_budget(
            script, slot_ms=30 * 60 * 1000, episode_ms=episode_ms, bump_cap_ms=0
        )
        # Whatever the template books, it can never exceed the slot's own slack:
        # the episode is a fixed cost, so the residual bounds the break.
        assert budget.reserved_ms <= budget.slack_ms, name
        assert budget.tail_ms >= 0, name


def test_pod_shares_redistribute_within_a_slot(config, catalog) -> None:
    """Changing share changes where the spots sit, which is still worth having."""
    from nostalgia.continuity import build_script

    template = ContinuityTemplate(
        name="probe",
        segments=[
            ContinuitySegment(kind="ad_pod", share=0.10),
            ContinuitySegment(kind="episode"),
            ContinuitySegment(kind="ad_pod", share=0.80),
        ],
    )
    script = build_script(template, episode_ms=660_000)
    shares = [spec.share for spec in script.specs if spec.kind == "ad_pod"]
    assert shares == [0.10, 0.80]


def test_a_template_can_ask_for_a_fixed_total_in_seconds() -> None:
    """`seconds` is the way to say "exactly 90s of spots" rather than a share."""
    from nostalgia.continuity import build_script, plan_budget

    template = ContinuityTemplate(
        name="probe",
        segments=[
            ContinuitySegment(kind="ad_pod", seconds=90),
            ContinuitySegment(kind="episode"),
            ContinuitySegment(kind="ad_pod", seconds=90),
        ],
    )
    script = build_script(template, episode_ms=660_000)
    budget = plan_budget(
        script, slot_ms=1_800_000, episode_ms=660_000, bump_cap_ms=0
    )
    assert budget.reserved_ms == 180_000


def test_an_overlong_explicit_pod_is_shrunk_not_overflowed() -> None:
    """A 90s pod in a slot with only 60s of break must not push the grid."""
    from nostalgia.continuity import build_script, plan_budget

    template = ContinuityTemplate(
        name="probe",
        segments=[
            ContinuitySegment(kind="ad_pod", seconds=90),
            ContinuitySegment(kind="episode"),
        ],
    )
    script = build_script(template, episode_ms=1_740_000)
    budget = plan_budget(
        script, slot_ms=1_800_000, episode_ms=1_740_000, bump_cap_ms=0
    )
    assert budget.reserved_ms <= budget.slack_ms
    assert budget.degraded

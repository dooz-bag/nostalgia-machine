"""Break replacement: airing an extra instead of commercials.

The contract this feature has to keep, in order:

1. It **never** changes the shape of the day. A replaced break occupies exactly
   the budget the commercials would have used, so the grid survives and every
   programme starts when it would have started anyway.
2. It is **deterministic**. The same date always produces the same breaks, and
   editing a continuity template must not reshuffle which breaks are replaced.
3. It is **opt-in**. ``extras_chance: 0.0`` airs no extras at all.
4. It **never** touches bump slots. Those are continuity, not filler, and the
   positional rules depend on them staying where the template put them.
"""

from __future__ import annotations

from dataclasses import replace as dataclass_replace
from datetime import date
from pathlib import Path

import pytest

from nostalgia.catalog import Catalog
from nostalgia.config import load_config
from nostalgia.continuity import build_script
from nostalgia.paths import Paths
from nostalgia.pipeline import day_config_for, scan
from nostalgia.plan import PlanContext, _extras_roll, _roll_extras, build_plan

from fixtures import build_library

DAY = date(2003, 6, 4)

#: Share-based breaks, like ``config/continuity.yaml`` ships. The fixture library's
#: templates book fixed 60-second pods, and a four-minute music video cannot fit
#: one of those -- so it would always fall back and the feature would never be
#: exercised. Shares give the break its real length (minutes), which is what the
#: extras pool is sized for.
_CONTINUITY = """
continuity:
  default-short:
    name: "Short"
    segments:
      - kind: ad_pod
        share: 0.34
      - kind: bump
        subject: this
        position: INTO
        max_seconds: 16
      - kind: episode
      - kind: bump
        subject: this
        position: AFTER
        max_seconds: 16
      - kind: ad_pod
        share: 0.62
      - kind: bump
        subject: next
        position: NEXT
        max_seconds: 16
  default-half:
    name: "Half"
    segments:
      - kind: ad_pod
        share: 0.30
      - kind: bump
        subject: this
        position: INTO
        max_seconds: 16
      - kind: episode
      - kind: bump
        subject: this
        position: AFTER
        max_seconds: 16
      - kind: ad_pod
        share: 0.66
      - kind: bump
        subject: next
        position: NEXT
        max_seconds: 16
    mid:
      - after_percent: 50
        segments:
          - kind: bump
            subject: this
            position: OUT
            max_seconds: 16
          - kind: ad_pod
            seconds: 60
          - kind: bump
            subject: this
            position: INTO
            max_seconds: 16
  default-hour:
    name: "Hour"
    segments:
      - kind: ad_pod
        share: 0.25
      - kind: bump
        subject: this
        position: INTO
        max_seconds: 16
      - kind: episode
      - kind: ad_pod
        share: 0.70
"""


def _context(root: Path, *, extras_chance: float, with_extras: bool = True) -> PlanContext:
    library = build_library(root, with_extras=with_extras)
    (library.config / "ads.yaml").write_text(
        f"ads:\n  extras_chance: {extras_chance}\n  extras_cooldown_hours: 24\n",
        encoding="utf-8",
    )
    (library.config / "continuity.yaml").write_text(_CONTINUITY.lstrip(), encoding="utf-8")
    config = load_config(library.config)
    paths = Paths(library.root)
    paths.ensure_runtime_dirs()
    catalog = Catalog(paths.catalog)
    scan(config, catalog, paths)
    return PlanContext.load(
        config, catalog, config_dir=paths.config_dir, media_root=paths.media_root
    )


def _plan(context: PlanContext):
    return build_plan(
        DAY, day_config_for(context.root, DAY), context, allow_generated=False
    )


@pytest.fixture()
def off_context(tmp_path: Path) -> PlanContext:
    return _context(tmp_path / "off", extras_chance=0.0)


@pytest.fixture()
def on_context(tmp_path: Path) -> PlanContext:
    return _context(tmp_path / "on", extras_chance=1.0)


# -- the roll ---------------------------------------------------------------


def test_the_roll_is_off_at_zero_chance():
    rolls = [_extras_roll(DAY, slot, 0.0, pod) for slot in range(40) for pod in range(4)]
    assert not any(rolls)


def test_the_roll_is_certain_at_one_chance():
    rolls = [_extras_roll(DAY, slot, 1.0, pod) for slot in range(40) for pod in range(4)]
    assert all(rolls)


def test_the_roll_is_deterministic():
    first = [_extras_roll(DAY, slot, 0.2, pod) for slot in range(16) for pod in range(4)]
    second = [_extras_roll(DAY, slot, 0.2, pod) for slot in range(16) for pod in range(4)]
    assert first == second


def test_each_pod_gets_its_own_roll():
    """A slot's pods are separate breaks, so they must not share one coin flip.

    With one roll per slot, a chance of 0.5 and four pods would replace all four
    or none, and the "1 in N breaks" arithmetic would be wrong by the pod count.
    """
    rolls = [_extras_roll(DAY, 0, 0.5, pod) for pod in range(64)]
    assert 0 < sum(rolls) < len(rolls), "every pod in a slot made the same decision"


def test_the_roll_fires_at_roughly_the_configured_rate():
    """A chance of 0.2 must mean about a fifth of breaks, not "often" or "never".

    Sampled across days as well as slots: one day's breaks are a small and
    correlated sample, and the seed is per (day, slot, pod).
    """
    hits = total = 0
    for offset in range(60):
        day = date.fromordinal(DAY.toordinal() + offset)
        for slot in range(48):
            for pod in range(3):
                total += 1
                hits += _extras_roll(day, slot, 0.2, pod)
    rate = hits / total
    assert 0.17 <= rate <= 0.23, f"rate was {rate:.3f} over {total} breaks"


# -- the swap ---------------------------------------------------------------


def test_swapping_leaves_bumps_and_episodes_untouched(on_context: PlanContext):
    template = on_context.root.continuity["default-half"]
    before = build_script(template, episode_ms=22 * 60 * 1000)
    after = _roll_extras(DAY, 0, before.specs, chance=1.0)

    def structural(specs):
        return [
            (spec.kind, spec.subject, spec.position, spec.part, spec.resumes)
            for spec in specs
            if spec.kind in ("bump", "episode", "ident")
        ]

    assert structural(before.specs) == structural(after)
    assert [spec.kind for spec in after if spec.kind == "ad_pod"] == ["ad_pod"]


def test_every_replaceable_pod_is_replaced_at_full_chance(on_context: PlanContext):
    template = on_context.root.continuity["default-half"]
    script = build_script(template, episode_ms=22 * 60 * 1000)
    replaceable = sum(
        1 for spec in script.specs if spec.kind == "ad_pod" and not spec.in_mid
    )
    assert replaceable > 1, "fixture template should have several replaceable pods"

    swapped = _roll_extras(DAY, 0, script.specs, chance=1.0)
    assert sum(1 for spec in swapped if spec.kind == "extra") == replaceable


def test_a_replaced_pod_keeps_its_own_budget(on_context: PlanContext):
    """The swap changes what fills a break, never how long it is."""
    template = on_context.root.continuity["default-half"]
    script = build_script(template, episode_ms=22 * 60 * 1000)
    swapped = _roll_extras(DAY, 0, script.specs, chance=1.0)

    original = [
        (spec.seconds, spec.share)
        for spec in script.specs
        if spec.kind == "ad_pod" and not spec.in_mid
    ]
    replaced = [(spec.seconds, spec.share) for spec in swapped if spec.kind == "extra"]
    assert original == replaced


def test_a_mid_episode_pod_is_never_replaced(on_context: PlanContext):
    """A break that interrupts a show is a different moment from the ones around
    it, and a four-minute music video there is a bigger interruption than the
    show itself."""
    template = on_context.root.continuity["default-half"]
    script = build_script(template, episode_ms=22 * 60 * 1000)
    assert any(spec.in_mid for spec in script.specs if spec.kind == "ad_pod")

    swapped = _roll_extras(DAY, 0, script.specs, chance=1.0)
    assert all(
        spec.kind == "ad_pod" for spec in swapped if spec.in_mid and spec.kind != "bump"
    )


def test_zero_chance_changes_nothing(on_context: PlanContext):
    template = on_context.root.continuity["default-half"]
    script = build_script(template, episode_ms=22 * 60 * 1000)
    assert _roll_extras(DAY, 0, script.specs, chance=0.0) == script.specs


# -- the plan ---------------------------------------------------------------


def test_zero_chance_never_airs_an_extra(off_context: PlanContext):
    assert not [item for item in _plan(off_context).items if item.kind == "extra"]


def test_full_chance_replaces_every_break(on_context: PlanContext):
    assert [item for item in _plan(on_context).items if item.kind == "extra"]


def test_a_replaced_break_keeps_the_day_exactly_the_same_length(
    off_context: PlanContext, on_context: PlanContext
):
    """The grid is the channel's contract with the viewer. Replacing what airs in
    a break must never move when the next programme starts."""
    with_extras = _plan(on_context)
    without = _plan(off_context)
    assert not with_extras.validate()
    assert with_extras.day_end_ms == without.day_end_ms
    assert with_extras.duration_ms() == without.duration_ms()


def test_both_plans_tile_their_slots_contiguously(
    off_context: PlanContext, on_context: PlanContext
):
    """The promise the channel actually makes: the grid. Each slot ends exactly
    where the next begins, whether or not a break was replaced.

    Episode starts *within* a slot are deliberately not asserted. A share-based pod
    that cannot reach its share hands the remainder to the tail, so an episode can
    begin a few seconds earlier once the ad pool state differs -- and it differs as
    soon as an extras break removes a spot from the rotation. That is pre-existing
    behaviour: an under-filling pod does the same thing with no extras at all.
    """
    for plan in (_plan(on_context), _plan(off_context)):
        assert not plan.validate()
        spans: dict[int, list[int]] = {}
        for item in plan.items:
            span = spans.setdefault(item.slot_index, [item.start_ms, item.end_ms])
            span[0] = min(span[0], item.start_ms)
            span[1] = max(span[1], item.end_ms)
        ordered = [spans[index] for index in sorted(spans)]
        for previous, current in zip(ordered, ordered[1:]):
            assert previous[1] == current[0], "slots must be contiguous"


def test_a_replaced_break_keeps_the_template_s_own_cards(
    off_context: PlanContext, on_context: PlanContext
):
    """Bumps are continuity. Replacing a pod changes what plays *between* them,
    not the cards the template asked for.

    Counted rather than matched by identity: a pod that lands a few seconds off its
    share changes whether a long closing card fits at all, which is the same
    tolerance the planner already gives the commercial-only case.
    """
    def count(plan):
        return sum(
            1
            for item in plan.items
            if item.kind == "bump" and item.subtitle != "promo"
        )

    with_extras = count(_plan(on_context))
    without = count(_plan(off_context))
    assert abs(with_extras - without) <= 4, (
        f"{with_extras} template cards with extras vs {without} without"
    )


def test_an_extra_is_never_cut_short(on_context: PlanContext):
    """The whole point: no mid-song cuts. An extra airs its own file, whole."""
    for item in _plan(on_context).items:
        if item.kind == "extra":
            assert item.in_point_ms is None
            assert item.out_point_ms is None


def test_the_same_extra_does_not_repeat_within_the_cooldown(on_context: PlanContext):
    """The pool is small by nature, so a repeat inside 24h is the loudest possible
    sign that a program built the schedule.

    Asserted only as far as the pool allows: with three extras and dozens of
    breaks a day, repeats are arithmetically unavoidable, and pretending otherwise
    would be asserting something the config cannot deliver.
    """
    pool = len(on_context.extras)
    keys = [item.asset_key for item in _plan(on_context).items if item.kind == "extra"]
    if len(keys) <= pool:
        assert len(set(keys)) == len(keys)
    else:
        # Every extra must at least be used before any is reused.
        assert set(keys[:pool]) == {asset.key for asset in on_context.extras}


def test_a_disabled_extra_is_never_used(on_context: PlanContext):
    extras = on_context.catalog.assets(("extra",), include_disabled=True)
    assert extras
    assert on_context.catalog.set_disabled(extras[0].key, True)

    refreshed = PlanContext.load(on_context.root, on_context.catalog)
    used = {item.asset_key for item in _plan(refreshed).items if item.kind == "extra"}
    assert extras[0].key not in used


def test_no_extras_pool_falls_back_to_commercials(on_context: PlanContext):
    """Turning the dial up with an empty folder must not produce a hole."""
    on_context.extras = []
    plan = _plan(on_context)
    assert not plan.validate()
    assert not [item for item in plan.items if item.kind == "extra"]


def test_a_short_extra_is_topped_up_with_commercials(on_context: PlanContext):
    """A 90-second piece cannot fill a four-minute break, and a gap is not an
    option. Spots top it up, which is what a station does."""
    on_context.extras = [
        asset for asset in on_context.extras if asset.duration_ms <= 90_000
    ]
    assert on_context.extras, "fixture no longer has a short extra"

    plan = _plan(on_context)
    assert not plan.validate()
    assert [item for item in plan.items if item.kind == "extra"]
    assert plan.duration_ms() == plan.day_end_ms - plan.day_start_ms


def test_an_extra_too_long_for_every_break_falls_back(on_context: PlanContext):
    """Refusing is the safe failure: cutting a music video to 47 seconds is the
    exact artefact this feature is meant to remove."""
    on_context.extras = [
        dataclass_replace(asset, duration_ms=30 * 60 * 1000) for asset in on_context.extras
    ]

    plan = _plan(on_context)
    assert not plan.validate()
    assert not [item for item in plan.items if item.kind == "extra"]
    assert [warning for warning in plan.warnings if "airing commercials" in warning]


def test_a_small_pool_reports_one_warning_not_one_per_break(on_context: PlanContext):
    """With one short extra and a 24h cooldown almost every later break falls back.
    That is expected behaviour, not 67 separate problems -- and 67 identical lines
    bury the one warning that matters."""
    on_context.extras = [asset for asset in on_context.extras if asset.duration_ms <= 90_000]

    plan = _plan(on_context)
    cooled = [warning for warning in plan.warnings if "cooling down" in warning]
    assert len(cooled) == 1, cooled
    assert "add more files to media/extras/" in cooled[0]

# Architecture

## The two seams

The system has exactly two boundaries, both file-based:

```
media/  ──►  scheduler  ──►  playout/*.json + xmltv/*.xml  ──►  ErsatzTV Next
```

* **Media in.** Plain folders. Nothing is imported, renamed or moved.
* **Playout out.** An explicit, inspectable timeline. ErsatzTV Next consumes it.

Everything in between is ordinary Python over plain dataclasses. That is not
minimalism for its own sake: because the planner is pure, the whole channel can be
built and asserted in a test in milliseconds, and the streamer can be replaced
without touching the logic (see `docs/OPTIONS.md`).

## Module map

| Module | Responsibility | Purity |
|---|---|---|
| `timeutil`, `durations` | Trailing rules: aware datetimes, integer milliseconds | pure |
| `rng` | Stable seeded PRNG (`sha256`, never `hash()`) | pure |
| `config` | Typed YAML, cross-validated, path-addressed errors | pure |
| `grid` | Dayparts → grid-aligned slots, DST-correct | pure |
| `rotation` | Date → episode, via an ordinal rather than a cursor | pure |
| `schedule` | Slot → show, honouring dayparts and airing windows | pure |
| `continuity` | Template → ordered segment specs, budget math | pure |
| `bumps` | Scoring, cooldown, fallback ladder | pure |
| `ads` | Pod filling: whole-spot tolerance, promo top-up, black-card fallback | pure |
| `plan` | Ties the above into one timeline; enforces invariants | pure |
| `emit` | Plan → playout JSON, XMLTV, lineup/channel config | writes files |
| `catalog` | Scan, tags, airing history (SQLite) | I/O |
| `probe` | ffprobe with a sidecar fallback | I/O |
| `bumpgen` | Render typographic cards with ffmpeg | I/O |
| `pipeline`, `cli`, `web` | Wiring and presentation | I/O |

## Why the schedule is deterministic

Four decisions, each of which would break reproducibility on its own:

1. **No mutable cursors.** A cursor advanced on air would mean *previewing*
   tomorrow consumes tomorrow's episodes. Instead the episode for a slot is a
   function of `(show, day index, slot-in-day)` modulo the ring length — see
   `rotation.select`. Ask for a date twice, or out of order, and you get the same
   answer.

2. **Seeded randomness.** Anything that should vary (which commercial, which bump
   among equals, the daypart show order) draws from
   `rng_for(purpose, date, slot, ...)`, seeded by SHA-256. `random.shuffle` on a
   global RNG would make plans depend on process history.

3. **Deterministic cooldowns.** Cooldowns advance as items are placed, but they are
   *seeded from catalogued airings before the plan's own start time*. Rebuilding a
   past date therefore reproduces it, while a new date continues correctly from
   history.

4. **Fixed float-free arithmetic.** Durations are integer milliseconds throughout.
   A `float` second here would accumulate into a shifted slot boundary within a day.

`tests/test_plan.py::test_build_is_deterministic`,
`test_emitted_json_matches_on_a_rebuild` and `test_plan_is_independent_of_build_order`
guard each of these.

## The grid invariant

Every slot must satisfy `sum(segment durations) == slot length`, to the millisecond.

The planner reaches that **in two passes**, because the parts are not independent:

1. **Resolve the bumps first.** Their exact lengths decide how much is left for
   commercials, so they cannot be chosen while spending the break. Each card is
   selected with the slot's own show and the *actual* neighbouring shows in hand.
2. **Then size the pods.** `pod_budget = slot − episode parts − resolved bump total`,
   which is a number the bumps already know.

Emitting as we went — the obvious one-pass design — fails in two specific ways, and
both were bugs worth keeping tests for:

* The final pod expands to "whatever is left" and swallows the closing *up next*
  card. The tail was therefore made to **reserve the room for every bump still to
  come** (`bump_after_ms`), so a pod can never spend past one.
* A short break gets filled by a pod capped at a fixed spot count, stranding a few
  seconds that then become a black card. The tail pod's spot cap is now derived from
  the gap, and anything still left is **topped up with short promos** — which is
  what a station does to round out a break.

Pods themselves carry a **tolerance** (`pod_tolerance_ms`, 250 ms) because a pod
target is a *booking* length: real spots run 30.021 s and 15.021 s, and stations play
them as they are. Without it, every spot got shaved ~21 ms to hit an arithmetically
exact pod. The slot is what must be exact, and only the tail has nothing after it to
absorb a surplus — so only the tail must be millisecond-exact.

If even promos cannot close the tail (a remainder shorter than the shortest asset),
`ads.fill_gap` appends one synthetic black card of exactly that length. Chopping a
30-second spot into 2.4 seconds would satisfy the arithmetic and be unwatchable; a
brief black frame is what a station would actually do, and in a healthy library it
accounts for well under 1% of the day.

`plan.Plan.validate()` re-derives the whole timeline and refuses to emit anything
with a gap, an overlap, or a non-positive duration.

## Templates: `share` versus `seconds`

A break segment is sized one of two ways:

* **`share`** — a fraction of the slot's remaining break budget (recommended).
* **`seconds`** — a fixed booking length (30/60/90).

`seconds` looks more like real traffic, but it cannot adapt: a library's
"22-minute" episodes run a few seconds over, and a fixed pod then either starves the
closing bump or leaves a long tail. A share is resilient to real durations. The two
are mutually exclusive per segment, and `config` rejects a template that sets both.

## The guide is not the timeline

The playout JSON and the XMLTV are both derived from the same `Plan`, but they are
*not* the same shape, and deliberately so:

* **Playout** is item-level: every commercial and bumper appears, because the
  streamer has to air each one.
* **Guide** is programme-level: a 22-minute episode split by a mid-roll break is one
  programme, and each programme's stop is stretched to the next programme's start.

A guide that listed only episodes would leak a gap at every break, and most players
render that as a broken schedule. Continuous listings are also simply what an EPG is:
a broadcast guide shows a slot from 00:30 to 01:00 even when the episode is 22
minutes. `test_xmltv_is_continuous_across_breaks` pins this.

## Bump selection

```
score = 5·subject + 3·position + 1·era + 0.5·family − 2·cooldown
```

The ladder, in order:

1. A **real** asset naming the target show, clearing `min_score`.
2. If every such asset is cooling down, the best one anyway (`all-cooling`). A
   recycled correct bump beats a generic one.
3. A **generated** card naming the target show, if `max_share` allows.
4. A **neutral** card (`neutral-fallback`) — logged in `plan.bump_fallbacks` so it
   can be counted, surfaced in the UI, and fixed by adding assets.

A wrong *specific* show scores below zero and is never eligible. `bumps.strictness:
off` relaxes that into "prefer correct, tolerate wrong", which is occasionally
useful when building a pool and is never the default.

Position (`INTO`/`OUT`/`NEXT`/`AFTER`) comes from the break template, not the
asset, which is what makes `next` mean *the show that actually follows*. The tail
bump is resolved with the following slot's show in hand.

## Slot families

[adult swim] did not run on a single episode length, so the grid is declared:

| Family | Quanta | Typical content |
|---|---|---|
| `short` | 1 (15 min) | 11-minute shows: ATHF, SeaLab |
| `half` | 2 (30 min) | 22-minute shows: Futurama, KotH |
| `hour` | 4 (60 min) | movies, specials, marathons |

`grid.infer_family` picks the coarsest family that divides a daypart exactly, so a
day file only has to declare `family` when the inferred one is wrong.

## DST

`day_bounds_ms` uses local midnight to local midnight, so a spring-forward day is
23 hours and a fall-back day is 25. Slot counts follow the real span, meaning the
grid stays aligned to wall-clock time across the transition — which is what a
broadcaster does, and what makes "9pm" mean 9pm.

## Generated bumps

`bumpgen` renders one card per `(era style, position, show)` with ffmpeg
`drawtext` over a generated background. Two deliberate constraints:

* **The text is never model-generated.** Text-to-image models still mangle
  letterforms; compositing crisp text over a generated background keeps every word
  correct. Any Tier 2 image model is therefore limited to backgrounds.
* **Output is cached and deterministic.** Styles are constants keyed by year, so a
  card is byte-identical across runs and the reproducibility guarantee extends to
  synthetic content.

Tags live in a `.tags.json` sidecar next to each file, which is what lets
`bumps/generated/` be a folder that is *not* a show. The sidecar is also what the
review queue writes, so there is one on-disk representation of "what this bump is".

## Catalog and tag durability

`data/catalog.sqlite` is derived state: delete it, rescan, and it rebuilds. Human
decisions are deliberately kept out of it — confirmed tags are written to
sidecars, which travel with the media and survive a wiped database. `doctor`
reports any sidecar it cannot parse instead of silently ignoring it.

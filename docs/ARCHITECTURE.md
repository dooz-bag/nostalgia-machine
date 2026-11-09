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
| `programming` | Hand-built lineup / whole-collection shuffle, and the grid they imply | pure |
| `continuity` | Template → ordered segment specs, budget math | pure |
| `bumps` | Scoring, cooldown, fallback ladder | pure |
| `ads` | Pod filling: whole-spot tolerance, promo top-up, black-card fallback; multi-spot hint | pure |
| `plan` | Ties the above into one timeline; enforces invariants | pure |
| `emit` | Plan → playout JSON, XMLTV, lineup/channel config | writes files |
| `mediaimport` | Download → library layout: alias match, filename parse, dry-run plan | pure |
| `watermark` | The corner bug: card render, placement layer, burn compositing | writes files |
| `catalog` | Scan, tags, airing history (SQLite) | I/O |
| `probe` | ffprobe with a sidecar fallback | I/O |
| `bumpgen` | Render typographic cards with ffmpeg, with sentences and a music bed | I/O |
| `bumplines` | The line bank, the truth guard, the approval queue, the local-model client | I/O |
| `music` | The bed library, levelling a bed under a card, harvesting beds from your bumps | I/O |
| `conform` | One canvas, frame rate and loudness for every spot and bump | I/O |
| `deploy` | Staged schedules: the switch boundary, and the swap that honours it | writes files |
| `mergebreaks` | Finding runs of conformed spots and joining each into one file | I/O |
| `web.commercials` | The commercial history page: what aired, and switching spots off | I/O |
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

## Break size is not a template property

A slot is a fixed budget and the episode is a fixed cost, so the break is the
difference. A template decides the *shape* of that break -- where the bumps and
pods sit, and how much of it each pod claims -- and the tail pod absorbs the
remainder. Consequently no combination of `share`/`seconds` can change how much
of the day is commercials: only the slot family (or which show occupies a slot)
can.

`doctor` reports two numbers, and the distinction matters:

* **ad minutes/hour** -- what a real plan spends on spots. This is the figure
  with a real-world reference point (US cable ≈14-16 min/hour), and it needs an
  actual plan because pod filling is a subset-sum over ragged durations.
* **break minutes/hour** -- the arithmetic `slot - episode` total that templates
  divide. The difference between the two is bumps.

They are not interchangeable. Quoting only the second reads as "27% of the
channel is commercials" when the spots are 14.5 min/hour and the remainder is
continuity, which on [adult swim] was the point rather than the padding.

The resolution order for a slot's template is daypart `template:` → show `slot:`
→ slot `family`. The daypart is the most specific statement about *this block of
the schedule*, so it wins.

## Templates: `share` versus `seconds`

A break segment is sized one of two ways:

* **`share`** — a fraction of the slot's remaining break budget (recommended).
* **`seconds`** — a fixed booking length (30/60/90).

`seconds` looks more like real traffic, but it cannot adapt: a library's
"22-minute" episodes run a few seconds over, and a fixed pod then either starves the
closing bump or leaves a long tail. A share is resilient to real durations. The two
are mutually exclusive per segment, and `config` rejects a template that sets both.

## Replacing a break is a swap, not a reschedule

`ads.extras_enabled` lets a break air something from `media/extras/` — a music
video, a DVD extra — instead of commercials, at the rate `ads.extras_chance`
sets. The whole feature is one function, `_roll_extras` in `plan.py`, and the
design rests on four decisions:

* **It swaps the filler, not the slot.** The replaced pod keeps its own `share` or
  `seconds`, so the break is the same length and the grid is untouched. Nothing
  about the day moves: this is what separates "a different thing aired" from "the
  schedule changed".
* **It rolls per break.** A 30-minute template has two or three pods and they are
  separate breaks, so the chance is per pod and the seed is
  `(day, slot index, pod index)`. Seeding from the template's *shape* instead would
  mean adding a bump to a slot silently reshuffled which breaks were special — a
  config edit with a scheduling side effect, which is the thing the day-planning
  code goes to some trouble to avoid everywhere else.
* **It refuses rather than cuts.** `_fill_extras_block` picks the longest piece
  that fits and tops up with spots; if every piece is longer than the break it
  returns `None` and the caller airs the commercials it was going to air anyway.
  Trimming a music video to hit a booking length is precisely the artefact the
  extras feature exists to remove.
* **One place combines on/off with how-often.** `AdsConfig.extras_rate` returns
  the chance when the switch is on and `0.0` when it is off, and every caller
  reads *that* rather than `extras_chance`. Two settings that must agree at every
  call site is how a feature ends up half working — a switch honoured in the
  planner and forgotten in a diagnostic, or the reverse. `0.0` also makes "off"
  and "rate happens to be zero" the same value downstream, so there is no second
  branch to keep in step.

Two moments are excluded on purpose. **Bump segments** are continuity, and the
positional rules in `bumps.py` depend on them staying where the template put them.
**Mid-episode pods** (`SegmentSpec.in_mid`) are a break that interrupts a show —
a four-minute video there is a bigger interruption than the show itself.

Extras carry their own cooldown, much longer than the commercial one, because the
pool is small by nature. `_emit_pod` therefore takes both trackers: a spot topping
up a short extras block belongs on the *ad* tracker even though it was emitted as
part of an extras run.

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
score = subject + 3·position + 1·era + 0.5·family − 2·cooldown
```

Subject is worth one of four rungs, and every rung is worth at least one position
grade, so a better subject can never be outbid by a better-fitting position:

| tier | worth | meaning |
|---|---|---|
| `exact` | 5.0 | names the show the slot asked for |
| `family` | 3.5 | another show in the same family |
| `promo` | 2.0 | a promo for an unrelated show |
| `neutral` | 1.0 | says nothing at all; claims nothing, so never wrong |

The ladder, in order:

1. A **real** asset naming the target show — exactly, by family, or as a promo for
   another show — clearing `min_score`. A `promo_share` fraction of picks goes to a
   **neutral** card instead, so the general pool keeps airing rather than being
   crowded out by show-specific cards.
2. If the show's own card is only excluded by a cooldown, the best one anyway
   (`all-cooling`). A recycled correct bump beats a generic one.
3. `strictness: off` — the whole ranked pool.
4. A **generated** card naming the target show, if `max_share` allows.
5. A **neutral** card (`neutral-fallback`) — logged in `plan.bump_fallbacks`
   (emitted item ids) and `plan.bump_fallback_reasons` (why), so it can be counted,
   surfaced in the UI, and fixed by adding assets.

Note the ids match the emitted `PlannedItem.item_id` exactly: an id that cannot be
looked up in `plan.items` is worse than no id at all.

### What is never allowed

A card is rejected outright only when it would **contradict the running order**.
Topical mismatch is not a contradiction; a positional claim is.

| Card | Slot | Verdict |
|---|---|---|
| "Deathklok returns with new episodes" | during Aqua Teen | fine — a promo |
| "Next up: Futurama" | before Aqua Teen | **rejected** — false promise |
| "We now return to Aqua Teen" | before Sealab | **rejected** — wrong show |
| "We now return to Aqua Teen" | after Aqua Teen ended | **rejected** — nothing returns |
| any untagged promo | anywhere | fine — it claims nothing |
| general card | anywhere, including the tail | fine — claims nothing |

Two separate rules apply, and they are checked independently:

1. **The phrase must be true at this moment.** `PERMITTED_MOMENTS` says which
   slot each printed sentence can occupy. `OUT` ("we now return to") only belongs
   where the show actually comes back — the mid-episode break, or the `INTO` that
   resumes it. At `AFTER` the episode has finished and nothing returns, so the
   same card naming the right show is still a lie. That is the bug the archived
   card collection exposed: it had only 3 `OUT` and 3 `AFTER` cards, and
   `ADJACENT_POSITIONS` treated them as interchangeable *for scoring*, so an
   `OUT` card was handed an `AFTER` slot and read "we now return to King of the
   Hill" immediately after King of the Hill had ended.
2. **The subject must match the moment.** A card's own label fixes which show it
   must name, regardless of the slot: an `NEXT`/`AFTER` card must name the show
   it sits next to on that side, while an untagged card is unconstrained.
   Additionally only a card labelled `NEXT` may close a slot, because that
   position *is* the up-next announcement.

`ADJACENT_POSITIONS` stays, but only as a scoring hint ("this card still fits
better than nothing"); correctness is the narrower table. Conflating the two is
what let a false sentence through, so they are deliberately separate.

Two `INTO` cards exist in a break and they are **not** the same moment: the one
before the episode announces it, and the one after a mid-roll pod says it is
continuing. `SegmentSpec.resumes` carries that distinction from `continuity.py`
to the matcher, because "stay tuned for" and "we now return to" point in opposite
directions in time.

The rendered wording follows the same rule: `position_words.AFTER` is
"YOU JUST WATCHED", not "WE NOW RETURN TO".

This is what makes "semi-strict" behave the way you'd hope instead of the way it
sounds. Rejecting every wrong subject instead cost the channel ~48% of its break
slots to generic cards, because a real 2003 archive is *mostly* promos for other
programmes.

`bumps.allow_other_shows: false` restores the old rule of requiring a card that
names the show or nothing.

Position (`INTO`/`OUT`/`NEXT`/`AFTER`) comes from the break template, not the
asset, which is what makes `next` mean *the show that actually follows*. The tail
bump is resolved with the following slot's show in hand.

`min_score` gates position and era, **not** the subject: the subject's own weight
is subtracted before comparison, so a flawless promo (which tops out near 2.0) is
judged on the same scale as a flawless exact match (which tops out near 9.0).

## The era is not the date

The timeline must carry a **real date** — a player and an EPG cannot be asked for
"some evening in 2003" — but the era being recreated is a separate fact, and it is
the one every era decision must use:

| Decision | Uses |
|---|---|
| Slot boundaries, EPG timestamps, rotation ordinal | `plan.day` (the real date) |
| Which commercials are eligible | `RootConfig.era_year` |
| Which bump card style is rendered | `RootConfig.era_year` |
| How a bump is scored against its era | `RootConfig.era_year` |

They are the same number only when the channel is airing on its own launch date.
Conflating them is a quiet failure rather than a loud one: a 2003 channel built
from today's date filters every spot out (nothing matches 2026), widens to the whole
pool, and renders 2020s-looking cards — while still producing a valid timeline. So
`era_year` is a first-class config field, resolved once on `RootConfig`
(`channel.era_year` → `on_air_date.year` → `bumps.generated.era_year`), and
`tests/test_era_year.py` asserts a plan built for 2026 still only airs 2003-era
spots.

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

* **Letters are never model-generated.** Text-to-image models still mangle
  letterforms; compositing crisp text over a generated background keeps every word
  correct. Any Tier 2 image model is therefore limited to backgrounds.
* **Output is cached and deterministic.** Styles are constants keyed by year, so a
  card is byte-identical across runs and the reproducibility guarantee extends to
  synthetic content.

Tags live in a `.tags.json` sidecar next to each file, which is what lets
`bumps/generated/` be a folder that is *not* a show. The sidecar is also what the
review queue writes, so there is one on-disk representation of "what this bump is".

## What a generated card says

A card's sentence comes from `config/bump-lines.yaml`, a plain-text bank grouped by
the moment the card airs (`INTO`, `NEXT`, `AFTER`, `OUT`, `ANY`). A local model may
*propose* lines, but it is never in the playout path: proposals go through
`bumplines.check_line`, wait in a queue on the Lines page, and enter the bank only
when a person approves them. Rendering reads the bank, picks with the seeded RNG
and caches the card, so a card is the same on every run.

The guard exists because the failure that matters is a card that lies about its
moment. Each position has a set of tenses it may not use: a card shown *before*
another show must not speak in the past ("you just watched"), a card shown *after*
an episode must not promise the future, and `OUT` appears both at the start of a
break and on the resume, so it may talk about returning but never about ending
or what is next. A line that names a show other than its own is refused outright.
This is a structural check, not a language model's judgement, so it runs the same
way on seed lines, proposals and at plan time.

Music follows the same rule. Beds come from the user's own files, from their own
text-on-black bumps, or — with `generated.music_from_bumps` — from the audio of
their archive bumps read straight out of the bump files. A bed is chosen
deterministically from its position and era and levelled over the segment that
will be heard. A bed that is near-silent is dropped rather than boosted, because
normalising it makes the hiss the music.

Beds taken from bumps are ranked by subject, because a card that says *Aqua Teen*
should not be bedded with the music of *Futurama*: a card uses a bump of its own
show first, then a bump that names no show, and never a bump of another show. The
renderer also verifies what it cached. A card is keyed by its content, so a card
rendered before beds existed would otherwise stay silent forever; instead the
renderer measures the peak of a cached card, re-renders it when it is silent, and
prunes generated cards that no longer belong to the plan. A card with no bed at
all still gets a silent audio track, so breaks stay stream-copy joinable.

## A generated card must not pass for an archive card

The generator produces cards that go into the same pool as several hundred real
bumps, so an unmarked generated card is a quiet falsification of the archive. The
mark is therefore part of the **picture** rather than a player-side overlay: only
something burned into the frames survives every transcode and shows up in a
screenshot, and a screenshot is the surface you would actually check.

It is drawn top-right, which the real cards never used, at about a third opacity,
inside a thin tag. The three signals work together: position (real cards kept text
left and the network bug bottom-right), shape (no archive card had a bordered
tag), and quietness (low enough to read as grime during playback).

The settings live at the top of `config/bumps-era.yaml` and are merged into each
style, because the mark is a channel-wide policy rather than an era look — while
still allowing one style to override it.

## Breaks are conformed, then merged

The streamer plays each playout item as its own encode. For a break of several
spots that means a new process, a new playlist discontinuity, and, because each
spot is scaled to fit on its own, black bars that change width at every cut.
Measured on the real library, 59% of spot-to-spot transitions changed bar width.

Two steps fix this without changing the plan:

1. **Conform** (`conform.py`) re-encodes every spot and bump once to a canvas,
   frame rate, codec, GOP and loudness that are identical for everything. It is
   keyed by a hash of its recipe, so it is idempotent, and stored beside the
   original (`media/derived/conformed/`) with the catalog's `air_path` pointing at
   the copy. The original path stays the key for tags and sidecars. B-frames are
   disabled in the encode because a stream-copy join of B-frame streams produced
   duplicate timestamps at the seams.
2. **Merge** (`mergebreaks.py`) stream-copies a run of consecutive conformed items
   into one file, only at the serialisation step. The plan, the guide, the airing
   history and the per-spot switches still see every spot. A run is only merged if
   every member is conformed, contiguous, and not an episode, a lavfi card, or a
   trimmed item; anything else splits the run and airs item by item, so the
   feature degrades to the old behaviour instead of failing.

Because spots arrive pre-fitted, `trim_slop` is forced to zero when merging is on
and the whole pool is conformed: a spot is never cut short to land on a pod length.

## The settings page is one form over files grouped by intent

`config_edit` is a schema first and a page second: each field declares its type,
label, help, bounds and whether it belongs under Advanced, and that one declaration
drives the render, the parsing of the submission and the validation. A setting
cannot be shown but rejected on save, or accepted and then ignored.

The page is **one** submission rather than one per file, because a setting's home
file is an implementation detail the reader should not have to remember. Controls
are named `<slug>::<key>`, which is what keeps `ads::era_drift_years` and
`bumps::era_drift_years` apart, and `save_all` writes only the files whose fields
were submitted. A file with no fields in the post is never opened, so a save cannot
reset settings the page did not show — which is what happened when every file's
missing checkboxes were filled in as "false".

Validation runs *after* the write, because a rule like "a daypart references a real
show" spans files and can only be judged on the whole document, so the route
snapshots every editable file and restores it on rejection. That is what makes
"nothing was saved" true rather than approximately true.

## A schedule change is staged, then launched

Rewriting a playout file *is* changing the channel: the streamer re-reads it at
every item boundary and positions itself by wall clock. So a save cannot be allowed
to write the live files directly, or an edit lands on whatever is airing next — a
commercial, or the middle of an episode.

`build(..., deploy=True)` writes the same plans into `playout/pending/` instead, and
records a `Deploy` last, so a half-built staging folder is never mistaken for a
deploy that is ready. The launch is where the two rules live:

1. **The boundary** is the end of the programme on air, never a break: cutting a
   break in half shows the tail of the old commercials and then a different
   schedule. A programme ending within five seconds switches at once (waiting would
   itself be a mid-item seam); with nothing on air, the next whole minute is used,
   because items are whole milliseconds and that is certainly a boundary.
2. **The merge** keeps every live item that had started before the boundary, whole,
   and takes the rest from the staged document. Keeping them whole rather than
   trimming them to the boundary is what makes "finish the programme" true even when
   the switch fires slightly early; a staged item that would start inside a kept one
   is moved to its end, and one that would end before it is dropped, so the result
   has no gap and no overlap.

Every file is written before anything is replaced, so the live folders are
inconsistent for a couple of `os.replace` calls rather than for the length of a
build, and a launch that fails before that point changes nothing. Merged break files
are made by the build before the swap, and they are not pruned during a stage: the
live document still names them.

The unattended scheduler `emit` still writes live. It is the path that keeps the
horizon rolling overnight, and a channel with no future playout is down.

Each CLI live emit records its last attempt, success and error in the catalog's
`state` table. `doctor` reports that history, while the scheduler container health
check asks whether a readable live playout covers the current time. This keeps a
failed refresh visible without marking a still-playing channel down prematurely.

## The corner bug belongs to one side or the other

A watermark can be applied by the scheduler or by the streamer, and doing both
gives you two bugs in the corner. So `plan.watermark_rule` returns a rule **only**
in `overlay` mode: in `burn` mode the file on disk already carries the mark and
the planner must not also flag the item.

`burn` swaps the *path* during scan (`media/derived/watermark/<key>.mkv` replaces
the original, resolved before probing so only the file that will air is
measured). `overlay` sets a `graphics` layer per item instead. Neither requires
anything downstream to know what a watermark is.

Two facts this rests on, both verified against the running image rather than
assumed:

* The playout schema's `graphics[].location` accepts `bottom_right`, and
  `within_source_content` keeps the mark inside the visible picture when bars
  appear. That is why overlay is preferred over a fixed pixel offset.
* Rendering the mark as a card with alpha needs `colorkey`. Neither
  `color=c=black@0.0` nor a `geq` alpha channel survives ffmpeg — both came back
  with alpha 255 when measured, which would have painted an opaque black box over
  every episode.

The card lives under `media/derived/` because the streamer mounts only the media
tree; a copy in `data/` is written but unreadable by the compositor.

## Guessing happens once, in one place

The scanner is deliberately strict about identity: it will not invent a season or
episode, because airing episodes out of order is worse than not airing them. That
means a messy download would otherwise be a silently empty show, so `mediaimport`
is the single place where guessing is permitted — and it is built as a *plan*
first, for three reasons:

* **It is a pure function** of the source tree plus the roster, so a dry run is
  worth reading: the same input always proposes the same targets.
* **It never overwrites.** A colliding target is reported, because the file
  already there is library content a person curated. `--overwrite` is explicit.
* **It copies by default.** The download is the backup until the owner is happy.

The one failure it cannot fix is the one worth guarding: a folder under
`media/shows/` that no roster entry names is never walked at all. `doctor`
therefore reports unrostered folders that contain video, which turns "my show
never plays and everything looks green" into a one-line warning.

## Catalog and tag durability

`data/catalog.sqlite` is derived state: delete it, rescan, and it rebuilds. Human
decisions are deliberately kept out of it — confirmed tags are written to
sidecars, which travel with the media and survive a wiped database. `doctor`
reports any sidecar it cannot parse instead of silently ignoring it.

The one exception is the **disabled** flag on a commercial. It lives in the
database because it is a playback decision rather than a description of the file,
and it is cheap to redo: a rescan clears it, and the Commercials page puts it back
in one click. That trade is deliberate — a sidecar per switched-off spot would put
a file next to the media for a setting that only matters to this channel.

### Airing history

`airings` records scheduled items whose start time has passed; it is not player
telemetry. The Commercials page and `last_aired` (which seeds the cooldowns) read
it, so `build` writes only items at or before the wall clock — a plan covers a whole
broadcast day, and a spot scheduled for 41:00 is tomorrow afternoon, not something
that has run. Each day's rows are replaced wholesale on every build, so a replan
cannot leave stale entries behind.

### HLS playback observations

The Docker Compose stream endpoint is a small HTTP proxy in front of ErsatzTV.
For a successful client request for an HLS segment, it reads that segment's
`EXT-X-PROGRAM-DATE-TIME` from the streamer's live playlist and maps the timestamp
to the full per-item schedule map written by the scheduler. This map is separate
from `airings`: it includes future items and keeps individual commercial intervals
even when the playout document merges a break into one file. Staged maps are
promoted when a schedule launches, retaining the item carried over the switch.

The proxy records one observed start per scheduled item in `playback_events` and
tracks subsequent segment requests separately. This proves a client requested
content from the channel, not that a person watched the full item; HLS clients may
buffer ahead. Direct native streaming bypasses the proxy and does not create these
observations.

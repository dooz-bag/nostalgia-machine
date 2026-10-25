# Options considered

The requirement that drives everything: **a bump naming a show must sit next to
that show, in roughly the right position.** That is a scheduling problem, not a
streaming problem, and it is where every off-the-shelf tool falls short.

## Summary

| Option | Bump adjacency | Determinism | Setup effort | Verdict |
|---|---|---|---|---|
| **A. ErsatzTV legacy alone** | weak | partial | low | feasible, not accurate |
| **B. ErsatzTV Next + this scheduler** | **exact** | **exact** | medium | **chosen** |
| **C. Tunarr alone** | partial (~70-80%) | no | lowest | good pilot, not the target |
| **D. Hand-rolled ffmpeg delivery** | exact | exact | high | unnecessary; B leaves the door open |

## A. ErsatzTV legacy alone

Legacy (.NET, v26.11.x) is a full media library + scheduler + streamer. It has
block schedules, decos, and **filler presets** with `Count` / `Duration` / `Pad`
modes plus pre-roll/mid-roll/post-roll/tail slots.

**Why it was not chosen:** filler is duration- or count-driven. Asking for "the
bump that names the next show" means attaching one preset per *adjacent show pair
per daypart*, which is combinatorial and breaks the moment the daypart order
changes. There is also no guarantee a pod sums to an exact target.

Its `scripted-schedule` API (`add_duration`, `pad_until_exact`, `pre_roll_on`, …)
looked promising, but it drives legacy's own playout builder, so the adjacency
constraint is still out of reach.

*Kept documented because it is a genuinely good single-binary option if the bump
rule ever stops mattering.*

## B. ErsatzTV Next + this scheduler — chosen

ErsatzTV Next (Rust, v0.2.x, actively developed) deliberately scoped itself to
**transcoding and streaming only**. Its README says to bring your own scheduler.
It consumes a **playout document**:

```json
{
  "version": "https://ersatztv.org/playout/version/0.0.7",
  "items": [
    {"id": "1", "start": "2026-04-13T20:00:00.000-05:00",
     "finish": "2026-04-13T20:30:00.000-05:00",
     "source": {"source_type": "local", "path": "/media/show/ep1.mkv"}}
  ]
}
```

That is the seam. It gives us:

* total control over ordering, so adjacency is exact;
* `in_point_ms` / `out_point_ms` for mid-roll episode splitting;
* `lavfi` sources, so a synthetic card can be a real timeline item;
* hardware-accelerated normalization, HLS, M3U and XMLTV that we do not write;
* gap filling and fallback, so a plan mistake degrades to a still image rather
  than a dead channel.

We write ~2,000 lines of well-factored Python and get accuracy the other options
cannot express.

## C. Tunarr (the credible zero-code option)

Tunarr is the modern successor to DizqueTV — the project ErsatzTV itself was
inspired by, and whose original author is Tunarr's #2 contributor. It is good:
Plex/Jellyfin/Emby or local folders as a source, a spoofed HDHomeRun tuner, Time
Slots with per-slot padding and max-lateness, per-slot filler types
(Head/Pre/Post/Tail/Mid/Fallback), mid-roll breaks, and a genuinely clever filler
shuffle (weighted raffle where wait time counts quadratically).

**The closest you can get without writing code:** one bump filler list per show,
assigned as Pre/Post on that show's Time Slot, ads as Mid/Fallback, pad to
` :00/:30`. That is roughly 70-80% of the feel, in an evening.

**Why it is not the target:**

* **Filler selection is content-agnostic.** It knows *when* a clip last played,
  never *which show* it belongs to. `INTO` and `NEXT` semantics are unreachable;
  a "next up: Futurama" list has to be hung off the *preceding* slot's Tail, one
  list per adjacent pair.
* **Pods are not exact.** Flex time fills with best-fit clips, never summing to a
  target.
* **The lineup loops from the channel's start time,** not from wall-clock date, so
  "what aired on 12 March 2003" is not reproducible and a restart can shift you.
* **No inspectable plan** — nothing to diff, version, unit-test, or hand to a
  different streamer.
* **One window per show,** so a late-joining show (KotH) is a manual lineup toggle
  rather than a declared date.
* Plex's Windows desktop client does not work with Tunarr Live TV (issue #718).

Tunarr remains the recommended thing to try *first* if you want to see whether the
missing adjacency actually bothers you in practice. If it doesn't, keep Tunarr.

## D. Hand-rolled ffmpeg delivery

Rolling HLS with our own segmenter. Maximum control — and you own seeking,
discontinuity handling, EPG generation and hardware acceleration. Not needed here,
because we already generate the playout document: swapping Next for a custom
delivery layer later is a delivery change, not a rewrite.

## Supporting choices inside option B

| Need | Chosen | Rejected, and why |
|---|---|---|
| Plex Live TV | Dispatcharr (HDHR emulation) | Next has no HDHomeRun emulation; Threadfin is the older equivalent but less active |
| Jellyfin / VLC | Next's own M3U + XMLTV | no bridge needed |
| Catalog | SQLite (stdlib) | a server DB is overkill for one channel |
| Config | YAML + pydantic | JSON lacks comments; a DSL would be an extra thing to learn |
| Bump rendering | ffmpeg `drawtext` | text-to-image models mangle letterforms |
| Container | Docker Compose | identical on Windows, rPi and VPS |
| Cloud | not for streaming (long-lived transcode + HLS needs a real host); fine for serving the read-only "now playing" page later | — |

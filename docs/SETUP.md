# Setup

## Prerequisites

| Need | Why | Notes |
|---|---|---|
| Python 3.12+ | the scheduler | only stdlib beyond 4 small packages |
| ffmpeg + ffprobe | durations, bump rendering | Docker images install it |
| ~2 GB free | catalog + generated cards | plus your media |
| yt-dlp *or* the `ia` CLI (optional) | bulk-downloading commercials | `scripts/import-breaks.ps1` needs neither — it uses plain HTTP |
| Docker (optional) | the compose deployment | |

On Windows:

```powershell
winget install Gyan.FFmpeg
.\run.ps1 setup
```

> **Do not** put `config/`, `playout/` or `data/` inside a OneDrive-synced folder.
> (Regenerated on every `emit`, so sync clients will thrash on them.)
> ErsatzTV Next writes video segments continuously while a channel plays, and
> OneDrive will corrupt that. `C:\Projects\...` is fine; `Documents` usually is not.

## 1. Lay out the media

Folders are a convention, and the convention is forgiving:

```
media/
  shows/
    aqua-teen-hunger-force/
      Season 01/
        Aqua Teen Hunger Force - S01E01 - Rabbot.mkv
        Aqua Teen Hunger Force - S01E01 - Rabbot.mkv.json      <- optional
    sealab-2021/
      S01E01.mkv
  bumps/
    aqua-teen-hunger-force/        <- folder name IS the subject show
      card-014.mp4
      into-01.mp4                  <- "into" in the name sets the position
    neutral/                       <- show-agnostic cards
    _review/                       <- unknown cards; tagged via the UI
  commercials/
    2003/                          <- year folder sets the era
      ad-0041-30s.mp4
    2005/
  idents/
    station-id.mp4
```

### Importing commercials in bulk

A break airs roughly **1,100 spots a day — six hours** — so the pool is the part
that decides whether the channel feels like TV. With `ads.cooldown_hours: 2`, each
spot may air at most 12×/day, which puts the absolute floor at ~92 unique spots and
comfortable variety at 250–350 (~2 GB).

`scripts/import-breaks.ps1` bulk-imports from the Internet Archive:

```powershell
# see what a query would pull, without downloading
.\scripts\import-breaks.ps1 -DryRun -Limit 5 `
    -Query 'title:"commercial break" AND year:[2002 TO 2004] AND title:(cartoon OR adult OR fox OR comedy)'

# for real
.\scripts\import-breaks.ps1 `
    -Query 'title:"commercial break" AND year:[2001 TO 2008] AND title:(cartoon OR adult)'
```

Recorded breaks are far more valuable than single ads: one item yields several
spots *plus* the network's own promos, which is exactly the continuity this project
exists to reproduce. **Filter by network** in the query — an unfiltered
`title:"commercial break"` returns Australian and Canadian stations (HSV-7, YTV)
that would look badly out of place.

How it splits: a break tape has a cut at every *shot* change, so raw scene
segmentation yields 1–7 s shards. The script instead snaps candidate boundaries to
real spot lengths (15/30/60 s, ±4.5 s), which recovers the actual commercials —
typically 4–8 spots per break. Each is written to `media/commercials/<year>/`, and
that year folder *is* the era tag.

#### Already downloading with something faster?

archive.org throttles **per connection** at roughly 0.4 MB/s, so the script issues
parallel range requests (~1.1 MB/s measured). A download manager such as
JDownloader will still beat it, and that is fine — use whichever you prefer and let
the script do the part a downloader cannot:

```powershell
# split tapes you already downloaded, filing each under the year in its name
.\scripts\import-breaks.ps1 -FromFolder D:\downloads\breaks

# force one era when the filenames carry no year
.\scripts\import-breaks.ps1 -FromFolder D:\downloads\breaks -Year 2003
```

`-FromFolder` takes the year from each **filename or folder path**, so
`October 2003 break.mp4` lands in `2003/`. Files are skipped if their output already
exists, so re-running after adding more is cheap.

#### Two things that will otherwise fool you

**The year comes from the title, not the upload date.** An item called
`Super Cartoon Sunday (Break #2, 1984)` is often uploaded in 2007; taking
`publicdate` would file a 1984 tape into a 2007 folder. Titles are checked first and
upload dates only as a last resort.

**Sports and news breaks need a looser pass.** Snapping boundaries to
15/30/60 s assumes there is a cut roughly every 15–60 s, which holds for an ad
block. A long static sports graphic can run 90 s with no cut at all, so strict
snapping stopped after one spot and discarded the rest. When no cut lines up with a
spot length, the script now falls back to the cut that yields a length inside the
acceptable range. A 157 s Fox Sports break went from *1 spot, 142 s discarded* to
*5 spots, 0 s discarded*.

Accepted episode filename forms: `S01E02`, `s1e2`, `1x02`,
`Season 1 Episode 2`, and an episode number inside a `Season NN/` folder.
Anything else is reported by `nostalgia doctor` rather than guessed at.

### Filing a download into the library

A fresh download is rarely shaped like the library — one flat folder per series,
names varying between `S01E01`, `1x01` and `Episode 1`, extras mixed in. Since the
scanner refuses to guess a season or episode, `nostalgia import` does the guessing
once, up front, where you can read the proposal before anything moves:

```powershell
# always start here: nothing is written
.\run.ps1 import D:\downloads\Futurama
.\run.ps1 import D:\downloads\Futurama --apply         # copy into place
.\run.ps1 import D:\downloads\Futurama --apply --move  # move instead
```

It matches the download folder against each show's id, `title` and `aliases`, so
`Futurama 1080p WEB-DL`, `aqua.teen.hunger.force` and `ATHF` all resolve without
being told. Every file is classified and the count is printed:

| Status | Meaning |
|---|---|
| `ok` | will be filed as `media/shows/<id>/Season NN/<id> - SxxExx.ext` |
| `unknown-show` | folder matches no roster entry — add one to `shows.yaml`, or use `--show <id>` |
| `unparsed` | it is a video but no season/episode can be found; rename it |
| `collision` | the target already exists — **never overwritten**, even with `--apply` |
| `duplicate` | two files claim the same episode |
| `too-small` | under `--min-mb` (default 20), i.e. a sample |

Three deliberate safeguards: it **copies** unless you pass `--move`, it **never
overwrites** a file already in the library unless you pass `--overwrite`, and it
rewrites names to the library's own `SxxExx` form instead of preserving whatever
the release group used. Use `--show <id>` when a folder name is unhelpful and
`--min-mb 0` only for a test library.

Then, as always: `.\run.ps1 scan`, then `.\run.ps1 plan --show` to confirm the
episode actually lands in the schedule.

**Durations** come from ffprobe. If you have no ffmpeg (or want deterministic
fixtures), a `<file>.json` sidecar with `{"duration_ms": 660000}` works:
`nostalgia scan --sidecars` writes them for you.

Probing runs in parallel (ffprobe is one process per file). `NOSTALGIA_SCAN_WORKERS`
overrides the count; the default is CPU count, clamped to 4–16. A 1,482-file
library scans in ~4 s.

### Importing a bumper archive

The Internet Archive **`AdultswimBumps`** collection is 7,385 bumpers — the classic
typographic [adult swim] cards — but it is a flat dump with names like
`2003_HowTo___ATHF_Next.mp4`, so nothing is organised the way the planner reads:

| What | Where the planner looks |
|---|---|
| subject | the **folder** under `media/bumps/` |
| era | any **directory** in the path containing a year |
| position | `_Next` / `_Into` / `_After` in the **filename** |

```powershell
.\scripts\import-bumps.ps1 -Source C:\Users\Dooz\Downloads\AdultswimBumps\AdultswimBumps -DryRun
.\scripts\import-bumps.ps1 -Source C:\Users\Dooz\Downloads\AdultswimBumps\AdultswimBumps
```

It maps filenames onto your roster (`ATHF`, `AQUA_TEEN` → `aqua-teen-hunger-force`,
`KOTH`/`KING_OF_THE_HILL` → `king-of-the-hill`, plus `SEALAB` and `FUTURAMA`),
writes a `.tags.json` sidecar recording the position and any year *prefix*, and
files everything else under `neutral/`.

Two things worth knowing:

* **`-MaxNeutral` caps the generic pool** (default 400 of 7,194). The archive is
  ~28 GB of generic cards and the planner cannot use more than a few hundred;
  selection is deterministic and spread across the alphabet, so eras are covered.
* **Bump cards run 15 s, not 10.** The archive's text bumps are almost all
  15.0–15.4 s, so `continuity.yaml` uses `max_seconds: 16`. A 12 s cap silently
  rejects ~350 of 600 imported cards.

### The corner bug (watermark)

[adult swim] ran a persistent ``[adult swim]`` mark bottom-right of every
programme, and library rips almost never have it — one of the fastest ways to
tell a media server from a broadcast. `config/watermark.yaml` puts it back, three
ways:

```yaml
watermark:
  mode: "overlay"   # none | overlay | burn
```

| Mode | How | Cost |
|---|---|---|
| `none` | nothing | — |
| `overlay` | the streamer composites a card per item during the transcode it already runs | free; needs a build whose playout schema has `graphics` |
| `burn` | ffmpeg writes a marked copy to `media/derived/watermark/`, and that copy airs | one encode per episode + disk |

**The typeface.** The wordmark is Helvetica Neue Condensed Bold. That font ships
with nothing and is not redistributable, so it is resolved through a chain: the
real face if present, otherwise a bold Helvetica-metric face (Arial on Windows,
Liberation Sans in a container) squeezed horizontally by `condense: 0.84` — the
ratio between the condensed and plain cuts. Set `font:` to an absolute path to
use a specific file.

**Which items.** Only episodes. Commercials and bumpers were never bugged, so
they are excluded structurally rather than by a filter. `shows: []` means every
show; list ids to restrict it.

**Overlay vs burn.** `overlay` sets `within_source_content: true`, so the mark
stays inside the visible picture and moves inward when letterbox or pillarbox
bars appear — which a fixed pixel offset cannot do. `burn` instead asks a
question per episode: `skip_when_burned_in` looks at the corner and leaves the
file alone if a mark is already there, so you never get two stacked in the same
corner.

```powershell
.\run.ps1 watermark            # burn mode: dry run, says what it would encode
.\run.ps1 watermark --apply    # actually encode into media/derived/watermark/
```

**Overlay is the mode that can regress silently.** It depends on a schema field
in the streamer, so a future image could drop it and the mark would simply stop
appearing while everything else kept working. `nostalgia doctor` therefore greps
the running streamer binary for the fields the layer needs and reports one of:

```
[ ok] watermark streamer composited per item (overlay mode)
[ERR] watermark overlay mode, but this streamer build has no graphics support;
      the mark will be silently ignored -- use mode: burn
```

`burn` mode has no such dependency — it rewrites the files — which is why it is
the safe choice if you would rather this be impossible.

> The card lives in `media/derived/`, **not** `data/`. The streamer mounts only
> the media tree, so a card written to `data/` is created by the scheduler and
> then unreadable by the process that has to composite it — which showed up as
> `/app/data/watermark.png: No such file or directory` and every episode
> replaced by black. `media/` is mounted read-only for the scheduler, so
> `media/derived` is remounted writable on its own.

### Changing how much of the day is commercials

**Two separate questions**, and the code answers them very differently:

| Question | Where |
|---|---|
| How is a break *divided*? | `config/continuity.yaml` |
| How much break *is there*? | the slot family, and which show sits in which slot |

The second is arithmetic, not a setting: a slot is a fixed length and the episode
is a fixed cost, so

$$\text{break} = \text{slot} - \text{episode}$$

A 22-minute episode in a 30-minute slot leaves 8 minutes of break; in a 60-minute
slot it leaves 38. `share:` and `seconds:` in a template decide where spots and
bumps sit *inside* that break — the tail pod absorbs whatever the template does
not spend, so **no template can change the total**. Measured on the default
config: 347.7 min/day (24%) whatever the shares are.

`nostalgia doctor` prints **two** figures, because they answer different questions
and conflating them makes an authentic channel look wrong:

```
[ ok] breaks   commercials 347 min/day (14.5 min/hour, in the US 14-16 min/hour band);
               bumps a further 36 min/day
[ ok] breaks   break budget 384 min/day (16.0 min/hour, 27% of the day)
               = slot minus episode, so pod shares only redistribute it
```

**Commercial load** is what matters for authenticity: US cable ran roughly
14–16 min/hour of spots. **Break budget** is the arithmetic total that templates
divide; the difference is bumps, which on [adult swim] were signature continuity
rather than overhead. The default config lands at 14.5 min/hour — inside the band
— and `doctor` escalates to `warn` outside 12–17.

To actually change it, pick one of:

```yaml
# config/days/weekday.yaml
- start: "05:00"
  end: "09:00"
  family: hour        # 22-min episode in a 60-min slot => 38 min of break
  shows: [futurama]
```

* **`family`** — the blunt lever. `short` (15 min), `half` (30), `hour` (60).
* **`slot:` per show** in `shows.yaml` — which family a show expects to sit in.
* **Which shows run where** — a block of 11-minute shows in 15-minute slots is
  ~27% break; a 22-minute show in a 60-minute slot is ~63%.
* **`mid:` breaks** add spots without changing the slot; they split the episode
  rather than extending the break.

### Pointing a block at its own break structure

A daypart can name a template, which is how you give one part of the schedule a
different rhythm without touching any other:

```yaml
# config/continuity.yaml
continuity:
  heavy-breaks:
    name: "Heavy breaks"
    segments:
      - kind: ad_pod
        share: 0.10        # short pod, then the show
      - kind: episode
      - kind: ad_pod
        share: 0.80        # most of the break at the end
```

```yaml
# config/days/weekday.yaml
- start: "00:00"
  end: "05:00"
  template: heavy-breaks   # wins over the show's slot family
  family: short
  shows: [aqua-teen-hunger-force]
```

Resolution order is **daypart `template:` → show `slot:` → slot `family`**. A
`template:` naming something that does not exist falls back rather than failing.

### Telling a generated bump from a real one

A generated card sits in the same pool as the archive. If it were
indistinguishable it would quietly rewrite history — you could no longer tell
what actually aired in 2003 from what the renderer invented last week. Every
generated card therefore carries a small tag in the **top-right**, the corner the
real cards never used ([adult swim] kept its own bug bottom-right):

```yaml
# config/bumps-era.yaml
artifact: true
artifact_text: "SG"       # Space Ghost, the [adult swim] mascot
artifact_opacity: 0.32    # reads as grime; unmistakable when you look
artifact_box: true        # thin tag; no archive card ever had one
```

Three properties make it the right kind of tell:

* **It is drawn into the picture**, not added by the player — so it survives any
  transcode and appears in a screenshot, which is exactly where you would check.
* **It is quiet.** A third opacity, small. A viewer reads compression grime.
* **No real card had a bordered tag**, so it is unambiguous once you know.

Declared once at the top level and inherited by every era style; a style may
override any of the keys if one era should read differently.

### The web UI wears a broadcast test card

`nostalgia serve` renders a SMPTE NTSC test pattern as its chrome — the seven 75%
colour bars, the castellation strip, and the PLUGE/black-level band — over a CRT
with scanlines.

The bars use the **75% amplitude** levels from SMPTE ECR 1-1978, not the
fully-saturated primaries people remember from a set pushed too far; a monitor
fed real 100% bars looks wrong, and so would the page.

PAL is available but not the default:

```yaml
# config/channel.yaml
test_card: pal     # EBU bars: white lead, no castellations
```

NTSC is correct for this channel — [adult swim] was a US 525/60 NTSC service, and
**Japan broadcast NTSC-J, which is the same system**. PAL exists for a PAL variant
of the channel, not because it would be more accurate here.

## 2. Configure the channel

Edit `config/channel.yaml`:

```yaml
channel:
  grid:
    anchor: "2001-09-02"      # day 0
    quantum_minutes: 15       # :00/:15/:30/:45
    timezone: Asia/Tokyo
  horizon_days: 3
```

The timezone is declared **twice** and the two must agree: `grid.timezone` decides
what "23:30" means for the schedule, and `TZ` in `.env` sets the container clock
that timestamps the logs. A `.env` left over from the template silently overrides
the compose default, so check both if the schedule and the logs disagree.

JST has no DST, so every day is exactly 24 hours and the grid's DST handling never
fires. `tests/test_web_theme.py` asserts this, since a wrong zone id would resolve
to something with DST and produce 23- and 25-hour days twice a year.

Edit `config/shows.yaml` to match your library. The `id` (the YAML key) must match
the folder name under `media/shows/`.

Edit `config/days/` for the daily blocks. Every daypart must start on a grid
boundary and span a whole number of slots; the validator says exactly which
daypart is wrong if not.

## 3. Scan, plan, emit

```powershell
.\run.ps1 scan
.\run.ps1 plan --date 2003-06-16 --show     # read the timeline first
.\run.ps1 emit
```

`plan --show` prints the whole day: clock, duration, kind and show. This is the
cheapest way to tune — no streaming, no waiting.

Expected output shape:

```
2003-06-16  00:00:00->00:00:00  911 items  96 episodes  24:00:00
  00:00:00    30.0s  ad       commercial
  00:00:30     4.2s  bump     Sealab 2021        (subject=sealab-2021,...)
  00:00:34   660.0s  episode  Sealab 2021
```

## 4. Run the streamer

### Docker

```bash
cp .env.example .env      # MEDIA_DIR, TZ
docker compose up -d --build
```

### Windows, natively

Download the `windows-x64` zip from
[ErsatzTV/next releases](https://github.com/ErsatzTV/next/releases) and extract it
into `.\ersatztv\`. It needs to sit next to its own `ffmpeg.exe`.

```powershell
.\run.ps1 stream
```

### URLs

| Purpose | URL |
|---|---|
| Channel list (M3U) | `http://localhost:8409/channels.m3u` |
| Guide (XMLTV) | `http://localhost:8409/xmltv.xml` |
| Channel 1 HLS | `http://localhost:8409/channel/1.m3u8` |

## 5. Connect a player

**Jellyfin / Emby**

1. Dashboard → Live TV → add tuner, type **M3U**, URL `http://<host>:8409/channels.m3u`.
2. Add guide data provider, type **XMLTV**, URL `http://<host>:8409/xmltv.xml`.

**Plex**

Plex needs an HDHomeRun-style tuner. ErsatzTV Next does not emulate one, so put
Dispatcharr in front:

```bash
docker compose --profile plex up -d
```

In Dispatcharr (`http://localhost:9191`):

* add an **M3U** source → `http://ersatztv:8409/channels.m3u`
* add an **XMLTV** source → `http://ersatztv:8409/xmltv.xml`
* create an HDHomeRun output for those channels

Then in Plex: Settings → Live TV & DVR → set up a tuner at `http://<host>:9191`
and point the guide at Dispatcharr's XMLTV URL. Plex Pass is required.

**VLC**: `Media → Open Network Stream → http://<host>:8409/channels.m3u`.

## Keeping it running

`docker compose` restarts the scheduler on failure and it re-runs `emit` every
`PLAN_INTERVAL_SECONDS` (default 6 hours) to keep the horizon rolling. ErsatzTV
Next picks up new playout files without a restart, so regeneration never interrupts
playback.

For a native install, a scheduled task running `emit` daily is enough.

## Tuning

| Symptom | Fix |
|---|---|
| Falls back to neutral bumps often | add bumps for that show, or `nostalgia bumps` to generate them |
| Same commercials too often | add spots, or lower `ads.cooldown_hours` |
| Breaks feel short | raise `pod_targets` in `ads.yaml` or add `ad_pod` segments in `continuity.yaml` |
| Slot ends early / late | the planner would have refused; check `plan.warnings` |
| Want a different era | `config/bumps-era.yaml`, and `bumps.generated.era_year` |
| High CPU | set `normalization.video.accel` in `lineup/channels/1/channel.json` to `qsv`/`nvenc`/`vaapi`; lower `bitrate_kbps` |

## Troubleshooting

`nostalgia doctor` checks config validity, ffmpeg presence, per-show episode
counts, bump/ad availability, untagged bumps, and the rotation cycle length. Run it
first; it names the file and field that is wrong.

Common causes:

* **"no episodes are catalogued"** — folder names under `media/shows/` must match
  the show ids in `shows.yaml`.
* **A folder with videos is reported as "never scanned"** — it has no entry in
  `shows.yaml`, so the scanner skips it entirely. This is the failure worth catching:
  the show is fully downloaded and simply never airs. Add the entry (or ask
  `nostalgia import` for a snippet) and rescan.
* **"is not a whole number of X slots"** — a daypart spans a duration that does not
  divide by its family's slot length. Change `family` or the boundaries.
* **"dayparts must ascend"** — reorder the dayparts in the day file.
* **Nothing plays in a browser player** — many need the HLS URL rather than the
  M3U; try `http://<host>:8409/channel/1.m3u8`.
* **`channel/1.m3u8` returns 503** — read `docker compose logs ersatztv`. Two
  causes account for nearly all of them, and neither shows up as a Python error:

  | Log line | Cause |
  |---|---|
  | `unsupported schema version …expected …/0.1.2` | `CHANNEL_VERSION` in `emit.py` is newer than the image supports. |
  | `unknown variant …, expected burn or convert` | an enum value in `channel.json` is not in the schema. |
  | `failed to create the output folder /app/hls/1: Permission denied` | `hls/` is not owned by uid 1000; the `hls-init` service does this — make sure it ran (`docker compose ps -a`). |

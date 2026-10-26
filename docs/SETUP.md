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

## 2. Configure the channel

Edit `config/channel.yaml`:

```yaml
channel:
  grid:
    anchor: "2001-09-02"      # day 0
    quantum_minutes: 15       # :00/:15/:30/:45
    timezone: America/New_York
  horizon_days: 3
```

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

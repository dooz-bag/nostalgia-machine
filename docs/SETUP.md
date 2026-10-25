# Setup

## Prerequisites

| Need | Why | Notes |
|---|---|---|
| Python 3.12+ | the scheduler | only stdlib beyond 4 small packages |
| ffmpeg + ffprobe | durations, bump rendering | Docker images install it |
| ~2 GB free | catalog + generated cards | plus your media |
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

Accepted episode filename forms: `S01E02`, `s1e2`, `1x02`,
`Season 1 Episode 2`, and an episode number inside a `Season NN/` folder.
Anything else is reported by `nostalgia doctor` rather than guessed at.

**Durations** come from ffprobe. If you have no ffmpeg (or want deterministic
fixtures), a `<file>.json` sidecar with `{"duration_ms": 660000}` works:
`nostalgia scan --sidecars` writes them for you.

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

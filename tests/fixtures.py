"""Synthetic media library for tests.

No real content is ever committed. Fixtures are zero-byte ``.mkv`` stubs with
``.json`` sidecars carrying a duration, which is exactly what
:func:`nostalgia.probe.sidecar_duration_ms` reads. That keeps the whole scheduler
testable without ffmpeg installed.

Durations are chosen to look like the real thing: 11-minute shorts in 15-minute
slots and 22-minute halves in 30-minute slots, so the planner's budget maths is
exercised the way it will be in production.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

SHORT_MS = 11 * 60 * 1000
HALF_MS = 22 * 60 * 1000
BUMP_MS = 4_200
AD_30_MS = 30_000
AD_15_MS = 15_000

SHOWS = {
    "aqua-teen-hunger-force": ("short", SHORT_MS, 3, 4),
    "sealab-2021": ("short", SHORT_MS, 2, 3),
    "futurama": ("half", HALF_MS, 2, 3),
    "king-of-the-hill": ("half", HALF_MS, 1, 2),
}

BUMPS = [
    # (relative path, duration_ms)
    ("aqua-teen-hunger-force/into-01.mkv", BUMP_MS),
    ("aqua-teen-hunger-force/after-01.mkv", BUMP_MS),
    ("aqua-teen-hunger-force/next-01.mkv", BUMP_MS),
    ("sealab-2021/into-01.mkv", BUMP_MS),
    ("sealab-2021/after-01.mkv", BUMP_MS),
    ("futurama/into-01.mkv", BUMP_MS),
    ("futurama/after-01.mkv", BUMP_MS),
    ("futurama/next-01.mkv", BUMP_MS),
    ("king-of-the-hill/into-01.mkv", BUMP_MS),
    ("king-of-the-hill/after-01.mkv", BUMP_MS),
    ("neutral/card-01.mkv", BUMP_MS),
    ("neutral/card-02.mkv", BUMP_MS),
    ("neutral/card-03.mkv", BUMP_MS),
    ("neutral/card-04.mkv", BUMP_MS),
]

ADS = [
    (f"2003/ad-{index:02d}-30s.mkv", AD_30_MS) for index in range(1, 7)
] + [(f"2003/ad-{index:02d}-15s.mkv", AD_15_MS) for index in range(1, 7)]

EXTRAS = [
    # Long-form filler, deliberately longer than a single pod so the planner has
    # to choose a piece that fits the break rather than cut one to size.
    ("music-videos/video-one.mkv", 3 * 60 * 1000),
    ("music-videos/video-two.mkv", 4 * 60 * 1000),
    ("dvd-extras/interview.mkv", 90 * 1000),
]


@dataclass(frozen=True, slots=True)
class FixtureLibrary:
    root: Path

    @property
    def media(self) -> Path:
        return self.root / "media"

    @property
    def config(self) -> Path:
        return self.root / "config"


def _stub(path: Path, duration_ms: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    sidecar = path.with_suffix(path.suffix + ".json")
    sidecar.write_text(
        json.dumps(
            {
                "duration_ms": duration_ms,
                "format_name": "matroska",
                "video": {"codec": "h264", "width": 640, "height": 480},
                "audio": {"codec": "aac", "channels": 2},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def build_library(
    root: Path,
    *,
    with_bumps: bool = True,
    with_ads: bool = True,
    with_extras: bool = False,
) -> FixtureLibrary:
    """Create a fixture library plus a matching ``config/`` directory.

    *with_extras* is off by default: an extras pool changes what airs, so a test
    that is not about extras should not have one.
    """
    library = FixtureLibrary(root)
    media = library.media

    for show_id, (_family, duration, seasons, episodes) in SHOWS.items():
        for season in range(1, seasons + 1):
            for episode in range(1, episodes + 1):
                path = (
                    media
                    / "shows"
                    / show_id
                    / f"Season {season:02d}"
                    / f"{show_id} - S{season:02d}E{episode:02d}.mkv"
                )
                _stub(path, duration)

    if with_bumps:
        for relative, duration in BUMPS:
            _stub(media / "bumps" / relative, duration)

    if with_ads:
        for relative, duration in ADS:
            _stub(media / "commercials" / relative, duration)

    if with_extras:
        for relative, duration in EXTRAS:
            _stub(media / "extras" / relative, duration)

    _stub(media / "idents" / "station-id.mkv", 5_000)
    _write_config(library)
    return library


def _write_config(library: FixtureLibrary) -> None:
    config = library.config
    (config / "days").mkdir(parents=True, exist_ok=True)

    (config / "channel.yaml").write_text(
        """
channel:
  name: "Test Channel"
  number: "1"
  tvg_id: "test.1"
  grid:
    anchor: "2001-09-02"
    quantum_minutes: 15
    timezone: "America/New_York"
  horizon_days: 1
media_root: "media"
""".lstrip(),
        encoding="utf-8",
    )

    (config / "shows.yaml").write_text(
        """
shows:
  aqua-teen-hunger-force:
    folder: shows/aqua-teen-hunger-force
    family: comedy
    slot: short
    weight: 1.4
  sealab-2021:
    folder: shows/sealab-2021
    family: comedy
    slot: short
    weight: 1.0
  futurama:
    folder: shows/futurama
    family: sitcom
    slot: half
    weight: 1.2
  king-of-the-hill:
    folder: shows/king-of-the-hill
    family: sitcom
    slot: half
    eligibility: always
    weight: 0.9
""".lstrip(),
        encoding="utf-8",
    )

    (config / "days" / "everyday.yaml").write_text(
        """
days:
  - name: everyday
    days: [mon, tue, wed, thu, fri, sat, sun]
    dayparts:
      - start: "00:00"
        end: "12:00"
        title: "Overnight"
        family: short
        shows: [aqua-teen-hunger-force, sealab-2021]
      - start: "12:00"
        end: "00:00"
        title: "Prime Time"
        family: half
        shows: [futurama, king-of-the-hill]
""".lstrip(),
        encoding="utf-8",
    )

    (config / "continuity.yaml").write_text(
        """
continuity:
  default-short:
    name: "Short"
    segments:
      - kind: ad_pod
        seconds: 30
      - kind: bump
        subject: this
        position: INTO
        max_seconds: 12
      - kind: episode
      - kind: bump
        subject: this
        position: AFTER
        max_seconds: 12
      - kind: ad_pod
        seconds: 60
      - kind: bump
        subject: next
        position: NEXT
        max_seconds: 12
  default-half:
    name: "Half"
    segments:
      - kind: ad_pod
        seconds: 60
      - kind: bump
        subject: this
        position: INTO
        max_seconds: 12
      - kind: episode
      - kind: bump
        subject: this
        position: AFTER
        max_seconds: 12
      - kind: ad_pod
        seconds: 60
      - kind: bump
        subject: next
        position: NEXT
        max_seconds: 12
    mid:
      - after_percent: 50
        segments:
          - kind: bump
            subject: this
            position: OUT
            max_seconds: 12
          - kind: ad_pod
            seconds: 60
          - kind: bump
            subject: this
            position: INTO
            max_seconds: 12
  default-hour:
    name: "Hour"
    segments:
      - kind: ad_pod
        seconds: 60
      - kind: episode
""".lstrip(),
        encoding="utf-8",
    )


__all__ = [
    "AD_15_MS",
    "AD_30_MS",
    "ADS",
    "BUMP_MS",
    "BUMPS",
    "EXTRAS",
    "FixtureLibrary",
    "HALF_MS",
    "SHORT_MS",
    "SHOWS",
    "build_library",
]

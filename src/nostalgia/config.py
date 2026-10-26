"""Typed configuration.

Every knob the channel has lives here, loaded from YAML. Validation errors are
collected and reported with a file/field path so a typo in ``shows.yaml`` never
surfaces as a stack trace halfway through a plan build.
"""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .durations import parse_duration_ms
from .timeutil import parse_hhmm, tz

SlotFamily = Literal["short", "half", "hour", "block"]
BumpSubject = str  # "this" | "next" | "prev" | "neutral" | "specific:<show-id>"
BumpPosition = Literal["INTO", "OUT", "NEXT", "AFTER", "ANY"]
Strictness = Literal["strict", "semi", "off"]
Eligibility = Literal["always", "window"]
Order = Literal["episode", "chronological", "shuffle"]

FAMILY_QUANTA: dict[str, int] = {"short": 1, "half": 2, "hour": 4}
"""Quanta each slot family consumes.

``block`` is intentionally absent: blocks are not grid-aligned and are sized by
an explicit duration instead.
"""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GridConfig(_Model):
    anchor: date = date(2001, 9, 2)
    quantum_minutes: int = Field(default=15, gt=0, le=60)
    timezone: str = "America/New_York"

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        try:
            tz(value)
        except Exception as exc:  # noqa: BLE001 - surfaced as a config error
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value


class BumpGeneratedConfig(_Model):
    enabled: bool = True
    """Render typographic bumps for slots the real pool cannot fill."""

    max_share: float = Field(default=0.25, ge=0.0, le=1.0)
    """Cap on the fraction of airing bump slots that may be generated."""

    cache_dir: str = "bumps/generated"
    style_pack: str | None = None
    """Force one era style instead of picking by air date."""

    era_year: int = 2003
    """Default look when the channel has no ``on_air_date``.

    A nostalgia channel should not default to "this year": that would pick the
    newest style pack and render a 2026-looking card for a 2003 season.
    """


class BumpConfig(_Model):
    strictness: Strictness = "semi"
    min_score: float = 6.0
    cooldown_hours: int = Field(default=6, ge=0)
    trim_slop_ms: int = Field(default=0)
    """Bumps are never trimmed by default; over-long clips are simply skipped."""
    generated: BumpGeneratedConfig = Field(default_factory=BumpGeneratedConfig)


class AdPodTarget(_Model):
    seconds: int = Field(gt=0)
    count: int = Field(gt=0)


class AdsConfig(_Model):
    cooldown_hours: int = Field(default=2, ge=0)
    era_drift_years: int = Field(default=1, ge=0)
    trim_slop_ms: int = Field(default=1500, ge=0)
    """Maximum trim applied to an ad when the pool cannot make an exact sum."""
    pod_tolerance_ms: int = Field(default=250, ge=0)
    """How far a pod may land from its target while still using whole spots.

    Real commercials are 30.02 s or 14.98 s, and a station plays them as booked.
    The *slot* is what must be exact; the planner's tail absorbs the remainder.
    Without this, every pod would shave ~20 ms off a spot for no reason.
    """
    pod_targets: list[AdPodTarget] = Field(
        default_factory=lambda: [
            AdPodTarget(seconds=30, count=1),
            AdPodTarget(seconds=60, count=1),
            AdPodTarget(seconds=90, count=1),
        ]
    )


class WatermarkConfig(_Model):
    """The corner bug.

    ``mode`` decides who does the work::

        none    no mark anywhere
        overlay ask the streamer to composite it per item (cheapest, needs a
                build whose playout schema has ``graphics``)
        burn    composite a marked copy into the media tree with ffmpeg, then
                air that copy (works everywhere, costs disk and one encode)

    Deliberately *not* called ``off``: unquoted YAML reads ``off`` as boolean
    false, which would fail validation with a message about ``mode`` that gives
    no hint why.
    """

    mode: Literal["none", "overlay", "burn"] = "none"

    @field_validator("mode", mode="before")
    @classmethod
    def _forgive_yaml_booleans(cls, value: Any) -> Any:
        # `mode: off` / `mode: no` / `mode: false` all arrive as Python False.
        if value is False or value is None:
            return "none"
        if value is True:
            return "burn"
        return value
    text: str = "[adult swim]"
    font: str = "Helvetica Neue Condensed Bold"
    """Requested face. Resolved through a fallback chain; see FONT_CHAIN."""
    condense: float = Field(default=0.84, gt=0.5, le=1.0)
    """Horizontal squeeze applied when the resolved face is not condensed."""
    font_size: float = Field(default=0.026, gt=0, le=0.2)
    """As a fraction of frame height. A corner bug is small."""
    colour: str = "white"
    opacity: float = Field(default=0.88, gt=0, le=1.0)
    margin_x_percent: float = Field(default=2.4, ge=0, le=40)
    margin_y_percent: float = Field(default=4.2, ge=0, le=40)
    width_percent: float = Field(default=11.0, gt=0, le=60)
    """Overlay mode only: the mark's width as a percent of content width."""
    uppercase: bool = False

    shows: list[str] = Field(default_factory=list)
    """Empty means every show."""

    skip_when_burned_in: bool = True
    """Leave an episode alone when it already carries a corner mark."""

    @property
    def enabled(self) -> bool:
        return self.mode != "none"


OrderMode = Literal["random", "by_season", "sequential"]
"""How a show's episodes are picked when the channel is programmed by hand.

``random``      any episode, uniformly.
``by_season``   pick a season, then an episode inside it -- so a 10-season show
                does not drown in its largest season.
``sequential``  in order, wrapping. The "just play it in order" option.
"""

ProgrammingMode = Literal["dayparts", "lineup", "shuffle"]
"""What decides the schedule.

``dayparts``  the hand-written day files (the original behaviour, default).
``lineup``    an explicit list of shows -- or individual episodes -- cycled
              deterministically.
``shuffle``   a continuous stream drawn from the whole collection.
"""


class ProgrammingEntry(_Model):
    """One show in a hand-built lineup, with how its episodes are chosen."""

    show_id: str
    order: OrderMode = "random"
    weight: float = Field(default=1.0, gt=0)

    @property
    def is_show(self) -> bool:
        return not self.show_id.startswith("episode:")


class ProgrammingConfig(_Model):
    """The channel's schedule, when it is not the day files.

    Kept as *data in a file* rather than hidden runtime state, so it is diffable,
    reviewable, and survives a wiped catalog -- the same reason bump tags live in
    sidecars. The web page is a form over this file, not a separate database.
    """

    mode: ProgrammingMode = "dayparts"

    lineup: list[ProgrammingEntry] = Field(default_factory=list)
    """For ``lineup``: the shows (and pinned episodes) to cycle."""

    pinned: list[str] = Field(default_factory=list)
    """Episode keys played verbatim, cycling after the lineup entries.

    Kept separate from ``lineup`` because a pinned episode is an exact statement
    ("this one"), while a lineup entry is a category ("anything from SeaLab").
    """

    shuffle_shows: list[str] = Field(default_factory=list)
    """For ``shuffle``: which shows take part. Empty means the whole roster."""

    shuffle_order: OrderMode = "random"
    """How ``shuffle`` picks within each show."""

    @field_validator("lineup", "pinned", "shuffle_shows", mode="before")
    @classmethod
    def _empty_for_none(cls, value: Any) -> Any:
        # An entirely commented-out YAML block parses as null, which is how a
        # template ships. Treating that as "no entries" is friendlier than an
        # error naming a field the owner has not filled in yet.
        return [] if value is None else value

    def active(self) -> bool:
        return self.mode != "dayparts"

    @model_validator(mode="after")
    def _check(self) -> "ProgrammingConfig":
        if self.mode == "lineup" and not self.lineup and not self.pinned:
            raise ValueError(
                "mode 'lineup' needs at least one show in `lineup` or `pinned`"
            )
        for entry in self.lineup:
            if entry.order not in ("random", "by_season", "sequential"):
                raise ValueError(f"unknown order {entry.order!r} for {entry.show_id}")
        return self


class ShowConfig(_Model):
    title: str
    folder: str
    family: str = "sitcom"
    slot: SlotFamily = "half"
    enable: bool = True

    airing_from: date | None = None
    airing_to: date | None = None
    eligibility: Eligibility = "window"

    order: Order = "episode"
    weight: float = Field(default=1.0, gt=0)
    cooldown_slots: int = Field(default=0, ge=0)
    """Minimum number of intervening slots before the show can recur."""

    aliases: list[str] = Field(default_factory=list)
    """Extra title forms used when matching bumps and folder names."""

    @model_validator(mode="after")
    def _check_window(self) -> "ShowConfig":
        if self.airing_from and self.airing_to and self.airing_to < self.airing_from:
            raise ValueError(f"{self.title}: airing_to is before airing_from")
        return self


class ContinuitySegment(_Model):
    kind: Literal["episode", "bump", "ad_pod", "ident"]

    # bump
    subject: BumpSubject = "this"
    position: BumpPosition = "ANY"
    max_seconds: float | None = None

    # ad_pod / ident
    seconds: int | None = None
    pod: str | None = None
    """Named pod target from ``ads.yaml``; ``seconds`` overrides it."""

    share: float | None = None
    """Fraction of the slot's remaining break budget (0-1).

    A fixed ``seconds`` cannot adapt to an episode that runs a minute long or
    short, which leaves the timeline with either a long tail of filler or no room
    for the closing bump. A share is computed from the actual budget, so a
    template stays balanced across the whole library.
    """

    guide: bool = False
    """True to also emit an XMLTV programme entry for this segment."""

    @model_validator(mode="after")
    def _check(self) -> "ContinuitySegment":
        if self.kind == "episode" and (self.seconds is not None or self.share is not None):
            raise ValueError("episode segments are sized by the episode, not by seconds")
        if self.kind in ("ad_pod", "ident") and not (self.seconds or self.pod or self.share):
            raise ValueError(f"{self.kind} segment needs 'seconds', 'pod' or 'share'")
        if self.share is not None and not 0 < self.share <= 1:
            raise ValueError("share must be between 0 and 1 (exclusive of 0)")
        if self.share is not None and (self.seconds or self.pod):
            raise ValueError("give a pod either 'share' or 'seconds', not both")
        return self


class BreakSlot(_Model):
    at: Literal["mid"] = "mid"
    after_percent: float = Field(default=50.0, gt=0, lt=100)
    segments: list[ContinuitySegment] = Field(default_factory=list)


class ContinuityTemplate(_Model):
    name: str
    segments: list[ContinuitySegment]
    mid: list[BreakSlot] = Field(default_factory=list)


class Daypart(_Model):
    """A contiguous run of the day filled by one or more shows."""

    start: time
    end: time | None = None
    """Exclusive end. ``None`` means "until the next daypart starts"."""

    shows: list[str] = Field(default_factory=list)
    family: SlotFamily | None = None
    template: str | None = None
    slots: int | None = Field(default=None, gt=0)
    """Fixed slot count; inferred from start/end when omitted."""

    title: str = ""

    @field_validator("start", "end", mode="before")
    @classmethod
    def _parse_time(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, time):
            return value
        return parse_hhmm(str(value))

    @model_validator(mode="after")
    def _check(self) -> "Daypart":
        if not self.shows:
            raise ValueError(f"daypart starting {self.start}: no shows listed")
        return self


class DayConfig(_Model):
    name: str = ""
    days: list[str] = Field(default_factory=lambda: ["mon"])
    """Weekday names this day file applies to (``mon``..``sun``)."""

    dayparts: list[Daypart]

    @field_validator("days", mode="before")
    @classmethod
    def _split_days(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip() for part in value.replace(",", " ").split() if part.strip()]
        return value

    @field_validator("days")
    @classmethod
    def _check_days(cls, value: list[str]) -> list[str]:
        valid = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        normalised = [day.lower()[:3] for day in value]
        unknown = [day for day in normalised if day not in valid]
        if unknown:
            raise ValueError(f"unknown weekday(s): {', '.join(unknown)}")
        return normalised


class ChannelConfig(_Model):
    name: str = "Adult Swim"
    number: str = "1"
    tvg_id: str = "adultswim.1"
    group: str = "Nostalgia"
    logo: str | None = None
    grid: GridConfig = Field(default_factory=GridConfig)
    on_air_date: date | None = None
    """Wall-clock date that maps to the channel's "start"; defaults to today."""

    era_year: int | None = Field(default=None, ge=1900, le=2999)
    """The year the channel is *pretending* to be in.

    The timeline has to carry a real date so the stream and the EPG can be
    consumed today, but every era decision -- which spots, which bump look, which
    card style -- must use the year being recreated. Left unset it falls back to
    ``on_air_date``, then to ``bumps.generated.era_year``.
    """

    horizon_days: int = Field(default=3, ge=1, le=14)
    ffmpeg_path: str | None = None

    test_card: Literal["ntsc", "pal"] = "ntsc"
    """Which broadcast test pattern the web UI uses as its chrome.

    NTSC is the default and the correct one: [adult swim] was a US 525/60 NTSC
    channel, and Japan broadcast NTSC-J, which is the same system. PAL exists for
    a PAL variant of the channel, not because it would be more accurate here.
    """


class Resolution(_Model):
    width: int = 1280
    height: int = 720
    bitrate_kbps: int = 2500
    audio_bitrate_kbps: int = 160


class RootConfig(_Model):
    channel: ChannelConfig
    shows: dict[str, ShowConfig]
    continuity: dict[str, ContinuityTemplate]
    days: list[DayConfig]
    ads: AdsConfig = Field(default_factory=AdsConfig)
    bumps: BumpConfig = Field(default_factory=BumpConfig)
    watermark: WatermarkConfig = Field(default_factory=WatermarkConfig)
    programming: ProgrammingConfig = Field(default_factory=ProgrammingConfig)
    encoding: Resolution = Field(default_factory=Resolution)
    media_root: str = "media"

    @property
    def era_year(self) -> int:
        """The year the channel is pretending to be in.

        Never "this year": a nostalgia channel whose era follows the wall clock
        airs the wrong decade's spots and renders the wrong decade's cards.
        """
        if self.channel.era_year is not None:
            return self.channel.era_year
        if self.channel.on_air_date is not None:
            return self.channel.on_air_date.year
        return self.bumps.generated.era_year

    @model_validator(mode="after")
    def _cross_reference(self) -> "RootConfig":
        show_ids = set(self.shows)
        for show_id, show in self.shows.items():
            if show_id != show_id.lower():
                raise ValueError(f"show id {show_id!r} must be lowercase")
        for day in self.days:
            for part in day.dayparts:
                for show_id in part.shows:
                    if show_id not in show_ids:
                        raise ValueError(
                            f"daypart {part.start} references unknown show {show_id!r}"
                        )
                if part.template and part.template not in self.continuity:
                    raise ValueError(
                        f"daypart {part.start} references unknown template {part.template!r}"
                    )
                if part.family and part.family not in self.continuity:
                    pass
        default_templates = {f"default-{family}" for family in ("short", "half", "hour")}
        for name in default_templates:
            if name not in self.continuity:
                raise ValueError(f"continuity template {name!r} is required")
        return self


class ConfigError(RuntimeError):
    """Raised with a human-readable, multi-file validation report."""


def _read_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"missing config file: {path}") from exc
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: invalid YAML\n{exc}") from exc


def _normalise_shows(raw: Any) -> dict[str, Any]:
    """Accept both the mapping form and a list of shows (id from ``id``/``folder``)."""
    if isinstance(raw, dict):
        out: dict[str, Any] = {}
        for key, value in raw.items():
            entry = dict(value or {})
            entry.setdefault("title", key.replace("-", " ").title())
            out[str(key)] = entry
        return out
    if isinstance(raw, list):
        out = {}
        for value in raw:
            entry = dict(value)
            show_id = entry.pop("id", None) or Path(str(entry.get("folder", ""))).name
            entry.setdefault("title", str(show_id).replace("-", " ").title())
            out[str(show_id)] = entry
        return out
    raise ConfigError("shows must be a mapping of id -> show, or a list with 'id'")


def load_config(config_dir: Path) -> RootConfig:
    """Load and cross-validate the whole config directory."""
    config_dir = Path(config_dir)
    errors: list[str] = []

    def load(name: str, *, required: bool = True) -> Any:
        path = config_dir / name
        if not path.exists():
            if required:
                errors.append(f"missing required file: {name}")
            return {}
        try:
            return _read_yaml(path)
        except ConfigError as exc:
            errors.append(str(exc))
            return {}

    raw_channel = load("channel.yaml")
    raw_shows = load("shows.yaml")
    raw_continuity = load("continuity.yaml")
    raw_ads = load("ads.yaml", required=False)
    raw_bumps = load("bumps.yaml", required=False)

    days: list[dict[str, Any]] = []
    for path in sorted((config_dir / "days").glob("*.y*ml")):
        data = load(f"days/{path.name}")
        if not data:
            continue
        entries = data.get("days", data)
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries:
            item = dict(entry)
            item.setdefault("name", path.stem)
            days.append(item)
    if not days:
        errors.append("no day files found under config/days/*.yaml")

    if errors:
        raise ConfigError("Configuration is invalid:\n  - " + "\n  - ".join(errors))

    payload: dict[str, Any] = {
        "channel": raw_channel.get("channel", raw_channel),
        "shows": _normalise_shows(raw_shows.get("shows", raw_shows)),
        "continuity": raw_continuity.get("continuity", raw_continuity),
        "days": days,
        "media_root": raw_channel.get("media_root") or "media",
    }
    if raw_ads:
        payload["ads"] = raw_ads.get("ads", raw_ads)
    if raw_bumps:
        payload["bumps"] = raw_bumps.get("bumps", raw_bumps)
    raw_watermark = load("watermark.yaml", required=False)
    if raw_watermark:
        payload["watermark"] = raw_watermark.get("watermark", raw_watermark)
    raw_programming = load("programming.yaml", required=False)
    if raw_programming:
        payload["programming"] = raw_programming.get("programming", raw_programming)

    try:
        return RootConfig.model_validate(payload)
    except ValidationError as exc:
        raise ConfigError("Configuration is invalid:\n" + _format_validation(exc)) from exc


def _format_validation(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def dump_config(config: RootConfig) -> str:
    """Serialise a resolved config (used by ``nostalgia doctor --dump-config``)."""
    return yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)


def parse_optional_date(value: Any) -> date | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value))


__all__ = [
    "AdPodTarget",
    "AdsConfig",
    "BreakSlot",
    "BumpConfig",
    "BumpGeneratedConfig",
    "ChannelConfig",
    "ConfigError",
    "ContinuitySegment",
    "ContinuityTemplate",
    "DayConfig",
    "Daypart",
    "FAMILY_QUANTA",
    "GridConfig",
    "Resolution",
    "OrderMode",
    "ProgrammingConfig",
    "ProgrammingEntry",
    "ProgrammingMode",
    "RootConfig",
    "ShowConfig",
    "WatermarkConfig",
    "dump_config",
    "load_config",
    "parse_duration_ms",
    "parse_optional_date",
]

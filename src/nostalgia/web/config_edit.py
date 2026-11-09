"""Reading and writing every settings file from the web page.

The page is a *form over the files*, never a second source of truth: it loads a
YAML file, changes only the keys the form owns, and writes it back. Hand-written
comments, key order and quote style survive, because these files are meant to be
edited by hand too and a UI that reformats them would make the two ways of
editing fight each other. That is what ``ruamel.yaml`` is here for; PyYAML, which
the rest of the project uses for *reading*, throws comments away.

Two rules keep this honest:

* **Schema, not guesswork.** Every field declares its type, label, help text and
  bounds once, and the same declaration drives the rendered form, the parsing of
  the submission, and the validation. A field can therefore never be shown in the
  UI but rejected on save, or accepted on save but ignored by the planner.
* **The whole config is re-validated before anything is written.** A saved file
  that cannot be loaded would break the channel at the next plan, and the user
  would have no idea which of their edits caused it, so a bad submission is
  rejected with the file left untouched.
"""

from __future__ import annotations

import io
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


class ConfigEditError(ValueError):
    """A submission the config schema will not accept."""


# -- field types ------------------------------------------------------------

TEXT = "text"
INT = "int"
FLOAT = "float"
BOOL = "bool"
CHOICE = "choice"
DAY = "day"
LIST = "list"
"""A list of strings, edited as one comma/space separated line."""


@dataclass(frozen=True, slots=True)
class Field:
    """One editable setting, and everything the page needs to draw it."""

    key: str
    label: str
    kind: str = TEXT
    help: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    """``(value, label)`` pairs for :data:`CHOICE`."""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    placeholder: str = ""
    default: Any = None
    advanced: bool = False
    """Shown with the main settings unless this is set.

    A section can be advanced as a whole; this is for the one field in an
    otherwise-main section that is a number you set once and forget.
    """

    def coerce(self, raw: str | None) -> Any:
        """Turn one submitted string into the value to write to YAML.

        Every branch raises :class:`ConfigEditError` rather than letting a bad
        value reach the file: a float where an int belongs validates fine to
        Pydantic and then surprises the planner, so the type is enforced here as
        well as in the model.
        """
        text = (raw or "").strip()

        if self.kind == BOOL:
            return text.lower() in ("1", "true", "on", "yes")

        if self.kind == INT:
            if text == "":
                return None
            try:
                value = int(float(text))
            except ValueError as exc:
                raise ConfigEditError(f"{self.label} must be a whole number") from exc
            return self._bound(value)

        if self.kind == FLOAT:
            if text == "":
                return None
            try:
                value = float(text)
            except ValueError as exc:
                raise ConfigEditError(f"{self.label} must be a number") from exc
            return self._bound(value)

        if self.kind == CHOICE:
            allowed = [choice for choice, _ in self.choices]
            if text not in allowed:
                raise ConfigEditError(
                    f"{self.label} must be one of {', '.join(allowed)}; got {text!r}"
                )
            return text

        if self.kind == LIST:
            # Comma or whitespace separated: a list is small and typing brackets
            # into a text box is worse than typing names. Brackets and quotes are
            # stripped rather than trusted, so a caller that posts ``repr(value)``
            # gets the list it meant instead of a weekday called ``["sat",``.
            cleaned = text.replace("[", " ").replace("]", " ").replace(",", " ").replace('"', " ")
            return [item for item in cleaned.replace("'", " ").split() if item]

        return text

    def _bound(self, value: float) -> float:
        if self.minimum is not None and value < self.minimum:
            raise ConfigEditError(f"{self.label} must be at least {self.minimum:g}")
        if self.maximum is not None and value > self.maximum:
            raise ConfigEditError(f"{self.label} must be at most {self.maximum:g}")
        return value


@dataclass(frozen=True, slots=True)
class Section:
    """A titled group of fields, rendered as one panel."""

    title: str
    note: str = ""
    fields: tuple[Field, ...] = ()
    advanced: bool = False
    """Tucked under "Advanced" rather than shown with the main settings.

    These are the numbers that decide *how* a break is assembled -- pod
    tolerance, match thresholds, type metrics -- where the default is right far
    more often than not. Keeping them out of the main list is what lets the page
    read as "here is what this channel is" rather than a wall of knobs.
    """


@dataclass(frozen=True, slots=True)
class SettingsFile:
    """One YAML file the page can edit, and how to reach its values."""

    slug: str
    filename: str
    """Path relative to ``config/``, e.g. ``days/weekday.yaml``."""
    title: str
    purpose: str
    root_path: tuple[str | int, ...]
    """Where the editable mapping lives inside the document.

    A string indexes a mapping, an int indexes a list. ``("channel",)`` is the
    usual case; a day file is ``("days", 0)`` because its payload is the first
    entry of a list, which is why a single "wrapper key" was not enough.
    """
    sections: tuple[Section, ...]
    category: str = ""
    """Heading this file is listed under, so related settings sit together."""

    @property
    def path(self) -> Path:
        return Path(*self.filename.split("/"))

    def all_fields(self) -> Iterable[Field]:
        for section in self.sections:
            yield from section.fields


# -- the registry -----------------------------------------------------------

_STRICTNESS = (
    ("strict", "Strict — only a card naming the show, or nothing"),
    ("semi", "Semi — right show, else a promo, else a neutral card"),
    ("off", "Off — also tolerate a card that fits the moment poorly"),
)


def _daily_choices() -> tuple[tuple[str, str], ...]:
    return tuple((day, day.title()) for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"))


SETTINGS: tuple[SettingsFile, ...] = (
    SettingsFile(
        slug="channel",
        filename="channel.yaml",
        title="Channel",
        purpose="What the channel is called, how the clock works, and how far ahead it plans.",
        category="The channel",
        root_path=("channel",),
        sections=(
            Section(
                "Identity",
                "How the channel appears in a player's guide.",
                (
                    Field("name", "Channel name", default="Adult Swim"),
                    Field("number", "Channel number", default="1"),
                    Field(
                        "tvg_id",
                        "TVG id",
                        help="Must match the guide's channel id. Change it only if you know why.",
                        default="adultswim.1",
                    ),
                    Field("group", "Guide group", default="Nostalgia"),
                    Field(
                        "logo",
                        "Logo URL",
                        help="Shown in client guides. Leave blank for none.",
                        placeholder="https://…",
                    ),
                ),
            ),
            Section(
                "Broadcast grid",
                advanced=True,
                note="Slot boundaries land on this grid, so it decides what \":30\" means.",
                fields=(
                    Field("grid.anchor", "Day zero", DAY, help="The date that maps to the channel's start."),
                    Field(
                        "grid.quantum_minutes",
                        "Slot quantum",
                        INT,
                        help="Minutes between slot boundaries. 15 gives :00/:15/:30/:45.",
                        minimum=1,
                        maximum=60,
                        default=15,
                    ),
                    Field(
                        "grid.timezone",
                        "Timezone",
                        TEXT,
                        help="IANA name. Decides what a wall-clock time means for you.",
                        placeholder="Asia/Tokyo",
                        default="Asia/Tokyo",
                    ),
                ),
            ),
            Section(
                "Era",
                note="The year being *recreated*. This must not follow today's date, or a "
                "2003 channel airs this year's commercials.",
                fields=(
                    Field(
                        "era_year",
                        "Era year",
                        INT,
                        help="Decides eligible commercials and the look of generated bumps.",
                        minimum=1900,
                        maximum=2999,
                        default=2003,
                    ),
                    Field(
                        "on_air_date",
                        "On-air date",
                        DAY,
                        help="Optional. Blank uses today, which is what a live channel wants.",
                    ),
                    Field(
                        "horizon_days",
                        "Plan ahead (days)",
                        INT,
                        help="How many days of playout to keep written. The streamer picks new files up without a restart.",
                        minimum=1,
                        maximum=14,
                        default=3,
                    ),
                ),
            ),
        ),
    ),
    SettingsFile(
        slug="ads",
        filename="ads.yaml",
        title="Commercials",
        purpose="How much of the hour is spots, and how strictly they respect their era.",
        category="What airs between the shows",
        root_path=("ads",),
        sections=(
            Section(
                "Pool",
                "",
                (
                    Field(
                        "cooldown_hours",
                        "No repeat within (hours)",
                        INT,
                        help="A commercial cannot air again inside this window.",
                        minimum=0,
                        default=2,
                    ),
                    Field(
                        "era_drift_years",
                        "Commercial era tolerance (± years)",
                        INT,
                        help="How far from the channel's era year a *commercial* may be and still "
                        "air. 0 is strictly the same year; 2 means a 2001-2005 spot can run on a "
                        "2003 channel. Untagged spots are always eligible, because unknown year "
                        "is not wrong year.",
                        minimum=0,
                        maximum=10,
                        default=2,
                    ),
                ),
            ),
            Section(
                "Pod fitting",
                advanced=True,
                note="Real spots run 30.02s or 14.98s, so a pod is allowed to land slightly off-target "
                "and the slot's tail absorbs the difference.",
                fields=(
                    Field(
                        "pod_tolerance_ms",
                        "Pod tolerance (ms)",
                        INT,
                        help="0 demands millisecond-exact pods and trims almost every spot.",
                        minimum=0,
                        maximum=5000,
                        default=250,
                    ),
                    Field(
                        "trim_slop_ms",
                        "Maximum trim (ms)",
                        INT,
                        help="When the pool cannot reach the target, shorten one spot by at most this.",
                        minimum=0,
                        maximum=5000,
                        default=1500,
                    ),
                ),
            ),
            Section(
                "Smooth breaks",
                advanced=True,
                note="Spots come from different networks and tapes, so cutting between them "
                "exposes every difference. Run `nostalgia conform` once to give them one "
                "picture shape, one loudness and a short dip between spots; this joins "
                "each break into a single file so the player has far fewer seams to cross.",
                fields=(
                    Field(
                        "merge_breaks",
                        "Join each break into one file",
                        BOOL,
                        help="Needs `nostalgia conform` to have run. Anything not conformed "
                        "still airs normally, so it is safe to turn on early. Cuts the number "
                        "of playlist boundaries a day from ~840 to ~300.",
                        default=False,
                    ),
                    Field(
                        "dip_ms",
                        "Dip between spots (ms)",
                        INT,
                        help="A few frames of black and silence between commercials, like a "
                        "recorded break. 0 is a hard cut. Changing it re-conforms the library "
                        "the next time you run `nostalgia conform`.",
                        minimum=0,
                        maximum=500,
                        default=100,
                    ),
                    Field(
                        "merge_max_minutes",
                        "Longest merged break (minutes)",
                        INT,
                        help="A longer run is split into several files.",
                        minimum=1,
                        maximum=30,
                        default=8,
                    ),
                ),
            ),
            Section(
                "Extras instead of commercials",
                advanced=True,
                note="Put music videos or DVD extras in media/extras/ and a break can occasionally air "
                "one instead of spots. Bump slots are never replaced. A replaced break keeps its "
                "own length, so the next programme still starts on time.",
                fields=(
                    Field(
                        "extras_enabled",
                        "Air extras instead of some commercial breaks",
                        BOOL,
                        help="Off (the default) means operate normally with full commercials at "
                        "every break. On replaces roughly one break in seven with something from "
                        "media/extras/, at the rate below.",
                        default=False,
                    ),
                    Field(
                        "extras_chance",
                        "Chance of an extras break",
                        FLOAT,
                        help="Only used when the switch above is on. 0.15 is roughly one break in "
                        "seven, 0.2 is one in five. The roll is seeded from the date and the "
                        "break's position, so the same day always produces the same breaks.",
                        minimum=0.0,
                        maximum=1.0,
                        step=0.01,
                        default=0.15,
                    ),
                    Field(
                        "extras_cooldown_hours",
                        "No extras repeat within (hours)",
                        INT,
                        help="Longer than the commercial cooldown on purpose: the extras pool is "
                        "small, and the same music video three times in an evening is the most "
                        "obvious sign that a program built the schedule.",
                        minimum=0,
                        maximum=720,
                        default=24,
                    ),
                ),
            ),
        ),
    ),
    SettingsFile(
        slug="bumps",
        filename="bumps.yaml",
        title="Bumpers",
        purpose="How carefully a continuity card has to match the show it sits next to.",
        category="What airs between the shows",
        root_path=("bumps",),
        sections=(
            Section(
                "Matching",
                advanced=True,
                note="A card can never contradict the running order — \"next up: X\" when X is not "
                "next is refused in code, not configurable. These decide everything else.",
                fields=(
                    Field(
                        "strictness",
                        "Strictness",
                        CHOICE,
                        choices=_STRICTNESS,
                        default="semi",
                    ),
                    Field(
                        "allow_other_shows",
                        "Allow promos for other shows",
                        BOOL,
                        help="A real 2003 break was mostly promos for other programmes. "
                        "Off requires a card naming the show, or nothing.",
                        default=True,
                    ),
                    Field(
                        "promo_share",
                        "General-card share",
                        FLOAT,
                        help="How often a general card is used when the show's own card exists. "
                        "0.0 always prefers the show's card; 1.0 never uses it.",
                        minimum=0.0,
                        maximum=1.0,
                        step=0.05,
                        default=0.3,
                    ),
                    Field(
                        "min_score",
                        "Match threshold",
                        FLOAT,
                        help="How well a card must fit the moment (position + era). Not a subject match.",
                        minimum=0.0,
                        maximum=20.0,
                        step=0.5,
                        default=6.0,
                    ),
                    Field(
                        "cooldown_hours",
                        "No repeat within (hours)",
                        INT,
                        minimum=0,
                        default=6,
                    ),
                    Field(
                        "era_drift_years",
                        "Bump era tolerance (± years)",
                        INT,
                        help="How far from the channel's era year a *continuity card* may be. "
                        "Kept separate from the commercial setting because the two pools are "
                        "tagged to different depths. Untagged cards always stay eligible.",
                        minimum=0,
                        maximum=10,
                        default=1,
                    ),
                ),
            ),
            Section(
                "Generated cards",
                "Rendered in the era's style for slots the archive cannot fill. Every one carries "
                "a small artifact so you can always tell it apart from a real card.",
                (
                    Field("generated.enabled", "Enabled", BOOL, default=True),
                    Field(
                        "generated.max_share",
                        "Maximum share of slots",
                        FLOAT,
                        help="Never let more than this fraction of the day's bump slots be synthetic.",
                        minimum=0.0,
                        maximum=1.0,
                        step=0.05,
                        default=0.25,
                    ),
                    Field(
                        "generated.lines",
                        "Cards say a sentence",
                        BOOL,
                        help="Use the approved lines on the Lines page instead of a bare "
                        "\"NEXT / SHOW TITLE\". With no lines approved this changes nothing.",
                        default=True,
                    ),
                    Field(
                        "generated.variants",
                        "Sentences per show and position",
                        INT,
                        help="How many different lines each show gets for each position, so "
                        "a card is not the same twice in a row.",
                        minimum=1,
                        maximum=10,
                        default=3,
                        advanced=True,
                    ),
                    Field(
                        "generated.music",
                        "Lay a music bed under cards",
                        BOOL,
                        help="Uses files in media/music/. Run `nostalgia music harvest` to take "
                        "beds from the text-on-black bumps in your own archive. With none "
                        "there, cards are silent.",
                        default=True,
                    ),
                    Field(
                        "generated.music_from_bumps",
                        "Borrow audio from your own bumps",
                        BOOL,
                        help="Use the real bumps as beds: a card about a show is scored with a "
                        "bump about that show, anything else with a neutral card. A bump about "
                        "a different show is never used, so no voice names the wrong programme.",
                        default=True,
                    ),
                ),
            ),
            Section(
                "Writing new lines",
                advanced=True,
                note="Only the Lines page uses these: asking a local model for more sentences "
                "to approve. Rendering a card never calls it, so a wrong address costs nothing "
                "on air.",
                fields=(
                    Field(
                        "generated.ollama_url",
                        "Local model address",
                        TEXT,
                        help="Where Ollama runs. Blank falls back to the default.",
                        default="http://host.docker.internal:11434",
                    ),
                    Field(
                        "generated.ollama_model",
                        "Local model",
                        TEXT,
                        help="Blank uses the first model Ollama reports.",
                    ),
                ),
            ),
        ),
    ),
    SettingsFile(
        slug="watermark",
        filename="watermark.yaml",
        title="Corner bug",
        purpose="The [adult swim] mark in the bottom-right of every programme.",
        category="The picture",
        root_path=("watermark",),
        sections=(
            Section(
                "Mode",
                "\"Overlay\" asks the streamer to composite it during the transcode it already runs "
                "— cheapest. \"Burn\" writes a marked copy to disk and works on any streamer.",
                (
                    Field(
                        "mode",
                        "Mode",
                        CHOICE,
                        choices=(
                            ("none", "None — no mark anywhere"),
                            ("overlay", "Overlay — composited by the streamer"),
                            ("burn", "Burn — a marked copy is written to disk"),
                        ),
                        default="overlay",
                    ),
                    Field("text", "Mark text", default="[adult swim]"),
                ),
            ),
            Section(
                "Type",
                advanced=True,
                note="The wordmark was Helvetica Neue Condensed Bold, which is not free to redistribute, "
                "so it falls back to a bold Helvetica-metric face squeezed to condensed width.",
                fields=(
                    Field("font", "Font", help="Requested face, resolved through a fallback chain."),
                    Field(
                        "condense",
                        "Condense",
                        FLOAT,
                        help="Horizontal squeeze applied when the resolved face is not condensed.",
                        minimum=0.5,
                        maximum=1.0,
                        step=0.01,
                        default=0.84,
                    ),
                    Field(
                        "font_size",
                        "Size",
                        FLOAT,
                        help="Fraction of frame height. A corner bug is small.",
                        minimum=0.005,
                        maximum=0.2,
                        step=0.001,
                        default=0.026,
                    ),
                    Field("colour", "Colour", default="white"),
                    Field(
                        "opacity",
                        "Opacity",
                        FLOAT,
                        minimum=0.05,
                        maximum=1.0,
                        step=0.01,
                        default=0.88,
                    ),
                    Field("uppercase", "Uppercase", BOOL, default=False),
                ),
            ),
            Section(
                "Placement",
                advanced=True,
                note="Fractions of the frame, so a 4:3 rip and a 16:9 episode each get a mark sized to "
                "their own picture.",
                fields=(
                    Field(
                        "margin_x_percent",
                        "Right margin (%)",
                        FLOAT,
                        minimum=0.0,
                        maximum=40.0,
                        step=0.1,
                        default=2.4,
                    ),
                    Field(
                        "margin_y_percent",
                        "Bottom margin (%)",
                        FLOAT,
                        minimum=0.0,
                        maximum=40.0,
                        step=0.1,
                        default=4.2,
                    ),
                    Field(
                        "width_percent",
                        "Width (%)",
                        FLOAT,
                        help="Overlay mode only.",
                        minimum=0.0,
                        maximum=60.0,
                        step=0.5,
                        default=11.0,
                    ),
                    Field(
                        "shows",
                        "Applies to these shows",
                        LIST,
                        help="Space or comma separated show ids. Blank means every show.",
                    ),
                    Field(
                        "except_shows",
                        "Skip these shows",
                        LIST,
                        help="Shows whose rips already carry the network's own logo, so adding "
                        "this one would put two in the corner. Erring towards marking is the "
                        "wrong way to be wrong: a missing logo looks like a clean master, a "
                        "duplicate looks broken.",
                    ),
                    Field(
                        "skip_when_burned_in",
                        "Skip episodes that already have one",
                        BOOL,
                        help="Burn mode only, so you never get two bugs stacked in the corner.",
                        default=True,
                    ),
                ),
            ),
        ),
    ),
    SettingsFile(
        slug="encoding",
        filename="channel.yaml",
        title="Encoding",
        purpose="The default transcode. 720p/2.5 Mbps keeps a 24/7 channel affordable.",
        category="The channel",
        root_path=("encoding",),
        sections=(
            Section(
                "Output",
                "",
                (
                    Field("width", "Width", INT, minimum=160, maximum=3840, default=1280),
                    Field("height", "Height", INT, minimum=120, maximum=2160, default=720),
                    Field(
                        "bitrate_kbps",
                        "Video bitrate (kbps)",
                        INT,
                        minimum=200,
                        maximum=40000,
                        default=2500,
                    ),
                    Field(
                        "audio_bitrate_kbps",
                        "Audio bitrate (kbps)",
                        INT,
                        minimum=32,
                        maximum=512,
                        default=160,
                    ),
                ),
            ),
        ),
    ),
    SettingsFile(
        slug="days/weekday",
        filename="days/weekday.yaml",
        title="Weekday schedule",
        purpose="The hand-written dayparts that decide what airs when, Monday to Friday.",
        category="The schedule",
        root_path=("days", 0),
        sections=(
            Section(
                "Which days",
                "This file applies to the weekdays ticked here. A day may only appear in one file.",
                (Field("days", "Days", LIST, help="mon tue wed thu fri sat sun"),),
            ),
        ),
    ),
    SettingsFile(
        slug="days/weekend",
        filename="days/weekend.yaml",
        title="Weekend schedule",
        purpose="The hand-written dayparts that decide what airs when, Saturday and Sunday.",
        category="The schedule",
        root_path=("days", 0),
        sections=(
            Section(
                "Which days",
                "This file applies to the weekdays ticked here. A day may only appear in one file.",
                (Field("days", "Days", LIST, help="mon tue wed thu fri sat sun"),),
            ),
        ),
    ),
)

BY_SLUG: dict[str, SettingsFile] = {entry.slug: entry for entry in SETTINGS}


# -- reading ----------------------------------------------------------------


def _yaml():
    """A round-trip YAML reader/writer.

    ``preserve_quotes`` matters more than it looks: these files quote strings
    like ``"1"`` that must stay strings, and silently unquoting a channel number
    to the integer 1 would change its meaning.
    """
    from ruamel.yaml import YAML

    reader = YAML()
    reader.preserve_quotes = True
    reader.width = 4096  # never re-wrap: a re-flowed file is a huge diff
    reader.indent(mapping=2, sequence=4, offset=2)
    return reader


def read_values(entry: SettingsFile, config_dir: Path) -> dict[str, Any]:
    """Current values of every editable field, flattened by dotted key."""
    document = _load_document(entry, config_dir)
    values: dict[str, Any] = {}
    for spec in entry.all_fields():
        found, value = _lookup(document, entry, spec.key)
        values[spec.key] = value if found else spec.default
    return values


def _load_document(entry: SettingsFile, config_dir: Path) -> Any:
    path = config_dir / entry.path
    if not path.is_file():
        return {}
    try:
        return _yaml().load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - an unreadable file must not break the page
        return {}


def _root(document: Any, entry: SettingsFile) -> Any:
    """The mapping that holds this file's editable values."""
    node = document
    for part in entry.root_path:
        if isinstance(part, int):
            if not isinstance(node, list) or part >= len(node):
                return None
            node = node[part]
        else:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
    return node


def _lookup(document: Any, entry: SettingsFile, dotted: str) -> tuple[bool, Any]:
    """Follow *dotted* into *document* from the file's root mapping."""
    node = _root(document, entry)
    if node is None:
        return False, None
    for part in dotted.split("."):
        if isinstance(node, list):
            if not part.isdigit() or int(part) >= len(node):
                return False, None
            node = node[int(part)]
            continue
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


# -- writing ----------------------------------------------------------------


def save_values(
    entry: SettingsFile,
    config_dir: Path,
    submitted: dict[str, str | None],
) -> list[str]:
    """Write *submitted* into the file, returning a list of changed labels.

    Only keys present in the schema are written, so a stray form field can never
    inject YAML the planner will silently ignore or, worse, act on.
    """
    from ruamel.yaml.comments import CommentedMap

    path = config_dir / entry.path

    reader = _yaml()
    original = path.read_bytes() if path.is_file() else b""
    document = (
        reader.load(original.decode("utf-8"))
        if original
        else CommentedMap()
    )
    if document is None:
        document = CommentedMap()

    changed: list[str] = []
    for spec in entry.all_fields():
        if spec.key not in submitted:
            continue
        value = spec.coerce(submitted[spec.key])
        existing, current = _lookup(document, entry, spec.key)
        leaf = spec.key.split(".")[-1]

        # A blank optional setting means "unset", not "the empty string". Writing
        # `style_pack: ''` uncomments a key the owner had deliberately left out and
        # leaves a line that says nothing; deleting it restores the model default.
        if value == "" and spec.kind in (TEXT, DAY, INT, FLOAT):
            if not existing:
                continue
            target = _target_map(document, entry, spec.key)
            del target[leaf]
            changed.append(spec.label)
            continue

        if existing and current == value:
            continue
        if not existing and _equals_default(spec, value):
            # The key is absent and the form only echoes the default, so writing it
            # would add a line that says nothing and turn "the page was saved" into
            # "every default was pinned into the file".
            continue
        _target_map(document, entry, spec.key)[leaf] = value
        changed.append(spec.label)

    if not changed:
        return changed

    buffer = io.StringIO()
    reader.dump(document, buffer)
    output = buffer.getvalue()
    if b"\r\n" in original:
        output = output.replace("\r\n", "\n").replace("\n", "\r\n")
    _write_atomic(path, output)
    return changed


def _write_atomic(path: Path, content: str | bytes) -> None:
    """Replace a YAML file atomically so concurrent readers never see half a write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = None
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp",
            dir=path.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
            data = content.encode("utf-8") if isinstance(content, str) else content
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_all(
    config_dir: Path,
    submitted: dict[str, str | None],
) -> list[tuple[str, list[str]]]:
    """Write one submission into every file it touches.

    The page has one save button, so the form is one namespace: each control is
    named ``<slug>::<key>`` and this splits it back apart. Only the files that
    actually changed are rewritten, so a save cannot damage a file whose fields
    were left alone.

    Validating the *whole* config happens in the route after this, because a
    change in one file can only be judged against the others (a day file that
    claims a weekday another file also claims is the classic case).
    """
    by_slug: dict[str, dict[str, str | None]] = {}
    present = {name for name in submitted if "::" in name}
    for name, value in submitted.items():
        slug, _, key = name.partition("::")
        if not key or slug not in BY_SLUG:
            # A field the page does not own is ignored rather than written: a
            # stray form key must never reach the YAML.
            continue
        by_slug.setdefault(slug, {})[key] = value

    # An unticked checkbox submits nothing at all, so a boolean that is *absent*
    # from the form means false. Only for files this submission covers, though:
    # defaulting every file in the registry would rewrite settings the page never
    # showed. A partial post is a legitimate case (the tests, and any future API).
    for slug in by_slug:
        for spec in BY_SLUG[slug].all_fields():
            if spec.kind == BOOL and f"{slug}::{spec.key}" not in present:
                by_slug[slug][spec.key] = ""

    # A file with no field in this submission is not written at all.

    written: list[tuple[str, list[str]]] = []
    for slug, values in by_slug.items():
        entry = BY_SLUG[slug]
        changed = save_values(entry, config_dir, values)
        if changed:
            written.append((entry.title, changed))
    return written


def _equals_default(spec: Field, value: Any) -> bool:
    """Whether *value* is exactly what the model already defaults to."""
    if spec.default is None:
        return value in (None, "", [])
    if isinstance(spec.default, bool) or isinstance(value, bool):
        return bool(spec.default) == bool(value)
    if isinstance(spec.default, (int, float)) and isinstance(value, (int, float)):
        return float(spec.default) == float(value)
    return spec.default == value


def _target_map(document: Any, entry: SettingsFile, dotted: str) -> Any:
    """The mapping that should receive *dotted*, creating parents as needed."""
    from ruamel.yaml.comments import CommentedMap

    # Walk the root path, creating it when the file is missing the wrapper (or the
    # list entry) altogether -- a hand-deleted `channel:` block should be
    # repairable from the page rather than throwing.
    node = document
    for part in entry.root_path:
        if isinstance(part, int):
            if not isinstance(node, list):
                raise ConfigEditError(f"{entry.filename} has no list to index at {part}")
            while len(node) <= part:
                node.append(CommentedMap())
            node = node[part]
            continue
        if not isinstance(node, dict):
            raise ConfigEditError(f"{entry.filename} is not a mapping at {part!r}")
        if part not in node:
            # A container is only created when it is genuinely absent. Overwriting
            # an existing one because it failed a type check is how a day file's
            # list of dayparts got replaced by an empty mapping.
            node[part] = CommentedMap()
        node = node[part]

    parts = dotted.split(".")[:-1]
    for part in parts:
        if isinstance(node, list):
            if not part.isdigit():
                raise ConfigEditError(f"{entry.filename}: {part!r} is not a list index")
            index = int(part)
            while len(node) <= index:
                node.append(CommentedMap())
            node = node[index]
            continue
        if not isinstance(node, dict):
            raise ConfigEditError(f"{entry.filename}: {part!r} is not a mapping")
        if part not in node:
            node[part] = CommentedMap()
        node = node[part]
    return node


__all__ = [
    "BOOL",
    "BY_SLUG",
    "CHOICE",
    "ConfigEditError",
    "DAY",
    "FLOAT",
    "Field",
    "INT",
    "LIST",
    "SETTINGS",
    "Section",
    "SettingsFile",
    "TEXT",
    "read_values",
    "save_all",
    "save_values",
]

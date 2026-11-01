"""FastAPI app: a small "what's on" page and the bump review queue.

Deliberately tiny and read-mostly. It exists because the one thing a semi-strict
bump matcher needs is a fast way for a human to confirm guesses, and because
tuning a 24/7 channel is much easier when you can see what is airing.

The page is server-rendered HTML with no build step, so it works with the same
Python environment as the scheduler.
"""

from __future__ import annotations

import json
import mimetypes
import sqlite3
from collections import Counter
from dataclasses import asdict
from datetime import date
from threading import Lock
from time import monotonic
from typing import Any, Callable, Sequence
from urllib.parse import quote_plus
from uuid import UUID

from fastapi import Request

from ..catalog import Catalog
from ..config import ConfigError, RootConfig, load_config
from ..media_review import (
    MediaImportError,
    analyze_source,
    export_segments,
    load_analysis,
    resolve_source,
)
from ..paths import Paths
from ..pipeline import build
from ..rotation import EpisodeRef
from .config_edit import (
    BOOL,
    CHOICE,
    DAY,
    FLOAT,
    INT,
    LIST,
    SETTINGS,
    ConfigEditError,
    Section,
    SettingsFile,
    read_values,
)
from .continuity_edit import (
    FAMILY_LABELS,
    MAX_MID,
    SLOT_FAMILIES,
    ContinuityEditError,
    apply_form,
    parse_form as parse_placement,
    pod_shares,
    read_all,
    template_for,
)
from . import lines as lines_web
from . import imports as imports_web
from .commercials import commercials_body, set_family_enabled, set_spot_enabled
from .schedule import (
    ScheduleFormError,
    load_schedule,
    parse_form,
    write_schedule,
)
from .view import apply_tag, now_playing, pending_tags, plan_for_day


class _ProgressTracker:
    """Short-lived progress snapshots for browser-submitted schedule operations."""

    _MAX_TASKS = 128
    _MAX_AGE_SECONDS = 600

    def __init__(self) -> None:
        self._lock = Lock()
        self._tasks: dict[str, dict[str, Any]] = {}

    def _prune(self, now: float) -> None:
        expired = [
            task_id
            for task_id, task in self._tasks.items()
            if task["state"] != "running"
            and now - task["updated"] > self._MAX_AGE_SECONDS
        ]
        for task_id in expired:
            del self._tasks[task_id]
        while len(self._tasks) > self._MAX_TASKS:
            oldest = min(self._tasks, key=lambda key: self._tasks[key]["updated"])
            del self._tasks[oldest]

    def start(self, task_id: str) -> None:
        now = monotonic()
        with self._lock:
            self._prune(now)
            self._tasks[task_id] = {
                "state": "running",
                "steps": [{"label": "Starting your change", "state": "active"}],
                "updated": now,
            }

    def advance(self, task_id: str | None, label: str) -> None:
        if task_id is None:
            return
        now = monotonic()
        with self._lock:
            self._prune(now)
            task = self._tasks.get(task_id)
            if task is None or task["state"] != "running":
                return
            task["steps"][-1]["state"] = "complete"
            task["steps"].append({"label": label, "state": "active"})
            task["updated"] = now

    def fail(self, task_id: str | None, label: str) -> None:
        if task_id is None:
            return
        now = monotonic()
        with self._lock:
            self._prune(now)
            task = self._tasks.get(task_id)
            if task is None or task["state"] != "running":
                return
            task["steps"][-1] = {"label": label, "state": "failed"}
            task["state"] = "failed"
            task["updated"] = now

    def finish(self, task_id: str, *, success: bool) -> None:
        now = monotonic()
        with self._lock:
            self._prune(now)
            task = self._tasks.get(task_id)
            if task is None or task["state"] != "running":
                return
            task["state"] = "complete" if success else "failed"
            if task["steps"]:
                task["steps"][-1]["state"] = "complete" if success else "failed"
            task["updated"] = now

    def get(self, task_id: str) -> dict[str, Any] | None:
        now = monotonic()
        with self._lock:
            self._prune(now)
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return {
                "state": task["state"],
                "steps": [dict(step) for step in task["steps"]],
            }


_CSS = """
/* SMPTE ECR 1-1978 NTSC colour bars. The official levels are 75% amplitude,
   which is why these are not the fully-saturated primaries people remember
   from a TV set pushed too far -- a monitor fed real 100% bars looks wrong. */
:root {
  color-scheme: dark;
  --gray:     #c0c0c0;
  --yellow:   #c0c000;
  --cyan:     #00c0c0;
  --green:    #00c000;
  --magenta:  #c000c0;
  --red:      #c00000;
  --blue:     #0000c0;
  --black:    #131313;
  --navy:     #101040;
  --white:    #ffffff;
  --pluge-lo: #0b0b0b;
  --pluge-hi: #1f1f1f;

  /* UI colours derived from the bars, so the page reads as one object. */
  --fg:       #e8e8e8;
  --dim:      #9aa0a6;
  --accent:   var(--cyan);
  --line:     #2b2b2b;
  --panel:    #0f0f10;
  --bar-h:    14px;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: #050505;
  color: var(--fg);
  /* A condensed grotesque is period-correct for the on-screen type, with
     fallbacks that exist on every platform. */
  font: 15px/1.55 "Arial Narrow", "Helvetica Neue", Arial, system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
}

/* The set itself: a CRT bezel around everything. */
.crt {
  min-height: 100vh;
  position: relative;
  overflow-x: hidden;
  background:
    radial-gradient(120% 90% at 50% 0%, #101012 0%, #08080a 55%, #000 100%);
}

/* Scanlines + a soft vignette. Kept low-opacity: this is a test card, not a
   filter over the content. */
.crt::after {
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 9999;
  background:
    repeating-linear-gradient(
      to bottom,
      rgba(255,255,255,.035) 0px,
      rgba(255,255,255,.035) 1px,
      transparent 1px,
      transparent 3px
    ),
    radial-gradient(130% 100% at 50% 50%, transparent 55%, rgba(0,0,0,.55) 100%);
  mix-blend-mode: overlay;
}

/* -- the colour bars ------------------------------------------------------- */

.bars {
  height: var(--bar-h);
  display: grid;
  /* Seven 75% bars, left to right: grey, yellow, cyan, green, magenta, red,
     blue. */
  grid-template-columns: repeat(7, 1fr);
  border-bottom: 1px solid #000;
}
.bars span:nth-child(1) { background: var(--gray); }
.bars span:nth-child(2) { background: var(--yellow); }
.bars span:nth-child(3) { background: var(--cyan); }
.bars span:nth-child(4) { background: var(--green); }
.bars span:nth-child(5) { background: var(--magenta); }
.bars span:nth-child(6) { background: var(--red); }
.bars span:nth-child(7) { background: var(--blue); }

/* The castellation strip: blue, black, magenta, black, cyan, black, grey. */
.castellations {
  height: calc(var(--bar-h) * .8);
  display: grid;
  grid-template-columns: repeat(7, 1fr);
}
.castellations span:nth-child(1) { background: var(--blue); }
.castellations span:nth-child(2) { background: var(--black); }
.castellations span:nth-child(3) { background: var(--magenta); }
.castellations span:nth-child(4) { background: var(--black); }
.castellations span:nth-child(5) { background: var(--cyan); }
.castellations span:nth-child(6) { background: var(--black); }
.castellations span:nth-child(7) { background: var(--gray); }

/* Bottom third: the PLUGE / black-level band. This is the part of the pattern
   that actually earns its keep on a real monitor -- and it reads as a rule under
   the page. */
.pluge {
  height: calc(var(--bar-h) * .7);
  display: grid;
  grid-template-columns: 1.2fr 2fr 1.2fr 3fr 2fr;
  align-items: stretch;
}
.pluge span:nth-child(1) { background: var(--navy); }
.pluge span:nth-child(2) { background: var(--white); }
.pluge span:nth-child(3) { background: var(--pluge-lo); }
.pluge span:nth-child(4) { background: var(--black); }
.pluge span:nth-child(5) { background: var(--pluge-hi); }

/* -- PAL variant ----------------------------------------------------------
   PAL bars are the same seven colours but led by a 100% white bar and without
   the castellation strip.

   Worth having, not worth defaulting to: [adult swim] was a US NTSC channel, and
   Japan used NTSC-J -- the same 525/60 system -- so NTSC is the correct pattern
   for a recreation of either. PAL is here for a PAL variant of the channel. */
.crt.pal .bars span:nth-child(1) { background: var(--white); }
.crt.pal .castellations { display: none; }
.crt.pal .pluge {
  grid-template-columns: repeat(5, 1fr);
  height: calc(var(--bar-h) * 1.1);
}
.crt.pal .pluge span:nth-child(1) { background: var(--white); }
.crt.pal .pluge span:nth-child(2) { background: var(--yellow); }
.crt.pal .pluge span:nth-child(3) { background: var(--cyan); }
.crt.pal .pluge span:nth-child(4) { background: var(--green); }
.crt.pal .pluge span:nth-child(5) { background: var(--magenta); }

/* -- chrome ---------------------------------------------------------------- */

header {
  padding: 18px 28px;
  display: flex;
  gap: 22px;
  align-items: baseline;
  flex-wrap: wrap;
  border-bottom: 1px solid var(--line);
  background: linear-gradient(180deg, #131315 0%, var(--panel) 100%);
}
header h1 {
  font-size: 17px;
  margin: 0;
  letter-spacing: .22em;
  text-transform: uppercase;
  font-weight: 700;
}
header h1 .bracket { color: var(--accent); }
header nav { margin-left: auto; }
header nav a {
  color: var(--dim);
  text-decoration: none;
  margin-left: 20px;
  font-size: 12px;
  letter-spacing: .16em;
  text-transform: uppercase;
  padding-bottom: 3px;
}
header nav a:hover { color: var(--fg); }
/* The active tab is marked with a bar, like a channel indicator. */
header nav a.active {
  color: var(--fg);
  border-bottom: 2px solid var(--accent);
}

main { padding: 26px 28px 60px; max-width: 940px; }

.card {
  border: 1px solid var(--line);
  background: var(--panel);
  padding: 18px 20px;
  margin-bottom: 16px;
  /* Period-appropriate: broadcast furniture was rectangles, not pills. */
  border-radius: 2px;
}
/* The "on now" card gets a colour-bar spine so the eye lands on it. */
.card.now { border-left: 4px solid var(--cyan); }
.card.now .now-title { text-shadow: 0 0 18px rgba(0,192,192,.25); }

.label {
  color: var(--dim);
  font-size: 10px;
  letter-spacing: .2em;
  text-transform: uppercase;
  margin: 0 0 4px;
}
.now-title { font-size: 27px; margin: 2px 0 2px; font-weight: 700; letter-spacing: .01em; }
.now-sub { color: var(--dim); margin: 0; }

/* Skip sits under the title, not beside it: the button is a deliberate act and
   should not be reachable by a stray click on the largest thing on the page. */
.skip-form { margin: 14px 0 6px; }
.skip-form button {
  font: inherit;
  font-weight: 700;
  letter-spacing: .04em;
  text-transform: uppercase;
  font-size: 13px;
  color: var(--ink);
  background: transparent;
  border: 2px solid var(--cyan);
  border-radius: 3px;
  padding: 8px 16px;
  cursor: pointer;
}
.skip-form button:hover,
.skip-form button:focus-visible {
  background: var(--cyan);
  color: #000;
  outline: none;
}
.kind { color: var(--accent); text-transform: uppercase; letter-spacing: .1em; }

table { border-collapse: collapse; width: 100%; }
td, th {
  text-align: left;
  padding: 7px 10px 7px 0;
  border-bottom: 1px solid #1a1a1c;
  font-size: 14px;
}
th {
  color: var(--dim);
  font-weight: 500;
  font-size: 10px;
  letter-spacing: .18em;
  text-transform: uppercase;
}
tbody tr:hover { background: #151517; }
.mono { font-variant-numeric: tabular-nums; color: var(--dim); font-feature-settings: "tnum"; }

form { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 12px; }
input, select, button {
  background: #101012;
  color: var(--fg);
  border: 1px solid var(--line);
  border-radius: 2px;
  padding: 7px 10px;
  font: inherit;
  font-size: 13px;
}
input:focus, select:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
button {
  cursor: pointer;
  background: var(--navy);
  border-color: #2a2a72;
  letter-spacing: .1em;
  text-transform: uppercase;
  font-size: 11px;
}
button:hover { background: #1a1a5e; border-color: var(--accent); }
button.secondary { background: transparent; color: var(--dim); }
button.secondary:hover { color: var(--fg); }

.empty { color: var(--dim); font-style: italic; }

/* A one-button form inside a table cell. The default `form` is a flex row with a
   top margin, which staggered every row of the commercials table. */
form.inline { display: inline; margin: 0; }
form.inline button { padding: 4px 10px; font-size: 10px; }

/* A link that reads as a control, for the filter toggles. */
a.button {
  display: inline-block;
  margin-left: 10px;
  padding: 4px 10px;
  border: 1px solid var(--line);
  border-radius: 2px;
  background: var(--navy);
  border-color: #2a2a72;
  color: var(--fg);
  text-decoration: none;
  font-size: 10px;
  letter-spacing: .1em;
  text-transform: uppercase;
}
a.button:hover { border-color: var(--accent); }

/* Disabled rows stay legible: they are the ones being looked for. */
tr:has(.kind) td { color: var(--dim); }

.stats { display: flex; gap: 34px; flex-wrap: wrap; }
.stat b { display: block; font-size: 21px; font-weight: 700; font-variant-numeric: tabular-nums; }

/* A legend, so the colours are not merely decorative. */
.legend {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  margin-top: 10px;
  font-size: 10px;
  letter-spacing: .14em;
  text-transform: uppercase;
  color: var(--dim);
}
.legend i {
  display: inline-block;
  width: 10px; height: 10px;
  margin-right: 5px;
  vertical-align: -1px;
  border: 1px solid #333;
}

footer {
  padding: 16px 28px 40px;
  color: #555;
  font-size: 11px;
  letter-spacing: .16em;
  text-transform: uppercase;
}

/* -- schedule editor ------------------------------------------------------
   The modes are exclusive, so only the panel for the chosen mode is shown.
   Done with :checked rather than script, so the page works before -- and
   without -- any JavaScript. */
.modes { display: flex; gap: 0; margin: 0 0 16px; border: 1px solid var(--line); }
.modes label {
  flex: 1;
  padding: 11px 12px;
  text-align: center;
  cursor: pointer;
  font-size: 11px;
  letter-spacing: .16em;
  text-transform: uppercase;
  color: var(--dim);
  border-right: 1px solid var(--line);
}
.modes label:last-child { border-right: 0; }
.modes input { position: absolute; opacity: 0; pointer-events: none; }
.modes input:checked + span { color: var(--fg); }
.modes label:has(input:checked) {
  background: var(--navy);
  color: var(--fg);
  box-shadow: inset 0 -2px 0 var(--accent);
}

.panel { display: none; }
.panel.show { display: block; }
/* Mode switching with no script: each radio drives its own panel through
   :has(), falling back to the server-rendered .show class where :has() is
   unavailable -- the page still works, it just needs a save to switch. */
body:has(.modes input[value="lineup"]:checked) .panel[data-mode="dayparts"],
body:has(.modes input[value="shuffle"]:checked) .panel[data-mode="dayparts"],
body:has(.modes input[value="dayparts"]:checked) .panel[data-mode="lineup"],
body:has(.modes input[value="shuffle"]:checked) .panel[data-mode="lineup"],
body:has(.modes input[value="dayparts"]:checked) .panel[data-mode="shuffle"],
body:has(.modes input[value="lineup"]:checked) .panel[data-mode="shuffle"] {
  display: none;
}
body:has(.modes input[value="lineup"]:checked) .panel[data-mode="lineup"],
body:has(.modes input[value="shuffle"]:checked) .panel[data-mode="shuffle"],
body:has(.modes input[value="dayparts"]:checked) .panel[data-mode="dayparts"] {
  display: block;
}

.row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.row select, .row input { flex: 1; min-width: 0; }
.row .num { flex: 0 0 78px; }
.row button { flex: 0 0 auto; }
.lineup-row { flex-wrap: wrap; }
.lineup-row select:first-child { flex: 2 1 190px; }
.lineup-row select:nth-child(2) { flex: 1 1 120px; }
.lineup-row input[type="number"] { flex: 0 1 82px; }

.hint { color: var(--dim); font-size: 12px; margin: 0 0 12px; max-width: 62ch; }
.hint code { color: var(--accent); }
.warn { color: var(--yellow); font-size: 12px; }
.dim { color: var(--dim); font-size: 12px; }

.shows { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 4px 14px; }
.shows label { font-size: 13px; color: var(--fg); display: flex; gap: 7px; align-items: center; }
.shows input { flex: 0 0 auto; }

/* -- settings editor ------------------------------------------------ */
/* One form for the whole page, so the save control is pinned to the top of the
   scroll rather than repeated per section. */
/* The panel blocks inside the form are `display: flex` at the top level, which
   put the intro paragraph and the first category heading side by side. The form
   is a column of full-width blocks. */
.settings-form { display: block; }
.settings-form > .hint { display: block; margin: 12px 0 0; max-width: 80ch; }
.settings-form > .category { margin-top: 24px; }
.save-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 0;
  position: sticky;
  top: 0;
  z-index: 3;
  background: var(--bg);
  border-bottom: 1px solid var(--line);
}
.save-bar .label { margin: 0; }
.save-bar.bottom { position: static; justify-content: flex-end; margin-top: 22px; }
.save-bar button { flex: 0 0 auto; }
.settings-form > .hint { margin: 12px 0 0; }
/* Advanced is collapsed by default: the main list is "what this channel is",
   and these are the numbers behind it. */
details.advanced { margin-top: 26px; }
details.advanced > summary {
  cursor: pointer;
  color: var(--accent);
  font-size: 12px;
  letter-spacing: .22em;
  text-transform: uppercase;
  padding: 10px 0;
  border-top: 2px solid var(--accent);
  border-bottom: 1px solid var(--line);
  list-style: none;
}
details.advanced > summary::-webkit-details-marker { display: none; }
details.advanced > summary::before { content: "▸ "; margin-right: 6px; }
details.advanced[open] > summary::before { content: "▾ "; margin-right: 6px; }
details.advanced > summary .dim { letter-spacing: .08em; text-transform: none; }
.panel-block.tucked { border-left: 2px solid var(--line); padding-left: 14px; }
.category {
  font-size: 12px;
  letter-spacing: .26em;
  text-transform: uppercase;
  color: var(--fg);
  margin: 30px 0 12px;
  padding-bottom: 8px;
  border-bottom: 2px solid var(--accent);
}
.category:first-of-type { margin-top: 0; }
/* Two columns on a wide screen so a long list stays scannable, one on a
   phone. The label column is fixed so the controls line up down the page,
   which is what makes a form read as a table of settings rather than a wall. */
/* `block`, not the default: a flex container puts its children side by side,
   which laid the mid-break heading alongside the field above it. */
.settings form { margin-top: 4px; display: block; }
.settings-group {
  border-top: 1px solid var(--line);
  padding-top: 14px;
  margin-top: 14px;
}
.settings-group:first-of-type { border-top: 0; padding-top: 0; margin-top: 0; }
.group-title {
  color: var(--accent);
  font-size: 10px;
  letter-spacing: .22em;
  text-transform: uppercase;
  margin: 0 0 4px;
}
.field {
  display: grid;
  /* The control column takes the wider share: a settings label is a phrase and
     a control needs room. Capped so a wide screen does not stretch a 4-digit
     number box across the page. */
  grid-template-columns: minmax(140px, 220px) minmax(0, 620px);
  gap: 4px 18px;
  align-items: start;
  padding: 7px 0;
  border-bottom: 1px solid #171719;
}
.field:last-child { border-bottom: 0; }
.field > label {
  color: var(--dim);
  font-size: 12px;
  letter-spacing: .04em;
  padding-top: 6px;
}
.field input[type="text"],
.field input[type="number"],
.field select { width: 100%; padding: 6px 8px; font: inherit; font-size: 13px; }
.field-help {
  grid-column: 2;
  color: var(--dim);
  font-size: 11px;
  margin: 1px 0 0;
  max-width: 60ch;
  line-height: 1.45;
}
/* A short number does not need 620px of column. */
.field.narrow { grid-template-columns: minmax(140px, 220px) minmax(0, 120px); }
.field.narrow .field-help { grid-column: 2; }
/* A checkbox reads as a sentence, not as a label above a box. */
.field.check { grid-template-columns: minmax(0, 1fr); }
.field.check > label {
  display: flex;
  gap: 9px;
  align-items: center;
  padding-top: 0;
  font-size: 13px;
  color: var(--fg);
  cursor: pointer;
}
.field.check .field-help { grid-column: 1; margin-left: 25px; }
.settings button { margin-top: 16px; }
.filename { margin: 10px 0 0; font-size: 11px; }

/* The mid-break heading needs its own line, or `display: grid` on the field
   above pulls it into that row. */
.mid-heading { margin-top: 22px; grid-column: 1 / -1; }
.mid-heading .group-title { margin-bottom: 2px; }

/* A mid-break is four related numbers describing one moment, so it reads as a
   single row rather than four more stacked fields. */
.mid-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 18px;
  align-items: center;
  padding: 9px 0;
  border-bottom: 1px solid #171719;
}
.mid-row:last-of-type { border-bottom: 0; }
.mid-label {
  flex: 0 0 108px;
  color: var(--accent);
  font-size: 10px;
  letter-spacing: .18em;
  text-transform: uppercase;
}
.mid-row label {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 12px;
  color: var(--dim);
}
.mid-row input, .mid-row select { padding: 5px 7px; font: inherit; font-size: 13px; }
@media (max-width: 620px) {
  .field { grid-template-columns: minmax(0, 1fr); }
  .field > label { padding-top: 0; }
  .field-help { grid-column: 1; }
}

.saved {
  border-left: 4px solid var(--green);
  padding: 12px 16px;
  margin-bottom: 16px;
  background: var(--panel);
  font-size: 13px;
}
.saved.bad { border-left-color: var(--red); }
.pending { border-left: 3px solid var(--yellow); }
.pending .label { color: var(--yellow); }

.work-progress {
  position: sticky;
  top: 8px;
  z-index: 5;
  border: 1px solid var(--cyan);
  border-left: 4px solid var(--cyan);
  background: var(--panel);
  padding: 12px 16px;
  margin-bottom: 16px;
  box-shadow: 0 4px 18px rgba(0,0,0,.45);
}
.work-progress[hidden] { display: none; }
.work-progress p { margin: 0 0 8px; }
.work-progress ol { list-style: none; margin: 0; padding: 0; }
.work-progress li {
  position: relative;
  min-height: 22px;
  padding: 2px 0 4px 28px;
  color: var(--dim);
}
.work-progress li::before {
  position: absolute;
  left: 2px;
  top: 3px;
  width: 14px;
  height: 14px;
  border: 2px solid var(--dim);
  border-radius: 50%;
  content: "";
}
.work-progress li.complete { color: var(--fg); }
.work-progress li.complete::before {
  content: "✓";
  border-color: var(--green);
  color: var(--green);
  font-size: 11px;
  line-height: 14px;
  text-align: center;
}
.work-progress li.active { color: var(--fg); }
.work-progress li.active::before {
  border-color: var(--cyan);
  border-right-color: transparent;
  animation: progress-spin .8s linear infinite;
}
.work-progress li.failed { color: var(--red); }
.work-progress li.failed::before { border-color: var(--red); }
@keyframes progress-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
  .work-progress li.active::before { animation: none; }
}

@media (max-width: 620px) {
  main, header, footer { padding-left: 16px; padding-right: 16px; }
  .now-title { font-size: 22px; }
  header nav { margin-left: 0; }
  .lineup-row select, .lineup-row input[type="number"] { flex: 1 1 42%; }
}
"""


_BARS = "".join("<span></span>" for _ in range(7))
_CASTELLATIONS = "".join("<span></span>" for _ in range(7))
_PLUGE = "".join("<span></span>" for _ in range(5))


def _test_card(card: str = "ntsc") -> str:
    """The broadcast test pattern, as the page's top chrome.

    Three strips, in the order a real card reads: the colour bars, the
    castellation strip that sits beneath them, and the PLUGE/black-level band.
    The differences between NTSC and PAL live in the CSS (`.crt.pal`), so the
    markup is shared and there is one place each variant is defined.
    """
    castellations = (
        "" if card == "pal" else f'<div class="castellations" aria-hidden="true">{_CASTELLATIONS}</div>'
    )
    return (
        f'<div class="bars" aria-hidden="true">{_BARS}</div>'
        f"{castellations}"
        f'<div class="pluge" aria-hidden="true">{_PLUGE}</div>'
    )



def _page(title: str, body: str, *, active: str = "", card: str = "ntsc") -> str:
    def nav(href: str, label: str) -> str:
        cls = ' class="active"' if href == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    crt_class = "crt pal" if card == "pal" else "crt"
    standard = "EBU PAL" if card == "pal" else "SMPTE ECR 1-1978 NTSC"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><style>{_CSS}</style></head>
<body>
<div class="{crt_class}">
{_test_card(card)}
<header>
  <h1><span class="bracket">[</span>adult swim<span class="bracket">]</span> machine</h1>
  <nav>{nav("/", "On now")}{nav("/schedule", "Schedule")}{nav("/bumps", "Bump review")}{nav("/commercials", "Commercials")}{nav("/imports", "Import media")}{nav("/lines", "Lines")}{nav("/config", "Config")}</nav>
</header>
<main>
  <section id="work-progress" class="work-progress" role="status" aria-live="polite" hidden>
    <p id="work-progress-title">Working on your change</p>
    <ol id="work-progress-steps"></ol>
  </section>
  {body}
</main>
<footer>Nostalgia Machine &middot; {standard}</footer>
</div>
<script>
(() => {{
  const panel = document.getElementById("work-progress");
  const title = document.getElementById("work-progress-title");
  const list = document.getElementById("work-progress-steps");
  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const taskId = () => {{
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    if (!window.crypto || !window.crypto.getRandomValues) return null;
    const bytes = window.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    return hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" +
      hex.slice(12, 16) + "-" + hex.slice(16, 20) + "-" + hex.slice(20);
  }};

  function showProgress(status) {{
    panel.hidden = false;
    const active = status.steps.find((step) => step.state === "active");
    title.textContent = active ? active.label : (
      status.state === "failed" ? "The request failed" : "Change complete"
    );
    list.replaceChildren(...status.steps.map((step) => {{
      const item = document.createElement("li");
      item.className = step.state;
      item.textContent = step.label;
      return item;
    }}));
  }}

  document.addEventListener("click", (event) => {{
    const add = event.target.closest("[data-lineup-add]");
    if (add) {{
      const template = document.getElementById("lineup-row-template");
      const rows = document.getElementById("lineup-rows");
      if (!template || !rows) return;
      const row = template.content.firstElementChild.cloneNode(true);
      rows.append(row);
      row.querySelector("select[name='lineup_show']").focus();
      return;
    }}
    const remove = event.target.closest("[data-lineup-remove]");
    if (remove) remove.closest("[data-lineup-row]").remove();
  }});

  document.addEventListener("submit", async (event) => {{
    const form = event.target.closest("form[data-progress]");
    if (!form) return;
    const id = taskId();
    if (!id) return;
    if (form.getAttribute("aria-busy") === "true") {{
      event.preventDefault();
      return;
    }}
    event.preventDefault();

    const formData = event.submitter
      ? new FormData(form, event.submitter)
      : new FormData(form);
    const controls = [...form.elements].filter((control) => "disabled" in control);
    const disabledBefore = controls.map((control) => control.disabled);
    controls.forEach((control) => {{ control.disabled = true; }});
    form.setAttribute("aria-busy", "true");
    showProgress({{
      state: "running",
      steps: [{{label: form.dataset.progress, state: "active"}}]
    }});

    let keepPolling = true;
    const poll = async () => {{
      while (keepPolling) {{
        try {{
          const response = await fetch(
            "/api/progress/" + encodeURIComponent(id),
            {{cache: "no-store"}}
          );
          if (response.ok) {{
            const status = await response.json();
            showProgress(status);
            if (status.state !== "running") return;
          }}
        }} catch (_) {{
          title.textContent = "Progress updates unavailable; waiting for the save to finish.";
        }}
        await pause(300);
      }}
    }};
    const polling = poll();

    try {{
      const response = await fetch(form.action || window.location.href, {{
        method: (form.method || "POST").toUpperCase(),
        body: formData,
        headers: {{"X-Progress-ID": id}},
        redirect: "follow"
      }});
      keepPolling = false;
      await polling;
      window.location.assign(response.url || form.action);
    }} catch (_) {{
      keepPolling = false;
      await polling;
      title.textContent = "Could not confirm the result. Refresh before trying again.";
      controls.forEach((control, index) => {{
        control.disabled = disabledBefore[index];
      }});
      form.removeAttribute("aria-busy");
    }}
  }});
}})();
</script>
</body></html>"""


def create_app(paths: Paths, config: RootConfig) -> Any:
    """Build the ASGI app. Imported lazily so the core has no FastAPI dependency."""
    try:
        from fastapi import FastAPI, Form, HTTPException
        from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise RuntimeError(
            "the web UI needs extra packages: pip install 'nostalgia-machine[web]'"
        ) from exc

    app = FastAPI(title="Nostalgia Machine", docs_url=None, redoc_url=None)
    progress_tracker = _ProgressTracker()

    @app.middleware("http")
    async def track_progress(request: Request, call_next: Callable) -> Any:
        raw_task_id = request.headers.get("x-progress-id")
        try:
            task_id = str(UUID(raw_task_id)) if raw_task_id else None
        except ValueError:
            task_id = None
        if request.method != "POST" or task_id is None:
            return await call_next(request)

        progress_tracker.start(task_id)
        request.state.progress_id = task_id
        try:
            response = await call_next(request)
        except Exception:
            progress_tracker.finish(task_id, success=False)
            raise
        progress_tracker.finish(task_id, success=response.status_code < 400)
        return response

    def report_progress(request: Request, label: str) -> None:
        progress_tracker.advance(getattr(request.state, "progress_id", None), label)

    def fail_progress(request: Request, label: str) -> None:
        progress_tracker.fail(getattr(request.state, "progress_id", None), label)

    @app.get("/api/progress/{task_id}")
    def progress_status(task_id: str) -> dict:
        try:
            normalized = str(UUID(task_id))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="progress task not found") from exc
        status = progress_tracker.get(normalized)
        if status is None:
            raise HTTPException(status_code=404, detail="progress task not found")
        return status

    def _render(html: str) -> Any:
        return HTMLResponse(html)

    @app.get("/", response_class=HTMLResponse)
    def index(skipped: str = "", built: str = "", resync: str = "") -> Any:
        with Catalog(paths.catalog) as catalog:
            try:
                state = now_playing(config, catalog, paths)
            except LookupError as exc:
                return _render(
                    _page(
                        "On now",
                        f'<div class="card"><p class="empty">{exc}</p>'
                        f'<p class="label">run: nostalgia scan &amp;&amp; nostalgia emit</p></div>',
                        active="/",
                        card=_card(config),
                    )
                )

        if state.current is None:
            current_html = '<p class="empty">Nothing scheduled right now.</p>'
        else:
            item = state.current
            # Only an episode can be skipped. A card or a commercial has seconds
            # left on the clock, and "skip to the next ad" is not a feature.
            skip_button = (
                f"""
  <form method="post" action="/skip" class="skip-form" data-progress="Replanning after the skip">
    <input type="hidden" name="slot" value="{item.slot_index}">
    <button type="submit">Skip this episode &rsaquo;</button>
  </form>
  <p class="hint">Swaps in the next episode from the same show and replans. The
  picture changes when a player next tunes in -- a live stream cannot cut into a
  session already running.</p>"""
                if item.kind == "episode"
                else ""
            )
            current_html = (
                f'<p class="label">{item.kind} &middot; {item.clock} &middot; '
                f'{item.duration_s:.0f}s</p>'
                f'<h2 class="now-title">{_h(item.title)}</h2>'
                f'<p class="now-sub">{_h(item.subtitle)}</p>'
                f"{skip_button}"
            )

        rows = "".join(
            f'<tr><td class="mono">{_h(item.clock)}</td><td>{_h(item.title)}</td>'
            f'<td class="mono">{_h(item.subtitle)}</td></tr>'
            for item in state.next_episodes
        )
        upcoming = (
            f"<table><thead><tr><th>Time</th><th>Show</th><th>Episode</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            if rows
            else '<p class="empty">No further programmes today.</p>'
        )

        flash = ""
        if skipped:
            flash = (
                f'<div class="saved">{_h(skipped)}'
                + (f" &middot; {_h(built)}" if built else "")
                + "</div>"
            )

        pending = pending_deploy(config, paths)
        body = f"""
{flash}
{pending}
<div class="card now">
  <p class="label">{state.day} &middot; {state.now_clock}</p>
  {current_html}
</div>
<div class="card">
  <p class="label">Up next</p>
  {upcoming}
</div>
<div class="card">
  <p class="label">Signal</p>
  <div class="stats">
    <span class="stat"><b>{state.fallbacks}</b>
      <span class="label">neutral fallbacks</span></span>
    <span class="stat"><b>{state.generated_share:.0%}</b>
      <span class="label">generated bumps</span></span>
  </div>
  <div class="legend">
    <span><i style="background:var(--gray)"></i>grey 75%</span>
    <span><i style="background:var(--cyan)"></i>cyan</span>
    <span><i style="background:var(--yellow)"></i>yellow</span>
    <span><i style="background:var(--magenta)"></i>magenta</span>
    <span><i style="background:var(--pluge-lo)"></i>pluge -4</span>
    <span><i style="background:var(--pluge-hi)"></i>pluge +4</span>
  </div>
</div>"""
        return _render(_page("On now", body, active="/", card=_card(config)))

    @app.post("/skip")
    def skip(request: Request, slot: str = Form(...)) -> Any:
        """Skip one episode, then replan so the stream actually changes.

        Deliberately *not* "skip whatever is on now": during a break that is a
        commercial, and "skip to the next ad" is not a feature. The page names the
        slot it meant, and this trusts that name -- the recorded ``episode_key``
        is what protects correctness, by refusing to fire if the plan has since
        moved that slot onto a different episode.
        """
        from ..skip import load_overrides as _load
        from ..skip import request_skip as _request

        report_progress(request, "Finding the episode to skip")
        try:
            target = int(slot)
        except ValueError:
            fail_progress(request, "The slot number was not valid")
            return RedirectResponse(
                "/?skipped=" + quote_plus("That is not a slot number"), status_code=303
            )

        with Catalog(paths.catalog) as catalog:
            try:
                state = now_playing(config, catalog, paths)
                plan = plan_for_day(config, catalog, paths, state.day)
            except LookupError as exc:
                fail_progress(request, "Could not load the current schedule")
                return RedirectResponse(
                    "/?skipped=" + quote_plus(str(exc)), status_code=303
                )

            playing = next(
                (item for item in plan.episode_items() if item.slot_index == target),
                None,
            )
            if playing is None:
                fail_progress(request, "That slot has no episode to skip")
                return RedirectResponse(
                    "/?skipped=" + quote_plus("That slot has no episode to skip"),
                    status_code=303,
                )
            _request(
                paths.skips,
                day=state.day,
                slot_index=target,
                episode_key=playing.asset_key,
            )
            label = f"Skipped {playing.title} {playing.subtitle}"

        # Staged rather than written live: a skip is a change to the schedule, and
        # it waits for a launch like every other change.
        message = queue_deploy(
            config,
            paths,
            progress=lambda label: report_progress(request, label),
            failure=lambda label: fail_progress(request, label),
        ).replace("saved, but", "skip saved, but")
        return RedirectResponse(
            f"/?skipped={quote_plus(label)}&built={quote_plus(message)}",
            status_code=303,
        )

    @app.post("/schedule/replan")
    def replan_schedule(request: Request) -> Any:
        """Build the settings into a staged schedule. The stream does not change."""
        current = _reload(config, paths)
        message = queue_deploy(
            current,
            paths,
            progress=lambda label: report_progress(request, label),
            failure=lambda label: fail_progress(request, label),
        )
        return RedirectResponse(
            f"/schedule?built={quote_plus(message)}", status_code=303
        )

    @app.post("/schedule/launch")
    def launch_schedule(request: Request) -> Any:
        """Swap the staged schedule in, at the end of what is on air."""
        current = _reload(config, paths)
        try:
            message = _launch(
                current,
                paths,
                progress=lambda label: report_progress(request, label),
            )
        except (LookupError, ValueError) as exc:
            fail_progress(request, "Schedule launch could not complete")
            return RedirectResponse(
                f"/schedule?error={quote_plus(str(exc))}", status_code=303
            )
        return RedirectResponse(
            f"/schedule?launched={quote_plus(message)}", status_code=303
        )

    @app.post("/schedule/cancel")
    def cancel_schedule(request: Request) -> Any:
        """Throw the staged schedule away and keep serving what is on air."""
        from ..deploy import cancel as discard

        report_progress(request, "Discarding the staged schedule")
        removed = discard(_request_deploy_paths(paths))
        noun = "file" if removed == 1 else "files"
        return RedirectResponse(
            f"/schedule?launched={quote_plus(f'pending schedule discarded ({removed} {noun})')}",
            status_code=303,
        )

    @app.get("/schedule", response_class=HTMLResponse)
    def schedule(saved: str = "", built: str = "", launched: str = "", error: str = "") -> Any:
        # Read what is on disk rather than what was loaded at startup: a save -- or
        # a hand edit -- happens after this process starts, and rendering the
        # stale values would make the page look like it ignored the change.
        current = _reload(config, paths)
        with Catalog(paths.catalog) as catalog:
            episodes = catalog.episodes()
        return _render(
            _schedule_page(
                current,
                episodes,
                card=_card(current),
                saved=saved,
                built=built,
                launched=launched,
                error=error,
                paths=paths,
            )
        )

    @app.post("/schedule")
    def save_schedule(
        request: Request,
        mode: str = Form("dayparts"),
        lineup_show: list[str] = Form(default=[]),
        lineup_order: list[str] = Form(default=[]),
        lineup_start_season: list[str] = Form(default=[]),
        lineup_start_episode: list[str] = Form(default=[]),
        lineup_start_date: list[str] = Form(default=[]),
        pinned_show: list[str] = Form(default=[]),
        pinned_season: list[str] = Form(default=[]),
        pinned_episode: list[str] = Form(default=[]),
        shuffle_shows: list[str] = Form(default=[]),
        shuffle_order: str = Form("random"),
    ) -> Any:
        report_progress(request, "Validating schedule settings")
        current = _reload(config, paths)
        # "Every show ticked" is the same instruction as "no restriction", and the
        # second survives a show being added later. Collapse it before saving.
        if set(shuffle_shows) >= set(current.shows):
            shuffle_shows = []
        try:
            form = parse_form(
                mode=mode,
                lineup_show=lineup_show,
                lineup_order=lineup_order,
                lineup_start_season=lineup_start_season,
                lineup_start_episode=lineup_start_episode,
                lineup_start_date=lineup_start_date,
                pinned_show=pinned_show,
                pinned_season=pinned_season,
                pinned_episode=pinned_episode,
                shuffle_shows=shuffle_shows,
                shuffle_order=shuffle_order,
            )
            previous = {
                entry.show_id: entry for entry in current.programming.lineup
            }
            form.lineup = [
                entry.model_copy(
                    update={
                        "start_date": (
                            None
                            if entry.start_season is None
                            else (
                                previous[entry.show_id].start_date
                                if entry.show_id in previous
                                and previous[entry.show_id].order == entry.order
                                and previous[entry.show_id].start_season
                                == entry.start_season
                                and previous[entry.show_id].start_episode
                                == entry.start_episode
                                and previous[entry.show_id].start_date is not None
                                else date.today()
                            )
                        )
                    }
                )
                for entry in form.lineup
            ]
            # Validate the whole config before writing: a saved file that cannot
            # be loaded would break the channel at the next plan, and the user
            # would have no idea which of their edits did it.
            new_config = current.model_copy(update={"programming": form.to_config()})
        except (ScheduleFormError, ValueError) as exc:
            fail_progress(request, "Schedule settings were rejected")
            return _render(
                _schedule_page(
                    current,
                    _episodes(paths),
                    card=_card(current),
                    error=str(exc),
                )
            )

        report_progress(request, "Saving the schedule settings")
        write_schedule(paths.config_dir / "programming.yaml", form)
        # Staged, not live: the schedule you can see changing is the one being
        # prepared, and it goes on air when you launch it.
        message = queue_deploy(
            new_config,
            paths,
            progress=lambda label: report_progress(request, label),
            failure=lambda label: fail_progress(request, label),
        )
        return RedirectResponse(
            f"/schedule?saved=1&built={quote_plus(message)}", status_code=303
        )

    @app.post("/bumps/placement/{family}")
    async def save_placement(family: str, request: Request) -> Any:
        if family not in SLOT_FAMILIES:
            raise HTTPException(status_code=404, detail=f"no slot family {family!r}")
        report_progress(request, "Validating continuity settings")
        form_data = await request.form()
        submitted = {
            str(key): ("" if value is None else str(value))
            for key, value in form_data.items()
        }
        try:
            form = parse_placement(family, submitted)

            import io

            from ruamel.yaml import YAML

            path = paths.config_dir / "continuity.yaml"
            reader = YAML()
            reader.preserve_quotes = True
            reader.width = 4096
            reader.indent(mapping=2, sequence=4, offset=2)
            document = reader.load(path.read_text(encoding="utf-8"))
            template = template_for(document, family)
            if template is None:
                raise ContinuityEditError(
                    f"continuity.yaml has no default-{family} template to edit"
                )
            apply_form(template, form, pod_shares=pod_shares(template))

            buffer = io.StringIO()
            reader.dump(document, buffer)
            report_progress(request, "Saving continuity settings")
            path.write_text(buffer.getvalue(), encoding="utf-8")

            # Re-validate the whole config before claiming success: a break
            # template that cannot load would break the channel at the next plan.
            updated = load_config(paths.config_dir)
        except (ContinuityEditError, ConfigError) as exc:
            fail_progress(request, "Continuity settings were rejected")
            return _render(
                _bumps_body(_reload(config, paths), paths, error=str(exc))
            )

        message = queue_deploy(
            updated,
            paths,
            progress=lambda label: report_progress(request, label),
            failure=lambda label: fail_progress(request, label),
        )
        query = f"saved={quote_plus(FAMILY_LABELS.get(family, family))}"
        if message:
            query += f"&built={quote_plus(message)}"
        return RedirectResponse(f"/bumps?{query}", status_code=303)

    @app.get("/bumps", response_class=HTMLResponse)
    def bumps(saved: str = "", built: str = "") -> Any:
        with Catalog(paths.catalog) as catalog:
            pending = pending_tags(catalog)
            shows = sorted(config.shows)

        if not pending:
            body = (
                _bumps_body(_reload(config, paths), paths, saved=saved, built=built)
                + '<div class="card"><p class="empty">Every bump has a confirmed subject.</p></div>'
            )
            return _render(_page("Bumpers", body, active="/bumps", card=_card(config)))

        positions = ("", "INTO", "OUT", "NEXT", "AFTER")
        cards = []
        for item in pending:
            chosen = item.subject or "neutral"
            options = '<option value="neutral">neutral</option>' + "".join(
                f'<option value="{_h(show)}"'
                f'{" selected" if show == chosen else ""}>{_h(show)}</option>'
                for show in shows
            )
            # The label may be a placeholder, but the *value* must be the real
            # position: an <option> with no value attribute submits its own text,
            # which is how "position? it was stored as a position.
            position_options = "".join(
                f'<option value="{value}"'
                f'{" selected" if value == (item.position or "") else ""}>'
                f"{value or 'position?'}</option>"
                for value in positions
            )
            cards.append(
                f"""<div class="card">
  <p class="label">{_h(item.filename)}</p>
  <p class="now-sub">{item.duration_s:.1f}s &middot; guess
     <b>{_h(item.subject or "unknown")}</b> &middot; from {_h(item.source)}
     &middot; {item.confidence:.0%}</p>
  <form method="post" action="/bumps/{_h(item.key)}">
    <select name="subject">{options}</select>
    <select name="position">{position_options}</select>
    <input name="era" placeholder="era" size="5" inputmode="numeric">
    <button type="submit">Confirm</button>
  </form>
</div>"""
            )
        body = (
            f'<div class="card"><p class="label">{len(pending)} awaiting confirmation</p></div>'
            + "".join(cards)
        )
        # Placement sits above the review queue: it is the setting people come
        # here for, and a queue of 600 untagged cards would bury it.
        body = (
            _bumps_body(_reload(config, paths), paths, saved=saved, built=built)
            + '<h2 class="category">Review queue</h2>'
            + body
        )
        return _render(_page("Bumpers", body, active="/bumps", card=_card(config)))

    @app.post("/bumps/{key:path}")
    def confirm(
        key: str,
        subject: str = Form(...),
        position: str = Form(""),
        era: str = Form(""),
    ) -> Any:
        try:
            apply_tag(
                Catalog(paths.catalog),
                key,
                subject=None if subject in ("neutral", "", "-") else subject,
                position=position or None,
                era=int(era) if era.strip().isdigit() else None,
            )
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/bumps", status_code=303)

    @app.get("/commercials", response_class=HTMLResponse)
    def commercials(
        all: str = "", saved: str = "", built: str = "", error: str = ""
    ) -> Any:
        current = _reload(config, paths)
        with Catalog(paths.catalog) as catalog:
            body = commercials_body(
                current,
                catalog,
                # `all=1` shows the switched-off rows too, so undoing a click
                # never requires hunting through the library list.
                show_disabled=bool(all),
                saved=saved,
                built=built,
                error=error,
            )
        return _render(_page("Commercials", body, active="/commercials", card=_card(current)))

    @app.get("/imports", response_class=HTMLResponse)
    def imports_page(
        imported: str = "", source: str = "", built: str = "", error: str = ""
    ) -> Any:
        message = (
            f"Imported {imported} clip(s) from {source}. The source video is unchanged."
            if imported
            else ""
        )
        current = _reload(config, paths)
        body = imports_web.imports_body(paths, current, error=error, message=message)
        if built:
            body += (
                f'<p class="hint">Schedule update: {_h(built)}. '
                '<a href="/schedule">Open Schedule</a>.</p>'
            )
        return _render(_page("Media import", body, active="/imports", card=_card(current)))

    @app.post("/imports/analyze")
    def analyze_import(request: Request, filename: str = Form(...)) -> Any:
        report_progress(request, "Analyzing video and detecting scene cuts")
        try:
            analysis = analyze_source(paths, filename)
        except MediaImportError as exc:
            fail_progress(request, "Video analysis failed")
            current = _reload(config, paths)
            return HTMLResponse(
                _page(
                    "Media import",
                    imports_web.imports_body(paths, current, error=str(exc)),
                    active="/imports",
                    card=_card(current),
                ),
                status_code=400,
            )
        return RedirectResponse(f"/imports/review/{analysis.token}", status_code=303)

    @app.get("/imports/source/{filename:path}")
    def import_source(filename: str) -> Any:
        try:
            source = resolve_source(paths, filename)
        except MediaImportError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        return FileResponse(source, media_type=media_type)

    @app.get("/imports/review/{token}", response_class=HTMLResponse)
    def review_import(token: str, error: str = "") -> Any:
        try:
            analysis, _ = load_analysis(paths, token)
        except MediaImportError as exc:
            current = _reload(config, paths)
            return HTMLResponse(
                _page(
                    "Media import",
                    imports_web.imports_body(paths, current, error=str(exc)),
                    active="/imports",
                    card=_card(current),
                ),
                status_code=404,
            )
        current = _reload(config, paths)
        return _render(
            _page(
                "Review video",
                imports_web.imports_body(paths, current, analysis=analysis, error=error),
                active="/imports",
                card=_card(current),
            )
        )

    @app.post("/imports/export")
    async def export_import(request: Request) -> Any:
        form = await request.form()
        token = str(form.get("token") or "")
        ad_year = str(form.get("ad_year") or "")
        bump_subject = str(form.get("bump_subject") or "neutral")
        bump_era = str(form.get("bump_era") or "")
        segment_text = str(form.get("segments") or "")
        current = _reload(config, paths)
        analysis = None
        try:
            analysis, _ = load_analysis(paths, token)
            try:
                segments = json.loads(segment_text)
            except json.JSONDecodeError as exc:
                raise MediaImportError(
                    "The segment review could not be read; please review it again."
                ) from exc
            report_progress(request, "Validating the approved segments")
            report_progress(request, "Encoding approved clips")
            exported = export_segments(
                paths,
                token,
                segments,
                ad_year=ad_year,
                bump_subject=bump_subject,
                bump_era=bump_era,
                known_subjects=set(current.shows),
            )
        except MediaImportError as exc:
            fail_progress(request, "Clip export failed")
            if analysis is None:
                return HTMLResponse(
                    _page(
                        "Media import",
                        imports_web.imports_body(paths, current, error=str(exc)),
                        active="/imports",
                        card=_card(current),
                    ),
                    status_code=400,
                )
            try:
                previous = json.loads(segment_text)
            except json.JSONDecodeError:
                previous = []
            if not isinstance(previous, list):
                previous = []
            return HTMLResponse(
                _page(
                    "Review video",
                    imports_web.imports_body(
                        paths,
                        current,
                        analysis=analysis,
                        error=str(exc),
                        prior_segments=previous,
                        ad_year=ad_year,
                        bump_subject=bump_subject,
                        bump_era=bump_era,
                    ),
                    active="/imports",
                    card=_card(current),
                ),
                status_code=400,
            )

        report_progress(request, "Refreshing the media catalog")
        from ..pipeline import scan

        try:
            with Catalog(paths.catalog) as catalog:
                scan(current, catalog, paths)
        except (OSError, sqlite3.Error, ValueError) as exc:
            fail_progress(request, "Catalog refresh failed")
            return HTMLResponse(
                _page(
                    "Media import",
                    imports_web.imports_body(
                        paths,
                        current,
                        error=(
                            f"Exported {len(exported)} clip(s), but the catalog could "
                            f"not refresh: {exc}. Run nostalgia scan."
                        ),
                    ),
                    active="/imports",
                    card=_card(current),
                ),
                status_code=500,
            )
        built = queue_deploy(
            current,
            paths,
            progress=lambda label: report_progress(request, label),
            failure=lambda label: fail_progress(request, label),
        )
        return RedirectResponse(
            f"/imports?imported={len(exported)}"
            f"&source={quote_plus(analysis.source_name)}&built={quote_plus(built)}",
            status_code=303,
        )

    @app.get("/lines", response_class=HTMLResponse)
    def lines_page(saved: str = "", error: str = "") -> Any:
        current = _reload(config, paths)
        body = lines_web.lines_body(current, paths, saved=saved, error=error)
        return _render(_page("Lines", body, active="/lines", card=_card(current)))

    @app.post("/lines/{action}")
    async def lines_action(action: str, request: Request) -> Any:
        """Generate, approve or reject bump lines.

        Every outcome is a redirect back to the page with a sentence, including
        failures: the model being offline is an ordinary state, not a server error.
        """
        form_data = await request.form()
        current = _reload(config, paths)
        try:
            if action == "generate":
                message = lines_web.generate(
                    current,
                    paths,
                    position=str(form_data.get("position") or "NEXT"),
                    show=str(form_data.get("show") or "") or None,
                    count=int(str(form_data.get("count") or "12")),
                )
            elif action == "approve":
                message = lines_web.approve(paths, str(form_data.get("id") or ""))
            elif action == "approve-all":
                message = lines_web.approve_all(paths)
            elif action == "reject":
                message = lines_web.reject(paths, str(form_data.get("id") or ""))
            elif action == "clear":
                message = lines_web.clear(paths)
            else:
                raise HTTPException(status_code=404, detail=f"no action {action!r}")
        except HTTPException:
            raise
        except (LookupError, ValueError, lines_web.bl.ModelError) as exc:
            return RedirectResponse("/lines?error=" + quote_plus(str(exc)), status_code=303)
        return RedirectResponse("/lines?saved=" + quote_plus(message), status_code=303)

    @app.post("/commercials/family/{action}")
    async def toggle_family(action: str, request: Request) -> Any:
        """Switch a whole compilation off (or back on) and replan.

        The unit that goes bad is usually the *recording*, not the spot: a worn tape
        or a station logo in the corner is shared by every slice of it.
        """
        if action not in ("disable", "enable"):
            raise HTTPException(status_code=404, detail=f"no action {action!r}")
        report_progress(request, "Updating commercial availability")
        form_data = await request.form()
        family = str(form_data.get("family") or "")
        if not family:
            fail_progress(request, "No commercial compilation was selected")
            return RedirectResponse(
                "/commercials?error=" + quote_plus("No compilation was named"), status_code=303
            )
        with Catalog(paths.catalog) as catalog:
            try:
                message = set_family_enabled(catalog, family, enabled=action == "enable")
            except LookupError as exc:
                fail_progress(request, "The selected compilation was not found")
                return RedirectResponse(
                    "/commercials?error=" + quote_plus(str(exc)), status_code=303
                )
        built = queue_deploy(
            config,
            paths,
            progress=lambda label: report_progress(request, label),
            failure=lambda label: fail_progress(request, label),
        )
        return RedirectResponse(
            f"/commercials?saved={quote_plus(message)}&built={quote_plus(built)}",
            status_code=303,
        )

    @app.post("/commercials/{action}")
    async def toggle_spot(action: str, request: Request) -> Any:
        """Switch one spot off (or back on) and replan.

        Replans immediately rather than waiting for the scheduler: the point of the
        page is to fix a break you just heard, and a click that leaves the channel
        unchanged for the next six hours reads as broken.
        """
        if action not in ("disable", "enable"):
            raise HTTPException(status_code=404, detail=f"no action {action!r}")
        report_progress(request, "Updating commercial availability")
        form_data = await request.form()
        key = str(form_data.get("key") or "")
        if not key:
            fail_progress(request, "No commercial file was selected")
            return RedirectResponse(
                "/commercials?error=" + quote_plus("No file was named"), status_code=303
            )

        with Catalog(paths.catalog) as catalog:
            try:
                message = set_spot_enabled(catalog, key, enabled=action == "enable")
            except LookupError as exc:
                fail_progress(request, "The selected commercial was not found")
                return RedirectResponse(
                    "/commercials?error=" + quote_plus(str(exc)), status_code=303
                )

        built = queue_deploy(
            config,
            paths,
            progress=lambda label: report_progress(request, label),
            failure=lambda label: fail_progress(request, label),
        )
        return RedirectResponse(
            f"/commercials?saved={quote_plus(message)}&built={quote_plus(built)}",
            status_code=303,
        )

    @app.get("/config", response_class=HTMLResponse)
    def config_page(
        saved: str = "", built: str = "", error: str = "", launched: str = ""
    ) -> Any:
        return _render(
            _config_page(
                _reload(config, paths),
                paths,
                card=_card(config),
                saved=saved.split("|") if saved else None,
                built=built,
                error=error,
                launched=launched,
            )
        )

    @app.post("/config")
    async def save_config(request: Request) -> Any:
        """Write the whole page in one submission.

        One button, one namespace (``<slug>::<key>``), one round of validation over
        the result. Only the files whose fields actually changed are rewritten, so
        a save cannot disturb a file the user did not touch -- which is what the
        per-file buttons used to guarantee, now without asking the user to remember
        which file a setting lives in.
        """
        from .config_edit import save_all

        report_progress(request, "Validating and saving settings")
        form = await request.form()
        submitted = {
            str(key): ("" if value is None else str(value)) for key, value in form.items()
        }
        before = _snapshot(paths, [entry for entry in SETTINGS])
        try:
            written = save_all(paths.config_dir, submitted)
            # Re-validate the *whole* config before claiming success: a day file
            # claiming a weekday another file also claims is only wrong in
            # combination, and a saved config that cannot load would break the
            # channel at the next plan with no clue which edit did it.
            updated = load_config(paths.config_dir)
        except ConfigEditError as exc:
            _restore(paths, before)
            fail_progress(request, "Settings were rejected")
            return _render(
                _config_page(_reload(config, paths), paths, card=_card(config), error=str(exc))
            )
        except ConfigError as exc:
            _restore(paths, before)
            fail_progress(request, "Settings would leave the config invalid")
            return _render(
                _config_page(
                    _reload(config, paths),
                    paths,
                    card=_card(config),
                    error=f"That would leave the config invalid, so nothing was saved:\n{exc}",
                )
            )
        except OSError as exc:
            _restore(paths, before)
            fail_progress(request, "Settings could not be saved")
            return _render(
                _config_page(
                    _reload(config, paths),
                    paths,
                    card=_card(config),
                    error=f"Settings could not be saved; no changes were kept: {exc}",
                )
            )

        names = [f"{title} ({len(changed)})" for title, changed in written]
        if written:
            report_progress(request, "Preparing a schedule from the updated settings")
            message = queue_deploy(
                updated,
                paths,
                progress=lambda label: report_progress(request, label),
                failure=lambda label: fail_progress(request, label),
            )
        else:
            message = ""
        query = f"saved={quote_plus('|'.join(names))}" if names else ""
        if message:
            query += ("&" if query else "") + f"built={quote_plus(message)}"
        return RedirectResponse(f"/config?{query}", status_code=303)

    @app.get("/api/now")
    def api_now() -> dict:
        with Catalog(paths.catalog) as catalog:
            state = now_playing(config, catalog, paths)
        return {
            "day": state.day.isoformat(),
            "clock": state.now_clock,
            "current": None if state.current is None else asdict(state.current),
            "next": [asdict(item) for item in state.next_episodes],
            "fallbacks": state.fallbacks,
            "generated_share": state.generated_share,
        }

    return app


def _reload(config: RootConfig, paths: Paths) -> RootConfig:
    """Re-read ``config/`` so a page reflects edits made since startup.

    Falls back to the startup config if the files are mid-write or briefly
    invalid: a stale page is much better than a crashed one.
    """
    from ..config import ConfigError, load_config

    try:
        return load_config(paths.config_dir)
    except ConfigError:
        return config


def _request_deploy_paths(paths: Paths):
    from ..deploy import Roots

    return Roots.of(paths.playout_dir, paths.xmltv_dir)


def queue_deploy(
    config: RootConfig,
    paths: Paths,
    *,
    progress: Callable[[str], None] | None = None,
    failure: Callable[[str], None] | None = None,
) -> str:
    """Stage the whole horizon, ready to launch. Returns a sentence for the page.

    Never raises: the settings are already saved, and saying both halves is the
    honest report. The live schedule is untouched either way, which is the point.
    """
    from ..deploy import fingerprint

    try:
        if progress is not None:
            progress("Opening the catalog and preparing the schedule")
        with Catalog(paths.catalog) as catalog:
            result = build(
                config,
                catalog,
                paths,
                deploy=True,
                progress=progress,
            )
    except Exception as exc:  # noqa: BLE001 - surfaced to the page
        if failure is not None:
            failure("Staging the new schedule failed")
        elif progress is not None:
            progress("Staging the new schedule failed")
        return f"saved, but staging the new schedule failed: {exc}"
    if progress is not None:
        progress("Schedule staged and ready to launch")
    return (
        f"{len(result.plans)} day(s) staged with {fingerprint(config)}"
        " -- launch it when you want it on air"
    )


def _launch(
    config: RootConfig,
    paths: Paths,
    *,
    progress: Callable[[str], None] | None = None,
) -> str:
    """Rebuild the staged horizon, then swap it in. Raises only on unusable input.

    Rebuilt first on purpose: the files on disk were built from the settings at the
    time, and a launch is the one moment where "what is live" is about to change, so
    it should reflect the settings as they are now.
    """
    from ..deploy import launch as push
    from ..deploy import missing_inputs

    roots = _request_deploy_paths(paths)
    with Catalog(paths.catalog) as catalog:
        if progress is not None:
            progress("Rebuilding the latest schedule")
        build(config, catalog, paths, deploy=True, progress=progress)
    if progress is not None:
        progress("Checking the staged media files")
    gone = missing_inputs(roots)
    if gone:
        raise LookupError(f"{len(gone)} file(s) the staged schedule needs are missing; press Replan")
    if progress is not None:
        progress("Switching the live schedule at a safe boundary")
    report = push(roots)
    from ..grid import format_clock

    zone = __import__("zoneinfo").ZoneInfo(config.channel.grid.timezone)
    at = format_clock(report.swap_ms, config.channel.grid)
    return f"launched {report.days} day(s) at {at}; {report.note}"


def _snapshot(paths: Paths, entries: Sequence[SettingsFile]) -> dict[Path, bytes | None]:
    """The current bytes of every file the form can write, so a bad save can be undone.

    Validation runs *after* writing, because a rule like "a weekday may only appear
    in one file" spans files and can only be judged on the whole document. That
    meant a rejected submission used to leave the edited file on disk; this is what
    makes "nothing was saved" true.
    """
    snapshot: dict[Path, bytes | None] = {}
    for entry in entries:
        path = paths.config_dir / entry.path
        snapshot[path] = path.read_bytes() if path.is_file() else None
    return snapshot


def _restore(paths: Paths, snapshot: dict[Path, bytes | None]) -> None:
    from .config_edit import _write_atomic

    for path, content in snapshot.items():
        if content is None:
            path.unlink(missing_ok=True)
        elif not path.is_file() or path.read_bytes() != content:
            _write_atomic(path, content)


def _episodes(paths: Paths) -> list[EpisodeRef]:
    with Catalog(paths.catalog) as catalog:
        return catalog.episodes()


_ORDERS = (
    ("random", "random"),
    ("by_season", "by season"),
    ("sequential", "sequential"),
)


def _order_options(selected: str) -> str:
    return "".join(
        f'<option value="{value}"{" selected" if value == selected else ""}>'
        f"{label}</option>"
        for value, label in _ORDERS
    )


def pending_deploy(config: RootConfig, paths: Paths) -> str:
    """The "schedule changes pending" notice, or "" when nothing is waiting.

    Shows what is staged and when it would take over, with the two actions that
    matter: launch it, or throw it away. ``launch`` rebuilds before swapping, so the
    time quoted here is an estimate of the seam, not a promise.
    """
    from ..deploy import clock as at_clock
    from ..deploy import (
        fingerprint,
        intervals,
        load,
        missing_inputs,
        plan_switch,
        preview_schedule,
    )
    import json as _json

    roots = _request_deploy_paths(paths)
    deploy = load(roots)
    if deploy is None:
        return ""

    zone = __import__("zoneinfo").ZoneInfo(config.channel.grid.timezone)
    switch = deploy.switch_at_ms
    try:
        from ..deploy import live_day_for

        today = live_day_for(roots, deploy.staged_at_ms)
        if today is not None:
            spans = intervals(_json.loads(today.read_text(encoding="utf-8")))
            horizon = spans[-1].finish_ms if spans else None
            switch = plan_switch(spans, deploy.staged_at_ms, horizon)
    except (OSError, ValueError):
        pass

    stale = deploy.fingerprint and deploy.fingerprint != fingerprint(config)
    gone = missing_inputs(roots)
    try:
        preview = preview_schedule(roots)
        total_changes = preview.added + preview.removed + preview.changed
        if preview.compared_days:
            preview_summary = (
                f"Compared {preview.compared_days} day(s) with the live schedule: "
                f"{preview.changed} replaced, {preview.added} added, "
                f"{preview.removed} removed."
            )
        else:
            preview_summary = (
                f"No matching live days; {preview.added} staged item(s) are new."
            )
        preview_details = (
            "<ul>"
            + "".join(f"<li>{_h(detail)}</li>" for detail in preview.details)
            + "</ul>"
            if preview.details
            else "<p>No item-level differences found.</p>"
        )
        if total_changes > len(preview.details):
            preview_details += (
                f"<p>{total_changes - len(preview.details)} more change(s) not shown.</p>"
            )
        preview_html = (
            '<details class="schedule-preview"><summary>Preview staged changes</summary>'
            f"<p>{_h(preview_summary)}</p>{preview_details}</details>"
        )
    except LookupError as exc:
        preview_html = (
            f'<p class="warn">Schedule preview unavailable: {_h(str(exc))}</p>'
        )
    notes = []
    if stale:
        notes.append("Settings have changed since this was prepared; launching rebuilds it first.")
    if gone:
        notes.append(f"{len(gone)} file(s) it needs have gone missing; press Replan again.")
    detail = " ".join(notes)

    return f"""<div class="card pending">
  <p class="label">Schedule changes pending</p>
  <p><b>{len(deploy.days)} day(s)</b> staged, ready to go on air at
  <b>{_h(at_clock(switch, zone))}</b> &mdash; the end of the programme on air now.</p>
  <form method="post" action="/schedule/launch" class="inline" data-progress="Rebuilding and launching the schedule">
    <button type="submit">Launch new schedule</button>
  </form>
  <form method="post" action="/schedule/cancel" class="inline" data-progress="Discarding the staged schedule">
    <button type="submit">Discard</button>
  </form>
  {preview_html}
  {f'<p class="warn">{_h(detail)}</p>' if detail else ''}
  <p class="hint">The stream keeps playing the old schedule until the launch. The
  switch finishes the programme that is on air, so it never cuts mid-episode.</p>
</div>"""


def _schedule_page(
    config: RootConfig,
    episodes: Sequence[EpisodeRef],
    *,
    card: str,
    saved: str = "",
    built: str = "",
    error: str = "",
    launched: str = "",
    paths: Paths | None = None,
) -> str:
    """The schedule editor.

    Everything the page needs is derived here: the roster comes from
    ``shows.yaml`` and the per-show episode counts come from the catalog, so a
    show with nothing downloaded is visibly unplayable rather than silently
    dropped when the plan is built.
    """
    shows = sorted(config.shows)
    counts = Counter(episode.show_id for episode in episodes)
    form = load_schedule(config)

    # When everything is ticked the selection is not a *choice*, it is the default,
    # so store "no restriction" and keep the roster open to shows added later.
    shuffle_all = len(form.shuffle_shows) == len(shows)

    def label(show_id: str) -> str:
        title = config.shows[show_id].title if show_id in config.shows else show_id
        return f"{title} ({counts.get(show_id, 0)})"

    def pick(selected: str, blank: str = "") -> str:
        head = f'<option value="">{_h(blank)}</option>' if blank else ""
        return head + "".join(
            f'<option value="{_h(show)}"{" selected" if show == selected else ""}>'
            f"{_h(label(show))}</option>"
            for show in shows
        )

    def lineup_row(
        show_id: str = "",
        order: str = "random",
        start_season: int | None = None,
        start_episode: int | None = None,
        start_date: str = "",
    ) -> str:
        return (
            '<div class="row lineup-row" data-lineup-row>'
            '<select name="lineup_show" aria-label="Show">'
            f'{pick(show_id, "— choose a show —")}</select>'
            '<select name="lineup_order" aria-label="Episode order">'
            f'{_order_options(order)}</select>'
            f'<input class="num" type="number" min="0" name="lineup_start_season" '
            f'value="{_h("" if start_season is None else start_season)}" '
            'placeholder="Season" aria-label="Start season">'
            f'<input class="num" type="number" min="0" name="lineup_start_episode" '
            f'value="{_h("" if start_episode is None else start_episode)}" '
            'placeholder="Episode" aria-label="Start episode">'
            f'<input type="hidden" name="lineup_start_date" value="{_h(start_date)}">'
            '<button type="button" class="secondary" data-lineup-remove>Remove</button>'
            "</div>"
        )

    def lineup_rows() -> str:
        rows = [
            lineup_row(
                entry.show_id,
                entry.order,
                entry.start_season,
                entry.start_episode,
                entry.start_date.isoformat() if entry.start_date else "",
            )
            for entry in form.lineup
        ]
        if not rows:
            rows.append(lineup_row())
        return "".join(rows)

    lineup_template = lineup_row()

    def pinned_rows() -> str:
        rows = []
        for key in form.pinned:
            show_id, _, code = key.partition(":")
            season = episode = ""
            if code.startswith("s") and "e" in code:
                raw_season, _, raw_episode = code[1:].partition("e")
                if raw_season.isdigit() and raw_episode.isdigit():
                    season, episode = str(int(raw_season)), str(int(raw_episode))
            rows.append(
                '<div class="row">'
                f'<select name="pinned_show">{pick(show_id)}</select>'
                f'<input class="num" name="pinned_season" value="{_h(season)}" '
                'placeholder="S" inputmode="numeric">'
                f'<input class="num" name="pinned_episode" value="{_h(episode)}" '
                'placeholder="E" inputmode="numeric">'
                "</div>"
            )
        rows.append(
            '<div class="row">'
            f'<select name="pinned_show">{pick("", "— pin an episode —")}</select>'
            '<input class="num" name="pinned_season" placeholder="S" inputmode="numeric">'
            '<input class="num" name="pinned_episode" placeholder="E" inputmode="numeric">'
            "</div>"
        )
        return "".join(rows)

    # Rendered exactly as configured. An empty list already means "the whole
    # roster", so pre-ticking everything would rewrite the file with every show
    # named and quietly turn "anything" into a frozen list.
    selected_shows = set(shows) if shuffle_all else set(form.shuffle_shows)
    shuffle_boxes = "".join(
        f'<label><input type="checkbox" name="shuffle_shows" value="{_h(show)}"'
        f'{" checked" if show in selected_shows else ""}>'
        f"<span>{_h(label(show))}</span></label>"
        for show in shows
    )

    empty = [show for show in shows if counts.get(show, 0) == 0]
    warnings = (
        f'<p class="warn">Nothing downloaded for: {_h(", ".join(empty))}. '
        f"Those shows cannot air until media is added.</p>"
        if empty
        else ""
    )

    banner = ""
    if launched:
        banner = f'<div class="saved">{_h(launched)}</div>'
    if error:
        banner = f'<div class="saved bad">{_h(error)}</div>'
    elif saved:
        banner = (
            f'<div class="saved">Schedule saved &middot; {_h(built or "not staged")}'
            f"</div>"
        )

    def mode_radio(value: str, text: str) -> str:
        checked = " checked" if form.mode == value else ""
        return (
            f'<label><input type="radio" name="mode" value="{value}"{checked}>'
            f"<span>{text}</span></label>"
        )

    pending = pending_deploy(config, paths) if paths is not None else ""
    body = f"""
{banner}
{pending}
<div class="card">
  <form method="post" action="/schedule" data-progress="Saving and staging the schedule">
    <p class="label">Programming mode</p>
    <div class="modes">
      {mode_radio("dayparts", "Day files")}
      {mode_radio("lineup", "Lineup")}
      {mode_radio("shuffle", "Shuffle all")}
    </div>
    {warnings}
    <div class="panel {"show" if form.mode == "dayparts" else ""}" data-mode="dayparts">
      <p class="hint">The hand-written day files in <code>config/days/</code>
      decide what airs and when. This is the mode that reproduces a real
      broadcast block: set start times, dayparts and per-daypart shows, and the
      planner fills each slot from that show's rotation.</p>
    </div>

    <div class="panel {"show" if form.mode == "lineup" else ""}" data-mode="lineup">
      <p class="hint">Add one row per show; each show takes one turn per pass.
      Add or remove rows before saving. Slots are sized
      from each show's own <code>slot</code> family, so a 22-minute show gets a
      30-minute slot and an 11-minute show gets 15. <code>by season</code>
      weighs seasons equally. For <code>sequential</code>, set a start season and
      optionally an episode; it continues from there across days. Leave both blank
      to continue from aired history (or start at the first episode). Random ignores
      the start fields.</p>
      <p class="label">Lineup</p>
      <div id="lineup-rows">{lineup_rows()}</div>
      <template id="lineup-row-template">{lineup_template}</template>
      <button type="button" class="secondary" data-lineup-add>Add another show</button>
      <p class="label" style="margin-top:16px">Pinned episodes</p>
      <p class="hint">Played verbatim, in this order, then the cycle repeats.</p>
      {pinned_rows()}
    </div>

    <div class="panel {"show" if form.mode == "shuffle" else ""}" data-mode="shuffle">
      <p class="hint">A continuous stream from the whole collection. Each show
      takes one turn per pass, in id order &mdash; deliberately not weighted by
      episode count, or a ten-season show would swallow the channel. Untick
      everything to mean the whole roster.</p>
      <p class="label">Shows in the shuffle</p>
      <div class="shows">{shuffle_boxes}</div>
      <p class="hint" style="margin-top:8px">None ticked means the whole roster,
      including shows added later.</p>
      <p class="label" style="margin-top:16px">Episode order within a show</p>
      <div class="row">
        <select name="shuffle_order">{_order_options(form.shuffle_order)}</select>
      </div>
    </div>

    <p style="margin-top:18px"><button type="submit">Save &amp; stage</button></p>
  </form>
</div>

<div class="card">
  <p class="label">How this is stored</p>
  <p class="hint">Saved to <code>config/programming.yaml</code>, so the schedule
  is diffable and survives a wiped catalog. Saving stages a new schedule; launch it
  from the notice on this page when you want it on air.
  immediately: playout files and the guide are rewritten in place.</p>
</div>
"""
    return _page("Schedule", body, active="/schedule", card=card)


def _config_page(
    config: RootConfig,
    paths: Paths,
    *,
    card: str,
    current: str = "",
    saved: list[str] | None = None,
    built: str = "",
    error: str = "",
    launched: str = "",
) -> str:
    """The settings editor: every editable field in every file, grouped by purpose.

    One long page rather than tabs, because the settings are few enough to scan
    and hiding half of them behind a click makes "what is this channel set to"
    into a hunt. Each file is a panel with its own save button, so a mistake in
    one file cannot block saving another.
    """
    body: list[str] = []
    if launched:
        body.append(f'<div class="saved">{_h(launched)}</div>')
    if error:
        body.append(f'<div class="saved bad">{_h(error)}</div>')
    elif saved:
        body.append(
            f'<div class="saved">Saved {_h(", ".join(saved))}'
            + (f" &middot; {_h(built)}" if built else "")
            + "</div>"
        )
    body.append(pending_deploy(config, paths))

    # One form for every file. The save button sits at the top where a long page
    # needs it, and again at the bottom so the last field is not a discouraging
    # scroll away from the control.
    main: list[str] = []
    advanced: list[str] = []
    previous = None
    for entry in SETTINGS:
        shown = [s for s in entry.sections if not s.advanced]
        tucked = [s for s in entry.sections if s.advanced]
        if shown:
            if entry.category != previous:
                main.append(f'<h2 class="category">{_h(entry.category)}</h2>')
                previous = entry.category
            main.append(_settings_panel(entry, shown, paths))
        if tucked:
            if entry.category != previous:
                advanced.append(f'<h2 class="category">{_h(entry.category)}</h2>')
                previous = entry.category
            advanced.append(_settings_panel(entry, tucked, paths, advanced=True))

    main_html = "".join(main)
    advanced_html = "".join(advanced)
    advanced_block = (
        f"""
<details class="advanced">
  <summary>Advanced options <span class="dim">({len(advanced)} group(s))</span></summary>
  <p class="hint">Tuning for how a break is assembled and how the picture is
  drawn. The defaults are right for most channels; change these when something
  specific is wrong.</p>
  {advanced_html}
</details>"""
        if advanced_html
        else ""
    )

    save_button = '<button type="submit">Save changes</button>'
    body.append(
        f"""
<form method="post" action="/config" class="settings-form" novalidate data-progress="Saving and validating settings">
  <div class="save-bar">
    <p class="label">Settings</p>
    {save_button}
  </div>
  <p class="hint">Every option writes straight back to the YAML in
  <code>config/</code>; comments and anything not shown here are left untouched,
  so the files stay hand-editable. Saving prepares a new schedule without
  touching the stream.</p>
  {main_html}
  {advanced_block}
  <div class="save-bar bottom">{save_button}</div>
</form>"""
    )
    return _page("Config", "".join(body), active="/config", card=card)


def _settings_panel(
    entry: SettingsFile,
    sections: Sequence[Section],
    paths: Paths,
    *,
    advanced: bool = False,
) -> str:
    """One file's worth of settings, as a group inside the single form.

    No save button and no ``<form>`` of its own: the page saves everything at
    once, so a panel is only namespaced fields. The file each group came from is
    named once at the bottom, because the page is grouped by *intent* and a reader
    editing three files at once should still be able to find them by hand.
    """
    values = read_values(entry, paths.config_dir)
    field_html = "".join(
        _form_section(section, values, entry) for section in sections
    )
    return f"""
<div class="panel-block settings{' tucked' if advanced else ''}">
  <p class="label">{_h(entry.title)}</p>
  <p class="hint">{_h(entry.purpose)}</p>
  {field_html}
  <p class="hint filename"><code>config/{_h(entry.filename)}</code></p>
</div>"""


def _form_section(section, values, entry: SettingsFile) -> str:
    rows = "".join(_form_field(spec, values.get(spec.key), entry) for spec in section.fields)
    note = f'<p class="hint">{_h(section.note)}</p>' if section.note else ""
    return f"""
<div class="settings-group">
  <p class="group-title">{_h(section.title)}</p>
  {note}
  {rows}
</div>"""


def _form_field(spec, value, entry: SettingsFile) -> str:
    """One labelled control, drawn from its declared type.

    Element ids are prefixed with the file slug as well: duplicate ids make the
    second label focus the first input, so clicking "Bump era tolerance" would
    edit the commercial one.
    """
    # The whole page is one form, so every control carries its file's slug: the
    # same key appears in more than one file (`era_drift_years` is set separately
    # for commercials and for cards), and without the prefix the two would post
    # the same name and one would silently win.
    name = _h(f"{entry.slug}::{spec.key}")
    field_id = _h(f"{entry.slug}--{spec.key}")
    help_html = f'<p class="field-help">{_h(spec.help)}</p>' if spec.help else ""

    if spec.kind == BOOL:
        checked = " checked" if value else ""
        control = (
            f'<input type="checkbox" name="{name}" value="1" id="{field_id}"{checked}>'
        )
        return f"""
<div class="field check">
  <label for="{field_id}">{control}<span>{_h(spec.label)}</span></label>
  {help_html}
</div>"""

    if spec.kind == CHOICE:
        options = "".join(
            f'<option value="{_h(choice)}"{" selected" if choice == value else ""}>'
            f"{_h(label)}</option>"
            for choice, label in spec.choices
        )
        control = f'<select name="{name}" id="{field_id}">{options}</select>'
    elif spec.kind == LIST:
        shown = " ".join(str(item) for item in (value or []))
        control = (
            f'<input type="text" name="{name}" id="{field_id}" '
            f'value="{_h(shown)}" placeholder="{_h(spec.placeholder or "—")}">'
        )
    else:
        # No `step`: it is a spinner hint, and a value off the step (12.75 against
        # 0.5) makes the *whole form* refuse to submit with "an invalid form
        # control ... is not focusable" -- which read as a dead save button. The
        # server checks the real bounds below, in `Field.coerce`.
        bump = ""
        if spec.minimum is not None:
            bump += f' min="{spec.minimum:g}"'
        if spec.maximum is not None:
            bump += f' max="{spec.maximum:g}"'
        kind = "number" if spec.kind in (INT, FLOAT) else "text"
        placeholder = spec.placeholder or ("—" if spec.kind == DAY else "")
        control = (
            f'<input type="{kind}" name="{name}" id="{field_id}" '
            f'value="{_h("" if value is None else value)}"{bump} '
            f'placeholder="{_h(placeholder)}">'
        )

    return f"""
<div class="field">
  <label for="{field_id}">{_h(spec.label)}</label>
  {control}
  {help_html}
</div>"""


def _bumps_body(
    config: RootConfig,
    paths: Paths,
    *,
    saved: str = "",
    error: str = "",
    built: str = "",
) -> str:
    """The placement panels as a fragment, for the Bumpers page to own.

    Returns a fragment rather than a whole document: the Bumpers page already has
    a shell and a review queue, and returning a full page here produced two
    headers and two test cards stacked on top of each other.
    """
    """How many continuity cards air, and where.

    The file behind this is ``continuity.yaml``, whose real shape is a flat list of
    interleaved bump / pod / episode segments. That is right for the planner and
    wrong for a person, so this page asks the question people actually have --
    "how many cards around the show, and where?" -- and :mod:`continuity_edit`
    translates.
    """
    document = _continuity_document(paths)
    forms = read_all(document)

    body = [
        '<div class="panel-block">',
        '<p class="label">Bump placement</p>',
        '<p class="hint">How many [adult swim] continuity cards air around each '
        "programme, and exactly where. One panel per slot length: the channel "
        "picks the panel that matches how long the show's slot is. "
        "<code>Before</code> and <code>After</code> and <code>Before the next</code> "
        "are the three standard points; a mid-break adds a pair either side of a "
        "commercial pod in the middle of the episode.</p>",
        "</div>",
    ]
    if error:
        body.append(f'<div class="saved bad">{_h(error)}</div>')
    elif saved:
        body.append(
            f'<div class="saved">Saved {_h(saved)}'
            + (f" &middot; {_h(built)}" if built else "")
            + "</div>"
        )

    for form in forms:
        body.append(_bumps_panel(form))

    body.append(
        '<div class="panel-block"><p class="label">Why these numbers matter</p>'
        '<p class="hint">A card must fit its slot or it is skipped, so raising a '
        "count does not guarantee it airs -- the break has to have room. The "
        "<code>Capped at</code> length is what stops a 15-second card being rejected: "
        "the classic [adult swim] text bumps run 15.0-15.4s, so anything below 16 "
        "silently drops most of the archive.</p></div>"
    )
    return "".join(body)


def _bumps_panel(form) -> str:
    family = form.family
    rows = [
        _count_row(f"{family}.into", "Before the show starts", form.into,
                   "The card that runs as the programme comes back from a break."),
        _count_row(f"{family}.after", "After the show ends", form.after,
                   "\"We now return to…\" style, immediately after the episode."),
        _count_row(f"{family}.next", "Before the next show", form.next,
                   "The closing \"up next\" card. Only an up-next card can close a slot."),
    ]
    mid_rows = []
    for index in range(MAX_MID):
        mid = form.mid[index] if index < len(form.mid) else None
        mid_rows.append(_mid_row(family, index, mid))

    return f"""
<div class="panel-block settings">
  <p class="label">{_h(FAMILY_LABELS.get(family, family))}</p>
  <p class="hint">{form.total_cards()} card(s) per slot across all points.</p>
  <form method="post" action="/bumps/placement/{_h(family)}" data-progress="Saving continuity settings">
    {''.join(rows)}
    <div class="field narrow">
      <label for="f-{family}-max">Capped at (seconds)</label>
      <input type="number" id="f-{family}-max" name="{family}.max_seconds"
             value="{form.max_seconds:g}" min="2" max="60" step="0.5">
      <p class="field-help">A card longer than this is skipped rather than cut short.
      16s covers the whole classic archive.</p>
    </div>
    <div class="mid-heading">
      <p class="group-title">Mid-break</p>
      <p class="hint">A mid-roll splits the episode and returns to it. Leave the counts
      at 0 for no mid-break.</p>
    </div>
    {''.join(mid_rows)}
    <button type="submit">Save {_h(family)} slots</button>
  </form>
  <p class="hint filename"><code>config/continuity.yaml</code> &rarr; default-{_h(family)}</p>
</div>"""


def _count_row(name: str, label: str, value: int, help_text: str) -> str:
    options = "".join(
        f'<option value="{n}"{" selected" if n == value else ""}>{n}</option>'
        for n in range(0, 5)
    )
    return f"""
<div class="field">
  <label for="f-{name}">{_h(label)}</label>
  <select name="{name}" id="f-{name}" style="max-width:120px">{options}</select>
  <p class="field-help">{_h(help_text)}</p>
</div>"""


def _mid_row(family: str, index: int, mid) -> str:
    out = mid.out if mid else 0
    into = mid.into if mid else 0
    percent = mid.percent if mid else 50.0
    pod = mid.pod_seconds if mid else 60

    def pick(name: str, value: int) -> str:
        return (
            f'<select name="{name}" style="max-width:90px">'
            + "".join(
                f'<option value="{n}"{" selected" if n == value else ""}>{n}</option>'
                for n in range(0, 4)
            )
            + "</select>"
        )

    return f"""
<div class="mid-row">
  <span class="mid-label">Mid-break {index + 1}</span>
  <label>{pick(f'{family}.mid{index}.out', out)}<span>going in</span></label>
  <label>{pick(f'{family}.mid{index}.into', into)}<span>coming back</span></label>
  <label><input type="number" name="{family}.mid{index}.percent" value="{percent:g}"
         min="5" max="95" step="1" style="width:74px"><span>% into the episode</span></label>
  <label><input type="number" name="{family}.mid{index}.pod" value="{pod}"
         min="15" max="300" step="15" style="width:74px"><span>s of ads</span></label>
</div>"""


def _continuity_document(paths: Paths):
    from ruamel.yaml import YAML

    path = paths.config_dir / "continuity.yaml"
    reader = YAML()
    reader.preserve_quotes = True
    reader.width = 4096
    reader.indent(mapping=2, sequence=4, offset=2)
    if not path.is_file():
        return {}
    try:
        return reader.load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - an unreadable file must not break the page
        return {}


def _card(config: RootConfig) -> str:
    """Which test pattern to draw: ``channel.test_card`` in channel.yaml."""
    mode = getattr(config.channel, "test_card", "ntsc")
    return "pal" if str(mode).lower() == "pal" else "ntsc"


def _h(value: object) -> str:
    """HTML-escape, since titles come from filenames and user config."""
    from html import escape

    return escape(str(value), quote=True)


__all__ = ["create_app"]

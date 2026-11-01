"""The commercials page: what aired, and turning off the bad ones.

Commercial playback artefacts are not evenly distributed. The breaks that lurch
are almost always the same handful of files -- a recorded break catalogued as one
long "spot", or a clip whose audio and picture are already damaged -- and there is
no automatic way to find them. Scene-cut counts and silence detection were both
tried and neither separates a 70-second recorded break from a 70-second single
spot, so the only reliable detector is a person watching the channel.

This page exists so that act costs one click instead of a CLI invocation. It reads
the ``airings`` table (written by :func:`nostalgia.pipeline.build` from what has
actually aired) and writes a flag on the media row; :meth:`Catalog.assets` filters
on that flag, so a disabled file leaves the pool at the very next plan with no
rescan.

Kept out of :mod:`nostalgia.web.app` so the routing module stays readable, and out
of :mod:`nostalgia.web.view` because that module is the testable read model and
this one is presentation.
"""

from __future__ import annotations

import re
from html import escape
from datetime import datetime

from ..catalog import Catalog
from ..config import RootConfig

#: A file longer than the longest pod the channel books can never play whole: it
#: will be trimmed, or it will steal the budget from the pods after it. That is
#: arithmetic, not a guess, so it is the one hint stated as a fact.
MULTI_SPOT_HINT_MS = 90_000

#: A file longer than the *shortest* pod may still be trimmed, but only when it
#: lands in a short pod -- a 45 s spot plays whole in a 60 s break. Listed as
#: "worth a look" rather than "cannot play whole", because the difference matters:
#: the first is a claim about the file, the second about where it landed.
SHORT_POD_HINT_MS = 30_000

#: How many airings the history lists. The point of the page is "which of the
#: spots that just played was the bad one", which is a question about the last few
#: minutes -- a whole day of history is a wall of rows nobody reads, and the only
#: rows worth having are the ones still in earshot. Twenty covers roughly an
#: hour of commercials.
AIRING_LIMIT = 20


def _h(value: object) -> str:
    return escape(str(value), quote=True)


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.1f}s"


def _when(aired_ms: int, timezone: str) -> str:
    """Wall clock of an airing, in the channel's timezone.

    Uses the same helper the rest of the UI uses so a time on this page agrees
    with the time on the schedule page -- which matters when the whole point is
    correlating "the break that jumped" with "what was in it".
    """
    try:
        moment = datetime.fromtimestamp(aired_ms / 1000, tz=_zone(timezone))
    except (OverflowError, OSError, ValueError):
        return "—"
    return moment.strftime("%a %H:%M:%S")


def _zone(timezone: str):
    from zoneinfo import ZoneInfo

    return ZoneInfo(timezone)


def _rows(
    airings: list[dict],
    *,
    timezone: str,
    show_disabled: bool,
) -> str:
    """The airing table.

    A disabled file is shown struck through rather than hidden even when the
    filter is off: the row that prompted the click is the row the reader is
    looking for, and having it disappear is how a page feels broken.
    """
    out: list[str] = []
    for entry in airings:
        disabled = entry["disabled"]
        if disabled and not show_disabled:
            continue
        action = "enable" if disabled else "disable"
        label = "Enable" if disabled else "Disable"
        flag = ' <span class="kind">disabled</span>' if disabled else ""
        hint = (
            f' <span class="warn" title="long file: probably several spots in one">'
            f"long file</span>"
            if entry["duration_ms"] >= MULTI_SPOT_HINT_MS
            else ""
        )
        clip = entry["played_ms"] < entry["duration_ms"] - 1000
        trimmed = (
            f' <span class="mono" title="cut short to fit the pod">'
            f"cut to {_seconds(entry['played_ms'])}</span>"
            if clip
            else ""
        )
        out.append(
            f"""<tr>
  <td class="mono">{_h(_when(entry['aired_ms'], timezone))}</td>
  <td>{_h(entry['name'])}{flag}{hint}</td>
  <td class="mono">{_h(_seconds(entry['duration_ms']))}{trimmed}</td>
  <td>
    <form method="post" action="/commercials/{action}" class="inline" data-progress="Updating commercials and replanning">
      <input type="hidden" name="key" value="{_h(entry['key'])}">
      <button type="submit">{label}</button>
    </form>
  </td>
</tr>"""
        )
    return "".join(out)


def _library_rows(assets, *, limit: int = 40) -> tuple[str, int, int]:
    """The longest commercials, longest first, plus counts for the header.

    Ordered by duration because that is the only signal that correlates with a bad
    file: a 90-second "commercial" is nearly always a whole recorded break, and it
    is the one that has to be cut to fit a pod. The threshold is the *shortest* pod,
    so a 45 s spot that only gets trimmed in a 30 s break is still listed -- with a
    softer label, because "long" is not the same claim as "cannot play whole".

    Capped, because a real library has hundreds of 40-70 s spots and a page listing
    all of them is a page nobody reads. The longest are the ones that lurch, so the
    longest are what is shown; the header states the total so the cap is not a lie.
    """
    ordered = sorted(assets, key=lambda asset: asset.duration_ms, reverse=True)
    candidates = [asset for asset in ordered if asset.duration_ms >= SHORT_POD_HINT_MS]
    out: list[str] = []
    for asset in candidates[:limit]:
        action = "enable" if asset.disabled else "disable"
        label = "Enable" if asset.disabled else "Disable"
        flag = ' <span class="kind">disabled</span>' if asset.disabled else ""
        # Two tiers, because the two claims are different. A file over the longest
        # pod cannot play whole anywhere; one over the shortest pod only gets
        # trimmed when it lands in a short break.
        if asset.duration_ms > MULTI_SPOT_HINT_MS:
            tier = (
                '<span class="warn" title="longer than any pod: cannot play whole">'
                "cannot play whole</span>"
            )
        else:
            tier = (
                '<span class="dim" title="fits a 60s or 90s pod; trimmed in a 30s one">'
                "long</span>"
            )
        out.append(
            f"""<tr>
  <td>{_h(asset.path.replace(chr(92), "/").rsplit("/", 1)[-1])}{flag}</td>
  <td class="mono">{_h(_seconds(asset.duration_ms))}</td>
  <td>{tier}</td>
  <td>
    <form method="post" action="/commercials/{action}" class="inline" data-progress="Updating commercials and replanning">
      <input type="hidden" name="key" value="{_h(asset.key)}">
      <button type="submit">{label}</button>
    </form>
  </td>
</tr>"""
        )
    return "".join(out), len(candidates), len(ordered)


def _disabled_rows(assets) -> str:
    """Every switched-off spot, with the button that puts it back.

    A separate section rather than a filter on the other two, because neither of
    them is guaranteed to contain a disabled file: the airing list only holds what
    has already played (a spot switched off before its first airing never appears
    there), and the long-file list is capped at the longest 40 (a 60 s spot sits
    around rank 190 in a real library). Without this section, disabling a spot
    could hide the only control that undoes it -- a click that cannot be taken
    back is worse than no button at all.
    """
    out: list[str] = []
    for asset in sorted(assets, key=lambda a: a.duration_ms, reverse=True):
        if not asset.disabled:
            continue
        out.append(
            f"""<tr>
  <td>{_h(asset.path.replace(chr(92), "/").rsplit("/", 1)[-1])}</td>
  <td class="mono">{_h(_seconds(asset.duration_ms))}</td>
  <td>
    <form method="post" action="/commercials/enable" class="inline" data-progress="Updating commercials and replanning">
      <input type="hidden" name="key" value="{_h(asset.key)}">
      <button type="submit">Enable</button>
    </form>
  </td>
</tr>"""
        )
    return "".join(out)


def commercials_body(
    config: RootConfig,
    catalog: Catalog,
    *,
    show_disabled: bool = False,
    saved: str = "",
    built: str = "",
    error: str = "",
    limit: int = AIRING_LIMIT,
) -> str:
    """The page body, as a fragment the shell can wrap."""
    timezone = config.channel.grid.timezone
    stats = catalog.commercial_stats()
    airings = catalog.commercial_airings(limit=limit)
    all_ads = catalog.assets(("ad",), include_disabled=True)

    body: list[str] = []
    if error:
        body.append(f'<div class="saved bad">{_h(error)}</div>')
    elif saved:
        suffix = f" &middot; {_h(built)}" if built else ""
        body.append(f'<div class="saved">{_h(saved)}{suffix}</div>')

    body.append(
        f"""<div class="card">
  <p class="label">Commercial library</p>
  <div class="stats">
    <span class="stat"><b>{stats['total']}</b>
      <span class="label">spots catalogued</span></span>
    <span class="stat"><b>{stats['disabled']}</b>
      <span class="label">switched off</span></span>
    <span class="stat"><b>{stats['multi_spot']}</b>
      <span class="label">marked multi-spot</span></span>
  </div>
  <p class="hint">A disabled spot leaves the pool at the next replan; the file
  stays catalogued, so re-enabling it costs nothing but a click. Disabling is the
  reliable way to fix a break that lurches: a recorded break stored as one long
  "commercial" cannot be detected automatically, only heard.</p>
</div>"""
    )

    # Always shown when anything is switched off, and never filtered by `all=1`:
    # this is the undo list. A spot disabled before its first airing appears in
    # neither of the sections below, so without this it would be unreachable.
    disabled_rows = _disabled_rows(all_ads)
    if disabled_rows:
        body.append(
            f"""<div class="card">
  <p class="label">Switched off</p>
  <p class="hint">These spots are out of the pool and will not air. The files are
  still catalogued, so enabling one puts it straight back.</p>
  <table>
    <thead><tr><th>File</th><th>Length</th><th></th></tr></thead>
    <tbody>{disabled_rows}</tbody>
  </table>
</div>"""
        )

    rows = _rows(airings, timezone=timezone, show_disabled=show_disabled)
    if airings and not rows:
        # Everything recent is switched off. Saying "nothing recorded" here was
        # wrong, and worse, it dropped the toggle -- the one control that reveals
        # the rows -- so a click that disabled the last visible spot hid its own
        # undo.
        rows = (
            '<tr><td colspan="4" class="empty">Every recent airing is switched off. '
            "Use <b>Show switched-off</b> to see them.</td></tr>"
        )
    if rows:
        toggle = (
            '<a class="button" href="/commercials">Hide switched-off</a>'
            if show_disabled
            else '<a class="button" href="/commercials?all=1">Show switched-off</a>'
        )
        # Say "most recent" rather than "every", because the list is capped: a
        # heading that promises the whole history and then stops is the sort of
        # thing that reads as a broken page rather than a deliberate limit.
        body.append(
            f"""<div class="card">
  <p class="label">Recently aired</p>
  <p class="hint">The last {AIRING_LIMIT} commercials that have actually gone out,
  newest first. Times are channel time ({_h(timezone)}). {toggle}</p>
  <table>
    <thead><tr><th>Aired</th><th>File</th><th>Length</th><th></th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""
        )
    else:
        body.append(
            """<div class="card">
  <p class="label">Recently aired</p>
  <p class="empty">Nothing recorded yet. The list fills as the channel airs:
  each build records what has already gone out.</p>
</div>"""
        )

    library, candidate_count, total_count = _library_rows(all_ads)
    if library:
        shown_rows = library.count("<tr>")
        # "the longest 40 of 484 long files, out of 840 commercials" -- three
        # numbers, so each one has to say what it counts. A bare "of 484 of 840"
        # reads as a typo and makes the cap look like a bug.
        shown = (
            f"the longest {shown_rows} of {candidate_count} long files, out of "
            f"{total_count} commercials"
            if candidate_count > shown_rows
            else f"all {candidate_count} long files, out of {total_count} commercials"
        )
        body.append(
            f"""<div class="card">
  <p class="label">Long files &mdash; worth a look</p>
  <p class="hint">A pod is booked at 30/60/90 seconds. A file longer than the pod
  it lands in gets trimmed, which is exactly the jump you see. <b>Cannot play
  whole</b> means longer than every pod the channel books, so it is trimmed
  wherever it goes &mdash; the strongest hint this page can give. <b>Long</b> means
  it only gets trimmed in a short break. Showing {shown}. Both labels are
  suggestions, not verdicts, so check the file before switching it off.</p>
  <table>
    <thead><tr><th>File</th><th>Length</th><th></th><th></th></tr></thead>
    <tbody>{library}</tbody>
  </table>
</div>"""
        )
    else:
        body.append(
            f"""<div class="card">
  <p class="label">Long files</p>
  <p class="empty">No commercial is longer than {SHORT_POD_HINT_MS // 1000}s, so
  nothing is being trimmed to fit a pod.</p>
</div>"""
        )

    body.append(_smooth_section(config, catalog))
    body.append(_compilations_section(all_ads))
    body.append(_extras_section(config, catalog))
    return "".join(body)


#: A source needs at least this many numbered files before it is worth listing as a
#: compilation: below it, switching off "the family" is just switching off a spot.
MIN_FAMILY_SIZE = 5

#: The most compilations the page lists, largest first.
FAMILY_LIMIT = 30


def ad_family(asset) -> str:
    """The recording a spot was sliced from, e.g. ``2000/cartoon-network-jan-18-2000-ads``.

    A library imported from recorded breaks is mostly numbered slices
    (``...-ads-013.mp4``), so the filename minus its trailing number plus its folder
    names the *tape*. That is the unit that actually looks or sounds bad: a worn
    tape, a station bug in the corner, a bad capture -- all of it shared by every
    slice.
    """
    path = asset.path.replace("\\", "/")
    parts = path.rsplit("/", 2)
    folder = parts[-2] if len(parts) >= 2 else ""
    stem = parts[-1].rsplit(".", 1)[0]
    stem = re.sub(r"[-_ ]?\d{1,3}$", "", stem)
    return f"{folder}/{stem}"


def families(assets) -> dict[str, list]:
    """Spots grouped by source recording, keeping only real compilations."""
    grouped: dict[str, list] = {}
    for asset in assets:
        grouped.setdefault(ad_family(asset), []).append(asset)
    return {name: members for name, members in grouped.items() if len(members) >= MIN_FAMILY_SIZE}


def _compilations_section(assets) -> str:
    groups = sorted(families(assets).items(), key=lambda item: (-len(item[1]), item[0]))
    if not groups:
        return ""
    rows: list[str] = []
    for name, members in groups[:FAMILY_LIMIT]:
        off = sum(1 for asset in members if asset.disabled)
        on = len(members) - off
        # One button, whichever way it would change things most usefully: if anything
        # is still on, offer to switch the lot off; once all are off, offer to undo.
        action, label = ("disable", "Switch all off") if on else ("enable", "Switch all on")
        status = (
            f'<span class="kind">all off</span>'
            if not on
            else (f'<span class="dim">{off} off</span>' if off else "")
        )
        rows.append(
            f"""<tr>
  <td class="mono">{_h(name)}</td>
  <td class="mono">{len(members)}</td>
  <td>{status}</td>
  <td>
    <form method="post" action="/commercials/family/{action}" class="inline" data-progress="Updating commercials and replanning">
      <input type="hidden" name="family" value="{_h(name)}">
      <button type="submit">{label}</button>
    </form>
  </td>
</tr>"""
        )
    shown = f"the {FAMILY_LIMIT} largest of {len(groups)}" if len(groups) > FAMILY_LIMIT else f"all {len(groups)}"
    return f"""<div class="card">
  <p class="label">Compilations &mdash; switch off a whole source</p>
  <p class="hint">Most spots in a recorded-break library are numbered slices of a
  small number of recordings. If one recording looks or sounds bad &mdash; a worn
  tape, a station logo in the corner, a poor capture &mdash; every slice shares the
  fault, so switching off the source is one click instead of dozens. Showing
  {shown} compilations, largest first. Reversible: the files stay catalogued.</p>
  <table>
    <thead><tr><th>Recording</th><th>Spots</th><th></th><th></th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>"""


def _smooth_section(config: RootConfig, catalog: Catalog) -> str:
    """Whether breaks are conformed and merged, in plain words."""
    from ..pipeline import conform_params

    stats = catalog.conform_stats(conform_params(config).signature())
    ads, bumps = stats.get("ad"), stats.get("bump")
    if not ads:
        return ""
    done = ads["conformed"] + (bumps["conformed"] if bumps else 0)
    total = ads["total"] + (bumps["total"] if bumps else 0)
    stale = ads["stale"] + (bumps["stale"] if bumps else 0)
    merging = config.ads.merge_breaks

    if done == 0:
        headline = "Not conformed yet"
        advice = (
            "Spots are played exactly as recorded, so the picture width changes "
            "between spots and every spot is a separate item for the player to "
            "resync on. Run <span class=\"mono\">nostalgia conform</span> once to fix "
            "the footage, then turn on <b>Join each break into one file</b> on the "
            "config page."
        )
    else:
        headline = f"{done} of {total} break files conformed"
        notes = []
        if stale:
            notes.append(f"{stale} out of date &mdash; re-run <span class=\"mono\">nostalgia conform</span>")
        if done < total:
            notes.append("breaks only merge where every piece is conformed")
        notes.append("merging is <b>on</b>" if merging else "merging is <b>off</b> &mdash; turn on <b>Join each break into one file</b> on the config page")
        advice = "; ".join(notes) + "."
    return f"""<div class="card">
  <p class="label">Smooth breaks</p>
  <p class="hint"><b>{headline}.</b> {advice}</p>
</div>"""


def _extras_section(config: RootConfig, catalog: Catalog) -> str:
    """The extras pool, and the dial that decides how often it is used.

    Extras are not commercials, but they are the other thing that can fill a break,
    and the one thing an owner will want to switch off here: a music video that
    turned out to be a bad rip is exactly as annoying as a bad spot and is fixed
    the same way.
    """
    extras = catalog.assets(("extra",), include_disabled=True)
    chance = config.ads.extras_chance
    if not config.ads.extras_enabled:
        # Off is a deliberate state, not a misconfiguration: say how to turn it on
        # rather than only that it is off.
        dial = "Switched off &mdash; every break airs full commercials"
        advice = (
            "Turn it on with <b>Air extras instead of some commercial breaks</b> "
            "on the config page."
        )
    elif chance <= 0:
        dial = "Switched on, but the chance is 0% &mdash; every break airs full commercials"
        advice = "Raise <b>Chance of an extras break</b> on the config page."
    else:
        dial = f"On &mdash; one break in {round(1 / chance)} is replaced"
        advice = (
            "A replaced break keeps its own length, so the next programme still "
            "starts on time; if the chosen piece is shorter than the break, spots "
            "top it up. Bump slots are never replaced, and neither is a break in "
            "the middle of a show. Switching one off here takes it out of the "
            "pool at the next replan."
        )

    rows: list[str] = []
    for asset in sorted(extras, key=lambda a: (a.duration_ms, a.key)):
        action = "enable" if asset.disabled else "disable"
        label = "Enable" if asset.disabled else "Disable"
        flag = ' <span class="kind">disabled</span>' if asset.disabled else ""
        rows.append(
            f"""<tr>
  <td>{_h(asset.path.replace(chr(92), "/").rsplit("/", 1)[-1])}{flag}</td>
  <td class="mono">{_h(_seconds(asset.duration_ms))}</td>
  <td>
    <form method="post" action="/commercials/{action}" class="inline" data-progress="Updating commercials and replanning">
      <input type="hidden" name="key" value="{_h(asset.key)}">
      <button type="submit">{label}</button>
    </form>
  </td>
</tr>"""
        )

    if not extras:
        return f"""<div class="card">
  <p class="label">Extras instead of commercials</p>
  <p class="hint"><b>{dial}</b>. {advice}</p>
  <p class="empty">No extras catalogued yet. Put music videos, DVD extras or
  anything else that is not a spot in <span class="mono">media/extras/</span> and
  rescan; they appear here with a switch each.</p>
</div>"""

    return f"""<div class="card">
  <p class="label">Extras instead of commercials</p>
  <p class="hint"><b>{dial}</b>. {advice}</p>
  <table>
    <thead><tr><th>File</th><th>Length</th><th></th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>"""


def set_family_enabled(catalog: Catalog, family: str, *, enabled: bool) -> str:
    """Switch every spot of one compilation on or off, returning a sentence.

    Raises ``LookupError`` for an unknown family, for the same reason a bad key
    does: a click that reports success and changes nothing is worse than an error.
    """
    members = families(catalog.assets(("ad",), include_disabled=True)).get(family)
    if not members:
        raise LookupError(f"no compilation named {family!r}")
    catalog.set_disabled_many([asset.key for asset in members], not enabled)
    return f"{len(members)} spots from {family} {'switched on' if enabled else 'switched off'}"


def set_spot_enabled(catalog: Catalog, key: str, *, enabled: bool) -> str:
    """Enable or disable one spot, returning a sentence for the page.

    Raises ``LookupError`` for an unknown key so the route can answer 404 instead
    of silently doing nothing -- a click that reports success and changes nothing
    is worse than an error.
    """
    if not catalog.set_disabled(key, disabled=not enabled):
        raise LookupError(f"no commercial with key {key!r}")
    name = key.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return f"{name} {'re-enabled' if enabled else 'switched off'}"


__all__ = [
    "AIRING_LIMIT",
    "MIN_FAMILY_SIZE",
    "MULTI_SPOT_HINT_MS",
    "SHORT_POD_HINT_MS",
    "ad_family",
    "commercials_body",
    "families",
    "set_family_enabled",
    "set_spot_enabled",
]

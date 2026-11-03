"""Website report for episode and commercial airing counts."""

from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from ..catalog import Catalog
from ..config import RootConfig
from ..playback import PlaybackStore

PERIODS = {
    "7d": ("Last 7 days", 7),
    "30d": ("Last 30 days", 30),
    "all": ("All recorded", None),
}
ROW_LIMIT = 100
DAY_MS = 24 * 60 * 60 * 1000


def _h(value: object) -> str:
    return escape(str(value), quote=True)


def _when(aired_ms: int, timezone: str) -> str:
    try:
        return datetime.fromtimestamp(
            aired_ms / 1000, tz=ZoneInfo(timezone)
        ).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "—"


def _rows(
    items: list[dict],
    *,
    kind: str,
    config: RootConfig,
    timezone: str,
) -> str:
    output: list[str] = []
    for item in items:
        if kind == "episode":
            show_id = item["show_id"]
            show = config.shows.get(show_id)
            show_name = show.title if show is not None else show_id or "Unknown show"
            title = item["title"] or item["key"]
            media = (
                f"S{item['season']:02d}E{item['episode']:02d} &mdash; "
                f"{_h(title)}"
            )
            output.append(
                f"""<tr>
  <td>{_h(show_name)} <span class="dim">({_h(show_id)})</span></td>
  <td>{media}</td>
  <td class="mono">{item['airings']}</td>
  <td class="mono">{item['scheduled_airings']}</td>
  <td class="mono">{_h(_when(item['last_aired_ms'], timezone))}</td>
</tr>"""
            )
        else:
            path = Path(item["path"])
            name = path.name if item["path"] else item["key"]
            output.append(
                f"""<tr>
  <td>{_h(name)}</td>
  <td class="mono">{item['airings']}</td>
  <td class="mono">{item['scheduled_airings']}</td>
  <td class="mono">{_h(_when(item['last_aired_ms'], timezone))}</td>
</tr>"""
            )
    return "".join(output)


def _table(
    title: str,
    report: dict[str, object],
    *,
    kind: str,
    config: RootConfig,
    timezone: str,
) -> str:
    items = report["items"]
    assert isinstance(items, list)
    if kind == "episode":
        headings = (
            "<th>Show</th><th>Episode</th><th>Observed starts</th>"
            "<th>Scheduled</th><th>Last segment request</th>"
        )
        empty_text = "No successful episode segment requests recorded for this period."
    else:
        headings = (
            "<th>Commercial</th><th>Observed starts</th>"
            "<th>Scheduled</th><th>Last segment request</th>"
        )
        empty_text = "No successful commercial segment requests recorded for this period."
    rows = _rows(items, kind=kind, config=config, timezone=timezone)
    content = (
        f"<table><thead><tr>{headings}</tr></thead><tbody>{rows}</tbody></table>"
        if rows
        else f'<p class="empty">{empty_text}</p>'
    )
    shown_note = (
        f'<p class="hint">Showing the top {ROW_LIMIT} of '
        f"{report['distinct_assets']} aired items.</p>"
        if report["distinct_assets"] > len(items)
        else ""
    )
    return f"""<div class="card">
  <p class="label">{title}</p>
  <div class="stats">
    <span class="stat"><b>{report['total_airings']}</b>
      <span class="label">observed starts</span></span>
    <span class="stat"><b>{report['distinct_assets']}</b>
      <span class="label">distinct items</span></span>
    <span class="stat"><b>{report['scheduled_airings']}</b>
      <span class="label">scheduled airings</span></span>
  </div>
  {shown_note}
  {content}
</div>"""


def rotation_body(
    config: RootConfig,
    catalog: Catalog,
    *,
    period: str = "30d",
    playback: PlaybackStore | None = None,
) -> str:
    """Render observed HLS starts alongside schedule counts."""
    if period not in PERIODS:
        raise ValueError(f"unsupported rotation period: {period!r}")

    label, days = PERIODS[period]
    now_ms = int(datetime.now().timestamp() * 1000)
    since_ms = None if days is None else now_ms - days * DAY_MS
    playback = playback or PlaybackStore(catalog.path)
    episodes = playback.airing_counts(
        "episode", since_ms=since_ms, before_ms=now_ms, limit=ROW_LIMIT
    )
    commercials = playback.airing_counts(
        "ad", since_ms=since_ms, before_ms=now_ms, limit=ROW_LIMIT
    )
    for report, kind in ((episodes, "episode"), (commercials, "ad")):
        scheduled = catalog.airing_counts_by_key(
            kind, since_ms=since_ms, before_ms=now_ms
        )
        for item in report["items"]:
            item["scheduled_airings"] = scheduled.get(item["key"], 0)
        report["scheduled_airings"] = sum(scheduled.values())
    options = "".join(
        f'<option value="{value}"{" selected" if value == period else ""}>'
        f"{_h(option_label)}</option>"
        for value, (option_label, _days) in PERIODS.items()
    )
    timezone = config.channel.grid.timezone
    return f"""<div class="card">
  <p class="label">Rotation report</p>
  <p class="hint">Observed starts count an item once a client successfully requests
  an HLS segment from it. Players may buffer ahead, and a request does not prove a
  person watched the full item. “Scheduled” is the scheduler's count for comparison.
  Docker Compose routes the stream through the telemetry proxy; native streaming
  without that proxy records no observed starts.</p>
  <form method="get" action="/rotation" class="inline">
    <label for="period">Period</label>
    <select id="period" name="period">{options}</select>
    <button type="submit">Update</button>
  </form>
  <p class="hint">{_h(label)} &middot; times shown in channel time
  ({_h(timezone)}). Tables are sorted by observed starts, then most recently requested.</p>
</div>
{_table('Episodes', episodes, kind='episode', config=config, timezone=timezone)}
{_table('Commercials', commercials, kind='ad', config=config, timezone=timezone)}"""


__all__ = ["PERIODS", "ROW_LIMIT", "rotation_body"]

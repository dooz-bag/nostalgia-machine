"""HTML for the mixed bumper/commercial import workflow."""

from __future__ import annotations

import html
import json
from typing import Any
from urllib.parse import quote

from ..config import RootConfig
from ..media_review import Analysis, list_sources, suggested_label
from ..paths import Paths


def _h(value: object) -> str:
    return html.escape(str(value), quote=True)


def imports_body(
    paths: Paths,
    config: RootConfig,
    *,
    analysis: Analysis | None = None,
    error: str = "",
    message: str = "",
    prior_segments: list[dict[str, Any]] | None = None,
    ad_year: str = "",
    bump_subject: str = "neutral",
    bump_era: str = "",
) -> str:
    flash = ""
    if error:
        flash = f'<div class="warn" role="alert">{_h(error)}</div>'
    elif message:
        flash = f'<div class="saved" role="status">{_h(message)}</div>'

    sources = list_sources(paths)
    if not sources:
        source_picker = (
            '<p class="empty">No videos are staged yet. Copy an authorized source '
            'video into <code>media/imports/</code>, then reload this page.</p>'
        )
    else:
        options = "".join(
            f'<option value="{_h(name)}">{_h(name)}</option>' for name in sources
        )
        source_picker = f"""
<form method="post" action="/imports/analyze" data-progress="Analyzing scene changes">
  <label for="import-source">Source video</label>
  <select id="import-source" name="filename" required>{options}</select>
  <p class="hint">The original file remains in the staging folder and is never modified.</p>
  <button type="submit">Analyze video</button>
</form>"""

    body = f"""
{flash}
<div class="card">
  <p class="label">Mixed-video importer</p>
  <h2>Separate bumpers and commercials</h2>
  <p>Put an authorized local video in <code>media/imports/</code>. Scene cuts and
  common clip lengths are suggestions only: review the video and approve each
  bumper or commercial before anything enters the library. You can also skip
  sections.</p>
  {source_picker}
</div>"""
    if analysis is None:
        return body

    source_url = "/imports/source/" + quote(analysis.source_name, safe="")
    duration = f"{analysis.duration:.3f}"
    cut_points = analysis.cut_points[1:-1]
    shown_cuts = cut_points[:400]
    cut_buttons = "".join(
        f'<button type="button" class="cut-point" data-time="{point:.3f}">'
        f'{point:.3f}s</button>'
        for point in shown_cuts
    )
    if not cut_buttons:
        cut_buttons = '<span class="hint">No scene cuts were detected.</span>'
    more_cuts = (
        f'<p class="hint">Showing the first 400 of {len(cut_points)} detected cuts.</p>'
        if len(cut_points) > len(shown_cuts)
        else ""
    )
    segment_rows = []
    for index, segment in enumerate(prior_segments or []):
        if not isinstance(segment, dict):
            continue
        start = _h(segment.get("start", ""))
        end = _h(segment.get("end", ""))
        label = str(segment.get("label", suggested_label(0)))
        if label not in {"bumper", "commercial", "skip"}:
            label = "skip"
        try:
            suggestion = suggested_label(float(end) - float(start))
        except ValueError:
            suggestion = "skip"
        segment_rows.append(_segment_row(index, start, end, label, suggestion))
    rows = "".join(segment_rows)
    show_options = ['<option value="neutral">Neutral / unknown show</option>']
    show_options.extend(
        f'<option value="{_h(show_id)}"{" selected" if bump_subject == show_id else ""}>'
        f'{_h(show.title)} ({_h(show_id)})</option>'
        for show_id, show in sorted(config.shows.items())
    )
    if bump_subject == "neutral":
        show_options[0] = '<option value="neutral" selected>Neutral / unknown show</option>'
    bump_metadata = "".join(show_options)
    body += f"""
<div class="card">
  <p class="label">Review video</p>
  <h2>{_h(analysis.source_name)}</h2>
  <video controls preload="metadata" style="width:100%;max-height:60vh"
    src="{_h(source_url)}" aria-label="Source video preview"></video>
  <p class="hint">Duration: {duration}s. Click a detected cut to seek there; the
  cuts are not assumed to be clip boundaries.</p>
  <div class="cut-points" aria-label="Detected scene cuts">{cut_buttons}</div>
  {more_cuts}
</div>
<div class="card">
  <p class="label">Mark segments</p>
  <p>Review the source, mark each clip’s start and end, then confirm the suggested
  label or choose another. These duration-based labels are not automatic decisions.</p>
  <div class="row">
    <button type="button" id="mark-start">Mark clip start at playhead</button>
    <button type="button" id="mark-end">Mark clip end &amp; add</button>
    <span id="mark-status" class="hint" role="status"></span>
  </div>
  <form method="post" action="/imports/export" id="import-export"
    data-progress="Exporting approved clips">
    <input type="hidden" name="token" value="{_h(analysis.token)}">
    <input type="hidden" name="segments" id="segments-json" value="[]">
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>Start (s)</th><th>End (s)</th><th>Suggestion</th><th>Class</th><th></th></tr></thead>
        <tbody id="segment-rows">{rows}</tbody>
      </table>
    </div>
    <h3>Library metadata</h3>
    <label for="ad-year">Commercial year (required if exporting commercials)</label>
    <input id="ad-year" name="ad_year" inputmode="numeric" pattern="[0-9]{{4}}"
      value="{_h(ad_year)}" placeholder="e.g. 2003">
    <label for="bump-subject">Bumper subject</label>
    <select id="bump-subject" name="bump_subject">{bump_metadata}</select>
    <label for="bump-era">Bumper era (optional)</label>
    <input id="bump-era" name="bump_era" inputmode="numeric" pattern="[0-9]{{4}}"
      value="{_h(bump_era)}" placeholder="e.g. 2003">
    <p class="hint">Bumpers are written with import provenance and an “ANY”
    position. Commercials are filed under their year. Existing files are never
    overwritten.</p>
    <button type="submit">Export approved segments</button>
  </form>
</div>
<script>
(() => {{
  const video = document.querySelector("video");
  const rows = document.getElementById("segment-rows");
  const status = document.getElementById("mark-status");
  let pendingStart = null;
  const seconds = () => Math.max(0, video.currentTime);
  const suggestion = (duration) => {{
    if (duration < 4) return "skip";
    if ([15, 30, 60].some((target) => Math.abs(duration - target) <= 3)) return "commercial";
    if (duration <= 13) return "bumper";
    if (duration <= 65) return "commercial";
    return "skip";
  }};
  document.querySelectorAll(".cut-point").forEach((button) => {{
    button.addEventListener("click", () => {{
      video.currentTime = Number(button.dataset.time);
      video.focus();
    }});
  }});
  document.getElementById("mark-start").addEventListener("click", () => {{
    pendingStart = seconds();
    status.textContent = `Start marked at ${{pendingStart.toFixed(3)}}s`;
  }});
  document.getElementById("mark-end").addEventListener("click", () => {{
    if (pendingStart === null) {{
      status.textContent = "Mark a clip start first.";
      return;
    }}
    const end = seconds();
    if (end - pendingStart < 2) {{
      status.textContent = "A clip must be at least 2 seconds.";
      return;
    }}
    rows.insertAdjacentHTML("beforeend", {json.dumps(_segment_template())}
      .replaceAll("__INDEX__", String(rows.children.length))
      .replaceAll("__START__", pendingStart.toFixed(3))
      .replaceAll("__END__", end.toFixed(3))
      .replaceAll("__SUGGESTION__", suggestion(end - pendingStart)));
    rows.lastElementChild.querySelector("[name='segment-label']").value =
      suggestion(end - pendingStart);
    pendingStart = end;
    status.textContent = `Segment added; next start is ${{end.toFixed(3)}}s`;
  }});
  rows.addEventListener("click", (event) => {{
    if (event.target instanceof HTMLButtonElement && event.target.dataset.remove !== undefined) {{
      event.target.closest("tr").remove();
    }}
  }});
  document.getElementById("import-export").addEventListener("submit", (event) => {{
    const segments = [...rows.querySelectorAll("tr")].map((row) => ({{
      start: row.querySelector("[name='segment-start']").value,
      end: row.querySelector("[name='segment-end']").value,
      label: row.querySelector("[name='segment-label']").value
    }}));
    if (!segments.length) {{
      event.preventDefault();
      event.stopPropagation();
      status.textContent = "Mark at least one segment before exporting.";
      return;
    }}
    document.getElementById("segments-json").value = JSON.stringify(segments);
  }});
}})();
</script>"""
    return body


def _segment_row(
    index: int, start: str, end: str, label: str, suggestion: str | None = None
) -> str:
    if suggestion is None:
        try:
            suggestion = suggested_label(float(end) - float(start))
        except ValueError:
            suggestion = "skip"
    selected = {
        value: " selected" if label == value else ""
        for value in ("bumper", "commercial", "skip")
    }
    return f"""
<tr>
  <td>{index + 1}</td>
  <td><input aria-label="Segment {index + 1} start" name="segment-start"
    type="number" step="0.001" min="0" value="{start}"></td>
  <td><input aria-label="Segment {index + 1} end" name="segment-end"
    type="number" step="0.001" min="0" value="{end}"></td>
  <td class="hint">Suggested: {_h(suggestion)}</td>
  <td><select aria-label="Segment {index + 1} classification" name="segment-label">
    <option value="bumper"{selected["bumper"]}>Bumper</option>
    <option value="commercial"{selected["commercial"]}>Commercial</option>
    <option value="skip"{selected["skip"]}>Skip</option>
  </select></td>
  <td><button type="button" data-remove>Remove</button></td>
</tr>"""


def _segment_template() -> str:
    return (
        _segment_row(
            0,
            "__START__",
            "__END__",
            "bumper",
            suggestion="__SUGGESTION__",
        )
        .replace("Segment 1", "Segment __INDEX__")
        .replace("<td>1</td>", "<td>__INDEX__</td>")
    )


__all__ = ["imports_body"]

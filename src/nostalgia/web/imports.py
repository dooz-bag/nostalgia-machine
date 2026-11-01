"""HTML for the mixed bumper/commercial import workflow."""

from __future__ import annotations

import html
import json
from typing import Any
from urllib.parse import quote

from ..config import RootConfig
from ..media_review import Analysis, PendingImportClip, list_sources, suggested_label
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
    pending_count: int = 0,
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
  <p>Put an authorized local video in <code>media/imports/</code>. Scene cuts are
  seek suggestions, not clip boundaries. Mark the clips you want to cut; every cut
  goes to a private review queue before entering the library. The original source
  remains unchanged.</p>
  {f'<p><a href="/imports/review-queue">Review {pending_count} cut clip(s)</a></p>' if pending_count else ""}
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
        try:
            suggestion = suggested_label(float(end) - float(start))
        except ValueError:
            suggestion = "skip"
        segment_rows.append(_segment_row(index, start, end, suggestion))
    rows = "".join(segment_rows)
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
  <p class="label">Cut clips for review</p>
  <p>Mark each clip’s start and end. The duration label is only a hint; every cut
  will be reviewed individually before it is categorized or discarded.</p>
  <div class="row">
    <button type="button" id="mark-start">Mark clip start at playhead</button>
    <button type="button" id="mark-end">Mark clip end &amp; add</button>
    <span id="mark-status" class="hint" role="status"></span>
  </div>
  <form method="post" action="/imports/cut" id="import-export"
    data-progress="Cutting clips for review">
    <input type="hidden" name="token" value="{_h(analysis.token)}">
    <input type="hidden" name="segments" id="segments-json" value="[]">
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>Start (s)</th><th>End (s)</th><th>Duration hint</th><th></th></tr></thead>
        <tbody id="segment-rows">{rows}</tbody>
      </table>
    </div>
    <p class="hint">The duration hint does not classify the clip. Review each cut
    in the import-only queue; trash removes only the cut copy, not the source.</p>
    <button type="submit">Cut clips for review</button>
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
      end: row.querySelector("[name='segment-end']").value
    }}));
    if (!segments.length) {{
      event.preventDefault();
      event.stopPropagation();
      status.textContent = "Mark at least one clip before cutting.";
      return;
    }}
    document.getElementById("segments-json").value = JSON.stringify(segments);
  }});
}})();
</script>"""
    return body


def review_queue_body(
    clips: list[PendingImportClip],
    config: RootConfig,
    *,
    error: str = "",
    message: str = "",
) -> str:
    flash = (
        f'<div class="warn" role="alert">{_h(error)}</div>'
        if error
        else f'<div class="saved" role="status">{_h(message)}</div>' if message else ""
    )
    if not clips:
        return (
            f'{flash}<div class="card"><p class="label">Import review</p>'
            '<p class="empty">No cut clips are awaiting review.</p>'
            '<p><a href="/imports">Back to Import media</a></p></div>'
        )

    show_options = [
        '<option value="">Choose show</option>',
        '<option value="neutral">Neutral / unknown show</option>',
    ]
    show_options.extend(
        f'<option value="{_h(show_id)}">{_h(show.title)} ({_h(show_id)})</option>'
        for show_id, show in sorted(config.shows.items())
    )
    shows = "".join(show_options)
    cards = []
    for clip in clips:
        cards.append(
            f"""
<div class="card import-review-card">
  <p class="label">{_h(clip.source_name)} &middot; {_h(f'{clip.start:.3f}')}&ndash;{_h(f'{clip.end:.3f}')}s
     ({clip.duration:.1f}s)</p>
  <video controls preload="metadata" style="width:100%;max-height:45vh"
    src="/imports/review-clip/{_h(clip.clip_id)}" aria-label="Cut clip preview"></video>
  <form method="post" action="/imports/review-clip/{_h(clip.clip_id)}"
    class="import-review-form">
    <label>Classification
      <select name="classification" required>
        <option value="">Choose one</option>
        <option value="commercial">Commercial</option>
        <option value="bumper">Bumper</option>
        <option value="trash">Trash</option>
      </select>
    </label>
    <div data-review-kind="commercial" hidden>
      <label>Commercial year
        <input name="ad_year" inputmode="numeric" pattern="[0-9]{{4}}"
          placeholder="e.g. 2002">
      </label>
    </div>
    <div data-review-kind="bumper" hidden>
      <label>Show it refers to
        <select name="bump_subject">{shows}</select>
      </label>
      <label>Bumper year (optional)
        <input name="bump_era" inputmode="numeric" pattern="[0-9]{{4}}"
          placeholder="e.g. 2002">
      </label>
      <label>Placement
        <select name="position">
          <option value="">Unspecified</option>
          <option value="INTO">Before the show</option>
          <option value="AFTER">After the show</option>
          <option value="OUT">In the middle of the show</option>
        </select>
      </label>
    </div>
    <button type="submit">Save choice</button>
  </form>
</div>"""
        )
    return (
        f'{flash}<div class="card"><p class="label">{len(clips)} cut clip(s) awaiting review</p>'
        '<p>Choose commercial, bumper, or trash for each clip. Trash permanently '
        'deletes only the cut copy; the source video remains untouched.</p></div>'
        + "".join(cards)
        + """
<script>
document.querySelectorAll(".import-review-form").forEach((form) => {
  const classification = form.querySelector("[name='classification']");
  const update = () => {
    form.querySelectorAll("[data-review-kind]").forEach((section) => {
      const active = section.dataset.reviewKind === classification.value;
      section.hidden = !active;
      section.querySelectorAll("input, select").forEach((field) => {
        field.disabled = !active;
        field.required = active && field.name !== "bump_era" && field.name !== "position";
      });
    });
  };
  classification.addEventListener("change", update);
  form.addEventListener("submit", (event) => {
    if (classification.value === "trash" &&
        !confirm("Permanently delete only this cut clip? The source video is unchanged.")) {
      event.preventDefault();
    }
  });
  update();
});
</script>"""
    )


def _segment_row(
    index: int, start: str, end: str, suggestion: str | None = None
) -> str:
    if suggestion is None:
        try:
            suggestion = suggested_label(float(end) - float(start))
        except ValueError:
            suggestion = "skip"
    return f"""
<tr>
  <td>{index + 1}</td>
  <td><input aria-label="Segment {index + 1} start" name="segment-start"
    type="number" step="0.001" min="0" value="{start}"></td>
  <td><input aria-label="Segment {index + 1} end" name="segment-end"
    type="number" step="0.001" min="0" value="{end}"></td>
  <td class="hint">{_h(suggestion)} (duration only)</td>
  <td><button type="button" data-remove>Remove</button></td>
</tr>"""


def _segment_template() -> str:
    return (
        _segment_row(
            0,
            "__START__",
            "__END__",
            suggestion="__SUGGESTION__",
        )
        .replace("Segment 1", "Segment __INDEX__")
        .replace("<td>1</td>", "<td>__INDEX__</td>")
    )


__all__ = ["imports_body", "review_queue_body"]

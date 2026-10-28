"""The Lines page: what generated bump cards say, and approving new ones.

A local model can propose lines; a person decides. Nothing a model writes reaches a
card until it has passed the truth guard (a ``NEXT`` card must not say "you just
watched") *and* been approved here. The page is the second gate, and it is also
where you can see what the first one refused and why.
"""

from __future__ import annotations

from html import escape

from .. import bumplines as bl
from ..config import RootConfig
from ..paths import Paths

POSITION_HELP = {
    "INTO": "the show is about to start",
    "NEXT": "another show follows this one",
    "AFTER": "the episode has just ended",
    "OUT": "the show is coming back from a break",
    "ANY": "any moment; says nothing about what is on",
}


def _h(value: object) -> str:
    return escape(str(value), quote=True)


def _files(paths: Paths):
    return (
        paths.config_dir / "bump-lines.yaml",
        bl.CandidateStore(paths.data_dir / "bump-line-candidates.json"),
    )


def _sample_title(config: RootConfig) -> str:
    for show in config.shows.values():
        if show.enable:
            return show.title
    return "Your Show"


def lines_body(
    config: RootConfig, paths: Paths, *, saved: str = "", error: str = ""
) -> str:
    bank_file, store = _files(paths)
    bank = bl.load_bank(bank_file)
    sample = _sample_title(config)
    generated = config.bumps.generated

    body: list[str] = []
    if error:
        body.append(f'<div class="saved bad">{_h(error)}</div>')
    elif saved:
        body.append(f'<div class="saved">{_h(saved)}</div>')

    body.append(_status_card(config, bank))
    body.append(_generate_card(config))

    pending = store.pending()
    if pending:
        body.append(_pending_card(pending, sample, config))
    else:
        body.append(
            """<div class="card">
  <p class="label">Waiting for approval</p>
  <p class="empty">Nothing is waiting. Ask the model for some above.</p>
</div>"""
        )
    body.append(_bank_card(bank, sample))
    if not generated.lines:
        body.append(
            '<div class="card"><p class="hint">Sentences are switched off '
            "(<span class=\"mono\">bumps.generated.lines</span>), so generated cards print "
            "a plain position word and title. Approved lines are kept for when you turn "
            "it on.</p></div>"
        )
    return "".join(body)


def _status_card(config: RootConfig, bank: bl.LineBank) -> str:
    generated = config.bumps.generated
    try:
        url, models = bl.find_ollama(generated.ollama_url, timeout=1.5)
        model = generated.ollama_model or (models[0] if models else "none installed")
        engine = f"<b>{_h(model)}</b> at <span class=\"mono\">{_h(url)}</span>"
    except bl.ModelError:
        engine = (
            "<b>not running.</b> Start Ollama and pull a model to generate new lines. "
            "The lines you already have work without it."
        )
    return f"""<div class="card">
  <p class="label">Generated bump text</p>
  <div class="stats">
    <span class="stat"><b>{bank.count()}</b><span class="label">approved lines</span></span>
    <span class="stat"><b>{len(bank.lines.get('ANY', ()))}</b><span class="label">general cards</span></span>
  </div>
  <p class="hint">Local model: {engine}. Every line is checked against what its position
  may truthfully say before it is even shown to you, so a <b>next</b> card can never claim
  "you just watched".</p>
</div>"""


def _generate_card(config: RootConfig) -> str:
    positions = "".join(
        f'<option value="{p}">{p} &mdash; {_h(POSITION_HELP[p])}</option>' for p in bl.POSITIONS
    )
    shows = '<option value="">General (fits any show)</option>' + "".join(
        f'<option value="{_h(sid)}">{_h(show.title)}</option>'
        for sid, show in config.shows.items()
        if show.enable
    )
    return f"""<div class="card">
  <p class="label">Ask the model for more</p>
  <form method="post" action="/lines/generate" class="inline">
    <select name="position">{positions}</select>
    <select name="show">{shows}</select>
    <input type="number" name="count" value="12" min="1" max="30" style="width:5em">
    <button type="submit">Generate</button>
  </form>
  <p class="hint">Takes a few seconds. Choose a show to get jokes that lean on its
  characters; leave it on General for lines that fit any show.</p>
</div>"""


def _pending_card(pending: list[bl.Candidate], sample: str, config: RootConfig) -> str:
    titles = {sid: show.title for sid, show in config.shows.items()}
    rows: list[str] = []
    for candidate in pending:
        for_show = titles.get(candidate.show or "", None)
        preview = bl.render_line(candidate.text, for_show or sample)
        scope = f"only {_h(for_show)}" if for_show else "any show"
        rows.append(
            f"""<tr>
  <td class="mono">{_h(candidate.position)}</td>
  <td>{_h(preview)}<br><span class="dim">{_h(candidate.text)} &middot; {scope}</span></td>
  <td>
    <form method="post" action="/lines/approve" class="inline">
      <input type="hidden" name="id" value="{_h(candidate.id)}">
      <button type="submit">Approve</button>
    </form>
    <form method="post" action="/lines/reject" class="inline">
      <input type="hidden" name="id" value="{_h(candidate.id)}">
      <button type="submit">Reject</button>
    </form>
  </td>
</tr>"""
        )
    return f"""<div class="card">
  <p class="label">Waiting for approval ({len(pending)})</p>
  <form method="post" action="/lines/approve-all" class="inline">
    <button type="submit">Approve all {len(pending)}</button>
  </form>
  <form method="post" action="/lines/clear" class="inline">
    <button type="submit">Discard all</button>
  </form>
  <table>
    <thead><tr><th>For</th><th>Reads as</th><th></th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>"""


def _bank_card(bank: bl.LineBank, sample: str) -> str:
    blocks: list[str] = []
    for position in bl.POSITIONS:
        items = bank.lines.get(position, ())
        if not items:
            continue
        shown = "".join(f"<li>{_h(bl.render_line(line, sample))}</li>" for line in items)
        blocks.append(
            f"<details><summary><b>{position}</b> &mdash; {len(items)} "
            f"<span class=\"dim\">({_h(POSITION_HELP[position])})</span></summary>"
            f"<ul>{shown}</ul></details>"
        )
    if not blocks:
        return ""
    return f"""<div class="card">
  <p class="label">The bank</p>
  <p class="hint">Shown with &ldquo;{_h(sample)}&rdquo; filled in. Edit
  <span class="mono">config/bump-lines.yaml</span> to remove one.</p>
  {''.join(blocks)}
</div>"""


# -- actions --------------------------------------------------------------------------


def generate(
    config: RootConfig,
    paths: Paths,
    *,
    position: str,
    show: str | None,
    count: int,
) -> str:
    """Ask the local model for lines and queue what passes. Returns a sentence."""
    position = position.upper()
    if position not in bl.POSITIONS:
        raise ValueError(f"unknown position {position!r}")
    if show and show not in config.shows:
        raise ValueError(f"unknown show {show!r}")

    bank_file, store = _files(paths)
    generated = config.bumps.generated
    url, models = bl.find_ollama(generated.ollama_url, timeout=3.0)
    model = generated.ollama_model or (models[0] if models else "")
    if not model:
        raise bl.ModelError("Ollama has no models installed; `ollama pull` one first")

    result = bl.propose(
        position,
        bank=bl.load_bank(bank_file),
        url=url,
        model=model,
        count=max(1, min(count, 30)),
        show=show or None,
        show_title=config.shows[show].title if show else None,
        other_titles=[s.title for s in config.shows.values()],
        existing=[c.text for c in store.pending(position)],
    )
    queued = sum(1 for text in result.accepted if store.add(position, text, show=show or None))
    refused = len(result.rejected)
    note = f", {refused} refused by the truth guard" if refused else ""
    return f"{queued} new {position} line(s) are waiting for your approval{note}"


def approve(paths: Paths, candidate_id: str) -> str:
    bank_file, store = _files(paths)
    candidate = store.remove(candidate_id)
    if candidate is None:
        raise LookupError("that line is no longer waiting")
    added = bl.append_line(bank_file, candidate.position, candidate.text, show=candidate.show)
    return "approved" if added else "already in the bank"


def approve_all(paths: Paths) -> str:
    bank_file, store = _files(paths)
    approved = 0
    for candidate in store.pending():
        store.remove(candidate.id)
        if bl.append_line(bank_file, candidate.position, candidate.text, show=candidate.show):
            approved += 1
    return f"approved {approved} line(s)"


def reject(paths: Paths, candidate_id: str) -> str:
    _, store = _files(paths)
    if store.remove(candidate_id) is None:
        raise LookupError("that line is no longer waiting")
    return "rejected"


def clear(paths: Paths) -> str:
    _, store = _files(paths)
    return f"discarded {store.clear()} line(s)"


__all__ = [
    "approve",
    "approve_all",
    "clear",
    "generate",
    "lines_body",
    "reject",
]

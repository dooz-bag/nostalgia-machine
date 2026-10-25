"""FastAPI app: a small "what's on" page and the bump review queue.

Deliberately tiny and read-mostly. It exists because the one thing a semi-strict
bump matcher needs is a fast way for a human to confirm guesses, and because
tuning a 24/7 channel is much easier when you can see what is airing.

The page is server-rendered HTML with no build step, so it works with the same
Python environment as the scheduler.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..catalog import Catalog
from ..config import RootConfig
from ..paths import Paths
from .view import apply_tag, now_playing, pending_tags

_CSS = """
:root { color-scheme: dark; --fg:#f2f2f2; --dim:#9a9a9a; --accent:#7dd3fc; }
* { box-sizing: border-box; }
body { margin:0; background:#0b0b0b; color:var(--fg);
       font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Arial, sans-serif; }
header { padding:20px 28px; border-bottom:1px solid #1f1f1f; display:flex; gap:22px; align-items:baseline; }
header h1 { font-size:17px; margin:0; letter-spacing:.14em; text-transform:uppercase; font-weight:600; }
header nav a { color:var(--dim); text-decoration:none; margin-right:18px; font-size:13px; }
header nav a:hover, header nav a.active { color:var(--fg); }
main { padding:28px; max-width:900px; }
.card { border:1px solid #1f1f1f; border-radius:10px; padding:20px 22px; margin-bottom:18px; }
.label { color:var(--dim); font-size:11px; letter-spacing:.16em; text-transform:uppercase; }
.now-title { font-size:26px; margin:6px 0 2px; font-weight:600; }
.now-sub { color:var(--dim); }
.kind { color:var(--accent); }
table { border-collapse:collapse; width:100%; }
td, th { text-align:left; padding:7px 10px 7px 0; border-bottom:1px solid #161616; font-size:14px; }
th { color:var(--dim); font-weight:500; font-size:11px; letter-spacing:.14em; text-transform:uppercase; }
.mono { font-variant-numeric:tabular-nums; color:var(--dim); }
form { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:12px; }
input, select, button { background:#141414; color:var(--fg); border:1px solid #2a2a2a;
       border-radius:6px; padding:7px 10px; font:inherit; font-size:13px; }
button { cursor:pointer; background:#1d4ed8; border-color:#1d4ed8; }
button:hover { background:#2563eb; }
.empty { color:var(--dim); font-style:italic; }
.stat { display:inline-block; margin-right:26px; }
.stat b { display:block; font-size:20px; font-weight:600; }
"""


def _page(title: str, body: str, *, active: str = "") -> str:
    def nav(href: str, label: str) -> str:
        cls = ' class="active"' if href == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{_CSS}</style></head>
<body>
<header>
  <h1>Nostalgia Machine</h1>
  <nav>{nav("/", "On now")}{nav("/bumps", "Bump review")}</nav>
</header>
<main>{body}</main>
</body></html>"""


def create_app(paths: Paths, config: RootConfig) -> Any:
    """Build the ASGI app. Imported lazily so the core has no FastAPI dependency."""
    try:
        from fastapi import FastAPI, Form, HTTPException
        from fastapi.responses import HTMLResponse, RedirectResponse
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise RuntimeError(
            "the web UI needs extra packages: pip install 'nostalgia-machine[web]'"
        ) from exc

    app = FastAPI(title="Nostalgia Machine", docs_url=None, redoc_url=None)

    def _render(html: str) -> Any:
        return HTMLResponse(html)

    @app.get("/", response_class=HTMLResponse)
    def index() -> Any:
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
                    )
                )

        if state.current is None:
            current_html = '<p class="empty">Nothing scheduled right now.</p>'
        else:
            item = state.current
            current_html = (
                f'<p class="label">{item.kind} &middot; {item.clock} &middot; '
                f'{item.duration_s:.0f}s</p>'
                f'<h2 class="now-title">{_h(item.title)}</h2>'
                f'<p class="now-sub">{_h(item.subtitle)}</p>'
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

        body = f"""
<div class="card">
  <p class="label">{state.day} &middot; {state.now_clock}</p>
  {current_html}
</div>
<div class="card">
  <p class="label">Up next</p>
  {upcoming}
</div>
<div class="card">
  <span class="stat"><b>{state.fallbacks}</b><span class="label">neutral fallbacks</span></span>
  <span class="stat"><b>{state.generated_share:.0%}</b><span class="label">generated bumps</span></span>
</div>"""
        return _render(_page("On now", body, active="/"))

    @app.get("/bumps", response_class=HTMLResponse)
    def bumps() -> Any:
        with Catalog(paths.catalog) as catalog:
            pending = pending_tags(catalog)
            shows = sorted(config.shows)

        if not pending:
            body = '<div class="card"><p class="empty">Every bump has a confirmed subject.</p></div>'
            return _render(_page("Bump review", body, active="/bumps"))

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
        return _render(_page("Bump review", body, active="/bumps"))

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


def _h(value: object) -> str:
    """HTML-escape, since titles come from filenames and user config."""
    from html import escape

    return escape(str(value), quote=True)


__all__ = ["create_app"]

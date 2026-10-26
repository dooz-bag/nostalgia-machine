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

.empty { color: var(--dim); font-style: italic; }

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

@media (max-width: 620px) {
  main, header, footer { padding-left: 16px; padding-right: 16px; }
  .now-title { font-size: 22px; }
  header nav { margin-left: 0; }
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
  <nav>{nav("/", "On now")}{nav("/bumps", "Bump review")}</nav>
</header>
<main>{body}</main>
<footer>Nostalgia Machine &middot; {standard}</footer>
</div>
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
                        card=_card(config),
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

    @app.get("/bumps", response_class=HTMLResponse)
    def bumps() -> Any:
        with Catalog(paths.catalog) as catalog:
            pending = pending_tags(catalog)
            shows = sorted(config.shows)

        if not pending:
            body = '<div class="card"><p class="empty">Every bump has a confirmed subject.</p></div>'
            return _render(_page("Bump review", body, active="/bumps", card=_card(config)))

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
        return _render(_page("Bump review", body, active="/bumps", card=_card(config)))

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


def _card(config: RootConfig) -> str:
    """Which test pattern to draw: ``channel.test_card`` in channel.yaml."""
    mode = getattr(config.channel, "test_card", "ntsc")
    return "pal" if str(mode).lower() == "pal" else "ntsc"


def _h(value: object) -> str:
    """HTML-escape, since titles come from filenames and user config."""
    from html import escape

    return escape(str(value), quote=True)


__all__ = ["create_app"]

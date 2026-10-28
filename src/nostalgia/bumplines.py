"""What a generated bump card *says*.

A card used to print a position word and a title ("NEXT / FUTURAMA"), which is
correct and has all the personality of a train departure board. A real [adult swim]
card was a joke told in four seconds: deadpan, terse, a little too honest.

Lines come from a bank (``config/bump-lines.yaml``) that a person approves. A
language model can *propose* lines -- locally, through Ollama -- but nothing it
writes can air until it has passed two gates:

1. **The truth guard** (:func:`check_line`), which is the same position table the
   matcher enforces, applied to the words. A card for ``AFTER`` may say the episode
   is over; it may not say anything is coming up. A card for ``NEXT`` may not say
   "you just watched". The model has no idea what moment a card appears in, and its
   first draft of a ``NEXT`` line was "you are now watching: <show>" -- exactly the
   false claim this project already had to fix once, in the renderer.
2. **A human**, through the approval queue.

Generation therefore happens offline and is cached, which also keeps the channel
deterministic: the same date builds the same break whether or not a model is
running.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import yaml

from .rng import rng_for

POSITIONS = ("INTO", "NEXT", "AFTER", "OUT", "ANY")

#: The placeholder for the show a card is about.
SHOW = "{show}"

#: A line longer than this stops being a bump and becomes a paragraph. Counted
#: without the placeholder, because the show's own title is not part of the joke.
MAX_WORDS = 16
MAX_CHARS = 110

# -- the truth guard ------------------------------------------------------------

#: Phrases that say the show is over.
PAST = (
    "watched", "that was", "has ended", "has concluded", "is finished", "has finished",
    "is over", "it is over", "concluded", "is done", "thank you for watching",
    "thanks for watching",
)
#: Phrases that say something is about to start.
FUTURE = (
    "stay tuned", "coming up", "up next", "next", "about to", "begins", "starts",
    "starting", "shortly", "soon", "later", "ahead", "after this", "in a moment",
    "brace for", "get comfortable", "up shortly", "standing by", "stand by",
    "following", "will be on", "is on next",
)
#: Phrases that say the show is coming back.
RETURN = (
    "return", "returning", "back to", "we are back", "we're back", "welcome back",
    "resume", "resumes", "resuming", "be right back",
)
#: Phrases that claim the show is playing *right now*, which no card can know: it is
#: always shown between programmes.
PRESENT = ("now watching", "now playing", "currently playing", "currently watching", "on now")

#: What each position may NOT say. ``OUT`` is shown at a mid-roll break *and* at the
#: card that resumes it (see ``bumps.PERMITTED_MOMENTS``), so it may talk about a
#: return but about nothing that has ended or is about to start.
FORBIDDEN: dict[str, tuple[tuple[str, ...], ...]] = {
    "INTO": (PAST, RETURN, PRESENT),
    "NEXT": (PAST, RETURN, PRESENT),
    "AFTER": (FUTURE, RETURN, PRESENT),
    "OUT": (PAST, FUTURE, PRESENT),
    "ANY": (PAST, FUTURE, RETURN, PRESENT),
}

# `%` and a backslash are special inside ffmpeg's drawtext, so a line containing
# one would render as something other than what was approved.
_BAD_CHARS = re.compile(r"[!#@%\\]|[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_WORD = re.compile(r"[a-z0-9']+")


def _contains(text: str, phrases: Iterable[str]) -> str | None:
    """The first phrase present as whole words, else ``None``.

    Whole-word matching matters: "next" must not fire inside "nextdoor", and
    "return" must not fire inside "returns" unless that is what is meant -- so a
    phrase is matched on word boundaries, with plurals/endings left to the list.
    """
    lowered = f" {' '.join(_WORD.findall(text.lower()))} "
    for phrase in phrases:
        needle = f" {' '.join(_WORD.findall(phrase.lower()))} "
        if needle in lowered:
            return phrase
    return None


def check_line(
    position: str,
    text: str,
    *,
    other_titles: Sequence[str] = (),
) -> list[str]:
    """Why *text* may not be a *position* card. Empty means it is acceptable.

    *other_titles* are show titles the line must not name: the only show a line may
    refer to is the one the ``{show}`` placeholder stands for, so a literal title in
    the text would be wrong for every other show it gets rendered for.
    """
    position = position.upper()
    problems: list[str] = []
    if position not in FORBIDDEN:
        return [f"unknown position {position!r}"]

    stripped = text.strip()
    if not stripped:
        return ["empty"]
    if _BAD_CHARS.search(stripped):
        problems.append("exclamation marks, hashtags and emoji are not this channel's voice")
    if stripped.count(SHOW) > 1:
        problems.append("names the show more than once")

    bare = stripped.replace(SHOW, " ")
    words = _WORD.findall(bare.lower())
    if len(words) > MAX_WORDS:
        problems.append(f"{len(words)} words; a bump is {MAX_WORDS} at most")
    if len(stripped) > MAX_CHARS:
        problems.append(f"{len(stripped)} characters; {MAX_CHARS} at most")

    for group in FORBIDDEN[position]:
        hit = _contains(bare, group)
        if hit:
            problems.append(f"{position} card says {hit!r}, which is false at that moment")
            break

    for title in other_titles:
        if title and _contains(bare, [title]):
            problems.append(f"names {title!r} literally; use {SHOW} so it fits any show")
            break
    return problems


# -- the bank ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineBank:
    """Approved lines, by position, plus any written for one specific show."""

    lines: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    per_show: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)

    def for_position(self, position: str, show: str | None = None) -> list[str]:
        """Every line usable for *position*, show-specific ones first-class.

        A line written for one show is included only for that show; the general
        lines are included for everyone.
        """
        position = position.upper()
        pool = list(self.lines.get(position, ()))
        if show:
            pool.extend(self.per_show.get(show, {}).get(position, ()))
        return pool

    def count(self) -> int:
        return sum(len(v) for v in self.lines.values()) + sum(
            len(lines) for table in self.per_show.values() for lines in table.values()
        )

    def __bool__(self) -> bool:
        return self.count() > 0


def load_bank(path: Path | None) -> LineBank:
    """Read ``bump-lines.yaml``. A missing file is an empty bank, not an error."""
    if path is None or not Path(path).is_file():
        return LineBank()
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    def clean(items) -> tuple[str, ...]:
        return tuple(str(item).strip() for item in (items or []) if str(item).strip())

    lines = {str(pos).upper(): clean(items) for pos, items in (raw.get("lines") or {}).items()}
    per_show = {
        str(show): {str(pos).upper(): clean(items) for pos, items in (table or {}).items()}
        for show, table in (raw.get("per_show") or {}).items()
    }
    return LineBank(lines=lines, per_show=per_show)


def render_line(text: str, show_title: str | None) -> str:
    """Substitute the show into a line. A show-less line is returned as is."""
    return text.replace(SHOW, show_title or "").strip() if SHOW in text else text.strip()


def wrap(text: str, width: int) -> list[str]:
    """Greedy word wrap, on the *rendered* text, for the card's drawtext lines."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def pick_lines(
    bank: LineBank,
    position: str,
    count: int,
    *,
    show: str | None = None,
    seed_parts: Sequence[object] = (),
) -> list[str]:
    """*count* distinct lines for a position, chosen deterministically.

    Seeded so that the same channel always renders the same cards: a re-render must
    not quietly change what a bump says.
    """
    pool = sorted(set(bank.for_position(position, show)))
    if not pool or count <= 0:
        return []
    order = list(pool)
    rng_for("bump-lines", position.upper(), show or "*", *seed_parts).shuffle(order)
    return order[: min(count, len(order))]


# -- writing approved lines ---------------------------------------------------------


def append_line(path: Path, position: str, text: str, *, show: str | None = None) -> bool:
    """Add one approved line, keeping the file's comments. False if already present.

    ``ruamel`` is used for the same reason the config editor uses it: this file is
    meant to be read and edited by hand, and a writer that discards its comments
    would make the two ways of editing fight each other.
    """
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    position = position.upper()
    yaml_rw = YAML()
    yaml_rw.preserve_quotes = True
    yaml_rw.width = 4096
    yaml_rw.indent(mapping=2, sequence=4, offset=2)

    document = (
        yaml_rw.load(path.read_text(encoding="utf-8")) if path.is_file() else None
    ) or CommentedMap()

    if show:
        table = document.setdefault("per_show", CommentedMap()).setdefault(show, CommentedMap())
    else:
        table = document.setdefault("lines", CommentedMap())
    bucket = table.setdefault(position, CommentedSeq())
    if _normalise(text) in {_normalise(str(existing)) for existing in bucket}:
        return False
    bucket.append(text)

    path.parent.mkdir(parents=True, exist_ok=True)
    import io

    buffer = io.StringIO()
    yaml_rw.dump(document, buffer)
    path.write_text(buffer.getvalue(), encoding="utf-8")
    return True


def _normalise(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


# -- the candidate queue --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    id: str
    position: str
    text: str
    show: str | None = None
    created: str = ""


class CandidateStore:
    """Lines a model has proposed and a person has not yet judged.

    A flat JSON file under ``data/``: it is runtime state (what is waiting for a
    decision), not configuration, so it is never committed.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> list[dict]:
        try:
            return list(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return []

    def _write(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def pending(self, position: str | None = None) -> list[Candidate]:
        rows = self._read()
        return [
            Candidate(**row)
            for row in rows
            if position is None or row.get("position") == position.upper()
        ]

    def add(self, position: str, text: str, *, show: str | None = None) -> Candidate | None:
        """Queue a line unless it is already queued. Returns it, or ``None``."""
        rows = self._read()
        key = (position.upper(), show, _normalise(text))
        if any((r["position"], r.get("show"), _normalise(r["text"])) == key for r in rows):
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        candidate = Candidate(
            id=f"{stamp}-{len(rows)}", position=position.upper(), text=text.strip(),
            show=show, created=stamp,
        )
        rows.append({k: getattr(candidate, k) for k in ("id", "position", "text", "show", "created")})
        self._write(rows)
        return candidate

    def remove(self, candidate_id: str) -> Candidate | None:
        rows = self._read()
        keep = [r for r in rows if r["id"] != candidate_id]
        gone = [r for r in rows if r["id"] == candidate_id]
        if not gone:
            return None
        self._write(keep)
        return Candidate(**gone[0])

    def clear(self) -> int:
        rows = self._read()
        self._write([])
        return len(rows)


# -- asking a local model ---------------------------------------------------------------

POSITION_BRIEF = {
    "INTO": (
        "It appears just BEFORE the show starts, so it may announce that the show is "
        "about to begin. It must NOT say anything has ended or is returning."
    ),
    "NEXT": (
        "It announces which show plays AFTER the current one. It must NOT say anything "
        "has been watched, has ended, or is returning."
    ),
    "AFTER": (
        "It appears just AFTER the show has ended, so it may only say the show is over "
        "or has been watched. It must NOT mention anything coming up, next, soon, or "
        "returning."
    ),
    "OUT": (
        "It appears at a break in the middle of the show, and the show is coming back. "
        "It may talk about returning or resuming. It must NOT say anything has ended or "
        "is coming up next."
    ),
    "ANY": (
        "It can appear at any moment, so it must say nothing about what just ended, what "
        "is next, or any show coming back. It is just a dry remark. It does not use {show}."
    ),
}


class ModelError(RuntimeError):
    pass


def build_prompt(
    position: str,
    *,
    examples: Sequence[str],
    count: int,
    show_title: str | None = None,
) -> str:
    position = position.upper()
    if position == "ANY":
        show_line = f"Do not mention any show and do not use {SHOW}."
    elif show_title:
        show_line = (
            f"The show this card is about is called {show_title!r}. In the text, always write "
            f"the placeholder {SHOW} instead of the title, so the line can be reused. You may "
            f"allude to the show's characters or premise, but never name a different show."
        )
    else:
        show_line = (
            f"The card is about some show you know nothing about. Write {SHOW} wherever its "
            f"name would go, and say nothing specific about it, so the line fits any show."
        )
    shots = "\n".join(f"- {line}" for line in examples[:8]) or "- (none yet)"
    return (
        "You write the on-screen text of late-night cartoon channel 'bumps': a short line "
        "of white text on a black card, shown for four seconds between programmes.\n"
        "The voice is deadpan, terse and anticlimactic, like a bored announcer who is "
        "slightly too honest. Dry. Specific beats generic. Never cute, never excited.\n\n"
        "Hard rules:\n"
        f"- At most {MAX_WORDS} words. No exclamation marks, hashtags or emoji.\n"
        "- Plain lowercase sentences are fine.\n"
        f"- {POSITION_BRIEF[position]}\n"
        f"- {show_line}\n\n"
        "Lines already approved, for the VOICE only. Write new ones, do not repeat them:\n"
        f"{shots}\n\n"
        f"Write {count} NEW lines. Reply with JSON only: {{\"lines\": [\"...\", \"...\"]}}"
    )


def parse_lines(raw: str) -> list[str]:
    """Pull lines out of a model reply, tolerating the ways models get JSON wrong."""
    text = raw.strip()
    candidates: list[str] = []
    match = re.search(r"\{.*\}|\[.*\]", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            items = data.get("lines") if isinstance(data, dict) else data
            candidates = [str(item) for item in (items or []) if isinstance(item, (str, int, float))]
        except ValueError:
            candidates = []
    if not candidates:
        for line in text.splitlines():
            line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip().strip('",')
            if line and not line.startswith(("{", "}", "[", "]")):
                candidates.append(line)
    return [c.strip().strip('"').strip() for c in candidates if c.strip()]


#: Where a local Ollama is usually found: from the host, then from inside a container.
OLLAMA_FALLBACKS = ("http://localhost:11434", "http://host.docker.internal:11434")


def find_ollama(preferred: str | None = None, *, timeout: float = 3.0) -> tuple[str, list[str]]:
    """The first reachable Ollama and the models it has.

    The configured address is tried first, then the usual ones. The same config has
    to work from the host (``localhost``) and from a container
    (``host.docker.internal``), and a setting that only suits one of them is a trap
    the first time it is run from the other.
    """
    tried: list[str] = []
    for url in dict.fromkeys([u for u in (preferred, *OLLAMA_FALLBACKS) if u]):
        try:
            return url, list_models(url, timeout=timeout)
        except ModelError as exc:
            tried.append(str(exc))
    raise ModelError(
        "no local model server found. Start Ollama (https://ollama.com) and pull a "
        "model, or set bumps.generated.ollama_url. Tried: " + "; ".join(tried)
    )


def list_models(url: str, *, timeout: float = 5.0) -> list[str]:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=timeout) as response:
            return [m["name"] for m in json.load(response).get("models", [])]
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise ModelError(f"cannot reach Ollama at {url}: {exc}") from exc


def ask_model(
    url: str,
    model: str,
    prompt: str,
    *,
    temperature: float = 0.95,
    timeout: float = 180.0,
) -> str:
    body = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": temperature, "num_predict": 600},
        }
    ).encode()
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/generate", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return str(json.load(response).get("response", ""))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise ModelError(f"model request failed: {exc}") from exc


@dataclass
class Proposal:
    accepted: list[str] = field(default_factory=list)
    rejected: list[tuple[str, list[str]]] = field(default_factory=list)


def propose(
    position: str,
    *,
    bank: LineBank,
    url: str,
    model: str,
    count: int = 12,
    show: str | None = None,
    show_title: str | None = None,
    other_titles: Sequence[str] = (),
    existing: Iterable[str] = (),
    ask=None,
) -> Proposal:
    """Ask the model for lines and return only those that pass the guard.

    Anything the guard rejects is returned with its reasons rather than dropped
    silently, because the reasons are the useful part: a model that keeps writing
    "you are now watching" on ``NEXT`` cards is telling you its prompt is wrong.
    """
    position = position.upper()
    prompt = build_prompt(
        position, examples=bank.for_position(position, show), count=count, show_title=show_title
    )
    # Resolved at call time, not as a default argument: a default is bound when the
    # function is defined, which would make the model impossible to substitute.
    reply = (ask or ask_model)(url, model, prompt)
    seen = {_normalise(line) for line in bank.for_position(position, show)}
    seen.update(_normalise(line) for line in existing)

    result = Proposal()
    for text in parse_lines(reply):
        if show_title and position != "ANY":
            # The model often writes the title out despite being told not to. Turn
            # it back into the placeholder so the line is reusable.
            text = re.sub(re.escape(show_title), SHOW, text, flags=re.I)
        text = text.strip().rstrip(".") + "." if text.strip() and text.strip()[-1] not in ".?" else text.strip()
        if _normalise(text) in seen:
            continue
        problems = check_line(position, text, other_titles=other_titles)
        if problems:
            result.rejected.append((text, problems))
            continue
        seen.add(_normalise(text))
        result.accepted.append(text)
    return result


__all__ = [
    "Candidate",
    "CandidateStore",
    "FORBIDDEN",
    "LineBank",
    "MAX_WORDS",
    "ModelError",
    "POSITIONS",
    "Proposal",
    "SHOW",
    "append_line",
    "ask_model",
    "build_prompt",
    "check_line",
    "find_ollama",
    "list_models",
    "load_bank",
    "parse_lines",
    "pick_lines",
    "propose",
    "render_line",
    "wrap",
]

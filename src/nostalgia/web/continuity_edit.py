"""Bump placement, edited as *break positions* rather than as raw segments.

``config/continuity.yaml`` already decides how many cards air and where, but its
shape is a flat list of interleaved ``bump`` / ``ad_pod`` / ``episode`` segments.
That is the right thing for the planner to consume and the wrong thing to ask a
person to edit: "how many bumps play around this show?" is a question about
*positions*, and answering it by inserting YAML list items is how a break template
gets silently broken.

So this module translates. The editor sees one row per attachment point:

    BEFORE the show starts   (INTO)
    AFTER  the show ends     (AFTER)
    NEXT show is coming up   (NEXT)
    MID-BREAK #1..#n         (OUT then INTO, either side of a pod)

and each row carries a count plus a maximum length. Reading rebuilds those counts
from the real segments; writing regenerates the segment list while preserving
everything else in the file -- the pod shares, the ``mid`` break points, comments.

The mapping is deliberately not lossless in one direction: segments that the
positional model cannot describe (two pods in a row, a bump after the closing
"NEXT") are *kept*, because dropping a hand-written segment would be a silent
change to the channel. They are reported as-is instead.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

SLOT_FAMILIES: tuple[str, ...] = ("short", "half", "hour")
"""Slot families a template is named after, in increasing length."""

FAMILY_LABELS: dict[str, str] = {
    "short": "Short slots (15 min) — ~11-minute shows",
    "half": "Half-hour slots (30 min) — ~22-minute shows",
    "hour": "Hour blocks (60 min) — movies and marathons",
}

#: Attachment points, in the order they appear in a slot.
POSITIONS: tuple[str, ...] = ("INTO", "AFTER", "NEXT")

POSITION_LABELS: dict[str, str] = {
    "INTO": "Before the show starts",
    "AFTER": "After the show ends",
    "NEXT": "Before the next show (the \"up next\" card)",
    "OUT": "Going into the mid-break",
}

#: The planner's cap on a single card. 16s because the classic [adult swim] text
#: cards are almost all 15.0-15.4s; anything longer is a sketch, not a bump.
MAX_CARD_SECONDS = 16.0

#: How many mid-break card slots to offer. The template can express more, but a
#: break with more than a couple of mid-rolls is not a 30-minute slot any more.
MAX_MID = 3


class ContinuityEditError(ValueError):
    """A submission that cannot become a break template."""


@dataclass
class MidBreak:
    """One mid-roll: the cards either side of its pod."""

    out: int = 1
    into: int = 1
    percent: float = 50.0
    pod_seconds: int = 60

    def total_cards(self) -> int:
        return self.out + self.into


@dataclass
class TemplateForm:
    """What the editor submits for one slot family."""

    family: str
    into: int = 1
    after: int = 1
    next: int = 1
    max_seconds: float = MAX_CARD_SECONDS
    mid: list[MidBreak] = field(default_factory=list)
    mid_touched: bool = False
    """Whether the submission said anything about mid-breaks at all.

    Distinguishes "the user cleared the mid-breaks" from "the caller only sent the
    before/after counts". Without it, a form that never mentioned mid-breaks would
    silently delete a hand-written one along with its comments.
    """

    def total_cards(self) -> int:
        return self.into + self.after + self.next + sum(m.total_cards() for m in self.mid)


# -- reading ---------------------------------------------------------------


def template_for(document: Any, family: str) -> dict[str, Any] | None:
    """The template mapping for *family*, whether or not it has a name yet."""
    continuity = document.get("continuity") if isinstance(document, dict) else None
    if not isinstance(continuity, dict):
        return None
    return continuity.get(f"default-{family}")


def read_form(document: Any, family: str) -> TemplateForm:
    """Count the cards at each attachment point in an existing template."""
    template = template_for(document, family)
    if template is None:
        return TemplateForm(family=family, into=0, after=0, next=0, mid=[])

    segments = template.get("segments") or []
    form = TemplateForm(family=family)
    form.into = form.after = form.next = 0

    for segment in segments:
        if not isinstance(segment, dict) or segment.get("kind") != "bump":
            continue
        if segment.get("max_seconds"):
            form.max_seconds = float(segment["max_seconds"])
        position = str(segment.get("position") or "ANY").upper()
        if position == "INTO":
            form.into += 1
        elif position == "AFTER":
            form.after += 1
        elif position == "NEXT":
            form.next += 1
        # A card with no position, or one this model cannot place, is not counted
        # but is preserved on write.

    form.mid = []
    for slot in template.get("mid") or []:
        if not isinstance(slot, dict):
            continue
        cards = [s for s in (slot.get("segments") or []) if isinstance(s, dict) and s.get("kind") == "bump"]
        pods = [s for s in (slot.get("segments") or []) if isinstance(s, dict) and s.get("kind") == "ad_pod"]
        form.mid.append(
            MidBreak(
                out=sum(1 for c in cards if str(c.get("position")).upper() == "OUT"),
                into=sum(1 for c in cards if str(c.get("position")).upper() == "INTO"),
                percent=float(slot.get("after_percent") or 50.0),
                pod_seconds=int(pods[0].get("seconds") or 60) if pods else 60,
            )
        )
    return form


def read_all(document: Any) -> list[TemplateForm]:
    return [read_form(document, family) for family in SLOT_FAMILIES]


# -- writing ---------------------------------------------------------------


def apply_form(
    template: dict[str, Any],
    form: TemplateForm,
    *,
    pod_shares: tuple[float, float],
) -> dict[str, Any]:
    """Rebuild *template* with *form*'s counts, preserving everything else.

    ``pod_shares`` are the opening and closing pod fractions, taken from the
    template's own existing pods so a hand-tuned balance is not reset just because
    the number of cards changed.
    """
    opening, closing = pod_shares
    max_seconds = float(form.max_seconds or MAX_CARD_SECONDS)

    # Pod shares are not editable here, so values are carried over unchanged. Reuse
    # the original scalar objects rather than re-formatting them: a rebuilt
    # `share: 0.3` used to replace the file's `share: 0.30`, so every save produced
    # a spurious diff in a file that is mostly hand-written prose.
    originals = [
        segment.get("share")
        for segment in (template.get("segments") or [])
        if isinstance(segment, dict) and segment.get("kind") == "ad_pod"
    ]

    def share(value: float, index: int) -> Any:
        carried = originals[index] if index < len(originals) else None
        if isinstance(carried, float) and abs(carried - value) < 1e-9:
            return carried
        return round(value, 3)

    def card(position: str) -> dict[str, Any]:
        subject = "next" if position == "NEXT" else "this"
        return {
            "kind": "bump",
            "subject": subject,
            "position": position,
            # An int when the value is whole, so a saved file reads `16`, not
            # `16.0` -- the same value, less noise in a hand-edited document.
            "max_seconds": int(max_seconds) if float(max_seconds).is_integer() else round(max_seconds, 1),
        }

    segments: list[dict[str, Any]] = [
        {"kind": "ad_pod", "share": share(opening, 0)}
    ]
    segments += [card("INTO") for _ in range(form.into)]
    segments.append({"kind": "episode"})
    segments += [card("AFTER") for _ in range(form.after)]
    segments.append({"kind": "ad_pod", "share": share(closing, len(originals) - 1)})
    segments += [card("NEXT") for _ in range(form.next)]

    mid = [
        {
            "after_percent": int(m.percent) if float(m.percent).is_integer() else round(m.percent, 1),
            "segments": [
                *[card("OUT") for _ in range(m.out)],
                {"kind": "ad_pod", "seconds": int(m.pod_seconds)},
                *[card("INTO") for _ in range(m.into)],
            ],
        }
        for m in form.mid
        if m.out or m.into
    ]

    # The comment that introduces the *next* template is stored at the very end of
    # this one, so it has to be captured while the original segments still exist.
    trailing = _last_comment(template.get("mid"))

    # Mutated in place, never reassigned. A `CommentedSeq` carries comments, and
    # assigning a plain list over one silently drops them.
    _replace_contents(template["segments"], segments)
    if form.mid_touched or mid:
        # An empty `mid:` describes the same thing as no `mid:` at all, but only
        # the absent key can hold the trailing comment -- a sequence needs an item
        # to attach it to, and an empty one prints the comment before its `[]`.
        anchor = template["segments"]
        if mid:
            _replace_contents(template["mid"], mid)
            anchor = template["mid"]
        else:
            template.pop("mid", None)
        # Only restored when the original was actually destroyed. Re-attaching in
        # the other case left two copies of the "# 60-minute block" heading.
        if trailing is not None:
            _keep_end_comment(anchor, trailing)
    return template


def _as_commented(value: Any) -> Any:
    """Plain dicts/lists become ruamel comments-carrying types.

    A plain ``dict`` appended to a ``CommentedSeq`` cannot hold a comment slot, so
    the trailing heading would have nowhere to live after a rebuild.
    """
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    if isinstance(value, dict) and not isinstance(value, CommentedMap):
        return CommentedMap({key: _as_commented(item) for key, item in value.items()})
    if isinstance(value, list) and not isinstance(value, CommentedSeq):
        return CommentedSeq([_as_commented(item) for item in value])
    return value


def _keep_end_comment(sequence: Any, entry: Any) -> bool:
    """Put *entry* back so it prints after *sequence*. False if it could not fit.

    ``ca.end`` is not printed for a block sequence (verified against ruamel 0.18),
    so the comment goes on a key inside the last item -- which is where YAML put it
    to begin with. The key must hold a **scalar**: a comment parked on a
    mapping-or-list key prints its value, then the comment, then the rest of the
    nested block, mangling the document.
    """
    from ruamel.yaml.comments import CommentedMap

    # Walk to the very last value *printed* under the sequence, remembering the
    # holder of each step. Taking any earlier scalar -- such as `after_percent` in a
    # `mid:` entry -- puts the comment in the middle of the mapping, because the
    # nested block it precedes still prints afterwards. Only the deepest last
    # scalar ends the template.
    #
    # The holder is tracked rather than searched for afterwards: small ints are
    # interned, so looking a value back up by identity finds the *first* segment
    # with `max_seconds: 16` rather than the last one.
    node: Any = sequence
    holder: Any = None
    key: Any = None
    while True:
        if isinstance(node, CommentedMap) and node:
            holder, key = node, next(reversed(node))
        elif isinstance(node, list) and node:
            holder, key = node, len(node) - 1
        else:
            break
        node = holder[key] if isinstance(holder, list) else node[key]

    if not isinstance(holder, CommentedMap):
        return False
    # Reuse the original token, just re-homed.
    holder.ca.items[key] = entry
    return True


def _replace_contents(sequence: Any, items: list[Any]) -> None:
    """Replace a sequence's contents, keeping the comments that follow it.

    A comment written *below* the last segment of a template is not stored on the
    sequence at all -- YAML attaches it to the final scalar, so
    "# 60-minute block (movies, specials, marathons)" above ``default-hour`` lives
    on ``default-half.mid[0].segments[2].max_seconds``. Regenerating either
    sequence therefore drops the *next* section's heading.

    Only end-of-line comments that carry no description of their own item are
    carried across; anything attached to a key is discarded, because the segments
    it described no longer exist and re-attaching it would annotate the wrong one.
    """
    from ruamel.yaml.comments import CommentedSeq

    del sequence[:]
    for item in items:
        sequence.append(_as_commented(item))

    if not isinstance(sequence, CommentedSeq):  # pragma: no cover - always is
        return
    # Per-item comments describe items that no longer exist, so they go. The
    # comment that follows a whole template is captured and restored exactly once
    # by `apply_form`; doing it here as well produced two copies of the
    # "# 60-minute block" heading.
    sequence.ca.items.clear()


def _last_comment(node: Any) -> Any:
    """The trailing comment slot on the last entry reachable from *node*.

    ruamel attaches a comment written *after* a nested value to that value's
    **key** inside the parent mapping -- ``{"max_seconds": [None, None, token,
    None]}`` -- not to the value itself and not to the sequence. So the search
    descends the last item and, at a mapping, takes the last key's slot.
    """
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    if isinstance(node, CommentedSeq):
        return _last_comment(node[-1]) if node else None
    if isinstance(node, CommentedMap):
        if not node:
            return None
        for key in reversed(list(node.keys())):
            entry = node.ca.items.get(key)
            if entry and entry[2] is not None:
                return entry
        return _last_comment(node[next(reversed(node))])
    ca = getattr(node, "ca", None)
    return ca.items.get(None) if ca is not None else None


def _attach_trailing(item: Any, entry: Any) -> None:
    """Re-attach a found trailing comment slot to the last entry of *item*."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    if isinstance(item, CommentedSeq) and item:
        _attach_trailing(item[-1], entry)
        return
    if isinstance(item, CommentedMap) and item:
        item.ca.items[list(item.keys())[-1]] = entry
        return
    ca = getattr(item, "ca", None)
    if ca is not None:
        ca.items[None] = entry


def pod_shares(template: dict[str, Any] | None) -> tuple[float, float]:
    """Existing opening/closing pod fractions, defaulting to 0.30 / 0.66."""
    shares = [
        float(segment.get("share") or 0)
        for segment in ((template or {}).get("segments") or [])
        if isinstance(segment, dict) and segment.get("kind") == "ad_pod" and segment.get("share")
    ]
    if len(shares) >= 2:
        return shares[0], shares[-1]
    if len(shares) == 1:
        return shares[0], max(0.1, 1.0 - shares[0])
    return 0.30, 0.66


# -- form parsing ----------------------------------------------------------


def parse_form(
    family: str,
    submitted: dict[str, str | None],
) -> TemplateForm:
    """Turn the submitted strings into a :class:`TemplateForm`.

    Counts are clamped rather than rejected: "how many cards" is a quantity, and
    a number outside the offered range is a typo the page can correct silently.
    What *is* rejected is a count that would break the channel -- a template with
    no episode segment at all.
    """
    def count(name: str, low: int, high: int) -> int:
        raw = (submitted.get(name) or "").strip()
        if raw == "":
            return low
        try:
            return max(low, min(high, int(float(raw))))
        except ValueError as exc:
            raise ContinuityEditError(f"{name} must be a whole number") from exc

    # Deliberately no `mid=` here, and the module-level list is copied below:
    # parsing a form must not mutate MAX_MID.
    form = TemplateForm(
        family=family,
        into=count(f"{family}.into", 0, 4),
        after=count(f"{family}.after", 0, 4),
        next=count(f"{family}.next", 0, 4),
        max_seconds=MAX_CARD_SECONDS,
    )

    raw_seconds = (submitted.get(f"{family}.max_seconds") or "").strip()
    if raw_seconds:
        try:
            form.max_seconds = max(2.0, min(60.0, float(raw_seconds)))
        except ValueError as exc:
            raise ContinuityEditError("maximum card length must be a number") from exc

    form.mid = []
    form.mid_touched = any(f"{family}.mid" in key for key in submitted)
    for index in range(MAX_MID):
        # A mid-row the caller never mentions is *unchanged*, not removed. Treating
        # absence as zero silently deleted a hand-written mid-break whenever only
        # the before/after counts were submitted, and took the comments attached to
        # it with it.
        mentioned = any(
            f"{family}.mid{index}.{part}" in submitted for part in ("out", "into")
        )
        if not mentioned:
            continue
        out = count(f"{family}.mid{index}.out", 0, 3)
        into = count(f"{family}.mid{index}.into", 0, 3)
        if not out and not into:
            continue
        percent_raw = (submitted.get(f"{family}.mid{index}.percent") or "").strip()
        try:
            percent = float(percent_raw) if percent_raw else 50.0
        except ValueError as exc:
            raise ContinuityEditError("mid-break position must be a number") from exc
        seconds_raw = (submitted.get(f"{family}.mid{index}.pod") or "").strip()
        try:
            pod = int(float(seconds_raw)) if seconds_raw else 60
        except ValueError as exc:
            raise ContinuityEditError("mid-break pod length must be a whole number") from exc
        form.mid.append(
            MidBreak(
                out=out,
                into=into,
                percent=max(5.0, min(95.0, percent)),
                pod_seconds=max(15, min(300, pod)),
            )
        )
    return form


__all__ = [
    "ContinuityEditError",
    "FAMILY_LABELS",
    "MAX_CARD_SECONDS",
    "MAX_MID",
    "MidBreak",
    "POSITIONS",
    "POSITION_LABELS",
    "SLOT_FAMILIES",
    "TemplateForm",
    "apply_form",
    "parse_form",
    "pod_shares",
    "read_all",
    "read_form",
    "template_for",
]

"""Report how much a day's playout is being cut up.

Two numbers matter, and both are symptoms of the same bug:

* **trimmed spots** -- a commercial whose played length is shorter than the file.
  A handful is normal (a 32 s spot in a 30 s pod). A hundred means the pod filler
  is reaching for files that cannot fit and cutting them mid-word.
* **sub-second black cards** -- a ``lavfi`` colour source shorter than a second.
  These are not cards; they are one or two frames of black between two spots, and
  they read as a flash. The target is zero.

Usage::

    python scripts/check_smoothing.py playout/*.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

#: A card shorter than this is a discontinuity, not a card. Mirrors
#: ``nostalgia.ads.BLACK_CARD_MIN_MS``.
SUB_SECOND_MS = 1_000


def _ms(stamp: str) -> int:
    return int(datetime.fromisoformat(stamp).timestamp() * 1000)


def _duration_ms(item: dict) -> int:
    return _ms(item["finish"]) - _ms(item["start"])


def _is_black_card(item: dict) -> bool:
    source = item.get("source") or {}
    return source.get("source_type") == "lavfi" and "color=c=black" in str(
        source.get("params", "")
    )


def _file_durations(catalog_path: Path) -> dict[str, int]:
    """Duration of every catalogued file, keyed by its basename.

    The playout JSON carries a container path (``/app/media/...``) while the
    catalog carries the host path, so the basename is the only join key that
    survives both.
    """
    import sqlite3

    out: dict[str, int] = {}
    if not catalog_path.is_file():
        return out
    with sqlite3.connect(catalog_path) as connection:
        for path, duration in connection.execute(
            "SELECT path, duration_ms FROM media WHERE duration_ms IS NOT NULL"
        ):
            out[Path(path).name] = int(duration)
    return out


def report(paths: list[Path], catalog_path: Path) -> int:
    durations = _file_durations(catalog_path)
    total_trimmed = 0
    total_cards = 0
    total_short_cards = 0
    worst: list[tuple[int, str]] = []

    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload["items"] if isinstance(payload, dict) else payload
        trimmed = 0
        cards = 0
        short = 0
        for item in items:
            played = _duration_ms(item)
            if _is_black_card(item):
                cards += 1
                if played < SUB_SECOND_MS:
                    short += 1
                    worst.append((played, f"{path.name} {item['id']}"))
                continue
            name = Path(str((item.get("source") or {}).get("path", ""))).name
            full = durations.get(name)
            if full and played < full - 1_000:
                trimmed += 1
        total_trimmed += trimmed
        total_cards += cards
        total_short_cards += short
        print(
            f"{path.name[:8]}  items={len(items):4d}  trimmed={trimmed:3d}  "
            f"black_cards={cards:3d}  sub_second={short:3d}"
        )

    print()
    print(f"trimmed spots      : {total_trimmed}")
    print(f"black cards        : {total_cards}")
    print(f"sub-second cards   : {total_short_cards}")
    for played, where in sorted(worst)[:10]:
        print(f"  {played:5d} ms  {where}")
    return 0 if total_short_cards == 0 else 1


def main(argv: list[str]) -> int:
    args = [Path(a) for a in argv[1:]]
    catalog = Path("data/catalog.sqlite")
    if not args:
        args = sorted(Path("playout").glob("*.json"))
    if not args:
        print("no playout files found", file=sys.stderr)
        return 2
    return report(args, catalog)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

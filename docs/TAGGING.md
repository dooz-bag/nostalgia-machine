# Tagging bumps

Bump matching is only as good as what the scheduler knows about each card. There
are three ways to tell it, in increasing order of precision.

## 1. Folder convention (lowest effort)

```
media/bumps/aqua-teen-hunger-force/card-014.mp4
```

The folder name is the show id. Files directly in `media/bumps/` and in the
reserved folders `neutral/`, `_review/` and `generated/` are *not* treated as
shows.

Position can come from the filename — `into`, `out`, `next`, `after`, `bumper`,
`return`, `stay` are recognised. Era comes from a year anywhere in a parent
folder name:

```
media/bumps/aqua-teen-hunger-force/2003/into-01.mp4   -> INTO, 2003
```

This alone is enough to make a library sound right.

## 2. Sidecar (exact, travels with the file)

Write `<file>.tags.json` next to the media:

```json
{
  "subject": "aqua-teen-hunger-force",
  "position": "AFTER",
  "era": 2003,
  "source": "manual",
  "confidence": 1.0,
  "confirmed": true
}
```

Fields:

| Field | Values | Notes |
|---|---|---|
| `subject` | show id, or `neutral` | omit for "unknown" |
| `position` | `INTO` `OUT` `NEXT` `AFTER` `ANY` | omit if unclear |
| `era` / `era_to` | year | a single year is usually enough |
| `family` | e.g. `comedy` | helps match when the exact show is absent |
| `source` | `manual` `folder` `ocr` `generated` | provenance, shown in the UI |
| `confidence` | 0–1 | below ~0.5 gets reviewed |
| `confirmed` | bool | set `true` once a human has agreed |

Sidecars are the durable record: they survive a wiped catalog and follow the media
if you move it. Both `nostalgia tag` and the web review queue write them.

## 3. The review queue (recommended for a big library)

```powershell
.\run.ps1 web
# http://127.0.0.1:8099/bumps
```

Every bump whose subject is not confirmed appears once, with:

* the folder-relative filename (so `aqua-teen-hunger-force/after-01.mkv` reads as
  what it is);
* the current guess, where it came from, and its confidence;
* a dropdown pre-selected to the guess, a position dropdown, and an era box.

Confirm in one click; the sidecar is written and the item leaves the queue. A
50-card library takes a few minutes.

From the terminal:

```powershell
nostalgia review
nostalgia tag "bump:aqua-teen-hunger-force/after-01.mkv" --subject aqua-teen-hunger-force --position AFTER --era 2003
```

## How the guess is resolved

Highest precedence first:

1. `.tags.json` sidecar
2. a confirmed row in the catalog database
3. the parent folder name (position and era from the filename and path)
4. nothing → `unknown`, and the card is grouped with neutral cards

`nostalgia doctor` counts untagged bumps, and `nostalgia plan` reports how many
slots fell back to a neutral card, so you can measure whether tagging is paying
off.

## Automatic tagging (optional, off by default)

`autotag` can sample frames from a bump and OCR them to propose a subject. It is
deliberately *advisory*: results land in the review queue at low confidence rather
than being trusted, because [adult swim] cards are frequently abstract and OCR on
a four-second card is easy to get wrong.

The manual paths above are fast enough that auto-tagging is a convenience, not a
requirement. If you want it, install the extra:

```powershell
.\.venv\Scripts\python.exe -m pip install ".[tag]"
# and a Tesseract binary on PATH
```

## Generated cards

Cards produced by `nostalgia bumps` are tagged automatically with
`source: generated` and `confirmed: true`. They are not part of the review queue —
they already say exactly what they were rendered to say — and they never displace a
real bump that names the same show.

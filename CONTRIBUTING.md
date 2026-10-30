# Contributing

Bug reports, accuracy fixes and "this didn't work on my machine" are genuinely
welcome. This is a personal project built to run one channel well, so it is
deliberately not multi-channel, not a Plex plugin, and it will not fetch media for
you — see the scope note in the README before proposing a feature.

## Before you send a change

Run the suite. It is fast and it is the project's main claim to correctness:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

On Linux or macOS, `python -m pytest -q`. Either way the `[web]` extra must be
installed, or the web tests skip and the run tests less than it claims — CI exists
specifically to catch that.

## What to look at first

| File | Why |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | the two seams, and why the schedule is deterministic |
| [`docs/SETUP.md`](docs/SETUP.md) | almost every behaviour, with the reasoning behind it |
| [`docs/OPTIONS.md`](docs/OPTIONS.md) | every configuration key |
| `src/nostalgia/plan.py` | the invariants a change is most likely to break |

## The rules that are not negotiable

These are pinned by tests, and a change that breaks one will be rejected however
well it works otherwise:

- **Determinism.** The same date and config must produce a byte-identical plan. All
  randomness goes through `rng_for(...)`, never `random` or `hash()`.
- **The grid invariant.** A day tiles exactly: no gap, no overlap, ending on the
  boundary it started from.
- **A card may never lie about the running order.** "Next up: X" when X is not next
  is a bug, not a setting.
- **Generated content is marked.** A synthetic card must never pass for an archive
  card.

## Style

Match the file you are editing. Comments explain *why*, not what — if a line looks
pointless, the comment should say what breaks without it. A new behaviour needs a
test that fails when the behaviour is removed; a test that asserts nothing about a
removal is worse than no test.

## Media

**Do not send media.** No shows, bumps, commercials or music. The repository
contains none by design, and a pull request with copyrighted footage will be closed
unmerged. See [`SECURITY.md`](SECURITY.md) for the longer version.

"""Nostalgia Machine -- a deterministic 24/7 [adult swim] channel.

The package is split along two seams:

* **Media in** -- :mod:`nostalgia.catalog` scans folders and records what exists.
* **Playout out** -- :mod:`nostalgia.emit` writes ErsatzTV Next playout JSON.

Everything between the seams (grid, rotation, schedule, continuity, ads) is pure
logic over plain dataclasses, which is what makes the channel reproducible.
"""

__version__ = "0.1.0"

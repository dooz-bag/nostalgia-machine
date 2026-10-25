"""Project path resolution.

Everything is derived from a single project root so the container, the native
Windows script and the tests all agree on where state lives. Overridable with
``NOSTALGIA_ROOT``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Paths:
    root: Path

    @classmethod
    def discover(cls, root: Path | str | None = None) -> "Paths":
        if root is not None:
            return cls(Path(root).resolve())
        env = os.environ.get("NOSTALGIA_ROOT")
        if env:
            return cls(Path(env).resolve())
        # src/nostalgia/paths.py -> project root
        return cls(Path(__file__).resolve().parents[2])

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def playout_dir(self) -> Path:
        return self.root / "playout"

    @property
    def xmltv_dir(self) -> Path:
        return self.root / "xmltv"

    @property
    def ersatztv_dir(self) -> Path:
        """Where ``lineup.json`` and ``channels/<n>/channel.json`` live.

        A directory of its own, rather than the project root, so the streamer can be
        handed one read-only mount containing exactly the files it needs. Named
        ``lineup`` rather than ``ersatztv`` because the native Windows setup keeps
        the ErsatzTV binary in ``ersatztv/`` and the two must not collide.
        """
        return self.root / "lineup"

    @property
    def catalog(self) -> Path:
        return self.data_dir / "catalog.sqlite"

    @property
    def media_root(self) -> Path:
        return self.root / "media"

    def saw(self, path: Path) -> Path:
        """Translate a host path into the path the *streamer* should be told about.

        Inside Docker the mounts do not mirror the repo (``./playout`` becomes
        ``/app/playout`` and the media mount can point anywhere), so writing host
        paths into ``lineup.json`` produces a config that only works on the host.
        Set ``NOSTALGIA_CONTAINER_ROOT=/app`` to remap; unset, this is identity.
        """
        target = os.environ.get("NOSTALGIA_CONTAINER_ROOT")
        if not target:
            return path
        path, root = Path(path).resolve(), self.root.resolve()
        try:
            relative = path.relative_to(root)
        except ValueError:
            return path
        return Path(target) / relative

    def media_for_streamer(self) -> Path:
        """The media root as the streamer sees it."""
        override = os.environ.get("NOSTALGIA_MEDIA_ROOT")
        return Path(override) if override else self.saw(self.media_root)

    def media(self, *parts: str) -> Path:
        return self.media_root.joinpath(*parts)

    def ensure_runtime_dirs(self) -> None:
        for path in (self.data_dir, self.playout_dir, self.xmltv_dir, self.ersatztv_dir):
            path.mkdir(parents=True, exist_ok=True)


__all__ = ["Paths"]

"""The mirror marker: which data roots are copies, and the refusal to write into one."""

from __future__ import annotations

from pathlib import Path

MIRROR_MARKER = ".mirror-of-spark"


class MirrorRootError(RuntimeError):
    """The root is a mirror of the live root; live state must not be written into it."""


def mirror_marker_of(root: str | Path) -> Path | None:
    """The marker file that makes ``root`` part of a mirror, or None when it is not."""
    root = Path(root).expanduser().absolute()
    for directory in (root, *root.parents):
        candidate = directory / MIRROR_MARKER
        if candidate.is_file():
            return candidate
    return None


def refuse_if_mirror(root: str | Path, what: str) -> None:
    """Raise :class:`MirrorRootError` when ``root`` is a mirror."""
    marker = mirror_marker_of(root)
    if marker is not None:
        raise MirrorRootError(
            f"refusing to {what}: {Path(root).expanduser()} is a mirror of the live root "
            f"(marker {marker}). Live state is written on the Spark only, by the trading user."
        )

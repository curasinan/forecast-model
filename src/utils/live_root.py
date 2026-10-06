"""The mirror marker: which data roots are copies, and the refusal to write into one.

After the runtime moved to the Spark (docs/plans/2026-10-05-spark-runtime-move-design.md,
section 6) exactly one root is live -- /srv/quant_data on the Spark -- and every
other ``quant_data`` is a mirror of it that carries a marker file. A program
that writes live state calls :func:`refuse_if_mirror` on the root it is about
to write under, before it creates or changes anything there and before it
talks to the venue. The copy scripts under scripts/spark/ do the opposite
check: they refuse to write into a root that does NOT carry the marker.
"""

from __future__ import annotations

from pathlib import Path

MIRROR_MARKER = ".mirror-of-spark"


class MirrorRootError(RuntimeError):
    """The root is a mirror of the live root; live state must not be written into it."""


def mirror_marker_of(root: str | Path) -> Path | None:
    """The marker file that makes ``root`` part of a mirror, or None when it is not.

    A mirror is a quant-data root that carries the marker, and everything
    below it: the marker is looked for in ``root`` and in every directory
    above it. So a lane (``ibkr_ticks``, ``climate_data``), a ``paper``
    directory and a file named deep inside a mirror are all recognised.
    ``root`` need not exist, and nothing is created.
    """
    root = Path(root).expanduser().absolute()
    for directory in (root, *root.parents):
        candidate = directory / MIRROR_MARKER
        if candidate.is_file():
            return candidate
    return None


def refuse_if_mirror(root: str | Path, what: str) -> None:
    """Raise :class:`MirrorRootError` when ``root`` is a mirror. ``what`` completes
    the sentence "refusing to ..." in the message."""
    marker = mirror_marker_of(root)
    if marker is not None:
        raise MirrorRootError(
            f"refusing to {what}: {Path(root).expanduser()} is a mirror of the live root "
            f"(marker {marker}). Live state is written on the Spark only, by the trading user."
        )

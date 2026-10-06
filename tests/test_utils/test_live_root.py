"""The mirror marker: which roots are copies, and the refusal to write into one."""

from __future__ import annotations

import pytest

from src.utils.live_root import MIRROR_MARKER, MirrorRootError, mirror_marker_of, refuse_if_mirror


def test_the_marker_name_is_the_one_the_copy_scripts_create():
    assert MIRROR_MARKER == ".mirror-of-spark"


def test_a_root_without_a_marker_is_live(tmp_path):
    root = tmp_path / "quant_data" / "ibkr_ticks"
    root.mkdir(parents=True)
    assert mirror_marker_of(root) is None
    refuse_if_mirror(root, "record quotes")  # does not raise


def test_a_marker_in_the_quant_root_marks_every_lane_under_it(tmp_path):
    quant = tmp_path / "quant_data"
    (quant / "ibkr_ticks").mkdir(parents=True)
    (quant / "climate_data").mkdir()
    marker = quant / MIRROR_MARKER
    marker.write_text("mirror of spark:/srv/quant_data\n", encoding="utf-8")

    assert mirror_marker_of(quant) == marker
    assert mirror_marker_of(quant / "ibkr_ticks") == marker
    assert mirror_marker_of(quant / "climate_data") == marker


def test_a_marker_marks_everything_below_it_however_deep(tmp_path):
    quant = tmp_path / "quant_data"
    quant.mkdir()
    marker = quant / MIRROR_MARKER
    marker.write_text("", encoding="utf-8")
    book = quant / "ibkr_ticks" / "paper" / "archive" / "ibkr_paper.db"  # does not exist

    assert mirror_marker_of(book) == marker
    assert mirror_marker_of(book.parent) == marker


def test_a_marker_beside_a_root_does_not_mark_it(tmp_path):
    (tmp_path / "mirror").mkdir()
    (tmp_path / "mirror" / MIRROR_MARKER).write_text("", encoding="utf-8")
    live = tmp_path / "live" / "ibkr_ticks"
    live.mkdir(parents=True)

    assert mirror_marker_of(live) is None
    refuse_if_mirror(live, "record quotes")  # does not raise


def test_a_directory_with_the_marker_name_is_not_a_marker(tmp_path):
    quant = tmp_path / "quant_data"
    (quant / MIRROR_MARKER).mkdir(parents=True)
    assert mirror_marker_of(quant) is None


def test_the_refusal_names_the_root_and_what_was_refused(tmp_path):
    quant = tmp_path / "quant_data"
    quant.mkdir()
    (quant / MIRROR_MARKER).write_text("", encoding="utf-8")

    with pytest.raises(MirrorRootError) as excinfo:
        refuse_if_mirror(quant / "ibkr_ticks", "record quotes")
    message = str(excinfo.value)
    assert "record quotes" in message and "ibkr_ticks" in message and MIRROR_MARKER in message


def test_looking_for_a_marker_creates_nothing(tmp_path):
    missing = tmp_path / "nowhere" / "quant_data" / "ibkr_ticks"
    assert mirror_marker_of(missing) is None
    assert not (tmp_path / "nowhere").exists()

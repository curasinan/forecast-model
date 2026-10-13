"""Tests for collect_news_volume: spike-ratio math, timeline summing, query builder."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from collectors.collect_news_volume import (
    build_query,
    compute_spike_ratio,
    sum_timeline_counts,
)

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


# Spike ratio


def test_spike_ratio_normal():
    # 30 articles over 30 days = 1/day; 9 articles in 72h -> ratio 9.0
    assert compute_spike_ratio(9, 30) == pytest.approx(9.0)


def test_spike_ratio_flat_news():
    assert compute_spike_ratio(3, 30) == pytest.approx(3.0)


def test_spike_ratio_null_safe():
    assert compute_spike_ratio(None, 30) is None
    assert compute_spike_ratio(9, None) is None
    assert compute_spike_ratio(9, 0) is None


# Timeline summing


def _point(hours_ago: float, value: int) -> dict:
    ts = NOW - timedelta(hours=hours_ago)
    return {"date": ts.strftime("%Y%m%dT%H%M%SZ"), "value": value}


def test_sum_timeline_counts_windows():
    payload = {
        "timeline": [{
            "series": "Article Count",
            "data": [
                _point(1, 5),      # inside 72h
                _point(48, 7),     # inside 72h
                _point(71.9, 3),   # inside 72h (boundary)
                _point(100, 11),   # outside 72h, inside 30d
                _point(600, 4),    # outside 72h, inside 30d
            ],
        }]
    }
    result = sum_timeline_counts(payload, NOW)
    assert result is not None
    a72, a30 = result
    assert a72 == 5 + 7 + 3
    assert a30 == 5 + 7 + 3 + 11 + 4


def test_sum_timeline_malformed_points_skipped():
    payload = {
        "timeline": [{
            "data": [
                _point(1, 5),
                {"date": "garbage", "value": 100},   # skipped
                {"value": 2},                        # no date -> skipped
            ],
        }]
    }
    a72, a30 = sum_timeline_counts(payload, NOW)
    assert a72 == 5 and a30 == 5


def test_sum_timeline_bad_shape_returns_none():
    assert sum_timeline_counts({}, NOW) is None
    assert sum_timeline_counts({"timeline": []}, NOW) is None
    assert sum_timeline_counts({"timeline": [{"nodata": 1}]}, NOW) is None


# Query builder


def test_build_query_quotes_phrases_and_ors():
    q = build_query(["Iran nuclear deal", "IAEA"])
    assert q == '("Iran nuclear deal" OR IAEA)'


def test_build_query_single_term():
    assert build_query(["ceasefire"]) == "ceasefire"


def test_build_query_empty():
    assert build_query([]) == ""
    assert build_query(["", "  "]) == ""

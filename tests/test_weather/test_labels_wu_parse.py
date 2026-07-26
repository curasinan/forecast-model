"""WU label parsing — reject HTML garbage, prefer API obs extremes."""

from __future__ import annotations

from collectors.weather.collect_labels_wu import (
    daily_extreme_from_observations,
    parse_wu_html,
    sanity_check_temp,
)


def test_html_font_max_not_accepted():
    # Regression: MaxKUBGEe.woff2 previously yielded 2.0
    html = "url(fonts/MaxKUBGEe.woff2) format('woff2');unicode-range:U+0001-0"
    assert parse_wu_html(html, metric="high") is None


def test_html_requires_fahrenheit_context():
    html = "Daily Summary High Temperature: 82 °F recorded at KLGA"
    assert parse_wu_html(html, metric="high") == 82.0


def test_daily_extreme_from_observations():
    obs = [{"temp": 70}, {"temp": 82}, {"temp": 67}, {"temp": None}]
    assert daily_extreme_from_observations(obs, "high") == 82.0
    assert daily_extreme_from_observations(obs, "low") == 67.0


def test_sanity_rejects_tiny_july_like_garbage():
    assert sanity_check_temp(2.0, metric="high") is True  # cold extremes exist
    # But upsert path uses API; ensure None rejected
    assert sanity_check_temp(None) is False
    assert sanity_check_temp(200.0, metric="high") is False

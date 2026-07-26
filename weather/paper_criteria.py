"""Pre-registered go-live criteria for weather paper trading (anti optional-stopping)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from weather.calibrate import CalibrationReport


@dataclass(frozen=True)
class GoLiveCriteria:
    min_paper_days: int = 90
    min_resolved_buckets: int = 200
    max_ece: float = 0.08
    min_reliability_slope: float = 0.9
    max_reliability_slope: float = 1.1
    require_beat_market_brier: bool = True
    min_brier_margin: float = 0.0  # model_brier + margin < market_brier


@dataclass
class GoLiveVerdict:
    passed: bool
    reasons: list[str]
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "reasons": self.reasons, "metrics": self.metrics}


def evaluate_go_live(
    report: CalibrationReport,
    *,
    paper_days: int,
    criteria: GoLiveCriteria | None = None,
) -> GoLiveVerdict:
    criteria = criteria or GoLiveCriteria()
    reasons: list[str] = []
    if paper_days < criteria.min_paper_days:
        reasons.append(f"paper_days {paper_days} < {criteria.min_paper_days}")
    if report.n < criteria.min_resolved_buckets:
        reasons.append(f"resolved_buckets {report.n} < {criteria.min_resolved_buckets}")
    if report.ece > criteria.max_ece:
        reasons.append(f"ece {report.ece:.4f} > {criteria.max_ece}")
    if report.reliability_slope is None:
        reasons.append("reliability_slope unavailable")
    else:
        if report.reliability_slope < criteria.min_reliability_slope:
            reasons.append(
                f"reliability_slope {report.reliability_slope:.3f} < {criteria.min_reliability_slope}"
            )
        if report.reliability_slope > criteria.max_reliability_slope:
            reasons.append(
                f"reliability_slope {report.reliability_slope:.3f} > {criteria.max_reliability_slope}"
            )
    if criteria.require_beat_market_brier:
        if report.market_brier is None:
            reasons.append("market_brier unavailable")
        elif report.brier + criteria.min_brier_margin >= report.market_brier:
            reasons.append(
                f"model_brier {report.brier:.4f} not better than market {report.market_brier:.4f}"
            )
    return GoLiveVerdict(
        passed=len(reasons) == 0,
        reasons=reasons,
        metrics={
            "paper_days": paper_days,
            **report.to_dict(),
            "criteria": {
                "min_paper_days": criteria.min_paper_days,
                "min_resolved_buckets": criteria.min_resolved_buckets,
                "max_ece": criteria.max_ece,
                "slope_range": [
                    criteria.min_reliability_slope,
                    criteria.max_reliability_slope,
                ],
            },
        },
    )

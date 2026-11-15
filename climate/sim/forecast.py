"""The GTM forecast as it could have been made at a past moment, for the replay."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from climate.emos import EmosModel
from climate.nn.availability import noaa_available_on, seas5_available_on
from climate.paper import freshest_init_by_target, predictive_by_target_month


@dataclass(frozen=True)
class Forecast:
    """mu and sigma per target month at one moment, with what they were built from."""

    predictive: dict[str, tuple[float, float]]
    inits: dict[str, str]
    labels_through: str | None  # the latest printed month the fit saw; None for a fixed model
    model_after_decision: bool = False  # a fixed model fitted after this moment
    notes: tuple[str, ...] = field(default_factory=tuple)


def print_times(months: Iterable[str], ledger: pd.DataFrame | None) -> dict[str, datetime]:
    """When each month's NOAA value became public (ledger first print, else the usual day)."""
    return {m: noaa_available_on(m, ledger) for m in sorted(set(months))}


class PointInTimeForecaster:
    """EMOS forecasts that use only what was public at the decision time.

    The SEAS5 inits are those released by then (the 13th, 12:00 UTC). With no
    fixed model, the EMOS is refitted on the labels printed by then, once per
    new print; with one, it is used as is, and a decision made before
    `model_fitted_at` (or with that time unknown) is flagged, because that fit
    may have seen labels printed after the decision.
    """

    def __init__(self, frame: pd.DataFrame, printed_at: Mapping[str, datetime], *,
                 trend: bool = True, leads: Iterable[int] = range(1, 7),
                 model: EmosModel | None = None, model_fitted_at: datetime | None = None):
        self.frame = frame.reset_index(drop=True)
        self.printed_at = {m: pd.Timestamp(t) for m, t in printed_at.items()}
        self.trend = trend
        self.leads = tuple(leads)
        self.model = model
        self.model_fitted_at = pd.Timestamp(model_fitted_at) if model_fitted_at is not None else None
        init_month = pd.to_datetime(self.frame["init_date"]).dt.strftime("%Y-%m")
        released = {m: pd.Timestamp(seas5_available_on(m)) for m in init_month.unique()}
        self._init_month = init_month.to_numpy()
        self._released = pd.to_datetime(init_month.map(released), utc=True)
        self._fits: dict[str, EmosModel] = {}
        self._cache: dict[tuple, Forecast] = {}

    def _labels_through(self, t: pd.Timestamp) -> str | None:
        public = [m for m, when in self.printed_at.items() if when <= t]
        return max(public) if public else None

    def _fit(self, through: str) -> EmosModel:
        if through not in self._fits:
            work = self.frame.copy()
            months = work["target_month"].astype(str)
            unpublished = ~months.map(lambda m: m in self.printed_at and m <= through)
            work.loc[unpublished.to_numpy(), "y_C"] = np.nan
            self._fits[through] = EmosModel(trend=self.trend).fit(work, leads=self.leads)
        return self._fits[through]

    def at(self, t: datetime) -> Forecast:
        t = pd.Timestamp(t)
        if t.tzinfo is None:
            raise ValueError("the decision time must be timezone-aware")
        released = (self._released <= t).to_numpy()
        latest_init = max(self._init_month[released]) if released.any() else None
        if self.model is not None:
            key = ("fixed", latest_init,
                   self.model_fitted_at is None or t < self.model_fitted_at)
        else:
            key = ("pit", latest_init, self._labels_through(t))
        if key in self._cache:
            return self._cache[key]

        notes: list[str] = []
        sub = self.frame[released]
        if self.model is not None:
            model, through = self.model, None
            after = self.model_fitted_at is None or t < self.model_fitted_at
        else:
            through, after = key[2], False
            if through is None:
                forecast = Forecast({}, {}, None, False, ("no label printed yet",))
                self._cache[key] = forecast
                return forecast
            model = self._fit(through)
        if sub.empty:
            notes.append("no SEAS5 init released yet")
            forecast = Forecast({}, {}, through, after, tuple(notes))
        else:
            forecast = Forecast(
                predictive_by_target_month(sub, model),
                freshest_init_by_target(sub, fitted_leads=model.params.keys()),
                through, after, tuple(notes),
            )
        self._cache[key] = forecast
        return forecast


__all__ = ["Forecast", "PointInTimeForecaster", "print_times"]

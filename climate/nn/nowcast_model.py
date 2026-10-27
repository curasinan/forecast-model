"""The nowcast network: the seasonal network's machinery on the month-to-date features."""

from __future__ import annotations

from typing import ClassVar

import numpy as np
import pandas as pd

from climate.nn.nowcast import NOWCAST_FEATURES
from climate.nn.seasonal_model import GtmNnModel


class GtmNowcastModel(GtmNnModel):
    """One small Gaussian network per seed on the nowcast features; a row with a gap day is out."""

    FEATURES: ClassVar[list[str]] = NOWCAST_FEATURES
    USABLE: ClassVar[str] = "a label, every feature and no gap day"

    def _admitted(self, frame: pd.DataFrame) -> np.ndarray:
        """Rows without a gap day: the month-to-date mean covers every day it claims to."""
        return frame["gap_days"].to_numpy() == 0

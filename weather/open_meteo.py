"""Open-Meteo client with commercial / self-host gating."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from src.config import settings

log = logging.getLogger(__name__)

# Forecast API (deterministic) vs Ensemble API (separate host).
PUBLIC_BASE = "https://api.open-meteo.com"
CUSTOMER_BASE = "https://customer-api.open-meteo.com"
PUBLIC_ENSEMBLE_BASE = "https://ensemble-api.open-meteo.com"
CUSTOMER_ENSEMBLE_BASE = "https://customer-ensemble-api.open-meteo.com"
ENSEMBLE_PATH = "/v1/ensemble"
FORECAST_PATH = "/v1/forecast"
PREVIOUS_RUNS_BASE = "https://previous-runs-api.open-meteo.com"
CUSTOMER_PREVIOUS_RUNS_BASE = "https://customer-previous-runs-api.open-meteo.com"
SINGLE_RUNS_BASE = "https://single-runs-api.open-meteo.com"

USER_AGENT = "polymarket-trader/0.1 (+weather.open_meteo)"
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 30.0
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Default ensembles for station daily max/min postprocessing.
DEFAULT_ENSEMBLE_MODELS = (
    "ecmwf_ifs025",
    "gfs_seamless",
    "icon_seamless",
)


class OpenMeteoLicenseError(RuntimeError):
    """Raised when trading-path collection would violate non-commercial ToS."""


def resolve_open_meteo_base_url() -> str:
    if settings.open_meteo_base_url.strip():
        return settings.open_meteo_base_url.rstrip("/")
    if settings.open_meteo_api_key.strip():
        return CUSTOMER_BASE
    return PUBLIC_BASE


def resolve_open_meteo_ensemble_base_url() -> str:
    """Ensemble lives on ensemble-api.*; customer prefix when key present."""
    custom = settings.open_meteo_base_url.strip()
    if custom:
        # If user pointed at ensemble host already, keep it; else derive.
        if "ensemble" in custom:
            return custom.rstrip("/")
        if "customer-" in custom:
            return CUSTOMER_ENSEMBLE_BASE
        return PUBLIC_ENSEMBLE_BASE
    if settings.open_meteo_api_key.strip():
        return CUSTOMER_ENSEMBLE_BASE
    return PUBLIC_ENSEMBLE_BASE


def resolve_open_meteo_previous_runs_base_url() -> str:
    """Previous Runs lives on previous-runs-api.*; customer prefix when key present."""
    custom = settings.open_meteo_base_url.strip()
    if custom:
        if "previous-runs" in custom:
            return custom.rstrip("/")
        if "customer-" in custom:
            return CUSTOMER_PREVIOUS_RUNS_BASE
        return custom.rstrip("/")
    if settings.open_meteo_api_key.strip():
        return CUSTOMER_PREVIOUS_RUNS_BASE
    return PREVIOUS_RUNS_BASE


def assert_open_meteo_allowed_for_trading() -> None:
    """Phase-0 gate: refuse production collection on free public host."""
    has_key = bool(settings.open_meteo_api_key.strip())
    custom_base = bool(settings.open_meteo_base_url.strip())
    if has_key or custom_base or settings.open_meteo_allow_noncommercial:
        return
    raise OpenMeteoLicenseError(
        "Open-Meteo free API is non-commercial. For trading bots set "
        "OPEN_METEO_API_KEY (customer API), or OPEN_METEO_BASE_URL to a "
        "self-hosted instance, or OPEN_METEO_ALLOW_NONCOMMERCIAL=true "
        "for research-only collection."
    )


class OpenMeteoClient:
    def __init__(
        self,
        base_url: str | None = None,
        ensemble_base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        for_trading: bool = True,
        previous_runs_base_url: str | None = None,
        session: Any | None = None,
    ):
        if for_trading:
            assert_open_meteo_allowed_for_trading()
        self.base_url = (base_url or resolve_open_meteo_base_url()).rstrip("/")
        self.ensemble_base_url = (
            ensemble_base_url or resolve_open_meteo_ensemble_base_url()
        ).rstrip("/")
        self.previous_runs_base_url = (
            previous_runs_base_url or resolve_open_meteo_previous_runs_base_url()
        ).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.open_meteo_api_key
        self.timeout = timeout
        # Test seam: object with .get(url, params=..., headers=...) -> response.
        self._session = session

    def _params(self, params: dict[str, Any]) -> dict[str, Any]:
        out = dict(params)
        if self.api_key:
            out["apikey"] = self.api_key
        return out

    def _get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        headers = {"User-Agent": USER_AGENT}
        if self._session is not None:
            return self._session.get(url, params=self._params(params), headers=headers)
        with httpx.Client(timeout=self.timeout, headers=headers) as client:
            return client.get(url, params=self._params(params))

    def get_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        retries: int = 0,
        retry_delay: float = RETRY_DELAY_SECONDS,
    ) -> dict[str, Any]:
        attempts = max(0, retries) + 1
        for attempt in range(attempts):
            last_attempt = attempt == attempts - 1
            try:
                resp = self._get(url, params)
            except httpx.TransportError:
                if last_attempt:
                    raise
                log.warning("Open-Meteo transport error; retry %s/%s", attempt + 1, retries)
                time.sleep(retry_delay)
                continue
            if resp.status_code in RETRYABLE_STATUS and not last_attempt:
                log.warning(
                    "Open-Meteo HTTP %s; retry %s/%s", resp.status_code, attempt + 1, retries
                )
                time.sleep(retry_delay)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")  # pragma: no cover

    def fetch_ensemble(
        self,
        latitude: float,
        longitude: float,
        *,
        models: tuple[str, ...] | list[str] = DEFAULT_ENSEMBLE_MODELS,
        temperature_unit: str = "fahrenheit",
        forecast_days: int = 7,
        timezone: str = "UTC",
        hourly: str | None = None,
        daily: str = "temperature_2m_max,temperature_2m_min",
    ) -> dict[str, Any]:
        url = f"{self.ensemble_base_url}{ENSEMBLE_PATH}"
        params: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "models": ",".join(models),
            "daily": daily,
            "temperature_unit": temperature_unit,
            "forecast_days": forecast_days,
            "timezone": timezone,
        }
        if hourly:
            params["hourly"] = hourly
        return self.get_json(url, params)

    def fetch_previous_runs(
        self,
        latitude: float,
        longitude: float,
        *,
        start_date: str,
        end_date: str,
        model: str,
        temperature_unit: str = "fahrenheit",
        max_previous_day: int = 7,
        retries: int = MAX_RETRIES,
        retry_delay: float = RETRY_DELAY_SECONDS,
    ) -> dict[str, Any]:
        """Hourly previous-run temperatures at fixed lead offsets (day 1..N)."""
        hourly = ",".join(
            f"temperature_2m_previous_day{n}" for n in range(1, max_previous_day + 1)
        )
        url = f"{self.previous_runs_base_url}/v1/forecast"
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "models": model,
            "hourly": hourly,
            "temperature_unit": temperature_unit,
            "timezone": "UTC",
            "start_date": start_date,
            "end_date": end_date,
        }
        return self.get_json(url, params, retries=retries, retry_delay=retry_delay)

    def fetch_single_run(
        self,
        latitude: float,
        longitude: float,
        *,
        run: str,
        models: str = "ecmwf_ifs025",
        temperature_unit: str = "fahrenheit",
        daily: str = "temperature_2m_max,temperature_2m_min",
        forecast_days: int = 10,
    ) -> dict[str, Any]:
        url = f"{SINGLE_RUNS_BASE}/v1/forecast"
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "models": models,
            "run": run,
            "daily": daily,
            "temperature_unit": temperature_unit,
            "forecast_days": forecast_days,
        }
        return self.get_json(url, params)

    @staticmethod
    def preview_url(path: str, params: dict[str, Any]) -> str:
        return f"{path}?{urlencode(params)}"

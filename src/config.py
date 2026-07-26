from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # API Keys
    anthropic_api_key: str = ""
    newsapi_key: str = ""
    github_token: str = ""

    # Model settings
    claude_model: str = "claude-sonnet-4-5-20250514"

    # Polymarket
    polymarket_api_base: str = "https://clob.polymarket.com"
    gamma_api_base: str = "https://gamma-api.polymarket.com"

    # Collection
    max_evidence_per_source: int = 20
    evidence_cache_hours: int = 6

    # Edge detection
    min_edge_threshold: float = 0.05  # 5% minimum edge to flag
    kelly_fraction_cap: float = 0.5  # Never bet more than half-Kelly

    # Database
    database_path: str = "data/predictions.db"

    # Trading
    polymarket_private_key: str = ""  # Polygon wallet private key for live trading
    polymarket_funder_address: str = ""  # Optional: funder address for gas
    trading_dry_run: bool = True  # Default to dry-run (no real trades)
    trading_bankroll: float = 1000.0  # Starting simulated bankroll ($)
    trading_max_position_usd: float = 50.0  # Max $ per single trade
    trading_max_exposure_usd: float = 200.0  # Max total exposure
    trading_max_daily_loss_usd: float = 50.0  # Daily stop-loss

    # Weather / Open-Meteo
    # Free api.open-meteo.com is non-commercial. For trading use either:
    #   - OPEN_METEO_API_KEY + customer-api.open-meteo.com (default when key set), or
    #   - self-hosted Open-Meteo and set OPEN_METEO_BASE_URL accordingly.
    open_meteo_api_key: str = ""
    open_meteo_base_url: str = ""  # empty → auto: customer host if key else public
    open_meteo_allow_noncommercial: bool = False  # research-only escape hatch
    # Keep weather Parquet/SQLite off OneDrive (SQLite + sync = corruption risk).
    # Empty → %LOCALAPPDATA%/polymarket_weather on Windows, else ~/.local/share/...
    weather_data_root: str = ""
    weather_min_liquidity: float = 1000.0
    weather_min_volume: float = 5000.0
    weather_snapshot_end_hours: float = 2.0  # allow nearer expiry than politics (24h)

    # Project root (derived)
    @property
    def project_root(self) -> Path:
        return Path(__file__).parent.parent

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed environment settings with safe paper-trading defaults."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = Field(default="local", alias="APP_ENV")
    service_name: str = Field(default="homerun-api", alias="SERVICE_NAME")
    paper_trading: bool = Field(default=True, alias="PAPER_TRADING")
    live_trading_enabled: bool = Field(default=False, alias="LIVE_TRADING_ENABLED")
    execution_kill_switch: bool = Field(default=True, alias="EXECUTION_KILL_SWITCH")
    kalshi_env: Literal["demo", "production"] = Field(default="demo", alias="KALSHI_ENV")
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    kalshi_api_key: SecretStr | None = Field(default=None, alias="KALSHI_API_KEY")
    kalshi_api_secret: SecretStr | None = Field(default=None, alias="KALSHI_API_SECRET")
    cors_origins: str = Field(default="http://localhost:3000", alias="CORS_ORIGINS")
    kalshi_rest_base_url: str = Field(
        default="https://demo-api.kalshi.co/trade-api/v2", alias="KALSHI_REST_BASE_URL"
    )
    kalshi_market_data_base_url: str = Field(
        default="https://external-api.kalshi.com/trade-api/v2", alias="KALSHI_MARKET_DATA_BASE_URL"
    )
    kalshi_ws_base_url: str = Field(
        default="wss://demo-api.kalshi.co/trade-api/ws/v2", alias="KALSHI_WS_BASE_URL"
    )
    mlb_stats_base_url: str = Field(default="https://statsapi.mlb.com/api/v1", alias="MLB_STATS_BASE_URL")
    market_discovery_enabled: bool = Field(default=True, alias="MARKET_DISCOVERY_ENABLED")
    kalshi_enable_broad_discovery: bool = Field(default=False, alias="KALSHI_ENABLE_BROAD_DISCOVERY")
    kalshi_market_sync_max_pages: int = Field(default=2, alias="KALSHI_MARKET_SYNC_MAX_PAGES")
    kalshi_market_sync_limit: int = Field(default=100, alias="KALSHI_MARKET_SYNC_LIMIT")
    market_family_discovery_enabled: bool = Field(default=True, alias="MARKET_FAMILY_DISCOVERY_ENABLED")
    market_family_discovery_max_pages: int = Field(default=2, alias="MARKET_FAMILY_DISCOVERY_MAX_PAGES")
    kalshi_discovery_enable_fallback_time_offsets: bool = Field(
        default=True, alias="KALSHI_DISCOVERY_ENABLE_FALLBACK_TIME_OFFSETS"
    )
    kalshi_discovery_max_fallback_offsets: int = Field(default=6, alias="KALSHI_DISCOVERY_MAX_FALLBACK_OFFSETS")
    kalshi_discovery_max_429_errors: int = Field(default=5, alias="KALSHI_DISCOVERY_MAX_429_ERRORS")
    kalshi_market_data_min_request_interval_ms: int = Field(
        default=500, alias="KALSHI_MARKET_DATA_MIN_REQUEST_INTERVAL_MS"
    )
    kalshi_market_data_max_retries: int = Field(default=2, alias="KALSHI_MARKET_DATA_MAX_RETRIES")
    kalshi_market_data_backoff_base_ms: int = Field(default=1000, alias="KALSHI_MARKET_DATA_BACKOFF_BASE_MS")
    kalshi_market_data_backoff_max_ms: int = Field(default=10000, alias="KALSHI_MARKET_DATA_BACKOFF_MAX_MS")
    open_position_price_refresh_enabled: bool = Field(default=True, alias="OPEN_POSITION_PRICE_REFRESH_ENABLED")
    open_position_price_refresh_max_per_run: int = Field(default=100, alias="OPEN_POSITION_PRICE_REFRESH_MAX_PER_RUN")
    paper_candidate_engine_enabled: bool = Field(default=True, alias="PAPER_CANDIDATE_ENGINE_ENABLED")
    default_paper_contracts: int = Field(default=1, alias="DEFAULT_PAPER_CONTRACTS")
    paper_max_trades_per_slate: int = Field(default=20, alias="PAPER_MAX_TRADES_PER_SLATE")
    paper_max_trades_per_game: int = Field(default=3, alias="PAPER_MAX_TRADES_PER_GAME")
    paper_max_trades_per_market_family: int = Field(default=8, alias="PAPER_MAX_TRADES_PER_MARKET_FAMILY")
    paper_max_trades_per_game_family: int = Field(default=1, alias="PAPER_MAX_TRADES_PER_GAME_FAMILY")
    paper_allow_multiple_lines_per_game_family: bool = Field(
        default=False, alias="PAPER_ALLOW_MULTIPLE_LINES_PER_GAME_FAMILY"
    )
    paper_allow_multiple_f5_winner_outcomes: bool = Field(
        default=False, alias="PAPER_ALLOW_MULTIPLE_F5_WINNER_OUTCOMES"
    )
    paper_max_open_positions: int = Field(default=50, alias="PAPER_MAX_OPEN_POSITIONS")
    paper_min_net_ev: Decimal = Field(default=Decimal("0.05"), alias="PAPER_MIN_NET_EV")
    paper_min_prob_edge: Decimal = Field(default=Decimal("0.03"), alias="PAPER_MIN_PROB_EDGE")
    paper_min_data_quality: Decimal = Field(default=Decimal("0.60"), alias="PAPER_MIN_DATA_QUALITY")
    paper_require_calibrated_for_trade: bool = Field(default=False, alias="PAPER_REQUIRE_CALIBRATED_FOR_TRADE")
    paper_max_price_staleness_seconds: int = Field(default=900, alias="PAPER_MAX_PRICE_STALENESS_SECONDS")
    paper_allow_last_price_fallback_for_trade: bool = Field(
        default=False, alias="PAPER_ALLOW_LAST_PRICE_FALLBACK_FOR_TRADE"
    )
    paper_starting_balance: Decimal = Field(default=Decimal("1000.00"), alias="PAPER_STARTING_BALANCE")
    kalshi_trade_fee_rate: Decimal = Field(default=Decimal("0.07"), alias="KALSHI_TRADE_FEE_RATE")
    kalshi_fee_estimate_mode: str = Field(default="conservative", alias="KALSHI_FEE_ESTIMATE_MODE")
    kalshi_fee_rounding_mode: str = Field(
        default="centicent_or_cent_conservative", alias="KALSHI_FEE_ROUNDING_MODE"
    )
    kalshi_assume_taker: bool = Field(default=True, alias="KALSHI_ASSUME_TAKER")
    feature_sync_enable_network_sources: bool = Field(default=False, alias="FEATURE_SYNC_ENABLE_NETWORK_SOURCES")
    open_meteo_base_url: str = Field(default="https://api.open-meteo.com/v1", alias="OPEN_METEO_BASE_URL")
    injury_provider_api_key: SecretStr | None = Field(default=None, alias="INJURY_PROVIDER_API_KEY")
    lineup_provider_api_key: SecretStr | None = Field(default=None, alias="LINEUP_PROVIDER_API_KEY")
    weather_provider_api_key: SecretStr | None = Field(default=None, alias="WEATHER_PROVIDER_API_KEY")
    model_training_min_samples: int = Field(default=100, alias="MODEL_TRAINING_MIN_SAMPLES")
    model_min_samples_train: int = Field(default=250, alias="MODEL_MIN_SAMPLES_TRAIN")
    model_min_samples_calibrate: int = Field(default=250, alias="MODEL_MIN_SAMPLES_CALIBRATE")
    model_min_samples_promote: int = Field(default=500, alias="MODEL_MIN_SAMPLES_PROMOTE")
    model_promotion_min_logloss_improvement: Decimal = Field(
        default=Decimal("0.01"), alias="MODEL_PROMOTION_MIN_LOGLOSS_IMPROVEMENT"
    )
    model_promotion_max_ece: Decimal = Field(default=Decimal("0.08"), alias="MODEL_PROMOTION_MAX_ECE")
    model_min_family_samples_for_family_calibration: int = Field(
        default=75, alias="MODEL_MIN_FAMILY_SAMPLES_FOR_FAMILY_CALIBRATION"
    )
    model_min_samples_for_isotonic: int = Field(default=1000, alias="MODEL_MIN_SAMPLES_FOR_ISOTONIC")
    dashboard_timezone: str = Field(default="America/New_York", alias="DASHBOARD_TIMEZONE")
    backend_api_key: SecretStr | None = Field(default=None, alias="BACKEND_API_KEY")

    @field_validator("kalshi_market_sync_max_pages")
    @classmethod
    def validate_kalshi_market_sync_max_pages(cls, value: int) -> int:
        return max(value, 1)

    @field_validator("kalshi_market_sync_limit")
    @classmethod
    def validate_kalshi_market_sync_limit(cls, value: int) -> int:
        return min(max(value, 1), 200)

    @field_validator("market_family_discovery_max_pages")
    @classmethod
    def validate_market_family_discovery_max_pages(cls, value: int) -> int:
        return max(value, 1)

    @field_validator("kalshi_discovery_max_fallback_offsets")
    @classmethod
    def validate_kalshi_discovery_max_fallback_offsets(cls, value: int) -> int:
        return min(max(value, 0), 6)

    @field_validator("kalshi_discovery_max_429_errors")
    @classmethod
    def validate_kalshi_discovery_max_429_errors(cls, value: int) -> int:
        return max(value, 1)

    @field_validator("kalshi_market_data_min_request_interval_ms")
    @classmethod
    def validate_kalshi_market_data_min_request_interval_ms(cls, value: int) -> int:
        return max(value, 0)

    @field_validator("kalshi_market_data_max_retries")
    @classmethod
    def validate_kalshi_market_data_max_retries(cls, value: int) -> int:
        return max(value, 0)

    @field_validator("kalshi_market_data_backoff_base_ms", "kalshi_market_data_backoff_max_ms")
    @classmethod
    def validate_kalshi_market_data_backoff_ms(cls, value: int) -> int:
        return max(value, 0)

    @field_validator("default_paper_contracts")
    @classmethod
    def validate_default_paper_contracts(cls, value: int) -> int:
        return max(value, 1)

    @field_validator(
        "open_position_price_refresh_max_per_run",
        "paper_max_trades_per_slate",
        "paper_max_trades_per_game",
        "paper_max_trades_per_market_family",
        "paper_max_trades_per_game_family",
        "paper_max_open_positions",
        "paper_max_price_staleness_seconds",
        "model_min_samples_train",
        "model_min_samples_calibrate",
        "model_min_samples_promote",
        "model_min_family_samples_for_family_calibration",
        "model_min_samples_for_isotonic",
    )
    @classmethod
    def validate_positive_ints(cls, value: int) -> int:
        return max(value, 1)

    @field_validator(
        "paper_min_net_ev",
        "paper_min_prob_edge",
        "paper_min_data_quality",
        "kalshi_trade_fee_rate",
        "model_promotion_min_logloss_improvement",
        "model_promotion_max_ece",
    )
    @classmethod
    def validate_nonnegative_decimals(cls, value: Decimal) -> Decimal:
        return max(value, Decimal("0"))

    @field_validator("paper_starting_balance")
    @classmethod
    def validate_paper_starting_balance(cls, value: Decimal) -> Decimal:
        return max(value, Decimal("0.00"))

    @field_validator("model_training_min_samples")
    @classmethod
    def validate_model_training_min_samples(cls, value: int) -> int:
        return max(value, 1)

    @property
    def sqlalchemy_database_url(self) -> str | None:
        if not self.database_url:
            return None

        if self.database_url.startswith("postgres://"):
            return self.database_url.replace("postgres://", "postgresql+psycopg://", 1)

        if self.database_url.startswith("postgresql://"):
            return self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)

        return self.database_url

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def kalshi_credentials_configured(self) -> bool:
        return bool(
            self.kalshi_api_key
            and self.kalshi_api_key.get_secret_value()
            and self.kalshi_api_secret
            and self.kalshi_api_secret.get_secret_value()
        )

    @property
    def backend_api_key_configured(self) -> bool:
        return bool(self.backend_api_key and self.backend_api_key.get_secret_value())

    @property
    def kalshi_market_data_base_kind(self) -> str:
        normalized = self.kalshi_market_data_base_url.strip().lower()
        if "external-api.kalshi.com" in normalized:
            return "production_public_market_data"
        if "demo-api.kalshi.co" in normalized:
            return "demo_market_data"
        return "custom_market_data"

    @property
    def kalshi_market_data_source(self) -> str:
        return self.kalshi_market_data_base_kind

    @property
    def safe_execution_posture(self) -> bool:
        return self.paper_trading and not self.live_trading_enabled and self.execution_kill_switch


@lru_cache
def get_settings() -> Settings:
    return Settings()

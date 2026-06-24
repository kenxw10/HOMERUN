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
    kalshi_ws_base_url: str = Field(
        default="wss://demo-api.kalshi.co/trade-api/ws/v2", alias="KALSHI_WS_BASE_URL"
    )
    mlb_stats_base_url: str = Field(default="https://statsapi.mlb.com/api/v1", alias="MLB_STATS_BASE_URL")
    market_discovery_enabled: bool = Field(default=True, alias="MARKET_DISCOVERY_ENABLED")
    paper_candidate_engine_enabled: bool = Field(default=True, alias="PAPER_CANDIDATE_ENGINE_ENABLED")
    default_paper_contracts: int = Field(default=1, alias="DEFAULT_PAPER_CONTRACTS")
    dashboard_timezone: str = Field(default="America/New_York", alias="DASHBOARD_TIMEZONE")
    backend_api_key: SecretStr | None = Field(default=None, alias="BACKEND_API_KEY")

    @field_validator("default_paper_contracts")
    @classmethod
    def validate_default_paper_contracts(cls, value: int) -> int:
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
    def safe_execution_posture(self) -> bool:
        return self.paper_trading and not self.live_trading_enabled and self.execution_kill_switch


@lru_cache
def get_settings() -> Settings:
    return Settings()

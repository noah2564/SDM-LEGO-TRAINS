"""Application configuration, sourced exclusively from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Every value can be overridden with an environment variable of the same
    name (case-insensitive). Defaults are development-only and are documented
    as such in .env.example.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- MQTT -----------------------------------------------------------
    mqtt_host: str = "mosquitto"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_keepalive: int = 30
    mqtt_client_id: str = "lego-backend"
    mqtt_reconnect_delay: float = 3.0

    # --- Database -------------------------------------------------------
    # Swap for e.g. postgresql+asyncpg://user:pass@db:5432/trains
    database_url: str = "sqlite+aiosqlite:////data/trains.db"

    # --- Safety / limits -------------------------------------------------
    max_speed: int = 100
    min_speed: int = 0
    # A train that has not been heard from for this long is considered stale.
    offline_timeout_seconds: float = 20.0
    # How often the backend proves it is alive to the fleet. The firmware
    # safety timeout must be comfortably larger than this value.
    heartbeat_interval_seconds: float = 2.0
    # How often the staleness sweep runs.
    monitor_interval_seconds: float = 2.0
    # Events retained per train (older ones are pruned).
    max_events_per_train: int = 200

    # --- HTTP ------------------------------------------------------------
    log_level: str = "INFO"
    cors_origins: str = "*"
    api_prefix: str = "/api"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

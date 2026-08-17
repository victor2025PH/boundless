# TrustCheck Bot - config from env (no secrets in code)
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    bot_token: str = Field(..., description="Telegram Bot token from @BotFather")
    api_base_url: str = Field(default="http://127.0.0.1:8000", description="Backend API base URL")
    api_internal_key: str = Field(default="", description="Optional API auth key for bot->api")
    production: bool = Field(default=False, description="If True, admin endpoints require API_INTERNAL_KEY (403 if unset)")
    database_url: str = Field(default="sqlite+aiosqlite:///./trustcheck.db", description="DB URL")
    admin_tg_ids: str = Field(default="", description="Comma-separated admin Telegram IDs")
    rate_limit_check_per_min: int = Field(default=10, ge=0, description="Max /check per user per minute")
    rate_limit_report_per_min: int = Field(default=3, ge=0, description="Max /report per user per minute")
    use_effective_report_count: bool = Field(default=True, description="Use reporter-weighted effective report count in scoring; False = use raw report_count only")
    bot_username: str = Field(default="xyc2026_bot", description="Bot username for invite link (no @)")
    trongrid_api_key: str = Field(default="", description="Optional TronGrid API key for TRC20 flow")
    toncenter_api_key: str = Field(default="", description="Optional TON Center API key for TON flow")
    flow_snapshot_retention_days: int = Field(default=90, ge=1, le=3650, description="Days to keep flow snapshots before cleanup")
    flow_sync_cooldown_seconds: int = Field(default=300, ge=60, le=86400, description="Min seconds between sync-flow per user (rate limit)")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    def get_admin_ids(self) -> list[int]:
        if not self.admin_tg_ids.strip():
            return []
        return [int(x.strip()) for x in self.admin_tg_ids.split(",") if x.strip()]


settings = Settings()

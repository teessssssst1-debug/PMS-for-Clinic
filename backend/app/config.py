from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parents[2]
_ENV_CANDIDATES = (
    Path.cwd() / ".env",
    _ROOT / ".env",
    _ROOT.parent / ".env",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=tuple(str(p) for p in _ENV_CANDIDATES if p.exists()) or ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    public_base_url: str = "http://localhost:8000"
    api_key: str = "change-me-clinic-api-key"
    timezone: str = "Asia/Kolkata"

    database_url: str = "sqlite+aiosqlite:///./clinic_voice.db"
    database_url_sync: str = "sqlite:///./clinic_voice.db"

    same_day_buffer_minutes: int = 15
    reschedule_fee_inr: int = 200
    reschedule_fee_window_hours: int = 24
    cancellation_fee_inr: int = 200
    cancellation_fee_window_hours: int = 24
    currency: str = "INR"

    cliniko_api_key: str = ""
    # Fallback only — effective URL is derived from the API key shard suffix.
    cliniko_base_url: str = "https://api.au1.cliniko.com/v1"
    cliniko_user_agent: str = "ClinicVoiceReceptionist (support@example.com)"
    cliniko_enabled: bool = True

    # Optional — used by /webhooks/bolna if you set a shared secret later
    bolna_webhook_secret: str = ""

    call_state_ttl_seconds: int = 60 * 60 * 6
    outbound_context_ttl_seconds: int = 60 * 60 * 48

    @property
    def cliniko_active(self) -> bool:
        return bool(self.cliniko_enabled and self.cliniko_api_key)

    @property
    def cliniko_effective_base_url(self) -> str:
        key = self.cliniko_api_key or ""
        if "-" in key:
            shard = key.rsplit("-", 1)[-1].strip()
            if shard:
                return f"https://api.{shard}.cliniko.com/v1"
        return self.cliniko_base_url


@lru_cache
def get_settings() -> Settings:
    return Settings()

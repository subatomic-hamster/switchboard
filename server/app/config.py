"""Settings, read from the repo-root `.env` and the process environment.

Every integration has a dry-run path, so a blank value never stops the server from starting.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = SERVER_ROOT / "fixtures"

# The real-money ceiling is a constant in code, not only a setting: a setting can lower it,
# nothing can raise it.
HARD_MAX_ORDER_USD = Decimal("2")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # server
    port: int = 8787
    database_path: str = "./data/switchboard.sqlite"
    public_base_url: str = ""

    # real-money guards
    trading_enabled: bool = False
    max_order_usd: Decimal = HARD_MAX_ORDER_USD

    # P1 stubs
    use_p1_stubs: bool = True

    # policy mirror (the contract is the authority once P1's vault is wired in)
    policy_transfer_cap_usd: Decimal = Decimal("200")
    policy_daily_budget_usd: Decimal = Decimal("500")
    passkey_threshold_usd: Decimal = Decimal("1000")
    approval_expiry_s: int = 300
    max_calls_per_day: int = 5

    # intent parser
    anthropic_api_key: str = ""
    intent_model: str = "claude-opus-5-5"

    # forecast opinion
    openrouter_api_key: str = ""
    openai_api_key: str = ""
    google_ai_api_key: str = ""
    xai_api_key: str = ""
    forecast_models: str = ""

    # ElevenLabs
    elevenlabs_api_key: str = ""
    elevenlabs_agent_id: str = ""
    elevenlabs_phone_number_id: str = ""
    elevenlabs_webhook_secret: str = ""
    elevenlabs_operator_voice_id: str = ""
    elevenlabs_risk_officer_voice_id: str = ""

    # phone calls
    owner_phone_number: str = ""
    calls_enabled: bool = False
    voice_token: str = ""

    # Grok Bots
    bot_token: str = ""
    grok_missed_call_webhook_url: str = ""

    # demo: divide stubbed transfer times by this so the ring view is watchable
    demo_time_scale: float = Field(default=4.0, gt=0)

    @property
    def effective_max_order_usd(self) -> Decimal:
        return min(self.max_order_usd, HARD_MAX_ORDER_USD)

    @property
    def calls_live(self) -> bool:
        """True only when a real outbound call can and may be placed."""
        return bool(
            self.calls_enabled
            and self.elevenlabs_api_key
            and self.elevenlabs_agent_id
            and self.elevenlabs_phone_number_id
            and self.owner_phone_number
        )

    @property
    def database_file(self) -> Path:
        path = Path(self.database_path)
        return path if path.is_absolute() else REPO_ROOT / path


@lru_cache
def get_settings() -> Settings:
    return Settings()

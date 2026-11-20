"""Settings, read from the repo-root `.env` and the process environment.

Every integration has a dry-run path, so a blank value never stops the server from starting.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = SERVER_ROOT / "fixtures"

# The real-money ceiling is a constant in code, not only a setting: a setting can lower it,
# nothing can raise it.
HARD_MAX_ORDER_USD = Decimal("2")

# Who places the approval call. `dry_run` places none.
CallProvider = Literal["dry_run", "agentphone", "elevenlabs_twilio"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """`.env` wins over the process environment.

        The project's `.env` is the one place its keys are set. A key that happens to be in the
        shell (for example one a coding tool exports for itself) must not silently replace it.
        """
        return init_settings, dotenv_settings, env_settings, file_secret_settings

    # server
    port: int = 8787
    database_path: str = "./data/switchboard.sqlite"
    public_base_url: str = ""

    # real-money guards
    trading_enabled: bool = False
    max_order_usd: Decimal = HARD_MAX_ORDER_USD

    # P1 stubs
    use_p1_stubs: bool = True

    # P1's vault on Arc (used when USE_P1_STUBS=false). The three keys are the owner, the
    # agent (asks for spends, redeems permits) and the approver (approves, denies, freezes).
    vault_address: str = ""
    arc_rpc_url: str = ""
    arc_chain_id: int = 5042002
    usdc_address: str = "0x3600000000000000000000000000000000000000"
    owner_private_key: str = ""
    agent_private_key: str = ""
    approver_private_key: str = ""
    quicknode_stream_secret: str = ""
    # Dead man's switch: no authenticated Stream delivery for this long means stale. The Stream
    # sends a heartbeat about every 30 seconds.
    stream_stale_after_s: int = Field(default=90, gt=0)

    # Anomaly baseline (app/chain/anomaly.py). Unset means on with the real vault, off with stubs.
    anomaly_enabled: bool | None = None
    anomaly_min_samples: int = Field(default=8, ge=1)
    anomaly_amount_factor: Decimal = Decimal("5")
    anomaly_burst_count: int = Field(default=4, ge=2)
    anomaly_burst_window_s: int = Field(default=600, gt=0)

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

    # ElevenLabs: dashboard speech, and the agent used by the elevenlabs_twilio provider
    # and the browser widget
    elevenlabs_api_key: str = ""
    elevenlabs_agent_id: str = ""
    elevenlabs_phone_number_id: str = ""
    elevenlabs_webhook_secret: str = ""
    elevenlabs_operator_voice_id: str = ""
    elevenlabs_risk_officer_voice_id: str = ""
    elevenlabs_tts_model: str = "eleven_flash_v2_5"
    elevenlabs_stt_model: str = "scribe_v3"

    # AgentPhone: the approval call when CALL_PROVIDER=agentphone
    agentphone_api_key: str = ""
    agentphone_agent_id: str = ""
    agentphone_number_id: str = ""
    agentphone_webhook_secret: str = ""

    # phone calls
    call_provider: CallProvider = "dry_run"
    owner_phone_number: str = ""
    calls_enabled: bool = False
    voice_token: str = ""

    # Grok Bots
    bot_token: str = ""
    grok_missed_call_webhook_url: str = ""
    # A Grok Bot routine's webhook is called with its own key as a bearer token.
    grok_missed_call_webhook_key: str = ""

    # demo: divide stubbed transfer times by this so the ring view is watchable
    demo_time_scale: float = Field(default=4.0, gt=0)

    @property
    def anomaly_on(self) -> bool:
        return (not self.use_p1_stubs) if self.anomaly_enabled is None else self.anomaly_enabled

    @property
    def effective_max_order_usd(self) -> Decimal:
        return min(self.max_order_usd, HARD_MAX_ORDER_USD)

    @field_validator("call_provider", mode="before")
    @classmethod
    def _blank_provider_is_dry_run(cls, value: object) -> object:
        """A blank CALL_PROVIDER means dry run; a misspelt one still stops the server."""
        if isinstance(value, str):
            return value.strip().lower() or "dry_run"
        return value

    @property
    def call_blockers(self) -> list[str]:
        """The settings still missing before the chosen provider could place a call."""
        needed: dict[str, str] = {}
        if self.call_provider == "agentphone":
            needed = {
                "AGENTPHONE_API_KEY": self.agentphone_api_key,
                "AGENTPHONE_AGENT_ID": self.agentphone_agent_id,
                "AGENTPHONE_NUMBER_ID": self.agentphone_number_id,
                # Without the secret a caller's turn cannot be verified, so the call would
                # be silence. It exists only once the webhook has been registered.
                "AGENTPHONE_WEBHOOK_SECRET": self.agentphone_webhook_secret,
            }
        elif self.call_provider == "elevenlabs_twilio":
            needed = {
                "ELEVENLABS_API_KEY": self.elevenlabs_api_key,
                "ELEVENLABS_AGENT_ID": self.elevenlabs_agent_id,
                "ELEVENLABS_PHONE_NUMBER_ID": self.elevenlabs_phone_number_id,
            }
        else:
            return ["CALL_PROVIDER"]
        needed["OWNER_PHONE_NUMBER"] = self.owner_phone_number
        missing = [name for name, value in needed.items() if not value]
        if not self.calls_enabled:
            missing.append("CALLS_ENABLED")
        return missing

    @property
    def calls_live(self) -> bool:
        """True only when a real outbound call can and may be placed."""
        return not self.call_blockers

    @property
    def agent_tools_live(self) -> bool:
        """True when the live call is hosted by the ElevenLabs agent, which has the rule
        tools. The AgentPhone call is a fixed script that only handles an approval."""
        return self.calls_live and self.call_provider == "elevenlabs_twilio"

    @property
    def database_file(self) -> Path:
        path = Path(self.database_path)
        return path if path.is_absolute() else REPO_ROOT / path


@lru_cache
def get_settings() -> Settings:
    return Settings()

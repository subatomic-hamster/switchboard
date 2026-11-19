"""Phase 0 proof: one outbound call that reaches a real phone and speaks a variable.

    uv run python -m scripts.phase0_call                      # dry run: prints the request
    uv run python -m scripts.phase0_call --place-real-call    # places ONE real call

The call goes through the provider in CALL_PROVIDER (`agentphone` or `elevenlabs_twilio`) and
rings OWNER_PHONE_NUMBER only. A real call needs CALLS_ENABLED=true and everything that
provider needs; the script says what is missing.

The opening line contains two per-call values (the amount and the payee), which is the thing
being proven. No request is pending for this call, so nothing can be approved on it: on
AgentPhone the line says so and hangs up after the first thing you say; the ElevenLabs agent
finds no request and says nothing was approved.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from typing import Any

import httpx

from app import call_script
from app.config import Settings, get_settings
from app.voice import (
    AGENTPHONE_API,
    AGENTPHONE_CALLS_PATH,
    ELEVENLABS_API,
    OUTBOUND_CALL_PATH,
    mask_number,
)

AMOUNT = Decimal("12.50")
PAYEE = "the Phase zero test wallet"
REASON = "this is a test call"

VARIABLES = {
    "request_id": "phase0",
    "amount_usd": "12.50",
    "amount_words": "12 dollars 50",
    "payee_label": PAYEE,
    "reason_code": "OVER_TRANSFER_CAP",
    "reason": REASON,
    "case_for": "This is a test.",
    "case_against": "This is a test.",
    "requires_passkey": "no",
    "spent_today_usd": "0",
}


def request_for(settings: Settings) -> tuple[str, dict[str, str], dict[str, Any], str]:
    """The URL, headers and body for one test call, and the body key that holds the number."""
    if settings.call_provider == "agentphone":
        return (
            f"{AGENTPHONE_API}{AGENTPHONE_CALLS_PATH}",
            {"Authorization": f"Bearer {settings.agentphone_api_key}"},
            {
                "agentId": settings.agentphone_agent_id,
                "toNumber": settings.owner_phone_number,
                "fromNumberId": settings.agentphone_number_id,
                "initialGreeting": call_script.greeting(AMOUNT, PAYEE, REASON),
            },
            "toNumber",
        )
    return (
        f"{ELEVENLABS_API}{OUTBOUND_CALL_PATH}",
        {"xi-api-key": settings.elevenlabs_api_key},
        {
            "agent_id": settings.elevenlabs_agent_id,
            "agent_phone_number_id": settings.elevenlabs_phone_number_id,
            "to_number": settings.owner_phone_number,
            "conversation_initiation_client_data": {"dynamic_variables": VARIABLES},
        },
        "to_number",
    )


def main() -> int:
    settings = get_settings()
    url, headers, body, number_key = request_for(settings)
    shown = {**body, number_key: mask_number(settings.owner_phone_number)}
    print(f"CALL_PROVIDER={settings.call_provider}")
    print(f"POST {url}")
    print(json.dumps(shown, indent=2))

    if "--place-real-call" not in sys.argv:
        print("\nDry run. Nothing was sent. Add --place-real-call to ring the owner number once.")
        return 0
    if not settings.calls_live:
        print(f"\nNot placing a call. Still to set in .env: {', '.join(settings.call_blockers)}")
        return 1

    response = httpx.post(url, headers=headers, json=body, timeout=30)
    print(f"\n{response.status_code} {response.text[:400]}")
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())

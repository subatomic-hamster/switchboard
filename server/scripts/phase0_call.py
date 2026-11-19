"""Phase 0 proof: one ElevenLabs outbound call that reaches a real phone and speaks a variable.

    uv run python -m scripts.phase0_call                      # dry run: prints the request
    uv run python -m scripts.phase0_call --place-real-call    # places ONE real call

A real call needs, in `.env`: ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID,
ELEVENLABS_PHONE_NUMBER_ID, OWNER_PHONE_NUMBER and CALLS_ENABLED=true. It rings
OWNER_PHONE_NUMBER only. The agent's first sentence contains two per-call variables (the amount
and the payee), which is the thing being proven. The request id is a placeholder, so the agent
will find no pending request and will say nothing was approved.
"""

from __future__ import annotations

import json
import sys

import httpx

from app.config import get_settings
from app.voice import ELEVENLABS_API, OUTBOUND_CALL_PATH, mask_number

VARIABLES = {
    "request_id": "phase0",
    "amount_usd": "12.50",
    "amount_words": "12 dollars 50",
    "payee_label": "the Phase zero test wallet",
    "reason_code": "OVER_TRANSFER_CAP",
    "reason": "this is a test call",
    "case_for": "This is a test.",
    "case_against": "This is a test.",
    "requires_passkey": "no",
    "spent_today_usd": "0",
}


def main() -> int:
    settings = get_settings()
    body = {
        "agent_id": settings.elevenlabs_agent_id,
        "agent_phone_number_id": settings.elevenlabs_phone_number_id,
        "to_number": settings.owner_phone_number,
        "conversation_initiation_client_data": {"dynamic_variables": VARIABLES},
    }
    shown = {**body, "to_number": mask_number(settings.owner_phone_number)}
    print(f"POST {ELEVENLABS_API}{OUTBOUND_CALL_PATH}")
    print(json.dumps(shown, indent=2))

    if "--place-real-call" not in sys.argv:
        print("\nDry run. Nothing was sent. Add --place-real-call to ring the owner number once.")
        return 0
    if not settings.calls_live:
        print(
            "\nNot placing a call: CALLS_ENABLED must be true and ELEVENLABS_API_KEY, "
            "ELEVENLABS_AGENT_ID, ELEVENLABS_PHONE_NUMBER_ID and OWNER_PHONE_NUMBER must be set."
        )
        return 1

    response = httpx.post(
        f"{ELEVENLABS_API}{OUTBOUND_CALL_PATH}",
        headers={"xi-api-key": settings.elevenlabs_api_key},
        json=body,
        timeout=30,
    )
    print(f"\n{response.status_code} {response.text[:400]}")
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())

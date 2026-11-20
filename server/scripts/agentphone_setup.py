"""Look up the AgentPhone ids `.env` needs, and register this server's webhook.

    uv run python -m scripts.agentphone_setup                     # read-only: show what exists
    uv run python -m scripts.agentphone_setup --write-env         # also save the two ids in .env
    uv run python -m scripts.agentphone_setup --register-webhook  # point the agent at this server
    uv run python -m scripts.agentphone_setup --set-webhook-mode  # switch the agent to webhook mode

The first two only read (`GET /v1/agents`, `GET /v1/numbers`, `GET /v1/agents/{id}/webhook`).
The last two change your AgentPhone account, so they are yours to run:

- `--register-webhook` sets the agent's webhook to PUBLIC_BASE_URL + /api/voice/hooks/agentphone
  and saves the signing secret AgentPhone returns as AGENTPHONE_WEBHOOK_SECRET in `.env`. The
  secret is never printed. It is a per-agent webhook, so other agents on the account are left
  alone.
- `--set-webhook-mode` sets the agent's `voiceMode` to `webhook`, so each thing the owner says
  is posted to this server. AgentPhone clears the agent's hosted prompt and greeting when it
  does this.

Nothing here creates an agent, buys a number, sends a message or places a call. The API key is
sent to api.agentphone.ai and nowhere else.

Needs AGENTPHONE_API_KEY in `.env`. If the account has more than one number, pick one with
`--number-ending 1234`.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx

from app.config import Settings, get_settings
from scripts.create_agent import _write_env

API = "https://api.agentphone.ai"
WEBHOOK_PATH = "/api/voice/hooks/agentphone"
WEBHOOK_TIMEOUT_S = 30


def _client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {settings.agentphone_api_key}"},
        timeout=30,
    )


def _check(response: httpx.Response) -> Any:
    if response.is_error:
        raise SystemExit(
            f"AgentPhone returned {response.status_code} for "
            f"{response.request.method} {response.request.url.path}: {response.text[:400]}"
        )
    return response.json()


def _items(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("data", []) if isinstance(payload, dict) else payload
    return [row for row in rows or [] if isinstance(row, dict)]


def _tail(number: object) -> str:
    return str(number or "")[-4:]


def choose(
    agents: list[dict[str, Any]],
    numbers: list[dict[str, Any]],
    settings: Settings,
    number_ending: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """Pick the number to call from and the agent that owns it. Returns a note when the
    choice cannot be made without the owner saying which."""
    number: dict[str, Any] | None = None
    if settings.agentphone_number_id:
        number = next((n for n in numbers if n.get("id") == settings.agentphone_number_id), None)
        if number is None:
            return None, None, "AGENTPHONE_NUMBER_ID in .env is not a number on this account."
    elif number_ending:
        matches = [n for n in numbers if str(n.get("phoneNumber", "")).endswith(number_ending)]
        if len(matches) != 1:
            return None, None, f"{len(matches)} numbers end in {number_ending}."
        number = matches[0]
    elif len(numbers) == 1:
        number = numbers[0]
    elif not numbers:
        return None, None, "This account has no phone number."
    else:
        return None, None, "More than one number: choose with --number-ending 1234."

    agent_id = settings.agentphone_agent_id or str(number.get("agentId") or "")
    agent = next((a for a in agents if a.get("id") == agent_id), None)
    if agent is None:
        return None, number, "That number is not attached to an agent on this account."
    if number.get("agentId") and number.get("agentId") != agent.get("id"):
        return agent, number, "AGENTPHONE_AGENT_ID is not the agent this number is attached to."
    return agent, number, ""


def show(settings: Settings, number_ending: str, write_env: bool) -> int:
    with _client(settings) as client:
        agents = _items(_check(client.get("/v1/agents", params={"limit": 100})))
        numbers = _items(_check(client.get("/v1/numbers", params={"limit": 100})))
        agent, number, note = choose(agents, numbers, settings, number_ending)
        webhook: Any = None
        if agent is not None:
            webhook = _check(client.get(f"/v1/agents/{agent['id']}/webhook"))

    print("Agents (AGENTPHONE_AGENT_ID):")
    for row in agents:
        print(f"  {row.get('id')}  {row.get('name')}  voiceMode: {row.get('voiceMode')}")
    if not agents:
        print("  (none)")
    print("\nNumbers (AGENTPHONE_NUMBER_ID):")
    for row in numbers:
        print(
            f"  {row.get('id')}  number ending {_tail(row.get('phoneNumber'))}  "
            f"status: {row.get('status')}  agent: {row.get('agentId')}"
        )
    if not numbers:
        print("  (none)")

    print()
    if note:
        print(f"Not chosen: {note}")
    if agent is None or number is None:
        return 1
    print(f"Using agent {agent['id']} and the number ending {_tail(number.get('phoneNumber'))}.")
    if write_env:
        _write_env("AGENTPHONE_AGENT_ID", str(agent["id"]))
        _write_env("AGENTPHONE_NUMBER_ID", str(number["id"]))
        print("Saved AGENTPHONE_AGENT_ID and AGENTPHONE_NUMBER_ID in .env.")
    else:
        print("Run again with --write-env to save both ids in .env.")

    print("\nBefore a real call:")
    steps: list[str] = []
    if agent.get("voiceMode") != "webhook":
        steps.append(
            f"the agent's voiceMode is {agent.get('voiceMode')!r}, not 'webhook': "
            "run --set-webhook-mode"
        )
    expected = f"{settings.public_base_url.rstrip('/')}{WEBHOOK_PATH}"
    registered = webhook.get("url") if isinstance(webhook, dict) else None
    if not settings.public_base_url.startswith("https://"):
        steps.append("open a tunnel and set PUBLIC_BASE_URL to its https:// address")
    if not registered:
        steps.append("the agent has no webhook: run --register-webhook")
    elif registered != expected:
        steps.append("the agent's webhook points somewhere else: run --register-webhook")
    if not settings.agentphone_webhook_secret:
        steps.append("AGENTPHONE_WEBHOOK_SECRET is blank: --register-webhook saves it")
    if not settings.owner_phone_number:
        steps.append("set OWNER_PHONE_NUMBER in .env")
    if settings.call_provider != "agentphone":
        steps.append("set CALL_PROVIDER=agentphone in .env")
    if not settings.calls_enabled:
        steps.append("set CALLS_ENABLED=true in .env when you want the phone to ring")
    for step in steps:
        print(f"  - {step}")
    if not steps:
        print("  nothing: the next over-limit request will ring the owner's phone")
    return 0


def _chosen_agent_id(settings: Settings, number_ending: str) -> str:
    if settings.agentphone_agent_id:
        return settings.agentphone_agent_id
    with _client(settings) as client:
        agents = _items(_check(client.get("/v1/agents", params={"limit": 100})))
        numbers = _items(_check(client.get("/v1/numbers", params={"limit": 100})))
    agent, _number, note = choose(agents, numbers, settings, number_ending)
    if agent is None:
        raise SystemExit(note or "No agent could be chosen. Run without flags to see why.")
    return str(agent["id"])


def register_webhook(settings: Settings, number_ending: str) -> int:
    if not settings.public_base_url.startswith("https://"):
        raise SystemExit(
            "Set PUBLIC_BASE_URL in .env to the public https:// address of this server first "
            "(for example a cloudflared tunnel). AgentPhone calls the webhook from the internet."
        )
    agent_id = _chosen_agent_id(settings, number_ending)
    url = f"{settings.public_base_url.rstrip('/')}{WEBHOOK_PATH}"
    with _client(settings) as client:
        created = _check(
            client.post(
                f"/v1/agents/{agent_id}/webhook",
                # contextLimit 0: the script keeps its own state, so no history is needed.
                json={"url": url, "contextLimit": 0, "timeout": WEBHOOK_TIMEOUT_S},
            )
        )
    secret = str(created.get("secret") or "")
    if not secret:
        raise SystemExit("AgentPhone returned no webhook secret. Nothing was saved.")
    _write_env("AGENTPHONE_WEBHOOK_SECRET", secret)
    _write_env("AGENTPHONE_AGENT_ID", agent_id)
    print(f"Agent {agent_id} now posts to {url}")
    print("Saved AGENTPHONE_WEBHOOK_SECRET in .env (not shown). Restart the server to load it.")
    return 0


def set_webhook_mode(settings: Settings, number_ending: str) -> int:
    agent_id = _chosen_agent_id(settings, number_ending)
    with _client(settings) as client:
        updated = _check(client.patch(f"/v1/agents/{agent_id}", json={"voiceMode": "webhook"}))
    print(f"Agent {agent_id} voiceMode is now {updated.get('voiceMode')!r}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--register-webhook", action="store_true", help="point the agent's webhook at this server"
    )
    mode.add_argument(
        "--set-webhook-mode", action="store_true", help="switch the agent to webhook voice mode"
    )
    parser.add_argument("--write-env", action="store_true", help="save the two ids in .env")
    parser.add_argument("--number-ending", default="", help="last digits of the number to use")
    args = parser.parse_args()
    settings = get_settings()
    if not settings.agentphone_api_key:
        raise SystemExit("Set AGENTPHONE_API_KEY in .env first.")
    if args.register_webhook:
        return register_webhook(settings, args.number_ending)
    if args.set_webhook_mode:
        return set_webhook_mode(settings, args.number_ending)
    return show(settings, args.number_ending, args.write_env)


if __name__ == "__main__":
    sys.exit(main())

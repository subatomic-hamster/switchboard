"""Create or update the ElevenLabs approval agent, and look up the ids `.env` needs.

    uv run python -m scripts.create_agent --list          # read-only: agents and phone numbers
    uv run python -m scripts.create_agent --dry-run       # print what would be sent
    uv run python -m scripts.create_agent --apply         # create (or update) the agent
    uv run python -m scripts.create_agent --apply --write-env   # also save the agent id in .env

`--apply` writes to your ElevenLabs account: it stores one workspace secret (the bearer token
for the voice tools), creates the seven webhook tools, and creates the agent, or updates it when
ELEVENLABS_AGENT_ID is already set. It places no call and costs no call minutes.

Needs in `.env`: ELEVENLABS_API_KEY, VOICE_TOKEN, and PUBLIC_BASE_URL (the public HTTPS address
of this server, for example a cloudflared tunnel), because ElevenLabs calls the tools from the
internet.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

import httpx

from app.agent_config import (
    AGENT_NAME,
    SECRET_NAME,
    agent_payload,
    market_tool_payloads,
    tool_payloads,
)
from app.config import REPO_ROOT, Settings, get_settings

API = "https://api.elevenlabs.io"


def _client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        base_url=API, headers={"xi-api-key": settings.elevenlabs_api_key}, timeout=30
    )


def _check(response: httpx.Response) -> Any:
    if response.is_error:
        raise SystemExit(
            f"ElevenLabs returned {response.status_code} for "
            f"{response.request.method} {response.request.url.path}: {response.text[:400]}"
        )
    return response.json()


def list_ids(settings: Settings) -> int:
    """Read-only. Shows the ids to copy into `.env`."""
    with _client(settings) as client:
        agents = _check(client.get("/v1/convai/agents", params={"page_size": 100}))
        numbers = _check(client.get("/v1/convai/phone-numbers"))
    print("Agents (ELEVENLABS_AGENT_ID):")
    for agent in agents.get("agents", []):
        marker = "  <- this project" if agent.get("name") == AGENT_NAME else ""
        print(f"  {agent.get('agent_id')}  {agent.get('name')}{marker}")
    if not agents.get("agents"):
        print("  (none yet; run with --apply to create one)")
    print("\nPhone numbers (ELEVENLABS_PHONE_NUMBER_ID):")
    for number in numbers:
        tail = str(number.get("phone_number", ""))[-4:]
        print(
            f"  {number.get('phone_number_id')}  {number.get('provider')}  "
            f"number ending {tail}  label: {number.get('label')}"
        )
    if not numbers:
        print("  (none; import your Twilio number in ElevenLabs first, see OVERNIGHT_LOG.md)")
    return 0


def _redacted(payload: Any) -> str:
    return json.dumps(payload, indent=2)


def dry_run(settings: Settings) -> int:
    base = settings.public_base_url or "https://YOUR-TUNNEL.example"
    print("# 1. workspace secret (value not shown)")
    print(_redacted({"type": "new", "name": SECRET_NAME, "value": "Bearer <VOICE_TOKEN>"}))
    print("\n# 2. tools: POST /v1/convai/tools, once each")
    for tool in [*tool_payloads(base, "<secret_id>"), *market_tool_payloads(base, "<secret_id>")]:
        print(_redacted(tool))
    print("\n# 3. agent: POST /v1/convai/agents/create (or PATCH when ELEVENLABS_AGENT_ID is set)")
    print(_redacted(agent_payload(settings, ["<tool ids from step 2>"])))
    return 0


def apply(settings: Settings, write_env: bool) -> int:
    missing = [
        name
        for name, value in (
            ("ELEVENLABS_API_KEY", settings.elevenlabs_api_key),
            ("VOICE_TOKEN", settings.voice_token),
            ("PUBLIC_BASE_URL", settings.public_base_url),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"Set these in .env first: {', '.join(missing)}")
    if not settings.public_base_url.startswith("https://"):
        raise SystemExit("PUBLIC_BASE_URL must be an https:// address ElevenLabs can reach.")

    with _client(settings) as client:
        secret = _check(
            client.post(
                "/v1/convai/secrets",
                json={
                    "type": "new",
                    "name": SECRET_NAME,
                    "value": f"Bearer {settings.voice_token}",
                },
            )
        )
        secret_id = str(secret["secret_id"])
        print(f"stored secret {SECRET_NAME}")

        tool_ids: list[str] = []
        for tool in [
            *tool_payloads(settings.public_base_url, secret_id),
            *market_tool_payloads(settings.public_base_url, secret_id),
        ]:
            created = _check(client.post("/v1/convai/tools", json=tool))
            tool_ids.append(str(created["id"]))
            print(f"created tool {tool['tool_config']['name']}")

        payload = agent_payload(settings, tool_ids)
        if settings.elevenlabs_agent_id:
            _check(client.patch(f"/v1/convai/agents/{settings.elevenlabs_agent_id}", json=payload))
            agent_id = settings.elevenlabs_agent_id
            print(f"updated agent {agent_id}")
        else:
            agent_id = str(
                _check(client.post("/v1/convai/agents/create", json=payload))["agent_id"]
            )
            print(f"created agent {agent_id}")

    if write_env:
        _write_env("ELEVENLABS_AGENT_ID", agent_id)
        print("saved ELEVENLABS_AGENT_ID in .env")
    else:
        print(f"\nAdd this line to .env:\nELEVENLABS_AGENT_ID={agent_id}")
    return 0


def _write_env(name: str, value: str) -> None:
    path = REPO_ROOT / ".env"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f"{name}={value}"
    if re.search(rf"^{name}=.*$", text, flags=re.MULTILINE):
        text = re.sub(rf"^{name}=.*$", line, text, flags=re.MULTILINE)
    else:
        text = f"{text.rstrip()}\n{line}\n"
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="read-only: show agent and number ids")
    mode.add_argument("--apply", action="store_true", help="create or update the agent")
    mode.add_argument("--dry-run", action="store_true", help="print the payloads (default)")
    parser.add_argument("--write-env", action="store_true", help="with --apply: save the agent id")
    args = parser.parse_args()
    settings = get_settings()

    if args.list:
        if not settings.elevenlabs_api_key:
            raise SystemExit("Set ELEVENLABS_API_KEY in .env first.")
        return list_ids(settings)
    if args.apply:
        return apply(settings, args.write_env)
    return dry_run(settings)


if __name__ == "__main__":
    sys.exit(main())

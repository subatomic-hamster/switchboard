# Switchboard

Tell it where you want your money to go, in plain speech, and a crew of agents moves it across
wallets, chains, banks and prediction markets, inside limits a contract enforces, phoning you
when something needs your say.

Built for the University Blockchain Conference 2026 Hackathon.

## Status

Planning is complete; implementation is starting. This README will be replaced with setup
instructions and architecture as the code lands.

## Documents

| File | What it is |
|---|---|
| [docs/product-plan.md](docs/product-plan.md) | What the product does, the safety design, sponsor tools, demo script |
| [docs/implementation-plan.md](docs/implementation-plan.md) | Roles for two people, stack, repo layout, shared interface, phases |
| [docs/prompts/](docs/prompts/) | Prompts for unattended Claude Code runs on each half |

## Stack

- Contracts: Solidity with Foundry, on Arc testnet
- Backend: Python, FastAPI
- Frontend: Vite, React, TypeScript
- Voice: ElevenLabs Agents with Twilio
- Data: QuickNode Streams, Gemini and Polymarket US market data

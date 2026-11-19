# Unattended run prompts

Two prompts for long Claude Code runs where nobody is available to answer questions.

| File | For |
|---|---|
| `p1-unattended.md` | P1, Money: contracts, chain, exchange clients |
| `p2-unattended.md` | P2, Voice and Surface: server skeleton, voice, dashboard |

Each prompt makes the agent ask everything it needs in one batch, then work without stopping and
record blockers and decisions in a log file at the repo root.

## How to use one

1. Clone this repo and open a terminal in it.
2. Start Claude Code and send: `Read docs/prompts/p2-unattended.md and follow it exactly.`
3. Answer its one batch of questions, and fill in `.env` when it lists the variable names.
4. Leave it running.

## Before you start

- **Permission prompts stall an unattended run.** Start Claude Code in a mode that does not ask
  before each command, and only in a folder that contains just this project. The safety rules in
  the prompts are instructions to the agent, not technical locks.
- **Leave out keys the work does not need.** P2 needs no exchange, wallet, Circle or QuickNode
  keys. Neither prompt allows real orders, and omitting exchange keys makes them impossible.
- **Keep the machine awake and online** for a local run.
- **Check you can push:** `gh auth status`.
- **Run P1 and P2 in separate clones** so two agents never share a working folder.

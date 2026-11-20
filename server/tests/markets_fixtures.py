"""Loaders for the recorded fixtures in server/fixtures/markets/ (see its README)."""

from __future__ import annotations

import json
from typing import Any

from app.config import FIXTURES_DIR

DIR = FIXTURES_DIR / "markets"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DIR / f"{name}.json").read_text(encoding="utf-8"))
    return data


def pm_market(case: str, slug: str) -> dict[str, Any]:
    return next(m for m in load(case)["polymarket"] if m["slug"] == slug)


def gem_event(case: str, ticker: str) -> dict[str, Any]:
    return next(e for e in load(case)["gemini"] if e["ticker"] == ticker)

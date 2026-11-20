"""The five-field equivalence check against recorded public responses.

Every row of the plan's reference table is covered. The real pairs were recorded on
2026-11-20 (see server/fixtures/markets/README.md); the NFL team total is synthetic, and its
fixture file says so.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.markets.match import MatchResult, evaluate
from tests.markets_fixtures import gem_event, load, pm_market


def run(case: str, slug: str, *tickers: str) -> MatchResult:
    events = load(case)["gemini"]
    if tickers:
        events = [e for e in events if e["ticker"] in tickers]
    return evaluate(pm_market(case, slug), events)


def fields(result: MatchResult) -> dict[str, Any]:
    return result.response.checks.model_dump()


def test_nba_game_winner_matches() -> None:
    r = run("nba_hou_den", "aec-nba-hou-den-2026-11-20")
    assert r.response.verdict == "match"
    assert fields(r) == {
        "event": "same",
        "strike": "n/a",
        "direction": "same",
        "time": "same",
        "source": "same",
    }
    assert r.response.gemini_symbol == "GEMI-NBA-2611210300-HOU-DEN-M-HOU"
    assert "postponement rules differ" in r.response.notes


def test_la_liga_three_way_matches_the_win_contract_not_the_draw() -> None:
    r = run("laliga_atm_get", "atc-lal-get-atm-2026-11-22-atm")
    assert r.response.verdict == "match"
    # Gemini lists Atletico, Tie and Getafe in one event; the Atletico contract is the one.
    assert r.response.gemini_symbol == "GEMI-LALIGA-2611222000-ATM-GET-M-ATM"


def test_bitcoin_above_strike_with_a_different_index_is_refused() -> None:
    r = run("btc_63000", "cpc-btc-above-day-2026-11-19-63000")
    assert r.response.verdict == "refuse"
    assert fields(r) == {
        "event": "same",
        "strike": "same",
        "direction": "same",
        "time": "same",
        "source": "different",
    }
    assert any("lukka" in n and "cf_benchmarks" in n for n in r.response.notes)
    assert r.response.gemini_symbol == "GEMI-BTC2611192200-HI63000"


def test_weather_bands_offset_by_one_degree_are_refused() -> None:
    # Polymarket US 56-57 against Gemini 55-56 and 57-58: no contract is the same band.
    r = run("wx_sfo_offset", "tc-temp-sfohigh-2026-11-20-gte56lt57f")
    assert r.response.verdict == "refuse"
    assert fields(r)["strike"] == "different"
    assert fields(r)["event"] == "same"
    assert r.response.gemini_symbol is not None  # the nearest band is named for the notes
    assert any("strikes differ" in n for n in r.response.notes)


def test_weather_with_aligned_bands_matches() -> None:
    r = run("wx_lax_aligned", "tc-temp-laxhigh-2026-11-20-gte66lt67f")
    assert r.response.verdict == "match"
    assert r.response.gemini_symbol == "GEMI-WXHIGH-LA-2611210459-66TO67"
    assert fields(r)["source"] == "same"


def test_weather_open_ended_bands_with_different_edges_are_refused() -> None:
    # Same city, same day, same mid bands; but "63 or below" is not "65 or below".
    low = run("wx_mdw_open_band", "tc-temp-mdwhigh-2026-11-20-lt64f")
    assert low.response.verdict == "refuse"
    assert fields(low)["strike"] == "different"
    mid = run("wx_mdw_open_band", "tc-temp-mdwhigh-2026-11-20-gte66lt67f")
    assert mid.response.verdict == "match"


def test_nfl_winner_matches_the_moneyline_not_the_totals() -> None:
    r = run("nfl_no_chi", "aec-nfl-no-chi-2026-11-22")
    assert r.response.verdict == "match"
    assert r.response.gemini_symbol == "GEMI-NFL-2611221800-NO-CHI-M-NO"


GAME_TOTAL = "NFL-2611221800-NO-CHI-T"


def synthetic(slug: str, *tickers: str) -> MatchResult:
    market = pm_market("nfl_team_total_SYNTHETIC", slug)
    events = [e for e in load("nfl_no_chi")["gemini"] if e["ticker"] in tickers]
    return evaluate(market, events)


def test_nfl_team_total_is_not_the_game_total_despite_identical_text() -> None:
    market = pm_market("nfl_team_total_SYNTHETIC", "tsc-nfl-no-chi-2026-11-22-tt-no-46pt5")
    twin = pm_market("nfl_team_total_SYNTHETIC", "tsc-nfl-no-chi-2026-11-22-total-46pt5")
    assert market["question"] == twin["question"]
    r = synthetic("tsc-nfl-no-chi-2026-11-22-tt-no-46pt5", GAME_TOTAL)
    assert r.response.verdict == "refuse"
    # Same line, same direction, same time: only the market differs.
    assert fields(r) == {
        "event": "different",
        "strike": "same",
        "direction": "same",
        "time": "same",
        "source": "same",
    }
    assert any("team total" in n and "total" in n for n in r.response.notes)


def test_nfl_team_total_is_refused_even_when_gemini_lists_team_totals() -> None:
    r = synthetic("tsc-nfl-no-chi-2026-11-22-tt-no-46pt5", GAME_TOTAL, "NFL-2611221800-NO-CHI-TT")
    assert r.response.verdict == "refuse"  # Gemini has no 46.5 team total for New Orleans


def test_nfl_game_total_matches_the_game_total() -> None:
    r = synthetic("tsc-nfl-no-chi-2026-11-22-total-46pt5", GAME_TOTAL)
    assert r.response.verdict == "match"
    assert r.response.gemini_symbol == "GEMI-NFL-2611221800-NO-CHI-T-O46"


def test_nfl_team_total_matches_the_same_teams_same_line_team_total() -> None:
    r = synthetic("tsc-nfl-no-chi-2026-11-22-tt-chi-14pt5", GAME_TOTAL, "NFL-2611221800-NO-CHI-TT")
    assert r.response.verdict == "match"
    assert r.response.gemini_symbol == "GEMI-NFL-2611221800-NO-CHI-TT-CHIO14"


def test_a_market_with_no_gemini_counterpart_is_refused_with_unknowns() -> None:
    r = evaluate(pm_market("nba_hou_den", "aec-nba-hou-den-2026-11-20"), [])
    assert r.response.verdict == "refuse"
    assert fields(r)["event"] == "unknown"
    assert r.response.gemini_symbol is None


def test_a_different_game_at_the_same_time_is_not_a_candidate() -> None:
    other = gem_event("laliga_atm_get", "LALIGA-2611222000-ATM-GET-M")
    r = evaluate(pm_market("nba_hou_den", "aec-nba-hou-den-2026-11-20"), [other])
    assert r.response.verdict == "refuse"
    assert fields(r)["event"] == "unknown"


def test_a_shifted_start_time_is_refused() -> None:
    market = dict(pm_market("nba_hou_den", "aec-nba-hou-den-2026-11-20"))
    market["gameStartTime"] = "2026-11-21T05:00:00Z"
    r = evaluate(market, load("nba_hou_den")["gemini"])
    # Two hours apart is still the same game for lookup, but the time check must fail.
    assert r.response.verdict == "refuse"
    assert fields(r)["time"] == "different"


def test_a_missing_direction_blocks_the_match() -> None:
    market = dict(pm_market("nba_hou_den", "aec-nba-hou-den-2026-11-20"))
    market["marketSides"] = [{**s, "long": False} for s in market["marketSides"]]
    r = evaluate(market, load("nba_hou_den")["gemini"])
    assert r.response.verdict == "refuse"
    assert fields(r)["direction"] == "unknown"


@pytest.mark.parametrize(
    "case",
    ["nba_hou_den", "laliga_atm_get", "btc_63000", "wx_sfo_offset", "nfl_no_chi"],
)
def test_real_fixtures_are_marked_as_recorded(case: str) -> None:
    doc = load(case)
    assert doc["synthetic"] is False
    assert doc["recorded_at"].startswith("2026-11-20")
    assert all(s.startswith("https://") for s in doc["sources"])

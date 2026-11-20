# Recorded market fixtures

Real public responses from Polymarket US (`gateway.polymarket.us`) and Gemini
(`api.gemini.com`), fetched on **2026-11-20** with `uv run python -m scripts.record_market_fixtures`
(public GETs, no keys). Each file lists its source URLs. Long rules text is trimmed. The tests read
these files and never touch the network.

| File | Polymarket US | Gemini | What it covers |
|---|---|---|---|
| `nba_hou_den.json` | `aec-nba-hou-den-2026-11-20` | `NBA-2611210300-HOU-DEN-M` | NBA game winner, match. Includes both order books. |
| `laliga_atm_get.json` | `atc-lal-get-atm-2026-11-22-atm` | `LALIGA-2611222000-ATM-GET-M` | La Liga three-way, match (Atletico win, not the draw). Both books. |
| `btc_63000.json` | `cpc-btc-above-day-2026-11-19-63000` | `BTC2611192200` | Bitcoin above $63,000: refuse, Lukka vs CF Benchmarks BRTI. Both venues already settled; still served. |
| `wx_sfo_offset.json` | `tc-temp-sfohigh-2026-11-20-gte56lt57f` | `WXHIGH-SFO-2611210459` | San Francisco high: Polymarket bands 56-57 / 58-59, Gemini bands 55-56 / 57-58. Refuse. |
| `wx_lax_aligned.json` | `tc-temp-laxhigh-2026-11-20-gte66lt67f` | `WXHIGH-LA-2611210459` | Los Angeles high, bands line up: match. |
| `wx_mdw_open_band.json` | `tc-temp-mdwhigh-2026-11-20-lt64f`, `...-gte66lt67f` | `WXHIGH-CHI-2611210459` | Chicago (Midway): the 66-67 band matches, "63 or below" vs "65 or below" does not. |
| `nfl_no_chi.json` | `aec-nfl-no-chi-2026-11-22` | `NFL-2611221800-NO-CHI-M`, `-T`, `-TT` | Real Gemini winner, game total and team total for one game, plus the real Polymarket winner market. |
| `nfl_team_total_SYNTHETIC.json` | three hand-built `tsc-` markets | (uses the real events in `nfl_no_chi.json`) | **Synthetic.** The team-total vs game-total look-alike. See its `note`. |

Why `nfl_team_total_SYNTHETIC.json` is synthetic: no Polymarket US `tsc-` (total) market was
listed for any NFL game on 2026-11-20 that I could find, so the Polymarket side of that row is
built by hand. The Gemini side is real: Gemini does list a game total (`...-T`) and a team total
(`...-TT`) for New Orleans at Chicago.

## What the reference table said, and what was live

| Row | Plan said | Seen live on 2026-11-20 |
|---|---|---|
| NBA game winner | Match | Match (game not yet played, starts 2026-11-21T03:00Z) |
| La Liga three-way | Match | Match |
| Bitcoin above a strike | Refuse, different index | Refuse. Gemini `sourceDetails.agency` = `lukka` (`LS-LRRR-XBTUSD`); Polymarket `assetPriceTerms.indexSymbol` = `BRTI` |
| City daily high | Refuse, bands offset by one degree | San Francisco is offset exactly as described. Los Angeles, New York and Philadelphia line up today; Chicago lines up in the middle and differs at the open-ended edges |
| NFL team total vs game total | Refuse | Gemini lists both; Polymarket's `tsc-` side not found, so synthetic |

## Field notes (confirmed in the recorded responses)

- Gemini `startTime` is the game start for sports events. For price events it is when the
  window opened; the settlement time is the contract's `expiryDate`. Weather events have no
  `startTime`; the date is in the title. Team-total events repeat each participant once per contract.
- Polymarket `gameStartTime` is the game start for sports, the window start for price markets
  (`assetPriceTerms.windowEnd` is the settlement time), and local midnight for daily weather.
  Weather band edges are in the market `title` ("66 to 67", "63 or below"), not the slug.
- Polymarket order books use the key `offers`, not `asks`.

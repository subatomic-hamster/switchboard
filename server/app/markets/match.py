"""The five-field equivalence check between a Polymarket US market and a Gemini contract.

Fields: event, strike, direction, event time, settlement source. Each comes out `same`,
`different`, `unknown` or `n/a`.

Where the times come from (never a listed end date, which is a settlement deadline):
- Polymarket US: `gameStartTime` for sports; `assetPriceTerms.windowEnd` for price markets;
  the date in the slug for daily weather (its `gameStartTime` is local midnight).
- Gemini: the event's `startTime` for sports games; the contract's `expiryDate` for price events
  (their `startTime` is when the window opened, not when it settles); the date in the event
  title for daily weather (no `startTime` is sent).
Settlement source: Gemini's `sourceDetails.agency` / `source` for price events, Polymarket's
`assetPriceTerms.indexSymbol`; the NWS climatological report for weather; the official result
of the contest for sports (the Gemini `agency` there is a data feed, not the settler).

Verdict: refuse if any field is `different`. It also refuses when the event, strike, direction
or time is `unknown`, because those four decide what is being bought: an unverified one is not
a match. An `unknown` source alone does not block (the plan's rule), but is noted.

Pure functions only. Fetching candidates is `app.markets.real`'s job.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.markets.gemini import contract_text
from app.schemas import CheckResult, MarketMatchResponse, MatchChecks

Kind = Literal["winner", "total", "team_total", "spread", "advance", "prop", "other"]

_STOP = {"club", "fc", "cf", "de", "the", "city", "united", "ac", "sc", "and", "of"}


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _dec(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


# ---------------------------------------------------------------------------- value types


@dataclass(frozen=True)
class Team:
    abbr: str = ""
    tokens: frozenset[str] = frozenset()

    @classmethod
    def of(cls, abbr: str = "", name: str = "") -> Team:
        words = {w for w in re.findall(r"[a-z]+", _fold(name)) if len(w) > 2 and w not in _STOP}
        return cls(abbr.lower(), frozenset(words))


def same_team(a: Team, b: Team) -> bool:
    """Abbreviations decide when both venues give one (Los Angeles Lakers and Clippers share
    every name word). Otherwise a shared distinctive name word counts."""
    if a.abbr and b.abbr:
        return a.abbr == b.abbr
    return bool(a.tokens & b.tokens)


def same_teams(a: Sequence[Team], b: Sequence[Team]) -> bool:
    return (
        len(a) == len(b)
        and all(any(same_team(x, y) for y in b) for x in a)
        and all(any(same_team(x, y) for x in a) for y in b)
    )


@dataclass(frozen=True)
class Strike:
    """A threshold (one bound None) or a band. `unit` is the temperature unit, else empty."""

    lo: Decimal | None = None
    hi: Decimal | None = None
    unit: str = ""

    @property
    def direction(self) -> str:
        if self.lo is not None and self.hi is not None:
            return "within"
        return "above" if self.lo is not None else "below"


@dataclass(frozen=True)
class SportsEvent:
    teams: tuple[Team, ...]
    kind: Kind
    scope: str = "full"
    subject: Team | None = None  # the team a team total is about


@dataclass(frozen=True)
class PriceEvent:
    asset: str
    style: str  # above_below, touch, range, up_down, other


@dataclass(frozen=True)
class WeatherEvent:
    city: str
    metric: str  # high or low


EventKey = SportsEvent | PriceEvent | WeatherEvent
Direction = Team | str


@dataclass(frozen=True)
class Descriptor:
    venue: str
    event: EventKey | None
    strike: Strike | None
    direction: Direction | None
    time: datetime | None
    time_tol: timedelta
    source: str | None
    notes: tuple[str, ...] = ()
    label: str = ""


# ----------------------------------------------------------------------------- weather

# Station codes on each venue that resolve to the same station (checked against both
# venues' rules text on 2026-11-20: Chicago-Midway/KMDW, LAX, NYC, SFO, PHL). A code not
# listed stands for itself.
_CITY = {"mdw": "chicago", "chi": "chicago", "lax": "los_angeles", "la": "los_angeles"}
_BAND = re.compile(r"(-?\d+)\s*°?\s*[FC]?\s+to\s+(-?\d+)", re.I)
_BELOW = re.compile(r"(-?\d+)\s*°?\s*[FC]?\s+or\s+below", re.I)
_ABOVE = re.compile(r"(-?\d+)\s*°?\s*[FC]?\s+or\s+(?:above|higher)", re.I)
_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        1,
    )
}


def parse_band(label: str, unit: str = "") -> Strike | None:
    if m := _BAND.search(label):
        return Strike(Decimal(m[1]), Decimal(m[2]), unit)
    if m := _BELOW.search(label):
        return Strike(None, Decimal(m[1]), unit)
    if m := _ABOVE.search(label):
        return Strike(Decimal(m[1]), None, unit)
    return None


def _day(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


# ------------------------------------------------------------------- Polymarket US side

_SPORTS_SLUG = re.compile(
    r"^(?P<prefix>[a-z]+)-(?P<league>[a-z0-9]+)-(?P<rest>.+?)-(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<tail>.+))?$"
)
_LINE = re.compile(r"(\d+)pt(\d+)")


def _pm_teams(market: Mapping[str, Any], abbrs: Sequence[str]) -> tuple[Team, ...]:
    names: dict[str, str] = {}
    for side in market.get("marketSides", []):
        team = side.get("team") or {}
        if team.get("abbreviation"):
            names[team["abbreviation"].lower()] = str(team.get("name", ""))
    return tuple(Team.of(a, names.get(a.lower(), "")) for a in abbrs)


def _over_under(text: str) -> str | None:
    low = text.lower()
    over, under = (
        re.search(r"\b(over|more than|above)\b", low),
        re.search(r"\b(under|fewer than|below)\b", low),
    )
    if over and not under:
        return "over"
    if under and not over:
        return "under"
    return None


def describe_polymarket(market: Mapping[str, Any]) -> Descriptor:
    slug = str(market.get("slug", ""))
    terms = market.get("assetPriceTerms")
    if isinstance(terms, Mapping):
        return _pm_price(market, terms)
    if slug.startswith("tc-temp-"):
        return _pm_weather(market)
    m = _SPORTS_SLUG.match(slug)
    if m and m["prefix"] in ("aec", "atc", "tsc", "asc", "ssc"):
        return _pm_sports(market, m)
    return Descriptor("polymarket", None, None, None, None, timedelta(0), None, label=slug)


def _pm_price(market: Mapping[str, Any], terms: Mapping[str, Any]) -> Descriptor:
    market_type = str(terms.get("marketType", ""))
    style = (
        "up_down"
        if "UP_DOWN" in market_type
        else "above_below"
        if "ABOVE_BELOW" in market_type
        else "touch"
        if ("HIT" in market_type or "TOUCH" in market_type)
        else "range"
        if "RANGE" in market_type
        else "other"
    )
    asset = str((terms.get("asset") or {}).get("symbol", "")).lower()
    text = str(market.get("description", ""))
    strike_value = _dec((terms.get("strike") or {}).get("value"))
    strike: Strike | None = None
    if style == "above_below" and strike_value is not None:
        low = text.lower()
        if re.search(r"at or above|above|greater than|at least", low) and not re.search(
            r"\bbelow\b", low
        ):
            strike = Strike(lo=strike_value)
        elif re.search(r"\bbelow\b|less than|under", low):
            strike = Strike(hi=strike_value)
    elif style == "range":
        lo, hi = (
            _dec((terms.get("rangeLower") or {}).get("value")),
            _dec((terms.get("rangeUpper") or {}).get("value")),
        )
        strike = Strike(lo, hi) if lo is not None or hi is not None else None
    index = str(terms.get("indexSymbol", ""))
    provider = _provider(index, text)
    return Descriptor(
        "polymarket",
        PriceEvent(asset, style) if asset else None,
        strike,
        "up" if style == "up_down" else strike.direction if strike else None,  # Yes = Up
        _dt(terms.get("windowEnd")),
        timedelta(minutes=1),
        provider,
        notes=(f"Polymarket US settles on {provider or 'an unnamed index'} ({index})",),
        label=str(market.get("slug", "")),
    )


# Index vendors. CF Benchmarks publishes BRTI (Bitcoin Real-Time Index) and the other "RTI"
# indices; Lukka publishes the LS-LRRR-* reference rates. Anything else is its own provider.
def _provider(index: str, text: str = "") -> str | None:
    probe = re.sub(r"[^a-z]", "", f"{index} {text}".lower())
    letters = re.sub(r"[^a-z]", "", index.lower())
    if "cfbenchmarks" in probe or letters.endswith("rti"):
        return "cf_benchmarks"
    if "lukka" in probe or index.upper().startswith("LS-"):
        return "lukka"
    return re.sub(r"[^a-z0-9]+", "_", index.lower()).strip("_") or None


def _pm_weather(market: Mapping[str, Any]) -> Descriptor:
    slug = str(market["slug"])
    m = re.match(
        r"^tc-temp-(?P<code>[a-z]+?)(?P<metric>high|low)-(?P<y>\d{4})-(?P<mo>\d\d)-(?P<d>\d\d)-.+?(?P<unit>[fc])$",
        slug,
    )
    if not m:
        return Descriptor("polymarket", None, None, None, None, timedelta(0), None, label=slug)
    strike = parse_band(str(market.get("title", "")), m["unit"].upper())
    text = str(market.get("description", ""))
    return Descriptor(
        "polymarket",
        WeatherEvent(_CITY.get(m["code"], m["code"]), m["metric"]),
        strike,
        strike.direction if strike else None,
        _day(int(m["y"]), int(m["mo"]), int(m["d"])),
        timedelta(0),
        "nws" if re.search(r"National Weather Service|\bNWS\b", text) else None,
        label=slug,
    )


def _pm_sports(market: Mapping[str, Any], m: re.Match[str]) -> Descriptor:
    slug = str(market["slug"])
    prefix, tail = m["prefix"], (m["tail"] or "")
    abbrs = m["rest"].split("-")[:2]
    teams = _pm_teams(market, abbrs)
    stype = str(market.get("sportsMarketType", "")).lower()
    tokens = f"-{m['rest']}-{tail}-"
    subject: Team | None = None
    strike: Strike | None = None
    direction: Direction | None = None
    kind: Kind = "other"
    if prefix == "tsc":
        line = _LINE.search(tail)
        if line:
            strike = Strike(lo=Decimal(f"{line[1]}.{line[2]}"))
        direction = _over_under(
            str(market.get("question", "")) + " " + str(market.get("description", ""))
        )
        if "-tt-" in tokens or "team_total" in stype:
            kind = "team_total"
            who = re.search(r"-tt-([a-z0-9]+)", tokens)
            if who:
                subject = _pm_teams(market, [who[1]])[0]
        elif "-total-" in tokens or "total" in stype:
            kind = "total"
    elif prefix in ("aec", "atc") and "winner" in stype:
        kind = "winner"
        long_side: Mapping[str, Any] = next(
            (s for s in market.get("marketSides", []) if s.get("long")), {}
        )
        t = long_side.get("team") or {}
        direction = Team.of(str(t.get("abbreviation", "")), str(t.get("name", ""))) if t else None
    elif prefix in ("ssc", "asc") or "spread" in stype:
        kind = "spread"
    scope = "full"
    if re.search(r"-(1h|2h|1q|2q|3q|4q)-|first_half|second_half|quarter", tokens + stype):
        scope = "partial"
    text = str(market.get("description", ""))
    return Descriptor(
        "polymarket",
        SportsEvent(teams, kind, scope, subject),
        strike if kind in ("total", "team_total", "spread") else None,
        direction,
        _dt(market.get("gameStartTime")),
        timedelta(minutes=5),
        "official_result" if kind != "other" else None,
        notes=(_postponement(text),),
        label=slug,
    )


def _postponement(pm_text: str) -> str:
    return "postponement rules differ"


# ------------------------------------------------------------------------ Gemini side

_TEMPLATE_KIND: dict[str, Kind] = {
    "sports-game": "winner",
    "sports-total": "total",
    "sports-team-totals": "team_total",
    "sports-spread": "spread",
    "sports-player-props": "prop",
}
_SUFFIX_KIND: dict[str, Kind] = {
    "M": "winner",
    "T": "total",
    "TT": "team_total",
    "S": "spread",
    "A": "advance",
}
_GEM_TICKER = re.compile(
    r"^(?P<league>[A-Z0-9]+)-(?P<ts>\d{10})-(?P<rest>.+)-(?P<suffix>(?:[1-4][HQ])?(?:TT|M|S|T|A|REGT|REGS)|PP[A-Z]+)$"
)


def _gem_kind(event: Mapping[str, Any]) -> tuple[Kind, str]:
    ticker = str(event.get("ticker", ""))
    m = _GEM_TICKER.match(ticker)
    suffix = m["suffix"] if m else ""
    scope = "partial" if re.match(r"^[1-4][HQ]", suffix) else "full"
    bare = re.sub(r"^[1-4][HQ]", "", suffix).removeprefix("REG")
    kind = _TEMPLATE_KIND.get(str(event.get("template", ""))) or _SUFFIX_KIND.get(bare) or "other"
    if suffix.startswith("PP"):
        kind = "prop"
    return kind, scope


def describe_gemini(event: Mapping[str, Any], contract: Mapping[str, Any]) -> Descriptor:
    ticker = str(event.get("ticker", ""))
    category = str(event.get("category", ""))
    label = str(contract.get("label", ""))
    if category == "Sports" or str(event.get("template", "")).startswith("sports"):
        return _gem_sports(event, contract)
    if ticker.startswith(("WXHIGH-", "WXLOW-")):
        return _gem_weather(event, contract)
    if category == "Crypto" or contract.get("strike") or "source" in event:
        return _gem_price(event, contract)
    return Descriptor("gemini", None, None, None, None, timedelta(0), None, label=label)


def _gem_price(event: Mapping[str, Any], contract: Mapping[str, Any]) -> Descriptor:
    ticker = str(event.get("ticker", ""))
    asset = (re.match(r"^[A-Z]+", ticker) or [""])[0].lower()
    strike_raw = contract.get("strike") or {}
    value = _dec(strike_raw.get("value"))
    stype = str(strike_raw.get("type", ""))
    strike: Strike | None = None
    if value is not None:
        strike = (
            Strike(lo=value)
            if stype in ("above", "over")
            else Strike(hi=value)
            if stype in ("below", "under")
            else None
        )
    style = (
        "above_below"
        if strike
        else "up_down"
        if str(contract.get("ticker", "")).upper() in ("UP", "DOWN")
        else "other"
    )
    details = event.get("sourceDetails") or {}
    agency, index = (
        str(details.get("agency", "")),
        str(event.get("source") or details.get("index", "")),
    )
    provider = _provider(agency) if agency else _provider(index)
    return Descriptor(
        "gemini",
        PriceEvent(asset, style) if asset else None,
        strike,
        str(contract.get("ticker", "")).lower()
        if style == "up_down"
        else strike.direction
        if strike
        else None,
        _dt(contract.get("expiryDate") or event.get("expiryDate")),
        timedelta(minutes=1),
        provider,
        notes=(f"Gemini settles on {provider or 'an unnamed index'} ({index})",),
        label=str(contract.get("label", "")),
    )


def _gem_weather(event: Mapping[str, Any], contract: Mapping[str, Any]) -> Descriptor:
    ticker = str(event["ticker"])
    m = re.match(r"^WX(?P<metric>HIGH|LOW)-(?P<code>[A-Z]+)-", ticker)
    title = str(event.get("title", ""))
    d = re.search(r"on ([A-Za-z]+) (\d{1,2}), (\d{4})", title)
    when = (
        _day(int(d[3]), _MONTHS[d[1].lower()], int(d[2])) if d and d[1].lower() in _MONTHS else None
    )
    label = str(contract.get("label", ""))
    unit = "C" if "°C" in label else "F" if "°F" in label else ""
    strike = parse_band(label, unit)
    if strike is None and contract.get("strike"):
        raw = contract["strike"]
        value = _dec(raw.get("value"))
        if value is not None:
            strike = (
                Strike(hi=value, unit=unit)
                if raw.get("type") == "below"
                else Strike(lo=value, unit=unit)
            )
    if strike is None and (single := re.fullmatch(r"\s*(-?\d+)\s*°\s*[FC]\s*", label)):
        strike = Strike(Decimal(single[1]), Decimal(single[1]), unit)
    text = contract_text(contract)
    return Descriptor(
        "gemini",
        WeatherEvent(_CITY.get(m["code"].lower(), m["code"].lower()), m["metric"].lower())
        if m
        else None,
        strike,
        strike.direction if strike else None,
        when,
        timedelta(0),
        "nws" if re.search(r"National Weather Service|\bNWS\b", text) else None,
        label=label,
    )


def _gem_sports(event: Mapping[str, Any], contract: Mapping[str, Any]) -> Descriptor:
    kind, scope = _gem_kind(event)
    # Team-total events repeat each participant once per contract (seen live): de-duplicate.
    unique = {
        str(p.get("abbreviation", "")).lower(): Team.of(
            str(p.get("abbreviation", "")), str(p.get("name", ""))
        )
        for p in event.get("participants", [])
    }
    teams = tuple(unique.values())
    symbol = str(contract.get("instrumentSymbol", ""))
    c_ticker = str(contract.get("ticker", ""))
    strike_raw = contract.get("strike") or {}
    strike: Strike | None = None
    value = _dec(strike_raw.get("value"))
    if value is not None and kind in ("total", "team_total", "spread"):
        strike = Strike(lo=value) if strike_raw.get("type") == "over" else Strike(hi=value)
    direction: Direction | None = None
    subject: Team | None = None
    if kind == "winner":
        if contract.get("sportsSide") == "tie" or c_ticker == "D":
            direction = "tie"
        else:
            target = contract.get("target") or {}
            direction = Team.of(c_ticker, str(target.get("name", "") or contract.get("label", "")))
    elif kind in ("total", "team_total"):
        direction = strike_raw.get("type") if strike_raw.get("type") in ("over", "under") else None
        if kind == "team_total":
            abbr = re.sub(r"[OU]\d+$", "", symbol.rsplit("-", 1)[-1]).lower()
            subject = next((t for t in teams if t.abbr == abbr), Team.of(abbr))
    return Descriptor(
        "gemini",
        SportsEvent(teams, kind, scope, subject),
        strike,
        direction,
        _dt(event.get("startTime")),
        timedelta(minutes=5),
        "official_result" if kind != "other" else None,
        label=str(contract.get("label", "")),
    )


# -------------------------------------------------------------------------- comparison


def _event_check(a: EventKey | None, b: EventKey | None) -> CheckResult:
    if a is None or b is None:
        return "unknown"
    if isinstance(a, SportsEvent) and isinstance(b, SportsEvent):
        if not a.teams or not b.teams:
            return "unknown"
        same = same_teams(a.teams, b.teams) and a.kind == b.kind and a.scope == b.scope
        if same and a.kind == "team_total":
            same = bool(a.subject and b.subject and same_team(a.subject, b.subject))
        return "same" if same else "different"
    if isinstance(a, PriceEvent) and isinstance(b, PriceEvent):
        return "same" if a == b else "different"
    if isinstance(a, WeatherEvent) and isinstance(b, WeatherEvent):
        return "same" if a == b else "different"
    return "different"


def compare(pm: Descriptor, gem: Descriptor) -> tuple[MatchChecks, list[str]]:
    notes: list[str] = []
    event = _event_check(pm.event, gem.event)

    if pm.strike is None and gem.strike is None:
        no_threshold = (
            isinstance(pm.event, SportsEvent) and pm.event.kind in ("winner", "advance")
        ) or (isinstance(pm.event, PriceEvent) and pm.event.style == "up_down")
        strike: CheckResult = "n/a" if no_threshold else "unknown"
    elif pm.strike is None or gem.strike is None:
        strike = "unknown"
    else:
        strike = "same" if pm.strike == gem.strike else "different"
        if strike == "different":
            notes.append(
                f"strikes differ: Polymarket US {_fmt(pm.strike)}, Gemini {_fmt(gem.strike)}"
            )

    if pm.direction is None or gem.direction is None:
        direction: CheckResult = "unknown"
    elif isinstance(pm.direction, Team) and isinstance(gem.direction, Team):
        direction = "same" if same_team(pm.direction, gem.direction) else "different"
    else:
        direction = "same" if pm.direction == gem.direction else "different"

    if pm.time is None or gem.time is None:
        time: CheckResult = "unknown"
    else:
        tol = max(pm.time_tol, gem.time_tol)
        time = "same" if abs(pm.time - gem.time) <= tol else "different"
        if time == "different":
            notes.append(f"event times differ: {pm.time.isoformat()} vs {gem.time.isoformat()}")

    if pm.source is None or gem.source is None:
        source: CheckResult = "unknown"
    else:
        source = "same" if pm.source == gem.source else "different"
        if source == "different":
            notes.append(
                "different settlement source: "
                f"Polymarket US uses {pm.source}, Gemini uses {gem.source}"
            )
    if event == "different":
        notes.append("the two contracts are not the same market: " + _event_diff(pm, gem))
    if isinstance(pm.event, SportsEvent) and event == "same" and gem.event is not None:
        notes.append("postponement rules differ")
    return MatchChecks(
        event=event, strike=strike, direction=direction, time=time, source=source
    ), notes


def _fmt(s: Strike) -> str:
    lo = "" if s.lo is None else f"{s.lo.normalize():f}"
    hi = "" if s.hi is None else f"{s.hi.normalize():f}"
    return f"{lo} to {hi}".strip() if lo and hi else f">= {lo}" if lo else f"<= {hi}"


def _event_diff(pm: Descriptor, gem: Descriptor) -> str:
    a, b = pm.event, gem.event
    if isinstance(a, SportsEvent) and isinstance(b, SportsEvent):
        if a.kind != b.kind:
            mine, theirs = a.kind.replace("_", " "), b.kind.replace("_", " ")
            return f"Polymarket US lists a {mine}, Gemini a {theirs}"
        if a.scope != b.scope:
            return "different part of the game"
        if a.kind == "team_total":
            return "team totals are about different teams"
        return "different teams"
    if isinstance(a, PriceEvent) and isinstance(b, PriceEvent):
        return f"different price market ({a.asset} {a.style} vs {b.asset} {b.style})"
    if isinstance(a, WeatherEvent) and isinstance(b, WeatherEvent):
        return f"different city or measure ({a.city} {a.metric} vs {b.city} {b.metric})"
    return "different kinds of market"


def verdict(checks: MatchChecks) -> Literal["match", "refuse"]:
    core = (checks.event, checks.strike, checks.direction, checks.time)
    if "different" in core or checks.source == "different" or "unknown" in core:
        return "refuse"
    return "match"


# ------------------------------------------------------------------ candidates and result


@dataclass
class MatchResult:
    response: MarketMatchResponse
    pm: Descriptor
    gemini: Descriptor | None = None
    gemini_event: Mapping[str, Any] | None = None
    gemini_contract: Mapping[str, Any] | None = None
    candidates: list[str] = field(default_factory=list)


def _related(pm: Descriptor, gem: Descriptor) -> bool:
    """Same game, same city and day, same asset at about the same time: worth comparing."""
    a, b = pm.event, gem.event
    if pm.time is None or gem.time is None:
        return False
    if isinstance(a, SportsEvent) and isinstance(b, SportsEvent):
        return same_teams(a.teams, b.teams) and abs(pm.time - gem.time) <= timedelta(hours=3)
    if isinstance(a, PriceEvent) and isinstance(b, PriceEvent):
        return a.asset == b.asset and abs(pm.time - gem.time) <= timedelta(days=1)
    if isinstance(a, WeatherEvent) and isinstance(b, WeatherEvent):
        return a.city == b.city and a.metric == b.metric and pm.time == gem.time
    return False


def _score(checks: MatchChecks) -> tuple[int, int]:
    values = list(checks.model_dump().values())
    return (values.count("same"), -values.count("different"))


def evaluate(
    pm_market: Mapping[str, Any], gemini_events: Sequence[Mapping[str, Any]]
) -> MatchResult:
    """Compare one Polymarket US market with the best-fitting Gemini contract among the
    contracts of the events that look like the same thing."""
    pm = describe_polymarket(pm_market)
    best: (
        tuple[
            tuple[int, int],
            MatchChecks,
            list[str],
            Descriptor,
            Mapping[str, Any],
            Mapping[str, Any],
        ]
        | None
    ) = None
    seen: list[str] = []
    for event in gemini_events:
        if event.get("status") not in (None, "active", "settled", "closed"):
            continue
        event_hit = False
        for contract in event.get("contracts", []):
            gem = describe_gemini(event, contract)
            if not _related(pm, gem):
                continue
            event_hit = True
            checks, notes = compare(pm, gem)
            score = _score(checks)
            if best is None or score > best[0]:
                best = (score, checks, notes, gem, event, contract)
        if event_hit:
            seen.append(str(event.get("ticker")))
    if best is None:
        unknown = MatchChecks(
            event="unknown", strike="unknown", direction="unknown", time="unknown", source="unknown"
        )
        return MatchResult(
            MarketMatchResponse(
                verdict="refuse",
                checks=unknown,
                gemini_symbol=None,
                notes=["no Gemini contract found for the same event"],
            ),
            pm,
        )
    _, checks, notes, gem, event, contract = best
    result = verdict(checks)
    if result == "refuse" and not any("not" in n or "differ" in n for n in notes):
        notes.append("a field could not be verified, so the move is refused")
    return MatchResult(
        MarketMatchResponse(
            verdict=result,
            checks=checks,
            gemini_symbol=str(contract.get("instrumentSymbol"))
            if result == "match" or checks.event == "same"
            else None,
            notes=notes,
        ),
        pm,
        gem,
        event,
        contract,
        seen,
    )

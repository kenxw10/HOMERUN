from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KalshiMarket, MarketFamilyDiscoveryItem, MarketFamilyDiscoveryRun, MarketMapping, MlbGame
from app.services.contracts import (
    FIRST_FIVE_SPREAD,
    FIRST_FIVE_TOTAL,
    FIRST_FIVE_WINNER,
    FULL_GAME_SPREAD,
    FULL_GAME_TOTAL,
    FULL_GAME_WINNER,
    MARKET_FAMILY_PREFIXES,
    PAPER_SUPPORTED_MARKET_FAMILIES,
    contract_labels,
    game_team_codes,
    market_type_from_ticker,
)
from app.services.kalshi_mlb_resolver import is_multivariate_market
from app.services.market_family_discovery import (
    _over_under_side,
    _parse_line_value,
    _selection_code,
)
from app.services.market_sync import _market_status, _update_market_fields
from app.services.spread_verification import verify_spread_market
from app.time_utils import ensure_aware_utc, get_dashboard_zone, today_eastern

PAPER_SUPPORTED = "paper_supported"
NEEDS_REVIEW = "needs_review"
UNSUPPORTED = "unsupported"
DISCOVERY_FINAL_STATUSES = {"completed", "partial_error", "partial_rate_limited"}
TEAM_TOTAL_PREFIX = "KXMLBTEAMTOTAL"
YES_SELECTION_TEXT_FIELDS = ("yes_sub_title", "yes_subtitle", "title", "subtitle")


@dataclass(frozen=True)
class ParsedFamilyMarket:
    family_key: str
    market_type: str
    line_value: Decimal | None
    selection_code: str | None
    over_under_side: str | None
    inning_scope: str
    settlement_rule_status: str
    mapping_status: str
    validation_status: str
    reason: str
    metadata: dict[str, object] | None = None


def _empty_family_counts() -> dict[str, int]:
    return {
        "items_seen": 0,
        "markets_upserted": 0,
        "mappings_created_or_updated": 0,
        "paper_supported": 0,
        "needs_review": 0,
        "unsupported": 0,
        "parse_failures": 0,
    }


def _raw_market(item: MarketFamilyDiscoveryItem) -> dict[str, Any]:
    raw = dict(item.raw_payload or {})
    raw.setdefault("ticker", item.returned_ticker)
    raw.setdefault("event_ticker", item.returned_event_ticker)
    raw.setdefault("title", item.title)
    raw.setdefault("subtitle", item.subtitle)
    raw.setdefault("yes_sub_title", item.yes_sub_title)
    raw.setdefault("no_sub_title", item.no_sub_title)
    raw.setdefault("rules_primary", item.rules_primary)
    raw.setdefault("rules_secondary", item.rules_secondary)
    raw.setdefault("custom_strike", item.custom_strike)
    raw.setdefault("functional_strike", item.functional_strike)
    raw.setdefault("status", item.raw_status or item.status)
    return raw


def _family_from_ticker(ticker: str) -> str | None:
    upper = ticker.upper()
    if upper.startswith(f"{TEAM_TOTAL_PREFIX}-"):
        return None
    for prefix, family in sorted(MARKET_FAMILY_PREFIXES.items(), key=lambda item: len(item[0]), reverse=True):
        if upper.startswith(f"{prefix}-"):
            return family
    return None


def _normalized_selection(raw: dict[str, Any], item: MarketFamilyDiscoveryItem) -> str | None:
    selection = item.selection_code or _selection_code(raw)
    return selection.upper() if selection else None


def _line_value(raw: dict[str, Any], item: MarketFamilyDiscoveryItem) -> Decimal | None:
    if item.line_value is not None:
        return item.line_value
    return _parse_line_value(raw)


def _total_line_value(line_value: Decimal | None) -> Decimal | None:
    if line_value is None:
        return None
    return abs(line_value)


def _is_team_selection(selection: str | None, game: MlbGame) -> bool:
    return selection in game_team_codes(game)


def _text_tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def _contains_phrase(tokens: list[str], phrase: str) -> bool:
    phrase_tokens = _text_tokens(phrase)
    if not phrase_tokens:
        return False
    return any(tokens[index : index + len(phrase_tokens)] == phrase_tokens for index in range(len(tokens)))


def _team_aliases(team: str, code: str | None) -> tuple[str, ...]:
    tokens = _text_tokens(team)
    aliases: list[str] = []
    if code:
        aliases.append(code)
    if tokens:
        aliases.append(" ".join(tokens))
    if len(tokens) >= 2:
        city = " ".join(tokens[:-1])
        nickname = tokens[-1]
        aliases.extend((city, nickname, f"{city} {nickname[0]}"))
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


def _team_selection_from_text(value: object, game: MlbGame) -> str | None:
    tokens = _text_tokens(str(value or ""))
    if not tokens:
        return None

    matches = set()
    for team, code in (
        (game.home_team, game.home_abbreviation),
        (game.away_team, game.away_abbreviation),
    ):
        normalized_code = (code or "").upper()
        if normalized_code and any(_contains_phrase(tokens, alias) for alias in _team_aliases(team, normalized_code)):
            matches.add(normalized_code)
    if len(matches) == 1:
        return next(iter(matches))
    return None


def _spread_selection_from_yes_text(raw: dict[str, Any], game: MlbGame) -> str | None:
    for field in YES_SELECTION_TEXT_FIELDS:
        selection = _team_selection_from_text(raw.get(field), game)
        if selection is not None:
            return selection
    return None


def _parse_discovered_item(item: MarketFamilyDiscoveryItem, game: MlbGame) -> ParsedFamilyMarket:
    raw = _raw_market(item)
    ticker = str(raw.get("ticker") or item.returned_ticker or "").upper()
    family_from_ticker = _family_from_ticker(ticker)
    family_key = item.family_key
    if family_from_ticker is None:
        return ParsedFamilyMarket(
            family_key=family_key,
            market_type="unknown",
            line_value=None,
            selection_code=None,
            over_under_side=None,
            inning_scope="unknown",
            settlement_rule_status=UNSUPPORTED,
            mapping_status="rejected",
            validation_status=UNSUPPORTED,
            reason="unsupported_or_team_total_prefix",
        )
    if family_key != family_from_ticker:
        family_key = family_from_ticker
    if family_key not in PAPER_SUPPORTED_MARKET_FAMILIES or family_key == "unsupported_team_total":
        return ParsedFamilyMarket(
            family_key=family_key,
            market_type=family_key,
            line_value=None,
            selection_code=None,
            over_under_side=None,
            inning_scope="unknown",
            settlement_rule_status=UNSUPPORTED,
            mapping_status="rejected",
            validation_status=UNSUPPORTED,
            reason="unsupported_family",
        )

    line_value = _line_value(raw, item)
    selection = _normalized_selection(raw, item)
    over_under = _over_under_side(raw)
    inning_scope = "first_five" if family_key.startswith("first_five") else "full_game"
    reason = "parsed"
    paper_supported = False
    metadata: dict[str, object] | None = None

    if family_key == FULL_GAME_WINNER:
        paper_supported = _is_team_selection(selection, game)
        reason = "team_selection_parsed" if paper_supported else "missing_or_invalid_team_selection"
    elif family_key == FIRST_FIVE_WINNER:
        paper_supported = _is_team_selection(selection, game) or selection == "TIE"
        reason = "outcome_selection_parsed" if paper_supported else "missing_or_invalid_f5_selection"
    elif family_key in {FULL_GAME_SPREAD, FIRST_FIVE_SPREAD}:
        if not _is_team_selection(selection, game):
            selection = _spread_selection_from_yes_text(raw, game)
        verification = verify_spread_market(
            game=game,
            family_key=family_key,
            raw=raw,
            line_value=line_value,
            selection_code=selection,
        )
        line_value = verification.line_value
        selection = verification.selection_code
        paper_supported = verification.verified
        reason = "spread_text_and_settlement_verified" if paper_supported else "spread_text_or_settlement_unverified"
        metadata = {"spread_verification": verification.as_metadata()}
    elif family_key in {FULL_GAME_TOTAL, FIRST_FIVE_TOTAL}:
        line_value = _total_line_value(line_value)
        paper_supported = over_under in {"over", "under"} and line_value is not None
        reason = "total_side_and_line_parsed" if paper_supported else "missing_total_side_or_line"

    settlement_rule_status = PAPER_SUPPORTED if paper_supported else NEEDS_REVIEW
    return ParsedFamilyMarket(
        family_key=family_key,
        market_type=family_key,
        line_value=line_value,
        selection_code=selection,
        over_under_side=over_under,
        inning_scope=inning_scope,
        settlement_rule_status=settlement_rule_status,
        mapping_status="confirmed" if paper_supported else NEEDS_REVIEW,
        validation_status=settlement_rule_status,
        reason=reason,
        metadata=metadata,
    )


def _latest_discovery_run(session: Session, target_date: date) -> MarketFamilyDiscoveryRun | None:
    return session.scalar(
        select(MarketFamilyDiscoveryRun)
        .where(MarketFamilyDiscoveryRun.target_date == target_date)
        .where(MarketFamilyDiscoveryRun.status.in_(DISCOVERY_FINAL_STATUSES))
        .order_by(MarketFamilyDiscoveryRun.started_at.desc(), MarketFamilyDiscoveryRun.id.desc())
        .limit(1)
    )


def _upsert_market_from_item(
    session: Session,
    item: MarketFamilyDiscoveryItem,
    parsed: ParsedFamilyMarket,
) -> KalshiMarket | None:
    raw = _raw_market(item)
    ticker = str(raw.get("ticker") or item.returned_ticker or "").strip()
    if not ticker:
        return None
    market = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == ticker))
    market = market or KalshiMarket(ticker=ticker, kalshi_market_id=str(raw.get("id") or raw.get("market_id") or ticker))
    _update_market_fields(market, raw, ticker, _market_status(raw))
    market.market_family = parsed.family_key
    market.market_type = parsed.market_type
    market.line_value = parsed.line_value
    market.selection_code = parsed.selection_code
    market.over_under_side = parsed.over_under_side
    market.inning_scope = parsed.inning_scope
    market.settlement_rule_status = parsed.settlement_rule_status
    if parsed.metadata:
        market.raw_payload = {**(market.raw_payload or {}), **parsed.metadata}
    session.add(market)
    session.flush()
    return market


def _upsert_mapping(
    session: Session,
    *,
    game: MlbGame,
    market: KalshiMarket,
    item: MarketFamilyDiscoveryItem,
    parsed: ParsedFamilyMarket,
) -> MarketMapping:
    mapping = session.scalar(
        select(MarketMapping)
        .where(MarketMapping.mlb_game_id == game.id)
        .where(MarketMapping.kalshi_market_id == market.id)
    )
    mapping = mapping or MarketMapping(mlb_game_id=game.id, kalshi_market_id=market.id)
    labels = contract_labels(
        game=game,
        market=market,
        market_ticker=market.ticker,
        market_type=parsed.market_type,
        selection_code=parsed.selection_code,
    )
    mapping.mapping_status = parsed.mapping_status
    mapping.confidence = item.confidence or Decimal("0.7500")
    mapping.rationale = f"PR3B_DISCOVERY_NORMALIZATION:{parsed.reason}"
    mapping.resolver_strategy = "market_family_discovery_sync"
    mapping.validation_status = parsed.validation_status
    mapping.market_family = parsed.family_key
    mapping.market_type = parsed.market_type
    mapping.line_value = parsed.line_value
    mapping.selection_code = parsed.selection_code
    mapping.over_under_side = parsed.over_under_side
    mapping.inning_scope = parsed.inning_scope
    mapping.settlement_rule_status = parsed.settlement_rule_status
    mapping.mapping_metadata = {
        **(mapping.mapping_metadata or {}),
        "source": "market_family_discovery",
        "discovery_item_id": item.id,
        "discovery_run_id": item.run_id,
        "family_key": parsed.family_key,
        "market_type": parsed.market_type,
        "line_value": str(parsed.line_value) if parsed.line_value is not None else None,
        "selection_code": parsed.selection_code,
        "over_under_side": parsed.over_under_side,
        "inning_scope": parsed.inning_scope,
        "settlement_rule_status": parsed.settlement_rule_status,
        "parse_reason": parsed.reason,
        "market_display": labels.market_display,
        "selection_display": labels.selection_display,
        "matchup_display": labels.matchup_display,
        "contract_display": labels.contract_display,
        **(parsed.metadata or {}),
    }
    session.add(mapping)
    return mapping


def sync_market_family_mappings(session: Session, target_date: date | None = None) -> dict[str, object]:
    day = target_date or today_eastern()
    run = _latest_discovery_run(session, day)
    summary: dict[str, object] = {
        "date": day.isoformat(),
        "run_id": run.id if run else None,
        "status": "no_discovery_run" if run is None else "completed",
        "by_family": {family: _empty_family_counts() for family in PAPER_SUPPORTED_MARKET_FAMILIES},
        "items_seen": 0,
        "markets_upserted": 0,
        "mappings_created_or_updated": 0,
        "paper_supported": 0,
        "needs_review": 0,
        "unsupported": 0,
        "parse_failures": 0,
        "warnings": [],
    }
    if run is None:
        return summary

    items = list(
        session.scalars(
            select(MarketFamilyDiscoveryItem)
            .where(MarketFamilyDiscoveryItem.run_id == run.id)
            .order_by(MarketFamilyDiscoveryItem.family_key.asc(), MarketFamilyDiscoveryItem.id.asc())
        )
    )
    by_family = summary["by_family"]
    assert isinstance(by_family, dict)

    for item in items:
        summary["items_seen"] = int(summary["items_seen"]) + 1
        family_counts = by_family.setdefault(item.family_key, _empty_family_counts())
        assert isinstance(family_counts, dict)
        family_counts["items_seen"] = int(family_counts["items_seen"]) + 1
        game = session.get(MlbGame, item.mlb_game_id) if item.mlb_game_id is not None else None
        raw = _raw_market(item)
        ticker = str(raw.get("ticker") or item.returned_ticker or "").upper()
        if game is None:
            summary["parse_failures"] = int(summary["parse_failures"]) + 1
            family_counts["parse_failures"] = int(family_counts["parse_failures"]) + 1
            continue
        if ticker.startswith(f"{TEAM_TOTAL_PREFIX}-") or is_multivariate_market(raw):
            summary["unsupported"] = int(summary["unsupported"]) + 1
            family_counts["unsupported"] = int(family_counts["unsupported"]) + 1
            continue

        parsed = _parse_discovered_item(item, game)
        family_counts = by_family.setdefault(parsed.family_key, family_counts)
        assert isinstance(family_counts, dict)
        if parsed.settlement_rule_status == UNSUPPORTED:
            summary["unsupported"] = int(summary["unsupported"]) + 1
            family_counts["unsupported"] = int(family_counts["unsupported"]) + 1
            continue

        market = _upsert_market_from_item(session, item, parsed)
        if market is None:
            summary["parse_failures"] = int(summary["parse_failures"]) + 1
            family_counts["parse_failures"] = int(family_counts["parse_failures"]) + 1
            continue
        _upsert_mapping(session, game=game, market=market, item=item, parsed=parsed)
        summary["markets_upserted"] = int(summary["markets_upserted"]) + 1
        summary["mappings_created_or_updated"] = int(summary["mappings_created_or_updated"]) + 1
        family_counts["markets_upserted"] = int(family_counts["markets_upserted"]) + 1
        family_counts["mappings_created_or_updated"] = int(family_counts["mappings_created_or_updated"]) + 1
        if parsed.settlement_rule_status == PAPER_SUPPORTED:
            summary["paper_supported"] = int(summary["paper_supported"]) + 1
            family_counts["paper_supported"] = int(family_counts["paper_supported"]) + 1
        else:
            summary["needs_review"] = int(summary["needs_review"]) + 1
            family_counts["needs_review"] = int(family_counts["needs_review"]) + 1

    session.commit()
    return summary


def latest_market_family_mapping_report(session: Session, target_date: date | None = None) -> dict[str, object]:
    day = target_date or today_eastern()
    local_start = datetime.combine(day, time.min, tzinfo=get_dashboard_zone())
    start = ensure_aware_utc(local_start)
    end = start + timedelta(days=1)
    rows = list(
        session.execute(
            select(MarketMapping, MlbGame, KalshiMarket)
            .join(MlbGame, MarketMapping.mlb_game_id == MlbGame.id)
            .join(KalshiMarket, MarketMapping.kalshi_market_id == KalshiMarket.id)
            .where(MlbGame.scheduled_start >= start)
            .where(MlbGame.scheduled_start < end)
            .order_by(MlbGame.scheduled_start.asc(), KalshiMarket.ticker.asc())
        )
    )
    items = []
    for mapping, game, market in rows[:500]:
        items.append(
            {
                "mapping_id": mapping.id,
                "game": f"{game.away_team} @ {game.home_team}",
                "market_ticker": market.ticker,
                "family_key": mapping.market_family or market.market_family or market_type_from_ticker(market.ticker),
                "mapping_status": mapping.mapping_status,
                "validation_status": mapping.validation_status,
                "settlement_rule_status": mapping.settlement_rule_status,
                "line_value": float(mapping.line_value) if mapping.line_value is not None else None,
                "selection_code": mapping.selection_code,
                "over_under_side": mapping.over_under_side,
                "inning_scope": mapping.inning_scope,
            }
        )
    return {"date": day.isoformat(), "items": items, "count": len(items)}

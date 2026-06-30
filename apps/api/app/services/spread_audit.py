from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KalshiMarket, MarketMapping, MlbGame
from app.services.spread_verification import SPREAD_FAMILIES, spread_verification_from_mapping
from app.time_utils import ensure_aware_utc, get_dashboard_zone, today_eastern, utc_now


def _day_bounds(target_date: date) -> tuple[datetime, datetime]:
    local_start = datetime.combine(target_date, time.min, tzinfo=get_dashboard_zone())
    start = ensure_aware_utc(local_start)
    return start, start + timedelta(days=1)


def run_spread_audit(
    session: Session,
    target_date: date | None = None,
    *,
    min_time_to_start_minutes: int | None = 45,
    max_time_to_start_minutes: int | None = 180,
) -> dict[str, object]:
    day = target_date or today_eastern()
    start, end = _day_bounds(day)
    now = utc_now()
    rows = list(
        session.execute(
            select(MarketMapping, MlbGame, KalshiMarket)
            .join(MlbGame, MarketMapping.mlb_game_id == MlbGame.id)
            .join(KalshiMarket, MarketMapping.kalshi_market_id == KalshiMarket.id)
            .where(MlbGame.scheduled_start >= start)
            .where(MlbGame.scheduled_start < end)
            .where((MarketMapping.market_family.in_(SPREAD_FAMILIES)) | (KalshiMarket.market_family.in_(SPREAD_FAMILIES)))
            .order_by(MlbGame.scheduled_start.asc(), KalshiMarket.ticker.asc())
        )
    )

    by_family: dict[str, dict[str, int]] = {}
    items: list[dict[str, object]] = []
    checked = verified = unverified = skipped_by_window = 0

    for mapping, game, market in rows:
        minutes_to_start = int((ensure_aware_utc(game.scheduled_start) - now).total_seconds() / 60)
        if min_time_to_start_minutes is not None and minutes_to_start < min_time_to_start_minutes:
            skipped_by_window += 1
            continue
        if max_time_to_start_minutes is not None and minutes_to_start > max_time_to_start_minutes:
            skipped_by_window += 1
            continue

        family = mapping.market_family or market.market_family or market.market_type or "unknown"
        family_counts = by_family.setdefault(family, {"checked": 0, "verified": 0, "unverified": 0})
        result = spread_verification_from_mapping(game=game, mapping=mapping, market=market)
        metadata = result.as_metadata()
        mapping.mapping_metadata = {**(mapping.mapping_metadata or {}), "spread_verification": metadata}
        if isinstance(market.raw_payload, dict):
            market.raw_payload = {**market.raw_payload, "spread_verification": metadata}
        else:
            market.raw_payload = {"spread_verification": metadata}
        session.add_all([mapping, market])

        checked += 1
        family_counts["checked"] += 1
        if result.verified:
            verified += 1
            family_counts["verified"] += 1
        else:
            unverified += 1
            family_counts["unverified"] += 1

        items.append(
            {
                "mapping_id": mapping.id,
                "market_ticker": market.ticker,
                "game": f"{game.away_abbreviation or game.away_team} @ {game.home_abbreviation or game.home_team}",
                "scheduled_start": ensure_aware_utc(game.scheduled_start).isoformat(),
                "minutes_to_start": minutes_to_start,
                "family": family,
                "verified": result.verified,
                "parser_status": result.parser_status,
                "settlement_rule_status": result.settlement_rule_status,
                "selection_code": result.selection_code,
                "line_value": float(result.line_value) if result.line_value is not None else None,
                "actual_contract_display": result.actual_contract_display,
                "no_contract_display": result.no_contract_display,
                "normalized_no_equivalent_display": result.normalized_no_equivalent_display,
                "parse_source": result.parse_source,
                "warnings": result.warnings,
            }
        )

    session.commit()
    return {
        "status": "completed",
        "target_date": day.isoformat(),
        "min_time_to_start_minutes": min_time_to_start_minutes,
        "max_time_to_start_minutes": max_time_to_start_minutes,
        "checked": checked,
        "verified": verified,
        "unverified": unverified,
        "skipped_by_window": skipped_by_window,
        "by_family": by_family,
        "items": items[:100],
        "paper_trades_created": 0,
    }

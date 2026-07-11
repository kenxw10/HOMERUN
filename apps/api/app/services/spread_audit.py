from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KalshiMarket, MarketMapping, MlbGame
from app.services.contracts import FULL_GAME_SPREAD
from app.services.spread_verification import FULL_GAME_SPREAD_AUDIT_STATUSES, SpreadVerification, spread_verification_from_mapping
from app.time_utils import ensure_aware_utc, get_dashboard_zone, today_eastern, utc_now


def _day_bounds(target_date: date) -> tuple[datetime, datetime]:
    local_start = datetime.combine(target_date, time.min, tzinfo=get_dashboard_zone())
    start = ensure_aware_utc(local_start)
    return start, start + timedelta(days=1)


def _status_kind(status: str) -> str:
    lowered = status.strip().lower()
    if any(token in lowered for token in ("cancel", "void")):
        return "void"
    if any(token in lowered for token in ("final", "game over", "completed")):
        return "final"
    return "open"


def _selected_score_pair(game: MlbGame, selection_code: str | None) -> tuple[int, int] | None:
    if game.home_score is None or game.away_score is None:
        return None
    selected = (selection_code or "").upper()
    if selected == (game.home_abbreviation or "").upper():
        return int(game.home_score), int(game.away_score)
    if selected == (game.away_abbreviation or "").upper():
        return int(game.away_score), int(game.home_score)
    return None


def _spread_result(value: Decimal) -> str:
    if value > 0:
        return "win"
    if value < 0:
        return "loss"
    return "push"


def _format_line(value: Decimal | None) -> str | None:
    if value is None:
        return None
    numeric = float(value)
    return f"+{numeric:g}" if numeric > 0 else f"{numeric:g}"


def _preview_formula_fields(game: MlbGame, verification: SpreadVerification) -> dict[str, object]:
    selected_code = verification.selection_code
    opponent_code = None
    if selected_code == (game.home_abbreviation or "").upper():
        opponent_code = (game.away_abbreviation or "").upper() or None
    elif selected_code == (game.away_abbreviation or "").upper():
        opponent_code = (game.home_abbreviation or "").upper() or None
    return {
        "selected_team": selected_code,
        "opponent_team": opponent_code,
        "threshold_runs": str(verification.threshold_runs) if verification.threshold_runs is not None else None,
        "selected_margin_required_gt": (
            str(verification.selected_team_margin_required_gt)
            if verification.selected_team_margin_required_gt is not None
            else None
        ),
        "normalized_spread_line_display": _format_line(verification.display_spread_line or verification.line_value),
        "yes_condition": verification.settlement_formula,
        "condition_type": verification.condition_type,
    }


def _settlement_preview(game: MlbGame, verification: SpreadVerification) -> dict[str, object]:
    selection_code = verification.selection_code
    line_value = verification.line_value
    formula_fields = _preview_formula_fields(game, verification)
    if _status_kind(game.status) == "void":
        return {"preview_status": "void_game", **formula_fields}
    if _status_kind(game.status) != "final":
        return {"preview_status": "pending_final", **formula_fields}
    if line_value is None:
        return {"preview_status": "missing_line", **formula_fields}
    scores = _selected_score_pair(game, selection_code)
    if scores is None:
        return {"preview_status": "missing_score_or_selection", **formula_fields}
    selected_runs, opponent_runs = scores
    margin = selected_runs - opponent_runs
    if (
        verification.condition_type == "team_wins_by_more_than"
        and verification.selected_team_margin_required_gt is not None
    ):
        threshold = verification.selected_team_margin_required_gt
        adjusted_margin = Decimal(margin) - threshold
        if Decimal(margin) > threshold:
            yes_outcome = "win"
            no_outcome = "loss"
            push = False
        elif Decimal(margin) == threshold and verification.push_possible and verification.push_rule_verified:
            yes_outcome = "push"
            no_outcome = "push"
            push = True
        else:
            yes_outcome = "loss"
            no_outcome = "win"
            push = False
    else:
        adjusted_margin = Decimal(margin) + line_value
        yes_outcome = _spread_result(adjusted_margin)
        no_outcome = yes_outcome
        if yes_outcome == "win":
            no_outcome = "loss"
        elif yes_outcome == "loss":
            no_outcome = "win"
        push = yes_outcome == "push"
    return {
        "preview_status": "computed",
        "selected_team_score": selected_runs,
        "opponent_score": opponent_runs,
        "line_value": str(line_value),
        "selected_team_margin": margin,
        "line_adjusted_margin": str(adjusted_margin.quantize(Decimal("0.0001"))),
        "yes_outcome": yes_outcome,
        "no_outcome": no_outcome,
        "push": push,
        **formula_fields,
    }


def _status_counter_template() -> dict[str, int]:
    return {status: 0 for status in sorted(FULL_GAME_SPREAD_AUDIT_STATUSES)}


def _coverage_status(
    *,
    target_date_mapping_count: int,
    in_window_mapping_count: int,
    checked: int,
) -> str:
    if target_date_mapping_count == 0:
        return "no_target_date_mappings"
    if in_window_mapping_count == 0:
        return "no_mappings_in_window"
    if checked == 0:
        return "zero_checked_with_eligible_mappings"
    if checked < in_window_mapping_count:
        return "partial_coverage"
    if checked == in_window_mapping_count:
        return "covered"
    return "unknown"


def _zero_checked_reason(
    *,
    target_date_mapping_count: int,
    in_window_mapping_count: int,
    checked: int,
) -> str:
    if checked > 0:
        return "not_applicable"
    if target_date_mapping_count == 0:
        return "no_target_date_mappings"
    if in_window_mapping_count == 0:
        return "all_target_date_mappings_outside_window"
    return "eligible_mappings_but_none_checked"


def _append_example(examples: dict[str, list[dict[str, object]]], reason: str, item: dict[str, object]) -> None:
    bucket = examples.setdefault(reason, [])
    if len(bucket) >= 3:
        return
    bucket.append(
        {
            "market_ticker": item.get("market_ticker"),
            "game": item.get("game"),
            "audit_status": item.get("audit_status"),
            "reason_codes": item.get("reason_codes", []),
        }
    )


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
            .where(
                (MarketMapping.market_family == FULL_GAME_SPREAD)
                | (MarketMapping.market_type == FULL_GAME_SPREAD)
                | (KalshiMarket.market_family == FULL_GAME_SPREAD)
                | (KalshiMarket.market_type == FULL_GAME_SPREAD)
                | (KalshiMarket.ticker.ilike("KXMLBSPREAD-%"))
            )
            .order_by(MlbGame.scheduled_start.asc(), KalshiMarket.ticker.asc())
        )
    )

    by_family: dict[str, dict[str, int]] = {}
    items: list[dict[str, object]] = []
    examples_by_reason: dict[str, list[dict[str, object]]] = {}
    status_counts = _status_counter_template()
    checked = verified = unverified = 0
    skipped_before_min_window_count = 0
    skipped_after_max_window_count = 0
    target_date_mapping_count = len(rows)
    target_date_distinct_market_count = len({market.id for _, _, market in rows})
    target_date_distinct_game_count = len({game.id for _, game, _ in rows})
    in_window_mapping_count = 0

    for mapping, game, market in rows:
        minutes_to_start = int((ensure_aware_utc(game.scheduled_start) - now).total_seconds() / 60)
        if min_time_to_start_minutes is not None and minutes_to_start < min_time_to_start_minutes:
            skipped_before_min_window_count += 1
            continue
        if max_time_to_start_minutes is not None and minutes_to_start > max_time_to_start_minutes:
            skipped_after_max_window_count += 1
            continue
        in_window_mapping_count += 1

        family = mapping.market_family or market.market_family or market.market_type or "unknown"
        family_counts = by_family.setdefault(family, {"checked": 0, "verified": 0, "unverified": 0})
        result = spread_verification_from_mapping(game=game, mapping=mapping, market=market)
        metadata = result.as_metadata()
        settlement_preview = _settlement_preview(game, result)

        checked += 1
        family_counts["checked"] += 1
        if result.verified:
            verified += 1
            family_counts["verified"] += 1
        else:
            unverified += 1
            family_counts["unverified"] += 1
        status_counts[result.audit_status] = status_counts.get(result.audit_status, 0) + 1

        item = {
            "mapping_id": mapping.id,
            "market_id": market.id,
            "market_ticker": market.ticker,
            "event_ticker": market.event_ticker,
            "game_id": game.id,
            "game": f"{game.away_abbreviation or game.away_team} @ {game.home_abbreviation or game.home_team}",
            "mapped_mlb_game": {
                "external_game_id": game.external_game_id,
                "away_team": game.away_team,
                "away_abbreviation": game.away_abbreviation,
                "home_team": game.home_team,
                "home_abbreviation": game.home_abbreviation,
                "status": game.status,
                "away_score": game.away_score,
                "home_score": game.home_score,
            },
            "scheduled_start": ensure_aware_utc(game.scheduled_start).isoformat(),
            "minutes_to_start": minutes_to_start,
            "family": family,
            "market_family": FULL_GAME_SPREAD,
            "inning_scope": result.inning_scope,
            "audit_status": result.audit_status,
            "reason_codes": list(result.reason_codes or []),
            "verified": result.verified,
            "trusted_audit_only": result.audit_status == "trusted_audit_only",
            "parser_status": result.parser_status,
            "settlement_rule_status": result.settlement_rule_status,
            "selection_code": result.selection_code,
            "selected_team": result.selection_code,
            "line_value": float(result.line_value) if result.line_value is not None else None,
            "line_value_raw": str(result.line_value) if result.line_value is not None else None,
            "line_sign": result.line_sign,
            "line_direction": result.line_direction,
            "condition_type": result.condition_type,
            "rules_threshold_runs": str(result.threshold_runs) if result.threshold_runs is not None else None,
            "raw_threshold_runs": str(result.raw_threshold_runs) if result.raw_threshold_runs is not None else None,
            "selected_team_margin_required_gt": (
                str(result.selected_team_margin_required_gt)
                if result.selected_team_margin_required_gt is not None
                else None
            ),
            "display_spread_line": str(result.display_spread_line) if result.display_spread_line is not None else None,
            "settlement_formula": result.settlement_formula,
            "ticker_suffix_line_raw": result.ticker_suffix_line_raw,
            "over_under_side": None,
            "title": market.title,
            "subtitle": market.subtitle,
            "rules": market.rules,
            "yes_subtitle": market.yes_subtitle,
            "no_subtitle": market.no_subtitle,
            "raw_contract_text": metadata["raw_contract_text"],
            "actual_contract_display": result.actual_contract_display,
            "no_contract_display": result.no_contract_display,
            "normalized_no_equivalent_display": result.normalized_no_equivalent_display,
            "yes_outcome_interpretation": result.yes_interpretation,
            "no_outcome_interpretation": result.no_interpretation,
            "no_is_true_complement": result.no_is_true_complement,
            "no_text_source": result.no_text_source,
            "no_complement_source": result.no_complement_source,
            "no_complement_confidence": result.no_complement_confidence,
            "complement_safe_for_paper_settlement": result.complement_safe_for_paper_settlement,
            "push_possible": result.push_possible,
            "push_condition": result.push_condition,
            "push_rule_verified": result.push_rule_verified,
            "settlement_preview": settlement_preview,
            "parse_source": result.parse_source,
            "warnings": result.warnings,
        }
        for reason in result.reason_codes or []:
            _append_example(examples_by_reason, reason, item)
        items.append(item)

    skipped_by_window = skipped_before_min_window_count + skipped_after_max_window_count
    coverage_ratio = round(checked / in_window_mapping_count, 4) if in_window_mapping_count else 0.0
    coverage_status = _coverage_status(
        target_date_mapping_count=target_date_mapping_count,
        in_window_mapping_count=in_window_mapping_count,
        checked=checked,
    )
    zero_checked_reason = _zero_checked_reason(
        target_date_mapping_count=target_date_mapping_count,
        in_window_mapping_count=in_window_mapping_count,
        checked=checked,
    )
    return {
        "status": "completed",
        "target_date": day.isoformat(),
        "min_time_to_start_minutes": min_time_to_start_minutes,
        "max_time_to_start_minutes": max_time_to_start_minutes,
        "target_date_mapping_count": target_date_mapping_count,
        "target_date_distinct_market_count": target_date_distinct_market_count,
        "target_date_distinct_game_count": target_date_distinct_game_count,
        "in_window_mapping_count": in_window_mapping_count,
        "checked": checked,
        "verified": verified,
        "unverified": unverified,
        "skipped_by_window": skipped_by_window,
        "skipped_before_min_window_count": skipped_before_min_window_count,
        "skipped_after_max_window_count": skipped_after_max_window_count,
        "coverage_ratio": coverage_ratio,
        "coverage_status": coverage_status,
        "zero_checked_reason": zero_checked_reason,
        "by_family": by_family,
        "audit_scope": "full_game_spread",
        "audit_only": True,
        "read_only": True,
        "mapping_mutations": 0,
        "settlement_rows_created": 0,
        "total_full_game_spread_markets_seen": checked,
        "mapped_to_games": checked,
        "status_counts": status_counts,
        "trusted_audit_only_count": status_counts.get("trusted_audit_only", 0),
        "needs_review_count": status_counts.get("needs_review", 0),
        "unsafe_count": status_counts.get("unsafe", 0),
        "parse_error_count": status_counts.get("parse_error", 0),
        "missing_line_count": status_counts.get("missing_line", 0),
        "ambiguous_team_selection_count": status_counts.get("ambiguous_team_selection", 0),
        "ambiguous_yes_no_semantics_count": status_counts.get("ambiguous_yes_no_semantics", 0),
        "ambiguous_line_direction_count": status_counts.get("ambiguous_line_direction", 0),
        "push_behavior_uncertain_count": status_counts.get("push_behavior_uncertain", 0),
        "settlement_text_unverified_count": status_counts.get("settlement_text_unverified", 0),
        "examples_by_reason": examples_by_reason,
        "items": items[:100],
        "paper_trades_created": 0,
    }

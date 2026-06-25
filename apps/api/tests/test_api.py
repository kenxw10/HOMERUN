from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import Base, database_status
from app.main import _candidate_summary, app
from app.models import BalanceSnapshot, KalshiMarket, MarketMapping, MlbGame, ModelCandidate, PaperTrade, Position
from app.security import require_internal_api_key
from app.services import candidates, dashboard, market_sync
from app.services.kalshi import KalshiClient, derive_orderbook_prices
from app.services.mapping import infer_market_type, score_mapping, sync_market_mappings
from app.time_utils import classify_time_bucket

client = TestClient(app)


def test_health_uses_safe_defaults() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "homerun-api"
    assert payload["paper_trading"] is True
    assert payload["live_trading_enabled"] is False
    assert "timestamp" in payload


def test_dashboard_summary_shape_is_empty_and_safe() -> None:
    response = client.get("/v1/dashboard/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["portfolio_series"] == []
    assert payload["performance"] == {
        "win_rate": None,
        "roi": None,
        "profit_loss": 0.0,
        "record": "0-0-0",
    }
    assert payload["positions"] == []
    assert payload["bot"]["mode"] == "paper"
    assert payload["bot"]["live_trading_enabled"] is False
    assert payload["bot"]["execution_kill_switch"] is True
    assert payload["model_status"]["candidate_count"] == 0


def test_system_status_redacts_secrets_and_allows_missing_database() -> None:
    response = client.get("/v1/system/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["backend"]["ready"] is True
    assert payload["database"]["configured"] is False
    assert payload["database"]["ready"] is False
    assert payload["config"]["ready"] is True
    assert payload["config"]["kalshi_credentials"] == "not_set"
    assert "KALSHI_API_KEY" not in str(payload)


def test_database_status_does_not_mark_unreachable_database_ready(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://bad:bad@127.0.0.1:1/bad")
    get_settings.cache_clear()

    try:
        status = database_status()
    finally:
        get_settings.cache_clear()

    assert status["configured"] is True
    assert status["ready"] is False
    assert status["dialect"] == "postgresql+psycopg"
    assert status["message"] == "Database connection failed; check DATABASE_URL and network access."


def test_kalshi_orderbook_derives_asks_from_yes_and_no_bids() -> None:
    derived = derive_orderbook_prices(
        {
            "yes": [[58, 100], [61, 40]],
            "no": [[37, 90], [39, 20]],
        }
    )

    assert str(derived["best_yes_bid"]) == "0.6100"
    assert str(derived["best_no_bid"]) == "0.3900"
    assert str(derived["implied_yes_ask"]) == "0.6100"
    assert str(derived["implied_no_ask"]) == "0.3900"

    low_price = derive_orderbook_prices({"yes": [[1, 100]], "no": [[99, 100]]})

    assert str(low_price["best_yes_bid"]) == "0.0100"
    assert str(low_price["best_no_bid"]) == "0.9900"
    assert str(low_price["implied_yes_ask"]) == "0.0100"
    assert str(low_price["implied_no_ask"]) == "0.9900"


def test_kalshi_client_iter_markets_follows_cursor_until_exhausted(monkeypatch) -> None:
    client_instance = KalshiClient(base_url="https://example.test")
    calls: list[dict[str, object]] = []

    def fake_get_markets(params: dict[str, object]):
        calls.append(dict(params))
        if len(calls) == 1:
            return {"markets": [{"ticker": "PAGE-1"}], "cursor": "next-page"}
        return {"markets": [{"ticker": "PAGE-2"}], "cursor": ""}

    monkeypatch.setattr(client_instance, "get_markets", fake_get_markets)

    markets = list(client_instance.iter_markets(params={"limit": 200}))

    assert [market["ticker"] for market in markets] == ["PAGE-1", "PAGE-2"]
    assert calls == [{"limit": 200}, {"limit": 200, "cursor": "next-page"}]


def test_time_bucket_classification() -> None:
    assert classify_time_bucket(1500) == "24H"
    assert classify_time_bucket(725) == "12H"
    assert classify_time_bucket(95) == "90M"
    assert classify_time_bucket(20) == "15M"
    assert classify_time_bucket(1) == "5M"
    assert classify_time_bucket(0) == "POST_START"
    assert classify_time_bucket(-1) == "POST_START"


def test_candidate_day_bounds_use_dashboard_timezone(monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_TIMEZONE", "America/New_York")
    get_settings.cache_clear()

    try:
        day, start, end = candidates._candidate_day_bounds(datetime(2026, 7, 2, 1, 0, tzinfo=UTC))
    finally:
        get_settings.cache_clear()

    assert day.isoformat() == "2026-07-01"
    assert start == datetime(2026, 7, 1, 4, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 2, 4, 0, tzinfo=UTC)


def test_generate_candidates_preserves_traded_candidate_snapshot(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="preserve-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=now + timedelta(hours=25),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-PRESERVE",
            ticker="KXMLB-PRESERVE",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=now + timedelta(hours=25),
            implied_yes_ask=Decimal("0.4000"),
        )
        session.add_all([game, market])
        session.commit()

        first_result = candidates.generate_candidates(session)
        trade = session.scalar(select(PaperTrade).where(PaperTrade.market_ticker == "KXMLB-PRESERVE"))
        assert trade is not None
        traded_candidate = session.get(ModelCandidate, trade.candidate_id)
        assert traded_candidate is not None
        assert first_result["paper_trades"] == 1
        assert traded_candidate.executable_price == Decimal("0.4000")

        market.implied_yes_ask = Decimal("0.3500")
        session.add(market)
        session.commit()

        second_result = candidates.generate_candidates(session)
        all_candidates = list(session.scalars(select(ModelCandidate).order_by(ModelCandidate.id.asc())))
        all_trades = list(session.scalars(select(PaperTrade)))

    assert second_result["paper_trades"] == 0
    assert len(all_trades) == 1
    assert len(all_candidates) == 2
    assert all_candidates[0].id == traded_candidate.id
    assert all_candidates[0].executable_price == Decimal("0.4000")
    assert all_candidates[1].executable_price == Decimal("0.3500")
    assert all_candidates[1].decision == "candidate_only_existing_trade"


def test_generate_candidates_avoids_duplicate_open_trade_across_days(monkeypatch) -> None:
    current_time = {"now": datetime(2026, 7, 1, 16, 0, tzinfo=UTC)}
    monkeypatch.setattr(candidates, "utc_now", lambda: current_time["now"])

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="duplicate-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 4, 0, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-DUPLICATE",
            ticker="KXMLB-DUPLICATE",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 4, 0, 0, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
        )
        session.add_all([game, market])
        session.commit()

        first_result = candidates.generate_candidates(session)
        current_time["now"] = datetime(2026, 7, 2, 16, 0, tzinfo=UTC)
        second_result = candidates.generate_candidates(session)

        all_candidates = list(session.scalars(select(ModelCandidate).order_by(ModelCandidate.id.asc())))
        all_trades = list(session.scalars(select(PaperTrade).order_by(PaperTrade.id.asc())))

    assert first_result["paper_trades"] == 1
    assert second_result["paper_trades"] == 0
    assert len(all_candidates) == 2
    assert len(all_trades) == 1
    assert all_trades[0].market_ticker == "KXMLB-DUPLICATE"
    assert all_candidates[0].decision == "paper_trade"
    assert all_candidates[1].decision == "candidate_only_existing_trade"


def test_generate_candidates_avoids_duplicate_open_trade_across_mappings(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "sync_market_mappings", lambda session: 0)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine, autoflush=False) as session:
        first_game = MlbGame(
            external_game_id="doubleheader-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 2, 18, 0, tzinfo=UTC),
            status="scheduled",
        )
        second_game = MlbGame(
            external_game_id="doubleheader-2",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 2, 20, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-DOUBLEHEADER",
            ticker="KXMLB-DOUBLEHEADER",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 2, 18, 5, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
        )
        session.add_all([first_game, second_game, market])
        session.flush()
        session.add_all(
            [
                MarketMapping(
                    mlb_game_id=first_game.id,
                    kalshi_market_id=market.id,
                    mapping_status="candidate",
                    confidence=Decimal("0.9500"),
                ),
                MarketMapping(
                    mlb_game_id=second_game.id,
                    kalshi_market_id=market.id,
                    mapping_status="candidate",
                    confidence=Decimal("0.9500"),
                ),
            ]
        )
        session.commit()

        result = candidates.generate_candidates(session)
        all_candidates = list(session.scalars(select(ModelCandidate).order_by(ModelCandidate.id.asc())))
        all_trades = list(session.scalars(select(PaperTrade).order_by(PaperTrade.id.asc())))

    assert result["candidates"] == 2
    assert result["paper_trades"] == 1
    assert len(all_candidates) == 2
    assert len(all_trades) == 1
    assert {candidate.decision for candidate in all_candidates} == {"paper_trade", "candidate_only_existing_trade"}
    assert all_trades[0].market_ticker == "KXMLB-DOUBLEHEADER"
    assert all_trades[0].contract_side == "yes"


def test_generate_candidates_refreshes_existing_open_trade_price(monkeypatch) -> None:
    current_time = {"now": datetime(2026, 7, 1, 16, 0, tzinfo=UTC)}
    monkeypatch.setattr(candidates, "utc_now", lambda: current_time["now"])

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="refresh-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 4, 0, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-REFRESH",
            ticker="KXMLB-REFRESH",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 4, 0, 0, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
        )
        session.add_all([game, market])
        session.commit()

        first_result = candidates.generate_candidates(session)
        trade = session.scalar(select(PaperTrade).where(PaperTrade.market_ticker == "KXMLB-REFRESH"))
        assert trade is not None
        assert trade.current_price == Decimal("0.4000")

        market.implied_yes_ask = Decimal("0.3200")
        session.add(market)
        session.commit()
        current_time["now"] = datetime(2026, 7, 2, 16, 0, tzinfo=UTC)
        second_result = candidates.generate_candidates(session)

        refreshed_trade = session.get(PaperTrade, trade.id)
        all_trades = list(session.scalars(select(PaperTrade)))

    assert first_result["paper_trades"] == 1
    assert second_result["paper_trades"] == 0
    assert len(all_trades) == 1
    assert refreshed_trade is not None
    assert refreshed_trade.entry_price == Decimal("0.4000")
    assert refreshed_trade.current_price == Decimal("0.3200")


def test_generate_candidates_blocks_unknown_market_type_from_paper_trading(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="unsupported-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-UNSUPPORTED",
            ticker="KXMLB-UNSUPPORTED",
            title="Aaron Judge special when the New York Yankees play the Boston Red Sox",
            status="open",
            occurrence_datetime=datetime(2026, 7, 1, 23, 10, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
        )
        session.add_all([game, market])
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))
        all_trades = list(session.scalars(select(PaperTrade)))

    assert result["candidates"] == 1
    assert result["paper_trades"] == 0
    assert candidate is not None
    assert candidate.market_type == "unknown"
    assert candidate.decision == "no_trade_unsupported_market_type"
    assert all_trades == []


def test_generate_candidates_preserves_zero_market_price(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="zero-price-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-ZERO-PRICE",
            ticker="KXMLB-ZERO-PRICE",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 1, 23, 10, tzinfo=UTC),
            implied_yes_ask=Decimal("0.0000"),
            yes_ask=Decimal("0.4000"),
        )
        session.add_all([game, market])
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))
        trade = session.scalar(select(PaperTrade))

    assert result["paper_trades"] == 1
    assert candidate is not None
    assert candidate.executable_price == Decimal("0.0000")
    assert candidate.expected_value == Decimal("0.500000")
    assert candidate.decision == "paper_trade"
    assert trade is not None
    assert trade.entry_price == Decimal("0.0000")
    assert trade.current_price == Decimal("0.0000")


def test_generate_candidates_requires_executable_yes_ask(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="no-ask-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-NO-ASK",
            ticker="KXMLB-NO-ASK",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 1, 23, 10, tzinfo=UTC),
            yes_mid=Decimal("0.3000"),
            last_price=Decimal("0.2800"),
            best_yes_bid=Decimal("0.2500"),
        )
        session.add_all([game, market])
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))
        all_trades = list(session.scalars(select(PaperTrade)))

    assert result["paper_trades"] == 0
    assert candidate is not None
    assert candidate.market_price is None
    assert candidate.executable_price is None
    assert candidate.decision == "no_trade_missing_price"
    assert all_trades == []


def test_generate_candidates_blocks_paused_markets(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="paused-market-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-PAUSED",
            ticker="KXMLB-PAUSED",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="paused",
            occurrence_datetime=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
        )
        session.add_all([game, market])
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))
        all_trades = list(session.scalars(select(PaperTrade)))

    assert result["candidates"] == 1
    assert result["paper_trades"] == 0
    assert candidate is not None
    assert candidate.decision == "no_trade_market_closed"
    assert all_trades == []


def test_generate_candidates_blocks_paper_trades_after_game_start(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 23, 15, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="started-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
            status="in_progress",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-STARTED",
            ticker="KXMLB-STARTED",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
        )
        session.add_all([game, market])
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))
        all_trades = list(session.scalars(select(PaperTrade)))

    assert result["candidates"] == 0
    assert result["paper_trades"] == 0
    assert candidate is None
    assert all_trades == []


def test_candidate_summary_preserves_zero_values() -> None:
    candidate = ModelCandidate(
        evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
        model_probability=Decimal("0.000000"),
        probability=Decimal("0.500000"),
        executable_price=Decimal("0.0000"),
        market_price=Decimal("0.4000"),
        decision="paper_trade",
    )

    summary = _candidate_summary(candidate, None, None)

    assert summary.model_probability == 0.0
    assert summary.executable_price == 0.0


def test_mapping_confidence_and_rationale() -> None:
    game = MlbGame(
        external_game_id="1",
        home_team="New York Yankees",
        away_team="Boston Red Sox",
        scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
        status="scheduled",
    )
    market = KalshiMarket(
        kalshi_market_id="KX-1",
        ticker="KXMLB-YANKEES-RED-SOX",
        title="Will the New York Yankees win the game against the Boston Red Sox?",
        status="open",
        occurrence_datetime=datetime(2026, 7, 1, 23, 10, tzinfo=UTC),
    )

    confidence, status, metadata = score_mapping(game, market)

    assert confidence >= 0
    assert status == "candidate"
    assert metadata["market_type"] == "full_game_moneyline"
    assert metadata["matched_team_count"] == 2
    assert metadata["date_proximity_matched"] is True
    assert infer_market_type("first five total runs") == "first_five_total"


def test_mapping_requires_date_proximity_for_candidate_status() -> None:
    game = MlbGame(
        external_game_id="date-match-1",
        home_team="New York Yankees",
        away_team="Boston Red Sox",
        scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
        status="scheduled",
    )
    missing_date_market = KalshiMarket(
        kalshi_market_id="KX-MISSING-DATE",
        ticker="KXMLB-YANKEES-RED-SOX-MISSING-DATE",
        title="Will the New York Yankees win the game against the Boston Red Sox?",
        status="open",
    )
    far_date_market = KalshiMarket(
        kalshi_market_id="KX-FAR-DATE",
        ticker="KXMLB-YANKEES-RED-SOX-FAR-DATE",
        title="Will the New York Yankees win the game against the Boston Red Sox?",
        status="open",
        occurrence_datetime=datetime(2026, 7, 8, 23, 5, tzinfo=UTC),
    )

    missing_confidence, missing_status, missing_metadata = score_mapping(game, missing_date_market)
    far_confidence, far_status, far_metadata = score_mapping(game, far_date_market)

    assert missing_confidence == Decimal("0.7500")
    assert missing_status == "rejected"
    assert missing_metadata["matched_team_count"] == 2
    assert missing_metadata["date_proximity_matched"] is False
    assert "DATE_PROXIMITY_MISSING" in missing_metadata["reasons"]
    assert far_confidence == Decimal("0.7500")
    assert far_status == "rejected"
    assert far_metadata["matched_team_count"] == 2
    assert far_metadata["date_proximity_matched"] is False
    assert "DATE_PROXIMITY_MISMATCH" in far_metadata["reasons"]


def test_sync_market_mappings_ignores_same_teams_on_wrong_date() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        old_game = MlbGame(
            external_game_id="same-teams-old",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
            status="final",
        )
        current_game = MlbGame(
            external_game_id="same-teams-current",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 8, 23, 5, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-SAME-TEAMS-CURRENT",
            ticker="KXMLB-YANKEES-RED-SOX-CURRENT",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 8, 23, 10, tzinfo=UTC),
        )
        session.add_all([old_game, current_game, market])
        session.commit()

        sync_market_mappings(session)
        mappings = session.execute(select(MarketMapping, MlbGame).join(MlbGame)).all()

    status_by_game = {game.external_game_id: mapping.mapping_status for mapping, game in mappings}
    assert status_by_game == {"same-teams-current": "candidate"}


def test_sync_market_mappings_marks_non_nearest_doubleheader_for_review() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        early_game = MlbGame(
            external_game_id="doubleheader-early",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 1, 18, 0, tzinfo=UTC),
            status="scheduled",
        )
        late_game = MlbGame(
            external_game_id="doubleheader-late",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 1, 20, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-DOUBLEHEADER-LATE",
            ticker="KXMLB-DOUBLEHEADER-LATE",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 1, 20, 5, tzinfo=UTC),
        )
        session.add_all([early_game, late_game, market])
        session.commit()

        sync_market_mappings(session)
        mappings = session.execute(select(MarketMapping, MlbGame).join(MlbGame)).all()

    status_by_game = {game.external_game_id: mapping.mapping_status for mapping, game in mappings}
    metadata_by_game = {game.external_game_id: mapping.mapping_metadata for mapping, game in mappings}
    assert status_by_game == {"doubleheader-early": "needs_review", "doubleheader-late": "candidate"}
    assert "NON_NEAREST_SAME_TEAM_GAME" in metadata_by_game["doubleheader-early"]["reasons"]


def test_mapping_requires_unambiguous_team_matches() -> None:
    game = MlbGame(
        external_game_id="2",
        home_team="New York Yankees",
        away_team="Boston Red Sox",
        scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
        status="scheduled",
    )
    market = KalshiMarket(
        kalshi_market_id="KX-2",
        ticker="KXMLB-METS-REDS",
        title="Will the New York Mets win the game against the Cincinnati Reds?",
        status="open",
        occurrence_datetime=datetime(2026, 7, 1, 23, 10, tzinfo=UTC),
    )

    confidence, status, metadata = score_mapping(game, market)

    assert confidence == Decimal("0.4000")
    assert status == "needs_review"
    assert metadata["matched_team_count"] == 0
    assert not any(str(reason).startswith("TEAM_MATCH") for reason in metadata["reasons"])


def test_sync_market_mappings_updates_existing_rows_to_rejected() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="stale-map-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-STALE-MAP",
            ticker="KXMLB-STALE-MAP",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
        )
        session.add_all([game, market])
        session.commit()

        sync_market_mappings(session)
        mapping = session.scalar(select(MarketMapping))
        assert mapping is not None
        assert mapping.mapping_status == "candidate"

        market.title = "Will the Los Angeles Dodgers win the game against the San Francisco Giants?"
        market.occurrence_datetime = None
        session.add(market)
        session.commit()

        sync_market_mappings(session)
        updated_mapping = session.get(MarketMapping, mapping.id)

    assert updated_mapping is not None
    assert updated_mapping.mapping_status == "rejected"
    assert updated_mapping.confidence == Decimal("0.1500")
    assert updated_mapping.mapping_metadata["matched_team_count"] == 0


def test_internal_sync_endpoints_require_api_key_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("BACKEND_API_KEY", "secret-test-key")
    get_settings.cache_clear()

    try:
        response = client.post("/v1/run/paper-candidate-engine")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 401


def test_internal_sync_endpoints_fail_closed_in_production_without_api_key(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("BACKEND_API_KEY", raising=False)
    get_settings.cache_clear()

    try:
        response = client.post("/v1/run/paper-candidate-engine")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 401
    assert "BACKEND_API_KEY" in response.json()["detail"]


def test_internal_api_key_fails_closed_without_explicit_local_env(monkeypatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("BACKEND_API_KEY", raising=False)
    get_settings.cache_clear()

    try:
        with pytest.raises(HTTPException) as exc_info:
            require_internal_api_key(x_api_key=None)
    finally:
        get_settings.cache_clear()

    assert exc_info.value.status_code == 401
    assert "APP_ENV explicitly enables local development" in exc_info.value.detail


def test_internal_api_key_allows_explicit_local_without_api_key(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.delenv("BACKEND_API_KEY", raising=False)
    get_settings.cache_clear()

    try:
        assert require_internal_api_key(x_api_key=None) is None
    finally:
        get_settings.cache_clear()


def test_market_sync_uses_valid_filters_and_clears_stale_orderbook(monkeypatch) -> None:
    class FakeKalshiClient:
        def __init__(self) -> None:
            self.params: dict[str, object] | None = None

        def iter_markets(self, params: dict[str, object], max_pages: int):
            self.params = params
            assert max_pages == 1
            yield {
                "id": "market-open",
                "ticker": "KXMLB-OPEN",
                "title": "MLB Yankees baseball market",
                "status": "open",
                "yes_ask": 46,
                "occurrence_datetime": "2026-07-01T23:00:00Z",
                "expected_expiration_time": "2026-08-15T23:00:00Z",
                "close_time": "2026-09-01T23:00:00Z",
            }
            yield {
                "id": "market-future",
                "ticker": "KXMLB-FUTURE",
                "title": "MLB future baseball market",
                "status": "open",
                "yes_ask": 45,
                "expected_expiration_time": "2026-08-15T23:00:00Z",
                "close_time": "2026-09-01T23:00:00Z",
            }
            yield {
                "id": "market-closed",
                "ticker": "KXMLB-CLOSED",
                "title": "MLB closed baseball market",
                "status": "closed",
                "yes_ask": 48,
                "close_time": "2026-07-01T23:00:00Z",
            }

        def get_orderbook(self, ticker: str):
            raise RuntimeError(f"orderbook unavailable for {ticker}")

    fake_client = FakeKalshiClient()
    monkeypatch.setattr(market_sync.KalshiClient, "from_settings", staticmethod(lambda: fake_client))
    monkeypatch.setattr(market_sync, "utc_now", lambda: datetime(2026, 6, 30, 12, 0, tzinfo=UTC))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            KalshiMarket(
                kalshi_market_id="market-open",
                ticker="KXMLB-OPEN",
                title="Old MLB market",
                status="open",
                best_yes_bid=Decimal("0.4200"),
                best_no_bid=Decimal("0.5700"),
                implied_yes_ask=Decimal("0.4300"),
                implied_no_ask=Decimal("0.5800"),
            )
        )
        session.commit()

        count = market_sync.sync_kalshi_markets(session, max_pages=1)

        assert count == 1
        assert fake_client.params is not None
        assert "status" not in fake_client.params
        assert "min_close_ts" not in fake_client.params
        assert "max_close_ts" not in fake_client.params

        open_market = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == "KXMLB-OPEN"))
        future_market = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == "KXMLB-FUTURE"))
        closed_market = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == "KXMLB-CLOSED"))

        assert future_market is None
        assert closed_market is None
        assert open_market is not None
        assert open_market.occurrence_datetime == datetime(2026, 7, 1, 23, 0)
        assert open_market.close_time == datetime(2026, 9, 1, 23, 0)
        assert open_market.best_yes_bid is None
        assert open_market.best_no_bid is None
        assert open_market.implied_yes_ask is None
        assert open_market.implied_no_ask is None
        assert open_market.yes_ask == Decimal("0.4600")
        assert open_market.orderbook_raw == {"error": "orderbook unavailable for KXMLB-OPEN"}


def test_market_sync_reads_kalshi_dollar_price_fields(monkeypatch) -> None:
    class FakeKalshiClient:
        def __init__(self) -> None:
            self.max_pages: int | None | object = object()

        def iter_markets(self, params: dict[str, object], max_pages: int | None):
            self.max_pages = max_pages
            yield {
                "id": "market-dollar-fields",
                "ticker": "KXMLB-DOLLARS",
                "title": "MLB Yankees baseball market",
                "status": "open",
                "yes_bid_dollars": "0.1200",
                "yes_ask_dollars": "0.3400",
                "no_bid_dollars": "0.6500",
                "no_ask_dollars": "0.8700",
                "last_price_dollars": "0.2500",
                "close_time": "2026-07-01T23:00:00Z",
            }

        def get_orderbook(self, ticker: str):
            raise AssertionError("orderbook fetch is disabled in this test")

    fake_client = FakeKalshiClient()
    monkeypatch.setattr(market_sync.KalshiClient, "from_settings", staticmethod(lambda: fake_client))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        count = market_sync.sync_kalshi_markets(session, fetch_orderbooks=False)
        row = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == "KXMLB-DOLLARS"))

        assert count == 1
        assert fake_client.max_pages is None
        assert row is not None
        assert row.yes_bid == Decimal("0.1200")
        assert row.yes_ask == Decimal("0.3400")
        assert row.yes_mid == Decimal("0.2300")
        assert row.no_bid == Decimal("0.6500")
        assert row.no_ask == Decimal("0.8700")
        assert row.no_mid == Decimal("0.7600")
        assert row.last_price == Decimal("0.2500")


def test_market_sync_reads_legacy_cent_price_fields(monkeypatch) -> None:
    class FakeKalshiClient:
        def iter_markets(self, params: dict[str, object], max_pages: int | None):
            yield {
                "id": "market-legacy-cents",
                "ticker": "KXMLB-CENTS",
                "title": "MLB Yankees baseball market",
                "status": "open",
                "yes_bid": 1,
                "yes_ask": 2,
                "no_bid": 99,
                "no_ask": 98,
                "last_price": 1,
                "close_time": "2026-07-01T23:00:00Z",
            }

        def get_orderbook(self, ticker: str):
            raise AssertionError("orderbook fetch is disabled in this test")

    monkeypatch.setattr(market_sync.KalshiClient, "from_settings", staticmethod(lambda: FakeKalshiClient()))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        count = market_sync.sync_kalshi_markets(session, fetch_orderbooks=False)
        row = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == "KXMLB-CENTS"))

        assert count == 1
        assert row is not None
        assert row.yes_bid == Decimal("0.0100")
        assert row.yes_ask == Decimal("0.0200")
        assert row.yes_mid == Decimal("0.0150")
        assert row.no_bid == Decimal("0.9900")
        assert row.no_ask == Decimal("0.9800")
        assert row.no_mid == Decimal("0.9850")
        assert row.last_price == Decimal("0.0100")


def test_market_sync_updates_existing_closed_market(monkeypatch) -> None:
    class FakeKalshiClient:
        def iter_markets(self, params: dict[str, object], max_pages: int):
            yield {
                "id": "market-closing",
                "ticker": "KXMLB-CLOSING",
                "title": "MLB Yankees baseball market",
                "status": "closed",
                "yes_ask": 12,
                "close_time": "2026-07-01T23:00:00Z",
            }

        def get_orderbook(self, ticker: str):
            raise AssertionError("closed markets should not fetch orderbooks")

    monkeypatch.setattr(market_sync.KalshiClient, "from_settings", staticmethod(lambda: FakeKalshiClient()))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            KalshiMarket(
                kalshi_market_id="market-closing",
                ticker="KXMLB-CLOSING",
                title="Old MLB market",
                status="open",
                yes_ask=Decimal("0.4400"),
                yes_mid=Decimal("0.4300"),
                best_yes_bid=Decimal("0.4200"),
                best_no_bid=Decimal("0.5700"),
                implied_yes_ask=Decimal("0.4300"),
                implied_no_ask=Decimal("0.5800"),
            )
        )
        session.commit()

        count = market_sync.sync_kalshi_markets(session, max_pages=1)
        row = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == "KXMLB-CLOSING"))

        assert count == 1
        assert row is not None
        assert row.status == "closed"
        assert row.yes_ask == Decimal("0.1200")
        assert row.yes_mid is None
        assert row.best_yes_bid is None
        assert row.best_no_bid is None
        assert row.implied_yes_ask is None
        assert row.implied_no_ask is None
        assert row.orderbook_raw == {"skipped": "market status closed"}


def test_market_sync_reads_fixed_point_orderbook_wrapper(monkeypatch) -> None:
    class FakeKalshiClient:
        def iter_markets(self, params: dict[str, object], max_pages: int):
            yield {
                "id": "market-fp",
                "ticker": "KXMLB-FP",
                "title": "MLB Yankees baseball market",
                "status": "open",
                "close_time": "2026-07-01T23:00:00Z",
            }

        def get_orderbook(self, ticker: str):
            return {
                "orderbook_fp": {
                    "yes_dollars": [["0.0100", "200.00"], ["0.4200", "13.00"]],
                    "no_dollars": [["0.2500", "50.00"], ["0.5600", "17.00"]],
                }
            }

    monkeypatch.setattr(market_sync.KalshiClient, "from_settings", staticmethod(lambda: FakeKalshiClient()))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        count = market_sync.sync_kalshi_markets(session, max_pages=1)
        row = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == "KXMLB-FP"))

        assert count == 1
        assert row is not None
        assert row.best_yes_bid == Decimal("0.4200")
        assert row.best_no_bid == Decimal("0.5600")
        assert row.implied_yes_ask == Decimal("0.4400")
        assert row.implied_no_ask == Decimal("0.5800")


def test_list_today_markets_uses_occurrence_or_mapped_game_time(monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "today_eastern", lambda: datetime(2026, 7, 1, tzinfo=UTC).date())

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        occurrence_today_market = KalshiMarket(
            kalshi_market_id="KX-OCCURRENCE-TODAY",
            ticker="KXMLB-OCCURRENCE-TODAY",
            title="Occurrence today market",
            status="open",
            occurrence_datetime=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            close_time=datetime(2026, 8, 1, 23, 0, tzinfo=UTC),
        )
        mapped_game = MlbGame(
            external_game_id="market-list-game",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 1, 22, 0, tzinfo=UTC),
            status="scheduled",
        )
        mapped_today_market = KalshiMarket(
            kalshi_market_id="KX-MAPPED-TODAY",
            ticker="KXMLB-MAPPED-TODAY",
            title="Mapped today market",
            status="open",
            close_time=datetime(2026, 8, 1, 23, 0, tzinfo=UTC),
        )
        tomorrow_market = KalshiMarket(
            kalshi_market_id="KX-OCCURRENCE-TOMORROW",
            ticker="KXMLB-OCCURRENCE-TOMORROW",
            title="Occurrence tomorrow market",
            status="open",
            occurrence_datetime=datetime(2026, 7, 2, 23, 0, tzinfo=UTC),
            close_time=datetime(2026, 8, 1, 23, 0, tzinfo=UTC),
        )
        close_today_only_market = KalshiMarket(
            kalshi_market_id="KX-CLOSE-TODAY",
            ticker="KXMLB-CLOSE-TODAY",
            title="Close today market",
            status="open",
            close_time=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        )
        session.add_all([occurrence_today_market, mapped_game, mapped_today_market, tomorrow_market, close_today_only_market])
        session.flush()
        session.add(
            MarketMapping(
                mlb_game_id=mapped_game.id,
                kalshi_market_id=mapped_today_market.id,
                mapping_status="candidate",
                confidence=Decimal("0.9500"),
            )
        )
        session.commit()

        rows = dashboard.list_today_markets(session)

    assert {market.ticker for market, _ in rows} == {"KXMLB-OCCURRENCE-TODAY", "KXMLB-MAPPED-TODAY"}


def test_dashboard_uses_newest_portfolio_snapshots() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured_at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

    with Session(engine) as session:
        for index in range(501):
            value = Decimal(index)
            session.add(
                BalanceSnapshot(
                    captured_at=captured_at + timedelta(minutes=index),
                    cash_balance=value,
                    portfolio_value=value,
                    source="paper",
                )
            )
        session.commit()

        summary = dashboard.dashboard_summary_from_db(session)

    assert len(summary.portfolio_series) == 500
    assert summary.portfolio_series[0].value == 1.0
    assert summary.portfolio_series[-1].value == 500.0
    assert summary.cash_balance == 500.0
    assert summary.portfolio_value == 500.0


def test_dashboard_preserves_zero_current_prices() -> None:
    opened_at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    trade = PaperTrade(
        market_ticker="KXMLB-ZERO",
        contract_side="yes",
        entry_price=Decimal("0.5000"),
        current_price=Decimal("0.0000"),
        quantity=2,
        entry_time=opened_at,
        status="open",
    )
    position = Position(
        market_ticker="KXMLB-ZERO",
        contract_side="yes",
        entry_price=Decimal("0.5000"),
        current_price=Decimal("0.0000"),
        quantity=2,
        opened_at=opened_at,
        status="open",
    )

    trade_summary = dashboard._position_from_trade(trade)
    position_summary = dashboard._position_from_position(position)

    assert trade_summary.current_price == 0.0
    assert trade_summary.profit_loss == -1.0
    assert trade_summary.profit_loss_percent == -1.0
    assert position_summary.current_price == 0.0
    assert position_summary.profit_loss == -1.0
    assert position_summary.profit_loss_percent == -1.0

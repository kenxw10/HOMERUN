from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app import main as main_module
from app.config import get_settings
from app.database import Base, database_status
from app.main import _candidate_summary, app
from app.models import (
    BalanceSnapshot,
    CalibrationRun,
    FeatureSnapshot,
    KalshiMarket,
    MarketMapping,
    MlbGame,
    ModelCandidate,
    ModelVersion,
    PaperTrade,
    Position,
    Settlement,
    TrainingRun,
)
from app.security import require_internal_api_key
from app.services import candidates, dashboard, market_sync
from app.services.contracts import selected_team_from_ticker
from app.services.http_json import HttpJsonError
from app.services.kalshi import KalshiAPIError, KalshiClient, derive_orderbook_prices
from app.services.kalshi_mlb_resolver import (
    build_event_ticker_candidates,
    build_market_ticker_candidates,
    normalize_team_abbreviation,
    resolve_game_markets,
    validate_market_for_game,
)
from app.services.mapping import infer_market_type, score_mapping, sync_market_mappings
from app.services.modeling import run_model_governance
from app.services.settlement import settle_paper_trades
from app.time_utils import classify_time_bucket, eastern_display

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


def test_generate_candidates_uses_contract_subtitles_for_market_type(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="subtitle-type-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-SUBTITLE-TYPE",
            ticker="KXMLB-SUBTITLE-TYPE",
            title="New York Yankees vs Boston Red Sox",
            yes_subtitle="New York Yankees win the game",
            no_subtitle="Boston Red Sox win the game",
            status="open",
            occurrence_datetime=datetime(2026, 7, 1, 23, 10, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
        )
        session.add_all([game, market])
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))
        trade = session.scalar(select(PaperTrade))

    assert result["candidates"] == 1
    assert result["paper_trades"] == 1
    assert candidate is not None
    assert candidate.market_type == "full_game_winner"
    assert candidate.decision == "paper_trade"
    assert trade is not None


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
    assert candidate.expected_value == candidate.model_probability
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


def test_generate_candidates_skips_non_playable_future_games(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="postponed-game-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
            status="postponed",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-POSTPONED",
            ticker="KXMLB-POSTPONED",
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


def test_generate_candidates_handles_no_mappings_cleanly(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="no-mapping-1",
                home_team="New York Yankees",
                away_team="Boston Red Sox",
                scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
                status="scheduled",
            )
        )
        session.commit()

        result = candidates.generate_candidates(session)
        all_candidates = list(session.scalars(select(ModelCandidate)))
        all_trades = list(session.scalars(select(PaperTrade)))

    assert result["candidates"] == 0
    assert result["paper_trades"] == 0
    assert all_candidates == []
    assert all_trades == []


def test_generate_candidates_preserves_confirmed_targeted_resolver_mapping(monkeypatch) -> None:
    now = datetime(2026, 6, 25, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="resolver-confirmed-1",
            home_team="San Diego Padres",
            away_team="Los Angeles Dodgers",
            home_abbreviation="SD",
            away_abbreviation="LAD",
            scheduled_start=datetime(2026, 6, 26, 22, 40, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-RESOLVER-CONFIRMED",
            ticker="KXMLBGAME-26JUN261840LADSD-LAD",
            event_ticker="KXMLBGAME-26JUN261840LADSD",
            title="Los Angeles D vs San Diego",
            yes_subtitle="Los Angeles D win the game",
            status="open",
            occurrence_datetime=datetime(2026, 6, 26, 22, 40, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
        )
        session.add_all([game, market])
        session.flush()
        session.add(
            MarketMapping(
                mlb_game_id=game.id,
                kalshi_market_id=market.id,
                mapping_status="confirmed",
                confidence=Decimal("0.9500"),
                rationale="MARKET_TICKER_MATCH",
                resolver_strategy="exact_market_tickers",
                validation_status="strong",
                mapping_metadata={"resolver_strategy_used": "exact_market_tickers"},
            )
        )
        session.commit()

        result = candidates.generate_candidates(session)
        mapping = session.scalar(select(MarketMapping))
        candidate = session.scalar(select(ModelCandidate))
        trade = session.scalar(select(PaperTrade))

    assert result["paper_trades"] == 1
    assert mapping is not None
    assert mapping.mapping_status == "confirmed"
    assert mapping.confidence == Decimal("0.9500")
    assert mapping.resolver_strategy == "exact_market_tickers"
    assert candidate is not None
    assert candidate.decision == "paper_trade"
    assert trade is not None


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


def test_team_abbreviation_normalization() -> None:
    assert normalize_team_abbreviation("Kansas City Royals") == "KC"
    assert normalize_team_abbreviation("Chicago White Sox") == "CWS"
    assert normalize_team_abbreviation("Arizona Diamondbacks") == "ARI"
    assert normalize_team_abbreviation("Athletics") == "ATH"
    assert normalize_team_abbreviation("Unknown Team", "UTM") == "UTM"


def test_candidate_event_ticker_generation_uses_observed_format() -> None:
    game = MlbGame(
        external_game_id="ticker-format-1",
        home_team="Detroit Tigers",
        away_team="Houston Astros",
        home_abbreviation="DET",
        away_abbreviation="HOU",
        scheduled_start=datetime(2026, 6, 26, 22, 40, tzinfo=UTC),
        status="scheduled",
    )

    events = build_event_ticker_candidates(game)
    markets = build_market_ticker_candidates(game)

    assert events[0] == "KXMLBGAME-26JUN261840HOUDET"
    assert "KXMLBGAME-26JUN261840HOUDET-HOU" in markets
    assert "KXMLBGAME-26JUN261840HOUDET-DET" in markets


def test_candidate_event_ticker_generation_uses_fixed_eastern_timezone(monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_TIMEZONE", "UTC")
    get_settings.cache_clear()

    try:
        game = MlbGame(
            external_game_id="ticker-fixed-zone-1",
            home_team="Detroit Tigers",
            away_team="Houston Astros",
            home_abbreviation="DET",
            away_abbreviation="HOU",
            scheduled_start=datetime(2026, 6, 26, 22, 40, tzinfo=UTC),
            status="scheduled",
        )

        events = build_event_ticker_candidates(game)
    finally:
        get_settings.cache_clear()

    assert events[0] == "KXMLBGAME-26JUN261840HOUDET"


def test_market_sync_uses_targeted_resolver_and_skips_broad_by_default(monkeypatch) -> None:
    class FakeKalshiClient:
        def __init__(self) -> None:
            self.ticker_requests: list[list[str]] = []
            self.broad_calls = 0

        def get_markets_by_tickers(self, tickers: list[str]):
            self.ticker_requests.append(tickers)
            event = "KXMLBGAME-26JUN261840HOUDET"
            return {
                "markets": [
                    {
                        "id": "market-targeted",
                        "ticker": f"{event}-HOU",
                        "event_ticker": event,
                        "title": "Will the Houston Astros win the game against the Detroit Tigers?",
                        "status": "active",
                        "yes_bid_dollars": "0.1200",
                        "yes_ask_dollars": "0.3400",
                        "no_bid_dollars": "0.6500",
                        "no_ask_dollars": "0.8700",
                        "last_price_dollars": "0.2500",
                        "occurrence_datetime": "2026-06-26T22:40:00Z",
                    }
                ]
            }

        def get_event(self, event_ticker: str):
            raise AssertionError("exact ticker resolver should stop after match")

        def get_markets_by_event_ticker(self, event_ticker: str, limit: int = 100):
            raise AssertionError("exact ticker resolver should stop after match")

        def get_markets_by_series_window(self, *args, **kwargs):
            raise AssertionError("series fallback should not run after exact match")

        def iter_markets(self, params: dict[str, object], max_pages: int | None):
            self.broad_calls += 1
            raise AssertionError("broad discovery should be disabled by default")

        def get_orderbook(self, ticker: str):
            return {
                "orderbook_fp": {
                    "yes_dollars": [["0.0100", "200.00"], ["0.4200", "13.00"]],
                    "no_dollars": [["0.2500", "50.00"], ["0.5600", "17.00"]],
                }
            }

    fake_client = FakeKalshiClient()
    monkeypatch.setattr(market_sync.KalshiClient, "from_settings", staticmethod(lambda: fake_client))
    monkeypatch.setattr(market_sync, "utc_now", lambda: datetime(2026, 6, 25, 12, 0, tzinfo=UTC))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="targeted-1",
            home_team="Detroit Tigers",
            away_team="Houston Astros",
            home_abbreviation="DET",
            away_abbreviation="HOU",
            scheduled_start=datetime(2026, 6, 26, 22, 40, tzinfo=UTC),
            status="scheduled",
        )
        session.add(game)
        session.commit()

        summary = market_sync.sync_kalshi_markets(session)
        row = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == "KXMLBGAME-26JUN261840HOUDET-HOU"))
        mapping = session.scalar(select(MarketMapping))

    assert summary["games_considered"] == 1
    assert summary["markets_upserted"] == 1
    assert summary["confirmed_mappings"] == 1
    assert fake_client.broad_calls == 0
    assert row is not None
    assert row.status == "open"
    assert row.raw_status == "active"
    assert row.yes_bid == Decimal("0.1200")
    assert row.yes_ask == Decimal("0.3400")
    assert row.best_yes_bid == Decimal("0.4200")
    assert row.implied_yes_ask == Decimal("0.4400")
    assert mapping is not None
    assert mapping.mapping_status == "confirmed"
    assert mapping.resolver_strategy == "exact_market_tickers"
    assert mapping.mapping_metadata["attempted_event_tickers"][0] == "KXMLBGAME-26JUN261840HOUDET"


def test_series_window_fallback_uses_mve_filter_exclude() -> None:
    client = KalshiClient(base_url="https://example.test")
    calls: list[tuple[dict[str, object], int | None]] = []

    def fake_iter_markets(params: dict[str, object], max_pages: int | None = None):
        calls.append((dict(params), max_pages))
        return iter([])

    client.iter_markets = fake_iter_markets  # type: ignore[method-assign]

    markets = client.get_markets_by_series_window("KXMLBGAME", 1, 2, limit=50, max_pages=2)

    assert markets == []
    assert calls == [
        (
            {
                "series_ticker": "KXMLBGAME",
                "min_close_ts": 1,
                "max_close_ts": 2,
                "limit": 50,
                "mve_filter": "exclude",
            },
            2,
        )
    ]


def test_resolver_series_fallback_close_window_covers_mlb_expiration() -> None:
    class FakeKalshiClient:
        def __init__(self) -> None:
            self.series_call: tuple[str, int, int, int, int] | None = None

        def get_markets_by_tickers(self, tickers: list[str]):
            return {"markets": []}

        def get_event(self, event_ticker: str):
            return {"event": {"markets": []}}

        def get_markets_by_event_ticker(self, event_ticker: str, limit: int = 100):
            return {"markets": []}

        def get_markets_by_series_window(
            self,
            series_ticker: str,
            min_close_ts: int,
            max_close_ts: int,
            *,
            limit: int = 100,
            max_pages: int = 2,
        ):
            self.series_call = (series_ticker, min_close_ts, max_close_ts, limit, max_pages)
            return []

    client = FakeKalshiClient()
    scheduled_start = datetime(2026, 6, 26, 22, 40, tzinfo=UTC)
    game = MlbGame(
        external_game_id="series-window-1",
        home_team="Detroit Tigers",
        away_team="Houston Astros",
        home_abbreviation="DET",
        away_abbreviation="HOU",
        scheduled_start=scheduled_start,
        status="scheduled",
    )

    resolve_game_markets(client, game)

    assert client.series_call is not None
    series_ticker, min_close_ts, max_close_ts, limit, max_pages = client.series_call
    assert series_ticker == "KXMLBGAME"
    assert min_close_ts == int((scheduled_start - timedelta(days=1)).timestamp())
    assert max_close_ts == int((scheduled_start + timedelta(days=21)).timestamp())
    assert max_close_ts - min_close_ts == 22 * 24 * 60 * 60
    assert limit == 100
    assert max_pages == 2


def test_market_sync_drops_unrelated_series_fallback_markets(monkeypatch) -> None:
    class FakeKalshiClient:
        def __init__(self) -> None:
            self.orderbook_calls: list[str] = []

        def get_markets_by_tickers(self, tickers: list[str]):
            return {"markets": []}

        def get_event(self, event_ticker: str):
            return {"event": {"markets": []}}

        def get_markets_by_event_ticker(self, event_ticker: str, limit: int = 100):
            return {"markets": []}

        def get_markets_by_series_window(
            self,
            series_ticker: str,
            min_close_ts: int,
            max_close_ts: int,
            *,
            limit: int = 100,
            max_pages: int = 2,
        ):
            return [
                {
                    "id": "unrelated-fallback-market",
                    "ticker": "KXMLBGAME-26JUN261940KCCWS-KC",
                    "event_ticker": "KXMLBGAME-26JUN261940KCCWS",
                    "title": "Kansas City Royals vs Chicago White Sox",
                    "status": "open",
                    "occurrence_datetime": "2026-06-26T23:40:00Z",
                    "yes_ask_dollars": "0.4400",
                }
            ]

        def get_orderbook(self, ticker: str):
            self.orderbook_calls.append(ticker)
            raise AssertionError("unrelated fallback markets should not fetch orderbooks")

    fake_client = FakeKalshiClient()
    monkeypatch.setattr(market_sync.KalshiClient, "from_settings", staticmethod(lambda: fake_client))
    monkeypatch.setattr(market_sync, "utc_now", lambda: datetime(2026, 6, 25, 12, 0, tzinfo=UTC))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="fallback-unrelated-1",
                home_team="Detroit Tigers",
                away_team="Houston Astros",
                home_abbreviation="DET",
                away_abbreviation="HOU",
                scheduled_start=datetime(2026, 6, 26, 22, 40, tzinfo=UTC),
                status="scheduled",
            )
        )
        session.commit()

        summary = market_sync.sync_kalshi_markets(session)
        markets = list(session.scalars(select(KalshiMarket)))
        mappings = list(session.scalars(select(MarketMapping)))

    assert summary["games_considered"] == 1
    assert summary["markets_upserted"] == 0
    assert summary["mappings_created_or_updated"] == 0
    assert markets == []
    assert mappings == []
    assert fake_client.orderbook_calls == []


def test_market_sync_rejects_multivariate_markets(monkeypatch) -> None:
    class FakeKalshiClient:
        def get_markets_by_tickers(self, tickers: list[str]):
            event = "KXMVE-26JUN261840HOUDET"
            return {
                "markets": [
                    {
                        "id": "market-mve",
                        "ticker": "KXMV-MLB-COMBO",
                        "event_ticker": event,
                        "title": "Multivariate MLB combo",
                        "status": "open",
                        "mve_selected_legs": [{"ticker": "KXMLBGAME-26JUN261840HOUDET-HOU"}],
                    }
                ]
            }

        def get_event(self, event_ticker: str):
            return {"event": {"markets": []}}

        def get_markets_by_event_ticker(self, event_ticker: str, limit: int = 100):
            return {"markets": []}

        def get_markets_by_series_window(self, *args, **kwargs):
            return []

        def get_orderbook(self, ticker: str):
            raise AssertionError("multivariate markets should not fetch orderbooks")

    monkeypatch.setattr(market_sync.KalshiClient, "from_settings", staticmethod(lambda: FakeKalshiClient()))
    monkeypatch.setattr(market_sync, "utc_now", lambda: datetime(2026, 6, 25, 12, 0, tzinfo=UTC))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="mve-1",
                home_team="Detroit Tigers",
                away_team="Houston Astros",
                home_abbreviation="DET",
                away_abbreviation="HOU",
                scheduled_start=datetime(2026, 6, 26, 22, 40, tzinfo=UTC),
                status="scheduled",
            )
        )
        session.commit()

        summary = market_sync.sync_kalshi_markets(session)
        mapping = session.scalar(select(MarketMapping))
        row = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == "KXMV-MLB-COMBO"))

    assert summary["rejected_multivariate"] == 1
    assert mapping is not None
    assert mapping.mapping_status == "rejected_multivariate"
    assert mapping.validation_status == "rejected_multivariate"
    assert row is not None
    assert row.orderbook_raw == {"skipped": "multivariate market"}


def test_market_sync_continues_fallbacks_after_rejected_only_exact_result(monkeypatch) -> None:
    class FakeKalshiClient:
        def __init__(self) -> None:
            self.event_calls: list[str] = []

        def get_markets_by_tickers(self, tickers: list[str]):
            return {
                "markets": [
                    {
                        "id": "market-mve",
                        "ticker": "KXMV-MLB-COMBO",
                        "event_ticker": "KXMVE-26JUN261840HOUDET",
                        "title": "Multivariate MLB combo",
                        "status": "open",
                        "mve_selected_legs": [{"ticker": "KXMLBGAME-26JUN261840HOUDET-HOU"}],
                    }
                ]
            }

        def get_event(self, event_ticker: str):
            self.event_calls.append(event_ticker)
            if event_ticker != "KXMLBGAME-26JUN261840HOUDET":
                return {"event": {"markets": []}}
            return {
                "event": {
                    "markets": [
                        {
                            "id": "market-targeted",
                            "ticker": "KXMLBGAME-26JUN261840HOUDET-HOU",
                            "event_ticker": "KXMLBGAME-26JUN261840HOUDET",
                            "title": "Will the Houston Astros win the game against the Detroit Tigers?",
                            "status": "open",
                            "occurrence_datetime": "2026-06-26T22:40:00Z",
                            "yes_ask_dollars": "0.3400",
                        }
                    ]
                }
            }

        def get_markets_by_event_ticker(self, event_ticker: str, limit: int = 100):
            raise AssertionError("event lookup should stop after usable fallback match")

        def get_markets_by_series_window(self, *args, **kwargs):
            raise AssertionError("series fallback should not run after usable fallback match")

        def get_orderbook(self, ticker: str):
            if ticker == "KXMV-MLB-COMBO":
                raise AssertionError("multivariate markets should not fetch orderbooks")
            return {"orderbook_fp": {"yes_dollars": [["0.3400", "10.00"]], "no_dollars": [["0.6500", "10.00"]]}}

    fake_client = FakeKalshiClient()
    monkeypatch.setattr(market_sync.KalshiClient, "from_settings", staticmethod(lambda: fake_client))
    monkeypatch.setattr(market_sync, "utc_now", lambda: datetime(2026, 6, 25, 12, 0, tzinfo=UTC))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="mve-fallback-1",
                home_team="Detroit Tigers",
                away_team="Houston Astros",
                home_abbreviation="DET",
                away_abbreviation="HOU",
                scheduled_start=datetime(2026, 6, 26, 22, 40, tzinfo=UTC),
                status="scheduled",
            )
        )
        session.commit()

        summary = market_sync.sync_kalshi_markets(session)
        mappings = list(session.scalars(select(MarketMapping).order_by(MarketMapping.mapping_status)))

    assert "KXMLBGAME-26JUN261840HOUDET" in fake_client.event_calls
    assert summary["rejected_multivariate"] == 1
    assert summary["confirmed_mappings"] == 1
    assert {mapping.mapping_status for mapping in mappings} == {"confirmed", "rejected_multivariate"}


def test_market_sync_returns_structured_upstream_errors(monkeypatch) -> None:
    class FakeKalshiClient:
        def _error(self):
            return KalshiAPIError(
                "upstream failed",
                source=HttpJsonError(
                    "GET failed",
                    endpoint="https://kalshi.example/markets",
                    params={"tickers": "KXMLBGAME-TEST"},
                    status_code=502,
                    body_preview="bad gateway",
                ),
            )

        def get_markets_by_tickers(self, tickers: list[str]):
            raise self._error()

        def get_event(self, event_ticker: str):
            raise self._error()

        def get_markets_by_event_ticker(self, event_ticker: str, limit: int = 100):
            raise self._error()

        def get_markets_by_series_window(self, *args, **kwargs):
            raise self._error()

    monkeypatch.setattr(market_sync.KalshiClient, "from_settings", staticmethod(lambda: FakeKalshiClient()))
    monkeypatch.setattr(market_sync, "utc_now", lambda: datetime(2026, 6, 25, 12, 0, tzinfo=UTC))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="error-1",
                home_team="Detroit Tigers",
                away_team="Houston Astros",
                home_abbreviation="DET",
                away_abbreviation="HOU",
                scheduled_start=datetime(2026, 6, 26, 22, 40, tzinfo=UTC),
                status="scheduled",
            )
        )
        session.commit()

        summary = market_sync.sync_kalshi_markets(session)

    assert summary["markets_upserted"] == 0
    assert summary["errors"]
    first_error = summary["errors"][0]
    assert first_error["endpoint"] == "https://kalshi.example/markets"
    assert first_error["upstream_status_code"] == 502
    assert first_error["body_preview"] == "bad gateway"
    assert first_error["retry_or_fallback_attempted"] is True


def test_resolve_preview_shape_without_db_writes() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="preview-1",
                home_team="Detroit Tigers",
                away_team="Houston Astros",
                home_abbreviation="DET",
                away_abbreviation="HOU",
                scheduled_start=datetime(2026, 6, 26, 22, 40, tzinfo=UTC),
                status="scheduled",
            )
        )
        session.commit()

        preview = market_sync.resolve_preview_for_date(session, datetime(2026, 6, 26, tzinfo=UTC).date(), query_kalshi=False)
        market_count = len(list(session.scalars(select(KalshiMarket))))
        mapping_count = len(list(session.scalars(select(MarketMapping))))

    assert preview["games_considered"] == 1
    game_preview = preview["games"][0]
    assert game_preview["game_label"] == "Houston Astros @ Detroit Tigers"
    assert game_preview["home_abbreviation"] == "DET"
    assert game_preview["away_abbreviation"] == "HOU"
    assert game_preview["attempted_event_tickers"][0] == "KXMLBGAME-26JUN261840HOUDET"
    assert market_count == 0
    assert mapping_count == 0


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
        duplicate_mapping_game = MlbGame(
            external_game_id="market-list-duplicate-game",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_start=datetime(2026, 7, 1, 20, 0, tzinfo=UTC),
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
        session.add_all(
            [
                occurrence_today_market,
                mapped_game,
                duplicate_mapping_game,
                mapped_today_market,
                tomorrow_market,
                close_today_only_market,
            ]
        )
        session.flush()
        session.add_all(
            [
                MarketMapping(
                    mlb_game_id=mapped_game.id,
                    kalshi_market_id=mapped_today_market.id,
                    mapping_status="candidate",
                    confidence=Decimal("0.9500"),
                ),
                MarketMapping(
                    mlb_game_id=duplicate_mapping_game.id,
                    kalshi_market_id=mapped_today_market.id,
                    mapping_status="needs_review",
                    confidence=Decimal("0.7000"),
                ),
            ]
        )
        session.commit()

        rows = dashboard.list_today_markets(session)

    tickers = [market.ticker for market, _ in rows]
    mapped_row = next((market, mapping) for market, mapping in rows if market.ticker == "KXMLB-MAPPED-TODAY")
    assert tickers.count("KXMLB-MAPPED-TODAY") == 1
    assert set(tickers) == {"KXMLB-OCCURRENCE-TODAY", "KXMLB-MAPPED-TODAY"}
    assert mapped_row[1] is not None
    assert mapped_row[1].mapping_status == "candidate"


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


def test_dashboard_includes_paper_trades_alongside_positions() -> None:
    opened_at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            Position(
                market_ticker="KXMLB-POSITION",
                contract_side="yes",
                entry_price=Decimal("0.5000"),
                current_price=Decimal("0.6000"),
                quantity=1,
                opened_at=opened_at,
                status="open",
            )
        )
        session.add(
            PaperTrade(
                market_ticker="KXMLB-TRADE",
                contract_side="yes",
                entry_price=Decimal("0.4000"),
                current_price=Decimal("0.5000"),
                quantity=1,
                entry_time=opened_at,
                status="open",
            )
        )
        session.add(
            PaperTrade(
                market_ticker="KXMLB-POSITION",
                contract_side="yes",
                entry_price=Decimal("0.5000"),
                current_price=Decimal("0.7000"),
                quantity=1,
                entry_time=opened_at,
                status="open",
            )
        )
        session.commit()

        summary = dashboard.dashboard_summary_from_db(session)

    markets = [position.market for position in summary.positions]
    assert markets == ["KXMLB-POSITION", "KXMLB-TRADE"]


def test_resolver_uses_event_ticker_time_and_team_codes_for_validation() -> None:
    game = MlbGame(
        external_game_id="resolver-pr3-1",
        home_team="Detroit Tigers",
        away_team="Houston Astros",
        home_abbreviation="DET",
        away_abbreviation="HOU",
        scheduled_start=datetime(2026, 6, 26, 22, 40, tzinfo=UTC),
        status="scheduled",
    )
    event_tickers = build_event_ticker_candidates(game)
    market_tickers = build_market_ticker_candidates(game)

    match = validate_market_for_game(
        game,
        {
            "ticker": "KXMLBGAME-26JUN261840HOUDET-HOU",
            "event_ticker": "KXMLBGAME-26JUN261840HOUDET",
            "title": "Houston vs Detroit",
            "expected_expiration_time": "2026-06-27T01:40:00Z",
        },
        event_tickers,
        market_tickers,
        "exact_market_tickers",
    )

    assert match.mapping_status == "confirmed"
    assert match.validation_status == "confirmed_for_paper"
    assert match.metadata["time_delta_minutes"] == 0
    assert match.metadata["team_match_score"] == 1.0
    assert match.metadata["ticker_team_codes_match"] is True


def test_paper_settlement_sync_settles_wins_losses_and_is_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settled_at = datetime(2026, 7, 2, 4, 0, tzinfo=UTC)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="settle-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="Final",
            home_score=5,
            away_score=3,
        )
        win_market = KalshiMarket(
            kalshi_market_id="KX-SETTLE-WIN",
            ticker="KXMLBGAME-26JUL011900SEAPIT-PIT",
            title="Will Pittsburgh win?",
            status="closed",
        )
        loss_market = KalshiMarket(
            kalshi_market_id="KX-SETTLE-LOSS",
            ticker="KXMLBGAME-26JUL011900SEAPIT-SEA",
            title="Will Seattle win?",
            status="closed",
        )
        session.add_all([game, win_market, loss_market])
        session.flush()
        win_mapping = MarketMapping(
            mlb_game_id=game.id,
            kalshi_market_id=win_market.id,
            mapping_status="confirmed",
            confidence=Decimal("0.9700"),
        )
        loss_mapping = MarketMapping(
            mlb_game_id=game.id,
            kalshi_market_id=loss_market.id,
            mapping_status="confirmed",
            confidence=Decimal("0.9700"),
        )
        session.add_all([win_mapping, loss_mapping])
        session.flush()
        win_candidate = ModelCandidate(
            mlb_game_id=game.id,
            kalshi_market_id=win_market.id,
            mapping_id=win_mapping.id,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="paper_trade",
            market_type="full_game_winner",
        )
        loss_candidate = ModelCandidate(
            mlb_game_id=game.id,
            kalshi_market_id=loss_market.id,
            mapping_id=loss_mapping.id,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="paper_trade",
            market_type="full_game_winner",
        )
        no_trade_candidate = ModelCandidate(
            mlb_game_id=game.id,
            kalshi_market_id=win_market.id,
            mapping_id=win_mapping.id,
            evaluated_at=datetime(2026, 7, 1, 16, 5, tzinfo=UTC),
            features={},
            decision="no_trade_edge_too_low",
            market_type="full_game_winner",
            contract_side="yes",
        )
        session.add_all([win_candidate, loss_candidate, no_trade_candidate])
        session.flush()
        session.add_all(
            [
                PaperTrade(
                    candidate_id=win_candidate.id,
                    market_ticker=win_market.ticker,
                    contract_side="yes",
                    entry_price=Decimal("0.4000"),
                    current_price=Decimal("0.4000"),
                    quantity=2,
                    entry_time=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
                    status="open",
                ),
                PaperTrade(
                    candidate_id=loss_candidate.id,
                    market_ticker=loss_market.ticker,
                    contract_side="yes",
                    entry_price=Decimal("0.3000"),
                    current_price=Decimal("0.3000"),
                    quantity=3,
                    entry_time=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
                    status="open",
                ),
            ]
        )
        session.commit()

        result = settle_paper_trades(session, date(2026, 7, 1), now=settled_at)
        second_result = settle_paper_trades(session, date(2026, 7, 1), now=settled_at)
        trades = list(session.scalars(select(PaperTrade).order_by(PaperTrade.market_ticker)))
        no_trade = session.scalar(select(ModelCandidate).where(ModelCandidate.decision == "no_trade_edge_too_low"))
        settlements = list(session.scalars(select(Settlement)))
        snapshots = list(session.scalars(select(BalanceSnapshot)))

    assert selected_team_from_ticker("KXMLBGAME-26JUL011900SEAPIT-PIT") == "PIT"
    assert result["candidate_labels_checked"] == 3
    assert result["candidate_labels_created"] == 3
    assert second_result["candidate_labels_created"] == 0
    assert second_result["candidate_labels_already_set"] == 3
    assert result["settled"] == 2
    assert second_result["settled"] == 0
    assert len(settlements) == 2
    assert len(snapshots) == 2
    assert [trade.outcome for trade in trades] == ["win", "loss"]
    assert [trade.realized_pnl for trade in trades] == [Decimal("1.20"), Decimal("-0.90")]
    assert no_trade is not None
    assert no_trade.outcome == "win"
    assert no_trade.outcome_source == "mlb_results_sync"


def test_paper_settlement_leaves_untrusted_ticker_selection_unresolved() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="settle-invalid-selection-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="Final",
            home_score=5,
            away_score=3,
        )
        market = KalshiMarket(
            kalshi_market_id="KX-INVALID-SELECTION",
            ticker="KXMLB-SUBTITLE-TYPE",
            title="Will Pittsburgh win?",
            status="closed",
        )
        session.add_all([game, market])
        session.flush()
        mapping = MarketMapping(
            mlb_game_id=game.id,
            kalshi_market_id=market.id,
            mapping_status="confirmed",
            confidence=Decimal("0.9700"),
        )
        session.add(mapping)
        session.flush()
        candidate = ModelCandidate(
            mlb_game_id=game.id,
            kalshi_market_id=market.id,
            mapping_id=mapping.id,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="paper_trade",
            market_type="full_game_winner",
            contract_side="yes",
        )
        session.add(candidate)
        session.flush()
        trade = PaperTrade(
            candidate_id=candidate.id,
            market_ticker=market.ticker,
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            entry_time=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            status="open",
        )
        session.add(trade)
        session.commit()

        result = settle_paper_trades(session, date(2026, 7, 1), now=datetime(2026, 7, 2, 4, 0, tzinfo=UTC))
        session.refresh(candidate)
        session.refresh(trade)
        settlement = session.scalar(select(Settlement))

    assert result["candidate_labels_skipped_invalid_selection"] == 1
    assert result["skipped_invalid_selection"] == 1
    assert candidate.outcome is None
    assert trade.status == "open"
    assert trade.outcome is None
    assert settlement is None


def test_paper_settlement_keeps_suspended_games_open() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="settle-suspended-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="Suspended",
            home_score=2,
            away_score=2,
        )
        market = KalshiMarket(
            kalshi_market_id="KX-SUSPENDED",
            ticker="KXMLBGAME-26JUL011900SEAPIT-PIT",
            title="Will Pittsburgh win?",
            status="open",
        )
        session.add_all([game, market])
        session.flush()
        mapping = MarketMapping(
            mlb_game_id=game.id,
            kalshi_market_id=market.id,
            mapping_status="confirmed",
            confidence=Decimal("0.9700"),
        )
        session.add(mapping)
        session.flush()
        candidate = ModelCandidate(
            mlb_game_id=game.id,
            kalshi_market_id=market.id,
            mapping_id=mapping.id,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="paper_trade",
            market_type="full_game_winner",
            contract_side="yes",
        )
        session.add(candidate)
        session.flush()
        trade = PaperTrade(
            candidate_id=candidate.id,
            market_ticker=market.ticker,
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            entry_time=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            status="open",
        )
        session.add(trade)
        session.commit()

        result = settle_paper_trades(session, date(2026, 7, 1), now=datetime(2026, 7, 2, 4, 0, tzinfo=UTC))
        session.refresh(candidate)
        session.refresh(trade)
        settlement = session.scalar(select(Settlement))

    assert result["candidate_labels_skipped_not_final"] == 1
    assert result["skipped_not_final"] == 1
    assert result["voided"] == 0
    assert candidate.outcome is None
    assert trade.status == "open"
    assert trade.outcome is None
    assert settlement is None


def test_dashboard_summary_uses_labels_snapshots_and_settled_performance() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    opened_at = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="dashboard-pr3-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-DASHBOARD-PR3",
            ticker="KXMLBGAME-26JUL011900SEAPIT-PIT",
            title="Will Pittsburgh win?",
            status="open",
        )
        session.add_all([game, market])
        session.flush()
        mapping = MarketMapping(
            mlb_game_id=game.id,
            kalshi_market_id=market.id,
            mapping_status="confirmed",
            confidence=Decimal("0.9700"),
        )
        session.add(mapping)
        session.flush()
        candidate = ModelCandidate(
            mlb_game_id=game.id,
            kalshi_market_id=market.id,
            mapping_id=mapping.id,
            evaluated_at=opened_at,
            features={},
            decision="paper_trade",
            market_type="full_game_winner",
        )
        session.add(candidate)
        session.flush()
        session.add_all(
            [
                PaperTrade(
                    candidate_id=candidate.id,
                    market_ticker=market.ticker,
                    contract_side="yes",
                    entry_price=Decimal("0.4000"),
                    current_price=Decimal("0.5500"),
                    quantity=1,
                    entry_time=opened_at,
                    status="open",
                    contract_display="FULL GAME WINNER · SEA @ PIT · PIT",
                    market_display="FULL GAME WINNER · SEA @ PIT · PIT",
                    selection_display="PIT",
                    matchup_display="SEA @ PIT",
                ),
                PaperTrade(
                    market_ticker="KXMLBGAME-SETTLED-PIT",
                    contract_side="yes",
                    entry_price=Decimal("0.4000"),
                    current_price=Decimal("1.0000"),
                    quantity=1,
                    entry_time=opened_at,
                    status="settled",
                    outcome="win",
                    realized_pnl=Decimal("0.60"),
                    resolution="WIN",
                ),
                BalanceSnapshot(
                    captured_at=opened_at,
                    cash_balance=Decimal("999.60"),
                    portfolio_value=Decimal("1000.15"),
                    source="paper",
                    snapshot_type="test",
                ),
            ]
        )
        session.commit()

        summary = dashboard.dashboard_summary_from_db(session)

    assert summary.cash_balance == 999.6
    assert summary.portfolio_value == 1000.15
    assert summary.performance.record == "1-0-0"
    assert summary.performance.profit_loss == 0.6
    assert summary.positions[0].market == "FULL GAME WINNER · SEA @ PIT · PIT"
    assert summary.positions[0].market_ticker == "KXMLBGAME-26JUL011900SEAPIT-PIT"
    assert summary.positions[0].time_entered_display is not None
    assert "EDT" in summary.positions[0].time_entered_display


def test_generate_candidates_uses_heuristic_probability_and_feature_snapshot(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="candidate-pr3-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-CANDIDATE-PR3",
            ticker="KXMLBGAME-26JUL011900SEAPIT-PIT",
            title="Will the Pittsburgh Pirates win the game against the Seattle Mariners?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
        )
        session.add_all([game, market])
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))
        feature_snapshot = session.scalar(select(FeatureSnapshot))

    assert result["model_version"] == "heuristic_full_game_winner_v1"
    assert candidate is not None
    assert candidate.model_probability != Decimal("0.500000")
    assert candidate.model_version_tag == "heuristic_full_game_winner_v1"
    assert candidate.market_type == "full_game_winner"
    assert candidate.contract_display == "FULL GAME WINNER · SEA @ PIT · PIT"
    assert candidate.features["weather"]["source_status"] == "missing"
    assert feature_snapshot is not None
    assert feature_snapshot.features["data_quality"] > 0


def test_model_governance_skips_training_and_records_runs_when_samples_are_too_small() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        result = run_model_governance(session, now=datetime(2026, 7, 1, 12, 0, tzinfo=UTC))
        training = session.scalar(select(TrainingRun))
        calibration = session.scalar(select(CalibrationRun))

    assert result["status"] == "skipped"
    assert result["resolved_samples"] == 0
    assert training is not None
    assert calibration is not None
    assert training.status == "skipped"
    assert calibration.status == "skipped"
    assert "INSUFFICIENT_RESOLVED_SAMPLES" in training.metrics["reason"]


def test_model_governance_keeps_only_one_active_champion() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        old_version = ModelVersion(
            version_tag="legacy_champion",
            description="Legacy active model",
            is_active=True,
            role="champion",
            model_family="full_game_winner",
        )
        session.add(old_version)
        session.commit()

        result = run_model_governance(session, now=datetime(2026, 7, 1, 12, 0, tzinfo=UTC))
        versions = list(session.scalars(select(ModelVersion).order_by(ModelVersion.version_tag)))

    active_versions = [version for version in versions if version.is_active]
    assert result["active_model_version"] == "heuristic_full_game_winner_v1"
    assert [version.version_tag for version in active_versions] == ["heuristic_full_game_winner_v1"]
    assert next(version for version in versions if version.version_tag == "legacy_champion").role == "inactive"


def test_resolve_preview_endpoint_returns_ok_with_partial_warnings(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    monkeypatch.setattr(
        main_module,
        "database_status",
        lambda: {"ready": True, "configured": True, "dialect": "sqlite", "message": "ok"},
    )
    monkeypatch.setattr(main_module, "get_session_factory", lambda: SessionLocal)
    monkeypatch.setattr(
        main_module,
        "resolve_preview_for_date",
        lambda session, target_date: {
            "date": target_date.isoformat(),
            "games_considered": 2,
            "games": [],
            "partial_errors": [{"status_code": 404, "message": "not found"}],
            "warnings": [{"message": "NO_MATCHING_KALSHI_MARKET"}],
            "errors": [{"status_code": 404, "message": "not found"}],
        },
    )
    app.dependency_overrides[require_internal_api_key] = lambda: None

    try:
        response = client.get("/v1/kalshi/resolve-preview?date=2026-07-01")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["result"]["partial_errors"][0]["status_code"] == 404


def test_eastern_display_includes_daylight_label() -> None:
    assert "EDT" in eastern_display(datetime(2026, 7, 1, 12, 0, tzinfo=UTC))

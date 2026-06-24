from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.config import get_settings
from app.database import database_status
from app.main import app
from app.models import KalshiMarket, MlbGame
from app.services.kalshi import derive_orderbook_prices
from app.services import market_sync
from app.services.mapping import infer_market_type, score_mapping
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


def test_time_bucket_classification() -> None:
    assert classify_time_bucket(1500) == "24H"
    assert classify_time_bucket(725) == "12H"
    assert classify_time_bucket(95) == "90M"
    assert classify_time_bucket(20) == "15M"
    assert classify_time_bucket(1) == "5M"


def test_mapping_confidence_and_rationale() -> None:
    game = MlbGame(
        external_game_id="1",
        home_team="New York Yankees",
        away_team="Boston Red Sox",
        scheduled_start="2026-07-01T23:05:00+00:00",
        status="scheduled",
    )
    market = KalshiMarket(
        kalshi_market_id="KX-1",
        ticker="KXMLB-YANKEES-RED-SOX",
        title="Will the New York Yankees win the game against the Boston Red Sox?",
        status="open",
    )

    confidence, status, metadata = score_mapping(game, market)

    assert confidence >= 0
    assert status in {"candidate", "needs_review"}
    assert metadata["market_type"] == "full_game_moneyline"
    assert infer_market_type("first five total runs") == "first_five_total"


def test_internal_sync_endpoints_require_api_key_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("BACKEND_API_KEY", "secret-test-key")
    get_settings.cache_clear()

    try:
        response = client.post("/v1/run/paper-candidate-engine")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 401


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
                "close_time": "2026-07-01T23:00:00Z",
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
        assert "min_close_ts" in fake_client.params
        assert "max_close_ts" in fake_client.params

        open_market = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == "KXMLB-OPEN"))
        closed_market = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == "KXMLB-CLOSED"))

        assert closed_market is None
        assert open_market is not None
        assert open_market.best_yes_bid is None
        assert open_market.best_no_bid is None
        assert open_market.implied_yes_ask is None
        assert open_market.implied_no_ask is None
        assert open_market.yes_ask == Decimal("0.4600")
        assert open_market.orderbook_raw == {"error": "orderbook unavailable for KXMLB-OPEN"}

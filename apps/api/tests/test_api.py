import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import main as main_module
from app.config import get_settings
from app.database import Base, database_status
from app.main import _candidate_summary, app
from app.models import (
    BalanceSnapshot,
    BullpenDailyFeature,
    CalibrationRun,
    DECISION_REASON_MAX_LENGTH,
    FeatureSnapshot,
    JobRun,
    KalshiMarket,
    LineupSnapshot,
    MarketFamilyDiscoveryItem,
    MarketFamilyDiscoveryRun,
    MarketMapping,
    MlbGame,
    MlbFeatureSnapshot,
    ModelCandidate,
    ModelParameterVersion,
    ModelPredictionOutput,
    ModelPredictionRun,
    ModelThresholdVersion,
    ModelTrainingDataset,
    ModelVersion,
    MarketDataWorkerStatus,
    PaperTrade,
    PaperTradingEpoch,
    Position,
    Settlement,
    TeamDailyFeature,
    TeamRecentFeature,
    TrainingRun,
    TravelScheduleFeature,
    WeatherSnapshot,
    PitcherDailyFeature,
)
from app.jobs import market_family_discovery as market_family_discovery_job
from app.jobs import mlb_feature_sync as mlb_feature_sync_job
from app.jobs import model_feature_snapshot_backfill as model_feature_snapshot_backfill_job
from app.jobs import runner as job_runner
from app.security import require_internal_api_key
from app.services import (
    candidates,
    dashboard,
    features,
    job_runs,
    market_family_discovery,
    market_family_mapping,
    market_sync,
    mlb,
    mlb_stats_client,
    modeling,
    position_refresh,
    pybaseball_client,
    ws_market_data,
)
from app.services.contracts import contract_labels, selected_team_from_ticker
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
from app.services.job_runs import run_job
from app.services.paper_epoch import (
    RESET_CONFIRMATION,
    create_new_active_epoch,
    get_or_create_active_paper_epoch,
    reset_paper_trading_epoch,
)
from app.services.portfolio import calculate_paper_portfolio
from app.services.settlement import settle_paper_trades
from app.services.spread_audit import run_spread_audit
from app.services.spread_verification import verify_spread_market
from app.time_utils import classify_time_bucket, eastern_display, today_eastern
from app.workers import kalshi_ws_paper

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_settings_cache_between_tests():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _relax_data_quality_gate(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_MIN_DATA_QUALITY", "0")
    monkeypatch.setenv("PAPER_OBSERVATION_MIN_DATA_QUALITY", "0")
    get_settings.cache_clear()


def _stub_public_feature_network(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    _stub_mlb_primary_stats_empty(monkeypatch)

    monkeypatch.setattr(features.pybaseball_client, "get_statcast_range", lambda *_args, **_kwargs: {"rows": []})
    monkeypatch.setattr(features.pybaseball_client, "get_pitcher_statcast_range", lambda *_args, **_kwargs: {"rows": []})
    monkeypatch.setattr(features, "_hydrate_schedule_window", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(features, "_fetch_open_meteo", lambda *_args, **_kwargs: {"hourly": {"time": []}})


def _stub_mlb_primary_stats_empty(monkeypatch) -> None:
    class EmptyMLBStatsClient:
        def get_team_season_stats(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_team_game_log_stats(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_team_stat_splits(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_pitcher_season_stats(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_pitcher_game_log_stats(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {}

    monkeypatch.setattr(features, "MLBStatsClient", EmptyMLBStatsClient)


def _stub_pybaseball_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {
            "available": False,
            "version": None,
            "module_path": None,
            "import_error": {"error_type": "ImportError", "message": "pybaseball unavailable in this test"},
        },
    )


def _add_candidate_mapping(
    session: Session,
    game: MlbGame,
    market: KalshiMarket,
    **overrides,
) -> MarketMapping:
    session.flush()
    market.market_price_updated_at = candidates.utc_now()
    session.add(market)
    values = {
        "mlb_game_id": game.id,
        "kalshi_market_id": market.id,
        "mapping_status": "candidate",
        "confidence": Decimal("0.9500"),
    }
    values.update(overrides)
    mapping = MarketMapping(**values)
    session.add(mapping)
    return mapping


def _active_epoch_id(session: Session) -> int:
    return get_or_create_active_paper_epoch(session).id


def _fixed_model_score(
    probability: str = "0.800000",
    *,
    data_quality: str = "1.0000",
    push_probability: str = "0.000000",
) -> modeling.ModelScore:
    calibrated = Decimal(probability)
    return modeling.ModelScore(
        probability=calibrated,
        fair_value=calibrated.quantize(Decimal("0.0001")),
        rationale={"test_model": "fixed"},
        probability_raw=calibrated,
        probability_calibrated=calibrated,
        data_quality=Decimal(data_quality),
        calibration_status="calibrated",
        training_eligible=True,
        training_exclusion_reason=None,
        push_probability=Decimal(push_probability),
    )


def _cap_intent(
    session: Session,
    *,
    epoch_id: int,
    target_date: date,
    market_ticker: str,
    price: str = "0.4000",
    side: str = "yes",
    family: str = "full_game_winner",
    game_id: int | None = None,
    score: str = "1.0000",
) -> candidates.TradeIntent:
    market = KalshiMarket(
        kalshi_market_id=f"{market_ticker}-ID",
        ticker=market_ticker,
        title="Test cap market",
        status="open",
        implied_yes_ask=Decimal(price),
    )
    candidate = ModelCandidate(
        paper_trading_epoch_id=epoch_id,
        mlb_game_id=game_id,
        kalshi_market_id=None,
        evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
        features={},
        target_date=target_date,
        decision="eligible_for_paper_trade",
        market_family=family,
        contract_side=side,
        net_expected_value=Decimal("0.100000"),
        gate_diagnostics={
            "gate_mapping_ok": True,
            "gate_market_open": True,
            "gate_game_not_started": True,
            "gate_f5_tie_enabled": True,
            "gate_selection_trusted_ok": True,
            "gate_spread_trading_enabled": True,
            "gate_spread_parser_verified": True,
            "gate_price_fresh_executable": True,
            "gate_price_floor_ok": True,
            "gate_low_price_probability_edge_ok": True,
            "gate_low_price_net_ev_ok": True,
            "gate_data_quality_ok": True,
            "gate_push_ok": True,
            "gate_probability_present": True,
            "gate_gross_ev_positive": True,
            "gate_fee_present": True,
            "gate_probability_edge_ok": True,
            "gate_net_ev_ok": True,
            "gate_calibration_ok": True,
            "gate_line_selection_ok": True,
            "gate_game_scope_correlation_ok": True,
            "gate_caps_ok": True,
            "gate_open_position_ok": True,
        },
    )
    session.add_all([market, candidate])
    session.flush()
    return candidates.TradeIntent(
        candidate=candidate,
        game=SimpleNamespace(id=game_id),
        market=market,
        price=Decimal(price),
        labels=SimpleNamespace(),
        score=Decimal(score),
    )


_CANDIDATE_DECISION_PATTERN = re.compile(
    r'"((?:candidate_only|eligible_for_paper_trade|no_trade|paper_trade)[a-z0-9_]*)"'
)


def test_candidate_decision_strings_fit_persisted_schema_length() -> None:
    source = Path(candidates.__file__).read_text(encoding="utf-8")
    decisions = set(_CANDIDATE_DECISION_PATTERN.findall(source))
    required_decisions = {
        "no_trade_same_game_scope_correlation_not_best",
        "no_trade_game_scope_correlation_cap",
        "no_trade_conflicting_side_signals",
        "no_trade_fee_adjusted_ev_too_low",
        "candidate_only_existing_trade",
        "candidate_only_dry_run",
        "no_trade_low_price_bucket_risk_cap",
        "no_trade_f5_tie_disabled",
        "no_trade_price_below_floor",
        "no_trade_low_price_probability_edge_low",
        "no_trade_low_price_ev_too_low",
        "no_trade_low_price_slate_cap",
        "no_trade_low_price_sweep_cap",
        "no_trade_post_cap_size_too_small",
        "no_trade_sweep_cap_reached",
        "no_trade_time_bucket_reserve",
        "no_trade_side_concentration_cap",
    }

    assert required_decisions <= decisions
    assert ModelCandidate.__table__.c.decision.type.length == DECISION_REASON_MAX_LENGTH
    assert ModelPredictionOutput.__table__.c.decision_reason.type.length == DECISION_REASON_MAX_LENGTH

    too_long_for_candidates = sorted(
        (decision, len(decision))
        for decision in decisions
        if len(decision) > DECISION_REASON_MAX_LENGTH
    )
    assert too_long_for_candidates == []


def test_default_kalshi_fee_rounding_mode_rounds_up_to_cent(monkeypatch) -> None:
    monkeypatch.delenv("KALSHI_FEE_ROUNDING_MODE", raising=False)
    get_settings.cache_clear()

    try:
        assert candidates._fee_rounding_step() == Decimal("0.01")
        assert candidates._estimate_trade_fee(Decimal("0.5000"), 1) == Decimal("0.020000")
    finally:
        get_settings.cache_clear()


def test_health_uses_safe_defaults() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "homerun-api"
    assert payload["paper_trading"] is True
    assert payload["live_trading_enabled"] is False
    assert "timestamp" in payload


def test_pr3e_feature_hardening_preserves_paper_safety_defaults(monkeypatch) -> None:
    for name in (
        "PAPER_TRADING",
        "LIVE_TRADING_ENABLED",
        "EXECUTION_KILL_SWITCH",
        "KALSHI_ENV",
        "WEBSOCKET_MARKET_DATA_ENABLED",
        "PAPER_SPREAD_TRADING_ENABLED",
        "PAPER_MIN_DATA_QUALITY",
        "PAPER_OBSERVATION_MIN_DATA_QUALITY",
        "LIVE_MIN_DATA_QUALITY",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()

    try:
        settings = get_settings()
        assert settings.paper_trading is True
        assert settings.live_trading_enabled is False
        assert settings.execution_kill_switch is True
        assert settings.kalshi_env == "demo"
        assert settings.websocket_market_data_enabled is False
        assert settings.paper_spread_trading_enabled is False
        assert settings.paper_min_data_quality == Decimal("0.60")
        assert settings.paper_observation_min_data_quality == Decimal("0.55")
        assert settings.live_min_data_quality == Decimal("0.60")
    finally:
        get_settings.cache_clear()


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
    assert payload["observation_filter"]["observation_start_date"] == "2026-07-02"
    assert payload["observation_filter"]["active"] is True


@pytest.mark.parametrize(
    "flag",
    [
        "include_diagnostics",
        "include_job_results",
        "include_source_details",
        "include_governance_details",
        "include_spread_audit_details",
        "include_candidate_diagnostics",
    ],
)
def test_dashboard_summary_diagnostic_flags_require_internal_auth(monkeypatch, flag: str) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("BACKEND_API_KEY", raising=False)
    get_settings.cache_clear()

    try:
        public_response = client.get("/v1/dashboard/summary")
        diagnostic_response = client.get(f"/v1/dashboard/summary?{flag}=true")
    finally:
        get_settings.cache_clear()

    assert public_response.status_code == 200
    assert diagnostic_response.status_code == 401
    assert "BACKEND_API_KEY" in diagnostic_response.json()["detail"]


def test_dashboard_summary_diagnostic_flags_accept_internal_auth(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("BACKEND_API_KEY", "test-dashboard-key")
    get_settings.cache_clear()

    try:
        response = client.get(
            "/v1/dashboard/summary?include_source_details=true",
            headers={"X-API-Key": "test-dashboard-key"},
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200


def test_paper_epoch_reset_archives_old_rows_and_starts_at_500() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch = get_or_create_active_paper_epoch(session, starting_balance=Decimal("1000.00"))
        trade = PaperTrade(
            paper_trading_epoch_id=epoch.id,
            market_ticker="KXMLBGAME-RESET-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            entry_time=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            status="open",
        )
        session.add(trade)
        session.commit()

        with pytest.raises(ValueError):
            reset_paper_trading_epoch(
                session,
                archive_current_as="pre_pr3d_validation",
                new_epoch="pr3d_paper_observation_v1",
                starting_balance=Decimal("500.00"),
                archive_open_positions=True,
                reset_dashboard_metrics=True,
                confirmation="WRONG",
            )

        result = reset_paper_trading_epoch(
            session,
            archive_current_as="pre_pr3d_validation",
            new_epoch="pr3d_paper_observation_v1",
            starting_balance=Decimal("500.00"),
            archive_open_positions=True,
            reset_dashboard_metrics=True,
            confirmation=RESET_CONFIRMATION,
        )
        summary = dashboard.dashboard_summary_from_db(session)
        archived_trade = session.scalar(select(PaperTrade).where(PaperTrade.market_ticker == "KXMLBGAME-RESET-PIT"))

    assert result["new_epoch_key"] == "pr3d_paper_observation_v1"
    assert result["starting_balance"] == 500.0
    assert result["new_balance_snapshot_id"] is not None
    assert archived_trade is not None
    assert archived_trade.status == "archived"
    assert summary.active_epoch is not None
    assert summary.active_epoch.epoch_key == "pr3d_paper_observation_v1"
    assert summary.portfolio_value == 500.0
    assert summary.cash_balance == 500.0
    assert summary.positions == []
    assert summary.closed_positions == []
    assert summary.performance.record == "0-0-0"
    assert summary.performance.profit_loss == 0.0


def test_active_paper_epoch_uses_bankroll_starting_balance_by_default(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_STARTING_BALANCE", "1000.00")
    monkeypatch.setenv("PAPER_BANKROLL_STARTING_BALANCE", "500.00")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            epoch = get_or_create_active_paper_epoch(session)
    finally:
        get_settings.cache_clear()

    assert epoch.starting_balance == Decimal("500.00")


def test_create_new_active_epoch_clears_rows_when_reusing_old_key() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        reused_epoch = PaperTradingEpoch(
            epoch_key="reused-pr3d-epoch",
            display_name="REUSED PR3D EPOCH",
            status="archived",
            mode="paper",
            starting_balance=Decimal("1000.00"),
            started_at=now - timedelta(days=2),
            archived_at=now - timedelta(days=1),
            archive_reason="previous_reset",
        )
        session.add(reused_epoch)
        session.flush()
        candidate = ModelCandidate(
            paper_trading_epoch_id=reused_epoch.id,
            evaluated_at=now - timedelta(days=1),
            features={},
            decision="candidate_only",
        )
        prediction_run = ModelPredictionRun(
            paper_trading_epoch_id=reused_epoch.id,
            started_at=now - timedelta(days=1),
            target_date=date(2026, 7, 1),
            status="completed",
        )
        balance_snapshot = BalanceSnapshot(
            paper_trading_epoch_id=reused_epoch.id,
            captured_at=now - timedelta(days=1),
            cash_balance=Decimal("900.00"),
            portfolio_value=Decimal("950.00"),
            source="paper",
        )
        job_run = JobRun(
            paper_trading_epoch_id=reused_epoch.id,
            job_name="candidate-sweep",
            job_type="paper_ops",
            status="succeeded",
            started_at=now - timedelta(days=1),
            completed_at=now - timedelta(days=1) + timedelta(minutes=1),
        )
        session.add_all([candidate, prediction_run, balance_snapshot, job_run])
        session.flush()
        trade = PaperTrade(
            paper_trading_epoch_id=reused_epoch.id,
            candidate_id=candidate.id,
            market_ticker="KXMLBGAME-REUSED-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            entry_time=now - timedelta(days=1),
            status="settled",
        )
        prediction_output = ModelPredictionOutput(
            paper_trading_epoch_id=reused_epoch.id,
            prediction_run_id=prediction_run.id,
            candidate_id=candidate.id,
            decision_reason="old_output",
        )
        feature_snapshot = FeatureSnapshot(
            candidate_id=candidate.id,
            captured_at=now - timedelta(days=1),
            features={},
            source="old_epoch",
        )
        session.add_all([trade, prediction_output, feature_snapshot])
        session.flush()
        settlement = Settlement(
            paper_trade_id=trade.id,
            settled_at=now - timedelta(days=1),
            resolution="win",
            payout=Decimal("1.00"),
            realized_pnl=Decimal("0.60"),
        )
        reused_epoch.current_balance_snapshot_id = balance_snapshot.id
        session.add_all([settlement, reused_epoch])
        session.commit()

        active = create_new_active_epoch(
            session,
            epoch_key="reused-pr3d-epoch",
            starting_balance=Decimal("500.00"),
            notes={"created_by": "test"},
        )
        session.commit()

        remaining = {
            "trades": list(session.scalars(select(PaperTrade).where(PaperTrade.paper_trading_epoch_id == active.id))),
            "settlements": list(session.scalars(select(Settlement))),
            "candidates": list(
                session.scalars(select(ModelCandidate).where(ModelCandidate.paper_trading_epoch_id == active.id))
            ),
            "outputs": list(
                session.scalars(select(ModelPredictionOutput).where(ModelPredictionOutput.paper_trading_epoch_id == active.id))
            ),
            "runs": list(
                session.scalars(select(ModelPredictionRun).where(ModelPredictionRun.paper_trading_epoch_id == active.id))
            ),
            "features": list(session.scalars(select(FeatureSnapshot))),
            "snapshots": list(
                session.scalars(select(BalanceSnapshot).where(BalanceSnapshot.paper_trading_epoch_id == active.id))
            ),
            "jobs": list(session.scalars(select(JobRun).where(JobRun.paper_trading_epoch_id == active.id))),
        }

    assert active.status == "active"
    assert active.starting_balance == Decimal("500.00")
    assert active.current_balance_snapshot_id is None
    assert active.notes is not None
    assert active.notes["cleared_reused_epoch_rows"]["paper_trades"] == 1
    assert all(not rows for rows in remaining.values())


def test_paper_epoch_reset_rejects_matching_archive_and_new_keys() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        session.commit()
        active_id = active.id

        with pytest.raises(ValueError, match="must be different"):
            reset_paper_trading_epoch(
                session,
                archive_current_as="same_epoch",
                new_epoch="same_epoch",
                starting_balance=Decimal("500.00"),
                archive_open_positions=True,
                reset_dashboard_metrics=True,
                confirmation=RESET_CONFIRMATION,
            )
        session.rollback()
        active_after = session.get(PaperTradingEpoch, active_id)

    assert active_after is not None
    assert active_after.status == "active"
    assert active_after.epoch_key == "pr3d_paper_observation_v1"


def test_archive_current_epoch_preserves_source_starting_balance(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_STARTING_BALANCE", "1000.00")
    monkeypatch.setenv("PAPER_BANKROLL_STARTING_BALANCE", "500.00")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            get_or_create_active_paper_epoch(session)
            archived = reset_paper_trading_epoch(
                session,
                archive_current_as="pre_pr3d_validation",
                new_epoch="pr3d_paper_observation_v1",
                starting_balance=Decimal("500.00"),
                archive_open_positions=True,
                reset_dashboard_metrics=True,
                confirmation=RESET_CONFIRMATION,
            )
            archived_epoch = session.scalar(select(PaperTradingEpoch).where(PaperTradingEpoch.epoch_key == archived["archived_epoch_key"]))
    finally:
        get_settings.cache_clear()

    assert archived_epoch is not None
    assert archived_epoch.starting_balance == Decimal("500.00")


def test_paper_epoch_reset_carries_open_positions_into_new_epoch() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch = get_or_create_active_paper_epoch(session, starting_balance=Decimal("1000.00"))
        trade = PaperTrade(
            paper_trading_epoch_id=epoch.id,
            market_ticker="KXMLBGAME-CARRY-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4500"),
            quantity=1,
            entry_time=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            status="open",
        )
        session.add(trade)
        session.commit()

        result = reset_paper_trading_epoch(
            session,
            archive_current_as="pre_pr3d_validation",
            new_epoch="pr3d_paper_observation_v1",
            starting_balance=Decimal("500.00"),
            archive_open_positions=False,
            reset_dashboard_metrics=True,
            confirmation=RESET_CONFIRMATION,
        )
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        carried_trade = session.scalar(select(PaperTrade).where(PaperTrade.market_ticker == "KXMLBGAME-CARRY-PIT"))
        reset_snapshot = session.get(BalanceSnapshot, result["new_balance_snapshot_id"])

    assert result["archived_trades_count"] == 0
    assert carried_trade is not None
    assert carried_trade.paper_trading_epoch_id == active.id
    assert carried_trade.status == "open"
    assert carried_trade.resolution is None
    assert carried_trade.exit_time is None
    assert reset_snapshot is not None
    assert reset_snapshot.cash_balance == Decimal("499.60")
    assert reset_snapshot.portfolio_value == Decimal("500.05")


def test_paper_epoch_reset_does_not_resurrect_old_archived_positions() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
        active_epoch = get_or_create_active_paper_epoch(session, starting_balance=Decimal("1000.00"))
        old_archive = PaperTradingEpoch(
            epoch_key="pre_pr3d_validation",
            display_name="PRE PR3D VALIDATION",
            status="archived",
            mode="paper",
            starting_balance=Decimal("1000.00"),
            started_at=now - timedelta(days=2),
            archived_at=now - timedelta(days=1),
            archive_reason="previous_reset",
        )
        current_trade = PaperTrade(
            paper_trading_epoch_id=active_epoch.id,
            market_ticker="KXMLBGAME-CURRENT-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4500"),
            quantity=1,
            entry_time=now,
            status="open",
        )
        old_trade = PaperTrade(
            paper_trading_epoch_id=None,
            market_ticker="KXMLBGAME-OLD-PIT",
            contract_side="yes",
            entry_price=Decimal("0.3000"),
            current_price=Decimal("0.3000"),
            quantity=1,
            entry_time=now - timedelta(days=2),
            exit_time=now - timedelta(days=1),
            status="archived",
            resolution="EPOCH_ARCHIVED",
        )
        session.add_all([old_archive, current_trade, old_trade])
        session.flush()
        old_trade.paper_trading_epoch_id = old_archive.id
        session.commit()

        reset_paper_trading_epoch(
            session,
            archive_current_as="pre_pr3d_validation",
            new_epoch="pr3d_paper_observation_v1",
            starting_balance=Decimal("500.00"),
            archive_open_positions=False,
            reset_dashboard_metrics=True,
            confirmation=RESET_CONFIRMATION,
        )
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        carried_trade = session.scalar(select(PaperTrade).where(PaperTrade.market_ticker == "KXMLBGAME-CURRENT-PIT"))
        stale_trade = session.scalar(select(PaperTrade).where(PaperTrade.market_ticker == "KXMLBGAME-OLD-PIT"))

    assert carried_trade is not None
    assert carried_trade.paper_trading_epoch_id == active.id
    assert carried_trade.status == "open"
    assert stale_trade is not None
    assert stale_trade.paper_trading_epoch_id == old_archive.id
    assert stale_trade.status == "archived"
    assert stale_trade.resolution == "EPOCH_ARCHIVED"


def test_archived_epoch_key_requires_include_archived() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        archived = PaperTradingEpoch(
            epoch_key="archived-dashboard",
            display_name="ARCHIVED DASHBOARD",
            status="archived",
            mode="paper",
            starting_balance=Decimal("1000.00"),
            started_at=now - timedelta(days=1),
            archived_at=now,
        )
        session.add(archived)
        session.commit()

        with pytest.raises(ValueError, match="include_archived=true"):
            dashboard.dashboard_summary_from_db(session, epoch_key="archived-dashboard")
        summary = dashboard.dashboard_summary_from_db(session, epoch_key="archived-dashboard", include_archived=True)

    assert summary.active_epoch is not None
    assert summary.active_epoch.epoch_key == "archived-dashboard"


def test_dashboard_endpoint_returns_bad_request_for_invalid_epoch_filter(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    with SessionLocal() as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        session.add(
            PaperTradingEpoch(
                epoch_key="archived-dashboard-api",
                display_name="ARCHIVED DASHBOARD API",
                status="archived",
                mode="paper",
                starting_balance=Decimal("1000.00"),
                started_at=now - timedelta(days=1),
                archived_at=now,
            )
        )
        session.commit()

    monkeypatch.setattr(
        main_module,
        "database_status",
        lambda: {"ready": True, "configured": True, "dialect": "sqlite", "message": "ok"},
    )
    monkeypatch.setattr(main_module, "get_session_factory", lambda: SessionLocal)

    archived_response = client.get("/v1/dashboard/summary?epoch_key=archived-dashboard-api")
    missing_response = client.get("/v1/dashboard/summary?epoch_key=missing-dashboard-api")

    assert archived_response.status_code == 400
    assert "include_archived=true" in archived_response.json()["detail"]
    assert missing_response.status_code == 400
    assert missing_response.json()["detail"] == "Unknown paper trading epoch: missing-dashboard-api"


def test_dashboard_job_status_is_scoped_to_active_epoch() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        archived = PaperTradingEpoch(
            epoch_key="archived-job-status",
            display_name="ARCHIVED JOB STATUS",
            status="archived",
            mode="paper",
            starting_balance=Decimal("1000.00"),
            started_at=now - timedelta(days=1),
            archived_at=now,
        )
        session.add(archived)
        session.flush()
        session.add_all(
            [
                JobRun(
                    job_name="candidate-sweep",
                    job_type="candidate-sweep",
                    paper_trading_epoch_id=active.id,
                    status="succeeded",
                    started_at=now,
                    completed_at=now + timedelta(minutes=1),
                    result={"epoch": "active"},
                ),
                JobRun(
                    job_name="candidate-sweep",
                    job_type="candidate-sweep",
                    paper_trading_epoch_id=archived.id,
                    status="failed",
                    started_at=now + timedelta(hours=1),
                    completed_at=now + timedelta(hours=1, minutes=1),
                    result={"epoch": "archived"},
                ),
            ]
        )
        session.commit()

        summary = dashboard.dashboard_summary_from_db(session)

    assert summary.job_status["candidate-sweep"].status == "succeeded"
    assert summary.job_status["candidate-sweep"].result == {}


def test_dashboard_job_status_fetches_latest_per_job_without_global_truncation() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        rows: list[JobRun] = []
        for index in range(55):
            rows.append(
                JobRun(
                    job_name="price-refresh",
                    job_type="paper_ops",
                    paper_trading_epoch_id=active.id,
                    status="succeeded",
                    started_at=now + timedelta(minutes=index),
                    completed_at=now + timedelta(minutes=index, seconds=10),
                    result={"refresh_index": index},
                )
            )
        rows.append(
            JobRun(
                job_name="governance",
                job_type="paper_ops",
                paper_trading_epoch_id=active.id,
                status="succeeded",
                started_at=now - timedelta(hours=1),
                completed_at=now - timedelta(minutes=59),
                result={"governance": "present"},
            )
        )
        rows.append(
            JobRun(
                job_name="full-paper-cycle",
                job_type="paper_ops",
                paper_trading_epoch_id=active.id,
                status="succeeded",
                started_at=now - timedelta(minutes=30),
                completed_at=now - timedelta(minutes=29),
                result={"cycle": "present"},
            )
        )
        session.add_all(rows)
        session.commit()

        summary = dashboard.dashboard_summary_from_db(session)
        debug_summary = dashboard.dashboard_summary_from_db(session, include_job_results=True)

    assert summary.job_status["price-refresh"].result == {}
    assert summary.job_status["governance"].result == {}
    assert summary.job_status["full-paper-cycle"].result == {}
    assert debug_summary.job_status["price-refresh"].result == {"refresh_index": 54}
    assert debug_summary.job_status["governance"].result == {"governance": "present"}
    assert debug_summary.job_status["full-paper-cycle"].result == {"cycle": "present"}


def test_dashboard_summary_compacts_heavy_job_payloads_by_default() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 3, 16, 0, tzinfo=UTC)
    heavy_items = [
        {
            "market_ticker": f"KXMLBSPREAD-ROW-{index}",
            "raw_payload": {"large": "x" * 1000},
            "features": {"large": "y" * 1000},
        }
        for index in range(8)
    ]

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        session.add(
            JobRun(
                job_name="spread-audit",
                job_type="paper_ops",
                paper_trading_epoch_id=active.id,
                status="succeeded",
                started_at=now,
                completed_at=now + timedelta(seconds=10),
                result={
                    "status": "completed",
                    "checked": 8,
                    "items": heavy_items,
                    "examples_by_reason": {"needs_review": heavy_items},
                },
                warnings=[{"message": "sample warning"}],
                errors=[],
                steps=[{"name": "spread_audit", "status": "succeeded"}],
            )
        )
        session.commit()

        summary = dashboard.dashboard_summary_from_db(session)
        debug_summary = dashboard.dashboard_summary_from_db(session, include_job_results=True)

    compact = summary.job_status["spread-audit"]
    assert compact.result_is_compact is True
    assert compact.step_count is None
    assert compact.warning_count is None
    assert compact.error_count is None
    assert compact.result == {}
    assert "raw_payload" not in json.dumps(compact.result)
    assert "features" not in json.dumps(compact.result)

    debug_result = debug_summary.job_status["spread-audit"].result
    assert debug_result["items"]["item_count"] == 8
    assert debug_result["items"]["truncated"] is False
    assert "raw_payload" not in json.dumps(debug_result)
    assert "features" not in json.dumps(debug_result)


def test_compact_dashboard_job_status_does_not_deserialize_job_json() -> None:
    def reject_json_deserialization(_value: object) -> object:
        raise AssertionError("compact job status must not deserialize JSON columns")

    engine = create_engine("sqlite+pysqlite:///:memory:", json_deserializer=reject_json_deserialization)
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 3, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        active_id = active.id
        session.add(
            JobRun(
                job_name="candidate-sweep",
                job_type="paper_ops",
                paper_trading_epoch_id=active_id,
                status="succeeded",
                started_at=now,
                completed_at=now + timedelta(seconds=10),
                result={"large": ["x" * 1000]},
                warnings=[{"message": "sample warning"}],
                errors=[],
                steps=[{"name": "paper_candidate_engine", "status": "succeeded"}],
            )
        )
        session.commit()

        latest = dashboard._latest_job_status(session, SimpleNamespace(id=active_id))

    assert latest["candidate-sweep"].status == "succeeded"
    assert latest["candidate-sweep"].result == {}


def test_spread_audit_details_only_deserializes_spread_job_json() -> None:
    def reject_candidate_sweep_json(value: object) -> object:
        raw = str(value)
        if "candidate-large" in raw:
            raise AssertionError("spread-audit details must not deserialize candidate-sweep JSON")
        return json.loads(raw)

    engine = create_engine("sqlite+pysqlite:///:memory:", json_deserializer=reject_candidate_sweep_json)
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 3, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        active_id = active.id
        session.add_all(
            [
                JobRun(
                    job_name="candidate-sweep",
                    job_type="paper_ops",
                    paper_trading_epoch_id=active_id,
                    status="succeeded",
                    started_at=now,
                    completed_at=now + timedelta(seconds=10),
                    result={"large": "candidate-large"},
                    warnings=[],
                    errors=[],
                    steps=[],
                ),
                JobRun(
                    job_name="spread-audit",
                    job_type="paper_ops",
                    paper_trading_epoch_id=active_id,
                    status="succeeded",
                    started_at=now,
                    completed_at=now + timedelta(seconds=10),
                    result={"status": "completed", "checked": 4},
                    warnings=[],
                    errors=[],
                    steps=[],
                ),
            ]
        )
        session.commit()

        latest = dashboard._latest_job_status(
            session,
            SimpleNamespace(id=active_id),
            detailed_job_names={"spread-audit"},
        )

    assert latest["candidate-sweep"].result == {}
    assert latest["candidate-sweep"].result_is_compact is True
    assert latest["spread-audit"].result == {"status": "completed", "checked": 4}
    assert latest["spread-audit"].result_is_compact is False


def test_dashboard_candidate_diagnostics_keep_compact_counts_by_default() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 3, 16, 0, tzinfo=UTC)
    counterfactuals = [
        {"candidate_id": index, "market_ticker": f"KXMLBGAME-{index}", "net_expected_value": "0.12"}
        for index in range(6)
    ]

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        session.add(
            ModelPredictionRun(
                paper_trading_epoch_id=active.id,
                started_at=now,
                completed_at=now,
                target_date=today_eastern(),
                status="completed",
                candidates_evaluated=6,
                trades_created=0,
                summary={
                    "candidate_diagnostics": {
                        "candidates_total": 6,
                        "trade_eligible_after_quality": 0,
                        "top_quality_blockers": counterfactuals,
                    },
                    "quality_ev_diagnostics": {
                        "candidates_total": 6,
                        "ev_pass_count": 2,
                        "top_counterfactual_candidates_blocked_by_quality": counterfactuals,
                    },
                },
            )
        )
        session.commit()

        summary = dashboard.dashboard_summary_from_db(session)
        debug_summary = dashboard.dashboard_summary_from_db(session, include_candidate_diagnostics=True)

    compact = summary.latest_candidate_diagnostics
    assert compact["candidate_diagnostics"]["candidates_total"] == 6
    assert compact["candidate_diagnostics"]["top_quality_blockers_count"] == 6
    assert "top_quality_blockers" not in compact["candidate_diagnostics"]
    assert (
        compact["quality_ev_diagnostics"]["top_counterfactual_candidates_blocked_by_quality_count"]
        == 6
    )
    assert "candidate_id" not in json.dumps(compact)
    debug_payload = debug_summary.latest_candidate_diagnostics
    assert debug_payload["quality_ev_diagnostics"]["top_counterfactual_candidates_blocked_by_quality"]["item_count"] == 6


def test_compact_dashboard_does_not_deserialize_prediction_summary() -> None:
    def reject_prediction_summary_json(value: object) -> object:
        raw = str(value)
        if "candidate-large" in raw:
            raise AssertionError("compact dashboard must not deserialize prediction summary JSON")
        return json.loads(raw)

    engine = create_engine("sqlite+pysqlite:///:memory:", json_deserializer=reject_prediction_summary_json)
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 3, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        session.add(
            ModelPredictionRun(
                paper_trading_epoch_id=active.id,
                started_at=now,
                completed_at=now,
                target_date=today_eastern(),
                status="completed",
                candidates_evaluated=1,
                trades_created=2,
                trade_policy={"mode": "paper"},
                summary={
                    "candidate_diagnostics": {
                        "candidates_total": 1,
                        "quality_threshold": "0.55",
                        "quality_block_reason_counts": {"low_quality_missing_cached_features": 1},
                        "top_quality_blockers": [{"candidate_id": "candidate-large"}],
                    },
                    "quality_ev_diagnostics": {
                        "candidates_total": 1,
                        "quality_blocked_count": 1,
                        "top_counterfactual_candidates_blocked_by_quality": [
                            {"candidate_id": "candidate-large"}
                        ],
                    },
                },
            )
        )
        session.commit()

        summary = dashboard.dashboard_summary_from_db(session)

    assert summary.latest_candidate_diagnostics["candidate_diagnostics"]["candidates_total"] == 1
    assert summary.latest_candidate_diagnostics["candidate_diagnostics"]["top_quality_blockers_count"] == 1
    assert summary.latest_candidate_diagnostics["quality_ev_diagnostics"]["quality_blocked_count"] == 1
    assert (
        summary.latest_candidate_diagnostics["quality_ev_diagnostics"][
            "top_counterfactual_candidates_blocked_by_quality_count"
        ]
        == 1
    )
    assert summary.model_status.data_quality_summary["quality_threshold"] == "0.55"
    assert summary.model_status.trade_policy == {"mode": "paper"}
    assert summary.model_status.trade_caps_used == {"paper_trades": 2}


def test_governance_status_defaults_to_compact_registry_and_allows_debug_details() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        compact = modeling.governance_status(session)
        detailed = modeling.governance_status(session, include_details=True)

    assert "governed_now" not in compact["governance_parameter_registry"]
    assert compact["governance_parameter_registry"]["governed_now_count"] >= 1
    assert "governed_now" in detailed["governance_parameter_registry"]


def test_dashboard_governance_details_flag_returns_registry_lists() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        compact = dashboard.dashboard_summary_from_db(session)
        detailed = dashboard.dashboard_summary_from_db(session, include_governance_details=True)

    compact_registry = compact.model_status.governance_parameter_registry
    detailed_registry = detailed.model_status.governance_parameter_registry
    assert "governed_now" not in compact_registry
    assert compact_registry["governed_now_count"] >= 1
    assert "governed_now" in detailed_registry
    assert detailed_registry["governed_now"]["item_count"] >= 1


def test_dashboard_source_status_is_summarized_for_model_status(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    def fake_source_status_report(_session):
        return {
            "feature_sync_enable_network_sources": True,
            "public_sources_enabled": True,
            "validation_status": "degraded_with_errors",
            "last_attempted_sync": "2026-07-03T12:00:00+00:00",
            "last_feature_sync_status": {
                "validation_status": "degraded_with_errors",
                "last_error": {"message": "source failed"},
                "raw_payload": {"large": "x" * 1000},
            },
            "latest_errors": [
                {"table": "weather_snapshots", "message": "failed"},
                {"table": "statcast", "message": "empty"},
                {"table": "pybaseball", "message": "403"},
                {"table": "extra", "message": "ignored"},
            ],
            "source_health": [
                {"source_name": "mlb_stats_api", "status": "available", "criticality": "critical"},
                {"source_name": "statcast_savant", "status": "cached", "criticality": "secondary"},
            ],
            "tables": {"large": {"raw_payload": "x" * 1000}},
            "source_inventory": [{"raw_payload": "x" * 1000}],
            "latest_feature_completeness": {"date": "2026-07-03", "summary": {"total_modules": 17}},
        }

    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        monkeypatch.setattr(dashboard, "source_status_report", fake_source_status_report)
        summary = dashboard.dashboard_summary_from_db(session)
        detailed = dashboard.dashboard_summary_from_db(session, include_source_details=True)

    payload = summary.model_status.last_feature_sync_status
    assert payload["validation_status"] == "degraded_with_errors"
    assert "raw_payload" not in json.dumps(payload)
    assert summary.model_status.source_details == {}
    assert detailed.model_status.source_details["source_health"]["item_count"] == 2
    assert detailed.model_status.source_details["source_inventory"]["item_count"] == 1
    assert "tables" in detailed.model_status.source_details
    assert "raw_payload" not in json.dumps(detailed.model_status.source_details)


def test_system_status_redacts_secrets_and_allows_missing_database() -> None:
    response = client.get("/v1/system/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["backend"]["ready"] is True
    assert payload["database"]["configured"] is False
    assert payload["database"]["ready"] is False
    assert payload["config"]["ready"] is True
    assert payload["config"]["kalshi_market_data_source"] == "production_public_market_data"
    assert payload["config"]["kalshi_market_data_base_kind"] == "production_public_market_data"
    assert payload["config"]["kalshi_credentials"] == "not_set"
    assert "KALSHI_API_KEY" not in str(payload)


def test_system_status_preserves_source_config_when_database_ready(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    def fake_source_status_report(_session):
        return {
            "feature_sync_enable_network_sources": True,
            "public_sources_enabled": True,
            "mlb_stats_base_url": "https://statsapi.mlb.com/api/v1",
            "open_meteo_base_url": "https://api.open-meteo.com/v1",
            "optional_injury_provider_configured": False,
            "optional_lineup_provider_configured": False,
            "optional_weather_provider_configured": True,
            "validation_status": "ok",
            "last_attempted_sync": "2026-07-03T12:00:00+00:00",
            "last_feature_sync_status": {
                "validation_status": "ok",
                "raw_payload": {"large": "x" * 1000},
            },
            "latest_errors": [],
            "source_health": [
                {"source_name": "mlb_stats_api", "status": "available", "criticality": "critical"},
            ],
        }

    monkeypatch.setattr(
        main_module,
        "database_status",
        lambda: {"ready": True, "configured": True, "dialect": "sqlite", "message": "ok"},
    )
    monkeypatch.setattr(main_module, "get_session_factory", lambda: SessionLocal)
    monkeypatch.setattr(main_module, "source_status_report", fake_source_status_report)

    response = client.get("/v1/system/status")

    assert response.status_code == 200
    source_status = response.json()["config"]["source_status"]
    assert source_status["mlb_stats_base_url"] == "https://statsapi.mlb.com/api/v1"
    assert source_status["open_meteo_base_url"] == "https://api.open-meteo.com/v1"
    assert source_status["optional_weather_provider_configured"] is True
    assert source_status["source_health_status_counts"] == {"available": 1}
    assert "raw_payload" not in json.dumps(source_status)


def test_model_sources_status_endpoint_reports_public_sources(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    monkeypatch.setattr(
        main_module,
        "database_status",
        lambda: {"ready": True, "configured": True, "dialect": "sqlite", "message": "ok"},
    )
    monkeypatch.setattr(main_module, "get_session_factory", lambda: SessionLocal)
    app.dependency_overrides[require_internal_api_key] = lambda: None
    try:
        response = client.get("/v1/model/sources/status")
        payload = response.json()
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["result"]["feature_sync_enable_network_sources"] is True
    assert payload["result"]["public_sources_enabled"] is True
    assert payload["result"]["mlb_stats_base_url"] == "https://statsapi.mlb.com/api/v1"
    assert "weather_snapshots" in payload["result"]["tables"]


def test_mlb_stats_client_uses_v1_1_for_live_game_feeds(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_get_json(url: str, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return {"ok": True}

    monkeypatch.setattr(mlb_stats_client, "get_json", fake_get_json)
    client = mlb_stats_client.MLBStatsClient(base_url="https://statsapi.mlb.com/api/v1")

    response = client.get_game_feed("12345")

    assert response == {"ok": True}
    assert captured["url"] == "https://statsapi.mlb.com/api/v1.1/game/12345/feed/live"
    assert captured["params"] == {}


def test_mlb_stats_client_default_schedule_hydrates_linescore(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_get_json(url: str, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return {"dates": []}

    monkeypatch.setattr(mlb_stats_client, "get_json", fake_get_json)
    client = mlb_stats_client.MLBStatsClient(base_url="https://statsapi.mlb.com/api/v1")

    response = client.get_schedule(date(2026, 7, 1))

    assert response == {"dates": []}
    assert captured["url"] == "https://statsapi.mlb.com/api/v1/schedule"
    assert captured["params"]["date"] == "2026-07-01"
    assert "linescore" in str(captured["params"]["hydrate"])


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


def test_kalshi_client_retries_429_and_respects_retry_after(monkeypatch) -> None:
    client_instance = KalshiClient(
        base_url="https://example.test",
        max_retries=1,
        backoff_base_ms=1000,
        backoff_max_ms=10000,
    )
    calls = 0
    sleeps: list[float] = []

    def fake_raw_get_json(path: str, params: dict[str, object] | None = None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HttpJsonError(
                "rate limited",
                endpoint=f"https://example.test{path}",
                params=params or {},
                status_code=429,
                body_preview="too_many_requests",
                response_headers={"Retry-After": "2"},
            )
        return {"markets": []}

    monkeypatch.setattr(client_instance, "_raw_get_json", fake_raw_get_json)
    monkeypatch.setattr("app.services.kalshi.time.sleep", lambda seconds: sleeps.append(seconds))

    payload = client_instance.get_markets({"limit": 1})

    assert payload == {"markets": []}
    assert calls == 2
    assert client_instance.rate_limited_count == 1
    assert client_instance.retries_attempted == 1
    assert sleeps == [2.0]


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


def test_paper_candidate_engine_endpoint_uses_explicit_target_date(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    monkeypatch.setattr(
        main_module,
        "database_status",
        lambda: {"ready": True, "configured": True, "dialect": "sqlite", "message": "ok"},
    )
    monkeypatch.setattr(main_module, "get_session_factory", lambda: SessionLocal)

    with SessionLocal() as session:
        target_game = MlbGame(
            external_game_id="target-date-game",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 2, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        other_game = MlbGame(
            external_game_id="other-date-game",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            home_abbreviation="NYY",
            away_abbreviation="BOS",
            scheduled_start=datetime(2026, 7, 3, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        target_market = KalshiMarket(
            kalshi_market_id="KX-TARGET-DATE",
            ticker="KXMLBGAME-TARGET-DATE-PIT",
            title="Will Pittsburgh win?",
            status="open",
            implied_yes_ask=Decimal("0.4000"),
        )
        other_market = KalshiMarket(
            kalshi_market_id="KX-OTHER-DATE",
            ticker="KXMLBGAME-OTHER-DATE-NYY",
            title="Will New York win?",
            status="open",
            implied_yes_ask=Decimal("0.4000"),
        )
        session.add_all([target_game, other_game, target_market, other_market])
        _add_candidate_mapping(session, target_game, target_market)
        _add_candidate_mapping(session, other_game, other_market)
        session.commit()

    app.dependency_overrides[require_internal_api_key] = lambda: None
    try:
        response = client.post("/v1/run/paper-candidate-engine?target_date=2026-07-02")
        payload = response.json()
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert payload["result"]["date"] == 20260702
    assert payload["result"]["target_date"] == "2026-07-02"
    assert payload["result"]["prediction_run_target_date"] == "2026-07-02"
    assert payload["result"]["evaluated_game_count"] == 1
    assert payload["result"]["candidates"] == 1


def test_generate_candidates_time_window_filters_games(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    now = datetime(2026, 7, 2, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.900000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game_specs = [
            ("in-window", now + timedelta(minutes=60), "PIT"),
            ("too-soon", now + timedelta(minutes=20), "SEA"),
            ("too-late", now + timedelta(minutes=240), "BOS"),
            ("started", now - timedelta(minutes=10), "NYY"),
            ("wrong-date", now + timedelta(days=1, minutes=60), "LAD"),
        ]
        for slug, scheduled_start, team_code in game_specs:
            game = MlbGame(
                external_game_id=f"window-{slug}",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation=team_code,
                away_abbreviation="SEA" if team_code != "SEA" else "PIT",
                scheduled_start=scheduled_start,
                status="scheduled",
            )
            market = KalshiMarket(
                kalshi_market_id=f"KX-WINDOW-{slug.upper()}",
                ticker=f"KXMLBGAME-WINDOW-{slug.upper()}-{team_code}",
                title=f"Will {team_code} win?",
                status="open",
                implied_yes_ask=Decimal("0.4000"),
                market_price_updated_at=now,
            )
            session.add_all([game, market])
            _add_candidate_mapping(
                session,
                game,
                market,
                mapping_status="confirmed",
                market_family="full_game_winner",
                market_type="full_game_winner",
                selection_code=team_code,
                settlement_rule_status="paper_supported",
            )
        session.commit()

        result = candidates.generate_candidates(
            session,
            target_date=date(2026, 7, 2),
            min_time_to_start_minutes=45,
            max_time_to_start_minutes=180,
            sweep_label="pregame_window",
        )
        all_candidates = list(session.scalars(select(ModelCandidate).order_by(ModelCandidate.id.asc())))
        all_trades = list(session.scalars(select(PaperTrade).order_by(PaperTrade.id.asc())))

    assert result["status"] == "completed"
    assert result["sweep_window_enabled"] is True
    assert result["sweep_label"] == "pregame_window"
    assert result["games_total_for_date"] == 4
    assert result["games_in_window"] == 1
    assert result["games_excluded_too_soon"] == 1
    assert result["games_excluded_too_late"] == 1
    assert result["games_excluded_started"] == 1
    assert result["games_excluded_wrong_date"] == 1
    assert result["candidates"] == 1
    assert result["candidates_in_window"] == 1
    assert result["paper_trades"] == 1
    assert result["paper_trades_in_window"] == 1
    assert len(all_candidates) == 1
    assert len(all_trades) == 1
    assert all_trades[0].market_ticker == "KXMLBGAME-WINDOW-IN-WINDOW-PIT"


def test_generate_candidates_time_window_returns_skipped_when_empty(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    now = datetime(2026, 7, 2, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.900000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="window-empty-late",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=now + timedelta(minutes=240),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-WINDOW-EMPTY",
            ticker="KXMLBGAME-WINDOW-EMPTY-PIT",
            title="Will Pittsburgh win?",
            status="open",
            implied_yes_ask=Decimal("0.4000"),
            market_price_updated_at=now,
        )
        session.add_all([game, market])
        _add_candidate_mapping(
            session,
            game,
            market,
            mapping_status="confirmed",
            market_family="full_game_winner",
            market_type="full_game_winner",
            selection_code="PIT",
            settlement_rule_status="paper_supported",
        )
        session.commit()

        result = candidates.generate_candidates(
            session,
            target_date=date(2026, 7, 2),
            min_time_to_start_minutes=45,
            max_time_to_start_minutes=180,
        )
        all_candidates = list(session.scalars(select(ModelCandidate)))
        all_trades = list(session.scalars(select(PaperTrade)))

    assert result["status"] == "skipped_no_games_in_window"
    assert result["games_in_window"] == 0
    assert result["games_excluded_too_late"] == 1
    assert result["zero_trade_reason"] == "skipped_no_games_in_window"
    assert result["candidates"] == 0
    assert result["paper_trades"] == 0
    assert all_candidates == []
    assert all_trades == []


def test_generate_candidates_dry_run_scores_without_opening_no_trade(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    now = datetime(2026, 7, 2, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.200000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="window-dry-run-no",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=now + timedelta(minutes=90),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-WINDOW-DRY-NO",
            ticker="KXMLBGAME-WINDOW-DRY-NO-PIT",
            title="Will Pittsburgh win?",
            status="open",
            no_ask=Decimal("0.4000"),
            market_price_updated_at=now,
        )
        session.add_all([game, market])
        _add_candidate_mapping(
            session,
            game,
            market,
            mapping_status="confirmed",
            market_family="full_game_winner",
            market_type="full_game_winner",
            selection_code="PIT",
            settlement_rule_status="paper_supported",
        )
        session.commit()

        result = candidates.generate_candidates(
            session,
            target_date=date(2026, 7, 2),
            min_time_to_start_minutes=45,
            max_time_to_start_minutes=180,
            dry_run_candidates_only=True,
        )
        no_candidate = session.scalar(select(ModelCandidate).where(ModelCandidate.contract_side == "no"))
        yes_candidate = session.scalar(select(ModelCandidate).where(ModelCandidate.contract_side == "yes"))
        trades = list(session.scalars(select(PaperTrade)))

    assert result["dry_run_candidates_only"] is True
    assert result["candidates"] == 2
    assert result["paper_trades"] == 0
    assert result["snapshot_id"] is None
    assert trades == []
    assert no_candidate is not None
    assert no_candidate.contract_side == "no"
    assert no_candidate.decision == "candidate_only_dry_run"
    assert no_candidate.training_eligible is False
    assert no_candidate.training_exclusion_reason == "dry_run_candidates_only"
    assert yes_candidate is not None
    assert yes_candidate.decision == "no_trade_missing_price"


def test_generate_candidates_window_uses_global_daily_trade_caps(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_SLATE", "1")
    get_settings.cache_clear()
    now = datetime(2026, 7, 2, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.900000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        existing_trade = PaperTrade(
            paper_trading_epoch_id=epoch_id,
            market_ticker="KXMLBGAME-EXISTING-CAP-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("1.0000"),
            quantity=1,
            entry_time=now - timedelta(hours=1),
            status="settled",
            market_family="full_game_winner",
        )
        game = MlbGame(
            external_game_id="window-cap-in",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=now + timedelta(minutes=60),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-WINDOW-CAP",
            ticker="KXMLBGAME-WINDOW-CAP-PIT",
            title="Will Pittsburgh win?",
            status="open",
            implied_yes_ask=Decimal("0.4000"),
            market_price_updated_at=now,
        )
        session.add_all([existing_trade, game, market])
        _add_candidate_mapping(
            session,
            game,
            market,
            mapping_status="confirmed",
            market_family="full_game_winner",
            market_type="full_game_winner",
            selection_code="PIT",
            settlement_rule_status="paper_supported",
        )
        session.commit()

        result = candidates.generate_candidates(
            session,
            target_date=date(2026, 7, 2),
            min_time_to_start_minutes=45,
            max_time_to_start_minutes=180,
        )
        candidate = session.scalar(select(ModelCandidate))
        trades = list(session.scalars(select(PaperTrade).where(PaperTrade.market_ticker == "KXMLBGAME-WINDOW-CAP-PIT")))

    assert result["games_in_window"] == 1
    assert result["candidates"] == 1
    assert result["paper_trades"] == 0
    assert result["cap_counts"]["no_trade_slate_cap"] == 1
    assert trades == []
    assert candidate is not None
    assert candidate.decision == "no_trade_slate_cap"


def test_job_runner_forwards_candidate_sweep_window_args(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class DummySession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_run_job(session, **kwargs):
        captured.update(kwargs)
        return {"status": "succeeded"}

    monkeypatch.setattr(job_runner, "get_session_factory", lambda: lambda: DummySession())
    monkeypatch.setattr(job_runner, "run_job", fake_run_job)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--job",
            "candidate-sweep",
            "--target-date",
            "2026-07-02",
            "--min-time-to-start-minutes",
            "45",
            "--max-time-to-start-minutes",
            "180",
            "--sweep-label",
            "pregame_window",
            "--dry-run-candidates-only",
            "false",
        ],
    )

    job_runner.main()

    assert captured["job_name"] == "candidate-sweep"
    assert captured["target_date"] == date(2026, 7, 2)
    assert captured["min_time_to_start_minutes"] == 45
    assert captured["max_time_to_start_minutes"] == 180
    assert captured["sweep_label"] == "pregame_window"
    assert captured["dry_run_candidates_only"] is False


def test_generate_candidates_preserves_traded_candidate_snapshot(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="preserve-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            home_abbreviation="NYY",
            away_abbreviation="BOS",
            scheduled_start=now + timedelta(hours=25),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-PRESERVE",
            ticker="KXMLBGAME-PRESERVE-NYY",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=now + timedelta(hours=25),
            implied_yes_ask=Decimal("0.4000"),
        )
        session.add_all([game, market])
        _add_candidate_mapping(session, game, market)
        session.commit()

        first_result = candidates.generate_candidates(session, target_date=date(2026, 7, 2))
        trade = session.scalar(select(PaperTrade).where(PaperTrade.market_ticker == "KXMLBGAME-PRESERVE-NYY"))
        assert trade is not None
        traded_candidate = session.get(ModelCandidate, trade.candidate_id)
        assert traded_candidate is not None
        assert first_result["paper_trades"] == 1
        assert traded_candidate.executable_price == Decimal("0.4000")

        market.implied_yes_ask = Decimal("0.3500")
        market.market_price_updated_at = now
        session.add(market)
        session.commit()

        second_result = candidates.generate_candidates(session, target_date=date(2026, 7, 2))
        all_candidates = list(session.scalars(select(ModelCandidate).order_by(ModelCandidate.id.asc())))
        all_trades = list(session.scalars(select(PaperTrade)))

    assert second_result["paper_trades"] == 0
    assert len(all_trades) == 1
    assert len(all_candidates) == 2
    assert all_candidates[0].id == traded_candidate.id
    assert all_candidates[0].executable_price == Decimal("0.4000")
    assert all_candidates[1].executable_price == Decimal("0.3500")
    assert all_candidates[1].decision == "candidate_only_existing_trade"


def test_generate_candidates_records_quality_gate_counterfactual(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_OBSERVATION_MIN_DATA_QUALITY", "0.55")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(
        candidates,
        "score_mature_candidate",
        lambda *_args, **_kwargs: _fixed_model_score("0.800000", data_quality="0.5000"),
    )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        game = MlbGame(
            external_game_id="quality-gate-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 2, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-QUALITY-GATE",
            ticker="KXMLBGAME-QUALITY-PIT",
            title="Will the Pittsburgh Pirates win?",
            status="open",
            implied_yes_ask=Decimal("0.4000"),
            market_price_updated_at=now,
        )
        session.add_all([game, market])
        _add_candidate_mapping(session, game, market, market_family="full_game_winner", market_type="full_game_winner")
        session.commit()

        result = candidates.generate_candidates(session, target_date=date(2026, 7, 2))
        candidate = session.scalar(select(ModelCandidate).order_by(ModelCandidate.id.desc()).limit(1))

    assert result["paper_trades"] == 0
    assert result["blocked_by_quality_only"] == 1
    assert result["would_pass_ev_if_quality_allowed"] == 1
    assert result["ev_edge_pass_but_quality_fail"] == 1
    assert candidate is not None
    assert candidate.decision == "no_trade_low_data_quality"
    assert candidate.gate_data_quality_ok is False
    assert candidate.counterfactual_trade_eligible_before_quality is True


def test_generate_candidates_uses_candidate_stage_quality_without_lowering_threshold(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_OBSERVATION_MIN_DATA_QUALITY", "0.55")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(
        candidates,
        "score_mature_candidate",
        lambda *_args, **_kwargs: _fixed_model_score("0.800000", data_quality="0.4500"),
    )

    core_modules = set(candidates.PAPER_OBSERVATION_QUALITY_WEIGHTS)

    def cached_feature_snapshot(*_args, **_kwargs):
        module_scores = {
            module: (0.9 if module in core_modules and module != "market_context" else 0.0)
            for module in features.CORE_MODULES
        }
        source_statuses = {
            module: ("available" if module in core_modules and module != "market_context" else "missing")
            for module in features.CORE_MODULES
        }
        return {
            "feature_version": features.FEATURE_VERSION,
            "data_quality": 0.45,
            "data_quality_summary": {
                "score": 0.45,
                "module_scores": module_scores,
                "data_quality_reason": [],
                "source_statuses": source_statuses,
                "context": "pregame",
            },
            "source_statuses": source_statuses,
            "market_context": {"source_status": "missing", "completeness": 0.0},
        }

    monkeypatch.setattr(candidates, "build_feature_snapshot", cached_feature_snapshot)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        game = MlbGame(
            external_game_id="candidate-stage-quality-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 2, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-CANDIDATE-STAGE-QUALITY",
            ticker="KXMLBGAME-CANDIDATE-STAGE-QUALITY-PIT",
            title="Will the Pittsburgh Pirates win?",
            status="open",
            implied_yes_ask=Decimal("0.4000"),
            market_price_updated_at=now,
            market_family="full_game_winner",
            market_type="full_game_winner",
            selection_code="PIT",
            settlement_rule_status="paper_supported",
        )
        session.add_all([game, market])
        _add_candidate_mapping(
            session,
            game,
            market,
            mapping_status="confirmed",
            market_family="full_game_winner",
            market_type="full_game_winner",
            selection_code="PIT",
            settlement_rule_status="paper_supported",
        )
        session.commit()

        result = candidates.generate_candidates(session, target_date=date(2026, 7, 2))
        candidate = session.scalar(select(ModelCandidate).order_by(ModelCandidate.id.desc()).limit(1))
        trade = session.scalar(select(PaperTrade))

    settings = get_settings()
    assert settings.paper_observation_min_data_quality == Decimal("0.55")
    assert result["paper_trades"] == 1
    assert result["raw_feature_snapshot_data_quality_avg"] == 0.45
    assert result["paper_observation_data_quality_avg"] is not None
    assert result["paper_observation_data_quality_avg"] >= 0.55
    assert result["candidate_stage_market_context_status_counts"] == {"available": 1}
    assert candidate is not None
    assert trade is not None
    assert candidate.data_quality is not None
    assert candidate.data_quality >= Decimal("0.55")
    assert candidate.features["raw_feature_snapshot_data_quality"] == 0.45
    assert candidate.features["paper_observation_data_quality"] >= 0.55
    quality = candidate.gate_diagnostics["quality_decomposition"]
    assert quality["candidate_stage_market_context"]["source_status"] == "available"
    assert quality["paper_observation"]["module_status"]["injuries"] == "missing"
    assert quality["paper_observation"]["module_role"]["injuries"] == "optional_structural"
    assert quality["paper_observation"]["quality_weight_by_module"]["injuries"] == 0.0


def test_raw_model_quality_does_not_bypass_paper_observation_profile(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_OBSERVATION_MIN_DATA_QUALITY", "0.55")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(
        candidates,
        "score_mature_candidate",
        lambda *_args, **_kwargs: _fixed_model_score("0.900000", data_quality="1.0000"),
    )

    def low_quality_cached_feature_snapshot(*_args, **_kwargs):
        module_scores = {module: 0.0 for module in features.CORE_MODULES}
        source_statuses = {module: "missing" for module in features.CORE_MODULES}
        return {
            "feature_version": features.FEATURE_VERSION,
            "data_quality": 1.0,
            "data_quality_summary": {
                "score": 1.0,
                "module_scores": module_scores,
                "data_quality_reason": [],
                "source_statuses": source_statuses,
                "context": "pregame",
            },
            "source_statuses": source_statuses,
            "market_context": {"source_status": "missing", "completeness": 0.0},
        }

    monkeypatch.setattr(candidates, "build_feature_snapshot", low_quality_cached_feature_snapshot)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        game = MlbGame(
            external_game_id="raw-quality-bypass-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 2, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-RAW-QUALITY-BYPASS",
            ticker="KXMLBGAME-RAW-QUALITY-BYPASS-PIT",
            title="Will the Pittsburgh Pirates win?",
            status="open",
            implied_yes_ask=Decimal("0.1000"),
            market_price_updated_at=now,
            market_family="full_game_winner",
            market_type="full_game_winner",
            selection_code="PIT",
            settlement_rule_status="paper_supported",
        )
        session.add_all([game, market])
        _add_candidate_mapping(
            session,
            game,
            market,
            mapping_status="confirmed",
            market_family="full_game_winner",
            market_type="full_game_winner",
            selection_code="PIT",
            settlement_rule_status="paper_supported",
        )
        session.commit()

        result = candidates.generate_candidates(session, target_date=date(2026, 7, 2))
        candidate = session.scalar(select(ModelCandidate).order_by(ModelCandidate.id.desc()).limit(1))
        trade = session.scalar(select(PaperTrade))

    assert result["paper_trades"] == 0
    assert result["blocked_by_quality_only"] == 1
    assert candidate is not None
    assert candidate.decision == "no_trade_low_data_quality"
    assert candidate.gate_data_quality_ok is False
    assert candidate.data_quality is not None
    assert candidate.data_quality < Decimal("0.55")
    assert candidate.gate_diagnostics["quality_decomposition"]["model_score_data_quality"] == 1.0
    assert candidate.gate_diagnostics["paper_observation_data_quality"] < 0.55
    assert trade is None


def test_ev_edge_diagnostics_dedupe_overlapping_candidate_surfaces() -> None:
    shared_gate_diagnostics = {
        "gate_gross_ev_positive": True,
        "gate_net_ev_ok": True,
        "gate_probability_edge_ok": True,
        "counterfactual_trade_eligible_before_quality": True,
        "blocked_by_quality_only": True,
    }
    candidates_under_test = [
        ModelCandidate(
            id=1,
            mlb_game_id=10,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="no_trade_low_data_quality",
            market_family="full_game_winner",
            inning_scope="full_game",
            contract_side="yes",
            net_expected_value=Decimal("0.120000"),
            probability_edge=Decimal("0.150000"),
            data_quality=Decimal("0.4500"),
            gate_diagnostics=shared_gate_diagnostics,
        ),
        ModelCandidate(
            id=2,
            mlb_game_id=10,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="no_trade_low_data_quality",
            market_family="full_game_winner",
            inning_scope="full_game",
            contract_side="no",
            net_expected_value=Decimal("0.100000"),
            probability_edge=Decimal("0.140000"),
            data_quality=Decimal("0.4400"),
            gate_diagnostics=shared_gate_diagnostics,
        ),
    ]

    diagnostics = candidates._opportunity_diagnostics(candidates_under_test)

    assert diagnostics["ev_and_edge_pass_count"] == 2
    assert diagnostics["pre_quality_trade_eligible_count"] == 2
    assert diagnostics["unique_game_scope_family_side_count"] == 2
    assert diagnostics["deduped_ev_edge_pass_count_by_game_scope_family"] == 1
    assert diagnostics["deduped_pre_quality_trade_eligible_count_by_game_scope_family"] == 1


def test_fixed_risk_sizing_uses_active_epoch_bankroll(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.800000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        game = MlbGame(
            external_game_id="sizing-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 2, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-SIZING",
            ticker="KXMLBGAME-SIZING-PIT",
            title="Will the Pittsburgh Pirates win?",
            status="open",
            implied_yes_ask=Decimal("0.4000"),
            market_price_updated_at=now,
        )
        session.add_all([game, market])
        _add_candidate_mapping(session, game, market, market_family="full_game_winner", market_type="full_game_winner")
        session.commit()

        result = candidates.generate_candidates(session, target_date=date(2026, 7, 2))
        trade = session.scalar(select(PaperTrade).where(PaperTrade.market_ticker == "KXMLBGAME-SIZING-PIT"))
        candidate = session.get(ModelCandidate, trade.candidate_id) if trade else None

    assert result["paper_trades"] == 1
    assert trade is not None
    assert candidate is not None
    assert trade.quantity > 1
    assert trade.bankroll_at_entry == Decimal("500.00")
    assert trade.risk_pct == Decimal("0.025000")
    assert trade.estimated_total_cost is not None
    assert candidate.one_contract_expected_value is not None
    assert candidate.sized_expected_value is not None


def test_generate_candidates_blocks_excess_daily_risk(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    monkeypatch.setenv("PAPER_MAX_DAILY_NEW_RISK_PCT", "0.025")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.900000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        for index, team in enumerate(("PIT", "SEA"), start=1):
            game = MlbGame(
                external_game_id=f"daily-risk-{index}",
                home_team="Pittsburgh Pirates" if team == "PIT" else "Seattle Mariners",
                away_team="Boston Red Sox",
                home_abbreviation=team,
                away_abbreviation="BOS",
                scheduled_start=datetime(2026, 7, 1, 23, index, tzinfo=UTC),
                status="scheduled",
            )
            market = KalshiMarket(
                kalshi_market_id=f"KX-DAILY-RISK-{index}",
                ticker=f"KXMLBGAME-DAILY-RISK-{team}",
                title=f"Will {team} win?",
                status="open",
                yes_ask=Decimal("0.5000"),
                market_price_updated_at=now,
            )
            session.add_all([game, market])
            _add_candidate_mapping(session, game, market, market_family="full_game_winner", market_type="full_game_winner")
        session.commit()

        result = candidates.generate_candidates(session)
        trades = list(session.scalars(select(PaperTrade)))
        rejected = list(
            session.scalars(select(ModelCandidate).where(ModelCandidate.decision == "no_trade_daily_risk_cap"))
        )

    assert result["paper_trades"] == 1
    assert result["cap_counts"]["no_trade_daily_risk_cap"] == 1
    assert len(trades) == 1
    assert len(rejected) == 1


def test_paper_portfolio_charges_open_trade_fee_estimates() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        trade = PaperTrade(
            paper_trading_epoch_id=epoch.id,
            market_ticker="KXMLBGAME-FEE-OPEN-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.5000"),
            quantity=2,
            entry_time=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            status="open",
            total_fee_estimate=Decimal("0.030000"),
        )
        session.add(trade)
        session.commit()

        totals = calculate_paper_portfolio(session, epoch=epoch)

    assert totals.open_cost == Decimal("0.83")
    assert totals.cash_balance == Decimal("499.17")
    assert totals.open_mark_value == Decimal("1.00")
    assert totals.portfolio_value == Decimal("500.17")


def test_job_lock_skips_existing_running_job(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 7, 2)
    monkeypatch.setattr(job_runs, "utc_now", lambda: datetime(2026, 7, 2, 12, 1, tzinfo=UTC))

    with Session(engine) as session:
        epoch = get_or_create_active_paper_epoch(session)
        running = JobRun(
            job_name="price-refresh",
            job_type="paper_ops",
            target_date=target,
            paper_trading_epoch_id=epoch.id,
            status="running",
            started_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
            heartbeat_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
            lock_key="price-refresh:global",
            triggered_by="cron",
        )
        session.add(running)
        session.commit()
        running_id = running.id

        result = run_job(session, job_name="price-refresh", target_date=target, triggered_by="api")
        skipped = session.scalar(select(JobRun).where(JobRun.status == "skipped"))

    assert result["status"] == "skipped"
    assert result["skipped_reason"] == "skipped_existing_run"
    assert skipped is not None
    assert skipped.result["existing_run_id"] == running_id


def test_price_refresh_lock_key_is_date_insensitive(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 7, 2)
    monkeypatch.setattr(job_runs, "utc_now", lambda: datetime(2026, 7, 2, 12, 1, tzinfo=UTC))

    with Session(engine) as session:
        epoch = get_or_create_active_paper_epoch(session)
        running = JobRun(
            job_name="price-refresh",
            job_type="paper_ops",
            target_date=target,
            paper_trading_epoch_id=epoch.id,
            status="running",
            started_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
            heartbeat_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
            lock_key="price-refresh:global",
            triggered_by="cron",
        )
        session.add(running)
        session.commit()
        running_id = running.id

        result = run_job(session, job_name="price-refresh", target_date=None, triggered_by="api")
        skipped = session.scalar(select(JobRun).where(JobRun.status == "skipped"))

    assert result["status"] == "skipped"
    assert result["skipped_reason"] == "skipped_existing_run"
    assert skipped is not None
    assert skipped.lock_key == "price-refresh:global"
    assert skipped.target_date is None
    assert skipped.result["existing_run_id"] == running_id


def test_default_date_scoped_job_lock_uses_today(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 7, 2)
    monkeypatch.setattr(job_runs, "today_eastern", lambda: target)
    monkeypatch.setattr(job_runs, "utc_now", lambda: datetime(2026, 7, 2, 12, 1, tzinfo=UTC))

    def fake_execute_steps(*_args, **_kwargs):
        return {"should_not_run": True}

    monkeypatch.setattr(job_runs, "_execute_job_steps", fake_execute_steps)

    with Session(engine) as session:
        epoch = get_or_create_active_paper_epoch(session)
        running = JobRun(
            job_name="daily-setup",
            job_type="paper_ops",
            target_date=target,
            paper_trading_epoch_id=epoch.id,
            status="running",
            started_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
            heartbeat_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
            lock_key="daily-setup:2026-07-02",
            triggered_by="cron",
        )
        session.add(running)
        session.commit()
        running_id = running.id

        result = run_job(session, job_name="daily-setup", target_date=None, triggered_by="api")
        skipped = session.scalar(select(JobRun).where(JobRun.status == "skipped"))

    assert result["status"] == "skipped"
    assert result["skipped_reason"] == "skipped_existing_run"
    assert skipped is not None
    assert skipped.lock_key == "daily-setup:2026-07-02"
    assert skipped.target_date == target
    assert skipped.result["existing_run_id"] == running_id


def test_candidate_sweep_refreshes_marks_after_candidate_engine(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 7, 2)
    call_order: list[str] = []

    def record(name: str, result: object) -> object:
        call_order.append(name)
        return result

    monkeypatch.setattr(job_runs, "sync_schedule", lambda *_args, **_kwargs: record("schedule", 0))
    monkeypatch.setattr(
        job_runs,
        "sync_mlb_features",
        lambda *_args, **_kwargs: pytest.fail("candidate-sweep must not run full feature sync"),
    )
    monkeypatch.setattr(
        job_runs,
        "sync_market_family_mappings",
        lambda *_args, **_kwargs: record("market_family_mappings", {}),
    )
    monkeypatch.setattr(
        job_runs,
        "sync_mlb_pregame_context",
        lambda *_args, **_kwargs: record(
            "pregame_context_refresh",
            {"validation_status": "ok", "feature_sync_mode": features.PREGAME_CONTEXT_SYNC_MODE},
        ),
    )
    monkeypatch.setattr(job_runs, "generate_candidates", lambda *_args, **_kwargs: record("candidate_engine", {}))
    monkeypatch.setattr(job_runs, "refresh_open_position_prices", lambda *_args, **_kwargs: record("price_refresh", {}))
    monkeypatch.setattr(
        job_runs,
        "create_balance_snapshot",
        lambda *_args, **_kwargs: record("balance_snapshot", SimpleNamespace(id=123)),
    )

    with Session(engine) as session:
        run = JobRun(
            job_name="candidate-sweep",
            job_type="paper_ops",
            target_date=target,
            status="running",
            started_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
            lock_key="candidate-sweep:2026-07-02",
            triggered_by="api",
            steps=[],
        )
        session.add(
            MlbFeatureSnapshot(
                mlb_game_id=1,
                target_date=target,
                source=features.FEATURE_VERSION,
                captured_at=datetime(2026, 7, 2, 11, 45, tzinfo=UTC),
                data_quality=Decimal("0.7500"),
                source_statuses={},
                features={},
            )
        )
        result = job_runs._execute_job_steps(session, run, "candidate-sweep", target)

    assert call_order == [
        "schedule",
        "pregame_context_refresh",
        "market_family_mappings",
        "candidate_engine",
        "price_refresh",
        "balance_snapshot",
    ]
    assert result["feature_sync"] == {
        "skipped": True,
        "feature_sync_mode": "cache_only",
        "feature_sync_skipped": True,
        "heavy_feature_sync_skipped": True,
        "reason": "candidate_sweep_uses_cached_feature_snapshots",
    }
    assert result["cached_features"]["status"] == "low_quality_missing_cached_features"
    assert result["cached_features"]["snapshot_count"] == 1
    assert result["balance_snapshot"] == {"snapshot_id": 123}


def test_candidate_sweep_cache_only_does_not_call_advanced_public_sources(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 7, 2)

    monkeypatch.setattr(job_runs, "sync_schedule", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        job_runs,
        "sync_mlb_features",
        lambda *_args, **_kwargs: pytest.fail("candidate-sweep must not run full feature sync"),
    )
    monkeypatch.setattr(
        features.pybaseball_client,
        "get_batting_stats",
        lambda *_args, **_kwargs: pytest.fail("candidate-sweep must not call FanGraphs batting_stats"),
    )
    monkeypatch.setattr(
        features.pybaseball_client,
        "get_pitching_stats",
        lambda *_args, **_kwargs: pytest.fail("candidate-sweep must not call FanGraphs pitching_stats"),
    )
    monkeypatch.setattr(
        features.pybaseball_client,
        "get_statcast_range",
        lambda *_args, **_kwargs: pytest.fail("candidate-sweep must not call Statcast/Savant team feed"),
    )
    monkeypatch.setattr(
        features.pybaseball_client,
        "get_pitcher_statcast_range",
        lambda *_args, **_kwargs: pytest.fail("candidate-sweep must not call Statcast/Savant pitcher feed"),
    )
    monkeypatch.setattr(
        features,
        "_fetch_open_meteo",
        lambda *_args, **_kwargs: pytest.fail("candidate-sweep must not call Open-Meteo"),
    )
    monkeypatch.setattr(
        job_runs,
        "sync_mlb_pregame_context",
        lambda *_args, **_kwargs: {
            "validation_status": "ok",
            "feature_sync_mode": features.PREGAME_CONTEXT_SYNC_MODE,
        },
    )
    monkeypatch.setattr(job_runs, "sync_market_family_mappings", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(job_runs, "generate_candidates", lambda *_args, **_kwargs: {"status": "completed"})
    monkeypatch.setattr(job_runs, "refresh_open_position_prices", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(job_runs, "create_balance_snapshot", lambda *_args, **_kwargs: SimpleNamespace(id=124))

    with Session(engine) as session:
        run = JobRun(
            job_name="candidate-sweep",
            job_type="paper_ops",
            target_date=target,
            status="running",
            started_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
            lock_key="candidate-sweep:2026-07-02",
            triggered_by="api",
            steps=[],
        )
        session.add(
            MlbFeatureSnapshot(
                mlb_game_id=1,
                target_date=target,
                source=features.FEATURE_VERSION,
                captured_at=datetime(2026, 7, 2, 11, 45, tzinfo=UTC),
                data_quality=Decimal("0.7500"),
                source_statuses={},
                features={},
            )
        )
        result = job_runs._execute_job_steps(session, run, "candidate-sweep", target)

    assert result["feature_sync_mode"] == "cache_only"
    assert result["feature_sync_skipped"] is True
    assert result["starter_refresh"]["status"] == "ok"
    assert result["starter_refresh"]["feature_sync_mode"] == "starter_refresh_lightweight"
    assert result["starter_refresh"]["heavy_feature_sync_skipped"] is True


def test_candidate_sweep_dry_run_skips_price_refresh_and_snapshot(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 7, 2)
    call_order: list[str] = []

    def record(name: str, result: object) -> object:
        call_order.append(name)
        return result

    monkeypatch.setattr(job_runs, "sync_schedule", lambda *_args, **_kwargs: record("schedule", 0))
    monkeypatch.setattr(
        job_runs,
        "sync_mlb_features",
        lambda *_args, **_kwargs: pytest.fail("candidate-sweep must not run full feature sync"),
    )
    monkeypatch.setattr(
        job_runs,
        "sync_market_family_mappings",
        lambda *_args, **_kwargs: record("market_family_mappings", {}),
    )
    monkeypatch.setattr(
        job_runs,
        "sync_mlb_pregame_context",
        lambda *_args, **_kwargs: record(
            "pregame_context_refresh",
            {"validation_status": "ok", "feature_sync_mode": features.PREGAME_CONTEXT_SYNC_MODE},
        ),
    )
    captured_generate_kwargs: dict[str, object] = {}

    def fake_generate_candidates(*_args, **kwargs):
        captured_generate_kwargs.update(kwargs)
        return record("candidate_engine", {"status": "completed", "dry_run_candidates_only": True})

    monkeypatch.setattr(
        job_runs,
        "generate_candidates",
        fake_generate_candidates,
    )
    monkeypatch.setattr(
        job_runs,
        "refresh_open_position_prices",
        lambda *_args, **_kwargs: pytest.fail("dry-run candidate sweep must not refresh open position marks"),
    )
    monkeypatch.setattr(
        job_runs,
        "create_balance_snapshot",
        lambda *_args, **_kwargs: pytest.fail("dry-run candidate sweep must not create a balance snapshot"),
    )

    with Session(engine) as session:
        run = JobRun(
            job_name="candidate-sweep",
            job_type="paper_ops",
            target_date=target,
            status="running",
            started_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
            lock_key="candidate-sweep:2026-07-02",
            triggered_by="api",
            steps=[],
        )
        session.add(
            MlbFeatureSnapshot(
                mlb_game_id=1,
                target_date=target,
                source=features.FEATURE_VERSION,
                captured_at=datetime(2026, 7, 2, 11, 45, tzinfo=UTC),
                data_quality=Decimal("0.7500"),
                source_statuses={},
                features={},
            )
        )
        result = job_runs._execute_job_steps(
            session,
            run,
            "candidate-sweep",
            target,
            min_time_to_start_minutes=45,
            max_time_to_start_minutes=180,
            sweep_label="rolling_pregame_window",
            dry_run_candidates_only=True,
        )

    assert call_order == ["schedule", "pregame_context_refresh", "market_family_mappings", "candidate_engine"]
    assert captured_generate_kwargs == {
        "min_time_to_start_minutes": 45,
        "max_time_to_start_minutes": 180,
        "sweep_label": "rolling_pregame_window",
        "dry_run_candidates_only": True,
    }
    assert result["feature_sync_mode"] == "cache_only"
    assert result["feature_sync_skipped"] is True
    assert result["price_refresh"] == {"skipped": True, "reason": "dry_run_candidates_only"}
    assert result["balance_snapshot"] == {
        "skipped": True,
        "reason": "dry_run_candidates_only",
        "snapshot_id": None,
    }


def test_candidate_sweep_completes_cleanly_when_cached_feature_snapshots_missing(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 7, 2)
    call_order: list[str] = []

    def record(name: str, result: object) -> object:
        call_order.append(name)
        return result

    monkeypatch.setattr(job_runs, "sync_schedule", lambda *_args, **_kwargs: record("schedule", 0))
    monkeypatch.setattr(
        job_runs,
        "sync_mlb_features",
        lambda *_args, **_kwargs: pytest.fail("candidate-sweep must not run full feature sync"),
    )
    monkeypatch.setattr(
        job_runs,
        "sync_market_family_mappings",
        lambda *_args, **_kwargs: pytest.fail("missing cached features should skip mapping sync"),
    )
    monkeypatch.setattr(
        job_runs,
        "sync_mlb_pregame_context",
        lambda *_args, **_kwargs: record(
            "pregame_context_refresh",
            {"validation_status": "ok", "feature_sync_mode": features.PREGAME_CONTEXT_SYNC_MODE},
        ),
    )
    monkeypatch.setattr(
        job_runs,
        "generate_candidates",
        lambda *_args, **_kwargs: pytest.fail("missing cached features should skip candidate generation"),
    )
    monkeypatch.setattr(job_runs, "refresh_open_position_prices", lambda *_args, **_kwargs: record("price_refresh", {}))
    monkeypatch.setattr(
        job_runs,
        "create_balance_snapshot",
        lambda *_args, **_kwargs: record("balance_snapshot", SimpleNamespace(id=456)),
    )

    with Session(engine) as session:
        run = JobRun(
            job_name="candidate-sweep",
            job_type="paper_ops",
            target_date=target,
            status="running",
            started_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
            lock_key="candidate-sweep:2026-07-02",
            triggered_by="api",
            steps=[],
        )
        result = job_runs._execute_job_steps(
            session,
            run,
            "candidate-sweep",
            target,
            min_time_to_start_minutes=45,
            max_time_to_start_minutes=180,
            sweep_label="rolling_pregame_window",
        )

    assert call_order == ["schedule", "pregame_context_refresh", "price_refresh", "balance_snapshot"]
    assert result["feature_sync_mode"] == "cache_only"
    assert result["feature_sync_skipped"] is True
    assert result["cached_features"]["status"] == "missing_cached_feature_snapshots"
    assert result["candidate_engine"]["status"] == "skipped_missing_cached_features"
    assert result["candidate_engine"]["zero_trade_reason"] == "no_candidates_missing_feature_snapshots"
    assert result["candidate_sweep_window"] == {
        "sweep_label": "rolling_pregame_window",
        "sweep_window_enabled": True,
        "min_time_to_start_minutes": 45,
        "max_time_to_start_minutes": 180,
        "dry_run_candidates_only": False,
        "status": "skipped_missing_cached_features",
    }
    assert result["warnings"] == [
        "no_candidates_missing_feature_snapshots: run daily-setup or feature sync before candidate-sweep."
    ]
    assert result["balance_snapshot"] == {"snapshot_id": 456}


def test_candidate_sweep_continues_when_lightweight_starter_refresh_fails(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 7, 2)
    call_order: list[str] = []

    def record(name: str, result: object) -> object:
        call_order.append(name)
        return result

    def fail_pregame_context_refresh(*_args, **_kwargs):
        raise TimeoutError("mlb starter timeout")

    monkeypatch.setattr(job_runs, "sync_schedule", lambda *_args, **_kwargs: record("schedule", 0))
    monkeypatch.setattr(
        job_runs,
        "sync_mlb_features",
        lambda *_args, **_kwargs: pytest.fail("candidate-sweep must not run full feature sync"),
    )
    monkeypatch.setattr(job_runs, "sync_mlb_pregame_context", fail_pregame_context_refresh)
    monkeypatch.setattr(
        job_runs,
        "sync_market_family_mappings",
        lambda *_args, **_kwargs: record("market_family_mappings", {}),
    )
    monkeypatch.setattr(job_runs, "generate_candidates", lambda *_args, **_kwargs: record("candidate_engine", {}))
    monkeypatch.setattr(job_runs, "refresh_open_position_prices", lambda *_args, **_kwargs: record("price_refresh", {}))
    monkeypatch.setattr(
        job_runs,
        "create_balance_snapshot",
        lambda *_args, **_kwargs: record("balance_snapshot", SimpleNamespace(id=457)),
    )

    with Session(engine) as session:
        run = JobRun(
            job_name="candidate-sweep",
            job_type="paper_ops",
            target_date=target,
            status="running",
            started_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
            lock_key="candidate-sweep:2026-07-02",
            triggered_by="api",
            steps=[],
        )
        session.add(
            MlbFeatureSnapshot(
                mlb_game_id=1,
                target_date=target,
                source=features.FEATURE_VERSION,
                captured_at=datetime(2026, 7, 2, 11, 45, tzinfo=UTC),
                data_quality=Decimal("0.7500"),
                source_statuses={"starter_identity": {"home": "missing", "away": "missing"}},
                features={},
            )
        )
        result = job_runs._execute_job_steps(session, run, "candidate-sweep", target)

    assert call_order == ["schedule", "market_family_mappings", "candidate_engine", "price_refresh", "balance_snapshot"]
    assert result["feature_sync_mode"] == "cache_only"
    assert result["pregame_context_refresh"]["status"] == "degraded_pregame_context_refresh_failed"
    assert result["pregame_context_refresh"]["heavy_feature_sync_skipped"] is True
    assert result["starter_refresh"]["status"] == "degraded_pregame_context_refresh_failed"
    assert result["starter_refresh"]["heavy_feature_sync_skipped"] is True
    assert result["starter_refresh"]["error"]["type"] == "TimeoutError"


def test_safe_starter_refresh_rolls_back_partial_refresh_writes(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 7, 2)

    def fail_after_partial_write(session, *_args, **_kwargs):
        session.add(
            MlbFeatureSnapshot(
                mlb_game_id=42,
                target_date=target,
                source="partial-starter-refresh",
                captured_at=datetime(2026, 7, 2, 11, 45, tzinfo=UTC),
                data_quality=Decimal("0.1000"),
                source_statuses={},
                features={},
            )
        )
        session.flush()
        raise RuntimeError("starter refresh write failed")

    monkeypatch.setattr(job_runs, "sync_mlb_starters", fail_after_partial_write)

    with Session(engine) as session:
        result = job_runs._safe_starter_refresh(session, target)
        snapshots = session.scalars(select(MlbFeatureSnapshot)).all()
        session.add(
            MlbFeatureSnapshot(
                mlb_game_id=43,
                target_date=target,
                source="post-failure-session-still-usable",
                captured_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
                data_quality=Decimal("0.1000"),
                source_statuses={},
                features={},
            )
        )
        session.flush()

    assert result["status"] == "degraded_starter_refresh_failed"
    assert result["error"]["type"] == "RuntimeError"
    assert snapshots == []


def test_daily_setup_still_runs_full_feature_sync(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 7, 2)
    call_order: list[str] = []

    def record(name: str, result: object) -> object:
        call_order.append(name)
        return result

    monkeypatch.setattr(job_runs, "sync_schedule", lambda *_args, **_kwargs: record("schedule", 0))
    monkeypatch.setattr(job_runs, "sync_results", lambda *_args, **_kwargs: record("results", {}))
    monkeypatch.setattr(job_runs, "sync_mlb_features", lambda *_args, **_kwargs: record("features", {"status": "ok"}))
    monkeypatch.setattr(
        job_runs,
        "run_market_family_discovery",
        lambda *_args, **_kwargs: record("market_family_discovery", {}),
    )
    monkeypatch.setattr(
        job_runs,
        "sync_market_family_mappings",
        lambda *_args, **_kwargs: record("market_family_mappings", {}),
    )
    monkeypatch.setattr(job_runs, "refresh_open_position_prices", lambda *_args, **_kwargs: record("price_refresh", {}))
    monkeypatch.setattr(
        job_runs,
        "create_balance_snapshot",
        lambda *_args, **_kwargs: record("balance_snapshot", SimpleNamespace(id=789)),
    )
    monkeypatch.setattr(job_runs, "feature_coverage", lambda *_args, **_kwargs: record("feature_coverage", {}))

    with Session(engine) as session:
        run = JobRun(
            job_name="daily-setup",
            job_type="paper_ops",
            target_date=target,
            status="running",
            started_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
            lock_key="daily-setup:2026-07-02",
            triggered_by="api",
            steps=[],
        )
        result = job_runs._execute_job_steps(session, run, "daily-setup", target)

    assert call_order == [
        "schedule",
        "results",
        "results",
        "features",
        "market_family_discovery",
        "market_family_mappings",
        "price_refresh",
        "balance_snapshot",
        "feature_coverage",
    ]
    assert result["features"] == {"status": "ok"}


def test_governance_job_scopes_resolved_samples_to_active_epoch() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 7, 2)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session)
        archived = PaperTradingEpoch(
            epoch_key="pre_pr3d_validation_test",
            display_name="Pre-PR3d validation",
            status="archived",
            mode="paper",
            starting_balance=Decimal("500.00"),
            started_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            archived_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
        )
        game = MlbGame(
            external_game_id="governance-epoch-scope",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 2, 23, 0, tzinfo=UTC),
            status="Final",
        )
        session.add_all([archived, game])
        session.flush()
        active_id = active.id
        archived_id = archived.id

        for epoch_id, outcome in ((active_id, "win"), (archived_id, "loss")):
            session.add(
                ModelCandidate(
                    paper_trading_epoch_id=epoch_id,
                    mlb_game_id=game.id,
                    evaluated_at=datetime(2026, 7, 2, 16, 0, tzinfo=UTC),
                    features={},
                    probability=Decimal("0.600000"),
                    model_probability=Decimal("0.600000"),
                    probability_calibrated=Decimal("0.600000"),
                    target_date=target,
                    fee_estimate=Decimal("0.010000"),
                    price_status="fresh_executable",
                    market_type="full_game_winner",
                    market_family="full_game_winner",
                    time_bucket="4H",
                    time_to_start_minutes=420,
                    decision="candidate_only",
                    outcome=outcome,
                    outcome_source="test",
                    resolved_at=datetime(2026, 7, 2, 4, 0, tzinfo=UTC),
                    feature_version=features.FEATURE_VERSION,
                    training_eligible=True,
                )
            )
        session.commit()

        result = run_job(session, job_name="governance", target_date=target, triggered_by="api")
        governance_result = result["result"]["governance"]
        training = session.get(TrainingRun, governance_result["training_run_id"])
        training_candidate_count = training.candidate_count if training else None
        training_metrics = training.metrics if training else None

    assert result["status"] == "succeeded"
    assert governance_result["resolved_samples"] == 1
    assert governance_result["paper_trading_epoch_id"] == active_id
    assert training is not None
    assert training_candidate_count == 1
    assert training_metrics["paper_trading_epoch_id"] == active_id


def test_job_endpoint_accepts_symbolic_target_date(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    captured: dict[str, object] = {}

    def fake_run_job(
        session,
        *,
        job_name,
        target_date,
        triggered_by,
        max_runtime_minutes=60,
        min_time_to_start_minutes=None,
        max_time_to_start_minutes=None,
        sweep_label=None,
        dry_run_candidates_only=False,
    ):
        captured["job_name"] = job_name
        captured["target_date"] = target_date
        captured["triggered_by"] = triggered_by
        return {"status": "succeeded", "target_date": target_date.isoformat()}

    monkeypatch.setattr(
        main_module,
        "database_status",
        lambda: {"ready": True, "configured": True, "dialect": "sqlite", "message": "ok"},
    )
    monkeypatch.setattr(main_module, "get_session_factory", lambda: SessionLocal)
    monkeypatch.setattr(main_module, "run_job", fake_run_job)
    monkeypatch.setattr(job_runs, "today_eastern", lambda: date(2026, 7, 2))
    app.dependency_overrides[require_internal_api_key] = lambda: None

    try:
        response = client.post("/v1/jobs/run/daily-setup?target_date=today_et")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert captured == {"job_name": "daily-setup", "target_date": date(2026, 7, 2), "triggered_by": "api"}
    assert response.json()["result"]["target_date"] == "2026-07-02"


def test_starter_endpoints_return_422_for_invalid_dates() -> None:
    app.dependency_overrides[require_internal_api_key] = lambda: None

    try:
        sync_response = client.post("/v1/sync/mlb-starters?target_date=not-a-date")
        pregame_response = client.post("/v1/sync/mlb-pregame-context?target_date=not-a-date")
        status_response = client.get("/v1/model/starter-status?date=not-a-date")
    finally:
        app.dependency_overrides.clear()

    assert sync_response.status_code == 422
    assert sync_response.json()["detail"] == "Invalid target_date. Use YYYY-MM-DD, today_et, or yesterday_et."
    assert pregame_response.status_code == 422
    assert pregame_response.json()["detail"] == "Invalid target_date. Use YYYY-MM-DD, today_et, or yesterday_et."
    assert status_response.status_code == 422
    assert status_response.json()["detail"] == "Invalid date. Use YYYY-MM-DD, today_et, or yesterday_et."


def test_job_lock_is_committed_before_steps_execute(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "job-lock.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    Base.metadata.create_all(engine)
    observed: dict[str, object] = {}

    def fake_execute_steps(session, run, job_name, target_date):
        with Session(engine) as verifier:
            visible_run = verifier.scalar(select(JobRun).where(JobRun.id == run.id))
        observed["visible_status"] = visible_run.status if visible_run else None
        return {"lock_visible": visible_run is not None}

    monkeypatch.setattr(job_runs, "_execute_job_steps", fake_execute_steps)

    with Session(engine) as session:
        result = job_runs.run_job(
            session,
            job_name="price-refresh",
            target_date=date(2026, 7, 2),
            triggered_by="api",
        )

    assert observed["visible_status"] == "running"
    assert result["status"] == "succeeded"
    assert result["result"] == {"lock_visible": True}


def test_running_job_lock_key_is_database_unique() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 7, 2)
    now = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)

    with Session(engine) as session:
        epoch = get_or_create_active_paper_epoch(session)
        session.add_all(
            [
                JobRun(
                    job_name="price-refresh",
                    job_type="paper_ops",
                    target_date=target,
                    paper_trading_epoch_id=epoch.id,
                    status="running",
                    started_at=now,
                    lock_key="price-refresh:global",
                    triggered_by="api",
                ),
                JobRun(
                    job_name="price-refresh",
                    job_type="paper_ops",
                    target_date=target,
                    paper_trading_epoch_id=epoch.id,
                    status="running",
                    started_at=now,
                    lock_key="price-refresh:global",
                    triggered_by="cron",
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_run_job_rolls_back_failed_transaction_before_marking_failed(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 7, 2)
    now = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)

    def fake_execute_steps(session, run, job_name, target_date):
        session.add(
            JobRun(
                job_name=job_name,
                job_type="paper_ops",
                target_date=target_date,
                paper_trading_epoch_id=run.paper_trading_epoch_id,
                status="running",
                started_at=now,
                lock_key=run.lock_key,
                triggered_by="duplicate",
                steps=[],
                result={},
                errors=[],
                warnings=[],
                idempotency_key=f"{run.lock_key}:duplicate",
            )
        )
        session.flush()

    monkeypatch.setattr(job_runs, "_execute_job_steps", fake_execute_steps)

    with Session(engine) as session:
        result = job_runs.run_job(
            session,
            job_name="price-refresh",
            target_date=target,
            triggered_by="api",
        )
        stored_run = session.get(JobRun, result["job_run_id"])

    assert result["status"] == "failed"
    assert result["errors"][0]["type"] == "IntegrityError"
    assert stored_run is not None
    assert stored_run.status == "failed"
    assert stored_run.completed_at is not None
    assert stored_run.errors[0]["type"] == "IntegrityError"


def test_run_job_preserves_failed_step_details_after_rollback(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 7, 2)

    def fake_execute_steps(_session, run, _job_name, _target_date):
        def fail_step():
            raise RuntimeError("forced step failure")

        return job_runs._run_step(run, "forced_failure", fail_step)

    monkeypatch.setattr(job_runs, "_execute_job_steps", fake_execute_steps)

    with Session(engine) as session:
        result = job_runs.run_job(
            session,
            job_name="price-refresh",
            target_date=target,
            triggered_by="api",
        )
        stored_run = session.get(JobRun, result["job_run_id"])

    assert result["status"] == "failed"
    assert result["steps"][0]["name"] == "forced_failure"
    assert result["steps"][0]["status"] == "failed"
    assert result["steps"][0]["error"]["type"] == "RuntimeError"
    assert stored_run is not None
    assert stored_run.steps == result["steps"]


def test_run_step_preserves_completed_details_after_nested_commit_reload() -> None:
    now = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)
    run = JobRun(
        job_name="price-refresh",
        job_type="paper_ops",
        status="running",
        started_at=now,
        lock_key="price-refresh:global",
        triggered_by="api",
        steps=[],
    )

    def fn():
        run.steps = [{"name": "price_refresh", "status": "running", "started_at": now.isoformat()}]
        return {"updated": 1}

    result = job_runs._run_step(run, "price_refresh", fn)

    assert result == {"updated": 1}
    assert len(run.steps) == 1
    assert run.steps[0]["name"] == "price_refresh"
    assert run.steps[0]["status"] == "succeeded"
    assert run.steps[0]["result"] == {"updated": 1}


def test_websocket_market_update_only_marks_active_epoch_trade() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        archived = PaperTradingEpoch(
            epoch_key="archived-test",
            display_name="ARCHIVED TEST",
            status="archived",
            mode="paper",
            starting_balance=Decimal("1000.00"),
            started_at=now,
            archived_at=now,
        )
        market = KalshiMarket(
            kalshi_market_id="KX-WS",
            ticker="KXMLBGAME-WS-PIT",
            title="Will Pittsburgh win?",
            status="open",
        )
        active_trade = PaperTrade(
            paper_trading_epoch_id=active.id,
            market_ticker="KXMLBGAME-WS-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            entry_time=now,
            status="open",
        )
        archived_trade = PaperTrade(
            paper_trading_epoch_id=None,
            market_ticker="KXMLBGAME-WS-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            entry_time=now,
            status="open",
        )
        session.add_all([archived, market, active_trade, archived_trade])
        session.flush()
        archived_trade.paper_trading_epoch_id = archived.id
        session.commit()

        result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-PIT",
            {"market_ticker": "KXMLBGAME-WS-PIT", "best_yes_bid": "0.5500"},
        )
        status_row = session.scalar(select(MarketDataWorkerStatus).where(MarketDataWorkerStatus.status_key == "kalshi_ws_paper"))
        session.commit()
        active_price = active_trade.current_price
        archived_price = archived_trade.current_price
        market_source = market.market_data_source
        status_source = status_row.source if status_row else None

    assert result["updated"] is True
    assert result["updated_trades"] == 1
    assert active_price == Decimal("0.5500")
    assert archived_price == Decimal("0.4000")
    assert market_source == "websocket"
    assert status_row is not None
    assert status_source == "websocket"


def test_websocket_market_update_converts_legacy_cent_prices() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        market = KalshiMarket(
            kalshi_market_id="KX-WS-CENTS",
            ticker="KXMLBGAME-WS-CENTS-PIT",
            title="Will Pittsburgh win?",
            status="open",
        )
        trade = PaperTrade(
            paper_trading_epoch_id=active.id,
            market_ticker="KXMLBGAME-WS-CENTS-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            entry_time=now,
            status="open",
        )
        session.add_all([market, trade])
        session.commit()

        result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-CENTS-PIT",
            {"market_ticker": "KXMLBGAME-WS-CENTS-PIT", "yes_bid": 55, "yes_ask": 56, "last_price": 54},
        )
        session.commit()
        market_values = {
            "yes_bid": market.yes_bid,
            "yes_ask": market.yes_ask,
            "best_yes_bid": market.best_yes_bid,
            "implied_yes_ask": market.implied_yes_ask,
            "last_price": market.last_price,
        }
        trade_price = trade.current_price

    assert result["updated"] is True
    assert market_values == {
        "yes_bid": Decimal("0.5500"),
        "yes_ask": Decimal("0.5600"),
        "best_yes_bid": Decimal("0.5500"),
        "implied_yes_ask": Decimal("0.5600"),
        "last_price": Decimal("0.5400"),
    }
    assert trade_price == Decimal("0.5500")


def test_websocket_orderbook_snapshot_updates_marks_and_executable_freshness(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(ws_market_data, "utc_now", lambda: now)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        market = KalshiMarket(
            kalshi_market_id="KX-WS-SNAPSHOT",
            ticker="KXMLBGAME-WS-SNAPSHOT-PIT",
            title="Will Pittsburgh win?",
            status="open",
        )
        trade = PaperTrade(
            paper_trading_epoch_id=active.id,
            market_ticker="KXMLBGAME-WS-SNAPSHOT-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            entry_time=now,
            status="open",
        )
        session.add_all([market, trade])
        session.commit()

        result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-SNAPSHOT-PIT",
            {
                "market_ticker": "KXMLBGAME-WS-SNAPSHOT-PIT",
                "yes_dollars_fp": [["0.5500", "12"]],
                "no_dollars_fp": [["0.4300", "8"]],
            },
        )
        status_row = session.scalar(select(MarketDataWorkerStatus).where(MarketDataWorkerStatus.status_key == "kalshi_ws_paper"))
        session.commit()
        values = {
            "best_yes_bid": market.best_yes_bid,
            "best_no_bid": market.best_no_bid,
            "implied_yes_ask": market.implied_yes_ask,
            "implied_no_ask": market.implied_no_ask,
            "market_price_updated_at": market.market_price_updated_at,
            "trade_price": trade.current_price,
            "status_last_message_at": status_row.last_message_at if status_row else None,
            "status_stale_count": status_row.stale_count if status_row else None,
        }

    assert result["updated"] is True
    assert result["updated_trades"] == 1
    assert values["best_yes_bid"] == Decimal("0.5500")
    assert values["best_no_bid"] == Decimal("0.4300")
    assert values["implied_yes_ask"] == Decimal("0.5700")
    assert values["implied_no_ask"] == Decimal("0.4500")
    assert values["market_price_updated_at"] is not None
    assert values["market_price_updated_at"].replace(tzinfo=UTC) == now
    assert values["trade_price"] == Decimal("0.5500")
    assert values["status_last_message_at"] is not None
    assert values["status_stale_count"] == 0


def test_websocket_orderbook_delta_updates_yes_bid_as_no_executable_price(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    stale = now - timedelta(minutes=30)
    monkeypatch.setattr(ws_market_data, "utc_now", lambda: now)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        market = KalshiMarket(
            kalshi_market_id="KX-WS-DELTA",
            ticker="KXMLBGAME-WS-DELTA-PIT",
            title="Will Pittsburgh win?",
            status="open",
            best_yes_bid=Decimal("0.4000"),
            yes_ask=Decimal("0.7000"),
            no_ask=Decimal("0.6200"),
            market_price_updated_at=stale,
            market_data_source="rest",
        )
        trade = PaperTrade(
            paper_trading_epoch_id=active.id,
            market_ticker="KXMLBGAME-WS-DELTA-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            current_price_updated_at=stale,
            quantity=1,
            entry_time=now,
            status="open",
        )
        session.add_all([market, trade])
        session.commit()

        result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-DELTA-PIT",
            {"market_ticker": "KXMLBGAME-WS-DELTA-PIT", "side": "yes", "price_dollars": "0.5500", "delta_fp": 1},
        )
        session.commit()
        no_price_context = candidates._market_side_price_context(market, "no", now)
        yes_price_context = candidates._market_yes_price_context(market, now)
        values = {
            "best_yes_bid": market.best_yes_bid,
            "implied_no_ask": market.implied_no_ask,
            "yes_ask": market.yes_ask,
            "no_ask": market.no_ask,
            "websocket_updated_at": market.websocket_updated_at,
            "market_price_updated_at": market.market_price_updated_at,
            "market_data_source": market.market_data_source,
            "trade_price": trade.current_price,
            "trade_price_updated_at": trade.current_price_updated_at,
            "no_price_context": no_price_context,
            "yes_price_context": yes_price_context,
        }

    assert result["updated"] is True
    assert result["updated_trades"] == 1
    assert values["best_yes_bid"] == Decimal("0.5500")
    assert values["implied_no_ask"] == Decimal("0.4500")
    assert values["yes_ask"] == Decimal("0.7000")
    assert values["no_ask"] is None
    assert values["websocket_updated_at"] is not None
    assert values["websocket_updated_at"].replace(tzinfo=UTC) == now
    assert values["market_price_updated_at"] is not None
    assert values["market_price_updated_at"].replace(tzinfo=UTC) == now
    assert values["market_data_source"] == "websocket"
    assert values["trade_price"] == Decimal("0.5500")
    assert values["trade_price_updated_at"] is not None
    assert values["trade_price_updated_at"].replace(tzinfo=UTC) == now
    assert values["no_price_context"].source == "orderbook_implied_no_ask"
    assert values["no_price_context"].executable_price == Decimal("0.4500")
    assert values["yes_price_context"].source == "yes_ask"
    assert values["yes_price_context"].status == "stale"
    assert values["yes_price_context"].executable_price is None


def test_websocket_orderbook_delta_clears_removed_best_bid_without_freshening_price(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    stale = now - timedelta(minutes=30)
    monkeypatch.setattr(ws_market_data, "utc_now", lambda: now)

    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        market = KalshiMarket(
            kalshi_market_id="KX-WS-DELTA-REMOVE",
            ticker="KXMLBGAME-WS-DELTA-REMOVE-PIT",
            title="Will Pittsburgh win?",
            status="open",
            best_no_bid=Decimal("0.4300"),
            implied_yes_ask=Decimal("0.5700"),
            market_price_updated_at=stale,
            market_data_source="rest",
        )
        session.add(market)
        session.commit()

        result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-DELTA-REMOVE-PIT",
            {"market_ticker": "KXMLBGAME-WS-DELTA-REMOVE-PIT", "side": "no", "price_dollars": "0.4300", "delta_fp": -1},
        )
        session.commit()
        values = {
            "best_no_bid": market.best_no_bid,
            "implied_yes_ask": market.implied_yes_ask,
            "websocket_updated_at": market.websocket_updated_at,
            "market_price_updated_at": market.market_price_updated_at,
            "market_data_source": market.market_data_source,
        }

    assert result["updated"] is True
    assert result["updated_trades"] == 0
    assert values["best_no_bid"] is None
    assert values["implied_yes_ask"] is None
    assert values["websocket_updated_at"] is not None
    assert values["websocket_updated_at"].replace(tzinfo=UTC) == now
    assert values["market_price_updated_at"] is not None
    assert values["market_price_updated_at"].replace(tzinfo=UTC) == stale
    assert values["market_data_source"] == "websocket"


def test_websocket_orderbook_delta_falls_back_to_next_book_level(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(ws_market_data, "utc_now", lambda: now)

    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        market = KalshiMarket(
            kalshi_market_id="KX-WS-DELTA-FALLBACK",
            ticker="KXMLBGAME-WS-DELTA-FALLBACK-PIT",
            title="Will Pittsburgh win?",
            status="open",
        )
        session.add(market)
        session.commit()

        snapshot_result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-DELTA-FALLBACK-PIT",
            {
                "market_ticker": "KXMLBGAME-WS-DELTA-FALLBACK-PIT",
                "no_dollars_fp": [["0.4300", "8"], ["0.4100", "5"]],
            },
        )
        delta_result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-DELTA-FALLBACK-PIT",
            {
                "market_ticker": "KXMLBGAME-WS-DELTA-FALLBACK-PIT",
                "side": "no",
                "price_dollars": "0.4300",
                "delta_fp": -8,
            },
        )
        session.commit()
        values = {
            "best_no_bid": market.best_no_bid,
            "implied_yes_ask": market.implied_yes_ask,
            "orderbook_raw": market.orderbook_raw,
        }

    assert snapshot_result["updated"] is True
    assert delta_result["updated"] is True
    assert values["best_no_bid"] == Decimal("0.4100")
    assert values["implied_yes_ask"] == Decimal("0.5900")
    assert values["orderbook_raw"]["websocket_orderbook"]["no_dollars"] == {"0.4100": "5.0000"}


def test_websocket_legacy_orderbook_delta_keeps_snapshot_price_units(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(ws_market_data, "utc_now", lambda: now)

    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        market = KalshiMarket(
            kalshi_market_id="KX-WS-LEGACY-DELTA",
            ticker="KXMLBGAME-WS-LEGACY-DELTA-PIT",
            title="Will Pittsburgh win?",
            status="open",
        )
        session.add(market)
        session.commit()

        snapshot_result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-LEGACY-DELTA-PIT",
            {
                "market_ticker": "KXMLBGAME-WS-LEGACY-DELTA-PIT",
                "yes": [[55, 10], [52, 5]],
            },
        )
        remove_result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-LEGACY-DELTA-PIT",
            {
                "market_ticker": "KXMLBGAME-WS-LEGACY-DELTA-PIT",
                "side": "yes",
                "price": 55,
                "delta_fp": -10,
            },
        )
        add_result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-LEGACY-DELTA-PIT",
            {
                "market_ticker": "KXMLBGAME-WS-LEGACY-DELTA-PIT",
                "side": "yes",
                "price_dollars": "0.5600",
                "delta_fp": 3,
            },
        )
        session.commit()
        values = {
            "best_yes_bid": market.best_yes_bid,
            "implied_no_ask": market.implied_no_ask,
            "orderbook_raw": market.orderbook_raw,
        }

    assert snapshot_result["updated"] is True
    assert remove_result["updated"] is True
    assert add_result["updated"] is True
    assert values["best_yes_bid"] == Decimal("0.5600")
    assert values["implied_no_ask"] == Decimal("0.4400")
    assert values["orderbook_raw"]["websocket_orderbook"]["yes"] == {
        "52.0000": "5.0000",
        "56.0000": "3.0000",
    }
    assert "0.5600" not in values["orderbook_raw"]["websocket_orderbook"]["yes"]


def test_websocket_inverse_ask_update_clears_stale_direct_yes_ask(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    stale = now - timedelta(minutes=30)
    monkeypatch.setattr(ws_market_data, "utc_now", lambda: now)

    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        market = KalshiMarket(
            kalshi_market_id="KX-WS-INVERSE-ASK",
            ticker="KXMLBGAME-WS-INVERSE-ASK-PIT",
            title="Will Pittsburgh win?",
            status="open",
            yes_ask=Decimal("0.7000"),
            implied_yes_ask=Decimal("0.6900"),
            market_price_updated_at=stale,
            market_data_source="rest",
        )
        session.add(market)
        session.commit()

        result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-INVERSE-ASK-PIT",
            {"market_ticker": "KXMLBGAME-WS-INVERSE-ASK-PIT", "no_dollars_fp": [["0.4300", "8"]]},
        )
        session.commit()
        price_context = candidates._market_yes_price_context(market, now)
        values = {
            "yes_ask": market.yes_ask,
            "implied_yes_ask": market.implied_yes_ask,
            "best_no_bid": market.best_no_bid,
            "market_price_updated_at": market.market_price_updated_at,
            "price_context": price_context,
        }

    assert result["updated"] is True
    assert values["yes_ask"] is None
    assert values["implied_yes_ask"] == Decimal("0.5700")
    assert values["best_no_bid"] == Decimal("0.4300")
    assert values["market_price_updated_at"] is not None
    assert values["market_price_updated_at"].replace(tzinfo=UTC) == now
    assert values["price_context"].source == "orderbook_implied_yes_ask"
    assert values["price_context"].executable_price == Decimal("0.5700")


def test_websocket_direct_implied_no_ask_update_refreshes_no_price_freshness(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    stale = now - timedelta(minutes=30)
    monkeypatch.setattr(ws_market_data, "utc_now", lambda: now)

    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        market = KalshiMarket(
            kalshi_market_id="KX-WS-INVERSE-NO-ASK",
            ticker="KXMLBGAME-WS-INVERSE-NO-ASK-PIT",
            title="Will Pittsburgh win?",
            status="open",
            no_ask=Decimal("0.7000"),
            implied_no_ask=Decimal("0.6900"),
            market_price_updated_at=stale,
            market_data_source="rest",
        )
        session.add(market)
        session.commit()

        result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-INVERSE-NO-ASK-PIT",
            {"market_ticker": "KXMLBGAME-WS-INVERSE-NO-ASK-PIT", "implied_no_ask": "0.5700"},
        )
        session.commit()
        price_context = candidates._market_side_price_context(market, "no", now)
        values = {
            "no_ask": market.no_ask,
            "implied_no_ask": market.implied_no_ask,
            "market_price_updated_at": market.market_price_updated_at,
            "price_context": price_context,
        }

    assert result["updated"] is True
    assert values["no_ask"] is None
    assert values["implied_no_ask"] == Decimal("0.5700")
    assert values["market_price_updated_at"] is not None
    assert values["market_price_updated_at"].replace(tzinfo=UTC) == now
    assert values["price_context"].source == "orderbook_implied_no_ask"
    assert values["price_context"].executable_price == Decimal("0.5700")


def test_websocket_direct_no_ask_preserves_yes_ask_without_freshening_it(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    stale = now - timedelta(minutes=30)
    monkeypatch.setattr(ws_market_data, "utc_now", lambda: now)

    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        market = KalshiMarket(
            kalshi_market_id="KX-WS-DIRECT-NO-ASK",
            ticker="KXMLBGAME-WS-DIRECT-NO-ASK-PIT",
            title="Will Pittsburgh win?",
            status="open",
            yes_ask=Decimal("0.7000"),
            market_price_updated_at=stale,
            market_data_source="rest",
        )
        session.add(market)
        session.commit()

        result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-DIRECT-NO-ASK-PIT",
            {"market_ticker": "KXMLBGAME-WS-DIRECT-NO-ASK-PIT", "no_ask_dollars": "0.5800"},
        )
        session.commit()
        no_price_context = candidates._market_side_price_context(market, "no", now)
        yes_price_context = candidates._market_yes_price_context(market, now)
        values = {
            "no_ask": market.no_ask,
            "yes_ask": market.yes_ask,
            "market_price_updated_at": market.market_price_updated_at,
            "no_price_context": no_price_context,
            "yes_price_context": yes_price_context,
        }

    assert result["updated"] is True
    assert values["no_ask"] == Decimal("0.5800")
    assert values["yes_ask"] == Decimal("0.7000")
    assert values["market_price_updated_at"] is not None
    assert values["market_price_updated_at"].replace(tzinfo=UTC) == now
    assert values["no_price_context"].source == "no_ask"
    assert values["no_price_context"].executable_price == Decimal("0.5800")
    assert values["yes_price_context"].source == "yes_ask"
    assert values["yes_price_context"].status == "stale"
    assert values["yes_price_context"].executable_price is None


def test_websocket_direct_yes_ask_preserves_no_ask_without_freshening_it(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    stale = now - timedelta(minutes=30)
    monkeypatch.setattr(ws_market_data, "utc_now", lambda: now)

    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        market = KalshiMarket(
            kalshi_market_id="KX-WS-DIRECT-YES-ASK",
            ticker="KXMLBGAME-WS-DIRECT-YES-ASK-PIT",
            title="Will Pittsburgh win?",
            status="open",
            no_ask=Decimal("0.6200"),
            market_price_updated_at=stale,
            market_data_source="rest",
        )
        session.add(market)
        session.commit()

        result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-DIRECT-YES-ASK-PIT",
            {"market_ticker": "KXMLBGAME-WS-DIRECT-YES-ASK-PIT", "yes_ask_dollars": "0.4100"},
        )
        session.commit()
        yes_price_context = candidates._market_yes_price_context(market, now)
        no_price_context = candidates._market_side_price_context(market, "no", now)
        values = {
            "yes_ask": market.yes_ask,
            "no_ask": market.no_ask,
            "market_price_updated_at": market.market_price_updated_at,
            "yes_price_context": yes_price_context,
            "no_price_context": no_price_context,
        }

    assert result["updated"] is True
    assert values["yes_ask"] == Decimal("0.4100")
    assert values["no_ask"] == Decimal("0.6200")
    assert values["market_price_updated_at"] is not None
    assert values["market_price_updated_at"].replace(tzinfo=UTC) == now
    assert values["yes_price_context"].source == "yes_ask"
    assert values["yes_price_context"].executable_price == Decimal("0.4100")
    assert values["no_price_context"].source == "no_ask"
    assert values["no_price_context"].status == "stale"
    assert values["no_price_context"].executable_price is None


def test_websocket_mark_update_refreshes_only_matching_trade_side(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    stale = now - timedelta(minutes=30)
    monkeypatch.setattr(ws_market_data, "utc_now", lambda: now)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        market = KalshiMarket(
            kalshi_market_id="KX-WS-SIDE-MARK",
            ticker="KXMLBGAME-WS-SIDE-MARK-PIT",
            title="Will Pittsburgh win?",
            status="open",
            best_yes_bid=Decimal("0.4000"),
            best_no_bid=Decimal("0.4500"),
            market_price_updated_at=stale,
            market_data_source="rest",
        )
        yes_trade = PaperTrade(
            paper_trading_epoch_id=active.id,
            market_ticker="KXMLBGAME-WS-SIDE-MARK-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            current_price_updated_at=stale,
            quantity=1,
            entry_time=now,
            status="open",
        )
        no_trade = PaperTrade(
            paper_trading_epoch_id=active.id,
            market_ticker="KXMLBGAME-WS-SIDE-MARK-PIT",
            contract_side="no",
            entry_price=Decimal("0.4500"),
            current_price=Decimal("0.4500"),
            current_price_updated_at=stale,
            quantity=1,
            entry_time=now,
            status="open",
        )
        session.add_all([market, yes_trade, no_trade])
        session.commit()

        result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-SIDE-MARK-PIT",
            {"market_ticker": "KXMLBGAME-WS-SIDE-MARK-PIT", "side": "no", "price_dollars": "0.5500", "delta_fp": 1},
        )
        session.commit()
        values = {
            "best_no_bid": market.best_no_bid,
            "yes_price": yes_trade.current_price,
            "yes_updated_at": yes_trade.current_price_updated_at,
            "no_price": no_trade.current_price,
            "no_updated_at": no_trade.current_price_updated_at,
        }

    assert result["updated"] is True
    assert result["updated_trades"] == 1
    assert values["best_no_bid"] == Decimal("0.5500")
    assert values["yes_price"] == Decimal("0.4000")
    assert values["yes_updated_at"] is not None
    assert values["yes_updated_at"].replace(tzinfo=UTC) == stale
    assert values["no_price"] == Decimal("0.5500")
    assert values["no_updated_at"] is not None
    assert values["no_updated_at"].replace(tzinfo=UTC) == now


def test_websocket_market_update_does_not_mark_non_price_message_fresh(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    stale = now - timedelta(minutes=30)
    monkeypatch.setattr(ws_market_data, "utc_now", lambda: now)

    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        market = KalshiMarket(
            kalshi_market_id="KX-WS-NON-PRICE",
            ticker="KXMLBGAME-WS-NON-PRICE-PIT",
            title="Will Pittsburgh win?",
            status="open",
            best_yes_bid=Decimal("0.4000"),
            market_price_updated_at=stale,
            market_data_source="rest",
        )
        status = MarketDataWorkerStatus(
            status_key="kalshi_ws_paper",
            enabled=True,
            running=True,
            source="websocket",
            subscribed_market_count=1,
            reconnect_count=0,
            stale_count=1,
            last_seen_at=stale,
            heartbeat_at=stale,
            last_message_at=stale,
            raw_status={},
        )
        session.add_all([market, status])
        session.commit()

        result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-NON-PRICE-PIT",
            {"market_ticker": "KXMLBGAME-WS-NON-PRICE-PIT", "side": "yes", "delta_fp": 1},
        )
        session.commit()
        values = {
            "market_price_updated_at": market.market_price_updated_at,
            "market_data_source": market.market_data_source,
            "last_message_at": status.last_message_at,
            "stale_count": status.stale_count,
        }

    assert result["updated"] is False
    assert result["updated_trades"] == 0
    assert values["market_price_updated_at"] is not None
    assert values["market_price_updated_at"].replace(tzinfo=UTC) == stale
    assert values["market_data_source"] == "rest"
    assert values["last_message_at"] is not None
    assert values["last_message_at"].replace(tzinfo=UTC) == stale
    assert values["stale_count"] == 1


def test_websocket_bid_update_does_not_freshen_executable_ask(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    stale = now - timedelta(minutes=30)
    monkeypatch.setattr(ws_market_data, "utc_now", lambda: now)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        market = KalshiMarket(
            kalshi_market_id="KX-WS-BID",
            ticker="KXMLBGAME-WS-BID-PIT",
            title="Will Pittsburgh win?",
            status="open",
            yes_ask=Decimal("0.7000"),
            no_ask=Decimal("0.6200"),
            market_price_updated_at=stale,
            market_data_source="rest",
        )
        trade = PaperTrade(
            paper_trading_epoch_id=active.id,
            market_ticker="KXMLBGAME-WS-BID-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            current_price_updated_at=stale,
            quantity=1,
            entry_time=now,
            status="open",
        )
        session.add_all([market, trade])
        session.commit()

        result = ws_market_data.apply_ws_market_update(
            session,
            "KXMLBGAME-WS-BID-PIT",
            {"market_ticker": "KXMLBGAME-WS-BID-PIT", "yes_bid_dollars": "0.5500"},
        )
        session.commit()
        no_price_context = candidates._market_side_price_context(market, "no", now)
        yes_price_context = candidates._market_yes_price_context(market, now)
        values = {
            "yes_bid": market.yes_bid,
            "best_yes_bid": market.best_yes_bid,
            "yes_ask": market.yes_ask,
            "no_ask": market.no_ask,
            "market_price_updated_at": market.market_price_updated_at,
            "market_data_source": market.market_data_source,
            "trade_price": trade.current_price,
            "trade_price_updated_at": trade.current_price_updated_at,
            "no_price_context": no_price_context,
            "yes_price_context": yes_price_context,
        }

    assert result["updated"] is True
    assert result["updated_trades"] == 1
    assert values["yes_bid"] == Decimal("0.5500")
    assert values["best_yes_bid"] == Decimal("0.5500")
    assert values["yes_ask"] == Decimal("0.7000")
    assert values["no_ask"] is None
    assert values["market_price_updated_at"] is not None
    assert values["market_price_updated_at"].replace(tzinfo=UTC) == now
    assert values["market_data_source"] == "websocket"
    assert values["trade_price"] == Decimal("0.5500")
    assert values["trade_price_updated_at"] is not None
    assert values["trade_price_updated_at"].replace(tzinfo=UTC) == now
    assert values["no_price_context"].source == "orderbook_best_yes_bid_inverse"
    assert values["no_price_context"].executable_price == Decimal("0.4500")
    assert values["yes_price_context"].source == "yes_ask"
    assert values["yes_price_context"].status == "stale"
    assert values["yes_price_context"].executable_price is None


def test_websocket_status_payload_expires_stale_running_worker(monkeypatch) -> None:
    monkeypatch.setenv("WEBSOCKET_MARKET_DATA_ENABLED", "true")
    monkeypatch.setenv("WS_HEARTBEAT_TIMEOUT_SECONDS", "30")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    stale = now - timedelta(seconds=31)
    monkeypatch.setattr(ws_market_data, "utc_now", lambda: now)

    try:
        with Session(engine) as session:
            session.add(
                MarketDataWorkerStatus(
                    status_key="kalshi_ws_paper",
                    enabled=True,
                    running=True,
                    source="websocket",
                    subscribed_market_count=3,
                    reconnect_count=0,
                    stale_count=0,
                    last_seen_at=stale,
                    heartbeat_at=stale,
                    last_message_at=stale,
                    raw_status={},
                )
            )
            session.commit()

            payload = ws_market_data.ws_status_payload(session)
            dashboard_status = dashboard._websocket_status(session)
    finally:
        get_settings.cache_clear()

    assert payload["enabled"] is True
    assert payload["running"] is False
    assert payload["source"] == "rest_fallback"
    assert dashboard_status.running is False
    assert dashboard_status.source == "rest_fallback"


def test_websocket_status_clears_stale_count_after_fresh_message(monkeypatch) -> None:
    monkeypatch.setenv("WEBSOCKET_MARKET_DATA_ENABLED", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    stale = now - timedelta(minutes=5)
    monkeypatch.setattr(ws_market_data, "utc_now", lambda: now)

    try:
        with Session(engine) as session:
            session.add(
                MarketDataWorkerStatus(
                    status_key="kalshi_ws_paper",
                    enabled=True,
                    running=True,
                    source="websocket",
                    subscribed_market_count=3,
                    reconnect_count=0,
                    stale_count=3,
                    last_seen_at=stale,
                    heartbeat_at=stale,
                    last_message_at=stale,
                    raw_status={},
                )
            )
            session.commit()

            row = ws_market_data.mark_ws_status(
                session,
                running=True,
                subscribed_market_count=3,
                last_message=True,
                source="websocket",
            )
            session.commit()
            stale_count = row.stale_count
    finally:
        get_settings.cache_clear()

    assert stale_count == 0


def test_ws_worker_uses_ticker_channel_and_nested_msg_payload() -> None:
    subscribe_message = kalshi_ws_paper._subscribe_message(["KXMLBGAME-WS-PIT"])
    assert subscribe_message["params"]["channels"] == ["ticker", "orderbook_delta"]

    update_message = kalshi_ws_paper._update_subscription_message(2, [101, 102], ["KXMLBGAME-WS-SEA"])
    assert update_message == {
        "id": 2,
        "cmd": "update_subscription",
        "params": {
            "sids": [101, 102],
            "market_tickers": ["KXMLBGAME-WS-SEA"],
            "action": "add_markets",
        },
    }

    ticker, update_payload = kalshi_ws_paper._market_update_payload(
        {
            "type": "ticker",
            "msg": {
                "market_ticker": "KXMLBGAME-WS-PIT",
                "yes_bid_dollars": "0.5500",
            },
        }
    )

    assert ticker == "KXMLBGAME-WS-PIT"
    assert update_payload == {"market_ticker": "KXMLBGAME-WS-PIT", "yes_bid_dollars": "0.5500"}


def test_ws_auth_headers_sign_handshake_path() -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    headers = kalshi_ws_paper._websocket_auth_headers(
        "test-key",
        pem,
        "wss://demo-api.kalshi.co/trade-api/ws/v2",
        timestamp_ms="1234567890000",
    )

    assert headers["KALSHI-ACCESS-KEY"] == "test-key"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1234567890000"
    assert headers["KALSHI-ACCESS-SIGNATURE"]
    assert kalshi_ws_paper._websocket_path("wss://demo-api.kalshi.co/trade-api/ws/v2?env=demo") == "/trade-api/ws/v2"
    assert kalshi_ws_paper._websocket_path("wss://demo-api.kalshi.co?env=demo") == "/trade-api/ws/v2"


def test_ws_connect_kwargs_supports_legacy_extra_headers() -> None:
    headers = {"KALSHI-ACCESS-KEY": "test-key"}

    def legacy_connect(_uri: str, *, extra_headers=None, ping_interval=None, ping_timeout=None):
        return None

    def current_connect(_uri: str, *, additional_headers=None, ping_interval=None, ping_timeout=None):
        return None

    legacy_kwargs = kalshi_ws_paper._websocket_connect_kwargs(legacy_connect, headers)
    current_kwargs = kalshi_ws_paper._websocket_connect_kwargs(current_connect, headers)

    assert legacy_kwargs["extra_headers"] == headers
    assert "additional_headers" not in legacy_kwargs
    assert current_kwargs["additional_headers"] == headers
    assert "extra_headers" not in current_kwargs


def test_ws_worker_continues_after_first_ticker_update(monkeypatch) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    monkeypatch.setenv("WEBSOCKET_MARKET_DATA_ENABLED", "true")
    monkeypatch.setenv("KALSHI_API_KEY", "test-key")
    monkeypatch.setenv("KALSHI_API_SECRET", pem)
    monkeypatch.setenv("KALSHI_WS_BASE_URL", "wss://demo-api.kalshi.co/trade-api/ws/v2")
    monkeypatch.setenv("WS_HEARTBEAT_TIMEOUT_SECONDS", "1")
    get_settings.cache_clear()

    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    captured: dict[str, object] = {}

    with SessionLocal() as session:
        epoch = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        market = KalshiMarket(
            kalshi_market_id="KX-WS-LOOP",
            ticker="KXMLBGAME-WS-LOOP-PIT",
            title="Will Pittsburgh win?",
            status="open",
        )
        trade = PaperTrade(
            paper_trading_epoch_id=epoch.id,
            market_ticker="KXMLBGAME-WS-LOOP-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            entry_time=now,
            status="open",
        )
        session.add_all([market, trade])
        session.commit()
        trade_id = trade.id

    class FakeWebSocket:
        def __init__(self) -> None:
            self.messages = [
                (0, json.dumps({"type": "subscribed", "msg": {"channel": "ticker"}})),
                (
                    0.6,
                    json.dumps(
                        {
                            "type": "ticker",
                            "msg": {"market_ticker": "KXMLBGAME-WS-LOOP-PIT", "yes_bid_dollars": "0.6100"},
                        }
                    ),
                ),
                (
                    0.6,
                    json.dumps(
                        {
                            "type": "ticker",
                            "msg": {"market_ticker": "KXMLBGAME-WS-LOOP-PIT", "yes_bid_dollars": "0.6200"},
                        }
                    ),
                ),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def send(self, message: str) -> None:
            captured["subscribe"] = json.loads(message)

        async def recv(self) -> str:
            if not self.messages:
                raise asyncio.TimeoutError
            delay, message = self.messages.pop(0)
            await asyncio.sleep(delay)
            return message

    def fake_connect(uri: str, **kwargs):
        captured["uri"] = uri
        captured["headers"] = kwargs.get("additional_headers")
        return FakeWebSocket()

    monkeypatch.setattr(kalshi_ws_paper, "get_session_factory", lambda: SessionLocal)
    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=fake_connect))

    try:
        result = asyncio.run(kalshi_ws_paper._run_worker_once())
        with SessionLocal() as session:
            refreshed_trade = session.get(PaperTrade, trade_id)
    finally:
        get_settings.cache_clear()

    assert result["status"] == "message_applied"
    assert result["applied_updates"] == 2
    assert result["skipped_messages"] == 1
    assert captured["subscribe"]["params"]["channels"] == ["ticker", "orderbook_delta"]
    assert captured["headers"]["KALSHI-ACCESS-KEY"] == "test-key"
    assert refreshed_trade is not None
    assert refreshed_trade.current_price == Decimal("0.6200")


def test_ws_worker_subscribes_new_active_tickers_without_reconnect(monkeypatch) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    monkeypatch.setenv("WEBSOCKET_MARKET_DATA_ENABLED", "true")
    monkeypatch.setenv("KALSHI_API_KEY", "test-key")
    monkeypatch.setenv("KALSHI_API_SECRET", pem)
    monkeypatch.setenv("KALSHI_WS_BASE_URL", "wss://demo-api.kalshi.co/trade-api/ws/v2")
    monkeypatch.setenv("WS_HEARTBEAT_TIMEOUT_SECONDS", "1")
    get_settings.cache_clear()

    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    sent_messages: list[dict[str, object]] = []

    with SessionLocal() as session:
        epoch = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        market = KalshiMarket(
            kalshi_market_id="KX-WS-REFRESH-PIT",
            ticker="KXMLBGAME-WS-REFRESH-PIT",
            title="Will Pittsburgh win?",
            status="open",
        )
        trade = PaperTrade(
            paper_trading_epoch_id=epoch.id,
            market_ticker="KXMLBGAME-WS-REFRESH-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            entry_time=now,
            status="open",
        )
        session.add_all([market, trade])
        session.commit()
        epoch_id = epoch.id

    class FakeWebSocket:
        def __init__(self) -> None:
            self.created_new_ticker = False
            self.messages = [
                (0, json.dumps({"type": "subscribed", "msg": {"channel": "ticker", "sid": 101}})),
                (0, json.dumps({"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 102}})),
                (
                    0.6,
                    json.dumps(
                        {
                            "type": "ticker",
                            "msg": {"market_ticker": "KXMLBGAME-WS-REFRESH-PIT", "yes_bid_dollars": "0.6100"},
                        }
                    ),
                ),
                (
                    0.6,
                    json.dumps(
                        {
                            "type": "ticker",
                            "msg": {"market_ticker": "KXMLBGAME-WS-REFRESH-PIT", "yes_bid_dollars": "0.6200"},
                        }
                    ),
                ),
                (0, json.dumps({"id": 2, "type": "ok", "msg": {}})),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def send(self, message: str) -> None:
            sent_messages.append(json.loads(message))

        async def recv(self) -> str:
            if not self.messages:
                raise asyncio.TimeoutError
            delay, message = self.messages.pop(0)
            if "KXMLBGAME-WS-REFRESH-PIT" in message and not self.created_new_ticker:
                with SessionLocal() as session:
                    new_market = KalshiMarket(
                        kalshi_market_id="KX-WS-REFRESH-SEA",
                        ticker="KXMLBGAME-WS-REFRESH-SEA",
                        title="Will Seattle win?",
                        status="open",
                    )
                    new_trade = PaperTrade(
                        paper_trading_epoch_id=epoch_id,
                        market_ticker="KXMLBGAME-WS-REFRESH-SEA",
                        contract_side="yes",
                        entry_price=Decimal("0.4000"),
                        current_price=Decimal("0.4000"),
                        quantity=1,
                        entry_time=now,
                        status="open",
                    )
                    session.add_all([new_market, new_trade])
                    session.commit()
                self.created_new_ticker = True
            await asyncio.sleep(delay)
            return message

    def fake_connect(_uri: str, **_kwargs):
        return FakeWebSocket()

    monkeypatch.setattr(kalshi_ws_paper, "get_session_factory", lambda: SessionLocal)
    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=fake_connect))

    try:
        result = asyncio.run(kalshi_ws_paper._run_worker_once())
    finally:
        get_settings.cache_clear()

    assert result["status"] == "message_applied"
    assert result["subscription_refreshes"] == 1
    assert result["subscribed_market_count"] == 2
    assert sent_messages[0]["params"]["market_tickers"] == ["KXMLBGAME-WS-REFRESH-PIT"]
    assert sent_messages[1] == {
        "id": 2,
        "cmd": "update_subscription",
        "params": {
            "sids": [101, 102],
            "market_tickers": ["KXMLBGAME-WS-REFRESH-SEA"],
            "action": "add_markets",
        },
    }


def test_generate_candidates_does_not_reuse_archived_epoch_candidate(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        archived = PaperTradingEpoch(
            epoch_key="archived-candidate-reuse",
            display_name="ARCHIVED CANDIDATE REUSE",
            status="archived",
            mode="paper",
            starting_balance=Decimal("1000.00"),
            started_at=now - timedelta(days=1),
            archived_at=now,
        )
        game = MlbGame(
            external_game_id="archived-reuse-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-ARCHIVED-REUSE",
            ticker="KXMLBGAME-ARCHIVED-REUSE-PIT",
            title="Will the Pittsburgh Pirates win?",
            status="open",
            implied_yes_ask=Decimal("0.1000"),
        )
        session.add_all([archived, game, market])
        session.flush()
        archived_id = archived.id
        mapping = _add_candidate_mapping(
            session,
            game,
            market,
            mapping_status="confirmed",
            market_family="full_game_winner",
            market_type="full_game_winner",
            selection_code="PIT",
            settlement_rule_status="paper_supported",
        )
        session.flush()
        archived_candidate = ModelCandidate(
            paper_trading_epoch_id=archived.id,
            mapping_id=mapping.id,
            mlb_game_id=game.id,
            kalshi_market_id=market.id,
            evaluated_at=now - timedelta(hours=1),
            target_date=date(2026, 7, 1),
            time_bucket=classify_time_bucket(420),
            features={},
            decision="archived_original",
            market_type="full_game_winner",
        )
        session.add(archived_candidate)
        session.commit()

        result = candidates.generate_candidates(session, target_date=date(2026, 7, 1))
        archived_after = session.get(ModelCandidate, archived_candidate.id)
        active_candidates = list(
            session.scalars(select(ModelCandidate).where(ModelCandidate.paper_trading_epoch_id == active.id))
            )

    assert result["candidates"] == 1
    assert archived_after is not None
    assert archived_after.paper_trading_epoch_id == archived_id
    assert archived_after.decision == "archived_original"
    assert len(active_candidates) == 1
    assert active_candidates[0].id != archived_candidate.id


def test_generate_candidates_avoids_duplicate_open_trade_across_days(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    current_time = {"now": datetime(2026, 7, 1, 16, 0, tzinfo=UTC)}
    monkeypatch.setattr(candidates, "utc_now", lambda: current_time["now"])

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="duplicate-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            home_abbreviation="NYY",
            away_abbreviation="BOS",
            scheduled_start=datetime(2026, 7, 4, 0, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-DUPLICATE",
            ticker="KXMLBGAME-DUPLICATE-NYY",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 4, 0, 0, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
        )
        session.add_all([game, market])
        _add_candidate_mapping(session, game, market)
        session.commit()

        first_result = candidates.generate_candidates(session, target_date=date(2026, 7, 3))
        current_time["now"] = datetime(2026, 7, 2, 16, 0, tzinfo=UTC)
        market.market_price_updated_at = current_time["now"]
        session.add(market)
        session.commit()
        second_result = candidates.generate_candidates(session, target_date=date(2026, 7, 3))

        all_candidates = list(session.scalars(select(ModelCandidate).order_by(ModelCandidate.id.asc())))
        all_trades = list(session.scalars(select(PaperTrade).order_by(PaperTrade.id.asc())))

    assert first_result["paper_trades"] == 1
    assert second_result["paper_trades"] == 0
    assert len(all_candidates) == 2
    assert len(all_trades) == 1
    assert all_trades[0].market_ticker == "KXMLBGAME-DUPLICATE-NYY"
    assert all_candidates[0].decision == "paper_trade"
    assert all_candidates[1].decision == "candidate_only_existing_trade"


def test_generate_candidates_avoids_duplicate_open_trade_across_mappings(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine, autoflush=False) as session:
        first_game = MlbGame(
            external_game_id="doubleheader-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            home_abbreviation="NYY",
            away_abbreviation="BOS",
            scheduled_start=datetime(2026, 7, 2, 18, 0, tzinfo=UTC),
            status="scheduled",
        )
        second_game = MlbGame(
            external_game_id="doubleheader-2",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            home_abbreviation="NYY",
            away_abbreviation="BOS",
            scheduled_start=datetime(2026, 7, 2, 20, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-DOUBLEHEADER",
            ticker="KXMLBGAME-DOUBLEHEADER-NYY",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 2, 18, 5, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
        )
        market.market_price_updated_at = now
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

        result = candidates.generate_candidates(session, target_date=date(2026, 7, 2))
        all_candidates = list(session.scalars(select(ModelCandidate).order_by(ModelCandidate.id.asc())))
        all_trades = list(session.scalars(select(PaperTrade).order_by(PaperTrade.id.asc())))

    assert result["candidates"] == 2
    assert result["paper_trades"] == 1
    assert len(all_candidates) == 2
    assert len(all_trades) == 1
    assert {candidate.decision for candidate in all_candidates} == {"paper_trade", "no_trade_correlated_market_cap"}
    assert all_trades[0].market_ticker == "KXMLBGAME-DOUBLEHEADER-NYY"
    assert all_trades[0].contract_side == "yes"


def test_generate_candidates_refreshes_existing_open_trade_price(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    current_time = {"now": datetime(2026, 7, 1, 16, 0, tzinfo=UTC)}
    monkeypatch.setattr(candidates, "utc_now", lambda: current_time["now"])

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="refresh-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            home_abbreviation="NYY",
            away_abbreviation="BOS",
            scheduled_start=datetime(2026, 7, 4, 0, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-REFRESH",
            ticker="KXMLBGAME-REFRESH-NYY",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 4, 0, 0, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
            best_yes_bid=Decimal("0.3800"),
        )
        session.add_all([game, market])
        _add_candidate_mapping(session, game, market)
        session.commit()

        first_result = candidates.generate_candidates(session, target_date=date(2026, 7, 3))
        trade = session.scalar(select(PaperTrade).where(PaperTrade.market_ticker == "KXMLBGAME-REFRESH-NYY"))
        assert trade is not None
        assert trade.current_price == Decimal("0.4000")

        current_time["now"] = datetime(2026, 7, 2, 16, 0, tzinfo=UTC)
        market.implied_yes_ask = Decimal("0.3200")
        market.best_yes_bid = Decimal("0.2800")
        market.market_price_updated_at = current_time["now"]
        session.add(market)
        session.commit()
        second_result = candidates.generate_candidates(session, target_date=date(2026, 7, 3))

        refreshed_trade = session.get(PaperTrade, trade.id)
        all_trades = list(session.scalars(select(PaperTrade)))

    assert first_result["paper_trades"] == 1
    assert second_result["paper_trades"] == 0
    assert len(all_trades) == 1
    assert refreshed_trade is not None
    assert refreshed_trade.entry_price == Decimal("0.4000")
    assert refreshed_trade.current_price == Decimal("0.2800")


def test_generate_candidates_refreshes_existing_no_trade_with_no_bid_mark(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        game = MlbGame(
            external_game_id="refresh-no-1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            home_abbreviation="NYY",
            away_abbreviation="BOS",
            scheduled_start=datetime(2026, 7, 4, 0, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-REFRESH-NO",
            ticker="KXMLBGAME-REFRESH-NO-NYY",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 4, 0, 0, tzinfo=UTC),
            no_ask=Decimal("0.4500"),
            best_no_bid=Decimal("0.3100"),
            market_price_updated_at=now,
        )
        session.add_all([game, market])
        _add_candidate_mapping(session, game, market)
        trade = PaperTrade(
            paper_trading_epoch_id=epoch_id,
            market_ticker=market.ticker,
            contract_side="no",
            entry_price=Decimal("0.4500"),
            current_price=Decimal("0.4500"),
            quantity=1,
            entry_time=now - timedelta(minutes=30),
            status="open",
        )
        session.add(trade)
        session.commit()

        result = candidates.generate_candidates(session, target_date=date(2026, 7, 3))
        refreshed_trade = session.get(PaperTrade, trade.id)
        no_candidate = session.scalar(select(ModelCandidate).where(ModelCandidate.contract_side == "no"))

    assert result["paper_trades"] == 0
    assert refreshed_trade is not None
    assert refreshed_trade.entry_price == Decimal("0.4500")
    assert refreshed_trade.current_price == Decimal("0.3100")
    assert no_candidate is not None
    assert no_candidate.executable_price == Decimal("0.4500")


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
        _add_candidate_mapping(session, game, market)
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))
        all_trades = list(session.scalars(select(PaperTrade)))

    assert result["candidates"] == 1
    assert result["paper_trades"] == 0
    assert candidate is not None
    assert candidate.market_type == "unknown"
    assert candidate.decision == "no_trade_unsupported_family"
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
            home_abbreviation="NYY",
            away_abbreviation="BOS",
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
        _add_candidate_mapping(session, game, market)
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))
        trade = session.scalar(select(PaperTrade))

    assert result["candidates"] == 1
    assert result["paper_trades"] == 0
    assert candidate is not None
    assert candidate.market_type == "full_game_winner"
    assert candidate.decision == "no_trade_untrusted_selection"
    assert candidate.gate_diagnostics["gate_selection_trusted_ok"] is False
    assert candidate.gate_diagnostics["gate_final_trade_eligible"] is False
    assert candidate.gate_diagnostics["counterfactual_trade_eligible_after_quality"] is False
    assert trade is None


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
            home_abbreviation="NYY",
            away_abbreviation="BOS",
            scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-ZERO-PRICE",
            ticker="KXMLBGAME-ZERO-PRICE-NYY",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 1, 23, 10, tzinfo=UTC),
            implied_yes_ask=Decimal("0.0000"),
        )
        session.add_all([game, market])
        _add_candidate_mapping(session, game, market)
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))
        trade = session.scalar(select(PaperTrade))

    assert result["paper_trades"] == 0
    assert candidate is not None
    assert candidate.market_price == Decimal("0.0000")
    assert candidate.executable_price is None
    assert candidate.fee_estimate is None
    assert candidate.price_status == "non_executable"
    assert candidate.decision == "no_trade_non_executable_price"
    assert trade is None


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
            home_abbreviation="NYY",
            away_abbreviation="BOS",
            scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-NO-ASK",
            ticker="KXMLBGAME-NO-ASK-NYY",
            title="Will the New York Yankees win the game against the Boston Red Sox?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 1, 23, 10, tzinfo=UTC),
            yes_mid=Decimal("0.3000"),
            last_price=Decimal("0.2800"),
            best_yes_bid=Decimal("0.2500"),
        )
        session.add_all([game, market])
        _add_candidate_mapping(session, game, market)
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))
        all_trades = list(session.scalars(select(PaperTrade)))

    assert result["paper_trades"] == 0
    assert candidate is not None
    assert candidate.market_price == Decimal("0.2800")
    assert candidate.executable_price is None
    assert candidate.price_status == "non_executable"
    assert candidate.decision == "no_trade_non_executable_price"
    assert all_trades == []


def test_market_side_price_context_uses_side_specific_quotes() -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    market = KalshiMarket(
        kalshi_market_id="KX-SIDE-PRICE",
        ticker="KXMLBGAME-SIDE-PRICE-PIT",
        title="Will Pittsburgh win?",
        status="open",
        yes_ask=Decimal("0.6100"),
        no_ask=Decimal("0.4200"),
        implied_yes_ask=Decimal("0.5900"),
        implied_no_ask=Decimal("0.4000"),
        market_price_updated_at=now,
    )

    yes_context = candidates._market_side_price_context(market, "yes", now)
    no_context = candidates._market_side_price_context(market, "no", now)

    assert yes_context.side == "yes"
    assert yes_context.executable_price == Decimal("0.6100")
    assert yes_context.source == "yes_ask"
    assert no_context.side == "no"
    assert no_context.executable_price == Decimal("0.4200")
    assert no_context.source == "no_ask"


def test_market_side_price_context_no_side_never_uses_yes_price() -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    market = KalshiMarket(
        kalshi_market_id="KX-NO-SIDE-NO-PRICE",
        ticker="KXMLBGAME-NO-SIDE-NO-PRICE-PIT",
        title="Will Pittsburgh win?",
        status="open",
        yes_ask=Decimal("0.6100"),
        implied_yes_ask=Decimal("0.5900"),
        market_price_updated_at=now,
    )

    no_context = candidates._market_side_price_context(market, "no", now)

    assert no_context.side == "no"
    assert no_context.market_price is None
    assert no_context.executable_price is None
    assert no_context.status == "missing"


def test_generate_candidates_creates_side_specific_no_candidate(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.200000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        game = MlbGame(
            external_game_id="side-aware-no-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-SIDE-AWARE-NO",
            ticker="KXMLBGAME-SIDE-AWARE-NO-PIT",
            title="Will Pittsburgh win?",
            status="open",
            yes_ask=Decimal("0.9500"),
            no_ask=Decimal("0.1500"),
            market_price_updated_at=now,
        )
        session.add_all([game, market])
        _add_candidate_mapping(session, game, market, market_family="full_game_winner", market_type="full_game_winner")
        session.commit()

        result = candidates.generate_candidates(session)
        rows = list(session.scalars(select(ModelCandidate).order_by(ModelCandidate.contract_side.asc())))
        trade = session.scalar(select(PaperTrade))

    assert result["candidates_yes"] == 1
    assert result["candidates_no"] == 1
    assert {row.contract_side for row in rows} == {"yes", "no"}
    assert next(row for row in rows if row.contract_side == "yes").executable_price == Decimal("0.9500")
    assert next(row for row in rows if row.contract_side == "no").executable_price == Decimal("0.1500")
    assert trade is not None
    assert trade.contract_side == "no"


def test_generate_candidates_uses_yes_ask_before_orderbook_implied_price(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *args, **kwargs: _fixed_model_score("0.900000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="yes-ask-source",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-YES-ASK-SOURCE",
            ticker="KXMLBGAME-YES-ASK-SOURCE-PIT",
            title="Will Pittsburgh win?",
            status="open",
            yes_ask=Decimal("0.4200"),
            implied_yes_ask=Decimal("0.3000"),
        )
        session.add_all([game, market])
        _add_candidate_mapping(session, game, market)
        session.commit()

        candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))

    assert candidate is not None
    assert candidate.executable_price == Decimal("0.4200")
    assert candidate.executable_price_source == "yes_ask"
    assert candidate.price_status == "fresh_executable"


def test_generate_candidates_derives_yes_ask_from_best_no_bid(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *args, **kwargs: _fixed_model_score("0.900000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="inverse-no-bid",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-INVERSE-NO-BID",
            ticker="KXMLBGAME-INVERSE-NO-BID-PIT",
            title="Will Pittsburgh win?",
            status="open",
            best_no_bid=Decimal("0.6100"),
        )
        session.add_all([game, market])
        _add_candidate_mapping(session, game, market)
        session.commit()

        candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))

    assert candidate is not None
    assert candidate.executable_price == Decimal("0.3900")
    assert candidate.executable_price_source == "orderbook_best_no_bid_inverse"


def test_generate_candidates_blocks_stale_prices(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_MAX_PRICE_STALENESS_SECONDS", "900")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *args, **kwargs: _fixed_model_score("0.900000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            game = MlbGame(
                external_game_id="stale-price",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
            market = KalshiMarket(
                kalshi_market_id="KX-STALE-PRICE",
                ticker="KXMLBGAME-STALE-PRICE-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.1000"),
            )
            session.add_all([game, market])
            _add_candidate_mapping(session, game, market)
            market.market_price_updated_at = now - timedelta(seconds=901)
            session.add(market)
            session.commit()

            result = candidates.generate_candidates(session)
            candidate = session.scalar(select(ModelCandidate))
            trade = session.scalar(select(PaperTrade))
    finally:
        get_settings.cache_clear()

    assert result["stale_price_count"] == 1
    assert result["paper_trades"] == 0
    assert candidate is not None
    assert candidate.price_status == "stale"
    assert candidate.decision == "no_trade_stale_price"
    assert candidate.training_eligible is False
    assert trade is None


def test_generate_candidates_uses_market_price_observation_timestamp(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_MAX_PRICE_STALENESS_SECONDS", "900")
    monkeypatch.setenv("PAPER_OBSERVATION_MIN_DATA_QUALITY", "0")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *args, **kwargs: _fixed_model_score("0.900000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            game = MlbGame(
                external_game_id="fresh-observed-price",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
            market = KalshiMarket(
                kalshi_market_id="KX-FRESH-OBSERVED-PRICE",
                ticker="KXMLBGAME-FRESH-OBSERVED-PRICE-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.1000"),
            )
            session.add_all([game, market])
            _add_candidate_mapping(session, game, market)
            market.updated_at = now - timedelta(seconds=901)
            market.market_price_updated_at = now
            session.add(market)
            session.commit()

            result = candidates.generate_candidates(session)
            candidate = session.scalar(select(ModelCandidate))
            trade = session.scalar(select(PaperTrade))
    finally:
        get_settings.cache_clear()

    assert result["stale_price_count"] == 0
    assert result["paper_trades"] == 1
    assert candidate is not None
    assert candidate.price_status == "fresh_executable"
    assert candidate.price_staleness_seconds == 0
    assert trade is not None


def test_generate_candidates_uses_fee_adjusted_ev_before_caps(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_MIN_NET_EV", "0.0500")
    monkeypatch.setenv("PAPER_MIN_PROB_EDGE", "0.0300")
    monkeypatch.setenv("PAPER_OBSERVATION_MIN_DATA_QUALITY", "0")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *args, **kwargs: _fixed_model_score("0.550000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            game = MlbGame(
                external_game_id="fee-ev",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
            market = KalshiMarket(
                kalshi_market_id="KX-FEE-EV",
                ticker="KXMLBGAME-FEE-EV-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.5200"),
            )
            session.add_all([game, market])
            _add_candidate_mapping(session, game, market)
            session.commit()

            result = candidates.generate_candidates(session)
            candidate = session.scalar(select(ModelCandidate))
            output = session.scalar(select(ModelPredictionOutput))
            trade = session.scalar(select(PaperTrade))
    finally:
        get_settings.cache_clear()

    assert result["paper_trades"] == 0
    assert result["trades_blocked_by_edge_or_fee"] == 1
    assert candidate is not None
    assert candidate.expected_value == Decimal("0.030000")
    assert candidate.fee_estimate is not None
    assert candidate.fee_estimate > Decimal("0")
    assert candidate.net_expected_value == candidate.expected_value - candidate.fee_estimate
    assert candidate.probability_edge == Decimal("0.030000")
    assert candidate.decision == "no_trade_fee_adjusted_ev_too_low"
    assert output is not None
    assert output.fee_estimate == candidate.fee_estimate
    assert output.expected_value_net == candidate.net_expected_value
    assert trade is None


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
        _add_candidate_mapping(session, game, market)
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
        _add_candidate_mapping(session, game, market)
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
        _add_candidate_mapping(session, game, market)
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
    assert result["zero_trade_reason"] == "no_candidates_missing_mappings"
    assert result["warnings"] == [
        "no_candidates_missing_mappings: run Kalshi market discovery and mapping sync for this target date."
    ]
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
            implied_yes_ask=Decimal("0.9000"),
        )
        market.market_price_updated_at = now
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

        result = candidates.generate_candidates(session, target_date=date(2026, 6, 26))
        mapping = session.scalar(select(MarketMapping))
        candidate = session.scalar(select(ModelCandidate))
        trade = session.scalar(select(PaperTrade))

    assert result["paper_trades"] == 0
    assert mapping is not None
    assert mapping.mapping_status == "confirmed"
    assert mapping.confidence == Decimal("0.9500")
    assert mapping.resolver_strategy == "exact_market_tickers"
    assert candidate is not None
    assert candidate.decision in {"no_trade_low_data_quality", "no_trade_edge_too_low", "no_trade_probability_edge_low"}
    assert candidate.model_version_tag == modeling.MATURE_MODEL_TAG
    assert trade is None


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
        _add_candidate_mapping(session, game, market)
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


def test_market_family_discovery_report_endpoints_require_api_key_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("BACKEND_API_KEY", "secret-test-key")
    get_settings.cache_clear()

    try:
        report_response = client.get("/v1/market-families/discovery?date=2026-07-01")
        preview_response = client.get("/v1/market-families/discovery-preview?date=2026-07-01")
    finally:
        get_settings.cache_clear()

    assert report_response.status_code == 401
    assert preview_response.status_code == 401


def test_model_read_endpoints_require_api_key_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("BACKEND_API_KEY", "secret-test-key")
    get_settings.cache_clear()

    try:
        governance_response = client.get("/v1/model/governance/status")
        coverage_response = client.get("/v1/model/features/coverage?date=2026-07-01")
        predictions_response = client.get("/v1/model/predictions/today")
        dated_predictions_response = client.get("/v1/model/predictions?date=2026-07-01")
    finally:
        get_settings.cache_clear()

    assert governance_response.status_code == 401
    assert coverage_response.status_code == 401
    assert predictions_response.status_code == 401
    assert dated_predictions_response.status_code == 401


def test_active_parameters_endpoint_persists_created_baseline(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    monkeypatch.setattr(
        main_module,
        "database_status",
        lambda: {"ready": True, "configured": True, "dialect": "sqlite", "message": "ok"},
    )
    monkeypatch.setattr(main_module, "get_session_factory", lambda: SessionLocal)
    app.dependency_overrides[require_internal_api_key] = lambda: None

    try:
        response = client.get("/v1/model/parameters/active")
    finally:
        app.dependency_overrides.clear()

    with SessionLocal() as session:
        persisted = session.scalar(
            select(ModelParameterVersion).where(
                ModelParameterVersion.version_tag == modeling.BASELINE_PARAMETER_VERSION_TAG
            )
        )

    assert response.status_code == 200
    assert response.json()["result"]["version_tag"] == modeling.BASELINE_PARAMETER_VERSION_TAG
    assert persisted is not None
    assert persisted.is_active is True


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


def test_feature_jobs_accept_positional_date_and_env_fallback(monkeypatch) -> None:
    monkeypatch.setenv("TARGET_DATE", "2026-06-24")

    assert mlb_feature_sync_job._target_date("2026-06-25") == date(2026, 6, 25)
    assert mlb_feature_sync_job._target_date() == date(2026, 6, 24)
    assert model_feature_snapshot_backfill_job._target_date("2026-06-26") == date(2026, 6, 26)
    assert model_feature_snapshot_backfill_job._target_date() == date(2026, 6, 24)

    monkeypatch.delenv("TARGET_DATE")
    assert mlb_feature_sync_job._target_date() is None
    assert model_feature_snapshot_backfill_job._target_date() is None


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


def test_market_sync_records_quote_observation_timestamp_when_prices_are_unchanged(monkeypatch) -> None:
    now = datetime(2026, 6, 25, 12, 0, tzinfo=UTC)
    stale = now - timedelta(hours=2)
    monkeypatch.setattr(market_sync, "utc_now", lambda: now)

    market = KalshiMarket(
        kalshi_market_id="KX-UNCHANGED-QUOTE",
        ticker="KXMLBGAME-UNCHANGED-QUOTE-PIT",
        title="Will Pittsburgh win?",
        status="open",
        yes_ask=Decimal("0.4000"),
    )
    market.updated_at = stale
    market.market_price_updated_at = stale

    market_sync._update_market_fields(
        market,
        {
            "id": "KX-UNCHANGED-QUOTE",
            "ticker": "KXMLBGAME-UNCHANGED-QUOTE-PIT",
            "title": "Will Pittsburgh win?",
            "status": "open",
            "yes_ask_dollars": "0.4000",
        },
        market.ticker,
        "open",
    )

    assert market.yes_ask == Decimal("0.4000")
    assert market.market_price_updated_at == now


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
    monkeypatch.setattr(market_sync.KalshiClient, "from_market_data_settings", staticmethod(lambda: fake_client))
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
    monkeypatch.setattr(market_sync.KalshiClient, "from_market_data_settings", staticmethod(lambda: fake_client))
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

    monkeypatch.setattr(market_sync.KalshiClient, "from_market_data_settings", staticmethod(lambda: FakeKalshiClient()))
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
    monkeypatch.setattr(market_sync.KalshiClient, "from_market_data_settings", staticmethod(lambda: fake_client))
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

    monkeypatch.setattr(market_sync.KalshiClient, "from_market_data_settings", staticmethod(lambda: FakeKalshiClient()))
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
    captured_at = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        for index in range(501):
            value = Decimal(index)
            session.add(
                BalanceSnapshot(
                    paper_trading_epoch_id=epoch_id,
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
        total_fee_estimate=Decimal("0.0200"),
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
    assert trade_summary.profit_loss == -1.02
    assert trade_summary.entry_notional == 1.0
    assert trade_summary.entry_total_cost == 1.02
    assert trade_summary.current_value == 0.0
    assert trade_summary.estimated_fee == 0.02
    assert trade_summary.profit_loss_percent == -1.0
    assert position_summary.current_price == 0.0
    assert position_summary.profit_loss == -1.0
    assert position_summary.entry_notional == 1.0
    assert position_summary.entry_total_cost == 1.0
    assert position_summary.current_value == 0.0
    assert position_summary.profit_loss_percent == -1.0


def test_dashboard_includes_paper_trades_alongside_positions() -> None:
    opened_at = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
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
                paper_trading_epoch_id=epoch_id,
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
                paper_trading_epoch_id=epoch_id,
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

        summary = dashboard.dashboard_summary_from_db(session, include_archived=True)

    markets = [position.market for position in summary.positions]
    assert markets == ["KXMLB-POSITION", "KXMLB-TRADE"]


def test_dashboard_summary_filters_closed_positions_by_selected_date() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        session.add_all(
            [
                PaperTrade(
                    paper_trading_epoch_id=epoch_id,
                    market_ticker="KXMLBGAME-CLOSED-JULY1-PIT",
                    contract_side="yes",
                    entry_price=Decimal("0.4000"),
                    current_price=Decimal("1.0000"),
                    exit_price=Decimal("1.0000"),
                    quantity=1,
                    entry_time=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
                    exit_time=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
                    status="settled",
                    outcome="win",
                    resolution="WIN",
                    realized_pnl=Decimal("0.60"),
                ),
                PaperTrade(
                    paper_trading_epoch_id=epoch_id,
                    market_ticker="KXMLBGAME-CLOSED-JULY2-PIT",
                    contract_side="yes",
                    entry_price=Decimal("0.4000"),
                    current_price=Decimal("0.0000"),
                    exit_price=Decimal("0.0000"),
                    quantity=1,
                    entry_time=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
                    exit_time=datetime(2026, 7, 2, 16, 0, tzinfo=UTC),
                    status="settled",
                    outcome="loss",
                    resolution="LOSS",
                    realized_pnl=Decimal("-0.40"),
                ),
            ]
        )
        session.commit()

        summary = dashboard.dashboard_summary_from_db(session, date(2026, 7, 1), include_pre_observation=True)

    assert summary.closed_positions_date == "2026-07-01"
    assert summary.closed_positions_count == 1
    assert summary.closed_positions[0].market_ticker == "KXMLBGAME-CLOSED-JULY1-PIT"
    assert summary.closed_positions[0].exit_price == 1.0
    assert summary.closed_positions[0].outcome == "win"


def test_dashboard_default_excludes_pre_observation_rows_with_history_opt_in() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        session.add_all(
            [
                PaperTrade(
                    paper_trading_epoch_id=epoch_id,
                    market_ticker="KXMLBGAME-PRE-CLOSED-PIT",
                    contract_side="yes",
                    entry_price=Decimal("0.4000"),
                    current_price=Decimal("1.0000"),
                    exit_price=Decimal("1.0000"),
                    quantity=1,
                    entry_time=datetime(2026, 7, 1, 20, 0, tzinfo=UTC),
                    exit_time=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="settled",
                    outcome="win",
                    resolution="WIN",
                    realized_pnl=Decimal("0.60"),
                ),
                PaperTrade(
                    paper_trading_epoch_id=epoch_id,
                    market_ticker="KXMLBGAME-PRE-OPEN-PIT",
                    contract_side="yes",
                    entry_price=Decimal("0.4000"),
                    current_price=Decimal("0.7000"),
                    quantity=1,
                    entry_time=datetime(2026, 7, 1, 22, 0, tzinfo=UTC),
                    status="open",
                ),
                PaperTrade(
                    paper_trading_epoch_id=epoch_id,
                    market_ticker="KXMLBGAME-POST-CLOSED-PIT",
                    contract_side="yes",
                    entry_price=Decimal("0.4000"),
                    current_price=Decimal("0.0000"),
                    exit_price=Decimal("0.0000"),
                    quantity=1,
                    entry_time=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
                    exit_time=datetime(2026, 7, 2, 18, 0, tzinfo=UTC),
                    status="settled",
                    outcome="loss",
                    resolution="LOSS",
                    realized_pnl=Decimal("-0.40"),
                ),
                PaperTrade(
                    paper_trading_epoch_id=epoch_id,
                    market_ticker="KXMLBGAME-POST-OPEN-PIT",
                    contract_side="yes",
                    entry_price=Decimal("0.5000"),
                    current_price=Decimal("0.6000"),
                    quantity=2,
                    entry_time=datetime(2026, 7, 2, 13, 0, tzinfo=UTC),
                    status="open",
                    total_fee_estimate=Decimal("0.020000"),
                ),
                BalanceSnapshot(
                    paper_trading_epoch_id=epoch_id,
                    captured_at=datetime(2026, 7, 2, 20, 0, tzinfo=UTC),
                    cash_balance=Decimal("999.00"),
                    portfolio_value=Decimal("999.00"),
                    source="paper",
                ),
            ]
        )
        session.commit()

        default_july2 = dashboard.dashboard_summary_from_db(session, date(2026, 7, 2))
        default_july1 = dashboard.dashboard_summary_from_db(session, date(2026, 7, 1))
        historical_july1 = dashboard.dashboard_summary_from_db(
            session,
            date(2026, 7, 1),
            include_pre_observation=True,
        )
        trades = list(session.scalars(select(PaperTrade).order_by(PaperTrade.market_ticker)))

    assert default_july2.observation_filter is not None
    assert default_july2.observation_filter.active is True
    assert default_july2.observation_filter.observation_start_date == "2026-07-02"
    assert default_july2.observation_filter.excluded_pre_observation_count == 2
    assert default_july2.observation_filter.historical_rows_available is True
    assert default_july2.positions[0].market_ticker == "KXMLBGAME-POST-OPEN-PIT"
    assert [position.market_ticker for position in default_july2.closed_positions] == [
        "KXMLBGAME-POST-CLOSED-PIT"
    ]
    assert default_july2.performance.record == "0-1-0"
    assert default_july2.performance.profit_loss == -0.4
    assert default_july2.cash_balance == 498.58
    assert default_july2.portfolio_value == 499.78
    assert default_july2.portfolio_series[0].value == 500.0
    assert default_july2.portfolio_series[-1].value == 499.78

    assert default_july1.closed_positions_count == 0
    assert default_july1.observation_filter is not None
    assert default_july1.observation_filter.excluded_pre_observation_closed_count == 1

    assert historical_july1.observation_filter is not None
    assert historical_july1.observation_filter.active is False
    assert historical_july1.closed_positions_count == 1
    assert historical_july1.closed_positions[0].market_ticker == "KXMLBGAME-PRE-CLOSED-PIT"
    assert historical_july1.performance.record == "1-1-0"
    assert historical_july1.performance.profit_loss == 0.2

    assert len(trades) == 4
    assert next(trade for trade in trades if trade.market_ticker == "KXMLBGAME-POST-OPEN-PIT").status == "open"


def test_dashboard_observation_cutoff_uses_fixed_eastern_time(monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_TIMEZONE", "UTC")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        session.add_all(
            [
                PaperTrade(
                    paper_trading_epoch_id=epoch_id,
                    market_ticker="KXMLBGAME-UTC-BEFORE-ET-CUTOFF",
                    contract_side="yes",
                    entry_price=Decimal("0.4000"),
                    current_price=Decimal("0.7000"),
                    quantity=1,
                    entry_time=datetime(2026, 7, 2, 3, 30, tzinfo=UTC),
                    status="open",
                ),
                PaperTrade(
                    paper_trading_epoch_id=epoch_id,
                    market_ticker="KXMLBGAME-UTC-AT-ET-CUTOFF",
                    contract_side="yes",
                    entry_price=Decimal("0.4000"),
                    current_price=Decimal("0.7000"),
                    quantity=1,
                    entry_time=datetime(2026, 7, 2, 4, 0, tzinfo=UTC),
                    status="open",
                ),
            ]
        )
        session.commit()

        summary = dashboard.dashboard_summary_from_db(session, date(2026, 7, 2))

    assert summary.observation_filter is not None
    assert summary.observation_filter.observation_start_at == "2026-07-02T04:00:00+00:00"
    assert summary.observation_filter.excluded_pre_observation_count == 1
    assert [position.market_ticker for position in summary.positions] == ["KXMLBGAME-UTC-AT-ET-CUTOFF"]


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
        epoch_id = _active_epoch_id(session)
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
            paper_trading_epoch_id=epoch_id,
            mlb_game_id=game.id,
            kalshi_market_id=win_market.id,
            mapping_id=win_mapping.id,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="paper_trade",
            market_type="full_game_winner",
        )
        loss_candidate = ModelCandidate(
            paper_trading_epoch_id=epoch_id,
            mlb_game_id=game.id,
            kalshi_market_id=loss_market.id,
            mapping_id=loss_mapping.id,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="paper_trade",
            market_type="full_game_winner",
        )
        no_trade_candidate = ModelCandidate(
            paper_trading_epoch_id=epoch_id,
            mlb_game_id=game.id,
            kalshi_market_id=win_market.id,
            mapping_id=win_mapping.id,
            evaluated_at=datetime(2026, 7, 1, 16, 5, tzinfo=UTC),
            features={},
            decision="no_trade_edge_too_low",
            market_type="full_game_moneyline",
            contract_side="yes",
        )
        session.add_all([win_candidate, loss_candidate, no_trade_candidate])
        session.flush()
        session.add_all(
            [
                PaperTrade(
                    paper_trading_epoch_id=epoch_id,
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
                    paper_trading_epoch_id=epoch_id,
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
    assert no_trade.market_type == "full_game_winner"


def test_contract_labels_normalize_no_on_first_five_tie_as_either_team_wins() -> None:
    game = MlbGame(
        external_game_id="label-f5-tie",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="Scheduled",
    )

    labels = contract_labels(
        game=game,
        market=None,
        market_ticker="KXMLBF5-26JUL011900SEAPIT-TIE",
        market_type="first_five_winner",
        selection_code="TIE",
        contract_side="no",
    )

    assert labels.actual_contract_display == "NO ON TIE FIRST 5 INNINGS WINNER"
    assert labels.normalized_equivalent_display == "PITTSBURGH PIRATES OR SEATTLE MARINERS WIN FIRST 5 INNINGS EQUIVALENT"


def test_paper_settlement_charges_stored_trade_fee_estimates() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settled_at = datetime(2026, 7, 2, 4, 0, tzinfo=UTC)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        game = MlbGame(
            external_game_id="settle-fee-1",
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
            kalshi_market_id="KX-SETTLE-FEE",
            ticker="KXMLBGAME-26JUL011900SEAPIT-PIT",
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
            paper_trading_epoch_id=epoch_id,
            mlb_game_id=game.id,
            kalshi_market_id=market.id,
            mapping_id=mapping.id,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="paper_trade",
            market_type="full_game_winner",
        )
        session.add(candidate)
        session.flush()
        trade = PaperTrade(
            paper_trading_epoch_id=epoch_id,
            candidate_id=candidate.id,
            market_ticker=market.ticker,
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            entry_time=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            status="open",
            total_fee_estimate=Decimal("0.040000"),
        )
        session.add(trade)
        session.commit()

        result = settle_paper_trades(session, date(2026, 7, 1), now=settled_at)
        settlement = session.scalar(select(Settlement))
        summary = dashboard.dashboard_summary_from_db(session, include_pre_observation=True)
        trade_values = {
            "fee_paid": trade.fee_paid,
            "realized_pnl": trade.realized_pnl,
        }

    assert result["settled"] == 1
    assert trade_values["fee_paid"] == Decimal("0.04")
    assert trade_values["realized_pnl"] == Decimal("0.56")
    assert settlement is not None
    assert settlement.fee_paid == Decimal("0.04")
    assert settlement.realized_pnl == Decimal("0.56")
    assert summary.cash_balance == 500.56
    assert summary.portfolio_value == 500.56
    assert summary.performance.profit_loss == 0.56
    assert summary.performance.roi == pytest.approx(float(Decimal("0.56") / Decimal("0.44")))


def test_paper_settlement_leaves_untrusted_ticker_selection_unresolved() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
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
            paper_trading_epoch_id=epoch_id,
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
            paper_trading_epoch_id=epoch_id,
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
        epoch_id = _active_epoch_id(session)
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
            paper_trading_epoch_id=epoch_id,
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
            paper_trading_epoch_id=epoch_id,
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


def _linescore_payload(innings: list[tuple[int | None, int | None]]) -> dict[str, object]:
    rows = []
    for away_runs, home_runs in innings:
        inning: dict[str, object] = {"away": {}, "home": {}}
        if away_runs is not None:
            inning["away"] = {"runs": away_runs}
        if home_runs is not None:
            inning["home"] = {"runs": home_runs}
        rows.append(inning)
    return {"linescore": {"innings": rows}}


def _add_settlement_trade(
    session: Session,
    *,
    epoch_id: int,
    game: MlbGame,
    ticker: str,
    family: str,
    contract_side: str = "yes",
    line: Decimal | None = None,
    selection: str | None = None,
    total_side: str | None = None,
    inning_scope: str = "full_game",
    market_status: str = "closed",
    current_price: Decimal = Decimal("0.4000"),
    with_position: bool = False,
) -> tuple[PaperTrade, ModelCandidate, KalshiMarket, Position | None]:
    market = KalshiMarket(
        kalshi_market_id=f"KX-{ticker}",
        ticker=ticker,
        title=ticker,
        status=market_status,
        market_family=family,
        market_type=family,
        line_value=line,
        selection_code=selection,
        over_under_side=total_side,
        inning_scope=inning_scope,
        settlement_rule_status="paper_supported",
    )
    session.add(market)
    session.flush()
    mapping = _add_candidate_mapping(
        session,
        game,
        market,
        mapping_status="confirmed",
        market_family=family,
        market_type=family,
        line_value=line,
        selection_code=selection,
        over_under_side=total_side,
        inning_scope=inning_scope,
        settlement_rule_status="paper_supported",
    )
    session.flush()
    candidate = ModelCandidate(
        paper_trading_epoch_id=epoch_id,
        mlb_game_id=game.id,
        kalshi_market_id=market.id,
        mapping_id=mapping.id,
        evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
        features={},
        decision="paper_trade",
        market_type=family,
        contract_side=contract_side,
        training_eligible=False,
        line_value=line,
        selection_code=selection,
        over_under_side=total_side,
        inning_scope=inning_scope,
        settlement_rule_status="paper_supported",
    )
    session.add(candidate)
    session.flush()
    trade = PaperTrade(
        paper_trading_epoch_id=epoch_id,
        candidate_id=candidate.id,
        market_ticker=ticker,
        contract_side=contract_side,
        entry_price=Decimal("0.4500"),
        current_price=current_price,
        quantity=1,
        entry_time=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
        status="open",
        market_family=family,
        line_value=line,
        selection_code=selection,
        over_under_side=total_side,
        inning_scope=inning_scope,
        settlement_rule_status="paper_supported",
        training_eligible=False,
    )
    session.add(trade)
    position = None
    if with_position:
        position = Position(
            kalshi_market_id=market.id,
            market_ticker=ticker,
            contract_side=contract_side,
            entry_price=trade.entry_price,
            current_price=current_price,
            quantity=1,
            opened_at=trade.entry_time,
            status="open",
        )
        session.add(position)
    return trade, candidate, market, position


def test_first_five_total_settles_before_full_game_final_and_is_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settled_at = datetime(2026, 7, 1, 22, 0, tzinfo=UTC)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        game = MlbGame(
            external_game_id="settle-f5-total-early-win",
            home_team="Milwaukee Brewers",
            away_team="Cincinnati Reds",
            home_abbreviation="MIL",
            away_abbreviation="CIN",
            scheduled_start=datetime(2026, 7, 1, 19, 10, tzinfo=UTC),
            status="In Progress",
            raw_payload=_linescore_payload([(1, 0), (0, 1), (1, 0), (0, 0), (1, 1)]),
        )
        session.add(game)
        session.flush()
        trade, candidate, _market, position = _add_settlement_trade(
            session,
            epoch_id=epoch_id,
            game=game,
            ticker="KXMLBF5TOTAL-26JUL011510CINMIL-OVER-3.5",
            family="first_five_total",
            line=Decimal("3.5000"),
            total_side="over",
            inning_scope="first_five",
            current_price=Decimal("0.0000"),
            with_position=True,
        )
        session.commit()

        result = settle_paper_trades(session, date(2026, 7, 1), now=settled_at)
        second_result = settle_paper_trades(session, date(2026, 7, 1), now=settled_at)
        session.refresh(trade)
        session.refresh(candidate)
        assert position is not None
        session.refresh(position)
        settlements = list(session.scalars(select(Settlement)))

    assert result["settled"] == 1
    assert result["skipped_not_final"] == 0
    assert result["skipped_first_five_not_complete"] == 0
    assert second_result["settled"] == 0
    assert len(settlements) == 1
    assert trade.status == "settled"
    assert trade.outcome == "win"
    assert trade.current_price == Decimal("1.0000")
    assert trade.exit_price == Decimal("1.0000")
    assert position.status == "settled"
    assert position.current_price == Decimal("1.0000")
    assert candidate.outcome == "win"


def test_first_five_total_loss_settles_before_full_game_final() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        game = MlbGame(
            external_game_id="settle-f5-total-early-loss",
            home_team="Milwaukee Brewers",
            away_team="Cincinnati Reds",
            home_abbreviation="MIL",
            away_abbreviation="CIN",
            scheduled_start=datetime(2026, 7, 1, 19, 10, tzinfo=UTC),
            status="In Progress",
            raw_payload=_linescore_payload([(0, 0), (1, 0), (0, 0), (0, 1), (0, 0)]),
        )
        session.add(game)
        session.flush()
        trade, _candidate, _market, _position = _add_settlement_trade(
            session,
            epoch_id=epoch_id,
            game=game,
            ticker="KXMLBF5TOTAL-26JUL011510CINMIL-OVER-3.5",
            family="first_five_total",
            line=Decimal("3.5000"),
            total_side="over",
            inning_scope="first_five",
        )
        session.commit()

        result = settle_paper_trades(session, date(2026, 7, 1), now=datetime(2026, 7, 1, 22, 0, tzinfo=UTC))
        session.refresh(trade)

    assert result["settled"] == 1
    assert trade.status == "settled"
    assert trade.outcome == "loss"
    assert trade.current_price == Decimal("0.0000")
    assert trade.exit_price == Decimal("0.0000")


def test_first_five_total_waits_when_linescore_incomplete() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        game = MlbGame(
            external_game_id="settle-f5-total-incomplete",
            home_team="Milwaukee Brewers",
            away_team="Cincinnati Reds",
            home_abbreviation="MIL",
            away_abbreviation="CIN",
            scheduled_start=datetime(2026, 7, 1, 19, 10, tzinfo=UTC),
            status="In Progress",
            raw_payload=_linescore_payload([(1, 0), (0, 1), (1, 0), (0, 0)]),
        )
        session.add(game)
        session.flush()
        trade, _candidate, _market, _position = _add_settlement_trade(
            session,
            epoch_id=epoch_id,
            game=game,
            ticker="KXMLBF5TOTAL-26JUL011510CINMIL-OVER-3.5",
            family="first_five_total",
            line=Decimal("3.5000"),
            total_side="over",
            inning_scope="first_five",
        )
        session.commit()

        result = settle_paper_trades(session, date(2026, 7, 1), now=datetime(2026, 7, 1, 21, 0, tzinfo=UTC))
        session.refresh(trade)

    assert result["settled"] == 0
    assert result["skipped_first_five_not_complete"] == 1
    assert result["skipped_not_final"] == 1
    assert result["skip_reasons"]["first_five_not_complete"] == 1
    assert trade.status == "open"
    assert trade.outcome is None


def test_full_game_total_still_waits_until_full_game_final() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        game = MlbGame(
            external_game_id="settle-full-total-open",
            home_team="Milwaukee Brewers",
            away_team="Cincinnati Reds",
            home_abbreviation="MIL",
            away_abbreviation="CIN",
            scheduled_start=datetime(2026, 7, 1, 19, 10, tzinfo=UTC),
            status="In Progress",
            home_score=4,
            away_score=3,
            raw_payload=_linescore_payload([(1, 0), (0, 1), (1, 0), (0, 0), (1, 1)]),
        )
        session.add(game)
        session.flush()
        trade, _candidate, _market, _position = _add_settlement_trade(
            session,
            epoch_id=epoch_id,
            game=game,
            ticker="KXMLBTOTAL-26JUL011510CINMIL-OVER-8.5",
            family="full_game_total",
            line=Decimal("8.5000"),
            total_side="over",
            inning_scope="full_game",
        )
        session.commit()

        result = settle_paper_trades(session, date(2026, 7, 1), now=datetime(2026, 7, 1, 22, 0, tzinfo=UTC))
        session.refresh(trade)

    assert result["settled"] == 0
    assert result["skipped_not_final_full_game"] == 1
    assert result["skipped_not_final"] == 1
    assert result["skip_reasons"]["not_final_full_game"] == 1
    assert trade.status == "open"
    assert trade.outcome is None


def test_first_five_winner_and_spread_settle_before_full_game_final() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        game = MlbGame(
            external_game_id="settle-f5-winner-spread-open",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="In Progress",
            raw_payload=_linescore_payload([(1, 0), (0, 0), (0, 1), (1, 0), (0, 0)]),
        )
        session.add(game)
        session.flush()
        winner_trade, _winner_candidate, _winner_market, _winner_position = _add_settlement_trade(
            session,
            epoch_id=epoch_id,
            game=game,
            ticker="KXMLBF5-26JUL011900SEAPIT-SEA",
            family="first_five_winner",
            selection="SEA",
            inning_scope="first_five",
        )
        spread_trade, _spread_candidate, _spread_market, _spread_position = _add_settlement_trade(
            session,
            epoch_id=epoch_id,
            game=game,
            ticker="KXMLBF5SPREAD-26JUL011900SEAPIT-PIT+1",
            family="first_five_spread",
            line=Decimal("1.0000"),
            selection="PIT",
            inning_scope="first_five",
        )
        session.commit()

        result = settle_paper_trades(session, date(2026, 7, 1), now=datetime(2026, 7, 2, 1, 0, tzinfo=UTC))
        session.refresh(winner_trade)
        session.refresh(spread_trade)

    assert result["settled"] == 2
    assert winner_trade.status == "settled"
    assert winner_trade.outcome == "win"
    assert winner_trade.current_price == Decimal("1.0000")
    assert spread_trade.status == "settled"
    assert spread_trade.outcome == "push"
    assert spread_trade.current_price == spread_trade.entry_price


def test_paper_settlement_handles_spread_total_and_first_five_families() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        game = MlbGame(
            external_game_id="settle-pr3b-families",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="Final",
            home_score=5,
            away_score=3,
            raw_payload={
                "linescore": {
                    "innings": [
                        {"away": {"runs": 1}, "home": {"runs": 0}},
                        {"away": {"runs": 0}, "home": {"runs": 0}},
                        {"away": {"runs": 0}, "home": {"runs": 1}},
                        {"away": {"runs": 1}, "home": {"runs": 1}},
                        {"away": {"runs": 0}, "home": {"runs": 0}},
                    ]
                }
            },
        )
        session.add(game)
        session.flush()

        def add_trade(
            *,
            ticker: str,
            family: str,
            line: Decimal | None = None,
            selection: str | None = None,
            total_side: str | None = None,
            inning_scope: str,
        ) -> None:
            market = KalshiMarket(
                kalshi_market_id=f"KX-{ticker}",
                ticker=ticker,
                title=ticker,
                status="closed",
                market_family=family,
                market_type=family,
                line_value=line,
                selection_code=selection,
                over_under_side=total_side,
                inning_scope=inning_scope,
                settlement_rule_status="paper_supported",
            )
            session.add(market)
            session.flush()
            mapping = _add_candidate_mapping(
                session,
                game,
                market,
                mapping_status="confirmed",
                market_family=family,
                market_type=family,
                line_value=line,
                selection_code=selection,
                over_under_side=total_side,
                inning_scope=inning_scope,
                settlement_rule_status="paper_supported",
            )
            session.flush()
            candidate = ModelCandidate(
                paper_trading_epoch_id=epoch_id,
                mlb_game_id=game.id,
                kalshi_market_id=market.id,
                mapping_id=mapping.id,
                evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
                features={},
                decision="paper_trade",
                market_type=family,
                contract_side="yes",
                training_eligible=False,
                line_value=line,
                selection_code=selection,
                over_under_side=total_side,
                inning_scope=inning_scope,
                settlement_rule_status="paper_supported",
            )
            session.add(candidate)
            session.flush()
            session.add(
                PaperTrade(
                    paper_trading_epoch_id=epoch_id,
                    candidate_id=candidate.id,
                    market_ticker=ticker,
                    contract_side="yes",
                    entry_price=Decimal("0.4000"),
                    current_price=Decimal("0.4000"),
                    quantity=1,
                    entry_time=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
                    status="open",
                    market_family=family,
                    line_value=line,
                    selection_code=selection,
                    over_under_side=total_side,
                    inning_scope=inning_scope,
                    settlement_rule_status="paper_supported",
                    training_eligible=False,
                )
            )

        add_trade(
            ticker="KXMLBSPREAD-26JUL011900SEAPIT-PIT-1.5",
            family="full_game_spread",
            line=Decimal("-1.5000"),
            selection="PIT",
            inning_scope="full_game",
        )
        add_trade(
            ticker="KXMLBTOTAL-26JUL011900SEAPIT-OVER-8",
            family="full_game_total",
            line=Decimal("8.0000"),
            total_side="over",
            inning_scope="full_game",
        )
        add_trade(
            ticker="KXMLBF5-26JUL011900SEAPIT-TIE",
            family="first_five_winner",
            selection="TIE",
            inning_scope="first_five",
        )
        add_trade(
            ticker="KXMLBF5TOTAL-26JUL011900SEAPIT-OVER-4.5",
            family="first_five_total",
            line=Decimal("4.5000"),
            total_side="over",
            inning_scope="first_five",
        )
        session.commit()

        result = settle_paper_trades(session, date(2026, 7, 1), now=datetime(2026, 7, 2, 4, 0, tzinfo=UTC))
        trades = list(session.scalars(select(PaperTrade).order_by(PaperTrade.market_family)))

    assert result["settled"] == 4
    assert result["skipped_missing_f5_linescore"] == 0
    assert {trade.outcome for trade in trades} == {"loss", "push", "win"}
    assert next(trade for trade in trades if trade.market_family == "full_game_spread").outcome == "win"
    assert next(trade for trade in trades if trade.market_family == "full_game_total").outcome == "push"
    assert next(trade for trade in trades if trade.market_family == "first_five_winner").outcome == "win"
    assert next(trade for trade in trades if trade.market_family == "first_five_total").outcome == "loss"


def test_paper_settlement_skips_first_five_without_linescore() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        game = MlbGame(
            external_game_id="settle-missing-f5-linescore",
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
            kalshi_market_id="KX-MISSING-F5",
            ticker="KXMLBF5-26JUL011900SEAPIT-TIE",
            title="First five tie",
            status="closed",
            market_family="first_five_winner",
            market_type="first_five_winner",
            selection_code="TIE",
            inning_scope="first_five",
            settlement_rule_status="paper_supported",
        )
        session.add_all([game, market])
        mapping = _add_candidate_mapping(
            session,
            game,
            market,
            mapping_status="confirmed",
            market_family="first_five_winner",
            market_type="first_five_winner",
            selection_code="TIE",
            inning_scope="first_five",
            settlement_rule_status="paper_supported",
        )
        session.flush()
        candidate = ModelCandidate(
            paper_trading_epoch_id=epoch_id,
            mlb_game_id=game.id,
            kalshi_market_id=market.id,
            mapping_id=mapping.id,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="paper_trade",
            market_type="first_five_winner",
            contract_side="yes",
            selection_code="TIE",
            inning_scope="first_five",
            settlement_rule_status="paper_supported",
        )
        session.add(candidate)
        session.flush()
        trade = PaperTrade(
            paper_trading_epoch_id=epoch_id,
            candidate_id=candidate.id,
            market_ticker=market.ticker,
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            entry_time=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            status="open",
            market_family="first_five_winner",
            selection_code="TIE",
            inning_scope="first_five",
            settlement_rule_status="paper_supported",
        )
        session.add(trade)
        session.commit()

        result = settle_paper_trades(session, date(2026, 7, 1), now=datetime(2026, 7, 2, 4, 0, tzinfo=UTC))
        session.refresh(trade)

    assert result["skipped_missing_f5_linescore"] == 1
    assert result["candidate_labels_skipped_missing_f5_linescore"] == 1
    assert trade.status == "open"
    assert trade.outcome is None


def test_dashboard_summary_uses_labels_snapshots_and_settled_performance() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    opened_at = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
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
            paper_trading_epoch_id=epoch_id,
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
                    paper_trading_epoch_id=epoch_id,
                    candidate_id=candidate.id,
                    market_ticker=market.ticker,
                    contract_side="yes",
                    entry_price=Decimal("0.4000"),
                    current_price=Decimal("0.5500"),
                    current_price_updated_at=opened_at + timedelta(minutes=10),
                    quantity=1,
                    entry_time=opened_at,
                    status="open",
                    contract_display="FULL GAME WINNER - SEA @ PIT - PIT",
                    market_display="FULL GAME WINNER - SEA @ PIT - PIT",
                    selection_display="PIT",
                    matchup_display="SEA @ PIT",
                ),
                PaperTrade(
                    paper_trading_epoch_id=epoch_id,
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
                    paper_trading_epoch_id=epoch_id,
                    captured_at=opened_at,
                    cash_balance=Decimal("999.60"),
                    portfolio_value=Decimal("1000.15"),
                    source="paper",
                    snapshot_type="test",
                ),
            ]
        )
        session.commit()

        summary = dashboard.dashboard_summary_from_db(session, include_pre_observation=True)

    assert summary.cash_balance == 999.6
    assert summary.portfolio_value == 1000.15
    assert summary.performance.record == "1-0-0"
    assert summary.performance.profit_loss == 0.6
    assert summary.paper_starting_balance == 500.0
    assert summary.positions[0].market == "FULL GAME WINNER - SEA @ PIT - PIT"
    assert summary.positions[0].market_ticker == "KXMLBGAME-26JUL011900SEAPIT-PIT"
    assert summary.positions[0].game_status == "NOT STARTED"
    assert summary.positions[0].current_price_updated_at_display is not None
    assert summary.positions[0].time_entered_display is not None
    assert "EDT" in summary.positions[0].time_entered_display


def test_dashboard_trade_caps_use_today_prediction_run(monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "today_eastern", lambda: date(2026, 7, 2))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        session.add(
            ModelPredictionRun(
                paper_trading_epoch_id=epoch_id,
                started_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
                target_date=date(2026, 7, 1),
                status="completed",
                trades_created=9,
                trade_policy={"paper_max_trades_per_slate": 20},
                summary={"cap_counts": {"no_trade_slate_cap": 4}},
            )
        )
        session.commit()

        summary_before_today_run = dashboard.dashboard_summary_from_db(session)

        session.add(
            ModelPredictionRun(
                paper_trading_epoch_id=epoch_id,
                started_at=datetime(2026, 7, 2, 16, 0, tzinfo=UTC),
                target_date=date(2026, 7, 2),
                status="completed",
                trades_created=2,
                trade_policy={"paper_max_trades_per_slate": 10},
                summary={"cap_counts": {"no_trade_slate_cap": 1}},
            )
        )
        session.commit()

        summary_after_today_run = dashboard.dashboard_summary_from_db(session)

    assert summary_before_today_run.model_status.trade_policy == {}
    assert summary_before_today_run.model_status.trade_caps_used == {"paper_trades": 0}
    assert summary_after_today_run.model_status.trade_policy == {"paper_max_trades_per_slate": 10}
    assert summary_after_today_run.model_status.trade_caps_used == {"no_trade_slate_cap": 1, "paper_trades": 2}


def test_dashboard_feature_status_uses_active_feature_source(monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "today_eastern", lambda: date(2026, 7, 1))
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured_at = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        session.add_all(
            [
                MlbFeatureSnapshot(
                    mlb_game_id=1,
                    target_date=date(2026, 7, 1),
                    source="mature_mlb_features_v1",
                    captured_at=captured_at + timedelta(minutes=5),
                    data_quality=Decimal("0.1000"),
                    source_statuses={"park_weather": "missing"},
                    features={},
                ),
                MlbFeatureSnapshot(
                    mlb_game_id=1,
                    target_date=date(2026, 7, 1),
                    source=features.FEATURE_VERSION,
                    captured_at=captured_at,
                    data_quality=Decimal("0.5000"),
                    source_statuses={
                        "lineup": {"home": "available", "away": "available"},
                        "park_weather": "partial",
                    },
                    features={},
                ),
                MlbFeatureSnapshot(
                    mlb_game_id=2,
                    target_date=date(2026, 7, 1),
                    source=features.FEATURE_VERSION,
                    captured_at=captured_at + timedelta(minutes=1),
                    data_quality=Decimal("0.3000"),
                    source_statuses={
                        "lineup": {"home": "missing", "away": "missing"},
                        "park_weather": "missing",
                    },
                    features={},
                ),
            ]
        )
        session.commit()

        summary = dashboard.dashboard_summary_from_db(session, include_pre_observation=True)

    assert summary.model_status.feature_completeness["park_weather"] == {"partial": 1, "missing": 1}
    assert summary.model_status.feature_completeness["lineup"] == {"available": 1, "missing": 1}
    assert summary.model_status.lineup_status == "partial"
    assert summary.model_status.weather_status == "partial"
    assert "LINEUP MISSING OR DEGRADED" in summary.model_status.critical_module_warnings
    assert "PARK_WEATHER MISSING OR DEGRADED" in summary.model_status.critical_module_warnings


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
        _add_candidate_mapping(session, game, market)
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))
        feature_snapshot = session.scalar(select(FeatureSnapshot))

    assert result["model_version"] == modeling.MATURE_MODEL_TAG
    assert candidate is not None
    assert candidate.model_probability != Decimal("0.500000")
    assert candidate.model_version_tag == modeling.MATURE_MODEL_TAG
    assert candidate.feature_version == features.FEATURE_VERSION
    assert candidate.market_type == "full_game_winner"
    assert candidate.contract_display == "YES ON PITTSBURGH PIRATES FULL GAME WINNER"
    assert candidate.features["park_weather"]["source_status"] == "missing"
    assert candidate.scoring_rationale["uses_market_price"] is False
    assert feature_snapshot is not None
    assert feature_snapshot.features["data_quality"] > 0


def test_candidate_diagnostics_include_defense_module_status(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    def team_daily(team_code: str) -> TeamDailyFeature:
        return TeamDailyFeature(
            target_date=date(2026, 7, 1),
            team_code=team_code,
            captured_at=now,
            source=features.MLB_STATS_SOURCE,
            source_status="available",
            confidence=Decimal("0.8500"),
            completeness=Decimal("0.8000"),
            features={
                "defense_season": {
                    "source": features.MLB_STATS_SOURCE,
                    "source_status": "available",
                    "reason": "team defense season from MLB Stats API fielding game logs",
                    "fielding_percentage": 0.985,
                    "errors_per_game": 0.4,
                    "double_plays_per_game": 0.9,
                    "completeness": 0.65,
                }
            },
        )

    def team_recent(team_code: str) -> TeamRecentFeature:
        return TeamRecentFeature(
            target_date=date(2026, 7, 1),
            team_code=team_code,
            window_days=14,
            captured_at=now,
            source=features.MLB_STATS_SOURCE,
            source_status="available",
            sample_size=10,
            confidence=Decimal("0.8000"),
            completeness=Decimal("0.7500"),
            features={
                "defense_recent": {
                    "source": features.MLB_STATS_SOURCE,
                    "source_status": "available",
                    "reason": "team defense recent from MLB Stats API fielding game logs over 14 days",
                    "fielding_percentage": 0.982,
                    "errors_per_game": 0.5,
                    "completeness": 0.60,
                }
            },
        )

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="candidate-defense-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-CANDIDATE-DEFENSE",
            ticker="KXMLBGAME-26JUL011900SEAPIT-PIT",
            title="Will the Pittsburgh Pirates win the game against the Seattle Mariners?",
            status="open",
            occurrence_datetime=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
            market_price_updated_at=now,
        )
        session.add_all(
            [
                game,
                market,
                team_daily("PIT"),
                team_daily("SEA"),
                team_recent("PIT"),
                team_recent("SEA"),
            ]
        )
        _add_candidate_mapping(session, game, market)
        session.commit()

        candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))

    assert candidate is not None
    defense = candidate.features["defense_catcher"]
    assert defense["source_status"] == "partial"
    assert defense["home"]["team_defense_season"]["source_status"] == "available"
    raw_quality = candidate.gate_diagnostics["quality_decomposition"]["raw_feature_snapshot"]
    paper_quality = candidate.gate_diagnostics["quality_decomposition"]["paper_observation"]
    assert raw_quality["module_status"]["defense_catcher"] == "partial"
    assert raw_quality["quality_weight_by_module"]["defense_catcher"] > 0
    assert raw_quality["quality_contribution_by_module"]["defense_catcher"] > 0
    assert raw_quality["quality_penalty_by_module"]["defense_catcher"] > 0
    assert paper_quality["module_status"]["defense_catcher"] == "partial"
    assert "defense_catcher" in paper_quality["quality_contribution_by_module"]
    assert "defense_catcher" in paper_quality["quality_penalty_by_module"]


def _add_governance_candidate(
    session: Session,
    *,
    epoch_id: int,
    game_id: int,
    target_date: date,
    evaluated_at: datetime,
    resolved_at: datetime,
    outcome: str = "win",
) -> ModelCandidate:
    candidate = ModelCandidate(
        paper_trading_epoch_id=epoch_id,
        mlb_game_id=game_id,
        evaluated_at=evaluated_at,
        features={},
        probability=Decimal("0.550000"),
        probability_calibrated=Decimal("0.550000"),
        target_date=target_date,
        fee_estimate=Decimal("0.010000"),
        price_status="fresh_executable",
        time_to_start_minutes=420,
        decision="candidate_only",
        outcome=outcome,
        resolved_at=resolved_at,
        model_version_tag=modeling.MATURE_MODEL_TAG,
        feature_version=features.FEATURE_VERSION,
        training_eligible=True,
        market_family="full_game_winner",
    )
    session.add(candidate)
    return candidate


def test_model_governance_skips_training_and_records_runs_when_samples_are_too_small() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        result = run_model_governance(session, now=datetime(2026, 7, 1, 12, 0, tzinfo=UTC))
        training = session.scalar(select(TrainingRun))
        calibration = session.scalar(select(CalibrationRun))
        status = modeling.governance_status(session)

    assert result["status"] == "skipped_insufficient_samples"
    assert result["resolved_samples"] == 0
    assert training is not None
    assert calibration is not None
    assert training.status == "skipped_insufficient_samples"
    assert calibration.status == "skipped_insufficient_samples"
    assert calibration.metrics["paper_trading_epoch_id"] == result["paper_trading_epoch_id"]
    assert status["calibration_status"] == "skipped_insufficient_samples"
    assert "INSUFFICIENT_MATURE_RESOLVED_SAMPLES" in training.metrics["reason"]


def test_model_governance_clean_cutoff_excludes_pre_cutoff_samples(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_MIN_SAMPLES_TRAIN", "3")
    monkeypatch.setenv("MODEL_MIN_SAMPLES_CALIBRATE", "3")
    monkeypatch.setenv("MODEL_MIN_SAMPLES_PROMOTE", "99")
    monkeypatch.setenv("MODEL_GOVERNANCE_CLEAN_START_AT", "2026-07-02T00:00:00-04:00")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        pre_clean_game = MlbGame(
            external_game_id="governance-clean-pre-cutoff",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="Final",
        )
        clean_game = MlbGame(
            external_game_id="governance-clean-post-cutoff",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 2, 23, 0, tzinfo=UTC),
            status="Final",
        )
        session.add_all([pre_clean_game, clean_game])
        session.flush()
        pre_clean_candidate = _add_governance_candidate(
            session,
            epoch_id=epoch_id,
            game_id=pre_clean_game.id,
            target_date=date(2026, 7, 1),
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            resolved_at=datetime(2026, 7, 2, 4, 0, tzinfo=UTC),
            outcome="win",
        )
        clean_candidates = [
            _add_governance_candidate(
                session,
                epoch_id=epoch_id,
                game_id=clean_game.id,
                target_date=date(2026, 7, 2),
                evaluated_at=datetime(2026, 7, 2, 16, index, tzinfo=UTC),
                resolved_at=datetime(2026, 7, 3, 4, index, tzinfo=UTC),
                outcome=outcome,
            )
            for index, outcome in enumerate(["win", "loss"], start=1)
        ]
        session.commit()

        result = run_model_governance(session, now=datetime(2026, 7, 3, 12, 0, tzinfo=UTC))
        dataset = session.get(ModelTrainingDataset, result["training_dataset_id"])
        challenger = session.scalar(select(ModelParameterVersion).where(ModelParameterVersion.role == "challenger"))
        status = modeling.governance_status(session)
        clean_candidate_ids = [candidate.id for candidate in clean_candidates]
        pre_clean_candidate_id = pre_clean_candidate.id

    assert result["status"] == "skipped_insufficient_samples"
    assert result["raw_resolved_mature_samples"] == 3
    assert result["clean_resolved_mature_samples"] == 2
    assert result["pre_clean_excluded_samples"] == 1
    assert result["clean_filter_exclusion_counts"] == {"target_date_before_clean_start": 1}
    assert challenger is None
    assert dataset is not None
    assert dataset.sample_count == 2
    assert dataset.candidate_ids == clean_candidate_ids
    assert pre_clean_candidate_id not in dataset.candidate_ids
    assert status["raw_resolved_mature_samples"] == 3
    assert status["clean_resolved_mature_samples"] == 2
    assert status["governance_training_policy"] == modeling.GOVERNANCE_CLEAN_TRAINING_POLICY
    assert status["governance_parameter_registry"]["governed_now_count"] >= 1


def test_governance_status_uses_aggregate_counts_without_candidate_loader(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_GOVERNANCE_CLEAN_START_AT", "2026-07-02T00:00:00-04:00")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        game = MlbGame(
            external_game_id="governance-status-aggregate",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 2, 23, 0, tzinfo=UTC),
            status="Final",
        )
        mismatched_game = MlbGame(
            external_game_id="governance-status-aggregate-mismatched-date",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 4, 23, 0, tzinfo=UTC),
            status="Final",
        )
        session.add_all([game, mismatched_game])
        session.flush()
        _add_governance_candidate(
            session,
            epoch_id=epoch_id,
            game_id=game.id,
            target_date=date(2026, 7, 2),
            evaluated_at=datetime(2026, 7, 2, 16, 0, tzinfo=UTC),
            resolved_at=datetime(2026, 7, 3, 4, 0, tzinfo=UTC),
        )
        _add_governance_candidate(
            session,
            epoch_id=epoch_id,
            game_id=mismatched_game.id,
            target_date=date(2026, 7, 2),
            evaluated_at=datetime(2026, 7, 2, 16, 5, tzinfo=UTC),
            resolved_at=datetime(2026, 7, 3, 4, 5, tzinfo=UTC),
            outcome="loss",
        )
        session.commit()

        def fail_full_loader(*_args, **_kwargs):
            raise AssertionError("governance status must not build the training candidate dataset")

        monkeypatch.setattr(modeling, "_resolved_mature_candidates", fail_full_loader)
        status = modeling.governance_status(session)

    assert status["raw_resolved_mature_samples"] == 1
    assert status["clean_resolved_mature_samples"] == 1
    assert status["pre_clean_excluded_samples"] == 0


def test_dashboard_summary_does_not_deserialize_candidate_json_for_compact_counts(monkeypatch) -> None:
    def reject_model_candidate_json(value: object) -> object:
        raw = value.decode("utf-8") if isinstance(value, bytes) else str(value)
        if "candidate-large-json" in raw:
            raise AssertionError("compact dashboard must not deserialize ModelCandidate JSON columns")
        return json.loads(raw)

    monkeypatch.setenv("MODEL_GOVERNANCE_CLEAN_START_AT", "2026-07-02T00:00:00-04:00")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:", json_deserializer=reject_model_candidate_json)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        game = MlbGame(
            external_game_id="dashboard-compact-candidate-json",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 2, 23, 0, tzinfo=UTC),
            status="Final",
        )
        session.add(game)
        session.flush()
        session.add(
            ModelCandidate(
                paper_trading_epoch_id=epoch_id,
                mlb_game_id=game.id,
                evaluated_at=datetime(2026, 7, 2, 16, 0, tzinfo=UTC),
                features={"marker": "candidate-large-json"},
                scoring_rationale={"marker": "candidate-large-json"},
                probability=Decimal("0.550000"),
                probability_calibrated=Decimal("0.550000"),
                target_date=date(2026, 7, 2),
                fee_estimate=Decimal("0.010000"),
                price_status="fresh_executable",
                time_to_start_minutes=420,
                decision="no_trade_low_quality",
                outcome="win",
                resolved_at=datetime(2026, 7, 3, 4, 0, tzinfo=UTC),
                model_version_tag=modeling.MATURE_MODEL_TAG,
                feature_version=features.FEATURE_VERSION,
                training_eligible=True,
                market_family="full_game_winner",
                data_quality=Decimal("0.5000"),
            )
        )
        session.commit()

        summary = dashboard.dashboard_summary_from_db(session, date(2026, 7, 2))

    assert summary.model_status.raw_resolved_mature_samples == 1
    assert summary.decision_breakdown_by_family["full_game_winner"]["no_trade_low_quality"] == 1


def test_pr3p2_status_query_indexes_are_registered() -> None:
    candidate_indexes = {index.name for index in ModelCandidate.__table__.indexes}
    job_indexes = {index.name for index in JobRun.__table__.indexes}
    feature_indexes = {index.name for index in MlbFeatureSnapshot.__table__.indexes}
    balance_indexes = {index.name for index in BalanceSnapshot.__table__.indexes}

    assert "ix_model_candidates_epoch_governance_counts" in candidate_indexes
    assert "ix_model_candidates_epoch_decision_scope" in candidate_indexes
    assert "ix_job_runs_epoch_name_started_id" in job_indexes
    assert "ix_mlb_feature_snapshots_date_source_captured" in feature_indexes
    assert "ix_balance_snapshots_epoch_captured" in balance_indexes


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
    assert result["active_model_version"] == modeling.MATURE_MODEL_TAG
    assert [version.version_tag for version in active_versions] == [modeling.MATURE_MODEL_TAG]
    assert next(version for version in versions if version.version_tag == "legacy_champion").role == "inactive"


def test_model_governance_counts_old_kxmlb_market_type_as_full_game_winner() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        market = KalshiMarket(
            kalshi_market_id="KX-OLD-MONEYLINE",
            ticker="KXMLBGAME-26JUL011900SEAPIT-PIT",
            title="Will Pittsburgh win?",
            status="closed",
        )
        session.add(market)
        session.flush()
        candidate = ModelCandidate(
            kalshi_market_id=market.id,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="no_trade_edge_too_low",
            market_type="full_game_moneyline",
            outcome="win",
            outcome_source="mlb_results_sync",
            resolved_at=datetime(2026, 7, 2, 4, 0, tzinfo=UTC),
        )
        session.add(candidate)
        session.commit()

        result = run_model_governance(session, now=datetime(2026, 7, 2, 12, 0, tzinfo=UTC))
        session.refresh(candidate)
        training = session.scalar(select(TrainingRun))

    assert result["resolved_samples"] == 0
    assert candidate.market_type == "full_game_moneyline"
    assert training is not None
    assert training.candidate_count == 0


def test_model_governance_counts_samples_after_settlement_labels_candidates() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        game = MlbGame(
            external_game_id="governance-after-settlement-1",
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
            kalshi_market_id="KX-GOV-SETTLE",
            ticker="KXMLBGAME-26JUL011900SEAPIT-PIT",
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
            paper_trading_epoch_id=epoch_id,
            mlb_game_id=game.id,
            kalshi_market_id=market.id,
            mapping_id=mapping.id,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="candidate_only",
            market_type="full_game_moneyline",
            contract_side="yes",
        )
        session.add(candidate)
        session.commit()

        settlement_result = settle_paper_trades(session, date(2026, 7, 1), now=datetime(2026, 7, 2, 4, 0, tzinfo=UTC))
        governance_result = run_model_governance(session, now=datetime(2026, 7, 2, 12, 0, tzinfo=UTC))
        session.refresh(candidate)

    assert settlement_result["candidate_labels_created"] == 1
    assert candidate.outcome == "win"
    assert candidate.market_type == "full_game_winner"
    assert governance_result["resolved_samples"] == 0


def test_model_governance_defaults_to_active_epoch_when_scope_omitted(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_GOVERNANCE_CLEAN_START_AT", "2026-07-01T00:00:00-04:00")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        archived = PaperTradingEpoch(
            epoch_key="archived-governance-default",
            display_name="ARCHIVED GOVERNANCE DEFAULT",
            status="archived",
            mode="paper",
            starting_balance=Decimal("1000.00"),
            started_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            archived_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
        )
        game = MlbGame(
            external_game_id="governance-default-epoch",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="Final",
        )
        session.add_all([archived, game])
        session.flush()
        active_id = active.id
        archived_id = archived.id
        for epoch_id, outcome in ((active_id, "win"), (archived_id, "loss")):
            session.add(
                ModelCandidate(
                    paper_trading_epoch_id=epoch_id,
                    mlb_game_id=game.id,
                    evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
                    features={},
                    probability=Decimal("0.600000"),
                    probability_calibrated=Decimal("0.600000"),
                    target_date=date(2026, 7, 1),
                    fee_estimate=Decimal("0.010000"),
                    price_status="fresh_executable",
                    time_to_start_minutes=420,
                    decision="candidate_only",
                    outcome=outcome,
                    resolved_at=datetime(2026, 7, 2, 4, 0, tzinfo=UTC),
                    model_version_tag=modeling.MATURE_MODEL_TAG,
                    feature_version=features.FEATURE_VERSION,
                    training_eligible=True,
                    market_family="full_game_winner",
                )
            )
        session.commit()

        result = run_model_governance(session, now=datetime(2026, 7, 2, 12, 0, tzinfo=UTC))
        training = session.get(TrainingRun, result["training_run_id"])

    assert result["resolved_samples"] == 1
    assert result["paper_trading_epoch_id"] == active_id
    assert training is not None
    assert training.candidate_count == 1


def test_model_governance_status_counts_active_epoch_only(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_GOVERNANCE_CLEAN_START_AT", "2026-07-01T00:00:00-04:00")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        active = get_or_create_active_paper_epoch(session, starting_balance=Decimal("500.00"))
        archived = PaperTradingEpoch(
            epoch_key="archived-governance-status",
            display_name="ARCHIVED GOVERNANCE STATUS",
            status="archived",
            mode="paper",
            starting_balance=Decimal("1000.00"),
            started_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            archived_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
        )
        game = MlbGame(
            external_game_id="governance-status-epoch",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="Final",
        )
        session.add_all([archived, game])
        session.flush()
        active_id = active.id
        archived_id = archived.id
        clean_window = modeling._governance_clean_training_window()
        active_clean_metrics = {
            "paper_trading_epoch_id": active_id,
            "governance_training_policy": clean_window["policy"],
            "clean_training_start_at": clean_window["start_at_utc"],
        }
        archived_clean_metrics = {
            "paper_trading_epoch_id": archived_id,
            "governance_training_policy": clean_window["policy"],
            "clean_training_start_at": clean_window["start_at_utc"],
        }
        for epoch_id, outcome in ((active_id, "win"), (archived_id, "loss")):
            session.add(
                ModelCandidate(
                    paper_trading_epoch_id=epoch_id,
                    mlb_game_id=game.id,
                    evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
                    features={},
                    probability=Decimal("0.600000"),
                    probability_calibrated=Decimal("0.600000"),
                    target_date=date(2026, 7, 1),
                    fee_estimate=Decimal("0.010000"),
                    price_status="fresh_executable",
                    time_to_start_minutes=420,
                    decision="candidate_only",
                    outcome=outcome,
                    resolved_at=datetime(2026, 7, 2, 4, 0, tzinfo=UTC),
                    model_version_tag=modeling.MATURE_MODEL_TAG,
                    feature_version=features.FEATURE_VERSION,
                    training_eligible=True,
                    market_family="full_game_winner",
                )
            )
        active_training = TrainingRun(
            started_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 7, 2, 12, 1, tzinfo=UTC),
            status="active_epoch_training",
            candidate_count=1,
            metrics=active_clean_metrics,
        )
        archived_training = TrainingRun(
            started_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 7, 3, 12, 1, tzinfo=UTC),
            status="archived_epoch_training",
            candidate_count=1,
            metrics=archived_clean_metrics,
        )
        legacy_active_training = TrainingRun(
            started_at=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 7, 4, 12, 1, tzinfo=UTC),
            status="legacy_active_training_without_clean_policy",
            candidate_count=9,
            metrics={"paper_trading_epoch_id": active_id},
        )
        active_calibration = CalibrationRun(
            started_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 7, 2, 12, 1, tzinfo=UTC),
            status="active_epoch_calibration",
            method="test",
            metrics=active_clean_metrics,
        )
        archived_calibration = CalibrationRun(
            started_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 7, 3, 12, 1, tzinfo=UTC),
            status="archived_epoch_calibration",
            method="test",
            metrics=archived_clean_metrics,
        )
        session.add_all(
            [active_training, archived_training, legacy_active_training, active_calibration, archived_calibration]
        )
        session.flush()
        session.add_all(
            [
                ModelThresholdVersion(
                    version_tag="active_epoch_threshold",
                    role="evaluation",
                    status="recorded",
                    is_active=False,
                    created_at_snapshot=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
                    source_training_run_id=active_training.id,
                    thresholds={"policy": "active_epoch_threshold"},
                    metrics=active_clean_metrics,
                ),
                ModelThresholdVersion(
                    version_tag="archived_epoch_threshold",
                    role="evaluation",
                    status="recorded",
                    is_active=False,
                    created_at_snapshot=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
                    source_training_run_id=archived_training.id,
                    thresholds={"policy": "archived_epoch_threshold"},
                    metrics=archived_clean_metrics,
                ),
            ]
        )
        session.commit()

        result = modeling.governance_status(session)
        summary = dashboard.dashboard_summary_from_db(session)

    assert result["paper_trading_epoch_id"] == active_id
    assert result["resolved_mature_samples"] == 1
    assert result["training_eligible_count"] == 1
    assert result["last_governance_status"] == "active_epoch_training"
    assert result["calibration_status"] == "active_epoch_calibration"
    assert result["trade_threshold_policy"] == {"policy": "active_epoch_threshold"}
    assert result["ignored_pre_clean_artifacts"]["training"]["ignored_count"] == 1
    assert summary.model_status.last_governance_status == "active_epoch_training"
    assert summary.model_status.calibration_status == "active_epoch_calibration"
    assert summary.model_status.trade_threshold_policy == {"policy": "active_epoch_threshold"}
    assert summary.model_status.raw_resolved_mature_samples == 1
    assert summary.model_status.clean_resolved_mature_samples == 1
    assert summary.model_status.ignored_pre_clean_artifacts["training"]["ignored_count"] == 1


def test_pr3c_feature_sync_records_source_statuses_and_no_umpire_fields(monkeypatch) -> None:
    _stub_public_feature_network(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="feature-sync-pr3c-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
            raw_payload={"venue": {"id": 31, "name": "PNC Park"}},
        )
        session.add(game)
        session.commit()

        result = features.sync_mlb_features(session, date(2026, 7, 1))
        snapshot = session.scalar(select(MlbFeatureSnapshot))

    assert result["feature_version"] == features.FEATURE_VERSION
    assert snapshot is not None
    assert snapshot.source_statuses["lineup"] == {"home": "missing", "away": "missing"}
    assert snapshot.source_statuses["park_weather"] == "partial"
    assert snapshot.features["park_weather"]["park"]["park_factor"] == 0.98
    assert "umpire" not in " ".join(snapshot.features.keys()).lower()


def test_feature_coverage_and_detail_filter_to_active_feature_version() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured_at = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        session.add_all(
            [
                MlbFeatureSnapshot(
                    mlb_game_id=1,
                    target_date=date(2026, 7, 1),
                    source="mature_mlb_features_v1",
                    captured_at=captured_at,
                    data_quality=Decimal("0.1000"),
                    source_statuses={"park_weather": "missing"},
                    features={"feature_version": "mature_mlb_features_v1"},
                ),
                MlbFeatureSnapshot(
                    mlb_game_id=1,
                    target_date=date(2026, 7, 1),
                    source=features.FEATURE_VERSION,
                    captured_at=captured_at,
                    data_quality=Decimal("0.5000"),
                    source_statuses={"park_weather": "partial"},
                    features={"feature_version": features.FEATURE_VERSION},
                ),
            ]
        )
        session.commit()

        coverage = features.feature_coverage(session, date(2026, 7, 1))
        detail = features.feature_detail(session, date(2026, 7, 1))

    assert coverage["snapshot_count"] == 1
    assert coverage["items"][0]["source"] == features.FEATURE_VERSION
    assert detail["count"] == 1
    assert detail["items"][0]["source"] == features.FEATURE_VERSION


def test_feature_coverage_detail_and_source_status_include_17_module_summary(monkeypatch) -> None:
    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {
            "available": False,
            "version": None,
            "module_path": None,
            "import_error": {"error_type": "ImportError", "message": "pybaseball unavailable in this test"},
        },
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured_at = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    feature_payload = {
        module_name: {
            "source_status": "available",
            "source": features.MLB_STATS_SOURCE,
            "reason": "source populated",
        }
        for module_name in features.CORE_MODULES
    }
    feature_payload["lineup"] = {
        "home": {
            "source_status": "available",
            "source": features.MLB_STATS_SOURCE,
            "reason": "confirmed lineup",
        },
        "away": {
            "source_status": "available",
            "source": features.MLB_STATS_SOURCE,
            "reason": "confirmed lineup",
        },
    }
    feature_payload["injuries"] = {
        "home": {
            "source_status": "missing",
            "source": "optional_provider",
            "reason": "injury provider not configured",
        },
        "away": {
            "source_status": "missing",
            "source": "optional_provider",
            "reason": "injury provider not configured",
        },
    }
    feature_payload["park_weather"] = {
        "source_status": "partial",
        "source": features.DERIVED_SOURCE,
        "reason": "static park profile available; weather missing",
        "park": {
            "source_status": "available",
            "source": features.STATIC_SOURCE,
            "reason": "park profile found",
        },
        "weather": {
            "source_status": "missing",
            "source": features.OPEN_METEO_SOURCE,
            "reason": "weather forecast unavailable",
        },
    }
    feature_payload["data_quality_summary"] = {
        "module_scores": {module_name: 0.8 for module_name in features.CORE_MODULES}
    }

    with Session(engine) as session:
        session.add(
            MlbFeatureSnapshot(
                mlb_game_id=1,
                target_date=date(2026, 7, 1),
                source=features.FEATURE_VERSION,
                captured_at=captured_at,
                data_quality=Decimal("0.7500"),
                source_statuses=features._source_statuses(feature_payload),
                features=feature_payload,
            )
        )
        session.commit()

        coverage = features.feature_coverage(session, date(2026, 7, 1))
        detail = features.feature_detail(session, date(2026, 7, 1))
        report = features.source_status_report(session)

    assert len(coverage["core_modules"]) == 17
    assert set(coverage["module_completeness"]) == set(features.CORE_MODULES)
    assert coverage["completeness_summary"]["core_module_count"] == 17
    assert coverage["completeness_summary"]["snapshot_count"] == 1
    assert coverage["module_completeness"]["lineup"]["available"] == 1
    assert coverage["module_completeness"]["injuries"]["missing"] == 1
    assert coverage["module_completeness"]["park_weather"]["partial"] == 1
    assert any(
        "weather forecast unavailable" in reason
        for reason in coverage["module_completeness"]["park_weather"]["reasons"]
    )
    assert detail["items"][0]["module_completeness"]["park_weather"]["status"] == "partial"
    assert detail["items"][0]["module_completeness"]["injuries"]["status"] == "missing"
    assert report["latest_feature_completeness"]["summary"]["core_module_count"] == 17
    assert report["latest_feature_completeness"]["modules"]["lineup"]["available"] == 1


def _fielding_test_game() -> MlbGame:
    return MlbGame(
        external_game_id="defense-fielding-1",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="scheduled",
        raw_payload={
            "venue": {"id": 31, "name": "PNC Park"},
            "teams": {
                "home": {"team": {"id": 134, "name": "Pittsburgh Pirates"}},
                "away": {"team": {"id": 136, "name": "Seattle Mariners"}},
            },
        },
    )


def _team_log_rows(group: str, count: int = 15) -> dict[str, object]:
    rows = []
    for index in range(count):
        if group == "hitting":
            stat = {
                "runs": 4,
                "hits": 8,
                "homeRuns": 1,
                "baseOnBalls": 3,
                "strikeOuts": 7,
                "atBats": 34,
                "plateAppearances": 38,
                "totalBases": 13,
            }
        elif group == "pitching":
            stat = {
                "inningsPitched": "9.0",
                "runs": 3,
                "earnedRuns": 3,
                "hits": 7,
                "homeRuns": 1,
                "baseOnBalls": 2,
                "strikeOuts": 8,
                "battersFaced": 36,
                "numberOfPitches": 142,
                "saves": 1,
                "holds": 1,
                "blownSaves": 0,
                "saveOpportunities": 1,
                "gamesFinished": 1,
            }
        else:
            stat = {
                "innings": "9.0",
                "errors": 1 if index % 5 == 0 else 0,
                "assists": 12,
                "putOuts": 27,
                "chances": 40,
                "doublePlays": 1,
                "passedBalls": 0,
                "wildPitches": 1 if index % 7 == 0 else 0,
                "stolenBases": 1,
                "caughtStealing": 1 if index % 3 == 0 else 0,
            }
        rows.append({"date": f"2026-06-{30 - index:02d}", "stat": stat})
    return {"stats": [{"splits": rows}]}


def test_mlb_stats_fielding_populates_baseline_defense_and_source_health(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    monkeypatch.setattr(features, "_fetch_open_meteo", lambda *_args, **_kwargs: {"hourly": {"time": []}})

    class FakeMLBStatsClient:
        def get_team_game_log_stats(self, team_id: str, group: str, season: int):
            return _team_log_rows(group)

        def get_team_stat_splits(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_pitcher_game_log_stats(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {}

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    def hydrate_schedule(session: Session, *_args, **_kwargs) -> int:
        session.add(_fielding_test_game())
        session.flush()
        return 1

    monkeypatch.setattr(features, "_hydrate_schedule_window", hydrate_schedule)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            result = features.sync_mlb_features(session, date(2026, 7, 1))
            team = session.scalar(
                select(TeamDailyFeature)
                .where(TeamDailyFeature.team_code == "PIT")
                .where(TeamDailyFeature.source == features.MLB_STATS_SOURCE)
            )
            recent = session.scalar(
                select(TeamRecentFeature)
                .where(TeamRecentFeature.team_code == "PIT")
                .where(TeamRecentFeature.window_days == 14)
                .where(TeamRecentFeature.source == features.MLB_STATS_SOURCE)
            )
            snapshot = session.scalar(
                select(MlbFeatureSnapshot)
                .where(MlbFeatureSnapshot.source == features.FEATURE_VERSION)
                .where(MlbFeatureSnapshot.mlb_game_id.is_not(None))
            )
            coverage = features.feature_coverage(session, date(2026, 7, 1))
            detail = features.feature_detail(session, date(2026, 7, 1))
            source_report = features.source_status_report(session)
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] == "ok"
    assert team is not None
    assert team.features["defense_season"]["source_status"] == "available"
    assert team.features["defense_season"]["fielding_percentage"] is not None
    assert team.features["defense_season"]["errors_per_game"] is not None
    assert recent is not None
    assert recent.features["defense_recent"]["source_status"] == "available"
    assert recent.features["defense_recent"]["double_plays_per_game"] is not None
    assert snapshot is not None
    defense = snapshot.features["defense_catcher"]
    assert defense["source_status"] == "partial"
    assert defense["home"]["team_defense_season"]["source_status"] == "available"
    assert defense["home"]["team_defense_recent"]["source_status"] == "available"
    assert defense["home"]["catcher_starting_lineup"]["reason"] == "catcher unavailable because official lineup not posted yet"
    assert defense["advanced_catcher_metrics"]["source_status"] == "unavailable"
    assert defense["advanced_catcher_metrics"]["source"] == "not_configured"
    assert defense["umpire"]["source_status"] == "excluded"
    assert coverage["module_completeness"]["defense_catcher"]["partial"] == 1
    assert detail["items"][0]["module_completeness"]["defense_catcher"]["status"] == "partial"
    source_names = {item["source_name"]: item for item in source_report["source_health"]}
    assert source_names["mlb_stats_api_fielding"]["status"] == "available"
    assert source_names["catcher_from_official_lineup"]["status"] in {"missing", "not_attempted"}
    assert source_names["advanced_catcher_metrics"]["status"] == "not_configured"
    assert source_names["umpire"]["status"] == "excluded"


def test_empty_fielding_logs_remain_partial_without_fabricated_zero_metrics() -> None:
    fielding = features._aggregate_team_fielding_logs(
        [{"date": "2026-06-30", "stat": {"gamesPlayed": 1}}],
        1,
    )
    section = features._defense_feature_section(
        fielding,
        component="defense_season",
        reason_available="team defense season from MLB Stats API fielding game logs",
        reason_missing="team defense season missing because MLB Stats API fielding game logs were unavailable or empty",
    )

    assert fielding["game_count"] == 1
    assert fielding["source_fields_present"] == []
    assert fielding["errors"] is None
    assert fielding["assists"] is None
    assert fielding["putouts"] is None
    assert fielding["double_plays"] is None
    assert fielding["fielding_percentage"] is None
    assert section["source_status"] == "partial"
    assert section["reason"] == "MLB Stats API fielding logs returned games but limited fielding metrics."


def test_incomplete_fielding_components_do_not_inflate_fielding_percentage() -> None:
    fielding = features._aggregate_team_fielding_logs(
        [{"date": "2026-06-30", "stat": {"putOuts": 27, "assists": 12}}],
        1,
    )
    section = features._defense_feature_section(
        fielding,
        component="defense_season",
        reason_available="team defense season from MLB Stats API fielding game logs",
        reason_missing="team defense season missing because MLB Stats API fielding game logs were unavailable or empty",
    )

    assert fielding["source_fields_present"] == ["assists", "putOuts"]
    assert fielding["chances"] is None
    assert fielding["fielding_percentage"] is None
    assert section["source_status"] == "partial"


def test_fielding_only_logs_do_not_create_mlb_team_offense_cache() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    day = date(2026, 7, 1)
    captured_at = datetime(2026, 7, 1, 15, 0, tzinfo=UTC)
    fielding_payload = {
        "stats": [
            {
                "splits": [
                    {
                        "date": "2026-06-30",
                        "stat": {
                            "errors": 1,
                            "assists": 12,
                            "putOuts": 27,
                            "doublePlays": 1,
                        },
                    }
                ]
            }
        ]
    }

    with Session(engine) as session:
        game = _fielding_test_game()
        session.add(game)
        session.flush()
        mlb_context = {"team_fielding_logs_by_id": {"134": fielding_payload}}

        daily = features._upsert_mlb_primary_team_daily(
            session,
            game,
            "home",
            day,
            captured_at,
            mlb_context,
            {"team_contact_by_code": {}},
        )
        recent = features._upsert_mlb_primary_team_recent(
            session,
            game,
            "home",
            day,
            captured_at,
            14,
            mlb_context,
            {"team_contact_by_code": {}},
        )

        persisted_daily = session.scalar(
            select(TeamDailyFeature).where(TeamDailyFeature.source == features.MLB_STATS_SOURCE)
        )
        persisted_recent = session.scalar(
            select(TeamRecentFeature).where(TeamRecentFeature.source == features.MLB_STATS_SOURCE)
        )

    assert daily is None
    assert recent is None
    assert persisted_daily is None
    assert persisted_recent is None


def test_caught_stealing_rate_requires_both_running_game_fields() -> None:
    stolen_only = features._aggregate_team_fielding_logs(
        [{"date": "2026-06-30", "stat": {"stolenBases": 2}}],
        1,
    )
    caught_only = features._aggregate_team_fielding_logs(
        [{"date": "2026-06-30", "stat": {"caughtStealing": 1}}],
        1,
    )
    complete = features._aggregate_team_fielding_logs(
        [{"date": "2026-06-30", "stat": {"stolenBases": 2, "caughtStealing": 1}}],
        1,
    )

    assert stolen_only["stolen_bases_allowed"] == 2.0
    assert stolen_only["caught_stealing"] is None
    assert stolen_only["caught_stealing_rate"] is None
    assert caught_only["stolen_bases_allowed"] is None
    assert caught_only["caught_stealing"] == 1.0
    assert caught_only["caught_stealing_rate"] is None
    assert complete["caught_stealing_rate"] == 0.3333


def test_fielding_fetch_failure_preserves_cached_defense_sections() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    day = date(2026, 7, 1)
    cached_at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    captured_at = datetime(2026, 7, 1, 15, 0, tzinfo=UTC)
    cached_season = {
        "component": "defense_season",
        "source_status": "available",
        "source": features.MLB_STATS_SOURCE,
        "reason": "team defense season from MLB Stats API fielding game logs",
        "fielding_percentage": 0.985,
        "source_fields_present": ["errors", "putOuts", "assists"],
    }
    cached_recent = {
        "component": "defense_recent",
        "source_status": "available",
        "source": features.MLB_STATS_SOURCE,
        "reason": "team defense recent from MLB Stats API fielding game logs over 14 days",
        "double_plays_per_game": 0.8,
        "source_fields_present": ["doublePlays"],
    }

    with Session(engine) as session:
        game = _fielding_test_game()
        session.add(game)
        session.flush()
        session.add(
            TeamDailyFeature(
                target_date=day,
                team_code="PIT",
                captured_at=cached_at,
                source=features.MLB_STATS_SOURCE,
                source_status="available",
                confidence=Decimal("0.8500"),
                completeness=Decimal("0.8000"),
                stale=False,
                features={"defense_season": cached_season},
            )
        )
        session.add(
            TeamRecentFeature(
                target_date=day,
                team_code="PIT",
                window_days=14,
                captured_at=cached_at,
                source=features.MLB_STATS_SOURCE,
                source_status="available",
                confidence=Decimal("0.8000"),
                completeness=Decimal("0.7500"),
                stale=False,
                features={"defense_recent": cached_recent},
            )
        )
        session.add(
            TeamDailyFeature(
                target_date=date(2026, 6, 30),
                team_code="SEA",
                captured_at=datetime(2026, 6, 30, 12, 0, tzinfo=UTC),
                source=features.MLB_STATS_SOURCE,
                source_status="available",
                confidence=Decimal("0.8500"),
                completeness=Decimal("0.8000"),
                stale=False,
                features={
                    "defense_season": {
                        **cached_season,
                        "captured_at": datetime(2026, 6, 30, 12, 0, tzinfo=UTC).isoformat(),
                    }
                },
            )
        )
        session.flush()
        mlb_context = {
            "team_hitting_logs_by_id": {"134": _team_log_rows("hitting")},
            "team_pitching_logs_by_id": {"134": _team_log_rows("pitching")},
        }

        daily = features._upsert_mlb_primary_team_daily(
            session,
            game,
            "home",
            day,
            captured_at,
            mlb_context,
            {"team_contact_by_code": {}},
        )
        recent = features._upsert_mlb_primary_team_recent(
            session,
            game,
            "home",
            day,
            captured_at,
            14,
            mlb_context,
            {"team_contact_by_code": {}},
        )
        defense_status = features._defense_db_status(session)
        source_report = features.source_status_report(session)

    assert daily is not None
    assert recent is not None
    assert daily.raw_payload["fielding_rows"] == 0
    assert recent.raw_payload["fielding_rows"] == 0
    assert daily.features["defense_season"]["fielding_percentage"] == cached_season["fielding_percentage"]
    assert daily.features["defense_season"]["captured_at"] == cached_at.isoformat()
    assert daily.features["defense_season"]["cache_reused_at"] == captured_at.isoformat()
    assert daily.features["defense_season"]["stale"] is True
    assert daily.features["defense_season"]["cache_reused"] is True
    assert recent.features["defense_recent"]["double_plays_per_game"] == cached_recent["double_plays_per_game"]
    assert recent.features["defense_recent"]["captured_at"] == cached_at.isoformat()
    assert recent.features["defense_recent"]["cache_reused_at"] == captured_at.isoformat()
    assert recent.features["defense_recent"]["stale"] is True
    assert recent.features["defense_recent"]["cache_reused"] is True
    assert defense_status["status"] == "cached"
    assert defense_status["last_successful_sync"] == cached_at.isoformat()
    assert defense_status["cache_reused_count"] == 2
    assert defense_status["fresh_success_count"] == 1
    assert defense_status["latest_cache_reused_count"] == 2
    assert defense_status["latest_fresh_success_count"] == 0
    source_health = {item["source_name"]: item for item in source_report["source_health"]}
    assert source_health["mlb_stats_api_fielding"]["status"] == "cached"
    assert source_health["mlb_stats_api_fielding"]["last_successful_sync"] == cached_at.isoformat()
    assert source_health["mlb_stats_api_fielding"]["fallback_used"] is True
    assert source_health["mlb_stats_api_fielding"]["fallback_source"] == "last_good_mlb_fielding_cache"


def test_fielding_source_failure_degrades_defense_without_blocking_offense(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    monkeypatch.setattr(features, "_fetch_open_meteo", lambda *_args, **_kwargs: {"hourly": {"time": []}})

    class FakeMLBStatsClient:
        def get_team_game_log_stats(self, team_id: str, group: str, season: int):
            if group == "fielding":
                raise RuntimeError("fielding source unavailable")
            return _team_log_rows(group)

        def get_team_stat_splits(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_pitcher_game_log_stats(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {}

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    def hydrate_schedule(session: Session, *_args, **_kwargs) -> int:
        session.add(_fielding_test_game())
        session.flush()
        return 1

    monkeypatch.setattr(features, "_hydrate_schedule_window", hydrate_schedule)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            result = features.sync_mlb_features(session, date(2026, 7, 1))
            snapshot = session.scalar(
                select(MlbFeatureSnapshot)
                .where(MlbFeatureSnapshot.source == features.FEATURE_VERSION)
                .where(MlbFeatureSnapshot.mlb_game_id.is_not(None))
            )
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] == "degraded_with_errors"
    assert any(error["table"] == "team_fielding_game_log" for error in result["errors"])
    assert snapshot is not None
    assert snapshot.features["offense_season"]["home"]["source_status"] == "available"
    assert snapshot.features["defense_catcher"]["source_status"] == "missing"
    assert snapshot.features["defense_catcher"]["home"]["team_defense_season"]["source_status"] == "missing"
    assert "fielding game logs" in snapshot.features["defense_catcher"]["home"]["team_defense_season"]["reason"]


def test_defense_catcher_infers_official_catcher_and_keeps_missing_reason() -> None:
    captured_at = datetime(2026, 7, 1, 19, 0, tzinfo=UTC)
    home_lineup = LineupSnapshot(
        mlb_game_id=1,
        target_date=date(2026, 7, 1),
        team_code="PIT",
        captured_at=captured_at,
        source=features.MLB_STATS_SOURCE,
        source_status="available",
        confirmed=True,
        features={"starters": [{"id": "44", "name": "Home Catcher", "position": "C"}]},
    )
    away_lineup = LineupSnapshot(
        mlb_game_id=1,
        target_date=date(2026, 7, 1),
        team_code="SEA",
        captured_at=captured_at,
        source=features.MLB_STATS_SOURCE,
        source_status="missing",
        confirmed=False,
        features={"starters": [], "missing_reason": features.LINEUP_NOT_POSTED_YET},
    )

    module = features._defense_catcher_module(None, None, None, None, home_lineup, away_lineup, captured_at)

    assert module["source_status"] == "partial"
    assert module["home"]["catcher_starting_lineup"]["source_status"] == "available"
    assert module["home"]["catcher_starting_lineup"]["catcher"]["name"] == "Home Catcher"
    assert module["away"]["catcher_starting_lineup"]["source_status"] == "missing"
    assert module["away"]["catcher_starting_lineup"]["reason"] == "catcher unavailable because official lineup not posted yet"
    assert module["advanced_catcher_metrics"]["reason"] == "advanced catcher metrics not configured/unavailable"
    assert module["umpire"]["reason"] == "umpire factors excluded by design"


def test_source_status_report_marks_catcher_partial_when_lineup_lacks_catcher() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured_at = datetime(2026, 7, 1, 19, 0, tzinfo=UTC)

    with Session(engine) as session:
        session.add(
            LineupSnapshot(
                mlb_game_id=1,
                target_date=date(2026, 7, 1),
                team_code="PIT",
                captured_at=captured_at,
                source=features.MLB_STATS_SOURCE,
                source_status="partial",
                confirmed=False,
                features={
                    "starters": [{"id": "11", "name": "Partial Starter", "position": "CF"}],
                    "missing_reason": features.PARTIAL_LINEUP_POSTED,
                },
            )
        )
        session.commit()
        report = features.source_status_report(session)

    source_health = {item["source_name"]: item for item in report["source_health"]}
    catcher_source = source_health["catcher_from_official_lineup"]
    assert catcher_source["status"] == "partial"
    assert catcher_source["last_successful_sync"] is None
    assert catcher_source["last_attempted_sync"] == captured_at.isoformat()
    assert catcher_source["sample_count"] == 1


def test_feature_sync_hydrates_final_games_for_backfill(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    monkeypatch.setattr(features, "_hydrate_schedule_window", lambda *_args, **_kwargs: 0)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    def boxscore_team(start_id: int, pitcher_id: int) -> dict[str, object]:
        batters = list(range(start_id, start_id + 9))
        players: dict[str, object] = {}
        for index, person_id in enumerate(batters, start=1):
            players[f"ID{person_id}"] = {
                "person": {"id": person_id, "fullName": f"Starter {person_id}"},
                "battingOrder": str(index * 100),
                "batSide": {"code": "R"},
                "position": {"abbreviation": "C" if index == 9 else "CF"},
            }
        players[f"ID{pitcher_id}"] = {
            "person": {"id": pitcher_id, "fullName": f"Pitcher {pitcher_id}"},
            "pitchHand": {"code": "R"},
        }
        return {"batters": batters, "pitchers": [pitcher_id], "players": players}

    def hydrate_final_game(game: MlbGame) -> None:
        game.raw_payload = {
            **(game.raw_payload or {}),
            "liveData": {
                "boxscore": {
                    "teams": {
                        "home": boxscore_team(1000, 1999),
                        "away": boxscore_team(2000, 2999),
                    }
                }
            },
        }

    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", hydrate_final_game)
    try:
        with Session(engine) as session:
            game = MlbGame(
                external_game_id="feature-sync-final-backfill",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="Final",
                home_score=5,
                away_score=3,
                raw_payload={"venue": {"id": 31, "name": "PNC Park"}},
            )
            session.add(game)
            session.commit()

            features.sync_mlb_features(session, date(2026, 7, 1))
            snapshot = session.scalar(select(MlbFeatureSnapshot))

        assert snapshot is not None
        assert snapshot.source_statuses["lineup"] == {"home": "available", "away": "available"}
        assert snapshot.source_statuses["starter_identity"] == {"home": "available", "away": "available"}
    finally:
        get_settings.cache_clear()


def test_network_disabled_returns_skipped_without_raw_writes(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "false")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="network-disabled-feature-sync",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
            raw_payload={"venue": {"id": 31, "name": "PNC Park"}},
        )
        session.add(game)
        session.commit()

        result = features.sync_mlb_lineups(session, date(2026, 7, 1))
        lineup_count = session.scalar(select(LineupSnapshot))

    assert result["network_sources_enabled"] is False
    assert result["validation_status"] == "skipped_network_disabled"
    assert result["rows_inserted"] == 0
    assert result["rows_updated"] == 0
    assert lineup_count is None


def test_schedule_hydration_deduplicates_game_pk_and_updates_existing() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    def game_payload(game_pk: int, home_name: str = "Pittsburgh Pirates") -> dict[str, object]:
        return {
            "gamePk": game_pk,
            "gameDate": "2026-07-01T23:00:00Z",
            "venue": {"id": 31, "name": "PNC Park"},
            "status": {"detailedState": "Scheduled"},
            "teams": {
                "home": {"score": 4, "team": {"name": home_name, "abbreviation": "PIT"}},
                "away": {"score": 2, "team": {"name": "Seattle Mariners", "abbreviation": "SEA"}},
            },
        }

    class FakeClient:
        def get_schedule(self, target_date=None, **kwargs):
            if kwargs.get("start_date"):
                return {"dates": []}
            return {"dates": [{"games": [game_payload(824518), game_payload(824518, "Old Home")]}]}

    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="824518",
                home_team="Existing Home",
                away_team="Existing Away",
                home_abbreviation="EXH",
                away_abbreviation="EXA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="Old",
                raw_payload={"liveData": {"cached": True}},
            )
        )
        session.commit()
        errors: list[dict[str, object]] = []

        result = features._hydrate_schedule_window(
            session,
            date(2026, 7, 1),
            client=FakeClient(),
            errors=errors,
        )
        session.commit()
        rows = list(session.scalars(select(MlbGame)))

    assert len(rows) == 1
    assert rows[0].external_game_id == "824518"
    assert rows[0].home_team == "Pittsburgh Pirates"
    assert rows[0].raw_payload["liveData"] == {"cached": True}
    assert result["rows_seen"] == 2
    assert result["duplicate_count"] == 1
    assert result["rows_upserted"] == 1
    assert errors == []


def test_team_feature_sync_can_run_twice_without_duplicate_games(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    _stub_mlb_primary_stats_empty(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeClient:
        def get_schedule(self, target_date=None, **kwargs):
            if kwargs.get("start_date"):
                return {"dates": []}
            return {
                "dates": [
                    {
                        "games": [
                            {
                                "gamePk": 824518,
                                "gameDate": "2026-07-01T23:00:00Z",
                                "venue": {"id": 31, "name": "PNC Park"},
                                "status": {"detailedState": "Scheduled"},
                                "teams": {
                                    "home": {
                                        "score": 0,
                                        "team": {"name": "Pittsburgh Pirates", "abbreviation": "PIT"},
                                    },
                                    "away": {
                                        "score": 0,
                                        "team": {"name": "Seattle Mariners", "abbreviation": "SEA"},
                                    },
                                },
                            }
                        ]
                    }
                ]
            }

    monkeypatch.setattr(features, "MLBStatsClient", FakeClient)
    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", lambda *_args, **_kwargs: None)
    try:
        with Session(engine) as session:
            first = features.sync_mlb_team_features(session, date(2026, 7, 1))
            second = features.sync_mlb_team_features(session, date(2026, 7, 1))
            games = list(session.scalars(select(MlbGame)))
            team_rows = list(session.scalars(select(TeamDailyFeature)))
    finally:
        get_settings.cache_clear()

    assert first["validation_status"] in {"ok", "degraded_no_available_public_rows"}
    assert second["refresh_schedule"] is False
    assert second["hydration_skipped_reason"] == "target_date_games_exist"
    assert len(games) == 1
    assert len(team_rows) == 2


def test_feature_sync_returns_degraded_and_records_source_error(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    _stub_mlb_primary_stats_empty(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    def broken_hydration(_session: Session, _day: date, **_kwargs) -> dict[str, object]:
        error = {
            "source": features.MLB_STATS_SOURCE,
            "table": "mlb_games",
            "game_pk": "824518",
            "error_type": "IntegrityError",
            "message": "duplicate key value violates unique constraint",
        }
        _kwargs["errors"].append(error)
        return {
            "rows_seen": 1,
            "rows_upserted": 0,
            "duplicate_count": 0,
            "error_count": 1,
            "validation_status": "degraded_with_errors",
            "errors": [error],
            "warnings": ["test degraded hydration"],
        }

    monkeypatch.setattr(features, "_hydrate_schedule_window", broken_hydration)
    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", lambda *_args, **_kwargs: None)
    try:
        with Session(engine) as session:
            session.add(
                MlbGame(
                    external_game_id="824518",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="scheduled",
                    raw_payload={"venue": {"id": 31, "name": "PNC Park"}},
                )
            )
            session.commit()

            result = features.sync_mlb_team_features(session, date(2026, 7, 1), refresh_schedule=True)
            report = features.source_status_report(session)
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] == "degraded_with_errors"
    assert result["hydration_error_count"] == 1
    assert result["error_count"] == 1
    assert report["validation_status"] == "degraded_with_errors"
    assert report["last_error"]["mlb_games"]["error_type"] == "IntegrityError"


def test_failed_sync_without_game_snapshots_persists_source_audit(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    _stub_mlb_primary_stats_empty(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    def failed_empty_hydration(_session: Session, _day: date, **kwargs) -> dict[str, object]:
        error = {
            "source": features.MLB_STATS_SOURCE,
            "table": "mlb_games",
            "error_type": "HttpJsonError",
            "message": "schedule unavailable",
        }
        kwargs["errors"].append(error)
        return {
            "rows_seen": 0,
            "rows_upserted": 0,
            "duplicate_count": 0,
            "error_count": 1,
            "validation_status": "degraded_with_errors",
            "errors": [error],
            "warnings": ["schedule unavailable"],
        }

    monkeypatch.setattr(features, "_hydrate_schedule_window", failed_empty_hydration)
    try:
        with Session(engine) as session:
            result = features.sync_mlb_team_features(session, date(2026, 7, 1), refresh_schedule=True)
            report = features.source_status_report(session)
            audit = session.scalar(
                select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_SYNC_AUDIT_SOURCE)
            )
    finally:
        get_settings.cache_clear()

    assert result["games_seen"] == 0
    assert result["feature_snapshots_upserted"] == 0
    assert result["validation_status"] == "degraded_with_errors"
    assert audit is not None
    assert audit.mlb_game_id is None
    assert report["last_attempted_sync"] is not None
    assert report["validation_status"] == "degraded_with_errors"
    assert report["last_error"]["mlb_games"]["message"] == "schedule unavailable"
    assert report["last_successful_sync"]["mlb_feature_snapshots"] is None
    assert report["tables"]["mlb_feature_snapshots"]["row_sample_count"] == 0


def test_source_status_errors_are_limited_to_latest_sync_attempt() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            [
                MlbFeatureSnapshot(
                    mlb_game_id=None,
                    target_date=date(2026, 7, 1),
                    source=features.FEATURE_SYNC_AUDIT_SOURCE,
                    captured_at=datetime(2026, 7, 1, 15, 0, tzinfo=UTC),
                    data_quality=None,
                    source_statuses={"sync": "degraded_with_errors"},
                    features={
                        "sync_status": {
                            "target_date": "2026-07-01",
                            "attempted_at": "2026-07-01T15:00:00+00:00",
                            "validation_status": "degraded_with_errors",
                            "error_count": 1,
                            "errors": [
                                {
                                    "source": features.MLB_STATS_SOURCE,
                                    "table": "mlb_games",
                                    "error_type": "HttpJsonError",
                                    "message": "old failure",
                                }
                            ],
                            "warnings": [],
                        }
                    },
                ),
                MlbFeatureSnapshot(
                    mlb_game_id=None,
                    target_date=date(2026, 7, 1),
                    source=features.FEATURE_SYNC_AUDIT_SOURCE,
                    captured_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
                    data_quality=None,
                    source_statuses={"sync": "ok"},
                    features={
                        "sync_status": {
                            "target_date": "2026-07-01",
                            "attempted_at": "2026-07-01T16:00:00+00:00",
                            "validation_status": "ok",
                            "error_count": 0,
                            "errors": [],
                            "warnings": [],
                        }
                    },
                ),
            ]
        )
        session.commit()

        report = features.source_status_report(session)

    assert report["validation_status"] == "ok"
    assert report["latest_errors"] == []
    assert "mlb_games" not in report["last_error"]


def test_refresh_schedule_false_skips_hydration_when_games_exist(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    called = {"hydration": 0}

    def forbidden_hydration(*_args, **_kwargs):
        called["hydration"] += 1
        raise AssertionError("schedule hydration should be skipped")

    monkeypatch.setattr(features, "_hydrate_schedule_window", forbidden_hydration)
    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", lambda *_args, **_kwargs: None)
    try:
        with Session(engine) as session:
            session.add(
                MlbGame(
                    external_game_id="refresh-skip-1",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="scheduled",
                    raw_payload={"venue": {"id": 31, "name": "PNC Park"}},
                )
            )
            session.commit()

            result = features.sync_mlb_team_features(session, date(2026, 7, 1), refresh_schedule=False)
    finally:
        get_settings.cache_clear()

    assert called["hydration"] == 0
    assert result["hydration_validation_status"] == "not_run"
    assert result["hydration_skipped_reason"] == "target_date_games_exist"


def test_full_feature_sync_refreshes_schedule_by_default_with_existing_games(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    called = {"hydration": 0}

    def tracked_hydration(*_args, **_kwargs):
        called["hydration"] += 1
        return {
            "rows_seen": 0,
            "rows_upserted": 0,
            "duplicate_count": 0,
            "error_count": 0,
            "validation_status": "ok",
            "errors": [],
            "warnings": [],
        }

    monkeypatch.setattr(features, "_hydrate_schedule_window", tracked_hydration)
    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(features, "_fetch_open_meteo", lambda *_args, **_kwargs: {"hourly": {"time": []}})
    try:
        with Session(engine) as session:
            session.add(
                MlbGame(
                    external_game_id="full-refresh-default-1",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="scheduled",
                    raw_payload={"venue": {"id": 31, "name": "PNC Park"}},
                )
            )
            session.commit()

            result = features.sync_mlb_features(session, date(2026, 7, 1))
    finally:
        get_settings.cache_clear()

    assert called["hydration"] == 1
    assert result["refresh_schedule"] is True
    assert result["hydration_validation_status"] == "ok"


def test_pybaseball_unavailable_is_reported_and_advanced_stats_degraded(monkeypatch) -> None:
    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {
            "available": False,
            "version": None,
            "module_path": None,
            "import_error": {"error_type": "ImportError", "message": "No module named pybaseball"},
        },
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        report = features.source_status_report(session)

    assert report["pybaseball_available"] is False
    assert report["pybaseball_import_error"]["message"] == "No module named pybaseball"
    assert report["advanced_public_stats_status"] == "unavailable_pybaseball_not_installed"


def test_pybaseball_import_check_reports_module_metadata(monkeypatch) -> None:
    class FakePybaseballModule:
        __version__ = "2.2.7"
        __file__ = "C:/site-packages/pybaseball/__init__.py"

    monkeypatch.setattr(pybaseball_client.importlib, "import_module", lambda name: FakePybaseballModule())

    status = pybaseball_client.import_status()

    assert status["available"] is True
    assert status["version"] == "2.2.7"
    assert status["module_path"] == "C:/site-packages/pybaseball/__init__.py"
    assert status["import_error"] is None


def test_pybaseball_records_normalize_non_json_scalars() -> None:
    class FakeScalar:
        def __init__(self, value: object) -> None:
            self.value = value

        def item(self) -> object:
            return self.value

    class FakeFrame:
        columns = ["Team", "OBP", "SLG", "PA", "as_of"]

        def to_dict(self, *_args, **_kwargs) -> list[dict[str, object]]:
            return [
                {
                    "Team": "PIT",
                    "OBP": float("nan"),
                    "SLG": float("inf"),
                    "PA": FakeScalar(401),
                    "as_of": date(2026, 7, 1),
                }
            ]

    result = pybaseball_client._records_from_frame(FakeFrame())

    assert result.rows == [{"Team": "PIT", "OBP": None, "SLG": None, "PA": 401, "as_of": "2026-07-01"}]
    json.dumps(result.to_dict(), allow_nan=False)


def test_pybaseball_ingestion_writes_available_advanced_rows_and_snapshots(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {
            "available": True,
            "version": "2.2.7",
            "module_path": "mocked/pybaseball.py",
            "import_error": None,
        },
    )
    monkeypatch.setattr(
        features.pybaseball_client,
        "get_batting_stats",
        lambda season: {
            "function": "batting_stats",
            "rows": [
                {
                    "Team": "PIT",
                    "G": 81,
                    "PA": 100,
                    "AB": 90,
                    "R": 10,
                    "OBP": ".250",
                    "SLG": ".300",
                    "ISO": ".100",
                    "K%": "30.0%",
                    "BB%": "5.0%",
                    "HR": 2,
                    "BABIP": ".250",
                    "wRC+": 80,
                    "wOBA": ".280",
                    "HardHit%": "40.0%",
                    "Barrel%": "5.0%",
                    "EV": 88.0,
                    "LA": 10.0,
                },
                {
                    "Team": "PIT",
                    "G": 78,
                    "PA": 300,
                    "AB": 270,
                    "R": 50,
                    "OBP": ".350",
                    "SLG": ".450",
                    "ISO": ".190",
                    "K%": "20.0%",
                    "BB%": "10.0%",
                    "HR": 15,
                    "BABIP": ".320",
                    "wRC+": 120,
                    "wOBA": ".340",
                    "HardHit%": "42.0%",
                    "Barrel%": "9.0%",
                    "EV": 90.0,
                    "LA": 13.0,
                },
                {
                    "Team": "SEA",
                    "G": 81,
                    "PA": 390,
                    "R": 340,
                    "OBP": ".316",
                    "SLG": ".401",
                    "ISO": ".160",
                    "K%": "24.1%",
                    "BB%": "8.1%",
                    "BABIP": ".295",
                    "wRC+": 101,
                    "wOBA": ".315",
                },
            ],
            "row_count": 3,
            "columns": ["Team", "G", "PA", "R", "OBP", "SLG", "ISO", "K%", "BB%", "wRC+", "wOBA"],
        },
    )
    monkeypatch.setattr(
        features.pybaseball_client,
        "get_pitching_stats",
        lambda season: {
            "function": "pitching_stats",
            "rows": [
                {
                    "Name": "Pitcher 1999",
                    "Team": "PIT",
                    "MLBAMID": "1999",
                    "IP": 102.1,
                    "ERA": 3.45,
                    "WHIP": 1.12,
                    "K%": "26.0%",
                    "BB%": "7.1%",
                    "K-BB%": "18.9%",
                    "HR/9": 0.92,
                    "FIP": 3.61,
                    "xFIP": 3.77,
                    "HardHit%": "35.0%",
                    "Barrel%": "6.2%",
                    "GB%": "43.3%",
                },
                {
                    "Name": "Pitcher 2999",
                    "Team": "SEA",
                    "MLBAMID": "2999",
                    "IP": 98.0,
                    "ERA": 3.88,
                    "WHIP": 1.21,
                    "K%": "24.2%",
                    "BB%": "7.9%",
                    "FIP": 3.96,
                },
            ],
            "row_count": 2,
            "columns": ["Name", "Team", "MLBAMID", "IP", "ERA", "WHIP", "K%", "BB%", "FIP"],
        },
    )

    class EmptyMLBStatsClient:
        def get_team_season_stats(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_team_game_log_stats(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_team_stat_splits(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_pitcher_season_stats(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_pitcher_game_log_stats(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {}

    monkeypatch.setattr(features, "MLBStatsClient", EmptyMLBStatsClient)
    monkeypatch.setattr(features.pybaseball_client, "get_statcast_range", lambda *_args, **_kwargs: {"rows": []})
    monkeypatch.setattr(features.pybaseball_client, "get_pitcher_statcast_range", lambda *_args, **_kwargs: {"rows": []})

    def hydrate_schedule(session: Session, day: date, **_kwargs) -> int:
        session.add(
            MlbGame(
                external_game_id="pybaseball-advanced-1",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
                raw_payload={
                    "venue": {"id": 31, "name": "PNC Park"},
                    "teams": {
                        "home": {"probablePitcher": {"id": 1999, "fullName": "Pitcher 1999"}},
                        "away": {"probablePitcher": {"id": 2999, "fullName": "Pitcher 2999"}},
                    },
                },
            )
        )
        session.flush()
        return 1

    monkeypatch.setattr(features, "_hydrate_schedule_window", hydrate_schedule)
    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(features, "_fetch_open_meteo", lambda *_args, **_kwargs: {"hourly": {"time": []}})

    try:
        with Session(engine) as session:
            result = features.sync_mlb_features(session, date(2026, 7, 1))
            advanced_team = session.scalar(
                select(TeamDailyFeature)
                .where(TeamDailyFeature.team_code == "PIT")
                .where(TeamDailyFeature.source == features.PYBASEBALL_SOURCE)
            )
            derived_team = session.scalar(
                select(TeamDailyFeature)
                .where(TeamDailyFeature.team_code == "PIT")
                .where(TeamDailyFeature.source == features.DERIVED_SOURCE)
            )
            advanced_pitcher = session.scalar(
                select(PitcherDailyFeature)
                .where(PitcherDailyFeature.team_code == "PIT")
                .where(PitcherDailyFeature.source == features.PYBASEBALL_SOURCE)
            )
            snapshot = session.scalar(
                select(MlbFeatureSnapshot)
                .where(MlbFeatureSnapshot.source == features.FEATURE_VERSION)
                .where(MlbFeatureSnapshot.mlb_game_id.is_not(None))
            )
            report = features.source_status_report(session)
    finally:
        get_settings.cache_clear()

    assert result["pybaseball_available"] is True
    assert result["pybaseball_functions_attempted"] == ["batting_stats", "pitching_stats"]
    assert result["pybaseball_rows_seen"] == 5
    assert result["pybaseball_rows_matched"] >= 4
    assert result["advanced_available_count"] >= 2
    assert advanced_team is not None
    assert advanced_team.source_status == "available"
    assert advanced_team.features["obp"] == 0.325
    assert advanced_team.features["runs_per_game"] == 0.7407
    assert advanced_team.features["wrc_plus"] == 110.0
    assert advanced_team.raw_payload["row"]["player_rows_aggregated"] == 2
    assert derived_team is not None
    assert derived_team.source_status in {"missing", "partial"}
    assert advanced_pitcher is not None
    assert advanced_pitcher.source_status == "available"
    assert advanced_pitcher.features["season"]["era"] == 3.45
    assert advanced_pitcher.features["recent"]["source_status"] == "missing"
    assert advanced_pitcher.features["workload"]["source_status"] == "missing"
    assert advanced_pitcher.features["workload"]["expected_innings_projection"] is None
    assert snapshot is not None
    assert snapshot.features["offense_season"]["home"]["source"] == features.PYBASEBALL_SOURCE
    assert snapshot.features["starter_season"]["home"]["source"] == features.PYBASEBALL_SOURCE
    assert snapshot.features["starter_recent"]["home"]["source_status"] == "missing"
    assert snapshot.features["starter_workload"]["home"]["source_status"] == "missing"
    assert snapshot.features["starter_workload"]["home"]["expected_innings_projection"] is None
    assert snapshot.data_quality is not None
    assert snapshot.data_quality > Decimal("0.3500")
    assert report["pybaseball_available"] is True
    assert report["pybaseball_version"] == "2.2.7"
    assert report["advanced_stats_status"] == "available"


def test_defense_catcher_reads_mlb_stats_row_when_offense_prefers_pybaseball() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target_date = date(2026, 7, 1)
    captured_at = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
    defense_season = {
        "component": "defense_season",
        "source_status": "available",
        "source": features.MLB_STATS_SOURCE,
        "reason": "team defense season from MLB Stats API fielding game logs",
        "fielding_percentage": 0.985,
        "source_fields_present": ["errors", "putOuts", "assists"],
    }
    defense_recent = {
        "component": "defense_recent",
        "source_status": "available",
        "source": features.MLB_STATS_SOURCE,
        "reason": "team defense recent from MLB Stats API fielding game logs over 14 days",
        "double_plays_per_game": 0.8,
        "source_fields_present": ["doublePlays"],
    }

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="defense-source-specific-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        session.add(game)
        session.flush()
        session.add_all(
            [
                TeamDailyFeature(
                    target_date=target_date,
                    team_code="PIT",
                    captured_at=captured_at,
                    source=features.PYBASEBALL_SOURCE,
                    source_status="available",
                    confidence=Decimal("0.9000"),
                    completeness=Decimal("0.9000"),
                    stale=False,
                    features={"runs_per_game": 5.0, "source_status": "available"},
                ),
                TeamDailyFeature(
                    target_date=target_date,
                    team_code="PIT",
                    captured_at=captured_at - timedelta(hours=1),
                    source=features.MLB_STATS_SOURCE,
                    source_status="partial",
                    confidence=Decimal("0.4500"),
                    completeness=Decimal("0.4500"),
                    stale=False,
                    features={"defense_season": defense_season},
                ),
                TeamRecentFeature(
                    target_date=target_date,
                    team_code="PIT",
                    window_days=14,
                    captured_at=captured_at,
                    source=features.DERIVED_SOURCE,
                    source_status="available",
                    confidence=Decimal("0.9000"),
                    completeness=Decimal("0.9000"),
                    stale=False,
                    features={"runs_per_game": 4.8},
                ),
                TeamRecentFeature(
                    target_date=target_date,
                    team_code="PIT",
                    window_days=14,
                    captured_at=captured_at - timedelta(hours=1),
                    source=features.MLB_STATS_SOURCE,
                    source_status="partial",
                    confidence=Decimal("0.4000"),
                    completeness=Decimal("0.4000"),
                    stale=False,
                    features={"defense_recent": defense_recent},
                ),
            ]
        )
        market = KalshiMarket(
            kalshi_market_id="KX-DEFENSE-SOURCE",
            ticker="KXMLBGAME-DEFENSE-SOURCE-PIT",
            title="Will Pittsburgh win?",
            status="open",
            market_family="full_game_winner",
        )
        mapping = MarketMapping(
            mlb_game_id=game.id,
            kalshi_market_id=1,
            mapping_status="confirmed",
            confidence=Decimal("0.9500"),
            market_family="full_game_winner",
            selection_code="PIT",
        )
        snapshot = features.build_feature_snapshot(game, market, mapping, session=session, now=captured_at)

    assert snapshot["offense_season"]["home"]["source"] == features.PYBASEBALL_SOURCE
    assert snapshot["offense_recent"]["home"]["source"] == features.DERIVED_SOURCE
    defense = snapshot["defense_catcher"]["home"]
    assert defense["team_defense_season"]["source"] == features.MLB_STATS_SOURCE
    assert defense["team_defense_season"]["fielding_percentage"] == 0.985
    assert defense["team_defense_recent"]["source"] == features.MLB_STATS_SOURCE
    assert defense["team_defense_recent"]["double_plays_per_game"] == 0.8


def test_handedness_platoon_requires_actual_split_values() -> None:
    captured_at = datetime(2026, 6, 24, 16, 0, tzinfo=UTC)
    empty_daily = TeamDailyFeature(
        target_date=date(2026, 6, 24),
        team_code="PIT",
        source=features.MLB_STATS_SOURCE,
        source_status="available",
        captured_at=captured_at,
        features={
            "handedness_splits": {
                "hitting": {
                    "source": features.MLB_STATS_SOURCE,
                    "basis": "hitting",
                    "vsLeft": None,
                    "vsRight": {},
                },
                "pitching": {
                    "source": features.MLB_STATS_SOURCE,
                    "basis": "pitching",
                    "vsLeft": {"code": "vl", "description": "vs Left", "avg": None},
                    "vsRight": None,
                },
            }
        },
    )

    empty_module = features._handedness_module(None, None, None, None, empty_daily, None, captured_at)

    assert empty_module["source_status"] == "missing"

    valued_daily = TeamDailyFeature(
        target_date=date(2026, 6, 24),
        team_code="PIT",
        source=features.MLB_STATS_SOURCE,
        source_status="available",
        captured_at=captured_at,
        features={
            "handedness_splits": {
                "hitting": {
                    "source": features.MLB_STATS_SOURCE,
                    "basis": "hitting",
                    "vsLeft": {"code": "vl", "description": "vs Left", "avg": 0.0},
                    "vsRight": None,
                },
                "pitching": {
                    "source": features.MLB_STATS_SOURCE,
                    "basis": "pitching",
                    "vsLeft": None,
                    "vsRight": None,
                },
            }
        },
    )

    valued_module = features._handedness_module(None, None, None, None, valued_daily, None, captured_at)

    assert valued_module["source_status"] == "available"


def test_pybaseball_fangraphs_status_detects_wrapped_403() -> None:
    stats = features._new_sync_stats(date(2026, 6, 24), {"team"})
    wrapped = features.pybaseball_client.PybaseballSourceError(
        "pybaseball call failed.",
        function_name="batting_stats",
        error=RuntimeError("HTTP Error 403: Forbidden"),
    )

    features._record_pybaseball_source_error(stats, "batting_stats", wrapped)

    assert stats["pybaseball_fangraphs_status"] == "unavailable_http_403"
    assert stats["errors"][0]["message"] == "HTTP Error 403: Forbidden"


def test_statcast_source_error_detects_wrapped_schema_error() -> None:
    wrapped = features.pybaseball_client.PybaseballSourceError(
        "pybaseball call failed.",
        function_name="statcast",
        error=ValueError("missing Statcast launch_speed column"),
    )

    error = features._source_error(source=features.STATCAST_SOURCE, table="statcast_team_contact", exc=wrapped)

    assert error["error_code"] == "statcast_schema_changed"
    assert error["message"] == "missing Statcast launch_speed column"


def test_mlb_stats_primary_populates_features_when_fangraphs_403(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    def team_split(team_id: int, stat: dict[str, object]) -> dict[str, object]:
        return {"team": {"id": team_id}, "stat": stat}

    class FakeMLBStatsClient:
        def get_team_season_stats(self, group: str, season: int):
            if group == "hitting":
                stat = {
                    "gamesPlayed": 70,
                    "runs": 350,
                    "hits": 620,
                    "homeRuns": 90,
                    "strikeOuts": 510,
                    "baseOnBalls": 220,
                    "atBats": 2400,
                    "plateAppearances": 2700,
                    "avg": ".258",
                    "obp": ".329",
                    "slg": ".431",
                    "ops": ".760",
                    "stolenBases": 44,
                    "babip": ".302",
                }
            else:
                stat = {
                    "gamesPlayed": 70,
                    "wins": 38,
                    "losses": 32,
                    "era": "3.91",
                    "whip": "1.24",
                    "inningsPitched": "625.1",
                    "strikeOuts": 610,
                    "baseOnBalls": 210,
                    "hits": 570,
                    "homeRuns": 72,
                    "runs": 305,
                    "earnedRuns": 272,
                    "saves": 20,
                    "blownSaves": 7,
                }
            return {"stats": [{"splits": [team_split(134, stat), team_split(136, stat)]}]}

        def get_team_game_log_stats(self, team_id: str, group: str, season: int):
            rows = []
            for index in range(1, 16):
                if group == "hitting":
                    stat = {
                        "runs": 4 + index % 3,
                        "hits": 8 + index % 4,
                        "homeRuns": index % 2,
                        "baseOnBalls": 3,
                        "strikeOuts": 7,
                        "atBats": 34,
                        "plateAppearances": 38,
                        "totalBases": 14,
                    }
                else:
                    stat = {
                        "inningsPitched": "9.0",
                        "runs": 3,
                        "earnedRuns": 3,
                        "hits": 7,
                        "homeRuns": 1,
                        "baseOnBalls": 2,
                        "strikeOuts": 8,
                        "battersFaced": 36,
                        "numberOfPitches": 142,
                        "saves": 1,
                        "holds": 1,
                        "blownSaves": 0,
                        "saveOpportunities": 1,
                        "gamesFinished": 1,
                    }
                rows.append({"date": f"2026-06-{24 - index:02d}", "stat": stat})
            return {"stats": [{"splits": rows}]}

        def get_team_stat_splits(self, team_id: str, group: str, season: int, sitCodes: str = "vl,vr"):
            return {
                "stats": [
                    {
                        "splits": [
                            {"split": {"code": "vl", "description": "vs Left"}, "stat": {"avg": ".260", "obp": ".330", "slg": ".420", "ops": ".750"}},
                            {"split": {"code": "vr", "description": "vs Right"}, "stat": {"avg": ".255", "obp": ".325", "slg": ".435", "ops": ".760"}},
                        ]
                    }
                ]
            }

        def get_pitcher_season_stats(self, person_id: str, season: int):
            return {
                "stats": [
                    {
                        "splits": [
                            {
                                "stat": {
                                    "wins": 7,
                                    "losses": 4,
                                    "era": "3.40",
                                    "whip": "1.10",
                                    "inningsPitched": "72.1",
                                    "strikeOuts": 82,
                                    "baseOnBalls": 20,
                                    "hits": 60,
                                    "homeRuns": 8,
                                    "gamesStarted": 12,
                                    "battersFaced": 290,
                                }
                            }
                        ]
                    }
                ]
            }

        def get_pitcher_game_log_stats(self, person_id: str, season: int):
            return {
                "stats": [
                    {
                        "splits": [
                            {
                                "date": f"2026-06-{23 - index:02d}",
                                "stat": {
                                    "inningsPitched": "6.0",
                                    "gamesStarted": 1,
                                    "strikeOuts": 7,
                                    "baseOnBalls": 2,
                                    "hits": 5,
                                    "homeRuns": 1,
                                    "runs": 2,
                                    "earnedRuns": 2,
                                    "numberOfPitches": 91,
                                },
                            }
                            for index in range(5)
                        ]
                    }
                ]
            }

        def get_game_feed(self, *_args, **_kwargs):
            return {}

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)
    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {"available": True, "version": "2.2.7", "module_path": "mocked", "import_error": None},
    )
    monkeypatch.setattr(features.pybaseball_client, "get_batting_stats", lambda *_args, **_kwargs: (_ for _ in ()).throw(Exception("HTTP 403")))
    monkeypatch.setattr(features.pybaseball_client, "get_pitching_stats", lambda *_args, **_kwargs: (_ for _ in ()).throw(Exception("HTTP 403")))
    monkeypatch.setattr(
        features.pybaseball_client,
        "get_statcast_range",
        lambda *_args, **_kwargs: {
            "rows": [
                {
                    "bat_team": team,
                    "launch_speed": 96,
                    "launch_angle": 18,
                    "estimated_woba_using_speedangle": 0.360,
                    "estimated_ba_using_speedangle": 0.280,
                    "woba_value": 0.350,
                    "iso_value": 0.180,
                    "babip_value": 0.310,
                    "bb_type": "line_drive",
                    "launch_speed_angle": "6",
                }
                for team in ["PIT", "SEA"]
                for _ in range(30)
            ],
            "columns": ["bat_team", "launch_speed", "launch_angle", "estimated_woba_using_speedangle"],
        },
    )
    monkeypatch.setattr(
        features.pybaseball_client,
        "get_pitcher_statcast_range",
        lambda *_args, **_kwargs: {
            "rows": [
                {"launch_speed": 88, "launch_angle": 12, "estimated_woba_using_speedangle": 0.300, "release_speed": 94}
                for _ in range(30)
            ],
            "columns": ["launch_speed", "launch_angle", "estimated_woba_using_speedangle", "release_speed"],
        },
    )
    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", lambda *_args, **_kwargs: None)

    def hydrate_schedule(session: Session, day: date, **_kwargs) -> int:
        session.add(
            MlbGame(
                external_game_id="mlb-primary-1",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 6, 24, 23, 0, tzinfo=UTC),
                status="scheduled",
                raw_payload={
                    "teams": {
                        "home": {"team": {"id": 134, "name": "Pittsburgh Pirates"}, "probablePitcher": {"id": 1999, "fullName": "Pitcher 1999"}},
                        "away": {"team": {"id": 136, "name": "Seattle Mariners"}, "probablePitcher": {"id": 2999, "fullName": "Pitcher 2999"}},
                    },
                    "venue": {"id": 31, "name": "PNC Park"},
                },
            )
        )
        session.flush()
        return 1

    monkeypatch.setattr(features, "_hydrate_schedule_window", hydrate_schedule)

    try:
        with Session(engine) as session:
            result = features.sync_mlb_features(session, date(2026, 6, 24))
            team = session.scalar(select(TeamDailyFeature).where(TeamDailyFeature.source == features.MLB_STATS_SOURCE))
            pitcher = session.scalar(
                select(PitcherDailyFeature)
                .where(PitcherDailyFeature.source == features.MLB_STATS_SOURCE)
                .where(PitcherDailyFeature.pitcher_id == "1999")
            )
            snapshot = session.scalar(select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_VERSION))
    finally:
        get_settings.cache_clear()

    assert result["pybaseball_fangraphs_status"] == "unavailable_http_403"
    assert result["player_mapping_failed_count"] == 0
    assert result["mlb_stats_api_primary_available_count"] > 0
    assert result["pitcher_season_stats_available_count"] > 0
    assert result["starter_recent_available_count"] > 0
    assert result["starter_workload_available_count"] > 0
    assert result["statcast_rows_seen"] > 0
    assert result["statcast_pitcher_rows_seen"] > 0
    assert team is not None
    assert team.source_status == "available"
    assert team.features["contact_quality_status"] == "available"
    assert pitcher is not None
    assert pitcher.source_status == "available"
    assert pitcher.features["recent"]["source_status"] == "available"
    assert pitcher.features["workload"]["expected_innings_projection"] >= 6.0
    assert snapshot is not None
    assert snapshot.features["offense_season"]["home"]["source"] == features.MLB_STATS_SOURCE
    assert snapshot.features["starter_recent"]["home"]["source_status"] == "available"
    assert snapshot.features["starter_workload"]["home"]["source_status"] == "available"
    assert snapshot.features["handedness_platoon"]["source_status"] == "available"


def test_mlb_stats_primary_hydrates_feed_before_collecting_pitcher_ids(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    pitcher_season_calls: list[str] = []
    pitcher_log_calls: list[str] = []

    class FakeMLBStatsClient:
        def get_pitcher_season_stats(self, person_id: str, season: int):
            pitcher_season_calls.append(str(person_id))
            return {
                "stats": [
                    {
                        "splits": [
                            {
                                "stat": {
                                    "era": "3.40",
                                    "whip": "1.10",
                                    "inningsPitched": "72.1",
                                    "strikeOuts": 82,
                                    "baseOnBalls": 20,
                                    "hits": 60,
                                    "homeRuns": 8,
                                    "gamesStarted": 12,
                                    "battersFaced": 290,
                                }
                            }
                        ]
                    }
                ]
            }

        def get_pitcher_game_log_stats(self, person_id: str, season: int):
            pitcher_log_calls.append(str(person_id))
            return {
                "stats": [
                    {
                        "splits": [
                            {
                                "date": f"2026-06-{22 - index:02d}",
                                "stat": {
                                    "inningsPitched": "6.0",
                                    "gamesStarted": 1,
                                    "strikeOuts": 7,
                                    "baseOnBalls": 2,
                                    "hits": 5,
                                    "homeRuns": 1,
                                    "runs": 2,
                                    "earnedRuns": 2,
                                    "numberOfPitches": 91,
                                },
                            }
                            for index in range(5)
                        ]
                        + [
                            {
                                "date": "2026-06-23",
                                "stat": {
                                    "inningsPitched": "1.0",
                                    "gamesStarted": 0,
                                    "strikeOuts": 1,
                                    "baseOnBalls": 0,
                                    "hits": 1,
                                    "homeRuns": 0,
                                    "runs": 0,
                                    "earnedRuns": 0,
                                    "numberOfPitches": 14,
                                },
                            }
                        ]
                    }
                ]
            }

    def hydrate_feed(game: MlbGame) -> None:
        game.raw_payload = {
            **(game.raw_payload or {}),
            "liveData": {
                "boxscore": {
                    "teams": {
                        "home": {
                            "pitchers": [1999],
                            "players": {
                                "ID1999": {
                                    "person": {"id": 1999, "fullName": "Pitcher 1999"},
                                    "pitchHand": {"code": "R"},
                                }
                            },
                        },
                        "away": {
                            "pitchers": [2999],
                            "players": {
                                "ID2999": {
                                    "person": {"id": 2999, "fullName": "Pitcher 2999"},
                                    "pitchHand": {"code": "L"},
                                }
                            },
                        },
                    }
                }
            },
        }

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)
    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", hydrate_feed)

    try:
        with Session(engine) as session:
            session.add(
                MlbGame(
                    external_game_id="hydrate-before-primary-pitchers",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 6, 24, 23, 0, tzinfo=UTC),
                    status="scheduled",
                    raw_payload={"venue": {"id": 31, "name": "PNC Park"}},
                )
            )
            session.commit()

            result = features.sync_mlb_pitcher_features(session, date(2026, 6, 24), refresh_schedule=False)
            pitcher = session.scalar(
                select(PitcherDailyFeature)
                .where(PitcherDailyFeature.source == features.MLB_STATS_SOURCE)
                .where(PitcherDailyFeature.pitcher_id == "1999")
            )
    finally:
        get_settings.cache_clear()

    assert result["probable_starters_seen"] == 2
    assert pitcher_season_calls == []
    assert pitcher_log_calls == ["1999", "2999"]
    assert pitcher is not None
    assert pitcher.features["season"]["stats_basis"] == "pitcher_game_logs_before_target_date"
    assert pitcher.features["recent"]["source_status"] == "available"
    assert pitcher.features["recent"]["last_5_starts"]["sample"]["last_date"] == "2026-06-22"
    assert pitcher.features["workload"]["source_status"] == "available"


def test_mlb_primary_pitcher_uses_logs_before_target_date(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    season_calls: list[str] = []

    class FakeMLBStatsClient:
        def get_pitcher_season_stats(self, person_id: str, season: int):
            season_calls.append(str(person_id))
            return {
                "stats": [
                    {
                        "splits": [
                            {
                                "stat": {
                                    "era": "0.01",
                                    "whip": "0.01",
                                    "inningsPitched": "200.0",
                                    "strikeOuts": 999,
                                    "gamesStarted": 30,
                                }
                            }
                        ]
                    }
                ]
            }

        def get_pitcher_game_log_stats(self, person_id: str, season: int):
            return {
                "stats": [
                    {
                        "splits": [
                            {
                                "date": "2026-06-30",
                                "stat": {
                                    "inningsPitched": "6.0",
                                    "gamesStarted": 1,
                                    "strikeOuts": 6,
                                    "baseOnBalls": 2,
                                    "hits": 5,
                                    "homeRuns": 1,
                                    "runs": 2,
                                    "earnedRuns": 2,
                                    "numberOfPitches": 90,
                                    "battersFaced": 25,
                                },
                            },
                            {
                                "date": "2026-07-01",
                                "stat": {
                                    "inningsPitched": "9.0",
                                    "gamesStarted": 1,
                                    "strikeOuts": 20,
                                    "baseOnBalls": 0,
                                    "hits": 0,
                                    "homeRuns": 0,
                                    "runs": 0,
                                    "earnedRuns": 0,
                                    "numberOfPitches": 110,
                                    "battersFaced": 27,
                                },
                            },
                            {
                                "date": "2026-07-02",
                                "stat": {
                                    "inningsPitched": "9.0",
                                    "gamesStarted": 1,
                                    "strikeOuts": 30,
                                    "baseOnBalls": 0,
                                    "hits": 0,
                                    "homeRuns": 0,
                                    "runs": 0,
                                    "earnedRuns": 0,
                                    "numberOfPitches": 110,
                                    "battersFaced": 27,
                                },
                            },
                        ]
                    }
                ]
            }

        def get_game_feed(self, *_args, **_kwargs):
            return {}

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)
    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", lambda *_args, **_kwargs: None)

    try:
        with Session(engine) as session:
            session.add(
                MlbGame(
                    external_game_id="bounded-primary-pitcher",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="scheduled",
                    raw_payload={
                        "teams": {
                            "home": {
                                "team": {"id": 134, "name": "Pittsburgh Pirates"},
                                "probablePitcher": {"id": 1999, "fullName": "Pitcher 1999"},
                            },
                            "away": {
                                "team": {"id": 136, "name": "Seattle Mariners"},
                                "probablePitcher": {"id": 2999, "fullName": "Pitcher 2999"},
                            },
                        }
                    },
                )
            )
            session.commit()

            result = features.sync_mlb_pitcher_features(session, date(2026, 7, 1), refresh_schedule=False)
            pitcher = session.scalar(
                select(PitcherDailyFeature)
                .where(PitcherDailyFeature.source == features.MLB_STATS_SOURCE)
                .where(PitcherDailyFeature.pitcher_id == "1999")
            )
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] == "ok"
    assert season_calls == []
    assert pitcher is not None
    assert pitcher.source_status == "available"
    assert pitcher.features["season"]["stats_basis"] == "pitcher_game_logs_before_target_date"
    assert pitcher.features["season"]["stats_cutoff_date"] == "2026-07-01"
    assert pitcher.features["season"]["innings_pitched"] == 6.0
    assert pitcher.features["season"]["strikeouts"] == 6
    assert pitcher.features["season"]["era"] == 3.0
    assert pitcher.features["season"]["whip"] == 1.1667
    assert pitcher.features["workload"]["expected_innings_projection"] == 6.0
    assert pitcher.raw_payload["season"]["bounded_before"] == "2026-07-01"
    assert pitcher.raw_payload["season"]["rows"] == 1


def test_mlb_primary_team_recent_uses_calendar_window(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    def game_log_row(date_value: str, runs: int, hits: int, home_runs: int, earned_runs: int) -> dict[str, object]:
        return {
            "date": date_value,
            "stat": {
                "runs": runs,
                "hits": hits,
                "homeRuns": home_runs,
                "baseOnBalls": 3,
                "strikeOuts": 6,
                "atBats": 32,
                "plateAppearances": 36,
                "totalBases": hits + (home_runs * 3),
                "inningsPitched": "9.0",
                "earnedRuns": earned_runs,
                "battersFaced": 36,
                "numberOfPitches": 140,
            },
        }

    def game_log_rows() -> list[dict[str, object]]:
        return [
            game_log_row("2026-06-30", runs=5, hits=8, home_runs=1, earned_runs=3),
            game_log_row("2026-06-25", runs=4, hits=7, home_runs=0, earned_runs=2),
            game_log_row("2026-06-20", runs=8, hits=11, home_runs=3, earned_runs=6),
        ]

    class FakeMLBStatsClient:
        def get_team_season_stats(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_team_game_log_stats(self, *_args, **_kwargs):
            return {"stats": [{"splits": game_log_rows()}]}

        def get_team_stat_splits(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)
    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", lambda *_args, **_kwargs: None)

    try:
        with Session(engine) as session:
            session.add(
                MlbGame(
                    external_game_id="calendar-window-team-recent",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="scheduled",
                    raw_payload={
                        "teams": {
                            "home": {"team": {"id": 134, "name": "Pittsburgh Pirates"}},
                            "away": {"team": {"id": 136, "name": "Seattle Mariners"}},
                        }
                    },
                )
            )
            session.commit()

            result = features.sync_mlb_team_features(session, date(2026, 7, 1), refresh_schedule=False)
            recent_7 = session.scalar(
                select(TeamRecentFeature)
                .where(TeamRecentFeature.source == features.MLB_STATS_SOURCE)
                .where(TeamRecentFeature.team_code == "PIT")
                .where(TeamRecentFeature.window_days == 7)
            )
            recent_14 = session.scalar(
                select(TeamRecentFeature)
                .where(TeamRecentFeature.source == features.MLB_STATS_SOURCE)
                .where(TeamRecentFeature.team_code == "PIT")
                .where(TeamRecentFeature.window_days == 14)
            )
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] == "ok"
    assert recent_7 is not None
    assert recent_7.features["hitting"]["game_count"] == 2
    assert recent_14 is not None
    assert recent_14.features["hitting"]["game_count"] == 3


def test_mlb_primary_team_daily_uses_logs_before_target_date(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    season_calls: list[str] = []

    class FakeMLBStatsClient:
        def get_team_season_stats(self, group: str, season: int):
            season_calls.append(group)
            return {
                "stats": [
                    {
                        "splits": [
                            {
                                "team": {"id": 134},
                                "stat": {
                                    "gamesPlayed": 162,
                                    "runs": 999,
                                    "hits": 999,
                                    "homeRuns": 999,
                                },
                            }
                        ]
                    }
                ]
            }

        def get_team_game_log_stats(self, team_id: str, group: str, season: int):
            if group == "hitting":
                rows = [
                    {
                        "date": "2026-06-30",
                        "stat": {
                            "runs": 4,
                            "hits": 8,
                            "homeRuns": 1,
                            "baseOnBalls": 2,
                            "strikeOuts": 7,
                            "atBats": 32,
                            "plateAppearances": 36,
                            "totalBases": 13,
                        },
                    },
                    {
                        "date": "2026-07-01",
                        "stat": {
                            "runs": 50,
                            "hits": 50,
                            "homeRuns": 10,
                            "baseOnBalls": 10,
                            "strikeOuts": 1,
                            "atBats": 40,
                            "plateAppearances": 50,
                            "totalBases": 90,
                        },
                    },
                    {
                        "date": "2026-07-02",
                        "stat": {
                            "runs": 60,
                            "hits": 60,
                            "homeRuns": 12,
                            "baseOnBalls": 12,
                            "strikeOuts": 1,
                            "atBats": 44,
                            "plateAppearances": 56,
                            "totalBases": 110,
                        },
                    },
                ]
            else:
                rows = [
                    {
                        "date": "2026-06-30",
                        "stat": {
                            "inningsPitched": "9.0",
                            "runs": 3,
                            "earnedRuns": 3,
                            "hits": 7,
                            "homeRuns": 1,
                            "baseOnBalls": 2,
                            "strikeOuts": 8,
                            "battersFaced": 35,
                            "numberOfPitches": 135,
                            "saves": 1,
                            "holds": 0,
                            "blownSaves": 0,
                            "saveOpportunities": 1,
                            "gamesFinished": 1,
                        },
                    },
                    {
                        "date": "2026-07-02",
                        "stat": {
                            "inningsPitched": "9.0",
                            "runs": 99,
                            "earnedRuns": 99,
                            "hits": 99,
                            "homeRuns": 20,
                            "baseOnBalls": 20,
                            "strikeOuts": 1,
                            "battersFaced": 60,
                            "numberOfPitches": 200,
                        },
                    },
                ]
            return {"stats": [{"splits": rows}]}

        def get_team_stat_splits(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {}

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)
    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", lambda *_args, **_kwargs: None)

    try:
        with Session(engine) as session:
            session.add(
                MlbGame(
                    external_game_id="bounded-primary-team-daily",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="scheduled",
                    raw_payload={
                        "teams": {
                            "home": {"team": {"id": 134, "name": "Pittsburgh Pirates"}},
                            "away": {"team": {"id": 136, "name": "Seattle Mariners"}},
                        }
                    },
                )
            )
            session.commit()

            result = features.sync_mlb_team_features(session, date(2026, 7, 1), refresh_schedule=False)
            team_daily = session.scalar(
                select(TeamDailyFeature)
                .where(TeamDailyFeature.source == features.MLB_STATS_SOURCE)
                .where(TeamDailyFeature.team_code == "PIT")
            )
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] == "ok"
    assert season_calls == []
    assert team_daily is not None
    assert team_daily.source_status == "available"
    assert team_daily.features["stats_basis"] == "team_game_logs_before_target_date"
    assert team_daily.features["sample_size"] == 1
    assert team_daily.features["runs"] == 4.0
    assert team_daily.features["runs_allowed"] == 3.0
    assert team_daily.features["home_runs"] == 1
    assert team_daily.raw_payload["bounded_before"] == "2026-07-01"
    assert team_daily.raw_payload["hitting_rows"] == 1
    assert team_daily.raw_payload["pitching_rows"] == 1


def test_bullpen_only_sync_skips_mlb_primary_team_fetches(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FailingTeamStatsClient:
        def get_team_season_stats(self, *_args, **_kwargs):
            raise AssertionError("bullpen-only sync should not fetch team season stats")

        def get_team_game_log_stats(self, *_args, **_kwargs):
            raise AssertionError("bullpen-only sync should not fetch team game logs")

        def get_team_stat_splits(self, *_args, **_kwargs):
            raise AssertionError("bullpen-only sync should not fetch team stat splits")

        def get_game_feed(self, *_args, **_kwargs):
            return {}

    monkeypatch.setattr(features, "MLBStatsClient", FailingTeamStatsClient)
    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", lambda *_args, **_kwargs: None)

    try:
        with Session(engine) as session:
            session.add(
                MlbGame(
                    external_game_id="bullpen-only-primary-skip",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="scheduled",
                    raw_payload={
                        "teams": {
                            "home": {"team": {"id": 134, "name": "Pittsburgh Pirates"}},
                            "away": {"team": {"id": 136, "name": "Seattle Mariners"}},
                        }
                    },
                )
            )
            session.commit()

            result = features.sync_mlb_bullpen_features(session, date(2026, 7, 1), refresh_schedule=False)
            bullpen_rows = list(session.scalars(select(BullpenDailyFeature)))
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] in {"ok", "degraded_no_available_public_rows"}
    assert result["error_count"] == 0
    assert len(bullpen_rows) == 2


def test_mlb_primary_team_fetch_timeout_degrades_sync(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    season_calls: list[str] = []

    class TimeoutTeamStatsClient:
        def get_team_season_stats(self, *_args, **_kwargs):
            season_calls.append("season")
            return {"stats": [{"splits": []}]}

        def get_team_game_log_stats(self, _team_id: str, group: str, _season: int):
            if group == "hitting":
                raise TimeoutError("team hitting log timed out")
            return {"stats": [{"splits": []}]}

        def get_team_stat_splits(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {}

    monkeypatch.setattr(features, "MLBStatsClient", TimeoutTeamStatsClient)
    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", lambda *_args, **_kwargs: None)

    try:
        with Session(engine) as session:
            session.add(
                MlbGame(
                    external_game_id="team-primary-timeout",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="scheduled",
                    raw_payload={
                        "teams": {
                            "home": {"team": {"id": 134, "name": "Pittsburgh Pirates"}},
                            "away": {"team": {"id": 136, "name": "Seattle Mariners"}},
                        }
                    },
                )
            )
            session.commit()

            result = features.sync_mlb_team_features(session, date(2026, 7, 1), refresh_schedule=False)
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] == "degraded_with_errors"
    assert result["error_count"] == 2
    assert season_calls == []
    assert {error["table"] for error in result["errors"]} == {"team_hitting_game_log"}
    assert all(error["error_type"] == "TimeoutError" for error in result["errors"])


def test_statcast_team_rows_use_pybaseball_team_aliases(monkeypatch) -> None:
    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {"available": True, "version": "2.2.7", "module_path": "mocked", "import_error": None},
    )
    monkeypatch.setattr(
        features.pybaseball_client,
        "get_statcast_range",
        lambda *_args, **_kwargs: {
            "rows": [
                {"bat_team": "CHW", "launch_speed": 96, "launch_angle": 18, "estimated_woba_using_speedangle": 0.360}
                for _ in range(30)
            ]
        },
    )

    stats = features._new_sync_stats(date(2026, 6, 24), {"team"})
    context = features._statcast_fetch_context([], date(2026, 6, 24), {"team"}, stats)

    assert "CWS" in context["team_contact_by_code"]
    assert "CHW" not in context["team_contact_by_code"]
    assert context["team_contact_by_code"]["CWS"]["batted_ball_events_count"] == 30


def test_statcast_team_rows_derive_batting_team_from_inning_half(monkeypatch) -> None:
    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {"available": True, "version": "2.2.7", "module_path": "mocked", "import_error": None},
    )
    monkeypatch.setattr(
        features.pybaseball_client,
        "get_statcast_range",
        lambda *_args, **_kwargs: {
            "rows": [
                {
                    "home_team": "PIT",
                    "away_team": "CHW",
                    "inning_topbot": "Top",
                    "launch_speed": 96,
                    "launch_angle": 18,
                }
                for _ in range(30)
            ]
            + [
                {
                    "home_team": "PIT",
                    "away_team": "CHW",
                    "inning_topbot": "Bot",
                    "launch_speed": 91,
                    "launch_angle": 12,
                }
                for _ in range(30)
            ]
        },
    )

    stats = features._new_sync_stats(date(2026, 6, 24), {"team"})
    context = features._statcast_fetch_context([], date(2026, 6, 24), {"team"}, stats)

    assert "CWS" in context["team_contact_by_code"]
    assert "PIT" in context["team_contact_by_code"]
    assert context["team_contact_by_code"]["CWS"]["batted_ball_events_count"] == 30
    assert context["team_contact_by_code"]["PIT"]["batted_ball_events_count"] == 30
    assert stats["statcast_rows_matched"] == 60


def test_statcast_unmatched_team_rows_mark_source_attempted(monkeypatch) -> None:
    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {"available": True, "version": "2.2.7", "module_path": "mocked", "import_error": None},
    )
    monkeypatch.setattr(
        features.pybaseball_client,
        "get_statcast_range",
        lambda *_args, **_kwargs: {
            "rows": [{"launch_speed": 96, "launch_angle": 18, "estimated_woba_using_speedangle": 0.360}]
        },
    )

    stats = features._new_sync_stats(date(2026, 6, 24), {"team"})
    context = features._statcast_fetch_context([], date(2026, 6, 24), {"team"}, stats)

    assert context["team_contact_by_code"] == {}
    assert stats["statcast_rows_seen"] == 1
    assert stats["statcast_rows_matched"] == 0
    assert stats["statcast_source_status"] == "statcast_unmatched_team_rows"
    assert stats["warnings"] == [
        "Statcast/Savant team contact returned rows but none mapped to known team codes."
    ]


def test_statcast_unmatched_team_rows_survive_pitcher_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {"available": True, "version": "2.2.7", "module_path": "mocked", "import_error": None},
    )
    monkeypatch.setattr(
        features.pybaseball_client,
        "get_statcast_range",
        lambda *_args, **_kwargs: {"rows": [{"launch_speed": 96, "launch_angle": 18}]},
    )
    monkeypatch.setattr(
        features.pybaseball_client,
        "get_pitcher_statcast_range",
        lambda *_args, **_kwargs: {"rows": [{"release_speed": 96.0, "launch_speed": 88.0, "launch_angle": 12.0}]},
    )
    game = MlbGame(
        external_game_id="statcast-unmatched-team-pitcher-available",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 6, 24, 23, 0, tzinfo=UTC),
        status="scheduled",
        raw_payload={"teams": {"home": {"probablePitcher": {"id": 1999, "fullName": "Home Starter"}}}},
    )

    stats = features._new_sync_stats(date(2026, 6, 24), {"team", "pitcher"})
    context = features._statcast_fetch_context([game], date(2026, 6, 24), {"team", "pitcher"}, stats)

    assert context["team_contact_by_code"] == {}
    assert context["pitcher_contact_by_id"]["1999"]["average_release_speed"] == 96.0
    assert stats["statcast_source_status"] == "statcast_unmatched_team_rows"
    assert stats["statcast_pitcher_rows_matched"] == 1


def test_statcast_empty_team_result_survives_pitcher_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {"available": True, "version": "2.2.7", "module_path": "mocked", "import_error": None},
    )
    monkeypatch.setattr(features.pybaseball_client, "get_statcast_range", lambda *_args, **_kwargs: {"rows": []})
    monkeypatch.setattr(
        features.pybaseball_client,
        "get_pitcher_statcast_range",
        lambda *_args, **_kwargs: {"rows": [{"release_speed": 96.0, "launch_speed": 88.0, "launch_angle": 12.0}]},
    )
    game = MlbGame(
        external_game_id="statcast-empty-team-pitcher-available",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 6, 24, 23, 0, tzinfo=UTC),
        status="scheduled",
        raw_payload={"teams": {"home": {"probablePitcher": {"id": 1999, "fullName": "Home Starter"}}}},
    )

    stats = features._new_sync_stats(date(2026, 6, 24), {"team", "pitcher"})
    context = features._statcast_fetch_context([game], date(2026, 6, 24), {"team", "pitcher"}, stats)

    assert context["team_contact_by_code"] == {}
    assert context["pitcher_contact_by_id"]["1999"]["average_release_speed"] == 96.0
    assert stats["statcast_source_status"] == "statcast_empty_result"
    assert stats["statcast_pitcher_rows_matched"] == 1


def test_statcast_empty_pitcher_result_marks_source_attempted(monkeypatch) -> None:
    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {"available": True, "version": "2.2.7", "module_path": "mocked", "import_error": None},
    )
    monkeypatch.setattr(features.pybaseball_client, "get_pitcher_statcast_range", lambda *_args, **_kwargs: {"rows": []})
    game = MlbGame(
        external_game_id="statcast-empty-pitcher",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 6, 24, 23, 0, tzinfo=UTC),
        status="scheduled",
        raw_payload={"teams": {"home": {"probablePitcher": {"id": 1999, "fullName": "Home Starter"}}}},
    )

    stats = features._new_sync_stats(date(2026, 6, 24), {"pitcher"})
    context = features._statcast_fetch_context([game], date(2026, 6, 24), {"pitcher"}, stats)

    assert context["pitcher_contact_by_id"] == {}
    assert stats["statcast_pitcher_rows_seen"] == 0
    assert stats["statcast_pitcher_rows_matched"] == 0
    assert stats["statcast_source_status"] == "statcast_pitcher_empty_result"
    assert stats["warnings"] == [
        "Statcast/Savant pitcher contact returned no rows for probable starters in the completed date range."
    ]


def test_statcast_contact_uses_all_release_speed_and_numeric_barrels() -> None:
    contact = features._aggregate_statcast_contact(
        [
            {"release_speed": 95.0},
            {
                "release_speed": 97.0,
                "launch_speed": 100.0,
                "launch_angle": 24.0,
                "launch_speed_angle": 6.0,
            },
            {
                "launch_speed": 88.0,
                "launch_angle": 10.0,
                "launch_speed_angle": "barrel",
            },
        ]
    )

    assert contact["batted_ball_events_count"] == 2
    assert contact["average_exit_velocity"] == 94.0
    assert contact["average_release_speed"] == 96.0
    assert contact["barrel_count"] == 2
    assert contact["barrel_pct"] == 1.0


def test_weather_sync_handles_open_meteo_failure_without_500(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    def failing_weather(_profile: dict[str, object], _scheduled_start: datetime) -> dict[str, object]:
        raise HttpJsonError("weather unavailable", endpoint="https://weather.test", params={})

    monkeypatch.setattr(features, "_hydrate_schedule_window", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(features, "_fetch_open_meteo", failing_weather)
    try:
        with Session(engine) as session:
            session.add(
                MlbGame(
                    external_game_id="weather-failure-1",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="scheduled",
                    raw_payload={"venue": {"id": 31, "name": "PNC Park"}},
                )
            )
            session.commit()

            result = features.sync_weather_features(session, date(2026, 7, 1))
            weather = session.scalar(select(WeatherSnapshot))
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] == "degraded_with_errors"
    assert result["error_count"] == 1
    assert result["errors"][0]["source"] == features.OPEN_METEO_SOURCE
    assert result["errors"][0]["table"] == "weather_snapshots"
    assert weather is not None
    assert weather.source_status == "missing"
    assert weather.raw_payload["error"]["source"] == features.OPEN_METEO_SOURCE
    assert weather.raw_payload["error"]["error_type"] == "HttpJsonError"


def test_lineup_sync_handles_missing_lineup_without_500(monkeypatch) -> None:
    _stub_public_feature_network(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="lineup-missing-1",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
                raw_payload={"venue": {"id": 31, "name": "PNC Park"}},
            )
        )
        session.commit()

        result = features.sync_mlb_lineups(session, date(2026, 7, 1))
        lineup = session.scalar(select(LineupSnapshot).where(LineupSnapshot.team_code == "PIT"))

    assert result["validation_status"] == "degraded_no_available_public_rows"
    assert lineup is not None
    assert lineup.source_status == "missing"
    assert lineup.features["missing_reason"] == "LINEUP_NOT_POSTED_YET"


def test_lineup_sync_marks_live_feed_failure_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FailingFeedClient:
        def get_game_feed(self, *_args, **_kwargs):
            raise HttpJsonError("live feed unavailable", endpoint="https://statsapi.test/feed", params={})

    monkeypatch.setattr(features, "MLBStatsClient", FailingFeedClient)

    try:
        with Session(engine) as session:
            session.add(
                MlbGame(
                    external_game_id="123456",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="scheduled",
                    raw_payload={"venue": {"id": 31, "name": "PNC Park"}},
                )
            )
            session.commit()

            result = features.sync_mlb_lineups(session, date(2026, 7, 1))
            lineup = session.scalar(select(LineupSnapshot).where(LineupSnapshot.team_code == "PIT"))
            game = session.scalar(select(MlbGame).where(MlbGame.external_game_id == "123456"))
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] == "degraded_with_errors"
    assert result["errors"][0]["table"] == "mlb_games_feed"
    assert lineup is not None
    assert lineup.source_status == "missing"
    assert lineup.features["missing_reason"] == features.LIVE_FEED_UNAVAILABLE
    assert game is not None
    assert game.raw_payload[features.LIVE_FEED_HYDRATION_KEY]["errors"][0]["table"] == "mlb_games_feed"


def test_lineup_sync_ignores_stale_starter_feed_error_after_feed_success(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class SuccessfulFeedClient:
        def get_game_feed(self, *_args, **_kwargs):
            return {"gameData": {"datetime": {"dateTime": "2026-07-01T23:00:00Z"}}}

    monkeypatch.setattr(features, "MLBStatsClient", SuccessfulFeedClient)

    try:
        with Session(engine) as session:
            session.add(
                MlbGame(
                    external_game_id="123456",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="scheduled",
                    raw_payload={
                        "venue": {"id": 31, "name": "PNC Park"},
                        "homerun_starter_hydration": {
                            "errors": [{"table": "mlb_games_feed", "message": "older feed outage"}]
                        },
                    },
                )
            )
            session.commit()

            result = features.sync_mlb_lineups(session, date(2026, 7, 1))
            lineup = session.scalar(select(LineupSnapshot).where(LineupSnapshot.team_code == "PIT"))
            game = session.scalar(select(MlbGame).where(MlbGame.external_game_id == "123456"))
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] == "degraded_no_available_public_rows"
    assert lineup is not None
    assert lineup.source_status == "missing"
    assert lineup.features["missing_reason"] == features.LINEUP_NOT_POSTED_YET
    assert game is not None
    assert features.LIVE_FEED_HYDRATION_KEY not in game.raw_payload


def test_feature_ingestion_scope_excludes_live_execution_team_totals_and_umpires() -> None:
    searchable = " ".join(
        [
            *features.RAW_TABLES_BY_MODULE.keys(),
            *features.ALL_SYNC_MODULES,
            *features.CORE_MODULES,
        ]
    ).lower()

    assert "team_total" not in searchable
    assert "team total" not in searchable
    assert "umpire" not in searchable
    assert "order" not in searchable
    assert "execution" not in searchable


def test_public_feature_sync_writes_raw_tables_from_fixture_payloads(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    _stub_pybaseball_unavailable(monkeypatch)
    _stub_mlb_primary_stats_empty(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    def boxscore_team(start_id: int, pitcher_id: int) -> dict[str, object]:
        batters = list(range(start_id, start_id + 9))
        players: dict[str, object] = {}
        for index, person_id in enumerate(batters, start=1):
            players[f"ID{person_id}"] = {
                "person": {"id": person_id, "fullName": f"Starter {person_id}"},
                "battingOrder": str(index * 100),
                "batSide": {"code": "L" if index % 2 else "R"},
                "position": {"abbreviation": "C" if index == 9 else "CF"},
            }
        players[f"ID{pitcher_id}"] = {
            "person": {"id": pitcher_id, "fullName": f"Pitcher {pitcher_id}"},
            "pitchHand": {"code": "R"},
        }
        return {"batters": batters, "pitchers": [pitcher_id], "players": players}

    def hydrate_schedule(session: Session, day: date, **_kwargs) -> int:
        game = MlbGame(
            external_game_id="12345",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
            raw_payload={
                "venue": {"id": 31, "name": "PNC Park"},
                "teams": {
                    "home": {"leagueRecord": {"wins": 44, "losses": 39}},
                    "away": {"leagueRecord": {"wins": 41, "losses": 42}},
                },
            },
        )
        previous = MlbGame(
            external_game_id="12344",
            home_team="Seattle Mariners",
            away_team="Pittsburgh Pirates",
            home_abbreviation="SEA",
            away_abbreviation="PIT",
            scheduled_start=datetime(2026, 6, 29, 23, 0, tzinfo=UTC),
            status="Final",
            home_score=3,
            away_score=5,
            raw_payload={"venue": {"id": 680, "name": "T-Mobile Park"}},
        )
        session.add_all([game, previous])
        session.flush()
        return 2

    def hydrate_feed(game: MlbGame) -> None:
        game.raw_payload = {
            **(game.raw_payload or {}),
            "liveData": {
                "boxscore": {
                    "teams": {
                        "home": boxscore_team(1000, 1999),
                        "away": boxscore_team(2000, 2999),
                    }
                }
            },
        }

    def fake_open_meteo(_profile: dict[str, object], _scheduled_start: datetime) -> dict[str, object]:
        return {
            "hourly": {
                "time": ["2026-07-01T19:00"],
                "temperature_2m": [82],
                "relative_humidity_2m": [61],
                "precipitation_probability": [12],
                "precipitation": [0],
                "rain": [0],
                "wind_speed_10m": [9],
                "wind_direction_10m": [220],
                "wind_gusts_10m": [15],
                "cloud_cover": [25],
            }
        }

    monkeypatch.setattr(features, "_hydrate_schedule_window", hydrate_schedule)
    monkeypatch.setattr(features, "_hydrate_game_endpoint_if_available", hydrate_feed)
    monkeypatch.setattr(features, "_fetch_open_meteo", fake_open_meteo)

    try:
        with Session(engine) as session:
            result = features.sync_mlb_features(session, date(2026, 7, 1))
            team_daily = session.scalar(select(TeamDailyFeature).where(TeamDailyFeature.team_code == "PIT"))
            team_recent = session.scalar(select(TeamRecentFeature).where(TeamRecentFeature.team_code == "PIT"))
            pitcher = session.scalar(select(PitcherDailyFeature).where(PitcherDailyFeature.team_code == "PIT"))
            bullpen = session.scalar(select(BullpenDailyFeature).where(BullpenDailyFeature.team_code == "PIT"))
            lineup = session.scalar(select(LineupSnapshot).where(LineupSnapshot.team_code == "PIT"))
            weather = session.scalar(select(WeatherSnapshot))
            snapshot = session.scalar(select(MlbFeatureSnapshot))
    finally:
        get_settings.cache_clear()

    assert result["network_sources_enabled"] is True
    assert result["validation_status"] == "ok"
    assert result["rows_inserted"] > 0
    assert "lineup_snapshots" in result["tables_written"]
    assert "weather_snapshots" in result["tables_written"]
    assert team_daily is not None
    assert team_daily.source_status == "partial"
    assert team_daily.features["runs_per_game"] == 5.0
    assert team_recent is not None
    assert pitcher is not None
    assert pitcher.source_status == "partial"
    assert bullpen is not None
    assert bullpen.source_status == "partial"
    assert lineup is not None
    assert lineup.confirmed is True
    assert lineup.features["starters"][0]["is_starter"] is True
    assert weather is not None
    assert weather.source_status == "available"
    assert weather.features["temperature"] == 82
    assert snapshot is not None
    assert snapshot.source_statuses["market_context"] == "missing"
    assert snapshot.source_statuses["lineup"] == {"home": "available", "away": "available"}


def test_source_status_report_exposes_public_source_diagnostics() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured_at = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)

    with Session(engine) as session:
        session.add(
            WeatherSnapshot(
                mlb_game_id=1,
                target_date=date(2026, 7, 1),
                venue_name="PNC Park",
                captured_at=captured_at,
                source=features.OPEN_METEO_SOURCE,
                source_status="available",
                confidence=Decimal("0.70"),
                completeness=Decimal("0.70"),
                stale=False,
                features={"temperature": 80},
                raw_payload={"ok": True},
            )
        )
        session.commit()
        report = features.source_status_report(session)

    assert report["feature_sync_enable_network_sources"] is True
    assert report["public_sources_enabled"] is True
    assert report["optional_weather_provider_configured"] is False
    assert report["last_successful_sync"]["weather_snapshots"] == captured_at.isoformat()
    assert "weather_snapshots" in report["tables"]


def test_source_status_report_includes_configured_optional_weather_provider(monkeypatch) -> None:
    monkeypatch.setenv("WEATHER_PROVIDER_API_KEY", "weather-test-key")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            report = features.source_status_report(session)
    finally:
        get_settings.cache_clear()

    inventory = {item["source_name"]: item for item in report["source_inventory"]}
    assert report["optional_weather_provider_configured"] is True
    assert inventory["optional_weather_provider"]["status"] == "not_wired"
    assert inventory["optional_weather_provider"]["modules_affected"] == ["park_weather"]


def test_source_status_report_exposes_source_inventory_and_cached_fallbacks(monkeypatch) -> None:
    monkeypatch.setenv("ADVANCED_PUBLIC_STATS_MAX_STALE_HOURS", "72")
    monkeypatch.setenv("STATCAST_CACHE_MAX_STALE_HOURS", "48")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured_at = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
    attempted_at = datetime(2026, 7, 1, 21, 0, tzinfo=UTC).isoformat()
    cached_contact = {
        "source": features.STATCAST_SOURCE,
        "captured_at": captured_at.isoformat(),
        "batted_ball_events_count": 30,
        "hard_hit_pct": 0.42,
    }
    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {"available": True, "version": "2.2.7", "module_path": "mocked", "import_error": None},
    )

    with Session(engine) as session:
        session.add_all(
            [
                TeamDailyFeature(
                    target_date=date(2026, 7, 1),
                    team_code="PIT",
                    captured_at=captured_at,
                    source=features.PYBASEBALL_SOURCE,
                    source_status="available",
                    confidence=Decimal("0.8500"),
                    completeness=Decimal("0.7500"),
                    stale=False,
                    features={"wrc_plus": 112},
                    raw_payload={"source_function": "batting_stats"},
                ),
                TeamDailyFeature(
                    target_date=date(2026, 7, 1),
                    team_code="PIT",
                    captured_at=captured_at,
                    source=features.MLB_STATS_SOURCE,
                    source_status="available",
                    confidence=Decimal("0.8500"),
                    completeness=Decimal("0.8000"),
                    stale=False,
                    features={"contact_quality": cached_contact, "contact_quality_status": "available"},
                    raw_payload={"bounded_before": "2026-07-01"},
                ),
                MlbFeatureSnapshot(
                    mlb_game_id=None,
                    target_date=date(2026, 7, 1),
                    source=features.FEATURE_SYNC_AUDIT_SOURCE,
                    captured_at=datetime(2026, 7, 1, 21, 1, tzinfo=UTC),
                    data_quality=None,
                    source_statuses={"sync": "degraded_with_errors"},
                    features={
                        "sync_status": {
                            "attempted_at": attempted_at,
                            "validation_status": "degraded_with_errors",
                            "errors": [
                                {
                                    "source": features.PYBASEBALL_SOURCE,
                                    "function": "batting_stats",
                                    "error_code": "fan_graphs_http_403",
                                    "message": "HTTP Error 403: Forbidden",
                                },
                                {
                                    "source": features.STATCAST_SOURCE,
                                    "table": "statcast_team_contact",
                                    "error_code": "statcast_request_failed",
                                    "message": "Statcast timeout",
                                },
                            ],
                            "warnings": [],
                        }
                    },
                ),
            ]
        )
        session.commit()
        report = features.source_status_report(session)

    inventory = {item["source_name"]: item for item in report["source_inventory"]}
    assert {
        "mlb_stats_api",
        "kalshi_public_market_data",
        "open_meteo",
        "static_homerun_reference",
        "derived_homerun",
        "pybaseball_fangraphs",
        "statcast_savant",
    } <= set(inventory)
    assert inventory["pybaseball_fangraphs"]["status"] == "cached"
    assert inventory["pybaseball_fangraphs"]["fallback_used"] is True
    assert inventory["pybaseball_fangraphs"]["fallback_reason"] == "fan_graphs_http_403"
    assert inventory["statcast_savant"]["status"] == "cached"
    assert inventory["statcast_savant"]["fallback_used"] is True
    assert inventory["statcast_savant"]["fallback_reason"] == "statcast_request_failed"
    assert report["statcast_savant_status"] == "cached"
    assert report["statcast_savant_row_sample_count"] == 1


def test_source_status_report_marks_empty_statcast_attempt_as_cached_fallback(monkeypatch) -> None:
    monkeypatch.setenv("STATCAST_CACHE_MAX_STALE_HOURS", "48")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    cached_at = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)

    try:
        with Session(engine) as session:
            session.add_all(
                [
                    TeamDailyFeature(
                        target_date=date(2026, 7, 1),
                        team_code="PIT",
                        captured_at=cached_at,
                        source=features.MLB_STATS_SOURCE,
                        source_status="available",
                        confidence=Decimal("0.8500"),
                        completeness=Decimal("0.8000"),
                        stale=False,
                        features={
                            "contact_quality": {
                                "source": features.STATCAST_SOURCE,
                                "captured_at": cached_at.isoformat(),
                                "batted_ball_events_count": 30,
                                "hard_hit_pct": 0.42,
                            },
                            "contact_quality_status": "available",
                        },
                        raw_payload={"bounded_before": "2026-07-01"},
                    ),
                    MlbFeatureSnapshot(
                        mlb_game_id=None,
                        target_date=date(2026, 7, 1),
                        source=features.FEATURE_SYNC_AUDIT_SOURCE,
                        captured_at=datetime(2026, 7, 1, 21, 0, tzinfo=UTC),
                        data_quality=None,
                        source_statuses={"sync": "completed"},
                        features={
                            "sync_status": {
                                "attempted_at": datetime(2026, 7, 1, 21, 0, tzinfo=UTC).isoformat(),
                                "validation_status": "completed",
                                "statcast_source_status": "statcast_empty_result",
                                "errors": [],
                                "warnings": [
                                    "Statcast/Savant team contact returned no rows for the completed date range."
                                ],
                            }
                        },
                    ),
                ]
            )
            session.commit()
            report = features.source_status_report(session)
    finally:
        get_settings.cache_clear()

    inventory = {item["source_name"]: item for item in report["source_inventory"]}
    assert report["statcast_savant_status"] == "cached"
    assert report["statcast_savant_last_error"]["error_code"] == "statcast_empty_result"
    assert inventory["statcast_savant"]["status"] == "cached"
    assert inventory["statcast_savant"]["fallback_used"] is True
    assert inventory["statcast_savant"]["fallback_reason"] == "statcast_empty_result"


def test_source_status_report_marks_unmatched_statcast_rows_as_cached_fallback(monkeypatch) -> None:
    monkeypatch.setenv("STATCAST_CACHE_MAX_STALE_HOURS", "48")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    cached_at = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)

    try:
        with Session(engine) as session:
            session.add_all(
                [
                    TeamDailyFeature(
                        target_date=date(2026, 7, 1),
                        team_code="PIT",
                        captured_at=cached_at,
                        source=features.MLB_STATS_SOURCE,
                        source_status="available",
                        confidence=Decimal("0.8500"),
                        completeness=Decimal("0.8000"),
                        stale=False,
                        features={
                            "contact_quality": {
                                "source": features.STATCAST_SOURCE,
                                "captured_at": cached_at.isoformat(),
                                "batted_ball_events_count": 30,
                                "hard_hit_pct": 0.42,
                            },
                            "contact_quality_status": "available",
                        },
                        raw_payload={"bounded_before": "2026-07-01"},
                    ),
                    MlbFeatureSnapshot(
                        mlb_game_id=None,
                        target_date=date(2026, 7, 1),
                        source=features.FEATURE_SYNC_AUDIT_SOURCE,
                        captured_at=datetime(2026, 7, 1, 21, 0, tzinfo=UTC),
                        data_quality=None,
                        source_statuses={"sync": "completed"},
                        features={
                            "sync_status": {
                                "attempted_at": datetime(2026, 7, 1, 21, 0, tzinfo=UTC).isoformat(),
                                "validation_status": "completed",
                                "statcast_source_status": "statcast_unmatched_team_rows",
                                "errors": [],
                                "warnings": [
                                    "Statcast/Savant team contact returned rows but none mapped to known team codes."
                                ],
                            }
                        },
                    ),
                ]
            )
            session.commit()
            report = features.source_status_report(session)
    finally:
        get_settings.cache_clear()

    inventory = {item["source_name"]: item for item in report["source_inventory"]}
    assert report["statcast_savant_status"] == "cached"
    assert report["statcast_savant_last_error"]["error_code"] == "statcast_unmatched_team_rows"
    assert inventory["statcast_savant"]["status"] == "cached"
    assert inventory["statcast_savant"]["fallback_used"] is True
    assert inventory["statcast_savant"]["fallback_reason"] == "statcast_unmatched_team_rows"


def test_source_status_report_marks_empty_pitcher_statcast_as_cached_fallback(monkeypatch) -> None:
    monkeypatch.setenv("STATCAST_CACHE_MAX_STALE_HOURS", "48")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    cached_at = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)

    try:
        with Session(engine) as session:
            session.add_all(
                [
                    PitcherDailyFeature(
                        target_date=date(2026, 7, 1),
                        team_code="PIT",
                        pitcher_id="1999",
                        pitcher_name="Home Starter",
                        captured_at=cached_at,
                        source=features.MLB_STATS_SOURCE,
                        source_status="available",
                        sample_size=30,
                        confidence=Decimal("0.8500"),
                        completeness=Decimal("0.8000"),
                        stale=False,
                        features={
                            "season_contact_quality": {
                                "source": features.STATCAST_SOURCE,
                                "captured_at": cached_at.isoformat(),
                                "batted_ball_events_count": 30,
                                "average_release_speed": 95.4,
                            }
                        },
                        raw_payload={"bounded_before": "2026-07-01"},
                    ),
                    MlbFeatureSnapshot(
                        mlb_game_id=None,
                        target_date=date(2026, 7, 1),
                        source=features.FEATURE_SYNC_AUDIT_SOURCE,
                        captured_at=datetime(2026, 7, 1, 21, 0, tzinfo=UTC),
                        data_quality=None,
                        source_statuses={"sync": "completed"},
                        features={
                            "sync_status": {
                                "attempted_at": datetime(2026, 7, 1, 21, 0, tzinfo=UTC).isoformat(),
                                "validation_status": "completed",
                                "statcast_source_status": "statcast_pitcher_empty_result",
                                "errors": [],
                                "warnings": [
                                    "Statcast/Savant pitcher contact returned no rows for probable starters in the completed date range."
                                ],
                            }
                        },
                    ),
                ]
            )
            session.commit()
            report = features.source_status_report(session)
    finally:
        get_settings.cache_clear()

    inventory = {item["source_name"]: item for item in report["source_inventory"]}
    assert report["statcast_savant_status"] == "cached"
    assert report["statcast_savant_last_error"]["error_code"] == "statcast_pitcher_empty_result"
    assert inventory["statcast_savant"]["status"] == "cached"
    assert inventory["statcast_savant"]["fallback_used"] is True
    assert inventory["statcast_savant"]["fallback_reason"] == "statcast_pitcher_empty_result"


def test_source_status_report_uses_statcast_contact_timestamp_for_cache_age(monkeypatch) -> None:
    fixed_now = datetime(2026, 7, 2, 21, 0, tzinfo=UTC)
    cached_at = fixed_now - timedelta(hours=3)
    monkeypatch.setenv("STATCAST_CACHE_MAX_STALE_HOURS", "1")
    monkeypatch.setattr(features, "utc_now", lambda: fixed_now)
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            session.add_all(
                [
                    TeamDailyFeature(
                        target_date=date(2026, 7, 2),
                        team_code="PIT",
                        captured_at=fixed_now,
                        source=features.MLB_STATS_SOURCE,
                        source_status="available",
                        confidence=Decimal("0.8500"),
                        completeness=Decimal("0.8000"),
                        stale=False,
                        features={
                            "contact_quality": {
                                "source": features.STATCAST_SOURCE,
                                "captured_at": cached_at.isoformat(),
                                "batted_ball_events_count": 30,
                                "hard_hit_pct": 0.42,
                            },
                            "contact_quality_status": "available",
                        },
                        raw_payload={"bounded_before": "2026-07-02"},
                    ),
                    MlbFeatureSnapshot(
                        mlb_game_id=None,
                        target_date=date(2026, 7, 2),
                        source=features.FEATURE_SYNC_AUDIT_SOURCE,
                        captured_at=fixed_now,
                        data_quality=None,
                        source_statuses={"sync": "completed"},
                        features={
                            "sync_status": {
                                "attempted_at": fixed_now.isoformat(),
                                "validation_status": "completed",
                                "statcast_source_status": "statcast_empty_result",
                                "errors": [],
                                "warnings": [
                                    "Statcast/Savant team contact returned no rows for the completed date range."
                                ],
                            }
                        },
                    ),
                ]
            )
            session.commit()
            report = features.source_status_report(session)
    finally:
        get_settings.cache_clear()

    inventory = {item["source_name"]: item for item in report["source_inventory"]}
    assert report["statcast_savant_last_successful_sync"] == cached_at.isoformat()
    assert report["statcast_savant_status"] == "stale"
    assert inventory["statcast_savant"]["status"] == "stale"
    assert inventory["statcast_savant"]["fallback_used"] is True


def test_source_status_report_uses_row_timestamp_for_legacy_statcast_cache(monkeypatch) -> None:
    fixed_now = datetime(2026, 7, 2, 21, 0, tzinfo=UTC)
    legacy_cache_at = fixed_now - timedelta(hours=3)
    monkeypatch.setenv("STATCAST_CACHE_MAX_STALE_HOURS", "1")
    monkeypatch.setattr(features, "utc_now", lambda: fixed_now)
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            session.add_all(
                [
                    TeamDailyFeature(
                        target_date=date(2026, 7, 2),
                        team_code="PIT",
                        captured_at=legacy_cache_at,
                        source=features.MLB_STATS_SOURCE,
                        source_status="available",
                        confidence=Decimal("0.8500"),
                        completeness=Decimal("0.8000"),
                        stale=False,
                        features={
                            "contact_quality": {
                                "source": features.STATCAST_SOURCE,
                                "batted_ball_events_count": 30,
                                "hard_hit_pct": 0.42,
                            },
                            "contact_quality_status": "available",
                        },
                        raw_payload={"bounded_before": "2026-07-02"},
                    ),
                    MlbFeatureSnapshot(
                        mlb_game_id=None,
                        target_date=date(2026, 7, 2),
                        source=features.FEATURE_SYNC_AUDIT_SOURCE,
                        captured_at=fixed_now,
                        data_quality=None,
                        source_statuses={"sync": "completed"},
                        features={
                            "sync_status": {
                                "attempted_at": fixed_now.isoformat(),
                                "validation_status": "completed",
                                "statcast_source_status": "statcast_empty_result",
                                "errors": [],
                                "warnings": [
                                    "Statcast/Savant team contact returned no rows for the completed date range."
                                ],
                            }
                        },
                    ),
                ]
            )
            session.commit()
            report = features.source_status_report(session)
    finally:
        get_settings.cache_clear()

    inventory = {item["source_name"]: item for item in report["source_inventory"]}
    assert report["statcast_savant_last_successful_sync"] == legacy_cache_at.isoformat()
    assert report["statcast_savant_status"] == "stale"
    assert inventory["statcast_savant"]["status"] == "stale"
    assert inventory["statcast_savant"]["fallback_used"] is True


def test_source_status_report_marks_old_statcast_cache_stale_without_error(monkeypatch) -> None:
    fixed_now = datetime(2026, 7, 2, 21, 0, tzinfo=UTC)
    stale_cache_at = fixed_now - timedelta(hours=3)
    monkeypatch.setenv("STATCAST_CACHE_MAX_STALE_HOURS", "1")
    monkeypatch.setattr(features, "utc_now", lambda: fixed_now)
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            session.add(
                TeamDailyFeature(
                    target_date=date(2026, 7, 2),
                    team_code="PIT",
                    captured_at=fixed_now,
                    source=features.MLB_STATS_SOURCE,
                    source_status="available",
                    confidence=Decimal("0.8500"),
                    completeness=Decimal("0.8000"),
                    stale=False,
                    features={
                        "contact_quality": {
                            "source": features.STATCAST_SOURCE,
                            "captured_at": stale_cache_at.isoformat(),
                            "batted_ball_events_count": 30,
                            "hard_hit_pct": 0.42,
                        },
                        "contact_quality_status": "available",
                    },
                    raw_payload={"bounded_before": "2026-07-02"},
                )
            )
            session.commit()
            report = features.source_status_report(session)
    finally:
        get_settings.cache_clear()

    inventory = {item["source_name"]: item for item in report["source_inventory"]}
    assert report["statcast_savant_last_successful_sync"] == stale_cache_at.isoformat()
    assert report["statcast_savant_status"] == "stale"
    assert inventory["statcast_savant"]["status"] == "stale"
    assert inventory["statcast_savant"]["fallback_used"] is False


def test_source_status_report_marks_pybaseball_unavailable_statcast_as_cached(monkeypatch) -> None:
    monkeypatch.setenv("STATCAST_CACHE_MAX_STALE_HOURS", "48")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    cached_at = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)

    try:
        with Session(engine) as session:
            session.add_all(
                [
                    TeamDailyFeature(
                        target_date=date(2026, 7, 1),
                        team_code="PIT",
                        captured_at=cached_at,
                        source=features.MLB_STATS_SOURCE,
                        source_status="available",
                        confidence=Decimal("0.8500"),
                        completeness=Decimal("0.8000"),
                        stale=False,
                        features={
                            "contact_quality": {
                                "source": features.STATCAST_SOURCE,
                                "captured_at": cached_at.isoformat(),
                                "batted_ball_events_count": 30,
                                "hard_hit_pct": 0.42,
                            },
                            "contact_quality_status": "available",
                        },
                        raw_payload={"bounded_before": "2026-07-01"},
                    ),
                    MlbFeatureSnapshot(
                        mlb_game_id=None,
                        target_date=date(2026, 7, 1),
                        source=features.FEATURE_SYNC_AUDIT_SOURCE,
                        captured_at=datetime(2026, 7, 1, 21, 0, tzinfo=UTC),
                        data_quality=None,
                        source_statuses={"sync": "completed"},
                        features={
                            "sync_status": {
                                "attempted_at": datetime(2026, 7, 1, 21, 0, tzinfo=UTC).isoformat(),
                                "validation_status": "completed",
                                "statcast_source_status": "unavailable_pybaseball_not_installed",
                                "errors": [],
                                "warnings": [],
                            }
                        },
                    ),
                ]
            )
            session.commit()
            report = features.source_status_report(session)
    finally:
        get_settings.cache_clear()

    inventory = {item["source_name"]: item for item in report["source_inventory"]}
    assert report["statcast_savant_status"] == "cached"
    assert report["statcast_savant_last_error"]["error_code"] == "unavailable_pybaseball_not_installed"
    assert inventory["statcast_savant"]["status"] == "cached"
    assert inventory["statcast_savant"]["fallback_used"] is True
    assert inventory["statcast_savant"]["fallback_reason"] == "unavailable_pybaseball_not_installed"


def test_source_status_report_does_not_fail_unattempted_statcast(monkeypatch) -> None:
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {
            "available": False,
            "version": None,
            "module_path": None,
            "import_error": "No module named pybaseball",
        },
    )

    try:
        with Session(engine) as session:
            session.add(
                MlbFeatureSnapshot(
                    mlb_game_id=None,
                    target_date=date(2026, 7, 2),
                    source=features.FEATURE_SYNC_AUDIT_SOURCE,
                    captured_at=datetime(2026, 7, 2, 21, 0, tzinfo=UTC),
                    data_quality=None,
                    source_statuses={"sync": "completed"},
                    features={
                        "sync_status": {
                            "attempted_at": datetime(2026, 7, 2, 21, 0, tzinfo=UTC).isoformat(),
                            "validation_status": "completed",
                            "statcast_source_status": "not_attempted",
                            "errors": [],
                            "warnings": [],
                        }
                    },
                )
            )
            session.commit()
            report = features.source_status_report(session)
    finally:
        get_settings.cache_clear()

    inventory = {item["source_name"]: item for item in report["source_inventory"]}
    assert report["statcast_savant_status"] == "not_attempted"
    assert report["statcast_savant_last_error"] is None
    assert inventory["statcast_savant"]["status"] == "not_attempted"
    assert inventory["statcast_savant"]["fallback_used"] is False
    assert inventory["statcast_savant"]["last_error"] is None


def test_source_status_report_ignores_pybaseball_player_mapping_misses(monkeypatch) -> None:
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured_at = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
    mapping_error = {
        "source": features.PYBASEBALL_SOURCE,
        "table": "pitcher_daily_features",
        "error_type": "PlayerMappingFailed",
        "message": "PLAYER_MAPPING_FAILED",
        "pitcher_id": "1999",
        "pitcher_name": "Home Starter",
        "team_code": "PIT",
    }
    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {"available": True, "version": "2.2.7", "module_path": "mocked", "import_error": None},
    )

    try:
        with Session(engine) as session:
            session.add_all(
                [
                    TeamDailyFeature(
                        target_date=date(2026, 7, 1),
                        team_code="PIT",
                        captured_at=captured_at,
                        source=features.PYBASEBALL_SOURCE,
                        source_status="available",
                        confidence=Decimal("0.8500"),
                        completeness=Decimal("0.7500"),
                        stale=False,
                        features={"wrc_plus": 112},
                        raw_payload={"source_function": "batting_stats"},
                    ),
                    PitcherDailyFeature(
                        target_date=date(2026, 7, 1),
                        team_code="PIT",
                        pitcher_id="1999",
                        pitcher_name="Home Starter",
                        captured_at=datetime(2026, 7, 1, 21, 0, tzinfo=UTC),
                        source=features.PYBASEBALL_SOURCE,
                        source_status="partial",
                        sample_size=None,
                        confidence=Decimal("0.3500"),
                        completeness=Decimal("0.2500"),
                        stale=False,
                        features={"season": {"era": None}},
                        raw_payload={"error": mapping_error},
                    ),
                    MlbFeatureSnapshot(
                        mlb_game_id=None,
                        target_date=date(2026, 7, 1),
                        source=features.FEATURE_SYNC_AUDIT_SOURCE,
                        captured_at=datetime(2026, 7, 1, 21, 1, tzinfo=UTC),
                        data_quality=None,
                        source_statuses={"sync": "degraded_with_errors"},
                        features={
                            "sync_status": {
                                "attempted_at": datetime(2026, 7, 1, 21, 1, tzinfo=UTC).isoformat(),
                                "validation_status": "degraded_with_errors",
                                "errors": [mapping_error],
                                "warnings": [],
                            }
                        },
                    ),
                ]
            )
            session.commit()
            report = features.source_status_report(session)
    finally:
        get_settings.cache_clear()

    inventory = {item["source_name"]: item for item in report["source_inventory"]}
    assert report["pybaseball_last_error"] is None
    assert inventory["pybaseball_fangraphs"]["status"] == "available"
    assert inventory["pybaseball_fangraphs"]["fallback_used"] is False
    assert inventory["pybaseball_fangraphs"]["last_error"] is None


def test_source_status_report_marks_old_pybaseball_cache_stale_without_error(monkeypatch) -> None:
    fixed_now = datetime(2026, 7, 2, 21, 0, tzinfo=UTC)
    stale_cache_at = fixed_now - timedelta(hours=3)
    monkeypatch.setenv("ADVANCED_PUBLIC_STATS_MAX_STALE_HOURS", "1")
    monkeypatch.setattr(features, "utc_now", lambda: fixed_now)
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {"available": True, "version": "2.2.7", "module_path": "mocked", "import_error": None},
    )

    try:
        with Session(engine) as session:
            session.add(
                TeamDailyFeature(
                    target_date=date(2026, 7, 2),
                    team_code="PIT",
                    captured_at=stale_cache_at,
                    source=features.PYBASEBALL_SOURCE,
                    source_status="available",
                    confidence=Decimal("0.8500"),
                    completeness=Decimal("0.7500"),
                    stale=False,
                    features={"wrc_plus": 112},
                    raw_payload={"source_function": "batting_stats"},
                )
            )
            session.commit()
            report = features.source_status_report(session)
    finally:
        get_settings.cache_clear()

    inventory = {item["source_name"]: item for item in report["source_inventory"]}
    assert report["pybaseball_last_successful_sync"] == stale_cache_at.isoformat()
    assert report["advanced_public_stats_status"] == "available"
    assert inventory["pybaseball_fangraphs"]["status"] == "stale"
    assert inventory["pybaseball_fangraphs"]["fallback_used"] is False
    assert inventory["pybaseball_fangraphs"]["last_error"] is None


def test_source_status_report_marks_pybaseball_import_failure_as_cached_fallback(monkeypatch) -> None:
    fixed_now = datetime(2026, 7, 2, 21, 0, tzinfo=UTC)
    cached_at = fixed_now - timedelta(hours=3)
    monkeypatch.setenv("ADVANCED_PUBLIC_STATS_MAX_STALE_HOURS", "72")
    monkeypatch.setattr(features, "utc_now", lambda: fixed_now)
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        features.pybaseball_client,
        "import_status",
        lambda: {
            "available": False,
            "version": None,
            "module_path": None,
            "import_error": {"error_type": "ImportError", "message": "No module named pybaseball"},
        },
    )

    try:
        with Session(engine) as session:
            session.add(
                TeamDailyFeature(
                    target_date=date(2026, 7, 2),
                    team_code="PIT",
                    captured_at=cached_at,
                    source=features.PYBASEBALL_SOURCE,
                    source_status="available",
                    confidence=Decimal("0.8500"),
                    completeness=Decimal("0.7500"),
                    stale=False,
                    features={"wrc_plus": 112},
                    raw_payload={"source_function": "batting_stats"},
                )
            )
            session.commit()
            report = features.source_status_report(session)
    finally:
        get_settings.cache_clear()

    inventory = {item["source_name"]: item for item in report["source_inventory"]}
    pybaseball_inventory = inventory["pybaseball_fangraphs"]
    assert report["pybaseball_available"] is False
    assert report["pybaseball_import_error"]["message"] == "No module named pybaseball"
    assert report["pybaseball_last_successful_sync"] == cached_at.isoformat()
    assert pybaseball_inventory["status"] == "cached"
    assert pybaseball_inventory["fallback_used"] is True
    assert pybaseball_inventory["fallback_source"] == "last_good_pybaseball_cache"
    assert pybaseball_inventory["fallback_reason"] == "pybaseball_import_failed"
    assert pybaseball_inventory["last_error"] == {
        "error_code": "pybaseball_import_failed",
        "message": "No module named pybaseball",
    }


def test_mlb_primary_team_daily_preserves_cached_statcast_when_current_fetch_empty() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    day = date(2026, 7, 1)
    captured_at = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
    cached_contact = {
        "source": features.STATCAST_SOURCE,
        "batted_ball_events_count": 35,
        "average_exit_velocity": 90.1,
        "hard_hit_pct": 0.44,
    }
    hitting_log = {
        "stats": [
            {
                "splits": [
                    {
                        "date": "2026-06-30",
                        "stat": {
                            "runs": 5,
                            "hits": 8,
                            "homeRuns": 1,
                            "baseOnBalls": 3,
                            "strikeOuts": 7,
                            "atBats": 34,
                            "plateAppearances": 38,
                            "totalBases": 14,
                        },
                    }
                ]
            }
        ]
    }
    pitching_log = {
        "stats": [
            {
                "splits": [
                    {
                        "date": "2026-06-30",
                        "stat": {
                            "inningsPitched": "9.0",
                            "runs": 3,
                            "earnedRuns": 3,
                            "hits": 7,
                            "homeRuns": 1,
                            "baseOnBalls": 2,
                            "strikeOuts": 8,
                            "battersFaced": 36,
                            "numberOfPitches": 142,
                        },
                    }
                ]
            }
        ]
    }

    with Session(engine) as session:
        legacy_cache_at = datetime(2026, 7, 1, 19, 0, tzinfo=UTC)
        game = MlbGame(
            external_game_id="statcast-cache-preserve",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
            raw_payload={
                "teams": {"home": {"team": {"id": 134, "name": "Pittsburgh Pirates"}}},
                "venue": {"id": 31, "name": "PNC Park"},
            },
        )
        session.add(game)
        session.flush()
        session.add(
            TeamDailyFeature(
                target_date=day,
                team_code="PIT",
                captured_at=legacy_cache_at,
                source=features.MLB_STATS_SOURCE,
                source_status="available",
                confidence=Decimal("0.8500"),
                completeness=Decimal("0.8000"),
                stale=False,
                features={"contact_quality": cached_contact, "contact_quality_status": "available"},
                raw_payload={"cached": True},
            )
        )
        session.flush()

        row = features._upsert_mlb_primary_team_daily(
            session,
            game,
            "home",
            day,
            captured_at,
            {
                "team_hitting_logs_by_id": {"134": hitting_log},
                "team_pitching_logs_by_id": {"134": pitching_log},
                "team_hitting_splits_by_id": {},
                "team_pitching_splits_by_id": {},
            },
            {"team_contact_by_code": {}},
        )

    assert row is not None
    assert row.features["contact_quality"] == {**cached_contact, "captured_at": legacy_cache_at.isoformat()}
    assert row.features["contact_quality_status"] == "available"


def test_static_stadium_table_covers_current_mlb_venues() -> None:
    required_venues = {
        "American Family Field",
        "Angel Stadium",
        "Busch Stadium",
        "Chase Field",
        "Citi Field",
        "Citizens Bank Park",
        "Comerica Park",
        "Coors Field",
        "Daikin Park",
        "Dodger Stadium",
        "Fenway Park",
        "George M. Steinbrenner Field",
        "Globe Life Field",
        "Great American Ball Park",
        "Kauffman Stadium",
        "loanDepot park",
        "Nationals Park",
        "Oracle Park",
        "Oriole Park at Camden Yards",
        "Petco Park",
        "PNC Park",
        "Progressive Field",
        "Rate Field",
        "Rogers Centre",
        "Sutter Health Park",
        "Target Field",
        "T-Mobile Park",
        "Truist Park",
        "Wrigley Field",
        "Yankee Stadium",
    }

    assert required_venues <= set(features.STADIUM_PROFILES)
    assert features.TEAM_HOME_VENUES["ATH"] == "Sutter Health Park"


def test_lineup_sync_preserves_confirmed_lineup_when_payload_lacks_boxscore() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured_at = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)

    players = {
        f"ID{person_id}": {
            "person": {"id": person_id, "fullName": f"Starter {slot}"},
            "battingOrder": str(slot * 100),
            "batSide": {"code": "R"},
            "position": {"abbreviation": "CF"},
        }
        for slot, person_id in enumerate(range(101, 110), start=1)
    }

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="lineup-preserve-confirmed",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="Final",
            raw_payload={
                "venue": {"id": 31, "name": "PNC Park"},
                "liveData": {"boxscore": {"teams": {"home": {"batters": list(range(101, 110)), "players": players}}}},
            },
        )
        session.add(game)
        session.flush()

        features._upsert_lineup(session, game, "home", date(2026, 7, 1), captured_at)
        game.raw_payload = {"venue": {"id": 31, "name": "PNC Park"}}
        features._upsert_lineup(
            session,
            game,
            "home",
            date(2026, 7, 1),
            datetime(2026, 7, 1, 21, 0, tzinfo=UTC),
        )
        row = session.scalar(select(LineupSnapshot).where(LineupSnapshot.mlb_game_id == game.id))

    assert row is not None
    assert row.confirmed is True
    assert row.source_status == "available"
    assert row.raw_payload == {"starter_count": 9}
    assert len(row.features["starters"]) == 9


def test_weather_sync_preserves_cached_forecast_when_network_disabled(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "false")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured_at = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="weather-preserve-cached",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
            raw_payload={"venue": {"id": 31, "name": "PNC Park"}},
        )
        session.add(game)
        session.flush()
        session.add(
            WeatherSnapshot(
                mlb_game_id=game.id,
                target_date=date(2026, 7, 1),
                venue_name="PNC Park",
                captured_at=captured_at,
                forecast_time=game.scheduled_start,
                source=features.OPEN_METEO_SOURCE,
                source_status="available",
                confidence=Decimal("0.70"),
                completeness=Decimal("0.70"),
                stale=False,
                features={"temperature_2m": 74, "wind_speed_10m": 8},
                raw_payload={"cached": True},
            )
        )
        session.flush()

        features._upsert_weather(
            session,
            game,
            date(2026, 7, 1),
            datetime(2026, 7, 1, 21, 0, tzinfo=UTC),
        )
        row = session.scalar(select(WeatherSnapshot).where(WeatherSnapshot.mlb_game_id == game.id))

    assert row is not None
    assert row.source_status == "available"
    assert row.features == {"temperature_2m": 74, "wind_speed_10m": 8}
    assert row.raw_payload == {"cached": True}


def test_open_meteo_base_url_tolerates_forecast_suffix(monkeypatch) -> None:
    monkeypatch.setenv("OPEN_METEO_BASE_URL", "https://api.open-meteo.com/v1/forecast")
    get_settings.cache_clear()
    captured: dict[str, object] = {}

    def fake_get_json(url: str, **_kwargs):
        captured["url"] = url
        captured["params"] = _kwargs.get("params")
        return {"hourly": {"time": []}}

    monkeypatch.setattr(features, "get_json", fake_get_json)
    try:
        features._fetch_open_meteo(
            {"latitude": 40.4469, "longitude": -80.0057},
            datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        )
    finally:
        get_settings.cache_clear()

    assert captured["url"] == "https://api.open-meteo.com/v1/forecast"
    assert captured["params"]["temperature_unit"] == "fahrenheit"
    assert captured["params"]["wind_speed_unit"] == "mph"


def test_open_meteo_request_uses_local_forecast_date(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_get_json(_url: str, **kwargs):
        captured["params"] = kwargs.get("params")
        return {"hourly": {"time": []}}

    monkeypatch.setattr(features, "get_json", fake_get_json)
    features._fetch_open_meteo(
        {"latitude": 40.4469, "longitude": -80.0057},
        datetime(2026, 7, 2, 0, 10, tzinfo=UTC),
    )

    assert captured["params"]["timezone"] == "America/New_York"
    assert captured["params"]["start_date"] == "2026-07-01"
    assert captured["params"]["end_date"] == "2026-07-01"


def test_schedule_upsert_preserves_cached_live_payload() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    live_data = {"boxscore": {"teams": {"home": {"batters": [101, 102]}}}}

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="12345",
            home_team="Cached Home",
            away_team="Cached Away",
            home_abbreviation="CH",
            away_abbreviation="CA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
            raw_payload={
                "gamePk": 12345,
                "venue": {"id": 1, "name": "Cached Park"},
                "liveData": live_data,
            },
        )
        session.add(game)
        session.flush()

        refreshed = features._upsert_game_from_schedule_payload(
            session,
            {
                "gamePk": 12345,
                "gameDate": "2026-07-01T23:00:00Z",
                "venue": {"id": 31, "name": "PNC Park"},
                "status": {"detailedState": "Scheduled"},
                "teams": {
                    "home": {
                        "score": 0,
                        "team": {"name": "Pittsburgh Pirates", "abbreviation": "PIT"},
                    },
                    "away": {
                        "score": 0,
                        "team": {"name": "Seattle Mariners", "abbreviation": "SEA"},
                    },
                },
            },
        )

        assert refreshed is not None
        assert refreshed.raw_payload["liveData"] == live_data
        assert refreshed.raw_payload["venue"]["name"] == "PNC Park"
        assert refreshed.home_abbreviation == "PIT"
        assert refreshed.away_abbreviation == "SEA"


def _starter_game_log_payload() -> dict[str, object]:
    return {
        "stats": [
            {
                "splits": [
                    {
                        "date": f"2026-06-{25 - index:02d}",
                        "stat": {
                            "inningsPitched": "6.0",
                            "gamesStarted": 1,
                            "strikeOuts": 6,
                            "baseOnBalls": 2,
                            "hits": 5,
                            "homeRuns": 1,
                            "runs": 2,
                            "earnedRuns": 2,
                            "numberOfPitches": 90,
                            "battersFaced": 25,
                        },
                    }
                    for index in range(5)
                ]
            }
        ]
    }


def _starter_schedule_game(*, home_probable: bool = True, away_probable: bool = True) -> dict[str, object]:
    home: dict[str, object] = {"team": {"id": 134, "name": "Pittsburgh Pirates", "abbreviation": "PIT"}}
    away: dict[str, object] = {"team": {"id": 136, "name": "Seattle Mariners", "abbreviation": "SEA"}}
    if home_probable:
        home["probablePitcher"] = {"id": 1999, "fullName": "Home Starter"}
    if away_probable:
        away["probablePitcher"] = {"id": 2999, "fullName": "Away Starter"}
    return {
        "gamePk": 123456,
        "gameDate": "2026-07-01T23:00:00Z",
        "status": {"detailedState": "Scheduled"},
        "venue": {"id": 31, "name": "PNC Park"},
        "teams": {"home": home, "away": away},
    }


def _boxscore_lineup_team(start_id: int, pitcher_id: int, *, starter_count: int = 9) -> dict[str, object]:
    batters = list(range(start_id, start_id + starter_count))
    players: dict[str, object] = {}
    for slot, person_id in enumerate(batters, start=1):
        players[f"ID{person_id}"] = {
            "person": {"id": person_id, "fullName": f"Lineup Starter {person_id}"},
            "battingOrder": str(slot * 100),
            "batSide": {"code": "R" if slot % 2 else "L"},
            "position": {"abbreviation": "C" if slot == 9 else "CF"},
        }
    players[f"ID{pitcher_id}"] = {
        "person": {"id": pitcher_id, "fullName": f"Pitcher {pitcher_id}"},
        "pitchHand": {"code": "R"},
    }
    return {"batters": batters, "pitchers": [pitcher_id], "players": players}


def _seed_existing_starter_feature_snapshot(
    session: Session,
    *,
    raw_payload: dict[str, object] | None = None,
) -> int:
    game = MlbGame(
        external_game_id="123456",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="scheduled",
        raw_payload=raw_payload or {},
    )
    session.add(game)
    session.flush()
    session.add(
        MlbFeatureSnapshot(
            mlb_game_id=game.id,
            target_date=date(2026, 7, 1),
            source=features.FEATURE_VERSION,
            captured_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            data_quality=Decimal("0.0000"),
            source_statuses={},
            features={},
        )
    )
    session.commit()
    return int(game.id)


def test_mlb_schedule_sync_requests_probable_pitchers_and_preserves_live_payload(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured_params: dict[str, object] = {}

    def fake_get_json(_url, params):
        captured_params.update(params)
        return {"dates": [{"games": [_starter_schedule_game()]}]}

    monkeypatch.setattr(mlb, "get_json", fake_get_json)

    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="123456",
                home_team="Cached Home",
                away_team="Cached Away",
                home_abbreviation="CH",
                away_abbreviation="CA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
                raw_payload={"liveData": {"boxscore": {"cached": True}}},
            )
        )
        session.commit()

        assert mlb.sync_schedule(session, date(2026, 7, 1)) == 1
        game = session.scalar(select(MlbGame).where(MlbGame.external_game_id == "123456"))

    assert "probablePitcher(note)" in str(captured_params["hydrate"])
    assert game is not None
    assert game.raw_payload["liveData"]["boxscore"] == {"cached": True}
    assert game.raw_payload["teams"]["home"]["probablePitcher"]["id"] == 1999


def test_mlb_schedule_sync_preserves_cached_starter_until_refresh_rechecks(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    def fake_get_json(_url, params):
        del params
        return {"dates": [{"games": [_starter_schedule_game(home_probable=False, away_probable=True)]}]}

    monkeypatch.setattr(mlb, "get_json", fake_get_json)

    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="123456",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
                raw_payload={
                    "teams": {
                        "home": {"probablePitcher": {"id": 1999, "fullName": "Scratched Home Starter"}},
                        "away": {"probablePitcher": {"id": 2999, "fullName": "Away Starter"}},
                    },
                    "gameData": {
                        "probablePitchers": {
                            "home": {"id": 1999, "fullName": "Scratched Home Starter"},
                            "away": {"id": 2999, "fullName": "Away Starter"},
                        }
                    },
                    "homerun_starter_hydration": {
                        "home": {"status": "available", "pitcher_id": "1999"},
                        "away": {"status": "available", "pitcher_id": "2999"},
                    },
                    "liveData": {
                        "boxscore": {
                            "teams": {
                                "home": {"pitchers": [1999], "players": {}},
                                "away": {"pitchers": [2999], "players": {}},
                            }
                        }
                    },
                },
            )
        )
        session.commit()

        assert mlb.sync_schedule(session, date(2026, 7, 1)) == 1
        game = session.scalar(select(MlbGame).where(MlbGame.external_game_id == "123456"))

    assert game is not None
    assert "probablePitcher" not in game.raw_payload["teams"]["home"]
    assert game.raw_payload["teams"]["away"]["probablePitcher"]["id"] == 2999
    assert game.raw_payload["homerun_starter_hydration"]["home"]["pitcher_id"] == "1999"
    assert game.raw_payload["homerun_starter_hydration"]["away"]["pitcher_id"] == "2999"
    assert game.raw_payload["gameData"]["probablePitchers"]["home"]["id"] == 1999
    assert game.raw_payload["gameData"]["probablePitchers"]["away"]["id"] == 2999
    assert game.raw_payload["liveData"]["boxscore"]["teams"]["home"]["pitchers"] == [1999]
    assert game.raw_payload["liveData"]["boxscore"]["teams"]["away"]["pitchers"] == [2999]
    assert features.probable_pitcher_from_payload(game.raw_payload or {}, "home")["id"] == "1999"


def test_sync_mlb_starters_hydrates_schedule_probables_and_pitcher_features(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    game_log_calls: list[str] = []

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game()]}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {}

        def get_pitcher_game_log_stats(self, person_id: str, _season: int):
            game_log_calls.append(str(person_id))
            return _starter_game_log_payload()

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    try:
        with Session(engine) as session:
            _seed_existing_starter_feature_snapshot(session)
            result = features.sync_mlb_starters(session, date(2026, 7, 1))
            game = session.scalar(select(MlbGame).where(MlbGame.external_game_id == "123456"))
            home_pitcher = session.scalar(
                select(PitcherDailyFeature)
                .where(PitcherDailyFeature.pitcher_id == "1999")
                .where(PitcherDailyFeature.source == features.MLB_STATS_SOURCE)
            )
            snapshot = session.scalar(select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_VERSION))
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] == "ok"
    assert result["games_with_both_starters"] == 1
    assert result["starter_identity_available_count"] == 2
    assert game_log_calls == ["1999", "2999"]
    assert game is not None
    assert game.raw_payload["homerun_starter_hydration"]["home"]["pitcher_id"] == "1999"
    assert home_pitcher is not None
    assert home_pitcher.source_status == "available"
    assert snapshot is not None
    assert snapshot.source_statuses["starter_identity"] == {"home": "available", "away": "available"}
    assert snapshot.source_statuses["starter_recent"]["home"] == "available"
    assert snapshot.source_statuses["starter_workload"]["home"] == "available"


def test_sync_mlb_starters_skips_snapshot_without_existing_mature_feature(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game()]}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {}

        def get_pitcher_game_log_stats(self, *_args, **_kwargs):
            return _starter_game_log_payload()

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    try:
        with Session(engine) as session:
            result = features.sync_mlb_starters(session, date(2026, 7, 1))
            snapshot = session.scalar(select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_VERSION))
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] == "ok"
    assert result["feature_snapshots_upserted"] == 0
    assert result["feature_snapshots_skipped_missing_existing"] == 1
    assert snapshot is None


def test_sync_mlb_pregame_context_updates_official_lineups_and_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game()]}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {
                "liveData": {
                    "boxscore": {
                        "teams": {
                            "home": _boxscore_lineup_team(1000, 1999),
                            "away": _boxscore_lineup_team(2000, 2999),
                        }
                    }
                }
            }

        def get_pitcher_game_log_stats(self, *_args, **_kwargs):
            return _starter_game_log_payload()

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    try:
        with Session(engine) as session:
            _seed_existing_starter_feature_snapshot(session)
            result = features.sync_mlb_pregame_context(session, date(2026, 7, 1))
            home_lineup = session.scalar(select(LineupSnapshot).where(LineupSnapshot.team_code == "PIT"))
            snapshot = session.scalar(select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_VERSION))
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] == "ok"
    assert result["feature_sync_mode"] == features.PREGAME_CONTEXT_SYNC_MODE
    assert result["heavy_feature_sync_skipped"] is True
    assert result["confirmed_lineup_count"] == 2
    assert result["pybaseball_functions_attempted"] == []
    assert result["statcast_rows_seen"] == 0
    assert home_lineup is not None
    assert home_lineup.source_status == "available"
    assert home_lineup.features["missing_reason"] is None
    assert snapshot is not None
    assert snapshot.source_statuses["lineup"] == {"home": "available", "away": "available"}
    assert snapshot.source_statuses["starter_identity"] == {"home": "available", "away": "available"}


def test_sync_mlb_pregame_context_fetches_boxscore_lineups_when_starters_known(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    boxscore_calls: list[str] = []

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game()]}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {
                "gameData": {
                    "datetime": {"dateTime": "2026-07-01T23:00:00Z"},
                    "probablePitchers": {
                        "home": {"id": 1999, "fullName": "Home Feed Starter", "pitchHand": {"code": "R"}},
                        "away": {"id": 2999, "fullName": "Away Feed Starter", "pitchHand": {"code": "L"}},
                    },
                }
            }

        def get_game_boxscore(self, game_pk: str):
            boxscore_calls.append(str(game_pk))
            return {
                "teams": {
                    "home": _boxscore_lineup_team(1000, 1999),
                    "away": _boxscore_lineup_team(2000, 2999),
                }
            }

        def get_pitcher_game_log_stats(self, *_args, **_kwargs):
            return _starter_game_log_payload()

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    try:
        with Session(engine) as session:
            _seed_existing_starter_feature_snapshot(session)
            result = features.sync_mlb_pregame_context(session, date(2026, 7, 1))
            home_lineup = session.scalar(select(LineupSnapshot).where(LineupSnapshot.team_code == "PIT"))
            snapshot = session.scalar(select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_VERSION))
    finally:
        get_settings.cache_clear()

    assert boxscore_calls == ["123456"]
    assert result["validation_status"] == "ok"
    assert result["confirmed_lineup_count"] == 2
    assert home_lineup is not None
    assert home_lineup.source_status == "available"
    assert home_lineup.features["missing_reason"] is None
    assert snapshot is not None
    assert snapshot.source_statuses["lineup"] == {"home": "available", "away": "available"}


def test_sync_mlb_pregame_context_reconciles_boxscore_starter_changes(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    game_log_calls: list[str] = []

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game()]}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {
                "gameData": {
                    "datetime": {"dateTime": "2026-07-01T23:00:00Z"},
                    "probablePitchers": {
                        "home": {"id": 1999, "fullName": "Scratched Home Starter", "pitchHand": {"code": "R"}},
                        "away": {"id": 2999, "fullName": "Scratched Away Starter", "pitchHand": {"code": "L"}},
                    },
                }
            }

        def get_game_boxscore(self, *_args, **_kwargs):
            return {
                "teams": {
                    "home": _boxscore_lineup_team(1000, 3999),
                    "away": _boxscore_lineup_team(2000, 4999),
                }
            }

        def get_pitcher_game_log_stats(self, person_id: str, _season: int):
            game_log_calls.append(str(person_id))
            return _starter_game_log_payload()

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    try:
        with Session(engine) as session:
            _seed_existing_starter_feature_snapshot(session)
            result = features.sync_mlb_pregame_context(session, date(2026, 7, 1))
            game = session.scalar(select(MlbGame).where(MlbGame.external_game_id == "123456"))
            reconciled_pitcher = session.scalar(
                select(PitcherDailyFeature)
                .where(PitcherDailyFeature.pitcher_id == "3999")
                .where(PitcherDailyFeature.source == features.MLB_STATS_SOURCE)
            )
            snapshot = session.scalar(select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_VERSION))
    finally:
        get_settings.cache_clear()

    assert game_log_calls == ["1999", "2999", "3999", "4999"]
    assert result["boxscore_starter_reconcile_count"] == 1
    assert game is not None
    assert features.probable_pitcher_from_payload(game.raw_payload or {}, "home")["id"] == "3999"
    assert reconciled_pitcher is not None
    assert reconciled_pitcher.source_status == "available"
    assert snapshot is not None
    assert snapshot.features["starter_identity"]["home"]["id"] == "3999"
    assert snapshot.source_statuses["starter_recent"]["home"] == "available"
    assert snapshot.source_statuses["starter_workload"]["home"] == "available"


def test_sync_mlb_pregame_context_does_not_overwrite_feature_sync_audit(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game()]}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {
                "liveData": {
                    "boxscore": {
                        "teams": {
                            "home": _boxscore_lineup_team(1000, 1999),
                            "away": _boxscore_lineup_team(2000, 2999),
                        }
                    }
                }
            }

        def get_pitcher_game_log_stats(self, *_args, **_kwargs):
            return _starter_game_log_payload()

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    try:
        with Session(engine) as session:
            _seed_existing_starter_feature_snapshot(session)
            session.add(
                MlbFeatureSnapshot(
                    mlb_game_id=None,
                    target_date=date(2026, 7, 1),
                    source=features.FEATURE_SYNC_AUDIT_SOURCE,
                    captured_at=datetime(2026, 7, 1, 13, 0, tzinfo=UTC),
                    data_quality=Decimal("0.0000"),
                    source_statuses={},
                    features={
                        "sync_status": {
                            "action": "mlb_feature_sync",
                            "validation_status": "degraded_with_errors",
                            "statcast_source_status": "statcast_empty_result",
                            "errors": [{"source": features.STATCAST_SOURCE, "message": "empty response"}],
                        }
                    },
                )
            )
            session.commit()

            result = features.sync_mlb_pregame_context(session, date(2026, 7, 1))
            audit = session.scalar(
                select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_SYNC_AUDIT_SOURCE)
            )
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] == "ok"
    assert result["feature_sync_audit_skipped"] is True
    assert result["audit_skipped_reason"] == "pregame_context_refresh_does_not_replace_full_feature_sync_audit"
    assert audit is not None
    assert audit.features["sync_status"]["action"] == "mlb_feature_sync"
    assert audit.features["sync_status"]["validation_status"] == "degraded_with_errors"
    assert audit.features["sync_status"]["statcast_source_status"] == "statcast_empty_result"


def test_sync_mlb_pregame_context_reports_partial_and_unposted_lineups(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game()]}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {
                "liveData": {
                    "boxscore": {
                        "teams": {
                            "home": _boxscore_lineup_team(1000, 1999, starter_count=4),
                            "away": {"batters": [], "pitchers": [2999], "players": {}},
                        }
                    }
                }
            }

        def get_pitcher_game_log_stats(self, *_args, **_kwargs):
            return _starter_game_log_payload()

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    try:
        with Session(engine) as session:
            _seed_existing_starter_feature_snapshot(session)
            result = features.sync_mlb_pregame_context(session, date(2026, 7, 1))
            home_lineup = session.scalar(select(LineupSnapshot).where(LineupSnapshot.team_code == "PIT"))
            away_lineup = session.scalar(select(LineupSnapshot).where(LineupSnapshot.team_code == "SEA"))
            snapshot = session.scalar(select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_VERSION))
    finally:
        get_settings.cache_clear()

    assert result["partial_lineup_count"] == 1
    assert result["missing_lineup_count"] == 1
    assert result["lineup_missing_reasons"] == {
        features.PARTIAL_LINEUP_POSTED: 1,
        features.LINEUP_NOT_POSTED_YET: 1,
    }
    assert home_lineup is not None
    assert home_lineup.source_status == "partial"
    assert home_lineup.features["missing_reason"] == features.PARTIAL_LINEUP_POSTED
    assert away_lineup is not None
    assert away_lineup.source_status == "missing"
    assert away_lineup.features["missing_reason"] == features.LINEUP_NOT_POSTED_YET
    assert snapshot is not None
    assert snapshot.source_statuses["lineup"] == {"home": "partial", "away": "missing"}


def test_sync_mlb_starters_preserves_cached_statcast_pitcher_fields(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    cached_contact = {
        "source": features.STATCAST_SOURCE,
        "batted_ball_events_count": 31,
        "average_release_speed": 94.7,
        "barrel_pct": 0.08,
    }

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game()]}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {}

        def get_pitcher_game_log_stats(self, *_args, **_kwargs):
            return _starter_game_log_payload()

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    try:
        with Session(engine) as session:
            _seed_existing_starter_feature_snapshot(session)
            legacy_cache_at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
            session.add(
                PitcherDailyFeature(
                    target_date=date(2026, 7, 1),
                    team_code="PIT",
                    pitcher_id="1999",
                    pitcher_name="Home Starter",
                    captured_at=legacy_cache_at,
                    source=features.MLB_STATS_SOURCE,
                    source_status="available",
                    sample_size=20,
                    confidence=Decimal("0.8500"),
                    completeness=Decimal("0.8000"),
                    stale=False,
                    features={
                        "season_contact_quality": cached_contact,
                        "recent_contact_quality": cached_contact,
                        "contact_quality_status": "available",
                    },
                    raw_payload={"statcast": cached_contact},
                )
            )
            session.commit()

            result = features.sync_mlb_starters(session, date(2026, 7, 1))
            pitcher = session.scalar(
                select(PitcherDailyFeature)
                .where(PitcherDailyFeature.target_date == date(2026, 7, 1))
                .where(PitcherDailyFeature.team_code == "PIT")
                .where(PitcherDailyFeature.pitcher_id == "1999")
                .where(PitcherDailyFeature.source == features.MLB_STATS_SOURCE)
            )
    finally:
        get_settings.cache_clear()

    assert result["validation_status"] == "ok"
    assert pitcher is not None
    expected_contact = {**cached_contact, "captured_at": legacy_cache_at.isoformat()}
    assert pitcher.features["season_contact_quality"] == expected_contact
    assert pitcher.features["recent_contact_quality"] == expected_contact
    assert pitcher.features["contact_quality_status"] == "available"
    assert pitcher.features["recent"]["velocity_trend"] == 94.7
    assert pitcher.raw_payload["statcast"] == expected_contact


def test_sync_mlb_starters_falls_back_to_game_feed_and_marks_missing_stats_partial(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game(home_probable=False, away_probable=False)]}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {
                "gameData": {
                    "datetime": {"dateTime": "2026-07-01T23:00:00Z"},
                    "probablePitchers": {
                        "home": {"id": 1999, "fullName": "Home Feed Starter", "pitchHand": {"code": "R"}},
                        "away": {"id": 2999, "fullName": "Away Feed Starter", "pitchHand": {"code": "L"}},
                    },
                }
            }

        def get_pitcher_game_log_stats(self, *_args, **_kwargs):
            return {"stats": [{"splits": []}]}

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    try:
        with Session(engine) as session:
            _seed_existing_starter_feature_snapshot(session)
            result = features.sync_mlb_starters(session, date(2026, 7, 1))
            report = features.starter_status_report(session, date(2026, 7, 1))
            snapshot = session.scalar(select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_VERSION))
    finally:
        get_settings.cache_clear()

    assert result["games_with_both_starters"] == 1
    assert report["summary"]["games_with_both_starters"] == 1
    assert report["games"][0]["home_probable_pitcher_name"] == "Home Feed Starter"
    assert snapshot is not None
    assert snapshot.source_statuses["starter_identity"] == {"home": "available", "away": "available"}
    assert snapshot.source_statuses["starter_season"] == {"home": "partial", "away": "partial"}


def test_sync_mlb_starters_prefers_feed_boxscore_over_probable_pitchers(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    game_log_calls: list[str] = []

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game(home_probable=False, away_probable=False)]}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {
                "gameData": {
                    "datetime": {"dateTime": "2026-07-01T23:00:00Z"},
                    "probablePitchers": {
                        "home": {"id": 1111, "fullName": "Scratched Home Probable", "pitchHand": {"code": "R"}},
                        "away": {"id": 2222, "fullName": "Scratched Away Probable", "pitchHand": {"code": "L"}},
                    },
                },
                "liveData": {
                    "boxscore": {
                        "teams": {
                            "home": {
                                "pitchers": [1999],
                                "players": {
                                    "ID1999": {
                                        "person": {"id": 1999, "fullName": "Actual Home Starter"},
                                        "pitchHand": {"code": "R"},
                                    }
                                },
                            },
                            "away": {
                                "pitchers": [2999],
                                "players": {
                                    "ID2999": {
                                        "person": {"id": 2999, "fullName": "Actual Away Starter"},
                                        "pitchHand": {"code": "L"},
                                    }
                                },
                            },
                        }
                    }
                },
            }

        def get_pitcher_game_log_stats(self, person_id: str, _season: int):
            game_log_calls.append(str(person_id))
            return _starter_game_log_payload()

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    try:
        with Session(engine) as session:
            _seed_existing_starter_feature_snapshot(session)
            result = features.sync_mlb_starters(session, date(2026, 7, 1))
            game = session.scalar(select(MlbGame).where(MlbGame.external_game_id == "123456"))
            snapshot = session.scalar(select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_VERSION))
    finally:
        get_settings.cache_clear()

    assert result["games_with_both_starters"] == 1
    assert game_log_calls == ["1999", "2999"]
    assert game is not None
    assert game.raw_payload["homerun_starter_hydration"]["home"]["pitcher_id"] == "1999"
    assert game.raw_payload["homerun_starter_hydration"]["away"]["pitcher_id"] == "2999"
    assert features.probable_pitcher_from_payload(game.raw_payload or {}, "home")["id"] == "1999"
    assert features.probable_pitcher_from_payload(game.raw_payload or {}, "away")["id"] == "2999"
    assert snapshot is not None
    assert snapshot.source_statuses["starter_identity"] == {"home": "available", "away": "available"}


def test_sync_mlb_starters_checks_feed_even_when_schedule_has_probables(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    feed_calls: list[str] = []
    game_log_calls: list[str] = []

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game()]}]}

        def get_game_feed(self, game_pk: str):
            feed_calls.append(str(game_pk))
            return {
                "liveData": {
                    "boxscore": {
                        "teams": {
                            "home": {
                                "pitchers": [3999],
                                "players": {
                                    "ID3999": {
                                        "person": {"id": 3999, "fullName": "Late Scratch Home Starter"},
                                        "pitchHand": {"code": "R"},
                                    }
                                },
                            },
                            "away": {
                                "pitchers": [4999],
                                "players": {
                                    "ID4999": {
                                        "person": {"id": 4999, "fullName": "Late Scratch Away Starter"},
                                        "pitchHand": {"code": "L"},
                                    }
                                },
                            },
                        }
                    }
                }
            }

        def get_game_boxscore(self, *_args, **_kwargs):
            pytest.fail("feed boxscore should satisfy starter refresh")

        def get_pitcher_game_log_stats(self, person_id: str, _season: int):
            game_log_calls.append(str(person_id))
            return _starter_game_log_payload()

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    try:
        with Session(engine) as session:
            _seed_existing_starter_feature_snapshot(session)
            result = features.sync_mlb_starters(session, date(2026, 7, 1))
            game = session.scalar(select(MlbGame).where(MlbGame.external_game_id == "123456"))
            snapshot = session.scalar(select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_VERSION))
    finally:
        get_settings.cache_clear()

    assert feed_calls == ["123456"]
    assert result["starter_identity_available_count"] == 2
    assert game_log_calls == ["3999", "4999"]
    assert game is not None
    assert game.raw_payload["homerun_starter_hydration"]["home"]["pitcher_id"] == "3999"
    assert game.raw_payload["homerun_starter_hydration"]["away"]["pitcher_id"] == "4999"
    assert features.probable_pitcher_from_payload(game.raw_payload or {}, "home")["id"] == "3999"
    assert features.probable_pitcher_from_payload(game.raw_payload or {}, "away")["id"] == "4999"
    assert snapshot is not None
    assert snapshot.source_statuses["starter_identity"] == {"home": "available", "away": "available"}


def test_sync_mlb_starters_clears_stale_schedule_after_feed_probable_update(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    game_log_calls: list[str] = []

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game()]}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {
                "gameData": {
                    "datetime": {"dateTime": "2026-07-01T23:00:00Z"},
                    "probablePitchers": {
                        "home": {"id": 3999, "fullName": "Updated Feed Home Starter", "pitchHand": {"code": "R"}},
                        "away": {"id": 4999, "fullName": "Updated Feed Away Starter", "pitchHand": {"code": "L"}},
                    },
                }
            }

        def get_game_boxscore(self, *_args, **_kwargs):
            pytest.fail("feed gameData should satisfy starter refresh")

        def get_pitcher_game_log_stats(self, person_id: str, _season: int):
            game_log_calls.append(str(person_id))
            return _starter_game_log_payload()

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    try:
        with Session(engine) as session:
            _seed_existing_starter_feature_snapshot(session)
            result = features.sync_mlb_starters(session, date(2026, 7, 1))
            game = session.scalar(select(MlbGame).where(MlbGame.external_game_id == "123456"))
            snapshot = session.scalar(select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_VERSION))
    finally:
        get_settings.cache_clear()

    assert result["starter_identity_available_count"] == 2
    assert game_log_calls == ["3999", "4999"]
    assert game is not None
    assert "probablePitcher" not in game.raw_payload["teams"]["home"]
    assert "probablePitcher" not in game.raw_payload["teams"]["away"]
    assert features.probable_pitcher_from_payload(game.raw_payload or {}, "home")["id"] == "3999"
    assert features.probable_pitcher_from_payload(game.raw_payload or {}, "away")["id"] == "4999"
    assert snapshot is not None
    assert snapshot.source_statuses["starter_identity"] == {"home": "available", "away": "available"}


def test_sync_mlb_starters_prefers_feed_metadata_when_starter_ids_match(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game()]}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {
                "gameData": {
                    "datetime": {"dateTime": "2026-07-01T23:00:00Z"},
                    "probablePitchers": {
                        "home": {"id": 1999, "fullName": "Home Feed Starter", "pitchHand": {"code": "R"}},
                        "away": {"id": 2999, "fullName": "Away Feed Starter", "pitchHand": {"code": "L"}},
                    },
                }
            }

        def get_game_boxscore(self, *_args, **_kwargs):
            pytest.fail("feed gameData should satisfy starter refresh")

        def get_pitcher_game_log_stats(self, *_args, **_kwargs):
            return _starter_game_log_payload()

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    try:
        with Session(engine) as session:
            _seed_existing_starter_feature_snapshot(session)
            result = features.sync_mlb_starters(session, date(2026, 7, 1))
            game = session.scalar(select(MlbGame).where(MlbGame.external_game_id == "123456"))
    finally:
        get_settings.cache_clear()

    assert result["starter_identity_available_count"] == 2
    assert game is not None
    assert "probablePitcher" not in game.raw_payload["teams"]["home"]
    assert "probablePitcher" not in game.raw_payload["teams"]["away"]
    home_pitcher = features.probable_pitcher_from_payload(game.raw_payload or {}, "home")
    away_pitcher = features.probable_pitcher_from_payload(game.raw_payload or {}, "away")
    assert home_pitcher is not None
    assert home_pitcher["id"] == "1999"
    assert home_pitcher["handedness"] == "R"
    assert home_pitcher["source_path"] == "game.gameData.probablePitchers.home"
    assert away_pitcher is not None
    assert away_pitcher["id"] == "2999"
    assert away_pitcher["handedness"] == "L"
    assert away_pitcher["source_path"] == "game.gameData.probablePitchers.away"


def test_sync_mlb_starters_does_not_fake_missing_starters(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game(home_probable=False, away_probable=False)]}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {"gameData": {"probablePitchers": {}}}

        def get_game_boxscore(self, *_args, **_kwargs):
            return {"teams": {"home": {}, "away": {}}}

        def get_pitcher_game_log_stats(self, *_args, **_kwargs):
            pytest.fail("pitcher logs should not be fetched without starter IDs")

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    try:
        with Session(engine) as session:
            result = features.sync_mlb_starters(session, date(2026, 7, 1))
            report = features.starter_status_report(session, date(2026, 7, 1))
            game = session.scalar(select(MlbGame).where(MlbGame.external_game_id == "123456"))
    finally:
        get_settings.cache_clear()

    assert result["starter_identity_available_count"] == 0
    assert result["games_missing_both_starters"] == 1
    assert report["summary"]["starter_refresh_status"] == "missing"
    assert report["games"][0]["home_probable_pitcher_id"] is None
    assert game is not None
    assert game.raw_payload["homerun_starter_hydration"]["home"]["missing_reason"] == "starter_not_available_from_mlb_stats_api"


def test_sync_mlb_starters_rechecks_when_schedule_drops_cached_probables(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    feed_calls: list[str] = []
    boxscore_calls: list[str] = []

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game(home_probable=False, away_probable=False)]}]}

        def get_game_feed(self, game_pk: str):
            feed_calls.append(str(game_pk))
            return {"gameData": {"probablePitchers": {}}}

        def get_game_boxscore(self, game_pk: str):
            boxscore_calls.append(str(game_pk))
            return {"teams": {"home": {}, "away": {}}}

        def get_pitcher_game_log_stats(self, *_args, **_kwargs):
            pytest.fail("stale cached starter IDs should not drive pitcher log fetches")

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    stale_raw_payload = {
        "gameData": {
            "probablePitchers": {
                "home": {"id": 1999, "fullName": "Scratched Home Starter"},
                "away": {"id": 2999, "fullName": "Scratched Away Starter"},
            }
        },
        "homerun_starter_hydration": {
            "home": {
                "status": "available",
                "pitcher_id": "1999",
                "pitcher_name": "Scratched Home Starter",
                "source_path": "homerun_starter_hydration.home",
            },
            "away": {
                "status": "available",
                "pitcher_id": "2999",
                "pitcher_name": "Scratched Away Starter",
                "source_path": "homerun_starter_hydration.away",
            },
        },
    }

    try:
        with Session(engine) as session:
            _seed_existing_starter_feature_snapshot(session, raw_payload=stale_raw_payload)
            result = features.sync_mlb_starters(session, date(2026, 7, 1))
            game = session.scalar(select(MlbGame).where(MlbGame.external_game_id == "123456"))
            snapshot = session.scalar(select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_VERSION))
    finally:
        get_settings.cache_clear()

    assert feed_calls == ["123456"]
    assert boxscore_calls == ["123456"]
    assert result["starter_identity_available_count"] == 0
    assert result["games_missing_both_starters"] == 1
    assert game is not None
    assert features.probable_pitcher_from_payload(game.raw_payload or {}, "home") is None
    assert features.probable_pitcher_from_payload(game.raw_payload or {}, "away") is None
    assert game.raw_payload["homerun_starter_hydration"]["home"]["status"] == "missing"
    assert game.raw_payload["homerun_starter_hydration"]["away"]["status"] == "missing"
    assert snapshot is not None
    assert snapshot.source_statuses["starter_identity"] == {"home": "missing", "away": "missing"}


def test_sync_mlb_starters_preserves_cached_starters_when_fallback_refresh_fails(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    game_log_calls: list[str] = []

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game(home_probable=False, away_probable=False)]}]}

        def get_game_feed(self, *_args, **_kwargs):
            raise TimeoutError("feed unavailable")

        def get_game_boxscore(self, *_args, **_kwargs):
            raise TimeoutError("boxscore unavailable")

        def get_pitcher_game_log_stats(self, person_id: str, _season: int):
            game_log_calls.append(str(person_id))
            return _starter_game_log_payload()

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    cached_raw_payload = {
        "gameData": {
            "probablePitchers": {
                "home": {"id": 1999, "fullName": "Cached Home Starter"},
                "away": {"id": 2999, "fullName": "Cached Away Starter"},
            }
        },
        "homerun_starter_hydration": {
            "home": {
                "status": "available",
                "pitcher_id": "1999",
                "pitcher_name": "Cached Home Starter",
                "source_path": "homerun_starter_hydration.home",
            },
            "away": {
                "status": "available",
                "pitcher_id": "2999",
                "pitcher_name": "Cached Away Starter",
                "source_path": "homerun_starter_hydration.away",
            },
        },
    }

    try:
        with Session(engine) as session:
            _seed_existing_starter_feature_snapshot(session, raw_payload=cached_raw_payload)
            result = features.sync_mlb_starters(session, date(2026, 7, 1))
            game = session.scalar(select(MlbGame).where(MlbGame.external_game_id == "123456"))
            snapshot = session.scalar(select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_VERSION))
    finally:
        get_settings.cache_clear()

    assert result["starter_identity_available_count"] == 2
    assert result["games_missing_both_starters"] == 0
    assert game_log_calls == ["1999", "2999"]
    assert game is not None
    assert features.probable_pitcher_from_payload(game.raw_payload or {}, "home")["id"] == "1999"
    assert features.probable_pitcher_from_payload(game.raw_payload or {}, "away")["id"] == "2999"
    assert game.raw_payload["homerun_starter_hydration"]["home"]["status"] == "available"
    assert game.raw_payload["homerun_starter_hydration"]["away"]["status"] == "available"
    assert len(game.raw_payload["homerun_starter_hydration"]["errors"]) == 2
    assert snapshot is not None
    assert snapshot.source_statuses["starter_identity"] == {"home": "available", "away": "available"}


def test_sync_mlb_starters_clears_changed_stale_boxscore_starters(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SYNC_ENABLE_NETWORK_SOURCES", "true")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    game_log_calls: list[str] = []

    class FakeMLBStatsClient:
        def get_schedule(self, *_args, **_kwargs):
            return {"dates": [{"games": [_starter_schedule_game()]}]}

        def get_game_feed(self, *_args, **_kwargs):
            return {}

        def get_game_boxscore(self, *_args, **_kwargs):
            pytest.fail("complete schedule probables should not require boxscore fallback")

        def get_pitcher_game_log_stats(self, person_id: str, _season: int):
            game_log_calls.append(str(person_id))
            return _starter_game_log_payload()

    monkeypatch.setattr(features, "MLBStatsClient", FakeMLBStatsClient)

    stale_raw_payload = {
        "liveData": {
            "boxscore": {
                "teams": {
                    "home": {
                        "pitchers": [1111],
                        "players": {"ID1111": {"person": {"id": 1111, "fullName": "Old Home Starter"}}},
                    },
                    "away": {
                        "pitchers": [2222],
                        "players": {"ID2222": {"person": {"id": 2222, "fullName": "Old Away Starter"}}},
                    },
                }
            }
        },
        "gameData": {
            "probablePitchers": {
                "home": {"id": 1111, "fullName": "Old Home Starter"},
                "away": {"id": 2222, "fullName": "Old Away Starter"},
            }
        },
    }

    try:
        with Session(engine) as session:
            _seed_existing_starter_feature_snapshot(session, raw_payload=stale_raw_payload)
            result = features.sync_mlb_starters(session, date(2026, 7, 1))
            game = session.scalar(select(MlbGame).where(MlbGame.external_game_id == "123456"))
            snapshot = session.scalar(select(MlbFeatureSnapshot).where(MlbFeatureSnapshot.source == features.FEATURE_VERSION))
    finally:
        get_settings.cache_clear()

    assert result["starter_identity_available_count"] == 2
    assert game_log_calls == ["1999", "2999"]
    assert game is not None
    assert features.probable_pitcher_from_payload(game.raw_payload or {}, "home")["id"] == "1999"
    assert features.probable_pitcher_from_payload(game.raw_payload or {}, "away")["id"] == "2999"
    assert game.raw_payload["homerun_starter_hydration"]["home"]["identity_changed"] is True
    assert game.raw_payload["homerun_starter_hydration"]["away"]["identity_changed"] is True
    assert snapshot is not None
    assert snapshot.source_statuses["starter_identity"] == {"home": "available", "away": "available"}


def test_open_meteo_parse_uses_nearest_forecast_hour() -> None:
    parsed = features._parse_open_meteo(
        {
            "hourly": {
                "time": ["2026-07-01T18:00"],
                "temperature_2m": [91],
                "wind_speed_10m": [12],
            }
        },
        datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
    )

    assert parsed is not None
    assert parsed["forecast_time"] == "2026-07-01T18:00"
    assert parsed["temperature"] == 91
    assert parsed["wind_speed"] == 12


def test_feature_sync_keeps_unknown_park_weather_missing(monkeypatch) -> None:
    _stub_public_feature_network(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="feature-sync-unknown-park",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
            raw_payload={"venue": {"id": 999, "name": "Unlisted Test Park"}},
        )
        session.add(game)
        session.commit()

        features.sync_mlb_features(session, date(2026, 7, 1))
        snapshot = session.scalar(select(MlbFeatureSnapshot))

    assert snapshot is not None
    assert snapshot.source_statuses["park_weather"] == "missing"
    assert snapshot.features["park_weather"]["source_status"] == "missing"


def test_travel_feature_uses_current_venue_for_away_team(monkeypatch) -> None:
    _stub_public_feature_network(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        previous = MlbGame(
            external_game_id="travel-previous-sea-home",
            home_team="Seattle Mariners",
            away_team="Boston Red Sox",
            home_abbreviation="SEA",
            away_abbreviation="BOS",
            scheduled_start=datetime(2026, 6, 30, 23, 0, tzinfo=UTC),
            status="Final",
            home_score=4,
            away_score=2,
            raw_payload={"venue": {"id": 680, "name": "T-Mobile Park"}},
        )
        current = MlbGame(
            external_game_id="travel-current-sea-at-pit",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
            raw_payload={"venue": {"id": 31, "name": "PNC Park"}},
        )
        session.add_all([previous, current])
        session.commit()
        current_id = current.id

        features.sync_mlb_features(session, date(2026, 7, 1))
        away_travel = session.scalar(
            select(TravelScheduleFeature)
            .where(TravelScheduleFeature.mlb_game_id == current_id)
            .where(TravelScheduleFeature.team_code == "SEA")
        )

    assert away_travel is not None
    assert away_travel.features["travel_distance_miles"] > 1000


def test_travel_feature_uses_home_venue_proxy_for_unknown_road_venue(monkeypatch) -> None:
    _stub_public_feature_network(monkeypatch)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        previous = MlbGame(
            external_game_id="travel-previous-sea-home-unknown-road",
            home_team="Seattle Mariners",
            away_team="Boston Red Sox",
            home_abbreviation="SEA",
            away_abbreviation="BOS",
            scheduled_start=datetime(2026, 6, 30, 23, 0, tzinfo=UTC),
            status="Final",
            home_score=4,
            away_score=2,
            raw_payload={"venue": {"id": 680, "name": "T-Mobile Park"}},
        )
        current = MlbGame(
            external_game_id="travel-current-sea-at-unknown",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
            raw_payload={"venue": {"id": 999, "name": "Unsupported Test Venue"}},
        )
        session.add_all([previous, current])
        session.commit()
        current_id = current.id

        features.sync_mlb_features(session, date(2026, 7, 1))
        away_travel = session.scalar(
            select(TravelScheduleFeature)
            .where(TravelScheduleFeature.mlb_game_id == current_id)
            .where(TravelScheduleFeature.team_code == "SEA")
        )

    assert away_travel is not None
    assert away_travel.features["travel_distance_miles"] > 1000


def test_lineup_parser_extracts_starters_and_excludes_substitutes() -> None:
    payload = {
        "liveData": {
            "boxscore": {
                "teams": {
                    "home": {
                        "batters": [10, 11, 12],
                        "players": {
                            "ID10": {
                                "person": {"id": 10, "fullName": "Starter One"},
                                "battingOrder": "100",
                                "batSide": {"code": "R"},
                                "position": {"abbreviation": "CF"},
                            },
                            "ID11": {
                                "person": {"id": 11, "fullName": "Sub One"},
                                "battingOrder": "150",
                                "batSide": {"code": "L"},
                                "position": {"abbreviation": "LF"},
                            },
                            "ID12": {
                                "person": {"id": 12, "fullName": "Starter Nine"},
                                "battingOrder": "900",
                                "batSide": {"code": "L"},
                                "position": {"abbreviation": "C"},
                            },
                        },
                    }
                }
            }
        }
    }

    starters = features.parse_starting_lineup_from_game_payload(payload, "home")

    assert [starter["name"] for starter in starters] == ["Starter One", "Starter Nine"]
    assert [starter["batting_order"] for starter in starters] == [100, 900]


def test_probable_pitcher_adapter_uses_boxscore_fallback() -> None:
    payload = {
        "teams": {
            "away": {
                "probablePitcher": {
                    "id": 88,
                    "fullName": "Stale Probable",
                }
            }
        },
        "liveData": {
            "boxscore": {
                "teams": {
                    "away": {
                        "pitchers": [99],
                        "players": {
                            "ID99": {
                                "person": {"id": 99, "fullName": "Fallback Starter"},
                                "pitchHand": {"code": "L"},
                            }
                        },
                    }
                }
            }
        }
    }

    pitcher = features.probable_pitcher_from_payload(payload, "away")

    assert pitcher == {
        "id": "99",
        "name": "Fallback Starter",
        "pitcher_name": "Fallback Starter",
        "handedness": "L",
        "note": None,
        "source_path": "game.liveData.boxscore.teams.away.pitchers[0]",
    }


def test_probable_pitcher_adapter_uses_fresh_probable_after_missing_refresh_cache() -> None:
    payload = {
        "homerun_starter_hydration": {
            "home": {"status": "missing", "missing_reason": "starter_not_available_from_mlb_stats_api"},
            "away": {"status": "missing", "missing_reason": "starter_not_available_from_mlb_stats_api"},
        },
        "teams": {
            "home": {
                "probablePitcher": {
                    "id": 1999,
                    "fullName": "Fresh Home Starter",
                    "pitchHand": {"code": "R"},
                }
            }
        },
        "gameData": {
            "probablePitchers": {
                "away": {
                    "id": 2999,
                    "fullName": "Fresh Away Starter",
                    "pitchHand": {"code": "L"},
                }
            }
        },
    }

    home_pitcher = features.probable_pitcher_from_payload(payload, "home")
    away_pitcher = features.probable_pitcher_from_payload(payload, "away")

    assert home_pitcher is not None
    assert home_pitcher["id"] == "1999"
    assert home_pitcher["source_path"] == "schedule.teams.home.probablePitcher"
    assert away_pitcher is not None
    assert away_pitcher["id"] == "2999"
    assert away_pitcher["source_path"] == "game.gameData.probablePitchers.away"


def test_probable_pitcher_adapter_prefers_schedule_over_stale_game_data() -> None:
    payload = {
        "gameData": {
            "probablePitchers": {
                "home": {
                    "id": 1999,
                    "fullName": "Old Feed Starter",
                    "pitchHand": {"code": "L"},
                }
            }
        },
        "teams": {
            "home": {
                "probablePitcher": {
                    "id": 2999,
                    "fullName": "Fresh Schedule Starter",
                    "pitchHand": {"code": "R"},
                }
            }
        },
    }

    pitcher = features.probable_pitcher_from_payload(payload, "home")

    assert pitcher is not None
    assert pitcher["id"] == "2999"
    assert pitcher["source_path"] == "schedule.teams.home.probablePitcher"


def test_data_quality_caps_missing_critical_modules() -> None:
    now = datetime(2026, 7, 1, 21, 45, tzinfo=UTC)
    game = MlbGame(
        external_game_id="quality-cap-1",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="scheduled",
    )
    market = KalshiMarket(
        kalshi_market_id="KX-QUALITY-CAP",
        ticker="KXMLBGAME-QUALITY-CAP-PIT",
        title="Will Pittsburgh win?",
        status="open",
        market_family="full_game_winner",
    )
    mapping = MarketMapping(
        mlb_game_id=1,
        kalshi_market_id=1,
        mapping_status="confirmed",
        confidence=Decimal("0.9500"),
        market_family="full_game_winner",
        selection_code="PIT",
    )

    snapshot = features.build_feature_snapshot(game, market, mapping, now=now)

    assert snapshot["data_quality"] <= 0.60
    assert "CAP_BOTH_STARTERS_MISSING" in snapshot["data_quality_reason"]
    assert "CAP_OFFENSE_SEASON_AND_RECENT_MISSING" in snapshot["data_quality_reason"]


def test_defense_catcher_diagnostics_do_not_change_model_quality() -> None:
    base_features = {
        module_name: {"source_status": "available", "completeness": 1.0}
        for module_name in features.QUALITY_WEIGHTS[features.FULL_GAME_WINNER]
    }
    missing_defense_features = {
        **base_features,
        "defense_catcher": {"source_status": "missing", "completeness": 0.0},
    }
    available_defense_features = {
        **base_features,
        "defense_catcher": {"source_status": "available", "completeness": 1.0},
    }

    missing_quality, missing_summary = features._quality_score(
        missing_defense_features,
        features.FULL_GAME_WINNER,
        180,
    )
    available_quality, available_summary = features._quality_score(
        available_defense_features,
        features.FULL_GAME_WINNER,
        180,
    )

    assert "defense_catcher" in features.QUALITY_WEIGHTS[features.FULL_GAME_WINNER]
    assert missing_quality == available_quality
    assert missing_summary["model_quality_score"] == available_summary["model_quality_score"]
    assert missing_summary["diagnostic_score"] < available_summary["diagnostic_score"]
    assert missing_summary["module_scores"]["defense_catcher"] == 0.0
    assert available_summary["module_scores"]["defense_catcher"] == 1.0
    assert "defense_catcher" in missing_summary["model_quality_excluded_modules"]
    assert missing_summary["source_statuses"]["defense_catcher"] == "missing"
    assert available_summary["source_statuses"]["defense_catcher"] == "available"


def test_first_five_expected_runs_use_starter_context_not_static_share() -> None:
    game = MlbGame(
        external_game_id="f5-share-1",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="scheduled",
        raw_payload={
            "teams": {
                "home": {"probablePitcher": {"id": 1, "fullName": "Home Starter"}},
                "away": {"probablePitcher": {"id": 2, "fullName": "Away Starter"}},
            }
        },
    )
    market = KalshiMarket(
        kalshi_market_id="KX-F5-SHARE",
        ticker="KXMLBF5-26JUL011900SEAPIT-PIT",
        title="Will Pittsburgh lead after five?",
        status="open",
        market_family="first_five_winner",
        selection_code="PIT",
    )
    mapping = MarketMapping(
        mlb_game_id=1,
        kalshi_market_id=1,
        mapping_status="confirmed",
        confidence=Decimal("0.9500"),
        market_family="first_five_winner",
        selection_code="PIT",
        settlement_rule_status="paper_supported",
    )
    snapshot = features.build_feature_snapshot(
        game,
        market,
        mapping,
        now=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
    )

    expectations = modeling.expected_runs(snapshot)

    assert expectations.home_first_five_runs_mean != (
        expectations.home_full_game_runs_mean * Decimal("0.49")
    ).quantize(Decimal("0.0001"))


def test_model_parameter_version_creation() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        version = modeling.get_or_create_active_parameter_version(session)
        again = modeling.get_or_create_active_parameter_version(session)

    assert version.version_tag == modeling.BASELINE_PARAMETER_VERSION_TAG
    assert again.id == version.id
    assert version.is_active is True
    assert "league_average_full_game_runs" in version.parameters
    assert "kalshi_price" not in " ".join(version.parameters.keys()).lower()


def test_active_parameter_version_preserves_promoted_challenger() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    promoted_at = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)

    with Session(engine) as session:
        baseline = ModelParameterVersion(
            version_tag=modeling.BASELINE_PARAMETER_VERSION_TAG,
            model_family=modeling.MODEL_FAMILY,
            role="inactive",
            status="active",
            is_active=False,
            trained_at=promoted_at,
            promoted_at=promoted_at,
            parameters=modeling.DEFAULT_MODEL_PARAMETERS,
        )
        challenger = ModelParameterVersion(
            version_tag="mature_mlb_run_distribution_v2_challenger_promoted",
            model_family=modeling.MODEL_FAMILY,
            role="champion",
            status="active",
            is_active=True,
            trained_at=promoted_at,
            promoted_at=promoted_at + timedelta(minutes=1),
            parameters={
                **modeling.DEFAULT_MODEL_PARAMETERS,
                "trained_from_samples": True,
                "market_family_probability_offsets": {"__global__": 0.01},
            },
        )
        session.add_all([baseline, challenger])
        session.commit()

        active = modeling.get_or_create_active_parameter_version(session)
        baseline_after = session.scalar(
            select(ModelParameterVersion).where(
                ModelParameterVersion.version_tag == modeling.BASELINE_PARAMETER_VERSION_TAG
            )
        )

    assert active.version_tag == challenger.version_tag
    assert active.is_active is True
    assert baseline_after is not None
    assert baseline_after.is_active is False


def test_calibration_applies_global_and_family_offsets() -> None:
    calibrated, status = modeling._calibrate_probability(
        Decimal("0.500000"),
        Decimal("0.8000"),
        market_type="full_game_winner",
        parameters={
            **modeling.DEFAULT_MODEL_PARAMETERS,
            "trained_from_samples": True,
            "market_family_probability_offsets": {
                "__global__": 0.02,
                "full_game_winner": -0.01,
            },
        },
    )

    assert status == "trained_parameterized"
    assert calibrated == Decimal("0.510000")


def test_family_offsets_are_residual_to_global_offset(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_MIN_FAMILY_SAMPLES_FOR_FAMILY_CALIBRATION", "2")
    get_settings.cache_clear()
    rows = [
        ModelCandidate(
            probability_calibrated=Decimal("0.950000"),
            outcome="win",
            market_family="full_game_winner",
        )
        for _index in range(3)
    ]

    offsets = modeling._fit_probability_offsets(rows)

    assert offsets["__global__"] == Decimal("0.050000")
    assert offsets["full_game_winner"] == Decimal("0.000000")


def test_challenger_offsets_preserve_active_parameter_offsets() -> None:
    combined = modeling._combine_probability_offsets(
        {
            "market_family_probability_offsets": {
                "__global__": 0.02,
                "full_game_winner": -0.01,
            }
        },
        {
            "__global__": Decimal("0.030000"),
            "full_game_winner": Decimal("0.020000"),
        },
    )

    assert combined["__global__"] == Decimal("0.050000")
    assert combined["full_game_winner"] == Decimal("0.010000")


def test_governance_trains_challenger_when_sample_threshold_met(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_MIN_SAMPLES_TRAIN", "3")
    monkeypatch.setenv("MODEL_MIN_SAMPLES_CALIBRATE", "3")
    monkeypatch.setenv("MODEL_MIN_SAMPLES_PROMOTE", "99")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target_date = date(2026, 7, 2)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        game = MlbGame(
            external_game_id="governance-train-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 2, 23, 0, tzinfo=UTC),
            status="Final",
            home_score=5,
            away_score=3,
        )
        session.add(game)
        session.flush()
        for index, outcome in enumerate(["win", "win", "loss", "win", "loss"], start=1):
            session.add(
                ModelCandidate(
                    paper_trading_epoch_id=epoch_id,
                    mlb_game_id=game.id,
                    evaluated_at=datetime(2026, 7, 2, 16, index, tzinfo=UTC),
                    features={},
                    probability=Decimal("0.550000"),
                    probability_calibrated=Decimal("0.550000"),
                    fee_estimate=Decimal("0.010000"),
                    target_date=target_date,
                    price_status="fresh_executable",
                    time_to_start_minutes=400,
                    decision="candidate_only",
                    outcome=outcome,
                    resolved_at=datetime(2026, 7, 3, 4, index, tzinfo=UTC),
                    model_version_tag=modeling.MATURE_MODEL_TAG,
                    feature_version=features.FEATURE_VERSION,
                    training_eligible=True,
                    market_family="full_game_winner",
                )
            )
        session.commit()

        result = run_model_governance(session, now=datetime(2026, 7, 2, 12, 0, tzinfo=UTC))
        challenger = session.scalar(
            select(ModelParameterVersion).where(ModelParameterVersion.role == "challenger")
        )

    assert result["status"] == "trained_not_promoted"
    assert result["challenger_parameter_version"] is not None
    assert challenger is not None
    assert challenger.parameters["trained_from_samples"] is True


def test_clean_governance_challenger_does_not_inherit_pre_clean_offsets(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_MIN_SAMPLES_TRAIN", "3")
    monkeypatch.setenv("MODEL_MIN_SAMPLES_CALIBRATE", "3")
    monkeypatch.setenv("MODEL_MIN_SAMPLES_PROMOTE", "99")
    monkeypatch.setenv("MODEL_GOVERNANCE_CLEAN_START_AT", "2026-07-02T00:00:00-04:00")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target_date = date(2026, 7, 2)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        session.add(
            ModelParameterVersion(
                version_tag="pre_clean_active_offsets",
                model_family=modeling.MODEL_FAMILY,
                role="champion",
                status="promoted",
                is_active=True,
                promoted_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
                parameters={
                    **modeling.DEFAULT_MODEL_PARAMETERS,
                    "market_family_probability_offsets": {"__global__": 0.02},
                    "trained_from_samples": True,
                },
                metrics={"paper_trading_epoch_id": epoch_id},
            )
        )
        game = MlbGame(
            external_game_id="governance-clean-offset-reset",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 2, 23, 0, tzinfo=UTC),
            status="Final",
        )
        session.add(game)
        session.flush()
        for index, outcome in enumerate(["win", "win", "loss", "win", "loss"], start=1):
            _add_governance_candidate(
                session,
                epoch_id=epoch_id,
                game_id=game.id,
                target_date=target_date,
                evaluated_at=datetime(2026, 7, 2, 16, index, tzinfo=UTC),
                resolved_at=datetime(2026, 7, 3, 4, index, tzinfo=UTC),
                outcome=outcome,
            )
        session.commit()

        result = run_model_governance(session, now=datetime(2026, 7, 3, 12, 0, tzinfo=UTC))
        challenger = session.scalar(
            select(ModelParameterVersion).where(
                ModelParameterVersion.version_tag == result["challenger_parameter_version"]
            )
        )

    assert result["status"] == "trained_not_promoted"
    assert challenger is not None
    combined_global = Decimal(str(challenger.metrics["combined_offsets"]["__global__"]))
    challenger_global = Decimal(str(challenger.parameters["market_family_probability_offsets"]["__global__"]))
    assert combined_global == Decimal("0.075")
    assert challenger_global == Decimal("0.075")
    assert challenger.metrics["parameter_seed_policy"] == "reset_to_default_parameters_pre_clean_active_ignored"
    assert challenger.metrics["parameter_seed_clean_policy_matched"] is False


@pytest.mark.parametrize(
    ("market_family", "selection_code", "line_value", "over_under_side"),
    [
        ("full_game_winner", "PIT", None, None),
        ("full_game_spread", "PIT", Decimal("-1.5000"), None),
        ("full_game_total", None, Decimal("8.5000"), "over"),
        ("first_five_winner", "TIE", None, None),
        ("first_five_spread", "SEA", Decimal("0.5000"), None),
        ("first_five_total", None, Decimal("4.5000"), "under"),
    ],
)
def test_pr3c_model_outputs_bounded_probabilities_for_all_supported_families(
    market_family: str,
    selection_code: str | None,
    line_value: Decimal | None,
    over_under_side: str | None,
) -> None:
    game = MlbGame(
        external_game_id=f"model-family-{market_family}",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="scheduled",
    )
    market = KalshiMarket(
        kalshi_market_id=f"KX-{market_family}",
        ticker=f"KXMLBTEST-{market_family}",
        title="Synthetic PR3c market",
        status="open",
        implied_yes_ask=Decimal("0.4000"),
        market_family=market_family,
        market_type=market_family,
        line_value=line_value,
        selection_code=selection_code,
        over_under_side=over_under_side,
        settlement_rule_status="paper_supported",
    )
    mapping = MarketMapping(
        mlb_game_id=1,
        kalshi_market_id=1,
        mapping_status="confirmed",
        confidence=Decimal("0.9500"),
        market_family=market_family,
        market_type=market_family,
        line_value=line_value,
        selection_code=selection_code,
        over_under_side=over_under_side,
        settlement_rule_status="paper_supported",
    )
    snapshot = features.build_feature_snapshot(
        game,
        market,
        mapping,
        now=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
    )
    score = modeling.score_mature_candidate(snapshot, market_type=market_family, settlement_status="paper_supported")

    assert Decimal("0") < score.probability < Decimal("1")
    assert score.rationale["uses_market_price"] is False
    assert score.rationale["run_expectations"]["home_full_game_runs_mean"] != 0


def test_full_game_winner_probabilities_allocate_tie_mass_to_no_tie_outcomes() -> None:
    game = MlbGame(
        external_game_id="model-no-tie-winner",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="scheduled",
    )

    def _score_for(selection_code: str) -> modeling.ModelScore:
        market = KalshiMarket(
            kalshi_market_id=f"KX-NO-TIE-{selection_code}",
            ticker=f"KXMLBGAME-NO-TIE-{selection_code}",
            title="Synthetic no-tie winner market",
            status="open",
            market_family="full_game_winner",
            market_type="full_game_winner",
            selection_code=selection_code,
            settlement_rule_status="paper_supported",
        )
        mapping = MarketMapping(
            mlb_game_id=1,
            kalshi_market_id=1,
            mapping_status="confirmed",
            confidence=Decimal("0.9500"),
            market_family="full_game_winner",
            market_type="full_game_winner",
            selection_code=selection_code,
            settlement_rule_status="paper_supported",
        )
        snapshot = features.build_feature_snapshot(
            game,
            market,
            mapping,
            now=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
        )
        return modeling.score_mature_candidate(snapshot, market_type="full_game_winner", settlement_status="paper_supported")

    home_score = _score_for("PIT")
    away_score = _score_for("SEA")

    assert home_score.probability_raw is not None
    assert away_score.probability_raw is not None
    assert home_score.probability_raw + away_score.probability_raw == Decimal("1.000000")


def test_pr3c_trade_policy_caps_slate_trades(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_SLATE", "2")
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_MARKET_FAMILY", "10")
    monkeypatch.setenv("PAPER_MIN_DATA_QUALITY", "0")
    monkeypatch.setenv("PAPER_OBSERVATION_MIN_DATA_QUALITY", "0")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.900000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine, autoflush=False) as session:
            for index in range(5):
                game = MlbGame(
                    external_game_id=f"cap-game-{index}",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC) + timedelta(minutes=index),
                    status="scheduled",
                )
                market = KalshiMarket(
                    kalshi_market_id=f"KX-CAP-{index}",
                    ticker=f"KXMLBGAME-CAP-{index}-PIT",
                    title="Cheap home winner",
                    status="open",
                    implied_yes_ask=Decimal("0.4000"),
                )
                session.add_all([game, market])
                session.flush()
                _add_candidate_mapping(
                    session,
                    game,
                    market,
                    market_type="full_game_winner",
                    selection_code="PIT",
                )
            session.commit()

            result = candidates.generate_candidates(session)
            trades = list(session.scalars(select(PaperTrade)))
            prediction_run = session.scalar(select(ModelPredictionRun))
            outputs = list(session.scalars(select(ModelPredictionOutput).order_by(ModelPredictionOutput.id.asc())))
            snapshot = session.get(BalanceSnapshot, result["snapshot_id"])
    finally:
        get_settings.cache_clear()

    assert result["paper_trades"] == 2
    assert len(trades) == 2
    assert prediction_run is not None
    assert prediction_run.trade_policy["paper_max_trades_per_slate"] == 2
    assert snapshot is not None
    open_cost_with_fees = sum(
        (trade.entry_price * trade.quantity) + (trade.total_fee_estimate or Decimal("0")) for trade in trades
    ).quantize(Decimal("0.01"))
    open_mark_value = sum((trade.current_price or trade.entry_price) * trade.quantity for trade in trades).quantize(Decimal("0.01"))
    assert open_cost_with_fees > Decimal("20.00")
    assert snapshot.cash_balance == Decimal("500.00") - open_cost_with_fees
    assert snapshot.portfolio_value == snapshot.cash_balance + open_mark_value
    assert result["cap_counts"]["no_trade_slate_cap"] == 3
    assert result["decision_counts"] == {"paper_trade": 2, "no_trade_slate_cap": 3}
    assert prediction_run.summary["decision_counts"] == {"paper_trade": 2, "no_trade_slate_cap": 3}
    assert [output.decision_reason for output in outputs].count("paper_trade") == 2
    assert [output.decision_reason for output in outputs].count("no_trade_slate_cap") == 3


def test_first_five_tie_markets_are_blocked_from_paper_trading(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_MIN_DATA_QUALITY", "0")
    monkeypatch.setenv("PAPER_OBSERVATION_MIN_DATA_QUALITY", "0")
    monkeypatch.setenv("PAPER_MIN_NET_EV", "0")
    monkeypatch.setenv("PAPER_MIN_PROB_EDGE", "0")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            game = MlbGame(
                external_game_id="tie-normal-gates",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
            market = KalshiMarket(
                kalshi_market_id="KX-TIE-NORMAL-GATES",
                ticker="KXMLBF5-26JUL011900SEAPIT-TIE",
                title="Will the first five innings end tied?",
                status="open",
                implied_yes_ask=Decimal("0.4000"),
                market_family="first_five_winner",
                market_type="first_five_winner",
                selection_code="TIE",
                inning_scope="first_five",
                settlement_rule_status="paper_supported",
            )
            session.add_all([game, market])
            session.flush()
            _add_candidate_mapping(
                session,
                game,
                market,
                mapping_status="confirmed",
                market_family="first_five_winner",
                market_type="first_five_winner",
                selection_code="TIE",
                inning_scope="first_five",
                settlement_rule_status="paper_supported",
            )
            session.commit()

            result = candidates.generate_candidates(session)
            candidate = session.scalar(select(ModelCandidate))
            trade = session.scalar(select(PaperTrade))
    finally:
        get_settings.cache_clear()

    assert result["paper_trades"] == 0
    assert candidate is not None
    assert candidate.decision == "no_trade_f5_tie_disabled"
    assert trade is None


def test_generate_candidates_blocks_prices_below_floor(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    monkeypatch.setenv("PAPER_MIN_NET_EV", "0")
    monkeypatch.setenv("PAPER_MIN_PROB_EDGE", "0")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.900000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="price-floor-game",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-PRICE-FLOOR",
            ticker="KXMLBGAME-PRICE-FLOOR-PIT",
            title="Will Pittsburgh win?",
            status="open",
            implied_yes_ask=Decimal("0.0900"),
        )
        session.add_all([game, market])
        session.flush()
        _add_candidate_mapping(session, game, market, mapping_status="confirmed", market_type="full_game_winner")
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))

    assert result["paper_trades"] == 0
    assert candidate is not None
    assert candidate.decision == "no_trade_price_below_floor"


def test_generate_candidates_requires_stricter_low_price_edge(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    monkeypatch.setenv("PAPER_MIN_NET_EV", "0")
    monkeypatch.setenv("PAPER_MIN_PROB_EDGE", "0.03")
    monkeypatch.setenv("PAPER_LOW_PRICE_MIN_PROB_EDGE", "0.05")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.190000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="low-price-edge-game",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-LOW-PRICE-EDGE",
            ticker="KXMLBGAME-LOW-PRICE-EDGE-PIT",
            title="Will Pittsburgh win?",
            status="open",
            implied_yes_ask=Decimal("0.1500"),
        )
        session.add_all([game, market])
        session.flush()
        _add_candidate_mapping(session, game, market, mapping_status="confirmed", market_type="full_game_winner")
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))

    assert result["paper_trades"] == 0
    assert candidate is not None
    assert candidate.decision == "no_trade_low_price_probability_edge_low"
    assert result["low_price_controls"]["low_price_candidates_considered"] == 1


def test_low_price_caps_apply_per_slate_and_per_sweep(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_LOW_PRICE_MAX_TRADES_PER_SLATE", "2")
    monkeypatch.setenv("PAPER_LOW_PRICE_MAX_TRADES_PER_SWEEP", "1")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    day_start, day_end = candidates._candidate_day_bounds(now, date(2026, 7, 1))[1:]

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        session.add(
            PaperTrade(
                paper_trading_epoch_id=epoch_id,
                market_ticker="KXMLBGAME-LOW-EXISTING-PIT",
                contract_side="yes",
                entry_price=Decimal("0.1500"),
                current_price=Decimal("0.1500"),
                quantity=1,
                entry_time=now - timedelta(hours=1),
                status="settled",
                market_family="full_game_winner",
            )
        )
        first = _cap_intent(
            session,
            epoch_id=epoch_id,
            target_date=date(2026, 7, 1),
            market_ticker="KXMLBGAME-LOW-FIRST-PIT",
            price="0.1500",
        )
        second = _cap_intent(
            session,
            epoch_id=epoch_id,
            target_date=date(2026, 7, 1),
            market_ticker="KXMLBGAME-LOW-SECOND-PIT",
            price="0.1500",
            score="0.9000",
        )

        selected, cap_counts, summary = candidates._apply_trade_caps(
            session,
            [first, second],
            date(2026, 7, 1),
            day_start,
            day_end,
            epoch_id,
        )

    assert len(selected) == 1
    assert cap_counts["no_trade_low_price_slate_cap"] == 1
    assert summary["low_price_existing_slate"] == 1
    assert summary["low_price_new_this_sweep"] == 1
    assert second.candidate.decision == "no_trade_low_price_slate_cap"


def test_low_price_sweep_cap_blocks_second_low_price_trade(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_LOW_PRICE_MAX_TRADES_PER_SLATE", "10")
    monkeypatch.setenv("PAPER_LOW_PRICE_MAX_TRADES_PER_SWEEP", "1")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    day_start, day_end = candidates._candidate_day_bounds(now, date(2026, 7, 1))[1:]

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        first = _cap_intent(
            session,
            epoch_id=epoch_id,
            target_date=date(2026, 7, 1),
            market_ticker="KXMLBGAME-LOW-SWEEP-FIRST-PIT",
            price="0.1500",
        )
        second = _cap_intent(
            session,
            epoch_id=epoch_id,
            target_date=date(2026, 7, 1),
            market_ticker="KXMLBGAME-LOW-SWEEP-SECOND-PIT",
            price="0.1500",
            score="0.9000",
        )

        selected, cap_counts, _summary = candidates._apply_trade_caps(
            session,
            [first, second],
            date(2026, 7, 1),
            day_start,
            day_end,
            epoch_id,
        )

    assert len(selected) == 1
    assert cap_counts["no_trade_low_price_sweep_cap"] == 1
    assert second.candidate.decision == "no_trade_low_price_sweep_cap"


def test_post_cap_tiny_quantity_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_MIN_POST_CAP_CONTRACTS", "5")
    monkeypatch.setenv("PAPER_MIN_POST_CAP_NOTIONAL", "2.00")
    get_settings.cache_clear()

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        intent = _cap_intent(
            session,
            epoch_id=epoch_id,
            target_date=date(2026, 7, 1),
            market_ticker="KXMLBGAME-POST-CAP-PIT",
            price="0.4000",
        )
        intent.quantity = 1
        intent.candidate.estimated_total_cost = Decimal("0.41")
        intent.sizing = {"original_contracts": 12, "adjusted_by_aggregate_risk_cap": True}

        selected, counts = candidates._apply_post_cap_size_guard(session, [intent])

    assert selected == []
    assert counts["no_trade_post_cap_size_too_small"] == 1
    assert intent.candidate.decision == "no_trade_post_cap_size_too_small"
    assert intent.candidate.gate_diagnostics["post_cap_size"]["original_intended_contracts"] == 12


def test_post_cap_reject_does_not_consume_aggregate_risk(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_MIN_POST_CAP_CONTRACTS", "5")
    monkeypatch.setenv("PAPER_MIN_POST_CAP_NOTIONAL", "2.00")
    monkeypatch.setenv("PAPER_MAX_DAILY_NEW_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_OPEN_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_MARKET_FAMILY_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_SCOPE_RISK_PCT", "0.008")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    day_start, day_end = candidates._candidate_day_bounds(now, date(2026, 7, 1))[1:]

    try:
        with Session(engine) as session:
            epoch_id = _active_epoch_id(session)
            reduced_below_minimum = _cap_intent(
                session,
                epoch_id=epoch_id,
                target_date=date(2026, 7, 1),
                market_ticker="KXMLBGAME-POST-CAP-REFUND-HIGH-PIT",
                price="0.9000",
                score="1.0000",
            )
            reduced_below_minimum.quantity = 10
            reduced_below_minimum.candidate.estimated_cost_per_contract = Decimal("0.920000")
            reduced_below_minimum.candidate.estimated_total_cost = Decimal("9.20")
            reduced_below_minimum.sizing = {"original_contracts": 10}

            valid_after_reject = _cap_intent(
                session,
                epoch_id=epoch_id,
                target_date=date(2026, 7, 1),
                market_ticker="KXMLBGAME-POST-CAP-REFUND-VALID-PIT",
                price="0.2000",
                score="0.9000",
            )
            valid_after_reject.quantity = 10
            valid_after_reject.candidate.estimated_cost_per_contract = Decimal("0.200000")
            valid_after_reject.candidate.estimated_total_cost = Decimal("2.00")
            valid_after_reject.sizing = {"original_contracts": 10}

            selected, cap_counts, summary = candidates._apply_aggregate_risk_caps(
                session,
                [reduced_below_minimum, valid_after_reject],
                target_date=date(2026, 7, 1),
                day_start=day_start,
                day_end=day_end,
                epoch_id=epoch_id,
                bankroll=Decimal("500.00"),
            )
    finally:
        get_settings.cache_clear()

    assert selected == [valid_after_reject]
    assert cap_counts["aggregate_risk_quantity_reduced"] == 1
    assert cap_counts["no_trade_post_cap_size_too_small"] == 1
    assert reduced_below_minimum.candidate.decision == "no_trade_post_cap_size_too_small"
    assert reduced_below_minimum.candidate.gate_diagnostics["post_cap_size"]["final_contracts"] == 4
    assert valid_after_reject.candidate.decision == "paper_trade"
    assert summary["daily_risk_used"] == 2.0


def test_per_sweep_cap_limits_new_trades(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_MAX_NEW_TRADES_PER_SWEEP", "3")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    day_start, day_end = candidates._candidate_day_bounds(now, date(2026, 7, 1))[1:]

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        intents = [
            _cap_intent(
                session,
                epoch_id=epoch_id,
                target_date=date(2026, 7, 1),
                market_ticker=f"KXMLBGAME-SWEEP-CAP-{index}-PIT",
                score=str(Decimal("1.0") - Decimal(index) / Decimal("10")),
            )
            for index in range(4)
        ]

        selected, cap_counts, summary = candidates._apply_trade_caps(
            session,
            intents,
            date(2026, 7, 1),
            day_start,
            day_end,
            epoch_id,
        )

    assert len(selected) == 3
    assert cap_counts["no_trade_sweep_cap_reached"] == 1
    assert summary["new_trades_this_sweep"] == 3


def test_time_bucket_reserve_leaves_later_slots(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_SLATE", "8")
    monkeypatch.setenv("PAPER_MAX_NEW_TRADES_PER_SWEEP", "10")
    monkeypatch.setenv("PAPER_MAX_NEW_TRADES_BEFORE_3PM_ET", "4")
    monkeypatch.setenv("PAPER_RESERVE_TRADES_AFTER_3PM_ET", "2")
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_MARKET_FAMILY", "10")
    get_settings.cache_clear()
    early_now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    day_start, day_end = candidates._candidate_day_bounds(early_now, date(2026, 7, 1))[1:]

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        monkeypatch.setattr(candidates, "utc_now", lambda: early_now)
        early_intents = [
            _cap_intent(
                session,
                epoch_id=epoch_id,
                target_date=date(2026, 7, 1),
                market_ticker=f"KXMLBGAME-EARLY-RESERVE-{index}-PIT",
                score=str(Decimal("1.0") - Decimal(index) / Decimal("10")),
            )
            for index in range(5)
        ]
        early_selected, early_counts, early_summary = candidates._apply_trade_caps(
            session,
            early_intents,
            date(2026, 7, 1),
            day_start,
            day_end,
            epoch_id,
        )
        for selected in early_selected:
            session.add(
                PaperTrade(
                    paper_trading_epoch_id=epoch_id,
                    candidate_id=selected.candidate.id,
                    market_ticker=selected.market.ticker,
                    contract_side="yes",
                    entry_price=selected.price,
                    current_price=selected.price,
                    quantity=1,
                    entry_time=early_now,
                    status="open",
                    market_family="full_game_winner",
                )
            )
        session.flush()

        later_now = datetime(2026, 7, 1, 20, 30, tzinfo=UTC)
        monkeypatch.setattr(candidates, "utc_now", lambda: later_now)
        later_intent = _cap_intent(
            session,
            epoch_id=epoch_id,
            target_date=date(2026, 7, 1),
            market_ticker="KXMLBGAME-LATER-RESERVE-PIT",
        )
        later_selected, later_counts, _later_summary = candidates._apply_trade_caps(
            session,
            [later_intent],
            date(2026, 7, 1),
            day_start,
            day_end,
            epoch_id,
        )

    assert len(early_selected) == 4
    assert early_counts["no_trade_time_bucket_reserve"] == 1
    assert early_summary["early_window_allowed"] == 4
    assert len(later_selected) == 1
    assert later_counts["no_trade_time_bucket_reserve"] == 0


def test_same_side_concentration_cap_blocks_excess_side(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_MAX_SAME_SIDE_TRADES_PER_SLATE", "1")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    day_start, day_end = candidates._candidate_day_bounds(now, date(2026, 7, 1))[1:]

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        session.add(
            PaperTrade(
                paper_trading_epoch_id=epoch_id,
                market_ticker="KXMLBGAME-SIDE-EXISTING-PIT",
                contract_side="yes",
                entry_price=Decimal("0.4000"),
                current_price=Decimal("0.4000"),
                quantity=1,
                entry_time=now - timedelta(hours=1),
                status="settled",
                market_family="full_game_winner",
            )
        )
        intent = _cap_intent(
            session,
            epoch_id=epoch_id,
            target_date=date(2026, 7, 1),
            market_ticker="KXMLBGAME-SIDE-CAP-PIT",
            side="yes",
        )

        selected, cap_counts, _summary = candidates._apply_trade_caps(
            session,
            [intent],
            date(2026, 7, 1),
            day_start,
            day_end,
            epoch_id,
        )

    assert selected == []
    assert cap_counts["no_trade_side_concentration_cap"] == 1
    assert intent.candidate.decision == "no_trade_side_concentration_cap"


def test_normal_non_low_price_candidate_can_still_open(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    monkeypatch.setenv("PAPER_MIN_NET_EV", "0.05")
    monkeypatch.setenv("PAPER_MIN_PROB_EDGE", "0.03")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.900000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="normal-paper-game",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-NORMAL-PAPER",
            ticker="KXMLBGAME-NORMAL-PAPER-PIT",
            title="Will Pittsburgh win?",
            status="open",
            implied_yes_ask=Decimal("0.4000"),
        )
        session.add_all([game, market])
        session.flush()
        _add_candidate_mapping(session, game, market, mapping_status="confirmed", market_type="full_game_winner")
        session.commit()

        result = candidates.generate_candidates(session)
        trade = session.scalar(select(PaperTrade).where(PaperTrade.market_ticker == "KXMLBGAME-NORMAL-PAPER-PIT"))

    assert result["paper_trades"] == 1
    assert trade is not None
    assert trade.contract_side == "yes"


def test_sweep_cap_refills_after_sizing_and_post_cap_rejections(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    monkeypatch.setenv("PAPER_MAX_NEW_TRADES_PER_SWEEP", "1")
    monkeypatch.setenv("PAPER_MAX_DAILY_NEW_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_OPEN_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_MARKET_FAMILY_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_SCOPE_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MIN_NET_EV", "0.05")
    monkeypatch.setenv("PAPER_MIN_PROB_EDGE", "0.03")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.990000"))
    monkeypatch.setattr(
        candidates,
        "_trade_rank_score",
        lambda candidate: Decimal("2.0000")
        if candidate.executable_price == Decimal("0.9000")
        else Decimal("1.0000"),
    )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine, autoflush=False) as session:
            high_rank_game = MlbGame(
                external_game_id="sweep-refill-high",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
            high_rank_market = KalshiMarket(
                kalshi_market_id="KX-SWEEP-REFILL-HIGH",
                ticker="KXMLBGAME-SWEEP-REFILL-HIGH-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.9000"),
            )
            refill_game = MlbGame(
                external_game_id="sweep-refill-valid",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
                status="scheduled",
            )
            refill_market = KalshiMarket(
                kalshi_market_id="KX-SWEEP-REFILL-VALID",
                ticker="KXMLBGAME-SWEEP-REFILL-VALID-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.1000"),
            )
            session.add_all([high_rank_game, high_rank_market, refill_game, refill_market])
            session.flush()
            _add_candidate_mapping(
                session,
                high_rank_game,
                high_rank_market,
                mapping_status="confirmed",
                market_type="full_game_winner",
            )
            _add_candidate_mapping(
                session,
                refill_game,
                refill_market,
                mapping_status="confirmed",
                market_type="full_game_winner",
            )
            session.commit()

            result = candidates.generate_candidates(session)
            trades = list(session.scalars(select(PaperTrade).order_by(PaperTrade.id.asc())))
            high_rank_candidate = session.scalar(
                select(ModelCandidate).where(ModelCandidate.kalshi_market_id == high_rank_market.id)
            )
            refill_candidate = session.scalar(
                select(ModelCandidate).where(ModelCandidate.kalshi_market_id == refill_market.id)
            )
    finally:
        get_settings.cache_clear()

    assert result["paper_trades"] == 1
    assert result["cap_counts"]["no_trade_post_cap_size_too_small"] == 1
    assert result["cap_counts"]["no_trade_sweep_cap_reached"] == 0
    assert trades[0].market_ticker == "KXMLBGAME-SWEEP-REFILL-VALID-PIT"
    assert high_rank_candidate is not None
    assert high_rank_candidate.decision == "no_trade_post_cap_size_too_small"
    assert refill_candidate is not None
    assert refill_candidate.decision == "paper_trade"


def test_early_reserve_refills_after_sizing_and_post_cap_rejections(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_SLATE", "8")
    monkeypatch.setenv("PAPER_MAX_NEW_TRADES_PER_SWEEP", "10")
    monkeypatch.setenv("PAPER_MAX_NEW_TRADES_BEFORE_3PM_ET", "1")
    monkeypatch.setenv("PAPER_RESERVE_TRADES_AFTER_3PM_ET", "2")
    monkeypatch.setenv("PAPER_MAX_DAILY_NEW_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_OPEN_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_MARKET_FAMILY_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_SCOPE_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MIN_NET_EV", "0.05")
    monkeypatch.setenv("PAPER_MIN_PROB_EDGE", "0.03")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.990000"))
    monkeypatch.setattr(
        candidates,
        "_trade_rank_score",
        lambda candidate: Decimal("2.0000")
        if candidate.executable_price == Decimal("0.9000")
        else Decimal("1.0000"),
    )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine, autoflush=False) as session:
            high_rank_game = MlbGame(
                external_game_id="early-refill-high",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
            high_rank_market = KalshiMarket(
                kalshi_market_id="KX-EARLY-REFILL-HIGH",
                ticker="KXMLBGAME-EARLY-REFILL-HIGH-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.9000"),
            )
            refill_game = MlbGame(
                external_game_id="early-refill-valid",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
                status="scheduled",
            )
            refill_market = KalshiMarket(
                kalshi_market_id="KX-EARLY-REFILL-VALID",
                ticker="KXMLBGAME-EARLY-REFILL-VALID-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.4000"),
            )
            session.add_all([high_rank_game, high_rank_market, refill_game, refill_market])
            session.flush()
            _add_candidate_mapping(
                session,
                high_rank_game,
                high_rank_market,
                mapping_status="confirmed",
                market_type="full_game_winner",
            )
            _add_candidate_mapping(
                session,
                refill_game,
                refill_market,
                mapping_status="confirmed",
                market_type="full_game_winner",
            )
            session.commit()

            result = candidates.generate_candidates(session)
            trades = list(session.scalars(select(PaperTrade).order_by(PaperTrade.id.asc())))
            high_rank_candidate = session.scalar(
                select(ModelCandidate).where(ModelCandidate.kalshi_market_id == high_rank_market.id)
            )
            refill_candidate = session.scalar(
                select(ModelCandidate).where(ModelCandidate.kalshi_market_id == refill_market.id)
            )
    finally:
        get_settings.cache_clear()

    assert result["paper_trades"] == 1
    assert result["cap_counts"]["no_trade_post_cap_size_too_small"] == 1
    assert result["cap_counts"]["no_trade_time_bucket_reserve"] == 0
    assert result["trade_allocation"]["early_window_used"] == 1
    assert trades[0].market_ticker == "KXMLBGAME-EARLY-REFILL-VALID-PIT"
    assert high_rank_candidate is not None
    assert high_rank_candidate.decision == "no_trade_post_cap_size_too_small"
    assert refill_candidate is not None
    assert refill_candidate.decision == "paper_trade"


def test_low_price_sweep_cap_refills_after_sizing_rejection(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    monkeypatch.setenv("PAPER_MAX_NEW_TRADES_PER_SWEEP", "10")
    monkeypatch.setenv("PAPER_LOW_PRICE_MAX_TRADES_PER_SWEEP", "1")
    monkeypatch.setenv("PAPER_LOW_PRICE_MAX_TRADES_PER_SLATE", "2")
    monkeypatch.setenv("PAPER_MIN_CONTRACTS", "5")
    monkeypatch.setenv("PAPER_MIN_POST_CAP_NOTIONAL", "0.50")
    monkeypatch.setenv("PAPER_RISK_PER_TRADE_PCT", "0.0016")
    monkeypatch.setenv("PAPER_MIN_NET_EV", "0.00")
    monkeypatch.setenv("PAPER_MIN_PROB_EDGE", "0.00")
    monkeypatch.setenv("PAPER_LOW_PRICE_MIN_NET_EV", "0.00")
    monkeypatch.setenv("PAPER_LOW_PRICE_MIN_PROB_EDGE", "0.00")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.990000"))
    monkeypatch.setattr(
        candidates,
        "_trade_rank_score",
        lambda candidate: Decimal("2.0000")
        if candidate.executable_price == Decimal("0.1900")
        else Decimal("1.0000"),
    )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine, autoflush=False) as session:
            high_rank_game = MlbGame(
                external_game_id="low-price-refill-high",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
            high_rank_market = KalshiMarket(
                kalshi_market_id="KX-LOW-PRICE-REFILL-HIGH",
                ticker="KXMLBGAME-LOW-PRICE-REFILL-HIGH-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.1900"),
            )
            refill_game = MlbGame(
                external_game_id="low-price-refill-valid",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
                status="scheduled",
            )
            refill_market = KalshiMarket(
                kalshi_market_id="KX-LOW-PRICE-REFILL-VALID",
                ticker="KXMLBGAME-LOW-PRICE-REFILL-VALID-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.1000"),
            )
            session.add_all([high_rank_game, high_rank_market, refill_game, refill_market])
            session.flush()
            _add_candidate_mapping(
                session,
                high_rank_game,
                high_rank_market,
                mapping_status="confirmed",
                market_type="full_game_winner",
            )
            _add_candidate_mapping(
                session,
                refill_game,
                refill_market,
                mapping_status="confirmed",
                market_type="full_game_winner",
            )
            session.commit()

            result = candidates.generate_candidates(session)
            trades = list(session.scalars(select(PaperTrade).order_by(PaperTrade.id.asc())))
            high_rank_candidate = session.scalar(
                select(ModelCandidate).where(ModelCandidate.kalshi_market_id == high_rank_market.id)
            )
            refill_candidate = session.scalar(
                select(ModelCandidate).where(ModelCandidate.kalshi_market_id == refill_market.id)
            )
    finally:
        get_settings.cache_clear()

    assert result["paper_trades"] == 1
    assert result["cap_counts"]["no_trade_low_price_sweep_cap"] == 0
    assert result["trade_allocation"]["low_price_new_this_sweep"] == 1
    assert trades[0].market_ticker == "KXMLBGAME-LOW-PRICE-REFILL-VALID-PIT"
    assert high_rank_candidate is not None
    assert high_rank_candidate.decision == "no_trade_insufficient_bankroll_or_contract_size"
    assert refill_candidate is not None
    assert refill_candidate.decision == "paper_trade"


def test_same_side_cap_refills_after_sizing_and_post_cap_rejections(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    monkeypatch.setenv("PAPER_MAX_SAME_SIDE_TRADES_PER_SLATE", "1")
    monkeypatch.setenv("PAPER_MAX_NEW_TRADES_PER_SWEEP", "10")
    monkeypatch.setenv("PAPER_MAX_DAILY_NEW_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_OPEN_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_MARKET_FAMILY_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_SCOPE_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MIN_NET_EV", "0.05")
    monkeypatch.setenv("PAPER_MIN_PROB_EDGE", "0.03")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.990000"))
    monkeypatch.setattr(
        candidates,
        "_trade_rank_score",
        lambda candidate: Decimal("2.0000")
        if candidate.executable_price == Decimal("0.9000")
        else Decimal("1.0000"),
    )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine, autoflush=False) as session:
            high_rank_game = MlbGame(
                external_game_id="same-side-refill-high",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
            high_rank_market = KalshiMarket(
                kalshi_market_id="KX-SAME-SIDE-REFILL-HIGH",
                ticker="KXMLBGAME-SAME-SIDE-REFILL-HIGH-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.9000"),
            )
            refill_game = MlbGame(
                external_game_id="same-side-refill-valid",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
                status="scheduled",
            )
            refill_market = KalshiMarket(
                kalshi_market_id="KX-SAME-SIDE-REFILL-VALID",
                ticker="KXMLBGAME-SAME-SIDE-REFILL-VALID-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.4000"),
            )
            session.add_all([high_rank_game, high_rank_market, refill_game, refill_market])
            session.flush()
            _add_candidate_mapping(
                session,
                high_rank_game,
                high_rank_market,
                mapping_status="confirmed",
                market_type="full_game_winner",
            )
            _add_candidate_mapping(
                session,
                refill_game,
                refill_market,
                mapping_status="confirmed",
                market_type="full_game_winner",
            )
            session.commit()

            result = candidates.generate_candidates(session)
            trades = list(session.scalars(select(PaperTrade).order_by(PaperTrade.id.asc())))
            high_rank_candidate = session.scalar(
                select(ModelCandidate).where(ModelCandidate.kalshi_market_id == high_rank_market.id)
            )
            refill_candidate = session.scalar(
                select(ModelCandidate).where(ModelCandidate.kalshi_market_id == refill_market.id)
            )
    finally:
        get_settings.cache_clear()

    assert result["paper_trades"] == 1
    assert result["cap_counts"]["no_trade_post_cap_size_too_small"] == 1
    assert result["cap_counts"]["no_trade_side_concentration_cap"] == 0
    assert result["trade_allocation"]["side_new_this_sweep"] == {"yes": 1}
    assert trades[0].market_ticker == "KXMLBGAME-SAME-SIDE-REFILL-VALID-PIT"
    assert high_rank_candidate is not None
    assert high_rank_candidate.decision == "no_trade_post_cap_size_too_small"
    assert refill_candidate is not None
    assert refill_candidate.decision == "paper_trade"


def test_slate_cap_refills_after_sizing_and_post_cap_rejections(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_SLATE", "1")
    monkeypatch.setenv("PAPER_MAX_NEW_TRADES_PER_SWEEP", "10")
    monkeypatch.setenv("PAPER_MAX_DAILY_NEW_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_OPEN_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_MARKET_FAMILY_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_SCOPE_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MIN_NET_EV", "0.05")
    monkeypatch.setenv("PAPER_MIN_PROB_EDGE", "0.03")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.990000"))
    monkeypatch.setattr(
        candidates,
        "_trade_rank_score",
        lambda candidate: Decimal("2.0000")
        if candidate.executable_price == Decimal("0.9000")
        else Decimal("1.0000"),
    )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine, autoflush=False) as session:
            high_rank_game = MlbGame(
                external_game_id="slate-refill-high",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
            high_rank_market = KalshiMarket(
                kalshi_market_id="KX-SLATE-REFILL-HIGH",
                ticker="KXMLBGAME-SLATE-REFILL-HIGH-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.9000"),
            )
            refill_game = MlbGame(
                external_game_id="slate-refill-valid",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
                status="scheduled",
            )
            refill_market = KalshiMarket(
                kalshi_market_id="KX-SLATE-REFILL-VALID",
                ticker="KXMLBGAME-SLATE-REFILL-VALID-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.4000"),
            )
            session.add_all([high_rank_game, high_rank_market, refill_game, refill_market])
            session.flush()
            _add_candidate_mapping(
                session,
                high_rank_game,
                high_rank_market,
                mapping_status="confirmed",
                market_type="full_game_winner",
            )
            _add_candidate_mapping(
                session,
                refill_game,
                refill_market,
                mapping_status="confirmed",
                market_type="full_game_winner",
            )
            session.commit()

            result = candidates.generate_candidates(session)
            trades = list(session.scalars(select(PaperTrade).order_by(PaperTrade.id.asc())))
            high_rank_candidate = session.scalar(
                select(ModelCandidate).where(ModelCandidate.kalshi_market_id == high_rank_market.id)
            )
            refill_candidate = session.scalar(
                select(ModelCandidate).where(ModelCandidate.kalshi_market_id == refill_market.id)
            )
    finally:
        get_settings.cache_clear()

    assert result["paper_trades"] == 1
    assert result["cap_counts"]["no_trade_post_cap_size_too_small"] == 1
    assert result["cap_counts"]["no_trade_slate_cap"] == 0
    assert result["trade_allocation"]["slate_trades_after_sizing_and_risk"] == 1
    assert trades[0].market_ticker == "KXMLBGAME-SLATE-REFILL-VALID-PIT"
    assert high_rank_candidate is not None
    assert high_rank_candidate.decision == "no_trade_post_cap_size_too_small"
    assert refill_candidate is not None
    assert refill_candidate.decision == "paper_trade"


def test_open_position_cap_refills_after_sizing_and_post_cap_rejections(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    monkeypatch.setenv("PAPER_MAX_OPEN_POSITIONS", "2")
    monkeypatch.setenv("PAPER_MAX_NEW_TRADES_PER_SWEEP", "10")
    monkeypatch.setenv("PAPER_MAX_DAILY_NEW_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_OPEN_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_MARKET_FAMILY_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MAX_SCOPE_RISK_PCT", "0.008")
    monkeypatch.setenv("PAPER_MIN_NET_EV", "0.00")
    monkeypatch.setenv("PAPER_MIN_PROB_EDGE", "0.00")
    monkeypatch.setenv("PAPER_LOW_PRICE_MIN_NET_EV", "0.00")
    monkeypatch.setenv("PAPER_LOW_PRICE_MIN_PROB_EDGE", "0.00")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.990000"))
    monkeypatch.setattr(
        candidates,
        "_trade_rank_score",
        lambda candidate: Decimal("2.0000")
        if candidate.executable_price == Decimal("0.9000")
        else Decimal("1.0000"),
    )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine, autoflush=False) as session:
            epoch_id = _active_epoch_id(session)
            session.add(
                PaperTrade(
                    paper_trading_epoch_id=epoch_id,
                    candidate_id=None,
                    market_ticker="KXMLBGAME-EXISTING-OPEN-PIT",
                    contract_side="yes",
                    entry_price=Decimal("0.1000"),
                    current_price=Decimal("0.1000"),
                    quantity=1,
                    entry_time=now - timedelta(hours=1),
                    status="open",
                    market_family="full_game_winner",
                )
            )
            high_rank_game = MlbGame(
                external_game_id="open-refill-high",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
            high_rank_market = KalshiMarket(
                kalshi_market_id="KX-OPEN-REFILL-HIGH",
                ticker="KXMLBGAME-OPEN-REFILL-HIGH-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.9000"),
            )
            refill_game = MlbGame(
                external_game_id="open-refill-valid",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 5, tzinfo=UTC),
                status="scheduled",
            )
            refill_market = KalshiMarket(
                kalshi_market_id="KX-OPEN-REFILL-VALID",
                ticker="KXMLBGAME-OPEN-REFILL-VALID-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.1000"),
            )
            session.add_all([high_rank_game, high_rank_market, refill_game, refill_market])
            session.flush()
            _add_candidate_mapping(
                session,
                high_rank_game,
                high_rank_market,
                mapping_status="confirmed",
                market_type="full_game_winner",
            )
            _add_candidate_mapping(
                session,
                refill_game,
                refill_market,
                mapping_status="confirmed",
                market_type="full_game_winner",
            )
            session.commit()

            result = candidates.generate_candidates(session)
            trades = list(session.scalars(select(PaperTrade).order_by(PaperTrade.id.asc())))
            high_rank_candidate = session.scalar(
                select(ModelCandidate).where(ModelCandidate.kalshi_market_id == high_rank_market.id)
            )
            refill_candidate = session.scalar(
                select(ModelCandidate).where(ModelCandidate.kalshi_market_id == refill_market.id)
            )
    finally:
        get_settings.cache_clear()

    assert result["paper_trades"] == 1
    assert result["cap_counts"]["no_trade_post_cap_size_too_small"] == 1
    assert result["cap_counts"]["no_trade_open_position_cap"] == 0
    assert result["trade_allocation"]["open_positions_after_sizing_and_risk"] == 2
    assert trades[-1].market_ticker == "KXMLBGAME-OPEN-REFILL-VALID-PIT"
    assert high_rank_candidate is not None
    assert high_rank_candidate.decision == "no_trade_post_cap_size_too_small"
    assert refill_candidate is not None
    assert refill_candidate.decision == "paper_trade"


def test_pr3c_trade_policy_counts_settled_trades_against_daily_caps(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_GAME", "1")
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_SLATE", "20")
    monkeypatch.setenv("PAPER_MIN_DATA_QUALITY", "0")
    monkeypatch.setenv("PAPER_OBSERVATION_MIN_DATA_QUALITY", "0")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            epoch_id = _active_epoch_id(session)
            game = MlbGame(
                external_game_id="settled-cap-game",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
            market = KalshiMarket(
                kalshi_market_id="KX-SETTLED-CAP-NEW",
                ticker="KXMLBGAME-SETTLED-CAP-PIT",
                title="Cheap home winner",
                status="open",
                implied_yes_ask=Decimal("0.1000"),
            )
            session.add_all([game, market])
            session.flush()
            _add_candidate_mapping(session, game, market)

            settled_candidate = ModelCandidate(
                paper_trading_epoch_id=epoch_id,
                mlb_game_id=game.id,
                evaluated_at=now - timedelta(hours=1),
                features={},
                decision="paper_trade",
                market_family="full_game_winner",
            )
            session.add(settled_candidate)
            session.flush()
            session.add(
                PaperTrade(
                    paper_trading_epoch_id=epoch_id,
                    candidate_id=settled_candidate.id,
                    market_ticker="KXMLBGAME-SETTLED-CAP-OLD",
                    contract_side="yes",
                    entry_price=Decimal("0.4000"),
                    current_price=Decimal("1.0000"),
                    quantity=1,
                    entry_time=now - timedelta(hours=1),
                    status="settled",
                    market_family="full_game_winner",
                )
            )
            session.commit()

            result = candidates.generate_candidates(session)
            new_trades = list(
                session.scalars(
                    select(PaperTrade).where(PaperTrade.market_ticker == "KXMLBGAME-SETTLED-CAP-PIT")
                )
            )
            latest_candidate = session.scalar(
                select(ModelCandidate).where(ModelCandidate.mapping_id.is_not(None)).order_by(ModelCandidate.id.desc())
            )
    finally:
        get_settings.cache_clear()

    assert result["paper_trades"] == 0
    assert result["cap_counts"]["no_trade_game_cap"] == 1
    assert new_trades == []
    assert latest_candidate is not None
    assert latest_candidate.decision == "no_trade_game_cap"


def test_candidate_less_trade_entry_time_counts_against_slate_cap(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_SLATE", "1")
    monkeypatch.setenv("PAPER_MIN_NET_EV", "0")
    monkeypatch.setenv("PAPER_MIN_PROB_EDGE", "0")
    monkeypatch.setenv("PAPER_OBSERVATION_MIN_DATA_QUALITY", "0")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *args, **kwargs: _fixed_model_score("0.900000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            epoch_id = _active_epoch_id(session)
            game = MlbGame(
                external_game_id="candidate-less-slate-cap-game",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
            market = KalshiMarket(
                kalshi_market_id="KX-CANDIDATE-LESS-SLATE-CAP",
                ticker="KXMLBGAME-CANDIDATE-LESS-SLATE-CAP-PIT",
                title="Will Pittsburgh win?",
                status="open",
                implied_yes_ask=Decimal("0.1000"),
            )
            session.add_all([game, market])
            session.flush()
            _add_candidate_mapping(session, game, market)
            session.add(
                PaperTrade(
                    paper_trading_epoch_id=epoch_id,
                    candidate_id=None,
                    market_ticker="LEGACY-MANUAL-PAPER-TRADE",
                    contract_side="yes",
                    entry_price=Decimal("0.4000"),
                    current_price=Decimal("0.4000"),
                    quantity=1,
                    entry_time=now - timedelta(hours=1),
                    status="open",
                    market_family="full_game_winner",
                )
            )
            session.commit()

            result = candidates.generate_candidates(session)
            candidate = session.scalar(select(ModelCandidate))
            new_trade = session.scalar(
                select(PaperTrade).where(PaperTrade.market_ticker == "KXMLBGAME-CANDIDATE-LESS-SLATE-CAP-PIT")
            )
    finally:
        get_settings.cache_clear()

    assert result["paper_trades"] == 0
    assert result["cap_counts"]["no_trade_slate_cap"] == 1
    assert candidate is not None
    assert candidate.decision == "no_trade_slate_cap"
    assert new_trade is None


def test_line_selection_rejects_correlated_total_lines_before_caps(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_SLATE", "20")
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_GAME_FAMILY", "1")
    monkeypatch.setenv("PAPER_MIN_NET_EV", "0")
    monkeypatch.setenv("PAPER_MIN_PROB_EDGE", "0")
    monkeypatch.setenv("PAPER_OBSERVATION_MIN_DATA_QUALITY", "0")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *args, **kwargs: _fixed_model_score("0.900000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            game = MlbGame(
                external_game_id="line-selection-game",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
            session.add(game)
            session.flush()
            for index, line in enumerate((Decimal("7.5000"), Decimal("8.5000"), Decimal("9.5000"))):
                market = KalshiMarket(
                    kalshi_market_id=f"KX-LINE-SELECTION-{index}",
                    ticker=f"KXMLBTOTAL-LINE-SELECTION-{index}-OVER-{line}",
                    title=f"Game total over {line}",
                    status="open",
                    implied_yes_ask=Decimal("0.1000") + (Decimal(index) * Decimal("0.0100")),
                    market_family="full_game_total",
                    market_type="full_game_total",
                    line_value=line,
                    over_under_side="over",
                    inning_scope="full_game",
                    settlement_rule_status="paper_supported",
                )
                session.add(market)
                _add_candidate_mapping(
                    session,
                    game,
                    market,
                    mapping_status="confirmed",
                    market_family="full_game_total",
                    market_type="full_game_total",
                    line_value=line,
                    over_under_side="over",
                    inning_scope="full_game",
                    settlement_rule_status="paper_supported",
                )
            session.commit()

            result = candidates.generate_candidates(session)
            decisions = [candidate.decision for candidate in session.scalars(select(ModelCandidate))]
            trades = list(session.scalars(select(PaperTrade)))
    finally:
        get_settings.cache_clear()

    assert result["line_selection_groups_considered"] == 1
    assert result["line_selection_candidates_kept"] == 1
    assert result["line_selection_candidates_rejected"] == 2
    assert decisions.count("paper_trade") == 1
    assert decisions.count("no_trade_line_selection_not_best") == 2
    assert len(trades) == 1


def test_trade_caps_allow_configured_multiple_total_lines(monkeypatch) -> None:
    monkeypatch.setenv("PAPER_ALLOW_MULTIPLE_LINES_PER_GAME_FAMILY", "true")
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_GAME_FAMILY", "2")
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_GAME_SCOPE", "2")
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_GAME", "3")
    monkeypatch.setenv("PAPER_MAX_TRADES_PER_MARKET_FAMILY", "8")
    monkeypatch.setenv("PAPER_LOW_PRICE_MAX_TRADES_PER_SWEEP", "10")
    monkeypatch.setenv("PAPER_MIN_NET_EV", "0")
    monkeypatch.setenv("PAPER_MIN_PROB_EDGE", "0")
    monkeypatch.setenv("PAPER_OBSERVATION_MIN_DATA_QUALITY", "0")
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *args, **kwargs: _fixed_model_score("0.900000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            game = MlbGame(
                external_game_id="multiple-line-cap-game",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
            session.add(game)
            session.flush()
            for index, line in enumerate((Decimal("7.5000"), Decimal("8.5000"), Decimal("9.5000"))):
                market = KalshiMarket(
                    kalshi_market_id=f"KX-MULTI-LINE-CAP-{index}",
                    ticker=f"KXMLBTOTAL-MULTI-LINE-CAP-{index}-OVER-{line}",
                    title=f"Game total over {line}",
                    status="open",
                    implied_yes_ask=Decimal("0.1000") + (Decimal(index) * Decimal("0.0100")),
                    market_family="full_game_total",
                    market_type="full_game_total",
                    line_value=line,
                    over_under_side="over",
                    inning_scope="full_game",
                    settlement_rule_status="paper_supported",
                )
                session.add(market)
                _add_candidate_mapping(
                    session,
                    game,
                    market,
                    mapping_status="confirmed",
                    market_family="full_game_total",
                    market_type="full_game_total",
                    line_value=line,
                    over_under_side="over",
                    inning_scope="full_game",
                    settlement_rule_status="paper_supported",
                )
            session.commit()

            result = candidates.generate_candidates(session)
            decisions = [candidate.decision for candidate in session.scalars(select(ModelCandidate))]
            trades = list(session.scalars(select(PaperTrade)))
    finally:
        get_settings.cache_clear()

    assert result["line_selection_candidates_kept"] == 2
    assert result["line_selection_candidates_rejected"] == 1
    assert result["paper_trades"] == 2
    assert result["cap_counts"]["no_trade_correlated_market_cap"] == 0
    assert result["cap_counts"]["no_trade_game_family_cap"] == 0
    assert decisions.count("paper_trade") == 2
    assert decisions.count("no_trade_line_selection_not_best") == 1
    assert len(trades) == 2


def test_model_predictions_today_filters_by_prediction_run_target_date(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    monkeypatch.setattr(main_module, "today_eastern", lambda: date(2026, 7, 2))
    monkeypatch.setattr(
        main_module,
        "database_status",
        lambda: {"ready": True, "configured": True, "dialect": "sqlite", "message": "ok"},
    )
    monkeypatch.setattr(main_module, "get_session_factory", lambda: SessionLocal)

    with SessionLocal() as session:
        active_epoch = get_or_create_active_paper_epoch(session)
        archived_epoch = PaperTradingEpoch(
            epoch_key="archived-predictions",
            display_name="ARCHIVED PREDICTIONS",
            status="archived",
            mode="paper",
            starting_balance=Decimal("1000.00"),
            started_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            archived_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
        )
        session.add(archived_epoch)
        session.flush()
        old_run = ModelPredictionRun(
            paper_trading_epoch_id=active_epoch.id,
            started_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            target_date=date(2026, 7, 1),
            status="completed",
        )
        today_run = ModelPredictionRun(
            paper_trading_epoch_id=active_epoch.id,
            started_at=datetime(2026, 7, 2, 16, 0, tzinfo=UTC),
            target_date=date(2026, 7, 2),
            status="completed",
        )
        archived_today_run = ModelPredictionRun(
            paper_trading_epoch_id=archived_epoch.id,
            started_at=datetime(2026, 7, 2, 15, 0, tzinfo=UTC),
            target_date=date(2026, 7, 2),
            status="completed",
        )
        session.add_all([old_run, today_run, archived_today_run])
        session.flush()
        session.add_all(
            [
                ModelPredictionOutput(
                    paper_trading_epoch_id=active_epoch.id,
                    prediction_run_id=old_run.id,
                    market_family="full_game_winner",
                    probability_calibrated=Decimal("0.610000"),
                    decision_reason="yesterday",
                ),
                ModelPredictionOutput(
                    paper_trading_epoch_id=active_epoch.id,
                    prediction_run_id=today_run.id,
                    market_family="full_game_total",
                    probability_calibrated=Decimal("0.550000"),
                    decision_reason="today",
                ),
                ModelPredictionOutput(
                    paper_trading_epoch_id=archived_epoch.id,
                    prediction_run_id=archived_today_run.id,
                    market_family="full_game_total",
                    probability_calibrated=Decimal("0.530000"),
                    decision_reason="archived_today",
                ),
            ]
        )
        session.commit()

    app.dependency_overrides[require_internal_api_key] = lambda: None
    try:
        response = client.get("/v1/model/predictions/today")
        dated_response = client.get("/v1/model/predictions?date=2026-07-01")
        payload = response.json()
        dated_payload = dated_response.json()
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert payload["result"]["count"] == 1
    assert payload["result"]["items"][0]["decision_reason"] == "today"
    assert dated_response.status_code == 200
    assert dated_payload["result"]["date"] == "2026-07-01"
    assert dated_payload["result"]["count"] == 1
    assert dated_payload["result"]["items"][0]["decision_reason"] == "yesterday"


def test_training_eligibility_repair_excludes_pre_pr3c_candidates() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        candidate = ModelCandidate(
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="candidate_only",
            feature_version="market_family_wire_v1_pre_full_model",
            model_version_tag="baseline_market_family_wire_v1",
            training_eligible=True,
        )
        session.add(candidate)
        session.commit()

        result = modeling.repair_training_eligibility(session)
        session.refresh(candidate)

    assert result["candidates_marked_ineligible"] == 1
    assert candidate.training_eligible is False
    assert candidate.training_exclusion_reason == "pre_pr3c_or_non_mature_model"


@pytest.mark.parametrize(
    ("external_game_id", "home_team", "away_team", "home_code", "away_code", "scheduled_start", "market_ticker"),
    [
        (
            "exact-resolver-kc-tb",
            "Tampa Bay Rays",
            "Kansas City Royals",
            "TB",
            "KC",
            datetime(2026, 6, 25, 16, 10, tzinfo=UTC),
            "KXMLBGAME-26JUN251210KCTB-KC",
        ),
        (
            "exact-resolver-tex-tor",
            "Toronto Blue Jays",
            "Texas Rangers",
            "TOR",
            "TEX",
            datetime(2026, 6, 25, 23, 7, tzinfo=UTC),
            "KXMLBGAME-26JUN251907TEXTOR-TEX",
        ),
    ],
)
def test_resolve_game_markets_exact_kxmlb_match_stays_confirmed_for_paper(
    external_game_id: str,
    home_team: str,
    away_team: str,
    home_code: str,
    away_code: str,
    scheduled_start: datetime,
    market_ticker: str,
) -> None:
    game = MlbGame(
        external_game_id=external_game_id,
        home_team=home_team,
        away_team=away_team,
        home_abbreviation=home_code,
        away_abbreviation=away_code,
        scheduled_start=scheduled_start,
        status="scheduled",
    )
    event_ticker = market_ticker.rsplit("-", 1)[0]

    class FakeExactClient:
        def get_markets_by_tickers(self, tickers):
            assert market_ticker in tickers
            return {
                "markets": [
                    {
                        "ticker": market_ticker,
                        "event_ticker": event_ticker,
                        "title": f"{away_team} vs {home_team}",
                        "status": "open",
                    }
                ]
            }

        def get_event(self, event_ticker: str):
            raise AssertionError("exact direct match should not need event fallback")

        def get_markets_by_event_ticker(self, event_ticker: str, limit: int = 100):
            raise AssertionError("exact direct match should not need event filter fallback")

        def get_markets_by_series_window(self, *args, **kwargs):
            raise AssertionError("exact direct match should not need series fallback")

    resolution = resolve_game_markets(FakeExactClient(), game)

    assert len(resolution.matches) == 1
    match = resolution.matches[0]
    assert match.mapping_status == "confirmed"
    assert match.validation_status == "confirmed_for_paper"
    assert match.confidence == Decimal("0.9700")
    assert match.metadata["time_delta_minutes"] == 0
    assert match.metadata["team_match_score"] == 1.0
    assert match.metadata["ticker_team_codes_match"] is True
    assert "MARKET_TICKER_MATCH" in match.rationale
    assert "EVENT_TICKER_MATCH" in match.rationale
    assert "TICKER_TEAM_CODE_MATCH" in match.rationale
    assert "TIME_DELTA_MINUTES:0" in match.rationale
    assert "TEAM_MATCH_SCORE:1.00" in match.rationale


def test_market_family_discovery_persists_structured_by_family_and_excludes_mve(monkeypatch) -> None:
    monkeypatch.setenv("KALSHI_DISCOVERY_ENABLE_FALLBACK_TIME_OFFSETS", "false")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    spread_market = {
        "ticker": "KXMLBSPREAD-26JUL011900SEAPIT",
        "event_ticker": "KXMLBSPREAD-26JUL011900SEAPIT",
        "title": "Pittsburgh Pirates spread -1.5 vs Seattle Mariners",
        "yes_sub_title": "Pittsburgh -1.5",
        "no_sub_title": "Seattle +1.5",
        "rules_primary": "If Pittsburgh wins by 2 or more runs, Yes wins.",
        "status": "open",
        "functional_strike": "-1.5",
    }

    class FakeDiscoveryClient:
        def __init__(self) -> None:
            self.ticker_batches: list[list[str]] = []
            self.event_tickers: list[str] = []

        def get_market(self, ticker: str):
            raise AssertionError("discovery should use batched ticker lookup, not one request per ticker")

        def get_markets_by_tickers(self, tickers: list[str]):
            self.ticker_batches.append(tickers)
            return {"markets": []}

        def get_markets_by_event_ticker(self, event_ticker: str):
            self.event_tickers.append(event_ticker)
            if event_ticker == "KXMLBSPREAD-26JUL011900SEAPIT":
                return {"markets": [spread_market]}
            if event_ticker == "KXMLBTOTAL-26JUL011900SEAPIT":
                return {
                    "markets": [
                        {
                            "ticker": "KXMLBTOTAL-26JUL011900SEAPIT-OVER-8",
                            "event_ticker": "KXMLBTOTAL-26JUL011900SEAPIT",
                            "title": "Multivariate combo",
                            "mve_selected_legs": [{"ticker": "LEG"}],
                        }
                    ]
                }
            return {"markets": []}

    fake_client = FakeDiscoveryClient()
    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="discovery-game-1",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
        )
        session.commit()

        result = market_family_discovery.run_market_family_discovery(
            session,
            date(2026, 7, 1),
            client=fake_client,
        )
        run = session.scalar(select(MarketFamilyDiscoveryRun))
        items = list(session.scalars(select(MarketFamilyDiscoveryItem)))

    assert result["markets_found"] == 1
    assert result["by_family"]["full_game_spread"]["status"] == "discovered_unverified"
    assert result["by_family"]["full_game_spread"]["market_count"] == 1
    assert result["by_family"]["full_game_spread"]["line_or_strike_parsing_status"] == "parsed_unverified"
    assert result["warnings"][0]["message"] == "MULTIVARIATE_MARKET_EXCLUDED"
    assert run is not None
    assert run.raw_summary["markets_found"] == 1
    assert len(items) == 1
    assert items[0].family_key == "full_game_spread"
    assert items[0].candidate_market_ticker is None
    assert items[0].line_value == Decimal("-1.5000")
    assert all("KXMLBSPREAD-26JUL011900SEAPIT" not in batch for batch in fake_client.ticker_batches)
    assert "KXMLBSPREAD-26JUL011900SEAPIT" in fake_client.event_tickers
    assert result["lookup_strategy_counts"]["event_ticker"] > 0
    assert result["lookup_strategy_counts"]["guessed_market_ticker"] == 0
    assert result["request_count"] > 0
    assert result["attempted_event_tickers_count"] > 0
    assert result["event_ticker_request_count"] > 0
    assert result["guessed_market_ticker_request_count"] == 0
    assert "KXMLBTEAMTOTAL" not in result["retired_legacy_prefixes_not_used"]


def test_market_family_discovery_suppresses_fallback_event_probes_after_exact_hit() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    exact_spread_market = {
        "ticker": "KXMLBSPREAD-26JUL011900SEAPIT-PIT-1.5",
        "event_ticker": "KXMLBSPREAD-26JUL011900SEAPIT",
        "title": "Pittsburgh Pirates spread -1.5 vs Seattle Mariners",
        "status": "open",
        "functional_strike": "-1.5",
    }

    class FakeEventFallbackClient:
        def __init__(self) -> None:
            self.event_tickers: list[str] = []

        def get_market(self, ticker: str):
            raise AssertionError("discovery should use batched ticker lookup, not one request per ticker")

        def get_markets_by_tickers(self, tickers: list[str]):
            return {"markets": []}

        def get_markets_by_event_ticker(self, event_ticker: str):
            self.event_tickers.append(event_ticker)
            if event_ticker == "KXMLBSPREAD-26JUL011900SEAPIT":
                return {"markets": [exact_spread_market]}
            if event_ticker.startswith("KXMLBSPREAD-"):
                raise AssertionError(f"fallback spread event should not be probed after exact hit: {event_ticker}")
            return {"markets": []}

    fake_client = FakeEventFallbackClient()
    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="event-fallback-suppressed",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
        )
        session.commit()

        result = market_family_discovery.run_market_family_discovery(
            session,
            date(2026, 7, 1),
            client=fake_client,
        )
        items = list(session.scalars(select(MarketFamilyDiscoveryItem)))

    assert "KXMLBSPREAD-26JUL011900SEAPIT" in fake_client.event_tickers
    assert "KXMLBSPREAD-26JUL011901SEAPIT" not in fake_client.event_tickers
    assert result["markets_found"] == 1
    assert result["by_family"]["full_game_spread"]["market_count"] == 1
    assert len([item for item in items if item.family_key == "full_game_spread"]) == 1


def _kalshi_probe_error(status_code: int, endpoint: str = "https://kalshi.test/probe") -> KalshiAPIError:
    return KalshiAPIError(
        f"Kalshi probe failed with {status_code}",
        source=HttpJsonError(
            f"GET {endpoint} failed with HTTP {status_code}.",
            endpoint=endpoint,
            params={},
            status_code=status_code,
            body_preview="not found" if status_code == 404 else "upstream unavailable",
        ),
    )


def test_market_family_discovery_persists_zero_market_run_when_all_probes_404(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    class FakeNoMatchClient:
        def get_market(self, ticker: str):
            raise AssertionError("discovery should use batched ticker lookup, not one request per ticker")

        def get_markets_by_tickers(self, tickers: list[str]):
            raise _kalshi_probe_error(404, "https://kalshi.test/markets")

        def get_markets_by_event_ticker(self, event_ticker: str):
            raise _kalshi_probe_error(404, "https://kalshi.test/markets")

    monkeypatch.setattr(
        main_module,
        "database_status",
        lambda: {"ready": True, "configured": True, "dialect": "sqlite", "message": "ok"},
    )
    monkeypatch.setattr(main_module, "get_session_factory", lambda: SessionLocal)
    monkeypatch.setattr(
        market_family_discovery.KalshiClient,
        "from_market_data_settings",
        staticmethod(lambda: FakeNoMatchClient()),
    )
    app.dependency_overrides[require_internal_api_key] = lambda: None

    with SessionLocal() as session:
        session.add(
            MlbGame(
                external_game_id="zero-market-404",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
        )
        session.commit()

    try:
        post_response = client.post("/v1/run/market-family-discovery?target_date=2026-07-01")
        get_response = client.get("/v1/market-families/discovery?date=2026-07-01")
    finally:
        app.dependency_overrides.clear()

    post_payload = post_response.json()
    get_payload = get_response.json()
    assert post_response.status_code == 200
    assert post_payload["ok"] is True
    assert post_payload["result"]["status"] == "completed"
    assert post_payload["result"]["markets_found"] == 0
    assert post_payload["result"]["errors"] == []
    assert post_payload["result"]["warnings"]
    assert post_payload["result"]["warnings"][0]["message"] == "MARKET_FAMILY_PROBE_NO_MATCH"
    assert post_payload["result"]["attempted_probe_count"] > 0
    assert post_payload["result"]["probe_attempts"][0]["outcome"] == "no_match"
    assert post_payload["result"]["request_count"] > 0
    assert post_payload["result"]["stopped_due_to_rate_limit"] is False

    assert get_response.status_code == 200
    assert get_payload["result"]["run"] is not None
    assert get_payload["result"]["run"]["status"] == "completed"
    assert get_payload["result"]["run"]["markets_found"] == 0
    assert get_payload["result"]["attempted_probe_count"] == post_payload["result"]["attempted_probe_count"]

    with SessionLocal() as session:
        run = session.scalar(select(MarketFamilyDiscoveryRun))
        items = list(session.scalars(select(MarketFamilyDiscoveryItem)))
        running_run = session.scalar(select(MarketFamilyDiscoveryRun).where(MarketFamilyDiscoveryRun.status == "running"))

    assert run is not None
    assert run.status == "completed"
    assert run.completed_at is not None
    assert run.markets_found == 0
    assert run.raw_summary["resolver_mode"] == "deterministic_ticker_registry_v1"
    assert run.raw_summary["attempted_probe_count"] > 0
    assert run.raw_summary["attempted_market_tickers_count"] > 0
    assert run.raw_summary["probe_attempts"][0]["outcome"] == "no_match"
    assert running_run is None
    assert items == []


def test_market_family_discovery_rate_limit_circuit_breaker_finalizes_partial_error(monkeypatch) -> None:
    monkeypatch.setenv("KALSHI_DISCOVERY_MAX_429_ERRORS", "1")
    get_settings.cache_clear()

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeRateLimitedClient:
        request_count = 0
        rate_limited_count = 0
        retries_attempted = 0

        def get_market(self, ticker: str):
            raise AssertionError("discovery should use batched ticker lookup, not one request per ticker")

        def get_markets_by_tickers(self, tickers: list[str]):
            self.request_count += 1
            self.rate_limited_count += 1
            raise _kalshi_probe_error(429, "https://kalshi.test/markets")

        def get_markets_by_event_ticker(self, event_ticker: str):
            raise AssertionError("event fallback should stop after the rate-limit circuit breaker opens")

    try:
        with Session(engine) as session:
            session.add(
                MlbGame(
                    external_game_id="rate-limit-market-family",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="scheduled",
                )
            )
            session.commit()

            result = market_family_discovery.run_market_family_discovery(
                session,
                date(2026, 7, 1),
                client=FakeRateLimitedClient(),
            )
            run = session.scalar(select(MarketFamilyDiscoveryRun))
            running_run = session.scalar(
                select(MarketFamilyDiscoveryRun).where(MarketFamilyDiscoveryRun.status == "running")
            )
    finally:
        get_settings.cache_clear()

    assert result["status"] == "partial_rate_limited"
    assert result["stopped_due_to_rate_limit"] is True
    assert result["rate_limited_count"] >= 1
    assert result["cooldown_until"] is not None
    assert result["errors"][0]["error"]["upstream_status_code"] == 429
    assert run is not None
    assert run.status == "partial_rate_limited"
    assert run.completed_at is not None
    assert running_run is None


def test_market_family_discovery_uses_recent_cache_unless_force_refresh(monkeypatch) -> None:
    fixed_now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(market_family_discovery, "utc_now", lambda: fixed_now)
    monkeypatch.setenv("KALSHI_DISCOVERY_USE_CACHE_FIRST", "true")
    monkeypatch.setenv("KALSHI_DISCOVERY_SKIP_IF_RECENT_MINUTES", "60")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class ExplodingClient:
        def get_markets_by_tickers(self, tickers: list[str]):
            raise AssertionError("recent cached discovery should skip network calls")

        def get_markets_by_event_ticker(self, event_ticker: str):
            raise AssertionError("recent cached discovery should skip network calls")

    class CountingClient:
        def __init__(self) -> None:
            self.market_batch_calls = 0
            self.request_count = 0
            self.rate_limited_count = 0
            self.retries_attempted = 0

        def get_markets_by_tickers(self, tickers: list[str]):
            self.market_batch_calls += 1
            return {"markets": []}

        def get_markets_by_event_ticker(self, event_ticker: str):
            return {"markets": []}

    try:
        with Session(engine) as session:
            session.add_all(
                [
                    MlbGame(
                        external_game_id="cached-discovery-game",
                        home_team="Pittsburgh Pirates",
                        away_team="Seattle Mariners",
                        home_abbreviation="PIT",
                        away_abbreviation="SEA",
                        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                        status="scheduled",
                    ),
                    MarketFamilyDiscoveryRun(
                        target_date=date(2026, 7, 1),
                        started_at=fixed_now - timedelta(minutes=5),
                        completed_at=fixed_now - timedelta(minutes=4),
                        status="completed",
                        games_considered=1,
                        families_considered=6,
                        markets_found=1,
                        errors=[],
                        warnings=[],
                        raw_summary={
                            "run_id": 1,
                            "date": "2026-07-01",
                            "resolver_mode": market_family_discovery.RESOLVER_MODE,
                            "status": "completed",
                            "markets_found": 1,
                            "warnings": [],
                            "errors": [],
                        },
                    ),
                ]
            )
            session.commit()

            cached = market_family_discovery.run_market_family_discovery(
                session,
                date(2026, 7, 1),
                client=ExplodingClient(),
            )
            counting_client = CountingClient()
            refreshed = market_family_discovery.run_market_family_discovery(
                session,
                date(2026, 7, 1),
                client=counting_client,
                force_refresh=True,
            )
    finally:
        get_settings.cache_clear()

    assert cached["served_from_cache"] is True
    assert cached["network_skipped_reason"] == "recent_cached_discovery"
    assert refreshed["served_from_cache"] is False
    assert counting_client.market_batch_calls > 0


def test_market_family_discovery_cooldown_blocks_repeated_rate_limited_calls(monkeypatch) -> None:
    fixed_now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(market_family_discovery, "utc_now", lambda: fixed_now)
    monkeypatch.setenv("KALSHI_DISCOVERY_MAX_429_ERRORS", "1")
    monkeypatch.setenv("KALSHI_DISCOVERY_COOLDOWN_SECONDS", "300")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class RateLimitedClient:
        request_count = 0
        rate_limited_count = 0
        retries_attempted = 0

        def get_markets_by_tickers(self, tickers: list[str]):
            self.request_count += 1
            raise _kalshi_probe_error(429, "https://kalshi.test/markets")

        def get_markets_by_event_ticker(self, event_ticker: str):
            raise AssertionError("event fallback should not run after the rate-limit breaker opens")

    class ExplodingClient:
        def get_markets_by_tickers(self, tickers: list[str]):
            raise AssertionError("cooldown should skip network calls")

        def get_markets_by_event_ticker(self, event_ticker: str):
            raise AssertionError("cooldown should skip network calls")

    try:
        with Session(engine) as session:
            session.add(
                MlbGame(
                    external_game_id="cooldown-discovery-game",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="scheduled",
                )
            )
            session.commit()

            first = market_family_discovery.run_market_family_discovery(
                session,
                date(2026, 7, 1),
                client=RateLimitedClient(),
                force_refresh=True,
            )
            second = market_family_discovery.run_market_family_discovery(
                session,
                date(2026, 7, 1),
                client=ExplodingClient(),
            )
    finally:
        get_settings.cache_clear()

    assert first["status"] == "partial_rate_limited"
    assert first["cooldown_until"] is not None
    assert second["served_from_cache"] is True
    assert second["network_skipped_reason"] == "discovery_cooldown_active"


def test_market_family_discovery_respects_configured_batch_size(monkeypatch) -> None:
    monkeypatch.setenv("KALSHI_DISCOVERY_MAX_BATCH_SIZE", "2")
    monkeypatch.setenv("KALSHI_DISCOVERY_USE_CACHE_FIRST", "false")
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class BatchTrackingClient:
        request_count = 0
        rate_limited_count = 0
        retries_attempted = 0

        def __init__(self) -> None:
            self.batch_sizes: list[int] = []

        def get_markets_by_tickers(self, tickers: list[str]):
            self.batch_sizes.append(len(tickers))
            return {"markets": []}

        def get_markets_by_event_ticker(self, event_ticker: str):
            return {"markets": []}

    fake_client = BatchTrackingClient()
    try:
        with Session(engine) as session:
            session.add(
                MlbGame(
                    external_game_id="batch-size-discovery-game",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="scheduled",
                )
            )
            session.commit()
            result = market_family_discovery.run_market_family_discovery(
                session,
                date(2026, 7, 1),
                client=fake_client,
                force_refresh=True,
            )
    finally:
        get_settings.cache_clear()

    assert result["discovery_batch_size"] == 2
    assert fake_client.batch_sizes
    assert max(fake_client.batch_sizes) <= 2


def test_market_family_discovery_finalizes_stale_running_runs(monkeypatch) -> None:
    fixed_now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(market_family_discovery, "utc_now", lambda: fixed_now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class EmptyDiscoveryClient:
        def get_market(self, ticker: str):
            raise AssertionError("discovery should use batched ticker lookup, not one request per ticker")

        def get_markets_by_tickers(self, tickers: list[str]):
            return {"markets": []}

        def get_markets_by_event_ticker(self, event_ticker: str):
            return {"markets": []}

    with Session(engine) as session:
        stale_run = MarketFamilyDiscoveryRun(
            target_date=date(2026, 7, 1),
            started_at=fixed_now - timedelta(minutes=11),
            status="running",
            games_considered=1,
            families_considered=6,
            markets_found=0,
            errors=[],
            warnings=[],
            raw_summary={},
        )
        session.add_all(
            [
                stale_run,
                MlbGame(
                    external_game_id="stale-run-finalizer",
                    home_team="Pittsburgh Pirates",
                    away_team="Seattle Mariners",
                    home_abbreviation="PIT",
                    away_abbreviation="SEA",
                    scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                    status="scheduled",
                ),
            ]
        )
        session.commit()
        stale_run_id = stale_run.id

        result = market_family_discovery.run_market_family_discovery(
            session,
            date(2026, 7, 1),
            client=EmptyDiscoveryClient(),
        )
        finalized = session.get(MarketFamilyDiscoveryRun, stale_run_id)
        running_run = session.scalar(select(MarketFamilyDiscoveryRun).where(MarketFamilyDiscoveryRun.status == "running"))

    assert result["stale_runs_finalized"] == 1
    assert finalized is not None
    assert finalized.status == "partial_error"
    assert finalized.completed_at == fixed_now.replace(tzinfo=None)
    assert finalized.warnings[0]["message"] == "STALE_RUNNING_RUN_FINALIZED"
    assert running_run is None


def test_market_family_discovery_records_non_404_errors_and_continues() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakePartialErrorClient:
        def __init__(self) -> None:
            self.failed_once = False

        def get_market(self, ticker: str):
            raise AssertionError("discovery should use batched ticker lookup, not one request per ticker")

        def get_markets_by_tickers(self, tickers: list[str]):
            if not self.failed_once:
                self.failed_once = True
                raise _kalshi_probe_error(500, "https://kalshi.test/markets")
            return {"markets": []}

        def get_markets_by_event_ticker(self, event_ticker: str):
            if event_ticker == "KXMLBSPREAD-26JUL011900SEAPIT":
                return {
                    "markets": [
                        {
                            "ticker": "KXMLBSPREAD-26JUL011900SEAPIT-PIT-1.5",
                            "event_ticker": event_ticker,
                            "title": "Pittsburgh Pirates spread -1.5 vs Seattle Mariners",
                            "status": "open",
                            "functional_strike": "-1.5",
                        }
                    ]
                }
            return {"markets": []}

    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="partial-error-market",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
        )
        session.commit()

        result = market_family_discovery.run_market_family_discovery(
            session,
            date(2026, 7, 1),
            client=FakePartialErrorClient(),
        )
        run = session.scalar(select(MarketFamilyDiscoveryRun))
        item = session.scalar(select(MarketFamilyDiscoveryItem))

    assert result["status"] == "partial_error"
    assert result["markets_found"] == 1
    assert result["errors"][0]["message"] == "MARKET_FAMILY_PROBE_ERROR"
    assert result["errors"][0]["error"]["upstream_status_code"] == 500
    assert run is not None
    assert run.status == "partial_error"
    assert run.raw_summary["errors"][0]["message"] == "MARKET_FAMILY_PROBE_ERROR"
    assert item is not None
    assert item.returned_ticker == "KXMLBSPREAD-26JUL011900SEAPIT-PIT-1.5"


def test_market_family_discovery_handles_batched_winner_market_response() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    winner_market = {
        "ticker": "KXMLBF5-26JUL011900SEAPIT-TIE",
        "event_ticker": "KXMLBF5-26JUL011900SEAPIT",
        "title": "Seattle Mariners vs Pittsburgh Pirates first five innings tie",
        "yes_sub_title": "Tie after five innings",
        "status": "open",
    }

    class FakeBatchedExactClient:
        def __init__(self) -> None:
            self.ticker_batches: list[list[str]] = []

        def get_market(self, ticker: str):
            raise AssertionError("discovery should use batched ticker lookup, not one request per ticker")

        def get_markets_by_tickers(self, tickers: list[str]):
            self.ticker_batches.append(tickers)
            if "KXMLBF5-26JUL011900SEAPIT-TIE" in tickers:
                return {"markets": [winner_market]}
            return {"markets": []}

        def get_markets_by_event_ticker(self, event_ticker: str):
            if event_ticker == "KXMLBF5-26JUL011900SEAPIT":
                raise AssertionError("winner families should use direct ticker lookup")
            return {"markets": []}

    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="direct-market-response",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
        )
        session.commit()

        fake_client = FakeBatchedExactClient()
        result = market_family_discovery.run_market_family_discovery(
            session,
            date(2026, 7, 1),
            client=fake_client,
        )
        run = session.scalar(select(MarketFamilyDiscoveryRun))
        item = session.scalar(select(MarketFamilyDiscoveryItem))

    assert result["status"] == "completed"
    assert result["markets_found"] == 1
    assert run is not None
    assert run.status == "completed"
    assert item is not None
    assert item.family_key == "first_five_winner"
    assert item.returned_ticker == "KXMLBF5-26JUL011900SEAPIT-TIE"
    assert item.source_strategy == "direct_ticker"
    assert item.candidate_market_ticker == "KXMLBF5-26JUL011900SEAPIT-TIE"
    assert len(fake_client.ticker_batches) < result["attempted_market_tickers_count"]
    assert result["requests_saved_by_batching"] > 0


def test_market_family_discovery_job_returns_nonzero_for_failed_status(monkeypatch) -> None:
    class DummySession:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(market_family_discovery_job, "get_session_factory", lambda: DummySession)
    monkeypatch.setattr(market_family_discovery_job, "_target_date_from_args", lambda: date(2026, 7, 1))
    monkeypatch.setattr(
        market_family_discovery_job,
        "run_market_family_discovery",
        lambda session, target_date: {"status": "failed", "errors": [{"message": "persisted failure"}]},
    )

    assert market_family_discovery_job.main() == 1


def test_market_family_discovery_parses_line_from_ticker_tail_not_date_prefix() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeTickerTailClient:
        def get_market(self, ticker: str):
            raise AssertionError("discovery should use batched ticker lookup, not one request per ticker")

        def get_markets_by_tickers(self, tickers: list[str]):
            return {"markets": []}

        def get_markets_by_event_ticker(self, event_ticker: str):
            if event_ticker != "KXMLBSPREAD-26JUL011900SEAPIT":
                return {"markets": []}
            return {
                "markets": [
                    {
                        "ticker": "KXMLBSPREAD-26JUL011900SEAPIT-PIT-1.5",
                        "event_ticker": event_ticker,
                        "title": "Pittsburgh Pirates spread market vs Seattle Mariners",
                        "rules_primary": "If Pittsburgh covers the spread, Yes wins.",
                        "status": "open",
                    }
                ]
            }

    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="discovery-ticker-tail-line",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
        )
        session.commit()

        result = market_family_discovery.run_market_family_discovery(
            session,
            date(2026, 7, 1),
            client=FakeTickerTailClient(),
        )
        item = session.scalar(select(MarketFamilyDiscoveryItem))

    assert result["markets_found"] == 1
    assert item is not None
    assert item.line_value == Decimal("-1.5000")
    assert item.selection_code == "PIT"
    assert item.line_value != Decimal("26.0000")


def test_market_family_discovery_parses_total_ticker_tail_as_positive_line() -> None:
    assert market_family_discovery._parse_line_value(
        {"ticker": "KXMLBTOTAL-26JUL011900SEAPIT-OVER-8"}
    ) == Decimal("8.0000")
    assert market_family_discovery._parse_line_value(
        {"ticker": "KXMLBF5TOTAL-26JUL011900SEAPIT-UNDER-4.5"}
    ) == Decimal("4.5000")


def test_market_family_discovery_prefers_total_side_from_ticker_before_text() -> None:
    payload = {
        "ticker": "KXMLBTOTAL-26JUL011900SEAPIT-UNDER-8",
        "title": "Total runs market",
        "no_sub_title": "No wins if the game goes over 8 runs",
        "rules_primary": "Over 8 is the opposite side of this contract.",
    }

    assert market_family_discovery._over_under_side(payload) == "under"


def test_market_family_discovery_skips_event_filter_for_exact_found_family() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    exact_market = {
        "ticker": "KXMLBF5-26JUL011900SEAPIT-TIE",
        "event_ticker": "KXMLBF5-26JUL011900SEAPIT",
        "title": "Seattle Mariners vs Pittsburgh Pirates first five innings tie",
        "yes_sub_title": "Tie after five innings",
        "status": "open",
    }

    class FakeExactFoundClient:
        def __init__(self) -> None:
            self.event_tickers: list[str] = []

        def get_market(self, ticker: str):
            raise AssertionError("discovery should use batched ticker lookup, not one request per ticker")

        def get_markets_by_tickers(self, tickers: list[str]):
            if "KXMLBF5-26JUL011900SEAPIT-TIE" in tickers:
                return {"markets": [exact_market]}
            return {"markets": []}

        def get_markets_by_event_ticker(self, event_ticker: str):
            if event_ticker == "KXMLBF5-26JUL011900SEAPIT":
                raise AssertionError("winner families should not use event filter after exact batch finds the family")
            self.event_tickers.append(event_ticker)
            return {"markets": []}

    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="discovery-dedupe-line",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
        )
        session.commit()

        result = market_family_discovery.run_market_family_discovery(
            session,
            date(2026, 7, 1),
            client=FakeExactFoundClient(),
        )
        items = list(session.scalars(select(MarketFamilyDiscoveryItem)))

    assert result["markets_found"] == 1
    assert result["by_family"]["first_five_winner"]["market_count"] == 1
    assert result["by_family"]["first_five_winner"]["event_filter_attempts"] == 0
    assert len(items) == 1
    assert items[0].returned_ticker == exact_market["ticker"]


def test_market_family_discovery_does_not_suppress_fallback_after_discarded_exact_hit() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    mve_market = {
        "ticker": "KXMLBSPREAD-26JUL011900SEAPIT",
        "event_ticker": "KXMLBSPREAD-26JUL011900SEAPIT",
        "title": "Multivariate combo",
        "mve_selected_legs": [{"ticker": "LEG"}],
    }
    fallback_market = {
        "ticker": "KXMLBSPREAD-26JUL011900SEAPIT-PIT-1.5",
        "event_ticker": "KXMLBSPREAD-26JUL011901SEAPIT",
        "title": "Pittsburgh Pirates spread -1.5 vs Seattle Mariners",
        "status": "open",
        "functional_strike": "-1.5",
    }

    class FakeDiscardedExactClient:
        def __init__(self) -> None:
            self.event_tickers: list[str] = []

        def get_market(self, ticker: str):
            raise AssertionError("discovery should use batched ticker lookup, not one request per ticker")

        def get_markets_by_tickers(self, tickers: list[str]):
            return {"markets": []}

        def get_markets_by_event_ticker(self, event_ticker: str):
            self.event_tickers.append(event_ticker)
            if event_ticker == "KXMLBSPREAD-26JUL011900SEAPIT":
                return {"markets": [mve_market]}
            if event_ticker == "KXMLBSPREAD-26JUL011901SEAPIT":
                return {"markets": [fallback_market]}
            return {"markets": []}

    fake_client = FakeDiscardedExactClient()
    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="discarded-exact-hit",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
        )
        session.commit()

        result = market_family_discovery.run_market_family_discovery(
            session,
            date(2026, 7, 1),
            client=fake_client,
        )
        items = list(session.scalars(select(MarketFamilyDiscoveryItem)))

    assert "KXMLBSPREAD-26JUL011900SEAPIT" in fake_client.event_tickers
    assert result["markets_found"] == 1
    assert result["warnings"][0]["message"] == "MULTIVARIATE_MARKET_EXCLUDED"
    assert len(items) == 1
    assert items[0].returned_ticker == fallback_market["ticker"]


def test_market_family_discovery_uses_observed_prefix_registry_only() -> None:
    game = MlbGame(
        external_game_id="registry-prefixes",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="scheduled",
    )

    expected_prefixes = {
        "full_game_winner": "KXMLBGAME",
        "full_game_spread": "KXMLBSPREAD",
        "full_game_total": "KXMLBTOTAL",
        "first_five_winner": "KXMLBF5",
        "first_five_spread": "KXMLBF5SPREAD",
        "first_five_total": "KXMLBF5TOTAL",
    }
    expected_statuses = {
        "full_game_winner": "supported_targeted_current",
        "full_game_spread": "deterministic_observed_pending_validation",
        "full_game_total": "deterministic_observed_pending_validation",
        "first_five_winner": "deterministic_observed_pending_validation",
        "first_five_spread": "deterministic_observed_pending_validation",
        "first_five_total": "deterministic_observed_pending_validation",
    }
    active_prefixes = {
        prefix
        for definition in market_family_discovery.FULL_REGISTRY.values()
        for prefix in definition["candidate_series_tickers"]
    }

    for family_key, prefix in expected_prefixes.items():
        assert market_family_discovery.FULL_REGISTRY[family_key]["candidate_series_tickers"] == [prefix]
        assert market_family_discovery.FULL_REGISTRY[family_key]["status"] == expected_statuses[family_key]
        assert (prefix, f"{prefix}-26JUL011900SEAPIT") in market_family_discovery._event_ticker_candidates(
            game,
            family_key,
        )

    assert "KXMLBTEAMTOTAL" not in active_prefixes
    assert set(market_family_discovery.RETIRED_LEGACY_PREFIXES_NOT_USED).isdisjoint(active_prefixes)
    assert market_family_discovery.DISCOVERY_QUERY_FAMILIES == [
        "full_game_winner",
        "full_game_spread",
        "full_game_total",
        "first_five_winner",
        "first_five_spread",
        "first_five_total",
    ]
    assert market_family_discovery._direct_market_ticker_candidates(
        game,
        "full_game_winner",
        "KXMLBGAME-26JUL011900SEAPIT",
    ) == [
        "KXMLBGAME-26JUL011900SEAPIT-SEA",
        "KXMLBGAME-26JUL011900SEAPIT-PIT",
    ]
    assert market_family_discovery._direct_market_ticker_candidates(
        game,
        "first_five_winner",
        "KXMLBF5-26JUL011900SEAPIT",
    ) == [
        "KXMLBF5-26JUL011900SEAPIT-SEA",
        "KXMLBF5-26JUL011900SEAPIT-PIT",
        "KXMLBF5-26JUL011900SEAPIT-TIE",
    ]
    assert market_family_discovery._direct_market_ticker_candidates(
        game,
        "full_game_spread",
        "KXMLBSPREAD-26JUL011900SEAPIT",
    ) == []


def test_market_family_discovery_first_five_winner_can_represent_tie() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeFirstFiveTieClient:
        def get_market(self, ticker: str):
            raise AssertionError("discovery should use batched ticker lookup, not one request per ticker")

        def get_markets_by_tickers(self, tickers: list[str]):
            if "KXMLBF5-26JUL011900SEAPIT-TIE" not in tickers:
                return {"markets": []}
            return {
                "markets": [
                    {
                        "ticker": "KXMLBF5-26JUL011900SEAPIT-TIE",
                        "event_ticker": "KXMLBF5-26JUL011900SEAPIT",
                        "title": "Seattle Mariners vs Pittsburgh Pirates first five innings tie",
                        "yes_sub_title": "Tie after five innings",
                        "rules_primary": "If the score is tied after five innings, Yes wins.",
                        "status": "open",
                    }
                ]
            }

        def get_markets_by_event_ticker(self, event_ticker: str):
            if event_ticker == "KXMLBF5-26JUL011900SEAPIT":
                raise AssertionError("event filter should not run after exact batch finds first-five tie")
            return {"markets": []}

    with Session(engine) as session:
        session.add(
            MlbGame(
                external_game_id="first-five-tie",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
        )
        session.commit()

        result = market_family_discovery.run_market_family_discovery(
            session,
            date(2026, 7, 1),
            client=FakeFirstFiveTieClient(),
        )
        item = session.scalar(select(MarketFamilyDiscoveryItem))

    assert result["markets_found"] == 1
    assert item is not None
    assert item.family_key == "first_five_winner"
    assert item.selection_code == "TIE"
    assert item.raw_payload["pr3a_classification"]["has_multiple_child_outcomes"] is True


def test_market_family_discovery_report_uses_latest_finalized_run() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        completed_run = MarketFamilyDiscoveryRun(
            target_date=date(2026, 7, 1),
            started_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 7, 1, 12, 1, tzinfo=UTC),
            status="completed",
            games_considered=1,
            families_considered=6,
            markets_found=0,
            errors=[],
            warnings=[],
            raw_summary={"resolver_mode": "deterministic_ticker_registry_v1", "by_family": {}},
        )
        stale_running_run = MarketFamilyDiscoveryRun(
            target_date=date(2026, 7, 1),
            started_at=datetime(2026, 7, 1, 12, 5, tzinfo=UTC),
            status="running",
            games_considered=1,
            families_considered=6,
            markets_found=0,
            errors=[],
            warnings=[],
            raw_summary={},
        )
        session.add_all([completed_run, stale_running_run])
        session.commit()

        result = market_family_discovery.latest_market_family_discovery(session, date(2026, 7, 1))

    assert result["run"]["run_id"] == completed_run.id
    assert result["run"]["status"] == "completed"
    assert result["resolver_mode"] == "deterministic_ticker_registry_v1"


def test_market_family_mapping_sync_promotes_only_parseable_supported_families() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="mapping-sync-pr3b",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        run = MarketFamilyDiscoveryRun(
            target_date=date(2026, 7, 1),
            started_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 7, 1, 12, 1, tzinfo=UTC),
            status="partial_rate_limited",
            games_considered=1,
            families_considered=6,
            markets_found=5,
            errors=[],
            warnings=[],
            raw_summary={},
        )
        session.add_all([game, run])
        session.flush()
        session.add_all(
            [
                MarketFamilyDiscoveryItem(
                    run_id=run.id,
                    mlb_game_id=game.id,
                    family_key="full_game_spread",
                    returned_ticker="KXMLBSPREAD-26JUL011900SEAPIT-PIT-1.5",
                    returned_event_ticker="KXMLBSPREAD-26JUL011900SEAPIT",
                title="Pittsburgh Pirates spread -1.5 vs Seattle Mariners",
                yes_sub_title="Pittsburgh -1.5",
                no_sub_title="Seattle +1.5",
                rules_primary="If Pittsburgh wins by more than 1.5 runs, this market resolves to Yes.",
                raw_status="open",
                confidence=Decimal("0.9500"),
                line_value=Decimal("-1.5000"),
                    selection_code="PIT",
                ),
                MarketFamilyDiscoveryItem(
                    run_id=run.id,
                    mlb_game_id=game.id,
                    family_key="first_five_winner",
                    returned_ticker="KXMLBF5-26JUL011900SEAPIT-TIE",
                    returned_event_ticker="KXMLBF5-26JUL011900SEAPIT",
                    title="Seattle Mariners vs Pittsburgh Pirates first five tie",
                    raw_status="open",
                    confidence=Decimal("0.9500"),
                    selection_code="TIE",
                ),
                MarketFamilyDiscoveryItem(
                    run_id=run.id,
                    mlb_game_id=game.id,
                    family_key="full_game_total",
                    returned_ticker="KXMLBTOTAL-26JUL011900SEAPIT-OVER-8",
                    returned_event_ticker="KXMLBTOTAL-26JUL011900SEAPIT",
                    title="Pittsburgh Pirates and Seattle Mariners total",
                    raw_status="open",
                    confidence=Decimal("0.9500"),
                    line_value=Decimal("-8.0000"),
                ),
                MarketFamilyDiscoveryItem(
                    run_id=run.id,
                    mlb_game_id=game.id,
                    family_key="full_game_total",
                    returned_ticker="KXMLBTEAMTOTAL-26JUL011900SEAPIT-PIT-3.5",
                    title="Pittsburgh team total",
                    raw_status="open",
                ),
                MarketFamilyDiscoveryItem(
                    run_id=run.id,
                    mlb_game_id=game.id,
                    family_key="full_game_spread",
                    returned_ticker="KXMLBSPREAD-26JUL011900SEAPIT-MVE",
                    title="Multivariate spread combo",
                    raw_payload={
                        "ticker": "KXMLBSPREAD-26JUL011900SEAPIT-MVE",
                        "title": "Multivariate spread combo",
                        "mve_selected_legs": [{"ticker": "LEG"}],
                    },
                ),
            ]
        )
        session.commit()

        result = market_family_mapping.sync_market_family_mappings(session, date(2026, 7, 1))
        mappings = list(session.scalars(select(MarketMapping).order_by(MarketMapping.market_family)))
        markets = list(session.scalars(select(KalshiMarket).order_by(KalshiMarket.market_family)))

    assert result["items_seen"] == 5
    assert result["paper_supported"] == 3
    assert result["unsupported"] == 2
    assert result["mappings_created_or_updated"] == 3
    assert [mapping.market_family for mapping in mappings] == [
        "first_five_winner",
        "full_game_spread",
        "full_game_total",
    ]
    assert {mapping.settlement_rule_status for mapping in mappings} == {"paper_supported"}
    assert next(mapping for mapping in mappings if mapping.market_family == "full_game_spread").line_value == Decimal(
        "-1.5000"
    )
    assert next(mapping for mapping in mappings if mapping.market_family == "full_game_total").line_value == Decimal(
        "8.0000"
    )
    assert next(mapping for mapping in mappings if mapping.market_family == "full_game_total").over_under_side == "over"
    assert next(mapping for mapping in mappings if mapping.market_family == "first_five_winner").selection_code == "TIE"
    assert {market.ticker for market in markets} == {
        "KXMLBF5-26JUL011900SEAPIT-TIE",
        "KXMLBSPREAD-26JUL011900SEAPIT-PIT-1.5",
        "KXMLBTOTAL-26JUL011900SEAPIT-OVER-8",
    }


def test_market_family_mapping_parses_event_level_spread_selection_from_yes_text() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="event-level-spread",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        run = MarketFamilyDiscoveryRun(
            target_date=date(2026, 7, 1),
            started_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 7, 1, 12, 1, tzinfo=UTC),
            status="completed",
            games_considered=1,
            families_considered=6,
            markets_found=1,
            errors=[],
            warnings=[],
            raw_summary={},
        )
        session.add_all([game, run])
        session.flush()
        session.add(
            MarketFamilyDiscoveryItem(
                run_id=run.id,
                mlb_game_id=game.id,
                family_key="full_game_spread",
                returned_ticker="KXMLBSPREAD-26JUL011900SEAPIT",
                returned_event_ticker="KXMLBSPREAD-26JUL011900SEAPIT",
                title="Seattle Mariners vs Pittsburgh Pirates run line",
                yes_sub_title="Pittsburgh -1.5",
                no_sub_title="Seattle +1.5",
                rules_primary="If Pittsburgh wins by more than 1.5 runs, this market resolves to Yes.",
                raw_status="open",
                confidence=Decimal("0.9500"),
                line_value=Decimal("-1.5000"),
            )
        )
        session.commit()

        result = market_family_mapping.sync_market_family_mappings(session, date(2026, 7, 1))
        mapping = session.scalar(select(MarketMapping))
        market = session.scalar(select(KalshiMarket))

    assert result["paper_supported"] == 1
    assert mapping is not None
    assert mapping.mapping_status == "confirmed"
    assert mapping.settlement_rule_status == "paper_supported"
    assert mapping.selection_code == "PIT"
    assert mapping.mapping_metadata["selection_display"] == "PIT -1.5"
    assert mapping.mapping_metadata["contract_display"] == "SEA @ PIT - FULL GAME SPREAD - PIT -1.5"
    assert mapping.mapping_metadata["spread_verification"]["verified"] is True
    assert market is not None
    assert market.selection_code == "PIT"


@pytest.mark.parametrize(
    ("threshold", "expected_line"),
    [
        ("1.5", Decimal("-1.5000")),
        ("2.5", Decimal("-2.5000")),
        ("3.5", Decimal("-3.5000")),
    ],
)
def test_full_game_spread_rules_wins_by_more_than_normalizes_to_lay_line(
    threshold: str,
    expected_line: Decimal,
) -> None:
    game = MlbGame(
        external_game_id=f"rules-spread-{threshold}",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="scheduled",
    )

    verification = verify_spread_market(
        game=game,
        family_key="full_game_spread",
        raw={
            "ticker": f"KXMLBSPREAD-26JUL011900SEAPIT-PIT{int(Decimal(threshold) + Decimal('0.5'))}",
            "title": "Seattle Mariners vs Pittsburgh Pirates run line",
            "yes_sub_title": f"If Pittsburgh wins by more than {threshold} runs, this market resolves to Yes.",
            "no_sub_title": f"If Pittsburgh wins by more than {threshold} runs, this market resolves to Yes.",
            "rules_primary": f"If Pittsburgh wins by more than {threshold} runs, this market resolves to Yes.",
        },
    )

    assert verification.verified is True
    assert verification.audit_status == "trusted_audit_only"
    assert verification.parse_source == "rules_text"
    assert verification.condition_type == "team_wins_by_more_than"
    assert verification.selection_code == "PIT"
    assert verification.threshold_runs == Decimal(threshold).quantize(Decimal("0.0001"))
    assert verification.line_value == expected_line
    assert verification.display_spread_line == expected_line
    assert verification.line_direction == "selected_team_lays_runs"
    assert verification.actual_contract_display == f"YES ON PITTSBURGH PIRATES {float(expected_line):g} FULL GAME"
    assert verification.no_contract_display == f"NO ON PITTSBURGH PIRATES {float(expected_line):g} FULL GAME"
    assert verification.normalized_no_equivalent_display == (
        f"SEATTLE MARINERS +{float(abs(expected_line)):g} FULL GAME EQUIVALENT"
    )
    assert verification.yes_interpretation == f"PITTSBURGH PIRATES {float(expected_line):g} COVERS FULL GAME"
    assert verification.no_interpretation == (
        f"PITTSBURGH PIRATES {float(expected_line):g} DOES NOT COVER FULL GAME"
    )
    assert verification.no_text_source == "duplicated_yes_text"
    assert verification.no_complement_source == "binary_market_complement"
    assert verification.no_complement_confidence == "high"
    assert verification.no_is_true_complement is True
    assert verification.push_possible is False
    assert verification.push_rule_verified is True
    assert verification.push_condition == "not_applicable_half_run_line"
    assert verification.settlement_formula == f"selected_team_runs - opponent_runs > {threshold}"
    assert "SPREAD_LINE_NOT_VERIFIED_FROM_KALSHI_TEXT" not in verification.warnings
    assert "rules_text_spread_condition_verified" in verification.reason_codes
    assert "binary_yes_no_complement_verified" in verification.reason_codes
    assert "half_run_no_push_verified" in verification.reason_codes


def test_full_game_spread_parses_secondary_rules_when_primary_is_generic() -> None:
    game = MlbGame(
        external_game_id="rules-spread-secondary",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="scheduled",
    )

    verification = verify_spread_market(
        game=game,
        family_key="full_game_spread",
        raw={
            "ticker": "KXMLBSPREAD-26JUL011900SEAPIT-PIT2",
            "title": "Seattle Mariners vs Pittsburgh Pirates run line",
            "yes_sub_title": "Pittsburgh -1.5",
            "no_sub_title": "Seattle +1.5",
            "rules_primary": "This market is based on the final score of the listed game.",
            "rules_secondary": "If Pittsburgh wins by more than 1.5 runs, this market resolves to Yes.",
        },
    )

    assert verification.verified is True
    assert verification.audit_status == "trusted_audit_only"
    assert verification.parse_source == "rules_text"
    assert verification.line_value == Decimal("-1.5000")
    assert verification.selection_code == "PIT"
    assert "rules_text_unparseable" not in verification.reason_codes
    assert "rules_text_spread_condition_verified" in verification.reason_codes
    assert "This market is based on the final score" in str(verification.raw_contract_text["rules"])
    assert "Pittsburgh wins by more than 1.5 runs" in str(verification.raw_contract_text["rules"])


def test_full_game_spread_rules_or_more_runs_normalizes_to_half_run_line() -> None:
    game = MlbGame(
        external_game_id="rules-spread-or-more",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="scheduled",
    )

    verification = verify_spread_market(
        game=game,
        family_key="full_game_spread",
        raw={
            "ticker": "KXMLBSPREAD-26JUL011900SEAPIT-PIT2",
            "title": "Seattle Mariners vs Pittsburgh Pirates run line",
            "yes_sub_title": "Pittsburgh -1.5",
            "no_sub_title": "Seattle +1.5",
            "rules_primary": "If Pittsburgh wins by 2 or more runs, Yes wins.",
            "functional_strike": "-1.5",
        },
    )

    assert verification.verified is True
    assert verification.audit_status == "trusted_audit_only"
    assert verification.line_value == Decimal("-1.5000")
    assert verification.threshold_runs == Decimal("1.5000")
    assert verification.raw_threshold_runs == Decimal("2.0000")
    assert verification.settlement_formula == "selected_team_runs - opponent_runs > 1.5"
    assert "rules_text_unparseable" not in verification.reason_codes
    assert "rules_text_spread_condition_verified" in verification.reason_codes


def test_full_game_spread_formula_preserves_whole_number_threshold_digits() -> None:
    game = MlbGame(
        external_game_id="rules-spread-whole-threshold",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="scheduled",
    )

    verification = verify_spread_market(
        game=game,
        family_key="full_game_spread",
        raw={
            "ticker": "KXMLBSPREAD-26JUL011900SEAPIT-PIT10",
            "title": "Seattle Mariners vs Pittsburgh Pirates run line",
            "yes_sub_title": "Pittsburgh wins by more than 10 runs",
            "no_sub_title": "Pittsburgh wins by more than 10 runs",
            "rules_primary": "If Pittsburgh wins by more than 10 runs, this market resolves to Yes.",
        },
    )

    assert verification.line_value == Decimal("-10.0000")
    assert verification.threshold_runs == Decimal("10.0000")
    assert verification.settlement_formula == "selected_team_runs - opponent_runs > 10"
    assert verification.settlement_formula != "selected_team_runs - opponent_runs > 1"


def test_full_game_spread_explicit_contradictory_no_text_remains_unsafe() -> None:
    game = MlbGame(
        external_game_id="rules-spread-conflicting-no",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="scheduled",
    )

    verification = verify_spread_market(
        game=game,
        family_key="full_game_spread",
        raw={
            "ticker": "KXMLBSPREAD-26JUL011900SEAPIT-PIT2",
            "yes_sub_title": "Pittsburgh wins by more than 1.5 runs",
            "no_sub_title": "Seattle -1.5",
            "rules_primary": "If Pittsburgh wins by more than 1.5 runs, this market resolves to Yes.",
        },
    )

    assert verification.verified is False
    assert verification.audit_status == "unsafe"
    assert verification.no_text_source == "explicit_no_text"
    assert verification.no_is_true_complement is False
    assert "explicit_no_text_conflicts_with_binary_complement" in verification.reason_codes
    assert "no_contract_text_conflicts_with_expected_complement" in verification.reason_codes


def test_full_game_spread_title_line_conflict_blocks_trusted_audit() -> None:
    game = MlbGame(
        external_game_id="rules-spread-conflicting-title",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="scheduled",
    )

    verification = verify_spread_market(
        game=game,
        family_key="full_game_spread",
        raw={
            "ticker": "KXMLBSPREAD-26JUL011900SEAPIT-PIT2",
            "title": "Pittsburgh Pirates +1.5 run line",
            "yes_sub_title": "If Pittsburgh wins by more than 1.5 runs, this market resolves to Yes.",
            "no_sub_title": "If Pittsburgh wins by more than 1.5 runs, this market resolves to Yes.",
            "rules_primary": "If Pittsburgh wins by more than 1.5 runs, this market resolves to Yes.",
        },
    )

    assert verification.verified is False
    assert verification.line_value == Decimal("-1.5000")
    assert verification.audit_status == "ambiguous_line_direction"
    assert "subtitle_rules_line_conflict" in verification.reason_codes
    assert verification.actual_contract_display == "YES ON PITTSBURGH PIRATES -1.5 FULL GAME"


def test_full_game_spread_integer_line_requires_verified_push_behavior() -> None:
    game = MlbGame(
        external_game_id="rules-spread-integer",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="scheduled",
    )

    verification = verify_spread_market(
        game=game,
        family_key="full_game_spread",
        raw={
            "ticker": "KXMLBSPREAD-26JUL011900SEAPIT-PIT1",
            "yes_sub_title": "Pittsburgh wins by more than 1 run",
            "no_sub_title": "Pittsburgh wins by more than 1 run",
            "rules_primary": "If Pittsburgh wins by more than 1 run, this market resolves to Yes.",
        },
    )

    assert verification.line_value == Decimal("-1.0000")
    assert verification.push_possible is True
    assert verification.push_rule_verified is False
    assert verification.audit_status == "push_behavior_uncertain"
    assert "integer_push_rule_unverified" in verification.reason_codes


def test_market_family_mapping_preserves_first_five_spread_verification() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="first-five-spread-verified",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        run = MarketFamilyDiscoveryRun(
            target_date=date(2026, 7, 1),
            started_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 7, 1, 12, 1, tzinfo=UTC),
            status="completed",
            games_considered=1,
            families_considered=6,
            markets_found=1,
            errors=[],
            warnings=[],
            raw_summary={},
        )
        session.add_all([game, run])
        session.flush()
        session.add(
            MarketFamilyDiscoveryItem(
                run_id=run.id,
                mlb_game_id=game.id,
                family_key="first_five_spread",
                returned_ticker="KXMLBF5SPREAD-26JUL011900SEAPIT",
                returned_event_ticker="KXMLBF5SPREAD-26JUL011900SEAPIT",
                title="Seattle Mariners vs Pittsburgh Pirates first five run line",
                yes_sub_title="Pittsburgh -0.5 first 5 innings",
                no_sub_title="Seattle +0.5 first 5 innings",
                rules_primary="The market is based on the score after the first 5 innings.",
                raw_status="open",
                confidence=Decimal("0.9500"),
                line_value=Decimal("-0.5000"),
            )
        )
        session.commit()

        result = market_family_mapping.sync_market_family_mappings(session, date(2026, 7, 1))
        mapping = session.scalar(select(MarketMapping))

    assert result["paper_supported"] == 1
    assert mapping is not None
    assert mapping.mapping_status == "confirmed"
    assert mapping.settlement_rule_status == "paper_supported"
    assert mapping.market_family == "first_five_spread"
    assert mapping.selection_code == "PIT"
    assert mapping.line_value == Decimal("-0.5000")
    assert mapping.mapping_metadata["spread_verification"]["verified"] is True
    assert mapping.mapping_metadata["spread_verification"]["audit_status"] == "trusted_audit_only"
    assert mapping.mapping_metadata["spread_verification"]["inning_scope"] == "first_five"


def test_market_family_mapping_keeps_spread_without_rules_needs_review() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="event-level-spread-no-rules",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        run = MarketFamilyDiscoveryRun(
            target_date=date(2026, 7, 1),
            started_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 7, 1, 12, 1, tzinfo=UTC),
            status="completed",
            games_considered=1,
            families_considered=6,
            markets_found=1,
            errors=[],
            warnings=[],
            raw_summary={},
        )
        session.add_all([game, run])
        session.flush()
        session.add(
            MarketFamilyDiscoveryItem(
                run_id=run.id,
                mlb_game_id=game.id,
                family_key="full_game_spread",
                returned_ticker="KXMLBSPREAD-26JUL011900SEAPIT",
                returned_event_ticker="KXMLBSPREAD-26JUL011900SEAPIT",
                title="Seattle Mariners vs Pittsburgh Pirates run line",
                yes_sub_title="Pittsburgh -1.5",
                no_sub_title="Seattle +1.5",
                raw_status="open",
                confidence=Decimal("0.9500"),
                line_value=Decimal("-1.5000"),
            )
        )
        session.commit()

        result = market_family_mapping.sync_market_family_mappings(session, date(2026, 7, 1))
        mapping = session.scalar(select(MarketMapping))

    assert result["paper_supported"] == 0
    assert result["needs_review"] == 1
    assert mapping is not None
    assert mapping.mapping_status == "needs_review"
    assert mapping.settlement_rule_status == "needs_review"
    assert mapping.mapping_metadata["spread_verification"]["audit_status"] == "settlement_text_unverified"
    assert "settlement_text_missing" in mapping.mapping_metadata["spread_verification"]["reason_codes"]


def test_new_market_families_require_paper_supported_metadata_for_trades(monkeypatch) -> None:
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="spread-no-trade-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-SPREAD-NO-TRADE",
            ticker="KXMLBSPREAD-26JUL011900SEAPIT-PIT-1.5",
            title="Pittsburgh Pirates spread -1.5 vs Seattle Mariners",
            status="open",
            occurrence_datetime=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
        )
        session.add_all([game, market])
        _add_candidate_mapping(session, game, market)
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))
        trade = session.scalar(select(PaperTrade))

    assert result["candidates"] == 1
    assert result["paper_trades"] == 0
    assert candidate is not None
    assert candidate.market_type == "full_game_spread"
    assert candidate.decision == "no_trade_missing_line"
    assert candidate.training_eligible is False
    assert candidate.model_version_tag == modeling.MATURE_MODEL_TAG
    assert candidate.feature_version == features.FEATURE_VERSION
    assert trade is None


def test_paper_supported_market_family_can_create_pre_model_paper_trade(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="spread-paper-supported-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-SPREAD-PAPER-SUPPORTED",
            ticker="KXMLBSPREAD-26JUL011900SEAPIT-PIT-1.5",
            title="Pittsburgh Pirates spread -1.5 vs Seattle Mariners",
            status="open",
            occurrence_datetime=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
            market_family="full_game_spread",
            market_type="full_game_spread",
            line_value=Decimal("-1.5000"),
            selection_code="PIT",
            inning_scope="full_game",
            settlement_rule_status="paper_supported",
        )
        session.add_all([game, market])
        _add_candidate_mapping(
            session,
            game,
            market,
            mapping_status="confirmed",
            market_family="full_game_spread",
            market_type="full_game_spread",
            line_value=Decimal("-1.5000"),
            selection_code="PIT",
            inning_scope="full_game",
            settlement_rule_status="paper_supported",
        )
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))
        feature_snapshot = session.scalar(select(FeatureSnapshot))
        trade = session.scalar(select(PaperTrade))

    assert result["candidates"] == 1
    assert result["paper_trades"] == 0
    assert candidate is not None
    assert candidate.market_type == "full_game_spread"
    assert candidate.model_probability != Decimal("0.500000")
    assert candidate.model_version_tag == modeling.MATURE_MODEL_TAG
    assert candidate.training_eligible is False
    assert candidate.training_exclusion_reason == "full_game_spread_audit_only"
    assert candidate.decision == "no_trade_full_game_spread_audit_only"
    assert feature_snapshot is not None
    assert feature_snapshot.source == features.FEATURE_VERSION
    assert trade is None


def test_event_level_spread_with_parsed_selection_can_create_paper_trade(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="event-spread-paper-supported-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-EVENT-SPREAD-PAPER-SUPPORTED",
            ticker="KXMLBSPREAD-26JUL011900SEAPIT",
            title="Seattle Mariners vs Pittsburgh Pirates run line",
            yes_subtitle="Pittsburgh -1.5",
            status="open",
            occurrence_datetime=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            implied_yes_ask=Decimal("0.4000"),
            market_family="full_game_spread",
            market_type="full_game_spread",
            line_value=Decimal("-1.5000"),
            inning_scope="full_game",
            settlement_rule_status="paper_supported",
        )
        session.add_all([game, market])
        _add_candidate_mapping(
            session,
            game,
            market,
            mapping_status="confirmed",
            market_family="full_game_spread",
            market_type="full_game_spread",
            line_value=Decimal("-1.5000"),
            selection_code="PIT",
            inning_scope="full_game",
            settlement_rule_status="paper_supported",
        )
        session.commit()

        result = candidates.generate_candidates(session)
        candidate = session.scalar(select(ModelCandidate))
        trade = session.scalar(select(PaperTrade))

    assert result["paper_trades"] == 0
    assert candidate is not None
    assert candidate.decision == "no_trade_full_game_spread_audit_only"
    assert candidate.training_eligible is False
    assert candidate.training_exclusion_reason == "full_game_spread_audit_only"
    assert candidate.selection_code == "PIT"
    assert candidate.selection_display == "PIT -1.5"
    assert trade is None


def test_open_position_price_refresh_updates_only_open_paper_trades() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeOrderbookClient:
        def get_orderbook(self, ticker: str):
            return {"orderbook": {"yes": [[44, 10]], "no": [[55, 20]]}}

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        market = KalshiMarket(
            kalshi_market_id="KX-REFRESH-OPEN",
            ticker="KXMLBGAME-26JUL011900SEAPIT-PIT",
            title="Will Pittsburgh win?",
            status="open",
        )
        session.add(market)
        session.flush()
        candidate = ModelCandidate(
            paper_trading_epoch_id=epoch_id,
            kalshi_market_id=market.id,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="paper_trade",
            market_type="full_game_winner",
        )
        session.add(candidate)
        session.flush()
        open_trade = PaperTrade(
            paper_trading_epoch_id=epoch_id,
            candidate_id=candidate.id,
            market_ticker=market.ticker,
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            entry_time=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            status="open",
        )
        legacy_position = Position(
            kalshi_market_id=market.id,
            market_ticker=market.ticker,
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            opened_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            status="open",
        )
        settled_trade = PaperTrade(
            market_ticker="KXMLBGAME-SETTLED-PIT",
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("1.0000"),
            quantity=1,
            entry_time=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            status="settled",
        )
        session.add_all([open_trade, legacy_position, settled_trade])
        session.commit()

        result = position_refresh.refresh_open_position_prices(session, client=FakeOrderbookClient())
        session.refresh(open_trade)
        session.refresh(legacy_position)
        session.refresh(settled_trade)
        snapshot = session.scalar(select(BalanceSnapshot))
        summary = dashboard.dashboard_summary_from_db(session, include_pre_observation=True)

    assert result["checked"] == 1
    assert result["updated"] == 1
    assert open_trade.current_price == Decimal("0.4400")
    assert open_trade.current_price_updated_at is not None
    assert legacy_position.current_price == Decimal("0.4400")
    assert summary.positions[0].current_price == 0.44
    assert settled_trade.current_price == Decimal("1.0000")
    assert snapshot is not None
    assert snapshot.source == "paper"
    assert snapshot.snapshot_type == "open_position_price_refresh"


def test_open_position_price_refresh_falls_back_to_orderbook_when_batch_quote_missing() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakePartialBatchClient:
        request_count = 0
        rate_limited_count = 0

        def get_markets_by_tickers(self, tickers: list[str]):
            self.request_count += 1
            return {"markets": []}

        def get_orderbook(self, ticker: str):
            return {"orderbook": {"yes": [[47, 10]], "no": [[52, 20]]}}

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        market = KalshiMarket(
            kalshi_market_id="KX-REFRESH-BATCH-MISS",
            ticker="KXMLBGAME-26JUL011900SEAPIT-PIT",
            title="Will Pittsburgh win?",
            status="open",
        )
        session.add(market)
        session.flush()
        candidate = ModelCandidate(
            paper_trading_epoch_id=epoch_id,
            kalshi_market_id=market.id,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="paper_trade",
            market_type="full_game_winner",
        )
        session.add(candidate)
        session.flush()
        open_trade = PaperTrade(
            paper_trading_epoch_id=epoch_id,
            candidate_id=candidate.id,
            market_ticker=market.ticker,
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            entry_time=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            status="open",
        )
        session.add(open_trade)
        session.commit()

        result = position_refresh.refresh_open_position_prices(session, client=FakePartialBatchClient())
        session.refresh(open_trade)

    assert result["checked"] == 1
    assert result["updated"] == 1
    assert result["request_counters"]["market_batch_requests"] == 1
    assert result["request_counters"]["orderbook_requests"] == 1
    assert open_trade.current_price == Decimal("0.4700")


def test_open_position_price_refresh_complements_last_price_for_no_side_batch_mark() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeLastPriceClient:
        request_count = 0
        rate_limited_count = 0

        def get_markets_by_tickers(self, tickers: list[str]):
            self.request_count += 1
            return {
                "markets": [
                    {
                        "ticker": "KXMLBGAME-26JUL011900SEAPIT-PIT",
                        "id": "KX-REFRESH-NO-LAST",
                        "title": "Will Pittsburgh win?",
                        "last_price_dollars": "0.7000",
                    }
                ]
            }

        def get_orderbook(self, ticker: str):
            raise AssertionError("last price complement should avoid orderbook fallback")

    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        market = KalshiMarket(
            kalshi_market_id="KX-REFRESH-NO-LAST",
            ticker="KXMLBGAME-26JUL011900SEAPIT-PIT",
            title="Will Pittsburgh win?",
            status="open",
        )
        session.add(market)
        session.flush()
        candidate = ModelCandidate(
            paper_trading_epoch_id=epoch_id,
            kalshi_market_id=market.id,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="paper_trade",
            market_type="full_game_winner",
        )
        session.add(candidate)
        session.flush()
        open_trade = PaperTrade(
            paper_trading_epoch_id=epoch_id,
            candidate_id=candidate.id,
            market_ticker=market.ticker,
            contract_side="no",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            quantity=1,
            entry_time=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            status="open",
        )
        session.add(open_trade)
        session.commit()

        result = position_refresh.refresh_open_position_prices(session, client=FakeLastPriceClient())
        session.refresh(open_trade)

    assert result["checked"] == 1
    assert result["updated"] == 1
    assert result["request_counters"]["market_batch_requests"] == 1
    assert result["request_counters"]["orderbook_requests"] == 0
    assert open_trade.current_price == Decimal("0.3000")


def test_open_position_price_refresh_skips_settlement_ready_first_five_trade() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeClosedF5Client:
        request_count = 0
        rate_limited_count = 0

        def get_markets_by_tickers(self, tickers: list[str]):
            self.request_count += 1
            return {
                "markets": [
                    {
                        "ticker": "KXMLBF5TOTAL-26JUL011510CINMIL-OVER-3.5",
                        "id": "KX-F5-READY",
                        "title": "First five total",
                        "status": "closed",
                        "yes_bid_dollars": "0.9900",
                    }
                ]
            }

        def get_orderbook(self, ticker: str):
            raise AssertionError("settlement-ready F5 marks should not fall back to orderbook")

    old_mark_time = datetime(2026, 7, 1, 19, 0, tzinfo=UTC)
    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        game = MlbGame(
            external_game_id="refresh-f5-settlement-ready",
            home_team="Milwaukee Brewers",
            away_team="Cincinnati Reds",
            home_abbreviation="MIL",
            away_abbreviation="CIN",
            scheduled_start=datetime(2026, 7, 1, 19, 10, tzinfo=UTC),
            status="In Progress",
            raw_payload=_linescore_payload([(1, 0), (0, 1), (1, 0), (0, 0), (1, 1)]),
        )
        session.add(game)
        session.flush()
        trade, _candidate, market, _position = _add_settlement_trade(
            session,
            epoch_id=epoch_id,
            game=game,
            ticker="KXMLBF5TOTAL-26JUL011510CINMIL-OVER-3.5",
            family="first_five_total",
            line=Decimal("3.5000"),
            total_side="over",
            inning_scope="first_five",
            current_price=Decimal("0.4500"),
            market_status="open",
        )
        trade.current_price_updated_at = old_mark_time
        session.commit()

        result = position_refresh.refresh_open_position_prices(session, client=FakeClosedF5Client())
        session.refresh(trade)
        session.refresh(market)

    assert result["checked"] == 1
    assert result["updated"] == 0
    assert result["skipped"] == 1
    assert result["skipped_first_five_settlement_ready"] == 1
    assert result["skipped_closed_f5_market"] == 0
    assert trade.current_price == Decimal("0.4500")
    assert trade.current_price_updated_at == old_mark_time.replace(tzinfo=None)
    assert market.status == "open"


def test_open_position_price_refresh_skips_closed_first_five_market_before_linescore_complete() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeClosedF5Client:
        request_count = 0
        rate_limited_count = 0

        def get_markets_by_tickers(self, tickers: list[str]):
            self.request_count += 1
            return {
                "markets": [
                    {
                        "ticker": "KXMLBF5TOTAL-26JUL011510CINMIL-OVER-3.5",
                        "id": "KX-F5-CLOSED-BEFORE-LINESCORE",
                        "title": "First five total",
                        "status": "closed",
                        "yes_bid_dollars": "0.9900",
                    }
                ]
            }

        def get_orderbook(self, ticker: str):
            raise AssertionError("closed F5 batch quote should not fall back to orderbook")

    old_mark_time = datetime(2026, 7, 1, 19, 0, tzinfo=UTC)
    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        game = MlbGame(
            external_game_id="refresh-f5-closed-before-linescore",
            home_team="Milwaukee Brewers",
            away_team="Cincinnati Reds",
            home_abbreviation="MIL",
            away_abbreviation="CIN",
            scheduled_start=datetime(2026, 7, 1, 19, 10, tzinfo=UTC),
            status="In Progress",
            raw_payload=_linescore_payload([(1, 0), (0, 1), (1, 0), (0, 0)]),
        )
        session.add(game)
        session.flush()
        trade, _candidate, market, _position = _add_settlement_trade(
            session,
            epoch_id=epoch_id,
            game=game,
            ticker="KXMLBF5TOTAL-26JUL011510CINMIL-OVER-3.5",
            family="first_five_total",
            line=Decimal("3.5000"),
            total_side="over",
            inning_scope="first_five",
            current_price=Decimal("0.4500"),
            market_status="open",
        )
        trade.current_price_updated_at = old_mark_time
        session.commit()

        result = position_refresh.refresh_open_position_prices(session, client=FakeClosedF5Client())
        session.refresh(trade)
        session.refresh(market)

    assert result["checked"] == 1
    assert result["updated"] == 0
    assert result["skipped"] == 1
    assert result["skipped_first_five_settlement_ready"] == 0
    assert result["skipped_closed_f5_market"] == 1
    assert trade.current_price == Decimal("0.4500")
    assert trade.current_price_updated_at == old_mark_time.replace(tzinfo=None)
    assert market.status == "closed"


def test_open_position_price_refresh_does_not_stamp_cached_prices_on_orderbook_error() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class BrokenOrderbookClient:
        def get_orderbook(self, ticker: str):
            raise RuntimeError("kalshi unavailable")

    old_mark_time = datetime(2026, 7, 1, 15, 0, tzinfo=UTC)
    with Session(engine) as session:
        epoch_id = _active_epoch_id(session)
        market = KalshiMarket(
            kalshi_market_id="KX-REFRESH-STALE",
            ticker="KXMLBGAME-26JUL011900SEAPIT-PIT",
            title="Will Pittsburgh win?",
            status="open",
            best_yes_bid=Decimal("0.4100"),
        )
        session.add(market)
        session.flush()
        candidate = ModelCandidate(
            paper_trading_epoch_id=epoch_id,
            kalshi_market_id=market.id,
            evaluated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            features={},
            decision="paper_trade",
            market_type="full_game_winner",
        )
        session.add(candidate)
        session.flush()
        open_trade = PaperTrade(
            paper_trading_epoch_id=epoch_id,
            candidate_id=candidate.id,
            market_ticker=market.ticker,
            contract_side="yes",
            entry_price=Decimal("0.4000"),
            current_price=Decimal("0.4000"),
            current_price_updated_at=old_mark_time,
            quantity=1,
            entry_time=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            status="open",
        )
        session.add(open_trade)
        session.commit()

        result = position_refresh.refresh_open_position_prices(session, client=BrokenOrderbookClient())
        session.refresh(open_trade)

    assert result["checked"] == 1
    assert result["updated"] == 0
    assert result["skipped"] == 1
    assert result["errors"][0]["error"]["type"] == "RuntimeError"
    assert open_trade.current_price == Decimal("0.4000")
    assert open_trade.current_price_updated_at == old_mark_time.replace(tzinfo=None)


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


def test_spread_mapping_keeps_ticker_only_parse_needs_review() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="spread-unverified-text-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        run = MarketFamilyDiscoveryRun(
            target_date=date(2026, 7, 1),
            started_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            status="completed",
            games_considered=1,
            families_considered=1,
            markets_found=1,
            errors=[],
            warnings=[],
            raw_summary={},
        )
        session.add_all([game, run])
        session.flush()
        session.add(
            MarketFamilyDiscoveryItem(
                run_id=run.id,
                mlb_game_id=game.id,
                family_key="full_game_spread",
                returned_ticker="KXMLBSPREAD-26JUL011900SEAPIT-PIT-1.5",
                returned_event_ticker="KXMLBSPREAD-26JUL011900SEAPIT",
                title="Seattle Mariners vs Pittsburgh Pirates",
                raw_status="open",
                confidence=Decimal("0.9500"),
                line_value=Decimal("-1.5000"),
            )
        )
        session.commit()

        result = market_family_mapping.sync_market_family_mappings(session, date(2026, 7, 1))
        mapping = session.scalar(select(MarketMapping))

    assert result["paper_supported"] == 0
    assert result["needs_review"] == 1
    assert mapping is not None
    assert mapping.mapping_status == "needs_review"
    assert mapping.settlement_rule_status == "needs_review"
    assert mapping.mapping_metadata["spread_verification"]["verified"] is False


def test_spread_audit_reports_verified_spread_without_trades(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="spread-audit-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 18, 0, tzinfo=UTC),
            status="scheduled",
        )
        market = KalshiMarket(
            kalshi_market_id="KX-SPREAD-AUDIT",
            ticker="KXMLBSPREAD-26JUL011400SEAPIT",
            title="Seattle Mariners vs Pittsburgh Pirates run line",
            yes_subtitle="Pittsburgh -1.5",
            no_subtitle="Seattle +1.5",
            rules="If Pittsburgh wins by more than 1.5 runs, this market resolves to Yes.",
            status="open",
            market_family="full_game_spread",
            market_type="full_game_spread",
            line_value=Decimal("-1.5000"),
            selection_code="PIT",
            inning_scope="full_game",
            settlement_rule_status="paper_supported",
        )
        session.add_all([game, market])
        session.flush()
        session.add(
            MarketMapping(
                mlb_game_id=game.id,
                kalshi_market_id=market.id,
                mapping_status="confirmed",
                confidence=Decimal("0.9500"),
                market_family="full_game_spread",
                market_type="full_game_spread",
                line_value=Decimal("-1.5000"),
                selection_code="PIT",
                inning_scope="full_game",
                settlement_rule_status="paper_supported",
                mapping_metadata={"existing_note": "preserve"},
            )
        )
        session.commit()
        monkeypatch.setattr("app.services.spread_audit.utc_now", lambda: now)
        result = run_spread_audit(
            session,
            date(2026, 7, 1),
            min_time_to_start_minutes=45,
            max_time_to_start_minutes=180,
        )
        trade = session.scalar(select(PaperTrade))
        settlement = session.scalar(select(Settlement))
        mapping = session.scalar(select(MarketMapping))

    assert result["checked"] == 1
    assert result["verified"] == 1
    assert result["trusted_audit_only_count"] == 1
    assert result["read_only"] is True
    assert result["mapping_mutations"] == 0
    assert result["settlement_rows_created"] == 0
    assert result["paper_trades_created"] == 0
    item = result["items"][0]
    assert item["audit_status"] == "trusted_audit_only"
    assert "rules_text_spread_condition_verified" in item["reason_codes"]
    assert "selected_team_threshold_verified" in item["reason_codes"]
    assert "binary_yes_no_complement_verified" in item["reason_codes"]
    assert "half_run_no_push_verified" in item["reason_codes"]
    assert "settlement_formula_verified" in item["reason_codes"]
    assert item["market_family"] == "full_game_spread"
    assert item["inning_scope"] == "full_game"
    assert item["selected_team"] == "PIT"
    assert item["condition_type"] == "team_wins_by_more_than"
    assert item["rules_threshold_runs"] == "1.5000"
    assert item["selected_team_margin_required_gt"] == "1.5000"
    assert item["settlement_formula"] == "selected_team_runs - opponent_runs > 1.5"
    assert item["no_text_source"] == "explicit_no_text"
    assert item["no_complement_source"] == "binary_market_complement"
    assert item["line_sign"] == "negative"
    assert item["line_direction"] == "selected_team_lays_runs"
    assert item["actual_contract_display"] == "YES ON PITTSBURGH PIRATES -1.5 FULL GAME"
    assert item["no_contract_display"] == "NO ON PITTSBURGH PIRATES -1.5 FULL GAME"
    assert item["normalized_no_equivalent_display"] == "SEATTLE MARINERS +1.5 FULL GAME EQUIVALENT"
    assert item["yes_outcome_interpretation"] == "PITTSBURGH PIRATES -1.5 COVERS FULL GAME"
    assert item["no_outcome_interpretation"] == "PITTSBURGH PIRATES -1.5 DOES NOT COVER FULL GAME"
    assert item["no_is_true_complement"] is True
    assert item["complement_safe_for_paper_settlement"] is True
    assert item["push_possible"] is False
    assert item["push_condition"] == "not_applicable_half_run_line"
    assert item["settlement_preview"]["preview_status"] == "pending_final"
    assert item["settlement_preview"]["selected_team"] == "PIT"
    assert item["settlement_preview"]["opponent_team"] == "SEA"
    assert item["settlement_preview"]["threshold_runs"] == "1.5000"
    assert item["settlement_preview"]["selected_margin_required_gt"] == "1.5000"
    assert item["settlement_preview"]["yes_condition"] == "selected_team_runs - opponent_runs > 1.5"
    assert trade is None
    assert settlement is None
    assert mapping is not None
    assert mapping.mapping_metadata == {"existing_note": "preserve"}


def test_spread_audit_reaudits_legacy_verified_metadata(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        _game, market, mapping = _add_spread_audit_case(
            session,
            external_game_id="spread-audit-legacy-metadata",
            ticker="KXMLBSPREAD-LEGACY-METADATA",
            scheduled_start=datetime(2026, 7, 1, 18, 0, tzinfo=UTC),
            yes_subtitle="Pittsburgh -1.5",
            no_subtitle=None,
            rules=None,
        )
        legacy_metadata = {
            "spread_verification": {
                "verified": True,
                "parser_status": "verified",
                "settlement_rule_status": "verified",
                "selection_code": "PIT",
                "line_value": "-1.5000",
            }
        }
        mapping.mapping_metadata = legacy_metadata
        session.commit()
        monkeypatch.setattr("app.services.spread_audit.utc_now", lambda: now)
        result = run_spread_audit(
            session,
            date(2026, 7, 1),
            min_time_to_start_minutes=45,
            max_time_to_start_minutes=180,
        )
        persisted_mapping = session.scalar(select(MarketMapping))

    assert result["checked"] == 1
    assert result["verified"] == 0
    item = result["items"][0]
    assert item["audit_status"] == "ambiguous_yes_no_semantics"
    assert "binary_complement_unverified" in item["reason_codes"]
    assert "settlement_text_missing" in item["reason_codes"]
    assert item["no_text_source"] == "missing"
    assert item["trusted_audit_only"] is False
    assert item["no_is_true_complement"] is False
    assert persisted_mapping is not None
    assert persisted_mapping.mapping_metadata == legacy_metadata


def _add_spread_audit_case(
    session: Session,
    *,
    external_game_id: str,
    ticker: str,
    scheduled_start: datetime,
    status: str = "scheduled",
    home_score: int | None = None,
    away_score: int | None = None,
    yes_subtitle: str | None = "Pittsburgh -1.5",
    no_subtitle: str | None = "Seattle +1.5",
    rules: str | None = None,
    line_value: Decimal | None = Decimal("-1.5000"),
    selection_code: str | None = "PIT",
) -> tuple[MlbGame, KalshiMarket, MarketMapping]:
    game = MlbGame(
        external_game_id=external_game_id,
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=scheduled_start,
        status=status,
        home_score=home_score,
        away_score=away_score,
    )
    market = KalshiMarket(
        kalshi_market_id=f"KX-{ticker}",
        ticker=ticker,
        title="Seattle Mariners vs Pittsburgh Pirates run line",
        yes_subtitle=yes_subtitle,
        no_subtitle=no_subtitle,
        rules=rules,
        status="open",
        market_family="full_game_spread",
        market_type="full_game_spread",
        line_value=line_value,
        selection_code=selection_code,
        inning_scope="full_game",
        settlement_rule_status="paper_supported",
    )
    session.add_all([game, market])
    session.flush()
    mapping = MarketMapping(
        mlb_game_id=game.id,
        kalshi_market_id=market.id,
        mapping_status="confirmed",
        confidence=Decimal("0.9500"),
        market_family="full_game_spread",
        market_type="full_game_spread",
        line_value=line_value,
        selection_code=selection_code,
        inning_scope="full_game",
        settlement_rule_status="paper_supported",
    )
    session.add(mapping)
    return game, market, mapping


def test_spread_audit_classifies_ambiguous_full_game_spreads(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        _add_spread_audit_case(
            session,
            external_game_id="spread-audit-missing-line",
            ticker="KXMLBSPREAD-MISSING-LINE",
            scheduled_start=datetime(2026, 7, 1, 18, 0, tzinfo=UTC),
            yes_subtitle="Pittsburgh run line",
            no_subtitle="Seattle run line",
            line_value=None,
        )
        _add_spread_audit_case(
            session,
            external_game_id="spread-audit-ambiguous-team",
            ticker="KXMLBSPREAD-AMBIGUOUS-TEAM",
            scheduled_start=datetime(2026, 7, 1, 18, 30, tzinfo=UTC),
            yes_subtitle="Seattle and Pittsburgh -1.5",
            no_subtitle="Seattle +1.5",
        )
        _add_spread_audit_case(
            session,
            external_game_id="spread-audit-ambiguous-line",
            ticker="KXMLBSPREAD-AMBIGUOUS-LINE",
            scheduled_start=datetime(2026, 7, 1, 19, 0, tzinfo=UTC),
            yes_subtitle="Pittsburgh run line",
            no_subtitle="Seattle +1.5",
        )
        _add_spread_audit_case(
            session,
            external_game_id="spread-audit-push-uncertain",
            ticker="KXMLBSPREAD-PUSH-UNCERTAIN",
            scheduled_start=datetime(2026, 7, 1, 19, 30, tzinfo=UTC),
            yes_subtitle="Pittsburgh -1",
            no_subtitle="Seattle +1",
            rules=None,
            line_value=Decimal("-1.0000"),
        )
        _add_spread_audit_case(
            session,
            external_game_id="spread-audit-no-wrong-sign",
            ticker="KXMLBSPREAD-NO-WRONG-SIGN",
            scheduled_start=datetime(2026, 7, 1, 19, 45, tzinfo=UTC),
            yes_subtitle="Pittsburgh -1.5",
            no_subtitle="Seattle -1.5",
        )
        session.commit()
        monkeypatch.setattr("app.services.spread_audit.utc_now", lambda: now)
        result = run_spread_audit(
            session,
            date(2026, 7, 1),
            min_time_to_start_minutes=45,
            max_time_to_start_minutes=240,
        )

    statuses = {item["market_ticker"]: item["audit_status"] for item in result["items"]}
    reasons = {item["market_ticker"]: item["reason_codes"] for item in result["items"]}
    assert statuses["KXMLBSPREAD-MISSING-LINE"] == "missing_line"
    assert statuses["KXMLBSPREAD-AMBIGUOUS-TEAM"] == "ambiguous_team_selection"
    assert statuses["KXMLBSPREAD-AMBIGUOUS-LINE"] == "ambiguous_line_direction"
    assert statuses["KXMLBSPREAD-PUSH-UNCERTAIN"] == "push_behavior_uncertain"
    assert statuses["KXMLBSPREAD-NO-WRONG-SIGN"] == "unsafe"
    assert "missing_line" in reasons["KXMLBSPREAD-MISSING-LINE"]
    assert "team_selection_not_verified" in reasons["KXMLBSPREAD-AMBIGUOUS-TEAM"]
    assert "line_direction_not_verified_from_text" in reasons["KXMLBSPREAD-AMBIGUOUS-LINE"]
    assert "push_behavior_unverified" in reasons["KXMLBSPREAD-PUSH-UNCERTAIN"]
    assert "no_contract_text_conflicts_with_expected_complement" in reasons["KXMLBSPREAD-NO-WRONG-SIGN"]
    assert result["missing_line_count"] == 1
    assert result["ambiguous_team_selection_count"] == 1
    assert result["unsafe_count"] == 1
    assert result["ambiguous_line_direction_count"] == 1
    assert result["push_behavior_uncertain_count"] == 1
    assert result["examples_by_reason"]["missing_line"][0]["market_ticker"] == "KXMLBSPREAD-MISSING-LINE"


def test_spread_audit_settlement_preview_handles_win_loss_push_and_pending(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    with Session(engine) as session:
        _add_spread_audit_case(
            session,
            external_game_id="spread-preview-win",
            ticker="KXMLBSPREAD-PREVIEW-WIN",
            scheduled_start=datetime(2026, 7, 1, 18, 0, tzinfo=UTC),
            status="Final",
            home_score=5,
            away_score=3,
        )
        _add_spread_audit_case(
            session,
            external_game_id="spread-preview-loss",
            ticker="KXMLBSPREAD-PREVIEW-LOSS",
            scheduled_start=datetime(2026, 7, 1, 18, 30, tzinfo=UTC),
            status="Final",
            home_score=4,
            away_score=3,
        )
        _add_spread_audit_case(
            session,
            external_game_id="spread-preview-push",
            ticker="KXMLBSPREAD-PREVIEW-PUSH",
            scheduled_start=datetime(2026, 7, 1, 19, 0, tzinfo=UTC),
            status="Final",
            home_score=4,
            away_score=3,
            yes_subtitle="Pittsburgh -1",
            no_subtitle="Seattle +1",
            line_value=Decimal("-1.0000"),
        )
        _add_spread_audit_case(
            session,
            external_game_id="spread-preview-rules-threshold",
            ticker="KXMLBSPREAD-PREVIEW-RULES-THRESHOLD",
            scheduled_start=datetime(2026, 7, 1, 19, 15, tzinfo=UTC),
            status="Final",
            home_score=4,
            away_score=3,
            yes_subtitle="Pittsburgh wins by more than 1 run",
            no_subtitle="Pittsburgh wins by more than 1 run",
            rules="If Pittsburgh wins by more than 1 run, this market resolves to Yes.",
            line_value=Decimal("-1.0000"),
        )
        _add_spread_audit_case(
            session,
            external_game_id="spread-preview-rules-threshold-push",
            ticker="KXMLBSPREAD-PREVIEW-RULES-THRESHOLD-PUSH",
            scheduled_start=datetime(2026, 7, 1, 19, 20, tzinfo=UTC),
            status="Final",
            home_score=4,
            away_score=3,
            yes_subtitle="Pittsburgh wins by more than 1 run",
            no_subtitle="Pittsburgh wins by more than 1 run",
            rules=(
                "If Pittsburgh wins by more than 1 run, this market resolves to Yes. "
                "If Pittsburgh wins by exactly 1 run, the market pushes and contracts are void."
            ),
            line_value=Decimal("-1.0000"),
        )
        _add_spread_audit_case(
            session,
            external_game_id="spread-preview-pending",
            ticker="KXMLBSPREAD-PREVIEW-PENDING",
            scheduled_start=datetime(2026, 7, 1, 19, 30, tzinfo=UTC),
            status="In Progress",
        )
        session.commit()
        monkeypatch.setattr("app.services.spread_audit.utc_now", lambda: now)
        result = run_spread_audit(
            session,
            date(2026, 7, 1),
            min_time_to_start_minutes=None,
            max_time_to_start_minutes=None,
        )
        settlements = list(session.scalars(select(Settlement)))
        trades = list(session.scalars(select(PaperTrade)))

    previews = {item["market_ticker"]: item["settlement_preview"] for item in result["items"]}
    assert previews["KXMLBSPREAD-PREVIEW-WIN"]["preview_status"] == "computed"
    assert previews["KXMLBSPREAD-PREVIEW-WIN"]["yes_outcome"] == "win"
    assert previews["KXMLBSPREAD-PREVIEW-WIN"]["no_outcome"] == "loss"
    assert previews["KXMLBSPREAD-PREVIEW-LOSS"]["yes_outcome"] == "loss"
    assert previews["KXMLBSPREAD-PREVIEW-LOSS"]["no_outcome"] == "win"
    assert previews["KXMLBSPREAD-PREVIEW-PUSH"]["yes_outcome"] == "push"
    assert previews["KXMLBSPREAD-PREVIEW-PUSH"]["no_outcome"] == "push"
    assert previews["KXMLBSPREAD-PREVIEW-RULES-THRESHOLD"]["yes_condition"] == (
        "selected_team_runs - opponent_runs > 1"
    )
    assert previews["KXMLBSPREAD-PREVIEW-RULES-THRESHOLD"]["line_adjusted_margin"] == "0.0000"
    assert previews["KXMLBSPREAD-PREVIEW-RULES-THRESHOLD"]["yes_outcome"] == "loss"
    assert previews["KXMLBSPREAD-PREVIEW-RULES-THRESHOLD"]["no_outcome"] == "win"
    assert previews["KXMLBSPREAD-PREVIEW-RULES-THRESHOLD"]["push"] is False
    assert previews["KXMLBSPREAD-PREVIEW-RULES-THRESHOLD-PUSH"]["yes_condition"] == (
        "selected_team_runs - opponent_runs > 1"
    )
    assert previews["KXMLBSPREAD-PREVIEW-RULES-THRESHOLD-PUSH"]["line_adjusted_margin"] == "0.0000"
    assert previews["KXMLBSPREAD-PREVIEW-RULES-THRESHOLD-PUSH"]["yes_outcome"] == "push"
    assert previews["KXMLBSPREAD-PREVIEW-RULES-THRESHOLD-PUSH"]["no_outcome"] == "push"
    assert previews["KXMLBSPREAD-PREVIEW-RULES-THRESHOLD-PUSH"]["push"] is True
    assert previews["KXMLBSPREAD-PREVIEW-PENDING"]["preview_status"] == "pending_final"
    assert result["settlement_rows_created"] == 0
    assert result["paper_trades_created"] == 0
    assert settlements == []
    assert trades == []


def test_spread_audit_job_does_not_run_mapping_sync(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    def fail_mapping_sync(*_args, **_kwargs):
        raise AssertionError("spread-audit job must be read-only and not run mapping sync")

    monkeypatch.setattr(job_runs, "sync_market_family_mappings", fail_mapping_sync)
    monkeypatch.setattr(job_runs, "run_spread_audit", lambda *_args, **_kwargs: {"status": "completed"})

    with Session(engine) as session:
        result = job_runs.run_job(session, job_name="spread-audit", target_date=date(2026, 7, 1))

    assert result["status"] == "succeeded"
    assert result["result"]["spread_audit"]["status"] == "completed"
    assert "market_family_mappings" not in result["result"]


def test_total_no_label_displays_under_equivalent() -> None:
    game = MlbGame(
        external_game_id="total-label-1",
        home_team="Pittsburgh Pirates",
        away_team="Seattle Mariners",
        home_abbreviation="PIT",
        away_abbreviation="SEA",
        scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
        status="scheduled",
    )
    labels = contract_labels(
        game=game,
        market=KalshiMarket(
            kalshi_market_id="KX-TOTAL-LABEL",
            ticker="KXMLBTOTAL-26JUL011900SEAPIT-OVER-8",
            title="Total runs over 8",
            status="open",
            line_value=Decimal("8.0000"),
            over_under_side="over",
        ),
        market_ticker="KXMLBTOTAL-26JUL011900SEAPIT-OVER-8",
        market_type="full_game_total",
        contract_side="no",
    )

    assert labels.actual_contract_display == "NO ON OVER 8 FULL GAME"
    assert labels.normalized_equivalent_display == "UNDER 8 FULL GAME EQUIVALENT"


def _add_scope_market(
    session: Session,
    game: MlbGame,
    *,
    ticker: str,
    family: str,
    scope: str,
    ask: str = "0.4000",
    line_value: Decimal | None = None,
    selection_code: str | None = None,
    over_under_side: str | None = None,
) -> None:
    market = KalshiMarket(
        kalshi_market_id=f"KX-{ticker}",
        ticker=ticker,
        title=ticker,
        status="open",
        occurrence_datetime=game.scheduled_start,
        implied_yes_ask=Decimal(ask),
        market_family=family,
        market_type=family,
        line_value=line_value,
        selection_code=selection_code,
        over_under_side=over_under_side,
        inning_scope=scope,
        settlement_rule_status="paper_supported",
    )
    session.add(market)
    _add_candidate_mapping(
        session,
        game,
        market,
        mapping_status="confirmed",
        market_family=family,
        market_type=family,
        line_value=line_value,
        selection_code=selection_code,
        over_under_side=over_under_side,
        inning_scope=scope,
        settlement_rule_status="paper_supported",
    )


def test_same_game_same_scope_correlation_blocks_second_trade(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.800000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="same-scope-cap-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        session.add(game)
        session.flush()
        _add_scope_market(
            session,
            game,
            ticker="KXMLBF5-26JUL011900SEAPIT-PIT",
            family="first_five_winner",
            scope="first_five",
            selection_code="PIT",
        )
        _add_scope_market(
            session,
            game,
            ticker="KXMLBF5TOTAL-26JUL011900SEAPIT-OVER-4.5",
            family="first_five_total",
            scope="first_five",
            line_value=Decimal("4.5000"),
            over_under_side="over",
        )
        session.commit()

        result = candidates.generate_candidates(session, target_date=date(2026, 7, 1))
        decisions = {candidate.decision for candidate in session.scalars(select(ModelCandidate))}

    assert result["paper_trades"] == 1
    assert result["game_scope_correlation_candidates_rejected"] == 1
    assert "no_trade_same_game_scope_correlation_not_best" in decisions
    assert result["risk_caps"]["risk_limit_basis_type"] == "active_epoch_portfolio_value"


def test_game_scope_correlation_rejection_persists_without_creating_trade(monkeypatch) -> None:
    monkeypatch.delenv("PAPER_MAX_TRADES_PER_GAME_SCOPE", raising=False)
    get_settings.cache_clear()
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)

    try:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            game = MlbGame(
                external_game_id="same-scope-direct-1",
                home_team="Pittsburgh Pirates",
                away_team="Seattle Mariners",
                home_abbreviation="PIT",
                away_abbreviation="SEA",
                scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
                status="scheduled",
            )
            session.add(game)
            session.flush()
            first_candidate = ModelCandidate(
                mlb_game_id=game.id,
                evaluated_at=now,
                features={},
                target_date=date(2026, 7, 1),
                market_family="first_five_total",
                market_type="first_five_total",
                inning_scope="first_five",
                decision="eligible_for_paper_trade",
                net_expected_value=Decimal("0.100000"),
                data_quality=Decimal("1.0000"),
            )
            second_candidate = ModelCandidate(
                mlb_game_id=game.id,
                evaluated_at=now,
                features={},
                target_date=date(2026, 7, 1),
                market_family="first_five_winner",
                market_type="first_five_winner",
                inning_scope="first_five",
                decision="eligible_for_paper_trade",
                net_expected_value=Decimal("0.090000"),
                data_quality=Decimal("1.0000"),
            )
            session.add_all([first_candidate, second_candidate])
            session.flush()

            market_one = KalshiMarket(
                kalshi_market_id="KX-SCOPE-DIRECT-1",
                ticker="KXMLBF5TOTAL-26JUL011900SEAPIT-OVER-4.5",
                title="First five total over 4.5",
                status="open",
            )
            market_two = KalshiMarket(
                kalshi_market_id="KX-SCOPE-DIRECT-2",
                ticker="KXMLBF5-26JUL011900SEAPIT-TIE",
                title="First five tie",
                status="open",
            )
            intent_one = candidates.TradeIntent(
                candidate=first_candidate,
                game=game,
                market=market_one,
                price=Decimal("0.4000"),
                labels=SimpleNamespace(),
                score=Decimal("2.0000"),
            )
            intent_two = candidates.TradeIntent(
                candidate=second_candidate,
                game=game,
                market=market_two,
                price=Decimal("0.4000"),
                labels=SimpleNamespace(),
                score=Decimal("1.0000"),
            )

            selected, counts, _summary = candidates._apply_game_scope_correlation(
                session,
                [intent_one, intent_two],
                epoch_id=None,
            )
            session.flush()
            session.commit()

            saved_rejected = session.get(ModelCandidate, second_candidate.id)
            trades = list(session.scalars(select(PaperTrade)))

        assert selected == [intent_one]
        assert counts["game_scope_correlation_candidates_rejected"] == 1
        assert saved_rejected is not None
        assert saved_rejected.decision == "no_trade_same_game_scope_correlation_not_best"
        assert trades == []
    finally:
        get_settings.cache_clear()


def test_same_game_different_scopes_can_both_trade(monkeypatch) -> None:
    _relax_data_quality_gate(monkeypatch)
    now = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(candidates, "utc_now", lambda: now)
    monkeypatch.setattr(candidates, "score_mature_candidate", lambda *_args, **_kwargs: _fixed_model_score("0.800000"))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        game = MlbGame(
            external_game_id="different-scope-cap-1",
            home_team="Pittsburgh Pirates",
            away_team="Seattle Mariners",
            home_abbreviation="PIT",
            away_abbreviation="SEA",
            scheduled_start=datetime(2026, 7, 1, 23, 0, tzinfo=UTC),
            status="scheduled",
        )
        session.add(game)
        session.flush()
        _add_scope_market(
            session,
            game,
            ticker="KXMLBGAME-26JUL011900SEAPIT-PIT",
            family="full_game_winner",
            scope="full_game",
            selection_code="PIT",
        )
        _add_scope_market(
            session,
            game,
            ticker="KXMLBF5-26JUL011900SEAPIT-PIT",
            family="first_five_winner",
            scope="first_five",
            selection_code="PIT",
        )
        session.commit()

        result = candidates.generate_candidates(session, target_date=date(2026, 7, 1))

    assert result["paper_trades"] == 2
    assert result["game_scope_correlation_candidates_rejected"] == 0

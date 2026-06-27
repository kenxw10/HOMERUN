# Railway Setup

Use Railway for the FastAPI backend and PostgreSQL database.

## Steps

1. Create a new Railway project.
2. Connect the GitHub repo `kenxw10/HOMERUN`.
3. Create a backend service from the repo.
4. Set the service root directory to `/apps/api`.
5. Add a Railway PostgreSQL service.
6. Set `DATABASE_URL` on the backend service from the Railway PostgreSQL connection string.
7. Set the backend start command:

```powershell
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

8. Set safe environment flags:

```powershell
APP_ENV=production
PAPER_TRADING=true
LIVE_TRADING_ENABLED=false
EXECUTION_KILL_SWITCH=true
KALSHI_ENV=demo
CORS_ORIGINS=https://YOUR-VERCEL-DASHBOARD-URL
KALSHI_REST_BASE_URL=https://demo-api.kalshi.co/trade-api/v2
KALSHI_MARKET_DATA_BASE_URL=https://external-api.kalshi.com/trade-api/v2
KALSHI_WS_BASE_URL=wss://demo-api.kalshi.co/trade-api/ws/v2
MLB_STATS_BASE_URL=https://statsapi.mlb.com/api/v1
FEATURE_SYNC_ENABLE_NETWORK_SOURCES=true
OPEN_METEO_BASE_URL=https://api.open-meteo.com/v1
INJURY_PROVIDER_API_KEY=
LINEUP_PROVIDER_API_KEY=
WEATHER_PROVIDER_API_KEY=
MARKET_DISCOVERY_ENABLED=true
KALSHI_ENABLE_BROAD_DISCOVERY=false
KALSHI_MARKET_SYNC_MAX_PAGES=2
KALSHI_MARKET_SYNC_LIMIT=100
MARKET_FAMILY_DISCOVERY_ENABLED=true
MARKET_FAMILY_DISCOVERY_MAX_PAGES=2
KALSHI_DISCOVERY_ENABLE_FALLBACK_TIME_OFFSETS=true
KALSHI_DISCOVERY_MAX_FALLBACK_OFFSETS=6
KALSHI_DISCOVERY_MAX_429_ERRORS=5
KALSHI_MARKET_DATA_MIN_REQUEST_INTERVAL_MS=500
KALSHI_MARKET_DATA_MAX_RETRIES=2
KALSHI_MARKET_DATA_BACKOFF_BASE_MS=1000
KALSHI_MARKET_DATA_BACKOFF_MAX_MS=10000
OPEN_POSITION_PRICE_REFRESH_ENABLED=true
OPEN_POSITION_PRICE_REFRESH_MAX_PER_RUN=100
PAPER_CANDIDATE_ENGINE_ENABLED=true
DEFAULT_PAPER_CONTRACTS=1
PAPER_MAX_TRADES_PER_SLATE=20
PAPER_MAX_TRADES_PER_GAME=3
PAPER_MAX_TRADES_PER_MARKET_FAMILY=8
PAPER_MAX_TRADES_PER_GAME_FAMILY=1
PAPER_ALLOW_MULTIPLE_LINES_PER_GAME_FAMILY=false
PAPER_ALLOW_MULTIPLE_F5_WINNER_OUTCOMES=false
PAPER_MAX_OPEN_POSITIONS=50
PAPER_MIN_NET_EV=0.05
PAPER_MIN_PROB_EDGE=0.03
PAPER_MIN_DATA_QUALITY=0.60
PAPER_REQUIRE_CALIBRATED_FOR_TRADE=false
PAPER_MAX_PRICE_STALENESS_SECONDS=900
PAPER_ALLOW_LAST_PRICE_FALLBACK_FOR_TRADE=false
PAPER_STARTING_BALANCE=1000.00
KALSHI_TRADE_FEE_RATE=0.07
KALSHI_FEE_ESTIMATE_MODE=conservative
KALSHI_FEE_ROUNDING_MODE=centicent_or_cent_conservative
KALSHI_ASSUME_TAKER=true
MODEL_TRAINING_MIN_SAMPLES=100
MODEL_MIN_SAMPLES_TRAIN=250
MODEL_MIN_SAMPLES_CALIBRATE=250
MODEL_MIN_SAMPLES_PROMOTE=500
MODEL_PROMOTION_MIN_LOGLOSS_IMPROVEMENT=0.01
MODEL_PROMOTION_MAX_ECE=0.08
MODEL_MIN_FAMILY_SAMPLES_FOR_FAMILY_CALIBRATION=75
MODEL_MIN_SAMPLES_FOR_ISOTONIC=1000
DASHBOARD_TIMEZONE=America/New_York
BACKEND_API_KEY=replace-with-a-long-random-secret
```

Use the exact Vercel dashboard origin for `CORS_ORIGINS`, without a trailing slash. Example: `https://homerun.vercel.app`.

9. Required for Railway: set `BACKEND_API_KEY` to a long random value. Internal POST run endpoints reject unauthenticated requests outside local development.
10. Do not add production Kalshi credentials in PR 3c.
11. Deploy the service.
12. Run database migrations.
13. After deploy, open `/health` and `/v1/system/status` on the Railway backend URL.

Expected `/health` result should include:

- `status: "ok"`
- `paper_trading: true`
- `live_trading_enabled: false`

Expected `/v1/system/status` result should include:

- `backend.ready: true`
- `database.ready: true`
- `config.live_trading_enabled: false`
- `config.execution_kill_switch: true`
- `config.kalshi_market_data_source: "production_public_market_data"` when using the default market-data URL.
- `config.kalshi_market_data_base_kind: "production_public_market_data"` when using the default market-data URL.

## Migration Command

When the Railway database is ready, run migrations from the backend service context:

```powershell
alembic upgrade head
```

If migration fails, check that `DATABASE_URL` exists and points to the Railway PostgreSQL service.

## PR 3c One-Off Job Commands

Run these from the Railway backend service shell after migrations succeed:

```powershell
python -m app.jobs.mlb_schedule_sync
python -m app.jobs.kalshi_market_sync
python -m app.jobs.paper_candidate_engine 2026-06-27
python -m app.jobs.mlb_results_sync
python -m app.jobs.paper_settlement_sync
python -m app.jobs.balance_snapshot
python -m app.jobs.model_governance
python -m app.jobs.mlb_feature_sync
python -m app.jobs.mlb_feature_sync 2026-06-27
python -m app.jobs.model_feature_snapshot_backfill
python -m app.jobs.training_eligibility_repair
python -m app.jobs.market_family_discovery
python -m app.jobs.market_family_mapping_sync
python -m app.jobs.open_position_price_refresh
```

These commands create database records for the dashboard and paper engine. They do not place live orders.

## PR 3c Targeted Resolver, Discovery, Mapping, Model, And Paper Results Validation

PR 3 keeps Kalshi market sync on targeted MLB resolution and adds paper results/model workflows. Normal production should leave broad discovery disabled:

```powershell
KALSHI_ENABLE_BROAD_DISCOVERY=false
```

After migrations and deploy, validate with the internal API key:

```powershell
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/sync/mlb-schedule
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/kalshi/resolve-preview?date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/sync/kalshi-markets
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/run/paper-candidate-engine?target_date=2026-06-27"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/predictions?date=2026-06-27"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/sync/mlb-results
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/paper-settlement-sync
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/balance-snapshot
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/model-governance
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/model/governance/status
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/mlb-features?target_date=2026-06-26"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/features/coverage?date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/run/market-family-discovery?target_date=2026-06-26"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/market-families/discovery?date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/market-family-mappings?target_date=2026-06-26"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/market-families/mappings?date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/open-position-price-refresh
```

Expected behavior:

- Resolve preview shows attempted `KXMLBGAME` event and market tickers for each MLB game.
- Resolve preview returns `ok=true` with per-game warnings/partial errors when only some games miss.
- Kalshi sync returns a structured summary with mapping counts and actionable error details if Kalshi upstream calls fail.
- Missing matching Kalshi markets should produce a clean summary, not a blank 502.
- Paper candidate engine creates candidates with `mature_mlb_run_distribution_v1` probabilities only for the requested Eastern `target_date`, records fee/edge/executable-price diagnostics, and creates paper trades only after fee-adjusted EV, freshness, line-selection, and cap filters pass.
- MLB results sync updates final scores/status.
- Paper settlement sync settles completed supported full-game winner paper trades.
- Balance snapshots populate `/v1/dashboard/summary`.
- Model governance records either a trained/promoted model or a clear skipped reason due to insufficient mature resolved samples.
- Model feature coverage reports explicit source statuses and does not fake missing lineup, weather, injury, umpire, team-total, or sportsbook data.
- PR 3b market-family discovery returns structured `by_family` output from the observed deterministic prefixes `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`, `KXMLBF5`, `KXMLBF5SPREAD`, and `KXMLBF5TOTAL`, while mapping sync promotes only parseable validated rows to `paper_supported`.
- Candidate generation can paper trade supported spread, total, and first-five mappings only when the market is open, the ask is executable, the edge threshold clears, and the safety posture remains paper-only.
- Candidate generation applies slate, game, market-family, open-position, and correlated game/family caps before creating paper trades.
- First-five settlement requires MLB linescore innings. Missing linescore should produce a skipped result and leave the trade open.
- Discovery uses batched exact ticker queries first, capped fallback offsets second, and `event_ticker` filtering only as a secondary fallback. The result should include `request_count`, `requests_saved_by_batching`, `rate_limited_count`, `retries_attempted`, and `stopped_due_to_rate_limit`.
- Open-position price refresh updates REST last marks for open paper positions only.
- PR3c fix3 public feature sync requires `FEATURE_SYNC_ENABLE_NETWORK_SOURCES=true` on Railway. When enabled, `/v1/sync/mlb-features` hydrates MLB Stats API schedule/feed data, Open-Meteo weather, raw feature cache tables, and `mature_mlb_features_v2` snapshots. When disabled, network-backed module syncs return `validation_status=skipped_network_disabled` with zero inserted/updated rows.
- Model governance uses active parameter versions and simulated threshold policies before promotion. It does not enable live orders.

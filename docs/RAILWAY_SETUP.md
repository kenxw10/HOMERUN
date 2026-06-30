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
KALSHI_DISCOVERY_MAX_BATCH_SIZE=20
KALSHI_DISCOVERY_REQUEST_SPACING_MS=750
KALSHI_DISCOVERY_MAX_429_ERRORS=3
KALSHI_DISCOVERY_COOLDOWN_SECONDS=300
KALSHI_DISCOVERY_USE_CACHE_FIRST=true
KALSHI_DISCOVERY_SKIP_IF_RECENT_MINUTES=60
KALSHI_MARKET_DATA_MIN_REQUEST_INTERVAL_MS=500
KALSHI_MARKET_DATA_MAX_RETRIES=2
KALSHI_MARKET_DATA_BACKOFF_BASE_MS=1000
KALSHI_MARKET_DATA_BACKOFF_MAX_MS=10000
OPEN_POSITION_PRICE_REFRESH_ENABLED=true
OPEN_POSITION_PRICE_REFRESH_MAX_PER_RUN=100
PAPER_CANDIDATE_ENGINE_ENABLED=true
DEFAULT_PAPER_CONTRACTS=1
PAPER_MAX_TRADES_PER_SLATE=8
PAPER_MAX_TRADES_PER_GAME=3
PAPER_MAX_TRADES_PER_MARKET_FAMILY=4
PAPER_MAX_TRADES_PER_GAME_FAMILY=1
PAPER_MAX_TRADES_PER_GAME_SCOPE=1
PAPER_ALLOW_MULTIPLE_LINES_PER_GAME_FAMILY=false
PAPER_ALLOW_MULTIPLE_F5_WINNER_OUTCOMES=false
PAPER_MAX_OPEN_POSITIONS=12
PAPER_SPREAD_TRADING_ENABLED=false
PAPER_MAX_DAILY_NEW_RISK_PCT=0.20
PAPER_MAX_OPEN_RISK_PCT=0.25
PAPER_MAX_MARKET_FAMILY_RISK_PCT=0.10
PAPER_MAX_SCOPE_RISK_PCT=0.15
PAPER_MAX_PRICE_BUCKET_RISK_PCT_UNDER_20C=0.08
PAPER_MIN_NET_EV=0.05
PAPER_MIN_PROB_EDGE=0.03
PAPER_MIN_DATA_QUALITY=0.60
PAPER_OBSERVATION_MIN_DATA_QUALITY=0.55
LIVE_MIN_DATA_QUALITY=0.60
PAPER_REQUIRE_CALIBRATED_FOR_TRADE=false
PAPER_MAX_PRICE_STALENESS_SECONDS=900
PAPER_ALLOW_LAST_PRICE_FALLBACK_FOR_TRADE=false
PAPER_STARTING_BALANCE=1000.00
PAPER_BANKROLL_STARTING_BALANCE=500.00
PAPER_POSITION_SIZING_MODE=fixed_risk
PAPER_RISK_PER_TRADE_PCT=0.025
PAPER_MIN_CONTRACTS=1
PAPER_MAX_CONTRACTS_PER_TRADE=100
PAPER_STORE_ONE_CONTRACT_EV=true
KALSHI_TRADE_FEE_RATE=0.07
KALSHI_FEE_ESTIMATE_MODE=conservative
KALSHI_FEE_ROUNDING_MODE=centicent_or_cent_conservative
KALSHI_ASSUME_TAKER=true
WEBSOCKET_MARKET_DATA_ENABLED=false
WS_SUBSCRIBE_OPEN_POSITIONS=true
WS_SUBSCRIBE_ACTIVE_CANDIDATES=true
WS_MAX_MARKETS=500
WS_RECONNECT_BACKOFF_SECONDS=5
WS_HEARTBEAT_TIMEOUT_SECONDS=30
WS_PRICE_STALE_AFTER_SECONDS=120
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

## PR3d One-Off Job Commands And Cron Services

Run these from the Railway backend service shell after migrations succeed:

```powershell
python -m app.jobs.runner --job daily-setup --target-date today_et
python -m app.jobs.runner --job candidate-sweep --target-date today_et --min-time-to-start-minutes 45 --max-time-to-start-minutes 180 --sweep-label rolling_pregame_window
python -m app.jobs.runner --job spread-audit --target-date today_et --min-time-to-start-minutes 45 --max-time-to-start-minutes 180
python -m app.jobs.runner --job price-refresh --target-date today_et
python -m app.jobs.runner --job settlement --target-date yesterday_et
python -m app.jobs.runner --job governance
python -m app.jobs.runner --job full-paper-cycle --target-date today_et
```

These commands create database records for the dashboard and paper engine. They do not place live orders.

The spread-audit command verifies spread parsing and settlement metadata from Kalshi raw text fields. It does not create paper trades. Do not add a Railway cron service for spread audit, enable `PAPER_SPREAD_TRADING_ENABLED`, or add any live execution service until the audit output has been manually validated against the Kalshi UI.

Recommended Railway cron services should be separate short-lived services, not the main web server. Times below show the intended ET cadence and the equivalent UTC cron during EDT:

| Service | Start command | ET cadence | UTC cron during EDT |
| --- | --- | --- | --- |
| `homerun-job-daily-setup` | `python -m app.jobs.runner --job daily-setup --target-date today_et` | 8:30 AM ET | `30 12 * * *` |
| `homerun-job-candidate-sweep` | `python -m app.jobs.runner --job candidate-sweep --target-date today_et --min-time-to-start-minutes 45 --max-time-to-start-minutes 180 --sweep-label rolling_pregame_window` | every 30 minutes, 10:30 AM-10:00 PM ET | `30 14 * * *`; `0,30 15-23,0-1 * * *`; `0 2 * * *` |
| `homerun-job-price-refresh` | `python -m app.jobs.runner --job price-refresh --target-date today_et` | every 15 minutes, 11:00 AM-1:30 AM ET | `0,15,30,45 15-23,0-4 * * *`; `0,15,30 5 * * *` |
| `homerun-job-settlement-today` | `python -m app.jobs.runner --job settlement --target-date today_et` | every 30 minutes, 2:30 PM-1:30 AM ET | `30 18 * * *`; `0,30 19-23,0-4 * * *`; `0,30 5 * * *` |
| `homerun-job-settlement-yesterday-catchup` | `python -m app.jobs.runner --job settlement --target-date yesterday_et` | 8:30 AM ET | `30 12 * * *` |
| `homerun-job-governance` | `python -m app.jobs.runner --job governance` | 9:00 AM ET | `0 13 * * *` |

Railway cron schedules use UTC. Adjust these schedules when EDT changes to EST. Railway may skip a run if the previous execution is still active; PR3d job locks also skip overlap or mark stale runs failed before starting safely.

Optional long-running paper market-data service:

```powershell
python -m app.workers.kalshi_ws_paper
```

Leave `WEBSOCKET_MARKET_DATA_ENABLED=false` until you deliberately validate the worker. When disabled, `/v1/ws/status` reports REST fallback.

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
- PR3d hotfix 3 adds a same-game same-scope cap with `PAPER_MAX_TRADES_PER_GAME_SCOPE=1` by default. First-five total plus first-five tie on the same game is blocked by default; one first-five and one full-game position remain separate scopes.
- PR3d hotfix 3 adds protected `POST /v1/jobs/run/spread-audit?target_date=YYYY-MM-DD&min_time_to_start_minutes=45&max_time_to_start_minutes=180`. Run it manually before enabling spread paper trading. Verified spread rows should include parser status, settlement rule status, actual contract display, normalized equivalent display, and raw Kalshi contract text.
- Candidate sweep summaries and the dashboard report risk caps using the active epoch portfolio value as the risk-limit basis.
- First-five settlement requires MLB linescore innings. Missing linescore should produce a skipped result and leave the trade open.
- Discovery uses direct exact market-ticker queries only for winner families where the selected team is deterministic. Spread and total families use `event_ticker` filtering because the actual market ticker includes Kalshi line/side details that should not be guessed. Normal runs should call `POST /v1/run/market-family-discovery?target_date=YYYY-MM-DD&force_refresh=false`, which reuses recent cached runs and honors active cooldowns. The result should include `request_count`, `requests_saved_by_batching`, `rate_limited_count`, `retries_attempted`, `stopped_due_to_rate_limit`, `served_from_cache`, and `cooldown_until`.
- Open-position price refresh updates REST last marks for open paper positions only.
- PR3c fix3 through fix6 public feature sync requires `FEATURE_SYNC_ENABLE_NETWORK_SOURCES=true` on Railway. When enabled, `/v1/sync/mlb-features` hydrates MLB Stats API schedule/feed/team stats/team game logs/pitcher stats/pitcher game logs/stat splits/boxscore data, Open-Meteo weather, optional pybaseball enrichment, raw feature cache tables, and `mature_mlb_features_v2` snapshots. Railway installs `pybaseball==2.2.7` from `apps/api/requirements.txt`; do not manually install it in PowerShell. FanGraphs-backed pybaseball 403s should degrade diagnostics but must not block MLB Stats API primary rows. When disabled, network-backed module syncs return `validation_status=skipped_network_disabled` with zero inserted/updated rows.
- PR3c fix4 makes public feature sync idempotent. Re-running `/v1/sync/mlb-team-features`, `/v1/sync/mlb-pitcher-features`, or `/v1/sync/mlb-features` for the same `target_date` should update existing `mlb_games.external_game_id` rows instead of raising duplicate-key errors. Responses include hydration counters and should return `validation_status=degraded_with_errors` with structured `errors[]` for source problems instead of unhandled 500s.
- Feature endpoints accept `refresh_schedule=true/false`. Module-specific syncs skip schedule hydration by default when target-date games already exist; force `refresh_schedule=true` only when validating schedule refresh behavior.
- `/v1/model/sources/status` reports `pybaseball_available` as an import diagnostic and `advanced_public_stats_status` as actual ingestion status. Treat MLB Stats API as the primary source, Statcast/Savant pybaseball functions as secondary contact-quality enrichment, FanGraphs-backed pybaseball functions as optional, and `derived_homerun_v2` rows as partial fallback.
- Model governance uses active parameter versions and simulated threshold policies before promotion. It does not enable live orders.

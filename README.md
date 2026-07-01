# HOMERUN

HOMERUN is a Kalshi-native MLB paper-trading system and dashboard. The current version includes the deployable PR 1 foundation, PR 2 data layer, PR 2.5 targeted Kalshi MLB resolver, PR 3 paper results/model infrastructure, PR 3a discovery/operator repairs, PR 3b validated market-family paper wiring, PR 3c full MLB model governance, the PR3c hotfix for date-scoped fee-aware trade selection, PR3c fix2 feature-complete model governance, PR3c fix3-fix6 public feature/discovery hardening, PR3e feature-completeness diagnostics, PR3f cache-only candidate sweeps, PR3g candidate-stage quality/EV diagnostics, PR3h probable-starter hydration repair, and PR3i candidate decision-length hotfix: MLB slate/results ingestion, targeted Kalshi `KXMLBGAME` market resolution, auditable game-to-market mapping, mature paper-only model candidates, paper settlement for validated MLB market families, MLB Stats API primary feature cache modules, Statcast/Savant secondary enrichment, trainable parameter governance, market-family discovery audits, portfolio snapshots, and a light trading-terminal dashboard.

This is not a sportsbook app. It does not use DraftKings, FanDuel, Odds API, or sportsbook odds behavior. Future trading logic should use Kalshi yes/no contract math, account for fees, and assume hold-to-settlement unless a later PR changes that context deliberately.

## Apps

- `apps/api` - Python FastAPI backend.
- `apps/web` - Next.js TypeScript dashboard.

## Safe Defaults

- `PAPER_TRADING=true`
- `LIVE_TRADING_ENABLED=false`
- `EXECUTION_KILL_SWITCH=true`
- `KALSHI_ENV=demo`
- `KALSHI_MARKET_DATA_BASE_URL=https://external-api.kalshi.com/trade-api/v2` for public market-data reads.
- Public market-data reads are throttled and retried by default with `KALSHI_MARKET_DATA_MIN_REQUEST_INTERVAL_MS=500`, `KALSHI_MARKET_DATA_MAX_RETRIES=2`, `KALSHI_MARKET_DATA_BACKOFF_BASE_MS=1000`, and `KALSHI_MARKET_DATA_BACKOFF_MAX_MS=10000`.
- Kalshi credentials are optional for PR 3 paper-mode discovery and must not be production credentials unless a later PR explicitly changes the safety plan.
- `BACKEND_API_KEY` is optional only for local development. Public or deployed backends must set it, and internal POST run endpoints require `X-API-Key`.
- Broad Kalshi discovery is diagnostic-only and disabled by default with `KALSHI_ENABLE_BROAD_DISCOVERY=false`.
- PR 3c scores validated MLB market-family rows with `mature_mlb_run_distribution_v2` and `mature_mlb_features_v2`.
- Team totals, multivariate/MVE markets, sportsbook data, and guessed/retired prefixes remain out of scope.
- Paper candidates are explicitly target-date scoped and side-aware: YES uses executable YES asks/orderbook-implied YES asks, and NO uses executable NO asks/orderbook-implied NO asks. The engine must not price a NO candidate from a YES ask.
- Spread paper trading is disabled by default with `PAPER_SPREAD_TRADING_ENABLED=false`. Spread rows can still be discovered, mapped, priced, and scored for diagnostics until the side-aware parser and settlement metadata are verified from Kalshi raw text fields.
- Paper trade caps default to `PAPER_MAX_TRADES_PER_SLATE=8`, `PAPER_MAX_TRADES_PER_MARKET_FAMILY=4`, and `PAPER_MAX_OPEN_POSITIONS=12`, plus aggregate bankroll caps for daily new risk, open risk, market-family risk, scope risk, and sub-20c low-price risk.
- Same-game same-scope correlation defaults to `PAPER_MAX_TRADES_PER_GAME_SCOPE=1`, so first-five total and first-five winner exposure cannot both trade for the same MLB game by default. One first-five and one full-game position remain separate scopes.
- Open-position current price uses REST last marks by default. PR3d adds an optional paper-only WebSocket market-data worker; it is disabled unless `WEBSOCKET_MARKET_DATA_ENABLED=true`.
- `PAPER_STARTING_BALANCE=1000.00` by default.
- Active PR3d observation epochs can be reset to a $500 paper bankroll through the protected reset endpoint. Archived validation rows stay in the database but are hidden from the main dashboard. The PR3d contaminated spread-validation epoch should be archived as `pr3d_bad_spread_parser_validation`, followed by a clean `pr3d_paper_observation_v2` epoch.
- PR3d paper trades use fixed-risk sizing by default: `PAPER_RISK_PER_TRADE_PCT=0.025` of the active epoch portfolio, bounded by `PAPER_MIN_CONTRACTS` and `PAPER_MAX_CONTRACTS_PER_TRADE`.
- Paper observation data quality uses `PAPER_OBSERVATION_MIN_DATA_QUALITY=0.55`; future live quality should remain `LIVE_MIN_DATA_QUALITY=0.60` or stricter.
- Governance skips training/calibration/promotion until clean resolved-sample thresholds are met.
- `FEATURE_SYNC_ENABLE_NETWORK_SOURCES=true` enables no-key public MLB Stats API and Open-Meteo feature ingestion by default. Set it to `false` only when you intentionally want source sync endpoints to skip network-backed ingestion.

## Local Backend

From Windows PowerShell:

```powershell
cd apps/api
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The API should be available at `http://127.0.0.1:8000`.

Useful checks:

```powershell
pytest
ruff check .
```

## Local Data Jobs

PR 3c keeps safe one-shot worker commands. They write MLB games/results, targeted Kalshi markets, mappings, model feature snapshots, model predictions, paper trades, settlements, balance snapshots, and model governance records to the configured database. They do not place live orders.

From `apps/api` after installing backend dependencies:

```powershell
.\.venv\Scripts\python.exe -m app.jobs.mlb_schedule_sync
.\.venv\Scripts\python.exe -m app.jobs.kalshi_market_sync
.\.venv\Scripts\python.exe -m app.jobs.paper_candidate_engine
.\.venv\Scripts\python.exe -m app.jobs.mlb_results_sync
.\.venv\Scripts\python.exe -m app.jobs.paper_settlement_sync
.\.venv\Scripts\python.exe -m app.jobs.balance_snapshot
.\.venv\Scripts\python.exe -m app.jobs.model_governance
.\.venv\Scripts\python.exe -m app.jobs.mlb_feature_sync
.\.venv\Scripts\python.exe -m app.jobs.model_feature_snapshot_backfill
.\.venv\Scripts\python.exe -m app.jobs.training_eligibility_repair
.\.venv\Scripts\python.exe -m app.jobs.market_family_discovery
.\.venv\Scripts\python.exe -m app.jobs.market_family_mapping_sync
.\.venv\Scripts\python.exe -m app.jobs.open_position_price_refresh
.\.venv\Scripts\python.exe -m app.jobs.runner --job daily-setup --target-date today_et
.\.venv\Scripts\python.exe -m app.jobs.runner --job candidate-sweep --target-date today_et --min-time-to-start-minutes 45 --max-time-to-start-minutes 180 --sweep-label rolling_pregame_window
.\.venv\Scripts\python.exe -m app.jobs.runner --job spread-audit --target-date today_et --min-time-to-start-minutes 45 --max-time-to-start-minutes 180
.\.venv\Scripts\python.exe -m app.jobs.runner --job price-refresh --target-date today_et
.\.venv\Scripts\python.exe -m app.jobs.runner --job settlement --target-date yesterday_et
.\.venv\Scripts\python.exe -m app.jobs.runner --job governance
.\.venv\Scripts\python.exe -m app.workers.kalshi_ws_paper --once
```

You can pass a specific date to the MLB schedule job:

```powershell
.\.venv\Scripts\python.exe -m app.jobs.mlb_schedule_sync 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.mlb_results_sync 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.paper_settlement_sync 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.mlb_feature_sync 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.model_feature_snapshot_backfill 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.market_family_discovery 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.market_family_mapping_sync 2026-06-24
```

PR 3c exposes these internal API run endpoints:

- `POST /v1/sync/mlb-schedule`
- `POST /v1/sync/mlb-results?target_date=YYYY-MM-DD`
- `POST /v1/sync/kalshi-markets`
- `POST /v1/run/paper-candidate-engine?target_date=YYYY-MM-DD`
- `POST /v1/run/paper-settlement-sync?target_date=YYYY-MM-DD`
- `POST /v1/run/balance-snapshot`
- `POST /v1/run/model-governance`
- `GET /v1/model/governance/status`
- `GET /v1/model/features/coverage?date=YYYY-MM-DD`
- `GET /v1/model/features/detail?date=YYYY-MM-DD`
- `GET /v1/model/starter-status?date=YYYY-MM-DD`
- `GET /v1/model/parameters/active`
- `GET /v1/model/sources/status`
- `GET /v1/model/training/latest`
- `GET /v1/model/predictions?date=YYYY-MM-DD`
- `GET /v1/model/predictions/today`
- `POST /v1/sync/mlb-features?target_date=YYYY-MM-DD`
- `POST /v1/sync/mlb-features?target_date=YYYY-MM-DD&include_modules=all`
- `POST /v1/sync/mlb-starters?target_date=today_et`
- `POST /v1/sync/mlb-team-features?target_date=YYYY-MM-DD`
- `POST /v1/sync/mlb-pitcher-features?target_date=YYYY-MM-DD`
- `POST /v1/sync/mlb-lineups?target_date=YYYY-MM-DD`
- `POST /v1/sync/mlb-bullpen-features?target_date=YYYY-MM-DD`
- `POST /v1/sync/weather?target_date=YYYY-MM-DD`
- `POST /v1/sync/travel-schedule?target_date=YYYY-MM-DD`
- `POST /v1/run/model-feature-snapshot-backfill?target_date=YYYY-MM-DD`
- `POST /v1/run/training-eligibility-repair`
- `POST /v1/run/open-position-price-refresh`
- `POST /v1/admin/paper-trading/reset-epoch`
- `POST /v1/jobs/run/daily-setup?target_date=YYYY-MM-DD`
- `POST /v1/jobs/run/candidate-sweep?target_date=YYYY-MM-DD&min_time_to_start_minutes=45&max_time_to_start_minutes=180&sweep_label=rolling_pregame_window`
- `POST /v1/jobs/run/spread-audit?target_date=YYYY-MM-DD&min_time_to_start_minutes=45&max_time_to_start_minutes=180`
- `POST /v1/jobs/run/price-refresh?target_date=YYYY-MM-DD`
- `POST /v1/jobs/run/settlement?target_date=YYYY-MM-DD`
- `POST /v1/jobs/run/governance`
- `POST /v1/jobs/run/full-paper-cycle?target_date=YYYY-MM-DD`
- `GET /v1/ws/status`
- `POST /v1/run/market-family-discovery?target_date=YYYY-MM-DD`
- `POST /v1/sync/market-family-mappings?target_date=YYYY-MM-DD`
- `GET /v1/market-families/discovery?date=YYYY-MM-DD`
- `GET /v1/market-families/discovery-preview?date=YYYY-MM-DD`
- `GET /v1/market-families/mappings?date=YYYY-MM-DD`
- `GET /v1/kalshi/resolve-preview?date=YYYY-MM-DD`

PR3d hotfix 2 adds time-windowed candidate sweeps for production paper observation. When min/max are supplied, the sweep only scores games whose first pitch is inside that minutes-to-start window. Games outside the window are counted in the job result and dashboard, but they do not create candidates or paper trades. A dry-run option (`dry_run_candidates_only=true`) saves labeled candidate diagnostics without opening trades or refreshing open-position marks.

PR3f makes the repeating candidate-sweep job feature-cache-only. Heavy MLB feature ingestion, pybaseball, FanGraphs, Statcast/Savant, Open-Meteo, and full `mature_mlb_features_v2` snapshot sync belong to daily setup or explicit feature-sync endpoints, not the 30-minute sweep. Candidate-sweep returns `feature_sync_mode=cache_only`, `feature_sync_skipped=true`, and `cached_features` diagnostics; if target-date mature feature snapshots are missing, it exits cleanly with `no_candidates_missing_feature_snapshots` instead of starting source ingestion.

PR3g adds candidate-stage quality and EV diagnostics for paper observation. Candidate and sweep results now preserve `raw_feature_snapshot_data_quality`, compute separate `paper_observation_data_quality` from explicit candidate-stage market context, and report module status/role/contribution/penalty, quality block reasons, EV/fee/edge decomposition, deduped game/scope/family opportunity counts, and top counterfactual candidates blocked by quality. The observation threshold is not lowered, candidate-sweep remains cache-only, and full-game spread remains disabled by default.

PR3h repairs probable-starter freshness. Lightweight MLB schedule sync now requests `probablePitcher(note)` and preserves cached live feed payloads instead of replacing them with thinner schedule rows. A protected `POST /v1/sync/mlb-starters?target_date=today_et` refreshes only official MLB starter identity, pitcher game-log cache rows, and target-date mature feature snapshots. It reads schedule probable pitchers, supplements with per-game live feed `gameData.probablePitchers` and boxscore starter data, prefers live feed/boxscore identities over stale schedule probables when both exist, never fabricates missing starters, and exposes `GET /v1/model/starter-status?date=YYYY-MM-DD` plus dashboard `starter_hydration` counts. Candidate-sweep still does not run full feature sync; it only runs this bounded starter refresh after cached mature feature snapshots exist.

PR3d hotfix 3 adds a protected spread-audit job and stricter display/correlation diagnostics. The audit verifies spread side, line, inning scope, and settlement support from Kalshi raw text fields instead of trusting the ticker alone. It does not create paper trades.

```powershell
.\.venv\Scripts\python.exe -m app.jobs.runner --job spread-audit --target-date today_et --min-time-to-start-minutes 45 --max-time-to-start-minutes 180
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/jobs/run/spread-audit?target_date=today_et&min_time_to_start_minutes=45&max_time_to_start_minutes=180"
```

Expected display examples:

- Actual spread contract: `NO ON PITTSBURGH PIRATES -1.5 FULL GAME`
- Normalized spread equivalent: `SEATTLE MARINERS +1.5 FULL GAME EQUIVALENT`
- Total NO equivalent: `UNDER 8 FULL GAME EQUIVALENT`, not `+8`

Candidate sweep diagnostics now include same-game same-scope correlation counts and risk-limit basis values. Risk caps are computed from the active epoch portfolio value at sweep time, and dashboard summaries label that basis explicitly. This hotfix does not enable live trading, spread paper trading, or new production cron services; keep spread trading disabled until audit output is manually validated against the Kalshi UI.

For local development, these endpoints can run without a key when `APP_ENV=local` and `BACKEND_API_KEY` is empty. For any public or deployed backend, set `BACKEND_API_KEY` and call these endpoints with an `X-API-Key` header.

`/v1/sync/kalshi-markets` now uses MLB games as its primary input and resolves the empirically observed `KXMLBGAME` full-game winner family first. Market sync, resolve preview, market-family discovery, and open-position mark refresh read from `KALSHI_MARKET_DATA_BASE_URL` by default without credentials. `KALSHI_REST_BASE_URL` and `KALSHI_ENV` remain the safe demo/execution context.

PR3a fix3 through PR3c fix6 market-family discovery uses low-request deterministic probes. Winner families use exact market-ticker lookups where the team selection is part of the ticker. Spread and total families use `event_ticker` filtering because their exact market ticker includes line/side details that should not be guessed. Discovery no longer batch-probes guessed spread/total market tickers. PR3c fix5/fix6 keep cache-first protection and rate-limit cooldowns: `POST /v1/run/market-family-discovery?target_date=YYYY-MM-DD&force_refresh=false` reuses a recent finalized discovery run by default, splits ticker batches with `KALSHI_DISCOVERY_MAX_BATCH_SIZE`, spaces requests with `KALSHI_DISCOVERY_REQUEST_SPACING_MS`, and persists `partial_rate_limited` plus `cooldown_until` when Kalshi returns 429s.

PR3b adds `market_family_mapping_sync`, which consumes the latest finalized discovery run and promotes only parseable rows for `full_game_winner`, `full_game_spread`, `full_game_total`, `first_five_winner`, `first_five_spread`, and `first_five_total` to `paper_supported`. Rows with missing line/selection/settlement metadata stay `needs_review`; `KXMLBTEAMTOTAL`, MVE/multivariate, sportsbook, and guessed prefixes stay unsupported.

Paper settlement supports full-game winner, full-game spread, full-game total, first-five winner, first-five spread, and first-five total when the row is `paper_supported`. First-five settlement requires MLB linescore innings; if missing, the trade stays open with a skipped reason. Paper candidate generation now estimates conservative configurable Kalshi fees before trade selection using `KALSHI_TRADE_FEE_RATE * quantity * price * (1 - price)`, rounded upward per `KALSHI_FEE_ROUNDING_MODE`; this remains a paper-trading estimate until live fill fees are available.

The PR3c fix3 through PR3f feature pipeline uses no-key public MLB, weather, and pybaseball sources by default. `FEATURE_SYNC_ENABLE_NETWORK_SOURCES=true` makes feature sync hydrate the MLB Stats API schedule/feed, write raw team, pitcher, bullpen, lineup, weather, park, travel, and final `mature_mlb_features_v2` snapshot rows, and expose `/v1/model/sources/status` diagnostics. PR3c fix6 makes MLB Stats API the primary baseball feature source for schedule, probable pitchers, team season stats, team game logs, pitcher season stats, pitcher game logs, stat splits, and boxscore/live feed payloads. Statcast/Savant data from pybaseball is secondary enrichment for contact quality. FanGraphs-backed pybaseball functions are optional; HTTP 403 or other upstream errors must degrade the sync but must not block MLB Stats API feature availability. Derived `derived_homerun_v2` rows remain partial fallback only. If network sync is disabled, network-backed module endpoints return `validation_status=skipped_network_disabled` with zero inserted/updated rows rather than pretending ingestion succeeded. Open-Meteo weather uses `OPEN_METEO_BASE_URL=https://api.open-meteo.com/v1`; optional injury, lineup, and weather provider keys remain optional and are not required for this PR. Feature endpoints accept optional `refresh_schedule=true/false`; module syncs skip schedule hydration by default when target-date games already exist. `/v1/model/features/coverage` and `/v1/model/features/detail` now expose all 17 mature modules through `core_modules`, `completeness_summary`, and `module_completeness` so operators can see available, partial, missing, and unavailable counts without changing candidate gates. Repeating candidate sweeps read this cached feature state only and do not call full feature sync.

`/v1/model/sources/status` reports `pybaseball_available`, `pybaseball_version`, `pybaseball_module_path`, import errors, last pybaseball sync/error, attempted functions, DB cache status, `advanced_stats_status`, and latest 17-module feature completeness. If pybaseball import or source calls fail, sync returns structured degraded JSON instead of a blank 500; MLB Stats API rows should still populate where the public Stats API returns data. Known limits remain: true bullpen reliever workload, pitch mix, catcher defense, injuries, and optional provider data are partial/missing unless a future PR adds reliable public-source ingestion.

The PR3c fix2/fix3 model pipeline uses `mature_mlb_run_distribution_v2`, a transparent paper-only run-distribution model that scores full-game and first-five winner, spread, and total families from `mature_mlb_features_v2` snapshots and additive feature cache tables. Feature snapshots record source availability as `available`, `partial`, `missing`, or `unavailable`; no sportsbook odds, team totals, umpire data, or fake production inputs are introduced. Candidate runs save every supported prediction for learning, but paper trades require fresh executable prices, clean mappings, trusted settlement metadata, data quality, probability edge, fee-adjusted net EV, and line/correlation selection before caps. Model governance creates an active baseline parameter version, records training datasets, fits bounded challenger parameter/calibration offsets only when enough resolved samples exist, and simulates threshold policy changes before promotion.

## Local Frontend

From the repo root:

```powershell
npm install
Copy-Item apps/web/.env.example apps/web/.env.local
npm --workspace apps/web run dev
```

The dashboard should be available at `http://localhost:3000`.

Frontend checks:

```powershell
npm --workspace apps/web run lint
npm --workspace apps/web run typecheck
npm --workspace apps/web run build
```

## Database Migrations

The backend can boot without `DATABASE_URL` for local UI work. To use the PR 2 data jobs and database-backed dashboard responses, configure PostgreSQL and run migrations:

```powershell
cd apps/api
$env:DATABASE_URL="postgresql+psycopg://USER:PASSWORD@HOST:PORT/DATABASE"
alembic upgrade head
```

PR 2 adds migration `0002_pr2_data_layer.py` for raw MLB payloads, Kalshi orderbook fields, mapping rationale, paper candidate fields, and paper trade mark-to-market fields.

PR 2.5 adds migration `0003_pr2_5_targeted_kalshi_resolver.py` for MLB team abbreviations, raw Kalshi status, resolver strategy, and validation status.

PR 3 adds migration `0004_pr3_results_model.py` for paper settlement fields, readable contract labels, feature/model metadata, snapshot type, and settlement-to-paper-trade linkage.

PR 3a adds migration `0005_pr3a_discovery.py` for market-family discovery audit tables and paper-trade current mark timestamps.

PR 3b adds migration `0006_pr3b_family_wiring.py` for market-family metadata on markets, mappings, candidates, and paper trades.

PR 3c adds migration `0007_pr3c_model_governance.py` for mature MLB feature snapshots, model prediction runs/outputs, governance events, and candidate/model feature metadata.

PR3c hotfix adds migration `0008_pr3c_fee_date_scope.py` for target-date, executable-price, price-staleness, fee, edge, and net-EV diagnostics on model candidates and prediction outputs.

PR3c fix2 adds migration `0009_pr3c_fix2_features.py` for additive MLB feature cache tables, model parameter versions, training datasets, and threshold policy versions.

PR3d adds migration `0010_pr3d_paper_ops.py` for paper observation epochs, active-epoch links, job run audits, WebSocket worker status, gate diagnostics, and fixed-risk paper sizing fields.

PR3i adds migration `0011_pr3i_decision_length.py` to widen `model_candidates.decision` so post-eligibility rejection reasons such as `no_trade_same_game_scope_correlation_not_best` persist safely during non-dry candidate sweeps.

## PR3d Paper Ops Validation

After deploy and migration, reset the active paper observation epoch:

```powershell
$body = @{
  archive_current_as = "pr3d_bad_spread_parser_validation"
  new_epoch = "pr3d_paper_observation_v2"
  starting_balance = 500.00
  archive_open_positions = $true
  reset_dashboard_metrics = $true
  confirmation = "RESET_PAPER_EPOCH"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"; "Content-Type"="application/json"} -Body $body "https://YOUR-RAILWAY-API/v1/admin/paper-trading/reset-epoch"
```

Then verify:

```powershell
Invoke-RestMethod https://YOUR-RAILWAY-API/health
Invoke-RestMethod https://YOUR-RAILWAY-API/v1/system/status
Invoke-RestMethod "https://YOUR-RAILWAY-API/v1/dashboard/summary"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/jobs/run/daily-setup?target_date=2026-06-27"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/jobs/run/candidate-sweep?target_date=2026-06-27"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/jobs/run/price-refresh?target_date=2026-06-27"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/jobs/run/settlement?target_date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/jobs/run/governance"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/ws/status"
```

Expected dashboard result after reset: active epoch `PR3D PAPER OBSERVATION V2`, portfolio value `500.00`, cash `500.00`, open positions `0`, closed positions `0`, P/L `$0.00`, record `0-0-0`, and archived contaminated spread-validation rows absent from active metrics. Candidate-sweep responses should include independent gate diagnostics, by-side counts, by-family/by-scope breakdowns, and aggregate risk-cap usage. Spread candidates should not create paper trades unless `PAPER_SPREAD_TRADING_ENABLED=true`.

## PR 3c Production Validation

After deploy and migration:

```powershell
Invoke-RestMethod https://YOUR-RAILWAY-API/health
Invoke-RestMethod https://YOUR-RAILWAY-API/v1/system/status
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/sync/mlb-schedule
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/kalshi/resolve-preview?date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/sync/kalshi-markets
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/run/paper-candidate-engine?target_date=2026-06-27"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/sync/mlb-results
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/paper-settlement-sync
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/balance-snapshot
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/model-governance
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/model/governance/status
```

Add these PR 3c market/model checks after the base flow:

```powershell
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/mlb-features?target_date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/mlb-team-features?target_date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/mlb-pitcher-features?target_date=2026-06-26"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/sources/status"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/features/coverage?date=2026-06-26"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/features/detail?date=2026-06-26"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/predictions?date=2026-06-27"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/model/predictions/today
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/run/market-family-discovery?target_date=2026-06-26"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/market-families/discovery?date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/market-family-mappings?target_date=2026-06-26"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/market-families/mappings?date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/open-position-price-refresh
```

Expected result: health and system status remain safe, `/v1/system/status` reports `config.kalshi_market_data_source: "production_public_market_data"` and `config.kalshi_market_data_base_kind: "production_public_market_data"` when the default public market-data URL is used, Alembic is at `0009_pr3c_fix2_features`, repeated feature syncs are idempotent, `validation_status=degraded_with_errors` is returned for source issues instead of a 500, hydration counters are present, source status shows latest attempted sync/errors and 17-module feature completeness, resolve preview keeps exact `KXMLBGAME` matches confirmed for paper, market-family discovery returns a structured `by_family` report, mapping sync promotes only parseable validated rows to `paper_supported`, feature coverage/detail show cache-module statuses plus `core_modules`, `completeness_summary`, and `module_completeness`, candidate runs return the requested `target_date`, `prediction_run_target_date`, and active parameter version, prediction outputs include nonzero fee estimates for normal prices, cap counts are not the primary selector unless caps are truly hit, open-position price refresh updates REST last marks for open paper positions only, the dashboard shows `GAME STATUS`, `LAST MARK TIME`, closed positions by selected date, chart range/P&L controls work, and model governance reports mature feature/model status without enabling live trading.

PR3b deterministic discovery validation: the registry contains `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`, `KXMLBF5`, `KXMLBF5SPREAD`, and `KXMLBF5TOTAL`. Normal market-family discovery does not redundantly probe `KXMLBGAME`, because full-game winner is handled by targeted sync/resolve. It does not probe guessed legacy variants or `KXMLBTEAMTOTAL`. If candidate probes return 404/no-match responses, the discovery POST should still return structured JSON with status `completed` or `partial_error`. The report GET should return the latest finalized run, not a stale running row. `markets_found=0` and zero `market_family_discovery_items` are valid when no markets are found, but `market_family_discovery_runs.raw_summary` must include attempted ticker counts, no-match counts, probe details, request/rate-limit metrics, and any warnings/errors. Mapping sync is the only step that can make non-winner families paper-supported, and only when line/selection/settlement metadata parses cleanly.

## Deployment

- Railway backend setup: see `docs/RAILWAY_SETUP.md`.
- Vercel frontend setup: see `docs/VERCEL_SETUP.md`.
- Operating rules and validation checklists: see `docs/OPERATIONS.md`.

Every future PR must update `PROJECT_CONTEXT.md`.

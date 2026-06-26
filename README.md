# HOMERUN

HOMERUN is a Kalshi-native MLB paper-trading system and dashboard. The current version includes the deployable PR 1 foundation, PR 2 data layer, PR 2.5 targeted Kalshi MLB resolver, PR 3 paper results/model infrastructure, PR 3a discovery/operator repairs, PR 3b validated market-family paper wiring, and PR 3c full MLB model governance: MLB slate/results ingestion, targeted Kalshi `KXMLBGAME` market resolution, auditable game-to-market mapping, mature paper-only model candidates, paper settlement for validated MLB market families, market-family discovery audits, portfolio snapshots, and a light trading-terminal dashboard.

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
- PR 3c scores validated MLB market-family rows with `mature_mlb_run_distribution_v1` and `mature_mlb_features_v1`.
- Team totals, multivariate/MVE markets, sportsbook data, and guessed/retired prefixes remain out of scope.
- Paper candidates are capped by slate, game, market family, open-position count, and correlated game/family exposure.
- Open-position current price is a REST last mark, not a WebSocket live price.
- `PAPER_STARTING_BALANCE=1000.00` by default.
- Governance skips training/calibration/promotion until clean resolved-sample thresholds are met.

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
- `POST /v1/run/paper-candidate-engine`
- `POST /v1/run/paper-settlement-sync?target_date=YYYY-MM-DD`
- `POST /v1/run/balance-snapshot`
- `POST /v1/run/model-governance`
- `GET /v1/model/governance/status`
- `GET /v1/model/features/coverage?date=YYYY-MM-DD`
- `GET /v1/model/predictions/today`
- `POST /v1/sync/mlb-features?target_date=YYYY-MM-DD`
- `POST /v1/run/model-feature-snapshot-backfill?target_date=YYYY-MM-DD`
- `POST /v1/run/training-eligibility-repair`
- `POST /v1/run/open-position-price-refresh`
- `POST /v1/run/market-family-discovery?target_date=YYYY-MM-DD`
- `POST /v1/sync/market-family-mappings?target_date=YYYY-MM-DD`
- `GET /v1/market-families/discovery?date=YYYY-MM-DD`
- `GET /v1/market-families/discovery-preview?date=YYYY-MM-DD`
- `GET /v1/market-families/mappings?date=YYYY-MM-DD`
- `GET /v1/kalshi/resolve-preview?date=YYYY-MM-DD`

For local development, these endpoints can run without a key when `APP_ENV=local` and `BACKEND_API_KEY` is empty. For any public or deployed backend, set `BACKEND_API_KEY` and call these endpoints with an `X-API-Key` header.

`/v1/sync/kalshi-markets` now uses MLB games as its primary input and resolves the empirically observed `KXMLBGAME` full-game winner family first. Market sync, resolve preview, market-family discovery, and open-position mark refresh read from `KALSHI_MARKET_DATA_BASE_URL` by default without credentials. `KALSHI_REST_BASE_URL` and `KALSHI_ENV` remain the safe demo/execution context.

PR3a fix3 market-family discovery uses low-request deterministic probes. It first batches exact scheduled-time ticker lookups with `GET /markets?tickers=...&mve_filter=exclude`, then optionally tries capped fallback time offsets for no-match families, then uses `event_ticker` filtering only as a secondary fallback. The discovery run summary reports request counts, batching savings, retry counts, rate-limit counts, and whether the 429 circuit breaker stopped remaining probes.

PR3b adds `market_family_mapping_sync`, which consumes the latest finalized discovery run and promotes only parseable rows for `full_game_winner`, `full_game_spread`, `full_game_total`, `first_five_winner`, `first_five_spread`, and `first_five_total` to `paper_supported`. Rows with missing line/selection/settlement metadata stay `needs_review`; `KXMLBTEAMTOTAL`, MVE/multivariate, sportsbook, and guessed prefixes stay unsupported.

Paper settlement supports full-game winner, full-game spread, full-game total, first-five winner, first-five spread, and first-five total when the row is `paper_supported`. First-five settlement requires MLB linescore innings; if missing, the trade stays open with a skipped reason. Fees remain structured but zero until the exact Kalshi fee formula is implemented.

The PR 3c model pipeline uses `mature_mlb_run_distribution_v1`, a transparent paper-only run-distribution model that scores full-game and first-five winner, spread, and total families from `mature_mlb_features_v1` snapshots. Feature snapshots record source availability as `available`, `partial`, `missing`, or `unavailable`; no sportsbook odds, team totals, umpire data, or fake production inputs are introduced. Model governance records skipped training/calibration/promotion until enough clean resolved mature candidates exist for chronological validation, reliability metrics, and calibration checks.

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

## PR 3c Production Validation

After deploy and migration:

```powershell
Invoke-RestMethod https://YOUR-RAILWAY-API/health
Invoke-RestMethod https://YOUR-RAILWAY-API/v1/system/status
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/sync/mlb-schedule
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/kalshi/resolve-preview?date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/sync/kalshi-markets
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/paper-candidate-engine
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/sync/mlb-results
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/paper-settlement-sync
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/balance-snapshot
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/model-governance
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/model/governance/status
```

Add these PR 3c market/model checks after the base flow:

```powershell
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/mlb-features?target_date=2026-06-26"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/features/coverage?date=2026-06-26"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/model/predictions/today
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/run/market-family-discovery?target_date=2026-06-26"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/market-families/discovery?date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/market-family-mappings?target_date=2026-06-26"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/market-families/mappings?date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/open-position-price-refresh
```

Expected result: health and system status remain safe, `/v1/system/status` reports `config.kalshi_market_data_source: "production_public_market_data"` and `config.kalshi_market_data_base_kind: "production_public_market_data"` when the default public market-data URL is used, Alembic is at `0007_pr3c_model_governance`, resolve preview keeps exact `KXMLBGAME` matches confirmed for paper, market-family discovery returns a structured `by_family` report, mapping sync promotes only parseable validated rows to `paper_supported`, open-position price refresh updates REST last marks for open paper positions only, the dashboard shows `GAME STATUS`, `LAST MARK TIME`, closed positions by selected date, chart range/P&L controls work, and model governance reports mature feature/model status without enabling live trading.

PR3b deterministic discovery validation: the registry contains `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`, `KXMLBF5`, `KXMLBF5SPREAD`, and `KXMLBF5TOTAL`. Normal market-family discovery does not redundantly probe `KXMLBGAME`, because full-game winner is handled by targeted sync/resolve. It does not probe guessed legacy variants or `KXMLBTEAMTOTAL`. If candidate probes return 404/no-match responses, the discovery POST should still return structured JSON with status `completed` or `partial_error`. The report GET should return the latest finalized run, not a stale running row. `markets_found=0` and zero `market_family_discovery_items` are valid when no markets are found, but `market_family_discovery_runs.raw_summary` must include attempted ticker counts, no-match counts, probe details, request/rate-limit metrics, and any warnings/errors. Mapping sync is the only step that can make non-winner families paper-supported, and only when line/selection/settlement metadata parses cleanly.

## Deployment

- Railway backend setup: see `docs/RAILWAY_SETUP.md`.
- Vercel frontend setup: see `docs/VERCEL_SETUP.md`.
- Operating rules and validation checklists: see `docs/OPERATIONS.md`.

Every future PR must update `PROJECT_CONTEXT.md`.

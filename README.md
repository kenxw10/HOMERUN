# HOMERUN

HOMERUN is a Kalshi-native MLB paper-trading system and dashboard. The current version includes the deployable PR 1 foundation, PR 2 data layer, PR 2.5 targeted Kalshi MLB resolver, and PR 3 paper results/model infrastructure: MLB slate/results ingestion, targeted Kalshi `KXMLBGAME` market resolution, auditable game-to-market mapping, conservative paper candidates, full-game winner paper settlement, portfolio snapshots, and a light trading-terminal dashboard.

This is not a sportsbook app. It does not use DraftKings, FanDuel, Odds API, or sportsbook odds behavior. Future trading logic should use Kalshi yes/no contract math, account for fees, and assume hold-to-settlement unless a later PR changes that context deliberately.

## Apps

- `apps/api` - Python FastAPI backend.
- `apps/web` - Next.js TypeScript dashboard.

## Safe Defaults

- `PAPER_TRADING=true`
- `LIVE_TRADING_ENABLED=false`
- `EXECUTION_KILL_SWITCH=true`
- `KALSHI_ENV=demo`
- Kalshi credentials are optional for PR 3 paper-mode discovery and must not be production credentials unless a later PR explicitly changes the safety plan.
- `BACKEND_API_KEY` is optional only for local development. Public or deployed backends must set it, and internal POST run endpoints require `X-API-Key`.
- Broad Kalshi discovery is diagnostic-only and disabled by default with `KALSHI_ENABLE_BROAD_DISCOVERY=false`.
- `PAPER_STARTING_BALANCE=1000.00` by default.
- `MODEL_TRAINING_MIN_SAMPLES=100` prevents trained-model promotion on tiny samples.

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

PR 3 keeps safe one-shot worker commands. They write MLB games/results, targeted Kalshi markets, mappings, model candidates, paper trades, settlements, balance snapshots, and model governance records to the configured database. They do not place live orders.

From `apps/api` after installing backend dependencies:

```powershell
.\.venv\Scripts\python.exe -m app.jobs.mlb_schedule_sync
.\.venv\Scripts\python.exe -m app.jobs.kalshi_market_sync
.\.venv\Scripts\python.exe -m app.jobs.paper_candidate_engine
.\.venv\Scripts\python.exe -m app.jobs.mlb_results_sync
.\.venv\Scripts\python.exe -m app.jobs.paper_settlement_sync
.\.venv\Scripts\python.exe -m app.jobs.balance_snapshot
.\.venv\Scripts\python.exe -m app.jobs.model_governance
```

You can pass a specific date to the MLB schedule job:

```powershell
.\.venv\Scripts\python.exe -m app.jobs.mlb_schedule_sync 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.mlb_results_sync 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.paper_settlement_sync 2026-06-24
```

PR 3 exposes these internal API run endpoints:

- `POST /v1/sync/mlb-schedule`
- `POST /v1/sync/mlb-results?target_date=YYYY-MM-DD`
- `POST /v1/sync/kalshi-markets`
- `POST /v1/run/paper-candidate-engine`
- `POST /v1/run/paper-settlement-sync?target_date=YYYY-MM-DD`
- `POST /v1/run/balance-snapshot`
- `POST /v1/run/model-governance`
- `GET /v1/kalshi/resolve-preview?date=YYYY-MM-DD`

For local development, these endpoints can run without a key when `APP_ENV=local` and `BACKEND_API_KEY` is empty. For any public or deployed backend, set `BACKEND_API_KEY` and call these endpoints with an `X-API-Key` header.

`/v1/sync/kalshi-markets` now uses MLB games as its primary input and resolves the empirically observed `KXMLBGAME` full-game winner family first. Spread, total, and first-five families are still pending discovery and are not faked.

Paper settlement currently supports only `full_game_winner`. It determines the selected team from the Kalshi ticker suffix, settles YES/NO contracts from final MLB scores, and uses hold-to-settlement P/L. Fees remain structured but zero until the exact Kalshi fee formula is implemented.

The PR 3 model pipeline uses `heuristic_full_game_winner_v1`, a deterministic paper-only model with explicit feature JSON and missing-source markers. Model governance records skipped training/calibration runs until enough clean resolved candidates exist for chronological validation.

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

## PR 3 Production Validation

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
```

Expected result: health and system status remain safe, Alembic is at `0004_pr3_results_model`, resolve preview returns structured per-game results with `ok=true`, Kalshi sync returns a structured summary, the candidate engine creates candidates with non-placeholder probabilities, results sync updates completed games, settlement settles completed full-game winner paper trades, balance snapshots populate the portfolio chart, and model governance records either a trained/promoted model or a clear skipped reason due to insufficient samples.

## Deployment

- Railway backend setup: see `docs/RAILWAY_SETUP.md`.
- Vercel frontend setup: see `docs/VERCEL_SETUP.md`.
- Operating rules and validation checklists: see `docs/OPERATIONS.md`.

Every future PR must update `PROJECT_CONTEXT.md`.

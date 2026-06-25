# HOMERUN

HOMERUN is a Kalshi-native MLB paper-trading system and dashboard. The current version includes the deployable PR 1 foundation, the PR 2 data layer, and the PR 2.5 targeted Kalshi MLB resolver: MLB slate ingestion, targeted Kalshi `KXMLBGAME` market resolution, auditable game-to-market mapping, conservative paper candidates, and a light trading-terminal dashboard.

This is not a sportsbook app. It does not use DraftKings, FanDuel, Odds API, or sportsbook odds behavior. Future trading logic should use Kalshi yes/no contract math, account for fees, and assume hold-to-settlement unless a later PR changes that context deliberately.

## Apps

- `apps/api` - Python FastAPI backend.
- `apps/web` - Next.js TypeScript dashboard.

## Safe Defaults

- `PAPER_TRADING=true`
- `LIVE_TRADING_ENABLED=false`
- `EXECUTION_KILL_SWITCH=true`
- `KALSHI_ENV=demo`
- Kalshi credentials are optional for PR 2.5 targeted discovery and must not be production credentials unless a later PR explicitly changes the safety plan.
- `BACKEND_API_KEY` is optional only for local development. Public or deployed backends must set it, and internal POST run endpoints require `X-API-Key`.
- Broad Kalshi discovery is diagnostic-only and disabled by default with `KALSHI_ENABLE_BROAD_DISCOVERY=false`.

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

PR 2.5 keeps the safe one-shot worker commands. They write MLB games, targeted Kalshi markets, mappings, model candidates, and paper trades to the configured database. They do not place live orders.

From `apps/api` after installing backend dependencies:

```powershell
.\.venv\Scripts\python.exe -m app.jobs.mlb_schedule_sync
.\.venv\Scripts\python.exe -m app.jobs.kalshi_market_sync
.\.venv\Scripts\python.exe -m app.jobs.paper_candidate_engine
```

You can pass a specific date to the MLB schedule job:

```powershell
.\.venv\Scripts\python.exe -m app.jobs.mlb_schedule_sync 2026-06-24
```

PR 2 also exposes internal API run endpoints:

- `POST /v1/sync/mlb-schedule`
- `POST /v1/sync/kalshi-markets`
- `POST /v1/run/paper-candidate-engine`
- `GET /v1/kalshi/resolve-preview?date=YYYY-MM-DD`

For local development, these endpoints can run without a key when `APP_ENV=local` and `BACKEND_API_KEY` is empty. For any public or deployed backend, set `BACKEND_API_KEY` and call these endpoints with an `X-API-Key` header.

`/v1/sync/kalshi-markets` now uses MLB games as its primary input and resolves the empirically observed `KXMLBGAME` full-game winner family first. Spread, total, and first-five families are still pending discovery and are not faked.

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

## PR 2.5 Production Validation

After deploy and migration:

```powershell
Invoke-RestMethod https://YOUR-RAILWAY-API/health
Invoke-RestMethod https://YOUR-RAILWAY-API/v1/system/status
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/sync/mlb-schedule
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/kalshi/resolve-preview?date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/sync/kalshi-markets
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/paper-candidate-engine
```

Expected result: health and system status remain safe, resolve preview shows attempted `KXMLBGAME` tickers per MLB game, Kalshi sync returns a structured summary instead of a blank upstream error, and missing Kalshi matches exit cleanly.

## Deployment

- Railway backend setup: see `docs/RAILWAY_SETUP.md`.
- Vercel frontend setup: see `docs/VERCEL_SETUP.md`.
- Operating rules and validation checklists: see `docs/OPERATIONS.md`.

Every future PR must update `PROJECT_CONTEXT.md`.

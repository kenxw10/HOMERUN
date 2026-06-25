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
KALSHI_WS_BASE_URL=wss://demo-api.kalshi.co/trade-api/ws/v2
MLB_STATS_BASE_URL=https://statsapi.mlb.com/api/v1
MARKET_DISCOVERY_ENABLED=true
KALSHI_ENABLE_BROAD_DISCOVERY=false
KALSHI_MARKET_SYNC_MAX_PAGES=2
KALSHI_MARKET_SYNC_LIMIT=100
MARKET_FAMILY_DISCOVERY_ENABLED=true
MARKET_FAMILY_DISCOVERY_MAX_PAGES=2
OPEN_POSITION_PRICE_REFRESH_ENABLED=true
PAPER_CANDIDATE_ENGINE_ENABLED=true
DEFAULT_PAPER_CONTRACTS=1
PAPER_STARTING_BALANCE=1000.00
MODEL_TRAINING_MIN_SAMPLES=100
DASHBOARD_TIMEZONE=America/New_York
BACKEND_API_KEY=replace-with-a-long-random-secret
```

Use the exact Vercel dashboard origin for `CORS_ORIGINS`, without a trailing slash. Example: `https://homerun.vercel.app`.

9. Required for Railway: set `BACKEND_API_KEY` to a long random value. Internal POST run endpoints reject unauthenticated requests outside local development.
10. Do not add production Kalshi credentials in PR 3a.
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

## Migration Command

When the Railway database is ready, run migrations from the backend service context:

```powershell
alembic upgrade head
```

If migration fails, check that `DATABASE_URL` exists and points to the Railway PostgreSQL service.

## PR 3 One-Off Job Commands

Run these from the Railway backend service shell after migrations succeed:

```powershell
python -m app.jobs.mlb_schedule_sync
python -m app.jobs.kalshi_market_sync
python -m app.jobs.paper_candidate_engine
python -m app.jobs.mlb_results_sync
python -m app.jobs.paper_settlement_sync
python -m app.jobs.balance_snapshot
python -m app.jobs.model_governance
python -m app.jobs.market_family_discovery
python -m app.jobs.open_position_price_refresh
```

These commands create database records for the dashboard and paper engine. They do not place live orders.

## PR 3a Targeted Resolver, Discovery, And Paper Results Validation

PR 3 keeps Kalshi market sync on targeted MLB resolution and adds paper results/model workflows. Normal production should leave broad discovery disabled:

```powershell
KALSHI_ENABLE_BROAD_DISCOVERY=false
```

After migrations and deploy, validate with the internal API key:

```powershell
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/sync/mlb-schedule
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/kalshi/resolve-preview?date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/sync/kalshi-markets
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/paper-candidate-engine
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/sync/mlb-results
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/paper-settlement-sync
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/balance-snapshot
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/model-governance
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/run/market-family-discovery?target_date=2026-06-26"
Invoke-RestMethod "https://YOUR-RAILWAY-API/v1/market-families/discovery?date=2026-06-26"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} https://YOUR-RAILWAY-API/v1/run/open-position-price-refresh
```

Expected behavior:

- Resolve preview shows attempted `KXMLBGAME` event and market tickers for each MLB game.
- Resolve preview returns `ok=true` with per-game warnings/partial errors when only some games miss.
- Kalshi sync returns a structured summary with mapping counts and actionable error details if Kalshi upstream calls fail.
- Missing matching Kalshi markets should produce a clean summary, not a blank 502.
- Paper candidate engine creates candidates with `heuristic_full_game_winner_v1` probabilities only after confirmed or candidate mappings exist.
- MLB results sync updates final scores/status.
- Paper settlement sync settles completed supported full-game winner paper trades.
- Balance snapshots populate `/v1/dashboard/summary`.
- Model governance records either a trained/promoted model or a clear skipped reason due to insufficient samples.
- PR 3a market-family discovery returns structured `by_family` output but does not trade spread, total, or first-five markets.
- Open-position price refresh updates REST last marks for open paper positions only.

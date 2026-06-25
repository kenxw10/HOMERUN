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
PAPER_CANDIDATE_ENGINE_ENABLED=true
DEFAULT_PAPER_CONTRACTS=1
DASHBOARD_TIMEZONE=America/New_York
BACKEND_API_KEY=replace-with-a-long-random-secret
```

Use the exact Vercel dashboard origin for `CORS_ORIGINS`, without a trailing slash. Example: `https://homerun.vercel.app`.

9. Required for Railway: set `BACKEND_API_KEY` to a long random value. Internal POST run endpoints reject unauthenticated requests outside local development.
10. Do not add production Kalshi credentials in PR 2.
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

## PR 2 One-Off Job Commands

Run these from the Railway backend service shell after migrations succeed:

```powershell
python -m app.jobs.mlb_schedule_sync
python -m app.jobs.kalshi_market_sync
python -m app.jobs.paper_candidate_engine
```

These commands create database records for the dashboard and paper engine. They do not place live orders.

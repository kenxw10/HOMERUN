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
```

Use the exact Vercel dashboard origin for `CORS_ORIGINS`, without a trailing slash. Example: `https://homerun.vercel.app`.

9. Do not add production Kalshi credentials in PR 1.
10. Deploy the service.
11. After deploy, open `/health` on the Railway backend URL.

Expected `/health` result should include:

- `status: "ok"`
- `paper_trading: true`
- `live_trading_enabled: false`

## Migration Command

When the Railway database is ready, run migrations from the backend service context:

```powershell
alembic upgrade head
```

If migration fails, check that `DATABASE_URL` exists and points to the Railway PostgreSQL service.

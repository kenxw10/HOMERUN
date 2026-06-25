# Operations

## Production Validation Checklist

Before treating a deployment as valid:

1. Confirm Railway `/health` returns `status: "ok"`.
2. Confirm Railway `/v1/system/status` does not expose secrets.
3. Confirm `PAPER_TRADING=true`.
4. Confirm `LIVE_TRADING_ENABLED=false`.
5. Confirm `EXECUTION_KILL_SWITCH=true`.
6. Confirm `KALSHI_ENV=demo` for PR 2 unless a later PR explicitly approves production credentials.
7. Confirm Vercel has `NEXT_PUBLIC_API_BASE_URL` pointed at the Railway backend.
8. Confirm Railway `/v1/system/status` reports `database.ready: true` after opening a real database connection.
9. Confirm the Vercel dashboard loads the light terminal UI without a dark theme, admin page, calendar, or sportsbook concepts.
10. Confirm the dashboard shows API connected, paper mode, live trading disabled, kill switch on, and database ready.
11. Confirm `BACKEND_API_KEY` is set in every public/deployed backend environment, and confirm internal POST endpoints reject requests without `X-API-Key`.

## No-Live-Trading Safety Checklist

Live trading must remain disabled until a future PR explicitly adds execution support.

Do not proceed if any of these are false:

1. `LIVE_TRADING_ENABLED=false`.
2. `EXECUTION_KILL_SWITCH=true`.
3. No production Kalshi credentials are configured for PR 2.
4. No code path places live Kalshi orders.
5. Dashboard labels are derived from `/v1/system/status` and show paper mode, live trading disabled, and kill switch on.

Future live execution must include hard environment guards, a kill switch, tests for disabled execution, and explicit documentation in `PROJECT_CONTEXT.md`.

## PR 2 Worker Commands

PR 2 worker commands are explicit one-shot commands. They should be run from the backend service context and should not be hidden inside the web dashboard.

From `apps/api`:

```powershell
.\.venv\Scripts\python.exe -m app.jobs.mlb_schedule_sync
.\.venv\Scripts\python.exe -m app.jobs.kalshi_market_sync
.\.venv\Scripts\python.exe -m app.jobs.paper_candidate_engine
```

Optional dated MLB schedule sync:

```powershell
.\.venv\Scripts\python.exe -m app.jobs.mlb_schedule_sync 2026-06-24
```

The jobs currently cover:

- MLB slate ingestion.
- Kalshi market discovery and orderbook snapshots.
- Auditable MLB game to Kalshi market mapping.
- Candidate scoring with a placeholder probability model.
- Conservative paper-trade simulation.

They do not cover settlement collection, model training, calibration, scheduled automation, or live execution.

Each worker should be idempotent where possible, log risk events, and avoid live order execution unless future safety gates are in place.

## Internal Run Endpoints

The backend also exposes these POST endpoints for controlled operational runs:

- `POST /v1/sync/mlb-schedule`
- `POST /v1/sync/kalshi-markets`
- `POST /v1/run/paper-candidate-engine`

For public or deployed backends, `BACKEND_API_KEY` is required and must be sent as `X-API-Key`. The unauthenticated bypass is only for explicit local development environments. Do not expose these endpoints as public dashboard buttons in PR 2.

## Required Context Updates

Every PR must update `PROJECT_CONTEXT.md`.

At minimum, each PR should document:

- What changed.
- Whether the paper/live trading safety posture changed.
- Any schema or deployment changes.
- Any new assumptions about Kalshi markets, model behavior, fees, settlement, or operations.
- Validation performed.

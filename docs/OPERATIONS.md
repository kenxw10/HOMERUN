# Operations

## Production Validation Checklist

Before treating a deployment as valid:

1. Confirm Railway `/health` returns `status: "ok"`.
2. Confirm Railway `/v1/system/status` does not expose secrets.
3. Confirm `PAPER_TRADING=true`.
4. Confirm `LIVE_TRADING_ENABLED=false`.
5. Confirm `EXECUTION_KILL_SWITCH=true`.
6. Confirm `KALSHI_ENV=demo` for PR 1.
7. Confirm Vercel has `NEXT_PUBLIC_API_BASE_URL` pointed at the Railway backend.
8. Confirm the Vercel dashboard loads without a dark theme, admin page, or calendar.
9. Confirm the contracts table is empty unless real paper-trading data exists.

## No-Live-Trading Safety Checklist

Live trading must remain disabled until a future PR explicitly adds execution support.

Do not proceed if any of these are false:

1. `LIVE_TRADING_ENABLED=false`.
2. `EXECUTION_KILL_SWITCH=true`.
3. No production Kalshi credentials are configured for PR 1.
4. No code path places live Kalshi orders.
5. Dashboard labels show Paper Mode and Live Trading Disabled.

Future live execution must include hard environment guards, a kill switch, tests for disabled execution, and explicit documentation in `PROJECT_CONTEXT.md`.

## Future Cron Workers

Future cron or scheduled workers should be added as separate services or clearly separated commands. They should not be hidden inside the web dashboard.

Recommended future worker categories:

- MLB slate ingestion.
- Kalshi market discovery.
- Market mapping.
- Candidate scoring.
- Paper-trade simulation.
- Settlement and outcome collection.
- Training and calibration runs.

Each worker should be idempotent where possible, log risk events, and avoid live order execution unless future safety gates are in place.

## Required Context Updates

Every PR must update `PROJECT_CONTEXT.md`.

At minimum, each PR should document:

- What changed.
- Whether the paper/live trading safety posture changed.
- Any schema or deployment changes.
- Any new assumptions about Kalshi markets, model behavior, fees, settlement, or operations.
- Validation performed.

# Operations

## Production Validation Checklist

Before treating a deployment as valid:

1. Confirm Railway `/health` returns `status: "ok"`.
2. Confirm Railway `/v1/system/status` does not expose secrets.
3. Confirm `PAPER_TRADING=true`.
4. Confirm `LIVE_TRADING_ENABLED=false`.
5. Confirm `EXECUTION_KILL_SWITCH=true`.
6. Confirm `KALSHI_ENV=demo` for PR 3 unless a later PR explicitly approves production credentials.
7. Confirm `/v1/system/status` reports `config.kalshi_market_data_source: "production_public_market_data"` and `config.kalshi_market_data_base_kind: "production_public_market_data"` when `KALSHI_MARKET_DATA_BASE_URL` uses the default public Kalshi market-data URL.
8. Confirm Vercel has `NEXT_PUBLIC_API_BASE_URL` pointed at the Railway backend.
9. Confirm Railway `/v1/system/status` reports `database.ready: true` after opening a real database connection.
10. Confirm the Vercel dashboard loads the light terminal UI without a dark theme, admin page, calendar, or sportsbook concepts.
11. Confirm the dashboard shows API connected, paper mode, live trading disabled, kill switch on, and database ready.
12. Confirm `BACKEND_API_KEY` is set in every public/deployed backend environment, and confirm internal POST endpoints reject requests without `X-API-Key`.
13. Confirm `KALSHI_ENABLE_BROAD_DISCOVERY=false` unless you are deliberately running bounded diagnostics.
14. Confirm PR 3a discovery-only families are not trade-enabled.
15. Confirm open-position current price is treated as a REST last mark, not WebSocket live price.

## No-Live-Trading Safety Checklist

Live trading must remain disabled until a future PR explicitly adds execution support.

Do not proceed if any of these are false:

1. `LIVE_TRADING_ENABLED=false`.
2. `EXECUTION_KILL_SWITCH=true`.
3. No production Kalshi credentials are configured for PR 3.
4. No code path places live Kalshi orders.
5. Dashboard labels are derived from `/v1/system/status` and show paper mode, live trading disabled, and kill switch on.

Future live execution must include hard environment guards, a kill switch, tests for disabled execution, and explicit documentation in `PROJECT_CONTEXT.md`.

## PR 3/PR 3a Worker Commands

PR 3 worker commands are explicit one-shot commands. They should be run from the backend service context and should not be hidden inside the web dashboard.

From `apps/api`:

```powershell
.\.venv\Scripts\python.exe -m app.jobs.mlb_schedule_sync
.\.venv\Scripts\python.exe -m app.jobs.kalshi_market_sync
.\.venv\Scripts\python.exe -m app.jobs.paper_candidate_engine
.\.venv\Scripts\python.exe -m app.jobs.mlb_results_sync
.\.venv\Scripts\python.exe -m app.jobs.paper_settlement_sync
.\.venv\Scripts\python.exe -m app.jobs.balance_snapshot
.\.venv\Scripts\python.exe -m app.jobs.model_governance
.\.venv\Scripts\python.exe -m app.jobs.market_family_discovery
.\.venv\Scripts\python.exe -m app.jobs.open_position_price_refresh
```

Optional dated MLB schedule sync:

```powershell
.\.venv\Scripts\python.exe -m app.jobs.mlb_schedule_sync 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.mlb_results_sync 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.paper_settlement_sync 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.market_family_discovery 2026-06-24
```

The jobs currently cover:

- MLB slate ingestion.
- Targeted Kalshi MLB market resolution for the empirically observed `KXMLBGAME` full-game winner family.
- Orderbook snapshots for targeted relevant markets only.
- Auditable MLB game to Kalshi market mapping.
- Candidate scoring with the `heuristic_full_game_winner_v1` paper model.
- Conservative paper-trade simulation.
- MLB results updates for completed games.
- Full-game winner paper settlement and hold-to-settlement P/L.
- Paper balance snapshots.
- Feature snapshots and conservative heuristic model scoring.
- Model governance records that skip training/promotion until sample thresholds are met.
- Deterministic discovery-only market-family audit reports for `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`, `KXMLBF5`, `KXMLBF5SPREAD`, and `KXMLBF5TOTAL`. Normal PR3a fix3 discovery does not redundantly probe `KXMLBGAME`; full-game winner remains handled by targeted sync/resolve.
- REST last-mark refresh for open paper positions.

They do not cover scheduled automation or live execution.
They also do not fake spread, total, or first-five market tickers.
They do not probe retired guessed prefixes or `KXMLBTEAMTOTAL`.

Each worker should be idempotent where possible, log risk events, and avoid live order execution unless future safety gates are in place.

## Internal Run Endpoints

The backend also exposes these POST endpoints for controlled operational runs:

- `POST /v1/sync/mlb-schedule`
- `POST /v1/sync/mlb-results?target_date=YYYY-MM-DD`
- `POST /v1/sync/kalshi-markets`
- `POST /v1/run/paper-candidate-engine`
- `POST /v1/run/paper-settlement-sync?target_date=YYYY-MM-DD`
- `POST /v1/run/balance-snapshot`
- `POST /v1/run/model-governance`
- `POST /v1/run/open-position-price-refresh`
- `POST /v1/run/market-family-discovery?target_date=YYYY-MM-DD`
- `GET /v1/market-families/discovery?date=YYYY-MM-DD`
- `GET /v1/market-families/discovery-preview?date=YYYY-MM-DD`
- `GET /v1/kalshi/resolve-preview?date=YYYY-MM-DD`

For public or deployed backends, `BACKEND_API_KEY` is required and must be sent as `X-API-Key`. The unauthenticated bypass is only for explicit local development environments. Do not expose these endpoints as public dashboard buttons in PR 3.

## PR 3a Production Validation

After deploying PR 3 and running `alembic upgrade head`, validate in this order:

1. `GET /health` returns `status: "ok"`.
2. `GET /v1/system/status` reports `database.ready: true` and does not expose secrets.
3. Alembic reports head revision `0005_pr3a_discovery`.
4. `POST /v1/sync/mlb-schedule` returns a games count.
5. `GET /v1/kalshi/resolve-preview?date=YYYY-MM-DD` returns attempted `KXMLBGAME` event and market tickers for each MLB game, with `ok=true` even when individual games have no match warnings.
6. `POST /v1/sync/kalshi-markets` returns a structured summary with `games_considered`, attempted ticker counts, mapping counts, and `errors` when upstream calls fail.
7. `GET /v1/markets/today` shows mapped markets if matching Kalshi markets exist.
8. `POST /v1/run/paper-candidate-engine` exits cleanly and creates candidates with `heuristic_full_game_winner_v1` probabilities.
9. `POST /v1/sync/mlb-results` updates completed games with scores/final status.
10. `POST /v1/run/paper-settlement-sync` settles completed supported full-game winner paper trades.
11. `POST /v1/run/balance-snapshot` creates a snapshot and `/v1/dashboard/summary` uses it for the portfolio chart.
12. `POST /v1/run/model-governance` records a training/calibration run and counts resolved `KXMLBGAME` candidates even when older rows used `full_game_moneyline`.
13. `POST /v1/run/market-family-discovery?target_date=YYYY-MM-DD` returns structured `by_family` output, attempted event/market ticker counts, exact/fallback/event-filter attempt counts, no-match counts, request/rate-limit metrics, and persists a finalized `market_family_discovery_runs` row even when no candidate markets are found.
14. `GET /v1/market-families/discovery?date=YYYY-MM-DD` returns the latest finalized report with `run` not null after the POST succeeds.
15. Confirm discovered spread, total, or first-five families do not create paper candidates/trades.
16. `POST /v1/run/open-position-price-refresh` updates current marks and last mark timestamps for open paper positions only.
17. The Vercel dashboard shows readable contract labels with the raw Kalshi ticker as secondary text.
18. The dashboard shows `GAME STATUS`, `LAST MARK TIME`, working chart ranges, and `NORM` / `P/L $` / `P/L %` chart modes.
19. Confirm no live execution path exists and live trading remains disabled.

Broad market discovery is diagnostic-only:

- Keep `KALSHI_ENABLE_BROAD_DISCOVERY=false` for normal operation.
- If enabled for diagnostics, it must stay bounded by `KALSHI_MARKET_SYNC_MAX_PAGES` and `KALSHI_MARKET_SYNC_LIMIT`.
- Broad diagnostic failures should not fail the targeted sync.

## PR 3a Hotfix Validation

The PR3a market-family discovery path is deterministic. It handles expected Kalshi no-match responses without aborting the job, and PR3a fix3 keeps request volume low enough for production by batching exact ticker queries before using fallback probes.

Validate the hotfix after deploy:

1. Run `POST /v1/run/market-family-discovery?target_date=YYYY-MM-DD` with `X-API-Key`.
2. Confirm the response is structured JSON, not a blank upstream error.
3. Confirm the response status is `completed` for 404/no-match-only runs or `partial_error` when non-404 upstream errors were recorded but the job completed.
4. Run `GET /v1/market-families/discovery?date=YYYY-MM-DD` with `X-API-Key`.
5. Confirm `run` is not null and `market_family_discovery_runs.raw_summary` includes `attempted_event_tickers_count`, `attempted_market_tickers_count`, `no_match_counts`, `attempted_probe_count`, `probe_attempts`, `request_count`, `requests_saved_by_batching`, `rate_limited_count`, `retries_attempted`, and `stopped_due_to_rate_limit`.
6. Treat `markets_found=0` and zero `market_family_discovery_items` as valid when no markets are returned.
7. Confirm active registry prefixes are only `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`, `KXMLBF5`, `KXMLBF5SPREAD`, and `KXMLBF5TOTAL`; guessed legacy prefixes and `KXMLBTEAMTOTAL` must not be probed.
8. Confirm spread, total, and first-five families remain discovery-only and do not create paper candidates or trades.
9. Confirm known exact `KXMLBGAME` full-game winner resolver matches remain `confirmed_for_paper` with confidence around `0.9700`, zero or near-zero time delta, and team match score `1.0`.
10. Confirm `request_count` is materially lower than the previous event-filter-heavy validation run, and that repeated 429s produce `partial_error` with `stopped_due_to_rate_limit=true` rather than leaving a run in `running`.

## Required Context Updates

Every PR must update `PROJECT_CONTEXT.md`.

At minimum, each PR should document:

- What changed.
- Whether the paper/live trading safety posture changed.
- Any schema or deployment changes.
- Any new assumptions about Kalshi markets, model behavior, fees, settlement, or operations.
- Validation performed.

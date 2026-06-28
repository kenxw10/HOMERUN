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
14. Confirm PR 3c only scores validated `paper_supported` market-family mappings and leaves uncertain rows in review.
15. Confirm open-position current price is treated as a REST last mark, not WebSocket live price.
16. Confirm `/v1/dashboard/summary` reports only the active paper epoch unless `epoch_key` and `include_archived=true` are deliberately used for backend debugging.
17. Confirm optional WebSocket market data is disabled or healthy: `GET /v1/ws/status` should show `source=rest_fallback` when disabled.

## No-Live-Trading Safety Checklist

Live trading must remain disabled until a future PR explicitly adds execution support.

Do not proceed if any of these are false:

1. `LIVE_TRADING_ENABLED=false`.
2. `EXECUTION_KILL_SWITCH=true`.
3. No production Kalshi credentials are configured for PR 3.
4. No code path places live Kalshi orders.
5. Dashboard labels are derived from `/v1/system/status` and show paper mode, live trading disabled, and kill switch on.

Future live execution must include hard environment guards, a kill switch, tests for disabled execution, and explicit documentation in `PROJECT_CONTEXT.md`.

## PR 3/PR 3c Worker Commands

PR 3 worker commands are explicit one-shot commands. They should be run from the backend service context and should not be hidden inside the web dashboard.

From `apps/api`:

```powershell
.\.venv\Scripts\python.exe -m app.jobs.mlb_schedule_sync
.\.venv\Scripts\python.exe -m app.jobs.kalshi_market_sync
.\.venv\Scripts\python.exe -m app.jobs.paper_candidate_engine 2026-06-27
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

Optional dated MLB schedule sync:

```powershell
.\.venv\Scripts\python.exe -m app.jobs.mlb_schedule_sync 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.mlb_results_sync 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.paper_settlement_sync 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.mlb_feature_sync 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.model_feature_snapshot_backfill 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.market_family_discovery 2026-06-24
.\.venv\Scripts\python.exe -m app.jobs.market_family_mapping_sync 2026-06-24
```

The jobs currently cover:

- MLB slate ingestion.
- Targeted Kalshi MLB market resolution for the empirically observed `KXMLBGAME` full-game winner family.
- Orderbook snapshots for targeted relevant markets only.
- Auditable MLB game to Kalshi market mapping.
- Candidate scoring with the `mature_mlb_run_distribution_v2` paper model.
- Conservative paper-trade simulation.
- MLB results updates for completed games.
- Full-game winner paper settlement and hold-to-settlement P/L.
- Paper balance snapshots.
- Mature MLB feature snapshots with explicit source statuses.
- Model governance records that skip training/calibration/promotion until mature resolved-sample thresholds are met.
- Deterministic market-family audit reports for `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`, `KXMLBF5`, `KXMLBF5SPREAD`, and `KXMLBF5TOTAL`. Normal discovery does not redundantly probe `KXMLBGAME`; full-game winner remains handled by targeted sync/resolve.
- Market-family mapping sync that promotes only cleanly parsed supported families to `paper_supported`.
- REST last-mark refresh for open paper positions.
- Strict paper trade caps by slate, game, market family, open-position count, and correlated game/family exposure.

They do not cover scheduled automation or live execution.
They also do not fake spread, total, or first-five market tickers.
They do not probe retired guessed prefixes or `KXMLBTEAMTOTAL`.

Each worker should be idempotent where possible, log risk events, and avoid live order execution unless future safety gates are in place.

## PR3d Active Paper Observation Epoch

PR3d adds active paper epochs so old validation data can stay in the database without polluting the live operator dashboard.

Use this protected reset exactly when starting the PR3d observation period:

```powershell
$body = @{
  archive_current_as = "pre_pr3d_validation"
  new_epoch = "pr3d_paper_observation_v1"
  starting_balance = 500.00
  archive_open_positions = $true
  reset_dashboard_metrics = $true
  confirmation = "RESET_PAPER_EPOCH"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"; "Content-Type"="application/json"} -Body $body "https://YOUR-RAILWAY-API/v1/admin/paper-trading/reset-epoch"
```

Expected reset result:

- `new_epoch_key=pr3d_paper_observation_v1`
- `starting_balance=500`
- `new_balance_snapshot_id` is present
- old active/unassigned paper rows are archived, not deleted

Expected dashboard immediately after reset:

- Portfolio value: `$500.00`
- Cash: `$500.00`
- Open positions: `0`
- Closed positions for today/yesterday/selected dates: `0`
- P/L: `$0.00`
- Record: `0-0-0`
- Active epoch: `PR3D PAPER OBSERVATION V1`

Do not add a frontend reset button. Reset remains protected API-only.

## PR3d Cron-Safe Paper Jobs

Use `app.jobs.runner` for Railway cron jobs. Each invocation opens a database session, acquires a job lock, runs one job, records `job_runs`, and exits.

```powershell
python -m app.jobs.runner --job daily-setup --target-date today_et
python -m app.jobs.runner --job candidate-sweep --target-date today_et
python -m app.jobs.runner --job price-refresh --target-date today_et
python -m app.jobs.runner --job settlement --target-date yesterday_et
python -m app.jobs.runner --job governance
python -m app.jobs.runner --job full-paper-cycle --target-date today_et
```

Protected manual endpoints mirror the cron jobs:

- `POST /v1/jobs/run/daily-setup?target_date=YYYY-MM-DD`
- `POST /v1/jobs/run/candidate-sweep?target_date=YYYY-MM-DD`
- `POST /v1/jobs/run/price-refresh?target_date=YYYY-MM-DD`
- `POST /v1/jobs/run/settlement?target_date=YYYY-MM-DD`
- `POST /v1/jobs/run/governance`
- `POST /v1/jobs/run/full-paper-cycle?target_date=YYYY-MM-DD`

The dashboard shows the last setup, candidate sweep, price refresh, settlement, governance, and WebSocket/REST status.

## PR3d WebSocket Market Data Worker

The optional paper-safe worker command is:

```powershell
python -m app.workers.kalshi_ws_paper
```

Safe defaults:

- `WEBSOCKET_MARKET_DATA_ENABLED=false`
- `WS_SUBSCRIBE_OPEN_POSITIONS=true`
- `WS_SUBSCRIBE_ACTIVE_CANDIDATES=true`
- `WS_MAX_MARKETS=500`

When disabled or unavailable, `/v1/ws/status` should report REST fallback. The worker must never place live orders and must only update active paper epoch market/trade marks.

## Internal Run Endpoints

The backend also exposes these POST endpoints for controlled operational runs:

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
- `GET /v1/model/parameters/active`
- `GET /v1/model/sources/status`
- `GET /v1/model/training/latest`
- `GET /v1/model/predictions?date=YYYY-MM-DD`
- `GET /v1/model/predictions/today`
- `POST /v1/sync/mlb-features?target_date=YYYY-MM-DD`
- `POST /v1/sync/mlb-features?target_date=YYYY-MM-DD&include_modules=all`
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
- `POST /v1/jobs/run/candidate-sweep?target_date=YYYY-MM-DD`
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

For public or deployed backends, `BACKEND_API_KEY` is required and must be sent as `X-API-Key`. The unauthenticated bypass is only for explicit local development environments. Do not expose these endpoints as public dashboard buttons in PR 3.

## PR 3c Production Validation

After deploying PR 3c and running `alembic upgrade head`, validate in this order:

1. `GET /health` returns `status: "ok"`.
2. `GET /v1/system/status` reports `database.ready: true` and does not expose secrets.
3. Alembic reports head revision `0009_pr3c_fix2_features`.
4. `POST /v1/sync/mlb-schedule` returns a games count.
5. `GET /v1/kalshi/resolve-preview?date=YYYY-MM-DD` returns attempted `KXMLBGAME` event and market tickers for each MLB game, with `ok=true` even when individual games have no match warnings.
6. `POST /v1/sync/kalshi-markets` returns a structured summary with `games_considered`, attempted ticker counts, mapping counts, and `errors` when upstream calls fail.
7. `GET /v1/markets/today` shows mapped markets if matching Kalshi markets exist.
8. `POST /v1/run/paper-candidate-engine?target_date=YYYY-MM-DD` exits cleanly, reports the same `target_date` and `prediction_run_target_date`, and creates candidates with `mature_mlb_run_distribution_v2` probabilities.
9. `POST /v1/sync/mlb-results` updates completed games with scores/final status.
10. `POST /v1/run/paper-settlement-sync` settles completed supported full-game winner paper trades.
11. `POST /v1/run/balance-snapshot` creates a snapshot and `/v1/dashboard/summary` uses it for the portfolio chart.
12. `POST /v1/run/model-governance` records a governance event and either skips with a clear mature-sample reason or reports calibration/promotion metrics.
13. `GET /v1/model/governance/status` returns active model, feature version, calibration status, thresholds, and latest governance status.
14. `POST /v1/sync/mlb-features?target_date=YYYY-MM-DD` records feature snapshots with explicit source statuses.
15. `GET /v1/model/features/coverage?date=YYYY-MM-DD` reports coverage without inventing missing lineup, weather, injury, umpire, team-total, or sportsbook data.
16. `POST /v1/run/market-family-discovery?target_date=YYYY-MM-DD&force_refresh=false` returns structured `by_family` output, attempted event/market ticker counts, exact/fallback/event-filter attempt counts, no-match counts, request/rate-limit metrics, and persists a finalized `market_family_discovery_runs` row even when no candidate markets are found. Leave `force_refresh=false` for normal operations so a recent usable run or active cooldown is reused instead of repeatedly calling Kalshi.
17. `GET /v1/market-families/discovery?date=YYYY-MM-DD` returns the latest finalized report with `run` not null after the POST succeeds.
18. `POST /v1/sync/market-family-mappings?target_date=YYYY-MM-DD` promotes only parseable supported families to `paper_supported`; missing line/selection/settlement rows stay `needs_review`.
19. `POST /v1/run/paper-candidate-engine?target_date=YYYY-MM-DD` applies executable-price freshness, conservative fee-adjusted EV, probability-edge, line-selection, and paper caps in that order. Cap-rejected candidates stay no-trade decisions, and the response should show `trade_eligible_after_ev_filters` far below total candidates before caps are the dominant selector.

PR3c hotfix production validation:

1. Run the candidate engine with an explicit date:

```powershell
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/run/paper-candidate-engine?target_date=2026-06-27"
```

2. Confirm `result.target_date` is `2026-06-27`, `result.date` is `20260627`, and `result.prediction_run_target_date` is `2026-06-27`.
3. Confirm `fee_estimate_avg`, `avg_expected_value_net`, `trade_eligible_after_ev_filters`, `line_selection_candidates_rejected`, `stale_price_count`, `decision_counts`, and `cap_counts` are present.
4. Fetch the same slate's predictions:

```powershell
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/predictions?date=2026-06-27"
```

5. Confirm normal executable candidates include `fee_estimate`, `expected_value_net`, `probability_edge`, `executable_price_source`, and `price_status`.

PR3c fix3 public feature-ingestion validation:

1. Confirm source diagnostics are enabled and do not expose secrets:

```powershell
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/sources/status"
Invoke-RestMethod "https://YOUR-RAILWAY-API/v1/system/status"
```

Expected: `feature_sync_enable_network_sources=true`, `public_sources_enabled=true`, `mlb_stats_base_url=https://statsapi.mlb.com/api/v1`, and `open_meteo_base_url=https://api.open-meteo.com/v1`.

2. Run public feature ingestion for a slate:

```powershell
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/mlb-features?target_date=2026-06-27&include_modules=all"
```

Expected: `network_sources_enabled=true`, nonzero `games_seen`, raw `tables_written`, and `validation_status=ok` or a degraded status with explicit warnings/errors. If `FEATURE_SYNC_ENABLE_NETWORK_SOURCES=false`, expected output is `validation_status=skipped_network_disabled`, `rows_inserted=0`, and `rows_updated=0`.

3. Validate raw tables and composed snapshots:

```powershell
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/features/coverage?date=2026-06-27"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/features/detail?date=2026-06-27"
```

Expected: lineup snapshots where MLB posted lineups, weather rows for stadiums with coordinates, pitcher rows where probable starters are available, partial bullpen proxies when exact reliever data is unavailable, and `data_quality_reason` caps when critical modules remain missing.

PR3c fix4 idempotent feature-ingestion validation:

1. Confirm repeated module syncs return structured JSON, not 500s:

```powershell
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/mlb-team-features?target_date=2026-06-27"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/mlb-team-features?target_date=2026-06-27"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/mlb-pitcher-features?target_date=2026-06-27"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/mlb-pitcher-features?target_date=2026-06-27"
```

Expected: response `ok=true`; `validation_status` is `ok`, `degraded_no_available_public_rows`, or `degraded_with_errors`; no duplicate-key 500; and each response includes `hydration_rows_seen`, `hydration_rows_upserted`, `hydration_duplicate_count`, `hydration_error_count`, `hydration_validation_status`, `refresh_schedule`, and `hydration_skipped_reason`.

2. Confirm `refresh_schedule` behavior:

```powershell
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/mlb-team-features?target_date=2026-06-27&refresh_schedule=false"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/mlb-features?target_date=2026-06-27&include_modules=all&refresh_schedule=true"
```

Expected: module syncs skip schedule hydration when target-date games already exist; forced refresh safely updates existing `mlb_games.external_game_id` rows instead of inserting duplicates.

3. Confirm source diagnostics capture degraded ingestion:

```powershell
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/sources/status"
```

Expected: `last_attempted_sync`, `validation_status`, `last_error`, `latest_errors`, and per-table `status_counts` are present. `pybaseball_available`, `pybaseball_version`, `pybaseball_module_path`, import errors, attempted functions, DB cache status, and `advanced_stats_status` should be visible. When pybaseball succeeds and rows match, team/pitcher/bullpen rows should include `source=pybaseball_public_stats_v1`; when it fails, the response should expose the source error and fall back to partial derived rows instead of returning a blank 500.

4. Confirm weather and lineup degraded behavior:

```powershell
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/weather?target_date=2026-06-27"
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/mlb-lineups?target_date=2026-06-27"
```

Expected: Open-Meteo/source errors produce missing weather rows with structured raw errors; not-posted lineups produce `LINEUP_NOT_POSTED_YET`; posted/final-game MLB boxscore lineups should parse as available.

PR3c fix2 feature/model validation:

1. Run the complete feature sync for a slate:

```powershell
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/mlb-features?target_date=2026-06-27&include_modules=all"
```

2. Confirm the response includes module counts for team, pitcher, bullpen, lineup, weather, park, and travel features. Missing optional provider data should be reported as `missing` or `unavailable`, not silently faked.
3. Fetch feature detail:

```powershell
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/features/detail?date=2026-06-27"
```

4. Confirm `feature_version` is `mature_mlb_features_v2`, critical module warnings are explicit, and static park/travel features are separate from optional network weather/provider inputs.
5. Fetch active model parameters:

```powershell
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/parameters/active"
```

6. Confirm the active parameter version exists and does not require live trading or production Kalshi credentials.
20. `POST /v1/run/paper-settlement-sync` settles completed supported spread, total, and first-five paper trades when final scores/linescore are available; missing first-five linescore rows are skipped, not closed.
21. `POST /v1/run/open-position-price-refresh` updates current marks and last mark timestamps for open paper positions only.
22. The Vercel dashboard shows readable contract labels with the raw Kalshi ticker as secondary text.
23. The dashboard shows `GAME STATUS`, `LAST MARK TIME`, closed positions by selected date, working chart ranges, `NORM` / `P/L $` / `P/L %` chart modes, and the expanded model quality panel.
24. Confirm no live execution path exists and live trading remains disabled.

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
7. Confirm active registry prefixes are only `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`, `KXMLBF5`, `KXMLBF5SPREAD`, and `KXMLBF5TOTAL`; guessed legacy prefixes and `KXMLBTEAMTOTAL` must not be probed. Spread and total families should be discovered through event-ticker filtering, not guessed exact market-ticker batches.
8. Confirm spread, total, and first-five families create paper candidates/trades only after `market_family_mapping_sync` marks the mapping `paper_supported`.
9. Confirm known exact `KXMLBGAME` full-game winner resolver matches remain `confirmed_for_paper` with confidence around `0.9700`, zero or near-zero time delta, and team match score `1.0`.
10. Confirm `request_count` is materially lower than the previous event-filter-heavy validation run, and that repeated 429s produce `partial_rate_limited`, `stopped_due_to_rate_limit=true`, and `cooldown_until` rather than leaving a run in `running`.
11. Re-run with `force_refresh=false` and confirm `served_from_cache=true` when a recent usable run or cooldown exists. Use `force_refresh=true` only for deliberate validation.

PR3c fix5 pybaseball validation:

1. Deploy with `pybaseball==2.2.7` installed from `apps/api/requirements.txt`; no manual PowerShell install should be needed.
2. Confirm source diagnostics:

```powershell
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/sources/status"
```

3. Confirm `pybaseball_available=true`, `pybaseball_import_error=$null`, and `pybaseball_version` or `pybaseball_module_path` is present.
4. Run team/pitcher feature sync and check `pybaseball_functions_attempted`, `pybaseball_rows_seen`, `pybaseball_rows_matched`, `advanced_available_count`, and `advanced_partial_count`.
5. If pybaseball calls fail, treat `advanced_stats_status` and `pybaseball_last_error` as the source of truth; the system should degrade to derived partial rows, not return a blank 500.

PR3c fix6 mature MLB ingestion and event-discovery validation:

1. Run a full feature sync for a known slate:

```powershell
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/sync/mlb-features?target_date=2026-06-27&include_modules=all&refresh_schedule=true"
```

2. Expected: `validation_status` is `ok` or `degraded_with_errors`, not a blank 500. The response should include MLB Stats API primary counters such as `mlb_stats_api_primary_available_count`, `probable_starters_seen`, `pitcher_season_stats_available_count`, `pitcher_game_log_available_count`, `starter_recent_available_count`, and `starter_workload_available_count`.
3. If FanGraphs-backed pybaseball functions return HTTP 403, expected behavior is degraded diagnostics with `pybaseball_fangraphs_status=unavailable_http_403` or an equivalent structured error. MLB Stats API primary rows should still be written where public Stats API data exists.
4. Check feature detail:

```powershell
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/model/features/detail?date=2026-06-27"
```

5. Expected: team strength, handedness/platoon, starter recent, and starter workload modules prefer `source=mlb_stats_api_primary_v1` when those rows are available. Statcast/Savant contact quality is secondary enrichment, and `derived_homerun_v2` remains partial fallback.
6. Run market-family discovery with cache disabled only for deliberate validation:

```powershell
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/run/market-family-discovery?target_date=2026-06-27&force_refresh=true"
Invoke-RestMethod -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/market-families/discovery?date=2026-06-27"
```

7. Expected: full-game and first-five winner families may use direct market-ticker probes. Spread and total families should show `event_ticker` lookup activity and should not spend ticker batches on guessed spread/total market tickers. Cache hits, no-match responses, source errors, and 429s should be structured; repeated 429s should produce `partial_rate_limited` plus cooldown metadata.

## Required Context Updates

Every PR must update `PROJECT_CONTEXT.md`.

At minimum, each PR should document:

- What changed.
- Whether the paper/live trading safety posture changed.
- Any schema or deployment changes.
- Any new assumptions about Kalshi markets, model behavior, fees, settlement, or operations.
- Validation performed.

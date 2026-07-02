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
- Conservative side-aware paper-trade simulation. YES candidates use YES-side executable prices; NO candidates use NO-side executable prices.
- MLB results updates for completed games.
- Full-game winner paper settlement and hold-to-settlement P/L.
- Paper balance snapshots.
- Mature MLB feature snapshots with explicit source statuses.
- Model governance records that skip training/calibration/promotion until mature resolved-sample thresholds are met.
- Deterministic market-family audit reports for `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`, `KXMLBF5`, `KXMLBF5SPREAD`, and `KXMLBF5TOTAL`. Normal discovery does not redundantly probe `KXMLBGAME`; full-game winner remains handled by targeted sync/resolve.
- Market-family mapping sync that promotes only cleanly parsed supported families to `paper_supported`.
- REST last-mark refresh for open paper positions.
- Strict paper trade caps by slate, game, market family, open-position count, correlated game/family exposure, and aggregate bankroll risk. Defaults are 8 trades per slate, 4 per family, 12 open positions, 20% daily new risk, 25% open risk, 10% family risk, 15% scope risk, and 8% sub-20c low-price bucket risk.
- PR3k adds stricter paper selection controls: first-five `TIE` is diagnostics-only, sub-10c prices are blocked, 10c-under-20c prices need stronger EV/edge and have low-price slate/sweep caps, each sweep opens at most 3 new trades by default, early sweeps reserve later slots, same-side exposure is capped by default, and risk-cap-reduced positions must still meet minimum size.
- Spread markets are diagnostics-only unless `PAPER_SPREAD_TRADING_ENABLED=true`. Do not enable spread paper trading until side-aware spread parsing and settlement have been manually verified against the Kalshi UI.

They do not cover scheduled automation or live execution.
They also do not fake spread, total, or first-five market tickers.
They do not probe retired guessed prefixes or `KXMLBTEAMTOTAL`.

Each worker should be idempotent where possible, log risk events, and avoid live order execution unless future safety gates are in place.

## PR3d Active Paper Observation Epoch

PR3d adds active paper epochs so old validation data can stay in the database without polluting the live operator dashboard.

Use this protected reset exactly when starting the PR3d observation period:

```powershell
$body = @{
  archive_current_as = "pr3d_bad_spread_parser_validation"
  new_epoch = "pr3d_paper_observation_v2"
  starting_balance = 500.00
  archive_open_positions = $true
  reset_dashboard_metrics = $true
  confirmation = "RESET_PAPER_EPOCH"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"; "Content-Type"="application/json"} -Body $body "https://YOUR-RAILWAY-API/v1/admin/paper-trading/reset-epoch"
```

Expected reset result:

- `new_epoch_key=pr3d_paper_observation_v2`
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
- Active epoch: `PR3D PAPER OBSERVATION V2`
- Candidate sweep reports YES/NO candidate counts, spread trading disabled, and aggregate risk-cap usage.
- Spread candidates remain diagnostics-only unless `PAPER_SPREAD_TRADING_ENABLED=true`.

Do not add a frontend reset button. Reset remains protected API-only.

## PR3d Cron-Safe Paper Jobs

Use `app.jobs.runner` for Railway cron jobs. Each invocation opens a database session, acquires a job lock, runs one job, records `job_runs`, and exits.

```powershell
python -m app.jobs.runner --job daily-setup --target-date today_et
python -m app.jobs.runner --job candidate-sweep --target-date today_et --min-time-to-start-minutes 45 --max-time-to-start-minutes 180 --sweep-label rolling_pregame_window
python -m app.jobs.runner --job spread-audit --target-date today_et --min-time-to-start-minutes 45 --max-time-to-start-minutes 180
python -m app.jobs.runner --job price-refresh --target-date today_et
python -m app.jobs.runner --job settlement --target-date yesterday_et
python -m app.jobs.runner --job governance
python -m app.jobs.runner --job full-paper-cycle --target-date today_et
```

Recommended production cadence:

- Daily setup at 8:30 AM ET. This is the normal owner of heavy MLB feature sync, including public MLB Stats API hydration, pybaseball/FanGraphs, Statcast/Savant, Open-Meteo, and `mature_mlb_features_v2` snapshot writes.
- Candidate sweep every 30 minutes from 10:30 AM ET through 10:00 PM ET using the 45-180 minute rolling pregame window. Candidate sweeps are feature-cache-only for heavy features and should not run full MLB feature sync; they may run the bounded official MLB Stats API pregame context refresh for starters, posted lineups, and pitcher game-log cache rows.
- Price refresh every 15 minutes from 11:00 AM ET through 1:30 AM ET. Price refresh intentionally has no time-to-start filter because it marks all active open paper positions.
- Settlement every 30 minutes from 2:30 PM ET through 1:30 AM ET for `today_et`, plus an 8:30 AM ET `yesterday_et` catch-up.
- Governance at 9:00 AM ET after the settlement catch-up.

Railway cron schedules are UTC. Adjust the UTC hours when New York changes between EDT and EST.

Protected manual endpoints mirror the cron jobs:

- `POST /v1/jobs/run/daily-setup?target_date=YYYY-MM-DD`
- `POST /v1/jobs/run/candidate-sweep?target_date=YYYY-MM-DD&min_time_to_start_minutes=45&max_time_to_start_minutes=180&sweep_label=rolling_pregame_window`
- `POST /v1/jobs/run/spread-audit?target_date=YYYY-MM-DD&min_time_to_start_minutes=45&max_time_to_start_minutes=180`
- `POST /v1/jobs/run/price-refresh?target_date=YYYY-MM-DD`
- `POST /v1/jobs/run/settlement?target_date=YYYY-MM-DD`
- `POST /v1/jobs/run/governance`
- `POST /v1/jobs/run/full-paper-cycle?target_date=YYYY-MM-DD`
- `POST /v1/sync/mlb-starters?target_date=today_et`
- `POST /v1/sync/mlb-pregame-context?target_date=today_et`
- `GET /v1/model/starter-status?date=YYYY-MM-DD`

The dashboard shows the last setup, candidate sweep, price refresh, settlement, governance, WebSocket/REST status, and the last candidate sweep window. A windowed sweep with no games in range should return `status=skipped_no_games_in_window`, count excluded games, and still be a successful no-work run rather than an error. Candidate-sweep results should report `feature_sync_mode=cache_only`, `feature_sync_skipped=true`, and `cached_features` diagnostics. If target-date mature feature snapshots are missing, the sweep should complete cleanly with `no_candidates_missing_feature_snapshots` instead of starting source ingestion or failing the job.

PR3m pregame context refresh uses only official MLB Stats API calls: target-date schedule with `probablePitcher(note)`, per-game live feed probable pitchers and lineups, boxscore starter/lineup data, and pitcher game-log stats for known starter IDs. Live feed/boxscore identities are preferred over stale schedule probables when both exist. It does not call pybaseball, FanGraphs, Statcast/Savant, Open-Meteo, full `sync_mlb_features`, sportsbook APIs, team totals, or umpire logic. If official MLB sources do not identify a starter or lineup, the status remains missing or partial with a reason; no neutral starter or fake lineup is inserted.

PR3m.1 dashboard observation cutover is reporting-only. Default `/v1/dashboard/summary` excludes active-epoch paper trades and legacy positions entered before midnight ET on `2026-07-02`; those rows remain in the database and are visible only when `include_pre_observation=true` is supplied. The response includes `observation_filter` metadata with excluded counts and the history parameter. This cutover must not reset epochs, close open positions, delete rows, alter candidate generation, change settlement, change cron schedules, or enable live execution.

## PR3n Defense Feature Transparency

Daily setup and explicit feature-sync endpoints own defense ingestion. Candidate-sweep remains heavy-source cache-only and must not call full `sync_mlb_features`, pybaseball, FanGraphs, Statcast/Savant ingestion, Open-Meteo, sportsbook APIs, team totals, or umpire sources.

Defense source hierarchy:

- MLB Stats API team fielding game logs are the baseline source for `defense_season` and `defense_recent`.
- Stored official baseline fields include errors, assists, putouts, chances, fielding percentage, double plays, passed balls, wild pitches, and stolen-base/caught-stealing fields when MLB provides them.
- Official MLB lineup/boxscore data is the source for catcher starter inference.
- Advanced catcher framing, blocking, throwing, OAA/DRS/UZR, and similar advanced defense metrics are not configured unless a future PR adds a reliable source.
- Umpire factors are explicitly excluded and are not a model blocker.

After deployment, validate:

1. Run daily setup or explicit feature sync for the target date.
2. Confirm `/v1/model/sources/status` includes `mlb_stats_api_fielding`, `catcher_from_official_lineup`, `advanced_catcher_metrics`, and `umpire`.
3. Confirm `/v1/model/features/coverage?date=YYYY-MM-DD` and `/v1/model/features/detail?date=YYYY-MM-DD` show `defense_catcher` as partial instead of opaque missing when baseline fielding exists.
4. Confirm detail reasons distinguish baseline team defense, recent defense, catcher inferred/not posted, advanced catcher metrics unavailable, and umpire excluded.
5. Run a dry candidate sweep and confirm `feature_sync_mode=cache_only`, `feature_sync_skipped=true`, and `heavy_feature_sync_skipped=true`.
6. Confirm candidate diagnostics include `defense_catcher` in quality contribution/penalty output.
7. Confirm PR3k selection controls, PR3m pregame context refresh, and PR3m.1 observation cutoff behavior still hold.
8. Keep PR3o spread audit and PR3p spread enablement separate.

After deployment, validate:

1. `POST /v1/sync/mlb-pregame-context?target_date=today_et` returns `feature_sync_mode=pregame_context_refresh_lightweight`, target-date games checked, starter IDs/names where MLB has announced them, lineup counts, and explicit lineup missing reasons such as `LINEUP_NOT_POSTED_YET`, `PARTIAL_LINEUP_POSTED`, or `LIVE_FEED_UNAVAILABLE`.
2. `POST /v1/sync/mlb-starters?target_date=today_et` still returns `feature_sync_mode=starter_refresh_lightweight`, target-date games checked, starter IDs/names where MLB has announced them, and explicit missing reasons otherwise.
3. `GET /v1/model/starter-status?date=YYYY-MM-DD` reports per-game gamePk, teams, scheduled start ET, starter IDs/names/status/source, last checked time, pitcher stat statuses, and starter feature module statuses.
4. `POST /v1/jobs/run/candidate-sweep?target_date=today_et&min_time_to_start_minutes=45&max_time_to_start_minutes=180&sweep_label=rolling_pregame_window` returns the sweep diagnostics.
5. The result contains `feature_sync_mode=cache_only`, `feature_sync_skipped=true`, `heavy_feature_sync_skipped=true`, `pregame_context_refresh`, and does not include a `sync_mlb_features` step.
6. Only in-window games create paper trades.
7. Out-of-window games are counted as too soon, too late, started, or wrong date.
8. Repeated sweeps do not duplicate paper trades.
9. Spread trading remains disabled unless explicitly enabled.
10. YES and NO candidates can still be scored in-window.
11. Daily/open/family/scope risk caps still apply across the full active epoch, not only the current sweep.
12. Price refresh updates all open positions.
13. The dashboard shows the last sweep window, starter hydration aggregate, and paper trades created in that sweep.
14. No live execution path or live order placement is enabled.
15. `GET /v1/dashboard/summary` shows `observation_filter.active=true`, `observation_start_date=2026-07-02`, clean July 2+ performance/portfolio metrics, and the compact frontend note about excluding pre-Jul 2 validation rows.
16. `GET /v1/dashboard/summary?closed_date=2026-07-01` returns no default pre-cutover closed rows, while `GET /v1/dashboard/summary?closed_date=2026-07-01&include_pre_observation=true` returns the preserved historical rows for audit/debugging.
17. The active July 2 paper position remains open and visible if it was entered at or after the observation cutoff.

PR3i widens the persisted candidate decision field so post-eligibility rejection reasons from line selection, same-game/scope correlation, and caps can be saved safely. After deploying and running `alembic upgrade head`, include one normal non-dry candidate-sweep validation during the 45-180 minute window and confirm it completes without `StringDataRightTruncation`; paper trades should open only if the existing gates and caps allow them.

## PR3k Selection And Sizing Controls

Candidate-sweep remains feature-cache-only. Daily setup still owns heavy feature sync; the repeating sweep must not run full MLB feature sync, pybaseball, FanGraphs, Statcast/Savant, Open-Meteo, sportsbook APIs, team totals, or umpire logic.

Default paper controls:

- `PAPER_MIN_TRADE_PRICE=0.10`
- `PAPER_LOW_PRICE_THRESHOLD=0.20`
- `PAPER_LOW_PRICE_MIN_NET_EV=0.08`
- `PAPER_LOW_PRICE_MIN_PROB_EDGE=0.05`
- `PAPER_LOW_PRICE_MAX_TRADES_PER_SLATE=2`
- `PAPER_LOW_PRICE_MAX_TRADES_PER_SWEEP=1`
- `PAPER_MAX_NEW_TRADES_PER_SWEEP=3`
- `PAPER_MAX_NEW_TRADES_BEFORE_3PM_ET=4`
- `PAPER_RESERVE_TRADES_AFTER_3PM_ET=2`
- `PAPER_MIN_POST_CAP_CONTRACTS=5`
- `PAPER_MIN_POST_CAP_NOTIONAL=2.00`
- `PAPER_MAX_SAME_SIDE_TRADES_PER_SLATE=6`

After deployment, validate:

1. The next windowed `candidate-sweep` result includes `trade_allocation` and `low_price_controls`.
2. First-five `TIE` candidates use `no_trade_f5_tie_disabled` and do not create paper trades.
3. Sub-10c candidates use `no_trade_price_below_floor`.
4. Early sweeps report reserved later slots and do not consume all daily slots.
5. Dashboard open/closed position tables show side, entry cost, current/exit value, fee, mark time, and P/L.
6. No live execution, WebSocket, spread activation, cron schedule, feature threshold, EV/model, settlement, market discovery, sportsbook, team-total, umpire, or defense behavior changed.

## PR3l Source Reliability And Statcast Fallbacks

Daily setup remains the owner of heavy public-source feature ingestion. Candidate-sweep remains cache-only for heavy features and must not call full MLB feature sync, pybaseball, FanGraphs, Statcast/Savant, Open-Meteo, sportsbook APIs, team totals, or umpire logic. The only sweep-time feature refresh allowed is the bounded official MLB Stats API pregame context refresh.

Source hierarchy:

- MLB Stats API: critical official baseball source for schedule, game context, probable starters, game logs, boxscore, linescore, and live-feed payloads already used by the feature pipeline.
- Kalshi public market data: critical market source for market context, prices, and orderbook-derived paper marks.
- Open-Meteo: weather enrichment source.
- Statcast/Savant: secondary cached public enrichment for contact quality.
- FanGraphs-backed pybaseball batting/pitching: optional cached enrichment only.
- Static HOMERUN reference data: park profiles, venue metadata, and team mappings.
- Derived HOMERUN: travel/rest/fatigue/workload proxies.
- Optional providers: injuries, external lineups, and optional weather keys are not configured unless their env vars are set.

`GET /v1/model/sources/status` now includes `source_inventory` and `source_health`. Each item is machine-readable and includes `source_name`, `source_kind`, `criticality`, `status`, `last_successful_sync`, `last_attempted_sync`, `last_error`, `sample_count`, `modules_affected`, `fallback_used`, `fallback_source`, `fallback_reason`, and `freshness_age_minutes` where meaningful.

Interpretation:

- `available`: current source rows are usable.
- `cached`: a latest source attempt failed, but a last-good cache within the configured age window is being used.
- `stale`: a last-good cache exists but is older than the configured source staleness window.
- `partial`: the source is present but incomplete.
- `failed`: no usable cache is available after a source failure.
- `not_configured`: optional provider or network-backed source is intentionally off.
- `not_wired`: an optional configured source is not implemented as a production input.

Cache age settings:

- `ADVANCED_PUBLIC_STATS_MAX_STALE_HOURS=72`
- `STATCAST_CACHE_MAX_STALE_HOURS=48`

Expected fail-soft behavior:

- FanGraphs HTTP 403 should appear as `fan_graphs_http_403` for `pybaseball_fangraphs`.
- Statcast/Savant request failures should appear as `statcast_request_failed` or `statcast_schema_changed`.
- Existing same-date Statcast contact-quality fields should be preserved when a later Statcast fetch fails or returns empty.
- MLB Stats API primary rows and mature feature snapshots should still write when enrichment sources degrade.
- Degraded enrichment should produce warnings/source-health state, not a candidate-sweep crash.

PR3l validation checklist:

1. `GET /v1/model/sources/status` shows `source_inventory` with MLB Stats API, Kalshi public market data, Open-Meteo, static HOMERUN reference, derived HOMERUN, pybaseball/FanGraphs, Statcast/Savant, and optional provider entries.
2. Run daily setup only when operationally safe: `POST /v1/jobs/run/daily-setup?target_date=today_et`.
3. If FanGraphs returns 403, confirm the daily setup job succeeds or succeeds with warnings, source health shows `pybaseball_fangraphs` as `cached`, `stale`, or `failed`, and MLB Stats API baseline rows still populate.
4. If Statcast/Savant fails, confirm source health shows `statcast_savant` fallback state and mature snapshots keep same-date cached contact-quality fields when present.
5. Run a dry candidate sweep and confirm `feature_sync_mode=cache_only`, `feature_sync_skipped=true`, and no heavy source sync step.
6. Confirm no live execution, sportsbook data, full-game spread enablement, defense module, heavy pregame ingestion, team totals, umpire factors, model threshold loosening, or cron cadence change.

## PR3g Candidate-Stage Quality And EV Diagnostics

PR3g does not change thresholds, cron schedules, trading gates, settlement, WebSocket behavior, or spread activation. It makes candidate-sweep results explain whether no-trade behavior is caused by data quality, price/mapping gates, EV/edge filters, duplicate market surfaces, or caps.

After deployment, inspect the next candidate-sweep job result or `GET /v1/dashboard/summary` and confirm these fields are present:

- `raw_feature_snapshot_data_quality_avg` and `paper_observation_data_quality_avg`
- `quality_threshold`
- `candidate_stage_market_context_status_counts`
- `quality_block_reason_counts`
- `top_quality_blockers`
- `quality_ev_diagnostics.ev_and_edge_pass_count`
- `quality_ev_diagnostics.deduped_ev_edge_pass_count_by_game_scope_family`
- `quality_ev_diagnostics.top_counterfactual_candidates_blocked_by_quality`

Expected behavior:

- `feature_sync_mode=cache_only` is still present for candidate-sweep.
- Missing optional/structural modules remain visible as missing/partial and are not marked available.
- Candidate-stage market context can be `available` only when mapping, settlement support, trusted selection, market status, side, and fresh executable price checks pass.
- Full-game spread still returns blocked diagnostics while `PAPER_SPREAD_TRADING_ENABLED=false`.
- If no trades are created, the dominant reason should be explicit in `decision_counts`, `candidate_diagnostics`, and `quality_ev_diagnostics`.

## PR3d Hotfix 3 Spread Audit And Correlation Validation

Run the spread audit before considering `PAPER_SPREAD_TRADING_ENABLED=true`:

```powershell
python -m app.jobs.runner --job spread-audit --target-date today_et --min-time-to-start-minutes 45 --max-time-to-start-minutes 180
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/jobs/run/spread-audit?target_date=today_et&min_time_to_start_minutes=45&max_time_to_start_minutes=180"
```

Expected audit behavior:

- The job updates spread mapping metadata only; it does not create paper trades.
- Verified spread rows include parsed selection, line, inning scope, settlement rule status, actual contract display, normalized equivalent display, and raw Kalshi contract text.
- Ticker-only spread rows remain `needs_review` / parser-unverified.
- Spread candidates still return `no_trade_spread_trading_disabled` while `PAPER_SPREAD_TRADING_ENABLED=false`.

Display checks:

- Actual contract display should describe the real Kalshi YES/NO contract, for example `NO ON PITTSBURGH PIRATES -1.5 FULL GAME`.
- Normalized equivalent display may show the operator-friendly equivalent, for example `SEATTLE MARINERS +1.5 FULL GAME EQUIVALENT`.
- Totals NO should display under/over equivalents, for example `UNDER 8 FULL GAME EQUIVALENT`, not a fake signed spread such as `+8`.

Correlation checks:

- Default `PAPER_MAX_TRADES_PER_GAME_SCOPE=1` blocks two positions for the same `target_date + mlb_game_id + inning_scope`.
- A first-five total and a first-five tie on the same game cannot both trade by default.
- One first-five position and one full-game position for the same game are different scopes and may both pass if every other gate and cap passes.
- Candidate sweep summaries should include `game_scope_correlation`, `trade_eligible_after_game_scope_correlation`, and `trades_blocked_by_game_scope_correlation`.

Risk-basis checks:

- Candidate sweep summaries should report risk caps using `risk_limit_basis_type=active_epoch_portfolio_value`.
- The dashboard model panel should show the active risk basis amount and game-scope cap.
- Selected positions should include a short rationale with edge, net EV, quality, and risk-basis context.

This hotfix does not enable live trading, new production cron services, or spread paper trading. Keep cron and spread trading blocked until the audit output is manually compared with the Kalshi UI for multiple spread markets.

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
- `GET /v1/model/starter-status?date=YYYY-MM-DD`
- `GET /v1/model/parameters/active`
- `GET /v1/model/sources/status`
- `GET /v1/model/training/latest`
- `GET /v1/model/predictions?date=YYYY-MM-DD`
- `GET /v1/model/predictions/today`
- `POST /v1/sync/mlb-features?target_date=YYYY-MM-DD`
- `POST /v1/sync/mlb-features?target_date=YYYY-MM-DD&include_modules=all`
- `POST /v1/sync/mlb-starters?target_date=today_et`
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
- `POST /v1/jobs/run/spread-audit?target_date=YYYY-MM-DD`
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
15. `GET /v1/model/features/coverage?date=YYYY-MM-DD` reports the 17-module `core_modules`, `completeness_summary`, and `module_completeness` without inventing missing lineup, weather, injury, umpire, team-total, or sportsbook data.
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

Expected: lineup snapshots where MLB posted lineups, weather rows for stadiums with coordinates, pitcher rows where probable starters are available, partial bullpen proxies when exact reliever data is unavailable, and `data_quality_reason` caps when critical modules remain missing. Coverage and detail responses should include all 17 mature modules under `core_modules`, per-date totals under `completeness_summary`, and per-module counts/reasons under `module_completeness`.

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

Expected: `last_attempted_sync`, `validation_status`, `last_error`, `latest_errors`, per-table `status_counts`, and `latest_feature_completeness` are present. `pybaseball_available`, `pybaseball_version`, `pybaseball_module_path`, import errors, attempted functions, DB cache status, and `advanced_stats_status` should be visible. When pybaseball succeeds and rows match, team/pitcher/bullpen rows should include `source=pybaseball_public_stats_v1`; when it fails, the response should expose the source error and fall back to partial derived rows instead of returning a blank 500.

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

4. Confirm `feature_version` is `mature_mlb_features_v2`, `core_modules` lists the 17 mature modules, `completeness_summary` totals the selected slate, critical module warnings are explicit, and static park/travel features are separate from optional network weather/provider inputs.
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

5. Expected: team strength, handedness/platoon, starter recent, and starter workload modules prefer `source=mlb_stats_api_primary_v1` when those rows are available. Detail responses should also expose each mature module under `module_completeness` with available/partial/missing/unavailable status and reasons. Statcast/Savant contact quality is secondary enrichment, and `derived_homerun_v2` remains partial fallback.
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

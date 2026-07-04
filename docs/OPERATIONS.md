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
- PR3s records compact exposure-taxonomy metadata on candidates and paper trades so operators can distinguish economic exposure from Kalshi YES/NO contract mechanics. These labels and line classes are display/diagnostic fields only and must not be used as a hidden replacement for existing paper trade caps, settlement, model math, or source-ingestion rules.
- PR3x adds a paper-only risk-governance pass after the live-like selector and before legacy caps/sizing. It enforces family, concept-cluster, same-game, alternate-line, low-price/tail, and drawdown-halt controls while preserving all scored candidates for diagnostics and governance. It blocks new paper trades only; it does not close positions or change settlement.
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

PR3s exposure taxonomy is also reporting-only. Candidate sweeps may persist `economic_exposure_*`, `concept_cluster_key`, `same_game_concept_cluster_key`, and line-ladder fields such as `line_class` and `line_ladder_distance_from_central`; these values are derived from current Kalshi mappings/contracts and are bounded to the sweep's current candidate set. They do not call sportsbook APIs, do not infer consensus lines, do not scan historical ladders, and do not change any existing candidate decision, risk-cap, settlement, governance, cron, WebSocket, or live-execution behavior.

PR3s.1 candidate-level taxonomy validation should rerun the known nonzero dry-run target date `2026-07-04` against the production backend `https://homerun-production-2551.up.railway.app`. Prompt only for the internal API key. Run component setup, then a dry-run candidate sweep with `dry_run_candidates_only=true`, `min_time_to_start_minutes=0`, and `max_time_to_start_minutes=1800`. The sweep must evaluate nonzero candidates, report `heavy_feature_sync_skipped=true`, create no paper trades, and expose non-null compact PR3s fields such as `economic_exposure_label`, `economic_exposure_key`, `contract_mechanics_label`, `concept_cluster_key`, `same_game_concept_cluster_key`, `line_class`, and both PR3s version markers in the sweep body and/or `/v1/model/predictions?date=2026-07-04`. Finish by checking the dashboard remains compact and `/v1/system/status` still reports paper mode, live trading disabled, kill switch enabled, and demo Kalshi.

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
8. Keep PR3o spread audit and any future spread-activation work separate.

## PR3n.1 First-Five Lifecycle Settlement

First-five paper markets use their own lifecycle. Once official MLB linescore data contains complete home and away runs for innings 1-5, first-five winner, spread, and total paper trades can settle even while the full game is still in progress. Full-game winner, spread, and total trades still wait for the full game to be final or void.

Completeness criteria:

- The game must have a usable `linescore.innings` payload.
- The first five inning rows must exist.
- Each of those five innings must include both `away.runs` and `home.runs`.
- If any of those values are missing while the game is still open, settlement records `first_five_not_complete` and leaves the trade open.
- If the game is final/closed but the first-five linescore is still unusable, settlement records `missing_f5_linescore` and leaves the trade open for manual/source follow-up.

Price refresh guard:

- If a first-five trade is already settlement-ready, open-position price refresh skips it and leaves final pricing to settlement.
- If the first-five linescore is not complete and Kalshi reports that first-five market as closed, price refresh preserves the existing paper mark instead of stamping a misleading 0c/99c closed-market price.
- Price refresh still marks ordinary open full-game positions and unresolved open first-five positions when the market remains open.

Dashboard behavior:

- No dashboard schema or frontend behavior changed.
- First-five trades should leave the open table earlier once settlement runs after the fifth inning is official.
- Closed-position P/L should come from settlement, not from a temporary closed-market quote.

After deployment, validate:

1. Run settlement during an in-progress game after the fifth inning is official and confirm supported first-five paper trades settle.
2. Confirm supported full-game paper trades for the same in-progress game remain open with `not_final_full_game`.
3. Confirm an incomplete first-five linescore leaves first-five trades open with `first_five_not_complete`.
4. Confirm open-position price refresh reports `skipped_first_five_settlement_ready` for first-five trades whose first five innings are complete.
5. Confirm open-position price refresh reports `skipped_closed_f5_market` and preserves the prior mark when Kalshi closes a first-five market before the linescore is complete.
6. Confirm the dashboard open/closed position tables reflect settlement state after settlement runs.
7. Confirm no live execution, WebSocket, cron schedule, candidate-generation, EV/model, risk-cap, market-discovery, sportsbook, team-total, umpire, defense, or full-game spread activation behavior changed.

## PR3o Full-Game Spread Audit Only

PR3o is audit-only. Full-game spread paper trading remains disabled with `PAPER_SPREAD_TRADING_ENABLED=false`, and the protected `spread-audit` job does not run market-family mapping sync, create paper trades, write settlements, mutate candidates, or enable mappings for trading.

Run the audit manually:

```powershell
python -m app.jobs.runner --job spread-audit --target-date today_et --min-time-to-start-minutes 45 --max-time-to-start-minutes 180
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/jobs/run/spread-audit?target_date=today_et&min_time_to_start_minutes=45&max_time_to_start_minutes=180"
```

Audit taxonomy:

- `trusted_audit_only`: team, line, YES/NO complement, and push/settlement evidence are coherent enough for the PR3q paper gate when `PAPER_FULL_GAME_SPREAD_TRADING_ENABLED=true`.
- `needs_review`: generic review bucket when evidence exists but is incomplete.
- `missing_line`: line value cannot be verified.
- `ambiguous_team_selection`: selected team cannot be uniquely verified against the mapped MLB game.
- `ambiguous_yes_no_semantics`: YES/NO text or complement relation is missing or contradictory.
- `ambiguous_line_direction`: line sign/direction was not verified from Kalshi rules text or supporting text conflicts with rules text.
- `settlement_text_unverified`: rules/settlement text is missing.
- `push_behavior_uncertain`: integer spread can push but rules do not verify push/void/refund behavior.
- `unsupported`, `parse_error`, `missing_market_data`, and `missing_game_mapping`: audit cannot safely evaluate the row.

Per-market rows should include ticker, event ticker, title/subtitle/rules/YES/NO text, mapped MLB game, selected team, line value/sign/direction, rules threshold, settlement formula, `YES selected team covers`, `NO selected team does not cover`, normalized NO equivalent, NO text/complement provenance, whether NO is a true complement, push condition, push-rule verification, and read-only settlement preview. Full-game spread rules text is primary: `Team wins by more than X runs` normalizes to the selected team laying `-X`. Final-game previews compute selected score, opponent score, adjusted margin, YES outcome, NO outcome, and push state without writing settlement rows. Non-final games report `preview_status=pending_final` plus the same formula fields.

Interpretation:

- `trusted_audit_only_count` is the only full-game spread audit bucket eligible for PR3q paper trading when the separate full-game spread flag is enabled.
- Any `needs_review`, ambiguous, missing, or push-uncertain status must remain blocked.
- Candidate sweeps should show full-game spread candidates as `no_trade_full_game_spread_trading_disabled` while `PAPER_FULL_GAME_SPREAD_TRADING_ENABLED=false`. When that flag is true, only cached `trusted_audit_only` rows can pass; untrusted rows must return explicit full-game spread audit rejection reasons.
- The broad `PAPER_SPREAD_TRADING_ENABLED` flag still controls first-five spread diagnostics. It does not enable full-game spread paper trading.

After deployment, validate:

1. Confirm `/v1/system/status` still reports paper mode, demo Kalshi environment, live trading disabled, and execution kill switch enabled.
2. Confirm default `/v1/dashboard/summary` still has the PR3m.1 observation cutoff active.
3. Run `POST /v1/jobs/run/spread-audit?target_date=today_et&min_time_to_start_minutes=45&max_time_to_start_minutes=180`.
4. Confirm the result includes `audit_scope=full_game_spread`, `read_only=true`, `mapping_mutations=0`, `settlement_rows_created=0`, `paper_trades_created=0`, `status_counts`, and `examples_by_reason`.
5. Inspect trusted and review examples manually against Kalshi UI text before considering spread activation.
6. Run a dry candidate sweep with `PAPER_FULL_GAME_SPREAD_TRADING_ENABLED=false` and confirm `feature_sync_mode=cache_only`, pregame context is present, full-game spread trades are not created, and PR3k controls remain active.
7. If enabling full-game spread paper observation, set only `PAPER_FULL_GAME_SPREAD_TRADING_ENABLED=true`, rerun candidate sweep, and confirm only `trusted_audit_only` full-game spread rows can create paper trades.
8. Run settlement and confirm trusted full-game spread rows settle only after final game status, while untrusted/missing audit rows stay open with spread audit skip reasons.
9. Confirm no live execution, WebSocket, cron schedule, model/EV/risk-cap, source-sync, defense, sportsbook/team-total/umpire, or market-discovery behavior changed.

## PR3p Clean Governance Training Autonomy

Governance training has an explicit clean cutoff. The default is `MODEL_GOVERNANCE_CLEAN_START_AT=2026-07-02T00:00:00-04:00`, which is midnight ET at the PR3m.1 observation cutover.

Operational meaning:

- Governance still runs from the protected governance job or `/v1/run/model-governance`.
- Training, calibration, challenger creation, threshold records, and promotion decisions use active-epoch mature resolved candidates only after the clean cutoff.
- A candidate must have both `target_date` and `evaluated_at` at or after the cutoff to be clean.
- Pre-clean candidates and legacy governance artifacts remain stored for audit/history but are not treated as current governance-ready artifacts.
- `/v1/model/governance/status` and `/v1/dashboard/summary` model status report raw resolved samples, clean resolved samples, pre-clean exclusion counts, ignored pre-clean artifacts, and the autonomous parameter registry.
- The registry is diagnostic. Currently governed items are bounded market-family/global probability offsets and active parameter-version promotion; safety flags, risk caps, and spread activation remain manual.

After deployment, validate:

1. Confirm `/v1/system/status` still reports paper mode, demo Kalshi environment, live trading disabled, and execution kill switch enabled.
2. Confirm `GET /v1/model/governance/status` includes `clean_training_start_at`, `raw_resolved_mature_samples`, `clean_resolved_mature_samples`, `pre_clean_excluded_samples`, `ignored_pre_clean_artifacts`, and `governance_parameter_registry`.
3. If raw samples exist but clean samples are below `MODEL_MIN_SAMPLES_TRAIN`, governance should return `skipped_insufficient_samples` with a clean-window reason and should not create a challenger.
4. If clean samples later meet thresholds, challenger training/promotion should still require the existing sample, logloss, and ECE guardrails.
5. Confirm dashboard model status shows the same clean sample counts as `/v1/model/governance/status`.
6. Confirm no live execution, WebSocket, cron schedule, model formula, EV threshold, risk-cap, source-sync, settlement, market discovery, sportsbook/team-total/umpire, or full-game spread activation behavior changed.

## PR3p.1 Dashboard Payload Memory Hygiene

Default dashboard summaries are intentionally compact. Use the compact default for the Vercel dashboard and normal operator refreshes:

```powershell
Invoke-RestMethod "https://YOUR-RAILWAY-API/v1/dashboard/summary"
```

The default response should not include full job result trees, `JobRun.steps`, source inventory tables, raw payloads, feature blobs, candidate IDs, candidate-level counterfactual arrays, full governance registry lists, or per-market spread audit rows. It should include compact status and counts for:

- latest paper jobs
- candidate sweep window and risk/cap summaries
- source health status counts and latest sync status
- governance clean sample counts and registry counts
- aggregate candidate quality/EV diagnostics

Use explicit dashboard debug flags only for short manual investigations, never as the frontend polling URL:

```powershell
Invoke-RestMethod "https://YOUR-RAILWAY-API/v1/dashboard/summary?include_job_results=true"
Invoke-RestMethod "https://YOUR-RAILWAY-API/v1/dashboard/summary?include_candidate_diagnostics=true"
Invoke-RestMethod "https://YOUR-RAILWAY-API/v1/dashboard/summary?include_source_details=true"
Invoke-RestMethod "https://YOUR-RAILWAY-API/v1/dashboard/summary?include_governance_details=true"
Invoke-RestMethod "https://YOUR-RAILWAY-API/v1/dashboard/summary?include_diagnostics=true"
```

Debug dashboard responses still cap samples and omit raw payload/features/rationale blobs. For full protected diagnostics, prefer the dedicated internal endpoints:

- `GET /v1/model/sources/status`
- `GET /v1/model/governance/status?include_details=true`
- `GET /v1/model/training/latest`
- `GET /v1/model/predictions?date=YYYY-MM-DD`

After deployment, validate:

1. Confirm `/v1/dashboard/summary` returns quickly and Railway logs include `endpoint_metrics endpoint=/v1/dashboard/summary`.
2. Confirm the logged `response_size_bytes` is materially smaller than the prior failing payload.
3. Confirm the Vercel dashboard still loads model, job, portfolio, open positions, and closed positions sections.
4. Confirm default summary JSON does not contain `top_counterfactual_candidates_blocked_by_quality`, `source_inventory`, full `source_health`, raw payloads, or per-market spread audit `items`.
5. Confirm `/v1/model/governance/status` is compact by default and `include_details=true` returns the protected full registry.
6. Confirm no live execution, cron schedule, candidate generation, model math, EV threshold, risk cap, source sync behavior, settlement, market discovery, WebSocket, sportsbook/team-total/umpire, spread activation, secrets, or environment variables changed.

## PR3p.2 Governance And Dashboard Query Materialization Hotfix

PR3p.2 fixes the remaining production memory problem after PR3p.1. PR3p.1 made the response payload compact; PR3p.2 makes the backend query path compact so status endpoints do not materialize large candidate or feature JSON before returning the small payload.

Expected behavior:

- `/v1/model/governance/status` uses aggregate count queries for raw, clean, and pre-clean mature sample counts. It must not build the full training candidate dataset.
- `/v1/dashboard/summary` uses compact governance status, grouped candidate decision counts, and column-only feature status rows by default.
- Actual governance training still builds the full clean training dataset only when `POST /v1/run/model-governance` or the governance job is explicitly run.
- Deep diagnostics remain protected and opt-in. Do not use debug dashboard flags as the frontend polling URL.

After deployment, validate:

1. Restart/redeploy the backend once and wait about 2 minutes for Railway memory to settle.
2. Confirm `/health` returns quickly.
3. Confirm `/v1/system/status` returns quickly and still reports paper mode, demo Kalshi environment, live trading disabled, execution kill switch enabled, and database ready.
4. Call protected `GET /v1/model/governance/status`.
5. Confirm it returns in under 5 seconds and includes `raw_resolved_mature_samples`, `clean_resolved_mature_samples`, `pre_clean_excluded_samples`, and `clean_filter_exclusion_counts`.
6. Call default `GET /v1/dashboard/summary?closed_date=2026-07-02`.
7. Confirm it returns in under 5 seconds, stays compact, and does not include candidate IDs, raw payloads, full source inventory, full job steps, or per-market spread audit rows.
8. Repeat the default dashboard summary call 3-5 times with 30-45 seconds between calls.
9. Confirm Railway memory does not staircase upward and there are no 502 responses or OOM warnings.
10. Open one Vercel dashboard tab and watch Railway memory for about 5 minutes.
11. Confirm no live execution, cron schedule, candidate generation, model math, EV threshold, risk cap, source sync behavior, settlement, market discovery, WebSocket, sportsbook/team-total/umpire, spread activation, secrets, or environment variables changed.

## PR3q Full-Game Spread Paper Gate

Full-game spread paper trading is paper-only and separately disabled by default.

## PR3r Governance Memory And Portfolio Time-Series Fidelity

PR3r keeps PR3p/PR3p.2 compact status behavior and makes the explicit governance job itself memory-light. The governance job trains from scalar clean sample rows instead of materializing full `ModelCandidate` ORM rows and JSON payloads. It preserves the clean cutoff, 70/30 chronological split, bounded calibration offsets, threshold records, and promotion guardrails. Governance results include compact `governance_phase_metrics` with phase durations and RSS values when the runtime can report RSS.

PR3r also improves portfolio chart fidelity. Balance snapshot creation skips duplicate rows when cash and portfolio value did not change, but still creates a new row when a trade entry, mark refresh, settlement, or manual refresh changes cash or portfolio value. `/v1/dashboard/summary` now sends a bounded 500-point active-epoch portfolio series from actual balance snapshots, preserving first/latest points and intraday high/low extrema instead of returning only the newest snapshots. The response exposes `portfolio_series_source`, `portfolio_series_point_count`, `portfolio_series_truncated`, and `portfolio_series_preserves_intraday_fluctuations=true`.

PR3r.1 tightens the dashboard source priority. If usable active-epoch balance snapshots exist after the observation cutoff on the same clean basis as current dashboard totals, the default summary must report `portfolio_series_source=active_epoch_balance_snapshots` and include `portfolio_series_active_epoch_id`, `portfolio_series_started_at`, and `portfolio_series_ended_at`. If filtered-out pre-observation trades can still affect whole-epoch snapshots, the summary falls back to `observation_filtered_portfolio_totals` with `portfolio_series_fallback_reason=pre_observation_trades_can_affect_snapshot_series`; if no usable snapshots exist, the fallback reason is `no_usable_active_epoch_balance_snapshots`. Snapshot points remain compact and must not include raw payloads, candidate ids, full positions, audit rows, or training data.

After deployment, validate:

1. Run protected `POST /v1/jobs/run/governance`.
2. Confirm the job result includes `governance_phase_metrics` and does not log raw candidate payloads, features, rationale blobs, secrets, or market payloads.
3. Confirm `/v1/model/governance/status` and default `/v1/dashboard/summary` still return compactly.
4. Trigger a price refresh or settlement when open paper positions exist, then confirm balance snapshots are created only when portfolio values changed.
5. Confirm `/v1/dashboard/summary` includes the portfolio series metadata fields, uses `active_epoch_balance_snapshots` when clean-basis snapshot rows exist, and preserves intraday rises/falls.
6. If no balance snapshots exist, confirm the summary falls back to `observation_filtered_portfolio_totals` with `portfolio_series_fallback_reason=no_usable_active_epoch_balance_snapshots`; if excluded pre-observation trades can still affect snapshots, confirm the fallback reason is `pre_observation_trades_can_affect_snapshot_series`.
7. Confirm no live execution, cron schedule, candidate generation, model math, EV threshold, risk cap, settlement logic, market discovery, WebSocket behavior, source ingestion, credentials, or environment variables changed.

Default:

- `PAPER_FULL_GAME_SPREAD_TRADING_ENABLED=false`
- Full-game spread candidates return `no_trade_full_game_spread_trading_disabled`.
- First-five spread behavior remains controlled by `PAPER_SPREAD_TRADING_ENABLED`.

Enablement rule:

- Set `PAPER_FULL_GAME_SPREAD_TRADING_ENABLED=true` only after validating PR3o.1 audit output.
- Candidate sweep must use cached mapping audit metadata. It must not run `spread-audit`, full feature sync, pybaseball, FanGraphs, Statcast/Savant, Open-Meteo, sportsbook APIs, team totals, or umpire logic.
- A full-game spread row may paper trade only when audit metadata is `trusted_audit_only` with full-game scope, verified selected team, verified line direction, true YES/NO complement, safe push/no-push handling, threshold, and settlement formula.
- Missing, parse-error, unsafe, needs-review, ambiguous, settlement-text-unverified, push-uncertain, or otherwise untrusted audit rows must remain no-trade with explicit full-game spread audit reasons.
- Full-game spread settlement revalidates the trusted audit before resolving. Missing/untrusted audit metadata leaves the trade open with a spread audit skip reason.

After deployment, validate:

1. Confirm `/v1/system/status` still reports paper mode, demo Kalshi environment, live trading disabled, and execution kill switch enabled.
2. Confirm `/v1/dashboard/summary` model status exposes `paper_full_game_spread_trading_enabled`, `full_game_spread_audit_gate_enabled`, and compact latest spread-audit counts.
3. With the new flag false, run candidate sweep and confirm full-game spread rows do not create trades.
4. With the new flag true in a controlled paper environment, confirm only `trusted_audit_only` rows can create full-game spread paper trades.
5. Confirm untrusted rows stay blocked with explicit full-game spread audit reasons and are not training eligible.
6. Run settlement after final scores and confirm trusted spread trades settle from the audited formula while untrusted rows are skipped.
7. Confirm no live execution, cron schedule, model math, EV threshold, risk cap, market discovery, source sync, WebSocket, sportsbook/team-total/umpire, credential, or frontend polling behavior changed.

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
- Full-game spread returns blocked diagnostics while `PAPER_FULL_GAME_SPREAD_TRADING_ENABLED=false`; when enabled, it still requires cached `trusted_audit_only` metadata.
- If no trades are created, the dominant reason should be explicit in `decision_counts`, `candidate_diagnostics`, and `quality_ev_diagnostics`.

## PR3d Hotfix 3 Spread Audit And Correlation Validation

Run the spread audit before considering `PAPER_SPREAD_TRADING_ENABLED=true`:

```powershell
python -m app.jobs.runner --job spread-audit --target-date today_et --min-time-to-start-minutes 45 --max-time-to-start-minutes 180
Invoke-RestMethod -Method Post -Headers @{"X-API-Key"="YOUR_KEY"} "https://YOUR-RAILWAY-API/v1/jobs/run/spread-audit?target_date=today_et&min_time_to_start_minutes=45&max_time_to_start_minutes=180"
```

Expected audit behavior:

- The job is read-only; it does not create paper trades, settlement rows, candidates, or mapping/market metadata mutations.
- Verified full-game spread rows include rules-primary parsed selection, normalized lay line, threshold, formula, inning scope, settlement rule status, actual contract display, normalized equivalent display, NO complement provenance, push metadata, and raw Kalshi contract text.
- Ticker-only spread rows remain `needs_review` / parser-unverified.
- Full-game spread candidates return `no_trade_full_game_spread_trading_disabled` while `PAPER_FULL_GAME_SPREAD_TRADING_ENABLED=false`; when enabled, first-five spread candidates remain controlled by `PAPER_SPREAD_TRADING_ENABLED` and full-game spread candidates still require trusted audit metadata.

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

This hotfix does not enable live trading or new production cron services. Keep full-game spread paper disabled unless the PR3q flag is deliberately enabled after the audit output is manually compared with the Kalshi UI for multiple spread markets.

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

## PR3t Live-Like Paper Selector Validation

PR3t keeps candidate sweeps feature-cache-only and paper-only, but changes which scored candidates can become paper trades when `PAPER_SELECTOR_MODE=live_like`.

Use the known nonzero validation slate `2026-07-04`.

1. Confirm safety baseline:

```powershell
$base = "https://homerun-production-2551.up.railway.app"
$targetDate = "2026-07-04"
$apiKeySecure = Read-Host -Prompt "Paste internal API key" -AsSecureString
$apiKeyPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiKeySecure)
try { $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiKeyPtr).Trim() } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiKeyPtr) }
$headers = @{ "X-API-Key" = $apiKey }
$status = Invoke-RestMethod -Headers $headers "$base/v1/system/status"
$status.config | Select-Object paper_trading,live_trading_enabled,execution_kill_switch,kalshi_env,kalshi_credentials | Format-List
```

Expected: paper trading true, live trading false, kill switch true, Kalshi demo, credentials not set unless deliberately configured for safe demo reads.

2. Prepare the known slate with normal component setup:

```powershell
Invoke-RestMethod -Method Post -Headers $headers "$base/v1/sync/mlb-features?target_date=$targetDate&include_modules=all&refresh_schedule=true"
Invoke-RestMethod -Method Post -Headers $headers "$base/v1/run/market-family-discovery?target_date=$targetDate&force_refresh=false"
Invoke-RestMethod -Method Post -Headers $headers "$base/v1/sync/market-family-mappings?target_date=$targetDate"
```

Feature sync may degrade for optional public sources such as FanGraphs/pybaseball, but it should return structured JSON and leave mature cached snapshots usable.

3. Run the PR3t dry-run selector proof:

```powershell
$label = "pr3t_validation_dry_run_" + (Get-Date -Format "yyyyMMdd_HHmmss")
$dry = Invoke-RestMethod -Method Post -Headers $headers "$base/v1/jobs/run/candidate-sweep?target_date=$targetDate&min_time_to_start_minutes=0&max_time_to_start_minutes=1800&sweep_label=$label&dry_run_candidates_only=true"
$engine = $dry.result.result.paper_candidate_engine
if (-not $engine) { $engine = $dry.result.result.candidate_engine }
if (-not $engine) { $engine = $dry.result.result }
$engine | Select-Object target_date,selector_policy_version,selector_mode,feature_sync_mode,feature_sync_skipped,heavy_feature_sync_skipped,candidates_evaluated,paper_trades_created,selector_pre_cluster_eligible,selector_selected_after_cluster | Format-List
```

Expected: nonzero candidates evaluated, `dry_run_candidates_only=true`, zero paper trades, `feature_sync_mode=cache_only`, `heavy_feature_sync_skipped=true`, `selector_policy_version=pr3t_live_like_selector_v1`, `selector_mode=live_like`, and nonzero selector diagnostics where qualifying candidates exist.

4. Inspect predictions:

```powershell
$predictions = Invoke-RestMethod -Headers $headers "$base/v1/model/predictions?date=$targetDate"
($predictions | ConvertTo-Json -Depth 100) -split "`n" |
  Where-Object { $_ -match "economic_exposure|contract_mechanics|concept_cluster|line_class|selector_" } |
  Select-Object -First 260
```

Expected: compact PR3s taxonomy fields and PR3t selector fields appear on candidate rows. Do not expect raw features, raw payloads, full scoring rationale, or orderbook blobs.

5. Final dashboard compactness check:

```powershell
$final = Invoke-RestMethod -Headers $headers "$base/v1/dashboard/summary?closed_date=2026-07-02"
($final | ConvertTo-Json -Depth 80) -split "`n" |
  Where-Object { $_ -match "selector_|portfolio_series_source|raw_payload|features|scoring_rationale|orderbook_raw" } |
  Select-Object -First 220
```

Expected: compact selector summary/rationale may appear, portfolio series metadata remains compact, and raw payload/features/scoring rationale/orderbook blobs do not appear in the default dashboard response.

## PR3u Family/Scope Probability Adapter Validation

PR3u adds compact, versioned candidate-stage probability adapter metadata. It does not enable PR3v calibration training and does not change live execution, cron cadence, source ingestion, selector rules, risk caps, EV thresholds, settlement, or spread audit gates.

Use the known nonzero validation slate `2026-07-04`.

```powershell
$base = "https://homerun-production-2551.up.railway.app"
$targetDate = "2026-07-04"
$apiKeySecure = Read-Host -Prompt "Paste internal API key" -AsSecureString
$apiKeyPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiKeySecure)
try { $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiKeyPtr).Trim() } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiKeyPtr) }
$headers = @{ "X-API-Key" = $apiKey }
```

Run a dry-run candidate sweep without opening paper trades:

```powershell
$label = "pr3u_validation_dry_run_" + (Get-Date -Format "yyyyMMdd_HHmmss")
$dry = Invoke-RestMethod -Method Post -Headers $headers "$base/v1/jobs/run/candidate-sweep?target_date=$targetDate&min_time_to_start_minutes=0&max_time_to_start_minutes=1800&sweep_label=$label&dry_run_candidates_only=true"
$engine = $dry.result.result.paper_candidate_engine
if (-not $engine) { $engine = $dry.result.result.candidate_engine }
if (-not $engine) { $engine = $dry.result.result }
$engine | Select-Object target_date,dry_run_candidates_only,feature_sync_mode,heavy_feature_sync_skipped,candidates_evaluated,paper_trades,trades_created,probability_adapter_policy_version,probability_adapter_missing_count,probability_adapter_error_count | Format-List
$engine.probability_adapter_counts
$engine.probability_adapter_calibration_hook_counts
$engine.probability_adapter_family_counts
```

Expected: nonzero candidates evaluated, `dry_run_candidates_only=true`, zero paper trades, `feature_sync_mode=cache_only`, `heavy_feature_sync_skipped=true`, `probability_adapter_policy_version=pr3u_family_scope_probability_adapters_v1`, adapter counts by key/version, hook counts, family counts, and no missing adapter fields for candidates with supported market families.

Inspect compact prediction rows:

```powershell
$predictions = Invoke-RestMethod -Headers $headers "$base/v1/model/predictions?date=$targetDate"
($predictions | ConvertTo-Json -Depth 100) -split "`n" |
  Where-Object { $_ -match "probability_adapter|economic_exposure|selector_" } |
  Select-Object -First 320
```

Expected: candidate rows include compact PR3u fields such as `probability_adapter_key`, `probability_adapter_version`, `probability_adapter_policy_version`, `probability_adapter_family`, `probability_adapter_scope`, `probability_adapter_calibration_hook`, `probability_adapter_calibration_version`, and `probability_adapter_feature_policy_version`. Raw adapter metadata, raw features, raw payloads, and full scoring rationale blobs should not appear in this prediction endpoint.

## PR3v Family-Specific Calibration Governance Validation

PR3v keeps the system paper-only and turns PR3u adapter hooks into compact family/scope governance units. Validate it against the fixed production backend and known replay slate `2026-07-04`. Prompt only for the internal API key.

```powershell
$base = "https://homerun-production-2551.up.railway.app"
$targetDate = "2026-07-04"
$apiKeySecure = Read-Host -Prompt "Paste internal API key" -AsSecureString
$apiKeyPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiKeySecure)
try { $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiKeyPtr).Trim() } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiKeyPtr) }
$headers = @{ "X-API-Key" = $apiKey }
```

Run governance and inspect compact family/scope status:

```powershell
$govRun = Invoke-RestMethod -Method Post -Headers $headers "$base/v1/jobs/run/governance"
$govAfter = Invoke-RestMethod -Headers $headers "$base/v1/model/governance/status"
$paramsAfter = Invoke-RestMethod -Headers $headers "$base/v1/model/parameters/active"

($govRun, $govAfter, $paramsAfter | ConvertTo-Json -Depth 100) -split "`n" |
  Where-Object { $_ -match "family_scope|calibration_hook|full_game_total|first_five_total|full_game_winner|first_five_winner|full_game_spread|first_five_spread|clean_resolved|pre_clean|train_sample|holdout|adapter_error|promoted|skipped|active" } |
  Select-Object -First 900
```

Expected: the governance job succeeds, clean cutoff remains enforced, all six family/scope units are present, units below thresholds skip explicitly, trained units report family-specific challenger/calibration metadata, adapter errors are counted and excluded from training, and any promotion is isolated to that family/scope only.

Run a dry-run candidate sweep regression:

```powershell
$label = "pr3v_validation_dry_run_" + (Get-Date -Format "yyyyMMdd_HHmmss")
$dry = Invoke-RestMethod -Method Post -Headers $headers "$base/v1/jobs/run/candidate-sweep?target_date=$targetDate&min_time_to_start_minutes=0&max_time_to_start_minutes=1800&sweep_label=$label&dry_run_candidates_only=true"
$engine = $dry.result.result.paper_candidate_engine
if (-not $engine) { $engine = $dry.result.result.candidate_engine }
if (-not $engine) { $engine = $dry.result.result }
$engine | Select-Object target_date,dry_run_candidates_only,feature_sync_mode,heavy_feature_sync_skipped,candidates_evaluated,paper_trades_created,probability_adapter_policy_version,selector_policy_version,selector_mode | Format-List
```

Expected: nonzero candidates are evaluated, no paper trades are created, `feature_sync_mode=cache_only`, `heavy_feature_sync_skipped=true`, PR3u adapter metadata remains present, and PR3t selector metadata remains present.

Inspect prediction rows and final safety:

```powershell
$predictions = Invoke-RestMethod -Headers $headers "$base/v1/model/predictions?date=$targetDate"
$status = Invoke-RestMethod -Headers $headers "$base/v1/system/status"

($predictions | ConvertTo-Json -Depth 100) -split "`n" |
  Where-Object { $_ -match "probability_adapter|calibration_hook|calibration_version|calibration_status|family_scope|economic_exposure|selector_" } |
  Select-Object -First 600

$status.config | Select-Object paper_trading,live_trading_enabled,execution_kill_switch,kalshi_env,kalshi_credentials | Format-List
```

Expected: predictions still expose compact PR3u adapter fields, calibration hook/version/status, PR3s taxonomy, and PR3t selector fields without raw payload/features/scoring-rationale blobs. Final safety remains paper mode, demo Kalshi, live trading disabled, execution kill switch enabled, and no production credential requirement.

## PR3w Tail and Alternate-Line Probability Hardening Validation

PR3w keeps candidate sweeps cache-only and paper-only while hardening alternate/tail line probabilities before PR3t selection. Validate it against the fixed production backend and known replay slate `2026-07-04`. Prompt only for the internal API key.

```powershell
$base = "https://homerun-production-2551.up.railway.app"
$targetDate = "2026-07-04"
$apiKeySecure = Read-Host -Prompt "Paste internal API key" -AsSecureString
$apiKeyPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiKeySecure)
try { $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiKeyPtr).Trim() } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiKeyPtr) }
$headers = @{ "X-API-Key" = $apiKey }
```

Run a dry-run sweep and inspect hardening summary:

```powershell
$label = "pr3w_validation_dry_run_" + (Get-Date -Format "yyyyMMdd_HHmmss")
$dry = Invoke-RestMethod -Method Post -Headers $headers "$base/v1/jobs/run/candidate-sweep?target_date=$targetDate&min_time_to_start_minutes=0&max_time_to_start_minutes=1800&sweep_label=$label&dry_run_candidates_only=true"
$engine = $dry.result.result.paper_candidate_engine
if (-not $engine) { $engine = $dry.result.result.candidate_engine }
if (-not $engine) { $engine = $dry.result.result }
$engine | Select-Object target_date,dry_run_candidates_only,feature_sync_mode,heavy_feature_sync_skipped,candidates_evaluated,paper_trades_created,probability_hardening_policy_version,probability_hardening_enabled,probability_hardening_applied_count,probability_hardening_shadow_only_count,probability_hardening_block_recommendation_count | Format-List
$engine.probability_hardening_status_counts
$engine.probability_hardening_by_line_class
$engine.probability_hardening_monotonicity_status_counts
$engine.probability_hardening_consistency_status_counts
```

Expected: nonzero candidates are evaluated, no paper trades are created, `feature_sync_mode=cache_only`, `heavy_feature_sync_skipped=true`, `probability_hardening_policy_version=pr3w_tail_alternate_probability_hardening_v1`, hardening summary counts are present, and tail/ambiguous hardening does not enable live execution.

Inspect compact prediction rows:

```powershell
$predictions = Invoke-RestMethod -Headers $headers "$base/v1/model/predictions?date=$targetDate"
($predictions | ConvertTo-Json -Depth 100) -split "`n" |
  Where-Object { $_ -match "probability_hardening|probability_raw_adapter|line_class|selector_" } |
  Select-Object -First 700
```

Expected: candidate rows include compact PR3w fields such as `probability_before_hardening`, `probability_after_hardening`, `probability_hardening_delta`, `probability_hardening_line_class`, consistency/monotonicity statuses, dampening factor, shadow/block recommendation, and `probability_raw_adapter`. Raw features, scoring rationale blobs, and full payloads should not appear in this prediction endpoint.

## PR3w.1 Candidate-Level Hardening Population Validation

PR3w.1 fixes the production validation gap where hardening config appeared in dashboard status but newly scored candidate/prediction rows still showed null PR3w fields. Validate against the fixed production backend and known replay slate `2026-07-04`. Prompt only for the internal API key.

Use the same helper setup from the PR3w validation section, then run a dry-run candidate sweep:

```powershell
$label = "pr3w1_validation_dry_run_" + (Get-Date -Format "yyyyMMdd_HHmmss")
$dry = Invoke-RestMethod -Method Post -Headers $headers "$base/v1/jobs/run/candidate-sweep?target_date=$targetDate&min_time_to_start_minutes=0&max_time_to_start_minutes=1800&sweep_label=$label&dry_run_candidates_only=true"
$engine = $dry.result.result.paper_candidate_engine
if (-not $engine) { $engine = $dry.result.result.candidate_engine }
if (-not $engine) { $engine = $dry.result.result }
$engine | Select-Object target_date,dry_run_candidates_only,feature_sync_mode,heavy_feature_sync_skipped,candidates_evaluated,paper_trades_created,probability_hardening_policy_version,probability_hardening_enabled,probability_hardening_missing_count,probability_hardening_applied_count,probability_hardening_shadow_only_count,probability_hardening_block_recommendation_count | Format-List
$engine.candidate_probability_hardening_field_counts
$engine.probability_hardening_status_counts
$engine.probability_hardening_by_line_class
```

Expected: nonzero candidates, `dry_run_candidates_only=true`, zero paper trades, `feature_sync_mode=cache_only`, `heavy_feature_sync_skipped=true`, `probability_hardening_policy_version=pr3w_tail_alternate_probability_hardening_v1`, `probability_hardening_missing_count=0`, and non-null field counts from evaluated `ModelCandidate` rows.

Inspect predictions:

```powershell
$predictions = Invoke-RestMethod -Headers $headers "$base/v1/model/predictions?date=$targetDate"
($predictions | ConvertTo-Json -Depth 100) -split "`n" |
  Where-Object { $_ -match "probability_hardening|probability_raw_adapter|probability_before_hardening|probability_after_hardening|probability_adapter|selector_|economic_exposure|line_class" } |
  Select-Object -First 1000
```

Expected: prediction rows expose non-null `probability_hardening_policy_version`, `probability_hardening_enabled`, `probability_raw_adapter`, before/after probability, status/reason, line class, consistency/monotonicity status, dampening factor, and shadow/block recommendation. Winner/not-applicable rows should carry no-hardening metadata; central rows should carry no-hardening metadata; near/deep/tail rows should carry dampening metadata. PR3s taxonomy, PR3u adapter metadata, and PR3t selector metadata should remain present.

Finish with governance and safety checks:

```powershell
Invoke-RestMethod -Method Post -Headers $headers "$base/v1/jobs/run/governance"
$statusFinal = Invoke-RestMethod -Headers $headers "$base/v1/system/status"
$statusFinal.config | Select-Object paper_trading,live_trading_enabled,execution_kill_switch,kalshi_env,kalshi_credentials | Format-List
```

Expected: governance succeeds or cleanly skips by existing thresholds, and safety remains paper trading true, live trading false, kill switch true, Kalshi demo, and no production credential requirement. PR3w.1 does not add migrations, live execution, sportsbook data, team totals, umpire factors, MVE/multivariate markets, cron changes, source-ingestion changes, settlement changes, or raw payload/debug response bloat.

## PR3x Paper Risk Governance Validation

PR3x keeps the system paper-only and adds compact risk-governance controls after PR3t selection and PR3w hardening. Validate it against the fixed production backend and known replay slate `2026-07-04`. Prompt only for the internal API key.

Expected safe config:

- `PAPER_RISK_GOVERNANCE_ENABLED=true`
- `PAPER_RISK_GOVERNANCE_POLICY_VERSION=pr3x_paper_risk_governance_v1`
- `PAPER_DRAWDOWN_HALT_ENABLED=true`
- `PAPER_DRAWDOWN_HALT_THRESHOLD_ABS=50.00`
- `PAPER_DRAWDOWN_HALT_THRESHOLD_PCT=0.10`
- `PAPER_TRADING=true`
- `LIVE_TRADING_ENABLED=false`
- `EXECUTION_KILL_SWITCH=true`
- `KALSHI_ENV=demo`

Run the same dry-run validation pattern:

```powershell
$base = "https://homerun-production-2551.up.railway.app"
$headers = @{"X-API-Key"="YOUR_KEY"}
$targetDate = "2026-07-04"
$label = "pr3x_risk_governance_validation"
$dry = Invoke-RestMethod -Method Post -Headers $headers "$base/v1/jobs/run/candidate-sweep?target_date=$targetDate&min_time_to_start_minutes=0&max_time_to_start_minutes=1800&sweep_label=$label&dry_run_candidates_only=true"
$predictions = Invoke-RestMethod -Method Get -Headers $headers "$base/v1/model/predictions?date=$targetDate"
$system = Invoke-RestMethod -Method Get "$base/v1/system/status"
```

Expected candidate-sweep result:

- nonzero `candidates_evaluated`
- `dry_run_candidates_only=true`
- `paper_trades=0`
- `feature_sync_mode=cache_only`
- `heavy_feature_sync_skipped=true`
- `risk_governance_policy_version=pr3x_paper_risk_governance_v1`
- `risk_candidates_considered`, `risk_approved_before_caps`, `risk_approved_after_caps`, and risk rejection counts are present
- `candidate_risk_governance_field_counts.risk_governance_policy_version` equals the evaluated candidate count
- `risk_drawdown_summary.status` is present and is normally `clear` unless the active epoch has breached the configured drawdown halt

Expected prediction rows:

- Newly scored candidate rows expose compact non-null PR3x fields where candidate data exists: `risk_governance_policy_version`, `risk_governance_enabled`, `risk_governance_status`, `risk_governance_decision`, `risk_governance_rejection_reason`, family/cap statuses, drawdown status, approved-before/after flags, shadow/blocked flags, and risk rank/score.
- Raw features, full scoring rationale blobs, and unbounded candidate arrays should not appear in `/v1/model/predictions`.

Expected dashboard/system checks:

- `/v1/dashboard/summary` compact diagnostics include a `risk_governance` block after a candidate sweep.
- `/v1/system/status` reports the PR3x risk-governance config fields without secrets.
- Open and closed position tables remain compact and only show PR3x rationale for selected paper trades.

PR3x must not change live execution, cron schedules, source ingestion, model math, EV thresholds, settlement, WebSocket behavior, market discovery, production credentials, sportsbook/Odds API scope, team totals, umpire factors, or MVE/multivariate markets.

## Required Context Updates

Every PR must update `PROJECT_CONTEXT.md`.

At minimum, each PR should document:

- What changed.
- Whether the paper/live trading safety posture changed.
- Any schema or deployment changes.
- Any new assumptions about Kalshi markets, model behavior, fees, settlement, or operations.
- Validation performed.

# HOMERUN Project Context

This file is durable project memory for HOMERUN. Every future PR must update the change log at the bottom so the repo keeps a clear record of product, architecture, and safety decisions.

## 1. Product Goal

HOMERUN is a Kalshi-native MLB trading bot and operator dashboard. The long-term goal is to evaluate every MLB game on the slate and every mapped Kalshi MLB market as a candidate, then paper trade or eventually execute only when the model finds a positive expected-value contract.

The user wants the system to become as hands-off as possible. Calibration, thresholds, weights, retraining, monitoring, and model governance should eventually run inside the bot instead of depending on manual daily tuning.

## 2. Current Scope

PR 3b builds on the merged PR 3a discovery and operator-repair workflow:

- FastAPI backend in `apps/api`.
- Next.js TypeScript frontend in `apps/web`.
- PostgreSQL-ready database models and Alembic migrations.
- MLB schedule ingestion from the public MLB Stats API.
- Targeted Kalshi MLB market resolution from MLB game rows using the empirically observed `KXMLBGAME` full-game winner family.
- Kalshi yes/no orderbook parsing and raw payload storage for targeted markets only.
- Auditable MLB game to Kalshi market mapping with confidence and rationale.
- Conservative paper candidate and paper-trade generation using a transparent MLB run-distribution probability model.
- MLB results sync for completed games.
- Paper settlement and realized P/L tracking for validated supported MLB market families.
- Paper balance snapshots from starting balance, open cost, realized P/L, and open mark value.
- Mature MLB feature snapshot storage for model candidates with explicit source-status markers.
- Automated model governance runs that skip training/calibration/promotion until sample thresholds are met.
- PR3a governance repair that normalizes resolved `KXMLBGAME` candidates to `full_game_winner` before counting samples.
- Kalshi MLB market-family discovery plus mapping sync for validated spread, total, and first-five families.
- REST last-mark refresh for open paper positions.
- Database-backed dashboard API responses when data exists.
- Light-theme trading-terminal dashboard that renders portfolio snapshots, chart range/P&L controls, paper metrics, open positions, closed positions by selected date, game status, last mark time, model status, and system status.
- Railway backend and PostgreSQL setup documentation.
- Vercel frontend setup documentation.
- CI scaffolding for backend tests and frontend checks.

The system still has no live trading, no production credentials requirement, no sportsbook logic, and no support for team totals, multivariate/MVE markets, guessed/retired prefixes, or live execution. All supported market families are paper-only and require validated `paper_supported` mapping metadata.

## 3. Non-Goals

This project is not a sportsbook betting app. Do not add sportsbook assumptions, sportsbook APIs, sportsbook odds conversion, DraftKings, FanDuel, or Odds API behavior.

PR 3 still intentionally excludes:

- Live order placement.
- Production Kalshi credentials.
- A trained challenger model with enough sample support for promotion.
- Admin password pages.
- Monthly calendars.
- Hidden dashboard-triggered worker automation.
- Automatic trained-model promotion on tiny samples.
- Fake spread, total, or first-five market tickers.

## 4. Architecture Decisions

The repo is a small monorepo:

- `apps/api` owns the backend API, typed configuration, database models, migrations, and backend tests.
- `apps/web` owns the frontend dashboard.
- Railway is the intended backend and PostgreSQL host.
- Vercel is the intended frontend host.

The backend exposes these read endpoints in PR 2:

- `GET /health`
- `GET /v1/dashboard/summary`
- `GET /v1/system/status`
- `GET /v1/games/today`
- `GET /v1/markets/today`
- `GET /v1/candidates/today`

It also exposes controlled internal run endpoints:

- `POST /v1/sync/mlb-schedule`
- `POST /v1/sync/mlb-results?target_date=YYYY-MM-DD`
- `POST /v1/sync/kalshi-markets`
- `POST /v1/run/paper-candidate-engine`
- `POST /v1/run/paper-settlement-sync?target_date=YYYY-MM-DD`
- `POST /v1/run/balance-snapshot`
- `POST /v1/run/model-governance`
- `POST /v1/run/open-position-price-refresh`
- `POST /v1/run/market-family-discovery?target_date=YYYY-MM-DD`
- `POST /v1/sync/market-family-mappings?target_date=YYYY-MM-DD`
- `GET /v1/market-families/discovery?date=YYYY-MM-DD`
- `GET /v1/market-families/discovery-preview?date=YYYY-MM-DD`
- `GET /v1/market-families/mappings?date=YYYY-MM-DD`
- `GET /v1/kalshi/resolve-preview?date=YYYY-MM-DD`

The database layer is PostgreSQL-ready but the API can still boot locally without a database for frontend and health-check work.
If `BACKEND_API_KEY` is configured, internal POST endpoints require `X-API-Key`.

## 5. Paper Trading First

The model starts in paper-trading mode. Paper trading is not a temporary UI label; it is the required safety posture for this project until live execution is deliberately added in a later PR.

Default execution flags:

- `PAPER_TRADING=true`
- `LIVE_TRADING_ENABLED=false`
- `EXECUTION_KILL_SWITCH=true`
- `KALSHI_ENV=demo`

Future live execution must be disabled by default, protected by hard environment guards, and paired with a kill switch.

## 6. No Sportsbook Assumptions

All future trading logic must use Kalshi market structure. The system should reason in yes/no contract prices, contract quantity, fees, settlement value, and resolution state.

Do not model this as a sportsbook odds product. Do not add sportsbook concepts unless a future PR explicitly documents why they are needed, and do not make sportsbook APIs part of the system.

## 7. Hold-to-Settlement Assumption

The starting assumption is hold-to-settlement. Candidate scoring, expected value, paper-trading performance, and future position accounting should assume the system holds contracts through resolution unless a later PR adds a documented exit strategy.

Current price is still useful for dashboard visibility, but realized performance should be based on settlement.
PR 2 stores `fee_estimate` as `0` for candidate and paper-trade rows until a later PR implements Kalshi fee modeling.

## 8. Candidate Dataset Philosophy

Training data should include every mapped candidate with a clean outcome, not only trades the bot entered. This matters because training only on paper/live trades would bias the dataset toward past threshold decisions.

Future ingestion and training jobs should preserve:

- Mapped MLB game and Kalshi market.
- Model version.
- Features available at the decision time.
- Market price at the decision time.
- Model probability and fair value.
- Whether the bot traded.
- Final clean outcome or reason the candidate was excluded.

## 9. Automated Model Governance Requirement

The long-term system should automate model governance. Future PRs should move toward:

- Scheduled retraining.
- Calibration tracking.
- Threshold selection based on backtests and paper-trading results.
- Drift checks.
- Versioned model promotion.
- Guardrails that prevent unsafe model versions from trading.

Manual constants and thresholds should be treated as temporary until they can be measured and governed by the bot.

## 10. Dashboard Requirements

The dashboard should be a light-theme trading dashboard, not a marketing landing page.

Required operator views:

- Portfolio value line graph.
- Win rate.
- ROI.
- Profit and loss.
- Record.
- Open and closed positions tables.
- Entry price, current price, quantity, status, and resolution state.
- Model status panel.
- System status panel.

The dashboard should clearly show paper mode, live trading disabled, and kill switch on based on backend API state. It should also show a clear light-theme error state when the API is unavailable.
Frontend user-facing dashboard labels should stay uppercase.

There should be no separate password-protected admin page and no monthly calendar.

## 11. Deployment Targets

Backend target:

- Railway service rooted at `apps/api`.
- Railway PostgreSQL service.
- `DATABASE_URL` supplied by Railway PostgreSQL.
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

Frontend target:

- Vercel project rooted at `apps/web`.
- `NEXT_PUBLIC_API_BASE_URL` points to the Railway backend URL.
- `NEXT_PUBLIC_REFRESH_MS` controls dashboard polling and defaults to `30000`.

Production Kalshi credentials should not be added during PR 3.

## 12. PR2.5 Kalshi Market Resolution

PR 2.5 switches the primary Kalshi market sync from broad universe crawling to targeted MLB resolution:

- MLB schedule sync creates or updates `mlb_games`.
- The Kalshi resolver builds candidate event tickers around each game's Eastern first pitch.
- The initial supported family is `full_game_winner` with series ticker `KXMLBGAME`.
- Observed event format: `KXMLBGAME-YYMMMDDHHMMAWAYHOME`, for example `KXMLBGAME-26JUN261840HOUDET`.
- Observed market format: `{EVENT}-{TEAM}`, for example `KXMLBGAME-26JUN261840HOUDET-HOU`.
- Spread, total, and first-five families remain discovery-only until validated and must not be faked.
- `mve_filter=exclude` is used for narrow market queries where supported.
- Multivariate markets are rejected for normal MLB mapping in PR 2.5.
- Broad discovery is diagnostic-only, disabled by default, bounded by page/limit env vars, and must not block targeted resolver success.

New PR 2.5 migration:

- `0003_pr2_5_targeted_kalshi_resolver.py`
- Adds MLB home/away abbreviations.
- Adds Kalshi raw status.
- Adds mapping resolver strategy and validation status.

## 13. PR3 Paper Results And Model Workflow

PR 3 adds the first closed-loop paper-results workflow:

- `app.jobs.mlb_results_sync` updates `mlb_games` with final statuses, scores, and raw MLB payloads.
- `app.jobs.paper_settlement_sync` settles only supported `full_game_winner` paper trades.
- Settlement determines the selected team from the Kalshi ticker suffix and computes hold-to-settlement P/L.
- Fees remain structured as `fee_estimate` / `fee_paid` with a zero value until a later PR implements the exact Kalshi fee formula.
- `app.jobs.balance_snapshot` creates portfolio snapshots from `PAPER_STARTING_BALANCE`, open trade cost, realized P/L, and current marks.
- `/v1/dashboard/summary` reads snapshots, settled paper trades, and open positions/trades for cash, portfolio value, P/L, ROI, record, and readable contract labels.

PR 3 also adds the first model-feature and governance infrastructure:

- Candidate generation builds `mlb_features_v1` snapshots from available MLB/Kalshi data.
- Missing starters, lineup, weather, injury, splits, and richer team metrics are explicit in JSON with `source_status: missing`.
- The active model is `heuristic_full_game_winner_v1`, a conservative deterministic model that does not blend toward Kalshi market price.
- Model governance writes training and calibration run records and skips automatic training/promotion until the resolved-sample threshold is met.
- Future trained models must use clean resolved candidates, chronological validation, calibration metrics, and documented champion/challenger promotion rules.

Frontend PR 3 display changes:

- Open positions show readable labels such as `FULL GAME WINNER · SEA @ PIT · PIT`.
- Raw Kalshi tickers remain visible as secondary text under the market label.
- The dashboard keeps the light terminal style, uppercase text, full-width chart, metrics, contracts table, model panel, and system panel.

Remaining PR 3 limitations:

- Only full-game winner markets are supported.
- Spread, total, and first-five market families remain pending discovery and must not be faked.
- The heuristic model is intentionally conservative and should be replaced only after governance has enough clean resolved samples.
- Live execution remains disabled and no live order path exists.

Expected next PR scope:

- Schedule the hands-off jobs in deployment infrastructure.
- Expand MLB feature coverage from reliable Stats API sources.
- Add fee-aware settlement once exact fee terms are verified.
- Begin trained challenger evaluation when enough resolved candidates exist.

## 14. PR3a Market-Family Discovery And Dashboard Repair

PR 3a adds production repairs and discovery-only market-family infrastructure:

- Migration `0005_pr3a_discovery.py` adds market-family discovery run/item audit tables and `paper_trades.current_price_updated_at`.
- The supported trading/settlement/model family remains `full_game_winner` only.
- `full_game_spread`, `full_game_total`, `first_five_winner`, `first_five_spread`, and `first_five_total` remain discovery-only and are not wired into candidate generation, paper trading, settlement, or model scoring.
- `app.jobs.market_family_discovery` and `POST /v1/run/market-family-discovery` produce a structured deterministic `by_family` report and persisted audit rows.
- The deterministic registry contains `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`, `KXMLBF5`, `KXMLBF5SPREAD`, and `KXMLBF5TOTAL`.
- PR3a fix3 keeps `KXMLBGAME` in the registry for status/reporting, but normal market-family discovery only probes the five non-full-game-winner families because full-game winner is already handled by the targeted resolver.
- Retired guessed prefixes are explicitly not active, and `KXMLBTEAMTOTAL` is not part of PR3a discovery.
- Market sync, market-family discovery, resolve preview, and open-position price refresh read from `KALSHI_MARKET_DATA_BASE_URL` by default. The default is the public Kalshi production market-data endpoint; `KALSHI_REST_BASE_URL` and `KALSHI_ENV` remain the demo/execution context.
- The PR3a hotfix makes discovery persist a completed or `partial_error` run even when candidate spread, total, or first-five probes return 404/no-match responses and no markets are found.
- PR3a fix3 changed discovery to a low-request flow: first batch exact scheduled-time ticker queries with `GET /markets?tickers=...&mve_filter=exclude`, then optionally try capped fallback time offsets, then use `event_ticker` filtering only as a secondary fallback for no-match game/family pairs.
- Market-data reads now have configurable throttling, 429 retry/backoff with `Retry-After` support, and a per-run 429 circuit breaker. Discovery summaries include request counts, rate-limit counts, retry counts, batching savings, and whether the circuit breaker stopped remaining probes.
- Discovery finalizes stale `running` runs older than 10 minutes and should not leave a run in `running` after the endpoint returns.
- Exact `KXMLBGAME` full-game winner resolver matches are an invariant: direct ticker, event ticker, team-code, and Eastern scheduled-time matches must stay `confirmed_for_paper` with confidence around `0.9700`, zero or near-zero time delta, and team match score `1.0`.
- `app.jobs.open_position_price_refresh` and `POST /v1/run/open-position-price-refresh` refresh open paper-trade marks from REST orderbook snapshots only. No WebSocket/live streaming is added.
- Dashboard summary position rows now include `game_status`, `game_status_display`, `current_price_updated_at`, and `current_price_updated_at_display`.
- Dashboard summary includes `paper_starting_balance` for frontend chart P/L modes.
- The frontend portfolio chart range controls and `NORM` / `P/L $` / `P/L %` controls now operate client-side on loaded snapshots.
- Open positions now show `GAME STATUS` and `LAST MARK TIME`.
- Model governance now counts clean resolved `KXMLBGAME` candidates using normalized family logic and repairs old `full_game_moneyline` rows to `full_game_winner` before counting.

Known PR 3a limitations:

- Discovery can prove that observed deterministic prefixes return markets, but PR3a does not claim settlement or line parsing is reliable enough to trade spreads, totals, or first-five markets.
- A valid discovery run may find zero markets. In that case `market_family_discovery_runs.raw_summary` should still show attempted event/market ticker counts, no-match counts, probe details, warnings, and errors so production validation can audit the no-match result.
- Production validation after PR3a fix2 found the registry directionally valid, with real `KXMLBSPREAD`, `KXMLBF5`, and `KXMLBF5TOTAL` markets discovered, but the old event-filter-heavy flow hit repeated Kalshi 429 responses and was too chatty for stable production use.
- Current price remains a REST last mark, not a real-time WebSocket mark.
- No cron jobs or dashboard admin controls are added.
- No live execution path exists.

Expected next PR scope:

- PR3b should review PR3a discovery reports and wire only proven reliable market families into mapping/candidate/settlement/model logic.
- A later operator PR can add scheduled runs after the one-shot jobs are validated in production.

## 15. PR3b Validated Market-Family Paper Wiring

PR 3b wires validated discovery outputs into paper-only candidate generation and settlement:

- Migration `0006_pr3b_family_wiring.py` adds market-family metadata to `kalshi_markets`, `market_mappings`, `model_candidates`, and `paper_trades`.
- `app.jobs.market_family_mapping_sync` and protected `POST /v1/sync/market-family-mappings?target_date=YYYY-MM-DD` consume the latest finalized market-family discovery run and normalize only supported rows.
- Supported paper families are `full_game_winner`, `full_game_spread`, `full_game_total`, `first_five_winner`, `first_five_spread`, and `first_five_total`.
- `KXMLBTEAMTOTAL`, guessed/retired prefixes, MVE/multivariate markets, and sportsbook concepts remain unsupported.
- Mapping sync marks rows `paper_supported` only when family, line/selection, inning scope, and settlement rule are clear. Otherwise rows remain `needs_review` or unsupported.
- Candidate generation no longer runs legacy heuristic market remapping first. It consumes explicit mappings, keeps existing resolver-confirmed mappings stable, and creates candidates for paper-supported families.
- Full-game winner candidates continue to use `heuristic_full_game_winner_v1` and remain training eligible.
- Newly wired non-winner families use `baseline_market_family_wire_v1`, feature tag `market_family_wire_v1_pre_full_model`, placeholder probability `0.50`, and `training_eligible=false` until a real model is built.
- Paper trades still require safe paper posture, non-live execution, an open/active market, an executable YES ask, and positive EV over the temporary threshold.
- Settlement supports full-game winner, full-game spread, full-game total, first-five winner including `TIE`, first-five spread, and first-five total. Spread/total pushes are handled explicitly.
- First-five settlement requires MLB linescore innings; missing linescore rows are skipped and left open rather than force-settled.
- `/v1/dashboard/summary?closed_date=YYYY-MM-DD` returns `closed_positions`, `closed_positions_date`, and `closed_positions_count`.
- The frontend dashboard shows a closed positions table below open positions with `PREVIOUS`, `TODAY`, `TOMORROW`, and compact date-picker controls.

PR 3b safety posture:

- Still no live orders.
- Still no production Kalshi credential requirement.
- Still no cron, admin page, monthly calendar, sportsbook integration, or team-total trading.
- New family trades are paper-only baseline plumbing and should not be treated as mature model training samples.

Expected next PR scope:

- Replace placeholder non-winner probabilities with measured family-specific models or calibrated heuristics.
- Add fee-aware EV after current Kalshi fee terms are verified.
- Add scheduler/automation only after the manual one-shot flow is production validated.

## 16. PR3c Full MLB Model And Automated Governance

PR 3c replaces the PR3b placeholder family probabilities with a paper-only mature MLB model and governance layer:

- Migration `0007_pr3c_model_governance.py` adds mature MLB feature snapshots, model prediction runs/outputs, governance events, and extra candidate/model feature metadata.
- Candidate generation now scores `full_game_winner`, `full_game_spread`, `full_game_total`, `first_five_winner`, `first_five_spread`, and `first_five_total` with `mature_mlb_run_distribution_v1`.
- Feature snapshots use `mature_mlb_features_v1` and record source status as available, partial, missing, or unavailable instead of filling gaps with fake data.
- The model uses a transparent run-distribution approach for full-game and first-five probabilities. It does not blend toward Kalshi market price and does not use sportsbook odds.
- Missing lineup, injury, weather, bullpen, defense/catcher, and other unavailable inputs are explicitly marked. Umpire data, team totals, sportsbook APIs, and MVE/multivariate markets remain out of scope.
- Paper trades are capped by slate, game, market family, open-position count, duplicate market ticker, and correlated game/family exposure before any trade row is created.
- Governance records resolved mature samples, reliability metrics, Brier/log-loss summaries, calibration status, and skipped reasons when sample thresholds are not met.
- New protected model endpoints expose governance status, feature coverage, prediction outputs, MLB feature sync, feature snapshot backfill, and training-eligibility repair.
- The dashboard model panel now shows active model, feature version, calibration status, training-eligible count, resolved mature samples, data quality, trade cap usage, and last governance status.

PR 3c safety posture:

- Still no live orders.
- Still no production Kalshi credential requirement.
- Still no cron, admin page, monthly calendar, sportsbook integration, team-total trading, or live execution path.
- `PAPER_REQUIRE_CALIBRATED_FOR_TRADE=false` by default so paper validation can continue while governance collects samples; production operators can turn it on later if they want stricter paper gating.

Expected next PR scope:

- PR3d should add the Railway cron/orchestration layer for the validated one-shot jobs, with stuck-run protection and operator monitoring.
- PR4 should add only a live-readiness shell and risk controls while keeping live execution off by default.

## 17. PR Change Log

Every future PR must update this section with:

- PR number or branch name.
- Plain-English summary.
- Any changes to scope, safety posture, architecture, model assumptions, deployment, or operations.
- Validation performed.

### PR 1 - Monorepo Foundation

- Created the initial FastAPI backend, Next.js dashboard shell, PostgreSQL-ready schema, setup docs, and CI scaffolding.
- Kept the system paper-trading only with live trading disabled and the execution kill switch enabled by default.
- Added empty dashboard responses and light-theme empty states without implementing Kalshi discovery, model scoring, or live execution.

### PR 2 - Data Layer And Dashboard

- Added MLB schedule sync, Kalshi market sync, auditable mapping, conservative paper candidate generation, and protected internal run endpoints.
- Added migration `0002_pr2_data_layer.py` for raw MLB payloads, Kalshi orderbook fields, mapping rationale, candidate scoring fields, and paper trade mark-to-market fields.
- Replaced the PR 1 dashboard shell with a light terminal-style dashboard that reads backend portfolio snapshots, positions, model status, and system status.
- Kept the safety posture paper-first: live trading disabled, execution kill switch enabled, no live order placement, and no production credential requirement.
- Validation performed: backend Ruff, backend pytest, frontend lint, frontend typecheck, and frontend production build.

### PR 2.5 - Targeted Kalshi MLB Resolver

- Replaced broad Kalshi market discovery as the primary path with an MLB-game-driven targeted resolver for the empirically observed `KXMLBGAME` full-game winner family.
- Added migration `0003_pr2_5_targeted_kalshi_resolver.py` for MLB abbreviations, raw Kalshi status, resolver strategy, and validation status.
- Added protected `GET /v1/kalshi/resolve-preview?date=YYYY-MM-DD` for production validation without database writes.
- Kept broad discovery as bounded diagnostics only via `KALSHI_ENABLE_BROAD_DISCOVERY=false` by default.
- Rejected multivariate Kalshi markets from normal MLB game mapping.
- Kept paper-first safety posture unchanged: no live orders, live trading disabled, kill switch enabled.
- Validation performed: backend Ruff and backend pytest.

### PR 3 - Paper Results, Portfolio, And Model Governance

- Added migration `0004_pr3_results_model.py` for paper settlement fields, readable contract labels, feature/model metadata, snapshot type, and settlement-to-paper-trade linkage.
- Added MLB results sync, paper settlement sync, balance snapshot, and model governance jobs/endpoints.
- Added hold-to-settlement P/L for supported `full_game_winner` paper trades and idempotent settlement records.
- Added paper portfolio accounting from `PAPER_STARTING_BALANCE`, realized P/L, open trade cost, and open mark value.
- Replaced placeholder `0.50` candidate probabilities with `heuristic_full_game_winner_v1`.
- Added feature snapshots with explicit missing-source markers and automated governance skips for insufficient samples.
- Updated dashboard summaries and frontend positions to show readable market labels while preserving raw Kalshi tickers.
- Changed resolve-preview semantics so structured partial no-match/error results return `ok=true` with warnings/partial errors.
- Kept paper-first safety posture unchanged: no live orders, live trading disabled, kill switch enabled.
- Validation performed: backend Ruff, backend pytest, Alembic head check, frontend lint, frontend typecheck, and frontend build.

### PR 3a - Market-Family Discovery And Operator Repairs

- Added migration `0005_pr3a_discovery.py` for discovery audit tables and paper-trade current mark timestamps.
- Added discovery-only market-family registry/reporting for full-game spread, full-game total, first-five winner, first-five spread, and first-five total.
- Added protected market-family discovery and open-position REST price-refresh run endpoints plus matching one-shot job modules.
- Fixed model governance resolved-sample counting by normalizing resolved `KXMLBGAME` candidates, including older `full_game_moneyline` rows.
- Added dashboard API fields for game status, last mark time, and paper starting balance.
- Made frontend portfolio chart range controls and `NORM` / `P/L $` / `P/L %` controls functional.
- Added `GAME STATUS` and `LAST MARK TIME` columns to open positions.
- Kept paper-first safety posture unchanged: no live orders, no live execution, no cron setup, and no new market families trade-enabled.
- Validation performed: backend focused tests during implementation; full PR validation recorded in the pull request.

### PR 3a Hotfix - Market-Family Discovery Failure Persistence

- Production validation found that `POST /v1/run/market-family-discovery?target_date=YYYY-MM-DD` could fail with an upstream error when spread, total, or first-five candidate probes returned expected Kalshi 404/no-match responses.
- The hotfix keeps PR3a discovery-only but treats 404 probe misses as structured `MARKET_FAMILY_PROBE_NO_MATCH` warnings, records non-404 upstream failures as run errors, and continues probing remaining families.
- `market_family_discovery_runs` now persists a completed or `partial_error` row even when `markets_found=0`; `raw_summary` includes `attempted_probe_count` and `probe_attempts` for auditability.
- `GET /v1/market-families/discovery?date=YYYY-MM-DD` should return the latest run instead of `run: null` after a zero-market discovery run.
- No schema migration, safety posture change, live execution, or spread/total/F5 trade enablement was added.
- Production validation steps: run the protected discovery POST, confirm structured JSON instead of a blank upstream error, confirm the protected report GET returns `run` not null, confirm the run row exists with probe attempts in `raw_summary`, confirm discovery item rows may be zero, and confirm `KXMLBGAME` full-game winner resolver behavior remains `confirmed_for_paper`.

### PR 3a Fix 2 - Deterministic Kalshi MLB Ticker Registry

- Replaced guessed PR3a market-family probes with the deterministic observed MLB registry: `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`, `KXMLBF5`, `KXMLBF5SPREAD`, and `KXMLBF5TOTAL`.
- Retired guessed legacy prefixes from active discovery and kept `KXMLBTEAMTOTAL` out of scope.
- Discovery now probes direct market lookup and `event_ticker` market lookup for each constructed ticker and records attempted ticker counts, no-match counts, examples, warnings, and errors.
- Added `KALSHI_MARKET_DATA_BASE_URL` for public market-data reads used by market sync, discovery, and open-position price refresh; demo/execution settings remain separate and paper-only.
- Report reads return the latest finalized discovery run, not stale `running` rows.
- No schema migration, safety posture change, live execution, or new family trade enablement was added.

### PR 3a Fix 3 - Stable Deterministic Kalshi Discovery

- Production validation after PR3a fix2 confirmed the deterministic prefix direction but produced `partial_error` runs due to repeated Kalshi 429 responses, especially from repeated `event_ticker` filters.
- Refactored market-family discovery to batch exact scheduled-time ticker probes first, use capped fallback time offsets only for no-match game/family pairs, and reserve `event_ticker` filtering as a secondary fallback.
- Added market-data throttle/retry/backoff settings and a discovery 429 circuit breaker: `KALSHI_MARKET_DATA_MIN_REQUEST_INTERVAL_MS`, `KALSHI_MARKET_DATA_MAX_RETRIES`, `KALSHI_MARKET_DATA_BACKOFF_BASE_MS`, `KALSHI_MARKET_DATA_BACKOFF_MAX_MS`, `KALSHI_DISCOVERY_ENABLE_FALLBACK_TIME_OFFSETS`, `KALSHI_DISCOVERY_MAX_FALLBACK_OFFSETS`, and `KALSHI_DISCOVERY_MAX_429_ERRORS`.
- Discovery summaries now report `request_count`, `requests_saved_by_batching`, `rate_limited_count`, `retries_attempted`, `stopped_due_to_rate_limit`, exact/fallback/event-filter attempt counts, and per-family no-match counts.
- Stale `market_family_discovery_runs` rows older than 10 minutes are finalized with `STALE_RUNNING_RUN_FINALIZED`; endpoint runs should return structured JSON and should not leave status `running`.
- `/v1/system/status` now exposes both `kalshi_market_data_source` and `kalshi_market_data_base_kind`, with the default public URL reported as `production_public_market_data`.
- Locked exact `KXMLBGAME` full-game winner resolver behavior so direct ticker matches for KC at TB and TEX at TOR remain `confirmed_for_paper` with confidence `0.9700`, zero time delta, and team match score `1.0`.
- No schema migration, safety posture change, live execution, sportsbook logic, team totals, admin page, calendar, or new family trade enablement was added.
- Validation performed: backend `tests/test_api.py` passed locally with 78 tests; final PR validation should also run Ruff, compileall, and diff checks.
- Expected next PR scope remains PR3b: review production discovery reports from this stable flow and wire only validated non-winner families into mapping, candidates, settlement, and model logic.

### PR 3b - Validated MLB Market-Family Paper Wiring

- Added migration `0006_pr3b_family_wiring.py` for market-family metadata on Kalshi markets, mappings, model candidates, and paper trades.
- Added `app.jobs.market_family_mapping_sync`, protected `POST /v1/sync/market-family-mappings`, and protected `GET /v1/market-families/mappings`.
- Wired validated discovery rows for full-game winner, full-game spread, full-game total, first-five winner, first-five spread, and first-five total into mapping, candidate generation, paper trades, settlement, and dashboard summaries.
- Kept team totals, MVE/multivariate markets, sportsbook data, guessed prefixes, retired prefixes, live execution, cron, admin pages, and monthly calendars out of scope.
- Tagged newly wired non-winner family candidates as `baseline_market_family_wire_v1`, `market_family_wire_v1_pre_full_model`, and `training_eligible=false`.
- Extended paper settlement for spread/total push handling and first-five winner/spread/total settlement with missing-linescore skips.
- Added `/v1/dashboard/summary?closed_date=YYYY-MM-DD` closed-position rows and a frontend closed positions table with previous/today/tomorrow/date controls.
- Kept paper-first safety posture unchanged: no live orders, live trading disabled, kill switch enabled, no production credential requirement.
- Validation performed: backend Ruff, backend pytest, backend compileall, frontend lint, frontend typecheck, frontend build, and `git diff --check`.

### PR 3c - Full MLB Model And Automated Governance

- Added migration `0007_pr3c_model_governance.py` for mature MLB feature snapshots, model prediction runs/outputs, governance events, and extra candidate/model feature metadata.
- Replaced PR3b placeholder non-winner probabilities with `mature_mlb_run_distribution_v1` across supported full-game and first-five winner/spread/total families.
- Added `mature_mlb_features_v1` snapshots with explicit source statuses and no fake missing lineup, weather, injury, umpire, sportsbook, team-total, or MVE data.
- Added strict paper trade caps by slate, game, market family, open-position count, duplicate market ticker, and correlated game/family exposure.
- Added governance status, feature coverage, prediction output, feature sync, feature snapshot backfill, and training-eligibility repair endpoints/jobs.
- Updated the dashboard model quality panel with active model, feature version, calibration status, training samples, data quality, cap usage, and governance state.
- Kept paper-first safety posture unchanged: no live orders, live trading disabled, kill switch enabled, no production credential requirement, no cron setup.
- Validation performed: backend Ruff, backend pytest, backend compileall, Alembic head check, frontend lint, frontend typecheck, frontend build, and `git diff --check`.

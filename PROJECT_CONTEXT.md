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

## 17. PR3c Hotfix - Date-Scoped Fee-Aware Trade Selection

Production validation after PR3c confirmed that feature sync, governance, and mature prediction generation work structurally, but found two production issues:

- `POST /v1/run/paper-candidate-engine?target_date=2026-06-27` could evaluate a future slate while reporting the current Eastern date in the prediction run.
- Too many predictions could become trade-eligible before caps because EV used a zero fee and line/correlation selection was not strict enough before slate caps.

The PR3c hotfix fixes this without enabling live trading:

- Candidate generation accepts an explicit `target_date`; if omitted, it defaults to today's Eastern date.
- Candidate generation evaluates only MLB games whose scheduled start falls on the evaluated Eastern target date.
- `ModelPredictionRun.target_date`, candidate `target_date`, cap accounting, response diagnostics, and prediction-output queries now use the same evaluated slate date.
- Added protected `GET /v1/model/predictions?date=YYYY-MM-DD`; `/v1/model/predictions/today` remains backward-compatible.
- Added migration `0008_pr3c_fee_date_scope.py` for candidate and prediction-output diagnostics: target date, probability edge, executable price source, price staleness, price status, gross EV, conservative fee estimate, and net EV.
- Paper EV now estimates conservative Kalshi fees with `KALSHI_TRADE_FEE_RATE * quantity * price * (1 - price)`, rounded upward by `KALSHI_FEE_ROUNDING_MODE`; this is paper-only until exact live fill fees are available.
- YES buys use executable YES asks first, then orderbook-implied YES asks from the highest NO bid. Last price is saved as market context but does not allow trading unless explicitly enabled.
- Zero, one-dollar, missing, stale, or non-executable prices save candidates for audit but block paper trades.
- Trade eligibility now requires clean mapping/settlement metadata, game not started, matching target date, fresh executable price, data quality, probability edge, fee estimate, fee-adjusted net EV, and push-safe status before line selection or caps.
- Line-selection now rejects correlated alternate lines before caps. Default policy keeps at most one trade per game/family and does not trade multiple first-five winner outcomes.
- Slate, game, and family caps are scoped to the evaluated target date. Global open-position cap remains global.
- Governance excludes stale/non-executable, missing-fee, after-start, and target-date-mismatched candidates from mature resolved sample counts.

New configuration:

- `KALSHI_TRADE_FEE_RATE=0.07`
- `KALSHI_FEE_ESTIMATE_MODE=conservative`
- `KALSHI_FEE_ROUNDING_MODE=centicent_or_cent_conservative`
- `KALSHI_ASSUME_TAKER=true`
- `PAPER_MAX_PRICE_STALENESS_SECONDS=900`
- `PAPER_ALLOW_LAST_PRICE_FALLBACK_FOR_TRADE=false`
- `PAPER_MAX_TRADES_PER_GAME_FAMILY=1`
- `PAPER_ALLOW_MULTIPLE_LINES_PER_GAME_FAMILY=false`
- `PAPER_ALLOW_MULTIPLE_F5_WINNER_OUTCOMES=false`

Known limitations:

- Fee modeling is conservative and configurable; it does not call a live order preview endpoint and does not require account credentials.
- No cron is added in this PR.
- No live order placement or live execution path is added.
- No sportsbook, team-total, umpire, admin-page, or calendar work is added.

Expected next PR scope:

- PR3d should focus on paper-ops orchestration/cron only after this target-date and fee-aware selection path validates in production.
- Continue using explicit target-date validation before enabling any scheduled candidate run.

## 18. PR Change Log

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

### PR3c Hotfix - Date-Scoped Fee-Aware Trade Selection

- Added explicit `target_date` support to the paper candidate engine and date-specific prediction reads.
- Added migration `0008_pr3c_fee_date_scope.py` for fee, edge, executable-price, stale-price, and target-date diagnostics.
- Replaced zero-fee EV decisions with conservative configurable Kalshi fee estimates and fee-adjusted net EV.
- Blocked stale, missing, zero, one-dollar, last-price-only, and otherwise non-executable prices from paper trading while still saving prediction/candidate rows for audit and learning.
- Added pre-cap line/correlation selection so caps are a final guard rather than the primary selector.
- Scoped slate/game/family caps to the evaluated target date while keeping open-position cap global.
- Updated docs and validation steps for `POST /v1/run/paper-candidate-engine?target_date=YYYY-MM-DD` and `GET /v1/model/predictions?date=YYYY-MM-DD`.
- Kept safety posture unchanged: paper trading only, live trading disabled, kill switch enabled, no live orders, no cron, no sportsbook/team-total/umpire/admin/calendar scope.

### PR3c Fix 2 - Feature-Complete MLB Model Governance

- Added migration `0009_pr3c_fix2_features.py` for additive feature cache tables: team daily/recent features, pitcher daily features, bullpen features, lineup snapshots, injury snapshots, weather snapshots, park factors, travel/schedule features, model parameter versions, model training datasets, and model threshold versions.
- Replaced `mature_mlb_features_v1` / `mature_mlb_run_distribution_v1` with `mature_mlb_features_v2` / `mature_mlb_run_distribution_v2` while keeping the system paper-only.
- Added MLB Stats API adapters for probable pitchers and starting lineups. The lineup parser uses MLB boxscore batting-order slots 100 through 900 as starters and excludes substitutes.
- Added static park, travel/rest, bullpen, pitcher, lineup, and weather feature modules. Optional network-backed feature fetches are off by default behind `FEATURE_SYNC_ENABLE_NETWORK_SOURCES=false`; Open-Meteo and provider API key settings are optional.
- Changed feature quality scoring from a free base score to module-weighted scoring with explicit `available`, `partial`, `missing`, and `unavailable` statuses, critical-module caps, and a `data_quality_reason`.
- Added active model parameter versions and bounded governance training: resolved samples create training datasets, challenger parameter/calibration offsets are fitted only above sample thresholds, and promotions require configured sample count, logloss improvement, and ECE gates.
- Added protected model/feature endpoints for feature detail, active parameters, training summary, and module-specific feature sync runs.
- Updated the dashboard model quality panel to show active parameter version, feature completeness, critical module warnings, lineup/starter/weather status, and governance status.
- Kept out of scope: live execution, cron/orchestration, sportsbook odds, team totals, umpire data, admin/calendar UI, and fake production data. PR3d should focus on scheduling/orchestration and monitoring only after this feature/governance stack is validated.
- Validation performed: backend Ruff, backend pytest, Alembic head check, frontend lint/typecheck/build, and `git diff --check`.

### PR3c Fix 3 - Real Public MLB Feature Ingestion

- Root cause: PR3c fix2 added schemas, endpoints, and feature snapshot composition, but `FEATURE_SYNC_ENABLE_NETWORK_SOURCES=false` by default meant deployed sync endpoints could return success-looking payloads while raw feature tables stayed empty or placeholder-only.
- Changed the default feature-source posture to `FEATURE_SYNC_ENABLE_NETWORK_SOURCES=true` for no-key public sources. Railway should set this explicitly; local/test runs can still disable it, and disabled network-backed module syncs now return `validation_status=skipped_network_disabled` with zero inserted/updated rows.
- Added a direct `MLBStatsClient` for public MLB Stats API schedule, live game feed, boxscore, and linescore reads. Feature sync hydrates the target slate and recent schedule history before writing raw feature rows.
- Upgraded public adapters: starting lineups parse boxscore batting-order slots 100 through 900 and exclude substitutes; probable starters prefer live boxscore starters and fall back to schedule `probablePitcher`; team offense and bullpen rows write conservative partial schedule-derived proxies; weather uses Open-Meteo hourly forecasts with Fahrenheit/mph units and stadium coordinates.
- Expanded the static park table to current MLB venues, including the Athletics' temporary Sutter Health Park context, and kept optional provider keys optional: `INJURY_PROVIDER_API_KEY`, `LINEUP_PROVIDER_API_KEY`, and `WEATHER_PROVIDER_API_KEY`.
- Added protected `GET /v1/model/sources/status` plus source/network visibility in `/v1/system/status` and dashboard model status. The diagnostics report public-source enablement, base URLs, optional-provider configuration booleans, pybaseball availability, and last successful/error state per raw feature table without exposing secrets.
- Mature feature snapshots now treat feature-only placeholder market context as missing, so it cannot inflate `data_quality`; candidate-generated snapshots still use real market/mapping context.
- Known limitations: no pybaseball dependency was added in this PR, so advanced Statcast-like offense/pitcher/bullpen fields remain partial/null unless available from existing public MLB payloads. Injury and catcher-defense modules remain missing/partial without optional providers. No team totals, umpire factors, sportsbook data, cron, admin page, calendar view, live execution, or live order placement were added.
- Validation target after deploy: source status shows network sources enabled; module sync responses include raw `tables_written`; `lineup_snapshots`, `weather_snapshots`, `pitcher_daily_features`, `team_daily_features`, `team_recent_features`, and `bullpen_daily_features` populate where public data exists; feature coverage/source statuses and dashboard critical warnings reflect real coverage; candidate generation still blocks paper trades below `PAPER_MIN_DATA_QUALITY`.
- Next expected PR scope: PR3d should add scheduling/orchestration and monitoring only after real feature ingestion validates in production.

### PR3c Fix 4 - Idempotent Public Feature Ingestion

- Root cause: enabling public network feature sources in production exposed non-idempotent schedule hydration. Repeated feature syncs could load duplicate MLB `gamePk` payloads into one session and trigger `uq_mlb_games_external_game_id`, causing module endpoints such as `/v1/sync/mlb-team-features` and `/v1/sync/mlb-pitcher-features` to return unhandled 500s.
- Schedule hydration now de-duplicates incoming MLB schedule payloads by `gamePk`, updates existing `MlbGame.external_game_id` rows instead of inserting duplicates, preserves cached live `raw_payload.liveData`, and uses row-level guarded flushes so one bad schedule row does not abort the whole feature sync.
- Feature sync responses now include hydration observability: `hydration_rows_seen`, `hydration_rows_upserted`, `hydration_duplicate_count`, `hydration_error_count`, `hydration_validation_status`, `hydration_skipped_reason`, and `refresh_schedule`.
- Added optional `refresh_schedule=true/false` to feature sync endpoints. By default, full combined feature syncs refresh schedule data, while module-specific syncs skip schedule hydration when target-date games already exist and hydrate safely when the slate is missing.
- Degraded ingestion errors are returned as structured JSON with `validation_status=degraded_with_errors`, `error_count`, `errors[]`, and `warnings[]` instead of escaping as source-ingestion 500s. Latest sync audit data is persisted in `mature_mlb_features_v2` snapshot JSON and surfaced through `GET /v1/model/sources/status`.
- Open-Meteo and MLB game-feed failures are recorded with source/table/game/error details and keep writing missing or partial rows where possible.
- `pybaseball` remains optional and is not added as a production dependency in this PR. `/v1/model/sources/status` reports `pybaseball_available` as an import diagnostic and `advanced_public_stats_status` as actual ingestion state; schedule-derived team/pitcher/bullpen rows remain partial rather than being labeled as complete advanced public stats, even if the package is installed but unused.
- Validation added for duplicate `gamePk` hydration, repeated team sync idempotency, degraded hydration response/status reporting, `refresh_schedule=false` behavior, pybaseball unavailable diagnostics, Open-Meteo failure handling, missing lineup handling, and scope guardrails excluding live execution, team totals, and umpire factors.
- Remaining limitations: advanced Statcast/Baseball Savant style ingestion is still unavailable unless a future PR adds a reliable dependency or direct no-key adapter. Injury and catcher-defense modules remain missing/partial without optional providers. No live execution, cron, sportsbook logic, team totals, umpire factors, admin page, or calendar view were added.
- Next expected PR scope remains PR3d only after production confirms repeated feature syncs return structured responses, raw rows insert/update, and source status captures degraded source errors.

### PR3c Fix 5 - Pybaseball Advanced Stats And Discovery Cooldowns

- Root cause: PR3c fix4 made public feature ingestion idempotent, but production still did not install `pybaseball`, so `/v1/model/sources/status` reported `pybaseball_available=false` and team/pitcher/bullpen rows stayed schedule-derived partial proxies. Market-family discovery also still allowed repeated Kalshi 429 partial errors, which made it unsafe to automate in PR3d.
- Added `pybaseball==2.2.7` to `apps/api/requirements.txt` so Railway installs the dependency from repo configuration. No manual local PowerShell package install is required.
- Added lazy adapter `app.services.pybaseball_client` with structured import/call errors and bounded broad-source functions for batting, pitching, recent batting, recent pitching, statcast range, and pitcher statcast range. The app can still boot and source status can still render if pybaseball import or source calls fail.
- Feature sync now fetches pybaseball season batting/pitching data once per run, builds in-memory team/pitcher indexes, writes `source=pybaseball_public_stats_v1` rows into existing `team_daily_features`, `pitcher_daily_features`, and `bullpen_daily_features`, and keeps existing `derived_homerun_v2` / MLB Stats rows as fallback. No schema migration was needed.
- Feature snapshot composition now prefers available pybaseball rows over partial derived rows for team daily/recent, pitcher, and bullpen modules. Data quality only rises when real available pybaseball rows exist; derived-only slates remain partial and can still be blocked by `PAPER_MIN_DATA_QUALITY`.
- `/v1/model/sources/status` now reports pybaseball availability, version, module path, import error, last successful pybaseball sync, last pybaseball error, attempted functions, DB cache status, row counts, and `advanced_stats_status`. Feature sync responses include `pybaseball_functions_attempted`, `pybaseball_rows_seen`, `pybaseball_rows_matched`, `pybaseball_error_count`, `advanced_available_count`, and `advanced_partial_count`.
- Market-family discovery now adds conservative automation safety settings: `KALSHI_DISCOVERY_MAX_BATCH_SIZE=20`, `KALSHI_DISCOVERY_REQUEST_SPACING_MS=750`, `KALSHI_DISCOVERY_MAX_429_ERRORS=3`, `KALSHI_DISCOVERY_COOLDOWN_SECONDS=300`, `KALSHI_DISCOVERY_USE_CACHE_FIRST=true`, and `KALSHI_DISCOVERY_SKIP_IF_RECENT_MINUTES=60`.
- `POST /v1/run/market-family-discovery?target_date=YYYY-MM-DD&force_refresh=false` now checks recent finalized discovery runs and active cooldowns before making network calls. Rate-limited runs persist `status=partial_rate_limited`, `cooldown_until`, retry/request counters, and cache policy metadata.
- Candidate generation remains cache-only for market data/mappings. It does not call heavy discovery; if no mappings exist for the target date, it reports `zero_trade_reason=no_candidates_missing_mappings` and a warning to run discovery/mapping first.
- Kept out of scope: live execution, live order placement, production trading credentials, sportsbook/DraftKings/FanDuel/Odds API logic, team totals, umpire factors, admin page, primary calendar view, cron/orchestration, and secrets changes.
- Validation target after deploy: `/v1/model/sources/status` should show `pybaseball_available=true` with no import error; team/pitcher feature sync should produce `advanced_available_count>0` where pybaseball rows match or expose exact pybaseball/player-mapping errors; `team_daily_features` and `pitcher_daily_features` should contain `source=pybaseball_public_stats_v1`; feature detail should prefer pybaseball modules; candidate generation should still block low-quality candidates; market discovery with `force_refresh=false` should reuse cache/cooldown and avoid repeated 429s.
- Remaining limitations: team recent advanced windows, true reliever workload, pitch mix, player Statcast details, catcher defense, injuries, and optional provider-backed modules remain partial/missing unless future PRs add reliable bounded source adapters. Bullpen pybaseball rows are season team pitching context, not true reliever-only workload.
- Next expected PR scope is PR3d: production paper-ops orchestration, Railway cron, job sequencing, job status table, stuck-run protection, and monitoring after fix5 validates in production.

### PR3c Fix 6 - Mature MLB Data Ingestion And Event-Based Kalshi Discovery

- Root cause: PR3c fix5 installed pybaseball but leaned too heavily on FanGraphs-backed pybaseball season functions for core model availability. Production can see HTTP 403 or other upstream failures from those endpoints, and those failures should not block no-key MLB model features. Discovery also still attempted guessed spread/total market tickers even though those families encode line/side details that must come from Kalshi event results, which wastes requests and can contribute to 429s.
- MLB Stats API is now the primary baseball data source for schedule, probable pitchers, team season stats, team game logs, pitcher season stats, pitcher game logs, stat splits, boxscore, linescore, and live feed payloads. This follows the older backend pattern of using MLB Stats schedule hydration, team/pitcher stat endpoints, game logs, stat splits, and boxscore payloads before derived fallbacks.
- Statcast/Savant data accessed through pybaseball is treated as secondary enrichment for contact quality. FanGraphs-backed pybaseball batting/pitching season calls remain optional diagnostics/enrichment. If those calls return 403 or another source error, feature sync should return structured degraded JSON and still write available MLB Stats API team/pitcher/recent/workload rows where Stats API data exists.
- Derived `derived_homerun_v2` rows remain partial fallback only. They must not overwrite richer `mlb_stats_api_primary_v1` rows, and snapshot composition should prefer the highest quality source available.
- Feature sync responses now expose additional primary/secondary counters such as MLB Stats API primary available/partial counts, probable starters seen, pitcher season/game-log availability, starter recent/workload availability, Statcast row counts, and player-mapping failures.
- Spread and total discovery no longer guesses exact market tickers. Full-game winner and first-five winner can still use direct market-ticker probes because the team selection is deterministic. Full-game spread, full-game total, first-five spread, and first-five total use `event_ticker` filtering so Kalshi returns the actual line/side market tickers.
- Market-family discovery keeps the PR3c fix5 cache-first and rate-limit behavior: cache hits avoid upstream calls, source errors are structured, and repeated 429s persist `partial_rate_limited` with cooldown metadata instead of leaving a failed or running audit.
- No schema migration, dependency, secret, live execution, cron, admin, calendar, team-total, sportsbook, or production credential change was added. `.env.example` did not need new variables for fix6.
- Validation added for MLB Stats API primary features surviving FanGraphs 403 and for event-ticker discovery of spread/total families without guessed spread/total market-ticker batches.
- Next expected PR scope remains PR3d: Railway cron/orchestration, job sequencing, stuck-run protection, and operator monitoring after this ingestion/discovery path is validated.

### PR3d - Paper Operations Orchestration, Epochs, Fixed-Risk Sizing, And WS Market Data

- Added migration `0010_pr3d_paper_ops.py` for active paper observation epochs, job run audits, WebSocket worker status, epoch links on paper trades/candidates/prediction runs/outputs/balance snapshots, candidate gate flags, and fixed-risk sizing fields.
- Existing pre-PR3d paper rows are assigned to archived epoch `pre_pr3d_validation` during migration. Main dashboard reads default to the active paper epoch only, so archived validation activity no longer appears in active open positions, closed positions, portfolio series, win rate, ROI, P/L, record, or candidate counts.
- Added protected `POST /v1/admin/paper-trading/reset-epoch`. Required confirmation is `RESET_PAPER_EPOCH`. The reset archives current/unassigned paper rows, marks old open paper trades inactive for dashboard purposes, creates a new active epoch, and writes a starting balance snapshot. The intended PR3d observation reset uses `starting_balance=500.00` and `new_epoch=pr3d_paper_observation_v1`.
- Added paper observation threshold `PAPER_OBSERVATION_MIN_DATA_QUALITY=0.55` while keeping `LIVE_MIN_DATA_QUALITY=0.60` and the older `PAPER_MIN_DATA_QUALITY=0.60` for backward compatibility. Paper candidate generation uses the observation threshold; live execution remains absent.
- Broadened paper observation caps to `PAPER_MAX_TRADES_PER_SLATE=30` and `PAPER_MAX_TRADES_PER_MARKET_FAMILY=15`, while preserving `PAPER_MAX_TRADES_PER_GAME_FAMILY=1`, `PAPER_ALLOW_MULTIPLE_LINES_PER_GAME_FAMILY=false`, and `PAPER_ALLOW_MULTIPLE_F5_WINNER_OUTCOMES=false` so correlated same-game/family lines are still blocked.
- Replaced default one-contract paper entries with fixed-risk paper sizing. Active epoch portfolio value is multiplied by `PAPER_RISK_PER_TRADE_PCT=0.025`; contracts are floored from estimated cost per contract and bounded by `PAPER_MIN_CONTRACTS` / `PAPER_MAX_CONTRACTS_PER_TRADE`. Candidate and trade rows store bankroll, risk, contracts, one-contract EV, sized EV, fee estimate, and cost estimate.
- Candidate generation now stores independent gate diagnostics for mapping, market open, game not started, executable price freshness, data quality, push risk, probability, gross EV, fee, edge, net EV, calibration, line selection, caps, open-position conflicts, and final eligibility. Run summaries include quality-only blockers, EV/edge counterfactuals, price/mapping/push/line/cap blockers, average/min/max data quality, and by-family/by-scope decision breakdowns.
- Added family/scope dashboard reporting for `full_game`, `first_five`, and supported families: full-game winner/spread/total and first-five winner/spread/total. Performance is active-epoch only.
- Added `job_runs` and cron-safe job runner `python -m app.jobs.runner --job ... --target-date today_et|yesterday_et|YYYY-MM-DD`. Jobs record steps, lock by job/date, skip overlapping runs, mark stale runs failed, and return structured failed/degraded output instead of blank failures.
- Added protected job endpoints: `/v1/jobs/run/daily-setup`, `/v1/jobs/run/candidate-sweep`, `/v1/jobs/run/price-refresh`, `/v1/jobs/run/settlement`, `/v1/jobs/run/governance`, and `/v1/jobs/run/full-paper-cycle`.
- Added paper-safe WebSocket market-data worker command `python -m app.workers.kalshi_ws_paper`. It is disabled by default with `WEBSOCKET_MARKET_DATA_ENABLED=false`, subscribes only to active-epoch open paper positions and active candidate watchlists when enabled, updates market/trade marks, records `/v1/ws/status`, and never places orders.
- Dashboard now shows the active observation epoch, starting balance, active-epoch portfolio metrics, family/scope performance, latest candidate diagnostics, job status, and WebSocket/REST fallback status. There is no frontend reset/admin page.
- Railway operations should use separate short-lived cron services for runner jobs and a separate long-running service for the optional WebSocket worker. Cron schedules are UTC and must be adjusted for EDT/EST. Main web service remains `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
- Safety posture is unchanged: no live order placement, no live execution path, no production Kalshi credentials required, no sportsbook/Odds API logic, no team totals, no umpire factors, no admin page, and no primary calendar view.
- Next expected PR scope after PR3d observation is a live-execution shell and risk-control design with explicit family/scope allowlists, still disabled by default until paper observation supports it.

### PR3d Hotfix 1 - Side-Aware Paper Candidates And Aggregate Risk Caps

- Root cause found in manual PR3d validation: candidate generation was YES-side only and spread displays could make a NO-on-favorite contract look like a fake plus-spread YES contract. Example: a displayed `LAA +2.5 YES` position did not exist in the Kalshi UI; the safe equivalent would be buying NO on the actual Seattle `-2.5` first-five contract.
- Candidate generation is now side-aware where observable prices exist. YES candidates use YES ask / implied YES ask / inverse best NO bid. NO candidates use NO ask / implied NO ask / inverse best YES bid. The engine must not price a NO candidate from a YES ask.
- Candidate and prediction-output JSON now record `contract_side`, side probability, actual YES/NO probabilities, price side/source, actual contract display, and normalized equivalent display. Dashboard positions show the actual contract as primary, the normalized equivalent as a muted line when available, and the raw ticker for auditability.
- Only one side of a ticker can become a paper trade in a sweep. Same-side open positions block duplicates, opposite-side open positions block new exposure, and simultaneous YES/NO eligibility on one ticker is rejected as `no_trade_conflicting_side_signals`.
- Spread paper trading defaults to disabled with `PAPER_SPREAD_TRADING_ENABLED=false`. Spread markets may still be discovered, mapped, scored, and displayed as diagnostics. First-five spreads return `no_trade_spread_trading_disabled` unless the env explicitly enables spread paper trading after parser validation. Full-game spreads require the separate `PAPER_FULL_GAME_SPREAD_TRADING_ENABLED=true` flag plus trusted PR3o.1 audit metadata; otherwise they return `no_trade_full_game_spread_trading_disabled` or a full-game spread audit rejection reason.
- Paper exposure defaults were tightened to `PAPER_MAX_TRADES_PER_SLATE=8`, `PAPER_MAX_TRADES_PER_MARKET_FAMILY=4`, and `PAPER_MAX_OPEN_POSITIONS=12`. Aggregate bankroll caps were added: daily new risk 20%, open risk 25%, family risk 10%, scope risk 15%, and sub-20c low-price bucket risk 8%.
- Fixed-risk sizing still starts from `PAPER_RISK_PER_TRADE_PCT`, but selected trades are reduced or rejected when aggregate risk remaining cannot support the full size. Rejection reasons include `no_trade_daily_risk_cap`, `no_trade_open_risk_cap`, `no_trade_family_risk_cap`, `no_trade_scope_risk_cap`, and `no_trade_low_price_bucket_risk_cap`.
- Current PR3d spread-heavy paper positions are contaminated validation data. After deploy, archive the active epoch as `pr3d_bad_spread_parser_validation` and create clean active epoch `pr3d_paper_observation_v2` with starting balance `500.00`.
- Reset payload remains protected API-only: `archive_open_positions=true`, `reset_dashboard_metrics=true`, and `confirmation=RESET_PAPER_EPOCH`. Archived rows must stay hidden from the main dashboard and active metrics.
- No live execution, cron setup, sportsbook/Odds API logic, team totals, umpire factors, production credentials, secrets changes, admin page, or frontend reset button were added.
- Validation target: default spread trading disabled; YES/NO side diagnostics present; NO prices sourced from NO-side quotes; no fake plus-spread YES labels; NO-side settlement remains inverted from the actual YES proposition; aggregate risk caps prevent excessive one-slate exposure; active dashboard starts clean after the v2 reset.

### PR3d Hotfix 2 - Time-Windowed Candidate Sweeps

- Added optional candidate-sweep window controls to the protected API and cron-safe runner: `min_time_to_start_minutes`, `max_time_to_start_minutes`, `sweep_label`, and `dry_run_candidates_only`.
- The production paper-observation sweep is intended to run every 30 minutes with a 45-180 minute rolling pregame window. Games outside that window are counted as too soon, too late, started, or wrong date, but normal windowed sweeps do not score them or create paper trades.
- Candidate-engine and job-run responses now include sweep diagnostics, including games in window, excluded counts, next eligible game, candidates in window, and paper trades created in the sweep. The dashboard model panel surfaces the latest sweep window and counts.
- Dry-run candidate sweeps save labeled diagnostics without opening paper trades, refreshing open-position marks, or writing balance snapshots.
- Risk caps remain active-epoch/global for the target date and are not reset per sweep. Price refresh remains unwindowed so all open paper positions continue to be marked.
- Safety posture is unchanged: no live order placement, no live execution path, no sportsbook/Odds API logic, no team totals, no umpire factors, and no production credential changes.

### PR3d Hotfix 3 - Spread Verification, Display Audit, And Game-Scope Correlation

- Added a spread verification service and protected spread-audit job that parse spread side, line, inning scope, settlement rule status, actual contract display, normalized equivalent display, and raw contract text from Kalshi raw fields. Ticker-only spread parsing is not enough to mark a row verified.
- Added `POST /v1/jobs/run/spread-audit?target_date=YYYY-MM-DD&min_time_to_start_minutes=45&max_time_to_start_minutes=180` and `python -m app.jobs.runner --job spread-audit ...`. The job updates mapping/market audit metadata only and does not create paper trades.
- First-five spread candidates can only trade when the parser is verified, settlement metadata is verified, all normal candidate gates pass, and `PAPER_SPREAD_TRADING_ENABLED=true`. Full-game spread candidates require `PAPER_FULL_GAME_SPREAD_TRADING_ENABLED=true` and cached PR3o.1 `trusted_audit_only` metadata that verifies selected team, line direction, YES/NO complement, settlement formula, and no-push or verified push behavior.
- Improved position/candidate display so the dashboard shows matchup first, actual Kalshi contract display, normalized equivalent display, display title/subtitle, raw ticker, and selected-position rationale. Totals NO displays under/over equivalents such as `UNDER 8 FULL GAME EQUIVALENT` rather than fake spread-style signed lines.
- Added same-game same-scope correlation selection with `PAPER_MAX_TRADES_PER_GAME_SCOPE=1` by default. First-five total plus first-five winner/tie on the same game is blocked by default; one first-five and one full-game exposure remain separate scopes.
- Candidate sweep summaries and dashboard risk panels now report the active epoch portfolio value used as the risk-limit basis and the max risk-at-sweep values.
- No live execution, live order placement, production credential changes, new cron service, schema migration, sportsbook/Odds API logic, team totals, or umpire factors were added. Keep spread paper trading and any new spread cron disabled until audit output is manually validated against the Kalshi UI.

### PR3e - Advanced Feature Completeness Diagnostics

- Added honest 17-module completeness summaries for the active `mature_mlb_features_v2` snapshot source. `/v1/model/features/coverage`, `/v1/model/features/detail`, and `/v1/model/sources/status` now expose `core_modules`, `completeness_summary`, and `module_completeness` so operators can see available, partial, missing, and unavailable module counts and reasons.
- The completeness layer is diagnostic-only. It does not lower `PAPER_OBSERVATION_MIN_DATA_QUALITY`, `PAPER_MIN_DATA_QUALITY`, or `LIVE_MIN_DATA_QUALITY`, and it does not change EV thresholds, risk caps, candidate sweep windows, cron schedules, settlement, calibration, market discovery, live execution, or spread-trading activation.
- Coverage/detail queries filter to the active feature version and exclude audit-only snapshot rows, so legacy rows and sync audit sentinels do not inflate mature-model completeness.
- Source diagnostics continue to distinguish MLB Stats API primary data, pybaseball/Statcast secondary enrichment, derived fallback rows, Open-Meteo/static weather and park data, and optional-provider gaps. Partial or missing modules stay partial/missing; no fake injury, catcher, pitch-mix, umpire, sportsbook, team-total, or production data is introduced.
- Safety posture remains unchanged: `PAPER_TRADING=true`, `LIVE_TRADING_ENABLED=false`, `EXECUTION_KILL_SWITCH=true`, `KALSHI_ENV=demo`, `WEBSOCKET_MARKET_DATA_ENABLED=false`, no production Kalshi credentials, no sportsbook/Odds API behavior, no team totals, no umpire factors, and no live order placement.
- No schema migration, dependency, environment-variable, secret, or frontend change was added.
- Remaining feature gaps are still explicit: injuries are optional-provider missing unless configured, catcher defense and pitch mix are not populated by this PR, true bullpen reliever workload remains limited, weather depends on Open-Meteo/network settings, and FanGraphs-backed pybaseball calls can degrade while MLB Stats API primary rows remain usable.

### PR3f - Cache-Only Candidate Sweeps

- Root cause: production candidate-sweep cron was running `sync_mlb_features(session, target, None, True)` every 30 minutes before candidate generation. That pulled full MLB feature sync, pybaseball/FanGraphs, Statcast/Savant, Open-Meteo, and mature snapshot writes into the repeating sweep path, which is too heavy for Railway cron and caused a roughly 303-second production failure on June 30, 2026.
- Candidate-sweep now skips full feature sync and reports `feature_sync_mode=cache_only`, `feature_sync_skipped=true`, and `cached_features` diagnostics. It reads cached `mature_mlb_features_v2` snapshots created by daily setup or explicit feature-sync endpoints.
- If target-date mature feature snapshots are missing, candidate-sweep completes as a successful no-trade/no-work run with `no_candidates_missing_feature_snapshots` and does not run market-family mapping sync or candidate generation. It may still run schedule sync, open-position price refresh, and a balance snapshot so paper marks stay current.
- Daily setup behavior is unchanged and remains the normal owner of heavy public-source feature ingestion. `sync_mlb_features` is still called by daily setup and explicit feature-sync endpoints.
- The 45-180 minute sweep window arguments, dry-run candidate mode, price-refresh-after-candidate ordering, job locks, stale-run handling, job status recording, and active-epoch scope are preserved.
- No trading logic, EV math, model probability, paper risk cap, same-game/scope cap, settlement, spread audit, market-family mapping semantics, WebSocket behavior, cron schedule, data-quality threshold, live execution, production credential, sportsbook/Odds API, team-total, umpire, or spread-activation change was added.
- Production validation after deploy: re-enable candidate-sweep cron, confirm the next run completes quickly, verify dashboard job status is succeeded or clean skipped, confirm the result includes cache-only feature diagnostics and no feature-sync step, confirm the 45-180 minute window is still respected, and confirm no live execution or safety flag changed.

### PR3g - Candidate-Stage Quality and EV Decomposition

- Candidate generation now preserves raw `mature_mlb_features_v2` quality separately from paper-observation candidate quality. The raw feature score remains auditable as `raw_feature_snapshot_data_quality`; the candidate-stage paper score is exposed as `paper_observation_data_quality`.
- Paper-observation quality uses an explicit candidate-stage profile without lowering `PAPER_OBSERVATION_MIN_DATA_QUALITY`. Real candidate-time market context is evaluated from mapping confidence, settlement support, trusted selection, market status, executable price freshness, side, and market family.
- Missing optional or structurally limited modules remain visible as missing/partial instead of being faked as available. The diagnostics report module status, role, contribution, penalty, top blockers, quality threshold, and quality block reasons.
- Candidate and prediction-output JSON now include EV decomposition: probability, executable price, gross EV, fee estimate, net EV, probability edge, and threshold pass/fail flags.
- Candidate-sweep summaries now include EV/edge pass counts, quality-blocked counts, deduped game/scope/family opportunity counts, top counterfactual candidates blocked by quality, and grouped average/max EV and edge by family, scope, and side. `/v1/dashboard/summary` surfaces the compact latest diagnostic payload.
- PR3g does not change live execution, cron schedules, WebSocket behavior, settlement, market discovery, probability math, EV/edge thresholds, risk caps, same-game/scope caps, spread activation, or the PR3f cache-only sweep behavior. Full-game spread remains blocked by default unless a later dedicated PR, now PR3q, enables the separate trusted-audit paper gate.

### PR3h - Probable Starter Hydration and Cache Freshness

- Root cause: the lightweight MLB schedule sync used by candidate-sweep requested only `team,linescore` and replaced `MlbGame.raw_payload`, which could erase `probablePitcher` data hydrated by daily setup before the pregame window.
- Schedule/result sync now requests `hydrate=probablePitcher(note),team,venue,linescore` and preserves cached live feed data when updating games.
- Added a bounded official-MLB starter refresh path. It reads target-date schedule probable pitchers, supplements with per-game MLB live feed `gameData.probablePitchers` and boxscore pitcher data, stores per-side starter metadata in existing `MlbGame.raw_payload`, and never fabricates a starter when official sources are missing. When multiple official identities are present, live feed/boxscore starter data takes priority over a stale schedule probable.
- Starter refresh writes only MLB Stats API/derived pitcher cache rows and refreshes mature feature snapshots for the target slate. It does not call pybaseball, FanGraphs, Statcast/Savant, Open-Meteo, full `sync_mlb_features`, sportsbook APIs, team totals, or umpire logic.
- Candidate-sweep remains cache-only for heavy features. It runs the lightweight starter refresh only after target-date mature feature snapshots already exist; if snapshots are missing, it still exits cleanly with `no_candidates_missing_feature_snapshots`.
- Added protected diagnostics: `POST /v1/sync/mlb-starters?target_date=today_et` and `GET /v1/model/starter-status?date=YYYY-MM-DD`. Dashboard model status also includes compact `starter_hydration` counts.
- No schema migration, threshold change, EV/model/risk-cap change, live execution change, WebSocket change, cron schedule change, full-game spread activation, dependency, secret, or environment-variable change was added.

### PR3i - Candidate Decision Length Hotfix

- Root cause: production non-dry candidate-sweep reached the post-eligibility same-game/scope correlation guard and assigned `no_trade_same_game_scope_correlation_not_best`, but `model_candidates.decision` was still `String(40)`, causing PostgreSQL `StringDataRightTruncation`.
- Widened `ModelCandidate.decision` to the same 120-character decision/reason capacity already used by prediction outputs and added migration `0011_pr3i_decision_length.py`.
- Added tests that scan candidate-engine decision strings against the persisted schema length and exercise the same-game/scope correlation rejection path through flush/commit without creating paper trades.
- Validation commands: `apps/api/.venv/Scripts/python.exe -m ruff check .`, `apps/api/.venv/Scripts/python.exe -m compileall app`, and `apps/api/.venv/Scripts/python.exe -m pytest`.
- No trading logic, EV/model threshold, paper risk-cap, candidate ranking, starter hydration, market discovery, WebSocket, cron schedule, live execution, credential, full-game spread activation, sportsbook, team-total, or umpire change was added.

### PR3k - Paper Selection, Sizing, and Dashboard Controls

- First-five winner `TIE` markets remain discoverable and scorable for diagnostics, but paper trading is disabled with `no_trade_f5_tie_disabled`.
- Tail-price controls now block sub-10c paper entries with `no_trade_price_below_floor` and apply stricter thresholds for 10c-under-20c contracts: `PAPER_LOW_PRICE_MIN_NET_EV=0.08`, `PAPER_LOW_PRICE_MIN_PROB_EDGE=0.05`, `PAPER_LOW_PRICE_MAX_TRADES_PER_SLATE=2`, and `PAPER_LOW_PRICE_MAX_TRADES_PER_SWEEP=1`.
- Candidate sweeps keep the daily cap, but now also enforce `PAPER_MAX_NEW_TRADES_PER_SWEEP=3`, early-day reservation with `PAPER_MAX_NEW_TRADES_BEFORE_3PM_ET=4` and `PAPER_RESERVE_TRADES_AFTER_3PM_ET=2`, optional same-side concentration via `PAPER_MAX_SAME_SIDE_TRADES_PER_SLATE=6`, and post-cap minimum size with `PAPER_MIN_POST_CAP_CONTRACTS=5` / `PAPER_MIN_POST_CAP_NOTIONAL=2.00`.
- Daily, low-price, and side cap accounting counts same-day paper trades in the active epoch even after settlement, so early resolved trades do not reopen same-day slots.
- Dashboard position summaries now expose side, entry notional, fee-aware entry total cost, current/exit value, fee paid/estimated fee, mark timestamp, and realized P/L. The frontend keeps compact tables and removes duplicate middle market description text.
- PR3k does not change candidate-sweep timing, cache-only feature behavior, EV/probability math, settlement, market discovery, WebSocket, live execution, credentials, sportsbook/Odds API logic, team totals, umpire factors, defense modules, full-game spread activation, or production cron schedules.

### PR3l - Source Reliability and Statcast Fallbacks

- Added a machine-readable source inventory to `/v1/model/sources/status` so operators can distinguish MLB Stats API, Kalshi public market data, Open-Meteo, static HOMERUN reference data, derived HOMERUN features, pybaseball/FanGraphs, Statcast/Savant, and optional provider gaps.
- Statcast/Savant remains secondary cached enrichment, not a critical-path trading source. Full feature sync keeps same-date last-good Statcast contact-quality fields when a later Statcast/Savant fetch is empty or fails, and source health reports cached/stale fallback state using `STATCAST_CACHE_MAX_STALE_HOURS`.
- FanGraphs-backed `pybaseball` batting and pitching calls remain optional enrichment. HTTP 403 and other pybaseball failures are classified in source health, preserve last-good cached advanced rows, and do not block MLB Stats API primary rows or candidate-sweep stability.
- Added cache-age settings: `ADVANCED_PUBLIC_STATS_MAX_STALE_HOURS=72` and `STATCAST_CACHE_MAX_STALE_HOURS=48`. Stale rows stay available for diagnostics but are labeled as cached/stale source health instead of being treated as fresh public ingestion.
- Candidate-sweep remains cache-only. It still must not call full `sync_mlb_features`, pybaseball, FanGraphs, Statcast/Savant, Open-Meteo, sportsbook APIs, team totals, or umpire logic.
- No schema migration, frontend redesign, live execution, credential change, sportsbook data, defense module, pregame context refresh, full-game spread paper enablement, model math change, threshold change, risk-cap change, or cron schedule change was added.

### PR3m - Official Pregame Context Refresh and Feature Transparency

- Added protected `POST /v1/sync/mlb-pregame-context?target_date=YYYY-MM-DD|today_et|yesterday_et`. It refreshes target-date starters, official lineups, MLB Stats API pitcher game-log cache rows, and existing mature feature snapshots from official MLB Stats API schedule/feed/boxscore data only.
- Candidate-sweep remains cache-only for heavy features and now includes a bounded `pregame_context_refresh` step. Sweep results expose `feature_sync_mode=cache_only`, `feature_sync_skipped=true`, `heavy_feature_sync_skipped=true`, and the pregame context summary; no `sync_mlb_features` step is run.
- The pregame refresh builds on PR3h starter hydration. It preserves the starter summary for existing job diagnostics while adding lineup counts and `lineup_missing_reasons`.
- Official lineups are marked available only when the MLB feed has nine starting batting-order slots. Partial posted lineups are `partial` with `PARTIAL_LINEUP_POSTED`; empty pregame feeds are `missing` with `LINEUP_NOT_POSTED_YET`; feed failures are `LIVE_FEED_UNAVAILABLE`; live/final feeds without lineup rows are `LIVE_FEED_LINEUP_EMPTY`.
- Feature coverage/detail reasons now describe official lineup states and optional-provider gaps more explicitly, instead of using one generic cached-lineup message.
- Daily setup remains the owner of heavy public-source feature ingestion. PR3m does not call pybaseball, FanGraphs, Statcast/Savant, Open-Meteo, sportsbook APIs, team totals, umpire logic, full feature sync, market discovery, WebSocket, settlement, risk caps, model math, thresholds, live execution, production credentials, or cron schedule changes from candidate-sweep.
- No schema migration, dependency, environment-variable, secret, frontend, live order, or production credential change was added.

### PR3m.1 - Dashboard Observation Cutover

- Default `/v1/dashboard/summary` results now apply a dashboard-only observation cutoff of midnight ET on `2026-07-02`. Pre-cutoff active-epoch paper trades and legacy positions stay in the database, but they are excluded from default open positions, closed positions, portfolio value, P/L, ROI, record, family/scope performance, and dashboard candidate counts.
- Historical/audit access is preserved with `include_pre_observation=true`, which can be combined with existing `closed_date`, `epoch_key`, and `include_archived` parameters. The summary includes `observation_filter` metadata with the cutoff date, display time, excluded counts, history parameter, and reason.
- The frontend shows a compact note that default dashboard views exclude pre-Jul 2 validation rows. It does not add reset/admin controls and does not delete, archive, close, or mutate any paper rows.
- PR3m.1 does not change candidate generation, settlement, source sync, pregame context refresh, model math, risk caps, cron schedules, WebSocket behavior, live execution, spread activation, sportsbook/team-total/umpire logic, secrets, or migrations.

### PR3n - Baseline Defense Features

- Added conservative baseline defense context to the existing `defense_catcher` mature feature module. MLB Stats API team fielding game logs now populate `defense_season` on team daily rows and `defense_recent` on team recent rows using basic official fields such as errors, assists, putouts, chances, fielding percentage, double plays, passed balls, wild pitches, and stolen-base/caught-stealing fields when present.
- Official catcher inference remains lineup-based. When the official MLB lineup has a starting catcher, the feature payload records the catcher id/name from that lineup; when the lineup is not posted or the feed is unavailable, the reason is explicit. Advanced catcher framing/blocking/throwing metrics remain `not_configured`/`unavailable`.
- Umpire factors remain explicitly excluded and are not treated as a missing required model input.
- `/v1/model/features/coverage`, `/v1/model/features/detail`, and `/v1/model/sources/status` now distinguish baseline fielding, recent defense, catcher-from-lineup, advanced catcher metric gaps, and umpire exclusion. Candidate diagnostics surface the `defense_catcher` module contribution/penalty through the existing quality decomposition.
- PR3n does not change model probability formulas, EV math, trade caps, risk thresholds, settlement, market discovery, WebSocket behavior, live execution, spread activation, sportsbook/team-total logic, cron schedules, secrets, or schemas. Candidate-sweep remains heavy-source cache-only; daily setup or explicit feature sync owns MLB fielding ingestion.

### PR3n.1 - First-Five Lifecycle Settlement

- First-five winner, spread, and total paper trades now settle as soon as the official MLB linescore contains complete home and away runs for innings 1-5. They no longer wait for the full game to reach final status when the first-five outcome is already official.
- Full-game winner, spread, and total paper settlement timing is unchanged and still waits for the full game to be final or void.
- Incomplete first-five linescores leave trades open with explicit skip diagnostics: `first_five_not_complete` while the game is still open, or `missing_f5_linescore` when a final/closed game lacks usable first-five inning data.
- Open-position price refresh now avoids overwriting first-five marks with misleading closed/illiquid Kalshi prices. If the first-five outcome is already complete, settlement owns the final mark; if the first-five linescore is incomplete and the market is closed, the existing paper mark is preserved.
- Dashboard behavior is unchanged except that first-five trades should move from open to closed earlier once settlement runs. No schema migration, frontend change, candidate-generation change, cron change, model/EV/risk-cap change, market-discovery change, live execution, credential, sportsbook/team-total/umpire, WebSocket, or spread-activation change was added.
- Follow-up PR3o remains separate for any full-game spread audit or activation work.

### PR3o - Full-Game Spread Audit Only

- The protected `spread-audit` job is now a full-game spread audit-only diagnostic. It no longer runs market-family mapping sync as part of the job and the audit service itself does not write paper trades, settlements, candidates, market rows, or mapping metadata.
- Audit output classifies full-game spread rows with stable statuses and reason codes, including `trusted_audit_only`, `needs_review`, `missing_line`, `ambiguous_team_selection`, `ambiguous_yes_no_semantics`, `ambiguous_line_direction`, `settlement_text_unverified`, and `push_behavior_uncertain`.
- Per-market audit rows expose raw Kalshi title/subtitle/rules/YES/NO text, mapped MLB game, selected team, line sign/direction, YES interpretation, NO complement/equivalent interpretation, push possibility, push condition, and read-only settlement preview for final games.
- `trusted_audit_only` means the parsed team, line, YES/NO complement, and push/settlement evidence are coherent enough for the later PR3q paper gate when the separate full-game spread flag is enabled. Rows with missing or ambiguous evidence remain `needs_review`/unsafe-style statuses and must not be trusted.
- PR3o itself did not enable full-game spread paper trades. PR3q adds a separate disabled-by-default `PAPER_FULL_GAME_SPREAD_TRADING_ENABLED` flag that still requires PR3o.1 trusted audit metadata.
- No live execution, credential, WebSocket, cron cadence, model probability, EV, risk-cap, source-sync, dashboard observation cutoff, defense, first-five lifecycle settlement, sportsbook/team-total/umpire, schema, or frontend change was added.
- Full-game spread paper enablement remains a separate future PR, and only if PR3o audit evidence passes manual validation.

### PR3o.1 - Full-Game Spread Audit Normalization

- Full-game spread audit now treats Kalshi rules text as the primary source of truth. A rule such as `Pittsburgh wins by more than 1.5 runs` normalizes the YES contract to `PIT -1.5`, not `PIT +1.5`.
- Audit rows now expose rules threshold, raw threshold, display spread line, settlement formula, ticker suffix line, NO text source, NO complement source, and complement confidence so operators can distinguish rules-primary evidence from subtitle-only support.
- Duplicated YES/NO subtitles can still be trusted when the rules text verifies the YES condition and the Kalshi market is confirmed binary; contradictory explicit NO text is classified unsafe. Supporting subtitles that conflict with rules keep the row out of trusted audit status.
- Half-run spreads are treated as no-push markets. Integer spreads remain untrusted unless push/void/refund behavior is verified from rules text.
- Candidate generation still blocks full-game spread rows by default with `no_trade_full_game_spread_trading_disabled`; the broad spread flag continues to apply only to first-five spread diagnostics. PR3o.1 does not enable full-game spread paper trades by itself.
- No live execution, credential, WebSocket, cron cadence, model probability, EV, risk-cap, source-sync, dashboard observation cutoff, defense, first-five lifecycle settlement, sportsbook/team-total/umpire, schema, migration, or frontend change was added.

### PR3p - Clean Governance Training Autonomy

- Governance now has an explicit clean training cutoff, `MODEL_GOVERNANCE_CLEAN_START_AT`, defaulting to `2026-07-02T00:00:00-04:00`.
- `run_model_governance` still scopes to the active paper epoch, but it trains, calibrates, creates challengers, records threshold versions, and promotes parameters only from clean mature resolved candidates whose target date and evaluated timestamp are at or after the cutoff.
- `/v1/model/governance/status` and dashboard model status distinguish raw resolved mature samples from clean resolved mature samples, report pre-clean exclusion counts, and surface ignored legacy/pre-clean training, calibration, and threshold artifacts.
- Existing pre-clean artifacts remain in the database for audit/history, but artifacts without current clean-policy metadata are not treated as current governance-ready state.
- A governance parameter registry reports currently governed autonomous offsets/version promotion, future-governable coefficients/threshold policy, and intentionally manual safety controls. It is diagnostic metadata only.
- No live execution, credential, WebSocket, cron cadence, model formula, EV threshold, risk-cap, source-sync, dashboard observation cutoff, settlement, market discovery, spread activation, sportsbook/team-total/umpire, schema, migration, or frontend change was added.

### PR3p.1 - Dashboard Payload Memory Hygiene

- Root cause hypothesis: `/v1/dashboard/summary` was serializing full stored job results, source-health details, governance registry lists, and candidate-level diagnostic arrays into the default dashboard payload. After PR3p diagnostics grew, that made the production summary endpoint slow and memory-heavy even though the frontend only needs compact operator status.
- Dashboard summaries are compact by default. Job status now returns scalar status/count summaries instead of full `JobRun.result` blobs; candidate diagnostics return aggregate counts without candidate IDs/counterfactual lists; source status is reduced to source-health counts and latest sync summary; and governance registry defaults to counts instead of full registry lists.
- Opt-in debug query flags are available on `/v1/dashboard/summary`: `include_diagnostics`, `include_job_results`, `include_source_details`, `include_governance_details`, `include_spread_audit_details`, and `include_candidate_diagnostics`. Even debug mode caps samples and omits raw payload/features/rationale blobs from the dashboard response.
- `/v1/model/governance/status` now defaults to the compact registry and accepts `include_details=true` for the full protected diagnostic registry.
- Lightweight endpoint metrics were added for dashboard summary and protected model diagnostics: duration, approximate response size, RSS before/after when available, and non-secret flag values.
- No candidate generation, cron schedule, model math, settlement, risk-cap, WebSocket, market-discovery, live execution, credentials, environment-variable, schema, or trading behavior changed.

### PR3p.2 - Governance and Dashboard Query Materialization Hotfix

- Root cause: production validation showed compact response bodies but `/v1/model/governance/status` and `/v1/dashboard/summary` still took roughly 100 seconds and triggered Railway memory pressure. The remaining problem was backend query/materialization work, especially governance status constructing full candidate datasets and dashboard summary loading full candidate/feature ORM rows before compacting.
- Governance status now uses SQL aggregate count helpers for raw mature resolved samples, clean mature resolved samples, and pre-clean exclusion counts. The full `_resolved_mature_candidates` row loader remains reserved for explicit `run_model_governance` training jobs.
- Dashboard summary now reuses compact governance status, builds decision breakdowns with grouped SQL counts, reads only `source_statuses`/`data_quality` for feature completeness, and avoids loading `ModelCandidate.features` / `scoring_rationale` JSON on compact position/count paths.
- Added non-destructive status-query indexes for mature candidate counts, dashboard decision grouping, feature snapshot date/source status lookup, balance snapshot epoch/time reads, and latest job status by epoch/job/time.
- Endpoint metrics now also cover `/v1/model/parameters/active`; logs remain compact and do not include secrets or full payloads.
- No candidate generation, cron schedule, governance promotion logic, model probability math, EV threshold, risk cap, settlement, source ingestion, market discovery, WebSocket, live execution, credentials, environment-variable, sportsbook/team-total/umpire, spread activation, or frontend behavior changed.
- Production validation target: after deploy, `/v1/model/governance/status` and default `/v1/dashboard/summary` should return in under 5 seconds, stay compact, and avoid Railway memory staircase/OOM under repeated dashboard polling.

### PR3q - Full-Game Spread Paper Enablement Behind Trusted Audit Gate

- Added `PAPER_FULL_GAME_SPREAD_TRADING_ENABLED=false` as a separate full-game spread paper flag. The existing `PAPER_SPREAD_TRADING_ENABLED` broad flag still controls first-five spread diagnostics and does not enable full-game spread paper trades.
- Full-game spread candidates can only become paper trades when the new flag is true and the cached PR3o.1 spread verification is `trusted_audit_only` with full-game scope, verified selected team, verified line direction, YES/NO complement safety, settlement formula, threshold, and no-push or verified push behavior. Missing, parse-error, unsafe, needs-review, ambiguous, or otherwise untrusted audit rows remain no-trade with explicit full-game spread audit reasons and are excluded from training.
- Candidate sweep does not run the spread-audit job or any heavy source ingestion. It reads the existing mapping/market audit metadata from prior mapping sync or spread-audit validation, stores only compact audit evidence in candidate diagnostics, and preserves the cache-only PR3f/PR3m sweep posture.
- Full-game spread paper settlement now revalidates trusted audit metadata before resolving. Trusted rows settle from the audit formula (`selected_team_runs - opponent_runs > threshold`), with NO treated as the verified complement. Rows without trusted metadata stay open with explicit spread audit skip reasons instead of falling back to generic spread math.
- Dashboard summaries expose the full-game spread enablement flag, the audit gate requirement, latest compact spread-audit counts, blocked decision counts, and compact spread audit rationale on position rows without loading raw spread-audit payloads by default.
- No schema migration, cron schedule change, market discovery change, model probability/EV/risk-cap change, source-sync change, WebSocket change, live execution, credential, sportsbook/team-total/umpire, or frontend redesign was added. Safety posture remains paper-only, demo Kalshi environment, live trading disabled, and kill switch on.

### PR3r - Governance Memory and Portfolio Time-Series Fidelity

- Root cause: PR3p.2 made governance status and dashboard status compact, but the explicit governance job still loaded full `ModelCandidate` ORM rows before fitting, which could materialize large JSON payloads when operators ran governance. The dashboard portfolio series also used only the newest balance snapshots, which could flatten or omit active-epoch intraday bankroll moves.
- `run_model_governance` now trains from scalar clean sample rows containing only candidate id, timestamps, probability, market family/time bucket, outcome, and linked game start time. It preserves active-epoch scoping, clean cutoff filtering, target-date/game-date matching, chronological 70/30 split, calibration offset fitting, challenger creation, threshold records, and promotion guardrails.
- Governance results now include compact `governance_phase_metrics` with phase durations and RSS values when the runtime supports RSS. Logs remain compact and do not include raw candidate payloads, features, scoring rationale, secrets, or market data.
- Balance snapshot creation now coalesces duplicate no-change snapshots by returning the latest existing row when cash and portfolio value are unchanged. Candidate generation, price refresh, settlement, and manual balance refresh still create snapshots when cash or portfolio value changes.
- `/v1/dashboard/summary` now builds the portfolio chart series from actual active-epoch balance snapshots using a bounded 500-point compactor that preserves first/latest points and intraday high/low extrema. The response exposes `portfolio_series_source`, `portfolio_series_point_count`, `portfolio_series_truncated`, and `portfolio_series_preserves_intraday_fluctuations=true`.
- No schema migration, live execution, cron schedule, candidate-generation rule, model formula, promotion guardrail, EV threshold, risk cap, settlement outcome logic, market discovery, WebSocket behavior, source-ingestion behavior, credential, environment-variable, sportsbook/team-total/umpire, spread activation, or frontend redesign changed.

### PR3r.1 - Active-Epoch Portfolio Series Source

- Root cause: PR3r built a compact snapshot series but the default dashboard still replaced it with the old two-point `observation_filtered_portfolio_totals` series whenever the observation cutoff excluded pre-Jul 2 rows. Production could therefore have fresh balance snapshots while the chart stayed sparse and flat.
- `/v1/dashboard/summary` now prefers post-cutoff active-epoch `BalanceSnapshot` rows whenever they are usable on the same clean basis as the current dashboard totals. The old observation-filtered totals series is used only when no usable active-epoch snapshot rows are available or when excluded pre-cutoff trades are still open/settling/updating after the cutoff. Fallback responses set `portfolio_series_fallback_reason` to either `no_usable_active_epoch_balance_snapshots` or `pre_observation_trades_can_affect_snapshot_series`.
- When the default observation filter excludes only constant pre-cutoff paper history, snapshot-derived chart points are shifted onto the same clean filtered basis as `cash_balance` and `portfolio_value`, so the frontend does not mix raw full-epoch snapshots with clean current totals.
- Portfolio points remain compact: timestamp, value, cash balance, snapshot id, source, and snapshot type only. No raw payloads, candidate ids, full positions, market metadata, training data, or audit rows are added.
- The response also exposes `portfolio_series_active_epoch_id`, `portfolio_series_started_at`, and `portfolio_series_ended_at`. Duplicate no-change snapshot points are coalesced, while first/latest points and intraday high/low reversals remain preserved under the 500-point cap.
- No governance, model, candidate-generation, EV threshold, risk cap, settlement outcome, price-refresh pricing, spread audit, live execution, cron, source-ingestion, credential, environment-variable, sportsbook/team-total/umpire, or WebSocket behavior changed.

### PR3s - Exposure Taxonomy and Kalshi Ladder Line Classification

- Candidate generation now records compact, scalar exposure metadata for each candidate and paper trade: economic exposure label/key, family, scope, direction, team, line, contract-mechanics label, concept cluster key, same-game concept cluster key, taxonomy version, and Kalshi ladder line-classification fields.
- Economic exposure labels distinguish contract mechanics from the modeled exposure. For example, `NO` on an `OVER 8.5` total is labeled as an `UNDER 8.5` exposure, while the original `NO ON OVER 8.5` mechanics label remains available for audit.
- Concept clusters group related same-game concepts without changing risk caps or selection logic. Full-game and first-five total unders share the `total_under` concept while scope remains explicit in separate metadata.
- Line classification is Kalshi-ladder-only and bounded to the current candidate set for the same game/family/scope. Lines are marked `central`, `near_alternate`, `deep_alternate`, `tail`, `unclassified`, or `not_applicable` using current mapped Kalshi lines only; it does not use sportsbook odds, consensus lines, or historical market scans.
- Dashboard position summaries now prefer the economic exposure label for the primary display and include the compact taxonomy payload in selected-position rationale. Raw payloads, full features, scoring rationale blobs, and job-result blobs are not added to default dashboard responses.
- PR3s is display and diagnostics metadata only. It does not change model probability math, EV thresholds, candidate eligibility, trade caps, risk sizing, spread audit gates, settlement formulas/outcomes, governance promotion, cron schedules, feature/source ingestion, WebSocket behavior, live execution, credentials, environment variables, sportsbook/team-total/umpire scope, or portfolio series behavior.

### PR3s.1 - Candidate-Level Exposure Taxonomy Population Fix

- Root cause: PR3s production validation on the known nonzero `2026-07-04` dry-run scored 924 candidates and reported PR3s version/count metadata, but compact candidate-level fields such as `economic_exposure_label` were not exposed by `/v1/model/predictions`.
- Newly scored candidates now expose compact PR3s scalar fields through prediction rows, and candidate-sweep summaries include bounded non-null field counts derived from the evaluated `ModelCandidate` rows. This proves candidate-level population without adding raw payloads, full features, scoring rationale blobs, or unbounded diagnostic arrays.
- Persistence remains on the nullable PR3s candidate/trade columns added by PR3s. PaperTrade propagation continues to copy compact taxonomy fields from the selected candidate when paper trades are created.
- Historical pre-PR3s rows can still have null taxonomy fields unless a future bounded backfill is explicitly added.
- Safety posture and selector behavior are unchanged: no model probability math, EV threshold, data-quality threshold, risk-cap, settlement, governance, cron, WebSocket, market-discovery, source-ingestion, live-execution, credential, sportsbook/team-total/umpire, or portfolio-series change was added.

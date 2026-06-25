# HOMERUN Project Context

This file is durable project memory for HOMERUN. Every future PR must update the change log at the bottom so the repo keeps a clear record of product, architecture, and safety decisions.

## 1. Product Goal

HOMERUN is a Kalshi-native MLB trading bot and operator dashboard. The long-term goal is to evaluate every MLB game on the slate and every mapped Kalshi MLB market as a candidate, then paper trade or eventually execute only when the model finds a positive expected-value contract.

The user wants the system to become as hands-off as possible. Calibration, thresholds, weights, retraining, monitoring, and model governance should eventually run inside the bot instead of depending on manual daily tuning.

## 2. Current Scope

PR 3 builds on the merged PR 2.5 targeted resolver:

- FastAPI backend in `apps/api`.
- Next.js TypeScript frontend in `apps/web`.
- PostgreSQL-ready database models and Alembic migrations.
- MLB schedule ingestion from the public MLB Stats API.
- Targeted Kalshi MLB market resolution from MLB game rows using the empirically observed `KXMLBGAME` full-game winner family.
- Kalshi yes/no orderbook parsing and raw payload storage for targeted markets only.
- Auditable MLB game to Kalshi market mapping with confidence and rationale.
- Conservative paper candidate and paper-trade generation using a transparent heuristic probability model.
- MLB results sync for completed games.
- Paper full-game winner settlement and realized P/L tracking.
- Paper balance snapshots from starting balance, open cost, realized P/L, and open mark value.
- Feature snapshot storage for model candidates with explicit missing-source markers.
- Automated model governance runs that skip training/promotion until sample thresholds are met.
- Database-backed dashboard API responses when data exists.
- Light-theme trading-terminal dashboard that renders portfolio snapshots, paper metrics, open positions, model status, and system status.
- Railway backend and PostgreSQL setup documentation.
- Vercel frontend setup documentation.
- CI scaffolding for backend tests and frontend checks.

The system still has no live trading, no production credentials requirement, no sportsbook logic, and no support for spreads, totals, or first-five markets.

## 3. Non-Goals

This project is not a sportsbook betting app. Do not add sportsbook assumptions, sportsbook APIs, sportsbook odds conversion, DraftKings, FanDuel, or Odds API behavior.

PR 3 still intentionally excludes:

- Live order placement.
- Production Kalshi credentials.
- A trained predictive model.
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
- Contracts or positions table.
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
- Spread, total, and first-five families remain `unknown_pending_discovery` and must not be faked.
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

## 14. PR Change Log

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

# HOMERUN Project Context

This file is durable project memory for HOMERUN. Every future PR must update the change log at the bottom so the repo keeps a clear record of product, architecture, and safety decisions.

## 1. Product Goal

HOMERUN is a Kalshi-native MLB trading bot and operator dashboard. The long-term goal is to evaluate every MLB game on the slate and every mapped Kalshi MLB market as a candidate, then paper trade or eventually execute only when the model finds a positive expected-value contract.

The user wants the system to become as hands-off as possible. Calibration, thresholds, weights, retraining, monitoring, and model governance should eventually run inside the bot instead of depending on manual daily tuning.

## 2. Current Scope

PR 1 creates the deployable foundation:

- FastAPI backend in `apps/api`.
- Next.js TypeScript frontend in `apps/web`.
- PostgreSQL-ready database models and initial Alembic migration.
- Empty but correctly shaped dashboard API responses.
- Light-theme trading dashboard shell with empty states.
- Railway backend and PostgreSQL setup documentation.
- Vercel frontend setup documentation.
- CI scaffolding for backend tests and frontend checks.

The system starts with no real Kalshi market discovery, no model scoring, no workers, and no live trading.

## 3. Non-Goals

This project is not a sportsbook betting app. Do not add sportsbook assumptions, sportsbook APIs, sportsbook odds conversion, DraftKings, FanDuel, or Odds API behavior.

PR 1 also intentionally excludes:

- Live order placement.
- Real Kalshi market discovery.
- Real MLB data ingestion.
- Admin password pages.
- Monthly calendars.
- Production secrets.
- Automated retraining jobs.
- Cron workers.

## 4. Architecture Decisions

The repo is a small monorepo:

- `apps/api` owns the backend API, typed configuration, database models, migrations, and backend tests.
- `apps/web` owns the frontend dashboard.
- Railway is the intended backend and PostgreSQL host.
- Vercel is the intended frontend host.

The backend exposes read-only foundation endpoints in PR 1:

- `GET /health`
- `GET /v1/dashboard/summary`
- `GET /v1/system/status`

The database layer is PostgreSQL-ready but the API can boot locally without a database so the first PR remains easy to run.

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

The dashboard should clearly show paper mode and live trading disabled. It should also show a clear light-theme error state when the API is unavailable.

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

Production Kalshi credentials should not be added during PR 1.

## 12. PR Change Log

Every future PR must update this section with:

- PR number or branch name.
- Plain-English summary.
- Any changes to scope, safety posture, architecture, model assumptions, deployment, or operations.
- Validation performed.

### PR 1 - Monorepo Foundation

- Created the initial FastAPI backend, Next.js dashboard shell, PostgreSQL-ready schema, setup docs, and CI scaffolding.
- Kept the system paper-trading only with live trading disabled and the execution kill switch enabled by default.
- Added empty dashboard responses and light-theme empty states without implementing Kalshi discovery, model scoring, or live execution.

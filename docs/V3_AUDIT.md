# V3 audit of v2.0 (what was real, partial, or docs-only)

Method: read the code on `main` (v2.0.0), ran the 79-test suite, inspected the live workflow/CI runs and the `data` branch DB. Status = **Implemented** (works + tested), **Partial** (works with a defect/limit listed), **Docs-only** (described but not enforced), **Missing**.

## Real file layout (v2.0)
`.github/workflows/run.yml` (scan/monitor/weekly/deploy), `ci_run.py`, `export_static.py`, `scheduler.py` (self-host APScheduler), `app/` (`main.py` FastAPI, `trading.py`, `engine.py`, `portfolio.py`, `ledger.py`, `migrations.py`, `settings_schema.py`, `control.py`, `ai.py`, `research.py`, `backtest2.py`, `strategies/{swing,meanrev,baseline,intraday}.py`, providers/), `site_src/` (static dashboard), `validation/results.json`, `docs/`, `tests/` (79 tests). Ledger DB `ailab.db` lives on branch `data`.

## Feature status at v2.0
| Area | Status | Finding |
|---|---|---|
| Append-only ledger, MYR deposit → USD sleeves, FX spread on real conversions | Implemented | triggers block UPDATE/DELETE; per-sleeve replay (`portfolio.replay2`) |
| Capital allocation | **Partial / contradicts v3** | swing 30 %, mean-reversion 30 %, **baseline 40 %** of real capital (SPY/QQQ were bought with trading cash) |
| Mean reversion | Implemented but live-trading | had 30 % of capital; the OOS backtest did not show positive net return after costs |
| Execution model | **Partial (integrity defect)** | pending orders were filled at *today's open* read from a daily bar fetched at run time, i.e. a price that was not observable when the run happened; no record of when the price became available |
| Stops | Partial | daily-bar `low <= stop`, gap → open; no missing-bar / halt / corporate-action handling; no explicit "not guaranteed" note |
| Order record | Partial | status/qty/fill price/ts only; no signal ts, data ts, eligible session, availability ts, fill-model version |
| NYSE calendar (DST/holidays/early close, MYT display) | Implemented | `pandas_market_calendars`, tested |
| Monitor cron | **Partial (defect)** | `*/15 13-20 * * 1-5` UTC: the winter (EST) session closes 21:00 UTC → last hour uncovered; not calendar-guarded at workflow level |
| Heartbeats / missed-run detection | Partial | `heartbeat` table = last timestamp per name only; no run records, no expected-run calculator, no skip reasons |
| Duplicate-run protection | Partial | workflow `concurrency` group only; no DB idempotency, no writer lease |
| Watchdog | Missing | |
| Forward verification (20 sessions) | Missing | |
| Shadow SPY/QQQ benchmarks | Partial | normalized lines on the equity chart; no RM10,000 virtual portfolios, no metrics table, no dividends |
| AI providers | Implemented | xAI/OpenAI/Anthropic/custom + GitHub Models via `GITHUB_TOKEN` |
| AI status / verification | Partial | label said "LLM" if a provider was *selected*, not if it *worked*; in CI GitHub Models answered plain text `OK` (no JSON) so the system ran in template mode without saying so clearly |
| Daily AI report | Missing | (daily *market summary* only) |
| Weekly evaluation | Partial | weekly strategy report; no profit factor / expectancy / by-signal tables |
| Owner controls on Pages | **Implemented but insecure by design** | browser form took a GitHub PAT (sessionStorage) able to write the repo |
| Self-hosted FastAPI | Implemented, **never deployed/verified** | Basic auth, CSRF header, rate limit, backup/restore; no Dockerfile; no writer-mode guard (two writers possible) |
| Validation | Partial | OOS split pre-declared, params untuned; survivorship bias (current index members) documented but not measured; no cost sensitivity / parameter stability |
| Provenance labels | Partial | "DELAYED/EOD" banner; no per-number labels, no data-request log |
| Tests | 79 pass | no tests for scheduler gaps, delayed fills, verification, writer lease |

## What v3 changes
See `docs/V2_VS_V3.md` (before/after) and `docs/MIGRATION_V3.md`. Every row above marked Partial/Missing/insecure has a v3 counterpart and a test.

# AI Trading Lab v3.0 - US-only paper trading (virtual RM10,000 -> USD, **never real money**)

Engine: Python + SQLite append-only ledger. Dashboard: static site (GitHub Pages) **or** FastAPI self-host. Data: **free yfinance only (EOD / delayed, never real-time, labelled everywhere)**.
No broker connection, no credentials in the repo or the frontend, the system cannot place real orders.

* Live site: https://harann02.github.io/ai-trading-lab/  (**read-only** — no token entry; owner controls via GitHub Actions inputs, see docs/CONTROLS.md)
* Engine/ledger state lives on branch `data` (`ailab.db`, `control/commands.json`); code on `main`; GitHub Actions (`.github/workflows/run.yml`) runs scans/monitors and publishes the JSON snapshot to Pages.

> **v3.0** is the current system (see `docs/V2_VS_V3.md`, `docs/V3_AUDIT.md`, `docs/MIGRATION_V3.md`). The earlier v2.0 description is kept only as a clearly labelled historical section in `docs/V2_VS_V3.md`.

## What v3.0 is (current configuration)
| Area | v3.0 behaviour |
|---|---|
| Market | **US only** (NYSE / NASDAQ / NYSE American). Bursa retired. Free yfinance data: end-of-day final bars + delayed (>=15 min) 5-minute candles, always labelled; never real-time. |
| Allocation | **Swing = 100 % of the trading pool** (the only strategy allowed to open new forward-paper positions). Mean reversion = **RESEARCH ONLY — TRADING DISABLED** (0 %). Intraday = **RESEARCH ONLY — TRADING DISABLED** (0 %). The old SPY/QQQ baseline sleeve is retired. |
| Benchmarks | **SPY and QQQ shadow portfolios** (virtual RM10,000, same start/FX/costs, separate tables, never trading capital). |
| Risk limits | 1 % risk per trade, 20 % max per stock, 80 % max exposure, 20 % minimum cash, max 5 positions, 10 % drawdown suspension, no leverage/shorts/derivatives. Settings are owner-adjustable only inside audited safe bounds (`app/settings_schema.py`; override is recorded in `audit_log`). |
| Money | Start **RM10,000** (ledger DEPOSIT in MYR) converted to USD at a recorded USDMYR rate + 30 bps spread; whole shares (fractional OFF by default); T+1 settlement; modelled fees/spread/slippage. `docs/ACCOUNTING.md`. |
| Execution | Delayed forward-paper fill model: orders become eligible next NYSE session and fill only from already-observable candles. `docs/EXECUTION_MODEL.md`. |
| AI layer | Explains verified data only; never decides. Four separate indicators (provider reachable / genuine AI output accepted / template fallback active / AI unavailable). "VERIFIED" is shown only when a model-written report was accepted by the validators in the last 7 days. `docs/AI_SETUP.md`. |
| Persistence | Ledger DB on branch `data` via compare-and-swap, hash-chain-verified saves (`app/persist.py`, `docs/PERSISTENCE.md`); rolling daily snapshots + append-only `ledger-log` branch. |
| Controls | Public dashboard is **read-only** (no token input). Owner changes go through GitHub Actions "Run workflow" inputs (`settings_json` / `command_json`) or the control file on branch `data`; the engine re-validates and audits everything (`docs/CONTROLS.md`). Strategy status is shown as "available for research" vs "enabled for forward-paper execution". |
| Verification | Forward verification counts only sessions run by the cron `schedule` event; manual/push runs are shown separately and never count as unattended evidence. Autonomous trading is **not** verified until the 20-session requirement is met. |

## Quick start (local)
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q                       # full suite (legacy v1 tests + v2 tests)
AITL_DB=data/ailab.db .venv/bin/python ci_run.py scan      # one real scan (network, yfinance)
.venv/bin/python export_static.py site && python3 -m http.server -d site 8000   # view the static dashboard
DASH_USER=me DASH_PASS=... .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765   # FastAPI self-host
```
`python run_validation.py` re-runs the historical comparison (analysis only; writes `validation/results.json`).

## Schedule (GitHub Actions; cron is best effort, minimum 5 min)
| Job | When (UTC) | MYT |
|---|---|---|
| scan | from NYSE close + 30 min (20:30 EDT / 21:30 EST; cron slot 21:41) Mon-Fri | about 04:30 (US summer) / 05:30 (US winter) Tue-Sat: first unattended run (GitHub cron or external backup) after final daily bars |
| monitor | every 15 min 13:00-21:59 Mon-Fri (gated by the NYSE calendar; skips recorded) | covers the full session in EDT and EST |
| weekly report | Sat 02:00 | Sat 10:00 |
| manual / owner-control | `workflow_dispatch` | any time |
Bursa jobs were removed. The schedule is decided by the NYSE calendar (America/New_York, DST, holidays, early closes via `pandas_market_calendars`).

## Execution model (v3)
See `docs/EXECUTION_MODEL.md`: orders become eligible at the next NYSE session and fill only from already-observable 5-minute candles (15 min availability delay) with modelled spread/slippage/fees/FX; otherwise awaiting-data / lapsed / review. No fabricated fills.

## Risk defaults
1% risk/trade, 20% single stock, 80% max exposure, 20% cash reserve, 5 positions, 10% drawdown suspension (manual resume), 2% intraday daily-loss breaker, no leverage/shorts/derivatives.
Size = floor(risk budget / (entry - stop + exit friction)) capped by affordability (SETTLED cash after reserve), allocation, exposure, liquidity (5% ADV).

## Honesty rules
Delayed yfinance data (unofficial; can be wrong). Fills are simulated. Backtests are labelled BACKTEST and never combined with forward paper results. Survivorship bias and missing point-in-time fundamentals are disclosed (`docs/VALIDATION.md`). Blockers needing credentials/paid feeds are listed in `docs/DATA_PROVIDERS.md`.

## Ledger & audit
`ledger` is append-only (SQLite triggers reject UPDATE/DELETE). Cash, positions, P&L, equity curves are derived from it. Decisions/orders/fills are idempotent (UNIQUE keys); re-running scans never duplicates. Schema changes use forward-only migrations (`app/migrations.py`) with a DB copy archived first. Settings changes and control commands are in `audit_log` / `control_applied`.

## Docs
`docs/SCORING.md` (score + regime + rule math) - `docs/STRATEGIES.md` - `docs/ACCOUNTING.md` - `docs/DATA_PROVIDERS.md` - `docs/CONTROLS.md` - `docs/VALIDATION.md` - `docs/DEPLOY.md` - `docs/AI_SETUP.md` - `docs/EXECUTION_MODEL.md` - `docs/SCHEDULER.md` - `docs/SHADOW_BENCHMARKS.md` - `docs/FORWARD_VERIFICATION.md` - `docs/MIGRATION_V3.md` - `docs/EXPERIMENT_PROCESS.md` - `docs/ENTRY_CADENCE_STUDY.md` - `docs/V3_AUDIT.md` - `docs/V2_VS_V3.md`.

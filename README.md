# AI Trading Lab v2.0 - US-only paper trading (virtual RM10,000 -> USD, **never real money**)

Engine: Python + SQLite append-only ledger. Dashboard: static site (GitHub Pages) **or** FastAPI self-host. Data: **free yfinance only (EOD / delayed, never real-time, labelled everywhere)**.
No broker connection, no credentials in the repo or the frontend, the system cannot place real orders.

* Live site: https://harann02.github.io/ai-trading-lab/  (read-only for visitors; owner controls after entering a GitHub token in the browser)
* Engine/ledger state lives on branch `data` (`ailab.db`, `control/commands.json`); code on `main`; GitHub Actions (`.github/workflows/run.yml`) runs scans/monitors and publishes the JSON snapshot to Pages.

## What v2.0 is
| Area | v2.0 behaviour (docs) |
|---|---|
| Market | **US only** (NASDAQ / NYSE / NYSE American). Bursa is retired: no screening, schedules, brokerage/lot rules, KLCI benchmark, UI selection or cron jobs. Old Bursa records are kept **read-only** (`archived_records`, `bursa_archive_decisions`, pre-migration DB copy in `archive/`). |
| Universe | Discovered automatically: S&P 500 (Wikipedia) + Nasdaq-100 (Nasdaq API, Wikipedia fallback) + Nasdaq Trader exchange directory; cached; filters: exchange, no ETFs/test issues, price >= $10, 50d avg volume >= 1M, 20d dollar volume >= $20M, vol sanity 5-150% annualised, >= 252 bars. Nothing is hard-coded as a recommendation. Rejections and reasons are stored (`universe_members`). |
| Money | Start **RM10,000** (ledger DEPOSIT in MYR). The treasury is converted to USD sleeves at a **recorded real USDMYR=X rate + configurable spread (default 30 bps)**; each conversion is a ledger row + `fx_conversions` audit row. Display uses mid (never charges spread). Stock return vs currency gain/loss are reported separately. See `docs/ACCOUNTING.md`. |
| Cash rules | Whole shares by default (`fractional_shares` off), cash-account mode, T+1 settlement (NYSE calendar), unsettled proceeds not reusable (configurable), US fees: $0 commission (configurable) + SEC fee and FINRA TAF on sells, modelled half-spread by liquidity tier + slippage. |
| Strategies | Independent engines with their own capital sleeve, strategy_id on decisions/orders/fills, own metrics: **A swing** (30%), **C mean reversion** (30%), **D buy&hold SPY/QQQ baseline** (40%), **B intraday** (0%, DISABLED, research only). `docs/STRATEGIES.md`. |
| Providers | `yfinance` (full), `alpaca` and `polygon` adapters (activate only with env keys, capabilities probed; **UNTESTED here: no keys**). Missing capability => strategy auto-disabled; stale data blocks trading. `docs/DATA_PROVIDERS.md`. |
| AI layer | Explains verified data only; never decides. Providers by env (`XAI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY`, any OpenAI-compatible endpoint via `AITL_LLM_API_KEY`+`AITL_LLM_BASE_URL`, free GitHub Models via `GITHUB_TOKEN`); otherwise template text labelled "template, no LLM". Every prompt/response/source is logged (`llm_log`); numbers in LLM output are verified against the source. |
| Dashboard | Dark terminal, responsive; KPI cards, discovery table (sort/search/filter), stock page (candles, SMA/VWAP-proxy/RSI/MACD, trade markers, score breakdown, memo), positions, strategies, research feed, activity monitor, validation lab, owner controls. US schedule shown in MYT. |
| Controls | `docs/CONTROLS.md`: owner token (sessionStorage only) -> Contents API writes `control/commands.json` on `data` -> `workflow_dispatch` -> engine validates, applies, audits -> page auto-polls. **Not real time: updates happen on workflow runs.** |

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
| scan | 21:30 Mon-Fri | 05:30 Tue-Sat (after the NYSE close in both EDT and EST) |
| monitor | every 15 min 13:00-20:59 Mon-Fri (guarded: acts only if NYSE is open) | covers 21:00-04:59 MYT |
| weekly report | Sat 02:00 | Sat 10:00 |
| manual / owner-control | `workflow_dispatch` | any time |
Bursa jobs were removed. The schedule is decided by the NYSE calendar (America/New_York, DST, holidays, early closes via `pandas_market_calendars`).

## Execution model
Scans run after the close and create **pending** orders; they fill at the **next session's open** (+modelled spread/slippage/fees) only while NYSE is open and today's (delayed) bar exists. Stops are checked intraday on the delayed bar's low; a gap through a stop fills at the open. Every gate is re-checked at fill time.

## Risk defaults
1% risk/trade, 20% single stock, 80% max exposure, 20% cash reserve, 5 positions, 10% drawdown suspension (manual resume), 2% intraday daily-loss breaker, no leverage/shorts/derivatives.
Size = floor(risk budget / (entry - stop + exit friction)) capped by affordability (SETTLED cash after reserve), allocation, exposure, liquidity (5% ADV).

## Honesty rules
Delayed yfinance data (unofficial; can be wrong). Fills are simulated. Backtests are labelled BACKTEST and never combined with forward paper results. Survivorship bias and missing point-in-time fundamentals are disclosed (`docs/VALIDATION.md`). Blockers needing credentials/paid feeds are listed in `docs/DATA_PROVIDERS.md`.

## Ledger & audit
`ledger` is append-only (SQLite triggers reject UPDATE/DELETE). Cash, positions, P&L, equity curves are derived from it. Decisions/orders/fills are idempotent (UNIQUE keys); re-running scans never duplicates. Schema changes use forward-only migrations (`app/migrations.py`) with a DB copy archived first. Settings changes and control commands are in `audit_log` / `control_applied`.

## Docs
`docs/SCORING.md` (score + regime + rule math) - `docs/STRATEGIES.md` - `docs/ACCOUNTING.md` - `docs/DATA_PROVIDERS.md` - `docs/CONTROLS.md` - `docs/VALIDATION.md` - `docs/DEPLOY_SELFHOST.md`.

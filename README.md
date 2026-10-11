# AI Trading Lab - paper-trading platform (virtual RM10,000, **never real money**)

FastAPI + SQLite (append-only ledger) + APScheduler (separate persistent process) + dark-terminal dashboard (TradingView Lightweight Charts, vendored locally).
Free data only: **yfinance** (Yahoo). Data is **EOD / delayed (>= ~15 min), never real-time**, and labeled so everywhere.
No broker connection, no API keys, no secrets in the frontend. The system cannot place real orders.

## Install
```bash
cd /workspace/ai-trading-lab
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # 32+ tests, all must pass
```

## Run (two independent processes)
```bash
./run.sh                 # starts web (http://127.0.0.1:8765) + scheduler with nohup; ./run.sh stop to stop
# or manually:
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765     # web/API/dashboard
.venv/bin/python scheduler.py                                    # persistent scheduler (works with the browser closed)
```
CLI: `python cli.py scan [BURSA|US]` (one real scan), `python cli.py monitor`, `python cli.py backtest`, `python cli.py report`.
Dashboard binds to 127.0.0.1 and **every route requires HTTP Basic auth** (`DASH_USER`/`DASH_PASS` from the environment or `.env`, chmod 600, never in frontend code; the server answers 503 if they are unset). The scheduler is a separate process and is unaffected. Still prefer an SSH tunnel or HTTPS reverse proxy for remote access.

**v1.1.0 (small-account tuning):** `min_trade_value_myr` 1600 (RM8 min brokerage <= 0.5%), `weekly_entries_only` (entries/SMA50 exits only on the week's closing session), ATR trailing stop 4.0x, buy score 70, chosen on train/validation only; the test window was evaluated once (see `backtests` table id 2; id 1 = v1.0.0 kept untouched).

### Persistent server (systemd)
```bash
sudo cp deploy/*.service /etc/systemd/system/      # edit WorkingDirectory/ExecStart paths if you move the project
sudo systemctl daemon-reload
sudo systemctl enable --now ai-trading-lab-web ai-trading-lab-scheduler
journalctl -u ai-trading-lab-scheduler -f
```
The scheduler stores jobs in `data/scheduler.db` (SQLAlchemy job store), coalesces missed runs and fires missed jobs within a 3h grace window after a restart.
The dashboard shows scheduler health (heartbeat every 30 s; DOWN if >120 s old) and next run times.

### Schedule (all jobs guard on the exchange calendar)
| Job | When | What |
|---|---|---|
| scan_bursa | Mon-Fri 17:30 MYT (Bursa closes 17:00) | collect, regime, screen, rank, validate, create pending orders, EOD reconcile, daily report |
| scan_us | Tue-Sat 05:30 MYT (NYSE closes 04:00/05:00 MYT depending on DST) | same for US session of the previous US day |
| monitor_bursa | Mon-Fri every 10 min, 09:00-17:50 MYT | **only if Bursa is open**: fetch delayed bars, fill pending orders at the session open, intraday stop checks |
| monitor_us | Mon-Fri every 10 min, 09:00-16:50 New York time | same for NYSE |
| weekly_review | Sat 09:00 MYT | weekly review report |
| heartbeat | every 30 s | scheduler health |

Execution model: scans run after the close and create **pending** orders; they are filled at the **next session's open** (+slippage, fees) only while the exchange is open
and today's (delayed) bar exists. Stops are checked intraday and at EOD; a stop is filled at the stop price, or at the **open if the price gapped through it**.

## Configuration
Everything is editable in the dashboard (section J) or via `PUT /api/settings`; defaults live in `app/config.py`: capital, markets, watchlists (Bursa `.KL`, US), benchmarks
(^KLSE, SPY), risk 1%/trade, max 20%/stock, 80% exposure, 20% min cash, 5 positions, 10% max drawdown (suspends new entries until you click *Authorize resume*),
ATR stop multiple, fees (Bursa brokerage 0.10% min RM8, clearing 0.03%, stamp RM1/RM1000; US $0.005/share min $1 + SEC fee on sells; 10 bps FX spread), slippage (10 bps Bursa / 5 bps US),
lot sizes (100 / 1), risk-free rate (3% p.a. for Sharpe/Sortino), auto-trade ON/OFF and manual-approval mode.
Environment: `AITL_DB` overrides the SQLite path (default `data/ailab.db`).

## Data honesty rules
* A symbol is tradable only if its last **final** daily bar equals the exchange's last completed session, has >= 210 bars and sane OHLC. Otherwise: flagged STALE/MISSING/INSUFFICIENT/ANOMALY, decision = REJECT "DATA BLOCKED", no order. Stale data is never substituted.
* If the database has no live bars at all the UI is in **DEMO** mode (banner); synthetic data is only generated for chart display, never stored as prices, and trading is disabled.
* Fundamentals/analyst targets come from `yfinance .info` (cached 7 days, may be missing or wrong); missing data is penalized/flagged, never invented. No news is used.
* Labels: **FORWARD PAPER** (live simulated), **BACKTEST** (historical hypothetical), no other results are claimed.

## Ledger & audit
`ledger` is append-only (SQLite triggers reject UPDATE/DELETE). Cash, positions, realized P&L, the equity curve and daily reports are all derived from it
(daily report hash is reproducible). Every BUY stores: algorithm version, input snapshot (indicator values, fundamentals, FX, regime, data status), factor scores, checks, decision, order, fill, and an investment memo.
Idempotency: `decisions.decision_key` and `orders.decision_key` are UNIQUE (algo:market:session:symbol:last_bar), fills are unique per order; re-running scans/refreshing never duplicates orders.
"Full reset" archives the old DB to `data/archive/` (the ledger itself is never edited).

## Docs
`docs/SCORING.md` - every factor normalization, regime rule, rule sets, sizing. `app/backtest.py` docstring - backtest design and caveats.

## Tests
`pytest` covers: RM10,000 credit, cash after buy incl. fees, realized P&L on sell, equity = cash + MTM, persistence across process restart, risk/capital limits, duplicate-signal idempotency,
closed-market blocking, missing/stale data => no trade, report reproducibility, no look-ahead (features, signals, backtest entries), indicators (RSI Wilder, ATR, MACD, SMA/EMA, R:R, sizing, fees),
gap-through-stop, drawdown suspension + manual resume, manual-approval gate, regime, fundamentals-missing rule, TWR/MWR with flows.

## Known limitations
See bottom of the report in the project hand-off; in short: yfinance is unofficial/delayed/occasionally wrong, Bursa holiday data is best-effort (pandas_market_calendars XKLS + Malaysia public holidays, conservative union),
backtest has survivorship bias and no point-in-time fundamentals, no LLM is wired in (template text only), the tiny RM10k account makes Bursa minimum brokerage (RM8/side) a large cost drag, and nothing here is investment advice.

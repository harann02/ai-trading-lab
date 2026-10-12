# Backtest vs live (forward) parity

Methodology ids: **`swing-parity-minus-fundamentals-1`** (new, `app/backtest_parity.py`, `run_validation_parity.py`) and **LEGACY** (v2 backtest, `validation/results_legacy_v2.json`, kept unchanged; its +56.5 % full / +40.9 % out-of-sample numbers are **LEGACY, not validated for the current live strategy**).

## Every difference found (legacy backtest vs forward engine) and how the new run treats it
| # | Area | Forward (live) engine | Legacy backtest | Parity run |
|---|---|---|---|---|
| 1 | **Fundamentals factor** (20 % weight) | used for the top-N shortlist (yfinance current `.info`) | omitted, other weights renormalised | still omitted (**no point-in-time fundamentals exist on free sources; current values are NOT substituted**); weights renormalised proportionally (/0.80) = live rule when a symbol has no fundamentals; labelled *parity-minus-fundamentals* |
| 2 | Analyst target in R:R | used when no resistance target exists (shortlist) | never | not available historically; target = prior 252d high only (documented residual) |
| 3 | Shortlist gate | only the top `universe_fundamentals_top_n` (=30) by light score among the filtered universe can become BUY ELIGIBLE; everything else is a compact row | every symbol passing filters evaluated | **same gate** (ranked per decision date) |
| 4 | Pre-filter | none (Minervini check inside the rule set) | extra pre-filter `close>SMA200 & SMA50>SMA150 & close>SMA50` | removed |
| 5 | RS percentile universe | percentile among the filtered universe | among all constituents | among the filtered universe |
| 6 | Execution-quality check | ADV >= min_adv_usd **and** ATR% <= 8 % | not applied | applied |
| 7 | Cadence | `entry_cadence=weekly` (Fri) default | weekly | same (live default config) |
| 8 | Score thresholds / regime / min R:R / ATR stop mult | `config.DEFAULTS` | same constants | same object (`config.DEFAULTS`) |
| 9 | Sizing / risk | `risk.size_position_usd` (1 % risk, 20 % cap, 80 % exposure, 20 % reserve, whole shares, settled cash) | same function | same function |
| 10 | Exits | stop, target, SMA50 trend break, ATR trailing stop | same | same |
| 11 | **Fill model** | next eligible NYSE session; fills only from already-available 5-minute candles (15 min delay), price = close of newest available candle; stale/gap protections | next-session **open** + modelled spread/slippage | daily bars cannot reproduce delayed 5-min fills: next-session open + spread/slippage; **sensitivity run with +10 bps extra slippage** as a delayed-fill proxy; remaining difference documented |
| 12 | Stops | simulated from available 5-min candles (gap -> open) | daily low touch (gap -> open) | same as legacy (daily low) |
| 13 | Gap-chase guard | none (corporate-action review only at >35 %) | skips entry if open > planned entry +3 % | kept (documented difference, conservative) |
| 14 | Dividends | ignored for the strategy; shadows include them | ignored | ignored (benchmarks therefore price-return) |
| 15 | Universe | current S&P500 + Nasdaq-100 + exchange directory, daily live filters | current constituents (survivorship bias) | current constituents (survivorship bias remains) |
| 16 | Account | actual ~US$2,441 whole shares | RM10,000 whole shares (same) | RM10,000 headline + small-account sensitivity (RM25k, RM50k, fractional what-if) |
| 17 | Affordability gate | reason-coded, never relaxes limits | implicit via size>=1 | implicit via size>=1 (identical effect) |
| 18 | Trade record | every order/fill stored | only 15 sample trades | **full trade lists** in `validation/trades_swing_parity.json`; win rate / profit factor / expectancy recomputable (`_trade_stats`) |

## Rules of the re-run
Pre-declared splits (unchanged from legacy): in-sample <= 2023-12-29, out-of-sample 2024-01-02 -> end. Parameters are `config.DEFAULTS`, **not tuned**; the out-of-sample window was not used to choose anything. No fabricated or substituted fundamentals. Point-in-time fundamentals are now archived forward-only in `fundamentals_pit` (append-only, retrieval timestamp per record) starting 2026-10-12: they cannot be back-filled and become usable only after enough history exists.

Results: see the Validation tab, `validation/results.json` and `REMEDIATION_REPORT.md`.

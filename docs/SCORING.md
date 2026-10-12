# AI Trading Lab - Scoring, regime and rule definitions (algo `ailab-1.1.0`)

All factor scores are on **0-100**, computed from real data only. A factor that cannot be computed is **excluded and
the remaining weights are renormalized, with a flag** (it is never silently zero-filled or invented).
`lin(x, lo, hi)` = clip((x-lo)/(hi-lo), 0, 1) x 100. `inv(x, best, worst)` = clip((worst-x)/(worst-best), 0, 1) x 100.

**Composite = 0.25 Momentum + 0.20 Fundamental + 0.20 Technical + 0.15 RS/Regime + 0.10 Liquidity + 0.10 Risk**

Bands: >=85 strong candidate | 70-84 watchlist/conditional | 50-69 hold (no new entry) | <50 no entry.
**A score never buys.** BUY additionally requires every check in "Decision checks" below.

## 1. Momentum (0.25)
Weighted average of available parts: 0.4 x lin(12-1 month return, -20%, +80%) + 0.3 x lin(126d return, -10%, +50%) + 0.3 x lin(63d return, -10%, +30%).
12-1 month = close[t-21]/close[t-252]-1 (skips the most recent month).

## 2. Fundamental (0.20) - yfinance `Ticker.info` (cached 7 days, labeled with fetch time)
Seven metrics, each 0-100: ROE lin(0,25%); Debt/Equity inv(0,200 %-units); Profit margin lin(0,25%); Revenue growth lin(-5%,+25%);
Earnings growth (quarterly) lin(-10%,+40%); Trailing P/E (<=0 -> 0, else inv(12,40)); FCF yield (freeCashflow/marketCap) lin(0,8%).
**Missing-data rule:** if >=4 of 7 are available: score = mean(available) - 2 points per missing metric (floored at 0), flag lists the missing ones.
If <4 available: score = 50 (neutral) - 10 penalty = **40**, flagged "fundamentals mostly missing".
(Banks typically lack Debt/Equity and FCF in yfinance; this is why they carry a small penalty.)

## 3. Technical (0.20)
0.50 x trend-template pass fraction (passed/8 x 100) + 0.15 x MACD histogram>0 (100/0) + 0.15 x RSI zone (50-70: 100; 40-50: 60; 70-80: 50; else 15)
+ 0.20 x EMA20>EMA50 (100/0).

## 4. Relative strength / regime (0.15)
0.6 x RS + 0.4 x regime score. RS = lin(126d excess return vs the market's benchmark, -15%, +25%), averaged 50/50 with the symbol's universe percentile
of 252d excess return when >=5 symbols are rankable. Regime score: bullish 100, sideways 50, volatile 25, bearish 0, unknown 40.

## 5. Liquidity (0.10)
20-day average traded value in MYR (close x volume x FX): log-scaled, RM1m -> 0, RM50m -> 100.

## 6. Risk (0.10)
0.4 x inv(ATR14/close, 1%, 8%) + 0.3 x inv(252d annualized volatility, 20%, 70%) + 0.3 x inv(drawdown from 252d high, 5%, 35%).

## Market regime (per market, from its benchmark: ^KLSE for Bursa, SPY for US)
Evaluated in this order (daily close data):
1. **volatile** if 20d annualized realized volatility >= 25%
2. **bearish** if close < SMA200 and SMA50 < SMA200
3. **bullish** if close > SMA200 and SMA50 > SMA200 and SMA200 21-day slope > 0
4. else **sideways**.
Default policy: entries allowed in bullish (score >= 70) and sideways (score >= 85); blocked in bearish/volatile/unknown.

## Rule sets (measurable)
**Minervini trend template (8 criteria; all 8 required for BUY):** close>SMA150 & SMA200; SMA150>SMA200; SMA200 rising over 21d; SMA50>SMA150>SMA200;
close>SMA50; close >= 1.3 x 52w low; close within 25% of 52w high; RS strong (universe RS percentile >= 70, fallback 252d excess return > 0).
Unknown criterion (insufficient history) => fail.

**CAN SLIM-lite (informational; shown in memo/counterarguments):** C quarterly earnings growth >=25%; A revenue growth >=15% or ROE >=17%; N within 5% of 52w high;
S volume ratio >= 1.0; L RS percentile >=80 (fallback excess126>0); I institutional ownership >=20%; M regime bullish. Missing items reported as "not evaluated".

**Graham-style (informational):** P/E<=15; P/B<=1.5; P/E x P/B<=22.5; Debt/Equity<=100%; current ratio>=2; positive trailing EPS; pays dividend.

## Entry trigger
*Breakout*: close > prior 20-day high, volume ratio (vs prior 20d mean) >= 1, RSI14 <= 80. *Near-high*: close within 2% of prior 20-day high, above EMA20, RSI 50-70.

## Trade plan (never invented)
Entry reference = last completed close (actual fill = next session open + slippage). Stop = close - 2.5 x ATR14 (Wilder); trailing: highest close since entry - 2.5 x current ATR, ratchets up only.
Target = prior 252-day high if above entry (historical resistance), else analyst mean target (yfinance, >=3 analysts) if above entry, else **none**.
Reward:risk = (target-entry)/(entry-stop); must be >= 2.0 **only when a credible target exists**; with no target the exit relies on the trailing stop/trend break (SMA50) and the memo says so.

## Position size
risk budget = equity x 1%; loss/share = (slipped entry - slipped stop) + per-share fees both sides; qty = floor(budget / (loss/share x FX));
then capped by: cash above the 20% minimum cash reserve (incl. fee/FX buffer), 20% max per stock, 80% max total exposure, 5% of 20-day ADV, and rounded down to lot size (Bursa 100, US 1).

## Decision checks (all must pass for BUY)
fresh data (final bar == last completed session; >=210 bars; sane OHLC) - FX available (US) - regime allowed - score threshold (70 bullish / 85 sideways) - Minervini 8/8 -
entry trigger - symbol not manually blocked - not already held/pending - R:R (if target) - size >= 1 lot - entries not suspended (drawdown) - max positions - min cash - max exposure -
execution quality (ADV >= RM1m, ATR% <= 8%) - then at fill time: market open, today's bar present, open not gapped >3% above plan or below stop, re-sized on live equity/cash.
BUY decisions become *pending* orders; if auto-trade is OFF no order is created; manual-approval mode holds the order until approved.


---
# v2.0 addendum (US-only; algo `swing-2.0.0` / `meanrev-2.0.0`)
* Weights are **25 / 20 / 20 / 15 / 10 / 10 = 100** (Momentum / Fundamental / Technical / RS-regime / Liquidity / Risk); `scoring.validate_weights()` raises if they do not sum to 100 or disagree with `W`. Tested.
* Liquidity is now in **USD** (20d average dollar volume); the Bursa/MYR liquidity and lot-size rules are gone (whole shares unless `fractional_shares`).
* v2 features added by `scoring.enrich`: SMA5/20/50/150/200, EMA12/26, RSI14 (Wilder), RSI2, MACD(12,26,9), ATR14, Bollinger(20,2), 52-week high/low proximity, relative strength vs **both SPY and QQQ** (63/126/252d), relative volume (vs prior 20d), breakout (close > prior 20d high) / pullback (above SMA50, within 1 ATR of EMA20) structure flags. Fundamentals come from yfinance when available (missing-data rule above).
* Regime is computed from SPY only. Market-hours/lot/brokerage rules for Bursa were removed.
* ATR trailing stop multiple default is 4.0 (v1.1 tuning; the 2.5 in the text above is the pre-v1.1 value). Position size is the USD formula in `docs/STRATEGIES.md`.

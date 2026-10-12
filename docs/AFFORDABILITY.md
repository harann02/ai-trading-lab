# Small-account affordability (US$ ~2,441 account)

The paper account is ~US$2,441.85 (RM10,000 after FX). With the **unchanged** limits (20 % single stock, 1 % risk per trade, 20 % cash reserve, 80 % exposure, whole shares) the usable numbers are:

| Item | Value |
|---|---|
| Single-stock cap (20 %) | **US$488.37** |
| Risk budget per trade (1 %) | **US$24.42** |
| Cash reserve (20 %) | US$488 (never spent) |
| Max exposure (80 %) | US$1,953 |

## The AFFORDABILITY gate
`app/affordability.py` is applied to every swing candidate (check `affordability_gate`) and shows a reason code in the Discovery / rejected lists. It **never relaxes a limit**; it only classifies and counts. Codes:

| Code | Meaning |
|---|---|
| `AFFORDABLE` | at least 1 whole share fits every cap |
| `UNAFFORDABLE_PRICE_GT_CAP` | one share (incl. modelled spread/fees) costs more than the 20 % single-stock cap |
| `UNAFFORDABLE_RISK_PER_SHARE` | the ATR-stop loss for one share (entry-stop + exit friction) is larger than the 1 % risk budget |
| `UNAFFORDABLE_CASH` | one share costs more than usable cash after the reserve |
| `UNAFFORDABLE_EXPOSURE` / `_LIQUIDITY` | no exposure room / exceeds ADV % cap |
| `UNAFFORDABLE_INVALID_STOP` | no valid stop |

If several caps block, the first by priority (price cap, cash, exposure, liquidity, risk) is the code and the others are listed as "also". Reward-to-risk, trigger and trend checks remain separate checks.
Every scan stores a daily **affordable-universe count** (`affordability_daily`, exported in `v3.json.affordability`, shown on the Discovery tab) with the whole-share count and an informational fractional what-if.
**"No affordable valid trade" is a legitimate decision; cash is an acceptable outcome.**

## Analysis on the 2026-10-12 scan data (436 stocks that passed the universe filters)
Reproduce: `AITL_DB=<copy of ailab.db> python tools/affordability_analysis.py` (result: `docs/affordability_analysis_2026-10-12.json`).

| | Whole shares (live setting) | Fractional what-if (OFF) |
|---|---|---|
| Affordable | **318 / 436 (72.9 %)** | 436 / 436 |
| `UNAFFORDABLE_RISK_PER_SHARE` | 95 | 0 |
| `UNAFFORDABLE_PRICE_GT_CAP` | 23 | 0 |

Median price US$119, p90 US$374. Affordability is not the main bottleneck: of the 30 swing WATCH names with full checks, the blockers are reward-to-risk (25), entry trigger (21), whole-share size (20) and trend (1). Only **2** (HUM, DDOG) fail *only* because of whole-share size; 7 more fail size **and** reward-to-risk. Today **0** swing candidates are BUY ELIGIBLE; fractional shares would at most make 2 additional names size-feasible, not eligible by default.

## Fractional shares: feasibility and decision
* **Execution assumptions**: our fills are modelled from delayed yfinance candles; a fractional fill is the same model with a fractional quantity (rounded down to 4 decimals). Real brokers differ: not every symbol is fractionable, minimum order sizes (~US$1-5), market-orders only, extended-hours rules, and rounding of the executed quantity. None of that can be verified on the free stack, so a fractional result would be *less* realistic than a whole-share one.
* **Fees/rounding**: with 0 commission the modelled fee is the spread/slippage; fractional sizing makes the 1 % risk rule bind exactly (position = risk budget / risk per share), which is why all 436 become "affordable" - but the position is then often tiny (risk US$24 on a US$10-30 stop distance = <1 share), so the account would hold many tiny positions and be dominated by cost drag and the 5-position cap.
* **Decision**: `fractional_shares` stays **OFF by default** and is never auto-enabled. The what-if count is shown for transparency only. The owner can enable it explicitly (settings validation allows it); that would be a methodology change to be recorded as a new experiment epoch.

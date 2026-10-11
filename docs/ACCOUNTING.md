# Accounting: MYR treasury, USD sleeves, FX, settlement

* **Start**: ledger DEPOSIT RM10,000 (MYR). Nothing is USD until converted.
* **Allocation**: each enabled strategy gets `allocation_pct` of *net contributed MYR* (swing 30 / meanrev 30 / baseline 40 / intraday 0). The sum must be <= 100 (validated; no double allocation). `trading.ensure_allocations` converts only the missing amount (idempotent).
* **Conversion**: `fx.convert_myr_to_usd` - rate = configured provider's mid (default yfinance `USDMYR=X`, stored in `fx_rates` with timestamp/source), effective rate = mid x (1 + spread_bps/10000), USD = MYR / effective. One atomic transaction writes `fx_conversions` (audit: mid, spread bps, effective rate, spread cost in MYR, provider, rate timestamp, source) and an append-only ledger row (MYR out / USD in) plus an `audit_log` entry. Stale (> `fx_max_age_days`) or missing rates refuse the conversion.
* **Display** uses the **mid** rate only. The spread is charged once, at a real conversion, never for display.
* **Trades** are USD ledger rows tagged `strategy_id`: price = reference +/- modelled half-spread (liquidity tier) and slippage; fees = commission (default $0) + SEC fee and FINRA TAF on sells.
* **P&L attribution** per sleeve: *stock P&L* = USD result valued at the sleeve's FX cost basis (MYR invested / USD received); *currency P&L* = USD equity x (current mid - cost basis rate); *FX spread paid* shown separately. total = stock + currency (+ spread already inside the basis).
* **Settlement**: SELL proceeds settle T+1 NYSE session (`calendars.add_sessions`). In cash-account mode BUY needs *settled* cash (cash minus unsettled proceeds) unless `unsettled_proceeds_reusable`. Buying power = settled cash (after pending-order reservations).
* **Fractional shares**: only when `fractional_shares` is true (default false).
* **Equity curves**: `equity_curve2` (per sleeve and ALL, USD and MYR at that day's mid FX) is derived deterministically from the ledger + final bars. If the FX bar for a day is missing the last known mid is used (never 0).

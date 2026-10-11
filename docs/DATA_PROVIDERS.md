# Data providers & capabilities

Interface: `app/providers/base.py` (`Provider`, `CapResult`). Capabilities: historical, eod, intraday_candles, realtime_quotes, bid_ask, trades, depth, corporate_actions, fundamentals, earnings_calendar, news. `registry.probe_all` actually calls each active provider and stores results in `provider_caps` (shown on the Strategies tab).

| Provider | Status | Notes |
|---|---|---|
| yfinance | **fully implemented, tested live** | EOD + delayed intraday (1m ~7d, 5m ~60d), fundamentals (`.info`, cached 7d), news headlines. Always labelled *delayed/EOD*, **never real-time**. No bid/ask/trades/depth. |
| Alpaca | adapter written, **UNTESTED (no keys)** | Activates only if `ALPACA_API_KEY` + `ALPACA_API_SECRET` exist. Free IEX feed = real-time quotes/trades for IEX only (not consolidated SIP). |
| Polygon | adapter written, **UNTESTED (no keys)** | Activates only with `POLYGON_API_KEY`. Free tier is delayed/rate-limited; real-time needs a paid plan. |

Rules: a strategy lists REQUIRES capabilities; if no active provider has them it is auto-disabled with a visible reason. Stale/missing/anomalous bars => `DATA BLOCKED`, no orders. Order-flow is never inferred from OHLCV; intraday VWAP/relative volume are explicitly **price+volume PROXIES**.
**Intraday (B) stays DISABLED for live use** unless a provider reports real-time quotes + intraday candles with >= `MIN_HISTORY_DAYS`, the owner switch is ON and allocation > 0.

Blockers / cheapest alternatives: real-time consolidated quotes & bid/ask/depth need paid data (Polygon/Alpaca SIP, ~US$29-99/mo). Free alternative: Alpaca free IEX feed (needs a free Alpaca account key; not tested here). Point-in-time fundamentals and historical index membership need paid sources (Sharadar/Norgate/FMP paid).

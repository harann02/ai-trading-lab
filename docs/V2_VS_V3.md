# v2.0 → v3.0 before/after
| Topic | v2.0 | v3.0 |
|---|---|---|
| Capital | swing 30 / mean-rev 30 / SPY-QQQ baseline 40 / intraday 0 | swing 100 % (PRIMARY); others 0 %; 80 % max exposure, 20 % reserve, 20 %/stock, 1 % risk, 5 positions |
| Mean reversion | live, 30 % | RESEARCH ONLY — NEW TRADES DISABLED (enforced in scan, fill, settings); reactivation = owner + validation |
| SPY/QQQ | bought with real capital | separate virtual RM10,000 shadow portfolios + metrics table + normalized chart |
| Sleeve cash move | n/a | auditable `SLEEVE_XFER_*` journal rows (no deposit, no P&L, no double FX); reconciliation tool |
| Fills | today's open read from a daily bar at run time | delayed-execution model: next-session eligibility, only observable 5-min candles, availability timestamp, awaiting-data / lapsed / review, never retroactive |
| Order record | status + fill | + signal/data ts, order type, eligible session, source, availability ts, exec ts/price, spread, slippage, fees, FX, model version, fill kind |
| Stops | daily low ≤ stop | gap-aware, extra slippage, "not guaranteed", missing-bar/halt/corp-action guard |
| Monitor cron | 13–20 UTC (missed EST 21:00 close) | 13–21 UTC + NYSE-calendar gate, skip reasons recorded |
| Heartbeat | last-timestamp per name | `job_runs` (scheduled/started/finished/status/reason), expected-run calculator, missed-run detection |
| Duplicates | workflow concurrency | + DB idempotency keys + writer lease + writer_mode |
| Watchdog | none | `watchdog.yml` → GitHub issue |
| Forward verification | none | 20-session module (15 items), 0/20 at launch, cannot self-certify |
| AI status | "LLM" if configured | VERIFIED only after a real grounded-JSON probe; categories logged; free-first providers; key-less path probed |
| Reports | daily market summary, weekly strategy | + stored daily AI report, weekly evaluation (PF with sample guard, expectancy, by stock/signal) with proposals only |
| Owner controls on Pages | browser GitHub PAT | none (read-only); Actions inputs / file edit / authenticated self-hosted backend |
| Self-hosted | auth+CSRF+rate limit, no Docker, two writers possible | + Dockerfile/compose, writer-mode 409 guard, scheduler lease, backup/restore scripts (image not built here) |
| Dashboard | 8 tabs | + Benchmarks, Reports, Health, Verification, Migration; strategy lab with 4 evidence classes; provenance labels; refresh vs market-data time |
| Validation | OOS split, untuned | + cadence study, cost ×1/2/3, parameter ±1, yearly regimes, adj-close total return, membership-bias measurement (limits stated) |
| Tests | 79 | see final report (pytest + headless-Chrome frontend tests) |


---
# Historical (v2.0) — NOT the current configuration
Kept for audit continuity only. v2.0 allocated swing 30 % / mean reversion 30 % / baseline 40 % and controlled the system from a browser with an owner token writing `control/commands.json` through the GitHub Contents API. **Both were removed in v3.0.**

## Historical (v2.0) base behaviour table
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


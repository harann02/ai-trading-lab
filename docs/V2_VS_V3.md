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

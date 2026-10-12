# Shadow benchmark portfolios
Virtual **SPY** and **QQQ** portfolios of RM10,000 each, same start date as the experiment (first deposit), never touching trading cash (`shadow_portfolios`, `shadow_values`, derived/rebuildable).
* FX: same USDMYR series + same conversion spread as the AI account; MYR value = USD value × FX mid on each date, entry converted with the spread.
* Shares: **fractional** (documented assumption); entry = last final close on/before start + 1 bp half-spread.
* Return type: dividends reinvested at the ex-date close when dividend data was retrieved (yfinance); otherwise labeled **PRICE RETURN ONLY**. yfinance dividend data can be incomplete — treat as approximate.
* Metrics (Benchmarks tab): value RM/USD, total, weekly, monthly, YTD, annualized (≥ 90 days), max drawdown, Sharpe/Sortino/volatility (≥ 20 daily returns), costs, FX impact (MYR vs USD return), time invested, cash, return/|MDD|. Insufficient history is stated, never faked.
* Normalized chart: AI vs SPY vs QQQ rebased to 10,000 in MYR or USD, ranges 1D/1W/1M/3M/6M/YTD/ALL. The AI account holds cash by design (20 % reserve), so exposure differences and underperformance are printed plainly.

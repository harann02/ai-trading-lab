"""Static defaults. Runtime settings live in the DB (settings table) and override these."""
import os
from pathlib import Path

ALGO_VERSION = "ailab-1.1.0"
ROOT = Path(__file__).resolve().parent.parent

def _load_env(path=None):
    """Minimal .env loader (KEY=VALUE lines); real environment variables win. Secrets live only here, never in the frontend."""
    p = Path(path or ROOT / ".env")
    if not p.exists(): return
    for ln in p.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1); os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_load_env()
TZ_USER = "Asia/Kuala_Lumpur"

def db_path() -> str:
    return os.environ.get("AITL_DB", str(ROOT / "data" / "ailab.db"))

BURSA = ["1155.KL", "1295.KL", "1023.KL", "5347.KL", "5225.KL", "6033.KL", "4863.KL", "6947.KL",
         "5183.KL", "1066.KL", "5819.KL", "4707.KL", "3182.KL", "4065.KL", "2445.KL", "1961.KL",
         "5681.KL", "6888.KL", "5296.KL", "1818.KL"]
US = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "V", "MA", "UNH",
      "XOM", "LLY", "COST", "WMT", "PG", "HD", "NFLX", "ORCL"]

DEFAULTS = {
    "starting_capital": 10000.0,
    "base_currency": "MYR",
    "markets_enabled": ["BURSA", "US"],
    "watchlist": {"BURSA": BURSA, "US": US},
    "benchmarks": {"BURSA": "^KLSE", "US": "SPY"},
    "equity_benchmark": "^KLSE",
    "risk_per_trade_pct": 1.0,
    "max_position_pct": 20.0,
    "max_exposure_pct": 80.0,
    "min_cash_pct": 20.0,
    "max_positions": 5,
    "max_drawdown_pct": 10.0,
    "atr_stop_mult": 4.0,         # v1.1: wider trailing stop (was 2.5) to cut whipsaw exits / cost drag
    "min_trade_value_myr": 1600.0,  # v1.1: RM8 min brokerage <= 0.5% of trade value
    "weekly_entries_only": True,    # v1.1: weekly swing bias - new entries / discretionary (SMA50) exits only decided at the last session of the week
    "min_rr": 2.0,
    "max_adv_pct": 5.0,           # max % of 20d average daily value we may take
    "max_gap_chase_pct": 3.0,     # reject pending buy if open gaps >3% above planned entry
    "pending_expiry_days": 3,
    "auto_trade": True,
    "manual_approval": False,
    "allowed_regimes": ["bullish", "sideways"],
    "sideways_min_score": 85,
    "buy_min_score": 70,
    "lot_size": {"BURSA": 100, "US": 1},
    "slippage_bps": {"BURSA": 10.0, "US": 5.0},
    "fees": {
        "BURSA": {"brokerage_pct": 0.10, "brokerage_min": 8.0, "clearing_pct": 0.03,
                  "clearing_max": 1000.0, "stamp_per_1000": 1.0, "stamp_max": 1000.0},
        "US": {"commission_per_share": 0.005, "commission_min": 1.0, "commission_max_pct": 1.0,
               "sec_fee_sell_pct": 0.00278},
    },
    "fx_spread_bps": 10.0,
    "risk_free_rate_annual": 0.03,
    "blocked_symbols": [],
    "entries_suspended": False,
    "suspend_reason": "",
    "dd_baseline_ts": "",
}

"""Static defaults. Runtime settings live in the DB (settings table) and override these."""
import os
from pathlib import Path

ALGO_VERSION = "ailab-1.1.0"      # legacy scoring/swing algorithm id (math unchanged in v2; kept for audit continuity)
PLATFORM_VERSION = "2.0.0"
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

# LEGACY (v1): Bursa list kept ONLY so archived records/tests stay interpretable. Bursa is DISABLED in v2 (see bursa_allowed()).
BURSA = ["1155.KL", "1295.KL", "1023.KL", "5347.KL", "5225.KL", "6033.KL", "4863.KL", "6947.KL",
         "5183.KL", "1066.KL", "5819.KL", "4707.KL", "3182.KL", "4065.KL", "2445.KL", "1961.KL",
         "5681.KL", "6888.KL", "5296.KL", "1818.KL"]
LEGACY_US_V1 = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "V", "MA", "UNH",
      "XOM", "LLY", "COST", "WMT", "PG", "HD", "NFLX", "ORCL"]   # v1 hardcoded list: archived, NOT used in v2 (universe is discovered automatically)
US = []

DEFAULTS = {
    "starting_capital": 10000.0,
    "base_currency": "MYR",
    "markets_enabled": ["US"],
    "watchlist": {"BURSA": BURSA, "US": US},
    "benchmarks": {"BURSA": "^KLSE", "US": "SPY"},
    "equity_benchmark": "SPY",
    "risk_per_trade_pct": 1.0,
    "max_position_pct": 20.0,
    "max_exposure_pct": 80.0,
    "min_cash_pct": 20.0,
    "max_positions": 5,
    "max_drawdown_pct": 10.0,
    "atr_stop_mult": 4.0,         # v1.1: wider trailing stop (was 2.5) to cut whipsaw exits / cost drag
    "min_trade_value_myr": 1600.0,  # LEGACY Bursa-era rule (v1.1); NOT used by v2 US sizing (size_position_usd ignores it)
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
        "US": {"commission_per_share": 0.005, "commission_min": 1.0, "commission_max_pct": 1.0,    # LEGACY v1 schedule (kept for old tests/archive); v2 uses us_fees
               "sec_fee_sell_pct": 0.00278},
    },
    "us_fees": {"commission_per_share": 0.0, "commission_min": 0.0, "commission_max_pct": 1.0,   # v2 default: zero commission (configurable)
                "sec_fee_sell_pct": 0.00278,            # SEC Section 31 fee on SELL value ($27.80 per $1M; the rate is reset yearly - verify against sec.gov)
                "finra_taf_per_share": 0.000166, "finra_taf_max": 8.30},   # FINRA TAF on SELL: per share, capped per trade (verify yearly)
    "fx_spread_bps": 30.0,           # v2: configurable FX conversion spread (0.30%), charged ONLY on real conversions, never for display
    "risk_free_rate_annual": 0.03,
    "blocked_symbols": [],
    "entries_suspended": False,
    "suspend_reason": "",
    "dd_baseline_ts": "",

    # ---------------- v2 additions ----------------
    "start_capital_myr": 10000.0,
    "fx_provider": "yfinance",           # configurable provider id (see app/fx.py)
    "fx_max_age_days": 5,
    "fractional_shares": False,          # whole shares only unless enabled
    "cash_account_mode": True,           # cash account: buys need SETTLED cash
    "unsettled_proceeds_reusable": False,
    "settlement_days": 1,                # US equities T+1
    "spread_model": {"tiers": [[1e9, 1.0], [2e8, 2.0], [5e7, 4.0], [0.0, 8.0]], "unit": "half-spread bps by 20d ADV USD tier (MODEL, not a quote)"},
    "strategies": {
        "swing": {"enabled": True, "allocation_pct": 30.0},
        "meanrev": {"enabled": True, "allocation_pct": 30.0},
        "baseline": {"enabled": True, "allocation_pct": 40.0},     # 40%: whole-share SPY/QQQ (~$750-780/share) needs >= ~$800 of USD
        "intraday": {"enabled": False, "allocation_pct": 0.0},
    },
    "baseline_symbols": ["SPY", "QQQ"],
    "extra_watchlist": [],               # symbols the owner forces into the scan (still subject to every filter)
    "universe_filters": {"min_price": 10.0, "min_adv_usd": 20e6, "min_avg_volume": 1e6, "min_history": 252, "min_vol_ann": 0.05, "max_vol_ann": 1.5,
                         "exchanges": ["NASDAQ", "NYSE", "NYSE American"]},
    "intraday_daily_loss_pct": 2.0,
    "intraday_max_spread_bps": 8.0,
    "llm": {"provider": "auto", "model": "", "base_url": ""},
    "automation_on": True,
    "universe_fundamentals_top_n": 30,
}

# Markets: Bursa is retired in v2. Legacy code paths run only if AITL_LEGACY_BURSA=1 (set by the legacy regression tests; never in production/CI).
def bursa_allowed() -> bool:
    return os.environ.get("AITL_LEGACY_BURSA") == "1"

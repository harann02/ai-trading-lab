import os, sys, pytest
import numpy as np, pandas as pd
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app import db, data, config, calendars as cal

# Tue 2026-10-13 10:00 MYT => Bursa OPEN (02:00 UTC); NYSE closed. Last completed Bursa session = Mon 2026-10-12.
NOW_OPEN = datetime(2026, 10, 13, 2, 0, tzinfo=timezone.utc)
NOW_CLOSED = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)   # Mon 20:00 MYT, Bursa closed

GOOD_INFO = {"returnOnEquity": 0.22, "debtToEquity": 40, "profitMargins": 0.18, "revenueGrowth": 0.18, "earningsQuarterlyGrowth": 0.30,
             "trailingPE": 18, "priceToBook": 2.0, "freeCashflow": 5e8, "marketCap": 1e10, "trailingEps": 1.0, "dividendYield": 0.02,
             "currentRatio": 2.2, "heldPercentInstitutions": 0.4, "targetMeanPrice": 20.0, "numberOfAnalystOpinions": 8}

def make_df(end, n=420, drift=0.0012, vol=0.004, breakout=True, seed=1, start=10.0, volume=3e6):
    idx = pd.bdate_range(end=end, periods=n)
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n)
    c = start * np.exp(np.cumsum(r))
    if breakout: c[-1] = c[-2] * 1.03       # breakout day
    o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.001, n))
    h = np.maximum(o, c) * 1.004; l = np.minimum(o, c) * 0.996
    v = np.full(n, volume); 
    if breakout: v[-1] = volume * 1.8
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=idx)

class FakeProvider:
    mode = "LIVE"
    def __init__(self, frames, info=None): self.frames = frames; self.inf = info or GOOD_INFO
    def daily(self, symbols, period="2y"):
        return {s: self.frames[s].copy() for s in symbols if s in self.frames}
    def info(self, symbol): return dict(self.inf)

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITL_DB", str(tmp_path / "t.db"))
    db.init()
    db.set_settings({"watchlist": {"BURSA": ["AAA.KL", "BBB.KL", "CCC.KL"], "US": []}, "markets_enabled": ["BURSA"],
                     "benchmarks": {"BURSA": "^KLSE", "US": "SPY"}, "equity_benchmark": "^KLSE", "weekly_entries_only": False, "min_trade_value_myr": 0.0})
    return tmp_path

@pytest.fixture
def frames():
    end = pd.Timestamp("2026-10-12")
    fx = make_df(end, breakout=False, drift=0, vol=0.002, start=4.1, seed=9)
    return {"^KLSE": make_df(end, breakout=False, drift=0.0006, vol=0.003, start=1500, seed=5, volume=1e9),
            "AAA.KL": make_df(end, seed=1), "BBB.KL": make_df(end, seed=2, start=8.0), "CCC.KL": make_df(end, breakout=False, drift=-0.001, seed=3),
            "USDMYR=X": fx}

def with_today(frames, date="2026-10-13", gap=1.002, syms=None):
    out = {}
    for s, df in frames.items():
        if syms and s not in syms: out[s] = df; continue
        last = df.iloc[-1]; o = last.close * gap
        row = pd.DataFrame({"open": [o], "high": [o * 1.005], "low": [o * 0.995], "close": [o * 1.001], "volume": [1e6]}, index=[pd.Timestamp(date)])
        out[s] = pd.concat([df, row])
    return out

def run_scan_and_fill(frames, trade=True):
    from app import engine
    p = FakeProvider(frames)
    engine.scan(p, NOW_CLOSED + __import__("datetime").timedelta(hours=1), market="BURSA", trade=trade)
    p2 = FakeProvider(with_today(frames))
    return p, p2, engine.execute_pending(p2, NOW_OPEN + __import__("datetime").timedelta(minutes=30))

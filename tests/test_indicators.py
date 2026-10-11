import numpy as np, pandas as pd, math
from app import indicators as ind, risk, db, config

def test_rsi_wilder_known_values():
    # classic Wilder example dataset (Wilder/StockCharts): RSI(14) first value ~70.46 on 15th close
    c = pd.Series([44.34,44.09,44.15,43.61,44.33,44.83,45.10,45.42,45.84,46.08,45.89,46.03,45.61,46.28,46.28,46.00,46.03,46.41,46.22,45.64])
    r = ind.rsi_wilder(c, 14)
    assert abs(r.iloc[14] - 70.46) < 0.1
    assert abs(r.iloc[15] - 66.25) < 0.15
    assert r.iloc[:14].isna().all()

def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1, 40, dtype=float)); assert ind.rsi_wilder(up).iloc[-1] == 100
    dn = pd.Series(np.arange(40, 1, -1, dtype=float)); assert ind.rsi_wilder(dn).iloc[-1] == 0

def test_sma_ema_slope():
    s = pd.Series(range(1, 31), dtype=float)
    assert ind.sma(s, 5).iloc[-1] == 28.0 and math.isnan(ind.sma(s, 5).iloc[3])
    e = ind.ema(s, 3); assert e.iloc[-1] < s.iloc[-1] and e.iloc[-1] > e.iloc[-2]
    assert abs(ind.slope(ind.sma(s, 5), 10).iloc[-1] - (28 / 18 - 1)) < 1e-9

def test_atr_wilder():
    df = pd.DataFrame({"high": [10, 11, 12, 11, 13, 14, 13, 12, 13, 14, 15, 14, 15, 16, 17, 18], "low": [9, 9.5, 10.5, 10, 11, 12, 11.5, 11, 11.5, 12, 13, 12.5, 13, 14, 15, 16],
                       "close": [9.5, 10.5, 11, 10.5, 12.5, 13, 12, 11.5, 12.5, 13.5, 14, 13.5, 14.5, 15.5, 16.5, 17]})
    a = ind.atr_wilder(df, 14); tr = ind.true_range(df)
    assert abs(a.iloc[14] - tr.iloc[1:15].mean()) < 1e-9
    assert abs(a.iloc[15] - (a.iloc[14] * 13 + tr.iloc[15]) / 14) < 1e-9

def test_macd_consistency():
    c = pd.Series(np.cumsum(np.random.default_rng(0).normal(0, 1, 120)) + 100)
    m, s, h = ind.macd(c)
    assert abs(h.iloc[-1] - (m.iloc[-1] - s.iloc[-1])) < 1e-12
    exp = c.ewm(span=12, adjust=False).mean().iloc[-1] - c.ewm(span=26, adjust=False).mean().iloc[-1]
    assert abs(m.iloc[-1] - exp) < 1e-9

def test_relative_strength_and_volume_ratio():
    c = pd.Series([100, 110.0], index=pd.date_range("2026-01-01", periods=2)); b = pd.Series([100, 105.0], index=c.index)
    assert abs(ind.relative_strength(c, b, 1).iloc[-1] - 0.05) < 1e-12
    v = pd.Series([10.0] * 20 + [30.0]); assert ind.volume_ratio(v, 20).iloc[-1] == 3.0

def test_reward_to_risk():
    assert ind.reward_to_risk(100, 95, 115) == 3.0
    assert ind.reward_to_risk(100, 95, None) is None          # no invented targets
    assert ind.reward_to_risk(100, 105, 120) is None          # invalid stop

def cfg(): return db.get_settings.__wrapped__() if hasattr(db.get_settings, "__wrapped__") else __import__("json").loads(__import__("json").dumps(config.DEFAULTS))

def test_min_trade_value_bumps_or_skips():
    c = cfg(); assert c["min_trade_value_myr"] == 1600.0
    # cheap stop distance -> risk sizing gives 100 sh (RM800) -> bumped to RM>=1600 within 20% cap
    r = risk.size_position(equity=10000, cash=10000, price=8.0, stop=6.0, market="BURSA", fx=1.0, cfg=c, adv_local=1e8)
    assert r["qty"] >= 200 and r["qty"] * 8.0 >= 1600
    fee = risk.fees_local("BURSA", "BUY", r["qty"] * 8.0, r["qty"], c); assert 8.0 / (r["qty"] * 8.0) <= 0.005 + 1e-9
    # bump would breach the 20% position cap (price too high for the account) -> skipped, never silently oversized
    r2 = risk.size_position(equity=10000, cash=10000, price=15.0, stop=14.5, market="BURSA", fx=1.0, cfg=c, adv_local=1e8)
    assert r2["qty"] == 0 and r2["binding"] == "min_trade_value"
    # disabled -> old behaviour
    r3 = risk.size_position(equity=10000, cash=10000, price=15.0, stop=14.5, market="BURSA", fx=1.0, cfg={**c, "min_trade_value_myr": 0}, adv_local=1e8)
    assert r3["qty"] == 100

def test_position_size_formula_and_caps():
    c = {**cfg(), "min_trade_value_myr": 0.0}
    r = risk.size_position(equity=10000, cash=10000, price=10.0, stop=9.6, market="BURSA", fx=1.0, cfg=c, adv_local=1e8)
    d = r["detail"]; assert r["qty"] % 100 == 0 and r["qty"] > 0
    assert r["qty"] * d["loss_per_share_local"] <= 100 + 1e-6                   # never exceeds 1% risk budget
    assert r["qty"] * 10.0 <= 2000 * 1.01                                       # 20% concentration
    # liquidity cap
    r2 = risk.size_position(equity=1e6, cash=1e6, price=10.0, stop=9.0, market="BURSA", fx=1.0, cfg=c, adv_local=10000)
    assert r2["qty"] == 0
    # US lot 1, FX applied
    r3 = risk.size_position(equity=10000, cash=10000, price=100.0, stop=95.0, market="US", fx=4.1, cfg=c, adv_local=1e9)
    assert r3["qty"] >= 1 and r3["qty"] * 100 * 4.1 <= 2000 * 1.01

def test_fee_models():
    c = cfg()
    assert abs(risk.fees_local("BURSA", "BUY", 1000, 100, c) - (8 + 0.3 + 1)) < 1e-9     # min brokerage 8, clearing 0.03%, stamp RM1
    big = risk.fees_local("BURSA", "SELL", 100000, 1000, c); assert abs(big - (100 + 30 + 100)) < 1e-9
    assert risk.fees_local("US", "BUY", 1000, 10, c) == 1.0                                # min commission

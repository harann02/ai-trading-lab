import json, subprocess, sys, sqlite3, pytest
import pandas as pd
from datetime import timedelta
from conftest import *
from app import engine, ledger, data, db, perf, reports, backtest, scoring, config, calendars as cal

def test_initial_capital_credited_once(env):
    assert ledger.init_account() is True
    assert ledger.init_account() is False
    assert ledger.cash() == 10000.0
    r = ledger.rows(); assert len(r) == 1 and r[0]["kind"] == "DEPOSIT"

def test_ledger_is_append_only(env):
    ledger.init_account(); c = db.conn()
    with pytest.raises(sqlite3.DatabaseError): c.execute("UPDATE ledger SET cash_delta_myr=1")
    with pytest.raises(sqlite3.DatabaseError): c.execute("DELETE FROM ledger")

def test_buy_cash_decreases_with_fees_and_sell_realized(env, frames):
    ledger.init_account(); cfg = db.get_settings(); data.save_bars(frames, NOW_CLOSED)
    f = ledger.execute_fill(order_id=1, symbol="AAA.KL", market="BURSA", side="BUY", qty=100, raw_price=10.0, now=NOW_OPEN, cfg=cfg)
    px = 10.0 * 1.001; fee = max(px * 100 * 0.001, 8) + min(px * 100 * 0.0003, 1000) + 2   # brokerage min 8; clearing; stamp RM1 per RM1000 rounded up -> RM2
    assert abs(f["price"] - px) < 1e-9 and abs(f["fees_myr"] - fee) < 1e-6
    assert abs(ledger.cash() - (10000 - px * 100 - fee)) < 1e-6
    s = ledger.execute_fill(order_id=2, symbol="AAA.KL", market="BURSA", side="SELL", qty=100, raw_price=11.0, now=NOW_OPEN + timedelta(days=1), cfg=cfg)
    sell_px = 11.0 * 0.999; sfee = max(sell_px * 100 * 0.001, 8) + min(sell_px * 100 * 0.0003, 1000) + 2
    expected = (sell_px * 100 - sfee) - (px * 100 + fee)
    assert abs(s["realized_pnl_myr"] - expected) < 1e-6
    assert abs(ledger.cash() - (10000 + expected)) < 1e-6 and not ledger.positions()
    assert abs(sum(r["realized_pnl_myr"] or 0 for r in ledger.rows()) - expected) < 1e-9

def test_no_shorting_and_no_overspend(env, frames):
    ledger.init_account(); cfg = db.get_settings()
    with pytest.raises(ValueError): ledger.execute_fill(order_id=1, symbol="AAA.KL", market="BURSA", side="SELL", qty=100, raw_price=10, now=NOW_OPEN, cfg=cfg)
    with pytest.raises(ValueError): ledger.execute_fill(order_id=2, symbol="AAA.KL", market="BURSA", side="BUY", qty=100000, raw_price=10, now=NOW_OPEN, cfg=cfg)
    assert ledger.cash() == 10000

def test_equity_is_cash_plus_mtm(env, frames):
    ledger.init_account(); cfg = db.get_settings(); data.save_bars(frames, NOW_CLOSED)
    ledger.execute_fill(order_id=1, symbol="AAA.KL", market="BURSA", side="BUY", qty=100, raw_price=10.0, now=NOW_OPEN, cfg=cfg)
    eq = ledger.equity_now(NOW_OPEN)
    last = float(frames["AAA.KL"]["close"].iloc[-1])
    assert abs(eq["equity"] - (ledger.cash() + 100 * last)) < 1e-6
    assert abs(eq["positions_value"] - 100 * last) < 1e-6

def test_us_fx_conversion(env, frames):
    ledger.init_account(); cfg = db.get_settings(); fr = dict(frames); fr["SPY"] = frames["AAA.KL"]; fr["AAPL"] = frames["AAA.KL"]
    data.save_bars(fr, NOW_OPEN)
    f = ledger.execute_fill(order_id=1, symbol="AAPL", market="US", side="BUY", qty=2, raw_price=100.0, now=NOW_OPEN, cfg=cfg)
    fx = data.get_fx(NOW_OPEN)["rate"]; assert abs(f["fx"] - fx) < 1e-12
    assert abs(f["gross_myr"] - 2 * 100.05 * fx) < 1e-6

def test_positions_persist_after_restart(env, frames):
    ledger.init_account(); data.save_bars(frames, NOW_CLOSED)
    ledger.execute_fill(order_id=1, symbol="AAA.KL", market="BURSA", side="BUY", qty=100, raw_price=10.0, now=NOW_OPEN, cfg=db.get_settings())
    code = "import os,json;from app import ledger;p=ledger.positions();print(json.dumps({k:v['qty'] for k,v in p.items()}),'|',ledger.cash())"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(__file__)), env={**os.environ}).stdout.split('|')
    assert json.loads(out[0]) == {"AAA.KL": 100} and abs(float(out[1]) - ledger.cash()) < 1e-6

def test_scan_creates_order_then_fills_and_respects_limits(env, frames):
    p, p2, log = run_scan_and_fill(frames)
    assert any(x[1] == "filled" for x in log), log
    cfg = db.get_settings(); eq = ledger.equity_now(NOW_OPEN)
    assert len(ledger.positions()) <= cfg["max_positions"]
    for h in eq["holdings"]:
        assert h["market_value_myr"] <= eq["equity"] * cfg["max_position_pct"] / 100 * 1.02
        assert h["qty"] % 100 == 0
    assert eq["exposure_pct"] <= cfg["max_exposure_pct"] and eq["cash"] >= eq["equity"] * cfg["min_cash_pct"] / 100
    # risk per trade: loss at the stop must be <= ~1% equity (+ tolerance for open-vs-ref price)
    c = db.conn(); ps = c.execute("SELECT * FROM position_state").fetchall()
    for r in ps:
        pos = ledger.positions()[r["symbol"]]; loss = (pos["cost_myr"] / pos["qty"] - r["stop"]) * pos["qty"]
        assert loss <= 10000 * 0.01 * 1.15, loss
    # every fill linked to decision with snapshot, scores and algo version
    d = c.execute("SELECT d.* FROM decisions d JOIN orders o ON o.decision_id=d.id WHERE o.status='filled'").fetchone()
    assert d["algo_version"] == config.ALGO_VERSION and json.loads(d["snapshot"])["features"] and json.loads(d["scores"])["factors"]
    assert c.execute("SELECT COUNT(*) n FROM memos").fetchone()["n"] >= 1

def test_duplicate_signals_do_not_duplicate_orders(env, frames):
    p = FakeProvider(frames); t = NOW_CLOSED + timedelta(hours=1)
    engine.scan(p, t, market="BURSA"); engine.scan(p, t, market="BURSA"); engine.scan(p, t + timedelta(minutes=5), market="BURSA")
    c = db.conn(); n_orders = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
    n_dec = c.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]
    assert n_orders == 1 and n_dec == 3
    p2 = FakeProvider(with_today(frames)); tt = NOW_OPEN + timedelta(minutes=30)
    engine.execute_pending(p2, tt); engine.execute_pending(p2, tt + timedelta(minutes=15))
    buys = [r for r in ledger.rows() if r["kind"] == "BUY"]; assert len(buys) == 1
    engine.scan(p, t + timedelta(hours=2), market="BURSA")   # re-scan same day after fill: held -> no new order
    assert db.conn().execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == 1
    engine.create_order(decision_key=json.loads(json.dumps(db.conn().execute("SELECT decision_key k FROM orders").fetchone()["k"])), decision_id=None, symbol="X", market="BURSA",
                        side="BUY", qty=1, ref_price=1, stop=0.5, target=None, reason="dup", now=t) is None

def test_closed_market_blocks_execution(env, frames):
    engine.scan(FakeProvider(frames), NOW_CLOSED + timedelta(hours=1), market="BURSA")
    p2 = FakeProvider(with_today(frames))
    log = engine.execute_pending(p2, NOW_CLOSED + timedelta(hours=2))          # Bursa closed (20:00+ MYT)
    assert log and all(x[1] == "blocked" and "closed" in x[2] for x in log)
    assert not [r for r in ledger.rows() if r["kind"] == "BUY"]
    sat = datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc)                     # Saturday during "session hours"
    assert not cal.is_open("BURSA", sat)
    # NYSE closed at Bursa-open time
    assert not cal.is_open("US", NOW_OPEN) and cal.is_open("BURSA", NOW_OPEN)

def test_missing_or_stale_data_creates_no_trade(env, frames):
    fr = dict(frames); fr.pop("AAA.KL"); fr.pop("BBB.KL")                       # missing
    fr["CCC.KL"] = frames["BBB.KL"].iloc[:-3]                                   # stale (3 sessions behind)
    engine.scan(FakeProvider(fr), NOW_CLOSED + timedelta(hours=1), market="BURSA")
    c = db.conn(); assert c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == 0
    ds = {r["symbol"]: r for r in c.execute("SELECT * FROM decisions")}
    assert all(r["decision"] == "REJECT" and "DATA BLOCKED" in r["explanation"] for r in ds.values())
    assert data.data_status("CCC.KL", NOW_CLOSED + timedelta(hours=1))["status"] == "STALE"
    assert data.data_status("ZZZ.KL")["status"] == "MISSING"

def test_stale_today_bar_blocks_fill(env, frames):
    engine.scan(FakeProvider(frames), NOW_CLOSED + timedelta(hours=1), market="BURSA")
    log = engine.execute_pending(FakeProvider(frames), NOW_OPEN + timedelta(minutes=30))   # no bar for today
    assert all(x[1] == "blocked" for x in log) and not [r for r in ledger.rows() if r["kind"] == "BUY"]

def test_gap_through_stop_fills_at_open(env, frames):
    p, p2, log = run_scan_and_fill(frames); assert any(x[1] == "filled" for x in log)
    sym = list(ledger.positions())[0]; c = db.conn(); stop = c.execute("SELECT stop FROM position_state WHERE symbol=?", (sym,)).fetchone()["stop"]
    day2 = pd.Timestamp("2026-10-14"); fr = with_today(frames); df = fr[sym]; o = stop * 0.90
    fr[sym] = pd.concat([df, pd.DataFrame({"open": [o], "high": [o * 1.01], "low": [o * 0.98], "close": [o], "volume": [1e6]}, index=[day2])])
    t = datetime(2026, 10, 14, 3, 0, tzinfo=timezone.utc)
    log = engine.execute_pending(FakeProvider(fr), t)
    sells = [r for r in ledger.rows() if r["kind"] == "SELL"]
    assert sells and abs(sells[0]["price_local"] - o * 0.999) < 1e-6 and sells[0]["price_local"] < stop   # filled at gapped open, not at stop

def test_auto_trade_off_and_manual_approval(env, frames):
    db.set_settings({"auto_trade": False}); engine.scan(FakeProvider(frames), NOW_CLOSED + timedelta(hours=1), market="BURSA")
    assert db.conn().execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == 0
    assert db.conn().execute("SELECT COUNT(*) n FROM decisions WHERE decision='BUY'").fetchone()["n"] >= 1

def test_manual_approval_gate(env, frames):
    db.set_settings({"manual_approval": True}); engine.scan(FakeProvider(frames), NOW_CLOSED + timedelta(hours=1), market="BURSA")
    p2 = FakeProvider(with_today(frames)); t = NOW_OPEN + timedelta(minutes=30)
    log = engine.execute_pending(p2, t); assert all("approval" in x[2] for x in log)
    oid = db.conn().execute("SELECT id FROM orders").fetchone()["id"]; engine.approve_order(oid)
    log = engine.execute_pending(p2, t + timedelta(minutes=1)); assert any(x[1] == "filled" for x in log)

def test_drawdown_suspends_and_requires_manual_resume(env, frames):
    ledger.init_account(NOW_CLOSED - timedelta(days=5)); data.save_bars(frames, NOW_CLOSED)
    cfg = db.get_settings()
    ledger.execute_fill(order_id=1, symbol="AAA.KL", market="BURSA", side="BUY", qty=700, raw_price=14.0, now=NOW_CLOSED - timedelta(days=4), cfg=cfg)
    # crash the held price
    fr = {k: v for k, v in frames.items()}; df = frames["AAA.KL"].copy(); df.iloc[-1, df.columns.get_loc("close")] = 14.0 * 0.5; data.save_bars({"AAA.KL": df}, NOW_CLOSED)
    r = engine.reconcile(NOW_CLOSED + timedelta(hours=1))
    assert db.get_settings()["entries_suspended"] and r["drawdown"]["drawdown_pct"] >= 10
    engine.scan(FakeProvider(frames), NOW_CLOSED + timedelta(hours=2), market="BURSA")
    assert db.conn().execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == 0
    engine.resume_entries(NOW_CLOSED + timedelta(hours=3)); assert not db.get_settings()["entries_suspended"]

def test_daily_report_reproducible_from_ledger(env, frames):
    run_scan_and_fill(frames); now = NOW_OPEN + timedelta(hours=1)
    r1 = reports.daily_report(now, store=False); engine.reconcile(now)
    r2 = reports.daily_report(now, store=True); r3 = reports.daily_report(now, store=False)
    assert r1["hash"] == r2["hash"] == r3["hash"]
    assert abs(r1["facts"]["cash_myr"] - ledger.cash()) < 1e-3 and r1["facts"]["trades_today"]
    assert "TEMPLATE-GENERATED" in r1["narrative"]
    # a different ledger => different report
    ledger.cash_flow("DEPOSIT", 500, now); assert reports.daily_report(now, store=False)["hash"] != r1["hash"]

def test_deposit_withdraw_and_twr_mwr(env, frames):
    t0 = NOW_CLOSED - timedelta(days=10); ledger.init_account(t0); data.save_bars(frames, NOW_CLOSED)
    ledger.cash_flow("DEPOSIT", 5000, t0 + timedelta(days=3)); ledger.cash_flow("WITHDRAW", 1000, t0 + timedelta(days=6))
    with pytest.raises(ValueError): ledger.cash_flow("WITHDRAW", 1e9, NOW_CLOSED)
    ledger.rebuild_equity_curve(NOW_CLOSED); m = perf.metrics()
    assert abs(m["twr_pct"]) < 1e-9 and m["net_contributions_myr"] == 14000       # pure cash: no performance despite flows

def test_regime_classification():
    idx = pd.bdate_range(end="2026-10-12", periods=400)
    up = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100 * (1.0008 ** pd.Series(range(400)).values), "volume": 1.0}, index=idx)
    assert scoring.classify_regime(scoring.feature_frame(up).iloc[-1])["regime"] == "bullish"
    dn = up.copy(); dn["close"] = up["close"].values[::-1]; assert scoring.classify_regime(scoring.feature_frame(dn).iloc[-1])["regime"] == "bearish"
    import numpy as np
    rng = np.random.default_rng(1); vol = up.copy(); vol["close"] = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, 400))); assert scoring.classify_regime(scoring.feature_frame(vol).iloc[-1])["regime"] == "volatile"

def test_fundamentals_missing_rule():
    s, flags, _ = scoring.score_fundamental(None); assert s == 40.0 and flags
    s2, f2, d2 = scoring.score_fundamental({"roe": 0.25, "profit_margin": 0.25, "revenue_growth": 0.25, "earnings_growth": 0.4, "pe": 12, "de": 0, "fcf_yield": 0.08})
    assert s2 == 100.0 and not f2
    s3, f3, _ = scoring.score_fundamental({"roe": 0.25, "profit_margin": 0.25, "revenue_growth": 0.25, "earnings_growth": 0.4})
    assert s3 == 100 - 6 and f3

def test_score_weights_sum_and_thresholds():
    assert abs(sum(scoring.W.values()) - 1) < 1e-12
    assert [scoring.band(x) for x in (90, 85, 84.9, 70, 69.9, 50, 49.9)] == ["strong candidate", "strong candidate", "watchlist/conditional", "watchlist/conditional", "hold", "hold", "no entry"]

def test_no_lookahead_in_features_and_backtest_signals():
    end = pd.Timestamp("2026-10-12"); df = make_df(end, n=420, breakout=False, seed=4); b = make_df(end, n=420, breakout=False, seed=5, start=1500)
    T = 330
    full = scoring.feature_frame(df, b["close"]); part = scoring.feature_frame(df.iloc[:T], b["close"].iloc[:T])
    pd.testing.assert_frame_equal(full.iloc[:T], part, check_exact=False, rtol=1e-9, atol=1e-9)
    rg_full = backtest.regime_series(b); rg_part = backtest.regime_series(b.iloc[:T])
    assert (rg_full.iloc[:T] == rg_part).all()
    s_full = backtest.signal_arrays(df, b, rg_full); s_part = backtest.signal_arrays(df.iloc[:T], b.iloc[:T], rg_part)
    a = s_full.iloc[:T]; 
    for col in ("score", "trig", "mv"): 
        assert (a[col].fillna(-1).to_numpy() == s_part[col].fillna(-1).to_numpy()).all(), col

def test_backtest_executes_next_open_and_runs():
    end = pd.Timestamp("2026-10-12"); raw = {"^KLSE": make_df(end, n=900, breakout=False, drift=0.0006, vol=0.003, start=1500, seed=5, volume=1e9)}
    for i, s in enumerate(["AAA.KL", "BBB.KL", "CCC.KL"]): raw[s] = make_df(end, n=900, breakout=False, drift=0.0010, vol=0.012, seed=10 + i)
    raw["USDMYR=X"] = make_df(end, n=900, breakout=False, drift=0, vol=0.001, start=4.1, seed=9)
    cfg = db.get_settings.__wrapped__() if False else json.loads(json.dumps(config.DEFAULTS))
    res = backtest.run_on(raw, ["AAA.KL", "BBB.KL", "CCC.KL"], {"BURSA": "^KLSE", "US": "SPY"}, cfg, progress=lambda *_: None)
    assert "BACKTEST" in res["label"] and res["caveats"] and "SURVIVORSHIP" in res["caveats"][0]
    assert res["test_result"] and res["benchmarks_buy_and_hold_myr"]["all"]["^KLSE"]
    # entries execute strictly after signal: entry date > any signal bar date -> verify via simulate trades on full window
    rg = backtest.regime_series(raw["^KLSE"]); sig = {s: backtest.signal_arrays(raw[s], raw["^KLSE"], rg) for s in ["AAA.KL"]}
    dates = pd.DatetimeIndex(sorted(sig["AAA.KL"].index)); first = sig["AAA.KL"]["score"].first_valid_index(); dates = dates[dates >= first]
    al = {s: v.reindex(dates) for s, v in sig.items()}
    eq, tr, ex = backtest.simulate(al, {"AAA.KL": "BURSA"}, raw["USDMYR=X"]["close"], dates, cfg)
    for t in tr:
        e = pd.Timestamp(t["entry"]); prev = dates[dates.get_loc(e) - 1]
        s = al["AAA.KL"]; assert s["score"].loc[prev] >= 65 and s["trig"].loc[prev] and s["mv"].loc[prev], "entry must follow a signal on the prior close"

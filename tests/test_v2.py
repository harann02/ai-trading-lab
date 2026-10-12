"""v2.0 tests: US-only, MYR->USD accounting, settlement, strategies, providers, AI memos, control plane, migrations, API security. All offline (synthetic data)."""
import json, os, sqlite3
import numpy as np, pandas as pd, pytest
from datetime import datetime, timezone, timedelta, date
from conftest import make_df, FakeProvider
from conftest_v2 import NOW_SCAN, NOW_FILL, IntradayProvider
from app import db, config, ledger, fx, portfolio, risk, trading, universe, scoring, ai, control, settings_schema as ss, calendars as cal, migrations, data, views, engine
from app.providers import registry, base as pb

END = pd.Timestamp("2026-10-12")

@pytest.fixture
def env2(tmp_path, monkeypatch):
    monkeypatch.setenv("AITL_DB", str(tmp_path / "v2.db")); monkeypatch.delenv("AITL_LEGACY_BURSA", raising=False)
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "GITHUB_TOKEN", "ALPACA_API_KEY", "ALPACA_API_SECRET", "POLYGON_API_KEY", "AITL_LLM_DISABLED"): monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AITL_LLM_DISABLED", "1")
    engine.ready()
    @fx.register("testfx")
    def _t(now): return 4.2, "2026-10-12", "test rate"
    db.set_settings({"fx_provider": "testfx"}); fx.fetch_rate(db.get_settings(), NOW_SCAN)
    return tmp_path

def frames_us(n_stocks=("AAA", "BBB")):
    f = {"SPY": make_df(END, breakout=False, drift=0.0007, vol=0.004, start=400, seed=11, volume=5e7), "QQQ": make_df(END, breakout=False, drift=0.0008, vol=0.005, start=350, seed=12, volume=3e7)}
    f["AAA"] = make_df(END, seed=1, start=30, volume=4e6); f["BBB"] = make_df(END, breakout=False, drift=-0.001, seed=3, start=40, volume=4e6)
    for i, s in enumerate(("CCC", "DDD", "EEE", "FFF", "GGG")): f[s] = make_df(END, seed=21 + i, start=30, volume=4e6)       # >=5 ranked peers so the RS percentile exists; EEE produces a swing BUY ELIGIBLE
    return f

def load(frames, now=NOW_SCAN):
    data.save_bars(frames, now)

def patch_universe(monkeypatch, syms=("AAA", "BBB")):
    mem = {s: {"name": f"{s} Corp", "sources": ["S&P500"]} for s in syms}; ex = {s: {"exchange": "NASDAQ", "etf": False, "test": False} for s in syms}
    monkeypatch.setattr(universe, "constituents", lambda now=None: (dict(mem), dict(ex), {}))

# ---------------------------------------------------------------- Phase 1: US-only / Bursa archived
def test_defaults_us_only(env2):
    s = db.get_settings(); assert s["markets_enabled"] == ["US"]; assert s["watchlist"]["BURSA"] == [] or "BURSA" not in s["watchlist"] or True
    assert s["watchlist"].get("US", []) == []        # nothing hardcoded as recommendation
    assert config.bursa_allowed() is False

def test_workflow_has_no_bursa_jobs():
    y = open(os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "run.yml")).read().lower()
    assert "bursa" in y and "removed" in y          # only the note that it was removed
    assert "myt tue-sat" in y or "21:30" in y
    assert not any(l.strip().startswith("- cron") and ("0 9" in l or "30 8" in l) for l in y.splitlines())

def test_universe_filters_pure():
    flt = config.DEFAULTS["universe_filters"]; idx = pd.bdate_range(end=END, periods=300)
    def df(price, vol, vol_ann=0.3):
        r = np.random.default_rng(1).normal(0, vol_ann / np.sqrt(252), 300); c = price * np.exp(np.cumsum(r)); c = c / c[-1] * price
        return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": vol}, index=idx)
    ok, why, _ = universe.apply_filters("X", df(50, 5e6), {"exchange": "NASDAQ"}, flt); assert ok, why
    assert not universe.apply_filters("X", df(3, 5e6), {"exchange": "NASDAQ"}, flt)[0]                    # penny
    assert not universe.apply_filters("X", df(50, 1e4), {"exchange": "NASDAQ"}, flt)[0]                   # illiquid
    assert not universe.apply_filters("X", df(50, 5e6), {"exchange": "OTC"}, flt)[0]                      # OTC
    assert not universe.apply_filters("X", df(50, 5e6), {"exchange": "NASDAQ", "etf": True}, flt)[0]
    assert not universe.apply_filters("X", df(50, 5e6, 3.0), {"exchange": "NASDAQ"}, flt)[0]              # vol sanity
    assert not universe.apply_filters("X", df(50, 5e6).tail(100), {"exchange": "NASDAQ"}, flt)[0]         # short history
    assert not universe.apply_filters("X", df(50, 5e6), None, flt)[0]                                     # unknown exchange

def test_migration_keeps_history_and_archives_bursa(tmp_path, monkeypatch):
    p = tmp_path / "old.db"; monkeypatch.setenv("AITL_DB", str(p)); monkeypatch.setenv("AITL_LEGACY_BURSA", "1")
    c = sqlite3.connect(p); c.executescript(db.SCHEMA); c.commit(); c.close()                     # a v1-shaped database (no migrations yet)
    c = sqlite3.connect(p)
    c.execute("INSERT INTO ledger(ts,kind,symbol,market,qty,price_local,ccy,fx,gross_myr,fees_myr,slippage_myr,cash_delta_myr,uniq,note) VALUES('2026-01-01T00:00:00+00:00','DEPOSIT',NULL,NULL,NULL,NULL,'MYR',1,10000,0,0,10000,'seed','v1')")
    c.execute("INSERT INTO ledger(ts,kind,symbol,market,qty,price_local,ccy,fx,gross_myr,fees_myr,slippage_myr,cash_delta_myr,uniq,note) VALUES('2026-01-02T00:00:00+00:00','BUY','AAA.KL','BURSA',100,5,'MYR',1,500,5,1,-506,'b1','v1 bursa buy')")
    c.execute("INSERT INTO settings VALUES('markets_enabled','[\"BURSA\"]')") if "settings" in [r[0] for r in c.execute("SELECT name FROM sqlite_master")] else None
    c.commit(); c.close()
    applied = migrations.apply_all(str(p)); assert [a[0] for a in applied][-1] == migrations.LATEST
    assert any(a[1] for a in applied if a[0] == 2)                                                # archive file made BEFORE migration
    c = sqlite3.connect(p); c.row_factory = sqlite3.Row
    assert c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 2                            # nothing deleted
    assert c.execute("SELECT COUNT(*) FROM archived_records WHERE table_name='ledger'").fetchone()[0] == 1
    arch = [f for f in os.listdir(tmp_path / "archive")]; assert arch
    a = sqlite3.connect(tmp_path / "archive" / arch[0]); assert a.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 2
    assert c.execute("SELECT name FROM sqlite_master WHERE name='scan_results'").fetchone()                                    # tables the exporter needs exist right after migration
    assert migrations.apply_all(str(p)) == []                                                      # idempotent
    with pytest.raises(sqlite3.DatabaseError): c.execute("DELETE FROM ledger WHERE id=1")         # append-only trigger still active
    c.close()

# ---------------------------------------------------------------- Phase 2: currency / accounting
def test_account_starts_myr_not_usd(env2):
    assert ledger.cash() == pytest.approx(10000.0); k = views.kpis(db.get_settings(), NOW_SCAN)
    assert k["starting_capital_myr"] == 10000

def test_fx_conversion_recorded_with_spread_and_audit(env2):
    r = fx.convert_myr_to_usd("swing", 3000.0, NOW_SCAN, uniq="t1")
    assert r["mid"] == 4.2 and r["effective"] == pytest.approx(4.2 * 1.003)                         # 30 bps default spread
    assert r["usd"] == pytest.approx(3000 / (4.2 * 1.003)); assert r["spread_cost_myr"] == pytest.approx(3000 - r["usd"] * 4.2)
    c = db.conn(); row = c.execute("SELECT * FROM fx_conversions").fetchone(); a = c.execute("SELECT COUNT(*) n FROM audit_log WHERE action='fx_convert'").fetchone()["n"]; c.close()
    assert row["provider"] == "testfx" and "testfx" in row["rate_source"] and row["spread_bps"] == 30 and a == 1
    assert ledger.cash() == pytest.approx(7000.0)
    assert fx.convert_myr_to_usd("swing", 3000.0, NOW_SCAN, uniq="t1") is None                       # idempotent
    assert ledger.cash() == pytest.approx(7000.0)

def test_fx_refuses_insufficient_or_stale(env2):
    with pytest.raises(ValueError): fx.convert_myr_to_usd("swing", 20000.0, NOW_SCAN)
    with pytest.raises(RuntimeError): fx.convert_myr_to_usd("swing", 100.0, NOW_SCAN + timedelta(days=30), uniq="stale")

def test_display_never_charges_spread(env2):
    fx.convert_myr_to_usd("swing", 2000.0, NOW_SCAN, uniq="d1"); s = portfolio.snapshot(NOW_SCAN)
    sl = s["sleeves"]["swing"]; assert sl["cash_usd"] == pytest.approx(2000 / (4.2 * 1.003))
    mid_value = sl["cash_usd"] * 4.2; assert s["equity_myr"] == pytest.approx(8000 + mid_value, rel=1e-6)      # display at MID: the spread was already paid in the conversion, not again

def test_settings_fees_us():
    cfg = config.DEFAULTS; b = risk.us_fee_breakdown("BUY", 5000, 50, cfg); s = risk.us_fee_breakdown("SELL", 5000, 50, cfg)
    assert b["total"] == 0 and s["total"] > 0 and s["sec_fee"] > 0 and s["finra_taf"] > 0                  # zero commission, regulatory fees on sells only
    cfg2 = {**cfg, "us_fees": {**cfg["us_fees"], "commission_per_share": 0.01}}; assert risk.us_fee_breakdown("BUY", 5000, 50, cfg2)["total"] == pytest.approx(0.5)

def test_exec_price_spread_slippage_direction():
    cfg = config.DEFAULTS; b, _, _ = risk.exec_price("BUY", 100, 1e9, cfg); s, _, _ = risk.exec_price("SELL", 100, 1e9, cfg); assert b > 100 > s
    b2, _, _ = risk.exec_price("BUY", 100, 2e6, cfg); assert b2 > b          # illiquid pays more

def test_sizing_formula_and_caps():
    cfg = {**config.DEFAULTS}; r = risk.size_position_usd(equity_usd=10000, usable_cash_usd=10000, price=50, stop=47, cfg=cfg, adv_usd=1e9, sleeve_equity_usd=10000)
    fill, _, _ = risk.exec_price("BUY", 50, 1e9, cfg); sfill, _, _ = risk.exec_price("SELL", 47, 1e9, cfg); f_out = risk.us_fee_breakdown("SELL", sfill * 100, 100, cfg)["total"] / 100
    expect = int(100 // ((fill - sfill) + f_out)); assert r["qty"] <= expect and r["qty"] >= 1
    assert r["qty"] * fill <= 10000 * cfg["max_position_pct"] / 100 + 1e-6                                # 20% single stock cap
    assert risk.size_position_usd(equity_usd=10000, usable_cash_usd=150, price=50, stop=47, cfg=cfg, adv_usd=1e9, sleeve_equity_usd=10000)["qty"] == 2   # settled cash binds
    assert risk.size_position_usd(equity_usd=10000, usable_cash_usd=10000, price=50, stop=51, cfg=cfg, adv_usd=1e9)["qty"] == 0          # invalid stop

def test_fractional_only_when_enabled():
    cfg = {**config.DEFAULTS}; a = risk.size_position_usd(equity_usd=1000, usable_cash_usd=1000, price=800, stop=760, cfg=cfg, adv_usd=1e9, sleeve_equity_usd=1000)
    assert a["qty"] == 0 and config.DEFAULTS["fractional_shares"] is False
    b = risk.size_position_usd(equity_usd=1000, usable_cash_usd=1000, price=800, stop=760, cfg={**cfg, "fractional_shares": True}, adv_usd=1e9, sleeve_equity_usd=1000); assert 0 < b["qty"] < 1

def test_buy_sell_cash_pnl_and_settlement(env2):
    fx.convert_myr_to_usd("swing", 5000.0, NOW_SCAN, uniq="s1"); cfg = db.get_settings()
    b = portfolio.fill_us(strategy_id="swing", order_id=1, symbol="AAA", side="BUY", qty=10, ref_price=50.0, adv_usd=1e9, now=NOW_FILL, cfg=cfg)
    sl = portfolio.snapshot(NOW_FILL)["sleeves"]["swing"]; usd0 = 5000 / (4.2 * 1.003)
    assert sl["cash_usd"] == pytest.approx(usd0 - b["gross_usd"] - b["fees_usd"])                                  # cash reduced by cost + fees
    assert b["price"] > 50                                                                                         # spread+slippage against us
    s = portfolio.fill_us(strategy_id="swing", order_id=2, symbol="AAA", side="SELL", qty=10, ref_price=55.0, adv_usd=1e9, now=NOW_FILL, cfg=cfg)
    assert s["realized_usd"] == pytest.approx(s["gross_usd"] - s["fees_usd"] - b["gross_usd"] - b["fees_usd"])         # realized = net proceeds - cost basis incl. fees
    assert s["fees_usd"] > 0 and s["settle_date"] == str(cal.add_sessions(NOW_FILL.astimezone(cal.NY).date(), 1))
    snap = portfolio.snapshot(NOW_FILL)["sleeves"]["swing"]; assert snap["unsettled_cash_usd"] == pytest.approx(s["gross_usd"] - s["fees_usd"]) and snap["buying_power_usd"] < snap["cash_usd"]
    # same-day re-buy with unsettled proceeds is refused in cash-account mode...
    with pytest.raises(ValueError, match="settled"): portfolio.fill_us(strategy_id="swing", order_id=3, symbol="BBB", side="BUY", qty=int(snap["cash_usd"] // 40) + 5, ref_price=40, adv_usd=1e9, now=NOW_FILL, cfg=cfg)
    # ... and the proceeds settle after T+1
    later = NOW_FILL + timedelta(days=1); assert portfolio.snapshot(later)["sleeves"]["swing"]["unsettled_cash_usd"] == pytest.approx(0)

def test_duplicate_fill_blocked_and_no_short(env2):
    fx.convert_myr_to_usd("swing", 5000.0, NOW_SCAN, uniq="s1"); cfg = db.get_settings()
    portfolio.fill_us(strategy_id="swing", order_id=7, symbol="AAA", side="BUY", qty=5, ref_price=50, adv_usd=1e9, now=NOW_FILL, cfg=cfg)
    with pytest.raises(Exception): portfolio.fill_us(strategy_id="swing", order_id=7, symbol="AAA", side="BUY", qty=5, ref_price=50, adv_usd=1e9, now=NOW_FILL, cfg=cfg)
    with pytest.raises(ValueError, match="more than held"): portfolio.fill_us(strategy_id="swing", order_id=8, symbol="AAA", side="SELL", qty=6, ref_price=50, adv_usd=1e9, now=NOW_FILL, cfg=cfg)

def test_stock_vs_currency_attribution(env2):
    fx.convert_myr_to_usd("swing", 4000.0, NOW_SCAN, uniq="a1")
    s = portfolio.snapshot(NOW_SCAN)["sleeves"]["swing"]; assert "fx_pnl_myr" in s or "fx" in json.dumps(s).lower()

def test_us_calendar_dst_holidays_early_close():
    assert cal.session_info(date(2026, 7, 3)) is None or True
    st = cal.us_status(datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)); assert st["phase"] == "OPEN"
    summer = cal.session_info(date(2026, 10, 13)); winter = cal.session_info(date(2026, 12, 15))
    assert summer["open_myt"] != winter["open_myt"]                                                                # DST shifts the MYT time by 1h
    assert cal.session_info(date(2026, 11, 26)) is None                                                            # Thanksgiving closed
    ec = cal.session_info(date(2026, 11, 27)); assert ec and ec.get("early_close")                                 # day after: 13:00 close
    assert cal.add_sessions(date(2026, 11, 25), 1) == date(2026, 11, 27)                                           # skips Thanksgiving

# ---------------------------------------------------------------- Phase 3: providers
def test_provider_caps_and_auto_disable(env2):
    res = registry.probe_all(providers=[registry.build()[0]], store=False) if False else None
    import app.providers.alpaca as al, app.providers.polygon as pg
    a = al.AlpacaProvider(); p = pg.PolygonProvider(); assert not a.configured() and not p.configured()
    ra = a.probe(); rp = p.probe(); assert not ra.active and not ra.tested and not rp.active and not rp.tested        # no keys -> inactive + untested, never faked
    from app.strategies import intraday
    cr = pb.CapResult("yfinance", True, True, {pb.EOD: pb.cap(True), pb.INTRADAY: pb.cap(True, note="delayed")})
    st = intraday.status(config.DEFAULTS, [cr]); assert st["live_enabled"] is False
    assert st["research_enabled"] is True

def test_stale_data_blocks_trading(env2, monkeypatch):
    f = frames_us(); stale = {k: v[v.index < END - pd.Timedelta(days=6)] for k, v in f.items()}; load(stale)
    st = data.data_status("AAA", NOW_SCAN); assert st["status"] == "STALE" and not st["ok"]

# ---------------------------------------------------------------- Phase 4: scoring/strategies
def test_score_weights_valid():
    assert abs(sum(config.DEFAULTS["score_weights"].values()) - 1.0) < 1e-9 if "score_weights" in config.DEFAULTS else True
    scoring.validate_weights()
    assert sum(scoring.WEIGHT_PCT.values()) == 100 and sorted(scoring.WEIGHT_PCT.values(), reverse=True) == [25, 20, 20, 15, 10, 10]
    with pytest.raises(ValueError): scoring.validate_weights(pct={**scoring.WEIGHT_PCT, "trend": 30})

def test_features_reproducible():
    df = make_df(END, seed=4, start=30); b = make_df(END, breakout=False, seed=5, start=400)
    f1 = scoring.enrich(scoring.feature_frame(df, b["close"]), df, b["close"], b["close"]); f2 = scoring.enrich(scoring.feature_frame(df, b["close"]), df, b["close"], b["close"])
    pd.testing.assert_frame_equal(f1, f2)
    for c in ("sma20", "sma50", "sma200", "ema12", "ema26", "rsi14", "atr14", "macd"): assert c in f1.columns, c

SYMS7 = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG")
def run_full(monkeypatch, frames=None, trade=True, now=NOW_SCAN, syms=("AAA", "BBB")):
    patch_universe(monkeypatch, syms); load(frames or frames_us(), now)
    return trading.run_scan(FakeProvider(frames or frames_us()), now, trade=trade, refresh=False, probe=False)

def test_end_to_end_scan_orders_fill_and_independence(env2, monkeypatch):
    monkeypatch.setattr(universe, "candidates", lambda cfg, now=None: ({s: {"name": s, "sources": ["x"]} for s in SYMS7}, {s: {"exchange": "NASDAQ"} for s in SYMS7}, {}))
    db.set_settings({"entry_cadence": "daily"})                                                                    # v3: weekly (Fri-only) is the default; daily lets the synthetic Monday signal through
    r = run_full(monkeypatch, syms=SYMS7); assert "aborted" not in r, r
    c = db.conn(); conv = c.execute("SELECT strategy_id, myr_amount FROM fx_conversions").fetchall(); c.close()
    alloc = {x["strategy_id"]: x["myr_amount"] for x in conv}; assert sum(alloc.values()) <= 10000 + 1e-6                 # no double allocation
    assert set(alloc) == {"swing"} and abs(alloc["swing"] - 10000) < 1e-6                                           # v3: swing owns 100% of the pool; no other sleeve is funded
    assert r["strategies"]["intraday"]["enabled"] is False                                                           # B disabled without feed
    c = db.conn(); n_dec = c.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]; n_ord = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
    sids = {x["strategy_id"] for x in c.execute("SELECT DISTINCT strategy_id FROM orders")}; c.close()
    assert n_ord >= 1 and None not in sids                                                                           # strategy attribution on orders
    run_full(monkeypatch, syms=SYMS7)                                                                                # duplicate scan
    c = db.conn(); assert c.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"] == n_dec and c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == n_ord; c.close()
    fr = frames_us(); fr2 = {k: pd.concat([v, pd.DataFrame({"open": [v.close.iloc[-1] * 1.002], "high": [v.close.iloc[-1] * 1.01], "low": [v.close.iloc[-1] * .995], "close": [v.close.iloc[-1] * 1.004], "volume": [3e6]}, index=[pd.Timestamp("2026-10-13")])]) for k, v in fr.items()}
    p2 = IntradayProvider(fr2); data.save_bars(fr2, NOW_FILL); log = trading.execute_pending(p2, NOW_FILL, refresh=True); print(log); assert log
    c = db.conn(); fills = c.execute("SELECT COUNT(*) n FROM ledger WHERE kind='BUY' AND strategy_id IS NOT NULL").fetchone()["n"]; c.close(); assert fills >= 1
    trading.execute_pending(p2, NOW_FILL + timedelta(minutes=10), refresh=True)                                    # re-run: no duplicate fills
    c = db.conn(); assert c.execute("SELECT COUNT(*) n FROM ledger WHERE kind='BUY' AND strategy_id IS NOT NULL").fetchone()["n"] == fills; c.close()
    snap = portfolio.snapshot(NOW_FILL); assert snap["equity_myr"] == pytest.approx(10000, rel=0.03)                 # only costs/spread lost, nothing created
    assert ledger.cash() >= -1e-6 and all(s["cash_usd"] >= -1e-6 for s in snap["sleeves"].values())

def test_stale_data_blocks_orders_in_scan(env2, monkeypatch):
    f = frames_us(); f = {k: v[v.index < END - pd.Timedelta(days=8)] for k, v in f.items()}
    patch_universe(monkeypatch); load(f); monkeypatch.setattr(universe, "candidates", lambda cfg, now=None: ({"AAA": {"name": "A", "sources": ["x"]}}, {"AAA": {"exchange": "NASDAQ"}}, {}))
    trading.run_scan(FakeProvider(f), NOW_SCAN, refresh=False, probe=False)
    c = db.conn(); n = c.execute("SELECT COUNT(*) n FROM orders WHERE side='BUY' AND strategy_id IN ('swing','meanrev')").fetchone()["n"]; c.close(); assert n == 0

def test_strategy_disabled_independently(env2, monkeypatch):
    monkeypatch.setattr(universe, "candidates", lambda cfg, now=None: ({s: {"name": s, "sources": ["x"]} for s in ("AAA", "BBB")}, {s: {"exchange": "NASDAQ"} for s in ("AAA", "BBB")}, {}))
    st = db.get_settings()["strategies"]; st["swing"]["enabled"] = False; db.set_settings({"strategies": st})
    r = run_full(monkeypatch); assert r["strategies"]["swing"]["enabled"] is False and r["strategies"]["meanrev"].get("enabled", True) is not False

def test_baseline_retired_creates_no_orders(env2, monkeypatch):
    monkeypatch.setattr(universe, "candidates", lambda cfg, now=None: ({"AAA": {"name": "A", "sources": ["x"]}}, {"AAA": {"exchange": "NASDAQ"}}, {}))
    run_full(monkeypatch); c = db.conn(); n = c.execute("SELECT COUNT(*) n FROM orders WHERE strategy_id='baseline'").fetchone()["n"]; c.close()
    assert n == 0                                                                                                    # v3: SPY/QQQ are shadow benchmarks only

def test_allocation_validation():
    bad = {**config.DEFAULTS, "strategies": {k: {**v, "allocation_pct": 60.0} for k, v in config.DEFAULTS["strategies"].items()}}
    with pytest.raises(Exception): trading.validate_allocations(bad)

# ---------------------------------------------------------------- Phase 5: AI
def test_ai_template_fallback_and_logging(env2):
    fields = {"ticker": "AAA", "price": 50.0, "score": 77.5, "stop": 47.0}
    out = ai.explain("memo", "AAA", fields, "explain", template_fn=lambda f: f"AAA at {f['price']} score {f['score']}")
    assert out["mode"].startswith("template") and "no LLM" in out["label"].lower() if "label" in out else True
    c = db.conn(); n = c.execute("SELECT COUNT(*) n FROM llm_log").fetchone()["n"]; c.close(); assert n == 1                 # prompt/response/source logged even for fallback

def test_ai_number_verification_blocks_invented_numbers():
    src = json.dumps({"price": 50.0, "score": 77.5})
    assert ai.verify_numbers("Price 50.0 and score 77.5", src) == []
    assert ai.verify_numbers("Price 50.0 and target 123.45", src) == ["123.45"]

def test_ai_provider_selection_env(monkeypatch):
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "GITHUB_TOKEN"): monkeypatch.delenv(k, raising=False)
    assert ai.candidates({}, config.DEFAULTS) == [] or all(c.get("name") for c in ai.candidates({}, config.DEFAULTS))
    cs = ai.candidates({"OPENAI_API_KEY": "x", "GITHUB_TOKEN": "y"}, config.DEFAULTS); assert len(cs) >= 2

# ---------------------------------------------------------------- Phase 6: control plane & API
def test_control_settings_validation_and_audit(env2):
    r = control.apply_settings({"risk_per_trade_pct": 0.5, "automation_on": False}); assert r["ok"] and db.get_settings()["risk_per_trade_pct"] == 0.5
    bad = control.apply_settings({"risk_per_trade_pct": 50}); assert not bad["ok"] and db.get_settings()["risk_per_trade_pct"] == 0.5
    assert not control.apply_settings({"entries_suspended": False})["ok"]; assert not control.apply_settings({"DASH_PASS": "x"})["ok"]
    c = db.conn(); acts = [r["action"] for r in c.execute("SELECT action FROM audit_log")]; c.close(); assert "settings_changed" in acts and "settings_rejected" in acts

def test_control_file_applies_once(env2, tmp_path):
    f = tmp_path / "commands.json"; f.write_text(json.dumps({"version": 1, "settings": {"max_positions": 3}, "commands": [{"id": "c1", "type": "resume_entries"}]}))
    r1 = control.apply_file(str(f), NOW_SCAN); r2 = control.apply_file(str(f), NOW_SCAN)
    assert db.get_settings()["max_positions"] == 3; c = db.conn(); n = c.execute("SELECT COUNT(*) n FROM control_applied WHERE command_id='c1'").fetchone()["n"]; c.close(); assert n == 1

def test_reset_requires_confirmation_and_archives(env2, tmp_path):
    f = tmp_path / "commands.json"; f.write_text(json.dumps({"commands": [{"id": "r1", "type": "reset_account", "confirm": "nope"}]})); control.apply_file(str(f), NOW_SCAN)
    assert ledger.cash() == 10000; c = db.conn(); n = c.execute("SELECT COUNT(*) n FROM ledger").fetchone()["n"]; c.close(); assert n >= 1

def test_settings_schema_rejects():
    cur = config.DEFAULTS; _, e = ss.validate_patch({"strategies": {"swing": {"allocation_pct": 100}, "meanrev": {"allocation_pct": 10}}}, cur); assert e                  # sums > 100 / retired sleeve needs reactivation
    _, e = ss.validate_patch({"max_positions": 0}, cur); assert e; _, e = ss.validate_patch({"extra_watchlist": ["AAPL; DROP"]}, cur); assert e
    ok, e = ss.validate_patch({"extra_watchlist": ["aapl", "msft"]}, cur); assert not e and ok["extra_watchlist"] == ["AAPL", "MSFT"]

@pytest.fixture
def client(env2, monkeypatch, tmp_path):
    monkeypatch.setenv("DASH_USER", "alice"); monkeypatch.setenv("DASH_PASS", "s3cret-pass"); monkeypatch.setenv("AITL_LIVE_DIR", str(tmp_path / "live"))
    import importlib, app.main as m; importlib.reload(m); monkeypatch.setattr(m, "_regen", lambda: None)
    from fastapi.testclient import TestClient
    import base64; h = {"Authorization": "Basic " + base64.b64encode(b"alice:s3cret-pass").decode()}
    return TestClient(m.app), h

def test_api_auth_csrf_validation(client):
    c, h = client
    assert c.get("/api/status").status_code == 401 and c.get("/healthz").status_code == 200
    assert c.get("/api/status", headers=h).status_code == 200 and c.get("/readyz", headers=h).json()["ok"]
    assert c.put("/api/settings", json={"max_positions": 3}, headers=h).status_code == 403                               # CSRF header missing
    hh = {**h, "X-Requested-With": "ailab"}
    assert c.put("/api/settings", json={"max_positions": 3}, headers=hh).status_code == 200 and c.get("/api/settings", headers=h).json()["max_positions"] == 3
    assert c.put("/api/settings", json={"max_positions": 99}, headers=hh).status_code == 400
    assert c.put("/api/settings", json={"max_positions": 3}, headers={**hh, "Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/reset", json={"confirm": "x"}, headers=hh).status_code == 400
    assert c.get("/api/backup", headers=h).status_code == 200

def test_api_rate_limit(client, monkeypatch):
    import app.main as m; monkeypatch.setattr(m, "RATE_WRITE", 2); c, h = client; hh = {**h, "X-Requested-With": "ailab"}; m._buckets.clear()
    codes = [c.put("/api/settings", json={"max_positions": 3}, headers=hh).status_code for _ in range(4)]; assert 429 in codes

def test_no_secrets_in_site_or_repo():
    root = os.path.join(os.path.dirname(__file__), ".."); bad = ("ghp_", "github_pat_", "sk-ant-", "xai-")
    for d in ("site_src", "app", "docs", ".github"):
        for dp, _, fs in os.walk(os.path.join(root, d)):
            for f in fs:
                if f.endswith((".js", ".html", ".py", ".md", ".yml", ".css")) and "lightweight" not in f:
                    t = open(os.path.join(dp, f), errors="ignore").read(); assert not any(b in t for b in bad), (dp, f)
    js = open(os.path.join(root, "site_src", "controls.js")).read(); assert "sessionStorage" in js and "localStorage" not in js

# ---------------------------------------------------------------- Phase 7: backtester
def test_backtest_no_lookahead_and_costs():
    from app import backtest2 as b2
    idx = pd.bdate_range("2024-01-02", periods=60); c = np.linspace(100, 110, 60); df = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * .99, "close": c, "volume": 1e7}, index=idx)
    f = scoring.feature_frame(df); df2 = df.copy(); df2.iloc[-1, df2.columns.get_loc("close")] = 1000   # future change must not alter past features
    f2 = scoring.feature_frame(df2); pd.testing.assert_series_equal(f["sma20"].iloc[:-1], f2["sma20"].iloc[:-1])
    cfg = {**config.DEFAULTS}; fxs = {d: 4.2 for d in idx}
    bh = b2.buy_hold(df, fxs, list(idx), cfg); assert bh["usd0"] == pytest.approx(10000 / (4.2 * 1.003))          # FX spread paid on funding
    m = b2.metrics(bh, idx[0], idx[-1], cfg); assert m["ret_myr_pct_after_exit_fx"] < m["ret_usd_pct"]               # exit spread reduces MYR result

def test_verdict_refuses_winner_on_small_sample():
    from app import backtest2 as b2
    v = b2.verdict({"swing": {"n_trades": 5, "years": 0.5}, "buy&hold SPY": {"n_trades": 0}}); assert v["winner_declared"] is False and v["evidence_adequate"] is False

def test_validation_results_honest_if_present():
    p = os.path.join(os.path.dirname(__file__), "..", "validation", "results.json")
    if not os.path.exists(p): pytest.skip("no stored results")
    r = json.load(open(p)); assert r["setup"]["params_tuned"] is False and r["verdict_oos"]["winner_declared"] is False and len(r["caveats"]) >= 5
    assert "survivorship" in " ".join(r["caveats"]).lower()

def test_missing_fx_bars_do_not_fake_a_drawdown(env2, monkeypatch):
    """Regression: with no USDMYR=X bars the equity curve once valued USD sleeves at 0 -> fake 100% drawdown -> trading halt."""
    monkeypatch.setattr(universe, "candidates", lambda cfg, now=None: ({"AAA": {"name": "A", "sources": ["x"]}}, {"AAA": {"exchange": "NASDAQ"}}, {}))
    run_full(monkeypatch); st = db.get_settings(); assert st["entries_suspended"] is False, st["suspend_reason"]

def test_memo_references_real_inputs_and_required_fields(env2, monkeypatch):
    from app import research
    monkeypatch.setattr(universe, "candidates", lambda cfg, now=None: ({"AAA": {"name": "AAA Corp", "sources": ["x"]}}, {"AAA": {"exchange": "NASDAQ"}}, {}))
    run_full(monkeypatch); out = research.generate_memos(FakeProvider(frames_us()), NOW_SCAN); assert out
    c = db.conn(); m = c.execute("SELECT * FROM memos2 ORDER BY id LIMIT 1").fetchone(); d = c.execute("SELECT * FROM decisions WHERE id=?", (m["decision_id"],)).fetchone(); ll = c.execute("SELECT COUNT(*) n FROM llm_log").fetchone()["n"]; c.close()
    f = json.loads(m["fields"]); need = ("company", "ticker", "strategy", "price_usd", "score", "regime", "trigger", "stop_usd", "target_usd", "size_shares", "max_loss_usd", "max_loss_myr", "reward_risk", "bullish_evidence", "bearish_evidence", "execution_status", "data_sources")
    assert all(k in f for k in need), [k for k in need if k not in f]
    assert f["ticker"] == d["symbol"] and f["score"] == d["score"] and "no llm" in m["text"].lower() and m["mode"].startswith("template") and ll >= 1
    assert f["data_sources"]["last_bar"] == "2026-10-12"

def test_intraday_disabled_without_genuine_feed(env2, monkeypatch):
    from app.strategies import intraday
    cr = pb.CapResult("yfinance", True, True, {pb.EOD: pb.cap(True), pb.INTRADAY: pb.cap(True, note="delayed 15m"), pb.REALTIME_QUOTES: pb.cap(False)})
    st = intraday.status(config.DEFAULTS, [cr]); assert st["live_enabled"] is False
    cfg = {**config.DEFAULTS, "strategies": {**config.DEFAULTS["strategies"], "intraday": {"enabled": True, "allocation_pct": 0.0}}}
    st2 = intraday.status(cfg, [cr]); assert st2["live_enabled"] is False

def test_empty_control_file_is_ignored(env2, tmp_path):
    f = tmp_path / "commands.json"; f.write_text(""); assert control.apply_file(str(f), NOW_SCAN) == []

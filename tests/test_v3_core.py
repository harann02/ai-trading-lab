"""v3 core tests: policy/allocation, execution integrity, scheduler/calendar, forward verification, shadow benchmarks, migration."""
import os, sys, json
import pytest, pandas as pd, numpy as np
from datetime import datetime, timezone, timedelta, date
sys.path.insert(0, os.path.dirname(__file__))
from test_v2 import env2, run_full, SYMS7, frames_us, load, NOW_SCAN, NOW_FILL
from conftest import FakeProvider
from conftest_v2 import IntradayProvider
from app import db, trading, universe, ledger, portfolio, config, calendars as cal, settings_schema as ss, engine
from app.v3 import policy, execution as X, schedule as S, verification as V, shadow, jobs, migrate
import tools.gate as gate

CFG = {**config.DEFAULTS}
def bars(session="2026-10-13", start="13:30", n=12, base=100.0, step=0.1, low_dip=None):
    idx = pd.date_range(f"{session} {start}", periods=n, freq="5min", tz="UTC"); c = base + np.arange(n) * step; o = np.r_[base, c[:-1]]
    low = np.minimum(o, c) - 0.05
    if low_dip: low[low_dip[0]] = low_dip[1]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.05, "low": low, "close": c, "volume": 1e5}, index=idx)
ORDER = {"created_ts": "2026-10-12T22:30:00+00:00", "side": "BUY", "symbol": "XYZ"}

# ----------------------------------------------------------------- capital / policy
def test_v3_defaults_capital_structure():
    s = config.DEFAULTS["strategies"]; assert s["swing"]["allocation_pct"] == 100 and s["meanrev"]["allocation_pct"] == 0 and s["baseline"]["allocation_pct"] == 0 and s["intraday"]["allocation_pct"] == 0
    d = config.DEFAULTS; assert d["max_exposure_pct"] == 80 and d["min_cash_pct"] == 20 and d["max_position_pct"] == 20 and d["risk_per_trade_pct"] == 1 and d["max_positions"] == 5
    assert d["start_capital_myr"] == 10000

def test_mean_reversion_cannot_open_and_is_labeled(env2, monkeypatch):
    assert policy.new_entries_allowed("meanrev") is False and policy.new_entries_allowed("swing") is True
    f = policy.get_flag("meanrev"); assert f["label"] == "RESEARCH ONLY — TRADING DISABLED" and "did not demonstrate a positive net return after costs" in f["explanation"]
    monkeypatch.setattr(universe, "candidates", lambda cfg, now=None: ({s: {"name": s, "sources": ["x"]} for s in SYMS7}, {s: {"exchange": "NASDAQ"} for s in SYMS7}, {}))
    db.set_settings({"entry_cadence": "daily"}); run_full(monkeypatch, syms=SYMS7)
    c = db.conn(); n = c.execute("SELECT COUNT(*) n FROM orders WHERE strategy_id IN ('meanrev','baseline','intraday')").fetchone()["n"]; c.close(); assert n == 0

def test_allocation_cannot_fund_retired_sleeves(env2):
    for sid in ("meanrev", "baseline", "intraday"):
        clean, errs = ss.validate_patch({"strategies": {sid: {"allocation_pct": 10}}}, db.get_settings()); assert errs and any("allocation" in k for k in errs)

def test_rm10000_no_double_count_swing_uses_pool(env2, monkeypatch):
    monkeypatch.setattr(universe, "candidates", lambda cfg, now=None: ({s: {"name": s, "sources": ["x"]} for s in SYMS7}, {s: {"exchange": "NASDAQ"} for s in SYMS7}, {}))
    run_full(monkeypatch, syms=SYMS7); run_full(monkeypatch, syms=SYMS7); sn = portfolio.snapshot(NOW_SCAN)
    assert sn["sleeves"]["swing"]["invested_myr"] == pytest.approx(10000.0, abs=1e-6)                                  # whole pool, once
    assert sum(s["invested_myr"] for s in sn["sleeves"].values()) == pytest.approx(10000.0, abs=1e-6)
    assert sum(r["cash_delta_myr"] for r in ledger.rows() if r["kind"] in ("DEPOSIT", "WITHDRAW")) == pytest.approx(10000.0)

def test_migration_idempotent_and_journal_not_deposit(env2):
    c = db.conn(); n = c.execute("SELECT COUNT(*) n FROM schema_migrations WHERE version=3").fetchone()["n"]; c.close(); assert n == 1
    engine.ready(); engine.ready(); c = db.conn(); assert c.execute("SELECT COUNT(*) n FROM schema_migrations WHERE version=3").fetchone()["n"] == 1
    assert c.execute("SELECT value FROM ledger_meta WHERE key='v3_applied_ts'").fetchone()["value"]; c.close()

# ----------------------------------------------------------------- execution integrity
def test_fill_not_eligible_before_next_session_open():
    now = datetime(2026, 10, 12, 23, 0, tzinfo=timezone.utc)                                                          # Mon evening, order created after the close
    r = X.decide_forward_fill(ORDER, bars(), now, CFG); assert r["status"] == "not_eligible" and r["eligible_session"] == "2026-10-13"

def test_no_fill_on_unobservable_price():
    now = datetime(2026, 10, 13, 13, 40, tzinfo=timezone.utc)                                                         # 10 min after open: first 5m bar ended 13:35, becomes visible 13:50
    r = X.decide_forward_fill(ORDER, bars(), now, CFG); assert r["status"] == "awaiting_data" and "no candle" in r["reason"]
    later = datetime(2026, 10, 13, 13, 52, tzinfo=timezone.utc); r2 = X.decide_forward_fill(ORDER, bars(), later, CFG)
    assert r2["status"] == "fill" and r2["first_available_bar"] is True and pd.Timestamp(r2["price_available_ts"]) <= pd.Timestamp(later)    # price_available <= decision time

def test_fill_never_uses_future_bar():
    now = datetime(2026, 10, 13, 14, 10, tzinfo=timezone.utc); b = bars(n=40, step=1.0)                               # prices keep rising; future bars must be invisible
    r = X.decide_forward_fill(ORDER, b, now, CFG); assert r["status"] == "fill"
    vis = X.available_bars(b, now); assert r["price"] == pytest.approx(float(vis["close"].iloc[-1])) and r["price"] < float(b["close"].iloc[-1])

def test_stale_feed_and_empty_feed_block_fills():
    now = datetime(2026, 10, 13, 19, 0, tzinfo=timezone.utc); r = X.decide_forward_fill(ORDER, bars(n=6), now, CFG); assert r["status"] == "awaiting_data" and "stale" in r["reason"]
    assert X.decide_forward_fill(ORDER, None, datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc), CFG)["status"] == "awaiting_data"

def test_lapsed_order_not_filled_retroactively():
    now = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc); r = X.decide_forward_fill(ORDER, bars(), now, CFG); assert r["status"] == "lapsed"

def test_corporate_action_jump_held_for_review():
    now = datetime(2026, 10, 13, 14, 10, tzinfo=timezone.utc); r = X.decide_forward_fill(ORDER, bars(base=100), now, CFG, daily_ref=250.0); assert r["status"] == "review"

def test_stop_gap_models_open_not_stop_price():
    b = bars(n=8, base=100); b.iloc[3, b.columns.get_loc("open")] = 90.0; b.iloc[3, b.columns.get_loc("low")] = 89.0; now = datetime(2026, 10, 13, 15, 30, tzinfo=timezone.utc)
    r = X.evaluate_stop(95.0, b, "2026-10-13T13:00:00+00:00", now, CFG); assert r["gap"] is True and r["ref_price"] < 90.0 and "NOT guaranteed" not in r["note"] and "GAP" in r["note"]
    b2 = bars(n=8, base=100, low_dip=(3, 94.0)); r2 = X.evaluate_stop(95.0, b2, "2026-10-13T13:00:00+00:00", now, CFG); assert r2["gap"] is False and r2["ref_price"] < 95.0 and "NOT guaranteed" in r2["note"]
    assert X.evaluate_stop(80.0, b2, "2026-10-13T13:00:00+00:00", now, CFG) is None

def test_order_records_store_execution_evidence(env2, monkeypatch):
    monkeypatch.setattr(universe, "candidates", lambda cfg, now=None: ({s: {"name": s, "sources": ["x"]} for s in SYMS7}, {s: {"exchange": "NASDAQ"} for s in SYMS7}, {}))
    db.set_settings({"entry_cadence": "daily"}); run_full(monkeypatch, syms=SYMS7); from app import data
    fr = frames_us(); data.save_bars(fr, NOW_FILL); trading.execute_pending(IntradayProvider(fr), NOW_FILL, refresh=True)
    c = db.conn(); o = dict(c.execute("SELECT * FROM orders WHERE status='filled' AND side='BUY' LIMIT 1").fetchone()); c.close()
    for k in ("signal_ts", "data_ts", "order_type", "eligible_session", "data_source", "price_available_ts", "exec_ts", "exec_price", "fill_model_version", "fill_kind"): assert o[k] not in (None, ""), k
    assert o["fill_kind"] == "FORWARD_PAPER" and pd.Timestamp(o["price_available_ts"]) <= pd.Timestamp(o["exec_ts"]) and pd.Timestamp(o["exec_ts"]) >= pd.Timestamp("2026-10-13T13:30:00+00:00")
    assert o["fees_usd"] is not None and o["slippage_usd"] is not None and o["fx_rate"]

def test_duplicate_signal_and_ledger_append_only(env2, monkeypatch):
    monkeypatch.setattr(universe, "candidates", lambda cfg, now=None: ({s: {"name": s, "sources": ["x"]} for s in SYMS7}, {s: {"exchange": "NASDAQ"} for s in SYMS7}, {}))
    db.set_settings({"entry_cadence": "daily"}); run_full(monkeypatch, syms=SYMS7); c = db.conn(); n = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]; c.close()
    run_full(monkeypatch, syms=SYMS7); c = db.conn(); assert c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == n
    with pytest.raises(Exception): c.execute("UPDATE ledger SET cash_delta_myr=1 WHERE id=1")
    with pytest.raises(Exception): c.execute("DELETE FROM ledger WHERE id=1")
    c.close()

# ----------------------------------------------------------------- scheduler / calendar
def test_calendar_dst_both_seasons_and_early_close():
    summer = cal.session_hours("US", date(2026, 7, 14)); winter = cal.session_hours("US", date(2026, 12, 15))
    assert summer[0].hour == 13 and summer[0].minute == 30 and summer[1].hour == 20                                   # EDT: 13:30-20:00 UTC
    assert winter[0].hour == 14 and winter[0].minute == 30 and winter[1].hour == 21                                   # EST: 14:30-21:00 UTC (the old cron missed this close)
    assert cal.session_hours("US", date(2026, 11, 26)) is None and cal.session_hours("US", date(2026, 7, 3)) is None  # Thanksgiving, Independence Day observed
    ec = cal.session_hours("US", date(2026, 11, 27)); assert ec[1].hour == 18                                         # early close 13:00 ET

def test_monitor_window_covers_full_session_and_skips_offmarket():
    ok, d, o, c, why = S.monitor_window(datetime(2026, 12, 15, 20, 55, tzinfo=timezone.utc)); assert ok                   # winter 20:55 UTC is inside (close 21:00)
    assert S.monitor_window(datetime(2026, 12, 15, 21, 20, tzinfo=timezone.utc))[0] is True and S.monitor_window(datetime(2026, 12, 15, 21, 40, tzinfo=timezone.utc))[0] is False
    ok2, _, _, _, why2 = S.monitor_window(datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)); assert not ok2 and "not an NYSE session" in why2
    assert S.monitor_window(datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc))[0] is False
    p, why3 = gate.decide("monitor", "schedule", datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)); assert p is False
    assert gate.decide("monitor", "workflow_dispatch", datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc))[0] is True

def test_cron_expression_covers_winter_close():
    yml = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".github/workflows/run.yml")).read()
    assert '"*/15 13-21 * * 1-5"' in yml and "tools/gate.py" in yml and "concurrency:" in yml and "group: ailab-ledger" in yml

def test_missed_run_detection(env2):
    now = datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc)                                                           # Tue 12:00 ET, session open since 13:30
    miss = S.missed_runs(now, lookback_h=3, runs=[]); assert any(m["job"] == "monitor" for m in miss)
    runs = [{"job": "monitor", "status": "success", "started_ts": e.isoformat(timespec="seconds")} for e in S.expected_monitor_runs(now - timedelta(hours=3), now - timedelta(minutes=S.TOL_MIN))]
    assert [m for m in S.missed_runs(now, lookback_h=3, runs=runs) if m["job"] == "monitor"] == []
    skipped = [{**r, "status": "skipped"} for r in runs]; assert any(m["job"] == "monitor" for m in S.missed_runs(now, lookback_h=3, runs=skipped))   # a skip never hides a miss

def test_restart_no_duplicate_scan_and_stale_running_retry(env2):
    a = jobs.begin("scan", NOW_SCAN, "schedule", "1", session="2026-10-12"); assert a
    assert jobs.begin("scan", NOW_SCAN + timedelta(minutes=3), "schedule", "2", session="2026-10-12") is None            # still running: duplicate refused
    assert jobs.begin("scan", NOW_SCAN + timedelta(minutes=60), "schedule", "3", session="2026-10-12")                   # stale 'running' (crashed run) may be retried
    jobs.finish(a, "success", now=NOW_SCAN)

# ----------------------------------------------------------------- forward verification
def _sess(d, status):
    c = db.conn(); c.execute("INSERT OR REPLACE INTO forward_sessions(session_date,status,qualifies,updated_ts) VALUES(?,?,?,?)", (d, status, int(status == "QUALIFIED"), "x")); c.commit(); c.close()

def test_verification_cannot_complete_prematurely(env2):
    c = db.conn(); c.execute("INSERT OR REPLACE INTO ledger_meta VALUES('v3_applied_ts','2026-10-01T00:00:00+00:00')"); c.commit(); c.close()
    assert V.summary(NOW_SCAN)["status"] == "NOT STARTED"
    days = [d.date().isoformat() for d in pd.bdate_range("2026-10-05", periods=25)]
    for d in days[:19]: _sess(d, "QUALIFIED")
    s = V.summary(NOW_SCAN); assert s["status"] == "IN PROGRESS" and s["consecutive_qualifying"] == 19
    _sess(days[19], "QUALIFIED"); assert V.summary(NOW_SCAN)["status"] == "OPERATIONALLY VERIFIED"

def test_failed_session_resets_streak_and_is_not_successful(env2):
    c = db.conn(); c.execute("INSERT OR REPLACE INTO ledger_meta VALUES('v3_applied_ts','2026-10-01T00:00:00+00:00')"); c.commit(); c.close()
    days = [d.date().isoformat() for d in pd.bdate_range("2026-10-05", periods=12)]
    for d in days[:10]: _sess(d, "QUALIFIED")
    _sess(days[10], "FAILED"); s = V.summary(NOW_SCAN); assert s["status"] == "ACTION REQUIRED" and s["consecutive_qualifying"] == 0 and s["failed_sessions"] == 1
    _sess(days[11], "QUALIFIED"); s = V.summary(NOW_SCAN); assert s["consecutive_qualifying"] == 1 and s["total_qualifying"] == 11 and s["status"] == "IN PROGRESS"

def test_session_without_real_scan_fails_never_qualifies(env2):
    c = db.conn(); c.execute("INSERT OR REPLACE INTO ledger_meta VALUES('v3_applied_ts','2026-10-01T00:00:00+00:00')"); c.commit(); c.close()
    item, fails, notes = V.compute_session(date(2026, 10, 7), datetime(2026, 10, 9, tzinfo=timezone.utc)); assert fails and item["scan_occurred"] == 0 and any("missed run" in f for f in fails)

def test_start_session_is_first_session_after_migration(env2):
    c = db.conn(); c.execute("INSERT OR REPLACE INTO ledger_meta VALUES('v3_applied_ts','2026-10-12T02:00:00+00:00')"); c.commit(); c.close()
    assert V.start_session() == date(2026, 10, 12)                                                                       # Mon 13:30 UTC open is after Mon 02:00 UTC migration
    c = db.conn(); c.execute("INSERT OR REPLACE INTO ledger_meta VALUES('v3_applied_ts','2026-10-12T15:00:00+00:00')"); c.commit(); c.close()
    assert V.start_session() == date(2026, 10, 13)

# ----------------------------------------------------------------- shadow benchmarks
def test_shadow_benchmarks_do_not_touch_trading_cash(env2, monkeypatch):
    from app import data
    monkeypatch.setattr(universe, "candidates", lambda cfg, now=None: ({s: {"name": s, "sources": ["x"]} for s in SYMS7}, {s: {"exchange": "NASDAQ"} for s in SYMS7}, {}))
    run_full(monkeypatch, syms=SYMS7); before = (len(ledger.rows()), ledger.cash(), json.dumps(portfolio.snapshot(NOW_SCAN)["sleeves"], default=str))
    info = shadow.build(NOW_SCAN); after = (len(ledger.rows()), ledger.cash(), json.dumps(portfolio.snapshot(NOW_SCAN)["sleeves"], default=str)); assert before == after
    c = db.conn(); sp = c.execute("SELECT * FROM shadow_portfolios").fetchall(); c.close()
    for r in sp: assert r["start_myr"] == 10000.0 and r["shares"] > 0 and "fractional" in r["method"]

# ---------------------------------------------------------------- P6: only cron `schedule` runs count as unattended evidence
def test_manual_scan_not_counted_but_schedule_scan_is(env2, monkeypatch):
    from app.v3 import verification as V2, jobs as J
    from datetime import date
    sd = V2.start_session() or date(2026, 10, 12)
    d = sd; h, nxt = V2._session_window(d); at = h[1] + timedelta(hours=1, minutes=30)
    c = db.conn(); c.execute("INSERT INTO job_runs(idem_key,job,trigger,scheduled_for,started_ts,finished_ts,status,run_id) VALUES('manual:scan:1','scan','workflow_dispatch',?,?,?,'success','111')", (at.isoformat(timespec="seconds"),) * 3); c.commit(); c.close()
    item, fails, notes = V2.compute_session(d, at + timedelta(hours=1)); assert item["scan_occurred"] == 0 and any("manual/push" in n for n in notes) and any("no real scan" in f for f in fails)
    c = db.conn(); c.execute("INSERT INTO job_runs(idem_key,job,trigger,scheduled_for,started_ts,finished_ts,status,run_id) VALUES('scan:s','scan','schedule',?,?,?,'success','222')", (at.isoformat(timespec="seconds"),) * 3); c.commit(); c.close()
    item, fails, notes = V2.compute_session(d, at + timedelta(hours=1)); assert item["scan_occurred"] == 1
    s = V2.summary(at + timedelta(hours=1)); assert s["scheduled_runs_recorded"] == 1 and s["manual_runs_not_counted"][0]["trigger"] == "workflow_dispatch" and "ONLY" in s["counted_basis"]

def test_ci_run_records_event_name_in_heartbeat_and_job_runs(env2, monkeypatch):
    import ci_run
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch"); monkeypatch.setenv("GITHUB_RUN_ID", "4242"); monkeypatch.delenv("AITL_CONTROL_DIR", raising=False)
    ci_run.main("export"); c = db.conn(); hb = c.execute("SELECT info FROM heartbeat WHERE name='last_ci_run'").fetchone()["info"]; tr = c.execute("SELECT trigger FROM job_runs WHERE run_id='4242'").fetchone()["trigger"]; c.close()
    assert "event=workflow_dispatch" in hb and "counts_as_unattended=False" in hb and tr == "workflow_dispatch"

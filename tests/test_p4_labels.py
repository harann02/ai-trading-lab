"""P4: docs / labels / KPI honesty / safe bounds. Execution permissions are NOT changed by any of this."""
import os, re, sys, json, pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from app import db, config, settings_schema as ss

@pytest.fixture
def env3(tmp_path, monkeypatch):
    monkeypatch.setenv("AITL_DB", str(tmp_path / "p4.db")); db.init(); return tmp_path

def test_readme_has_no_obsolete_v2_allocation_or_token_flow_as_current():
    r = open(os.path.join(ROOT, "README.md")).read()
    assert "30%/30%/40%" not in r and "(30%)" not in r and "**A swing** (30%)" not in r
    assert "sessionStorage" not in r and "Contents API" not in r
    assert "Swing = 100 % of the trading pool" in r and "80 %" in r and "SPY and QQQ shadow" in r
    h = open(os.path.join(ROOT, "docs", "V2_VS_V3.md")).read(); assert "Historical (v2.0)" in h
    for f in os.listdir(os.path.join(ROOT, "docs")):
        t = open(os.path.join(ROOT, "docs", f)).read()
        if re.search(r"swing\*?\*? \(30%\)|browser token|sessionStorage", t): assert "HISTORICAL" in t.upper() or "Historical" in t, f

def test_active_strategies_lists_only_swing(env3):
    from app import views
    db.set_settings({"strategies": {"swing": {"enabled": True, "allocation_pct": 100}, "meanrev": {"enabled": True, "allocation_pct": 0}, "baseline": {"enabled": False, "allocation_pct": 0}, "intraday": {"enabled": False, "allocation_pct": 0}}})
    k = views.kpis(); assert k["active_strategies"] == ["swing"] and k["n_active_strategies"] == 1

def test_research_only_exact_label(env3):
    from app.v3 import policy
    fl = policy.all_flags()
    for sid in ("meanrev", "intraday"): assert fl[sid]["label"] == "RESEARCH ONLY — TRADING DISABLED" and fl[sid]["forward_paper_execution"] is False
    assert fl["swing"]["forward_paper_execution"] is True
    # an OLD label stored in the DB row is displayed normalised, permission untouched
    c = db.conn(); c.execute("UPDATE strategy_flags SET label='RESEARCH ONLY — NEW TRADES DISABLED' WHERE strategy_id='meanrev'"); c.commit(); c.close()
    assert policy.all_flags()["meanrev"]["label"] == "RESEARCH ONLY — TRADING DISABLED" and not policy.new_entries_allowed("meanrev")

def test_safe_bounds_defaults_pass_and_extremes_need_audited_override(env3):
    cur = db.get_settings()
    ok, errs = ss.validate_patch({"max_position_pct": 20, "min_cash_pct": 20, "max_exposure_pct": 80, "risk_per_trade_pct": 1, "max_positions": 5}, cur); assert not errs, errs
    for patch in ({"max_position_pct": 60}, {"min_cash_pct": 0}, {"risk_per_trade_pct": 4}, {"max_exposure_pct": 100}, {"max_positions": 10}):
        ok, errs = ss.validate_patch(patch, cur); assert errs and "safe bound" in list(errs.values())[0], patch
    ok, errs = ss.validate_patch({"max_position_pct": 60, "owner_limit_override": True}, cur); assert not errs and ok["max_position_pct"] == 60
    c = db.conn(); n = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='risk_limit_override'").fetchone()[0]; c.close(); assert n == 1, "override is recorded in the audit log"
    assert config.DEFAULTS["max_position_pct"] == 20 and config.DEFAULTS["min_cash_pct"] == 20, "effective defaults unchanged"

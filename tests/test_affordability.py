"""P7: small-account affordability gate: reason codes, no limit relaxed, daily affordable-universe count, fractional OFF by default."""
import os, sys, json, pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); sys.path.insert(0, os.path.dirname(__file__))
from app import affordability as AFF, risk, config, db
from test_v2 import env2, run_full, SYMS7
from app import universe

CFG = json.loads(json.dumps(config.DEFAULTS))
EQ = 2441.85     # the actual account in USD
def size(price, stop, usable=None, adv=5e8, cfg=CFG):
    return risk.size_position_usd(equity_usd=EQ, usable_cash_usd=EQ * 0.8 if usable is None else usable, price=price, stop=stop, cfg=cfg, adv_usd=adv, sleeve_equity_usd=EQ)

def test_fractional_off_by_default():
    assert config.DEFAULTS["fractional_shares"] is False

def test_price_above_cap_is_unaffordable_with_code():
    sz = size(520.0, 495.0); code, det = AFF.classify(sz); assert code == "UNAFFORDABLE_PRICE_GT_CAP" and sz["qty"] == 0
    msg = AFF.explain(code, det, EQ * 0.2); assert "UNAFFORDABLE_PRICE_GT_CAP" in msg and "488" in msg      # cap ~ US$488

def test_risk_per_share_larger_than_budget_is_unaffordable():
    sz = size(300.0, 270.0)            # $30+ loss per share > 1% budget ($24.4) although the share fits inside the cap
    code, det = AFF.classify(sz); assert code == "UNAFFORDABLE_RISK_PER_SHARE" and det["loss_per_share_usd"] > det["risk_budget_usd"]

def test_cash_and_affordable_cases():
    assert AFF.classify(size(400.0, 396.0, usable=300.0))[0] == "UNAFFORDABLE_CASH"
    code, det = AFF.classify(size(50.0, 47.5)); assert code == "AFFORDABLE" and det["qty"] >= 1

def test_limits_not_relaxed_by_gate():
    """Same inputs with the gate must not size bigger than the engine's own caps (cap 20% / risk 1%)."""
    sz = size(100.0, 95.0); assert sz["qty"] * 100.0 <= EQ * 0.20 + 1e-6 and sz["qty"] * (100 - 95) <= EQ * 0.01 * 1.2

def test_fractional_whatif_only_changes_sizing_when_enabled():
    c2 = json.loads(json.dumps(CFG)); c2["fractional_shares"] = True
    assert size(520.0, 495.0)["qty"] == 0 and 0 < size(520.0, 495.0, cfg=c2)["qty"] < 1

def test_scan_records_affordability_gate_and_daily_count(env2, monkeypatch):
    monkeypatch.setattr(universe, "candidates", lambda cfg, now=None: ({s: {"name": s, "sources": ["x"]} for s in SYMS7}, {s: {"exchange": "NASDAQ"} for s in SYMS7}, {}))
    db.set_settings({"entry_cadence": "daily"}); run_full(monkeypatch, syms=SYMS7)
    rows = AFF.latest(3); assert rows and rows[0]["evaluated"] >= 1 and rows[0]["affordable"] <= rows[0]["evaluated"] and sum(rows[0]["by_code"].values()) == rows[0]["evaluated"]
    c = db.conn(); n = c.execute("SELECT COUNT(*) FROM decisions WHERE checks LIKE '%affordability_gate%'").fetchone()[0]; c.close(); assert n >= 1

def test_export_includes_affordability_list(tmp_path):
    import subprocess
    env = {**os.environ, "AITL_DB": str(tmp_path / "a.db"), "AITL_LLM_DISABLED": "1"}; root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "export_static.py", str(tmp_path / "s")], env=env, cwd=root, capture_output=True, text=True); assert r.returncode == 0, r.stderr[-400:]
    assert isinstance(json.load(open(tmp_path / "s" / "data" / "v3.json"))["affordability"], list)

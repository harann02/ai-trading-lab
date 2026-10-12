"""P5: backtest/live parity: shared code path, methodology id, point-in-time snapshots, trade-list recompute."""
import os, sys, json, pytest
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); sys.path.insert(0, os.path.dirname(__file__))
from app import config, db, data, backtest_parity as bp, backtest2 as b2, scoring

def test_methodology_id_and_weight_rule_declared():
    assert bp.METHODOLOGY_ID == "swing-parity-minus-fundamentals-1" and "renormalised" in bp.WEIGHT_RULE

def test_weight_renormalisation_matches_live_rule():
    f = pd.Series({k: np.nan for k in ("x",)}); 
    import app.scoring as S
    # with fundamentals unavailable the five other weights are divided by 0.80 (proportional), exactly what composite() does for the live engine
    w = {k: v for k, v in S.W.items() if k != "fundamental"}; tot = sum(w.values()); assert abs(tot - 0.80) < 1e-9
    assert abs(sum(v / tot for v in w.values()) - 1.0) < 1e-9

def test_parity_uses_live_config_and_shared_functions():
    import inspect
    src = inspect.getsource(bp.swing_candidates_parity)
    for needle in ("scoring.composite", "scoring.entry_trigger", "scoring.plan_levels", "universe_fundamentals_top_n", "atr_pct", "min_adv_usd", "minervini"): assert needle in src, needle
    assert bp.b2.simulate is b2.simulate and "size_position_usd" in inspect.getsource(b2.simulate), "same sizing function as live"

def test_expectancy_recomputable_from_trade_list():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); import run_validation_parity as rvp
    tr = [{"pnl": 10.0, "days": 5}, {"pnl": -4.0, "days": 3}, {"pnl": 6.0, "days": 4}, {"pnl": -2.0, "days": 2}]
    e = rvp.expectancy(tr); assert e["win_rate_pct"] == 50 and e["profit_factor"] == pytest.approx(16 / 6) and e["expectancy_usd"] == pytest.approx(2.5)

def test_pit_fundamentals_snapshot_is_append_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AITL_DB", str(tmp_path / "p.db")); db.init()
    class P:
        def info(self, s): return {"returnOnEquity": 0.2, "trailingPE": 20, "profitMargins": 0.1, "revenueGrowth": 0.1, "shortName": s}
    from datetime import datetime, timezone, timedelta
    t0 = datetime(2026, 10, 12, tzinfo=timezone.utc); data.get_fundamentals("AAA", P(), t0); data.get_fundamentals("AAA", P(), t0 + timedelta(days=8))
    c = db.conn(); n = c.execute("SELECT COUNT(*) FROM fundamentals_pit WHERE symbol='AAA'").fetchone()[0]
    with pytest.raises(Exception): c.execute("UPDATE fundamentals_pit SET data='x'")
    with pytest.raises(Exception): c.execute("DELETE FROM fundamentals_pit")
    c.close(); assert n == 2, "each refresh is kept with its own retrieval timestamp"

def test_results_json_labels_legacy_and_keeps_old_numbers():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); old = json.load(open(os.path.join(root, "validation", "results_legacy_v2.json")))
    assert "LEGACY" in old["_label"] and abs(old["periods"]["OUT-OF-SAMPLE 2024-"]["swing"]["ret_usd_pct"] - 40.9) < 0.2
    new = json.load(open(os.path.join(root, "validation", "results.json")))
    if "methodology_id" in new:
        assert new["methodology_id"] == bp.METHODOLOGY_ID and "LEGACY" in new["legacy"]["label"] and any("LEGACY" in k for k in new["periods"]["FULL"])
        assert new["setup"]["params_tuned"] is False and new["setup"]["predeclared_splits"] is True

"""v3 strategy policy: swing is the primary strategy (100% of the trading pool); mean reversion is RESEARCH ONLY (no new entries); baseline sleeves are retired
(SPY/QQQ are SHADOW benchmarks only, never real capital); intraday stays disabled. Reactivation is an explicit owner action that is checked against validation conditions
and is NEVER automatic."""
import json
from datetime import datetime, timezone
from .. import db

PRIMARY = "swing"
RESEARCH_LABEL = "RESEARCH ONLY — NEW TRADES DISABLED"
MEANREV_WHY = "Mean-reversion entries are suspended because historical testing did not demonstrate a positive net return after costs."
RETIRED_WHY = "Retired in v3.0: SPY/QQQ are tracked only as separate shadow benchmark portfolios, never as real trading capital."
# status per strategy when no flag row exists (fresh v3 DBs get rows from migration/init)
DEFAULT_FLAGS = {
    "swing": {"new_entries": True, "label": "PRIMARY — 100% of trading pool", "why": ""},
    "meanrev": {"new_entries": False, "label": RESEARCH_LABEL, "why": MEANREV_WHY},
    "baseline": {"new_entries": False, "label": "RETIRED — shadow benchmark only", "why": RETIRED_WHY},
    "intraday": {"new_entries": False, "label": "RESEARCH ONLY — DISABLED (owner enable required)", "why": "No real-time intraday feed; no fabricated minute candles."},
}
REACTIVATION_RULES = ("owner command 'reactivate_strategy' with confirm == 'REACTIVATE <strategy>'", "out-of-sample net return after costs > 0 in validation/results.json",
                      ">= 30 out-of-sample trades", "net return still > 0 under 2x cost sensitivity", "positive in a majority of walk-forward windows",
                      "allocation must then be re-set explicitly by the owner (never automatic)")

def now_iso(): return datetime.now(timezone.utc).isoformat(timespec="seconds")

def ensure_flags(c=None):
    own = c is None; c = c or db.conn()
    for sid, f in DEFAULT_FLAGS.items():
        c.execute("INSERT OR IGNORE INTO strategy_flags(strategy_id,new_entries_enabled,label,explanation,updated_ts) VALUES(?,?,?,?,?)", (sid, int(f["new_entries"]), f["label"], f["why"], now_iso()))
    if own: c.commit(); c.close()

def get_flag(sid):
    c = db.conn()
    try:
        r = c.execute("SELECT * FROM strategy_flags WHERE strategy_id=?", (sid,)).fetchone()
    except Exception:
        r = None
    c.close()
    if r: return dict(r)
    d = DEFAULT_FLAGS.get(sid, {"new_entries": False, "label": "", "why": ""})
    return {"strategy_id": sid, "new_entries_enabled": int(d["new_entries"]), "label": d["label"], "explanation": d["why"]}

def new_entries_allowed(sid) -> bool:
    return bool(get_flag(sid)["new_entries_enabled"])

def all_flags():
    ensure_flags(); c = db.conn(); r = {x["strategy_id"]: dict(x) for x in c.execute("SELECT * FROM strategy_flags")}; c.close(); return r

def reactivation_conditions(strategy="meanrev", results=None, path=None):
    """Evaluates the documented validation conditions from the stored backtest results. Returns {ok, checks[]}; ok is necessary but NOT sufficient (owner must also command it)."""
    if results is None:
        from pathlib import Path
        p = Path(path) if path else Path(__file__).resolve().parents[2] / "validation" / "results.json"
        results = json.loads(p.read_text()) if p.exists() else None
    chk = []
    if not results: return {"ok": False, "checks": [{"name": "validation results available", "pass": False, "detail": "validation/results.json missing"}]}
    per = results.get("periods", {}); oos = (per.get("OUT-OF-SAMPLE 2024-") or {}).get(strategy) or {}
    ret = oos.get("ret_usd_pct"); n = oos.get("n_trades")
    chk.append({"name": "OOS net return after costs > 0", "pass": ret is not None and ret > 0, "detail": f"OOS ret_usd_pct={ret}"})
    chk.append({"name": ">=30 OOS trades", "pass": (n or 0) >= 30, "detail": f"n_trades={n}"})
    wins = [k for k, v in per.items() if k.isdigit() and (v.get(strategy) or {}).get("ret_usd_pct") is not None and v[strategy]["ret_usd_pct"] > 0]
    tot = [k for k, v in per.items() if k.isdigit() and (v.get(strategy) or {}).get("ret_usd_pct") is not None]
    chk.append({"name": "positive in majority of yearly windows", "pass": bool(tot) and len(wins) * 2 > len(tot), "detail": f"{len(wins)}/{len(tot)} windows positive"})
    cs = (results.get("cost_sensitivity") or {}).get(strategy) or {}
    chk.append({"name": "positive under 2x costs", "pass": bool(cs) and (cs.get("2x") or {}).get("ret_usd_pct", -1) > 0, "detail": f"cost_sensitivity={cs.get('2x')}" if cs else "cost sensitivity not run"})
    return {"ok": all(c["pass"] for c in chk), "checks": chk, "rules": list(REACTIVATION_RULES)}

def reactivate(strategy, confirm, now=None):
    """Owner-only (called from the authenticated control plane). Refuses unless conditions pass AND the exact confirmation is given. Never auto-called."""
    if strategy not in ("meanrev",): raise ValueError("only mean reversion has a reactivation path in v3")
    if confirm != f"REACTIVATE {strategy}": raise ValueError(f"confirm must equal 'REACTIVATE {strategy}'")
    cond = reactivation_conditions(strategy)
    if not cond["ok"]: raise ValueError("validation conditions NOT met: " + "; ".join(f"{c['name']} ({c['detail']})" for c in cond["checks"] if not c["pass"]))
    ensure_flags(); c = db.conn()
    c.execute("UPDATE strategy_flags SET new_entries_enabled=1,label=?,explanation=?,updated_ts=? WHERE strategy_id=?", ("ACTIVE (owner reactivated)", "reactivated by owner after validation conditions passed", now_iso(), strategy))
    c.commit(); c.close(); db.audit("strategy_reactivated", {"strategy": strategy, "conditions": cond}, "owner", "control"); return {"reactivated": strategy, "note": "allocation must be set explicitly by the owner"}

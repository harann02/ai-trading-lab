"""Export the READ-ONLY static dashboard snapshot (JSON + frontend) into ./site (published to GitHub Pages).
Usage: python export_static.py [outdir]   (env AITL_DB selects the SQLite file). Only paper-trading data; no secrets, no tokens, no credentials."""
import os, sys, json, shutil, csv, io
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import db, config, views, ledger, data, calendars as cal, ai, control, portfolio, universe
from app.providers import registry

ROOT = Path(__file__).resolve().parent
out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "site"); D = out / "data"
if out.exists(): shutil.rmtree(out)
D.mkdir(parents=True); (D / "candles").mkdir(); (D / "decision").mkdir()
for f in (ROOT / "site_src").iterdir():
    if f.is_file(): shutil.copy(f, out / f.name)
(out / ".nojekyll").write_text("")
if (ROOT / "docs").exists(): shutil.copytree(ROOT / "docs", out / "docs")

def dump(name, obj):
    p = D / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(obj, separators=(",", ":"), default=str))

db.init(); cfg = db.get_settings(); now = datetime.now(timezone.utc)
k = views.kpis(cfg, now); dump("kpis.json", k); dump("equity.json", views.equity_curves(cfg)); dump("holdings.json", views.holdings(cfg)); dump("orders.json", views.orders())
led = views.ledger_rows(); dump("ledger.json", led)
cols = list(led[0].keys()) if led else ["id"]
s = io.StringIO(); w = csv.writer(s); w.writerow(cols); [w.writerow([r.get(c) for c in cols]) for r in led]; (D / "ledger.csv").write_text(s.getvalue())
dump("fx.json", {"conversions": views.fx_conversions(), "current": views.fx_status(cfg), "spread_bps": cfg["fx_spread_bps"], "note": "Spread charged only on real conversions; display uses mid."})
disc = views.discovery(); dump("discovery.json", disc); dump("universe.json", views.universe_summary())
sv = views.strategies_view(cfg); dump("strategies.json", sv)
dump("providers.json", [r for r in registry.stored()]); dump("monitor.json", views.monitor_view()); dump("feed.json", views.feed())
c = db.conn()
reps = {kind: (json.loads(r["body"]) if r else None) for kind in ("market_summary", "weekly_strategy") for r in [c.execute("SELECT body FROM reports WHERE kind=? ORDER BY period DESC LIMIT 1", (kind,)).fetchone()]}
bt = [json.loads(r["result"]) for r in c.execute("SELECT result FROM experiments ORDER BY id DESC LIMIT 6")]
c.close(); dump("reports.json", reps); vr = ROOT / "validation" / "results.json"; dump("validation.json", {"cadence_study": (json.loads((ROOT / "validation" / "cadence_study.json").read_text()) if (ROOT / "validation" / "cadence_study.json").exists() else None), "membership_check": (json.loads((ROOT / "validation" / "membership_check.json").read_text()) if (ROOT / "validation" / "membership_check.json").exists() else None), "backtest": json.loads(vr.read_text()) if vr.exists() else None, "experiments": bt, "note": "HISTORICAL BACKTEST / OUT-OF-SAMPLE results live here only and are never combined with forward paper results."})
dump("control.json", {**control.status(), "settings": {kk: cfg[kk] for kk in ("risk_per_trade_pct", "max_position_pct", "max_exposure_pct", "min_cash_pct", "max_positions", "max_drawdown_pct", "automation_on", "manual_approval",
                      "fractional_shares", "cash_account_mode", "fx_spread_bps", "strategies", "extra_watchlist", "blocked_symbols", "entries_suspended", "suspend_reason", "buy_min_score", "atr_stop_mult")},
                      "pending_orders": [o for o in views.orders() if o["status"] == "pending"]})
# per-symbol decision + candles: top discovery rows, holdings, pending, benchmarks
want = [r["symbol"] for r in disc["rows"][:150]] + [h["symbol"] for h in views.holdings(cfg)] + [o["symbol"] for o in views.orders() if o["status"] == "pending"] + ["SPY", "QQQ"]
n = 0
for sym in dict.fromkeys(want):
    try:
        cd = views.candles(sym)
        if cd: dump(f"candles/{sym}.json", cd); n += 1
        dump(f"decision/{sym}.json", views.decision_for(sym))
    except Exception as e: print("skip", sym, e)
gh = os.environ; run_url = f"{gh['GITHUB_SERVER_URL']}/{gh['GITHUB_REPOSITORY']}/actions/runs/{gh['GITHUB_RUN_ID']}" if gh.get("GITHUB_RUN_ID") else None
c = db.conn(); hb = {r["name"]: r["ts"] for r in c.execute("SELECT name,ts FROM heartbeat")}; applied = c.execute("SELECT COUNT(*) n FROM control_applied").fetchone()["n"]; c.close()
dump("meta.json", {"generated_utc": now.isoformat(timespec="seconds"), "generated_myt": now.astimezone(ledger.MYT).strftime("%Y-%m-%d %H:%M MYT"), "trigger": gh.get("AITL_TRIGGER") or "local",
                   "run_url": run_url, "run_number": gh.get("GITHUB_RUN_NUMBER"), "platform_version": config.PLATFORM_VERSION, "data_label": data.DATA_LABEL, "heartbeats": hb,
                   "mode": "PAPER TRADING / DELAYED yfinance DATA / updates on workflow runs (not real-time ticks)", "us_market": cal.us_status(now), "specials": cal.upcoming_special_days(now.date(), 75),
                   "controls_applied_count": applied, "repo": "harann02/ai-trading-lab", "control_branch": "data", "control_path": "control/commands.json", "workflow": "run.yml"})
try:
    from app.v3 import export as v3x; dump("v3.json", v3x.build(cfg, now))
except Exception as e: print("v3 export failed", e); dump("v3.json", {"error": str(e)})
print(f"exported to {out}: {len(list(D.rglob('*.json')))} json files, {n} candle files")

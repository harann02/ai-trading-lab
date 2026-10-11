"""One CI run (GitHub Actions). Usage: python ci_run.py scan|monitor|control|weekly|export
Order: apply owner control file (validated, audited) -> action -> record heartbeat. Idempotent (UNIQUE decision/order/fill keys).
Writes $GITHUB_OUTPUT 'changed=true|false' so the workflow can skip persisting/deploying when a monitor run did nothing."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime, timezone
from app import engine, trading, db, control, research, calendars as cal, config
from app.providers.yf import YFinanceProvider

def main(a):
    now = datetime.now(timezone.utc); engine.ready(); changed = False
    db.event("INFO", "actions", f"GitHub Actions run: {a}")
    cdir = os.environ.get("AITL_CONTROL_DIR")
    if cdir:
        res = control.apply_file(os.path.join(cdir, "commands.json"), now)
        if res: changed = True; print("control:", json.dumps(res, default=str)[:600])
    prov = YFinanceProvider()
    try:
        from app import ai; st = ai.status(); print("AI layer:", json.dumps(st)); db.event("INFO", "ai", "LLM status: " + json.dumps(st)[:400])
    except Exception as e: print("AI status failed", e)
    if a in ("scan", "scan_all"):
        s = trading.run_scan(prov, now); changed = True
        print(json.dumps({k: v for k, v in s.items() if k not in ("capabilities",)}, default=str)[:1500])
        try: research.generate_memos(prov, now); research.daily_market_summary(now)
        except Exception as e: db.event("WARN", "ai", f"research step failed: {e}")
    if a in ("monitor", "control", "scan", "scan_all"):
        if cal.is_open("US", now) or a in ("scan", "scan_all", "control"):
            log = trading.execute_pending(prov, now, refresh=cal.is_open("US", now))
            if any(r[1] == "filled" for r in log): changed = True
            db.heartbeat("monitor_US", json.dumps(log)[:500])
    if a == "weekly":
        try: research.weekly_strategy_report(now); changed = True
        except Exception as e: db.event("WARN", "ai", f"weekly report failed: {e}")
    if a in ("export", "control"): changed = True
    db.heartbeat("last_ci_run", a)
    if a == "monitor" and cal.is_open("US", now): changed = True         # keep heartbeat/last-update fresh during market hours
    out = os.environ.get("GITHUB_OUTPUT")
    if out: open(out, "a").write(f"changed={'true' if changed else 'false'}\n")

if __name__ == "__main__": main(sys.argv[1] if len(sys.argv) > 1 else "export")

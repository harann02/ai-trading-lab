"""Scheduled job functions (importable by APScheduler's persistent job store)."""
import traceback, json
from datetime import datetime, timezone
from . import db, engine, data, calendars as cal, reports, ledger

_SCHED = None

def _provider(): return data.YFProvider()

def scan_market(market):
    now = datetime.now(timezone.utc)
    try:
        engine.ready()
        if not cal.is_session_day(market, cal.last_completed_session(market, now)):
            return
        res = engine.scan(_provider(), now, market=market)
        db.event("INFO", "scheduler", f"{market} scan job finished: {json.dumps(res.get('markets', {}).get(market, {}).get('counts'))}")
    except Exception as e:
        db.event("ERROR", "scheduler", f"{market} scan job failed: {e}\n{traceback.format_exc()[-600:]}")

def monitor(market):
    now = datetime.now(timezone.utc)
    try:
        engine.ready()
        if not cal.is_open(market, now): return
        log = engine.execute_pending(_provider(), now)
        db.heartbeat(f"monitor_{market}", json.dumps(log)[:500])
    except Exception as e:
        db.event("ERROR", "scheduler", f"{market} monitor failed: {e}")

def weekly():
    try: engine.ready(); reports.weekly_review(); db.event("INFO", "scheduler", "weekly review generated")
    except Exception as e: db.event("ERROR", "scheduler", f"weekly review failed: {e}")

def beat():
    info = {}
    try:
        if _SCHED: info = {j.id: (j.next_run_time.isoformat() if j.next_run_time else None) for j in _SCHED.get_jobs()}
    except Exception: pass
    db.heartbeat("scheduler", json.dumps(info))

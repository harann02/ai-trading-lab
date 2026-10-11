"""Scheduled job functions (importable by APScheduler's persistent job store). v2.0: US only; Bursa jobs no longer exist."""
import traceback, json
from datetime import datetime, timezone
from . import db, engine, config, calendars as cal, reports, ledger, trading, research
from .providers.yf import YFinanceProvider

_SCHED = None

def _provider(): return YFinanceProvider()

def scan_market(market="US"):
    now = datetime.now(timezone.utc)
    if market != "US": return                                   # Bursa retired in v2.0
    try:
        engine.ready()
        if not cal.is_session_day("US", cal.last_completed_session("US", now)): return
        p = _provider(); res = trading.run_scan(p, now); trading.execute_pending(p, now, refresh=False)
        try: research.generate_memos(p, now); research.daily_market_summary(now)
        except Exception as e: db.event("WARN", "ai", f"research step failed: {e}")
        db.event("INFO", "scheduler", f"US scan job finished: {json.dumps({k: v.get('counts') for k, v in res.get('strategies', {}).items() if isinstance(v, dict)})[:400]}")
    except Exception as e:
        db.event("ERROR", "scheduler", f"US scan job failed: {e}\n{traceback.format_exc()[-600:]}")

def monitor(market="US"):
    now = datetime.now(timezone.utc)
    if market != "US": return
    try:
        engine.ready()
        if not cal.is_open("US", now): return
        log = trading.execute_pending(_provider(), now)
        db.heartbeat("monitor_US", json.dumps(log)[:500])
    except Exception as e:
        db.event("ERROR", "scheduler", f"US monitor failed: {e}")

def weekly():
    try: engine.ready(); research.weekly_strategy_report(); db.event("INFO", "scheduler", "weekly strategy report generated")
    except Exception as e: db.event("ERROR", "scheduler", f"weekly report failed: {e}")

def beat():
    info = {}
    try:
        if _SCHED: info = {j.id: (j.next_run_time.isoformat() if j.next_run_time else None) for j in _SCHED.get_jobs()}
    except Exception: pass
    db.heartbeat("scheduler", json.dumps(info))

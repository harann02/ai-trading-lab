"""Scheduled job functions (importable by APScheduler's persistent job store). v2.0: US only; Bursa jobs no longer exist."""
import traceback, json
from datetime import datetime, timezone
from . import db, engine, config, calendars as cal, reports, ledger, trading, research
from .providers.yf import YFinanceProvider

_SCHED = None

def _writer(job):
    """Self-hosted scheduler may write ONLY when ledger writer mode is 'selfhosted' and it holds the lease. Records a job_runs row (heartbeat) either way."""
    import os, socket
    from .v3 import jobs as j3
    now = datetime.now(timezone.utc); holder = f"selfhosted:{socket.gethostname()}:{os.getpid()}"
    ok, why = j3.acquire_lease(holder, "selfhosted", now=now)
    if not ok: j3.record_skip(job, now, f"self-hosted scheduler not the writer: {why}"); return None, None, why
    jid = j3.begin(job, now, "schedule", None, session=str(cal.last_completed_session("US", now)))
    if jid is None: j3.release_lease(holder); return None, None, "duplicate"
    return holder, jid, "ok"

def _provider(): return YFinanceProvider()

def scan_market(market="US"):
    now = datetime.now(timezone.utc)
    if market != "US": return                                   # Bursa retired in v2.0
    engine.ready(); holder, jid, why = _writer("scan")
    if not holder: return
    from .v3 import jobs as j3
    try:
        if not cal.is_session_day("US", cal.last_completed_session("US", now)): return
        p = _provider(); res = trading.run_scan(p, now); trading.execute_pending(p, now, refresh=False)
        try: research.generate_memos(p, now); research.daily_market_summary(now)
        except Exception as e: db.event("WARN", "ai", f"research step failed: {e}")
        db.event("INFO", "scheduler", f"US scan job finished: {json.dumps({k: v.get('counts') for k, v in res.get('strategies', {}).items() if isinstance(v, dict)})[:400]}")
        j3.finish(jid, "success", None, None, now)
    except Exception as e:
        j3.finish(jid, "failed", e, None, now); db.event("ERROR", "scheduler", f"US scan job failed: {e}\n{traceback.format_exc()[-600:]}")
    finally: j3.release_lease(holder)

def monitor(market="US"):
    now = datetime.now(timezone.utc)
    if market != "US": return
    engine.ready()
    if not cal.is_open("US", now): return
    holder, jid, why = _writer("monitor")
    if not holder: return
    from .v3 import jobs as j3
    try:
        log = trading.execute_pending(_provider(), now)
        db.heartbeat("monitor_US", json.dumps(log)[:500]); j3.finish(jid, "success", None, {"orders": len(log)}, now)
    except Exception as e:
        j3.finish(jid, "failed", e, None, now); db.event("ERROR", "scheduler", f"US monitor failed: {e}")
    finally: j3.release_lease(holder)

def weekly():
    engine.ready(); holder, jid, why = _writer("weekly")
    if not holder: return
    from .v3 import jobs as j3, weekly_eval
    try: research.weekly_strategy_report(); weekly_eval.build(); db.event("INFO", "scheduler", "weekly strategy report + evaluation generated"); j3.finish(jid, "success", None, None)
    except Exception as e: j3.finish(jid, "failed", e); db.event("ERROR", "scheduler", f"weekly report failed: {e}")
    finally: j3.release_lease(holder)

def beat():
    info = {}
    try:
        if _SCHED: info = {j.id: (j.next_run_time.isoformat() if j.next_run_time else None) for j in _SCHED.get_jobs()}
    except Exception: pass
    db.heartbeat("scheduler", json.dumps(info))

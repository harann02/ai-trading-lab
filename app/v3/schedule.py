"""Scheduler reliability: expected-run calculator, missed-run detection, stale-data detection and the dashboard scheduler-health model.
Calendar = pandas_market_calendars NYSE (America/New_York: DST, holidays, early closes); shown in MYT. The monitor cron is WIDE in UTC (every 15 min 13:00-21:59 UTC Mon-Fri,
covers the summer 13:30-20:00 and winter 14:30-21:00 sessions incl. the 21:00 UTC winter close) and every run is guarded by this calendar.
Crons are OFF-GRID (monitor :04/:19/:34/:49, scan :41, weekly 02:13) so no two schedules share a minute (GitHub delivered only ONE run when the 21:30 scan and the
*/15 monitor collided) and they avoid the :00/:15/:30/:45 load peaks where GitHub delays/drops scheduled runs. Slot classification is by INTENT (classify()), not by
which cron string fired: a delayed or dropped scan slot is taken by the next unattended run inside the session's EOD-scan window."""
import json
from datetime import datetime, timezone, timedelta, date
from .. import db, calendars as cal

MONITOR_STEP_MIN = 15
MONITOR_OFFSET_MIN = 4       # cron '4,19,34,49 13-21 * * 1-5'
POST_CLOSE_MIN = 25          # candles of the last bar become visible ~20 min after the close (5m bar + 15 min delay)
SCAN_UTC = (21, 41)          # cron '41 21 * * 1-5' (off-grid; no monitor minute coincides)
SCAN_EARLIEST_AFTER_CLOSE_MIN = 30   # EOD-scan window opens at max(close + 30 min, 21:30 UTC of the session day) ...
SCAN_EARLIEST_UTC = (21, 30)         # ... and stays open until the next session's open
WEEKLY_CRON = "13 2 * * 6"
TOL_MIN = 25

def monitor_window(now):
    """(in_window, session_date, open_utc, close_utc, reason)."""
    d = now.astimezone(cal.NY).date(); h = cal.session_hours("US", d)
    if not h: return False, None, None, None, f"{d} is not an NYSE session (weekend/holiday)"
    o, c = h
    if o <= now <= c + timedelta(minutes=POST_CLOSE_MIN): return True, d, o, c, "in session window"
    return False, d, o, c, f"outside NYSE hours ({o.astimezone(cal.NY):%H:%M}-{c.astimezone(cal.NY):%H:%M} ET)"

def expected_monitor_runs(start_utc, end_utc):
    """All 15-minute cron slots (UTC) that fall inside a monitor window of an NYSE session between start and end."""
    out = []; d = (start_utc - timedelta(days=1)).astimezone(cal.NY).date()
    while d <= end_utc.astimezone(cal.NY).date() + timedelta(days=1):
        h = cal.session_hours("US", d)
        if h:
            o, c = h; t = o.replace(minute=(o.minute // MONITOR_STEP_MIN) * MONITOR_STEP_MIN + MONITOR_OFFSET_MIN, second=0, microsecond=0)
            while t - timedelta(minutes=MONITOR_STEP_MIN) >= o: t -= timedelta(minutes=MONITOR_STEP_MIN)
            while t < o: t += timedelta(minutes=MONITOR_STEP_MIN)
            while t <= c + timedelta(minutes=POST_CLOSE_MIN):
                if start_utc <= t <= end_utc: out.append(t)
                t += timedelta(minutes=MONITOR_STEP_MIN)
        d += timedelta(days=1)
    return out

def expected_scans(start_utc, end_utc):
    """One scan per NYSE session, due at 21:30 UTC of the session day (after the close in both EDT and EST)."""
    out = []; d = start_utc.date() - timedelta(days=1)
    while d <= end_utc.date():
        if cal.session_hours("US", d):
            t = datetime(d.year, d.month, d.day, SCAN_UTC[0], SCAN_UTC[1], tzinfo=timezone.utc)
            if start_utc <= t <= end_utc: out.append((d, t))
        d += timedelta(days=1)
    return out

def eod_scan_window(session):
    """[start, end) UTC during which an unattended run performs the EOD scan for `session`: after the close (+ final bars), before the next open."""
    h = cal.session_hours("US", session)
    if not h: return None
    floor = datetime(session.year, session.month, session.day, SCAN_EARLIEST_UTC[0], SCAN_EARLIEST_UTC[1], tzinfo=timezone.utc)
    start = max(h[1] + timedelta(minutes=SCAN_EARLIEST_AFTER_CLOSE_MIN), floor)
    nx = cal.schedule("US", session + timedelta(days=1), session + timedelta(days=10))
    end = nx.iloc[0]["market_open"].to_pydatetime() if len(nx) else h[1] + timedelta(days=3)
    return start, end

def eod_scan_done(session):
    """True if a successful scan / catch-up scan is recorded for `session` (ANY trigger). Used for idempotency only, never as verification evidence."""
    w = eod_scan_window(session)
    if not w: return False
    h = cal.session_hours("US", session); c = db.conn()
    try:
        return bool(c.execute("SELECT 1 FROM job_runs WHERE job IN ('scan','catchup_scan') AND status='success' AND started_ts>=? AND started_ts<? LIMIT 1",
                              (h[1].isoformat(timespec="seconds"), w[1].isoformat(timespec="seconds"))).fetchone())
    finally: c.close()

def classify(now, scan_done=None):
    """Intent-based classification of an UNATTENDED run (cron `schedule` or external scheduler), independent of which cron string fired.
    -> (action, session|None, reason). 'scan' iff now is inside the EOD-scan window of the last completed session and no scan is recorded for it yet
    (idempotent: once one succeeded, every later run in the window is a monitor); otherwise 'monitor' (calendar-gated as before)."""
    scan_done = scan_done or eod_scan_done
    sess = cal.last_completed_session("US", now); w = eod_scan_window(sess) if sess else None
    if w and w[0] <= now < w[1]:
        if not scan_done(sess): return "scan", sess, f"EOD scan due for session {sess} (window {w[0]:%Y-%m-%d %H:%M}-{w[1]:%Y-%m-%d %H:%M} UTC, none recorded yet)"
        return "monitor", sess, f"EOD scan for {sess} already recorded: running as monitor"
    return "monitor", None, "not in an EOD-scan window"

def _runs(since):
    c = db.conn(); r = [dict(x) for x in c.execute("SELECT * FROM job_runs WHERE started_ts>=? ORDER BY started_ts", (since.isoformat(timespec="seconds"),))]; c.close(); return r

def missed_runs(now, lookback_h=26, tol_min=TOL_MIN, runs=None):
    """Expected slots with NO executed run (success/failed/running) near them. Skips never satisfy an expectation, duplicates neither.
    A scan slot is satisfied by a successful scan or catch-up scan for that session at any later time."""
    start = now - timedelta(hours=lookback_h); runs = runs if runs is not None else _runs(start - timedelta(hours=2)); miss = []
    ex = [r for r in runs if r["status"] in ("success", "failed", "running") and r["job"] in ("monitor", "scan", "catchup_scan", "control", "export", "weekly")]
    for t in expected_monitor_runs(start, now - timedelta(minutes=tol_min)):
        if not any(r["job"] in ("monitor", "scan", "catchup_scan") and t - timedelta(minutes=2) <= datetime.fromisoformat(r["started_ts"]) <= t + timedelta(minutes=tol_min) for r in ex):
            miss.append({"job": "monitor", "expected": t.isoformat(timespec="seconds")})
    for d, t in expected_scans(start, now - timedelta(minutes=tol_min)):
        nx = cal.schedule("US", d + timedelta(days=1), d + timedelta(days=10)); nxt_open = nx.iloc[0]["market_open"].to_pydatetime() if len(nx) else t + timedelta(days=3)
        # satisfied only by a successful scan/catch-up scan that started after this session's scan slot and BEFORE the next session opens (later runs do not hide a miss)
        w0 = (eod_scan_window(d) or (t,))[0]                   # intent-based scan: any unattended run inside the EOD window may perform it (may start before the :41 slot)
        ok = [r for r in ex if r["job"] in ("scan", "catchup_scan") and r["status"] == "success" and min(t, w0) - timedelta(minutes=5) <= datetime.fromisoformat(r["started_ts"]) < nxt_open]
        if not ok: miss.append({"job": "scan", "expected": t.isoformat(timespec="seconds"), "session": str(d)})
    return miss

def data_freshness(now, cfg=None):
    from .. import data
    spy = data.load_bars("SPY", include_partial=False)
    last = spy.index[-1].date() if len(spy) else None; exp = cal.last_completed_session("US", now)
    lag = (len(cal.schedule("US", last, exp)) - 1) if last and last < exp else 0
    # exchange timestamp = the bar's session date; retrieval timestamp = when our provider call fetched that bar (bars.fetched_at). Neither is the dashboard refresh time.
    retrieved = None
    try:
        c = db.conn(); r = c.execute("SELECT fetched_at FROM bars WHERE symbol='SPY' AND final=1 ORDER BY date DESC LIMIT 1").fetchone(); c.close(); retrieved = r["fetched_at"] if r else None
    except Exception: pass
    return {"spy_last_final_bar": str(last) if last else None, "exchange_session_date": str(last) if last else None, "retrieved_utc": retrieved, "provider": "yfinance (free, end-of-day final bars)",
            "expected_last_completed_session": str(exp), "sessions_behind": lag, "stale": bool(lag > 0 or last is None)}

def health(now=None, cfg=None):
    """Dashboard scheduler-health model. Distinguishes DEPLOYMENT status (the Pages snapshot) from ENGINE execution status (job_runs)."""
    now = now or datetime.now(timezone.utc); cfg = cfg or db.get_settings(); c = db.conn()
    last_sched = c.execute("SELECT * FROM job_runs WHERE trigger='schedule' AND status!='skipped' ORDER BY started_ts DESC LIMIT 1").fetchone()
    last_ok = c.execute("SELECT * FROM job_runs WHERE status='success' ORDER BY started_ts DESC LIMIT 1").fetchone()
    last_scan = c.execute("SELECT * FROM job_runs WHERE job IN ('scan','catchup_scan') AND status='success' ORDER BY started_ts DESC LIMIT 1").fetchone()
    last_err = c.execute("SELECT * FROM job_runs WHERE status='failed' ORDER BY started_ts DESC LIMIT 1").fetchone()
    week = (now - timedelta(days=7)).isoformat(timespec="seconds")
    cnt = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM job_runs WHERE started_ts>=? GROUP BY status", (week,))}
    skips = [dict(r) for r in c.execute("SELECT started_ts, job, skip_reason FROM job_runs WHERE status='skipped' ORDER BY started_ts DESC LIMIT 8")]
    hb = {r["name"]: r["ts"] for r in c.execute("SELECT name,ts FROM heartbeat")}
    ev_err = c.execute("SELECT ts,source,message FROM events WHERE level IN ('ERROR','CRITICAL') ORDER BY id DESC LIMIT 1").fetchone(); c.close()
    miss = missed_runs(now); fr = data_freshness(now, cfg); us = cal.us_status(now)
    nxt = None
    inwin = monitor_window(now)
    t = now
    for e in expected_monitor_runs(now, now + timedelta(days=5)):
        if e > now: nxt = e; break
    ns = [x for x in expected_scans(now, now + timedelta(days=5)) if x[1] > now]
    n_runs = (cnt.get("success", 0) + cnt.get("failed", 0) + cnt.get("running", 0))
    status = "NO DATA"
    if last_ok:
        age_h = (now - datetime.fromisoformat(last_ok["started_ts"])).total_seconds() / 3600
        recent_miss = [m for m in miss if datetime.fromisoformat(m["expected"]) > now - timedelta(hours=6)]
        if last_err and last_ok and last_err["started_ts"] > last_ok["started_ts"]: status = "DEGRADED (latest run failed)"
        elif fr["stale"] and fr["sessions_behind"] > 1: status = "DEGRADED (stale data)"
        elif recent_miss and inwin[0]: status = "DEGRADED (missed runs)"
        elif not us["open"] and age_h > 80: status = "STALE"
        else: status = "HEALTHY" if not recent_miss else "HEALTHY (earlier missed runs recorded)"
    j = lambda r: dict(r) if r else None
    return {"status": status, "market": us["phase"], "market_open": us["open"], "last_scheduled_run": j(last_sched), "last_successful_run": j(last_ok), "last_scan": j(last_scan),
            "last_reconciliation": hb.get("last_reconciliation"), "next_expected_monitor_utc": nxt.isoformat(timespec="minutes") if nxt else None, "next_expected_scan_utc": ns[0][1].isoformat(timespec="minutes") if ns else None,
            "next_expected_scan_myt": ns[0][1].astimezone(cal.MYT).strftime("%Y-%m-%d %H:%M MYT") if ns else None,
            "data_freshness": fr, "latest_error": ({"ts": last_err["started_ts"], "job": last_err["job"], "error": last_err["error"]} if last_err else (dict(ev_err) if ev_err else None)),
            "counts_7d": {"success": cnt.get("success", 0), "failed": cnt.get("failed", 0), "skipped": cnt.get("skipped", 0), "running": cnt.get("running", 0)}, "missed_runs_26h": miss[-20:], "n_missed_26h": len(miss),
            "recent_skips": skips, "writer_mode": __import__("app.v3.jobs", fromlist=["x"]).writer_mode(),
            "deployment_vs_engine": "DEPLOYMENT = the static Pages snapshot you are viewing (generated by the last exporting run). ENGINE = scheduled jobs recorded in job_runs. A green deploy does not prove the engine ran on schedule.",
            "cron_note": "GitHub Actions cron is best-effort: runs can start late or be dropped; the watchdog opens an issue and the next run self-heals (catch-up scan)."}

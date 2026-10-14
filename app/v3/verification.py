"""Forward Trading Verification: tracks consecutive scheduled US sessions of REAL operation with 15 recorded items each (see docs/FORWARD_VERIFICATION.md).
Rules (pre-declared, enforced in code):
  * start session = the first NYSE session whose OPEN is after the v3 migration timestamp (ledger_meta.v3_applied_ts)
  * a session QUALIFIES only if: scan occurred on schedule (job_run success with a real run id, before the next open), data retrieved, stocks evaluated > 0, positions
    reconciled, P&L identity correct, daily report generated, 0 errors, 0 stale feeds, and monitor coverage >= 75% of expected slots (no 2h+ gap). A session with NO trades
    qualifies if every check completed (cash is a valid decision).
  * a session with no record after its deadline (next open) FAILS ("missed run").
  * status: NOT STARTED | IN PROGRESS | OPERATIONALLY VERIFIED (>= 20 consecutive qualifying sessions) | ACTION REQUIRED (latest completed session failed; streak reset) | FAILED (3+ failed sessions since start)
  * OPERATIONALLY VERIFIED can never be reached with fewer than 20 qualifying consecutive sessions, nor from sessions without a real run id (tests/demo never count)."""
import json
from datetime import datetime, timezone, timedelta, date
from .. import db, calendars as cal
from . import schedule as S

UNATTENDED = ("schedule", "external_scheduler")          # the ONLY trigger classes that count (manual workflow_dispatch / push never count)
SOURCE_LABEL = {"schedule": "github-schedule", "external_scheduler": "external-backup"}
REQUIRED = 20; MIN_COVERAGE = 0.75; MAX_GAP_MIN = 120; FAIL_LIMIT = 3
ITEMS = ["scan_occurred", "data_retrieved", "stocks_evaluated", "candidates", "passed_risk", "proposed", "submitted", "rejected", "filled", "positions_reconciled", "pnl_correct", "daily_report", "errors", "stale_feeds", "missed_runs"]

def _meta(key):
    c = db.conn(); r = c.execute("SELECT value FROM ledger_meta WHERE key=?", (key,)).fetchone(); c.close(); return r["value"] if r else None

def start_session():
    ts = _meta("v3_applied_ts")
    if not ts: return None
    t = datetime.fromisoformat(ts); sch = cal.schedule("US", t.astimezone(cal.NY).date(), t.astimezone(cal.NY).date() + timedelta(days=10))
    for idx, r in sch.iterrows():
        if r["market_open"].to_pydatetime() > t: return idx.date()
    return None

def record_scan(session, summary, now):
    """Called by the scan job: stores the scan-time counts (immutable thereafter)."""
    st = (summary.get("strategies") or {}); sw = (st.get("swing") or {}).get("counts") or {}
    uni = summary.get("universe") or {}; dat = summary.get("data") or {}
    vals = {"data_retrieved": int(bool(dat.get("symbols_ok", 0) > 0) and "aborted" not in summary), "stocks_evaluated": int(uni.get("passed") or 0), "candidates": int(uni.get("candidates") or 0),
            "passed_risk": int(sw.get("BUY ELIGIBLE", 0)), "stale_feeds": int(len([1 for s in (summary.get("stale") or [])]))}
    c = db.conn(); c.execute("INSERT OR IGNORE INTO forward_sessions(session_date,status,updated_ts) VALUES(?,?,?)", (str(session), "PENDING", now.isoformat(timespec="seconds")))
    c.execute("UPDATE forward_sessions SET data_retrieved=?, stocks_evaluated=?, candidates=?, passed_risk=?, stale_feeds=COALESCE(stale_feeds,?) WHERE session_date=? AND candidates IS NULL",
              (vals["data_retrieved"], vals["stocks_evaluated"], vals["candidates"], vals["passed_risk"], vals["stale_feeds"], str(session)))
    c.commit(); c.close()

def reconcile_checks(now=None):
    """Independent identity checks on the ledger-derived numbers. ok only if every check passes."""
    from .. import portfolio, ledger
    now = now or datetime.now(timezone.utc); rows = ledger.rows(); snap = portfolio.snapshot(now); chk = []
    cash_myr = sum(r["cash_delta_myr"] for r in rows)
    chk.append({"name": "equity = MYR cash + sum(sleeve equity)", "pass": abs(snap["equity_myr"] - (cash_myr + sum(s["equity_myr"] for s in snap["sleeves"].values()))) < 1e-6, "detail": f"{snap['equity_myr']:.4f}"})
    usd = sum(r.get("cash_delta_usd") or 0 for r in rows); chk.append({"name": "USD cash conservation (ledger = sleeves)", "pass": abs(usd - sum(s["cash_usd"] for s in snap["sleeves"].values())) < 1e-6, "detail": f"{usd:.4f}"})
    chk.append({"name": "no negative cash", "pass": cash_myr >= -1e-6 and all(s["cash_usd"] >= -1e-6 for s in snap["sleeves"].values()), "detail": ""})
    dep = sum(r["cash_delta_myr"] for r in rows if r["kind"] in ("DEPOSIT", "WITHDRAW"))
    chk.append({"name": "net contributions unchanged by journal entries", "pass": all(r["cash_delta_myr"] == 0 for r in rows if r["kind"].startswith("SLEEVE_XFER")), "detail": f"deposits RM{dep:.2f}"})
    c = db.conn(); filled = c.execute("SELECT COUNT(*) n FROM orders WHERE status='filled' AND strategy_id IS NOT NULL").fetchone()["n"]; led = c.execute("SELECT COUNT(*) n FROM ledger WHERE kind IN ('BUY','SELL') AND strategy_id IS NOT NULL").fetchone()["n"]; c.close()
    chk.append({"name": "filled orders = ledger BUY/SELL rows", "pass": filled == led, "detail": f"orders {filled} / ledger {led}"})
    v = {"ts": now.isoformat(timespec="seconds"), "ok": all(x["pass"] for x in chk), "checks": chk}
    db.heartbeat("last_reconciliation", json.dumps(v)); return v

def _session_window(d):
    h = cal.session_hours("US", d)
    nx = cal.schedule("US", d + timedelta(days=1), d + timedelta(days=10)); nxt = nx.iloc[0]["market_open"].to_pydatetime() if len(nx) else None
    return h, nxt

def compute_session(d, now):
    """Builds the 15 items for session d from stored evidence."""
    h, nxt = _session_window(d); c = db.conn(); item = {k: None for k in ITEMS}; notes = []
    o, cl = h; scan_slot = datetime(d.year, d.month, d.day, S.SCAN_UTC[0], S.SCAN_UTC[1], tzinfo=timezone.utc)
    scans = [dict(r) for r in c.execute("SELECT * FROM job_runs WHERE job IN ('scan','catchup_scan') AND status='success' AND started_ts>=? ORDER BY started_ts", ((cl).isoformat(timespec="seconds"),))]
    scans = [r for r in scans if (nxt is None or datetime.fromisoformat(r["started_ts"]) < nxt)]
    # UNATTENDED evidence = runs started by the GitHub cron `schedule` event OR the external backup cron (repository_dispatch type external-cron -> trigger
    # class 'external_scheduler'; user-approved to count). Each is labelled by source. workflow_dispatch / push / manual runs are shown separately and NEVER count.
    real = [r for r in scans if r.get("run_id") and r.get("trigger") in UNATTENDED]; item["scan_occurred"] = int(bool(real))
    item["scan_source"] = ("+".join(sorted({SOURCE_LABEL[r["trigger"]] for r in real})) if real else None)
    ext = [r for r in real if r.get("trigger") == "external_scheduler"]
    if ext: notes.append(f"{len(ext)} scan run(s) from the external backup cron (repository_dispatch, labelled external-backup): counted as unattended evidence")
    manual = [r for r in scans if r.get("run_id") and r.get("trigger") not in UNATTENDED]
    if manual: notes.append(f"{len(manual)} manual/push scan run(s) ({', '.join(sorted({str(r.get('trigger')) for r in manual}))}) shown separately: NOT counted as unattended evidence")
    if scans and not real and not manual: notes.append("scan has no real run id (not counted as real evidence)")
    on_time = any(r["job"] == "scan" for r in real)
    fs = c.execute("SELECT * FROM forward_sessions WHERE session_date=?", (str(d),)).fetchone(); fs = dict(fs) if fs else {}
    for k in ("data_retrieved", "stocks_evaluated", "candidates", "passed_risk"): item[k] = fs.get(k)
    ds, de = o.isoformat(timespec="seconds"), (nxt or cl + timedelta(days=3)).isoformat(timespec="seconds")
    item["proposed"] = c.execute("SELECT COUNT(*) n FROM decisions WHERE scan_date=? AND strategy_id IS NOT NULL AND decision='BUY ELIGIBLE'", (str(d),)).fetchone()["n"]
    item["submitted"] = c.execute("SELECT COUNT(*) n FROM orders WHERE signal_ts>=? AND signal_ts<? AND strategy_id IS NOT NULL AND side='BUY' AND needs_approval=0", (cl.isoformat(timespec="seconds"), de)).fetchone()["n"]
    item["rejected"] = c.execute("SELECT COUNT(*) n FROM order_events WHERE status IN ('rejected','canceled') AND ts>=? AND ts<?", (ds, de)).fetchone()["n"]
    item["filled"] = c.execute("SELECT COUNT(*) n FROM order_events WHERE status='filled' AND ts>=? AND ts<?", (ds, de)).fetchone()["n"]
    item["errors"] = c.execute("SELECT COUNT(*) n FROM events WHERE level IN ('ERROR','CRITICAL') AND ts>=? AND ts<?", (ds, de)).fetchone()["n"] + c.execute("SELECT COUNT(*) n FROM job_runs WHERE status='failed' AND started_ts>=? AND started_ts<?", (ds, de)).fetchone()["n"]
    rep = c.execute("SELECT 1 FROM reports WHERE kind='daily_ai' AND period=?", (str(d),)).fetchone(); item["daily_report"] = int(bool(rep))
    rc = c.execute("SELECT ts,info FROM heartbeat WHERE name='last_reconciliation'").fetchone()
    ok_rec = False
    if rc:
        try: v = json.loads(rc["info"]); ok_rec = bool(v.get("ok")) and ds <= rc["ts"] < de
        except Exception: ok_rec = False
    item["positions_reconciled"] = int(ok_rec); item["pnl_correct"] = int(ok_rec)
    sf = c.execute("SELECT stale_feeds FROM forward_sessions WHERE session_date=?", (str(d),)).fetchone(); item["stale_feeds"] = (sf["stale_feeds"] if sf and sf["stale_feeds"] is not None else 0)
    # monitor coverage during the session
    exp = S.expected_monitor_runs(o, cl); mon = [dict(r) for r in c.execute("SELECT started_ts, trigger FROM job_runs WHERE trigger IN ('schedule','external_scheduler') AND job IN ('monitor','scan','catchup_scan','control') AND status IN ('success','failed') AND started_ts>=? AND started_ts<=?", ((o - timedelta(minutes=5)).isoformat(timespec="seconds"), (cl + timedelta(minutes=40)).isoformat(timespec="seconds")))]
    ran = [datetime.fromisoformat(r["started_ts"]) for r in mon]
    item["monitor_runs_by_source"] = {lab: len([r for r in mon if r["trigger"] == t]) for t, lab in SOURCE_LABEL.items()}
    hit = [e for e in exp if any(e - timedelta(minutes=2) <= r <= e + timedelta(minutes=S.TOL_MIN) for r in ran)]
    cov = (len(hit) / len(exp)) if exp else 1.0
    gap = 0; last = o
    for r in sorted([x for x in ran if o <= x <= cl]) + [cl]:
        gap = max(gap, (r - last).total_seconds() / 60); last = r
    item["missed_runs"] = (len(exp) - len(hit)) + (0 if on_time or not real else 1)
    if not on_time and real: notes.append("scan ran only as catch-up (scheduled scan missed)")
    c.close()
    fails = []
    if not item["scan_occurred"]: fails.append("no real scan before the next open (missed run)")
    if item["data_retrieved"] != 1: fails.append("data not retrieved")
    if not (item["stocks_evaluated"] or 0) > 0: fails.append("no stocks evaluated")
    if not item["positions_reconciled"]: fails.append("positions not reconciled")
    if not item["pnl_correct"]: fails.append("P&L identity not confirmed")
    if not item["daily_report"]: fails.append("daily report missing")
    if item["errors"]: fails.append(f"{item['errors']} error(s)")
    if item["stale_feeds"]: fails.append("stale feed(s)")
    if cov < MIN_COVERAGE: fails.append(f"monitor coverage {cov*100:.0f}% < {MIN_COVERAGE*100:.0f}%")
    if gap > MAX_GAP_MIN: fails.append(f"monitor gap {gap:.0f} min > {MAX_GAP_MIN}")
    if not on_time and real: fails.append("scheduled scan missed (catch-up only)")
    item["monitor_coverage_pct"] = round(cov * 100, 1); item["max_monitor_gap_min"] = round(gap)
    return item, fails, notes

def update_all(now=None):
    """Recomputes every session from the start session to the last completed one (idempotent) and stores the rows."""
    now = now or datetime.now(timezone.utc); st = start_session()
    if not st: return []
    last = cal.last_completed_session("US", now); sch = cal.schedule("US", st, last) if st <= last else []
    out = []
    for idx in sch.index:
        d = idx.date(); h, nxt = _session_window(d)
        deadline = nxt or (h[1] + timedelta(days=3))
        item, fails, notes = compute_session(d, now)
        final = now >= deadline or (not fails)
        status = "QUALIFIED" if not fails else ("FAILED" if now >= deadline else "PENDING")
        if status == "PENDING" and not item["scan_occurred"] and now < deadline: pass
        q = int(status == "QUALIFIED"); upd = now.isoformat(timespec="seconds")
        c = db.conn(); c.execute("INSERT OR IGNORE INTO forward_sessions(session_date,updated_ts) VALUES(?,?)", (str(d), upd))
        c.execute("UPDATE forward_sessions SET scan_occurred=?, proposed=?, submitted=?, rejected=?, filled=?, positions_reconciled=?, pnl_correct=?, daily_report=?, errors=?, missed_runs=?, status=?, qualifies=?, notes=?, updated_ts=? WHERE session_date=?",
                  (item["scan_occurred"], item["proposed"], item["submitted"], item["rejected"], item["filled"], item["positions_reconciled"], item["pnl_correct"], item["daily_report"], item["errors"], item["missed_runs"], status, q,
                   json.dumps({"fails": fails, "notes": notes, "scan_source": item.get("scan_source"), "monitor_runs_by_source": item.get("monitor_runs_by_source"), "monitor_coverage_pct": item["monitor_coverage_pct"], "max_monitor_gap_min": item["max_monitor_gap_min"]}), upd, str(d)))
        c.commit(); c.close(); out.append((d, status, fails))
    return out

def summary(now=None):
    now = now or datetime.now(timezone.utc); st = start_session(); c = db.conn(); rows = [dict(r) for r in c.execute("SELECT * FROM forward_sessions ORDER BY session_date")]; c.close()
    for r in rows:
        try: r["detail"] = json.loads(r.get("notes") or "{}")
        except Exception: r["detail"] = {}
    done = [r for r in rows if r["status"] in ("QUALIFIED", "FAILED")]
    streak = 0
    for r in reversed(done):
        if r["status"] == "QUALIFIED": streak += 1
        else: break
    n_failed = len([r for r in done if r["status"] == "FAILED"]); total_q = len([r for r in done if r["status"] == "QUALIFIED"])
    if not st or not done: status = "NOT STARTED"
    elif streak >= REQUIRED: status = "OPERATIONALLY VERIFIED"
    elif n_failed >= FAIL_LIMIT: status = "FAILED"
    elif done[-1]["status"] == "FAILED": status = "ACTION REQUIRED"
    else: status = "IN PROGRESS"
    nxt_session = None
    try:
        cs = cal.current_or_next_session("US", now); nxt_session = str(cs[0]) if cs else None
    except Exception: pass
    c = db.conn(); manual_runs = [dict(r) for r in c.execute("SELECT job,trigger,started_ts,status,run_id FROM job_runs WHERE trigger NOT IN ('schedule','external_scheduler') AND status!='skipped' AND run_id IS NOT NULL ORDER BY id DESC LIMIT 12")]
    sched_runs = c.execute("SELECT COUNT(*) n FROM job_runs WHERE trigger='schedule' AND status IN ('success','failed')").fetchone()["n"]
    ext_runs = c.execute("SELECT COUNT(*) n FROM job_runs WHERE trigger='external_scheduler' AND status IN ('success','failed')").fetchone()["n"]
    ext_list = [dict(r) for r in c.execute("SELECT job,trigger,started_ts,status,run_id FROM job_runs WHERE trigger='external_scheduler' AND status!='skipped' ORDER BY id DESC LIMIT 12")]
    def _scan_sessions(t): return c.execute("SELECT COUNT(DISTINCT session) n FROM (SELECT substr(idem_key, instr(idem_key, ':')+1) session FROM job_runs WHERE trigger=? AND job IN ('scan','catchup_scan') AND status='success' AND run_id IS NOT NULL)", (t,)).fetchone()["n"]
    ext_scan_sessions = _scan_sessions("external_scheduler"); sched_scan_sessions = _scan_sessions("schedule"); c.close()
    for r in rows: r["scan_source"] = r["detail"].get("scan_source"); r["monitor_runs_by_source"] = r["detail"].get("monitor_runs_by_source")
    qsrc = {}
    for r in done:
        if r["status"] == "QUALIFIED": k = r.get("scan_source") or "unknown"; qsrc[k] = qsrc.get(k, 0) + 1
    breakdown = {"github-schedule": {"runs_recorded": sched_runs, "scan_sessions": sched_scan_sessions}, "external-backup": {"runs_recorded": ext_runs, "scan_sessions": ext_scan_sessions},
                 "qualifying_sessions_by_scan_source": qsrc, "manual_or_push_runs_listed_not_counted": len(manual_runs)}
    return {"counted_basis": "sessions are counted ONLY from UNATTENDED runs: GitHub cron (event 'schedule', labelled github-schedule) AND the external backup cron (repository_dispatch type external-cron, trigger class external_scheduler, labelled external-backup). Manual workflow_dispatch / push runs are listed separately and never count",
            "unattended_triggers": list(UNATTENDED), "source_labels": SOURCE_LABEL, "source_breakdown": breakdown,
            "scheduled_runs_recorded": sched_runs, "manual_runs_not_counted": manual_runs, "unattended_runs_recorded": sched_runs + ext_runs,
            "external_scheduler_runs_recorded": ext_runs, "external_scheduler_scan_sessions": ext_scan_sessions, "external_scheduler_runs": ext_list,
            "external_scheduler_basis": "COUNTED as unattended evidence (user-approved): runs started by the external backup cron (repository_dispatch type external-cron) count toward the 20-session verification and are labelled 'external-backup' per run/session, separately from 'github-schedule'.",
            "status": status, "required_sessions": REQUIRED, "consecutive_qualifying": streak, "total_qualifying": total_q, "failed_sessions": n_failed, "start_session": str(st) if st else None,
            "sessions": rows[-40:], "items": ITEMS, "next_session": nxt_session, "started_after_v3_migration_ts": _meta("v3_applied_ts"),
            "honesty": f"{streak} of {REQUIRED} qualifying consecutive sessions completed. Nothing is claimed beyond recorded evidence. Sessions are only counted after their deadline (next open) with a real run id."}

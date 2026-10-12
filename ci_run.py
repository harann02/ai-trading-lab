"""One CI run (GitHub Actions). Usage: python ci_run.py auto|scan|monitor|control|weekly|export
'auto' = unattended run (cron `schedule` or external `repository_dispatch`): scan vs monitor is decided by INTENT (app/v3/schedule.classify), idempotently per session.
v3 flow: ingest gate skips -> single-writer lease (mode 'actions') -> idempotent job record (job_runs) -> apply owner control (file + workflow_dispatch inputs; validated, audited)
-> action -> reconciliation/verification -> release lease. A failed engine run is RECORDED in job_runs, persisted, and the process exits non-zero at the end (workflow shows red).
Writes $GITHUB_OUTPUT: changed=true|false, engine_status=ok|failed|skipped|duplicate."""
import sys, os, json, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime, timezone
from app import engine, trading, db, control, research, calendars as cal, config
from app.providers.yf import YFinanceProvider
from app.v3 import jobs, schedule as S, verification as V, shadow, daily_report, weekly_eval

def _out(**kw):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            for k, v in kw.items(): f.write(f"{k}={v}\n")

def _scan_done_for(session, now):
    h = cal.session_hours("US", session); c = db.conn()
    r = c.execute("SELECT 1 FROM job_runs WHERE job IN ('scan','catchup_scan') AND status='success' AND started_ts>=? LIMIT 1", (h[1].isoformat(timespec="seconds"),)).fetchone(); c.close(); return bool(r)

def do_scan(prov, now, job):
    s = trading.run_scan(prov, now); sd = cal.last_completed_session("US", now)
    print(json.dumps({k: v for k, v in s.items() if k not in ("capabilities",)}, default=str)[:1500])
    try: print("dividends:", shadow.fetch_dividends(now=now))
    except Exception as e: db.event("WARN", "data", f"dividend fetch failed: {e}")
    try: research.generate_memos(prov, now); research.daily_market_summary(now)
    except Exception as e: db.event("WARN", "ai", f"research step failed: {e}")
    log = trading.execute_pending(prov, now, refresh=cal.is_open("US", now)); db.heartbeat("monitor_US", json.dumps(log)[:500])
    rec = V.reconcile_checks(now); trading.reconcile(now)
    try: shadow.build(now)
    except Exception as e: db.event("WARN", "shadow", f"shadow benchmark build failed: {e}")
    try: V.record_scan(sd, s, now)
    except Exception as e: db.event("WARN", "verification", f"record_scan failed: {e}")
    try: daily_report.build(sd, now)
    except Exception as e: db.event("WARN", "report", f"daily report failed: {e}\n{traceback.format_exc()[-300:]}")
    return {"scan": {k: s.get(k) for k in ("session", "universe", "aborted")}, "reconciliation_ok": rec["ok"]}

def main(a):
    now = datetime.now(timezone.utc); engine.ready(); changed = False; status = "ok"
    ev = jobs.trigger_class(os.environ.get("GITHUB_EVENT_NAME", "manual")); unattended = ev in ("schedule", "external_scheduler"); run_id = os.environ.get("GITHUB_RUN_ID"); actor = os.environ.get("GITHUB_ACTOR", "owner")
    cdir = os.environ.get("AITL_CONTROL_DIR"); holder = f"actions:{run_id or 'local'}"
    if cdir:
        try: jobs.ingest_gate_skips(os.path.join(os.path.dirname(cdir.rstrip('/')), "status", "skips.json"))
        except Exception as e: print("skip ingest failed", e)
    ok, why = jobs.acquire_lease(holder, "actions", now=now, run_id=run_id)
    if not ok:
        jobs.record_skip(a, now, f"writer lease: {why}", ev, run_id); print("SKIPPED:", why); _out(changed="true", engine_status="skipped"); return
    cls = None
    if a == "auto":
        a, sess, why = S.classify(now); cls = {"classified_as": a, "reason": why, "trigger": ev}; print("classify:", json.dumps(cls))
        if a == "scan" and S.eod_scan_done(sess):              # re-checked under the writer lease (idempotent: never a second scan for one session)
            a = "monitor"; cls["reason"] += "; scan recorded meanwhile -> monitor"
    sd_prev = cal.last_completed_session("US", now); job = {"scan": "scan", "scan_all": "scan", "monitor": "monitor", "weekly": "weekly", "control": "control", "export": "export"}.get(a, a)
    jid = jobs.begin(job, now, ev, run_id, session=str(sd_prev))
    if jid is None:
        print("duplicate run prevented by idempotency key"); jobs.release_lease(holder); _out(changed="false", engine_status="duplicate"); return
    err = None
    try:
        db.event("INFO", "actions", f"GitHub Actions run: {a} (event {ev}, run {run_id})")
        if cdir:
            res = control.apply_file(os.path.join(cdir, "commands.json"), now)
            if res: changed = True; print("control:", json.dumps(res, default=str)[:600])
        if os.environ.get("AITL_INPUT_SETTINGS") or os.environ.get("AITL_INPUT_COMMAND"):
            res = control.apply_dispatch(os.environ.get("AITL_INPUT_SETTINGS"), os.environ.get("AITL_INPUT_COMMAND"), actor, run_id, now); changed = True; print("dispatch control:", json.dumps(res, default=str)[:600])
        prov = YFinanceProvider()
        try: from app import ai; st = ai.status(); print("AI status:", st["label"], json.dumps(st["probes"])[:400]); db.event("INFO", "ai", "AI status: " + st["label"])
        except Exception as e: print("AI status failed", e)
        detail = {"classification": cls} if cls else {}
        if a in ("scan", "scan_all"):
            detail = do_scan(prov, now, job); changed = True
        elif a == "monitor":
            inwin = S.monitor_window(now)
            if unattended and not inwin[0]:
                jobs.finish(jid, "skipped", None, {"reason": inwin[4]}, now); jobs.record_skip("monitor", now, inwin[4], ev, run_id); db.heartbeat("last_ci_run", "monitor-skipped"); jobs.release_lease(holder); _out(changed="false", engine_status="skipped"); return
            prev = cal.last_completed_session("US", now)
            if not _scan_done_for(prev, now):                       # self-healing: the scheduled scan for the last session never ran -> catch-up scan now (orders become eligible next session, honest)
                jid2 = jobs.begin("catchup_scan", now, ev, run_id, session=str(prev))
                if jid2:
                    try: detail["catchup"] = do_scan(prov, now, "catchup_scan"); jobs.finish(jid2, "success", None, detail["catchup"], now)
                    except Exception as e: jobs.finish(jid2, "failed", e, None, now); raise
                    changed = True
            log = trading.execute_pending(prov, now, refresh=cal.is_open("US", now)); db.heartbeat("monitor_US", json.dumps(log)[:500]); detail["orders"] = len(log)
            if any(r[1] == "filled" for r in log): trading.reconcile(now); V.reconcile_checks(now); changed = True
            changed = True
        elif a == "control":
            log = trading.execute_pending(prov, now, refresh=cal.is_open("US", now)); changed = True
        elif a == "weekly":
            research.weekly_strategy_report(now); weekly_eval.build(now); changed = True
        elif a == "export": changed = True
        try: V.update_all(now)
        except Exception as e: db.event("WARN", "verification", f"verification update failed: {e}")
        db.heartbeat("last_ci_run", f"{a}|event={ev}|run={run_id}|counts_as_unattended={ev == 'schedule'}"); jobs.finish(jid, "success", None, detail, now)
    except Exception as e:
        err = e; status = "failed"; changed = True
        db.event("ERROR", "actions", f"{a} failed: {e}\n{traceback.format_exc()[-600:]}"); jobs.finish(jid, "failed", e, None, now)
        print("ENGINE FAILED:", e); traceback.print_exc()
    finally:
        jobs.release_lease(holder)
    _out(changed="true" if changed else "false", engine_status=status)
    if err is not None and os.environ.get("AITL_FAIL_AFTER_PERSIST") == "1": return        # the workflow fails AFTER persisting (see run.yml)
    if err is not None: raise err

if __name__ == "__main__": main(sys.argv[1] if len(sys.argv) > 1 else "export")

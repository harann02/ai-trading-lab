"""Scheduler reliability fix (issue #3): off-grid crons, intent-based scan/monitor classification, idempotency, external_scheduler trigger class."""
import os, re
from datetime import datetime, timezone, timedelta, date
from test_v2 import env2
from app import db, calendars as cal
from app.v3 import schedule as S, verification as V, jobs
import tools.gate as gate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
U = lambda *a: datetime(*a, tzinfo=timezone.utc)

def _crons(f):
    return re.findall(r'-\s*cron:\s*"([^"]+)"', open(os.path.join(ROOT, ".github/workflows", f)).read())

def _minutes(expr):
    m = expr.split()[0]; return {int(x) for x in m.split(",")}

def test_crons_off_grid_and_no_collision():
    run = _crons("run.yml"); wd = _crons("watchdog.yml")
    assert run == ["41 21 * * 1-5", "4,19,34,49 13-21 * * 1-5", "13 2 * * 6"]
    assert wd == ["56 21 * * 1-5", "11 */3 * * 1-5"]
    for e in run + wd: assert not (_minutes(e) & {0, 15, 30, 45}), e
    assert not (_minutes(run[0]) & _minutes(run[1]))          # scan minute never coincides with a monitor minute in hour 21
    y = open(os.path.join(ROOT, ".github/workflows/run.yml")).read()
    assert "repository_dispatch:" in y and "external-cron" in y and "a=auto" in y

def test_expected_monitor_slots_are_offset():
    ex = S.expected_monitor_runs(U(2026, 10, 13, 13, 0), U(2026, 10, 13, 21, 0))
    assert ex and all(t.minute in (4, 19, 34, 49) for t in ex) and ex[0] == U(2026, 10, 13, 13, 34)

def test_classify_by_intent_delayed_and_dropped_slots():
    done = lambda s: False
    # summer (EDT close 20:00): scan due from 21:30 UTC; a delayed monitor slot (21:49) or a late external run still yields the scan
    assert S.classify(U(2026, 10, 13, 21, 41), done)[0] == "scan"
    assert S.classify(U(2026, 10, 13, 21, 49), done)[0] == "scan"
    assert S.classify(U(2026, 10, 14, 2, 0), done)[:2] == ("scan", date(2026, 10, 13))
    assert S.classify(U(2026, 10, 13, 21, 19), done)[0] == "monitor"          # before the window: monitor
    assert S.classify(U(2026, 10, 14, 13, 34), done)[0] == "monitor"          # after the next open: no longer this session's on-time scan
    # winter (EST close 21:00): window opens 21:30
    assert S.classify(U(2026, 12, 15, 21, 34), done)[0] == "scan" and S.classify(U(2026, 12, 15, 21, 19), done)[0] == "monitor"
    # Friday session scanned on Saturday UTC
    assert S.classify(U(2026, 10, 17, 1, 0), done)[:2] == ("scan", date(2026, 10, 16))
    # idempotent: once a scan is recorded every later run is a monitor
    assert S.classify(U(2026, 10, 13, 21, 49), lambda s: True)[0] == "monitor"

def test_gate_auto_for_schedule_and_external(monkeypatch):
    monkeypatch.setattr(S, "eod_scan_done", lambda s: False)
    assert gate.decide("auto", "schedule", U(2026, 10, 13, 21, 41))[0] is True
    assert gate.decide("auto", "repository_dispatch", U(2026, 10, 14, 2, 0))[0] is True
    assert gate.decide("auto", "repository_dispatch", U(2026, 10, 14, 6, 0))[0] is True        # still before the next open -> scan
    monkeypatch.setattr(S, "eod_scan_done", lambda s: True)
    assert gate.decide("auto", "repository_dispatch", U(2026, 10, 14, 6, 0))[0] is False       # scan done, market closed -> skip
    assert gate.decide("auto", "schedule", U(2026, 10, 13, 16, 4))[0] is True                  # in session -> monitor

def test_trigger_class_and_shared_idempotency(env2):
    assert jobs.trigger_class("repository_dispatch") == "external_scheduler" and jobs.trigger_class("schedule") == "schedule"
    t = U(2026, 10, 13, 21, 41)
    a = jobs.begin("scan", t, "schedule", "1", session="2026-10-13"); assert a; jobs.finish(a, "success", now=t)
    assert jobs.begin("scan", t + timedelta(hours=2), "external_scheduler", "2", session="2026-10-13") is None   # no double scan across triggers
    assert S.eod_scan_done(date(2026, 10, 13)) is True

def test_external_runs_separate_counter_never_schedule(env2):
    sd = V.start_session() or date(2026, 10, 12)
    h, nxt = V._session_window(sd); at = h[1] + timedelta(hours=2)
    c = db.conn(); c.execute("INSERT INTO job_runs(idem_key,job,trigger,scheduled_for,started_ts,finished_ts,status,run_id) VALUES(?,?,?,?,?,?,?,?)",
                             (f"scan:{sd}", "scan", "external_scheduler", *(at.isoformat(timespec="seconds"),) * 3, "success", "777")); c.commit(); c.close()
    item, fails, notes = V.compute_session(sd, at + timedelta(hours=1))
    assert item["scan_occurred"] == 0 and any("external_scheduler" in n for n in notes)
    s = V.summary(at + timedelta(hours=1))
    assert s["scheduled_runs_recorded"] == 0 and s["external_scheduler_runs_recorded"] == 1 and s["external_scheduler_scan_sessions"] == 1
    assert all(r["trigger"] != "external_scheduler" for r in s["manual_runs_not_counted"]) and "NOT event=schedule" in s["external_scheduler_basis"]

def test_ci_run_auto_external_records_trigger_class(env2, monkeypatch):
    import ci_run
    monkeypatch.setenv("GITHUB_EVENT_NAME", "repository_dispatch"); monkeypatch.setenv("GITHUB_RUN_ID", "9191"); monkeypatch.delenv("AITL_CONTROL_DIR", raising=False)
    monkeypatch.setattr(S, "classify", lambda now, *a: ("monitor", None, "test"))
    monkeypatch.setattr(S, "monitor_window", lambda now: (False, None, None, None, "outside test"))
    ci_run.main("auto"); c = db.conn(); r = c.execute("SELECT trigger, job FROM job_runs WHERE run_id='9191'").fetchall(); c.close()
    assert r and all(x["trigger"] == "external_scheduler" for x in r) and all(x["job"] == "monitor" for x in r)

def test_all_tools_compile():
    import py_compile, glob
    for f in glob.glob(os.path.join(ROOT, "tools", "*.py")) + [os.path.join(ROOT, "ci_run.py"), os.path.join(ROOT, "export_static.py")]: py_compile.compile(f, doraise=True)

"""EOD-scan timing/labelling (15 Oct 2026 incident): an external-backup run 8 min after the close performed the session's scan via the monitor
'self-heal' catch-up path, before final daily bars, and was labelled catch-up. Rule now: scan only from NYSE close + 30 min (calendar: DST, early closes)
with the final daily bar present; the first unattended run (github-schedule OR external-backup) in [close+30, next open) is the ON-TIME scan;
catch-up only once that window lapsed (at/after the next session's open)."""
from datetime import datetime, timezone, timedelta, date
import pandas as pd
from test_v2 import env2
from app import db, calendars as cal
from app.v3 import schedule as S, verification as V
import tools.gate as gate

U = lambda *a: datetime(*a, tzinfo=timezone.utc)
NONE = lambda s: False

def test_window_opens_close_plus_30_summer_winter_early_close():
    # summer (EDT): Thu 15 Oct 2026 close 16:00 ET = 20:00 UTC
    assert S.eod_scan_window(date(2026, 10, 15))[0] == U(2026, 10, 15, 20, 30)
    # winter (EST): Tue 15 Dec 2026 close 16:00 ET = 21:00 UTC
    assert S.eod_scan_window(date(2026, 12, 15))[0] == U(2026, 12, 15, 21, 30)
    # early closes (13:00 ET): day after Thanksgiving + Christmas Eve 2026 = 18:00 UTC
    assert S.eod_scan_window(date(2026, 11, 27))[0] == U(2026, 11, 27, 18, 30)
    assert S.eod_scan_window(date(2026, 12, 24))[0] == U(2026, 12, 24, 18, 30)
    # window ends at the next session's open (Fri -> Mon)
    assert S.eod_scan_window(date(2026, 10, 16))[1] == U(2026, 10, 19, 13, 30)

def test_classify_close_plus_8_is_monitor_close_plus_35_is_scan():
    for close in (U(2026, 10, 15, 20, 0), U(2026, 12, 15, 21, 0), U(2026, 11, 27, 18, 0), U(2026, 12, 24, 18, 0)):
        assert S.classify(close + timedelta(minutes=8), NONE)[0] == "monitor", close
        assert S.classify(close + timedelta(minutes=29), NONE)[0] == "monitor", close
        a, sess, _ = S.classify(close + timedelta(minutes=35), NONE); assert (a, sess) == ("scan", close.astimezone(cal.NY).date()), close

def test_gate_external_close_plus_8_monitor_close_plus_35_scan(monkeypatch):
    monkeypatch.setattr(S, "eod_scan_done", NONE)
    ok, why = gate.decide("auto", "repository_dispatch", U(2026, 10, 15, 20, 8)); assert ok and why.startswith("monitor")
    ok, why = gate.decide("auto", "repository_dispatch", U(2026, 10, 15, 20, 35)); assert ok and "EOD scan due" in why
    ok, why = gate.decide("auto", "schedule", U(2026, 12, 15, 21, 35)); assert ok and "EOD scan due" in why

def test_catchup_only_when_window_lapsed():
    s = date(2026, 10, 15)
    assert not S.catchup_due(U(2026, 10, 15, 20, 8), s, NONE)      # the 15 Oct bug: no catch-up 8 min after the close
    assert not S.catchup_due(U(2026, 10, 15, 20, 35), s, NONE)     # inside the window: on-time scan, not catch-up
    assert not S.catchup_due(U(2026, 10, 16, 13, 29), s, NONE)
    assert S.catchup_due(U(2026, 10, 16, 13, 34), s, NONE)         # next session open passed with no scan: genuine catch-up
    assert not S.catchup_due(U(2026, 10, 16, 13, 34), s, lambda x: True)

class _P:
    def __init__(self, last): self.last = last
    def daily(self, syms, period="5d"):
        idx = pd.bdate_range(end=pd.Timestamp(self.last), periods=5); return {s: pd.DataFrame({"close": range(5)}, index=idx) for s in syms}

def test_final_bars_required():
    assert S.final_bars_ready(_P("2026-10-15"), date(2026, 10, 15))[0] is True
    assert S.final_bars_ready(_P("2026-10-14"), date(2026, 10, 15))[0] is False
    class Boom:
        def daily(self, *a, **k): raise RuntimeError("down")
    assert S.final_bars_ready(Boom(), date(2026, 10, 15))[0] is False

def _ins(sd, job, trig, t, rid):
    c = db.conn(); c.execute("INSERT INTO job_runs(idem_key,job,trigger,scheduled_for,started_ts,finished_ts,status,run_id) VALUES(?,?,?,?,?,?,?,?)",
                             (f"{job}:{sd}", job, trig, *(t.isoformat(timespec="seconds"),) * 3, "success", rid)); c.commit(); c.close()

def test_external_scan_at_close_plus_35_is_on_time(env2):
    sd = V.start_session() or date(2026, 10, 12); h, nxt = V._session_window(sd); t = h[1] + timedelta(minutes=35)
    _ins(sd, "scan", "external_scheduler", t, "501")
    item, fails, notes = V.compute_session(sd, t + timedelta(hours=1))
    assert item["scan_occurred"] == 1 and "scheduled scan missed (catch-up only)" not in fails and not any("catch-up" in n for n in notes)

def test_scan_before_window_not_on_time(env2):
    # historical 15 Oct shape: a catch-up scan 8 min after the close stays NOT on-time (records are not rewritten)
    sd = V.start_session() or date(2026, 10, 12); h, nxt = V._session_window(sd); t = h[1] + timedelta(minutes=8)
    _ins(sd, "catchup_scan", "external_scheduler", t, "502")
    item, fails, notes = V.compute_session(sd, t + timedelta(hours=2)); assert "scheduled scan missed (catch-up only)" in fails

def _frozen(monkeypatch, ci_run, t):
    class FD(datetime):
        @classmethod
        def now(cls, tz=None): return t
    monkeypatch.setattr(ci_run, "datetime", FD)

def test_ci_run_external_close_plus_8_monitor_then_close_plus_35_on_time_scan(env2, monkeypatch, tmp_path):
    import ci_run
    from conftest import FakeProvider
    from test_v2 import frames_us
    calls = []
    monkeypatch.setattr(ci_run, "do_scan", lambda prov, now, job: calls.append((job, now)) or {})
    monkeypatch.setattr(ci_run, "YFinanceProvider", lambda: FakeProvider(frames_us()))
    monkeypatch.setattr(ci_run.trading, "execute_pending", lambda *a, **k: [])
    monkeypatch.setattr(S, "final_bars_ready", lambda prov, sess: (True, "test"))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "repository_dispatch"); monkeypatch.delenv("AITL_CONTROL_DIR", raising=False); monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "o"))
    monkeypatch.setenv("GITHUB_RUN_ID", "601"); _frozen(monkeypatch, ci_run, U(2026, 10, 15, 20, 8)); ci_run.main("auto")
    assert calls == []                                                   # close+8: monitor only, no (catch-up) scan
    monkeypatch.setenv("GITHUB_RUN_ID", "602"); _frozen(monkeypatch, ci_run, U(2026, 10, 15, 20, 35)); ci_run.main("auto")
    assert [c[0] for c in calls] == ["scan"]                             # close+35: the on-time scan (job 'scan', not 'catchup_scan')
    c = db.conn(); r = c.execute("SELECT job,trigger FROM job_runs WHERE run_id='602' AND status='success'").fetchall(); c.close()
    assert [(x["job"], x["trigger"]) for x in r] == [("scan", "external_scheduler")]

def test_ci_run_defers_scan_without_final_bars(env2, monkeypatch, tmp_path):
    import ci_run
    from conftest import FakeProvider
    from test_v2 import frames_us
    calls = []
    monkeypatch.setattr(ci_run, "do_scan", lambda prov, now, job: calls.append(job) or {})
    monkeypatch.setattr(ci_run, "YFinanceProvider", lambda: FakeProvider(frames_us()))
    monkeypatch.setattr(ci_run.trading, "execute_pending", lambda *a, **k: [])
    monkeypatch.setattr(S, "final_bars_ready", lambda prov, sess: (False, "SPY last daily bar 2026-10-14"))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule"); monkeypatch.setenv("GITHUB_RUN_ID", "603"); monkeypatch.delenv("AITL_CONTROL_DIR", raising=False); monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "o"))
    _frozen(monkeypatch, ci_run, U(2026, 10, 15, 20, 35)); ci_run.main("auto")
    assert calls == [] and not S.eod_scan_done(date(2026, 10, 15))      # deferred, not recorded: the next run in the window retries

"""Workflow gate: decides BEFORE the engine runs whether a scheduled run should proceed (NYSE calendar, America/New_York, DST, holidays, early closes).
Wide UTC cron + this guard = full regular session coverage in both EDT and EST. Writes GITHUB_OUTPUT proceed=true|false and skip_reason. When skipping, appends the reason to
status/skips.json in the data checkout (committed + pushed as a tiny commit; the engine folds it into job_runs on its next persisted run).
Usage: python tools/gate.py <action> <event_name> <data_checkout_dir>"""
import sys, os, json, subprocess
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.v3 import schedule as S
from app import calendars as cal

UNATTENDED_EVENTS = ("schedule", "repository_dispatch")      # repository_dispatch (type external-cron) = external backup scheduler; gated exactly like cron

def decide(action, event, now):
    if event not in UNATTENDED_EVENTS: return True, "manual/push event: always runs (engine is idempotent)"
    if action == "auto":                                        # intent-based: scan if the last session's EOD scan is due and missing, else monitor
        def _done(sess):
            try: return S.eod_scan_done(sess)
            except Exception as e: print("gate: could not read job_runs (", e, "); the engine re-checks idempotently"); return False
        a, sess, why = S.classify(now, _done)
        if a == "scan": return True, why
        ok, d, o, c, why2 = S.monitor_window(now)
        return ok, (f"monitor: in session window ({why})" if ok else f"monitor: {why2}; {why}")
    if action == "monitor":
        ok, d, o, c, why = S.monitor_window(now)
        return ok, ("in session window" if ok else why)
    if action in ("scan", "weekly"):
        if action == "weekly": return True, "weekly report"
        d = now.astimezone(cal.NY).date()
        return (cal.session_hours("US", d) is not None), ("NYSE session day" if cal.session_hours("US", d) else f"{d} is not an NYSE session (weekend/holiday): no scan")
    return True, "ok"

def main():
    action, event, ddir = sys.argv[1], sys.argv[2], sys.argv[3]; now = datetime.now(timezone.utc)
    ok, why = decide(action, event, now)
    print(f"gate: action={action} event={event} proceed={ok} reason={why} now_utc={now.isoformat(timespec='seconds')} now_ny={now.astimezone(cal.NY):%Y-%m-%d %H:%M %Z}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out: open(out, "a").write(f"proceed={'true' if ok else 'false'}\nskip_reason={why}\n")
    if not ok:
        sm = os.environ.get("GITHUB_STEP_SUMMARY")
        if sm: open(sm, "a").write(f"**Skipped** `{action}` at {now.isoformat(timespec='seconds')} UTC ({now.astimezone(cal.MYT):%H:%M} MYT): {why}\n")
        p = os.path.join(ddir, "status"); os.makedirs(p, exist_ok=True); f = os.path.join(p, "skips.json")
        doc = json.load(open(f)) if os.path.exists(f) else {"skips": []}
        doc["skips"] = (doc.get("skips", []) + [{"ts": now.isoformat(timespec="seconds"), "job": action, "reason": why, "run_id": os.environ.get("GITHUB_RUN_ID")}])[-100:]
        json.dump(doc, open(f, "w"), indent=0)
        try:
            subprocess.run(["git", "-C", ddir, "config", "user.name", "ailab-bot"], check=True); subprocess.run(["git", "-C", ddir, "config", "user.email", "ailab-bot@users.noreply.github.com"], check=True)
            subprocess.run(["git", "-C", ddir, "add", "status/skips.json"], check=True); subprocess.run(["git", "-C", ddir, "commit", "-qm", f"gate skip {action} {now:%Y-%m-%dT%H:%MZ}: {why[:80]}"], check=True)
            for i in range(3):
                if subprocess.run(["git", "-C", ddir, "push", "-q", "origin", "HEAD:data"]).returncode == 0: break
                subprocess.run(["git", "-C", ddir, "pull", "-q", "--rebase", "origin", "data"])
        except Exception as e: print("could not persist skip record:", e)
if __name__ == "__main__": main()

"""Watchdog: detects missed scheduled runs / stale data from the ledger (job_runs) on branch `data` and surfaces them as ONE GitHub issue (label scheduler-alert).
Free, no ledger writes (read-only; does not touch the writer lease). Usage: python tools/watchdog.py <path-to-ailab.db> [--dry-run]
Env: GITHUB_TOKEN, GITHUB_REPOSITORY (set by Actions). Exit code 0 always (alerts are issues, not red runs) unless the DB cannot be read."""
import sys, os, json, sqlite3
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests

def evaluate(db_path, now=None):
    os.environ["AITL_DB"] = db_path
    from app import db, calendars as cal
    from app.v3 import schedule as S
    now = now or datetime.now(timezone.utc)
    h = S.health(now); problems = []
    miss = [m for m in h["missed_runs_26h"] if datetime.fromisoformat(m["expected"]) < now]
    scans = [m for m in miss if m["job"] == "scan"]; mons = [m for m in miss if m["job"] == "monitor"]
    if scans: problems.append(f"MISSED SCAN for session(s) {', '.join(m.get('session', '?') for m in scans)}; expected {', '.join(m['expected'] for m in scans)} UTC")
    if len(mons) >= 3: problems.append(f"{len(mons)} monitor slots missed in the last 26h (latest expected {mons[-1]['expected']} UTC)")
    fr = h["data_freshness"]
    if fr.get("stale") and fr.get("sessions_behind", 0) > 1: problems.append(f"market data stale: {fr}")
    if h["status"].startswith("DEGRADED (latest run failed)"): problems.append(f"latest engine run failed: {json.dumps(h.get('latest_error'), default=str)[:300]}")
    return {"now": now.isoformat(timespec="seconds"), "status": h["status"], "problems": problems, "n_missed": h["n_missed_26h"]}

def gh(method, path, **kw):
    return requests.request(method, f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}", headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}, timeout=30, **kw)

def surface(res):
    TITLE = "Scheduler alert: missed/failed run detected by watchdog"
    r = gh("GET", "/issues", params={"state": "open", "labels": "scheduler-alert"}); r.raise_for_status(); open_ = r.json()
    if res["problems"]:
        body = f"Watchdog check at {res['now']} UTC found:\n\n" + "\n".join(f"- {p}" for p in res["problems"]) + "\n\nGitHub cron is best-effort. The next monitor run performs a catch-up scan automatically; to force one now: Actions -> scan-monitor-deploy -> Run workflow -> action=scan.\nThis issue closes itself when the watchdog sees a healthy state."
        if open_: gh("POST", f"/issues/{open_[0]['number']}/comments", json={"body": body}).raise_for_status(); return f"commented on #{open_[0]['number']}"
        gh("POST", "/labels", json={"name": "scheduler-alert", "color": "d93f0b"})
        r = gh("POST", "/issues", json={"title": TITLE, "body": body, "labels": ["scheduler-alert"]}); r.raise_for_status(); return f"opened #{r.json()['number']}"
    for i in open_:
        gh("POST", f"/issues/{i['number']}/comments", json={"body": f"Healthy again at {res['now']} UTC; closing."}); gh("PATCH", f"/issues/{i['number']}", json={"state": "closed"})
    return "healthy" + (f"; closed {len(open_)} issue(s)" if open_ else "")

if __name__ == "__main__":
    res = evaluate(sys.argv[1]); print(json.dumps(res, indent=1))
    sm = os.environ.get("GITHUB_STEP_SUMMARY")
    if sm: open(sm, "a").write(f"### Watchdog {res['now']}\nstatus **{res['status']}**; problems: {res['problems'] or 'none'}\n")
    if "--dry-run" not in sys.argv: print(surface(res))

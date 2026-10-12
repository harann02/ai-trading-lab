"""Job heartbeats (scheduled / started / finished / success per job), duplicate-run prevention (idempotency keys) and the SINGLE-WRITER LEASE for the ledger.
Authority: exactly one of {GitHub Actions workflow, self-hosted FastAPI backend} may write the ledger. ledger_meta.writer_mode ('actions' default | 'selfhosted') says which;
a writer must hold an unexpired lease in writer_lease and its mode must match, otherwise it must not write (it records a skipped run with the reason)."""
import json, os
from datetime import datetime, timezone, timedelta
from .. import db

def _iso(t=None): return (t or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")

def slot(now, minutes=15):
    t = now.astimezone(timezone.utc); return t.replace(minute=(t.minute // minutes) * minutes, second=0, microsecond=0)

def idem_key(job, now, trigger, run_id=None, session=None):
    if trigger in ("workflow_dispatch", "manual", "push"): return f"manual:{job}:{run_id or _iso(now)}"
    if job == "scan" or job == "weekly" or job == "catchup_scan": return f"{job}:{session or now.astimezone(timezone.utc).date()}"
    return f"{job}:{_iso(slot(now))}"

def begin(job, now, trigger="schedule", run_id=None, session=None, scheduled_for=None, stale_running_min=45):
    """Returns job_run id, or None if this idempotency key already succeeded / is running (duplicate prevented)."""
    key = idem_key(job, now, trigger, run_id, session); c = db.conn()
    try:
        r = c.execute("SELECT id,status,started_ts FROM job_runs WHERE idem_key=?", (key,)).fetchone()
        if r:
            running_recent = r["status"] == "running" and (now - datetime.fromisoformat(r["started_ts"])) < timedelta(minutes=stale_running_min)
            if r["status"] == "success" or running_recent:
                c.execute("INSERT INTO job_runs(idem_key,job,trigger,scheduled_for,started_ts,finished_ts,status,skip_reason,run_id) VALUES(?,?,?,?,?,?,?,?,?)",
                          (f"dup:{key}:{_iso(now)}", job, trigger, scheduled_for or _iso(slot(now)), _iso(now), _iso(now), "skipped", f"duplicate prevented: idempotency key {key} already {r['status']}", run_id)); c.commit(); return None
            c.execute("DELETE FROM job_runs WHERE id=?", (r["id"],))            # failed/stale earlier attempt: this is a retry, replace the row
        cur = c.execute("INSERT INTO job_runs(idem_key,job,trigger,scheduled_for,started_ts,status,run_id) VALUES(?,?,?,?,?,?,?)", (key, job, trigger, scheduled_for or _iso(slot(now)), _iso(now), "running", run_id))
        c.commit(); return cur.lastrowid
    finally: c.close()

def finish(jid, status="success", error=None, detail=None, now=None):
    if jid is None: return
    c = db.conn(); c.execute("UPDATE job_runs SET finished_ts=?, status=?, error=?, detail=? WHERE id=?", (_iso(now), status, (str(error)[:500] if error else None), json.dumps(detail, default=str)[:2000] if detail is not None else None, jid)); c.commit(); c.close()

def record_skip(job, now, reason, trigger="schedule", run_id=None, scheduled_for=None):
    c = db.conn(); c.execute("INSERT OR IGNORE INTO job_runs(idem_key,job,trigger,scheduled_for,started_ts,finished_ts,status,skip_reason,run_id) VALUES(?,?,?,?,?,?,?,?,?)",
                             (f"skip:{job}:{_iso(now)}", job, trigger, scheduled_for or _iso(slot(now)), _iso(now), _iso(now), "skipped", reason, run_id)); c.commit(); c.close()

def ingest_gate_skips(path, limit=200):
    """The workflow gate (tools/gate.py) records off-market skips in status/skips.json on the data branch (cheap, no DB write there); the engine folds them into job_runs."""
    if not os.path.exists(path): return 0
    try: doc = json.load(open(path))
    except Exception: return 0
    n = 0
    for s in (doc.get("skips") or [])[-limit:]:
        c = db.conn(); cur = c.execute("INSERT OR IGNORE INTO job_runs(idem_key,job,trigger,scheduled_for,started_ts,finished_ts,status,skip_reason,run_id) VALUES(?,?,?,?,?,?,?,?,?)",
                                       (f"gateskip:{s.get('ts')}:{s.get('job')}", s.get("job") or "monitor", "schedule", s.get("ts"), s.get("ts"), s.get("ts"), "skipped", s.get("reason"), s.get("run_id"))); n += cur.rowcount; c.commit(); c.close()
    return n

# ------------------------------------------------------------------ single authoritative writer
def writer_mode():
    c = db.conn()
    try: r = c.execute("SELECT value FROM ledger_meta WHERE key='writer_mode'").fetchone()
    except Exception: r = None
    c.close(); return r["value"] if r else "actions"

def set_writer_mode(mode, actor="owner"):
    if mode not in ("actions", "selfhosted"): raise ValueError("mode must be actions|selfhosted")
    c = db.conn(); c.execute("INSERT INTO ledger_meta(key,value) VALUES('writer_mode',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (mode,)); c.execute("DELETE FROM writer_lease WHERE name='ledger'"); c.commit(); c.close()
    db.audit("writer_mode_changed", {"mode": mode}, actor, "control")

def acquire_lease(holder, mode="actions", ttl_min=25, now=None, run_id=None):
    """(ok, reason). Fails when the configured writer mode differs from `mode` or another live holder owns the lease."""
    now = now or datetime.now(timezone.utc); wm = writer_mode()
    if wm != mode: return False, f"ledger writer mode is '{wm}' - '{mode}' must not write"
    c = db.conn()
    try:
        c.execute("BEGIN IMMEDIATE"); r = c.execute("SELECT * FROM writer_lease WHERE name='ledger'").fetchone()
        if r and r["holder"] != holder and datetime.fromisoformat(r["expires_ts"]) > now:
            c.rollback(); return False, f"lease held by {r['holder']} until {r['expires_ts']}"
        c.execute("INSERT INTO writer_lease(name,holder,mode,acquired_ts,expires_ts,run_id) VALUES('ledger',?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET holder=excluded.holder,mode=excluded.mode,acquired_ts=excluded.acquired_ts,expires_ts=excluded.expires_ts,run_id=excluded.run_id",
                  (holder, mode, _iso(now), _iso(now + timedelta(minutes=ttl_min)), run_id)); c.commit(); return True, "acquired"
    finally: c.close()

def release_lease(holder):
    c = db.conn(); c.execute("DELETE FROM writer_lease WHERE name='ledger' AND holder=?", (holder,)); c.commit(); c.close()

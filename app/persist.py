"""Concurrency-safe persistence of the ledger DB on the GitHub `data` branch (stdlib only; no engine imports).

Why: the old workflow loop, after a rejected --force-with-lease push, ran `git checkout --detach origin/data` which REPLACED the freshly written DB with the remote (older) DB
and then pushed that as 'pushed' => silent ledger rollback (audit finding F-1). This module replaces that loop.

Guarantees
  * The DB produced by the run is stashed OUTSIDE the git work tree and is never overwritten by any git operation.
  * Verified before push: PRAGMA integrity_check, ledger invariants (append-only, strictly increasing ids, unique idempotency keys),
    the new ledger EXTENDS the remote ledger (hash-chain prefix equality; row count >= remote; nothing lost, nothing changed).
  * Compare-and-swap push: `--force-with-lease=<branch>:<exact sha we built on>`; one atomic single commit.
  * On conflict NEVER overwrite: if only non-DB files moved (owner control file, gate skip record) we rebuild on top of the new remote tree and keep OUR db;
    if the remote DB advanced (someone else wrote the ledger) we raise ConflictRerun: nothing is pushed and the caller re-runs the job on the fresh DB
    (idempotency keys make that safe). If the remote ledger is not a prefix of ours => Irreconcilable (fail loudly).
  * After push: re-fetch and compare sha256 of the remote DB + manifest with what we meant to push.
  * Monotonic version `seq` stored in the DB (ledger_meta.persist_seq) AND in the sidecar status/ledger_manifest.json.
  * Rolling history: backups/daily/ailab-YYYYMMDD.db.gz (last N days, in the same commit) + append-only `ledger-log` branch (ledger.jsonl, fast-forward only push).
Exit codes of the CLI: 0 ok, 3 conflict/irreconcilable (nothing pushed), 4 post-push verification failed, 2 usage/other error.
"""
import gzip, hashlib, json, os, shutil, sqlite3, subprocess, sys, tempfile, time
from datetime import datetime, timezone

MANIFEST = "status/ledger_manifest.json"
DB_NAME = "ailab.db"
KEEP_DAILY = 7


class PersistError(Exception): pass
class ConflictRerun(PersistError): """Remote DB advanced by another writer: nothing pushed, re-run the job on the fresh DB."""
class Irreconcilable(PersistError): """Remote ledger is not a prefix of the new ledger (or integrity failed): nothing pushed."""
class VerificationFailed(PersistError): """Post-push re-fetch did not match."""


def _now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()

# ------------------------------------------------------------------ ledger fingerprint
def _ro(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True); return c

def ledger_chain(path):
    """Return (heads, ids, uniqs): heads[i] = chain hash after i+1 rows, in id order. Every column of every row is covered."""
    c = _ro(path)
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(ledger)")]
        if not cols: return [], [], []
        h = b"genesis"; heads = []; ids = []; uniqs = []
        for row in c.execute("SELECT * FROM ledger ORDER BY id"):
            h = hashlib.sha256(h + json.dumps(row, default=str, separators=(",", ":")).encode()).digest(); heads.append(h.hex())
            ids.append(row[cols.index("id")]); uniqs.append(row[cols.index("uniq")] if "uniq" in cols else None)
        return heads, ids, uniqs
    finally: c.close()

def fingerprint(path):
    """Cheap-but-complete summary of a DB file for verification and the sidecar manifest."""
    c = _ro(path)
    try:
        integ = c.execute("PRAGMA integrity_check").fetchone()[0]
        try: seq = int((c.execute("SELECT value FROM ledger_meta WHERE key='persist_seq'").fetchone() or [0])[0])
        except Exception: seq = 0
    finally: c.close()
    heads, ids, uniqs = ledger_chain(path)
    return {"integrity": integ, "persist_seq": seq, "ledger_count": len(heads), "ledger_max_id": ids[-1] if ids else 0,
            "ledger_chain_head": heads[-1] if heads else "genesis", "db_sha256": sha256_file(path), "db_bytes": os.path.getsize(path)}

def check_invariants(path):
    try: fp = fingerprint(path)
    except sqlite3.DatabaseError as e: raise Irreconcilable(f"database unreadable/corrupt: {e}")
    if fp["integrity"] != "ok": raise Irreconcilable(f"integrity_check failed: {fp['integrity']}")
    heads, ids, uniqs = ledger_chain(path)
    if any(b <= a for a, b in zip(ids, ids[1:])): raise Irreconcilable("ledger ids not strictly increasing")
    u = [x for x in uniqs if x is not None]
    if len(u) != len(set(u)): raise Irreconcilable("duplicate ledger idempotency keys (uniq)")
    return fp

def check_extends(new_path, old_path):
    """new ledger must contain the old ledger as an exact prefix (nothing lost, duplicated or altered)."""
    nh, _, _ = ledger_chain(new_path); oh, _, _ = ledger_chain(old_path)
    if len(nh) < len(oh): raise Irreconcilable(f"ledger shrank: remote has {len(oh)} rows, new DB has {len(nh)}")
    if oh and nh[len(oh) - 1] != oh[-1]: raise Irreconcilable(f"ledger history differs from remote within its first {len(oh)} rows (rollback/rewrite)")
    return len(nh) - len(oh)

# ------------------------------------------------------------------ git helpers
def git(repo, *a, check=True, capture=True, env=None):
    e = dict(os.environ); e.update({"GIT_TERMINAL_PROMPT": "0"}); e.update(env or {})
    r = subprocess.run(["git", "-C", repo, *a], capture_output=capture, text=True, env=e)
    if check and r.returncode != 0: raise PersistError(f"git {' '.join(a[:2])} failed: {(r.stderr or '')[-300:]}")
    return r

def _ident(repo):
    git(repo, "config", "user.name", os.environ.get("AITL_GIT_NAME", "ailab-bot")); git(repo, "config", "user.email", os.environ.get("AITL_GIT_EMAIL", "ailab-bot@users.noreply.github.com"))

def fetch_remote(repo, remote="origin", branch="data"):
    git(repo, "fetch", "-q", remote, f"+{branch}:refs/remotes/{remote}/{branch}")
    return git(repo, "rev-parse", f"refs/remotes/{remote}/{branch}").stdout.strip()

def show_to(repo, rev, path, dest):
    r = subprocess.run(["git", "-C", repo, "show", f"{rev}:{path}"], capture_output=True)
    if r.returncode != 0: return False
    with open(dest, "wb") as f: f.write(r.stdout)
    return True

def read_manifest(repo, rev):
    r = git(repo, "show", f"{rev}:{MANIFEST}", check=False)
    if r.returncode != 0: return {}
    try: return json.loads(r.stdout)
    except Exception: return {}

# ------------------------------------------------------------------ begin / stash
def begin(repo, remote="origin", branch="data", state_file=None):
    """Called BEFORE the engine runs: refresh the work tree to the exact remote tip and record what we based the run on."""
    sha = fetch_remote(repo, remote, branch)
    git(repo, "checkout", "-q", "-f", "--detach", sha)
    dbp = os.path.join(repo, DB_NAME)
    base = {"base_sha": sha, "ts": _now()}
    if os.path.exists(dbp):
        base.update(fingerprint(dbp)); base["base_db_path"] = dbp
    else: base.update({"db_sha256": None, "ledger_count": 0, "persist_seq": 0})
    base["manifest_seq"] = read_manifest(repo, sha).get("seq", 0)
    if state_file:
        with open(state_file, "w") as f: json.dump(base, f)
    return base

def stash_db(src, dest):
    """Consistent, vacuumed, DELETE-journal copy of the finished DB, outside the git work tree."""
    if os.path.exists(dest): os.remove(dest)
    c = sqlite3.connect(src); c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute(f"VACUUM INTO '{dest}'"); c.close()
    return dest

def _stamp_seq(path, seq):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS ledger_meta(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT INTO ledger_meta(key,value) VALUES('persist_seq',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(seq),))
    c.commit(); c.execute("PRAGMA journal_mode=DELETE"); c.close()
    for s in ("-wal", "-shm"):
        if os.path.exists(path + s): os.remove(path + s)

# ------------------------------------------------------------------ publish
def _daily_snapshot(repo, db_path, keep=KEEP_DAILY):
    d = os.path.join(repo, "backups", "daily"); os.makedirs(d, exist_ok=True)
    today = os.path.join(d, f"ailab-{datetime.now(timezone.utc):%Y%m%d}.db.gz")
    if not os.path.exists(today):
        with open(db_path, "rb") as fi, gzip.open(today, "wb", compresslevel=6) as fo: shutil.copyfileobj(fi, fo)
    files = sorted(f for f in os.listdir(d) if f.startswith("ailab-") and f.endswith(".db.gz"))
    for f in files[:-keep]: os.remove(os.path.join(d, f))

def publish(repo, new_db, base, run_id="local", remote="origin", branch="data", max_attempts=5, sleep=2, hooks=None):
    """Push new_db (a stashed copy OUTSIDE `repo`) as ONE atomic commit with compare-and-swap. Returns manifest dict. See module docstring."""
    hooks = hooks or {}
    assert os.path.abspath(new_db).startswith(os.path.abspath(repo) + os.sep) is False, "stash the DB outside the git work tree"
    _ident(repo); check_invariants(new_db)       # corrupt / non-append-only DB is refused before anything else
    base_sha, base_db_sha = base.get("base_sha"), base.get("db_sha256")
    last_err = None
    for attempt in range(1, max_attempts + 1):
        R = fetch_remote(repo, remote, branch)
        tmp = tempfile.mkdtemp(prefix="persist-")
        try:
            rdb = os.path.join(tmp, "remote.db"); has_rdb = show_to(repo, R, DB_NAME, rdb)
            rsha = sha256_file(rdb) if has_rdb else None
            rman = read_manifest(repo, R)
            if R != base_sha and has_rdb and rsha != base_db_sha:
                # someone else advanced the ledger DB
                if str(rman.get("run_id")) == str(run_id) and rman.get("built_on_sha") == base_sha: return rman   # this very save already landed (retry of the same publish)
                raise ConflictRerun(f"remote DB advanced since base ({str(base_sha)[:8]} -> {R[:8]}); re-run on fresh DB")
            # remote DB identical to our base (or no DB yet): only non-DB files may have moved
            if has_rdb: check_extends(new_db, rdb)
            work = os.path.join(tmp, "new.db"); shutil.copyfile(new_db, work)
            seq = max(int(rman.get("seq") or 0), int(base.get("persist_seq") or 0), int(base.get("manifest_seq") or 0)) + 1
            _stamp_seq(work, seq)
            fp = check_invariants(work)
            if hooks.get("pre_commit"): hooks["pre_commit"](attempt)
            git(repo, "checkout", "-q", "-f", "--detach", R)                    # work tree = exact remote tree (owner/gate files preserved); OUR db is safe in `new_db`/`work`
            shutil.copyfile(work, os.path.join(repo, DB_NAME))
            for s in ("-wal", "-shm"):
                if os.path.exists(os.path.join(repo, DB_NAME + s)): os.remove(os.path.join(repo, DB_NAME + s))
            man = {"format": 1, "seq": seq, **{k: fp[k] for k in ("ledger_count", "ledger_max_id", "ledger_chain_head", "db_sha256", "db_bytes", "integrity")},
                   "run_id": str(run_id), "built_on_sha": R, "written_utc": _now()}
            os.makedirs(os.path.join(repo, "status"), exist_ok=True)
            with open(os.path.join(repo, MANIFEST), "w") as f: json.dump(man, f, indent=1)
            _daily_snapshot(repo, work)
            git(repo, "checkout", "-q", "--orphan", f"tmp-{int(time.time()*1000)}-{attempt}")
            git(repo, "add", "-A"); git(repo, "commit", "-qm", f"ledger snapshot seq={seq} rows={fp['ledger_count']} run={run_id} ({man['written_utc']})")
            br = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            if hooks.get("pre_push"): hooks["pre_push"](attempt)
            r = git(repo, "push", "-q", f"--force-with-lease={branch}:{R}", remote, f"{br}:{branch}", check=False)
            if r.returncode != 0:
                last_err = (r.stderr or "")[-200:]; print(f"persist: push rejected (attempt {attempt}): remote moved or refused; re-evaluating on fresh remote (never overwriting)")
                time.sleep(sleep); continue
            # post-push verification (re-fetch + compare)
            time.sleep(0)
            R2 = fetch_remote(repo, remote, branch); chk = os.path.join(tmp, "check.db")
            if not show_to(repo, R2, DB_NAME, chk) or sha256_file(chk) != fp["db_sha256"]:
                if R2 != git(repo, "rev-parse", br).stdout.strip():
                    # someone legitimately pushed on top after us: our DB must still be what we wrote OR a verified extension; refuse to guess
                    m2 = read_manifest(repo, R2)
                    if int(m2.get("seq") or 0) > seq: return m2
                raise VerificationFailed("remote DB sha256 differs from what was pushed")
            m2 = read_manifest(repo, R2)
            if int(m2.get("seq") or -1) < seq: raise VerificationFailed("remote manifest seq lower than pushed")
            return man
        finally: shutil.rmtree(tmp, ignore_errors=True)
    raise ConflictRerun(f"push kept being rejected after {max_attempts} attempts ({last_err}); nothing was overwritten")

# ------------------------------------------------------------------ append-only ledger log branch
def append_ledger_log(repo, db_path, remote="origin", branch="ledger-log", max_attempts=4):
    """Append new ledger rows (full rows, JSON lines) to the fast-forward-only `ledger-log` branch: real history + point-in-time recovery of the money record."""
    _ident(repo); wd = tempfile.mkdtemp(prefix="llog-")
    try:
        c = _ro(db_path); cols = [r[1] for r in c.execute("PRAGMA table_info(ledger)")]; rows = [dict(zip(cols, r)) for r in c.execute("SELECT * FROM ledger ORDER BY id")]; c.close()
        for attempt in range(max_attempts):
            r = git(repo, "fetch", "-q", remote, f"+{branch}:refs/remotes/{remote}/{branch}", check=False)
            exists = r.returncode == 0; have = []
            if exists:
                out = git(repo, "show", f"refs/remotes/{remote}/{branch}:ledger.jsonl", check=False).stdout
                have = [json.loads(l) for l in out.splitlines() if l.strip()]
                if [h["id"] for h in have] != [x["id"] for x in rows[:len(have)]] or have != rows[:len(have)]:
                    raise Irreconcilable("ledger-log is not a prefix of the DB ledger")
            new = rows[len(have):]
            if not new and exists: return 0
            git(repo, "checkout", "-q", "-f", "--detach", f"refs/remotes/{remote}/{branch}") if exists else git(repo, "checkout", "-q", "--orphan", f"ll-{attempt}")
            if not exists: git(repo, "rm", "-rqf", "--ignore-unmatch", ".", check=False)
            with open(os.path.join(repo, "ledger.jsonl"), "a") as f:
                for x in new: f.write(json.dumps(x, default=str, sort_keys=False) + "\n")
            git(repo, "add", "ledger.jsonl"); git(repo, "commit", "-qm", f"ledger log +{len(new)} rows (total {len(rows)})")
            br = git(repo, "rev-parse", "HEAD").stdout.strip()
            if git(repo, "push", "-q", remote, f"{br}:refs/heads/{branch}", check=False).returncode == 0: return len(new)
            time.sleep(1)
        raise PersistError("ledger-log push kept failing")
    finally: shutil.rmtree(wd, ignore_errors=True)

# ------------------------------------------------------------------ CLI
def main(argv):
    import argparse
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="cmd")
    a = sub.add_parser("begin"); a.add_argument("repo"); a.add_argument("--state", required=True)
    b = sub.add_parser("publish"); b.add_argument("repo"); b.add_argument("--state", required=True); b.add_argument("--db", required=True); b.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
    v = sub.add_parser("verify"); v.add_argument("db")
    args = p.parse_args(argv)
    try:
        if args.cmd == "begin": print(json.dumps(begin(args.repo, state_file=args.state))); return 0
        if args.cmd == "verify": print(json.dumps(check_invariants(args.db))); return 0
        if args.cmd == "publish":
            base = json.load(open(args.state)); m = publish(args.repo, args.db, base, args.run_id); print("PERSISTED", json.dumps(m))
            try: print("ledger-log +", append_ledger_log(args.repo, args.db))
            except Exception as e: print(f"::warning::ledger-log append failed: {e}")
            return 0
    except (ConflictRerun, Irreconcilable) as e: print(f"::error::PERSIST NOT DONE (nothing pushed): {e}"); return 3
    except VerificationFailed as e: print(f"::error::POST-PUSH VERIFICATION FAILED: {e}"); return 4
    except PersistError as e: print(f"::error::persist error: {e}"); return 2
    p.print_help(); return 2

if __name__ == "__main__": sys.exit(main(sys.argv[1:]))

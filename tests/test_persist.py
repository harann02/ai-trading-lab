"""Ledger persistence on the data branch: simulated with LOCAL BARE git repos. No transaction may disappear, duplicate or roll back."""
import os, sqlite3, subprocess, sys, json, shutil, signal, textwrap, pytest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from app import persist
from app.db import SCHEMA

def sh(*a, cwd=None, check=True):
    r = subprocess.run(a, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode: raise RuntimeError(r.stderr)
    return r.stdout.strip()

def make_db(path, rows, extra_table_rows=0):
    """rows: list of uniq-keys, in id order."""
    if os.path.exists(path): os.remove(path)
    c = sqlite3.connect(path); c.executescript(SCHEMA)
    c.execute("CREATE TABLE IF NOT EXISTS ledger_meta(key TEXT PRIMARY KEY, value TEXT)")
    for i, u in enumerate(rows):
        c.execute("INSERT INTO ledger(ts,kind,cash_delta_myr,uniq,note) VALUES(?,?,?,?,?)", (f"2026-10-1{i%9}T00:00:00Z", "DEPOSIT", 100.0 + i, u, "t"))
    c.commit(); c.execute("PRAGMA journal_mode=DELETE"); c.close(); return path

def extend_db(src, dst, new_keys):
    if src != dst: shutil.copyfile(src, dst)
    c = sqlite3.connect(dst)
    for u in new_keys: c.execute("INSERT INTO ledger(ts,kind,cash_delta_myr,uniq,note) VALUES('2026-10-12T00:00:00Z','BUY',-5,?, 'new')", (u,))
    c.commit(); c.close(); return dst

def ledger_keys(db):
    c = sqlite3.connect(db); k = [r[0] for r in c.execute("SELECT uniq FROM ledger ORDER BY id")]; c.close(); return k

@pytest.fixture
def env(tmp_path):
    remote = str(tmp_path / "remote.git"); sh("git", "init", "-q", "--bare", "-b", "data", remote)
    seed = str(tmp_path / "seed"); sh("git", "init", "-q", "-b", "data", seed)
    for k, v in (("user.name", "t"), ("user.email", "t@t")): sh("git", "-C", seed, "config", k, v)
    make_db(os.path.join(seed, "ailab.db"), ["k1", "k2", "k3"]); os.makedirs(os.path.join(seed, "control"))
    open(os.path.join(seed, "control", "commands.json"), "w").write('{"version":1}')
    sh("git", "-C", seed, "add", "-A"); sh("git", "-C", seed, "commit", "-qm", "seed"); sh("git", "-C", seed, "push", "-q", remote, "data")
    def clone(name):
        d = str(tmp_path / name); sh("git", "clone", "-q", "-b", "data", remote, d)
        for k, v in (("user.name", "t"), ("user.email", "t@t")): sh("git", "-C", d, "config", k, v)
        return d
    class E: pass
    e = E(); e.remote, e.clone, e.tmp = remote, clone, tmp_path
    e.remote_db = lambda: _remote_db(remote, tmp_path)
    e.owner_commit = lambda: _owner_commit(remote, tmp_path)
    return e

def _remote_db(remote, tmp):
    p = str(tmp / f"rdb_{os.urandom(3).hex()}.db"); open(p, "wb").write(subprocess.run(["git", "-C", remote, "show", "data:ailab.db"], capture_output=True).stdout); return p

def _owner_commit(remote, tmp):
    d = str(tmp / f"owner_{os.urandom(3).hex()}"); sh("git", "clone", "-q", "-b", "data", remote, d)
    for k, v in (("user.name", "o"), ("user.email", "o@o")): sh("git", "-C", d, "config", k, v)
    open(os.path.join(d, "control", "commands.json"), "w").write('{"version":2,"owner":"edit"}')
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "owner edit"); sh("git", "-C", d, "push", "-q", "origin", "data")

def run_job(env, name, new_keys, hooks=None):
    """Simulates one workflow run: begin -> engine writes new ledger rows -> stash -> publish."""
    wd = env.clone(name); st = str(env.tmp / f"{name}.json"); base = persist.begin(wd, state_file=st)
    stash_dir = env.tmp / f"{name}_stash"; stash_dir.mkdir(exist_ok=True)
    live = os.path.join(wd, "ailab.db"); extend_db(live, live, new_keys)           # engine writes into the work tree DB
    stash = persist.stash_db(live, str(stash_dir / "new.db"))
    return wd, base, stash, (lambda: persist.publish(wd, stash, base, run_id=name, sleep=0, hooks=hooks))

# ---------------------------------------------------------------- the ORIGINAL buggy loop (run.yml before this fix), reproduced faithfully
def legacy_persist(dbstore, hook_after_fetch=None):
    git = lambda *a, **k: subprocess.run(["git", "-C", dbstore, *a], capture_output=True, text=True, **k)
    for i in range(1, 6):
        git("fetch", "-q", "origin", "data:refs/remotes/origin/data"); head = git("rev-parse", "origin/data").stdout.strip()
        if hook_after_fetch and i == 1: hook_after_fetch()
        git("checkout", "-q", "--orphan", f"tmp-data-{i}"); git("add", "-A"); git("commit", "-qm", "snap")
        if git("push", "-q", f"--force-with-lease=data:{head}", "origin", f"tmp-data-{i}:data").returncode == 0: return i
        git("checkout", "-q", "--detach", "origin/data")           # <-- THE BUG: replaces the new DB with the remote's old DB
    return None

def test_legacy_logic_rolls_back_ledger_on_owner_commit(env):
    """Reproduces audit F-1 on the OLD logic: the run's new rows vanish yet the loop reports 'pushed'."""
    wd = env.clone("legacy"); extend_db(os.path.join(wd, "ailab.db"), os.path.join(wd, "ailab.db"), ["new1", "new2"])
    n = legacy_persist(wd, hook_after_fetch=env.owner_commit)
    assert n == 2, "old loop reported success on iteration 2"
    assert ledger_keys(env.remote_db()) == ["k1", "k2", "k3"], "OLD LOGIC BUG: new ledger rows were rolled back"

def test_new_logic_survives_owner_commit_during_run(env):
    wd, base, stash, pub = run_job(env, "job", ["new1", "new2"], hooks={"pre_commit": lambda a: env.owner_commit() if a == 1 else None})
    man = pub()
    assert ledger_keys(env.remote_db()) == ["k1", "k2", "k3", "new1", "new2"]
    assert sh("git", "-C", env.remote, "show", "data:control/commands.json") == '{"version":2,"owner":"edit"}', "owner edit preserved"
    assert man["seq"] == 1 and man["ledger_count"] == 5
    assert sh("git", "-C", env.remote, "rev-list", "--count", "data") == "1", "single atomic commit"

def test_basic_publish_manifest_and_postverify(env):
    wd, base, stash, pub = run_job(env, "job", ["n1"]); man = pub()
    rdb = env.remote_db(); fp = persist.fingerprint(rdb)
    assert fp["db_sha256"] == man["db_sha256"] and fp["persist_seq"] == man["seq"] == 1 and fp["integrity"] == "ok"
    m = json.loads(sh("git", "-C", env.remote, "show", "data:status/ledger_manifest.json")); assert m["ledger_chain_head"] == fp["ledger_chain_head"]
    wd2, base2, stash2, pub2 = run_job(env, "job2", ["n2"]); assert pub2()["seq"] == 2, "monotonic version"

def test_concurrent_writer_never_overwritten_then_rerun_merges(env):
    """Overlapping jobs A and B on the same base. A publishes first; B must NOT overwrite; B re-runs on the fresh DB and both sets of rows survive exactly once."""
    wdA, bA, sA, pubA = run_job(env, "A", ["a1", "a2"]); wdB, bB, sB, pubB = run_job(env, "B", ["b1"])
    pubA(); before = sh("git", "-C", env.remote, "rev-parse", "data")
    with pytest.raises(persist.ConflictRerun): pubB()
    assert sh("git", "-C", env.remote, "rev-parse", "data") == before, "nothing pushed on conflict"
    assert ledger_keys(env.remote_db()) == ["k1", "k2", "k3", "a1", "a2"]
    wdB2, bB2, sB2, pubB2 = run_job(env, "B2", ["b1"]); pubB2()      # re-run on fresh DB (what tools/cycle.py does)
    assert ledger_keys(env.remote_db()) == ["k1", "k2", "k3", "a1", "a2", "b1"]

def test_workflow_retry_same_run_twice_is_idempotent(env):
    wd, base, stash, pub = run_job(env, "R", ["x1"]); pub(); sha = sh("git", "-C", env.remote, "rev-parse", "data")
    persist.publish(wd, stash, base, run_id="R", sleep=0)          # same run published again (Re-run jobs)
    assert sh("git", "-C", env.remote, "rev-parse", "data") == sha
    assert ledger_keys(env.remote_db()).count("x1") == 1

def test_interrupted_before_push_leaves_remote_consistent(env):
    before = sh("git", "-C", env.remote, "rev-parse", "data")
    def die(a): raise KeyboardInterrupt("killed mid-save")
    wd, base, stash, pub = run_job(env, "I", ["z"], hooks={"pre_push": die})
    with pytest.raises(KeyboardInterrupt): pub()
    assert sh("git", "-C", env.remote, "rev-parse", "data") == before
    db = env.remote_db(); assert persist.fingerprint(db)["integrity"] == "ok" and ledger_keys(db) == ["k1", "k2", "k3"]

def test_process_sigkill_mid_save(env):
    before = sh("git", "-C", env.remote, "rev-parse", "data")
    wd = env.clone("K"); st = str(env.tmp / "K.json"); base = persist.begin(wd, state_file=st); live = os.path.join(wd, "ailab.db"); extend_db(live, live, ["kk"])
    stash = persist.stash_db(live, str(env.tmp / "kstash.db"))
    code = textwrap.dedent(f"""
        import sys, os, json, signal; sys.path.insert(0, {ROOT!r}); from app import persist
        base = json.load(open({st!r}))
        persist.publish({wd!r}, {stash!r}, base, run_id='K', hooks={{'pre_push': lambda a: os.kill(os.getpid(), signal.SIGKILL)}})""")
    r = subprocess.run([sys.executable, "-c", code]); assert r.returncode == -signal.SIGKILL
    assert sh("git", "-C", env.remote, "rev-parse", "data") == before
    assert ledger_keys(env.remote_db()) == ["k1", "k2", "k3"]
    # next run starts clean and succeeds (partial local files in the work tree are irrelevant)
    open(os.path.join(wd, "ailab.db-wal"), "wb").write(b"junk"); open(os.path.join(wd, "partial.tmp"), "w").write("x")
    wd2, b2, s2, pub2 = run_job(env, "K2", ["kk"]); pub2(); assert ledger_keys(env.remote_db())[-1] == "kk"

def test_remote_rejects_push_hook_nothing_changes(env):
    hook = os.path.join(env.remote, "hooks", "pre-receive"); open(hook, "w").write("#!/bin/sh\nexit 1\n"); os.chmod(hook, 0o755)
    before = sh("git", "-C", env.remote, "rev-parse", "data"); wd, base, stash, pub = run_job(env, "H", ["h"])
    with pytest.raises(persist.ConflictRerun): pub()
    assert sh("git", "-C", env.remote, "rev-parse", "data") == before

def test_rollback_or_history_rewrite_is_refused(env):
    wd = env.clone("RB"); base = persist.begin(wd, state_file=str(env.tmp / "rb.json"))
    d = env.tmp / "rbs"; d.mkdir()
    fewer = make_db(str(d / "fewer.db"), ["k1", "k2"])
    with pytest.raises(persist.Irreconcilable): persist.publish(wd, fewer, base, sleep=0)
    rewritten = make_db(str(d / "rew.db"), ["k1", "kX", "k3", "k4"])
    with pytest.raises(persist.Irreconcilable): persist.publish(wd, rewritten, base, sleep=0)
    assert ledger_keys(env.remote_db()) == ["k1", "k2", "k3"]

def test_corrupt_db_is_refused(env):
    wd = env.clone("C"); base = persist.begin(wd, state_file=str(env.tmp / "c.json")); bad = env.tmp / "bad.db"; bad.write_bytes(b"not a database" * 100)
    with pytest.raises(persist.Irreconcilable): persist.publish(wd, str(bad), base, sleep=0)

def test_owner_commit_between_fetch_and_push_rejected_push_retries_safely(env):
    """The lease on the exact sha rejects our push; we re-evaluate on the new tip and still never replace our DB with the remote's."""
    wd, base, stash, pub = run_job(env, "L", ["l1"], hooks={"pre_push": lambda a: env.owner_commit() if a == 1 else None})
    pub(); assert ledger_keys(env.remote_db())[-1] == "l1"
    assert json.loads(sh("git", "-C", env.remote, "show", "data:control/commands.json"))["version"] == 2

def test_rolling_daily_snapshots_and_ledger_log(env, monkeypatch):
    for i in range(1, 4):
        wd, base, stash, pub = run_job(env, f"S{i}", [f"s{i}"]); pub(); persist.append_ledger_log(wd, stash)
    files = sh("git", "-C", env.remote, "ls-tree", "--name-only", "data", "backups/daily/").split()
    assert len(files) == 1, "one snapshot per UTC day"
    log = sh("git", "-C", env.remote, "show", "ledger-log:ledger.jsonl").splitlines(); assert [json.loads(l)["uniq"] for l in log] == ["k1", "k2", "k3", "s1", "s2", "s3"]
    assert int(sh("git", "-C", env.remote, "rev-list", "--count", "ledger-log")) == 3, "append-only history kept"
    # idempotent re-append
    assert persist.append_ledger_log(wd, stash) == 0
    d = env.clone("pr"); dd = os.path.join(d, "backups", "daily")
    for day in range(10, 20): open(os.path.join(dd, f"ailab-202601{day}.db.gz"), "wb").write(b"x")
    persist._daily_snapshot(d, os.path.join(d, "ailab.db")); assert len(os.listdir(dd)) == persist.KEEP_DAILY

def test_stale_and_live_writer_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("AITL_DB", str(tmp_path / "l.db"))
    from app import db as D; from app.v3 import jobs; from datetime import datetime, timezone, timedelta
    D.init(); now = datetime.now(timezone.utc)
    ok, _ = jobs.acquire_lease("actions:1", "actions", now=now, ttl_min=25); assert ok
    ok, why = jobs.acquire_lease("actions:2", "actions", now=now + timedelta(minutes=5)); assert not ok and "lease held" in why
    ok, _ = jobs.acquire_lease("actions:2", "actions", now=now + timedelta(minutes=30)); assert ok, "stale lease may be taken over"

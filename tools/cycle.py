"""engine -> export -> verified compare-and-swap persist, with bounded RE-RUN on conflict. Usage: python tools/cycle.py <action> <dbstore_dir> <site_dir>
Replaces the old inline persist loop of run.yml (see app/persist.py for guarantees). Exit: 0 ok; 3 irreconcilable/conflict after retries (NOTHING pushed); 4 post-push verification failed."""
import os, sys, json, subprocess, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from app import persist

MAX_CYCLES = int(os.environ.get("AITL_PERSIST_CYCLES", "3"))

def out(**kw):
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a") as f:
            for k, v in kw.items(): f.write(f"{k}={v}\n")

def run_py(args, env_extra=None):
    env = dict(os.environ); env.update(env_extra or {}); return subprocess.run([sys.executable, *args], cwd=ROOT, env=env).returncode

def main(action, dbstore, site):
    dbstore = os.path.abspath(dbstore); state = os.path.join(tempfile.mkdtemp(prefix="cycle-"), "base.json"); stash = os.path.join(tempfile.mkdtemp(prefix="stash-"), "ailab.new.db")
    os.makedirs(os.path.join(dbstore, "control"), exist_ok=True)
    for cycle in range(1, MAX_CYCLES + 1):
        base = persist.begin(dbstore, state_file=state)
        os.makedirs(os.path.join(dbstore, "control"), exist_ok=True)
        print(f"cycle {cycle}/{MAX_CYCLES}: base {base['base_sha'][:8]} ledger_rows={base.get('ledger_count')} persist_seq={base.get('persist_seq')}")
        gh = tempfile.mktemp(prefix="ghout-"); open(gh, "w").close()
        envx = {"GITHUB_OUTPUT": gh, "AITL_DB": os.path.join(dbstore, "ailab.db"), "AITL_CONTROL_DIR": os.path.join(dbstore, "control")}
        rc = run_py(["ci_run.py", action], envx)
        o = dict(l.strip().split("=", 1) for l in open(gh) if "=" in l)
        changed = o.get("changed", "false") == "true"; est = o.get("engine_status", "failed" if rc else "ok")
        if rc != 0 and not o: print("::error::engine crashed before reporting"); out(changed="false", engine_status="failed", persisted="false"); return 1
        if not changed:
            out(changed="false", engine_status=est, persisted="false"); return 0
        if run_py(["export_static.py", site], {**envx, "AITL_TRIGGER": f"{os.environ.get('GITHUB_EVENT_NAME','manual')}:{action}"}) != 0: print("::warning::export_static failed")
        persist.stash_db(os.path.join(dbstore, "ailab.db"), stash)
        try:
            man = persist.publish(dbstore, stash, base, run_id=os.environ.get("GITHUB_RUN_ID", "local") + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
            print("PERSISTED", json.dumps(man))
            try: print("ledger-log +", persist.append_ledger_log(dbstore, stash))
            except Exception as e: print(f"::warning::ledger-log append failed: {e}")
            out(changed="true", engine_status=est, persisted="true", persist_seq=man.get("seq"), ledger_rows=man.get("ledger_count"))
            return 0
        except persist.ConflictRerun as e:
            print(f"::warning::persist conflict, nothing pushed ({e}); re-running on the fresh DB ({cycle}/{MAX_CYCLES})")
        except persist.Irreconcilable as e:
            print(f"::error::IRRECONCILABLE ledger state, NOTHING pushed: {e}"); out(changed="false", engine_status="failed", persisted="false", persist_failed="true"); return 3
        except persist.VerificationFailed as e:
            print(f"::error::POST-PUSH VERIFICATION FAILED: {e}"); out(changed="false", engine_status="failed", persisted="false", persist_failed="true"); return 4
    print("::error::persist conflict not resolved after bounded re-runs; nothing pushed"); out(changed="false", engine_status="failed", persisted="false", persist_failed="true"); return 3

if __name__ == "__main__": sys.exit(main(*sys.argv[1:4]))

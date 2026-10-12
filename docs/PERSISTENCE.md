# Ledger persistence on the `data` branch (v3.1)

Problem fixed (audit F-1): the old workflow loop replaced the freshly written DB with the remote's older DB after a rejected push and then reported "pushed" - a silent ledger rollback.

## How it works now (`app/persist.py`, `tools/cycle.py`, `.github/workflows/run.yml`)
1. `begin`: fetch the exact remote tip; record base sha, DB sha256, ledger row count + hash-chain head.
2. The engine runs on that DB; the finished DB is `VACUUM INTO` a stash **outside the git work tree** (git can never overwrite it).
3. `publish`: integrity_check + ledger invariants (append-only, strictly increasing ids, unique idempotency keys); remote ledger must be an exact prefix of ours (SHA-256 hash chain over every column of every row); stamp monotonic `persist_seq` (in the DB `ledger_meta` and in sidecar `status/ledger_manifest.json`).
4. ONE atomic commit on top of the remote tree (owner/gate files preserved) pushed with `--force-with-lease=data:<exact sha built on>`.
5. Post-push: re-fetch, compare DB sha256 and manifest seq; mismatch = exit 4.
6. Conflict handling: only non-DB files moved -> rebuild on the new tip keeping OUR DB. Remote DB advanced (another writer) -> `ConflictRerun`: nothing pushed; `cycle.py` re-runs the whole job on the fresh DB (idempotency keys make that safe), at most 3 cycles. Ledger not a superset of remote / corrupt -> `Irreconcilable`: exit 3, nothing pushed, a failure issue is opened by the workflow, heartbeat/job failed, deploy skipped.
7. Single-writer lease (`writer_lease`) and `concurrency: ailab-ledger` are unchanged.

## History / recovery
* `data:backups/daily/ailab-YYYYMMDD.db.gz` - one compressed snapshot per UTC day, last 7 kept, inside the same atomic commit (~5 MB each).
* Branch `ledger-log` - append-only `ledger.jsonl` (every ledger row in full), fast-forward-only pushes, real git history: the money record is recoverable to any commit even if the DB were lost.
* Existing manual backups under `backups/` and `archive/` are untouched.
* Size: `ailab.db` ~23 MB (limit 100 MB/file); `bars` grows ~0.4 MB/day; watch via manifest `db_bytes`.

## Restore
`git show data:backups/daily/ailab-<date>.db.gz | gunzip > ailab.db` (verify `sha256` against the manifest of that commit), or rebuild the ledger from `ledger-log:ledger.jsonl`.
Tests: `tests/test_persist.py` (local bare repos: overlapping jobs, concurrent writer, owner commit during run, workflow retry, SIGKILL mid-push, rejected push, rollback/rewrite refused, corrupt DB, stale lease, and a faithful reproduction of the OLD loop that loses rows).

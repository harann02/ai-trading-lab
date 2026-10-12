# Owner controls (v3): the public site is READ-ONLY
v2's browser flow (paste a GitHub token on the page) was **removed**: a repo-write token must never live in a public web page. Controls now go through GitHub's own authentication or the self-hosted backend.

1. **Actions UI (recommended, free):** Actions → *scan-monitor-deploy* → *Run workflow* → fill `settings_json` and/or `command_json` (the Controls tab builds ready-to-paste JSON). Only users with write access can run workflows.
2. **Edit `control/commands.json` on branch `data`** in the GitHub web UI; applied at the next run (`control.apply_file`).
3. **Self-hosted dashboard** (`docs/DEPLOY.md`, writer mode `selfhosted`): authenticated, CSRF-guarded, rate-limited `PUT /api/settings`, `POST /api/command`.

Every path ends in the same server-side validation (`app/settings_schema.py`: types, ranges, allocations ≤ 100 %, retired sleeves must stay 0 %, engine-controlled keys not writable) and is audited (`audit_log`, `control_applied`: pending → accepted/rejected/applied). Commands: `resume_entries`, `approve_order`, `cancel_order`, `reset_account` (typed confirmation), `decide_migration_proposal` (KEEP / APPROVED_SELL / REJECTED), `reactivate_strategy` (`REACTIVATE meanrev` + validation conditions), `start_v3_experiment` (`START NEW EXPERIMENT`), `set_writer_mode`.
Nothing is real time: the engine acts when a workflow run executes.

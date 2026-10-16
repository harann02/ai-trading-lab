# Unattended-scheduling monitoring routine

**Principle:** only unattended runs count as evidence: GitHub `schedule` (labelled `github-schedule`) and the external backup cron `repository_dispatch` `external-cron` (trigger class `external_scheduler`, labelled `external-backup`). Manual/push never count. Every run records `event_name` in `job_runs.trigger` and in heartbeat `last_ci_run` (`...|event=<name>|counts_as_unattended=<bool>`). The Verification tab counts a session only from unattended runs (source shown per session) and lists manual / push runs separately. Nothing in this repo dispatches a run to simulate the schedule.

## Facts that make cron work (checked via API 2026-10-12)
* Schedule triggers only run from the **default branch (`main`)**; the three workflows (`scan-monitor-deploy`, `watchdog`, `ai-probe`, `ci`) were `active` on main.
* GitHub disables scheduled workflows after 60 days without repo activity (`disabled_inactivity`); the periodic bot commits to `data` do not necessarily count as default-branch activity, so re-check `state` monthly.
* Cron is UTC, best-effort; start times can be delayed 5-60 min under load and a run can be dropped. Minimum interval 5 min.

## Cron coverage (DST safe)
| Cron (UTC) | Job | Coverage |
|---|---|---|
| `4,19,34,49 13-21 * * 1-5` | auto (monitor) | 13:04-21:49: covers EDT session 13:30-20:00 and EST 14:30-21:00 incl. post-close; `tools/gate.py` skips (and records) off-hours/holidays/early closes. 21:34/21:49 also take the EOD scan if it is still missing |
| `41 21 * * 1-5` | auto (EOD scan slot) | fallback slot after close + 30 min in both EDT and EST; 05:41 MYT (Tue-Sat). In US summer the 20:34/20:49 UTC monitor slots usually do the scan first (~04:34 MYT) |
| `13 2 * * 6` | weekly report | Sat 10:13 MYT |
| watchdog `56 21 * * 1-5`, `11 */3 * * 1-5` | watchdog | opens an issue on missed runs / failures |
| `repository_dispatch` `external-cron` | auto (optional external backup) | see `docs/EXTERNAL_CRON.md`; recorded as `external_scheduler` |
All minutes are off-grid (never :00/:15/:30/:45) and no two schedules share a minute: on 2026-10-12 the old `30 21` scan collided with `*/15` and GitHub delivered a single run (classified monitor), so the scan was lost (issue #3).
Scan vs monitor is decided by **intent** (`app/v3/schedule.classify`), not by which cron string fired: any unattended run inside the EOD-scan window (from NYSE close + 30 min — 20:30 UTC in EDT, 21:30 UTC in EST, 18:30 UTC on 13:00 ET early closes — until the next open, and only once the final daily bar is available) that finds no successful scan for that session performs it; idempotency key `scan:<session>` (shared by all unattended triggers) guarantees one scan per session.

## Routine (what to check and when)
1. `GITHUB_TOKEN=<read token> python tools/check_schedule.py` (read-only): workflow `state` must be `active`; `schedule runs total` must grow.
2. After the first in-session slot (Mon 13:30 UTC = 21:30 MYT): expect `schedule` runs of `scan-monitor-deploy` roughly every 15 min 13:30-20:15 UTC (first one may start up to ~15-60 min late).
3. From NYSE close + 30 min (20:30 UTC ≈ 04:30 MYT in US summer, 21:30 UTC ≈ 05:30 MYT in winter): one unattended scan run (first GitHub-cron or external-backup run once final bars exist) and the watchdog run at ~21:50 UTC.
4. In the dashboard Verification tab: "scheduled (cron) runs recorded" > 0; forward session row for the session should be QUALIFIED only when all 15 items are satisfied from schedule runs. Manual runs appear under "Manual / push runs (NOT counted)".
5. If no `schedule` run exists 60+ min after an expected slot: check (a) workflow state via API, (b) that the workflow file is on `main`, (c) cron syntax, (d) repo inactivity, (e) Actions enabled for the repo. Do not "fix" it by dispatching and counting that run.
6. After each run check `data` branch `status/ledger_manifest.json`: `seq` increases by 1 per persisted run, `ledger_count` never decreases.

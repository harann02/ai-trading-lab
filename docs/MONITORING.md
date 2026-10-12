# Unattended-scheduling monitoring routine

**Principle:** only runs whose GitHub `event_name` is `schedule` count as unattended evidence. Every run records `event_name` in `job_runs.trigger` and in heartbeat `last_ci_run` (`...|event=<name>|counts_as_unattended=<bool>`). The Verification tab counts a session only from `schedule` runs and lists manual / push runs separately. Nothing in this repo dispatches a run to simulate the schedule.

## Facts that make cron work (checked via API 2026-10-12)
* Schedule triggers only run from the **default branch (`main`)**; the three workflows (`scan-monitor-deploy`, `watchdog`, `ai-probe`, `ci`) were `active` on main.
* GitHub disables scheduled workflows after 60 days without repo activity (`disabled_inactivity`); the periodic bot commits to `data` do not necessarily count as default-branch activity, so re-check `state` monthly.
* Cron is UTC, best-effort; start times can be delayed 5-60 min under load and a run can be dropped. Minimum interval 5 min.

## Cron coverage (DST safe)
| Cron (UTC) | Job | Coverage |
|---|---|---|
| `*/15 13-21 * * 1-5` | monitor | 13:00-21:59: covers EDT session 13:30-20:00 and EST 14:30-21:00 incl. post-close; `tools/gate.py` skips (and records) off-hours/holidays/early closes |
| `30 21 * * 1-5` | scan | after the close in both EDT (20:00) and EST (21:00); lands 05:30 MYT next day (Tue-Sat) |
| `0 2 * * 6` | weekly report | Sat 10:00 MYT |
| watchdog `50 21 * * 1-5`, `5 */3 * * 1-5` | watchdog | opens an issue on missed runs / failures |
Note the 21:30 UTC slot fires scan and monitor crons together; the self-healing catch-up scan covers a displaced scan.

## Routine (what to check and when)
1. `GITHUB_TOKEN=<read token> python tools/check_schedule.py` (read-only): workflow `state` must be `active`; `schedule runs total` must grow.
2. After the first in-session slot (Mon 13:30 UTC = 21:30 MYT): expect `schedule` runs of `scan-monitor-deploy` roughly every 15 min 13:30-20:15 UTC (first one may start up to ~15-60 min late).
3. After 21:30 UTC: one `schedule` scan run (+ monitor run) and the watchdog run at ~21:50 UTC.
4. In the dashboard Verification tab: "scheduled (cron) runs recorded" > 0; forward session row for the session should be QUALIFIED only when all 15 items are satisfied from schedule runs. Manual runs appear under "Manual / push runs (NOT counted)".
5. If no `schedule` run exists 60+ min after an expected slot: check (a) workflow state via API, (b) that the workflow file is on `main`, (c) cron syntax, (d) repo inactivity, (e) Actions enabled for the repo. Do not "fix" it by dispatching and counting that run.
6. After each run check `data` branch `status/ledger_manifest.json`: `seq` increases by 1 per persisted run, `ledger_count` never decreases.

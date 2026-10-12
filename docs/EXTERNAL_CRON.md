# External backup cron (free, optional)

GitHub Actions `schedule` is best-effort (delays, dropped runs; see issue #3). A free external cron service can POST a
`repository_dispatch` event as a **backup trigger**. Nothing is set up by default and no external account has been created.

## How it behaves
* `.github/workflows/run.yml` listens for `repository_dispatch` with `types: [external-cron]` and runs `action=auto`, exactly like a cron run:
  calendar gate, then intent-based classification (`app/v3/schedule.classify`): EOD scan if the last session's scan is due and missing, else monitor.
* `client_payload` is **ignored**: an external caller cannot choose an action, change settings, enable strategies or trade manually.
* Idempotent: the scan key `scan:<session>` is shared with cron runs, so a backup call right after a successful cron scan becomes a monitor (no double scan, no double trade). The workflow `concurrency: ailab-ledger` group + writer lease serialise runs.
* Recorded honestly as trigger class **`external_scheduler`** in `job_runs` / run logs / the dashboard. It is **never** counted as `event=schedule`: forward-verification sessions and "scheduled (cron) runs recorded" stay schedule-only; external runs have their own counters (`external_scheduler_runs_recorded`, `external_scheduler_scan_sessions`).

## Setup (what the user needs to do, ~10 min, free)
1. **Fine-grained personal access token** (GitHub → Settings → Developer settings → Fine-grained tokens → Generate):
   * Repository access: **Only select repositories → `harann02/ai-trading-lab`**.
   * Repository permissions: **Contents: Read and write** (required by the `repository_dispatch` API). Metadata: Read (automatic). Nothing else. (Actions: write is *not* needed for repository_dispatch; only add it if you later switch to `workflow_dispatch`.)
   * Expiry: as short as you are comfortable with (e.g. 90 days); set a reminder to rotate.
2. **cron-job.org** (free account) → Create cronjob:
   * URL: `https://api.github.com/repos/harann02/ai-trading-lab/dispatches`
   * Method: **POST**; Request body: `{"event_type":"external-cron"}`
   * Headers: `Accept: application/vnd.github+json`, `Authorization: Bearer <YOUR_TOKEN>`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`
   * Schedule (UTC, Mon-Fri) — suggested, off-grid and off the GitHub slots: **21:58 and 23:58 UTC** (scan backup after the close in EDT and EST), optionally `:26` every 30 min 13-21 UTC for monitor backup. Keep it sparse; every call is a full workflow run (Actions minutes).
   * Expected response: **HTTP 204**.
3. Nothing to change in the repo. The first backup run shows up in Actions with event `repository_dispatch` and in `job_runs.trigger = external_scheduler`.

## Security notes
* The token can push to this repo's contents: store it only in cron-job.org, never in the repo. Revoke it at GitHub → Fine-grained tokens if leaked.
* Disable: pause/delete the cron-job.org job or revoke the token. No code change needed.

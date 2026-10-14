# External backup cron (free, optional)

GitHub Actions `schedule` is best-effort (delays, dropped runs; see issue #3). A free external cron service can POST a
`repository_dispatch` event as a **backup trigger**. Nothing is set up by default and no external account has been created.

## How it behaves
* `.github/workflows/run.yml` listens for `repository_dispatch` with `types: [external-cron]` and runs `action=auto`, exactly like a cron run:
  calendar gate, then intent-based classification (`app/v3/schedule.classify`): EOD scan if the last session's scan is due and missing, else monitor.
* `client_payload` is **ignored**: an external caller cannot choose an action, change settings, enable strategies or trade manually.
* Idempotent: the scan key `scan:<session>` is shared with cron runs, so a backup call right after a successful cron scan becomes a monitor (no double scan, no double trade). The workflow `concurrency: ailab-ledger` group + writer lease serialise runs.
* Recorded as trigger class **`external_scheduler`** in `job_runs` (heartbeat `last_ci_run` shows `source=external-backup`). **It COUNTS toward the 20-session forward verification** as an unattended run (user-approved 2026-10-15), labelled `external-backup` per run/session, separately from `github-schedule`; see the `source_breakdown` in the verification JSON and the Verification tab. Manual `workflow_dispatch` / `push` runs still never count.

## Setup (what the user needs to do, ~10 min, free)
1. **Fine-grained personal access token** (GitHub → Settings → Developer settings → Fine-grained tokens → Generate):
   * Repository access: **Only select repositories → `harann02/ai-trading-lab`**.
   * Repository permissions: **Contents: Read and write** (required by the `repository_dispatch` API). Metadata: Read (automatic). Nothing else. (Actions: write is *not* needed for repository_dispatch; only add it if you later switch to `workflow_dispatch`.)
   * Expiry: as short as you are comfortable with (e.g. 90 days); set a reminder to rotate.
2. **cron-job.org** (free account) → Create cronjob:
   * URL: `https://api.github.com/repos/harann02/ai-trading-lab/dispatches`
   * Method: **POST**; Request body: `{"event_type":"external-cron"}`
   * Headers: `Accept: application/vnd.github+json`, `Authorization: Bearer <YOUR_TOKEN>`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`
   * Schedule (UTC, Mon-Fri) — off-grid, never on GitHub's own minutes (:04/:19/:34/:49, 21:41):
     * **Monitor backup:** every 15 min at **:08, :23, :38, :53**, hours **13-21 UTC** (13:08 … 21:53), Mon-Fri. Crontab: `8,23,38,53 13-21 * * 1-5`
     * **EOD scan backup:** **21:58** and **23:58 UTC**, Mon-Fri. Crontab: `58 21,23 * * 1-5`
     * (cron-job.org takes one schedule per job: create two jobs with identical URL/headers/body, or one "custom" schedule per the above.) Every call is a full workflow run (Actions minutes); runs outside the NYSE session are gated to a cheap no-op/monitor by `tools/gate.py`.
   * Expected response: **HTTP 204**.
3. Nothing to change in the repo. The first backup run shows up in Actions with event `repository_dispatch` and in `job_runs.trigger = external_scheduler`.

## Security notes
* The token can push to this repo's contents: store it only in cron-job.org, never in the repo. Revoke it at GitHub → Fine-grained tokens if leaked.
* Disable: pause/delete the cron-job.org job or revoke the token. No code change needed.

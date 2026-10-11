# Interactive controls on a free static host (no persistent server)

**Architecture** - GitHub is the backend. Nothing here is real time: *the engine only runs when a GitHub Actions workflow runs* (scheduled every ~15 min during US hours, best-effort; or when you trigger it). Data is delayed yfinance.

1. Owner opens the Pages site -> **Controls** tab -> pastes a GitHub token (fine-grained: repo `harann02/ai-trading-lab`, permissions *Contents: read/write* and *Actions: read/write*; or a classic token with `repo`). The token is kept in **sessionStorage only** (cleared when the tab closes), never written to the repo, never sent anywhere except `api.github.com`.
2. The page verifies it (`GET /user`, `GET /repos/...` must show push permission and the owner login). Visitors without a valid owner token see **read-only** pages.
3. Saving settings/commands writes `control/commands.json` on branch **`data`** through the Contents API (optimistic concurrency with the file SHA; merged, version bumped) and calls `POST .../actions/workflows/run.yml/dispatches` (`action=control`).
4. The workflow checks out `data`, `control.apply_file` **re-validates everything server-side** (`app/settings_schema.py`: types, ranges, allocations <= 100%, ticker syntax; engine-controlled keys like `entries_suspended` are not writable), applies it, logs `settings_changed` / `settings_rejected` in `audit_log` (and the ledger-adjacent `control_applied`, one row per command id => each command applies once), runs the engine step, exports JSON and redeploys Pages.
5. The page shows **"applying..."** and polls `data/meta.json` every 20 s; when `control.json` reports the command as applied, the status flips and the dashboard refreshes. Last-update time is always visible in the header.

Supported: strategy on/off + allocation, risk % per trade, max exposure/position/cash reserve, max positions, drawdown limit, watchlist add/block lists, automation ON/OFF, manual-approval mode, approve/cancel a pending order, **resume after a drawdown halt**, **reset account** (typed confirmation `RESET MY ACCOUNT`; the old DB is archived first).

Security notes: the repo is public, so the *control file itself is public* (it contains only settings/commands). Only a token with write access can change it; a malicious write would still be validated and audited by the engine. Use a fine-grained token limited to this repo and revoke it when done.
Self-hosting (FastAPI) offers the same actions through authenticated, CSRF-guarded, rate-limited endpoints (`/api/docs`).

# Migration v2.0 → v3.0 (+ rollback)

## Principles
* Forward-only schema migration #3 (`m3_v3`), run automatically by `engine.ready()` → `migrations.apply_all`, **after** an online SQLite backup (`archive/ailab-premigrate-v3-<UTC>.db`, written next to the DB, therefore committed to branch `data`).
* The ledger stays append-only: v3 adds new row kinds, never edits or deletes rows.
* Migration timestamp = `ledger_meta.v3_applied_ts` (shown on the Migration tab). The **forward-verification start session** is the first NYSE session whose open is after it.

## What the migration does (all in one transaction, idempotent)
1. Archives the complete v2 account state (sleeves, orders, FX conversions, equity curve, settings) → `v2_archive` (append-only; shown read-only on the **Migration** tab).
2. Cancels still-pending **BUY** orders of the retired sleeves (`baseline`, `meanrev`) with audit entries (unfilled, nothing to sell).
3. Moves free USD cash of retired sleeves to `swing` with paired journal rows `SLEEVE_XFER_OUT` / `SLEEVE_XFER_IN`. They carry the USD amount **and its MYR cost basis + FX spread already paid**, so: not a deposit, not a profit, **no second FX conversion/spread**, realized P&L untouched.
4. **Never sells.** If a retired sleeve holds shares, a `migration_proposals` row `PENDING_OWNER_APPROVAL` is written (decide with command `decide_migration_proposal`: `KEEP` / `APPROVED_SELL` / `REJECTED`). Until decided the position keeps protective monitoring/exits.
5. Sets v3 defaults: swing 100 % of the pool, meanrev/baseline/intraday 0 %; max exposure 80 %, cash reserve 20 %, 20 % per stock, 1 % risk/trade, max 5 positions; `strategy_flags` (meanrev = `RESEARCH ONLY — NEW TRADES DISABLED`).
6. Reconciliation (`tools/reconcile_migration.py before.db after.db`): net deposits, MYR cash, USD cash, sleeve cash sum, invested MYR, USD received, realized P&L, FX spread paid, positions, equity-at-cost must be **identical**.

## Procedure used for the live DB
1. `backups/` copy on branch `data` + `/workspace/backups` (online backup, integrity_check ok, sha256).
2. Migration dry-run on a **copy** of the live DB, reconciliation PASS (see results in the PR / final report).
3. Merge `v3-dev` → `main` only after CI green. The first v3 workflow run applies the migration to the live DB (it creates its own `archive/` backup first) and publishes the snapshot.
4. Post-migration reconciliation against the backup (same tool) and screenshots.

## Rollback
* **Before the first live v3 run:** nothing to roll back (revert the merge commit).
* **After:** (1) disable the workflow (Actions → disable) so nothing writes; (2) `git checkout data`; copy the chosen backup over `ailab.db` (`scripts/restore.sh backups/<file>.db ailab.db` verifies sha256 + integrity); (3) set the repo back to the `v2.0.0` tag (`git revert` the merge or `git reset` a branch to the tag, then re-enable); (4) commit to `data`. Everything v3 wrote after the backup (journal rows, forward sessions, reports) is lost by design; the v2 account is exactly as it was.
* v2.0 code and history stay reachable via tag `v2.0.0` and branch `v2-dev`; the v2 account view stays in `v2_archive`.

## Fresh RM10,000 v3 experiment (optional, **not triggered**)
Command `start_v3_experiment` with `confirm = "START NEW EXPERIMENT"` archives the current account (existing `reset_account` path) and records an `account_epochs` row. It is never run automatically; the owner decides.

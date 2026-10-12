#!/usr/bin/env bash
# Restore a backup (keeps a .pre-restore copy). STOP web+scheduler first. Usage: scripts/restore.sh <backup.db> [db_path]
set -euo pipefail
B="$1"; DB="${2:-${AITL_DB:-data/ailab.db}}"
[ -f "$B.sha256" ] && (cd "$(dirname "$B")" && sha256sum -c "$(basename "$B").sha256")
python3 - "$B" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"; print("backup integrity ok, ledger rows:", c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0])
PY
[ -f "$DB" ] && cp "$DB" "$DB.pre-restore-$(date -u +%Y%m%dT%H%M%SZ)"
cp "$B" "$DB"; rm -f "$DB-wal" "$DB-shm"; echo "restored $B -> $DB"

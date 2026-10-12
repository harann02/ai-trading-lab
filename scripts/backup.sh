#!/usr/bin/env bash
# Online SQLite backup + integrity check + checksum. Usage: scripts/backup.sh [db_path] [dest_dir]
set -euo pipefail
DB="${1:-${AITL_DB:-data/ailab.db}}"; DEST="${2:-backups}"; mkdir -p "$DEST"
OUT="$DEST/ailab-$(date -u +%Y%m%dT%H%M%SZ).db"
python3 - "$DB" "$OUT" <<'PY'
import sqlite3, sys
s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); s.backup(d)
ok = d.execute("PRAGMA integrity_check").fetchone()[0]; n = d.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
print(f"backup ok integrity={ok} ledger_rows={n}"); sys.exit(0 if ok == "ok" else 1)
PY
sha256sum "$OUT" | tee "$OUT.sha256"; echo "$OUT"

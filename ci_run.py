"""One CI run: python ci_run.py scan_bursa|scan_us|scan_all|monitor|weekly|export
Idempotent: scans/orders/fills use UNIQUE keys in the append-only ledger, so repeating a run never duplicates trades."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import engine, jobs, db
a = sys.argv[1] if len(sys.argv) > 1 else "export"
engine.ready()
db.event("INFO", "actions", f"GitHub Actions run: {a}")
if a in ("scan_bursa", "scan_all"): jobs.scan_market("BURSA")
if a in ("scan_us", "scan_all"): jobs.scan_market("US")
if a in ("monitor", "scan_all", "scan_bursa", "scan_us"):   # monitor fills pending orders / checks stops only while a market is open
    jobs.monitor("BURSA"); jobs.monitor("US")
if a == "weekly": jobs.weekly()
db.heartbeat("last_ci_run", a)

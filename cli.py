"""CLI: python cli.py scan [BURSA|US] | monitor | backtest | report | status"""
import sys, json
from datetime import datetime, timezone
from app import engine, data, db, reports, backtest

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"; engine.ready()
    if cmd == "scan":
        m = sys.argv[2].upper() if len(sys.argv) > 2 else None
        r = engine.scan(data.YFProvider(), market=m); print(json.dumps(r, indent=1, default=str))
    elif cmd == "monitor": print(engine.execute_pending(data.YFProvider()))
    elif cmd == "backtest":
        r = backtest.run(); backtest.save(r); print(json.dumps({k: r[k] for k in ("label", "period", "selected_params", "test_result")}, indent=1, default=str))
    elif cmd == "report": print(reports.daily_report()["narrative"])
    else: print("usage: cli.py scan|monitor|backtest|report")
main()

"""CLI (v2.0, US only): python cli.py scan | monitor | validate | report"""
import sys, json
from datetime import datetime, timezone
from app import engine, data, db, reports, backtest, trading, research
from app.providers.yf import YFinanceProvider

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"; engine.ready()
    if cmd == "scan":
        r = trading.run_scan(YFinanceProvider()); print(json.dumps({k: v for k, v in r.items() if k != "capabilities"}, indent=1, default=str)[:4000])
    elif cmd == "monitor": print(trading.execute_pending(YFinanceProvider()))
    elif cmd == "validate": import run_validation; run_validation.main()
    elif cmd == "backtest":
        print("legacy v1 backtester retired; use: python cli.py validate")
    elif cmd == "report": print(reports.daily_report()["narrative"])
    else: print("usage: cli.py scan|monitor|backtest|report")
main()

#!/usr/bin/env bash
# Start web + scheduler in the background without systemd (nohup). Stop with ./run.sh stop
cd "$(dirname "$0")"; mkdir -p logs
case "$1" in
 stop) pkill -f "uvicorn app.main:app"; pkill -f "python scheduler.py";;
 *) nohup .venv/bin/uvicorn app.main:app --host ${HOST:-127.0.0.1} --port ${PORT:-8765} >> logs/web.log 2>&1 &
    nohup .venv/bin/python scheduler.py >> logs/scheduler.log 2>&1 &
    echo "web: http://127.0.0.1:${PORT:-8765}  (scheduler running)";;
esac

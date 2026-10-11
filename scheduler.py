"""Standalone persistent scheduler process (independent of the web server/browser).
Run:  .venv/bin/python scheduler.py      (see deploy/ai-trading-lab-scheduler.service)
Jobs persist in data/scheduler.db (SQLAlchemy job store) and survive restarts; missed runs within the grace window fire on startup."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from app import config, db, engine, jobs

def build():
    os.makedirs(config.ROOT / "data", exist_ok=True)
    s = BlockingScheduler(jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{config.ROOT / 'data' / 'scheduler.db'}")},
                          job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3 * 3600}, timezone="Asia/Kuala_Lumpur")
    KL, NY = "Asia/Kuala_Lumpur", "America/New_York"
    add = lambda fn, id, trig, **kw: s.add_job(fn, trig, id=id, replace_existing=True, **kw)
    # v2.0: US only. Bursa jobs were removed (their old ids are deleted from the persistent job store below).
    add("app.jobs:scan_market", "scan_us", CronTrigger(day_of_week="tue-sat", hour=5, minute=30, timezone=KL), args=["US"])   # after NYSE close (04:00/05:00 MYT)
    add("app.jobs:monitor", "monitor_us", CronTrigger(day_of_week="mon-fri", hour="9-16", minute="*/10", timezone=NY), args=["US"])
    add("app.jobs:weekly", "weekly_review", CronTrigger(day_of_week="sat", hour=10, minute=0, timezone=KL))
    for old in ("scan_bursa", "monitor_bursa"):
        try: s.remove_job(old)
        except Exception: pass
    add("app.jobs:beat", "heartbeat", CronTrigger(second="*/30", timezone=KL))
    return s

if __name__ == "__main__":
    engine.ready(); s = build(); jobs._SCHED = s
    db.event("INFO", "scheduler", "scheduler process started")
    jobs.beat()
    try: s.start()
    except (KeyboardInterrupt, SystemExit): db.event("WARN", "scheduler", "scheduler stopped")

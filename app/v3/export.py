"""v3 dashboard data (exported to site/data/v3.json): scheduler health, forward verification, benchmarks, AI status, policy flags, migration, archive of v2, provenance, reports, security mode.
Everything is derived from the ledger/DB. No secrets are ever included (see tests: snapshot secret scan)."""
import json
from datetime import datetime, timezone
from .. import db, ai, calendars as cal, ledger, config
from . import schedule as S, verification as V, shadow, policy, provenance, daily_report, jobs, VERSION

def _rows(sql, args=()):
    c = db.conn(); r = [dict(x) for x in c.execute(sql, args)]; c.close(); return r

def _latest_report(kind):
    c = db.conn(); r = c.execute("SELECT period,ts,body FROM reports WHERE kind=? ORDER BY period DESC LIMIT 1", (kind,)).fetchone(); c.close()
    return {"period": r["period"], "ts": r["ts"], **json.loads(r["body"])} if r else None

def build(cfg=None, now=None):
    cfg = cfg or db.get_settings(); now = now or datetime.now(timezone.utc); out = {"version": VERSION, "generated_utc": now.isoformat(timespec="seconds"), "generated_myt": now.astimezone(ledger.MYT).strftime("%Y-%m-%d %H:%M MYT")}
    def safe(name, fn):
        try: out[name] = fn()
        except Exception as e: out[name] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    safe("scheduler", lambda: S.health(now, cfg)); safe("verification", lambda: V.summary(now)); safe("benchmarks", lambda: shadow.comparison(now, cfg)); safe("ai", lambda: ai.status())
    safe("policy", lambda: {"flags": policy.all_flags(), "primary": policy.PRIMARY, "reactivation_rules": policy.REACTIVATION_RULES})
    safe("migration", lambda: {"log": _rows("SELECT ts,step,detail FROM v3_migration_log ORDER BY id"), "proposals": _rows("SELECT * FROM migration_proposals ORDER BY id"),
                              "applied_ts": V._meta("v3_applied_ts"), "backup": V._meta("v3_backup_ref"), "epochs": _rows("SELECT * FROM account_epochs ORDER BY id")})
    safe("archive_v2", lambda: [{"ts": r["ts"], "kind": r["kind"], "payload": json.loads(r["payload"])} for r in _rows("SELECT * FROM v2_archive ORDER BY id")])
    safe("data_requests", lambda: provenance.recent(25)); safe("daily_reports", lambda: daily_report.latest(10)); safe("weekly_eval", lambda: _latest_report("weekly_eval"))
    safe("jobs", lambda: _rows("SELECT job,trigger,scheduled_for,started_ts,finished_ts,status,skip_reason,error,run_id FROM job_runs ORDER BY id DESC LIMIT 60"))
    safe("market_data", lambda: S.data_freshness(now, cfg))
    out["labels"] = provenance.LABELS
    out["security"] = {"pages": "READ-ONLY static snapshot. No token input exists on this site.", "writer_mode": jobs.writer_mode(), "owner_controls": ["GitHub Actions 'Run workflow' inputs (settings_json / command_json)", "edit control/commands.json on branch data (GitHub web UI)", "self-hosted authenticated FastAPI dashboard (writer_mode=selfhosted)"]}
    return out

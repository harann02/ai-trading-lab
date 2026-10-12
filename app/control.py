"""Control plane for the GitHub-hosted (no persistent server) deployment.
The static dashboard (owner token entered in the browser, sessionStorage only) writes control/commands.json on the `data` branch via the GitHub Contents API and
triggers the Actions workflow; at the next run the engine calls apply_file() which RE-VALIDATES everything server-side, applies it, and logs every change to audit_log.
Authority = repo write access (a non-owner cannot push to the branch). Unknown/invalid items are rejected and recorded, never partially trusted."""
import json, hashlib, shutil
from datetime import datetime, timezone
from pathlib import Path
from . import db, config, engine, ledger, settings_schema as ss

def _iso(): return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _record(cid, status, detail):
    c = db.conn(); c.execute("INSERT OR REPLACE INTO control_applied VALUES(?,?,?,?)", (cid, _iso(), status, json.dumps(detail, default=str))); c.commit(); c.close()

def _done(cid):
    c = db.conn(); r = c.execute("SELECT 1 FROM control_applied WHERE command_id=?", (cid,)).fetchone(); c.close(); return bool(r)

def apply_settings(patch, actor="owner", source="control", now=None):
    cur = db.get_settings(); clean, errs = ss.validate_patch(patch, cur)
    if errs: db.audit("settings_rejected", {"patch": patch, "errors": errs}, actor, source); return {"ok": False, "errors": errs}
    changed = {k: {"from": cur[k], "to": v} for k, v in clean.items() if cur.get(k) != v}
    if changed:
        db.set_settings(clean); db.audit("settings_changed", changed, actor, source)
        db.event("INFO", "control", f"settings changed by {actor} via {source}: {list(changed)}")
    return {"ok": True, "changed": changed}

def _cmd(cmd, now):
    t = cmd.get("type")
    if t == "approve_order":
        oid = int(cmd["order_id"]); c = db.conn(); r = c.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone(); c.close()
        if not r or r["status"] != "pending": raise ValueError(f"order {oid} not pending")
        engine.approve_order(oid, now); return {"approved": oid}
    if t == "cancel_order":
        oid = int(cmd["order_id"]); engine.cancel_order(oid, now); return {"canceled": oid}
    if t == "resume_entries":
        if cmd.get("confirm") != "RESUME": raise ValueError("confirm must equal 'RESUME'")
        engine.resume_entries(now); return {"resumed": True}
    if t == "set_strategy":
        return apply_settings({"strategies": {cmd["strategy"]: {k: cmd[k] for k in ("enabled", "allocation_pct") if k in cmd}}}, source="control-cmd")
    if t == "reset_account":
        if cmd.get("confirm") != "RESET": raise ValueError("typed confirmation must equal 'RESET'")
        return reset_account(now)
    if t == "reactivate_strategy":
        from .v3 import policy; return policy.reactivate(cmd.get("strategy", "meanrev"), cmd.get("confirm", ""), now)
    if t == "decide_migration_proposal":
        pid = int(cmd["proposal_id"]); dec = cmd.get("decision")
        if dec not in ("APPROVED_SELL", "KEEP", "REJECTED"): raise ValueError("decision must be APPROVED_SELL|KEEP|REJECTED")
        c = db.conn(); r = c.execute("SELECT * FROM migration_proposals WHERE id=?", (pid,)).fetchone()
        if not r or r["status"] != "PENDING_OWNER_APPROVAL": c.close(); raise ValueError("proposal not pending")
        c.execute("UPDATE migration_proposals SET status=?, decided_ts=?, decided_by=? WHERE id=?", (dec, _iso(), cmd.get("actor", "owner"), pid)); c.commit(); c.close()
        if dec == "APPROVED_SELL":
            engine.create_order(decision_key=f"MIGSELL:{pid}", decision_id=None, symbol=r["symbol"], market="US", side="SELL", qty=json.loads(r["detail"])["qty"], ref_price=0.0, stop=None, target=None,
                                reason=f"owner-approved sale of retired-sleeve holding (migration proposal #{pid})", now=now, strategy_id=r["strategy_id"], signal_status="EXIT")
        return {"proposal": pid, "decision": dec}
    if t == "set_writer_mode":
        from .v3 import jobs; jobs.set_writer_mode(cmd["mode"], cmd.get("actor", "owner")); return {"writer_mode": cmd["mode"]}
    if t == "start_v3_experiment":
        if cmd.get("confirm") != "START NEW EXPERIMENT": raise ValueError("confirm must equal 'START NEW EXPERIMENT'")
        r = reset_account(now); c = db.conn(); c.execute("INSERT INTO account_epochs(ts,label,archive_path,note) VALUES(?,?,?,?)", (_iso(), "v3 fresh RM10,000 experiment", r["archived"], "explicit owner reset; previous account archived")); c.commit(); c.close(); return r
    raise ValueError(f"unknown command type {t!r}")

def apply_dispatch(settings_json, command_json, actor, run_id, now):
    """Owner controls via the GitHub Actions UI (workflow_dispatch inputs). GitHub itself authenticates the actor (write access required); the engine re-validates everything."""
    out = []
    if settings_json and settings_json.strip():
        try: patch = json.loads(settings_json)
        except Exception as e: return [{"ok": False, "error": f"settings_json invalid: {e}"}]
        r = apply_settings(patch, actor, "workflow_dispatch", now); _record(f"dispatch-settings:{run_id}", "applied" if r["ok"] else "rejected", r); out.append({"settings": r})
    if command_json and command_json.strip():
        try: cmd = json.loads(command_json)
        except Exception as e: out.append({"ok": False, "error": f"command_json invalid: {e}"}); return out
        cid = f"dispatch-command:{run_id}"
        try: r = _cmd(cmd, now); _record(cid, "applied", r); db.audit(f"command:{cmd.get('type')}", {**{k: v for k, v in cmd.items() if k != "confirm"}, "result": r}, actor, "workflow_dispatch"); out.append({"command": cmd.get("type"), "ok": True, **(r if isinstance(r, dict) else {})})
        except Exception as e: _record(cid, "rejected", str(e)); db.audit(f"command_rejected:{cmd.get('type')}", {"error": str(e)}, actor, "workflow_dispatch"); out.append({"command": cmd.get("type"), "ok": False, "error": str(e)})
    return out

def reset_account(now=None):
    """Archives the whole DB first (never destructive without archive), then starts a fresh RM10,000 account with v2 schema. Settings are kept."""
    p = Path(config.db_path()); arch = p.parent / "archive"; arch.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"); dest = arch / f"{p.stem}-reset-{stamp}.db"
    import sqlite3
    src = sqlite3.connect(str(p)); dst = sqlite3.connect(str(dest)); src.backup(dst); dst.close(); src.close()
    keep = db.get_settings(); c = db.conn()
    for t in ("fx_conversions", "ledger", "audit_log"):
        pass
    c.close()
    for suf in ("", "-wal", "-shm"):
        f = Path(str(p) + suf)
        if f.exists(): f.unlink()
    db.init(); db.set_settings({k: v for k, v in keep.items() if k not in ("entries_suspended", "suspend_reason", "dd_baseline_ts")})
    ledger.init_account(); db.audit("account_reset", {"archived_as": f"archive/{dest.name}"}, "owner", "control"); db.event("WARN", "reset", f"FULL RESET: old database archived as archive/{dest.name}")
    return {"archived": f"archive/{dest.name}"}

def apply_file(path, now=None):
    """Reads control/commands.json; applies new settings revision + unseen commands. Returns summary list. Never raises on bad input."""
    out = []; p = Path(path)
    if not p.exists(): return out
    if not p.read_text().strip(): return out                         # empty placeholder file = nothing to apply
    try: doc = json.loads(p.read_text())
    except Exception as e: db.audit("control_file_invalid", str(e), "unknown", "control"); return [{"error": f"invalid JSON: {e}"}]
    actor = str(doc.get("updated_by", "owner"))[:40]
    if "settings" in doc and isinstance(doc["settings"], dict):
        rev = str(doc.get("settings_rev") or hashlib.sha256(json.dumps(doc["settings"], sort_keys=True).encode()).hexdigest()[:16])[:40]; cid = f"settings:{rev}"
        if not _done(cid):
            r = apply_settings(doc["settings"], actor, "control-file", now); _record(cid, "applied" if r["ok"] else "rejected", r); out.append({"id": cid, **r})
    for cmd in doc.get("commands", []) or []:
        cid = str(cmd.get("id", ""))[:64]
        if not cid or _done(cid): continue
        try: r = _cmd(cmd, now); _record(cid, "applied", r); db.audit(f"command:{cmd.get('type')}", {k: v for k, v in cmd.items() if k != "confirm"} | {"result": r}, actor, "control-file"); out.append({"id": cid, "ok": True, **(r if isinstance(r, dict) else {})})
        except Exception as e: _record(cid, "rejected", str(e)); db.audit(f"command_rejected:{cmd.get('type')}", {"id": cid, "error": str(e)}, actor, "control-file"); out.append({"id": cid, "ok": False, "error": str(e)})
    return out

def status():
    c = db.conn(); rows = [dict(r) for r in c.execute("SELECT * FROM control_applied ORDER BY ts DESC LIMIT 30")]
    aud = [dict(r) for r in c.execute("SELECT ts,actor,action,detail,source FROM audit_log ORDER BY id DESC LIMIT 40")]; c.close()
    return {"applied": rows, "audit": aud}

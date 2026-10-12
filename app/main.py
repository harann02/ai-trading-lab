"""FastAPI backend (self-hosting) for AI Trading Lab v2.0: US-only paper trading. Same dashboard as the GitHub Pages site (served from site_src + live JSON export).
Security: HTTP Basic on EVERY route (fails closed 503 without DASH_USER/DASH_PASS), CSRF guard (state-changing requests must carry X-Requested-With: ailab and same-origin Origin),
rate limiting (per-IP token bucket), validated settings (same schema as the control plane), structured JSON logging, /healthz + /readyz, backup/restore of the SQLite ledger.
No secrets are ever returned by any endpoint."""
import base64, json, logging, os, secrets, shutil, sqlite3, subprocess, sys, threading, time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Body, Request, UploadFile, File
from fastapi.responses import Response, FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from . import config, db, engine, ledger, data, views, control, trading, research, settings_schema, calendars as cal, portfolio

ROOT = Path(__file__).resolve().parent.parent
LIVE = Path(os.environ.get("AITL_LIVE_DIR", ROOT / "site_live"))
app = FastAPI(title="AI Trading Lab v2 (paper trading only)", docs_url="/api/docs", openapi_url="/api/openapi.json")
_lock = threading.Lock(); _jobs = {"scan": None, "export": None}

class JsonFmt(logging.Formatter):
    def format(self, r): return json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "level": r.levelname, "logger": r.name, "msg": r.getMessage()})
log = logging.getLogger("ailab"); _h = logging.StreamHandler(); _h.setFormatter(JsonFmt()); log.addHandler(_h); log.setLevel(logging.INFO)

_buckets = defaultdict(lambda: deque())
RATE = int(os.environ.get("AITL_RATE_PER_MIN", "120")); RATE_WRITE = int(os.environ.get("AITL_RATE_WRITE_PER_MIN", "20"))
SAFE = ("GET", "HEAD", "OPTIONS")

class Guard(BaseHTTPMiddleware):
    """Auth (fail closed) -> rate limit -> CSRF for mutations -> structured access log."""
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in ("/healthz",): return await call_next(request)          # liveness only, reveals nothing
        if os.environ.get("AITL_AUTH_DISABLED") != "1":
            u, p = os.environ.get("DASH_USER", ""), os.environ.get("DASH_PASS", "")
            if not u or not p: return Response("auth not configured (set DASH_USER/DASH_PASS)", status_code=503)
            ok = False; h = request.headers.get("authorization", "")
            if h.lower().startswith("basic "):
                try:
                    gu, _, gp = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                    ok = secrets.compare_digest(gu.encode(), u.encode()) & secrets.compare_digest(gp.encode(), p.encode())
                except Exception: ok = False
            if not ok:
                log.warning(f"auth failed {request.method} {path} from {request.client.host if request.client else '?'}")
                return Response("authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="AI Trading Lab", charset="UTF-8"'})
        ip = request.client.host if request.client else "?"; now = time.time(); q = _buckets[(ip, request.method not in SAFE)]; lim = RATE if request.method in SAFE else RATE_WRITE
        while q and now - q[0] > 60: q.popleft()
        if len(q) >= lim: return Response("rate limit exceeded", status_code=429, headers={"Retry-After": "30"})
        q.append(now)
        if request.method not in SAFE:
            from .v3 import jobs as _j3
            wm = _j3.writer_mode()
            if wm != "selfhosted": return Response(f"409 ledger writer mode is '{wm}': this backend is READ-ONLY. Hand the ledger over first (command set_writer_mode=selfhosted via Actions; see docs/DEPLOY.md). Exactly one writer is allowed.", status_code=409)
            if request.headers.get("x-requested-with") != "ailab": return Response("CSRF: missing X-Requested-With header", status_code=403)
            org = request.headers.get("origin")
            if org and org.split("://", 1)[-1] != request.headers.get("host", ""): return Response("CSRF: cross-origin request refused", status_code=403)
        t0 = time.time(); resp = await call_next(request)
        log.info(f"{request.method} {path} {resp.status_code} {int((time.time()-t0)*1000)}ms"); return resp
app.add_middleware(Guard)

@app.on_event("startup")
def _start():
    engine.ready()

def now(): return datetime.now(timezone.utc)
def live_provider(): return data.YFProvider()

def _regen():
    """Regenerate the live JSON snapshot the dashboard reads (same exporter as the Pages workflow)."""
    def run():
        with _lock:
            subprocess.run([sys.executable, str(ROOT / "export_static.py"), str(LIVE)], cwd=ROOT, capture_output=True, env={**os.environ, "AITL_TRIGGER": "self-hosted"})
    if _jobs["export"] and _jobs["export"].is_alive(): return
    t = threading.Thread(target=run, daemon=True); t.start(); _jobs["export"] = t

# ------------------------------------------------------------ health / status
@app.get("/healthz")
def healthz(): return {"ok": True}

@app.get("/readyz")
def readyz():
    try:
        c = db.conn(); n = c.execute("SELECT COUNT(*) n FROM ledger").fetchone()["n"]; v = c.execute("PRAGMA user_version").fetchone()[0]; c.close(); return {"ok": True, "ledger_rows": n}
    except Exception as e: raise HTTPException(503, f"db not ready: {e}")

def _scheduler_health():
    c = db.conn(); r = c.execute("SELECT ts,info FROM heartbeat WHERE name='scheduler'").fetchone(); c.close()
    if not r: return {"healthy": False, "detail": "scheduler process never started", "next_runs": {}}
    age = (now() - datetime.fromisoformat(r["ts"])).total_seconds()
    return {"healthy": age < 120, "last_heartbeat": r["ts"], "age_s": round(age), "next_runs": json.loads(r["info"] or "{}"), "detail": "ok" if age < 120 else f"no heartbeat for {int(age)}s - scheduler DOWN"}

@app.get("/api/status")
def status():
    cfg = db.get_settings(); c = db.conn(); src = [dict(r) for r in c.execute("SELECT * FROM source_status")]; ls = c.execute("SELECT ts FROM heartbeat WHERE name='last_scan'").fetchone(); c.close()
    return {"now_utc": now().isoformat(timespec="seconds"), "now_myt": now().astimezone(ledger.MYT).strftime("%Y-%m-%d %H:%M:%S MYT"), "platform_version": config.PLATFORM_VERSION, "data_label": data.DATA_LABEL,
            "us_market": cal.us_status(now()), "sources": src, "scheduler": _scheduler_health(), "last_scan": ls["ts"] if ls else None, "scan_running": bool(_jobs["scan"] and _jobs["scan"].is_alive()),
            "entries_suspended": cfg["entries_suspended"], "suspend_reason": cfg["suspend_reason"], "automation_on": cfg["automation_on"], "manual_approval": cfg["manual_approval"], "paper_only": True}

# ------------------------------------------------------------ read API (JSON views shared with the static export)
@app.get("/api/kpis")
def kpis(): return views.kpis(db.get_settings(), now())
@app.get("/api/equity")
def equity(): return views.equity_curves(db.get_settings())
@app.get("/api/holdings")
def holdings(): return views.holdings(db.get_settings())
@app.get("/api/orders")
def orders(): return views.orders()
@app.get("/api/ledger")
def ledger_api(): return views.ledger_rows()
@app.get("/api/fx")
def fx_api(): return {"conversions": views.fx_conversions(), "current": views.fx_status(db.get_settings())}
@app.get("/api/discovery")
def discovery(): return views.discovery()
@app.get("/api/universe")
def universe_api(): return views.universe_summary()
@app.get("/api/strategies")
def strategies(): return views.strategies_view(db.get_settings())
@app.get("/api/monitor")
def monitor_api(): return views.monitor_view()
@app.get("/api/feed")
def feed(limit: int = 60): return views.feed(min(limit, 300))
@app.get("/api/candles/{symbol}")
def candles(symbol: str): return views.candles(symbol.upper()) or {}
@app.get("/api/decision/{symbol}")
def decision(symbol: str): return views.decision_for(symbol.upper())
@app.get("/api/control/status")
def control_status(): return control.status()

@app.get("/api/settings")
def get_settings():
    cfg = db.get_settings(); return {k: cfg[k] for k in settings_schema.SCHEMA if k in cfg}

@app.get("/api/export/ledger.csv")
def export_csv():
    import csv, io
    led = views.ledger_rows(100000); s = io.StringIO(); cols = list(led[0].keys()) if led else ["id"]; w = csv.writer(s); w.writerow(cols); [w.writerow([r.get(c) for c in cols]) for r in led]
    return Response(s.getvalue(), media_type="text/csv")

# ------------------------------------------------------------ write API (CSRF + rate limited + validated + audited)
@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    r = control.apply_settings(body, actor="api-owner", source="api")
    if not r["ok"]: raise HTTPException(400, "; ".join(map(str, r["errors"])) if isinstance(r["errors"], list) else str(r["errors"]))
    _regen(); return get_settings()

@app.post("/api/command")
def command(body: dict = Body(...)):
    """v3 owner commands (same validated command set as the Actions path): reactivate_strategy, decide_migration_proposal, start_v3_experiment, resume_entries, ... Typed confirmations are enforced inside the command."""
    try: r = control._cmd(body, now())
    except Exception as e: db.audit(f"command_rejected:{body.get('type')}", {"error": str(e)}, "api-owner", "api"); raise HTTPException(400, str(e))
    db.audit(f"command:{body.get('type')}", {k: v for k, v in body.items() if k != "confirm"}, "api-owner", "api"); _regen(); return {"ok": True, "result": r}

@app.get("/api/v3")
def v3_api():
    from .v3 import export as v3x; return v3x.build(db.get_settings(), now())

@app.post("/api/scan")
def scan():
    if _jobs["scan"] and _jobs["scan"].is_alive(): raise HTTPException(409, "scan already running")
    def run():
        with _lock:
            try: trading.run_scan(live_provider(), now()); trading.execute_pending(live_provider(), now()); research.generate_memos(live_provider(), now())
            except Exception as e: db.event("ERROR", "scan", f"manual scan failed: {e}")
        _regen()
    t = threading.Thread(target=run, daemon=True); t.start(); _jobs["scan"] = t; return {"started": True}

@app.post("/api/monitor")
def monitor_run():
    with _lock: out = trading.execute_pending(live_provider(), now())
    _regen(); return {"log": out}

@app.post("/api/orders/{oid}/approve")
def approve(oid: int):
    r = control._cmd({"type": "approve_order", "order_id": oid}, now()); _regen(); return {"ok": True, "result": r}
@app.post("/api/orders/{oid}/cancel")
def cancel(oid: int):
    r = control._cmd({"type": "cancel_order", "order_id": oid}, now()); _regen(); return {"ok": True, "result": r}
@app.post("/api/resume")
def resume(body: dict = Body(...)):
    if body.get("confirm") != "RESUME": raise HTTPException(400, "confirm must equal 'RESUME'")
    r = control._cmd({"type": "resume_entries"}, now()); _regen(); return {"ok": True, "result": r}
@app.post("/api/reset")
def reset(body: dict = Body(...)):
    if body.get("confirm") != "RESET MY ACCOUNT": raise HTTPException(400, "type 'RESET MY ACCOUNT' to confirm")
    r = control.reset_account(now()); _regen(); return {"ok": True, "result": r}

# ------------------------------------------------------------ backup / restore (SQLite online backup; restore validates the file)
@app.get("/api/backup")
def backup():
    dst = Path(config.db_path()).with_suffix(".backup.db")
    src = db.conn(); out = sqlite3.connect(dst); src.backup(out); out.close(); src.close()
    return FileResponse(dst, filename=f"ailab-backup-{now():%Y%m%d-%H%M}.db", media_type="application/octet-stream")

@app.post("/api/restore")
async def restore(file: UploadFile = File(...), confirm: str = ""):
    if confirm != "RESTORE": raise HTTPException(400, "pass confirm=RESTORE")
    blob = await file.read(); tmp = Path("/tmp/ailab-restore.db"); tmp.write_bytes(blob)
    try:
        t = sqlite3.connect(tmp); ok = t.execute("PRAGMA integrity_check").fetchone()[0] == "ok"; has = t.execute("SELECT name FROM sqlite_master WHERE name='ledger'").fetchone(); t.close()
        if not ok or not has: raise ValueError("not a valid AI Trading Lab ledger")
    except Exception as e: raise HTTPException(400, f"invalid backup: {e}")
    cur = Path(config.db_path())
    if cur is None: raise HTTPException(500, "db path unknown")
    shutil.copy(cur, cur.with_suffix(f".pre-restore-{now():%Y%m%d%H%M%S}.db")); shutil.copy(tmp, cur); db.init(); _regen(); return {"ok": True}

# ------------------------------------------------------------ dashboard (same frontend as Pages; JSON comes from the live export)
@app.get("/data/{name:path}")
def data_file(name: str):
    p = (LIVE / "data" / name).resolve()
    if not str(p).startswith(str((LIVE / "data").resolve())) or not p.is_file():
        if not LIVE.exists(): _regen()
        raise HTTPException(404, "snapshot not generated yet - POST /api/scan or wait a few seconds")
    return FileResponse(p, headers={"Cache-Control": "no-store"})

ALLOWED_ASSETS = ("app.js", "controls.js", "v3.js", "v3b.js", "style.css", "lightweight-charts.js")      # every asset index.html loads (F-6: v3.js/v3b.js were missing -> 404)
@app.get("/")
def index(): return FileResponse(ROOT / "site_src" / "index.html")
@app.get("/{fn}")
def assets(fn: str):
    p = ROOT / "site_src" / fn
    if fn in ALLOWED_ASSETS and p.is_file(): return FileResponse(p)
    raise HTTPException(404, "not found")

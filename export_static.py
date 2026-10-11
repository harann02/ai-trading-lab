"""Export a static READ-ONLY snapshot of the dashboard (JSON + static frontend) into ./site.
Usage: python export_static.py [outdir]   (env AITL_DB selects the SQLite file; run metadata from GITHUB_* env vars)
Uses the real FastAPI app in-process (auth bypass is local to this process), so the JSON is exactly what the live dashboard shows.
Only paper-trading data is exported. No secrets, no settings other than strategy parameters."""
import os, sys, json, shutil, csv, io
from datetime import datetime, timezone
from pathlib import Path
os.environ["AITL_AUTH_DISABLED"] = "1"          # in-process only; nothing is served
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient
from app import main, db, data, calendars as cal, ledger, config

ROOT = Path(__file__).resolve().parent
out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "site"); D = out / "data"
if out.exists(): shutil.rmtree(out)
D.mkdir(parents=True); (D / "candles").mkdir(); (D / "decision").mkdir()
for f in (ROOT / "site_src").iterdir(): shutil.copy(f, out / f.name)
(out / ".nojekyll").write_text("")

def dump(name, obj):
    p = D / name; p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, separators=(",", ":"), default=str))

with TestClient(main.app) as c:
    def get(u):
        r = c.get(u); r.raise_for_status(); return r.json()
    status = get("/api/status")
    status["scheduler"] = {"healthy": True, "detail": "GitHub Actions cron (best-effort; runs can be delayed 15-60 min)", "next_runs": {}}
    dump("status.json", status)
    dump("kpis.json", get("/api/kpis"))
    for rg in ("ALL", "1D", "1W", "1M", "3M", "YTD"): dump(f"equity_{rg}.json", get(f"/api/equity?range={rg}"))
    dump("holdings.json", get("/api/holdings"))
    disc = get("/api/discovery"); dump("discovery.json", disc)
    led = get("/api/ledger"); dump("ledger.json", led)
    cols = ["id", "ts_myt", "kind", "symbol", "market", "qty", "price_local", "ccy", "fx", "gross_myr", "fees_myr", "slippage_myr", "cash_delta_myr", "realized_pnl_myr", "order_id", "note"]
    s = io.StringIO(); w = csv.writer(s); w.writerow(cols); [w.writerow([r.get(k) for k in cols]) for r in led]; (D / "ledger.csv").write_text(s.getvalue())
    dump("orders.json", get("/api/orders"))
    dump("performance.json", get("/api/performance")); dump("analytics.json", get("/api/analytics"))
    dump("feed.json", get("/api/feed?limit=100"))
    try: dump("report_daily.json", get("/api/reports/daily"))
    except Exception as e: dump("report_daily.json", {"narrative": f"no report: {e}", "hash": "", "facts": {}})
    dump("backtest.json", get("/api/backtest"))
    st = get("/api/settings")                    # strategy parameters only (no secrets exist in settings)
    dump("settings.json", st)
    n = 0
    for d in disc:                               # per-symbol latest decision (by id and by symbol) + candles
        try:
            dd = get(f"/api/decision/{d['id']}"); dump(f"decision/{d['id']}.json", dd); dump(f"decision/{d['symbol']}.json", dd)
        except Exception: pass
        try: dump(f"candles/{d['symbol']}.json", get(f"/api/candles/{d['symbol']}")); n += 1
        except Exception as e: print("no candles", d["symbol"], e)
    for r in led:                                # decisions referenced by held positions
        pass
    # ---- meta / freshness
    now = datetime.now(timezone.utc); fresh = []
    for m in cal.MARKETS:
        syms = st["watchlist"].get(m, []); ok = 0; last = None; bad = 0
        for sym in syms:
            ds = data.data_status(sym, now); last = max(filter(None, [last, ds.get("last_bar")]), default=last)
            ok += bool(ds.get("ok")); bad += not ds.get("ok")
        fresh.append({"market": m, "symbols": len(syms), "ok_symbols": ok, "blocked_symbols": bad, "last_bar": last, "ok": bad == 0, "status": f"{ok}/{len(syms)} symbols fresh"})
    gh = os.environ
    run_url = f"{gh['GITHUB_SERVER_URL']}/{gh['GITHUB_REPOSITORY']}/actions/runs/{gh['GITHUB_RUN_ID']}" if gh.get("GITHUB_RUN_ID") else None
    dump("meta.json", {"generated_utc": now.isoformat(timespec="seconds"), "generated_myt": now.astimezone(ledger.MYT).strftime("%Y-%m-%d %H:%M MYT"),
                       "trigger": gh.get("AITL_TRIGGER") or gh.get("GITHUB_EVENT_NAME") or "local", "run_url": run_url, "run_number": gh.get("GITHUB_RUN_NUMBER"),
                       "algo_version": config.ALGO_VERSION, "freshness": fresh, "data_label": data.DATA_LABEL, "mode": "PAPER TRADING / DELAYED DATA / READ-ONLY",
                       "kpis": get("/api/kpis")})
print(f"exported to {out}: {len(list(D.rglob('*.json')))} json files, {n} charts")

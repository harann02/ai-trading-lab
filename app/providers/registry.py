"""Provider registry: builds all adapters, activates only those configured, probes capabilities, answers 'who can do X?'"""
import json
from datetime import datetime, timezone
from .base import *
from .yf import YFinanceProvider
from .alpaca import AlpacaProvider
from .polygon import PolygonProvider

def build(env=None):
    return [YFinanceProvider(), AlpacaProvider(env), PolygonProvider(env)]

def probe_all(providers=None, store=True):
    providers = providers or build(); results = []
    for p in providers:
        try: r = p.probe()
        except Exception as e: r = CapResult(p.name, p.configured(), False, {}, f"probe error: {e}")
        results.append(r)
    if store:
        from .. import db
        c = db.conn(); now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for r in results:
            c.execute("INSERT OR REPLACE INTO provider_caps VALUES(?,?,?,?,?)", (r.provider, now, int(r.active), json.dumps(r.caps), r.note))
        c.commit(); c.close()
    return results

def provider_for(results, capability, providers=None):
    """First active provider (in registry order) whose probed capability is available; else None."""
    for r in results:
        if r.has(capability): return r.provider
    return None

def stored():
    from .. import db
    c = db.conn(); rows = [dict(r) for r in c.execute("SELECT * FROM provider_caps")]; c.close()
    for r in rows: r["caps"] = json.loads(r["caps"] or "{}")
    return rows

def results_from_store():
    return [CapResult(r["provider"], bool(r["active"]), True, r["caps"], r["note"]) for r in stored()]

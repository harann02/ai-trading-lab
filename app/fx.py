"""USD/MYR: real recorded rates, configurable provider, configurable spread, full audit trail.
Spread is charged ONLY on actual conversions (fx_convert). Display/valuation uses the mid rate, never the spread."""
import json
from datetime import datetime, timezone, timedelta
from . import db, config, data, ledger

PROVIDERS = {}
def register(name):
    def deco(fn): PROVIDERS[name] = fn; return fn
    return deco

@register("yfinance")
def _yf(now=None):
    """Latest USDMYR=X from yfinance (delayed/EOD-class). Returns (rate, rate_ts_iso, source)."""
    import yfinance as yf
    df = yf.download("USDMYR=X", period="7d", interval="1d", auto_adjust=False, progress=False)
    if df is None or len(df) == 0: raise RuntimeError("no USDMYR=X data")
    import pandas as pd
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"]); v = float(df["Close"].iloc[-1])
    return v, df.index[-1].strftime("%Y-%m-%d"), "yfinance USDMYR=X last daily close (delayed)"

def fetch_rate(cfg=None, now=None) -> dict:
    """Fetch from the configured provider and store in fx_rates. Falls back to the stored bar ONLY if recent (flagged)."""
    cfg = cfg or db.get_settings(); now = now or datetime.now(timezone.utc); prov = cfg.get("fx_provider", "yfinance")
    try:
        rate, rts, src = PROVIDERS[prov](now)
        c = db.conn(); c.execute("INSERT OR REPLACE INTO fx_rates VALUES(?,?,?,?)", (rts, prov, rate, now.isoformat(timespec="seconds"))); c.commit(); c.close()
        return {"rate": rate, "rate_ts": rts, "provider": prov, "source": src, "ok": True}
    except Exception as e:
        r = current(cfg, now)
        r["error"] = f"live fetch failed: {e}"; return r

def current(cfg=None, now=None) -> dict:
    """Best stored mid rate (fx_rates table, then USDMYR=X bars). ok=False if older than fx_max_age_days."""
    cfg = cfg or db.get_settings(); now = now or datetime.now(timezone.utc); prov = cfg.get("fx_provider", "yfinance")
    c = db.conn(); r = c.execute("SELECT date,rate,fetched_at FROM fx_rates WHERE provider=? ORDER BY date DESC LIMIT 1", (prov,)).fetchone(); c.close()
    if r: d, rate = r["date"], r["rate"]; src = f"{prov} (fx_rates, fetched {r['fetched_at']})"
    else:
        df = data.load_bars(data.FX_SYMBOL, include_partial=True)
        if len(df) == 0: return {"rate": None, "rate_ts": None, "provider": prov, "source": "none", "ok": False, "status": "MISSING"}
        d, rate = str(df.index[-1].date()), float(df["close"].iloc[-1]); src = "yfinance USDMYR=X bars"
    age = (now.date() - datetime.fromisoformat(d).date()).days
    return {"rate": rate, "rate_ts": d, "provider": prov, "source": src, "ok": age <= cfg.get("fx_max_age_days", 5), "status": "OK" if age <= cfg.get("fx_max_age_days", 5) else "STALE"}

def rate_on(d, cfg=None):
    """Historical mid rate for date d (<=d) from stored bars; None if unknown."""
    v = data.fx_on(d)
    return v

def convert_myr_to_usd(strategy_id, myr, now=None, cfg=None, rate=None, note="", uniq=None):
    """Real conversion with spread. Writes FX audit row + ledger row (MYR out, USD in) atomically. Idempotent via uniq."""
    cfg = cfg or db.get_settings(); now = now or datetime.now(timezone.utc)
    r = rate or current(cfg, now)
    if not r.get("ok") or not r.get("rate"): raise RuntimeError(f"FX unavailable/stale: {r}")
    if myr <= 0: raise ValueError("amount must be positive")
    avail = ledger_cash_myr()
    if myr > avail + 1e-9: raise ValueError(f"insufficient MYR cash: {avail:.2f} < {myr:.2f}")
    mid = float(r["rate"]); sp = float(cfg["fx_spread_bps"]); eff = mid * (1 + sp / 1e4)
    usd = myr / eff; spread_cost = myr - usd * mid
    uniq = uniq or f"FX:{strategy_id}:{now.isoformat(timespec='seconds')}"
    c = db.conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute("INSERT INTO fx_conversions(ts,direction,strategy_id,myr_amount,usd_amount,mid_rate,spread_bps,effective_rate,spread_cost_myr,provider,rate_ts,rate_source,note,ledger_uniq) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ledger.iso(now), "MYR->USD", strategy_id, myr, usd, mid, sp, eff, spread_cost, r["provider"], r["rate_ts"], r["source"], note, uniq))
        cid = cur.lastrowid
        c.execute("INSERT INTO ledger(ts,kind,symbol,market,qty,price_local,ccy,fx,gross_myr,fees_myr,slippage_myr,cash_delta_myr,uniq,note,strategy_id,cash_delta_usd,gross_usd,fx_basis,fx_conv_id) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (ledger.iso(now), "FX_CONVERT", None, None, None, None, "USD", eff, myr, spread_cost, 0.0, -myr, uniq, f"convert RM{myr:.2f} -> USD{usd:.2f} @ {eff:.5f} (mid {mid:.5f} + {sp:g} bps) strategy={strategy_id}",
                   strategy_id, usd, usd, eff, cid))
        c.commit()
    except Exception as e:
        c.rollback(); c.close()
        if "UNIQUE" in str(e): return None            # duplicate conversion key -> idempotent
        raise
    c.close()
    db.audit("fx_convert", {"strategy": strategy_id, "myr": myr, "usd": usd, "mid": mid, "effective": eff, "spread_bps": sp, "spread_cost_myr": spread_cost, "rate_ts": r["rate_ts"], "source": r["source"]}, source="fx")
    return {"conversion_id": cid, "myr": myr, "usd": usd, "mid": mid, "effective": eff, "spread_cost_myr": spread_cost, "rate_ts": r["rate_ts"], "provider": r["provider"]}

def ledger_cash_myr(): return ledger.cash()

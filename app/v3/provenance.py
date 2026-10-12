"""Data provenance: every data request records provider, retrieval time, exchange (bar) time, completeness, symbol validation and staleness; numbers shown in the
UI carry one of the labels below. yfinance is NEVER labelled LIVE."""
import json, re
from datetime import datetime, timezone
import pandas as pd
from .. import db

LABELS = ("LIVE", "DELAYED", "END-OF-DAY", "HISTORICAL BACKTEST", "FORWARD PAPER", "DEMO")
TICKER = re.compile(r"^[A-Z]{1,5}([.-][A-Z])?$|^[\^A-Z]{1,6}=?X?$")
def label_for(provider_name: str, kind: str) -> str:
    """yfinance daily = END-OF-DAY; yfinance intraday = DELAYED. Only a provider that explicitly declares real-time AND is tested may be LIVE (none configured)."""
    if kind in ("daily", "eod"): return "END-OF-DAY"
    if kind in ("intraday", "quote"): return "DELAYED"
    return "END-OF-DAY"

def record_request(provider, kind, symbols, bars_by_symbol, now, interval="1d", delay=0, store=True):
    """Computes completeness/staleness for one retrieval and stores a row in data_requests. Returns the dict."""
    got = [s for s in symbols if s in bars_by_symbol and bars_by_symbol[s] is not None and len(bars_by_symbol[s]) > 0]
    bad = [s for s in symbols if not TICKER.match(str(s))]
    ex_ts = None
    for s in got:
        idx = bars_by_symbol[s].index[-1]
        t = pd.Timestamp(idx); t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        if ex_ts is None or t > ex_ts: ex_ts = t
    stale = None
    if ex_ts is not None: stale = round((pd.Timestamp(now).tz_convert("UTC") - ex_ts).total_seconds() / 60, 1) if pd.Timestamp(now).tzinfo else None
    name = getattr(provider, "name", "yfinance"); lab = label_for(name, "intraday" if kind == "intraday" else "daily")
    rec = {"ts": now.isoformat(timespec="seconds"), "provider": name, "kind": kind, "n_symbols": len(symbols), "retrieved_ts": now.isoformat(timespec="seconds"), "exchange_ts": ex_ts.isoformat() if ex_ts is not None else None,
           "completeness": round(len(got) / len(symbols), 4) if symbols else None, "symbols_valid": len(symbols) - len(bad), "symbols_invalid": json.dumps(bad), "staleness": f"{stale} min since newest bar start" if stale is not None else None,
           "ok": int(bool(got)), "label": lab, "detail": f"interval={interval}, assumed feed delay={delay} min; missing={[s for s in symbols if s not in got][:10]}"}
    if store:
        c = db.conn(); c.execute("INSERT INTO data_requests(ts,provider,kind,n_symbols,retrieved_ts,exchange_ts,completeness,symbols_valid,symbols_invalid,staleness,ok,label,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 tuple(rec[k] for k in ("ts", "provider", "kind", "n_symbols", "retrieved_ts", "exchange_ts", "completeness", "symbols_valid", "symbols_invalid", "staleness", "ok", "label", "detail")))
        c.execute("DELETE FROM data_requests WHERE id NOT IN (SELECT id FROM data_requests ORDER BY id DESC LIMIT 500)"); c.commit(); c.close()
    return rec

def recent(limit=15):
    c = db.conn(); r = [dict(x) for x in c.execute("SELECT * FROM data_requests ORDER BY id DESC LIMIT ?", (limit,))]; c.close(); return r

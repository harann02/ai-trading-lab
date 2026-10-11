"""Automatic US universe discovery (no hardcoded tickers).
Sources (free): S&P 500 + Nasdaq-100 constituent tables from Wikipedia (cached 7 days in DB; stale cache used with a flag if the fetch fails),
exchange/ETF/test-issue flags from the NASDAQ Trader symbol directory. Then liquidity/price/volatility/history filters on real daily bars.
OTC/penny/ETF/test issues are excluded. Survivorship note: constituents are CURRENT members."""
import io, json, re
from datetime import datetime, timezone, timedelta
import numpy as np, pandas as pd, requests
from . import db

UA = {"User-Agent": "Mozilla/5.0 (ai-trading-lab research; contact: github.com/harann02/ai-trading-lab)"}
WIKI_SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_NDX = "https://en.wikipedia.org/wiki/Nasdaq-100"
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
EXCH = {"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca", "Z": "Cboe BZX", "V": "IEX"}
TTL_DAYS = 7

def yf_symbol(s: str) -> str: return s.strip().upper().replace(".", "-")

def _cache_get(key):
    c = db.conn(); r = c.execute("SELECT fetched_at,payload FROM universe_cache WHERE key=?", (key,)).fetchone(); c.close()
    return (datetime.fromisoformat(r["fetched_at"]), json.loads(r["payload"])) if r else (None, None)

def _cache_put(key, payload, now):
    c = db.conn(); c.execute("INSERT OR REPLACE INTO universe_cache VALUES(?,?,?)", (key, now.isoformat(timespec="seconds"), json.dumps(payload))); c.commit(); c.close()

def _cached(key, fetch, now, flags):
    ts, payload = _cache_get(key)
    if payload is not None and (now - ts) < timedelta(days=TTL_DAYS): flags[key] = f"cache {ts.date()}"; return payload
    try:
        payload = fetch(); _cache_put(key, payload, now); flags[key] = "fresh"; return payload
    except Exception as e:
        if payload is not None: flags[key] = f"STALE cache {ts.date()} (fetch failed: {str(e)[:80]})"; return payload
        flags[key] = f"UNAVAILABLE ({str(e)[:80]})"; return None

def _read_html(url, match):
    r = requests.get(url, headers=UA, timeout=30); r.raise_for_status()
    return pd.read_html(io.StringIO(r.text), match=match)

def fetch_sp500():
    t = _read_html(WIKI_SP500, "Symbol")[0]
    return [{"symbol": yf_symbol(r["Symbol"]), "name": r.get("Security")} for _, r in t.iterrows()]

NDX_API = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
WIKI_NDX2 = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"

def fetch_ndx():
    """Primary: Nasdaq's own public JSON list (free). Fallback: Wikipedia components table."""
    try:
        r = requests.get(NDX_API, headers={**UA, "Accept": "application/json"}, timeout=30); r.raise_for_status()
        rows = r.json()["data"]["data"]["rows"]
        if len(rows) >= 90: return [{"symbol": yf_symbol(x["symbol"]), "name": x.get("companyName")} for x in rows]
    except Exception: pass
    for t in _read_html(WIKI_NDX2, "Ticker"):
        if "Ticker" in t.columns and len(t) > 80:
            nm = "Company" if "Company" in t.columns else t.columns[0]
            return [{"symbol": yf_symbol(r["Ticker"]), "name": r.get(nm)} for _, r in t.iterrows()]
    raise RuntimeError("Nasdaq-100 constituents unavailable")

def fetch_exchanges():
    out = {}
    for url, kind in ((NASDAQ_LISTED, "nasdaq"), (OTHER_LISTED, "other")):
        txt = requests.get(url, headers=UA, timeout=30).text
        df = pd.read_csv(io.StringIO(txt), sep="|", dtype=str, keep_default_na=False)
        df = df[(df["Security Name"] != "") & ~df.iloc[:, 0].str.startswith("File Creation")]
        for _, r in df.iterrows():
            if kind == "nasdaq": s, ex = r["Symbol"], "NASDAQ"; etf = r.get("ETF"); test = r.get("Test Issue")
            else: s, ex = r["ACT Symbol"], EXCH.get(r["Exchange"], r["Exchange"]); etf = r.get("ETF"); test = r.get("Test Issue")
            out[yf_symbol(s)] = {"exchange": ex, "etf": etf == "Y", "test": test == "Y", "name": r["Security Name"]}
    return out

def constituents(now=None):
    """Returns (members: {sym: {name, sources[]}}, exchanges: {sym:{..}}, flags)."""
    now = now or datetime.now(timezone.utc); flags = {}
    sp = _cached("sp500", fetch_sp500, now, flags); nd = _cached("ndx100", fetch_ndx, now, flags); ex = _cached("exchanges", fetch_exchanges, now, flags)
    mem = {}
    for src, lst in (("S&P500", sp), ("NASDAQ-100", nd)):
        for r in lst or []:
            m = mem.setdefault(r["symbol"], {"name": r.get("name"), "sources": []}); m["sources"].append(src)
    return mem, ex or {}, flags

def apply_filters(sym, df, info, flt):
    """Pure function: returns (passed, reasons[], metrics). df = daily OHLCV (final bars)."""
    reasons = []; m = {}
    if info is None: reasons.append("exchange unknown (not in NASDAQ Trader directory)")
    else:
        if info["exchange"] not in flt["exchanges"]: reasons.append(f"exchange {info['exchange']} not in {flt['exchanges']}")
        if info.get("etf"): reasons.append("ETF excluded from stock universe")
        if info.get("test"): reasons.append("test issue")
    if df is None or len(df) == 0: return False, reasons + ["no price history"], m
    close, vol = df["close"], df["volume"]
    m["price"] = float(close.iloc[-1]); m["bars"] = int(len(df))
    m["avg_volume_50"] = float(vol.tail(50).mean()); m["adv_usd_20"] = float((close * vol).tail(20).mean())
    lr = np.log(close).diff().dropna().tail(60); m["vol_ann_60"] = float(lr.std() * np.sqrt(252)) if len(lr) > 20 else None
    if m["price"] < flt["min_price"]: reasons.append(f"price ${m['price']:.2f} < ${flt['min_price']:.0f} (penny/low-price excluded)")
    if m["avg_volume_50"] < flt["min_avg_volume"]: reasons.append(f"avg volume {m['avg_volume_50']:,.0f} < {flt['min_avg_volume']:,.0f}")
    if m["adv_usd_20"] < flt["min_adv_usd"]: reasons.append(f"dollar volume ${m['adv_usd_20']:,.0f} < ${flt['min_adv_usd']:,.0f}")
    if m["bars"] < flt["min_history"]: reasons.append(f"history {m['bars']} bars < {flt['min_history']}")
    if m["vol_ann_60"] is None: reasons.append("volatility not computable")
    elif not (flt["min_vol_ann"] <= m["vol_ann_60"] <= flt["max_vol_ann"]): reasons.append(f"volatility sanity: {m['vol_ann_60']*100:.0f}% outside [{flt['min_vol_ann']*100:.0f}%,{flt['max_vol_ann']*100:.0f}%]")
    return (not reasons), reasons, m

def candidates(cfg, now=None):
    """Symbols to download: constituents ∪ extra watchlist (extras must still pass filters), minus blocked."""
    mem, ex, flags = constituents(now)
    for s in cfg.get("extra_watchlist", []):
        mem.setdefault(yf_symbol(s), {"name": s, "sources": ["owner watchlist"]})
    blocked = set(cfg.get("blocked_symbols", []))
    return {s: v for s, v in mem.items() if s not in blocked}, ex, flags

def evaluate(cfg, bars_loader, now=None):
    """Runs filters over all candidates using stored bars; persists universe_members. Returns (passed_symbols, table, flags)."""
    now = now or datetime.now(timezone.utc); mem, ex, flags = candidates(cfg, now); flt = cfg["universe_filters"]; table = []; passed = []
    c = db.conn(); c.execute("DELETE FROM universe_members")
    for s, v in sorted(mem.items()):
        df = bars_loader(s); ok, why, met = apply_filters(s, df, ex.get(s), flt)
        row = (s, v.get("name"), (ex.get(s) or {}).get("exchange"), ",".join(v["sources"]), int(ok), json.dumps(why), json.dumps(met), now.isoformat(timespec="seconds"))
        c.execute("INSERT INTO universe_members VALUES(?,?,?,?,?,?,?,?)", row); table.append(row)
        if ok: passed.append(s)
    c.commit(); c.close()
    return passed, table, flags

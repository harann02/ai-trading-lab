"""Market data: yfinance provider (free; EOD/delayed - never real-time), bar store, freshness checks, fundamentals, DEMO generator."""
import json, math
from datetime import datetime, timezone, timedelta
import numpy as np, pandas as pd
from . import db, calendars as cal

FX_SYMBOL = "USDMYR=X"
MIN_BARS = 210
DATA_LABEL = "yfinance (Yahoo Finance, free): daily bars = EOD; intraday-updated bar = DELAYED (typically >=15 min). NOT real-time."

def _norm(df):
    df = df.copy(); df.columns = [str(c).lower() for c in df.columns]
    df = df[[c for c in ("open", "high", "low", "close", "volume") if c in df.columns]]
    df = df.dropna(subset=["open", "high", "low", "close"])
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df[~df.index.duplicated(keep="last")]

class YFProvider:
    mode = "LIVE"
    def daily(self, symbols, period="2y"):
        import yfinance as yf
        out = {}
        raw = yf.download(list(symbols), period=period, interval="1d", auto_adjust=False, group_by="ticker",
                          progress=False, threads=True)
        for s in symbols:
            try:
                d = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
                d = d.dropna(how="all")
                if len(d): out[s] = _norm(d)
            except Exception:
                pass
        return out
    def info(self, symbol):
        import yfinance as yf
        return yf.Ticker(symbol).info or {}

class DemoProvider:
    """Synthetic random-walk data. EVERYTHING it returns is fake and must be labeled DEMO. Trading is blocked on it."""
    mode = "DEMO"
    def daily(self, symbols, period="2y"):
        out = {}
        for s in symbols:
            rng = np.random.default_rng(abs(hash(s)) % (2**32) if False else sum(map(ord, s)))
            idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=520)
            r = rng.normal(0.0004, 0.015, len(idx)); c = 50 * np.exp(np.cumsum(r))
            o = c * (1 + rng.normal(0, 0.003, len(idx)))
            h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.006, len(idx)))); l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.006, len(idx))))
            out[s] = pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": rng.integers(1e6, 5e6, len(idx)).astype(float)}, index=idx)
        return out
    def info(self, symbol): return {}

def save_bars(bars: dict, now=None):
    now = now or datetime.now(timezone.utc); c = db.conn(); n = 0
    for sym, df in bars.items():
        m = cal.market_of(sym) if sym != FX_SYMBOL else "US"
        lcs = cal.last_completed_session(m, now) if sym != FX_SYMBOL else (now.date() - timedelta(days=0))
        rows = []
        for d, r in df.iterrows():
            final = 1 if (d.date() <= lcs) else 0
            rows.append((sym, d.strftime("%Y-%m-%d"), float(r.open), float(r.high), float(r.low), float(r.close),
                         float(r.get("volume", 0) or 0), final, now.isoformat(timespec="seconds")))
        c.executemany("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET open=excluded.open,high=excluded.high,"
                      "low=excluded.low,close=excluded.close,volume=excluded.volume,final=excluded.final,fetched_at=excluded.fetched_at", rows)
        n += len(rows)
    c.commit(); c.close(); return n

def load_bars(symbol, include_partial=False, end=None) -> pd.DataFrame:
    c = db.conn()
    q = "SELECT date,open,high,low,close,volume,final FROM bars WHERE symbol=?" + ("" if include_partial else " AND final=1")
    p = [symbol]
    if end: q += " AND date<=?"; p.append(str(end))
    rows = c.execute(q + " ORDER BY date", p).fetchall(); c.close()
    if not rows: return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "final"])
    df = pd.DataFrame([dict(r) for r in rows]); df.index = pd.to_datetime(df.pop("date"))
    return df

def data_status(symbol, now=None, df=None) -> dict:
    """Flags stale/missing/insufficient/anomalous data. Anything but status OK blocks trading in that symbol."""
    now = now or datetime.now(timezone.utc)
    m = cal.market_of(symbol); df = load_bars(symbol) if df is None else df
    base = {"symbol": symbol, "source": "yfinance", "latency": "EOD (final daily bar)", "ok": False}
    if df is None or len(df) == 0: return {**base, "status": "MISSING", "reason": "no bars available"}
    last = df.index[-1].date(); exp = cal.last_completed_session(m, now)
    base.update(last_bar=str(last), expected_bar=str(exp), bars=len(df))
    if last < exp:
        lag = len(cal.schedule(m, last, exp)) - 1
        return {**base, "status": "STALE", "reason": f"last bar {last} older than last completed session {exp} ({lag} sessions behind)"}
    if len(df) < MIN_BARS: return {**base, "status": "INSUFFICIENT", "reason": f"only {len(df)} bars (<{MIN_BARS})"}
    r = df.iloc[-1]
    if not (r.close > 0 and r.open > 0 and r.high >= r.low) or df["close"].tail(260).isna().any():
        return {**base, "status": "ANOMALY", "reason": "non-positive or inconsistent OHLC"}
    ret = df["close"].pct_change().abs().tail(5)
    if (ret > 0.5).any(): return {**base, "status": "ANOMALY", "reason": "daily move >50% in last 5 bars (possible bad data/split)"}
    return {**base, "status": "OK", "ok": True, "reason": "fresh"}

def get_fx(now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    df = load_bars(FX_SYMBOL, include_partial=True)
    if len(df) == 0: return {"rate": None, "status": "MISSING", "date": None, "ok": False}
    age = (now.date() - df.index[-1].date()).days
    ok = age <= 5
    return {"rate": float(df["close"].iloc[-1]), "date": str(df.index[-1].date()), "status": "OK" if ok else "STALE", "ok": ok,
            "pair": "USD/MYR", "source": "yfinance USDMYR=X (delayed/EOD)"}

def fx_on(date) -> float | None:
    df = load_bars(FX_SYMBOL, include_partial=True, end=str(date)[:10])
    if len(df) == 0: return None
    return float(df["close"].iloc[-1])

def parse_info(info: dict) -> dict:
    g = lambda k: (None if info.get(k) in (None, "Infinity") or (isinstance(info.get(k), float) and math.isnan(info.get(k))) else info.get(k))
    fcf, mc = g("freeCashflow"), g("marketCap")
    n_an = g("numberOfAnalystOpinions") or 0
    return {"roe": g("returnOnEquity"), "de": g("debtToEquity"), "profit_margin": g("profitMargins"),
            "revenue_growth": g("revenueGrowth"), "earnings_growth": g("earningsQuarterlyGrowth") if g("earningsQuarterlyGrowth") is not None else g("earningsGrowth"),
            "pe": g("trailingPE"), "pb": g("priceToBook"), "fcf_yield": (fcf / mc) if fcf is not None and mc else None,
            "eps": g("trailingEps"), "div_yield": g("dividendYield"), "current_ratio": g("currentRatio"),
            "inst_pct": g("heldPercentInstitutions"),
            "analyst_target": g("targetMeanPrice") if n_an >= 3 else None, "analysts": n_an,
            "name": g("shortName") or g("longName")}

def pit_snapshot(c, symbol, data):
    """Forward-accumulating POINT-IN-TIME fundamentals archive (append-only): each fetched record is kept with its retrieval timestamp so future backtests can use only what was known at each date.
    Free sources have no historical point-in-time fundamentals; this table starts accumulating now and can NOT be back-filled."""
    c.execute("CREATE TABLE IF NOT EXISTS fundamentals_pit(symbol TEXT NOT NULL, fetched_at TEXT NOT NULL, data TEXT NOT NULL, source TEXT DEFAULT 'yfinance .info (current values at fetch time)', PRIMARY KEY(symbol, fetched_at))")
    c.execute("CREATE TRIGGER IF NOT EXISTS fpit_no_update BEFORE UPDATE ON fundamentals_pit BEGIN SELECT RAISE(ABORT,'fundamentals_pit is append-only'); END")
    c.execute("CREATE TRIGGER IF NOT EXISTS fpit_no_delete BEFORE DELETE ON fundamentals_pit BEGIN SELECT RAISE(ABORT,'fundamentals_pit is append-only'); END")
    c.execute("INSERT OR IGNORE INTO fundamentals_pit(symbol,fetched_at,data) VALUES(?,?,?)", (symbol, data["fetched_at"], json.dumps(data)))

def get_fundamentals(symbol, provider, now=None, ttl_days=7, fetch=True) -> dict | None:
    now = now or datetime.now(timezone.utc)
    c = db.conn(); r = c.execute("SELECT fetched_at,data FROM fundamentals WHERE symbol=?", (symbol,)).fetchone(); c.close()
    if r and (now - datetime.fromisoformat(r["fetched_at"])).days < ttl_days:
        return json.loads(r["data"])
    if not fetch: return json.loads(r["data"]) if r else None
    try:
        data = parse_info(provider.info(symbol)); data["fetched_at"] = now.isoformat(timespec="seconds")
        if all(data.get(k) is None for k in ("roe", "pe", "profit_margin", "revenue_growth")) and r: return json.loads(r["data"])
        c = db.conn(); c.execute("INSERT INTO fundamentals VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET fetched_at=excluded.fetched_at,data=excluded.data",
                                 (symbol, data["fetched_at"], json.dumps(data)))
        pit_snapshot(c, symbol, data); c.commit(); c.close()
        return data
    except Exception as e:
        return json.loads(r["data"]) if r else None

def set_source(name, ok, detail=""):
    c = db.conn(); c.execute("INSERT INTO source_status VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET ts=excluded.ts,ok=excluded.ok,detail=excluded.detail",
                             (name, datetime.now(timezone.utc).isoformat(timespec="seconds"), int(ok), detail)); c.commit(); c.close()

def refresh(provider, symbols, now=None, period="2y"):
    """Download + store bars. Records source status. Returns (n_symbols_ok, missing list)."""
    now = now or datetime.now(timezone.utc)
    syms = list(dict.fromkeys(list(symbols) + [FX_SYMBOL]))
    try:
        bars = provider.daily(syms, period=period)
    except Exception as e:
        set_source("yfinance", False, f"download failed: {e}"); db.event("ERROR", "data", f"yfinance download failed: {e}")
        return 0, syms
    if provider.mode == "DEMO": return len(bars), [s for s in syms if s not in bars]
    save_bars(bars, now)
    missing = [s for s in syms if s not in bars]
    set_source("yfinance", len(bars) > 0, f"{len(bars)}/{len(syms)} symbols; missing: {missing[:10]}")
    return len(bars), missing

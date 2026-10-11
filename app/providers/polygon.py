"""Polygon.io adapter. ACTIVATED ONLY IF env POLYGON_API_KEY is present.
STATUS: code written against Polygon's documented REST API; UNTESTED here (no key). Free tier = delayed EOD aggregates, ~5 calls/min,
2y history; real-time/quotes/trades need paid plans -> capabilities are PROBED (a 403 'NOT_AUTHORIZED' marks the capability unavailable)."""
import os
import pandas as pd, requests
from .base import *

class PolygonProvider(Provider):
    name = "polygon"; BASE = "https://api.polygon.io"
    def __init__(self, env=None):
        e = env if env is not None else os.environ; self.key = e.get("POLYGON_API_KEY", "")
    def configured(self): return bool(self.key)
    def _get(self, path, params=None):
        p = dict(params or {}); p["apiKey"] = self.key
        r = requests.get(self.BASE + path, params=p, timeout=20); r.raise_for_status(); return r.json()
    def probe(self) -> CapResult:
        if not self.configured(): return CapResult(self.name, False, False, {}, "inactive: POLYGON_API_KEY not set (adapter code UNTESTED)")
        caps = {c: cap(False) for c in ALL_CAPS}
        def t(name, fn, **kw):
            try: fn(); caps[name] = cap(True, **kw)
            except Exception as e: caps[name] = cap(False, note=str(e)[:120])
            caps[name]["tested"] = True
        t(HISTORICAL, lambda: self._get("/v2/aggs/ticker/AAPL/range/1/day/2024-01-02/2024-01-10"), latency="EOD (free tier delayed)")
        caps[EOD] = caps[HISTORICAL]
        t(INTRADAY, lambda: self._get("/v2/aggs/ticker/AAPL/range/5/minute/2024-01-02/2024-01-03"), latency="plan dependent")
        t(REALTIME_QUOTES, lambda: self._get("/v2/last/nbbo/AAPL"), latency="paid plans only")
        caps[BID_ASK] = caps[REALTIME_QUOTES]
        t(TRADES, lambda: self._get("/v2/last/trade/AAPL"), latency="paid plans only")
        t(CORP_ACTIONS, lambda: self._get("/v3/reference/splits", {"limit": 1}))
        t(FUNDAMENTALS, lambda: self._get("/vX/reference/financials", {"limit": 1, "ticker": "AAPL"}))
        t(NEWS, lambda: self._get("/v2/reference/news", {"limit": 1, "ticker": "AAPL"}))
        return CapResult(self.name, True, True, caps, "probed with provided key")
    def daily(self, symbols, period="2y"):
        out = {}; end = pd.Timestamp.utcnow().normalize(); start = end - pd.Timedelta(days=730)
        for s in symbols:
            j = self._get(f"/v2/aggs/ticker/{s}/range/1/day/{start.date()}/{end.date()}", {"adjusted": "true", "limit": 5000})
            r = j.get("results") or []
            if r:
                df = pd.DataFrame(r).rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
                df.index = pd.to_datetime(df["t"], unit="ms").dt.normalize(); out[s] = df[["open", "high", "low", "close", "volume"]]
        return out
    def intraday(self, symbol, interval="5m", period="5d"):
        mult = int(interval[:-1]); end = pd.Timestamp.utcnow().normalize(); start = end - pd.Timedelta(days=int(period.rstrip("d")) + 2)
        r = (self._get(f"/v2/aggs/ticker/{symbol}/range/{mult}/minute/{start.date()}/{end.date()}", {"limit": 50000}).get("results")) or []
        df = pd.DataFrame(r).rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        if len(df): df.index = pd.to_datetime(df.pop("t"), unit="ms"); df = df[["open", "high", "low", "close", "volume"]]
        return df

"""Alpaca Market Data adapter. ACTIVATED ONLY IF env ALPACA_API_KEY_ID + ALPACA_API_SECRET_KEY are present.
STATUS: code written against Alpaca's documented REST v2 data API; UNTESTED (no keys available in this environment).
Free Alpaca tier = IEX-only real-time feed (not full SIP) -> `realtime_quotes`/`bid_ask` are probed, never assumed."""
import os
import pandas as pd, requests
from .base import *

class AlpacaProvider(Provider):
    name = "alpaca"; BASE = "https://data.alpaca.markets"
    def __init__(self, env=None):
        e = env if env is not None else os.environ
        self.key, self.secret = e.get("ALPACA_API_KEY_ID", ""), e.get("ALPACA_API_SECRET_KEY", "")
        self.feed = e.get("ALPACA_DATA_FEED", "iex")
    def configured(self): return bool(self.key and self.secret)
    def _get(self, path, params=None):
        r = requests.get(self.BASE + path, params=params, headers={"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}, timeout=20)
        r.raise_for_status(); return r.json()
    def probe(self) -> CapResult:
        if not self.configured(): return CapResult(self.name, False, False, {}, "inactive: ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY not set (adapter code UNTESTED)")
        caps = {c: cap(False) for c in ALL_CAPS}
        def t(name, fn, **kw):
            try: fn(); caps[name] = cap(True, **kw); caps[name]["tested"] = True
            except Exception as e: caps[name] = cap(False, note=str(e)[:120]); caps[name]["tested"] = True
        t(HISTORICAL, lambda: self._get("/v2/stocks/AAPL/bars", {"timeframe": "1Day", "limit": 5, "feed": self.feed}), latency="EOD/15min delay on free IEX feed")
        caps[EOD] = caps[HISTORICAL]
        t(INTRADAY, lambda: self._get("/v2/stocks/AAPL/bars", {"timeframe": "5Min", "limit": 5, "feed": self.feed}), latency=f"feed={self.feed}")
        t(REALTIME_QUOTES, lambda: self._get("/v2/stocks/AAPL/quotes/latest", {"feed": self.feed}), latency=f"feed={self.feed} (IEX-only on free tier)")
        caps[BID_ASK] = caps[REALTIME_QUOTES]
        t(TRADES, lambda: self._get("/v2/stocks/AAPL/trades/latest", {"feed": self.feed}), latency=f"feed={self.feed}")
        t(NEWS, lambda: self._get("/v1beta1/news", {"symbols": "AAPL", "limit": 1}))
        t(CORP_ACTIONS, lambda: self._get("/v1/corporate-actions", {"symbols": "AAPL", "limit": 1}))
        return CapResult(self.name, True, True, caps, "probed with provided keys")
    def daily(self, symbols, period="2y"):
        out = {}
        for s in symbols:
            j = self._get(f"/v2/stocks/{s}/bars", {"timeframe": "1Day", "limit": 1000, "adjustment": "split", "feed": self.feed,
                                                    "start": (pd.Timestamp.utcnow() - pd.Timedelta(days=760)).strftime("%Y-%m-%d")})
            b = j.get("bars") or []
            if b:
                df = pd.DataFrame(b).rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "date"})
                df.index = pd.to_datetime(df.pop("date")).dt.tz_localize(None).dt.normalize(); out[s] = df[["open", "high", "low", "close", "volume"]]
        return out
    def intraday(self, symbol, interval="5m", period="5d"):
        tf = {"1m": "1Min", "5m": "5Min", "15m": "15Min"}[interval]
        j = self._get(f"/v2/stocks/{symbol}/bars", {"timeframe": tf, "limit": 10000, "feed": self.feed, "start": (pd.Timestamp.utcnow() - pd.Timedelta(days=int(period.rstrip('d')) + 2)).isoformat()})
        df = pd.DataFrame(j.get("bars") or []).rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "date"})
        if len(df): df.index = pd.to_datetime(df.pop("date")); df = df[["open", "high", "low", "close", "volume"]]
        return df
    def quote(self, symbol):
        q = self._get(f"/v2/stocks/{symbol}/quotes/latest", {"feed": self.feed})["quote"]; return {"bid": q["bp"], "ask": q["ap"], "ts": q["t"]}

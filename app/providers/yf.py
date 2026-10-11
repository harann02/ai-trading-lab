"""yfinance provider (free). DELAYED/EOD only: never labelled or used as real-time. Fully implemented; probed live."""
from datetime import datetime, timezone
import pandas as pd
from .base import *
from .. import data as _data

class YFinanceProvider(_data.YFProvider, Provider):
    name = "yfinance"
    LABEL = "yfinance (Yahoo, free, unofficial): daily bars = EOD; intraday candles are DELAYED (~15 min+) and history-limited (1m ~7d, 5m ~60d). NOT real-time."
    def configured(self): return True
    def declared_caps(self):
        return {HISTORICAL: cap(True, "EOD"), EOD: cap(True, "EOD"), INTRADAY: cap(True, "delayed >=15min; 1m~7d, 5m~60d"), REALTIME_QUOTES: cap(False, note="Yahoo quotes are delayed; not treated as real-time"),
                BID_ASK: cap(False, note="bid/ask in Ticker.info is a delayed snapshot; not usable for execution"), TRADES: cap(False), DEPTH: cap(False),
                CORP_ACTIONS: cap(True, "EOD", "splits/dividends via history actions"), FUNDAMENTALS: cap(True, "unofficial, no point-in-time"),
                EARNINGS: cap(True, "unofficial"), NEWS: cap(True, "headline list with timestamps")}
    def probe(self) -> CapResult:
        caps = self.declared_caps(); note = ""
        try:
            d = self.daily(["SPY"], period="5d"); ok = "SPY" in d and len(d["SPY"]) > 0; caps[HISTORICAL]["tested"] = ok; caps[EOD]["tested"] = ok
            if not ok: caps[HISTORICAL]["available"] = caps[EOD]["available"] = False
        except Exception as e:
            caps[HISTORICAL]["available"] = caps[EOD]["available"] = False; note = f"daily probe failed: {e}"
        try:
            i = self.intraday("SPY", "5m", "1d"); ok = len(i) > 0; caps[INTRADAY]["tested"] = True
            if not ok: caps[INTRADAY]["available"] = False; caps[INTRADAY]["note"] += " (empty now: market closed or no data)"
        except Exception as e: caps[INTRADAY]["available"] = False; note += f" intraday probe failed: {e}"
        return CapResult(self.name, True, True, caps, note or self.LABEL)
    def intraday(self, symbol, interval="5m", period="5d"):
        import yfinance as yf
        df = yf.download(symbol, period=period, interval=interval, auto_adjust=False, progress=False, prepost=False)
        if df is None or len(df) == 0: return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df.columns = [str(c).lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]].dropna(subset=["open", "high", "low", "close"])
        df.index = pd.to_datetime(df.index)
        return df
    def news(self, symbol, limit=8):
        import yfinance as yf
        out = []
        for n in (yf.Ticker(symbol).news or [])[:limit]:
            c = n.get("content") or n
            title = c.get("title") or n.get("title"); ts = c.get("pubDate") or c.get("displayTime") or n.get("providerPublishTime")
            if isinstance(ts, (int, float)): ts = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
            pub = ((c.get("provider") or {}).get("displayName")) or n.get("publisher")
            if title: out.append({"title": title, "published": ts, "publisher": pub, "source": "yfinance news"})
        return out
    def earnings(self, symbol):
        import yfinance as yf
        try:
            cal = yf.Ticker(symbol).calendar
            ed = (cal or {}).get("Earnings Date") if isinstance(cal, dict) else None
            return [str(x) for x in ed] if ed else None
        except Exception: return None

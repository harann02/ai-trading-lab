"""Modular market-data provider interface with capability detection.
A strategy declares the capabilities it REQUIRES; if no ACTIVE provider has them the strategy is auto-disabled (never faked)."""
from dataclasses import dataclass, field

HISTORICAL = "historical"; EOD = "eod"; INTRADAY = "intraday_candles"; REALTIME_QUOTES = "realtime_quotes"; BID_ASK = "bid_ask"
TRADES = "trades"; DEPTH = "depth"; CORP_ACTIONS = "corporate_actions"; FUNDAMENTALS = "fundamentals"; EARNINGS = "earnings_calendar"; NEWS = "news"
ALL_CAPS = [HISTORICAL, EOD, INTRADAY, REALTIME_QUOTES, BID_ASK, TRADES, DEPTH, CORP_ACTIONS, FUNDAMENTALS, EARNINGS, NEWS]

@dataclass
class CapResult:
    provider: str; active: bool; tested: bool; caps: dict = field(default_factory=dict); note: str = ""
    def has(self, cap): return bool(self.active and self.caps.get(cap, {}).get("available"))
    def to_dict(self): return {"provider": self.provider, "active": self.active, "tested": self.tested, "caps": self.caps, "note": self.note}

def cap(available, latency=None, note=""): return {"available": bool(available), "latency": latency, "note": note}

class Provider:
    """Interface. `daily`/`info` keep the signature used by the v1 engine (so existing code and tests keep working)."""
    name = "base"; mode = "LIVE"
    def configured(self) -> bool: return True
    def declared_caps(self) -> dict: return {}
    def probe(self) -> CapResult:
        """Capabilities detected by actually calling the provider (network). Providers without keys return active=False, tested=False."""
        raise NotImplementedError
    def daily(self, symbols, period="2y"): raise NotImplementedError
    def info(self, symbol): return {}
    def intraday(self, symbol, interval="5m", period="5d"): raise NotImplementedError(f"{self.name}: intraday not supported")
    def news(self, symbol, limit=8): return []
    def earnings(self, symbol): return None

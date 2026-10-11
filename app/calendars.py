"""Exchange calendars: Bursa (XKLS) and NYSE. Holidays from pandas_market_calendars, unioned
(conservatively) with Malaysian public holidays from the `holidays` lib for Bursa: if either says
closed, we treat Bursa as closed (blocks trading; never opens on a possible holiday)."""
from datetime import datetime, date, timedelta, timezone
from functools import lru_cache
import pandas as pd
import pandas_market_calendars as pmc
import holidays as _hol
from zoneinfo import ZoneInfo

MARKETS = {
    "BURSA": {"cal": "XKLS", "tz": "Asia/Kuala_Lumpur", "ccy": "MYR", "name": "Bursa Malaysia"},
    "US": {"cal": "NYSE", "tz": "America/New_York", "ccy": "USD", "name": "NYSE/Nasdaq"},
}

def market_of(symbol: str) -> str:
    return "BURSA" if symbol.upper().endswith(".KL") or symbol == "^KLSE" else "US"

@lru_cache(maxsize=4)
def _cal(name): return pmc.get_calendar(name)

@lru_cache(maxsize=8)
def _my_hol(year): return _hol.country_holidays("MY", years=range(year - 1, year + 2))

def tz(market): return ZoneInfo(MARKETS[market]["tz"])

def local_now(market, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None: now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(tz(market))

def schedule(market, start: date, end: date) -> pd.DataFrame:
    sch = _cal(MARKETS[market]["cal"]).schedule(start_date=start, end_date=end)
    if market == "BURSA" and len(sch):
        hol = _my_hol(end.year)
        sch = sch[[d.date() not in hol for d in sch.index]]
    return sch

def sessions_around(market, d: date):
    return schedule(market, d - timedelta(days=12), d + timedelta(days=12))

def is_session_day(market, d: date) -> bool:
    s = schedule(market, d, d)
    return len(s) > 0

def session_hours(market, d: date):
    s = schedule(market, d, d)
    if not len(s): return None
    return s.iloc[0]["market_open"].to_pydatetime(), s.iloc[0]["market_close"].to_pydatetime()

def is_open(market, now=None) -> bool:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None: now = now.replace(tzinfo=timezone.utc)
    d = local_now(market, now).date()
    h = session_hours(market, d)
    return bool(h and h[0] <= now <= h[1])

def last_completed_session(market, now=None) -> date:
    """Most recent session date whose close is <= now."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None: now = now.replace(tzinfo=timezone.utc)
    d = local_now(market, now).date()
    s = schedule(market, d - timedelta(days=14), d + timedelta(days=1))
    done = s[s["market_close"] <= pd.Timestamp(now)]
    return done.index[-1].date()

def current_or_next_session(market, now=None):
    now = now or datetime.now(timezone.utc)
    d = local_now(market, now).date()
    s = schedule(market, d, d + timedelta(days=14))
    for idx, r in s.iterrows():
        if r["market_close"] > pd.Timestamp(now): return idx.date(), r["market_open"].to_pydatetime(), r["market_close"].to_pydatetime()
    return None

def market_status(market, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    nxt = current_or_next_session(market, now)
    return {"market": market, "open": is_open(market, now), "tz": MARKETS[market]["tz"],
            "local_time": local_now(market, now).strftime("%Y-%m-%d %H:%M"),
            "next_session": str(nxt[0]) if nxt else None,
            "next_open_utc": nxt[1].isoformat() if nxt else None,
            "last_completed_session": str(last_completed_session(market, now))}

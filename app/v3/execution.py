"""v3 execution integrity: DELAYED-EXECUTION fill methodology for FORWARD PAPER orders (model id fwd-paper-delayed-v1).

PRINCIPLE: a forward paper fill may only use a price that was OBSERVABLE to the simulator at the moment it acted. The free feed (yfinance) delivers intraday
candles ~15 min late and daily bars are only final after the close, so an order created from an end-of-day signal can NEVER be filled at "tomorrow's open" read back
from a daily bar (that price was not known when the decision was made / when the simulator ran).  Method:
  1. signal at the close of session D (data_ts = last final daily bar).  order_type = MARKET (simulated), eligible_session = first NYSE session whose OPEN is after
     the order was created (never the session already underway).
  2. the order is not eligible before that session's open (status stays pending, reason 'not eligible until ...').
  3. during the eligible session each monitor run fetches real intraday candles. A candle becomes AVAILABLE at  bar_end + fill_delay_min  (default 15 min).
     Only candles with available_ts <= now count. If none exists, or the newest is older than max_price_age_min  -> status stays 'awaiting-data' (no fill).
  4. fill reference price = CLOSE of the newest available candle (the last print the simulator could actually see), then modelled half-spread + slippage + fees + FX.
     exec_ts = the simulator's action time (now); price_available_ts = when that price became visible; observed_ts = retrieval time. Never earlier than the session open.
  5. if the eligible session ends with no observable window the order lapses (next-opportunity: the next scan creates a new order from fresh data). No retroactive fill.
STOPS: resting stop-market orders are simulated from candles AVAILABLE at run time. A trigger needs candle.low <= stop. Fill = stop price, or the candle OPEN when the candle
opened below the stop (gap), plus extra stop slippage. NOT guaranteed: halts, missing candles (-> awaiting-data), overnight gaps, and unexplained jumps (possible corporate
action -> fills held for review) are not hidden. Historical BACKTEST fills use a different model (bt-next-open-v1) and are labelled HISTORICAL_BACKTEST."""
from datetime import datetime, timezone, timedelta, date
import pandas as pd
from .. import db, ledger, calendars as cal
from . import FILL_MODEL_FORWARD

_INT = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "60m": 60}

def _utc(ts):
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")

def normalize_bars(df, interval="5m"):
    """Intraday candles indexed by UTC bar START. Drops rows with non-positive or inconsistent OHLC (never repaired or invented)."""
    if df is None or len(df) == 0: return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    d = df.copy(); d.index = [_utc(i) for i in d.index]
    d = d[(d["open"] > 0) & (d["close"] > 0) & (d["high"] >= d["low"])]
    return d[~d.index.duplicated(keep="last")].sort_index()

def available_ts(bar_start, interval="5m", delay_min=15):
    return _utc(bar_start) + pd.Timedelta(minutes=_INT.get(interval, 5) + delay_min)

def available_bars(df, now, interval="5m", delay_min=15):
    """Only candles whose availability time has passed. This is THE guard against look-ahead."""
    d = normalize_bars(df, interval)
    if len(d) == 0: return d
    avail = d.index + pd.Timedelta(minutes=_INT.get(interval, 5) + delay_min)
    n = pd.Timestamp(now); n = n.tz_localize("UTC") if n.tzinfo is None else n.tz_convert("UTC")
    return d[avail <= n]

def eligible_session_for(created: datetime):
    """First NYSE session whose regular OPEN is strictly after `created` -> (date, open_utc, close_utc)."""
    created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    d0 = created.astimezone(cal.NY).date()
    sch = cal.schedule("US", d0, d0 + timedelta(days=14))
    for idx, r in sch.iterrows():
        o = r["market_open"].to_pydatetime()
        if o > created: return idx.date(), o, r["market_close"].to_pydatetime()
    return None

def stamp_new_order(oid, *, created, signal_ts, data_ts, data_source, order_type="MARKET", fill_basis=None, immediate=False):
    """Writes the order-record fields at creation (signal/data timestamps, eligible session, model version)."""
    es = eligible_session_for(created)
    c = db.conn()
    c.execute("UPDATE orders SET signal_ts=?, data_ts=?, order_type=?, eligible_session=?, eligible_ts=?, data_source=?, fill_model_version=?, fill_kind=?, fill_basis=? WHERE id=?",
              (signal_ts, data_ts, order_type, str(es[0]) if es else None, (es[1].isoformat() if (es and not immediate) else created.isoformat()), data_source, FILL_MODEL_FORWARD, "FORWARD_PAPER", fill_basis, oid))
    c.commit(); c.close()

def decide_forward_fill(order, bars, now, cfg, daily_ref=None):
    """Pure decision (no DB writes). `bars` = raw intraday candles (DataFrame) or None. Returns dict(status, reason, ...).
    status: 'fill' | 'not_eligible' | 'awaiting_data' | 'lapsed' | 'review'."""
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    interval = cfg.get("fill_bar_interval", "5m"); delay = float(cfg.get("fill_delay_min", 15)); max_age = float(cfg.get("max_price_age_min", 45))
    created = datetime.fromisoformat(order["created_ts"]); es = eligible_session_for(created)
    if order.get("eligible_session"):
        d = date.fromisoformat(order["eligible_session"]); h = cal.session_hours("US", d)
        es = (d, h[0], h[1]) if h else es
    if not es: return {"status": "awaiting_data", "reason": "no upcoming NYSE session found"}
    sdate, s_open, s_close = es
    if now < s_open: return {"status": "not_eligible", "reason": f"not eligible until the {sdate} session opens ({s_open.isoformat(timespec='minutes')} UTC)", "eligible_session": str(sdate)}
    if now > s_close + timedelta(minutes=delay + 5 + 5):
        return {"status": "lapsed", "reason": f"eligible session {sdate} ended without an observable fill window (awaiting-data); no retroactive fill - next-opportunity on the next scan", "eligible_session": str(sdate)}
    if bars is None or len(bars) == 0:
        return {"status": "awaiting_data", "reason": "no intraday candles retrieved (feed empty/unavailable): no fill without an observable price", "eligible_session": str(sdate)}
    av = available_bars(bars, now, interval, delay)
    av = av[(av.index >= pd.Timestamp(s_open)) & (av.index < pd.Timestamp(s_close))]
    if len(av) == 0: return {"status": "awaiting_data", "reason": f"no candle of the {sdate} session is available yet (candles become visible {delay:g} min after they end)", "eligible_session": str(sdate)}
    last = av.iloc[-1]; bs = av.index[-1]; avail = available_ts(bs, interval, delay); age_min = (pd.Timestamp(now) - (bs + pd.Timedelta(minutes=_INT.get(interval, 5)))).total_seconds() / 60
    if age_min > delay + max_age:
        return {"status": "awaiting_data", "reason": f"newest available candle is stale ({age_min:.0f} min after its end; limit {delay + max_age:.0f}): stale feed blocks fills", "eligible_session": str(sdate)}
    px = float(last["close"])
    if daily_ref and daily_ref > 0 and abs(px / daily_ref - 1) * 100 > float(cfg.get("corp_action_jump_pct", 35.0)):
        return {"status": "review", "reason": f"price {px:.2f} is {abs(px/daily_ref-1)*100:.0f}% away from the last daily close {daily_ref:.2f}: possible split/corporate action or bad data - fill held for review", "eligible_session": str(sdate)}
    first = av.index[0] == bs
    return {"status": "fill", "price": px, "bar_start": bs.isoformat(), "price_available_ts": avail.isoformat(), "observed_ts": now.isoformat(timespec="seconds"),
            "first_available_bar": bool(first), "fill_basis": "close of the newest candle available at run time (delayed feed)", "eligible_session": str(sdate), "session_open": s_open.isoformat(),
            "reason": "price observable at simulator action time"}

def evaluate_stop(stop, bars, entry_ts, now, cfg):
    """Stop-market simulation from AVAILABLE candles after entry. Returns dict or None (no trigger / nothing observable)."""
    interval = cfg.get("fill_bar_interval", "5m"); delay = float(cfg.get("fill_delay_min", 15))
    av = available_bars(bars, now, interval, delay)
    if len(av) == 0: return None
    av = av[av.index >= _utc(entry_ts)]
    hit = av[av["low"] <= stop]
    if len(hit) == 0: return None
    b = hit.iloc[0]; bs = hit.index[0]; gap = bool(b["open"] < stop)
    px = float(b["open"]) if gap else float(stop)
    px *= (1 - float(cfg.get("stop_extra_slippage_bps", 5.0)) / 1e4)
    return {"trigger_bar": bs.isoformat(), "gap": gap, "ref_price": px, "stop": float(stop), "price_available_ts": available_ts(bs, interval, delay).isoformat(),
            "note": ("GAP through stop: filled at the candle open, worse than the stop" if gap else "stop-market: filled at stop + extra slippage (NOT guaranteed)")}

def record_fill_fields(oid, ex, fl, now, *, spread_bps=None):
    """Persists the execution evidence on the order record after a ledger fill."""
    c = db.conn()
    c.execute("UPDATE orders SET exec_ts=?, exec_price=?, price_available_ts=?, observed_ts=?, spread_bps=?, slippage_usd=?, fees_usd=?, fx_rate=?, fill_basis=?, fill_model_version=?, fill_kind='FORWARD_PAPER' WHERE id=?",
              (ledger.iso(now), fl["price"], ex.get("price_available_ts"), ex.get("observed_ts"), spread_bps, fl.get("slip_usd"), fl.get("fees_usd"), fl.get("fx"), ex.get("fill_basis"), FILL_MODEL_FORWARD, oid))
    c.commit(); c.close()

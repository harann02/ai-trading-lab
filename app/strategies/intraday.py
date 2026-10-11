"""Strategy B - Intraday / scalping (VWAP, opening-range breakout, relative volume). **PRICE+VOLUME PROXY ONLY.**
This engine never sees order flow, bid/ask, trades or depth. Its VWAP is computed from (delayed) OHLCV candles = a PROXY for true VWAP, and it is labelled so everywhere.
It is DISABLED unless a provider with genuine intraday candles AND real-time quotes is detected AND there is enough intraday history; live execution additionally needs REALTIME_QUOTES.
With yfinance only (delayed): research/backtest on REAL downloaded candles is allowed; live execution is not. Never backtest on fabricated candles (loader refuses synthetic data)."""
import math
import numpy as np, pandas as pd
from ..providers import base as B

SID = "intraday"; VERSION = "intraday-2.0.0-proxy"; NAME = "Intraday / scalping (proxy research)"
REQUIRES_LIVE = [B.INTRADAY, B.REALTIME_QUOTES]; REQUIRES_RESEARCH = [B.INTRADAY]
MIN_HISTORY_DAYS = 20
PARAMS = {"or_minutes": 30, "rv_min": 1.5, "stop_atr_mult": 1.0, "rr": 1.5, "daily_loss_breaker_pct": 2.0, "max_spread_bps": 8.0, "bar_minutes": 5}
LABEL = "PROXY: VWAP/RVOL from OHLCV candles only. No order flow, bid/ask, trades or depth is available or inferred."

def status(cfg, cap_results, history_days=None):
    """Returns dict(live_enabled, research_enabled, reasons[]). Live needs real-time quotes + intraday + history + owner switch."""
    reasons = []
    has = lambda cap: any(r.has(cap) for r in cap_results)
    research = has(B.INTRADAY)
    if not research: reasons.append("no provider with intraday candles")
    live = research and has(B.REALTIME_QUOTES)
    if not has(B.REALTIME_QUOTES): reasons.append("no provider with REAL-TIME quotes (yfinance is delayed; Alpaca/Polygon keys absent or plan lacks it)")
    if history_days is not None and history_days < MIN_HISTORY_DAYS: live = False; reasons.append(f"intraday history {history_days}d < {MIN_HISTORY_DAYS}d")
    if not cfg["strategies"]["intraday"]["enabled"]: live = False; reasons.append("owner switch OFF")
    if cfg["strategies"]["intraday"]["allocation_pct"] <= 0: live = False; reasons.append("allocation 0%")
    return {"live_enabled": bool(live), "research_enabled": bool(research), "reasons": reasons, "label": LABEL}

def vwap_proxy(day: pd.DataFrame) -> pd.Series:
    tp = (day["high"] + day["low"] + day["close"]) / 3
    return (tp * day["volume"]).cumsum() / day["volume"].cumsum().replace(0, np.nan)

def validate_candles(df: pd.DataFrame):
    """Refuses fabricated/invalid candles: OHLC consistency, positive prices, monotonic unique index, volume >=0."""
    if df is None or len(df) == 0: raise ValueError("no candles")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates: raise ValueError("candles index not monotonic/unique")
    if ((df["high"] < df[["open", "close"]].max(axis=1) - 1e-9) | (df["low"] > df[["open", "close"]].min(axis=1) + 1e-9)).any(): raise ValueError("inconsistent OHLC")
    if (df[["open", "high", "low", "close"]] <= 0).any().any() or (df["volume"] < 0).any(): raise ValueError("non-positive prices/negative volume")
    return True

def backtest(candles: dict, cfg, capital_usd=2000.0, params=None, adv_usd: dict | None = None):
    """Research backtest on REAL intraday candles (dict sym -> OHLCV with tz-aware ET index). Entry at the bar AFTER the signal bar (no look-ahead).
    Rules: after opening range (first `or_minutes`), long if close > OR high AND close > VWAP-proxy AND relative volume >= rv_min (volume vs mean of same-clock bars in prior days).
    Stop = entry - stop_atr*ATR(bar, 14); target = rr * risk. Forced liquidation at last bar of the session. Daily loss circuit breaker (% of capital) stops new entries.
    Costs: modelled half-spread + slippage both sides, SEC/TAF on sells. One position at a time per symbol."""
    from .. import risk
    p = {**PARAMS, **(params or {})}; trades = []; cap = capital_usd; daily = {}
    for sym, df in candles.items():
        validate_candles(df); df = df.copy(); df["d"] = df.index.date; adv = (adv_usd or {}).get(sym)
        df["tod"] = df.index.strftime("%H:%M"); pv = df.groupby("tod")["volume"]
        days = sorted(df["d"].unique())
        for di, d in enumerate(days):
            day = df[df["d"] == d]
            if len(day) < 20: continue
            prior = df[df["d"] < d]; rv_base = prior.groupby("tod")["volume"].mean() if len(prior) else None
            if rv_base is None or prior["d"].nunique() < 5: continue
            vw = vwap_proxy(day); nbar = max(1, p["or_minutes"] // p["bar_minutes"]); orh = day["high"].iloc[:nbar].max()
            tr = pd.concat([day["high"] - day["low"], (day["high"] - day["close"].shift()).abs(), (day["low"] - day["close"].shift()).abs()], axis=1).max(axis=1)
            atr = tr.rolling(14, min_periods=3).mean(); pos = None; day_pnl = 0.0
            for i in range(nbar, len(day) - 1):
                row = day.iloc[i]
                if pos is None:
                    if day_pnl <= -cap * p["daily_loss_breaker_pct"] / 100: break                       # circuit breaker: no new entries
                    rvb = rv_base.get(day["tod"].iloc[i]); rv = row["volume"] / rvb if rvb and rvb > 0 else np.nan
                    spread_ok = risk.half_spread_bps(adv, cfg) * 2 <= p["max_spread_bps"] if adv else False
                    if row["close"] > orh and row["close"] > vw.iloc[i] and rv >= p["rv_min"] and not np.isnan(atr.iloc[i]) and spread_ok:
                        nxt = day.iloc[i + 1]; ent, _, _ = risk.exec_price("BUY", float(nxt["open"]), adv, cfg); a = float(atr.iloc[i])
                        stop = ent - p["stop_atr_mult"] * a; tgt = ent + p["rr"] * (ent - stop); qty = math.floor(cap * cfg["risk_per_trade_pct"] / 100 / max(ent - stop, 1e-9))
                        qty = min(qty, math.floor(cap * cfg["max_position_pct"] / 100 / ent))
                        if qty >= 1: pos = {"i": i + 1, "ent": ent, "stop": stop, "tgt": tgt, "qty": qty}
                else:
                    if i <= pos["i"]: continue
                    r = day.iloc[i]; ex = None
                    if r["low"] <= pos["stop"]: ex = min(float(r["open"]), pos["stop"]); why = "stop"
                    elif r["high"] >= pos["tgt"]: ex = max(float(r["open"]), pos["tgt"]) if r["open"] >= pos["tgt"] else pos["tgt"]; why = "target"
                    if ex is not None:
                        pnl = _close(pos, ex, adv, cfg, why, sym, day.index[i], trades); day_pnl += pnl; pos = None
            if pos is not None:                                             # forced EOD liquidation at the last bar's close
                ex = float(day["close"].iloc[-1]); day_pnl += _close(pos, ex, adv, cfg, "EOD liquidation", sym, day.index[-1], trades)
            daily[str(d)] = daily.get(str(d), 0.0) + day_pnl
    tr_df = pd.DataFrame(trades)
    out = {"label": "RESEARCH BACKTEST on real delayed yfinance candles. " + LABEL, "n_trades": len(trades), "capital_usd": capital_usd, "params": p,
           "days_with_data": len(daily), "symbols": list(candles)}
    if len(tr_df):
        out.update({"net_pnl_usd": float(tr_df["pnl"].sum()), "win_rate": float((tr_df["pnl"] > 0).mean()), "costs_usd": float(tr_df["cost"].sum()), "avg_pnl_usd": float(tr_df["pnl"].mean()),
                    "return_pct_on_capital": float(tr_df["pnl"].sum() / capital_usd * 100), "worst_day_usd": float(min(daily.values())), "breaker_pct": p["daily_loss_breaker_pct"],
                    "exit_reasons": tr_df["why"].value_counts().to_dict()})
    return out, trades

def _close(pos, raw_exit, adv, cfg, why, sym, ts, trades):
    from .. import risk
    px, spr, slp = risk.exec_price("SELL", raw_exit, adv, cfg); val = px * pos["qty"]
    fin = risk.us_fee_breakdown("BUY", pos["ent"] * pos["qty"], pos["qty"], cfg)["total"]; fout = risk.us_fee_breakdown("SELL", val, pos["qty"], cfg)["total"]
    pnl = val - pos["ent"] * pos["qty"] - fin - fout
    trades.append({"symbol": sym, "ts": str(ts), "qty": pos["qty"], "entry": pos["ent"], "exit": px, "pnl": pnl, "cost": fin + fout + (spr + slp) * pos["qty"], "why": why}); return pnl

def circuit_breaker_hit(start_equity, equity_now, pct=2.0): return equity_now <= start_equity * (1 - pct / 100)

"""Feature frame (causal), regime classification, factor scores 0-100, rule checks. See docs/SCORING.md."""
import math
import numpy as np, pandas as pd
from . import indicators as ind

W = {"momentum": 0.25, "fundamental": 0.20, "technical": 0.20, "rs_regime": 0.15, "liquidity": 0.10, "risk": 0.10}
VOL_THRESHOLD = 0.25  # annualized 20d realized vol that makes a benchmark "volatile"

def lin(x, lo, hi):
    """Linear normalization to 0..100, clipped. NaN -> None."""
    if x is None or (isinstance(x, float) and math.isnan(x)): return None
    return float(min(100.0, max(0.0, (x - lo) / (hi - lo) * 100)))

def wavg(parts):
    """parts: list of (value or None, weight). Weighted avg over available; None if none."""
    a = [(v, w) for v, w in parts if v is not None]
    if not a: return None
    return sum(v * w for v, w in a) / sum(w for _, w in a)

def feature_frame(df: pd.DataFrame, bench_close: pd.Series | None = None) -> pd.DataFrame:
    """df columns open/high/low/close/volume indexed by date. Everything causal."""
    c = df["close"]; f = pd.DataFrame(index=df.index)
    f["close"] = c; f["open"] = df["open"]; f["high"] = df["high"]; f["low"] = df["low"]; f["volume"] = df["volume"]
    for n in (20, 50, 150, 200): f[f"sma{n}"] = ind.sma(c, n)
    for n in (50, 150, 200): f[f"sma{n}_slope"] = ind.slope(f[f"sma{n}"], 21)
    f["ema20"] = ind.ema(c, 20); f["ema50"] = ind.ema(c, 50)
    f["macd"], f["macd_sig"], f["macd_hist"] = ind.macd(c)
    f["rsi14"] = ind.rsi_wilder(c, 14)
    f["atr14"] = ind.atr_wilder(df, 14); f["atr_pct"] = f["atr14"] / c
    f["vol_ratio"] = ind.volume_ratio(df["volume"], 20)
    f["adv20_local"] = (c * df["volume"]).rolling(20, min_periods=20).mean()
    for n in (21, 63, 126, 252): f[f"r{n}"] = c / c.shift(n) - 1
    f["r252_ex21"] = c.shift(21) / c.shift(252) - 1       # 12-1 month momentum
    lr = np.log(c).diff()
    f["vol20"] = lr.rolling(20, min_periods=20).std() * math.sqrt(252)
    f["vol252"] = lr.rolling(252, min_periods=126).std() * math.sqrt(252)
    f["hi252"] = df["high"].rolling(252, min_periods=126).max(); f["lo252"] = df["low"].rolling(252, min_periods=126).min()
    f["dd_from_hi"] = c / f["hi252"] - 1
    f["hh20_prev"] = df["high"].shift(1).rolling(20, min_periods=20).max()
    f["res_prev252"] = df["high"].shift(1).rolling(252, min_periods=126).max()
    if bench_close is not None and len(bench_close):
        f["rs126"] = ind.relative_strength(c, bench_close, 126)
        f["rs252"] = ind.relative_strength(c, bench_close, 252)
    else:
        f["rs126"] = np.nan; f["rs252"] = np.nan
    return f

def classify_regime(f: pd.Series) -> dict:
    """Quantitative regime from benchmark features row. Order: volatile > bearish > bullish > sideways."""
    need = ["close", "sma50", "sma200", "sma200_slope", "vol20"]
    if any(pd.isna(f.get(k)) for k in need):
        return {"regime": "unknown", "reason": "insufficient benchmark history"}
    up = f["close"] > f["sma200"] and f["sma50"] > f["sma200"] and f["sma200_slope"] > 0
    down = f["close"] < f["sma200"] and f["sma50"] < f["sma200"]
    if f["vol20"] >= VOL_THRESHOLD: r = "volatile"
    elif down: r = "bearish"
    elif up: r = "bullish"
    else: r = "sideways"
    return {"regime": r, "close": float(f["close"]), "sma50": float(f["sma50"]), "sma200": float(f["sma200"]),
            "sma200_slope_21d": float(f["sma200_slope"]), "vol20": float(f["vol20"]),
            "reason": f"close {'>' if f['close']>f['sma200'] else '<'} SMA200, SMA50 {'>' if f['sma50']>f['sma200'] else '<'} SMA200, "
                      f"SMA200 21d slope {f['sma200_slope']*100:+.2f}%, 20d vol {f['vol20']*100:.1f}% (volatile>= {VOL_THRESHOLD*100:.0f}%)"}

REGIME_SCORE = {"bullish": 100, "sideways": 50, "volatile": 25, "bearish": 0, "unknown": 40}

def minervini(f: pd.Series, rs_pct: float | None = None) -> dict:
    """8-point Minervini trend template. rs_pct = universe RS percentile (0-100); None -> fallback rs252>0 proxy."""
    g = lambda k: f.get(k)
    def ok(cond): return None if cond is None else bool(cond)
    def cmp(a, b, op):
        if pd.isna(g(a)) or (isinstance(b, str) and pd.isna(g(b))): return None
        bv = g(b) if isinstance(b, str) else b
        return op(g(a), bv)
    rs_ok = (rs_pct >= 70) if rs_pct is not None else (None if pd.isna(g("rs252")) else g("rs252") > 0)
    crit = {
        "close>SMA150&SMA200": ok(None if any(pd.isna(g(k)) for k in ("close","sma150","sma200")) else g("close") > g("sma150") and g("close") > g("sma200")),
        "SMA150>SMA200": cmp("sma150", "sma200", lambda a, b: a > b),
        "SMA200 rising 1m": cmp("sma200_slope", 0, lambda a, b: a > b),
        "SMA50>SMA150>SMA200": ok(None if any(pd.isna(g(k)) for k in ("sma50","sma150","sma200")) else g("sma50") > g("sma150") > g("sma200")),
        "close>SMA50": cmp("close", "sma50", lambda a, b: a > b),
        "close>=1.3x 52w low": ok(None if any(pd.isna(g(k)) for k in ("close","lo252")) else g("close") >= 1.3 * g("lo252")),
        "close within 25% of 52w high": ok(None if any(pd.isna(g(k)) for k in ("close","hi252")) else g("close") >= 0.75 * g("hi252")),
        "RS strong (pctile>=70 or excess>0)": rs_ok,
    }
    passed = sum(1 for v in crit.values() if v); unknown = [k for k, v in crit.items() if v is None]
    return {"criteria": crit, "passed": passed, "total": 8, "unknown": unknown, "pass": passed >= 8 and not unknown}

def canslim_lite(f, fund, regime, rs_pct=None) -> dict:
    fu = fund or {}
    def th(v, t): return None if v is None else v >= t
    crit = {
        "C quarterly earnings growth>=25%": th(fu.get("earnings_growth"), 0.25),
        "A revenue growth>=15% or ROE>=17%": (None if fu.get("revenue_growth") is None and fu.get("roe") is None else
                                              bool((fu.get("revenue_growth") or -1) >= 0.15 or (fu.get("roe") or -1) >= 0.17)),
        "N within 5% of 52w high": None if pd.isna(f.get("dd_from_hi")) else bool(f["dd_from_hi"] >= -0.05),
        "S volume ratio>=1.0": None if pd.isna(f.get("vol_ratio")) else bool(f["vol_ratio"] >= 1.0),
        "L leader (RS pctile>=80 or excess126>0)": (rs_pct >= 80) if rs_pct is not None else (None if pd.isna(f.get("rs126")) else bool(f["rs126"] > 0)),
        "I institutional ownership>=20%": th(fu.get("inst_pct"), 0.20),
        "M market regime bullish": regime == "bullish",
    }
    return {"criteria": crit, "passed": sum(1 for v in crit.values() if v), "evaluated": sum(1 for v in crit.values() if v is not None), "total": 7}

def graham_style(f, fund) -> dict:
    fu = fund or {}
    pe, pb = fu.get("pe"), fu.get("pb")
    crit = {
        "P/E<=15": None if pe is None else bool(0 < pe <= 15),
        "P/B<=1.5": None if pb is None else bool(0 < pb <= 1.5),
        "P/E x P/B<=22.5": None if pe is None or pb is None else bool(0 < pe * pb <= 22.5),
        "Debt/Equity<=100%": None if fu.get("de") is None else bool(fu["de"] <= 100),
        "Current ratio>=2": None if fu.get("current_ratio") is None else bool(fu["current_ratio"] >= 2),
        "Positive earnings (trailing EPS>0)": None if fu.get("eps") is None else bool(fu["eps"] > 0),
        "Pays dividend": None if fu.get("div_yield") is None else bool(fu["div_yield"] > 0),
    }
    return {"criteria": crit, "passed": sum(1 for v in crit.values() if v), "evaluated": sum(1 for v in crit.values() if v is not None), "total": 7}

def score_momentum(f):
    parts = [(lin(f.get("r252_ex21"), -0.2, 0.8), .4), (lin(f.get("r126"), -0.1, 0.5), .3), (lin(f.get("r63"), -0.1, 0.3), .3)]
    parts = [(None if v is None or pd.isna(v) else v, w) for v, w in parts]
    return wavg(parts)

def score_fundamental(fund):
    """Returns (score, flags, detail). Documented missing-data rule: need >=4 of 7 metrics; average the available;
    penalty 2 pts per missing metric; <4 available -> 50 neutral minus 10 and flag."""
    fu = fund or {}
    inv = lambda v, best, worst: None if v is None else float(min(100, max(0, (worst - v) / (worst - best) * 100)))
    pe = fu.get("pe")
    comps = {
        "roe": lin(fu.get("roe"), 0.0, 0.25),
        "debt_equity": inv(fu.get("de"), 0, 200),
        "profit_margin": lin(fu.get("profit_margin"), 0.0, 0.25),
        "revenue_growth": lin(fu.get("revenue_growth"), -0.05, 0.25),
        "earnings_growth": lin(fu.get("earnings_growth"), -0.1, 0.4),
        "pe": (None if pe is None else (0.0 if pe <= 0 else inv(pe, 12, 40))),
        "fcf_yield": lin(fu.get("fcf_yield"), 0.0, 0.08),
    }
    avail = {k: v for k, v in comps.items() if v is not None}; missing = [k for k in comps if comps[k] is None]
    flags = []
    if len(avail) < 4:
        flags.append(f"fundamentals mostly missing ({len(avail)}/7): neutral 50 with -10 penalty")
        return 40.0, flags, {"components": comps, "missing": missing}
    s = sum(avail.values()) / len(avail) - 2 * len(missing)
    if missing: flags.append(f"fundamental metrics missing: {', '.join(missing)} (-2 each)")
    return max(0.0, s), flags, {"components": comps, "missing": missing}

def score_technical(f, mv):
    tr = mv["passed"] / mv["total"] * 100
    macd = None if pd.isna(f.get("macd_hist")) else (100.0 if f["macd_hist"] > 0 else 0.0)
    r = f.get("rsi14")
    rsi = None if pd.isna(r) else (100.0 if 50 <= r <= 70 else 60.0 if 40 <= r < 50 else 50.0 if 70 < r <= 80 else 15.0)
    ema = None if pd.isna(f.get("ema20")) or pd.isna(f.get("ema50")) else (100.0 if f["ema20"] > f["ema50"] else 0.0)
    return wavg([(tr, .5), (macd, .15), (rsi, .15), (ema, .20)])

def score_rs_regime(f, regime, rs_pct=None):
    rs = lin(f.get("rs126"), -0.15, 0.25)
    rs = None if rs is None or pd.isna(rs) else rs
    if rs_pct is not None: rs = 0.5 * (rs if rs is not None else rs_pct) + 0.5 * rs_pct
    return wavg([(rs, .6), (REGIME_SCORE.get(regime, 40), .4)])

def score_liquidity(f, fx):
    adv = f.get("adv20_local")
    if pd.isna(adv) or adv <= 0: return None
    myr = adv * fx
    return float(min(100, max(0, (math.log10(myr) - 6) / (math.log10(5e7) - 6) * 100)))  # RM1m ->0, RM50m ->100

def score_risk(f):
    inv = lambda v, best, worst: None if v is None or pd.isna(v) else float(min(100, max(0, (worst - v) / (worst - best) * 100)))
    return wavg([(inv(f.get("atr_pct"), 0.01, 0.08), .4), (inv(f.get("vol252"), 0.20, 0.70), .3), (inv(-f.get("dd_from_hi", np.nan) if not pd.isna(f.get("dd_from_hi")) else None, 0.05, 0.35), .3)])

def band(score):
    return "strong candidate" if score >= 85 else "watchlist/conditional" if score >= 70 else "hold" if score >= 50 else "no entry"

def composite(f: pd.Series, *, fund, regime, fx=1.0, rs_pct=None, use_fundamentals=True, light=False):
    """Returns dict(score, factors, weights_used, flags, mv, canslim, graham). Factors that are unavailable (None)
    are excluded and the weights renormalized with a flag (never silently zero-filled)."""
    flags = []
    mv = minervini(f, rs_pct)
    fs, fflags, fdet = score_fundamental(fund) if use_fundamentals else (None, [], {})
    flags += fflags
    factors = {"momentum": score_momentum(f), "fundamental": fs, "technical": score_technical(f, mv),
               "rs_regime": score_rs_regime(f, regime, rs_pct), "liquidity": score_liquidity(f, fx), "risk": score_risk(f)}
    used = {k: W[k] for k, v in factors.items() if v is not None}
    if not use_fundamentals: flags.append("fundamentals excluded (not available historically) - weights renormalized")
    for k, v in factors.items():
        if v is None and not (k == "fundamental" and not use_fundamentals): flags.append(f"factor {k} unavailable - weight renormalized")
    tot = sum(used.values())
    score = sum(factors[k] * w for k, w in used.items()) / tot if tot else 0.0
    return {"score": round(float(score), 2), "factors": {k: (None if v is None else round(float(v), 2)) for k, v in factors.items()},
            "weights_used": {k: round(w / tot, 4) for k, w in used.items()} if tot else {}, "flags": flags,
            "minervini": mv, "canslim": None if light else canslim_lite(f, fund, regime, rs_pct), "graham": None if light else graham_style(f, fund),
            "fund_detail": fdet}

def entry_trigger(f: pd.Series) -> dict:
    """Breakout: close > prior 20d high, vol_ratio>=1, RSI<=80.  Near-high: within 2% of prior 20d high, close>EMA20, RSI 50-70."""
    if any(pd.isna(f.get(k)) for k in ("close", "hh20_prev", "rsi14", "ema20")):
        return {"fired": False, "type": None, "reason": "insufficient data"}
    c, hh, rsi = f["close"], f["hh20_prev"], f["rsi14"]; vr = f.get("vol_ratio")
    if c > hh and (pd.isna(vr) or vr >= 1.0) and rsi <= 80:
        return {"fired": True, "type": "breakout", "reason": f"close {c:.3f} > prior 20d high {hh:.3f}, RSI {rsi:.0f}"}
    if c >= 0.98 * hh and c > f["ema20"] and 50 <= rsi <= 70:
        return {"fired": True, "type": "near-high", "reason": f"close {c:.3f} within 2% of prior 20d high {hh:.3f}, above EMA20, RSI {rsi:.0f}"}
    return {"fired": False, "type": None, "reason": f"no trigger: close {c:.3f} vs prior 20d high {hh:.3f}, RSI {rsi:.0f}"}

def plan_levels(f: pd.Series, cfg, analyst_target=None):
    """Entry (last close ref), ATR stop, and credible target (never invented).
    Target = prior 252d high if above entry (historical resistance); else analyst mean target (>=3 analysts) if above entry; else None."""
    c, a = f["close"], f["atr14"]
    stop = c - cfg["atr_stop_mult"] * a
    tgt, src = None, None
    res = f.get("res_prev252")
    if res is not None and not pd.isna(res) and res > c * 1.005: tgt, src = float(res), "prior 252d high (resistance)"
    elif analyst_target and analyst_target > c * 1.005: tgt, src = float(analyst_target), "analyst consensus mean target"
    rr = ind.reward_to_risk(c, stop, tgt)
    return {"entry": float(c), "stop": float(stop), "target": tgt, "target_source": src, "rr": rr, "atr": float(a)}

# ------------------------------------------------------------------ v2 additions (existing math above is unchanged)
WEIGHT_PCT = {"momentum": 25, "fundamental": 20, "technical": 20, "rs_regime": 15, "liquidity": 10, "risk": 10}

def validate_weights(w=None, pct=None):
    """Raises ValueError unless the score weights are exactly 25/20/20/15/10/10 and sum to 100 (and W matches)."""
    w = w or W; pct = pct or WEIGHT_PCT
    if sum(pct.values()) != 100: raise ValueError(f"score weights sum to {sum(pct.values())}, expected 100")
    if set(w) != set(pct) or any(abs(w[k] * 100 - pct[k]) > 1e-9 for k in pct): raise ValueError("W and WEIGHT_PCT disagree")
    if abs(sum(w.values()) - 1.0) > 1e-9: raise ValueError("fractional weights must sum to 1.0")
    return True
validate_weights()    # import-time validation

def enrich(f: pd.DataFrame, df: pd.DataFrame, spy_close=None, qqq_close=None) -> pd.DataFrame:
    """Adds v2 features (causal): SMA5, EMA12/26, RSI2, Bollinger(20,2), 52w proximity, RS vs SPY and QQQ, pullback/breakout structure flags."""
    g = f.copy(); c = df["close"]
    g["sma5"] = ind.sma(c, 5); g["ema12"] = ind.ema(c, 12); g["ema26"] = ind.ema(c, 26)
    g["rsi2"] = ind.rsi_wilder(c, 2)
    m, sd = ind.sma(c, 20), c.rolling(20, min_periods=20).std(ddof=0)
    g["bb_lower"] = m - 2 * sd; g["bb_upper"] = m + 2 * sd
    g["prox52"] = c / g["hi252"]                         # 1.0 = at 52-week high
    g["above_lo52"] = c / g["lo252"] - 1
    g["rs_spy126"] = ind.relative_strength(c, spy_close, 126) if spy_close is not None and len(spy_close) else np.nan
    g["rs_qqq126"] = ind.relative_strength(c, qqq_close, 126) if qqq_close is not None and len(qqq_close) else np.nan
    g["breakout20"] = (c > g["hh20_prev"]) & (g["vol_ratio"] >= 1.0)
    pull = (g["sma50"] > g["sma150"]) & (c < g["ema20"]) & (c > g["sma50"]) & (g["rsi14"].between(35, 55))
    g["pullback"] = pull                                 # uptrend (SMA50>SMA150), dipped below EMA20 but holding SMA50
    return g

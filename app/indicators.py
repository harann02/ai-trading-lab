"""Pure, causal indicator functions (value at t uses data <= t only)."""
import numpy as np, pandas as pd

def sma(s, n): return s.rolling(n, min_periods=n).mean()
def ema(s, n): return s.ewm(span=n, adjust=False, min_periods=n).mean()

def rsi_wilder(close: pd.Series, n=14) -> pd.Series:
    """Wilder RSI: seed = simple mean of first n gains/losses, then Wilder smoothing (alpha=1/n)."""
    d = close.diff()
    gain = d.clip(lower=0).to_numpy(); loss = (-d.clip(upper=0)).to_numpy()
    out = np.full(len(close), np.nan)
    if len(close) <= n: return pd.Series(out, index=close.index)
    ag = gain[1:n+1].mean(); al = loss[1:n+1].mean()
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(close)):
        ag = (ag * (n - 1) + gain[i]) / n; al = (al * (n - 1) + loss[i]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return pd.Series(out, index=close.index)

def true_range(df):
    pc = df["close"].shift()
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1, skipna=False)

def atr_wilder(df, n=14) -> pd.Series:
    tr = true_range(df).to_numpy(); out = np.full(len(df), np.nan)
    if len(df) <= n: return pd.Series(out, index=df.index)
    a = np.mean(tr[1:n+1]); out[n] = a
    for i in range(n + 1, len(df)):
        a = (a * (n - 1) + tr[i]) / n; out[i] = a
    return pd.Series(out, index=df.index)

def macd(close, fast=12, slow=26, sig=9):
    m = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    m[: slow - 1] = np.nan
    s = m.ewm(span=sig, adjust=False, ignore_na=True).mean()
    return m, s, m - s

def slope(s: pd.Series, n=20) -> pd.Series:
    """Fractional change over n bars."""
    return s / s.shift(n) - 1

def relative_strength(close, bench, n=126) -> pd.Series:
    """Excess return of asset vs benchmark over n bars (aligned on asset dates, benchmark ffilled)."""
    b = bench.reindex(close.index, method="ffill")
    return (close / close.shift(n) - 1) - (b / b.shift(n) - 1)

def volume_ratio(vol, n=20) -> pd.Series:
    """Today's volume / average of previous n days (excludes today)."""
    return vol / vol.shift(1).rolling(n, min_periods=n).mean()

def reward_to_risk(entry, stop, target):
    """(target-entry)/(entry-stop); None when no credible target or invalid stop."""
    if target is None or entry is None or stop is None: return None
    risk = entry - stop
    if risk <= 0 or target <= entry: return None
    return (target - entry) / risk

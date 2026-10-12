import os, sys
import numpy as np, pandas as pd
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from conftest import make_df, FakeProvider
NOW_SCAN = datetime(2026, 10, 12, 22, 30, tzinfo=timezone.utc)      # Mon 18:30 EDT (after close) = Tue 06:30 MYT
NOW_FILL = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)       # Tue 10:00 EDT (market open)

class IntradayProvider(FakeProvider):
    """FakeProvider that also serves 5-minute candles (real-shaped, UTC-indexed) for one session date: base price drifts by `step` per candle."""
    def __init__(self, frames, session="2026-10-13", base=None, step=0.0005, until="20:00", start="13:30", low_extra=None):
        super().__init__(frames); self.session = session; self.base = base; self.step = step; self.until = until; self.start = start; self.low_extra = low_extra or {}
    def intraday(self, symbol, interval="5m", period="5d"):
        if symbol not in self.frames: return pd.DataFrame()
        idx = pd.date_range(f"{self.session} {self.start}", f"{self.session} {self.until}", freq="5min", tz="UTC", inclusive="left")
        b = (self.base.get(symbol) if isinstance(self.base, dict) and symbol in self.base else None) or float(self.frames[symbol]["close"].iloc[-1]) * 1.002
        c = np.array([b * (1 + self.step * i) for i in range(len(idx))]); o = np.r_[b, c[:-1]]
        low = np.minimum(o, c) * 0.9995
        if symbol in self.low_extra:
            for k, v in self.low_extra[symbol].items(): low[k] = v
        return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.0005, "low": low, "close": c, "volume": 1e5}, index=idx)

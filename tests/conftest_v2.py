import os, sys
import numpy as np, pandas as pd
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from conftest import make_df, FakeProvider
NOW_SCAN = datetime(2026, 10, 12, 22, 30, tzinfo=timezone.utc)      # Mon 18:30 EDT (after close) = Tue 06:30 MYT
NOW_FILL = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)       # Tue 10:00 EDT (market open)

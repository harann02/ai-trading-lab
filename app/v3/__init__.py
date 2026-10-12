"""AI Trading Lab v3.0 layer: accounting migration, execution integrity, scheduler reliability, verification, shadow benchmarks, AI status.
Kept in its own package so the v2 engine modules stay readable; v2 modules call into it at well-defined points."""
VERSION = "3.0.0"
FILL_MODEL_FORWARD = "fwd-paper-delayed-v1"
FILL_MODEL_BACKTEST = "bt-next-open-v1"

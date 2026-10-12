# Execution model v3 (forward paper vs historical backtest)

Two **separate** fill models, recorded in `orders.fill_model_version` / `fill_kind`:
* `FORWARD_PAPER` (`app/v3/execution.py`) — delayed-execution methodology.
* `HISTORICAL_BACKTEST` (`backtest2.py`) — next-open fills on daily bars; hypothetical, never mixed into forward results.

## Forward fill rules
1. A signal created after the close becomes **eligible for the next NYSE session** (calendar-aware: weekends, holidays). Before that: `not_eligible`.
2. During the eligible session the simulator may fill only from **5-minute candles that were already observable**: a candle becomes available `fill_delay_min` (15) minutes after it ends. Fill price = close of the newest available candle (+ modelled half-spread + slippage, FX, fees). The order stores `price_available_ts` ≤ `exec_ts`.
3. No observable candle (feed empty, stale beyond `max_price_age_min`, session not started) → `awaiting_data`; session ended without a fill window → `lapsed` (next opportunity = next scan). **Never** a retroactive fill at an opening price seen later on EOD data. No fabricated candles.
4. A price > `corp_action_jump_pct` away from the last daily close → `review` (possible split / bad data), not filled.
5. Stops (`evaluate_stop`): stop-market from available candles after entry; a candle opening below the stop is a **gap** (fill at the open, worse than the stop); otherwise stop + `stop_extra_slippage_bps`. Always noted "NOT guaranteed". Missing bars / halts → no fill is invented; corporate actions are not modelled (flagged).
6. Recorded per order: signal ts, data ts, order type, qty, eligible session, data source, earliest availability ts, simulated exec ts & price, spread bps, slippage, fees, FX rate, fill-model version, status, fill kind/basis.
7. Cash account: BUY needs **settled** cash; SELL proceeds settle T+1 (NYSE sessions); equity = cash (settled + unsettled) + holdings at last verified price with timestamp.

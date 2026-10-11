"""Fees, slippage, position sizing, risk checks. All money values in MYR unless noted."""
import math

def fees_local(market, side, value_local, qty, cfg):
    """Fees in LOCAL currency for a trade of `value_local` (price*qty)."""
    f = cfg["fees"][market]
    if market == "BURSA":
        brk = max(value_local * f["brokerage_pct"] / 100, f["brokerage_min"])
        clr = min(value_local * f["clearing_pct"] / 100, f["clearing_max"])
        stamp = min(math.ceil(value_local / 1000) * f["stamp_per_1000"], f["stamp_max"])
        return brk + clr + stamp
    com = min(max(qty * f["commission_per_share"], f["commission_min"]), value_local * f["commission_max_pct"] / 100)
    sec = value_local * f["sec_fee_sell_pct"] / 100 if side == "SELL" else 0.0
    return com + sec

def slip_price(market, side, price, cfg):
    bps = cfg["slippage_bps"][market] / 1e4
    return price * (1 + bps) if side == "BUY" else price * (1 - bps)

def size_position(*, equity, cash, price, stop, market, fx, cfg, adv_local=None, current_exposure=0.0, n_positions=0):
    """Returns dict(qty, binding, detail). price/stop local; equity/cash/exposure MYR.
    qty = floor(risk_budget / loss_per_share_incl_slippage_and_fees) then capped by cash, lot, liquidity, concentration, exposure."""
    lot = cfg["lot_size"][market]
    fill = slip_price(market, "BUY", price, cfg)
    stop_fill = slip_price(market, "SELL", stop, cfg)
    per_sh_fee_est = (fees_local(market, "BUY", fill * 100, 100, cfg) + fees_local(market, "SELL", stop_fill * 100, 100, cfg)) / 100
    loss_ps = (fill - stop_fill) + per_sh_fee_est      # local ccy per share
    if loss_ps <= 0 or price <= 0 or stop >= price:
        return {"qty": 0, "binding": "invalid_stop", "detail": {}}
    budget = equity * cfg["risk_per_trade_pct"] / 100                      # MYR
    caps = {"risk": math.floor(budget / (loss_ps * fx))}
    unit_cost = fill * fx * (1 + cfg["fx_spread_bps"] / 1e4 if market == "US" else 1)
    cash_avail = max(0.0, cash - equity * cfg["min_cash_pct"] / 100)
    caps["cash"] = math.floor(cash_avail / (unit_cost * 1.01 + 1e-9))
    caps["position_pct"] = math.floor(equity * cfg["max_position_pct"] / 100 / unit_cost)
    caps["exposure"] = math.floor(max(0.0, equity * cfg["max_exposure_pct"] / 100 - current_exposure) / unit_cost)
    if adv_local and adv_local > 0:
        caps["liquidity"] = math.floor(adv_local * cfg["max_adv_pct"] / 100 / fill)
    caps = {k: max(0, v) for k, v in caps.items()}
    q = min(caps.values()); binding = min(caps, key=caps.get)
    q = (q // lot) * lot
    # small-account cost control: a trade must be worth >= min_trade_value_myr so the fixed Bursa minimum brokerage (RM8/side) stays small
    mtv = float(cfg.get("min_trade_value_myr", 0) or 0)
    if mtv > 0 and q * unit_cost < mtv:
        need = math.ceil(mtv / (unit_cost * lot)) * lot            # smallest lot-multiple reaching the minimum value
        room = min(v for k, v in caps.items() if k != "risk")      # bump may exceed the 1% risk budget but never cash/position/exposure/liquidity caps
        if need <= room:
            q = need
        else:
            return {"qty": 0, "binding": "min_trade_value", "detail": {"caps": caps, "lot": lot, "min_trade_value_myr": mtv, "fill_price_local": fill}}
    return {"qty": int(q), "binding": binding if q > 0 else (binding if min(caps.values()) < lot else "lot_size"),
            "detail": {"risk_budget_myr": budget, "loss_per_share_local": loss_ps, "caps": caps, "lot": lot,
                       "fill_price_local": fill}}

def portfolio_checks(*, cfg, equity, cash, n_positions, exposure_after, cost_myr, suspended):
    """Returns list of (name, passed, detail)."""
    c = []
    c.append(("entries_not_suspended", not suspended, "drawdown suspension active" if suspended else "ok"))
    c.append(("max_positions", n_positions < cfg["max_positions"], f"{n_positions}/{cfg['max_positions']}"))
    c.append(("min_cash", cash - cost_myr >= equity * cfg["min_cash_pct"] / 100 - 1e-6,
              f"cash after {cash - cost_myr:.2f} vs min {equity*cfg['min_cash_pct']/100:.2f}"))
    c.append(("max_exposure", exposure_after <= equity * cfg["max_exposure_pct"] / 100 + 1e-6,
              f"exposure after {exposure_after:.2f} vs max {equity*cfg['max_exposure_pct']/100:.2f}"))
    return c

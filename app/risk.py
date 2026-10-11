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

# ------------------------------------------------------------------ v2: USD-native US equity costs and sizing (v1 functions above are untouched)
def us_fee_breakdown(side, value_usd, qty, cfg):
    """Explicit regulatory/broker fees in USD. Default commission 0 (configurable). SEC fee + FINRA TAF apply to SELLS only."""
    f = cfg["us_fees"]
    comm = min(max(qty * f.get("commission_per_share", 0.0), f.get("commission_min", 0.0)), value_usd * f.get("commission_max_pct", 1.0) / 100) if (f.get("commission_per_share", 0) or f.get("commission_min", 0)) else 0.0
    sec = value_usd * f.get("sec_fee_sell_pct", 0.0) / 100 if side == "SELL" else 0.0
    taf = min(qty * f.get("finra_taf_per_share", 0.0), f.get("finra_taf_max", 1e9)) if side == "SELL" else 0.0
    return {"commission": comm, "sec_fee": sec, "finra_taf": taf, "total": comm + sec + taf}

def half_spread_bps(adv_usd, cfg):
    """MODELLED half-spread by liquidity tier (we have no quote feed on the free stack; this is an assumption, labelled as such)."""
    for lo, bps in cfg["spread_model"]["tiers"]:
        if adv_usd is not None and adv_usd >= lo: return float(bps)
    return float(cfg["spread_model"]["tiers"][-1][1])

def exec_price(side, ref_price, adv_usd, cfg):
    """Returns (fill_price, spread_per_share, slippage_per_share). Spread (half) + slippage move the price against us."""
    hs = half_spread_bps(adv_usd, cfg) / 1e4; sl = cfg["slippage_bps"]["US"] / 1e4
    sgn = 1 if side == "BUY" else -1
    return ref_price * (1 + sgn * (hs + sl)), ref_price * hs, ref_price * sl

def size_position_usd(*, equity_usd, usable_cash_usd, price, stop, cfg, adv_usd=None, sleeve_equity_usd=None, sleeve_exposure_usd=0.0, total_exposure_usd=0.0):
    """qty = floor(risk budget / (entry-stop + exit friction)), then capped by affordability (SETTLED usable cash after reserve), single-stock %, sleeve exposure,
    liquidity (ADV %). Whole shares unless cfg['fractional_shares']. Returns dict(qty, binding, detail). All values USD."""
    fill, _, _ = exec_price("BUY", price, adv_usd, cfg); sfill, _, _ = exec_price("SELL", stop, adv_usd, cfg)
    f_in = us_fee_breakdown("BUY", fill * 100, 100, cfg)["total"] / 100; f_out = us_fee_breakdown("SELL", sfill * 100, 100, cfg)["total"] / 100
    loss_ps = (fill - sfill) + f_in + f_out                   # entry-stop plus exit friction (spread/slippage on the exit fill and fees both sides)
    if price <= 0 or stop >= price or loss_ps <= 0: return {"qty": 0, "binding": "invalid_stop", "detail": {}}
    sleeve_eq = sleeve_equity_usd if sleeve_equity_usd is not None else equity_usd
    budget = equity_usd * cfg["risk_per_trade_pct"] / 100
    frac = bool(cfg.get("fractional_shares"))
    fl = (lambda x: math.floor(x * 1e4 + 1e-9) / 1e4) if frac else (lambda x: math.floor(x + 1e-9))
    unit = fill + f_in
    caps = {"risk": fl(budget / loss_ps),
            "affordability": fl(max(0.0, usable_cash_usd) / unit),
            "single_stock_pct": fl(equity_usd * cfg["max_position_pct"] / 100 / unit),
            "sleeve_exposure": fl(max(0.0, sleeve_eq * cfg["max_exposure_pct"] / 100 - sleeve_exposure_usd) / unit)}
    if adv_usd and adv_usd > 0: caps["liquidity"] = fl(adv_usd * cfg["max_adv_pct"] / 100 / fill)
    caps = {k: max(0, v) for k, v in caps.items()}
    q = min(caps.values()); binding = min(caps, key=caps.get)
    return {"qty": q, "binding": binding if q > 0 else (binding if min(caps.values()) <= 0 else "none"),
            "detail": {"risk_budget_usd": budget, "loss_per_share_usd": loss_ps, "caps": caps, "fill_price_usd": fill, "fractional": frac}}

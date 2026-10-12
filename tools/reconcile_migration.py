"""Before/after reconciliation of the v3 migration. Usage: python tools/reconcile_migration.py <before.db> <after.db>
Compares ledger-derived totals (cash MYR, cash USD, deposits, positions, equity at the same FX) and verifies the journal entries are not deposits/withdrawals/P&L."""
import sys, os, json, sqlite3, importlib
def load(path):
    os.environ["AITL_DB"] = path
    import app.db as dbm; importlib.reload(dbm)
    for m in ("app.ledger", "app.portfolio"):
        importlib.reload(importlib.import_module(m))
    from app import ledger, portfolio
    rows = ledger.rows(); rp = portfolio.replay2(rows)
    return rows, rp
def summarize(path, fx):
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row
    led = [dict(r) for r in c.execute("SELECT * FROM ledger ORDER BY id")]
    dep = sum(r["cash_delta_myr"] for r in led if r["kind"] in ("DEPOSIT", "WITHDRAW"))
    myr = sum(r["cash_delta_myr"] for r in led); usd = sum(r["cash_delta_usd"] or 0 for r in led)
    kinds = {}
    for r in led: kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    c.close()
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    rows, rp = load(path)
    pos = {f"{k[0]}:{k[1]}": {"qty": v["qty"], "cost_usd": round(v["cost_usd"], 4)} for k, v in rp["positions"].items()}
    sl = {k: {"cash_usd": round(s["cash_usd"], 4), "invested_myr": round(s["invested_myr"], 4), "usd_received": round(s["usd_received"], 4)} for k, s in rp["sleeves"].items()}
    cost = sum(v["cost_usd"] for v in rp["positions"].values())
    return {"ledger_rows": len(led), "kinds": kinds, "net_deposits_myr": round(dep, 4), "cash_myr": round(myr, 4), "cash_usd_total": round(usd, 4), "sleeve_cash_usd_sum": round(sum(s["cash_usd"] for s in rp["sleeves"].values()), 4),
            "sleeves": sl, "positions": pos, "positions_cost_usd": round(cost, 4), "invested_myr_sum": round(sum(s["invested_myr"] for s in rp["sleeves"].values()), 4), "usd_received_sum": round(sum(s["usd_received"] for s in rp["sleeves"].values()), 4),
            "realized_usd_sum": round(sum(s["realized_usd"] for s in rp["sleeves"].values()), 4), "fx_spread_myr_sum": round(sum(s["fx_spread_myr"] for s in rp["sleeves"].values()), 4),
            "equity_at_cost_usd": round(sum(s["cash_usd"] for s in rp["sleeves"].values()) + cost, 4)}
if __name__ == "__main__":
    b = sys.argv[1]; a = sys.argv[2]; fx = 1.0
    sb = summarize(b, fx); 
    import subprocess; sa = None
    # run 'after' in a fresh interpreter to avoid cached module state
    if len(sys.argv) > 3 and sys.argv[3] == "--child": print(json.dumps(summarize(a, fx))); sys.exit()
    out = subprocess.run([sys.executable, __file__, b, a, "--child"], capture_output=True, text=True); sa = json.loads(out.stdout.strip().splitlines()[-1])
    checks = [("net deposits unchanged", sb["net_deposits_myr"] == sa["net_deposits_myr"]), ("MYR cash unchanged", abs(sb["cash_myr"] - sa["cash_myr"]) < 1e-6),
              ("USD cash total unchanged (internal journal nets to 0)", abs(sb["cash_usd_total"] - sa["cash_usd_total"]) < 1e-6), ("sleeve USD cash sum unchanged", abs(sb["sleeve_cash_usd_sum"] - sa["sleeve_cash_usd_sum"]) < 1e-6),
              ("invested MYR (cost basis) sum unchanged", abs(sb["invested_myr_sum"] - sa["invested_myr_sum"]) < 1e-6), ("USD received sum unchanged", abs(sb["usd_received_sum"] - sa["usd_received_sum"]) < 1e-6),
              ("realized P&L unchanged (journal creates no profit)", abs(sb["realized_usd_sum"] - sa["realized_usd_sum"]) < 1e-6), ("FX spread paid unchanged (no double FX)", abs(sb["fx_spread_myr_sum"] - sa["fx_spread_myr_sum"]) < 1e-6),
              ("positions unchanged (nothing sold)", sb["positions"] == sa["positions"]), ("equity at cost unchanged", abs(sb["equity_at_cost_usd"] - sa["equity_at_cost_usd"]) < 1e-6)]
    print(json.dumps({"before": sb, "after": sa}, indent=1)); [print(("PASS " if ok else "FAIL ") + n) for n, ok in checks]; sys.exit(0 if all(ok for _, ok in checks) else 1)

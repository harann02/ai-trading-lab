"""Survivorship / index-membership exposure check (free data: Wikipedia S&P 500 change log + yfinance coverage of removed tickers).
Measures (does NOT fix) the bias in the 2021+ backtests: how many current members joined after the backtest start (look-ahead on additions) and how many removed names still have price data.
Usage: python tools/index_membership_check.py [out.json]"""
import sys, json, io, requests, pandas as pd
URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
def main(out):
    h = requests.get(URL, headers={"User-Agent": "ai-trading-lab-research/3.0 (paper-trading study)"}, timeout=30); h.raise_for_status()
    t = pd.read_html(io.StringIO(h.text)); cur, chg = t[0], t[1]
    cur["Date added"] = pd.to_datetime(cur["Date added"], errors="coerce"); start = pd.Timestamp("2021-01-04")
    late = cur[cur["Date added"] > start]
    removed, ok, note = [], [], "removed-names log NOT available: the free Wikipedia page no longer exposes the changes table in this layout"
    try:
        chg = t[1] if len(t) > 1 and t[1].shape[1] >= 6 else None
        if chg is not None:
            chg.columns = ["_".join([str(x) for x in c if "Unnamed" not in str(x)]) if isinstance(c, tuple) else str(c) for c in chg.columns]
            dcol = [c for c in chg.columns if c.lower().startswith(("effective", "date"))][0]; chg[dcol] = pd.to_datetime(chg[dcol], errors="coerce")
            rcol = [c for c in chg.columns if "removed" in c.lower() and "ticker" in c.lower()]
            removed = sorted({str(x) for x in chg[chg[dcol] > start][rcol[0]].dropna()}); note = "changes table parsed"
            import yfinance as yf
            for s_ in removed:
                try:
                    if len(yf.download(s_, period="6y", interval="1d", progress=False, auto_adjust=False)) > 300: ok.append(s_)
                except Exception: pass
    except Exception as e: note += f" ({type(e).__name__}: {e})"
    res = {"source": "Wikipedia S&P 500 constituents + changes (free; incomplete/unofficial)", "backtest_start": str(start.date()), "current_members": int(len(cur)),
           "current_members_added_after_start": int(len(late)), "pct_added_after_start": round(len(late) / len(cur) * 100, 1), "removed_since_start": len(removed), "removed_with_usable_yfinance_history": len(ok), "removed_names_note": note,
           "removed_with_history_tickers": ok[:60],
           "reading": "A static 'current members' universe includes names that only entered the index after 2021 (selected by their later success) and omits names removed since (often losers). A complete free point-in-time membership + delisted-price history was not obtainable here, so a point-in-time universe cannot be rebuilt. The bias is MEASURED here, not removed; results stay optimistic by an unknown amount."}
    json.dump(res, open(out, "w"), indent=1); print(json.dumps({k: v for k, v in res.items() if k != "removed_with_history_tickers"}, indent=1))
if __name__ == "__main__": main(sys.argv[1] if len(sys.argv) > 1 else "validation/membership_check.json")

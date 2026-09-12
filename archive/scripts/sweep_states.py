"""
Per-state sweep of the §12 kill-check across every state and UT in SHRUG 2.2.

Per-state, never pooled. A pooled all-India histogram would look well spread
precisely BECAUSE states differ (Punjab uniformly irrigated, Jharkhand uniformly
rainfed) -- that measures between-state variation, not the within-unit
heterogeneity the index is about. Pooling would flatter the result.

Reuses the measurement functions in killcheck_mp.py, so this runs the identical
computation as the single-state report rather than a reimplementation.

Usage:  python scripts/sweep_states.py
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import killcheck_mp as kc  # noqa: E402

# Display labels only. NOT a data source: PC11 state codes are the join key and
# come from shrid2. Proper names belong in SHRUG Core Keys, which is not yet
# downloaded -- swap these for Core Keys names before anything reaches the UI
# (§9.2: every label on screen traces to a named source).
STATE_NAMES = {
    1: "Jammu & Kashmir", 2: "Himachal Pradesh", 3: "Punjab", 4: "Chandigarh",
    5: "Uttarakhand", 6: "Haryana", 7: "NCT of Delhi", 8: "Rajasthan",
    9: "Uttar Pradesh", 10: "Bihar", 11: "Sikkim", 12: "Arunachal Pradesh",
    13: "Nagaland", 14: "Manipur", 15: "Mizoram", 16: "Tripura",
    17: "Meghalaya", 18: "Assam", 19: "West Bengal", 20: "Jharkhand",
    21: "Odisha", 22: "Chhattisgarh", 23: "Madhya Pradesh", 24: "Gujarat",
    25: "Daman & Diu", 26: "Dadra & Nagar Haveli", 27: "Maharashtra",
    28: "Andhra Pradesh", 29: "Karnataka", 30: "Goa", 31: "Lakshadweep",
    32: "Kerala", 33: "Tamil Nadu", 34: "Puducherry",
    35: "Andaman & Nicobar Islands",
}

OUT_CSV = Path("data/interim/state_sweep.csv")
MIN_VILLAGES = 100  # below this, distribution statistics are not meaningful


def main():
    print("Loading Village Directory (one pass)...")
    df = kc.read_vd(kc.DEFAULT_DTA)
    df["pc11_state_id"] = kc.state_code_series(df)
    total = len(df)
    unknown = int(df["pc11_state_id"].isna().sum())
    print(f"rows: {total:,}   unparseable state code: {unknown:,}")

    codes = sorted(int(c) for c in df["pc11_state_id"].dropna().unique())
    print(f"states/UTs present: {len(codes)}\n")

    rows = []
    for code in codes:
        sub = df[df["pc11_state_id"] == code]
        m = kc.measures(sub)
        c1, d5, d6 = m["component1"], m["div5"], m["div6"]
        rows.append({
            "code": code,
            "state": STATE_NAMES.get(code, f"UNKNOWN({code})"),
            "villages": m["n_villages"],
            "rainfed_mean": c1["mean"],
            "rainfed_median": c1["median"],
            "rainfed_sd": c1["sd"],
            "rainfed_iqr": c1["iqr"],
            "extreme_share": c1["extreme_share"],
            "dom6_095": d6["dom_0.95"],
            "dom5_095": d5["dom_0.95"],
            "undefined_6cat": d6["n_undefined"],
            "integrity_share": m["integrity_share"],
            "zero_sown": m["zero_sown"],
            "recon_exact_zero": m["reconciliation"]["exact_zero_share"],
            "recon_max_abs": m["reconciliation"]["max_abs"],
            "recon_fail_share": m["reconciliation"]["fail_share"],
            "sources_sum_ha": m["reconciliation"]["sources_sum"],
            "aggregate_sum_ha": m["reconciliation"]["aggregate_sum"],
            "verdict": m["verdict"] if m["n_villages"] >= MIN_VILLAGES else "TOO FEW ROWS",
        })

    out = pd.DataFrame(rows).sort_values("rainfed_iqr", ascending=False)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_CSV, index=False)

    hdr = (f"{'code':>4}  {'state':<26} {'villages':>9} {'mean':>6} {'IQR':>6} "
           f"{'extreme':>8} {'dom6':>7} {'recon!':>7} {'integ':>6}  verdict")
    print(hdr)
    print("-" * len(hdr))
    for r in out.itertuples():
        print(f"{r.code:>4}  {r.state:<26} {r.villages:>9,} {r.rainfed_mean:>6.3f} "
              f"{r.rainfed_iqr:>6.3f} {r.extreme_share:>7.1%} {r.dom6_095:>6.1%} "
              f"{r.recon_fail_share:>6.1%} {r.integrity_share:>5.1%}  {r.verdict}")

    print(f"\n{'-' * 74}")
    counted = out[out["verdict"] != "TOO FEW ROWS"]
    for v in ("PASSES", "FAILS", "MECHANISM WEAK", "MECHANISM ABSENT",
              "SOURCE DATA UNRELIABLE", "NO DATA"):
        sel = counted[counted["verdict"] == v]
        print(f"{v:<18} {len(sel):>3} states   {sel['villages'].sum():>9,} villages "
              f"({sel['villages'].sum() / total:6.2%} of all rows)")
    small = out[out["verdict"] == "TOO FEW ROWS"]
    if len(small):
        print(f"{'TOO FEW ROWS':<18} {len(small):>3} states   "
              f"{small['villages'].sum():>9,} villages")
    print(f"\nWritten: {OUT_CSV}")


if __name__ == "__main__":
    main()

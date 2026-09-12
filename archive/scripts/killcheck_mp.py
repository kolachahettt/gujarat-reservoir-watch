"""
Kill-check: PMFBY basis-risk index, §12 task 1.

Loads the SHRUG Village Directory for one state and reports whether the
irrigated/unirrigated split has enough spread to support a village-level
heterogeneity index. See PROJECT_BRIEF.md §7 (index spec) and §12 (kill criterion).

Reports, in this order:
  1. rainfed-share histogram
  2. the four headline numbers
  3. verdict

No imputation anywhere. Every exclusion is counted and reported (§9.1, §9.5).
Undefined values are reported as undefined, never coerced to zero.

The measurement functions here are shared with scripts/sweep_states.py so the
national sweep runs the identical computation, not a reimplementation.

Usage:  python scripts/killcheck_mp.py [--state 23]
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyreadstat

# --- fixed decisions ----------------------------------------------------------

# Kill criterion (§12): if more than this share of villages are effectively
# single-category on the 6-category measure, the index as designed is dead.
KILL_THRESHOLD = 0.70

# "Effectively single-category" = largest category share at or above this.
DOMINANCE_PRIMARY = 0.95
DOMINANCE_REPORTED = (0.90, 0.95, 0.99)

# Component-1 variance test. §7 makes the irrigated/unirrigated split THE
# mechanism, so a state where that split has no variance must report
# "mechanism absent", not "passes" on the second-order source-mix signal.
#
# THRESHOLD JUSTIFICATION (recorded in PROJECT_BRIEF.md §13).
#
# A single eyeballed cut cannot be justified. Across the 35 states the IQR runs
# 0.000, 0.017, 0.088, 0.115, 0.148, 0.151, 0.166, 0.228, 0.243, ... -- it is
# continuous through the entire region where a cut would sit, so any single
# number in 0.09-0.16 is arbitrary. The earlier MECHANISM_MIN_IQR = 0.10 was
# exactly that, and Haryana (0.088) vs Goa (0.151) turned on it.
#
# The binary is therefore replaced by a banded rule with a substantive anchor
# and an explicit MARGINAL band for states near the line.
#
# Anchor: PMFBY's own indemnity levels step in 10-percentage-point increments
# (70/80/90%). Two villages are treated as materially different in exposure when
# their rainfed shares differ by at least one such step. IQR >= 0.15 means the
# middle half of a state's villages span more than one step. IQR < 0.05 means
# they span less than half a step: effectively one regime.
#
# Caveat, stated because it matters: indemnity levels are percentages of
# threshold YIELD, while rainfed share is a percentage of AREA. The anchor is an
# argument by analogy to the scheme's own granularity, not a derived constant.
# It is a documented convention -- do not present it as physics.
MECHANISM_ABSENT_MAX_IQR = 0.05   # below this: one regime, index uninformative
MECHANISM_WEAK_MAX_IQR = 0.15     # below this: under one indemnity step of spread

# Reconciliation gate. The 6-category measure is built on the FIVE source
# columns plus un_irr, not on the reported src_irr aggregate. Where the five
# do not reconcile with the aggregate, the measure is computed on an incomplete
# denominator and its output is meaningless -- Maharashtra reports 15.1m ha
# irrigated but its five source columns sum to 3.7m ha, with 8,171 villages
# showing an irrigated total and all five sources zero. That state scored the
# LOWEST single-category share in the national sweep (0.01%) purely as an
# artifact. Threshold set at 5% after seeing the national spread; it separates
# Maharashtra (20.7%) and Arunachal (12.2%) from Uttarakhand (0.17%) and the
# 25 states at 0.00%.
RECON_FAIL_MAX = 0.05

PC11_STATE_MP = 23

IRR_SOURCE_COLS = [
    "pc11_vd_land_canal_irr",
    "pc11_vd_land_wl_tw_irr",
    "pc11_vd_land_tnk_lk_irr",
    "pc11_vd_land_w_fall_irr",
    "pc11_vd_land_oth_src_irr",
]
COL_SRC_IRR_TOTAL = "pc11_vd_land_src_irr"
COL_UN_IRR = "pc11_vd_land_un_irr"
COL_NET_SOWN = "pc11_vd_land_nt_swn"
COL_AREA = "pc11_vd_area"
COL_POWER_SUM = "pc11_vd_power_agr_sum"
FLAG_COLS = ["land_flag", "dist_flag"]

REQUIRED = IRR_SOURCE_COLS + [COL_SRC_IRR_TOTAL, COL_UN_IRR, COL_NET_SOWN]
LOAD_COLS = REQUIRED + [COL_AREA, COL_POWER_SUM] + FLAG_COLS + ["shrid2"]

DEFAULT_DTA = Path("data/raw/pc11_vd_clean_shrid.dta")
OUT_DB = Path("data/interim/killcheck.duckdb")


def rule(title):
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


# --- loading ------------------------------------------------------------------

def read_vd(path, usecols=None):
    """Read the .dta, validating required columns. Fails loudly."""
    if not path.exists():
        sys.exit(f"MISSING FILE: {path}\nNothing has been computed. Download it first.")

    _, meta = pyreadstat.read_dta(str(path), metadataonly=True)
    available = list(meta.column_names)
    missing = [c for c in REQUIRED if c not in available]
    if missing:
        print("COLUMN INVENTORY (first 60):")
        for c in available[:60]:
            print(f"  {c}")
        sys.exit(
            f"\nREQUIRED COLUMNS ABSENT: {missing}\n"
            "Column naming differs from PROJECT_BRIEF.md §5. Resolve before computing."
        )
    if "shrid2" not in available:
        sys.exit("shrid2 absent: cannot identify state. Stopping.")

    wanted = [c for c in (usecols or LOAD_COLS) if c in available]
    df, _ = pyreadstat.read_dta(str(path), usecols=sorted(set(wanted)))
    return df


def state_code_series(df):
    """shrid2 is YY-SS-DDD-SSSSS-VVVVVV. field[1] is the PC11 state code.

    Verified 11 Sep 2026: field[0] is the census year, constant '11'.
    """
    return pd.to_numeric(df["shrid2"].astype(str).str.split("-").str[1], errors="coerce")


# --- measurement (shared with the sweep; no printing unless verbose) ----------

def shannon(shares):
    with np.errstate(divide="ignore", invalid="ignore"):
        terms = np.where(shares > 0, shares * np.log(shares), 0.0)
    return -terms.sum(axis=1)


def category_measures(df, cols, label="", verbose=True):
    """Diversity + dominance over a set of area columns. Zero-area rows stay undefined."""
    area = df[cols].apply(pd.to_numeric, errors="coerce").to_numpy(dtype="float64")
    area = np.where(area < 0, np.nan, area)  # negative area is not data
    total = np.nansum(area, axis=1)

    defined = total > 0
    shares = np.full_like(area, np.nan)
    shares[defined] = area[defined] / total[defined, None]

    h = np.full(len(df), np.nan)
    h[defined] = shannon(shares[defined])
    dom = np.full(len(df), np.nan)
    dom[defined] = np.nanmax(shares[defined], axis=1)

    stats = {
        "n_defined": int(defined.sum()),
        "n_undefined": int((~defined).sum()),
        "h_mean": float(np.nanmean(h)) if defined.any() else float("nan"),
        "h_median": float(np.nanmedian(h)) if defined.any() else float("nan"),
        "zero_h_share": float((h[defined] == 0).mean()) if defined.any() else float("nan"),
    }
    for t in DOMINANCE_REPORTED:
        stats[f"dom_{t:.2f}"] = (
            float((dom[defined] >= t).mean()) if defined.any() else float("nan")
        )

    if verbose:
        print(f"\n--- {label} ({len(cols)} categories, max H = ln {len(cols)} "
              f"= {np.log(len(cols)):.3f}) ---")
        print(f"defined (total area > 0):   {stats['n_defined']:,}")
        print(f"undefined (zero area):      {stats['n_undefined']:,}   "
              "<- reported as undefined, NOT as zero diversity")
        if defined.any():
            print(f"H mean / median:            {stats['h_mean']:.3f} / "
                  f"{stats['h_median']:.3f}")
            print(f"H exactly 0 (single cat):   {(h[defined] == 0).sum():,}  "
                  f"({stats['zero_h_share']:.2%} of defined)")
            for t in DOMINANCE_REPORTED:
                mark = "  <- criterion" if t == DOMINANCE_PRIMARY else ""
                print(f"dominance >= {t:.2f}:           {stats[f'dom_{t:.2f}']:7.2%}{mark}")
    return h, dom, defined, stats


def rainfed_shares(df):
    """Two denominators. In SHRUG 2.2 these are the same quantity; see identity_rate."""
    un = pd.to_numeric(df[COL_UN_IRR], errors="coerce")
    irr = pd.to_numeric(df[COL_SRC_IRR_TOTAL], errors="coerce")
    sown = pd.to_numeric(df[COL_NET_SOWN], errors="coerce")

    denom_a = un + irr
    share_a = np.where(denom_a > 0, un / denom_a, np.nan)
    share_b = np.where(sown > 0, un / sown, np.nan)

    both = sown > 0
    ident = ((un + irr) - sown).abs()
    identity_rate = float((ident[both] <= 0.01).mean()) if both.any() else float("nan")
    return share_a, share_b, identity_rate


def component1(share):
    """Variance of the §7 mechanism: the irrigated/unirrigated split itself."""
    s = pd.Series(share).dropna()
    if s.empty:
        return {"n": 0, "mean": float("nan"), "median": float("nan"), "sd": float("nan"),
                "iqr": float("nan"), "p5": float("nan"), "p95": float("nan"),
                "extreme_share": float("nan"), "band": "no_data"}
    iqr = float(s.quantile(0.75) - s.quantile(0.25))
    extreme = float(((s < 0.05) | (s > 0.95)).mean())
    return {
        "n": int(len(s)),
        "mean": float(s.mean()), "median": float(s.median()), "sd": float(s.std()),
        "iqr": iqr, "p5": float(s.quantile(0.05)), "p95": float(s.quantile(0.95)),
        "extreme_share": extreme,
        "band": ("absent" if iqr < MECHANISM_ABSENT_MAX_IQR
                 else "weak" if iqr < MECHANISM_WEAK_MAX_IQR else "present"),
    }


def verdict(dom6_share, c1, n_defined=None, recon_fail_share=None):
    """Five outcomes. Data availability and integrity outrank the spread tests.

    Order matters: a state with no data must not be reported as having no
    mechanism, and a state whose source columns do not reconcile must not be
    reported on at all -- its spread statistics are computed on an incomplete
    denominator and can look arbitrarily good.
    """
    if n_defined is not None and n_defined == 0:
        return ("NO DATA",
                "every land-use column is zero for every village in this state. "
                "Not a finding about irrigation: the Village Directory land module "
                "has no coverage here.")
    if recon_fail_share is not None and recon_fail_share > RECON_FAIL_MAX:
        return ("SOURCE DATA UNRELIABLE",
                f"{recon_fail_share:.1%} of irrigated villages have a source breakdown "
                f"that does not reconcile with the reported irrigated total (gate: "
                f"{RECON_FAIL_MAX:.0%}). The 6-category measure is computed on an "
                "incomplete denominator here; its value is not interpretable.")
    if c1["band"] == "absent":
        return ("MECHANISM ABSENT",
                f"rainfed-share IQR = {c1['iqr']:.3f} < {MECHANISM_ABSENT_MAX_IQR}. The §7 "
                "mechanism has no variance here: the index would be carried entirely by "
                "the second-order source-mix signal. Report as INDEX UNINFORMATIVE, "
                "not as a pass.")
    if c1["band"] == "weak":
        return ("MECHANISM WEAK",
                f"rainfed-share IQR = {c1['iqr']:.3f}, below one PMFBY indemnity step "
                f"({MECHANISM_WEAK_MAX_IQR}). The mechanism varies, but the middle half of "
                "villages span less than one step of exposure. Excluded from v1 primary "
                "coverage; reported with the margin visible, not silently binned.")
    if dom6_share > KILL_THRESHOLD:
        return ("FAILS",
                f"{dom6_share:.2%} of villages effectively single-category, above the "
                f"{KILL_THRESHOLD:.0%} threshold. Index as designed is dead; reshape "
                "rather than patch.")
    return ("PASSES",
            f"{dom6_share:.2%} single-category (threshold {KILL_THRESHOLD:.0%}), "
            f"rainfed-share IQR = {c1['iqr']:.3f}. Spread supports the index.")


def integrity_checks(df, verbose=True):
    """Substitute for the degenerate shipped flags (§5). Reports; drops nothing."""
    if verbose:
        rule("INTEGRITY CHECKS (substitute for the degenerate shipped flags)")
    sown = pd.to_numeric(df[COL_NET_SOWN], errors="coerce")
    irr = pd.to_numeric(df[COL_SRC_IRR_TOTAL], errors="coerce")
    un = pd.to_numeric(df[COL_UN_IRR], errors="coerce")
    area = pd.to_numeric(df[COL_AREA], errors="coerce") if COL_AREA in df else None

    checks = {
        "net_sown == 0": (sown == 0),
        "net_sown missing": sown.isna(),
        "un_irr missing": un.isna(),
    }
    if area is not None:
        checks["area == 0"] = (area == 0)
        checks["src_irr > total area (impossible)"] = (irr > area)
        checks["net_sown > total area (impossible)"] = (sown > area)

    masks = {k: v.fillna(False) for k, v in checks.items()}
    low_conf = np.logical_or.reduce([m.to_numpy() for m in masks.values()])
    counts = {k: int(m.sum()) for k, m in masks.items()}
    counts["union"] = int(low_conf.sum())

    if verbose:
        for label, m in masks.items():
            print(f"  {label:<38} {int(m.sum()):>7,}  ({m.mean():6.2%})")
        print(f"\n  union of integrity failures (low-confidence per §9.4): "
              f"{counts['union']:,} ({low_conf.mean():.2%})")
    return counts, low_conf


def reconciliation(df, verbose=True):
    """§12 step 3: does the reported irrigated total match the source columns?"""
    parts = df[IRR_SOURCE_COLS].apply(pd.to_numeric, errors="coerce").sum(axis=1)
    total = pd.to_numeric(df[COL_SRC_IRR_TOTAL], errors="coerce")
    resid = total - parts
    # Reconciliation failure, measured only where the aggregate says there IS
    # irrigation: either the five sources sum to nothing, or they disagree with
    # the aggregate by more than 1 ha.
    irrigated = total > 0
    broken = irrigated & (parts.isna() | (parts <= 0) | (resid.abs() > 1))
    stats = {
        "n": int(resid.notna().sum()),
        "exact_zero_share": float((resid == 0).mean()),
        "within_1ha": int((resid.abs() <= 1).sum()),
        "median": float(resid.median()),
        "max_abs": float(resid.abs().max()),
        "n_irrigated": int(irrigated.sum()),
        "fail_share": float(broken.sum() / irrigated.sum()) if irrigated.any()
        else float("nan"),
        "sources_sum": float(parts.sum()),
        "aggregate_sum": float(total.sum()),
    }
    if verbose:
        rule("RECONCILIATION: pc11_vd_land_src_irr vs sum of five source columns")
        print(f"n with both sides computable: {stats['n']:,}")
        print(f"exact zero residual:          {(resid == 0).sum():,}  "
              f"({stats['exact_zero_share']:.2%})")
        print(f"|residual| <= 1 ha:           {stats['within_1ha']:,}")
        print("\nresidual distribution (ha):")
        print(resid.describe(percentiles=[0.01, 0.25, 0.5, 0.75, 0.99]).to_string())
        print(f"\nlargest |residual|:           {stats['max_abs']:,.1f} ha")
    return resid, stats


def measures(df_state):
    """Every kill-check number for one state's rows. No printing. Used by the sweep."""
    share_a, share_b, identity_rate = rainfed_shares(df_state)
    c1 = component1(share_a)
    integ, low_conf = integrity_checks(df_state, verbose=False)
    _, recon = reconciliation(df_state, verbose=False)
    _, _, _, d5 = category_measures(df_state, IRR_SOURCE_COLS, verbose=False)
    _, dom6, def6, d6 = category_measures(
        df_state, IRR_SOURCE_COLS + [COL_UN_IRR], verbose=False)

    code, text = verdict(d6[f"dom_{DOMINANCE_PRIMARY:.2f}"], c1,
                         n_defined=d6["n_defined"],
                         recon_fail_share=recon["fail_share"])
    sown = pd.to_numeric(df_state[COL_NET_SOWN], errors="coerce")
    power = (pd.to_numeric(df_state[COL_POWER_SUM], errors="coerce")
             if COL_POWER_SUM in df_state else pd.Series(dtype="float64"))
    return {
        "n_villages": len(df_state),
        "component1": c1,
        "identity_rate": identity_rate,
        "integrity": integ,
        "integrity_share": integ["union"] / len(df_state) if len(df_state) else float("nan"),
        "reconciliation": recon,
        "div5": d5,
        "div6": d6,
        "zero_sown": int((sown == 0).sum()),
        "power_missing": int(power.isna().sum()) if len(power) else None,
        "verdict": code,
        "verdict_text": text,
        "_share_a": share_a,
        "_share_b": share_b,
        "_dom6": dom6,
        "_low_conf": low_conf,
    }


# --- output -------------------------------------------------------------------

def histogram(values, label, bins=20):
    v = pd.Series(values).dropna()
    rule(f"HISTOGRAM: {label}")
    if v.empty:
        print("no defined values -- nothing to plot")
        return
    edges = np.linspace(0, 1, bins + 1)
    counts, _ = np.histogram(v, bins=edges)
    widest = counts.max() if counts.max() else 1
    print(f"n = {len(v):,}   mean = {v.mean():.3f}   median = {v.median():.3f}   "
          f"sd = {v.std():.3f}")
    print(f"min = {v.min():.3f}   max = {v.max():.3f}\n")
    for i, c in enumerate(counts):
        bar = "#" * int(round(60 * c / widest))
        print(f"  [{edges[i]:.2f}-{edges[i+1]:.2f})  {c:>7,}  {bar}")
    print("\nquantiles:")
    print(v.quantile([0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95]).to_string())


def report_flags(df):
    """The shipped flags are constant in SHRUG 2.2. Report, drop nothing (§5)."""
    rule("DATA QUALITY FLAGS (§12 step 2)")
    n = len(df)
    out = {}
    for c in FLAG_COLS:
        if c not in df.columns:
            print(f"{c:<12} NOT PRESENT in file")
            continue
        s = pd.to_numeric(df[c], errors="coerce")
        distinct = int(s.nunique(dropna=True))
        out[c] = distinct
        print(f"{c:<12} distinct values: {distinct}   nulls: {s.isna().sum():,}")
        for v, k in list(s.value_counts(dropna=False).items())[:5]:
            print(f"{'':<12}   value {v!r}: {k:,} ({k / n:.2%})")
        if distinct <= 1:
            print(f"{'':<12}   -> DEGENERATE: carries no information. NOT used as a filter.")
    print(f"\nRows dropped on shipped flags: 0 of {n:,}")
    return out


def persist(df, state_code, m):
    """Append this state to a single table, keyed by state. Never clobbers others."""
    import duckdb
    OUT_DB.parent.mkdir(parents=True, exist_ok=True)
    work = df.assign(
        pc11_state_id=int(state_code),
        rainfed_share_a=m["_share_a"],
        rainfed_share_b=m["_share_b"],
        dominance_6cat=m["_dom6"],
        low_confidence=m["_low_conf"],
    )
    con = duckdb.connect(str(OUT_DB))
    con.register("work", work)
    con.execute("CREATE TABLE IF NOT EXISTS vd_working AS SELECT * FROM work WHERE 0=1")
    con.execute("DELETE FROM vd_working WHERE pc11_state_id = ?", [int(state_code)])
    con.execute("INSERT INTO vd_working SELECT * FROM work")
    n = con.execute("SELECT count(*) FROM vd_working").fetchone()[0]
    states = con.execute(
        "SELECT count(DISTINCT pc11_state_id) FROM vd_working").fetchone()[0]
    con.close()
    print(f"\nPersisted: {OUT_DB}  table vd_working now holds {n:,} rows "
          f"across {states} state(s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dta", type=Path, default=DEFAULT_DTA)
    ap.add_argument("--state", type=int, default=PC11_STATE_MP)
    args = ap.parse_args()

    rule("LOAD")
    df = read_vd(args.dta)
    print(f"File:                 {args.dta}")
    print(f"Raw rows (all-India): {len(df):,}")
    print("State column used:    shrid2 field[1] (no explicit state id present)")
    sub = df[state_code_series(df) == args.state].copy()
    print(f"Rows for state {args.state}:   {len(sub):,}")
    if sub.empty:
        sys.exit("State filter returned 0 rows. Stopping rather than reporting on nothing.")

    report_flags(sub)
    integrity_checks(sub)
    resid, _ = reconciliation(sub)
    m = measures(sub)

    print(f"\n  un_irr + src_irr == net_sown (within 0.01 ha): {m['identity_rate']:.2%}")
    print("  -> the two rainfed-share denominators below are the SAME quantity.")

    histogram(m["_share_a"], "rainfed share = un_irr / (un_irr + src_irr)")
    histogram(m["_share_b"], "rainfed share = un_irr / net_sown  [denominator check]")

    rule("DIVERSITY MEASURES")
    category_measures(sub, IRR_SOURCE_COLS, "5-source (original spec)")
    category_measures(sub, IRR_SOURCE_COLS + [COL_UN_IRR],
                      "6-category incl. unirrigated (PREFERRED)")

    c1 = m["component1"]
    rule("COMPONENT 1 VARIANCE (the §7 mechanism itself)")
    print(f"  rainfed share  sd = {c1['sd']:.3f}   IQR = {c1['iqr']:.3f}   "
          f"p5-p95 = {c1['p5']:.3f}-{c1['p95']:.3f}")
    print(f"  villages in extreme bins (<0.05 or >0.95): {c1['extreme_share']:.2%}")
    print(f"  band: {c1['band']}  (absent < {MECHANISM_ABSENT_MAX_IQR} "
          f"<= weak < {MECHANISM_WEAK_MAX_IQR} <= present)")
    if c1["band"] == "present":
        margin = c1["iqr"] - MECHANISM_WEAK_MAX_IQR
        print(f"  margin above the 'present' line: {margin:+.3f}")

    rule("THE FOUR NUMBERS")
    print(f"1. histogram spread (share_a):  sd = {c1['sd']:.3f}, IQR = {c1['iqr']:.3f}, "
          f"p5-p95 = {c1['p5']:.3f}-{c1['p95']:.3f}")
    r = m["reconciliation"]
    print(f"2. reconciliation residual:     median = {r['median']:.3f} ha, "
          f"exact-zero = {r['exact_zero_share']:.2%}, max|r| = {r['max_abs']:,.1f} ha")
    print("3. dropped by land_flag / dist_flag:  0 / 0  (both DEGENERATE, see §5)")
    print(f"4. villages with zero net sown area: {m['zero_sown']:,}")
    if m["power_missing"] is not None and len(sub):
        print(f"   [context] power hours missing:    {m['power_missing']:,} "
              f"({m['power_missing'] / len(sub):.2%})  -> low-confidence per §9.4")

    rule("VERDICT (§12)")
    print(f"*** {m['verdict']}: {m['verdict_text']}")
    persist(sub, args.state, m)


if __name__ == "__main__":
    main()

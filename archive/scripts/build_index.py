"""
Build the village-level structural basis-risk index (PROJECT_BRIEF.md §7).

SCOPE: all 35 states and UTs, keyed on shrid2, every row carrying its state
verdict. This is a licence obligation, not a preference -- SHRUG's bundled
README requires that data linked to the SHRUG be published with SHRUG
identifiers and that the all-India series be posted rather than just the
analysis sample. The 18-state v1 decision (§13) governs what the MAP RENDERS,
not what the dataset contains.

What this index IS: a map of STRUCTURAL DRIVERS of basis risk (§9.3). It is not
measured basis risk. Measuring that needs IU-level yields, which are not public.

No imputation. A village whose components cannot all be computed receives NO
index and an `index_status` saying why. Nothing is silently filled, and no
village is scored on a different component set from its neighbours.

Usage:  python scripts/build_index.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import killcheck_mp as kc  # noqa: E402

SWEEP_CSV = Path("data/interim/state_sweep.csv")
OUT_PARQUET = Path("data/processed/basis_risk_index.parquet")
OUT_DB = Path("data/processed/basis_risk.duckdb")
OUT_SUMMARY = Path("data/processed/index_summary.csv")

PILOT_STATES = (8, 24, 23)          # map/UI pilot: Rajasthan, Gujarat, MP
V1_VERDICT = "PASSES"               # verdict that earns map coverage

COL_TUBEWELL = "pc11_vd_land_wl_tw_irr"

# --- weights ------------------------------------------------------------------
# Justified by the §7 component ordering, NOT tuned to the output. Half to the
# mechanism; the remainder split preserving c2 and c4's original 0.25 : 0.10
# ratio to within rounding.
#
# c3 (agricultural power hours) WAS BUILT AND DELIBERATELY DROPPED -- see §7.
# It is still computed and stored as c3_power_constraint_dropped, and the
# "restore_c3" sensitivity row quantifies the decision. Do not re-add it.
WEIGHTS = {
    "c1_split_heterogeneity": 0.50,
    "c2_source_mix": 0.35,
    "c4_unit_size": 0.15,
}

C3_DROPPED = "c3_power_constraint_dropped"

ALTERNATIVES = {
    "equal": {"c1_split_heterogeneity": 1 / 3, "c2_source_mix": 1 / 3,
              "c4_unit_size": 1 / 3},
    "mechanism_only": {"c1_split_heterogeneity": 1.0, "c2_source_mix": 0.0,
                       "c4_unit_size": 0.0},
    "mechanism_heavy": {"c1_split_heterogeneity": 0.70, "c2_source_mix": 0.20,
                        "c4_unit_size": 0.10},
    "drop_size": {"c1_split_heterogeneity": 0.60, "c2_source_mix": 0.40,
                  "c4_unit_size": 0.0},
    "restore_c3": {"c1_split_heterogeneity": 0.50, "c2_source_mix": 0.25,
                   C3_DROPPED: 0.15, "c4_unit_size": 0.10},
}

SUBDIVISION_DECILE = 0.90  # §9.6: top decile within state. Relative, not absolute.


def state_verdicts():
    if not SWEEP_CSV.exists():
        sys.exit(f"MISSING {SWEEP_CSV}. Run scripts/sweep_states.py first.")
    s = pd.read_csv(SWEEP_CSV)
    s["code"] = s["code"].astype(int)
    return s.set_index("code")[["state", "verdict", "recon_fail_share", "rainfed_iqr"]]


def components(df, c2_invalid_states):
    """The three §7 components, each on [0, 1], NaN where not computable."""
    src = df[kc.IRR_SOURCE_COLS].apply(pd.to_numeric, errors="coerce")
    src = src.mask(src < 0)
    src_sum = src.sum(axis=1, min_count=1)
    area = pd.to_numeric(df[kc.COL_AREA], errors="coerce")
    power = pd.to_numeric(df[kc.COL_POWER_SUM], errors="coerce")

    out = pd.DataFrame(index=df.index)

    # c1 -- the mechanism. Rainfed share p, then normalised Bernoulli variance
    # 4p(1-p): 0 when a village is wholly irrigated or wholly rainfed, 1 at an
    # even split. This is the variance of irrigation status across a randomly
    # chosen unit of the village's sown area, scaled to [0, 1].
    #
    # The denominator uses the AGGREGATE irrigated column, not the sum of the
    # five source columns. That matters: in states failing the reconciliation
    # gate the five columns are incomplete, so a five-column denominator would
    # silently corrupt the mechanism itself. An earlier version of this script
    # used the five-column sum -- numerically identical in the 18 reconciling
    # states, wrong everywhere else.
    p, _, _ = kc.rainfed_shares(df)
    out["rainfed_share"] = p
    out["c1_split_heterogeneity"] = 4.0 * p * (1.0 - p)

    # c2 -- source mix among IRRIGATED area only, Shannon normalised by ln(5).
    # Wholly rainfed villages have no irrigation to mix: the component is 0 by
    # definition, not missing.
    shares = src.div(src_sum, axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        h = -(shares * np.log(shares.where(shares > 0))).sum(axis=1, min_count=1)
    mix = h / np.log(len(kc.IRR_SOURCE_COLS))
    mix = mix.where(src_sum > 0, 0.0)
    mix = mix.where(src_sum.notna())
    # Gate: where the source breakdown does not reconcile with the aggregate,
    # this component is computed on an incomplete denominator and is withheld.
    mix = mix.mask(df["pc11_state_id"].isin(c2_invalid_states))
    out["c2_source_mix"] = mix

    # c3 -- DROPPED FROM THE INDEX. Computed and stored so the drop stays
    # auditable. Not a component; do not add it to WEIGHTS.
    tw_share = np.where(src_sum > 0,
                        pd.to_numeric(df[COL_TUBEWELL], errors="coerce") / src_sum,
                        0.0)
    out[C3_DROPPED] = tw_share * (1.0 - power.clip(lower=0, upper=24) / 24.0)

    # c4 -- unit size. Larger unit, more scope for internal variation. Ranked
    # WITHIN state, so this never smuggles a cross-state level difference in.
    out["_area"] = area
    out["c4_unit_size"] = (
        out.groupby(df["pc11_state_id"])["_area"].rank(pct=True, na_option="keep")
    )
    out = out.drop(columns=["_area"])

    out["power_hours_summer"] = power
    out["tubewell_share"] = tw_share
    return out


def composite(comp, weights):
    """Weighted sum. ALL weighted components required -- no renormalisation.

    §7 makes c1 the mechanism, so a score without it is a different quantity
    rather than a weaker one. The same argument applies to any missing
    component: a village scored on two components and its neighbour scored on
    three are not comparable, and the index's whole purpose is comparing
    neighbours. So a village either has every component or it has no index.
    """
    cols = [c for c, w in weights.items() if w != 0]
    vals = comp[cols]
    w = np.array([weights[c] for c in cols], dtype="float64")
    score = (vals.to_numpy() * w).sum(axis=1) / w.sum()
    complete = vals.notna().all(axis=1).to_numpy()
    return pd.Series(np.where(complete, score, np.nan), index=vals.index), complete


def main():
    verdicts = state_verdicts()
    c2_invalid = set(
        verdicts.index[verdicts["recon_fail_share"].fillna(1.0) > kc.RECON_FAIL_MAX]
    )
    v1_states = set(verdicts.index[verdicts["verdict"] == V1_VERDICT])
    print(f"states/UTs in sweep:        {len(verdicts)}")
    print(f"v1 map coverage (PASSES):   {len(v1_states)}")
    print(f"c2 withheld (recon gate):   {sorted(c2_invalid)}")

    print("\nLoading Village Directory (one pass)...")
    df = kc.read_vd(kc.DEFAULT_DTA)
    df["pc11_state_id"] = kc.state_code_series(df).astype("Int64")
    print(f"rows: {len(df):,}  (ALL states -- published series, §9.7 licence)")

    comp = components(df, c2_invalid)
    base, complete = composite(comp, WEIGHTS)

    out = pd.DataFrame({
        "shrid2": df["shrid2"].values,
        "pc11_state_id": df["pc11_state_id"].astype(int).values,
    })
    for c in ("rainfed_share", "c1_split_heterogeneity", "c2_source_mix",
              "c4_unit_size", C3_DROPPED, "power_hours_summer", "tubewell_share"):
        out[c] = comp[c].values
    out["basis_risk_index"] = base.values

    out["state_verdict"] = out["pc11_state_id"].map(verdicts["verdict"]).fillna("UNKNOWN")
    out["in_v1_coverage"] = out["pc11_state_id"].isin(v1_states)

    # --- why a village has no index ------------------------------------------
    status = pd.Series("issued", index=out.index, dtype="object")
    status[out["c2_source_mix"].isna() & out["pc11_state_id"].isin(c2_invalid)] = \
        "withheld_source_data_unreliable"
    status[out["c1_split_heterogeneity"].isna()] = "no_mechanism_component"
    all_zero = (
        out["rainfed_share"].isna() & out["c1_split_heterogeneity"].isna()
        & (out["state_verdict"] == "NO DATA")
    )
    status[all_zero] = "no_land_use_data"
    status[out["basis_risk_index"].notna()] = "issued"
    out["index_status"] = status

    # --- confidence (§9.4) ---------------------------------------------------
    integ, low_conf = kc.integrity_checks(df, verbose=False)
    out["integrity_fail"] = low_conf
    out["power_missing"] = comp["power_hours_summer"].isna().values
    out["low_confidence"] = out["integrity_fail"] | out["basis_risk_index"].isna()

    # --- percentile and the §9.6 subdivision cut -----------------------------
    out["index_pct_within_state"] = (
        out.groupby("pc11_state_id")["basis_risk_index"].rank(pct=True, na_option="keep")
    )
    # The subdivision flag is a RECOMMENDATION, so it is issued only where the
    # index is endorsed. Outside v1 the value is published but not flagged.
    out["subdivision_priority"] = np.where(
        out["in_v1_coverage"] & out["index_pct_within_state"].notna(),
        out["index_pct_within_state"] >= SUBDIVISION_DECILE,
        np.nan,
    )

    print("\n=== PUBLISHED SERIES: all states ===")
    print(f"  rows:                    {len(out):,}")
    print(f"  index issued:            {out['basis_risk_index'].notna().sum():,} "
          f"({out['basis_risk_index'].notna().mean():.2%})")
    print("\n  index_status:")
    for k, v in out["index_status"].value_counts().items():
        print(f"    {k:<34} {v:>8,}  ({v / len(out):6.2%})")
    print("\n  by state verdict:")
    g0 = out.groupby("state_verdict").agg(
        villages=("shrid2", "size"), issued=("basis_risk_index", "count"))
    g0["issued_share"] = g0["issued"] / g0["villages"]
    print(g0.sort_values("villages", ascending=False).to_string())

    v1 = out[out["in_v1_coverage"]]
    print(f"\n=== v1 MAP SUBSET ({len(v1_states)} states) ===")
    print(f"  villages:                {len(v1):,}")
    print(f"  index issued:            {v1['basis_risk_index'].notna().sum():,} "
          f"({v1['basis_risk_index'].notna().mean():.2%})")
    print(f"  low confidence:          {v1['low_confidence'].sum():,} "
          f"({v1['low_confidence'].mean():.2%})")
    print(f"  subdivision priority:    {int(v1['subdivision_priority'].sum()):,}")

    # --- sensitivity (§7, §10.4), computed on the v1 subset ------------------
    print("\n=== SENSITIVITY on v1 subset: rank correlation vs stated weighting ===")
    cv = comp.loc[df["pc11_state_id"].isin(v1_states).values]
    base_v1 = v1["basis_risk_index"].reset_index(drop=True)
    state_v1 = v1["pc11_state_id"].reset_index(drop=True)
    top_v1 = (v1["index_pct_within_state"].reset_index(drop=True) >= SUBDIVISION_DECILE)
    sens = []
    for name, w in ALTERNATIVES.items():
        alt, _ = composite(cv, w)
        alt = alt.reset_index(drop=True)
        rho = base_v1.corr(alt, method="spearman")
        alt_pct = alt.groupby(state_v1).rank(pct=True, na_option="keep")
        churn = (top_v1 != (alt_pct >= SUBDIVISION_DECILE)).mean()
        sens.append({"scheme": name, "spearman": rho, "decile_churn": churn})
        print(f"  {name:<16} rho = {rho:.4f}   decile churn = {churn:.2%}")

    # --- per-state summary ---------------------------------------------------
    g = out.groupby("pc11_state_id").agg(
        villages=("basis_risk_index", "size"),
        index_issued=("basis_risk_index", "count"),
        index_mean=("basis_risk_index", "mean"),
        low_confidence=("low_confidence", "sum"),
        subdivision_priority=("subdivision_priority", "sum"),
    ).reset_index()
    g["low_conf_share"] = g["low_confidence"] / g["villages"]
    g = g.join(verdicts, on="pc11_state_id")
    g = g.sort_values(["verdict", "index_mean"], ascending=[True, False])
    g.to_csv(OUT_SUMMARY, index=False)

    OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT_PARQUET, index=False)
    pd.DataFrame(sens).to_csv(OUT_SUMMARY.with_name("index_sensitivity.csv"), index=False)

    import duckdb
    con = duckdb.connect(str(OUT_DB))
    con.register("idx", out)
    con.execute("CREATE OR REPLACE TABLE basis_risk_index AS SELECT * FROM idx")
    con.execute("CREATE OR REPLACE TABLE v1 AS SELECT * FROM idx WHERE in_v1_coverage")
    con.execute("CREATE OR REPLACE TABLE pilot AS SELECT * FROM idx WHERE "
                f"pc11_state_id IN ({','.join(str(s) for s in PILOT_STATES)})")
    n1, n2 = (con.execute("SELECT count(*) FROM v1").fetchone()[0],
              con.execute("SELECT count(*) FROM pilot").fetchone()[0])
    con.close()

    print("\nWritten:")
    print(f"  {OUT_PARQUET}  ({len(out):,} villages, all 35 states)")
    print(f"  {OUT_DB}  tables: basis_risk_index ({len(out):,}), "
          f"v1 ({n1:,}), pilot ({n2:,})")
    print(f"  {OUT_SUMMARY}")


if __name__ == "__main__":
    main()

"""
Gujarat Reservoir Watch — coverage report and the same-calendar-date deviation.

Headline question: for each scheme, how does today's % filling compare with the
mean % filling on the SAME CALENDAR DATE in previous years?

Language rules, enforced here and not negotiable:
  * Days of water renders as "no current release" where canal release is zero —
    never a number, never infinity.
  * Every days-of-water figure is labelled "at today's release rate".
  * Nothing in this output forecasts anything. A deviation is a comparison of
    two observations. It is not a prediction, and the wording never implies one.

Usage:  python scripts/report_deviation.py [--date 2026-09-11] [--top 20]
"""

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

DB = Path("data/processed/reservoir.duckdb")
# Written by check_capacity.py: one capacity per scheme-season, plus the class
# that decides whether the season may enter a baseline at all.
SEASON_CAP = Path("data/processed/season_capacity.csv")
IN_SEASON_MONTHS = (6, 7, 8, 9, 10)

# Option 0 — capacity tolerance. Two design values are the same capacity when
# they differ by less than this. Five of the twenty flagged schemes differed by
# under 0.35%, which is rounding in the source, not a restatement.
EPS_REL = 0.005

# Option C — like-for-like comparison. A prior year enters a scheme's baseline
# only if that year's design capacity matches today's within EPS_REL. Nothing is
# rebased and no historical figure is altered: every value stays exactly as the
# source published it. The cost is a shorter baseline for restated schemes, and
# that cost is shown per row (`yrs`) rather than hidden.
#
# Rejected alternatives, recorded so the choice stays auditable:
#   B  rebasing history onto one canonical denominator — comparable by
#      construction, but our historical % would stop matching the published
#      report. Held in reserve as a clearly-labelled derived column only.
#   D  level-based (PWL/FRL) — viable as a periodic cross-check since only 4
#      schemes have a restated FRL, but level-to-volume is non-linear so it is a
#      different quantity, never a headline metric.
#   E  excluding restated schemes from the ranking — would drop real signal, and
#      with option 0 applied most of the flags turn out to be rounding anyway.
#
# Capacity is compared at SEASON level, not row level. A season can publish more
# than one design value, and which one is "the" capacity is a decision taken once
# in check_capacity.py rather than re-derived from whichever row a query happens
# to land on:
#   * clean           — one value all season.
#   * edge_exception  — the water-year edit landed on the season's first day or
#                       two. Season capacity is the majority value (152 of 153
#                       days); the minority day is recorded, not used.
#   * mid_season      — a genuine change inside the season. EXCLUDED from any
#                       baseline, because it cannot be resolved by redefining a
#                       season, and correcting it silently would be worse.
# Excluded scheme-seasons are counted and named in the output, never dropped
# quietly. See brief §14.


def rule(t):
    print(f"\n{'=' * 96}\n{t}\n{'=' * 96}")


def fmt_days(v):
    """Zero release is undefined, not a large number (§8.5)."""
    if v is None or pd.isna(v):
        return "no current release"
    return f"{v:,.1f} d"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-09-11")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--season-cap", default=None,
                    help=f"override {SEASON_CAP}, e.g. a scratch copy when "
                         "validating against partial coverage.")
    args = ap.parse_args()
    con = duckdb.connect(str(DB), read_only=True)

    # ---------------- coverage -------------------------------------------------
    rule("COVERAGE")
    cov = con.execute("""
        SELECT min(report_date) AS first_date, max(report_date) AS last_date,
               count(DISTINCT report_date) AS dates_loaded,
               count(*) AS storage_rows
        FROM fact_storage
    """).df()
    print(cov.to_string(index=False))

    led = con.execute("""
        SELECT status, count(*) AS dates
        FROM ledger GROUP BY status ORDER BY dates DESC
    """).df()
    if not led.empty:
        print("\nledger status (attempted so far):")
        print(led.to_string(index=False))

    gaps = con.execute("""
        WITH span AS (
            SELECT min(report_date) a, max(report_date) b FROM fact_storage
        ),
        cal AS (
            SELECT UNNEST(generate_series(
                (SELECT a FROM span), (SELECT b FROM span), INTERVAL 1 DAY
            ))::DATE AS d
        )
        SELECT c.d AS missing_date,
               COALESCE(l.status, 'not attempted') AS ledger_status,
               l.note
        FROM cal c
        LEFT JOIN (SELECT DISTINCT report_date FROM fact_storage) f
               ON f.report_date = c.d
        LEFT JOIN ledger l ON l.report_date = c.d
        WHERE f.report_date IS NULL
        ORDER BY c.d
    """).df()
    print(f"\ndates inside the loaded span with NO storage rows: {len(gaps)}")
    if len(gaps):
        print(gaps.head(40).to_string(index=False))
        if len(gaps) > 40:
            print(f"  ... and {len(gaps) - 40} more")

    odd = con.execute("""
        SELECT report_date, schemes, distinct_schemes, layout, taluka_null,
               round(state_gross_mcm, 2) AS state_gross_mcm, schemes_releasing
        FROM v_coverage
        WHERE schemes <> 206 OR distinct_schemes <> schemes
        ORDER BY report_date
    """).df()
    print(f"\ndates NOT at exactly 206 distinct schemes: {len(odd)}")
    if len(odd):
        print(odd.head(30).to_string(index=False))

    layouts = con.execute("""
        SELECT layout, count(DISTINCT report_date) AS dates,
               min(report_date) AS first_date, max(report_date) AS last_date,
               sum(CASE WHEN taluka_null > 0 THEN 1 ELSE 0 END) AS dates_missing_taluka
        FROM v_coverage GROUP BY layout ORDER BY layout
    """).df()
    print("\nsource layouts seen (23-col has no Taluka column):")
    print(layouts.to_string(index=False))

    # ---------------- scheme drift --------------------------------------------
    rule("SCHEME LIST DRIFT")
    span_raw = con.execute("SELECT min(report_date), max(report_date) "
                           "FROM fact_storage").fetchone()
    # DuckDB hands back date objects; the dataframe columns are datetime64.
    span = (pd.Timestamp(span_raw[0]), pd.Timestamp(span_raw[1]))
    drift = con.execute("""
        SELECT scheme_id, scheme_name_latest, district_latest, region_latest,
               first_seen, last_seen, days_present,
               n_name_variants, n_district_variants, n_design_variants
        FROM dim_scheme
        ORDER BY first_seen, scheme_id
    """).df()
    print(f"schemes ever seen: {len(drift)}")
    added = drift[drift["first_seen"] > span[0]]
    dropped = drift[drift["last_seen"] < span[1]]
    renamed = drift[drift["n_name_variants"] > 1]
    moved = drift[drift["n_district_variants"] > 1]
    recap = drift[drift["n_design_variants"] > 1]
    print(f"  appear after the first loaded date : {len(added)}")
    print(f"  absent from the last loaded date   : {len(dropped)}")
    print(f"  more than one name                 : {len(renamed)}")
    print(f"  more than one district             : {len(moved)}")
    print(f"  more than one design capacity      : {len(recap)}")
    for label, sub in (("ADDED", added), ("DROPPED", dropped),
                       ("RENAMED", renamed), ("DISTRICT CHANGED", moved),
                       ("DESIGN CAPACITY CHANGED", recap)):
        if len(sub):
            print(f"\n  {label}:")
            print(sub.head(25).to_string(index=False))

    # ---------------- the headline query -------------------------------------
    rule(f"SAME-CALENDAR-DATE DEVIATION — {args.date} vs the mean of prior years")
    season_cap_path = Path(args.season_cap) if args.season_cap else SEASON_CAP
    if not season_cap_path.exists():
        sys.exit(f"MISSING {season_cap_path}. Run check_capacity.py first — the "
                 "baseline needs one capacity per scheme-season, and deciding "
                 "which\nvalue that is belongs there, not here.")
    today_month = int(args.date[5:7])
    if today_month not in IN_SEASON_MONTHS:
        sys.exit(f"--date {args.date} is outside Jun 1 - Oct 31. The season "
                 "baseline is undefined for off-season dates; see "
                 "check_capacity.py.")
    today_season = int(args.date[:4])
    scap = pd.read_csv(season_cap_path)[["scheme_id", "season", "capacity_class",
                                         "season_capacity_mcm"]]
    con.register("season_cap", scap)

    today = con.execute("""
        SELECT f.scheme_id, f.scheme_name, f.district, f.region,
               f.present_gross_mcm, f.design_gross_mcm, f.present_live_mcm,
               f.pct_filling, f.outflow_canal_cusecs, f.days_water_at_release,
               f.warning, f.crf,
               sc.season_capacity_mcm AS cap_season_today,
               sc.capacity_class      AS class_today
        FROM fact_storage f
        LEFT JOIN season_cap sc ON sc.scheme_id = f.scheme_id
                               AND sc.season = ?
        WHERE f.report_date = ?::DATE
    """, [today_season, args.date]).df()

    ann = con.execute("""
        SELECT f.scheme_id, f.report_date, f.pct_filling,
               EXTRACT(year FROM f.report_date)::INT AS season,
               sc.season_capacity_mcm AS cap_season_prior,
               sc.capacity_class
        FROM fact_storage f
        LEFT JOIN season_cap sc ON sc.scheme_id = f.scheme_id
                               AND sc.season = EXTRACT(year FROM f.report_date)
        WHERE EXTRACT(month FROM f.report_date) = EXTRACT(month FROM ?::DATE)
          AND EXTRACT(day   FROM f.report_date) = EXTRACT(day   FROM ?::DATE)
          AND f.report_date < ?::DATE
          AND f.pct_filling IS NOT NULL
    """, [args.date] * 3).df()

    if today.empty or ann.empty:
        print("No anniversary matches loaded yet — cannot compute the comparison.")
        con.close()
        return

    ann = ann.merge(today[["scheme_id", "cap_season_today"]], on="scheme_id")
    ann["is_mid_season"] = ann["capacity_class"] == "mid_season"
    ratio = ((ann["cap_season_prior"] - ann["cap_season_today"]).abs()
             / ann["cap_season_today"].replace(0, pd.NA))
    ann["capacity_matches"] = ratio <= EPS_REL
    ann["comparable"] = (ann["capacity_matches"].fillna(False)
                         & ~ann["is_mid_season"]
                         & ann["cap_season_prior"].notna()
                         & ann["cap_season_today"].notna())

    def years(s):
        return sorted(int(x) for x in s)

    base = ann[ann["comparable"]].groupby("scheme_id").agg(
        mean_pct_prior=("pct_filling", "mean"),
        min_pct_prior=("pct_filling", "min"),
        max_pct_prior=("pct_filling", "max"),
        n_prior_years=("pct_filling", "size"),
        years_used=("season", years)).reset_index()

    drop_cap = ann[~ann["comparable"] & ~ann["is_mid_season"]] \
        .groupby("scheme_id").agg(
            yrs_dropped_capacity=("season", "size"),
            years_dropped=("season", years)).reset_index()
    drop_mid = ann[ann["is_mid_season"]].groupby("scheme_id").agg(
        yrs_dropped_midseason=("season", "size"),
        years_dropped_midseason=("season", years)).reset_index()

    dsc = con.execute("SELECT scheme_id, n_design_variants FROM dim_scheme").df()
    dev = (today.merge(base, on="scheme_id")
                .merge(dsc, on="scheme_id", how="left")
                .merge(drop_cap, on="scheme_id", how="left")
                .merge(drop_mid, on="scheme_id", how="left"))
    if dev.empty:
        print("No comparable prior anniversary for any scheme — "
              "cannot compute the comparison.")
        con.close()
        return
    for c in ("yrs_dropped_capacity", "yrs_dropped_midseason"):
        dev[c] = dev[c].fillna(0).astype(int)
    dev["design_restated"] = dev["n_design_variants"] > 1
    dev = dev.rename(columns={"pct_filling": "pct_today"})
    dev["deviation_pp"] = dev["pct_today"] - dev["mean_pct_prior"]
    dev = dev.sort_values("deviation_pp").reset_index(drop=True)

    # Schemes with NO comparable prior year are reported, never silently lost.
    orphan_ids = sorted(set(ann["scheme_id"])
                        - set(ann.loc[ann["comparable"], "scheme_id"]))
    orphan = ann[ann["scheme_id"].isin(orphan_ids)].groupby("scheme_id").agg(
        prior_anniversaries=("season", "size"),
        incomparable_years=("season", years),
        n_midseason=("is_mid_season", "sum")).reset_index()
    if len(orphan):
        orphan = orphan.merge(today[["scheme_id", "scheme_name", "district"]],
                              on="scheme_id", how="left")

    print("baseline rule: option 0 (capacity tolerance "
          f"{EPS_REL:.2%}) + option C (like-for-like capacity only), compared at "
          "SEASON level.\nNo historical figure is rebased.")
    print(f"schemes with >=1 comparable prior anniversary: {len(dev)}")
    print(f"prior years available: "
          f"{sorted({int(y) for row in dev['years_used'] for y in row})}")
    print(f"schemes per comparable-prior-year count: "
          f"{dev['n_prior_years'].value_counts().sort_index().to_dict()}")

    ndrop = int((dev["yrs_dropped_capacity"] > 0).sum())
    print(f"\nschemes with >=1 prior year dropped, capacity mismatch: {ndrop}")
    if ndrop:
        print(dev[dev["yrs_dropped_capacity"] > 0][
            ["scheme_id", "scheme_name", "district", "n_prior_years",
             "yrs_dropped_capacity", "years_dropped"]].to_string(index=False))

    nmid = int((dev["yrs_dropped_midseason"] > 0).sum())
    print(f"\nschemes with >=1 prior year dropped, MID-SEASON capacity change: "
          f"{nmid}")
    if nmid:
        print("(the season published two capacities and neither can stand for "
              "it — brief §14)")
        print(dev[dev["yrs_dropped_midseason"] > 0][
            ["scheme_id", "scheme_name", "district", "n_prior_years",
             "yrs_dropped_midseason", "years_dropped_midseason"]]
            .to_string(index=False))

    print(f"\nschemes with NO comparable prior year (not ranked): {len(orphan)}")
    if len(orphan):
        print(orphan.to_string(index=False))

    out = dev.head(args.top).copy()
    out["days_water"] = out["days_water_at_release"].map(fmt_days)
    out["cap_note"] = out["design_restated"].map({True: "restated", False: ""})
    show = out[["scheme_id", "scheme_name", "district", "region", "pct_today",
                "mean_pct_prior", "deviation_pp", "n_prior_years",
                "present_live_mcm", "design_gross_mcm", "days_water", "cap_note"]]
    show = show.rename(columns={
        "pct_today": "pct_now", "mean_pct_prior": "mean_prior",
        "deviation_pp": "dev_pp", "n_prior_years": "yrs",
        "present_live_mcm": "live_mcm", "design_gross_mcm": "design_mcm",
        "days_water": "days_at_today_rate", "cap_note": "capacity"})
    print(f"\nTHE {args.top} LARGEST NEGATIVE DEVIATIONS")
    print("(percentage points below the same-date mean of prior years; "
          "days-of-water is at today's release rate)")
    with pd.option_context("display.width", 220, "display.max_columns", 30,
                           "display.float_format", lambda v: f"{v:,.1f}"):
        print(show.to_string(index=False))
    n_rest = int(out["design_restated"].sum())
    if n_rest:
        print(f"\n  NOTE: {n_rest} of the top {args.top} had their DESIGN capacity "
              "restated between years, so their % filling is not strictly "
              "comparable across those years. Flagged 'restated' above.")

    # Percentage-point ranking favours small reservoirs that were full before and
    # are empty now. The volume view answers a different, equally fair question.
    dev["shortfall_mcm"] = (dev["mean_pct_prior"] - dev["pct_today"]) / 100.0 \
        * dev["design_gross_mcm"]
    vol = dev.nlargest(10, "shortfall_mcm")[
        ["scheme_id", "scheme_name", "district", "region", "design_gross_mcm",
         "pct_today", "mean_pct_prior", "deviation_pp", "shortfall_mcm"]]
    print("\nFOR CONTRAST — the same comparison weighted by volume "
          "(implied shortfall in MCM against the prior same-date mean):")
    with pd.option_context("display.width", 220,
                           "display.float_format", lambda v: f"{v:,.1f}"):
        print(vol.to_string(index=False))

    print("\nstate position on this date vs prior same-date mean:")
    st = con.execute("""
        SELECT EXTRACT(year FROM report_date) AS yr,
               round(100.0 * sum(present_gross_mcm) / sum(design_gross_mcm), 2)
                   AS state_pct_filling,
               round(sum(present_gross_mcm), 1) AS gross_mcm,
               count(*) AS schemes
        FROM fact_storage
        WHERE EXTRACT(month FROM report_date) = EXTRACT(month FROM ?::DATE)
          AND EXTRACT(day   FROM report_date) = EXTRACT(day   FROM ?::DATE)
        GROUP BY 1 ORDER BY 1
    """, [args.date, args.date]).df()
    print(st.to_string(index=False))

    rel = con.execute("""
        SELECT count(*) AS schemes,
               sum(CASE WHEN outflow_canal_cusecs > 0 THEN 1 ELSE 0 END) AS releasing,
               sum(CASE WHEN outflow_canal_cusecs > 0 THEN 0 ELSE 1 END) AS no_release
        FROM fact_storage WHERE report_date = ?::DATE
    """, [args.date]).df()
    print(f"\ndays-of-water reporting on {args.date}: "
          f"{int(rel['releasing'].iloc[0])} schemes have a current canal release; "
          f"{int(rel['no_release'].iloc[0])} render as \"no current release\".")
    print("Every days-of-water figure above is at today's release rate. "
          "These are comparisons of observations, not forecasts.")
    con.close()


if __name__ == "__main__":
    main()

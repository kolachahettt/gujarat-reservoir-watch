"""
Gujarat Reservoir Watch — capacity gate, season capacities, restatement table.

THIS RUNS BEFORE THE ANALYSIS AND IT CAN STILL HALT THE PIPELINE.

Three jobs:

1. **Season capacity per scheme.** Option C needs one design capacity per
   scheme-season to compare like with like. Establishing it is not a lookup,
   because a season can carry more than one published value (see below).

2. **The gate.** It halts on an *unacknowledged* mid-season capacity change.
   The known three are handled by the agreed rule; a fourth is news and stops
   everything, which is the point.

3. **The restatement table.** A systematic, mixed-direction revision in which
   the state total barely moves is a finding in its own right: it silently
   breaks any historical % filling comparison made by anyone who did not check.

----------------------------------------------------------------------------
Why only Jun 1 - Oct 31 dates count towards a season
----------------------------------------------------------------------------
`pin_changeover.py` caches the off-season dates it bisects, and
`parse_cached.py` parses every cached PDF, so March-May dates reach
`fact_storage`. They must not be grouped into a season: a March-May date
carries the PREVIOUS water year's capacity, so under calendar-year grouping the
water-year restatement (brief §11) appears as a within-season change. On the
2026-09-11 run that produced 11 of 20 false gate breaches.

The obvious fix — relabel by water year — does not work, because **the boundary
is not a fixed date**: the 2023->2024 edit landed on 1-2 June 2024, the
2024->2025 edit on 31 May 2025. Any fixed cutoff misclassifies one of them. So
off-season rows are excluded from season grouping rather than relabelled. They
stay in the database and on disk: they are real reports, they are what pinned
the changeovers, and nothing is deleted.

----------------------------------------------------------------------------
The two agreed rules for a season with more than one value
----------------------------------------------------------------------------
* **Edge exception** — the minority value is confined to a run of at most
  EDGE_MAX_DAYS at the very start of the season. This is the water-year edit
  landing one day inside the range: the 2023->2024 restatement was a two-day
  edit across 1-2 June 2024, so for schemes that flipped on the 2nd, 1 June
  still carries the old value and the other 152 days carry the new one. The
  season capacity is the majority value and the exception is recorded. Affects
  Rudramata, Kaila, Suvi, Fatehgadh, Jhuj, Kelia in 2024.

* **Mid-season change** — anything else. Cannot be resolved by redefining
  anything, so the scheme-season is **excluded from the like-for-like baseline**
  and published in its own table with the change window. Known cases:
  Shetrunji 2024, Jhuj 2025, Kelia 2025 — each begins its season at a value
  matching neither the season before nor after, then reverts mid-season.

Tolerance (option 0): two design values are the same capacity when they differ
by less than EPS_REL. On the 15-date sample five of twenty flagged schemes
differed by under 0.35% — pure rounding in the source.

Usage:
  python scripts/check_capacity.py
  python scripts/check_capacity.py --eps 0.005 --edge-max-days 2
Exit codes: 0 = clear, 2 = UNACKNOWLEDGED mid-season change, do not rebuild.
"""

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

DB = Path("data/processed/reservoir.duckdb")
OUT = Path("data/processed/capacity_restatements.csv")
OUT_SEASON = Path("data/processed/season_capacity.csv")
OUT_MID = Path("data/processed/capacity_midseason_exceptions.csv")

EPS_REL = 0.005          # 0.5% — below this, treat as the same capacity
IN_SEASON_MONTHS = (6, 7, 8, 9, 10)

# The one tunable parameter of the edge rule, stated rather than hidden. The
# observed water-year edit spans at most two days (1-2 June 2024), so a minority
# run longer than that at the start of a season is not the boundary effect.
EDGE_MAX_DAYS = 2

# Mid-season changes already reviewed and dispositioned (brief §14). A case NOT
# in this set halts the pipeline, because the decision to exclude a scheme-season
# from the baseline is not one this script should take on its own.
ACKNOWLEDGED_MID_SEASON = {(76, 2024), (43, 2025), (44, 2025)}


def rule(t):
    print(f"\n{'=' * 92}\n{t}\n{'=' * 92}")


def canon_map(values, eps):
    """Cluster near-equal capacities onto one representative each.

    24.44 and 24.45 are the same capacity reported to different rounding; 24.84
    is not. Clusters on sorted order, so it needs no assumption about how many
    distinct values a scheme has.
    """
    out, cluster = {}, []
    for v in sorted(values):
        if cluster and v and abs(v - cluster[0]) / abs(v) > eps:
            rep = max(cluster, key=lambda x: (cluster.count(x), x))
            out.update({c: rep for c in cluster})
            cluster = []
        cluster.append(v)
    if cluster:
        rep = max(cluster, key=lambda x: (cluster.count(x), x))
        out.update({c: rep for c in cluster})
    return out


def classify_season(g, eps, edge_max_days):
    """One scheme-season -> (class, season_capacity, detail dict).

    g is sorted by report_date and holds only in-season dates.
    """
    cmap = canon_map(g["design_gross_mcm"].dropna().unique(), eps)
    cap = g["design_gross_mcm"].map(cmap)
    dates = list(g["report_date"])

    # contiguous runs of one canonical capacity
    runs = []
    for d, c in zip(dates, cap):
        if runs and runs[-1][2] == c:
            runs[-1][1] = d
            runs[-1][3] += 1
        else:
            runs.append([d, d, c, 1])

    by_value = cap.value_counts()
    majority = by_value.index[0]
    n_days = len(cap)
    n_major = int(by_value.iloc[0])
    n_minor = n_days - n_major

    detail = {
        "n_dates": n_days, "n_values": int(cap.nunique()),
        "n_days_majority": n_major, "n_days_minority": n_minor,
        "runs": "; ".join(f"{c:g}@{a.date()}..{b.date()}({n})"
                          for a, b, c, n in runs),
        "values": "/".join(f"{v:g}" for v in sorted(by_value.index)),
        "first_date": dates[0].date(), "last_date": dates[-1].date(),
        "change_dates": ",".join(str(r[0].date()) for r in runs[1:]),
    }

    if cap.nunique() <= 1:
        return "clean", float(majority), detail

    # Edge exception: every non-majority day sits in ONE run that starts on the
    # season's first loaded date and is no longer than edge_max_days.
    #
    # n_major > n_minor is required explicitly rather than assumed: on a tie
    # value_counts picks arbitrarily, and "the majority value" would then be
    # whichever way the sort fell. A tie cannot be resolved by this rule, so it
    # falls through to mid_season and gets looked at.
    minor_runs = [r for r in runs if r[2] != majority]
    is_edge = (len(minor_runs) == 1
               and minor_runs[0][0] == dates[0]
               and minor_runs[0][3] <= edge_max_days
               and minor_runs[0][3] == n_minor
               and n_major > n_minor)
    if is_edge:
        return "edge_exception", float(majority), detail
    return "mid_season", None, detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eps", type=float, default=EPS_REL)
    ap.add_argument("--edge-max-days", type=int, default=EDGE_MAX_DAYS)
    ap.add_argument("--dry-run", action="store_true",
                    help="classify and report, write no files. For checking the "
                         "rules against partial coverage without leaving "
                         "provisional outputs that look authoritative.")
    ap.add_argument("--out-dir", default=None,
                    help="redirect the CSV outputs, e.g. to a scratch dir when "
                         "validating against partial coverage.")
    args = ap.parse_args()

    def write(df, path, label=""):
        if args.out_dir:
            path = Path(args.out_dir) / path.name
        if args.dry_run:
            print(f"[dry-run] not written: {path} ({len(df)} rows){label}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)
        print(f"written: {path}{label}")
    if not DB.exists():
        sys.exit("No database. Run build_db.py first.")
    con = duckdb.connect(str(DB), read_only=True)

    span = con.execute("""
        SELECT min(report_date), max(report_date), count(DISTINCT report_date)
        FROM fact_storage
    """).fetchone()
    print(f"loaded span {span[0]} .. {span[1]}   dates {span[2]}")
    print(f"tolerance   : values within {args.eps:.3%} are the same capacity")
    print(f"edge rule   : a minority run of <= {args.edge_max_days} day(s) at the "
          f"start of a season is the water-year edit, not a mid-season change")

    months = ",".join(str(m) for m in IN_SEASON_MONTHS)
    cov = con.execute(f"""
        SELECT EXTRACT(year FROM report_date)::INT AS season,
               count(DISTINCT report_date) FILTER (
                   WHERE EXTRACT(month FROM report_date) IN ({months})) AS in_season,
               count(DISTINCT report_date) FILTER (
                   WHERE EXTRACT(month FROM report_date) NOT IN ({months})) AS off_season
        FROM fact_storage GROUP BY 1 ORDER BY 1
    """).df()
    print("\ndates per season (only in_season dates count towards a season):")
    print(cov.to_string(index=False))
    n_off = int(cov["off_season"].sum())
    if n_off:
        print(f"\n{n_off} off-season dates present (bisection probes). Excluded "
              f"from season grouping,\nkept in the database — see the module "
              f"docstring for why relabelling by water year does not work.")

    det = con.execute(f"""
        SELECT scheme_id, scheme_name, district, region, report_date,
               EXTRACT(year FROM report_date)::INT AS season,
               design_gross_mcm
        FROM fact_storage
        WHERE design_gross_mcm IS NOT NULL
          AND EXTRACT(month FROM report_date) IN ({months})
        ORDER BY scheme_id, report_date
    """).df()
    det["report_date"] = pd.to_datetime(det["report_date"])

    # ---------------- 1. SEASON CAPACITY + CLASSIFICATION --------------------
    rule("SEASON CAPACITY — one value per scheme-season, or none")
    rows = []
    for (sid, season), g in det.groupby(["scheme_id", "season"], sort=True):
        cls, cap, d = classify_season(g.sort_values("report_date"),
                                      args.eps, args.edge_max_days)
        rows.append({"scheme_id": int(sid), "season": int(season),
                     "scheme_name": g["scheme_name"].iloc[-1],
                     "district": g["district"].iloc[-1],
                     "region": g["region"].iloc[-1],
                     "capacity_class": cls, "season_capacity_mcm": cap, **d})
    seas = pd.DataFrame(rows)
    print(seas["capacity_class"].value_counts().to_string())

    edge = seas[seas["capacity_class"] == "edge_exception"]
    midi = seas[seas["capacity_class"] == "mid_season"]

    if len(edge):
        print(f"\nEDGE EXCEPTIONS — {len(edge)} scheme-season(s). Season capacity "
              f"is the majority value; the\nminority day is the water-year edit "
              f"landing inside the range and is recorded, not used:")
        with pd.option_context("display.width", 240):
            print(edge[["scheme_id", "scheme_name", "season",
                        "season_capacity_mcm", "n_days_majority",
                        "n_days_minority", "values", "change_dates"]]
                  .to_string(index=False))

    # ---------------- 2. THE GATE --------------------------------------------
    rule("GATE — mid-season capacity changes")
    if midi.empty:
        print("CLEAR — no scheme changes design capacity inside a season.")
    else:
        keys = set(zip(midi["scheme_id"], midi["season"]))
        known = keys & ACKNOWLEDGED_MID_SEASON
        novel = keys - ACKNOWLEDGED_MID_SEASON
        print(f"mid-season changes: {len(keys)}   "
              f"already reviewed: {len(known)}   NEW: {len(novel)}")
        with pd.option_context("display.width", 240):
            print("\n" + midi[["scheme_id", "scheme_name", "season", "n_dates",
                               "values", "change_dates", "runs"]]
                  .to_string(index=False))
        print("\nThese are excluded from the like-for-like baseline per the "
              "agreed rule (brief §14):\nthey cannot be resolved by redefining "
              "a season, so they are published with their\nchange window "
              "instead of being silently corrected.")
        write(midi, OUT_MID)

        if novel:
            print("\n" + "!" * 92)
            print("UNACKNOWLEDGED MID-SEASON CAPACITY CHANGE — STOPPING.")
            print("These scheme-seasons are not in ACKNOWLEDGED_MID_SEASON, so no")
            print("disposition has been agreed for them. Excluding a scheme-season")
            print("from the baseline is not a decision this script should take on")
            print("its own. Review, then add them to the set or change the rule.")
            print("!" * 92)
            for sid, se in sorted(novel):
                r = midi[(midi.scheme_id == sid) & (midi.season == se)].iloc[0]
                print(f"  scheme {sid:>4} {r['scheme_name']:<14} season {se}   "
                      f"values {r['values']}   changed {r['change_dates']}")
            con.close()
            sys.exit(2)

    print()
    write(seas, OUT_SEASON,
          f"  ({seas['season_capacity_mcm'].notna().sum()} usable scheme-seasons, "
          f"{seas['season_capacity_mcm'].isna().sum()} excluded)")

    # ---------------- 3. THE RESTATEMENT TABLE -------------------------------
    rule("CAPACITY RESTATEMENTS — a finding in its own right")
    # Per SEASON, not per distinct value. Grouping by value assumes the series
    # steps monotonically through time; it does not. Dantiwada alternates
    # between two capacities across seasons, and a value-grouped view of that
    # produces a meaningless negative changeover gap.
    vals = seas[seas["season_capacity_mcm"].notna()].copy()
    vals = vals.rename(columns={"season_capacity_mcm": "design_gross_mcm"})
    vals["season_first_date"] = pd.to_datetime(vals["first_date"])
    vals["season_last_date"] = pd.to_datetime(vals["last_date"])

    rows, shapes = [], []
    for sid, grp in vals.groupby("scheme_id"):
        g = grp.sort_values("season").reset_index(drop=True)
        caps = g["design_gross_mcm"].tolist()
        spread = (max(caps) - min(caps)) / min(caps) if min(caps) else 0
        if spread <= args.eps:
            continue                       # option 0: rounding, not a restatement

        transitions = []
        for i in range(1, len(g)):
            prev, cur = g.loc[i - 1], g.loc[i]
            ratio = (cur["design_gross_mcm"] - prev["design_gross_mcm"]) \
                / prev["design_gross_mcm"]
            if abs(ratio) <= args.eps:
                continue
            transitions.append((i, prev, cur, ratio))
        if not transitions:
            continue

        # Oscillating = a capacity is left and later returned to. That is
        # neither rounding nor a one-off restatement; it is the source being
        # internally inconsistent between seasons, and it is worth naming.
        rounded = [round(c, 4) for c in caps]
        distinct_in_order = [k for j, k in enumerate(rounded)
                             if j == 0 or k != rounded[j - 1]]
        oscillating = len(distinct_in_order) > len(set(distinct_in_order))
        shape = ("oscillating" if oscillating
                 else "single_step" if len(transitions) == 1 else "multi_step")
        shapes.append({"scheme_id": sid, "scheme_name": g.loc[0, "scheme_name"],
                       "shape": shape, "n_transitions": len(transitions),
                       "seasons": "/".join(str(s) for s in g["season"]),
                       "capacities": "/".join(f"{c:g}" for c in caps)})

        for i, prev, cur, ratio in transitions:
            rows.append({
                "scheme_id": sid, "scheme_name": cur["scheme_name"],
                "district": cur["district"], "region": cur["region"],
                "shape": shape,
                "season_before": int(prev["season"]),
                "season_after": int(cur["season"]),
                "design_before_mcm": prev["design_gross_mcm"],
                "design_after_mcm": cur["design_gross_mcm"],
                "change_mcm": cur["design_gross_mcm"] - prev["design_gross_mcm"],
                "change_pct": 100 * ratio,
                "direction": "up" if ratio > 0 else "down",
                "last_seen_before": prev["season_last_date"],
                "first_seen_after": cur["season_first_date"],
                "changeover_gap_days": (cur["season_first_date"]
                                        - prev["season_last_date"]).days,
            })

    rest = pd.DataFrame(rows)
    if rest.empty:
        print("No restatements above tolerance.")
        con.close()
        return

    rest = rest.sort_values("change_pct", key=abs, ascending=False)
    print(f"schemes restated above tolerance: {rest['scheme_id'].nunique()}   "
          f"restatement events: {len(rest)}")
    print(f"direction: {rest['direction'].value_counts().to_dict()}")
    print(f"net change across all restatements: "
          f"{rest['change_mcm'].sum():+,.2f} MCM   "
          f"gross movement: {rest['change_mcm'].abs().sum():,.2f} MCM")
    print("\n(the net barely moving while the gross is large is the signature of a "
          "correction exercise,\n not of physical capacity change — siltation "
          "would be uniformly downward)")

    with pd.option_context("display.width", 230, "display.max_columns", 30,
                           "display.float_format", lambda v: f"{v:,.2f}"):
        print("\n" + rest.to_string(index=False))

    shp = pd.DataFrame(shapes)
    print("\nshape of each restated scheme's capacity series:")
    print(shp["shape"].value_counts().to_string())
    osc = shp[shp["shape"] == "oscillating"]
    if len(osc):
        print("\nOSCILLATING — capacity is left and later returned to. Not a "
              "restatement and not rounding: the source disagrees with itself "
              "between seasons. Option C handles these correctly by keeping only "
              "the seasons matching today's value.")
        print(osc.to_string(index=False))

    rule("CHANGEOVER WINDOW")
    cg = rest[rest["changeover_gap_days"] > 0].groupby(
        ["season_before", "season_after", "last_seen_before", "first_seen_after",
         "changeover_gap_days"]).agg(schemes=("scheme_id", "nunique")).reset_index()
    cg = cg.sort_values(["schemes"], ascending=False)
    print(cg.to_string(index=False))

    gap = int(cg["changeover_gap_days"].min()) if len(cg) else None
    print(f"\nThe narrowest window is {gap} days wide.")
    print("""
LIMIT OF THIS DATA: the revision happens BETWEEN seasons, in the Nov-May
off-season, which the Jun-Oct download range deliberately excludes. The
changeover therefore cannot be pinned to a day from season data alone, however
many season dates are added — the evidence simply is not in the range.

To pin it exactly, binary-search the off-season gap: fetch the midpoint date
between the last October report and the first June report, see which capacity it
carries, and halve again. That is about 8 requests per boundary -- roughly five
minutes at the server's ~33 s per request, not a re-run of the backfill.""")

    # State total per date: does the revision net out?
    rule("DID THE STATE TOTAL MOVE?")
    st = con.execute(f"""
        SELECT EXTRACT(year FROM report_date)::INT AS season,
               count(DISTINCT report_date) AS dates,
               round(avg(total), 2) AS mean_state_design_mcm,
               round(min(total), 2) AS min_total, round(max(total), 2) AS max_total
        FROM (
            SELECT report_date, sum(design_gross_mcm) AS total
            FROM fact_storage
            WHERE EXTRACT(month FROM report_date) IN ({months})
            GROUP BY report_date
        ) GROUP BY 1 ORDER BY 1
    """).df()
    print(st.to_string(index=False))

    print()
    write(rest, OUT)
    con.close()


if __name__ == "__main__":
    main()

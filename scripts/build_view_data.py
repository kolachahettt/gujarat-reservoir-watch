"""
Build the JSON the default view reads. No network, no rendering.

Everything the interface shows is computed here so the HTML holds no analysis:
if a number is wrong it is wrong in one place, and the provenance travels with
it.

----------------------------------------------------------------------------
Aligned day-of-season, never absolute date
----------------------------------------------------------------------------
Day 1 is 1 June. Comparing 11 September 2026 with 11 September 2022 is the
point of the whole dataset, so the x-axis is day-of-season and the absolute
date is a tooltip detail. 2026 runs to day 103 (11 September); prior seasons
run to day 153 (31 October).

----------------------------------------------------------------------------
The denominator, and why it is not simply the published one
----------------------------------------------------------------------------
Regional % filling is sum(present gross) / sum(design gross). Taking design
gross per date — the obvious choice — puts visible steps in the prior-year
lines that are not water events:

  * Saurashtra 2024 jumps 68.97 MCM (2.66% of the region) on 3 September, when
    Shetrunji's published capacity changes mid-season.
  * Kutch 2024 jumps 7.03 MCM (2.16%) between day 1 and day 2, because the
    four Kachchh edge-exception schemes still carry the previous water year's
    capacity on 1 June only.

So the denominator is the **season capacity** from check_capacity.py: one value
per scheme-season, which is exactly what removes both artefacts. For the three
scheme-seasons that published two capacities and have no season value
(Shetrunji 2024, Jhuj 2025, Kelia 2025 — brief §14) the scheme's **2026**
capacity is used instead. That is the value every other season of those three
agrees on, so it is the least-assumption substitute, and it is the narrow,
labelled use of option B that was held in reserve rather than a rebasing of
everything.

Scheme composition is held constant across seasons: all 206 in every season,
every year. Dropping a scheme-season would make the lines incomparable in a way
no reader could see.

Usage:  python scripts/build_view_data.py
"""

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_dam as fd  # noqa: E402

DB = Path("data/processed/reservoir.duckdb")
# Optional. Supply it and the second view renders a real map; without it the
# map cannot be drawn, because the WRD report publishes district and taluka but
# never coordinates. Columns: scheme_id, lat, lon [, source].
COORDS = Path("data/reference/dam_coordinates.csv")
SEASON_CAP = Path("data/processed/season_capacity.csv")
MID = Path("data/processed/capacity_midseason_exceptions.csv")
LEDGER = Path("data/interim/backfill_ledger.csv")
OUT = Path("web/data/reservoir_view.json")

SOURCE_URL = "https://wrd-dam.gujarat.gov.in/downloads/home_pdf.php?dt=<base64 date>"
# Derived from the data in main(), never hardcoded. These are the fallbacks
# only, and they are overwritten on every run.
#
# They WERE hardcoded, and the daily refresh is what exposed it: once
# 2026-09-12 loaded, the facets and the headline picked up day 104 from the
# series while the deviation tables, the scheme details and the provenance
# footer all still said 2026-09-11, because they key off REPORT_DATE. A view
# that dates itself one day behind the numbers it is showing is worse than a
# stale one, because nothing on its face reveals the disagreement.
REPORT_DATE = "2026-09-11"
CURRENT_SEASON = 2026
PRIOR_SEASONS = [2022, 2023, 2024, 2025]
N_PRIOR_SEASONS = 4
SEASON_MONTHS_SQL = (6, 7, 8, 9, 10)
SEASON_START = (6, 1)          # 1 June
SEASON_DAYS = 153              # to 31 October

# The one tunable parameter of the crossover marker, stated in the interface.
CROSSOVER_MIN_RUN = 5

REGION_NAME = {"SG": "South Gujarat", "NG": "North Gujarat",
               "CG": "Central Gujarat", "Sau": "Saurashtra", "Kutch": "Kutch"}


def day_of_season(d):
    start = date(d.year, *SEASON_START)
    return (d - start).days + 1


def crossover(cur, priors, min_run):
    """§9: aligned day-of-season, all four prior years required, run of >=N.

    Returns one of three distinct cases. A day is *evaluable* only if the
    current season and all four prior seasons have a value for it — a missing
    prior year is skipped, never treated as passing. Non-evaluable days are
    left out of the sequence rather than breaking it (there is exactly one such
    day in the whole dataset: 2025-09-26 is absent upstream, day 118).
    """
    ev = []
    for d in sorted(cur):
        if any(d not in p for p in priors):
            continue
        ev.append((d, cur[d] < min(p[d] for p in priors)))
    if not ev:
        return {"case": "not_evaluable", "n_evaluable": 0}

    runs, start = [], None
    for i, (d, below) in enumerate(ev):
        if below and start is None:
            start = i
        elif not below and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(ev) - 1))
    qual = [r for r in runs if r[1] - r[0] + 1 >= min_run]

    # Where does it stand on the latest evaluable day, and since when? The
    # marker is by definition the FIRST qualifying run, which may sit early in
    # a season that recovered and then fell below again — the state series does
    # exactly that. Reporting only the first run would read as "recovered" when
    # it is below today, so the current run travels alongside it.
    currently_below = ev[-1][1]
    current_run = None
    if currently_below:
        i = len(ev) - 1
        while i > 0 and ev[i - 1][1]:
            i -= 1
        current_run = {"start_day": ev[i][0], "length": len(ev) - i,
                       "qualifies": len(ev) - i >= min_run}

    base = {"n_evaluable": len(ev), "min_run": min_run,
            "first_evaluable_day": ev[0][0], "last_evaluable_day": ev[-1][0],
            "n_qualifying_runs": len(qual),
            "currently_below": currently_below, "current_run": current_run}
    if not qual:
        return {"case": "never_below", **base}

    a, b = qual[0]
    if a == 0:
        # Below from the first evaluable day: there was no crossing, and
        # reporting a date would assert one that never happened.
        out = {"case": "below_from_start", "marker_day": None,
               "run_start_day": ev[a][0], **base}
    else:
        out = {"case": "crossed", "marker_day": ev[a][0], **base}
    # Did that first run come back up? Only meaningful if it ended before the
    # data did.
    if b < len(ev) - 1:
        out["recovered_day"] = ev[b + 1][0]
        out["run_end_day"] = ev[b][0]
    return out


def main():
    global REPORT_DATE, CURRENT_SEASON, PRIOR_SEASONS
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="report date to build for. Default: the newest "
                         "in-season date in the database.")
    args = ap.parse_args()

    for p in (DB, SEASON_CAP):
        if not p.exists():
            sys.exit(f"MISSING {p}. Run the pipeline first.")
    con = duckdb.connect(str(DB), read_only=True)

    # The view dates itself from the data, so a daily refresh cannot leave the
    # provenance and the tables disagreeing about which day is "today".
    months = ",".join(str(m) for m in SEASON_MONTHS_SQL)
    newest = con.execute(f"""
        SELECT max(report_date) FROM fact_storage
        WHERE EXTRACT(month FROM report_date) IN ({months})
    """).fetchone()[0]
    if newest is None:
        sys.exit("no in-season dates loaded; nothing to build")
    REPORT_DATE = args.date or str(newest)
    CURRENT_SEASON = int(REPORT_DATE[:4])
    PRIOR_SEASONS = [CURRENT_SEASON - n
                     for n in range(N_PRIOR_SEASONS, 0, -1)]
    have = con.execute("SELECT count(*) FROM fact_storage WHERE report_date = ?::DATE",
                       [REPORT_DATE]).fetchone()[0]
    if not have:
        sys.exit(f"{REPORT_DATE} is not loaded; run the pipeline for it first")
    print(f"building for {REPORT_DATE} (season {CURRENT_SEASON}, "
          f"prior {PRIOR_SEASONS})")
    scap = pd.read_csv(SEASON_CAP)
    con.register("season_cap", scap[["scheme_id", "season", "capacity_class",
                                     "season_capacity_mcm"]])

    # ---- denominator: season capacity, falling back to the 2026 value -------
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW cap AS
        WITH cur AS (
            SELECT scheme_id, design_gross_mcm AS cap_2026
            FROM fact_storage WHERE report_date = DATE '{REPORT_DATE}'
        )
        SELECT sc.scheme_id, sc.season, sc.capacity_class,
               COALESCE(sc.season_capacity_mcm, c.cap_2026) AS cap_mcm,
               sc.season_capacity_mcm IS NULL AS cap_substituted
        FROM season_cap sc JOIN cur c USING (scheme_id)
    """)

    # ---- per region per season per day-of-season ---------------------------
    ser = con.execute(f"""
        SELECT d.region_latest AS region,
               EXTRACT(year FROM f.report_date)::INT AS season,
               f.report_date,
               sum(f.present_gross_mcm) AS present_mcm,
               sum(c.cap_mcm)           AS design_mcm,
               count(*)                 AS schemes
        FROM fact_storage f
        JOIN dim_scheme d USING (scheme_id)
        JOIN cap c ON c.scheme_id = f.scheme_id
                  AND c.season = EXTRACT(year FROM f.report_date)
        WHERE EXTRACT(month FROM f.report_date) IN (6,7,8,9,10)
        GROUP BY 1,2,3
    """).df()
    ser["report_date"] = pd.to_datetime(ser["report_date"])
    ser["dos"] = ser["report_date"].dt.date.map(day_of_season)
    ser["pct"] = 100.0 * ser["present_mcm"] / ser["design_mcm"]

    # ---- rainfall: regional mean of the 24 h column ------------------------
    rain = con.execute("""
        SELECT d.region_latest AS region,
               EXTRACT(year FROM r.report_date)::INT AS season,
               r.report_date,
               avg(r.rain_last_24h_mm) AS rain_mm,
               count(*) AS schemes
        FROM fact_rainfall r JOIN dim_scheme d USING (scheme_id)
        WHERE EXTRACT(month FROM r.report_date) IN (6,7,8,9,10)
        GROUP BY 1,2,3
    """).df()
    rain["report_date"] = pd.to_datetime(rain["report_date"])
    rain["dos"] = rain["report_date"].dt.date.map(day_of_season)

    def series_map(df, region, season, col):
        s = df[(df["region"] == region) & (df["season"] == season)]
        return {int(r.dos): round(float(getattr(r, col)), 3)
                for r in s.itertuples() if pd.notna(getattr(r, col))}

    def dates_map(df, region, season):
        s = df[(df["region"] == region) & (df["season"] == season)]
        return {int(r.dos): r.report_date.strftime("%Y-%m-%d")
                for r in s.itertuples()}

    regions = []
    for code in ser["region"].unique():
        cur = series_map(ser, code, CURRENT_SEASON, "pct")
        priors = [series_map(ser, code, y, "pct") for y in PRIOR_SEASONS]
        mcm_cur = series_map(ser, code, CURRENT_SEASON, "present_mcm")
        latest_day = max(cur) if cur else None
        prior_at_latest = [p[latest_day] for p in priors if latest_day in p]
        sub = con.execute("""
            SELECT count(*) FROM cap c JOIN dim_scheme d USING (scheme_id)
            WHERE c.cap_substituted AND d.region_latest = ?
        """, [code]).fetchone()[0]
        regions.append({
            "code": code, "name": REGION_NAME.get(code, code),
            "n_schemes": int(ser[(ser.region == code)
                                 & (ser.season == CURRENT_SEASON)]
                             ["schemes"].max()),
            "design_mcm": round(float(
                ser[(ser.region == code) & (ser.season == CURRENT_SEASON)]
                ["design_mcm"].iloc[-1]), 1),
            "pct_now": round(cur[latest_day], 2) if latest_day else None,
            "mcm_now": round(mcm_cur.get(latest_day, float("nan")), 1),
            "mean_prior_at_now": (round(sum(prior_at_latest)
                                        / len(prior_at_latest), 2)
                                  if prior_at_latest else None),
            "n_capacity_substituted": int(sub),
            "series": {str(CURRENT_SEASON): cur,
                       **{str(y): series_map(ser, code, y, "pct")
                          for y in PRIOR_SEASONS}},
            "mcm": {str(CURRENT_SEASON): mcm_cur,
                    **{str(y): series_map(ser, code, y, "present_mcm")
                       for y in PRIOR_SEASONS}},
            "rain": {str(CURRENT_SEASON): series_map(rain, code,
                                                     CURRENT_SEASON, "rain_mm"),
                     **{str(y): series_map(rain, code, y, "rain_mm")
                        for y in PRIOR_SEASONS}},
            "dates": dates_map(ser, code, CURRENT_SEASON),
            "crossover": crossover(cur, priors, CROSSOVER_MIN_RUN),
        })
    # Worst first: the facet order carries information, and it is stated in the
    # interface so nobody reads it as geographic.
    regions.sort(key=lambda r: (r["pct_now"] - r["mean_prior_at_now"])
                 if r["pct_now"] is not None and r["mean_prior_at_now"] is not None
                 else 0)

    # ---- state, same construction ------------------------------------------
    st = ser.groupby(["season", "dos"], as_index=False).agg(
        present_mcm=("present_mcm", "sum"), design_mcm=("design_mcm", "sum"))
    st["pct"] = 100.0 * st["present_mcm"] / st["design_mcm"]

    def st_map(season, col):
        s = st[st["season"] == season]
        return {int(r.dos): round(float(getattr(r, col)), 3)
                for r in s.itertuples()}

    st_cur = st_map(CURRENT_SEASON, "pct")
    st_priors = [st_map(y, "pct") for y in PRIOR_SEASONS]
    st_day = max(st_cur)
    state = {
        "pct_now": round(st_cur[st_day], 2),
        "mcm_now": round(st_map(CURRENT_SEASON, "present_mcm")[st_day], 1),
        "design_mcm": round(float(st[st.season == CURRENT_SEASON]
                                  ["design_mcm"].iloc[-1]), 1),
        "day_of_season": int(st_day),
        "prior_at_now": {str(y): round(p[st_day], 2)
                         for y, p in zip(PRIOR_SEASONS, st_priors)
                         if st_day in p},
        "series": {str(CURRENT_SEASON): st_cur,
                   **{str(y): st_map(y, "pct") for y in PRIOR_SEASONS}},
        # % filling is what the facets plot; MCM rides along for the tooltip.
        "mcm": {str(CURRENT_SEASON): st_map(CURRENT_SEASON, "present_mcm"),
                **{str(y): st_map(y, "present_mcm") for y in PRIOR_SEASONS}},
        "crossover": crossover(st_cur, st_priors, CROSSOVER_MIN_RUN),
    }
    state["mean_prior_at_now"] = round(
        sum(state["prior_at_now"].values()) / len(state["prior_at_now"]), 2)

    # ---- both deviation tables, recomputed here from the same rules ---------
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW today AS
        SELECT f.scheme_id, d.scheme_name_latest AS scheme_name,
               d.district_latest AS district, d.region_latest AS region,
               f.pct_filling, f.present_live_mcm, f.design_gross_mcm,
               f.outflow_canal_cusecs, f.days_water_at_release, f.warning,
               d.n_design_variants
        FROM fact_storage f JOIN dim_scheme d USING (scheme_id)
        WHERE f.report_date = DATE '{REPORT_DATE}'
    """)
    # Deliberately season_cap, not the `cap` view: the baseline must use the
    # PUBLISHED season capacity. The 2026 substitution exists only to keep the
    # regional denominator free of within-season steps, and letting it leak in
    # here would quietly re-admit the three excluded scheme-seasons.
    ann = con.execute(f"""
        SELECT f.scheme_id, EXTRACT(year FROM f.report_date)::INT AS season,
               f.pct_filling, c.capacity_class, c.season_capacity_mcm,
               t.design_gross_mcm AS cap_today
        FROM fact_storage f
        JOIN today t USING (scheme_id)
        JOIN season_cap c ON c.scheme_id = f.scheme_id
                         AND c.season = EXTRACT(year FROM f.report_date)
        WHERE EXTRACT(month FROM f.report_date)
                  = EXTRACT(month FROM DATE '{REPORT_DATE}')
          AND EXTRACT(day FROM f.report_date)
                  = EXTRACT(day FROM DATE '{REPORT_DATE}')
          AND f.report_date < DATE '{REPORT_DATE}'
          AND f.pct_filling IS NOT NULL
    """).df()
    eps = 0.005
    ann["mid"] = ann["capacity_class"] == "mid_season"
    ok = (~ann["mid"]) & (
        ((ann["season_capacity_mcm"] - ann["cap_today"]).abs()
         / ann["cap_today"]) <= eps)
    base = ann[ok].groupby("scheme_id").agg(
        mean_prior=("pct_filling", "mean"), n_prior=("pct_filling", "size"),
        years=("season", lambda s: sorted(int(x) for x in s))).reset_index()
    tod = con.execute("SELECT * FROM today").df()
    # Hazard 5, applied as a DERIVED fix rather than a re-parse: dim_scheme
    # takes district from the latest report, and on 2026-09-11 Kabarka's cell
    # reads a truncated 'Devbhumi', which produced a phantom 28th district in
    # published output. The raw parse stays untouched and auditable; the
    # vocabulary lives in fetch_dam so a future re-parse applies it at source.
    canon = tod["district"].map(lambda d: fd.canon_district(d)[0])
    moved = int((canon != tod["district"]).sum())
    if moved:
        for a, b in sorted(set(zip(tod.loc[canon != tod["district"], "district"],
                                   canon[canon != tod["district"]]))):
            print(f"district canonicalised: {a!r} -> {b!r}")
    tod["district"] = canon
    print(f"districts after canonicalisation: {tod['district'].nunique()} "
          f"({moved} row(s) relabelled)")

    dev = tod.merge(base, on="scheme_id")
    dev["dev_pp"] = dev["pct_filling"] - dev["mean_prior"]
    dev["shortfall_mcm"] = (dev["mean_prior"] - dev["pct_filling"]) / 100.0 \
        * dev["design_gross_mcm"]
    dev["restated"] = dev["n_design_variants"] > 1

    def rows(df, cols):
        return json.loads(df[cols].to_json(orient="records"))

    cols_pp = ["scheme_id", "scheme_name", "district", "region", "pct_filling",
               "mean_prior", "dev_pp", "n_prior", "present_live_mcm",
               "design_gross_mcm", "days_water_at_release", "restated"]
    dev_pp = rows(dev.nsmallest(20, "dev_pp"), cols_pp)
    dev_vol = rows(dev.nlargest(10, "shortfall_mcm"),
                   ["scheme_id", "scheme_name", "district", "region",
                    "design_gross_mcm", "pct_filling", "mean_prior", "dev_pp",
                    "shortfall_mcm"])

    sch = tod.merge(base[["scheme_id", "mean_prior", "n_prior"]],
                    on="scheme_id", how="left").sort_values("scheme_name")
    sch["dev_pp"] = sch["pct_filling"] - sch["mean_prior"]
    schemes = rows(sch, ["scheme_id", "scheme_name", "district", "region",
                         "pct_filling", "present_live_mcm", "design_gross_mcm",
                         "outflow_canal_cusecs", "days_water_at_release",
                         "warning", "mean_prior", "n_prior", "dev_pp"])

    # ---- coordinates, if anyone has supplied them --------------------------
    coords, coords_meta = {}, {
        "available": False, "n_placed": 0, "n_schemes": int(len(tod)),
        "source": None,
        "why_absent": ("The WRD daily report publishes district and taluka for "
                       "each dam, never coordinates. OpenStreetMap name "
                       "matching resolved only 9 of 206 schemes (4.4%), so it "
                       "cannot place them either."),
        "expected_file": str(COORDS),
        "expected_columns": ["scheme_id", "lat", "lon", "source (optional)"],
    }
    if COORDS.exists():
        cdf = pd.read_csv(COORDS)
        need = {"scheme_id", "lat", "lon"}
        if not need <= set(cdf.columns):
            print(f"WARNING {COORDS} lacks {need - set(cdf.columns)}; ignored")
        else:
            cdf = cdf.dropna(subset=["lat", "lon"])
            known = set(tod["scheme_id"])
            cdf = cdf[cdf["scheme_id"].isin(known)]
            coords = {int(r.scheme_id): [round(float(r.lat), 5),
                                         round(float(r.lon), 5)]
                      for r in cdf.itertuples()}
            coords_meta.update({
                "available": len(coords) > 0, "n_placed": len(coords),
                "source": (str(cdf["source"].iloc[0])
                           if "source" in cdf.columns and len(cdf) else "supplied"),
            })
            print(f"coordinates: {len(coords)} of {len(tod)} schemes placed")

    # ---- provenance --------------------------------------------------------
    led = pd.read_csv(LEDGER, dtype={"report_date": str})
    row = led[led["report_date"] == REPORT_DATE]
    cov = con.execute("""
        SELECT count(DISTINCT report_date) AS dates_loaded,
               min(report_date) AS first_date, max(report_date) AS last_date
        FROM fact_storage
    """).df().iloc[0]
    n_rel = int((tod["outflow_canal_cusecs"] > 0).sum())
    mid_rows = (json.loads(pd.read_csv(MID)[
        ["scheme_id", "scheme_name", "season", "values", "change_dates"]]
        .to_json(orient="records")) if MID.exists() else [])

    doc = {
        "meta": {
            "report_date": REPORT_DATE,
            "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "Gujarat Water Resources Department — daily dam storage report",
            "source_url": SOURCE_URL,
            "report_sha256": (row["sha256"].iloc[0] if len(row) else None),
            "report_bytes": (int(row["bytes"].iloc[0])
                             if len(row) and pd.notna(row["bytes"].iloc[0]) else None),
            "dates_loaded": int(cov["dates_loaded"]),
            "span": [str(cov["first_date"]), str(cov["last_date"])],
            "absent_upstream": ["2025-09-26", "2026-09-12"],
            "seasons_prior": PRIOR_SEASONS,
            "season_current": CURRENT_SEASON,
            "season_window": "1 June – 31 October",
            "crossover_min_run": CROSSOVER_MIN_RUN,
            "capacity_tolerance_pct": eps * 100,
            "n_schemes": int(len(tod)),
            "n_releasing": n_rel,
            "n_no_release": int(len(tod) - n_rel),
            "midseason_exceptions": mid_rows,
        },
        "state": state,
        "regions": regions,
        "deviation_pp": dev_pp,
        "deviation_volume": dev_vol,
        "schemes": schemes,
        "coords": coords,
        "coords_meta": coords_meta,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    print(f"written: {OUT}  ({OUT.stat().st_size / 1024:,.0f} KB)")
    print(f"state {state['pct_now']}% vs prior mean "
          f"{state['mean_prior_at_now']}% on day {state['day_of_season']}")
    for r in regions:
        c = r["crossover"]
        detail = c.get("marker_day") or c.get("case")
        print(f"  {r['name']:<16} {r['pct_now']:>6.2f}%  "
              f"prior mean {r['mean_prior_at_now']:>6.2f}%  "
              f"crossover: {c['case']}"
              f"{'' if c['case'] != 'crossed' else f' day {detail}'}"
              f"   evaluable {c['n_evaluable']}"
              f"   cap-substituted schemes {r['n_capacity_substituted']}")
    con.close()


if __name__ == "__main__":
    main()

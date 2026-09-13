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
# Written by the same script. Every scheme NOT placed, with the reason, so the
# page can say which of the 206 are absent and why rather than just omitting
# them silently.
COORDS_EX = Path("data/reference/dam_coordinates_excluded.csv")
# Written by scripts/build_command_area.py. Culturable command area per
# scheme, for the 40 of 206 where the state's own Data Bank page survives its
# three self-consistency checks. The excluded file records every rejection.
CCA_FILE = Path("data/reference/scheme_command_area.csv")
CCA_EX = Path("data/reference/scheme_command_area_excluded.csv")
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
                                     "season_capacity_mcm",
                                     "live_capacity_class",
                                     "season_live_capacity_mcm"]])

    # ---- denominator: season capacity, falling back to the 2026 value -------
    #
    # LIVE runs alongside GROSS, by the same rule and with the same fallback.
    # Live is the water that can actually be released — gross includes dead
    # storage below the lowest outlet, which no canal can reach — so live is
    # what the page leads with (§25). The two denominators differ by 1,204 MCM
    # statewide, and the difference is not spread evenly: Kutch is 22.8% full
    # on gross and 17.2% on live, so gross flatters the driest region most.
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW cap AS
        WITH cur AS (
            SELECT scheme_id, design_gross_mcm AS cap_2026,
                   design_live_mcm  AS lcap_2026
            FROM fact_storage WHERE report_date = DATE '{REPORT_DATE}'
        )
        SELECT sc.scheme_id, sc.season, sc.capacity_class,
               COALESCE(sc.season_capacity_mcm, c.cap_2026) AS cap_mcm,
               sc.season_capacity_mcm IS NULL AS cap_substituted,
               COALESCE(sc.season_live_capacity_mcm, c.lcap_2026) AS lcap_mcm,
               sc.season_live_capacity_mcm IS NULL AS lcap_substituted
        FROM season_cap sc JOIN cur c USING (scheme_id)
    """)

    # ---- per region per season per day-of-season ---------------------------
    ser = con.execute(f"""
        SELECT d.region_latest AS region,
               EXTRACT(year FROM f.report_date)::INT AS season,
               f.report_date,
               sum(f.present_gross_mcm) AS present_mcm,
               sum(c.cap_mcm)           AS design_mcm,
               sum(f.present_live_mcm)  AS present_live_mcm,
               sum(c.lcap_mcm)          AS design_live_mcm,
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
    ser["lpct"] = 100.0 * ser["present_live_mcm"] / ser["design_live_mcm"]

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
        lcur = series_map(ser, code, CURRENT_SEASON, "lpct")
        lpriors = [series_map(ser, code, y, "lpct") for y in PRIOR_SEASONS]
        lmcm_cur = series_map(ser, code, CURRENT_SEASON, "present_live_mcm")
        lprior_at_latest = [p[latest_day] for p in lpriors if latest_day in p]
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
            # live, same construction — including its own prior baseline, so a
            # live percentage is never compared against a gross average
            "design_live_mcm": round(float(
                ser[(ser.region == code) & (ser.season == CURRENT_SEASON)]
                ["design_live_mcm"].iloc[-1]), 1),
            "live_pct_now": (round(lcur[latest_day], 2)
                             if latest_day in lcur else None),
            "live_mcm_now": round(lmcm_cur.get(latest_day, float("nan")), 1),
            "live_mean_prior_at_now": (round(sum(lprior_at_latest)
                                             / len(lprior_at_latest), 2)
                                       if lprior_at_latest else None),
            "live_series": {str(CURRENT_SEASON): lcur,
                            **{str(y): series_map(ser, code, y, "lpct")
                               for y in PRIOR_SEASONS}},
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
        present_mcm=("present_mcm", "sum"), design_mcm=("design_mcm", "sum"),
        present_live_mcm=("present_live_mcm", "sum"),
        design_live_mcm=("design_live_mcm", "sum"))
    st["pct"] = 100.0 * st["present_mcm"] / st["design_mcm"]
    st["lpct"] = 100.0 * st["present_live_mcm"] / st["design_live_mcm"]

    def st_map(season, col):
        s = st[st["season"] == season]
        return {int(r.dos): round(float(getattr(r, col)), 3)
                for r in s.itertuples()}

    st_cur = st_map(CURRENT_SEASON, "pct")
    st_priors = [st_map(y, "pct") for y in PRIOR_SEASONS]
    st_lcur = st_map(CURRENT_SEASON, "lpct")
    st_lpriors = [st_map(y, "lpct") for y in PRIOR_SEASONS]
    st_day = max(st_cur)
    state = {
        "pct_now": round(st_cur[st_day], 2),
        "mcm_now": round(st_map(CURRENT_SEASON, "present_mcm")[st_day], 1),
        "design_mcm": round(float(st[st.season == CURRENT_SEASON]
                                  ["design_mcm"].iloc[-1]), 1),
        # LIVE — what the page leads with. Its own denominator and its own
        # prior baseline: mixing a live numerator with a gross average would
        # manufacture a deviation out of dead storage.
        "live_pct_now": round(st_lcur[st_day], 2),
        "live_mcm_now": round(st_map(CURRENT_SEASON,
                                     "present_live_mcm")[st_day], 1),
        "design_live_mcm": round(float(st[st.season == CURRENT_SEASON]
                                       ["design_live_mcm"].iloc[-1]), 1),
        "live_prior_at_now": {str(y): round(p[st_day], 2)
                              for y, p in zip(PRIOR_SEASONS, st_lpriors)
                              if st_day in p},
        "live_series": {str(CURRENT_SEASON): st_lcur,
                        **{str(y): st_map(y, "lpct") for y in PRIOR_SEASONS}},
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
    state["live_mean_prior_at_now"] = round(
        sum(state["live_prior_at_now"].values())
        / len(state["live_prior_at_now"]), 2) if state["live_prior_at_now"] else None
    state["mean_prior_at_now"] = round(
        sum(state["prior_at_now"].values()) / len(state["prior_at_now"]), 2)

    # ---- both deviation tables, recomputed here from the same rules ---------
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW today AS
        SELECT f.scheme_id, d.scheme_name_latest AS scheme_name,
               d.district_latest AS district, d.region_latest AS region,
               -- present_gross is the NUMERATOR of the published percentage.
               -- Without it the interface prints a percentage and a storage
               -- figure that cannot be divided into each other: live storage
               -- over design gross is a different ratio, off by the dead
               -- storage (9.2 pp at Ukai). Carried so the arithmetic closes
               -- on screen and in the CSV.
               f.pct_filling, f.present_gross_mcm, f.present_live_mcm,
               f.design_gross_mcm,
               -- the live denominator, so the scheme detail can print a live
               -- percentage whose numerator and denominator divide into each
               -- other. present_live / design_GROSS was the ratio the note
               -- above warns about; this is the one that closes.
               f.design_live_mcm,
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
               t.design_gross_mcm AS cap_today,
               -- the live percentage on the same prior date, on that season's
               -- own live capacity. Computed here rather than taken from the
               -- source, which publishes only the gross percentage.
               CASE WHEN c.season_live_capacity_mcm > 0
                    THEN 100.0 * f.present_live_mcm / c.season_live_capacity_mcm
               END AS live_pct,
               c.live_capacity_class, c.season_live_capacity_mcm,
               t.design_live_mcm AS lcap_today
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
    # The live baseline gets its OWN comparability test against the live
    # capacity, not a borrowed pass from the gross one. A scheme whose gross
    # capacity held steady while its live capacity was restated would otherwise
    # contribute a prior year measured on a different denominator.
    lmid = ann["live_capacity_class"] == "mid_season"
    lok = (~lmid) & ann["live_pct"].notna() & (
        ((ann["season_live_capacity_mcm"] - ann["lcap_today"]).abs()
         / ann["lcap_today"]) <= eps)
    lbase = ann[lok].groupby("scheme_id").agg(
        live_mean_prior=("live_pct", "mean"),
        n_prior_live=("live_pct", "size")).reset_index()
    tod = con.execute("SELECT * FROM today").df()
    # Hazard 5, applied as a DERIVED fix rather than a re-parse: dim_scheme
    # takes district from the latest report, and on 2026-09-11 Kabarka's cell
    # reads a truncated 'Devbhumi', which produced a phantom 28th district in
    # published output. The raw parse stays untouched and auditable; the
    # vocabulary lives in fetch_dam so a future re-parse applies it at source.
    # This WAS a downstream patch that rewrote district here, leaving the
    # parquet and DuckDB holding the broken values. The fix now lives in
    # fetch_dam.parse_detail, so this is a CHECK rather than a repair: if any
    # district still needs rewriting, a day was parsed with the old code and
    # the fix has silently regressed.
    canon = tod["district"].map(lambda d: fd.canon_district(d)[0])
    stale = tod.loc[canon != tod["district"], "district"].unique()
    if len(stale):
        print(f"WARNING: {len(stale)} district value(s) not canonical in the "
              f"parsed data: {sorted(stale)}")
        print("         The parser fix did not reach these rows — re-parse "
              "the affected days.")
        tod["district"] = canon          # do not publish the broken value
    else:
        print(f"districts: {tod['district'].nunique()} distinct, all canonical "
              f"at parse time")

    dev = tod.merge(base, on="scheme_id")
    dev = dev.merge(lbase, on="scheme_id", how="left")
    dev["dev_pp"] = dev["pct_filling"] - dev["mean_prior"]
    dev["restated"] = dev["n_design_variants"] > 1
    # Live, and the page RANKS on it. This is not cosmetic: ranking the twenty
    # worst on gross and on live agrees on only fourteen of them, so the two
    # bases name six different dams as furthest below. A page that leads with
    # live and then lists the gross worst-twenty is contradicting itself about
    # which reservoirs are in trouble.
    dev["live_pct_today"] = pd.Series(
        100.0 * dev["present_live_mcm"] / dev["design_live_mcm"]
    ).where(dev["design_live_mcm"] > 0)
    dev["live_dev_pp"] = dev["live_pct_today"] - dev["live_mean_prior"]
    # The volume shortfall follows the same basis: how much RELEASABLE water is
    # missing against the prior-year norm, over the live capacity.
    dev["shortfall_mcm"] = (dev["live_mean_prior"] - dev["live_pct_today"]) \
        / 100.0 * dev["design_live_mcm"]
    # Gross kept alongside, so the CSV can still reconcile to the source's own
    # published percentage.
    dev["shortfall_gross_mcm"] = (dev["mean_prior"] - dev["pct_filling"]) \
        / 100.0 * dev["design_gross_mcm"]

    def rows(df, cols):
        return json.loads(df[cols].to_json(orient="records"))

    cols_pp = ["scheme_id", "scheme_name", "district", "region", "pct_filling",
               "mean_prior", "dev_pp", "n_prior", "present_live_mcm",
               "design_gross_mcm", "design_live_mcm", "live_pct_today",
               "live_mean_prior", "live_dev_pp", "n_prior_live",
               "days_water_at_release", "restated"]
    rank = "live_dev_pp" if dev["live_dev_pp"].notna().all() else "dev_pp"
    dev_pp = rows(dev.nsmallest(20, rank), cols_pp)
    dev_vol = rows(dev.nlargest(10, "shortfall_mcm"),
                   ["scheme_id", "scheme_name", "district", "region",
                    "design_gross_mcm", "design_live_mcm", "pct_filling",
                    "mean_prior", "dev_pp", "live_pct_today",
                    "live_mean_prior", "live_dev_pp", "shortfall_mcm",
                    "shortfall_gross_mcm"])
    print(f"deviation tables ranked on: {rank}")

    sch = tod.merge(base[["scheme_id", "mean_prior", "n_prior"]],
                    on="scheme_id", how="left").sort_values("scheme_name")
    sch["dev_pp"] = sch["pct_filling"] - sch["mean_prior"]
    sch = sch.merge(lbase, on="scheme_id", how="left")
    # today's live percentage on today's live capacity, against the live
    # baseline. Both sides live, so the deviation is not part dead storage.
    sch["live_pct_today"] = pd.Series(
        100.0 * sch["present_live_mcm"] / sch["design_live_mcm"]
    ).where(sch["design_live_mcm"] > 0)
    sch["live_dev_pp"] = sch["live_pct_today"] - sch["live_mean_prior"]

    # ---- per-scheme five-year series, for the detail sparkline -------------
    # Uses fact_storage.pct_filling - the SAME basis as the figure printed
    # beside it on the detail panel - so the two cannot disagree.
    #
    # It previously used the season-capacity denominator borrowed from the
    # regional facets, and that was wrong here. The facets aggregate many
    # schemes, where a fixed per-season denominator stops a restatement
    # putting a step in a line no rainfall caused. A single scheme's chart sits
    # next to that scheme's published percentage, and for the three mid-season
    # scheme-seasons the two bases diverged by up to 15 percentage points -
    # Jhuj on 2025-07-16 read 102.1% against a published 87.12%. Values above
    # 100% are impossible for a reservoir and were the tell.
    #
    # The trade is deliberate and the opposite of the facets': this line can
    # now step where a capacity was restated between seasons. That step is in
    # the published record, and on a per-scheme chart showing the published
    # number is worth more than a smooth line.
    #
    # Thinned to every THIN_TO-th day: at 153 days x 5 seasons x 206 schemes
    # the full series would add about 2.5 MB to a 220 KB file, and a sparkline
    # cannot resolve single days anyway. Stated in the interface.
    # LIVE, for the same reason the rest of the page is (§25). The invariant
    # this block exists to hold is that the sparkline and the "Filling now"
    # figure beside it share a basis, and that figure is now live — so the
    # series has to be too, or the caption's promise that "the two always
    # agree" becomes false.
    #
    # The denominator is each ROW's own published design_live_mcm, exactly as
    # pct_filling uses each row's own design_gross_mcm. That preserves the
    # property argued for above: a restatement shows as a step, because the
    # step is in the published record.
    THIN_TO = 3
    per = con.execute(f"""
        SELECT f.scheme_id,
               EXTRACT(year FROM f.report_date)::INT AS season,
               f.report_date,
               CASE WHEN f.design_live_mcm > 0
                    THEN 100.0 * f.present_live_mcm / f.design_live_mcm
               END AS pct
        FROM fact_storage f
        WHERE EXTRACT(month FROM f.report_date) IN ({months})
          AND f.pct_filling IS NOT NULL
          AND f.design_live_mcm > 0
          AND f.present_live_mcm IS NOT NULL
    """).df()
    per["report_date"] = pd.to_datetime(per["report_date"])
    per["dos"] = per["report_date"].dt.date.map(day_of_season)
    latest_dos = int(per[per["season"] == CURRENT_SEASON]["dos"].max())
    # Keep every THIN_TO-th day, and always the latest day of the current
    # season so the line ends where the headline number is.
    per = per[(per["dos"] % THIN_TO == 1) | (per["dos"] == latest_dos)]
    series_by_scheme = {}
    for (sid, season), grp in per.groupby(["scheme_id", "season"]):
        series_by_scheme.setdefault(int(sid), {})[str(int(season))] = {
            int(r.dos): round(float(r.pct), 1)
            for r in grp.itertuples() if pd.notna(r.pct)}

    schemes = rows(sch, ["scheme_id", "scheme_name", "district", "region",
                         "pct_filling", "present_gross_mcm",
                         "present_live_mcm", "design_gross_mcm",
                         "design_live_mcm",
                         "outflow_canal_cusecs", "days_water_at_release",
                         "warning", "mean_prior", "n_prior", "dev_pp",
                         "live_mean_prior", "n_prior_live", "live_dev_pp"])
    # Attach each scheme's own five-year series, and its live percentage.
    for row in schemes:
        row["series"] = series_by_scheme.get(row["scheme_id"], {})
        dl, pl = row.get("design_live_mcm"), row.get("present_live_mcm")
        # Guarded rather than assumed: a zero or absent live capacity would
        # divide by zero, and the honest output for that scheme is no live
        # percentage at all. All 206 carry one today; the guard is for the day
        # one of them does not.
        row["live_pct"] = (round(100.0 * pl / dl, 1)
                           if dl and pl is not None and dl > 0 else None)

    # ---- command area, for the schemes where it is verified ----------------
    # Written by scripts/build_command_area.py from the NWRWS Data Bank, which
    # is the only source that publishes it per scheme. 40 of 206 survive its
    # three source checks and a name+district match; the rest carry nothing,
    # and the page says how many and why rather than omitting them silently.
    cca_meta = {"available": False, "n_with_cca": 0,
                "n_schemes": int(len(tod)), "cca_ha_total": 0,
                "source": None, "source_url": None,
                "expected_file": str(CCA_FILE)}
    if CCA_FILE.exists():
        cdf = pd.read_csv(CCA_FILE)
        need = {"scheme_id", "cca_ha", "gca_ha"}
        if not need <= set(cdf.columns):
            print(f"WARNING {CCA_FILE} lacks {need - set(cdf.columns)}; ignored")
        else:
            keep = ["gca_ha", "cca_ha", "max_irrigated_ha",
                    "max_irrigated_year", "scheme_class", "year_completed",
                    "river", "n_command_villages", "contentid", "match_tier"]
            by_id = {int(r["scheme_id"]): r for _, r in cdf.iterrows()}
            n_max = 0
            for row in schemes:
                c = by_id.get(row["scheme_id"])
                if c is None:
                    continue
                cmd = {}
                for k in keep:
                    v = c.get(k)
                    cmd[k] = None if pd.isna(v) else (
                        int(v) if k in ("gca_ha", "cca_ha", "max_irrigated_ha",
                                        "n_command_villages", "contentid")
                        else v)
                # Empty max_irrigated means NOT RECORDED (blank year in the
                # source), never zero hectares — nine of the forty are in that
                # position and the interface must not imply they never
                # irrigated anything.
                if cmd.get("max_irrigated_ha") is not None:
                    n_max += 1
                row["command"] = cmd
            with_cca = [r for r in schemes if r.get("command")]
            mx = [r for r in with_cca
                  if r["command"].get("max_irrigated_ha") is not None]
            cca_meta.update(
                available=bool(with_cca), n_with_cca=len(with_cca),
                cca_ha_total=int(sum(r["command"]["cca_ha"] for r in with_cca)),
                n_with_max=len(mx),
                max_irrigated_ha_total=int(
                    sum(r["command"]["max_irrigated_ha"] for r in mx)),
                cca_ha_of_those_with_max=int(
                    sum(r["command"]["cca_ha"] for r in mx)),
                source="Gujarat NWRWS Data Bank — Canals and Command Area",
                source_url=("https://guj-nwrws.gujarat.gov.in/showpage.aspx"
                            "?contentid=1467&lang=English"),
                excluded_file=str(CCA_EX))
            print(f"command area: {len(with_cca)} of {len(schemes)} schemes "
                  f"({cca_meta['cca_ha_total']:,} ha CCA); "
                  f"{len(mx)} carry a recorded maximum irrigated")

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
            # The evidence mix travels with the coordinates. A map that places
            # 183 of 206 has to be able to say what placed them and why the
            # rest are missing, and neither fact can live only in a commit
            # message — the page states it, so the page needs the numbers.
            if "tier" in cdf.columns:
                coords_meta["tiers"] = {
                    str(k): int(v) for k, v in
                    cdf["tier"].value_counts().sort_index().items()}
            # Share of design capacity placed, which is the number that says
            # how much of the STORAGE the map accounts for. 183 of 206 schemes
            # is 98.8% of capacity, because the unplaced are nearly all small.
            placed_mcm = float(tod[tod["scheme_id"].isin(coords)]
                               ["design_gross_mcm"].sum())
            all_mcm = float(tod["design_gross_mcm"].sum())
            coords_meta["pct_capacity_placed"] = (
                round(placed_mcm / all_mcm * 100, 1) if all_mcm else None)
            if "outside_state_outline" in cdf.columns:
                coords_meta["n_outside_state"] = int(
                    (cdf["outside_state_outline"].astype(str).str.lower()
                     == "yes").sum())
            if COORDS_EX.exists():
                exdf = pd.read_csv(COORDS_EX)
                coords_meta["excluded"] = [
                    {"reason": str(k), "n": int(v)} for k, v in
                    exdf["reason"].value_counts().items()]
                coords_meta["n_excluded"] = int(len(exdf))
            print(f"coordinates: {len(coords)} of {len(tod)} schemes placed "
                  f"({coords_meta['pct_capacity_placed']}% of capacity); "
                  f"tiers {coords_meta.get('tiers')}")

    # ---- provenance --------------------------------------------------------
    led = pd.read_csv(LEDGER, dtype={"report_date": str})
    row = led[led["report_date"] == REPORT_DATE]
    cov = con.execute("""
        SELECT count(DISTINCT report_date) AS dates_loaded,
               min(report_date) AS first_date, max(report_date) AS last_date
        FROM fact_storage
    """).df().iloc[0]
    # Dates the server genuinely has no report for, read from the ledger rather
    # than listed by hand. This WAS a hardcoded ["2025-09-26", "2026-09-12"],
    # and it went stale the moment 2026-09-12 arrived: the page stated that the
    # very report it was displaying did not exist upstream. The ledger is one
    # row per date and is rewritten when a retry succeeds, so a date that later
    # turned up drops out of this list by itself.
    absent = sorted(led.loc[led["status"].isin(fd.ABSENT_UPSTREAM),
                            "report_date"].astype(str).unique())
    if REPORT_DATE in absent:            # cannot both be shown and be missing
        raise SystemExit(f"FATAL: {REPORT_DATE} is the report date but the "
                         f"ledger marks it absent upstream. Refusing to "
                         f"publish a view that contradicts itself.")
    print(f"absent upstream: {len(absent)} date(s)"
          + (f" — {', '.join(absent)}" if absent else ""))
    n_rel = int((tod["outflow_canal_cusecs"] > 0).sum())
    mid_rows = (json.loads(pd.read_csv(MID)[
        ["scheme_id", "scheme_name", "season", "values", "change_dates"]]
        .to_json(orient="records")) if MID.exists() else [])

    doc = {
        "meta": {
            "report_date": REPORT_DATE,
            "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "Gujarat Water Resources Department — daily dam storage report",
            # The REAL URL for this report date, not the pattern. It used to
            # publish SOURCE_URL verbatim, so the footer printed a literal
            # "?dt=<base64 date>" — a placeholder presented as provenance,
            # which a reader cannot follow and cannot verify. Built with the
            # same fd.token_for the fetcher uses, so the page cites the address
            # the file actually came from.
            "source_url": fd.BASE + fd.token_for(REPORT_DATE),
            # Kept separately, because the pattern is still worth stating —
            # it is how someone reproduces any other date.
            "source_url_pattern": SOURCE_URL,
            "report_sha256": (row["sha256"].iloc[0] if len(row) else None),
            "report_bytes": (int(row["bytes"].iloc[0])
                             if len(row) and pd.notna(row["bytes"].iloc[0]) else None),
            "dates_loaded": int(cov["dates_loaded"]),
            "span": [str(cov["first_date"]), str(cov["last_date"])],
            "absent_upstream": absent,
            "seasons_prior": PRIOR_SEASONS,
            "season_current": CURRENT_SEASON,
            "season_window": "1 June – 31 October",
            # The page computes the age of the data against the reader's clock
            # and warns past this. Same constant the pipeline fails on, so the
            # two cannot disagree about what counts as stale.
            "max_stale_days": fd.MAX_STALE_DAYS,
            "season_months": list(SEASON_MONTHS_SQL),
            "crossover_min_run": CROSSOVER_MIN_RUN,
            "scheme_series_thin_to": THIN_TO,
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
        "cca_meta": cca_meta,
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

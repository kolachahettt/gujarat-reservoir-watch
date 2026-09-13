"""
Gujarat Reservoir Watch — how each district actually irrigates, from the
2011 Census village directory.

WHY THE PAGE NEEDS THIS. The monitor watches 206 surface reservoirs, which
invites the reading that reservoirs are where Gujarat's irrigation comes from.
They are not. Statewide, wells and tubewells water 71.8% of the irrigated area
and canals 22.0%, and the imbalance is far sharper in the regions the page most
often reports as worst: Saurashtra's dams sit in districts irrigating about
eight hectares from wells for every one from canals.

That changes what a shortfall MEANS. Where canals carry the water, an empty
reservoir is a canal that will not open. Where wells carry it, the same empty
reservoir is mostly a signal about the recharge that did not happen — the same
failed monsoon, reaching the farmer through the water table instead of through
a canal. The page cannot say which without saying this.

SOURCE. `data/raw/pc11_vd_clean_shrid.dta`, the SHRUG distribution of the 2011
Census Village Directory, already in the repository for the district polygons
(see data/raw/MANIFEST.md; CC BY-NC-SA 4.0, Development Data Lab). It carries,
per village and in hectares: net area sown, total irrigated, and irrigated by
canal / well-or-tubewell / tank-or-lake / waterfall / other.

WHAT THIS IS NOT. It is not command area and cannot be used as one. It counts
where water is APPLIED, not which scheme supplied it, and 'irrigated by canals'
includes the Narmada canal network, which no dam in this monitor feeds.
Aggregation stops at the district: command follows the canal, not the contour,
so no dam is credited with any hectare here.

It is also 2011, which predates fifteen years of Narmada extension and the
SAUNI transfers into Saurashtra. Both are stated on the page.

Usage:
  python scripts/build_irrigation_mix.py
Outputs:
  data/reference/district_irrigation_mix.csv
"""

import csv
import json
import sys
from pathlib import Path

import pandas as pd

DTA = Path("data/raw/pc11_vd_clean_shrid.dta")
KEYS = Path("data/raw/corekeys/shrid_loc_names.dta")
VIEW = Path("web/data/reservoir_view.json")
OUT = Path("data/reference/district_irrigation_mix.csv")

COLS = {
    "pc11_vd_land_nt_swn": "net_sown_ha",
    "pc11_vd_land_src_irr": "irrigated_ha",
    "pc11_vd_land_canal_irr": "canal_ha",
    "pc11_vd_land_wl_tw_irr": "well_tubewell_ha",
    "pc11_vd_land_tnk_lk_irr": "tank_lake_ha",
    "pc11_vd_land_w_fall_irr": "waterfall_ha",
    "pc11_vd_land_oth_src_irr": "other_source_ha",
    "pc11_vd_land_un_irr": "unirrigated_ha",
    "pc11_vd_land_cult_waste": "culturable_waste_ha",
}

# WRD's current district names -> the 2011 census district they fall in.
# Districts created after 2011 map to the parent they were carved from, which
# is a real containment rather than an approximation.
TO_PC11 = {
    "ahmedabad": "ahmadabad", "banaskantha": "banas kantha",
    "chhotaudepur": "vadodara", "devbhumi dwarka": "jamnagar",
    "gir somnath": "junagadh", "mahisagar": "kheda", "morbi": "rajkot",
    "panchmahal": "panch mahals", "sabarkantha": "sabar kantha",
    "botad": "bhavnagar", "arvalli": "sabar kantha",
    "aravalli": "sabar kantha", "dahod": "dohad", "dang": "the dangs",
}


def main():
    for p in (DTA, KEYS, VIEW):
        if not p.exists():
            sys.exit(f"MISSING {p} — see DATA.md")

    vd = pd.read_stata(DTA, columns=["shrid2"] + list(COLS),
                       convert_categoricals=False).rename(columns=COLS)
    # No state column in this file; the state id is shrid2 field [1].
    vd = vd[vd["shrid2"].str.split("-").str[1] == "24"].copy()
    keys = pd.read_stata(KEYS, columns=["shrid2", "district_name",
                                        "town_name"],
                         convert_categoricals=False)
    vd = vd.merge(keys, on="shrid2", how="left")
    # Towns carry no village-directory land schedule; keeping them would add
    # rows of zeros to the denominators.
    is_town = vd["town_name"].fillna("").str.strip() != ""
    vd = vd[~is_town]
    print(f"Gujarat villages: {len(vd):,}")

    g = vd.groupby("district_name", as_index=False)[list(COLS.values())].sum()
    g["villages"] = vd.groupby("district_name")["shrid2"].size().values

    # An internal consistency test, not a formality: the five source columns
    # must sum to the 'all sources' total. They do, exactly, which is the
    # strongest available evidence the columns mean what they are labelled.
    parts = g[["canal_ha", "well_tubewell_ha", "tank_lake_ha",
               "waterfall_ha", "other_source_ha"]].sum(axis=1)
    worst = float(((parts - g["irrigated_ha"]).abs()
                   / g["irrigated_ha"].clip(lower=1)).max())
    print(f"source columns vs the total, worst district: {worst:.4%}")
    if worst > 0.001:
        sys.exit("the per-source columns do not sum to the irrigated total; "
                 "refusing to publish a mix built on them")

    g["canal_pct_of_irrigated"] = (100 * g["canal_ha"]
                                   / g["irrigated_ha"].clip(lower=1)).round(1)
    g["well_pct_of_irrigated"] = (100 * g["well_tubewell_ha"]
                                  / g["irrigated_ha"].clip(lower=1)).round(1)
    g["irrigated_pct_of_net_sown"] = (100 * g["irrigated_ha"]
                                      / g["net_sown_ha"].clip(lower=1)).round(1)
    # Hectares from wells per hectare from canals. Reported as a ratio because
    # that is the sentence the page makes: "eight to one".
    g["well_to_canal"] = (g["well_tubewell_ha"]
                          / g["canal_ha"].clip(lower=1)).round(2)

    # ---- attach the WRD region, via the schemes that sit in each district --
    view = json.loads(VIEW.read_text("utf-8"))
    reg_of = {}
    for s in view["schemes"]:
        d = s["district"].strip().lower()
        pc = TO_PC11.get(d, d)
        reg_of.setdefault(pc, set()).add(s["region"])
    multi = {k: v for k, v in reg_of.items() if len(v) > 1}
    if multi:
        # Not fatal, but it must be visible: a district hosting schemes from
        # two regions cannot be attributed to one of them.
        print(f"  NOTE {len(multi)} census district(s) host schemes from more "
              f"than one region: "
              + ", ".join(f"{k} {sorted(v)}" for k, v in multi.items()))
    g["region"] = g["district_name"].map(
        lambda d: (sorted(reg_of.get(d, {"—"}))[0]
                   if len(reg_of.get(d, {"—"})) == 1 else "multiple"))
    g["has_monitored_dam"] = g["district_name"].map(
        lambda d: d in reg_of)

    g = g.sort_values("canal_ha", ascending=False)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    g.to_csv(OUT, index=False, quoting=csv.QUOTE_MINIMAL,
             float_format="%.1f")
    print(f"written {OUT} ({len(g)} districts)")

    tot = g[list(COLS.values())].sum()
    print(f"\nGujarat 2011, all {len(g)} districts:")
    print(f"  net area sown        {tot['net_sown_ha']:>11,.0f} ha")
    print(f"  irrigated            {tot['irrigated_ha']:>11,.0f} ha "
          f"({tot['irrigated_ha']/tot['net_sown_ha']:.1%} of net sown)")
    print(f"    by canals          {tot['canal_ha']:>11,.0f} ha "
          f"({tot['canal_ha']/tot['irrigated_ha']:.1%})")
    print(f"    by wells/tubewells {tot['well_tubewell_ha']:>11,.0f} ha "
          f"({tot['well_tubewell_ha']/tot['irrigated_ha']:.1%})")
    print(f"    wells : canals     {tot['well_tubewell_ha']/tot['canal_ha']:.2f} : 1")

    print(f"\nby region, districts that host a monitored dam:")
    print(f"{'region':<10}{'districts':>10}{'canal ha':>12}{'well ha':>12}"
          f"{'well:canal':>12}{'canal % irr':>13}")
    sel = g[g["has_monitored_dam"] & (g["region"] != "multiple")]
    for code, grp in sel.groupby("region"):
        c, w = grp["canal_ha"].sum(), grp["well_tubewell_ha"].sum()
        i = grp["irrigated_ha"].sum()
        print(f"{code:<10}{len(grp):>10}{c:>12,.0f}{w:>12,.0f}"
              f"{w/max(c,1):>11.2f}:1{100*c/max(i,1):>12.1f}%")


if __name__ == "__main__":
    main()

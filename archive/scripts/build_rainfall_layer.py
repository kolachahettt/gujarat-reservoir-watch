"""
Join IMD current-district rainfall departure onto PC11 district geometry and
build the watchlist layer.

THE JOIN PROBLEM, AND HOW IT IS HANDLED HONESTLY
------------------------------------------------
IMD reports on Gujarat's 33 present-day districts. Our index is built on the
2011 Census's 26. Seven districts were created in 2013 out of parts of the
older ones, and neither source carries a code -- the only key is the name.

Every one of the 26 PC11 districts still exists today under the same name (or a
spelling variant), so the primary join is 26 clean 1:1 mappings. What changed is
that several present-day districts cover a SMALLER area than their 2011
namesake, because territory seceded to a new district.

We therefore:
  * take each PC11 district's departure from its same-name present-day district;
  * carry the seceded children's departures alongside, named;
  * flag `within_pc11_mixed` where a child's IMD category differs from the
    parent's, so a PC11 district whose seceded half behaves differently is
    visible rather than averaged away.

We do NOT invent an area-weighted recombination. Correct weighting needs the
present-day district boundaries, which SHRUG does not publish, or a sourced
taluka composition for each new district, which we do not have. Writing a
plausible-looking weight would be exactly the §9 failure this project exists to
avoid. The parent's own figure is used and the divergence is shown.

Crosswalk: data/reference/gujarat_district_crosswalk_2011_2026.csv (hand-built,
hand-checked, every row carrying a confidence and a note).

Usage:  python scripts/build_rainfall_layer.py
"""

import json
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import killcheck_mp as kc  # noqa: E402
from build_tiles import district_lut  # noqa: E402

IMD_CSV = Path("data/processed/imd_district_rainfall.csv")
IMD_META = Path("data/processed/imd_provenance.json")
CROSSWALK = Path("data/reference/gujarat_district_crosswalk_2011_2026.csv")
DISTRICTS = Path("web/data/gj_districts.geojson")
INDEX_PARQUET = Path("data/processed/basis_risk_index.parquet")
OUT_GEOJSON = Path("web/data/gj_district_rainfall.geojson")
OUT_META = Path("web/data/gj_rainfall_meta.json")
STATE = 24

DEFICIT_CATS = {"D", "LD", "NR"}     # IMD's own categories; not a threshold of ours
SURPLUS_CATS = {"E", "LE"}


def rule(t):
    print(f"\n{'=' * 74}\n{t}\n{'=' * 74}")


def load_inputs():
    for p in (IMD_CSV, IMD_META, CROSSWALK, DISTRICTS, INDEX_PARQUET):
        if not p.exists():
            sys.exit(f"MISSING {p}")
    imd = pd.read_csv(IMD_CSV)
    meta = json.loads(IMD_META.read_text())
    xw = pd.read_csv(CROSSWALK).fillna({"other_pc11_parents": ""})
    dist = gpd.read_file(DISTRICTS)
    return imd, meta, xw, dist


def audit_crosswalk(imd, xw, dist):
    rule("CROSSWALK AUDIT (§9.5)")
    imd_names, xw_names = set(imd["imd_district"]), set(xw["imd_district"])
    pc11_geom = set(dist["district_name"])
    pc11_xw = set(xw.loc[xw["relation"] == "same_name", "pc11_district_name"])

    print(f"IMD districts in feed:            {len(imd_names)}")
    print(f"crosswalk rows:                   {len(xw)}")
    print(f"  same_name (1:1 to PC11):        {(xw['relation'] == 'same_name').sum()}")
    print(f"  seceded (post-2011 districts):  {(xw['relation'] == 'seceded').sum()}")
    print(f"PC11 districts in geometry:       {len(pc11_geom)}")

    miss_a = imd_names - xw_names
    miss_b = xw_names - imd_names
    miss_c = pc11_geom - pc11_xw
    miss_d = pc11_xw - pc11_geom
    print(f"\nIMD district not in crosswalk:    {sorted(miss_a) or 'none'}")
    print(f"crosswalk name not in IMD feed:   {sorted(miss_b) or 'none'}")
    print(f"PC11 district with no same_name:  {sorted(miss_c) or 'none'}")
    print(f"crosswalk PC11 name not in geom:  {sorted(miss_d) or 'none'}")
    if miss_a or miss_b or miss_c or miss_d:
        sys.exit("\nCrosswalk is not complete. Fix it before building the layer.")
    print("\nall four checks clean — crosswalk is total and unambiguous")
    print(f"confidence: {xw['confidence'].value_counts().to_dict()}")


def build(imd, xw, dist):
    rule("JOIN")
    cat = imd.set_index("imd_district")
    same = xw[xw["relation"] == "same_name"].set_index("pc11_district_name")

    recs = []
    for name in dist["district_name"]:
        src = same.loc[name, "imd_district"]
        row = cat.loc[src]
        kids = xw[(xw["relation"] == "seceded")
                  & ((xw["pc11_district_name"] == name)
                     | xw["other_pc11_parents"].str.contains(rf"\b{name}\b", regex=True))]
        child_info = []
        for k in kids["imd_district"]:
            kr = cat.loc[k]
            child_info.append({
                "name": k.title(), "dep_pct": int(kr["cum_dep_pct"]),
                "cat": kr["cum_cat"],
            })
        mixed = any(_band(c["cat"]) != _band(row["cum_cat"]) for c in child_info)
        recs.append({
            "district_name": name,
            "imd_district": src,
            "cum_actual_mm": float(row["cum_actual_mm"]),
            "cum_normal_mm": float(row["cum_normal_mm"]),
            "cum_dep_pct": int(row["cum_dep_pct"]),
            "cum_cat": row["cum_cat"],
            "deficient": bool(row["cum_cat"] in DEFICIT_CATS),
            "seceded_children": json.dumps(child_info) if child_info else "",
            "within_pc11_mixed": bool(mixed),
        })
    out = dist.merge(pd.DataFrame(recs), on="district_name", how="left",
                     validate="1:1")
    print(f"districts joined: {len(out)}   "
          f"deficient: {int(out['deficient'].sum())}   "
          f"within-PC11 divergence flagged: {int(out['within_pc11_mixed'].sum())}")
    return out


def _band(c):
    return "deficit" if c in DEFICIT_CATS else "surplus" if c in SURPLUS_CATS else "normal"


def watchlist(out):
    """High-index villages sitting in a deficient district."""
    rule("WATCHLIST")
    idx = pd.read_parquet(INDEX_PARQUET,
                          columns=["shrid2", "pc11_state_id", "basis_risk_index",
                                   "index_pct_within_state", "subdivision_priority"])
    idx = idx[idx["pc11_state_id"] == STATE].copy()
    idx["district_code"] = idx["shrid2"].astype(str).str.split("-").str[2]
    idx["district_name"] = idx["district_code"].map(district_lut(STATE))

    deficit = set(out.loc[out["deficient"], "district_name"])
    idx["in_deficit_district"] = idx["district_name"].isin(deficit)
    idx["high_index"] = idx["subdivision_priority"] == 1
    idx["watchlist"] = idx["high_index"] & idx["in_deficit_district"]

    n = len(idx)
    print(f"Gujarat villages:                       {n:,}")
    print(f"  with an index:                        {idx['basis_risk_index'].notna().sum():,}")
    print(f"  high index (top decile in state):     {int(idx['high_index'].sum()):,}")
    print(f"  in a deficient district:              {int(idx['in_deficit_district'].sum()):,}")
    print(f"  ON WATCHLIST (both):                  {int(idx['watchlist'].sum()):,}")

    by = (idx[idx["watchlist"]].groupby("district_name").size()
          .sort_values(ascending=False))
    print("\nwatchlist villages by district:")
    dep = out.set_index("district_name")["cum_dep_pct"]
    for d, c in by.items():
        print(f"  {d:<18} {c:>5,}   rainfall {int(dep[d]):+d}%")
    return idx, int(idx["watchlist"].sum())


def main():
    imd, meta, xw, dist = load_inputs()
    audit_crosswalk(imd, xw, dist)
    out = build(imd, xw, dist)
    idx, n_watch = watchlist(out)

    keep = ["district_name", "imd_district", "cum_actual_mm", "cum_normal_mm",
            "cum_dep_pct", "cum_cat", "deficient", "within_pc11_mixed",
            "seceded_children", "mean_index", "mean_rainfed", "n_villages",
            "geometry"]
    out[[c for c in keep if c in out.columns]].to_file(OUT_GEOJSON, driver="GeoJSON")
    print(f"\nwritten: {OUT_GEOJSON} ({OUT_GEOJSON.stat().st_size / 1e3:,.0f} KB)")

    deficit_names = sorted(out.loc[out["deficient"], "district_name"])
    OUT_META.write_text(json.dumps({
        "imd": {k: meta[k] for k in
                ("source_name", "source_url", "pdf_day", "period_from",
                 "period_to", "fetched_utc", "sha256")},
        "crosswalk": str(CROSSWALK),
        "deficit_categories": sorted(DEFICIT_CATS),
        "districts_total": int(len(out)),
        "districts_deficient": int(out["deficient"].sum()),
        "deficient_districts": deficit_names,
        "within_pc11_mixed": sorted(
            out.loc[out["within_pc11_mixed"], "district_name"]),
        "watchlist_villages": n_watch,
    }, indent=2))
    print(f"         {OUT_META}")


if __name__ == "__main__":
    main()

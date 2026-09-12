"""
Geometry join, reference layers and PMTiles for one state (default: Gujarat).

Outputs
  web/tiles/<st>_villages.pmtiles     z8-13  village polygons
  web/tiles/<st>_overview.pmtiles     z4-7   subdistrict aggregate
  web/data/<st>_districts.geojson     district boundaries (named)
  web/data/<st>_district_labels.geojson  label points (named)
  web/data/<st>_state.geojson         state outline / coastline
  web/data/<st>_meta.json             quantile breaks + counts, data-derived

Design notes
  * Reference geography is REQUIRED, not decoration. Without district
    boundaries and labels the choropleth is unusable as a lookup map -- there
    is no way to tell where you are.
  * Class breaks are QUANTILES computed within the state and written to
    <st>_meta.json, not equal intervals hardcoded in the viewer. Gujarat's
    index is concentrated in 0.30-0.60, so equal intervals on 0-1 flatten it.
    Emitting them as data keeps the legend traceable (§9.2).
  * District names come from SHRUG Core Keys (`shrid_loc_names.dta`), not a
    hardcoded dictionary.
  * Towns are retained and tagged, never deleted -- deleting them left holes
    that read as missing data (§13).

Usage:  python scripts/build_tiles.py [--state 24] [--gpkg path]
"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import pyreadstat

STATE_SLUG = {24: "gj", 23: "mp", 8: "rj"}
INDEX_PARQUET = Path("data/processed/basis_risk_index.parquet")
# Authoritative admin geometry, and the district names ship with it -- so the
# labels on the map and the names in the district table cannot disagree.
# Dissolving village polygons was the earlier approach and was wrong twice
# over: village edges are not perfectly coincident, so it produced sliver
# artefacts, and the result was 2.8 MB of GeoJSON that delayed first paint.
ADMIN_DIST = Path("data/raw/admin/district.gpkg")
ADMIN_STATE = Path("data/raw/admin/state.gpkg")
OUT_DIR = Path("data/processed")
TILE_DIR = Path("web/tiles")
WEB_DATA = Path("web/data")

TARGET_CRS = "EPSG:4326"
EQUAL_AREA_CRS = "EPSG:6933"      # area weights only, never for output

VILLAGE_ZOOM = (8, 13)
OVERVIEW_ZOOM = (4, 7)
OVERVIEW_SIMPLIFY_DEG = 0.003
# Reference boundaries are context, not data. At 0.002 deg they came to 2.8 MB
# of GeoJSON that parsed before the choropleth could paint, so the map appeared
# empty for seconds on load. 0.005 deg (~500 m) is still finer than the line
# weight they are drawn at.
DISTRICT_SIMPLIFY_DEG = 0.005
STATE_SIMPLIFY_DEG = 0.005

N_CLASSES = 5

TILE_ATTRS = [
    "shrid2", "district_name", "basis_risk_index", "index_pct_within_state",
    "subdivision_priority", "low_confidence", "rainfed_share", "is_town",
]


def rule(t):
    print(f"\n{'=' * 74}\n{t}\n{'=' * 74}")


def find_gpkg(explicit):
    if explicit:
        p = Path(explicit)
        if not p.exists():
            sys.exit(f"MISSING FILE: {p}")
        return p
    cands = sorted(Path("data/raw").rglob("*.gpkg"), key=lambda p: p.stat().st_size)
    if not cands:
        sys.exit("No .gpkg under data/raw/. Download SHRUG Shrid Polygons first.")
    return cands[-1]


def load_state(gpkg, state):
    rule(f"READ polygons for state {state}")
    layers = pyogrio.list_layers(gpkg)
    layer = layers[0][0]
    info = pyogrio.read_info(gpkg, layer=layer)
    key = next((f for f in info["fields"] if f.lower() in ("shrid2", "shrid")), None)
    if key is None:
        sys.exit(f"No shrid key in {list(info['fields'])}.")
    if info["crs"] is None:
        sys.exit("Source has NO CRS. Refusing to guess (§8).")

    gdf = pyogrio.read_dataframe(gpkg, layer=layer, where=f"{key} LIKE '11-{state:02d}-%'")
    print(f"features: {len(gdf):,}   source CRS: {gdf.crs.to_string()}")
    if gdf.empty:
        sys.exit("Filter returned 0 features.")
    if gdf.crs.to_string().upper() != TARGET_CRS:
        print(f"transforming -> {TARGET_CRS} (explicit, §8)")
        gdf = gdf.to_crs(TARGET_CRS)
    else:
        print(f"already {TARGET_CRS}; no transform")

    gdf = gdf.rename(columns={key: "shrid2"})
    parts = gdf["shrid2"].astype(str).str.split("-")
    gdf["district_code"] = parts.str[2]
    gdf["subdist_key"] = parts.str[1] + "-" + parts.str[2] + "-" + parts.str[3]
    gdf["is_town"] = gdf["pc11_id"].astype(str).str.startswith("8").astype("int8")
    print(f"towns tagged (retained): {int(gdf['is_town'].sum()):,}")
    print(f"null/invalid geometries: "
          f"{int((gdf.geometry.isna() | ~gdf.geometry.is_valid).sum()):,}")
    return gdf


def district_lut(state):
    """pc11_district_id -> district_name, from the district polygon layer (§9.2)."""
    if not ADMIN_DIST.exists():
        sys.exit(f"MISSING {ADMIN_DIST}. Download SHRUG 'PC11 District Polygons'.")
    attrs = pyogrio.read_dataframe(
        ADMIN_DIST, read_geometry=False,
        where=f"pc11_state_id = '{state:02d}'")
    return attrs.set_index("pc11_district_id")["district_name"]


def attach_names(gdf, state):
    rule("DISTRICT NAMES (PC11 district polygon layer)")
    lut = district_lut(state)
    gdf["district_name"] = gdf["district_code"].map(lut)
    miss = int(gdf["district_name"].isna().sum())
    print(f"districts in layer: {len(lut)}   named village rows: "
          f"{gdf['district_name'].notna().sum():,}   unnamed: {miss:,}")
    if miss:
        print("  unmatched district codes: "
              f"{sorted(gdf.loc[gdf['district_name'].isna(), 'district_code'].unique())}")
    return gdf


def join_audit(gdf, idx, state):
    rule("JOIN AUDIT (§9.5)")
    idx = idx[idx["pc11_state_id"] == state].copy()
    villages = gdf[gdf["is_town"] == 0]
    gkeys, ikeys = set(villages["shrid2"]), set(idx["shrid2"])
    print(f"geometry rows (all):              {len(gdf):,}")
    print(f"  towns (tagged, retained):       {int(gdf['is_town'].sum()):,}")
    print(f"  village polygons:               {len(villages):,}")
    print(f"index rows for state:             {len(idx):,}")
    print(f"matched village keys:             {len(gkeys & ikeys):,}")
    print(f"village geometry with NO index:   {len(gkeys - ikeys):,} "
          f"({len(gkeys - ikeys) / max(len(gkeys), 1):.2%})")
    print(f"index row with NO geometry:       {len(ikeys - gkeys):,}")
    out = gdf.merge(idx.drop(columns=["pc11_state_id"]), on="shrid2",
                    how="left", validate="1:1")
    has = out["basis_risk_index"].notna()
    print(f"\njoined rows: {len(out):,}   with an index: {int(has.sum()):,} "
          f"({has.mean():.2%})")
    return out


def quantile_breaks(values, n=N_CLASSES):
    """Within-state quantiles. Equal intervals on 0-1 flatten a distribution
    concentrated in the middle, which is what Gujarat's is."""
    v = pd.Series(values).dropna()
    qs = np.linspace(0, 1, n + 1)[1:-1]
    breaks = [float(v.quantile(q)) for q in qs]
    return breaks, v


def build_overview(gdf):
    rule("OVERVIEW LAYER (subdistrict, z%d-%d)" % OVERVIEW_ZOOM)
    v = gdf[gdf["is_town"] == 0].copy()
    areas = v.to_crs(EQUAL_AREA_CRS).area
    v["_w"] = np.where(v["basis_risk_index"].notna(), areas, 0.0)
    v["_num"] = v["_w"] * v["basis_risk_index"].fillna(0.0)
    v["_rw"] = np.where(v["rainfed_share"].notna(), areas, 0.0)
    v["_rnum"] = v["_rw"] * v["rainfed_share"].fillna(0.0)

    agg = v.groupby("subdist_key").agg(
        district_name=("district_name", "first"),
        n_villages=("shrid2", "size"),
        n_with_index=("basis_risk_index", "count"),
        n_subdiv=("subdivision_priority", "sum"),
        w=("_w", "sum"), num=("_num", "sum"),
        rw=("_rw", "sum"), rnum=("_rnum", "sum"),
    )
    agg["mean_index"] = np.where(agg["w"] > 0, agg["num"] / agg["w"], np.nan)
    agg["mean_rainfed"] = np.where(agg["rw"] > 0, agg["rnum"] / agg["rw"], np.nan)
    agg["measured_share"] = agg["n_with_index"] / agg["n_villages"]
    agg["subdiv_share"] = np.where(agg["n_with_index"] > 0,
                                   agg["n_subdiv"] / agg["n_with_index"], np.nan)

    t0 = time.time()
    print(f"subdistricts: {len(agg):,}  dissolving...")
    geom = v[["subdist_key", "geometry"]].dissolve(by="subdist_key")
    print(f"  dissolve {time.time() - t0:.1f}s")
    ov = geom.join(agg[["district_name", "n_villages", "n_with_index",
                        "measured_share", "mean_index", "mean_rainfed",
                        "subdiv_share"]]).reset_index()
    ov["geometry"] = ov.geometry.simplify(OVERVIEW_SIMPLIFY_DEG, preserve_topology=True)
    return gpd.GeoDataFrame(ov, geometry="geometry", crs=TARGET_CRS)


def build_reference(gdf, slug, state):
    """District boundaries, label points and the state outline / coastline,
    from the authoritative admin layers."""
    rule("REFERENCE GEOGRAPHY (PC11 admin polygons)")
    v = gdf[gdf["is_town"] == 0]
    dist = pyogrio.read_dataframe(ADMIN_DIST, where=f"pc11_state_id = '{state:02d}'")
    dist = dist.to_crs(TARGET_CRS) if dist.crs.to_string().upper() != TARGET_CRS else dist
    print(f"districts: {len(dist)}")
    dist["geometry"] = dist.geometry.simplify(DISTRICT_SIMPLIFY_DEG, preserve_topology=True)

    # Area-weighted stats per district, for the label tooltip and the table.
    areas = v.to_crs(EQUAL_AREA_CRS).area
    w = pd.DataFrame({
        "district_name": v["district_name"].values,
        "rw": np.where(v["rainfed_share"].notna(), areas, 0.0),
        "rnum": np.where(v["rainfed_share"].notna(), areas, 0.0)
        * v["rainfed_share"].fillna(0.0).values,
        "iw": np.where(v["basis_risk_index"].notna(), areas, 0.0),
        "inum": np.where(v["basis_risk_index"].notna(), areas, 0.0)
        * v["basis_risk_index"].fillna(0.0).values,
        "n": 1,
        "rs_simple": v["rainfed_share"].values,
    }).groupby("district_name").agg(
        rw=("rw", "sum"), rnum=("rnum", "sum"), iw=("iw", "sum"),
        inum=("inum", "sum"), n_villages=("n", "sum"),
        mean_rainfed_simple=("rs_simple", "mean"),
    )
    w["mean_rainfed"] = w["rnum"] / w["rw"].replace(0, np.nan)
    w["mean_index"] = w["inum"] / w["iw"].replace(0, np.nan)
    dist = dist.merge(
        w[["mean_rainfed", "mean_rainfed_simple", "mean_index", "n_villages"]],
        on="district_name", how="left")

    labels = dist[["district_name", "mean_rainfed", "mean_index",
                   "n_villages", "geometry"]].copy()
    labels["geometry"] = dist.geometry.representative_point()

    if not ADMIN_STATE.exists():
        sys.exit(f"MISSING {ADMIN_STATE}. Download SHRUG 'PC11 State Polygons'.")
    st = pyogrio.read_dataframe(ADMIN_STATE, where=f"pc11_state_id = '{state:02d}'")
    st = st.to_crs(TARGET_CRS) if st.crs.to_string().upper() != TARGET_CRS else st
    st["geometry"] = st.geometry.simplify(STATE_SIMPLIFY_DEG, preserve_topology=True)
    print(f"state outline: {len(st)} feature(s) — {st['state_name'].iloc[0]}")

    WEB_DATA.mkdir(parents=True, exist_ok=True)
    for name, frame in ((f"{slug}_districts", dist),
                        (f"{slug}_district_labels", labels),
                        (f"{slug}_state", st)):
        p = WEB_DATA / f"{name}.geojson"
        frame.to_file(p, driver="GeoJSON")
        print(f"  {p}  ({p.stat().st_size / 1e3:,.0f} KB)")
    return dist


def write_pmtiles(gdf, stem, zooms, attrs):
    mb = OUT_DIR / f"{stem}.mbtiles"
    pm = TILE_DIR / f"{stem}.pmtiles"
    keep = [c for c in attrs if c in gdf.columns] + ["geometry"]
    slim = gdf[keep].copy()
    for c in ("subdivision_priority", "low_confidence", "is_town"):
        if c in slim:
            slim[c] = slim[c].astype("Int8")
    if mb.exists():
        mb.unlink()
    print(f"\nwriting {stem} z{zooms[0]}-{zooms[1]} ({len(slim):,} features)...")
    pyogrio.write_dataframe(
        slim, mb, driver="MBTiles", layer=stem.split("_", 1)[1],
        dataset_options={"MINZOOM": str(zooms[0]), "MAXZOOM": str(zooms[1])})
    TILE_DIR.mkdir(parents=True, exist_ok=True)
    if pm.exists():
        pm.unlink()
    from pmtiles.convert import mbtiles_to_pmtiles
    mbtiles_to_pmtiles(str(mb), str(pm), zooms[1])
    print(f"  MBTiles {mb.stat().st_size / 1e6:,.1f} MB  ->  "
          f"PMTiles {pm.stat().st_size / 1e6:,.1f} MB")
    return pm.stat().st_size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", type=int, default=24)
    ap.add_argument("--gpkg", default=None)
    args = ap.parse_args()
    slug = STATE_SLUG.get(args.state, f"s{args.state}")

    if not INDEX_PARQUET.exists():
        sys.exit(f"MISSING {INDEX_PARQUET}. Run scripts/build_index.py first.")

    gdf = load_state(find_gpkg(args.gpkg), args.state)
    gdf = attach_names(gdf, args.state)
    idx = pd.read_parquet(INDEX_PARQUET)
    joined = join_audit(gdf, idx, args.state)

    rule("CLASS BREAKS (within-state quantiles)")
    vals = joined.loc[joined["is_town"] == 0, "basis_risk_index"]
    breaks, v = quantile_breaks(vals)
    print(f"villages with an index: {len(v):,}")
    print(f"observed range: {v.min():.3f} - {v.max():.3f}   "
          f"median {v.median():.3f}")
    print(f"quintile breaks: {[round(b, 4) for b in breaks]}")
    edges = [float(v.min())] + breaks + [float(v.max())]
    counts = []
    for i in range(N_CLASSES):
        lo, hi = edges[i], edges[i + 1]
        sel = (v >= lo) & (v < hi) if i < N_CLASSES - 1 else (v >= lo) & (v <= hi)
        counts.append(int(sel.sum()))
        print(f"  class {i + 1}: {lo:.3f} - {hi:.3f}   {int(sel.sum()):>7,} villages")
    print("  (equal intervals on 0-1 would have put "
          f"{int(((v >= 0.30) & (v < 0.60)).sum()):,} of {len(v):,} "
          f"({((v >= 0.30) & (v < 0.60)).mean():.0%}) in two classes)")

    dist = build_reference(joined, slug, args.state)
    ov = build_overview(joined)

    rule("TILES")
    n_v = write_pmtiles(joined, f"{slug}_villages", VILLAGE_ZOOM, TILE_ATTRS)
    n_o = write_pmtiles(ov, f"{slug}_overview", OVERVIEW_ZOOM,
                        ["subdist_key", "district_name", "mean_index",
                         "mean_rainfed", "n_villages", "n_with_index",
                         "measured_share", "subdiv_share"])

    meta = {
        "state_code": args.state,
        "slug": slug,
        "generated": date.today().isoformat(),
        "source": "SHRUG 2.2 Pakora (CC BY-NC-SA 4.0); names from Core Keys",
        "villages_total": int((joined["is_town"] == 0).sum()),
        "villages_with_index": int(len(v)),
        "towns": int((joined["is_town"] == 1).sum()),
        "subdistricts": int(len(ov)),
        "districts": int(len(dist)),
        "break_method": "within-state quintiles of basis_risk_index",
        "breaks": [round(b, 4) for b in breaks],
        "edges": [round(e, 4) for e in edges],
        "class_counts": counts,
        "index_min": round(float(v.min()), 4),
        "index_max": round(float(v.max()), 4),
        "index_median": round(float(v.median()), 4),
        "village_zoom": list(VILLAGE_ZOOM),
        "overview_zoom": list(OVERVIEW_ZOOM),
    }
    (WEB_DATA / f"{slug}_meta.json").write_text(json.dumps(meta, indent=2))

    rule("DISTRICT TABLE — mean rainfed share (area-weighted)")
    d = dist[["district_name", "n_villages", "mean_rainfed",
              "mean_rainfed_simple", "mean_index"]].copy()
    d = d.sort_values("mean_rainfed")
    print(f"{'district':<18}{'villages':>9}{'rainfed(aw)':>13}"
          f"{'rainfed(simple)':>17}{'index':>8}")
    for r in d.itertuples():
        print(f"{r.district_name:<18}{r.n_villages:>9,}{r.mean_rainfed:>12.1%}"
              f"{r.mean_rainfed_simple:>16.1%}{r.mean_index:>8.3f}")
    d.to_csv(OUT_DIR / f"{slug}_district_rainfed.csv", index=False)

    rule("DONE")
    print(f"villages archive {n_v / 1e6:,.1f} MB   overview {n_o / 1e6:,.1f} MB")
    print(f"meta: {WEB_DATA / (slug + '_meta.json')}")


if __name__ == "__main__":
    main()

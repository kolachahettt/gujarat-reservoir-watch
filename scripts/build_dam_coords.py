"""
Gujarat Reservoir Watch — dam coordinates from the National Register of Large Dams.

Source: https://www.cwc.gov.in/sites/default/files/nrld-2019.pdf
Central Water Commission, National Register of Large Dams 2019. Gujarat is
pages 79-101 of 300; 632 dams, each with a latitude and a longitude in DMS.

Produces data/reference/dam_coordinates.csv, which build_view_data.py picks up
and which switches the map on. Also writes dam_coordinates_excluded.csv: every
scheme NOT placed, with the reason, because a map that omits 23 of 206 has to
say which 23 and why (§16).

THE RULE THIS SCRIPT EXISTS TO ENFORCE: a dam plotted at a guessed location is
worse than no map. A name resemblance alone never places a dam here. Every
accepted coordinate is corroborated by at least one fact that does not involve
the spelling of the name:

  CAPACITY   WRD publishes design gross storage in MCM; NRLD publishes gross
             storage in m3. Capacity spans four orders of magnitude across the
             206, so agreement within 10% is not something two different dams
             do by coincidence.
  GEOGRAPHY  The NRLD coordinate must fall inside the district WRD assigns the
             scheme to, tested against the 2011 census district polygons via
             the crosswalk. Districts created after 2011 are tested against
             their parent, whose area is a superset — coarser, still sound.

Usage:
  python scripts/build_dam_coords.py              # fetch if absent, then build
  python scripts/build_dam_coords.py --no-fetch   # fail if the PDF is absent
  python scripts/build_dam_coords.py --report     # print the tier table only
"""

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import duckdb
from shapely.geometry import Point, shape
from shapely.ops import nearest_points

NRLD_URL = "https://www.cwc.gov.in/sites/default/files/nrld-2019.pdf"
RAW_DIR = Path("data/raw/nrld")
PDF = RAW_DIR / "nrld-2019.pdf"
PROV = RAW_DIR / "nrld-2019.provenance.json"

DB = Path("data/processed/reservoir.duckdb")
DISTRICTS = Path("web/data/gj_districts.geojson")
# The state outline, used to FLAG rather than reject. One placed dam sits just
# outside it and correctly so: Damanganga's dam is on the river at the Gujarat
# border and was built jointly with the UT of Dadra & Nagar Haveli, so its
# coordinate falls a kilometre beyond the 2011 Valsad boundary. Dropping a
# correct coordinate would be as wrong as inventing one, but a lone dot outside
# the drawn outline reads as a bug, so the count is published and the page
# explains it.
STATE_GEO = Path("web/data/gj_state.geojson")
CROSSWALK = Path("data/reference/gujarat_district_crosswalk_2011_2026.csv")
OUT = Path("data/reference/dam_coordinates.csv")
OUT_EX = Path("data/reference/dam_coordinates_excluded.csv")

STATE = "GJ"                  # NRLD's PIC prefix for Gujarat
GJ_BBOX = (20.0, 24.9, 68.0, 74.9)          # lat lo/hi, lon lo/hi

# --- thresholds, each set against a measured distribution, not by taste -----
# Capacity: among name-matched pairs the disagreement is bimodal. 121 agree
# within 1%, and the next population starts above 13%. 10% sits in the gap.
CAP_TOL = 0.10
# Taluka vs NRLD's "Neareast City" (the source's spelling). Same-name talukas
# score 1.0; the nearest wrong pair measured 0.75.
TAL_TOL = 0.85
# Name stem similarity. Safe to have this low because the ordinal must match
# exactly and a verification must still pass; at 0.90 with neither guard,
# 'Sasoi-II' matched 'Sasoi'.
FUZZ = 0.86
# Boundary slack. Of the accepted points 175 are strictly inside their district
# and 5 within 1.01km. The failures are two populations: four within 6.8km and
# five between 41 and 170km, with nothing in between. 7km separates "my test is
# coarse" from "the source is wrong".
NEAR_KM = 7.0
DIST_BUFFER_DEG = 0.01

ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7}
NOISE = re.compile(r"\b(dam|tank|reservoir|project|weir|scheme|irrigation|"
                   r"stg|stage)\b")
# Ordered longest-first so 'chh' folds before 'ch'. These are transliteration
# conventions, not misspellings: WRD writes Machchhu where NRLD writes Machhu,
# WRD Phodarness where NRLD Fodaraness, WRD Waidy where NRLD Vaidy. Folding to
# a consonant skeleton is what makes those the same key.
FOLD = [("chh", "c"), ("ch", "c"), ("shh", "s"), ("sh", "s"), ("ph", "f"),
        ("th", "t"), ("dh", "d"), ("bh", "b"), ("gh", "g"), ("kh", "k"),
        ("jh", "j"), ("w", "v"), ("y", "i"), ("ee", "i"), ("oo", "u"),
        ("aa", "a"), ("z", "j"), ("q", "k"), ("x", "ks")]
# WRD's district spellings against the crosswalk's. Without these five, 34
# schemes were untestable on geography rather than tested.
DIST_ALIAS = {"kachchh": "kutch", "mahesana": "mehsana", "arvalli": "aravalli",
              "chhotaudepur": "chhota udepur",
              "devbhumi dwarka": "devbhoomi dwarka"}


# --------------------------------------------------------------------- fetch
def fetch(allow):
    if PDF.exists():
        return
    if not allow:
        sys.exit(f"{PDF} absent and --no-fetch given")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    print(f"fetching {NRLD_URL}")
    req = urllib.request.Request(
        NRLD_URL, headers={"User-Agent": "gujarat-reservoir-watch/1.0"})
    with urllib.request.urlopen(req, timeout=300) as r:
        blob = r.read()
    PDF.write_bytes(blob)
    sha = hashlib.sha256(blob).hexdigest()
    PROV.write_text(json.dumps({
        "url": NRLD_URL, "bytes": len(blob), "sha256": sha,
        "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "publication": "CWC National Register of Large Dams 2019",
    }, indent=1), encoding="utf-8")
    print(f"  {len(blob):,} bytes  sha256 {sha[:16]}…")


# ------------------------------------------------------------------- extract
DMS = re.compile(r"^\s*(\d{1,3})\s*°\s*(\d{1,2})?\s*['’]?\s*"
                 r"(\d{1,2}(?:\.\d+)?)?\s*[\"”]?\s*$")


def dms(s):
    """'22° 24' 20"' -> 22.405556. None if it is not a clean DMS value."""
    if not s:
        return None
    t = (s.replace("’", "'").replace("”", '"')
          .replace("''", '"').replace("`", "'"))
    m = DMS.match(re.sub(r"\s+", " ", t).strip())
    if not m:
        return None
    d, mi, se = (float(m.group(1)), float(m.group(2) or 0),
                 float(m.group(3) or 0))
    if mi > 60 or se > 60:
        return None
    # The register writes 60" where it means the next whole minute, on four
    # rows. 60 seconds IS a minute: a notation quirk, not an unreadable value.
    if se == 60:
        se, mi = 0, mi + 1
    if mi == 60:
        mi, d = 0, d + 1
    if mi >= 60 or se >= 60:
        return None
    return round(d + mi / 60 + se / 3600, 6)


def extract_nrld():
    """NRLD Gujarat rows, by geometric reassembly.

    extract_text() CANNOT do this. The Latitude column is 22pt wide, a DMS
    value does not fit, and it wraps to a second baseline inside the row. Line
    reading then interleaves the two columns and the two baselines, so Vijarkhi
    comes out as `22° 24 ' 70° 10 '` followed by `20" 59"` — the seconds for
    BOTH columns after the degrees for both. No regex recovers which second
    belongs to which column, because what distinguishes them is horizontal
    position, and reading by line has already discarded it.

    So: bucket chars into the ruled grid and read each cell on its own. The
    grid is in the page's thin `rects` — this file has no `lines` objects at
    all — so the column edges are measured off the document, not inferred from
    where text happens to start.
    """
    import pdfplumber

    rows = []
    with pdfplumber.open(PDF) as pdf:
        for pno, page in enumerate(pdf.pages):
            xs = sorted({round(r["x0"], 1) for r in page.rects
                         if r["x1"] - r["x0"] < 2})
            ys = sorted({round(r["top"], 1) for r in page.rects
                         if r["bottom"] - r["top"] < 2})
            if len(xs) < 15 or len(ys) < 5:
                continue
            cells = defaultdict(list)
            for c in page.chars:
                cx, cy = (c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2
                col = next((i for i in range(len(xs) - 1)
                            if xs[i] <= cx < xs[i + 1]), None)
                row = next((j for j in range(len(ys) - 1)
                            if ys[j] <= cy < ys[j + 1]), None)
                if col is not None and row is not None:
                    cells[(row, col)].append(c)
            txt = {}
            for k, cs in cells.items():
                # top-to-bottom then left-to-right, baselines grouped to 1.5pt
                # so two chars on one visual line stay together.
                cs.sort(key=lambda c: (round(c["top"] / 1.5), c["x0"]))
                txt[k] = "".join(c["text"] for c in cs).strip()

            nrow = len(ys) - 1
            # The PIC column is whichever one holds codes like GJ04LH0001.
            # Found rather than assumed, so a layout change fails loudly.
            piccol = next(
                (col for col in range(len(xs) - 1)
                 if sum(1 for r in range(nrow)
                        if re.fullmatch(r"[A-Z]{2}\d{2}[A-Z]{2}\d{4}",
                                        txt.get((r, col), ""))) >= 3), None)
            if piccol is None:
                continue
            if not any(txt.get((r, piccol), "").startswith(STATE)
                       for r in range(nrow)):
                continue
            # Column identity from the page's own header, not from offsets.
            hdr = next((r for r in range(nrow)
                        if "latitude" in " ".join(
                            txt.get((r, c), "") for c in range(len(xs) - 1)
                        ).lower()), None)
            idx = {}
            if hdr is not None:
                for c in range(len(xs) - 1):
                    v = txt.get((hdr, c), "").lower()
                    for field, test in (("lat", "latitude"),
                                        ("lon", "longitude"),
                                        ("name", "name of dam"),
                                        ("city", "city"),
                                        ("gross", "gross storage")):
                        if v.startswith(test) or test in v:
                            idx.setdefault(field, c)
            idx.setdefault("name", piccol + 1)
            idx.setdefault("lat", piccol + 3)
            idx.setdefault("lon", piccol + 4)
            idx.setdefault("city", piccol + 8)
            idx.setdefault("gross", piccol + 14)

            for r in range(nrow):
                pic = txt.get((r, piccol), "")
                if not re.fullmatch(rf"{STATE}\d{{2}}[A-Z]{{2}}\d{{4}}", pic):
                    continue
                lat, lon = dms(txt.get((r, idx["lat"]), "")), \
                    dms(txt.get((r, idx["lon"]), ""))
                g = txt.get((r, idx["gross"]), "").replace(",", "")
                rows.append({
                    "pic": pic, "page": pno,
                    "name": txt.get((r, idx["name"]), ""),
                    "lat": lat, "lon": lon,
                    "city": txt.get((r, idx["city"]), ""),
                    "mcm": float(g) / 1e6 if re.fullmatch(r"\d+(\.\d+)?", g) else None,
                })
    return rows


# --------------------------------------------------------------------- names
def split_name(s):
    """-> (stem, ordinal). The ordinal is what separates one dam in a numbered
    series from another: a roman numeral, a digit, or a parenthetical letter.
    It is matched EXACTLY, never fuzzily — a near miss on the stem is a
    spelling variant, a mismatch on the ordinal is a different dam."""
    t = (s or "").lower().strip().replace("&", " and ")
    t = re.sub(r"[.–—]", "-", t)
    t = re.sub(r"\(([^)]*)\)",
               lambda m: " " if len(m.group(1)) > 2 else m.group(0), t)
    ordinal, paren = "", False
    m = re.search(r"\(\s*([a-z])\s*\)\s*$", t)
    if m:
        ordinal, paren, t = m.group(1), True, t[:m.start()]
    m = re.search(r"[-\s]*\b(i{1,3}|iv|vi{0,2}|v)\b\s*$", t)
    if m:
        ordinal, t = str(ROMAN[m.group(1)]), t[:m.start()]
    else:
        m = re.search(r"[-\s]+([1-7])\s*$", t)
        if m:
            ordinal, t = m.group(1), t[:m.start()]
    return re.sub(r"[^a-z0-9]+", "", NOISE.sub(" ", t)), ordinal, paren


def fold(stem):
    t = stem
    for a, b in FOLD:
        t = t.replace(a, b)
    return re.sub(r"[aeiou]", "", re.sub(r"(.)\1+", r"\1", t))


def ratio(a, b):
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------- geography
def load_state():
    from shapely.ops import unary_union
    gj = json.loads(STATE_GEO.read_text(encoding="utf-8"))
    return unary_union([shape(f["geometry"]) for f in gj["features"]])


def load_geo():
    gj = json.loads(DISTRICTS.read_text(encoding="utf-8"))
    polys = {f["properties"]["district_name"].strip().lower(): shape(f["geometry"])
             for f in gj["features"]}
    xw = {}
    for r in csv.DictReader(CROSSWALK.open(encoding="utf-8")):
        ps = [r["pc11_district_name"].strip()]
        if r["other_pc11_parents"]:
            ps += [p.strip() for p in r["other_pc11_parents"].split(",")
                   if p.strip()]
        xw[r["imd_district"].strip().lower()] = ps
    return polys, xw


def km_outside(polys, xw, lat, lon, district):
    """0.0 inside; kilometres to the nearest edge outside; None untestable.

    Distance, not in/out: a point 3km beyond a generalised 2011 polygon for a
    district redrawn after 2011 is a limit of the test, and one 75km beyond is
    the source being wrong. Returning a boolean would report the first as the
    second.
    """
    key = (district or "").strip().lower()
    ps = xw.get(DIST_ALIAS.get(key, key))
    if not ps:
        return None
    pt, best = Point(lon, lat), None
    for p in ps:
        poly = polys.get(p.strip().lower())
        if poly is None:
            continue
        if poly.buffer(DIST_BUFFER_DEG).contains(pt):
            return 0.0
        a, b = nearest_points(poly, pt)
        d = math.hypot((a.x - b.x) * 111.32 * math.cos(math.radians(lat)),
                       (a.y - b.y) * 110.57)
        best = d if best is None else min(best, d)
    return best


# ------------------------------------------------------------------- matching
def build():
    nrld = [r for r in extract_nrld() if r["lat"] is not None
            and r["lon"] is not None]
    lo_la, hi_la, lo_lo, hi_lo = GJ_BBOX
    off = [r for r in nrld
           if not (lo_la <= r["lat"] <= hi_la and lo_lo <= r["lon"] <= hi_lo)]
    if off:
        # Never seen in the 2019 edition. If it starts happening the geometry
        # has moved and the extraction is no longer trustworthy.
        sys.exit(f"FAIL {len(off)} NRLD coordinates outside Gujarat: "
                 f"{[r['name'] for r in off][:5]}")
    print(f"NRLD Gujarat dams with coordinates: {len(nrld)}")
    for r in nrld:
        r["stem"], r["ord"], _ = split_name(r["name"])
        r["fold"] = fold(r["stem"])

    con = duckdb.connect(str(DB), read_only=True)
    wrd = [dict(scheme_id=a, name=b, district=c, taluka=d, mcm=e)
           for a, b, c, d, e in con.execute("""
               SELECT scheme_id, scheme_name_latest, district_latest,
                      taluka_latest, design_gross_mcm_max
               FROM dim_scheme ORDER BY scheme_id""").fetchall()]
    polys, xw = load_geo()

    by_key = defaultdict(list)
    for r in nrld:
        by_key[(r["stem"], r["ord"])].append(r)

    accepted, excluded = [], []
    for w in wrd:
        stem, ordn, paren = split_name(w["name"])
        wf = fold(stem)
        cands = list(by_key.get((stem, ordn), []))
        how = "exact stem+ordinal"
        if not cands:
            pool = [r for r in nrld if r["ord"] == ordn]
            if paren:
                pool += [r for r in nrld if r["ord"] == ""]
            scored = sorted(((max(ratio(stem, r["stem"]), ratio(wf, r["fold"])), r)
                             for r in pool), key=lambda t: -t[0])
            if scored and scored[0][0] >= FUZZ:
                cands = [r for s, r in scored if s >= FUZZ]
                how = f"folded stem {scored[0][0]:.2f}, ordinal exact"
            else:
                excluded.append((w, "no NRLD row found",
                                 f"best name similarity "
                                 f"{scored[0][0]:.2f} to "
                                 f"{scored[0][1]['name']!r}" if scored else ""))
                continue
        best = None
        for c in cands:
            cap = (abs(c["mcm"] - w["mcm"]) / max(c["mcm"], w["mcm"])
                   if c["mcm"] and w["mcm"] else None)
            km = km_outside(polys, xw, c["lat"], c["lon"], w["district"])
            tal = (ratio(re.sub(r"[^a-z]", "", (w["taluka"] or "").lower()),
                         re.sub(r"[^a-z]", "", (c["city"] or "").lower()))
                   if w["taluka"] and c["city"] else None)
            rank = (1 if km == 0.0 else 0, -(cap if cap is not None else 9),
                    tal or 0)
            if best is None or rank > best[0]:
                best = (rank, c, cap, km, tal)
        _, c, cap, km, tal = best
        cap_ok = cap is not None and cap <= CAP_TOL
        tal_ok = tal is not None and tal >= TAL_TOL

        if paren and not cap_ok:
            excluded.append((w, "parenthetical variant, capacity disagrees",
                             f"{c['name']!r} differs by "
                             f"{'n/a' if cap is None else format(cap, '.1%')}"))
            continue
        exact = how.startswith("exact")
        if km is None:
            tier = "B2" if cap_ok else None
        elif km == 0.0:
            # Tier C has only district containment behind it, and a district
            # holds many dams — that is the same weak evidence OSM was
            # rejected on, so it cannot also carry a FOLDED name. The fold is a
            # hypothesis about transliteration; corroborated by capacity or
            # taluka it is fine, uncorroborated it is a guess. It let
            # 'WNKL.-BHEY' reach 'Vankol' (31x the capacity) and 'Bantva-Kharo'
            # reach 'Bantwakharo' (100x).
            if cap_ok:
                tier = "A"
            elif tal_ok:
                tier = "B"
            elif exact:
                tier = "C"
            else:
                tier = None
        elif km <= NEAR_KM and cap_ok:
            tier = "A-bdy"
        else:
            reason = ("NRLD coordinate contradicts the district"
                      if km > NEAR_KM else
                      "near the district edge and capacity disagrees")
            excluded.append((w, reason,
                             f"{c['name']!r} at {c['lat']:.4f},{c['lon']:.4f} "
                             f"is {km:.0f} km outside {w['district']}, "
                             f"capacity differs by "
                             f"{'n/a' if cap is None else format(cap, '.1%')}"))
            continue
        if tier is None:
            excluded.append((w, "matched but nothing corroborates it",
                             f"{c['name']!r}: "
                             + ("district untestable" if km is None
                                else "name match is a transliteration fold")
                             + ", taluka does not match, and capacity differs "
                             f"by {'n/a' if cap is None else format(cap, '.1%')}"))
            continue
        accepted.append({
            "scheme_id": w["scheme_id"], "lat": c["lat"], "lon": c["lon"],
            "source": "CWC NRLD 2019", "tier": tier, "match": how,
            "nrld_pic": c["pic"], "nrld_name": c["name"],
            "cap_diff_pct": "" if cap is None else round(cap * 100, 2),
            "km_outside_district": "" if km is None else round(km, 2),
            "taluka_similarity": "" if tal is None else round(tal, 3),
            "outside_state_outline": "",      # filled in below
        })

    # ONE NRLD DAM CANNOT BE TWO SCHEMES. WRD lists both 'Ozat-Weir' and
    # 'Ozat- Weir(Vanthali)' in Vanthali taluka at 1.9 and 1.8 MCM, and NRLD
    # has a single 'Ozat Weir (Vanthali)' at 1.8. Placing both puts two dots on
    # one pixel and asserts a position for a scheme that has not earned one, so
    # the better-evidenced claim keeps the row and the other is excluded with
    # the reason. Ranked by capacity agreement first, then taluka.
    claims = defaultdict(list)
    for a in accepted:
        claims[a["nrld_pic"]].append(a)
    for pic, group in claims.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda a: (
            float(a["cap_diff_pct"]) if a["cap_diff_pct"] != "" else 1e9,
            -(float(a["taluka_similarity"]) if a["taluka_similarity"] != "" else 0)))
        keep = group[0]
        for loser in group[1:]:
            w = next(x for x in wrd if x["scheme_id"] == loser["scheme_id"])
            accepted.remove(loser)
            excluded.append((
                w, "NRLD row already claimed by a closer match",
                f"{loser['nrld_name']!r} is also scheme "
                f"{keep['scheme_id']}, which agrees on capacity to "
                f"{keep['cap_diff_pct']}% against this one's "
                f"{loser['cap_diff_pct']}%"))
            print(f"  note: scheme {loser['scheme_id']} released "
                  f"{pic} to scheme {keep['scheme_id']}")

    # Flag, do not drop: a dam on the state border has a correct coordinate
    # that falls outside the outline the map draws.
    state = load_state()
    n_out_state = 0
    for a in accepted:
        if not state.contains(Point(a["lon"], a["lat"])):
            a["outside_state_outline"] = "yes"
            n_out_state += 1
            print(f"  note: {a['nrld_name']!r} ({a['lat']:.4f},{a['lon']:.4f}) "
                  f"is outside the Gujarat outline — drawn anyway, the "
                  f"coordinate is the register's and the dam is on the border")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        wtr = csv.DictWriter(f, fieldnames=list(accepted[0].keys()))
        wtr.writeheader()
        wtr.writerows(accepted)
    with OUT_EX.open("w", newline="", encoding="utf-8") as f:
        wtr = csv.writer(f)
        wtr.writerow(["scheme_id", "scheme_name", "district", "design_gross_mcm",
                      "reason", "detail"])
        for w, reason, detail in excluded:
            wtr.writerow([w["scheme_id"], w["name"], w["district"],
                          w["mcm"], reason, detail])

    tiers = defaultdict(int)
    for a in accepted:
        tiers[a["tier"]] += 1
    reasons = defaultdict(int)
    for _, reason, _ in excluded:
        reasons[reason] += 1
    print(f"\nplaced {len(accepted)} of {len(wrd)} "
          f"({len(accepted) / len(wrd):.1%})")
    for k in sorted(tiers):
        print(f"    {tiers[k]:>4}  tier {k}")
    print(f"not placed {len(excluded)}")
    for k in sorted(reasons):
        print(f"    {reasons[k]:>4}  {k}")
    mcm_in = sum(a and w["mcm"] for a, w in
                 zip([1] * len(accepted),
                     [next(x for x in wrd if x["scheme_id"] == a["scheme_id"])
                      for a in accepted]))
    mcm_all = sum(w["mcm"] for w in wrd)
    print(f"design capacity represented: {mcm_in:,.0f} of {mcm_all:,.0f} MCM "
          f"({mcm_in / mcm_all:.1%})")
    print(f"\nwritten {OUT}  and  {OUT_EX}")
    return accepted, excluded


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--report", action="store_true",
                    help="print the tier table from the existing CSV and exit")
    a = ap.parse_args()
    if a.report:
        if not OUT.exists():
            sys.exit(f"{OUT} does not exist")
        rows = list(csv.DictReader(OUT.open(encoding="utf-8")))
        t = defaultdict(int)
        for r in rows:
            t[r["tier"]] += 1
        print(f"{len(rows)} placed")
        for k in sorted(t):
            print(f"  {t[k]:>4}  tier {k}")
        sys.exit(0)
    fetch(not a.no_fetch)
    build()

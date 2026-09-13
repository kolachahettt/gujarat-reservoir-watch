"""
Gujarat Reservoir Watch — culturable command area per scheme, verified.

WHAT THIS ANSWERS. The daily report says how much water is in each reservoir
and never says who depends on it. The command area is the missing half: the
cultivable land a scheme is built to irrigate. NRLD does not carry it — all 20
of its columns were checked — and neither does the live WRD daily-report host,
which serves only daily PDFs. It exists in exactly one place: the NWRWS
department's own **Data Bank**, one page per dam, at

    https://guj-nwrws.gujarat.gov.in/showpage.aspx?contentid=<id>

indexed from contentid=1467. Each page carries Gross Command Area, Cultivable
Command Area, "Maximum irrigation" with the year it was achieved, canal lengths
and discharges, year of completion, the Major/Medium/Minor class, and the
benefited villages by taluka. 138 dams are indexed; 136 have a page.

WHY IT NEEDS THREE CHECKS. The source is not reliable row by row, and a naive
read produces figures that are impossible on their face (CCA greater than GCA).
Each page prints GCA/CCA TWICE, in a "Details Of Canal" table and a "Command
Area" table built independently, and:

  1. The table can be about a different dam. 9 of 102 pages that parse carry
     another scheme's row, and the dangerous ones are ordinal slips — the page
     indexed `Bhogavo-2` holds Bhogavo-1's row, `Machchundri-2` holds
     Machchundri's, `Raval-2` holds Raval's, `Kharo` holds Kharo-1's. Accepting
     those would attach one dam's command to its neighbour.

  2. The page can contradict itself. 6 pages disagree between their own two
     tables, and the disagreements are plainly duplicated rows: 'Umariya' prints
     Ukai's 66,168 ha in its second table, 'Khambhda' prints Umariya's
     4,148/2,192 in its first. Shetrunji disagrees with itself by 23x — 57,060
     against 2,514 ha. NEITHER table can be preferred a priori, because
     Umariya's canal table is the right one and Khambhda's is not, so a page
     that disagrees with itself yields nothing. Picking a side would be a guess.

  3. Two schemes can share one row. Machchhu-I and Machchhu-II both come out
     18,218/10,409/7,709; Godhatad and Waidy both 6,014/1,328/0. An identical
     (GCA, CCA, max-irrigated) triple across two schemes means at least one is
     a copy, and nothing on the pages says which.

Then the surviving rows are matched to the 206 monitored schemes on NAME AND
DISTRICT, for the same reason the coordinates were: matching on name alone put
'Dhari' (3.01 MCM, Rajkot) onto Dharoi's page (813 MCM, Mahesana) at 0.89
similarity, and in doing so consumed the page the real Dharoi needed.

40 of 206 schemes survive all of it. That is the honest number and the page
states it.

ONE MORE TRAP. `Maximum irrigation = 0` with a BLANK YEAR means NOT RECORDED,
not zero hectares. All 31 real figures carry a year; all 9 zeros are blank.
Publishing those as "has never irrigated anything" would be a false claim about
six Kutch schemes.

Usage:
  python scripts/build_command_area.py              # uses the cached pages
  python scripts/build_command_area.py --fetch      # fetch anything missing
Outputs:
  data/reference/scheme_command_area.csv            # the 40, with evidence
  data/reference/scheme_command_area_excluded.csv   # every rejection + reason
"""

import argparse
import csv
import difflib
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_dam_coords import FOLD, NOISE, split_name  # noqa: E402

RAW = Path("data/raw/nwrws")
PAGES = RAW / "pages"
INDEX = RAW / "index_command_area.html"
VIEW = Path("web/data/reservoir_view.json")
OUT = Path("data/reference/scheme_command_area.csv")
OUT_EX = Path("data/reference/scheme_command_area_excluded.csv")

BASE = "https://guj-nwrws.gujarat.gov.in/showpage.aspx"
INDEX_ID = 1467
UA = {"User-Agent": "gujarat-reservoir-watch/1.0 (research; contact via repo)"}
DELAY_S = 1.0

# Name fuzz for the stem only; the ordinal is always exact. Same threshold the
# coordinate matcher uses, and for the same reason.
FUZZ = 0.86
# Two capacities/areas are the same figure when within this of each other.
EPS_REL = 0.01

# WRD's district spellings against the Data Bank's. Both are Gujarat
# government, but the Data Bank pages are older and transliterate differently.
# Post-2011 districts map to the parent they were carved from, because the Data
# Bank pages predate the split.
DALIAS = {
    "kachchh": "kutch", "mahesana": "mehsana", "panchamahal": "panchmahal",
    "panch mahals": "panchmahal", "vadodra": "vadodara",
    "chhotaudepur": "chhota udepur", "chota udeypur": "chhota udepur",
    "devbhumi dwarka": "jamnagar", "devbhoomi dwarka": "jamnagar",
    "gir somnath": "junagadh", "morbi": "rajkot", "botad": "bhavnagar",
    "mahisagar": "panchmahal", "arvalli": "sabarkantha",
    "aravalli": "sabarkantha", "sabar kantha": "sabarkantha",
    "banas kantha": "banaskantha", "ahmadabad": "ahmedabad",
    "dohad": "dahod", "surendra nagar": "surendranagar",
}


def dnorm(s):
    t = " ".join((s or "").lower().split())
    return DALIAS.get(t, t)


def fold(s):
    t = NOISE.sub(" ", (s or "").lower())
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = " ".join(t.split())
    for a, b in FOLD:
        t = t.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", t).replace(" ", "")


def key(s):
    stem, ordinal, _ = split_name(s)
    return fold(stem), (ordinal or "")


def num(s):
    s = (s or "").replace(",", "").strip().rstrip(".")
    return float(s) if re.fullmatch(r"\d+(\.\d+)?", s) else None


# ------------------------------------------------------------------ fetching
def fetch_index(allow):
    if INDEX.exists():
        return INDEX.read_text("utf-8", errors="replace")
    if not allow:
        sys.exit(f"{INDEX} absent and --fetch not given")
    RAW.mkdir(parents=True, exist_ok=True)
    r = requests.get(f"{BASE}?contentid={INDEX_ID}&lang=English",
                     headers=UA, timeout=90)
    r.raise_for_status()
    INDEX.write_text(r.text, encoding="utf-8")
    return r.text


def dam_index(html):
    """-> [{contentid, name}]. The dam links live in div#basin0..N, grouped by
    river basin; everything else on the page is site chrome."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for div in soup.find_all("div", id=re.compile(r"^basin\d+$")):
        for a in div.find_all("a", href=re.compile(r"contentid=\d+")):
            cid = int(re.search(r"contentid=(\d+)", a["href"]).group(1))
            nm = " ".join(a.get_text().split())
            if nm and cid not in seen:
                seen.add(cid)
                out.append({"contentid": cid, "name": nm})
    return out


def fetch_pages(index, allow):
    PAGES.mkdir(parents=True, exist_ok=True)
    missing = [d for d in index
               if not (PAGES / f"{d['contentid']}.html").exists()]
    if missing and not allow:
        print(f"  {len(missing)} page(s) not cached and --fetch not given; "
              f"they will be reported as absent")
        return
    for i, d in enumerate(missing, 1):
        try:
            r = requests.get(f"{BASE}?contentid={d['contentid']}&lang=English",
                             headers=UA, timeout=90)
            r.raise_for_status()
            (PAGES / f"{d['contentid']}.html").write_text(r.text,
                                                          encoding="utf-8")
        except Exception as e:
            print(f"  FAILED {d['contentid']} {d['name']}: {str(e)[:60]}")
        time.sleep(DELAY_S)
        if i % 20 == 0:
            print(f"  fetched {i}/{len(missing)}")


# ------------------------------------------------------------------- parsing
def leaf_tables(node):
    """Innermost tables only. Each table is nested inside several wrappers, so
    an unfiltered find_all matches a container and reads the wrong row — that
    is what produced 'the page titled Juj says Kadana' on the first pass."""
    for tb in node.find_all("table"):
        if tb.find("table") is None:
            yield tb


def parse_page(cid, name, html):
    """One page -> a record, with both tables' figures kept separately so they
    can be compared against each other."""
    soup = BeautifulSoup(html, "html.parser")
    content = soup.find("div", id="ContentCTL1_lblContent") or soup
    r = {"contentid": cid, "index_name": name, "canal_name": "", "river": "",
         "taluka": "", "district": "", "year_completed": "", "gca_ha": None,
         "cca_ha": None, "maxirr_year": "", "maxirr_ha": None,
         "cmd_name": "", "cmd_gca_ha": None, "cmd_cca_ha": None,
         "scheme_type": "", "n_villages": 0}
    for tb in leaf_tables(content):
        trs = tb.find_all("tr")
        if len(trs) < 3:
            continue
        h = [" ".join(c.get_text().split())
             for c in trs[0].find_all(["td", "th"])]
        if not (h and h[0].lower().startswith("name of scheme")):
            continue
        d = [" ".join(c.get_text().split())
             for c in trs[2].find_all(["td", "th"])]
        # 13 fixed columns: name river taluko district L-len L-dis R-len R-dis
        # yearCompleted GCA CCA maxIrrYear maxIrrArea
        if any("river" in x.lower() for x in h) and len(d) >= 13:
            r.update(canal_name=d[0], river=d[1], taluka=d[2], district=d[3],
                     year_completed=d[8], gca_ha=num(d[9]), cca_ha=num(d[10]),
                     maxirr_year=d[11], maxirr_ha=num(d[12]))
        # 7 fixed columns: name taluko district GCA CCA maxIrrYear maxIrrArea
        elif any("c.c.a" in x.lower() for x in h) and len(d) >= 5:
            r.update(cmd_name=d[0], cmd_gca_ha=num(d[3]), cmd_cca_ha=num(d[4]))
        elif any("type of scheme" in x.lower() for x in h):
            for tr in trs[2:]:
                for x in (" ".join(c.get_text().split())
                          for c in tr.find_all(["td", "th"])):
                    if x.lower() in ("major", "medium", "minor"):
                        r["scheme_type"] = r["scheme_type"] or x
                    if x.count(",") >= 1 and len(x) > 12:
                        r["n_villages"] += len([v for v in x.split(",")
                                                if v.strip()])
    return r


def judge(r):
    """The three checks. Returns (usable, reason) — reason is '' when usable."""
    if not r["canal_name"]:
        return False, "no command-area table on the page"
    if fold(r["canal_name"]) != fold(r["index_name"]):
        return False, (f"the page's table is about a different dam "
                       f"('{r['canal_name']}', {r['district']})")
    if r["gca_ha"] is None or r["cca_ha"] is None:
        return False, "GCA or CCA missing from the table"
    # A CCA of zero is not a command area of zero hectares, it is a blank cell
    # the parser read as a number. Tappar (GCA 8,090, CCA 0) and Und-I
    # (GCA 10,940, CCA 0) are both real irrigation schemes; publishing them as
    # commanding nothing would be a false claim, not a conservative one.
    if r["cca_ha"] <= 0 or r["gca_ha"] <= 0:
        return False, (f"CCA or GCA is zero (GCA {r['gca_ha']:,.0f}, CCA "
                       f"{r['cca_ha']:,.0f}) — a blank cell, not an area")
    if r["cca_ha"] > r["gca_ha"]:
        return False, (f"CCA {r['cca_ha']:,.0f} exceeds GCA "
                       f"{r['gca_ha']:,.0f}, which cannot be")
    if (r["maxirr_ha"] or 0) > r["cca_ha"] * 1.05:
        return False, (f"maximum irrigated {r['maxirr_ha']:,.0f} exceeds CCA "
                       f"{r['cca_ha']:,.0f}")
    if r["cmd_cca_ha"] is None:
        return False, "only one of the two tables carries a CCA"
    if abs(r["cca_ha"] - r["cmd_cca_ha"]) > EPS_REL * max(r["cca_ha"], 1):
        return False, (f"the page disagrees with itself: canal table "
                       f"{r['cca_ha']:,.0f} ha vs command table "
                       f"{r['cmd_cca_ha']:,.0f} ha")
    return True, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true",
                    help="fetch the index and any uncached dam page")
    args = ap.parse_args()

    index = dam_index(fetch_index(args.fetch))
    print(f"Data Bank index: {len(index)} dams")
    fetch_pages(index, args.fetch)

    recs, excluded = [], []
    for d in index:
        p = PAGES / f"{d['contentid']}.html"
        if not p.exists():
            excluded.append({"scheme_id": "", "scheme_name": "",
                             "district": "", "nwrws_name": d["name"],
                             "contentid": d["contentid"],
                             "reason": "page not retrieved", "detail": ""})
            continue
        r = parse_page(d["contentid"], d["name"],
                       p.read_text("utf-8", errors="replace"))
        ok, why = judge(r)
        if ok:
            recs.append(r)
        else:
            excluded.append({"scheme_id": "", "scheme_name": "",
                             "district": "", "nwrws_name": d["name"],
                             "contentid": d["contentid"],
                             "reason": "source row rejected", "detail": why})
    print(f"  pages parsed to a usable CCA: {len(recs)} of {len(index)}")

    # ---- check 3: an identical triple across two pages means a copy --------
    triples = {}
    for r in recs:
        triples.setdefault((r["gca_ha"], r["cca_ha"], r["maxirr_ha"]),
                           []).append(r)
    dupe_ids = set()
    for t, rs in triples.items():
        if len(rs) > 1:
            names = ", ".join(f"{x['index_name']} ({x['contentid']})"
                              for x in rs)
            for x in rs:
                dupe_ids.add(x["contentid"])
                excluded.append({
                    "scheme_id": "", "scheme_name": "", "district": "",
                    "nwrws_name": x["index_name"],
                    "contentid": x["contentid"],
                    "reason": "row duplicated across schemes",
                    "detail": (f"GCA {t[0]:,.0f} / CCA {t[1]:,.0f} / "
                               f"max {(t[2] or 0):,.0f} also appears for: "
                               f"{names}")})
    recs = [r for r in recs if r["contentid"] not in dupe_ids]
    print(f"  after dropping duplicated triples: {len(recs)}")

    # ---- match to the 206, on name AND district ---------------------------
    view = json.loads(VIEW.read_text("utf-8"))
    schemes = view["schemes"]
    by_key = {}
    for r in recs:
        by_key.setdefault(key(r["index_name"]), []).append(r)

    rows, taken = [], set()
    for s in schemes:
        k = key(s["scheme_name"])
        sd = dnorm(s["district"])
        pick, tier, score = None, "", 0.0
        for r in by_key.get(k, []):
            if r["contentid"] in taken:
                continue
            if dnorm(r["district"]) == sd:
                pick, tier, score = r, "A", 1.0
                break
        if pick is None:
            best, bs = None, 0.0
            for r in recs:
                if r["contentid"] in taken:
                    continue
                rk = key(r["index_name"])
                if rk[1] != k[1]:          # ordinal must match exactly
                    continue
                q = difflib.SequenceMatcher(None, k[0], rk[0]).ratio()
                if q > bs:
                    best, bs = r, q
            if best is not None and bs >= FUZZ \
                    and dnorm(best["district"]) == sd:
                pick, tier, score = best, "B", bs
            elif k in by_key:
                r = next((x for x in by_key[k]
                          if x["contentid"] not in taken), None)
                if r is not None:
                    excluded.append({
                        "scheme_id": s["scheme_id"],
                        "scheme_name": s["scheme_name"],
                        "district": s["district"],
                        "nwrws_name": r["index_name"],
                        "contentid": r["contentid"],
                        "reason": "name matched but district disagreed",
                        "detail": (f"scheme is in {s['district']}, the page "
                                   f"says {r['district']}")})
        if pick is None:
            continue
        taken.add(pick["contentid"])
        # max irrigated: 0 with a BLANK YEAR means not recorded. Publishing it
        # as zero would assert six Kutch schemes have never irrigated anything.
        mi = pick["maxirr_ha"]
        recorded = bool(pick["maxirr_year"].strip()) and mi is not None \
            and mi > 0
        rows.append({
            "scheme_id": s["scheme_id"], "scheme_name": s["scheme_name"],
            "district": s["district"], "region": s["region"],
            "gca_ha": round(pick["gca_ha"]), "cca_ha": round(pick["cca_ha"]),
            "max_irrigated_ha": round(mi) if recorded else "",
            "max_irrigated_year": pick["maxirr_year"] if recorded else "",
            "scheme_class": pick["scheme_type"],
            "year_completed": pick["year_completed"],
            "river": pick["river"], "n_command_villages": pick["n_villages"],
            "source": "Gujarat NWRWS Data Bank",
            "contentid": pick["contentid"],
            "nwrws_name": pick["index_name"],
            "match_tier": tier, "name_score": round(score, 3),
        })

    matched_ids = {r["scheme_id"] for r in rows}
    for s in schemes:
        if s["scheme_id"] not in matched_ids:
            if any(e.get("scheme_id") == s["scheme_id"] for e in excluded):
                continue
            excluded.append({
                "scheme_id": s["scheme_id"],
                "scheme_name": s["scheme_name"], "district": s["district"],
                "nwrws_name": "", "contentid": "",
                "reason": "no verified command-area page",
                "detail": "no Data Bank page survived the checks for this name"})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: -r["cca_ha"]))
    with OUT_EX.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["scheme_id", "scheme_name",
                                          "district", "nwrws_name",
                                          "contentid", "reason", "detail"])
        w.writeheader()
        w.writerows(excluded)

    cca = sum(r["cca_ha"] for r in rows)
    mi_rows = [r for r in rows if r["max_irrigated_ha"] != ""]
    mi = sum(r["max_irrigated_ha"] for r in mi_rows)
    mi_cca = sum(r["cca_ha"] for r in mi_rows)
    print(f"\nwritten {OUT}  ({len(rows)} of {len(schemes)} schemes, "
          f"{len(rows)/len(schemes):.1%})")
    print(f"  culturable command area   {cca:>9,} ha")
    print(f"  max ever irrigated        {mi:>9,} ha across {len(mi_rows)} "
          f"schemes = {mi/mi_cca:.0%} of THEIR {mi_cca:,} ha")
    print(f"  {len(rows)-len(mi_rows)} scheme(s) have no recorded maximum "
          f"(blank year), published as empty not zero")
    print(f"written {OUT_EX} ({len(excluded)} rejections with reasons)")
    by_tier = {}
    for r in rows:
        by_tier[r["match_tier"]] = by_tier.get(r["match_tier"], 0) + 1
    print(f"  match tiers: {by_tier}")


if __name__ == "__main__":
    main()

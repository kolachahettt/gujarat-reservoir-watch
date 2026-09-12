"""
Fetch and parse IMD district-wise cumulative rainfall departure for Gujarat.

Source (stable URL, no session token, refreshed daily):
  https://mausam.imd.gov.in/Rainfall/DISTRICT_RAINFALL_DISTRIBUTION_COUNTRY_INDIA_cd.pdf
  India Meteorological Department, Hydromet Division, New Delhi.

The PDF carries its own DAY and PERIOD line; both are captured alongside the
fetch timestamp and the file hash, because a rainfall figure with no date is
useless and a departure figure with no period is misleading (§9.2).

Columns in the source, given twice — daily first, then cumulative:
  MET.SUBDIVISION/UT/STATE/DISTRICT | ACTUAL(mm) | NORMAL(mm) | %DEP. | CAT.

CAT. is IMD's own category, which is what the watchlist uses rather than a
threshold of ours:
  LE large excess | E excess | N normal | D deficient | LD large deficient
  NR no rain | ND no data

Usage:  python scripts/fetch_imd.py [--state GUJARAT]
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

URL = ("https://mausam.imd.gov.in/Rainfall/"
       "DISTRICT_RAINFALL_DISTRIBUTION_COUNTRY_INDIA_cd.pdf")
RAW_DIR = Path("data/raw/imd")
OUT_CSV = Path("data/processed/imd_district_rainfall.csv")
OUT_META = Path("data/processed/imd_provenance.json")

# A numbered district row: rank, NAME, then two groups of actual/normal/%dep/cat.
# %dep and cat are absent when IMD reports ND (no data), so both groups are
# matched loosely and validated afterwards rather than assumed present.
ROW = re.compile(
    r"^(\d+)\s+([A-Z][A-Z&.\-'()/ ]+?)\s+"
    r"([\d.]+)\s+([\d.]+|ND)\s*(-?\d+%)?\s*([A-Z]{1,2})?\s+"
    r"([\d.]+)\s+([\d.]+|ND)\s*(-?\d+%)?\s*([A-Z]{1,2})?\s*$"
)
# A header line: an unnumbered name followed by numbers (state, UT or subdivision).
HEADER = re.compile(r"^([A-Z][A-Z&.\-'()/ ]+?)\s+([\d.]+)\s+")


def download():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    # IMD's chain does not validate in this environment; the artefact is a
    # public PDF whose hash we record, so the content is auditable even though
    # the transport is not verified. Documented rather than silently disabled.
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    fetched = datetime.now(timezone.utc)
    r = requests.get(URL, timeout=180, verify=False)
    r.raise_for_status()
    blob = r.content
    sha = hashlib.sha256(blob).hexdigest().upper()
    path = RAW_DIR / f"district_rainfall_{fetched:%Y%m%d}.pdf"
    path.write_bytes(blob)
    print(f"downloaded {len(blob):,} bytes -> {path}")
    print(f"sha256 {sha}")
    return path, fetched, sha, len(blob)


def parse(path, want_state):
    import pdfplumber
    day = period = None
    owner = None
    rows = []
    with pdfplumber.open(str(path)) as pdf:
        npages = len(pdf.pages)
        for page in pdf.pages:
            for line in (page.extract_text() or "").splitlines():
                s = " ".join(line.split())
                if day is None:
                    m = re.search(r"DAY:\s*([\d-]+)\s*PERIOD:\s*([\d-]+)\s*to\s*([\d-]+)", s)
                    if m:
                        day, period = m.group(1), (m.group(2), m.group(3))
                        continue
                m = ROW.match(s)
                if m:
                    if owner == want_state:
                        rows.append({
                            "rank": int(m.group(1)),
                            "imd_district": m.group(2).strip(),
                            "daily_actual_mm": float(m.group(3)),
                            "daily_normal_mm": None if m.group(4) == "ND" else float(m.group(4)),
                            "daily_dep_pct": None if not m.group(5) else int(m.group(5).rstrip("%")),
                            "daily_cat": m.group(6),
                            "cum_actual_mm": float(m.group(7)),
                            "cum_normal_mm": None if m.group(8) == "ND" else float(m.group(8)),
                            "cum_dep_pct": None if not m.group(9) else int(m.group(9).rstrip("%")),
                            "cum_cat": m.group(10),
                        })
                    continue
                h = HEADER.match(s)
                if h:
                    owner = h.group(1).strip()
    return rows, day, period, npages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="GUJARAT")
    ap.add_argument("--pdf", default=None, help="skip download, parse a local file")
    args = ap.parse_args()

    if args.pdf:
        path, fetched, sha, nbytes = Path(args.pdf), None, None, None
    else:
        path, fetched, sha, nbytes = download()

    rows, day, period, npages = parse(path, args.state)
    if not rows:
        sys.exit(f"Parsed 0 districts for {args.state}. Layout may have changed — "
                 "inspect the PDF before trusting anything downstream.")

    df = pd.DataFrame(rows)
    # The state appears once per met subdivision, so ranks restart. Deduplicate
    # on name and report, rather than assuming a single contiguous block.
    dupes = df["imd_district"].duplicated().sum()
    df = df.drop_duplicates(subset="imd_district", keep="first")

    print(f"\nPDF: {npages} pages   DAY {day}   PERIOD {period[0]} to {period[1]}")
    print(f"{args.state} districts parsed: {len(df)}   duplicate rows dropped: {dupes}")
    missing = df["cum_dep_pct"].isna().sum()
    print(f"rows without a cumulative departure: {missing}")

    cats = df["cum_cat"].value_counts().to_dict()
    print(f"cumulative categories: {cats}")
    print("\nmost deficient:")
    print(df.nsmallest(6, "cum_dep_pct")[
        ["imd_district", "cum_actual_mm", "cum_normal_mm", "cum_dep_pct", "cum_cat"]
    ].to_string(index=False))
    print("\nmost surplus:")
    print(df.nlargest(4, "cum_dep_pct")[
        ["imd_district", "cum_actual_mm", "cum_normal_mm", "cum_dep_pct", "cum_cat"]
    ].to_string(index=False))

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False)

    meta = {
        "source_name": "IMD Hydromet Division — District Rainfall Distribution",
        "source_url": URL,
        "state": args.state,
        "pdf_day": day,
        "period_from": period[0],
        "period_to": period[1],
        "fetched_utc": fetched.isoformat() if fetched else None,
        "sha256": sha,
        "bytes": nbytes,
        "local_pdf": str(path),
        "districts_parsed": int(len(df)),
        "rows_missing_departure": int(missing),
        "categories": {k: int(v) for k, v in cats.items()},
        "category_key": {
            "LE": "large excess >= +60%", "E": "excess +20 to +59%",
            "N": "normal -19 to +19%", "D": "deficient -20 to -59%",
            "LD": "large deficient -60 to -99%", "NR": "no rain -100%",
            "ND": "no data",
        },
        "note": ("Departure is against the period normal for 01-06 to the DAY "
                 "date, computed by IMD. Categories are IMD's own."),
    }
    OUT_META.write_text(json.dumps(meta, indent=2))
    print(f"\nwritten: {OUT_CSV}\n         {OUT_META}")


if __name__ == "__main__":
    main()

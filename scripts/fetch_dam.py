"""
Gujarat Reservoir Watch — daily dam storage ingest.

Source: https://wrd-dam.gujarat.gov.in/downloads/home_pdf.php?dt=<base64 YYYY-MM-DD>
Narmada, Water Resources, Water Supply & Kalpasar Dept, Government of Gujarat.
See PROJECT_BRIEF.md §2 for the URL scheme and §3 for the schema.

Parses the 24-column ruled detail table (206 schemes) plus the page-1 regional
abstract, and reconciles the two. Handles the three documented parsing hazards
(§4): repeated two-row headers, values that wrap mid-cell INCLUDING numbers,
and the abstract's duplicated MCM/MCFT rows.

Provenance — URL, date token, fetch time, byte count, SHA-256 — is recorded for
every file (§8.3).

Usage:
  python scripts/fetch_dam.py                 # today
  python scripts/fetch_dam.py --date 2026-09-10
  python scripts/fetch_dam.py --pdf path.pdf --date 2026-09-11   # parse local
"""

import argparse
import base64
import hashlib
import json
import re
import ssl
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

BASE = "https://wrd-dam.gujarat.gov.in/downloads/home_pdf.php?dt="
RAW_DIR = Path("data/raw/dam")
OUT_DIR = Path("data/processed")

EXPECTED_SCHEMES = 206
CUSEC_TO_MCM_PER_DAY = 0.00244658      # 1 cusec ≈ 2446.58 m³/day (§3)

# Detail table, 24 columns in source order (§3).
COLS = [
    "sr_no", "scheme_id", "location_id", "district", "taluka", "scheme_name",
    "region", "dam_type", "osl_m", "frl_m", "pwl_m",
    "design_gross_mcm", "design_live_mcm", "design_dead_mcm",
    "present_gross_mcm", "present_live_mcm", "present_dead_mcm",
    "pct_filling_src", "rf", "crf", "warning",
    "inflow_cusecs", "outflow_river_cusecs", "outflow_canal_cusecs",
]
TEXT_COLS = {"district", "taluka", "scheme_name", "region", "dam_type", "warning"}
INT_COLS = {"sr_no", "scheme_id", "location_id"}

# SCHEMA DRIFT (§4, hazard 4). The 2022 reports carry 23 columns, not 24: the
# `Taluka` column did not exist, and `Region` was spelled out in full rather
# than coded. Both layouts are accepted and normalised to the 24-column shape,
# with `taluka` set to None where the source never had it and `source_layout`
# recorded on every row so the difference stays visible in the data.
COLS_23 = [c for c in COLS if c != "taluka"]
LAYOUTS = {len(COLS): COLS, len(COLS_23): COLS_23}

REGION_CANON = {
    "SG": "SG", "SOUTH GUJARAT": "SG",
    "NG": "NG", "NORTH GUJARAT": "NG",
    "CG": "CG", "CENTRAL GUJARAT": "CG",
    "SAU": "Sau", "SAURASHTRA": "Sau",
    "KUTCH": "Kutch", "KACHCHH": "Kutch",
}


def canon_region(raw):
    if raw is None:
        return None, False
    key = re.sub(r"\s+", " ", str(raw)).strip().upper()
    if key in REGION_CANON:
        return REGION_CANON[key], True
    return re.sub(r"\s+", " ", str(raw)).strip(), False

DETAIL_MARKER = "details of dams"
ABSTRACT_MARKER = "Region wise storage position"


def token_for(d):
    """§2: the only parameter is base64 of the plain ISO date, padding included."""
    return base64.b64encode(str(d).encode()).decode()


def download(d):
    tok = token_for(d)
    url = BASE + tok
    # This host's TLS chain does not validate here. Verification is disabled
    # deliberately and the file hash recorded instead (§2), so the artefact is
    # auditable even though the transport is not.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    fetched = datetime.now(timezone.utc)
    with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
        blob = r.read()
    if not blob.startswith(b"%PDF-"):
        sys.exit(f"Response for {d} is not a PDF (first bytes {blob[:16]!r}). "
                 "Refusing to parse.")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"dam_{d}.pdf"
    path.write_bytes(blob)
    prov = {
        "source_name": ("Gujarat WRD Reservoir Data Management System — "
                        "daily storage report"),
        "url": url, "date_token": tok, "report_date": str(d),
        "fetched_utc": fetched.isoformat(),
        "bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest().upper(),
        "local_pdf": str(path),
        "tls_verified": False,
    }
    print(f"fetched {len(blob):,} bytes -> {path}")
    print(f"  token  {tok}")
    print(f"  sha256 {prov['sha256']}")
    return path, prov


def clean(val, numeric):
    """Hazard 2 (§4): wraps arrive as embedded newlines, in numbers too.

    Numeric fields strip ALL whitespace ('7414.2\\n9' -> 7414.29).
    Text fields collapse whitespace to one space ('HIGH\\nALERT' -> 'HIGH ALERT').
    """
    if val is None:
        return None
    s = str(val)
    if numeric:
        s = re.sub(r"\s+", "", s)
    else:
        s = re.sub(r"\s+", " ", s).strip()
    return s or None


def to_num(s):
    if s is None:
        return None
    s = s.replace("%", "").replace(",", "")
    if s in ("", "-", "NIL", "nil", "NA", "N.A."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# Hazard 2, continued (§4). Wraps inside the warning cell can fall BETWEEN
# words ("HIGH\nALERT") or MID-WORD ("WARNI\nNG"). Neither a space-join nor a
# bare concatenation is right for both, so the value is matched against the
# closed vocabulary after removing all whitespace. Anything unrecognised is
# kept verbatim and reported rather than guessed at.
WARNING_VOCAB = {
    "NIL": "NIL",
    "ALERT": "ALERT",
    "HIGHALERT": "HIGH ALERT",
    "WARNING": "WARNING",
    "DANGER": "DANGER",
}


def normalise_warning(raw):
    """Canonical warning level, plus whether it was known and whether repaired.

    TRUNCATION IN THE SOURCE, NOT IN THE EXTRACTION
    The detail table clips this cell in the PDF itself: on 2026-09-04 row 79
    (Pingli) the cell holds 'HIGH' and the second word is not present anywhere
    in the row, in extract_tables() OR in extract_text(). So it is not a cell
    bounding box losing an overflow — WRD renders a clipped string. 'HIGH' is
    not a level WRD publishes, and it is worse than merely wrong: it reads
    MILDER than the 'ALERT' level that does exist, when the value it stands for
    ('HIGH ALERT') is the more severe of the two.

    The same report proves the intended value. The 14-column major-schemes
    list, which this parser counts and discards, prints the level in full for
    the schemes it covers — on 2022-07-13 Khambhala reads 'WARNI' in the detail
    table and 'WARNING' in the major-schemes list, same scheme, same date. The
    source contradicts itself and one side is complete.

    Resolution is by UNIQUE PREFIX against the closed vocabulary, which is
    total and unambiguous here because no vocabulary key is a prefix of another
    (asserted below — 'HIGH' can only be 'HIGHALERT', 'WARNI' only 'WARNING').
    A truncation that matched two members, or matched none, is still kept
    verbatim and reported rather than guessed at.

    Returns (value, known, repaired). `repaired` is True only for a
    prefix-resolved value, so the repair is counted as a field warning rather
    than passing silently — a fix that leaves no trace is indistinguishable
    from data that was always clean.
    """
    if raw is None:
        return None, False, False
    squashed = re.sub(r"\s+", "", str(raw)).upper()
    if squashed in WARNING_VOCAB:
        return WARNING_VOCAB[squashed], True, False
    if squashed:
        hits = [k for k in WARNING_VOCAB if k.startswith(squashed)]
        if len(hits) == 1:
            return WARNING_VOCAB[hits[0]], True, True
    return re.sub(r"\s+", " ", str(raw)).strip(), False, False


# The prefix rule above is only sound while this holds. Checked at import
# rather than trusted: adding a level named 'WARN' would make 'WARNI' ambiguous
# and silently change how 53 historical rows resolve.
for _a in WARNING_VOCAB:
    for _b in WARNING_VOCAB:
        if _a != _b and _b.startswith(_a):
            raise AssertionError(
                f"WARNING_VOCAB key {_a!r} is a prefix of {_b!r}; the "
                f"unique-prefix repair in normalise_warning is no longer "
                f"unambiguous and must be reconsidered before use.")
del _a, _b


# Hazard 5 (brief §13). clean() strips ALL whitespace from numeric fields — that
# is what turns '7414.2\n9' into 7414.29 — but only collapses it in text, so a
# district name broken mid-word across two lines in the PDF cell arrives as
# 'Surendranaga r'. Stripping whitespace from text instead would give
# 'GirSomnath', so the fix is a closed vocabulary keyed on the whitespace-free,
# case-folded form — the same technique WARNING_VOCAB uses for 'WARNI NG'.
#
# Aliases cover three separate defects seen across 732 days:
#   * wrapped mid-word      Surendranaga r
#   * source abbreviation   D.B.Dwarka
#   * source truncation     bare 'Devbhumi' (10 days; it is Devbhumi Dwarka)
# plus the spellings WRD itself normalised at the 2023-06-13 refresh.
DISTRICT_CANON = {
    "surendranagar": "Surendranagar",
    "devbhumidwarka": "Devbhumi Dwarka",
    "dbdwarka": "Devbhumi Dwarka",
    "devbhumi": "Devbhumi Dwarka",
    "chhotaudepur": "Chhotaudepur",
    "aravalli": "Arvalli",
    "arvalli": "Arvalli",
    "mehsana": "Mahesana",
    "mahesana": "Mahesana",
    "girsomnath": "Gir Somnath",
    "banaskantha": "Banaskantha",
    "sabarkantha": "Sabarkantha",
    "panchmahal": "Panchmahal",
    "panchmahals": "Panchmahal",
}


def canon_district(raw):
    """Return (canonical_name, matched_vocabulary)."""
    if raw is None:
        return None, False
    key = re.sub(r"[^a-z0-9]+", "", str(raw).lower())
    if key in DISTRICT_CANON:
        return DISTRICT_CANON[key], True
    return re.sub(r"\s+", " ", str(raw)).strip(), False


N_SCHEMES_EXPECTED = 206

# WHY THIS IS AN ABSOLUTE MCM TOLERANCE AND NOT A FRACTION
# It was MATERIAL_RESIDUAL = 1e-5, a fraction of the source's own total, set
# from a single clean day whose residual was 4.85e-6. Against the full 733 days
# about 45 exceed it, and the worst — 2e-4 to 3.3e-4 — are all OFF-SEASON. That
# is not a worse parse. The residual was divided by each pair's own published
# total, and "today's gross storage" in May is a fifth of what it is in
# October, so the same absolute rounding error read five times worse. A
# threshold whose denominator moves with the seasons measures the seasons.
#
# The noise here is rounding, and rounding is absolute. The 206 per-scheme
# values are each published to two decimals, while WRD's grand total is
# computed from unrounded figures and rounded once. Our sum of rounded values
# therefore differs from their total by accumulated rounding:
#
#   worst case, every value rounding the same way   N * 0.005      = 1.03 MCM
#   typical, independent and uniform      sqrt(N) * 0.005/sqrt(3)  = 0.041 MCM
#
# The observed median residual is 0.046 MCM, which matches the typical figure
# and confirms the model — WRD does hold more precision than it publishes.
#
# The tolerance is twice the worst case. Not tuned to the observed maximum:
# derived from the rounding bound, then checked against the distribution.
# Doubling covers the case where the report's own total is itself inconsistent
# by a cent or two without leaving room for anything structural.
RESIDUAL_ROUNDING_MCM = N_SCHEMES_EXPECTED * 0.005          # 1.03
MATERIAL_RESIDUAL_MCM = 2.0 * RESIDUAL_ROUNDING_MCM         # 2.06

# WHAT THIS CHECK CANNOT SEE, stated rather than implied
# A column shift misreads all 206 values and moves the total by thousands of
# MCM, which is why reconciliation catches it. A wrapped value losing its last
# digit moves a two-decimal figure by at most 0.09 MCM — an order of magnitude
# BELOW the rounding floor, so reconciliation cannot detect it and never could.
# It is a check against structural misreads, not against fine truncation. The
# warning-cell truncation in normalise_warning is exactly the kind of defect it
# would have missed, and did.

# How many days behind today the newest data may be before it is stale. Lives
# here rather than in daily_update.py because the published page states the
# same threshold to the reader; two copies of this number would eventually
# disagree, and the page would call data fresh that the pipeline had already
# failed on. daily_update.py takes it as the --max-stale-days default and
# build_view_data.py publishes it in the view's meta.
#
# Three days is not an SLA. The source publishes daily but skips days without
# announcing it, so one missing report is normal and two is not yet alarming.
MAX_STALE_DAYS = 3

# Ledger statuses meaning "the server does not have this date", as distinct
# from "we failed to fetch it". This server answers a date it has no report for
# with HTTP 200, Content-Type application/pdf, Content-Length 0 — it does not
# use 404. Retrying never fixes one, so these are counted as absent rather than
# as failures. Canonical here because backfill.py writes them and
# build_view_data.py publishes the resulting list.
ABSENT_UPSTREAM = ("missing_upstream", "empty_upstream")


def reconcile(det, abstract):
    """Check the parsed detail rows against the report's OWN grand total.

    The strongest check available on a single day, because the source is
    checking itself: page 1's abstract is computed by WRD, not by us, so
    agreement means the 206 rows were read in the right columns with no wrapped
    number silently truncated (§4 hazard 2). A parse that shifts a column will
    almost always break the sum; one that matches to 10^-5 did not.

    Returns a dict with `ok`, both residuals and the numbers behind them.
    """
    out = {"ok": False, "reason": None, "n_parsed": int(len(det))}
    if abstract is None or getattr(abstract, "empty", True):
        out["reason"] = "abstract not parsed, so nothing to reconcile against"
        return out
    tot = abstract[abstract["region"] == "Total"]
    if tot.empty:
        out["reason"] = "no Total row found in the abstract"
        return out

    # Two rows are labelled Total: a 45-scheme sub-total of North+Central+South
    # and the state grand total. Pick by scheme count, never by position.
    grand = tot.loc[tot["n_schemes"].idxmax()]
    out["n_abstract"] = int(grand["n_schemes"])
    pairs = (("design", float(grand["design_gross_mcm"]),
              float(det["design_gross_mcm"].sum())),
             ("today", float(grand["today_gross_mcm"]),
              float(det["present_gross_mcm"].sum())))
    worst, worst_mcm = 0.0, 0.0
    for label, src, ours in pairs:
        ratio = abs(ours - src) / src if src else (0.0 if ours == 0 else 1.0)
        worst = max(worst, ratio)
        worst_mcm = max(worst_mcm, abs(ours - src))
        out[label] = {"abstract": round(src, 2), "parsed": round(ours, 2),
                      "residual_mcm": round(ours - src, 2),
                      "residual_ratio": ratio}
    # The ratio is still reported — it is the readable form, and the logs and
    # the ledger keep it — but the TEST is on absolute MCM. See the note on
    # MATERIAL_RESIDUAL_MCM for why the fraction measured the season.
    out["worst_ratio"] = worst
    out["worst_mcm"] = worst_mcm
    if out["n_abstract"] != out["n_parsed"]:
        out["reason"] = (f"scheme count disagrees: abstract says "
                         f"{out['n_abstract']}, parsed {out['n_parsed']}")
        return out
    if worst_mcm > MATERIAL_RESIDUAL_MCM:
        out["reason"] = (f"residual {worst_mcm:.2f} MCM exceeds "
                         f"{MATERIAL_RESIDUAL_MCM:.2f} MCM "
                         f"({worst:.2e} of total) — the parse does not "
                         f"reproduce the source's own total")
        return out
    out["ok"] = True
    return out


def check_vocabularies(det):
    """Unrecognised closed-vocabulary values, which must block a publish.

    Distinct from field warnings, which are normal — 190 of 732 days parse as
    `ok_with_warnings` and blocking on those would stop almost every run. What
    must block is a value that fell through a CLOSED vocabulary, because that
    means a category we thought we had enumerated has grown, and silently
    bucketing it would corrupt every count that uses it.
    """
    bad = {}
    warn_ok = set(WARNING_VOCAB.values())
    seen = set(det["warning"].dropna().unique())
    if seen - warn_ok:
        bad["warning"] = sorted(seen - warn_ok)
    region_ok = set(REGION_CANON.values())
    seen_r = set(det["region"].dropna().unique())
    if seen_r - region_ok:
        bad["region"] = sorted(seen_r - region_ok)
    return bad


def verify_day(det, abstract):
    """Every check a day's rows must pass before anything is built on them.

    THE THREE CHECKS
      row_count   exactly N_SCHEMES_EXPECTED rows
      vocabulary  no value fell through a closed vocabulary
      reconcile   the rows reproduce the report's own grand total

    WHY THIS IS ONE FUNCTION RATHER THAN THREE CALLS AT EACH SITE
    daily_update.py ran all three on the one report it fetches. parse_cached.py,
    which is what run_pipeline.py drives, ran NONE of them — it checked only the
    date line and that the detail table was non-empty. So a day that reached the
    database through the scheduled path had been reconciled against the source's
    own total, and the same day reaching it through a manual re-parse had not.
    Two paths into one DuckDB file with different standards of proof is not a
    defensible position, and which one a given row came from is not recorded.

    The guidance text lives here with the check rather than at the call site,
    because the advice does not depend on who is asking: a grown vocabulary
    needs the same fix whether it was found by the daily run or a backfill.

    Returns (problems, rec). `problems` is a list of dicts with `check`,
    `summary` and `guidance`; empty means the day is trustworthy. `rec` is the
    full reconcile result, kept even when it fails so the residuals can be
    logged and recorded.
    """
    problems = []

    if len(det) != N_SCHEMES_EXPECTED:
        problems.append({
            "check": "row_count",
            "summary": f"{len(det)} rows, expected {N_SCHEMES_EXPECTED}",
            "guidance": (
                "A dropped row and a genuinely new scheme look identical from "
                "here, so\nthis stops for a human either way. If a dam really "
                "was added, update\nN_SCHEMES_EXPECTED in fetch_dam.py and "
                "re-run."),
        })

    bad_vocab = check_vocabularies(det)
    if bad_vocab:
        problems.append({
            "check": "vocabulary",
            "summary": "; ".join(f"{k}: {v}" for k, v in bad_vocab.items()),
            "guidance": (
                "A closed vocabulary grew. Silently bucketing the new value "
                "would\ncorrupt every count that uses it, so this blocks. Add "
                "it to the\nvocabulary in fetch_dam.py once you know what it "
                "means."),
        })

    rec = reconcile(det, abstract)
    if not rec["ok"]:
        problems.append({
            "check": "reconcile",
            "summary": rec.get("reason") or "did not reconcile",
            "guidance": (
                "The parsed rows do not reproduce the total the report computes "
                "for\nitself. That is the strongest signal available that a "
                "column shifted\nor a wrapped number was truncated."),
        })

    return problems, rec


def is_header(row):
    """Hazard 1 (§4): the two-row header repeats on every detail page."""
    joined = " ".join(clean(c, False) or "" for c in row[:8]).lower()
    return ("sr" in joined and "scheme" in joined) or "gross" in joined[:40]


def parse_detail(pdf):
    """Extract the 206-scheme detail table.

    The report holds SIX different tables of differing widths (see §3): the
    19-column regional abstract, an 8-column district summary, a 14-column
    major-schemes list, this 24-column detail table, an 11-column percentage
    -storage statement and a 12-column rainfall statement. Selecting on the
    page heading was fragile because continuation pages repeat only the column
    header, so selection is on **column count == 24**. Every table not taken is
    counted and reported rather than silently ignored (§8.4).
    """
    rows, failures, pages_used = [], [], []
    other_tables = {}
    for pno, page in enumerate(pdf.pages, start=1):
        for table in page.extract_tables():
            width = len(table[0]) if table else 0
            layout = LAYOUTS.get(width)
            if layout is None:
                key = f"{width}-col"
                other_tables[key] = other_tables.get(key, 0) + 1
                continue
            for raw in table:
                if not raw or is_header(raw):
                    continue
                first = clean(raw[0], True)
                if not first or not first.isdigit():
                    continue
                if len(raw) != len(layout):
                    failures.append({"page": pno, "reason":
                                     f"row has {len(raw)} cells in a "
                                     f"{len(layout)}-col table",
                                     "raw": raw[:6]})
                    continue
                rec, bad = {}, []
                rec["taluka"] = None          # absent in the 23-column layout
                rec["source_layout"] = width
                for name, cell in zip(layout, raw):
                    numeric = name not in TEXT_COLS
                    s = clean(cell, numeric)
                    if numeric:
                        v = to_num(s)
                        if v is None and s not in (None, "", "0"):
                            bad.append(f"{name}={s!r}")
                        if name in INT_COLS:
                            v = int(v) if v is not None else None
                        rec[name] = v
                    elif name == "warning":
                        val, known, repaired = normalise_warning(cell)
                        rec[name] = val
                        if not known:
                            failures.append({
                                "page": pno, "sr_no": rec.get("sr_no"),
                                "reason": f"warning value not in vocabulary: {val!r}"})
                        elif repaired:
                            failures.append({
                                "page": pno, "sr_no": rec.get("sr_no"),
                                "reason": f"warning truncated in source: "
                                          f"{clean(cell, False)!r} -> {val!r}"})
                    elif name == "region":
                        val, known = canon_region(cell)
                        rec[name] = val
                        if not known:
                            failures.append({
                                "page": pno, "sr_no": rec.get("sr_no"),
                                "reason": f"region not recognised: {val!r}"})
                    elif name == "district":
                        # Hazard 5 (§13). Fixed HERE rather than downstream:
                        # text fields only collapse whitespace, so a district
                        # name wrapped mid-word in the PDF cell arrives as
                        # 'Surendranaga r'. Stripping whitespace from text
                        # instead would give 'GirSomnath', so it takes a closed
                        # vocabulary — the same technique WARNING_VOCAB uses for
                        # 'WARNI NG' and REGION_CANON for 'SOUTH GUJARAT'.
                        #
                        # An unrecognised district is NOT a failure. Unlike
                        # warning and region, the district list is open: Gujarat
                        # has created seven districts since 2011 and may create
                        # more, so a name not in the vocabulary is far more
                        # likely to be a legitimate district than a parse fault.
                        # It is kept verbatim and left alone.
                        val, _known = canon_district(cell)
                        rec[name] = val
                    else:
                        rec[name] = s
                if bad:
                    failures.append({"page": pno, "sr_no": rec.get("sr_no"),
                                     "scheme_name": rec.get("scheme_name"),
                                     "reason": "unparseable numeric: " + ", ".join(bad)})
                rows.append(rec)
            if pno not in pages_used:
                pages_used.append(pno)
    return pd.DataFrame(rows), failures, pages_used, other_tables


RAIN_COLS = [
    "scheme_id", "scheme_name", "district", "region_full",
    "cumm_rainfall_mm", "rain_last_24h_mm",
    "band_0_25", "band_26_50", "band_51_75", "band_76_100",
    "band_101_150", "band_gt_150",
]
RAIN_TEXT = {"scheme_name", "district", "region_full"}
# Band edges, for validating the one-hot placement the source does itself.
RAIN_BANDS = [("band_0_25", 0, 25), ("band_26_50", 25, 50), ("band_51_75", 50, 75),
              ("band_76_100", 75, 100), ("band_101_150", 100, 150),
              ("band_gt_150", 150, float("inf"))]


def parse_rainfall(pdf):
    """Per-scheme rainfall, the 12-column table on pages 18-23 (§3).

    Finer than IMD's district feed and free in the same file. The six band
    columns are a one-hot histogram of the 24-hour value, so they are kept as a
    self-check rather than as data: the value should land in exactly one band
    consistent with its own magnitude.

    Note the `Region` here is spelled out in full ("South Gujarat") whereas the
    detail table uses codes ("SG"). Do not join on it; join on scheme_id.
    """
    rows, failures = [], []
    pages_used = []
    for pno, page in enumerate(pdf.pages, start=1):
        for table in page.extract_tables():
            if not table or len(table[0]) != len(RAIN_COLS):
                continue
            hdr = " ".join(clean(c, False) or "" for c in table[0][:6]).lower()
            if "rainfall" not in hdr and "scheme" not in hdr:
                continue
            for raw in table:
                first = clean(raw[0], True)
                if not first or not first.isdigit():
                    continue
                rec = {}
                for name, cell in zip(RAIN_COLS, raw):
                    numeric = name not in RAIN_TEXT
                    s = clean(cell, numeric)
                    rec[name] = to_num(s) if numeric else s
                rec["scheme_id"] = (int(rec["scheme_id"])
                                    if rec["scheme_id"] is not None else None)
                rows.append(rec)
            if pno not in pages_used:
                pages_used.append(pno)
    df = pd.DataFrame(rows)
    if not df.empty:
        # Self-check: does the flagged band match the 24-hour value?
        def band_ok(r):
            v = r["rain_last_24h_mm"]
            if v is None or pd.isna(v):
                return None
            flagged = [n for n, lo, hi in RAIN_BANDS
                       if r.get(n) is not None and not pd.isna(r.get(n))]
            if len(flagged) != 1:
                return len(flagged) == 0 and v == 0
            n = flagged[0]
            lo, hi = next((lo, hi) for nm, lo, hi in RAIN_BANDS if nm == n)
            return (v == 0 and n == "band_0_25") or (lo < v <= hi) or (lo == 0 and v <= hi)
        df["band_consistent"] = df.apply(band_ok, axis=1)
        bad = df[df["band_consistent"] == False]          # noqa: E712
        for r in bad.itertuples():
            failures.append({"scheme_id": r.scheme_id,
                             "reason": "rainfall band inconsistent with 24h value "
                                       f"({r.rain_last_24h_mm})"})
    return df, failures, pages_used


# Fallback table strategy for the abstract page. pdfplumber's default "lines"
# strategy needs ruling lines, and on 2025-10-15..30 the abstract page carries
# the text with no detectable rules, so extract_tables() returned NOTHING and
# those 16 days were never reconciled at all — reconcile reported "abstract not
# parsed" and nothing downstream noticed, because nothing downstream was
# checking. Aligning on text positions instead recovers all seven rows.
ABSTRACT_TEXT_SETTINGS = {"vertical_strategy": "text",
                          "horizontal_strategy": "text"}

ABSTRACT_WANT = ("North Gujarat", "Central Gujarat", "South Gujarat", "Kutch",
                 "Saurashtra", "Total")


def _abstract_rows(tables):
    """Region rows out of whatever tables a strategy produced."""
    out = []
    for table in tables:
        for raw in table:
            cells = [clean(c, False) for c in raw]
            label = cells[0] if cells else None
            # The abstract carries TWO rows labelled Total: a sub-total of
            # North+Central+South (45 schemes) and the state grand total
            # (206 schemes, printed as "Total :"). Strip the colon so both
            # are captured, then pick the grand total by scheme count.
            if label:
                label = label.rstrip(" :")
            if label not in ABSTRACT_WANT:
                continue
            nums = [to_num(clean(c, True)) for c in raw[1:]]
            nums = [n for n in nums if n is not None]
            if len(nums) < 4:
                continue
            out.append({"region": label, "n_schemes": nums[0],
                        "n_filled": nums[1], "design_gross_mcm": nums[2],
                        "today_gross_mcm": nums[3]})
    return out


def parse_abstract(pdf):
    """Hazard 3 (§4): each region prints twice, MCM then MCFT. Keep the MCM row.

    Two extraction strategies, tried in order. The default is kept first
    because it is the one 717 of 733 days need and it uses the ruling lines the
    document actually has; the text-aligned fallback runs only when the default
    finds no region rows on a page that does carry the abstract marker. Both
    read the same four values in the same order — scheme count, dams filled,
    design gross, today's gross — which is why one downstream mapping serves
    both, and the recovered days reconcile against the detail rows to between
    6e-7 and 6e-6 of total.
    """
    out = []
    for page in pdf.pages:
        if ABSTRACT_MARKER not in (page.extract_text() or ""):
            continue
        out = _abstract_rows(page.extract_tables())
        if not out:
            out = _abstract_rows(page.extract_tables(ABSTRACT_TEXT_SETTINGS))
        break
    # Same label can appear twice (sub-total and grand total); keep first of each.
    seen, dedup = set(), []
    for r in out:
        key = (r["region"], r["design_gross_mcm"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(r)
    return pd.DataFrame(dedup)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=str(date.today()))
    ap.add_argument("--pdf", default=None)
    args = ap.parse_args()

    print("=" * 78)
    print(f"GUJARAT RESERVOIR WATCH — ingest for {args.date}")
    print("=" * 78)

    if args.pdf:
        path, prov = Path(args.pdf), {"report_date": args.date,
                                      "local_pdf": args.pdf, "url": None}
    else:
        path, prov = download(args.date)

    import pdfplumber
    with pdfplumber.open(str(path)) as pdf:
        prov["pdf_pages"] = len(pdf.pages)
        head = (pdf.pages[0].extract_text() or "").splitlines()[:1]
        prov["pdf_date_line"] = head[0].strip() if head else None
        abstract = parse_abstract(pdf)
        df, failures, pages, other = parse_detail(pdf)

    print(f"\nPDF pages: {prov['pdf_pages']}   header line: {prov['pdf_date_line']!r}")
    print(f"detail pages (24-col table): {pages[0]}–{pages[-1]}" if pages
          else "no detail pages found")
    print(f"other tables in the report, not parsed here: {other}")
    prov["other_tables"] = other

    # --- derived: days of water at current canal release (§3) ----------------
    rel = df["outflow_canal_cusecs"].fillna(0)
    df["days_water_at_release"] = (
        df["present_live_mcm"] / (rel * CUSEC_TO_MCM_PER_DAY)
    ).where(rel > 0)
    df["pct_filling_calc"] = (
        100 * df["present_gross_mcm"] / df["design_gross_mcm"]
    ).where(df["design_gross_mcm"] > 0)

    # --- counts ---------------------------------------------------------------
    print("\n" + "-" * 78)
    print("ROW COUNTS")
    print("-" * 78)
    print(f"  schemes parsed:            {len(df)}   (expected {EXPECTED_SCHEMES})")
    print(f"  distinct scheme_id:        {df['scheme_id'].nunique()}")
    print(f"  distinct scheme_name:      {df['scheme_name'].nunique()}")
    print(f"  districts:                 {df['district'].nunique()}")
    print(f"  talukas:                   {df['taluka'].nunique()}")
    print(f"  regions:                   {sorted(df['region'].dropna().unique())}")
    if len(df) != EXPECTED_SCHEMES:
        print(f"  *** COUNT MISMATCH: {EXPECTED_SCHEMES - len(df):+d} vs expected")

    print("\n  nulls by column (only columns with any):")
    nulls = df[COLS].isna().sum()
    for c, n in nulls[nulls > 0].items():
        print(f"    {c:<26} {n}")
    if not (nulls > 0).any():
        print("    none — every source column populated for every scheme")

    print("\n" + "-" * 78)
    print("PARSE FAILURES")
    print("-" * 78)
    if failures:
        for f in failures:
            print(f"  {f}")
    else:
        print("  none")

    # --- reconciliation against the source's own totals (§8.6) ---------------
    print("\n" + "-" * 78)
    print("RECONCILIATION vs the report's own abstract")
    print("-" * 78)
    if abstract.empty:
        print("  abstract not parsed — cannot reconcile")
    else:
        print(abstract.to_string(index=False))
        tot = abstract[abstract["region"] == "Total"]
        if not tot.empty:
            grand = tot.loc[tot["n_schemes"].idxmax()]
            src_design = float(grand["design_gross_mcm"])
            src_today = float(grand["today_gross_mcm"])
            our_design = float(df["design_gross_mcm"].sum())
            our_today = float(df["present_gross_mcm"].sum())
            print(f"\n  grand-total row used: {int(grand['n_schemes'])} schemes")
            print(f"  design gross  abstract {src_design:>12,.2f}   "
                  f"parsed {our_design:>12,.2f}   diff {our_design - src_design:+,.2f}")
            print(f"  today  gross  abstract {src_today:>12,.2f}   "
                  f"parsed {our_today:>12,.2f}   diff {our_today - src_today:+,.2f}")
            print(f"  scheme count  abstract {int(grand['n_schemes']):>12,d}   "
                  f"parsed {len(df):>12,d}")
            for lbl, dif, base in (("design", our_design - src_design, src_design),
                                   ("today", our_today - src_today, src_today)):
                ratio = abs(dif) / base if base else 0
                verdict = ("rounding-level" if ratio < 1e-5
                           else "MATERIAL — investigate before trusting this day")
                print(f"    {lbl} residual {dif:+,.2f} MCM = {ratio:.2e} of total "
                      f"-> {verdict}")

    # --- the table ------------------------------------------------------------
    print("\n" + "-" * 78)
    print(f"EXTRACTED TABLE — all {len(df)} schemes")
    print("-" * 78)
    show = df[["sr_no", "scheme_id", "district", "taluka", "scheme_name", "region",
               "design_gross_mcm", "present_gross_mcm", "present_live_mcm",
               "pct_filling_src", "pct_filling_calc", "crf", "warning",
               "inflow_cusecs", "outflow_canal_cusecs", "days_water_at_release"]]
    with pd.option_context("display.max_rows", 400, "display.width", 250,
                           "display.max_columns", 40,
                           "display.float_format", lambda v: f"{v:,.2f}"):
        print(show.to_string(index=False))

    # --- summary --------------------------------------------------------------
    print("\n" + "-" * 78)
    print("SUMMARY")
    print("-" * 78)
    print(f"  state design gross storage:  {df['design_gross_mcm'].sum():>12,.2f} MCM")
    print(f"  state present gross storage: {df['present_gross_mcm'].sum():>12,.2f} MCM")
    print(f"  state present live storage:  {df['present_live_mcm'].sum():>12,.2f} MCM")
    print(f"  state filling:               "
          f"{100 * df['present_gross_mcm'].sum() / df['design_gross_mcm'].sum():>12,.2f} %")
    print(f"  schemes with canal release:  {int((rel > 0).sum())} of {len(df)}")
    print(f"  days-of-water computable:    {int(df['days_water_at_release'].notna().sum())}"
          "   (undefined where release is zero — §8.5)")
    wc = df["warning"].value_counts(dropna=False).to_dict()
    print(f"  warning levels:              {wc}")
    d = df["days_water_at_release"].dropna()
    if len(d):
        print(f"  days-of-water: min {d.min():,.1f}  median {d.median():,.1f}  "
              f"max {d.max():,.1f}")
        print("\n  tightest 8 schemes by days-of-water at current release:")
        t = df.nsmallest(8, "days_water_at_release")[
            ["scheme_name", "district", "present_live_mcm",
             "outflow_canal_cusecs", "days_water_at_release"]]
        print(t.to_string(index=False))

    # --- write ----------------------------------------------------------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv = OUT_DIR / f"dam_storage_{args.date}.csv"
    df.to_csv(csv, index=False)
    prov.update({"schemes_parsed": int(len(df)),
                 "schemes_expected": EXPECTED_SCHEMES,
                 "parse_failures": failures,
                 "detail_pages": pages})
    pj = OUT_DIR / f"dam_provenance_{args.date}.json"
    pj.write_text(json.dumps(prov, indent=2))
    print(f"\nwritten: {csv}\n         {pj}")


if __name__ == "__main__":
    main()

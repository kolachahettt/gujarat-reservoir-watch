"""
Parse every cached dam PDF into per-day parquet, in parallel.

Split out from backfill.py because the two phases have opposite constraints:
downloading must be polite and therefore serial, while parsing is local,
CPU-bound and embarrassingly parallel. Measured cost is ~13-17 s of table
extraction per 23-page PDF, so single-threaded parsing of the full 2022-2026
range would take about six hours; across workers it is closer to one.

Touches the network never. Writes the ledger that build_db.py and
report_deviation.py read, merging in any download failures recorded by
backfill.py --download-only.

Usage:
  python scripts/parse_cached.py                 # all cached, skip done
  python scripts/parse_cached.py --workers 6
  python scripts/parse_cached.py --force
"""

import argparse
import hashlib
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PDF_DIR = Path("data/raw/dam")
DAY_DIR = Path("data/interim/day")
RAIN_DIR = Path("data/interim/rain")
LEDGER = Path("data/interim/backfill_ledger.csv")
DL_LOG = Path("data/interim/download_log.csv")

LEDGER_COLS = ["report_date", "status", "http_status", "bytes", "sha256",
               "pdf_date_line", "n_schemes", "n_rainfall", "note", "attempted_utc"]


def parse_one(pdf_path):
    """Runs in a worker process. Returns a ledger record."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import pdfplumber
    import fetch_dam as fd

    d = Path(pdf_path).stem.replace("dam_", "")
    blob_len = Path(pdf_path).stat().st_size
    sha = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest().upper()
    base = {"report_date": d, "http_status": None, "bytes": blob_len,
            "sha256": sha, "n_schemes": 0, "n_rainfall": 0,
            "attempted_utc": datetime.now(timezone.utc).isoformat()}
    try:
        with pdfplumber.open(pdf_path) as pdf:
            head = (pdf.pages[0].extract_text() or "").splitlines()[:1]
            date_line = head[0].strip() if head else None
            det, det_fail, _, _ = fd.parse_detail(pdf)
            rain, _, _ = fd.parse_rainfall(pdf)
    except Exception as e:                           # noqa: BLE001
        base.update({"status": "malformed", "pdf_date_line": None,
                     "note": f"parse_error:{type(e).__name__}"})
        return base

    base["pdf_date_line"] = date_line
    # The PDF states its own date; if it disagrees with the requested date the
    # server served the wrong day and the row must not be trusted (§8.2).
    stated = None
    if date_line and "-" in date_line:
        try:
            parts = date_line.split(":-")[-1].strip()
            dd, mm, yy = parts.split("-")
            stated = f"{yy}-{mm}-{dd}"
        except Exception:                            # noqa: BLE001
            stated = None
    if stated and stated != d:
        base.update({"status": "date_mismatch", "n_schemes": len(det),
                     "n_rainfall": len(rain),
                     "note": f"requested {d}, pdf says {stated}"})
        return base

    if det.empty:
        base.update({"status": "malformed", "note": "no_detail_rows",
                     "n_rainfall": len(rain)})
        return base

    DAY_DIR.mkdir(parents=True, exist_ok=True)
    RAIN_DIR.mkdir(parents=True, exist_ok=True)
    det.assign(report_date=d).to_parquet(DAY_DIR / f"{d}.parquet", index=False)
    if not rain.empty:
        rain.assign(report_date=d).to_parquet(RAIN_DIR / f"{d}.parquet", index=False)

    base.update({"status": "ok" if not det_fail else "ok_with_warnings",
                 "n_schemes": len(det), "n_rainfall": len(rain),
                 "note": "" if not det_fail else f"{len(det_fail)} field warnings"})
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max-seconds", type=float, default=0,
                    help="stop cleanly after this long and write the ledger. "
                         "Parsing is resumable — already-parsed days are "
                         "skipped — so a budgeted run can be repeated instead "
                         "of risking a hard kill part-way through a write.")
    args = ap.parse_args()

    existing = {}
    if LEDGER.exists():
        old = pd.read_csv(LEDGER, dtype={"report_date": str})
        # .to_dict() matters: keeping Series here and mixing them with the dicts
        # the workers return makes DataFrame() fail on construction.
        existing = {r["report_date"]: r.to_dict() for _, r in old.iterrows()}

    OK = ("ok", "ok_with_warnings")
    pdfs = sorted(PDF_DIR.glob("dam_*.pdf"))

    def needs_parse(p):
        d = p.stem.replace("dam_", "")
        if args.force or not (DAY_DIR / f"{d}.parquet").exists():
            return True
        # Self-heal: the parquet is here but the ledger still records a failure
        # from an earlier pass, because the download that fixed it found the
        # PDF cached and skipped the row. Re-parsing is the only way to know
        # whether the day is ok or ok_with_warnings, and it is cheap.
        return existing.get(d, {}).get("status") not in OK

    todo = [p for p in pdfs if needs_parse(p)]
    stale = [p for p in todo
             if (DAY_DIR / f"{p.stem.replace('dam_', '')}.parquet").exists()]
    print(f"cached PDFs: {len(pdfs)}   to parse: {len(todo)}   "
          f"workers: {args.workers}")
    if stale and not args.force:
        print(f"  of those, {len(stale)} already have parquet but a stale "
              f"failure status in the ledger — re-parsed so the ledger stops "
              f"under-reporting coverage")

    done = 0
    stopped_early = False
    t0 = time.time()
    if todo:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(parse_one, str(p)): p for p in todo}
            for f in as_completed(futs):
                rec = f.result()
                existing[rec["report_date"]] = rec
                done += 1
                if done % 20 == 0 or done == len(todo):
                    print(f"  parsed {done}/{len(todo)}  "
                          f"({time.time() - t0:,.0f}s)", flush=True)
                if done % 100 == 0:
                    pd.DataFrame(list(existing.values())).to_csv(LEDGER, index=False)
                if args.max_seconds and time.time() - t0 > args.max_seconds:
                    # Cancel what has not started; in-flight workers finish
                    # their file so no parquet is left half-written.
                    for g in futs:
                        g.cancel()
                    stopped_early = True
                    print(f"  time budget of {args.max_seconds:,.0f}s reached "
                          f"after {done}/{len(todo)} — stopping cleanly",
                          flush=True)
                    break

    # Merge in dates that never produced a PDF, so absence is recorded.
    if DL_LOG.exists():
        dl = pd.read_csv(DL_LOG, dtype={"report_date": str})
        for _, r in dl.iterrows():
            if r["report_date"] in existing:
                continue
            existing[r["report_date"]] = {
                "report_date": r["report_date"], "status": r.get("status", "fetch_failed"),
                "http_status": r.get("http_status"), "bytes": r.get("bytes"),
                "sha256": None, "pdf_date_line": None, "n_schemes": 0,
                "n_rainfall": 0, "note": r.get("note"),
                "attempted_utc": r.get("attempted_utc")}

    df = pd.DataFrame(list(existing.values()))
    for c in LEDGER_COLS:
        if c not in df.columns:
            df[c] = None
    df[LEDGER_COLS].sort_values("report_date").to_csv(LEDGER, index=False)

    print("\nstatus counts:")
    print(df["status"].value_counts().to_string())
    print(f"ledger: {LEDGER}")
    # A PDF with no parquet after a COMPLETE pass is malformed, not unparsed —
    # that is a finding, not a reason to stop. Only an early stop means there
    # is work left, and then the pipeline must not proceed on a partial parse.
    no_parquet = [p.stem.replace("dam_", "") for p in pdfs
                  if not (DAY_DIR / f"{p.stem.replace('dam_', '')}.parquet").exists()]
    if no_parquet:
        print(f"\nPDFs that produced no parquet: {len(no_parquet)}")
        if len(no_parquet) <= 20:
            print(f"  {no_parquet}")
    if stopped_early:
        print(f"\nSTOPPED EARLY — {len(no_parquet)} PDF(s) still unparsed. "
              "Re-run to continue.")
        sys.exit(3)


if __name__ == "__main__":
    main()

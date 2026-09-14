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

# The workers re-import this themselves — a ProcessPoolExecutor child does not
# inherit the parent's modules on Windows (spawn, not fork). This one is for
# main(), which reports the tolerance the verdicts were judged against.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_dam as fd  # noqa: E402

PDF_DIR = Path("data/raw/dam")
DAY_DIR = Path("data/interim/day")
RAIN_DIR = Path("data/interim/rain")
LEDGER = Path("data/interim/backfill_ledger.csv")
DL_LOG = Path("data/interim/download_log.csv")

LEDGER_COLS = ["report_date", "status", "http_status", "bytes", "sha256",
               "pdf_date_line", "n_schemes", "n_rainfall", "rain_col0",
               "verify", "worst_residual", "residual_mcm", "note",
               "attempted_utc"]


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
            # `det` is required, not optional: the rainfall statement's first
            # column was a plain row counter from 18 Jun to 14 Oct 2025, so the
            # scheme is identified by name against page 3 of this same PDF.
            rain, rain_fail, _ = fd.parse_rainfall(pdf, detail=det)
            # Page 1's grand total, read while the PDF is still open. This is
            # the only reason the abstract is parsed here at all, and it is
            # cheap — parse_abstract stops at the page carrying the marker.
            abstract = fd.parse_abstract(pdf)
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

    # The row count, the closed vocabularies and the reconciliation against the
    # report's own grand total — the same three checks fd.verify_day runs for
    # the daily fetch, which until now ran ONLY there. A day that entered the
    # database through a manual re-parse had never been reconciled against the
    # source's own total.
    #
    # ENFORCED. The first pass at this recorded the verdict without acting on
    # it, because 246 of 733 days then failed and halting would have dropped a
    # third of the archive. All three causes were defects, not over-strict
    # checks, and all three are fixed: warning values truncated in the source
    # are resolved against the closed vocabulary, parse_abstract falls back to
    # a text strategy on the 16 days whose abstract page has no ruling lines,
    # and the residual tolerance is now derived from the rounding bound instead
    # of from one clean day.
    #
    # No parquet is written on failure, which is the convention the
    # date_mismatch and malformed branches above already follow. build_db.py
    # globs the parquet directory, so a day that cannot prove itself is simply
    # not there to be ingested and build_db needs to know nothing about it. The
    # day is re-parsed next run, since `unverified` is not in the OK set.
    problems, rec = fd.verify_day(det, abstract)
    if problems:
        base.update({
            "status": "unverified", "n_schemes": len(det),
            "n_rainfall": len(rain),
            "verify": "; ".join(f"{p['check']}: {p['summary']}"
                                for p in problems),
            "worst_residual": rec.get("worst_ratio"),
            "residual_mcm": rec.get("worst_mcm"),
            "note": "verification failed; no parquet written",
        })
        return base
    verify = "pass" if not problems else "; ".join(
        f"{p['check']}: {p['summary']}" for p in problems)
    # Both forms recorded: MCM is what the threshold tests (the noise is
    # rounding, which is absolute), the ratio is the readable form. Keeping
    # both means the threshold can be re-calibrated from the ledger without
    # re-parsing 733 PDFs to recover the other one.
    residual_mcm = rec.get("worst_mcm")

    DAY_DIR.mkdir(parents=True, exist_ok=True)
    RAIN_DIR.mkdir(parents=True, exist_ok=True)
    det.assign(report_date=d).to_parquet(DAY_DIR / f"{d}.parquet", index=False)
    if not rain.empty:
        rain.assign(report_date=d).to_parquet(RAIN_DIR / f"{d}.parquet", index=False)

    warn = list(det_fail) + list(rain_fail)
    note = ""
    if det_fail:
        note = f"{len(det_fail)} field warnings"
    if rain_fail:
        # Named separately from the detail warnings. A rainfall identification
        # failure is not a field warning — it means a row's scheme could not be
        # established — and it must be visible in the ledger rather than
        # folded into a count.
        note = (note + "; " if note else "") + \
            f"{len(rain_fail)} rainfall warnings: " + \
            "; ".join(str(f.get("reason"))[:90] for f in rain_fail[:3])
    base.update({"status": "ok" if not warn else "ok_with_warnings",
                 "n_schemes": len(det), "n_rainfall": len(rain),
                 # How column 0 of the rainfall statement was interpreted, per
                 # day, so the 'Sr No' window is a recorded fact rather than
                 # something to be rediscovered.
                 "rain_col0": (rain["rain_col0_meaning"].iloc[0]
                               if not rain.empty
                               and "rain_col0_meaning" in rain.columns else None),
                 # Both retained even when the day passes: the residual is the
                 # evidence that it reconciled, and a ledger that records only
                 # failures cannot show that a check actually ran.
                 "verify": verify,
                 "worst_residual": rec.get("worst_ratio"),
                 "residual_mcm": residual_mcm,
                 "note": note})
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
    ap.add_argument("--only-existing", action="store_true",
                    help="with --force, re-parse ONLY days that already have "
                         "a parquet, ignoring newly cached PDFs. For fixing a "
                         "parser bug in the current dataset without also "
                         "ingesting a part-finished backfill of new dates.")
    ap.add_argument("--from", dest="date_from", default=None,
                    help="earliest report date to parse, ISO. With --force, "
                         "re-parses a bounded window — a parser fix that "
                         "affects a known date range does not need the other "
                         "four years re-derived, and build_db unions parquet "
                         "by name so a mixed schema is safe.")
    ap.add_argument("--to", dest="date_to", default=None,
                    help="latest report date to parse, ISO")
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

    if args.only_existing:
        pdfs = [p for p in pdfs
                if (DAY_DIR / f"{p.stem.replace('dam_', '')}.parquet").exists()]
        print(f"--only-existing: restricted to the {len(pdfs)} day(s) already "
              f"derived")
    if args.date_from or args.date_to:
        lo = args.date_from or "0000-00-00"
        hi = args.date_to or "9999-99-99"
        pdfs = [p for p in pdfs
                if lo <= p.stem.replace("dam_", "") <= hi]
        print(f"--from/--to: restricted to {len(pdfs)} day(s) in "
              f"{lo} .. {hi}")
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

    # Verification summary, grouped by which check failed so the shape of the
    # problem is visible rather than a single pass/fail count.
    ver = df["verify"].dropna() if "verify" in df.columns else pd.Series(
        [], dtype=str)
    if len(ver):
        n_pass = int((ver == "pass").sum())
        print(f"\nverification (fd.verify_day, same checks as the daily "
              f"fetch): {n_pass}/{len(ver)} pass")
        res = pd.to_numeric(df.get("residual_mcm"), errors="coerce").dropna()
        if len(res):
            print(f"  reconcile residual, MCM: median {res.median():.3f}   "
                  f"p99 {res.quantile(.99):.3f}   max {res.max():.3f}   "
                  f"(tolerance {fd.MATERIAL_RESIDUAL_MCM:.2f}, rounding bound "
                  f"{fd.RESIDUAL_ROUNDING_MCM:.2f})")
        fails = df[df["verify"].notna() & (df["verify"] != "pass")]
        if len(fails):
            kinds = {}
            for _, r in fails.iterrows():
                for part in str(r["verify"]).split("; "):
                    kinds.setdefault(part.split(":")[0], []).append(
                        r["report_date"])
            for k in sorted(kinds):
                v = sorted(kinds[k])
                print(f"  {k:<12} {len(v):>4} day(s)   {v[0]} .. {v[-1]}")

    # A day that failed verification is not a day to build on. Reported after
    # the ledger is written so the evidence is on disk either way, and exits
    # non-zero so run_pipeline.py halts at step 1 rather than rebuilding a
    # database and three analyses on rows that do not add up.
    unver = df[df["status"] == "unverified"]
    if len(unver):
        print("\n" + "!" * 88)
        print(f"VERIFICATION FAILED ON {len(unver)} DAY(S) — nothing "
              f"downstream should be built")
        for _, r in unver.sort_values("report_date").iterrows():
            print(f"  {r['report_date']}  {r['verify']}")
        print("\nThese are the same three checks the daily fetch applies: 206 "
              "rows, closed\nvocabularies recognised, and reconciliation "
              "against the report's own grand\ntotal. No parquet was written "
              "for the days above, so the database cannot pick\nthem up; they "
              "are re-parsed on the next run. See fd.verify_day.")
        print("!" * 88)
        sys.exit(4)


if __name__ == "__main__":
    main()

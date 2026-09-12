"""
Wait for the polite serial download to finish, retry what failed, and only then
run the pipeline.

History, because it cost a wasted pipeline run on 2026-09-11: the first version
watched the count of cached PDFs and treated 12 minutes of no growth as "the
download has finished". A failed fetch writes no PDF, so a run of consecutive
transport failures freezes that count while the downloader is working normally.
The 2026-09-11 pass lost 133 dates in runs of 24 to 50 consecutive days; the
watcher read one of those runs as completion and ran the analysis on 352 of 715
dates, with the 2024 season only 36/153 complete.

Three changes follow from that:

  * Progress is the freshest of (download log mtime, newest PDF mtime). The log
    is flushed on every failure, so it moves even when nothing downloads.
  * The stall threshold is generous, because it is only a crash fallback. The
    definitive signal is the downloader printing "done."
  * COMPLETENESS IS A PRECONDITION. The pipeline does not run while any
    requested date is still missing. Incomplete coverage does not announce
    itself in the output — a deviation table built on a quarter of a season
    looks exactly like one built on all of it — so it has to be refused here.

On a stall the watcher reports and exits without touching the network: it cannot
tell a crashed downloader from a slow one, and starting a second downloader
against a server that may already be refusing us would be both impolite and a
plausible cause of the refusals.

Exit codes:  0 pipeline ran and succeeded   2 gate tripped (see run_pipeline)
             3 needs attention (stall, or dates still missing after retries)

Usage:
  python scripts/wait_then_run.py --date 2026-09-11 --skip-pin
"""

import argparse
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DL_LOG = Path("data/interim/download_run.log")
DL_CSV = Path("data/interim/download_log.csv")
DL_DONE = Path("data/interim/download_DONE.json")
PDF_DIR = Path("data/raw/dam")
POLL_S = 60


def newest_mtime():
    """Freshest sign of life: either log, or the most recent PDF."""
    ts = [0.0]
    for p in (DL_LOG, DL_CSV):
        if p.exists():
            ts.append(p.stat().st_mtime)
    pdfs = list(PDF_DIR.glob("dam_*.pdf"))
    if pdfs:
        ts.append(max(p.stat().st_mtime for p in pdfs))
    return max(ts)


def n_pdfs():
    return len(list(PDF_DIR.glob("dam_*.pdf")))


def finished():
    """The downloader writes DL_DONE only on a clean end of pass.

    Deliberately not a tail of captured stdout: whatever captures that stdout
    may hold the file open, and on Windows that makes it unreadable here.
    """
    return DL_DONE.exists()


def requested_days(start, end, months):
    lo, hi = (int(x) for x in months.split("-")) if months else (1, 12)
    n = (end - start).days + 1
    return [start + timedelta(days=i) for i in range(n)
            if lo <= (start + timedelta(days=i)).month <= hi]


def absent_upstream():
    """Dates the server does not have, as opposed to ones we failed to get.

    `empty_upstream` is this server's way of saying "no report for that date":
    HTTP 200, Content-Type application/pdf, Content-Length 0. Retrying never
    fixes it, so it must not sit in the completeness denominator forever — but
    it is reported every time, never quietly dropped.
    """
    if not DL_CSV.exists():
        return set()
    import pandas as pd
    df = pd.read_csv(DL_CSV, dtype={"report_date": str})
    return set(df.loc[df["status"].isin(("missing_upstream", "empty_upstream")),
                      "report_date"])


def missing_dates(days):
    """Requested dates with no cached PDF, excluding those absent upstream."""
    gone = absent_upstream()
    return [d for d in days
            if not (PDF_DIR / f"dam_{d}.pdf").exists()
            and str(d) not in gone]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=str(date.today()))
    ap.add_argument("--start", default="2022-06-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--months", default="6-10")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--stall-min", type=int, default=45,
                    help="crash fallback only; 'done.' is the real signal")
    ap.add_argument("--max-hours", type=float, default=9.0)
    ap.add_argument("--retry-passes", type=int, default=3,
                    help="re-runs of the downloader to pick up failed dates")
    ap.add_argument("--retry-cooldown-min", type=int, default=10)
    ap.add_argument("--skip-pin", action="store_true")
    ap.add_argument("--no-pipeline", action="store_true",
                    help="wait, retry and verify coverage, then stop. Use when "
                         "a gate finding is under review and nothing should be "
                         "rebuilt yet.")
    args = ap.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.fromisoformat(args.date)
    days = requested_days(date.fromisoformat(args.start), end, args.months)

    t0 = time.time()
    print(f"{n_pdfs()} PDFs cached at start, {len(days)} dates requested, "
          f"{len(missing_dates(days))} missing", flush=True)

    # If nothing is missing the download is already complete, whatever the
    # sentinel says. Waiting on it would only burn the stall timeout.
    while missing_dates(days):
        if finished():
            print("downloader reported done.", flush=True)
            break
        quiet_min = (time.time() - newest_mtime()) / 60
        if quiet_min >= args.stall_min:
            print(f"\nNO SIGN OF LIFE for {quiet_min:.0f} min "
                  f"({n_pdfs()} PDFs cached).", flush=True)
            print("Not starting a retry pass: a second downloader against a "
                  "server that may already be refusing us is the wrong move, "
                  "and a stalled process cannot be told from a slow one here.")
            print("Check the downloader, then re-run this watcher.")
            sys.exit(3)
        if (time.time() - t0) / 3600 >= args.max_hours:
            print(f"\nmax wait of {args.max_hours} h reached "
                  f"({n_pdfs()} PDFs cached); stopping without running "
                  "the pipeline.", flush=True)
            sys.exit(3)
        if int((time.time() - t0) / POLL_S) % 15 == 0:
            miss = len(missing_dates(days))
            print(f"  [{datetime.now(timezone.utc):%H:%M}Z] {n_pdfs()} PDFs, "
                  f"{miss} dates still missing, quiet {quiet_min:.0f} min",
                  flush=True)
        time.sleep(POLL_S)

    # ---- retry passes -------------------------------------------------------
    dl_cmd = [sys.executable, "-u", "scripts/backfill.py", "--download-only",
              "--start", args.start, "--end", str(end),
              "--months", args.months, "--delay", str(args.delay)]
    for p in range(1, args.retry_passes + 1):
        miss = missing_dates(days)
        if not miss:
            break
        print(f"\n{'=' * 80}\nRETRY PASS {p}/{args.retry_passes}: "
              f"{len(miss)} dates missing "
              f"({miss[0]} .. {miss[-1]})\n{'=' * 80}", flush=True)
        subprocess.run(dl_cmd)
        if missing_dates(days) and p < args.retry_passes:
            print(f"\ncooling down {args.retry_cooldown_min} min before the "
                  "next pass", flush=True)
            time.sleep(args.retry_cooldown_min * 60)

    # ---- completeness gate --------------------------------------------------
    miss = missing_dates(days)
    if miss:
        print(f"\n{'!' * 80}")
        print(f"COVERAGE INCOMPLETE — {len(miss)} of {len(days)} requested "
              f"dates have no PDF after {args.retry_passes} retry passes.")
        print("The pipeline has NOT been run. A deviation table built on "
              "partial coverage is\nindistinguishable from one built on full "
              "coverage, so this is refused rather\nthan reported as a "
              "footnote.")
        by_season = {}
        for d in miss:
            by_season.setdefault(d.year, []).append(d)
        for y in sorted(by_season):
            v = by_season[y]
            print(f"  {y}: {len(v):>3} missing  ({v[0]} .. {v[-1]})")
        print("!" * 80)
        sys.exit(3)

    gone = sorted(absent_upstream() & {str(d) for d in days})
    print(f"\ncoverage complete: {len(days) - len(gone)} of {len(days)} "
          f"requested dates cached.", flush=True)
    if gone:
        print(f"{len(gone)} date(s) absent upstream, not retrievable: "
              f"{', '.join(gone)}")
        print("These are counted as absent, not as failures. Any figure that "
              "depends on them\nmust say so — see the date-count rule in brief "
              "§13.")
    if args.no_pipeline:
        print("--no-pipeline: stopping here. Nothing has been parsed or "
              "rebuilt.", flush=True)
        sys.exit(0)
    print("starting pipeline.", flush=True)
    cmd = [sys.executable, "-u", "scripts/run_pipeline.py", "--date", args.date]
    if args.skip_pin:
        cmd.append("--skip-pin")
    r = subprocess.run(cmd)
    print(f"pipeline exit {r.returncode}")
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()

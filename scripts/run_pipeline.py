"""
Gujarat Reservoir Watch — run the post-download pipeline in the agreed order.

  1. parse every cached PDF (parallel, no network), and VERIFY each day with
     fd.verify_day — the same three checks the daily fetch applies: exactly 206
     rows, closed vocabularies recognised, and the rows reconciling against the
     report's own grand total
  2. rebuild DuckDB
  3. THE CAPACITY GATE — establishes one capacity per scheme-season, and halts
     everything on an UNACKNOWLEDGED within-season capacity change
  4. both deviation tables (ranking on option 0 + C; volume list unchanged)
  5. scheme drift re-check
  6. pin the changeover dates (2023->2024 shared boundary, Dantiwada separately)

On the ordering: the gate has to run *after* parse and DB rebuild, because it
can only inspect data that has been loaded. What it gates is the ANALYSIS —
nothing in steps 4-6 runs if the within-season stability assumption that option
C depends on turns out to be false. Parquet and the DuckDB file are derived,
idempotent and cheap to rebuild; a deviation ranking published on an invalid
baseline is the thing that does damage.

If the gate trips, this script stops with exit code 2 and writes
data/processed/GATE_TRIPPED.txt. Nothing downstream is produced.

ON THE STEP-1 VERIFICATION
--------------------------
Those three checks used to run only in daily_update.py, so a day that reached
the database through this path had never been reconciled against the source's
own total while the same day arriving through the scheduled fetch had. Two
paths into one DuckDB file with different standards of proof, and no record of
which path a given row came from. One implementation (fd.verify_day) now
serves both, the per-day verdict goes in the ledger's `verify` column, and a
failure halts this script with exit code 4 — no parquet is written for a day
that cannot prove itself, so build_db.py cannot ingest it.

Turning that on required fixing what it found. The first pass recorded verdicts
without acting on them because 246 of 733 days failed, and all three causes
were defects rather than over-strict checks:

  * parse_detail emitted warning values truncated IN THE SOURCE — 'HIGH' where
    the report means 'HIGH ALERT', on 190 rows. Not a bounding-box artefact:
    the second word is absent from the page text too. The same report prints
    the level in full in its 14-column major-schemes list, which is how the
    intended value is known rather than guessed.
  * parse_abstract returned nothing for 2025-10-15..30 because those abstract
    pages carry no ruling lines, so extract_tables() found no table at all.
    Sixteen days were never reconciled and nothing noticed, because nothing
    was checking.
  * the residual tolerance was a fraction of each pair's own total, so the
    same rounding error read five times worse in May than in October. It is
    now an absolute MCM bound derived from the rounding arithmetic.

Usage:
  python scripts/run_pipeline.py
  python scripts/run_pipeline.py --skip-pin
"""

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

HALT = Path("data/processed/GATE_TRIPPED.txt")


def run(cmd, label, allow_fail=False):
    print("\n" + "#" * 88)
    print(f"# {label}")
    print(f"# $ {' '.join(cmd)}")
    print("#" * 88, flush=True)
    r = subprocess.run([sys.executable, "-u"] + cmd)
    if r.returncode != 0 and not allow_fail:
        print(f"\n{label} exited {r.returncode}; stopping.")
        sys.exit(r.returncode)
    return r.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=str(date.today()))
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--skip-pin", action="store_true")
    args = ap.parse_args()

    HALT.unlink(missing_ok=True)

    run(["scripts/parse_cached.py", "--workers", str(args.workers)],
        "1/6  PARSE cached PDFs (parallel, no network)")
    run(["scripts/build_db.py"], "2/6  REBUILD DuckDB")

    code = run(["scripts/check_capacity.py"],
               "3/6  CAPACITY GATE (exit 2 = halt)", allow_fail=True)
    if code == 2:
        msg = (
            "CAPACITY GATE TRIPPED\n\n"
            "A scheme changes design capacity WITHIN a single Jun-Oct season and "
            "is NOT in\ncheck_capacity.ACKNOWLEDGED_MID_SEASON — no disposition "
            "has been agreed for it.\n\n"
            "Excluding a scheme-season from the like-for-like baseline is not a "
            "decision the\npipeline should take on its own, so no deviation "
            "ranking has been produced.\n\n"
            "See the gate output above, data/processed/"
            "capacity_midseason_exceptions.csv,\nand brief §14 for how the three "
            "known cases were dispositioned.\n"
            "Review before re-running. Nothing downstream was built.\n")
        HALT.parent.mkdir(parents=True, exist_ok=True)
        HALT.write_text(msg)
        print("\n" + "!" * 88)
        print(msg)
        print("!" * 88)
        sys.exit(2)
    if code != 0:
        sys.exit(code)

    # The rainfall gate. Separate from the capacity gate and with its own exit
    # code, because it protects a different thing: the capacity gate stops a
    # restated denominator breaking a percentage, this stops a misassigned
    # rainfall row being published as a scheme's own. It exists because that
    # failure reached the live page and nothing was checking (§28).
    code = run(["scripts/check_rainfall.py"],
               "3b/6  RAINFALL GATE (exit 3 = halt)", allow_fail=True)
    if code == 3:
        msg = (
            "RAINFALL INTEGRITY GATE TRIPPED\n\n"
            "One of three tests failed: the cumulative series falls somewhere, "
            "or page 3\ndisagrees with the rainfall statement, or the "
            "cumulative figure does not advance\nby the daily one.\n\n"
            "The most likely cause is the report changing the first column of "
            "the rainfall\nstatement again. It was 'Scheme Id' until 17 June "
            "2025, 'Sr No' (a row counter,\nsorted by rainfall) until 14 "
            "October 2025, and 'Scheme Id' since. parse_rainfall\nkeys on that "
            "header word; a third spelling would need handling there.\n\n"
            "See the gate output above, data/processed/rainfall_integrity.csv, "
            "and brief §28.\n")
        HALT.parent.mkdir(parents=True, exist_ok=True)
        HALT.write_text(msg)
        print("\n" + "!" * 88)
        print(msg)
        print("!" * 88)
        sys.exit(3)
    if code != 0:
        sys.exit(code)

    run(["scripts/report_deviation.py", "--date", args.date,
         "--top", str(args.top)],
        "4/6 + 5/6  DEVIATION TABLES (both lists) AND DRIFT RE-CHECK")

    if args.skip_pin:
        print("\n6/6  skipped (--skip-pin)")
    else:
        run(["scripts/pin_changeover.py", "--boundary", "2023-2024",
             "--scheme", "4"],
            "6/6  PIN CHANGEOVER (2023->2024 shared boundary + Dantiwada)",
            allow_fail=True)

    print("\n" + "=" * 88)
    print("pipeline complete")
    print("=" * 88)


if __name__ == "__main__":
    main()

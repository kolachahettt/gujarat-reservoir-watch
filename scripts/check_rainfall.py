"""
Gujarat Reservoir Watch — rainfall integrity gate.

THIS RUNS BEFORE THE ANALYSIS AND IT CAN HALT THE PIPELINE.

It exists because a silent failure got all the way to the published page. The
rainfall statement's first column was headed 'Scheme Id' until 17 June 2025,
'Sr No' from 18 June to 14 October 2025, and 'Scheme Id' again afterwards.
Under 'Sr No' it is a row counter and the rows are sorted by 24-hour rainfall
descending, so reading it as an identifier hands every scheme a different
scheme's rainfall. That corrupted 24,004 of 24,308 scheme-days inside the
window (98.7%) and 0 outside it, and nothing noticed for a year: the page's
regional rain strip showed Central Gujarat at 11.6 mm/day on a date the sound
column says 0.4.

Nothing was checking, so this checks. Three tests, each one of which would
have caught it on its own:

1. MONOTONICITY. A cumulative seasonal total cannot fall. The broken series
   had 10,908 backward steps out of 147,290 (7.41%), worst fall 2,611 mm; the
   sound one has 18 (0.01%).

2. THE PAGE-3 CROSS-CHECK. The same figures appear twice in every report — on
   page 3 beside the storage (RF, CRF) and in the statement on pages 17-23.
   They agreed on 100% of rows in 2022, 2023, 2024 and 2026 and on 1.3% inside
   the window. This is the test that found it, so it is the test that stays.

3. THE STEP IDENTITY. Same gauge, so the cumulative figure must rise by
   exactly the 24-hour figure from one day to the next. Holds on 99.7% of
   consecutive-day steps in the sound data.

Exit codes: 0 = clear, 3 = an integrity test failed, do not publish.

Usage:
  python scripts/check_rainfall.py
  python scripts/check_rainfall.py --max-backward-pct 0.5 --max-disagree-pct 0.5
"""

import argparse
import sys
from pathlib import Path

import duckdb

DB = Path("data/processed/reservoir.duckdb")
OUT = Path("data/processed/rainfall_integrity.csv")

# Tolerances, stated rather than hidden. The sound years sit at 0.01% backward
# and 0.00% disagreement, so anything approaching a tenth of a percent is a
# new fault and not drift. The step identity is looser because the source
# genuinely restates the odd figure: it holds on 99.7%, not 100%.
MAX_BACKWARD_PCT = 0.10
MAX_DISAGREE_PCT = 0.10
MAX_STEP_BREAK_PCT = 1.00


def rule(t):
    print(f"\n{'=' * 88}\n{t}\n{'=' * 88}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-backward-pct", type=float, default=MAX_BACKWARD_PCT)
    ap.add_argument("--max-disagree-pct", type=float, default=MAX_DISAGREE_PCT)
    ap.add_argument("--max-step-break-pct", type=float,
                    default=MAX_STEP_BREAK_PCT)
    args = ap.parse_args()

    if not DB.exists():
        sys.exit("No database. Run build_db.py first.")
    con = duckdb.connect(str(DB), read_only=True)
    tables = {t[0] for t in con.execute("SHOW TABLES").fetchall()}
    if "fact_rainfall" not in tables:
        sys.exit("fact_rainfall missing from the database.")

    failed = []

    # ---------------- 1. MONOTONICITY -----------------------------------
    rule("1. MONOTONICITY — a cumulative total cannot fall")
    q = """
      WITH s AS (
        SELECT scheme_id, scheme_name,
               EXTRACT(year FROM report_date)::INT AS season, report_date,
               cumm_rainfall_mm AS v,
               lag(cumm_rainfall_mm) OVER (
                   PARTITION BY scheme_id, EXTRACT(year FROM report_date)
                   ORDER BY report_date) AS prev
          FROM fact_rainfall
         WHERE EXTRACT(month FROM report_date) BETWEEN 6 AND 10
           AND cumm_rainfall_mm IS NOT NULL)
      SELECT count(*) AS n,
             sum(CASE WHEN prev IS NOT NULL AND v < prev - 0.001
                      THEN 1 ELSE 0 END) AS drops,
             coalesce(max(CASE WHEN prev IS NOT NULL AND v < prev
                               THEN prev - v END), 0) AS worst
        FROM s"""
    n, drops, worst = con.execute(q).fetchone()
    pct = 100.0 * drops / max(n, 1)
    print(f"  steps {n:,}   backward {drops:,} ({pct:.3f}%)   "
          f"worst fall {worst:,.0f} mm   limit {args.max_backward_pct}%")
    if pct > args.max_backward_pct:
        failed.append(f"cumulative rainfall falls on {drops:,} steps "
                      f"({pct:.2f}%), limit {args.max_backward_pct}%")
        print("  worst offenders:")
        for x in con.execute("""
          WITH s AS (
            SELECT scheme_name, report_date, cumm_rainfall_mm AS v,
                   lag(cumm_rainfall_mm) OVER (
                       PARTITION BY scheme_id, EXTRACT(year FROM report_date)
                       ORDER BY report_date) AS prev
              FROM fact_rainfall
             WHERE EXTRACT(month FROM report_date) BETWEEN 6 AND 10
               AND cumm_rainfall_mm IS NOT NULL)
          SELECT report_date, scheme_name, prev, v FROM s
           WHERE prev IS NOT NULL AND v < prev - 0.001
           ORDER BY prev - v DESC LIMIT 8""").fetchall():
            print(f"    {x[0]} {str(x[1])[:22]:<23} {x[2]:>8,.0f} -> "
                  f"{x[3]:>8,.0f} mm")

    # ---------------- 2. THE PAGE-3 CROSS-CHECK -------------------------
    rule("2. PAGE-3 CROSS-CHECK — the report prints these figures twice")
    x = con.execute("""
        SELECT count(*) AS n,
               sum(CASE WHEN abs(s.crf - r.cumm_rainfall_mm) > 0.001
                        THEN 1 ELSE 0 END) AS cum_differ,
               sum(CASE WHEN abs(s.rf - r.rain_last_24h_mm) > 0.001
                        THEN 1 ELSE 0 END) AS day_differ
          FROM fact_storage s JOIN fact_rainfall r USING (report_date, scheme_id)
         WHERE s.crf IS NOT NULL AND r.cumm_rainfall_mm IS NOT NULL""").fetchone()
    cpct = 100.0 * x[1] / max(x[0], 1)
    dpct = 100.0 * x[2] / max(x[0], 1)
    print(f"  joined rows {x[0]:,}")
    print(f"  cumulative disagrees {x[1]:,} ({cpct:.3f}%)   "
          f"limit {args.max_disagree_pct}%")
    print(f"  24-hour disagrees    {x[2]:,} ({dpct:.3f}%)")
    if cpct > args.max_disagree_pct or dpct > args.max_disagree_pct:
        failed.append(f"page 3 and the rainfall statement disagree on "
                      f"{x[1]:,} cumulative ({cpct:.2f}%) and {x[2]:,} daily "
                      f"({dpct:.2f}%) values")
        print("  worst by date:")
        for y in con.execute("""
            SELECT s.report_date, count(*) AS n,
                   sum(CASE WHEN abs(s.crf - r.cumm_rainfall_mm) > 0.001
                            THEN 1 ELSE 0 END) AS differ
              FROM fact_storage s
              JOIN fact_rainfall r USING (report_date, scheme_id)
             GROUP BY 1 HAVING differ > 0
             ORDER BY differ DESC, 1 LIMIT 8""").fetchall():
            print(f"    {y[0]}  {y[2]} of {y[1]} rows differ")

    # ---------------- 3. THE STEP IDENTITY ------------------------------
    rule("3. STEP IDENTITY — cumulative must rise by the 24-hour figure")
    z = con.execute("""
        WITH s AS (
          SELECT scheme_id, report_date, cumm_rainfall_mm AS v,
                 rain_last_24h_mm AS d,
                 lag(cumm_rainfall_mm) OVER (
                     PARTITION BY scheme_id, EXTRACT(year FROM report_date)
                     ORDER BY report_date) AS prev,
                 date_diff('day', lag(report_date) OVER (
                     PARTITION BY scheme_id, EXTRACT(year FROM report_date)
                     ORDER BY report_date), report_date) AS gap
            FROM fact_rainfall
           WHERE EXTRACT(month FROM report_date) BETWEEN 6 AND 10
             AND cumm_rainfall_mm IS NOT NULL AND rain_last_24h_mm IS NOT NULL)
        SELECT count(*) AS n,
               sum(CASE WHEN abs((v - prev) - d) > 1.001 THEN 1 ELSE 0 END) AS broken
          FROM s WHERE prev IS NOT NULL AND gap = 1""").fetchone()
    spct = 100.0 * z[1] / max(z[0], 1)
    print(f"  consecutive-day steps {z[0]:,}   off by more than 1 mm "
          f"{z[1]:,} ({spct:.3f}%)   limit {args.max_step_break_pct}%")
    if spct > args.max_step_break_pct:
        failed.append(f"the cumulative series does not advance by the daily "
                      f"figure on {z[1]:,} steps ({spct:.2f}%)")

    # ---------------- what the parser thought column 0 meant -------------
    # Read from the database, not the ledger: fact_rainfall carries the meaning
    # per ROW, so it cannot drift from the data it describes.
    rule("HOW COLUMN 0 WAS READ")
    cols = {c[0] for c in con.execute("DESCRIBE fact_rainfall").fetchall()}
    if "rain_col0_meaning" not in cols:
        print("  fact_rainfall has no rain_col0_meaning column; re-run "
              "parse_cached.py then build_db.py")
    else:
        for x in con.execute("""
            SELECT coalesce(rain_col0_meaning, '(parsed before this was '
                                               'recorded)') AS how,
                   count(*) AS rows, count(DISTINCT report_date) AS dates,
                   min(report_date) AS a, max(report_date) AS b
              FROM fact_rainfall GROUP BY 1 ORDER BY 2 DESC""").fetchall():
            print(f"  {x[0]:<38}{x[1]:>8,} rows  {x[2]:>4} dates  "
                  f"{x[3]} .. {x[4]}")
        sr = con.execute("""
            SELECT count(DISTINCT report_date), min(report_date),
                   max(report_date) FROM fact_rainfall
             WHERE rain_col0_meaning = 'sr_no'""").fetchone()
        if sr[0]:
            print(f"\n  'Sr No' dates: {sr[0]} ({sr[1]} .. {sr[2]}). Column 0 "
                  f"is a row counter on these;\n  the scheme is identified by "
                  f"name against page 3 of the same report (§28).")
            # The repair has to be demonstrated, not assumed: these are the
            # dates that were wrong, so they are the ones that must now agree.
            chk = con.execute("""
                SELECT count(*) AS n,
                       sum(CASE WHEN abs(s.crf - r.cumm_rainfall_mm) > 0.001
                                THEN 1 ELSE 0 END) AS differ
                  FROM fact_storage s
                  JOIN fact_rainfall r USING (report_date, scheme_id)
                 WHERE r.rain_col0_meaning = 'sr_no'""").fetchone()
            print(f"  inside that window: {chk[1]:,} of {chk[0]:,} rows "
                  f"disagree with page 3 "
                  f"({100.0 * chk[1] / max(chk[0], 1):.3f}%)")

    Path(OUT).parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (
          SELECT s.report_date, count(*) AS rows,
                 sum(CASE WHEN abs(s.crf - r.cumm_rainfall_mm) > 0.001
                          THEN 1 ELSE 0 END) AS cum_disagree,
                 sum(CASE WHEN abs(s.rf - r.rain_last_24h_mm) > 0.001
                          THEN 1 ELSE 0 END) AS day_disagree
            FROM fact_storage s JOIN fact_rainfall r USING (report_date, scheme_id)
           GROUP BY 1 ORDER BY 1
        ) TO '{OUT.as_posix()}' (HEADER, DELIMITER ',')""")
    print(f"\nwritten {OUT}")

    rule("RESULT")
    if failed:
        print("RAINFALL INTEGRITY GATE TRIPPED\n")
        for f in failed:
            print(f"  - {f}")
        print("\nThe published rainfall would be wrong. Nothing downstream "
              "should be built on it.\nSee PROJECT_BRIEF §28.")
        sys.exit(3)
    print("CLEAR — all three tests pass.")


if __name__ == "__main__":
    main()

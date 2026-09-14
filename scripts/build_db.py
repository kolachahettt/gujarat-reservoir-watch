"""
Gujarat Reservoir Watch — load the parsed day files into DuckDB.

Model
  dim_scheme     one row per scheme_id, with first_seen / last_seen, the
                 current name/district/taluka/region and a count of name
                 variants so renames and drop-outs are visible.
  fact_storage   one row per (report_date, scheme_id) — the daily detail table.
  fact_rainfall  one row per (report_date, scheme_id) — the per-scheme rainfall
                 table from pages 18-23.
  v_coverage     per-date completeness, joined to the backfill ledger.

Built idempotently from `data/interim/day/*.parquet` and `data/interim/rain/*`,
so it can be re-run at any point during a backfill to load whatever exists.

Usage:  python scripts/build_db.py
"""

import sys
from pathlib import Path

import duckdb
import pandas as pd

DAY_GLOB = "data/interim/day/*.parquet"
RAIN_GLOB = "data/interim/rain/*.parquet"
LEDGER = Path("data/interim/backfill_ledger.csv")
DB = Path("data/processed/reservoir.duckdb")

CUSEC_TO_MCM_PER_DAY = 0.00244658


def main():
    days = sorted(Path("data/interim/day").glob("*.parquet"))
    if not days:
        sys.exit("No parsed day files in data/interim/day/. Run backfill.py first.")
    rains = sorted(Path("data/interim/rain").glob("*.parquet"))
    print(f"day files: {len(days)}   rainfall files: {len(rains)}")

    DB.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB))

    con.execute(f"""
        CREATE OR REPLACE TABLE fact_storage AS
        SELECT
            CAST(report_date AS DATE)        AS report_date,
            CAST(scheme_id  AS INTEGER)      AS scheme_id,
            CAST(sr_no      AS INTEGER)      AS sr_no,
            district, taluka, scheme_name, region, dam_type,
            CAST(source_layout AS INTEGER)   AS source_layout,
            osl_m, frl_m, pwl_m,
            design_gross_mcm, design_live_mcm, design_dead_mcm,
            present_gross_mcm, present_live_mcm, present_dead_mcm,
            pct_filling_src, rf, crf, warning,
            inflow_cusecs, outflow_river_cusecs, outflow_canal_cusecs,
            CASE WHEN design_gross_mcm > 0
                 THEN 100.0 * present_gross_mcm / design_gross_mcm END
                                             AS pct_filling,
            -- Days of water AT TODAY'S RELEASE RATE. NULL where there is no
            -- current release: undefined, never infinite (§8.5).
            CASE WHEN outflow_canal_cusecs > 0
                 THEN present_live_mcm / (outflow_canal_cusecs * {CUSEC_TO_MCM_PER_DAY})
            END                              AS days_water_at_release
        FROM read_parquet('{DAY_GLOB}', union_by_name = true)
    """)

    if rains:
        con.execute(f"""
            CREATE OR REPLACE TABLE fact_rainfall AS
            SELECT
                CAST(report_date AS DATE)   AS report_date,
                CAST(scheme_id AS INTEGER)  AS scheme_id,
                scheme_name, district, region_full,
                cumm_rainfall_mm, rain_last_24h_mm, band_consistent,
                -- How the parser read column 0 of the rainfall statement for
                -- this day: 'scheme_id' or 'sr_no'. NULL for days parsed
                -- before that distinction existed. The report switched to a
                -- row counter for four months of 2025 and misassigned every
                -- row; carrying the meaning per row makes the window visible
                -- in the data rather than only in a ledger. See §28.
                rain_col0_meaning
            FROM read_parquet('{RAIN_GLOB}', union_by_name = true)
        """)
    else:
        con.execute("CREATE OR REPLACE TABLE fact_rainfall AS "
                    "SELECT NULL::DATE report_date, NULL::INTEGER scheme_id "
                    "WHERE false")

    # Scheme dimension. `scheme_id` is the stable key (§3); names drift, so the
    # dimension keeps the latest name and counts how many distinct names a
    # scheme has carried, which is how a rename shows up.
    con.execute("""
        CREATE OR REPLACE TABLE dim_scheme AS
        WITH latest AS (
            SELECT scheme_id, scheme_name, district, taluka, region,
                   ROW_NUMBER() OVER (PARTITION BY scheme_id
                                      ORDER BY report_date DESC) AS rn
            FROM fact_storage
        ),
        agg AS (
            SELECT scheme_id,
                   MIN(report_date) AS first_seen,
                   MAX(report_date) AS last_seen,
                   COUNT(*)                        AS days_present,
                   COUNT(DISTINCT scheme_name)     AS n_name_variants,
                   COUNT(DISTINCT district)        AS n_district_variants,
                   MAX(design_gross_mcm)           AS design_gross_mcm_max,
                   COUNT(DISTINCT design_gross_mcm) AS n_design_variants
            FROM fact_storage GROUP BY scheme_id
        )
        SELECT a.scheme_id, l.scheme_name AS scheme_name_latest,
               l.district AS district_latest, l.taluka AS taluka_latest,
               l.region AS region_latest,
               a.first_seen, a.last_seen, a.days_present,
               a.n_name_variants, a.n_district_variants,
               a.design_gross_mcm_max, a.n_design_variants
        FROM agg a JOIN latest l USING (scheme_id)
        WHERE l.rn = 1
    """)

    if LEDGER.exists():
        led = pd.read_csv(LEDGER, dtype={"report_date": str})
        con.register("led_df", led)
        con.execute("""
            CREATE OR REPLACE TABLE ledger AS
            SELECT CAST(report_date AS DATE) AS report_date, status, http_status,
                   bytes, sha256, pdf_date_line, n_schemes, n_rainfall, note,
                   attempted_utc
            FROM led_df
        """)
    else:
        con.execute("CREATE OR REPLACE TABLE ledger AS "
                    "SELECT NULL::DATE report_date WHERE false")

    con.execute("""
        CREATE OR REPLACE VIEW v_coverage AS
        SELECT f.report_date,
               COUNT(*)                                  AS schemes,
               COUNT(DISTINCT f.scheme_id)               AS distinct_schemes,
               MAX(f.source_layout)                      AS layout,
               SUM(CASE WHEN f.taluka IS NULL THEN 1 ELSE 0 END) AS taluka_null,
               SUM(f.present_gross_mcm)                  AS state_gross_mcm,
               SUM(f.design_gross_mcm)                   AS state_design_mcm,
               SUM(CASE WHEN f.outflow_canal_cusecs > 0 THEN 1 ELSE 0 END)
                                                         AS schemes_releasing
        FROM fact_storage f GROUP BY f.report_date
    """)

    n_f = con.execute("SELECT count(*) FROM fact_storage").fetchone()[0]
    n_r = con.execute("SELECT count(*) FROM fact_rainfall").fetchone()[0]
    n_s = con.execute("SELECT count(*) FROM dim_scheme").fetchone()[0]
    n_d = con.execute("SELECT count(DISTINCT report_date) FROM fact_storage").fetchone()[0]
    print(f"\nfact_storage : {n_f:,} rows over {n_d} dates")
    print(f"fact_rainfall: {n_r:,} rows")
    print(f"dim_scheme   : {n_s} schemes")
    print(f"written: {DB}")
    con.close()


if __name__ == "__main__":
    main()

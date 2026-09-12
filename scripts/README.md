# The pipeline, in order

Read this before running anything. The scripts are not interchangeable steps — each
assumes the previous one finished, and two of them will deliberately stop the whole run.

Only **one** of these touches the network.

| # | Script | Network | What it does |
|---|---|---|---|
| 1 | `backfill.py` | **yes** | Downloads the daily WRD PDFs. Serial, rate-limited, resumable. |
| 2 | `parse_cached.py` | no | Parses every cached PDF to parquet, across worker processes. |
| 3 | `build_db.py` | no | Builds the DuckDB star schema from the parquet. |
| 4 | `check_capacity.py` | no | **THE GATE.** One capacity per scheme-season; halts on an unacknowledged mid-season change. |
| 5 | `report_deviation.py` | no | Coverage, scheme drift, both deviation tables. |
| 6 | `build_view_data.py` | no | All view computation → `web/data/reservoir_view.json`. |

`run_pipeline.py` runs 2–5 in order and stops on the gate. `build_view_data.py` is
separate because it is what you re-run when only the site needs refreshing.

```bash
python scripts/backfill.py --download-only --months 6-10 --delay 1.0
```

```bash
python scripts/run_pipeline.py --date 2026-09-11 --skip-pin
```

```bash
python scripts/build_view_data.py
```

```bash
python scripts/serve.py --root web --port 8021
```

## Supporting scripts

| Script | What |
|---|---|
| `fetch_dam.py` | **The shared parsing library**, plus a one-day ingest. Layout handling, the closed vocabularies, the hazard fixes. Imported by 1, 2 and 6 — change it and everything downstream changes. |
| `pin_changeover.py` | Bisects the Nov–May off-season to date a capacity changeover. Needs the network; run it only when a new restatement appears. |
| `wait_then_run.py` | Waits for a long download, runs retry passes, verifies coverage, then runs the pipeline. For unattended backfills. |
| `serve.py` | Static server with HTTP Range support, for viewing `web/` locally. |

## The two things that will stop a run, on purpose

**Coverage completeness.** `wait_then_run.py` refuses to start the pipeline while any
requested date is missing. A deviation table built on a quarter of a season looks exactly
like one built on all of it, so incomplete coverage is blocked rather than footnoted.

**The capacity gate.** `check_capacity.py` exits 2 and writes
`data/processed/GATE_TRIPPED.txt` if a scheme changes design capacity *within* one season
and is not in its acknowledged list. Three such cases are known and dispositioned; a fourth
needs a human, because excluding a scheme-season from the baseline is not a decision the
code should make on its own.

Neither is a nuisance check. Both exist because the failure they catch is invisible in the
output.

## Order of dependence

```
fetch_dam.py ──────────────┐ (library)
                           ↓
backfill.py → parse_cached.py → build_db.py → check_capacity.py ─┬→ report_deviation.py
  (network)     (parallel)        (DuckDB)      (THE GATE)       └→ build_view_data.py → web/
```

`check_capacity.py` writes `data/processed/season_capacity.csv`, which **both**
`report_deviation.py` and `build_view_data.py` read. Neither will run without it — which is
the point: the decision about what capacity a season has is taken once, in one place.

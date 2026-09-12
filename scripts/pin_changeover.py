"""
Pin a capacity changeover to an exact date by binary search.

Why this exists: the capacity revision happens in the Nov-May off-season, which
the Jun-Oct download range excludes by design. No number of season dates will
narrow the window — the evidence is not in the range. Bisecting the off-season
gap costs about 8 requests per boundary instead of a 14-hour off-season
backfill.

Scope, deliberately narrow: the 2023->2024 boundary where 13 of 16 transitions
land, and Dantiwada separately because its capacity oscillates rather than
stepping once. The other transitions are the same event and are not re-searched.

Politeness: serial, one request at a time, same delay and User-Agent as the
backfill, and every fetched PDF is cached like any other so a re-run costs
nothing.

Usage:
  python scripts/pin_changeover.py --boundary 2023-2024
  python scripts/pin_changeover.py --scheme 4          # Dantiwada, all boundaries
  python scripts/pin_changeover.py --boundary 2023-2024 --scheme 4
"""

import argparse
import ssl
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backfill as bf  # noqa: E402
import fetch_dam as fd  # noqa: E402

DB = Path("data/processed/reservoir.duckdb")
REST = Path("data/processed/capacity_restatements.csv")
OUT = Path("data/processed/capacity_changeover_dates.csv")
EPS_REL = 0.005


def rule(t):
    print(f"\n{'=' * 88}\n{t}\n{'=' * 88}")


def design_on(d, scheme_ids, delay, ctx, cache):
    """Design gross storage on date d, for EVERY scheme in that day's report.

    The cache is keyed on date alone, so it must hold the whole day, not the
    subset one caller asked for. An earlier version cached the filtered dict:
    the shared-boundary search cached 2024-05-31 and 2024-06-01 holding only its
    own 12 schemes, and the Dantiwada search then hit that cache, failed to find
    scheme 4, and reported the days as unreadable. They were not — both parse to
    206 rows and carry scheme 4. That was a bug here, not a server fault.
    """
    if d in cache:
        return cache[d]
    path, _, _, _, note, did_net = bf.fetch_pdf(d, delay, ctx)
    if path is None:
        cache[d] = None
        print(f"    {d}  FETCH FAILED ({note})")
        if did_net:
            time.sleep(delay)
        return None
    import pdfplumber
    with pdfplumber.open(str(path)) as pdf:
        det, _, _, _ = fd.parse_detail(pdf)
    if did_net:
        time.sleep(delay)
    if det.empty:
        cache[d] = None
        print(f"    {d}  PARSED 0 SCHEMES")
        return None
    got = det.set_index("scheme_id")["design_gross_mcm"].to_dict()
    cache[d] = got
    return got


def same(a, b):
    if a is None or b is None:
        return False
    return abs(a - b) / b <= EPS_REL if b else a == b


def bisect(lo_date, hi_date, probe_id, old_val, new_val, ids, delay, ctx, cache):
    """Find the earliest date carrying new_val, bracketed by (lo_date, hi_date].

    lo_date is known to carry old_val, hi_date is known to carry new_val.
    """
    lo, hi = lo_date, hi_date
    probes = 0
    while (hi - lo).days > 1:
        mid = lo + timedelta(days=(hi - lo).days // 2)
        vals = design_on(mid, ids, delay, ctx, cache)
        probes += 1
        # Two distinct failures, previously conflated: the DAY is unreadable
        # (vals is None), or the day is fine but this SCHEME is absent from it.
        # The second is a real finding about the scheme, not a fetch problem.
        if vals is not None and probe_id not in vals:
            print(f"    probe {mid}  day readable ({len(vals)} schemes) but "
                  f"scheme {probe_id} ABSENT — scheme-level gap, not a fetch fault")
        if vals is None or probe_id not in vals:
            alt = mid + timedelta(days=1)
            if alt >= hi:
                print(f"    cannot resolve around {mid}; leaving bracket "
                      f"({lo}, {hi}]")
                break
            vals = design_on(alt, ids, delay, ctx, cache)
            probes += 1
            if vals is None or probe_id not in vals:
                print(f"    cannot decide at {mid} or {alt}; stopping")
                break
            mid = alt
        v = vals[probe_id]
        side = "new" if same(v, new_val) else "old" if same(v, old_val) else "?"
        print(f"    probe {mid}  scheme {probe_id} = {v:g}  -> {side}")
        if side == "new":
            hi = mid
        elif side == "old":
            lo = mid
        else:
            print(f"    unexpected third value {v:g} at {mid}; stopping")
            break
    return lo, hi, probes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boundary", default=None, help="e.g. 2023-2024")
    ap.add_argument("--scheme", type=int, default=None,
                    help="pin every boundary of one scheme (for oscillators)")
    ap.add_argument("--schemes", default=None,
                    help="comma-separated scheme ids to search as ONE boundary, "
                         "e.g. the stragglers that did not flip with the rest")
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()
    if not REST.exists():
        sys.exit(f"MISSING {REST}. Run check_capacity.py first.")

    rest = pd.read_csv(REST)
    con = duckdb.connect(str(DB), read_only=True)
    ctx = bf.ctx_noverify()
    cache = {}
    results = []

    jobs = []
    if args.boundary:
        sb, sa = (int(x) for x in args.boundary.split("-"))
        grp = rest[(rest["season_before"] == sb) & (rest["season_after"] == sa)]
        if args.scheme is not None:
            grp = grp[grp["scheme_id"] != args.scheme]
        if grp.empty:
            print(f"no transitions at the {sb}->{sa} boundary")
        else:
            jobs.append((f"{sb}->{sa} shared boundary", grp))
    if args.schemes:
        want = [int(x) for x in args.schemes.split(",")]
        grp = rest[rest["scheme_id"].isin(want)]
        missing = sorted(set(want) - set(grp["scheme_id"]))
        if missing:
            print(f"not in the restatement table: {missing}")
        for (sb, sa), sub in grp.groupby(["season_before", "season_after"]):
            jobs.append((f"stragglers {int(sb)}->{int(sa)} "
                         f"({len(sub)} schemes)", sub))
    if args.scheme is not None:
        grp = rest[rest["scheme_id"] == args.scheme]
        for _, r in grp.iterrows():
            jobs.append((f"scheme {args.scheme} "
                         f"{int(r['season_before'])}->{int(r['season_after'])}",
                         pd.DataFrame([r])))

    for label, grp in jobs:
        rule(f"BISECTING {label}")
        ids = sorted(grp["scheme_id"].astype(int).tolist())
        sb = int(grp["season_before"].iloc[0])
        sa = int(grp["season_after"].iloc[0])
        # Anchors: the last loaded date of the earlier season and the first of
        # the later one. Both are already known, so neither costs a request.
        anchors = con.execute("""
            SELECT EXTRACT(year FROM report_date)::INT AS season,
                   min(report_date) AS first_date, max(report_date) AS last_date
            FROM fact_storage
            WHERE EXTRACT(year FROM report_date) IN (?, ?)
            GROUP BY 1 ORDER BY 1
        """, [sb, sa]).df()
        if len(anchors) < 2:
            print("  both seasons must be loaded to anchor the search; skipping")
            continue
        lo_date = pd.Timestamp(anchors.iloc[0]["last_date"]).date()
        hi_date = pd.Timestamp(anchors.iloc[1]["first_date"]).date()

        probe_id = int(grp.reindex(grp["change_pct"].abs().sort_values(
            ascending=False).index)["scheme_id"].iloc[0])
        old_val = float(grp[grp["scheme_id"] == probe_id]["design_before_mcm"].iloc[0])
        new_val = float(grp[grp["scheme_id"] == probe_id]["design_after_mcm"].iloc[0])
        print(f"  schemes in this boundary : {len(ids)}")
        print(f"  bracket                  : ({lo_date}, {hi_date}]  "
              f"{(hi_date - lo_date).days} days")
        print(f"  probe scheme             : {probe_id} "
              f"({old_val:g} -> {new_val:g}, sharpest change)")

        lo, hi, probes = bisect(lo_date, hi_date, probe_id, old_val, new_val,
                                ids, args.delay, ctx, cache)
        width = (hi - lo).days
        print(f"\n  narrowed to ({lo}, {hi}]  width {width} day(s) "
              f"in {probes} probes")

        # Confirm: did every scheme in this boundary flip on the same day?
        together, split = [], []
        if width == 1:
            before = design_on(lo, ids, args.delay, ctx, cache)
            after = design_on(hi, ids, args.delay, ctx, cache)
            if before and after:
                for _, r in grp.iterrows():
                    sid = int(r["scheme_id"])
                    b, a = before.get(sid), after.get(sid)
                    flipped = (same(b, float(r["design_before_mcm"]))
                               and same(a, float(r["design_after_mcm"])))
                    (together if flipped else split).append(sid)
                print(f"  flipped exactly at {hi}: {len(together)} of {len(ids)}")
                if split:
                    print(f"  NOT flipped at {hi} (different date): {split}")
                    print("  -> this boundary is more than one event; those "
                          "schemes need their own search")
        results.append({
            "label": label, "schemes": len(ids), "season_before": sb,
            "season_after": sa, "probe_scheme": probe_id,
            "bracket_lo": str(lo), "bracket_hi": str(hi),
            "window_days": width,
            "changeover_date": str(hi) if width == 1 else "",
            "schemes_flipping_together": len(together),
            "schemes_flipping_elsewhere": ",".join(str(s) for s in split),
            "probes": probes,
        })

    if results:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(results).to_csv(OUT, index=False)
        rule("RESULT")
        print(pd.DataFrame(results).to_string(index=False))
        print(f"\nnetwork requests this run: {len([k for k in cache])}")
        print(f"written: {OUT}")
    con.close()


if __name__ == "__main__":
    main()

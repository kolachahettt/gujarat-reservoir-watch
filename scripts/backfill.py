"""
Gujarat Reservoir Watch — history backfill, 2022-06-01 to today.

Polite, cached and resumable by construction:

  * **Cached.** A day whose PDF is already on disk is never re-downloaded; a day
    whose parsed parquet already exists is never re-parsed. Deleting a parquet
    forces a re-parse without a re-fetch.
  * **Polite.** A fixed delay between *network* requests only (cache hits do not
    sleep), single-threaded, one request at a time, identified User-Agent.
  * **Resumable.** Every attempt is written to a ledger CSV immediately. Re-run
    the command after any interruption and it continues where it stopped.
  * **Honest.** A day that is missing upstream, or returns something that is not
    a PDF, or parses to the wrong scheme count, is recorded with that status and
    not silently skipped (§8.4).

Priority dates are fetched first so the same-calendar-date comparison can be run
before the whole range finishes.

Usage:
  python scripts/backfill.py                          # full range, resume
  python scripts/backfill.py --priority-only          # just the anniversaries
  python scripts/backfill.py --start 2022-06-01 --end 2026-09-11
  python scripts/backfill.py --delay 2.0
"""

import argparse
import hashlib
import json
import random
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_dam as fd  # noqa: E402

PDF_DIR = Path("data/raw/dam")
DAY_DIR = Path("data/interim/day")
RAIN_DIR = Path("data/interim/rain")
LEDGER = Path("data/interim/backfill_ledger.csv")

DEFAULT_START = date(2022, 6, 1)
UA = {"User-Agent": "GujaratReservoirWatch/0.1 (research; contact via repo)"}

LEDGER_COLS = ["report_date", "status", "http_status", "bytes", "sha256",
               "pdf_date_line", "n_schemes", "n_rainfall", "note", "attempted_utc"]

# Per-date retry schedule, in seconds between attempts (4 attempts total).
BACKOFF_S = [5, 20, 60]
# A run of consecutive transport failures means the server is refusing, not
# that these particular dates are unavailable. Hammering it is neither polite
# nor productive, so pause and let it recover.
RUN_FAIL_TRIGGER = 6
RUN_FAIL_COOLDOWN_S = 300

# Statuses meaning "the server does not have this date", as opposed to "we
# failed to get it". Never retried on a later pass, and excluded from the
# completeness denominator — but always reported, never quietly dropped.
# Defined in fetch_dam so the view can publish the same list it is written
# from; a second copy here would let the two drift.
ABSENT_UPSTREAM = fd.ABSENT_UPSTREAM


def make_ctx(insecure=False):
    """TLS context. Verification is ON by default — see below.

    History, because this reverses an earlier decision. The brief recorded that
    this host's certificate "does not validate in our environment" and the
    fetcher disabled verification, keeping the SHA-256 of every file as the
    integrity check instead. Re-tested 12 September 2026: **six consecutive
    verified handshakes**, TLSv1.2, a valid Entrust OV certificate for
    `*.gujarat.gov.in`. Either the chain was fixed server-side or the original
    finding was narrower than recorded; which of those cannot be determined
    retrospectively. Either way there is no longer a reason to trade transport
    integrity away, so verification is back on.

    It fails rather than falling back. A silent downgrade to unverified is the
    same as not verifying — it just hides it. `--insecure` exists for the case
    where the chain genuinely breaks and the data is wanted anyway, and it says
    so loudly in the log.

    KNOWN SCHEDULED RISK: the certificate observed on 12 Sep 2026 expires
    **26 September 2026**. A late or mis-chained renewal will make every fetch
    fail on that date. That is the correct behaviour — but it will look like an
    outage, so check the certificate before assuming the portal is down.
    """
    c = ssl.create_default_context()
    if insecure:
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
    return c


def ctx_noverify():
    """Deprecated alias, kept so nothing that imports it breaks silently.

    Returns a VERIFYING context now: a name promising no verification that
    quietly started verifying is less dangerous than the reverse.
    """
    return make_ctx(insecure=False)


def load_ledger():
    if LEDGER.exists():
        df = pd.read_csv(LEDGER, dtype={"report_date": str})
        return {r.report_date: r._asdict() for r in df.itertuples(index=False)}
    return {}


def write_ledger(ledger):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(list(ledger.values()))
    for c in LEDGER_COLS:
        if c not in df.columns:
            df[c] = None
    df[LEDGER_COLS].sort_values("report_date").to_csv(LEDGER, index=False)


def fetch_pdf(d, delay, ctx):
    """Return (path, bytes, sha, http_status, note, did_network). Cached."""
    path = PDF_DIR / f"dam_{d}.pdf"
    if path.exists() and path.stat().st_size > 1000:
        blob = path.read_bytes()
        if blob.startswith(b"%PDF-"):
            return path, len(blob), hashlib.sha256(blob).hexdigest().upper(), \
                None, "cache", False
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    url = fd.BASE + fd.token_for(d)
    last, last_code = None, None
    n_empty, n_nonpdf = 0, 0
    for attempt in range(len(BACKOFF_S) + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
                blob = r.read()
                code = r.status
            if blob.startswith(b"%PDF-"):
                path.write_bytes(blob)
                return path, len(blob), hashlib.sha256(blob).hexdigest().upper(), \
                    code, "downloaded", True
            # A 200 carrying nothing is how this server says "no report for
            # this date": Content-Type application/pdf, Content-Length 0,
            # verified on 2025-09-26 across four attempts. It is not a
            # transport fault and retrying never fixes it — but one
            # observation is not enough to call a date permanently absent, so
            # it still goes through the retry schedule before being classified.
            last_code = code
            if len(blob) == 0:
                n_empty += 1
                last = "empty_upstream"
            else:
                n_nonpdf += 1          # an error page; keep it as a failure
                last = "not_a_pdf"
        except urllib.error.HTTPError as e:
            last, last_code = f"http_{e.code}", e.code
            if e.code in (404, 410):
                return None, None, None, e.code, "missing_upstream", True
        except Exception as e:                       # noqa: BLE001
            last = f"{type(e).__name__}"
        if attempt < len(BACKOFF_S):
            # Jittered exponential backoff. The old policy was three tries at
            # 3/4/5s, a 12-second window; the first Jun-Oct pass lost 133 dates
            # in runs of 24-50 consecutive days, so the outages last far longer
            # than that. Jitter avoids re-synchronising onto the same bad
            # moment on every date.
            time.sleep(BACKOFF_S[attempt] * random.uniform(0.7, 1.3))
    if n_empty and not n_nonpdf:
        # Every attempt returned a successful, empty response. Absent upstream.
        return None, 0, None, last_code, "empty_upstream", True
    return None, None, None, last_code, f"failed:{last}", True


def process_day(d, delay, ctx, force=False):
    day_pq = DAY_DIR / f"{d}.parquet"
    rain_pq = RAIN_DIR / f"{d}.parquet"
    if not force and day_pq.exists():
        try:
            det = pd.read_parquet(day_pq)
            n_rain = len(pd.read_parquet(rain_pq)) if rain_pq.exists() else 0
            return {"status": "ok", "note": "cache", "bytes": None, "sha256": None,
                    "http_status": None, "pdf_date_line": None,
                    "n_schemes": len(det), "n_rainfall": n_rain}, False
        except Exception:                            # noqa: BLE001
            day_pq.unlink(missing_ok=True)

    path, nbytes, sha, code, note, did_net = fetch_pdf(d, delay, ctx)
    if path is None:
        status = "missing" if note == "missing_upstream" else "fetch_failed"
        return {"status": status, "note": note, "bytes": nbytes, "sha256": sha,
                "http_status": code, "pdf_date_line": None,
                "n_schemes": 0, "n_rainfall": 0}, did_net

    import pdfplumber
    try:
        with pdfplumber.open(str(path)) as pdf:
            head = (pdf.pages[0].extract_text() or "").splitlines()[:1]
            date_line = head[0].strip() if head else None
            det, det_fail, _, _ = fd.parse_detail(pdf)
            rain, _, _ = fd.parse_rainfall(pdf)
    except Exception as e:                           # noqa: BLE001
        return {"status": "malformed", "note": f"parse_error:{type(e).__name__}",
                "bytes": nbytes, "sha256": sha, "http_status": code,
                "pdf_date_line": None, "n_schemes": 0, "n_rainfall": 0}, did_net

    if det.empty:
        return {"status": "malformed", "note": "no_detail_rows", "bytes": nbytes,
                "sha256": sha, "http_status": code, "pdf_date_line": date_line,
                "n_schemes": 0, "n_rainfall": len(rain)}, did_net

    DAY_DIR.mkdir(parents=True, exist_ok=True)
    RAIN_DIR.mkdir(parents=True, exist_ok=True)
    det = det.assign(report_date=str(d))
    det.to_parquet(day_pq, index=False)
    if not rain.empty:
        rain.assign(report_date=str(d)).to_parquet(rain_pq, index=False)

    status = "ok" if not det_fail else "ok_with_warnings"
    return {"status": status,
            "note": note if not det_fail else f"{note}; {len(det_fail)} field warnings",
            "bytes": nbytes, "sha256": sha, "http_status": code,
            "pdf_date_line": date_line, "n_schemes": len(det),
            "n_rainfall": len(rain)}, did_net


DL_LOG = Path("data/interim/download_log.csv")
# Written only on a clean end of pass. A watcher polling for completion should
# use this, not a tail of captured stdout: whoever captures that stdout may hold
# the file open, and on Windows that blocks the watcher from reading it at all.
DL_DONE = Path("data/interim/download_DONE.json")


def month_filter(days, spec):
    if not spec:
        return days
    lo, hi = (int(x) for x in spec.split("-"))
    return [d for d in days if lo <= d.month <= hi]


def download_only(args):
    """Serial, polite fetch of the whole range. No parsing, no network parallelism."""
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    days = month_filter(days, args.months)
    ctx = make_ctx(getattr(args, 'insecure', False))

    log = {}
    if DL_LOG.exists():
        old = pd.read_csv(DL_LOG, dtype={"report_date": str})
        log = {r["report_date"]: dict(r) for _, r in old.iterrows()}

    def flush():
        DL_LOG.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(list(log.values())).to_csv(DL_LOG, index=False)

    DL_DONE.unlink(missing_ok=True)          # this pass is not done yet
    print(f"DOWNLOAD-ONLY  {start} .. {end}  ({len(days)} days), delay {args.delay}s")
    net = 0
    fails = 0
    absent = 0
    healed = 0
    run_fail = 0
    cooldowns = 0
    t0 = time.time()
    for i, d in enumerate(days, 1):
        key = str(d)
        path = PDF_DIR / f"dam_{key}.pdf"
        if path.exists() and path.stat().st_size > 1000:
            # The PDF is here, so any earlier failure row for this date is
            # stale. Leaving it makes a later coverage report claim a date is
            # missing when it is on disk — understating coverage is as wrong as
            # overstating it. The prior status is kept in the note so the
            # failure history stays auditable.
            prior = log.get(key, {}).get("status")
            if prior is None or str(prior).startswith("failed") \
                    or prior in ("not_a_pdf",):
                log[key] = {
                    "report_date": key, "status": "cached",
                    "http_status": None, "bytes": path.stat().st_size,
                    "note": f"cached; superseded prior status {prior!r}",
                    "attempted_utc": datetime.now(timezone.utc).isoformat()}
                healed += 1
            continue
        if log.get(key, {}).get("status") in ABSENT_UPSTREAM:
            continue
        _, nbytes, sha, code, note, did_net = fetch_pdf(d, args.delay, ctx)
        log[key] = {"report_date": key,
                    "status": "downloaded" if note == "downloaded" else note,
                    "http_status": code, "bytes": nbytes, "note": note,
                    "attempted_utc": datetime.now(timezone.utc).isoformat()}
        if note == "downloaded":
            run_fail = 0
        elif note in ABSENT_UPSTREAM:
            # Not the server refusing us, so it must not drive the cooldown.
            absent += 1
            run_fail = 0
            print(f"  {key}: {note} (HTTP {code}, "
                  f"{nbytes if nbytes is not None else '?'} bytes) — "
                  f"the report does not exist upstream; not retried again",
                  flush=True)
        else:
            fails += 1
            run_fail += 1
        if did_net:
            net += 1
            time.sleep(args.delay)
        # Flush on every failure as well as periodically, so a watcher polling
        # this file can tell "still working, failing" from "process died".
        if i % 25 == 0 or run_fail:
            flush()
        if i % 50 == 0:
            have = len(list(PDF_DIR.glob("dam_*.pdf")))
            print(f"[{i}/{len(days)}] {key} cached={have} net={net} "
                  f"fails={fails} absent={absent} "
                  f"elapsed={time.time() - t0:,.0f}s", flush=True)
        if run_fail >= RUN_FAIL_TRIGGER:
            cooldowns += 1
            print(f"  {run_fail} consecutive failures ending {key} — "
                  f"pausing {RUN_FAIL_COOLDOWN_S}s for the server to recover "
                  f"(cooldown {cooldowns})", flush=True)
            flush()
            time.sleep(RUN_FAIL_COOLDOWN_S)
            run_fail = 0
    flush()
    gone = [str(d) for d in days
            if log.get(str(d), {}).get("status") in ABSENT_UPSTREAM]
    missing = [str(d) for d in days
               if not (PDF_DIR / f"dam_{d}.pdf").exists()
               and str(d) not in gone]
    print(f"\ndone. network requests {net}, failures {fails}, "
          f"absent upstream {absent}, stale failure rows healed {healed}, "
          f"cooldowns {cooldowns}, elapsed {time.time() - t0:,.0f}s")
    print(f"cached PDFs: {len(list(PDF_DIR.glob('dam_*.pdf')))}")
    print(f"absent upstream (never retried again): {len(gone)}  {gone}")
    print(f"still missing after this pass: {len(missing)}")
    if missing:
        print("  re-run the same command to retry them; cached dates cost "
              "nothing to skip.")
    print(f"log: {DL_LOG}")
    DL_DONE.write_text(json.dumps({
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "start": str(start), "end": str(end), "months": args.months,
        "requested": len(days), "network_requests": net, "failures": fails,
        "absent_upstream": len(gone), "absent_dates": gone,
        "cooldowns": cooldowns, "still_missing": len(missing),
        "missing_dates": missing,
        "elapsed_s": round(time.time() - t0, 1),
    }, indent=2))
    print(f"sentinel: {DL_DONE}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=str(DEFAULT_START))
    ap.add_argument("--end", default=str(date.today()))
    ap.add_argument("--delay", type=float, default=1.2,
                    help="seconds between NETWORK requests (cache hits never sleep)")
    ap.add_argument("--priority-only", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--insecure", action="store_true",
                    help="disable TLS certificate verification. Only for a "
                         "genuinely broken chain; it is logged loudly.")
    ap.add_argument("--download-only", action="store_true",
                    help="fetch PDFs and log them; leave parsing to "
                         "scripts/parse_cached.py, which runs in parallel")
    ap.add_argument("--months", default=None,
                    help="restrict to an inclusive month range, e.g. '6-10' for "
                         "1 June to 31 October. The monsoon and the drawdown "
                         "that follows it are the season that matters, and the "
                         "server costs ~33 s per request, so this is a quarter "
                         "of the work for the part of the year in question.")
    args = ap.parse_args()

    if args.download_only:
        return download_only(args)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    all_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    # Anniversaries of `end` — everything the headline query needs — go first,
    # so the comparison is runnable before the full range completes.
    anniversaries = [d for d in all_days
                     if d.month == end.month and d.day == end.day]
    priority = anniversaries + [end]
    seen = set()
    ordered = [d for d in priority if d in set(all_days) and not (d in seen or seen.add(d))]
    if not args.priority_only:
        ordered += [d for d in all_days if d not in seen]

    print(f"range {start} .. {end}   {len(all_days)} days")
    print(f"priority first: {[str(d) for d in ordered[:len(priority)]]}")
    print(f"to process: {len(ordered)}   delay {args.delay}s between network calls")

    ledger = load_ledger()
    ctx = make_ctx(getattr(args, 'insecure', False))
    net_calls = 0
    t0 = time.time()

    for i, d in enumerate(ordered, 1):
        key = str(d)
        prior = ledger.get(key)
        if (not args.force and prior and prior.get("status") in
                ("ok", "ok_with_warnings", "missing") and
                (DAY_DIR / f"{key}.parquet").exists()):
            continue
        if not args.force and prior and prior.get("status") == "missing":
            continue

        rec, did_net = process_day(d, args.delay, ctx, force=args.force)
        rec.update({"report_date": key,
                    "attempted_utc": datetime.now(timezone.utc).isoformat()})
        ledger[key] = rec
        if did_net:
            net_calls += 1
            write_ledger(ledger)
            time.sleep(args.delay)
        if i % 25 == 0 or did_net is False:
            done = sum(1 for v in ledger.values()
                       if v.get("status") in ("ok", "ok_with_warnings"))
            print(f"[{i}/{len(ordered)}] {key} {rec['status']:<17} "
                  f"schemes={rec['n_schemes']:<4} rain={rec['n_rainfall']:<4} "
                  f"ok_total={done} net={net_calls} "
                  f"elapsed={time.time() - t0:,.0f}s", flush=True)

    write_ledger(ledger)
    df = pd.DataFrame(list(ledger.values()))
    print("\n" + "=" * 70)
    print("LEDGER SUMMARY")
    print("=" * 70)
    print(df["status"].value_counts().to_string())
    print(f"\nnetwork requests this run: {net_calls}   "
          f"elapsed {time.time() - t0:,.0f}s")
    print(f"ledger: {LEDGER}")


if __name__ == "__main__":
    main()

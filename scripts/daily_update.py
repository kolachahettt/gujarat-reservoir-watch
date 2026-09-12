"""
Daily update — fetch one report, verify it, rebuild the site, commit and push.

WHY THIS RUNS LOCALLY AND NOT IN CI
-----------------------------------
Probed 12 September 2026: a GitHub-hosted runner cannot reach
wrd-dam.gujarat.gov.in at all. Raw TCP to 103.78.200.187:443 times out and the
IPv6 address is unreachable, from egress 172.214.44.0. That is the portal
refusing the address range, not an IPv6 routing artefact — the IPv4-forced
probe failed too. So the refresh has to run on a machine the portal serves.
See PROJECT_BRIEF §17.

THE ORDER, AND IT STOPS AT THE FIRST FAILURE
--------------------------------------------
 1. target date = yesterday in IST, because the report is Indian and a UTC
    "yesterday" is the wrong day for seven and a half hours of every day
 2. already have it?                 -> exit 0, nothing to do, no network
 3. fetch ONE report, existing retry schedule (4 attempts, jittered backoff)
 4. empty 200 upstream               -> exit 0 QUIETLY, no commit
 5. exactly 206 rows, and the PDF's own date line agrees with the request
 6. closed vocabularies recognised   (field warnings are normal and allowed)
 7. RECONCILE against the report's own grand total
 8. rebuild DuckDB
 9. CAPACITY GATE                    -> exit 2, halt, no commit
10. staleness: newest data within --max-stale-days of today
11. rebuild the view — in season only (1 Jun – 31 Oct)
12. commit, with the report date in the subject, and push

EXIT CODES
  0  committed, or nothing to do, or not published yet
  1  loud failure — look at the log
  2  capacity gate tripped; a new mid-season change needs a human

Run it by hand before trusting the schedule:
  python scripts/daily_update.py --dry-run
  python scripts/daily_update.py --date 2026-09-11 --dry-run
  python scripts/daily_update.py --no-push
"""

import argparse
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)                      # every path below is repo-relative
sys.path.insert(0, str(ROOT / "scripts"))

# The log is prose and contains en dashes and section marks. Windows consoles
# default to cp1252 and render those as replacement characters, which makes a
# log written to be read by a human harder to read. The file handle is already
# UTF-8; this fixes the console half.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                    # noqa: BLE001
        pass

import backfill as bf               # noqa: E402
import fetch_dam as fd              # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
SEASON_MONTHS = (6, 7, 8, 9, 10)

DAY_DIR = Path("data/interim/day")
PDF_DIR = Path("data/raw/dam")
VIEW = Path("web/data/reservoir_view.json")
DAILY_LOG_CSV = Path("data/processed/daily_log.csv")
LOG_DIR = Path("logs")
HALT = Path("data/processed/GATE_TRIPPED.txt")

_log_fh = None


def log(msg=""):
    line = f"{datetime.now(IST):%Y-%m-%d %H:%M:%S %Z} | {msg}" if msg else ""
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


def rule(t):
    log()
    log("=" * 66)
    log(t)
    log("=" * 66)


def die(code, verdict, detail=""):
    rule(f"VERDICT: {verdict}")
    if detail:
        for ln in detail.strip().splitlines():
            log("  " + ln)
    log(f"exit {code}")
    sys.exit(code)


def run(cmd, label, allow=(0,)):
    log(f"$ {' '.join(cmd)}")
    p = subprocess.run([sys.executable, "-u"] + cmd, capture_output=True,
                       text=True, errors="replace")
    for ln in (p.stdout or "").splitlines():
        log("  | " + ln)
    if p.stderr:
        for ln in p.stderr.splitlines()[-25:]:
            log("  ! " + ln)
    if p.returncode not in allow:
        die(1, f"{label} FAILED (exit {p.returncode})",
            "The step above did not succeed. Nothing has been committed.")
    return p.returncode


def git(*args, check=True):
    p = subprocess.run(["git"] + list(args), capture_output=True, text=True,
                       errors="replace")
    if check and p.returncode != 0:
        die(1, "GIT FAILED", f"git {' '.join(args)}\n{p.stdout}\n{p.stderr}")
    return p.stdout.strip()


def newest_loaded_date():
    ds = sorted(p.stem for p in DAY_DIR.glob("*.parquet"))
    return date.fromisoformat(ds[-1]) if ds else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="override the target date (default: yesterday IST)")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--max-stale-days", type=int, default=3,
                    help="fail loudly if the newest data is older than this")
    ap.add_argument("--dry-run", action="store_true",
                    help="do everything except commit and push")
    ap.add_argument("--no-push", action="store_true",
                    help="commit but do not push")
    ap.add_argument("--insecure", action="store_true",
                    help="disable TLS verification (logged loudly)")
    args = ap.parse_args()

    global _log_fh
    LOG_DIR.mkdir(exist_ok=True)
    _log_fh = open(LOG_DIR / "daily_update.log", "a", encoding="utf-8")

    now_ist = datetime.now(IST)
    target = (date.fromisoformat(args.date) if args.date
              else (now_ist.date() - timedelta(days=1)))
    in_season = target.month in SEASON_MONTHS

    rule(f"DAILY UPDATE — target {target}")
    log(f"now (IST)          {now_ist:%Y-%m-%d %H:%M:%S}")
    log(f"target date        {target}  ({'in' if in_season else 'OFF'} season)")
    log(f"repo               {ROOT}")
    log(f"mode               {'DRY RUN' if args.dry_run else 'live'}"
        f"{', no push' if args.no_push else ''}")
    if args.insecure:
        log("TLS                !! VERIFICATION DISABLED via --insecure !!")

    # ---- 2. is there actually anything left to do? ----------------------
    # "Already parsed" is the wrong question, and getting it wrong strands
    # data: --dry-run is NOT side-effect free - it fetches, parses and
    # rebuilds, and skips only the commit. Keying the early exit on the
    # parquet alone meant a dry run consumed the day and the next real run
    # reported nothing to do while the day sat uncommitted forever.
    #
    # The right question is whether the day is parsed AND recorded AND there
    # is nothing outstanding to commit. Anything less and the run continues
    # from wherever it got to. A cached PDF costs no network request either
    # way, so continuing is cheap and makes the run self-healing.
    parsed = (DAY_DIR / f"{target}.parquet").exists()
    recorded = (DAILY_LOG_CSV.exists()
                and f"{target}," in DAILY_LOG_CSV.read_text(encoding="utf-8"))
    outstanding = git("status", "--porcelain", "--", str(VIEW),
                      str(DAILY_LOG_CSV), "data/processed")
    log(f"parsed / recorded  {parsed} / {recorded}")
    if parsed and recorded and not outstanding:
        die(0, "NOTHING TO DO",
            "The day is parsed, recorded in the daily log, and nothing is\n"
            "outstanding to commit. This is the normal result of a second run\n"
            "on the same day. No network request was made.")
    if outstanding:
        log("outstanding changes to commit:")
        for ln in outstanding.splitlines():
            log("  " + ln)

    # ---- 3. fetch one report --------------------------------------------
    rule("FETCH")
    ctx = bf.make_ctx(args.insecure)
    path, nbytes, sha, code, note, did_net = bf.fetch_pdf(target, args.delay, ctx)
    log(f"status             {note}  (http {code})")

    # ---- 4. not published yet: quiet ------------------------------------
    if note == "empty_upstream":
        log(f"bytes              {nbytes}")
        log("\nHTTP 200 with a zero-length body is how this server says "
            "'no report for\nthat date'. It does not use 404.")
        newest = newest_loaded_date()
        gap = (now_ist.date() - newest).days if newest else 9999
        log(f"newest loaded      {newest}  ({gap} days behind today IST)")
        if gap > args.max_stale_days:
            die(1, f"STALE — {gap} DAYS BEHIND",
                f"The report for {target} is not published, and the newest data "
                f"is {gap} days\nold, past the {args.max_stale_days}-day "
                f"threshold. A publishing lag this long is\nnot a lag. Check "
                f"the portal by hand.")
        die(0, "NOT PUBLISHED YET",
            "Quiet by design: a one-day lag must not produce a daily alarm, or\n"
            "the alarm stops meaning anything. Nothing was committed.")

    if path is None:
        die(1, f"FETCH FAILED — {note}",
            "Four attempts with jittered backoff all failed. If this persists, "
            "check\nwhether the certificate expired (a scheduled risk: the one "
            "seen on\n12 Sep 2026 expires 26 Sep 2026) before assuming the "
            "portal is down.")
    log(f"bytes              {nbytes:,}")
    log(f"sha256             {sha}")

    # ---- 5/6/7. verify the day before letting it near the database ------
    rule("VERIFY")
    import pdfplumber
    with pdfplumber.open(str(path)) as pdf:
        head = (pdf.pages[0].extract_text() or "").splitlines()[:1]
        date_line = head[0].strip() if head else ""
        det, det_fail, _, _ = fd.parse_detail(pdf)
        abstract = fd.parse_abstract(pdf)

    log(f"pdf date line      {date_line!r}")
    if str(target.day).zfill(2) not in date_line or \
            str(target.year) not in date_line:
        die(1, "DATE MISMATCH",
            f"The PDF's own header does not mention {target}. The server served "
            f"a\ndifferent day than was requested; the row values cannot be "
            f"trusted as\n{target}.")

    log(f"rows parsed        {len(det)}")
    if len(det) != fd.N_SCHEMES_EXPECTED:
        die(1, f"ROW COUNT {len(det)}, EXPECTED {fd.N_SCHEMES_EXPECTED}",
            "A dropped row and a genuinely new scheme look identical from here, "
            "so\nthis stops for a human either way. If a dam really was added, "
            "update\nN_SCHEMES_EXPECTED in fetch_dam.py and re-run.")

    # Field warnings are normal; 190 of 732 historical days have them.
    log(f"field warnings     {len(det_fail)} (normal; not blocking)")
    bad_vocab = fd.check_vocabularies(det)
    if bad_vocab:
        die(1, "UNRECOGNISED CLOSED-VOCABULARY VALUE",
            "\n".join(f"{k}: {v}" for k, v in bad_vocab.items()) +
            "\n\nA closed vocabulary grew. Silently bucketing the new value "
            "would\ncorrupt every count that uses it, so this blocks. Add it "
            "to the\nvocabulary in fetch_dam.py once you know what it means.")

    rec = fd.reconcile(det, abstract)
    if "design" in rec:
        log(f"design gross       abstract {rec['design']['abstract']:>12,.2f}"
            f"   parsed {rec['design']['parsed']:>12,.2f}"
            f"   {rec['design']['residual_ratio']:.2e}")
        log(f"today  gross       abstract {rec['today']['abstract']:>12,.2f}"
            f"   parsed {rec['today']['parsed']:>12,.2f}"
            f"   {rec['today']['residual_ratio']:.2e}")
    if not rec["ok"]:
        die(1, "RECONCILIATION FAILED", (rec["reason"] or "") +
            "\n\nThe parsed rows do not reproduce the total the report computes "
            "for\nitself. That is the strongest signal available that a column "
            "shifted\nor a wrapped number was truncated. Nothing committed.")
    log(f"reconciliation     OK (worst residual {rec['worst_ratio']:.2e} "
        f"of total)")

    state_pct = 100.0 * rec["today"]["parsed"] / rec["design"]["parsed"]
    log(f"state filling      {state_pct:.2f}%")

    # ---- 8. persist ------------------------------------------------------
    # parse_cached does the authoritative parquet + ledger write. The PDF is
    # therefore opened twice — once above for the checks, once by it — which
    # costs ~15s a day and keeps one implementation of persistence rather than
    # two that can drift.
    rule("PARSE AND REBUILD")
    run(["scripts/parse_cached.py", "--workers", "4"], "parse")
    run(["scripts/build_db.py"], "build_db")

    # ---- 9. the gate -----------------------------------------------------
    HALT.unlink(missing_ok=True)
    if run(["scripts/check_capacity.py"], "capacity gate", allow=(0, 2)) == 2:
        HALT.parent.mkdir(parents=True, exist_ok=True)
        HALT.write_text(
            f"CAPACITY GATE TRIPPED on {target}\n\n"
            "A scheme changes design capacity WITHIN one season and is not in\n"
            "check_capacity.ACKNOWLEDGED_MID_SEASON. Nothing was published.\n")
        die(2, "CAPACITY GATE TRIPPED",
            "A new mid-season capacity change. Excluding a scheme-season from "
            "the\nbaseline is not a decision this script should take, so it "
            "halts.\nSee data/processed/capacity_midseason_exceptions.csv and "
            "brief §14.")

    # ---- 10. staleness ---------------------------------------------------
    newest = newest_loaded_date()
    gap = (now_ist.date() - newest).days if newest else 9999
    log(f"\nnewest loaded      {newest}  ({gap} days behind today IST)")
    if gap > args.max_stale_days:
        die(1, f"STALE — {gap} DAYS BEHIND",
            f"Past the {args.max_stale_days}-day threshold even after a "
            f"successful fetch.\nSomething is skipping days.")

    # ---- 11. the view, in season only ------------------------------------
    if in_season:
        run(["scripts/build_view_data.py"], "build_view_data")
    else:
        log("\nOFF SEASON — view not rebuilt, by design. The season is "
            "1 Jun – 31 Oct;\nadding off-season dates to season facts would "
            "mix water years (brief §14).\nThe site keeps showing the "
            "completed season, with its date on its face.")

    # ---- the dated archive ----------------------------------------------
    # Off season nothing else changes, so without this git would go quiet for
    # seven months. One row a day keeps the provenance versioned year-round.
    DAILY_LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
    new = not DAILY_LOG_CSV.exists()
    # Append-only, but not twice for the same day: a re-run after an
    # interrupted run must not duplicate the row.
    if not new and f"{target}," in DAILY_LOG_CSV.read_text(encoding="utf-8"):
        log(f"already recorded   {target} is in {DAILY_LOG_CSV}, not re-appended")
    else:
        with open(DAILY_LOG_CSV, "a", encoding="utf-8", newline="") as fh:
            if new:
                fh.write("report_date,fetched_utc,bytes,sha256,n_schemes,"
                         "state_pct_filling,worst_residual_ratio,in_season\n")
            fh.write(f"{target},"
                     f"{datetime.now(timezone.utc).isoformat(timespec='seconds')},"
                     f"{nbytes},{sha},{len(det)},{state_pct:.4f},"
                     f"{rec['worst_ratio']:.3e},{int(in_season)}\n")
        log(f"appended to        {DAILY_LOG_CSV}")

    # ---- 12. commit and push --------------------------------------------
    rule("COMMIT")
    git("add", "--", str(VIEW), str(DAILY_LOG_CSV), "data/processed")
    staged = git("diff", "--cached", "--name-only")
    if not staged:
        die(0, "NOTHING CHANGED",
            "The day was fetched and verified but produced no change to any "
            "tracked\nfile. Normal off-season.")
    log("staged:")
    for f in staged.splitlines():
        log("  " + f)

    if args.dry_run:
        git("reset", "-q")
        die(0, "DRY RUN — NOT COMMITTED",
            "Everything up to the commit ran and passed. Staging was reset.")

    subject = f"data: {target} report"
    body = (f"State storage {state_pct:.2f}% of design capacity on {target}.\n\n"
            f"{len(det)} schemes. Reconciled against the report's own grand "
            f"total;\nworst residual {rec['worst_ratio']:.2e} of total. "
            f"SHA-256 {sha}.\n"
            f"{'View rebuilt.' if in_season else 'Off season: archived, view not rebuilt.'}\n\n"
            f"Fetched locally — the source is unreachable from cloud runners "
            f"(brief §17).\n\n"
            f"Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n")
    p = subprocess.run(["git", "commit", "-q", "-m", subject, "-m", body],
                       capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        die(1, "COMMIT FAILED", p.stdout + p.stderr)
    log(f"committed          {git('rev-parse', '--short', 'HEAD')}  {subject}")

    if args.no_push:
        die(0, "COMMITTED, NOT PUSHED", "--no-push was given.")

    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(["git", "push", "origin", "main"], capture_output=True,
                       text=True, errors="replace", env=env)
    if p.returncode != 0:
        die(1, "PUSH FAILED",
            (p.stdout + p.stderr).strip() +
            "\n\nThe commit is made locally and is not lost. If this says the "
            "username\ncould not be read, the cached credential has expired — "
            "run `git push`\nonce by hand to refresh it.")
    log("pushed to origin/main")
    die(0, f"DONE — {target} PUBLISHED",
        f"State {state_pct:.2f}%. GitHub Pages redeploys from the push.")


if __name__ == "__main__":
    main()

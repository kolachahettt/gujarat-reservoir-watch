# Gujarat Reservoir Watch

A daily view of Gujarat's **206 reservoir schemes** that answers one question the source
does not answer for you: **how does today's storage compare with the same calendar date in
previous years?**

The Gujarat Water Resources Department publishes a storage report every day as a PDF. It
tells you where each dam stands today, and — at regional level only — how that compares
with the same day one year ago. It does not give you five years of daily history, a
per-dam comparison, or any way to tell whether a reservoir that is 40% full is recovering
or falling. This project builds that history from the published reports and shows it.

**Latest report: 11 September 2026. Gujarat is 65.29% full against a four-year mean of
82.61% on the same date.** The weakest prior year on this date was 2023, at 73.53% — 2026
is 8.24 points below even that.

> **Nothing here is a forecast.** Every figure is a comparison of observed readings. No
> number on any page states what storage will be on any future date.

---

## What it shows

Open `web/reservoir.html`. Two views; the first is the default.

**Season** — a state headline, then five regional facets ordered worst-deviation-first.
Each facet plots % filling for 2026 heavily against the four prior years thinly, on an
**aligned day-of-season axis** (day 1 = 1 June), with a rainfall strip beneath sharing the
x-axis. Below the facets, two deviation tables that answer deliberately different
questions. A scheme search in the header opens any of the 206.

Each facet carries a **crossover marker**: the date 2026 fell below *every* prior year.
A day only counts if the current season and all four prior years have a value for it, and
the marker is the first day of the first run of **≥5 consecutive** such days. Five days is
the one tunable parameter and the interface states it, because a shorter rule would turn a
one-day dip into a headline. On the current data:

| | Crossed below all four prior years |
|---|---|
| Kutch | 19 July, and has stayed below — 55 days |
| Saurashtra | 20 July, still below — 54 days |
| South Gujarat | 25 July, recovered 2 Aug; 2 days below now, **short of the rule, so no second crossing is claimed** |
| North Gujarat | 29 August, still below — 14 days |
| Central Gujarat | **never** — below the prior mean, but never under all four for five days |
| Gujarat (state) | Below every prior year since 26 August — 17 days. Also fell below briefly on 27 July, recovering 2 August |

**Schemes** — all 206 dams as one point each, sized by design capacity and coloured by
deviation, with a region filter. Not a map; see [Why there is no map](#why-there-is-no-map).

---

## Data source and licence

**Source:** Reservoir Data Management System, Narmada, Water Resources, Water Supply &
Kalpasar Department, Government of Gujarat — <https://wrd-dam.gujarat.gov.in>

The daily report is a **download, not a scrape**. The only URL parameter is the date, as
standard base64 of the plain ISO string, padding included:

```
https://wrd-dam.gujarat.gov.in/downloads/home_pdf.php?dt=MjAyNi0wOS0xMQ==
                                                         └─ base64("2026-09-11")
```

So the whole archive is directly addressable: no crawling, no pagination, no HTML parsing
to discover links. A backfill is a loop over dates.

**Everything shown comes from that one file**, including rainfall — pages 18–23 carry
per-scheme rainfall in the same PDF. (The IMD district feed in `data/processed/` belongs to
the archived predecessor project, not to this one.)

### Licence: not stated, so not asserted

**The portal publishes no copyright policy, terms of use or data licence**, and this
project does not claim one on its behalf. It is a Government of Gujarat departmental
publication. The Government Open Data Licence – India covers data published through
`data.gov.in`; this is a departmental download and I have not verified that GODL applies to
it. **If you intend to redistribute the source PDFs or a bulk dataset derived from them,
check with the department first.**

What this repository holds is the code, plus derived figures carried with their provenance
(report date, byte count and SHA-256 of each file). The PDFs themselves are cached under
`data/raw/dam/` and are not redistributed here.

### On transport security

**TLS certificate verification is deliberately disabled for this host**, which does not
validate in this environment. The mitigation is that the fetcher records the **SHA-256 and
byte count of every file** in the ledger, so content is auditable even though transport is
not. The 11 September 2026 report is 276,237 bytes, SHA-256
`10F4783D…0B2C4C28`. If you can validate the certificate, re-enable verification in
`scripts/backfill.py:ctx_noverify`.

---

## Method

```
backfill.py  →  parse_cached.py  →  build_db.py  →  check_capacity.py
 download        parse (parallel)     DuckDB          THE GATE
                                                          ↓
                              build_view_data.py  ←  report_deviation.py
                                      ↓
                              web/reservoir.html
```

Run it:

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
python scripts/serve.py --root web --port 8020
```

**Download and parse are separate because they have opposite constraints.** The server
generates each PDF on demand and takes about 33 seconds, so downloading must be serial and
polite — a fixed delay between *network* requests, cache hits never sleeping, one request
at a time, an identified User-Agent. Parsing is local and CPU-bound at ~13–17 s per
23-page file, so it runs across worker processes. Both are resumable: a cached PDF is never
re-fetched, a parsed day never re-parsed.

**Coverage is a precondition, not a footnote.** The pipeline refuses to run while any
requested date is missing, because a deviation table built on a quarter of a season looks
exactly like one built on all of it. Partial coverage does not announce itself in the
output, so it is blocked at the door. Current state: **714 of 716 requested dates** (1 June
– 31 October, 2022–2026), plus 18 off-season dates fetched to pin capacity changeovers =
**732 daily reports, 150,792 storage rows, every date carrying exactly 206 schemes.**

The two absences are upstream, not failures: `2025-09-26` and `2026-09-12` return **HTTP
200 with `Content-Type: application/pdf` and `Content-Length: 0`**. That is how this server
says "no report for that date"; it does not use 404. They are counted as absent and named
in the interface, never silently dropped.

### Comparing like with like

Design capacity is **restated between seasons** — 15 schemes, 17 events, 12 down and 5 up,
for a net change of **−8.24 MCM against 54.96 MCM of gross movement**. A net that barely
moves while the gross is large is the signature of a correction exercise, not of physical
change; siltation would be uniformly downward. This silently breaks any historical %
filling comparison made by someone who did not check.

So a prior year enters a scheme's baseline **only if that year's published capacity matches
today's** within 0.5%. Nothing is rebased and no published figure is altered; the cost is a
shorter baseline for restated schemes, and it is shown per row rather than hidden. 190 of
206 schemes have all four prior years; none has zero.

Capacity is established once per scheme-season, with three outcomes:

| | | Count |
|---|---|---|
| `clean` | one value all season | 1021 |
| `edge_exception` | the water-year edit landed on the season's first day or two; the majority value (152 of 153 days) is used and the exception recorded | 6 |
| `mid_season` | a genuine change inside the season — **excluded from the baseline entirely** | 3 |

The three: **Shetrunji 2024** (415.44 MCM for 94 days, then 346.48 from 3 September),
**Jhuj and Kelia 2025** (both changing on 7 August, both in Navsari — a coordinated edit).
Each begins its season at a value matching neither the season before nor after. They cannot
be resolved by redefining a season, and correcting them silently would be worse than losing
a baseline year. A **fourth such case halts the pipeline**, because excluding a
scheme-season is not a decision the code should take on its own.

### Days of water

```
days = present_live_MCM / (outflow_canal_cusecs × 0.00244658)
```

It appears **only on a scheme's detail**, always labelled *at today's release rate*. Where
canal release is zero the figure is undefined, not infinite, and renders **"no current
release"** — on 11 September 2026 that is **179 of 206 schemes**, so it is the normal case,
not an edge case.

It is a reading of one day's release rate and nothing more. Releases are set daily and often
stop. It flags *which reservoirs to look at*; it does not say when any reservoir empties.

---

## The four parsing hazards

Each of these silently corrupts data if unhandled. The first three appeared in the first
file; the fourth only once the backfill reached 2022.

**1. Header rows repeat on every detail page and span two physical rows.** Row 0 holds
column names with merged spans (`Design Storage in MCM` above three sub-columns, emitted as
nulls); row 1 holds `Gross`/`Live`/`Dead`. Both must be skipped on *every* page.

**2. Cell contents wrap mid-value, including numbers.** The dangerous one. `pdfplumber`
returns the wrap as an embedded newline:

| Raw cell | Correct value |
|---|---|
| `"7414.2\n9"` | **7414.29** |
| `"HIGH\nALERT"` | `HIGH ALERT` |
| `"WARNI\nNG"` | `WARNING` |

The rule differs by field type — **text collapses whitespace to one space, numbers strip it
entirely.** Get it wrong and Ukai's 7,414.29 MCM becomes 7,414.2, or 7,414.2 plus a stray 9
that shifts every remaining field on the row.

**And no whitespace rule works for `Type of Warning`**, because the wrap falls between words
in `HIGH\nALERT` and mid-word in `WARNI\nNG`. Collapsing gives correct `HIGH ALERT` and
corrupt `WARNI NG`; stripping gives correct `WARNING` and corrupt `HIGHALERT`. The field is
matched against a **closed vocabulary** after removing all whitespace, and anything
unrecognised is kept verbatim and reported as a failure rather than guessed at. 16 of 206
rows carried `WARNI NG` on the first clean run.

**3. The regional abstract prints every figure twice, in MCM and MCFT, as two physical rows
per region** — and the second row carries no region label. Parse it without pairing rows and
you get duplicate regions with nonsensical magnitudes (MCFT ≈ 35 × MCM).

**4. Schema drift: the 2022 reports have 23 columns, not 24.** `Taluka` did not exist, and
`Region` was spelled out (`South Gujarat`) rather than coded (`SG`). Two changes at once,
neither announced anywhere in the file. Both layouts are accepted and normalised, and
**every row carries `source_layout`** so the difference stays visible in the data rather
than only in documentation. Complete coverage pins the changeover: **23 columns through
2023-06-16, 24 from 2023-06-17.**

**The general lesson: select tables by column count, never by page heading.** Continuation
pages repeat only the column header, and the file holds six tables of differing widths. A
heading-based selector logged ~100 false failures against the 11-column percentage-storage
table before this was fixed.

**A fifth, found later, and this one is ours.** Because text fields only *collapse*
whitespace, a district name wrapped mid-word arrives as `Surendranaga r` — on 97 dates,
23-column layout only. Stripping whitespace from text instead would give `GirSomnath`, so
the fix is a closed vocabulary for district, keyed on the whitespace-free form. Those rows
are otherwise sound: taluka, design and present storage all parse correctly, and **no
storage figure is affected.**

---

## Why there is no map

**The report gives every dam a district and a taluka and never a coordinate.** There is no
latitude or longitude column in any of the six tables in the file.

Two ways to supply them were tried and both rejected:

**OpenStreetMap.** An Overpass query for dams and reservoirs in Gujarat returned 2,210
elements, of which 110 carry both a name and a position. Normalised matching against the
206 schemes resolved **9 — 4.4%.** The failure is structural, not a normalisation bug: WRD
writes `Aji - II`, `Demi - I`, `Bhadar (P)` where OSM has `Aji Dam 3`, `Demi Dam 1`,
`Bhadar`. Cleverer matching might reach 30%, but asserting that `Aji-II` is `Aji Dam 3`
would place dams **confidently in the wrong place**, which is worse than not placing them.

**CWC's National Register of Large Dams.** NRLD does carry latitude and longitude. But both
the 2019 and 2023 editions are published **only as PDFs**, and the coordinate pages have no
ruled lines, lay their headers out character-by-character, and split each coordinate into
fragments across two baselines interleaved with the adjacent column — `extract_tables`
returns one cell per page and `extract_words` returns single characters. Reconstructing it
needs per-character geometric reassembly, where a silent error misplaces a dam. That is the
same failure mode as guessing OSM names, so it was not shipped.

**Centroid placement is worse than no map.** Several dams share a taluka, so taluka-centroid
points would coincide and need jitter — synthetic position. And the district geometry held
here is 2011 vintage, 26 districts: Devbhoomi Dwarka, Morbi, Botad, Arvalli, Gir Somnath,
Mahisagar and Chhotaudepur did not exist yet, so **46 of 206 schemes would sit at a parent
district's centroid** — including Devbhoomi Dwarka, which carries most of this season's
deficit.

So the Schemes view draws the requested encodings — one point per dam, sized by design
capacity, coloured by deviation, nothing filled — against the two axes the data does
support: deviation against design capacity.

**The real map is already written.** `mapSVG()` in `web/reservoir.html` draws state and
district geometry as **strokes only, never fills**, with the same encodings. It switches on
by itself as soon as this file exists:

```
data/reference/dam_coordinates.csv
scheme_id,lat,lon,source
```

The view then reports how many of 206 are placed and does not imply a position for the rest.

---

## Known limitations

**Dam storage is not village water availability.** This is the most important caveat and
the one most likely to be forgotten. The report locates a dam **by the taluka the dam sits
in** — it publishes no command area, so nothing here can say which villages or fields a
reservoir irrigates. A dam at 5% in one taluka does not mean the next village has no water,
and a full dam does not mean the surrounding farms are supplied. Canal networks cross
taluka and district boundaries, and groundwater — which is what most Gujarat irrigation
actually runs on — is not in this data at all. **Any village-level or farm-level claim from
this dataset would be invented.** The interface says so on its face.

**No groundwater.** Deliberately out of scope, and it is the larger part of the picture in
North Gujarat and Saurashtra.

**Regional aggregates use a fixed per-season denominator, not the per-date published one.**
Taking design capacity per date puts steps in the prior-year lines that no rainfall caused:
Saurashtra 2024 jumps 68.97 MCM (2.66% of the region) on 3 September from Shetrunji's
restatement, and Kutch 2024 jumps 7.03 MCM between day 1 and day 2. For the three
scheme-seasons with no single season capacity, the 2026 value is used — the value every
other season of those three agrees on — and the affected facets say so on their face.

**District labels drift, and the scheme list does not.** Across 732 days: **0 schemes added,
0 dropped**, 1 renamed, but **29 schemes carry more than one district label.** Only 3 of
those are genuine reassignments; the rest are abbreviation, spelling and wrapping variants,
concentrated on 2023-06-13, four days before the column-layout change. Use
`dim_scheme.district_latest`, never the per-row `fact_storage.district`, or the same
reservoir appears under two districts in one query.

**`% filling` is recomputed, not trusted.** The source prints its own percentage; this
project recalculates from gross storage and design capacity and reconciles the total against
the report's own abstract. On 11 September 2026 the residual was −0.05 MCM out of 15,797.56
(4.85 × 10⁻⁶), which is the abstract rounding 206 two-decimal values, not a parse error.

**Days of water is a snapshot, stated above** — not a drawdown estimate.

**The colour palette was not machine-validated.** It uses only values documented as passing
in the reference palette it was built against (2026 orange; prior years the blue sequential
ramp, light→dark, because year is ordered data). The validator needs Node, which is not
installed here, so documented-passing values were taken rather than new steps eyeballed.

**One season is short.** Four prior years is enough for a same-date mean and not enough for
a distribution. The interface shows the individual years rather than a band for that reason.

---

## Repository layout

```
README.md               this file
DATA.md                 how to regenerate everything not in git
PROJECT_BRIEF.md        the long-form working record
.gitignore              raw PDFs and multi-GB source data stay out
.github/workflows/       Pages deploy
scripts/                the pipeline
data/reference/         small committed lookups
data/processed/         derived CSV/JSON — the audit trail (binaries ignored)
web/                    THE DEPLOYABLE SITE — 4 files, 628 KB
archive/                the predecessor project, kept separate
```

| Path | What |
|---|---|
| `scripts/fetch_dam.py` | Ingest and the shared parsing library — layouts, vocabularies, hazards |
| `scripts/backfill.py` | Polite serial download, resumable, with a ledger |
| `scripts/parse_cached.py` | Parallel local parse of cached PDFs |
| `scripts/build_db.py` | DuckDB star schema — `dim_scheme`, `fact_storage`, `fact_rainfall`, `ledger` |
| `scripts/check_capacity.py` | Season capacity, the restatement table, **and the gate** |
| `scripts/report_deviation.py` | Coverage, scheme drift, both deviation tables |
| `scripts/pin_changeover.py` | Bisects the off-season to date a capacity changeover |
| `scripts/run_pipeline.py` | The six steps in order, halting on the gate |
| `scripts/build_view_data.py` | All view computation → `web/data/reservoir_view.json` |
| `scripts/serve.py` | Static server with Range support, for local viewing |
| `web/index.html` | The interface. No analysis in the page |
| `web/data/reservoir_view.json` | Everything the page reads |
| `archive/` | Predecessor basis-risk project and an abandoned mandi verification — see `archive/README.md` |

`PROJECT_BRIEF.md` is the long form. It records the findings, the rejected options and the
mistakes — including two occasions where an analysis ran on partial data, one where a
caching bug was reported as a server fault before being traced home, and one where a
district-label defect was called cosmetic before it turned out to be visible in published
output.

---

## Deploying

The site is **four static files, 628 KB** — one HTML document, one JSON, and two GeoJSON
boundary files that only load once coordinates exist. No backend, no build step, no runtime
dependency on the source. Any static host works.

Run it locally first:

```bash
python scripts/serve.py --root web --port 8021
```

### GitHub Pages

`.github/workflows/pages.yml` publishes `web/` on every push that touches it. Pages can
only serve a repository root or `/docs` directly, so the workflow uploads `web/` as the
artefact instead — that keeps the deployable folder's name and puts the site at the URL
root rather than under `/web/`. It runs no build: it verifies `web/index.html` and
`web/data/reservoir_view.json` exist and that the JSON still holds 206 schemes and 5
regions, then uploads.

```bash
git init -b main && git add -A && git commit -m "Gujarat Reservoir Watch"
```

```bash
git remote add origin git@github.com:<you>/gujarat-reservoir-watch.git && git push -u origin main
```

Then set **Settings → Pages → Source → GitHub Actions**. The URL will be
`https://<you>.github.io/gujarat-reservoir-watch/`.

### Cloudflare Pages

Connect the repository and set **build command: none**, **output directory: `web`**. Or
deploy the folder directly without a repository:

```bash
npx wrangler pages deploy web --project-name gujarat-reservoir-watch
```

### Updating the published figures

The JSON is committed, so the deployed page is a snapshot of whenever it was last built —
it does not fetch from the source at load time and will not update itself. To refresh:

```bash
python scripts/backfill.py --download-only --months 6-10 --delay 1.0
```

```bash
python scripts/run_pipeline.py --date <new-date> --skip-pin && python scripts/build_view_data.py
```

Then commit `web/data/reservoir_view.json`. **Do not add a deploy-time build step that
fetches from the source**: each PDF takes ~33 seconds to generate server-side, a full
refresh is hours, and pointing CI at a government portal on every push is neither polite nor
reliable. The page states its own report date and build time on its face, so a stale
snapshot is visible rather than silent.

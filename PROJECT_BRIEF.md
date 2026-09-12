# Project brief — Gujarat Reservoir Watch

Written 11 September 2026. Supersedes the basis-risk project, which is finished and
preserved separately in `ARCHIVE_basis_risk_BRIEF.md`. Nothing in `data/` or `scripts/`
from that work has been deleted.

---

## 1. What this is

A daily view of Gujarat's 206 reservoir schemes that answers two questions a state
irrigation officer and a farmer both ask in September:

1. **How does today's storage compare with the same date in previous years?**
2. **At the current canal release, how many days of water are left?**

Daily storage from the Gujarat Water Resources Department, compared against the same
calendar date in prior years, with rainfall from IMD alongside it. Storage without rainfall
is half the picture: a dam at 40% after a wet week is a different object from a dam at 40%
after a dry month.

---

## 2. Verified data source

**Portal:** `https://wrd-dam.gujarat.gov.in/index.php`
Reservoir Data Management System, Narmada, Water Resources, Water Supply & Kalpasar
Department, Government of Gujarat.

### The URL pattern — a download, not a scrape

```
https://wrd-dam.gujarat.gov.in/downloads/home_pdf.php?dt=<TOKEN>
```

`<TOKEN>` is **standard base64 of the plain ISO date string `YYYY-MM-DD`**, including the
`=` padding. Nothing else is encoded — no session, no cookie, no listing page.

```python
token = base64.b64encode("2026-09-11".encode()).decode()   # 'MjAyNi0wOS0xMQ=='
```

**This is the single most important fact in the brief.** Because the date is the only
parameter and it is trivially constructible, the entire archive is directly addressable:
no crawling, no pagination, no HTML parsing to discover links. A backfill is a `for` loop
over dates.

**Verified end to end, 11 Sep 2026:**

| Date | Token | Response |
|---|---|---|
| 2026-09-11 | `MjAyNi0wOS0xMQ==` | 276,237 bytes, `%PDF-` |
| 2026-09-10 | `MjAyNi0wOS0xMA==` | 275,966 bytes, `%PDF-` |
| 2025-06-15 | `MjAyNS0wNi0xNQ==` | 268,802 bytes, `%PDF-` |
| 2022-06-10 | `MjAyMi0wNi0xMA==` | 258,782 bytes, `%PDF-` |

**Coverage:** daily, latest = today. The portal's own date list reaches back to about
**June 2022**, giving roughly 1,550 daily files — enough for four same-date comparisons
per calendar day. TLS on this host does not validate in our environment; the fetcher
disables verification deliberately and records the SHA-256 of every file instead, so
content is auditable even though transport is not.

**Format: PDF only.** No CSV, Excel or API. `pdfplumber` handles it.

---

## 3. Schema — 206 schemes

The PDF is 23 pages holding **six different ruled tables of different widths**. Mapped
completely on 11 Sep 2026 — this matters because selecting the right one by page heading is
fragile (continuation pages repeat only the column header), so the ingest selects on
**column count**:

| Pages | Table | Cols | Status |
|---|---|---|---|
| 1 | ABSTRACT — region-wise storage position | 19 | Parsed, used to reconcile |
| 2 | **Districtwise Storage** | 8 | Not parsed yet — see note below |
| 3 | Gross Storage of major schemes (levels in ft) | 14 | Not parsed |
| **4–11** | **Statement showing the details of dams in Gujarat** | **24** | **The 206-scheme table** |
| 12–17 | Statement showing percentage storage (DLS, gate type) | 11 | Not parsed |
| 18–23 | **Statement showing details of rainfall** — per scheme, with range bands | 12 | Not parsed yet |
| 17 | small trailing table | 3 | Not parsed |

Two of the unparsed tables are worth knowing about rather than discovering later:

- **Page 2 gives district-wise storage directly.** The §5 command-area caveat still applies
  in full — it aggregates by the district the *dam sits in* — but if a district view is ever
  wanted, the source computes one and we should use theirs rather than rolling our own.
- **Pages 18–23 give per-scheme rainfall with range bands**, a second rainfall signal inside
  the same file, at scheme rather than district resolution. This complements the IMD feed
  (§6) and does not replace it.

### Regional abstract (page 1)

Regions: **North Gujarat (15 schemes), Central Gujarat (17), South Gujarat (13),
Kutch (20), Saurashtra (141)** — 206 total, with a sub-total for the first three and a
grand total. Columns: no. of schemes, dams completely filled, design gross storage,
today's gross storage, percentage filling, previous day gross storage and difference, same
day previous year gross storage and difference, gross storage as on 31 May, and the
increase/decrease against 31 May.

The same-day-previous-year column means **the comparison the product needs is already in
the source at regional level** — our job is to do it per dam, and to extend it beyond one
year back.

### Dam detail table — all 24 columns

| # | Column | Type | Notes |
|---|---|---|---|
| 1 | `Sr No.` | int | 1–206, resets per page in the PDF but is continuous |
| 2 | `Scheme Id` | int | The stable dam key. Use this, not the name |
| 3 | `Location Id` | int | Tracks `Sr No.` in the day sampled; do not assume |
| 4 | `District` | text | Present-day district (33-district Gujarat) |
| 5 | `Taluka` | text | Taluka of the **dam itself** — see §5 |
| 6 | `Name of Schemes` | text | Dam name |
| 7 | `Region` | code | `SG`, `NG`, `CG`, `Sau`, `Kut` |
| 8 | `Type` | code | `G` gated, `UG` ungated, `FG` — confirm the legend |
| 9 | `OSL (m)` | float | Observed/outlet sill level |
| 10 | `FRL (m)` | float | Full reservoir level |
| 11 | `PWL (m)` | float | Present water level |
| 12 | `Design Storage Gross` | float MCM | |
| 13 | `Design Storage Live` | float MCM | |
| 14 | `Design Storage Dead` | float MCM | |
| 15 | `Present Storage Gross` | float MCM | |
| 16 | `Present Storage Live` | float MCM | **The numerator for days-of-water** |
| 17 | `Present Storage Dead` | float MCM | Not extractable water |
| 18 | `Percentage Filling %` | float | Gross basis; recompute rather than trust |
| 19 | `RF` | float | Rainfall, current period |
| 20 | `CRF` | float | **Cumulative rainfall** for the dam's catchment |
| 21 | `Type of Warning` | text | `NIL`, `ALERT`, `HIGH ALERT`, `WARNING` |
| 22 | `Inflow in Cusecs` | float | |
| 23 | `Outflow River in Cusecs` | float | |
| 24 | `Outflow Canal in Cusecs` | float | **The denominator for days-of-water** |

**`RF`/`CRF` are a bonus worth noting:** the dam report already carries catchment rainfall,
so a first-order rainfall signal arrives in the same file as storage. IMD is still used
(§6) because it is district-wide, dated and independently sourced, but the two can be
cross-checked against each other.

### Days of water remaining

```
days = present_live_MCM / (outflow_canal_cusecs × 0.00244658)
```
1 cusec ≈ 0.0283168 m³/s ≈ 2,446.58 m³/day ≈ 0.00244658 MCM/day.

**Where canal release is zero the figure is undefined, not infinite.** Render it as "no
current release", never as a large number. On the day sampled a majority of schemes were
at zero canal release, so this is the common case, not an edge case.

---

## 4. The four parsing hazards

The first three were hit on the first file; the fourth only appeared once the backfill
reached 2022. Each silently corrupts data if unhandled.

**1. Header rows repeat on every detail page, and span two physical rows.**
Row 0 holds the column names with merged spans (`Design Storage in MCM` above three
sub-columns, emitted as `null`s); row 1 holds `Gross`/`Live`/`Dead`. Both must be skipped
on **every** detail page, not just the first.

**2. Cell contents wrap mid-value, including numbers.** This is the dangerous one.
`pdfplumber` returns the wrap as an embedded newline inside a single cell:

| Raw cell | Correct value |
|---|---|
| `"HIGH\nALERT"` | `HIGH ALERT` |
| `"WARNI\nNG"` | `WARNING` |
| `"7414.2\n9"` | **7414.29** |
| `"6729.9\n0"` | **6729.90** |
| `"5727.8\n1"` | **5727.81** |
| `"Devbhumi\nDwarka"` | `Devbhumi Dwarka` |

The rule differs by field type: **text fields collapse whitespace to a single space;
numeric fields strip whitespace entirely.** Treating a wrapped number as text, or
splitting on the newline, turns Ukai's 7,414.29 MCM into 7,414.2 — or into 7,414.2 and a
stray 9 in the next column, shifting every remaining field on the row. A naive text-regex
parser fails here, which is why the ingest uses ruled-table extraction, not line regex.

**And neither rule works for `Type of Warning`.** The wrap there falls *between* words in
`"HIGH\nALERT"` but *mid-word* in `"WARNI\nNG"`. Collapsing to a space gives the correct
`HIGH ALERT` and the corrupt `WARNI NG`; stripping whitespace gives the correct `WARNING`
and the corrupt `HIGHALERT`. There is no whitespace rule that is right for both. The field
is therefore matched against a **closed vocabulary** — `NIL`, `ALERT`, `HIGH ALERT`,
`WARNING`, `DANGER` — after removing all whitespace, and anything unrecognised is kept
verbatim and reported as a parse failure rather than guessed at. This bit on the first
clean run: 16 of 206 rows carried `WARNI NG` before the vocabulary was added, and it would
have silently split one warning level into two categories forever.

**3. The regional abstract prints every figure twice, in MCM and MCFT, as two physical
rows per region.** The second row carries no region label. Parsing the abstract without
pairing rows yields duplicate regions with nonsensical magnitudes (MCFT ≈ 35× MCM).

**4. SCHEMA DRIFT — the 2022 reports have 23 columns, not 24.** `Taluka` did not exist in
the detail table, and `Region` was spelled out in full (`South Gujarat`) rather than coded
(`SG`). Two changes in one, and neither is announced anywhere in the file.

This is the hazard that fails *loudly if you are lucky and silently if you are not*. The
first parse of 2022-09-11 returned **zero detail rows** and was correctly recorded as
`malformed` — because the column-count selector found no 24-column table. Had the parser
instead matched on page heading and read positionally, every field from `Taluka` onward
would have shifted one column left, producing 206 plausible rows of wrong data.

Both layouts are accepted and normalised to the 24-column shape: `taluka` is `None` where
the source never had it, `region` is canonicalised to the coded form, and **every row
carries `source_layout`** so the difference stays visible in the data rather than only in
this document.

**Pinned for free by complete coverage:** 23 columns on 174 dates, 2022-06-01 to
**2023-06-16**; 24 columns on 558 dates, **2023-06-17** to 2026-09-11. No bisection was
needed — unlike the capacity changeover, this one happens *inside* the Jun–Oct range, so
the season data contains the evidence. Note how close it sits to the 2023-06-13 district
label tidy-up (§13): the report was reissued with a new template and cleaned labels in the
same week.

**The general lesson:** select tables by **column count**, never by page heading.
Continuation pages repeat only the column header, and the report holds six tables of
differing widths (§3). A heading-based selector logged ~100 false failures from the
11-column percentage-storage table before this was fixed.

---

## 5. The taluka limitation on command areas — state this plainly

**The `Taluka` column is where the dam sits, not where its water goes.**

The report gives each scheme's own location. It does **not** give the command area — the
set of talukas and villages the canal network irrigates. Those are different geographies
and frequently far apart: a dam in a hilly upstream taluka commands lowland talukas tens
of kilometres away.

Consequences, to be honoured in the UI and not quietly forgotten:

- **Days-of-water is a property of the reservoir, not of a place.** It can be reported per
  dam. It cannot be attributed to a farmer's village without command-area boundaries we
  do not have.
- **Do not aggregate storage to taluka or district and present it as local water
  availability.** A district containing a large dam is not thereby well supplied.
- **Do not join this to village-level data** — including the archived basis-risk index —
  and imply a supply relationship. The join would run on dam location, which is the wrong
  geography, and would be wrong in a way that looks plausible.

If command areas are wanted later, they need a separate source (CCA statements by scheme,
or canal network geometry). Until then the unit of analysis is the **scheme**, and the map,
if one is built, shows **dams as points**, not filled administrative areas.

---

## 6. Reuse from the archive

`scripts/fetch_imd.py` is carried over unchanged and works: it parses IMD's district
rainfall departure from a stable URL
(`mausam.imd.gov.in/Rainfall/DISTRICT_RAINFALL_DISTRIBUTION_COUNTRY_INDIA_cd.pdf`),
records fetch timestamp, the PDF's own DAY and PERIOD, and a SHA-256, and on 11 Sep 2026
parsed all 33 Gujarat districts with zero missing departures. Rainfall categories are
IMD's own (`N`, `D`, `LD`, `E`, `LE`, `NR`, `ND`).

Also reusable if needed: `data/reference/gujarat_district_crosswalk_2011_2026.csv`, the
hand-checked 33→26 district crosswalk, if dam districts ever need reconciling to census
geography. Note the dam report uses **present-day** district names, so it aligns with the
IMD feed directly and needs no crosswalk for the rainfall join.

---

## 7. Groundwater — explicitly not a dependency

`indiawris.gov.in` is unreachable from the build machine (TCP 443 fails; `cgwb.gov.in`
answers normally, so it is host-specific and probably geo-restricted). CGWB well data is
station-level but only **quarterly** (January, March–May, August, November), and the Atal
Bhujal dataset on data.gov.in is biannual and **stops in 2022**.

**The project does not depend on any of it.** If India-WRIS loads from a different
connection, groundwater becomes an optional additional layer. It is not on the critical
path and no design decision should assume it.

---

## 8. Honesty rules, carried forward

These earned their place on the previous project and are not renegotiable.

1. **No synthetic data anywhere** — not for demos, not for placeholders.
2. **Every number on screen traces to a named source**, with its date.
3. **Provenance is captured at fetch time**: URL, date token, fetch timestamp, byte count,
   SHA-256. A storage figure without a date is useless.
4. **Report row counts and parse failures at every step.** 206 schemes are expected; if
   204 parse, the two missing are named, not rounded away.
5. **Undefined is not zero and not infinity.** Zero canal release means days-of-water is
   undefined. A missing dam means missing, not 0 MCM.
6. **Validate against the source's own totals.** The abstract page states the regional and
   state totals; the sum of parsed dam rows must reconcile with them, and the residual
   must be reported.

---

## 9. The default view — specification

**Audience: state and regional.** An irrigation officer, a journalist, a researcher. Scheme
search sits in the header as a destination, **not** as a layout driver.

**The farmer-at-one-dam case is explicitly not served.** Without command areas (§5) we
cannot tell a farmer whether a given reservoir supplies their land, and a scheme-level view
dressed up as a local one would imply exactly the relationship the data cannot support.
Say so in the interface rather than half-serving it.

**The question the default view answers:** is this season unusual, where, and when did it
start diverging? Today's storage is in the source PDF and needs no product. The daily
five-season history is the asset, so the default view has to use it.

### Layout — one view, three stacked elements, readable with no interaction

**1. Seasonal trajectory small multiples (the main element).** Five facets, one per region:
South Gujarat, Central Gujarat, North Gujarat, Saurashtra, Kutch. X = day of season
(1 June – 31 October), Y = **region % filling**, shared across all five so regions are
comparable. MCM goes in the tooltip. Scheme count on each facet (Saurashtra is 141 of 206;
Kutch is 20).

**2026 drawn heavy; 2022, 2023, 2024 and 2025 as four thin, de-emphasised, labelled lines.**
Not a shaded band: four observations do not make a distribution, and a band would both
imply one and hide that **2023 was itself weak (73.53% state)**. 2026 being below even 2023
is the headline, and it only reads if all four prior years are visible.

**Five facets, not one state line.** The state average demonstrably hides the story. Today:
South Gujarat 77.76%, Central 62.31%, Saurashtra 49.02%, North 42.32%, **Kutch 22.88%**.
The rainfall work showed the same shape — state −20%, mainland −6%, Saurashtra & Kutch −38%.
A single state line would be the least informative chart this data can produce.

**Rainfall as a thin sparkline strip beneath each facet, sharing the x-axis** — cumulative
CRF for the region's schemes. Storage without rainfall is half the picture (§1), and the
report carries per-scheme CRF for free. **Height is the enemy of a default view:** if the
strip makes the view awkward, move rainfall to a second view rather than growing this one.

**2. A one-line state headline above.** "65.29% of design capacity, against 82.61% mean on
this date 2022–2025", with report date and fetch date beside it, always visible.

**3. Both deviation lists beneath, side by side, each headed by the question it answers.**
Top 10 by percentage-point deviation and top 10 by volume shortfall. They disagree by
design: pp surfaces small emptied reservoirs (Vartu-I, Sonmati), volume surfaces Ukai
−580.9 MCM and Dharoi −299.7. Either alone misleads. Capacity-restated schemes carry their
flag and their shortened baseline (`yrs`) here.

### The crossover marker

Each facet marks **the date 2026 fell below every prior year** — "Kutch fell below every
prior year on 8 July" is a statement only a daily history supports, and it is invisible
without the marker.

It needs a definition, because a naive one turns a one-day data blip into a headline:

- Compared on **aligned day-of-season** (month-day), never absolute date.
- A day is **evaluable** only if all four prior years have a value for that day-of-season.
  Days where a prior year is missing are skipped, not treated as passing.
- The condition is `pct_2026 < min(pct_2022..2025)` on that day.
- **The marker is the first day of the first run of ≥5 consecutive evaluable days** on which
  the condition holds. Five days is about a working week: long enough to reject a single-day
  dip or a bad parse, short enough not to miss a real step change. **This is the one tunable
  parameter and it should be stated in the interface, not hidden.**
- Three cases must be rendered distinctly rather than collapsed into a date:
  - **"Below from the start of the season"** — the condition held from the first evaluable
    day. There was no crossing, and claiming one would be wrong.
  - **"Not below all prior years this season"** — no qualifying run. No marker.
  - **"Crossed below on X, recovered on Y"** — the run ended. Both dates shown.
- 2026 data runs only to the latest report date, so a crossover can only ever be found
  inside 1 June to that date. State the window.

### Non-negotiables carried into the build

- **No forecast language anywhere.** The four prior-year lines are history, not an interval.
  Nothing extrapolates, and the crossover marker is an observation about the past.
- **Days-of-water appears only on the scheme detail view**, labelled "at today's release
  rate", rendering **"no current release"** where release is zero — 179 of 206 schemes on
  11 Sep 2026, so that is the common case and must look deliberate rather than broken.
- **Dams as points on any map view, never filled administrative areas** (§5). No default
  choropleth: it would invite the supply inference the data cannot support.
- Provenance always visible: report date, fetch date, source.

**Dependency:** this view needs all five seasons at daily resolution. It is buildable when
the Jun–Oct backfill lands, not before.

---

## 10. Scope and state of play

**Done.** Ingest (`fetch_dam.py`) — one clean day, 206 of 206 schemes, reconciled to the
report's own grand total. Rainfall pages 18–23 parsed. DuckDB model: `dim_scheme`,
`fact_storage`, `fact_rainfall`, `ledger`, `v_coverage`. Both deviation lists. The capacity
gate, the restatement table and the pinned changeover dates (§11).

**Running.** The Jun–Oct backfill, 2022–2026: **715 days at ~33 s per request**, because the
server generates each PDF on demand. Download is serial and polite (`backfill.py
--download-only`); parsing is parallel and local (`parse_cached.py --workers 6`), since
table extraction costs ~13–17 s per file. `wait_then_run.py` chains the pipeline to the
download's completion.

**Backfill complete.** 714 of 716 requested Jun–Oct dates for 2022–2026, plus the 18
off-season bisection probes = 732 days loaded, 150,792 storage rows. The two absences are
upstream, not failures (§13). Every loaded date carries exactly 206 schemes.

**Drift on full coverage — the data side is closed.** 206 schemes throughout: **0 added, 0
dropped**, 1 renamed (Shedhabhadthari, id 117), **29** schemes with more than one district
label, **22** with more than one design capacity. Against the provisional 75-date result
(1 renamed, 28 districts, 20 capacities) the direction was as expected — more evidence
surfaced more variation — but the magnitude was not: the scheme list itself is completely
stable, and `scheme_id` is a reliable key across five seasons. The variation is all in
*labels and capacities*, never in membership.

**What the 29 district changes actually are.** I first wrote that they were the 2013 district
reorganisation propagating through the report. That was an inference from the district names,
and checking it showed it is wrong — only three of the 29 are reassignments at all. The
decomposition:

| Kind | Schemes | What it is |
|---|---|---|
| Abbreviation expanded | 12 | `D.B.Dwarka` → `Devbhumi Dwarka`, all on **2023-06-13** |
| Spelling normalised | 6 | `Aravalli` → `Arvalli` (5), `Mehsana` → `Mahesana` (1), same date |
| **Wrapped cell — our bug** | 3 | `Surendranaga r` — 99 rows, **layout-23 only** |
| Transient truncation | 5 | bare `Devbhumi` for 1–10 days; taluka and all numerics intact |
| Season-scoped alternate label | 4 | Und-I `Jamnagar`↔`Rajkot`; Sukhbhadar `Botad`↔`Surendranagar`; Hathmati `Aravalli`→`Sabarkantha`→`Arvalli`; Sukhi/Rami `Chhota udepur` |
| **Genuine reassignment** | 3 | Panam `Panchmahal`→`Mahisagar`; Hadaf `Panchmahal`→`Dahod`; Hiran-I `Gir Somnath`→`Junagadh` |

Two things follow.

**`Surendranaga r` is hazard 5, and it is ours.** `clean()` strips *all* whitespace from
numeric fields — that is what turns `7414.2\n9` into `7414.29` (§4) — but only *collapses*
whitespace in text fields. A district name broken mid-word across two lines in the PDF cell
therefore becomes `Surendranaga` + `r` → `Surendranaga r`. It appears on 97 dates across
Dholidhaja, Lim-Bhogavo-I and Lim-Bhogavo-II, and **only in the 23-column layout**, so it is
confined to 2022 – mid-2023. The rows are otherwise sound: taluka, design and present storage
all parse correctly, so nothing is shifted and no storage figure is affected. The fix is not
to strip whitespace from text — that would produce `GirSomnath` — but a **closed vocabulary
for district**, exactly as `WARNING_VOCAB` handles `WARNI NG` and `REGION_CANON` handles
`SOUTH GUJARAT`: match on the whitespace-stripped, case-folded form against the known Gujarat
districts, with aliases for `D.B.Dwarka`, bare `Devbhumi`, `Mehsana` and `Aravalli`. Not yet
applied — it requires a full re-parse, and it changes no storage number.

**The label churn is concentrated on 2023-06-13**, four days before the 23→24 column layout
change on 2023-06-17. So the report was reissued with a new template *and* tidied district
labels in the same week. This is the same administrative-refresh pattern as §11, on a
different field.

**Rule:** a district rollup must use `dim_scheme.district_latest`, never the per-row
`fact_storage.district`, or the same reservoir will appear under two districts in one query.

**Default view built** — see §15. `scripts/build_view_data.py` →
`web/data/reservoir_view.json` → `web/reservoir.html`.

**Next, in order.**
1. Map as a **second** view, dams as points only.
2. Hazard 5, the district vocabulary (§13) — deferred: it moves no storage number
   and needs a full re-parse.

**Not in scope, deliberately:** groundwater (§7), command areas and therefore any
village-level or farmer-facing claim (§5), and forecasting of any kind (§8).

---

## 11. Capacity is restated at the turn of the water year — an administrative process

**Every changeover pinned so far lands on 31 May or 1–2 June.** Design capacities are
refreshed at the turn of the water year. This is an administrative process on a calendar,
not noise, and it has a structural consequence: **a Jun–Oct download range cannot observe
it**, because the edit happens on the boundary of, or immediately before, the range.

Pinned by bisecting the off-season gap (`scripts/pin_changeover.py`, ~9 probes per
boundary, about five minutes each at the server's ~33 s per request):

| Boundary | Changeover | Schemes |
|---|---|---|
| 2022 → 2023 | **2023-06-01** | Dantiwada |
| 2023 → 2024 | **2024-06-01** | 9 schemes |
| 2023 → 2024 | **2024-06-02** | 4 schemes |
| 2024 → 2025 | **2025-05-31** | Dantiwada |

**The 2023→2024 revision is a two-day edit, not one event.** Day-by-day from the cached
reports, `design_gross_mcm`:

| Date | 25 Edalwada | 8 Chopadvav | 18 Patadungari | 43 Jhuj | 44 Kelia | 59 Fatehgadh | 55 Suvi | 48 Rudramata |
|---|---|---|---|---|---|---|---|---|
| 2024-05-31 | 6.42 | 12.05 | 33.03 | 28.65 | 19.98 | 7.44 | 10.46 | 61.53 |
| **2024-06-01** | **14.08** | **7.18** | **41.05** | **24.84** | **17.57** | 7.44 | 10.46 | 61.53 |
| **2024-06-02** | 14.08 | 7.18 | 41.05 | **24.45** | **17.46** | **4.04** | **8.85** | **59.92** |

Nine schemes change on 1 June, four on 2 June — and **Jhuj and Kelia change on both,
passing through an intermediate value on 1 June before settling on 2 June**. So the right
description is one editing episode spanning two consecutive days, not "at least two
revisions". Anyone comparing a 1 June report against a 2 June report for those two schemes
is comparing against a half-applied capacity table.

**Method note that earned its place:** the bisection confirms whether *every* scheme in a
boundary flips on the pinned date, and names those that do not. Without that check the
2023→2024 boundary would have been reported as a single date, confidently and wrongly. It
also refuses to continue when it meets an unexpected third value, which is how Jhuj's
intermediate 24.84 was found rather than smoothed over.

**Correction, recorded so it is not repeated:** the first run reported 2024-05-31 and
2024-06-01 as "unreadable days" and raised the possibility that the fetcher's retry policy
was too weak and the backfill had the same gap. **That was wrong — it was a caching bug in
`pin_changeover.py`**, which keyed the cache on date while storing only the subset of
schemes the first caller asked for. Both dates fetch and parse cleanly to 206 schemes. The
cache now holds the whole day, and the code distinguishes "day unreadable" from "scheme
absent from a readable day", which are different findings. **The backfill needs no retry
change on this evidence:** 119 of 119 download-log rows are `downloaded`, with zero
failures, and the parse ledger is 54 `ok` plus 16 `ok_with_warnings` with no `malformed`.

---

## 12. First clean day — 11 September 2026

`scripts/fetch_dam.py`. Outputs `data/processed/dam_storage_2026-09-11.csv` and
`dam_provenance_2026-09-11.json`.

**Provenance:** token `MjAyNi0wOS0xMQ==`, 276,237 bytes, SHA-256
`10F4783D812B83530D6B108AC131E4B03FDA075B48778183369BF4B60B2C4C28`, 23 pages, internal
header line `Date :- 11-09-2026`.

**Parse result: 206 of 206 schemes, zero nulls in any source column, zero parse failures.**
206 distinct `scheme_id`, 206 distinct names, 28 districts, 102 talukas, regions
`CG / Kutch / NG / SG / Sau`.

**Reconciliation against the report's own grand total (§8.6):**

| | Abstract | Parsed | Residual |
|---|---|---|---|
| Design gross | 15,797.56 MCM | 15,797.56 | **0.00** |
| Today gross | 10,313.86 MCM | 10,313.81 | −0.05 (4.85 × 10⁻⁶ — rounding) |
| Scheme count | 206 | 206 | 0 |

The −0.05 MCM is the source abstract rounding 206 two-decimal values, not a parse error.
The ingest classifies any residual above 10⁻⁵ of total as material and says so.

**State position:** 10,313.81 MCM gross against 15,797.56 design = **65.29% filled**; live
storage 9,150.95 MCM. Warnings: NIL 147, HIGH ALERT 34, WARNING 16, ALERT 9 (= 206).

**Days-of-water is computable for only 27 of 206 schemes** because 179 were at zero canal
release. This confirms §3: zero release is the normal case in mid-September, not an edge
case, and the metric must render as "no current release" rather than a large number. Of the
27, median 85.5 days; the tightest were Dholidhaja (Surendranagar) at **2.8 days** on
1,995 cusecs, Brahmani-II (Morbi) 5.7, Uben (Junagadh) 8.0.

**Caveat on the tight ones:** a low days-of-water figure is a snapshot of an instantaneous
release rate, not a drawdown forecast. Releases are changed daily and often stop. It flags
*which reservoirs to look at*, and must not be presented as "this dam empties in 2.8 days".

---

## 13. The server refuses in long runs — the backfill needs passes, not attempts

**Finding, 11 September 2026.** The first Jun–Oct download pass lost **133 of 715 dates** to
transport-level failures (`URLError` — connection, not HTTP status). They were not scattered.
They came in **eight contiguous runs, the longest 50 days**:

| Run | Days |
|---|---|
| 2023-10-19 .. 2023-10-31 | 13 |
| 2024-06-03 .. 2024-06-04 | 2 |
| 2024-06-06 .. 2024-06-10 | 5 |
| 2024-06-13 .. 2024-07-06 | 24 |
| 2024-08-06 .. 2024-09-10 | 36 |
| 2024-09-12 .. 2024-10-31 | 50 |
| 2025-06-01 | 1 |
| 2025-06-03 .. 2025-06-04 | 2 |

A failing request returns in about a second; a successful one takes ~33 s. So failure runs
are *fast*, and the pass sailed through them.

**This is an availability pattern, not a per-date property.** Dates inside a failure run are
fetched fine later — 2024-06-01, 06-02, 06-05, 06-11 and 06-12 all succeeded as bisection
probes while their neighbours were failing. Whatever the cause (sustained-request throttling
or server-side outage), **the date is not missing upstream, so it must be retried, not
recorded as absent.**

**The old retry policy could not possibly have worked:** three attempts at 3 / 4 / 5 s, a
12-second window, against outages lasting tens of minutes. Fixed in `backfill.py`:

* four attempts with jittered exponential backoff (5 / 20 / 60 s);
* **a cooldown at the run level** — after 6 consecutive failures the pass pauses 5 minutes,
  because continuing to hit a server that is refusing is neither polite nor productive;
* the pass reports failures and what is still missing, and writes
  `data/interim/download_DONE.json` on a clean end of pass;
* re-running the same command retries only what is missing (cached dates cost a `stat`), so
  **completeness comes from repeated passes**, not from more attempts per date.

### The consequence that actually did damage: an analysis on a quarter of a season

`wait_then_run.py` watched the count of cached PDFs and treated 12 minutes of no growth as
"the download has finished". **A failed fetch writes no PDF**, so a failure run freezes that
count while the downloader is working normally. It read the 36-day run of August 2024 as
completion and ran the whole pipeline on **352 of 715 dates**, with the 2024 season at
**36 of 153** and 2025 at **4 of 153**.

Three changes, and the third is the one that matters:

1. Progress is the freshest of (download-log mtime, newest-PDF mtime); the log is flushed on
   every failure, so it moves even when nothing downloads.
2. The stall threshold is 45 min and is only a crash fallback; completion is the sentinel
   file. On a stall the watcher **reports and exits without touching the network** — it
   cannot tell a crashed downloader from a slow one, and starting a second downloader
   against a server that may already be refusing us is the wrong move.
3. **Coverage completeness is a precondition for the pipeline.** Nothing is parsed or
   rebuilt while any requested date is still missing. This is refused rather than
   footnoted, because *a deviation table built on a quarter of a season looks exactly like
   one built on all of it.* Partial coverage does not announce itself in the output.

**Rule carried forward:** every figure published from this dataset must be accompanied by
the date count it rests on. "Full season coverage" is a claim to be checked, not assumed.

### Two further failure modes found while completing the backfill

**1. A successful response carrying nothing.** `2025-09-26` and `2026-09-12` return
**HTTP 200, `Content-Type: application/pdf`, `Content-Length: 0`** — verified across six
attempts including two deliberate probes 25 s apart. That is how this server says "no report
for that date"; it does not use 404. The original code returned immediately on any non-PDF
body with no retry, and the date then sat in the "missing" set forever — the completeness
gate would have refused the pipeline permanently over a date that does not exist. Now:

* an empty 200 goes through the **full retry schedule** first, because one observation is not
  enough to call a date permanently absent;
* only if *every* attempt returns an empty body is it classified `empty_upstream`;
* a **non-empty** non-PDF stays a failure — that is an error page and it should be seen;
* `empty_upstream` is never retried on later passes and leaves the completeness denominator,
  but is printed on every pass and named in the sentinel. **Counted as absent, never quietly
  dropped** — silently excluding it would make 714/716 read as 716/716.

**2. Stale failure rows understating coverage.** After the retry passes, 32 dates had a PDF
*and* a parsed parquet while the ledger still recorded `failed:URLError`. Cause: when a later
pass finds the PDF already cached it `continue`s without refreshing the row, so the first
pass's failure survives. Coverage reporting reads the ledger, so it would have reported 32
dates as failures that were on disk and parsed. **Understating coverage is as wrong as
overstating it.** Fixed in two places: the downloader rewrites a stale failure row to
`cached` (keeping the prior status in the note, so the failure history stays auditable), and
the parser re-parses any day whose parquet exists but whose ledger status is not `ok` —
re-parsing being the only way to know whether it is `ok` or `ok_with_warnings`.

Final ledger: **542 `ok` + 190 `ok_with_warnings` = 732 parsed, 2 `empty_upstream`, zero
failures, zero malformed, zero date mismatches.**

---

## 14. The capacity gate tripped — decomposition, and one open decision

The gate first reported **20 scheme-seasons** with more than one design capacity inside a
single season. It decomposes into three causes with three different meanings.

**Confirmed on complete coverage (714 dates):** `clean` 1021, `edge_exception` 6,
`mid_season` 3 — *identical* to the counts on 352 dates. Quadrupling the evidence added no
new mid-season cases. The three are the population, not a sample of it. What did change is
that the change dates are now pinned to the day rather than bracketed.

### Cause 1 — my own probe dates, 11 of 20. Not a data property.

`pin_changeover.py` caches every PDF it fetches, by design. Its bisection probes were
**18 off-season dates** (March–May of 2023, 2024, 2025). `parse_cached.py` parses *every*
cached PDF, so they entered `fact_storage`, and `check_capacity.py` groups by
`EXTRACT(year …)`. A March–May date carries the **previous** water year's capacity under the
**next** calendar year's season label, so the water-year restatement (§11) appears as a
within-season change. Cleared entirely by restricting to Jun 1 – Oct 31: Dantiwada
2023/2024/2025, Chopadvav, Kakdi-Amba, Patadungari, WNKL.-BHEY, Edalwada, Doswada 2024,
Ver-II and Lakhigam 2023.

**Root cause: `season` is defined as the calendar year, but §11 established that the water
year turns on 31 May / 1 June.** The two disagree for exactly the March–May dates.

### Cause 2 — the 1-June edge, 6 of 20. One day wide.

The 2023→2024 restatement is a **two-day edit (1–2 June 2024)**. For the schemes that flipped
on **2 June**, the season's first day — 1 June, inside the Jun–Oct range — still carries the
old value. So the season holds two capacities: the old one on one day, the new one on the
other 152. Affects **Rudramata, Kaila, Suvi, Fatehgadh, Jhuj, Kelia** in 2024.

### Cause 3 — genuine mid-season restatement, 3 of 20. This is the real finding.

Pinned to the day on complete coverage (714 dates). The change date is **exact**, not a
window — every day either side is loaded:

| Scheme | Season | Held | Changed on | Then |
|---|---|---|---|---|
| Shetrunji (76) | 2024 | 415.44 MCM, 1 Jun – 2 Sep (94 days) | **3 Sep 2024** | 346.48 MCM, 3 Sep – 31 Oct (59 days) |
| Jhuj (43) | 2025 | 28.65 MCM, 1 Jun – 6 Aug (67 days) | **7 Aug 2025** | 24.45 MCM, 7 Aug – 31 Oct (85 days) |
| Kelia (44) | 2025 | 19.98 MCM, 1 Jun – 6 Aug (67 days) | **7 Aug 2025** | 17.46 MCM, 7 Aug – 31 Oct (85 days) |

**Jhuj and Kelia changed on the same day, and they are both in Navsari.** Two schemes in one
district revised together mid-season is a coordinated administrative edit, not a stray typo
in one row.

**These are not parse errors and not column shifts.** In every case the whole design triple
is restated coherently and `gross = live + dead` still holds, while OSL and FRL are
unchanged:

* Shetrunji 2024-08-05: 415.44 = 374.41 + 41.03, FRL 55.53 → 2024-09-11: 346.48 = 341.45 +
  **5.03**, FRL 55.53. Dead storage revised from 41.03 to 5.03.
* Jhuj 2025-06-11: 28.65 = 27.58 + 1.07 → 2025-09-11: 24.45 = 24.17 + 0.28.
* Kelia 2025-06-11: 19.98 = 19.23 + 0.75 → 2025-09-11: 17.46 = 17.36 + 0.11.

**The shape is a reversion, in both directions.** Each of the three begins its season at a
value that does not match the season before *or* after, then returns to it mid-season.
Shetrunji is 346.48 in 2022, 2023, 2025 and 2026 — the 415.44 exists only for part of 2024.
Jhuj and Kelia open the 2025 season at their **pre-2024** capacity and then go back to the
2024 value.

**Correction to what I said on partial coverage.** With 352 dates I read this as a season
table published with a stale figure and corrected shortly after, and said so. The complete
data does not support "shortly after": the wrong value stood for **67 days** for Jhuj and
Kelia and **94 days** for Shetrunji — two to three months of published reports, most of the
monsoon. The reversion shape holds; the "quickly corrected" reading does not. Whatever the
mechanism, these seasons have two genuine regimes, which is exactly why neither value can
stand for the season and why exclusion rather than correction is the right treatment.

**Two seasons with near-complete coverage — 2022 at 153/153 and 2023 at 140/153 — have zero
in-season capacity changes for all 206 schemes.** The within-season stability assumption is
not generally false; it has identified exceptions.

### Resolution — decided and implemented

Option C's like-for-like baseline needs one capacity per scheme-season. Establishing it is
now a decision taken once, in `check_capacity.py`, and written to
`data/processed/season_capacity.csv`, rather than re-derived from whichever row a query
happens to land on. Every scheme-season gets a class:

| Class | Rule | Season capacity | Count |
|---|---|---|---|
| `clean` | one value all season | that value | 1021 |
| `edge_exception` | minority value confined to a run of ≤ `EDGE_MAX_DAYS` at the season's **start** | the **majority** value; minority day recorded, not used | 6 |
| `mid_season` | anything else | **none — excluded from any baseline** | 3 |

**Cause 1 — off-season dates are excluded from season grouping, not relabelled.** I had
intended to define `season` as the water year (`year if month ≥ 6 else year − 1`), which
would classify the probe dates correctly rather than discarding them. **That does not work,
because the boundary is not a fixed date:** the 2023→2024 edit landed on 1–2 June 2024, the
2024→2025 edit on **31 May 2025**. Any fixed cutoff misclassifies one of them. So only
Jun 1 – Oct 31 dates count towards a season. The off-season reports stay in the database and
on disk — they are real reports, they are what pinned the changeovers, and nothing is
deleted.

This also sharpens §11: the water-year refresh is an administrative process on a calendar,
but the calendar moves by a day or two between years. "31 May or 1–2 June" is the finding,
not "1 June".

**Cause 2 — the majority value, with the exception recorded.** `EDGE_MAX_DAYS = 2`, because
the observed water-year edit spans at most two days. **This is a tunable parameter and it is
stated, not hidden** — same principle as the ≥5-day crossover run in §9. A minority run
longer than two days at the start of a season is not the boundary effect and falls through to
`mid_season`.

**Cause 3 — excluded from the like-for-like baseline, and published.** They cannot be
resolved by redefining anything, and correcting them silently would be worse than losing a
baseline year. Written to `capacity_midseason_exceptions.csv` with the change window. Effect
on the 2026-09-11 ranking: Shetrunji keeps 2022/2023/2025 and drops 2024; Jhuj and Kelia drop
2025. Reported as its own line in the output, never folded into the capacity-mismatch count —
the two exclusions mean different things.

### The gate still bites, on the right thing

`ACKNOWLEDGED_MID_SEASON = {(76, 2024), (43, 2025), (44, 2025)}`. A mid-season change **not**
in that set halts the pipeline with exit 2. Excluding a scheme-season from the baseline is a
decision the pipeline must not take on its own; these three were reviewed, a fourth is news.
As coverage completes, new cases are expected and must be reviewed, not auto-absorbed.

---

## 15. The default view — as built

`scripts/build_view_data.py` (all computation, no rendering) → `web/data/reservoir_view.json`
(215 KB) → `web/reservoir.html` (self-contained, hand-rolled inline SVG, no CDN and no
network at render time). Served locally:
`python scripts/serve.py --root web --port 8020` then `/reservoir.html`.

**Nothing is computed in the page.** Every figure comes from the JSON, so a wrong number is
wrong in one place and the provenance travels with it.

### What it shows

State headline (65.29%, hero) with the four-year mean and the explicit 2023 comparison, over a
full-width state line chart. Then five regional facets — **ordered worst deviation first, and
the interface says so** so the order is not misread as geographic. Each facet: % filling for
2026 heavy against the four prior years thin, a rainfall strip beneath sharing the x-axis, the
crossover marker, and the crossover sentence in words. Both deviation tables below with their
own headings and the question each answers. Provenance in the header, in the methodology
block and in the footer, including the report SHA-256.

### Crossover results on the first build

| Facet | Case | Detail |
|---|---|---|
| Kutch | crossed | **19 Jul 2026** (day 49), still below — 55 days |
| Saurashtra | crossed | **20 Jul 2026** (day 50), still below — 54 days |
| South Gujarat | crossed | **25 Jul 2026** (day 55), recovered 2 Aug; below 2 days now — *short of the rule, so no second crossing is claimed* |
| North Gujarat | crossed | **29 Aug 2026** (day 90), still below — 14 days |
| Central Gujarat | **never_below** | below the prior mean, but never under *all four* for 5 days |
| Gujarat (state) | crossed | **27 Jul 2026** (day 57), recovered 2 Aug, **below again since 26 Aug** — 17 days |

**The spec needed one addition.** §9 defines the marker as the *first* qualifying run. The
state series crossed on 27 July, recovered on 2 August, and fell below again on 26 August — so
reporting only the first run would have read as "recovered" while it is below every prior year
today. The marker definition is unchanged; a **current run** now travels with it, and the
wording distinguishes a current run that meets the 5-day rule from one that does not. South
Gujarat is the case that proves it: 2 days below is reported as 2 days below, not as a crossing.

### The regional denominator — and why it is not the published one

Regional % filling is sum(present gross) / sum(design gross). Taking design gross *per date*
puts steps in the prior-year lines that no rainfall caused:

* **Saurashtra 2024** jumps **68.97 MCM (2.66% of the region)** on 3 September — Shetrunji's
  mid-season restatement (§14).
* **Kutch 2024** jumps **7.03 MCM (2.16%)** between day 1 and day 2, because the four Kachchh
  edge-exception schemes still carry the previous water year's capacity on 1 June only.

So the denominator is the **season capacity** (§14), which removes both. For the three
scheme-seasons with no season capacity, the scheme's **2026** capacity is used — the value
every other season of those three agrees on. That is the narrow, labelled use of option B held
in reserve, not a rebasing of everything, and the affected facets say so on their face.
Scheme composition is held constant at all 206 in every season: dropping a scheme-season would
make the lines incomparable in a way no reader could see.

**The deviation tables deliberately do not use that substitution** — they join the *published*
season capacity, so the three excluded scheme-seasons stay excluded from the baseline.

### Honesty details that took specific work

* **A "latest report" rule on every facet.** 2026 stops at day 103 while prior years run to
  153. Without the rule the shorter line reads as a collapse.
* **No rainfall strip on the state chart.** A regional mean of regional means is not worth
  showing, and an empty panel reads as "no rain" rather than "not shown".
* **Days of water appears only on a scheme's detail**, always labelled *at today's release
  rate*, and reads **"no current release"** for the 179 of 206 at zero — verified on Kaila
  (none) and Dholidhaja (1,995 cusecs → 2.8 days).
* **A table view for every chart**, so no value is reachable only by hover; keyboard arrows
  move the crosshair and show the same figures.
* **No forecast language.** Checked against the rendered text, not just intent.
* The interface states the ≥5-day run length, the 0.5% capacity tolerance, the three excluded
  scheme-seasons by name, the two dates absent upstream, and that command areas are not in
  this data so it cannot speak to any particular field.

### Colour

Built against the data-viz reference palette using **only values documented there as passing**:
2026 is categorical slot 2 (orange `#eb6834` / `#d95926`); the four prior years are the blue
*sequential* ramp stepped light→dark for 2022→2025, because year is ordered data rather than
nominal. Dark mode uses that palette's own dark steps, not an inverted flip. **The palette
validator was not run — it needs Node, which is deliberately not installed here** — so the
mitigation is to take documented-passing values rather than eyeball new ones. Identity never
rests on hue alone: a legend is always present, 2026 is directly labelled, and the tooltip and
table view carry every year's value.

---

## 16. The map cannot be built from this source — and what was built instead

**The blocker, with evidence.** The WRD daily report gives every dam a **district and a
taluka and never a coordinate**. There is no latitude or longitude column in any of the six
tables in the file (checked against the parsed 206-row day: zero columns matching
`lat|lon|coord|geom|easting|northing`).

**OpenStreetMap cannot supply them either.** An Overpass query for dams and reservoirs in
Gujarat (`waterway=dam`, `water=reservoir`, `landuse=reservoir`) returned 2,210 elements, of
which **110 carry both a name and a position**. Normalised name matching against the 206
schemes resolved **9 exact single matches — 4.4%** — with 4 ambiguous and 193 unmatched. The
failure is structural, not a normalisation bug: WRD writes `Aji - II`, `Demi - I`,
`Bhadar (P)` where OSM has `Aji Dam 3`, `Demi Dam 1`, `Bhadar`. Cleverer matching might reach
30%, but asserting that `Aji-II` is `Aji Dam 3` would place dams **confidently in the wrong
place**, which is worse than not placing them.

**Centroid placement was the other option and is worse than no map.** Several dams share a
taluka, so taluka-centroid points would coincide and need jitter — synthetic position, banned
by §8.1. And the admin geometry held here (`web/data/gj_districts.geojson`,
`data/raw/admin/district.gpkg`) is **PC11 2011 vintage, 26 districts**: Devbhoomi Dwarka,
Morbi, Botad, Arvalli, Gir Somnath, Mahisagar and Chhotaudepur did not exist yet, so
**46 of 206 schemes would sit at a parent district's centroid** — and that set includes
Devbhoomi Dwarka, which carries most of this season's deficit.

### What the second view is

A **"Schemes" view**, reached from a switcher in the header; **Season remains the default**.
It renders all 206 dams as **one point each, sized by design capacity, coloured by deviation,
with nothing filled** — every requested encoding except the geography, positioned against the
two axes the data does support: deviation (x) against design capacity (y, log). A region
filter row sits above the plot; clicking a point opens that scheme's detail.

* **Colour is diverging, and that is a data fact rather than a preference:** 164 of 206
  schemes are below their prior same-date mean and **42 are above** (up to +42.9 pp), so
  polarity is real. Documented diverging pair, blue ↔ red, neutral grey straddling zero.
* **Size is binned, not a continuous area scale.** Design capacity spans 1.33 to 7,414 MCM —
  3.7 orders of magnitude — so a true area encoding would render everything except Ukai as a
  dot. Five bins with an exact legend, which does not pretend to be readable to the MCM.
* **`mapSVG()` is the real map and is already written.** It draws state and district geometry
  as **strokes only, never fills**, with the same size and colour encodings. It switches on by
  itself the moment `data/reference/dam_coordinates.csv` exists with
  `scheme_id, lat, lon [, source]`; the view states how many of 206 are placed and does not
  imply a position for the rest. Boundary geojson is only fetched when there is something to
  draw under it.

**To get the real map, supply coordinates.** CWC's National Register of Large Dams and
India-WRIS both publish reservoir positions; either would cover the large schemes. That is the
only remaining step.

### Two defects found while building it

**1. Hazard 5 was not cosmetic, and my "it moves no number" was too narrow.** It is true of
storage and false of district labels. `dim_scheme.district_latest` takes the label from the
**latest** report, and on 2026-09-11 Kabarka's district cell reads a truncated `Devbhumi` — so
Gujarat had a **phantom 28th district holding one scheme**, already visible in the shipped
deviation table as `Kabarka | Devbhumi`. Fixed with the closed vocabulary now in
`fetch_dam.DISTRICT_CANON` / `canon_district()`, **applied as a derived fix in
`build_view_data.py`** rather than a re-parse: the raw parse stays untouched and auditable, the
vocabulary lives in one place, and a future re-parse applies it at source. 28 → 27 districts,
one row relabelled, Kabarka back with the other eleven Devbhumi Dwarka schemes.

**2. Days of water carried spurious precision at the top end.** Ukai, releasing 800 cusecs
against 5,043 MCM live, computes to **2,576.8 days**. Quoting a seven-year figure to a tenth of
a day asserts precision the arithmetic does not carry. Now: tenths below 100 days, whole days
to a year, and beyond that the years in brackets — Ukai reads **"2,577 days (about 7.1 years)
at today's release rate"**. The zero case is unchanged at **"no current release"**.

### Verified in the browser, not assumed

206 points; region filter Kutch → 20 → back to 206; point click opens the detail; both legend
scales render; Season hidden when Schemes is shown; no console errors. Days of water:
Dholidhaja 2.8 days, Ukai 2,577 days (about 7.1 years), Kaila and Kadana "no current release".
The stale-tooltip fix was verified by dispatching the real events: tooltip opens on hover
(crosshair 0.5), **dismissed on scroll**, reopens, **dismissed on resize** (crosshair 0).

---

## 17. The source is unreachable from GitHub-hosted runners

**Probed 12 September 2026, before building any daily automation.** The question was whether
a scheduled GitHub Actions workflow could fetch the daily report at all. It was worth one
minute of probing rather than a day of building, and the answer was no.

`.github/workflows/probe-reachability.yml` — stdlib only, no checkout, no `pip install`,
`contents: read`, manual trigger. Nothing that could fail *before* the request, so the result
could not be ambiguous.

| | Result |
|---|---|
| GitHub-hosted runner, egress `64.236.134.161` | **`Network is unreachable`, both TLS attempts** |
| This machine, Indian residential address | HTTP 200 in 21.4 s, 276,237 bytes, **SHA-256 match** |

### The error is a route failure, not a refusal — and that distinction matters

`Network is unreachable` is `ENETUNREACH`: no route to the address. A portal *refusing* an
address range produces a 403, a connection reset, or a timeout — not this. And the host
publishes **both** families:

```
A     103.78.200.187
AAAA  2001:df6:c800::674e:c8bb
```

GitHub-hosted runners routinely have IPv6 configured but unrouted. `getaddrinfo` returns the
AAAA record first, the connection dies with exactly this error, and **the A record is never
tried.** So two completely different conclusions produce an identical symptom:

* IPv6 is broken on the runner → force IPv4 and everything works
* the portal refuses the runner's address range → the approach is dead

The revised probe settles it in one run rather than guessing: raw TCP to port 443 **per
address family**, then HTTPS twice — normal dual-stack resolution, then IPv4 forced through a
`getaddrinfo` filter — printing the matrix. Verified locally that both paths return the
expected SHA-256, that the filter leaves SNI and certificate validation intact, and that the
forced path is a resolution change only: no URL, no parsing, nothing else moves.

**Result of the IPv4 re-run: pending.** This section is finalised when it lands.

### A correction to §2 while here

§2 records that TLS "does not validate in our environment" and `backfill.py` disables
verification accordingly, keeping SHA-256 as the integrity check instead. **Both probe runs
validated the certificate on the first attempt, with `ssl.create_default_context()` and no
fallback.** Either the chain was fixed since, or the original finding was narrower than
recorded. Not yet acted on — one observation is not grounds for changing the fetcher — but if
the runner also verifies, verification should go back on and transport integrity stops being
something this project trades away.

### What follows regardless

Whether or not IPv4 rescues it, this is a real limitation and belongs in the README, not
buried here: **the published page is a dated snapshot, not a live feed.** It states its own
report date and build time on its face, so staleness is visible rather than silent. If
hosted runners cannot reach the source, the refresh has to run somewhere that can — a
machine on a network the portal serves — and the honest options are a scheduled task on such
a machine, or a documented one-command manual refresh.

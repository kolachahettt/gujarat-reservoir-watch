# Project brief — PMFBY basis-risk decision engine

Handoff document. Read fully before proposing anything. Written 11 September 2026.

**Revised 11 September 2026** after review. Ten changes accepted: §10.1 deleted (wrong
project's validation), MP PMFBY data corrected to *applications* not claims, district yield CV
removed from the index, **irrigated/unirrigated split promoted to core spec**, §10 rewritten
around construct validation, H3 and DuckDB-WASM cut from v1, novelty claim narrowed,
panchayat/village unit mismatch added to §6, §9 confidence and cut-point rules given concrete
definitions, licence moved to day one. §12 reordered so the kill-check runs first.

---

## 1. What this is

A village-level decision engine for India's crop insurance scheme (PMFBY) that tells a
farmer whether the product is **structurally likely to fail him** even when it operates
exactly as designed — and tells a block officer which insurance units need subdividing.

Not an atlas. A decision engine with a verdict at the point of decision.

---

## 2. Hard constraints — do not renegotiate these

| Constraint | Detail |
|---|---|
| Timeline | 3–4 weeks to demoable. Placement interviews imminent. |
| No satellite imagery | Explicitly ruled out by the user. Vector and tabular only. |
| India | Not EU, not US. |
| Compact | One index, one map, one validation. Not a platform. |
| Every number traceable | See §9. This is the non-negotiable one. |
| Role targets | Data science, GIS/remote sensing, full-stack, agritech domain. |

---

## 3. Decision trail — already rejected, do not re-propose

- **Any satellite/imagery project** (Sentinel-1 paddy transplanting compliance in Punjab,
  crop classification, field boundary delineation) — user ruled out imagery.
- **Himachal apple chilling hours.** Killed on physics: winter temperature inversions cause
  cold-air pooling in Himalayan valleys during Nov–Dec, flattening the lapse rate exactly
  in the chill-accumulation season. Gridded reanalysis is documented as failing to capture
  western-Himalayan lapse-rate processes. Chill is a threshold function near 7 °C, so a 2 °C
  downscaling error destroys the answer. Novelty was also overstated — GIS chill mapping is
  established (published mainland-Spain study used 72 stations over 1975–2015; `chillR` has
  been on CRAN for years). Himachal's apple belt lacks that station density.
- **Kansas / Ogallala aquifer and EU (France RPG, Denmark)** — best data in the world, but
  the user wants India.
- **Cold-chain / post-harvest loss facility siting** — viable fallback, data verified
  (see §5), but less app-shaped than the current choice.
- **Region-specific pilots** (Mehsana, Anand, Punjab districts) — moot; the current project
  is tabular and works at state or national scale.

---

## 4. The mechanism being modelled

PMFBY pays on an **area-yield index**, not individual loss.

- Insurance Unit (IU) = **village / village panchayat** for major crops; revenue circle,
  hobli or mandal for other crops.
- Threshold Yield (TY) = average of the **best five of the past seven years** × indemnity
  level (70 / 80 / 90 %).
- Actual Yield (AY) from Crop Cutting Experiments, supplemented from Kharif 2023 by
  YES-TECH (remote sensing + smart sampling).
- Claim = `((TY − AY) / TY) × Sum Insured × Area`, paid to **every** enrolled farmer in
  the unit if AY < TY.

**Basis risk** is the gap between unit-average yield and an individual farm's yield. It is
officially acknowledged: a PMFBY compendium paper on the scheme's own portal states that
homogeneity within an IU is rare, that natural resources, management practices and pests
vary spatially, and that basis risk can leave some farmers worse off than with no insurance
at all.

**Novelty claim, narrowed:** there is no *public village-level map of structural basis-risk
drivers* that could be found. Do **not** say "nobody has mapped it." Published work exists on
basis risk in area-yield insurance and on PMFBY insurance-unit homogeneity — be ready to name
it. Overstated novelty is the cheapest thing for an interviewer to puncture, and §3 records
that exact mistake already being made once on the Himachal option.

---

## 5. Verified data inventory

Everything below was checked against primary documentation on 11 Sep 2026. Figures are
DDL's own completeness statistics.

### SHRUG (Development Data Lab) — the spatial and covariate backbone

- **Version: SHRUG 2.2 "Pakora", released August 2026.** Re-download; do not use older.
- Unit = `shrid2`, a village/town unit with **consistent boundaries since 1991**.
- Open PC11 village/town shapefile: **649,618 polygons**. Open shrid-level file:
  **595,438 polygons** — **measured in `data/raw/shrid2_open.gpkg` on 11 Sep 2026.**
  An earlier revision of this brief said 575,153; that was an error. **The file is
  authoritative.** The layer is named `shrid2`, single layer, CRS **EPSG:4326**, fields
  `shrid2, pc11_id, n, geometry_type, polysource, n_ncontig, maxdist_km`.
- **The polygon layer contains towns as well as villages.** The Village Directory does not.
  See §13 for the identification rule and the exclusion.
- **`shrid1` and `shrid2` are not interchangeable.** v1.5 "Samosa" IDs do not match v2.0+.
  Never mix versions.
- Village Directory file: `pc11_vd_clean_shrid.dta`, keyed on `shrid2`. Stata format —
  read with pandas `read_stata` or `pyreadstat`.
- Subdistrict file: `pc11_vd_clean_pc11subdist.dta`, keyed on `pc11_state_id`,
  `pc11_district_id`, `pc11_subdistrict_id`.

**Modules to download:** Open Polygons and Spatial Statistics (geometry), Population Census
(Village Directory), **Core Keys** (names and census keys — nothing joins without it).

**Irrigation by source, per village:**

| Variable | Description | Missing | Villages |
|---|---|---|---|
| `pc11_vd_land_canal_irr` | Area irrigated by canals (ha) | 0% | 588,058 |
| `pc11_vd_land_wl_tw_irr` | Area irrigated by wells/tube wells (ha) | 0% | 587,800 |
| `pc11_vd_land_tnk_lk_irr` | Area irrigated by tanks/lakes (ha) | 0% | 587,020 |
| `pc11_vd_land_w_fall_irr` | Area irrigated by waterfall (ha) | 0% | 586,735 |
| `pc11_vd_land_oth_src_irr` | Area irrigated by other source (ha) | 0% | 587,062 |
| `pc11_vd_land_src_irr` | Total area irrigated by source (ha) | 0% | 588,339 |
| `pc11_vd_land_un_irr` | Total unirrigated area (ha) | 0% | 588,743 |

**Land use, per village (all 0% missing):** `pc11_vd_land_nt_swn` (net area sown),
`pc11_vd_area` (total geographical area), `pc11_vd_land_cur_fal`, `pc11_vd_land_fallow`,
`pc11_vd_land_cult_waste`, `pc11_vd_land_uncult`, `pc11_vd_land_non_agri`,
`pc11_vd_land_fores`, `pc11_vd_land_pst_grz`, `pc11_vd_land_misc_trcp`.

**Agricultural power supply — second index dimension:**

| Variable | Description | Missing |
|---|---|---|
| `pc11_vd_power_agr` | Power supply for agricultural use | 6% |
| `pc11_vd_power_agr_sum` | Hours/day, summer (Apr–Sep) | 13% |
| `pc11_vd_power_agr_win` | Hours/day, winter (Oct–Mar) | 13% |

Why this matters: 90% tubewell irrigation with 6 hours of summer power is a different risk
object from 90% tubewell with 14 hours. Same irrigation mix, different ability to pump when
the monsoon fails.

**Market and infrastructure access (all 0% missing):** `pc11_vd_mrkt` (mandis/regular
market), `pc11_vd_wkl_haat`, `pc11_vd_ams` (agricultural marketing society), `pc11_vd_acs`
(agricultural credit societies), `pc11_vd_trctr` (tractors, 587,820), and the full road
hierarchy `pc11_vd_rd_nhw` / `rd_shw` / `rd_mdr` / `rd_odr` / `rd_p_btr` / `rd_k_grav` /
`rd_wbm` / `rd_all_wthr`.

**Distances:** `pc11_vd_subdistrict_hq_dist` (9% missing), `pc11_vd_district_hq_dist` (7%),
`pc11_vd_town_dist` (24%), `pc11_vd_s_town_dist` (13%).

**DATA QUALITY FLAGS — DO NOT USE. THEY ARE DEGENERATE IN SHRUG 2.2.**

Measured directly, 11 Sep 2026, on `pc11_vd_clean_shrid.dta`:

- `land_flag` — Stata label: "Flag for land values greater than area". **Constant `1` for all
  588,969 non-null rows (4 nulls). Zero distinct informative values.**
- `dist_flag` — Stata label: "Flag for distances greater than 3000". **Constant `1` for all
  588,973 rows.**

An earlier revision of this brief said "filter on both before computing anything, and report
how many rows were dropped." **That is not executable.** Filtering on a constant removes
either 100% or 0% of rows depending on assumed polarity; neither is a quality filter. The
first run of §12 task 1 dropped all 51,877 MP villages and reported on an empty set before
this was caught.

That revision also read "present for 588,969 villages" as the count of *flagged* villages.
It is the count of *non-null* values. The flags mark nothing.

**Substitute — build the checks yourself and report each separately:**

| Check | MP result | Note |
|---|---|---|
| `pc11_vd_land_nt_swn == 0` | 446 (0.86%) | rainfed share undefined |
| `pc11_vd_land_un_irr` missing | 185 (0.36%) | |
| `pc11_vd_area == 0` | 89 (0.17%) | |
| `pc11_vd_land_src_irr > pc11_vd_area` | 86 (0.17%) | impossible |
| `pc11_vd_land_nt_swn > pc11_vd_area` | 1 | impossible |
| **union** | **620 (1.20%)** | mark low-confidence per §9.4, do **not** delete |

The underlying land-use data is otherwise internally consistent: the nine land-use categories
sum to total geographic area at every quantile, and `un_irr + src_irr` equals `nt_swn` to
within 0.01 ha for 99.66% of rows. The flags are broken; the data is not.

**Known SHRUG limitations:** the codebook notes `tdist` distance variables are computed
**centroid to centroid** (straight line, not network). Agricultural production data is
"slated for inclusion in future versions" — i.e. SHRUG has no crop output. Limitations page:
`docs.devdatalab.org/Getting-Started/limitations/`. Variable search:
`docs.devdatalab.org/variable-search/`.

**Licence: CC BY-NC-SA 4.0.** Non-commercial, attribution required, share-alike on derived
data. DDL disclaims all warranty on accuracy, adequacy and completeness, and notes data may
go out of date quickly. Quote this in the README rather than glossing it.

### ICRISAT–TCI District Level Database — the agricultural time series

- `data.icrisat.org/dld/` — 571 districts, 20 states, **1966–2020**.
- **74 datasets, 1,030 variables, 11M+ data points.**
- Two versions: **apportioned** (1966 boundaries, post-1966 districts given back to parent
  districts, so unbroken 55-year time series) and **unapportioned** (current boundaries).
- Core: crop area / production / yield, irrigation by source and by crop, inputs, weather,
  infrastructure, demography, operational holdings, land use, agricultural wages, farm
  harvest prices.
- Additional files: season-wise crop area and production, fruit and vegetable breakdown by
  crop, night lights, agricultural credit, **godowns and cold storage**.
- Environment variables (AET, PET, precipitation) exist **only** in unapportioned,
  1956–2015.
- Districts are coded by Agro-Ecological Region and by 14 Production Systems — stratify on
  these rather than administrative units.

### PMFBY

- Public data is **state-wise and district-wise only**.
- **IU-level yield is NOT publicly downloadable.** Operational Guidelines require ten years
  of yield data at the notified level plus TY to be uploaded to the portal, but that is for
  tendering. RTI guides instruct farmers to write to the Block/District Agriculture Officer
  to obtain their own unit's CCE yield and TY. Treat IU yield as unavailable.
- `data.gov.in` has district-wise PMFBY farmer **applications** for **Madhya Pradesh,
  2021-22 to 2023-24** — confirmed. **Applications, not claims: enrolment, not payouts.**
  An earlier draft of this brief called these claims in §10; that was an error. This is
  **not** a validation target — see §10 for why district-level PMFBY validation is not
  attempted at all.

### Verified fallback dataset (if the primary project fails)

Post-harvest loss coefficients on `data.gov.in`: the CIPHET 2015 study (45 crops, 120
districts, 14 agro-climatic zones) published with **`Loss in Transport (%)` and
`Loss in Storage (%)` as separate fields** alongside overall total. NABCONS 2022 (reference
year 2020–22) is the newer study and found the same commodity loses differently across
states. NHB publishes cold storage name/address/district as state PDFs plus an ICAP map
platform — needs parsing and geocoding.

---

## 6. Open questions — resolve before building

1. **Mission Antyodaya Village Facilities (2020)** is a SHRUG module. It is a village-level
   survey nine years fresher than the 2011 Census. **Does it carry irrigation coverage?**
   If yes, build the index on 2020 data and the "your data is 15 years old" weakness
   largely disappears. **Now §12 task two, not task one** — the kill-check comes first.
   (URL encoding blocked a remote fetch; check directly at `docs.devdatalab.org`.)
   Decision rule and expected answer are in §12.
2. **PMFBY premium caps.** The standard structure is 2% of sum insured for kharif
   food/oilseed, 1.5% rabi, 5% annual commercial/horticultural — **but this was not
   independently verified and state notifications vary by crop and season.** Verify against
   the current season's state notification before hardcoding.
3. **Scale of Finance** values for the chosen state and crops — from the state notification.
4. **Panchayat / village unit mismatch — the analysis unit is not the insurance unit.**
   §4 states the IU is the **village panchayat** for major crops. A panchayat can contain
   several census villages, and `shrid2` is a census-village unit. So the model operates at a
   unit *finer* than the IU.

   This is not fatal. Aggregating villages up to a panchayat can only increase measured
   heterogeneity, so the village-level index is **conservative** — it understates basis risk
   relative to the real IU. But it must be stated explicitly in the UI and README, never
   glossed as "village = insurance unit."

   **Resolvable — not a permanent caveat.** An earlier revision of this brief recorded the
   crosswalk as unavailable and out of scope. That was wrong: SHRUG's **user-contributed**
   modules include **"Local Governance Bodies to Shrid Keys"** (contributor: Pratik Mahajan),
   which matches all shrid-level villages to Gram Panchayats and Urban Local Bodies using
   Local Government Directory (LGD) codes published by the Ministry of Panchayati Raj.
   Verified present in the live download catalogue, 11 Sep 2026.

   **Deferred to week 2.** Aggregating the village index to true GP-level insurance units is
   the correct unit and should be done, but it is not on the kill-check path. Until it is
   done, the conservative-direction argument above is what ships, and the UI must not claim
   village = insurance unit.

   Caveat to check when the crosswalk is used: it is a user-contributed module, so its
   coverage and match rate are its contributor's, not DDL's core QA. Audit match rates with
   row counts per §9.5 before relying on it.

---

## 7. Index design

Basis risk is higher when the insurance unit is internally heterogeneous.

**Core spec: the irrigated/unirrigated split is the mechanism. Source mix is second-order.**

Source diversity and farm-level heterogeneity are not the same thing. A village where every
farm draws on both a canal and a tubewell scores high on source diversity and is perfectly
homogeneous. A village at 100% canal with half its net sown area unirrigated scores *zero*
source diversity and is exactly the bimodal case where an area-yield index fails a farmer.
An earlier draft of this brief had these the other way round.

Components, in order of weight:

1. **Irrigated / unirrigated split** of net sown area — the mechanism. Irrigated and rainfed
   farms sharing one threshold yield diverge most in a drought year.
2. **Irrigation-source mix** among the irrigated area (Shannon or Simpson) — second-order.
   Included because pumping-dependent and canal-dependent farms fail in *different* years,
   not because mix alone implies heterogeneity.
3. **Village geographic area** — larger unit, more internal variation.

**~~Agricultural power hours (summer), as a pumping-capacity constraint.~~ BUILT AND DROPPED
11 Sep 2026 — do not re-add.** It was implemented as tubewell share × power shortfall, run
nationally, and removed. Reasons, measured:

- It cost **9.3% of national coverage** (41,049 villages missing `pc11_vd_power_agr_sum`)
  while barely moving the ranking: Spearman **0.9632** against the weighting that included it,
  with only **4.87%** churn in the top-decile subdivision list.
- The missingness is concentrated, not random: **Karnataka 99.4%**, Chhattisgarh 52.5%,
  Himachal 13.1%, every other state ≈0%. §5's national 13% figure is almost entirely these
  three states.
- Including it rendered **Karnataka's 27,237 villages as 99.5% low-confidence** for a
  component that barely changes their rank. Dropping it took national low-confidence from
  **14.40% to 5.32%** and Karnataka from 99.5% to 0.9%.

The component is still **computed and stored** as `c3_power_constraint_dropped` in the output,
and a standing `restore_c3` row in the sensitivity table quantifies the decision, so it stays
auditable rather than becoming folklore. It is not in the index.

Operationally, 1 and 2 collapse into a single **6-category** diversity measure over the five
irrigation-source columns **plus `pc11_vd_land_un_irr`**. That specification is preferred over
the 5-source version. §12 task one computes both and reports both distributions — the
6-category version is not a robustness check, it is the substantively better spec.

**The index is strictly village-level.** District yield CV is deliberately **not** a component.
Two reasons: (a) it would be circular, since §10 uses it as the external check; (b) it is
constant across every village in a district, so including it injects a district-level signal
that visually dominates a village-level map and makes the product look like a district
choropleth.

Weighting must be justified and a sensitivity analysis run. Do not hand-tune weights to
make the map look good.

**Weights as built** (`scripts/build_index.py`): c1 **0.50**, c2 **0.35**, c4 **0.15**. Half
to the mechanism per the ordering above; the remainder split preserving c2 and c4's original
0.25 : 0.10 ratio to within rounding.

**Every component is mandatory — there is no renormalisation.** A village either has all
three or gets no index. The reasoning generalises the c1 rule: a village scored on two
components and its neighbour scored on three are not comparable, and comparing neighbours is
the index's entire purpose. Nationally 513,273 of 588,973 villages (87.15%) receive an index;
the rest carry an `index_status` saying why.

**c1's denominator is the AGGREGATE irrigated column, not the sum of the five source
columns.** This matters and an earlier version got it wrong: in states failing the
reconciliation gate the five columns are incomplete, so a five-column denominator silently
corrupts the mechanism itself. Numerically identical across the 18 reconciling states, wrong
everywhere else — exactly the class of error that survives testing on a good state.

Measured sensitivity, Spearman against the stated weighting with churn in the top-decile
subdivision list:

| Scheme | ρ | Decile churn |
|---|---|---|
| mechanism_heavy (0.70/0.20/0.10) | 0.9837 | 3.33% |
| drop_size (0.60/0.40/0) | 0.9724 | 4.40% |
| restore_c3 (the dropped component) | 0.9632 | 4.87% |
| equal thirds | 0.9352 | 4.70% |
| mechanism only (c1 alone) | 0.9225 | 9.57% |

**The ranking is robust to weighting** — even equal thirds holds ρ 0.935, and no scheme moves
more than 9.6% of the subdivision list. The weights are not load-bearing, which is the
outcome a sensitivity analysis should produce. Report it this way rather than claiming the
weights are optimal.

---

## 8. Stack

| Layer | Choice | Note |
|---|---|---|
| Storage | GeoParquet | bbox columns + **Hilbert ordering** for row-group skipping |
| Compute | DuckDB + spatial extension | v1.5 "variegata"; embeds PROJ so `ST_Transform` works, R-tree indexing, GDAL-backed `ST_Read` |
| Optimisation | `geoparquet-io` (gpio) | Released March 2026; enforces bbox, Hilbert, ZSTD, row-group sizing |
| Tiles | PMTiles | Single file, static hosting, no tile server |
| Frontend | OpenLayers | User is already fluent — do not make them learn MapLibre under time pressure |
| Deploy | Static host | Zero cost, stays live through placement season |

**v1 pipeline:** offline Python/DuckDB → GeoParquet → PMTiles → OpenLayers. Fully proven,
reaches a live URL in days.

**Cut from v1 — do not re-add under time pressure:**

- **H3.** The unit of decision is the village polygon and PMFBY IUs are village panchayats
  (§6.4). Hexagonal aggregation solves a problem this project does not have.
- **DuckDB-WASM in-browser querying.** An earlier draft put "verify spatial and H3 extensions
  load in WASM" in week one — that spends the scarcest week on an optional capability, and
  DuckDB-WASM extension support has documented limitations. Client-side DuckDB is a **week-4
  stretch panel only**. It is feasible later, not on the critical path: GeoLibre 1.0
  (June 2026) streams a sorted 180 GB GeoParquet with millions of polygons in-browser via
  DuckDB, generating tiles on pan/zoom with features still clickable.

**Risk to handle at ingest in week one — this one stays:**

- DuckDB geometry columns historically do **not** persist projection info. Track CRS yourself.
- Silent failure mode: WGS84 lat/lon joined against polygons in a projected CRS gives wrong
  distances and wrong joins with **no error**. Standardise CRS explicitly at ingest.

**At national zoom-out**, 575k polygons need a generalised overview layer. Plan for it; do
not discover it in week three.

---

## 9. Honesty rules — the lesson from the previous project

The user's prior app (AgriRent, 9,555 lines) was well engineered but its entire data layer
was synthetic: hardcoded `REGIONS`, `LABOUR`, `OWNER_INVENTORY`, `OWNER_BOOKINGS`,
`ANALYTICS_DATA` arrays plus `Math.random()`. Its report presented invented figures — 250
equipment units, 1,200 farmers, 4,000 transactions, ₹50.6 lakh subsidy — as findings. OSRM
was the only live external service.

Rules for this project:

1. **No synthetic data anywhere.** Not for demos, not for placeholders.
2. **Every number on screen traces to a named source** the user can say out loud in an
   interview.
3. **The index maps structural drivers of basis risk, not measured basis risk.** Measuring
   it needs IU-level yields, which are not public. Never let the framing drift.
4. **Uncertainty visible in the UI — definition fixed now, not in week three.** A village is
   **low-confidence** if it carries `land_flag`, is missing agricultural power hours (13% of
   villages), or reports zero net sown area. Low-confidence villages render as explicit
   no-data. **Never impute.**
5. **Report row counts at every join.** Census 2011 codes, LGD codes and `shrid2` are three
   incompatible systems; student projects silently lose 20–40% of rows on joins. Audit
   explicitly.
6. **The subdivision cut point is a stated rule, not a judgement.** The block-officer verdict
   ("which IUs need subdividing") implies a threshold on a continuous index. The rule —
   e.g. top decile within state — is chosen and written down **before** the map is rendered.
   §7's ban on hand-tuning weights applies equally to the cut point.
7. **Licence obligations go into the repo on day one.** CC BY-NC-SA share-alike attaches to
   the published tiles and the derived index, not just to a README paragraph — it constrains
   what may be published, so it cannot be a week-four task.

   **The obligation is more specific than "attribute DDL".** SHRUG's bundled `README.md`
   requires that any data linked to the SHRUG be **posted with SHRUG identifiers** at
   publication, and that **the all-India series be posted rather than just the analysis
   sample**. So:

   **The published dataset covers all 35 states and UTs, keyed on `shrid2`, every row
   carrying its state verdict. The 18-state v1 decision governs what the MAP RENDERS, not
   what the dataset contains.**

   This is not a grudging compliance step — it is the better artefact. A dataset that
   publishes 588,973 villages with an honest per-row status, including the 45,427 where the
   source data is unreliable and the 6,183 with no land-use data at all, is more useful and
   more defensible than one silently truncated to the states that happened to work. The
   negative results are part of the finding (§13).

---

## 10. Validation design — all desk-based, no fieldwork

**Outcome validation is impossible, and that is a designed boundary, not an omission.**
Measuring whether the index predicts realised basis risk requires IU-level yields, which §5
establishes are not public. Say that plainly. Do **not** ship a weak correlation dressed as a
test — an interviewer will respect the stated boundary and dismantle the fake test.

What ships instead:

1. **Construct validation.** Does the index behave as the §4 mechanism predicts — high in
   canal-tail and mixed irrigated/rainfed tracts, low in uniformly rainfed and uniformly
   irrigated ones?
2. **Face validity against known landscapes.** Named, checkable places where the expected
   answer is independently known.
3. **Coarse external check: district yield CV** per crop, ICRISAT DLD 1966–2020. A district of
   internally heterogeneous villages should show higher yield CV. This is precisely why
   district CV is kept *out* of the index (§7) — it cannot be both component and check.
4. **Ablation.** Does each index component earn its place?
5. Report all of the above **with confidence intervals, not a single headline number.**

**Not attempted, and why — both were errors in the original brief:**

- **NABCONS 2022 state commodity totals.** Carryover from the §5 cold-chain fallback. A
  heterogeneity index has no commodity totals to aggregate and NABCONS publishes nothing to
  compare against. Deleted outright.
- **District-level PMFBY validation.** The MP data on `data.gov.in` is farmer **applications,
  not claims** — enrolment, not payouts. Even with real claim data, district claim frequency
  is driven overwhelmingly by weather rather than by basis risk, with an ecological-inference
  problem on top: a village-level heterogeneity index regressed on district outcomes tests
  nothing.

---

## 11. Week plan

- **Week 1** — §12 in order: kill-check, Mission Antyodaya, then the full download. Licence
  and attribution into the repo on day one (§9.7). CRS standardised at ingest (§8). Apply
  `land_flag` / `dist_flag`. Join audit with row counts. **No WASM testing.**
- **Week 2** — Build the index per §7. Sensitivity analysis. Validation per §10. Pull the
  LGD→shrid Gram Panchayat crosswalk and aggregate to true insurance units (§6.4), auditing
  match rates.
- **Week 3** — PMTiles, decision-engine UI, deploy to a live URL.
- **Week 4** — README with honest limitations. Buffer. Client-side DuckDB panel only if
  everything above is genuinely done.

---

## 12. First three tasks — reordered

Order is **3 → 1 → 2** from the original brief. The kill-check was gated behind a
multi-gigabyte download and an open question; both are wasted effort if the distribution
turns out degenerate.

### Task 1 — the kill-check: Madhya Pradesh Village Directory

**MP because of its genuine irrigated/rainfed mix.** Note the rationale has changed: it is
*not* the PMFBY data, which lapsed as a validation target under §10. ~55k villages.

Needs only `pc11_vd_clean_shrid.dta` plus Core Keys. **No geometry** — a distribution does not
require polygons.

1. Load to DuckDB, filter to MP, report raw row count.
2. **Do not filter on `land_flag` / `dist_flag` — they are constant `1` and carry no
   information (§5).** Report their distinct-value counts so the degeneracy is visible in the
   output, drop nothing on them, and run the substitute integrity checks in §5 instead,
   reporting each separately and their union as low-confidence (§9.4).
3. **Reconciliation QA (not in the original brief):** does `pc11_vd_land_src_irr` equal the
   sum of the five source columns? Report the residual distribution. If it does not reconcile,
   the diversity measure is being computed on inconsistent denominators, and that is resolved
   before anything else.
4. Compute diversity **both ways** and report both distributions:
   - 5 irrigation-source columns only (original spec, max ln 5 ≈ 1.61)
   - **6 categories** — the five sources plus `pc11_vd_land_un_irr` (**preferred**, per §7)
5. Report: full histogram, share at exactly zero diversity, share with only one non-zero
   source, share with zero net sown area, share missing power hours (§9.4 confidence rule).

**Kill criterion, fixed in advance:** if the 6-category version puts **>70%** of villages at
effectively single-category, the index as designed is dead and the project **reshapes rather
than gets patched**. Written down before the histogram is on screen so the threshold cannot
drift once the numbers are visible.

### Task 2 — Mission Antyodaya (2020)

Check `docs.devdatalab.org` variable-search and the MA questionnaire directly.

**Decision rule:** MA replaces the Census Village Directory as the index base **only** if it
carries irrigated area by source in hectares. Expectation is that it does not — MA is a
facilities/amenities survey, so irrigation is likely binary availability or facility counts,
which cannot produce a source-share measure. Also check MA's **unit** (some rounds are
gram-panchayat-level, a mismatch with `shrid2` — see §6.4) and its actual coverage rate.

Realistic best case is a **2020 covariate overlay and freshness cross-check** — do villages the
2011 VD calls unirrigated report irrigation facilities in 2020? — not a replacement base. That
does not make the "your data is 15 years old" weakness disappear the way §6.1 hoped.

### Task 3 — full SHRUG 2.2 download

Only after the index survives task 1.

- **Open Polygons — shrid-level (575,153), not PC11 village-level (649,618).**
- Population Census (Village Directory) — already pulled in task 1.
- Core Keys — already pulled in task 1.

Verify the v2.2 "Pakora" version string on the live download page rather than trusting this
brief, and check whether download requires registration.

**`data/raw/MANIFEST.md` records, for every file:** download URL, SHRUG version string, file
hash, byte size, retrieval date. Provenance is captured at download time — reconstructing it
from memory later is how §9 gets violated.

---

## 13. Measured results — §12 task 1, completed 11 September 2026

Source: `pc11_vd_clean_shrid.dta`, SHRUG 2.2 Pakora, 588,973 rows, 284 columns.
Scripts: `scripts/killcheck_mp.py` (single state), `scripts/sweep_states.py` (all 35).
Full per-state output: `data/interim/state_sweep.csv`.

**Verdict: the kill criterion is passed and the index is viable — but only in 18 of 35
states and UTs, and the reasons the other 17 drop out are not all the same reason.**

### The four states run first

Rainfed share = `un_irr / (un_irr + src_irr)`. Dominance = share of villages where one of the
six categories holds ≥95% of area.

| State | Villages | Rainfed mean / median / IQR | 6-cat dominance | 5-source dominance | Verdict |
|---|---|---|---|---|---|
| Madhya Pradesh | 51,877 | 0.581 / 0.608 / 0.486 | **15.3%** | 58.4% | PASSES |
| Rajasthan | 39,669 | 0.585 / 0.617 / 0.608 | 30.0% | 97.1% | PASSES |
| Jharkhand | 29,481 | 0.815 / 0.934 / 0.263 | 48.0% | 42.7% | PASSES |
| Punjab | 12,163 | 0.031 / 0.000 / **0.000** | 63.3% | 71.4% | **MECHANISM ABSENT** |

**The §7 core-spec change was load-bearing.** Under the original 5-source specification
Punjab (71.4%) and Rajasthan (97.1%) both exceed the 70% kill threshold — the project would
have died on its second state. The 6-category measure including `un_irr` is what makes it
viable. This is the single most consequential decision in the brief.

**The Punjab caveat — why a fourth verdict state was added.** Punjab's rainfed-share IQR is
exactly 0.000: 10,949 of 12,151 villages sit in the lowest histogram bin. The §7 *mechanism*
has no variance there. It nonetheless cleared the dominance test at 63.3%, carried entirely
by canal-versus-tubewell mix — the component §7 explicitly demotes to second-order. A state
like this must report **"mechanism absent, index uninformative here"**, never PASSES.

### The mechanism threshold — justification

The first version used a single cut, `MECHANISM_MIN_IQR = 0.10`, chosen by eye after seeing
four states. **That cut was not defensible.** Across the 35 states the rainfed-share IQR runs
0.000, 0.017, 0.088, 0.115, 0.148, 0.151, 0.166, 0.228, 0.243, 0.250, 0.263 … — continuous
through the entire region where a cut would sit. Any single number in 0.09–0.16 is arbitrary,
and Haryana (0.088) versus Goa (0.151) turned on it.

Replaced by a **banded rule with a substantive anchor**:

| Band | Rule | Meaning |
|---|---|---|
| `MECHANISM ABSENT` | IQR < **0.05** | Middle half of villages span under half an indemnity step. One regime; index uninformative. |
| `MECHANISM WEAK` | 0.05 ≤ IQR < **0.15** | Mechanism varies but spans less than one indemnity step. Excluded from v1 primary coverage, reported with the margin visible. |
| present | IQR ≥ **0.15** | Middle half span more than one indemnity step. Proceed to the dominance test. |

**Anchor:** PMFBY's own indemnity levels step in 10-percentage-point increments (70 / 80 /
90%). Two villages are treated as materially different in exposure when their rainfed shares
differ by at least one such step; a state qualifies when the middle half of its villages span
more than one.

**Caveat, stated because it matters:** indemnity levels are percentages of threshold *yield*,
while rainfed share is a percentage of *area*. The anchor is an argument by analogy to the
scheme's own granularity, **not a derived constant**. It is a documented convention. Do not
present it as physics, and do not defend it as anything stronger in an interview.

**Effect of the change:** Haryana (0.088) moves from ABSENT to WEAK — more honest, and
excluded from v1 either way. **No state enters or leaves v1.** Coverage stays at 18 states
and 442,918 villages.

**Uttar Pradesh is the fragile inclusion and must be labelled as such.** IQR 0.166 clears the
`present` line by **+0.016**, and UP carries **97,737 villages — 22% of all v1 coverage**, the
largest state in the product. Its mean rainfed share is 0.128 with 57.2% of villages in the
extreme bins: a heavily irrigated state with a rainfed tail, not a mixed one. UP is included,
but it renders with its marginal status visible per §9.4, and any headline coverage figure
that leans on UP's 97,737 villages should say so.

### National sweep — per state, never pooled

A pooled all-India histogram would look well spread **precisely because** states differ
(Punjab uniformly irrigated, Assam uniformly rainfed). That measures between-state variation,
not the within-unit heterogeneity the index is about. Pooling flatters the result; do not
do it.

| Verdict | States | Villages | Share of rows |
|---|---|---|---|
| PASSES | 18 | 442,918 | 75.20% |
| FAILS (dominance > 70%) | 1 — Odisha | 47,472 | 8.06% |
| MECHANISM WEAK | 1 — Haryana | 6,610 | 1.12% |
| MECHANISM ABSENT | 4 — Punjab, Assam, Manipur, A&N | 40,183 | 6.82% |
| SOURCE DATA UNRELIABLE | 3 — Maharashtra, Arunachal, Nagaland | 45,427 | 7.71% |
| NO DATA | 2 — Meghalaya, Mizoram | 6,183 | 1.05% |
| TOO FEW ROWS (<100) | 6 | 180 | — |

Full detail in `data/interim/state_sweep.csv`. The Maharashtra defect is written up separately
in `docs/finding-maharashtra-source-reconciliation.md`.

Strongest spread (highest rainfed IQR): Jammu & Kashmir 0.812, West Bengal 0.769,
Rajasthan 0.608, Gujarat 0.598, Tamil Nadu 0.583.

### Maharashtra — the near-miss that matters most

**Maharashtra reports 15,120,923 ha irrigated while its five source columns sum to
3,711,391 ha**, and 8,171 villages report an irrigated total with all five sources at zero.
Because the 6-category measure is built on the five source columns plus `un_irr` — *not* on
the `src_irr` aggregate — Maharashtra scored **0.0% single-category, the best number in the
entire national table, as a pure artefact of missing data.** 40,810 villages, the
fourth-largest state, would have been presented as the strongest evidence for the index.

This is exactly the §9 failure mode. The reconciliation QA in §12 step 3 is the check that
catches it, so it is now a **verdict gate, not a reported statistic**:
`RECON_FAIL_MAX = 0.05` on the share of irrigated villages whose source breakdown does not
reconcile with the reported total to within 1 ha. Maharashtra fails at 99.7%, Nagaland 100%,
Arunachal 70.7%; 25 states are at 0.0%.

**Rule: never compute the diversity measure without gating on reconciliation first.**

### Data quality varies by state far more than §5 implies

One national caveat cannot cover this. Integrity-failure share (union of the §5 substitute
checks), best to worst:

| Band | States |
|---|---|
| ≤1% | Punjab 0.10%, Haryana 0.11%, Kerala 0.2%, Rajasthan 0.8%, Karnataka 0.9%, MP 1.2% |
| 1–5% | Gujarat 1.9%, Maharashtra 1.4%, Uttarakhand 2.6%, Himachal 2.6%, Andhra 3.4%, Chhattisgarh 3.9%, Jharkhand 4.9% |
| 5–20% | Sikkim 6.9%, Tripura 8.0%, Odisha 14.5%, Assam 17.9% |
| >20% | **West Bengal 38.2%**, A&N 56.6%, Manipur 82.0%, Arunachal 92.5%, Nagaland 94.2%, Meghalaya & Mizoram 100% |

**West Bengal is the trap in this list:** it has the second-best spread in the country
(IQR 0.769) and passes cleanly, yet 38.2% of its 37,284 villages — 14,241 — fail an integrity
check. It must ship with more than a third of its map rendered as low-confidence (§9.4), or
not ship.

**Meghalaya and Mizoram have every land-use column at exactly zero for every village.** That
is not a finding about irrigation; the Village Directory land module has no coverage there.
Reporting it as "mechanism absent" would have been wrong, which is why NO DATA is a separate
verdict.

### Geometry join — pilot states, 11 September 2026

Source: `data/raw/shrid2_open.gpkg`, layer `shrid2`, 595,438 polygons, EPSG:4326 (read,
logged and asserted — no transform needed, §8). Script: `scripts/build_tiles.py`.

**Towns are identifiable, and are RETAINED AND TAGGED — not dropped.** The polygon layer
covers villages *and* towns; the Village Directory covers villages only. **Towns carry a
`pc11_id` in the `8xxxxx` series, and that series is completely disjoint from the Village
Directory** — measured on the three pilot states, 673 of the 1,030 unmatched polygons carry
the prefix and **zero of the 109,319 matched polygons do**.

PMFBY insurance units are villages (§4), so towns carry no index and are excluded from every
statistic. But an earlier version **deleted** them, which was wrong for a different reason:
it left holes in the map. The blank at the centre of the default view was Bhopal, and it read
as missing data. **Two different absences must not render the same way** (§9.4) — so towns are
now tagged `is_town`, rendered in a neutral non-ramp fill, given their own legend entry
("town, outside scheme unit") and their own click message. Present, identified, excluded from
the numbers.

Join audit for Rajasthan + Gujarat + Madhya Pradesh:

| | |
|---|---|
| Polygons read (pushdown filter) | 110,349 |
| Town polygons excluded (`pc11_id` 8xxxxx) | 673 — RJ 169, MP 336, GJ 168 |
| Village polygons retained | 109,676 |
| Index rows, pilot states | 109,347 |
| Matched keys | 109,319 |
| Geometry with no index row | 357 (0.33%) |
| Index row with no geometry | 28 (0.03%) |
| **Joined with an index value** | **108,025 (98.49%)** |
| Joined without one → no-data | 1,651 (357 unmatched + 1,294 with no computable c1) |

Zero null or invalid geometries. Merge run with `validate="1:1"`.

### Published series — all 35 states, 11 September 2026

`data/processed/basis_risk_index.parquet`, 588,973 villages keyed on `shrid2`, every row
carrying `state_verdict`, `in_v1_coverage` and `index_status`. Required by the licence (§9.7).

| `index_status` | Villages | Share |
|---|---|---|
| `issued` | 513,273 | 87.15% |
| `withheld_source_data_unreliable` | 40,568 | 6.89% |
| `no_mechanism_component` | 28,949 | 4.92% |
| `no_land_use_data` | 6,183 | 1.05% |

| State verdict | Villages | Index issued |
|---|---|---|
| PASSES | 442,918 | 97.66% |
| FAILS | 47,472 | 85.48% |
| SOURCE DATA UNRELIABLE | 45,427 | **0%** — c2 withheld by the reconciliation gate |
| MECHANISM ABSENT | 40,183 | 83.01% |
| MECHANISM WEAK | 6,610 | 99.88% |
| NO DATA | 6,183 | **0%** |

**An index value is published where it is computable; the verdict travels with it.** States
failing the reconciliation gate get **no index at all** rather than a number with a caveat —
c2 there is computed on an incomplete denominator and is withheld outright. The
**subdivision flag is issued only inside v1 coverage**, because a flag is a recommendation
rather than a measurement.

### Overview layer — subdistrict, z4–7

GDAL's MVT writer simplifies but does not **drop** features, so a single village layer put
all 110,349 polygons into every overview tile: **563 KB at z6**, past the practical limit.
§8 anticipated this.

Fixed by dissolving villages to subdistrict — derivable from `shrid2` field [3] with no extra
download — with an **area-weighted** mean index, weights computed in EPSG:6933 and towns and
unindexed villages excluded from both weight and mean. 810 subdistricts across the three
pilot states.

| Archive | Zooms | Features | Size |
|---|---|---|---|
| `pilot_overview.pmtiles` | 4–7 | 810 | **0.9 MB** |
| `pilot_villages.pmtiles` | 8–12 | 110,349 | 115.3 MB |

### Viewer — Gujarat, 11 September 2026

The three-state map was **visual noise**: 110k polygons with no reference geography, equal
intervals that flattened the distribution, and subdivision casings that formed white webs.
Five changes, all measured:

1. **Scope cut to Gujarat alone.** Village archive **115.3 MB → 31.3 MB**, overview
   0.9 → 0.3 MB. 18,114 polygons, 17,946 villages, 168 towns, 224 subdistricts, 26 districts.
2. **Reference geography is mandatory, not decoration.** Without district boundaries, district
   labels and a state outline the choropleth cannot be used to look anything up. Sourced from
   **PC11 District Polygons and PC11 State Polygons** — the names ship with the geometry, so
   labels and the district table cannot disagree. Dissolving village polygons (the first
   attempt) was wrong twice: village edges are not perfectly coincident, so it produced
   sliver artefacts, and it came to 2.8 MB of GeoJSON that delayed first paint. The admin
   layers are **284 KB + 79 KB**.
3. **Casing is off below z11.** At z9 the outlines on ~20% of villages formed webs that
   dominated the frame. The flag is a lookup aid, and lookup happens zoomed in.
4. **Class breaks are within-Gujarat quintiles, computed from the data** and written to
   `web/data/gj_meta.json` — not equal intervals hardcoded in the viewer (§9.2). Breaks:
   **0.149 / 0.339 / 0.491 / 0.581**. Equal intervals on 0–1 put **8,430 of 17,454 villages
   (48%) into two classes**, which is what flattened the map.
5. **Default view is the whole state on the subdistrict layer**, fitted to the state extent
   (≈z7.4). Zoomed out is for pattern, zoomed in is for lookup — one zoom should not do both.

### THE CENTRAL FINDING — structural index built on 2011 data converges with the 2026 season

**This is the headline. Lead with it.**

The index uses **no weather input of any kind**. It is built entirely on 2011 Census land-use
structure: the irrigated/unirrigated split, source mix and unit size. It has never seen a
rainfall figure.

Then IMD's district rainfall for the 2026 kharif season, cumulative 01-06-2026 to 11-09-2026:

| | Structural index (2011 data) | 2026 season departure | IMD category |
|---|---|---|---|
| **Jamnagar** | **0.575** — highest in Gujarat | **−61%** | Large deficient |
| **Rajkot** | **0.510** — 2nd highest | **−36%** | Deficient |
| … | | | |
| **Valsad** | **0.289** — 2nd lowest | **+44%** | Excess |
| **The Dangs** | **0.124** — lowest in Gujarat | **+15%** | Normal |

**The two districts the index ranks most structurally vulnerable are in severe deficit this
season. The two it ranks least vulnerable are in surplus.** A 2011 structural measure with no
weather term has landed on the districts where the averaging mechanism is about to be tested.

Why this is not a coincidence and not circular: the mechanism is the same in both directions.
Semi-arid mixed-irrigation tracts in Saurashtra are *both* internally heterogeneous (some
farms on wells, some rainfed — the §7 mechanism) *and* exposed to monsoon failure. The
eastern tribal belt is uniformly rainfed, therefore internally homogeneous, and this year wet.
The index did not predict the rainfall; it identified the places where irrigation access is
unevenly distributed, and those are the same places where an uneven monsoon has uneven
consequences.

**What it is not:** not a claim forecast. Claim outcomes depend on insurance-unit yields,
which §5 establishes are not public. The product is a **watchlist**: units where the
averaging mechanism is most likely to be tested this season. Say it that way.

### Season overlay — IMD district rainfall, built 11 September 2026

Source: `mausam.imd.gov.in/Rainfall/DISTRICT_RAINFALL_DISTRIBUTION_COUNTRY_INDIA_cd.pdf`,
IMD Hydromet Division — stable URL, no session token, refreshed daily, parses with
`pdfplumber`. Scripts: `scripts/fetch_imd.py`, `scripts/build_rainfall_layer.py`.
Provenance (fetch timestamp, PDF's own DAY and PERIOD, SHA-256) is stored in
`data/processed/imd_provenance.json` **and shown in the UI** — a rainfall figure without a
date is useless and a departure without a period is misleading.

**Gujarat is at −20%, not −14%.** An earlier revision of this brief said ~14% from a
secondary source; IMD is authoritative. Actual 519.5 mm against a normal of 647.0 mm for
01-06 to 11-09-2026 = **−20%, IMD category "Deficient"**.

**And the state average is the least useful number in the table:**

| Unit | Actual | Normal | Departure |
|---|---|---|---|
| Gujarat state | 519.5 mm | 647.0 mm | **−20%** (Deficient) |
| Gujarat Region (mainland) | 796.5 | 850.6 | **−6%** (Normal) |
| Saurashtra & Kutch | 300.6 | 484.4 | **−38%** (Deficient) |

The deficit is almost entirely Saurashtra and Kutch. 33 districts parsed, 0 missing
departures: 14 Normal, 12 Deficient, 4 Large Deficient, 3 Excess. Worst: Devbhoomi Dwarka
−88%, Porbandar −74%, Kutch −62%, Jamnagar −61%.

**Rainfall is a filter over the index, never a term in it.** The deficit is district-level and
the index is village-level; blending them would shift every village in a district by a
constant, which is precisely the error §7 rejected when it excluded district yield CV. It is a
separate layer plus a toggle, drawn in a reserved status colour off the blue ramp.

**The watchlist:** 12 of 26 PC11 districts are in an IMD deficit category; 1,746 Gujarat
villages are top-decile index; **925 villages are both**. By district: Rajkot 191 (−36%),
Jamnagar 181 (−61%), Vadodara 154 (−35%), Sabar Kantha 79 (−47%), Kachchh 72 (−62%),
Mahesana 61 (−30%), Dohad 48 (−48%), Patan 48 (−21%), Junagadh 47 (−26%), Gandhinagar 19
(−33%), Banas Kantha 18 (−32%), Porbandar 7 (−74%).

### The 33 → 26 district crosswalk

`data/reference/gujarat_district_crosswalk_2011_2026.csv` — hand-built, hand-checked, every
row carrying a confidence and a note. Audited four ways in `build_rainfall_layer.py`, all
clean: no IMD district missing from the crosswalk, no crosswalk name absent from the feed, no
PC11 district without a same-name successor, no crosswalk name absent from the geometry.

**All 26 PC11 districts still exist today under the same name or a spelling variant**, so the
primary join is 26 clean 1:1 mappings. Spelling divergences: `MEHSANA`/Mahesana,
`DAHOD`/Dohad, `PANCHMAHAL`/Panch Mahals, `BANASKANTHA`/Banas Kantha,
`SABARKANTHA`/Sabar Kantha, `KUTCH`/Kachchh, `DANGS`/The Dangs.

**Seven districts were created in 2013** and are carried as `seceded`, with their parent(s)
named, so a PC11 district whose seceded half behaves differently is visible rather than
averaged away (`within_pc11_mixed`, 2 districts flagged):

| Post-2011 district | Ceded from | Confidence |
|---|---|---|
| Aravalli | Sabar Kantha | high (single parent) |
| Chhota Udepur | Vadodara | high (single parent) |
| **Devbhoomi Dwarka** | **Jamnagar** | high (single parent) |
| Gir Somnath | Junagadh | high (single parent) |
| Botad | Bhavnagar + Ahmadabad | medium — split share not sourced |
| Mahisagar | Panch Mahals + Kheda | medium — split share not sourced |
| Morbi | Rajkot + Jamnagar + Surendranagar | medium — split share not sourced |

**Area weighting was NOT invented, and this is deliberate.** Correct recombination needs
present-day district boundaries, which SHRUG does not publish, or a sourced taluka
composition for each new district, which we do not have. Writing a plausible-looking weight
would be exactly the §9 failure this project exists to avoid. Instead each PC11 district takes
its same-name successor's figure and the seceded children's departures are carried alongside.

**The headline survives this limitation, and it is worth checking why.** Devbhoomi Dwarka
(−88%) seceded from PC11 Jamnagar, our highest-index district — so PC11 Jamnagar's true
departure lies between its residual's −61% and the seceded portion's −88%. Both are in the
Large Deficient band, so the direction is unambiguous whichever weighting is used. Morbi
(−2%, Normal) also draws a small share from PC11 Jamnagar, which is why Jamnagar is flagged
`within_pc11_mixed` — stated rather than hidden.

### Construct validation — the index is not a rainfed-share proxy

§10.1 asks whether the index behaves as the §4 mechanism predicts. Gujarat's 26 districts
answer it, and the answer is the strongest evidence the project has produced:

| | Spearman vs district mean index |
|---|---|
| Mean rainfed share | **−0.490** |
| **Distance from an even split, \|rainfed − 0.5\|** | **−0.785** (Pearson −0.840) |

**The index tracks mixedness, not dryness** — which is what `c1 = 4p(1−p)` is built to do.

- Highest index: **Jamnagar 0.575** (rainfed 0.594), **Rajkot 0.510** (0.639),
  **Navsari 0.507** (0.512), **Junagadh 0.487** (0.491) — all near an even split.
- Lowest index: **The Dangs 0.124** with the state's *highest* rainfed share (0.971), and
  **Anand 0.237** with the state's *lowest* (0.079).

The Dangs and Anand are opposite extremes of irrigation and both score low, because both are
internally uniform. **The interview line: the highest basis risk is not the driest district,
it is the most mixed one.** A district where every farm is rainfed shares a threshold yield
that fits every farm.

### Known geography check — Gujarat district rainfed share

| Expectation | Result |
|---|---|
| Low in the Narmada canal command | **Confirmed where the command is** — Anand 7.9% (lowest in state), Kheda 43.1%, Vadodara 46.6%. **But Narmada district is 78.2% and Bharuch 72.2%.** Not a failure: Narmada *district* is the tribal upland around the dam, outside the command, which runs north and west. Do not confuse the district with the command. |
| Low in the North Gujarat tubewell belt | **Confirmed** — Mahesana 26.6% (2nd lowest), Gandhinagar 32.3%, Sabar Kantha 41.9%, Banas Kantha 44.9%. Exception: **Patan 67.5%**, consistent with its arid north-western tehsils toward the Little Rann. |
| High across Saurashtra and Kutch | **Confirmed** — Kachchh 81.4%; Amreli 73.3%, Porbandar 71.1%, Surendranagar 68.1%, Rajkot 63.9%, Jamnagar 59.4%, Bhavnagar 59.2%. Exception: **Junagadh 49.1%**, consistent with better groundwater in the Girnar/coastal south. |
| Eastern tribal belt | Not asked, and the sharpest confirmation: **The Dangs 97.1%**, Dohad 83.5%, Valsad 83.3%, Panch Mahals 78.2%. |

Three of three expectations hold, with two explicable exceptions and one naming trap. Full
table: `data/processed/gj_district_rainfed.csv`.

### v1 coverage decision

**v1 covers the 18 PASSES states — 442,918 villages, 75.2% of India.** The other 17 states
and UTs render as explicit non-coverage **with their specific reason shown** (fails /
mechanism absent / source data unreliable / no data / too few rows), never as blank space or
as a low score. A village-level product that silently omits 25% of the country is the §9
problem in map form.

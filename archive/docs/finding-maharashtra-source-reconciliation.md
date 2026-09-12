# Finding: Maharashtra's irrigation-source columns do not reconcile, and the gap produces a spuriously perfect heterogeneity score

**Dataset:** SHRUG 2.2 "Pakora", `pc11_vd_clean_shrid.dta` (2011 Population Census Village
Directory, shrid2 level, 588,973 rows).
**Measured:** 11 September 2026.
**Reproduce:** `python scripts/sweep_states.py` — see `recon_fail_share` in
`data/interim/state_sweep.csv`.

---

## Summary

For Maharashtra, the Village Directory's **aggregate** irrigated-area column
(`pc11_vd_land_src_irr`) and the **five source-breakdown columns** that should decompose it
disagree by roughly a factor of four. Because a source-mix diversity measure is necessarily
built on the breakdown columns rather than the aggregate, Maharashtra returns the *lowest*
single-category share of any state in India — which reads as "the most internally
heterogeneous state in the country" and is in fact an artefact of absent data.

The state is India's fourth-largest by village count (40,810). Had the discrepancy not been
caught, it would have been the single strongest piece of evidence for the index.

## The numbers

| Quantity | Value |
|---|---|
| Reported total irrigated area (`pc11_vd_land_src_irr`, summed) | **15,120,923 ha** |
| Sum of the five source columns | **3,711,391 ha** |
| Ratio | **4.07 ×** |
| Villages with `src_irr > 0` | 39,531 |
| Villages with `src_irr > 0` but **all five source columns zero** | **8,171** (20.7% of irrigated villages) |
| Villages failing reconciliation to within 1 ha | **99.7%** of irrigated villages |

The five columns are `pc11_vd_land_canal_irr`, `pc11_vd_land_wl_tw_irr`,
`pc11_vd_land_tnk_lk_irr`, `pc11_vd_land_w_fall_irr`, `pc11_vd_land_oth_src_irr`.

Per-column non-zero counts for Maharashtra:

| Column | Non-zero villages | Sum (ha) |
|---|---|---|
| `wl_tw_irr` (wells / tube wells) | 27,621 | 2,149,018 |
| `canal_irr` | 7,864 | 851,081 |
| `oth_src_irr` | 6,331 | 395,617 |
| `tnk_lk_irr` (tanks / lakes) | 6,015 | 298,778 |
| `w_fall_irr` (waterfall) | 430 | 16,897 |
| **five-column total** | — | **3,711,391** |
| `src_irr` (reported aggregate) | 39,531 | **15,120,923** |

## Why it produces a *better*-looking score

The heterogeneity measure is a diversity statistic (Shannon entropy, plus a dominance
share) computed over six area categories: the five irrigation sources plus unirrigated area.
A village is "effectively single-category" when one category holds ≥95% of the total.

Where the breakdown is incomplete, the category totals are wrong in a specific direction:

1. Villages with an irrigated total but all five sources zero contribute **no** irrigated
   categories at all, so the measure sees only unirrigated area — or, where unirrigated area
   is also zero, the village drops out as undefined.
2. Villages with a *partial* breakdown have their dominant source truncated. If only
   `wl_tw_irr` and `un_irr` survive at comparable magnitudes, maximum category share lands
   near 0.50 rather than near 1.00.

Effect (2) is visible in the distribution: Maharashtra's 99th percentile of maximum-category
share is **0.5000**. Almost no village can reach the 0.95 dominance cut, so the
single-category share collapses to **0.0062%** — against 15.3% in Madhya Pradesh, 30.0% in
Rajasthan and 48.0% in Jharkhand.

**Low dominance is the signature of a heterogeneous, high-basis-risk state. Maharashtra
manufactures that signature out of missing values.** The failure is silent: no null, no
error, no out-of-range value. Every individual column is internally plausible.

## How it was caught

Not by inspecting Maharashtra. It was caught by a reconciliation check written for a
different purpose — verifying that `src_irr` equals the sum of its five parts — which had
already been run on Madhya Pradesh, where it passed at 100% of villages within 1 ha and
therefore looked like a formality.

Maharashtra was flagged only because the sweep printed a dominance figure of 0.0% that was
*too good*. Investigating an implausibly favourable result, rather than an implausibly
unfavourable one, is what surfaced it.

## Consequence for the pipeline

Reconciliation is now a **verdict gate, not a reported diagnostic**. Any state where more
than 5% of irrigated villages fail to reconcile to within 1 ha is reported as
`SOURCE DATA UNRELIABLE` and receives no spread score at all, because its spread statistics
are computed on an incomplete denominator and can look arbitrarily good.

States failing the gate: **Maharashtra 99.7%**, **Nagaland 100%**, **Arunachal Pradesh
70.7%**. Twenty-five states sit at 0.0%; Uttarakhand (0.3%), Manipur (1.9%), West Bengal and
Uttar Pradesh (0.1%) pass comfortably.

Rule: **never compute the diversity measure without gating on reconciliation first.**

## What this does not establish

- **No cause is identified.** Whether the gap originates in the 2011 Census enumeration, in
  Maharashtra's state land-records reporting, or in SHRUG's cleaning is not determined here.
  It is not attributed to any of them.
- **It is not a claim that the aggregate is correct and the breakdown wrong**, only that they
  are mutually inconsistent at a magnitude that makes the breakdown unusable.
- **It is not a general statement about SHRUG quality.** Twenty-five states reconcile exactly.
  The affected columns are the source breakdown only; Maharashtra's land-use totals,
  net-sown-area and unirrigated-area columns pass every integrity check applied (1.4%
  failure rate, better than the national median).
- **The shipped quality flags do not mark it.** `land_flag` and `dist_flag` are constant `1`
  across the whole file and flag nothing anywhere (see PROJECT_BRIEF.md §5).

Anyone using `pc11_vd_land_*_irr` breakdown columns for Maharashtra, Nagaland or Arunachal
Pradesh should verify against the aggregate before relying on them.

---

*Data: SHRUG 2.2 (Development Data Lab), licensed CC BY-NC-SA 4.0. DDL disclaims all warranty
as to accuracy, adequacy and completeness. This note is an observation about a specific
release of a specific file, measured on the date stated.*

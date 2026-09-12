# Archive

Finished and abandoned work, kept separate from Gujarat Reservoir Watch. Nothing here is
maintained and nothing in the live project depends on it. It is preserved because the
findings are worth having and because two of them are cautionary.

## `ARCHIVE_basis_risk_BRIEF.md` — PMFBY basis-risk decision engine

A village-level index of crop-insurance basis risk across 18 Indian states, built on SHRUG
2.2 census data, published for all 35 states keyed on `shrid2`, with an OpenLayers +
PMTiles viewer for Gujarat.

**Its central finding is the reason it is kept.** The index was built on 2011 census
structure with no weather input, and its two highest-scoring Gujarat districts —
Jamnagar 0.575 and Rajkot 0.510 — were in severe 2026 rainfall deficit while its two lowest
— Dangs 0.124 and Valsad 0.289 — were in surplus. That convergence, between a structural
index and an independent weather signal it never saw, is the result.

**Its cautionary finding** is the construct-validation check: Spearman(|rainfed − 0.5|,
index) = −0.785 against Spearman(rainfed, index) = −0.490. The index tracks *mixedness* of
irrigation, not dryness. Those are not the same thing, and the name did not say so.

`scripts/` holds the kill-check, the 35-state sweep, the index build, the PMTiles build, and
the IMD rainfall fetch and layer. `web/index.html` is the viewer.

**The viewer will not render a map when deployed.** Its PMTiles are ~140 MB, excluded from
git for size, and above GitHub's 100 MB per-file limit. Rebuild them locally with
`scripts/build_tiles.py`; the source SHRUG polygons are a separate download.

## `docs/finding-maharashtra-source-reconciliation.md`

A standalone data-quality note: Maharashtra's irrigation-source columns do not reconcile
against reported hectares, and 8,171 villages report zero irrigation source. The effect was
that Maharashtra scored the *best* source-dominance in the country as an artefact of missing
data. Written up separately from the map because it is a finding about the source, useful to
anyone else using SHRUG irrigation columns.

## `reference/` — abandoned mandi-price verification

`agmarknet_gujarat_markets.csv` and `mandi_geocode_feasibility.csv` are from a Gujarat mandi
price advisor that was scoped and dropped at the verification stage. The make-or-break test
was whether ~200 Gujarat APMC mandis could be geocoded, since without locations there is no
distance and no product. These files record how far that got. Nothing was built.

## Data

None of the source data is here. SHRUG village-level `.dta` and the admin boundary `.gpkg`
come to about 2.4 GB and are excluded; SHRUG in any case requires accepting its own terms at
<https://www.devdatalab.org/shrug> and cannot be redistributed. The brief records which
modules were selected on the download form.

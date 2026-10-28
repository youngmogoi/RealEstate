# NC Land Intelligence

## Purpose
Score every parcel in Mecklenburg County, North Carolina weekly to find land for
(1) my own single-family spec homes and (2) townhome and subdivision
developers in my network. Output: a ranked list with a recommended action
per parcel (Build / Route to partner / Entitle candidate / Skip).

## Stack
- Python 3.12, uv for packages, pytest for tests
- PostgreSQL 16 + PostGIS in Docker (docker-compose.yml)
- GeoPandas, Shapely, Rasterio, SQLAlchemy
- All geometry stored in EPSG:2264 (NC State Plane, US feet)

## Layout
- `ingest/` one module per data source, each idempotent
- `features/` spatial joins that build parcel_features
- `scoring/` yield, residual land value, routing
- `intel/` LLM tools: minutes watcher, ordinance Q&A, seller signals
- `ops/` digest, deal sheets, outreach, CRM
- `config/` YAML I edit: costs, prices, zoning rules, partners
- `data/raw/` downloads (gitignored)
- `tests/` pytest, with a small fixture of real parcels

## Rules
- Never hard-code costs, prices, margins or zoning rules; read from config/.
- Every ingest keeps dated snapshots; never overwrite history.
- Every number shown to me must be traceable to its source row.
- If a data source, field meaning or zoning rule is unclear, stop and ask me.
- Write tests for every scoring function using hand-checked parcels.
- Secrets in .env only (never committed).

## Domain notes
- Sewer distance drives value more than anything; treat it carefully.
- County GIS acreage and zoning can be wrong; flag outliers, don't trust them.
- Subdivision net usable land = 65-75% of net acres (config value).
- Acreage mismatch rule (decided 2026-10-27, config/scoring.yaml): parcels whose GIS and
  assessor acres differ by >20% score on the smaller figure and are marked "verify
  acreage". Parcels the larger figure would push into the top picks go on a weekly
  "check acreage" list. Record each manual check as a correction.
- Unknown sewer (decided 2026-10-27, for scoring): inside a municipal limit, assume served and
  add a "verify sewer" flag; outside, score without public sewer and also report an
  "if sewer available" value. Low-confidence "served" is a risk, not a fact.
- Water status uses the sewer proxies for now; split it when water-specific data exists.
- Phase 2.2 (decided 2026-10-27): single-family minimum lot size and lot width checks use
  GROSS lot area, because zoning measures the whole lot. Net buildable acres apply only to
  subdivision and townhome yield.
- Phase 2.4 deal routing (decided 2026-10-28): exclude government, utility, church/nonprofit,
  HOA and common-area parcels from routing. Keep them in the data with their flags
  (owner_type, land use, access_via_common_area), but never score them as opportunities.
- Floodplain flag (decided 2026-10-27): 1% annual chance only (the community layer's 0.2%
  shapes are dropped), and open water (NWI lakes and ponds) is subtracted, since the FEMA
  layer has no zone codes and 40% of it lies over lakes.

## Weekly refresh
- `make weekly` reloads every source, rebuilds parcel_features, writes changes, and logs to
  logs/weekly_<date>_<time>.log (first line = STATUS; failures name the step and error).
  Takes ~20 min. County layers are manual: `make refresh-manual` lists them with their ages;
  the log warns when one is over 90 days old.
- Disk check (config `weekly` in features.yaml): under 5 GB free on C: puts "DISK WARNING" on
  line 2 of the log and shows a Windows notification; under 2.5 GB the run is skipped
  (STATUS: FAILED at step 'disk space'). Fix: clear Downloads/Recycle Bin or compact Docker's
  disk image (it grows but never shrinks).
- Scheduled with Windows Task Scheduler (this PC is Windows; cron/launchd don't apply), Sundays
  22:00, via scripts/weekly.cmd (starts Docker Desktop if needed). The PC must be on and you
  logged in at that time.
  - install (works in PowerShell and cmd; the path has no spaces, so no inner quotes):
    `schtasks /Create /TN "NC Land Weekly" /TR "C:\Users\josko\OneDrive\Documents\RealEstate\scripts\weekly.cmd" /SC WEEKLY /D SUN /ST 22:00 /F`
  - check: `schtasks /Query /TN "NC Land Weekly" /V /FO LIST`
  - run now: `schtasks /Run /TN "NC Land Weekly"`
  - remove: `schtasks /Delete /TN "NC Land Weekly" /F`

## Lessons learned
(append corrections here as we go)
- 2026-10-26: NC OneMap's Mecklenburg parcels lag the county by months (the Jan 2026 file
  was still current in Oct). Use the county's own Open Mapping downloads
  (gis.mecknc.gov/data-center): Tax Parcel Boundaries, Ownership Data, Sales. NC OneMap is
  kept only as the 2026-01-07 snapshot in `nconemap_parcels_snapshots`.
- NC OneMap left site address blank on many new builds and wrote CIR as CR. The county file
  has addresses for 99%+ of parcels.
- County `num_totalac` is in the unit named by `txt_landunittype`: acres for ACRE, square
  feet for SQUARE FEET, a lot count (1) for LOT. Only ACRE and SQUARE FEET give acres.
- Condos are one parcel shape with many owner accounts sharing its PIN. Score land on
  `parcels` (one row per shape), never on `parcel_owners`.
- The county sales file repeats ~73k rows exactly; drop them. Recent sales have a blank
  validity code until the assessor reviews them.
- Biggest GIS-vs-assessor acreage gaps are submerged land, government and mining tracts;
  check these before trusting either figure.
- 2026-10-27: Charlotte Water does not publish sewer/water line GIS (checked county portal, city
  map server). Sewer status is estimated (ingest/utilities.py) and carries a confidence.
  Any Charlotte Water file in data/raw/utilities/ is under a security agreement: use it only
  inside scoring, never on deal sheets, maps, or anything shared.
- Charlotte Water capital projects are in the city CIP "View of Polygons" layer (MapServer/23),
  not the lines layer, which holds only street and bike projects.
- TODO (Phase 3): sewer source 2, Charlotte Water comments in rezoning staff analyses, is
  deferred to share the minutes watcher's scraper and extractor. Findings from 2026-10-27:
  the city's Legistar API (webapi.legistar.com/v1/charlottenc) lists "Zoning Hearing"
  matters (~500 in 3 years) whose attachments include a pre-hearing staff analysis ("PHSA");
  its "Charlotte Water:" bullet gives main type, size and location. The PDFs list no parcel
  IDs, so match petitions to parcels with the city's PLN/Rezonings and PLN/RezoningHistory
  polygon layers. The city web pages build links with JavaScript; use the API, not the site.
- Sewer proxies: plat_first_home (first home on the plat built after 1980) and
  plat_age_fallback (no homes yet, plat book 20-89, inside a town limit, median lot < 0.5 ac).
  Plat book numbers run in date order up to ~80; 90+ is an older series.
- Charlotte's zoning layer lags rezonings by ~4 months (last rezoning June 2026 in the Oct
  download); the county's "Parcels with zoning" list is newer.
- 2026-10-27: the county's map server (meckags.mecklenburgcountync.gov) now answers 403 to
  scripts. Download county layers by hand from Open Mapping into data/raw/; don't work around it.
- The county portal's "Wetlands" layer is the National Wetlands Inventory (USFWS), not
  county-delineated wetlands, so it is a flag only (possible_unmapped_wetland_acres).
- USGS 3DEP elevation (exportImage) returns meters and errors on tiles above ~2,000 px;
  the slope band raster is cached in data/raw/constraints/derived/.
- 2026-10-27: street centerlines come from the city's map server (CountyData/Streets), which
  accepts scripts. Lot lines sit 25-50 ft from local centerlines and farther on cul-de-sac bulbs
  and arterials, so frontage reaches 65 ft (local) / 90 ft (major thoroughfares) and counts only
  lot-line segments roughly parallel to the street (side lines would add ~40 ft). Two frontages
  that meet = corner lot; on opposite sides = through lot.
- 2026-10-28 landlocked review (25-parcel sample): most "landlocked" lots fronted streets the
  city still marks Proposed (built, status lagging), were townhomes on private drives, or sat
  beyond a 50 ft reach. Proposed streets now count (frontage_proposed_only flags them);
  townhome/condo/common parcels with no frontage are access_via_common_area. 32,578 -> 8,155.
- Owner names rarely say "estate" in the county file (~170 parcels); "C/O" and "%" in-care-of
  names live in the second owner field (18k parcels, mostly investor portfolios' agents).
  Real estate detection needs probate data (Phase 3).
- Geometry unions and float sums in parallel queries vary in the last bits run to run, which
  breaks "rerun = zero changes". Every ST_Collect/sum feeding a stored value needs an ORDER BY.
- There is no ETJ layer; etj_jurisdiction = zoning jurisdiction of an unincorporated parcel.
  Year built is from the NC OneMap 2026-01-07 snapshot (the county files have none); heated
  square feet has no source until "Tax Parcel CAMA Data" is downloaded.
- This PC has 7 GB RAM and a small, nearly full C: drive. Load big files in chunks, and
  don't let Docker's disk fill up: it corrupted Postgres once.

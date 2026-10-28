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
- Zoning rules (Phase 2.1, decided 2026-10-28; config/zoning_rules.yaml, check with
  `make zoning-rules`, ordinances via `make udo` into data/raw/udo/ with manifest.csv):
  townhome = UDO "Multi-Family Attached Unit". Yield is stored per housing type (sfh, duplex,
  triplex, quadraplex), each only on lots meeting its own requirements; RLV picks the best.
  Townhome rezoning yield uses the 2040 Policy Map place type (Neighborhood 2 -> N2-A,
  Neighborhood 1 -> N1-F, else none) and is speculative. Site-plan districts (CD, MX-1/2/3,
  legacy conditional): by-right yield null, flag site_plan_governs, petition number kept.
- Yield (Phase 2.2, `make yield`, scoring/yield.py -> parcel_yield; import it with
  importlib since `yield` is a keyword): every option stored, no winner. Per type on the lot as
  is (gross area, lot width), lot splits, subdivision (net buildable x usable_ratio / min lot)
  plus conservation/compact/mixed-income with obligations, townhome by right or by place-type
  rezoning (speculative), ADU yes/no. Draft rules are used too; rule_status is stored. Charlotte
  districts below the 2026-10-28 cutoff are `unsupported` (no_zoning_rule); codes with (CD) etc.
  are site_plan_governs. Arterial = thoroughfare classes in scoring.yaml (collectors excluded);
  setback_varies approximates an established blockface by developed parcels within 300 ft.
  Lot splits use the smaller of frontage and lot width (decided 2026-10-28).
- Buildable pieces (decided 2026-10-28, `make pieces`, features/pieces.py -> parcel_pieces):
  land outside the vector unbuildable layers (stream/lake buffers, floodway, encroachment) is
  split into pieces (slivers < 500 sf dropped); slope is measured per piece. A piece is
  reachable if it touches any street (frontage reach) or a same-owner neighbor
  (assemblage_access). Only pieces meeting the district min lot get yield. Subdivision and
  townhome yield use reachable pieces; cut-off land -> cutoff_acres, creek_divides or
  constraint_divides, and if_crossed_* yields. RLV adds a crossing cost (config) and values
  if_crossed, never as the by-right best use. Lot as it is and splits stay on gross area.
- RLV (Phase 2.3, built 2026-10-28, `make rlv`, scoring/rlv.py, config/economics.yaml): comps =
  arm's-length (blank validity code) new-construction sales (sold 0-2 years after year built, one
  building, CAMA heated sf), median $/sf within 1 -> 2 -> 3 mi until 5 comps; products sfh,
  townhome, plex (duplex/triplex/quadraplex pooled: CAMA can't tell them apart, 11 sales).
  Every option's inputs are stored in parcel_rlv_options; `python -m scoring.rlv --explain <pin>`.
  Blank costs are left out and flagged (blank_inputs, rlv_incomplete), never 0; while the three
  core costs are blank the run builds comps and ratios only. Price = assessed x median sale/assessed
  ratio by municipality and vacant/improved (vacant = building value 0), 20+ sales or broader.
  Affordable units: capped sale price (affordable_max_price). ADU: flat premium on the main unit.
- For RLV (Phase 2.3): an ADU adds value to the principal unit, never a sale unit; quadraplex
  and mixed-income options carry affordability obligations stored with the yield;
  conservation, compact and mixed-income are separate yield options; flag duplex/triplex
  values when comps are thin.
- OPEN QUESTION (2026-10-28, you are asking Charlotte zoning staff in writing): does a
  multi-dwelling development (UDO 15.4.HHH, several principal buildings on one lot) need the
  district minimum lot area per building? Until answered it is excluded from yield and only
  flagged (multi_dwelling_possible).
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
- 2026-10-28: the 2040 Policy Map (place types) is not on the city map server; it is the
  city's ArcGIS Online layer Charlotte_Future_2040_Policy_Map (services.arcgis.com/9Nl857LBlQVyzq54),
  found via the official viewer's web map. Its short code disagrees with the full name on 8
  shapes and 2 shapes have no geometry; the full name is used (`make place-types`).
- A column added to a versioned table is filled in place on unchanged rows (no history churn).
  Even so, one day of reloads (zoning, place types, features) grew Docker's disk image from
  5.2 to 7.3 GB while the database is 2.2 GB: the image never shrinks on its own.
- This PC has 7 GB RAM and a small, nearly full C: drive. Load big files in chunks, and
  don't let Docker's disk fill up: it corrupted Postgres once.
- 2026-10-28: a comps query joining 206k parcels to comps within 3 mi (80M pairs) sorted on disk
  and filled C: (Docker crashed; Postgres recovered). Comps are now matched in Python (shapely
  STRtree, batches) and the database has temp_file_limit = 3GB. Before a new big spatial join,
  count its pairs. Freeing Docker's disk: `wsl -d docker-desktop -u root fstrim -av` first, then
  stop Docker, `wsl --shutdown`, and diskpart `compact vdisk` (admin); without the trim, compact
  frees nothing. 12.0 -> 5.9 GB that day.
- CAMA (Tax Parcel CAMA Data, `make cama` -> parcel_cama): one row per building; heatedarea is
  account-level (the houses' total, but not on apartment complexes). vacorimprov "VAC" sits on
  ~7,800 accounts with a building: don't use it for vacant. resunits is 0/blank on most plexes.

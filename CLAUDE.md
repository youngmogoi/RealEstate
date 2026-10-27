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
- This PC has 7 GB RAM and a small, nearly full C: drive. Load big files in chunks, and
  don't let Docker's disk fill up: it corrupted Postgres once.

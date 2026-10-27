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

## Lessons learned
(append corrections here as we go)

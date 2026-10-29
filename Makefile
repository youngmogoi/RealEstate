.PHONY: up down test ingest cama addresses ingest-nconemap zoning utilities constraints streets place-types features pieces changes weekly refresh-manual udo zoning-rules yield rlv route score digest

.env:
	cp .env.example .env

up: .env
	docker compose up -d --wait

down:
	docker compose down

test:
	uv run pytest -v

ingest:
	uv run python -m ingest.county_parcels

cama:
	uv run python -m ingest.cama

addresses:
	uv run python -m ingest.addresses

ingest-nconemap:
	uv run python -m ingest.nconemap_parcels

utilities:
	uv run python -m ingest.utilities

constraints:
	uv run python -m ingest.constraints

zoning:
	uv run python -m ingest.zoning

place-types:
	uv run python -m ingest.place_types

streets:
	uv run python -m ingest.streets

features:
	uv run python -m features.build

pieces:
	uv run python -m features.pieces

changes:
	uv run python -m features.changes

weekly: up
	uv run python -m ops.weekly

refresh-manual:
	uv run python -m ops.manual_refresh

udo:
	uv run python -m ingest.udo

zoning-rules:
	uv run python -m scoring.zoning_rules

yield:
	uv run python -m scoring.yield

rlv:
	uv run python -m scoring.rlv

route:
	uv run python -m scoring.route

score: route

digest:
	@echo "digest: not implemented yet" && exit 1

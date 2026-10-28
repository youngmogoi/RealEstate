.PHONY: up down test ingest ingest-nconemap zoning utilities constraints streets features changes weekly refresh-manual udo zoning-rules score digest

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

ingest-nconemap:
	uv run python -m ingest.nconemap_parcels

utilities:
	uv run python -m ingest.utilities

constraints:
	uv run python -m ingest.constraints

zoning:
	uv run python -m ingest.zoning

streets:
	uv run python -m ingest.streets

features:
	uv run python -m features.build

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

score:
	@echo "score: not implemented yet" && exit 1

digest:
	@echo "digest: not implemented yet" && exit 1

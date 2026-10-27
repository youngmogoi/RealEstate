.PHONY: up down test ingest ingest-nconemap zoning utilities constraints score digest

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

score:
	@echo "score: not implemented yet" && exit 1

digest:
	@echo "digest: not implemented yet" && exit 1

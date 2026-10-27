.PHONY: up down test ingest ingest-nconemap score digest

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

score:
	@echo "score: not implemented yet" && exit 1

digest:
	@echo "digest: not implemented yet" && exit 1

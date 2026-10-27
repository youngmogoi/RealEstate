.PHONY: up down test ingest score digest

.env:
	cp .env.example .env

up: .env
	docker compose up -d --wait

down:
	docker compose down

test:
	uv run pytest -v

ingest:
	@echo "ingest: not implemented yet" && exit 1

score:
	@echo "score: not implemented yet" && exit 1

digest:
	@echo "digest: not implemented yet" && exit 1

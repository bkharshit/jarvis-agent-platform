.PHONY: install test test-db test-all lint typecheck format clean \
	gen-api web-install web web-test web-lint web-e2e

install:
	uv sync --extra dev

# Regenerate the committed OpenAPI snapshot + typed client schema whenever
# src/jarvis/api/schemas.py or a route changes (plan: backend is the schema
# authority).
gen-api:
	uv run python scripts/gen_openapi.py
	cd web && npx openapi-typescript src/api/openapi.json -o src/api/schema.d.ts

# --- frontend (web/, stage F1) ------------------------------------------

web-install:
	cd web && npm install

web:
	cd web && npm run dev

web-test:
	cd web && npm run typecheck && npm run lint && npm run test

web-lint:
	cd web && npm run typecheck && npm run lint

# E2e smoke against a running backend (prerequisite: uv run jarvis serve
# with local Postgres — no Docker on this machine). Vite is started by
# Playwright's webServer.
web-e2e:
	cd web && npx playwright test

test:
	uv run pytest tests/unit

test-db:
	uv run docker compose up -d postgres
	uv run alembic upgrade head
	uv run pytest -m db

test-all:
	$(MAKE) test
	$(MAKE) test-db

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck:
	uv run mypy src

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
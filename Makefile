.PHONY: install test test-db test-all lint typecheck format clean

install:
	uv sync --extra dev

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
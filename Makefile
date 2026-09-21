.PHONY: setup test lint sim run

setup:
	uv sync --extra dev
	pre-commit install

test:
	uv run pytest tests/ -v

lint:
	uv run ruff check src/
	uv run mypy src/radio/core src/radio/grid || true

sim:
	uv run radio simulate --hours 24 --seed 1

run:
	uv run radio station

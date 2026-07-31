.PHONY: test lint typecheck validate-fixtures check

test:
	uv run python -m pytest

lint:
	uv run python -m ruff check .

typecheck:
	uv run python -m mypy src

validate-fixtures:
	@find evals/fixtures -name backlog.yaml -exec uv run planning validate {} \;

check: lint typecheck test validate-fixtures

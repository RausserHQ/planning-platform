.PHONY: test lint typecheck validate-fixtures check

test:
	python -m pytest

lint:
	python -m ruff check .

typecheck:
	python -m mypy src

validate-fixtures:
	@find evals/fixtures -name backlog.yaml -print0 | xargs -0 -n1 planning validate

check: lint typecheck test validate-fixtures


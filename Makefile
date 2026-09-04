.PHONY: install format lint test check

install:
	python -m pip install -e ".[dev]"

format:
	ruff check --fix .
	ruff format .

lint:
	ruff check .
	ruff format --check .

test:
	pytest -q

check: lint test

.PHONY: install format lint test check package macos-app macos-package

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

package:
	python -m build

macos-app:
	./scripts/build_macos_app.sh

macos-package:
	./scripts/package_macos_app.sh

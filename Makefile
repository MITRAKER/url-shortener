# Shortcuts for everyday tasks. Run `make help` to list them.
.DEFAULT_GOAL := help
.PHONY: help install lock format lint typecheck power test audit check up down

help: ## List the available commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  make %-10s %s\n", $$1, $$2}'

install: ## Install the locked dependencies and the git pre-commit hook
	pip install --require-hashes -r requirements-dev.txt
	pre-commit install

lock: ## Rebuild the lock files after editing requirements.in or requirements-dev.in
	pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras --output-file=requirements.txt requirements.in
	pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras --output-file=requirements-dev.txt requirements-dev.in

format: ## Auto-format and auto-fix what can be fixed
	ruff check --fix .
	ruff format .

lint: ## Lint and check formatting
	ruff check .
	ruff format --check .

typecheck: ## Strict static type check
	mypy

power: ## NASA/JPL Power of Ten checks
	python scripts/check_power_of_ten.py app scripts tests

test: ## Run the tests with coverage (fails below 95%)
	pytest --cov

audit: ## Look for known vulnerabilities in our dependencies
	pip-audit --strict --disable-pip -r requirements.txt -r requirements-dev.txt

check: ## What CI checks, except Docker and the PostgreSQL test run
	pre-commit run --all-files
	pytest --cov
	$(MAKE) --no-print-directory audit

up: ## Start the app with PostgreSQL in Docker
	docker compose up --build

down: ## Stop the Docker containers
	docker compose down

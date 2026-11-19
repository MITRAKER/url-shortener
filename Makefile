# Shortcuts for everyday tasks. Run `make help` to list them.
.DEFAULT_GOAL := help
.PHONY: help install lock format lint typecheck power test audit check migrate worker up down \
	loadtest monitoring-check

help: ## List the available commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  make %-16s %s\n", $$1, $$2}'

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

migrate: ## Bring the database (DATABASE_URL) up to the latest migration
	alembic upgrade head

worker: ## Count the clicks waiting in Redis (needs DATABASE_URL and REDIS_URL)
	python -m app.worker

up: ## Start everything in Docker: Nginx, 3 app instances, the worker, databases, monitoring
	docker compose up --build

down: ## Stop the Docker containers
	docker compose down

# The load test's settings, which you can override: make loadtest RATE=500 DURATION=2m
RATE ?= 200
DURATION ?= 1m
LINKS ?= 100
export RATE DURATION LINKS

loadtest: ## Load-test the Docker stack with k6, rate limits raised (see loadtest/redirects.js)
	docker compose -f docker-compose.yml -f loadtest/compose.yml up --build --detach --wait
	docker compose -f docker-compose.yml -f loadtest/compose.yml run --rm --no-deps k6

monitoring-check: ## Check the Prometheus configuration and run the alerts' tests (needs Docker)
	docker compose run --rm --no-deps --entrypoint promtool prometheus \
		check config /etc/prometheus/prometheus.yml
	docker compose run --rm --no-deps --entrypoint promtool --workdir /etc/prometheus prometheus \
		test rules alerts_test.yml

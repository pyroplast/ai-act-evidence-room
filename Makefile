.DEFAULT_GOAL := help
SHELL := /bin/bash

PYTHON ?= python3.11
VENV   ?= .venv
BIN    := $(VENV)/bin
COMPOSE ?= docker compose
DB_PORT ?= 5433
TEST_DATABASE_URL ?= postgresql+psycopg://evidence:evidence@localhost:$(DB_PORT)/evidence_room

.PHONY: help venv up down logs test lint fmt demo bench clean

help: ## list targets
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-8s %s\n", $$1, $$2}'

$(BIN)/activate: pyproject.toml constraints.txt
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -c constraints.txt -e '.[dev]'
	@touch $@

venv: $(BIN)/activate ## create the local virtualenv with dev tools

up: ## build and start db, app and worker; waits until healthy
	$(COMPOSE) up -d --build --wait db app worker
	@echo "app: http://localhost:$${APP_PORT:-8000}/healthz"

down: ## stop everything (keeps volumes)
	$(COMPOSE) --profile demo down

logs: ## follow app and worker logs
	$(COMPOSE) logs -f app worker

test: venv ## run the test suite against the compose Postgres
	$(COMPOSE) up -d --wait db
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(BIN)/pytest

lint: venv ## ruff lint + format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

fmt: venv ## apply ruff formatting and safe fixes
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

demo: ## full stack + demo services (seeding and scan rounds land in M6)
	$(COMPOSE) --profile demo up -d --build --wait
	@echo "app:      http://localhost:$${APP_PORT:-8000}/"
	@echo "fixtures: http://localhost:$${FIXTURES_PORT:-8081}/sites/northwind-good/"
	@echo "sink:     http://localhost:$${SINK_PORT:-8082}/deliveries"

bench: ## measure and write docs/benchmarks.md (lands in M6)
	@echo "make bench is implemented in milestone M6; no numbers are produced before then." >&2
	@exit 2

clean: ## remove local caches (not docker volumes)
	rm -rf .pytest_cache .ruff_cache build dist src/*.egg-info

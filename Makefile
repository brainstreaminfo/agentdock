# Common commands. Run `make help` to list them.
# Uses the project virtualenv (.venv) created by `make install`.

PY      := .venv/bin/python
UV      := uv
MANAGE  := $(PY) manage.py
# Tailwind standalone CLI (downloaded by `make vendor`). Override if it can't run
# on your machine, e.g.:  make css TAILWIND="npx @tailwindcss/cli@4.1.11"
TAILWIND ?= ./bin/tailwindcss

.PHONY: help install vendor css css-watch run worker beat flower dev migrate migrations \
        superuser shell test test-fast lint fmt typecheck check logs up down seed schema sync-tools

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Create .venv and install all dependencies (needs uv)
	$(UV) sync
	$(PY) -m pre_commit install || true
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example — fill in the blanks.")

vendor:  ## Download HTMX/Alpine/Chart.js/Lucide + Tailwind CLI
	./scripts/vendor.sh

css:  ## Build static/css/app.css once
	$(TAILWIND) -i assets/tailwind.css -o static/css/app.css --minify

css-watch:  ## Rebuild CSS on every template change
	$(TAILWIND) -i assets/tailwind.css -o static/css/app.css --watch

run:  ## Web server with auto-reload (http://localhost:8000)
	$(PY) -m uvicorn config.asgi:application --reload --port 8000

worker:  ## Celery worker
	$(PY) -m celery -A config worker -l info

beat:  ## Celery beat (schedules)
	$(PY) -m celery -A config beat -l info

flower:  ## Celery monitoring UI (http://localhost:5555)
	$(PY) -m celery -A config flower --port=5555

migrate:  ## Apply database migrations
	$(MANAGE) migrate

seed:  ## Create the example agent templates
	$(MANAGE) seed_agent_templates

sync-tools:  ## Register built-in tools in the database (also runs on deploy)
	$(MANAGE) sync_tools

schema:  ## Download Shopify's Admin GraphQL schema for SHOPIFY_API_VERSION
	$(PY) scripts/update_shopify_schema.py $${SHOPIFY_API_VERSION:-2026-10}

migrations:  ## Create migrations after changing models
	$(MANAGE) makemigrations

superuser:  ## Create a staff user for Django Admin / Agent Studio
	$(MANAGE) createsuperuser

shell:  ## Django shell
	$(MANAGE) shell

test:  ## Run the test suite with coverage
	$(PY) -m pytest --cov=apps --cov-report=term-missing:skip-covered

test-fast:  ## Run tests, stop at first failure
	$(PY) -m pytest -x -q

lint:  ## Ruff lint + format check
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

fmt:  ## Auto-fix lint issues and format code
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

typecheck:  ## mypy
	$(PY) -m mypy apps config

check: lint typecheck test  ## Everything CI runs (each phase must end green)
	./scripts/check_no_embedded.sh

up:  ## Start everything in Docker (postgres, redis, web, worker, beat, flower)
	docker compose up --build

down:  ## Stop Docker services
	docker compose down

logs:  ## Tail web + worker logs (Docker)
	docker compose logs -f --tail=100 web worker

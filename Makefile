# One-stop wrappers for the commands in docs/action-plan.md section 2.
# `make check` runs lint + tests for backend and frontend.

PY      ?= backend/.venv/bin/python
PORT    ?= 31337
IMAGE   ?= woosarr:dev
DEVCONF ?= $(CURDIR)/config

.PHONY: help install lint test check ui dev image smoke openapi

help:
	@echo "install  create backend/.venv, install backend + frontend deps"
	@echo "check    lint + tests, backend and frontend (what CI runs)"
	@echo "openapi  refresh frontend/openapi.json after changing API models"
	@echo "dev      run the backend on :$(PORT) with ./config and the built UI"
	@echo "image    build the Docker image ($(IMAGE))"
	@echo "smoke    run the image and check /api/health (needs Docker)"

backend/.venv:
	python3 -m venv backend/.venv

install: backend/.venv
	cd backend && .venv/bin/pip install -e ".[dev]"
	cd frontend && if [ -f package-lock.json ]; then npm ci; else npm install; fi

lint:
	cd backend && ../$(PY) -m ruff check .

test:
	cd backend && ../$(PY) -m pytest -q
	cd frontend && npm test

ui:
	cd frontend && npm run build

openapi:
	cd backend && ../$(PY) -m woosarr.openapi ../frontend/openapi.json
	cd frontend && npm run gen

check: lint test ui

dev: ui
	WOOSARR_CONFIG_DIR=$(DEVCONF) WOOSARR_STATIC_DIR=$(CURDIR)/frontend/dist WOOSARR_LOG_FORMAT=text \
		WOOSARR_PORT=$(PORT) $(PY) -m woosarr

image:
	docker build -f docker/Dockerfile -t $(IMAGE) .

smoke:
	scripts/smoke-test.sh $(IMAGE) $(PORT)

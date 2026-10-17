# One-stop wrappers for the commands in docs/action-plan.md section 2.
# `make check` runs lint + tests for backend and frontend.

PY      ?= backend/.venv/bin/python
PORT    ?= 31337
IMAGE   ?= woosarr:dev
DEVCONF ?= $(CURDIR)/config

.PHONY: help install lint test check ui dev image smoke openapi e2e contrast demo lock audit sso-docs

help:
	@echo "install  create backend/.venv, install backend + frontend deps"
	@echo "check    lint + tests, backend and frontend (what CI runs)"
	@echo "openapi  refresh frontend/openapi.json after changing API models"
	@echo "e2e      Playwright UI tests against a demo backend"
	@echo "demo     run on :$(PORT) with fake Plex/Discord data (WOOSARR_DEMO)"
	@echo "dev      run the backend on :$(PORT) with ./config and the built UI"
	@echo "image    build the Docker image ($(IMAGE))"
	@echo "smoke    run the image and check /api/health (needs Docker)"

backend/.venv:
	python3 -m venv backend/.venv

install: backend/.venv
	cd backend && .venv/bin/pip install -c constraints.txt -e ".[dev]"
	cd frontend && if [ -f package-lock.json ]; then npm ci; else npm install; fi

lint:
	cd backend && ../$(PY) -m ruff check .

test:
	cd backend && ../$(PY) -m pytest -q
	cd frontend && npm test

ui:
	cd frontend && npm run build

sso-docs:
	cd backend && ../$(PY) -m woosarr.services.oidc_presets ../docs/sso.md

openapi:
	cd backend && ../$(PY) -m woosarr.openapi ../frontend/openapi.json
	cd frontend && npm run gen

contrast:
	python3 scripts/check_contrast.py

check: lint test ui contrast

e2e:
	cd frontend && npm run e2e

demo: ui
	WOOSARR_DEMO=true WOOSARR_SETUP_CODE_REQUIRED=false $(MAKE) dev

dev: ui
	WOOSARR_CONFIG_DIR=$(DEVCONF) WOOSARR_STATIC_DIR=$(CURDIR)/frontend/dist WOOSARR_LOG_FORMAT=text \
		WOOSARR_PORT=$(PORT) $(PY) -m woosarr

# Re-pin backend runtime dependencies from a clean venv.
lock:
	rm -rf .lockvenv && python3 -m venv .lockvenv && .lockvenv/bin/pip install -q ./backend
	( echo "# Pinned runtime dependency versions (plan P10-05). Regenerate with \`make lock\`."; \
	  echo "# Used by the Dockerfile and CI: pip install -c constraints.txt ."; \
	  .lockvenv/bin/pip freeze --exclude-editable | grep -viE "^woosarr" ) > backend/constraints.txt
	rm -rf .lockvenv

audit:
	cd backend && .venv/bin/pip-audit --skip-editable
	cd frontend && npm audit --omit=dev --audit-level=moderate

image:
	docker build -f docker/Dockerfile -t $(IMAGE) .

smoke:
	scripts/smoke-test.sh $(IMAGE) $(PORT)

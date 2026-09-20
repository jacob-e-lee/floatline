# Floatline - developer tasks
#
# `make` is OPTIONAL: scripts/dev.py does everything the dev targets do, and
# works even where make/execution-policy blocks get in the way.
#
# INTERPRETER: on this machine bare `python` is MSYS2's interpreter and has NONE
# of the backend dependencies, so PY defaults to the Windows launcher. Override
# for Linux/macOS or a virtualenv:
#
#     make test PY=python3
#     make dev  PY=.venv/bin/python
#
PY  ?= py -3.14
NPM ?= npm

.PHONY: help dev dev-prod smoke backend-only seed test up up-local down logs

help:
	@echo "make dev         - backend + Astro dev server (PID loop ON, real trades)"
	@echo "make dev-prod    - build + serve the production bundle on :4173"
	@echo "make smoke       - start the stack, run PID unit tests + end-to-end smoke test"
	@echo "make backend-only- start just the FastAPI API"
	@echo "make seed        - seed 30 days of synthetic telemetry into TimescaleDB"
	@echo "make test        - run the backend PID unit tests (no network)"
	@echo "make up          - docker compose up (fullstack + Caddy, needs Docker)"
	@echo "make up-local    - docker compose with Caddyfile.local (plain HTTP :8080)"
	@echo "make down        - stop the docker stack"
	@echo "make logs        - tail container logs"
	@echo ""
	@echo "Override the interpreter with PY=... (current: $(PY))"

dev:
	$(PY) scripts/dev.py

dev-prod:
	$(PY) scripts/dev.py --prod

smoke:
	$(PY) scripts/dev.py --smoke

backend-only:
	$(PY) scripts/dev.py --backend-only

seed:
	cd backend && $(PY) -m app.scripts.seed_history

test:
	cd backend && $(PY) -m pytest tests -q

up:
	docker compose up -d --build

up-local:
	docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100


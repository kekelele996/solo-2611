SHELL := /bin/zsh

.PHONY: setup install check backend-check frontend-check contract-check gen-api grep-check test-linux dev app install-service uninstall-service

setup:
	python3.12 -m venv backend/.venv
	backend/.venv/bin/python -m pip install -e 'backend[dev]'
	cd frontend && npm install

install:
	backend/.venv/bin/python -m pip install -e 'backend[dev]'
	cd frontend && npm ci

check: backend-check contract-check frontend-check grep-check

backend-check:
	cd backend && PYTHONPATH=. .venv/bin/ruff check solo_auto tests scripts
	cd backend && PYTHONPATH=. .venv/bin/mypy --strict solo_auto
	cd backend && PYTHONPATH=. .venv/bin/pytest -q

frontend-check:
	cd frontend && npm run typecheck
	cd frontend && npm run lint
	cd frontend && npm run test
	cd frontend && npm run build

contract-check:
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
		cd backend && PYTHONPATH=. .venv/bin/python scripts/export_openapi.py "$$tmp/openapi.json"; \
		cd ../frontend && ./node_modules/.bin/openapi-typescript "$$tmp/openapi.json" -o "$$tmp/schema.ts"; \
		cmp openapi.json "$$tmp/openapi.json" && cmp src/api/schema.ts "$$tmp/schema.ts" || \
		(echo "OpenAPI 或 TypeScript schema 未同步，请运行 make gen-api" && exit 1)

gen-api:
	cd frontend && npm run gen:api

grep-check:
	@! rg -n "def (generate|polish|rewrite)_feedback|write_generated_feedback" backend/solo_auto
	@! rg -n 'delivery/(feedback\.md|提示词\.md)' backend/solo_auto
	@! rg -n "outline:\s*none" frontend/src
	@! rg -n -- "--allow-incomplete" backend/solo_auto
	@! rg -n "<textarea|contentEditable|CodeMirror" frontend/src/pages/AuditPage*
	@! rg -n 'ackedBy.*"user"' backend/solo_auto/pipeline/autopilot.py backend/solo_auto/pipeline/audit.py
	@! rg -n '"docker",\s*"ps"' backend/solo_auto -g '!backend/solo_auto/dispatch/**'
	@! rg -n 'desired_state\s*=\s*["\x27]running' backend/solo_auto -g '!dispatch/kernel.py'
	@! rg -n 'monitor/state\.json.*write_text|write_text\([^\n]*state\.json' backend/solo_auto
	@! rg -n 'continue_max\s*=\s*[3-9]|continues_max\s*=\s*[3-9]|continueMax[^\n]*[3-9]' backend/solo_auto -g '!**/tests/**'
	@! rg -n 'INSERT INTO session_rounds' backend/solo_auto -g '!backend/solo_auto/store/repo.py'
	@count=$$(rg -n "transition:|animation:" frontend/src | wc -l | tr -d ' '); \
		test "$$count" -le 15 || (echo "动效声明过多: $$count" && exit 1)

test-linux:
	docker run --rm -v "$$PWD":/workspace -w /workspace/backend python:3.12-slim \
		sh -lc "apt-get update >/tmp/apt.log && apt-get install -y git >/tmp/apt-install.log && \
		pip install --no-cache-dir -e '.[dev]' >/tmp/pip.log && pytest -q"

dev:
	SOLO_AUTO_HOME=$${SOLO_AUTO_HOME:-$$HOME/solo-auto} backend/.venv/bin/python -m solo_auto

app: frontend-check
	SOLO_AUTO_HOME=$${SOLO_AUTO_HOME:-$$HOME/solo-auto} backend/.venv/bin/python -m solo_auto

install-service:
	@scripts/launchd-service.sh install

uninstall-service:
	@scripts/launchd-service.sh uninstall

SHELL := /bin/zsh

.PHONY: setup install check backend-check frontend-check grep-check test-linux dev app

setup:
	python3.11 -m venv backend/.venv
	backend/.venv/bin/python -m pip install -e 'backend[dev]'
	cd frontend && npm install

install:
	backend/.venv/bin/python -m pip install -e 'backend[dev]'
	cd frontend && npm ci

check: backend-check frontend-check grep-check

backend-check:
	cd backend && .venv/bin/ruff check solo_auto tests scripts
	cd backend && .venv/bin/mypy --strict solo_auto/pipeline solo_auto/gates solo_auto/gateway solo_auto/store
	cd backend && .venv/bin/pytest -q

frontend-check:
	cd frontend && npm run gen:api
	@if git rev-parse --verify HEAD >/dev/null 2>&1; then \
		git diff --exit-code -- frontend/openapi.json frontend/src/api/schema.ts; \
	fi
	cd frontend && npm run typecheck
	cd frontend && npm run lint
	cd frontend && npm run test
	cd frontend && npm run build

grep-check:
	@! rg -n "def (generate|polish|rewrite)_feedback|write_generated_feedback" backend/solo_auto
	@! rg -n 'delivery/(feedback\.md|提示词\.md)' backend/solo_auto
	@! rg -n "outline:\s*none" frontend/src
	@! rg -n -- "--allow-incomplete" backend/solo_auto
	@count=$$(rg -n "transition:|animation:" frontend/src | wc -l | tr -d ' '); \
		test "$$count" -le 12 || (echo "动效声明过多: $$count" && exit 1)

test-linux:
	docker run --rm -v "$$PWD":/workspace -w /workspace/backend python:3.11-slim \
		sh -lc "apt-get update >/tmp/apt.log && apt-get install -y git >/tmp/apt-install.log && \
		pip install --no-cache-dir -e '.[dev]' >/tmp/pip.log && pytest -q"

dev:
	SOLO_AUTO_HOME=$${SOLO_AUTO_HOME:-$$HOME/solo-auto} backend/.venv/bin/python -m solo_auto

app: frontend-check
	SOLO_AUTO_HOME=$${SOLO_AUTO_HOME:-$$HOME/solo-auto} backend/.venv/bin/python -m solo_auto

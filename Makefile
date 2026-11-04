.PHONY: install dev test lint build check frontend-install

install:
	python -m pip install -r requirements-dev.txt

frontend-install:
	cd frontend && npm ci

dev:
	python -m uvicorn app.main:app --host 127.0.0.1 --port 8787 --reload

test:
	python -m pytest

lint:
	python -m ruff check app tests
	python -m ruff format --check app tests

build:
	cd frontend && npm run build

check: lint test
	cd frontend && npm run typecheck && npm run build

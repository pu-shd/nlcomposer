VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
UVICORN := $(VENV)/bin/uvicorn

.PHONY: help install dev test run docker-test docker-run clean

help:
	@echo "make install     create venv + install deps"
	@echo "make dev         install dev deps (pytest, etc.)"
	@echo "make test        run pytest in venv"
	@echo "make run         run uvicorn locally on :8000"
	@echo "make docker-test run pytest inside Docker"
	@echo "make docker-run  run app inside Docker (port 8000)"
	@echo "make clean       remove venv + caches"

$(VENV):
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

install: $(VENV)
	$(PIP) install -r requirements.txt

dev: $(VENV)
	$(PIP) install -r requirements-dev.txt

test: dev
	$(PYTEST) -q

run: install
	$(UVICORN) app.main:app --reload --host 127.0.0.1 --port 8000

docker-test:
	docker-compose --profile test run --rm tests

docker-run:
	docker-compose up --build app

clean:
	rm -rf $(VENV) .pytest_cache **/__pycache__

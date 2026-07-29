.PHONY: install format lint typecheck test run migrate seed verify-bookmakers compose-up

install:
	python -m pip install -e '.[dev]'
format:
	ruff format .
lint:
	ruff check .
typecheck:
	mypy app tests
test:
	pytest
run:
	uvicorn app.main:app --reload
migrate:
	alembic upgrade head
seed:
	python -m app.scripts.seed_mock_data
verify-bookmakers:
	python -m app.scripts.verify_oddspapi_bookmakers
compose-up:
	docker compose up -d postgres redis

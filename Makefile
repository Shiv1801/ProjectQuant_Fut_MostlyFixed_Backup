.PHONY: setup db-migrate paper live both backtest test lint

setup:
	python -m venv venv
	. venv/bin/activate && pip install -r requirements.txt

db-migrate:
	python scripts/setup_db.py

login:
	python scripts/fyers_login.py

paper:
	python scripts/run_paper.py

live:
	@echo "Live mode requires go-live gates cleared (PRD §2). Use scripts/run_live.py deliberately."
	python scripts/run_live.py

both:
	python scripts/run_both.py

backtest:
	python scripts/run_backtest.py --models all --window 24m

retrain:
	python scripts/retrain_models.py

api:
	uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

test:
	pytest -v --cov=engine --cov=api

lint:
	ruff check . && black --check .

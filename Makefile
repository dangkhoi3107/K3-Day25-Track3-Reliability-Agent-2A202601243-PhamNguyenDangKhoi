.PHONY: test lint typecheck run-chaos report clean docker-up docker-down

test:
	pytest -q -p no:cacheprovider

lint:
	ruff check src tests scripts

typecheck:
	mypy src

run-chaos:
	python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json

report:
	python scripts/generate_report.py --metrics reports/metrics.json --out reports/final_report.md

docker-up:
	docker compose up -d

docker-down:
	docker compose down

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache reports/metrics.json reports/final_report.md

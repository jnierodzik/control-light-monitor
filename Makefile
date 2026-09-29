# Development is uv-based on the laptop. On the Pi the venv must be created with
# --system-site-packages so that apt's python3-picamera2 is importable; see README.

CONFIG ?= config/dev-synthetic.yaml
UV     ?= uv

.PHONY: help setup dev dry-run node test lint fmt calibrate deploy clean

help:
	@echo "setup      install the dev environment (uv, Python 3.11)"
	@echo "dev        run the detection engine against $(CONFIG) on a demo script"
	@echo "dry-run    run the full node pipeline without touching a broker"
	@echo "node       run the node for real (needs a broker)"
	@echo "test       run the test suite"
	@echo "lint       ruff check + format check"
	@echo "fmt        ruff format + autofix"
	@echo "calibrate  serve the calibration web UI"
	@echo "deploy     push to the Pi"
	@echo ""
	@echo "override the config with:  make dev CONFIG=config/machine1.yaml"

setup:
	$(UV) sync --extra dev

dev:
	$(UV) run stacklight detect $(CONFIG) --demo --fast

# Runs the full node pipeline, recording publishes instead of sending them.
dry-run:
	$(UV) run stacklight node $(CONFIG) --dry-run --seconds 20

node:
	$(UV) run stacklight node $(CONFIG)

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

fmt:
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests

calibrate:
	@echo "The calibration UI is not built yet (step 4)." && exit 1

deploy:
	@echo "Deployment is not built yet (step 6)." && exit 1

clean:
	rm -rf .pytest_cache .ruff_cache dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

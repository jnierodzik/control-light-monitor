# Development is uv-based on the laptop. On the Pi the venv must be created with
# --system-site-packages so that apt's python3-picamera2 is importable; see README.

CONFIG ?= config/dev-synthetic.yaml
UV     ?= uv

.PHONY: help setup dev dry-run node test lint fmt calibrate stage bootstrap deploy logs restart probe clean

help:
	@echo "setup      install the dev environment (uv, Python 3.11)"
	@echo "dev        run the detection engine against $(CONFIG) on a demo script"
	@echo "dry-run    run the full node pipeline without touching a broker"
	@echo "node       run the node for real (needs a broker)"
	@echo "test       run the test suite"
	@echo "lint       ruff check + format check"
	@echo "fmt        ruff format + autofix"
	@echo "calibrate  serve the calibration web UI"
	@echo ""
	@echo "On the Pi (override PI, PI_USER, NODE):"
	@echo "stage      copy code + build the venv in ~/stacklight (no sudo, no systemd)"
	@echo "bootstrap  one-time: apt packages, dirs, and a --system-site-packages venv"
	@echo "deploy     rsync the code, install the config and the systemd unit"
	@echo "logs       follow journalctl for the node"
	@echo "restart    restart the node"
	@echo "probe      grab a locked underexposed frame from the camera"
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
	@echo "The calibration UI is not built yet." && exit 1

# Deployment. Override the target on the command line:
#   make deploy PI=stacklight.local PI_USER=jesse NODE=machine1
PI      ?= stacklight.local
PI_USER ?= jnierodzik
NODE    ?= machine1
REMOTE  ?= $(PI_USER)@$(PI)
# -4 is not optional: <host>.local resolves to an IPv6 link-local address first, which
# carries no scope id and so fails with "No route to host".
SSH     ?= ssh -4
SCP     ?= scp -4

# Everything needed to run the tap by hand on a freshly flashed Pi, with no sudo and no
# systemd. This is what gets re-run after a reflash; it takes about a minute.
#
#   make stage PI=stacklight.local PI_USER=jnierodzik
STAGE_DIR ?= stacklight
stage:
	rsync -az --delete -e '$(SSH)' --exclude '.venv' --exclude '.git' \
		--exclude '__pycache__' --exclude 'samples' --exclude '.pytest_cache' \
		--exclude '.ruff_cache' --exclude 'hardware' ./ $(REMOTE):$(STAGE_DIR)/
	$(SSH) $(REMOTE) 'cd $(STAGE_DIR) && python3 -m venv --system-site-packages .venv \
		&& .venv/bin/pip install -q -e . \
		&& .venv/bin/python -c "import gpiozero, lgpio; print(\"gpio stack OK\")"'
	$(SSH) $(REMOTE) '$(STAGE_DIR)/.venv/bin/stacklight validate $(STAGE_DIR)/config/machine1.yaml'
	@echo
	@echo "Staged. Watch the tap with:"
	@echo "  $(SSH) $(REMOTE) '$(STAGE_DIR)/.venv/bin/stacklight detect $(STAGE_DIR)/config/machine1.yaml'"

# One-time setup on a fresh Pi: system packages, directories, and the venv that can see
# apt's picamera2.
bootstrap:
	$(SSH) $(REMOTE) 'sudo apt update && sudo apt install -y python3-picamera2 python3-venv rsync'
	$(SSH) $(REMOTE) 'sudo mkdir -p /opt/stacklight /etc/stacklight && sudo chown -R $(PI_USER) /opt/stacklight /etc/stacklight'
	$(SSH) $(REMOTE) 'cd /opt/stacklight && python3 -m venv --system-site-packages .venv'
	@echo
	@echo "Now: make deploy PI=$(PI) PI_USER=$(PI_USER) NODE=$(NODE)"

deploy:
	rsync -az --delete -e '$(SSH)' --exclude '.venv' --exclude '.git' --exclude '__pycache__' 		--exclude 'samples' --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude 'hardware' 		./ $(REMOTE):/opt/stacklight/
	$(SSH) $(REMOTE) '/opt/stacklight/.venv/bin/pip install -q -e /opt/stacklight'
	$(SCP) config/$(NODE).yaml $(REMOTE):/etc/stacklight/$(NODE).yaml
	$(SSH) $(REMOTE) 'sudo cp /opt/stacklight/systemd/stacklight-node@.service /etc/systemd/system/ && sudo systemctl daemon-reload'
	$(SSH) $(REMOTE) '/opt/stacklight/.venv/bin/stacklight validate /etc/stacklight/$(NODE).yaml'
	@echo
	@echo "Deployed. Start it with:"
	@echo "  $(SSH) $(REMOTE) 'sudo systemctl enable --now stacklight-node@$(NODE)'"

# Follow the node's log on the Pi.
logs:
	$(SSH) $(REMOTE) 'journalctl -u stacklight-node@$(NODE) -f -n 50'

restart:
	$(SSH) $(REMOTE) 'sudo systemctl restart stacklight-node@$(NODE)'

# Capture a locked, underexposed frame straight from the camera and bring it back, to
# check aim and exposure without any of this project's code involved.
probe:
	$(SSH) $(REMOTE) 'rpicam-still -o /tmp/probe.jpg --width 1280 --height 720 --shutter 800 --gain 1.0 --awb off --denoise off --immediate'
	$(SCP) $(REMOTE):/tmp/probe.jpg /tmp/probe.jpg
	@echo "wrote /tmp/probe.jpg"

clean:
	rm -rf .pytest_cache .ruff_cache dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

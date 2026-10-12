# Development is uv-based on the laptop. On the Pi the venv must be created with
# --system-site-packages so that apt's python3-picamera2 is importable; see README.

CONFIG ?= config/dev-synthetic.yaml
UV     ?= uv

.PHONY: help setup dev dry-run node rules test lint fmt calibrate stage install-cmd bootstrap deploy logs restart shutdown probe clean

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
	@echo "install-cmd print the one-time root setup to run on the Pi"
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

rules:
	$(UV) run stacklight rules config/rules.yaml

history:
	$(UV) run stacklight history config/rules.yaml

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

# Deployment. Runs on your workstation and pushes to the Pi over ssh -- there is no
# Makefile on the Pi, so running this there just says "No rule to make target".
# The defaults below are the live machine tap, so plain `make deploy` is usually right.
# Override only to target a different Pi or config:
#   make deploy PI=other-pi.local PI_USER=someone NODE=other-node
PI      ?= stacklight.local
PI_USER ?= jnierodzik
# The config basename under config/, which is also the systemd instance name:
# stacklight-node@machine1 reads /etc/stacklight/machine1.yaml. Defaults to the tap,
# not the camera -- machine1.yaml needs picamera2 and a camera that is not attached.
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
# Everything a freshly flashed Pi needs before `make deploy` will work. Uses ssh -t so
# sudo can prompt for your password -- without a tty it fails with no useful message.
#
# picamera2 is deliberately absent: it is a large install and only a camera node needs
# it. For one of those, add it by hand. A GPIO node needs gpiozero and lgpio instead,
# and from apt rather than pip, because RPi.GPIO does not work on the Pi 5's RP1.
bootstrap:
	$(SSH) -t $(REMOTE) 'sudo apt update && sudo apt install -y \
		mosquitto mosquitto-clients python3-gpiozero python3-lgpio python3-venv rsync'
	$(SSH) -t $(REMOTE) 'sudo systemctl enable --now mosquitto'
	$(SSH) -t $(REMOTE) 'sudo mkdir -p /opt/stacklight /etc/stacklight /var/lib/stacklight && \
		sudo chown -R $(PI_USER) /opt/stacklight /etc/stacklight /var/lib/stacklight'
	$(SSH) $(REMOTE) 'cd /opt/stacklight && python3 -m venv --system-site-packages .venv'
	@echo
	@echo "Now: make deploy PI=$(PI) PI_USER=$(PI_USER) NODE=$(NODE)"

# One command the user runs ON the Pi, interactively, because every step needs root.
# Printed rather than executed: sudo wants a password and ssh cannot supply one.
install-cmd:
	@echo "Run this on the Pi (ssh $(REMOTE)), it will ask for your password:"
	@echo
	@echo "  sudo apt update && sudo apt install -y mosquitto mosquitto-clients python3-picamera2 python3-gpiozero python3-lgpio rsync"
	@echo "  sudo systemctl enable --now mosquitto"
	@echo "  sudo mkdir -p /opt/stacklight /etc/stacklight /var/lib/stacklight"
	@echo "  sudo chown -R $(PI_USER) /opt/stacklight /etc/stacklight /var/lib/stacklight"
	@echo "  python3 -m venv --system-site-packages /opt/stacklight/.venv"
	@echo
	@echo "Then back here:  make deploy PI=$(PI) PI_USER=$(PI_USER) NODE=$(NODE)"

deploy:
	rsync -az --delete -e '$(SSH)' --exclude '.venv' --exclude '.git' --exclude '__pycache__' 		--exclude 'samples' --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude 'hardware' 		./ $(REMOTE):/opt/stacklight/
	$(SSH) $(REMOTE) '/opt/stacklight/.venv/bin/pip install -q -e /opt/stacklight'
	$(SCP) config/$(NODE).yaml $(REMOTE):/etc/stacklight/$(NODE).yaml
	@# The committed rules config carries a placeholder topic, because on ntfy.sh the
	@# topic name is the only secret there is. The real one lives in the gitignored
	@# local override, and that is what actually gets deployed when it exists.
	$(SCP) $$([ -f config/rules-pi.local.yaml ] && echo config/rules-pi.local.yaml || echo config/rules-pi.yaml) \
		$(REMOTE):/etc/stacklight/rules.yaml
	$(SSH) $(REMOTE) '/opt/stacklight/.venv/bin/stacklight validate /etc/stacklight/$(NODE).yaml'
	@echo
	@echo "Code and configs are in place. Finish on the Pi (needs your password):"
	@echo
	@echo "  sudo cp /opt/stacklight/systemd/*.service /etc/systemd/system/"
	@echo "  sudo systemctl daemon-reload"
	@echo "  sudo systemctl enable --now stacklight-node@$(NODE) stacklight-rules"
	@echo
	@echo "Then:  make logs PI=$(PI) PI_USER=$(PI_USER) NODE=$(NODE)"

# Follow the node's log on the Pi.
# Always use this rather than pulling the plug. An unclean power-off leaves the
# filesystem dirty, and enough of them corrupt the card -- which is how this unit lost
# an OS and needed reflashing. Wait for the LED to go dark before removing power.
#
# The Pi 5 also has a physical power button beside the USB-C socket: one system asks for
# the same clean shutdown, which is the one to use when you are standing at the machine.
shutdown:
	$(SSH) -t $(REMOTE) 'sudo systemctl poweroff' || true
	@echo
	@echo "Shutdown requested. Wait for the LED to go dark, then it is safe to unplug."

logs:
	$(SSH) $(REMOTE) 'journalctl -u stacklight-node@$(NODE) -f -n 50'

# Both services, because a config change usually touches both and restarting only the
# node leaves the rules engine running the previous rules -- with no sign that it is.
# ssh -t so sudo can prompt; without a tty it fails and prints nothing useful.
restart:
	$(SSH) -t $(REMOTE) 'sudo systemctl restart stacklight-node@$(NODE) stacklight-rules'
	@sleep 2
	@$(SSH) $(REMOTE) 'systemctl is-active stacklight-node@$(NODE) stacklight-rules | tr "\n" " "; echo'

# Capture a locked, underexposed frame straight from the camera and bring it back, to
# check aim and exposure without any of this project's code involved.
probe:
	$(SSH) $(REMOTE) 'rpicam-still -o /tmp/probe.jpg --width 1280 --height 720 --shutter 800 --gain 1.0 --awb off --denoise off --immediate'
	$(SCP) $(REMOTE):/tmp/probe.jpg /tmp/probe.jpg
	@echo "wrote /tmp/probe.jpg"

clean:
	rm -rf .pytest_cache .ruff_cache dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

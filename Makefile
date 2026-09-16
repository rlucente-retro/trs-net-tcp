# Makefile for trs-net-tcp
PYTHON ?= $(shell which python3)
VENV   ?= .venv

# Upstream distribution for Daniel Paul Martin's TRS-NET assets
UPSTREAM_URL := https://danielpaulmartin.com/sitepad-data/uploads/TRS-NET.zip
ARCHIVE      := TRS-NET.zip
VOLUMES_DIR  := Volumes
DEFAULT_DISK := $(VOLUMES_DIR)/sys720k.dsk
PRINTER_DIR  := printer

# Default server configuration
PORT         ?= 65432
HOST         ?= 0.0.0.0
VOLUME       ?= $(DEFAULT_DISK)
PRINTER      ?= $(PRINTER_DIR)/print_out.txt

.PHONY: all run run-verbose fetch test clean distclean help

all: run

# Ensure default disk image is available before running
run: $(DEFAULT_DISK)
	@if [ -d "$(VENV)" ]; then \
		$(VENV)/bin/python trs-netd.py --port $(PORT) --host $(HOST) --volume $(VOLUME) --printer $(PRINTER); \
	else \
		$(PYTHON) trs-netd.py --port $(PORT) --host $(HOST) --volume $(VOLUME) --printer $(PRINTER); \
	fi

run-verbose: $(DEFAULT_DISK)
	@if [ -d "$(VENV)" ]; then \
		$(VENV)/bin/python trs-netd.py --port $(PORT) --host $(HOST) --volume $(VOLUME) --printer $(PRINTER) --verbose; \
	else \
		$(PYTHON) trs-netd.py --port $(PORT) --host $(HOST) --volume $(VOLUME) --printer $(PRINTER) --verbose; \
	fi

# Download and unpack disk images from Daniel's site
fetch: $(DEFAULT_DISK)

$(DEFAULT_DISK):
	@echo "Fetching TRS-NET distribution from Daniel Paul Martin's site..."
	curl -s -L -o $(ARCHIVE) $(UPSTREAM_URL)
	@echo "Unpacking Volumes/..."
	unzip -q -o $(ARCHIVE) "Volumes/*"
	@mkdir -p $(PRINTER_DIR)
	@rm -f $(ARCHIVE)
	@echo "Disk volumes ready in $(VOLUMES_DIR)/."

# Run unit and integration tests (uses synthetic disk image in temp dir)
test:
	@if [ -d "$(VENV)" ]; then \
		$(VENV)/bin/python test_trs_netd.py; \
	else \
		$(PYTHON) test_trs_netd.py; \
	fi

clean:
	rm -rf __pycache__ *.pyc .pytest_cache

# Remove all downloaded assets, leaving only tracked repository code
distclean: clean
	rm -rf $(VOLUMES_DIR) $(PRINTER_DIR) $(ARCHIVE) upstream

help:
	@echo "trs-net-tcp - Network Server Daemon for TRS-OS"
	@echo ""
	@echo "Targets:"
	@echo "  make run         - Run trs-netd server (auto-fetches volume if missing)"
	@echo "  make run-verbose - Run trs-netd with verbose logging"
	@echo "  make fetch       - Download disk images from upstream"
	@echo "  make test        - Run test_trs_netd.py test suite"
	@echo "  make clean       - Remove cached Python files"
	@echo "  make distclean   - Remove all downloaded disk images and archives"

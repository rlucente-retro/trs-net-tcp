# Makefile for trs-net-tcp
PYTHON ?= $(shell which python3)
VENV   ?= .venv

# Default configuration
PORT   ?= 65432
HOST   ?= 0.0.0.0
VOLUME ?= Volumes/sys720k.dsk
PRINTER ?= printer/print_out.txt

.PHONY: all run test clean help

all: run

run:
	@if [ -d "$(VENV)" ]; then \
		$(VENV)/bin/python trs-netd.py --port $(PORT) --host $(HOST) --volume $(VOLUME) --printer $(PRINTER); \
	else \
		$(PYTHON) trs-netd.py --port $(PORT) --host $(HOST) --volume $(VOLUME) --printer $(PRINTER); \
	fi

run-verbose:
	@if [ -d "$(VENV)" ]; then \
		$(VENV)/bin/python trs-netd.py --port $(PORT) --host $(HOST) --volume $(VOLUME) --printer $(PRINTER) --verbose; \
	else \
		$(PYTHON) trs-netd.py --port $(PORT) --host $(HOST) --volume $(VOLUME) --printer $(PRINTER) --verbose; \
	fi

test:
	@if [ -d "$(VENV)" ]; then \
		$(VENV)/bin/python test_trs_netd.py; \
	else \
		$(PYTHON) test_trs_netd.py; \
	fi

clean:
	rm -rf __pycache__ *.pyc .pytest_cache

help:
	@echo "trs-net-tcp - Network Server Daemon for TRS-OS"
	@echo ""
	@echo "Targets:"
	@echo "  make run         - Run trs-netd server (PORT=$(PORT), VOLUME=$(VOLUME))"
	@echo "  make run-verbose - Run trs-netd with verbose logging"
	@echo "  make test        - Run test_trs_netd.py unit test suite"
	@echo "  make clean       - Remove cached files"

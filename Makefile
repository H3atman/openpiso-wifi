.PHONY: setup check sim e2e dev venv

setup: venv
	git config --local core.hooksPath .githooks

check:
	$(MAKE) -C controller check
	$(MAKE) -C controller build
	$(MAKE) -C controller armv7
	$(MAKE) -C firmware/subvendo/sim run

sim:
	$(MAKE) -C firmware/subvendo/sim run

e2e:
	$(MAKE) -C controller e2e

dev:
	python3 -I lab/dev/dev.py up

venv:
	$(MAKE) -C firmware/subvendo venv

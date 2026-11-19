GO ?= go
VERSION ?= dev
# Output directory for the binaries; the update E2E builds its release tree elsewhere.
OUT ?= bin
# Release public key (base64 Ed25519), compiled in so the binary can verify updates. Empty when
# release.pub is absent, which makes every update operation fail.
RELEASE_KEY := $(shell cat release.pub 2>/dev/null | tr -d '[:space:]')
LDFLAGS := -X main.version=$(VERSION) -X github.com/H3atman/openpiso-wifi/controller/internal/update.ReleaseKey=$(RELEASE_KEY)

.PHONY: build check e2e cross armv7 arm64 amd64 e2e-armv7 e2e-armv7-update

build:
	CGO_ENABLED=0 $(GO) build -ldflags "$(LDFLAGS)" -o $(OUT)/openpiso ./cmd/openpiso

check:
	CGO_ENABLED=0 $(GO) vet ./...

# Lab for `e2e`: docker (a throwaway lab per run) or libvirt (exclusive chr-lab and chr-client,
# goes away with #39); see README.md before running.
LAB ?= docker

e2e:
	python3 -I e2e/run.py --build --lab $(LAB) --binary "$(CURDIR)/bin/openpiso"

armv7:
	CGO_ENABLED=0 GOOS=linux GOARCH=arm GOARM=7 $(GO) build -ldflags "$(LDFLAGS)" -o $(OUT)/openpiso-linux-armv7 ./cmd/openpiso

arm64:
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -ldflags "$(LDFLAGS)" -o $(OUT)/openpiso-linux-arm64 ./cmd/openpiso

amd64:
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 $(GO) build -ldflags "$(LDFLAGS)" -o $(OUT)/openpiso-linux-amd64 ./cmd/openpiso

cross: armv7 arm64 amd64

# Same scenario body as native, with the ARMv7 guest (QEMU/TCG in a container) in the Controller's
# place on a throwaway Docker lab; writes e2e/reports/docker-armv7.json.
e2e-armv7:
	python3 -I e2e/run.py --build --armv7 --binary "$(CURDIR)/bin/openpiso-linux-armv7"

# ARMv7 guest only, no CHR: install v0.1.0, update, roll back and refuse; see e2e/update_vm.py.
# Builds its own throwaway-key releases with $(GO).
e2e-armv7-update:
	GO="$(GO)" python3 -I e2e/run.py --armv7 --only update

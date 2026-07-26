SHELL := /bin/bash

PYTHON ?= python3
DOCKER ?= docker
VERSION = $(shell PYTHON="$(PYTHON)" "$(CURDIR)/scripts/version.sh" current)
TAG ?= v$(VERSION)

RELEASE_DIR ?= $(CURDIR)/dist/release
WHEEL_DIR ?= $(CURDIR)/dist/wheels
NICO_HOME ?= $(HOME)/.nico
NICO_BIN_DIR ?= $(HOME)/.local/bin
RUNTIME ?= native
PROVIDER ?=
WEB_SEARCH ?= searxng
INSTALL_ARGS ?=

BACKEND_IMAGE = nico-agent-backend:$(TAG)
HERMES_IMAGE = nico-agent-hermes:$(TAG)
WEB_IMAGE = nico-agent-web:$(TAG)

.DEFAULT_GOAL := help
.PHONY: help dev-setup infra-up run stop clean cli demo infra-down dev-images version version-set tag \
	validate-release release release-images release-assets install uninstall test-install regressions

help:
	@printf '%s\n' \
	  'make dev-setup     Install editable backend and frontend dependencies' \
	  'make infra-up      Start only Compose infrastructure dependencies' \
	  'make run           Sync/install CLI, then run infrastructure and source services' \
	  'make stop          Stop only source API, Worker, Sandbox and Web processes' \
	  'make clean         Stop all development services; preserve data volumes' \
	  'make cli           Run the installed development CLI; pass ARGS="chat"' \
	  'make demo          Run the credential-free demo against the source API' \
	  'make infra-down    Stop development infrastructure and preserve data' \
	  'make dev-images    Explicitly build branch-commit tagged development images' \
	  '' \
	  'make version       Show package, Git commit and release-tag state' \
	  'make version-set   Set the next SemVer; requires VERSION=X.Y.Z' \
	  'make tag           Create the package version annotated tag from clean main' \
	  '' \
	  'make release       Build local images and release installation assets' \
	  'make install       Reuse or build local assets, then install and start Nico' \
	  'make uninstall     Stop local services and remove program files' \
	  'make test-install  Run installer and release contract tests' \
	  'make regressions   Validate and run the traceable unit regression catalog' \
	  '' \
	  'Options: RUNTIME=native|hermes PROVIDER=openrouter|openai|anthropic' \
	  '         WEB_SEARCH=brave|searxng INSTALL_ARGS="--no-start --non-interactive"' \
	  'Local stack: nico-agent-local-release, API :28000, Web :28080'

dev-setup:
	@NICO_BIN_DIR="$(NICO_BIN_DIR)" "$(CURDIR)/scripts/local-dev.sh" setup

infra-up:
	@NICO_DEV_WEB_SEARCH="$(WEB_SEARCH)" "$(CURDIR)/scripts/local-dev.sh" infra-up

run:
	@NICO_BIN_DIR="$(NICO_BIN_DIR)" NICO_DEV_WEB_SEARCH="$(WEB_SEARCH)" \
	  "$(CURDIR)/scripts/local-dev.sh" run

stop:
	@"$(CURDIR)/scripts/local-dev.sh" stop

clean:
	@NICO_DEV_WEB_SEARCH="$(WEB_SEARCH)" "$(CURDIR)/scripts/local-dev.sh" clean

cli:
	@"$(CURDIR)/scripts/nico-dev" $(ARGS)

demo:
	@NICO_DEMO_API_BASE="$${NICO_DEMO_API_BASE:-http://localhost:8000}" \
	NICO_DEMO_CONSOLE_BASE="$${NICO_DEMO_CONSOLE_BASE:-http://localhost:5173}" \
	  "$(CURDIR)/scripts/demo.sh"

infra-down:
	@NICO_DEV_WEB_SEARCH="$(WEB_SEARCH)" "$(CURDIR)/scripts/local-dev.sh" infra-down

dev-images:
	@set -euo pipefail; \
	  dev_tag="$$(DTN_SUB="$(DTN_SUB)" PYTHON="$(PYTHON)" \
	    "$(CURDIR)/scripts/version.sh" development)"; \
	  $(DOCKER) build --file "$(CURDIR)/backend/Dockerfile" \
	    --tag "nico-agent-backend:$$dev_tag" "$(CURDIR)/backend"; \
	  $(DOCKER) build --file "$(CURDIR)/backend/Dockerfile.hermes" \
	    --tag "nico-agent-hermes:$$dev_tag" "$(CURDIR)/backend"; \
	  $(DOCKER) build --file "$(CURDIR)/frontend/Dockerfile" \
	    --tag "nico-agent-web:$$dev_tag" "$(CURDIR)/frontend"; \
	  printf '[nico-make] development images tagged %s\n' "$$dev_tag"

version:
	@"$(CURDIR)/scripts/version.sh" status

version-set:
	@if [[ "$(origin VERSION)" != "command line" ]]; then \
	  printf '[nico-make] usage: make version-set VERSION=X.Y.Z\n' >&2; exit 2; \
	fi
	@"$(CURDIR)/scripts/version.sh" set "$(VERSION)"

tag:
	@"$(CURDIR)/scripts/version.sh" create-tag

release: release-images release-assets
	@printf '[nico-make] local release %s is ready in %s\n' "$(TAG)" "$(RELEASE_DIR)"

validate-release:
	"$(CURDIR)/scripts/package-release.sh" --tag "$(TAG)" --validate-only

release-images release-assets: validate-release

release-images:
	$(DOCKER) build --file "$(CURDIR)/backend/Dockerfile" \
	  --tag "$(BACKEND_IMAGE)" "$(CURDIR)/backend"
	$(DOCKER) build --file "$(CURDIR)/backend/Dockerfile.hermes" \
	  --tag "$(HERMES_IMAGE)" "$(CURDIR)/backend"
	$(DOCKER) build --file "$(CURDIR)/frontend/Dockerfile" \
	  --tag "$(WEB_IMAGE)" "$(CURDIR)/frontend"

release-assets:
	@mkdir -p "$(WHEEL_DIR)"
	$(PYTHON) -m pip wheel --no-deps --wheel-dir "$(WHEEL_DIR)" "$(CURDIR)/backend"
	@wheel="$$(find "$(WHEEL_DIR)" -maxdepth 1 -type f \
	  -name 'nico_agent_platform-$(VERSION)-*.whl' -print -quit)"; \
	  [[ -n "$$wheel" ]] || { printf '[nico-make] CLI wheel was not produced\n' >&2; exit 1; }; \
	  "$(CURDIR)/scripts/package-release.sh" --tag "$(TAG)" \
	    --wheel "$$wheel" --output "$(RELEASE_DIR)"

install:
	@set -euo pipefail; \
	  needs_release=false; \
	  for asset in install.sh nico-agent-bundle.tar.gz version.txt SHA256SUMS; do \
	    [[ -f "$(RELEASE_DIR)/$$asset" ]] || needs_release=true; \
	  done; \
	  if [[ -f "$(RELEASE_DIR)/version.txt" ]] && \
	     [[ "$$(tr -d '[:space:]' < "$(RELEASE_DIR)/version.txt")" != "$(TAG)" ]]; then \
	    needs_release=true; \
	  fi; \
	  if [[ "$$needs_release" == false ]] && ! (cd "$(RELEASE_DIR)" && \
	     { command -v sha256sum >/dev/null 2>&1 && sha256sum --check SHA256SUMS >/dev/null 2>&1 || \
	       command -v shasum >/dev/null 2>&1 && shasum -a 256 --check SHA256SUMS >/dev/null 2>&1; }); then \
	    needs_release=true; \
	  fi; \
	  $(DOCKER) image inspect "$(BACKEND_IMAGE)" "$(HERMES_IMAGE)" "$(WEB_IMAGE)" \
	    >/dev/null 2>&1 || needs_release=true; \
	  if [[ "$$needs_release" == true ]]; then \
	    printf '[nico-make] local release %s is missing or incomplete; building it\n' "$(TAG)"; \
	    $(MAKE) --no-print-directory release; \
	  else \
	    printf '[nico-make] reusing local release %s from %s\n' "$(TAG)" "$(RELEASE_DIR)"; \
	  fi; \
	  provider_args=(); \
	  if [[ -n "$(PROVIDER)" ]]; then provider_args=(--provider "$(PROVIDER)"); fi; \
	  NICO_HOME="$(NICO_HOME)" NICO_BIN_DIR="$(NICO_BIN_DIR)" \
	    bash "$(RELEASE_DIR)/install.sh" \
	      --version "$(TAG)" \
	      --bundle "$(RELEASE_DIR)/nico-agent-bundle.tar.gz" \
	      --local-images \
	      --runtime "$(RUNTIME)" \
	      "$${provider_args[@]}" $(INSTALL_ARGS)

uninstall:
	"$(CURDIR)/scripts/uninstall.sh" \
	  --dir "$(NICO_HOME)" \
	  --bin-dir "$(NICO_BIN_DIR)"

test-install:
	"$(CURDIR)/scripts/test-install.sh"

regressions:
	"$(CURDIR)/scripts/test-regressions.sh"

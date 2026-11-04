.DEFAULT_GOAL := help

.PHONY: help sync test test-unit test-integration test-e2e validate-plugins coverage lint format format-check mypy pyright typecheck check run tui tui-background tui-list tui-attach tui-detach tui-stop build exe-standalone exe-onefile bot-plugin bot-plugin-nonebot bot-plugin-astrbot clean clean-records clean-trash clean-gateways clean-config

ROOT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
PY ?= uv run --directory "$(ROOT)" --frozen --all-packages --group dev
SESSION ?=

### Setup

sync: ## Install/update the workspace and dev toolchain
	uv sync --project "$(ROOT)" --locked --all-packages --group dev

### Quality gates

test: ## Run the full test suite (pytest)
	$(PY) pytest -q

test-unit: ## Run isolated component, domain and transport-contract tests
	$(PY) pytest -q tests/unit

test-integration: ## Run component, SQLite, ASGI and headless TUI integration tests
	$(PY) pytest -q tests/integration

test-e2e: ## Run real CLI processes against temporary configuration and databases
	$(PY) pytest -q tests/e2e

validate-plugins: ## Check bundled plugins against core contracts with simulated I/O
	$(PY) python -B -m mailflow_testkit.plugin_smoke --root plugins --source

coverage: ## Run all tests with coverage for the current packages and plugins
	$(PY) pytest --cov --cov-report=term-missing --cov-report=html --cov-report=xml

lint: ## Ruff lint check
	$(PY) ruff check .

format: ## Auto-format all sources with Ruff
	$(PY) ruff format .

format-check: ## Verify formatting without changing files
	$(PY) ruff format --check .

mypy: ## Strict mypy type check (packages/plugins/tools)
	$(PY) mypy packages plugins tools

pyright: ## Strict pyright type check (whole workspace)
	$(PY) python -c "import subprocess, sys; sys.exit(subprocess.call([sys.executable, '-m', 'pyright', '--pythonpath', sys.executable]))"

typecheck: mypy pyright ## Run both type checkers

check: test validate-plugins ## Full quality gate: tests and plugins, then lint, format and types
	$(MAKE) -C "$(ROOT)" lint format-check typecheck

### Run

run: ## Start the service in the foreground (configs/development.toml)
	$(PY) mailflow run -c configs/development.toml

tui: ## Launch or reconnect to the TUI (optional SESSION=<name>; F8 detaches)
	$(PY) mailflow tui -c configs/development.toml $(if $(SESSION),--name "$(SESSION)")

tui-background: ## Start a TUI in the background (optional SESSION=<name>)
	$(PY) mailflow tui -c configs/development.toml --background $(if $(SESSION),--name "$(SESSION)")

tui-list: ## List running TUI instances
	$(PY) mailflow tui list

tui-attach: ## Reconnect to a TUI (SESSION=<name> when multiple instances exist)
	$(PY) mailflow tui attach $(if $(SESSION),"$(SESSION)")

tui-detach: ## Detach a terminal while keeping its TUI running (optional SESSION=<name>)
	$(PY) mailflow tui detach $(if $(SESSION),"$(SESSION)")

tui-stop: ## Stop a TUI and release its resources (optional SESSION=<name>)
	$(PY) mailflow tui stop $(if $(SESSION),"$(SESSION)")

### Package

build: ## Build every workspace wheel under dist/wheels
	$(PY) python tools/build_all.py

exe-standalone: ## Build the Nuitka standalone executable (smoke test before onefile)
	$(PY) python tools/build_exe.py --mode standalone

exe-onefile: ## Build the Nuitka onefile executable (only after a standalone smoke test)
	$(PY) python tools/build_exe.py --mode onefile

### Bot framework plugins

bot-plugin: ## Export MailFlow as a chatbot-framework plugin (FRAMEWORK=<id> OUTPUT=<dir>)
	$(PY) mailflow export --framework $(FRAMEWORK) --output $(OUTPUT) -c configs/development.toml

bot-plugin-nonebot: ## Export the NoneBot plugin (configs/development.toml -> dist/nonebot_plugin_mailflow)
	$(PY) mailflow export --framework nonebot --output dist/nonebot_plugin_mailflow -c configs/development.toml

bot-plugin-astrbot: ## Export the AstrBot plugin (configs/development.toml -> dist/astrbot_plugin_mailflow)
	$(PY) mailflow export --framework astrbot --output dist/astrbot_plugin_mailflow -c configs/development.toml

### Cleanup

clean: ## Remove build output, test reports and caches; keep environments and user data
	$(PY) python tools/clean.py

clean-records: ## Move every stored mail + schedule entry to the trash (recoverable)
	$(PY) mailflow command "clean records" -c configs/development.toml

clean-trash: ## Permanently delete everything in the trash (destructive, irreversible)
	$(PY) mailflow command "clean trash CONFIRM" -c configs/development.toml

clean-gateways: ## Delete data/gateways/ after confirmation
	$(PY) python tools/clean.py --gateways

clean-config: ## Delete local config files after confirmation
	$(PY) python tools/clean.py --config

help: ## Show this help
	@$(PY) python -c "import re; from pathlib import Path; [print(f'{name:20} {description}') for name, description in re.findall(r'^([\w-]+):[^\n]*?## (.+)$$', Path('Makefile').read_text(encoding='utf-8'), re.M)]"

.DEFAULT_GOAL := help

.PHONY: help sync test test-unit test-integration test-e2e validate-plugins coverage lint format format-check mypy pyright typecheck check run tui tui-background tui-list tui-attach tui-detach tui-stop build exe-standalone exe-onefile bot-plugin bot-plugin-nonebot bot-plugin-astrbot clean clean-records clean-trash clean-gateways clean-config _require-env

PYTHON ?= $(if $(filter Windows_NT,$(OS)),python,python3)
ROOT := $(shell $(PYTHON) -c "from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve().parent.as_posix())" "$(MAKEFILE_LIST)")
VENV := $(if $(UV_PROJECT_ENVIRONMENT),$(UV_PROJECT_ENVIRONMENT),.venv)
export UV_PROJECT_ENVIRONMENT := $(VENV)
ENV_PYTHON := $(VENV)/$(if $(filter Windows_NT,$(OS)),Scripts/python.exe,bin/python)
PY ?= "$(ENV_PYTHON)"
CONFIG ?= configs/development.toml
SESSION ?=

_require-env:
ifeq ($(origin PY),file)
	@cd "$(ROOT)" && $(PYTHON) -c "from pathlib import Path; import sys; ready=Path(sys.argv[1]).is_file(); print('Run make sync first, or set VENV=... / \u8bf7\u5148\u8fd0\u884c make sync\uff0c\u6216\u6307\u5b9a VENV\u3002') if not ready else None; sys.exit(0 if ready else 2)" "$(ENV_PYTHON)"
endif

test test-unit test-integration test-e2e validate-plugins coverage lint format format-check mypy pyright run tui tui-background tui-list tui-attach tui-detach tui-stop build exe-standalone exe-onefile bot-plugin bot-plugin-nonebot bot-plugin-astrbot clean-records clean-trash: _require-env

### Setup

sync: ## Install/update the workspace and dev toolchain
	uv sync --project "$(ROOT)" --locked --all-packages --group dev

### Quality gates

test: ## Run the full test suite (pytest)
	cd "$(ROOT)" && $(PY) -m pytest -q

test-unit: ## Run isolated component, domain and transport-contract tests
	cd "$(ROOT)" && $(PY) -m pytest -q tests/unit

test-integration: ## Run component, SQLite, ASGI and headless TUI integration tests
	cd "$(ROOT)" && $(PY) -m pytest -q tests/integration

test-e2e: ## Run real CLI processes against temporary configuration and databases
	cd "$(ROOT)" && $(PY) -m pytest -q tests/e2e

validate-plugins: ## Check bundled plugins against core contracts with simulated I/O
	cd "$(ROOT)" && $(PY) -B -m mailflow_testkit.plugin_smoke --root plugins --source

coverage: ## Run all tests with coverage for the current packages and plugins
	cd "$(ROOT)" && $(PY) -m pytest --cov --cov-report=term-missing --cov-report=html --cov-report=xml

lint: ## Ruff lint check
	cd "$(ROOT)" && $(PY) -m ruff check .

format: ## Auto-format all sources with Ruff
	cd "$(ROOT)" && $(PY) -m ruff format .

format-check: ## Verify formatting without changing files
	cd "$(ROOT)" && $(PY) -m ruff format --check .

mypy: ## Strict mypy type check (packages/plugins/tools)
	cd "$(ROOT)" && $(PY) -m mypy packages plugins tools

pyright: ## Strict pyright type check (whole workspace)
	cd "$(ROOT)" && $(PY) -c "import subprocess, sys; sys.exit(subprocess.call([sys.executable, '-m', 'pyright', '--pythonpath', sys.executable]))"

typecheck: mypy pyright ## Run both type checkers

check: ## Full quality gate: tests, plugins, lint, format and types
	$(MAKE) -C "$(ROOT)" test
	$(MAKE) -C "$(ROOT)" validate-plugins
	$(MAKE) -C "$(ROOT)" lint format-check typecheck

### Run

run: ## Start the service in the foreground (configs/development.toml)
	cd "$(ROOT)" && $(PY) -m mailflow_cli.app run -c "$(CONFIG)"

tui: ## Launch or reconnect to the TUI (optional SESSION=<name>; F8 detaches)
	cd "$(ROOT)" && $(PY) -m mailflow_cli.app tui -c "$(CONFIG)" $(if $(SESSION),--name "$(SESSION)")

tui-background: ## Start a TUI in the background (optional SESSION=<name>)
	cd "$(ROOT)" && $(PY) -m mailflow_cli.app tui -c "$(CONFIG)" --background $(if $(SESSION),--name "$(SESSION)")

tui-list: ## List running TUI instances
	cd "$(ROOT)" && $(PY) -m mailflow_cli.app tui list

tui-attach: ## Reconnect to a TUI (SESSION=<name> when multiple instances exist)
	cd "$(ROOT)" && $(PY) -m mailflow_cli.app tui attach $(if $(SESSION),"$(SESSION)")

tui-detach: ## Detach a terminal while keeping its TUI running (optional SESSION=<name>)
	cd "$(ROOT)" && $(PY) -m mailflow_cli.app tui detach $(if $(SESSION),"$(SESSION)")

tui-stop: ## Stop a TUI and release its resources (optional SESSION=<name>)
	cd "$(ROOT)" && $(PY) -m mailflow_cli.app tui stop $(if $(SESSION),"$(SESSION)")

### Package

build: ## Build every workspace wheel under dist/wheels
	cd "$(ROOT)" && $(PY) tools/build_all.py

exe-standalone: ## Build the Nuitka standalone executable (smoke test before onefile)
	cd "$(ROOT)" && $(PY) tools/build_exe.py --mode standalone

exe-onefile: ## Build the Nuitka onefile executable (only after a standalone smoke test)
	cd "$(ROOT)" && $(PY) tools/build_exe.py --mode onefile

### Bot framework plugins

bot-plugin: ## Export MailFlow as a chatbot-framework plugin (FRAMEWORK=<id> OUTPUT=<dir>)
	cd "$(ROOT)" && $(PY) -m mailflow_cli.app export --framework "$(FRAMEWORK)" --output "$(OUTPUT)" -c "$(CONFIG)"

bot-plugin-nonebot: ## Export the NoneBot plugin (configs/development.toml -> dist/nonebot_plugin_mailflow)
	cd "$(ROOT)" && $(PY) -m mailflow_cli.app export --framework nonebot --output dist/nonebot_plugin_mailflow -c "$(CONFIG)"

bot-plugin-astrbot: ## Export the AstrBot plugin (configs/development.toml -> dist/astrbot_plugin_mailflow)
	cd "$(ROOT)" && $(PY) -m mailflow_cli.app export --framework astrbot --output dist/astrbot_plugin_mailflow -c "$(CONFIG)"

### Cleanup

clean: ## Remove build output, test reports and caches; keep environments and user data
	$(PYTHON) "$(ROOT)/tools/clean.py"

clean-records: ## Move every stored mail + schedule entry to the trash (recoverable)
	cd "$(ROOT)" && $(PY) -m mailflow_cli.app command "clean records" -c "$(CONFIG)"

clean-trash: ## Permanently delete everything in the trash (destructive, irreversible)
	cd "$(ROOT)" && $(PY) -m mailflow_cli.app command "clean trash CONFIRM" -c "$(CONFIG)"

clean-gateways: ## Delete data/gateways/ after confirmation
	$(PYTHON) "$(ROOT)/tools/clean.py" --gateways

clean-config: ## Delete local config files after confirmation
	$(PYTHON) "$(ROOT)/tools/clean.py" --config

help: ## Show this help
	@$(PYTHON) -c "import re,sys; from pathlib import Path; [print(f'{name:20} {description}') for name, description in re.findall(r'^([\w-]+):[^\n]*?## (.+)$$', Path(sys.argv[1]).read_text(encoding='utf-8'), re.M)]" "$(ROOT)/Makefile"

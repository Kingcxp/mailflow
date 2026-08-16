.DEFAULT_GOAL := help

.PHONY: help sync test coverage lint format format-check mypy pyright typecheck check run tui build exe-standalone exe-onefile bot-plugin bot-plugin-nonebot bot-plugin-astrbot docs clean

PY ?= uv run

### Setup

sync: ## Install/update the workspace and dev toolchain
	uv sync --all-packages --group dev

### Quality gates

test: ## Run the full test suite (pytest)
	$(PY) pytest -q

coverage: ## Run tests with per-package coverage report (term + html)
	$(PY) pytest --cov=mailflow --cov=mailflow_bundled --cov=mailflow_cli --cov=mailflow_tui --cov=mailflow_testkit --cov=mailflow_mail_fake --cov=mailflow_storage_sqlite --cov=mailflow_llm_openai_compatible --cov=mailflow_processor_rules --cov=mailflow_processor_llm_importance --cov=mailflow_notify_console --cov-report=term-missing --cov-report=html

lint: ## Ruff lint check
	$(PY) ruff check .

format: ## Auto-format all sources with Ruff
	$(PY) ruff format .

format-check: ## Verify formatting without changing files
	$(PY) ruff format --check .

mypy: ## Strict mypy type check (packages/plugins/tools)
	$(PY) mypy packages plugins tools

pyright: ## Strict pyright type check (whole workspace)
	$(PY) pyright

typecheck: mypy pyright ## Run both type checkers

check: lint format-check typecheck test ## Full quality gate: lint + format + types + tests + docs
	$(PY) python tools/check_docs.py

### Run

run: ## Start the service in the foreground (configs/development.toml)
	$(PY) mailflow run -c configs/development.toml

tui: ## Launch the Textual terminal UI (configs/development.toml)
	$(PY) mailflow tui -c configs/development.toml

### Package

build: ## Build wheels for every workspace package (uv build --all-packages)
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

### Docs & cleanup

docs: ## Verify all mandatory documentation is present
	$(PY) python tools/check_docs.py

clean: ## Remove caches, build output and local runtime data (keeps .venv)
	$(PY) python tools/clean.py

help: ## Show this help
	@grep -E '^([a-zA-Z_-]+:.*##|### )' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS = ":.*?## "}; {if ($$0 ~ /^###/) print "\n\033[1m" substr($$0,5) "\033[0m"; else printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

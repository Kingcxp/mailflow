.PHONY: sync test coverage lint format format-check mypy pyright typecheck check run tui build exe-standalone exe-onefile docs clean

PY ?= uv run

sync:
	uv sync --all-packages --group dev

test:
	$(PY) pytest -q

coverage:
	$(PY) pytest --cov=mailflow --cov=mailflow_bundled --cov=mailflow_cli --cov=mailflow_tui --cov=mailflow_testkit --cov=mailflow_mail_fake --cov=mailflow_storage_sqlite --cov=mailflow_llm_openai_compatible --cov=mailflow_processor_rules --cov=mailflow_processor_llm_importance --cov=mailflow_notify_console --cov-report=term-missing --cov-report=html

lint:
	$(PY) ruff check .

format:
	$(PY) ruff format .

format-check:
	$(PY) ruff format --check .

mypy:
	$(PY) mypy packages plugins tools

pyright:
	$(PY) pyright

typecheck: mypy pyright

check: lint format-check typecheck test
	$(PY) python tools/check_docs.py

run:
	$(PY) mailflow run -c configs/development.toml

tui:
	$(PY) mailflow tui -c configs/development.toml

build:
	$(PY) python tools/build_all.py

exe-standalone:
	$(PY) python tools/build_exe.py --mode standalone

exe-onefile:
	$(PY) python tools/build_exe.py --mode onefile

docs:
	$(PY) python tools/check_docs.py

clean:
	$(PY) python tools/clean.py

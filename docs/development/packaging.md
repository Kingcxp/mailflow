# Packaging

## Wheels

```bash
make build          # uv build --all-packages --wheel -> dist/wheels/*.whl
```

Every package builds independently; Core has no runtime dependency on any
plugin, so consumers can install `mailflow-core` alone and provide their own
components.

## Frozen executables

```bash
make exe-standalone   # tools/build_exe.py --mode standalone
# smoke-test the standalone binary FIRST:
./dist/standalone/frozen_entry.dist/mailflow.exe config-check -c configs/development.toml
make exe-onefile      # tools/build_exe.py --mode onefile
```

`tools/build_exe.py` runs Nuitka with:

- `--standalone` / `--onefile`;
- explicit `--include-package` for `mailflow`, `mailflow_bundled`,
  `mailflow_cli`, `mailflow_server`, `mailflow_tui` and all built-in plugins — the official
  plugin set is registered by static imports in `mailflow-bundled`, so
  frozen builds do **not** depend on entry-point metadata;
- `--include-package-data` for `mailflow` (locale JSON) and `mailflow_tui`
  (app.tcss);
- `--no-deployment-flag=self-execution` (all resources are bundled; the
  self-execution guard otherwise misparses CLI arguments such as `-c`).

Onefile output is `dist/onefile/mailflow.exe` on Windows and `dist/onefile/mailflow` on Linux.
Compiler downloads are disabled by default; pass `--download-tools` explicitly when needed.
The release workflow uses the same wheel script and checks the tag against the core version.
Plugin versions are independent and must match their runtime metadata.

Release notes:

- Test standalone before onefile; they compile the same sources.
- Arbitrary post-build Python plugin discovery is **not** promised in frozen
  mode — bundle third-party plugins by extending `INCLUDE_PACKAGES`.
- `configs/` and `translations/` are external CWD-relative files; ship them
  alongside the executable or use absolute paths.
- `tools/frozen_entry.py` is the stable entry point (calls the CLI `main`).

```bash
make clean          # caches, reports and builds; keeps virtual environments, data/ and logs/
```

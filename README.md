# eraMegaten Engine

A modern Python implementation of an EraBasic / Emuera-style runtime for the eraMegaten game family.

The project focuses on loading existing game scripts and data, interpreting core EraBasic control flow, and exposing enough runtime state for command-line replay, regression testing, save handling, and future GUI front ends.

## What it does

- Loads ERB / ERH scripts and CSV data from a user-provided game directory.
- Builds function, label, variable, constant, and character-data indexes.
- Executes common EraBasic / Emuera flow control, expressions, print commands, input commands, arrays, string helpers, and save helpers.
- Provides compatibility shims for many private Emuera extensions used by eraMegaten-style scripts.
- Exposes page, layout, HTML, button, image, and sprite metadata for a modern UI layer.
- Reads existing save data where supported and writes engine sidecar saves without modifying original game files by default.
- Includes a broad regression suite that exercises parser, runtime, save, graphics, and compatibility behavior.

## Project status

This is an experimental compatibility engine, not a complete replacement for Emuera yet. It is useful for analysis, automated replay, regression tests, and incremental engine development. Full visual parity and complete game coverage are still ongoing work.

Current test baseline:

```powershell
python -m pytest tests\test_engine.py -q
```

## Requirements

- Python 3.11 or newer
- pytest for running tests
- Pillow is optional, but required for PNG rendering/export helpers

## Quick start

Install the package in editable mode:

```powershell
python -m pip install -e .
```

Audit a game directory:

```powershell
python -m eramegaten_engine.cli audit <game-root> --top 30
```

Inspect a function:

```powershell
python -m eramegaten_engine.cli inspect <game-root> SYSTEM_TITLE --limit 80
```

Run an entry point non-interactively:

```powershell
python -m eramegaten_engine.cli run <game-root> --entry SYSTEM_TITLE --inputs 1,0 --non-interactive --max-steps 50000 --state-dir <state-dir>
```

Export runtime graphics or sprites after a run:

```powershell
python -m eramegaten_engine.cli run <game-root> --entry <entry> --non-interactive --quiet --export-page <output-png>
```

## Python API example

```python
from pathlib import Path
from eramegaten_engine.loader import load_program
from eramegaten_engine.runtime import EraRuntime

program = load_program(Path("<game-root>"))
runtime = EraRuntime(program, interactive=False, inputs=["1", "0"])
steps = runtime.run("SYSTEM_TITLE", max_steps=50000)

print(steps)
print("".join(runtime.output))
print(runtime.warnings)
```

## Repository layout

```text
eramegaten_engine/   Runtime, parser, loaders, memory model, CLI, save and graphics helpers
tests/               Regression tests
pyproject.toml       Python package metadata
```

## Notes for public use

This repository contains only the engine implementation and tests. It does not include game assets, game scripts, saves, or other copyrighted game data. Provide your own local game directory when using the CLI or API.

By default, the engine writes sidecar state when a state directory is provided, so original game files do not need to be modified.

## License

No license has been selected yet. Treat the code as all rights reserved until a license file is added.

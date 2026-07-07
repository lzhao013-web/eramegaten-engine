# eraMegaten Engine

A Python compatibility engine for running and testing EraBasic / Emuera-style eraMegaten scripts.

This repository contains the engine code only. It does not include game assets, game scripts, saves, or other game data.

## Features

- Load ERB / ERH scripts and CSV data from a user-provided game directory.
- Interpret common EraBasic control flow, expressions, variables, arrays, input, output, and save helpers.
- Provide a CLI for auditing, inspecting, and replaying script entry points.
- Include regression tests for parser, runtime, save, graphics, and compatibility behavior.

## Quick start

```powershell
python -m pip install -e .
python -m pytest tests\test_engine.py -q
```

Run the CLI with your own game directory:

```powershell
python -m eramegaten_engine.cli audit <game-root>
python -m eramegaten_engine.cli inspect <game-root> SYSTEM_TITLE
python -m eramegaten_engine.cli run <game-root> --entry SYSTEM_TITLE --non-interactive
```

## Requirements

- Python 3.11 or newer
- pytest for tests
- Pillow for optional image rendering/export helpers

## Status

The engine is experimental and still improving. It is useful for script analysis, automated replay, compatibility testing, and future UI work.

## License

No license has been selected yet.

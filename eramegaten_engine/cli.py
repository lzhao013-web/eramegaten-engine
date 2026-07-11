from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from .loader import load_program
from .native_save import SaveFileType, native_save_from_memory, write_native_save
from .runtime import EraRuntime


def parse_int_auto(text: str) -> int:
    return int(text, 0)


def _native_metadata(program, args: argparse.Namespace) -> tuple[int, int]:
    csv = getattr(program, "csv", None)
    code = args.native_script_code
    version = args.native_script_version
    if code is None and csv is not None:
        code = csv.resolve_constant("GAMEBASE_CODE", default=0)
    if version is None and csv is not None:
        version = csv.resolve_constant("GAMEBASE_VERSION", default=0)
    return int(code or 0), int(version or 0)


def _write_native_export(path_text: str, *, program, rt: EraRuntime, file_type: int, args: argparse.Namespace) -> None:
    path = Path(path_text)
    if path.exists() and not args.overwrite_native_export:
        raise SystemExit(f"native export target already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    script_code, script_version = _native_metadata(program, args)
    save = native_save_from_memory(
        rt.memory,
        file_type=file_type,
        save_text=args.native_save_text or "",
        script_code=script_code,
        script_version=script_version,
    )
    write_native_save(path, save, program)


def _write_graphic_export(spec: str, *, rt: EraRuntime) -> None:
    if "=" not in spec:
        raise SystemExit(f"graphic export must be GID=PATH: {spec}")
    gid_text, path_text = spec.split("=", 1)
    try:
        gid = parse_int_auto(gid_text.strip())
    except ValueError as exc:
        raise SystemExit(f"invalid graphic id for export: {gid_text}") from exc
    if not path_text.strip():
        raise SystemExit(f"missing graphic export path for id {gid}")
    rt.export_graphic_png(gid, Path(path_text.strip()))


def _write_sprite_export(spec: str, *, rt: EraRuntime) -> None:
    if "=" not in spec:
        raise SystemExit(f"sprite export must be NAME=PATH: {spec}")
    name, path_text = spec.split("=", 1)
    name = name.strip()
    if not name:
        raise SystemExit(f"missing sprite name for export: {spec}")
    if not path_text.strip():
        raise SystemExit(f"missing sprite export path for {name}")
    rt.export_sprite_png(name, Path(path_text.strip()))


def _write_page_export(path_text: str, *, rt: EraRuntime, args: argparse.Namespace) -> None:
    if not path_text.strip():
        raise SystemExit("missing page export path")
    rt.export_page_png(
        Path(path_text.strip()),
        char_width=args.page_char_width,
        line_height=args.page_line_height,
        viewport_width=args.page_viewport_width,
        html_unit_scale=args.page_html_unit_scale,
    )


def command_audit(args: argparse.Namespace) -> int:
    program = load_program(args.root, debug_blocks=args.debug_blocks, load_csv=not args.no_csv)
    command_counts: Counter[str] = Counter()
    line_count = 0
    for funcs in program.functions.values():
        for fn in funcs:
            for line in fn.lines:
                text = line.text.strip()
                if not text or text.startswith("$") or text.startswith("#"):
                    continue
                command_counts[text.split(None, 1)[0].upper()] += 1
                line_count += 1
    data = {
        "root": str(Path(args.root).resolve()),
        "files": len(program.files),
        "functions": program.function_count,
        "uniqueFunctions": len(program.functions),
        "duplicateFunctions": program.duplicate_functions,
        "varDecls": len(program.var_decls),
        "defines": len(program.defines),
        "csvCharacters": len(program.csv.characters) if program.csv else 0,
        "csvConstants": len(program.csv.constants) if program.csv else 0,
        "lines": line_count,
        "topCommands": command_counts.most_common(args.top),
        "warnings": program.warnings[:50],
    }
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"root: {data['root']}")
        print(f"ERB/ERH files: {data['files']}")
        print(f"functions: {data['functions']} ({data['uniqueFunctions']} unique, {len(data['duplicateFunctions'])} duplicated names)")
        print(f"var decls: {data['varDecls']}  defines: {data['defines']}")
        print(f"CSV characters: {data['csvCharacters']}  constants: {data['csvConstants']}")
        print(f"runtime lines: {data['lines']}")
        print("top commands:")
        for name, count in data["topCommands"]:
            print(f"  {count:8d} {name}")
        if data["warnings"]:
            print("warnings:")
            for w in data["warnings"]:
                print(f"  - {w}")
    return 0


def command_run(args: argparse.Namespace) -> int:
    program = load_program(args.root, debug_blocks=args.debug_blocks, load_csv=not args.no_csv)
    inputs = []
    if args.inputs:
        inputs = args.inputs.split(",")
    rt = EraRuntime(program, echo=not args.quiet, interactive=not args.non_interactive, inputs=inputs, state_dir=args.state_dir)
    steps = rt.run(args.entry, max_steps=args.max_steps)
    if args.save_json:
        rt.memory.save_json(args.save_json)
    if args.export_native_save:
        _write_native_export(args.export_native_save, program=program, rt=rt, file_type=SaveFileType.NORMAL, args=args)
    if args.export_native_global:
        _write_native_export(args.export_native_global, program=program, rt=rt, file_type=SaveFileType.GLOBAL, args=args)
    for spec in args.export_graphic or []:
        _write_graphic_export(spec, rt=rt)
    for spec in args.export_sprite or []:
        _write_sprite_export(spec, rt=rt)
    if args.export_page:
        _write_page_export(args.export_page, rt=rt, args=args)
    if args.quiet:
        print("".join(rt.output))
    print(f"\n[eramegaten] steps={steps} warnings={len(rt.warnings)} chars={len(rt.memory.characters)}")
    if rt.warnings and args.show_warnings:
        for w in rt.warnings[:args.show_warnings]:
            print(f"WARN {w}")
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    program = load_program(args.root, debug_blocks=args.debug_blocks, load_csv=not args.no_csv)
    rt = EraRuntime(program, echo=False, interactive=False)
    print(rt.inspect_function(args.function, limit=args.limit))
    return 0


def command_gui(args: argparse.Namespace) -> int:
    # Import lazily so audit/run/inspect remain usable on hosts without Tk.
    from .gui import launch_gui

    return launch_gui(
        args.root or "",
        entry=args.entry,
        max_steps=args.max_steps,
        auto_run=not args.no_auto_run,
        legacy_tk=args.legacy_tk,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eramegaten", description="Modern Python EraBasic/Emuera-compatible engine for eraMegaten")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ["audit", "run", "inspect"]:
        sp = sub.add_parser(name)
        sp.add_argument("root", help="eraMegaten root, e.g. E:\\mgt")
        sp.add_argument("--debug-blocks", action="store_true", help="include [IF_DEBUG] preprocessor blocks")
        sp.add_argument("--no-csv", action="store_true", help="skip CSV loading")
        if name == "audit":
            sp.add_argument("--json", action="store_true")
            sp.add_argument("--top", type=int, default=30)
            sp.set_defaults(func=command_audit)
        elif name == "run":
            sp.add_argument("--entry", default="SYSTEM_TITLE")
            sp.add_argument("--max-steps", type=int, default=100000)
            sp.add_argument("--inputs", help="comma-separated queued inputs")
            sp.add_argument("--non-interactive", action="store_true")
            sp.add_argument("--quiet", action="store_true")
            sp.add_argument("--show-warnings", type=int, default=20)
            sp.add_argument("--save-json")
            sp.add_argument("--state-dir", help="directory for Python-engine sidecar SAVEGLOBAL/SAVEDATA files")
            sp.add_argument("--export-native-save", help="write current memory snapshot as an Emuera 1808 normal .sav to this explicit path")
            sp.add_argument("--export-native-global", help="write current global snapshot as an Emuera 1808 global.sav to this explicit path")
            sp.add_argument("--native-save-text", default="", help="caption text embedded in exported native saves")
            sp.add_argument("--native-script-code", type=parse_int_auto, help="script code embedded in exported native saves; defaults to GAMEBASE_CODE or 0")
            sp.add_argument("--native-script-version", type=parse_int_auto, help="script version embedded in exported native saves; defaults to GAMEBASE_VERSION or 0")
            sp.add_argument("--overwrite-native-export", action="store_true", help="allow replacing an existing explicit native export target")
            sp.add_argument("--export-graphic", action="append", metavar="GID=PATH", help="render a runtime graphic registry entry to a PNG after execution; can be repeated")
            sp.add_argument("--export-sprite", action="append", metavar="NAME=PATH", help="render a sprite/resource name to a PNG after execution; can be repeated")
            sp.add_argument("--export-page", metavar="PATH", help="render the current page/layout snapshot to a PNG after execution")
            sp.add_argument("--page-char-width", type=int, default=8, help="character cell width used by --export-page")
            sp.add_argument("--page-line-height", type=int, default=20, help="line height used by --export-page")
            sp.add_argument("--page-viewport-width", type=int, help="optional viewport width used by --export-page alignment")
            sp.add_argument(
                "--page-html-unit-scale",
                type=float,
                default=1.0,
                help="scale explicit HTML pos/width/height/ypos units for --export-page; use font-size/100 for Emuera TextRenderer units",
            )
            sp.set_defaults(func=command_run)
        else:
            sp.add_argument("function")
            sp.add_argument("--limit", type=int, default=80)
            sp.set_defaults(func=command_inspect)
    gui = sub.add_parser("gui", help="open the desktop inspection frontend")
    gui.add_argument("root", nargs="?", default="", help="game root; it can also be selected in the window")
    gui.add_argument("--entry", default="SYSTEM_TITLE")
    gui.add_argument("--max-steps", type=int, default=30000)
    gui.add_argument("--no-auto-run", action="store_true")
    gui.add_argument("--legacy-tk", action="store_true")
    gui.set_defaults(func=command_gui)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
from typing import Any


def launch_gui(
    root: str = "",
    *,
    entry: str = "SYSTEM_TITLE",
    max_steps: int = 30_000,
    auto_run: bool = True,
    legacy_tk: bool = False,
) -> int:
    """Launch the polished Qt frontend, with the Tk inspector as fallback."""

    if legacy_tk:
        from .tk_gui import launch_gui as launch_tk

        return launch_tk(root, entry=entry, max_steps=max_steps, auto_run=auto_run)
    try:
        from .qt_gui import launch_gui as launch_qt
    except ImportError as exc:
        # Source checkouts that have not installed the GUI extra remain usable;
        # packaged/public builds declare PySide6-Essentials and use Qt.
        if getattr(exc, "name", "") not in {"PySide6", "shiboken6"}:
            raise
        from .tk_gui import launch_gui as launch_tk

        return launch_tk(root, entry=entry, max_steps=max_steps, auto_run=auto_run)
    return launch_qt(root, entry=entry, max_steps=max_steps, auto_run=auto_run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="eraMegaten Engine desktop frontend")
    parser.add_argument("root", nargs="?", default="", help="game root; it can also be selected in the window")
    parser.add_argument("--entry", default="SYSTEM_TITLE", help="entry function")
    parser.add_argument("--max-steps", type=int, default=30_000, help="maximum steps per execution slice")
    parser.add_argument("--no-auto-run", action="store_true", help="open the window without loading the supplied root")
    parser.add_argument("--legacy-tk", action="store_true", help="use the compatibility Tk inspector")
    return parser


def main(argv: list[str] | None = None) -> int:
    args: Any = build_parser().parse_args(argv)
    return launch_gui(
        args.root,
        entry=args.entry,
        max_steps=args.max_steps,
        auto_run=not args.no_auto_run,
        legacy_tk=args.legacy_tk,
    )


if __name__ == "__main__":
    raise SystemExit(main())

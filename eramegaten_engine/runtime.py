from __future__ import annotations

import random
import re
import sys
import time
import json
import struct
import html as html_lib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .builtins import BUILTINS, call_builtin, color_by_known_name, _config_raw, _parse_rgb_config
from .csvdb import parse_era_int
from .expr import Value, eval_expr, parse_lvalue, to_int, to_str, truth
from .formatter import EraFormatter
from .loader import parse_var_decl
from .memory import (
    CHARA_NUMERIC_ARRAYS,
    CHARA_STRING_ARRAYS,
    Memory,
    NUMERIC_ARRAYS,
    STRING_ARRAYS,
)
from .model import EraFunction, EraInputBlocked, Program, SourceLine, norm_name, read_text_auto, split_era_args
from .native_save import (
    SaveFileType,
    SaveFormatError,
    is_native_binary_save,
    read_legacy_text_global_save,
    read_legacy_text_save,
    read_native_save,
)


_NATIVE_SAVE_MAGIC = b"\x89ERA\r\n\x1a\n"


@dataclass(slots=True)
class ExecFrame:
    fn: EraFunction
    pc: int = 0
    loops: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class RefArg:
    base: str
    indices: tuple[Value, ...] = ()
    frame: Any | None = None


OMITTED_ARG = object()


class EraFatalError(Exception):
    """Internal control-flow exception used to stop execution immediately."""


class EraRuntime:
    def __init__(
        self,
        program: Program,
        *,
        echo: bool = True,
        interactive: bool = True,
        inputs: list[str] | None = None,
        state_dir: str | Path | None = None,
    ):
        self.program = program
        self.memory = Memory(program)
        self.formatter = EraFormatter(self)
        self.echo = echo
        self.interactive = interactive
        self.inputs = list(inputs or [])
        self.had_explicit_inputs = bool(inputs)
        self.state_dir = Path(state_dir) if state_dir is not None else program.root / ".eramegaten_engine_saves"
        self.stack: list[ExecFrame] = []
        self.output: list[str] = []
        self.debug_output: list[str] = []
        self.warnings: list[str] = []
        self.fatal_error: str | None = None
        self.waiting_for_input = False
        self.trace: list[str] = []
        self.pending_buttons: list[str] = []
        self.html_output: list[str] = []
        self.html_buttons: list[dict[str, str]] = []
        self.html_images: list[dict[str, str]] = []
        self.html_nonbuttons: list[dict[str, str]] = []
        self.html_text_runs: list[dict[str, Any]] = []
        self.print_buttons: list[dict[str, Any]] = []
        self.print_images: list[dict[str, Any]] = []
        self.print_rects: list[dict[str, Any]] = []
        self.print_spaces: list[dict[str, Any]] = []
        self.text_spans: list[dict[str, Any]] = []
        self._html_fragments: list[tuple[int, int, str]] = []
        self._html_button_lines: list[int] = []
        self._html_image_lines: list[int] = []
        self._html_nonbutton_lines: list[int] = []
        self._html_text_lines: list[int] = []
        self._print_button_lines: list[int] = []
        self._print_rect_lines: list[int] = []
        self._print_space_lines: list[int] = []
        self._html_button_styles: list[dict[str, Any]] = []
        self._html_image_styles: list[dict[str, Any]] = []
        self._html_nonbutton_styles: list[dict[str, Any]] = []
        self._print_button_styles: list[dict[str, Any]] = []
        self._print_rect_styles: list[dict[str, Any]] = []
        self._print_space_styles: list[dict[str, Any]] = []
        self._print_image_lines: list[int] = []
        self._html_visual_line_extra = 0
        self._html_raw_line_breaks: dict[int, int] = {}
        self.printc_counter = 0
        self.max_call_depth = 200
        self.current_redraw = 1
        self.current_alignment = "LEFT"
        self.skip_display = False
        self.log_skip = False
        self.bitmap_cache_enabled = False
        self.no_skip_depth = 0
        self.message_skip = False
        self.mouse_skip = False
        self.output_log_files: list[str] = []
        self.save_info_lines: list[str] = []
        self.default_font = "ＭＳ ゴシック"
        self.current_font = self.default_font
        self.current_font_style = 0
        self.default_color = _parse_rgb_config(_config_raw(self, "文字色"), 0xC0C0C0)
        self.current_color = self.default_color
        self.default_bgcolor = _parse_rgb_config(_config_raw(self, "背景色"), 0x000000)
        self.current_bgcolor = self.default_bgcolor
        self.current_tooltip_delay = 0
        self.current_tooltip_color = self.default_color
        self.force_kana_mode = 0
        self.line_width = 72
        # Front-end injectable input state for Emuera AWAIT-era polling APIs.
        # The terminal replay leaves these neutral by default; browser/GUI
        # adapters can update them before/around AWAIT ticks.
        self.is_active = True
        self.key_state: set[int] = set()
        self.key_triggered: set[int] = set()
        self.mouse_x = 0
        self.mouse_y = 0
        self.mouse_button = ""
        self.await_count = 0
        self.last_await_millis = 0
        self.timed_wait_events: list[dict[str, Any]] = []
        self.flow_input_default: str | None = None
        self.flow_input_allow_click = 0
        self.flow_input_allow_skip = 0
        self.flow_input_force_skip = 0
        self.flow_inputs_enabled = False
        self.flow_inputs_default = ""
        self.sound_effects: list[str] = []
        self.current_bgm = ""
        self.sound_volume = 100
        self.bgm_volume = 100
        self.sound_events: list[dict[str, Any]] = []
        self.graphics: dict[int, dict[str, Any]] = {}
        self.sprites: dict[str, dict[str, Any]] = {}
        self._resource_sprites: dict[str, dict[str, Any]] | None = None
        self._ordered_function_cache: dict[str, list[EraFunction]] = {}
        self._paused_native_input_many: dict[str, Any] | None = None
        self._paused_native_input_select: tuple[str, ...] | None = None
        self._paused_native_input_select_menu: tuple[str, ...] | None = None
        self._paused_native_input_split: dict[str, Any] | None = None
        self._paused_native_input_yn: tuple[str, ...] | None = None
        self._paused_native_input_onekey: tuple[str, ...] | None = None
        self._paused_native_message_window: dict[str, Any] | None = None
        self._paused_native_message_window_config: dict[str, Any] | None = None
        self._paused_native_message_window_log: dict[str, Any] | tuple[str, ...] | None = None
        self._paused_print_wait: tuple[str, ...] | None = None
        self._paused_printdata_wait: dict[str, Any] | None = None

    # ---- EvalContext API -------------------------------------------------
    def get_var(self, base: str, indices: list[Value]) -> Value:
        if norm_name(base) == "LINECOUNT" and not indices:
            return self._line_count()
        return self.memory.get_var(base, indices)

    def set_var(self, base: str, indices: list[Value], value: Value) -> None:
        self.memory.set_var(base, indices, value)

    def has_symbol(self, name: str) -> bool:
        return self.memory.has_symbol(name) or bool(self.program.get_functions(name)) or call_builtin(self, name, []) is not None if norm_name(name) == "__NEVER__" else self.memory.has_symbol(name)

    def has_callable(self, name: str) -> bool:
        key = norm_name(name)
        return bool(self._ordered_functions(key)) or key in BUILTINS

    def index_segment_should_evaluate(self, name: str) -> bool:
        return self.memory.index_segment_should_evaluate(name)

    def ref_arg_positions_for_call(self, name: str) -> set[int]:
        if norm_name(name) == "REGEXPMATCH":
            return {2, 3}
        if norm_name(name) == "ARRAYMSORTEX":
            return {0, 1}
        funcs = self._ordered_functions(name)
        fn = funcs[0] if funcs else None
        return self._ref_param_positions(fn) if fn else set()

    def make_ref_arg_for_call(self, text: str) -> RefArg | None:
        return self._make_ref_arg(text)

    def omitted_arg_for_call(self, name: str, index: int) -> Any:
        funcs = self._ordered_functions(name)
        fn = funcs[0] if funcs else None
        return OMITTED_ARG if fn and index in fn.defaults else ""

    def call_expr_function(self, name: str, args: list[Value]) -> Value:
        built = call_builtin(self, name, args)
        if built is not None:
            return built
        funcs = self._ordered_functions(name)
        fn = funcs[0] if funcs else None
        if fn:
            return self._call_sync(fn.name, args, returns_string=fn.returns_string)
        self.warn(f"unknown expression function: {name}")
        return "" if name.endswith("S") else 0

    def render_form(self, text: str) -> str:
        return self.formatter.render_form(text)

    def _render_plain_print_form(self, text: str) -> str:
        # Plain PRINT/PRINTL/PRINTW in eraMegaten sometimes uses %...% or
        # \@...\@ form snippets even without the FORM suffix, but huge AA blocks
        # also use literal braces as line art.  Render percent/conditional
        # markers for compatibility while leaving bare { } untouched unless the
        # command explicitly requested PRINTFORM* or string-expression PRINTS*.
        return self.formatter.render_form(text, render_braces=False)

    def warn(self, message: str) -> None:
        if len(self.warnings) < 2000:
            self.warnings.append(message)

    def _fatal(self, message: str) -> None:
        self.fatal_error = message
        self.warn(message)
        self.stack.clear()
        self.memory.frames.clear()
        raise EraFatalError(message)

    # ---- public -----------------------------------------------------------
    def run(self, entry: str = "SYSTEM_TITLE", *, max_steps: int = 100000) -> int:
        self.fatal_error = None
        self.waiting_for_input = False
        if not self._push_call_sequence(entry, []):
            raise KeyError(f"entry function not found: {entry}")
        return self._run_loop(max_steps=max_steps, stop_depth=0)

    def continue_run(self, *, max_steps: int = 100000) -> int:
        """Continue an existing paused call stack.

        Non-interactive GUI adapters can run until an input point, inspect the
        page/layout model, call ``queue_html_click`` or append to ``inputs``,
        then continue without re-entering the scenario from the beginning.
        """

        if not self.stack:
            return 0
        self.fatal_error = None
        self.waiting_for_input = False
        return self._run_loop(max_steps=max_steps, stop_depth=0)

    def inspect_function(self, name: str, limit: int = 80) -> str:
        fn = self.program.get_function(name)
        if not fn:
            return f"Function not found: {name}"
        rows = [f"@{fn.name} ({self.program.file_of(fn)}:{fn.source_line})"]
        for i, line in enumerate(fn.lines[:limit]):
            rows.append(f"{i:04d} {line.number:05d}: {line.text}")
        if len(fn.lines) > limit:
            rows.append(f"... {len(fn.lines) - limit} more lines")
        return "\n".join(rows)

    # ---- call/return ------------------------------------------------------
    MULTI_EVENT_FUNCTIONS = {
        "EVENTFIRST", "EVENTSHOP", "EVENTTRAIN", "EVENTCOM", "EVENTCOMEND",
        "EVENTTURNEND", "EVENTEND", "EVENTLOAD",
    }

    def _ordered_functions(self, name: str) -> list[EraFunction]:
        key = norm_name(name)
        cached = self._ordered_function_cache.get(key)
        if cached is None:
            cached = sorted(self.program.functions.get(key, []), key=lambda f: (-f.priority, f.later, f.source_file, f.source_line))
            self._ordered_function_cache[key] = cached
        return cached

    def _is_multi_event(self, name: str) -> bool:
        return norm_name(name) in self.MULTI_EVENT_FUNCTIONS

    def _push_call(self, name: str, args: list[Value], *, try_only: bool = False) -> bool:
        funcs = self._ordered_functions(name)
        if not funcs:
            if not try_only:
                self.warn(f"function not found: {name}")
            return False
        return self._push_function(funcs[0], args)

    def _push_call_sequence(self, name: str, args: list[Value], *, try_only: bool = False) -> bool:
        funcs = self._ordered_functions(name)
        if not funcs:
            if not try_only:
                self.warn(f"function not found: {name}")
            return False
        if not self._is_multi_event(name):
            return self._push_function(funcs[0], args)
        if len(self.stack) + len(funcs) > self.max_call_depth:
            self.warn(f"max call depth exceeded at {name}")
            return False
        for fn in reversed(funcs):
            self._push_function(fn, args)
        return True

    def _push_shop_loop(self) -> bool:
        if not (self.program.get_function("SHOW_SHOP") or self.program.get_function("USERSHOP")):
            return False
        fn = EraFunction(
            name="__ENGINE_SHOP_LOOP",
            lines=[
                SourceLine("$LOOP", -1, 0),
                SourceLine("CALL SHOW_SHOP", -1, 0),
                SourceLine("__SHOPINPUT", -1, 0),
                SourceLine("CALL USERSHOP", -1, 0),
                SourceLine("CALL EVENTSHOP", -1, 0),
                SourceLine("GOTO LOOP", -1, 0),
            ],
            labels={"LOOP": 0},
            source_file=-1,
            source_line=0,
        )
        return self._push_function(fn, [])

    def _push_train_loop(self) -> bool:
        if not (
            self.program.get_function("EVENTTRAIN")
            or self.program.get_function("SHOW_STATUS")
            or self.program.get_function("SHOW_USERCOM")
            or self.program.get_function("USERCOM")
        ):
            return False
        fn = EraFunction(
            name="__ENGINE_TRAIN_LOOP",
            lines=[
                SourceLine("TRYCALL EVENTTRAIN", -1, 0),
                SourceLine("$LOOP", -1, 0),
                SourceLine("TRYCALL SHOW_STATUS", -1, 0),
                SourceLine("TRYCALL SHOW_USERCOM", -1, 0),
                SourceLine("__TRAININPUT", -1, 0),
                SourceLine("TRYCALL USERCOM", -1, 0),
                SourceLine("GOTO LOOP", -1, 0),
            ],
            labels={"LOOP": 1},
            source_file=-1,
            source_line=0,
        )
        return self._push_function(fn, [])

    def _push_dotrain_flow(self, command: int) -> None:
        command = int(command)
        self.memory.set_var("SELECTCOM", [], command)
        # Emuera's DOTRAIN is a training command phase, not a plain COM call:
        # it runs EVENTCOM, then the selected command, the source/palam
        # settlement hook, and finally EVENTCOMEND.  eraMegaten funnels actual
        # commands through COM_COMMON -> ACT_COMn, so keep SELECTCOM dynamic
        # after EVENTCOM has had a chance to rewrite it.
        if self.program.get_function("COM_COMMON"):
            action = "CALL COM_COMMON, SELECTCOM"
        elif self.program.get_function(f"ACT_COM{command}"):
            action = f"CALL ACT_COM{command}"
        else:
            action = f"CALL COM{command}"
        fn = EraFunction(
            name="__ENGINE_DOTRAIN_FLOW",
            lines=[
                SourceLine("TRYCALL EVENTCOM", -1, 0),
                SourceLine(action, -1, 0),
                SourceLine("TRYCALL SOURCE_CHECK", -1, 0),
                SourceLine("TRYCALL EVENTCOMEND", -1, 0),
            ],
            source_file=-1,
            source_line=0,
        )
        self._push_function(fn, [])

    def _push_function(self, fn: EraFunction, args: list[Value]) -> bool:
        if len(self.stack) >= self.max_call_depth:
            self.warn(f"max call depth exceeded at {fn.name}")
            return False
        self.stack.append(ExecFrame(fn=fn))
        applied = self._apply_defaults(fn, args)
        frame_args: list[Value] = [0 if isinstance(arg, RefArg) else arg for arg in applied]
        self.memory.push_frame(fn.name, frame_args)
        self._init_frame_locals(fn)
        for i, param in enumerate(fn.params):
            if i < len(applied):
                self._bind_param(param, applied[i])
        return True

    def _apply_defaults(self, fn: EraFunction, args: list[Any]) -> list[Any]:
        out = list(args)
        if fn.defaults:
            max_index = max(fn.defaults)
            while len(out) <= max_index:
                out.append(OMITTED_ARG)
            for i, default_expr in fn.defaults.items():
                if out[i] is OMITTED_ARG:
                    out[i] = eval_expr(self, default_expr)
        out = ["" if arg is OMITTED_ARG else arg for arg in out]
        return out

    def _return(self, value: Value | None = None) -> None:
        self._return_values([] if value is None else [value])

    def _return_values(self, values: list[Value]) -> None:
        for i, value in enumerate(values):
            self._store_return_value(value, i)
        if self.stack:
            self.stack.pop()
            self.memory.pop_frame()

    def _store_return_value(self, value: Value, index: int) -> None:
        if isinstance(value, str):
            self.memory.set_var("RESULTS", [index], value)
            if index == 0:
                self.memory.set_var("RESULTS", [], value)
        else:
            ivalue = to_int(value)
            self.memory.set_var("RESULT", [index], ivalue)
            if index == 0:
                self.memory.set_var("RESULT", [], ivalue)

    def _init_frame_locals(self, fn: EraFunction) -> None:
        fr = self.memory.frame
        if fr is None:
            return
        if fn.local_size_expr:
            fr.dims["LOCAL"] = (max(0, to_int(eval_expr(self, fn.local_size_expr, default=0))),)
        if fn.locals_size_expr:
            fr.dims["LOCALS"] = (max(0, to_int(eval_expr(self, fn.locals_size_expr, default=0))),)
        for line in fn.lines:
            text = line.text.strip()
            decl = parse_var_decl(text)
            if not decl:
                continue
            key = norm_name(decl.name)
            dims = self._resolve_local_decl_dims(decl)
            fr.dims[key] = dims
            local_table = not (decl.global_scope or decl.savedata or decl.charadata)
            if local_table and decl.is_string:
                fr.strings.setdefault(key, {})
            elif local_table:
                fr.numeric.setdefault(key, {})
            elif decl.is_string:
                self.memory.strings.setdefault(key, {})
            else:
                self.memory.numeric.setdefault(key, {})
            if not decl.initial:
                continue
            default: Value = "" if decl.is_string else 0
            values = [eval_expr(self, raw, default=default) if raw.strip() else default for raw in decl.initial]
            if len(values) == 1 and not dims:
                self.memory.set_var(decl.name, [], values[0])
                self.memory.set_var(decl.name, [0], values[0])
                continue
            for i, value in enumerate(values):
                self.memory.set_var(decl.name, [i], value)
            if values and not dims:
                self.memory.set_var(decl.name, [], values[0])

    def _resolve_local_decl_dims(self, decl) -> tuple[int, ...]:
        if getattr(decl, "dim_exprs", ()):
            return tuple(max(0, to_int(eval_expr(self, expr, default=0))) for expr in decl.dim_exprs)
        return tuple(max(0, int(dim)) for dim in decl.dims)

    def _bind_param(self, param: str, value: Value) -> None:
        fr = self.memory.frame
        if fr is None:
            return
        p = param.strip()
        if not p:
            return
        # Remove declaration-like flags occasionally used in Emuera headers.
        words = p.split()
        while words and norm_name(words[0]) in {"REF", "DYNAMIC"}:
            words.pop(0)
        p = " ".join(words)
        if not p:
            return
        try:
            ref = parse_lvalue(self, p)
        except Exception:
            return
        key = norm_name(ref.base)
        if isinstance(value, RefArg):
            src_key = norm_name(value.base)
            if (value.frame is not None and src_key in value.frame.ref_aliases) or value.indices:
                ref_alias = self.memory.create_ref_alias(value.base, value.indices, value.frame)
                fr.ref_aliases[key] = ref_alias
                if ref_alias.dims:
                    fr.dims[key] = ref_alias.dims
                if ref_alias.is_string:
                    fr.strings.setdefault(key, {})
                    fr.numeric.pop(key, None)
                else:
                    fr.numeric.setdefault(key, {})
                    fr.strings.pop(key, None)
                return
            if value.frame is not None:
                src_frame = value.frame
                if src_key in src_frame.strings:
                    fr.strings[key] = src_frame.strings.setdefault(src_key, {})
                    fr.numeric.pop(key, None)
                else:
                    fr.numeric[key] = src_frame.numeric.setdefault(src_key, {})
                    fr.strings.pop(key, None)
                if src_key in src_frame.dims:
                    fr.dims[key] = src_frame.dims[src_key]
            else:
                if self.memory.is_string_base(value.base):
                    fr.strings[key] = self.memory.strings.setdefault(src_key, {})
                    fr.numeric.pop(key, None)
                else:
                    fr.numeric[key] = self.memory.numeric.setdefault(src_key, {})
                    fr.strings.pop(key, None)
                decl = self.program.var_decls.get(src_key)
                if decl:
                    fr.dims[key] = decl.dims
            return
        idx = tuple(self.memory.resolve_indices(key, ref.indices))
        is_numeric_target = key in NUMERIC_ARRAYS or (key in fr.numeric and key not in fr.strings)
        is_string_target = (
            key in STRING_ARRAYS
            or key.startswith("ARGS")
            or key in fr.strings
            or (isinstance(value, str) and not is_numeric_target)
        )
        if is_string_target:
            table = fr.strings.setdefault(key, {})
            table[idx] = to_str(value)
            if key in {"ARGS", "LOCALS"} and idx == ():
                table[(0,)] = to_str(value)
        else:
            table = fr.numeric.setdefault(key, {})
            table[idx] = to_int(value)
            if key in {"ARG", "LOCAL"} and idx == ():
                table[(0,)] = to_int(value)

    def _call_sync(self, name: str, args: list[Value], *, returns_string: bool = False, max_steps: int = 20000) -> Value:
        depth = len(self.stack)
        if not self._push_call(name, args, try_only=True):
            return "" if returns_string else 0
        old_result = dict(self.memory.numeric.get("RESULT", {}))
        old_results = dict(self.memory.strings.get("RESULTS", {}))
        # Expression functions start with a fresh return slot.  If a #FUNCTION
        # falls through without RETURNF, Emuera-compatible callers should see
        # the neutral default instead of whatever RESULT happened to contain in
        # the caller (important for predicates such as 魔晶装備(LOCAL)).
        self.memory.numeric.setdefault("RESULT", {})[()] = 0
        self.memory.strings.setdefault("RESULTS", {})[()] = ""
        self._run_loop(max_steps=max_steps, stop_depth=depth)
        if self.fatal_error:
            self.memory.numeric["RESULT"] = old_result
            self.memory.strings["RESULTS"] = old_results
            raise EraFatalError(self.fatal_error)
        value = self.memory.get_var("RESULTS", []) if returns_string else self.memory.get_var("RESULT", [])
        self.memory.numeric["RESULT"] = old_result
        self.memory.strings["RESULTS"] = old_results
        return value

    def _run_loop(self, *, max_steps: int, stop_depth: int) -> int:
        steps = 0
        while len(self.stack) > stop_depth and steps < max_steps and not self.fatal_error and not self.waiting_for_input:
            frame = self.stack[-1]
            if frame.pc >= len(frame.fn.lines):
                self._return()
                continue
            line = frame.fn.lines[frame.pc]
            steps += 1
            try:
                self._execute_line(frame, line)
            except EraFatalError:
                break
            except EraInputBlocked:
                self.waiting_for_input = True
                break
            except Exception as exc:
                self.warn(f"{self.program.file_of(line)}:{line.number}: {line.text}: {exc}")
                frame.pc += 1
        if steps >= max_steps and not self.fatal_error:
            self.warn(f"max step limit reached ({max_steps})")
        return steps

    # ---- rendering/input --------------------------------------------------
    def _display_suppressed(self) -> bool:
        return self.skip_display and self.no_skip_depth <= 0

    def _write(self, text: str, newline: bool = False, *, harvest_buttons: bool = True, record_style: bool = True) -> None:
        if self._display_suppressed():
            return
        if harvest_buttons and not self.interactive:
            for m in re.finditer(r"\[\s*([+-]?\d{1,6})\s*\]", text):
                self.pending_buttons.append(m.group(1))
        if record_style:
            self._record_output_text(text, newline=newline)
        if newline:
            self.output.append(text + "\n")
            if self.echo:
                print(text)
        else:
            self.output.append(text)
            if self.echo:
                print(text, end="")

    def _style_snapshot(self) -> dict[str, Any]:
        return {
            "color": self.current_color,
            "bgcolor": self.current_bgcolor,
            "font": self.current_font,
            "font_style": self.current_font_style,
            "alignment": self.current_alignment,
            "tooltip_delay": self.current_tooltip_delay,
            "tooltip_color": self.current_tooltip_color,
        }

    def _next_visual_write_start_line(self) -> int:
        return self._next_write_start_line() + self._html_visual_line_extra

    def _record_output_text(self, text: str, *, newline: bool) -> None:
        """Record styled text spans for GUI/front-end renderers.

        The terminal transcript deliberately stays as plain strings, but a
        modern renderer needs to know which fragments were printed under the
        current Emuera font/color/alignment state.  Store spans by visible
        display line and column so page/layout models can expose them without
        altering legacy `output` consumers.
        """

        rendered = text + ("\n" if newline else "")
        if rendered == "":
            return
        existing = "".join(self.output)
        display_line = self._next_visual_write_start_line()
        col = 0 if not existing or existing.endswith("\n") else self._layout_text_width(existing.rsplit("\n", 1)[-1])
        style = self._style_snapshot()
        start = 0
        for idx, ch in enumerate(rendered):
            if ch != "\n":
                continue
            if idx > start:
                segment = rendered[start:idx]
                self.text_spans.append(
                    {
                        "line": display_line - 1,
                        "display_line": display_line,
                        "col": col,
                        "text": segment,
                        **style,
                    }
                )
                col += self._layout_text_width(segment)
            display_line += 1
            col = 0
            start = idx + 1
        if start < len(rendered):
            segment = rendered[start:]
            self.text_spans.append(
                {
                    "line": display_line - 1,
                    "display_line": display_line,
                    "col": col,
                    "text": segment,
                    **style,
                }
            )

    def _clear_visible_buttons(self) -> None:
        self.pending_buttons.clear()
        self.html_buttons.clear()
        self.html_images.clear()
        self.html_nonbuttons.clear()
        self.html_text_runs.clear()
        self.print_buttons.clear()
        self.print_images.clear()
        self.print_rects.clear()
        self.print_spaces.clear()
        self._html_button_lines.clear()
        self._html_image_lines.clear()
        self._html_nonbutton_lines.clear()
        self._html_text_lines.clear()
        self._print_button_lines.clear()
        self._print_rect_lines.clear()
        self._print_space_lines.clear()
        self._html_button_styles.clear()
        self._html_image_styles.clear()
        self._html_nonbutton_styles.clear()
        self._print_button_styles.clear()
        self._print_rect_styles.clear()
        self._print_space_styles.clear()
        self._print_image_lines.clear()

    def _trim_text_spans_to_line_count(self, keep_lines: int) -> None:
        keep_lines = max(0, int(keep_lines))
        self.text_spans = [
            span
            for span in self.text_spans
            if 1 <= int(span.get("display_line", 0)) <= keep_lines
        ]

    def _line_count(self) -> int:
        text = "".join(self.output)
        if not text:
            return 0
        return text.count("\n") + (0 if text.endswith("\n") else 1)

    def _display_lines(self) -> list[str]:
        text = "".join(self.output)
        if not text:
            return []
        return text.splitlines()

    def html_page_model(self) -> dict[str, Any]:
        """Return the current visible page with HTML metadata grouped by line.

        The legacy transcript-oriented attributes (`output`, `html_buttons`,
        `html_images`, `html_nonbuttons`) are flat lists.  A modern GUI needs a
        stable snapshot that ties those elements back to the visible display
        rows after CLEARLINE/REUSELASTLINE redraws.  Internal HTML line markers
        are 1-based because they track Emuera visible line counts; the returned
        rows expose both 0-based `index` and 1-based `display_line`.
        """

        display_lines = self._display_lines()
        max_line = len(display_lines)
        line_sources: list[int] = []
        line_sources.extend(self._html_button_lines)
        line_sources.extend(self._html_image_lines)
        line_sources.extend(self._html_nonbutton_lines)
        line_sources.extend(self._html_text_lines)
        line_sources.extend(self._print_button_lines)
        line_sources.extend(self._print_rect_lines)
        line_sources.extend(self._print_space_lines)
        line_sources.extend(self._print_image_lines)
        line_sources.extend(end for _, end, _ in self._html_fragments)
        if line_sources:
            max_line = max(max_line, max(0, *line_sources))

        rows: list[dict[str, Any]] = []
        for idx in range(max_line):
            rows.append(
                {
                    "index": idx,
                    "display_line": idx + 1,
                    "text": display_lines[idx] if idx < len(display_lines) else "",
                    "style_spans": [],
                    "html": [],
                    "buttons": [],
                    "images": [],
                    "nonbuttons": [],
                    "html_text": [],
                    "print_buttons": [],
                    "print_rects": [],
                    "print_spaces": [],
                    "print_images": [],
                }
            )

        def ensure_row(display_line: int) -> dict[str, Any]:
            idx = max(0, int(display_line) - 1)
            while idx >= len(rows):
                rows.append(
                    {
                        "index": len(rows),
                        "display_line": len(rows) + 1,
                        "text": "",
                        "style_spans": [],
                        "html": [],
                        "buttons": [],
                        "images": [],
                        "nonbuttons": [],
                        "html_text": [],
                        "print_buttons": [],
                        "print_rects": [],
                        "print_spaces": [],
                        "print_images": [],
                    }
                )
            return rows[idx]

        fragments: list[dict[str, Any]] = []
        for start, end, html in self._html_fragments:
            item = {
                "line": max(0, start - 1),
                "end_line": max(0, end - 1),
                "display_line": start,
                "display_end_line": end,
                "html": html,
            }
            fragments.append(item)
            ensure_row(start)["html"].append(item)

        style_spans: list[dict[str, Any]] = []
        for span in self.text_spans:
            item = dict(span)
            display_line = int(item.get("display_line", 0))
            if display_line <= 0:
                continue
            item["line"] = max(0, display_line - 1)
            style_spans.append(item)
            ensure_row(display_line)["style_spans"].append(item)

        def attach(
            elements: list[dict[str, Any]],
            element_lines: list[int],
            key: str,
            styles: list[dict[str, Any]] | None = None,
        ) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for i, (element, display_line) in enumerate(zip(elements, element_lines)):
                style = styles[i] if styles is not None and i < len(styles) else {}
                item: dict[str, Any] = dict(element)
                item.update(style)
                item["line"] = max(0, int(display_line) - 1)
                item["display_line"] = int(display_line)
                out.append(item)
                ensure_row(int(display_line))[key].append(item)
            return out

        return {
            "lines": rows,
            "style_spans": style_spans,
            "html": fragments,
            "buttons": attach(self.html_buttons, self._html_button_lines, "buttons", self._html_button_styles),
            "images": attach(self.html_images, self._html_image_lines, "images", self._html_image_styles),
            "nonbuttons": attach(self.html_nonbuttons, self._html_nonbutton_lines, "nonbuttons", self._html_nonbutton_styles),
            "html_text": attach(self.html_text_runs, self._html_text_lines, "html_text"),
            "print_buttons": attach(self.print_buttons, self._print_button_lines, "print_buttons", self._print_button_styles),
            "print_rects": attach(self.print_rects, self._print_rect_lines, "print_rects", self._print_rect_styles),
            "print_spaces": attach(self.print_spaces, self._print_space_lines, "print_spaces", self._print_space_styles),
            "print_images": attach(self.print_images, self._print_image_lines, "print_images"),
        }

    def html_layout_model(
        self,
        *,
        char_width: int = 1,
        line_height: int = 1,
        viewport_width: int | None = None,
        html_unit_scale: float = 1.0,
    ) -> dict[str, Any]:
        """Return a simple drawable coordinate model for the current page.

        This is intentionally a front-end bridge rather than a full browser
        layout engine.  It preserves Emuera HTML metadata and maps the pieces
        eraMegaten actually relies on to coordinates:

        * display row -> ``y = line * line_height``
        * ``pos`` / parent ``pos`` -> x coordinate
        * ``ypos`` -> image y offset from the row baseline
        * explicit ``width/height`` fall back to natural sprite size

        The result is suitable for modern GUI adapters that want to draw the
        current page without reparsing the flat transcript and HTML fragments.
        Emuera/TextRenderer HTML numeric attributes are often expressed in
        font-relative units (100 == one configured font size); pass
        ``html_unit_scale=font_size/100`` to convert explicit HTML ``pos``,
        ``width``, ``height`` and ``ypos`` to pixels while leaving legacy
        callers at the previous 1:1 bridge scale.
        """

        char_width = max(1, int(char_width))
        line_height = max(1, int(line_height))
        viewport = max(0, int(viewport_width or 0))
        try:
            html_scale = float(html_unit_scale)
        except Exception:
            html_scale = 1.0
        if html_scale <= 0:
            html_scale = 1.0
        page = self.html_page_model()

        def int_attr(value: Any, default: int = 0) -> int:
            if value is None:
                return default
            text = str(value).strip()
            if text == "":
                return default
            try:
                return int(float(text))
            except Exception:
                return default

        def sized(element: dict[str, Any]) -> tuple[int, int]:
            explicit_width = str(element.get("width", "")).strip() != ""
            explicit_height = str(element.get("height", "")).strip() != ""
            natural_width = int_attr(element.get("natural_width"), 0)
            natural_height = int_attr(element.get("natural_height"), 0)
            width = html_unit_attr(element.get("width")) if explicit_width else natural_width
            height = html_unit_attr(element.get("height")) if explicit_height else natural_height
            if explicit_width and not explicit_height and natural_width > 0 and natural_height > 0:
                height = int(round(width * natural_height / natural_width))
            elif explicit_height and not explicit_width and natural_width > 0 and natural_height > 0:
                width = int(round(height * natural_width / natural_height))
            if height <= 0:
                height = line_height
            return max(0, width), max(0, height)

        def html_unit_attr(value: Any, default: int = 0) -> int:
            return int(round(int_attr(value, default) * html_scale))

        rows: list[dict[str, Any]] = []
        drawables: list[dict[str, Any]] = []
        row_cursors: dict[int, int] = {}
        max_x = 0
        max_y = 0

        def add_drawable(item: dict[str, Any]) -> None:
            nonlocal max_x, max_y
            drawables.append(item)
            max_x = max(max_x, int_attr(item.get("x")) + int_attr(item.get("width")))
            max_y = max(max_y, int_attr(item.get("y")) + int_attr(item.get("height")))

        for row in page["lines"]:
            line_index = int_attr(row.get("index"))
            base_y = line_index * line_height
            row_model = {
                "index": line_index,
                "display_line": row.get("display_line", line_index + 1),
                "y": base_y,
                "height": line_height,
                "text": row.get("text", ""),
                "style_spans": row.get("style_spans", []),
            }
            rows.append(row_model)
            cursor = row_cursors.setdefault(line_index, 0)
            text = str(row.get("text", ""))
            row_spaces = sorted(
                [space for space in row.get("print_spaces", []) if isinstance(space, dict)],
                key=lambda space: (int_attr(space.get("col")), int_attr(space.get("cells"))),
            )
            row_image_spaces: list[dict[str, int]] = []
            for image_index, image in enumerate(row.get("images", [])):
                if not isinstance(image, dict):
                    continue
                explicit_x_text = str(image.get("pos", "")).strip() or str(image.get("parent_pos", "")).strip()
                if explicit_x_text or not str(image.get("col", "")).strip():
                    continue
                width, _ = sized(image)
                raw_width = int_attr(image.get("width")) if str(image.get("width", "")).strip() else int_attr(image.get("natural_width"))
                fallback_cells = max(1, (max(0, raw_width) + 7) // 8) if raw_width else 1
                row_image_spaces.append(
                    {
                        "col": max(0, int_attr(image.get("col"))),
                        "cells": fallback_cells,
                        "target_width": max(0, width),
                        "index": image_index,
                    }
                )
            row_image_spaces.sort(key=lambda space: (space["col"], space["index"]))

            def adjusted_x_for_col(col_value: Any, *, space_limit: int | None = None) -> int:
                col = max(0, int_attr(col_value))
                delta = 0
                active_spaces = row_spaces if space_limit is None else row_spaces[: max(0, space_limit)]
                for space in active_spaces:
                    space_col = max(0, int_attr(space.get("col")))
                    cells = max(0, int_attr(space.get("cells")))
                    if col >= space_col + cells:
                        fallback_width = cells * char_width
                        target_width = max(0, html_unit_attr(space.get("width")))
                        delta += target_width - fallback_width
                for space in row_image_spaces:
                    space_col = max(0, int_attr(space.get("col")))
                    cells = max(1, int_attr(space.get("cells"), 1))
                    if col >= space_col + cells:
                        fallback_width = cells * char_width
                        target_width = max(0, int_attr(space.get("target_width")))
                        delta += target_width - fallback_width
                return col * char_width + delta

            def tooltip_attrs(element: dict[str, Any]) -> dict[str, int]:
                return {
                    "tooltip_delay": int_attr(element.get("tooltip_delay"), self.current_tooltip_delay),
                    "tooltip_color": int_attr(element.get("tooltip_color"), self.current_tooltip_color),
                }

            for space_index, space in enumerate(row_spaces):
                col = max(0, int_attr(space.get("col")))
                width = max(0, html_unit_attr(space.get("width")))
                x = adjusted_x_for_col(col, space_limit=space_index)
                add_drawable(
                    {
                        "type": "print_space",
                        "line": line_index,
                        "display_line": row_model["display_line"],
                        "x": x,
                        "y": base_y,
                        "width": width,
                        "height": line_height,
                        "col": col,
                        "cells": max(0, int_attr(space.get("cells"))),
                        "raw_width": max(0, int_attr(space.get("width"))),
                        "color": int_attr(space.get("color"), self.current_color),
                        "bgcolor": int_attr(space.get("bgcolor"), self.current_bgcolor),
                        "font": space.get("font", self.current_font),
                        "font_style": int_attr(space.get("font_style"), 0),
                        "alignment": space.get("alignment", ""),
                        **tooltip_attrs(space),
                        "explicit_x": False,
                    }
                )
                cursor = max(cursor, x + width)
            spans = [span for span in row.get("style_spans", []) if str(span.get("text", "")) != ""]
            if spans:
                for span in spans:
                    segment = str(span.get("text", ""))
                    add_drawable(
                        {
                            "type": "text",
                            "line": line_index,
                            "display_line": row_model["display_line"],
                            "x": adjusted_x_for_col(span.get("col")),
                            "y": base_y,
                            "width": self._layout_text_width(segment) * char_width,
                            "height": line_height,
                            "text": segment,
                            "color": int_attr(span.get("color")),
                            "bgcolor": int_attr(span.get("bgcolor")),
                            "font": span.get("font", ""),
                            "font_style": int_attr(span.get("font_style")),
                            "alignment": span.get("alignment", ""),
                            **tooltip_attrs(span),
                            "explicit_x": False,
                        }
                    )
            elif text and not row.get("html"):
                add_drawable(
                    {
                        "type": "text",
                        "line": line_index,
                        "display_line": row_model["display_line"],
                        "x": 0,
                        "y": base_y,
                        "width": self._layout_text_width(text) * char_width,
                        "height": line_height,
                        "text": text,
                        **self._style_snapshot(),
                        "explicit_x": False,
                    }
                )
            for button in row.get("print_buttons", []):
                label = str(button.get("label", ""))
                if not label:
                    continue
                x = adjusted_x_for_col(button.get("col"))
                width = self._layout_text_width(label) * char_width
                add_drawable(
                    {
                        "type": "print_button",
                        "line": line_index,
                        "display_line": row_model["display_line"],
                        "x": x,
                        "y": base_y,
                        "width": width,
                        "height": line_height,
                        "value": button.get("value", ""),
                        "label": label,
                        "color": int_attr(button.get("color"), self.current_color),
                        "bgcolor": int_attr(button.get("bgcolor"), self.current_bgcolor),
                        "font": button.get("font", self.current_font),
                        "font_style": int_attr(button.get("font_style"), 0),
                        "alignment": button.get("alignment", ""),
                        **tooltip_attrs(button),
                        "explicit_x": False,
                    }
                )
                cursor = max(cursor, x + width)
            for rect in row.get("print_rects", []):
                x = adjusted_x_for_col(rect.get("col"))
                width = max(char_width, int_attr(rect.get("width")))
                height = max(line_height, int_attr(rect.get("height"), line_height))
                add_drawable(
                    {
                        "type": "print_rect",
                        "line": line_index,
                        "display_line": row_model["display_line"],
                        "x": x,
                        "y": base_y,
                        "width": width,
                        "height": height,
                        "color": int_attr(rect.get("color"), self.current_color),
                        "bgcolor": int_attr(rect.get("bgcolor"), self.current_bgcolor),
                        "font": rect.get("font", self.current_font),
                        "font_style": int_attr(rect.get("font_style"), 0),
                        "alignment": rect.get("alignment", ""),
                        **tooltip_attrs(rect),
                        "explicit_x": False,
                    }
                )
                cursor = max(cursor, x + width)
            for button in row.get("buttons", []):
                explicit_x = str(button.get("pos", "")).strip() != ""
                x = html_unit_attr(button.get("pos")) if explicit_x else adjusted_x_for_col(button.get("col")) if "col" in button else cursor
                label = str(button.get("label", ""))
                width = self._layout_text_width(label) * char_width if label else 0
                add_drawable(
                    {
                        "type": "button",
                        "line": line_index,
                        "display_line": row_model["display_line"],
                        "x": x,
                        "y": base_y,
                        "width": width,
                        "height": line_height,
                        "value": button.get("value", ""),
                        "title": button.get("title", ""),
                        "label": label,
                        "color": int_attr(button.get("color"), self.current_color),
                        "bgcolor": int_attr(button.get("bgcolor"), self.current_bgcolor),
                        "font": button.get("font", self.current_font),
                        "font_style": int_attr(button.get("font_style"), 0),
                        "alignment": button.get("alignment", ""),
                        **tooltip_attrs(button),
                        "explicit_x": explicit_x,
                    }
                )
                cursor = max(cursor, x + width)
            for nonbutton in row.get("nonbuttons", []):
                explicit_x = str(nonbutton.get("pos", "")).strip() != ""
                x = html_unit_attr(nonbutton.get("pos")) if explicit_x else adjusted_x_for_col(nonbutton.get("col")) if "col" in nonbutton else cursor
                label = str(nonbutton.get("label", ""))
                width = self._layout_text_width(label) * char_width if label else 0
                add_drawable(
                    {
                        "type": "nonbutton",
                        "line": line_index,
                        "display_line": row_model["display_line"],
                        "x": x,
                        "y": base_y,
                        "width": width,
                        "height": line_height,
                        "title": nonbutton.get("title", ""),
                        "label": label,
                        "color": int_attr(nonbutton.get("color"), self.current_color),
                        "bgcolor": int_attr(nonbutton.get("bgcolor"), self.current_bgcolor),
                        "font": nonbutton.get("font", self.current_font),
                        "font_style": int_attr(nonbutton.get("font_style"), 0),
                        "alignment": nonbutton.get("alignment", ""),
                        **tooltip_attrs(nonbutton),
                        "explicit_x": explicit_x,
                    }
                )
                cursor = max(cursor, x + width)
            for run in row.get("html_text", []):
                segment = str(run.get("text", ""))
                if not segment:
                    continue
                x = adjusted_x_for_col(run.get("col"))
                add_drawable(
                    {
                        "type": "html_text",
                        "line": line_index,
                        "display_line": row_model["display_line"],
                        "x": x,
                        "y": base_y,
                        "width": self._layout_text_width(segment) * char_width,
                        "height": line_height,
                        "text": segment,
                        "color": int_attr(run.get("color"), self.current_color),
                        "bgcolor": int_attr(run.get("bgcolor"), self.current_bgcolor),
                        "font": run.get("font", self.current_font),
                        "font_style": int_attr(run.get("font_style"), 0),
                        "alignment": run.get("alignment", ""),
                        **tooltip_attrs(run),
                        "explicit_x": False,
                    }
                )
                cursor = max(cursor, x + self._layout_text_width(segment) * char_width)
            for image in row.get("images", []):
                width, height = sized(image)
                explicit_x_text = str(image.get("pos", "")).strip() or str(image.get("parent_pos", "")).strip()
                explicit_x = bool(explicit_x_text)
                x = html_unit_attr(explicit_x_text) if explicit_x_text else adjusted_x_for_col(image.get("col")) if str(image.get("col", "")).strip() else cursor
                y = base_y + html_unit_attr(image.get("ypos"), 0)
                add_drawable(
                    {
                        "type": "image",
                        "line": line_index,
                        "display_line": row_model["display_line"],
                        "x": x,
                        "y": y,
                        "width": width,
                        "height": height,
                        "src": image.get("src", ""),
                        "title": image.get("title", ""),
                        "natural_width": int_attr(image.get("natural_width")),
                        "natural_height": int_attr(image.get("natural_height")),
                        "parent": image.get("parent", ""),
                        "parent_value": image.get("parent_value", ""),
                        "parent_title": image.get("parent_title", ""),
                        "color": int_attr(image.get("color"), self.current_color),
                        "bgcolor": int_attr(image.get("bgcolor"), self.current_bgcolor),
                        "font": image.get("font", self.current_font),
                        "font_style": int_attr(image.get("font_style"), 0),
                        "alignment": image.get("alignment", ""),
                        **tooltip_attrs(image),
                        "explicit_x": explicit_x,
                    }
                )
                cursor = max(cursor, x + width)
            for image in row.get("print_images", []):
                width = int_attr(image.get("width"))
                height = int_attr(image.get("height"), line_height)
                x = adjusted_x_for_col(image.get("col")) if "col" in image else 0
                add_drawable(
                    {
                        "type": "print_image",
                        "line": line_index,
                        "display_line": row_model["display_line"],
                        "x": x,
                        "y": base_y,
                        "width": width,
                        "height": height,
                        "src": image.get("src", ""),
                        "explicit_x": False,
                    }
                )
                cursor = max(cursor, x + width)
            row_cursors[line_index] = cursor

        def expand_html_control_bounds() -> None:
            images = [item for item in drawables if item.get("type") == "image" and item.get("parent") in {"button", "nonbutton"}]
            if not images:
                return
            controls = [item for item in drawables if item.get("type") in {"button", "nonbutton"}]
            for control in controls:
                kind = str(control.get("type", ""))
                control_line = int_attr(control.get("line"))
                for image in sorted(images, key=lambda item: (int_attr(item.get("line")), int_attr(item.get("x")), int_attr(item.get("y")))):
                    if image.get("parent") != kind or int_attr(image.get("line")) != control_line:
                        continue
                    if kind == "button" and to_str(image.get("parent_value", "")) != to_str(control.get("value", "")):
                        continue
                    if to_str(image.get("parent_title", "")) != to_str(control.get("title", "")):
                        continue
                    cx = int_attr(control.get("x"))
                    cy = int_attr(control.get("y"))
                    cw = int_attr(control.get("width"))
                    ch = int_attr(control.get("height"))
                    ix = int_attr(image.get("x"))
                    iy = int_attr(image.get("y"))
                    iw = int_attr(image.get("width"))
                    ih = int_attr(image.get("height"))
                    if iw <= 0 or ih <= 0:
                        continue
                    # Avoid binding duplicate same-valued controls to distant
                    # sibling images.  Image-only controls start at the image x;
                    # mixed label/image controls place later images immediately
                    # after the currently known control width, so update the
                    # union incrementally as images are accepted.
                    right = cx + max(cw, char_width)
                    if ix < cx - 1 or ix > right + 1:
                        continue
                    left = min(cx, ix)
                    top = min(cy, iy)
                    new_right = max(cx + cw, ix + iw)
                    new_bottom = max(cy + ch, iy + ih)
                    control["x"] = left
                    control["y"] = top
                    control["width"] = max(0, new_right - left)
                    control["height"] = max(0, new_bottom - top)

        expand_html_control_bounds()

        if viewport > 0:
            row_bounds: dict[tuple[int, str], list[int]] = {}
            for item in drawables:
                if item.get("explicit_x"):
                    continue
                align = str(item.get("alignment", "")).upper()
                if align not in {"CENTER", "RIGHT"}:
                    continue
                width = int_attr(item.get("width"))
                if width <= 0:
                    continue
                key = (int_attr(item.get("line")), align)
                bounds = row_bounds.setdefault(key, [10**12, -10**12])
                x = int_attr(item.get("x"))
                bounds[0] = min(bounds[0], x)
                bounds[1] = max(bounds[1], x + width)
            row_shift: dict[tuple[int, str], int] = {}
            for key, (left, right) in row_bounds.items():
                if right < left:
                    continue
                _, align = key
                content_width = max(0, right - left)
                target_left = (viewport - content_width) // 2 if align == "CENTER" else viewport - content_width
                row_shift[key] = max(0, target_left) - left
            if row_shift:
                for item in drawables:
                    if item.get("explicit_x"):
                        continue
                    align = str(item.get("alignment", "")).upper()
                    delta = row_shift.get((int_attr(item.get("line")), align), 0)
                    if delta:
                        item["x"] = int_attr(item.get("x")) + delta
        max_x = 0
        max_y = 0
        for item in drawables:
            max_x = max(max_x, int_attr(item.get("x")) + int_attr(item.get("width")))
            max_y = max(max_y, int_attr(item.get("y")) + int_attr(item.get("height")))
        max_y = max(max_y, len(rows) * line_height)
        return {
            "page": page,
            "rows": rows,
            "drawables": drawables,
            "texts": [d for d in drawables if d.get("type") == "text"],
            "buttons": [d for d in drawables if d.get("type") == "button"],
            "print_buttons": [d for d in drawables if d.get("type") == "print_button"],
            "print_rects": [d for d in drawables if d.get("type") == "print_rect"],
            "print_spaces": [d for d in drawables if d.get("type") == "print_space"],
            "nonbuttons": [d for d in drawables if d.get("type") == "nonbutton"],
            "html_text": [d for d in drawables if d.get("type") == "html_text"],
            "images": [d for d in drawables if d.get("type") == "image"],
            "print_images": [d for d in drawables if d.get("type") == "print_image"],
            "canvas": {"width": max_x, "height": max_y},
        }

    def html_hit_test(
        self,
        x: int,
        y: int,
        *,
        char_width: int = 1,
        line_height: int = 1,
        viewport_width: int | None = None,
        html_unit_scale: float = 1.0,
    ) -> dict[str, Any] | None:
        """Return the topmost drawable under a GUI coordinate.

        Images nested in ``<button>`` inherit ``parent_value`` as
        ``button_value`` so a GUI adapter can turn a mouse click directly into
        the string that should be submitted to ``INPUTS``/mouse UI handlers.
        Non-button images/nonbuttons are still reported for hover/tooltip
        consumers, but their ``button_value`` is empty.
        """

        try:
            px = int(x)
            py = int(y)
        except Exception:
            return None
        layout = self.html_layout_model(
            char_width=char_width,
            line_height=line_height,
            viewport_width=viewport_width,
            html_unit_scale=html_unit_scale,
        )
        # Later HTML elements visually sit on top of earlier ones in the simple
        # model; scan in reverse draw order so image buttons beat row text.
        for item in reversed(layout.get("drawables", [])):
            if item.get("type") == "print_space":
                continue
            width = to_int(item.get("width", 0))
            height = to_int(item.get("height", 0))
            if width <= 0 or height <= 0:
                continue
            ix = to_int(item.get("x", 0))
            iy = to_int(item.get("y", 0))
            if not (ix <= px < ix + width and iy <= py < iy + height):
                continue
            hit = dict(item)
            if hit.get("type") in {"button", "print_button"}:
                hit["button_value"] = to_str(hit.get("value", ""))
            elif hit.get("type") == "image" and hit.get("parent") == "button":
                hit["button_value"] = to_str(hit.get("parent_value", ""))
            else:
                hit["button_value"] = ""
            return hit
        return None

    def html_click_value(
        self,
        x: int,
        y: int,
        *,
        char_width: int = 1,
        line_height: int = 1,
        viewport_width: int | None = None,
        html_unit_scale: float = 1.0,
    ) -> str | None:
        """Return the clickable button value at a coordinate, if any."""

        try:
            px = int(x)
            py = int(y)
        except Exception:
            return None
        layout = self.html_layout_model(
            char_width=char_width,
            line_height=line_height,
            viewport_width=viewport_width,
            html_unit_scale=html_unit_scale,
        )
        for item in reversed(layout.get("drawables", [])):
            if item.get("type") == "print_space":
                continue
            width = to_int(item.get("width", 0))
            height = to_int(item.get("height", 0))
            if width <= 0 or height <= 0:
                continue
            ix = to_int(item.get("x", 0))
            iy = to_int(item.get("y", 0))
            if not (ix <= px < ix + width and iy <= py < iy + height):
                continue
            if item.get("type") in {"button", "print_button"}:
                value = to_str(item.get("value", ""))
            elif item.get("type") == "image" and item.get("parent") == "button":
                value = to_str(item.get("parent_value", ""))
            else:
                value = ""
            if value != "":
                return value
        return None

    def queue_input(self, value: str) -> str:
        """Queue a front-end input value and make future reads non-blocking."""

        text = to_str(value)
        self.inputs.append(text)
        self.had_explicit_inputs = True
        self.waiting_for_input = False
        return text

    def queue_html_click(
        self,
        x: int,
        y: int,
        *,
        char_width: int = 1,
        line_height: int = 1,
        viewport_width: int | None = None,
        html_unit_scale: float = 1.0,
    ) -> str | None:
        """Queue the clickable value at a GUI coordinate, if one exists.

        The click also updates the runtime mouse polling state used by
        ``MOUSEX()``, ``MOUSEY()`` and ``MOUSEB()``.
        """

        try:
            self.mouse_x = int(x)
            self.mouse_y = int(y)
        except Exception:
            return None
        value = self.html_click_value(
            self.mouse_x,
            self.mouse_y,
            char_width=char_width,
            line_height=line_height,
            viewport_width=viewport_width,
            html_unit_scale=html_unit_scale,
        )
        self.mouse_button = value or ""
        if value is None:
            return None
        return self.queue_input(value)

    def _get_display_line(self, index: int) -> str:
        lines = self._display_lines()
        return lines[index] if 0 <= index < len(lines) else ""

    def _current_line_text(self) -> str:
        text = "".join(self.output)
        if not text or text.endswith("\n"):
            return ""
        return text.rsplit("\n", 1)[-1]

    def _current_line_col(self) -> int:
        return self._layout_text_width(self._current_line_text())

    def _last_printed_line_text(self) -> str:
        text = "".join(self.output).rstrip("\n")
        if not text:
            return ""
        return text.rsplit("\n", 1)[-1]

    def _next_write_start_line(self) -> int:
        text = "".join(self.output)
        if not text:
            return 1
        current = self._line_count()
        return current + 1 if text.endswith("\n") else current

    def _trim_html_to_line_count(self, keep_lines: int) -> None:
        keep_lines = max(0, keep_lines)
        self._html_fragments = [
            (start, end, text)
            for start, end, text in self._html_fragments
            if start <= keep_lines and end <= keep_lines
        ]
        self.html_output = [text for _, _, text in self._html_fragments]

        def _filter_elements(elements: list[dict[str, Any]], lines: list[int]) -> tuple[list[dict[str, Any]], list[int]]:
            kept_elements: list[dict[str, Any]] = []
            kept_lines: list[int] = []
            for element, line in zip(elements, lines):
                if line <= keep_lines:
                    kept_elements.append(element)
                    kept_lines.append(line)
            return kept_elements, kept_lines

        def _filter_elements_with_styles(
            elements: list[dict[str, Any]],
            lines: list[int],
            styles: list[dict[str, Any]],
        ) -> tuple[list[dict[str, Any]], list[int], list[dict[str, Any]]]:
            kept_elements: list[dict[str, Any]] = []
            kept_lines: list[int] = []
            kept_styles: list[dict[str, Any]] = []
            for element, line, style in zip(elements, lines, styles):
                if line <= keep_lines:
                    kept_elements.append(element)
                    kept_lines.append(line)
                    kept_styles.append(style)
            return kept_elements, kept_lines, kept_styles

        self.html_buttons, self._html_button_lines, self._html_button_styles = _filter_elements_with_styles(
            self.html_buttons,
            self._html_button_lines,
            self._html_button_styles,
        )
        self.html_images, self._html_image_lines, self._html_image_styles = _filter_elements_with_styles(
            self.html_images,
            self._html_image_lines,
            self._html_image_styles,
        )
        self.html_nonbuttons, self._html_nonbutton_lines, self._html_nonbutton_styles = _filter_elements_with_styles(
            self.html_nonbuttons,
            self._html_nonbutton_lines,
            self._html_nonbutton_styles,
        )
        self.html_text_runs, self._html_text_lines = _filter_elements(self.html_text_runs, self._html_text_lines)
        self.print_buttons, self._print_button_lines, self._print_button_styles = _filter_elements_with_styles(
            self.print_buttons,
            self._print_button_lines,
            self._print_button_styles,
        )
        self.print_rects, self._print_rect_lines, self._print_rect_styles = _filter_elements_with_styles(
            self.print_rects,
            self._print_rect_lines,
            self._print_rect_styles,
        )
        self.print_spaces, self._print_space_lines, self._print_space_styles = _filter_elements_with_styles(
            self.print_spaces,
            self._print_space_lines,
            self._print_space_styles,
        )
        self.print_images, self._print_image_lines = _filter_elements(self.print_images, self._print_image_lines)

    def _clear_lines(self, count: int) -> None:
        count = max(0, count)
        if count <= 0 or not self.output:
            return
        text = "".join(self.output)
        lines = text.splitlines(keepends=True)
        if count >= len(lines):
            self.output.clear()
            self._html_visual_line_extra = 0
            self._html_raw_line_breaks.clear()
            self._trim_text_spans_to_line_count(0)
            self._trim_html_to_line_count(0)
            self._clear_visible_buttons()
            return
        self.output = ["".join(lines[:-count])]
        keep_lines = len(lines) - count
        removed_extra = 0
        for raw_line in list(self._html_raw_line_breaks):
            if raw_line > keep_lines:
                removed_extra += self._html_raw_line_breaks.pop(raw_line)
        if removed_extra:
            self._html_visual_line_extra = max(0, self._html_visual_line_extra - removed_extra)
        visual_keep_lines = keep_lines + self._html_visual_line_extra
        self._trim_text_spans_to_line_count(visual_keep_lines)
        self._trim_html_to_line_count(visual_keep_lines)
        # CLEARLINE removes visible UI rows.  Any harvested button values from
        # those rows must disappear as well; otherwise non-interactive runs can
        # accidentally click stale buttons from menus that eraMegaten has just
        # redrawn or hidden (notably MESSAGE_WINDOW_D's temporary dungeon UI).
        self.pending_buttons.clear()

    def _input(self, default: str = "") -> str:
        if self.inputs:
            value = self.inputs.pop(0)
            self._clear_visible_buttons()
            if self.echo:
                print(f"> {value}")
            return value
        if self.interactive:
            try:
                value = input("> ")
                self._clear_visible_buttons()
                return value
            except EOFError:
                return default
        if default != "":
            value = default
        elif self.pending_buttons:
            value = self.pending_buttons[0]
        else:
            value = default
        self._clear_visible_buttons()
        return value

    def _exec_await(self, rest: str) -> None:
        millis = max(0, to_int(eval_expr(self, rest, default=0))) if rest else 0
        self.await_count += 1
        self.last_await_millis = millis
        self._record_timed_wait("AWAIT", millis, allow_skip=False)
        if self.interactive and millis > 0:
            time.sleep(millis / 1000.0)

    def _record_timed_wait(self, command: str, millis: int, *, allow_skip: bool) -> None:
        self.timed_wait_events.append({
            "command": command,
            "millis": max(0, int(millis)),
            "allow_skip": bool(allow_skip),
        })

    def _exec_twait(self, rest: str) -> None:
        parts = split_era_args(rest)
        millis = max(0, to_int(eval_expr(self, parts[0], default=0))) if parts else 0
        allow_skip = truth(eval_expr(self, parts[1], default=0)) if len(parts) >= 2 and parts[1].strip() else False
        self._record_timed_wait("TWAIT", millis, allow_skip=allow_skip)
        if self.interactive and millis > 0:
            time.sleep(millis / 1000.0)

    def _exec_forcewait(self) -> None:
        self._record_timed_wait("FORCEWAIT", 0, allow_skip=False)
        if self.interactive or self.inputs:
            self._input("")

    def _record_timed_input_wait(self, key: str, rest: str) -> None:
        if key not in {"TINPUT", "TINPUTS", "TONEINPUT", "TONEINPUTS"}:
            return
        parts = split_era_args(rest)
        millis = max(0, to_int(eval_expr(self, parts[0], default=0))) if parts else 0
        allow_skip = truth(eval_expr(self, parts[2], default=0)) if len(parts) >= 3 and parts[2].strip() else False
        self._record_timed_wait(key, millis, allow_skip=allow_skip)

    def _eval_sound_media_arg(self, rest: str) -> str:
        parts = split_era_args(rest)
        raw = parts[0] if parts else ""
        if not raw:
            return ""
        if self._argument_is_bare_form(raw):
            return self.render_form(raw).strip(" 	")
        default = self.render_form(raw).strip().strip('"') if ("%" in raw or "{" in raw or "\\@" in raw) else raw.strip().strip('"')
        return to_str(eval_expr(self, raw, default=default))

    def _sound_event(self, action: str, **payload: Any) -> None:
        event = {"action": action}
        event.update(payload)
        self.sound_events.append(event)

    def _exec_sound_command(self, key: str, rest: str) -> None:
        if key == "PLAYSOUND":
            media = self._eval_sound_media_arg(rest)
            if media:
                self.sound_effects.append(media)
                # Emuera can play up to 10 PLAYSOUND effects concurrently.
                if len(self.sound_effects) > 10:
                    self.sound_effects = self.sound_effects[-10:]
            self._sound_event("playsound", media=media, exists=self._sound_file_exists(media))
            return
        if key == "STOPSOUND":
            self.sound_effects.clear()
            self._sound_event("stopsound")
            return
        if key == "PLAYBGM":
            media = self._eval_sound_media_arg(rest)
            self.current_bgm = media
            self._sound_event("playbgm", media=media, exists=self._sound_file_exists(media))
            return
        if key == "STOPBGM":
            self.current_bgm = ""
            self._sound_event("stopbgm")
            return
        volume = max(0, min(100, to_int(eval_expr(self, rest, default=100)))) if rest else 100
        if key == "SETSOUNDVOLUME":
            self.sound_volume = volume
            self._sound_event("setsoundvolume", volume=volume)
            return
        if key == "SETBGMVOLUME":
            self.bgm_volume = volume
            self._sound_event("setbgmvolume", volume=volume)
            return

    def _sound_path_for(self, name: str) -> Path | None:
        rel = to_str(name).strip().replace("\\", "/")
        if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
            return None
        parts = [part for part in rel.split("/") if part not in {"", "."}]
        if not parts or any(part == ".." for part in parts):
            return None
        try:
            sound_root = (self.program.root / "sound").resolve()
            candidate = sound_root.joinpath(*parts).resolve(strict=False)
            if candidate == sound_root or sound_root not in candidate.parents:
                return None
            return candidate
        except Exception:
            return None

    def _sound_file_exists(self, name: str) -> bool:
        path = self._sound_path_for(name)
        if path is None:
            return False
        try:
            if path.is_file():
                return True
        except OSError:
            return False
        # Keep fixtures portable on case-sensitive hosts while matching the
        # Windows-oriented Emuera/game layout.
        try:
            sound_root = (self.program.root / "sound").resolve()
            current = sound_root
            rel = path.relative_to(sound_root)
            for part in rel.parts:
                if not current.is_dir():
                    return False
                lowered = part.lower()
                match = next((child for child in current.iterdir() if child.name.lower() == lowered), None)
                if match is None:
                    return False
                current = match
            return current.is_file()
        except Exception:
            return False

    # ---- line execution ---------------------------------------------------
    def _execute_line(self, frame: ExecFrame, src: SourceLine) -> None:
        text = src.text.strip(" \t\r\n")
        if not text.strip() or text.startswith(";"):
            frame.pc += 1
            return
        if text.startswith("$") or text.startswith("#"):
            frame.pc += 1
            return
        key, rest = self._keyword(text)

        # Control flow first.
        if key == "SIF":
            cond = eval_expr(self, rest)
            frame.pc += 1 if truth(cond) else 2
            return
        if key == "IF":
            if truth(eval_expr(self, rest)):
                frame.pc += 1
            else:
                frame.pc = self._find_if_branch(frame, frame.pc)
            return
        if key == "ELSEIF" or key == "ELSE":
            frame.pc = self._find_matching(frame, frame.pc, {"ENDIF"}) + 1
            return
        if key == "ENDIF":
            frame.pc += 1
            return
        if key == "SELECTCASE":
            value = eval_expr(self, rest)
            frame.pc = self._find_case(frame, frame.pc, value)
            return
        if key in {"CASE", "CASEELSE"}:
            frame.pc = self._find_matching(frame, frame.pc, {"ENDSELECT"}) + 1
            return
        if key == "ENDSELECT":
            frame.pc += 1
            return
        if key == "FOR":
            self._exec_for(frame, rest)
            return
        if key == "NEXT":
            self._exec_next(frame)
            return
        if key == "WHILE":
            if truth(eval_expr(self, rest)):
                frame.loops.append({"type": "WHILE", "pc": frame.pc})
                frame.pc += 1
            else:
                frame.pc = self._find_matching(frame, frame.pc, {"WEND"}) + 1
            return
        if key == "WEND":
            loop = self._last_loop(frame, "WHILE")
            if loop:
                frame.pc = loop["pc"]
                frame.loops.remove(loop)
            else:
                frame.pc += 1
            return
        if key == "DO":
            frame.loops.append({"type": "DO", "pc": frame.pc})
            frame.pc += 1
            return
        if key == "LOOP":
            loop = self._last_loop(frame, "DO")
            cond = True if not rest else truth(eval_expr(self, rest))
            if loop and cond:
                frame.pc = loop["pc"] + 1
            else:
                if loop:
                    frame.loops.remove(loop)
                frame.pc += 1
            return
        if key == "REPEAT":
            count = to_int(eval_expr(self, rest, default=0))
            if count <= 0:
                frame.pc = self._find_matching(frame, frame.pc, {"REND"}) + 1
            else:
                self.memory.set_var("COUNT", [], 0)
                frame.loops.append({"type": "REPEAT", "pc": frame.pc, "count": count, "i": 0})
                frame.pc += 1
            return
        if key == "REND":
            loop = self._last_loop(frame, "REPEAT")
            if loop:
                loop["i"] += 1
                if loop["i"] < loop["count"]:
                    self.memory.set_var("COUNT", [], loop["i"])
                    frame.pc = loop["pc"] + 1
                else:
                    frame.loops.remove(loop)
                    frame.pc += 1
            else:
                frame.pc += 1
            return
        if key == "BREAK":
            frame.pc = self._jump_target_pc(frame, self._find_loop_end(frame, frame.pc) + 1)
            return
        if key == "CONTINUE":
            frame.pc = self._find_loop_continue(frame, frame.pc)
            return
        if key == "GOTO":
            label = norm_name(rest.lstrip("$"))
            frame.pc = self._jump_target_pc(frame, frame.fn.labels.get(label, frame.pc + 1))
            return
        if key == "GOTOFORM":
            label = norm_name(self.render_form(rest).strip().lstrip("$"))
            frame.pc = self._jump_target_pc(frame, frame.fn.labels.get(label, frame.pc + 1))
            return
        if key in {"TRYGOTO", "TRYCGOTO"}:
            label = norm_name(rest.strip().lstrip("$"))
            frame.pc = self._jump_target_pc(frame, frame.fn.labels.get(label, frame.pc + 1))
            return
        if key in {"TRYGOTOFORM", "TRYCGOTOFORM"}:
            label = norm_name(self.render_form(rest).strip().lstrip("$"))
            frame.pc = self._jump_target_pc(frame, frame.fn.labels.get(label, frame.pc + 1))
            return
        if key == "JUMP":
            target, args = self._parse_call(rest)
            self._return()
            self._push_call_sequence(target, args)
            return
        if key in {"TRYJUMP", "TRYCJUMP"}:
            target, args = self._parse_call(rest)
            self._exec_try_jump(frame, target, args)
            return
        if key in {"TRYJUMPFORM", "TRYCJUMPFORM"}:
            target, args = self._parse_callform(rest)
            self._exec_try_jump(frame, target, args)
            return
        if key in {"TRYCALLLIST", "TRYJUMPLIST", "TRYGOTOLIST"}:
            self._exec_try_list(frame, key)
            return
        if key.startswith("RETURN"):
            expr = rest if key == "RETURNF" else text[len(key):].strip()
            self._return_values(self._eval_return_values(key, expr) if expr else [])
            return

        # Calls and state transitions.
        if key in {"CALL", "CALLF"}:
            target, args = self._parse_call(rest)
            if self._exec_native_call(target, args):
                if not self.waiting_for_input:
                    frame.pc += 1
                return
            self._push_call_sequence(target, args)
            frame.pc += 1
            return
        if key in {"TRYCALL", "TRYCCALL"}:
            target, args = self._parse_call(rest)
            self._exec_try_call(frame, target, args)
            return
        if key in {"CALLFORM", "CALLFORMF"}:
            target, args = self._parse_callform(rest)
            if self._exec_native_call(target, args):
                if not self.waiting_for_input:
                    frame.pc += 1
                return
            self._push_call_sequence(target, args)
            frame.pc += 1
            return
        if key in {"TRYCALLFORM", "TRYCCALLFORM"}:
            target, args = self._parse_callform(rest)
            self._exec_try_call(frame, target, args)
            return
        if key == "CATCH":
            frame.pc = self._find_matching_catch_end(frame, frame.pc) + 1
            return
        if key == "ENDCATCH":
            frame.pc += 1
            return
        if key == "THROW":
            self._fatal(f"THROW: {self.render_form(rest)}")
        if key in {"FUNC", "ENDFUNC"}:
            # FUNC/ENDFUNC are control records for TRYCALLLIST-style blocks.
            # Normal execution should skip such blocks from the opener, but
            # keep the records harmless if a script jumps into/over them.
            frame.pc += 1
            return
        if key == "BEGIN":
            self._begin(rest)
            return
        if key == "RESTART":
            # Emuera's RESTART re-enters the currently executing function.
            # eraMegaten uses it heavily in menu/input helpers (OPTION_INPUT,
            # INPUTINT, dungeon/battle selectors) as a local retry/redisplay,
            # not as a global title reset.  Keep the call stack and frame
            # variables/arguments intact; just rewind the current frame and
            # discard loop contexts that belonged to the abandoned pass.
            frame.pc = 0
            frame.loops.clear()
            return
        if key == "QUIT":
            self.stack.clear(); self.memory.frames.clear()
            return

        if key in {"DRAWLINE", "CUSTOMDRAWLINE", "DRAWLINEFORM"}:
            fill = self._drawline_fill(key, rest)
            self._write((fill * 72)[:72], newline=True)
            frame.pc += 1
            return

        # Variables may legally start with command names (e.g. PRINT_MODE).
        # Give top-level assignments priority before PRINT*/HTML_PRINT command
        # dispatch so apostrophe assignment does not get rendered as text.
        if find_assignment(text) and self._try_assignment(text):
            frame.pc += 1
            return

        # I/O commands.
        if key.startswith("DEBUGPRINT"):
            self._exec_debug_print(key, rest)
            frame.pc += 1
            return
        if key.startswith("PRINTDATA") or key.startswith("RANDDATA"):
            frame.pc = self._exec_data(frame, key)
            return
        if key in {"DATA", "DATAFORM", "DATALIST", "ENDLIST", "ENDDATA"}:
            # DATA-family records are payload for PRINTDATA/RANDDATA blocks, not
            # standalone imperative commands.  Real eraMegaten files contain a
            # few reachable-looking DATAFORM/ENDDATA records in event blocks; if
            # execution ever lands on such a marker, keep it inert instead of
            # warning or rendering the payload.
            frame.pc += 1
            return
        if self._is_print_key(key):
            self._exec_print(key, rest, (frame.fn.name, str(frame.pc), key, rest))
            if not self.waiting_for_input:
                frame.pc += 1
            return
        if key == "AWAIT":
            self._exec_await(rest)
            frame.pc += 1
            return
        if key in {"PLAYSOUND", "STOPSOUND", "PLAYBGM", "STOPBGM", "SETSOUNDVOLUME", "SETBGMVOLUME"}:
            self._exec_sound_command(key, rest)
            frame.pc += 1
            return
        if key in {"WAIT", "WAITANYKEY"}:
            if self.interactive or (key == "WAITANYKEY" and self.inputs):
                self._input("")
            elif key == "WAITANYKEY" and not self.interactive and self.had_explicit_inputs:
                self.waiting_for_input = True
                return
            frame.pc += 1
            return
        if key in {"FORCEWAIT", "TWAIT"}:
            if key == "TWAIT":
                self._exec_twait(rest)
            elif key == "FORCEWAIT" and (self.interactive or self.inputs):
                self._exec_forcewait()
            elif key == "FORCEWAIT" and not self.interactive and self.had_explicit_inputs:
                self.waiting_for_input = True
                return
            elif key == "FORCEWAIT":
                self._exec_forcewait()
            frame.pc += 1
            return
        if key in {"INPUT", "ONEINPUT", "INPUTS", "ONEINPUTS", "TINPUT", "TINPUTS", "TONEINPUT", "TONEINPUTS"}:
            if self._input_would_block(key, rest):
                self.waiting_for_input = True
                return
            self._exec_input(key, rest)
            frame.pc += 1
            return
        if key == "INPUTANY":
            if self._input_would_block(key, rest):
                self.waiting_for_input = True
                return
            self._exec_input_any()
            frame.pc += 1
            return
        if key in {"BINPUT", "BINPUTS"}:
            if self._input_would_block(key, rest):
                self.waiting_for_input = True
                return
            self._exec_binput(key, rest)
            frame.pc += 1
            return
        if key == "FLOWINPUT":
            self._exec_flow_input(rest)
            frame.pc += 1
            return
        if key == "FLOWINPUTS":
            self._exec_flow_inputs(rest)
            frame.pc += 1
            return
        if key == "__SHOPINPUT":
            flow_key, flow_rest = self._flow_input_command()
            if not self.interactive and not self.inputs and not self._flow_input_has_default():
                # In a real Emuera UI this would block waiting for the player.
                # Non-interactive regression runs should stop after rendering
                # the menu instead of auto-selecting status/menu PRINTBUTTONs.
                self.waiting_for_input = True
                return
            self._exec_input(flow_key, flow_rest)
            frame.pc += 1
            return
        if key == "__TRAININPUT":
            flow_key, flow_rest = self._flow_input_command()
            if self._input_would_block(flow_key, flow_rest):
                self.waiting_for_input = True
                return
            self._exec_input(flow_key, flow_rest)
            frame.pc += 1
            return
        # Runtime/data commands.
        if key in {"VARI", "VARS"}:
            self._exec_dynamic_var(key, rest)
            frame.pc += 1
            return
        if key == "VARSET":
            self._exec_varset(rest)
            frame.pc += 1
            return
        if key in {"SETBIT", "CLEARBIT", "INVERTBIT"}:
            self._exec_bit_command(key, rest)
            frame.pc += 1
            return
        if key == "SWAP":
            self._exec_swap(rest)
            frame.pc += 1
            return
        if key == "CVARSET":
            self._exec_cvarset(rest)
            frame.pc += 1
            return
        if key == "DOTRAIN":
            self._push_dotrain_flow(to_int(eval_expr(self, rest)))
            frame.pc += 1
            return
        if key == "CHKDATA":
            self._exec_chkdata(rest)
            frame.pc += 1
            return
        if key == "TIMES":
            self._exec_times(rest)
            frame.pc += 1
            return
        if key == "ENCODETOUNI":
            self._exec_encode_to_uni(rest)
            frame.pc += 1
            return
        if key in {"STRLENFORM", "STRLENFORMU"}:
            fn_name = "STRLENFORMU" if key == "STRLENFORMU" else "STRLENFORM"
            value = to_int(call_builtin(self, fn_name, [rest]) or 0)
            self.memory.set_var("RESULT", [], value)
            self.memory.set_var("RESULTS", [], str(value))
            frame.pc += 1
            return
        if key == "GETTIME":
            now = int(time.time() * 1000)
            self.memory.set_var("RESULT", [], now)
            self.memory.set_var("RESULTS", [], str(now))
            frame.pc += 1
            return
        if key == "SAVENOS":
            value = to_int(call_builtin(self, "SAVENOS", []) or 0)
            if rest:
                try:
                    ref = parse_lvalue(self, rest)
                    self.memory.set_var(ref.base, ref.indices, value)
                except Exception:
                    self.memory.set_var(rest.strip(), [], value)
            self.memory.set_var("RESULT", [], value)
            self.memory.set_var("RESULTS", [], str(value))
            frame.pc += 1
            return
        if key == "RANDOMIZE":
            seed = to_int(eval_expr(self, rest, default=int(time.time() * 1000))) if rest else int(time.time() * 1000)
            random.seed(seed)
            frame.pc += 1
            return
        if key == "DUMPRAND":
            self._exec_dumprand()
            frame.pc += 1
            return
        if key == "INITRAND":
            self._exec_initrand()
            frame.pc += 1
            return
        if key == "SPLIT" or key == "SPLITRAND":
            self._exec_split(rest, randomize=(key == "SPLITRAND"))
            frame.pc += 1
            return
        if key == "RESETDATA":
            self._reset_display_style()
            self.memory.characters.clear(); self.memory.numeric.setdefault("CHARANUM", {})[()] = 0
            frame.pc += 1
            return
        if key == "RESETGLOBAL":
            self._exec_resetglobal()
            frame.pc += 1
            return
        if key == "ADDDEFCHARA":
            if any(active.fn.key == "SYSTEM_TITLE" for active in self.stack):
                self.memory.add_default_characters()
            else:
                self.warn("ADDDEFCHARA is only valid in @SYSTEM_TITLE")
            frame.pc += 1
            return
        if key == "ADDCHARA":
            for part in split_era_args(rest):
                if part.strip():
                    self.memory.add_chara(to_int(eval_expr(self, part)))
            frame.pc += 1
            return
        if key == "ADDSPCHARA":
            for part in split_era_args(rest):
                if part.strip():
                    self.memory.add_sp_chara(to_int(eval_expr(self, part)))
            frame.pc += 1
            return
        if key == "ADDVOIDCHARA":
            idx = self.memory.add_void_chara()
            self.memory.set_var("RESULT", [], idx)
            self.memory.set_var("RESULTS", [], str(idx))
            frame.pc += 1
            return
        if key == "DELCHARA":
            self.memory.del_charas([to_int(eval_expr(self, part)) for part in split_era_args(rest) if part.strip()])
            frame.pc += 1
            return
        if key == "DELALLCHARA":
            self.memory.del_all_charas()
            frame.pc += 1
            return
        if key == "PICKUPCHARA":
            keep: list[int] = []
            for part in split_era_args(rest):
                raw = part.strip()
                if not raw:
                    continue
                value = to_int(eval_expr(self, raw))
                raw_key = norm_name(raw)
                if value < 0:
                    if raw_key in {"MASTER", "PLAYER", "TARGET", "ASSI"} or any(raw_key.startswith(prefix + ":") for prefix in ("MASTER", "PLAYER", "TARGET", "ASSI")):
                        continue
                    self.warn(f"PICKUPCHARA ignored negative character index: {raw}")
                    continue
                keep.append(value)
            self.memory.pickup_charas(keep)
            frame.pc += 1
            return
        if key == "ADDCOPYCHARA":
            src = to_int(eval_expr(self, rest, default=0)) if rest else 0
            idx = self.memory.copy_chara(src, None)
            self.memory.set_var("RESULT", [], idx)
            self.memory.set_var("RESULTS", [], str(idx))
            frame.pc += 1
            return
        if key == "COPYCHARA":
            args = [to_int(eval_expr(self, a)) for a in split_era_args(rest)]
            self.memory.copy_chara(args[0], args[1] if len(args) > 1 else None) if args else None
            frame.pc += 1
            return
        if key == "SWAPCHARA":
            args = [to_int(eval_expr(self, a)) for a in split_era_args(rest)]
            if len(args) >= 2: self.memory.swap_chara(args[0], args[1])
            frame.pc += 1
            return
        if key == "SORTCHARA":
            self._exec_sortchara(rest)
            frame.pc += 1
            return
        if key in {"LOADGLOBAL", "SAVEGLOBAL", "LOADDATA", "SAVEDATA", "SAVEGAME", "LOADGAME"}:
            load_flow_started = self._exec_persistence(key, rest)
            if not load_flow_started:
                frame.pc += 1
            return
        if key == "DELDATA":
            self._exec_deldata(rest)
            frame.pc += 1
            return
        if key in {"ARRAYSHIFT", "ARRAYREMOVE", "ARRAYSORT", "ARRAYMSORT"}:
            self._exec_array_command(key, rest)
            frame.pc += 1
            return
        if key == "ARRAYMSORTEX":
            self._exec_arraymsortex(rest)
            frame.pc += 1
            return
        if key == "ARRAYCOPY":
            self._exec_arraycopy(rest)
            frame.pc += 1
            return
        if key == "BAR":
            parts = split_era_args(rest)
            args = [
                eval_expr(self, part, default=0) if part != "" else 0
                for part in parts[:3]
            ]
            self._write(to_str(call_builtin(self, "BARSTR", args) or "[]"), newline=False)
            frame.pc += 1
            return
        if key == "REDRAW":
            value = to_int(eval_expr(self, rest, default=self.current_redraw)) if rest else self.current_redraw
            # Emuera treats bit 0 as the persistent redraw state and bit 1 as
            # an immediate refresh request: REDRAW 0/2 leave drawing
            # suppressed, while REDRAW 1/3 restore normal drawing.
            self.current_redraw = to_int(value) & 1
            frame.pc += 1
            return
        if key == "ALIGNMENT":
            # Emuera tracks ALIGNMENT as display state; terminal transcript mode
            # keeps raw text unchanged but exposes the state via CURRENTALIGN().
            alignment = self._eval_alignment_arg(rest)
            if alignment in {"LEFT", "CENTER", "RIGHT"}:
                self.current_alignment = alignment
            frame.pc += 1
            return
        if key == "SETFONT":
            if rest:
                self.current_font = to_str(eval_expr(self, rest, default=self.render_form(rest).strip().strip('"')))
            else:
                # Emuera treats bare SETFONT as "restore the configured
                # default font".  eraMegaten AA/dialogue files commonly save
                # GETFONT(), switch to a proportional/AA font, and later issue
                # SETFONT with no argument; leaving the previous font active
                # leaks display state into following text and makes GETFONT()
                # disagree with the script's expected reset point.
                self.current_font = self.default_font
            frame.pc += 1
            return
        if key == "CHKFONT":
            font = eval_expr(self, rest, default=self.render_form(rest).strip().strip('"')) if rest else ""
            value = call_builtin(self, "CHKFONT", [font])
            self.memory.set_var("RESULT", [], to_int(value or 0))
            self.memory.set_var("RESULTS", [], str(to_int(value or 0)))
            frame.pc += 1
            return
        if key == "FONTSTYLE":
            # Emuera tracks font style independently from the font face.  The
            # numeric value follows .NET FontStyle bit flags in practice
            # (regular=0, bold=1, italic=2, underline=4, strikeout=8).
            self.current_font_style = max(0, to_int(eval_expr(self, rest, default=0))) if rest else 0
            frame.pc += 1
            return
        if key == "FONTBOLD":
            self.current_font_style |= 1
            frame.pc += 1
            return
        if key == "FONTITALIC":
            self.current_font_style |= 2
            frame.pc += 1
            return
        if key == "FONTUNDERLINE":
            self.current_font_style |= 4
            frame.pc += 1
            return
        if key == "FONTSTRIKEOUT":
            self.current_font_style |= 8
            frame.pc += 1
            return
        if key == "FONTREGULAR":
            self.current_font_style = 0
            frame.pc += 1
            return
        if key == "CLEARLINE":
            self._clear_lines(to_int(eval_expr(self, rest, default=1)) if rest else 1)
            frame.pc += 1
            return
        if key == "REUSELASTLINE":
            self._exec_reuse_last_line(rest)
            frame.pc += 1
            return
        if key == "SKIPDISP":
            # Emuera's SKIPDISP conditionally suppresses display output until
            # the next SKIPDISP 0.  eraMegaten uses it both for optional UI
            # blocks (e.g. hidden reserve-character panels) and for direct
            # command paths that should execute logic without harvesting
            # invisible PRINTBUTTONs.
            self.skip_display = truth(eval_expr(self, rest, default=0)) if rest else True
            frame.pc += 1
            return
        if key == "SKIPLOG":
            self.log_skip = truth(eval_expr(self, rest, default=0)) if rest else True
            frame.pc += 1
            return
        if key == "BITMAP_CACHE_ENABLE":
            self.bitmap_cache_enabled = truth(eval_expr(self, rest, default=0)) if rest else True
            frame.pc += 1
            return
        if key == "OUTPUTLOG":
            self._exec_outputlog(rest)
            frame.pc += 1
            return
        if key == "PUTFORM":
            self._exec_putform(rest)
            frame.pc += 1
            return
        if key == "RESET_STAIN":
            self._exec_reset_stain(rest)
            frame.pc += 1
            return
        if key == "UPCHECK":
            self._exec_upcheck(rest)
            frame.pc += 1
            return
        if key == "CUPCHECK":
            self._exec_cupcheck(rest)
            frame.pc += 1
            return
        if key == "TOOLTIP_SETDELAY":
            self.current_tooltip_delay = max(0, to_int(eval_expr(self, rest, default=0))) if rest else 0
            frame.pc += 1
            return
        if key == "TOOLTIP_SETCOLOR":
            self.current_tooltip_color = self._eval_color_value(rest, self.current_tooltip_color)
            frame.pc += 1
            return
        if key == "HTML_TAGSPLIT":
            self._exec_html_tagsplit(rest)
            frame.pc += 1
            return
        if key == "FORCEKANA":
            # Emuera私家改造版: FORCEKANA selects the conversion used by the
            # PRINTK family only (0 none, 1 hira->kata, 2 fullwidth kata->hira,
            # 3 fullwidth/halfwidth kata->hira).
            self.force_kana_mode = max(0, min(3, to_int(eval_expr(self, rest, default=0)))) if rest else 0
            frame.pc += 1
            return
        if key == "NOSKIP":
            # NOSKIP..ENDNOSKIP temporarily ignores SKIPDISP's display
            # suppression without changing the underlying ISSKIP/SKIPDISP
            # state.  eraMegaten uses it for messages/inputs that must stay
            # visible even in otherwise hidden report sections.
            self.no_skip_depth += 1
            frame.pc += 1
            return
        if key == "ENDNOSKIP":
            self.no_skip_depth = max(0, self.no_skip_depth - 1)
            frame.pc += 1
            return
        if key == "PRINT_SPACE":
            # Emuera's PRINT_SPACE takes a pixel-ish width. In terminal mode,
            # approximate it with one monospace cell per 100 units.
            units = to_int(eval_expr(self, rest, default=100)) if rest else 100
            cells = max(0, units // 100)
            self._record_print_space(units, self._next_visual_write_start_line(), self._current_line_col(), cells)
            self._write(" " * cells, newline=False)
            frame.pc += 1
            return
        if key in {"GCREATE", "GCREATEFROMFILE", "GCLEAR", "GDISPOSE", "SPRITECREATE", "SPRITEDISPOSE", "GDRAWSPRITE"}:
            self._exec_graphics_command(key, rest)
            frame.pc += 1
            return
        if key in {"SETCOLOR", "SETCOLORBYNAME", "RESETCOLOR", "SETBGCOLOR", "SETBGCOLORBYNAME", "RESETBGCOLOR"}:
            self._exec_color_command(key, rest)
            frame.pc += 1
            return
        if key in {"SETCOLOR", "SETCOLORBYNAME", "RESETCOLOR", "SETBGCOLOR", "SETBGCOLORBYNAME", "RESETBGCOLOR", "REDRAW", "CLEARLINE", "SETFONT", "CHKFONT", "REFRESH", "MOUSESKIP", "SPRITECREATE", "GDRAWSPRITE", "GCREATE", "GCLEAR", "GDISPOSE", "SPRITEDISPOSE"}:
            frame.pc += 1
            return

        # Assignment or fallback.
        if self._try_assignment(text):
            frame.pc += 1
            return
        if self._try_incdec_statement(text):
            frame.pc += 1
            return
        if self._exec_builtin_command(key, rest):
            frame.pc += 1
            return
        # Some ERB files use expression functions as commands through CALLF, but
        # unknown imperative commands are safest as no-ops with a warning.
        self.warn(f"unsupported command: {key} ({self.program.file_of(src)}:{src.number})")
        frame.pc += 1

    # ---- command helpers --------------------------------------------------
    def _keyword(self, text: str) -> tuple[str, str]:
        parts = text.split(None, 1)
        key = norm_name(parts[0]) if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        # RETURNF has no separating space in many files.
        if key.startswith("RETURNF") and key not in {"RETURNF", "RETURNFORM"}:
            return "RETURNF", text[len("RETURNF"):].strip()
        return key, rest

    def _parse_call_texts(self, rest: str, *, form_target: bool = False) -> tuple[str, list[str]]:
        parsed = split_call_syntax(rest)
        if not parsed:
            return "", []
        target_expr, arg_texts = parsed
        if form_target:
            target = self._canonical_form_call_target(self.render_form(target_expr))
        else:
            target = to_str(eval_expr(self, target_expr, default=target_expr)) if target_expr.startswith('"') or target_expr.startswith('@"') else self.render_form(target_expr).strip()
        return target, arg_texts

    def _canonical_form_call_target(self, target: str) -> str:
        # CALLFORM/JUMPFORM targets are identifiers assembled from form text.
        # Emuera treats spaces around the `?`/`#` separators in form
        # conditionals as syntax padding in common target builders such as
        # `PRINT_%\@ U == MASTER ? MASTER # SLAVE \@%_STATUS`; keep display
        # form rendering unchanged elsewhere, but canonicalize ASCII padding in
        # function/label targets so the example resolves to PRINT_MASTER_STATUS.
        return re.sub(r"[ \t]+", "", target.strip())

    def _parse_call(self, rest: str) -> tuple[str, list[Any]]:
        target, arg_texts = self._parse_call_texts(rest)
        return target, self._eval_call_args(target, arg_texts)

    def _parse_callform(self, rest: str) -> tuple[str, list[Any]]:
        target, arg_texts = self._parse_call_texts(rest, form_target=True)
        return target, self._eval_call_args(target, arg_texts)

    def _eval_return_expr(self, expr: str) -> Value:
        s = expr.strip()
        if self._return_expr_is_bare_form(s):
            # eraMegaten's ANATANAME uses RETURNF with an unquoted form
            # conditional:
            #   RETURNF \@CALLNAME:MASTER == "あなた" ? %ARGS% # ...\@
            # Treat that as form-string text instead of sending the leading
            # %...% branch bodies through the arithmetic expression parser.
            return self.render_form(s).strip(" \t")
        return eval_expr(self, s)

    def _eval_return_values(self, key: str, expr: str) -> list[Value]:
        parts = split_era_args(expr)
        if not parts:
            return []
        if key == "RETURNFORM":
            return [self.render_form(part).strip(" \t") for part in parts]
        return [self._eval_return_expr(part) for part in parts]

    def _return_expr_is_bare_form(self, expr: str) -> bool:
        return expr.startswith("\\@") and expr.endswith("\\@")

    def _argument_is_bare_form(self, expr: str) -> bool:
        s = expr.strip()
        return (s.startswith("\\@") and s.endswith("\\@")) or (s.startswith("%\\@") and s.endswith("\\@%"))

    def _eval_call_args(self, target: str, arg_texts: list[str]) -> list[Any]:
        fn = self.program.get_function(target)
        ref_positions = self._ref_param_positions(fn) if fn else set()
        out: list[Any] = []
        for i, part in enumerate(arg_texts):
            if part == "":
                out.append(OMITTED_ARG if fn and i in fn.defaults else "")
                continue
            if i in ref_positions:
                ref_arg = self._make_ref_arg(part)
                out.append(ref_arg if ref_arg is not None else eval_expr(self, part))
            elif self._argument_is_bare_form(part):
                out.append(self.render_form(part).strip(" \t"))
            else:
                out.append(eval_expr(self, part))
        if norm_name(target) == "DPOINT":
            # eraMegaten's automap code first primes DPOINT with the current
            # dungeon name and then uses compact DPOINT("=", ...) /
            # DPOINT(,,x,y) shorthand.  Emuera resolves this against the
            # current dungeon context; mirror that so the write-back branch does
            # not fall into DPOINT's CASEELSE diagnostic path.
            while len(out) < 6:
                out.append("")
            if to_str(out[5]) == "":
                out[5] = f"ダンジョン{to_int(self.memory.get_var('FLAG', ['現ダンジョン'])):02d}"
        return out

    def _ref_param_positions(self, fn: EraFunction) -> set[int]:
        ref_names: set[str] = set()
        for line in fn.lines[: max(12, len(fn.params) + 4)]:
            raw = line.text.strip()
            if "REF" not in raw.upper():
                continue
            decl = parse_var_decl(raw)
            if decl:
                ref_names.add(norm_name(decl.name))
        out: set[int] = set()
        for i, param in enumerate(fn.params):
            try:
                ref = parse_lvalue(self, self._clean_param_text(param))
            except Exception:
                continue
            if norm_name(ref.base) in ref_names:
                out.add(i)
        return out

    def _clean_param_text(self, param: str) -> str:
        words = param.strip().split()
        while words and norm_name(words[0]) in {"REF", "DYNAMIC"}:
            words.pop(0)
        return " ".join(words)

    def _make_ref_arg(self, text: str) -> RefArg | None:
        try:
            ref = parse_lvalue(self, text)
        except Exception:
            return None
        frame = self.memory.frame
        key = norm_name(ref.base)
        if frame and (key in frame.numeric or key in frame.strings or key in frame.ref_aliases):
            return RefArg(ref.base, tuple(ref.indices), frame)
        return RefArg(ref.base, tuple(ref.indices), None)

    def _exec_native_call(self, target: str, args: list[Value]) -> bool:
        key = norm_name(target)
        if (
            key.startswith("SETTING_")
            or key.startswith("BATTLE_SETTING_")
            or key.startswith("SKILLGAGE_")
            or key.startswith("GLOBAL_BADEND_")
            or key.startswith("SKILL_EQUIPTHEORY_")
            or key in {
                "SHOPCOMABLE_700", "SHOP_COM_700", "SHOPCOMABLE_701", "SHOP_COM_701",
                "MATCHING_WEAPON_CHECK", "WEAPON_STYLE_CHECK", "WEAPON_CHECK_MIX",
                "LIFTING_A_BAN", "AION式召喚術_人間時技能反映", "子宮最大容量初期設定",
                "SEARCH_SKILL_FUNCTION", "MULTI_SEARCH_SKILL_FUNCTION", "SKILL_TIMING",
                "VAR_REGEN", "VAR_REGENABLE_CHECK", "VAR_KAJA",
                "WRITE_IMG", "SHOW_IMG", "顔グラ追加", "EQUIP_DETAIL_ITEM_LIST",
            }
        ) and key in BUILTINS:
            call_builtin(self, key, args)
            return True
        if key == "SKILL_SPECIAL_TARGET_0":
            # eraMegaten's ordinary ATTACK action reaches SELECT_SKILL_TARGET
            # through `CALLFORM SKILL_SPECIAL_TARGET_{ARG}` with ARG == 0, but
            # no ERB helper exists for that default action.  Emuera-era data
            # treats the missing specialization as "target already passed the
            # generic filters"; make it deterministic instead of leaking the
            # caller's stale RESULT or warning on every visible target row.
            self.memory.set_var("RESULT", [], 1)
            self.memory.set_var("RESULTS", [], "1")
            return True
        if key == "TEMP_STATUS_RESET":
            self._exec_native_temp_status_reset()
            return True
        if key == "INPUT_MANY":
            self._exec_native_input_many(args)
            return True
        if key == "INPUT_SELECT":
            self._exec_native_input_select(args)
            return True
        if key in {"INPUT_SELECT_M", "INPUT_SELECT_D"}:
            self._exec_native_input_select_menu(key, args)
            return True
        if key == "INPUT_SPLIT":
            self._exec_native_input_split(args)
            return True
        if key == "INPUT_CHAR":
            self._exec_native_input_char(args)
            return True
        if key == "MESSAGE_WINDOW_CONFIG":
            self._exec_native_message_window_config()
            return True
        if key == "MESSAGE_WINDOW_LOG":
            self._exec_native_message_window_log(args)
            return True
        if key in {"MESSAGE_WINDOW", "MESSAGE_WINDOW_D"}:
            self._exec_native_message_window(key, args)
            return True
        if key in {
            "SET_STAIN", "MOVE_STAIN", "FLAG_RESET", "SET_COMFLAG", "SET_NEXTTRAIN", "EVENTTURNEND",
            "EVENT_SETBIT", "EVENT_CLEARBIT", "EVENT_INVERTBIT", "TRAIN_SETBIT",
            "CLOTHES_INITIALIZE", "CHECK_EXP", "CHECK_EXPOSE", "CHECK_SOURCE",
            "SET_CLOTHES_DROP_ALL", "SET_CLOTHES_DROP_BOTTOMS", "SET_CLOTHES_DROP_INNER",
            "SET_CLOTHES_DROP_OUTER", "SET_CLOTHES_DROP_TOPS", "SET_CLOTHES_EQUIP_ALL",
            "SET_CLOTHES_NAKED_BREAST", "SET_CLOTHES_NAKED_CROTCH",
            "おっぱいオープンチェック", "股間構造チェック", "触覚チェック", "ずらしチェック",
            "ADD_EXP", "ADDS_EXP", "ADD_GUEST_COMPANION", "DEL_GUEST", "キャラ削除", "気絶処理",
            "SET_BATTLE_STATUS", "SET_EVENTFLAG", "SET_RELATION", "SET_SEX", "ＴＳ処理", "初ＴＳ処理",
            "BASE_INCENSE", "LVUP_BOOSTER", "LVUP_BOOSTER_MAGATAMA",
            "ATTACK_MIN_HP", "MEMORIZE_WEAKNESS", "CHECK_WEAKNESS",
            "GET_PERSONA_NAME", "現在のPERSONA", "PERSONA資料", "装備PERSONA資料", "PERSONA編集",
            "GETCOLOR_9", "MESSAGE_B", "MESSAGE_BL", "MESSAGE_B2", "MESSAGE_P", "MESSAGE_P2",
            "MESSAGE_COMP_OVER", "SET_AISYOU_COLOR", "SHOW_AISYOU_COLOR_LIST", "TOSTR1000",
            "NOWALIGNMENT", "PREVALIGNMENT", "SET_ALIGNMENT",
            "SHOW_PICTURE", "SHOW_FORCEMOVE",
            "WRITE_IMG", "SHOW_IMG", "顔グラ追加", "EQUIP_DETAIL_ITEM_LIST",
            "LIST_FOREACH", "DIC_FOREACH",
            "ANATANAME", "BARCOLORSET", "PRINT_COLOR", "BTL_COLOR_TABLE", "BTL_COLOR_TABLE_NUM",
            "TOSTR_HTML", "COLORDRAWLINE", "PRINTFORM_LF", "PRINT_COLORBAR", "PRINT_EIGHT_BAR",
            "PRINT_STR", "PRINT_STR_F", "PRINT_STRL", "PRINT_STRW", "PRINT_STR_INPUT",
            "PRINT_STR_INPUTS", "HEARTMARK", "WHITE_HEARTMARK", "BIG_HEARTMARK",
            "HEART", "HEARTB", "HEARTW", "HEARTBW", "HEARTD", "HEARTDB", "HEARTDW", "HEARTDBW",
            "TOALIGNMENT",
        }:
            call_builtin(self, key, args)
            return True
        if key == "装備強化_展開" and self._exec_native_equipment_enhance_expand(args):
            return True
        if key in {
            "MOUSEUISTORE_EXSITS_ITEMS",
            "MOUSEUISTORE_EXSITS_CUSTOMITEM",
            "MOUSEUISTORE_SET_VALUE",
            "MOUSEUISTORE_YEN_ONSALES",
            "MOUSEUISTORE_DISPLAYS",
        }:
            self._exec_native_mouseui_store_helper(key, args)
            return True
        if key == "ENTRY_EQUIPMENT_COMPENDIUM":
            self._exec_native_entry_equipment_compendium()
            return True
        if key == "PRINT_FORMATION_FACE_P" and self._exec_native_print_formation_face_p(args):
            return True
        if key == "STRFLAG_NUM_CPD":
            self._exec_native_strflag_num_cpd(args)
            return True
        if key.startswith("NAMEDIC_") and key in BUILTINS:
            call_builtin(self, key, args)
            return True
        if key == "INPUTINT":
            allowed = [to_int(a) for a in args]
            if not allowed:
                allowed = [0]
            input_key = "INPUT" if any(value < 0 or value > 9 for value in allowed) else "ONEINPUT"
            while True:
                if self._input_would_block(input_key, ""):
                    self.waiting_for_input = True
                    return True
                raw = self._input("").strip()
                if input_key == "ONEINPUT" and raw:
                    raw = raw[:1]
                value = to_int(raw)
                if value in allowed:
                    self.memory.set_var("RESULT", [], value)
                    self.memory.set_var("RESULTS", [], str(value))
                    return True
            return True
        if key == "TINPUTINT":
            millis = max(0, to_int(args[0])) if args else 0
            default = to_int(args[1]) if len(args) >= 2 else 0
            show_timer = truth(args[2]) if len(args) >= 3 else False
            allowed = [to_int(a) for a in args[3:]]
            while True:
                if (
                    not self.interactive
                    and self.inputs
                    and self.pending_buttons
                    and self.had_explicit_inputs
                    and self._next_input_misses_visible_numeric_menu()
                ):
                    self.waiting_for_input = True
                    return True
                self._record_timed_wait("TINPUTINT", millis, allow_skip=show_timer)
                raw = self._input(str(default))
                value = to_int(raw)
                if value == default or value in allowed:
                    self.memory.set_var("RESULT", [], value)
                    self.memory.set_var("RESULTS", [], str(value))
                    return True
        if key in {"INPUT_YN", "INPUT_YN_M", "INPUT_YN_D"}:
            self._exec_native_input_yn(key, args)
            return True
        if key in {"INPUT_ONEKEY_TAP", "INPUT_ONEKEY_TAP_RESULTS"}:
            self._exec_native_input_onekey_tap(key, args)
            return True
        return False

    def _exec_native_input_yn(self, key: str, args: list[Value]) -> None:
        """Fast two-choice helper that renders before replay blocking.

        eraMegaten's INPUT_YN helpers draw their own [0]/[1] prompt before the
        ONEINPUTS wait.  The native shortcut must therefore park after drawing
        the prompt, and resume without duplicating the same lines.
        """

        yes = to_str(args[0]) if len(args) >= 1 and to_str(args[0]) != "" else "はい"
        no = to_str(args[1]) if len(args) >= 2 and to_str(args[1]) != "" else "いいえ"
        style = to_int(args[2]) if key == "INPUT_YN" and len(args) >= 3 else 1
        delimiter = to_str(args[2]) if key in {"INPUT_YN_M", "INPUT_YN_D"} and len(args) >= 3 else "/"
        window_options = (
            to_str(args[3])
            if key in {"INPUT_YN_M", "INPUT_YN_D"} and len(args) >= 4
            else "ログを残さない/ボタンを利用する"
        )
        controls_enabled = key in {"INPUT_YN_M", "INPUT_YN_D"} and "ボタンを利用する" in window_options
        if delimiter == "":
            delimiter = "/"
        signature = (key,) + tuple(to_str(a) for a in args)
        resume = self._paused_native_input_yn == signature

        if not resume:
            if key == "INPUT_YN" and style == 2:
                self._write(f"[0] {yes} [1] {no}", newline=True)
            elif key == "INPUT_YN":
                self._write(f"[0] {yes}", newline=True)
                self._write(f"[1] {no}", newline=True)
            else:
                self._write(f"[0] {yes}{delimiter}[1] {no}", newline=True)
            self.pending_buttons.extend(["0", "1"])
            if key in {"INPUT_YN_M", "INPUT_YN_D"}:
                rows = max(1, to_int(args[5]) if len(args) >= 6 else (4 if key == "INPUT_YN_D" else 2))
                width = max(1, to_int(args[6]) if len(args) >= 7 else 72)
                self._register_window_menu_log(f"[0] {yes}{delimiter}[1] {no}", delimiter, rows, width)

        if resume and self._resume_window_control_button():
            return

        yes_tokens = {"0", " ", "y", "yes", "はい"}
        no_tokens = {"1", "n", "no", "いいえ"}
        mode = to_int(self.memory.get_var("FLAG", ["双选输入设定"]) or self.memory.get_var("FLAG", ["双选入力设定"]))

        while True:
            if not self.interactive and not self.inputs and self.had_explicit_inputs:
                self._paused_native_input_yn = signature
                self.waiting_for_input = True
                return
            raw = self._input("")
            control = raw.strip()
            if controls_enabled and control in {"+", "-", "*", "/"}:
                if self._exec_window_control_button(control):
                    self._paused_native_input_yn = signature
                    return
                continue
            if raw == " ":
                value = 0
                self.memory.set_var("RESULT", [], value)
                self.memory.set_var("RESULTS", [], str(value))
                self._paused_native_input_yn = None
                return
            s = raw.strip()
            folded = s.lower()
            if s == "" and not self.had_explicit_inputs and not self.interactive:
                value = 0
            elif folded in {"0", " "}:
                value = 0
            elif folded == "1":
                value = 1
            elif folded in {"y", "yes", "はい"} and (mode == 2 or (mode == 1 and yes in {"はい", "Yes"})):
                value = 0
            elif folded in {"n", "no", "いいえ"} and (mode == 2 or (mode == 1 and no in {"いいえ", "No"})):
                value = 1
            elif folded in yes_tokens and mode == 2:
                value = 0
            elif folded in no_tokens and mode == 2:
                value = 1
            else:
                if not self.inputs and not self.had_explicit_inputs:
                    break
                continue
            self.memory.set_var("RESULT", [], value)
            self.memory.set_var("RESULTS", [], str(value))
            self._paused_native_input_yn = None
            return

        self.memory.set_var("RESULT", [], 0)
        self.memory.set_var("RESULTS", [], "0")
        self._paused_native_input_yn = None

    def _toggle_window_control_flag(self, flag_name: str) -> None:
        current = to_int(self.memory.get_var("FLAG", [flag_name]))
        self.memory.set_var("FLAG", [flag_name], current ^ 1)

    def _register_window_menu_log(self, body: str, delimiter: str, rows: int, width: int) -> None:
        call_builtin(self, "MESSAGE_WINDOW_LOG", ["", body, delimiter or "/", rows, width, 0])

    def _resume_window_control_button(self) -> bool:
        if self._paused_native_message_window_log is not None:
            self._exec_native_message_window_log(["", "", "", 0, 0, 1])
            if self.waiting_for_input:
                return True
        if self._paused_native_message_window_config is not None:
            self._exec_native_message_window_config()
            if self.waiting_for_input:
                return True
        return False

    def _exec_window_control_button(self, control: str) -> bool:
        if control == "-":
            self._toggle_window_control_flag("オート送り")
            return False
        if control == "*":
            self._toggle_window_control_flag("ウィンドウメッセージスキップ")
            return False
        if control == "+":
            self._exec_native_message_window_log(["", "", "", 0, 0, 1])
            return self.waiting_for_input
        if control == "/":
            self._exec_native_message_window_config()
            return self.waiting_for_input
        return False

    def _exec_native_message_window(self, key: str, args: list[Value]) -> None:
        """MESSAGE_WINDOW/MESSAGE_WINDOW_D wait loop with LOG/CONFIG controls.

        The lightweight builtin renders and logs the final window, but native
        CALL dispatch must keep the outer CALL parked when a non-interactive
        replay exhausts its explicit input script at the message wait.  The
        original ERB records ``LINE = LINECOUNT`` before drawing, opens nested
        log/config viewers for control buttons, then clears/redraws from that
        same base line before asking again.
        """

        signature = (key,) + tuple(to_str(a) for a in args)
        paused = self._paused_native_message_window if (
            self._paused_native_message_window
            and self._paused_native_message_window.get("signature") == signature
        ) else None
        state: dict[str, Any] = paused or {
            "signature": signature,
            "rendered": False,
            "base_line": self._line_count(),
            "needs_redraw": False,
        }

        def effective_options() -> str:
            if key == "MESSAGE_WINDOW_D":
                options = to_str(args[3]) if len(args) >= 4 else "デフォルト"
                if options == "デフォルト":
                    return "ログを残さない/ボタンを利用する/再利用しない"
                return options
            return to_str(args[3]) if len(args) >= 4 else "ログを残さない/ボタンを利用する/再利用しない"

        def render_window() -> None:
            state["base_line"] = self._line_count()
            previous = getattr(self, "_defer_message_window_clear", False)
            try:
                self._defer_message_window_clear = True
                call_builtin(self, key, args)
            finally:
                self._defer_message_window_clear = previous
            state["rendered"] = True
            state["needs_redraw"] = False

        def clear_window() -> None:
            self._clear_lines(max(0, self._line_count() - to_int(state.get("base_line", self._line_count()))))

        def finish(*, clear_after_wait: bool) -> None:
            options = effective_options()
            if clear_after_wait and "ログを残す" not in options:
                clear_window()
            self.memory.set_var("RESULT", [], 1)
            self.memory.set_var("RESULTS", [], "1")
            self._paused_native_message_window = None

        while True:
            if not bool(state.get("rendered", False)):
                render_window()

            if self._resume_window_control_button():
                self._paused_native_message_window = state
                return

            if bool(state.get("needs_redraw", False)):
                clear_window()
                render_window()

            options = effective_options()
            if "NOWAIT" in options or to_int(self.memory.get_var("FLAG", ["ウィンドウメッセージスキップ"])) != 0:
                finish(clear_after_wait=False)
                return

            controls_enabled = "ボタンを利用する" in options and "ボタンを利用しない" not in options
            auto_enabled = to_int(self.memory.get_var("FLAG", ["オート送り"])) != 0

            if controls_enabled and auto_enabled:
                raw = ""
                if self.interactive or self.inputs:
                    raw = self._input("A")
                    raw = raw[:1] if raw else ""
                if raw in {"+", "-", "*", "/"}:
                    if self._exec_window_control_button(raw):
                        state["needs_redraw"] = True
                        self._paused_native_message_window = state
                        return
                    state["needs_redraw"] = True
                    continue
                finish(clear_after_wait=True)
                return

            if not controls_enabled and auto_enabled and "NOANIME" not in options:
                if self.interactive or self.inputs:
                    self._input("")
                finish(clear_after_wait=True)
                return

            if self.interactive or self.inputs:
                raw = self._input("")
                raw = raw[:1] if raw else ""
                if controls_enabled and raw in {"+", "-", "*", "/"}:
                    if self._exec_window_control_button(raw):
                        state["needs_redraw"] = True
                        self._paused_native_message_window = state
                        return
                    state["needs_redraw"] = True
                    continue
                finish(clear_after_wait=True)
                return

            if not self.interactive and self.had_explicit_inputs:
                self._paused_native_message_window = state
                self.waiting_for_input = True
                return

            finish(clear_after_wait=True)
            return

    def _exec_native_input_select_menu(self, key: str, args: list[Value]) -> None:
        """Fast path for framed string-choice INPUT_SELECT_M/D calls.

        The real helpers render a delimited list of ``[value] label`` choices
        before waiting.  Many dungeon/event scripts call these directly; native
        handling keeps explicit replay exhaustion parked on the outer CALL and
        avoids re-entering the ERB wrapper just to redisplay the same prompt.
        """

        text = to_str(args[0]) if len(args) >= 1 else ""
        delimiter = to_str(args[1]) if len(args) >= 2 else "/"
        delimiter = delimiter or "/"
        window_options = to_str(args[2]) if len(args) >= 3 else "ログを残さない/ボタンを利用する"
        controls_enabled = "ボタンを利用する" in window_options
        columns = max(1, to_int(args[3]) if len(args) >= 4 else 1)
        rows_min = max(1, to_int(args[4]) if len(args) >= 5 else (4 if key == "INPUT_SELECT_D" else 1))
        width = max(1, to_int(args[6]) if len(args) >= 7 else 72)
        choices = [part for part in text.split(delimiter)]
        values: list[str] = []
        one_key = True
        for choice in choices:
            m = re.search(r"\[([^\]]+)\]", choice)
            if not m:
                continue
            value = m.group(1).strip()
            values.append(value)
            if len(value) > 1:
                one_key = False

        signature = (key,) + tuple(to_str(a) for a in args)
        resume = self._paused_native_input_select_menu == signature
        if not resume:
            row_count = max(rows_min, (len(choices) + columns - 1) // columns)
            rendered_rows: list[str] = []
            for row in range(row_count):
                cells: list[str] = []
                for col in range(columns):
                    idx = row * columns + col
                    cells.append(choices[idx] if idx < len(choices) else "")
                rendered = "　".join(cells).rstrip()[: max(width * columns, 1)]
                rendered_rows.append(rendered)
                self._write(rendered, newline=True)
            for value in values:
                self.pending_buttons.append(value)
            self._register_window_menu_log(delimiter.join(rendered_rows), delimiter, row_count, max(width * columns, 1))

        if resume and self._resume_window_control_button():
            return

        if not values:
            self.memory.set_var("RESULT", [], 0)
            self.memory.set_var("RESULTS", [], "0")
            self._paused_native_input_select_menu = None
            return

        control_values = {"+", "-", "/"} if controls_enabled else set()
        # INPUT_SELECT_M.ERB renders a [*] SKIP footer button, but line 106 only
        # gates "+", "-", "/" before the otherwise unreachable "*" branch.  Let
        # replay scripts consume "*" as a visible inert button instead of
        # treating it as an unrelated bad input or toggling skip.
        inert_control_values = {"*"} if controls_enabled else set()
        while True:
            if not self.interactive and not self.inputs and self.had_explicit_inputs:
                self._paused_native_input_select_menu = signature
                self.waiting_for_input = True
                return
            if not self.interactive and self.inputs and self.had_explicit_inputs:
                candidate = to_str(self.inputs[0]).strip()
                if one_key and candidate:
                    candidate = candidate[:1]
                if candidate not in set(values) | control_values | inert_control_values:
                    self._paused_native_input_select_menu = signature
                    self.waiting_for_input = True
                    return
            entered = self._input(values[0]).strip()
            if one_key and entered:
                entered = entered[:1]
            if entered in values:
                self.memory.set_var("RESULT", [], to_int(entered))
                self.memory.set_var("RESULTS", [], entered)
                self._paused_native_input_select_menu = None
                return
            if entered in control_values:
                # LOG/AUTO/SKIP/CONFIG controls redraw the same menu in ERB.
                # Keep the outer native CALL parked if the nested log/config
                # viewer itself blocks for a follow-up input.
                if self._exec_window_control_button(entered):
                    self._paused_native_input_select_menu = signature
                    return
                continue
            if entered in inert_control_values:
                continue

    def _exec_native_input_many(self, args: list[Value]) -> None:
        """Headless-compatible fast path for eraMegaten's INPUT_MANY helper.

        The ERB helper renders its own range prompt before reading.  When a
        non-interactive replay has exhausted an explicit input script, keep the
        call frame parked on the CALL line after rendering that prompt, then
        consume a later queue_input without drawing the prompt twice.
        """
        lo = to_int(args[0]) if len(args) >= 1 else 0
        hi = to_int(args[1]) if len(args) >= 2 else lo
        if lo > hi:
            lo, hi = hi, lo
        options = to_str(args[2]) if len(args) >= 3 else "ログを残す"
        exceptions = {
            to_int(part)
            for part in (to_str(args[3]) if len(args) >= 4 else "").split("/")
            if part != ""
        }
        signature = tuple(to_str(a) for a in args)
        paused = self._paused_native_input_many if (
            self._paused_native_input_many
            and self._paused_native_input_many.get("signature") == signature
        ) else None
        state: dict[str, Any] = paused or {"signature": signature, "value": 0, "sign": 1, "rendered": False}
        shop_exchange_const = None
        if self.program.csv and norm_name("ショップ:魔貨交換") in self.program.csv.constants:
            shop_exchange_const = self.program.csv.resolve_constant("ショップ:魔貨交換", -1)
        show_money_exchange_button = (
            hi > 20000
            and shop_exchange_const is not None
            and to_int(self.memory.get_var("FLAG", ["商店指令"])) == to_int(shop_exchange_const)
        )

        def render_button_row(buttons: list[tuple[str, str]]) -> None:
            if self._display_suppressed():
                return
            line = self._next_visual_write_start_line()
            parts: list[str] = []
            col = 0
            for label, value in buttons:
                if parts:
                    parts.append("　")
                    col += 1
                self.pending_buttons.append(value)
                self._record_print_button(label, value, line, col)
                parts.append(label)
                col += self._layout_text_width(label)
            self._write("".join(parts), newline=True, harvest_buttons=False)

        def render_prompt() -> None:
            if self._display_suppressed():
                state["rendered"] = True
                state["rendered_lines"] = 0
                return
            rendered_lines = 0
            current_value = to_int(state.get("value", 0))
            prompt = f"【{current_value}】　《【{lo}】 - 【{hi}】》"
            if show_money_exchange_button:
                prompt += f"　【￥{current_value * 50}】"
            self._write(prompt, newline=True)
            rendered_lines += 1
            sign_button = ("[+]", "+") if to_int(state.get("sign", 1)) == -1 else ("[-]", "-")
            render_button_row([("[7]", "７"), ("[8]", "８"), ("[9]", "９"), ("[ AC]", "AC")])
            render_button_row([("[4]", "４"), ("[5]", "５"), ("[6]", "６"), ("[Max]", "MAX")])
            render_button_row([("[1]", "１"), ("[2]", "２"), ("[3]", "３"), ("[Min]", "MIN")])
            render_button_row([("[0]", "０"), sign_button, ("[ENTER]", "ENTER")])
            rendered_lines += 4
            if show_money_exchange_button:
                render_button_row([("[￥1,000,000]", "20000")])
                rendered_lines += 1
            self._write("※キーボードから、直接数値を入力することもできます", newline=True)
            rendered_lines += 1
            state["rendered"] = True
            state["rendered_lines"] = rendered_lines

        def accept(value: int) -> bool:
            if lo <= value <= hi or value in exceptions:
                if "ログを残す" not in options:
                    self._clear_lines(to_int(state.get("rendered_lines", 1)) or 1)
                self.memory.set_var("RESULT", [], value)
                self.memory.set_var("RESULTS", [], str(value))
                self._paused_native_input_many = None
                return True
            return False

        fw_digits = "０１２３４５６７８９"
        digit_map = {ch: i for i, ch in enumerate(fw_digits)}
        while True:
            if not bool(state.get("rendered", False)):
                render_prompt()
            if not self.interactive and not self.inputs and self.had_explicit_inputs:
                self._paused_native_input_many = state
                self.waiting_for_input = True
                return
            raw = self._input(str(lo)).strip()
            upper = raw.upper()
            state["rendered"] = False
            if raw in digit_map:
                digit = digit_map[raw]
                current = to_int(state.get("value", 0))
                if current == 0:
                    state["value"] = abs(current) + digit * to_int(state.get("sign", 1))
                else:
                    state["value"] = current * 10 + digit
                continue
            if upper == "AC":
                state["value"] = 0
                state["sign"] = 1
                continue
            if raw == "+":
                state["value"] = to_int(state.get("value", 0)) * -1
                state["sign"] = 1
                continue
            if raw == "-":
                state["value"] = to_int(state.get("value", 0)) * -1
                state["sign"] = -1
                continue
            if upper == "MIN":
                state["value"] = lo
                continue
            if upper == "MAX":
                state["value"] = hi
                continue
            if upper in {"ENTER", ""}:
                if accept(to_int(state.get("value", 0))):
                    return
                continue
            else:
                if re.fullmatch(r"\d+", raw or ""):
                    value = to_int(raw)
                else:
                    continue
            if accept(value):
                return

    def _exec_native_input_select(self, args: list[Value]) -> None:
        """Headless-compatible fast path for eraMegaten's INPUT_SELECT helper.

        Like INPUT_MANY, the ERB helper draws the choices before reading.  Do
        not auto-fallback to the first choice after an explicit replay script
        runs out; keep execution parked on the CALL and allow a later
        queue_input() to answer the already-rendered menu.
        """
        pairs: list[tuple[int, str]] = []
        for i in range(0, min(len(args), 40), 2):
            value = to_int(args[i])
            label = to_str(args[i + 1]) if i + 1 < len(args) else ""
            if value != 0:
                pairs.append((value, label))
        if not pairs:
            self.memory.set_var("RESULT", [], 0)
            self.memory.set_var("RESULTS", [], "0")
            self._paused_native_input_select = None
            return

        signature = tuple(to_str(a) for a in args)
        resume = self._paused_native_input_select == signature
        value_width = max(len(str(value)) for value, _ in pairs)
        if not resume:
            label_width = max((len(label) for _, label in pairs), default=0)
            row: list[str] = []
            for value, label in pairs:
                row.append(f"[{value:>{value_width}}] {label.ljust(label_width)}　　")
                if len(row) == 2:
                    self._write("".join(row).rstrip(), newline=True)
                    row.clear()
            if row:
                self._write("".join(row).rstrip(), newline=True)

        allowed = {value for value, _ in pairs}
        one_key = value_width == 1
        while True:
            if not self.interactive and not self.inputs and self.had_explicit_inputs:
                self._paused_native_input_select = signature
                self.waiting_for_input = True
                return
            raw = self._input(str(pairs[0][0])).strip()
            if one_key and raw:
                raw = raw[:1]
            value = to_int(raw)
            if value != 0 and value in allowed:
                self.memory.set_var("RESULT", [], value)
                self.memory.set_var("RESULTS", [], str(value))
                self._paused_native_input_select = None
                return

    def _exec_native_input_split(self, args: list[Value]) -> None:
        """Headless-compatible fast path for eraMegaten's INPUT_SPLIT helper.

        INPUT_SPLIT renders a paged list before reading.  If a scripted replay
        runs out of explicit input after the menu is visible, park on the CALL
        and remember the current page so queue_input() can resume without
        duplicating the same page transcript.
        """
        title = to_str(args[0]) if len(args) >= 1 else ""
        items_text = to_str(args[1]) if len(args) >= 2 else ""
        delim = to_str(args[2]) if len(args) >= 3 else "/"
        cancel_text = to_str(args[3]) if len(args) >= 4 else "　"
        columns = max(1, to_int(args[4]) if len(args) >= 5 else 1)
        initial_page = max(0, to_int(args[5]) if len(args) >= 6 else 0)
        start_no = to_int(args[6]) if len(args) >= 7 else 1
        prev_no = to_int(args[7]) if len(args) >= 8 else 1001
        cancel_no = to_int(args[8]) if len(args) >= 9 else 0
        next_no = to_int(args[9]) if len(args) >= 10 else 1003
        items = [part for part in items_text.split(delim) if part != ""]
        page_size = max(1, 20 * columns)
        page_count = max(1, (len(items) + page_size - 1) // page_size)

        signature = tuple(to_str(a) for a in args)
        paused = self._paused_native_input_split if (
            self._paused_native_input_split
            and self._paused_native_input_split.get("signature") == signature
        ) else None
        page = min(max(0, to_int(paused.get("page", initial_page)) if paused else initial_page), page_count - 1)
        rendered_page = to_int(paused.get("rendered_page", -1)) if paused else -1

        if not items and cancel_text in {"", "　"}:
            self.memory.set_var("RESULTS", [], "")
            self.memory.set_var("RESULT", [], 0)
            self.memory.set_var("RESULT", [1], page)
            self._paused_native_input_split = None
            return

        while True:
            begin = page * page_size
            visible_items = items[begin: begin + page_size]
            choices = [(start_no + begin + i, text) for i, text in enumerate(visible_items)]
            footer: list[tuple[int, str]] = []
            if page > 0:
                footer.append((prev_no, "前一頁"))
            if cancel_text not in {"", "　"}:
                footer.append((cancel_no, cancel_text))
            if page + 1 < page_count:
                footer.append((next_no, "下一頁"))

            if rendered_page != page:
                self._write(title, newline=True)
                self._write("─" * 72, newline=True)
                width = max((len(text) + 1 for _, text in choices), default=1)
                num_width = max((len(str(num)) for num, _ in choices), default=1)
                row: list[str] = []
                for num, text in choices:
                    row.append(f"[{num:>{num_width}}]{text.ljust(width)}")
                    if len(row) >= columns:
                        self._write("".join(row).rstrip(), newline=True)
                        row.clear()
                if row:
                    self._write("".join(row).rstrip(), newline=True)
                self._write("─" * 72, newline=True)
                if footer:
                    self._write(" ".join(f"[{num}]{label}" for num, label in footer), newline=True)
                rendered_page = page

            if not self.interactive and not self.inputs and self.had_explicit_inputs:
                self._paused_native_input_split = {
                    "signature": signature,
                    "page": page,
                    "rendered_page": rendered_page,
                }
                self.waiting_for_input = True
                return

            raw = self._input(str(choices[0][0] if choices else cancel_no)).strip()
            value = to_int(raw)
            if value == prev_no and page > 0:
                page -= 1
                rendered_page = -1
                continue
            if value == next_no and page + 1 < page_count:
                page += 1
                rendered_page = -1
                continue
            if value == cancel_no and cancel_text not in {"", "　"}:
                self.memory.set_var("RESULTS", [], cancel_text)
                self.memory.set_var("RESULT", [], value)
                self.memory.set_var("RESULT", [1], page)
                self._paused_native_input_split = None
                return
            idx = value - start_no
            if 0 <= idx < len(items):
                self.memory.set_var("RESULTS", [], items[idx])
                self.memory.set_var("RESULT", [], value)
                self.memory.set_var("RESULT", [1], page)
                self._paused_native_input_split = None
                return

    def _exec_native_input_char(self, args: list[Value]) -> None:
        """Headless-compatible fast path for eraMegaten's INPUT_CHAR helper."""
        allowed = to_str(args[0]) if len(args) >= 1 else ""
        allow_enter = to_int(args[1]) != 0 if len(args) >= 2 else False
        while True:
            if not self.interactive and not self.inputs and self.had_explicit_inputs:
                self.waiting_for_input = True
                return
            text = self._input("")
            char = text[:1] if text else ""
            if char == "" and allow_enter:
                self.memory.set_var("RESULTS", [], "")
                self.memory.set_var("RESULT", [], 0)
                return
            if char and char in allowed:
                self.memory.set_var("RESULTS", [], char)
                self.memory.set_var("RESULT", [], to_int(char))
                return
            if not self.inputs and not self.had_explicit_inputs:
                break

        fallback = "" if allow_enter else (allowed[:1] if allowed else "")
        self.memory.set_var("RESULTS", [], fallback)
        self.memory.set_var("RESULT", [], to_int(fallback))

    def _exec_native_input_onekey_tap(self, key: str, args: list[Value]) -> None:
        """Fast path for eraMegaten's one-key tap helper with replay blocking."""
        tap_args = list(args)
        if key == "INPUT_ONEKEY_TAP_RESULTS":
            tap_args = [
                args[0] if len(args) >= 1 else 0,
                args[1] if len(args) >= 2 else "-",
                args[2] if len(args) >= 3 else "_",
            ]
            tap_args.extend(self.memory.get_var("RESULTS", [i]) for i in range(20))

        wasd = to_int(tap_args[0]) != 0 if len(tap_args) >= 1 else False
        line_char = to_str(tap_args[1]) if len(tap_args) >= 2 else "-"
        line_char = line_char or "-"
        delimiter = to_str(tap_args[2]) if len(tap_args) >= 3 else "_"
        delimiter = delimiter or "_"
        entries: list[tuple[str, str, str, int]] = []
        for value in tap_args[3:23]:
            parts = to_str(value).split(delimiter)
            entry_key = parts[0] if len(parts) > 0 else ""
            button = parts[1] if len(parts) > 1 else ""
            tag = parts[2] if len(parts) > 2 else ""
            color = to_int(parts[3]) if len(parts) > 3 and re.fullmatch(r"[+-]?\d+", parts[3] or "") else 0
            entries.append((entry_key, button, tag, color))

        signature = (key,) + tuple(to_str(a) for a in tap_args)
        resume = getattr(self, "_paused_native_input_onekey", None) == signature
        if not resume:
            self._write((line_char * 72)[:72], newline=True)
            if wasd:
                self._write("　　[W]　　", newline=True)
                self._write("　[A]┼[D]　", newline=True)
                self._write("　　[S]　　", newline=True)
            else:
                self._write("　　[8]　　", newline=True)
                self._write("　[4]┼[6]　", newline=True)
                self._write("　　[2]　　", newline=True)
            labels = [f"[{button}]{tag}" for _, button, tag, _ in entries if button or tag]
            if labels:
                self._write(" ".join(labels), newline=True)
            self._write((line_char * 72)[:72], newline=True)

        allowed = ("wasdWASD" if wasd else "8462") + "".join(entry_key for entry_key, _, _, _ in entries)
        allow_enter = any(entry_key == "" and button != "" for entry_key, button, _, _ in entries)
        total = to_int(self.memory.get_var("ARG", [1]))
        spent = to_int(self.memory.get_var("消費済み", []))
        auto = "6" if total > spent else ""

        while True:
            if not self.interactive and not self.inputs and self.had_explicit_inputs:
                self._paused_native_input_onekey = signature
                self.waiting_for_input = True
                return
            if not self.interactive and not self.inputs and not self.had_explicit_inputs:
                raw = auto
                self._clear_visible_buttons()
            else:
                raw = self._input(auto)
            char = raw[:1] if raw else ""
            if char == "" and (allow_enter or auto == ""):
                self.memory.set_var("RESULTS", [], "")
                self.memory.set_var("RESULT", [], 0)
                self._paused_native_input_onekey = None
                return
            if char and char in allowed:
                self.memory.set_var("RESULTS", [], char)
                self.memory.set_var("RESULT", [], to_int(char))
                self._paused_native_input_onekey = None
                return
            if not self.inputs and not self.had_explicit_inputs:
                break

        fallback = auto if auto != "" else ""
        self.memory.set_var("RESULTS", [], fallback)
        self.memory.set_var("RESULT", [], to_int(fallback))
        self._paused_native_input_onekey = None

    def _exec_native_message_window_config(self) -> None:
        """Small interactive config menu used by MESSAGE_WINDOW helpers.

        Keep the lightweight native menu, but do not silently consume default
        sub-values after an explicit replay script runs out midway through the
        config flow.  The real ERB saves a setting, clears/repaints, and jumps
        back to START until the user selects [9] 設定終了.
        """
        state = self._paused_native_message_window_config or {}
        stage = to_str(state.get("stage", "choice"))
        choice = to_int(state.get("choice", 9))
        rendered = bool(state.get("rendered", False))
        desc_rendered = bool(state.get("desc_rendered", False))
        base_line = to_int(state.get("base_line", self._line_count()))

        def clear_config_lines() -> None:
            self._clear_lines(max(0, self._line_count() - base_line))

        def render_menu() -> bool:
            self._write("[0] メッセージ速度", newline=True)
            self._write("[1] オート時ウェイト", newline=True)
            self._write("[2] 右クリック時ウェイト", newline=True)
            self._write("[3] メッセージアニメ利用", newline=True)
            self._write("[9] 設定終了", newline=True)
            return True

        def render_description(selected: int) -> None:
            descriptions = {
                0: [
                    "タイプ方式のメッセージ速度を設定します",
                    "0.1秒ごとに入力した数値の字数づつ表示されていきます",
                    "また0を指定した場合、タイプ方式のアニメを停止させます",
                    "0-9の間の数値を入力してください",
                ],
                1: [
                    "AUTO設定時のウェイト時間を設定します",
                    "AUTO機能がONの時文字がすべて表示されてから",
                    "設定時間が経過すると自動的に次に進みます",
                    "単位は1msごとですが100未満を設定した場合はノーウェイトモードとなります",
                ],
                2: [
                    "右クリック設定時のウェイト時間を設定します",
                    "右クリック時に設定時間の間だけ",
                    "メッセージを表示するためにウェイトをかけます",
                    "単位は1msごとですが100未満を設定した場合はノーウェイトモードとなります",
                ],
                3: [
                    "メッセージのアニメーションを行うかどうか設定します",
                    "ここで利用しないと指定した場合",
                    "フェードイン・タイプ両方式のアニメーションを停止させます",
                ],
            }
            self._write("┌" + "─" * 36 + "┐", newline=True)
            for line in descriptions.get(selected, []):
                self._write("│" + line.ljust(72) + "│", newline=True)
            self._write("└" + "─" * 36 + "┘", newline=True)

        while True:
            if stage == "choice" and not rendered:
                rendered = render_menu()

            if stage == "choice":
                if not self.interactive and not self.inputs and self.had_explicit_inputs:
                    self._paused_native_message_window_config = {
                        "stage": "choice",
                        "rendered": rendered,
                        "base_line": base_line,
                    }
                    self.waiting_for_input = True
                    return
                choice = to_int(self._input("9"))
                if choice not in {0, 1, 2, 3}:
                    clear_config_lines()
                    self.memory.set_var("RESULT", [], 1)
                    self.memory.set_var("RESULTS", [], "1")
                    self._paused_native_message_window_config = None
                    return
                stage = "yn" if choice == 3 else "value"
                desc_rendered = False

            if stage == "value":
                if not desc_rendered:
                    render_description(choice)
                    desc_rendered = True
                self._exec_native_input_many([0, 9 if choice == 0 else 10000])
                if self.waiting_for_input:
                    self._paused_native_message_window_config = {
                        "stage": stage,
                        "choice": choice,
                        "rendered": rendered,
                        "desc_rendered": desc_rendered,
                        "base_line": base_line,
                    }
                    return
                value = to_int(self.memory.get_var("RESULT", []))
                if choice == 0:
                    self.memory.set_var("GLOBAL", ["メッセージ速度"], max(0, min(9, value)))
                elif choice == 1:
                    self.memory.set_var("GLOBAL", ["オート時ウェイト"], 0 if value < 100 else value)
                elif choice == 2:
                    self.memory.set_var("GLOBAL", ["右クリック時ウェイト"], 0 if value < 100 else value)
            elif stage == "yn":
                if not desc_rendered:
                    render_description(choice)
                    desc_rendered = True
                self._paused_native_message_window_config = None
                self._exec_native_input_yn("INPUT_YN", ["利用しない", "利用する"])
                if self.waiting_for_input:
                    self._paused_native_message_window_config = {
                        "stage": stage,
                        "choice": choice,
                        "rendered": rendered,
                        "desc_rendered": desc_rendered,
                        "base_line": base_line,
                    }
                    return
                value = to_int(self.memory.get_var("RESULT", []))
                self.memory.set_var("GLOBAL", ["メッセージアニメ利用"], value)

            self._exec_persistence("SAVEGLOBAL", "")
            clear_config_lines()
            stage = "choice"
            rendered = False
            desc_rendered = False

    def _exec_native_message_window_log(self, args: list[Value]) -> None:
        """MESSAGE_WINDOW_LOG viewer with non-interactive replay blocking."""
        mode = to_int(args[5]) if len(args) >= 6 else 0
        if mode <= 0:
            call_builtin(self, "MESSAGE_WINDOW_LOG", args)
            self._paused_native_message_window_log = None
            return

        signature = tuple(to_str(a) for a in args)
        paused = self._paused_native_message_window_log
        if isinstance(paused, dict):
            resume = paused.get("signature") == signature
            base_line = to_int(paused.get("base_line", self._line_count()))
        else:
            resume = paused == signature
            base_line = self._line_count()

        def clear_viewer() -> None:
            self._clear_lines(max(0, self._line_count() - base_line))

        if not resume:
            base_line = self._line_count()
            call_builtin(self, "MESSAGE_WINDOW_LOG", args)

        if self.interactive:
            clear_viewer()
            self._paused_native_message_window_log = None
            return

        if not self.interactive:
            if self.inputs:
                self._input("")
                clear_viewer()
                self._paused_native_message_window_log = None
                return
            if self.had_explicit_inputs:
                self._paused_native_message_window_log = {
                    "signature": signature,
                    "base_line": base_line,
                }
                self.waiting_for_input = True
                return

        clear_viewer()
        self._paused_native_message_window_log = None

    def _exec_native_mouseui_store_helper(self, key: str, args: list[Any]) -> None:
        """Fast path for eraMegaten mouse-shop cache builders.

        These helpers mostly precompute sparse 20,000-element shop tables before
        any player-visible prompt.  Running the ERB literally is correct but
        burns hundreds of thousands of interpreter steps on empty/default data.
        Keep the externally relevant state deterministic and sparse so
        non-interactive compatibility runs reach the actual menu input point.
        """
        if key == "MOUSEUISTORE_SET_VALUE":
            self.memory.varset("MouseUIStore_ItemValues", 0)
        elif key == "MOUSEUISTORE_YEN_ONSALES" and args and isinstance(args[0], RefArg):
            self._varset_ref(args[0], 0)
        elif key == "MOUSEUISTORE_DISPLAYS" and len(args) >= 3 and isinstance(args[2], RefArg):
            self._varset_ref(args[2], 0)
        self.memory.set_var("RESULT", [], 0)
        self.memory.set_var("RESULTS", [], "0")

    def _exec_native_temp_status_reset(self) -> None:
        """Fast path for eraMegaten's battle-end temporary status cleanup.

        The ERB helper bulk-resets a set of CFLAG damage/status modifiers and
        then calls the very expensive `SYNC_STATUS` for every registered
        character.  Battle-end paths commonly include temporary enemy speed
        modifiers for arena encounters, but those enemies are deleted shortly
        afterwards.  Reset the same modifier cells directly and only replay
        `SYNC_STATUS` for non-enemy characters whose HP/MP/speed modifiers were
        actually non-zero.
        """

        csv = self.program.csv

        def cflag_index(name: str) -> int | None:
            if not csv:
                return None
            value = csv.resolve_index("CFLAG", name)
            return value if value != 0 else None

        reset_indices: set[int] = set()
        for name in ("物理被傷害補正", "物理与傷害補正"):
            base = cflag_index(name)
            if base is not None:
                reset_indices.update(base + i for i in range(3))
        for name in ("剣撃被傷害補正", "剣撃与傷害補正"):
            base = cflag_index(name)
            if base is not None:
                reset_indices.update(base + i for i in range(19))

        hp_idx = cflag_index("ＨＰ補正")
        mp_idx = cflag_index("ＭＰ補正")
        speed_idx = cflag_index("速度補正")
        pt_idx = cflag_index("PTフラグ")
        sync_indices = [i for i in (hp_idx, mp_idx, speed_idx) if i is not None]

        needs_sync: list[int] = []
        for chara, state in enumerate(self.memory.characters):
            cflag = state.numeric.setdefault("CFLAG", {})
            for idx in reset_indices:
                cflag[(idx,)] = 0
            touched_sync_field = False
            for idx in sync_indices:
                if to_int(cflag.get((idx,), 0)) != 0:
                    touched_sync_field = True
                cflag[(idx,)] = 0
            if not touched_sync_field:
                continue
            if pt_idx is None or to_int(cflag.get((pt_idx,), 0)) != 0:
                needs_sync.append(chara)

        for chara in needs_sync:
            self._call_sync("SYNC_STATUS", [chara], max_steps=50000)

    def _exec_native_equipment_enhance_expand(self, args: list[Value]) -> bool:
        """Fast path for the common no-enhancement branch of 装備強化_展開.

        High-progress saves call this helper thousands of times while
        recalculating battle stats/resistances.  When 魔装術 is not installed,
        or when the character has no CSTR:装備強化 payload, the ERB function
        usually returns one of the existing/default values without inspecting
        the 100-entry enhancement list.  Handle only those straight-through
        cases here and fall back to ERB for actual enhancement data or
        強化段階 queries.
        """

        if len(args) < 3:
            return False
        chara = to_int(args[0])
        equipment = to_int(args[1]) if len(args) >= 2 else 0
        category = to_str(args[2])
        detail = to_str(args[3]) if len(args) >= 4 else ""
        current = to_int(args[4]) if len(args) >= 5 else -1

        if chara < 0:
            return False

        def return_result(value: int) -> None:
            self.memory.set_var("RESULT", [], value)
            self.memory.set_var("RESULTS", [], str(value))

        master = to_int(self.memory.get_var("MASTER", []))
        magic_art_on = to_int(self.memory.get_var("EQUIP", [master, "魔装術"])) != 0
        if not magic_art_on:
            if category == "戦闘能力修正":
                if equipment == 0 or current == -1:
                    return True  # bare RETURN: preserve caller's RESULT
                return_result(current)
                return True
            if category == "防御相性":
                return_result(100 if equipment == 0 or current == -1 else current)
                return True
            if category in {"攻撃相性", "追加効果"}:
                return True  # bare RETURN
            return False

        if category == "強化段階":
            return False
        cstr = to_str(self.memory.get_var("CSTR", [chara, "装備強化"]))
        if cstr != "":
            return False
        return_result(0 if detail == "ステート" and current == -1 else current)
        return True

    def _cpd_registration_field_base(self, name: str) -> str | None:
        db = self.program.csv
        if not db:
            return None
        # CPD_REGISTRATION.ERB uses GETNUM(ABL/BASE/CFLAG, name) > 0 in this
        # exact order.  Treat only names actually declared for that variable as
        # matches; the engine-wide bare CSV constants (e.g. BASE:LV installing a
        # bare LV) must not make GETNUM(ABL,"LV") choose ABL here.
        for base in ("ABL", "BASE", "CFLAG"):
            idx = db.name_to_index.get(norm_name(base), {}).get(norm_name(name), 0)
            if idx > 0:
                return base
        return None

    def _cpd_field_name(self, index: int) -> str:
        return to_str(call_builtin(self, "GET_CPD_STRFLAG", [index]) or "")

    def _cpd_field_count(self) -> int:
        return to_int(call_builtin(self, "GET_CPD_STRFLAG_NUM", [""]) or 0)

    def _cpd_find_slot(self, field: str, value: int) -> int:
        return to_int(call_builtin(self, "STRFLAG_NUM_CPD_FIND", [field, value]) or -1)

    def _cpd_saved_value(self, slot: int, field: str) -> str:
        value = call_builtin(self, "GET_CPD_SAVESTR_NUM", [slot, field])
        return "" if value is None else to_str(value)

    def _cpd_talent_name(self, index: int) -> str:
        if not self.program.csv:
            return ""
        return self.program.csv.index_to_name.get("TALENT", {}).get(index, "")

    def _cpd_current_payload(self, chara: int) -> str:
        no = to_int(self.memory.get_var("NO", [chara]))
        fields: list[str] = [str(no)]
        for idx in range(2, self._cpd_field_count()):
            name = self._cpd_field_name(idx)
            base = self._cpd_registration_field_base(name)
            if base is None:
                fields.append("想定外の動作です")
            else:
                fields.append(str(to_int(self.memory.get_var(base, [chara, name]))))

        for talent_idx in range(159):
            tname = self._cpd_talent_name(talent_idx)
            if talent_idx == 0:
                if to_int(self.memory.get_var("TALENT", [chara, talent_idx])) or to_int(call_builtin(self, "CSVTALENT", [no, talent_idx, 0]) or 0):
                    fields.append(tname)
                elif to_int(self.memory.get_var("TALENT", [chara, "再生処女"])):
                    fields.append("再生処女")
                continue
            if talent_idx in {1, 3, 4, 5, 6, 7, 8, 9, 73, 74, 75, 76, 77, 153, 154}:
                continue
            if talent_idx in {83, 84, 145, 155}:
                if to_int(call_builtin(self, "CSVTALENT", [no, talent_idx, 0]) or 0):
                    fields.append(tname)
            elif to_int(self.memory.get_var("TALENT", [chara, talent_idx])):
                fields.append(tname)
        return ",".join(fields)

    def _exec_native_strflag_num_cpd(self, args: list[Value]) -> None:
        chara = to_int(args[0]) if args else -1
        mode = to_str(args[1]) if len(args) >= 2 else ""
        if chara < 0 or chara >= to_int(self.memory.get_var("CHARANUM", [])):
            self._fatal("THROW: 登録番号が異常です")
            return

        no = to_int(self.memory.get_var("NO", [chara]))
        if mode == "CLEAR":
            slot = self._cpd_find_slot("NO", no)
            if slot == -1:
                return
            last = self._cpd_find_slot("", -1) - 1
            if last >= 2000:
                self.memory.set_var("SAVESTR", [slot], self.memory.get_var("SAVESTR", [last]))
                self.memory.set_var("SAVESTR", [last], "")
            return

        if mode in {"ADD", "DIFF"}:
            payload = self._cpd_current_payload(chara)
            slot = self._cpd_find_slot("NO", no)
            if mode == "ADD":
                if slot == -1:
                    slot = self._cpd_find_slot("", -1)
                self.memory.set_var("SAVESTR", [slot], f"{to_str(self.memory.get_var('NAME', [chara]))},{payload}")
                return

            unreg_idx = self.program.csv.resolve_index("CFLAG", "全書召喚不可") if self.program.csv else 0
            cond_idx = self.program.csv.resolve_index("CFLAG", "合体条件有り") if self.program.csv else 0
            cannot_summon = to_int(call_builtin(self, "CSVCFLAG", [no, unreg_idx, 0]) or 0) == 1
            condition_locked = to_int(call_builtin(self, "CSVCFLAG", [no, cond_idx, 0]) or 0) == 1 and to_int(self.memory.get_var("FLAG", [10000 + no])) == 0
            high_race = to_int(self.memory.get_var("ABL", [chara, "種族"])) > 18
            color = 0
            if cannot_summon or condition_locked or high_race:
                color = 0
            elif slot == -1:
                color = 0x00FFFF
            elif to_int(self.memory.get_var("BASE", [chara, "ＥＸＰ"])) < to_int(self._cpd_saved_value(slot, "ＥＸＰ")):
                color = 0xDC143C
            else:
                saved = to_str(self.memory.get_var("SAVESTR", [slot]))
                comma = saved.find(",")
                if payload != (saved[comma + 1 :] if comma >= 0 else ""):
                    color = 0x00FFFF
            self.memory.set_var("文字色変更", [], color)
            return

        if mode == "REFER":
            slot = self._cpd_find_slot("NO", no)
            if slot == -1:
                return
            parts = to_str(self.memory.get_var("SAVESTR", [slot])).split(",")
            for idx in range(2, self._cpd_field_count()):
                name = self._cpd_field_name(idx)
                base = self._cpd_registration_field_base(name)
                if base is None:
                    self._fatal(f"THROW: 想定外の動作が発生しました。{self._cpd_field_count()}")
                    return
                self.memory.set_var(base, [chara, name], to_int(parts[idx]) if idx < len(parts) else 0)
            saved_talents = set(parts)
            for talent_idx in range(159):
                tname = self._cpd_talent_name(talent_idx)
                self.memory.set_var("TALENT", [chara, talent_idx], 1 if tname != "" and tname in saved_talents else 0)
            return

    def _exec_native_entry_equipment_compendium(self) -> None:
        """Register owned equipment/item ids in the compendium flag range.

        The eraMegaten helper is a straight `FOR LOCAL,1000,9999` scan:
        `SIF ITEM:LOCAL > 0; FLAG:(LOCAL + 40000) = 1`. Replaying that loop
        literally costs tens of thousands of interpreter steps during shop
        redraws after some submenus. The Python runtime stores arrays sparsely,
        so iterating materialized ITEM cells preserves the same externally
        visible state for all non-zero inventory slots while avoiding the
        redraw budget spike.
        """
        for idx, value in list(self.memory.numeric.get("ITEM", {}).items()):
            if len(idx) != 1 or int(value) <= 0:
                continue
            item_no = int(idx[0])
            if 1000 <= item_no < 9999:
                self.memory.set_var("FLAG", [item_no + 40000], 1)

    def _exec_native_print_formation_face_p(self, args: list[Value]) -> bool:
        """Fast path for formation HTML image lookups.

        `PRINT_FORMATION_FACE_P` is called many times while rendering party
        and arena-match formation panels.  In the common `"ア禮服取得"` mode it
        does not print anything; it only returns an image resource name in
        RESULTS or `NO_IMG`.  Replaying the full ERB path runs every character's
        special-face hook and several resource probes for each visible row.  For
        ordinary, non-transformed characters we can compute the same resource
        probe directly from `NO`, face-graphic settings and the sprite registry.
        Complex transformed/custom-image cases fall back to the ERB function.
        """
        mode = to_str(args[3]) if len(args) >= 4 else ""
        if "ア禮服取得" not in mode:
            return False
        chara = to_int(args[0]) if args else -1
        line = to_int(args[1]) if len(args) >= 2 else 1
        img_size = to_int(args[2]) if len(args) >= 3 else 0
        if chara < 0 or chara >= len(self.memory.characters):
            self.memory.set_var("RESULTS", [], "NO_IMG")
            self.memory.set_var("RESULT", [], 0)
            return True
        if (
            to_int(self.memory.get_var("CFLAG", [chara, "悪魔変身"]))
            or to_int(self.memory.get_var("FLAG", ["画像表示設定"])) == 3
        ):
            return False

        img_type = img_size
        if img_type < 2:
            face = to_int(self.memory.get_var("CFLAG", [chara, "顔グラ"]))
            if face > 0:
                img_type += face * 100
        candidates: list[int] = []
        appearance = to_int(self.memory.get_var("CFLAG", [chara, "外見番号"]))
        if appearance > 0:
            candidates.append(appearance)
        candidates.append(to_int(self.memory.get_var("NO", [chara])))
        for image_no in candidates:
            name = f"A{image_no}_{img_type}_{line}"
            if self._sprite_created(name):
                self.memory.set_var("RESULTS", [], name)
                self.memory.set_var("RESULT", [], 1)
                return True
        self.memory.set_var("RESULTS", [], "NO_IMG")
        self.memory.set_var("RESULT", [], 0)
        return True

    def _varset_ref(self, ref: RefArg, value: Value = 0) -> None:
        key = norm_name(ref.base)
        if ref.frame is not None:
            if key in ref.frame.strings or self.memory.is_string_base(key) or isinstance(value, str):
                ref.frame.strings[key] = {}
                if value not in (0, ""):
                    ref.frame.strings[key][()] = to_str(value)
            else:
                ref.frame.numeric[key] = {}
                if value not in (0, ""):
                    ref.frame.numeric[key][()] = to_int(value)
            return
        self.memory.varset(ref.base, value)

    def _exec_try_call(self, frame: ExecFrame, target: str, args: list[Value]) -> None:
        exists = bool(self._ordered_functions(target))
        catch_pc = self._find_catch_for_try(frame, frame.pc)
        if exists:
            self._push_call_sequence(target, args, try_only=True)
            frame.pc += 1
        elif catch_pc is not None:
            frame.pc = catch_pc + 1
        else:
            frame.pc += 1

    def _exec_try_jump(self, frame: ExecFrame, target: str, args: list[Value]) -> None:
        if self._ordered_functions(target):
            self._return()
            self._push_call_sequence(target, args, try_only=True)
        else:
            frame.pc += 1

    def _exec_try_list(self, frame: ExecFrame, key: str) -> None:
        """Execute private-Emuera TRYCALLLIST/TRYJUMPLIST/TRYGOTOLIST blocks.

        The block shape is:

            TRYCALLLIST
                FUNC Candidate1
                FUNC Candidate2
            ENDFUNC

        FUNC candidates are tried in order.  TRYCALLLIST resumes after ENDFUNC
        once the selected function returns; TRYJUMPLIST replaces the current
        frame like JUMP; TRYGOTOLIST jumps to the first existing label in the
        current function.  Missing candidates are silent, matching TRY* calls.
        """
        end_pc, candidates = self._try_list_block(frame)
        after_pc = end_pc + 1
        if key == "TRYGOTOLIST":
            for target_text, _ in candidates:
                label = norm_name(self.render_form(target_text).strip().lstrip("$"))
                if label in frame.fn.labels:
                    frame.pc = self._jump_target_pc(frame, frame.fn.labels[label])
                    return
            frame.pc = after_pc
            return
        for target, args in candidates:
            if not self._ordered_functions(target):
                continue
            if key == "TRYCALLLIST":
                frame.pc = after_pc
                self._push_call_sequence(target, args, try_only=True)
            else:
                self._return()
                self._push_call_sequence(target, args, try_only=True)
            return
        frame.pc = after_pc

    def _try_list_block(self, frame: ExecFrame) -> tuple[int, list[tuple[str, list[Any]]]]:
        candidates: list[tuple[str, list[Any]]] = []
        depth = 0
        i = frame.pc + 1
        while i < len(frame.fn.lines):
            text = frame.fn.lines[i].text.strip()
            k, rest = self._keyword(text)
            if k in {"TRYCALLLIST", "TRYJUMPLIST", "TRYGOTOLIST"}:
                depth += 1
            elif k == "ENDFUNC":
                if depth == 0:
                    return i, candidates
                depth -= 1
            elif depth == 0 and k == "FUNC":
                parsed = split_call_syntax(rest)
                if parsed:
                    target_expr, arg_texts = parsed
                    target = self.render_form(target_expr).strip()
                    candidates.append((target, self._eval_call_args(target, arg_texts)))
            i += 1
        return len(frame.fn.lines) - 1, candidates

    def _begin(self, rest: str) -> None:
        state = norm_name(rest.split()[0] if rest else "TITLE")
        self._reset_display_style()
        # A BEGIN transition redraws a different Emuera state.  Any numeric
        # choices harvested from the abandoned screen must not constrain the
        # first INPUT in the new state; otherwise non-interactive replays can
        # stop before consuming a valid queued command for TRAIN/ABLUP/etc.
        self._clear_visible_buttons()
        if state == "SHOP":
            self.stack.clear(); self.memory.frames.clear()
            if not self._push_shop_loop():
                self._push_call_sequence("EVENTSHOP", [], try_only=True)
            return
        if state == "TRAIN":
            self.stack.clear(); self.memory.frames.clear()
            if not self._push_train_loop():
                self._push_call_sequence("EVENTTRAIN", [], try_only=True)
            return
        if state == "ABLUP":
            self._begin_ablup_flow()
            return
        mapping = {
            "TITLE": "SYSTEM_TITLE",
            "FIRST": "EVENTFIRST",
            "SHOP": "EVENTSHOP",
            "COM": "EVENTCOM",
            "AFTERTRAIN": "EVENTEND",
            "TURNEND": "EVENTTURNEND",
            "LOAD": "EVENTLOAD",
        }
        target = mapping.get(state, state)
        # State transition clears current function stack in Era semantics.
        self.stack.clear(); self.memory.frames.clear()
        if not self._push_call_sequence(target, [], try_only=True):
            self.warn(f"BEGIN target not found: {state}->{target}")

    def _begin_ablup_flow(self) -> None:
        """Headless-compatible ABLUP state transition.

        Emuera has a built-in `BEGIN ABLUP` phase between EVENTEND and
        EVENTTURNEND.  eraMegaten supplies the actual ability-up menu in ERB
        (`ABL_MANUAL_MAIN` plus `ABLUP0..99`).  Recreate that state by pushing
        the post-ABL event underneath the optional menu frame so the flow does
        not dead-end at an unknown `ABLUP` function.  In non-interactive
        compatibility runs with no queued input, skip the optional menu and
        proceed to turn-end rather than accidentally auto-clicking the first
        printed ability button.
        """
        self.stack.clear(); self.memory.frames.clear()
        # Do not use a synthetic `CALL EVENTTURNEND` line here: CALL dispatch
        # intentionally treats EVENTTURNEND as a native compatibility no-op in
        # some source paths, while BEGIN-state transitions must run the real
        # event sequence.
        self._push_call_sequence("EVENTTURNEND", [], try_only=True)
        if (self.interactive or self.inputs) and self.program.get_function("ABL_MANUAL_MAIN"):
            self._push_call_sequence("ABL_MANUAL_MAIN", [1], try_only=True)

    def _reset_display_style(self) -> None:
        # Emuera私家改造版 resets display style on BEGIN and RESETDATA.  Keep
        # transient flow/input flags intact, but clear the visible font/color
        # style state that would otherwise leak into the next screen.
        self.current_font = self.default_font
        self.current_font_style = 0
        self.current_color = self.default_color
        self.current_bgcolor = self.default_bgcolor

    def _exec_resetglobal(self) -> None:
        # RESETGLOBAL resets only GLOBAL/GLOBALS and #DIM/#DIMS GLOBAL
        # variables.  Clearing the sparse tables is equivalent to assigning
        # 0/"" to every element because Memory defaults absent numeric/string
        # slots to those values.
        numeric_keys = {"GLOBAL"}
        string_keys = {"GLOBALS"}
        for key, decl in self.program.var_decls.items():
            if not decl.global_scope:
                continue
            if decl.is_string:
                string_keys.add(key)
            else:
                numeric_keys.add(key)
        for key in numeric_keys:
            self.memory.numeric[key] = {}
        for key in string_keys:
            self.memory.strings[key] = {}

    def _exec_print(self, key: str, rest: str, pause_signature: tuple[str, ...] | None = None) -> None:
        if self._display_suppressed():
            return
        kana_print = self._is_printk_key(key)
        if kana_print:
            key = self._strip_printk_key(key)
        default_color_print = self._is_default_color_print_key(key)
        if default_color_print:
            key = self._strip_print_default_color_key(key)
        wait_after_print = key.endswith("W")
        signature = pause_signature or (key, rest)
        saved_color = self.current_color
        if default_color_print:
            self.current_color = self.default_color
        try:
            if wait_after_print and self._paused_print_wait == signature:
                if self.interactive or self.inputs:
                    self._input("")
                    self._paused_print_wait = None
                    return
                if not self.interactive and self.had_explicit_inputs:
                    self.waiting_for_input = True
                    return
                self._paused_print_wait = None
                return
            plain_print = key.startswith("PRINTPLAIN")
            newline = key.endswith(("L", "W", "LC", "WC")) or "FORML" in key or "FORMW" in key or key in {"PRINTL", "PRINTW", "PRINTDATA", "HTML_PRINT"}
            if key == "PRINT_RECT":
                frame_width = to_int(eval_expr(self, rest, default=0)) if rest else 0
                placeholder = "▭" * max(1, min(72, frame_width // 100 if frame_width else 1))
                self._record_print_rect(frame_width, self._next_visual_write_start_line(), self._current_line_col())
                self._write(placeholder, newline=False)
                return
            if key == "PRINT_IMG":
                name = to_str(eval_expr(self, rest, default=self.render_form(rest) if ("%" in rest or "{" in rest or "\\@" in rest) else rest)).strip()
                if name:
                    self._record_print_img(name, self._next_visual_write_start_line(), self._current_line_col())
                    self._write(f"[IMG:{name}]", newline=False)
                return
            if key.startswith("PRINTBUTTON"):
                args = split_era_args(rest)
                text = to_str(eval_expr(self, args[0], default="")) if args else ""
                value = to_str(eval_expr(self, args[1], default=self.render_form(args[1]))) if len(args) >= 2 else ""
                if len(args) >= 2 and not self.interactive and not self._display_suppressed():
                    self.pending_buttons.append(value)
                if key in {"PRINTBUTTONC", "PRINTBUTTONLC"}:
                    display_text = self._printc_cell_text(text)
                    self._record_print_button(display_text, value, self._next_visual_write_start_line(), self._current_line_col())
                    self._write_printc_cell(text, newline=newline)
                else:
                    self._record_print_button(text, value, self._next_visual_write_start_line(), self._current_line_col())
                    self._write(text, newline=False)
                return
            if key.startswith("PRINTV"):
                text = to_str(eval_expr(self, rest, default="")) if rest else ""
            elif key.startswith("PRINTS"):
                text = self._eval_prints_text(rest)
            elif key == "HTML_PRINT":
                text = to_str(eval_expr(self, rest, default=self.render_form(rest) if ("%" in rest or "{" in rest or "\\@" in rest) else rest)) if rest else ""
            elif "FORM" in key:
                text = self.render_form(rest)
            elif rest.startswith('"') or rest.startswith('@"'):
                text = to_str(eval_expr(self, rest, default=""))
            elif "%" in rest or "\\@" in rest:
                text = self._render_plain_print_form(rest)
            else:
                text = rest
            if kana_print:
                text = self._apply_force_kana(text)
            if self._is_printc_key(key):
                self._write_printc_cell(text, newline=newline, harvest_buttons=not plain_print)
                return
            html_start_line = self._next_write_start_line() if key == "HTML_PRINT" else 0
            html_visual_start_line = self._next_visual_write_start_line() if key == "HTML_PRINT" else 0
            html_visual_start_col = self._current_line_col() if key == "HTML_PRINT" else 0
            self._write(text, newline=newline, harvest_buttons=(key != "HTML_PRINT" and not plain_print), record_style=(key != "HTML_PRINT"))
            if key == "HTML_PRINT":
                self._record_html_print(text, html_start_line, self._line_count(), html_visual_start_line, html_visual_start_col)
            if wait_after_print:
                if self.interactive or self.inputs:
                    self._input("")
                elif not self.interactive and self.had_explicit_inputs:
                    self._paused_print_wait = signature
                    self.waiting_for_input = True
        finally:
            if default_color_print:
                self.current_color = saved_color

    _HTML_BUTTON_RE = re.compile(r"<\s*button\b(?P<attrs>[^>]*)>(?P<label>.*?)<\s*/\s*button\s*>", re.IGNORECASE | re.DOTALL)
    _HTML_NONBUTTON_RE = re.compile(r"<\s*nonbutton\b(?P<attrs>[^>]*)>(?P<label>.*?)<\s*/\s*nonbutton\s*>", re.IGNORECASE | re.DOTALL)
    _HTML_IMG_RE = re.compile(r"<\s*img\b(?P<attrs>[^>]*)/?\s*>", re.IGNORECASE | re.DOTALL)
    _HTML_BR_RE = re.compile(r"<\s*br\b[^>]*>", re.IGNORECASE)
    _HTML_TAG_RE = re.compile(r"<\s*(?P<close>/)?\s*(?P<name>[a-zA-Z][\w:-]*)(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
    _HTML_ATTR_RE = re.compile(
        r"""(?P<name>[\w:-]+)\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s"'=<>`]+))""",
        re.IGNORECASE,
    )

    def _html_attrs(self, attr_text: str) -> dict[str, str]:
        attrs: dict[str, str] = {}
        for attr in self._HTML_ATTR_RE.finditer(attr_text):
            raw = attr.group("dq") if attr.group("dq") is not None else attr.group("sq") if attr.group("sq") is not None else attr.group("bare")
            attrs[norm_name(attr.group("name"))] = html_lib.unescape(raw or "")
        return attrs

    def _html_style_payload(self, style: dict[str, Any]) -> dict[str, Any]:
        return {
            "color": to_int(style.get("color", self.current_color)),
            "bgcolor": to_int(style.get("bgcolor", self.current_bgcolor)),
            "font": to_str(style.get("font", self.current_font)),
            "font_style": to_int(style.get("font_style", 0)),
            "alignment": to_str(style.get("alignment", self.current_alignment)),
            "tooltip_delay": to_int(style.get("tooltip_delay", self.current_tooltip_delay)),
            "tooltip_color": to_int(style.get("tooltip_color", self.current_tooltip_color)),
        }

    def _html_parse_color(self, value: str, default: int) -> int:
        raw = html_lib.unescape(to_str(value)).strip().strip("\"'")
        if not raw:
            return default
        text = raw
        if text.startswith("#"):
            text = text[1:]
            if len(text) == 3 and re.fullmatch(r"[0-9a-fA-F]{3}", text):
                text = "".join(ch * 2 for ch in text)
            if re.fullmatch(r"[0-9a-fA-F]{6,8}", text):
                return int(text[-6:], 16)
        if re.fullmatch(r"0x[0-9a-fA-F]+", text):
            try:
                return int(text, 16) & 0xFFFFFF
            except Exception:
                return default
        if re.fullmatch(r"[0-9a-fA-F]{6,8}", text):
            return int(text[-6:], 16)
        try:
            return parse_era_int(text) & 0xFFFFFF
        except Exception:
            return color_by_known_name(text, default)

    def _html_apply_open_tag_style(self, style: dict[str, Any], tag_name: str, attrs: dict[str, str]) -> dict[str, Any]:
        style = dict(style)
        tag = norm_name(tag_name)
        if tag == "FONT":
            if "COLOR" in attrs:
                style["color"] = self._html_parse_color(attrs.get("COLOR", ""), to_int(style.get("color", self.current_color)))
            if "FACE" in attrs:
                style["font"] = attrs.get("FACE", "") or style.get("font", self.current_font)
        elif tag in {"B", "STRONG"}:
            style["font_style"] = to_int(style.get("font_style", 0)) | 1
        elif tag in {"I", "EM"}:
            style["font_style"] = to_int(style.get("font_style", 0)) | 2
        elif tag == "U":
            style["font_style"] = to_int(style.get("font_style", 0)) | 4
        elif tag in {"S", "STRIKE", "DEL"}:
            style["font_style"] = to_int(style.get("font_style", 0)) | 8
        elif tag in {"P", "DIV"}:
            align = norm_name(attrs.get("ALIGN", ""))
            if align in {"LEFT", "CENTER", "RIGHT"}:
                style["alignment"] = align
        elif tag == "CENTER":
            style["alignment"] = "CENTER"
        return style

    def _html_style_at_offset(self, html_text: str, offset: int) -> dict[str, Any]:
        stack: list[tuple[str, dict[str, Any]]] = []
        style = self._style_snapshot()
        for match in self._HTML_TAG_RE.finditer(html_text):
            if match.start() >= offset:
                break
            tag = norm_name(match.group("name"))
            if tag == "BR":
                continue
            if match.group("close"):
                for i in range(len(stack) - 1, -1, -1):
                    if stack[i][0] == tag:
                        del stack[i:]
                        break
                style = stack[-1][1] if stack else self._style_snapshot()
                continue
            attrs = self._html_attrs(match.group("attrs"))
            style = self._html_apply_open_tag_style(style, tag, attrs)
            if not match.group("attrs").strip().endswith("/"):
                stack.append((tag, style))
        return self._html_style_payload(style)

    def _html_label_style(self, label_html: str, base_style: dict[str, Any]) -> dict[str, Any]:
        style = dict(base_style)
        stack: list[tuple[str, dict[str, Any]]] = []
        pos = 0
        for match in self._HTML_TAG_RE.finditer(label_html):
            if match.start() > pos and label_html[pos:match.start()].strip():
                return self._html_style_payload(style)
            tag = norm_name(match.group("name"))
            if tag == "BR":
                pos = match.end()
                continue
            if match.group("close"):
                for i in range(len(stack) - 1, -1, -1):
                    if stack[i][0] == tag:
                        del stack[i:]
                        break
                style = stack[-1][1] if stack else dict(base_style)
            else:
                attrs = self._html_attrs(match.group("attrs"))
                style = self._html_apply_open_tag_style(style, tag, attrs)
                if not match.group("attrs").strip().endswith("/"):
                    stack.append((tag, style))
            pos = match.end()
        return self._html_style_payload(style)

    def _record_html_text_run(self, text: str, display_line: int, col: int, style: dict[str, Any]) -> int:
        plain = html_lib.unescape(text)
        if not plain:
            return col
        self.html_text_runs.append(
            {
                "text": plain,
                "col": col,
                **self._html_style_payload(style),
            }
        )
        self._html_text_lines.append(display_line)
        return col + self._layout_text_width(plain)

    def _html_img_width_cols(self, attrs: dict[str, str]) -> int:
        width_text = attrs.get("WIDTH", "").strip()
        width = 0
        if width_text:
            try:
                width = int(float(width_text))
            except Exception:
                width = 0
        if width <= 0 and attrs.get("SRC"):
            natural_width, _ = self._sprite_dimension_strings(attrs.get("SRC", ""))
            try:
                width = int(float(natural_width)) if natural_width else 0
            except Exception:
                width = 0
        # Page-model `col` is a character-cell coordinate.  HTML image widths
        # are pixel-like, so approximate one cell as the default 8 px font
        # cell; `html_layout_model(char_width=...)` then maps the column back
        # to the caller's chosen coordinate system.
        return max(1, (max(0, width) + 7) // 8) if width else 1

    def _html_fragment_visible_cols(self, html_fragment: str) -> int:
        total = 0
        pos = 0
        for match in self._HTML_TAG_RE.finditer(html_fragment):
            if match.start() > pos:
                total += self._layout_text_width(html_lib.unescape(html_fragment[pos:match.start()]))
            tag = norm_name(match.group("name"))
            if tag == "IMG" and not match.group("close"):
                total += self._html_img_width_cols(self._html_attrs(match.group("attrs")))
            pos = match.end()
        if pos < len(html_fragment):
            total += self._layout_text_width(html_lib.unescape(html_fragment[pos:]))
        return total

    def _html_control_lengths(self, html_text: str) -> tuple[dict[int, int], dict[int, int]]:
        open_lengths: dict[int, int] = {}
        close_lengths: dict[int, int] = {}
        for regex in (self._HTML_BUTTON_RE, self._HTML_NONBUTTON_RE):
            for match in regex.finditer(html_text):
                length = self._html_fragment_visible_cols(match.group("label").strip())
                open_lengths[match.start()] = length
                close_start = html_text.rfind("</", match.start(), match.end())
                if close_start >= 0:
                    close_lengths[close_start] = length
        return open_lengths, close_lengths

    def _record_html_text_runs(
        self,
        html_text: str,
        visual_start_line: int,
        visual_start_col: int = 0,
    ) -> tuple[dict[int, tuple[int, int]], dict[int, tuple[int, int]]]:
        """Extract styled visible text outside button/nonbutton controls.

        Raw `output` intentionally keeps the original HTML markup, but modern
        GUI adapters need styled text runs such as Formation's
        ``<font color=...>■</font>WEAK`` legends.  Record a lightweight visual
        stream: tags mutate style, ``<br>`` advances the visual row, text inside
        button/nonbutton controls is represented by those controls instead and
        is therefore skipped here.
        """

        line = int(visual_start_line)
        col = max(0, int(visual_start_col))
        pos = 0
        style = self._style_snapshot()
        style_stack: list[tuple[str, dict[str, Any]]] = []
        suppressed = 0
        control_positions: dict[int, tuple[int, int]] = {}
        image_positions: dict[int, tuple[int, int]] = {}
        control_length_stack: list[int] = []
        open_control_lengths, close_control_lengths = self._html_control_lengths(html_text)
        void_tags = {"BR", "IMG", "HR", "INPUT", "META", "LINK"}

        for match in self._HTML_TAG_RE.finditer(html_text):
            if match.start() > pos and suppressed <= 0:
                col = self._record_html_text_run(html_text[pos:match.start()], line, col, style)
            tag = norm_name(match.group("name"))
            attrs_text = match.group("attrs")
            self_closing = tag in void_tags or attrs_text.strip().endswith("/")

            if tag == "BR" and not match.group("close"):
                if suppressed <= 0:
                    line += 1
                    col = 0
                pos = match.end()
                continue
            if tag == "IMG" and not match.group("close"):
                if suppressed <= 0:
                    image_positions[match.start()] = (line, col)
                    col += self._html_img_width_cols(self._html_attrs(attrs_text))
                pos = match.end()
                continue

            if match.group("close"):
                if tag in {"BUTTON", "NONBUTTON"} and suppressed > 0:
                    suppressed -= 1
                    fallback_length = control_length_stack.pop() if control_length_stack else 0
                    length = close_control_lengths.get(match.start(), fallback_length)
                    if suppressed <= 0:
                        col += length
                else:
                    for i in range(len(style_stack) - 1, -1, -1):
                        if style_stack[i][0] == tag:
                            del style_stack[i:]
                            break
                    style = style_stack[-1][1] if style_stack else self._style_snapshot()
                pos = match.end()
                continue

            if tag in {"BUTTON", "NONBUTTON"}:
                control_positions[match.start()] = (line, col)
                control_length_stack.append(open_control_lengths.get(match.start(), 0))
                suppressed += 1
            elif tag not in void_tags:
                attrs = self._html_attrs(attrs_text)
                style = self._html_apply_open_tag_style(style, tag, attrs)
                if not self_closing:
                    style_stack.append((tag, style))
            pos = match.end()

        if pos < len(html_text) and suppressed <= 0:
            self._record_html_text_run(html_text[pos:], line, col, style)
        return control_positions, image_positions

    def _html_img_parent(
        self,
        parents: list[tuple[int, int, str, dict[str, str]]],
        start: int,
        end: int,
    ) -> tuple[str, dict[str, str]]:
        containing = [p for p in parents if p[0] <= start and end <= p[1]]
        if not containing:
            return "", {}
        # Pick the innermost button/nonbutton if markup is ever nested.
        _, _, kind, attrs = min(containing, key=lambda p: p[1] - p[0])
        return kind, attrs

    def _record_html_print(
        self,
        text: str,
        start_line: int,
        end_line: int,
        visual_start_line: int | None = None,
        visual_start_col: int = 0,
    ) -> None:
        """Keep raw HTML and clickable <button value=...> metadata for UIs.

        The terminal transcript still prints HTML as text and, intentionally,
        does not feed those buttons into `pending_buttons`; non-interactive
        replay should not auto-click a mouse UI.  A GUI/front-end can instead
        inspect `html_output`/`html_buttons` and submit the selected value as a
        normal queued/input value.

        Emuera HTML renders ``<br>`` as visual row breaks inside a single
        ``HTML_PRINT``.  The terminal transcript keeps the raw tag text for
        debugging compatibility, so track a separate visual line offset for
        page/layout models and place elements after the corresponding number
        of preceding ``<br>`` tags.
        """
        if not text:
            return
        visual_start = int(visual_start_line if visual_start_line is not None else start_line)
        visual_col = max(0, int(visual_start_col))
        br_positions = [m.start() for m in self._HTML_BR_RE.finditer(text)]

        def visual_line_for_offset(offset: int) -> int:
            line = visual_start
            for br_pos in br_positions:
                if br_pos < offset:
                    line += 1
                else:
                    break
            return line

        visual_end = max(visual_start, visual_start + len(br_positions))
        self._html_fragments.append((visual_start, visual_end, text))
        self.html_output.append(text)
        control_positions, image_positions = self._record_html_text_runs(text, visual_start, visual_col)
        img_parents: list[tuple[int, int, str, dict[str, str]]] = []
        control_image_contexts: list[tuple[int, int, int, int, int, dict[str, str]]] = []

        def image_position_inside_control(
            label_start: int,
            parent_line: int,
            parent_col: int,
            image_start: int,
        ) -> tuple[int, int]:
            prefix = text[label_start:image_start]
            brs = list(self._HTML_BR_RE.finditer(prefix))
            if brs:
                tail = prefix[brs[-1].end():]
                return parent_line + len(brs), self._html_fragment_visible_cols(tail)
            return parent_line, parent_col + self._html_fragment_visible_cols(prefix)

        for match in self._HTML_BUTTON_RE.finditer(text):
            attrs = self._html_attrs(match.group("attrs"))
            img_parents.append((match.start(), match.end(), "button", attrs))
            value = attrs.get("VALUE", "")
            label = re.sub(r"<[^>]*>", "", match.group("label"))
            self.html_buttons.append({
                "value": value,
                "title": attrs.get("TITLE", ""),
                "pos": attrs.get("POS", ""),
                "label": html_lib.unescape(label).strip(),
            })
            base_style = self._html_style_at_offset(text, match.start())
            line, col = control_positions.get(match.start(), (visual_line_for_offset(match.start()), 0))
            control_image_contexts.append((match.start(), match.end(), match.start("label"), line, col, attrs))
            style = self._html_label_style(match.group("label"), base_style)
            style["col"] = col
            self._html_button_styles.append(style)
            self._html_button_lines.append(line)
        for match in self._HTML_NONBUTTON_RE.finditer(text):
            attrs = self._html_attrs(match.group("attrs"))
            img_parents.append((match.start(), match.end(), "nonbutton", attrs))
            label = re.sub(r"<[^>]*>", "", match.group("label"))
            self.html_nonbuttons.append({
                "title": attrs.get("TITLE", ""),
                "pos": attrs.get("POS", ""),
                "label": html_lib.unescape(label).strip(),
            })
            base_style = self._html_style_at_offset(text, match.start())
            line, col = control_positions.get(match.start(), (visual_line_for_offset(match.start()), 0))
            control_image_contexts.append((match.start(), match.end(), match.start("label"), line, col, attrs))
            style = self._html_label_style(match.group("label"), base_style)
            style["col"] = col
            self._html_nonbutton_styles.append(style)
            self._html_nonbutton_lines.append(line)
        for match in self._HTML_IMG_RE.finditer(text):
            attrs = self._html_attrs(match.group("attrs"))
            src = attrs.get("SRC")
            if src is None:
                continue
            natural_width, natural_height = self._sprite_dimension_strings(src)
            parent_type, parent_attrs = self._html_img_parent(img_parents, match.start(), match.end())
            if (
                match.start() not in image_positions
                and not attrs.get("POS", "").strip()
                and not parent_attrs.get("POS", "").strip()
            ):
                containing = [
                    ctx
                    for ctx in control_image_contexts
                    if ctx[0] <= match.start() and match.end() <= ctx[1] and not ctx[5].get("POS", "").strip()
                ]
                if containing:
                    _, _, label_start, parent_line, parent_col, _ = min(containing, key=lambda ctx: ctx[1] - ctx[0])
                    image_positions[match.start()] = image_position_inside_control(label_start, parent_line, parent_col, match.start())
            image_line, image_col = image_positions.get(match.start(), (visual_line_for_offset(match.start()), 0))
            self.html_images.append({
                "src": src,
                "title": attrs.get("TITLE", ""),
                "pos": attrs.get("POS", ""),
                "width": attrs.get("WIDTH", ""),
                "height": attrs.get("HEIGHT", ""),
                "ypos": attrs.get("YPOS", ""),
                "col": str(image_col) if match.start() in image_positions else "",
                "natural_width": natural_width,
                "natural_height": natural_height,
                "parent": parent_type,
                "parent_pos": parent_attrs.get("POS", ""),
                "parent_title": parent_attrs.get("TITLE", ""),
                "parent_value": parent_attrs.get("VALUE", ""),
            })
            self._html_image_styles.append(self._html_style_at_offset(text, match.start()))
            self._html_image_lines.append(image_line)
        if br_positions:
            count = len(br_positions)
            self._html_visual_line_extra += count
            self._html_raw_line_breaks[start_line] = self._html_raw_line_breaks.get(start_line, 0) + count

    def _sprite_dimension_strings(self, name: str) -> tuple[str, str]:
        info = self._sprite_info(name) or {}
        if not info:
            return "", ""
        return str(to_int(info.get("width", 0))), str(to_int(info.get("height", 0)))

    def _record_print_img(self, name: str, line: int, col: int = 0) -> None:
        """Record PRINT_IMG metadata for GUI/front-end image rendering.

        The terminal transcript keeps the historical ``[IMG:name]`` placeholder,
        but modern front-ends need the image resource name and natural sprite
        size just like HTML ``<img>`` metadata.  eraMegaten's PRINT_IMG names
        are resource sprite identifiers produced by WRITE_IMG/SHOW_IMG or
        resource CSV entries, so resolve dimensions through the same sprite
        registry used by SPRITEWIDTH/SPRITEHEIGHT and PNG export.  Preserve
        the current transcript column too: SHOW_IMG/WRITE_IMG often prints
        full-width padding before PRINT_IMG to place face graphics side by
        side, and the GUI/layout model must not collapse those images to x=0.
        """

        width, height = self._sprite_dimension_strings(name)
        self.print_images.append(
            {
                "src": name,
                "width": width,
                "height": height,
                "col": max(0, int(col)),
            }
        )
        self._print_image_lines.append(max(1, int(line)))

    def _record_print_button(self, label: str, value: str, line: int, col: int) -> None:
        """Record PRINTBUTTON metadata for GUI/front-end hit testing.

        Plain PRINTBUTTON output is also present in the terminal transcript,
        but modern front-ends need the submitted value and screen coordinate in
        the same page/layout model used for HTML buttons.  Keep it separate
        from `pending_buttons`: the latter is only a non-interactive replay
        fallback, while this metadata represents the visible UI element.
        """

        if self._display_suppressed():
            return
        self.print_buttons.append(
            {
                "value": to_str(value),
                "label": to_str(label),
                "col": max(0, int(col)),
            }
        )
        self._print_button_lines.append(max(1, int(line)))
        self._print_button_styles.append(self._style_snapshot())

    def _record_print_rect(self, width: int, line: int, col: int) -> None:
        """Record PRINT_RECT metadata for GUI/front-end placeholder drawing.

        eraMegaten uses PRINT_RECT as the no-resource fallback in WRITE_IMG:
        the terminal transcript keeps a small monospace placeholder, while a
        modern GUI should know the requested rectangle width and current style
        so it can draw an image-sized frame instead of guessing from glyphs.
        """

        if self._display_suppressed():
            return
        self.print_rects.append(
            {
                "width": max(0, int(width)),
                "col": max(0, int(col)),
            }
        )
        self._print_rect_lines.append(max(1, int(line)))
        self._print_rect_styles.append(self._style_snapshot())

    def _record_print_space(self, width: int, line: int, col: int, cells: int) -> None:
        """Record PRINT_SPACE's original Emuera-width spacing for GUI layout.

        The transcript still receives the historical monospace fallback
        (``width // 100`` cells), but D3D-style eraMegaten screens use
        ``PRINT_SPACE`` as a pixel/font-unit spacer between drawables.  Keep the
        raw requested width and the fallback cell count so ``html_layout_model``
        can shift following row elements by the difference without changing
        terminal-compatible text output.
        """

        if self._display_suppressed():
            return
        self.print_spaces.append(
            {
                "width": max(0, int(width)),
                "col": max(0, int(col)),
                "cells": max(0, int(cells)),
            }
        )
        self._print_space_lines.append(max(1, int(line)))
        self._print_space_styles.append(self._style_snapshot())

    def _html_display_lines(self) -> list[str]:
        text = "".join(self.output).rstrip("\n")
        return text.split("\n") if text else []

    def _html_get_printed_str(self, line_no: int | None = None) -> str:
        lines = self._html_display_lines()
        if not lines:
            return ""
        if line_no is None:
            return lines[-1]
        idx = int(line_no)
        return lines[idx] if 0 <= idx < len(lines) else ""

    def _html_pop_printing_str(self) -> str:
        text = "".join(self.output)
        if not text or text.endswith("\n"):
            return ""
        pos = text.rfind("\n")
        current = text[pos + 1:] if pos >= 0 else text
        kept = text[: pos + 1] if pos >= 0 else ""
        self.output = [kept] if kept else []
        self._trim_text_spans_to_line_count(self._line_count())
        return current

    def _split_html_tags(self, text: str) -> list[str]:
        parts: list[str] = []
        start = 0
        i = 0
        while i < len(text):
            if text[i] != "<":
                i += 1
                continue
            if i > start:
                parts.append(text[start:i])
            j = i + 1
            quote = ""
            while j < len(text):
                ch = text[j]
                if quote:
                    if ch == quote:
                        quote = ""
                elif ch in {"'", '"'}:
                    quote = ch
                elif ch == ">":
                    j += 1
                    break
                j += 1
            else:
                j = len(text)
            parts.append(text[i:j])
            start = j
            i = j
        if start < len(text):
            parts.append(text[start:])
        return [part for part in parts if part != ""]

    def _exec_html_tagsplit(self, rest: str) -> None:
        args = split_era_args(rest)
        try:
            source = to_str(eval_expr(self, args[0], default=self.render_form(args[0]))) if args else ""
            parts = self._split_html_tags(source)
            count = len(parts)
        except Exception:
            parts = []
            count = -1

        if len(args) >= 2 and args[1].strip():
            try:
                ref = parse_lvalue(self, args[1])
                self.memory.set_var(ref.base, ref.indices, count)
            except Exception:
                self.memory.set_var("RESULT", [], count)
        else:
            self.memory.set_var("RESULT", [], count)

        if len(args) >= 3 and args[2].strip():
            try:
                ref = parse_lvalue(self, args[2])
                prefix = list(ref.indices)
                start_index = 0
                if prefix and isinstance(prefix[-1], int):
                    start_index = int(prefix.pop())
                for offset, value in enumerate(parts):
                    self.memory.set_var(ref.base, [*prefix, start_index + offset], value)
                return
            except Exception:
                pass
        for offset, value in enumerate(parts):
            self.memory.set_var("RESULTS", [offset], value)
        if parts:
            self.memory.set_var("RESULTS", [], parts[0])

    def _exec_debug_print(self, key: str, rest: str) -> None:
        """Handle Emuera DEBUGPRINT* commands outside visible game output.

        eraMegaten's distributed `emuera.config` has
        `デバッグコマンドを使用する:NO`, so these lines are normally recognized
        no-ops.  If a probe/test enables the option, keep the rendered debug
        text in a side transcript instead of harvesting buttons or mutating the
        visible terminal transcript.
        """
        if not self._debug_print_enabled():
            return
        print_key = "PRINT" + key[len("DEBUGPRINT") :]
        kana_print = self._is_printk_key(print_key)
        if kana_print:
            print_key = self._strip_printk_key(print_key)
        newline = (
            print_key.endswith(("L", "W", "LC", "WC"))
            or "FORML" in print_key
            or "FORMW" in print_key
            or print_key in {"PRINTL", "PRINTW"}
        )
        if print_key.startswith("PRINTV"):
            text = to_str(eval_expr(self, rest, default="")) if rest else ""
        elif print_key.startswith("PRINTS"):
            text = self._eval_prints_text(rest)
        elif "FORM" in print_key:
            text = self.render_form(rest)
        elif rest.startswith('"') or rest.startswith('@"'):
            text = to_str(eval_expr(self, rest, default=""))
        elif "%" in rest or "\\@" in rest:
            text = self._render_plain_print_form(rest)
        else:
            text = rest
        if kana_print:
            text = self._apply_force_kana(text)
        self.debug_output.append(text + ("\n" if newline else ""))

    def _debug_print_enabled(self) -> bool:
        raw = _config_raw(self, "デバッグコマンドを使用する").strip()
        return norm_name(raw) in {"YES", "TRUE", "ON", "1"}

    def _write_printc_cell(self, text: str, *, newline: bool, harvest_buttons: bool = True) -> None:
        text = self._printc_cell_text(text)
        self._write(text, newline=False, harvest_buttons=harvest_buttons)
        self.printc_counter += 1
        if newline or (self._printc_per_line() > 0 and self.printc_counter >= self._printc_per_line()):
            self._write("", newline=True)
            self.printc_counter = 0

    def _display_width(self, text: str) -> int:
        lang = (_config_raw(self, "内部で使用する東アジア言語") or "").upper()
        if any(marker in lang for marker in ("CHINESE_HANS", "SIMPLIFIED", "ZH_CN", "ZH-HANS")):
            encoding = "gbk"
        elif any(marker in lang for marker in ("CHINESE_HANT", "TRADITIONAL", "ZH_TW", "ZH-HANT")):
            encoding = "cp950"
        elif any(marker in lang for marker in ("KOREAN", "KO_KR", "KO-KR")):
            encoding = "cp949"
        else:
            encoding = "cp932"
        return len(to_str(text).encode(encoding, errors="replace"))

    def _layout_text_width(self, text: str) -> int:
        """Return the front-end character-cell width for visible text.

        Most legacy unit tests use ASCII-only synthetic games and historically
        treated Python characters as one layout cell.  Real eraMegaten declares
        ``内部で使用する東アジア言語:CHINESE_HANS`` and its Emuera UI measures
        CJK strings by the configured East Asian byte width.  When that locale
        is explicitly configured, use the same display width for page-model
        columns and hit boxes so Chinese/Japanese labels do not collapse to
        half-size clickable regions.
        """

        if (_config_raw(self, "内部で使用する東アジア言語") or "").strip():
            return self._display_width(text)
        return len(to_str(text))

    def _truncate_display_width(self, text: str, width: int) -> str:
        if width <= 0:
            return ""
        out: list[str] = []
        used = 0
        for ch in to_str(text):
            ch_width = self._display_width(ch)
            if used + ch_width > width:
                break
            out.append(ch)
            used += ch_width
        return "".join(out)

    def _printc_cell_text(self, text: str) -> str:
        width = self._printc_width()
        text = to_str(text)
        if width <= 0:
            return text
        text = self._truncate_display_width(text, width)
        return text + (" " * max(0, width - self._display_width(text)))

    def _exec_reuse_last_line(self, rest: str) -> None:
        """Replace the latest rendered terminal line with a message.

        eraMegaten uses REUSELASTLINE mostly for validation/error feedback after
        input helpers.  In a terminal transcript we cannot move the cursor, so
        for the message form the closest replayable behavior is to remove the
        previous line from the buffered output and append the replacement line.
        The bare form is commonly used after CLEARLINE as a cursor hint; keep it
        as a transcript no-op so it does not erase an extra line.
        """
        if not rest:
            return
        self._clear_lines(1)
        self._write(self.render_form(rest), newline=True)

    def _exec_reset_stain(self, rest: str) -> None:
        """Reset all stain slots for one character.

        `RESET_STAIN chara` is an Emuera command used by eraMegaten after
        rest/dungeon-return and in several wash/bath events.  The game stores
        stain flags are character data (`STAIN:chara:part`) in Emuera's
        fixed character variable set.  Older versions of this reimplementation
        also materialized them in the global numeric table, so clear both
        layouts to keep sidecar/back-compat state deterministic.
        """
        chara = to_int(eval_expr(self, rest, default=self.memory.get_var("TARGET", []))) if rest else to_int(self.memory.get_var("TARGET", []))
        if 0 <= chara < len(self.memory.characters):
            self.memory.characters[chara].numeric["STAIN"] = {}
        table = self.memory.numeric.get("STAIN", {})
        for idx in list(table):
            if idx and idx[0] == chara:
                del table[idx]

    def _temporary_delta_chara(self, rest: str) -> int:
        chara = to_int(eval_expr(self, rest, default=self.memory.get_var("TARGET", []))) if rest else to_int(self.memory.get_var("TARGET", []))
        return chara

    def _apply_temporary_deltas(
        self,
        chara: int,
        sources: tuple[tuple[str, int], ...],
        *,
        explicit_chara_prefix: bool,
        implicit_current_character: bool,
    ) -> None:
        for source, sign in sources:
            table = self.memory.numeric.setdefault(source, {})
            for idx, raw_value in list(table.items()):
                if explicit_chara_prefix and len(idx) >= 2 and idx[0] == chara:
                    palam_indices = list(idx[1:])
                elif implicit_current_character and len(idx) == 1:
                    palam_indices = [idx[0]]
                else:
                    continue
                delta = to_int(raw_value)
                if delta:
                    current = to_int(self.memory.get_var("PALAM", [chara] + palam_indices))
                    self.memory.set_var("PALAM", [chara] + palam_indices, max(0, current + sign * delta))
                table[idx] = 0

    def _exec_upcheck(self, rest: str) -> None:
        """Apply global UP/DOWN deltas to one character's PALAM and clear them."""
        self._apply_temporary_deltas(
            self._temporary_delta_chara(rest),
            (("UP", 1), ("DOWN", -1)),
            explicit_chara_prefix=False,
            implicit_current_character=True,
        )

    def _exec_cupcheck(self, rest: str) -> None:
        """Apply per-character CUP/CDOWN deltas to PALAM and clear them.

        Private Emuera adds `CUPCHECK chara` as the CUP/CDOWN counterpart of
        UPCHECK.  eraMegaten stores most CUP deltas as `CUP:chara:palam`, while
        some口上-style snippets use the implicit-current-character form
        `CUP:palam`; when an explicit character is supplied, both forms are
        resolved into that character's PALAM slots.
        """
        self._apply_temporary_deltas(
            self._temporary_delta_chara(rest),
            (("CUP", 1), ("CDOWN", -1)),
            explicit_chara_prefix=True,
            implicit_current_character=True,
        )

    def _eval_prints_text(self, rest: str) -> str:
        s = rest.strip()
        if not s:
            return ""
        if s.startswith('"') or s.startswith('@"'):
            return to_str(eval_expr(self, s, default=""))
        if "%" in s or "{" in s or "\\@" in s:
            return self.render_form(s)
        if (
            self._string_assignment_rhs_must_eval(s)
            or self._string_assignment_rhs_has_expression_syntax(s)
        ):
            return to_str(eval_expr(self, s, default=""))
        return rest

    def _is_print_key(self, key: str) -> bool:
        if key == "HTML_PRINT":
            return True
        if self._is_printk_key(key):
            return True
        if key in {"PRINT_IMG", "PRINT_RECT"}:
            return True
        if not key.startswith("PRINT"):
            return False
        return key.startswith(
            (
                "PRINTFORM",
                "PRINTL",
                "PRINTD",
                "PRINTBUTTON",
                "PRINTDATA",
                "PRINTPLAIN",
                "PRINTS",
                "PRINTSINGLE",
                "PRINTC",
                "PRINTV",
                "PRINTW",
            )
        ) or key in {"PRINT", "PRINTL", "PRINTW"}

    def _is_printk_key(self, key: str) -> bool:
        if not key.startswith("PRINT") or "K" not in key:
            return False
        stripped = self._strip_printk_key(key)
        return stripped != key and stripped.startswith("PRINT")

    def _strip_printk_key(self, key: str) -> str:
        # The private-mod PRINTK family is named by inserting one K into the
        # corresponding PRINT command: PRINTFORMKL -> PRINTFORML,
        # PRINTLCK -> PRINTLC, PRINTDATAKW -> PRINTDATAW, etc.
        if key.startswith("PRINT") and "K" in key:
            return key.replace("K", "", 1)
        return key

    _DEFAULT_COLOR_PRINT_STEMS = (
        # Private Emuera 1728h added D-suffixed PRINT variants whose visible
        # text ignores SETCOLOR and uses the configured default foreground.
        # The D marker is inserted before the optional L/W line/wait suffix.
        "PRINTSINGLEFORMS",
        "PRINTSINGLEFORM",
        "PRINTSINGLEV",
        "PRINTSINGLES",
        "PRINTSINGLE",
        "PRINTFORMLC",
        "PRINTFORMC",
        "PRINTFORMS",
        "PRINTFORM",
        "PRINTDATA",
        "PRINTLC",
        "PRINTC",
        "PRINTV",
        "PRINTS",
        "PRINT",
    )

    def _strip_print_default_color_key(self, key: str) -> str:
        for suffix in ("DL", "DW", "D"):
            if not key.endswith(suffix):
                continue
            stem = key[: -len(suffix)]
            if stem in self._DEFAULT_COLOR_PRINT_STEMS:
                return stem + suffix[1:]
        return key

    def _is_default_color_print_key(self, key: str) -> bool:
        return self._strip_print_default_color_key(key) != key

    def _apply_force_kana(self, text: str) -> str:
        mode = max(0, min(3, to_int(getattr(self, "force_kana_mode", 0))))
        if mode == 0 or not text:
            return text
        out: list[str] = []
        if mode == 1:
            for ch in text:
                code = ord(ch)
                if 0x3041 <= code <= 0x3096:
                    out.append(chr(code + 0x60))
                else:
                    out.append(ch)
            return "".join(out)
        for ch in text:
            source = ch
            if mode == 3 and 0xFF61 <= ord(ch) <= 0xFF9F:
                source = unicodedata.normalize("NFKC", ch)
            converted: list[str] = []
            for c in source:
                code = ord(c)
                if 0x30A1 <= code <= 0x30F6:
                    converted.append(chr(code - 0x60))
                else:
                    converted.append(c)
            out.append("".join(converted))
        return unicodedata.normalize("NFC", "".join(out))

    def _is_printc_key(self, key: str) -> bool:
        return key == "PRINTC" or key.startswith("PRINTFORMC") or key.startswith("PRINTPLAINC") or key.endswith("C") or key.endswith("CD")

    def _printc_width(self) -> int:
        value = call_builtin(self, "GETCONFIG", ["PRINTCの文字数"])
        return max(0, to_int(value if value is not None else 25) or 25)

    def _printc_per_line(self) -> int:
        value = call_builtin(self, "PRINTCPERLINE", [])
        return max(0, to_int(value if value is not None else 3))

    def _exec_color_command(self, key: str, rest: str) -> None:
        if key == "RESETCOLOR":
            self.current_color = self.default_color
            return
        if key == "RESETBGCOLOR":
            self.current_bgcolor = self.default_bgcolor
            return
        if key == "SETCOLORBYNAME":
            # Emuera accepts .NET KnownColor names here as bare words
            # (`SETCOLORBYNAME Gold`).  A bare color name should not be treated
            # as an Era variable lookup, and this command is distinct from
            # eraMegaten's custom COLOR() helper (where "RED" intentionally
            # maps to a darker battle color).
            name = self._eval_color_name_arg(rest)
            self.current_color = color_by_known_name(name, self.default_color)
            return
        if key == "SETBGCOLORBYNAME":
            name = self._eval_color_name_arg(rest)
            self.current_bgcolor = color_by_known_name(name, self.default_bgcolor)
            return
        color = self._eval_color_value(rest, self.current_bgcolor if key == "SETBGCOLOR" else self.current_color)
        if key == "SETBGCOLOR":
            self.current_bgcolor = color
        else:
            self.current_color = color

    def _drawline_fill(self, key: str, rest: str) -> str:
        if key == "DRAWLINE":
            return "─"
        raw = rest.strip()
        if not raw:
            return "─"
        if key == "DRAWLINEFORM" or "%" in raw or "{" in raw or "\\@" in raw:
            text = self.render_form(raw).strip()
        elif (raw.startswith('"') and raw.endswith('"')) or (raw.startswith('@"') and raw.endswith('"')):
            text = to_str(eval_expr(self, raw, default=raw.strip('"'))).strip()
        else:
            # CUSTOMDRAWLINE takes a display string directly.  eraMegaten uses
            # bare separators such as "=" / "-" / "･" throughout ERB, so do
            # not route ordinary bare text through the expression parser.
            text = raw
        return text or "─"

    def _eval_color_name_arg(self, rest: str) -> str:
        raw = rest.strip()
        if not raw:
            return ""
        if (raw.startswith('"') and raw.endswith('"')) or raw.startswith('@"') or raw.startswith("%") or raw.startswith("{"):
            return to_str(eval_expr(self, raw, default=self.render_form(raw).strip().strip('"')))
        return self.render_form(raw).strip().strip('"')

    def _eval_alignment_arg(self, rest: str) -> str:
        raw = rest.strip()
        if not raw:
            return self.current_alignment
        if (raw.startswith('"') and raw.endswith('"')) or raw.startswith('@"') or raw.startswith("%") or raw.startswith("{"):
            value = eval_expr(self, raw, default=self.render_form(raw).strip().strip('"'))
        else:
            value = self.render_form(raw).strip().strip('"')
        return norm_name(to_str(value))

    def _exec_graphics_command(self, key: str, rest: str) -> None:
        parts = split_era_args(rest)
        if key == "GCREATE":
            gid = to_int(eval_expr(self, parts[0], default=0)) if parts else 0
            width = to_int(eval_expr(self, parts[1], default=0)) if len(parts) > 1 else 0
            height = to_int(eval_expr(self, parts[2], default=0)) if len(parts) > 2 else 0
            self._graphics_create(gid, width, height, source="")
            self._set_result(1)
            return
        if key == "GCREATEFROMFILE":
            gid = to_int(eval_expr(self, parts[0], default=0)) if parts else 0
            filename = to_str(eval_expr(self, parts[1], default=self.render_form(parts[1]) if len(parts) > 1 else "")) if len(parts) > 1 else ""
            self._set_result(self._graphics_create_from_file(gid, filename))
            return
        if key == "GDISPOSE":
            gid = to_int(eval_expr(self, parts[0], default=0)) if parts else 0
            self.graphics.pop(gid, None)
            return
        if key == "GCLEAR":
            gid = to_int(eval_expr(self, parts[0], default=0)) if parts else 0
            if gid in self.graphics:
                color = to_int(eval_expr(self, parts[1], default=0)) if len(parts) > 1 else 0
                self.graphics[gid]["clear"] = color
                # GCLEAR resets the current bitmap contents.  Keep the latest
                # clear color for front-ends, but discard stale draw history so
                # replaying composition loops (画像合成.ERB / 画像表示.ERB) leaves
                # the registry representing the current graphic, not every past
                # frame drawn into the same GID.
                self.graphics[gid]["draws"] = []
                self.graphics[gid]["draw_ops"] = []
            return
        if key == "SPRITECREATE":
            name = to_str(eval_expr(self, parts[0], default=self.render_form(parts[0]))) if parts else ""
            gid = to_int(eval_expr(self, parts[1], default=0)) if len(parts) > 1 else 0
            x = to_int(eval_expr(self, parts[2], default=0)) if len(parts) > 2 else 0
            y = to_int(eval_expr(self, parts[3], default=0)) if len(parts) > 3 else 0
            graphic = self.graphics.get(gid, {})
            width = to_int(eval_expr(self, parts[4], default=0)) if len(parts) > 4 else to_int(graphic.get("width", 0))
            height = to_int(eval_expr(self, parts[5], default=0)) if len(parts) > 5 else to_int(graphic.get("height", 0))
            if name:
                self.sprites[norm_name(name)] = {"name": name, "graphic": gid, "x": x, "y": y, "width": width, "height": height}
            return
        if key == "SPRITEDISPOSE":
            name = to_str(eval_expr(self, parts[0], default=self.render_form(parts[0]))) if parts else ""
            self.sprites.pop(norm_name(name), None)
            return
        if key == "GDRAWSPRITE":
            if len(parts) >= 2:
                gid = to_int(eval_expr(self, parts[0], default=0))
                sprite = to_str(eval_expr(self, parts[1], default=self.render_form(parts[1])))
                if gid in self.graphics:
                    info = self._sprite_info(sprite) or {}
                    x = to_int(eval_expr(self, parts[2], default=0)) if len(parts) > 2 else 0
                    y = to_int(eval_expr(self, parts[3], default=0)) if len(parts) > 3 else 0
                    width = to_int(eval_expr(self, parts[4], default=to_int(info.get("width", 0)))) if len(parts) > 4 else to_int(info.get("width", 0))
                    height = to_int(eval_expr(self, parts[5], default=to_int(info.get("height", 0)))) if len(parts) > 5 else to_int(info.get("height", 0))
                    color = eval_expr(self, parts[6], default=0) if len(parts) > 6 else None
                    self.graphics[gid].setdefault("draws", []).append(sprite)
                    self.graphics[gid].setdefault("draw_ops", []).append(
                        {
                            "sprite": sprite,
                            "x": x,
                            "y": y,
                            "width": max(0, width),
                            "height": max(0, height),
                            "color_matrix": color,
                            "color_matrix_arg": parts[6].strip() if len(parts) > 6 else "",
                        }
                    )
            return

    def _set_result(self, value: Value) -> None:
        if isinstance(value, str):
            self.memory.set_var("RESULTS", [], value)
            self.memory.set_var("RESULT", [], to_int(value))
        else:
            self.memory.set_var("RESULT", [], to_int(value))
            self.memory.set_var("RESULTS", [], str(to_int(value)))

    def _graphics_create(self, gid: int, width: int, height: int, *, source: str = "") -> None:
        self.graphics[int(gid)] = {"width": max(0, int(width)), "height": max(0, int(height)), "source": source}

    def _graphics_created(self, gid: int) -> int:
        return 1 if int(gid) in self.graphics else 0

    def _graphics_width(self, gid: int) -> int:
        return to_int(self.graphics.get(int(gid), {}).get("width", 0))

    def _graphics_height(self, gid: int) -> int:
        return to_int(self.graphics.get(int(gid), {}).get("height", 0))

    def _graphics_create_from_file(self, gid: int, filename: str) -> int:
        path = self._resolve_resource_file(filename)
        if path is None:
            return 0
        width, height = self._read_image_size(path)
        self._graphics_create(gid, width, height, source=str(path))
        return 1

    def _graphics_clear_rgba(self, value: int) -> tuple[int, int, int, int]:
        color = max(0, int(value)) & 0xFFFFFFFF
        if color == 0:
            # eraMegaten's image-composition helpers use GCLEAR ...,0x00000000
            # as a transparent canvas clear before layering sprites.
            return (0, 0, 0, 0)
        if color > 0xFFFFFF:
            return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF, (color >> 24) & 0xFF)
        return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF, 0xFF)

    def _load_pillow_image(self, path: Path):
        try:
            from PIL import Image
        except Exception as exc:  # pragma: no cover - depends on optional host package
            raise RuntimeError("Pillow is required for graphic rendering/export") from exc
        return Image.open(path).convert("RGBA")

    def _sprite_image_obj(self, name: str, stack: set[int]):
        info = self._sprite_info(name)
        if not info:
            return None
        source = None
        if "graphic" in info:
            gid = to_int(info.get("graphic", 0))
            if gid in self.graphics:
                source = self._render_graphic_image_obj(gid, stack)
        elif "file" in info:
            path = self._resolve_resource_file(to_str(info.get("file", "")))
            if path is not None:
                source = self._load_pillow_image(path)
        if source is None:
            return None
        x = max(0, to_int(info.get("x", 0)))
        y = max(0, to_int(info.get("y", 0)))
        width = to_int(info.get("width", 0))
        height = to_int(info.get("height", 0))
        if width <= 0:
            width = max(0, source.width - x)
        if height <= 0:
            height = max(0, source.height - y)
        if width <= 0 or height <= 0:
            from PIL import Image
            return Image.new("RGBA", (0, 0), (0, 0, 0, 0))
        return source.crop((x, y, x + width, y + height))

    def _color_matrix_from_arg(self, text: str) -> list[list[int]] | None:
        raw = text.strip()
        if not raw:
            return None
        try:
            ref = parse_lvalue(self, raw)
        except Exception:
            return None
        # GDRAWSPRITE's 7th argument may point at the first element of a 5x5
        # Emuera/System.Drawing ColorMatrix, e.g.
        # ``カ羅摩トリクス:COLORINT:0:0``.  Treat all leading indices as the
        # matrix selector and read the final two axes as row/column.
        prefix = list(ref.indices[:-2]) if len(ref.indices) >= 2 else []
        matrix: list[list[int]] = []
        any_value = False
        for row in range(5):
            values: list[int] = []
            for col in range(5):
                value = to_int(self.memory.get_var(ref.base, [*prefix, row, col]))
                any_value = any_value or value != 0
                values.append(value)
            matrix.append(values)
        return matrix if any_value else None

    def _apply_color_matrix_to_image(self, image, matrix: list[list[int]]):
        """Apply an Emuera ColorMatrix to an RGBA Pillow image.

        eraMegaten stores matrix coefficients scaled by 256 and passes
        ``カ羅摩トリクス:*:0:0`` to GDRAWSPRITE.  This mirrors the
        System.Drawing row-vector convention: old R/G/B/A rows contribute to
        each output channel, and row 4 is the additive translation.
        """

        if not matrix:
            return image
        try:
            from PIL import Image
        except Exception as exc:  # pragma: no cover - depends on optional host package
            raise RuntimeError("Pillow is required for graphic rendering/export") from exc

        def clamp(value: float) -> int:
            return max(0, min(255, int(round(value))))

        out = []
        for r, g, b, a in image.getdata():
            nr = (r * matrix[0][0] + g * matrix[1][0] + b * matrix[2][0] + a * matrix[3][0]) / 256.0 + matrix[4][0]
            ng = (r * matrix[0][1] + g * matrix[1][1] + b * matrix[2][1] + a * matrix[3][1]) / 256.0 + matrix[4][1]
            nb = (r * matrix[0][2] + g * matrix[1][2] + b * matrix[2][2] + a * matrix[3][2]) / 256.0 + matrix[4][2]
            na = (r * matrix[0][3] + g * matrix[1][3] + b * matrix[2][3] + a * matrix[3][3]) / 256.0 + matrix[4][3]
            out.append((clamp(nr), clamp(ng), clamp(nb), clamp(na)))
        result = Image.new("RGBA", image.size, (0, 0, 0, 0))
        result.putdata(out)
        return result

    def _render_graphic_image_obj(self, gid: int, stack: set[int]):
        try:
            from PIL import Image
        except Exception as exc:  # pragma: no cover - depends on optional host package
            raise RuntimeError("Pillow is required for graphic rendering/export") from exc
        gid = int(gid)
        info = self.graphics.get(gid)
        if info is None:
            raise KeyError(f"graphic not created: {gid}")
        if gid in stack:
            return Image.new("RGBA", (max(0, to_int(info.get("width", 0))), max(0, to_int(info.get("height", 0)))), (0, 0, 0, 0))
        stack.add(gid)
        try:
            width = max(0, to_int(info.get("width", 0)))
            height = max(0, to_int(info.get("height", 0)))
            source = to_str(info.get("source", ""))
            source_img = None
            if source:
                source_path = Path(source)
                if source_path.exists():
                    source_img = self._load_pillow_image(source_path)
                    width = width or source_img.width
                    height = height or source_img.height
            if width <= 0 or height <= 0:
                return Image.new("RGBA", (0, 0), (0, 0, 0, 0))
            if "clear" in info:
                canvas = Image.new("RGBA", (width, height), self._graphics_clear_rgba(to_int(info.get("clear", 0))))
            elif source_img is not None:
                canvas = source_img.crop((0, 0, width, height))
                if canvas.size != (width, height):
                    fixed = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                    fixed.alpha_composite(canvas, (0, 0))
                    canvas = fixed
            else:
                canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            for op in info.get("draw_ops", []):
                if not isinstance(op, dict):
                    continue
                sprite_img = self._sprite_image_obj(to_str(op.get("sprite", "")), stack)
                if sprite_img is None:
                    continue
                dst_w = to_int(op.get("width", 0)) or sprite_img.width
                dst_h = to_int(op.get("height", 0)) or sprite_img.height
                if dst_w <= 0 or dst_h <= 0:
                    continue
                if sprite_img.size != (dst_w, dst_h):
                    sprite_img = sprite_img.resize((dst_w, dst_h))
                matrix = self._color_matrix_from_arg(to_str(op.get("color_matrix_arg", "")))
                if matrix is not None:
                    sprite_img = self._apply_color_matrix_to_image(sprite_img, matrix)
                canvas.alpha_composite(sprite_img, (to_int(op.get("x", 0)), to_int(op.get("y", 0))))
            return canvas
        finally:
            stack.discard(gid)

    def render_graphic_image(self, gid: int):
        """Render a runtime graphic registry entry into a Pillow RGBA image.

        This is intentionally an optional bridge for modern GUI/export layers:
        normal terminal replay still only tracks registry state, while callers
        that have Pillow available can materialize GCREATE/GCREATEFROMFILE,
        SPRITECREATE and GDRAWSPRITE output as an actual bitmap.
        """

        return self._render_graphic_image_obj(int(gid), set())

    def export_graphic_png(self, gid: int, path: str | Path) -> None:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        self.render_graphic_image(gid).save(out, "PNG")

    def render_sprite_image(self, name: str):
        """Render a sprite/resource name into a Pillow RGBA image.

        This is the GUI/export counterpart of PRINT_IMG and HTML ``<img
        src=...>``.  It works for both runtime sprites created by SPRITECREATE
        and static resource sprites loaded from ``resources/**/*.csv``.
        """

        image = self._sprite_image_obj(to_str(name), set())
        if image is None:
            raise KeyError(f"sprite not found: {name}")
        return image

    def export_sprite_png(self, name: str, path: str | Path) -> None:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        self.render_sprite_image(name).save(out, "PNG")

    def _page_render_rgba(self, value: Any, default: int) -> tuple[int, int, int, int]:
        color = to_int(value if value is not None and str(value).strip() != "" else default) & 0xFFFFFF
        return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF, 0xFF)

    def _page_render_font(self, line_height: int):
        try:
            from PIL import ImageFont
        except Exception as exc:  # pragma: no cover - depends on optional host package
            raise RuntimeError("Pillow is required for page rendering/export") from exc
        size = max(8, int(line_height) - 4)
        candidates = [
            Path(r"C:\Windows\Fonts\meiryo.ttc"),
            Path(r"C:\Windows\Fonts\msgothic.ttc"),
            Path(r"C:\Windows\Fonts\arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        ]
        for path in candidates:
            try:
                if path.exists():
                    return ImageFont.truetype(str(path), size=size)
            except Exception:
                continue
        return ImageFont.load_default()

    def render_page_image(
        self,
        *,
        char_width: int = 8,
        line_height: int = 20,
        viewport_width: int | None = None,
        html_unit_scale: float = 1.0,
        background: int | None = None,
    ):
        """Render the current page/layout snapshot into a Pillow RGBA image.

        This is a lightweight modern-GUI bridge: it consumes the same
        ``html_layout_model()`` drawables used for hit testing, paints text and
        button/rectangle outlines, and composites resource/runtime sprites for
        ``PRINT_IMG`` and HTML ``<img>`` nodes.  It is not a full browser or
        Emuera window renderer, but it gives front-ends and regression tests a
        concrete bitmap for the currently visible page.
        """

        try:
            from PIL import Image, ImageDraw
        except Exception as exc:  # pragma: no cover - depends on optional host package
            raise RuntimeError("Pillow is required for page rendering/export") from exc

        char_width = max(1, int(char_width))
        line_height = max(1, int(line_height))
        layout = self.html_layout_model(
            char_width=char_width,
            line_height=line_height,
            viewport_width=viewport_width,
            html_unit_scale=html_unit_scale,
        )
        drawables = [dict(item) for item in layout.get("drawables", []) if isinstance(item, dict)]

        def attr_int(item: dict[str, Any], key: str, default: int = 0) -> int:
            try:
                return int(float(str(item.get(key, default)).strip() or default))
            except Exception:
                return default

        min_x = 0
        min_y = 0
        max_x = to_int(layout.get("canvas", {}).get("width", 0))
        max_y = to_int(layout.get("canvas", {}).get("height", 0))
        for item in drawables:
            width = attr_int(item, "width")
            height = attr_int(item, "height")
            if width <= 0 or height <= 0:
                continue
            x = attr_int(item, "x")
            y = attr_int(item, "y")
            extra_x = max(1, line_height // 4) + 2 if (to_int(item.get("font_style", 0)) & 2) else 0
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x + width + extra_x)
            max_y = max(max_y, y + height)
        width = max(1, max_x - min_x)
        height = max(1, max_y - min_y)
        bg = self._page_render_rgba(background if background is not None else self.default_bgcolor, self.default_bgcolor)
        canvas = Image.new("RGBA", (width, height), bg)
        draw = ImageDraw.Draw(canvas)
        font = self._page_render_font(line_height)

        def shifted(item: dict[str, Any]) -> tuple[int, int, int, int]:
            return (
                attr_int(item, "x") - min_x,
                attr_int(item, "y") - min_y,
                max(0, attr_int(item, "width")),
                max(0, attr_int(item, "height")),
            )

        def draw_text(item: dict[str, Any], text: str) -> None:
            if not text:
                return
            x, y, w, h = shifted(item)
            if w <= 0 or h <= 0:
                return
            bgcolor = to_int(item.get("bgcolor", self.default_bgcolor))
            if bgcolor != self.default_bgcolor:
                draw.rectangle((x, y, x + w - 1, y + h - 1), fill=self._page_render_rgba(bgcolor, self.default_bgcolor))
            color = self._page_render_rgba(item.get("color", self.default_color), self.default_color)
            style = to_int(item.get("font_style", 0))
            safe_text = text

            def draw_safe_text(target_draw: Any, pos: tuple[int, int]) -> str:
                nonlocal safe_text
                try:
                    target_draw.text(pos, safe_text, fill=color, font=font)
                except UnicodeEncodeError:
                    safe_text = text.encode("latin-1", "replace").decode("latin-1")
                    target_draw.text(pos, safe_text, fill=color, font=font)
                return safe_text

            if style & 2:
                # Emuera uses .NET FontStyle.Italic bit 2.  When a real italic
                # CJK font is unavailable, render the text to a small layer and
                # shear scanlines deterministically so page snapshots preserve
                # visible italic intent for casino titles and styled HTML runs.
                slant = max(1, h // 4)
                layer = Image.new("RGBA", (w + slant + 2, h), (0, 0, 0, 0))
                layer_draw = ImageDraw.Draw(layer)
                draw_safe_text(layer_draw, (0, 0))
                if style & 1 and w > 1:
                    layer_draw.text((1, 0), safe_text, fill=color, font=font)
                sheared = Image.new("RGBA", layer.size, (0, 0, 0, 0))
                denom = max(1, h - 1)
                for row in range(h):
                    offset = int(round((h - 1 - row) * slant / denom))
                    strip = layer.crop((0, row, layer.width, row + 1))
                    sheared.alpha_composite(strip, (offset, row))
                canvas.alpha_composite(sheared, (x, y))
            else:
                draw_safe_text(draw, (x, y))
            if (style & 1) and not (style & 2) and w > 1:
                # Emuera/.NET FontStyle uses bit 1 for bold.  The lightweight
                # page snapshot renderer may not have a matching bold CJK font
                # on every host, so overdraw by one pixel as a deterministic
                # fallback.
                draw.text((x + 1, y), safe_text, fill=color, font=font)
            if style & 8:
                strike_y = y + min(max(0, h - 1), max(1, h // 2))
                draw.line((x, strike_y, x + w - 1, strike_y), fill=color)
            if style & 4:
                underline_y = y + max(0, h - 2)
                draw.line((x, underline_y, x + w - 1, underline_y), fill=color)

        def paste_sprite(item: dict[str, Any]) -> bool:
            src = to_str(item.get("src", ""))
            if not src:
                return False
            x, y, w, h = shifted(item)
            if w <= 0 or h <= 0:
                return False
            try:
                sprite = self.render_sprite_image(src)
            except Exception:
                return False
            if sprite.size != (w, h):
                sprite = sprite.resize((w, h))
            canvas.alpha_composite(sprite, (x, y))
            return True

        for item in drawables:
            kind = to_str(item.get("type", ""))
            x, y, w, h = shifted(item)
            if w <= 0 or h <= 0:
                continue
            color = self._page_render_rgba(item.get("color", self.default_color), self.default_color)
            if kind in {"image", "print_image"}:
                if not paste_sprite(item):
                    draw.rectangle((x, y, x + w - 1, y + h - 1), outline=color)
                    draw_text({**item, "x": x + min_x + 2, "y": y + min_y + 2, "width": max(0, w - 4), "height": h}, to_str(item.get("src", "")))
                continue
            if kind in {"button", "print_button", "nonbutton", "print_rect"}:
                draw.rectangle((x, y, x + w - 1, y + h - 1), outline=color)
                if kind in {"button", "print_button", "nonbutton"}:
                    draw_text(item, to_str(item.get("label", "")))
                continue
            if kind in {"text", "html_text"}:
                draw_text(item, to_str(item.get("text", "")))
        return canvas

    def export_page_png(
        self,
        path: str | Path,
        *,
        char_width: int = 8,
        line_height: int = 20,
        viewport_width: int | None = None,
        html_unit_scale: float = 1.0,
        background: int | None = None,
    ) -> None:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        self.render_page_image(
            char_width=char_width,
            line_height=line_height,
            viewport_width=viewport_width,
            html_unit_scale=html_unit_scale,
            background=background,
        ).save(out, "PNG")

    def _sprite_info(self, name: str) -> dict[str, Any] | None:
        key = norm_name(name)
        if key in self.sprites:
            return self.sprites[key]
        return self._load_resource_sprites().get(key)

    def _sprite_created(self, name: str) -> int:
        return 1 if self._sprite_info(name) is not None else 0

    def _sprite_width(self, name: str) -> int:
        info = self._sprite_info(name)
        return to_int(info.get("width", 0)) if info else 0

    def _sprite_height(self, name: str) -> int:
        info = self._sprite_info(name)
        return to_int(info.get("height", 0)) if info else 0

    def _load_resource_sprites(self) -> dict[str, dict[str, Any]]:
        if self._resource_sprites is not None:
            return self._resource_sprites
        sprites: dict[str, dict[str, Any]] = {}
        root = self.program.root / "resources"
        if root.exists():
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.lower() != ".csv":
                    continue
                try:
                    lines = read_text_auto(path).splitlines()
                except Exception:
                    continue
                for raw in lines:
                    line = raw.strip()
                    if not line or line.startswith((";", "#")):
                        continue
                    parts = split_era_args(line)
                    if len(parts) < 6:
                        continue
                    name = parts[0].strip().strip('"')
                    if not name:
                        continue
                    try:
                        x = int(parts[2].strip() or 0)
                        y = int(parts[3].strip() or 0)
                        width = int(parts[4].strip() or 0)
                        height = int(parts[5].strip() or 0)
                    except ValueError:
                        continue
                    sprites.setdefault(
                        norm_name(name),
                        {
                            "name": name,
                            "file": parts[1].strip().strip('"'),
                            "x": x,
                            "y": y,
                            "width": max(0, width),
                            "height": max(0, height),
                            "source": str(path),
                        },
                    )
        self._resource_sprites = sprites
        return sprites

    def _resolve_resource_file(self, filename: str) -> Path | None:
        raw = filename.strip().strip('"')
        if not raw:
            return None
        p = Path(raw)
        candidates: list[Path] = []
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append(self.program.root / p)
            candidates.append(self.program.root / "resources" / p)
            if len(p.parts) == 1:
                # Resource CSVs often refer to files by basename while the
                # actual image lives in a subdirectory.
                root = self.program.root / "resources"
                if root.exists():
                    try:
                        found = next(root.rglob(raw), None)
                    except Exception:
                        found = None
                    if found is not None:
                        candidates.append(found)
        for candidate in candidates:
            try:
                if candidate.exists() and candidate.is_file():
                    return candidate
            except OSError:
                continue
        return None

    def _read_image_size(self, path: Path) -> tuple[int, int]:
        try:
            data = path.read_bytes()[:65536]
        except OSError:
            return (0, 0)
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            return struct.unpack(">II", data[16:24])
        if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
            return struct.unpack("<HH", data[6:10])
        if data.startswith(b"BM") and len(data) >= 26:
            width = abs(struct.unpack("<i", data[18:22])[0])
            height = abs(struct.unpack("<i", data[22:26])[0])
            return (width, height)
        if data.startswith(b"\xff\xd8"):
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                i += 2
                if marker in {0xD8, 0xD9}:
                    continue
                if i + 2 > len(data):
                    break
                seg_len = struct.unpack(">H", data[i:i + 2])[0]
                if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and i + 7 <= len(data):
                    height, width = struct.unpack(">HH", data[i + 3:i + 7])
                    return (width, height)
                i += max(2, seg_len)
        return (0, 0)

    def _eval_color_value(self, rest: str, default: int) -> int:
        parts = split_era_args(rest)
        if len(parts) >= 3:
            rgb = [
                max(0, min(255, to_int(eval_expr(self, part, default=0))))
                for part in parts[:3]
            ]
            return (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
        if parts:
            return to_int(eval_expr(self, parts[0], default=default))
        return default

    def _exec_input(self, key: str, rest: str) -> None:
        _, default = self._input_default(key, rest)
        self._record_timed_input_wait(key, rest)
        value = self._input(default)
        if key in {"ONEINPUT", "ONEINPUTS", "TONEINPUT", "TONEINPUTS"} and value:
            value = value[:1]
        if key.endswith("S"):
            self.memory.set_var("RESULTS", [], value)
        else:
            self.memory.set_var("RESULT", [], to_int(value))
            self.memory.set_var("RESULTS", [], value)

    def _input_value_is_numeric(self, value: str) -> tuple[bool, int]:
        try:
            return True, parse_era_int(value.strip())
        except Exception:
            return False, 0

    def _exec_input_any(self) -> None:
        value = self._input("")
        is_numeric, numeric = self._input_value_is_numeric(value)
        if is_numeric:
            self.memory.set_var("RESULT", [], numeric)
            self.memory.set_var("RESULTS", [], "")
        else:
            self.memory.set_var("RESULT", [], 0)
            self.memory.set_var("RESULTS", [], value)

    def _current_button_values(self) -> list[str]:
        return [str(button).strip() for button in self.pending_buttons if str(button).strip() != ""]

    def _next_binput_value(self, default: str) -> str:
        buttons = self._current_button_values()
        if not buttons:
            self._clear_visible_buttons()
            return default
        allowed = set(buttons)
        if self.inputs:
            value = self.inputs.pop(0)
            self._clear_visible_buttons()
            return value if value in allowed else default
        if self.interactive:
            while True:
                value = self._input("")
                if value in allowed:
                    return value
        value = buttons[0]
        self._clear_visible_buttons()
        return value

    def _exec_binput(self, key: str, rest: str) -> None:
        _, default = self._input_default(key, rest)
        value = self._next_binput_value(default)
        if key == "BINPUTS":
            self.memory.set_var("RESULTS", [], value)
            self.memory.set_var("RESULT", [], to_int(value))
        else:
            self.memory.set_var("RESULT", [], to_int(value))
            self.memory.set_var("RESULTS", [], value)

    def _era_quote_string(self, value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    def _flow_input_has_default(self) -> bool:
        return self.flow_input_force_skip != 0 or self.flow_input_default is not None or self.flow_inputs_enabled

    def _flow_input_command(self) -> tuple[str, str]:
        if self.flow_inputs_enabled:
            return "INPUTS", self._era_quote_string(self.flow_inputs_default)
        value = self.flow_input_default
        if value is None:
            value = "0" if self.flow_input_force_skip else ""
        return "INPUT", value

    def _exec_flow_input(self, rest: str) -> None:
        parts = split_era_args(rest)
        if not parts:
            self.flow_input_default = None
            self.flow_input_allow_click = 0
            self.flow_input_allow_skip = 0
            self.flow_input_force_skip = 0
            self.memory.set_var("RESULT", [], 0)
            self.memory.set_var("RESULTS", [], "0")
            return
        self.flow_input_default = str(to_int(eval_expr(self, parts[0], default=0))) if parts[0] != "" else "0"
        self.flow_input_allow_click = to_int(eval_expr(self, parts[1], default=0)) if len(parts) >= 2 and parts[1] != "" else 0
        self.flow_input_allow_skip = to_int(eval_expr(self, parts[2], default=0)) if len(parts) >= 3 and parts[2] != "" else 0
        self.flow_input_force_skip = to_int(eval_expr(self, parts[3], default=0)) if len(parts) >= 4 and parts[3] != "" else 0
        self.memory.set_var("RESULT", [], to_int(self.flow_input_default))
        self.memory.set_var("RESULTS", [], self.flow_input_default)

    def _exec_flow_inputs(self, rest: str) -> None:
        parts = split_era_args(rest)
        enabled = to_int(eval_expr(self, parts[0], default=0)) if parts and parts[0] != "" else 0
        self.flow_inputs_enabled = enabled != 0
        if len(parts) >= 2 and parts[1] != "":
            self.flow_inputs_default = to_str(eval_expr(self, parts[1], default=self.render_form(parts[1]).strip().strip('"')))
        elif not self.flow_inputs_enabled:
            self.flow_inputs_default = ""
        self.memory.set_var("RESULT", [], 1 if self.flow_inputs_enabled else 0)
        self.memory.set_var("RESULTS", [], self.flow_inputs_default if self.flow_inputs_enabled else "0")

    def _input_default(self, key: str, rest: str) -> tuple[bool, str]:
        parts = split_era_args(rest)
        default_expr: str | None = None
        if key in {"INPUT", "INPUTS", "ONEINPUT", "ONEINPUTS", "BINPUT", "BINPUTS"}:
            if parts:
                default_expr = parts[0]
        elif key in {"TINPUT", "TINPUTS", "TONEINPUT", "TONEINPUTS"}:
            if len(parts) >= 2:
                default_expr = parts[1]
        if default_expr is None:
            return False, ""
        if key in {"INPUTS", "ONEINPUTS", "TINPUTS", "TONEINPUTS", "BINPUTS"}:
            value = to_str(eval_expr(self, default_expr, default=self.render_form(default_expr).strip().strip('"')))
        else:
            ivalue = to_int(eval_expr(self, default_expr, default=0))
            if key in {"ONEINPUT", "TONEINPUT"} and ivalue < 0:
                # Emuera 1807+ treats negative default values for one-key
                # numeric input as invalid rather than passing a multi-character
                # "-1" through the one-key result path.
                return False, ""
            value = str(ivalue)
        return True, value

    def _input_would_block(self, key: str, rest: str) -> bool:
        if key in {"BINPUT", "BINPUTS"}:
            has_default, _ = self._input_default(key, rest)
            buttons = self._current_button_values()
            if not buttons:
                return not has_default
            if self.inputs and self.had_explicit_inputs and self.inputs[0] not in set(buttons):
                return True
            return False
        if (
            not self.interactive
            and self.inputs
            and self.pending_buttons
            and self.had_explicit_inputs
            and key in {"INPUT", "ONEINPUT", "INPUTS", "ONEINPUTS", "INPUTANY"}
            and self._next_input_misses_visible_numeric_menu()
        ):
            return True
        has_default, _ = self._input_default(key, rest)
        if has_default and not self.inputs:
            return False
        if (
            not self.interactive
            and not self.inputs
            and not self.had_explicit_inputs
            and self.pending_buttons
            and key in {"INPUT", "ONEINPUT", "INPUTS", "ONEINPUTS", "INPUTANY"}
        ):
            choices = [str(button).strip() for button in self.pending_buttons if str(button).strip() != ""]
            if len(choices) > 1 and not all(re.fullmatch(r"[+-]?\d+", choice) for choice in choices):
                return True
        if self.interactive or self.inputs or self.pending_buttons:
            if not self.interactive and not self.inputs and self.had_explicit_inputs:
                return True
            return False
        return True

    def _next_input_misses_visible_numeric_menu(self) -> bool:
        choices = [str(button).strip() for button in self.pending_buttons if str(button).strip() != ""]
        if not choices:
            return False
        # Non-interactive queued input is normally used to click deterministic
        # rendered menus.  If the script has just printed a small numeric menu
        # and the next queued value is not one of those choices, stop at the
        # prompt instead of consuming the mismatched value and spinning in
        # scripts that validate RESULT without issuing another INPUT.
        numeric_choices = [choice for choice in choices if re.fullmatch(r"[+-]?\d+", choice)]
        if not numeric_choices:
            return False
        if len(set(numeric_choices)) > 32:
            return False
        nxt = str(self.inputs[0]).strip() if self.inputs else ""
        if not re.fullmatch(r"[+-]?\d+", nxt):
            return False
        return nxt not in set(numeric_choices)

    def _exec_data(self, frame: ExecFrame, key: str) -> int:
        signature = (frame.fn.name, str(frame.pc), key)
        paused = self._paused_printdata_wait if (
            self._paused_printdata_wait
            and self._paused_printdata_wait.get("signature") == signature
        ) else None
        if paused is not None:
            next_pc = to_int(paused.get("next_pc", frame.pc + 1))
            if self.interactive or self.inputs:
                self._input("")
                self._paused_printdata_wait = None
                return next_pc
            if not self.interactive and self.had_explicit_inputs:
                self.waiting_for_input = True
                return frame.pc
            self._paused_printdata_wait = None
            return next_pc

        _, rest = self._keyword(frame.fn.lines[frame.pc].text.strip())
        variants: list[list[str]] = []
        pc = frame.pc + 1
        while pc < len(frame.fn.lines):
            line = frame.fn.lines[pc].text.strip()
            k, r = self._keyword(line)
            if k == "ENDDATA":
                break
            if k == "DATA":
                variants.append([r])
            elif k == "DATAFORM":
                variants.append([self.render_form(r)])
            elif k == "DATALIST":
                block: list[str] = []
                pc += 1
                while pc < len(frame.fn.lines):
                    inner = frame.fn.lines[pc].text.strip()
                    ik, ir = self._keyword(inner)
                    if ik in {"ENDLIST", "ENDDATA"}:
                        break
                    if ik == "DATA":
                        block.append(ir)
                    elif ik == "DATAFORM":
                        block.append(self.render_form(ir))
                    pc += 1
                if block:
                    variants.append(block)
                if pc < len(frame.fn.lines) and self._keyword(frame.fn.lines[pc].text.strip())[0] == "ENDDATA":
                    break
            pc += 1
        if variants:
            index = random.randrange(len(variants))
            style_key = self._strip_printk_key(key) if self._is_printk_key(key) else key
            default_color_print = self._is_default_color_print_key(style_key)
            saved_color = self.current_color
            if default_color_print:
                self.current_color = self.default_color
            try:
                for row in variants[index]:
                    if self._is_printk_key(key):
                        row = self._apply_force_kana(row)
                    self._write(row, newline=True)
            finally:
                if default_color_print:
                    self.current_color = saved_color
            if rest:
                try:
                    ref = parse_lvalue(self, rest)
                    self.memory.set_var(ref.base, ref.indices, index)
                except Exception:
                    pass
        next_pc = pc + 1
        if key.endswith("W"):
            if self.interactive or self.inputs:
                self._input("")
            elif not self.interactive and self.had_explicit_inputs:
                self._paused_printdata_wait = {
                    "signature": signature,
                    "next_pc": next_pc,
                }
                self.waiting_for_input = True
                return frame.pc
        return next_pc

    def _exec_bit_command(self, key: str, rest: str) -> None:
        parts = split_era_args(rest)
        if len(parts) < 2:
            return
        ref = parse_lvalue(self, parts[0])
        cur = to_int(self.memory.get_var(ref.base, ref.indices))
        for part in parts[1:]:
            bit = to_int(eval_expr(self, part))
            if bit < 0:
                continue
            if key == "SETBIT":
                cur |= 1 << bit
            elif key == "CLEARBIT":
                cur &= ~(1 << bit)
            else:
                cur ^= 1 << bit
        self.memory.set_var(ref.base, ref.indices, cur)

    def _dynamic_var_name_arg(self, raw: str) -> str:
        text = raw.strip()
        if not text:
            return ""
        if text.startswith('"') or text.startswith('@"') or "%" in text or "{" in text or "\\@" in text:
            return self._eval_stringish_arg(text).strip()
        return self._raw_identifier_text(text).strip()

    def _exec_dynamic_var(self, key: str, rest: str) -> None:
        fr = self.memory.frame
        if fr is None:
            return
        is_string = key == "VARS"
        default: Value = "" if is_string else 0
        name_text = ""
        value: Value | None = None
        dims: tuple[int, ...] = ()
        found = find_assignment(rest)
        if found and found[1] == "=":
            lhs, _op, rhs = found
            name_text = self._dynamic_var_name_arg(lhs)
            value = eval_expr(self, rhs, default=default) if rhs else default
            value = to_str(value) if is_string else to_int(value)
        else:
            parts = split_era_args(rest)
            if not parts:
                return
            name_text = self._dynamic_var_name_arg(parts[0])
            raw_dims: list[int] = []
            for part in parts[1:4]:
                if part.strip():
                    raw_dims.append(max(0, to_int(eval_expr(self, part, default=0))))
            dims = tuple(raw_dims)
        if not name_text:
            return
        key_name = norm_name(name_text)
        fr.dims[key_name] = dims
        if is_string:
            fr.strings[key_name] = {}
            fr.numeric.pop(key_name, None)
        else:
            fr.numeric[key_name] = {}
            fr.strings.pop(key_name, None)
        fr.ref_aliases.pop(key_name, None)
        if value is not None:
            self.memory.set_var(name_text, [], value)

    def _exec_varset(self, rest: str) -> None:
        parts = split_era_args(rest)
        if not parts:
            return
        target_base = parts[0].split(":", 1)[0]
        is_string_target = self.memory.is_string_base(target_base)
        value = eval_expr(self, parts[1], default="" if is_string_target else 0) if len(parts) > 1 else ("" if is_string_target else 0)
        try:
            ref = parse_lvalue(self, parts[0])
        except Exception:
            self.memory.varset(parts[0], value)
            return
        if len(parts) >= 4:
            start = to_int(eval_expr(self, parts[2]))
            end = to_int(eval_expr(self, parts[3]))
            resolved = self._varset_target_indices(ref)
            prefix = resolved[:-1] if resolved else []
            for i in range(start, max(start, end)):
                self.memory.set_var(ref.base, prefix + [i], value)
            return
        if ref.indices or self._is_non_scalar_chara_array(ref.base):
            resolved = self._varset_target_indices(ref)
            prefix = tuple(resolved[:-1])
            start = resolved[-1] if resolved else 0
            axis = self._array_scan_axis(ref.base, prefix)
            dims = self.array_dimensions(ref.base)
            end = dims[axis] if axis < len(dims) else None
            if value not in (0, "") and end is not None:
                for i in range(start, max(start, end)):
                    self.memory.set_var(ref.base, list(prefix) + [i], value)
                return
            touched = False
            scan_pos = len(prefix)
            for idx in self._materialized_indices(ref.base):
                if (
                    len(idx) >= scan_pos + 1
                    and idx[:scan_pos] == prefix
                    and idx[scan_pos] >= start
                    and (end is None or idx[scan_pos] < end)
                ):
                    self.memory.set_var(ref.base, list(idx), value)
                    touched = True
            if not touched:
                self.memory.set_var(ref.base, ref.indices, value)
            return
        self.memory.varset(ref.base, value)

    def _exec_varsetex_values(
        self,
        var_name: Value,
        value: Value = 0,
        set_all_dim: Value = 1,
        start_value: Value | None = None,
        end_value: Value | None = None,
    ) -> int:
        name = self._raw_identifier_text(var_name)
        try:
            ref = parse_lvalue(self, name)
        except Exception:
            self.memory.varset(name, value)
            return 1
        set_all = to_int(set_all_dim) != 0
        resolved = self._varset_target_indices(ref)
        dims = self.array_dimensions(ref.base)
        if not dims:
            self.memory.set_var(ref.base, resolved, value)
            return 1
        # VARSETEX with setAllDim enabled fills every higher-dimensional row
        # while using the rightmost index in varName as the low-dimension start
        # offset.  This mirrors Emuera's documented example
        # VARSETEX "A2D:1:2", v -> all rows, columns 2..end.
        if set_all:
            low_start = resolved[-1] if resolved else 0
            if start_value is not None:
                low_start = max(low_start, to_int(start_value))
            low_end = to_int(end_value) if end_value is not None else dims[-1]
            high_ranges = [range(dim) for dim in dims[:-1]]
            if not high_ranges:
                high_indices = [()]
            else:
                from itertools import product

                high_indices = product(*high_ranges)
            for prefix in high_indices:
                for i in range(max(0, low_start), max(max(0, low_start), min(max(0, low_end), dims[-1]))):
                    self.memory.set_var(ref.base, list(prefix) + [i], value)
            return 1
        prefix = resolved[:-1] if resolved else []
        axis = min(len(prefix), len(dims) - 1)
        start = resolved[-1] if resolved else 0
        if start_value is not None:
            start = max(start, to_int(start_value))
        end = to_int(end_value) if end_value is not None else dims[axis]
        end = min(max(0, end), dims[axis])
        for i in range(max(0, start), max(max(0, start), end)):
            self.memory.set_var(ref.base, list(prefix) + [i], value)
        return 1

    def _is_non_scalar_chara_array(self, base: str) -> bool:
        key = norm_name(base)
        return self.memory.is_chara_base(key) and not self.memory.is_chara_scalar_base(key)

    def _resolve_chara_slot(self, base: str, segment: Value) -> int:
        return self.memory._resolve_chara_slot_segment(norm_name(base), segment)

    def _resolve_chara_index(self, segment: Value) -> int:
        return self.memory._resolve_chara_index_segment(segment)

    def _canonical_array_prefix(self, ref, *, single_index_is_slot: bool = False) -> list[int]:
        key = norm_name(ref.base)
        indices = list(ref.indices)
        if self._is_non_scalar_chara_array(key):
            target = to_int(self.memory.get_var("TARGET", []))
            if not indices:
                # Array commands operate on the current character's per-chara
                # array when the character axis is omitted.
                return [target]
            if len(indices) == 1 and (single_index_is_slot or isinstance(indices[0], str)):
                return [target, self._resolve_chara_slot(key, indices[0])]
            chara = self._resolve_chara_index(indices[0])
            return [chara, *[self._resolve_chara_slot(key, seg) for seg in indices[1:]]]
        if self.memory.is_chara_scalar_base(key):
            return [self._resolve_chara_index(indices[0])] if indices else []
        return self.memory.resolve_indices(key, indices)

    def _varset_target_indices(self, ref) -> list[int]:
        # VARSET treats its lvalue as "start element of the affected range".
        # For non-scalar character arrays, a single omitted-character index
        # (VARSET CFLAG:友好度, 0) denotes TARGET's slot, not character 友好度.
        if self._is_non_scalar_chara_array(ref.base):
            indices = list(ref.indices)
            if not indices:
                return [to_int(self.memory.get_var("TARGET", [])), 0]
            if len(indices) == 1:
                return [
                    to_int(self.memory.get_var("TARGET", [])),
                    self._resolve_chara_slot(ref.base, indices[0]),
                ]
        return self._canonical_array_prefix(ref)

    def _array_scan_axis(self, base: str, canonical_prefix: tuple[int, ...] | list[int]) -> int:
        axis = len(canonical_prefix)
        if self._is_non_scalar_chara_array(base) and axis:
            # VariableSize.csv dimensions for character arrays describe the
            # per-character payload only; the leading character position is not
            # part of that dimension tuple.
            axis -= 1
        return max(0, axis)

    def _materialized_indices(self, base: str) -> list[tuple[int, ...]]:
        key = norm_name(base)
        fr = self.memory.frame
        if fr and key in fr.ref_aliases:
            alias = fr.ref_aliases[key]
            out: set[tuple[int, ...]] = set()
            for source_idx in self.memory._materialized_ref_alias_source_indices(alias):
                local = self._ref_alias_local_index_from_source(alias, source_idx)
                if local is not None:
                    out.add(local)
            return sorted(out)
        out: set[tuple[int, ...]] = set()
        if fr:
            out.update(fr.numeric.get(key, {}).keys())
            out.update(fr.strings.get(key, {}).keys())
        out.update(self.memory.numeric.get(key, {}).keys())
        out.update(self.memory.strings.get(key, {}).keys())
        for ci, ch in enumerate(self.memory.characters):
            for rest in ch.numeric.get(key, {}).keys():
                out.add((ci, *rest))
            for rest in ch.strings.get(key, {}).keys():
                out.add((ci, *rest))
        return sorted(out)

    def _ref_alias_local_index_from_source(self, alias, source_idx: tuple[int, ...]) -> tuple[int, ...] | None:
        prefix = alias.prefix
        if not prefix:
            return source_idx
        if alias.offset_last:
            scan_pos = len(prefix) - 1
            if (
                len(source_idx) >= len(prefix)
                and source_idx[:scan_pos] == prefix[:scan_pos]
                and source_idx[scan_pos] >= prefix[-1]
            ):
                return (source_idx[scan_pos] - prefix[-1], *source_idx[scan_pos + 1 :])
            return None
        if len(source_idx) >= len(prefix) and source_idx[: len(prefix)] == prefix:
            return source_idx[len(prefix) :]
        return None

    def _exec_array_command(self, key: str, rest: str) -> None:
        parts = split_era_args(rest)
        if not parts:
            return
        ref = parse_lvalue(self, parts[0])
        if key == "ARRAYREMOVE":
            start = to_int(eval_expr(self, parts[1])) if len(parts) >= 2 else 0
            count = max(0, to_int(eval_expr(self, parts[2]))) if len(parts) >= 3 and parts[2] else 1
            self._array_remove(ref, start, count)
            return
        if key == "ARRAYSHIFT":
            shift = to_int(eval_expr(self, parts[1])) if len(parts) >= 2 else 1
            value = eval_expr(self, parts[2], default="" if self.memory.is_string_base(ref.base) else 0) if len(parts) >= 3 else 0
            start = to_int(eval_expr(self, parts[3])) if len(parts) >= 4 and parts[3] else 0
            count = to_int(eval_expr(self, parts[4])) if len(parts) >= 5 and parts[4] else self._array_default_count(ref, start)
            self._array_shift(ref, shift, value, start, count)
            return
        if key == "ARRAYSORT":
            direction = norm_name(parts[1]) if len(parts) >= 2 else "FORWARD"
            start = to_int(eval_expr(self, parts[2])) if len(parts) >= 3 and parts[2] else 0
            count = to_int(eval_expr(self, parts[3])) if len(parts) >= 4 and parts[3] else self._array_default_count(ref, start)
            self._array_sort([ref], direction, start, count)
            return
        if key == "ARRAYMSORT":
            refs = [parse_lvalue(self, p) for p in parts if p.strip()]
            if refs:
                result = self._array_msort_apply(refs[0], refs, ascending=True, count=self._array_msort_count(refs[0], None))
                self.memory.set_var("RESULT", [], result)
                self.memory.set_var("RESULTS", [], str(result))

    def _exec_arraymsortex(self, rest: str) -> None:
        parts = split_era_args(rest)
        if len(parts) < 2:
            self.memory.set_var("RESULT", [], 0)
            self.memory.set_var("RESULTS", [], "0")
            return
        sort_ascending = eval_expr(self, parts[2], default=1) if len(parts) >= 3 and parts[2] != "" else 1
        size = to_int(eval_expr(self, parts[3], default=0)) if len(parts) >= 4 and parts[3] != "" else None
        value = self._exec_arraymsortex_values(parts[0], parts[1], sort_ascending, size)
        self.memory.set_var("RESULT", [], value)
        self.memory.set_var("RESULTS", [], str(value))

    def _ref_from_array_arg(self, value: Any):
        if isinstance(value, RefArg):
            return value
        text = self._raw_identifier_text(value)
        if not text:
            return None
        try:
            return parse_lvalue(self, text)
        except Exception:
            return None

    def _array_name_list(self, list_ref) -> list[str]:
        count = self._array_default_count(list_ref, 0)
        dims = self.array_dimensions(list_ref.base)
        prefix = self._array_prefix(list_ref)
        axis = self._array_scan_axis(list_ref.base, prefix)
        if dims and dims != (100000,) and axis < len(dims):
            count = max(count, dims[axis])
        names: list[str] = []
        for i in range(max(0, count)):
            name = to_str(self.memory.get_var(list_ref.base, prefix + [i])).strip()
            if not name:
                break
            names.append(name)
        return names

    def _exec_arraymsortex_values(
        self,
        index_arg: Any,
        array_names_arg: Any,
        sort_ascending: Any = 1,
        size: Any | None = None,
    ) -> int:
        index_ref = self._ref_from_array_arg(index_arg)
        list_ref = self._ref_from_array_arg(array_names_arg)
        if index_ref is None or list_ref is None:
            return 0
        refs = [ref for name in self._array_name_list(list_ref) if (ref := self._ref_from_array_arg(name)) is not None]
        if not refs:
            return 0
        count = self._array_msort_count(index_ref, size)
        return self._array_msort_apply(index_ref, refs, ascending=truth(sort_ascending), count=count)

    def _exec_arraycopy(self, rest: str) -> None:
        parts = split_era_args(rest)
        if len(parts) < 2:
            return
        src_name = to_str(eval_expr(self, parts[0], default=parts[0].strip().strip('"')))
        dst_name = to_str(eval_expr(self, parts[1], default=parts[1].strip().strip('"')))
        try:
            src = parse_lvalue(self, src_name)
        except Exception:
            src = parse_lvalue(self, parts[0].strip().strip('"'))
        try:
            dst = parse_lvalue(self, dst_name)
        except Exception:
            dst = parse_lvalue(self, parts[1].strip().strip('"'))
        src_prefix = tuple(self._canonical_array_prefix(src))
        dst_prefix = self._canonical_array_prefix(dst)
        copied = 0
        for idx in self._materialized_indices(src.base):
            if len(idx) >= len(src_prefix) and idx[: len(src_prefix)] == src_prefix:
                suffix = list(idx[len(src_prefix) :])
                value = self.memory.get_var(src.base, list(idx))
                self.memory.set_var(dst.base, dst_prefix + suffix, value)
                copied += 1
        self.memory.set_var("RESULT", [], copied)
        self.memory.set_var("RESULTS", [], str(copied))

    def _array_prefix(self, ref) -> list[int]:
        return self._canonical_array_prefix(ref)

    def _array_get(self, ref, i: int) -> Value:
        return self.memory.get_var(ref.base, self._array_prefix(ref) + [i])

    def _array_set(self, ref, i: int, value: Value) -> None:
        self.memory.set_var(ref.base, self._array_prefix(ref) + [i], value)

    def _array_default_count(self, ref, start: int) -> int:
        prefix = tuple(self._array_prefix(ref))
        max_i = start
        for idx in self._materialized_indices(ref.base):
            if len(idx) >= len(prefix) + 1 and idx[: len(prefix)] == prefix:
                max_i = max(max_i, idx[len(prefix)])
        return max(0, max_i - start + 1)

    def _array_remove(self, ref, start: int, count: int) -> None:
        if count <= 0:
            return
        total = self._array_default_count(ref, start)
        fill: Value = "" if self.memory.is_string_base(ref.base) else 0
        for i in range(start, start + max(0, total - count)):
            self._array_set(ref, i, self._array_get(ref, i + count))
        for i in range(start + max(0, total - count), start + total):
            self._array_set(ref, i, fill)

    def _array_shift(self, ref, shift: int, value: Value, start: int, count: int) -> None:
        if shift == 0 or count <= 0:
            return
        if shift > 0:
            for i in range(start + count - 1, start - 1, -1):
                self._array_set(ref, i + shift, self._array_get(ref, i))
            for i in range(start, start + shift):
                self._array_set(ref, i, value)
        else:
            n = -shift
            for i in range(start, start + max(0, count - n)):
                self._array_set(ref, i, self._array_get(ref, i + n))
            fill: Value = "" if self.memory.is_string_base(ref.base) else 0
            for i in range(start + max(0, count - n), start + count):
                self._array_set(ref, i, fill)

    def _array_sort(self, refs: list[Any], direction: str, start: int, count: int) -> None:
        if count <= 1:
            return
        reverse = direction.startswith("BACK") or direction.startswith("DESC")
        rows = []
        for i in range(start, start + count):
            rows.append((self._array_get(refs[0], i), [self._array_get(r, i) for r in refs]))
        rows.sort(key=lambda row: to_str(row[0]) if isinstance(row[0], str) else to_int(row[0]), reverse=reverse)
        for off, (_, values) in enumerate(rows):
            for ref, value in zip(refs, values):
                self._array_set(ref, start + off, value)

    def _array_msort_count(self, index_ref, size: Any | None) -> int:
        if size is not None:
            return max(0, to_int(size))
        count = self._array_default_count(index_ref, 0)
        dims = self.array_dimensions(index_ref.base)
        prefix = self._array_prefix(index_ref)
        axis = self._array_scan_axis(index_ref.base, prefix)
        if dims and dims != (100000,) and axis < len(dims):
            count = max(count, dims[axis])
        for i in range(max(0, count)):
            value = self._array_get(index_ref, i)
            if isinstance(value, str):
                if value == "":
                    return i
            elif to_int(value) == 0:
                return i
        return max(0, count)

    def _array_row_suffixes(self, ref, count: int) -> list[tuple[int, ...]]:
        prefix = tuple(self._array_prefix(ref))
        suffixes: set[tuple[int, ...]] = set()
        for idx in self._materialized_indices(ref.base):
            if len(idx) >= len(prefix) + 1 and idx[: len(prefix)] == prefix:
                row = idx[len(prefix)]
                if 0 <= row < count:
                    suffixes.add(idx[len(prefix) + 1 :])
        if not suffixes:
            suffixes.add(())
        return sorted(suffixes)

    def _array_row_get(self, ref, row: int, suffix: tuple[int, ...]) -> Value:
        return self.memory.get_var(ref.base, self._array_prefix(ref) + [row, *suffix])

    def _array_row_set(self, ref, row: int, suffix: tuple[int, ...], value: Value) -> None:
        self.memory.set_var(ref.base, self._array_prefix(ref) + [row, *suffix], value)

    def _array_msort_apply(self, index_ref, refs: list[Any], *, ascending: bool, count: int) -> int:
        if count <= 0:
            return 1
        keys = [self._array_get(index_ref, i) for i in range(count)]
        order = sorted(
            range(count),
            key=lambda i: to_str(keys[i]) if isinstance(keys[i], str) else to_int(keys[i]),
            reverse=not ascending,
        )
        row_snapshots: list[tuple[Any, list[tuple[int, ...]], list[dict[tuple[int, ...], Value]]]] = []
        for ref in refs:
            suffixes = self._array_row_suffixes(ref, count)
            rows = [
                {suffix: self._array_row_get(ref, i, suffix) for suffix in suffixes}
                for i in range(count)
            ]
            row_snapshots.append((ref, suffixes, rows))
        for new_i, old_i in enumerate(order):
            for ref, suffixes, rows in row_snapshots:
                for suffix in suffixes:
                    self._array_row_set(ref, new_i, suffix, rows[old_i][suffix])
        return 1

    def _exec_swap(self, rest: str) -> None:
        parts = split_era_args(rest)
        if len(parts) < 2:
            return
        a = parse_lvalue(self, parts[0])
        b = parse_lvalue(self, parts[1])
        av = self.memory.get_var(a.base, a.indices)
        bv = self.memory.get_var(b.base, b.indices)
        self.memory.set_var(a.base, a.indices, bv)
        self.memory.set_var(b.base, b.indices, av)

    def _exec_cvarset(self, rest: str) -> None:
        parts = split_era_args(rest)
        if len(parts) < 3:
            return
        base = parts[0].strip()
        index = eval_expr(self, parts[1])
        value = eval_expr(self, parts[2], default="" if self.memory.is_string_base(base) else 0)
        decl = self.program.var_decls.get(norm_name(base))
        if norm_name(base) in {"BASE", "MAXBASE", "ABL", "TALENT", "EXP", "EX", "MARK", "PALAM", "JUEL", "CFLAG", "CDFLAG", "EQUIP", "TEQUIP", "NO", "CSTR", "NAME", "CALLNAME", "NICKNAME"} or (decl and decl.charadata):
            for chara in range(len(self.memory.characters)):
                self.memory.set_var(base, [chara, index], value)
        else:
            self.memory.set_var(base, [index], value)

    def _exec_sortchara(self, rest: str) -> None:
        parts = split_era_args(rest)
        if not parts or len(self.memory.characters) <= 1:
            return
        ref = parse_lvalue(self, parts[0])
        base = norm_name(ref.base)
        reverse = any(norm_name(p) in {"BACK", "DESC", "DESCENDING", "REVERSE"} for p in parts[1:])

        # SORTCHARA's lvalue omits the leading character index: BASE:LV means
        # "for each character, compare BASE:<character>:LV".  NO:U is used by
        # eraMegaten as "sort by character number"; the U placeholder is not a
        # CSV data index and should not be resolved as one.
        if base == "NO":
            idx_tail: list[Value] = []
        else:
            idx_tail = ref.indices

        def value_for(old_index: int) -> Value:
            if base == "NO":
                ch = self.memory.characters[old_index]
                return ch.numeric.get("NO", {}).get((), ch.template_no)
            if base in CHARA_STRING_ARRAYS:
                return self.memory.get_var(ref.base, [old_index, *idx_tail])
            if base in CHARA_NUMERIC_ARRAYS:
                return self.memory.get_var(ref.base, [old_index, *idx_tail])
            decl = self.program.var_decls.get(base)
            if decl and decl.charadata:
                return self.memory.get_var(ref.base, [old_index, *idx_tail])
            return self.memory.get_var(ref.base, idx_tail)

        def sort_key(pair: tuple[int, Any]) -> tuple[int, Value]:
            value = value_for(pair[0])
            if isinstance(value, str) or self.memory.is_string_base(base):
                return (1, to_str(value))
            return (0, to_int(value))

        pairs = list(enumerate(self.memory.characters))
        pairs.sort(key=sort_key, reverse=reverse)
        old_to_new = {old: new for new, (old, _) in enumerate(pairs)}
        self.memory.characters = [ch for _, ch in pairs]
        self.memory.remap_character_index_vars(old_to_new, old_len=len(old_to_new))

    def _remap_character_index_vars(self, old_to_new: dict[int, int]) -> None:
        self.memory.remap_character_index_vars(old_to_new)

    def _state_path(self, slot: int | None = None) -> Path:
        if slot is None:
            return self.state_dir / "global.engine.json"
        return self.state_dir / f"save{slot:03d}.engine.json"

    def _native_save_path(self, slot: int) -> Path:
        name = f"save{slot:02d}.sav" if 0 <= slot < 100 else f"save{slot}.sav"
        return self.program.root / name

    def _native_global_path(self) -> Path:
        return self.program.root / "global.sav"

    def _snapshot_globals(self) -> dict[str, Any]:
        return self.memory.to_global_json_obj()

    def _restore_globals(self, snapshot: dict[str, Any]) -> None:
        if snapshot:
            self.memory.apply_json_obj(snapshot, overlay=True)

    def _begin_load_data_flow(self) -> None:
        # Emuera treats LOADDATA as a flow-control command: after the data is
        # loaded, the current script stack is discarded, SYSTEM_LOADEND is
        # called if present, then all EVENTLOAD handlers run, and a normal load
        # falls through to the shop screen unless one of those handlers BEGINs a
        # different state.  Push the continuation first so the stack top keeps
        # the same execution order.
        self.stack.clear()
        self.memory.frames.clear()
        if not self._push_shop_loop():
            self._push_call("SHOW_SHOP", [], try_only=True)
        self._push_call_sequence("EVENTLOAD", [], try_only=True)
        self._push_call("SYSTEM_LOADEND", [], try_only=True)

    def _exec_chkdata(self, rest: str) -> None:
        slot = to_int(eval_expr(self, rest, default=0)) if rest else 0
        exists, text = self._save_slot_info(slot)
        # Emuera's CHKDATA convention is inverted from Python truthiness:
        # RESULT == 0 means a save exists and RESULTS contains its caption;
        # non-zero means no usable data.
        self.memory.set_var("RESULT", [], 0 if exists else 1)
        self.memory.set_var("RESULTS", [], text if exists else "")

    def _save_slot_info(self, slot: int) -> tuple[bool, str]:
        sidecar = self._state_path(slot)
        if sidecar.exists():
            return True, self._sidecar_save_text(sidecar)
        native = self._native_save_path(slot)
        if native.exists():
            return True, self._native_save_text(native)
        return False, ""

    def _sidecar_save_text(self, path: Path) -> str:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        meta = data.get("_meta") if isinstance(data, dict) else None
        if isinstance(meta, dict):
            return to_str(meta.get("text", ""))
        return ""

    def _native_save_text(self, path: Path) -> str:
        try:
            data = path.read_bytes()
        except Exception:
            return ""
        if not data.startswith(_NATIVE_SAVE_MAGIC):
            return self._legacy_text_save_text(data)
        if len(data) <= 0x22:
            return ""
        # Emuera save files used by eraMegaten store a small binary header:
        # magic, version/code fields, one kind byte at 0x20, then a LEB128 byte
        # length for a UTF-16LE display string.  Decoding just the caption is
        # enough for CHKDATA/TITLE_LOADGAME without mutating the native file.
        pos = 0x21
        length = 0
        shift = 0
        while pos < len(data):
            b = data[pos]
            pos += 1
            length |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
            if shift > 28:
                return ""
        if length <= 0 or pos + length > len(data):
            return ""
        raw = data[pos : pos + length]
        try:
            return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
        except Exception:
            return ""

    def _legacy_text_save_text(self, data: bytes) -> str:
        for enc in ("utf-8-sig", "utf-8", "cp932", "shift_jis", "utf-16"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = data.decode("utf-8", errors="replace")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        for line in lines:
            if re.match(r"^\d{4}/\d{1,2}/\d{1,2}\s+", line):
                return line
        return lines[2] if len(lines) >= 3 else lines[0]

    def _exec_persistence(self, key: str, rest: str) -> bool:
        if key in {"SAVEGLOBAL", "LOADGLOBAL"}:
            path = self._state_path(None)
            if key == "SAVEGLOBAL":
                self.state_dir.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(self.memory.to_global_json_obj(), ensure_ascii=False, indent=2), encoding="utf-8")
            elif path.exists():
                self.memory.load_json(path, overlay=True)
            else:
                native = self._native_global_path()
                if native.exists():
                    try:
                        save = (
                            read_native_save(native, self.program)
                            if is_native_binary_save(native)
                            else read_legacy_text_global_save(native, self.program)
                        )
                        if save.file_type == SaveFileType.GLOBAL:
                            self.memory.apply_json_obj(save.to_json_obj(), overlay=True)
                    except (OSError, SaveFormatError, ValueError) as exc:
                        self.warn(f"native Emuera global loading failed: {native}: {exc}")
            return False

        parts = split_era_args(rest)
        slot = to_int(eval_expr(self, parts[0], default=0)) if parts else 0
        path = self._state_path(slot)
        if key in {"SAVEDATA", "SAVEGAME"}:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            data = self.memory.to_json_obj()
            if len(parts) >= 2:
                data["_meta"] = {"text": to_str(eval_expr(self, parts[1], default=""))}
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self.memory.set_var("RESULT", [], 1)
            return False

        if path.exists():
            globals_snapshot = self._snapshot_globals()
            self.memory.load_json(path)
            self._restore_globals(globals_snapshot)
            self.memory.set_var("LASTLOAD_NO", [], slot)
            self.memory.set_var("RESULT", [], 1)
            self.memory.set_var("RESULTS", [], "1")
            self._begin_load_data_flow()
            return True
        else:
            native = self._native_save_path(slot)
            if native.exists():
                try:
                    save = read_native_save(native, self.program) if is_native_binary_save(native) else read_legacy_text_save(native, self.program)
                    if save.file_type != SaveFileType.NORMAL:
                        raise SaveFormatError(f"not a normal save: type={save.file_type}")
                    globals_snapshot = self._snapshot_globals()
                    self.memory.apply_json_obj(save.to_json_obj())
                    self._restore_globals(globals_snapshot)
                    self.memory.set_var("LASTLOAD_NO", [], slot)
                    self.memory.set_var("LASTLOAD_VERSION", [], save.script_version)
                    self.memory.set_var("LASTLOAD_TEXT", [], save.save_text)
                    self.memory.set_var("RESULT", [], 1)
                    self.memory.set_var("RESULTS", [], "1")
                    self._begin_load_data_flow()
                    return True
                except (OSError, SaveFormatError, ValueError) as exc:
                    self.warn(f"native Emuera save loading failed: {native}: {exc}")
            self.memory.set_var("RESULT", [], 0)
            self.memory.set_var("RESULTS", [], "0")
        return False

    def _exec_deldata(self, rest: str) -> None:
        slot = to_int(eval_expr(self, rest, default=0)) if rest else 0
        path = self._state_path(slot)
        deleted = 0
        if path.exists():
            path.unlink()
            deleted = 1
        self.memory.set_var("RESULT", [], deleted)
        self.memory.set_var("RESULTS", [], str(deleted))

    def _exec_times(self, rest: str) -> None:
        parts = split_era_args(rest)
        if len(parts) < 2:
            return
        ref = parse_lvalue(self, parts[0])
        cur = to_int(self.memory.get_var(ref.base, ref.indices))
        factor = eval_float(self, parts[1])
        self.memory.set_var(ref.base, ref.indices, int(cur * factor))

    def _eval_stringish_arg(self, rest: str, *, default: str = "") -> str:
        raw = rest.strip()
        if not raw:
            return default
        if "%" in raw or "{" in raw or "\\@" in raw:
            return self.render_form(raw).strip(" \t").strip('"')
        return to_str(eval_expr(self, raw, default=raw.strip('"')))

    def _resolve_output_file(self, name: str) -> Path | None:
        rel = name.strip().replace("\\", "/")
        if not rel:
            rel = "emuera.log"
        if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
            return None
        parts = [part for part in rel.split("/") if part not in {"", "."}]
        if any(part == ".." for part in parts):
            return None
        try:
            root = self.program.root.resolve()
            candidate = root.joinpath(*parts).resolve(strict=False)
            if candidate == root or root not in candidate.parents:
                return None
            return candidate
        except Exception:
            return None

    def _exec_outputlog(self, rest: str) -> None:
        path = self._resolve_output_file(self._eval_stringish_arg(rest, default="emuera.log"))
        ok = 0
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("".join(self.output), encoding="utf-16")
                self.output_log_files.append(str(path))
                ok = 1
            except OSError as exc:
                self.warn(f"OUTPUTLOG failed: {exc}")
        self.memory.set_var("RESULT", [], ok)
        self.memory.set_var("RESULTS", [], str(ok))

    def _exec_putform(self, rest: str) -> None:
        text = self.render_form(rest) if rest else ""
        self.save_info_lines.append(text)
        self.memory.set_var("RESULTS", [], text)
        self.memory.set_var("RESULT", [], to_int(text))

    def _exec_dumprand(self) -> None:
        """Save Python's MT random state into Emuera's RANDDATA array."""
        state = random.getstate()
        internal = tuple(state[1])
        for i, value in enumerate(internal):
            self.memory.set_var("RANDDATA", [i], int(value))

    def _exec_initrand(self) -> None:
        """Restore Python's MT random state from RANDDATA when it is valid."""
        values = [to_int(self.memory.get_var("RANDDATA", [i])) for i in range(625)]
        try:
            random.setstate((3, tuple(values), None))
        except Exception:
            # Emuera documents invalid RANDDATA as undefined/broken for RAND.
            # Keep the current generator usable in the terminal replay instead
            # of crashing the engine on an accidental INITRAND-before-DUMPRAND.
            return

    def _exec_encode_to_uni(self, rest: str) -> None:
        s = rest.strip()
        if not s:
            text = ""
        elif "%" in s or "{" in s or "\\@" in s:
            text = self.render_form(s)
        else:
            text = to_str(eval_expr(self, s, default=s.strip('"')))
        self.memory.set_var("RESULT", [], len(text))
        for i, ch in enumerate(text, 1):
            self.memory.set_var("RESULT", [i], ord(ch))

    def _try_incdec_statement(self, text: str) -> bool:
        m = re.match(r"^(.*?)(\+\+|--)\s*$", text)
        if not m:
            return False
        target = m.group(1).strip()
        if not target:
            return False
        ref = parse_lvalue(self, target)
        old = to_int(self.memory.get_var(ref.base, ref.indices))
        self.memory.set_var(ref.base, ref.indices, old + (1 if m.group(2) == "++" else -1))
        return True

    def _exec_replace_command(self, rest: str) -> bool:
        parts = split_era_args(rest)
        if len(parts) < 3:
            return False
        target_text = parts[0].strip()
        try:
            ref = parse_lvalue(self, target_text)
        except Exception:
            ref = None
        if ref is not None:
            source = to_str(self.memory.get_var(ref.base, ref.indices))
        else:
            source = to_str(eval_expr(self, parts[0], default=self.render_form(parts[0]).strip().strip('"')))
        pattern = to_str(eval_expr(self, parts[1], default=self.render_form(parts[1]).strip().strip('"')))
        replacement = to_str(eval_expr(self, parts[2], default=self.render_form(parts[2]).strip().strip('"')))
        value = to_str(call_builtin(self, "REPLACE", [source, pattern, replacement]) or "")
        if ref is not None:
            self.memory.set_var(ref.base, ref.indices, value)
        self.memory.set_var("RESULTS", [], value)
        self.memory.set_var("RESULT", [], to_int(value))
        return True

    def _exec_builtin_command(self, key: str, rest: str) -> bool:
        if key == "REPLACE":
            return self._exec_replace_command(rest)
        if not rest and call_builtin(self, key, []) is None:
            return False
        raw_first = key in {"GETNUM", "SUMARRAY", "SUMCARRAY", "MAXARRAY", "MINARRAY", "MAXCARRAY", "MINCARRAY", "INRANGEARRAY", "INRANGECARRAY", "MATCH", "FINDELEMENT", "FINDLASTELEMENT", "VARSIZE", "ERDNAME", "FINDCHARA", "FINDLASTCHARA", "CMATCH", "ISDEFINED", "EXISTVAR", "EXISTFUNCTION", "GETVAR", "GETVARS", "SETVAR", "VARSETEX", "STRJOIN"} or key.startswith(("ENUMFUNC", "ENUMVAR", "ENUMMACRO"))
        ref_positions = self.ref_arg_positions_for_call(key)
        parts = split_era_args(rest)
        args: list[Value] = []
        for i, part in enumerate(parts):
            if i == 0 and raw_first:
                args.append(part.strip())
            elif i in ref_positions:
                ref_arg = self._make_ref_arg(part)
                args.append(ref_arg if ref_arg is not None else eval_expr(self, part, default="" if part.strip().startswith('"') or part.strip().startswith('@"') else 0))
            elif part == "":
                args.append("")
            elif self._argument_is_bare_form(part):
                args.append(self.render_form(part).strip(" \t"))
            else:
                args.append(eval_expr(self, part, default="" if part.strip().startswith('"') or part.strip().startswith('@"') else 0))
        value = call_builtin(self, key, args)
        if value is None:
            if key in {"CHKDATA", "GETFONT", "GETBGCOLOR", "SPRITECREATED"}:
                value = 0
            else:
                return False
        if key == "VARSIZE" and args:
            dims = self.array_dimensions(self._raw_identifier_text(args[0]))
            table = self.memory.numeric.setdefault("RESULT", {})
            table.clear()
            dimension_arg = None
            if len(args) >= 2 and args[1] != "":
                dimension_arg = to_int(args[1])
            if dimension_arg is None:
                first = dims[0] if dims else to_int(value)
            elif 0 <= dimension_arg < len(dims):
                first = dims[dimension_arg]
            else:
                first = 0
            table[()] = first
            for i, dim in enumerate(dims):
                table[(i,)] = dim
            self.memory.set_var("RESULTS", [], str(first))
            return True
        if key == "ENUMFILES" or key.startswith(("ENUMFUNC", "ENUMVAR", "ENUMMACRO")):
            # ENUM* writes names into RESULTS:0.. as its primary side effect.
            # Do not write the numeric count into RESULTS/RESULTS:0 via the
            # generic command footer, or the first enumerated value would be
            # lost (string arrays alias scalar and :0 in Emuera-style storage).
            self.memory.set_var("RESULT", [], to_int(value))
            return True
        if key == "REGEXPMATCH" and len(args) >= 3:
            ref_output = len(args) >= 4 and isinstance(args[2], RefArg) and isinstance(args[3], RefArg)
            result_output = not isinstance(args[2], RefArg) and truth(args[2])
            if ref_output or result_output:
                self.memory.set_var("RESULT", [], to_int(value))
                return True
        if isinstance(value, str):
            self.memory.set_var("RESULTS", [], value)
            self.memory.set_var("RESULT", [], to_int(value))
        else:
            self.memory.set_var("RESULT", [], to_int(value))
            self.memory.set_var("RESULTS", [], str(to_int(value)))
        return True

    def _raw_identifier_text(self, text: Value) -> str:
        s = to_str(text).strip()
        if s.startswith('@"') and s.endswith('"') and len(s) >= 3:
            return s[2:-1].replace('""', '"')
        if s.startswith('"') and s.endswith('"') and len(s) >= 2:
            return s[1:-1].replace('""', '"')
        return s

    def array_dimensions(self, name: str) -> tuple[int, ...]:
        key = norm_name(self._raw_identifier_text(name))
        if self.memory.frame:
            dims = self.memory.frame.dims.get(key)
            if dims and any(dim > 0 for dim in dims):
                return tuple(max(0, dim) for dim in dims)
            table = self.memory.frame.strings.get(key) or self.memory.frame.numeric.get(key)
            inferred = self._infer_table_dimensions(table)
            if inferred:
                return inferred
        decl = self.program.var_decls.get(key)
        if decl and decl.dims:
            return tuple(max(0, dim) for dim in decl.dims)
        if self.program.csv and key in self.program.csv.variable_sizes:
            raw_dims = self.program.csv.variable_sizes[key]
            if isinstance(raw_dims, (tuple, list)):
                return tuple(max(0, int(dim)) for dim in raw_dims)
            return (max(0, int(raw_dims)),)
        table = self.memory.strings.get(key) or self.memory.numeric.get(key)
        inferred = self._infer_table_dimensions(table)
        if inferred:
            return inferred
        if key in {"LOCAL", "LOCALS", "ARG", "ARGS"}:
            return (1000,)
        if key in {"CHARA", "BASE", "CFLAG", "CSTR"}:
            return (10000,)
        return (100000,)

    def _infer_table_dimensions(self, table: dict[tuple[int, ...], Any] | None) -> tuple[int, ...]:
        if not table:
            return ()
        rank = max((len(idx) for idx in table if idx), default=0)
        if rank <= 0:
            return (len(table),)
        dims: list[int] = []
        for axis in range(rank):
            values = [idx[axis] for idx in table if len(idx) > axis]
            dims.append(max(values) + 1 if values else 0)
        return tuple(dims)

    def array_size(self, name: str) -> int:
        dims = self.array_dimensions(name)
        return dims[0] if dims else 0

    def _exec_split(self, rest: str, *, randomize: bool) -> None:
        parts = split_era_args(rest)
        if len(parts) < 3:
            return
        source = self.render_form(parts[0]) if "\\@" in parts[0] else to_str(eval_expr(self, parts[0], default=""))
        delim = self.render_form(parts[1]) if len(parts) > 1 and "\\@" in parts[1] else to_str(eval_expr(self, parts[1], default="_"))
        values = source.split(delim) if delim else [source]
        if randomize:
            random.shuffle(values)
        dest = parts[2].strip()
        try:
            dest_ref = parse_lvalue(self, dest)
        except Exception:
            dest_ref = None
        for i, value in enumerate(values):
            if dest_ref is not None:
                if i == 0 and not dest_ref.indices:
                    # Emuera treats the scalar form of many result/local arrays
                    # as element 0.  eraMegaten relies on this after
                    # `SPLIT ..., RESULTS` and then reading bare RESULTS.
                    self.memory.set_var(dest_ref.base, [], value)
                self.memory.set_var(dest_ref.base, [*dest_ref.indices, i], value)
            else:
                if i == 0:
                    self.memory.set_var(dest, [], value)
                self.memory.set_var(dest, [i], value)
        count = len(values)
        # Private Emuera 1736a returns the split count in RESULT.  Do not sync
        # RESULTS here: scripts frequently split into RESULTS and expect bare
        # RESULTS to remain the first split element, not the numeric count.
        self.memory.set_var("RESULT", [], count)
        if len(parts) >= 4 and parts[3].strip():
            try:
                count_ref = parse_lvalue(self, parts[3].strip())
                self.memory.set_var(count_ref.base, count_ref.indices, count)
            except Exception:
                self.memory.set_var(parts[3].strip(), [], count)

    def _string_assignment_rhs_must_eval(self, rhs: str) -> bool:
        """Return true when a string assignment RHS is a real expression.

        Era scripts commonly write menu labels and item names as bare text in
        string assignments, e.g. ``RESULTS = 探索``.  eraMegaten also installs
        those same words as CSV/_Rename numeric constants, so a plain
        ``has_symbol`` check would wrongly turn them into numbers.  Only force
        expression evaluation for actual variables/arrays/decls/defines; CSV
        constants alone stay as form/literal text in string-context assignment.
        Numeric expressions, function calls, and quoted strings are already
        excluded by ``should_form_string`` before this helper is consulted.
        """

        s = rhs.strip()
        if not s:
            return False
        if "%" in s or "{" in s or "\\@" in s:
            # Mixed form strings may start with words that are also numeric
            # built-ins (e.g. ``TARGET:[{CPOS(...)}] %CALLNAME...,LEFT%`` in
            # battle messages).  Do not force expression evaluation just
            # because that leading label matches a symbol; only a standalone
            # lvalue-like RHS such as ``CSTR:ARG:0`` must be evaluated.
            try:
                parse_lvalue(self, s)
                return True
            except Exception:
                return False
        first = s.split(":", 1)[0].strip()
        key = norm_name(first)
        full_key = norm_name(s)
        if key in NUMERIC_ARRAYS or key in STRING_ARRAYS or key in CHARA_NUMERIC_ARRAYS or key in CHARA_STRING_ARRAYS:
            return True
        decl = self.program.var_decls.get(key)
        if decl and (decl.global_scope or decl.savedata or decl.module_scope or decl.charadata or decl.const):
            return True
        if key in self.program.defines:
            return True
        if key in self.memory.numeric or key in self.memory.strings:
            return True
        if self.memory.frame and (key in self.memory.frame.numeric or key in self.memory.frame.strings):
            return True
        # A fully-scoped define/global variable name is unusual but cheap to support.
        full_decl = self.program.var_decls.get(full_key)
        if full_decl and (full_decl.global_scope or full_decl.savedata or full_decl.module_scope or full_decl.charadata or full_decl.const):
            return True
        if full_key in self.program.defines:
            return True
        return False

    def _string_assignment_rhs_is_text_label(self, rhs: str) -> bool:
        s = rhs.strip()
        if not s or not ("/" in s or "／" in s):
            return False
        if s.startswith('"') or s.startswith('@"') or "%" in s or "{" in s or "\\@" in s:
            return False
        # Slash is very common in eraMegaten menu labels ("名称/愛称変更").
        # Keep it as division only when the left-hand side of the slash is an
        # obvious numeric expression source, e.g. "LOCAL / 2" or "FLAG:x / 2".
        if any(op in s for op in ["+", "*", "%", "==", "!=", "&&", "||", ">=", "<=", "<<", ">>", "&", "|", "^", "?", "#"]):
            return False
        if "(" in s and ")" in s:
            return False
        parts = [part.strip() for part in re.split(r"[／/]", s) if part.strip()]
        if not parts:
            return False
        all_parts_are_expression_terms = all(
            re.fullmatch(r"[+-]?\d+", part) or self._string_assignment_rhs_must_eval(part)
            for part in parts
        )
        return not all_parts_are_expression_terms

    def _is_string_assignment_target(self, base: str) -> bool:
        key = norm_name(base)
        if self.memory.is_string_base(base):
            return True
        fr = self.memory.frame
        return bool(fr and key in fr.strings)

    def _try_assignment(self, text: str) -> bool:
        found = find_assignment(text)
        if not found:
            return False
        lhs, op, rhs = found
        if op == "'=":
            lhs = lhs.rstrip("'").rstrip()
            op = "="
        lhs = lhs.rstrip(",").rstrip()
        if self._is_print_key(norm_name(lhs)):
            return False
        try:
            ref = parse_lvalue(self, lhs)
        except Exception:
            return False
        is_string_target = self._is_string_assignment_target(ref.base)
        expression_rhs = self._string_assignment_rhs_has_expression_syntax(rhs)
        text_label_rhs = (
            self._string_assignment_rhs_is_text_label(rhs)
            or self._string_assignment_rhs_is_parenthetical_text_label(rhs)
            or self._string_assignment_rhs_is_parenthesized_form_text(rhs)
        )
        literal_string_rhs = (
            is_string_target
            and (
                text_label_rhs
                or (
                    not expression_rhs
                    and should_form_string(rhs)
                    and not self._string_assignment_rhs_must_eval(rhs)
                )
            )
        )
        rhs_parts = split_era_args(rhs)
        if op == "=" and ref.indices and len(rhs_parts) > 1 and not literal_string_rhs:
            resolved = self.memory.resolve_indices(norm_name(ref.base), ref.indices)
            prefix = resolved[:-1]
            start = resolved[-1] if resolved else 0
            for offset, part in enumerate(rhs_parts):
                expr_part = self._prepare_string_expression_rhs(part) if is_string_target else part
                value = eval_expr(self, expr_part, default="" if is_string_target else 0) if part else ("" if is_string_target else 0)
                self.memory.set_var(ref.base, [*prefix, start + offset], value)
            return True
        old = self.memory.get_var(ref.base, ref.indices)
        if literal_string_rhs:
            value: Value = self.render_form(rhs)
        else:
            expr_rhs = self._prepare_string_expression_rhs(rhs) if is_string_target else rhs
            value = eval_expr(self, expr_rhs, default="" if is_string_target else 0) if rhs else ("" if is_string_target else 0)
        if op != "=":
            if op == "+=": value = to_str(old) + to_str(value) if isinstance(old, str) or isinstance(value, str) else to_int(old) + to_int(value)
            elif op == "-=": value = to_int(old) - to_int(value)
            elif op == "*=": value = to_int(old) * to_int(value)
            elif op == "/=": value = 0 if to_int(value) == 0 else int(to_int(old) / to_int(value))
            elif op == "%=": value = 0 if to_int(value) == 0 else to_int(old) % to_int(value)
            elif op == "|=": value = to_int(old) | to_int(value)
            elif op == "&=": value = to_int(old) & to_int(value)
            elif op == "^=": value = to_int(old) ^ to_int(value)
        self.memory.set_var(ref.base, ref.indices, value)
        return True

    def _prepare_string_expression_rhs(self, rhs: str) -> str:
        if "\\@" not in rhs:
            return rhs
        return self._inline_form_conditionals_as_string_literals(rhs)

    def _inline_form_conditionals_as_string_literals(self, text: str) -> str:
        r"""Convert bare form conditionals inside string expressions to literals.

        eraMegaten uses Emuera form conditionals as string fragments inside
        normal string expressions, e.g. ``NAME + \@VAL >= 0 ?+#\@ + TOSTR(VAL)``
        to insert a visible plus sign.  The arithmetic expression parser cannot
        parse the form markers directly, so for string-target assignments we
        evaluate each conditional and quote the chosen branch as a string
        literal while leaving raw/quoted strings untouched.
        """
        out: list[str] = []
        i = 0
        guard = 0
        while i < len(text):
            if text.startswith('@"', i) or text[i] == '"':
                end = self._find_era_string_end(text, i + (1 if text.startswith('@"', i) else 0))
                out.append(text[i:end])
                i = end
                continue
            if not text.startswith("\\@", i):
                out.append(text[i])
                i += 1
                continue
            end = text.find("\\@", i + 2)
            if end == -1:
                out.append(text[i:])
                break
            body = text[i + 2:end]
            q = self.formatter._find_top(body, "?")
            h = self.formatter._find_top(body, "#")
            if q != -1 and h != -1 and q < h:
                cond = body[:q].strip()
                yes = body[q + 1:h]
                no = body[h + 1:]
                chosen = yes if truth(eval_expr(self, cond)) else no
                rendered = self.render_form(chosen) if ("%" in chosen or "{" in chosen or "\\@" in chosen) else chosen
                out.append(self._quote_era_string(rendered))
            else:
                out.append(self._quote_era_string(body))
            i = end + 2
            guard += 1
            if guard > 1000:
                out.append(text[i:])
                break
        return "".join(out)

    def _find_era_string_end(self, text: str, quote_index: int) -> int:
        # quote_index points at the opening quote, including for @"..."
        i = quote_index + 1
        while i < len(text):
            if text[i] == '"':
                if i + 1 < len(text) and text[i + 1] == '"':
                    i += 2
                    continue
                return i + 1
            if text[i] == "\\" and i + 1 < len(text):
                i += 2
                continue
            i += 1
        return len(text)

    def _quote_era_string(self, text: str) -> str:
        return '"' + text.replace('"', '""') + '"'

    def _string_assignment_rhs_has_expression_syntax(self, rhs: str) -> bool:
        s = rhs.strip()
        if not s:
            return False
        if (s.startswith("%") and s.endswith("%")) or (s.startswith("{") and s.endswith("}")):
            return False
        depth = 0
        in_str = False
        i = 0
        while i < len(s):
            ch = s[i]
            if in_str:
                if ch == '"':
                    if i + 1 < len(s) and s[i + 1] == '"':
                        i += 2
                        continue
                    if i == 0 or s[i - 1] != "\\":
                        in_str = False
                i += 1
                continue
            if s.startswith("\\@", i):
                end = s.find("\\@", i + 2)
                i = len(s) if end == -1 else end + 2
                continue
            if s.startswith("[[", i):
                end = s.find("]]", i + 2)
                i = len(s) if end == -1 else end + 2
                continue
            if ch == "%":
                end = self._find_string_assignment_percent_end(s, i + 1)
                if end != -1:
                    i = end + 1
                    continue
            if ch == "{":
                end = self._find_string_assignment_brace_end(s, i)
                if end != -1:
                    i = end + 1
                    continue
            if ch == "<":
                end = self._find_string_assignment_html_tag_end(s, i)
                if end != -1:
                    i = end + 1
                    continue
            if ch == '@' and i + 1 < len(s) and s[i + 1] == '"':
                in_str = True
                i += 2
                continue
            if ch == '"':
                in_str = True
                i += 1
                continue
            if ch in "([{":
                if depth == 0 and ch == "(" and self._string_assignment_paren_is_expression_syntax(s, i):
                    return True
                depth += 1
            elif ch in ")]}" and depth:
                depth -= 1
            elif depth == 0:
                if any(s.startswith(op, i) for op in ["==", "!=", "&&", "||", ">=", "<=", "+", "*", "?", "&", "|", "^"]):
                    return True
                if (
                    (s.startswith("<<", i) or s.startswith(">>", i))
                    and self._string_assignment_shift_is_expression_operator(s, i)
                ):
                    return True
                if ch in "<>" and self._string_assignment_angle_is_expression_operator(s, i):
                    return True
            i += 1
        return False

    def _find_string_assignment_percent_end(self, text: str, start: int) -> int:
        depth = 0
        in_str = False
        i = start
        while i < len(text):
            ch = text[i]
            if in_str:
                if ch == '"':
                    if i + 1 < len(text) and text[i + 1] == '"':
                        i += 2
                        continue
                    if i == 0 or text[i - 1] != "\\":
                        in_str = False
                i += 1
                continue
            if ch == '"':
                in_str = True
            elif ch == "(":
                depth += 1
            elif ch == ")" and depth:
                depth -= 1
            elif ch == "%" and depth == 0:
                return i
            i += 1
        return -1

    def _find_string_assignment_brace_end(self, text: str, open_index: int) -> int:
        depth = 0
        in_str = False
        i = open_index
        while i < len(text):
            ch = text[i]
            if in_str:
                if ch == '"':
                    if i + 1 < len(text) and text[i + 1] == '"':
                        i += 2
                        continue
                    if i == 0 or text[i - 1] != "\\":
                        in_str = False
                i += 1
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return -1

    def _find_string_assignment_html_tag_end(self, text: str, open_index: int) -> int:
        j = open_index - 1
        while j >= 0 and text[j].isspace():
            j -= 1
        # Treat obvious literal tags at the start of a string fragment as text,
        # but keep comparisons such as "A < B" in expression mode.
        if j >= 0 and re.match(r"[\w\u0080-\uffff)\]}\"']", text[j]):
            return -1
        k = open_index + 1
        if k >= len(text):
            return -1
        if text[k] == "/":
            k += 1
        if k >= len(text) or not re.match(r"[A-Za-z!\?]", text[k]):
            return -1
        quote = ""
        i = k + 1
        while i < len(text):
            ch = text[i]
            if quote:
                if ch == quote:
                    quote = ""
                i += 1
                continue
            if ch in {"'", '"'}:
                quote = ch
            elif ch == ">":
                return i
            i += 1
        return -1

    def _string_assignment_angle_is_expression_operator(self, text: str, index: int) -> bool:
        j = index - 1
        while j >= 0 and text[j].isspace():
            j -= 1
        k = index + 1
        while k < len(text) and text[k].isspace():
            k += 1
        if j < 0 or k >= len(text):
            return False
        return bool(re.match(r"[\w\u0080-\uffff)\]}\"']", text[j]) and re.match(r"[\w\u0080-\uffff({\[\"']", text[k]))

    def _string_assignment_shift_is_expression_operator(self, text: str, index: int) -> bool:
        j = index - 1
        while j >= 0 and text[j].isspace():
            j -= 1
        k = index + 2
        while k < len(text) and text[k].isspace():
            k += 1
        if j < 0 or k >= len(text):
            return False
        # Bare text labels in eraMegaten often start with arrows such as
        # ``>>> %RESULTS%``.  Treat ``<<``/``>>`` as expression syntax only
        # when both sides look like actual expression terms; otherwise keep the
        # assignment in form-string/literal mode.
        return bool(re.match(r"[\w\u0080-\uffff)\]}\"']", text[j]) and re.match(r"[\w\u0080-\uffff({\[\"']", text[k]))

    def _string_assignment_paren_is_expression_syntax(self, text: str, open_index: int) -> bool:
        j = open_index - 1
        while j >= 0 and text[j].isspace():
            j -= 1
        if j < 0:
            return True
        prev = text[j]
        if prev in "%}）］」』】":
            return False
        if prev in "+-*/%&|^=<>!,?:#([":
            return True
        return bool(re.match(r"[\w\u0080-\uffff]", prev))

    def _string_assignment_rhs_is_parenthetical_text_label(self, rhs: str) -> bool:
        """Return true for literal labels such as `生命消耗(小)`.

        Many eraMegaten string tables use unquoted display labels containing
        parenthetical qualifiers: skill names, request titles, clothes names,
        enemy aliases, etc.  They look like function calls to the expression
        parser, but when the prefix is not a known callable Emuera keeps them as
        literal/form text in string assignment context.
        """
        s = rhs.strip()
        if not s or s.startswith('"') or s.startswith('@"'):
            return False
        open_index = self._first_top_level_parenthesis(s)
        if open_index is None:
            return False
        close_index = self._matching_parenthesis(s, open_index)
        if close_index is None:
            return False
        prefix = s[:open_index].strip()
        if not prefix:
            return False
        # Do not mistake Emuera's parenthesized dynamic index syntax for a
        # literal display label.  eraMegaten uses this in string assignments
        # such as `GDS:LCOUNT:ID '= TEMPS:(LCOUNT * 2)`, where the RHS must read
        # the local string array rather than preserving the source text.
        try:
            ref = parse_lvalue(self, s)
            key = norm_name(ref.base)
            if ref.indices and (
                key in NUMERIC_ARRAYS
                or key in STRING_ARRAYS
                or key in CHARA_NUMERIC_ARRAYS
                or key in CHARA_STRING_ARRAYS
                or key in self.program.var_decls
                or key in self.program.defines
                or key in self.memory.numeric
                or key in self.memory.strings
                or (
                    self.memory.frame is not None
                    and (key in self.memory.frame.numeric or key in self.memory.frame.strings)
                )
            ):
                return False
        except Exception:
            pass
        # A real function call should still evaluate.
        candidate = prefix.rsplit(None, 1)[-1].strip()
        if candidate and self.has_callable(candidate):
            return False
        return not self._rhs_has_top_level_operator_before(s, open_index)

    def _string_assignment_rhs_is_parenthesized_form_text(self, rhs: str) -> bool:
        """Return true for form text wrapped in literal parentheses.

        Real eraMegaten status scripts contain string assignments such as
        ``LOCALS = ({LOCAL:3}/12)``.  The outer parentheses are display text,
        while ``{LOCAL:3}`` is a form-string interpolation and ``/12`` is a
        literal suffix.  Without this special case the string-target assignment
        classifier treats the leading ``(`` as arithmetic grouping and sends
        the whole RHS to the expression parser, which cannot parse the form
        braces.
        """
        s = rhs.strip()
        if not s or s.startswith('"') or s.startswith('@"') or not s.startswith("("):
            return False
        close_index = self._matching_parenthesis(s, 0)
        if close_index != len(s) - 1:
            return False
        return self._rhs_has_unquoted_form_marker(s[1:-1])

    def _rhs_has_unquoted_form_marker(self, text: str) -> bool:
        in_str = False
        i = 0
        while i < len(text):
            ch = text[i]
            if in_str:
                if ch == '"':
                    if i + 1 < len(text) and text[i + 1] == '"':
                        i += 2
                        continue
                    if i == 0 or text[i - 1] != "\\":
                        in_str = False
                i += 1
                continue
            if text.startswith('@"', i):
                in_str = True
                i += 2
                continue
            if ch == '"':
                in_str = True
                i += 1
                continue
            if text.startswith("[[", i):
                end = text.find("]]", i + 2)
                i = len(text) if end == -1 else end + 2
                continue
            if text.startswith("\\@", i):
                return True
            if ch == "{":
                end = self._find_string_assignment_brace_end(text, i)
                if end != -1:
                    return True
            if ch == "%":
                end = self._find_string_assignment_percent_end(text, i + 1)
                if end != -1:
                    return True
            i += 1
        return False

    def _first_top_level_parenthesis(self, text: str) -> int | None:
        depth = 0
        in_str = False
        i = 0
        while i < len(text):
            ch = text[i]
            if in_str:
                if ch == '"':
                    if i + 1 < len(text) and text[i + 1] == '"':
                        i += 2
                        continue
                    if i == 0 or text[i - 1] != "\\":
                        in_str = False
                i += 1
                continue
            if text.startswith('@"', i):
                in_str = True
                i += 2
                continue
            if ch == '"':
                in_str = True
                i += 1
                continue
            if text.startswith("\\@", i):
                end = text.find("\\@", i + 2)
                i = len(text) if end == -1 else end + 2
                continue
            if text.startswith("[[", i):
                end = text.find("]]", i + 2)
                i = len(text) if end == -1 else end + 2
                continue
            if ch == "%":
                end = self._find_string_assignment_percent_end(text, i + 1)
                if end != -1:
                    i = end + 1
                    continue
            if ch == "{":
                end = self._find_string_assignment_brace_end(text, i)
                if end != -1:
                    i = end + 1
                    continue
            if ch == "(" and depth == 0:
                return i
            if ch in "([{":
                depth += 1
            elif ch in ")]}" and depth:
                depth -= 1
            i += 1
        return None

    def _matching_parenthesis(self, text: str, open_index: int) -> int | None:
        depth = 0
        in_str = False
        i = open_index
        while i < len(text):
            ch = text[i]
            if in_str:
                if ch == '"':
                    if i + 1 < len(text) and text[i + 1] == '"':
                        i += 2
                        continue
                    if i == 0 or text[i - 1] != "\\":
                        in_str = False
                i += 1
                continue
            if text.startswith('@"', i):
                in_str = True
                i += 2
                continue
            if ch == '"':
                in_str = True
                i += 1
                continue
            if text.startswith("\\@", i):
                end = text.find("\\@", i + 2)
                i = len(text) if end == -1 else end + 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return None

    def _rhs_has_top_level_operator_before(self, text: str, limit: int) -> bool:
        i = 0
        while i < limit:
            if text.startswith("[[", i):
                end = text.find("]]", i + 2)
                i = limit if end == -1 else min(limit, end + 2)
                continue
            if text.startswith("\\@", i):
                end = text.find("\\@", i + 2)
                i = limit if end == -1 else min(limit, end + 2)
                continue
            if any(text.startswith(op, i) for op in ["==", "!=", "&&", "||", ">=", "<=", "<<", ">>", "+", "*", "/", "%", "&", "|", "^", "?", "#"]):
                return True
            i += 1
        return False

    # ---- flow scanning ----------------------------------------------------
    def _find_if_branch(self, frame: ExecFrame, pc: int) -> int:
        i = pc + 1; depth = 0
        while i < len(frame.fn.lines):
            k, r = self._keyword(frame.fn.lines[i].text.strip())
            if k == "IF": depth += 1
            elif k == "ENDIF":
                if depth == 0: return i + 1
                depth -= 1
            elif depth == 0 and k == "ELSE": return i + 1
            elif depth == 0 and k == "ELSEIF":
                if truth(eval_expr(self, r)):
                    return i + 1
            i += 1
        return len(frame.fn.lines)

    def _find_matching(self, frame: ExecFrame, pc: int, targets: set[str]) -> int:
        start_key, _ = self._keyword(frame.fn.lines[pc].text.strip())
        pairs = {"IF": "ENDIF", "SELECTCASE": "ENDSELECT", "FOR": "NEXT", "WHILE": "WEND", "DO": "LOOP", "REPEAT": "REND"}
        openers = set(pairs)
        depth = 0
        i = pc + 1
        while i < len(frame.fn.lines):
            k, _ = self._keyword(frame.fn.lines[i].text.strip())
            if k in openers and pairs.get(k) in targets:
                depth += 1
            elif k in targets:
                if depth == 0: return i
                depth -= 1
            i += 1
        return len(frame.fn.lines) - 1

    def _find_case(self, frame: ExecFrame, pc: int, value: Value) -> int:
        end = self._find_matching(frame, pc, {"ENDSELECT"})
        caseelse = end
        i = pc + 1; depth = 0
        while i < end:
            k, r = self._keyword(frame.fn.lines[i].text.strip())
            if k == "SELECTCASE": depth += 1
            elif k == "ENDSELECT": depth -= 1
            elif depth == 0 and k == "CASEELSE":
                caseelse = i
            elif depth == 0 and k == "CASE" and self._case_matches(r, value):
                return i + 1
            i += 1
        return caseelse + (1 if caseelse != end else 0)

    def _case_matches(self, rest: str, value: Value) -> bool:
        for part in split_era_args(rest):
            p = part.strip()
            up = p.upper()
            if up.startswith("IS "):
                expr = f"{to_int(value)} {p[3:].strip()}"
                if truth(eval_expr(self, expr)): return True
            elif " TO " in up:
                a, b = re_split_to(p)
                if to_int(eval_expr(self, a)) <= to_int(value) <= to_int(eval_expr(self, b)): return True
            elif eval_expr(self, p) == value:
                return True
        return False

    def _find_catch_for_try(self, frame: ExecFrame, try_pc: int) -> int | None:
        depth = 0
        i = try_pc + 1
        while i < len(frame.fn.lines):
            k, _ = self._keyword(frame.fn.lines[i].text.strip())
            if k in {"TRYCALL", "TRYCCALL", "TRYCALLFORM", "TRYCCALLFORM"}:
                depth += 1
            elif k == "CATCH":
                if depth == 0:
                    return i
            elif k == "ENDCATCH":
                if depth == 0:
                    return None
                depth -= 1
            i += 1
        return None

    def _find_matching_catch_end(self, frame: ExecFrame, catch_pc: int) -> int:
        depth = 0
        i = catch_pc + 1
        while i < len(frame.fn.lines):
            k, _ = self._keyword(frame.fn.lines[i].text.strip())
            if k in {"TRYCALL", "TRYCCALL", "TRYCALLFORM", "TRYCCALLFORM"}:
                depth += 1
            elif k == "ENDCATCH":
                if depth == 0:
                    return i
                depth -= 1
            i += 1
        return len(frame.fn.lines) - 1

    def _exec_for(self, frame: ExecFrame, rest: str) -> None:
        parts = split_era_args(rest)
        if len(parts) < 3:
            frame.pc += 1; return
        ref = parse_lvalue(self, parts[0])
        start = to_int(eval_expr(self, parts[1])); end = to_int(eval_expr(self, parts[2])); step = to_int(eval_expr(self, parts[3])) if len(parts) >= 4 else 1
        if step == 0: step = 1
        self.memory.set_var(ref.base, ref.indices, start)
        active = start < end if step > 0 else start > end
        if not active:
            frame.pc = self._find_matching(frame, frame.pc, {"NEXT"}) + 1
            return
        frame.loops.append({"type": "FOR", "pc": frame.pc, "var": ref, "end": end, "step": step})
        frame.pc += 1

    def _exec_next(self, frame: ExecFrame) -> None:
        loop = self._last_loop(frame, "FOR")
        if not loop:
            frame.pc += 1; return
        ref = loop["var"]
        cur = to_int(self.memory.get_var(ref.base, ref.indices)) + loop["step"]
        self.memory.set_var(ref.base, ref.indices, cur)
        active = cur < loop["end"] if loop["step"] > 0 else cur > loop["end"]
        if active:
            frame.pc = loop["pc"] + 1
        else:
            frame.loops.remove(loop)
            frame.pc += 1

    def _last_loop(self, frame: ExecFrame, typ: str) -> dict[str, Any] | None:
        for loop in reversed(frame.loops):
            if loop.get("type") == typ:
                return loop
        return None

    def _find_loop_end(self, frame: ExecFrame, pc: int) -> int:
        return self._find_matching(frame, pc, {"NEXT", "WEND", "LOOP", "REND"})

    def _find_loop_continue(self, frame: ExecFrame, pc: int) -> int:
        return self._find_loop_end(frame, pc)

    def _jump_target_pc(self, frame: ExecFrame, target: int) -> int:
        if frame.loops:
            frame.loops = [loop for loop in frame.loops if self._loop_contains_pc(frame, loop, target)]
        return target

    def _loop_contains_pc(self, frame: ExecFrame, loop: dict[str, Any], target: int) -> bool:
        start = int(loop.get("pc", -1))
        typ = loop.get("type")
        end_targets = {
            "FOR": {"NEXT"},
            "WHILE": {"WEND"},
            "DO": {"LOOP"},
            "REPEAT": {"REND"},
        }.get(typ)
        if start < 0 or not end_targets:
            return False
        end = self._find_matching(frame, start, end_targets)
        return start < target <= end


ASSIGN_OPS = ["+=", "-=", "*=", "/=", "%=", "|=", "&=", "^=", "'=", "="]


def split_call_syntax(rest: str) -> tuple[str, list[str]] | None:
    s = rest.strip()
    if not s:
        return None
    first_comma = _find_top_level_char(s, ",")
    first_paren = _find_top_level_char(s, "(")
    if first_paren != -1 and (first_comma == -1 or first_paren < first_comma):
        close = _find_matching_paren(s, first_paren)
        if close != -1:
            target = s[:first_paren].strip()
            inner = s[first_paren + 1 : close]
            args = split_era_args(inner) if inner.strip() else []
            tail = s[close + 1 :].strip()
            if tail.startswith(","):
                args.extend(split_era_args(tail[1:]))
            return target, args
    parts = split_era_args(s)
    if not parts:
        return None
    return parts[0], parts[1:]


def _find_top_level_char(text: str, char: str) -> int:
    depth = 0
    in_str = False
    in_percent = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == '"' and (i == 0 or text[i - 1] != "\\"):
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
        elif text.startswith("[[", i):
            end = text.find("]]", i + 2)
            i = len(text) if end == -1 else end + 2
            continue
        elif text.startswith("\\@", i):
            end = text.find("\\@", i + 2)
            i = len(text) if end == -1 else end + 2
            continue
        elif ch == "%":
            in_percent = not in_percent
        elif in_percent:
            pass
        elif ch in "{[":
            depth += 1
        elif ch in "}]" and depth:
            depth -= 1
        elif ch == "(":
            if depth == 0 and char == "(":
                return i
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif ch == char and depth == 0:
            return i
        i += 1
    return -1


def _find_matching_paren(text: str, open_index: int) -> int:
    depth = 0
    in_str = False
    i = open_index
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == '"' and (i == 0 or text[i - 1] != "\\"):
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
        elif text.startswith("[[", i):
            end = text.find("]]", i + 2)
            i = len(text) if end == -1 else end + 2
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def find_assignment(text: str) -> tuple[str, str, str] | None:
    depth = 0; in_str = False; i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == '"' and text[i - 1] != "\\": in_str = False
            i += 1; continue
        if ch == '"': in_str = True; i += 1; continue
        if text.startswith("[[", i):
            end = text.find("]]", i + 2); i = len(text) if end == -1 else end + 2; continue
        if ch in "(": depth += 1
        elif ch in ")" and depth: depth -= 1
        elif depth == 0:
            for op in ASSIGN_OPS:
                if text.startswith(op, i):
                    # avoid comparison operators
                    prev = text[i - 1] if i else ""
                    nxt = text[i + len(op)] if i + len(op) < len(text) else ""
                    if op == "=" and (prev in "!<>=" or nxt == "="):
                        continue
                    return text[:i].strip(), op, text[i + len(op):].strip(" \t")
        i += 1
    return None


def should_form_string(rhs: str) -> bool:
    s = rhs.strip()
    if not s:
        return True
    if s.startswith('"') or s.startswith('@"'):
        return False
    if "%" in s or "{" in s or "\\@" in s:
        return True
    if s in {"/", "／", "|", "｜"}:
        return True
    # If the RHS contains explicit arithmetic/call syntax, evaluate it.
    if any(op in s for op in ["+", "*", "/", "%", "==", "!=", "&&", "||"]):
        return False
    if "(" in s and ")" in s:
        return False
    return True


def eval_float(ctx: EraRuntime, text: str) -> float:
    s = text.strip()
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", s):
        return float(s)
    return float(to_int(eval_expr(ctx, s, default=0)))


def re_split_to(text: str) -> tuple[str, str]:
    import re
    m = re.split(r"\s+TO\s+", text, flags=re.IGNORECASE, maxsplit=1)
    return (m[0], m[1]) if len(m) == 2 else (text, text)

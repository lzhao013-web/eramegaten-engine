from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .loader import load_program
from .model import Program
from .runtime import EraRuntime


class FrontendSession:
    """Headless controller shared by live front ends and frontend tests.

    The runtime remains synchronous.  A concrete GUI should call these methods
    from a worker thread, then read ``status()``/``layout()`` only after the
    worker has completed.  This keeps Tk/Qt event-loop concerns out of the
    engine and provides one stable adapter for later front ends.
    """

    def __init__(self, *, max_steps: int = 100_000, state_dir: str | Path | None = None):
        self.max_steps = max(1, int(max_steps))
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.root: Path | None = None
        self.entry = "SYSTEM_TITLE"
        self.program: Program | None = None
        self.runtime: EraRuntime | None = None
        self.load_seconds = 0.0
        self.last_steps = 0
        self.total_steps = 0
        self.step_limited = False
        self.stop_requested = False

    def load(self, root: str | Path, *, entry: str = "SYSTEM_TITLE") -> int:
        """Load a game tree and run ``entry`` until completion or input."""

        target = Path(root).expanduser().resolve()
        if not (target / "ERB").is_dir():
            raise FileNotFoundError(f"ERB directory not found: {target / 'ERB'}")
        started = time.perf_counter()
        program = load_program(target)
        self.load_seconds = time.perf_counter() - started
        self.root = target
        self.program = program
        return self.run_entry(entry)

    def run_entry(self, entry: str | None = None) -> int:
        """Start a fresh runtime while reusing the already loaded program."""

        if self.program is None:
            raise RuntimeError("no game has been loaded")
        if entry is not None:
            self.entry = str(entry).strip() or "SYSTEM_TITLE"
        runtime = EraRuntime(
            self.program,
            echo=False,
            interactive=False,
            state_dir=self.state_dir,
            pause_on_input=True,
        )
        self.runtime = runtime
        self.total_steps = 0
        self.stop_requested = False
        return self._record_run(lambda: runtime.run(self.entry, max_steps=self.max_steps))

    def submit(self, value: Any) -> int:
        """Submit one text/button value and continue the paused runtime."""

        runtime = self._require_runtime()
        if not runtime.stack:
            return 0
        runtime.queue_input(str(value))
        return self._record_run(lambda: runtime.continue_run(max_steps=self.max_steps))

    def advance(self) -> int:
        """Advance a WAIT/WAITANYKEY boundary without choosing a value."""

        runtime = self._require_runtime()
        if not runtime.stack:
            return 0
        if runtime.waiting_for_input:
            runtime.queue_input("")
        return self._record_run(lambda: runtime.continue_run(max_steps=self.max_steps))

    def input_boundary(self) -> dict[str, Any]:
        """Describe the input boundary currently blocking the runtime.

        Front ends use this distinction to offer a safe message-skip action:
        message waits may be advanced automatically, while menus and free-form
        input must always remain under explicit player control.
        """

        runtime = self.runtime
        if runtime is None:
            return {"kind": "unloaded", "keyword": "", "function": "", "pc": -1, "source": ""}
        if not runtime.stack:
            return {"kind": "finished", "keyword": "", "function": "", "pc": -1, "source": ""}
        frame = runtime.stack[-1]
        source = frame.fn.lines[frame.pc].text.strip() if frame.pc < len(frame.fn.lines) else ""
        keyword, _rest = runtime._keyword(source) if source else ("", "")
        if not runtime.waiting_for_input:
            kind = "running"
        elif (
            keyword in {"WAIT", "WAITANYKEY", "FORCEWAIT"}
            or (keyword.startswith("PRINT") and (keyword.endswith("W") or "FORMW" in keyword))
            or (keyword.startswith("RANDDATA") and keyword.endswith("W"))
        ):
            kind = "message"
        elif keyword in {
            "INPUT",
            "ONEINPUT",
            "INPUTS",
            "ONEINPUTS",
            "INPUTANY",
            "BINPUT",
            "BINPUTS",
            "TINPUT",
            "TINPUTS",
            "TONEINPUT",
            "TONEINPUTS",
            "FLOWINPUT",
            "FLOWINPUTS",
            "__SHOPINPUT",
            "__TRAININPUT",
        }:
            kind = "input"
        else:
            # Unknown waits are deliberately treated as choices.  It is much
            # safer to stop a batch early than to feed an empty value into a
            # persistence dialog or a game-specific input implementation.
            kind = "choice"
        return {
            "kind": kind,
            "keyword": keyword,
            "function": frame.fn.name,
            "pc": frame.pc,
            "source": source,
        }

    def skip_messages(self, max_messages: int = 20) -> dict[str, Any]:
        """Advance consecutive message waits and stop before any real input.

        The safety limit prevents an unexpectedly long script from monopolizing
        the GUI worker.  No numeric, string, shop, training, or unknown input
        boundary is ever auto-selected.
        """

        limit = max(1, min(500, int(max_messages)))
        skipped = 0
        executed_steps = 0
        while skipped < limit:
            boundary = self.input_boundary()
            if boundary["kind"] != "message":
                break
            runtime = self._require_runtime()
            signature = (boundary["function"], boundary["pc"], boundary["source"])
            runtime.queue_input("")
            steps = self._record_run(lambda: runtime.continue_run(max_steps=self.max_steps))
            executed_steps += steps
            skipped += 1
            next_boundary = self.input_boundary()
            next_signature = (
                next_boundary.get("function", ""),
                next_boundary.get("pc", -1),
                next_boundary.get("source", ""),
            )
            if steps <= 0 or (next_boundary.get("kind") == "message" and next_signature == signature):
                break
        boundary = self.input_boundary()
        return {
            "skipped": skipped,
            "limit": limit,
            "steps": executed_steps,
            "stopped_at": boundary["kind"],
            "keyword": boundary["keyword"],
            "function": boundary["function"],
        }

    def click(
        self,
        x: int,
        y: int,
        *,
        char_width: int = 8,
        line_height: int = 20,
        viewport_width: int | None = None,
        html_unit_scale: float = 1.0,
        advance_if_empty: bool = True,
    ) -> tuple[str | None, int]:
        """Queue a page-model click and continue if it selects/advances."""

        runtime = self._require_runtime()
        px = int(x)
        py = int(y)
        value = runtime.html_click_value(
            px,
            py,
            char_width=char_width,
            line_height=line_height,
            viewport_width=viewport_width,
            html_unit_scale=html_unit_scale,
        )
        return self.activate_pointer(
            px,
            py,
            value,
            advance_if_empty=advance_if_empty,
        )

    def activate_pointer(
        self,
        x: int,
        y: int,
        button_value: str | None,
        *,
        advance_if_empty: bool = True,
    ) -> tuple[str | None, int]:
        """Activate an already hit-tested scene position.

        GUI scene views can resolve the exact visual item under the cursor and
        pass its value here.  This avoids rebuilding/reflowing the layout
        between mouse press and execution, which previously made clicks drift
        after scrolling, resizing, or zooming.
        """

        runtime = self._require_runtime()
        runtime.mouse_x = int(x)
        runtime.mouse_y = int(y)
        runtime.mouse_button = "" if button_value is None else str(button_value)
        value = None if button_value is None else str(button_value)
        if value is not None:
            runtime.queue_input(value)
        elif not advance_if_empty or not runtime.waiting_for_input:
            return None, 0
        else:
            runtime.queue_input("")
        if not runtime.stack:
            return value, 0
        return value, self._record_run(lambda: runtime.continue_run(max_steps=self.max_steps))

    def update_pointer(self, x: int, y: int, hover_value: str | None = None) -> None:
        """Update live MOUSEX/MOUSEY/MOUSEB state without submitting input."""

        runtime = self.runtime
        if runtime is None:
            return
        runtime.mouse_x = int(x)
        runtime.mouse_y = int(y)
        runtime.mouse_button = "" if hover_value is None else str(hover_value)

    def update_key(self, key_code: int, *, pressed: bool, triggered: bool = False) -> None:
        """Update the runtime key polling state used by GETKEY/AWAIT helpers."""

        runtime = self.runtime
        if runtime is None:
            return
        code = int(key_code)
        if pressed:
            runtime.key_state.add(code)
            if triggered:
                runtime.key_triggered.add(code)
        else:
            runtime.key_state.discard(code)

    def set_active(self, active: bool) -> None:
        runtime = self.runtime
        if runtime is not None:
            runtime.is_active = bool(active)

    def request_stop(self) -> bool:
        """Ask an executing runtime slice to stop at its next loop boundary."""

        runtime = self.runtime
        if runtime is None or not runtime.stack:
            return False
        self.stop_requested = True
        runtime.stack.clear()
        runtime.memory.frames.clear()
        runtime.waiting_for_input = False
        return True

    def layout(
        self,
        *,
        char_width: int = 8,
        line_height: int = 20,
        viewport_width: int | None = None,
        html_unit_scale: float = 1.0,
    ) -> dict[str, Any]:
        runtime = self._require_runtime()
        return runtime.html_layout_model(
            char_width=char_width,
            line_height=line_height,
            viewport_width=viewport_width,
            html_unit_scale=html_unit_scale,
        )

    def status(self) -> dict[str, Any]:
        runtime = self.runtime
        program = self.program
        if runtime is None:
            return {
                "loaded": program is not None,
                "root": str(self.root or ""),
                "entry": self.entry,
                "load_seconds": self.load_seconds,
                "last_steps": self.last_steps,
                "total_steps": self.total_steps,
                "step_limited": self.step_limited,
                "stopped": self.stop_requested,
                "waiting": False,
                "finished": False,
                "warnings": list(program.warnings) if program else [],
                "stack": [],
            }
        stack = [
            {
                "function": frame.fn.name,
                "pc": frame.pc,
                "source": str(program.file_of(frame.fn)) if program else "",
            }
            for frame in reversed(runtime.stack)
        ]
        return {
            "loaded": True,
            "root": str(self.root or ""),
            "entry": self.entry,
            "load_seconds": self.load_seconds,
            "files": len(program.files) if program else 0,
            "functions": program.function_count if program else 0,
            "last_steps": self.last_steps,
            "total_steps": self.total_steps,
            "step_limited": self.step_limited,
            "stopped": self.stop_requested,
            "waiting": runtime.waiting_for_input,
            "finished": not runtime.stack,
            "fatal_error": runtime.fatal_error,
            "warnings": [*(program.warnings if program else []), *runtime.warnings],
            "stack": stack,
            "lines": runtime._line_count(),
            "output_chars": sum(len(part) for part in runtime.output),
            "buttons": len(runtime.print_buttons) + len(runtime.html_buttons),
            "images": len(runtime.print_images) + len(runtime.html_images),
            "timed_waits": len(runtime.timed_wait_events),
            "sound_events": len(runtime.sound_events),
        }

    def _require_runtime(self) -> EraRuntime:
        if self.runtime is None:
            raise RuntimeError("no runtime has been started")
        return self.runtime

    def _record_run(self, action: Callable[[], int]) -> int:
        runtime = self._require_runtime()
        warning_count = len(runtime.warnings)
        self.step_limited = False
        self.last_steps = int(action())
        self.total_steps += self.last_steps
        max_warning = f"max step limit reached ({self.max_steps})"
        if len(runtime.warnings) > warning_count and runtime.warnings[-1] == max_warning:
            # In a live front end max_steps is a cooperative execution slice,
            # not a compatibility failure.  Keep it in structured status and
            # omit the noisy warning that would otherwise accumulate on every
            # press of Continue.
            runtime.warnings.pop()
            self.step_limited = True
        return self.last_steps

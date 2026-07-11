from __future__ import annotations

import argparse
import queue
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from .frontend import FrontendSession

try:
    from PIL import ImageTk
except Exception:  # pragma: no cover - depends on the host GUI environment
    ImageTk = None


class EraMegatenTkApp:
    """Small dependency-light desktop inspector for the engine page model."""

    MAX_DRAWABLES = 20_000

    def __init__(
        self,
        window: tk.Tk,
        *,
        root_path: str = "",
        entry: str = "SYSTEM_TITLE",
        max_steps: int = 30_000,
        auto_run: bool = True,
    ):
        self.window = window
        self.session = FrontendSession(max_steps=max_steps)
        self._results: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._busy = False
        self._closed = False
        self._photos: list[Any] = []
        self._font_cache: dict[tuple[str, int], tkfont.Font] = {}
        self._worker_buttons: list[ttk.Button] = []
        self._last_layout_metrics = (8, 20, 1, 1.0)

        self.path_var = tk.StringVar(value=root_path)
        self.entry_var = tk.StringVar(value=entry or "SYSTEM_TITLE")
        self.max_steps_var = tk.StringVar(value=str(max(1, int(max_steps))))
        self.input_var = tk.StringVar()
        self.status_var = tk.StringVar(value="请选择游戏目录并加载")
        self.follow_var = tk.BooleanVar(value=True)

        self.window.title("eraMegaten Engine - 检阅前端")
        self.window.geometry("1400x900")
        self.window.minsize(900, 600)
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        self._configure_style()
        self._build_widgets()

        if auto_run and root_path and (Path(root_path).expanduser() / "ERB").is_dir():
            self.window.after(250, self._load_and_run)

    # ---- UI construction -------------------------------------------------
    def _configure_style(self) -> None:
        self.window.configure(background="#17191c")
        style = ttk.Style(self.window)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background="#202328", foreground="#e7e7e7")
        style.configure("TFrame", background="#202328")
        style.configure("TLabel", background="#202328", foreground="#e7e7e7")
        style.configure("TCheckbutton", background="#202328", foreground="#e7e7e7")
        style.configure("TButton", padding=(8, 5))
        style.configure("TEntry", fieldbackground="#111317", foreground="#f2f2f2")
        style.configure("TNotebook", background="#202328")
        style.configure("TNotebook.Tab", padding=(10, 5))

    def _build_widgets(self) -> None:
        toolbar = ttk.Frame(self.window, padding=8)
        toolbar.pack(side=tk.TOP, fill=tk.X)
        toolbar.columnconfigure(1, weight=1)

        ttk.Label(toolbar, text="游戏目录").grid(row=0, column=0, padx=(0, 6), pady=3, sticky="w")
        path_entry = ttk.Entry(toolbar, textvariable=self.path_var)
        path_entry.grid(row=0, column=1, columnspan=5, padx=(0, 6), pady=3, sticky="ew")
        ttk.Button(toolbar, text="浏览…", command=self._browse).grid(row=0, column=6, pady=3)

        ttk.Label(toolbar, text="入口").grid(row=1, column=0, padx=(0, 6), pady=3, sticky="w")
        ttk.Entry(toolbar, textvariable=self.entry_var, width=28).grid(row=1, column=1, padx=(0, 6), pady=3, sticky="w")
        ttk.Label(toolbar, text="单次步数").grid(row=1, column=2, padx=(8, 6), pady=3)
        ttk.Entry(toolbar, textvariable=self.max_steps_var, width=10).grid(row=1, column=3, padx=(0, 6), pady=3)
        load_button = ttk.Button(toolbar, text="加载并运行", command=self._load_and_run)
        load_button.grid(row=1, column=4, padx=3, pady=3)
        rerun_button = ttk.Button(toolbar, text="重新运行入口", command=self._rerun)
        rerun_button.grid(row=1, column=5, padx=3, pady=3)
        shot_button = ttk.Button(toolbar, text="导出当前页", command=self._export_page)
        shot_button.grid(row=1, column=6, padx=(3, 0), pady=3)
        self._worker_buttons.extend([load_button, rerun_button, shot_button])

        status_bar = ttk.Frame(self.window, padding=(8, 0, 8, 6))
        status_bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(status_bar, textvariable=self.status_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Checkbutton(status_bar, text="自动跟随输出", variable=self.follow_var).pack(side=tk.RIGHT)

        paned = ttk.Panedwindow(self.window, orient=tk.HORIZONTAL)
        paned.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8)

        game_frame = ttk.Frame(paned)
        inspector_frame = ttk.Frame(paned, width=360)
        paned.add(game_frame, weight=4)
        paned.add(inspector_frame, weight=1)

        game_frame.rowconfigure(0, weight=1)
        game_frame.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            game_frame,
            background="#000000",
            highlightthickness=0,
            xscrollincrement=1,
            yscrollincrement=1,
        )
        y_scroll = ttk.Scrollbar(game_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        x_scroll = ttk.Scrollbar(game_frame, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.canvas.bind("<Button-1>", self._canvas_click)
        self.canvas.bind("<MouseWheel>", self._mouse_wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._shift_mouse_wheel)

        notebook = ttk.Notebook(inspector_frame)
        notebook.pack(fill=tk.BOTH, expand=True)
        self.info_text = self._text_tab(notebook, "状态")
        self.warning_text = self._text_tab(notebook, "警告")
        self.transcript_text = self._text_tab(notebook, "文本")

        input_bar = ttk.Frame(self.window, padding=8)
        input_bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(input_bar, text="输入").pack(side=tk.LEFT, padx=(0, 6))
        input_entry = ttk.Entry(input_bar, textvariable=self.input_var)
        input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        input_entry.bind("<Return>", lambda _event: self._submit())
        submit_button = ttk.Button(input_bar, text="发送", command=self._submit)
        submit_button.pack(side=tk.LEFT, padx=(6, 3))
        advance_button = ttk.Button(input_bar, text="继续 / 任意键", command=self._advance)
        advance_button.pack(side=tk.LEFT, padx=(3, 0))
        self._worker_buttons.extend([submit_button, advance_button])

    def _text_tab(self, notebook: ttk.Notebook, title: str) -> tk.Text:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=title)
        text = tk.Text(
            frame,
            wrap=tk.WORD,
            background="#111317",
            foreground="#d8d8d8",
            insertbackground="#ffffff",
            relief=tk.FLAT,
            padx=8,
            pady=8,
        )
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scroll.set, state=tk.DISABLED)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        return text

    # ---- commands --------------------------------------------------------
    def _browse(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.path_var.get() or str(Path.cwd()))
        if selected:
            self.path_var.set(selected)

    def _read_max_steps(self) -> int:
        try:
            value = max(1, int(self.max_steps_var.get().strip()))
        except ValueError:
            value = 30_000
            self.max_steps_var.set(str(value))
        self.session.max_steps = value
        return value

    def _load_and_run(self) -> None:
        path = self.path_var.get().strip()
        if not path:
            self._browse()
            path = self.path_var.get().strip()
        if not path:
            return
        entry = self.entry_var.get().strip() or "SYSTEM_TITLE"
        self._read_max_steps()
        self._run_async("正在加载脚本、CSV 与资源索引…", lambda: self.session.load(path, entry=entry))

    def _rerun(self) -> None:
        if self.session.program is None:
            self._load_and_run()
            return
        entry = self.entry_var.get().strip() or "SYSTEM_TITLE"
        self._read_max_steps()
        self._run_async(f"正在运行入口 {entry}…", lambda: self.session.run_entry(entry))

    def _submit(self) -> None:
        if self.session.runtime is None:
            return
        value = self.input_var.get()
        self.input_var.set("")
        self._read_max_steps()
        self._run_async(f"提交输入 {value!r}…", lambda: self.session.submit(value))

    def _advance(self) -> None:
        if self.session.runtime is None:
            return
        self._read_max_steps()
        self._run_async("继续运行…", self.session.advance)

    def _export_page(self) -> None:
        runtime = self.session.runtime
        if runtime is None:
            return
        target = filedialog.asksaveasfilename(
            title="导出当前页",
            defaultextension=".png",
            filetypes=[("PNG image", "*.png")],
        )
        if not target:
            return
        char_width, line_height, viewport, html_scale = self._last_layout_metrics
        self._run_async(
            "正在导出当前页…",
            lambda: runtime.export_page_png(
                target,
                char_width=char_width,
                line_height=line_height,
                viewport_width=viewport,
                html_unit_scale=html_scale,
            ),
        )

    def _canvas_click(self, event: tk.Event) -> None:
        runtime = self.session.runtime
        if runtime is None or self._busy:
            return
        x = int(self.canvas.canvasx(event.x))
        y = int(self.canvas.canvasy(event.y))
        char_width, line_height, viewport, html_scale = self._last_layout_metrics
        self._run_async(
            f"点击 ({x}, {y})…",
            lambda: self.session.click(
                x,
                y,
                char_width=char_width,
                line_height=line_height,
                viewport_width=viewport,
                html_unit_scale=html_scale,
            ),
        )

    def _mouse_wheel(self, event: tk.Event) -> str:
        delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta * 3, "units")
        return "break"

    def _shift_mouse_wheel(self, event: tk.Event) -> str:
        delta = -1 if event.delta > 0 else 1
        self.canvas.xview_scroll(delta * 3, "units")
        return "break"

    # ---- worker orchestration -------------------------------------------
    def _run_async(self, label: str, action: Callable[[], Any]) -> None:
        if self._busy or self._closed:
            return
        self._busy = True
        self.status_var.set(label)
        self._set_worker_buttons(tk.DISABLED)

        def worker() -> None:
            try:
                self._results.put(("ok", action()))
            except Exception:
                self._results.put(("error", traceback.format_exc()))

        threading.Thread(target=worker, daemon=True, name="eramegaten-gui-worker").start()
        self.window.after(60, self._poll_worker)

    def _poll_worker(self) -> None:
        if self._closed:
            return
        try:
            kind, payload = self._results.get_nowait()
        except queue.Empty:
            self.window.after(60, self._poll_worker)
            return
        self._busy = False
        self._set_worker_buttons(tk.NORMAL)
        if kind == "error":
            self.status_var.set("操作失败")
            self._replace_text(self.warning_text, str(payload))
            messagebox.showerror("eraMegaten Engine", str(payload).splitlines()[-1] if payload else "操作失败")
            return
        self._render_runtime()

    def _set_worker_buttons(self, state: str) -> None:
        for button in self._worker_buttons:
            try:
                button.configure(state=state)
            except tk.TclError:
                pass

    # ---- rendering -------------------------------------------------------
    def _base_font(self) -> tkfont.Font:
        key = ("ＭＳ ゴシック", 16)
        font = self._font_cache.get(key)
        if font is None:
            font = tkfont.Font(self.window, family=key[0], size=key[1])
            self._font_cache[key] = font
        return font

    def _drawable_font(self, item: dict[str, Any]) -> tkfont.Font:
        family = str(item.get("font", "") or "ＭＳ ゴシック")
        style = self._int(item.get("font_style"), 0)
        key = (family, style)
        font = self._font_cache.get(key)
        if font is None:
            font = tkfont.Font(
                self.window,
                family=family,
                size=16,
                weight="bold" if style & 1 else "normal",
                slant="italic" if style & 2 else "roman",
                underline=bool(style & 4),
                overstrike=bool(style & 8),
            )
            self._font_cache[key] = font
        return font

    def _render_runtime(self) -> None:
        runtime = self.session.runtime
        if runtime is None:
            return
        self.window.update_idletasks()
        base_font = self._base_font()
        char_width = max(7, int(base_font.measure("0")))
        line_height = max(18, int(base_font.metrics("linespace")) + 4)
        viewport = max(1, int(self.canvas.winfo_width()))
        html_scale = 0.16
        self._last_layout_metrics = (char_width, line_height, viewport, html_scale)
        layout = self.session.layout(
            char_width=char_width,
            line_height=line_height,
            viewport_width=viewport,
            html_unit_scale=html_scale,
        )

        self.canvas.delete("all")
        self._photos.clear()
        self.canvas.configure(background=self._color(runtime.default_bgcolor, "#000000"))
        drawables = list(layout.get("drawables", []))
        truncated = len(drawables) > self.MAX_DRAWABLES
        if truncated:
            drawables = drawables[-self.MAX_DRAWABLES :]

        for item in drawables:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type", ""))
            if kind == "print_space":
                continue
            x = self._int(item.get("x"))
            y = self._int(item.get("y"))
            width = max(0, self._int(item.get("width")))
            height = max(0, self._int(item.get("height"), line_height))
            color = self._color(item.get("color"), self._color(runtime.current_color, "#c0c0c0"))
            bgcolor = self._color(item.get("bgcolor"), self._color(runtime.default_bgcolor, "#000000"))
            clickable = kind in {"button", "print_button", "implicit_button"} or (kind == "image" and item.get("parent") == "button")
            tags = ("clickable",) if clickable else ()

            if kind in {"image", "print_image"}:
                if not self._draw_image(runtime, item, x, y, width, height, tags):
                    self.canvas.create_rectangle(x, y, x + max(1, width), y + max(1, height), outline=color, tags=tags)
                    self.canvas.create_text(x + 3, y + 2, anchor="nw", text=str(item.get("src", "")), fill=color, font=base_font, tags=tags)
                continue
            if width > 0 and height > 0 and bgcolor.lower() != self._color(runtime.default_bgcolor, "#000000").lower():
                self.canvas.create_rectangle(x, y, x + width, y + height, fill=bgcolor, outline="")
            if kind == "implicit_button":
                # The transcript text is already rendered by its text item;
                # retain only a transparent canvas hit region here.
                self.canvas.create_rectangle(
                    x,
                    y,
                    x + max(1, width),
                    y + max(1, height),
                    fill="",
                    outline="",
                    tags=tags,
                )
                continue
            if kind in {"button", "print_button", "nonbutton", "print_rect"}:
                outline = "#ffd54a" if clickable else color
                label = str(item.get("label", ""))
                # Full-line space buttons are used as transparent click areas
                # during title animations.  Keep their hit boxes in the
                # runtime layout without covering the page in visible frames.
                if kind == "print_rect" or label.strip():
                    self.canvas.create_rectangle(x, y, x + max(1, width), y + max(1, height), outline=outline, tags=tags)
                if kind != "print_rect":
                    self.canvas.create_text(
                        x,
                        y,
                        anchor="nw",
                        text=label,
                        fill=color,
                        font=self._drawable_font(item),
                        tags=tags,
                    )
                continue
            if kind in {"text", "html_text"}:
                self.canvas.create_text(
                    x,
                    y,
                    anchor="nw",
                    text=str(item.get("text", "")),
                    fill=color,
                    font=self._drawable_font(item),
                )

        canvas_info = layout.get("canvas", {})
        canvas_width = max(viewport, self._int(canvas_info.get("width"), viewport) + 20)
        canvas_height = max(line_height, self._int(canvas_info.get("height"), line_height) + 20)
        self.canvas.configure(scrollregion=(0, 0, canvas_width, canvas_height))
        if self.follow_var.get():
            self.canvas.yview_moveto(1.0)
        self._update_inspector(truncated=truncated)

    def _draw_image(
        self,
        runtime: Any,
        item: dict[str, Any],
        x: int,
        y: int,
        width: int,
        height: int,
        tags: tuple[str, ...],
    ) -> bool:
        if ImageTk is None or width <= 0 or height <= 0:
            return False
        source = str(item.get("src", ""))
        if not source:
            return False
        try:
            image = runtime.render_sprite_image(source)
            if image.size != (width, height):
                image = image.resize((width, height))
            photo = ImageTk.PhotoImage(image, master=self.window)
        except Exception:
            return False
        self._photos.append(photo)
        self.canvas.create_image(x, y, anchor="nw", image=photo, tags=tags)
        return True

    def _update_inspector(self, *, truncated: bool = False) -> None:
        status = self.session.status()
        stack_rows = status.get("stack", [])
        lines = [
            f"目录: {status.get('root', '')}",
            f"入口: {status.get('entry', '')}",
            f"加载: {status.get('load_seconds', 0):.2f}s",
            f"文件/函数: {status.get('files', 0)} / {status.get('functions', 0)}",
            f"本次/累计步数: {status.get('last_steps', 0)} / {status.get('total_steps', 0)}",
            f"状态: {'等待输入' if status.get('waiting') else '已结束' if status.get('finished') else '达到步数片上限' if status.get('step_limited') else '可继续'}",
            f"输出: {status.get('lines', 0)} 行，{status.get('output_chars', 0)} 字符",
            f"按钮/图片: {status.get('buttons', 0)} / {status.get('images', 0)}",
            f"计时等待/声音事件: {status.get('timed_waits', 0)} / {status.get('sound_events', 0)}",
            "",
            "调用栈（顶部优先）:",
        ]
        for row in stack_rows[:30]:
            lines.append(f"  {row.get('function')}  pc={row.get('pc')}")
        if not stack_rows:
            lines.append("  <empty>")
        if truncated:
            lines.extend(["", f"画布元素超过 {self.MAX_DRAWABLES}，仅显示末尾元素。"])
        self._replace_text(self.info_text, "\n".join(lines))

        warnings = status.get("warnings", [])
        self._replace_text(self.warning_text, "\n".join(str(value) for value in warnings) or "无警告")
        runtime = self.session.runtime
        transcript = "".join(runtime.output) if runtime is not None else ""
        if len(transcript) > 250_000:
            transcript = "[仅显示末尾 250000 字符]\n" + transcript[-250_000:]
        self._replace_text(self.transcript_text, transcript)

        if status.get("waiting"):
            state = "等待输入"
        elif status.get("finished"):
            state = "运行结束"
        elif status.get("step_limited"):
            state = "达到步数片上限，可继续"
        else:
            state = "已暂停"
        self.status_var.set(
            f"{state}｜累计 {status.get('total_steps', 0)} 步｜"
            f"{status.get('lines', 0)} 行｜警告 {len(warnings)}"
        )

    @staticmethod
    def _replace_text(widget: tk.Text, value: str) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert("1.0", value)
        widget.configure(state=tk.DISABLED)

    @staticmethod
    def _int(value: Any, default: int = 0) -> int:
        try:
            return int(float(str(value).strip() or default))
        except Exception:
            return default

    @classmethod
    def _color(cls, value: Any, default: str = "#c0c0c0") -> str:
        if isinstance(value, str) and value.startswith("#"):
            return value
        try:
            return f"#{int(value) & 0xFFFFFF:06x}"
        except Exception:
            return default

    def _close(self) -> None:
        self._closed = True
        self.window.destroy()


def launch_gui(
    root: str = "",
    *,
    entry: str = "SYSTEM_TITLE",
    max_steps: int = 30_000,
    auto_run: bool = True,
) -> int:
    window = tk.Tk()
    EraMegatenTkApp(
        window,
        root_path=root,
        entry=entry,
        max_steps=max_steps,
        auto_run=auto_run,
    )
    window.mainloop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="eraMegaten Engine desktop inspector")
    parser.add_argument("root", nargs="?", default="", help="game root; it can also be selected in the window")
    parser.add_argument("--entry", default="SYSTEM_TITLE", help="entry function")
    parser.add_argument("--max-steps", type=int, default=30_000, help="maximum steps per run/continue")
    parser.add_argument("--no-auto-run", action="store_true", help="open the window without loading the supplied root")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return launch_gui(
        args.root,
        entry=args.entry,
        max_steps=args.max_steps,
        auto_run=not args.no_auto_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


def norm_name(name: str) -> str:
    """Normalize Era identifiers.

    Emuera is normally case-insensitive for functions and variables.  Python's
    upper() keeps Japanese/Chinese names intact while unifying ASCII names.
    """

    return name.strip().upper()


class EraInputBlocked(Exception):
    """Internal sentinel used when expression-time input must pause execution."""


@dataclass(slots=True)
class SourceLine:
    text: str
    file_index: int
    number: int


@dataclass(slots=True)
class EraFunction:
    name: str
    params: list[str] = field(default_factory=list)
    defaults: dict[int, str] = field(default_factory=dict)
    lines: list[SourceLine] = field(default_factory=list)
    labels: dict[str, int] = field(default_factory=dict)
    is_function: bool = False
    returns_string: bool = False
    priority: int = 0
    later: bool = False
    source_file: int = -1
    source_line: int = 0
    local_size_expr: str = ""
    locals_size_expr: str = ""

    @property
    def key(self) -> str:
        return norm_name(self.name)


@dataclass(slots=True)
class VarDecl:
    name: str
    is_string: bool = False
    dims: tuple[int, ...] = ()
    dim_exprs: tuple[str, ...] = ()
    global_scope: bool = False
    charadata: bool = False
    savedata: bool = False
    module_scope: bool = False
    const: bool = False
    initial: tuple[str, ...] = ()
    raw: str = ""


@dataclass(slots=True)
class CharacterTemplate:
    no: int
    csv_no: int | None = None
    is_sp: bool = False
    name: str = ""
    callname: str = ""
    numeric: dict[str, dict[int, int]] = field(default_factory=dict)
    strings: dict[str, dict[int, str]] = field(default_factory=dict)
    raw: dict[str, list[list[str]]] = field(default_factory=dict)
    source: str = ""


@dataclass(slots=True)
class Program:
    root: Path
    files: list[str] = field(default_factory=list)
    functions: dict[str, list[EraFunction]] = field(default_factory=dict)
    var_decls: dict[str, VarDecl] = field(default_factory=dict)
    defines: dict[str, str] = field(default_factory=dict)
    define_names: dict[str, str] = field(default_factory=dict)
    csv: Any = None
    warnings: list[str] = field(default_factory=list)

    def add_file(self, path: Path) -> int:
        text = str(path)
        try:
            return self.files.index(text)
        except ValueError:
            self.files.append(text)
            return len(self.files) - 1

    def add_function(self, fn: EraFunction) -> None:
        self.functions.setdefault(fn.key, []).append(fn)

    def get_functions(self, name: str) -> list[EraFunction]:
        return self.functions.get(norm_name(name), [])

    def get_function(self, name: str) -> EraFunction | None:
        funcs = self.get_functions(name)
        if not funcs:
            return None
        # Emuera supports priority for event functions; for ordinary calls the
        # first loaded definition is the safest deterministic choice.
        return sorted(funcs, key=lambda f: (-f.priority, f.later, f.source_file, f.source_line))[0]

    @property
    def function_count(self) -> int:
        return sum(len(v) for v in self.functions.values())

    @property
    def duplicate_functions(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.functions.items() if len(v) > 1}

    def file_of(self, line: SourceLine | EraFunction | int) -> str:
        idx = line if isinstance(line, int) else line.file_index if isinstance(line, SourceLine) else line.source_file
        if 0 <= idx < len(self.files):
            return self.files[idx]
        return "<unknown>"


def split_era_args(text: str, delimiter: str = ",") -> list[str]:
    """Split an Era expression argument list without splitting nested forms."""

    args: list[str] = []
    start = 0
    depth = 0
    in_str = False
    raw_str = False
    form_brace_depth = 0
    form_percent_expr = False
    form_percent_string = False
    i = 0
    while i < len(text):
        ch = text[i]
        prev = text[i - 1] if i else ""
        if in_str:
            if raw_str:
                if ch == "{":
                    form_brace_depth += 1
                    i += 1
                    continue
                if ch == "}" and form_brace_depth:
                    form_brace_depth -= 1
                    i += 1
                    continue
                if ch == "%" and not form_brace_depth and not form_percent_string:
                    form_percent_expr = not form_percent_expr
                    i += 1
                    continue
            if ch == '"':
                if raw_str and form_percent_expr:
                    form_percent_string = not form_percent_string
                    i += 1
                    continue
                if i + 1 < len(text) and text[i + 1] == '"':
                    i += 2
                    continue
                if prev != "\\" and not (raw_str and (form_brace_depth or form_percent_expr)):
                    in_str = False
                    raw_str = False
                    form_brace_depth = 0
                    form_percent_expr = False
                    form_percent_string = False
            i += 1
            continue
        if ch == '@' and i + 1 < len(text) and text[i + 1] == '"':
            raw_str = True
            in_str = True
            form_brace_depth = 0
            form_percent_expr = False
            form_percent_string = False
            i += 2
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if text.startswith("[[", i):
            end = text.find("]]", i + 2)
            if end == -1:
                i += 2
            else:
                i = end + 2
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}" and depth:
            depth -= 1
        elif ch == delimiter and depth == 0:
            args.append(text[start:i].strip())
            start = i + 1
        i += 1
    tail = text[start:].strip()
    if tail or text.strip():
        args.append(tail)
    return args


def strip_comment(line: str) -> str:
    """Strip ; comments outside strings.

    Emuera treats ;!; as an escape for lines that should be ignored by
    eramaker but executed by Emuera, so the caller handles that prefix before
    using this helper.
    """

    in_str = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_str:
            if ch == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    i += 2
                    continue
                if i == 0 or line[i - 1] != "\\":
                    in_str = False
            i += 1
            continue
        if ch == '@' and i + 1 < len(line) and line[i + 1] == '"':
            in_str = True
            i += 2
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == ';':
            return line[:i].rstrip()
        i += 1
    return line.rstrip()


def read_text_auto(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp932", "shift_jis", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")

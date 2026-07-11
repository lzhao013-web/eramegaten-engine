from __future__ import annotations

import re
from pathlib import Path

from .csvdb import EraCsvDatabase
from .expr import eval_expr, to_int
from .model import EraFunction, Program, SourceLine, VarDecl, norm_name, read_text_auto, split_era_args, strip_comment

_HEADER_RE = re.compile(r"^@\s*(.+)$")


def parse_function_header(text: str) -> tuple[str, list[str], dict[int, str]]:
    body = text.strip()[1:].strip()
    params_text = ""
    if "(" in body:
        name, rest = body.split("(", 1)
        depth = 1
        end = 0
        for i, ch in enumerate(rest):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        params_text = rest[:end]
    else:
        parts = split_era_args(body)
        name = parts[0] if parts else body
        params_text = ",".join(parts[1:]) if len(parts) > 1 else ""
    name = name.strip().split()[0].rstrip(",")
    params: list[str] = []
    defaults: dict[int, str] = {}
    if params_text.strip():
        for i, raw in enumerate(split_era_args(params_text)):
            if not raw:
                params.append(f"ARG:{i}")
                continue
            if "=" in raw:
                p, d = raw.split("=", 1)
                defaults[i] = d.strip()
            else:
                p = raw
            params.append(p.strip())
    return name, params, defaults


_DECL_FLAGS = {"GLOBAL", "SAVEDATA", "CHARADATA", "REF", "DYNAMIC", "CONST"}


def _split_top_level_equals(text: str) -> tuple[str, str | None]:
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
        if ch == '@' and i + 1 < len(text) and text[i + 1] == '"':
            in_str = True
            i += 2
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if text.startswith("[[", i):
            end = text.find("]]", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}" and depth:
            depth -= 1
        elif ch == "=" and depth == 0:
            return text[:i].strip(), text[i + 1 :].strip()
        i += 1
    return text.strip(), None


def parse_var_decl(line: str) -> VarDecl | None:
    raw = strip_comment(line).strip()
    upper = raw.upper()
    if not (upper.startswith("#DIM") or upper.startswith("#DIMS")):
        return None
    is_string = upper.startswith("#DIMS")
    rest = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""
    before_eq, init_text = _split_top_level_equals(rest)
    flags: list[str] = []
    parts = split_era_args(before_eq)
    if not parts:
        return None
    first = parts[0]
    first_words = first.split()
    while first_words and norm_name(first_words[0]) in _DECL_FLAGS:
        flags.append(norm_name(first_words.pop(0)))
    if not first_words:
        return None
    name = first_words[0]
    dim_parts = first_words[1:] + parts[1:]
    dim_exprs = tuple(p.strip() for p in dim_parts if p.strip())
    dims: list[int] = []
    all_literal_dims = True
    for p in dim_exprs:
        try:
            dims.append(int(p, 0))
        except ValueError:
            all_literal_dims = False
    initials = tuple(split_era_args(init_text)) if init_text is not None else ()
    return VarDecl(
        name=name,
        is_string=is_string,
        dims=tuple(dims) if all_literal_dims else (),
        dim_exprs=dim_exprs,
        global_scope="GLOBAL" in flags,
        charadata="CHARADATA" in flags,
        savedata="SAVEDATA" in flags,
        const="CONST" in flags,
        initial=initials,
        raw=raw,
    )


def _register_var_decl(program: Program, decl: VarDecl, *, module_scope: bool) -> None:
    decl.module_scope = module_scope
    key = norm_name(decl.name)
    old = program.var_decls.get(key)
    if old is None or (decl.module_scope and not old.module_scope) or decl.global_scope or decl.savedata:
        program.var_decls[key] = decl


def load_program(root: str | Path, *, debug_blocks: bool = False, load_csv: bool = True) -> Program:
    root = Path(root)
    program = Program(root=root)
    if load_csv:
        program.csv = EraCsvDatabase.load(root)
        program.warnings.extend(program.csv.warnings)
    erb_root = root / "ERB"
    if not erb_root.exists():
        raise FileNotFoundError(f"ERB directory not found: {erb_root}")
    files = [p for p in erb_root.rglob("*") if p.is_file() and p.suffix.lower() in {".erb", ".erh"}]
    files.sort(key=lambda p: str(p).lower())
    for path in files:
        _load_erb_file(program, path, debug_blocks=debug_blocks)
    _resolve_var_decl_dimensions(program)
    return program


class _DeclEvalContext:
    def __init__(self, program: Program):
        self.program = program
        self._values: dict[str, int] = {}
        self._resolving: set[str] = set()

    def warn(self, message: str) -> None:
        return None

    def has_symbol(self, name: str) -> bool:
        key = norm_name(name)
        return key in self.program.var_decls or key in self.program.defines or bool(self.program.csv and key in self.program.csv.constants)

    def has_callable(self, name: str) -> bool:
        return False

    def call_expr_function(self, name: str, args: list[object]) -> int:
        return 0

    def get_var(self, base: str, indices: list[object]) -> int:
        key = norm_name(base)
        if indices:
            return 0
        if key in self._values:
            return self._values[key]
        if key in self._resolving:
            return 0
        self._resolving.add(key)
        try:
            if key in self.program.defines:
                value = to_int(eval_expr(self, self.program.defines[key], default=0))
            elif key in self.program.var_decls and self.program.var_decls[key].initial:
                value = to_int(eval_expr(self, self.program.var_decls[key].initial[0], default=0))
            elif self.program.csv and key in self.program.csv.constants:
                value = to_int(self.program.csv.constants[key])
            else:
                value = 0
            self._values[key] = value
            return value
        finally:
            self._resolving.discard(key)

    def set_var(self, base: str, indices: list[object], value: object) -> None:
        self._values[norm_name(base)] = to_int(value)


def _resolve_var_decl_dimensions(program: Program) -> None:
    ctx = _DeclEvalContext(program)
    for decl in program.var_decls.values():
        if not decl.dim_exprs:
            continue
        dims: list[int] = []
        for expr in decl.dim_exprs:
            dims.append(max(0, to_int(eval_expr(ctx, expr, default=0))))
        decl.dims = tuple(dims)


def _load_erb_file(program: Program, path: Path, *, debug_blocks: bool) -> None:
    file_index = program.add_file(path)
    try:
        text = read_text_auto(path)
    except Exception as exc:
        program.warnings.append(f"{path}: {exc}")
        return
    current: EraFunction | None = None
    skip_stack: list[str] = []
    pending_priority = 0
    pending_later = False
    block_parts: list[str] | None = None
    block_start = 0
    for number, raw_line in enumerate(text.splitlines(), 1):
        raw_lstrip = raw_line.lstrip()
        if raw_lstrip.startswith(";!;"):
            source_line = raw_lstrip[3:]
            line = source_line.strip()
        else:
            if raw_lstrip.startswith(";"):
                continue
            uncommented = strip_comment(raw_line)
            source_line = uncommented.lstrip()
            line = uncommented.strip()
        if not line:
            continue
        uline = line.upper()
        if uline == "[SKIPSTART]":
            skip_stack.append("SKIP")
            continue
        if uline == "[SKIPEND]":
            if skip_stack:
                skip_stack.pop()
            continue
        if uline == "[IF_DEBUG]":
            if not debug_blocks:
                skip_stack.append("IF_DEBUG")
            continue
        if uline == "[ENDIF]":
            if skip_stack and skip_stack[-1] == "IF_DEBUG":
                skip_stack.pop()
            continue
        if skip_stack:
            continue

        if block_parts is not None:
            if line == "}":
                line = " ".join(part.strip() for part in block_parts if part.strip())
                source_line = line
                number = block_start
                block_parts = None
                if not line:
                    continue
            else:
                block_parts.append(line)
                continue
        elif line == "{":
            block_parts = []
            block_start = number
            continue

        if line.startswith("#DEFINE"):
            parts = line.split(None, 2)
            if len(parts) >= 2:
                key = norm_name(parts[1])
                program.defines[key] = parts[2].strip() if len(parts) >= 3 else ""
                program.define_names[key] = parts[1]
            continue

        decl = parse_var_decl(line)
        if decl:
            # Local declarations are also useful at runtime; keep directive lines
            # inside functions, but index all declarations by name for metadata.
            _register_var_decl(program, decl, module_scope=current is None)
            if current is None:
                continue

        if line.startswith("#PRI"):
            # #PRI can appear at the top of duplicate event functions.  Emuera's
            # exact priority syntax is richer; this captures the common form.
            parts = line.split()
            try:
                pending_priority = int(parts[1]) if len(parts) > 1 else pending_priority + 1
            except ValueError:
                pending_priority += 1
            if current is not None:
                current.priority = pending_priority
            continue

        if line.startswith("#LATER"):
            if current is not None:
                current.later = True
            else:
                pending_later = True
            continue

        if line.startswith("@"):
            try:
                name, params, defaults = parse_function_header(line)
            except Exception as exc:
                program.warnings.append(f"{path}:{number}: bad function header {line!r}: {exc}")
                current = None
                continue
            current = EraFunction(
                name=name,
                params=params,
                defaults=defaults,
                source_file=file_index,
                source_line=number,
                priority=pending_priority,
                later=pending_later,
            )
            pending_priority = 0
            pending_later = False
            program.add_function(current)
            continue

        if current is None:
            continue

        if uline.startswith("#LOCALSIZE") or uline.startswith("#LOCALSSIZE"):
            parts = line.split(None, 1)
            size_expr = parts[1].strip() if len(parts) > 1 else ""
            if uline.startswith("#LOCALSSIZE"):
                current.locals_size_expr = size_expr
            else:
                current.local_size_expr = size_expr
            continue

        if uline.startswith("#FUNCTIONS"):
            current.is_function = True
            current.returns_string = True
            continue
        if uline.startswith("#FUNCTION"):
            current.is_function = True
            current.returns_string = False
            continue

        idx = len(current.lines)
        if line.startswith("$"):
            label = line[1:].strip().split()[0]
            current.labels[norm_name(label)] = idx
        source_key = norm_name(line.split(None, 1)[0]) if line.split(None, 1) else ""
        preserve_print_spacing = source_key.startswith("PRINT") or source_key == "HTML_PRINT"
        current.lines.append(
            SourceLine(
                text=source_line if preserve_print_spacing else line,
                file_index=file_index,
                number=number,
            )
        )

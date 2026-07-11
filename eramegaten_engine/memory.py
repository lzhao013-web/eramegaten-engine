from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Any, Iterable

from .csvdb import EraCsvDatabase
from .expr import Value, eval_expr, parse_lvalue, to_int, to_str
from .model import CharacterTemplate, Program, norm_name
from .csvdb import parse_era_int

NUMERIC_ARRAYS = {
    "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q",
    "R", "S", "T", "U", "V", "W", "X", "Y", "Z",
    "LOCAL", "ARG", "RESULT", "RAND", "COUNT", "FLAG", "TFLAG", "GLOBAL",
    "BASE", "MAXBASE", "ABL", "TALENT", "EXP", "EX", "MARK", "PALAM", "JUEL",
    "CFLAG", "CDFLAG", "ITEM", "EQUIP", "TEQUIP", "MONEY", "DAY", "TIME",
    "TARGET", "ASSI", "MASTER", "PLAYER", "CHARA", "CHARANUM", "NO", "SOURCE", "DOWN", "UP",
    "PALAMLV", "EXPLV", "EJAC", "CUP", "CDOWN",
    "ITEMPRICE", "ITEMSALES", "NOITEM", "LOSEBASE", "SELECTCOM", "ASSIPLAY", "PREVCOM",
    "NEXTCOM", "PBAND", "BOUGHT", "RELATION", "STAIN", "GOTJUEL", "NOWEX", "TCVAR",
    "DITEMTYPE", "DA", "DB", "DC", "DD", "DE", "TA", "TB",
}

STRING_ARRAYS = {
    "LOCALS", "ARGS", "RESULTS", "STR", "CSTR", "GLOBALS", "SAVESTR", "TSTR", "NAME", "CALLNAME", "NICKNAME", "MASTERNAME",
    "ITEMNAME", "TRAINNAME", "入力", "入力退避", "LOG", "ログ",
    "BASENAME", "ABLNAME", "TALENTNAME", "EXPNAME", "PALAMNAME", "EXNAME", "CFLAGNAME",
    "MARKNAME", "SOURCENAME", "EQUIPNAME", "TEQUIPNAME", "STAINNAME",
}

CHARA_NUMERIC_ARRAYS = {
    "BASE", "MAXBASE", "ABL", "TALENT", "EXP", "MARK", "PALAM", "SOURCE", "EX",
    "CFLAG", "CDFLAG", "JUEL", "RELATION", "EQUIP", "TEQUIP", "STAIN", "GOTJUEL",
    "NOWEX", "ISASSI", "NO",
}
CHARA_STRING_ARRAYS = {"CSTR", "NAME", "CALLNAME", "NICKNAME", "MASTERNAME"}
CHARA_SCALAR_NUMERIC_ARRAYS = {"ISASSI", "NO"}
CHARA_SCALAR_STRING_ARRAYS = {"NAME", "CALLNAME", "NICKNAME", "MASTERNAME"}
CHARA_INDEX_VARS = ("MASTER", "PLAYER", "TARGET", "ASSI")


def key_from_indices(indices: list[int]) -> tuple[int, ...]:
    return tuple(indices)


def zero_alias(idx: tuple[int, ...]) -> tuple[int, ...] | None:
    """Return the omitted-index alias for scalar/slot-0 references.

    Era variables are array-backed: a bare variable reference denotes its first
    slot, so ``RESULT`` and ``RESULT:0`` (and a character's ``CFLAG:chara`` and
    ``CFLAG:chara:0``) are two spellings of the same value.  Keep both sparse
    keys synchronized for the common one-dimensional/omitted trailing-index
    form while leaving higher-dimensional explicit indices independent.
    """

    if idx == ():
        return (0,)
    if idx == (0,):
        return ()
    return None


def table_get_alias(table: dict[tuple[int, ...], Any], idx: tuple[int, ...], default: Any) -> Any:
    if idx in table:
        return table[idx]
    alt = zero_alias(idx)
    if alt is not None and alt in table:
        return table[alt]
    if idx == ():
        # For declared multi-dimensional arrays, Emuera also treats omitted
        # indices as zeros.  Prefer the shortest all-zero materialized key.
        for dims in range(2, 5):
            zidx = (0,) * dims
            if zidx in table:
                return table[zidx]
    return default


def table_set_alias(table: dict[tuple[int, ...], Any], idx: tuple[int, ...], value: Any) -> None:
    table[idx] = value
    alt = zero_alias(idx)
    if alt is not None:
        table[alt] = value


@dataclass(slots=True)
class CharacterState:
    template_no: int = -1
    numeric: dict[str, dict[tuple[int, ...], int]] = field(default_factory=dict)
    strings: dict[str, dict[tuple[int, ...], str]] = field(default_factory=dict)

    @classmethod
    def from_template(cls, tmpl: CharacterTemplate) -> "CharacterState":
        ch = cls(template_no=tmpl.no)
        for var, values in tmpl.numeric.items():
            ch.numeric[norm_name(var)] = {(idx,): int(val) for idx, val in values.items()}
        for var, values in tmpl.strings.items():
            ch.strings[norm_name(var)] = {(idx,): str(val) for idx, val in values.items()}
        ch.numeric.setdefault("NO", {})[()] = tmpl.no
        ch.numeric.setdefault("NO", {})[(0,)] = tmpl.no
        if tmpl.name:
            ch.strings.setdefault("NAME", {})[()] = tmpl.name
            ch.strings.setdefault("NAME", {})[(0,)] = tmpl.name
        if tmpl.callname:
            ch.strings.setdefault("CALLNAME", {})[()] = tmpl.callname
            ch.strings.setdefault("CALLNAME", {})[(0,)] = tmpl.callname
        for key in ("NICKNAME", "MASTERNAME"):
            value = ch.strings.get(key, {}).get((0,), "")
            if value:
                ch.strings.setdefault(key, {})[()] = value
        return ch


@dataclass(slots=True)
class FrameMemory:
    name: str
    numeric: dict[str, dict[tuple[int, ...], int]] = field(default_factory=dict)
    strings: dict[str, dict[tuple[int, ...], str]] = field(default_factory=dict)
    dims: dict[str, tuple[int, ...]] = field(default_factory=dict)
    ref_aliases: dict[str, "RefAliasState"] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.numeric.setdefault("LOCAL", {})
        self.numeric.setdefault("ARG", {})
        self.strings.setdefault("LOCALS", {})
        self.strings.setdefault("ARGS", {})


@dataclass(slots=True)
class RefAliasState:
    base: str
    prefix: tuple[int, ...] = ()
    frame: FrameMemory | None = None
    is_string: bool = False
    is_chara: bool = False
    offset_last: bool = False
    dims: tuple[int, ...] = ()


class Memory:
    def __init__(self, program: Program):
        self.program = program
        self.csv: EraCsvDatabase | None = program.csv
        self.numeric: dict[str, dict[tuple[int, ...], int]] = {}
        self.strings: dict[str, dict[tuple[int, ...], str]] = {}
        self.frames: list[FrameMemory] = []
        self.characters: list[CharacterState] = []
        self.start_millis = int(time.time() * 1000)
        self._init_defaults()

    @property
    def frame(self) -> FrameMemory | None:
        return self.frames[-1] if self.frames else None

    def push_frame(self, name: str, args: list[Value] | None = None) -> FrameMemory:
        fr = FrameMemory(name=name)
        for i, arg in enumerate(args or []):
            if isinstance(arg, str):
                fr.strings.setdefault("ARGS", {})[(i,)] = arg
                if i == 0:
                    fr.strings.setdefault("ARGS", {})[()] = arg
            else:
                fr.numeric.setdefault("ARG", {})[(i,)] = int(arg)
                if i == 0:
                    fr.numeric.setdefault("ARG", {})[()] = int(arg)
        self.frames.append(fr)
        return fr

    def pop_frame(self) -> None:
        if self.frames:
            self.frames.pop()

    def _init_defaults(self) -> None:
        def numeric_default(name: str, value: int) -> None:
            table = self.numeric.setdefault(name, {})
            if () not in table and (0,) not in table:
                table_set_alias(table, (), value)

        def string_default(name: str, value: str) -> None:
            table = self.strings.setdefault(name, {})
            if () not in table and (0,) not in table:
                table_set_alias(table, (), value)

        # Native Emuera saves encode these scalar-like built-ins as one-element
        # arrays.  Reinitialising the empty-index alias after a load used to
        # shadow the restored ``(0,)`` value (TARGET became 0 and ASSI became
        # 1), which in turn selected the wrong portraits and character stats.
        numeric_default("RESULT", 0)
        string_default("RESULTS", "")
        numeric_default("MASTER", 0)
        numeric_default("PLAYER", 0)
        numeric_default("TARGET", 0)
        numeric_default("ASSI", 1)
        numeric_default("CHARANUM", 0)
        for i, value in enumerate([0, 100, 500, 3000, 10000, 30000, 60000, 100000, 150000, 250000, 1000000, 5000000, 30000000, 100000000, 250000000, 450000000, 650000000, 900000000]):
            self.numeric.setdefault("PALAMLV", {}).setdefault((i,), value)
        for i, value in enumerate([0, 1, 4, 20, 50, 200, 500, 1000, 1500, 2000, 3000]):
            self.numeric.setdefault("EXPLV", {}).setdefault((i,), value)
        if self.csv:
            gb = self.csv.gamebase
            gamebase_strings = {
                "GAMEBASE_TITLE": gb.get("称号", "eraMegaten"),
                "GAMEBASE_AUTHOR": gb.get("作者", ""),
                "GAMEBASE_YEAR": gb.get("製作年", ""),
                "GAMEBASE_INFO": gb.get("追加情報", ""),
            }
            for k, v in gamebase_strings.items():
                self.strings.setdefault(k, {})[()] = v
            for src, dest in {"コード": "GAMEBASE_CODE", "バージョン": "GAMEBASE_VERSION"}.items():
                try:
                    self.numeric.setdefault(dest, {})[()] = parse_era_int(gb.get(src, "0"))
                except ValueError:
                    self.numeric.setdefault(dest, {})[()] = 0
            # CSV names are symbolic indices, not initial contents of mutable
            # string arrays.  Seeding STR/SAVESTR/TSTR with those labels makes
            # an untouched SAVESTR:299 contain ``コロシアム・ランダムマッチ``;
            # VERUP_TEMP then reports a corrupt save that the original Emuera
            # correctly considers empty.  ITEMNAME/TRAINNAME and the other
            # *NAME views are resolved lazily in get_var instead.
        self._init_decl_initializers()

    def _init_decl_initializers(self) -> None:
        for key, decl in self.program.var_decls.items():
            if not decl.initial:
                continue
            if not (decl.module_scope or decl.global_scope or decl.savedata):
                continue
            key = norm_name(decl.name)
            table: dict[tuple[int, ...], Any]
            if decl.is_string:
                table = self.strings.setdefault(key, {})
                default: Value = ""
            else:
                table = self.numeric.setdefault(key, {})
                default = 0
            values: list[Value] = []
            for raw in decl.initial:
                raw = raw.strip()
                if raw:
                    value = eval_expr(self, raw, default=default)
                else:
                    value = default
                values.append(to_str(value) if decl.is_string else to_int(value))
            if len(values) == 1 and not decl.dims:
                table.setdefault((), values[0])
                table.setdefault((0,), values[0])
                continue
            for i, value in enumerate(values):
                table.setdefault((i,), value)
            if values and not decl.dims:
                table.setdefault((), values[0])

    def call_expr_function(self, name: str, args: list[Value]) -> Value:
        from .builtins import call_builtin

        built = call_builtin(self, name, args)
        if built is not None:
            return built
        return "" if norm_name(name).endswith("S") else 0

    def render_form(self, text: str) -> str:
        return text

    def warn(self, message: str) -> None:
        return None

    def has_symbol(self, name: str) -> bool:
        key = norm_name(name)
        if key in self.numeric or key in self.strings:
            return True
        if self.frame and (key in self.frame.numeric or key in self.frame.strings or key in self.frame.ref_aliases):
            return True
        if key in NUMERIC_ARRAYS or key in STRING_ARRAYS:
            return True
        if key in self.program.var_decls or key in self.program.defines:
            return True
        if self.csv and key in self.csv.constants:
            return True
        return False

    def index_segment_should_evaluate(self, name: str) -> bool:
        """Return true when an array index segment is a variable, not CSV-only.

        Bare index names such as ``TEQUIP:TARGET:下衣`` must be resolved against
        the array's own CSV namespace.  Treating every CSV constant as a scalar
        value is wrong when names are duplicated across CFLAG/TEQUIP/etc.
        """

        key = norm_name(name)
        if key in self.numeric or key in self.strings:
            return True
        if self.frame and (key in self.frame.numeric or key in self.frame.strings or key in self.frame.ref_aliases):
            return True
        if key in NUMERIC_ARRAYS or key in STRING_ARRAYS:
            return True
        if key in self.program.var_decls or key in self.program.defines:
            return True
        return False

    def has_callable(self, name: str) -> bool:
        from .builtins import BUILTINS

        return norm_name(name) in BUILTINS or bool(self.program.get_functions(name))

    def is_string_base(self, base: str) -> bool:
        alias = self._define_lvalue(base, [])
        if alias is not None:
            return self.is_string_base(alias[0])
        key = norm_name(base)
        if self.frame:
            ref_alias = self.frame.ref_aliases.get(key)
            if ref_alias is not None:
                return ref_alias.is_string
            if key in self.frame.strings:
                return True
            if key in self.frame.numeric:
                return False
        if key in STRING_ARRAYS or key in CHARA_STRING_ARRAYS:
            return True
        decl = self.program.var_decls.get(key)
        return bool(decl and decl.is_string)

    def is_chara_numeric_base(self, base: str) -> bool:
        key = norm_name(base)
        if key in CHARA_NUMERIC_ARRAYS:
            return True
        decl = self.program.var_decls.get(key)
        return bool(decl and decl.charadata and not decl.is_string)

    def is_chara_string_base(self, base: str) -> bool:
        key = norm_name(base)
        if key in CHARA_STRING_ARRAYS:
            return True
        decl = self.program.var_decls.get(key)
        return bool(decl and decl.charadata and decl.is_string)

    def is_chara_base(self, base: str) -> bool:
        return self.is_chara_numeric_base(base) or self.is_chara_string_base(base)

    def is_chara_scalar_base(self, base: str) -> bool:
        key = norm_name(base)
        if key in CHARA_SCALAR_NUMERIC_ARRAYS or key in CHARA_SCALAR_STRING_ARRAYS:
            return True
        decl = self.program.var_decls.get(key)
        return bool(decl and decl.charadata and not decl.dims)

    def resolve_indices(self, base: str, indices: list[Value]) -> list[int]:
        key = norm_name(base)
        out: list[int] = []
        # For character arrays, the first index is a character position and the
        # second is resolved through the CSV map for that variable.
        for i, seg in enumerate(indices):
            if isinstance(seg, str):
                if self.csv:
                    if i == 0 and self.is_chara_base(key) and not self.is_chara_scalar_base(key):
                        out.append(to_int(self.get_var(seg, [])) if self.has_symbol(seg) else self.csv.resolve_constant(seg, 0))
                    else:
                        out.append(self.csv.resolve_index(key, seg))
                else:
                    out.append(to_int(seg))
            else:
                out.append(int(seg))
        return out

    def _current_chara_index(self) -> int:
        return to_int(table_get_alias(self.numeric.setdefault("TARGET", {}), (), 0))

    def _resolve_chara_index_segment(self, segment: Value) -> int:
        if isinstance(segment, str):
            if self.has_symbol(segment):
                return to_int(self.get_var(segment, []))
            if self.csv:
                return self.csv.resolve_constant(segment, 0)
            return to_int(segment)
        return int(segment)

    def _resolve_chara_slot_segment(self, key: str, segment: Value) -> int:
        if isinstance(segment, str):
            if self.csv:
                return self.csv.resolve_index(key, segment)
            return to_int(segment)
        return int(segment)

    def _resolve_chara_ref(
        self,
        key: str,
        indices: list[Value],
        *,
        scalar_per_chara: bool,
    ) -> tuple[int, tuple[int, ...]]:
        """Resolve Emuera character-variable shorthand.

        Character data arrays such as ``TALENT``/``CFLAG``/``CSTR`` have a
        leading character axis plus a per-character slot axis.  Era scripts may
        omit the character axis; in that case Emuera uses the current
        ``TARGET`` and the supplied index is the slot name/number.  Scalar
        character variables such as ``NAME`` and ``NO`` keep their single index
        as the character position.
        """

        key = norm_name(key)
        if scalar_per_chara:
            chara = self._current_chara_index() if not indices else self._resolve_chara_index_segment(indices[0])
            return chara, ()
        if not indices:
            return self._current_chara_index(), ()
        if len(indices) == 1:
            return self._current_chara_index(), (self._resolve_chara_slot_segment(key, indices[0]),)
        chara = self._resolve_chara_index_segment(indices[0])
        rest = tuple(self._resolve_chara_slot_segment(key, seg) for seg in indices[1:])
        return chara, rest

    def create_ref_alias(self, base: str, indices: Iterable[Value], frame: FrameMemory | None = None) -> RefAliasState:
        key = norm_name(base)
        idx_values = list(indices)
        if frame is not None and key in frame.ref_aliases:
            parent = frame.ref_aliases[key]
            prefix = self._translate_ref_alias_indices(parent, idx_values)
            dims = self._remaining_ref_dims(parent.dims, self.resolve_indices(parent.base, idx_values))
            return RefAliasState(
                base=parent.base,
                prefix=prefix,
                frame=parent.frame,
                is_string=parent.is_string,
                is_chara=parent.is_chara,
                offset_last=parent.offset_last or bool(idx_values),
                dims=dims,
            )
        if frame is not None:
            prefix = tuple(self.resolve_indices(key, idx_values))
            is_string = key in frame.strings or (key not in frame.numeric and self.is_string_base(key))
            dims = self._remaining_ref_dims(frame.dims.get(key, ()), prefix)
            return RefAliasState(
                base=key,
                prefix=prefix,
                frame=frame,
                is_string=is_string,
                is_chara=False,
                offset_last=bool(prefix),
                dims=dims,
            )
        if self.is_chara_base(key):
            prefix = tuple(self._canonical_chara_alias_prefix(key, idx_values))
            is_string = self.is_chara_string_base(key)
            payload_prefix = prefix[1:] if prefix else ()
            dims = self._remaining_ref_dims(self._declared_array_dimensions(key, frame=None), payload_prefix)
            return RefAliasState(
                base=key,
                prefix=prefix,
                frame=None,
                is_string=is_string,
                is_chara=True,
                offset_last=len(prefix) > 1,
                dims=dims,
            )
        prefix = tuple(self.resolve_indices(key, idx_values))
        is_string = self.is_string_base(key)
        dims = self._remaining_ref_dims(self._declared_array_dimensions(key, frame=None), prefix)
        return RefAliasState(
            base=key,
            prefix=prefix,
            frame=None,
            is_string=is_string,
            is_chara=False,
            offset_last=bool(prefix),
            dims=dims,
        )

    def _canonical_chara_alias_prefix(self, key: str, indices: list[Value]) -> list[int]:
        key = norm_name(key)
        if self.is_chara_scalar_base(key):
            return [self._resolve_chara_index_segment(indices[0])] if indices else []
        target = self._current_chara_index()
        if not indices:
            return [target]
        if len(indices) == 1 and isinstance(indices[0], str):
            return [target, self._resolve_chara_slot_segment(key, indices[0])]
        chara = self._resolve_chara_index_segment(indices[0])
        return [chara, *[self._resolve_chara_slot_segment(key, seg) for seg in indices[1:]]]

    def _declared_array_dimensions(self, key: str, *, frame: FrameMemory | None) -> tuple[int, ...]:
        key = norm_name(key)
        if frame is not None and key in frame.dims:
            return tuple(max(0, int(dim)) for dim in frame.dims[key])
        decl = self.program.var_decls.get(key)
        if decl and decl.dims:
            return tuple(max(0, int(dim)) for dim in decl.dims)
        if self.csv and key in self.csv.variable_sizes:
            raw_dims = self.csv.variable_sizes[key]
            if isinstance(raw_dims, (tuple, list)):
                return tuple(max(0, int(dim)) for dim in raw_dims)
            return (max(0, int(raw_dims)),)
        return ()

    def _remaining_ref_dims(self, dims: tuple[int, ...], prefix: tuple[int, ...]) -> tuple[int, ...]:
        if not dims:
            return ()
        if not prefix:
            return tuple(max(0, int(dim)) for dim in dims)
        axis = min(len(prefix) - 1, len(dims) - 1)
        first = max(0, int(dims[axis]) - int(prefix[-1]))
        return (first, *tuple(max(0, int(dim)) for dim in dims[axis + 1 :]))

    def _translate_ref_alias_indices(self, alias: RefAliasState, indices: Iterable[Value]) -> tuple[int, ...]:
        local = tuple(self.resolve_indices(alias.base, list(indices)))
        if alias.prefix and local and alias.offset_last:
            return (*alias.prefix[:-1], alias.prefix[-1] + local[0], *local[1:])
        return (*alias.prefix, *local)

    def _get_ref_alias(self, alias: RefAliasState, indices: list[Value]) -> Value:
        idx = self._translate_ref_alias_indices(alias, indices)
        if alias.is_chara:
            if not idx:
                return "" if alias.is_string else 0
            chara, rest = idx[0], idx[1:]
            if 0 <= chara < len(self.characters):
                if alias.is_string:
                    return table_get_alias(self.characters[chara].strings.setdefault(alias.base, {}), rest, "")
                return table_get_alias(self.characters[chara].numeric.setdefault(alias.base, {}), rest, 0)
            return "" if alias.is_string else 0
        if alias.frame is not None:
            if alias.is_string:
                return table_get_alias(alias.frame.strings.setdefault(alias.base, {}), idx, "")
            return table_get_alias(alias.frame.numeric.setdefault(alias.base, {}), idx, 0)
        if alias.is_string:
            return table_get_alias(self.strings.setdefault(alias.base, {}), idx, "")
        return table_get_alias(self.numeric.setdefault(alias.base, {}), idx, 0)

    def _set_ref_alias(self, alias: RefAliasState, indices: list[Value], value: Value) -> None:
        self._set_ref_alias_source(alias, self._translate_ref_alias_indices(alias, indices), value)

    def _set_ref_alias_source(self, alias: RefAliasState, idx: tuple[int, ...], value: Value) -> None:
        if alias.is_chara:
            if not idx:
                return
            chara, rest = idx[0], idx[1:]
            self._ensure_chara(chara)
            if alias.is_string:
                table_set_alias(self.characters[chara].strings.setdefault(alias.base, {}), rest, to_str(value))
            else:
                table_set_alias(self.characters[chara].numeric.setdefault(alias.base, {}), rest, to_int(value))
            return
        if alias.frame is not None:
            if alias.is_string:
                table_set_alias(alias.frame.strings.setdefault(alias.base, {}), idx, to_str(value))
            else:
                table_set_alias(alias.frame.numeric.setdefault(alias.base, {}), idx, to_int(value))
            return
        if alias.is_string:
            table_set_alias(self.strings.setdefault(alias.base, {}), idx, to_str(value))
        else:
            table_set_alias(self.numeric.setdefault(alias.base, {}), idx, to_int(value))

    def _materialized_ref_alias_source_indices(self, alias: RefAliasState) -> list[tuple[int, ...]]:
        out: set[tuple[int, ...]] = set()
        if alias.is_chara:
            for ci, ch in enumerate(self.characters):
                table = ch.strings.get(alias.base, {}) if alias.is_string else ch.numeric.get(alias.base, {})
                for rest in table:
                    out.add((ci, *rest))
            return sorted(out)
        if alias.frame is not None:
            table = alias.frame.strings.get(alias.base, {}) if alias.is_string else alias.frame.numeric.get(alias.base, {})
            return sorted(table.keys())
        table = self.strings.get(alias.base, {}) if alias.is_string else self.numeric.get(alias.base, {})
        return sorted(table.keys())

    def _ref_alias_contains_source_idx(self, alias: RefAliasState, idx: tuple[int, ...]) -> bool:
        prefix = alias.prefix
        if not prefix:
            return True
        if alias.offset_last:
            scan_pos = len(prefix) - 1
            return len(idx) >= len(prefix) and idx[:scan_pos] == prefix[:scan_pos] and idx[scan_pos] >= prefix[-1]
        return len(idx) >= len(prefix) and idx[: len(prefix)] == prefix

    def varset_ref_alias(self, alias: RefAliasState, value: Value = 0) -> None:
        fill: Value = to_str(value) if alias.is_string else to_int(value)
        if value not in (0, "") and alias.dims:
            for idx in self._varset_dimension_indices(alias.dims):
                self._set_ref_alias(alias, list(idx), fill)
            return
        default: Value = "" if alias.is_string else 0
        touched = False
        for idx in self._materialized_ref_alias_source_indices(alias):
            if self._ref_alias_contains_source_idx(alias, idx):
                self._set_ref_alias_source(alias, idx, default if value in (0, "") else fill)
                touched = True
        if not touched and value not in (0, ""):
            self._set_ref_alias(alias, [], fill)

    def _maybe_define_alias(self, base: str) -> str:
        key = norm_name(base)
        if key in self.program.defines:
            repl = self.program.defines[key].strip()
            # Simple aliases only; expression-valued defines are handled by the
            # expression evaluator through normal lookup returning 0 if needed.
            if re_match_simple_identifier(repl):
                return repl
        return base

    def _define_lvalue(self, base: str, indices: list[Value]) -> tuple[str, list[Value]] | None:
        key = norm_name(base)
        repl = self.program.defines.get(key)
        if repl is None:
            return None
        try:
            ref = parse_lvalue(self, repl.strip())
        except Exception:
            return None
        if norm_name(ref.base) == key and not ref.indices:
            return None
        return ref.base, list(ref.indices) + list(indices)

    def get_var(self, base: str, indices: list[Value]) -> Value:
        alias = self._define_lvalue(base, indices)
        if alias is not None:
            return self.get_var(alias[0], alias[1])
        define_expr = self.program.defines.get(norm_name(base))
        if define_expr is not None and not indices:
            return eval_expr(self, define_expr, default=0)
        key = norm_name(base)
        if key == "RAND":
            limit = to_int(indices[0]) if indices else 100
            return random.randrange(max(1, limit))
        if key == "GETMILLISECOND":
            return int(time.time() * 1000) - self.start_millis
        frame_has_key = bool(self.frame and (key in self.frame.numeric or key in self.frame.strings))
        if (
            self.csv
            and not indices
            and key in self.csv.constants
            and key not in NUMERIC_ARRAYS
            and key not in STRING_ARRAYS
            and key not in self.numeric
            and key not in self.strings
            and not frame_has_key
            and key not in self.program.var_decls
        ):
            return self.csv.constants[key]
        # Local frame arrays/scalars first.
        if self.frame:
            if key in self.frame.ref_aliases:
                return self._get_ref_alias(self.frame.ref_aliases[key], indices)
            if key in self.frame.strings:
                idx = key_from_indices(self.resolve_indices(key, indices))
                return table_get_alias(self.frame.strings[key], idx, "")
            if key in self.frame.numeric:
                idx = key_from_indices(self.resolve_indices(key, indices))
                return table_get_alias(self.frame.numeric[key], idx, 0)
            if key in {"LOCAL", "ARG"}:
                idx = key_from_indices(self.resolve_indices(key, indices))
                return table_get_alias(self.frame.numeric.setdefault(key, {}), idx, 0)
            if key in {"LOCALS", "ARGS"}:
                idx = key_from_indices(self.resolve_indices(key, indices))
                return table_get_alias(self.frame.strings.setdefault(key, {}), idx, "")
        # Character arrays.  Non-scalar character variables may omit the
        # character index (e.g. TALENT:恋慕 means TALENT:TARGET:恋慕).
        if self.is_chara_numeric_base(key):
            char_index, rest = self._resolve_chara_ref(
                key,
                indices,
                scalar_per_chara=self.is_chara_scalar_base(key),
            )
            if 0 <= char_index < len(self.characters):
                return table_get_alias(self.characters[char_index].numeric.setdefault(key, {}), rest, 0)
            return 0
        if self.is_chara_string_base(key):
            char_index, rest = self._resolve_chara_ref(
                key,
                indices,
                scalar_per_chara=self.is_chara_scalar_base(key),
            )
            if 0 <= char_index < len(self.characters):
                return table_get_alias(self.characters[char_index].strings.setdefault(key, {}), rest, "")
            return ""
        if key.endswith("NAME") and indices and key not in {"NAME", "CALLNAME", "NICKNAME"}:
            map_name = {
                "BASENAME": "BASE",
                "ABLNAME": "ABL",
                "TALENTNAME": "TALENT",
                "EXPNAME": "EXP",
                "PALAMNAME": "PALAM",
                "ITEMNAME": "ITEM",
                "CFLAGNAME": "CFLAG",
                "MARKNAME": "MARK",
                "SOURCENAME": "SOURCE",
                "EQUIPNAME": "EQUIP",
                "TEQUIPNAME": "TEQUIP",
                "STAINNAME": "STAIN",
            }.get(key, key[:-4])
            idxs = self.resolve_indices(map_name, indices)
            return self.csv.name_of(map_name, idxs[0]) if self.csv and idxs else ""
        # Global strings/numbers.
        if key in self.strings or key in STRING_ARRAYS or self.is_string_base(key):
            idx = key_from_indices(self.resolve_indices(key, indices))
            return table_get_alias(self.strings.setdefault(key, {}), idx, "")
        idx = key_from_indices(self.resolve_indices(key, indices))
        return table_get_alias(self.numeric.setdefault(key, {}), idx, 0)

    def set_var(self, base: str, indices: list[Value], value: Value) -> None:
        alias = self._define_lvalue(base, indices)
        if alias is not None:
            self.set_var(alias[0], alias[1], value)
            return
        key = norm_name(base)
        is_string = self.is_string_base(key) or isinstance(value, str)
        if self.frame and key in self.frame.ref_aliases:
            self._set_ref_alias(self.frame.ref_aliases[key], indices, value)
            return
        if self.frame and key in {"LOCAL", "ARG", "LOCALS", "ARGS"}:
            idx = key_from_indices(self.resolve_indices(key, indices))
            if key in {"LOCALS", "ARGS"} or is_string:
                table_set_alias(self.frame.strings.setdefault(key, {}), idx, to_str(value))
            else:
                table_set_alias(self.frame.numeric.setdefault(key, {}), idx, to_int(value))
            return
        if self.frame and key in self.frame.numeric:
            idx = key_from_indices(self.resolve_indices(key, indices))
            table_set_alias(self.frame.numeric[key], idx, to_int(value))
            return
        if self.frame and key in self.frame.strings:
            idx = key_from_indices(self.resolve_indices(key, indices))
            table_set_alias(self.frame.strings[key], idx, to_str(value))
            return
        if self.is_chara_numeric_base(key):
            char_index, rest = self._resolve_chara_ref(
                key,
                indices,
                scalar_per_chara=self.is_chara_scalar_base(key),
            )
            self._ensure_chara(char_index)
            table_set_alias(self.characters[char_index].numeric.setdefault(key, {}), rest, to_int(value))
            return
        if self.is_chara_string_base(key):
            char_index, rest = self._resolve_chara_ref(
                key,
                indices,
                scalar_per_chara=self.is_chara_scalar_base(key),
            )
            self._ensure_chara(char_index)
            table_set_alias(self.characters[char_index].strings.setdefault(key, {}), rest, to_str(value))
            return
        idx = key_from_indices(self.resolve_indices(key, indices))
        if is_string:
            table_set_alias(self.strings.setdefault(key, {}), idx, to_str(value))
        else:
            table_set_alias(self.numeric.setdefault(key, {}), idx, to_int(value))
        if key == "CHARANUM":
            self.numeric.setdefault("CHARANUM", {})[()] = to_int(value)

    def _ensure_chara(self, index: int) -> None:
        while len(self.characters) <= index:
            self.characters.append(CharacterState())
        self.numeric.setdefault("CHARANUM", {})[()] = len(self.characters)

    def _set_chara_count(self) -> None:
        self.numeric.setdefault("CHARANUM", {})[()] = len(self.characters)

    def _void_chara(self) -> CharacterState:
        ch = CharacterState(template_no=-1)
        ch.numeric.setdefault("NO", {})[()] = -1
        ch.numeric.setdefault("NO", {})[(0,)] = -1
        return ch

    def add_chara(self, no: int) -> int:
        if self.csv and no in self.csv.characters:
            ch = CharacterState.from_template(self.csv.characters[no])
        else:
            ch = CharacterState(template_no=no)
            ch.numeric.setdefault("NO", {})[()] = no
            ch.numeric.setdefault("NO", {})[(0,)] = no
            ch.strings.setdefault("NAME", {})[()] = f"Chara{no}"
            ch.strings.setdefault("CALLNAME", {})[()] = f"Chara{no}"
        self.characters.append(ch)
        self._set_chara_count()
        return len(self.characters) - 1

    def add_sp_chara(self, no: int) -> int:
        if self.csv and no in self.csv.sp_characters:
            ch = CharacterState.from_template(self.csv.sp_characters[no])
        else:
            ch = CharacterState(template_no=no)
            ch.numeric.setdefault("NO", {})[()] = no
            ch.numeric.setdefault("NO", {})[(0,)] = no
            ch.numeric.setdefault("CFLAG", {})[(0,)] = 1
            ch.strings.setdefault("NAME", {})[()] = f"Chara{no}"
            ch.strings.setdefault("CALLNAME", {})[()] = f"Chara{no}"
        self.characters.append(ch)
        self._set_chara_count()
        return len(self.characters) - 1

    def add_void_chara(self) -> int:
        self.characters.append(self._void_chara())
        self._set_chara_count()
        return len(self.characters) - 1

    def add_chara_by_csv_file(self, file_no: int) -> int:
        """Add a character by Chara*.csv file number rather than CSV NO.

        Emuera's ADDDEFCHARA uses the number in the Chara*.csv filename
        (for example Chara01.csv for 1) when reproducing eramaker startup,
        while ADDCHARA searches by the character's `番号`/NO value.
        """

        if self.csv and file_no in self.csv.characters_by_file_no:
            ch = CharacterState.from_template(self.csv.characters_by_file_no[file_no])
        else:
            ch = self._void_chara()
        self.characters.append(ch)
        self._set_chara_count()
        return len(self.characters) - 1

    def add_default_characters(self) -> list[int]:
        """Reproduce Emuera ADDDEFCHARA's startup character insertion."""

        added = [self.add_chara_by_csv_file(0)]
        if self.csv:
            for file_no in self.csv.initial_characters:
                added.append(self.add_chara_by_csv_file(file_no))
        if len(added) >= 2:
            self.numeric.setdefault("TARGET", {})[()] = added[1]
        else:
            self.numeric.setdefault("TARGET", {})[()] = added[0]
        self.numeric.setdefault("MASTER", {})[()] = added[0]
        self.numeric.setdefault("PLAYER", {})[()] = added[0]
        return added

    def del_chara(self, index: int) -> None:
        self.del_charas([index])

    def del_charas(self, indices: list[int]) -> int:
        old_len = len(self.characters)
        targets = {i for i in indices if 0 <= i < old_len}
        if not targets:
            return 0
        new_characters: list[CharacterState] = []
        old_to_new: dict[int, int] = {}
        for old_index, ch in enumerate(self.characters):
            if old_index in targets:
                continue
            old_to_new[old_index] = len(new_characters)
            new_characters.append(ch)
        self.characters = new_characters
        self._set_chara_count()
        self.remap_character_index_vars(old_to_new, old_len=old_len)
        return len(targets)

    def del_all_charas(self) -> int:
        return self.del_charas(list(range(len(self.characters))))

    def pickup_charas(self, indices: list[int]) -> int:
        old_len = len(self.characters)
        keep = {i for i in indices if 0 <= i < old_len}
        new_characters: list[CharacterState] = []
        old_to_new: dict[int, int] = {}
        for old_index, ch in enumerate(self.characters):
            if old_index not in keep:
                continue
            old_to_new[old_index] = len(new_characters)
            new_characters.append(ch)
        self.characters = new_characters
        self._set_chara_count()
        self.remap_character_index_vars(old_to_new, old_len=old_len)
        return len(new_characters)

    def remap_character_index_vars(self, old_to_new: dict[int, int], *, old_len: int | None = None) -> None:
        old_upper = len(self.characters) if old_len is None else old_len
        for key in CHARA_INDEX_VARS:
            table = self.numeric.get(key)
            if not table:
                continue
            for idx, raw_value in list(table.items()):
                value = to_int(raw_value)
                if value in old_to_new:
                    table[idx] = old_to_new[value]
                elif 0 <= value < old_upper:
                    table[idx] = -1

    def copy_chara(self, src: int, dest: int | None = None) -> int:
        if not (0 <= src < len(self.characters)):
            return -1
        import copy
        ch = copy.deepcopy(self.characters[src])
        if dest is None or dest >= len(self.characters):
            self.characters.append(ch)
            self._set_chara_count()
            return len(self.characters) - 1
        self._ensure_chara(dest)
        self.characters[dest] = ch
        return dest

    def swap_chara(self, a: int, b: int) -> None:
        if 0 <= a < len(self.characters) and 0 <= b < len(self.characters):
            self.characters[a], self.characters[b] = self.characters[b], self.characters[a]

    def varset(self, base: str, value: Value = 0) -> None:
        key = norm_name(base)
        if self.frame:
            if key in self.frame.ref_aliases:
                self.varset_ref_alias(self.frame.ref_aliases[key], value)
                return
            if key in {"LOCAL", "ARG"} or key in self.frame.numeric:
                table = self.frame.numeric[key] = {}
                self._fill_varset_table(table, key, to_int(value), value, frame=self.frame)
                return
            if key in {"LOCALS", "ARGS"} or key in self.frame.strings:
                table = self.frame.strings[key] = {}
                self._fill_varset_table(table, key, to_str(value), value, frame=self.frame)
                return
        if self.is_string_base(key) or isinstance(value, str):
            table = self.strings[key] = {}
            self._fill_varset_table(table, key, to_str(value), value, frame=None)
        else:
            table = self.numeric[key] = {}
            self._fill_varset_table(table, key, to_int(value), value, frame=None)

    def _fill_varset_table(
        self,
        table: dict[tuple[int, ...], Any],
        key: str,
        fill: Value,
        raw_value: Value,
        *,
        frame: FrameMemory | None,
    ) -> None:
        if raw_value in (0, ""):
            return
        dims = self._varset_dimensions(key, frame=frame)
        if not dims:
            table_set_alias(table, (), fill)
            return
        for idx in self._varset_dimension_indices(dims):
            table_set_alias(table, idx, fill)

    def _varset_dimensions(self, key: str, *, frame: FrameMemory | None) -> tuple[int, ...]:
        key = norm_name(key)
        if frame is not None:
            if key in frame.ref_aliases:
                return tuple(max(0, int(dim)) for dim in frame.ref_aliases[key].dims)
            if key in frame.dims:
                return tuple(max(0, int(dim)) for dim in frame.dims[key])
        decl = self.program.var_decls.get(key)
        if decl and decl.dims:
            return tuple(max(0, int(dim)) for dim in decl.dims)
        if decl and not decl.dims and key not in NUMERIC_ARRAYS and key not in STRING_ARRAYS:
            return ()
        if self.csv and key in self.csv.variable_sizes:
            raw_dims = self.csv.variable_sizes[key]
            if isinstance(raw_dims, (tuple, list)):
                return tuple(max(0, int(dim)) for dim in raw_dims)
            return (max(0, int(raw_dims)),)
        if key in NUMERIC_ARRAYS or key in STRING_ARRAYS:
            return (1000,)
        return ()

    def _varset_dimension_indices(self, dims: tuple[int, ...]) -> Iterable[tuple[int, ...]]:
        safe_dims = tuple(max(0, min(int(dim), 1000000)) for dim in dims)
        total = 1
        for dim in safe_dims:
            total *= dim
            if total > 1000000:
                # Avoid accidentally materializing runaway arrays from malformed
                # dimensions; the normal eraMegaten sizes stay below this cap
                # for VARSET fill use.
                return iter(())
        return product(*(range(dim) for dim in safe_dims))

    def to_json_obj(self) -> dict[str, Any]:
        def enc_map(m: dict[str, dict[tuple[int, ...], Any]]) -> dict[str, dict[str, Any]]:
            return {k: {"|".join(map(str, kk)): vv for kk, vv in v.items()} for k, v in m.items()}
        return {
            "numeric": enc_map(self.numeric),
            "strings": enc_map(self.strings),
            "characters": [
                {"template_no": c.template_no, "numeric": enc_map(c.numeric), "strings": enc_map(c.strings)}
                for c in self.characters
            ],
        }

    def to_global_json_obj(self) -> dict[str, Any]:
        global_keys = {"GLOBAL", "GLOBALS"}
        for key, decl in self.program.var_decls.items():
            if decl.global_scope:
                global_keys.add(key)

        def enc_map(m: dict[str, dict[tuple[int, ...], Any]]) -> dict[str, dict[str, Any]]:
            return {k: {"|".join(map(str, kk)): vv for kk, vv in v.items()} for k, v in m.items() if k in global_keys}

        return {"numeric": enc_map(self.numeric), "strings": enc_map(self.strings), "characters": []}

    def apply_json_obj(self, data: dict[str, Any], *, overlay: bool = False) -> None:
        def dec_map(raw: dict[str, dict[str, Any]], *, string_values: bool) -> dict[str, dict[tuple[int, ...], Any]]:
            out: dict[str, dict[tuple[int, ...], Any]] = {}
            for key, values in (raw or {}).items():
                table: dict[tuple[int, ...], Any] = {}
                for idx_text, value in (values or {}).items():
                    idx = tuple(int(p) for p in idx_text.split("|") if p != "")
                    table[idx] = to_str(value) if string_values else to_int(value)
                out[norm_name(key)] = table
            return out

        nums = dec_map(data.get("numeric", {}), string_values=False)
        strs = dec_map(data.get("strings", {}), string_values=True)
        if overlay:
            self.numeric.update(nums)
            self.strings.update(strs)
        else:
            self.numeric = nums
            self.strings = strs
            self.characters = []
            for raw_ch in data.get("characters", []) or []:
                ch = CharacterState(template_no=to_int(raw_ch.get("template_no", -1)))
                ch.numeric = dec_map(raw_ch.get("numeric", {}), string_values=False)
                ch.strings = dec_map(raw_ch.get("strings", {}), string_values=True)
                self.characters.append(ch)
            self._init_defaults()
        self.numeric.setdefault("CHARANUM", {})[()] = len(self.characters)

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json_obj(), ensure_ascii=False, indent=2), encoding="utf-8")

    def load_json(self, path: str | Path, *, overlay: bool = False) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.apply_json_obj(data, overlay=overlay)


_SIMPLE_IDENT_RE = None


def re_match_simple_identifier(text: str) -> bool:
    import re
    return bool(re.match(r"^[^\s,():+\-*/%<>=!&|^?#~\"\[\]]+$", text))

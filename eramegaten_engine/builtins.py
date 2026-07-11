from __future__ import annotations

import math
import fnmatch
import gc
import html
import os
import random
import re
import sys
import time
import unicodedata
from typing import Any, Callable

from .expr import Value, parse_lvalue, to_int, to_str, truth
from .model import EraInputBlocked, norm_name, read_text_auto, strip_comment
from .csvdb import parse_era_int
from .memory import CHARA_NUMERIC_ARRAYS, CHARA_STRING_ARRAYS, NUMERIC_ARRAYS, STRING_ARRAYS


def call_builtin(ctx, name: str, args: list[Value]) -> Value | None:
    key = norm_name(name)
    fn = BUILTINS.get(key)
    if fn is None:
        return None
    marker = object()
    old = getattr(ctx, "_builtin_call_name", marker)
    try:
        setattr(ctx, "_builtin_call_name", key)
    except Exception:
        pass
    try:
        return fn(ctx, args)
    finally:
        try:
            if old is marker:
                delattr(ctx, "_builtin_call_name")
            else:
                setattr(ctx, "_builtin_call_name", old)
        except Exception:
            pass


def arg(args: list[Value], i: int, default: Value = 0) -> Value:
    return args[i] if i < len(args) else default


def b_const(ctx, args):
    name = to_str(arg(args, 0, ""))
    if ctx.program.csv:
        alias = ctx.program.csv.aliases.get(norm_name(name))
        if alias is not None:
            return alias
        return ctx.program.csv.resolve_constant(name, 0)
    return 0


def b_abs(ctx, args): return abs(to_int(arg(args, 0)))
def b_sign(ctx, args):
    value = to_int(arg(args, 0))
    return -1 if value < 0 else 1 if value > 0 else 0
def b_max(ctx, args): return max([to_int(a) for a in args] or [0])
def b_min(ctx, args): return min([to_int(a) for a in args] or [0])
def b_power(ctx, args): return int(pow(to_int(arg(args, 0)), to_int(arg(args, 1))))
def b_sqrt(ctx, args): return int(math.sqrt(max(0, to_int(arg(args, 0)))))
def b_cbrt(ctx, args):
    value = to_int(arg(args, 0))
    sign = -1 if value < 0 else 1
    n = abs(value)
    root = int(round(n ** (1.0 / 3.0)))
    while (root + 1) ** 3 <= n:
        root += 1
    while root ** 3 > n:
        root -= 1
    return sign * root
def b_log(ctx, args): return int(math.log(max(1, to_int(arg(args, 0)))))
def b_log10(ctx, args): return int(math.log10(max(1, to_int(arg(args, 0)))))
def b_exponent(ctx, args):
    try:
        return int(math.exp(to_int(arg(args, 0))))
    except OverflowError:
        return int(math.exp(709))
def b_limit(ctx, args):
    x = to_int(arg(args, 0)); lo = to_int(arg(args, 1)); hi = to_int(arg(args, 2))
    return max(lo, min(hi, x))
def b_inrange(ctx, args):
    x = to_int(arg(args, 0)); lo = to_int(arg(args, 1)); hi = to_int(arg(args, 2))
    return 1 if lo <= x <= hi else 0

def b_range(ctx, args):
    x = to_int(arg(args, 0)); lo = to_int(arg(args, 1)); hi = to_int(arg(args, 2))
    return 1 if lo <= x < hi else 0

def b_rand(ctx, args):
    if len(args) >= 2:
        lo = to_int(args[0]); hi = to_int(args[1])
        if hi <= lo:
            return lo
        return random.randrange(lo, hi)
    lim = max(1, to_int(arg(args, 0, 100)))
    return random.randrange(lim)

def b_getbit(ctx, args): return 1 if (to_int(arg(args, 0)) & (1 << to_int(arg(args, 1)))) else 0
def b_setbit(ctx, args):
    value = to_int(arg(args, 0))
    for bit_arg in args[1:]:
        bit = to_int(bit_arg)
        if bit >= 0:
            value |= 1 << bit
    return value
def b_clearbit(ctx, args):
    value = to_int(arg(args, 0))
    for bit_arg in args[1:]:
        bit = to_int(bit_arg)
        if bit >= 0:
            value &= ~(1 << bit)
    return value
def b_invertbit(ctx, args):
    value = to_int(arg(args, 0))
    for bit_arg in args[1:]:
        bit = to_int(bit_arg)
        if bit >= 0:
            value ^= 1 << bit
    return value

def b_toint(ctx, args): return to_int(arg(args, 0))
def b_tostr(ctx, args):
    value = arg(args, 0)
    if len(args) >= 2 and isinstance(args[1], str) and args[1]:
        formatted = _format_era_number(to_int(value), args[1])
        if formatted is not None:
            return formatted
    return to_str(value)

def _format_era_number(value: int, fmt: str) -> str | None:
    """Small Emuera/.NET-style numeric format subset used by eraMegaten."""
    if not fmt:
        return str(value)
    if set(fmt) <= {"0"}:
        return str(value).zfill(len(fmt))
    if re.fullmatch(r"[#,0]+", fmt) and "," in fmt:
        return f"{value:,}"
    m = re.fullmatch(r"([xX])(\d*)", fmt)
    if m:
        digits = format(abs(value), "X" if m.group(1) == "X" else "x")
        width = to_int(m.group(2)) if m.group(2) else 0
        if width > 0:
            digits = digits.zfill(width)
        return ("-" if value < 0 else "") + digits
    if fmt == "0'.'00":
        sign = "-" if value < 0 else ""
        digits = str(abs(value)).zfill(3)
        return f"{sign}{int(digits[:-2])}.{digits[-2:]}"
    try:
        return format(value, fmt)
    except Exception:
        return None

def b_isnumeric(ctx, args):
    try:
        parse_era_int(to_str(arg(args, 0)).strip())
        return 1
    except Exception:
        return 0

def _east_asian_text_encoding(ctx) -> str:
    lang = (_config_raw(ctx, "内部で使用する東アジア言語") or "").upper()
    if any(marker in lang for marker in ("CHINESE_HANS", "SIMPLIFIED", "ZH_CN", "ZH-HANS")):
        return "gbk"
    if any(marker in lang for marker in ("CHINESE_HANT", "TRADITIONAL", "ZH_TW", "ZH-HANT")):
        return "cp950"
    if any(marker in lang for marker in ("KOREAN", "KO_KR", "KO-KR")):
        return "cp949"
    return "cp932"

def _locale_strlen(ctx, text: str) -> int:
    return len(text.encode(_east_asian_text_encoding(ctx), errors="replace"))

def _unicode_strlen(text: str) -> int:
    return len(text)

def b_strlen(ctx, args): return _locale_strlen(ctx, to_str(arg(args, 0)))
def b_strlens(ctx, args): return _locale_strlen(ctx, to_str(arg(args, 0)))
def b_strlenu(ctx, args): return _unicode_strlen(to_str(arg(args, 0)))
def b_strlensu(ctx, args): return _unicode_strlen(to_str(arg(args, 0)))
def b_strlenform(ctx, args): return _locale_strlen(ctx, ctx.render_form(to_str(arg(args, 0, ""))))
def b_strlenformu(ctx, args): return _unicode_strlen(ctx.render_form(to_str(arg(args, 0, ""))))
def b_substring(ctx, args):
    s = to_str(arg(args, 0))
    start = max(0, to_int(arg(args, 1)))
    length = to_int(arg(args, 2, len(s) - start))
    # Emuera commonly uses -1 as "to the end" for SUBSTRING's length
    # argument.  eraMegaten relies on this when splitting saved comma-delimited
    # strings (e.g. CPD/STRFLAG helpers); Python's negative slice stop would
    # otherwise return an empty/truncated string.
    if length < 0:
        return s[start:]
    return s[start:start + length]
def b_substringu(ctx, args): return b_substring(ctx, args)
def b_strfind(ctx, args):
    start = max(0, to_int(arg(args, 2, 0)))
    return to_str(arg(args, 0)).find(to_str(arg(args, 1)), start)
def b_strfindu(ctx, args): return b_strfind(ctx, args)
def _expand_dotnet_replacement(template: str, match: re.Match[str]) -> str:
    out: list[str] = []
    i = 0
    while i < len(template):
        ch = template[i]
        if ch != "$" or i + 1 >= len(template):
            out.append(ch)
            i += 1
            continue
        nxt = template[i + 1]
        if nxt == "$":
            out.append("$")
            i += 2
            continue
        if nxt == "&":
            out.append(match.group(0))
            i += 2
            continue
        if nxt.isdigit():
            j = i + 1
            while j < len(template) and template[j].isdigit():
                j += 1
            group_no = int(template[i + 1 : j])
            try:
                out.append(match.group(group_no) or "")
            except IndexError:
                out.append("")
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)

def b_strcount(ctx, args):
    text = to_str(arg(args, 0))
    pattern = to_str(arg(args, 1))
    try:
        return sum(1 for _ in re.finditer(pattern, text))
    except re.error:
        return text.count(pattern)

def b_replace(ctx, args):
    text = to_str(arg(args, 0))
    pattern = to_str(arg(args, 1))
    repl = to_str(arg(args, 2))
    try:
        return re.sub(pattern, lambda m: _expand_dotnet_replacement(repl, m), text)
    except re.error:
        return text.replace(pattern, repl)
def b_unicode(ctx, args):
    try: return chr(to_int(arg(args, 0)))
    except Exception: return ""

def b_escape(ctx, args): return re.escape(to_str(arg(args, 0)))

def b_strform(ctx, args):
    text = to_str(arg(args, 0, ""))
    renderer = getattr(ctx, "render_form", None)
    if callable(renderer):
        try:
            return renderer(text)
        except Exception:
            pass
    return text

def _ref_arg_like(value: Any) -> bool:
    return hasattr(value, "base") and hasattr(value, "indices")

def _ref_arg_indices(value: Any) -> list[Value]:
    try:
        return list(getattr(value, "indices", ()))
    except Exception:
        return []

def _set_ref_arg_value(ctx, ref: Any, local_indices: list[Value], value: Value) -> None:
    memory = getattr(ctx, "memory", None)
    if memory is not None and hasattr(memory, "create_ref_alias") and hasattr(memory, "_set_ref_alias"):
        try:
            alias = memory.create_ref_alias(ref.base, _ref_arg_indices(ref), getattr(ref, "frame", None))
            memory._set_ref_alias(alias, local_indices, value)
            return
        except Exception:
            pass
    _ctx_set_var(ctx, ref.base, _ref_arg_indices(ref) + list(local_indices), value)

def _clear_results_strings(ctx) -> None:
    memory = getattr(ctx, "memory", None)
    if memory is None:
        _ctx_set_var(ctx, "RESULTS", [], "")
        return
    table = getattr(memory, "strings", {}).setdefault("RESULTS", {})
    table.clear()
    table[()] = ""
    table[(0,)] = ""

def _regexpmatch_payload(text: str, pattern: str) -> tuple[int, list[str]]:
    try:
        regex = re.compile(pattern)
    except re.error:
        return 0, []
    group_count = regex.groups + 1
    values: list[str] = []
    for match in regex.finditer(text):
        for i in range(group_count):
            try:
                values.append(match.group(i) or "")
            except IndexError:
                values.append("")
    return group_count, values

def b_regexpmatch(ctx, args):
    text = to_str(arg(args, 0, ""))
    pattern = to_str(arg(args, 1, ""))
    try:
        regex = re.compile(pattern)
    except re.error:
        return 0
    matches = list(regex.finditer(text))
    group_count, values = _regexpmatch_payload(text, pattern)

    if len(args) >= 4 and _ref_arg_like(args[2]) and _ref_arg_like(args[3]):
        _set_ref_arg_value(ctx, args[2], [], group_count)
        for i, value in enumerate(values):
            _set_ref_arg_value(ctx, args[3], [i], value)
        return len(matches)

    output = False
    if len(args) >= 3:
        third = args[2]
        if _ref_arg_like(third):
            try:
                output = truth(_ctx_get_var(ctx, third.base, _ref_arg_indices(third)))
            except Exception:
                output = False
        else:
            output = truth(third)
    if output:
        _ctx_set_var(ctx, "RESULT", [1], group_count)
        _clear_results_strings(ctx)
        for i, value in enumerate(values):
            _ctx_set_var(ctx, "RESULTS", [i], value)
    return len(matches)

def b_strjoin(ctx, args):
    ref = _dynamic_var_ref(ctx, arg(args, 0, ""))
    if ref is None:
        return ""
    sep = to_str(arg(args, 1, ",")) if len(args) >= 2 else ","
    start_arg = to_int(args[2]) if len(args) >= 3 and args[2] != "" else 0
    dims = _ctx_array_dimensions(ctx, ref.base)
    indices = list(ref.indices)
    if indices and (not dims or len(indices) >= len(dims)):
        prefix = indices[:-1]
        base_start = to_int(indices[-1])
    else:
        prefix = indices
        base_start = 0
    start = max(0, base_start + start_arg)
    if len(args) >= 4 and args[3] != "":
        end = start + max(0, to_int(args[3]))
    else:
        scan_axis = len(prefix)
        inferred = _materialized_scan_end(ctx, ref.base, prefix)
        if dims and scan_axis < len(dims) and dims != (100000,):
            end = dims[scan_axis]
        elif inferred:
            end = inferred
        elif dims and scan_axis < len(dims):
            end = dims[scan_axis]
        else:
            end = start
    if end <= start:
        return ""
    return sep.join(to_str(_ctx_get_var(ctx, ref.base, prefix + [i])) for i in range(start, end))

def _scalar_values_match(left: Value, right: Value) -> bool:
    if isinstance(left, str) or isinstance(right, str):
        if _looks_numeric(left) and _looks_numeric(right):
            return to_int(left) == to_int(right)
        return to_str(left) == to_str(right)
    return to_int(left) == to_int(right)

def b_match(ctx, args):
    """MATCH(array, needle[, start[, end]]) -> number of matching elements."""
    if len(args) < 2:
        return 0
    var = _raw_identifier_text(arg(args, 0))
    ref = _safe_lvalue(ctx, var)
    if ref is None:
        # Compatibility fallback for malformed first arguments: behave like
        # the old scalar helper instead of turning every call into a warning.
        target = arg(args, 0)
        return sum(1 for x in args[1:] if _scalar_values_match(target, x))
    needle = arg(args, 1)
    start = to_int(args[2]) if len(args) >= 3 and args[2] != "" else 0
    end = _match_scan_end(ctx, ref, args)
    if start < 0:
        start = 0
    if end < start:
        return 0
    prefix = ref.indices[:-1] if ref.indices else []
    count = 0
    for i in range(start, end):
        value = ctx.memory.get_var(ref.base, prefix + [i])
        if _find_value_matches(ctx, ref.base, value, needle):
            count += 1
    return count

def b_groupmatch(ctx, args):
    """GROUPMATCH(target, values...) -> count of values equal to target."""
    if len(args) < 2:
        return 0
    target = arg(args, 0)
    return sum(1 for x in args[1:] if _scalar_values_match(target, x))

def b_equalcheck(ctx, args):
    """eraMegaten's EQUALCHECK: boolean GROUPMATCH with trailing-zero care."""
    if len(args) < 2:
        return 0
    target = arg(args, 0)
    candidates = list(args[1:])
    if not truth(target):
        # The script version ignores omitted/default trailing zero arguments so
        # that checking for an actual zero still works when it is supplied
        # before the trailing default slots.
        while candidates and not truth(candidates[-1]):
            candidates.pop()
    return 1 if any(_scalar_values_match(target, x) for x in candidates) else 0

def b_equalcheck_turn(ctx, args):
    """EQUALCHECK_TURN: return the 1-based ordinal of the first match."""
    if len(args) < 2:
        return 0
    target = arg(args, 0)
    candidates = list(args[1:])
    if not truth(target):
        while candidates and not truth(candidates[-1]):
            candidates.pop()
    for i, value in enumerate(candidates, 1):
        if _scalar_values_match(target, value):
            return i
    return 0

def b_equalcheck_str(ctx, args):
    """String variant: empty target never matches and matches return ordinal."""
    target = to_str(arg(args, 0, ""))
    if target == "":
        return 0
    for i, value in enumerate(args[1:], 1):
        if target == to_str(value):
            return i
    return 0

def b_truecheck(ctx, args):
    return sum(1 for value in args[:20] if to_int(value) > 0)

def b_nosames(ctx, args):
    seen: list[Value] = []
    for value in args:
        if any(_scalar_values_match(value, old) for old in seen):
            return 0
        seen.append(value)
    return 1

def b_allsames(ctx, args):
    if not args:
        return 1
    first = args[0]
    return 1 if all(_scalar_values_match(first, value) for value in args[1:]) else 0


def _raw_identifier_text(text: Value) -> str:
    s = to_str(text).strip()
    if s.startswith('@"') and s.endswith('"') and len(s) >= 3:
        return s[2:-1].replace('""', '"')
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        return s[1:-1].replace('""', '"')
    return s


def b_getnum(ctx, args):
    var = _raw_identifier_text(arg(args, 0))
    name = to_str(arg(args, 1))
    return _program_csv_index(ctx, var, name)

def b_erdname(ctx, args):
    var = _raw_identifier_text(arg(args, 0))
    index = to_int(arg(args, 1, 0))
    dimension = to_int(arg(args, 2, 1)) if len(args) >= 3 and arg(args, 2, "") != "" else 1
    db = getattr(getattr(ctx, "program", None), "csv", None)
    if db is None:
        return ""
    getter = getattr(db, "erd_name_of", None)
    return getter(var, index, dimension) if callable(getter) else ""

def _csv_index(ctx, var: str, name: str) -> int:
    return _program_csv_index(ctx, var, name)

def _program_csv_index(ctx, var: str, name: str) -> int:
    db = getattr(ctx.program, "csv", None)
    if not db:
        return 0
    program = getattr(ctx, "program", None)
    key = (norm_name(var), norm_name(str(name)))
    cache = getattr(program, "_csv_index_cache", None)
    if cache is None:
        return db.resolve_index(var, name)
    if key not in cache:
        cache[key] = db.resolve_index(var, name)
    return cache[key]

def _script_index(ctx, var: str, name: str) -> int:
    """Resolve a bare CSV-name segment as the expression parser would.

    In ERB code such as ``TALENT:ARG:男性`` or ``FLAG:技能数`` the final
    segment is parsed as an identifier first.  That may resolve to a scalar,
    a #DIM name, or a bare constant before the array base gets a chance to do
    key-specific CSV lookup.  Passing a Python string directly to
    Memory.resolve_indices intentionally follows dynamic string indexing;
    hot-path helpers that mirror literal ERB segments need this variant.
    """
    try:
        should_eval = getattr(ctx, "index_segment_should_evaluate", None)
        eval_segment = bool(should_eval(name)) if callable(should_eval) else bool(hasattr(ctx, "has_symbol") and ctx.has_symbol(name))
        if eval_segment:
            return to_int(_ctx_get_var(ctx, name, []))
    except Exception:
        pass
    return _program_csv_index(ctx, var, name)

def _csv_name(ctx, var: str, index: int) -> str:
    db = getattr(ctx.program, "csv", None)
    return db.name_of(var, index) if db else ""

def _has_csv_name(ctx, var: str, name: str) -> bool:
    db = getattr(ctx.program, "csv", None)
    if not db:
        return False
    key = norm_name(var)
    cache_key = (key, norm_name(name))
    cache = getattr(getattr(ctx, "program", None), "_csv_name_presence_cache", None)
    if cache is None:
        return norm_name(name) in db.name_to_index.get(key, {}) or norm_name(f"{key}:{name}") in db.constants
    if cache_key not in cache:
        cache[cache_key] = norm_name(name) in db.name_to_index.get(key, {}) or norm_name(f"{key}:{name}") in db.constants
    return cache[cache_key]

def _offset_num(ctx, args, var: str, anchor: str) -> int | None:
    if not _has_csv_name(ctx, var, anchor):
        return None
    return _csv_index(ctx, var, to_str(arg(args, 0))) - _csv_index(ctx, var, anchor)

def _offset_name(ctx, args, var: str, anchor: str) -> str | None:
    if not _has_csv_name(ctx, var, anchor):
        return None
    return _csv_name(ctx, var, _csv_index(ctx, var, anchor) + to_int(arg(args, 0, 0)))

def b_get_basestatus(ctx, args): return _offset_name(ctx, args, "BASE", "LV")
def b_get_basestatus_num(ctx, args): return _offset_num(ctx, args, "BASE", "LV")
def b_get_battlestatus(ctx, args): return _offset_name(ctx, args, "BASE", "攻撃")
def b_get_battlestatus_num(ctx, args): return _offset_num(ctx, args, "BASE", "攻撃")
def b_get_type(ctx, args): return _offset_name(ctx, args, "BASE", "剣撃")
def b_get_type_num(ctx, args): return _offset_num(ctx, args, "BASE", "剣撃")
def b_get_state(ctx, args): return _offset_name(ctx, args, "BASE", "良好")
def b_get_state_num(ctx, args): return _offset_num(ctx, args, "BASE", "良好")
def b_get_equip(ctx, args): return _offset_name(ctx, args, "EQUIP", "剣")
def b_get_equipnum(ctx, args): return _offset_num(ctx, args, "EQUIP", "剣")
def b_get_succession(ctx, args): return _offset_name(ctx, args, "TALENT", "剣撃")
def b_get_succession_num(ctx, args): return _offset_num(ctx, args, "TALENT", "剣撃")

def _lookup_static_name(args, values: list[str]) -> str:
    idx = to_int(arg(args, 0, 0))
    return values[idx] if 0 <= idx < len(values) else ""

def b_get_ali1(ctx, args): return _lookup_static_name(args, ["", "Light", "Neutral", "Dark"])
def b_get_ali2(ctx, args): return _lookup_static_name(args, ["", "Law", "Neutral", "Chaos"])
def b_get_range(ctx, args): return _lookup_static_name(args, ["", "Ｓ", "Ｍ", "Ｌ", "Ｓ＋"])
def b_get_sphere(ctx, args): return _lookup_static_name(args, ["", "単体", "一列", "全体", "敵味方全体"])
def b_get_guntype(ctx, args): return _lookup_static_name(args, ["", "ハンドガン", "ショットガン", "機械ガン", "ライフル", "其他の銃"])

_COEFFICIENT_EXP = {42: 30, 43: 18, 8: 16, 9: 16, 10: 16, 11: 8, 12: 8, 31: 8, 32: 8, 13: 6}
_COEFFICIENT_MAG = {42: 160, 43: 48, 8: 120, 9: 120, 10: 120, 11: 220, 12: 220, 31: 220, 32: 220, 13: 400}
_COEFFICIENT_MONEY = {42: 20, 43: 14, 8: 5, 9: 5, 10: 5, 11: 12, 12: 12, 31: 12, 32: 12, 13: 2}
_COEFFICIENT_SUMMONER_RACES = {1, 2, 3, 15, 16}


def b_coefficient_exp(ctx, args):
    race = to_int(arg(args, 0, 0))
    return 20 if race in _COEFFICIENT_SUMMONER_RACES else _COEFFICIENT_EXP.get(race, 14)


def b_coefficient_mag(ctx, args):
    race = to_int(arg(args, 0, 0))
    return 96 if race in _COEFFICIENT_SUMMONER_RACES else _COEFFICIENT_MAG.get(race, 128)


def b_coefficient_money(ctx, args):
    race = to_int(arg(args, 0, 0))
    return 10 if race in _COEFFICIENT_SUMMONER_RACES else _COEFFICIENT_MONEY.get(race, 8)


def b_divergence(ctx, args):
    idx = to_int(arg(args, 0, 0))
    # Match the ERB helper's `RANGE(ARG,1,20)`: upper bound is exclusive.
    return to_int(arg(args, idx, 0)) if 1 <= idx < 20 else 0


def b_equipskillnum(ctx, args):
    return 21


def _str_scan_end(ctx) -> int:
    mem = _ctx_memory(ctx)
    table = None
    frame = getattr(mem, "frame", None)
    if frame is not None:
        table = frame.strings.get("STR") or frame.numeric.get("STR")
    table = table or getattr(mem, "strings", {}).get("STR") or getattr(mem, "numeric", {}).get("STR")
    if table:
        return max((idx[0] for idx in table if idx), default=-1) + 1
    dims = _ctx_array_dimensions(ctx, "STR")
    return min(dims[0], 10000) if dims else 10000


def _str_find(ctx, needle: str, *, complete: bool = True, start: int = 0) -> int:
    for i in range(max(0, start), max(0, _str_scan_end(ctx))):
        value = to_str(_ctx_get_var(ctx, "STR", [i]))
        if (value == needle) if complete else _find_element_matches(ctx, "STR", value, needle, complete=False):
            return i
    return -1


def _prefixed_str_tail(text: str, prefix: str, fallback_start: int) -> str:
    return text[len(prefix):] if text.startswith(prefix) else b_substring(None, [text, fallback_start])


def _script_args_returnf_map(ctx, name: str) -> dict[str, int]:
    cache_name = "_eramegaten_args_returnf_maps"
    cache = getattr(ctx, cache_name, None)
    if cache is None:
        cache = {}
        try:
            setattr(ctx, cache_name, cache)
        except Exception:
            pass
    key = norm_name(name)
    if key in cache:
        return cache[key]
    program = getattr(ctx, "program", None)
    fn = program.get_function(name) if program is not None else None
    mapping: dict[str, int] = {}
    if fn is not None:
        lines = list(getattr(fn, "lines", []))
        for pos, line in enumerate(lines[:-1]):
            text = to_str(getattr(line, "text", "")).strip()
            if not text.upper().startswith("SIF"):
                continue
            names = [
                raw.replace('""', '"')
                for raw in re.findall(r'\bARGS\b\s*==\s*"((?:[^"]|"")*)"', text, flags=re.IGNORECASE)
            ]
            if not names:
                continue
            ret = to_str(getattr(lines[pos + 1], "text", "")).strip()
            m = re.match(r"RETURNF\s+\\?\(?\s*(-?\d+)\s*\)?", ret, flags=re.IGNORECASE)
            if not m:
                continue
            value = int(m.group(1))
            for item in names:
                mapping.setdefault(norm_name(item), value)
    cache[key] = mapping
    return mapping


def _script_args_returnf_inverse(ctx, name: str) -> dict[int, str]:
    inverse: dict[int, str] = {}
    for label_key, value in _script_args_returnf_map(ctx, name).items():
        inverse.setdefault(value, label_key)
    return inverse


def _build_persona_slot_defaults() -> tuple[dict[str, int], dict[int, str]]:
    pairs: list[tuple[str, int]] = [
        ("装備状態", 0), ("NO", 1), ("LV", 2), ("ＬＶ", 2),
        ("力", 3), ("知恵", 4), ("魔力", 5), ("耐力", 6), ("体力", 6),
        ("速度", 7), ("運", 8), ("等階", 9), ("EXP", 10), ("CP", 19),
        ("変更相性1", 20), ("変更相性値1", 21), ("変更相性2", 22), ("変更相性値2", 23),
        ("変更相性3", 24), ("変更相性値3", 25), ("属性LD", 30), ("属性LC", 31),
        ("陥落", 32), ("Persona所持者", 33), ("強化可能回数", 34),
        ("潜在能力", 80), ("技能ロック設定", 81), ("変異判定フラグ", 98), ("余剰EXP", 99),
    ]
    pairs.extend((f"技能{i}", 10 + i) for i in range(1, 9))
    pairs.extend((f"習得技能{i}", 39 + i) for i in range(1, 21))
    pairs.extend((f"習得LV{i}", 59 + i) for i in range(1, 21))
    pairs.extend((f"Persona{i}", i) for i in range(1, 12))
    pairs.extend((f"ゲストPersona{i}", 20 + i) for i in range(1, 4))
    pairs.extend((f"初期Persona{i}", 49 + i) for i in range(1, 2))
    mapping: dict[str, int] = {}
    inverse: dict[int, str] = {}
    for label, value in pairs:
        mapping.setdefault(norm_name(label), value)
        inverse.setdefault(value, label)
    return mapping, inverse


PERSONA_SLOT_DEFAULTS, PERSONA_SLOT_DEFAULT_LABELS = _build_persona_slot_defaults()


def _persona_slot_num(ctx, value: Value) -> int | None:
    text = to_str(value)
    mapping = _script_args_returnf_map(ctx, "Persona")
    key = norm_name(text)
    if key in mapping:
        return mapping[key]
    if key in PERSONA_SLOT_DEFAULTS:
        return PERSONA_SLOT_DEFAULTS[key]
    anchor = _str_find(ctx, "Persona資料／装備状態", complete=True)
    if anchor >= 0:
        found = _str_find(ctx, f"Persona資料／{text}", complete=True)
        if found >= anchor:
            return found - anchor
    return None


def b_persona_slot(ctx, args):
    value = _persona_slot_num(ctx, arg(args, 0, ""))
    return value if value is not None else None


def b_get_ditemtype(ctx, args):
    inverse = _script_args_returnf_inverse(ctx, "Persona")
    slot = to_int(arg(args, 0, 0))
    if slot in inverse:
        return inverse[slot]
    if slot in PERSONA_SLOT_DEFAULT_LABELS:
        return PERSONA_SLOT_DEFAULT_LABELS[slot]
    anchor = _str_find(ctx, "Persona資料／装備状態", complete=True)
    if anchor < 0:
        return ""
    text = to_str(_ctx_get_var(ctx, "STR", [anchor + to_int(arg(args, 0, 0))]))
    return _prefixed_str_tail(text, "Persona資料／", 16)


def b_get_ditemtype_num(ctx, args):
    mapped = _persona_slot_num(ctx, arg(args, 0, ""))
    if mapped is not None:
        return mapped
    anchor = _str_find(ctx, "Persona資料／装備状態", complete=True)
    if anchor < 0:
        return 0
    return _str_find(ctx, f"Persona資料／{to_str(arg(args, 0, ''))}", complete=True) - anchor


def b_current_persona(ctx, args):
    if not _has_csv_names(ctx, "TALENT", ("Persona使",)) or not _has_csv_names(ctx, "EQUIP", ("装備Persona",)) or not _has_csv_names(ctx, "CFLAG", ("初期Personaナンバー",)):
        return None
    chara = to_int(arg(args, 0, 0))
    if not _talent(ctx, chara, "Persona使"):
        return 0
    equipped = to_int(_ctx_get_var(ctx, "EQUIP", [chara, _script_index(ctx, "EQUIP", "装備Persona")]))
    return equipped if equipped else _cflag(ctx, chara, "初期Personaナンバー")


def b_persona_data(ctx, args):
    persona = to_int(arg(args, 0, 0))
    slot = to_int(b_get_ditemtype_num(ctx, [arg(args, 1, "")]))
    return to_int(_ctx_get_var(ctx, "DITEMTYPE", [persona, slot]))


def b_equipped_persona_data(ctx, args):
    current = b_current_persona(ctx, [arg(args, 0, 0)])
    if current is None:
        return None
    return b_persona_data(ctx, [current, arg(args, 1, "")])


def b_persona_edit(ctx, args):
    persona = to_int(arg(args, 0, 0))
    slot = to_int(b_get_ditemtype_num(ctx, [arg(args, 1, "")]))
    value = to_int(arg(args, 2, 0))
    _ctx_set_var(ctx, "DITEMTYPE", [persona, slot], value)
    return value


def b_get_persona_name(ctx, args):
    if not _has_csv_names(ctx, "EQUIP", ("装備Persona",)) or not _has_csv_names(ctx, "ABL", ("初期Persona",)):
        return None
    chara = to_int(arg(args, 0, 0))
    equipped = to_int(_ctx_get_var(ctx, "EQUIP", [chara, _script_index(ctx, "EQUIP", "装備Persona")]))
    if equipped:
        no = to_int(_ctx_get_var(ctx, "DITEMTYPE", [equipped, _persona_no_slot(ctx)]))
    else:
        no = to_int(_ctx_get_var(ctx, "ABL", [chara, _script_index(ctx, "ABL", "初期Persona")]))
    return b_csvcallname(ctx, [no, 0])


def b_get_job(ctx, args):
    anchor = _str_find(ctx, "待機中、――", complete=True)
    return b_autosplit(ctx, [to_str(_ctx_get_var(ctx, "STR", [anchor + to_int(arg(args, 0, 0))])), "、", 0])


def b_get_job_omit(ctx, args):
    anchor = _str_find(ctx, "待機中、――", complete=True)
    return b_autosplit(ctx, [to_str(_ctx_get_var(ctx, "STR", [anchor + to_int(arg(args, 0, 0))])), "、", 1])


MANTRA_MAP_GRID = [
    ["NONE", "NONE", "NONE", "女神", "NONE", "NONE", "NONE", "NONE", "NONE", "NONE", "破壊神", "NONE", "NONE", "NONE", "NONE"],
    ["NONE", "NONE", "邪神", "NONE", "妃神", "NONE", "悪神", "NONE", "不動", "NONE", "天津神", "NONE", "鬼神", "NONE", "NONE"],
    ["NONE", "NONE", "幻魔", "NONE", "戦姫", "NONE", "神獣", "覇王", "NONE", "修羅王", "NONE", "鬼王", "NONE", "NONE", "NONE"],
    ["NONE", "NONE", "NONE", "秘魔", "NONE", "天女", "聖獣", "御霊五", "荒神", "猛将", "NONE", "戦鬼", "NONE", "NONE", "NONE"],
    ["NONE", "NONE", "NONE", "呪魔", "夜魔", "御霊四", "巫女", "魔獣", "御霊三", "修羅漢", "闘鬼", "NONE", "NONE", "NONE", "NONE"],
    ["NONE", "地神王", "地母神", "地祗", "秘言五", "業魔", "比丘尼", "秘言ニ", "沙門", "業鬼", "秘言六", "飛仙", "風神", "風神王", "NONE"],
    ["NONE", "NONE", "NONE", "地王", "地之祠", "御霊ニ", "精霊", "修羅", "御霊一", "飛天", "天龍", "NONE", "NONE", "NONE", "NONE"],
    ["熾天使", "智天使", "座天使", "大天使", "秘言七", "地之宮", "地霊", "喰奴", "飛龍", "龍王", "秘言八", "魔将", "魔帥", "魔帝", "魔王"],
    ["NONE", "NONE", "NONE", "夜叉", "天使", "雷仙", "護法", "氷妖", "氷隠", "氷将", "氷狼", "NONE", "NONE", "NONE", "NONE"],
    ["NONE", "死神", "死霊", "悪霊", "御霊八", "雷王", "英霊", "秘言一", "炎魔", "金剛", "御霊六", "氷帥", "氷神", "氷神王", "NONE"],
    ["NONE", "NONE", "NONE", "雷帝", "真雷", "秘言三", "威霊", "大炎魔", "秘言四", "吉祥", "明王", "NONE", "NONE", "NONE", "NONE"],
    ["NONE", "NONE", "NONE", "万雷仙", "NONE", "和霊", "黄泉", "御霊七", "炎魔将", "炎帝", "NONE", "神将", "NONE", "NONE", "NONE"],
    ["NONE", "NONE", "雷神", "NONE", "祖霊", "NONE", "六道", "三界", "NONE", "絶焔", "NONE", "成道", "NONE", "NONE", "NONE"],
    ["NONE", "NONE", "雷神王", "NONE", "冥霊", "NONE", "討竜", "NONE", "光神", "NONE", "火神", "NONE", "無色", "NONE", "NONE"],
    ["NONE", "NONE", "NONE", "魔神", "NONE", "NONE", "NONE", "NONE", "NONE", "NONE", "火神王", "NONE", "NONE", "NONE", "NONE"],
]


def _mantra_anchor(ctx) -> int:
    anchor = _str_find(ctx, "真言／喰奴", complete=True)
    if anchor >= 0:
        return anchor
    db = getattr(getattr(ctx, "program", None), "csv", None)
    if db is not None:
        for idx, text in getattr(db, "index_to_name", {}).get("STR", {}).items():
            if text == "真言／喰奴":
                return int(idx)
    return -1


def _mantra_str_name(ctx, index: int) -> str:
    anchor = _mantra_anchor(ctx)
    if anchor < 0:
        return ""
    text = to_str(_ctx_get_var(ctx, "STR", [anchor + index - 1]))
    if text:
        return text
    db = getattr(getattr(ctx, "program", None), "csv", None)
    if db is not None:
        return to_str(getattr(db, "index_to_name", {}).get("STR", {}).get(anchor + index - 1, ""))
    return ""


def b_get_mantra(ctx, args):
    text = _mantra_str_name(ctx, to_int(arg(args, 0, 0)))
    return _prefixed_str_tail(text, "真言／", 10) if text else ""


def b_get_mantra_num(ctx, args):
    name = to_str(arg(args, 0, ""))
    if name == "NONE" or not name:
        return 0
    anchor = _mantra_anchor(ctx)
    if anchor < 0:
        return 0
    found = _str_find(ctx, f"真言／{name}", complete=True)
    if found < 0:
        db = getattr(getattr(ctx, "program", None), "csv", None)
        if db is not None:
            for idx, text in getattr(db, "index_to_name", {}).get("STR", {}).items():
                if text == f"真言／{name}":
                    found = int(idx)
                    break
    return found - anchor + 1 if found >= anchor else 0


def b_get_mantra_mapname(ctx, args):
    x = to_int(arg(args, 0, 0))
    y = to_int(arg(args, 1, 0))
    if not (0 <= x < 15 and 0 <= y < 15):
        _ctx_set_var(ctx, "RESULT", [1], -1)
        return "NONE"
    _ctx_set_var(ctx, "RESULT", [1], 0)
    return MANTRA_MAP_GRID[y][x]


def b_get_race(ctx, args):
    anchor = _str_find(ctx, "人間", complete=False)
    return to_str(_ctx_get_var(ctx, "STR", [to_int(arg(args, 0, 0)) - anchor]))


def b_get_race_num(ctx, args):
    return _str_find(ctx, to_str(arg(args, 0, "")), complete=False) - _str_find(ctx, "人間", complete=False)


def b_race_name(ctx, args):
    if not _has_csv_name(ctx, "ABL", "種族"):
        return None
    chara = to_int(arg(args, 0, 0))
    custom = to_str(_ctx_get_var(ctx, "CSTR", [chara, _script_index(ctx, "CSTR", "種族名")]))
    if custom != "":
        return custom
    race = to_int(_ctx_get_var(ctx, "ABL", [chara, _script_index(ctx, "ABL", "種族")]))
    return to_str(_ctx_get_var(ctx, "STR", [race]))


def b_csv_race_name(ctx, args):
    if not _has_csv_name(ctx, "ABL", "種族"):
        return None
    no = to_int(arg(args, 0, 0))
    sp = to_int(arg(args, 1, 0))
    cstr_idx = _csv_index(ctx, "CSTR", "種族名")
    custom = b_csvcstr(ctx, [no, cstr_idx, sp])
    if to_str(custom) != "":
        return to_str(custom)
    race = to_int(b_csvabl(ctx, [no, _csv_index(ctx, "ABL", "種族"), sp]))
    return to_str(_ctx_get_var(ctx, "STR", [race]))


def b_get_damage_type(ctx, args):
    return _lookup_static_name(args, ["不正な引数が与えられました", "物理", "魔法"]) or "不正な引数が与えられました"


def b_get_damage_type_num(ctx, args):
    name = to_str(arg(args, 0, ""))
    return 1 if name == "物理" else 2 if name == "魔法" else -1


def b_get_attack_type(ctx, args):
    return _lookup_static_name(args, ["不正な引数が与えられました", "物理", "魔法", "銃", "物品", "割合"]) or "不正な引数が与えられました"


def b_get_attack_type_num(ctx, args):
    name = to_str(arg(args, 0, ""))
    if name == "物理":
        return 1
    if name == "魔法":
        return 2
    if name in {"銃", "GUN", "ＧＵＮ"}:
        return 3
    if name in {"物品", "道具"}:
        return 4
    if name in {"割合", "割合傷害"}:
        return 5
    return -1


def b_get_ex(ctx, args):
    query = to_str(arg(args, 0, ""))
    values = [to_int(_ctx_get_var(ctx, "NOWEX", [i])) for i in range(4)]
    if all(value > 0 for value in values):
        values = [int(value / 8) for value in values]
        if query in {"4重", "４重", "四重"}:
            return 1
    elif (
        (values[0] > 0 and values[1] > 0 and values[2] > 0)
        or (values[0] > 0 and values[1] > 0 and values[3] > 0)
        or (values[0] > 0 and values[2] > 0 and values[3] > 0)
        or (values[1] > 0 and values[2] > 0 and values[3] > 0)
    ):
        values = [int(value / 4) for value in values]
        if query in {"3重", "３重", "三重"}:
            return 1
    elif (
        (values[0] > 0 and values[1] > 0)
        or (values[0] > 0 and values[2] > 0)
        or (values[0] > 0 and values[3] > 0)
        or (values[0] > 1 and values[2] > 0)
        or (values[0] > 1 and values[3] > 0)
        or (values[0] > 2 and values[3] > 0)
    ):
        values = [int(value / 2) for value in values]
        if query in {"2重", "２重", "ニ重"}:
            return 1
    if query in {"C", "Ｃ"}:
        return values[0]
    if query in {"V", "Ｖ"}:
        return values[1]
    if query in {"A", "Ａ"}:
        return values[2]
    if query in {"B", "Ｂ"}:
        return values[3]
    return 0


def b_palamlv_f(ctx, args):
    chara = to_int(arg(args, 0, 0))
    slot = arg(args, 1, 0)
    return b_getpalamlv(ctx, [_ctx_get_var(ctx, "PALAM", [chara, slot]), 18])


def b_pluginname(ctx, args):
    no = to_int(arg(args, 0, 0))
    if not (8000 <= no < 8500):
        _strflag_throw(ctx, "ARGが不正です。確認してください")
        return None
    name = to_str(_ctx_get_var(ctx, "ITEMNAME", [no]))
    prefix = "プラグイン／"
    return name[len(prefix):] if name.startswith(prefix) else b_substring(ctx, [name, 12])

_STAIN_BODY_SLOTS = {
    "口": 0,
    "手": 1,
    "陰茎": 2,
    "陰道": 3,
    "肛門": 4,
    "胸": 5,
    "膣内": 6,
    "髪": 7,
}

_STAIN_BITS = {
    "愛液": 0,
    "陰茎": 1,
    "精液": 2,
    "肛門": 3,
    "母乳": 4,
    "粘液": 5,
    "破瓜の血": 6,
}

_NON_DEVIL_RACES = {0, 36, 45}
_GET_SUMMONER_MLV_BAD_STATES = {"瀕死", "石化", "飛翔", "麻痺", "束縛", "魅惑", "休眠", "恐慌"}
_ACTIONABLE_BAD_STATES = {"瀕死", "石化", "麻痺", "束縛", "凍結", "休克", "休眠", "灼熱", "跌倒"}
_BADSTATE_ALIASES = {
    "良好": "良好",
    "幸福": "幸福",
    "休克": "休克",
    "感電": "休克",
    "恐慌": "恐慌",
    "混乱": "恐慌",
    "休眠": "休眠",
    "睡眠": "休眠",
    "眠り": "休眠",
    "凍結": "凍結",
    "束縛": "束縛",
    "金縛り": "束縛",
    "金縛": "束縛",
    "沈默": "沈默",
    "沈黙": "沈默",
    "中毒": "中毒",
    "毒": "中毒",
    "猛毒": "中毒",
    "爆炸": "爆炸",
    "爆弾": "爆炸",
    "魅惑": "魅惑",
    "魅了": "魅惑",
    "麻痺": "麻痺",
    "詛咒": "詛咒",
    "呪い": "詛咒",
    "飛翔": "飛翔",
    "蝿化": "飛翔",
    "石化": "石化",
    "瀕死": "瀕死",
    "即死": "瀕死",
    "呪殺": "瀕死",
    "昇天": "瀕死",
    "死亡": "瀕死",
    "狂暴": "狂暴",
    "オルギア": "狂暴",
    "灼熱": "灼熱",
    "席特": "灼熱",
    "燃烧": "燃烧",
    "炎上": "燃烧",
    "跌倒": "跌倒",
    "転倒": "跌倒",
    "烙印": "烙印",
}

_FALLEN_STAGE3 = {"妻", "夫", "淫魔", "玩具", "盟友"}
_FALLEN_STAGE2 = {"親愛", "娼婦", "隷属", "相棒"}
_FALLEN_STAGE1 = {"恋慕", "淫乱", "服従", "信頼"}
_FALLEN_ROUTE_GROUPS = [
    ("恋慕", ("妻", "夫", "親愛", "恋慕")),
    ("淫乱", ("淫魔", "娼婦", "淫乱")),
    ("服従", ("玩具", "隷属", "服従")),
    ("信頼", ("盟友", "相棒", "信頼")),
]
_FALLEN_REQUIRED_TALENTS = (
    "妻", "夫", "親愛", "恋慕",
    "淫魔", "娼婦", "淫乱",
    "玩具", "隷属", "服従",
    "盟友", "相棒", "信頼", "ＮＴＲ",
)


def _has_csv_names(ctx, var: str, names) -> bool:
    return all(_has_csv_name(ctx, var, name) for name in names)


def _target_or_arg(ctx, value: Value, omitted: int = -1) -> int:
    chara = to_int(value)
    return to_int(_ctx_get_var(ctx, "TARGET", [])) if chara == omitted else chara


def _master(ctx) -> int:
    return to_int(_ctx_get_var(ctx, "MASTER", []))


def _talent(ctx, chara: int, name: str) -> int:
    return to_int(_ctx_get_var(ctx, "TALENT", [chara, _script_index(ctx, "TALENT", name)]))


def _cflag(ctx, chara: int, name: str) -> int:
    return to_int(_ctx_get_var(ctx, "CFLAG", [chara, _script_index(ctx, "CFLAG", name)]))


def _set_results(ctx, value: str) -> None:
    _ctx_set_var(ctx, "RESULTS", [], value)


def _fallen_requirements(ctx, *, relation: bool = False) -> bool:
    if not _has_csv_names(ctx, "TALENT", _FALLEN_REQUIRED_TALENTS):
        return False
    if relation and not _has_csv_names(ctx, "CFLAG", ("陥落キャラ", "キャラ固有の番号")):
        return False
    return True


def _fallen_stage(ctx, chara: int) -> int:
    if any(_talent(ctx, chara, name) for name in _FALLEN_STAGE3):
        return 3
    if any(_talent(ctx, chara, name) for name in _FALLEN_STAGE2):
        return 2
    if any(_talent(ctx, chara, name) for name in _FALLEN_STAGE1):
        return 1
    return 0


def _fallen_route_from_talents(ctx, chara: int) -> str:
    for route, names in _FALLEN_ROUTE_GROUPS:
        if any(_talent(ctx, chara, name) for name in names):
            return route
    return ""


def _fallen_link_matches(ctx, chara: int, other: int) -> bool:
    return _cflag(ctx, chara, "陥落キャラ") == _cflag(ctx, other, "キャラ固有の番号")


def _fallen_relationship(ctx, route: str, stage: int, chara: int, other: int) -> int:
    master = _master(ctx)
    ntr = _talent(ctx, chara, "ＮＴＲ")
    if route == "恋慕":
        if other == master:
            return 1 if not ntr and _talent(ctx, chara, "恋慕") else 0
        return 1 if ntr == 1 and _fallen_stage(ctx, chara) == 1 and _fallen_link_matches(ctx, chara, other) else 0
    if route == "親愛":
        if other == master:
            return 1 if not ntr and _talent(ctx, chara, "親愛") else 0
        return 1 if ntr == 1 and _fallen_stage(ctx, chara) >= 2 and _fallen_link_matches(ctx, chara, other) else 0
    if route == "淫乱":
        return 1 if (ntr == 2 and _fallen_stage(ctx, chara) == 1) or (_talent(ctx, chara, "淫乱") and ntr == 0) else 0
    if route == "娼婦":
        return 1 if (ntr == 2 and _fallen_stage(ctx, chara) > 1) or (_talent(ctx, chara, "娼婦") and ntr == 0) else 0
    if route == "服従":
        if other == master:
            return 1 if not ntr and _talent(ctx, chara, "服従") else 0
        return 1 if ntr == 3 and _fallen_stage(ctx, chara) == 1 and _fallen_link_matches(ctx, chara, other) else 0
    if route == "隷属":
        if other == master:
            return 1 if not ntr and _talent(ctx, chara, "隷属") else 0
        return 1 if ntr == 3 and _fallen_stage(ctx, chara) >= 2 and _fallen_link_matches(ctx, chara, other) else 0
    if route == "信頼":
        return 1 if other == master and not ntr and _talent(ctx, chara, "信頼") else 0
    if route == "相棒":
        return 1 if other == master and not ntr and _talent(ctx, chara, "相棒") else 0
    return 0


def b_fallen(ctx, args):
    second = to_int(arg(args, 1, -1))
    if not _fallen_requirements(ctx, relation=second != -1):
        return None
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    if second == -1:
        _set_results(ctx, _fallen_route_from_talents(ctx, chara))
        return _fallen_stage(ctx, chara)
    route = ""
    if _fallen_relationship(ctx, "恋慕", 1, chara, second) or _fallen_relationship(ctx, "親愛", 2, chara, second):
        route = "恋慕"
    elif _fallen_relationship(ctx, "淫乱", 1, chara, second) or _fallen_relationship(ctx, "娼婦", 2, chara, second):
        route = "淫乱"
    elif _fallen_relationship(ctx, "服従", 1, chara, second) or _fallen_relationship(ctx, "隷属", 2, chara, second):
        route = "服従"
    elif _fallen_relationship(ctx, "信頼", 1, chara, second) or _fallen_relationship(ctx, "相棒", 2, chara, second):
        route = "信頼"
    else:
        _set_results(ctx, "")
        return 0
    _set_results(ctx, route)
    return _fallen_stage(ctx, chara)


def _relationship_builtin(route: str):
    def fn(ctx, args):
        if not _fallen_requirements(ctx, relation=route not in {"淫乱", "娼婦"}):
            return None
        chara = _target_or_arg(ctx, arg(args, 0, -1))
        other = to_int(arg(args, 1, -1))
        if other == -1:
            other = _master(ctx)
        return _fallen_relationship(ctx, route, 0, chara, other)
    return fn


b_renbo = _relationship_builtin("恋慕")
b_shinai = _relationship_builtin("親愛")
b_inran = _relationship_builtin("淫乱")
b_shoufu = _relationship_builtin("娼婦")
b_fukujuu = _relationship_builtin("服従")
b_reizoku = _relationship_builtin("隷属")
b_shinrai = _relationship_builtin("信頼")
b_aibou = _relationship_builtin("相棒")


def b_heart(ctx, args):
    return "\u2661" * max(0, to_int(arg(args, 0, 1)))


def b_heart_b(ctx, args):
    return "\u2665" * max(0, to_int(arg(args, 0, 1)))


_COMTYPE_NAMES = [
    "愛撫系", "コミュ系", "道具系", "対調教者道具系", "Ｖ性交系", "Ａ性交系",
    "奉仕系", "調教者奉仕系", "ＳＭ系", "助手・レズ系", "ハード系", "触手系", "特殊指令系",
]


def b_comtype(ctx, args):
    needle = to_str(arg(args, 0, ""))
    try:
        return _COMTYPE_NAMES.index(needle)
    except ValueError:
        return -1


def b_is_male(ctx, args):
    if not _has_csv_name(ctx, "TALENT", "男性"):
        return None
    return 1 if _talent(ctx, _target_or_arg(ctx, arg(args, 0, -1)), "男性") else 0


def b_is_lookslike_male(ctx, args):
    if not _has_csv_names(ctx, "TALENT", ("男性", "偽娘")):
        return None
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    return 1 if _talent(ctx, chara, "男性") and not _talent(ctx, chara, "偽娘") else 0


def b_have_penis(ctx, args):
    if not _has_csv_names(ctx, "TALENT", ("男性", "FUTA")):
        return None
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    return 1 if _talent(ctx, chara, "男性") or _talent(ctx, chara, "FUTA") else 0


def b_is_lesbian(ctx, args):
    if not _has_csv_name(ctx, "TALENT", "男性"):
        return None
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    other = to_int(arg(args, 1, 0))
    return 1 if not _talent(ctx, chara, "男性") and not _talent(ctx, other, "男性") else 0


def b_is_gay(ctx, args):
    if not _has_csv_names(ctx, "TALENT", ("男性", "偽娘")) or not _has_csv_name(ctx, "FLAG", "偽娘ＢＬ設定"):
        return None
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    other = to_int(arg(args, 1, 0))
    male_a = bool(_talent(ctx, chara, "男性"))
    male_b = bool(_talent(ctx, other, "男性"))
    fake_a = bool(_talent(ctx, chara, "偽娘"))
    fake_b = bool(_talent(ctx, other, "偽娘"))
    mode = _flag(ctx, "偽娘ＢＬ設定")
    if mode == 0:
        return 1 if male_a and not fake_a and male_b and not fake_b else 0
    if mode == 1:
        return 1 if male_a and male_b else 0
    if mode == 2:
        return 1 if male_a and male_b and (not fake_a or not fake_b) else 0
    return 0


def b_have_clitoris(ctx, args):
    if not _has_csv_names(ctx, "TALENT", ("男性", "FUTA")):
        return None
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    return 1 if not _talent(ctx, chara, "男性") and not _talent(ctx, chara, "FUTA") else 0


def b_have_vagina(ctx, args):
    if not _has_csv_name(ctx, "TALENT", "男性"):
        return None
    return 1 if not _talent(ctx, _target_or_arg(ctx, arg(args, 0, -1)), "男性") else 0


def b_have_tit(ctx, args):
    if not _has_csv_names(ctx, "TALENT", ("男性", "偽娘")):
        return None
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    return 1 if not _talent(ctx, chara, "男性") or _talent(ctx, chara, "偽娘") else 0


def b_is_lover(ctx, args):
    if not _has_csv_names(ctx, "TALENT", ("恋慕", "親愛", "陥落履歴(親愛)")):
        return None
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    return 1 if _talent(ctx, chara, "恋慕") or _talent(ctx, chara, "親愛") or _talent(ctx, chara, "陥落履歴(親愛)") else 0


def b_is_bitchy(ctx, args):
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    return 1 if to_int(_ctx_get_var(ctx, "TALENT", [chara, 4])) or to_int(_ctx_get_var(ctx, "TALENT", [chara, 7])) else 0


def b_is_slavery(ctx, args):
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    return 1 if to_int(_ctx_get_var(ctx, "TALENT", [chara, 5])) or to_int(_ctx_get_var(ctx, "TALENT", [chara, 8])) else 0


def b_is_engage(ctx, args):
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    return 1 if to_int(_ctx_get_var(ctx, "TALENT", [chara, 170])) or to_int(_ctx_get_var(ctx, "TALENT", [chara, 171])) else 0


def b_is_beast(ctx, args):
    chara = to_int(arg(args, 0, 0))
    return 1 if any(to_int(_ctx_get_var(ctx, "TALENT", [chara, idx])) for idx in range(213, 218)) else 0


def b_is_human(ctx, args):
    if not _has_csv_name(ctx, "ABL", "種族"):
        return None
    race = to_int(_ctx_get_var(ctx, "ABL", [to_int(arg(args, 0, 0)), _script_index(ctx, "ABL", "種族")]))
    return 1 if race in {0, 36} else 0


def b_xgender(ctx, args):
    names = ("男性", "女性", "中性", "雄性", "雌性", "双性", "無性")
    if not _has_csv_names(ctx, "TALENT", names):
        return None
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    return 1 if any(_talent(ctx, chara, name) for name in names) else 0


def b_hate_male(ctx, args):
    if not _has_csv_names(ctx, "TALENT", ("討厭男人", "男性", "恋慕", "親愛", "陥落履歴(親愛)")):
        return None
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    actor = to_int(arg(args, 1, 0))
    master = _master(ctx)
    lover = bool(b_is_lover(ctx, [chara]))
    return 1 if _talent(ctx, chara, "討厭男人") and b_is_male(ctx, [actor]) and not (lover and actor == master and not b_is_male(ctx, [chara])) else 0


def b_hate_female(ctx, args):
    if not _has_csv_names(ctx, "TALENT", ("討厭女人", "男性", "恋慕", "親愛", "陥落履歴(親愛)")):
        return None
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    actor = to_int(arg(args, 1, 0))
    master = _master(ctx)
    lover = bool(b_is_lover(ctx, [chara]))
    return 1 if _talent(ctx, chara, "討厭女人") and not b_is_male(ctx, [actor]) and not (lover and actor == master and b_is_male(ctx, [chara])) else 0


def b_hate(ctx, args):
    male = b_hate_male(ctx, args)
    female = b_hate_female(ctx, args)
    if male is None or female is None:
        return None
    _ctx_set_var(ctx, "RESULT", [1], 82 if male else 88)
    return 1 if male or female else 0


def b_harmonizer_output(ctx, args):
    flag_names = tuple(f"ポジション{i}" for i in range(1, 7))
    if (
        not _has_csv_names(ctx, "FLAG", flag_names)
        or not _has_csv_name(ctx, "CFLAG", "ステート")
        or not _has_csv_name(ctx, "TALENT", "召喚師")
    ):
        return None
    total = 0
    blocked_states = {"瀕死", "石化", "飛翔", "麻痺", "束縛", "魅惑", "休眠", "恐慌"}
    for pos in range(1, 7):
        chara = to_int(_ctx_get_var(ctx, "FLAG", [f"ポジション{pos}"]))
        if chara <= -1:
            continue
        state = b_get_state(ctx, [_cflag(ctx, chara, "ステート")])
        if state in blocked_states:
            continue
        summoner = _talent(ctx, chara, "召喚師")
        if summoner > 4:
            total += 30
        elif summoner > 2:
            total += 20
        elif summoner > 0:
            total += 15
    if total > 60:
        total = 60
    if total < 1:
        total = 1
    return total


def b_body_size(ctx, args):
    if not _has_csv_names(ctx, "TALENT", ("小人体型", "体型嬌小", "高大", "巨人")):
        return None
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    if _talent(ctx, chara, "小人体型"):
        return -10
    if _talent(ctx, chara, "体型嬌小"):
        return -1
    if _talent(ctx, chara, "高大"):
        return 1
    if _talent(ctx, chara, "巨人"):
        return 10
    return 0


def b_body_size_diff(ctx, args):
    a = _target_or_arg(ctx, arg(args, 0, -1))
    b = _master(ctx) if to_int(arg(args, 1, -1)) == -1 else to_int(arg(args, 1, -1))
    first = b_body_size(ctx, [b])
    second = b_body_size(ctx, [a])
    if first is None or second is None:
        return None
    return first - second


def b_initial_gender(ctx, args):
    if not _has_csv_name(ctx, "EXP", "ＴＳ経験"):
        return None
    return to_int(_ctx_get_var(ctx, "EXP", [to_int(arg(args, 0, 0)), _script_index(ctx, "EXP", "ＴＳ経験")])) % 2


_PURE_SPECIAL_TALENTS = ("召喚師", "Persona使", "喰奴", "悪魔変身", "達人", "悪魔憑依", "Aion式召喚術")


def b_pure_innate(ctx, args):
    if not _has_csv_names(ctx, "TALENT", ("異能者",) + _PURE_SPECIAL_TALENTS):
        return None
    chara = to_int(arg(args, 0, 0))
    if not _talent(ctx, chara, "異能者"):
        return 0
    if _talent(ctx, chara, "召喚師") > 0:
        return 2
    for name in _PURE_SPECIAL_TALENTS[1:]:
        if _talent(ctx, chara, name):
            return 2
    return 1


def b_pure_tatsujin(ctx, args):
    names = ("達人", "召喚師", "Persona使", "喰奴", "悪魔変身", "異能者", "悪魔憑依", "Aion式召喚術")
    if not _has_csv_names(ctx, "TALENT", names):
        return None
    chara = to_int(arg(args, 0, 0))
    if not _talent(ctx, chara, "達人"):
        return 0
    if _talent(ctx, chara, "召喚師") > 0:
        return 0
    for name in names[2:]:
        if _talent(ctx, chara, name):
            return 0
    return 1


def b_chastity(ctx, args):
    if not _has_csv_names(ctx, "TALENT", _FALLEN_REQUIRED_TALENTS + ("不在乎貞操", "貞操観念")):
        return None
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    other = _master(ctx) if to_int(arg(args, 1, -1)) == -1 else to_int(arg(args, 1, -1))
    if _talent(ctx, chara, "不在乎貞操"):
        return 0
    fallen = b_fallen(ctx, [chara])
    if fallen is None:
        return None
    route = to_str(_ctx_get_var(ctx, "RESULTS", []))
    bonus = _talent(ctx, chara, "貞操観念")
    if fallen and route == "恋慕":
        rel = b_fallen(ctx, [chara, other])
        if rel is None:
            return None
        return 1 + bonus if rel else -1 - bonus
    if route not in {"淫乱", "服従"}:
        spouse = to_str(_ctx_get_var(ctx, "CSTR", [chara, "配偶者"]))
        if spouse != "":
            return 1 + bonus if b_csv_spouse(ctx, [chara, other]) else -1 - bonus
    return 0


def _tequip(ctx, chara: int, segment: int | str) -> int:
    idx = segment if isinstance(segment, int) else _script_index(ctx, "TEQUIP", segment)
    return to_int(_ctx_get_var(ctx, "TEQUIP", [chara, idx]))


def _item(ctx, segment: int | str) -> int:
    idx = segment if isinstance(segment, int) else _script_index(ctx, "ITEM", segment)
    return to_int(_ctx_get_var(ctx, "ITEM", [idx]))


def _tcvar(ctx, chara: int, segment: int | str) -> int:
    idx = segment if isinstance(segment, int) else _script_index(ctx, "TCVAR", segment)
    return to_int(_ctx_get_var(ctx, "TCVAR", [chara, idx]))


def b_get_add_exp(ctx, args):
    target = to_int(arg(args, 2, -99))
    if target == -99:
        target = to_int(_ctx_get_var(ctx, "TARGET", []))
    return _tcvar(ctx, target, to_int(arg(args, 0, 0)))


def b_gets_add_exp(ctx, args):
    if not _has_csv_name(ctx, "EXP", to_str(arg(args, 0, ""))):
        return None
    exp_no = _csv_index(ctx, "EXP", to_str(arg(args, 0, "")))
    return b_get_add_exp(ctx, [exp_no, arg(args, 1, 0), arg(args, 2, -99)])


def b_item_anus(ctx, args):
    chara = to_int(arg(args, 0, 0))
    for slot, ret in ((14, 1), (15, 2), (21, 4), (22, 8), (23, 16), (41, 1)):
        if _tequip(ctx, chara, slot):
            return ret
    return 0


def b_item_foot(ctx, args):
    return 1 if _tequip(ctx, to_int(arg(args, 0, 0)), 19) else 0


def b_item_hand(ctx, args):
    return 1 if _tequip(ctx, to_int(arg(args, 0, 0)), 19) else 0


def b_item_niple(ctx, args):
    chara = to_int(arg(args, 0, 0))
    for slot, ret in ((16, 1), (17, 2), (24, 4)):
        if _tequip(ctx, chara, slot):
            return ret
    return 0


def b_item_penis(ctx, args):
    chara = to_int(arg(args, 0, 0))
    for slot in (12, 41, 43, 44):
        if _tequip(ctx, chara, slot):
            return 1
    return 0


def b_item_vagina(ctx, args):
    chara = to_int(arg(args, 0, 0))
    for slot in (13, 41):
        if _tequip(ctx, chara, slot):
            return 1
    return 0


def _tequip_touch_allows(ctx, chara: int, touch: int, name: str) -> bool:
    value = _tequip(ctx, chara, name)
    return bool((touch & 1 and (value & 2)) or (touch & 2 and (value & 4)))


def b_use_anus(ctx, args):
    required = ("Ａ不可", "臀部露出", "Ａ触覚")
    if not _has_csv_names(ctx, "TEQUIP", required):
        return None
    chara = to_int(arg(args, 0, 0))
    touch = to_int(arg(args, 1, 0))
    shift = to_int(arg(args, 2, 0))
    blocked = _tequip(ctx, chara, "Ａ不可")
    if shift == 2 and blocked == 0:
        return 0
    if _tequip(ctx, chara, "臀部露出") != -1 and not _tequip_touch_allows(ctx, chara, touch, "Ａ触覚") and not (shift and blocked):
        return 0
    if b_item_anus(ctx, [chara]) > 0:
        return 0
    return 1


def b_use_breast(ctx, args):
    required = ("乳房露出", "乳房触覚", "胸構造")
    if not _has_csv_names(ctx, "TEQUIP", required):
        return None
    chara = to_int(arg(args, 0, 0))
    touch = to_int(arg(args, 1, 0))
    structure = to_int(arg(args, 2, 0))
    if (
        _tequip(ctx, chara, "乳房露出") != -1
        and not _tequip_touch_allows(ctx, chara, touch, "乳房触覚")
        and not (structure and (_tequip(ctx, chara, "胸構造") & structure))
    ):
        return 0
    if b_item_niple(ctx, [chara]) > 0:
        return 0
    return 1


def b_use_cli(ctx, args):
    if not _has_csv_names(ctx, "TEQUIP", ("陰唇露出", "Ｃ触覚")):
        return None
    chara = to_int(arg(args, 0, 0))
    if not b_have_clitoris(ctx, [chara]):
        return 0
    touch = to_int(arg(args, 1, 0))
    if _tequip(ctx, chara, "陰唇露出") != -1 and not _tequip_touch_allows(ctx, chara, touch, "Ｃ触覚"):
        return 0
    for slot in (41, 43, 44, 11):
        if _tequip(ctx, chara, slot):
            return 0
    return 1


def b_use_eye(ctx, args):
    if not _has_csv_name(ctx, "TALENT", "目"):
        return None
    return 1 if _talent(ctx, to_int(arg(args, 0, 0)), "目") else 0


def b_use_foot(ctx, args):
    if not _has_csv_names(ctx, "TALENT", ("足", "鉤足")):
        return None
    chara = to_int(arg(args, 0, 0))
    if not _talent(ctx, chara, "足") and not _talent(ctx, chara, "鉤足"):
        return 0
    if _talent(ctx, chara, "鉤足") and _abl(ctx, chara, 2) < 2 and chara == to_int(_ctx_get_var(ctx, "PLAYER", [])):
        return 0
    if b_item_foot(ctx, [chara]) > 0:
        return 0
    return 1


def b_use_hand(ctx, args):
    if not _has_csv_names(ctx, "TALENT", ("腕", "羽", "鉤爪")):
        return None
    chara = to_int(arg(args, 0, 0))
    has_arm = bool(_talent(ctx, chara, "腕"))
    has_wing = bool(_talent(ctx, chara, "羽"))
    if not has_arm and not has_wing:
        return 0
    if (_talent(ctx, chara, "鉤爪") or (has_wing and not has_arm)) and _abl(ctx, chara, 2) < 2 and chara == to_int(_ctx_get_var(ctx, "PLAYER", [])):
        return 0
    if b_item_hand(ctx, [chara]) > 0:
        return 0
    return 1


def b_use_head(ctx, args):
    if not _has_csv_name(ctx, "TALENT", "頭"):
        return None
    return 1 if _talent(ctx, to_int(arg(args, 0, 0)), "頭") else 0


def b_use_mouth(ctx, args):
    if not _has_csv_name(ctx, "TALENT", "口"):
        return None
    chara = to_int(arg(args, 0, 0))
    if _tequip(ctx, chara, 20):
        return 0
    return 1 if _talent(ctx, chara, "口") else 0


def b_use_niple(ctx, args):
    if not _has_csv_names(ctx, "TEQUIP", ("乳首露出", "乳首触覚")):
        return None
    chara = to_int(arg(args, 0, 0))
    touch = to_int(arg(args, 1, 0))
    if _tequip(ctx, chara, "乳首露出") != -1 and not _tequip_touch_allows(ctx, chara, touch, "乳首触覚"):
        return 0
    if b_item_niple(ctx, [chara]) > 0:
        return 0
    return 1


def b_use_pband(ctx, args):
    if not _has_csv_name(ctx, "ITEM", "可穿戴式陽具"):
        return None
    chara = to_int(arg(args, 0, 0))
    if not _item(ctx, "可穿戴式陽具"):
        return 0
    if b_have_penis(ctx, [chara]):
        return 0
    if _tequip(ctx, chara, 11):
        return 0
    return 1


def b_use_penis(ctx, args):
    if not _has_csv_names(ctx, "TEQUIP", ("Vずらし中", "陰唇露出", "Ｃ触覚")):
        return None
    chara = to_int(arg(args, 0, 0))
    if not b_have_penis(ctx, [chara]):
        return 0
    if _tequip(ctx, chara, "Vずらし中") == -1:
        return 1
    touch = to_int(arg(args, 1, 0))
    if _tequip(ctx, chara, "陰唇露出") != -1 and not _tequip_touch_allows(ctx, chara, touch, "Ｃ触覚"):
        return 0
    if b_item_penis(ctx, [chara]) > 0:
        return 0
    return 1


def b_use_tail(ctx, args):
    if not _has_csv_name(ctx, "TALENT", "尾"):
        return None
    return 1 if _talent(ctx, to_int(arg(args, 0, 0)), "尾") else 0


def b_use_vagina(ctx, args):
    if not _has_csv_names(ctx, "TEQUIP", ("Ｖ不可", "Vずらし中", "陰唇露出", "Ｖ触覚")):
        return None
    chara = to_int(arg(args, 0, 0))
    if not b_have_vagina(ctx, [chara]):
        return 0
    touch = to_int(arg(args, 1, 0))
    shift = to_int(arg(args, 2, 0))
    blocked = _tequip(ctx, chara, "Ｖ不可")
    if shift == 2 and blocked == 0:
        return 0
    if _tequip(ctx, chara, "Vずらし中") == -1:
        return 1
    if _tequip(ctx, chara, "陰唇露出") != -1 and not _tequip_touch_allows(ctx, chara, touch, "Ｖ触覚") and not (shift and blocked):
        return 0
    if b_item_vagina(ctx, [chara]) > 0:
        return 0
    return 1


def b_use_wing(ctx, args):
    if not _has_csv_name(ctx, "TALENT", "羽"):
        return None
    return 1 if _talent(ctx, to_int(arg(args, 0, 0)), "羽") else 0


_FAVORITE_REQUIRED = [
    ("CDFLAG", "キャラ間好感度"),
    ("CFLAG", "キャラ固有の番号"),
    ("ABL", "百合属性"), ("ABL", "百合中毒"), ("ABL", "ＢＬ属性"), ("ABL", "ＢＬ中毒"),
    ("TALENT", "男性"), ("TALENT", "偽娘"), ("TALENT", "兩面通吃"), ("TALENT", "討厭女人"), ("TALENT", "討厭男人"),
    ("FLAG", "偽娘ＢＬ設定"),
]


def _favorite_ready(ctx) -> bool:
    return all(_has_csv_name(ctx, var, name) for var, name in _FAVORITE_REQUIRED)


def _cdfavorite(ctx, source: int, target_id: int) -> int:
    return to_int(_ctx_get_var(ctx, "CDFLAG", [source, _script_index(ctx, "CDFLAG", "キャラ間好感度"), target_id + 100]))


def _favorite(ctx, source: int, target: int) -> int | None:
    if not _favorite_ready(ctx):
        return None
    if target < 0:
        return _cdfavorite(ctx, source, target)
    if target >= to_int(_ctx_get_var(ctx, "CHARANUM", [])):
        return 0
    target_id = _cflag(ctx, target, "キャラ固有の番号")
    if target_id < 0:
        return 0
    if b_is_lesbian(ctx, [source, target]):
        ratio = _abl(ctx, source, _script_index(ctx, "ABL", "百合属性")) * 10 + _abl(ctx, source, _script_index(ctx, "ABL", "百合中毒")) * 20 + _talent(ctx, source, "兩面通吃") * 50
    elif b_is_gay(ctx, [source, target]):
        ratio = _abl(ctx, source, _script_index(ctx, "ABL", "ＢＬ属性")) * 10 + _abl(ctx, source, _script_index(ctx, "ABL", "ＢＬ中毒")) * 20 + _talent(ctx, source, "兩面通吃") * 50
    else:
        ratio = 100
    if _talent(ctx, source, "討厭女人") and not b_is_male(ctx, [target]):
        ratio -= 50
    if _talent(ctx, source, "討厭男人") and b_is_male(ctx, [target]):
        ratio -= 50
    ratio = max(1, min(100, ratio))
    return int(_cdfavorite(ctx, source, target_id) * ratio / 100)


def b_favorite(ctx, args):
    return _favorite(ctx, to_int(arg(args, 0, 0)), to_int(arg(args, 1, 0)))


def b_favorite_id(ctx, args):
    if not _favorite_ready(ctx):
        return None
    source = to_int(arg(args, 0, 0))
    target_id = to_int(arg(args, 1, 0))
    if target_id < 0:
        return _cdfavorite(ctx, source, target_id)
    target = b_findchara_id(ctx, [target_id])
    if target < 0:
        return 0
    return _favorite(ctx, source, target)


def b_favorite_1(ctx, args):
    if not _favorite_ready(ctx):
        return None
    source = to_int(arg(args, 0, 0))
    start = 0 if to_int(arg(args, 1, arg(args, 2, 0))) != 0 else -to_int(_ctx_get_var(ctx, "MAX_NTR_CHARA", []))
    end = to_int(_ctx_get_var(ctx, "CHARANUM", []))
    best_target = 0
    best_value = 0
    for target in range(start, end):
        if target == source:
            continue
        value = abs(to_int(_favorite(ctx, source, target)))
        if value > best_value:
            best_target = target
            best_value = value
    return best_target


def b_favorite_1_id(ctx, args):
    if not _favorite_ready(ctx):
        return None
    source = to_int(arg(args, 0, 0))
    start = 0 if to_int(arg(args, 1, arg(args, 2, 0))) != 0 else -to_int(_ctx_get_var(ctx, "MAX_NTR_CHARA", []))
    end = to_int(_ctx_get_var(ctx, "MAX_PLAYER_CHARA", []))
    if end <= 0:
        end = to_int(_ctx_get_var(ctx, "CHARANUM", []))
    source_id = _cflag(ctx, source, "キャラ固有の番号")
    best_id = 0
    best_value = 0
    for target_id in range(start, end):
        if target_id == source_id:
            continue
        value = abs(to_int(b_favorite_id(ctx, [source, target_id])))
        if value > best_value:
            best_id = target_id
            best_value = value
    return best_id


def b_skill_change(ctx, args):
    if not _has_csv_name(ctx, "CFLAG", "被リンクフラグ"):
        return None
    chara = to_int(arg(args, 0, -1))
    target_no = to_int(arg(args, 1, 0))
    if chara <= -1 or b_getchara(ctx, [target_no]) <= -1:
        return 0
    if to_int(_ctx_get_var(ctx, "NO", [chara])) == target_no:
        return 1
    linked_id = _cflag(ctx, chara, "被リンクフラグ")
    if linked_id >= 0:
        linked = b_findchara_id(ctx, [linked_id])
        if linked >= 0 and to_int(_ctx_get_var(ctx, "NO", [linked])) == target_no:
            return 1
    return 0


_WEAPON_TYPE_NAMES = ["其他", "刀", "刺剣", "弓", "剣", "槍", "斧、鈍器", "鞭", "投具", "杖、祭具", "棍", "拳闘具"]
_WEAPON_TYPE_ALIASES = {
    "刀": 1, "日本刀": 1,
    "刺剣": 2, "突剣": 2,
    "弓": 3,
    "剣": 4,
    "槍": 5, "薙刀": 5, "長刀": 5,
    "斧、鈍器": 6, "斧": 6, "鈍器": 6, "超度ー": 6,
    "鞭": 7, "ムチ": 7,
    "投具": 8, "ブーメラン": 8, "飛刀": 8,
    "杖、祭具": 9, "杖": 9, "祭具": 9,
    "棍": 10, "棒": 10,
    "拳闘具": 11, "拳": 11, "グ長袍": 11,
}


def b_get_weapon_type(ctx, args):
    idx = to_int(arg(args, 0, 0))
    return _WEAPON_TYPE_NAMES[idx] if 0 <= idx < len(_WEAPON_TYPE_NAMES) else "其他"


def b_get_weapon_type_num(ctx, args):
    return _WEAPON_TYPE_ALIASES.get(to_str(arg(args, 0, "")), 0)


def b_skill_name_f(ctx, args):
    skill = to_int(arg(args, 0, 0))
    if skill == 0:
        return "ＡＴＴＡＣＫ"
    name = to_str(_ctx_get_var(ctx, "ITEMNAME", [10000 + skill]))
    if not name:
        return "EMPTY"
    start = name.find("【")
    end = name.find("】", start + 1)
    return name[start + 1 : end] if start >= 0 and end > start else name


def b_skill_num_f(ctx, args):
    needle = "【" + to_str(arg(args, 0, "")) + "】"
    db = getattr(ctx.program, "csv", None)
    if not db or "ITEM" not in db.index_to_name:
        return None
    for idx in sorted(db.index_to_name["ITEM"]):
        if idx >= 10000 and needle in db.index_to_name["ITEM"][idx]:
            return idx - 10000
    return -1


def b_pu_num(ctx, args):
    return 12


def _pu_ready(ctx) -> bool:
    return (
        _has_csv_name(ctx, "CSTR", "専用技1")
        and _has_csv_names(ctx, "TALENT", ("悪魔変身", "Aion式召喚術"))
        and _has_csv_names(ctx, "CFLAG", ("悪魔変身", "リンク悪魔"))
        and _has_csv_name(ctx, "ABL", "技能5")
    )


def _pu_skill_base(ctx) -> int | None:
    value = b_skill_num_f(ctx, ["専用技1"])
    return None if value is None else to_int(value)


def _pu_skill_cstr(ctx, chara: int, slot: int) -> str | None:
    if not _pu_ready(ctx):
        return None
    linked_id = _cflag(ctx, chara, "リンク悪魔")
    if _talent(ctx, chara, "悪魔変身") and _cflag(ctx, chara, "悪魔変身") and linked_id > 0:
        linked = b_findchara_id(ctx, [linked_id])
        return to_str(_ctx_get_var(ctx, "CSTR", [linked, f"専用技{slot}"])) if linked >= 0 else ""
    if _talent(ctx, chara, "Aion式召喚術") and linked_id > 0:
        base = _pu_skill_base(ctx)
        count = _skill_count(ctx, chara)
        if base is not None and count is not None:
            for skill_slot in range(5, count + 1):
                if _abl(ctx, chara, f"技能{skill_slot}") == base + slot:
                    linked = b_findchara_id(ctx, [linked_id])
                    return to_str(_ctx_get_var(ctx, "CSTR", [linked, f"専用技{slot}"])) if linked >= 0 else ""
    return to_str(_ctx_get_var(ctx, "CSTR", [chara, f"専用技{slot}"]))


def b_get_pu_skill_cstr(ctx, args):
    return _pu_skill_cstr(ctx, to_int(arg(args, 0, 0)), to_int(arg(args, 1, 0)))


def b_pu_skillnum_get(ctx, args):
    if not _pu_ready(ctx):
        return None
    chara = to_int(arg(args, 0, 0))
    name = to_str(arg(args, 1, ""))
    if name == "":
        return 0
    base = _pu_skill_base(ctx)
    if base is None:
        return None
    for slot in range(0, b_pu_num(ctx, [])):
        if name == (_pu_skill_cstr(ctx, chara, slot + 1) or ""):
            return base + slot
    return -1


def b_pu_skill_check(ctx, args):
    if not _pu_ready(ctx):
        return None
    chara = to_int(arg(args, 0, 0))
    name = to_str(arg(args, 1, ""))
    if name == "":
        return 0
    for slot in range(0, b_pu_num(ctx, [])):
        if name == (_pu_skill_cstr(ctx, chara, slot + 1) or ""):
            return slot if to_int(arg(args, 2, 0)) == 1 else 1
    return 0


def b_is_pu_skill(ctx, args):
    base = _pu_skill_base(ctx)
    if base is None or not _pu_ready(ctx):
        return None
    skill = to_int(arg(args, 1, 0))
    return 1 if b_pu_skillnum_get(ctx, [to_int(arg(args, 0, 0)), to_str(arg(args, 2, ""))]) == skill and base <= skill < base + b_pu_num(ctx, []) else 0


def b_have_pu_skill(ctx, args):
    skill = b_pu_skillnum_get(ctx, [to_int(arg(args, 0, 0)), to_str(arg(args, 1, ""))])
    if skill is None:
        return None
    if skill < 0:
        return 0
    return b_have_skill(ctx, [to_int(arg(args, 0, 0)), skill, to_int(arg(args, 2, 0))])


def b_pueq_num_check(ctx, args):
    no = to_int(arg(args, 0, 0))
    return 1 if no in {3439, 3939, 4399, 4939, 5349} or 2378 <= no < 2389 or 2935 <= no < 2940 else 0


def b_pueq_num_get(ctx, args):
    text = to_str(arg(args, 0, ""))
    weapon = b_get_weapon_type_num(ctx, [text])
    if weapon > 0:
        return 2378 + weapon
    direct = {
        "頭": 3439, "胴": 3939, "腕": 4399, "足": 4939, "飾": 5349, "飾品": 5349,
        "武器": 2378, "武具": 2378, "剣0": 2378, "其他": 2378,
        "銃1": 2935, "HG": 2935, "ＨＧ": 2935, "ハンドガン": 2935,
        "銃2": 2936, "SG": 2936, "ＳＧ": 2936, "ショットガン": 2936,
        "銃3": 2937, "AR": 2937, "ＡＲ": 2937, "アサルトライフル": 2937, "MG": 2937, "ＭＧ": 2937, "機械ガン": 2937,
        "銃4": 2938, "SR": 2938, "ＳＲ": 2938, "狙撃ライフル": 2938, "スナイパーライフル": 2938,
        "銃": 2939, "銃0": 2939, "銃5": 2939, "ETC": 2939, "手榴弾": 2939, "グレネード": 2939, "ミサイル": 2939, "其他銃": 2939,
    }
    if text in direct:
        return direct[text]
    if text.startswith("剣"):
        n = to_int(text[1:])
        if 0 <= n <= 11:
            return 2378 + n
    return 0


def _pueq_name_get(ctx, chara: int, equip_no: int) -> str | None:
    if not _has_csv_name(ctx, "CSTR", "専用装備"):
        return None
    if not b_charanum_check(ctx, [chara]) or not b_pueq_num_check(ctx, [equip_no]):
        return ""
    parts = to_str(_ctx_get_var(ctx, "CSTR", [chara, "専用装備"])).split("_")
    for i, part in enumerate(parts[:100]):
        if i > 1 and part == "":
            return ""
        if b_pueq_num_get(ctx, [part]) == equip_no:
            return parts[i - 1] if i > 0 else ""
    return ""


def b_pueq_name_get(ctx, args):
    return _pueq_name_get(ctx, to_int(arg(args, 0, 0)), to_int(arg(args, 1, 0)))


def b_pueq_name_gets(ctx, args):
    num = b_pueq_num_get(ctx, [to_str(arg(args, 1, ""))])
    return _pueq_name_get(ctx, to_int(arg(args, 0, 0)), num)


def _strflag_arg(args: list[Value], i: int, default: Value = 0) -> Value:
    value = arg(args, i, default)
    # Runtime supplies a private OMITTED_ARG sentinel for omitted parameters
    # whose ERB #FUNCTION has a default expression.  Builtins run before the
    # script fallback can apply those defaults, so normalize non-scalar sentinels
    # here without importing runtime internals.
    return default if not isinstance(value, (int, str)) else value


def _add_strflag(text: str, flag: str) -> str:
    if text == "":
        text = "/"
    return text + flag + "/"


def _del_strflag(text: str, flag: str) -> str:
    needle = f"/{flag}/"
    pos = text.find(needle)
    if pos < 0:
        return text
    return text[:pos] + "/" + text[pos + len(needle):]


def _swap_strflag(text: str, old_flag: str, new_flag: str) -> str:
    # Match the current ERB helper exactly: SWAP_STRFLAG first builds "/old/",
    # then passes that *already delimited* string to DEL_STRFLAG, whose own
    # wrapping can leave the old entry in place.  Some eraMegaten flows rely on
    # this observed script behavior, so preserve it rather than "fixing" it.
    needle = f"/{old_flag}/"
    if text.find(needle) < 0:
        return text
    return _add_strflag(_del_strflag(text, needle), new_flag)


def b_add_strflag(ctx, args):
    return _add_strflag(to_str(_strflag_arg(args, 0, "")), to_str(_strflag_arg(args, 1, "")))


def b_del_strflag(ctx, args):
    return _del_strflag(to_str(_strflag_arg(args, 0, "")), to_str(_strflag_arg(args, 1, "")))


def b_swap_strflag(ctx, args):
    return _swap_strflag(
        to_str(_strflag_arg(args, 0, "")),
        to_str(_strflag_arg(args, 1, "")),
        to_str(_strflag_arg(args, 2, "")),
    )


def _change_strflag_num(text: str, name: str, value: int) -> str:
    parts = text.split("|")
    deleted = ""
    found = False
    for i, part in enumerate(parts):
        if part == name:
            old_value = parts[i + 1] if i + 1 < len(parts) else ""
            old_entry = f"|{part}|{old_value}|"
            deleted = _del_strflag(text, old_entry)
            found = True
            break
    new_entry = f"|{name}|{value}|"
    return _add_strflag(deleted if found else text, new_entry)


def b_change_strflag_num(ctx, args):
    return _change_strflag_num(
        to_str(_strflag_arg(args, 0, "")),
        to_str(_strflag_arg(args, 1, "")),
        to_int(_strflag_arg(args, 2, 0)),
    )


def _strflag_num_value(text: str, name: str) -> int:
    parts = text.split("|")
    for i, part in enumerate(parts):
        if part == name:
            return to_int(parts[i + 1]) if i + 1 < len(parts) else 0
    return 0


def _strflag_throw(ctx, message: str) -> None:
    fatal = getattr(ctx, "_fatal", None)
    if callable(fatal):
        fatal(f"THROW: {message}")


def _resolve_strflag_save_slot(ctx, value: Value, default_flag: str, offset: int, label: str) -> int | None:
    slot = to_int(value)
    if slot == -1:
        slot = _flag(ctx, default_flag)
    if slot < 0 or slot > 199:
        _strflag_throw(ctx, f"{label}ナンバーが異常です")
        return None
    return offset + slot


def _strflag_text(ctx, args, *, offset: int, default_flag: str, label: str):
    slot = _resolve_strflag_save_slot(
        ctx,
        _strflag_arg(args, 2, -1),
        default_flag,
        offset,
        label,
    )
    if slot is None:
        return None
    name = to_str(_strflag_arg(args, 0, ""))
    op = to_int(_strflag_arg(args, 1, 0))
    replacement = to_str(_strflag_arg(args, 3, ""))
    text = to_str(_ctx_get_var(ctx, "SAVESTR", [slot]))
    needle = f"/{name}/"
    if b_strcount(ctx, [text, needle]):
        if op == -1:
            _ctx_set_var(ctx, "SAVESTR", [slot], _del_strflag(text, name))
        if op == 2:
            _ctx_set_var(ctx, "SAVESTR", [slot], _swap_strflag(text, name, replacement))
        return 1
    if op == 1:
        _ctx_set_var(ctx, "SAVESTR", [slot], _add_strflag(text, name))
    return 0


def b_strflag_d(ctx, args):
    return _strflag_text(ctx, args, offset=100, default_flag="現ダンジョン", label="ダンジョン")


def b_strflag_ev(ctx, args):
    return _strflag_text(ctx, args, offset=200, default_flag="進行中事件", label="事件")


def b_strflag_clo(ctx, args):
    return _strflag_text(ctx, args, offset=300, default_flag="進行中コロシアム", label="コロシアム")


def b_strflag_req(ctx, args):
    return _strflag_text(ctx, args, offset=400, default_flag="進行中依頼", label="依頼")


def _strflag_num_apply(ctx, base: str, indices: list[Value], name: str, op: str, compare: int, new_value: int):
    text = to_str(_ctx_get_var(ctx, base, indices))
    current = _strflag_num_value(text, name)
    if op == "=":
        _ctx_set_var(ctx, base, indices, _change_strflag_num(text, name, new_value))
        return 1
    if op == "+=":
        _ctx_set_var(ctx, base, indices, _change_strflag_num(text, name, current + new_value))
        return 1
    if op == "-=":
        _ctx_set_var(ctx, base, indices, _change_strflag_num(text, name, current - new_value))
        return 1
    matched = {
        "==": current == compare,
        ">": current > compare,
        ">=": current >= compare,
        "<": current < compare,
        "<=": current <= compare,
        "!=": current != compare,
    }.get(op)
    if matched is None:
        return current
    if matched:
        _ctx_set_var(ctx, base, indices, _change_strflag_num(text, name, new_value))
        return 1
    return 0


def _strflag_num_save(ctx, args, *, offset: int, default_flag: str, label: str):
    slot = _resolve_strflag_save_slot(
        ctx,
        _strflag_arg(args, 4, -1),
        default_flag,
        offset,
        label,
    )
    if slot is None:
        return None
    return _strflag_num_apply(
        ctx,
        "SAVESTR",
        [slot],
        to_str(_strflag_arg(args, 0, "")),
        to_str(_strflag_arg(args, 1, "")),
        to_int(_strflag_arg(args, 2, 0)),
        to_int(_strflag_arg(args, 3, 0)),
    )


def b_strflag_num_d(ctx, args):
    return _strflag_num_save(ctx, args, offset=100, default_flag="現ダンジョン", label="ダンジョン")


def b_strflag_num_ev(ctx, args):
    return _strflag_num_save(ctx, args, offset=200, default_flag="進行中事件", label="事件")


def b_strflag_num_col(ctx, args):
    return _strflag_num_save(ctx, args, offset=300, default_flag="進行中コロシアム", label="コロシアム")


def b_strflag_num_req(ctx, args):
    return _strflag_num_save(ctx, args, offset=400, default_flag="進行中依頼", label="依頼")


def b_cstrflag_num(ctx, args):
    slot = to_int(_strflag_arg(args, 4, 1))
    if slot < 0 or slot > 99:
        _strflag_throw(ctx, "CSTR番号が異常です")
        return None
    if slot in {11, 12, 16, 17} or 19 < slot < 28 or 29 < slot < 41:
        _strflag_throw(ctx, "CSVで予約済みのCSTR番号です")
        return None
    return _strflag_num_apply(
        ctx,
        "CSTR",
        [slot],
        to_str(_strflag_arg(args, 0, "")),
        to_str(_strflag_arg(args, 1, "")),
        to_int(_strflag_arg(args, 2, 0)),
        to_int(_strflag_arg(args, 3, 0)),
    )


def b_tstrflag_num(ctx, args):
    slot = to_int(_strflag_arg(args, 4, 1))
    if slot < 0 or slot > 99:
        _strflag_throw(ctx, "TSTR番号が異常です")
        return None
    return _strflag_num_apply(
        ctx,
        "TSTR",
        [slot],
        to_str(_strflag_arg(args, 0, "")),
        to_str(_strflag_arg(args, 1, "")),
        to_int(_strflag_arg(args, 2, 0)),
        to_int(_strflag_arg(args, 3, 0)),
    )


_CPD_STRFLAGS = (
    "NAME", "NO", "LV",
    "力", "力強化回数", "知恵", "知恵強化回数", "魔力", "魔力強化回数",
    "耐力", "耐力強化回数", "速度", "速度強化回数", "運", "運強化回数",
    "ＥＸＰ", "能力強化回数", "変異", "変異等級",
    "技能1", "技能2", "技能3", "技能4", "技能5", "技能6", "技能7", "技能8",
    "習得技能1", "習得技能2", "習得技能3", "習得技能4", "習得技能5",
    "習得技能6", "習得技能7", "習得技能8", "習得技能9", "習得技能10",
    "習得技能11", "習得技能12", "習得技能13", "習得技能14", "習得技能15",
    "習得技能16", "習得技能17", "習得技能18", "習得技能19", "習得技能20",
    "習得LV1", "習得LV2", "習得LV3", "習得LV4", "習得LV5",
    "習得LV6", "習得LV7", "習得LV8", "習得LV9", "習得LV10",
    "習得LV11", "習得LV12", "習得LV13", "習得LV14", "習得LV15",
    "習得LV16", "習得LV17", "習得LV18", "習得LV19", "習得LV20",
    "変更相性1", "変更相性値1", "攻撃相性", "射程", "攻撃範囲",
)
_CPD_STRFLAGS_TEXT = ",".join(_CPD_STRFLAGS)


def _cpd_strflag_num(name: str) -> int:
    if name == "":
        return len(_CPD_STRFLAGS)
    try:
        return _CPD_STRFLAGS.index(name)
    except ValueError:
        return -1


def b_get_cpd_strflag(ctx, args):
    idx = to_int(_strflag_arg(args, 0, -1))
    if idx == 99:
        return _CPD_STRFLAGS_TEXT
    if idx < 0 or idx > 99:
        return "登録されていません"
    return _CPD_STRFLAGS[idx] if idx < len(_CPD_STRFLAGS) else "登録されていません"


def b_get_cpd_strflag_num(ctx, args):
    return _cpd_strflag_num(to_str(_strflag_arg(args, 0, "")))


def _cpd_saved_parts(ctx, slot: int) -> list[str]:
    return to_str(_ctx_get_var(ctx, "SAVESTR", [slot])).split(",")


def b_strflag_num_cpd_find(ctx, args):
    name = to_str(_strflag_arg(args, 0, ""))
    target = to_int(_strflag_arg(args, 1, 0))
    field_idx = _cpd_strflag_num(name)
    for slot in range(2000, 7000):
        text = to_str(_ctx_get_var(ctx, "SAVESTR", [slot]))
        if text == "":
            return slot if target == -1 else -1
        if name != "" and field_idx >= 0:
            parts = text.split(",")
            value = parts[field_idx] if field_idx < len(parts) else ""
            if to_int(value) == target:
                return slot
    return -2


def b_get_cpd_savestr_num(ctx, args):
    slot = to_int(_strflag_arg(args, 0, 0))
    name = to_str(_strflag_arg(args, 1, ""))
    field_idx = _cpd_strflag_num(name)
    if slot < 2000 or slot >= 7000:
        _strflag_throw(ctx, f"引数が異常です。{slot}")
        return None
    text = to_str(_ctx_get_var(ctx, "SAVESTR", [slot]))
    if text == "" or field_idx < 0 or field_idx > 99:
        return "登録されていません"
    parts = text.split(",")
    return parts[field_idx] if field_idx < len(parts) else ""


def b_current_hp_rate(ctx, args):
    if not _has_csv_name(ctx, "BASE", "ＨＰ"):
        return None
    chara = to_int(arg(args, 0, 0))
    hp_idx = _script_index(ctx, "BASE", "ＨＰ")
    hp = to_int(_ctx_get_var(ctx, "BASE", [chara, hp_idx]))
    max_hp = to_int(_ctx_get_var(ctx, "MAXBASE", [chara, hp_idx]))
    return 0 if max_hp == 0 else int(hp * 100 / max_hp)


def b_current_mp_rate(ctx, args):
    if not _has_csv_name(ctx, "BASE", "ＭＰ"):
        return None
    chara = to_int(arg(args, 0, 0))
    mp_idx = _script_index(ctx, "BASE", "ＭＰ")
    mp = to_int(_ctx_get_var(ctx, "BASE", [chara, mp_idx]))
    max_mp = to_int(_ctx_get_var(ctx, "MAXBASE", [chara, mp_idx]))
    return 0 if max_mp == 0 else int(mp * 100 / max_mp)


def b_damage_rate(ctx, args):
    if not _has_csv_name(ctx, "BASE", "ＨＰ"):
        return None
    chara = to_int(arg(args, 0, 0))
    damage = to_int(arg(args, 1, 0))
    max_hp = to_int(_ctx_get_var(ctx, "MAXBASE", [chara, _script_index(ctx, "BASE", "ＨＰ")]))
    return 0 if max_hp == 0 else int(damage * 100 / max_hp)


def b_danger_day(ctx, args):
    required = [
        ("FLAG", "月齢"), ("FLAG", "月齢ベクトル"),
        ("ABL", "種族"),
        ("TALENT", "男性"), ("TALENT", "偽娘"), ("TALENT", "可以発情"),
        ("CFLAG", "発情妊娠"), ("CFLAG", "ダンジョン内発情"), ("CFLAG", "危険日"),
    ]
    if not all(_has_csv_name(ctx, var, name) for var, name in required):
        return None
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    moon = to_int(_ctx_get_var(ctx, "FLAG", [_script_index(ctx, "FLAG", "月齢")]))
    if 0 < moon < 8 and to_int(_ctx_get_var(ctx, "FLAG", [_script_index(ctx, "FLAG", "月齢ベクトル")])) == 1:
        moon = 16 - moon
    race = to_int(_ctx_get_var(ctx, "ABL", [chara, _script_index(ctx, "ABL", "種族")]))
    male = _talent(ctx, chara, "男性") and not _talent(ctx, chara, "偽娘")
    if moon == 8 and race > 0 and race != 45:
        return -2 if male else 3
    if _cflag(ctx, chara, "発情妊娠") or _cflag(ctx, chara, "ダンジョン内発情"):
        return 2
    if male:
        return -1
    if moon == _cflag(ctx, chara, "危険日"):
        return 2 if _talent(ctx, chara, "可以発情") else 1
    return 0


_SKILLCOUNT_REQUIRED = [
    ("CFLAG", "PTフラグ"), ("CFLAG", "ボスフラグ"), ("CFLAG", "リンク悪魔"), ("CFLAG", "悪魔変身"),
    ("TALENT", "Aion式召喚術"), ("TALENT", "Persona使"), ("TALENT", "異能者"), ("TALENT", "達人"), ("TALENT", "人修羅"),
    ("FLAG", "技能数"), ("FLAG", "異能者技能数"),
]


def _skillcount_ready(ctx) -> bool:
    return all(_has_csv_name(ctx, var, name) for var, name in _SKILLCOUNT_REQUIRED)


def _flag(ctx, name: str) -> int:
    return to_int(_ctx_get_var(ctx, "FLAG", [_script_index(ctx, "FLAG", name)]))


def _skill_count(ctx, chara: int, human_state: int = 1, *, operation_mode: bool = False) -> int | None:
    if not _skillcount_ready(ctx):
        return None
    if _cflag(ctx, chara, "PTフラグ") < 1 and _cflag(ctx, chara, "ボスフラグ"):
        return 20
    if _talent(ctx, chara, "Aion式召喚術"):
        if not operation_mode and _cflag(ctx, chara, "リンク悪魔") > 0:
            return 4 + _flag(ctx, "技能数")
        return 4
    if _talent(ctx, chara, "Persona使"):
        return _flag(ctx, "技能数")
    if (
        (_talent(ctx, chara, "異能者") or _talent(ctx, chara, "達人") or _talent(ctx, chara, "人修羅"))
        and (not _cflag(ctx, chara, "悪魔変身") or human_state == 0)
    ):
        return _flag(ctx, "異能者技能数")
    return _flag(ctx, "技能数")


def b_chara_skillcount(ctx, args):
    return _skill_count(ctx, to_int(arg(args, 0, 0)), to_int(arg(args, 1, 1)), operation_mode=False)


def b_chara_skillcount_for_ops(ctx, args):
    return _skill_count(ctx, to_int(arg(args, 0, 0)), to_int(arg(args, 1, 1)), operation_mode=True)


def _skill_helpers_ready(ctx, *, initial: bool = False) -> bool:
    names = ["装備技能1", "技能1"]
    if initial:
        names.append("初期変身悪魔技能1")
    return _skillcount_ready(ctx) and _has_csv_names(ctx, "ABL", names)


def _abl(ctx, chara: int, segment: int | str) -> int:
    return to_int(_ctx_get_var(ctx, "ABL", [chara, segment]))


def _skill_slot_value(ctx, chara: int, prefix: str, slot: int) -> int:
    # Dynamic LOCALS/@"..." segments resolve through the CSV name map, not
    # through the first-installed bare constant.
    return _abl(ctx, chara, f"{prefix}{slot}")


def _count_skill_matches(ctx, chara: int, skill: int, start_idx: int, end_idx: int) -> int:
    return sum(1 for idx in range(start_idx, end_idx) if _abl(ctx, chara, idx) == skill)


def _have_skill(ctx, args, *, initial: bool = False, overlap: bool = False, check_only: bool = False):
    if not _skill_helpers_ready(ctx, initial=initial):
        return None
    chara = to_int(arg(args, 0, 0))
    if chara < 0:
        return 0
    skill = to_int(arg(args, 1, 0))
    skill_count = _skill_count(ctx, chara)
    if skill_count is None:
        return None
    base_name = "初期変身悪魔技能1" if initial else "技能1"
    base_start = _csv_index(ctx, "ABL", base_name)
    normal_matches = _count_skill_matches(ctx, chara, skill, base_start, base_start + skill_count + 1)
    if overlap:
        total = normal_matches
        if not check_only:
            equip_start = _csv_index(ctx, "ABL", "装備技能1")
            total += _count_skill_matches(ctx, chara, skill, equip_start, equip_start + 22)
        return total
    if check_only:
        return 1 if normal_matches else 0
    if normal_matches:
        if to_int(arg(args, 2, 0)) == 0:
            return 1
        # HAVE_SKILL_C uses the same return-position loop text as HAVE_SKILL:
        # LOCALS = 技能{LOCAL}; preserve that script quirk.
        for slot in range(1, skill_count + 1):
            if _skill_slot_value(ctx, chara, "技能", slot) == skill:
                return slot
    equip_start = _csv_index(ctx, "ABL", "装備技能1")
    equip_matches = _count_skill_matches(ctx, chara, skill, equip_start, equip_start + 22)
    if equip_matches:
        if to_int(arg(args, 2, 0)) == 0:
            return 1
        for slot in range(1, 22):
            if _skill_slot_value(ctx, chara, "装備技能", slot) == skill:
                return 20 + slot
    return 0


def b_have_skill(ctx, args):
    return _have_skill(ctx, args)


def b_have_skill_c(ctx, args):
    return _have_skill(ctx, args, initial=True)


def b_have_skill_overlap(ctx, args):
    return _have_skill(ctx, args, initial=to_int(arg(args, 2, 0)) != 0, overlap=True)


def b_check_skill(ctx, args):
    return _have_skill(ctx, args, check_only=True)


def b_check_skill_overlap(ctx, args):
    return _have_skill(ctx, args, overlap=True, check_only=True)


def b_bust(ctx, args):
    if not _has_csv_names(ctx, "TALENT", ("絶壁", "貧乳", "巨乳", "爆乳", "魔乳")):
        return None
    chara = to_int(arg(args, 0, 0))
    if _talent(ctx, chara, "絶壁"):
        return 1
    if _talent(ctx, chara, "貧乳"):
        return 2
    if _talent(ctx, chara, "巨乳"):
        return 4
    if _talent(ctx, chara, "爆乳"):
        return 5
    if _talent(ctx, chara, "魔乳"):
        return 6
    return 3


def _sync_call_result(ctx, name: str, args: list[Value], default: int = -1) -> int:
    if not _ctx_can_call_script(ctx, name):
        return default
    try:
        return to_int(ctx.call_expr_function(name, args))
    except Exception:
        return default


def _skill_search_ready(ctx) -> bool:
    return _skill_helpers_ready(ctx)


def _skill_sphere_matches(query: str, result: int) -> bool:
    return (
        (query == "単体" and result == 1)
        or (query == "一列" and result == 2)
        or (query == "全体" and result == 3)
        or (query == "敵味方全体" and result == 4)
        or (query == "範囲" and result in {2, 3})
    )


def _skill_effect_matches(query: str, result: int) -> bool:
    return (
        (query == "攻撃" and result == 1)
        or (query == "回復" and result == 2)
        or (query == "状態変化" and result == 3)
        or (query == "特殊" and result == 4)
        or (query == "状態回復" and result == 5)
    )


def _skill_kind_matches(query: str, result: int) -> bool:
    return (
        (query in {"EXTRA", "物理"} and result == 1)
        or (query in {"MAGIC", "魔法"} and result == 2)
        or (query in {"OTHER", "其他"} and result not in {1, 2})
    )


def _skill_expected_power(ctx, chara: int, skill: int) -> int:
    call_arg = -1 if skill in {229, 230} else chara
    power = _sync_call_result(ctx, f"SKILL_POWER_{skill}", [chara], -1)
    if power < 0:
        return power
    max_hits = _sync_call_result(ctx, f"SKILL_MAXATTACKNUMBER_{skill}", [call_arg], 0)
    per_hits = _sync_call_result(ctx, f"SKILL_MAXATK_PER_{skill}", [call_arg], -1)
    if per_hits > 0:
        max_hits = per_hits
    min_hits = _sync_call_result(ctx, f"SKILL_MINATTACKNUMBER_{skill}", [call_arg], 0)
    if 1 < max_hits + min_hits:
        power = int(power * (max_hits + min_hits) / 2)
    return power


def _skill_check_common(
    ctx,
    *,
    chara: int,
    skill: int,
    type_name: str = "",
    sphere_name: str = "",
    effect_name: str = "",
    kind_name: str = "",
    state_name: str = "",
    power_threshold: int = 0,
    link_skill: int = -1,
) -> int | None:
    if not skill:
        return 0
    if kind_name:
        result = _sync_call_result(ctx, f"SKILL_DECIDE_TYPE_{skill}", [], -1)
        if not _skill_kind_matches(kind_name, result):
            return 0
    if type_name:
        result = _sync_call_result(ctx, f"SKILL_TYPE_{skill}", [chara], -1)
        type_no = b_get_type_num(ctx, [type_name])
        if type_no is None or result != to_int(type_no):
            return 0
    if sphere_name:
        result = _sync_call_result(ctx, f"SKILL_SPHERE_{skill}", [], -1)
        if not _skill_sphere_matches(sphere_name, result):
            return 0
    if effect_name:
        result = _sync_call_result(ctx, f"SKILL_EFECT_{skill}", [], -1)
        if not _skill_effect_matches(effect_name, result):
            return 0
    if state_name:
        result = _sync_call_result(ctx, f"SKILL_ADDTIONAL_STATE_{skill}", [chara], -1)
        state_no = b_get_state_num(ctx, [state_name])
        if state_no is None or result != to_int(state_no):
            return 0
    if power_threshold != 0:
        power = _skill_expected_power(ctx, chara, skill)
        if power_threshold > 0:
            if power < power_threshold:
                return 0
        else:
            if power >= -power_threshold:
                return 0
    actionable = _sync_call_result(ctx, "CHECK_ACTIONABLE", [chara, skill], 1)
    if not actionable:
        return 0
    if 4000 <= link_skill <= 4999:
        if not _sync_call_result(ctx, f"SKILL_SPECIAL_ACTIONABLE_{link_skill}", [chara], 0):
            return 0
    return 1


def b__skill_check(ctx, args):
    return _skill_check_common(
        ctx,
        chara=to_int(arg(args, 0, -1)),
        type_name=to_str(arg(args, 1, "")),
        sphere_name=to_str(arg(args, 2, "")),
        effect_name=to_str(arg(args, 3, "")),
        kind_name=to_str(arg(args, 4, "")),
        link_skill=to_int(arg(args, 5, -1)),
        skill=to_int(arg(args, 6, 0)),
    )


def b__skill_check2(ctx, args):
    return _skill_check_common(
        ctx,
        chara=to_int(arg(args, 0, -1)),
        type_name=to_str(arg(args, 1, "")),
        sphere_name=to_str(arg(args, 2, "")),
        effect_name=to_str(arg(args, 3, "")),
        kind_name=to_str(arg(args, 4, "")),
        state_name=to_str(arg(args, 5, "")),
        power_threshold=to_int(arg(args, 6, 0)),
        link_skill=to_int(arg(args, 7, -1)),
        skill=to_int(arg(args, 8, 0)),
    )


def _skill_search_slots(ctx, chara: int, *, include_equip: bool) -> list[int] | None:
    skill_count = _skill_count(ctx, chara)
    if skill_count is None:
        return None
    values = [_skill_slot_value(ctx, chara, "技能", slot) for slot in range(1, skill_count + 1)]
    if include_equip:
        for slot in range(1, 22):
            value = _skill_slot_value(ctx, chara, "装備技能", slot)
            if not value:
                break
            values.append(value)
    return values


def _skill_search(ctx, args, *, include_equip: bool, extended: bool):
    if not _skill_search_ready(ctx):
        return None
    chara = to_int(arg(args, 0, -1))
    type_name = to_str(arg(args, 1, ""))
    sphere_name = to_str(arg(args, 2, ""))
    effect_name = to_str(arg(args, 3, ""))
    kind_name = to_str(arg(args, 4, ""))
    state_name = to_str(arg(args, 5, "")) if extended else ""
    power = to_int(arg(args, 6, 0)) if extended else 0
    link = to_int(arg(args, 7, -1)) if extended else to_int(arg(args, 5, -1))
    slots = _skill_search_slots(ctx, chara, include_equip=include_equip)
    if slots is None:
        return None
    for skill in slots:
        ok = _skill_check_common(
            ctx,
            chara=chara,
            skill=skill,
            type_name=type_name,
            sphere_name=sphere_name,
            effect_name=effect_name,
            kind_name=kind_name,
            state_name=state_name,
            power_threshold=power,
            link_skill=link,
        )
        if ok:
            return 1
    return 0


def b_check_skill_search(ctx, args):
    return _skill_search(ctx, args, include_equip=False, extended=False)


def b_have_skill_search(ctx, args):
    return _skill_search(ctx, args, include_equip=True, extended=False)


def b_check_skill_search2(ctx, args):
    return _skill_search(ctx, args, include_equip=False, extended=True)


def b_have_skill_search2(ctx, args):
    return _skill_search(ctx, args, include_equip=True, extended=True)


def _dedicated_skill_bounds(ctx) -> tuple[int, int]:
    db = getattr(ctx.program, "csv", None)
    if not db:
        return 3904, 3915
    start = to_int(db.resolve_constant("技能:専用技1", 3904))
    end = to_int(db.resolve_constant("技能:専用技12", start + 11))
    return start, end


def _skill_slot_name(slot: int) -> str:
    return f"装備技能{slot - 20}" if slot > 20 else f"技能{slot}"


def _skill_name_from_script(ctx, skill: int, chara: int) -> str:
    _ctx_set_var(ctx, "RESULTS", [], "")
    _ctx_set_var(ctx, "RESULTS", [0], "")
    _call_script_procedure_live(ctx, f"SKILL_NAME_{skill}", [chara])
    return to_str(_ctx_get_var(ctx, "RESULTS", []))


def _regen_rank_for_skill(ctx, chara: int, kind: str, skill: int) -> int:
    _ctx_set_var(ctx, "RESULT", [], 0)
    _ctx_set_var(ctx, "RESULT", [0], 0)
    start, end = _dedicated_skill_bounds(ctx)
    if start <= skill <= end:
        name = _skill_name_from_script(ctx, skill, -1)
        suffix = to_str(_ctx_get_var(ctx, "CSTR", [chara, name])) if name else ""
    else:
        suffix = str(skill)
    if suffix:
        _ctx_set_var(ctx, "RESULT", [], 0)
        _ctx_set_var(ctx, "RESULT", [0], 0)
        _call_script_procedure_live(ctx, f"SKILL_{kind}_REGEN_RANK_{suffix}", [chara])
    return to_int(_ctx_get_var(ctx, "RESULT", []))


def b_search_skill_function(ctx, args):
    chara = to_int(arg(args, 0, 0))
    timing = to_str(arg(args, 1, ""))
    mode = to_int(arg(args, 2, 0))
    if chara < 0 or chara >= to_int(_ctx_get_var(ctx, "CHARANUM", [])):
        return 0
    skill_count = to_int(call_builtin(ctx, "CHARA_SKILLCOUNT", [chara]) or 0)
    start, end = _dedicated_skill_bounds(ctx)
    seen: set[int] = set()
    local = 1
    while local < 42:
        if (mode > 0 and local > 20) or (mode < 0 and local < 21):
            local += 1
            continue
        if local == skill_count + 1:
            local = 21
        skill = to_int(_ctx_get_var(ctx, "ABL", [chara, _skill_slot_name(local)]))
        if not skill and local > skill_count:
            break
        if skill in seen:
            local += 1
            continue
        seen.add(skill)
        suffix = ""
        if start <= skill <= end:
            suffix = to_str(_ctx_get_var(ctx, "CSTR", [chara, f"専用技{skill - start + 1}"]))
            if to_int(call_builtin(ctx, "PU_SKILLNUM_GET", [chara, suffix]) or 0) != skill:
                local += 1
                continue
        suffix = suffix if suffix else str(skill)
        _call_script_procedure_live(ctx, f"SKILL_{timing}_{suffix}", [chara])
        local += 1
    return 0


def _position_chara_by_flag(ctx, pos: int) -> int:
    return to_int(_ctx_get_var(ctx, "FLAG", [f"ポジション{pos}"]))


def b_multi_search_skill_function(ctx, args):
    timing = to_str(arg(args, 1, ""))
    mode = to_int(arg(args, 2, 0))
    start = to_int(arg(args, 3, 0))
    end = to_int(arg(args, 4, 0))
    skip_assi = to_int(arg(args, 5, 0)) != 0
    assi = to_int(_ctx_get_var(ctx, "ASSI", []))
    for pos in range(start, end + 1):
        user = _position_chara_by_flag(ctx, pos) if pos != end else assi
        if user < 0 or (pos != end and user == assi) or (pos == end and skip_assi):
            continue
        b_search_skill_function(ctx, [user, timing, mode])
    return 0


def b_skill_timing(ctx, args):
    timing = to_str(arg(args, 0, "BATTLE_START"))
    if timing not in {"BATTLE_START", "BATTLE_END", "TURNSTART", "TURNEND", "INITIALIZE"}:
        return 0
    b_multi_search_skill_function(ctx, [0, timing, 0, 1, 7 if timing == "INITIALIZE" else 17])
    return 0


def b_var_regenable_check(ctx, args):
    chara = to_int(arg(args, 0, 0))
    skill = to_int(arg(args, 1, 0))
    kind = unicodedata.normalize("NFKC", to_str(arg(args, 2, "HP")))
    if chara < 0 or skill < 0:
        return 0
    own_rank = _regen_rank_for_skill(ctx, chara, kind, skill)
    best_rank = 0
    skill_count = to_int(call_builtin(ctx, "CHARA_SKILLCOUNT", [chara]) or 0)
    for slot in range(1, skill_count + 1):
        best_rank = max(best_rank, _regen_rank_for_skill(ctx, chara, kind, to_int(_ctx_get_var(ctx, "ABL", [chara, f"技能{slot}"]))))
    for slot in range(1, 22):
        best_rank = max(best_rank, _regen_rank_for_skill(ctx, chara, kind, to_int(_ctx_get_var(ctx, "ABL", [chara, f"装備技能{slot}"]))))
    result = 0 if own_rank < best_rank else 1
    _ctx_set_var(ctx, "RESULT", [], result)
    _ctx_set_var(ctx, "RESULT", [0], result)
    return result


def b_var_regen(ctx, args):
    chara = to_int(arg(args, 0, 0))
    skill = to_int(arg(args, 1, 0))
    kind = unicodedata.normalize("NFKC", to_str(arg(args, 2, "ＨＰ")))
    amount = to_int(arg(args, 3, 0))
    mag_cost = to_int(arg(args, 4, 0))
    suppress_message = truth(arg(args, 5, 0))
    if b_var_regenable_check(ctx, [chara, skill, kind]) == 0:
        return 0
    mag_target = chara
    mag_text = ""
    if _cflag(ctx, chara, "PTフラグ") > 0 and mag_cost > 0:
        master = _master(ctx)
        mag_target = master if _cflag(ctx, chara, "ＭＡＧ自己消費") == 0 and _abl(ctx, chara, "種族") not in {0, 45} else chara
        if mag_target == master and to_int(_ctx_get_var(ctx, "BASE", [master, "ＭＡＧ"])) < mag_cost:
            mag_target = chara
        if to_int(_ctx_get_var(ctx, "BASE", [mag_target, "ＭＡＧ"])) < mag_cost:
            return 0
        mag_text = "MAG主人消費" if mag_target == master and master != chara else "MAG自己消費"
        _call_script_procedure_live(ctx, "CONTROL_MAG", [mag_target, -mag_cost])
    _ctx_set_var(ctx, "RESULTS", [], "")
    _ctx_set_var(ctx, "RESULTS", [0], "")
    if skill > 0:
        _call_script_procedure_live(ctx, f"SKILL_NAME_{skill}", [chara])
    if not suppress_message:
        _message_write(ctx, f"{to_str(_ctx_get_var(ctx, 'RESULTS', []))} {to_str(_ctx_get_var(ctx, 'CALLNAME', [chara]))} >>>>> {amount}回復 {mag_text}")
    _call_script_procedure_live(ctx, f"VAR_{kind}", [chara, amount, 3])
    return 0


def b_var_kaja(ctx, args):
    owner = to_int(arg(args, 0, 0))
    buff_no = min(to_int(arg(args, 1, 0)), 7)
    amount = to_int(arg(args, 2, 4))
    cap = to_int(arg(args, 3, 32))
    start_arg = to_int(arg(args, 4, 0))
    end_arg = to_int(arg(args, 5, 0))
    if start_arg < 1 or end_arg < 1:
        start = 1 if _cflag(ctx, owner, "PTフラグ") > 0 else 7
        end = 7 if _cflag(ctx, owner, "PTフラグ") > 0 else 17
    else:
        start, end = start_arg, end_arg
    raw_base = call_builtin(ctx, "GETNUM", ["CFLAG", "攻撃強化"])
    base = _csv_index(ctx, "CFLAG", "攻撃強化") if raw_base is None or to_int(raw_base) < 0 else to_int(raw_base)
    buff_type = _csv_name(ctx, "CFLAG", base + buff_no)
    for pos in range(start, end):
        chara = _position_chara_by_flag(ctx, pos)
        if chara < 0:
            continue
        current = to_int(_ctx_get_var(ctx, "CFLAG", [chara, buff_type]))
        _ctx_set_var(ctx, "CFLAG", [chara, buff_type], max(current, min(current + amount, cap)))
    return 0


def _skillgage_ready(ctx) -> bool:
    return (
        _skill_helpers_ready(ctx)
        and _has_csv_names(ctx, "TALENT", ("Persona使",))
        and _has_csv_names(ctx, "CFLAG", ("悪魔変身",))
        and _has_csv_names(ctx, "EQUIP", ("装備Persona", "所持Persona2", "所持Persona3"))
    )


def _skillgage_num(ctx, chara: int, skill: int) -> int | None:
    if not _skillgage_ready(ctx):
        return None
    pos = to_int(b_have_skill(ctx, [chara, skill, 1]))
    if pos == 0:
        return 0
    if pos > 20:
        pos += 9
    if pos < 13:
        if _talent(ctx, chara, "Persona使"):
            equipped = to_int(_ctx_get_var(ctx, "EQUIP", [chara, _script_index(ctx, "EQUIP", "装備Persona")]))
            if equipped == to_int(_ctx_get_var(ctx, "EQUIP", [chara, _script_index(ctx, "EQUIP", "所持Persona2")])):
                pos += 8
            elif equipped == to_int(_ctx_get_var(ctx, "EQUIP", [chara, _script_index(ctx, "EQUIP", "所持Persona3")])):
                pos += 16
        elif _cflag(ctx, chara, "悪魔変身") == 1:
            pos += 12
    return pos


def b_skillgage_num(ctx, args):
    return _skillgage_num(ctx, to_int(arg(args, 0, 0)), to_int(arg(args, 1, 0)))


def _skillgage_value(ctx, args, kind: str):
    num = _skillgage_num(ctx, to_int(arg(args, 0, 0)), to_int(arg(args, 1, 0)))
    if num is None:
        return None
    if num == 0:
        return 0
    return to_int(_ctx_get_var(ctx, "CFLAG", [to_int(arg(args, 0, 0)), f"技能ゲージ{kind}{num}"]))


def b_skillgage_h_get(ctx, args): return _skillgage_value(ctx, args, "H")
def b_skillgage_d_get(ctx, args): return _skillgage_value(ctx, args, "D")
def b_skillgage_f_get(ctx, args): return _skillgage_value(ctx, args, "F")


def _skillgage_getbit(ctx, args, kind: str):
    bit = to_int(arg(args, 2, -1))
    if bit < 0 or bit >= 64:
        return -1
    value = _skillgage_value(ctx, args, kind)
    if value is None:
        return None
    return (value >> bit) & 1 if _skillgage_num(ctx, to_int(arg(args, 0, 0)), to_int(arg(args, 1, 0))) else -1


def b_skillgage_h_getbit(ctx, args): return _skillgage_getbit(ctx, args, "H")
def b_skillgage_d_getbit(ctx, args): return _skillgage_getbit(ctx, args, "D")
def b_skillgage_f_getbit(ctx, args): return _skillgage_getbit(ctx, args, "F")


_SETTING_BITS = {
    "3SIZE": 0,
    "MAKKA_RATE": 1,
    "VELVET_STATUS_UP": 2,
}

_BATTLE_SETTING_BITS = {
    "TALENT": 0,
    "BAD_STATUS": 1,
    "1MORE": 2,
    "EQUIP_EFFECT": 3,
    "PERSONA_NEW_FUNCTION": 4,
    "EQUIPTHEORY": 5,
    "ITEM_DAMAGE": 6,
    "ITEM_HITRATE": 7,
}


def _bit_flag_value(ctx, flag_name: str) -> int:
    return to_int(_ctx_get_var(ctx, "FLAG", [flag_name]))


def _set_bit_flag_value(ctx, flag_name: str, value: int) -> None:
    _ctx_set_var(ctx, "FLAG", [flag_name], value)


def _setting_bit_from_call(ctx, prefix: str, mapping: dict[str, int]) -> tuple[str, int, str] | None:
    name = norm_name(getattr(ctx, "_builtin_call_name", ""))
    for op in ("IS", "SET", "INVERT"):
        head = f"{prefix}_{op}_"
        if name.startswith(head):
            suffix = name[len(head):]
            if suffix in mapping:
                return op, mapping[suffix], suffix
    return None


def _setting_switch(ctx, args, *, prefix: str, flag_name: str, mapping: dict[str, int]):
    spec = _setting_bit_from_call(ctx, prefix, mapping)
    if spec is None:
        return None
    op, bit, _suffix = spec
    value = _bit_flag_value(ctx, flag_name)
    if op == "IS":
        return 1 if value & (1 << bit) else 0
    if op == "INVERT":
        value ^= 1 << bit
    else:
        if truth(arg(args, 0, 0)):
            value |= 1 << bit
        else:
            value &= ~(1 << bit)
    _set_bit_flag_value(ctx, flag_name, value)
    return to_int(_ctx_get_var(ctx, "RESULT", []))


def b_setting_switch(ctx, args):
    return _setting_switch(ctx, args, prefix="SETTING", flag_name="其他設定スイッチ", mapping=_SETTING_BITS)


def b_battle_setting_switch(ctx, args):
    return _setting_switch(ctx, args, prefix="BATTLE_SETTING", flag_name="戦闘難易度関連設定开关", mapping=_BATTLE_SETTING_BITS)


def _skillgage_slot_name(kind: str, num: int) -> str:
    return f"技能ゲージ{kind}{to_int(num)}"


def _skillgage_slot_num(ctx, chara: int, skill: int) -> int | None:
    return _skillgage_num(ctx, chara, skill)


def _skillgage_get_slot(ctx, chara: int, kind: str, num: int) -> int:
    return to_int(_ctx_get_var(ctx, "CFLAG", [chara, _skillgage_slot_name(kind, num)]))


def _skillgage_set_slot(ctx, chara: int, kind: str, num: int, value: int) -> None:
    _ctx_set_var(ctx, "CFLAG", [chara, _skillgage_slot_name(kind, num)], to_int(value))


def _skillgage_mutate(ctx, args, kind: str, op: str):
    chara = to_int(arg(args, 0, 0))
    skill = to_int(arg(args, 1, 0))
    num = _skillgage_slot_num(ctx, chara, skill)
    if num is None:
        return None
    if num > 0:
        value = _skillgage_get_slot(ctx, chara, kind, num)
        operand = to_int(arg(args, 2, 0))
        if op == "=":
            value = operand
        elif op == "+":
            value += operand
        elif op == "-":
            value -= operand
        elif op == "*":
            value *= operand
        elif op == "/" and operand != 0:
            value = int(value / operand)
        elif op == "%" and operand != 0:
            value %= operand
        _skillgage_set_slot(ctx, chara, kind, num, value)
    return to_int(_ctx_get_var(ctx, "RESULT", []))


def _skillgage_bit_mutate(ctx, args, kind: str, op: str):
    chara = to_int(arg(args, 0, 0))
    skill = to_int(arg(args, 1, 0))
    bit = to_int(arg(args, 2, -1))
    num = _skillgage_slot_num(ctx, chara, skill)
    if num is None:
        return None
    if num > 0 and 0 <= bit < 64:
        value = _skillgage_get_slot(ctx, chara, kind, num)
        if op == "SET":
            value |= 1 << bit
        elif op == "CLEAR":
            value &= ~(1 << bit)
        elif op == "INVERT":
            value ^= 1 << bit
        _skillgage_set_slot(ctx, chara, kind, num, value)
    return to_int(_ctx_get_var(ctx, "RESULT", []))


def _skillgage_calc_op(args) -> str:
    op = to_str(arg(args, 3, "+"))
    return op if op in {"=", "+", "-", "*", "/", "%"} else "+"


def b_skillgage_h_set(ctx, args): return _skillgage_mutate(ctx, args, "H", "=")
def b_skillgage_d_set(ctx, args): return _skillgage_mutate(ctx, args, "D", "=")
def b_skillgage_f_set(ctx, args): return _skillgage_mutate(ctx, args, "F", "=")
def b_skillgage_h_add(ctx, args): return _skillgage_mutate(ctx, args, "H", "+")
def b_skillgage_d_add(ctx, args): return _skillgage_mutate(ctx, args, "D", "+")
def b_skillgage_f_add(ctx, args): return _skillgage_mutate(ctx, args, "F", "+")
def b_skillgage_h_calculation(ctx, args): return _skillgage_mutate(ctx, args, "H", _skillgage_calc_op(args))
def b_skillgage_d_calculation(ctx, args): return _skillgage_mutate(ctx, args, "D", _skillgage_calc_op(args))
def b_skillgage_f_calculation(ctx, args): return _skillgage_mutate(ctx, args, "F", _skillgage_calc_op(args))
def b_skillgage_h_setbit(ctx, args): return _skillgage_bit_mutate(ctx, args, "H", "SET")
def b_skillgage_d_setbit(ctx, args): return _skillgage_bit_mutate(ctx, args, "D", "SET")
def b_skillgage_f_setbit(ctx, args): return _skillgage_bit_mutate(ctx, args, "F", "SET")
def b_skillgage_h_clearbit(ctx, args): return _skillgage_bit_mutate(ctx, args, "H", "CLEAR")
def b_skillgage_d_clearbit(ctx, args): return _skillgage_bit_mutate(ctx, args, "D", "CLEAR")
def b_skillgage_f_clearbit(ctx, args): return _skillgage_bit_mutate(ctx, args, "F", "CLEAR")
def b_skillgage_h_invertbit(ctx, args): return _skillgage_bit_mutate(ctx, args, "H", "INVERT")
def b_skillgage_d_invertbit(ctx, args): return _skillgage_bit_mutate(ctx, args, "D", "INVERT")
def b_skillgage_f_invertbit(ctx, args): return _skillgage_bit_mutate(ctx, args, "F", "INVERT")


def b_skillgage_direct_swap(ctx, args):
    chara = to_int(arg(args, 0, 0))
    a = to_int(arg(args, 1, 0))
    b = to_int(arg(args, 2, 0))
    for kind in ("H", "D", "F"):
        va = _skillgage_get_slot(ctx, chara, kind, a)
        vb = _skillgage_get_slot(ctx, chara, kind, b)
        _skillgage_set_slot(ctx, chara, kind, a, vb)
        _skillgage_set_slot(ctx, chara, kind, b, va)
    return 0


def b_skillgage_swap(ctx, args):
    chara = to_int(arg(args, 0, 0))
    a = _skillgage_slot_num(ctx, chara, to_int(arg(args, 1, 0)))
    b = _skillgage_slot_num(ctx, chara, to_int(arg(args, 2, 0)))
    if a is None or b is None:
        return None
    return b_skillgage_direct_swap(ctx, [chara, a, b])


def b_skillgage_direct_clear(ctx, args):
    chara = to_int(arg(args, 0, 0))
    num = to_int(arg(args, 1, 0))
    for kind in ("H", "D", "F"):
        _skillgage_set_slot(ctx, chara, kind, num, 0)
    return 0


def b_skillgage_clear(ctx, args):
    chara = to_int(arg(args, 0, 0))
    num = _skillgage_slot_num(ctx, chara, to_int(arg(args, 1, 0)))
    if num is None:
        return None
    return b_skillgage_direct_clear(ctx, [chara, num])


def b_skillgage_charge(ctx, args):
    chara = to_int(arg(args, 0, 0))
    actor = to_int(arg(args, 1, chara))
    count = to_int(b_chara_skillcount(ctx, [chara]) or 0)
    for i in range(max(0, count + 20)):
        slot_name = f"技能{i + 1}" if i < count else f"装備技能{i - count + 1}"
        skill = to_int(_ctx_get_var(ctx, "ABL", [chara, slot_name]))
        if skill:
            _call_script_value(ctx, f"SKILLGAGE_CHARGE_{skill}", [chara, actor], 0)
    return to_int(_ctx_get_var(ctx, "RESULT", []))


def b_global_badend_init(ctx, args):
    _ctx_set_var(ctx, "GLOBAL", ["バッドエンド記録1"], 0)
    _ctx_set_var(ctx, "GLOBAL", ["バッドエンド記録2"], 0)
    _set_return_values(ctx, 1)
    return 1


def b_shopcomable_700(ctx, args):
    if truth(_ctx_get_var(ctx, "FLAG", ["DEBUG"])):
        _ctx_set_var(ctx, "RESULTS", [], "バッドエンド記録リセット")
        _ctx_set_var(ctx, "RESULT", [], 1)
        _ctx_set_var(ctx, "RESULT", [0], 1)
        return 1
    _ctx_set_var(ctx, "RESULT", [], -1)
    _ctx_set_var(ctx, "RESULT", [0], -1)
    return -1


def b_shop_com_700(ctx, args):
    b_global_badend_init(ctx, [])
    _message_write(ctx, "バットエンド記録をリセットしました")
    return 1


def b_shopcomable_701(ctx, args):
    if truth(_ctx_get_var(ctx, "FLAG", ["DEBUG"])):
        _ctx_set_var(ctx, "RESULTS", [], "旧あなた加入問題修正")
        _ctx_set_var(ctx, "RESULT", [], 1)
        _ctx_set_var(ctx, "RESULT", [0], 1)
        return 1
    _ctx_set_var(ctx, "RESULT", [], -1)
    _ctx_set_var(ctx, "RESULT", [0], -1)
    return -1


def _program_has_script_function(ctx, name: str) -> bool:
    try:
        ordered = getattr(ctx, "_ordered_functions", None)
        return bool(ordered(name)) if callable(ordered) else False
    except Exception:
        return False


def _copy_old_master_to_new_slot(ctx, old_chara: int, new_chara: int) -> None:
    fixed_id = _ctx_get_var(ctx, "CFLAG", [new_chara, "キャラ固有の番号"])
    mem = _ctx_memory(ctx)
    if hasattr(mem, "copy_chara"):
        mem.copy_chara(old_chara, new_chara)
    _ctx_set_var(ctx, "CFLAG", [new_chara, "キャラ固有の番号"], fixed_id)
    _ctx_set_var(ctx, "NO", [new_chara], 4998)
    b_character_delete(ctx, [old_chara])


def b_shop_com_701(ctx, args):
    # When the original ERB body is loaded, execute it verbatim.  The native
    # fallback below keeps small/unit fixtures useful when only BUILTINS are
    # present, while real E:\mgt runs retain the 600+ line specialty repair
    # table from FIX_ADD_MASTER.ERB.
    if _program_has_script_function(ctx, "SHOP_COM_701"):
        if _call_script_procedure_live(ctx, "SHOP_COM_701", list(args), max_steps=300000):
            return to_int(_ctx_get_var(ctx, "RESULT", []))

    _message_write(ctx, "本メニューは旧あなたに該当するキャラの固有番号と技能を修正します。")
    _message_write(ctx, "該当するキャラが複数人いる場合はその数だけ実施が必要となります。")
    _message_write(ctx, "(※該当キャラがいない場合は「修正対象となるキャラは存在しませんでした。」となります)")
    _message_write(ctx, "また、固有番号の修正は再加入させる形で行うため、加入可能人数(200人)より少ない状態で行ってください。")
    _message_write(ctx, "修正を実行してもよろしいですか？", newline=False)
    if to_int(b_input_yn(ctx, ["Yes", "NO"])) != 0:
        return 0

    status = 0
    charanum = to_int(_ctx_get_var(ctx, "CHARANUM", []))
    for chara in range(1, charanum):
        no = to_int(_ctx_get_var(ctx, "NO", [chara]))
        fixed = to_int(_ctx_get_var(ctx, "CFLAG", [chara, "キャラ固有の番号"]))
        if fixed == 0 or no == 4999:
            status = 1
            _ctx_set_var(ctx, "RESULT", [], 0)
            _ctx_set_var(ctx, "RESULT", [1], -1)
            _call_script_procedure_live(ctx, "ADD_NEW_COMPANION", [4998, 500], max_steps=50000)
            if to_int(_ctx_get_var(ctx, "RESULT", [])) == 1:
                new_chara = to_int(_ctx_get_var(ctx, "RESULT", [1]))
                if new_chara >= 0:
                    _copy_old_master_to_new_slot(ctx, chara, new_chara)
                    status = 2
                    break
        elif no == 4998:
            # The full script has a large branch table for every specialty.
            # Fallback fixtures at least replay the general summoner skill
            # repair hook and mark the target as processed.
            if to_int(_ctx_get_var(ctx, "CFLAG", [chara, "あなたの専攻分野"])) == 1:
                for bit in range(64):
                    if not (to_int(_ctx_get_var(ctx, "FLAG", ["主人獲得技能"])) & (1 << bit)):
                        continue
                    skill = _sync_call_result(ctx, "MASTER_SKILL", [bit], -1)
                    if skill >= 0:
                        _call_script_procedure_live(ctx, "LEARN_SKILL", [chara, skill], max_steps=10000)
            status = 3

    if status == 0:
        _message_write(ctx, "修正対象となるキャラは存在しませんでした。")
    elif status == 1:
        _message_write(ctx, "修正対象となるキャラが存在しますが再加入できませんでした。")
        _message_write(ctx, "キャラクター数を調整してから再実行してください。")
    elif status == 2:
        _message_write(ctx, "旧あなたに該当するキャラの固有番号を修正しました。")
    elif status == 3:
        _message_write(ctx, "旧あなたに該当する全てのキャラの取得技能を修正しました。")
    _set_return_values(ctx, status)
    return status


def b_global_badend_set(ctx, args):
    no = to_int(arg(args, 0, 0))
    if no < 64:
        value = to_int(_ctx_get_var(ctx, "GLOBAL", ["バッドエンド記録1"]))
        _ctx_set_var(ctx, "GLOBAL", ["バッドエンド記録1"], value | (1 << max(0, no)))
    else:
        bit = no - 64
        value = to_int(_ctx_get_var(ctx, "GLOBAL", ["バッドエンド記録2"]))
        if bit >= 0:
            _ctx_set_var(ctx, "GLOBAL", ["バッドエンド記録2"], value | (1 << bit))
    _set_return_values(ctx, 1)
    return 1


def b_global_badend_get(ctx, args):
    no = to_int(arg(args, 0, 0))
    if no < 64:
        result = 1 if (to_int(_ctx_get_var(ctx, "GLOBAL", ["バッドエンド記録1"])) & (1 << max(0, no))) else 0
    else:
        bit = no - 64
        # Preserve the current ERB helper's behavior: it adds 64 after GETBIT.
        result = (1 if bit >= 0 and (to_int(_ctx_get_var(ctx, "GLOBAL", ["バッドエンド記録2"])) & (1 << bit)) else 0) + 64
    _set_return_values(ctx, result)
    return result


def _global_badend_titles(ctx) -> dict[int, str]:
    cached = getattr(ctx, "_global_badend_titles", None)
    if cached is not None:
        return cached
    titles: dict[int, str] = {}
    root = getattr(getattr(ctx, "program", None), "root", None)
    path = root / "ERB" / "関数" / "私家版追加関数" / "GLOBAL_BADEND.ERB" if root is not None else None
    if path and path.exists():
        try:
            current: int | None = None
            for raw in read_text_auto(path).splitlines():
                m_case = re.match(r"\s*CASE\s+(\d+)\s*$", strip_comment(raw))
                if m_case:
                    current = int(m_case.group(1))
                    continue
                m_print = re.match(r"\s*PRINTL\s+(Bad Ending .*)$", strip_comment(raw))
                if current is not None and m_print:
                    titles[current] = m_print.group(1).strip()
                    current = None
        except Exception:
            pass
    try:
        setattr(ctx, "_global_badend_titles", titles)
    except Exception:
        pass
    return titles


def b_global_badend_disp_badendlist(ctx, args):
    _message_write(ctx, "─" * 72)
    _message_write(ctx, "発見済みのBADエンド")
    _message_write(ctx, "─" * 72)
    titles = _global_badend_titles(ctx)
    for no in range(127):
        if to_int(b_global_badend_get(ctx, [no])) == 0:
            continue
        if no in titles:
            _message_write(ctx, "　" + titles[no])
    _message_write(ctx, "")
    _set_return_values(ctx, 1)
    return 1


def b_get_state_kanji(ctx, args):
    idx = to_int(arg(args, 0, 0))
    short = truth(arg(args, 1, 0))
    full = ["正常", "幸福", "感電", "混乱", "睡眠", "凍結", "金縛", "沈黙", "猛毒", "爆弾", "魅了", "麻痺", "呪い", "蠅化", "石化", "死亡", "狂暴", "灼熱", "炎上", "転倒", "烙印"]
    brief = ["正", "幸", "感", "混", "眠", "凍", "縛", "黙", "毒", "爆", "魅", "痺", "呪", "蠅", "石", "死", "OR", "HE", "炎", "倒", "烙"]
    table = brief if short else full
    return table[idx] if 0 <= idx < len(table) else ("　" if short else "　　")


def b_state_color(ctx, args):
    idx = to_int(arg(args, 0, 0))
    table = {
        1: 0xFFCCCC, 2: 0xFFFF99, 3: 0x99CCFF, 4: 0x009999, 5: 0x0099CC,
        6: 0xCCCC00, 7: 0xCC99FF, 8: 0xCC00CC, 10: 0xFF00FF, 11: 0x66CC99,
        12: 0x6600FF, 13: 0xFFFF00, 14: 0x66CC99, 15: 0x990000, 16: 0xFFAA11,
        17: 0xAA0000, 18: 0xFF1E86, 19: 0xFFDE86, 20: 0xFF6400,
    }
    return table.get(idx, to_int(call_builtin(ctx, "GETDEFCOLOR", []) or 0xC0C0C0))


def b_change_ms_to_hhmiss(ctx, args):
    ms = max(0, to_int(arg(args, 0, 0)))
    total = ms // 1000
    hours = total // 3600
    if hours >= 100:
        return "99:59:59"
    minutes = (total // 60) % 60
    seconds = total % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def b_resist_one_char(ctx, args):
    value = to_int(arg(args, 0, 0))
    if value < 0:
        return "吸"
    if value == 0:
        return "無"
    if value < 100:
        return str(value)
    if value == 100:
        return "等"
    if value < 200:
        return "弱"
    if value == 999:
        return "反"
    return "倍"


def b_enemy_count(ctx, args):
    include_dead = truth(arg(args, 0, 0))
    total = 0
    for pos in range(7, 17):
        chara = to_int(_ctx_get_var(ctx, "FLAG", [f"ポジション{pos}"]))
        if chara < 0:
            continue
        if not include_dead and to_str(call_builtin(ctx, "GET_STATE", [_cflag(ctx, chara, "ステート")]) or "") == "瀕死":
            continue
        total += 1
    return total


def b_add_kmgt(ctx, args):
    value = to_int(arg(args, 0, 0))
    width = to_int(arg(args, 1, 5))
    text = f"{value:{width}d}" if width > 0 else str(value)
    if width < 4 or (value < 0 and width < 5) or value == 0:
        return text
    sign = "-" if value < 0 else ""
    n = abs(value)
    digits = len(str(n))
    if width + (1 if value > 0 else 0) > digits:
        out = str(n)
    elif digits > 4:
        short = n // (10 ** (digits - 3))
        unit = "KMGTPE"[max(0, min(5, ((digits - 1) // 3) - 1))]
        ones = short % 10
        tens = (short // 10) % 10
        hundreds = short // 100
        out = str(ones) + unit
        if digits % 3 == 2:
            out = "." + out
        if tens or hundreds:
            out = str(tens) + out
        if digits % 3 == 1:
            out = "." + out
        if hundreds:
            out = str(hundreds) + out
    else:
        out = str(n)
    out = sign + out
    return (" " * max(width - _locale_strlen(ctx, out), 0)) + out


def b_s_name(ctx, args):
    chara = to_int(arg(args, 0, -1))
    width = to_int(arg(args, 1, 0))
    mode = to_int(arg(args, 2, 0))
    fallback = to_str(arg(args, 3, ""))
    if not (0 <= chara < to_int(_ctx_get_var(ctx, "CHARANUM", []))):
        return fallback
    name = to_str(_ctx_get_var(ctx, "NAME", [chara]))
    callname = to_str(_ctx_get_var(ctx, "CALLNAME", [chara]))
    hname = unicodedata.normalize("NFKC", name)
    hcall = unicodedata.normalize("NFKC", callname)
    if _locale_strlen(ctx, name) <= width and mode != 2:
        return name
    if _locale_strlen(ctx, hname) <= width and mode != 2:
        return hname
    if _locale_strlen(ctx, callname) <= width and mode != 1:
        return callname
    if _locale_strlen(ctx, hcall) <= width and mode != 1:
        return hcall
    return (hname if mode == 1 else hcall)[:max(0, width)]


def b_is_randomchara(ctx, args):
    no = to_int(_ctx_get_var(ctx, "NO", [to_int(arg(args, 0, 0))]))
    db = getattr(ctx.program, "csv", None)
    daughter = db.resolve_constant("キャラ:你的女兒", None) if db else None
    slave_daughter = db.resolve_constant("キャラ:奴隸的女兒", None) if db else None
    zouma = db.resolve_constant("キャラ:造魔", None) if db else None
    return 1 if (4901 <= no <= 4912 or no in {daughter, slave_daughter, zouma}) else 0


def b_lifting_a_ban(ctx, args):
    no = to_int(arg(args, 0, 0))
    if not truth(call_builtin(ctx, "EXISTCSV", [no]) or 0):
        _message_write(ctx, f"{no}番のCSVがありません")
        return 0
    if not truth(call_builtin(ctx, "CSVCFLAG", [no, 1165]) or 0):
        _message_write(ctx, f"{no}番にCFLAG合体条件有りは設定されていません")
        return 0
    slot = 10000 + no
    if to_int(_ctx_get_var(ctx, "FLAG", [slot])):
        return 0
    _ctx_set_var(ctx, "FLAG", [slot], 1)
    _message_write(ctx, "─" * 72)
    _message_write(ctx, f"{call_builtin(ctx, 'CSVCALLNAME', [no])}の合体を解禁しました。")
    _message_write(ctx, "─" * 72)
    return 0


def b_aion_skill_slot_check(ctx, args):
    chara = to_int(arg(args, 0, 0))
    slot = to_int(arg(args, 1, 0))
    if _talent(ctx, chara, "Aion式召喚術"):
        return -1
    if slot < 1 or slot > 4:
        return 0
    return 1


def b_aion_human_skill_reflect(ctx, args):
    chara = to_int(arg(args, 0, 0))
    for i in range(1, 5):
        _ctx_set_var(ctx, "ABL", [chara, f"人間時技能{i}"], to_int(_ctx_get_var(ctx, "ABL", [chara, f"技能{i}"])))
    return 0


def _equiptheory_skill_numbers(ctx) -> list[int]:
    db = getattr(ctx.program, "csv", None)
    out: list[int] = []
    if db:
        for lv in range(6):
            value = db.resolve_constant(f"技能:装備知識Lv{lv}", None)
            if value is None:
                value = db.resolve_constant(f"装備知識Lv{lv}", None)
            if value is not None:
                out.append(to_int(value))
    return out or [9000 + i for i in range(6)]


def b_skill_equiptheory_is_have_skill(ctx, args):
    chara = to_int(arg(args, 0, -1))
    if chara == -1:
        return 0
    return 1 if any(truth(call_builtin(ctx, "HAVE_SKILL", [chara, skill]) or 0) for skill in _equiptheory_skill_numbers(ctx)) else 0


def b_skill_equiptheory_is_skill(ctx, args):
    skill = to_int(arg(args, 0, 0))
    return 1 if skill in set(_equiptheory_skill_numbers(ctx)) else 0


def b_skill_equiptheory_del_skill(ctx, args):
    chara = to_int(arg(args, 0, -1))
    skill = to_int(arg(args, 1, 0))
    if chara != -1 and truth(call_builtin(ctx, "GET_DEVIL", [chara]) or 0) and b_skill_equiptheory_is_skill(ctx, [skill]) and not b_skill_equiptheory_is_have_skill(ctx, [chara]):
        _call_script_procedure(ctx, "全装備解除", [chara])
        _message_write(ctx, f"{to_str(_ctx_get_var(ctx, 'CALLNAME', [chara]))}は装備知識の技能を失ったため、装備が解除されました。")
    return 0


def _equiptheory_level(ctx, chara: int) -> int | None:
    nums = _equiptheory_skill_numbers(ctx)
    for lv in range(min(5, len(nums) - 1), -1, -1):
        if truth(call_builtin(ctx, "HAVE_SKILL", [chara, nums[lv]]) or 0):
            return lv
    return None


def _equiptheory_adjust(ctx, args, *, minimum: int) -> int:
    chara = to_int(arg(args, 0, -1))
    value = to_int(arg(args, 1, minimum))
    if chara == -1:
        return minimum
    race = _abl(ctx, chara, "種族")
    lv = _equiptheory_level(ctx, chara)
    if race in {0, 36}:
        if to_int(_ctx_get_var(ctx, "FLAG", ["人間戦闘ステ設定"])) == 3:
            bonus = 125 if lv == 5 else 100
            value = int(value * (bonus + int(to_int(_ctx_get_var(ctx, "BASE", [chara, "LV"])) / 2)) / 100)
        elif lv == 5:
            value = int(value * 1.25)
    else:
        factors = {5: 1.25, 4: 1.0, 3: 0.75, 2: 0.5, 1: 0.25, 0: 0.1}
        if lv is not None:
            value = int(value * factors.get(lv, 1.0))
    return max(minimum, value)


def b_skill_equiptheory_equip_status(ctx, args):
    return _equiptheory_adjust(ctx, args, minimum=1)


def b_skill_equiptheory_equip_hit(ctx, args):
    return _equiptheory_adjust(ctx, args, minimum=25)


def b_matching_weapon_check(ctx, args):
    chara = to_int(arg(args, 0, 0))
    if _abl(ctx, chara, "種族") not in {0, 36} or _cflag(ctx, chara, "悪魔変身") > 0:
        _set_return_values(ctx, 0)
        return 0
    sword = to_int(_ctx_get_var(ctx, "EQUIP", [chara, "剣"]))
    result = to_int(_call_script_value(ctx, f"剣タイプ_{sword}", [], 0))
    out = result if 0 < result < 12 and to_int(_ctx_get_var(ctx, "TALENT", [chara, 249 + result])) > 0 else 0
    _set_return_values(ctx, out)
    return out


def b_weapon_style_check(ctx, args):
    chara = to_int(arg(args, 0, 0))
    style = to_int(arg(args, 1, 0))
    if _abl(ctx, chara, "種族") not in {0, 36} or _cflag(ctx, chara, "悪魔変身") > 0 or not (1 <= style <= 11):
        _set_return_values(ctx, 0)
        return 0
    sword = to_int(_ctx_get_var(ctx, "EQUIP", [chara, "剣"]))
    result = to_int(_call_script_value(ctx, f"剣タイプ_{sword}", [], 0))
    out = to_int(_ctx_get_var(ctx, "TALENT", [chara, 249 + result])) if 0 < result < 12 and style == result else 0
    _set_return_values(ctx, out)
    return out


def b_weapon_check_mix(ctx, args):
    chara = to_int(arg(args, 0, 0))
    matched = to_int(b_matching_weapon_check(ctx, [chara]))
    style = to_int(b_weapon_style_check(ctx, [chara, matched]))
    out = matched + style * 100
    _set_return_values(ctx, out)
    return out


def b_get_charaparam(ctx, args):
    no = to_int(arg(args, 0, 0))
    param = to_str(arg(args, 1, ""))
    preferred = to_str(arg(args, 4, ""))
    raw_found = call_builtin(ctx, "FINDCHARA_B", [no, arg(args, 2, -100), arg(args, 3, 0)])
    found = -1 if raw_found is None else to_int(raw_found)
    chara = to_int(_ctx_get_var(ctx, "RESULT", [1]))
    if found < 0:
        return -1
    order = [preferred] if preferred in {"ABL", "BASE", "CFLAG", "TALENT", "EXP", "MARK"} else ["ABL", "BASE", "CFLAG", "TALENT", "EXP", "MARK"]
    if preferred and len(order) == 1:
        order += [x for x in ["ABL", "BASE", "CFLAG", "TALENT", "EXP", "MARK"] if x != preferred]
    for base in order:
        raw_index = call_builtin(ctx, "GETNUM", [base, param])
        if raw_index is not None and to_int(raw_index) > -1:
            return to_int(_ctx_get_var(ctx, base, [chara, param]))
    return -1


def b_womb_capacity_init(ctx, args):
    chara = to_int(arg(args, 0, 0))
    if _cflag(ctx, chara, "子宮最大容量") <= 0:
        value = 50
        if _talent(ctx, chara, "小人体型"):
            value = 10
        elif _talent(ctx, chara, "体型嬌小"):
            value = 30
        elif _talent(ctx, chara, "高大"):
            value = 65
        elif _talent(ctx, chara, "巨人"):
            value = 80
        if _talent(ctx, chara, "容易懷孕"):
            value += 50
        _ctx_set_var(ctx, "CFLAG", [chara, "子宮最大容量"], value)
    return 0


def b_is_anti_ntr_clothes(ctx, args):
    chara = to_int(arg(args, 0, -1))
    if chara < 0 or chara > to_int(_ctx_get_var(ctx, "CHARANUM", [])) - 1:
        return 0
    db = getattr(ctx.program, "csv", None)
    def c(name: str) -> int:
        return to_int(db.resolve_constant(f"衣装:{name}", 0) if db else 0)
    protect = {c("貞節の耳環"), c("貞操の前張り"), c("鉄壁裙"), c("純潔の袖扣"), c("專属奴隸項圈")}
    induce = {c("繁殖礼賛耳札"), c("生殖歓迎札"), c("避孕套腰ミノ"), c("淫蕩の念珠"), c("背徳戒指")}
    slots = ["帽子", "内衣（下）", "下衣", "手", "其他", "其他2", "其他3"]
    values = [to_int(_ctx_get_var(ctx, base, [chara, slot])) for base in ("CFLAG", "TEQUIP") for slot in slots]
    if any(v in protect and v != 0 for v in values):
        return 1
    if any(v in induce and v != 0 for v in values):
        return -1
    return 0


def b_get_stain(ctx, args):
    bit = _STAIN_BITS.get(to_str(arg(args, 1, "")))
    if bit is None:
        return 0
    chara = to_int(arg(args, 2, -99))
    if chara == -99:
        chara = to_int(_ctx_get_var(ctx, "TARGET", []))
    # The ERB helper leaves LOCAL at its zero default when the body-part name
    # is not matched, so unknown-but-valid stain kinds probe slot 0.
    slot = _STAIN_BODY_SLOTS.get(to_str(arg(args, 0, "")), 0)
    return 1 if (to_int(_ctx_get_var(ctx, "STAIN", [chara, slot])) & (1 << bit)) else 0


def _stain_chara(ctx, value: Value = -99) -> int:
    chara = to_int(value)
    return to_int(_ctx_get_var(ctx, "TARGET", [])) if chara == -99 else chara


def _stain_slot(name: Value) -> int:
    # The ERB helpers leave LOCAL at its zero default for unknown body names.
    return _STAIN_BODY_SLOTS.get(to_str(name), 0)


def b_set_stain(ctx, args):
    chara = _stain_chara(ctx, arg(args, 2, -99))
    slot = _stain_slot(arg(args, 0, ""))
    bit = _STAIN_BITS.get(to_str(arg(args, 1, "")))
    if bit is not None:
        value = to_int(_ctx_get_var(ctx, "STAIN", [chara, slot]))
        _ctx_set_var(ctx, "STAIN", [chara, slot], value | (1 << bit))
    return 0


def b_move_stain(ctx, args):
    dest_body = arg(args, 0, "")
    dest_chara = to_int(arg(args, 1, 0))
    source_body = arg(args, 2, "")
    source_chara = to_int(arg(args, 3, 0))
    dest_slot = _stain_slot(dest_body)
    source_slot = _stain_slot(source_body)
    merged = to_int(_ctx_get_var(ctx, "STAIN", [dest_chara, dest_slot])) | to_int(_ctx_get_var(ctx, "STAIN", [source_chara, source_slot]))
    _ctx_set_var(ctx, "STAIN", [dest_chara, dest_slot], merged)
    _ctx_set_var(ctx, "STAIN", [source_chara, source_slot], merged)
    return 0


def b_dirty(ctx, args):
    body = arg(args, 0, "")
    chara = _stain_chara(ctx, arg(args, 1, -99))
    count = 0
    mask = 0
    checks = [
        ("陰茎", 2, to_str(body) != "陰茎"),
        ("精液", 4, True),
        ("肛門", 8, to_str(body) != "肛門"),
        ("粘液", 32, True),
        ("破瓜の血", 64, True),
    ]
    for stain, flag, enabled in checks:
        if enabled and b_get_stain(ctx, [body, stain, chara]):
            count += 1
            mask |= flag
    _ctx_set_var(ctx, "RESULT", [], count)
    _ctx_set_var(ctx, "RESULT", [1], mask)
    _ctx_set_var(ctx, "RESULTS", [], str(count))
    return count


def _target_if_zero(ctx, value: Value) -> int:
    """Mirror eraMegaten's `ARG = ARG ? ARG # TARGET` helper default idiom."""
    chara = to_int(value)
    return to_int(_ctx_get_var(ctx, "TARGET", [])) if chara == 0 else chara


def b_ini(ctx, args):
    # INI uses a dynamic string segment: FLAG:("行動順" + TOSTR(ARG)).
    return to_int(_ctx_get_var(ctx, "FLAG", [f"行動順{to_int(arg(args, 0, 0))}"]))


_FLAG_RESET_GROUPS = (
    ("ダンジョン出現1", "ダンジョン出現2"),
    ("闘技場出現1", "闘技場出現2"),
    ("事件出現1", "事件出現2"),
    ("依頼出現1", "依頼出現2"),
)


def b_flag_reset(ctx, args):
    no = to_int(arg(args, 0, 0))
    kind = to_int(arg(args, 1, 0))
    if kind < 0 or kind >= len(_FLAG_RESET_GROUPS):
        return 0
    first, second = _FLAG_RESET_GROUPS[kind]
    name = first if no <= 63 else second
    bit = no if no <= 63 else no - 64
    if bit >= 0:
        idx = _script_index(ctx, "FLAG", name)
        value = to_int(_ctx_get_var(ctx, "FLAG", [idx]))
        _ctx_set_var(ctx, "FLAG", [idx], value & ~(1 << bit))
    return 0


def b_set_kojo_function_cflag(ctx, args):
    # The original guard is `<= 200 && >= 900`, so it is never true; preserve
    # the effective behavior and simply return CFLAG:chara:KOJO_FUNCTION使用.
    chara = _target_if_zero(ctx, arg(args, 0, 0))
    return to_int(_ctx_get_var(ctx, "CFLAG", [chara, _script_index(ctx, "CFLAG", "KOJO_FUNCTION使用")]))


def _kojo_flag_slot(ctx, chara: int, flag_no: int, *, offset: int = 0) -> tuple[int, int]:
    base = to_int(b_set_kojo_function_cflag(ctx, [chara]) or 0)
    return int(flag_no / 63) + base + offset, flag_no % 63


def _set_cflag_bit(ctx, chara: int, slot: int, bit: int, enabled: bool) -> None:
    value = to_int(_ctx_get_var(ctx, "CFLAG", [chara, slot]))
    if enabled:
        value |= 1 << bit
    else:
        value &= ~(1 << bit)
    _ctx_set_var(ctx, "CFLAG", [chara, slot], value)


def _get_mutating_kojo_flag(ctx, args, *, offset: int = 0) -> int:
    flag_no = to_int(arg(args, 0, 0))
    mode = to_int(arg(args, 1, 0))
    chara = _target_if_zero(ctx, arg(args, 2, 0))
    if flag_no >= 1260 or flag_no < 0 or chara < 0:
        return 0
    slot, bit = _kojo_flag_slot(ctx, chara, flag_no, offset=offset)
    old_value = to_int(_ctx_get_var(ctx, "CFLAG", [chara, slot]))
    existed = 1 if (old_value & (1 << bit)) else 0
    if mode == 1:
        _set_cflag_bit(ctx, chara, slot, bit, True)
    elif mode == -1:
        _set_cflag_bit(ctx, chara, slot, bit, False)
    return existed


def b_get_comflag(ctx, args):
    return _get_mutating_kojo_flag(ctx, args, offset=0)


def b_get_eventflag(ctx, args):
    return _get_mutating_kojo_flag(ctx, args, offset=20)


def b_cini(ctx, args):
    if not _has_csv_name(ctx, "CFLAG", "行動順"):
        return None
    return _cflag(ctx, to_int(arg(args, 0, 0)), "行動順")


def _cstr(ctx, chara: int, segment: int | str) -> str:
    return to_str(_ctx_get_var(ctx, "CSTR", [chara, segment]))


def _no(ctx, chara: int) -> int:
    return to_int(_ctx_get_var(ctx, "NO", [chara]))


def _csv_callname(ctx, chara: int) -> str:
    return to_str(b_csvcallname(ctx, [_no(ctx, chara), 0]) or "")


def _relation_has(ctx, chara: int, field: str, needle: str) -> bool:
    return ("_" + needle) in ("_" + _cstr(ctx, chara, field))


def _relation_groups(ctx, chara: int) -> list[str]:
    text = _cstr(ctx, chara, "相性グループ")
    return [x for x in text.split("_") if x]


def b_is_relation_group(ctx, args):
    if not _has_csv_name(ctx, "CSTR", "相性グループ"):
        return None
    chara = _target_or_arg(ctx, arg(args, 0, -1))
    group = to_str(arg(args, 1, ""))
    return b_strcount(ctx, ["_" + _cstr(ctx, chara, "相性グループ"), "_" + group])


def _other_relation_group(ctx, other: int, name: str) -> bool:
    return bool(b_is_relation_group(ctx, [other, name]))


def _csv_level(ctx, chara: int) -> int:
    lv_idx = _csv_index(ctx, "BASE", "LV")
    return to_int(b_csvbase(ctx, [_no(ctx, chara), lv_idx, 0]) or 0)


def b_get_relation_group(ctx, args):
    if not _has_csv_name(ctx, "CSTR", "相性グループ"):
        return None
    target = _target_or_arg(ctx, arg(args, 0, -1))
    other = to_int(arg(args, 1, -1))
    if other == -1:
        other = to_int(_ctx_get_var(ctx, "PLAYER", []))
    group = to_str(arg(args, 2, ""))
    callname = _csv_callname(ctx, other)

    if group == "高天原":
        if callname in {"伊邪那美", "伊邪那岐", "天照"}:
            return 200
        if callname == "須佐之男":
            return 50
        if _other_relation_group(ctx, other, "高天原"):
            return 150
        if _other_relation_group(ctx, other, "豊葦原"):
            return 75
    elif group == "豊葦原":
        if callname in {"伊邪那美", "伊邪那岐", "須佐之男"}:
            return 200
        if callname == "天照":
            return 50
        if _other_relation_group(ctx, other, "豊葦原"):
            return 150
        if _other_relation_group(ctx, other, "高天原"):
            return 75
    elif group == "ヘブライ天使":
        if callname == "YHVH":
            return 200
        if callname == "路西法":
            return 50
        if _other_relation_group(ctx, other, "ヘブライ天使"):
            return 150 if _csv_level(ctx, target) < _csv_level(ctx, other) else 125
        if _other_relation_group(ctx, other, "ソロモン72柱"):
            return 75
    elif group == "ソロモン72柱":
        if callname == "YHVH":
            return 50
        if _other_relation_group(ctx, other, "ソロモン72柱") or _other_relation_group(ctx, other, "ヘブライ天使"):
            return 75
    elif group == "アムシャ・スプンタ":
        if callname == "阿胡拉馬茲達":
            return 200
        if callname == "祖爾宛":
            return 50
        if _other_relation_group(ctx, other, "アムシャ・スプンタ"):
            return 150
        if _other_relation_group(ctx, other, "ゾ罗阿斯塔悪神"):
            return 75
    elif group == "ゾ罗阿斯塔悪神":
        if callname == "祖爾宛":
            return 200
        if callname == "阿胡拉馬茲達":
            return 50
        if _other_relation_group(ctx, other, "ゾ罗阿斯塔悪神"):
            return 150
        if _other_relation_group(ctx, other, "アムシャ・スプンタ"):
            return 75
    elif group == "アス風刃ド":
        if callname == "奧丁":
            return 200
        if callname == "洛基":
            return 50
        if _other_relation_group(ctx, other, "アス風刃ド"):
            return 150
        if _other_relation_group(ctx, other, "諸神黄昏"):
            return 75
    elif group == "諸神黄昏":
        if callname == "洛基":
            return 200
        if callname == "奧丁":
            return 50
        if _other_relation_group(ctx, other, "諸神黄昏"):
            return 150
        if _other_relation_group(ctx, other, "アス風刃ド"):
            return 75
    elif group == "猫":
        if _other_relation_group(ctx, other, "猫"):
            return 150
        if _other_relation_group(ctx, other, "犬"):
            return 75
    elif group == "犬":
        if _other_relation_group(ctx, other, "犬"):
            return 150 if _csv_level(ctx, target) < _csv_level(ctx, other) else 125
        if _other_relation_group(ctx, other, "猿"):
            return 75
    elif group == "猿":
        if _other_relation_group(ctx, other, "猿"):
            return 150
        if _other_relation_group(ctx, other, "犬"):
            return 75
    elif group == "エリン":
        if _other_relation_group(ctx, other, "エリン"):
            return 125
    elif group == "ジャック一家":
        if _other_relation_group(ctx, other, "ジャックシ莉兹"):
            return 150
    elif group == "吸血鬼":
        if callname == "吸血鬼猟人・庫雷什尼克":
            return 75
    return 100


def b_get_relation(ctx, args):
    required_cstr = ("相性_最高", "相性_抜群", "相性_良好", "相性_不良", "相性_最悪", "相性グループ")
    if not _has_csv_names(ctx, "CSTR", required_cstr) or not _has_csv_names(ctx, "CFLAG", ("キャラ固有の番号",)):
        return None
    target = _target_or_arg(ctx, arg(args, 0, -1))
    other = to_int(arg(args, 1, -1))
    if other == -1:
        other = to_int(_ctx_get_var(ctx, "PLAYER", []))
    third = to_int(arg(args, 2, -1))
    callname = _csv_callname(ctx, other)
    relation = 100
    for field, value in (
        ("相性_最高", 200),
        ("相性_抜群", 150),
        ("相性_良好", 125),
        ("相性_不良", 75),
        ("相性_最悪", 50),
    ):
        if _relation_has(ctx, target, field, callname):
            relation = value
            break
    else:
        spouse = b_csv_spouse(ctx, [target, other])
        if spouse:
            relation = 200
        elif _sync_call_result(ctx, "近親チェック", [target, other], 0):
            relation = 150
        elif _cstr(ctx, other, "相性グループ") != "":
            for group in _relation_groups(ctx, other):
                for field, value in (
                    ("相性_最高", 200),
                    ("相性_抜群", 150),
                    ("相性_良好", 125),
                    ("相性_不良", 75),
                    ("相性_最悪", 50),
                ):
                    if _relation_has(ctx, target, field, group):
                        relation = value
                        break
                if relation != 100:
                    break
            if relation == 100 and _cstr(ctx, target, "相性グループ") != "":
                for group in _relation_groups(ctx, other):
                    relation = to_int(b_get_relation_group(ctx, [target, other, group]) or 100)
                    if relation != 100:
                        break
    for slot in range(1, 21):
        if _cflag(ctx, target, f"キャラ相性{slot}") < 0:
            continue
        if _cflag(ctx, target, f"キャラ相性{slot}") == _cflag(ctx, other, "キャラ固有の番号"):
            relation = _cflag(ctx, target, f"キャラ相性値{slot}")
            break
    if third > -1:
        return relation
    if not _has_csv_names(ctx, "ABL", ("属性LD", "属性LC")):
        return None
    ld_a = _abl(ctx, target, _script_index(ctx, "ABL", "属性LD"))
    ld_b = _abl(ctx, other, _script_index(ctx, "ABL", "属性LD"))
    if ld_a == 2 and ld_b == 2:
        relation += 10
    elif ld_a == ld_b:
        relation += 25
    elif (ld_a, ld_b) in {(1, 3), (3, 1)}:
        relation -= 25
    lc_a = _abl(ctx, target, _script_index(ctx, "ABL", "属性LC"))
    lc_b = _abl(ctx, other, _script_index(ctx, "ABL", "属性LC"))
    if lc_a == 2 and lc_b == 2:
        relation += 10
    elif lc_a == lc_b:
        relation += 25
    elif (lc_a, lc_b) in {(1, 3), (3, 1)}:
        relation -= 25
    return relation


def b_get_mark_way(ctx, args):
    mark = to_int(arg(args, 0, 0))
    chara = to_int(arg(args, 1, 0)) or to_int(_ctx_get_var(ctx, "TARGET", []))
    if mark > 3 or mark < 0:
        return 0
    tflag24 = to_int(_ctx_get_var(ctx, "TFLAG", [24]))
    if mark == 0 and tflag24:
        return tflag24
    local = 0
    for flag_no in range(mark * 3 + 997, mark * 3 + 1000):
        local += 1
        if b_get_eventflag(ctx, [flag_no, 0, chara]):
            return local
    return 0


def b_get_charasellable(ctx, args):
    names = ("ボスフラグ", "この場に居ないフラグ", "労役フラグ", "売却可能", "売却不可フラグ")
    if not _has_csv_names(ctx, "CFLAG", names):
        return None
    chara = to_int(arg(args, 0, 0))
    if chara == _master(ctx):
        return 0
    if _cflag(ctx, chara, "ボスフラグ"):
        return 0
    if _cflag(ctx, chara, "この場に居ないフラグ") == 1:
        return 0
    if _sync_call_result(ctx, "CHECK_CHILD_CARE", [chara], 0) and _has_csv_name(ctx, "FLAG", "出産機能ONOFF") and _flag(ctx, "出産機能ONOFF") == 1:
        return 0
    if _cflag(ctx, chara, "労役フラグ") == 3:
        return 0
    if _cflag(ctx, chara, "売却可能") == 0:
        return 0
    if _cflag(ctx, chara, "売却不可フラグ"):
        return 0
    return 1


def b_video_com_include_cflag(ctx, args):
    chara = to_int(arg(args, 0, 0))
    for slot in range(10):
        if to_int(_ctx_get_var(ctx, "CFLAG", [chara, slot + 1500 - 1])):
            return slot
    return 0


def b_video_com_include_tcvar(ctx, args):
    chara = to_int(arg(args, 0, 0))
    for slot in range(10):
        if to_int(_ctx_get_var(ctx, "TCVAR", [chara, slot + 110 - 1])):
            return slot
    return 0


def _equip(ctx, chara: int, segment: int | str) -> int:
    idx = segment if isinstance(segment, int) else _script_index(ctx, "EQUIP", segment)
    return to_int(_ctx_get_var(ctx, "EQUIP", [chara, idx]))


def _exp(ctx, chara: int, segment: int | str) -> int:
    idx = segment if isinstance(segment, int) else _script_index(ctx, "EXP", segment)
    return to_int(_ctx_get_var(ctx, "EXP", [chara, idx]))


def _palam(ctx, chara: int, segment: int | str) -> int:
    idx = segment if isinstance(segment, int) else _script_index(ctx, "PALAM", segment)
    return to_int(_ctx_get_var(ctx, "PALAM", [chara, idx]))


_ERO_EQUIP_BODY_ITEMS = (
    "高叉甲冑",
    "娼婦之服",
    "戦闘短装",
    "無敵納米裙",
    "尖刺文胸",
    "战斗吊带衫",
    "高开衩内衣",
)


def b_is_eroequip_f(ctx, args):
    if not _has_csv_name(ctx, "EQUIP", "胴"):
        return None
    chara = to_int(arg(args, 0, -1))
    if chara == -1:
        chara = to_int(_ctx_get_var(ctx, "PLAYER", []))
    body = _equip(ctx, chara, "胴")
    return 1 if any(body == _script_index(ctx, "ITEM", name) for name in _ERO_EQUIP_BODY_ITEMS) else 0


def b_item_use_requirement(ctx, args):
    if not _has_csv_names(ctx, "CFLAG", ("PTフラグ", "物品使用能力")):
        return None
    chara = to_int(arg(args, 0, 0))
    skill = to_int(arg(args, 1, 0))
    item_no = to_int(arg(args, 2, 0))
    required_ability = to_int(arg(args, 3, 1))
    amount = to_int(arg(args, 4, 1))
    have = b_have_skill(ctx, [chara, skill])
    if _cflag(ctx, chara, "PTフラグ") == 0 or truth(have):
        return 1
    if have is None:
        return None
    if to_int(_ctx_get_var(ctx, "ITEM", [item_no])) < amount or _cflag(ctx, chara, "物品使用能力") < required_ability:
        return 0
    return 1


def _dirty(ctx, body: str, chara: int) -> int:
    return to_int(b_dirty(ctx, [body, chara]) or 0)


def _talent_slot(ctx, chara: int, slot: int) -> int:
    return to_int(_ctx_get_var(ctx, "TALENT", [chara, slot]))


def _small_body_gate(ctx, actor: int, target: int, exp_name: str) -> bool:
    # Source uses fixed TALENT slots: 142 小人体型, 144 禁忌的知識.
    return bool(
        _talent_slot(ctx, target, 142)
        and ((_talent_slot(ctx, actor, 144) == 0 or actor != _master(ctx)) and (_talent_slot(ctx, actor, 142) == 0 and _exp(ctx, target, exp_name) < 10))
    )


def _lubrication_gate(ctx, actor: int, target: int, strict: int) -> bool:
    assi = to_int(_ctx_get_var(ctx, "ASSI", []))
    palamlv2 = to_int(_ctx_get_var(ctx, "PALAMLV", [2]))
    if _palam(ctx, target, 4) < palamlv2 and actor == assi and not b_is_male(ctx, [actor]):
        # Source uses fixed slots: ABL:0 従順, ABL:11 百合属性, TALENT:83 抖Ｓ.
        return (_abl(ctx, actor, 0) <= strict or _abl(ctx, actor, 11) <= strict) and not _talent_slot(ctx, actor, 83)
    return False


def b_play_analsex(ctx, args):
    actor = to_int(arg(args, 0, 0))
    target = to_int(arg(args, 1, 0))
    if _small_body_gate(ctx, actor, target, "Ａ拡張経験"):
        return 0
    if _lubrication_gate(ctx, actor, target, 3):
        return 0
    if not b_use_anus(ctx, [target]) or (not b_use_penis(ctx, [actor]) and not b_use_pband(ctx, [actor])):
        return 0
    return 1


def b_play_cunni(ctx, args):
    actor = to_int(arg(args, 0, 0))
    target = to_int(arg(args, 1, 0))
    master = _master(ctx)
    assi = to_int(_ctx_get_var(ctx, "ASSI", []))
    if _dirty(ctx, "陰道", target) and actor == master and _talent(ctx, actor, "汚臭無視") == 0:
        return 0
    if _dirty(ctx, "陰道", target) and actor == assi:
        if _abl(ctx, actor, "従順") <= 3 and _talent(ctx, actor, "汚臭敏感") and _talent(ctx, actor, "汚臭無視") == 0:
            return 0
    if not b_use_mouth(ctx, [actor]):
        return 0
    if not b_use_cli(ctx, [target, 1]):
        return 0
    return 1


def b_play_fella(ctx, args):
    actor = to_int(arg(args, 0, 0))
    target = to_int(arg(args, 1, 0))
    master = _master(ctx)
    assi = to_int(_ctx_get_var(ctx, "ASSI", []))
    if _dirty(ctx, "陰茎", target) and actor == master and _talent(ctx, actor, "汚臭無視") == 0:
        return 0
    if _dirty(ctx, "陰茎", target) and actor == assi:
        if _abl(ctx, actor, "従順") <= 3 and _talent(ctx, actor, "汚臭敏感") and _talent(ctx, actor, "汚臭無視") == 0:
            return 0
    if _talent(ctx, actor, "猫舌") and _abl(ctx, actor, "技巧") < 3:
        return 0
    if not b_use_mouth(ctx, [actor]):
        return 0
    if not b_use_penis(ctx, [target]):
        return 0
    return 1


def b_play_kiss(ctx, args):
    actor = to_int(arg(args, 0, 0))
    target = to_int(arg(args, 1, 0))
    if _dirty(ctx, "口", target) and not _talent(ctx, actor, "汚臭無視"):
        return 0
    if not b_use_mouth(ctx, [target]) or not b_use_mouth(ctx, [actor]):
        return 0
    return 1


def b_play_sex(ctx, args):
    actor = to_int(arg(args, 0, 0))
    target = to_int(arg(args, 1, 0))
    assi = to_int(_ctx_get_var(ctx, "ASSI", []))
    if _small_body_gate(ctx, actor, target, "Ｖ拡張経験"):
        return 0
    if _talent_slot(ctx, target, 0) and actor == assi and not b_is_male(ctx, [actor]):
        if (_abl(ctx, actor, 0) <= 4 or _abl(ctx, actor, 11) <= 4) and not _talent_slot(ctx, actor, 83):
            return 0
    if _lubrication_gate(ctx, actor, target, 3):
        return 0
    if not b_use_vagina(ctx, [target]) or (not b_use_penis(ctx, [actor]) and not b_use_pband(ctx, [actor])):
        return 0
    return 1


def _csvname_of_chara(ctx, chara: int) -> str:
    return to_str(b_csvname(ctx, [_no(ctx, chara), 0]) or "")


def _kin_label_for_child(term: str, other_is_male: bool) -> str:
    if term in {"兄弟", "息子"} and not other_is_male:
        return "姉妹" if term == "兄弟" else "娘"
    if term in {"姉妹", "娘"} and other_is_male:
        return "兄弟" if term == "姉妹" else "息子"
    return term


def _pregnancy_id(ctx, chara: int, name: str) -> int:
    return _cflag(ctx, chara, name)


def b_kinship_check(ctx, args):
    a = to_int(arg(args, 0, 0))
    b = to_int(arg(args, 1, 0))
    labels = ["", ""]
    pair = [a, b]
    for side in range(2):
        chara = pair[side]
        other = pair[1 - side]
        other_name = _csvname_of_chara(ctx, other)
        found = False
        for term in ("父", "母", "兄弟", "姉妹", "娘", "息子"):
            if to_str(b_autosplit(ctx, [_cstr(ctx, chara, f"近親判定_{term}"), "_", 0, other_name])) != "":
                labels[side] = _kin_label_for_child(term, bool(b_is_male(ctx, [other])))
                found = True
                break
        if found:
            continue
        father = _pregnancy_id(ctx, chara, "娘の父親の固有番号娘")
        mother = _pregnancy_id(ctx, chara, "娘の産みの親の固有番号娘")
        other_id = _cflag(ctx, other, "キャラ固有の番号")
        chara_id = _cflag(ctx, chara, "キャラ固有の番号")
        other_father = _pregnancy_id(ctx, other, "娘の父親の固有番号娘")
        other_mother = _pregnancy_id(ctx, other, "娘の産みの親の固有番号娘")
        if father == other_id:
            labels[side] = "父"
            labels[1 - side] = "息子" if b_is_male(ctx, [chara]) else "娘"
            break
        if mother == other_id:
            labels[side] = "母"
            labels[1 - side] = "息子" if b_is_male(ctx, [chara]) else "娘"
            break
        if chara_id == other_father:
            labels[side] = "息子" if b_is_male(ctx, [other]) else "娘"
            labels[1 - side] = "父"
            break
        if chara_id == other_mother:
            labels[side] = "息子" if b_is_male(ctx, [other]) else "娘"
            labels[1 - side] = "母"
            break
        parent_ids = [
            9999 if father == -1 else father,
            9998 if other_father == -1 else other_father,
            9997 if mother == -1 else mother,
            9996 if other_mother == -1 else other_mother,
        ]
        if parent_ids[0] in {parent_ids[1], parent_ids[3]} or parent_ids[2] in {parent_ids[1], parent_ids[3]}:
            labels[side] = "兄弟" if b_is_male(ctx, [other]) else "姉妹"
            labels[1 - side] = "兄弟" if b_is_male(ctx, [chara]) else "姉妹"
            break
        return 0
    _ctx_set_var(ctx, "RESULTS", [], labels[0])
    _ctx_set_var(ctx, "RESULTS", [1], labels[1])
    return 1


def _choice_rows(text: str, delimiter: str) -> list[str]:
    if delimiter == "":
        return [text]
    return [part for part in text.split(delimiter) if part != ""]


def _choice_value(choice: str) -> str | None:
    m = re.search(r"\[([^\]]+)\]", choice)
    return m.group(1).strip() if m else None


def _write_choice_line(ctx, text: str) -> None:
    if hasattr(ctx, "_write"):
        ctx._write(text, newline=True)


def _read_string_input(ctx, default: str = "") -> str:
    if hasattr(ctx, "_input"):
        return to_str(ctx._input(default))
    return default


def _expression_input_should_block(ctx) -> bool:
    return (
        not bool(getattr(ctx, "interactive", False))
        and bool(getattr(ctx, "had_explicit_inputs", False))
        and not bool(getattr(ctx, "inputs", None))
        and hasattr(ctx, "waiting_for_input")
    )


def _block_expression_input(ctx) -> None:
    try:
        ctx.waiting_for_input = True
    except Exception:
        pass
    raise EraInputBlocked()


def _expression_input_signature(name: str, args: list[Value]) -> tuple[str, tuple[str, ...]]:
    return (name, tuple(to_str(a) for a in args))


def _expression_input_resuming(ctx, signature: tuple[str, tuple[str, ...]]) -> bool:
    return getattr(ctx, "_paused_expression_input", None) == signature


def _pause_expression_input(ctx, signature: tuple[str, tuple[str, ...]]) -> None:
    try:
        setattr(ctx, "_paused_expression_input", signature)
    except Exception:
        pass
    _block_expression_input(ctx)


def _clear_expression_input_pause(ctx, signature: tuple[str, tuple[str, ...]]) -> None:
    try:
        if getattr(ctx, "_paused_expression_input", None) == signature:
            delattr(ctx, "_paused_expression_input")
    except Exception:
        pass


def _register_lightweight_window_log(ctx, body: str, delimiter: str, rows: int, width: int) -> None:
    try:
        b_message_window_log(ctx, ["", body, delimiter or "/", rows, width, 0])
    except Exception:
        pass


def _toggle_window_control_flag(ctx, flag_name: str) -> None:
    current = to_int(_ctx_get_var(ctx, "FLAG", [flag_name]))
    _ctx_set_var(ctx, "FLAG", [flag_name], current ^ 1)


def _resume_expression_window_control(ctx, signature: tuple[str, tuple[str, ...]]) -> None:
    resume = getattr(ctx, "_resume_window_control_button", None)
    if not callable(resume):
        return
    try:
        if resume() and _expression_input_should_block(ctx):
            _pause_expression_input(ctx, signature)
    except EraInputBlocked:
        raise
    except Exception:
        return


def _handle_window_control_input(ctx, control: str, *, enabled: bool, allow_skip: bool = True) -> bool:
    if not enabled or control not in {"+", "-", "*", "/"}:
        return False
    if control == "+":
        try:
            native_log = getattr(ctx, "_exec_native_message_window_log", None)
            if callable(native_log):
                native_log(["", "", "", 0, 0, 1])
            else:
                b_message_window_log(ctx, ["", "", "", 0, 0, 1])
        except Exception:
            pass
    elif control == "-":
        _toggle_window_control_flag(ctx, "オート送り")
    elif control == "*":
        if not allow_skip:
            return False
        _toggle_window_control_flag(ctx, "ウィンドウメッセージスキップ")
    elif control == "/":
        try:
            native_config = getattr(ctx, "_exec_native_message_window_config", None)
            if callable(native_config):
                native_config()
            else:
                b_message_window_config(ctx, [])
        except Exception:
            pass
    return True


def b_input_select_m(ctx, args):
    signature = _expression_input_signature(to_str(getattr(ctx, "_builtin_call_name", "INPUT_SELECT_M")), args)
    resuming = _expression_input_resuming(ctx, signature)
    text = to_str(arg(args, 0, ""))
    delimiter = to_str(arg(args, 1, "/"))
    window_options = to_str(arg(args, 2, "ログを残さない/ボタンを利用する"))
    controls_enabled = "ボタンを利用する" in window_options
    columns = max(1, to_int(arg(args, 3, 1)))
    rows_min = max(1, to_int(arg(args, 4, 1)))
    choices = _choice_rows(text, delimiter)
    values: list[str] = []
    one_key = True
    for choice in choices:
        value = _choice_value(choice)
        if value is not None:
            values.append(value)
            if len(value) > 1:
                one_key = False
    # Lightweight terminal rendering: keep visible choices, harvest their
    # numeric buttons, and register the same log body used by the [+] control.
    width = max(1, to_int(arg(args, 6, 72)))
    row_count = max(rows_min, (len(choices) + columns - 1) // columns)
    if not resuming:
        rendered_rows: list[str] = []
        for row in range(row_count):
            cells = []
            for col in range(columns):
                idx = row * columns + col
                cells.append(choices[idx] if idx < len(choices) else "")
            rendered = "　".join(cells).rstrip()[: max(width * columns, 1)]
            rendered_rows.append(rendered)
            _write_choice_line(ctx, rendered)
        _register_lightweight_window_log(ctx, delimiter.join(rendered_rows), delimiter, row_count, max(width * columns, 1))
        for value in values:
            if hasattr(ctx, "pending_buttons"):
                ctx.pending_buttons.append(value)
    if not values:
        _clear_expression_input_pause(ctx, signature)
        return 0
    while True:
        _resume_expression_window_control(ctx, signature)
        if _expression_input_should_block(ctx):
            _pause_expression_input(ctx, signature)
        entered = _read_string_input(ctx, values[0]).strip()
        # INPUT_SELECT_M.ERB displays [*] SKIP in the footer, but its control
        # gate only GROUPMATCHes "+", "-", "/" before the "*" branch.  Preserve
        # that eraMegaten quirk: "*" is consumed like an invalid visible button,
        # not a skip-toggle, while YN_M still handles it below.
        if _handle_window_control_input(ctx, entered, enabled=controls_enabled, allow_skip=False):
            if not getattr(ctx, "inputs", None):
                if _expression_input_should_block(ctx):
                    _pause_expression_input(ctx, signature)
                break
            continue
        if one_key and entered:
            entered = entered[:1]
        if entered in values:
            _clear_expression_input_pause(ctx, signature)
            return to_int(entered)
        if not getattr(ctx, "inputs", None):
            if _expression_input_should_block(ctx):
                _pause_expression_input(ctx, signature)
            break
    _clear_expression_input_pause(ctx, signature)
    return to_int(values[0])


def b_input_select_d(ctx, args):
    return b_input_select_m(
        ctx,
        [
            arg(args, 0, ""),
            arg(args, 1, "/"),
            arg(args, 2, "ログを残さない/ボタンを利用する"),
            arg(args, 3, 1),
            arg(args, 4, 4),
            arg(args, 5, "CENTER"),
            arg(args, 6, 72),
        ],
    )


def _yesno_mode(ctx) -> int:
    try:
        return _flag(ctx, "双选入力设定") or _flag(ctx, "双选输入设定")
    except Exception:
        return 0


def b_input_yn_m(ctx, args):
    signature = _expression_input_signature(to_str(getattr(ctx, "_builtin_call_name", "INPUT_YN_M")), args)
    resuming = _expression_input_resuming(ctx, signature)
    yes = to_str(arg(args, 0, "はい"))
    no = to_str(arg(args, 1, "いいえ"))
    delimiter = to_str(arg(args, 2, "/"))
    window_options = to_str(arg(args, 3, "ログを残さない/ボタンを利用する"))
    controls_enabled = "ボタンを利用する" in window_options
    rows = max(1, to_int(arg(args, 5, 2)))
    width = max(1, to_int(arg(args, 6, 72)))
    if not resuming:
        _write_choice_line(ctx, f"[0] {yes}{delimiter}[1] {no}")
        _register_lightweight_window_log(ctx, f"[0] {yes}{delimiter}[1] {no}", delimiter, rows, width)
    if hasattr(ctx, "pending_buttons") and not resuming:
        ctx.pending_buttons.extend(["0", "1"])
    mode = _yesno_mode(ctx)
    while True:
        _resume_expression_window_control(ctx, signature)
        if _expression_input_should_block(ctx):
            _pause_expression_input(ctx, signature)
        raw = _read_string_input(ctx, "0")
        if _handle_window_control_input(ctx, raw.strip(), enabled=controls_enabled):
            if not getattr(ctx, "inputs", None):
                if _expression_input_should_block(ctx):
                    _pause_expression_input(ctx, signature)
                break
            continue
        if raw == " ":
            _clear_expression_input_pause(ctx, signature)
            return 0
        entered = raw.strip()
        if entered:
            entered = entered[:1]
        if entered in {"0", " "}:
            _clear_expression_input_pause(ctx, signature)
            return 0
        if entered == "1":
            _clear_expression_input_pause(ctx, signature)
            return 1
        if entered in {"y", "Y"} and (mode == 2 or (mode == 1 and yes in {"はい", "Yes"})):
            _clear_expression_input_pause(ctx, signature)
            return 0
        if entered in {"n", "N"} and (mode == 2 or (mode == 1 and no in {"いいえ", "No"})):
            _clear_expression_input_pause(ctx, signature)
            return 1
        if not getattr(ctx, "inputs", None):
            if _expression_input_should_block(ctx):
                _pause_expression_input(ctx, signature)
            break
    _clear_expression_input_pause(ctx, signature)
    return 0


def b_input_yn_d(ctx, args):
    return b_input_yn_m(
        ctx,
        [
            arg(args, 0, "はい"),
            arg(args, 1, "いいえ"),
            arg(args, 2, "/"),
            arg(args, 3, "ログを残さない/ボタンを利用する"),
            arg(args, 4, "CENTER"),
            arg(args, 5, 4),
            arg(args, 6, 72),
        ],
    )


def _input_set_numeric(ctx, value: int) -> int:
    _ctx_set_var(ctx, "RESULT", [], value)
    _ctx_set_var(ctx, "RESULTS", [], str(value))
    return value


def _input_set_string(ctx, value: str) -> str:
    _ctx_set_var(ctx, "RESULTS", [], value)
    _ctx_set_var(ctx, "RESULT", [], to_int(value))
    return value


def _input_allowed_values(args: list[Value], *, default_zero: bool = False) -> list[int]:
    values = [to_int(a) for a in args]
    if not values and default_zero:
        values = [0]
    return values


def _input_read_numeric(ctx, default: int = 0, *, one_key: bool = False) -> int:
    text = _read_string_input(ctx, str(default)).strip()
    if one_key and text:
        text = text[:1]
    return to_int(text)


def _to_ascii_digit_text(text: str) -> str:
    trans = str.maketrans("０１２３４５６７８９", "0123456789")
    return text.translate(trans)


def b_inputint(ctx, args):
    allowed = _input_allowed_values(args, default_zero=True)
    one_key = all(0 <= value <= 9 for value in allowed)
    while True:
        if _expression_input_should_block(ctx):
            _block_expression_input(ctx)
        value = _input_read_numeric(ctx, allowed[0] if allowed else 0, one_key=one_key)
        if value in allowed:
            return _input_set_numeric(ctx, value)
        if not getattr(ctx, "inputs", None):
            if _expression_input_should_block(ctx):
                _block_expression_input(ctx)
            break
    return _input_set_numeric(ctx, allowed[0] if allowed else 0)


def b_tinputint(ctx, args):
    default = to_int(arg(args, 1, 0))
    allowed = [default] + [to_int(a) for a in args[3:]]
    while True:
        if _expression_input_should_block(ctx):
            _block_expression_input(ctx)
        value = _input_read_numeric(ctx, default)
        if value in allowed:
            return _input_set_numeric(ctx, value)
        if not getattr(ctx, "inputs", None):
            if _expression_input_should_block(ctx):
                _block_expression_input(ctx)
            break
    return _input_set_numeric(ctx, default)


def b_input_char(ctx, args):
    allowed = to_str(arg(args, 0, ""))
    allow_enter = to_int(arg(args, 1, 0)) != 0
    while True:
        if _expression_input_should_block(ctx):
            _block_expression_input(ctx)
        text = _read_string_input(ctx, "")
        char = text[:1] if text else ""
        if char == "" and allow_enter:
            _input_set_string(ctx, "")
            return 0
        if char and char in allowed:
            _input_set_string(ctx, char)
            return 0
        if not getattr(ctx, "inputs", None):
            if _expression_input_should_block(ctx):
                _block_expression_input(ctx)
            break
    fallback = "" if allow_enter else (allowed[:1] if allowed else "")
    _input_set_string(ctx, fallback)
    return 0


def b_input_many(ctx, args):
    signature = _expression_input_signature("INPUT_MANY", args)
    lo = to_int(arg(args, 0, 0))
    hi = to_int(arg(args, 1, lo))
    if lo > hi:
        lo, hi = hi, lo
    options = to_str(arg(args, 2, "ログを残す"))
    exceptions = {to_int(part) for part in to_str(arg(args, 3, "")).split("/") if part != ""}
    paused_state = getattr(ctx, "_paused_expression_input_many", None)
    if isinstance(paused_state, dict) and paused_state.get("signature") == signature:
        current = to_int(paused_state.get("current", 0))
        sign = -1 if to_int(paused_state.get("sign", 1)) < 0 else 1
        rendered = bool(paused_state.get("rendered", False))
    else:
        current = 0
        sign = 1
        rendered = False
    digit_map = {ch: i for i, ch in enumerate("０１２３４５６７８９")}

    def render_prompt(*, replace: bool = False) -> None:
        nonlocal rendered
        if rendered and replace and hasattr(ctx, "_clear_lines"):
            ctx._clear_lines(1)
            rendered = False
        if not rendered and hasattr(ctx, "_write"):
            ctx._write(f"【{current}】　《【{lo}】 - 【{hi}】》", newline=True)
            rendered = True

    def pause() -> None:
        try:
            setattr(
                ctx,
                "_paused_expression_input_many",
                {"signature": signature, "current": current, "sign": sign, "rendered": rendered},
            )
        except Exception:
            pass
        _pause_expression_input(ctx, signature)

    def clear_pause() -> None:
        try:
            state = getattr(ctx, "_paused_expression_input_many", None)
            if isinstance(state, dict) and state.get("signature") == signature:
                delattr(ctx, "_paused_expression_input_many")
        except Exception:
            pass
        _clear_expression_input_pause(ctx, signature)

    def accept(value: int) -> Value | None:
        if lo <= value <= hi or value in exceptions:
            if "ログを残す" not in options and hasattr(ctx, "_clear_lines"):
                ctx._clear_lines(1)
            clear_pause()
            return _input_set_numeric(ctx, value)
        return None

    render_prompt()
    while True:
        if _expression_input_should_block(ctx):
            pause()
        raw = _read_string_input(ctx, str(lo)).strip()
        upper = raw.upper()
        if upper == "AC":
            current = 0
            sign = 1
            render_prompt(replace=True)
            if not getattr(ctx, "inputs", None):
                if _expression_input_should_block(ctx):
                    pause()
                break
            continue
        elif upper == "MIN":
            current = lo
            render_prompt(replace=True)
            if not getattr(ctx, "inputs", None):
                if _expression_input_should_block(ctx):
                    pause()
                break
            continue
        elif upper == "MAX":
            current = hi
            render_prompt(replace=True)
            if not getattr(ctx, "inputs", None):
                if _expression_input_should_block(ctx):
                    pause()
                break
            continue
        elif raw == "+":
            current *= -1
            sign = 1
            render_prompt(replace=True)
            if not getattr(ctx, "inputs", None):
                if _expression_input_should_block(ctx):
                    pause()
                break
            continue
        elif raw == "-":
            current *= -1
            sign = -1
            render_prompt(replace=True)
            if not getattr(ctx, "inputs", None):
                if _expression_input_should_block(ctx):
                    pause()
                break
            continue
        elif raw in digit_map:
            digit = digit_map[raw]
            if current == 0:
                current = abs(current) + digit * sign
            else:
                current = current * 10 + digit
            render_prompt(replace=True)
            if not getattr(ctx, "inputs", None):
                if _expression_input_should_block(ctx):
                    pause()
                break
            continue
        elif upper in {"ENTER", ""}:
            value = current
        else:
            normalized = _to_ascii_digit_text(raw)
            if re.fullmatch(r"\d+", normalized or ""):
                value = to_int(normalized)
            else:
                if not getattr(ctx, "inputs", None):
                    if _expression_input_should_block(ctx):
                        pause()
                    break
                continue
        result = accept(value)
        if result is not None:
            return result
        if not getattr(ctx, "inputs", None):
            if _expression_input_should_block(ctx):
                pause()
            break
    clear_pause()
    return _input_set_numeric(ctx, lo)


def b_input_select(ctx, args):
    signature = _expression_input_signature("INPUT_SELECT", args)
    resuming = _expression_input_resuming(ctx, signature)
    pairs: list[tuple[int, str]] = []
    for i in range(0, min(len(args), 40), 2):
        value = to_int(arg(args, i, 0))
        label = to_str(arg(args, i + 1, ""))
        if value != 0:
            pairs.append((value, label))
    if not pairs:
        _clear_expression_input_pause(ctx, signature)
        return _input_set_numeric(ctx, 0)
    value_width = max(len(str(value)) for value, _ in pairs)
    label_width = max((len(label) for _, label in pairs), default=0)
    if not resuming:
        line: list[str] = []
        for value, label in pairs:
            cell = f"[{value:>{value_width}}] {label.ljust(label_width)}　　"
            line.append(cell)
            if len(line) == 2:
                _write_choice_line(ctx, "".join(line).rstrip())
                line.clear()
        if line:
            _write_choice_line(ctx, "".join(line).rstrip())
    allowed = {value for value, _ in pairs}
    one_key = value_width == 1
    while True:
        if _expression_input_should_block(ctx):
            _pause_expression_input(ctx, signature)
        value = _input_read_numeric(ctx, pairs[0][0], one_key=one_key)
        if value != 0 and value in allowed:
            _clear_expression_input_pause(ctx, signature)
            return _input_set_numeric(ctx, value)
        if not getattr(ctx, "inputs", None):
            if _expression_input_should_block(ctx):
                _pause_expression_input(ctx, signature)
            break
    _clear_expression_input_pause(ctx, signature)
    return _input_set_numeric(ctx, pairs[0][0])


def _input_split_render(ctx, title: str, choices: list[tuple[int, str]], *, columns: int) -> None:
    if hasattr(ctx, "_write"):
        ctx._write(title, newline=True)
        ctx._write("─" * 72, newline=True)
    width = max((len(text) + 1 for _, text in choices), default=1)
    num_width = max((len(str(num)) for num, _ in choices), default=1)
    row: list[str] = []
    for num, text in choices:
        row.append(f"[{num:>{num_width}}]{text.ljust(width)}")
        if len(row) >= columns:
            _write_choice_line(ctx, "".join(row).rstrip())
            row.clear()
    if row:
        _write_choice_line(ctx, "".join(row).rstrip())
    if hasattr(ctx, "_write"):
        ctx._write("─" * 72, newline=True)


def b_input_split(ctx, args):
    signature = _expression_input_signature("INPUT_SPLIT", args)
    title = to_str(arg(args, 0, ""))
    items_text = to_str(arg(args, 1, ""))
    delim = to_str(arg(args, 2, "/"))
    cancel_text = to_str(arg(args, 3, "　"))
    columns = max(1, to_int(arg(args, 4, 1)))
    page = max(0, to_int(arg(args, 5, 0)))
    start_no = to_int(arg(args, 6, 1))
    prev_no = to_int(arg(args, 7, 1001))
    cancel_no = to_int(arg(args, 8, 0))
    next_no = to_int(arg(args, 9, 1003))
    items = [part for part in items_text.split(delim) if part != ""]
    page_size = max(1, 20 * columns)
    page_count = max(1, (len(items) + page_size - 1) // page_size)
    page = min(page, page_count - 1)
    rendered_page = -1
    paused_state = getattr(ctx, "_paused_expression_input_split", None)
    if isinstance(paused_state, dict) and paused_state.get("signature") == signature:
        page = max(0, min(page_count - 1, to_int(paused_state.get("page", page))))
        rendered_page = to_int(paused_state.get("rendered_page", -1))
    choices: list[tuple[int, str]] = []

    def pause() -> None:
        try:
            setattr(
                ctx,
                "_paused_expression_input_split",
                {"signature": signature, "page": page, "rendered_page": rendered_page},
            )
        except Exception:
            pass
        _pause_expression_input(ctx, signature)

    def clear_pause() -> None:
        try:
            state = getattr(ctx, "_paused_expression_input_split", None)
            if isinstance(state, dict) and state.get("signature") == signature:
                delattr(ctx, "_paused_expression_input_split")
        except Exception:
            pass
        _clear_expression_input_pause(ctx, signature)

    while True:
        begin = page * page_size
        visible_items = items[begin:begin + page_size]
        choices = [(start_no + begin + i, text) for i, text in enumerate(visible_items)]
        if rendered_page != page:
            _input_split_render(ctx, title, choices, columns=columns)
            footer = []
            if page > 0:
                footer.append((prev_no, "前一頁"))
            if cancel_text not in {"", "　"}:
                footer.append((cancel_no, cancel_text))
            if page + 1 < page_count:
                footer.append((next_no, "下一頁"))
            if footer:
                _write_choice_line(ctx, " ".join(f"[{num}]{label}" for num, label in footer))
            rendered_page = page
        if _expression_input_should_block(ctx):
            pause()
        value = _input_read_numeric(ctx, choices[0][0] if choices else cancel_no)
        if value == prev_no and page > 0:
            page -= 1
            rendered_page = -1
            continue
        if value == next_no and page + 1 < page_count:
            page += 1
            rendered_page = -1
            continue
        if value == cancel_no and cancel_text not in {"", "　"}:
            _ctx_set_var(ctx, "RESULTS", [], cancel_text)
            _ctx_set_var(ctx, "RESULT", [], value)
            _ctx_set_var(ctx, "RESULT", [1], page)
            clear_pause()
            return value
        idx = value - start_no
        if 0 <= idx < len(items):
            _ctx_set_var(ctx, "RESULTS", [], items[idx])
            _ctx_set_var(ctx, "RESULT", [], value)
            _ctx_set_var(ctx, "RESULT", [1], page)
            clear_pause()
            return value
        if not getattr(ctx, "inputs", None):
            if _expression_input_should_block(ctx):
                pause()
            break
    fallback_text = items[0] if items else cancel_text
    fallback_value = start_no if items else cancel_no
    _ctx_set_var(ctx, "RESULTS", [], fallback_text)
    _ctx_set_var(ctx, "RESULT", [], fallback_value)
    _ctx_set_var(ctx, "RESULT", [1], page)
    clear_pause()
    return fallback_value


def _tap_entries(args: list[Value], delimiter: str, start: int) -> list[tuple[str, str, str, int]]:
    entries: list[tuple[str, str, str, int]] = []
    for value in args[start:start + 20]:
        parts = to_str(value).split(delimiter)
        key = parts[0] if len(parts) > 0 else ""
        button = parts[1] if len(parts) > 1 else ""
        tag = parts[2] if len(parts) > 2 else ""
        color = to_int(parts[3]) if len(parts) > 3 and re.fullmatch(r"[+-]?\d+", parts[3] or "") else 0
        entries.append((key, button, tag, color))
    return entries


def b_input_onekey_tap(ctx, args):
    wasd = to_int(arg(args, 0, 0)) != 0
    line_char = to_str(arg(args, 1, "-")) or "-"
    delimiter = to_str(arg(args, 2, "_")) or "_"
    entries = _tap_entries(args, delimiter, 3)
    if hasattr(ctx, "_write"):
        ctx._write((line_char * 72)[:72], newline=True)
        labels = [f"[{button}]{tag}" for key, button, tag, _ in entries if button or tag]
        if labels:
            ctx._write(" ".join(labels), newline=True)
        ctx._write((line_char * 72)[:72], newline=True)
    allowed = ("wasdWASD" if wasd else "8462") + "".join(key for key, _, _, _ in entries)
    allow_enter = any(key == "" and button != "" for key, button, _, _ in entries)
    for key, button, tag, _ in entries:
        if hasattr(ctx, "pending_buttons") and button:
            ctx.pending_buttons.append(key if key != "" else button)
    b_input_char(ctx, [allowed, 1 if allow_enter else 0])
    return 0


def b_input_onekey_tap_results(ctx, args):
    result_args = [arg(args, 0, 0), arg(args, 1, "-"), arg(args, 2, "_")]
    result_args.extend(_ctx_get_var(ctx, "RESULTS", [i]) for i in range(20))
    return b_input_onekey_tap(ctx, result_args)


def b_input_yn(ctx, args):
    signature = _expression_input_signature("INPUT_YN", args)
    resuming = _expression_input_resuming(ctx, signature)
    yes = to_str(arg(args, 0, "はい"))
    no = to_str(arg(args, 1, "いいえ"))
    style = to_int(arg(args, 2, 1))
    if not resuming:
        if style == 1:
            _write_choice_line(ctx, f"[0] {yes}")
            _write_choice_line(ctx, f"[1] {no}")
        elif style == 2:
            _write_choice_line(ctx, f"[0] {yes} [1] {no}")
    if hasattr(ctx, "pending_buttons"):
        ctx.pending_buttons.extend(["0", "1"])
    mode = _yesno_mode(ctx)
    while True:
        if _expression_input_should_block(ctx):
            _pause_expression_input(ctx, signature)
        entered = _read_string_input(ctx, "0")
        s = entered[:1] if entered else ""
        if s in {"0", " "}:
            _clear_expression_input_pause(ctx, signature)
            return _input_set_numeric(ctx, 0)
        if s == "1":
            _clear_expression_input_pause(ctx, signature)
            return _input_set_numeric(ctx, 1)
        if s in {"y", "Y"} and (mode == 2 or (mode == 1 and yes in {"はい", "Yes"})):
            _clear_expression_input_pause(ctx, signature)
            return _input_set_numeric(ctx, 0)
        if s in {"n", "N"} and (mode == 2 or (mode == 1 and no in {"いいえ", "No"})):
            _clear_expression_input_pause(ctx, signature)
            return _input_set_numeric(ctx, 1)
        if not getattr(ctx, "inputs", None):
            if _expression_input_should_block(ctx):
                _pause_expression_input(ctx, signature)
            break
    _clear_expression_input_pause(ctx, signature)
    return _input_set_numeric(ctx, 0)


def b_set_comflag(ctx, args):
    flag_no = to_int(arg(args, 0, 0))
    clear = truth(arg(args, 1, 0))
    chara = _target_if_zero(ctx, arg(args, 2, 0))
    if flag_no >= 1260 or flag_no < 0 or chara < 0:
        return 0
    slot, bit = _kojo_flag_slot(ctx, chara, flag_no, offset=0)
    _set_cflag_bit(ctx, chara, slot, bit, not clear)
    return 0


def _array_materialized_count(ctx, base: str, start: int) -> int:
    mem = _ctx_memory(ctx)
    key = norm_name(base)
    tables = []
    frame = getattr(mem, "frame", None)
    if frame is not None:
        if key in getattr(frame, "numeric", {}):
            tables.append(frame.numeric[key])
        if key in getattr(frame, "strings", {}):
            tables.append(frame.strings[key])
    if key in getattr(mem, "numeric", {}):
        tables.append(mem.numeric[key])
    if key in getattr(mem, "strings", {}):
        tables.append(mem.strings[key])
    max_i = start
    for table in tables:
        for idx in table.keys():
            if len(idx) == 1:
                max_i = max(max_i, to_int(idx[0]))
    return max(0, max_i - start + 1)


def b_set_nexttrain(ctx, args):
    value = to_int(arg(args, 0, 0)) + to_int(arg(args, 1, 0)) * 10000
    start = 1
    count = _array_materialized_count(ctx, "SELECTCOM", start)
    for i in range(start + count - 1, start - 1, -1):
        _ctx_set_var(ctx, "SELECTCOM", [i + 1], _ctx_get_var(ctx, "SELECTCOM", [i]))
    _ctx_set_var(ctx, "SELECTCOM", [start], value)
    return 0


def b_oncerand(ctx, args):
    size = max(0, to_int(arg(args, 0, 0)))
    group = to_int(arg(args, 1, 0)) + 1
    reset_mode = to_int(arg(args, 2, 0))
    state = getattr(ctx, "_oncerand_state", None)
    if state is None:
        state = {}
        setattr(ctx, "_oncerand_state", state)
    if reset_mode > 0:
        state.clear()
    if reset_mode == 2:
        return 0
    mask = int(state.get(group, 0))
    used = sum(1 for bit in range(size) if mask & (1 << bit))
    remaining = size - used
    if remaining <= 0:
        return 0
    pick = random.randrange(remaining)
    for bit in range(size):
        if mask & (1 << bit):
            continue
        if pick == 0:
            state[group] = mask | (1 << bit)
            return bit + 1
        pick -= 1
    return -1


def b_chara_bond_check(ctx, args):
    if not _has_csv_name(ctx, "BASE", "忠誠度"):
        return None
    a = b_getchara(ctx, [to_int(arg(args, 0, 0))])
    b = b_getchara(ctx, [to_int(arg(args, 1, 0))])
    fav_ba = b_favorite(ctx, [b, a])
    fav_ab = b_favorite(ctx, [a, b])
    if fav_ba is None or fav_ab is None:
        return None
    loyalty_idx = _script_index(ctx, "BASE", "忠誠度")
    total = (abs(to_int(fav_ba)) + abs(to_int(fav_ab))) * 10
    total += to_int(_ctx_get_var(ctx, "BASE", [a, loyalty_idx]))
    total += to_int(_ctx_get_var(ctx, "BASE", [b, loyalty_idx]))
    return max(total, 0)


def b_chara_exists_check(ctx, args):
    required = [
        ("TALENT", "非戦闘員"),
        ("CFLAG", "戦闘参加不可能"),
        ("CFLAG", "この場に居ないフラグ"),
    ]
    if not all(_has_csv_name(ctx, var, name) for var, name in required):
        return None
    chara = b_getchara(ctx, [to_int(arg(args, 0, 0))])
    if chara == -1:
        return 0
    if _talent(ctx, chara, "非戦闘員"):
        fallen = b_fallen(ctx, [chara])
        if fallen is None:
            return None
        if not truth(fallen):
            return 0
    elif _cflag(ctx, chara, "戦闘参加不可能") > 0:
        return 0
    if _cflag(ctx, chara, "この場に居ないフラグ") > 0:
        return 0
    return 1


def b_event_10_two_chara_check(ctx, args):
    if not truth(b_chara_exists_check(ctx, [arg(args, 0, 0)])):
        return 0
    if not truth(b_chara_exists_check(ctx, [arg(args, 1, 0)])):
        return 0
    bond = b_chara_bond_check(ctx, [arg(args, 0, 0), arg(args, 1, 0)])
    if bond is None:
        return None
    if to_int(bond) < to_int(arg(args, 2, 0)):
        return 0
    return 1


def _once_event_store(ctx, args, *, start: int, end: int, reset_end: int, max_name_len: int, limit_slot: int) -> int:
    name = to_str(arg(args, 0, ""))
    readonly = truth(arg(args, 1, 0))
    reset = truth(arg(args, 2, 0))
    if reset:
        for slot in range(start, reset_end):
            _ctx_set_var(ctx, "SAVESTR", [slot], "/")
        return 0
    if b_strlens(ctx, [name]) > max_name_len:
        _strflag_throw(ctx, "ARGSが長すぎます")
        return 0
    token = f"/{name}/"
    for slot in range(start, end):
        text = to_str(_ctx_get_var(ctx, "SAVESTR", [slot]))
        if text == "":
            text = "/"
            _ctx_set_var(ctx, "SAVESTR", [slot], text)
        if to_int(b_strcount(ctx, [text, token])) == 0:
            if not readonly:
                text = f"{text}{name}/"
                _ctx_set_var(ctx, "SAVESTR", [slot], text)
            if b_strlens(ctx, [_ctx_get_var(ctx, "SAVESTR", [limit_slot])]) > 2000:
                _strflag_throw(ctx, "記録可能な事件数を超えてしまいました")
            return 1
        return 0
    return 0


def b_onceday(ctx, args):
    return _once_event_store(ctx, args, start=0, end=10, reset_end=10, max_name_len=19, limit_slot=9)


def b_onceturn(ctx, args):
    return _once_event_store(ctx, args, start=10, end=20, reset_end=19, max_name_len=18, limit_slot=19)


def b_eventturnend(ctx, args):
    b_onceturn(ctx, ["0", 0, 1])
    if to_int(_ctx_get_var(ctx, "TIME", [])) == 0:
        b_onceday(ctx, ["0", 0, 1])
    return 0


def b_onceplay(ctx, args):
    flag_no = to_int(arg(args, 0, 0))
    target_kind = to_int(arg(args, 1, 0))
    chara = to_int(arg(args, 2, 0))
    readonly = truth(arg(args, 3, 0))
    reset_mode = to_int(arg(args, 4, 0))
    if target_kind == 0:
        chara = to_int(_ctx_get_var(ctx, "TARGET", []))
    elif target_kind == 1:
        chara = to_int(_ctx_get_var(ctx, "MASTER", []))
    elif target_kind == 2:
        chara = to_int(_ctx_get_var(ctx, "ASSI", []))
    if flag_no >= 64 or flag_no < 0 or chara >= to_int(_ctx_get_var(ctx, "CHARANUM", [])):
        return 0
    state = getattr(ctx, "_onceplay_state", None)
    if state is None:
        state = {}
        setattr(ctx, "_onceplay_state", state)
    if reset_mode == 2:
        state.clear()
        return 0
    if reset_mode == 1:
        state[chara] = 0
        return 0
    value = to_int(state.get(chara, 0))
    if not (value & (1 << flag_no)):
        if not readonly:
            state[chara] = value | (1 << flag_no)
        return 1
    return 0


def b_weekday(ctx, args):
    return ["日", "月", "火", "水", "木", "金", "土"][to_int(arg(args, 0, -1))] if 0 <= to_int(arg(args, 0, -1)) <= 6 else "？"


def b_exist_item(ctx, args):
    item_no = to_int(arg(args, 0, 0))
    if to_int(_ctx_get_var(ctx, "ITEM", [item_no])) == 0 and to_int(_ctx_get_var(ctx, "NOITEM", [])) == 0:
        return 0
    return 1


def b_csv_spouse(ctx, args):
    chara = to_int(arg(args, 0, 0))
    spouse_chara = to_int(arg(args, 1, 0))
    if spouse_chara == -1:
        return 0
    no = to_int(_ctx_get_var(ctx, "NO", [spouse_chara]))
    name = to_str(b_csvname(ctx, [no, 0]))
    text = to_str(_ctx_get_var(ctx, "CSTR", [chara, "配偶者"]))
    return 1 if to_str(b_autosplit(ctx, [text, "_", 0, name])) != "" else 0


_EVENT_KEYWORD_FALLBACK = {
    "調教初回事件": 200,
    "調教開始事件": 201,
    "調教終了事件": 202,
    "調教中事件": 245,
    "強絶頂初回": 0,
    "射精": 1,
    "噴乳": 2,
    "放尿": 3,
    "PALAM変化": 246,
    "潤滑Lv5": 0,
    "欲情Lv3": 2,
    "屈服Lv4": 4,
    "朝事件": 250,
    "調教後事件": 251,
    "夜事件": 252,
    "妊娠事件": 253,
    "労働": 256,
    "探索中性処理": 257,
    "探索中セックス": 4,
    "Ｖ挿入経験": 260,
    "Ａ挿入経験": 261,
    "ペニバン経験": 262,
    "陰茎経験": 263,
    "被射精経験": 264,
    "射精経験": 265,
}


def _event_keyword_map(ctx) -> dict[str, int]:
    program = getattr(ctx, "program", None)
    cached = getattr(program, "_event_keyword_map", None) if program is not None else None
    if cached is not None:
        return cached
    mapping = dict(_EVENT_KEYWORD_FALLBACK)
    root = getattr(program, "root", None)
    candidates = []
    if root is not None:
        try:
            candidates = list(root.rglob("EVENT_BIT.ERB"))
        except Exception:
            candidates = []
    for path in candidates[:1]:
        try:
            text = read_text_auto(path)
        except Exception:
            continue
        current: str | None = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith(";"):
                continue
            m_case = re.match(r'CASE\s+(?:"([^"]+)"|(.+))$', line, flags=re.IGNORECASE)
            if m_case:
                current = (m_case.group(1) if m_case.group(1) is not None else m_case.group(2).strip())
                continue
            if current is None:
                continue
            m_ret = re.match(r"RETURNF\s+(.+)$", line, flags=re.IGNORECASE)
            if m_ret:
                try:
                    value = parse_era_int(m_ret.group(1).strip())
                except Exception:
                    current = None
                    continue
                mapping.setdefault(current, value)
                current = None
    if program is not None:
        try:
            setattr(program, "_event_keyword_map", mapping)
        except Exception:
            pass
    return mapping


def b_event_keyword(ctx, args):
    key = to_str(arg(args, 0, ""))
    mapping = _event_keyword_map(ctx)
    if key in mapping:
        return mapping[key]
    return -1


def _event_bit_args(ctx, args) -> tuple[int, str, str]:
    def omitted(v: Value) -> bool:
        return v == "" or v is None
    if len(args) == 0:
        return to_int(_ctx_get_var(ctx, "TARGET", [])), "", "ALL"
    if len(args) == 1:
        return to_int(_ctx_get_var(ctx, "TARGET", [])), to_str(args[0]), "ALL"
    first = arg(args, 0, -1)
    if len(args) == 2 and (isinstance(first, str) and first != "" and not _looks_numeric(first)):
        return to_int(_ctx_get_var(ctx, "TARGET", [])), to_str(args[0]), to_str(args[1])
    chara = to_int(_ctx_get_var(ctx, "TARGET", [])) if omitted(first) or to_int(first) == -1 else to_int(first)
    category = to_str(arg(args, 1, ""))
    content = to_str(arg(args, 2, "ALL"))
    return chara, category, content


def _event_bit_slot(ctx, category: str, content: str = "ALL") -> tuple[int, int]:
    return to_int(b_event_keyword(ctx, [category])), to_int(b_event_keyword(ctx, [content]))


def b_event_setbit(ctx, args):
    chara, category, content = _event_bit_args(ctx, args)
    slot, bit = _event_bit_slot(ctx, category, content)
    value = to_int(_ctx_get_var(ctx, "CFLAG", [chara, slot]))
    _ctx_set_var(ctx, "CFLAG", [chara, slot], value | (1 << bit))
    return 0


def b_event_clearbit(ctx, args):
    chara, category, content = _event_bit_args(ctx, args)
    slot, bit = _event_bit_slot(ctx, category, content)
    value = to_int(_ctx_get_var(ctx, "CFLAG", [chara, slot]))
    _ctx_set_var(ctx, "CFLAG", [chara, slot], value & ~(1 << bit))
    return 0


def b_event_invertbit(ctx, args):
    chara, category, content = _event_bit_args(ctx, args)
    slot, bit = _event_bit_slot(ctx, category, content)
    value = to_int(_ctx_get_var(ctx, "CFLAG", [chara, slot]))
    _ctx_set_var(ctx, "CFLAG", [chara, slot], value ^ (1 << bit))
    return 0


def b_event_getbit(ctx, args):
    chara, category, content = _event_bit_args(ctx, args)
    slot = to_int(b_event_keyword(ctx, [category]))
    value = to_int(_ctx_get_var(ctx, "CFLAG", [chara, slot]))
    if content == "ALL":
        return value
    bit = to_int(b_event_keyword(ctx, [content]))
    return 1 if value & (1 << bit) else 0


def _train_bit_bitsize(ctx) -> int:
    program = getattr(ctx, "program", None)
    value = getattr(program, "defines", {}).get(norm_name("TRAIN_BIT_BITSIZE"), "3") if program is not None else "3"
    return max(1, to_int(value))


def _train_set_args(ctx, args) -> tuple[int, int, int]:
    if len(args) == 0:
        _strflag_throw(ctx, "引数が指定されていません")
        return 0, 0, 0
    if len(args) == 1:
        return to_int(_ctx_get_var(ctx, "TARGET", [])), to_int(_ctx_get_var(ctx, "SELECTCOM", [])), to_int(args[0])
    if len(args) == 2:
        return to_int(_ctx_get_var(ctx, "TARGET", [])), to_int(args[0]), to_int(args[1])
    return to_int(args[0]), to_int(args[1]), to_int(args[2])


def _train_get_args(ctx, args) -> tuple[int, int]:
    if len(args) == 0:
        return to_int(_ctx_get_var(ctx, "TARGET", [])), to_int(_ctx_get_var(ctx, "SELECTCOM", []))
    if len(args) == 1:
        return to_int(_ctx_get_var(ctx, "TARGET", [])), to_int(args[0])
    return to_int(args[0]), to_int(args[1])


def b_train_setbit(ctx, args):
    chara, command, stored = _train_set_args(ctx, args)
    bitsize = _train_bit_bitsize(ctx)
    if not (0 <= stored < (1 << bitsize)):
        _strflag_throw(ctx, f"代入された値({stored})は範囲外です")
        return 0
    slot = int(command / 64) * bitsize + 300
    bit = command % 64
    for off in range(bitsize):
        value = to_int(_ctx_get_var(ctx, "CFLAG", [chara, slot + off]))
        if stored & (1 << off):
            value |= 1 << bit
        else:
            value &= ~(1 << bit)
        _ctx_set_var(ctx, "CFLAG", [chara, slot + off], value)
    return 0


def b_train_getbit(ctx, args):
    chara, command = _train_get_args(ctx, args)
    bitsize = _train_bit_bitsize(ctx)
    slot = int(command / 64) * bitsize + 300
    bit = command % 64
    value = 0
    for off in range(bitsize):
        if to_int(_ctx_get_var(ctx, "CFLAG", [chara, slot + off])) & (1 << bit):
            value += 1 << off
    return value


def _choice(values: list[str]) -> str:
    return values[random.randrange(max(1, len(values)))] if values else ""


def b_dirty_word_ochin(ctx, args):
    prefixes = ["剥きたて", "見栄張り", "皮かむり", "お子様", "包茎", "短小", "小さな", "背伸び", "童貞", "粉色の", ""]
    return _choice(prefixes) + "おちんちん"


def b_dirty_word_penis(ctx, args):
    prefixes = ["カリ太", "デカ", "ぶっとい", "包茎", "カタい", "臭い", "勃起", "ガチガチ", "極太", "胴長"]
    return _choice(prefixes) + "鴆ポ"


def b_dirty_word_semen(ctx, args):
    chara = to_int(arg(args, 0, -1))
    choices = ["精液", "子種汁", "ザー汁", "鴆ポミルク", "鴆ポエキス"]
    if chara == -1:
        choices.extend(["ザーメン", "鴆ポ汁", "オスミルク", "オス汁"])
    elif 0 <= chara <= to_int(_ctx_get_var(ctx, "CHARANUM", [])) - 1:
        if truth(b_is_beast(ctx, [chara])):
            choices.append("野生ザーメン")
            if _talent(ctx, chara, "獣"):
                choices.extend(["獣姦精液", "獣姦鴆ポ汁", "ケモミルク", "ケダモノ汁"])
            if _talent(ctx, chara, "爬虫類"):
                choices.extend(["爬虫類エキス", "爬虫類ミルク", "トカゲ汁"])
            if _talent(ctx, chara, "不定形"):
                choices.extend(["体液", "史莱姆汁"])
        else:
            choices.append("ザーメン")
            size = to_int(b_body_size(ctx, [chara]) or 0)
            if truth(b_is_lookslike_male(ctx, [chara])) and size in {0, 1}:
                choices.extend(["鴆ポ汁", "オスミルク", "オス汁"])
            elif truth(b_is_male(ctx, [chara])) and (_talent(ctx, chara, "偽娘") or size == -1):
                choices.extend(["ショタエキス", "ショタミルク", "ショタ汁"])
            elif not truth(b_is_male(ctx, [chara])):
                choices.extend(["牝鴆ポ汁", "牝鴆ポミルク", "FUTA汁", "FUTAエキス"])
            if size <= -10:
                choices.extend(["妖精ミルク", "妖精鴆ポ汁", "妖精ミルク", "妖精鴆ポ汁"])
            elif size >= 10:
                choices.extend(["デカ鴆ポ汁", "デカマラ汁", "デカマラミルク"])
            if _talent(ctx, chara, "精力超群"):
                choices.append("無限ザーメン")
            if _talent(ctx, chara, "Ｃ敏感"):
                choices.extend(["早漏ザーメン", "早漏精液"])
    text = _choice(choices)
    roll = random.randrange(100)
    if chara != -1:
        if to_int(_ctx_get_var(ctx, "EXP", [chara, _script_index(ctx, "EXP", "射精経験")])) == 0:
            text = "初物" + text
        elif to_int(_ctx_get_var(ctx, "EXP", [chara, _script_index(ctx, "EXP", "性交経験")])) == 0:
            text = "童貞" + text
        elif roll < 3:
            text = "濃厚" + text
        elif roll < 6:
            text = "特濃" + text
    elif roll < 3:
        text = "濃厚" + text
    elif roll < 6:
        text = "特濃" + text
    return text


def b_get_devil(ctx, args):
    if not _has_csv_name(ctx, "ABL", "種族"):
        return None
    race_idx = _csv_index(ctx, "ABL", "種族")
    target = to_int(arg(args, 0, 0))
    if to_int(arg(args, 1, -1)) >= 0:
        db = getattr(ctx.program, "csv", None)
        race = to_int(db.csv_value("ABL", target, race_idx, 0)) if db else 0
    else:
        race = to_int(_ctx_get_var(ctx, "ABL", [target, race_idx]))
    return 0 if race in _NON_DEVIL_RACES else 1


def b_get_next_exp(ctx, args):
    level = to_int(arg(args, 0, 0))
    multiplier = 5 if truth(arg(args, 1, 0)) else 4
    # Match emuera integer division order from:
    # 5 * ARG * (ARG+1) * (ARG+2) / 3 * (...) / 4
    value = int((5 * level * (level + 1) * (level + 2)) / 3)
    return int((value * multiplier) / 4)


def b_get_summoner_mlv(ctx, args):
    required = [
        ("BASE", "良好"),
        ("CFLAG", "ステート"),
        ("TALENT", "召喚師"),
        ("FLAG", "ポジション1"),
    ]
    if not all(_has_csv_name(ctx, var, name) for var, name in required):
        return None
    state_idx = _script_index(ctx, "CFLAG", "ステート")
    summoner_idx = _script_index(ctx, "TALENT", "召喚師")
    good_idx = _csv_index(ctx, "BASE", "良好")
    best = 0
    for pos_no in range(1, 7):
        chara = to_int(_ctx_get_var(ctx, "FLAG", [f"ポジション{pos_no}"]))
        if chara <= -1:
            continue
        state_offset = to_int(_ctx_get_var(ctx, "CFLAG", [chara, state_idx]))
        if _csv_name(ctx, "BASE", good_idx + state_offset) in _GET_SUMMONER_MLV_BAD_STATES:
            continue
        best = max(best, to_int(_ctx_get_var(ctx, "TALENT", [chara, summoner_idx])))
    return best


def _literal_chara_value(ctx, base: str, chara: int, name: str) -> int:
    return to_int(_ctx_get_var(ctx, base, [chara, _script_index(ctx, base, name)]))


def _state_name(ctx, chara: int) -> str:
    good_idx = _csv_index(ctx, "BASE", "良好")
    state_idx = _script_index(ctx, "CFLAG", "ステート")
    state_offset = to_int(_ctx_get_var(ctx, "CFLAG", [chara, state_idx]))
    return _csv_name(ctx, "BASE", good_idx + state_offset)


def b_convert_badstate_name(ctx, args):
    name = to_str(arg(args, 0, ""))
    return _BADSTATE_ALIASES.get(name.upper(), name)


def b_actionable_chara_f(ctx, args):
    if not _has_csv_names(ctx, "BASE", ("良好",)) or not _has_csv_names(ctx, "CFLAG", ("ステート",)):
        return None
    return 0 if _state_name(ctx, to_int(arg(args, 0, 0))) in _ACTIONABLE_BAD_STATES else 1


def b_is_badstate(ctx, args):
    if not _has_csv_names(ctx, "BASE", ("良好",)) or not _has_csv_names(ctx, "CFLAG", ("ステート",)):
        return None
    chara = to_int(arg(args, 0, 0))
    return 1 if _state_name(ctx, chara) == b_convert_badstate_name(ctx, [arg(args, 1, "")]) else 0


def b_is_friend(ctx, args):
    if not _has_csv_names(ctx, "CFLAG", ("PTフラグ",)):
        return None
    left = to_int(arg(args, 0, 0))
    right = to_int(arg(args, 1, 0))
    if left < 0 or right < 0:
        return 0
    left_party = _cflag(ctx, left, "PTフラグ")
    right_party = _cflag(ctx, right, "PTフラグ")
    if left_party > 0 and right_party <= 0:
        return 0
    if right_party > 0 and left_party <= 0:
        return 0
    return 1


def b_is_front(ctx, args):
    pos = b_cpos(ctx, [arg(args, 0, 0)])
    return 1 if (1 <= pos <= 3 or 7 <= pos <= 16) else 0


def b_get_btl_range(ctx, args):
    friend = b_is_friend(ctx, args)
    if friend is None:
        return None
    if truth(friend):
        return 0
    return 1 + (0 if truth(b_is_front(ctx, [arg(args, 0, 0)])) else 1) + (0 if truth(b_is_front(ctx, [arg(args, 1, 0)])) else 1)


def b_get_pos_min(ctx, args):
    target = to_int(arg(args, 0, 0))
    if 1 <= target <= 16:
        return target
    if target in {17, 19, 23}:
        return 1
    if target == 18:
        return 4
    if target in {20, 22}:
        return 7
    if target == 21:
        return 12
    _ctx_set_var(ctx, "STR", [-1], "指定されたターゲットが異常な数値です。")
    return None


def b_get_pos_max(ctx, args):
    target = to_int(arg(args, 0, 0))
    if 1 <= target <= 16:
        return target
    if target == 17:
        return 3
    if target in {18, 19}:
        return 6
    if target == 20:
        return 11
    if target in {21, 22, 23}:
        return 16
    _ctx_set_var(ctx, "STR", [-1], "指定されたターゲットが異常な数値です。")
    return None


def b_get_weakness(ctx, args):
    value = to_int(arg(args, 0, 0))
    if value == 999:
        return "反射"
    if value < 0:
        return "吸収"
    if value == 0:
        return "無効"
    if value < 100:
        return "耐性"
    if value == 100:
        return "通常"
    return "弱点"


def b_is_target_able(ctx, args):
    if not _has_csv_names(ctx, "BASE", ("良好", "瀕死")) or not _has_csv_names(ctx, "CFLAG", ("ステート",)):
        return None
    chara = to_int(arg(args, 0, 0))
    if chara < 0:
        return 0
    if b_cpos(ctx, [chara]) <= 0:
        return 0
    dead_state = _csv_index(ctx, "BASE", "瀕死") - _csv_index(ctx, "BASE", "良好")
    if _cflag(ctx, chara, "ステート") == dead_state:
        return 0
    return 1


def _btl_no_initial_link(ctx, chara: int) -> int:
    no = to_int(_ctx_get_var(ctx, "NO", [chara]))
    initial_idx = _csv_index(ctx, "CFLAG", "初期LINK悪魔")
    if truth(b_csvcflag(ctx, [no, initial_idx])):
        return no
    return _cflag(ctx, chara, "初期LINK悪魔")


def _btl_no_linked_no(ctx, chara: int) -> int | None:
    link = _cflag(ctx, chara, "リンク悪魔")
    if link > 0:
        linked = b_findchara_id(ctx, [link])
        if linked > 0:
            return to_int(_ctx_get_var(ctx, "NO", [linked]))
    return None


def _persona_no_slot(ctx) -> Value:
    # In real eraMegaten `Persona("NO")` is a #FUNCTION returning a numeric
    # DITEMTYPE column.  Tiny isolated tests may not load that script; keep the
    # DITEMTYPE name table fallback when available before using the parser's
    # literal fallback for bare-minimum fixtures.
    if _ctx_can_call_script(ctx, "Persona"):
        try:
            return to_int(ctx.call_expr_function("Persona", ["NO"]))
        except Exception:
            pass
    if _str_find(ctx, "Persona資料／NO", complete=True) >= 0:
        return to_int(b_get_ditemtype_num(ctx, ["NO"]))
    return 'Persona("NO")'


def b_btl_no(ctx, args):
    required = [
        ("CFLAG", "悪魔変身"), ("CFLAG", "リンク悪魔"), ("CFLAG", "初期LINK悪魔"),
        ("TALENT", "悪魔変身"), ("TALENT", "喰奴"), ("TALENT", "Aion式召喚術"), ("TALENT", "Persona使"),
        ("EQUIP", "装備Persona"),
    ]
    if not all(_has_csv_name(ctx, var, name) for var, name in required):
        return None
    chara = to_int(arg(args, 0, 0))
    own_no = to_int(_ctx_get_var(ctx, "NO", [chara]))
    if (_talent(ctx, chara, "悪魔変身") or _talent(ctx, chara, "喰奴")) and _cflag(ctx, chara, "悪魔変身"):
        linked_no = _btl_no_linked_no(ctx, chara)
        return linked_no if linked_no is not None else _btl_no_initial_link(ctx, chara)
    if _talent(ctx, chara, "Aion式召喚術"):
        linked_no = _btl_no_linked_no(ctx, chara)
        return linked_no if linked_no is not None else own_no
    if _talent(ctx, chara, "Persona使"):
        equip = to_int(_ctx_get_var(ctx, "EQUIP", [chara, _script_index(ctx, "EQUIP", "装備Persona")]))
        return own_no if equip == 0 else to_int(_ctx_get_var(ctx, "DITEMTYPE", [equip, _persona_no_slot(ctx)]))
    return own_no


def _can_count_active_party_chara(ctx, chara: int) -> bool:
    return _state_name(ctx, chara) not in _GET_SUMMONER_MLV_BAD_STATES


def _party_position_chara(ctx, pos_no: int) -> int:
    return to_int(_ctx_get_var(ctx, "FLAG", [f"ポジション{pos_no}"]))


def b_get_summoner_lv(ctx, args):
    required = [("BASE", "良好"), ("CFLAG", "ステート"), ("TALENT", "召喚師"), ("FLAG", "ポジション1")]
    if not all(_has_csv_name(ctx, var, name) for var, name in required):
        return None
    total = 0
    summoner_idx = _script_index(ctx, "TALENT", "召喚師")
    for pos_no in range(1, 7):
        chara = _party_position_chara(ctx, pos_no)
        if chara <= -1:
            continue
        if not _can_count_active_party_chara(ctx, chara):
            continue
        # Preserve GET_SUMMONER_LV.ERB exactly: LOCAL:1 +=
        # MAX(TALENT:(FLAG:LOCALS):召喚師, LOCAL:1).
        total += max(to_int(_ctx_get_var(ctx, "TALENT", [chara, summoner_idx])), total)
    return total


def b_num_summoner(ctx, args):
    required = [("BASE", "良好"), ("CFLAG", "ステート"), ("TALENT", "召喚師"), ("FLAG", "ポジション1")]
    if not all(_has_csv_name(ctx, var, name) for var, name in required):
        return None
    if _flag(ctx, "COMP使用不能"):
        return 0
    threshold = to_int(arg(args, 0, 3))
    summoner_idx = _script_index(ctx, "TALENT", "召喚師")
    count = 0
    for pos_no in range(1, 7):
        chara = _party_position_chara(ctx, pos_no)
        if chara <= -1:
            continue
        if to_int(_ctx_get_var(ctx, "TALENT", [chara, summoner_idx])) < threshold:
            continue
        if _can_count_active_party_chara(ctx, chara):
            count += 1
    return count


def b_num_haveskill(ctx, args):
    required = [("BASE", "良好"), ("CFLAG", "ステート"), ("FLAG", "ポジション1")]
    if not all(_has_csv_name(ctx, var, name) for var, name in required):
        return None
    skill = to_int(arg(args, 0, 0))
    count = 0
    for pos_no in range(1, 7):
        chara = _party_position_chara(ctx, pos_no)
        if chara <= -1:
            continue
        have = b_have_skill(ctx, [chara, skill])
        if have is None:
            return None
        if truth(have) and _can_count_active_party_chara(ctx, chara):
            count += 1
    return count


def b_csv_numeric(ctx, args, var: str):
    no = to_int(arg(args, 0)); idx = to_int(arg(args, 1)); sp = to_int(arg(args, 2, 0)) != 0
    if ctx.program.csv:
        return ctx.program.csv.csv_value(var, no, idx, 0, sp=sp)
    return 0

def b_csvbase(ctx, args): return b_csv_numeric(ctx, args, "BASE")
def b_csvabl(ctx, args): return b_csv_numeric(ctx, args, "ABL")
def b_csvtalent(ctx, args): return b_csv_numeric(ctx, args, "TALENT")
def b_csvcflag(ctx, args): return b_csv_numeric(ctx, args, "CFLAG")
def b_csvequip(ctx, args): return b_csv_numeric(ctx, args, "EQUIP")
def b_csvexp(ctx, args): return b_csv_numeric(ctx, args, "EXP")
def b_csvmark(ctx, args): return b_csv_numeric(ctx, args, "MARK")
def b_csvrelation(ctx, args): return b_csv_numeric(ctx, args, "RELATION")

def b_csvcharanum(ctx, args):
    db = getattr(ctx.program, "csv", None)
    if not db:
        return 0
    sp = to_int(arg(args, 0, 0)) != 0
    table = db.sp_characters if sp else db.characters
    return (max(table) + 1) if table else 0

def b_csv_string(ctx, args, var: str):
    no = to_int(arg(args, 0)); idx = to_int(arg(args, 1, 0)); sp = to_int(arg(args, 2, 0)) != 0
    if ctx.program.csv:
        return to_str(ctx.program.csv.csv_value(var, no, idx, "", sp=sp))
    return ""

def b_csvname(ctx, args):
    no = to_int(arg(args, 0))
    sp = to_int(arg(args, 1, 0)) != 0
    if ctx.program.csv:
        tmpl = ctx.program.csv.csv_template(no, sp=sp)
        if tmpl:
            return tmpl.name
    return ""

def b_csvcallname(ctx, args):
    no = to_int(arg(args, 0))
    sp = to_int(arg(args, 1, 0)) != 0
    if ctx.program.csv:
        tmpl = ctx.program.csv.csv_template(no, sp=sp)
        if tmpl:
            return tmpl.callname
    return ""

def b_csv_chara_string(ctx, args, var: str):
    no = to_int(arg(args, 0))
    sp = to_int(arg(args, 1, 0)) != 0
    if ctx.program.csv:
        tmpl = ctx.program.csv.csv_template(no, sp=sp)
        if not tmpl:
            return ""
        return tmpl.strings.get(var, {}).get(0, "")
    return ""

def b_csvnickname(ctx, args): return b_csv_chara_string(ctx, args, "NICKNAME")
def b_csvmastername(ctx, args): return b_csv_chara_string(ctx, args, "MASTERNAME")

def b_csvcstr(ctx, args): return b_csv_string(ctx, args, "CSTR")

def _looks_numeric(value: Value) -> bool:
    if isinstance(value, int):
        return True
    try:
        parse_era_int(to_str(value).strip())
        return True
    except Exception:
        return False

def _find_ref(ctx, var_text: str):
    try:
        return parse_lvalue(ctx, var_text)
    except Exception:
        return None

def _chara_var_value(ctx, ref, chara_index: int) -> Value:
    return ctx.memory.get_var(ref.base, [chara_index, *ref.indices])

def _find_value_matches(ctx, base: str, value: Value, needle: Value) -> bool:
    if ctx.memory.is_string_base(base) or isinstance(value, str) or (isinstance(needle, str) and not _looks_numeric(needle)):
        return to_str(value) == to_str(needle)
    return to_int(value) == to_int(needle)

def _find_element_matches(ctx, base: str, value: Value, needle: Value, *, complete: bool) -> bool:
    string_mode = ctx.memory.is_string_base(base) or isinstance(value, str) or (isinstance(needle, str) and not _looks_numeric(needle))
    if not string_mode:
        return to_int(value) == to_int(needle)
    text = to_str(value)
    pattern = to_str(needle)
    try:
        return (re.fullmatch(pattern, text) is not None) if complete else (re.search(pattern, text) is not None)
    except re.error:
        return text == pattern if complete else pattern in text

def _ctx_memory(ctx):
    return getattr(ctx, "memory", ctx)

def _ctx_get_var(ctx, base: str, indices: list[Value]) -> Value:
    if hasattr(ctx, "get_var"):
        return ctx.get_var(base, indices)
    return _ctx_memory(ctx).get_var(base, indices)

def _ctx_set_var(ctx, base: str, indices: list[Value], value: Value) -> None:
    if hasattr(ctx, "set_var"):
        ctx.set_var(base, indices, value)
    else:
        _ctx_memory(ctx).set_var(base, indices, value)

def _ctx_can_call_script(ctx, name: str) -> bool:
    # Runtime can dispatch ERB #FUNCTION bodies through _call_sync.  Memory's
    # small EvalContext facade intentionally cannot, even though it exposes
    # has_callable(), so avoid silently treating script predicates as false.
    return bool(hasattr(ctx, "_call_sync") and hasattr(ctx, "has_callable") and ctx.has_callable(name))

def _character_cflag(ctx, chara_index: int, name: str, default: int = 0) -> int:
    try:
        return to_int(_ctx_get_var(ctx, "CFLAG", [chara_index, name]))
    except Exception:
        return default

def b_findchara(ctx, args):
    # FINDCHARA(VAR, value[, start[, end]]) or GETCHARA(no)
    if not args:
        return -1
    if len(args) == 1:
        no = to_int(args[0])
        for i, ch in enumerate(ctx.memory.characters):
            if ch.template_no == no:
                return i
        return -1
    ref = _find_ref(ctx, to_str(args[0]))
    if ref is None:
        return -1
    needle = args[1]
    start = max(0, to_int(arg(args, 2, 0)))
    end = to_int(args[3]) if len(args) >= 4 and args[3] != "" else len(ctx.memory.characters)
    end = min(max(0, end), len(ctx.memory.characters))
    if end < start:
        return -1
    for i in range(start, end):
        if _find_value_matches(ctx, ref.base, _chara_var_value(ctx, ref, i), needle):
            return i
    return -1

def _character_state_is_sp(ch) -> bool:
    return to_int(ch.numeric.get("CFLAG", {}).get((0,), 0)) != 0

def _find_chara_by_no(ctx, no: int, *, include_sp: bool = False, sp_only: bool = False) -> int:
    first_sp = -1
    for i, ch in enumerate(_ctx_memory(ctx).characters):
        if ch.template_no != no:
            continue
        is_sp = _character_state_is_sp(ch)
        if sp_only:
            if is_sp:
                return i
            continue
        if is_sp:
            if first_sp < 0:
                first_sp = i
            continue
        return i
    return first_sp if include_sp and not sp_only else -1

def b_getchara(ctx, args):
    if not args:
        return -1
    no = to_int(arg(args, 0))
    include_sp = to_int(arg(args, 1, 0)) != 0
    return _find_chara_by_no(ctx, no, include_sp=include_sp)

def b_getspchara(ctx, args):
    if not args:
        return -1
    return _find_chara_by_no(ctx, to_int(arg(args, 0)), sp_only=True)

def b_findchara_id(ctx, args):
    if not args:
        return -1
    needle = to_int(arg(args, 0))
    for i in range(len(_ctx_memory(ctx).characters)):
        if _character_cflag(ctx, i, "キャラ固有の番号") == needle:
            return i
    return -1

def _inputable_chara(ctx, chara_index: int) -> bool:
    if _ctx_can_call_script(ctx, "INPUTABLEF_CHARA"):
        try:
            return truth(ctx.call_expr_function("INPUTABLEF_CHARA", [chara_index]))
        except Exception:
            return True
    return True

def b_findchara_b(ctx, args):
    if not args:
        _ctx_set_var(ctx, "RESULT", [1], -1)
        return -1
    no = to_int(arg(args, 0))
    event_join = to_int(arg(args, 1, -100))
    best_value = -1
    best_chara = -1
    mem = _ctx_memory(ctx)
    for i in range(len(mem.characters) - 1, -1, -1):
        ch = mem.characters[i]
        if ch.template_no != no:
            continue
        if event_join > -100 and _character_cflag(ctx, i, "事件加入") != event_join:
            continue
        pt_flag = _character_cflag(ctx, i, "PTフラグ")
        if pt_flag == 0:
            continue
        if pt_flag == 1:
            if _character_cflag(ctx, i, "所属ＣＯＭＰ") == -1:
                # 自宅サーバー内の悪魔: lowest priority, keep the first hit
                # seen by the original reverse scan.
                if best_value == -1:
                    best_value = 0
                    best_chara = i
            elif best_value % 10 != 2:
                if _inputable_chara(ctx, i):
                    best_value = 1
                    best_chara = i
                elif best_value != 1:
                    best_value = 11
                    best_chara = i
        else:
            # PTフラグ == 2 is the normal battle-member case in eraMegaten.
            # The script treats any non-1 non-zero value through this branch.
            if _inputable_chara(ctx, i):
                _ctx_set_var(ctx, "RESULT", [1], i)
                return 2
            if best_value != 2:
                best_value = 12
                best_chara = i
    _ctx_set_var(ctx, "RESULT", [1], best_chara)
    return best_value

def b_findchara_m(ctx, args):
    chara_index = to_int(arg(args, 0, -1))
    mem = _ctx_memory(ctx)
    if chara_index < 0 or chara_index >= len(mem.characters):
        return 0
    no = mem.characters[chara_index].template_no
    return sum(1 for x in args[1:11] if _scalar_values_match(no, x))


def b_contract(ctx, args):
    chara = to_int(arg(args, 0, 0))
    if _talent(ctx, chara, "妻") or _talent(ctx, chara, "夫"):
        return 1
    if _talent(ctx, chara, "淫魔"):
        return 2
    if _talent(ctx, chara, "玩具"):
        return 3
    if _talent(ctx, chara, "盟友"):
        return 4
    return 0


def b_findchara_no_c(ctx, args):
    no = to_int(arg(args, 0, 0))
    if not _has_csv_names(ctx, "CFLAG", ("PTフラグ", "所属ＣＯＭＰ")):
        return None
    mem = _ctx_memory(ctx)
    for i, ch in enumerate(mem.characters):
        if _cflag(ctx, i, "PTフラグ") == 0:
            continue
        if ch.template_no in {3501, 3502, 4402}:
            continue
        if ch.template_no == no and b_contract(ctx, [i]) == 0 and _cflag(ctx, i, "所属ＣＯＭＰ") != -1:
            return i
    return -1


def b_findchara_enemy(ctx, args):
    no = to_int(arg(args, 0, 0))
    event_join = to_int(arg(args, 1, -100))
    required = [("BASE", "良好"), ("CFLAG", "ステート"), ("CFLAG", "事件加入"), ("CFLAG", "PTフラグ")]
    if not all(_has_csv_name(ctx, var, name) for var, name in required):
        return None
    result = 0
    result_chara = -1
    mem = _ctx_memory(ctx)
    for i, ch in enumerate(mem.characters):
        if ch.template_no != no:
            continue
        if event_join > -100 and _cflag(ctx, i, "事件加入") != event_join:
            continue
        if _cflag(ctx, i, "PTフラグ") > 0:
            continue
        if _state_name(ctx, i) == "瀕死":
            result = 3
            result_chara = i
        elif _inputable_chara(ctx, i):
            _ctx_set_var(ctx, "RESULT", [1], i)
            return 1
        else:
            result = 2
            result_chara = i
    _ctx_set_var(ctx, "RESULT", [1], result_chara)
    return result


def b_charanum_digit(ctx, args):
    n = to_int(_ctx_get_var(ctx, "CHARANUM", []))
    return 1 if n <= 10 else 2 if n <= 100 else 3


def _count_nakama(ctx, *, headcount: bool = False) -> int | None:
    required = [("ABL", "種族"), ("CFLAG", "容量未使用"), ("CFLAG", "所属ＣＯＭＰ"), ("CFLAG", "PTフラグ")]
    if not all(_has_csv_name(ctx, var, name) for var, name in required):
        return None
    master = _master(ctx)
    count = 0
    for i in range(len(_ctx_memory(ctx).characters)):
        if i == master:
            continue
        race = _literal_chara_value(ctx, "ABL", i, "種族")
        if race < 1 or race > 45:
            continue
        if _cflag(ctx, i, "容量未使用"):
            continue
        if _cflag(ctx, i, "所属ＣＯＭＰ") == -1:
            continue
        if _cflag(ctx, i, "PTフラグ") == 0:
            continue
        count += 1
    if not headcount and count <= 6 and to_int(_ctx_get_var(ctx, "EQUIP", [master, _script_index(ctx, "EQUIP", "恶魔会议室")])):
        return 0
    return count


def b_num_nakama(ctx, args):
    return _count_nakama(ctx, headcount=False)


def b_num_nakama_headcount(ctx, args):
    return _count_nakama(ctx, headcount=True)


def _soft_capacity(ctx) -> int:
    if _ctx_can_call_script(ctx, "ソフト容量"):
        try:
            return to_int(ctx.call_expr_function("ソフト容量", []))
        except Exception:
            pass
    return 0


def b_comp_empty_capacity(ctx, args):
    head = b_num_nakama_headcount(ctx, [])
    num = b_num_nakama(ctx, [])
    if head is None or num is None:
        return None
    soft = _soft_capacity(ctx)
    capacity = _flag(ctx, "ＣＯＭＰ容量")
    if to_int(_ctx_get_var(ctx, "EQUIP", [_master(ctx), _script_index(ctx, "EQUIP", "恶魔会议室")])):
        if head < 6 or capacity > head + soft:
            return 1
        if (head == 6 and capacity - soft < 7) or (head > 6 and capacity == head + soft):
            return 0
        return -1
    if capacity > num + soft:
        return 1
    if capacity == num + soft:
        return 0
    return -1


def b_num_fusionable(ctx, args):
    required = [("ABL", "種族"), ("CFLAG", "合体不可"), ("CFLAG", "この場に居ないフラグ"), ("CFLAG", "PTフラグ")]
    if not all(_has_csv_name(ctx, var, name) for var, name in required):
        return None
    count = 0
    for i in range(len(_ctx_memory(ctx).characters)):
        race = _literal_chara_value(ctx, "ABL", i, "種族")
        if race < 1 or race > 44:
            continue
        if _cflag(ctx, i, "合体不可") or _cflag(ctx, i, "この場に居ないフラグ") or _cflag(ctx, i, "PTフラグ") == 0:
            continue
        count += 1
    return count


def b_num_zouma(ctx, args):
    if not _has_csv_names(ctx, "ABL", ("種族",)) or not _has_csv_names(ctx, "CFLAG", ("PTフラグ",)):
        return None
    count = 0
    for i in range(len(_ctx_memory(ctx).characters)):
        if _literal_chara_value(ctx, "ABL", i, "種族") == 45 and _cflag(ctx, i, "PTフラグ") != 0:
            count += 1
    return count


def b_findlastchara(ctx, args):
    if len(args) < 2:
        return b_findchara(ctx, args)
    ref = _find_ref(ctx, to_str(args[0]))
    if ref is None:
        return -1
    needle = args[1]
    start = max(0, to_int(arg(args, 2, 0)))
    end = to_int(args[3]) if len(args) >= 4 and args[3] != "" else len(ctx.memory.characters)
    end = min(max(0, end), len(ctx.memory.characters))
    if end < start:
        return -1
    for i in range(end - 1, start - 1, -1):
        if _find_value_matches(ctx, ref.base, _chara_var_value(ctx, ref, i), needle):
            return i
    return -1

def b_charanum_check(ctx, args):
    i = to_int(arg(args, 0))
    return 1 if 0 <= i < len(ctx.memory.characters) else 0

def b_pos(ctx, args): return _position_chara_by_flag(ctx, to_int(arg(args, 0)))
def b_cpos(ctx, args):
    if not _has_csv_name(ctx, "CFLAG", "ポジション"):
        return to_int(arg(args, 0))
    return _cflag(ctx, to_int(arg(args, 0, 0)), "ポジション")

def b_sumarray(ctx, args):
    vals = _array_values(ctx, to_str(arg(args, 0)), args[1:])
    return sum(vals)

def b_sumcarray(ctx, args):
    vals = [to_int(v) for v in _carray_values(ctx, to_str(arg(args, 0)), args[1:])]
    return sum(vals)

def b_maxarray(ctx, args):
    vals = _array_values(ctx, to_str(arg(args, 0)), args[1:])
    return max(vals) if vals else 0

def b_minarray(ctx, args):
    vals = _array_values(ctx, to_str(arg(args, 0)), args[1:])
    return min(vals) if vals else 0

def b_maxcarray(ctx, args):
    vals = [to_int(v) for v in _carray_values(ctx, to_str(arg(args, 0)), args[1:])]
    return max(vals) if vals else 0

def b_mincarray(ctx, args):
    vals = [to_int(v) for v in _carray_values(ctx, to_str(arg(args, 0)), args[1:])]
    return min(vals) if vals else 0

def b_inrangearray(ctx, args):
    vals = _array_values(ctx, to_str(arg(args, 0)), args[3:])
    lo = to_int(arg(args, 1))
    hi = to_int(arg(args, 2))
    return sum(1 for value in vals if lo <= to_int(value) < hi)

def b_inrangecarray(ctx, args):
    vals = _carray_values(ctx, to_str(arg(args, 0)), args[3:])
    lo = to_int(arg(args, 1))
    hi = to_int(arg(args, 2))
    return sum(1 for value in vals if lo <= to_int(value) < hi)

def _carray_suffix(ref) -> list[Value]:
    base = norm_name(ref.base)
    indices = list(ref.indices)
    if base in {"NO", "NAME", "CALLNAME", "NICKNAME", "MASTERNAME"}:
        return indices[1:] if len(indices) >= 2 else []
    # C-array functions scan the character-position axis.  eraMegaten writes
    # both CMATCH(CFLAG:キャラ固有の番号, id) (only the per-character slot is
    # supplied) and MAXCARRAY(BASE:ARG:LV) (a dummy current character is
    # supplied before the slot).  With two or more indices, replace the first
    # one by the scan character; with one index, keep it as the slot.
    return indices[1:] if len(indices) >= 2 else indices

def _carray_range(ctx, range_args: list[Value]) -> tuple[int, int]:
    charanum = len(_ctx_memory(ctx).characters)
    if len(range_args) >= 2:
        start = to_int(range_args[0])
        end = to_int(range_args[1])
    elif len(range_args) == 1:
        start = 0
        end = to_int(range_args[0])
    else:
        start = 0
        end = charanum
    start = max(0, start)
    end = max(start, min(charanum, end))
    return start, end

def _carray_values(ctx, ref_text: str, range_args: list[Value]) -> list[Value]:
    ref = _safe_lvalue(ctx, ref_text)
    if not ref:
        return []
    suffix = _carray_suffix(ref)
    start, end = _carray_range(ctx, range_args)
    return [_ctx_get_var(ctx, ref.base, [i, *suffix]) for i in range(start, end)]

def _cmatch(ctx, args):
    if len(args) < 2:
        return 0
    ref_text = to_str(arg(args, 0))
    ref = _safe_lvalue(ctx, ref_text)
    if not ref:
        return 0
    needle = args[1]
    suffix = _carray_suffix(ref)
    start, end = _carray_range(ctx, args[2:])
    count = 0
    for i in range(start, end):
        if _find_value_matches(ctx, ref.base, _ctx_get_var(ctx, ref.base, [i, *suffix]), needle):
            count += 1
    return count

def _findelement_bounds(ctx, ref, var: str, args: list[Value]) -> tuple[int, int]:
    start = to_int(args[2]) if len(args) >= 3 and args[2] != "" else 0
    if len(args) >= 4 and args[3] != "":
        end = to_int(args[3])
    else:
        prefix = ref.indices[:-1] if ref and ref.indices else []
        base = ref.base if ref else var
        dims = _ctx_array_dimensions(ctx, base)
        scan_axis = len(prefix)
        if dims and scan_axis < len(dims):
            end = dims[scan_axis]
        elif dims and len(dims) == 1:
            end = dims[0]
        else:
            inferred = _materialized_scan_end(ctx, base, prefix)
            if inferred:
                end = inferred
            else:
                array_size = getattr(ctx, "array_size", None)
                if array_size is not None:
                    end = int(array_size(base))
                else:
                    end = 10000
    start = max(0, start)
    end = max(0, end)
    return start, end

def _findelement_scan(ctx, args: list[Value], *, reverse: bool) -> int:
    var = _raw_identifier_text(arg(args, 0))
    needle = arg(args, 1)
    ref = _safe_lvalue(ctx, var)
    start, end = _findelement_bounds(ctx, ref, var, args)
    complete = truth(arg(args, 4, 0))
    if ref and ref.indices:
        prefix = ref.indices[:-1]
        base = ref.base
        indices = range(end - 1, start - 1, -1) if reverse else range(start, end)
        for i in indices:
            value = ctx.memory.get_var(base, prefix + [i])
            if _find_element_matches(ctx, base, value, needle, complete=complete):
                return i
        return -1
    base = ref.base if ref else var
    indices = range(end - 1, start - 1, -1) if reverse else range(start, end)
    for i in indices:
        value = ctx.memory.get_var(base, [i])
        if _find_element_matches(ctx, base, value, needle, complete=complete):
            return i
    return -1

def b_findelement(ctx, args):
    return _findelement_scan(ctx, args, reverse=False)

def b_findlastelement(ctx, args):
    return _findelement_scan(ctx, args, reverse=True)

def _safe_lvalue(ctx, text: str):
    try:
        return parse_lvalue(ctx, _raw_identifier_text(text))
    except Exception:
        return None

def _ctx_array_dimensions(ctx, base: str) -> tuple[int, ...]:
    getter = getattr(ctx, "array_dimensions", None)
    if callable(getter):
        try:
            return tuple(max(0, int(x)) for x in getter(base))
        except Exception:
            pass
    key = norm_name(_raw_identifier_text(base))
    memory = getattr(ctx, "memory", None)
    if memory is not None and getattr(memory, "frame", None) is not None:
        dims = memory.frame.dims.get(key)
        if dims:
            return tuple(max(0, int(x)) for x in dims)
    program = getattr(ctx, "program", None)
    decls = getattr(program, "var_decls", {}) if program is not None else {}
    decl = decls.get(key)
    if decl and getattr(decl, "dims", ()):
        return tuple(max(0, int(x)) for x in decl.dims)
    csv = getattr(program, "csv", None) if program is not None else None
    sizes = getattr(csv, "variable_sizes", {}) if csv is not None else {}
    if key in sizes:
        raw_dims = sizes[key]
        if isinstance(raw_dims, (tuple, list)):
            return tuple(max(0, int(x)) for x in raw_dims)
        return (max(0, int(raw_dims)),)
    return ()

def _ref_alias_for(ctx, base: str):
    memory = getattr(ctx, "memory", None)
    frame = getattr(memory, "frame", None) if memory is not None else None
    if frame is None:
        return None
    return frame.ref_aliases.get(norm_name(base))

def _ref_alias_local_index(alias, source_idx: tuple[int, ...]) -> tuple[int, ...] | None:
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

def _ref_alias_materialized_local_indices(ctx, base: str) -> list[tuple[int, ...]]:
    memory = getattr(ctx, "memory", None)
    alias = _ref_alias_for(ctx, base)
    if memory is None or alias is None:
        return []
    out: set[tuple[int, ...]] = set()
    for source_idx in memory._materialized_ref_alias_source_indices(alias):
        local_idx = _ref_alias_local_index(alias, source_idx)
        if local_idx is not None:
            out.add(local_idx)
    return sorted(out)

def _materialized_scan_end(ctx, base: str, prefix: list[Value]) -> int:
    memory = getattr(ctx, "memory", None)
    if memory is None:
        return 0
    key = norm_name(base)
    ref_alias = _ref_alias_for(ctx, key)
    if ref_alias is not None:
        resolved_prefix = tuple(memory.resolve_indices(ref_alias.base, prefix)) if prefix else ()
        max_index = -1
        for idx in _ref_alias_materialized_local_indices(ctx, key):
            if len(idx) > len(resolved_prefix) and idx[: len(resolved_prefix)] == resolved_prefix:
                max_index = max(max_index, idx[len(resolved_prefix)])
        return max_index + 1 if max_index >= 0 else 0
    resolved_prefix = tuple(memory.resolve_indices(base, prefix)) if prefix else ()
    tables: list[dict[tuple[int, ...], Value]] = []
    frame = getattr(memory, "frame", None)
    if frame is not None:
        for table_map in (frame.numeric, frame.strings):
            table = table_map.get(key)
            if table:
                tables.append(table)
    for table_map in (memory.numeric, memory.strings):
        table = table_map.get(key)
        if table:
            tables.append(table)
    max_index = -1
    for table in tables:
        for idx in table:
            if len(idx) > len(resolved_prefix) and idx[: len(resolved_prefix)] == resolved_prefix:
                max_index = max(max_index, idx[len(resolved_prefix)])
    return max_index + 1 if max_index >= 0 else 0

def _match_scan_end(ctx, ref, args: list[Value]) -> int:
    if len(args) >= 4 and args[3] != "":
        return to_int(args[3])
    prefix = ref.indices[:-1] if ref.indices else []
    dims = _ctx_array_dimensions(ctx, ref.base)
    scan_axis = len(prefix)
    if dims:
        if scan_axis < len(dims):
            return dims[scan_axis]
        # Built-in character variables are sized by their per-character second
        # dimension in VariableSize.csv; ABL:ARG:0 therefore scans 0..ABL size.
        if len(dims) == 1:
            return dims[0]
    inferred = _materialized_scan_end(ctx, ref.base, prefix)
    if inferred:
        return inferred
    array_size = getattr(ctx, "array_size", None)
    if callable(array_size):
        try:
            return int(array_size(ref.base))
        except Exception:
            pass
    return 10000

def _array_values(ctx, ref_text: str, range_args: list[Value]) -> list[int]:
    ref = _safe_lvalue(ctx, ref_text)
    if not ref:
        return [to_int(ref_text)]
    start: int | None = None
    end: int | None = None
    if len(range_args) >= 2:
        start = to_int(range_args[0])
        end = to_int(range_args[1])
    elif len(range_args) == 1:
        start = 0
        end = to_int(range_args[0])
    if start is not None and end is not None:
        prefix = ref.indices[:-1] if ref.indices else []
        return [to_int(ctx.memory.get_var(ref.base, prefix + [i])) for i in range(start, end)]
    # No explicit range: if the reference points to a scalar, use it. If it is a
    # bare array, scan currently materialized sparse entries.
    if ref.indices:
        return [to_int(ctx.memory.get_var(ref.base, ref.indices))]
    key = norm_name(ref.base)
    if _ref_alias_for(ctx, key) is not None:
        return [to_int(ctx.memory.get_var(key, list(idx))) for idx in _ref_alias_materialized_local_indices(ctx, key)]
    values: list[int] = []
    fr = ctx.memory.frame
    if fr:
        if key in fr.numeric:
            values.extend(to_int(v) for v in _canonical_table_values(fr.numeric[key]))
        if key in fr.strings:
            values.extend(to_int(v) for v in _canonical_table_values(fr.strings[key]))
    if key in ctx.memory.numeric:
        values.extend(to_int(v) for v in _canonical_table_values(ctx.memory.numeric[key]))
    if key in ctx.memory.strings:
        values.extend(to_int(v) for v in _canonical_table_values(ctx.memory.strings[key]))
    return values

def _canonical_table_values(table: dict[tuple[int, ...], Value]):
    for idx, value in table.items():
        if idx == () and (0,) in table:
            continue
        yield value

def b_printcperline(ctx, args):
    # Emuera exposes the configured PRINTC column count via PRINTCPERLINE().
    # eraMegaten reads it during SYSTEM/OPTION setup and also treats 0 as a
    # meaningful "do not auto-wrap PRINTC columns" value, so do not coerce 0
    # back to the default.
    return max(0, to_int(_config_raw(ctx, "PRINTCを並べる数")))
def b_printclength(ctx, args):
    value = to_int(_config_raw(ctx, "PRINTCの文字数"))
    return value if value > 0 else 25
def b_barstr(ctx, args):
    value = to_int(arg(args, 0))
    maximum = to_int(arg(args, 1, 1))
    width = max(0, to_int(arg(args, 2, 0)))
    if width <= 0:
        return "[]"
    if maximum <= 0:
        filled = width if value > 0 else 0
    else:
        filled = value * width // maximum
    filled = max(0, min(width, filled))
    return "[" + ("*" * filled) + ("." * (width - filled)) + "]"
def b_getmillisecond(ctx, args): return int(time.time() * 1000) - ctx.memory.start_millis
def b_gettime(ctx, args): return int(time.time() * 1000)
def b_gettimes(ctx, args): return time.strftime("%Y/%m/%d %H:%M:%S", time.localtime())
def b_getsecond(ctx, args): return int(time.time()) + 62135596800

def _window_config_int(ctx, key: str, default: int) -> int:
    value = to_int(_config_raw(ctx, key) or default)
    return value if value > 0 else default

def b_clientwidth(ctx, args):
    return _window_config_int(ctx, "ウィンドウ幅", 1600)

def b_clientheight(ctx, args):
    return _window_config_int(ctx, "ウィンドウ高さ", 950)

def b_getdisplayline(ctx, args):
    getter = getattr(ctx, "_get_display_line", None)
    if callable(getter):
        return to_str(getter(to_int(arg(args, 0, 0))))
    output = getattr(ctx, "output", [])
    try:
        lines = "".join(output).splitlines()
        index = to_int(arg(args, 0, 0))
        return lines[index] if 0 <= index < len(lines) else ""
    except Exception:
        return ""

def b_bitmap_cache_enable(ctx, args):
    value = truth(arg(args, 0, 1))
    try:
        setattr(ctx, "bitmap_cache_enabled", value)
    except Exception:
        pass
    return 1 if value else 0

def _emuera_relative_path(ctx, text: str):
    root = getattr(getattr(ctx, "program", None), "root", None)
    if root is None:
        return None
    rel = to_str(text).strip().replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return None
    parts = [part for part in rel.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        return None
    try:
        root_resolved = root.resolve()
        candidate = root_resolved.joinpath(*parts).resolve()
        if candidate == root_resolved or root_resolved in candidate.parents:
            return candidate
    except Exception:
        return None
    return None

def _emuera_relative_display_path(root, path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except Exception:
        rel = path.name
    return str(rel).replace("/", "\\")

def b_existfile(ctx, args):
    path = _emuera_relative_path(ctx, to_str(arg(args, 0, "")))
    return 1 if path is not None and path.is_file() else 0


def _emuera_sound_path(ctx, text: str):
    root = getattr(getattr(ctx, "program", None), "root", None)
    if root is None:
        return None
    rel = to_str(text).strip().replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return None
    parts = [part for part in rel.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    try:
        sound_root = (root / "sound").resolve()
        candidate = sound_root.joinpath(*parts).resolve(strict=False)
        if candidate == sound_root or sound_root not in candidate.parents:
            return None
        return candidate
    except Exception:
        return None


def _case_insensitive_file(path) -> bool:
    try:
        if path.is_file():
            return True
    except OSError:
        return False
    try:
        current = path.anchor and type(path)(path.anchor) or path
        parts = path.parts[1:] if path.anchor else path.parts
        for part in parts:
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


def b_existsound(ctx, args):
    name = to_str(arg(args, 0, ""))
    helper = getattr(ctx, "_sound_file_exists", None)
    if callable(helper):
        return 1 if helper(name) else 0
    path = _emuera_sound_path(ctx, name)
    return 1 if path is not None and _case_insensitive_file(path) else 0

def b_enumfiles(ctx, args):
    directory = _emuera_relative_path(ctx, to_str(arg(args, 0, "")))
    pattern = to_str(arg(args, 1, "*")) or "*"
    recursive = to_int(arg(args, 2, 0)) != 0
    matches = []
    if directory is not None and directory.is_dir():
        pattern_key = pattern.lower()
        iterator = directory.rglob("*") if recursive else directory.iterdir()
        for candidate in iterator:
            try:
                if candidate.is_file() and fnmatch.fnmatchcase(candidate.name.lower(), pattern_key):
                    matches.append(candidate)
            except OSError:
                continue
    root = getattr(getattr(ctx, "program", None), "root", directory)
    rel_matches = sorted((_emuera_relative_display_path(root, p) for p in matches), key=lambda s: s.lower())
    for i, value in enumerate(rel_matches):
        _ctx_set_var(ctx, "RESULTS", [i], value)
    _ctx_set_var(ctx, "RESULTS", [], rel_matches[0] if rel_matches else "")
    return len(rel_matches)

def _process_memory_usage_bytes() -> int:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            if ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
        except Exception:
            pass
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(usage if sys.platform == "darwin" else usage * 1024)
    except Exception:
        return max(1, sys.getsizeof(gc.get_objects()))

def b_getmemoryusage(ctx, args):
    return max(0, _process_memory_usage_bytes())

def b_clearmemory(ctx, args):
    before = _process_memory_usage_bytes()
    gc.collect()
    after = _process_memory_usage_bytes()
    return max(0, before - after)

def b_updatecheck(ctx, args):
    # Desktop update checks are host-UI side effects; terminal replay treats the
    # command as recognized and inert.
    return 0

def _raw_identifier_arg(value: Value) -> str:
    text = to_str(value).strip()
    if text.startswith('@"') and text.endswith('"') and len(text) >= 3:
        return text[2:-1].replace('""', '"')
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text[1:-1].replace('""', '"')
    return text

def b_isdefined(ctx, args):
    key = norm_name(_raw_identifier_arg(arg(args, 0, "")))
    defines = getattr(getattr(ctx, "program", None), "defines", {})
    return 1 if key in defines else 0

def _infer_existing_table_rank(table: dict[tuple[int, ...], Any] | None) -> int:
    if not table:
        return 0
    return max((len(idx) for idx in table if idx), default=0)

def _existvar_dims(ctx, key: str, *, frame_decl: bool) -> tuple[int, ...]:
    if frame_decl:
        frame = getattr(getattr(ctx, "memory", None), "frame", None)
        if frame is not None and key in frame.dims:
            return tuple(max(0, int(dim)) for dim in frame.dims[key])
    if hasattr(ctx, "array_dimensions"):
        try:
            dims = tuple(max(0, int(dim)) for dim in ctx.array_dimensions(key))
            if dims and dims != (100000,):
                return dims
        except Exception:
            pass
    program = getattr(ctx, "program", None)
    decl = getattr(program, "var_decls", {}).get(key) if program is not None else None
    if decl is not None and getattr(decl, "dims", ()):
        return tuple(max(0, int(dim)) for dim in decl.dims)
    csv = getattr(program, "csv", None) if program is not None else None
    sizes = getattr(csv, "variable_sizes", {}) if csv is not None else {}
    if key in sizes:
        raw = sizes[key]
        if isinstance(raw, (tuple, list)):
            return tuple(max(0, int(dim)) for dim in raw)
        return (max(0, int(raw)),)
    return ()

def b_existvar(ctx, args):
    # Emuera reports a bit field: integer=1, string=2, const=4,
    # two-dimensional=8, three-dimensional=16.  Keep local #DIM declarations
    # scoped to the active frame instead of leaking every loaded function's
    # declarations through the global Program index.
    raw = _raw_identifier_arg(arg(args, 0, "")).strip()
    if not raw:
        return 0
    key = norm_name(raw.split(":", 1)[0])
    flags = 0
    dims: tuple[int, ...] = ()
    memory = getattr(ctx, "memory", None)
    frame = getattr(memory, "frame", None)
    frame_decl = False
    if frame is not None:
        alias = getattr(frame, "ref_aliases", {}).get(key)
        if alias is not None:
            flags |= 2 if getattr(alias, "is_string", False) else 1
            dims = tuple(max(0, int(dim)) for dim in getattr(alias, "dims", ()))
            frame_decl = True
        elif key in getattr(frame, "strings", {}):
            flags |= 2
            dims = tuple(max(0, int(dim)) for dim in getattr(frame, "dims", {}).get(key, ()))
            if not dims:
                dims = ( _infer_existing_table_rank(frame.strings.get(key, {})), )
            frame_decl = True
        elif key in getattr(frame, "numeric", {}):
            flags |= 1
            dims = tuple(max(0, int(dim)) for dim in getattr(frame, "dims", {}).get(key, ()))
            if not dims:
                dims = ( _infer_existing_table_rank(frame.numeric.get(key, {})), )
            frame_decl = True
    program = getattr(ctx, "program", None)
    decl = getattr(program, "var_decls", {}).get(key) if program is not None else None
    if flags == 0 and decl is not None and (
        getattr(decl, "module_scope", False)
        or getattr(decl, "global_scope", False)
        or getattr(decl, "savedata", False)
        or getattr(decl, "charadata", False)
    ):
        flags |= 2 if getattr(decl, "is_string", False) else 1
        if getattr(decl, "const", False):
            flags |= 4
        dims = tuple(max(0, int(dim)) for dim in getattr(decl, "dims", ()))
    if flags == 0:
        if key in STRING_ARRAYS or key in CHARA_STRING_ARRAYS or (memory is not None and key in getattr(memory, "strings", {})):
            flags |= 2
        elif key in NUMERIC_ARRAYS or key in CHARA_NUMERIC_ARRAYS or (memory is not None and key in getattr(memory, "numeric", {})):
            flags |= 1
    if key in CHARA_NUMERIC_ARRAYS or key in CHARA_STRING_ARRAYS:
        dims = dims or (0, 0)
    if flags == 0:
        defines = getattr(program, "defines", {}) if program is not None else {}
        csv = getattr(program, "csv", None) if program is not None else None
        constants = getattr(csv, "constants", {}) if csv is not None else {}
        if key in defines or key in constants:
            flags |= 1 | 4
    if flags:
        dims = dims or _existvar_dims(ctx, key, frame_decl=frame_decl)
        rank = len([dim for dim in dims if dim != 0])
        if rank == 0:
            table = None
            if memory is not None:
                table = getattr(memory, "strings", {}).get(key) or getattr(memory, "numeric", {}).get(key)
            rank = _infer_existing_table_rank(table)
        if rank >= 2:
            flags |= 8
        if rank >= 3:
            flags |= 16
    return flags

def _enum_match_name(name: str, keyword: str, mode: str) -> bool:
    lhs = norm_name(name)
    rhs = norm_name(keyword)
    if mode == "BEGIN":
        return lhs.startswith(rhs)
    if mode == "END":
        return lhs.endswith(rhs)
    return rhs in lhs

def _enum_mode(ctx, prefix: str) -> str:
    call_name = norm_name(getattr(ctx, "_builtin_call_name", prefix + "WITH"))
    if call_name.endswith("BEGINSWITH"):
        return "BEGIN"
    if call_name.endswith("ENDSWITH"):
        return "END"
    return "WITH"

def _write_enum_results(ctx, names: list[str]) -> int:
    for i, value in enumerate(names):
        _ctx_set_var(ctx, "RESULTS", [i], value)
    _ctx_set_var(ctx, "RESULTS", [], names[0] if names else "")
    return len(names)

def _unique_preserve_order(values) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = to_str(value)
        key = norm_name(text)
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out

def b_existfunction(ctx, args):
    name = _raw_identifier_arg(arg(args, 0, ""))
    program = getattr(ctx, "program", None)
    if program is None:
        return 0
    fn = program.get_function(name) if hasattr(program, "get_function") else None
    if fn is None:
        return 0
    if getattr(fn, "is_function", False):
        return 3 if getattr(fn, "returns_string", False) else 2
    return 1

def b_enumfunc(ctx, args):
    keyword = _raw_identifier_arg(arg(args, 0, ""))
    mode = _enum_mode(ctx, "ENUMFUNC")
    program = getattr(ctx, "program", None)
    functions = []
    if program is not None:
        for group in getattr(program, "functions", {}).values():
            functions.extend(group)
    functions.sort(key=lambda fn: (getattr(fn, "source_file", 0), getattr(fn, "source_line", 0), norm_name(getattr(fn, "name", ""))))
    names = _unique_preserve_order(getattr(fn, "name", "") for fn in functions)
    return _write_enum_results(ctx, [name for name in names if _enum_match_name(name, keyword, mode)])

def b_enummacro(ctx, args):
    keyword = _raw_identifier_arg(arg(args, 0, ""))
    mode = _enum_mode(ctx, "ENUMMACRO")
    program = getattr(ctx, "program", None)
    if program is None:
        return _write_enum_results(ctx, [])
    define_names = getattr(program, "define_names", {})
    names = [define_names.get(key, key) for key in getattr(program, "defines", {})]
    names = _unique_preserve_order(names)
    return _write_enum_results(ctx, [name for name in names if _enum_match_name(name, keyword, mode)])

def b_enumvar(ctx, args):
    keyword = _raw_identifier_arg(arg(args, 0, ""))
    mode = _enum_mode(ctx, "ENUMVAR")
    program = getattr(ctx, "program", None)
    names: list[str] = []
    names.extend(sorted(NUMERIC_ARRAYS | STRING_ARRAYS | CHARA_NUMERIC_ARRAYS | CHARA_STRING_ARRAYS))
    if program is not None:
        for key, decl in getattr(program, "var_decls", {}).items():
            if (
                getattr(decl, "module_scope", False)
                or getattr(decl, "global_scope", False)
                or getattr(decl, "savedata", False)
                or getattr(decl, "charadata", False)
            ):
                names.append(getattr(decl, "name", key))
        csv = getattr(program, "csv", None)
        if csv is not None:
            names.extend(getattr(csv, "variable_sizes", {}).keys())
            names.extend(getattr(csv, "constants", {}).keys())
    names = _unique_preserve_order(names)
    return _write_enum_results(ctx, [name for name in names if _enum_match_name(name, keyword, mode)])

def _dynamic_var_ref(ctx, value: Value):
    text = _raw_identifier_arg(value)
    try:
        return parse_lvalue(ctx, text)
    except Exception:
        return None

def b_getvar(ctx, args):
    ref = _dynamic_var_ref(ctx, arg(args, 0, ""))
    if ref is None:
        return 0
    return to_int(_ctx_get_var(ctx, ref.base, list(ref.indices)))

def b_getvars(ctx, args):
    ref = _dynamic_var_ref(ctx, arg(args, 0, ""))
    if ref is None:
        return ""
    return to_str(_ctx_get_var(ctx, ref.base, list(ref.indices)))

def b_setvar(ctx, args):
    ref = _dynamic_var_ref(ctx, arg(args, 0, ""))
    if ref is None:
        return 0
    key = norm_name(ref.base)
    program = getattr(ctx, "program", None)
    decl = getattr(program, "var_decls", {}).get(key) if program is not None else None
    if program is not None and key in getattr(program, "defines", {}):
        return 0
    if decl is not None and getattr(decl, "const", False):
        return 0
    memory = _ctx_memory(ctx)
    is_string = bool(getattr(memory, "is_string_base", lambda _base: False)(ref.base))
    value = arg(args, 1, "" if is_string else 0)
    _ctx_set_var(ctx, ref.base, list(ref.indices), value)
    return 1

def b_varsetex(ctx, args):
    helper = getattr(ctx, "_exec_varsetex_values", None)
    if callable(helper):
        var_name = arg(args, 0, "")
        value = arg(args, 1, 0)
        set_all_dim = arg(args, 2, 1)
        start_value = arg(args, 3, None) if len(args) >= 4 else None
        end_value = arg(args, 4, None) if len(args) >= 5 else None
        return helper(var_name, value, set_all_dim, start_value, end_value)
    ref = _dynamic_var_ref(ctx, arg(args, 0, ""))
    if ref is None:
        return 0
    _ctx_set_var(ctx, ref.base, list(ref.indices), arg(args, 1, 0))
    return 1

def b_arraymsortex(ctx, args):
    helper = getattr(ctx, "_exec_arraymsortex_values", None)
    if callable(helper):
        size = arg(args, 3, None) if len(args) >= 4 else None
        return helper(arg(args, 0, ""), arg(args, 1, ""), arg(args, 2, 1), size)
    return 0

def _runtime_is_active(ctx) -> bool:
    return bool(getattr(ctx, "is_active", getattr(ctx, "active", True)))

def _state_contains(container, key: int) -> bool:
    if isinstance(container, dict):
        return truth(container.get(key, container.get(str(key), 0)))
    try:
        return key in container or str(key) in container
    except Exception:
        return False

def _state_consume(container, key: int) -> bool:
    present = _state_contains(container, key)
    if not present:
        return False
    if isinstance(container, dict):
        if key in container:
            container[key] = 0
        if str(key) in container:
            container[str(key)] = 0
        return True
    try:
        if hasattr(container, "discard"):
            container.discard(key)
            container.discard(str(key))
        elif hasattr(container, "remove"):
            try:
                container.remove(key)
            except ValueError:
                container.remove(str(key))
    except Exception:
        pass
    return True

def b_getkey(ctx, args):
    if not _runtime_is_active(ctx):
        return 0
    code = to_int(arg(args, 0, 0))
    return 1 if _state_contains(getattr(ctx, "key_state", set()), code) else 0

def b_getkeytriggered(ctx, args):
    if not _runtime_is_active(ctx):
        return 0
    code = to_int(arg(args, 0, 0))
    return 1 if _state_consume(getattr(ctx, "key_triggered", set()), code) else 0

def b_mousex(ctx, args):
    return to_int(getattr(ctx, "mouse_x", 0))

def b_mousey(ctx, args):
    return to_int(getattr(ctx, "mouse_y", 0))

def b_mouseb(ctx, args):
    for name in ("mouse_button", "mouseb", "hover_button", "mouse_hover_button"):
        value = getattr(ctx, name, "")
        if value:
            return to_str(value)
    return ""

def b_isactive(ctx, args):
    return 1 if _runtime_is_active(ctx) else 0

def b_moneystr(ctx, args):
    value = arg(args, 0, _ctx_get_var(ctx, "MONEY", []))
    if len(args) >= 2 and args[1] != "":
        number = _format_era_number(to_int(value), to_str(args[1]))
        if number is None:
            number = to_str(value)
    else:
        number = to_str(value)
    db = getattr(getattr(ctx, "program", None), "csv", None)
    replacements = getattr(db, "replacements", {}) if db is not None else {}
    unit = replacements.get(norm_name("お金の単位"), "円")
    pos = replacements.get(norm_name("単位の位置"), "後")
    return f"{unit}{number}" if to_str(pos).strip().startswith("前") else f"{number}{unit}"

def b_chkdata(ctx, args):
    slot = to_int(arg(args, 0))
    if not hasattr(ctx, "_save_slot_info"):
        return 1
    exists, text = ctx._save_slot_info(slot)
    if hasattr(ctx, "memory"):
        ctx.memory.set_var("RESULTS", [], text if exists else "")
    return 0 if exists else 1
def b_getcolor(ctx, args): return getattr(ctx, "current_color", 0xC0C0C0)
def b_getdefcolor(ctx, args): return getattr(ctx, "default_color", 0xC0C0C0)
def b_getbgcolor(ctx, args): return getattr(ctx, "current_bgcolor", 0x000000)
def b_getdefbgcolor(ctx, args): return getattr(ctx, "default_bgcolor", 0x000000)

def _parse_rgb_config(text: str, default: int) -> int:
    s = text.strip()
    if not s:
        return default
    parts = [p.strip() for p in s.split(",")]
    if len(parts) >= 3:
        rgb = [max(0, min(255, parse_era_int(p))) for p in parts[:3]]
        return (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
    if s.startswith("#"):
        try:
            return int(s[1:], 16)
        except ValueError:
            return default
    try:
        return parse_era_int(s)
    except ValueError:
        return default

def _color_key(name: Any) -> str:
    return to_str(name).strip().upper()


def _flag_rgb_color(ctx, name: str) -> int:
    """Convert eraMegaten's decimal RRR/GGG/BBB FLAG color setting to RGB."""
    raw = to_int(ctx.get_var("FLAG", [name])) if hasattr(ctx, "get_var") else 0
    return ((raw // 1000 // 1000) * 256 * 256) + (((raw // 1000) % 1000) * 256) + (raw % 1000)


def color_by_known_name(name: Any, default: int = 0xC0C0C0) -> int:
    r"""Subset of .NET KnownColor names used by SETCOLORBYNAME in E:\mgt."""
    table = {
        "BLACK": 0x000000, "WHITE": 0xFFFFFF, "GRAY": 0x808080, "GREY": 0x808080,
        "DARKGRAY": 0xA9A9A9, "DARK-GRAY": 0xA9A9A9, "LIGHTGRAY": 0xD3D3D3, "LIGHT-GRAY": 0xD3D3D3,
        "RED": 0xFF0000, "GREEN": 0x008000, "BLUE": 0x0000FF, "YELLOW": 0xFFFF00,
        "AQUA": 0x00FFFF, "CYAN": 0x00FFFF, "BROWN": 0xA52A2A, "GOLD": 0xFFD700,
        "FORESTGREEN": 0x228B22, "FOREST-GREEN": 0x228B22, "GREENYELLOW": 0xADFF2F,
        "GREEN-YELLOW": 0xADFF2F, "PINK": 0xFFC0CB, "VIOLET": 0xEE82EE,
        "ORANGE": 0xFFA500, "CRIMSON": 0xDC143C, "LIME": 0x00FF00,
    }
    return table.get(_color_key(name), default)


def b_color(ctx, args):
    # eraMegaten defines ERB\関数\汎用組み込み関数\メッセージ\COLOR.ERB, but
    # this runtime exposes COLOR as a native fast path.  Keep the native table
    # equivalent to that ERB helper so expression calls do not silently fall
    # back to unrelated .NET colors or the default color.
    key = _color_key(arg(args, 0, ""))
    if key in {"DEFAULT", "DEF", "デフォルト"}:
        return getattr(ctx, "default_color", 0xC0C0C0)
    table = {
        "紅": 0xB815B8,
        "深紅": 0xEF5445,
        "RED": 0x990000, "赤": 0x990000,
        "P-RED": 0xC07070, "PASTEL-RED": 0xC07070, "パ赤": 0xC07070,
        "P-BLUE": 0x7070C0, "PASTEL-BLUE": 0x7070C0, "パ青": 0x7070C0,
        "P-GREEN": 0x70C070, "PASTEL-GREEN": 0x70C070, "パ緑": 0x70C070,
        "P-PURPLE": 0xC070C0, "PASTEL-PURPLE": 0xC070C0, "パ紫": 0xC070C0,
        "P-YELLOW": 0x505020, "PASTEL-YELLOW": 0x505020, "パ黄": 0x505020,
        "P-BGREEN": 0x205050, "P-BLUEGREEN": 0x205050, "PASTEL-BLUEGREEN": 0x205050, "パ青緑": 0x205050,
        "WHITE": 0xC0C0C0, "白": 0xC0C0C0,
        "BLACK": 0x000000, "黒": 0x000000,
        "暗水色": 0xB0CCEF,
        "AQUA": 0x66FFFF, "水色": 0x66FFFF,
        "DARK-GRAY": 0x404040, "暗灰色": 0x404040,
        "GRAY": 0x777777, "GREY": 0x777777, "灰色": 0x777777,
        "LIGHT-GRAY": 0x909090, "LIGHT-GREY": 0x909090, "明灰色": 0x909090,
        "DARK-PINK": 0x990099, "黒桃": 0x990099,
        "ショッキング粉": 0xEF0EEF,
        "粉": 0xEF857C,
        "PINK": 0xFF33FF, "桃": 0xFF33FF, "桃色": 0xFF33FF,
        "ハート粉": 0xFFC0CB, "明桃色": 0xFFC0CB,
        "BROWN": 0x90623C, "茶色": 0x90623C,
        "黄色": 0xEFD540, "YELLOW": 0xEFD540, "イエロー": 0xEFD540,
        "暗黄色": 0xC4C400, "DARK-YELLOW": 0xC4C400,
        "明黄色": 0xFFFF00, "LIGHT-YELLOW": 0xFFFF00, "レモン": 0xFFFF00, "レモン色": 0xFFFF00, "レモンイエロー": 0xFFFF00,
        "ライム": 0x5AFF19, "LIME": 0x5AFF19, "LIGHT-GREEN": 0x5AFF19, "明緑色": 0x5AFF19,
        "藍色": 0x165E83,
        "FRIENDLY": 0x33FFCC, "友好": 0x33FFCC, "友好色": 0x33FFCC, "友好水色": 0x33FFCC,
        "WARNING": 0xFF0033, "警告": 0xFF0033, "警告色": 0xFF0033, "警告赤": 0xFF0033,
        "FRIEND": 0x33FFCC, "味方水色": 0x33FFCC,
        "ENEMY": 0xFF0033, "敵赤": 0xFF0033,
        "バフ": 0x66FFFF,
        "デバフ": 0x404040,
    }
    if key in table:
        return table[key]
    if key in {"BATTLE", "BTL", "バトルカ拉"}:
        chara = to_int(arg(args, 1, 0))
        reverse = truth(arg(args, 2, 0))
        party_flag = to_int(ctx.get_var("CFLAG", [chara, "PTフラグ"])) if hasattr(ctx, "get_var") else 0
        friendly = (party_flag > 0 and not reverse) or (party_flag <= 0 and reverse)
        return 0x33FFCC if friendly else 0xFF0033
    if key in {"通常カ拉", "弱点カ拉", "耐性カ拉", "無効カ拉", "吸収カ拉", "反射カ拉"}:
        return _flag_rgb_color(ctx, to_str(arg(args, 0, "")))
    return 0

def b_color_fromrgb(ctx, args):
    r = max(0, min(255, to_int(arg(args, 0))))
    g = max(0, min(255, to_int(arg(args, 1))))
    b = max(0, min(255, to_int(arg(args, 2))))
    return (r << 16) | (g << 8) | b

def b_getcolor_9(ctx, args):
    """Pack/unpack eraMegaten's decimal RRR/GGG/BBB color helper."""
    r_or_packed = to_int(arg(args, 0, 0))
    g = to_int(arg(args, 1, 0))
    b = to_int(arg(args, 2, -1))
    if len(args) >= 3 and b >= 0:
        value = r_or_packed * 1000000 + g * 1000 + b
        _ctx_set_var(ctx, "RESULT", [], value)
        _ctx_set_var(ctx, "RESULTS", [], str(value))
        return value
    blue = r_or_packed % 1000
    tmp = r_or_packed // 1000
    green = tmp % 1000
    red = tmp // 1000
    _set_return_values(ctx, red, green, blue)
    return red


def _message_write(ctx, text: str = "", *, newline: bool = True) -> None:
    if hasattr(ctx, "_write"):
        ctx._write(text, newline=newline)


def _message_display_len(ctx, text: str) -> int:
    try:
        return _locale_strlen(ctx, text)
    except Exception:
        return len(text)


def _message_left(text: str, width: int) -> str:
    return text if len(text) >= width else text + (" " * (width - len(text)))


def _message_title(ctx, title: str) -> str:
    if title == "":
        title = ">MESSAGE"
    if title == ">MESSAGE":
        return title
    if _message_display_len(ctx, title) % 2:
        title += " "
    return f"┓＠{title}┏"


def _message_lines(args: list[Value], start: int, limit: int = 30) -> list[str]:
    lines: list[str] = []
    for value in args[start:start + limit]:
        text = to_str(value)
        if text == "":
            break
        lines.append(text)
    return lines


def _message_box(ctx, title: str, lines: list[str], *, width: int | None = None) -> int:
    if not lines:
        return 0
    title = _message_title(ctx, title)
    box_width = 72 if width is None else max(0, width)
    box_width = max(box_width, _message_display_len(ctx, title) + 4)
    for line in lines:
        box_width = max(box_width, _message_display_len(ctx, line))
    if box_width % 2:
        box_width += 1
    title_fill = max(0, (box_width - _message_display_len(ctx, title) + 1) // 2)
    _message_write(ctx, "┏" + title + ("━" * title_fill) + "┓")
    for line in lines:
        _message_write(ctx, "┃" + _message_left(line, box_width) + "┃")
    _message_write(ctx, "┗" + ("━" * max(0, box_width // 2)) + "┛")
    return 0


def b_message_b(ctx, args):
    count = to_int(arg(args, 0, 1))
    if count <= 0 or count > 5:
        return 0
    _message_write(ctx, "┏>MESSAGE━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓")
    for i in range(count):
        _message_write(ctx, "┃" + _message_left(to_str(arg(args, i + 1, "")), 72) + "┃")
    _message_write(ctx, "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛")
    return 0


def b_message_bl(ctx, args):
    return b_message_b(ctx, args)


def b_message_b2(ctx, args):
    title = to_str(arg(args, 0, ">MESSAGE"))
    lines = _message_lines(args, 2, 30)
    return _message_box(ctx, title, lines)


def b_message_p(ctx, args):
    title = to_str(arg(args, 2, ">MESSAGE"))
    lines = _message_lines(args, 4, 30)
    return _message_box(ctx, title, lines)


def b_message_p2(ctx, args):
    return b_message_p(ctx, args)


def b_message_comp_over(ctx, args):
    comp_capacity = to_int(_ctx_get_var(ctx, "FLAG", ["ＣＯＭＰ容量"]))
    nakama = to_int(call_builtin(ctx, "NUM_NAKAMA", []) or 0)
    software = 0
    if hasattr(ctx, "has_callable") and ctx.has_callable("ソフト容量"):
        try:
            software = to_int(ctx.call_expr_function("ソフト容量", []))
        except Exception:
            software = 0
    if comp_capacity < nakama + software:
        _message_write(ctx, "")
        _message_write(ctx, "＞一時的にCOMP容量が最大値をオーバーしました。")
        _message_write(ctx, "＞動作としては問題ありませんが、容量が通常通りに最大値未満になるまで")
        _message_write(ctx, "＞仲魔およびインストールソフトの追加はできませんので注意してください")
        _message_write(ctx, "")
    return 0


def b_set_aisyou_color(ctx, args):
    value = to_int(arg(args, 0, 0))
    if value == 999:
        flag_name = "反射カ拉"
    elif value > 100:
        flag_name = "弱点カ拉"
    elif value == 100:
        flag_name = "通常カ拉"
    elif value > 0:
        flag_name = "耐性カ拉"
    elif value == 0:
        flag_name = "無効カ拉"
    else:
        flag_name = "吸収カ拉"
    raw = to_int(_ctx_get_var(ctx, "FLAG", [flag_name]))
    red = to_int(b_getcolor_9(ctx, [raw]))
    green = to_int(_ctx_get_var(ctx, "RESULT", [1]))
    blue = to_int(_ctx_get_var(ctx, "RESULT", [2]))
    try:
        rgb = [max(0, min(255, x)) for x in (red, green, blue)]
        ctx.current_color = (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
    except Exception:
        pass
    _set_return_values(ctx, value)
    return value


def b_show_aisyou_color_list(ctx, args):
    index = to_int(arg(args, 0, 0))
    rows = [
        (200, "WEAK"),
        (100, "NORMAL"),
        (50, "RESIST"),
        (0, "BLOCK"),
        (-50, "ABSORB"),
        (999, "REFLECT"),
    ]
    if 0 <= index < len(rows):
        color_value, label = rows[index]
        b_set_aisyou_color(ctx, [color_value])
    else:
        label = ""
    _message_write(ctx, "■", newline=False)
    try:
        ctx.current_color = getattr(ctx, "default_color", 0xC0C0C0)
    except Exception:
        pass
    _message_write(ctx, _message_left(label, 7), newline=False)
    return 0


def b_tostr1000(ctx, args):
    return f"{to_int(arg(args, 0, 0)):,}"


def _mw_lines(text: str, delimiter: str) -> list[str]:
    if delimiter == "":
        return [text]
    return text.split(delimiter)


def _mw_even_width(ctx, width: int, title: str, lines: list[str]) -> int:
    width = max(0, width)
    if title:
        width = max(width, _message_display_len(ctx, title) + 2)
    for line in lines:
        width = max(width, _message_display_len(ctx, line))
    return width + (width % 2)


def _mw_align_text(ctx, text: str, width: int, mode: str) -> str:
    text_len = _message_display_len(ctx, text)
    pad = max(0, width - text_len)
    mode = mode.upper()
    if mode == "RIGHT":
        return (" " * pad) + text
    if mode == "CENTER":
        return (" " * (pad // 2)) + text + (" " * (pad - pad // 2))
    return text + (" " * pad)


def _mw_speaker(ctx, speaker: str) -> str:
    if speaker and _message_display_len(ctx, speaker) % 2:
        speaker += " "
    return f"┤{speaker}├" if speaker else ""


def _mw_render_box(ctx, speaker: str, lines: list[str], *, width: int = 72, row_count: int = -1, text_align: str = "LEFT", buttons: bool = True) -> None:
    speaker = _mw_speaker(ctx, speaker)
    width = _mw_even_width(ctx, max(0, width), speaker, lines)
    if row_count < 0:
        row_count = max(1, len(lines))
    else:
        row_count = max(row_count, len(lines), 0)
    top_dashes = max(0, (width - _message_display_len(ctx, speaker) + 1) // 2)
    _message_write(ctx, "┌" + speaker + ("─" * top_dashes) + "┐")
    for i in range(row_count):
        line = lines[i] if i < len(lines) else ""
        _message_write(ctx, "│" + _mw_align_text(ctx, line, width, text_align) + "│")
    if buttons:
        footer = "[+] LOG [-] AUTO [*] SKIP [/] CONFIG"
        dashes = max(0, width // 2 - _message_display_len(ctx, footer) // 2)
        _message_write(ctx, "└" + ("─" * dashes) + footer + "┘")
    else:
        _message_write(ctx, "└" + ("─" * max(0, width // 2)) + "┘")


def _mw_logs(ctx) -> list[tuple[str, str, str, int, int]]:
    logs = getattr(ctx, "message_window_logs", None)
    if not isinstance(logs, list):
        logs = []
        try:
            setattr(ctx, "message_window_logs", logs)
        except Exception:
            pass
    return logs


def b_nowalignment(ctx, args):
    if not args or to_str(arg(args, 0, "")) == "":
        value = to_str(getattr(ctx, "message_window_now_alignment", "")) or "LEFT"
        _set_return_values(ctx, value)
        return value
    value = to_str(arg(args, 0, "LEFT")).upper()
    try:
        ctx.message_window_now_alignment = value
    except Exception:
        pass
    _set_return_values(ctx, value)
    return value


def b_prevalignment(ctx, args):
    if not args or to_str(arg(args, 0, "")) == "":
        value = to_str(getattr(ctx, "message_window_prev_alignment", "")) or "LEFT"
        _set_return_values(ctx, value)
        return value
    value = to_str(arg(args, 0, "LEFT")).upper()
    try:
        ctx.message_window_prev_alignment = value
    except Exception:
        pass
    _set_return_values(ctx, value)
    return value


def b_set_alignment(ctx, args):
    requested = to_str(arg(args, 0, "LEFT")).upper()
    if requested == "PREV":
        requested = to_str(getattr(ctx, "message_window_prev_alignment", "")) or "LEFT"
    if requested not in {"LEFT", "CENTER", "RIGHT"}:
        _message_write(ctx, "指定ミスです！")
        _set_return_values(ctx, 0)
        return 0
    old = to_str(getattr(ctx, "message_window_now_alignment", "")) or to_str(getattr(ctx, "current_alignment", "")) or "LEFT"
    try:
        ctx.message_window_prev_alignment = old
        ctx.message_window_now_alignment = requested
        ctx.current_alignment = requested
    except Exception:
        pass
    _set_return_values(ctx, 1)
    return 1


def b_message_window_log(ctx, args):
    mode = to_int(arg(args, 5, 0))
    logs = _mw_logs(ctx)
    if mode > 0:
        for speaker, body, delimiter, rows, width in reversed(logs):
            _mw_render_box(ctx, speaker, _mw_lines(body, delimiter), width=width, row_count=rows, buttons=False)
        _wait_if_interactive(ctx)
        return 1
    if mode < 0:
        if logs:
            logs.pop(0)
        return 1
    speaker = to_str(arg(args, 0, ""))
    body = to_str(arg(args, 1, ""))
    delimiter = to_str(arg(args, 2, "/")) or "/"
    rows = to_int(arg(args, 3, max(1, len(_mw_lines(body, delimiter)))))
    width = to_int(arg(args, 4, 72))
    logs.insert(0, (speaker, body, delimiter, rows, width))
    del logs[200:]
    return 1


def b_message_window(ctx, args):
    base_line = _ctx_line_count(ctx)
    speaker = to_str(arg(args, 0, ""))
    body = to_str(arg(args, 1, ""))
    delimiter = to_str(arg(args, 2, "/")) or "/"
    options = to_str(arg(args, 3, "ログを残さない/ボタンを利用する/再利用しない"))
    position = to_str(arg(args, 4, "")) or "LEFT"
    width = to_int(arg(args, 5, 72))
    rows = to_int(arg(args, 6, -1))
    text_align = to_str(arg(args, 10, "LEFT")) or "LEFT"
    if "再利用する" in options:
        prev_body = to_str(getattr(ctx, "message_window_previous_body", ""))
        prev_speaker = to_str(getattr(ctx, "message_window_previous_speaker", ""))
        prev_width = to_int(getattr(ctx, "message_window_previous_width", 0))
        prev_rows = to_int(getattr(ctx, "message_window_previous_rows", -1))
        prev_position = to_str(getattr(ctx, "message_window_previous_position", ""))
        if speaker == "":
            speaker = prev_speaker
        if rows == 0:
            rows = prev_rows
        if position == "":
            position = prev_position or "LEFT"
        if prev_body:
            body = prev_body + (delimiter + body if body else "")
        width = max(width, prev_width)
        b_message_window_log(ctx, ["", "", "", 0, 0, -1])
    lines = _mw_lines(body, delimiter)
    width = _mw_even_width(ctx, width, _mw_speaker(ctx, speaker), lines)
    if rows < 0:
        rows = max(1, len(lines))
    else:
        rows = max(rows, len(lines))
    b_set_alignment(ctx, [position])
    _mw_render_box(ctx, speaker, lines, width=width, row_count=rows, text_align=text_align, buttons=("ボタンを利用しない" not in options))
    b_set_alignment(ctx, ["PREV"])
    b_message_window_log(ctx, [speaker, body, delimiter, rows, width, 0])
    try:
        ctx.message_window_previous_body = body
        ctx.message_window_previous_speaker = speaker
        ctx.message_window_previous_width = width
        ctx.message_window_previous_rows = rows
        ctx.message_window_previous_position = position
    except Exception:
        pass
    if "ログを残す" not in options and not bool(getattr(ctx, "_defer_message_window_clear", False)):
        _ctx_clear_lines(ctx, max(0, _ctx_line_count(ctx) - base_line))
    _set_return_values(ctx, 1)
    return 1


def b_message_window_d(ctx, args):
    options = to_str(arg(args, 3, "デフォルト"))
    if options == "デフォルト":
        options = "ログを残さない/ボタンを利用する/再利用しない"
    return b_message_window(
        ctx,
        [
            arg(args, 0, ""),
            arg(args, 1, ""),
            arg(args, 2, "/"),
            options,
            arg(args, 4, "CENTER"),
            arg(args, 5, 72),
            max(to_int(arg(args, 6, 4)), to_str(arg(args, 1, "")).count(to_str(arg(args, 2, "/")) or "/") + 1),
            arg(args, 7, "TYPE"),
            arg(args, 8, 10),
            arg(args, 9, -1),
            arg(args, 10, "LEFT"),
        ],
    )


def b_message_window_config(ctx, args):
    while True:
        _write_choice_line(ctx, "[0] メッセージ速度")
        _write_choice_line(ctx, "[1] オート時ウェイト")
        _write_choice_line(ctx, "[2] 右クリック時ウェイト")
        _write_choice_line(ctx, "[3] メッセージアニメ利用")
        _write_choice_line(ctx, "[9] 設定終了")
        choice = to_int(_read_string_input(ctx, "9"))
        if choice == 9:
            break
        if choice == 0:
            _ctx_set_var(ctx, "GLOBAL", ["メッセージ速度"], max(0, min(9, to_int(_read_string_input(ctx, "1")))))
        elif choice == 1:
            value = to_int(_read_string_input(ctx, "0"))
            _ctx_set_var(ctx, "GLOBAL", ["オート時ウェイト"], 0 if value < 100 else value)
        elif choice == 2:
            value = to_int(_read_string_input(ctx, "0"))
            _ctx_set_var(ctx, "GLOBAL", ["右クリック時ウェイト"], 0 if value < 100 else value)
        elif choice == 3:
            _ctx_set_var(ctx, "GLOBAL", ["メッセージアニメ利用"], b_input_yn(ctx, ["利用しない", "利用する"]))
        elif not getattr(ctx, "inputs", None):
            break
        else:
            continue
        try:
            if hasattr(ctx, "_exec_persistence"):
                ctx._exec_persistence("SAVEGLOBAL", "")
        except Exception:
            pass
    _set_return_values(ctx, 1)
    return 1


def _ctx_line_count(ctx) -> int:
    if hasattr(ctx, "_line_count"):
        return to_int(ctx._line_count())
    return len(getattr(ctx, "output", []) or [])


def _ctx_clear_lines(ctx, count: int) -> None:
    if hasattr(ctx, "_clear_lines"):
        ctx._clear_lines(max(0, count))


def _call_script_procedure(ctx, name: str, args: list[Value] | None = None) -> None:
    args = args or []
    try:
        if _ctx_can_call_script(ctx, name):
            ctx._call_sync(name, args)
    except Exception:
        return


def _call_script_procedure_live(ctx, name: str, args: list[Value] | None = None, *, max_steps: int = 20000) -> bool:
    args = args or []
    try:
        if not _ctx_can_call_script(ctx, name):
            return False
        depth = len(ctx.stack)
        if not ctx._push_call_sequence(name, args, try_only=True):
            return False
        ctx._run_loop(max_steps=max_steps, stop_depth=depth)
        return True
    except Exception:
        return False


def _show_picture_floor_name(ctx) -> None:
    name = f"FLOORNAME_{to_int(_ctx_get_var(ctx, 'FLAG', ['現ダンジョン']))}"
    if _ctx_can_call_script(ctx, name):
        _call_script_procedure(ctx, name, [])


def _set_current_alignment(ctx, value: str) -> None:
    try:
        ctx.current_alignment = value
    except Exception:
        pass


def b_show_picture(ctx, args):
    values = [
        to_str(arg(args, 0, "")),
        to_str(arg(args, 1, "")),
        to_str(arg(args, 2, "")),
        to_str(arg(args, 3, "/")) or "/",
        to_str(arg(args, 4, "LEFT")) or "LEFT",
    ]
    if values[0] in {"再利用", "再利用する"}:
        values = list(getattr(ctx, "show_picture_previous_args", values))
    else:
        try:
            ctx.show_picture_previous_args = list(values)
        except Exception:
            pass
    mode, title, body, delimiter, alignment = values
    mode_key = mode.upper()
    if mode_key in {"D", "DUNGEON"} or mode == "ダンジョン":
        try:
            ctx.current_redraw = 0
        except Exception:
            pass
        _ctx_clear_lines(ctx, to_int(_ctx_get_var(ctx, "FLAG", ["指令表示行数"])) - _ctx_line_count(ctx))
        _call_script_procedure(ctx, "SHOW_FLOOR", [])
        _call_script_procedure(ctx, "SHOW_NOW_FORMATION_P", [0, 2, "", 2])
        _ctx_set_var(ctx, "FLAG", ["指令表示行数"], _ctx_line_count(ctx))
        try:
            ctx.current_redraw = 1
        except Exception:
            pass
        _set_return_values(ctx, 1)
        return 1
    if mode_key == "NONFLOORD" or mode == "blank":
        if title:
            _message_write(ctx, title)
        else:
            _show_picture_floor_name(ctx)
        if mode_key == "NONFLOORD":
            money = to_int(_ctx_get_var(ctx, "MONEY", []))
            nakama = to_int(call_builtin(ctx, "NUM_NAKAMA", []) or 0)
            software = to_int(_call_script_value(ctx, "ソフト容量", [], 0))
            capacity = to_int(_ctx_get_var(ctx, "FLAG", ["ＣＯＭＰ容量"]))
            _message_write(ctx, f"  ￥{money:8d}    ＣＯＭＰ容量： {nakama + software}/{capacity}使用中")
            mag = to_int(_ctx_get_var(ctx, "BASE", [_master(ctx), "ＭＡＧ"]))
            moon = to_int(_ctx_get_var(ctx, "FLAG", ["月齢"]))
            vector = to_int(_ctx_get_var(ctx, "FLAG", ["月齢ベクトル"]))
            _print_no_newline(ctx, f"MAG:{mag:8d}    ")
            if moon not in {0, 8}:
                _message_write(ctx, f"{moon}/8 {'ＹＯＵＮＧ' if vector == 0 else 'ＯＬＤ'} ＭＯＯＮ")
            else:
                _message_write(ctx, "ＦＵＬＬ ＭＯＯＮ" if moon == 8 else "ＮＥＷ ＭＯＯＮ")
        if not body:
            for _ in range(18):
                _message_write(ctx, "")
        else:
            align = alignment.upper()
            _set_current_alignment(ctx, align if align in {"CENTER", "RIGHT"} else "LEFT")
            for line in body.split(delimiter):
                _message_write(ctx, line)
            _set_current_alignment(ctx, "LEFT")
        if mode_key == "NONFLOORD":
            _call_script_procedure(ctx, "SHOW_NOW_FORMATION_P", [0, 2, "", 2])
        _ctx_set_var(ctx, "FLAG", ["指令表示行数"], _ctx_line_count(ctx))
        _set_return_values(ctx, 1)
        return 1
    if mode_key == "EMPTY":
        try:
            ctx.current_redraw = 0
        except Exception:
            pass
        for _ in range(50):
            _message_write(ctx, "")
        try:
            ctx.current_redraw = 0
        except Exception:
            pass
        _ctx_clear_lines(ctx, 50)
        _set_return_values(ctx, 1)
        return 1
    _message_write(ctx, "引数の指定が間違っています！")
    _wait_if_interactive(ctx)
    _set_return_values(ctx, 0)
    return 0


def _move_forcemove_step(ctx, direction: str) -> None:
    if direction == "U":
        _ctx_set_var(ctx, "FLAG", ["現Y"], to_int(_ctx_get_var(ctx, "FLAG", ["現Y"])) - 1)
    elif direction == "D":
        _ctx_set_var(ctx, "FLAG", ["現Y"], to_int(_ctx_get_var(ctx, "FLAG", ["現Y"])) + 1)
    elif direction == "L":
        _ctx_set_var(ctx, "FLAG", ["現X"], to_int(_ctx_get_var(ctx, "FLAG", ["現X"])) - 1)
    elif direction == "R":
        _ctx_set_var(ctx, "FLAG", ["現X"], to_int(_ctx_get_var(ctx, "FLAG", ["現X"])) + 1)


def _show_forcemove_frame(ctx, mode: str, line_base: int, speaker: str, body: str, delimiter: str, options: str, position: str, width: int, rows: int) -> None:
    _ctx_clear_lines(ctx, _ctx_line_count(ctx) - line_base)
    if mode.upper() in {"D", "DUNGEON"} or mode == "ダンジョン":
        _call_script_procedure(ctx, "AUTOMAP", [])
        b_show_picture(ctx, ["D", "", "", "/", "LEFT"])
        if body != "EMPTY":
            b_message_window(ctx, [speaker, body, delimiter, options, position, width, rows])
            b_message_window_log(ctx, ["", "", "", 0, 0, -1])
        else:
            _call_script_procedure(ctx, "SHOW_DUNGEON_COMMAND", [])
        try:
            ctx.current_redraw = 0
        except Exception:
            pass


def b_show_forcemove(ctx, args):
    moves = to_str(arg(args, 0, ""))
    mode = to_str(arg(args, 1, ""))
    speaker = to_str(arg(args, 3, ""))
    body = to_str(arg(args, 4, "EMPTY"))
    delimiter = to_str(arg(args, 5, "/")) or "/"
    options = to_str(arg(args, 6, "ログを残す/ボタンを利用する/再利用しない/NOWAIT/NOANIME"))
    position = to_str(arg(args, 7, "CENTER")) or "CENTER"
    width = to_int(arg(args, 8, 72))
    rows = to_int(arg(args, 9, 4))
    line_base = _ctx_line_count(ctx)
    shown = 0
    last_dir = "NONE"
    try:
        ctx.current_redraw = 0
    except Exception:
        pass
    _ctx_set_var(ctx, "FLAG", [233], to_int(_ctx_get_var(ctx, "FLAG", [233])) | 1)
    _show_forcemove_frame(ctx, mode, line_base, speaker, body, delimiter, options, position, width, rows)
    shown += 1
    i = 0
    while i < len(moves):
        ch = moves[i]
        direction = ""
        repeat = 0
        if ch in "UDLR":
            direction = ch
            last_dir = ch
            repeat = 1
            i += 1
        elif ch == "<":
            end = moves.find(">", i + 1)
            if end >= 0:
                direction = last_dir
                repeat = max(0, to_int(moves[i + 1 : end]) - 1)
                i = end + 1
            else:
                i += 1
        else:
            i += 1
        for _ in range(repeat):
            _move_forcemove_step(ctx, direction)
            _show_forcemove_frame(ctx, mode, line_base, speaker, body, delimiter, options, position, width, rows)
            shown += 1
    if body != "EMPTY":
        b_message_window_log(ctx, [speaker, body, delimiter, rows, width])
    try:
        ctx.current_redraw = 1
    except Exception:
        pass
    _set_return_values(ctx, shown)
    return shown


def _ai_position_chara(ctx, pos_no: int) -> int:
    name = f"ポジション{pos_no}"
    if _has_csv_name(ctx, "FLAG", name):
        return to_int(_ctx_get_var(ctx, "FLAG", [name]))
    return to_int(call_builtin(ctx, "POS", [pos_no]) or -1)


def _ai_state_name(ctx, chara: int) -> str:
    state = to_int(_ctx_get_var(ctx, "CFLAG", [chara, "ステート"]))
    name = call_builtin(ctx, "GET_STATE", [state])
    return to_str(name) if name is not None else to_str(state)


def b_attack_min_hp(ctx, args):
    attacker = to_int(arg(args, 0, 0))
    enemy_side = truth(arg(args, 1, 0))
    start, end = (7, 17) if enemy_side else (1, 7)
    best_hp = None
    best_pos = -1
    for pos_no in range(start, end):
        chara = _ai_position_chara(ctx, pos_no)
        if chara < 0:
            continue
        if _ai_state_name(ctx, chara) == "瀕死":
            continue
        hp = to_int(_ctx_get_var(ctx, "BASE", [chara, "ＨＰ"]))
        if best_hp is None or hp <= best_hp:
            best_hp = hp
            best_pos = pos_no
    _ctx_set_var(ctx, "CFLAG", [attacker, "ターゲット"], best_pos)
    _set_return_values(ctx, best_pos)
    return best_pos


def b_check_weakness(ctx, args):
    value = to_int(arg(args, 0, 0))
    if value == 999:
        result = -4
    elif value < 0:
        result = -3
    elif value == 0:
        result = -2
    elif value < 100:
        result = -1
    elif value == 100:
        result = 0
    elif value < 999:
        result = 1
    elif value == 1000:
        result = 100
    else:
        result = 0
    _set_return_values(ctx, result)
    return result


def _weakness_records(text: str) -> list[str]:
    return [part for part in to_str(text).split("/") if part != ""]


def _weakness_value_for(ctx, chara: int, affinity: str) -> int:
    index: int | str
    if _has_csv_name(ctx, "BASE", affinity):
        index = _csv_index(ctx, "BASE", affinity)
    else:
        index = affinity
    return to_int(_ctx_get_var(ctx, "MAXBASE", [chara, index]))


def _weakness_positions(target_pos: int) -> range:
    if target_pos < 7:
        return range(target_pos, target_pos + 1)
    if target_pos == 20:
        return range(1, 4)
    if target_pos == 21:
        return range(4, 7)
    return range(1, 7)


def b_memorize_weakness(ctx, args):
    learner = to_int(arg(args, 0, 0))
    target_pos = to_int(arg(args, 1, 0))
    affinity = to_str(arg(args, 2, ""))
    no_update = truth(arg(args, 3, 0))
    max_value = 1000
    max_pos = -1
    min_value = 1000
    min_pos = -1
    current_records = _weakness_records(to_str(_ctx_get_var(ctx, "CSTR", [learner, 50])))
    records = list(current_records)
    for pos_no in _weakness_positions(target_pos):
        if pos_no <= 0:
            continue
        target = _ai_position_chara(ctx, pos_no)
        if target < 0:
            continue
        value = _weakness_value_for(ctx, target, affinity)
        prefix = f"{target}_{affinity}_"
        full = f"{prefix}{value}"
        found_exact = False
        new_records: list[str] = []
        for rec in records:
            if rec.startswith(prefix):
                if rec == full:
                    found_exact = True
                    known = value
                    if max_value == 1000:
                        max_value = min_value = known
                        max_pos = min_pos = pos_no
                    else:
                        if max_value < known:
                            max_value = known
                            max_pos = pos_no
                        if min_value > known:
                            min_value = known
                            min_pos = pos_no
                    new_records.append(rec)
                elif no_update:
                    new_records.append(rec)
                # changed affinity record is forgotten on update-enabled calls
            else:
                new_records.append(rec)
        if not no_update and not found_exact:
            if len(new_records) >= 99:
                new_records = []
            new_records.append(full)
        records = new_records
        if not no_update:
            _ctx_set_var(ctx, "CSTR", [learner, 50], "/".join(records) if records else "")
    _ctx_set_var(ctx, "RESULT", [1], max_value)
    _ctx_set_var(ctx, "RESULT", [2], max_pos)
    _ctx_set_var(ctx, "RESULT", [3], min_value)
    _ctx_set_var(ctx, "RESULT", [4], min_pos)
    _ctx_set_var(ctx, "RESULTS", [1], str(max_value))
    _ctx_set_var(ctx, "RESULTS", [2], str(max_pos))
    _ctx_set_var(ctx, "RESULTS", [3], str(min_value))
    _ctx_set_var(ctx, "RESULTS", [4], str(min_pos))
    _ctx_set_var(ctx, "RESULT", [], max_value)
    _ctx_set_var(ctx, "RESULTS", [], str(max_value))
    return max_value


def _message_get_chara(ctx, name: str, default: int = -1) -> int:
    try:
        return to_int(_ctx_get_var(ctx, name, []))
    except Exception:
        return default


def _message_chara_text(ctx, base: str, chara: int, default: str = "") -> str:
    try:
        return to_str(_ctx_get_var(ctx, base, [chara]))
    except Exception:
        return default


def _safe_cstr(ctx, chara: int, segment: str | int, default: str = "") -> str:
    try:
        return to_str(_ctx_get_var(ctx, "CSTR", [chara, segment]))
    except Exception:
        return default


def _print_no_newline(ctx, text: str, *, harvest_buttons: bool = True) -> None:
    if hasattr(ctx, "_write"):
        try:
            ctx._write(text, newline=False, harvest_buttons=harvest_buttons)
        except TypeError:
            ctx._write(text, newline=False)


def _wait_if_interactive(ctx) -> None:
    if getattr(ctx, "interactive", False) and hasattr(ctx, "_input"):
        ctx._input("")


def _call_noarg_if_possible(ctx, name: str) -> None:
    if not name:
        return
    if hasattr(ctx, "has_callable") and ctx.has_callable(name) and hasattr(ctx, "_call_sync"):
        ctx._call_sync(name, [], max_steps=20000)
    elif hasattr(ctx, "warn"):
        ctx.warn(f"function not found: {name}")


def _shop_train_const(ctx) -> int:
    try:
        return to_int(b_const(ctx, ["ショップ:調教"]))
    except Exception:
        return 0


def _print_str_piece(ctx, token: str, chara: int, *, collect: bool) -> str | None:
    target = _message_get_chara(ctx, "TARGET", 0)
    player = _message_get_chara(ctx, "PLAYER", 0)
    master = _message_get_chara(ctx, "MASTER", 0)
    assi = _message_get_chara(ctx, "ASSI", -1)
    assiplay = to_int(_ctx_get_var(ctx, "ASSIPLAY", []))
    shop_cmd = to_int(_ctx_get_var(ctx, "FLAG", ["商店指令"]))
    in_train = chara == target and shop_cmd == _shop_train_const(ctx) and assiplay > 0

    if token in {"CALLNAME:TARGET", "調教対象", "被調教者"}:
        return _message_chara_text(ctx, "CALLNAME", target)
    if token in {"CALLNAME:PLAYER", "調教者"}:
        return _message_chara_text(ctx, "CALLNAME", player)
    if token in {"CALLNAME:ASSI", "助手呼", "助手"}:
        return _message_chara_text(ctx, "CALLNAME", assi) if assi > -1 else ""
    if token in {"CALLNAME:MASTER", "主人", "主人公"}:
        return _message_chara_text(ctx, "CALLNAME", master)
    if token == "一人称":
        return _safe_cstr(ctx, chara, "一人称")
    if token == "二人称":
        if in_train and assi > -1:
            return _message_chara_text(ctx, "CALLNAME", assi)
        return _safe_cstr(ctx, chara, "二人称")
    if token == "第三者":
        if in_train:
            return _safe_cstr(ctx, chara, "二人称")
        return _message_chara_text(ctx, "CALLNAME", assi) if assi > -1 else ""
    if token == "主人呼":
        return _safe_cstr(ctx, chara, "二人称")
    if token == "呼び名":
        return _message_chara_text(ctx, "CALLNAME", chara)
    if token == "名前":
        return _message_chara_text(ctx, "NAME", chara)
    if token in {"H", "PH"}:
        return "\u2665"
    if token in {"WH", "WPH"}:
        return "\u2661"
    if token in {"BH", "BPH"}:
        return "\u2764"
    return None


def b_anataname(ctx, args):
    master = _message_get_chara(ctx, "MASTER", 0)
    callname = _message_chara_text(ctx, "CALLNAME", master)
    value = to_str(arg(args, 0, "")) if callname == "あなた" else callname + to_str(arg(args, 1, ""))
    _set_return_values(ctx, value)
    return value


def b_barcolorset(ctx, args):
    table = {
        "赤": (0xC07070, 0x502020),
        "青": (0x7070C0, 0x202050),
        "緑": (0x70C070, 0x205020),
        "紫": (0xC070C0, 0x502050),
        "黄": (0xC0C070, 0x505020),
        "青緑": (0x70C0C0, 0x205050),
    }
    fg, bg = table.get(to_str(arg(args, 0, "")), (0xC0C0C0, 0x202020))
    _set_return_values(ctx, fg, bg)
    return fg


def b_print_color(ctx, args):
    old = getattr(ctx, "current_color", 0xC0C0C0)
    try:
        ctx.current_color = to_int(arg(args, 1, old))
    except Exception:
        pass
    _print_no_newline(ctx, to_str(arg(args, 0, "")))
    suffix = to_str(arg(args, 2, "")).upper()
    if suffix in {"W", "WAIT", "L"}:
        _message_write(ctx, "")
        if suffix in {"W", "WAIT"}:
            _wait_if_interactive(ctx)
    try:
        ctx.current_color = old
    except Exception:
        pass
    return 0


def b_btl_color_table(ctx, args):
    value = "白/暗灰色/灰色/警告/赤/深紅/パ赤/黒桃/パ紫/藍色/パ青/水色/暗水色/パ緑/ライム/黄色/暗黄色/茶色"
    _set_return_values(ctx, value)
    return value


def b_btl_color_table_num(ctx, args):
    value = to_str(b_btl_color_table(ctx, [])).count("/")
    _set_return_values(ctx, value)
    return value


def b_tostr_html(ctx, args):
    formatted = _format_era_number(to_int(arg(args, 0, 0)), "X8")
    value = "#" + (formatted if formatted is not None else f"{to_int(arg(args, 0, 0)):08X}")
    _set_return_values(ctx, value)
    return value


def b_colordrawline(ctx, args):
    text = to_str(arg(args, 0, "─"))
    color = to_int(arg(args, 1, -1))
    font = to_str(arg(args, 2, ""))
    old_color = getattr(ctx, "current_color", 0xC0C0C0)
    old_font = getattr(ctx, "current_font", "")
    try:
        if color != -1:
            ctx.current_color = color
        if font and to_int(call_builtin(ctx, "CHKFONT", [font]) or 0):
            ctx.current_font = font
        fill = ctx.render_form(text).strip() if hasattr(ctx, "render_form") else text.strip()
        _message_write(ctx, ((fill or "─") * 72)[:72])
    finally:
        try:
            ctx.current_color = old_color
            if old_font:
                ctx.current_font = old_font
        except Exception:
            pass
    return 0


def b_printform_lf(ctx, args):
    text = to_str(arg(args, 0, ""))
    rendered = ctx.render_form(text) if hasattr(ctx, "render_form") else text
    _print_no_newline(ctx, rendered)
    for _ in range(rendered.count("\r") + 1):
        _message_write(ctx, "")
    if to_str(arg(args, 1, "")).upper() in {"W", "WAIT"}:
        _wait_if_interactive(ctx)
    return 0


def b_print_colorbar(ctx, args):
    value = to_int(arg(args, 0, 0))
    maximum = to_int(arg(args, 1, 1))
    width = max(0, to_int(arg(args, 2, 0)))
    fill_char = to_str(arg(args, 3, "*"))
    empty_char = to_str(arg(args, 4, "."))
    old = getattr(ctx, "current_color", 0xC0C0C0)
    filled = width if maximum <= 0 and value > 0 else (value * width // maximum if maximum > 0 else 0)
    filled = max(0, min(width, filled))
    _print_no_newline(ctx, fill_char * filled + empty_char * (width - filled))
    try:
        ctx.current_color = old
    except Exception:
        pass
    return 0


def b_print_eight_bar(ctx, args):
    value = to_int(arg(args, 0, 0))
    width = max(0, to_int(arg(args, 1, 32)))
    old = getattr(ctx, "current_color", 0xC0C0C0)
    if width <= 0:
        return 0
    total = width * 8
    pos = value % total if total > 0 else 0
    full = max(pos // 8, 0)
    rem = pos % 8
    chars: list[str] = []
    for i in range(width):
        if i < full:
            chars.append("\u2588")
        elif i == full:
            chars.append(chr(0x2588 + 8 - rem) if rem else " ")
        else:
            chars.append(" ")
    _print_no_newline(ctx, "".join(chars))
    try:
        ctx.current_color = old
    except Exception:
        pass
    return 0


def b_print_str(ctx, args):
    text = to_str(arg(args, 0, ""))
    chara = _message_get_chara(ctx, "TARGET", 0) if to_int(arg(args, 1, -1)) == -1 else to_int(arg(args, 1, 0))
    old_color = getattr(ctx, "current_color", 0xC0C0C0)
    numeric_input_seen = False
    numeric_input = 0
    string_input_seen = False
    string_input = ""
    tokens = text.split("_")
    i = 0
    while i < min(100, len(tokens)):
        token = tokens[i]
        piece = _print_str_piece(ctx, token, chara, collect=False)
        if piece is not None:
            _print_no_newline(ctx, piece)
            i += 1
            continue
        if token == "BUTTON":
            i += 1
            button = tokens[i] if i < len(tokens) else ""
            if hasattr(ctx, "pending_buttons"):
                ctx.pending_buttons.append(button)
            _print_no_newline(ctx, button)
        elif token == "NOBUTTON":
            i += 1
            _print_no_newline(ctx, tokens[i] if i < len(tokens) else "", harvest_buttons=False)
        elif token == "CALL":
            i += 1
            _call_noarg_if_possible(ctx, tokens[i] if i < len(tokens) else "")
        elif token == "INPUT":
            value = ctx._input("") if hasattr(ctx, "_input") else ""
            numeric_input = to_int(value)
            numeric_input_seen = True
            _ctx_set_var(ctx, "RESULT", [], numeric_input)
            _ctx_set_var(ctx, "RESULTS", [], to_str(value))
        elif token == "INPUTS":
            value = ctx._input("") if hasattr(ctx, "_input") else ""
            string_input = to_str(value)
            string_input_seen = True
            _ctx_set_var(ctx, "RESULTS", [], string_input)
        elif token == "-":
            try:
                ctx.current_font_style = 4
            except Exception:
                pass
        elif token in {"L", "改行"}:
            _message_write(ctx, "")
        elif token == "W":
            _message_write(ctx, "")
            _wait_if_interactive(ctx)
        elif token in {"WAIT", "FORCEWAIT"}:
            _wait_if_interactive(ctx)
        elif token == "／／／":
            _print_no_newline(ctx, "///")
            try:
                ctx.current_color = old_color
            except Exception:
                pass
        elif token.startswith("CALL ") and len(token) > 5:
            _call_noarg_if_possible(ctx, token[5:])
        elif to_int(call_builtin(ctx, "COLOR", [token]) or 0) > 0:
            try:
                ctx.current_color = to_int(call_builtin(ctx, "COLOR", [token]) or old_color)
            except Exception:
                pass
        else:
            _print_no_newline(ctx, token)
            try:
                ctx.current_color = old_color
                ctx.current_font_style = 0
            except Exception:
                pass
        i += 1
    if string_input_seen:
        _ctx_set_var(ctx, "RESULTS", [], string_input)
    if numeric_input_seen:
        _set_return_values(ctx, numeric_input)
        return numeric_input
    if string_input_seen:
        _ctx_set_var(ctx, "RESULT", [], 0)
        return 0
    _set_return_values(ctx, 0)
    return 0


def b_print_str_f(ctx, args):
    text = to_str(arg(args, 0, ""))
    chara = _message_get_chara(ctx, "TARGET", 0) if to_int(arg(args, 1, -1)) == -1 else to_int(arg(args, 1, 0))
    out: list[str] = []
    for token in text.split("_")[:100]:
        piece = _print_str_piece(ctx, token, chara, collect=True)
        out.append(piece if piece is not None else token)
    value = "".join(out)
    _set_return_values(ctx, value)
    return value


def b_print_strl(ctx, args):
    value = b_print_str(ctx, args)
    _message_write(ctx, "")
    return value


def b_print_strw(ctx, args):
    value = b_print_str(ctx, args)
    _wait_if_interactive(ctx)
    return value


def b_print_str_input(ctx, args):
    b_print_str(ctx, args)
    value = ctx._input("") if hasattr(ctx, "_input") else ""
    result = to_int(value)
    _ctx_set_var(ctx, "RESULT", [], result)
    _ctx_set_var(ctx, "RESULTS", [], to_str(value))
    return result


def b_print_str_inputs(ctx, args):
    b_print_str(ctx, args)
    result_after_print = to_int(_ctx_get_var(ctx, "RESULT", []))
    value = ctx._input("") if hasattr(ctx, "_input") else ""
    _ctx_set_var(ctx, "RESULTS", [], to_str(value))
    return result_after_print


def b_heartmark(ctx, args):
    _print_no_newline(ctx, "\u2665")
    return 0


def b_white_heartmark(ctx, args):
    _print_no_newline(ctx, "\u2661")
    return 0


def b_big_heartmark(ctx, args):
    _print_no_newline(ctx, "\u2764")
    return 0


def _heart_previous_result(ctx) -> int:
    try:
        return to_int(_ctx_get_var(ctx, "RESULT", []))
    except Exception:
        return 0


def _emit_heart_series(ctx, args: list[Value], *, filled: bool, wait_line: bool, default_color: bool = False, invert_hide: bool = False) -> int:
    old_result = _heart_previous_result(ctx)
    old_font = getattr(ctx, "current_font", "")
    old_color = getattr(ctx, "current_color", 0xC0C0C0)
    count = max(0, to_int(arg(args, 0, 1)))
    font = to_str(arg(args, 1, ""))
    if not font:
        font = "Times New Roman" if not filled else "Times New Roman"
    try:
        if default_color:
            ctx.current_color = getattr(ctx, "default_color", 0xC0C0C0)
        if font and to_int(call_builtin(ctx, "CHKFONT", [font]) or 0):
            ctx.current_font = font
        current_font = to_str(getattr(ctx, "current_font", ""))
        heart = "\u00A9" if filled and current_font == "Symbol" else ("\u2665" if filled else "\u2661")
        _print_no_newline(ctx, heart * count)
        if wait_line:
            suffix = to_str(arg(args, 2, ""))
            hide = truth(arg(args, 3, 0))
            if invert_hide:
                hide = not hide
            if not hide and suffix == "":
                suffix = "」"
            _message_write(ctx, suffix)
            _wait_if_interactive(ctx)
    finally:
        try:
            if old_font:
                ctx.current_font = old_font
            if default_color:
                ctx.current_color = old_color
        except Exception:
            pass
    _set_return_values(ctx, old_result)
    return old_result


def b_heart_cmd(ctx, args):
    return _emit_heart_series(ctx, args, filled=False, wait_line=False)


def b_heartb_cmd(ctx, args):
    return _emit_heart_series(ctx, args, filled=True, wait_line=False)


def b_heartw_cmd(ctx, args):
    return _emit_heart_series(ctx, args, filled=False, wait_line=True)


def b_heartbw_cmd(ctx, args):
    return _emit_heart_series(ctx, args, filled=True, wait_line=True)


def b_heartd_cmd(ctx, args):
    return _emit_heart_series(ctx, args, filled=False, wait_line=False, default_color=True)


def b_heartdb_cmd(ctx, args):
    return _emit_heart_series(ctx, args, filled=True, wait_line=False, default_color=True)


def b_heartdw_cmd(ctx, args):
    return _emit_heart_series(ctx, args, filled=False, wait_line=True, default_color=True, invert_hide=True)


def b_heartdbw_cmd(ctx, args):
    return _emit_heart_series(ctx, args, filled=True, wait_line=True, default_color=True, invert_hide=True)


def b_toalignment(ctx, args):
    text = to_str(arg(args, 0, ""))
    width = max(0, to_int(arg(args, 1, 0)))
    mode = to_str(arg(args, 2, "")).upper()
    pad = max(width - _message_display_len(ctx, text), 0)
    if mode == "LEFT":
        value = text + (" " * pad)
        _set_return_values(ctx, value)
        return value
    if mode == "RIGHT":
        value = (" " * pad) + text
        _set_return_values(ctx, value)
        return value
    if mode == "CENTER":
        value = (" " * (pad // 2)) + text + (" " * ((pad + 1) // 2))
        _set_return_values(ctx, value)
        return value
    _set_return_values(ctx, "")
    return ""

def _split_parts(text: str, sep: str) -> list[str]:
    return text.split(sep) if sep else [text]


def _split_join_trim(parts: list[str], sep: str) -> str:
    """Rebuild LOCALS the way eraMegaten's SPLIT helper ERB does.

    The source helpers keep element 0 as the scalar value and append elements
    1..last-nonempty, thereby preserving leading/interior empty fields while
    trimming trailing empty fields.
    """

    last = 0
    for i, value in enumerate(parts[:200]):
        if value != "":
            last = i
    if last <= 0:
        return parts[0] if parts else ""
    return sep.join(parts[: last + 1])


def _split_find(parts: list[str], needle: str, *, complete: bool = False, escape: bool = False) -> int:
    pattern = re.escape(needle) if escape else needle
    for i, value in enumerate(parts):
        try:
            matched = re.fullmatch(pattern, value) is not None if complete else re.search(pattern, value) is not None
        except re.error:
            matched = value == needle if complete else needle in value
        if matched:
            return i
    return -1


def _ensure_split_index(parts: list[str], index: int) -> None:
    if index >= len(parts):
        parts.extend([""] * (index - len(parts) + 1))


def _is_numeric_text(value: Any) -> bool:
    text = to_str(value).strip()
    if text == "":
        return False
    try:
        parse_era_int(text)
        return True
    except Exception:
        return False


def _signed_positive_text(value: str) -> str:
    return f"+{value}" if to_int(value) > 0 and not value.startswith("+") else value


def b_add_split(ctx, args):
    parts = _split_parts(to_str(arg(args, 0, "")), to_str(arg(args, 1, "/")))
    sep = to_str(arg(args, 1, "/"))
    add = to_str(arg(args, 2, ""))
    for i in range(200):
        _ensure_split_index(parts, i)
        if parts[i] == "":
            parts[i] = add
            return _split_join_trim(parts, sep)
    return to_str(arg(args, 0, ""))


def b_change_split(ctx, args):
    parts = _split_parts(to_str(arg(args, 0, "")), to_str(arg(args, 1, "/")))
    sep = to_str(arg(args, 1, "/"))
    index = to_int(arg(args, 2, 0))
    if len(args) >= 5:
        needle = to_str(arg(args, 3, ""))
        replacement = to_str(arg(args, 4, ""))
    else:
        needle = ""
        replacement = to_str(arg(args, 3, ""))
    if needle != "":
        index += _split_find(parts, needle, escape=True)
    if index >= 0:
        _ensure_split_index(parts, index)
        parts[index] = replacement
    return _split_join_trim(parts, sep)


def b_calc_split(ctx, args):
    parts = _split_parts(to_str(arg(args, 0, "")), to_str(arg(args, 1, "/")))
    sep = to_str(arg(args, 1, "/"))
    index = to_int(arg(args, 2, 0))
    if len(args) >= 6:
        needle = to_str(arg(args, 3, ""))
        op = to_str(arg(args, 4, ""))
        operand = to_str(arg(args, 5, ""))
    else:
        needle = ""
        op = to_str(arg(args, 3, ""))
        operand = to_str(arg(args, 4, ""))
    if needle != "":
        found = _split_find(parts, needle)
        if found == -1:
            for i in range(199):
                _ensure_split_index(parts, i + max(index, 0))
                if parts[i] == "" and parts[i + index] == "":
                    parts[i] = needle
                    found = i - 1
                    break
        index += found
    if index < 0:
        index = 0
    _ensure_split_index(parts, index)
    cur = parts[index]
    if op == "+=":
        if _is_numeric_text(operand) and _is_numeric_text(cur):
            parts[index] = str(to_int(cur) + to_int(operand))
        else:
            parts[index] = cur + operand
    elif op == "-=":
        parts[index] = str(to_int(cur) - to_int(operand))
    elif op == "*=":
        parts[index] = str(to_int(cur) * to_int(operand))
    elif op == "/=":
        denom = to_int(operand)
        parts[index] = str(int(to_int(cur) / denom)) if denom else "0"
    elif op == "==":
        parts[index] = operand
    parts[index] = _signed_positive_text(parts[index])
    return _split_join_trim(parts, sep)


def b_random_split(ctx, args):
    parts = _split_parts(to_str(arg(args, 0, "")), to_str(arg(args, 1, "/")))
    sep = to_str(arg(args, 1, "/"))
    count = max(0, min(to_int(arg(args, 2, 0)), len(parts)))
    pool = list(parts)
    out: list[str] = []
    for _ in range(count):
        if not pool:
            break
        i = random.randrange(len(pool))
        out.append(pool.pop(i))
    return sep.join(out)


def b_shift_split(ctx, args):
    parts = _split_parts(to_str(arg(args, 0, "")), to_str(arg(args, 1, "/")))
    sep = to_str(arg(args, 1, "/"))
    shift = to_int(arg(args, 2, 0))
    fill = to_str(arg(args, 3, ""))
    start = max(0, to_int(arg(args, 4, 0)))
    count = max(0, to_int(arg(args, 5, 200)))
    _ensure_split_index(parts, start + count + abs(shift) + 2)
    if shift > 0:
        for i in range(start + count - 1, start - 1, -1):
            parts[i + shift] = parts[i]
        for i in range(start, start + shift):
            parts[i] = fill
    elif shift < 0:
        n = -shift
        for i in range(start, start + max(0, count - n)):
            parts[i] = parts[i + n]
        for i in range(start + max(0, count - n), start + count):
            parts[i] = ""
    return _split_join_trim(parts, sep)

def b_autosplit(ctx, args):
    s = to_str(arg(args, 0))
    sep = to_str(arg(args, 1, "/"))
    idx = to_int(arg(args, 2, 0))
    cache = getattr(ctx, "_autosplit_cache", None)
    if cache is None:
        cache = []
        setattr(ctx, "_autosplit_cache", cache)
    if s != "再利用":
        cache = s.split(sep)
        setattr(ctx, "_autosplit_cache", cache)
    parts = cache
    needle = to_str(arg(args, 3, ""))
    if needle != "":
        try:
            found = parts.index(needle)
        except ValueError:
            return ""
        pos = found + idx
        return parts[pos] if 0 <= pos < len(parts) else ""
    return parts[idx] if 0 <= idx < len(parts) else ""


def b_autosplit_int(ctx, args):
    return to_int(b_autosplit(ctx, args))


def b_autosplit_num(ctx, args):
    s = to_str(arg(args, 0))
    sep = to_str(arg(args, 1, "/"))
    needle = to_str(arg(args, 2, ""))
    offset = to_int(arg(args, 3, 0))
    parts = s.split(sep)
    try:
        return parts.index(needle) + offset
    except ValueError:
        return -1

def b_text_random(ctx, args):
    s = to_str(arg(args, 0)); sep = "/"
    parts = [p for p in s.split(sep) if p != ""]
    return random.choice(parts) if parts else ""

def b_rand_split(ctx, args):
    s = to_str(arg(args, 0)); sep = to_str(arg(args, 1, "_"))
    parts = [p for p in s.split(sep) if p != ""]
    return random.choice(parts) if parts else ""


def b_lineisempty(ctx, args):
    current = getattr(ctx, "_current_line_text", None)
    if current:
        return 1 if current() == "" else 0
    text = "".join(getattr(ctx, "output", []))
    if text == "":
        return 1
    return 1 if text.rfind("\n") == len(text) - 1 else 0

def b_getlinestr(ctx, args):
    fill = to_str(arg(args, 0, "─")) or "─"
    width = max(1, to_int(arg(args, 1, getattr(ctx, "line_width", 72))))
    return (fill * ((width // max(1, len(fill))) + 1))[:width]

def _to_fullwidth(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch == " ":
            out.append("\u3000")
        elif 0x21 <= code <= 0x7E:
            out.append(chr(code + 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)

def b_tofull(ctx, args): return _to_fullwidth(to_str(arg(args, 0, "")))
def b_tohalf(ctx, args): return unicodedata.normalize("NFKC", to_str(arg(args, 0, "")))

def b_charatu(ctx, args):
    s = to_str(arg(args, 0, ""))
    i = to_int(arg(args, 1, 0))
    return s[i] if 0 <= i < len(s) else ""

def b_currentalign(ctx, args): return to_str(getattr(ctx, "current_alignment", "LEFT")).upper()
def b_currentredraw(ctx, args): return to_int(getattr(ctx, "current_redraw", 1)) & 1

def _config_values(ctx) -> dict[str, str]:
    cached = getattr(ctx, "_config_values_cache", None)
    if cached is not None:
        return cached
    values: dict[str, str] = {}
    path = ctx.program.root / "emuera.config"
    if path.exists():
        try:
            for raw in read_text_auto(path).splitlines():
                line = raw.strip()
                if not line or line.startswith((";", "#", "[")):
                    continue
                if ":" in line:
                    k, v = line.split(":", 1)
                elif "=" in line:
                    k, v = line.split("=", 1)
                else:
                    continue
                values[norm_name(k)] = v.strip()
        except Exception:
            pass
    defaults = {
        "描画インターフェース": "WINAPI",
        "フォント名": "ＭＳ ゴシック",
        "フォントサイズ": "18",
        "一行の高さ": "18",
        "PRINTCを並べる数": "3",
        "PRINTCの文字数": "25",
        "ウィンドウ幅": "1600",
        "ウィンドウ高さ": "950",
        "文字色": "192,192,192",
        "背景色": "0,0,0",
        "選択中文字色": "255,255,0",
        "表示するセーブデータ数": "20",
    }
    for k, v in defaults.items():
        values.setdefault(norm_name(k), v)
    setattr(ctx, "_config_values_cache", values)
    return values

def _config_raw(ctx, key: str) -> str:
    return _config_values(ctx).get(norm_name(key), "")

def b_getconfig(ctx, args):
    return to_int(_config_raw(ctx, to_str(arg(args, 0, ""))))

def b_getconfigs(ctx, args):
    return _config_raw(ctx, to_str(arg(args, 0, "")))

def b_savenos(ctx, args):
    return to_int(_config_raw(ctx, "表示するセーブデータ数") or 20)

def b_getfocuscolor(ctx, args):
    return _parse_rgb_config(_config_raw(ctx, "選択中文字色"), 0xFFFF00)

def b_getfont(ctx, args):
    font = getattr(ctx, "current_font", None)
    if font:
        return to_str(font)
    return _config_raw(ctx, "フォント名") or "ＭＳ ゴシック"

def b_getstyle(ctx, args):
    return to_int(getattr(ctx, "current_font_style", 0))

def b_chkfont(ctx, args):
    # Host font availability varies.  Accept non-empty names to keep text
    # rendering branches deterministic in headless compatibility runs.
    return 1 if to_str(arg(args, 0, "")).strip() else 0

def b_mouseskip(ctx, args):
    # Terminal compatibility runs do not have a right-click WAIT-skip state.
    # Keep a state hook for future GUI/front-end integration and expose the
    # Emuera extension function without warning in expression contexts.
    return 1 if truth(getattr(ctx, "mouse_skip", 0)) else 0

def b_isskip(ctx, args):
    return 1 if truth(getattr(ctx, "skip_display", 0)) else 0

def b_messkip(ctx, args):
    # Emuera exposes MESSKIP() while message text is being fast-forwarded by a
    # held click/Enter/right-click style input.  The headless runtime has no
    # live GUI event pump, but tests/front-ends can drive this state explicitly;
    # treat the existing mouse-skip hook as a compatible right-click fallback.
    return 1 if truth(getattr(ctx, "message_skip", 0)) or truth(getattr(ctx, "mouse_skip", 0)) else 0

def b_gcreated(ctx, args):
    fn = getattr(ctx, "_graphics_created", None)
    return fn(to_int(arg(args, 0, 0))) if fn else 0

def b_spritecreated(ctx, args):
    fn = getattr(ctx, "_sprite_created", None)
    return fn(to_str(arg(args, 0, ""))) if fn else 0

def b_spritewidth(ctx, args):
    fn = getattr(ctx, "_sprite_width", None)
    return fn(to_str(arg(args, 0, ""))) if fn else 0

def b_spriteheight(ctx, args):
    fn = getattr(ctx, "_sprite_height", None)
    return fn(to_str(arg(args, 0, ""))) if fn else 0

def b_gwidth(ctx, args):
    fn = getattr(ctx, "_graphics_width", None)
    return fn(to_int(arg(args, 0, 0))) if fn else 0

def b_gheight(ctx, args):
    fn = getattr(ctx, "_graphics_height", None)
    return fn(to_int(arg(args, 0, 0))) if fn else 0

def b_gcreatefromfile(ctx, args):
    fn = getattr(ctx, "_graphics_create_from_file", None)
    if not fn:
        return 0
    return fn(to_int(arg(args, 0, 0)), to_str(arg(args, 1, "")))


def _picture_sprite_name(chara_no: int, img_type: int = 0, line: int = 1) -> str:
    return f"A{to_int(chara_no)}_{to_int(img_type)}_{to_int(line)}"


def _sprite_exists(ctx, name: str) -> bool:
    fn = getattr(ctx, "_sprite_created", None)
    if callable(fn):
        return truth(fn(name))
    return False


def _write_plain(ctx, text: str, *, newline: bool = False) -> None:
    if hasattr(ctx, "_write"):
        ctx._write(text, newline=newline)
    else:
        _message_write(ctx, text, newline=newline)


def _print_img_placeholder(ctx, name: str) -> None:
    if name:
        record = getattr(ctx, "_record_print_img", None)
        next_line = getattr(ctx, "_next_visual_write_start_line", None)
        if not callable(next_line):
            next_line = getattr(ctx, "_next_write_start_line", None)
        current_line = getattr(ctx, "_current_line_text", None)
        if callable(record):
            record(
                name,
                next_line() if callable(next_line) else 1,
                len(current_line()) if callable(current_line) else 0,
            )
        # Keep the marker in the plain transcript for CLI/debug compatibility,
        # but never register it as styled GUI text.  Native WRITE_IMG reaches
        # this helper instead of the PRINT_IMG command path; recording the
        # marker here made every transparent portrait slice reveal a literal
        # ``[IMG:...]`` label underneath the actual sprite.
        write = getattr(ctx, "_write", None)
        if callable(write):
            write(
                f"[IMG:{name}]",
                newline=False,
                harvest_buttons=False,
                record_style=False,
            )
        else:
            _message_write(ctx, f"[IMG:{name}]", newline=False)


def _print_rect_placeholder(ctx, width: int) -> None:
    record = getattr(ctx, "_record_print_rect", None)
    next_line = getattr(ctx, "_next_visual_write_start_line", None)
    if not callable(next_line):
        next_line = getattr(ctx, "_next_write_start_line", None)
    current_line = getattr(ctx, "_current_line_text", None)
    if callable(record):
        record(
            to_int(width),
            next_line() if callable(next_line) else 1,
            len(current_line()) if callable(current_line) else 0,
        )
    _write_plain(ctx, "▭" * max(1, min(72, to_int(width) // 100 if to_int(width) else 1)), newline=False)


def _set_numeric_result(ctx, value: int, results: str | None = None) -> int:
    _ctx_set_var(ctx, "RESULT", [], to_int(value))
    _ctx_set_var(ctx, "RESULT", [0], to_int(value))
    if results is not None:
        _ctx_set_var(ctx, "RESULTS", [], results)
        _ctx_set_var(ctx, "RESULTS", [0], results)
    else:
        _ctx_set_var(ctx, "RESULTS", [], str(to_int(value)))
        _ctx_set_var(ctx, "RESULTS", [0], str(to_int(value)))
    return to_int(value)


def b_exist_picture(ctx, args):
    chara_no = to_int(arg(args, 0, 0))
    img_type = to_int(arg(args, 1, 0))
    line = to_int(arg(args, 2, 1))
    return 1 if _sprite_exists(ctx, _picture_sprite_name(chara_no, img_type, line)) else 0


def b_get_img_type(ctx, args):
    chara = to_int(arg(args, 0, 0))
    img_type = to_int(arg(args, 1, 0))
    screen = to_int(arg(args, 2, -100))
    if img_type >= 2:
        return img_type
    face = to_int(_ctx_get_var(ctx, "CFLAG", [chara, "顔グラ"]))
    if _sync_call_result(ctx, "特殊変身顔グラ変更", [chara], 0) == 1:
        special = to_int(_ctx_get_var(ctx, "CFLAG", [chara, "特殊変身顔グラ"]))
        if special != face:
            return img_type + special * 100
    if screen == 0:
        shop = to_int(_ctx_get_var(ctx, "CFLAG", [chara, "ショップ顔グラ"]))
        if shop != face:
            return img_type + shop * 100
    elif screen == 1:
        train = to_int(_ctx_get_var(ctx, "CFLAG", [chara, "調教顔グラ"]))
        if train != face:
            return img_type + train * 100
    elif screen == 2:
        devil = to_int(_ctx_get_var(ctx, "CFLAG", [chara, "悪魔変身顔グラ"]))
        if devil != face:
            return img_type + devil * 100
    elif screen == 3:
        generic_devil = to_int(_ctx_get_var(ctx, "CFLAG", [chara, "汎用悪魔変身顔グラ"]))
        if generic_devil != face:
            return img_type + generic_devil * 100
    if face > 0:
        return img_type + face * 100
    return img_type


def b_write_img(ctx, args):
    img_arg = to_int(arg(args, 0, 0))
    img_type = to_int(arg(args, 1, 0))
    line = to_int(arg(args, 2, 0))
    options = to_str(arg(args, 3, ""))
    address_mode = "ア禮服取得" in options
    chara_number_mode = "キャラ番号指定" in options

    if chara_number_mode:
        chara = img_arg
        appearance = to_int(_ctx_get_var(ctx, "CFLAG", [chara, "外見番号"]))
        if appearance > 0 and truth(b_exist_picture(ctx, [appearance, img_type])):
            img_no = appearance
        else:
            img_no = to_int(_ctx_get_var(ctx, "NO", [chara]))
    else:
        img_no = img_arg
        chara = to_int(b_findchara(ctx, ["NO", img_no]))

    if chara >= 0:
        hook = f"PRINT_SPECIAL_FACE_CHARA_{img_no}"
        if _program_has_script_function(ctx, hook):
            _ctx_set_var(ctx, "RESULT", [], 0)
            _ctx_set_var(ctx, "RESULT", [0], 0)
            _ctx_set_var(ctx, "RESULTS", [], "")
            _ctx_set_var(ctx, "RESULTS", [0], "")
            _call_script_procedure_live(ctx, hook, [chara, int(img_type / 100), img_type % 10, line % 10], max_steps=20000)
            if truth(_ctx_get_var(ctx, "RESULT", [])):
                result_name = to_str(_ctx_get_var(ctx, "RESULTS", []))
                if not address_mode:
                    _print_img_placeholder(ctx, result_name)
                return _set_numeric_result(ctx, 1, result_name if address_mode else None)

    name = _picture_sprite_name(img_no, img_type, line)
    if _sprite_exists(ctx, name):
        if address_mode:
            return _set_numeric_result(ctx, 1, name)
        _print_img_placeholder(ctx, name)
        return _set_numeric_result(ctx, 1)

    if address_mode or chara_number_mode:
        return _set_numeric_result(ctx, 0, "NO_IMG")
    _print_rect_placeholder(ctx, 400 if img_type % 2 == 0 else 600)
    return _set_numeric_result(ctx, 1)


def _show_img_padding(line: int, screen: int, target: int, assi: int) -> tuple[str, str]:
    if screen != 0:
        return "", ""
    target_pad = ""
    assi_pad = ""
    if line in {1, 3}:
        if target >= 0 and assi > 0:
            target_pad = "　　"
            assi_pad = " "
        elif target >= 0 and 0 > assi:
            target_pad = "　　"
        elif 0 >= target and assi > 0:
            assi_pad = " 　　　　　　"
    elif line in {2, 4}:
        if target >= 0 and assi > 0:
            target_pad = " "
            assi_pad = " "
        elif target >= 0 and 0 > assi:
            target_pad = " "
        elif 0 >= target and assi > 0:
            assi_pad = "　　　　　"
    return target_pad, assi_pad


def _show_img_one(ctx, chara: int, line: int, screen: int, fallback: int, pad: str, *, assi_side: bool = False) -> None:
    if chara < 0:
        return
    img_size = screen
    img_type = to_int(b_get_img_type(ctx, [chara, img_size, screen]))
    if pad:
        _write_plain(ctx, pad, newline=False)
    screen_name = "ショップ" if screen == 0 else "調教"
    if _sync_call_result(ctx, "モブ画像_設定確認", [chara, screen_name], 0):
        _call_script_procedure_live(ctx, "モブ画像_表示", [chara, screen_name, line, screen], max_steps=20000)
        return
    b_write_img(ctx, [chara, img_type, line, "キャラ番号指定"])
    if truth(_ctx_get_var(ctx, "RESULT", [])):
        return
    if fallback == 1:
        no = to_int(_ctx_get_var(ctx, "NO", [chara]))
        race_chara = to_int(_ctx_get_var(ctx, "TARGET", [])) if assi_side else chara
        _call_script_procedure_live(ctx, "RACE_ICON", [no, line, race_chara, img_size], max_steps=20000)
    elif fallback == 2:
        _call_script_procedure_live(ctx, "ONI_MARK", [chara, line, img_size], max_steps=20000)


def b_show_img(ctx, args):
    if _program_has_script_function(ctx, "SHOW_IMG"):
        if _call_script_procedure_live(ctx, "SHOW_IMG", list(args), max_steps=100000):
            return to_int(_ctx_get_var(ctx, "RESULT", []))
    line = to_int(arg(args, 0, 0))
    screen = to_int(arg(args, 1, 0))
    fallback = to_int(arg(args, 2, 0))
    target = to_int(_ctx_get_var(ctx, "TARGET", []))
    assi = to_int(_ctx_get_var(ctx, "ASSI", []))
    target_pad, assi_pad = _show_img_padding(line, screen, target, assi)
    if fallback in {1, 2}:
        if target >= 0:
            _show_img_one(ctx, target, line, screen, fallback, target_pad)
        if assi > 0:
            _show_img_one(ctx, assi, line, screen, fallback, assi_pad, assi_side=True)
    elif fallback == 3:
        if target >= 0:
            if target_pad:
                _write_plain(ctx, target_pad, newline=False)
            _call_script_procedure_live(ctx, "ONI_MARK", [target, line, screen], max_steps=20000)
        if assi > 0:
            if assi_pad:
                _write_plain(ctx, assi_pad, newline=False)
            _call_script_procedure_live(ctx, "ONI_MARK", [assi, line, screen], max_steps=20000)
    if screen == 0:
        _write_plain(ctx, "", newline=True)
    return 0


def b_face_graphic_add(ctx, args):
    if _program_has_script_function(ctx, "顔グラ追加"):
        if _call_script_procedure_live(ctx, "顔グラ追加", list(args), max_steps=200000):
            return to_int(_ctx_get_var(ctx, "RESULT", []))
    cnum = to_int(arg(args, 0, 0))
    create_file = getattr(ctx, "_graphics_create_from_file", None)
    gcreated = getattr(ctx, "_graphics_created", None)
    gwidth = getattr(ctx, "_graphics_width", None)
    gheight = getattr(ctx, "_graphics_height", None)
    if not all(callable(fn) for fn in (create_file, gcreated, gwidth, gheight)) or not hasattr(ctx, "sprites"):
        return 0
    for seq in range(64):
        if truth(b_exist_picture(ctx, [cnum, seq * 100, 1])):
            continue
        gid = cnum * 100 + seq
        if truth(gcreated(gid)):
            continue
        filename = f"画像_自家製\\A{cnum}_{seq}.png"
        if not truth(create_file(gid, filename)):
            continue
        variant = 0
        for existing in range(64):
            if truth(b_exist_picture(ctx, [cnum, existing * 100, 1])):
                variant += 1
            else:
                break
        side = min(to_int(gheight(gid)), to_int(gwidth(gid)))
        for size in (0, 1):
            cell_h = int(side / (6 if size == 1 else 4)) if side else 0
            cell_w = side
            for row in range(1, 5 + size * 2):
                name = _picture_sprite_name(cnum, variant * 100 + size, row)
                ctx.sprites[norm_name(name)] = {
                    "name": name,
                    "graphic": gid,
                    "x": 0,
                    "y": cell_h * (row - 1),
                    "width": max(0, cell_w),
                    "height": max(0, cell_h),
                }
    return 0


def _swap_indexed_values(ctx, left_base: str, left_idx: list[Value], right_base: str, right_idx: list[Value]) -> None:
    left = _ctx_get_var(ctx, left_base, left_idx)
    right = _ctx_get_var(ctx, right_base, right_idx)
    _ctx_set_var(ctx, left_base, left_idx, right)
    _ctx_set_var(ctx, right_base, right_idx, left)


def _materialized_array_values(ctx, base: str) -> list[int]:
    mem = _ctx_memory(ctx)
    values: set[int] = set()
    for table in (getattr(mem, "numeric", {}).get(norm_name(base), {}), getattr(mem, "strings", {}).get(norm_name(base), {})):
        for idx, raw in table.items():
            if len(idx) == 1:
                values.add(to_int(raw))
    frame = getattr(mem, "frame", None)
    if frame is not None:
        for table in (getattr(frame, "numeric", {}).get(norm_name(base), {}), getattr(frame, "strings", {}).get(norm_name(base), {})):
            for idx, raw in table.items():
                if len(idx) == 1:
                    values.add(to_int(raw))
    return list(values)


def b_equip_detail_item_list(ctx, args):
    signature = tuple(to_str(a) for a in args)
    pause_state = getattr(ctx, "_paused_native_equip_detail_item_list", None)
    mode = to_int(arg(args, 0, 0))
    chara = to_int(arg(args, 1, -1))
    list_count = to_int(call_builtin(ctx, "FINDELEMENT", ["物品リスト", -1]) or 0)
    selected = 0
    alt_chara = -1
    page = 0
    rendered = False
    if isinstance(pause_state, dict) and pause_state.get("signature") == signature:
        selected = to_int(pause_state.get("selected", selected))
        alt_chara = to_int(pause_state.get("alt_chara", alt_chara))
        page = to_int(pause_state.get("page", page))
        chara = to_int(pause_state.get("chara", chara))
        rendered = bool(pause_state.get("rendered", False))
    elif pause_state is not None:
        try:
            setattr(ctx, "_paused_native_equip_detail_item_list", None)
        except Exception:
            pass

    def render() -> None:
        if hasattr(ctx, "_write"):
            ctx._write("─" * 72, newline=True)
        swapped: list[tuple[str, list[Value], str, list[Value]]] = []
        if selected == 0 and chara > -1:
            for slot in range(7):
                planned = to_int(_ctx_get_var(ctx, "変更予定装備", [slot]))
                if planned != -1:
                    equip_name = to_str(call_builtin(ctx, "GET_EQUIP", [slot]) or slot)
                    _swap_indexed_values(ctx, "EQUIP", [chara, equip_name], "変更予定装備", [slot])
                    swapped.append(("EQUIP", [chara, equip_name], "変更予定装備", [slot]))
        _call_script_procedure_live(ctx, "PRINT_RPGITEMLIST_2", [list_count, mode, chara, selected], max_steps=50000)
        for left_base, left_idx, right_base, right_idx in reversed(swapped):
            _swap_indexed_values(ctx, left_base, left_idx, right_base, right_idx)

    def pause_for_input() -> Value:
        try:
            setattr(
                ctx,
                "_paused_native_equip_detail_item_list",
                {
                    "signature": signature,
                    "chara": chara,
                    "selected": selected,
                    "alt_chara": alt_chara,
                    "page": page,
                    "rendered": rendered,
                },
            )
        except Exception:
            pass
        if hasattr(ctx, "waiting_for_input"):
            ctx.waiting_for_input = True
        return _set_numeric_result(ctx, 100)

    def clear_pause() -> None:
        try:
            setattr(ctx, "_paused_native_equip_detail_item_list", None)
        except Exception:
            pass

    values = set(_materialized_array_values(ctx, "物品リスト"))
    for _ in range(16):
        if not rendered:
            render()
            rendered = True
        if (
            not getattr(ctx, "interactive", False)
            and not getattr(ctx, "inputs", None)
            and getattr(ctx, "had_explicit_inputs", False)
        ):
            return pause_for_input()
        raw = _read_string_input(ctx, "100").strip()
        result = to_int(raw)
        if result == 100:
            chara = max(chara, alt_chara)
            if chara >= 0:
                _call_script_procedure_live(ctx, "SYNC_STATUS", [chara], max_steps=50000)
            clear_pause()
            return _set_numeric_result(ctx, 100)
        if result == 6:
            chara, alt_chara = alt_chara, chara
            rendered = False
            continue
        if result == 8:
            selected = 1 - selected
            rendered = False
            continue
        if result == 7 and page > 0:
            page -= 1
            _ctx_set_var(ctx, "P", [], page)
            rendered = False
            continue
        if result == 9 and page < int((max(0, list_count) - 1) / 10):
            page += 1
            _ctx_set_var(ctx, "P", [], page)
            rendered = False
            continue
        if result in values and result != -1:
            selected = result
            chara = max(chara, alt_chara)
            if chara >= 0:
                _call_script_procedure_live(ctx, "SYNC_STATUS", [chara], max_steps=50000)
            clear_pause()
            return _set_numeric_result(ctx, selected)
        _ctx_clear_lines(ctx, 1)
        rendered = False
    clear_pause()
    return _set_numeric_result(ctx, 100)


def _list_items(text: str) -> list[str]:
    if text == "":
        return []
    parts = text.split(",")
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def _list_join(items: list[str]) -> str:
    return "".join(f"{item}," for item in items)


def b_list_set(ctx, args):
    text = to_str(arg(args, 0, ""))
    index = to_int(arg(args, 1, 0))
    value = to_str(arg(args, 2, ""))
    if index < 0:
        return text
    count = text.count(",")
    if index >= count:
        return text + ("," * (index - count)) + value + ","
    items = _list_items(text)
    while len(items) <= index:
        items.append("")
    items[index] = value
    return _list_join(items)


def b_list_add(ctx, args):
    return to_str(arg(args, 0, "")) + to_str(arg(args, 1, "")) + ","


def b_list_addlist(ctx, args):
    return to_str(arg(args, 0, "")) + "," + to_str(arg(args, 1, ""))


def b_list_get(ctx, args):
    items = _list_items(to_str(arg(args, 0, "")))
    index = to_int(arg(args, 1, 0))
    return items[index] if 0 <= index < len(items) else ""


def b_list_count(ctx, args):
    return to_str(arg(args, 0, "")).count(",")


def b_list_insertat(ctx, args):
    text = to_str(arg(args, 0, ""))
    index = to_int(arg(args, 1, 0))
    value = to_str(arg(args, 2, ""))
    if index < 0:
        return text
    count = text.count(",")
    if index >= count:
        return text + ("," * (index - count)) + value + ","
    items = _list_items(text)
    items.insert(index, value)
    return _list_join(items)


def b_list_removeat(ctx, args):
    text = to_str(arg(args, 0, ""))
    index = to_int(arg(args, 1, 0))
    items = _list_items(text)
    if not (0 <= index < len(items)):
        return text
    del items[index]
    return _list_join(items)


def b_list_indexof(ctx, args):
    needle = to_str(arg(args, 1, ""))
    for i, item in enumerate(_list_items(to_str(arg(args, 0, "")))):
        if item == needle:
            return i
    return -1


def b_list_removeall(ctx, args):
    needle = to_str(arg(args, 1, ""))
    return _list_join([item for item in _list_items(to_str(arg(args, 0, ""))) if item != needle])


def b_list_sort(ctx, args):
    return _list_join(sorted(_list_items(to_str(arg(args, 0, "")))))


def b_list_sort_r(ctx, args):
    return _list_join(sorted(_list_items(to_str(arg(args, 0, ""))), reverse=True))


def b_list_foreach(ctx, args):
    func = to_str(arg(args, 1, ""))
    param = to_str(arg(args, 2, ""))
    if not func:
        return 0
    for item in _list_items(to_str(arg(args, 0, ""))):
        _call_script_procedure_live(ctx, func, [item, param], max_steps=20000)
    return 0


def _dic_pairs(text: str) -> list[tuple[str, str, str]]:
    pairs: list[tuple[str, str, str]] = []
    for m in re.finditer(r"\[([^:\[\]]+):([^\[\]]*)\]", text):
        pairs.append((m.group(1), m.group(2), m.group(0)))
    return pairs


def b_dic_set(ctx, args):
    text = to_str(arg(args, 0, ""))
    key = to_str(arg(args, 1, ""))
    value = to_str(arg(args, 2, ""))
    token = f"[{key}:{value}]"
    pattern = re.compile(r"\[" + re.escape(key) + r":[^\[\]]*\]")
    if pattern.search(text):
        return pattern.sub(token, text, count=1)
    return text + token


def b_dic_containskey(ctx, args):
    return 1 if f"[{to_str(arg(args, 1, ''))}:" in to_str(arg(args, 0, "")) else 0


def b_dic_remove(ctx, args):
    key = to_str(arg(args, 1, ""))
    return re.sub(r"\[" + re.escape(key) + r":[^\[\]]*\]", "", to_str(arg(args, 0, "")), count=1)


def b_dic_get(ctx, args):
    key = to_str(arg(args, 1, ""))
    m = re.search(r"\[" + re.escape(key) + r":([^\[\]]*)\]", to_str(arg(args, 0, "")))
    return m.group(1) if m else ""


def b_dic_count(ctx, args):
    return to_str(arg(args, 0, "")).count("[")


def b_dic_foreach(ctx, args):
    func = to_str(arg(args, 1, ""))
    param = to_str(arg(args, 2, ""))
    if not func:
        return 0
    for key, value, _ in _dic_pairs(to_str(arg(args, 0, ""))):
        _call_script_procedure_live(ctx, func, [key, value, param], max_steps=20000)
    return 0


def _call_current_script_value(ctx, args, *, returns_string: bool, default: Value) -> Value:
    name = to_str(getattr(ctx, "_builtin_call_name", ""))
    if not name:
        return default
    try:
        if _program_has_script_function(ctx, name):
            return ctx._call_sync(name, list(args), returns_string=returns_string, max_steps=100000)
    except Exception:
        return default
    return default


def b_script_string_function(ctx, args):
    return to_str(_call_current_script_value(ctx, args, returns_string=True, default=""))


def b_script_numeric_function(ctx, args):
    return to_int(_call_current_script_value(ctx, args, returns_string=False, default=0))


def b_script_procedure(ctx, args):
    name = to_str(getattr(ctx, "_builtin_call_name", ""))
    if name and _program_has_script_function(ctx, name):
        _call_script_procedure_live(ctx, name, list(args), max_steps=200000)
    return to_int(_ctx_get_var(ctx, "RESULT", []))


def b_html_getprintedstr(ctx, args):
    getter = getattr(ctx, "_html_get_printed_str", None)
    if getter:
        return getter(None if not args else to_int(arg(args, 0, 0)))
    last_line = getattr(ctx, "_last_printed_line_text", None)
    if last_line:
        return last_line()
    return "".join(getattr(ctx, "output", []))

def b_html_popprintingstr(ctx, args):
    popper = getattr(ctx, "_html_pop_printing_str", None)
    if popper:
        return popper()
    current = getattr(ctx, "_current_line_text", None)
    return current() if current else ""

def b_html_escape(ctx, args):
    return html.escape(to_str(arg(args, 0, "")), quote=True)

def b_html_toplaintext(ctx, args):
    s = to_str(arg(args, 0, ""))
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)

_HTML_TOKEN_RE = re.compile(r"<!--.*?-->|<[^>]*>|[^<]+", re.DOTALL)
_HTML_VOID_TAGS = {"AREA", "BASE", "BR", "COL", "EMBED", "HR", "IMG", "INPUT", "LINK", "META", "PARAM", "SOURCE", "TRACK", "WBR"}

def _html_font_half_width(ctx) -> int:
    font_size = to_int(_config_raw(ctx, "フォントサイズ") or 16) or 16
    return max(1, font_size // 2)

def _html_tag_name(tag: str) -> tuple[str, bool]:
    m = re.match(r"(?is)<\s*(/)?\s*([A-Za-z0-9:_-]+)", tag)
    if not m:
        return "", False
    return m.group(2).upper(), bool(m.group(1))

def _html_is_self_closing(tag: str) -> bool:
    return tag.rstrip().endswith("/>")

def _html_close_suffix(active_tags: list[tuple[str, str]]) -> str:
    return "".join(f"</{name.lower()}>" for name, _ in reversed(active_tags) if name)

def _html_open_prefix(active_tags: list[tuple[str, str]]) -> str:
    return "".join(tag for _, tag in active_tags)

def _html_update_active_tags(active_tags: list[tuple[str, str]], tag: str) -> None:
    if tag.startswith("<!--"):
        return
    name, closing = _html_tag_name(tag)
    if not name or name in _HTML_VOID_TAGS:
        return
    if closing:
        for i in range(len(active_tags) - 1, -1, -1):
            if active_tags[i][0] == name:
                del active_tags[i]
                break
        return
    if not _html_is_self_closing(tag):
        active_tags.append((name, tag))

def _html_tag_forces_linebreak(tag: str) -> bool:
    name, closing = _html_tag_name(tag)
    return not closing and name == "BR"

def _html_text_segments(text: str):
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\r":
            if i + 1 < len(text) and text[i + 1] == "\n":
                yield text[i : i + 2], "\n", True
                i += 2
            else:
                yield ch, "\n", True
                i += 1
            continue
        if ch == "\n":
            yield ch, "\n", True
            i += 1
            continue
        if ch == "&":
            semi = text.find(";", i + 1, min(len(text), i + 40))
            if semi != -1:
                raw = text[i : semi + 1]
                decoded = html.unescape(raw)
                if decoded != raw:
                    yield raw, decoded, False
                    i = semi + 1
                    continue
        yield ch, ch, False
        i += 1

def _html_char_pixel_width(ctx, ch: str, *, bold: bool) -> int:
    if not ch or unicodedata.combining(ch) or unicodedata.category(ch) in {"Cf", "Mn", "Me"}:
        return 0
    half = _html_font_half_width(ctx)
    if unicodedata.east_asian_width(ch) in {"F", "W"}:
        width = half * 2
    else:
        width = half
    if bold and width > 0:
        width += 1
    return width

def _html_segment_pixel_width(ctx, text: str, active_tags: list[tuple[str, str]]) -> int:
    bold = any(name in {"B", "STRONG"} for name, _ in active_tags)
    return sum(_html_char_pixel_width(ctx, ch, bold=bold) for ch in text)

def _html_segment_unit_width(ctx, text: str, active_tags: list[tuple[str, str]]) -> int:
    half = _html_font_half_width(ctx)
    pixels = _html_segment_pixel_width(ctx, text, active_tags)
    return int(math.ceil(pixels / half)) if pixels > 0 else 0

def _html_rendered_width(ctx, html_text: str, *, return_pixel: bool) -> int:
    active_tags: list[tuple[str, str]] = []
    current = 0
    max_width = 0
    for m in _HTML_TOKEN_RE.finditer(html_text):
        token = m.group(0)
        if token.startswith("<"):
            if _html_tag_forces_linebreak(token):
                max_width = max(max_width, current)
                current = 0
                continue
            _html_update_active_tags(active_tags, token)
            continue
        for _, display, is_break in _html_text_segments(token):
            if is_break:
                max_width = max(max_width, current)
                current = 0
            else:
                current += _html_segment_pixel_width(ctx, display, active_tags)
    max_width = max(max_width, current)
    if return_pixel:
        return max_width
    half = _html_font_half_width(ctx)
    return int(math.ceil(max_width / half)) if max_width > 0 else 0

def _html_split_first(ctx, html_text: str, width: int) -> tuple[str, str]:
    limit = max(1, width)
    active_tags: list[tuple[str, str]] = []
    first_parts: list[str] = []
    current_width = 0
    for m in _HTML_TOKEN_RE.finditer(html_text):
        token = m.group(0)
        if token.startswith("<"):
            if _html_tag_forces_linebreak(token):
                first = "".join(first_parts) + _html_close_suffix(active_tags)
                rest = _html_open_prefix(active_tags) + html_text[m.end() :]
                return first, rest
            first_parts.append(token)
            _html_update_active_tags(active_tags, token)
            continue
        offset = 0
        for raw, display, is_break in _html_text_segments(token):
            start = m.start() + offset
            end = start + len(raw)
            offset += len(raw)
            if is_break:
                first = "".join(first_parts) + _html_close_suffix(active_tags)
                rest = _html_open_prefix(active_tags) + html_text[end:]
                return first, rest
            seg_width = _html_segment_unit_width(ctx, display, active_tags)
            if current_width > 0 and current_width + seg_width > limit:
                first = "".join(first_parts) + _html_close_suffix(active_tags)
                rest = _html_open_prefix(active_tags) + html_text[start:]
                return first, rest
            first_parts.append(raw)
            current_width += seg_width
    return html_text, ""

def _html_wrapped_lines(ctx, html_text: str, width: int) -> list[str]:
    lines: list[str] = []
    rest = html_text
    for _ in range(10000):
        first, next_rest = _html_split_first(ctx, rest, width)
        lines.append(first)
        if not next_rest:
            break
        if next_rest == rest:
            lines.append(next_rest)
            break
        rest = next_rest
    return lines or [""]

def b_html_stringlen(ctx, args):
    return_pixel = to_int(arg(args, 1, 0)) != 0
    return _html_rendered_width(ctx, to_str(arg(args, 0, "")), return_pixel=return_pixel)

def b_html_substring(ctx, args):
    html_text = to_str(arg(args, 0, ""))
    first, rest = _html_split_first(ctx, html_text, to_int(arg(args, 1, 0)))
    _ctx_set_var(ctx, "RESULTS", [], first)
    _ctx_set_var(ctx, "RESULTS", [0], first)
    _ctx_set_var(ctx, "RESULTS", [1], rest)
    _ctx_set_var(ctx, "RESULT", [], 1)
    return first

def b_html_stringlines(ctx, args):
    return len(_html_wrapped_lines(ctx, to_str(arg(args, 0, "")), to_int(arg(args, 1, 0))))

def b_cmatch(ctx, args):
    return _cmatch(ctx, args)

def b_convert(ctx, args):
    value = to_int(arg(args, 0, 0))
    base = max(2, min(36, to_int(arg(args, 1, 10))))
    sign = "-" if value < 0 else ""
    n = abs(value)
    digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, base)
        out.append(digits[r])
    return sign + "".join(reversed(out))

PALAMLV_DEFAULTS = [
    0, 100, 500, 3000, 10000, 30000, 60000, 100000, 150000, 250000,
    1000000, 5000000, 30000000, 100000000, 250000000, 450000000,
    650000000, 900000000,
]
EXPLV_DEFAULTS = [0, 1, 4, 20, 50, 200, 500, 1000, 1500, 2000, 3000]

def _level_threshold(ctx, table: str, defaults: list[int], level: int) -> int:
    value = to_int(ctx.memory.get_var(table, [level]))
    if value:
        return value
    if 0 <= level < len(defaults):
        return defaults[level]
    return defaults[-1]

def _get_threshold_level(ctx, args, table: str, defaults: list[int]) -> int:
    value = to_int(arg(args, 0, 0))
    max_level = max(0, to_int(arg(args, 1, len(defaults) - 1)))
    materialized = [idx[0] for idx in ctx.memory.numeric.get(table, {}) if len(idx) == 1]
    highest = max([len(defaults) - 1, *materialized])
    max_level = min(max_level, highest)
    for level in range(max_level, -1, -1):
        if value >= _level_threshold(ctx, table, defaults, level):
            return level
    return 0

def b_getpalamlv(ctx, args):
    return _get_threshold_level(ctx, args, "PALAMLV", PALAMLV_DEFAULTS)

def b_getexplv(ctx, args):
    return _get_threshold_level(ctx, args, "EXPLV", EXPLV_DEFAULTS)


def b_varsize(ctx, args):
    name = norm_name(_raw_identifier_text(arg(args, 0)))
    if len(args) >= 2 and arg(args, 1, "") != "":
        dim = to_int(arg(args, 1))
        if dim < 0:
            return 0
        dims = _ctx_array_dimensions(ctx, name)
        if dims:
            return dims[dim] if dim < len(dims) else 0
        if dim != 0:
            return 0
    array_size = getattr(ctx, "array_size", None)
    if array_size is not None:
        try:
            return int(array_size(name))
        except Exception:
            pass
    csv = getattr(getattr(ctx, "program", None), "csv", None)
    if csv is not None and name in getattr(csv, "variable_sizes", {}):
        raw_dims = csv.variable_sizes[name]
        if isinstance(raw_dims, (tuple, list)):
            return int(raw_dims[0]) if raw_dims else 0
        return int(raw_dims)
    # Emuera arrays are statically sized.  Return generous compatibility sizes.
    if name in {"LOCAL", "LOCALS", "ARG", "ARGS"}:
        return 1000
    if name in {"CHARA", "BASE", "CFLAG", "CSTR"}:
        return 10000
    return 100000

def b_existcsv(ctx, args):
    no = to_int(arg(args, 0))
    sp = to_int(arg(args, 1, 0)) != 0
    return 1 if ctx.program.csv and ctx.program.csv.csv_exists(no, sp=sp) else 0

def b_analyze_count_check(ctx, args):
    """Native equivalent of eraMegaten's hot ANALYZE_COUNT_CHECK #FUNCTION.

    The script version scans CSV ids 1..9998 and is called from shop/event
    availability checks.  Keeping the same predicate here avoids exhausting the
    synchronous expression-function step budget while preserving the ERB result.
    """
    db = ctx.program.csv
    if not db:
        return 0
    megaten_only = to_int(arg(args, 0))
    analyze_bottom = to_int(arg(args, 1))
    analyze_top = to_int(arg(args, 2))
    level_bottom = to_int(arg(args, 3))
    level_top = to_int(arg(args, 4))
    category = to_int(arg(args, 5, -1))
    idx_race = db.resolve_index("ABL", "種族")
    idx_extra = db.resolve_index("CFLAG", "EXTRA出典")
    idx_lv = db.resolve_index("BASE", "LV")
    count = 0
    for no, tmpl in db.characters.items():
        if not (1 <= no < 9999):
            continue
        race = to_int(tmpl.numeric.get("ABL", {}).get(idx_race, 0))
        if race in {0, 36, 39, 40}:
            continue
        if megaten_only == 1 and to_int(tmpl.numeric.get("CFLAG", {}).get(idx_extra, 0)) >= 1:
            continue
        level = to_int(tmpl.numeric.get("BASE", {}).get(idx_lv, 0))
        if not (level_bottom <= level <= level_top):
            continue
        if category != -1 and race != category:
            continue
        analyzed = to_int(ctx.memory.get_var("FLAG", [20000 + no]))
        if analyze_bottom * 10 <= analyzed <= analyze_top * 10:
            count += 1
    return count

_REMODEL_EQUIP_RANGES = (
    (2390, 2399),
    (2940, 2949),
    (3440, 3449),
    (3940, 3949),
    (4400, 4409),
    (4940, 4949),
)

_MAGIC_EQUIP_RANGES = (
    (2450, 2499),
    (2950, 2999),
    (3450, 3499),
    (3950, 3999),
    (4450, 4499),
    (4950, 4999),
)


def b_magic_equipment(ctx, args):
    value = to_int(arg(args, 0))
    return 1 if any(start <= value <= end for start, end in _MAGIC_EQUIP_RANGES) else 0

def _remodel_equipment_index(value: int) -> int:
    offset = 0
    for start, end in _REMODEL_EQUIP_RANGES:
        if start <= value <= end:
            return offset + (value - start)
        offset += end - start + 1
    return -1

def _remodel_equipment_number(index: int) -> int:
    offset = 0
    for start, end in _REMODEL_EQUIP_RANGES:
        size = end - start + 1
        if offset <= index < offset + size:
            return start + (index - offset)
        offset += size
    return 0

def b_remodel_equipment(ctx, args):
    return 1 if _remodel_equipment_index(to_int(arg(args, 0))) >= 0 else 0

def b_remodel_equipment_index(ctx, args):
    index = _remodel_equipment_index(to_int(arg(args, 0)))
    return index if index >= 0 else 0

def b_remodel_equipment_number(ctx, args):
    return _remodel_equipment_number(to_int(arg(args, 0)))

def b_toupper(ctx, args): return to_str(arg(args, 0)).upper()
def b_tolower(ctx, args): return to_str(arg(args, 0)).lower()


def b_inis(ctx, args): return "行動順" + to_str(to_int(arg(args, 0, 0)))
def b_poss(ctx, args): return "ポジション" + to_str(to_int(arg(args, 0, 0)))
def b_skillnum(ctx, args): return to_int(_ctx_get_var(ctx, "ABL", [arg(args, 0, 0), "技能" + to_str(to_int(arg(args, 1, 0)))]))


def b_subplayer(ctx, args):
    assi = to_int(_ctx_get_var(ctx, "ASSI", []))
    if assi < 0:
        return -2
    if truth(_ctx_get_var(ctx, "ASSIPLAY", [])):
        return to_int(_ctx_get_var(ctx, "MASTER", []))
    return assi


_CLOTHES_PRIORITY = [13, 12, 11, 10, 3, 1, 2, 0, 9, 7, 4, 8, 5, 6]
_BREAST_PRIORITY = [10, 3, 1, 2, 0, 9, 7, 4, 8, 5, 6]
_CLOTHES_LOWER = ("全身服", "下衣", "全身内衣", "内衣（下）")


def _tequip_index(ctx, name: str) -> int:
    return _csv_index(ctx, "TEQUIP", name)


def _tequip_name(ctx, index: int) -> str:
    return _csv_name(ctx, "TEQUIP", index)


def _clothes_slot_name(ctx, slot: int) -> str:
    slot = to_int(slot)
    if slot < 12:
        return _tequip_name(ctx, _tequip_index(ctx, "帽子") + slot)
    return _tequip_name(ctx, _tequip_index(ctx, "其他2") + (slot - 12))


def _expose_name(ctx, part: int) -> str:
    return _tequip_name(ctx, _tequip_index(ctx, "腕露出") + to_int(part))


def _tequip_get(ctx, chara: int, name: str) -> int:
    return to_int(_ctx_get_var(ctx, "TEQUIP", [chara, name]))


def _tequip_set(ctx, chara: int, name: str, value: Value) -> None:
    _ctx_set_var(ctx, "TEQUIP", [chara, name], value)


def _clothes_value(ctx, chara: int, slot: int) -> int:
    return _tequip_get(ctx, chara, _clothes_slot_name(ctx, slot))


def _call_script_value(ctx, name: str, args: list[Value] | None = None, default: Value = 0) -> Value:
    args = args or []
    try:
        if _ctx_can_call_script(ctx, name):
            return ctx._call_sync(name, args)
    except Exception:
        return default
    return default


def _call_clothes(ctx, kind: str, item: int, args: list[Value] | None = None, default: int = 0) -> int:
    return to_int(_call_script_value(ctx, f"CLOTHES_{kind}_{to_int(item)}", args or [], default))


def _lower_naked_reset(ctx, chara: int) -> None:
    if all(_tequip_get(ctx, chara, name) == 0 for name in _CLOTHES_LOWER):
        if _tequip_get(ctx, chara, "Vずらし中") == -1:
            _tequip_set(ctx, chara, "Vずらし中", 0)


def _tequip_recheck(ctx, chara: int) -> None:
    b_breast_open_check(ctx, [chara])
    b_check_expose(ctx, [chara])
    b_crotch_structure_check(ctx, [chara])
    b_touch_check(ctx, [chara])
    b_shift_check(ctx, [chara])


def b_get_clothes(ctx, args):
    return _tequip_index(ctx, to_str(arg(args, 0, ""))) - _tequip_index(ctx, "帽子")


def b_get_clothesname(ctx, args):
    return _clothes_slot_name(ctx, to_int(arg(args, 0, 0)))


def b_name_expose(ctx, args):
    return _expose_name(ctx, to_int(arg(args, 0, 0)))


def b_clothesnamef(ctx, args):
    chara = to_int(arg(args, 0, -99))
    if chara == -99:
        chara = to_int(_ctx_get_var(ctx, "TARGET", []))
    slot = to_int(arg(args, 1, 0))
    if slot < 0 or slot > 13:
        return "不正な衣装名"
    item = to_int(_ctx_get_var(ctx, "CFLAG", [chara, _clothes_slot_name(ctx, slot)]))
    return to_str(_ctx_get_var(ctx, "ITEMNAME", [6000 + item]))


def b_clothes_name(ctx, args):
    slot = to_int(arg(args, 0, 0))
    chara = to_int(arg(args, 1, 0))
    item = to_int(_ctx_get_var(ctx, "CFLAG", [chara, _clothes_slot_name(ctx, slot)]))
    text = to_str(_ctx_get_var(ctx, "ITEMNAME", [6000 + item]))
    if hasattr(ctx, "_write"):
        ctx._write(text, newline=False)
    return text


def b_clothes_initialize(ctx, args):
    chara = to_int(arg(args, 0, 0))
    bit = 1
    for slot in range(14):
        name = _clothes_slot_name(ctx, slot)
        initial = to_int(_ctx_get_var(ctx, "CFLAG", [chara, 60 + slot]))
        if initial == -1:
            value = 0
        else:
            no = to_int(_ctx_get_var(ctx, "NO", [chara]))
            default = to_int(call_builtin(ctx, "CSVCFLAG", [no, 40 + slot, 0]) or 0)
            value = initial if initial else default
        if value < 0:
            value = 0
        try:
            if to_int(_ctx_get_var(ctx, "NO", [chara])) == to_int(_ctx_get_var(ctx, "NO", [to_int(_ctx_get_var(ctx, "MASTER", []))])) and slot >= 12:
                value = 0
        except Exception:
            pass
        _ctx_set_var(ctx, "CFLAG", [chara, name], value)
        if value:
            cur = to_int(_ctx_get_var(ctx, "CFLAG", [chara, 23]))
            _ctx_set_var(ctx, "CFLAG", [chara, 23], cur | bit)
        bit <<= 1
    return 0


def b_check_exp(ctx, args):
    chara = to_int(arg(args, 0, 0))
    for slot in range(14):
        item = _clothes_value(ctx, chara, slot)
        if item:
            _call_script_value(ctx, f"CLOTHES_EXP_{item}", [chara], 0)
    return 0


def b_check_source(ctx, args):
    chara = to_int(arg(args, 0, 0))
    for slot in range(14):
        item = _clothes_value(ctx, chara, slot)
        if item:
            _call_script_value(ctx, f"CLOTHES_SOURCE_{item}", [chara], 0)
    return 0


def b_check_expose(ctx, args):
    chara = to_int(arg(args, 0, 0))
    for part in range(8):
        expose = _expose_name(ctx, part)
        _tequip_set(ctx, chara, expose, -1)
        if part in {3, 4} and _tequip_get(ctx, chara, "打開胸前") == 1:
            continue
        covered = [0] * 14
        for slot in range(14):
            item = _clothes_value(ctx, chara, slot)
            if _call_clothes(ctx, "EXPOSE", item, [part + 1], 0) == 0:
                covered[slot] = 1
        for slot in _CLOTHES_PRIORITY:
            if covered[slot]:
                _tequip_set(ctx, chara, expose, slot)
                break
    return 0


def b_set_clothes_drop_all(ctx, args):
    chara = to_int(arg(args, 0, 0))
    for slot in range(14):
        _tequip_set(ctx, chara, _clothes_slot_name(ctx, slot), 0)
    _lower_naked_reset(ctx, chara)
    _tequip_recheck(ctx, chara)
    return 0


def b_set_clothes_drop_bottoms(ctx, args):
    chara = to_int(arg(args, 0, 0))
    for name in ("全身服", "下衣"):
        _tequip_set(ctx, chara, name, 0)
    _lower_naked_reset(ctx, chara)
    _tequip_recheck(ctx, chara)
    return 0


def b_set_clothes_drop_inner(ctx, args):
    chara = to_int(arg(args, 0, 0))
    mode = to_int(arg(args, 1, 0))
    if mode != 2:
        _tequip_set(ctx, chara, "内衣（上）", 0)
    if mode != 1:
        _tequip_set(ctx, chara, "内衣（下）", 0)
    _tequip_set(ctx, chara, "全身内衣", 0)
    _lower_naked_reset(ctx, chara)
    _tequip_recheck(ctx, chara)
    return 0


def b_set_clothes_drop_outer(ctx, args):
    chara = to_int(arg(args, 0, 0))
    _tequip_set(ctx, chara, "外衣", 0)
    _tequip_recheck(ctx, chara)
    return 0


def b_set_clothes_drop_tops(ctx, args):
    chara = to_int(arg(args, 0, 0))
    _tequip_set(ctx, chara, "全身服", 0)
    _tequip_set(ctx, chara, "服", 0)
    _tequip_recheck(ctx, chara)
    return 0


def b_set_clothes_equip_all(ctx, args):
    chara = to_int(arg(args, 0, 0))
    for slot in range(14):
        name = _clothes_slot_name(ctx, slot)
        _tequip_set(ctx, chara, name, to_int(_ctx_get_var(ctx, "CFLAG", [chara, name])))
    if _tequip_get(ctx, chara, "Vずらし中") == -1:
        _tequip_set(ctx, chara, "Vずらし中", 0)
    _tequip_recheck(ctx, chara)
    return 0


def b_breast_open_check(ctx, args):
    chara = to_int(arg(args, 0, 0))
    opens = [0] * 14
    _tequip_set(ctx, chara, "胸構造", 0)
    for slot in range(14):
        item = _clothes_value(ctx, chara, slot)
        r1 = _call_clothes(ctx, "EXPOSE", item, [4], 0)
        r2 = _call_clothes(ctx, "EXPOSE", item, [5], 0)
        if r1 == 1 and r2 == 1:
            continue
        breast = _call_clothes(ctx, "BREAST", item, [], 0)
        if breast == 0:
            _tequip_set(ctx, chara, "打開胸前", 0)
            return 0
        opens[slot] = breast
    for slot in _BREAST_PRIORITY:
        if opens[slot]:
            _tequip_set(ctx, chara, "胸構造", opens[slot])
            break
    if _tequip_get(ctx, chara, "胸構造") == 6:
        _tequip_set(ctx, chara, "打開胸前", 1)
    return 0


def b_crotch_structure_check(ctx, args):
    chara = to_int(arg(args, 0, 0))
    structures = [0] * 14
    _tequip_set(ctx, chara, "股間構造", 0)
    _tequip_set(ctx, chara, "裙子被向上巻起", 0)
    _tequip_set(ctx, chara, "陰唇可視", _tequip_get(ctx, chara, "陰唇露出"))
    _tequip_set(ctx, chara, "臀部可視", _tequip_get(ctx, chara, "臀部露出"))
    bits = 0
    for slot in range(14):
        item = _clothes_value(ctx, chara, slot)
        crotch = _call_clothes(ctx, "CROTCH", item, [], 0)
        if crotch <= 0:
            continue
        if crotch == 3:
            skirt = _call_clothes(ctx, "SKIRT", item, [], 3)
            if skirt == 0:
                skirt = 3
            bits |= 1 << (9 + skirt)
        elif _tequip_get(ctx, chara, "陰唇露出") == slot:
            if crotch > 3:
                crotch -= 1
            bits |= 1 << (crotch - 1)
        structures[slot] = crotch
    if _tequip_get(ctx, chara, "陰唇露出") == -1 and 4 in structures:
        bits |= 1 << 2
    _tequip_set(ctx, chara, "股間構造", bits)
    if bits & (1 << 13):
        _tequip_set(ctx, chara, "裙子被向上巻起", 4)
        _tequip_set(ctx, chara, "陰唇可視", 0)
        _tequip_set(ctx, chara, "臀部可視", 0)
    elif bits & (1 << 12):
        _tequip_set(ctx, chara, "裙子被向上巻起", 3)
        _tequip_set(ctx, chara, "陰唇可視", 0)
        _tequip_set(ctx, chara, "臀部可視", 0)
    elif bits & (1 << 14):
        _tequip_set(ctx, chara, "裙子被向上巻起", 1)
        if not truth(call_builtin(ctx, "HAVE_PENIS", [chara]) or 0):
            _tequip_set(ctx, chara, "陰唇可視", 0)
        _tequip_set(ctx, chara, "臀部可視", 0)
    elif bits & (1 << 11):
        _tequip_set(ctx, chara, "裙子被向上巻起", 2)
        if not truth(call_builtin(ctx, "HAVE_PENIS", [chara]) or 0):
            _tequip_set(ctx, chara, "陰唇可視", 0)
        _tequip_set(ctx, chara, "臀部可視", 0)
    elif bits & (1 << 10):
        _tequip_set(ctx, chara, "裙子被向上巻起", 1)
    if _tequip_get(ctx, chara, "打開股間前") and _tequip_get(ctx, chara, "Ｖ不可") and _tequip_get(ctx, chara, "Vずらし中") == -1:
        if not truth(call_builtin(ctx, "HAVE_PENIS", [chara]) or 0):
            _tequip_set(ctx, chara, "陰唇可視", 0)
        _tequip_set(ctx, chara, "臀部可視", 0)
    return 0


def b_shift_check(ctx, args):
    chara = to_int(arg(args, 0, 0))
    for label, expose_label, expose_arg in (("Ｖ", "陰唇", 7), ("Ａ", "臀部", 8)):
        _tequip_set(ctx, chara, label + "不可", 1)
        if _tequip_get(ctx, chara, expose_label + "露出") == -1:
            _tequip_set(ctx, chara, label + "不可", 0)
            continue
        for slot in range(14):
            item = _clothes_value(ctx, chara, slot)
            if _call_clothes(ctx, "EXPOSE", item, [expose_arg], 0) == 1:
                continue
            if _call_clothes(ctx, "CROTCH", item, [], 0) != 2:
                _tequip_set(ctx, chara, label + "不可", 0)
                break
    return 0


def b_touch_check(ctx, args):
    chara = to_int(arg(args, 0, 0))
    checks = [
        ("Ｃ", "陰唇", 7, 0),
        ("Ｖ", "陰唇", 7, 1),
        ("Ａ", "臀部", 8, 2),
        ("乳首", "乳首", 5, 3),
        ("乳房", "乳房", 4, 4),
    ]
    for label, expose_label, expose_arg, touch_arg in checks:
        touches = [1] * 14
        _tequip_set(ctx, chara, label + "触覚", 1)
        _tequip_set(ctx, chara, "被覆愛撫" + label, 0)
        _tequip_set(ctx, chara, "服内部愛撫" + label, 0)
        if _tequip_get(ctx, chara, expose_label + "露出") == -1:
            continue
        blocked = False
        for slot in range(14):
            item = _clothes_value(ctx, chara, slot)
            exposed = _call_clothes(ctx, "EXPOSE", item, [expose_arg], 0)
            if exposed != 1 and expose_arg in {4, 5} and _tequip_get(ctx, chara, "打開胸前"):
                if _call_clothes(ctx, "OPENFRONT", item, [], 0):
                    exposed = 1
            if exposed == 1:
                touches[slot] = 1
            else:
                touch = _call_clothes(ctx, "触覚", item, [touch_arg], 0)
                if touch == 0:
                    _tequip_set(ctx, chara, label + "触覚", 0)
                    blocked = True
                    break
                touches[slot] = touch
        if blocked or _tequip_get(ctx, chara, label + "触覚") == 0:
            continue
        trans = 0
        for touch in touches:
            if touch != 1 and (touch & 2) == 0:
                trans += 2
                break
            if touch & 2:
                trans += 1
        if trans > 1:
            _tequip_set(ctx, chara, label + "触覚", 0)
            continue
        if trans == 1:
            _tequip_set(ctx, chara, label + "触覚", 2)
            _tequip_set(ctx, chara, "被覆愛撫" + label, 1)
        inside = 0
        for touch in touches:
            if touch != 1 and (touch & 4) == 0:
                _tequip_set(ctx, chara, label + "触覚", 0)
                inside = 0
                break
            if touch != 1 and (touch & 4):
                inside += 1
        if inside > 0:
            if _tequip_get(ctx, chara, label + "触覚") == 1:
                _tequip_set(ctx, chara, label + "触覚", 0)
            _tequip_set(ctx, chara, label + "触覚", _tequip_get(ctx, chara, label + "触覚") + 4)
            _tequip_set(ctx, chara, "服内部愛撫" + label, 1)
    return 0


def b_set_clothes_naked_breast(ctx, args):
    chara = to_int(arg(args, 0, 0))
    mode = to_int(arg(args, 1, 0))
    for name in ("服", "全身服", "外衣"):
        item = _tequip_get(ctx, chara, name)
        if item and _call_clothes(ctx, "BREAST", item, [], 0) == 0:
            _tequip_set(ctx, chara, name, 0)
    if mode == 0:
        b_breast_open_check(ctx, [chara])
        if _tequip_get(ctx, chara, "胸構造") & (1 + 2 + 4 + 8 + 16):
            _tequip_set(ctx, chara, "打開胸前", 0 if _tequip_get(ctx, chara, "打開胸前") & 1 else 1)
        else:
            b_set_clothes_drop_inner(ctx, [chara, 1])
    _tequip_recheck(ctx, chara)
    return 0


def b_set_clothes_naked_crotch(ctx, args):
    chara = to_int(arg(args, 0, 0))
    mode = to_int(arg(args, 1, 0))
    for name in ("下衣", "全身服", "外衣", "襪子"):
        item = _tequip_get(ctx, chara, name)
        if item and _call_clothes(ctx, "CROTCH", item, [], 0) == 1:
            _tequip_set(ctx, chara, name, 0)
    if mode == 0:
        b_shift_check(ctx, [chara])
        b_crotch_structure_check(ctx, [chara])
        if (_tequip_get(ctx, chara, "股間構造") & (1 + 2 + 4 + 8 + 16)) and _tequip_get(ctx, chara, "Ｖ不可"):
            _tequip_set(ctx, chara, "打開股間前", 0 if _tequip_get(ctx, chara, "打開股間前") & 1 else 1)
            _tequip_set(ctx, chara, "Vずらし中", -1)
        else:
            b_set_clothes_drop_inner(ctx, [chara, 2])
    _tequip_recheck(ctx, chara)
    return 0


def _set_return_values(ctx, *values: Value) -> None:
    for i, value in enumerate(values):
        if isinstance(value, str):
            _ctx_set_var(ctx, "RESULTS", [i], value)
            if i == 0:
                _ctx_set_var(ctx, "RESULTS", [], value)
        else:
            _ctx_set_var(ctx, "RESULT", [i], to_int(value))
            _ctx_set_var(ctx, "RESULTS", [i], str(to_int(value)))
            if i == 0:
                _ctx_set_var(ctx, "RESULT", [], to_int(value))
                _ctx_set_var(ctx, "RESULTS", [], str(to_int(value)))


def b_add_exp(ctx, args):
    exp_no = to_int(arg(args, 0, 0))
    amount = to_int(arg(args, 1, 0))
    chara = to_int(_ctx_get_var(ctx, "TARGET", [])) if to_int(arg(args, 2, -99)) == -99 else to_int(arg(args, 2, 0))
    if chara < 0:
        return 0
    if exp_no == _csv_index(ctx, "EXP", "膣射経験"):
        _ctx_set_var(ctx, "TCVAR", [chara, 101], to_int(_ctx_get_var(ctx, "TCVAR", [chara, 101])) + amount)
    _ctx_set_var(ctx, "TCVAR", [chara, exp_no], to_int(_ctx_get_var(ctx, "TCVAR", [chara, exp_no])) + amount)
    return 0


def b_adds_exp(ctx, args):
    return b_add_exp(ctx, [_csv_index(ctx, "EXP", to_str(arg(args, 0, ""))), arg(args, 1, 0), arg(args, 2, -99)])


def b_set_battle_status(ctx, args):
    chara = to_int(arg(args, 0, 0))
    value = to_int(arg(args, 1, 0))
    name = to_str(arg(args, 2, ""))
    delta = value - to_int(_ctx_get_var(ctx, "MAXBASE", [chara, name]))
    _ctx_set_var(ctx, "CFLAG", [chara, name + "補正"], to_int(_ctx_get_var(ctx, "CFLAG", [chara, name + "補正"])) + delta)
    _ctx_set_var(ctx, "MAXBASE", [chara, name], to_int(_ctx_get_var(ctx, "MAXBASE", [chara, name])) + delta)
    return 0


def b_set_eventflag(ctx, args):
    event_no = to_int(arg(args, 0, 0))
    clear = truth(arg(args, 1, 0))
    chara_arg = to_int(arg(args, 2, 0))
    chara = chara_arg if chara_arg else to_int(_ctx_get_var(ctx, "TARGET", []))
    if event_no >= 1260 or event_no < 0 or chara < 0:
        _set_return_values(ctx, 0)
        return 0
    slot = int(event_no / 63) + to_int(b_set_kojo_function_cflag(ctx, [chara]) or 0) + 20
    bit = event_no % 63
    value = to_int(_ctx_get_var(ctx, "CFLAG", [chara, slot]))
    value = (value & ~(1 << bit)) if clear else (value | (1 << bit))
    _ctx_set_var(ctx, "CFLAG", [chara, slot], value)
    _set_return_values(ctx, 0)
    return 0


def b_set_relation(ctx, args):
    chara = to_int(arg(args, 0, 0))
    start = _csv_index(ctx, "CFLAG", "キャラ相性値1")
    end = _csv_index(ctx, "CFLAG", "相性値20") + 1
    no = to_int(_ctx_get_var(ctx, "NO", [chara]))
    mother = to_int(_ctx_get_var(ctx, "CFLAG", [chara, "娘の産みの親のキャラ番号娘"]))
    father = to_int(_ctx_get_var(ctx, "CFLAG", [chara, "娘の父親のキャラ番号娘"]))
    for slot in range(start, end, 2):
        source_no = no
        if no in {3501, 3502} and (mother >= 0 or father >= 0):
            source_no = mother if mother >= 0 else father
        value = to_int(call_builtin(ctx, "CSVCFLAG", [source_no, slot, 0]) or 0)
        if value == 0:
            value = 100
            _ctx_set_var(ctx, "CFLAG", [chara, slot - 1], -1)
        _ctx_set_var(ctx, "CFLAG", [chara, slot], value)
    return 0


def b_set_sex(ctx, args):
    _ctx_set_var(ctx, "TALENT", [to_int(arg(args, 0, -1)), "男性"], to_int(arg(args, 1, 0)))
    return 0


def _swap_var(ctx, base1: str, idx1: list[Value], base2: str, idx2: list[Value]) -> None:
    v1 = _ctx_get_var(ctx, base1, idx1)
    v2 = _ctx_get_var(ctx, base2, idx2)
    _ctx_set_var(ctx, base1, idx1, v2)
    _ctx_set_var(ctx, base2, idx2, v1)


def b_initial_ts(ctx, args):
    chara = to_int(arg(args, 0, 0))
    if _talent(ctx, chara, "男性"):
        _ctx_set_var(ctx, "CFLAG", [chara, "元処女"], 1)
    for cf in ("ＴＳ時会話類型", "人化時会話類型"):
        if to_int(_ctx_get_var(ctx, "CFLAG", [chara, cf])) == 0:
            _ctx_set_var(ctx, "CFLAG", [chara, cf], to_int(_ctx_get_var(ctx, "ABL", [chara, "会話類型"])))
    if to_int(_ctx_get_var(ctx, "CFLAG", [chara, "ＴＳ人化時会話類型"])) == 0:
        _ctx_set_var(ctx, "CFLAG", [chara, "ＴＳ人化時会話類型"], to_int(_ctx_get_var(ctx, "CFLAG", [chara, "ＴＳ時会話類型"])))
    for dst, src in ((16, 11), (17, 12)):
        if to_str(_ctx_get_var(ctx, "CSTR", [chara, dst])) == "":
            _ctx_set_var(ctx, "CSTR", [chara, dst], to_str(_ctx_get_var(ctx, "CSTR", [chara, src])))
    return 0


def b_ts_process(ctx, args):
    chara = to_int(arg(args, 0, 0))
    bust = to_int(arg(args, 1, 0))
    if to_int(_ctx_get_var(ctx, "CFLAG", [chara, "元胸サイズ"])) == 0:
        _ctx_set_var(ctx, "CFLAG", [chara, "元胸サイズ"], 3 if _talent(ctx, chara, "男性") else 1)
    if not to_int(_ctx_get_var(ctx, "EXP", [chara, "ＴＳ経験"])):
        b_initial_ts(ctx, [chara])
    _ctx_set_var(ctx, "TALENT", [chara, "男性"], 0 if _talent(ctx, chara, "男性") else 1)
    for t, c in (("処女", "元処女"), ("FUTA", "元FUTA"), ("偽娘", "元偽娘")):
        _swap_var(ctx, "TALENT", [chara, t], "CFLAG", [chara, c])
    if to_int(_ctx_get_var(ctx, "CFLAG", [chara, "元Ｖ敏感"])):
        _ctx_set_var(ctx, "TALENT", [chara, "Ｖ敏感"], 1)
    if to_int(_ctx_get_var(ctx, "CFLAG", [chara, "元Ｖ鈍感"])):
        _ctx_set_var(ctx, "TALENT", [chara, "Ｖ鈍感"], 1)
    _swap_var(ctx, "ABL", [chara, "会話類型"], "CFLAG", [chara, "ＴＳ時会話類型"])
    _swap_var(ctx, "CFLAG", [chara, "人化時会話類型"], "CFLAG", [chara, "ＴＳ人化時会話類型"])
    _swap_var(ctx, "ABL", [chara, "Ｖ感覚"], "CFLAG", [chara, "元Ｖ感覚"])
    _swap_var(ctx, "CSTR", [chara, 16], "CSTR", [chara, 11])
    _swap_var(ctx, "CSTR", [chara, 17], "CSTR", [chara, 12])
    _ctx_set_var(ctx, "CFLAG", [chara, "現胸サイズ"], to_int(call_builtin(ctx, "BUST", [chara]) or 0))
    _swap_var(ctx, "CFLAG", [chara, "現胸サイズ"], "CFLAG", [chara, "元胸サイズ"])
    for name in ("絶壁", "貧乳", "巨乳", "爆乳", "魔乳"):
        _ctx_set_var(ctx, "TALENT", [chara, name], 0)
    size = bust if bust else to_int(_ctx_get_var(ctx, "CFLAG", [chara, "現胸サイズ"]))
    if size == 1:
        _ctx_set_var(ctx, "TALENT", [chara, "絶壁"], 1)
    elif size == 2:
        _ctx_set_var(ctx, "TALENT", [chara, "貧乳"], 1)
    elif size == 4:
        _ctx_set_var(ctx, "TALENT", [chara, "巨乳"], 1)
    elif size == 5:
        _ctx_set_var(ctx, "TALENT", [chara, "爆乳"], 1)
    elif size == 6:
        _ctx_set_var(ctx, "TALENT", [chara, "魔乳"], 1)
    return 0


def _position_name(pos: int) -> str:
    return f"ポジション{pos}"


def _position_chara(ctx, pos: int) -> int:
    return to_int(_ctx_get_var(ctx, "FLAG", [_position_name(pos)]))


def _remove_position(ctx, pos: int) -> int:
    chara = _position_chara(ctx, pos)
    if chara >= 0:
        _ctx_set_var(ctx, "CFLAG", [chara, "ポジション"], 0)
    _ctx_set_var(ctx, "FLAG", [_position_name(pos)], -1)
    return chara


def _insert_position(ctx, pos: int, chara: int) -> None:
    _ctx_set_var(ctx, "FLAG", [_position_name(pos)], chara)
    if chara >= 0:
        _ctx_set_var(ctx, "CFLAG", [chara, "ポジション"], pos)


def b_add_guest_companion(ctx, args):
    no = to_int(arg(args, 0, 0))
    loyalty = to_int(arg(args, 1, 0))
    reverse = truth(arg(args, 2, 0))
    order = range(6, 0, -1) if reverse else range(1, 7)
    pos = -1
    displaced = -1
    for p in order:
        if _position_chara(ctx, p) < 0:
            pos = p
            break
    if pos == -1:
        special = _csv_index(ctx, "キャラ", "梅亜麗")
        for p in order:
            ch = _position_chara(ctx, p)
            if ch == to_int(_ctx_get_var(ctx, "MASTER", [])):
                continue
            if ch >= 0 and (to_int(_ctx_get_var(ctx, "ABL", [ch, "種族"])) or to_int(_ctx_get_var(ctx, "NO", [ch])) == special):
                pos = p
                displaced = _remove_position(ctx, p)
                break
    is_new = 0
    if pos >= 0:
        chara = to_int(call_builtin(ctx, "GETCHARA", [no]) or -1)
        if chara >= 0:
            _ctx_set_var(ctx, "CFLAG", [chara, "この場に居ないフラグ"], 0)
        else:
            mem = _ctx_memory(ctx)
            chara = mem.add_chara(no) if hasattr(mem, "add_chara") else -1
            _ctx_set_var(ctx, "CFLAG", [chara, "忠誠度"], loyalty)
            is_new = 1
        _ctx_set_var(ctx, "CFLAG", [chara, "ゲスト加入フラグ"], 1)
        _insert_position(ctx, pos, chara)
        _set_return_values(ctx, pos, displaced, is_new)
        return pos
    _set_return_values(ctx, 0)
    return 0


def b_character_delete(ctx, args):
    chara = to_int(arg(args, 0, -1))
    if chara < 0:
        return 0
    # Clear direct position/target references before compacting character arrays.
    pos = to_int(_ctx_get_var(ctx, "CFLAG", [chara, "ポジション"]))
    if pos > 0:
        _remove_position(ctx, pos)
    for base in ("TARGET", "ASSI"):
        value = to_int(_ctx_get_var(ctx, base, []))
        if value == chara:
            _ctx_set_var(ctx, base, [], -1)
    if to_int(_ctx_get_var(ctx, "CFLAG", [chara, "PTフラグ"])) != 0:
        for slot in range(8):
            equip_name = to_str(call_builtin(ctx, "GET_EQUIP", [slot]) or "")
            item = to_int(_ctx_get_var(ctx, "EQUIP", [chara, equip_name]))
            if item > 0:
                _ctx_set_var(ctx, "ITEM", [item], to_int(_ctx_get_var(ctx, "ITEM", [item])) + 1)
                _ctx_set_var(ctx, "EQUIP", [chara, equip_name], 0)
    mem = _ctx_memory(ctx)
    if hasattr(mem, "del_charas"):
        mem.del_charas([chara])
    return 0


def b_del_guest(ctx, args):
    chara = to_int(arg(args, 0, 0))
    for slot in range(8):
        equip_name = to_str(call_builtin(ctx, "GET_EQUIP", [slot]) or "")
        item = to_int(_ctx_get_var(ctx, "EQUIP", [chara, equip_name]))
        if item > 0:
            _ctx_set_var(ctx, "ITEM", [item], to_int(_ctx_get_var(ctx, "ITEM", [item])) + 1)
            _ctx_set_var(ctx, "EQUIP", [chara, equip_name], 0)
    no = to_int(_ctx_get_var(ctx, "NO", [chara]))
    for slot in range(8):
        name = to_str(arg(args, slot + 1, ""))
        item = _csv_index(ctx, "ITEM", name) if name else to_int(call_builtin(ctx, "CSVEQUIP", [no, _csv_index(ctx, "EQUIP", to_str(call_builtin(ctx, "GET_EQUIP", [slot]) or "")), 0]) or 0)
        if not item:
            continue
        stock = to_int(_ctx_get_var(ctx, "ITEM", [item]))
        if stock > 0:
            _ctx_set_var(ctx, "ITEM", [item], max(stock - 1, 0))
        else:
            for ch in range(len(_ctx_memory(ctx).characters)):
                equip_name = to_str(call_builtin(ctx, "GET_EQUIP", [slot]) or "")
                if to_int(_ctx_get_var(ctx, "EQUIP", [ch, equip_name])) == item:
                    _ctx_set_var(ctx, "EQUIP", [ch, equip_name], 0)
                    break
    b_character_delete(ctx, [chara])
    return 0


def b_knockout(ctx, args):
    chara = to_int(arg(args, 0, 0))
    hp = to_int(_ctx_get_var(ctx, "BASE", [chara, "体力"]))
    if 1 <= hp <= 499:
        _set_return_values(ctx, 1)
        return 1
    if hp > 0:
        _set_return_values(ctx, 0)
        return 0
    if _talent(ctx, chara, "蓬莱人"):
        _ctx_set_var(ctx, "BASE", [chara, "体力"], 1)
        _set_return_values(ctx, 2)
        return 2
    obedience = to_int(_ctx_get_var(ctx, "ABL", [chara, "従順"]))
    if obedience > 0:
        _ctx_set_var(ctx, "ABL", [chara, "従順"], obedience - 1)
    stop = max(12, to_int(_ctx_get_var(ctx, "CFLAG", [chara, "体力回復停止フラグ"])) * 2)
    _ctx_set_var(ctx, "CFLAG", [chara, "体力回復停止フラグ"], stop)
    pain_mark = to_int(_ctx_get_var(ctx, "MARK", [chara, "苦痛刻印"]))
    _ctx_set_var(ctx, "CFLAG", [chara, "圧力値"], to_int(_ctx_get_var(ctx, "CFLAG", [chara, "圧力値"])) + stop * (pain_mark + 1))
    _ctx_set_var(ctx, "BASE", [chara, "体力"], 1)
    _ctx_set_var(ctx, "CFLAG", [chara, "労役フラグ"], 0)
    _set_return_values(ctx, 2)
    return 2


def b_base_incense(ctx, args):
    chara = to_int(arg(args, 0, 0))
    raw = ""
    if getattr(ctx, "inputs", None):
        raw = to_str(ctx._input(""))
    elif (
        not getattr(ctx, "interactive", False)
        and getattr(ctx, "had_explicit_inputs", False)
        and hasattr(ctx, "waiting_for_input")
    ):
        ctx.waiting_for_input = True
        return 0
    if raw == "":
        return 0
    choice = to_int(raw)
    if choice <= 0:
        return 0
    stat = to_str(call_builtin(ctx, "GET_BASESTATUS", [choice]) or "")
    item = choice + 1009
    if stat and to_int(_ctx_get_var(ctx, "ITEM", [item])) > 0:
        _ctx_set_var(ctx, "BASE", [chara, stat], to_int(_ctx_get_var(ctx, "BASE", [chara, stat])) + 1)
        _ctx_set_var(ctx, "ITEM", [item], to_int(_ctx_get_var(ctx, "ITEM", [item])) - 1)
    return 0


def b_lvup_booster(ctx, args):
    chara = to_int(arg(args, 0, 0))
    tries = max(0, to_int(arg(args, 1, 0)))
    stat_names = ["力", "知恵", "魔力", "耐力", "速度", "運"]
    install_names = ["赫拉克罗斯", "奧德修斯", "俄耳甫斯", "奥里翁", "阿卡琉斯速度神", "特修斯運勢神"]
    accessory_names = ["力之源", "知恵之源", "魔力之源", "活力之源", "速度之源", "運之源"]
    for _ in range(tries):
        for stat, install, accessory in zip(stat_names, install_names, accessory_names):
            applies = False
            if not truth(call_builtin(ctx, "IS_HUMAN", [chara]) or 0) and truth(call_builtin(ctx, "NUM_SUMMONER", []) or 0):
                applies = to_int(_ctx_get_var(ctx, "EQUIP", [to_int(_ctx_get_var(ctx, "MASTER", [])), install])) != 0
            if not applies:
                applies = to_int(_ctx_get_var(ctx, "EQUIP", [chara, "飾品"])) == _csv_index(ctx, "ITEM", accessory)
            if applies:
                _ctx_set_var(ctx, "MAXBASE", [chara, stat], to_int(_ctx_get_var(ctx, "MAXBASE", [chara, stat])) + 1)
                _ctx_set_var(ctx, "CFLAG", [chara, "能力強化回数"], to_int(_ctx_get_var(ctx, "CFLAG", [chara, "能力強化回数"])) + 1)
                _ctx_set_var(ctx, "CFLAG", [chara, stat + "強化回数"], to_int(_ctx_get_var(ctx, "CFLAG", [chara, stat + "強化回数"])) + 1)
                break
    return 0


def _lower_resist(ctx, chara: int, name: str, cap: int, step: int = 5) -> None:
    value = to_int(_ctx_get_var(ctx, "BASE", [chara, name]))
    if value > cap:
        _ctx_set_var(ctx, "BASE", [chara, name], max(cap, value - step))


def b_lvup_booster_magatama(ctx, args):
    chara = to_int(arg(args, 0, 0))
    tries = max(1, to_int(arg(args, 1, 1)))
    if not _talent(ctx, chara, "人修羅"):
        return 0
    equip = to_int(_ctx_get_var(ctx, "EQUIP", [chara, to_str(call_builtin(ctx, "GET_EQUIP", [6]) or "飾品")]))
    table = {
        8200: [("瀕死", -50, 5)], 8201: [("氷結", 50, 5)], 8202: [("破魔", 50, 5)],
        8203: [("精神", 50, 5)], 8204: [("火炎", 50, 5)], 8205: [("衝撃", 50, 5)],
        8206: [("剣撃", 75, 5), ("打撃", 75, 5)], 8207: [("電撃", 50, 5)],
        8208: [("呪殺", 25, 5)], 8209: [("氷結", 25, 5)], 8210: [("破魔", 25, 5)],
        8211: [("飛具", 50, 5), ("戦技", 50, 5)], 8212: [("破魔", 25, 10)],
        8213: [("神経", 25, 5)], 8214: [("神経", 25, 5), ("精神", 25, 5)],
        8215: [("火炎", 0, 10)], 8216: [("剣撃", 25, 5)],
    }
    for _ in range(tries):
        for name, cap, step in table.get(equip, []):
            _lower_resist(ctx, chara, name, cap, step)
    return 0


def _namedic_tables(ctx) -> dict[str, list[str]]:
    cache = getattr(ctx, "_namedic_tables_cache", None)
    if cache is not None:
        return cache
    tables: dict[str, list[str]] = {}
    root = getattr(getattr(ctx, "program", None), "root", None)
    if root is not None:
        base = root / "ERB" / "関数" / "組み込み関数" / "文字列生成" / "ランダムネーム"
        if base.exists():
            for path in base.glob("*.ERB"):
                try:
                    text = read_text_auto(path)
                except Exception:
                    continue
                m_name = re.search(r"^\s*@([^\(,\s]+)", text, re.M)
                if not m_name:
                    continue
                # Most NAMEDIC files are a single giant underscore-delimited
                # LOCALS assignment followed by RANDOM_SPLIT.  For the special
                # generators below, this still provides suffix/component lists.
                values: list[str] = []
                for raw in text.splitlines():
                    m = re.match(r"\s*LOCALS(?::\d+)?\s*=\s*(.*)$", raw)
                    if m:
                        values.extend([p for p in m.group(1).split("_") if p != ""])
                tables[norm_name(m_name.group(1))] = values
    try:
        setattr(ctx, "_namedic_tables_cache", tables)
    except Exception:
        pass
    return tables


def _namedic_random_katakana(ctx, count: int, size: int) -> list[str]:
    syllables = _namedic_tables(ctx).get(norm_name("NAMEDIC_隨機カタ卡娜"), [])
    if not syllables:
        syllables = list("アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワ")
    out: list[str] = []
    for _ in range(max(0, count)):
        for _attempt in range(20):
            text = "".join(random.choice(syllables) for _ in range(max(1, size)))
            if not text.startswith(("ン", "ー")):
                break
        out.append(text)
    return out


def _namedic_sample(ctx, key: str, count: int, size: int) -> list[str]:
    tables = _namedic_tables(ctx)
    if key == norm_name("NAMEDIC_隨機カタ卡娜"):
        return _namedic_random_katakana(ctx, count, size)
    if key == norm_name("NAMEDIC_隨機女性名"):
        suffixes = ["", "アンヌ", "シア", "リーナ", "ザード", "ティ", "ハール", "ラン", "リン", "ルク"]
        return [base + random.choice(suffixes) for base in _namedic_random_katakana(ctx, count, random.randrange(1, 4))]
    if key == norm_name("NAMEDIC_姫君風／中世日本"):
        base_names = _namedic_sample(ctx, norm_name("NAMEDIC_日本人の考える女性的で古風な名前"), count, size)
        suffixes = ["姫", "子", "御前"]
        return [name + random.choice(suffixes) for name in base_names]
    if key == norm_name("NAMEDIC_姫君風／近代日本"):
        base_names = _namedic_sample(ctx, norm_name("NAMEDIC_日本人の考える女性的で古風な名前"), count, size)
        suffixes = ["絵", "恵", "江", "佳", "華", "花", "香", "子", "奈", "音", "葉", "美", "代", "理"]
        return [name + random.choice(suffixes) for name in base_names]
    if key == norm_name("NAMEDIC_姫君風／中国伝奇"):
        # The ERB combines one prefix and one virtue/title component.
        first = ["永", "華", "恭", "月", "光", "沙", "慈", "順", "昭", "正", "太", "美", "陽", "安", "秀", "春", "夏", "秋", "冬"]
        second = ["穏", "貴", "輝", "興", "恵", "真", "仁", "智", "天", "徳", "明", "蘭", "典", "慶", "麗", "文", "華"]
        return [random.choice(first) + random.choice(second) + "公主" for _ in range(max(0, count))]
    values = tables.get(key, [])
    if not values:
        return ["" for _ in range(max(0, count))]
    pool = list(values)
    random.shuffle(pool)
    if count <= len(pool):
        return pool[:count]
    return [random.choice(values) for _ in range(max(0, count))]


def b_namedic(ctx, args):
    key = norm_name(getattr(ctx, "_builtin_call_name", ""))
    count = to_int(arg(args, 0, 1))
    size = to_int(arg(args, 1, 1))
    if count <= 0:
        return ""
    results = _namedic_sample(ctx, key, count, size)
    for i, value in enumerate(results):
        _ctx_set_var(ctx, "RESULTS", [i], value)
    first = results[0] if results else ""
    _ctx_set_var(ctx, "RESULTS", [], first)
    return first


BUILTINS: dict[str, Callable] = {
    "__CONST__": b_const,
    "ABS": b_abs, "SIGN": b_sign, "MAX": b_max, "MIN": b_min, "POWER": b_power, "SQRT": b_sqrt, "CBRT": b_cbrt, "LOG": b_log, "LOG10": b_log10, "EXPONENT": b_exponent,
    "LIMIT": b_limit, "INRANGE": b_inrange, "RANGE": b_range, "RAND": b_rand,
    "GETBIT": b_getbit, "SETBIT": b_setbit, "CLEARBIT": b_clearbit, "INVERTBIT": b_invertbit,
    "TOINT": b_toint, "TOSTR": b_tostr, "ISNUMERIC": b_isnumeric,
    "STRLEN": b_strlen, "STRLENS": b_strlens, "STRLENFORM": b_strlenform,
    "STRLENU": b_strlenu, "STRLENSU": b_strlensu, "STRLENFORMU": b_strlenformu,
    "SUBSTRING": b_substring, "SUBSTRINGU": b_substringu, "STRFIND": b_strfind, "STRFINDU": b_strfindu, "STRCOUNT": b_strcount,
    "REPLACE": b_replace, "UNICODE": b_unicode, "ESCAPE": b_escape,
    "STRFORM": b_strform, "STRJOIN": b_strjoin, "REGEXPMATCH": b_regexpmatch,
    "MATCH": b_match, "GROUPMATCH": b_groupmatch, "EQUALCHECK": b_equalcheck,
    "EQUALCHECK_TURN": b_equalcheck_turn, "EQUALCHECK_STR": b_equalcheck_str, "TRUECHECK": b_truecheck,
    "NOSAMES": b_nosames, "ALLSAMES": b_allsames,
    "GETNUM": b_getnum,
    "GET_BASESTATUS": b_get_basestatus, "GET_BASESTATUS_NUM": b_get_basestatus_num,
    "GET_BATTLESTATUS": b_get_battlestatus, "GET_BATTLESTATUS_NUM": b_get_battlestatus_num,
    "GET_TYPE": b_get_type, "GET_TYPE_NUM": b_get_type_num, "GET_STATE": b_get_state, "GET_STATE_NUM": b_get_state_num,
    "GET_EQUIP": b_get_equip, "GET_EQUIPNUM": b_get_equipnum, "GET_SUCCESSION": b_get_succession, "GET_SUCCESSION_NUM": b_get_succession_num,
    "GET_ALI1": b_get_ali1, "GET_ALI2": b_get_ali2, "GET_RANGE": b_get_range, "GET_SPHERE": b_get_sphere, "GET_GUNTYPE": b_get_guntype,
    "COEFFICIENT_EXP": b_coefficient_exp, "COEFFICIENT_MAG": b_coefficient_mag, "COEFFICIENT_MONEY": b_coefficient_money,
    "DIVERGENCE": b_divergence, "EQUIPSKILLNUM": b_equipskillnum,
    "GET_DITEMTYPE": b_get_ditemtype, "GET_DITEMTYPE_NUM": b_get_ditemtype_num, "PERSONA": b_persona_slot,
    "GET_PERSONA_NAME": b_get_persona_name, "現在のPERSONA": b_current_persona,
    "PERSONA資料": b_persona_data, "装備PERSONA資料": b_equipped_persona_data, "PERSONA編集": b_persona_edit,
    "GET_JOB": b_get_job, "GET_JOB_OMIT": b_get_job_omit,
    "GET_MANTRA": b_get_mantra, "GET_MANTRA_NUM": b_get_mantra_num, "GET_MANTRA_MAPNAME": b_get_mantra_mapname,
    "GET_EX": b_get_ex,
    "GET_RACE": b_get_race, "GET_RACE_NUM": b_get_race_num,
    "種族名": b_race_name, "CSV種族名": b_csv_race_name,
    "GET_傷害タイプ": b_get_damage_type, "GET_傷害タイプ_NUM": b_get_damage_type_num,
    "GET_攻撃タイプ": b_get_attack_type, "GET_攻撃タイプ_NUM": b_get_attack_type_num,
    "PALAMLV_F": b_palamlv_f, "PLUGINNAME": b_pluginname,
    "GET_STAIN": b_get_stain, "SET_STAIN": b_set_stain, "MOVE_STAIN": b_move_stain, "DIRTY": b_dirty,
    "INI": b_ini, "FLAG_RESET": b_flag_reset, "SET_COMFLAG": b_set_comflag, "SET_NEXTTRAIN": b_set_nexttrain,
    "SET_KOJO_FUNCTION_CFLAG": b_set_kojo_function_cflag, "GET_COMFLAG": b_get_comflag, "GET_EVENTFLAG": b_get_eventflag,
    "CINI": b_cini, "GET_CHARASELLABLE": b_get_charasellable, "GET_MARK_WAY": b_get_mark_way,
    "GET_RELATION": b_get_relation, "GET_RELATION_GROUP": b_get_relation_group,
    "IS_RELATION_GROUP": b_is_relation_group, "VIDEO_COM_INCLUDE_CFLAG": b_video_com_include_cflag,
    "VIDEO_COM_INCLUDE_TCVAR": b_video_com_include_tcvar,
    "ONCERAND": b_oncerand,
    "キャラ絆確かめ": b_chara_bond_check, "キャラ存在確かめ": b_chara_exists_check,
    "EVENT_10_2人掛け合いチェック": b_event_10_two_chara_check,
    "ONCEDAY": b_onceday, "ONCETURN": b_onceturn, "ONCEPLAY": b_onceplay, "EVENTTURNEND": b_eventturnend, "WEEKDAY": b_weekday,
    "EXIST_ITEM": b_exist_item, "CSV配偶者": b_csv_spouse,
    "IS_EROEQUIP_F": b_is_eroequip_f, "ITEM_USE_REQUIREMENT": b_item_use_requirement,
    "PLAY_ANALSEX": b_play_analsex, "PLAY_CUNNI": b_play_cunni, "PLAY_FELLA": b_play_fella,
    "PLAY_KISS": b_play_kiss, "PLAY_SEX": b_play_sex, "近親チェック": b_kinship_check,
    "EVENT_SETBIT": b_event_setbit, "EVENT_CLEARBIT": b_event_clearbit, "EVENT_INVERTBIT": b_event_invertbit,
    "EVENT_GETBIT": b_event_getbit, "EVENT_KEYWORD": b_event_keyword,
    "TRAIN_SETBIT": b_train_setbit, "TRAIN_GETBIT": b_train_getbit,
    "卑語_おちん": b_dirty_word_ochin, "卑語_陰茎": b_dirty_word_penis, "卑語_精液": b_dirty_word_semen,
    "INPUTINT": b_inputint, "TINPUTINT": b_tinputint, "INPUT_CHAR": b_input_char,
    "INPUT_MANY": b_input_many, "INPUT_ONEKEY_TAP": b_input_onekey_tap,
    "INPUT_ONEKEY_TAP_RESULTS": b_input_onekey_tap_results, "INPUT_SELECT": b_input_select,
    "INPUT_SPLIT": b_input_split, "INPUT_YN": b_input_yn,
    "INPUT_SELECT_D": b_input_select_d, "INPUT_SELECT_M": b_input_select_m,
    "INPUT_YN_D": b_input_yn_d, "INPUT_YN_M": b_input_yn_m,
    "GET_DEVIL": b_get_devil, "GET_NEXT_EXP": b_get_next_exp,
    "CONVERT_BADSTATE_NAME": b_convert_badstate_name,
    "ACTIONABLE_CHARA": b_actionable_chara_f, "ACTIONABLE_CHARA_F": b_actionable_chara_f,
    "BTL_NO": b_btl_no, "GET_BTL_RANGE": b_get_btl_range,
    "GET_POS_MIN": b_get_pos_min, "GET_POS_MAX": b_get_pos_max, "GET_WEAKNESS": b_get_weakness,
    "IS_BADSTATE": b_is_badstate, "IS_FRIEND": b_is_friend, "IS_FRONT": b_is_front, "IS_TARGET_ABLE": b_is_target_able,
    "ATTACK_MIN_HP": b_attack_min_hp, "MEMORIZE_WEAKNESS": b_memorize_weakness, "CHECK_WEAKNESS": b_check_weakness,
    "GET_SUMMONER_LV": b_get_summoner_lv, "GET_SUMMONER_MLV": b_get_summoner_mlv,
    "NUM_SUMMONER": b_num_summoner, "NUM_HAVESKILL": b_num_haveskill,
    "陥落": b_fallen, "恋慕": b_renbo, "親愛": b_shinai, "淫乱": b_inran, "娼婦": b_shoufu,
    "服従": b_fukujuu, "隷属": b_reizoku, "信頼": b_shinrai, "相棒": b_aibou,
    "契約": b_contract,
    "ハート": b_heart, "ハートＢ": b_heart_b, "COMTYPE": b_comtype,
    "IS_MALE": b_is_male, "IS_LOOKSLIKE_MALE": b_is_lookslike_male, "HAVE_PENIS": b_have_penis,
    "IS_LESBIAN": b_is_lesbian, "IS_GAY": b_is_gay,
    "HAVE_CLITORIS": b_have_clitoris, "HAVE_TIT": b_have_tit, "HAVE_VAGINA": b_have_vagina,
    "IS_BEAST": b_is_beast, "IS_BITCHY": b_is_bitchy, "IS_ENGAGE": b_is_engage,
    "IS_HUMAN": b_is_human, "IS_LOVER": b_is_lover, "IS_SLAVERY": b_is_slavery, "XGENDER": b_xgender,
    "HATE": b_hate, "HATE_MALE": b_hate_male, "HATE_FEMALE": b_hate_female,
    "GET_调和者出力": b_harmonizer_output, "体格": b_body_size, "体格差": b_body_size_diff,
    "初期性別参照": b_initial_gender, "純異能者チェック": b_pure_innate,
    "純達人チェック": b_pure_tatsujin, "貞操": b_chastity,
    "GET_ADD_EXP": b_get_add_exp, "GETS_ADD_EXP": b_gets_add_exp,
    "ITEM_ANUS": b_item_anus, "ITEM_FOOT": b_item_foot, "ITEM_HAND": b_item_hand,
    "ITEM_NIPLE": b_item_niple, "ITEM_PENIS": b_item_penis, "ITEM_VAGINA": b_item_vagina,
    "USE_ANUS": b_use_anus, "USE_BREAST": b_use_breast, "USE_CLI": b_use_cli, "USE_EYE": b_use_eye,
    "USE_FOOT": b_use_foot, "USE_HAND": b_use_hand, "USE_HEAD": b_use_head, "USE_MOUTH": b_use_mouth,
    "USE_NIPLE": b_use_niple, "USE_PBAND": b_use_pband, "USE_PENIS": b_use_penis,
    "USE_TAIL": b_use_tail, "USE_VAGINA": b_use_vagina, "USE_WING": b_use_wing,
    "FAVORITE": b_favorite, "FAVORITE_ID": b_favorite_id, "FAVORITE_1": b_favorite_1, "FAVORITE_1_ID": b_favorite_1_id,
    "SKILL_CHANGE": b_skill_change,
    "GET_WEAPON_TYPE": b_get_weapon_type, "GET_WEAPON_TYPE_NUM": b_get_weapon_type_num,
    "SKILL_NAME_F": b_skill_name_f, "SKILL_NUM_F": b_skill_num_f,
    "PU_NUM": b_pu_num, "GET_PU_SKILL_CSTR": b_get_pu_skill_cstr, "PU_SKILLNUM_GET": b_pu_skillnum_get,
    "PU_SKILL_CHECK": b_pu_skill_check, "IS_PU_SKILL": b_is_pu_skill, "HAVE_PU_SKILL": b_have_pu_skill,
    "PUEQ_NUM_CHECK": b_pueq_num_check, "PUEQ_NUM_GET": b_pueq_num_get, "PUEQ_NAME_GET": b_pueq_name_get, "PUEQ_NAME_GETS": b_pueq_name_gets,
    "ADD_STRFLAG": b_add_strflag, "DEL_STRFLAG": b_del_strflag, "SWAP_STRFLAG": b_swap_strflag,
    "CHANGE_STRFLAG_NUM": b_change_strflag_num,
    "STRFLAG_D": b_strflag_d, "STRFLAG_EV": b_strflag_ev, "STRFLAG_CLO": b_strflag_clo, "STRFLAG_REQ": b_strflag_req,
    "STRFLAG_NUM_D": b_strflag_num_d, "STRFLAG_NUM_EV": b_strflag_num_ev,
    "STRFLAG_NUM_COL": b_strflag_num_col, "STRFLAG_NUM_REQ": b_strflag_num_req,
    "CSTRFLAG_NUM": b_cstrflag_num, "TSTRFLAG_NUM": b_tstrflag_num,
    "STRFLAG_NUM_CPD_FIND": b_strflag_num_cpd_find,
    "GET_CPD_STRFLAG": b_get_cpd_strflag, "GET_CPD_STRFLAG_NUM": b_get_cpd_strflag_num,
    "GET_CPD_SAVESTR_NUM": b_get_cpd_savestr_num,
    "現HP割合": b_current_hp_rate, "現MP割合": b_current_mp_rate, "傷害割合": b_damage_rate, "危険日": b_danger_day,
    "CHARA_SKILLCOUNT": b_chara_skillcount, "CHARA_SKILLCOUNT_技能操作用": b_chara_skillcount_for_ops,
    "HAVE_SKILL": b_have_skill, "HAVE_SKILL_C": b_have_skill_c, "HAVE_SKILL_OVERLAP": b_have_skill_overlap,
    "CHECK_SKILL": b_check_skill, "CHECK_SKILL_OVERLAP": b_check_skill_overlap,
    "BUST": b_bust,
    "CHECK_SKILL_SEARCH": b_check_skill_search, "HAVE_SKILL_SEARCH": b_have_skill_search, "_SKILL_CHECK": b__skill_check,
    "CHECK_SKILL_SEARCH2": b_check_skill_search2, "HAVE_SKILL_SEARCH2": b_have_skill_search2, "_SKILL_CHECK2": b__skill_check2,
    "SEARCH_SKILL_FUNCTION": b_search_skill_function, "MULTI_SEARCH_SKILL_FUNCTION": b_multi_search_skill_function,
    "SKILL_TIMING": b_skill_timing, "VAR_REGEN": b_var_regen, "VAR_REGENABLE_CHECK": b_var_regenable_check, "VAR_KAJA": b_var_kaja,
    "SKILLGAGE_NUM": b_skillgage_num,
    "SKILLGAGE_H_GET": b_skillgage_h_get, "SKILLGAGE_D_GET": b_skillgage_d_get, "SKILLGAGE_F_GET": b_skillgage_f_get,
    "SKILLGAGE_H_GETBIT": b_skillgage_h_getbit, "SKILLGAGE_D_GETBIT": b_skillgage_d_getbit, "SKILLGAGE_F_GETBIT": b_skillgage_f_getbit,
    "CSVBASE": b_csvbase, "CSVABL": b_csvabl, "CSVTALENT": b_csvtalent,
    "CSVCFLAG": b_csvcflag, "CSVEQUIP": b_csvequip, "CSVEXP": b_csvexp, "CSVMARK": b_csvmark, "CSVRELATION": b_csvrelation, "CSVCHARANUM": b_csvcharanum,
    "CSVNAME": b_csvname, "CSVCALLNAME": b_csvcallname, "CSVNICKNAME": b_csvnickname, "CSVMASTERNAME": b_csvmastername, "CSVCSTR": b_csvcstr,
    "ADD_EXP": b_add_exp, "ADDS_EXP": b_adds_exp, "ADD_GUEST_COMPANION": b_add_guest_companion,
    "DEL_GUEST": b_del_guest, "キャラ削除": b_character_delete, "気絶処理": b_knockout,
    "SET_BATTLE_STATUS": b_set_battle_status, "SET_EVENTFLAG": b_set_eventflag, "SET_RELATION": b_set_relation,
    "SET_SEX": b_set_sex, "ＴＳ処理": b_ts_process, "初ＴＳ処理": b_initial_ts,
    "BASE_INCENSE": b_base_incense, "LVUP_BOOSTER": b_lvup_booster, "LVUP_BOOSTER_MAGATAMA": b_lvup_booster_magatama,
    "GET_CLOTHES": b_get_clothes, "GET_CLOTHESNAME": b_get_clothesname, "NAME_EXPOSE": b_name_expose,
    "CLOTHESNAMEF": b_clothesnamef, "CLOTHES_NAME": b_clothes_name, "CLOTHES_INITIALIZE": b_clothes_initialize,
    "CHECK_EXP": b_check_exp, "CHECK_EXPOSE": b_check_expose, "CHECK_SOURCE": b_check_source,
    "SET_CLOTHES_DROP_ALL": b_set_clothes_drop_all,
    "SET_CLOTHES_DROP_BOTTOMS": b_set_clothes_drop_bottoms,
    "SET_CLOTHES_DROP_INNER": b_set_clothes_drop_inner,
    "SET_CLOTHES_DROP_OUTER": b_set_clothes_drop_outer,
    "SET_CLOTHES_DROP_TOPS": b_set_clothes_drop_tops,
    "SET_CLOTHES_EQUIP_ALL": b_set_clothes_equip_all,
    "SET_CLOTHES_NAKED_BREAST": b_set_clothes_naked_breast,
    "SET_CLOTHES_NAKED_CROTCH": b_set_clothes_naked_crotch,
    "おっぱいオープンチェック": b_breast_open_check, "股間構造チェック": b_crotch_structure_check,
    "触覚チェック": b_touch_check, "ずらしチェック": b_shift_check,
    "FINDCHARA": b_findchara, "GETCHARA": b_getchara, "GETSPCHARA": b_getspchara, "FINDCHARA_ID": b_findchara_id,
    "FINDCHARA_B": b_findchara_b, "FINDCHARA_M": b_findchara_m, "FINDCHARA_NO_C": b_findchara_no_c,
    "FINDCHARA_ENEMY": b_findchara_enemy, "FINDLASTCHARA": b_findlastchara,
    "CHARANUM_CHECK": b_charanum_check, "CHARANUM_DIGIT": b_charanum_digit, "SUBPLAYER": b_subplayer,
    "NUM_NAKAMA": b_num_nakama, "NUM_NAKAMA_HEADCOUNT": b_num_nakama_headcount, "ＣＯＭＰ空き容量": b_comp_empty_capacity,
    "NUM_FUSIONABLE": b_num_fusionable, "NUM_ZOUMA": b_num_zouma,
    "POS": b_pos, "CPOS": b_cpos, "SUMARRAY": b_sumarray, "SUMCARRAY": b_sumcarray, "MAXARRAY": b_maxarray,
    "MINARRAY": b_minarray, "MAXCARRAY": b_maxcarray, "MINCARRAY": b_mincarray, "INRANGEARRAY": b_inrangearray, "INRANGECARRAY": b_inrangecarray, "FINDELEMENT": b_findelement, "FINDLASTELEMENT": b_findlastelement,
    "PRINTCPERLINE": b_printcperline, "PRINTCLENGTH": b_printclength, "SAVENOS": b_savenos, "BARSTR": b_barstr, "GETMILLISECOND": b_getmillisecond, "GETTIME": b_gettime, "GETTIMES": b_gettimes, "GETSECOND": b_getsecond,
    "CLIENTWIDTH": b_clientwidth, "CLIENTHEIGHT": b_clientheight, "EXISTFILE": b_existfile, "ENUMFILES": b_enumfiles, "EXISTSOUND": b_existsound, "UPDATECHECK": b_updatecheck, "GETMEMORYUSAGE": b_getmemoryusage, "CLEARMEMORY": b_clearmemory,
    "GETDISPLAYLINE": b_getdisplayline, "BITMAP_CACHE_ENABLE": b_bitmap_cache_enable,
    "GETKEY": b_getkey, "GETKEYTRIGGERED": b_getkeytriggered, "MOUSEX": b_mousex, "MOUSEY": b_mousey, "MOUSEB": b_mouseb, "ISACTIVE": b_isactive,
    "MONEYSTR": b_moneystr, "CHKDATA": b_chkdata, "GETCOLOR": b_getcolor, "GETDEFCOLOR": b_getdefcolor, "GETBGCOLOR": b_getbgcolor, "GETDEFBGCOLOR": b_getdefbgcolor, "GETFOCUSCOLOR": b_getfocuscolor,
    "COLOR": b_color, "COLOR_FROMRGB": b_color_fromrgb, "COLORFROMRGB": b_color_fromrgb,
    "GETCOLOR_9": b_getcolor_9, "MESSAGE_B": b_message_b, "MESSAGE_BL": b_message_bl,
    "MESSAGE_B2": b_message_b2, "MESSAGE_P": b_message_p, "MESSAGE_P2": b_message_p2,
    "MESSAGE_COMP_OVER": b_message_comp_over, "SET_AISYOU_COLOR": b_set_aisyou_color,
    "SHOW_AISYOU_COLOR_LIST": b_show_aisyou_color_list, "TOSTR1000": b_tostr1000,
    "MESSAGE_WINDOW": b_message_window, "MESSAGE_WINDOW_D": b_message_window_d,
    "MESSAGE_WINDOW_LOG": b_message_window_log, "MESSAGE_WINDOW_CONFIG": b_message_window_config,
    "NOWALIGNMENT": b_nowalignment, "PREVALIGNMENT": b_prevalignment, "SET_ALIGNMENT": b_set_alignment,
    "SHOW_PICTURE": b_show_picture, "SHOW_FORCEMOVE": b_show_forcemove,
    "WRITE_IMG": b_write_img, "GET_IMG_TYPE": b_get_img_type, "SHOW_IMG": b_show_img,
    "EXIST_PICTURE": b_exist_picture, "顔グラ追加": b_face_graphic_add,
    "ANATANAME": b_anataname, "BARCOLORSET": b_barcolorset, "PRINT_COLOR": b_print_color,
    "BTL_COLOR_TABLE": b_btl_color_table, "BTL_COLOR_TABLE_NUM": b_btl_color_table_num,
    "TOSTR_HTML": b_tostr_html, "COLORDRAWLINE": b_colordrawline,
    "PRINTFORM_LF": b_printform_lf, "PRINT_COLORBAR": b_print_colorbar,
    "PRINT_EIGHT_BAR": b_print_eight_bar, "PRINT_STR": b_print_str,
    "PRINT_STR_F": b_print_str_f, "PRINT_STRL": b_print_strl, "PRINT_STRW": b_print_strw,
    "PRINT_STR_INPUT": b_print_str_input, "PRINT_STR_INPUTS": b_print_str_inputs,
    "HEARTMARK": b_heartmark, "WHITE_HEARTMARK": b_white_heartmark, "BIG_HEARTMARK": b_big_heartmark,
    "HEART": b_heart_cmd, "HEARTB": b_heartb_cmd, "HEARTW": b_heartw_cmd, "HEARTBW": b_heartbw_cmd,
    "HEARTD": b_heartd_cmd, "HEARTDB": b_heartdb_cmd, "HEARTDW": b_heartdw_cmd, "HEARTDBW": b_heartdbw_cmd,
    "TOALIGNMENT": b_toalignment,
    "GLOBAL_BADEND_INIT": b_global_badend_init, "GLOBAL_BADEND_SET": b_global_badend_set,
    "GLOBAL_BADEND_GET": b_global_badend_get, "GLOBAL_BADEND_DISP_BADENDLIST": b_global_badend_disp_badendlist,
    "SHOPCOMABLE_700": b_shopcomable_700, "SHOP_COM_700": b_shop_com_700,
    "SHOPCOMABLE_701": b_shopcomable_701, "SHOP_COM_701": b_shop_com_701,
    "GET_STATE_KANJI": b_get_state_kanji, "STATE_COLOR": b_state_color,
    "CHANGE_MS_TO_HHMISS": b_change_ms_to_hhmiss, "耐性一文字": b_resist_one_char,
    "ENEMY_COUNT": b_enemy_count, "ADD_KMGT": b_add_kmgt, "S_NAME": b_s_name,
    "IS_RANDOMCHARA": b_is_randomchara, "LIFTING_A_BAN": b_lifting_a_ban,
    "AION式召喚術_技能枠判定": b_aion_skill_slot_check, "AION式召喚術_人間時技能反映": b_aion_human_skill_reflect,
    "SKILL_EQUIPTHEORY_IS_HAVE_SKILL": b_skill_equiptheory_is_have_skill,
    "SKILL_EQUIPTHEORY_IS_SKILL_EQUIPTHEORY": b_skill_equiptheory_is_skill,
    "SKILL_EQUIPTHEORY_DEL_SKILL": b_skill_equiptheory_del_skill,
    "SKILL_EQUIPTHEORY_EQUIP_STATUS": b_skill_equiptheory_equip_status,
    "SKILL_EQUIPTHEORY_EQUIP_HIT": b_skill_equiptheory_equip_hit,
    "MATCHING_WEAPON_CHECK": b_matching_weapon_check, "WEAPON_STYLE_CHECK": b_weapon_style_check,
    "WEAPON_CHECK_MIX": b_weapon_check_mix, "GET_CHARAPARAM": b_get_charaparam,
    "子宮最大容量初期設定": b_womb_capacity_init, "IS_ANTI_NTR_CLOTHES": b_is_anti_ntr_clothes,
    "AUTO_SPLIT": b_autosplit, "AUTO_SPLIT_INT": b_autosplit_int, "AUTO_SPLIT_NUM": b_autosplit_num,
    "ADD_SPLIT": b_add_split, "CHANGE_SPLIT": b_change_split, "CALC_SPLIT": b_calc_split,
    "RANDOM_SPLIT": b_random_split, "SHIFT_SPLIT": b_shift_split, "TEXTR": b_text_random, "RAND_SPLIT": b_rand_split,
    "LINEISEMPTY": b_lineisempty, "GETLINESTR": b_getlinestr, "TOFULL": b_tofull, "TOHALF": b_tohalf,
    "CHARATU": b_charatu, "CURRENTALIGN": b_currentalign, "CURRENTREDRAW": b_currentredraw, "GETCONFIG": b_getconfig, "GETCONFIGS": b_getconfigs,
    "GETFONT": b_getfont, "GETSTYLE": b_getstyle, "CHKFONT": b_chkfont, "MOUSESKIP": b_mouseskip, "ISSKIP": b_isskip, "MESSKIP": b_messkip, "GCREATED": b_gcreated,
    "SPRITECREATED": b_spritecreated, "SPRITEWIDTH": b_spritewidth, "SPRITEHEIGHT": b_spriteheight,
    "GWIDTH": b_gwidth, "GHEIGHT": b_gheight, "GCREATEFROMFILE": b_gcreatefromfile,
    "EQUIP_DETAIL_ITEM_LIST": b_equip_detail_item_list,
    "LIST_SET": b_list_set, "LIST_ADD": b_list_add, "LIST_ADDLIST": b_list_addlist,
    "LIST_GET": b_list_get, "LIST_COUNT": b_list_count, "LIST_INSERTAT": b_list_insertat,
    "LIST_REMOVEAT": b_list_removeat, "LIST_INDEXOF": b_list_indexof,
    "LIST_REMOVEALL": b_list_removeall, "LIST_SORT": b_list_sort, "LIST_SORT_R": b_list_sort_r,
    "LIST_FOREACH": b_list_foreach,
    "DIC_SET": b_dic_set, "DIC_CONTAINSKEY": b_dic_containskey, "DIC_REMOVE": b_dic_remove,
    "DIC_GET": b_dic_get, "DIC_COUNT": b_dic_count, "DIC_FOREACH": b_dic_foreach,
    "HTML_GETPRINTEDSTR": b_html_getprintedstr, "HTML_POPPRINTINGSTR": b_html_popprintingstr,
    "HTML_ESCAPE": b_html_escape, "HTML_TOPLAINTEXT": b_html_toplaintext,
    "HTML_STRINGLEN": b_html_stringlen, "HTML_SUBSTRING": b_html_substring, "HTML_STRINGLINES": b_html_stringlines,
    "CMATCH": b_cmatch, "CONVERT": b_convert, "GETPALAMLV": b_getpalamlv, "GETEXPLV": b_getexplv,
    "ISDEFINED": b_isdefined, "EXISTVAR": b_existvar, "EXISTFUNCTION": b_existfunction,
    "ENUMFUNCBEGINSWITH": b_enumfunc, "ENUMFUNCENDSWITH": b_enumfunc, "ENUMFUNCWITH": b_enumfunc,
    "ENUMVARBEGINSWITH": b_enumvar, "ENUMVARENDSWITH": b_enumvar, "ENUMVARWITH": b_enumvar,
    "ENUMMACROBEGINSWITH": b_enummacro, "ENUMMACROENDSWITH": b_enummacro, "ENUMMACROWITH": b_enummacro,
    "GETVAR": b_getvar, "GETVARS": b_getvars, "SETVAR": b_setvar, "VARSETEX": b_varsetex, "ARRAYMSORTEX": b_arraymsortex,
    "VARSIZE": b_varsize, "ERDNAME": b_erdname, "EXISTCSV": b_existcsv, "TOUPPER": b_toupper, "TOLOWER": b_tolower,
    "ANALYZE_COUNT_CHECK": b_analyze_count_check,
    "INIS": b_inis, "POSS": b_poss, "SKILLNUM": b_skillnum,
    "NAMEDIC_アラブ人女性": b_namedic, "NAMEDIC_アラブ人男性": b_namedic,
    "NAMEDIC_イタリア人女性": b_namedic, "NAMEDIC_イタリア人男性": b_namedic,
    "NAMEDIC_インド人女性": b_namedic, "NAMEDIC_インド人男性": b_namedic,
    "NAMEDIC_ギリシャ人女性": b_namedic, "NAMEDIC_ギリシャ人男性": b_namedic,
    "NAMEDIC_スウェーデン人女性": b_namedic, "NAMEDIC_スウェーデン人男性": b_namedic,
    "NAMEDIC_スペイン人女性": b_namedic, "NAMEDIC_スペイン人男性": b_namedic,
    "NAMEDIC_ドイツ人女性": b_namedic, "NAMEDIC_ドイツ人男性": b_namedic,
    "NAMEDIC_フィンランド人女性": b_namedic, "NAMEDIC_フィンランド人男性": b_namedic,
    "NAMEDIC_芙蘭ス人女性": b_namedic, "NAMEDIC_芙蘭ス人男性": b_namedic,
    "NAMEDIC_隨機カタ卡娜": b_namedic, "NAMEDIC_隨機女性名": b_namedic,
    "NAMEDIC_ロシア人女性": b_namedic, "NAMEDIC_ロシア人男性": b_namedic,
    "NAMEDIC_中国人女性": b_namedic, "NAMEDIC_中国人男性": b_namedic,
    "NAMEDIC_姫君風／中世日本": b_namedic, "NAMEDIC_姫君風／中国伝奇": b_namedic,
    "NAMEDIC_姫君風／近代日本": b_namedic, "NAMEDIC_日本人の考える女性的で古風な名前": b_namedic,
    "NAMEDIC_日本人女性": b_namedic, "NAMEDIC_日本人男性": b_namedic, "NAMEDIC_日本人苗字": b_namedic,
    "NAMEDIC_百家姓": b_namedic, "NAMEDIC_英語圏の女性": b_namedic, "NAMEDIC_英語圏の男性": b_namedic,
    "魔晶装備": b_magic_equipment,
    "改造装備": b_remodel_equipment,
    "改造装備番号": b_remodel_equipment_index,
    "改造装備物品ナンバー": b_remodel_equipment_number,
}

for _suffix in _SETTING_BITS:
    BUILTINS[f"SETTING_IS_{_suffix}"] = b_setting_switch
    BUILTINS[f"SETTING_SET_{_suffix}"] = b_setting_switch
    BUILTINS[f"SETTING_INVERT_{_suffix}"] = b_setting_switch

for _suffix in _BATTLE_SETTING_BITS:
    BUILTINS[f"BATTLE_SETTING_IS_{_suffix}"] = b_battle_setting_switch
    BUILTINS[f"BATTLE_SETTING_SET_{_suffix}"] = b_battle_setting_switch
    BUILTINS[f"BATTLE_SETTING_INVERT_{_suffix}"] = b_battle_setting_switch

BUILTINS.update({
    "SKILLGAGE_H_SET": b_skillgage_h_set, "SKILLGAGE_D_SET": b_skillgage_d_set, "SKILLGAGE_F_SET": b_skillgage_f_set,
    "SKILLGAGE_H_ADD": b_skillgage_h_add, "SKILLGAGE_D_ADD": b_skillgage_d_add, "SKILLGAGE_F_ADD": b_skillgage_f_add,
    "SKILLGAGE_H_CALCULATION": b_skillgage_h_calculation, "SKILLGAGE_D_CALCULATION": b_skillgage_d_calculation, "SKILLGAGE_F_CALCULATION": b_skillgage_f_calculation,
    "SKILLGAGE_H_SETBIT": b_skillgage_h_setbit, "SKILLGAGE_D_SETBIT": b_skillgage_d_setbit, "SKILLGAGE_F_SETBIT": b_skillgage_f_setbit,
    "SKILLGAGE_H_CLEARBIT": b_skillgage_h_clearbit, "SKILLGAGE_D_CLEARBIT": b_skillgage_d_clearbit, "SKILLGAGE_F_CLEARBIT": b_skillgage_f_clearbit,
    "SKILLGAGE_H_INVERTBIT": b_skillgage_h_invertbit, "SKILLGAGE_D_INVERTBIT": b_skillgage_d_invertbit, "SKILLGAGE_F_INVERTBIT": b_skillgage_f_invertbit,
    "SKILLGAGE_SWAP": b_skillgage_swap, "SKILLGAGE_DIRECT_SWAP": b_skillgage_direct_swap,
    "SKILLGAGE_CLEAR": b_skillgage_clear, "SKILLGAGE_DIRECT_CLEAR": b_skillgage_direct_clear,
    "SKILLGAGE_CHARGE": b_skillgage_charge,
})

BUILTINS.update({
    # 画像エディット用ライブラリ: keep the original ERB bodies authoritative
    # when they are loaded, while making the helper names visible to expression
    # dispatch and small fixtures.
    "パーツデータ": b_script_string_function,
    "パーツ選択肢": b_script_string_function,
    "パーツ初期設定": b_script_string_function,
    "パーツ名抽出": b_script_string_function,
    "パーツ選択項目調整": b_script_string_function,
    "パーツ画像サイズ": b_script_numeric_function,
    "モブ画像_設定確認": b_script_numeric_function,
    "画像编辑画面": b_script_procedure,
    "画像编辑_上部選択肢表示": b_script_procedure,
    "画像编辑_上部選択項目生成": b_script_procedure,
    "画像编辑_右メニュー表示": b_script_procedure,
    "画像编辑_隨機処理": b_script_procedure,
    "パーツ隨機変更": b_script_procedure,
    "画像编辑_クリア処理": b_script_procedure,
    "合成画像領域初期化": b_script_procedure,
    "合成画像生成": b_script_procedure,
    "モブ画像合成": b_script_procedure,
    "CN_FE_CHARACTER_SPRITE_LIST": b_script_procedure,
    "カ羅摩トリクス初期化": b_script_procedure,
    "モブ画像_生成": b_script_procedure,
    "モブ画像_スプライト登録": b_script_procedure,
    "モブ画像_リセット": b_script_procedure,
    "モブ画像_スプライト破棄": b_script_procedure,
    "モブ画像_表示": b_script_procedure,
})

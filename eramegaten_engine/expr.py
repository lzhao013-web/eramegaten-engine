from __future__ import annotations

import random
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from .model import EraInputBlocked, norm_name, split_era_args

Value = int | str


class EvalContext(Protocol):
    def get_var(self, base: str, indices: list[Value]) -> Value: ...
    def set_var(self, base: str, indices: list[Value], value: Value) -> None: ...
    def has_symbol(self, name: str) -> bool: ...
    def has_callable(self, name: str) -> bool: ...
    def call_expr_function(self, name: str, args: list[Value]) -> Value: ...
    def render_form(self, text: str) -> str: ...
    def warn(self, message: str) -> None: ...


@dataclass(slots=True)
class VarRef:
    base: str
    indices: list[Value]


@dataclass(slots=True)
class Token:
    kind: str
    value: str


class ExprError(Exception):
    pass


OPS = ["==", ">=", "<=", "!=", "&&", "||", "^^", "!&", "!|", ">>", "<<", "++", "--", "+", "-", "*", "/", "%", ">", "<", "&", "|", "^", "!", "~", "?", "#", "=", ":", ",", "(", ")"]
OP_SET = set(OPS)
DELIMS = set(' \t\r\n,():+-*/%<>=!&|^?#~"')


def truth(v: Value) -> bool:
    if isinstance(v, str):
        return len(v) > 0 and v != "0"
    return v != 0


def to_int(v: Value) -> int:
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if not s:
        return 0
    try:
        return int(s, 0)
    except ValueError:
        try:
            return int(s, 10)
        except ValueError:
            pass
        # Emuera's TOINT returns 0 on non-numeric strings in common configs.
        m = re.match(r"^[+-]?\d+", s)
        return int(m.group(0)) if m else 0


def to_str(v: Value) -> str:
    return v if isinstance(v, str) else str(v)


def tokenize(text: str) -> list[Token]:
    out: list[Token] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if text.startswith("[[", i):
            end = text.find("]]", i + 2)
            if end == -1:
                out.append(Token("QIDENT", text[i + 2 :]))
                break
            out.append(Token("QIDENT", text[i + 2 : end]))
            i = end + 2
            continue
        if ch == '@' and i + 1 < n and text[i + 1] == '"':
            value, i = _read_string(text, i + 1)
            out.append(Token("FORM_STRING", value))
            continue
        if ch == '"':
            value, i = _read_string(text, i)
            out.append(Token("STRING", value))
            continue
        if ch in "0123456789":
            start = i
            if text.startswith(("0x", "0X"), i):
                i += 2
                while i < n and text[i] in "0123456789abcdefABCDEF":
                    i += 1
            else:
                while i < n and text[i] in "0123456789":
                    i += 1
            out.append(Token("NUMBER", text[start:i]))
            continue
        matched = False
        for op in OPS:
            if text.startswith(op, i):
                kind = "OP"
                if op == "(":
                    kind = "LPAREN"
                elif op == ")":
                    kind = "RPAREN"
                elif op == ",":
                    kind = "COMMA"
                elif op == ":":
                    kind = "COLON"
                out.append(Token(kind, op))
                i += len(op)
                matched = True
                break
        if matched:
            continue
        start = i
        while i < n and text[i] not in DELIMS:
            # Stop before an operator sequence even if the first char was not in DELIMS.
            if any(text.startswith(op, i) for op in OPS):
                break
            i += 1
        if start == i:
            out.append(Token("IDENT", ch))
            i += 1
        else:
            out.append(Token("IDENT", text[start:i]))
    out.append(Token("END", ""))
    return out


@lru_cache(maxsize=65536)
def cached_tokens(text: str) -> tuple[Token, ...]:
    # Expression parsing is deliberately context-dependent, but tokenization is
    # pure for a given source string.  Real eraMegaten menus repeatedly evaluate
    # the same small expressions in long loops; caching only tokens keeps
    # variable reads, function calls, and side effects evaluated live.
    return tuple(tokenize(_strip_form_markers_outside_strings(text.strip())))


def _read_string(text: str, quote_index: int) -> tuple[str, int]:
    assert text[quote_index] == '"'
    form_string = quote_index > 0 and text[quote_index - 1] == "@"
    brace_depth = 0
    percent_expr = False
    percent_string = False
    i = quote_index + 1
    chars: list[str] = []
    while i < len(text):
        ch = text[i]
        if form_string:
            if ch == "{":
                brace_depth += 1
                chars.append(ch)
                i += 1
                continue
            if ch == "}" and brace_depth:
                brace_depth -= 1
                chars.append(ch)
                i += 1
                continue
            if ch == "%" and not brace_depth and not percent_string:
                percent_expr = not percent_expr
                chars.append(ch)
                i += 1
                continue
        if ch == '"':
            if form_string and percent_expr:
                percent_string = not percent_string
                chars.append(ch)
                i += 1
                continue
            if i + 1 < len(text) and text[i + 1] == '"':
                chars.append('"')
                i += 2
                continue
            if form_string and brace_depth:
                chars.append(ch)
                i += 1
                continue
            return "".join(chars), i + 1
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
            if nxt in escapes:
                chars.append(escapes[nxt])
                i += 2
            else:
                chars.append(ch)
                i += 1
            continue
        chars.append(ch)
        i += 1
    return "".join(chars), i


def _strip_form_markers_outside_strings(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith('@"', i):
            _, end = _read_string(text, i + 1)
            out.append(text[i:end])
            i = end
            continue
        if text[i] == '"':
            _, end = _read_string(text, i)
            out.append(text[i:end])
            i = end
            continue
        if text.startswith("\\@", i):
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


PRECEDENCE = {
    "||": 40,
    "&&": 40,
    "^^": 40,
    "!&": 40,
    "!|": 40,
    "&": 50,
    "|": 50,
    "^": 50,
    "==": 60,
    "!=": 60,
    ">": 65,
    "<": 65,
    ">=": 65,
    "<=": 65,
    ">>": 70,
    "<<": 70,
    "+": 80,
    "-": 80,
    "*": 90,
    "/": 90,
    "%": 90,
}

RAW_FIRST_ARG_FUNCTIONS = {
    "GETNUM",
    "SUMARRAY",
    "SUMCARRAY",
    "MAXARRAY",
    "MINARRAY",
    "MAXCARRAY",
    "MINCARRAY",
    "INRANGEARRAY",
    "INRANGECARRAY",
    "MATCH",
    "FINDELEMENT",
    "FINDLASTELEMENT",
    "VARSIZE",
    "ERDNAME",
    "FINDCHARA",
    "FINDLASTCHARA",
    "CMATCH",
    "STRJOIN",
}


class ExpressionParser:
    def __init__(self, context: EvalContext, text: str):
        self.context = context
        self.text = text.strip()
        # Era form ternary sometimes appears as \@ cond ? A # B \@; the form
        # renderer strips it, but plain expressions also benefit from removing
        # balanced markers.
        self.tokens = cached_tokens(self.text)
        self.pos = 0

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def pop(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def accept(self, value: str) -> bool:
        if self.peek().value == value:
            self.pos += 1
            return True
        return False

    def expect(self, value: str) -> None:
        if not self.accept(value):
            raise ExprError(f"expected {value!r}, got {self.peek().value!r} in {self.text!r}")

    def parse(self) -> Value:
        if self.peek().kind == "END":
            return 0
        value = self.expr(0)
        return value

    def expr(self, min_bp: int) -> Value:
        left = self.prefix()
        while True:
            tok = self.peek()
            if tok.kind == "OP" and tok.value == "?":
                bp = 5
                if bp < min_bp:
                    break
                self.pop()
                if truth(left):
                    middle = self.expr(0)
                    # Consume and skip false branch syntactically.
                    self.expect("#")
                    _ = self.expr(bp + 1)
                    left = middle
                else:
                    _ = self.expr_until_hash()
                    self.expect("#")
                    left = self.expr(bp + 1)
                continue
            if tok.kind != "OP" or tok.value not in PRECEDENCE:
                break
            op = tok.value
            bp = PRECEDENCE[op]
            if bp < min_bp:
                break
            self.pop()
            right = self.expr(bp + 1)
            left = apply_binary(op, left, right)
        return left

    def expr_until_hash(self) -> Value:
        # Used only to consume the middle branch of an unselected ternary.  This
        # still evaluates it, which is acceptable for side-effect-free Era
        # expressions and keeps the parser small.
        return self.expr(0)

    def prefix(self) -> Value:
        tok = self.pop()
        if tok.kind == "OP" and tok.value in {"++", "--"}:
            if self.peek().kind == "LPAREN":
                self.pop()
                ref = self.parse_var_ref()
                self.expect(")")
            else:
                ref = self.parse_var_ref()
            old = to_int(self.context.get_var(ref.base, ref.indices))
            new = old + (1 if tok.value == "++" else -1)
            self.context.set_var(ref.base, ref.indices, new)
            return new
        if tok.kind == "OP" and tok.value in {"+", "-", "!", "~"}:
            val = self.expr(100)
            if tok.value == "+":
                return to_int(val)
            if tok.value == "-":
                return -to_int(val)
            if tok.value == "!":
                return 0 if truth(val) else 1
            return ~to_int(val)
        if tok.kind == "NUMBER":
            return to_int(tok.value)
        if tok.kind == "STRING":
            return tok.value
        if tok.kind == "FORM_STRING":
            return self.context.render_form(tok.value)
        if tok.kind == "QIDENT":
            value = self.context.call_expr_function("__CONST__", [tok.value])
            if isinstance(value, str):
                # _Rename.csv may map a [[qualified name]] directly to an
                # lvalue (for example ``依頼フラグ:2:0``).  The alias must be
                # dereferenced even when the source QIDENT has no extra
                # ``:index`` suffix; returning the non-empty alias string made
                # every such condition truthy.
                ref = ExpressionParser(self.context, value).parse_var_ref()
                while self.accept(":"):
                    ref.indices.append(self.index_segment())
                return self.context.get_var(ref.base, ref.indices)
            return value
        if tok.kind == "LPAREN":
            val = self.expr(0)
            self.expect(")")
            return val
        if tok.kind == "IDENT":
            return self.ident_value(tok.value)
        if tok.kind == "END":
            return 0
        raise ExprError(f"unexpected token {tok} in {self.text!r}")

    def ident_value(self, name: str) -> Value:
        # Function call
        if self.peek().kind == "LPAREN":
            if not self.context_has_callable(name):
                literal = self.collect_parenthesized_index_literal(name)
                return self.context.get_var(literal, [])
            self.pop()
            raw_first = norm_name(name) in RAW_FIRST_ARG_FUNCTIONS
            ref_positions = self.context_ref_arg_positions(name)
            args = self.parse_call_arguments(call_name=name, raw_first=raw_first, ref_positions=ref_positions)
            self.expect(")")
            return self.context.call_expr_function(name, args)
        indices: list[Value] = []
        while self.accept(":"):
            indices.append(self.index_segment())
        if indices or self.context.has_symbol(name):
            value = self.context.get_var(name, indices)
            if self.peek().kind == "OP" and self.peek().value in {"++", "--"}:
                op = self.pop().value
                old = to_int(value)
                self.context.set_var(name, indices, old + (1 if op == "++" else -1))
                return old
            return value
        # Unknown bare identifiers are usually CSV constants.  Let the context
        # resolve them; if it cannot, it will return 0.
        value = self.context.get_var(name, [])
        if self.peek().kind == "OP" and self.peek().value in {"++", "--"}:
            op = self.pop().value
            old = to_int(value)
            self.context.set_var(name, [], old + (1 if op == "++" else -1))
            return old
        return value

    def parse_call_arguments(self, *, call_name: str = "", raw_first: bool = False, ref_positions: set[int] | None = None) -> list[Any]:
        args: list[Any] = []
        ref_positions = ref_positions or set()
        first = True
        while self.peek().kind != "RPAREN":
            arg_index = len(args)
            if self.peek().kind == "COMMA":
                # Emuera allows omitted function arguments, e.g.
                # F("x", , , 4).  Preserve them as empty strings; numeric
                # parameters coerce this to 0 on binding, while ARGS/string
                # parameters see the Emuera-style blank value.
                args.append(self.omitted_argument(call_name, arg_index))
                self.pop()
                first = False
                continue
            if raw_first and first:
                args.append(self.collect_raw_argument())
            elif arg_index in ref_positions:
                raw = self.collect_raw_argument()
                maker = getattr(self.context, "make_ref_arg_for_call", None)
                ref_arg = maker(raw) if maker is not None else None
                args.append(ref_arg if ref_arg is not None else ExpressionParser(self.context, raw).parse())
            else:
                args.append(self.expr(0))
            first = False
            if self.peek().kind != "COMMA":
                break
            self.pop()
            if self.peek().kind == "RPAREN":
                args.append(self.omitted_argument(call_name, len(args)))
                break
        return args

    def omitted_argument(self, name: str, index: int) -> Any:
        getter = getattr(self.context, "omitted_arg_for_call", None)
        if getter is not None:
            try:
                return getter(name, index)
            except Exception:
                pass
        return ""

    def index_segment(self) -> Value:
        tok = self.peek()
        if tok.kind == "LPAREN":
            self.pop()
            val = self.expr(0)
            self.expect(")")
            return val
        if tok.kind in {"NUMBER", "STRING", "FORM_STRING", "QIDENT"}:
            return self.prefix()
        if tok.kind == "IDENT":
            self.pop()
            name = tok.value
            if self.peek().kind == "LPAREN":
                if self.context_has_callable(name):
                    return self.ident_value(name)
                return self.collect_parenthesized_index_literal(name)
            # A segment followed by an operator is still just a segment.  Use
            # known variables/constants as values; otherwise keep the literal for
            # CSV name resolution (e.g. BASE:MASTER:LV).
            should_eval = getattr(self.context, "index_segment_should_evaluate", None)
            eval_segment = bool(should_eval(name)) if callable(should_eval) else self.context.has_symbol(name)
            if eval_segment:
                value = self.context.get_var(name, [])
                if self.peek().kind == "OP" and self.peek().value in {"++", "--"}:
                    op = self.pop().value
                    old = to_int(value)
                    self.context.set_var(name, [], old + (1 if op == "++" else -1))
                    return old
                return value
            return name
        return self.expr(0)

    def context_has_callable(self, name: str) -> bool:
        checker = getattr(self.context, "has_callable", None)
        if checker is None:
            return False
        try:
            return bool(checker(name))
        except Exception:
            return False

    def context_ref_arg_positions(self, name: str) -> set[int]:
        getter = getattr(self.context, "ref_arg_positions_for_call", None)
        if getter is None:
            return set()
        try:
            return set(getter(name))
        except Exception:
            return set()

    def collect_parenthesized_index_literal(self, name: str) -> str:
        parts = [name]
        depth = 0
        while True:
            tok = self.pop()
            if tok.kind == "END":
                break
            parts.append(self.token_source(tok))
            if tok.kind == "LPAREN":
                depth += 1
            elif tok.kind == "RPAREN":
                depth -= 1
                if depth <= 0:
                    break
        return "".join(parts)

    def token_source(self, tok: Token) -> str:
        if tok.kind == "STRING":
            return '"' + tok.value.replace('"', '""') + '"'
        if tok.kind == "FORM_STRING":
            return '@"' + tok.value.replace('"', '""') + '"'
        if tok.kind == "QIDENT":
            return "[[" + tok.value + "]]"
        return tok.value

    def collect_raw_argument(self) -> str:
        parts: list[str] = []
        depth = 0
        while True:
            tok = self.peek()
            if tok.kind == "END":
                break
            if depth == 0 and tok.kind in {"COMMA", "RPAREN"}:
                break
            tok = self.pop()
            if tok.kind == "LPAREN":
                depth += 1
            elif tok.kind == "RPAREN" and depth:
                depth -= 1
            if tok.kind == "STRING":
                parts.append('"' + tok.value.replace('"', '""') + '"')
            elif tok.kind == "FORM_STRING":
                parts.append('@"' + tok.value.replace('"', '""') + '"')
            elif tok.kind == "QIDENT":
                parts.append("[[" + tok.value + "]]")
            else:
                parts.append(tok.value)
        return "".join(parts).strip()

    def parse_var_ref(self) -> VarRef:
        tok = self.pop()
        if tok.kind == "QIDENT":
            value = self.context.call_expr_function("__CONST__", [tok.value])
            if not isinstance(value, str):
                raise ExprError(f"invalid lvalue {self.text!r}")
            ref = ExpressionParser(self.context, value).parse_var_ref()
            while self.accept(":"):
                ref.indices.append(self.index_segment())
            return ref
        if tok.kind != "IDENT":
            raise ExprError(f"invalid lvalue {self.text!r}")
        indices: list[Value] = []
        while self.accept(":"):
            indices.append(self.index_segment())
        return VarRef(tok.value, indices)


def apply_binary(op: str, a: Value, b: Value) -> Value:
    if op == "+":
        if isinstance(a, str) or isinstance(b, str):
            return to_str(a) + to_str(b)
        return to_int(a) + to_int(b)
    if op == "-":
        return to_int(a) - to_int(b)
    if op == "*":
        if isinstance(a, str) and isinstance(b, int):
            return a * max(0, b)
        if isinstance(b, str) and isinstance(a, int):
            return b * max(0, a)
        return to_int(a) * to_int(b)
    if op == "/":
        rb = to_int(b)
        return 0 if rb == 0 else int(to_int(a) / rb)
    if op == "%":
        rb = to_int(b)
        return 0 if rb == 0 else to_int(a) % rb
    if op == ">>":
        return to_int(a) >> to_int(b)
    if op == "<<":
        return to_int(a) << to_int(b)
    if op == "==":
        return 1 if a == b else 0
    if op == "!=":
        return 1 if a != b else 0
    if op == ">":
        return 1 if (to_str(a) > to_str(b) if isinstance(a, str) or isinstance(b, str) else to_int(a) > to_int(b)) else 0
    if op == "<":
        return 1 if (to_str(a) < to_str(b) if isinstance(a, str) or isinstance(b, str) else to_int(a) < to_int(b)) else 0
    if op == ">=":
        return 1 if (to_str(a) >= to_str(b) if isinstance(a, str) or isinstance(b, str) else to_int(a) >= to_int(b)) else 0
    if op == "<=":
        return 1 if (to_str(a) <= to_str(b) if isinstance(a, str) or isinstance(b, str) else to_int(a) <= to_int(b)) else 0
    if op == "&&":
        return 1 if truth(a) and truth(b) else 0
    if op == "||":
        return 1 if truth(a) or truth(b) else 0
    if op == "^^":
        return 1 if truth(a) ^ truth(b) else 0
    if op == "!&":
        return 1 if not (truth(a) and truth(b)) else 0
    if op == "!|":
        return 1 if not (truth(a) or truth(b)) else 0
    if op == "&":
        return to_int(a) & to_int(b)
    if op == "|":
        return to_int(a) | to_int(b)
    if op == "^":
        return to_int(a) ^ to_int(b)
    raise ExprError(f"unsupported operator {op}")


def eval_expr(context: EvalContext, text: str, default: Value = 0) -> Value:
    try:
        return ExpressionParser(context, text).parse()
    except Exception as exc:
        if isinstance(exc, EraInputBlocked):
            raise
        context.warn(f"expression failed {text!r}: {exc}")
        return default


def parse_lvalue(context: EvalContext, text: str) -> VarRef:
    parser = ExpressionParser(context, strip_outer_parens(text))
    ref = parser.parse_var_ref()
    if parser.peek().kind != "END":
        raise ExprError(f"trailing tokens in lvalue {text!r}")
    return ref


def strip_outer_parens(text: str) -> str:
    s = text.strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        in_str = False
        closes_at_end = False
        i = 0
        while i < len(s):
            ch = s[i]
            if in_str:
                if ch == '"' and (i == 0 or s[i - 1] != "\\"):
                    in_str = False
                i += 1
                continue
            if ch == '"':
                in_str = True
            elif s.startswith("[[", i):
                end = s.find("]]", i + 2)
                if end == -1:
                    break
                i = end + 2
                continue
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    closes_at_end = i == len(s) - 1
                    break
            i += 1
        if not closes_at_end:
            break
        s = s[1:-1].strip()
    return s

from __future__ import annotations

import re
from dataclasses import dataclass

from .expr import eval_expr, to_int, to_str, truth
from .model import split_era_args


@dataclass(slots=True)
class EraFormatter:
    context: object

    def render_form(self, text: str, *, render_braces: bool = True) -> str:
        # Most real command/function targets are plain identifiers or already
        # concrete text.  Avoid rescanning them through every FORM pass; keep the
        # slower paths only when their marker can actually appear.
        if "\\@" in text:
            text = self._render_conditionals(text)
        if render_braces and "{" in text:
            text = self._render_braces(text)
        if "%" in text:
            text = self._render_percents(text)
        if "\\" in text:
            text = self._unescape_form_text(text)
        return text

    def _render_conditionals(self, text: str) -> str:
        # Handles \@ cond ? true # false \@.  This is a form-string feature and
        # is intentionally permissive; nested conditionals are processed by
        # repeated passes.
        start = text.find("\\@")
        guard = 0
        while start != -1 and guard < 1000:
            end = text.find("\\@", start + 2)
            if end == -1:
                break
            body = text[start + 2 : end]
            q = self._find_top(body, "?")
            h = self._find_top(body, "#")
            if q != -1 and h != -1 and q < h:
                cond = body[:q].strip()
                yes = body[q + 1 : h]
                no = body[h + 1 :]
                chosen = yes if truth(eval_expr(self.context, cond)) else no
                # eraMegaten frequently wraps a form conditional in percent
                # delimiters, e.g. ``%\@FLAG ?(予約中)#\@%``.  The conditional
                # branches are already form text; after choosing a literal
                # branch it must not be fed back to the %expr% evaluator (which
                # would coerce bare text such as ``(予約中)`` to 0).  If the
                # conditional is exactly surrounded by one percent pair, consume
                # that pair while keeping any %expr% markers inside the chosen
                # branch for the later percent-rendering pass.
                replace_start = start
                replace_end = end + 2
                if start > 0 and text[start - 1] == "%" and end + 2 < len(text) and text[end + 2] == "%":
                    replace_start -= 1
                    replace_end += 1
                text = text[:replace_start] + chosen + text[replace_end:]
                start = text.find("\\@")
            else:
                text = text[:start] + body + text[end + 2 :]
                start = text.find("\\@", start + len(body))
            guard += 1
        return text

    def _find_top(self, text: str, char: str) -> int:
        depth = 0
        in_str = False
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
            elif ch in "({[":
                depth += 1
            elif ch in ")}]" and depth:
                depth -= 1
            elif ch == char and depth == 0:
                return i
            i += 1
        return -1

    def _render_braces(self, text: str) -> str:
        return self._replace_balanced(text, "{", "}", self._eval_format_item)

    def _render_percents(self, text: str) -> str:
        out: list[str] = []
        i = 0
        while i < len(text):
            if text[i] != "%" or self._is_escaped(text, i):
                out.append(text[i])
                i += 1
                continue
            end = self._find_percent_end(text, i + 1)
            if end == -1:
                out.append(text[i])
                i += 1
                continue
            body = text[i + 1 : end]
            out.append(self._eval_format_item(body))
            i = end + 1
        return "".join(out)

    def _find_percent_end(self, text: str, start: int) -> int:
        depth = 0
        in_str = False
        i = start
        while i < len(text):
            ch = text[i]
            if in_str:
                if ch == '"' and (i == 0 or text[i - 1] != "\\"):
                    in_str = False
                i += 1
                continue
            if ch == '"':
                in_str = True
            elif ch in "(":
                depth += 1
            elif ch in ")" and depth:
                depth -= 1
            elif ch == "%" and depth == 0 and not self._is_escaped(text, i):
                return i
            i += 1
        return -1

    def _replace_balanced(self, text: str, open_ch: str, close_ch: str, fn) -> str:
        out: list[str] = []
        i = 0
        while i < len(text):
            if text[i] != open_ch or self._is_escaped(text, i):
                out.append(text[i])
                i += 1
                continue
            depth = 1
            j = i + 1
            in_str = False
            while j < len(text):
                ch = text[j]
                if in_str:
                    if ch == '"' and text[j - 1] != "\\":
                        in_str = False
                    j += 1
                    continue
                if ch == '"':
                    in_str = True
                elif ch == open_ch:
                    depth += 1
                elif ch == close_ch:
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= len(text):
                out.append(text[i])
                i += 1
            else:
                out.append(fn(text[i + 1 : j]))
                i = j + 1
        return "".join(out)

    def _is_escaped(self, text: str, index: int) -> bool:
        count = 0
        i = index - 1
        while i >= 0 and text[i] == "\\":
            count += 1
            i -= 1
        return bool(count % 2)

    def _unescape_form_text(self, text: str) -> str:
        # In Era form strings a backslash is used to print otherwise-special
        # marker characters literally.  eraMegaten uses this heavily for
        # percentages and display parentheses, e.g. ``{RATE}\%`` and
        # ``{DAY}日目\(%TIME%\)``.  Keep unknown backslash sequences intact so
        # paths and AA text are not mangled.
        escapes = {
            "%": "%",
            "(": "(",
            ")": ")",
            "{": "{",
            "}": "}",
            "?": "?",
            "#": "#",
            "\\": "\\",
            "n": "\n",
        }
        out: list[str] = []
        i = 0
        while i < len(text):
            if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in escapes:
                out.append(escapes[text[i + 1]])
                i += 2
                continue
            out.append(text[i])
            i += 1
        return "".join(out)

    def _eval_format_item(self, body: str) -> str:
        if body.strip() in {"/", "／", "|", "｜"}:
            return body
        parts = split_era_args(body)
        if not parts:
            return ""
        value = eval_expr(self.context, parts[0], default="")
        s = to_str(value)
        if len(parts) >= 2 and parts[1] != "":
            width = to_int(eval_expr(self.context, parts[1], default=0))
            align = parts[2].strip().upper() if len(parts) >= 3 else "RIGHT"
            if width > 0:
                if align.startswith("LEFT"):
                    s = s.ljust(width)
                elif align.startswith("CENTER"):
                    s = s.center(width)
                else:
                    s = s.rjust(width)
        return s

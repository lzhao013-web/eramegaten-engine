from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .model import CharacterTemplate, norm_name, read_text_auto, strip_comment


STANDARD_MAPS = {
    "ABL", "BASE", "CFLAG", "CDFLAG", "CSTR", "DA", "DB", "DC", "EQUIP",
    "EX", "EXP", "FLAG", "GLOBAL", "GLOBALS", "ITEM", "JUEL", "MARK",
    "NOWEX", "PALAM", "SAVESTR", "SOURCE", "STAIN", "STR", "TALENT", "TSTR",
    "TFLAG", "TEQUIP", "TCVAR", "TCSTR", "DOWN", "UP",
}

STRING_MAP_ALIASES = {
    # Emuera exposes CSV item/train display names through string arrays even
    # though the numeric namespace is ITEM/TRAIN.  eraMegaten uses both heavily:
    # ITEMNAME:idx in item/clothing UIs and TRAINNAME:idx in the training menu.
    "ITEM": "ITEMNAME",
    "TRAIN": "TRAINNAME",
}

CHARA_NUMERIC_KIND = {
    "基礎": "BASE",
    "能力": "ABL",
    "素質": "TALENT",
    "経験": "EXP",
    "刻印": "MARK",
    "珠": "JUEL",
    "Ｃフラグ": "CFLAG",
    "CFLAG": "CFLAG",
    "フラグ": "CFLAG",
    "装備": "EQUIP",
    "相性": "BASE",
}

CHARA_STRING_KIND = {
    "Ｃ文字列": "CSTR",
    "CSTR": "CSTR",
    "呼び名": "CALLNAME",
    "名前": "NAME",
    "ニックネーム": "NICKNAME",
    "NICKNAME": "NICKNAME",
    "主人名": "MASTERNAME",
    "マスター名": "MASTERNAME",
    "主人公呼び名": "MASTERNAME",
    "MASTERNAME": "MASTERNAME",
}


def parse_era_int(text: str) -> int:
    s = str(text).strip()
    try:
        return int(s, 0)
    except ValueError:
        return int(s, 10)


@dataclass(slots=True)
class EraCsvDatabase:
    root: Path
    name_to_index: dict[str, dict[str, int]] = field(default_factory=dict)
    index_to_name: dict[str, dict[int, str]] = field(default_factory=dict)
    constants: dict[str, int] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    variable_sizes: dict[str, tuple[int, ...]] = field(default_factory=dict)
    gamebase: dict[str, str] = field(default_factory=dict)
    replacements: dict[str, str] = field(default_factory=dict)
    characters: dict[int, CharacterTemplate] = field(default_factory=dict)
    sp_characters: dict[int, CharacterTemplate] = field(default_factory=dict)
    characters_by_file_no: dict[int, CharacterTemplate] = field(default_factory=dict)
    initial_characters: list[int] = field(default_factory=list)
    resources: dict[str, list[str]] = field(default_factory=dict)
    erd_index_to_name: dict[str, dict[int, dict[int, str]]] = field(default_factory=dict)
    erd_name_to_index: dict[str, dict[int, dict[str, int]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, root: str | Path) -> "EraCsvDatabase":
        root = Path(root)
        csv_root = root / "CSV"
        db = cls(root=root)
        if not csv_root.exists():
            db.warnings.append(f"CSV directory not found: {csv_root}")
            return db
        for path in sorted(csv_root.rglob("*"), key=lambda p: str(p).lower()):
            if not path.is_file() or path.suffix.lower() != ".csv":
                continue
            stem = path.stem
            stem_key = norm_name(stem)
            try:
                if stem_key == "GAMEBASE":
                    db._load_gamebase(path)
                elif stem_key == "_RENAME":
                    db._load_rename(path)
                elif stem_key == "_REPLACE":
                    db._load_replace(path)
                elif stem_key == "VARIABLESIZE":
                    db._load_variable_size(path)
                elif re.match(r"CHARA\d+", stem, re.IGNORECASE):
                    db._load_chara(path)
                elif stem_key in STANDARD_MAPS:
                    db._load_map(path, stem_key)
                    if stem_key in STRING_MAP_ALIASES:
                        db._load_map(path, STRING_MAP_ALIASES[stem_key])
                elif stem_key in STRING_MAP_ALIASES:
                    db._load_map(path, stem_key)
                    db._load_map(path, STRING_MAP_ALIASES[stem_key])
                elif (base_key := re.sub(r"\d+$", "", stem_key)) in STANDARD_MAPS:
                    # eraMegaten splits a few standard variable maps into
                    # numbered files (notably Cdflag1.csv/Cdflag2.csv).  Emuera
                    # still treats them as one CDFLAG namespace, so merge them
                    # into the canonical map instead of ignoring the suffix.
                    db._load_map(path, base_key)
                    if base_key in STRING_MAP_ALIASES:
                        db._load_map(path, STRING_MAP_ALIASES[base_key])
                elif "RESOURCE" in stem_key or "画像" in str(path):
                    db._load_resource(path)
            except Exception as exc:  # keep loading; content is untrusted data
                db.warnings.append(f"{path}: {exc}")
        for path in sorted(root.rglob("*"), key=lambda p: str(p).lower()):
            if not path.is_file() or path.suffix.lower() != ".erd":
                continue
            try:
                db._load_erd(path)
            except Exception as exc:
                db.warnings.append(f"{path}: {exc}")
        db._install_gamebase_constants()
        db._install_builtin_constants()
        return db

    def _rows(self, path: Path) -> Iterable[list[str]]:
        text = read_text_auto(path)
        for raw in text.splitlines():
            line = strip_comment(raw).strip()
            if not line:
                continue
            try:
                row = next(csv.reader([line], skipinitialspace=True))
            except Exception:
                row = [x.strip() for x in line.split(",")]
            row = [cell.strip() for cell in row]
            if row:
                yield row

    def _load_map(self, path: Path, key: str) -> None:
        n2i = self.name_to_index.setdefault(key, {})
        i2n = self.index_to_name.setdefault(key, {})
        for row in self._rows(path):
            if len(row) < 2:
                continue
            try:
                idx = parse_era_int(row[0])
            except ValueError:
                continue
            name = row[1].strip()
            if not name:
                continue
            n2i[norm_name(name)] = idx
            i2n[idx] = name
            # Also install a scoped constant so [[BASE:LV]] and bare LV can be resolved.
            self.constants.setdefault(norm_name(f"{key}:{name}"), idx)
            self.constants.setdefault(norm_name(name), idx)

    def _load_rename(self, path: Path) -> None:
        for row in self._rows(path):
            if len(row) < 2:
                continue
            name = row[1]
            try:
                idx = parse_era_int(row[0])
            except ValueError:
                if name:
                    self.aliases[norm_name(name)] = row[0].strip()
                    if ":" in name:
                        _, short = name.split(":", 1)
                        self.aliases.setdefault(norm_name(short), row[0].strip())
                continue
            if name:
                self.constants[norm_name(name)] = idx
                if ":" in name:
                    scope, short = name.split(":", 1)
                    self.constants.setdefault(norm_name(short), idx)

    def _load_replace(self, path: Path) -> None:
        for row in self._rows(path):
            if len(row) >= 2 and row[0]:
                self.replacements[norm_name(row[0])] = row[1]

    def _load_gamebase(self, path: Path) -> None:
        for row in self._rows(path):
            if len(row) >= 2:
                self.gamebase[row[0]] = row[1]
                if row[0] == "最初からいるキャラ":
                    for cell in row[1:]:
                        if cell == "":
                            continue
                        try:
                            self.initial_characters.append(parse_era_int(cell))
                        except ValueError:
                            self.warnings.append(f"{path}: invalid 初期キャラ value: {cell}")

    def _load_variable_size(self, path: Path) -> None:
        for row in self._rows(path):
            if len(row) < 2:
                continue
            dims: list[int] = []
            for cell in row[1:]:
                if cell == "":
                    continue
                try:
                    dims.append(max(0, parse_era_int(cell)))
                except ValueError:
                    dims = []
                    break
            if not dims:
                continue
            self.variable_sizes[norm_name(row[0])] = tuple(dims)

    def _load_erd(self, path: Path) -> None:
        stem = path.stem
        dimension = 1
        base = stem
        if "@" in stem:
            before, after = stem.rsplit("@", 1)
            if before and after.isdigit():
                base = before
                dimension = max(1, parse_era_int(after))
        key = norm_name(base)
        i2n = self.erd_index_to_name.setdefault(key, {}).setdefault(dimension, {})
        n2i = self.erd_name_to_index.setdefault(key, {}).setdefault(dimension, {})
        for row in self._rows(path):
            if len(row) < 2:
                continue
            try:
                idx = parse_era_int(row[0])
            except ValueError:
                continue
            name = row[1].strip()
            if not name:
                continue
            i2n[idx] = name
            n2i[norm_name(name)] = idx
            if dimension == 1:
                self.constants.setdefault(norm_name(f"{key}:{name}"), idx)

    def _install_gamebase_constants(self) -> None:
        mapping = {
            "コード": "GAMEBASE_CODE",
            "バージョン": "GAMEBASE_VERSION",
            "称号": "GAMEBASE_TITLE",
            "作者": "GAMEBASE_AUTHOR",
            "製作年": "GAMEBASE_YEAR",
            "追加情報": "GAMEBASE_INFO",
        }
        for key, var in mapping.items():
            if key in self.gamebase:
                value = self.gamebase[key]
                try:
                    self.constants[norm_name(var)] = parse_era_int(value)
                except ValueError:
                    # string gamebase constants are exposed through Memory defaults.
                    pass

    def _install_builtin_constants(self) -> None:
        for name, value in {
            "MASTER": 0,
            "PLAYER": 0,
            "TARGET": 0,
            "ASSI": 1,
            "CHARA": 0,
            "NO": 0,
            "TRUE": 1,
            "FALSE": 0,
            "NULL": 0,
        }.items():
            self.constants.setdefault(name, value)

    def _load_chara(self, path: Path) -> None:
        file_no: int | None = None
        m_file_no = re.match(r"Chara(\d+)", path.stem, re.IGNORECASE)
        if m_file_no:
            try:
                file_no = parse_era_int(m_file_no.group(1))
            except ValueError:
                file_no = None
        template = CharacterTemplate(no=file_no if file_no is not None else -1, csv_no=file_no, source=str(path))
        relation_targets: dict[int, int] = {}

        def parse_numeric_cell(text: str) -> int:
            try:
                return parse_era_int(text)
            except ValueError:
                return self.resolve_constant(text, default=0)

        def set_chara_numeric(kind: str, name: str, value: int) -> None:
            idx = self.resolve_index(kind, name)
            template.numeric.setdefault(kind, {})[idx] = value

        def set_relation_slot(slot: int, target: int | None = None, value: int | None = None) -> None:
            if target is not None:
                relation_targets[slot] = target
                set_chara_numeric("CFLAG", f"相性{slot}", target)
                set_chara_numeric("CFLAG", f"キャラ相性{slot}", target)
            elif slot in relation_targets:
                target = relation_targets[slot]
            if value is not None:
                set_chara_numeric("CFLAG", f"相性値{slot}", value)
                set_chara_numeric("CFLAG", f"キャラ相性値{slot}", value)
                if target is not None:
                    template.numeric.setdefault("RELATION", {})[target] = value

        for row in self._rows(path):
            head = row[0]
            template.raw.setdefault(head, []).append(row[1:])
            if head == "番号" and len(row) >= 2:
                try:
                    template.no = parse_era_int(row[1])
                except ValueError:
                    pass
            elif head == "名前" and len(row) >= 2:
                template.name = row[1]
            elif head == "呼び名" and len(row) >= 2:
                template.callname = row[1]
            elif head in CHARA_NUMERIC_KIND and len(row) >= 2:
                kind = CHARA_NUMERIC_KIND[head]
                if kind == "CFLAG" and len(row) >= 3:
                    try:
                        if parse_era_int(row[1]) == 0 and parse_numeric_cell(row[2]) != 0:
                            template.is_sp = True
                    except ValueError:
                        pass
                    m_target = re.fullmatch(r"相性(\d+)", row[1].strip())
                    if m_target:
                        set_relation_slot(int(m_target.group(1)), target=parse_numeric_cell(row[2]))
                        continue
                    m_value = re.fullmatch(r"相性値(\d+)", row[1].strip())
                    if m_value:
                        slot = int(m_value.group(1))
                        if len(row) >= 4 and row[3] != "":
                            set_relation_slot(slot, target=parse_numeric_cell(row[2]), value=parse_numeric_cell(row[3]))
                        else:
                            set_relation_slot(slot, value=parse_numeric_cell(row[2]))
                        continue
                idx = self.resolve_index(kind, row[1])
                value = 1
                if len(row) >= 3 and row[2] != "":
                    try:
                        value = parse_era_int(row[2])
                    except ValueError:
                        value = self.resolve_constant(row[2], default=0)
                template.numeric.setdefault(kind, {})[idx] = value
            elif head in CHARA_STRING_KIND and len(row) >= 2:
                kind = CHARA_STRING_KIND[head]
                if kind in {"NAME", "CALLNAME", "NICKNAME", "MASTERNAME"}:
                    template.strings.setdefault(kind, {})[0] = row[1]
                elif len(row) >= 3:
                    idx = self.resolve_index(kind, row[1])
                    template.strings.setdefault(kind, {})[idx] = row[2]
        if template.no >= 0:
            if not template.callname:
                template.callname = template.name
            template.strings.setdefault("NAME", {})[0] = template.name
            template.strings.setdefault("CALLNAME", {})[0] = template.callname
            template.numeric.setdefault("NO", {})[0] = template.no
            is_sp = self._template_is_sp(template)
            if is_sp:
                self.sp_characters[template.no] = template
            else:
                self.characters[template.no] = template
            if file_no is not None and not is_sp:
                self.characters_by_file_no[file_no] = template
            if template.name:
                self.constants.setdefault(norm_name(f"キャラ:{template.name}"), template.no)
                self.constants.setdefault(norm_name(template.name), template.no)

    def _template_is_sp(self, template: CharacterTemplate) -> bool:
        return template.is_sp

    def _load_resource(self, path: Path) -> None:
        for row in self._rows(path):
            if len(row) >= 2:
                self.resources[row[0]] = row[1:]

    def resolve_constant(self, name: str, default: int | None = None) -> int:
        key = norm_name(name)
        if key in self.constants:
            return self.constants[key]
        if default is None:
            raise KeyError(name)
        return default

    def resolve_index(self, var: str, segment: str | int) -> int:
        if isinstance(segment, int):
            return segment
        text = str(segment).strip()
        if text == "":
            return 0
        try:
            return parse_era_int(text)
        except ValueError:
            pass
        key = norm_name(var)
        if key in self.name_to_index and norm_name(text) in self.name_to_index[key]:
            return self.name_to_index[key][norm_name(text)]
        erd_dim1 = self.erd_name_to_index.get(key, {}).get(1, {})
        if norm_name(text) in erd_dim1:
            return erd_dim1[norm_name(text)]
        scoped = norm_name(f"{key}:{text}")
        if scoped in self.constants:
            return self.constants[scoped]
        if norm_name(text) in self.constants:
            return self.constants[norm_name(text)]
        return 0

    def name_of(self, var: str, idx: int) -> str:
        return self.index_to_name.get(norm_name(var), {}).get(idx, str(idx))

    def erd_name_of(self, var: str, idx: int, dimension: int = 1) -> str:
        key = norm_name(var)
        dim = max(1, int(dimension))
        return self.erd_index_to_name.get(key, {}).get(dim, {}).get(int(idx), "")

    def csv_template(self, chara_no: int, *, sp: bool = False) -> CharacterTemplate | None:
        return (self.sp_characters if sp else self.characters).get(chara_no)

    def csv_exists(self, chara_no: int, *, sp: bool = False) -> bool:
        return self.csv_template(chara_no, sp=sp) is not None

    def csv_value(self, var: str, chara_no: int, idx: int, default: int | str = 0, *, sp: bool = False) -> int | str:
        tmpl = self.csv_template(chara_no, sp=sp)
        if not tmpl:
            return default
        key = norm_name(var)
        if key in tmpl.numeric:
            return tmpl.numeric[key].get(idx, default)
        if key in tmpl.strings:
            return tmpl.strings[key].get(idx, default)
        return default

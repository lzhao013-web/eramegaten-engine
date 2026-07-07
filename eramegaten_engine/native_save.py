from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory import CHARA_NUMERIC_ARRAYS, CHARA_STRING_ARRAYS, CharacterState
from .model import Program, norm_name


MAGIC = 0x0A1A0A0D41524589
VERSION_1808 = 1808
LEGACY_EMUERA_START_MARKERS = (
    "__EMUERA_1808_STRAT__",
    "__EMUERA_1803_STRAT__",
    "__EMUERA_1729_STRAT__",
    "__EMUERA_1708_STRAT__",
    "__EMUERA_STRAT__",
)


class SaveFormatError(ValueError):
    pass


class SaveFileType:
    NORMAL = 0x00
    GLOBAL = 0x01
    VAR = 0x02
    CHARVAR = 0x03


class SaveDataType:
    INT = 0x00
    INT_ARRAY = 0x01
    INT_ARRAY_2D = 0x02
    INT_ARRAY_3D = 0x03
    STR = 0x10
    STR_ARRAY = 0x11
    STR_ARRAY_2D = 0x12
    STR_ARRAY_3D = 0x13
    SEPARATOR = 0xFD
    EOC = 0xFE
    EOF = 0xFF


class B:
    BYTE = 0xCF
    INT16 = 0xD0
    INT32 = 0xD1
    INT64 = 0xD2
    STRING = 0xD8
    EOA1 = 0xE0
    EOA2 = 0xE1
    ZERO = 0xF0
    ZERO_A1 = 0xF1
    ZERO_A2 = 0xF2
    EOD = 0xFF


@dataclass(slots=True)
class NativeSave:
    file_type: int
    script_code: int = 0
    script_version: int = 0
    save_text: str = ""
    numeric: dict[str, dict[tuple[int, ...], int]] = field(default_factory=dict)
    strings: dict[str, dict[tuple[int, ...], str]] = field(default_factory=dict)
    characters: list[CharacterState] = field(default_factory=list)

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
            "_meta": {"text": self.save_text, "script_code": self.script_code, "script_version": self.script_version},
        }


class EraBinarySaveReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        magic = self.u64()
        if magic != MAGIC:
            raise SaveFormatError("not an Emuera binary save")
        self.version = self.u32()
        if self.version != VERSION_1808:
            raise SaveFormatError(f"unsupported Emuera save version: {self.version}")
        count = self.u32()
        self.pos += count * 4
        if self.pos > len(self.data):
            raise SaveFormatError("truncated header")

    @classmethod
    def from_path(cls, path: str | Path) -> "EraBinarySaveReader":
        return cls(Path(path).read_bytes())

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise SaveFormatError("truncated data")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def byte(self) -> int:
        return self.take(1)[0]

    def i16(self) -> int:
        return struct.unpack("<h", self.take(2))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.take(8))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def dotnet_string(self) -> str:
        length = self._read_7bit_encoded_int()
        raw = self.take(length)
        if not raw:
            return ""
        return raw.decode("utf-16-le", errors="replace")

    def _read_7bit_encoded_int(self) -> int:
        value = 0
        shift = 0
        while True:
            b = self.byte()
            value |= (b & 0x7F) << shift
            if not b & 0x80:
                return value
            shift += 7
            if shift > 35:
                raise SaveFormatError("bad 7-bit encoded length")

    def packed_int(self, first: int | None = None) -> int:
        b = self.byte() if first is None else first
        if b <= B.BYTE:
            return b
        if b == B.INT16:
            return self.i16()
        if b == B.INT32:
            return self.i32()
        if b == B.INT64:
            return self.i64()
        raise SaveFormatError(f"bad packed integer marker: 0x{b:02x}")

    def variable_code(self) -> tuple[int, str | None]:
        typ = self.byte()
        if typ in {SaveDataType.SEPARATOR, SaveDataType.EOC, SaveDataType.EOF}:
            return typ, None
        return typ, self.dotnet_string()

    def read_value(self, typ: int) -> dict[tuple[int, ...], int | str] | int | str:
        if typ == SaveDataType.INT:
            return self.packed_int()
        if typ == SaveDataType.STR:
            return self.dotnet_string()
        if typ == SaveDataType.INT_ARRAY:
            return self.read_int_array(1)
        if typ == SaveDataType.INT_ARRAY_2D:
            return self.read_int_array(2)
        if typ == SaveDataType.INT_ARRAY_3D:
            return self.read_int_array(3)
        if typ == SaveDataType.STR_ARRAY:
            return self.read_str_array(1)
        if typ == SaveDataType.STR_ARRAY_2D:
            return self.read_str_array(2)
        if typ == SaveDataType.STR_ARRAY_3D:
            return self.read_str_array(3)
        raise SaveFormatError(f"unsupported data type: 0x{typ:02x}")

    def read_int_array(self, dim: int) -> dict[tuple[int, ...], int]:
        lengths = [self.i32() for _ in range(dim)]
        out: dict[tuple[int, ...], int] = {}
        idx = [0, 0, 0]
        while True:
            b = self.byte()
            if b == B.EOD:
                return out
            if dim >= 3 and b == B.ZERO_A2:
                idx[0] += self.packed_int()
                idx[1] = idx[2] = 0
                continue
            if dim >= 3 and b == B.EOA2:
                idx[0] += 1
                idx[1] = idx[2] = 0
                continue
            if dim >= 2 and b == B.ZERO_A1:
                idx[0 if dim == 2 else 1] += self.packed_int()
                idx[1 if dim == 2 else 2] = 0
                continue
            if dim >= 2 and b == B.EOA1:
                idx[0 if dim == 2 else 1] += 1
                idx[1 if dim == 2 else 2] = 0
                continue
            if b == B.ZERO:
                idx[dim - 1] += self.packed_int()
                continue
            value = self.packed_int(b)
            key = tuple(idx[:dim])
            if value:
                out[key] = value
            idx[dim - 1] += 1
            # Trust Emuera's end markers, but avoid unbounded indices on
            # malformed files.
            if lengths and idx[0] > max(lengths[0], 0) + 1_000_000:
                raise SaveFormatError("runaway integer array decode")

    def read_str_array(self, dim: int) -> dict[tuple[int, ...], str]:
        lengths = [self.i32() for _ in range(dim)]
        out: dict[tuple[int, ...], str] = {}
        idx = [0, 0, 0]
        while True:
            b = self.byte()
            if b == B.EOD:
                return out
            if dim >= 3 and b == B.ZERO_A2:
                idx[0] += self.packed_int()
                idx[1] = idx[2] = 0
                continue
            if dim >= 3 and b == B.EOA2:
                idx[0] += 1
                idx[1] = idx[2] = 0
                continue
            if dim >= 2 and b == B.ZERO_A1:
                idx[0 if dim == 2 else 1] += self.packed_int()
                idx[1 if dim == 2 else 2] = 0
                continue
            if dim >= 2 and b == B.EOA1:
                idx[0 if dim == 2 else 1] += 1
                idx[1 if dim == 2 else 2] = 0
                continue
            if b == B.ZERO:
                idx[dim - 1] += self.packed_int()
                continue
            if b != B.STRING:
                raise SaveFormatError(f"bad string array marker: 0x{b:02x}")
            value = self.dotnet_string()
            if value:
                out[tuple(idx[:dim])] = value
            idx[dim - 1] += 1
            if lengths and idx[0] > max(lengths[0], 0) + 1_000_000:
                raise SaveFormatError("runaway string array decode")


class EraBinarySaveWriter:
    def __init__(self, program: Program | None = None):
        self.program = program
        self.out = bytearray()

    def write(self, data: bytes) -> None:
        self.out += data

    def byte(self, value: int) -> None:
        self.out.append(value & 0xFF)

    def i16(self, value: int) -> None:
        self.write(struct.pack("<h", int(value)))

    def i32(self, value: int) -> None:
        self.write(struct.pack("<i", int(value)))

    def i64(self, value: int) -> None:
        self.write(struct.pack("<q", int(value)))

    def u32(self, value: int) -> None:
        self.write(struct.pack("<I", int(value)))

    def u64(self, value: int) -> None:
        self.write(struct.pack("<Q", int(value)))

    def dotnet_string(self, value: str) -> None:
        raw = str(value).encode("utf-16-le")
        self._write_7bit_encoded_int(len(raw))
        self.write(raw)

    def _write_7bit_encoded_int(self, value: int) -> None:
        n = max(0, int(value))
        while n >= 0x80:
            self.byte((n & 0x7F) | 0x80)
            n >>= 7
        self.byte(n)

    def packed_int(self, value: int) -> None:
        self.write(_encode_packed_int(value))

    def save(self, save: NativeSave) -> bytes:
        self.u64(MAGIC)
        self.u32(VERSION_1808)
        # Optional header-offset table.  A zero-count table is accepted by the
        # matching reader and keeps generated 1808-compatible saves compact.
        self.u32(0)
        self.byte(save.file_type)
        self.i64(save.script_code)
        self.i64(save.script_version)
        self.dotnet_string(save.save_text)
        if save.file_type == SaveFileType.NORMAL:
            self.i64(len(save.characters))
            for ch in save.characters:
                self._write_character(ch)
        elif save.file_type not in {SaveFileType.GLOBAL, SaveFileType.VAR, SaveFileType.CHARVAR}:
            raise SaveFormatError(f"bad file type: {save.file_type}")
        if save.file_type in {SaveFileType.NORMAL, SaveFileType.GLOBAL}:
            self._write_variable_records(save.numeric, save.strings)
        else:
            self.variable_code(SaveDataType.EOF)
        return bytes(self.out)

    def variable_code(self, typ: int, name: str | None = None) -> None:
        self.byte(typ)
        if typ not in {SaveDataType.SEPARATOR, SaveDataType.EOC, SaveDataType.EOF}:
            self.dotnet_string(name or "")

    def _write_character(self, ch: CharacterState) -> None:
        fixed_records = self._variable_payloads(ch.numeric, ch.strings, include=self._is_fixed_chara_record)
        user_records = self._variable_payloads(ch.numeric, ch.strings, include=self._is_user_chara_record)
        self._write_payload_records(fixed_records)
        if user_records:
            # Emuera 1808 separates built-in character fields from user-defined
            # #DIM/#DIMS CHARADATA fields inside each character record.  The
            # reader already uses this marker to reject undeclared custom fields;
            # emitting it keeps exported saves layout-compatible with Emuera.
            self.variable_code(SaveDataType.SEPARATOR)
            self._write_payload_records(user_records)
        self.variable_code(SaveDataType.EOC)

    def _write_variable_records(
        self,
        numeric: dict[str, dict[tuple[int, ...], int]],
        strings: dict[str, dict[tuple[int, ...], str]],
        *,
        end_code: int = SaveDataType.EOF,
    ) -> int:
        records = self._variable_payloads(numeric, strings)
        self._write_payload_records(records)
        self.variable_code(end_code)
        return len(records)

    def _variable_payloads(
        self,
        numeric: dict[str, dict[tuple[int, ...], int]],
        strings: dict[str, dict[tuple[int, ...], str]],
        *,
        include: Any | None = None,
    ) -> list[tuple[str, int, bytes]]:
        records: list[tuple[str, int, bytes]] = []
        for key in sorted(numeric):
            nkey = norm_name(key)
            if include is not None and not include(nkey, False):
                continue
            payload = self._record_payload(key, numeric[key], is_string=False)
            if payload is None:
                continue
            typ, body = payload
            records.append((key, typ, body))
        for key in sorted(strings):
            nkey = norm_name(key)
            if include is not None and not include(nkey, True):
                continue
            payload = self._record_payload(key, strings[key], is_string=True)
            if payload is None:
                continue
            typ, body = payload
            records.append((key, typ, body))
        return records

    def _write_payload_records(self, records: list[tuple[str, int, bytes]]) -> None:
        for key, typ, body in records:
            self.variable_code(typ, key)
            self.write(body)

    def _is_user_chara_record(self, key: str, is_string: bool) -> bool:
        if key in CHARA_NUMERIC_ARRAYS or key in CHARA_STRING_ARRAYS:
            return False
        decl = self.program.var_decls.get(key) if self.program is not None else None
        return bool(decl and decl.charadata)

    def _is_fixed_chara_record(self, key: str, is_string: bool) -> bool:
        return not self._is_user_chara_record(key, is_string)

    def _record_payload(
        self,
        name: str,
        table: dict[tuple[int, ...], int | str],
        *,
        is_string: bool,
    ) -> tuple[int, bytes] | None:
        if not table:
            return None
        entries, scalar = _normalise_native_table(table, is_string=is_string)
        dim = max((len(idx) for idx in entries), default=0)
        if dim <= 0:
            if is_string:
                return SaveDataType.STR, _encode_dotnet_string(str(scalar if scalar is not None else ""))
            return SaveDataType.INT, _encode_packed_int(int(scalar or 0))
        if dim > 3:
            raise SaveFormatError(f"cannot write {dim}D native save array: {name}")
        normalized: dict[tuple[int, ...], int | str] = {}
        for idx, value in entries.items():
            full = tuple(int(i) for i in idx) + (0,) * max(0, dim - len(idx))
            if len(full) != dim or any(i < 0 for i in full):
                continue
            if is_string:
                if str(value) == "":
                    continue
                normalized[full] = str(value)
            else:
                n = int(value)
                if n == 0:
                    continue
                normalized[full] = n
        if not normalized:
            return None
        lengths = self._array_lengths(norm_name(name), normalized, dim)
        body = bytearray()
        for length in lengths:
            body += struct.pack("<i", max(0, int(length)))
        body += _encode_sparse_array_body(normalized, dim, is_string=is_string)
        if is_string:
            typ = [SaveDataType.STR_ARRAY, SaveDataType.STR_ARRAY_2D, SaveDataType.STR_ARRAY_3D][dim - 1]
        else:
            typ = [SaveDataType.INT_ARRAY, SaveDataType.INT_ARRAY_2D, SaveDataType.INT_ARRAY_3D][dim - 1]
        return typ, bytes(body)

    def _array_lengths(self, name: str, entries: dict[tuple[int, ...], int | str], dim: int) -> list[int]:
        lengths: list[int] = []
        if self.program is not None:
            decl = self.program.var_decls.get(norm_name(name))
            if decl and len(decl.dims) == dim and all(d >= 0 for d in decl.dims):
                lengths = [int(d) for d in decl.dims]
            elif self.program.csv and norm_name(name) in self.program.csv.variable_sizes:
                raw = self.program.csv.variable_sizes[norm_name(name)]
                if isinstance(raw, (tuple, list)) and len(raw) == dim:
                    lengths = [int(d) for d in raw]
        if not lengths:
            lengths = [0] * dim
        for idx in entries:
            for i, value in enumerate(idx):
                lengths[i] = max(lengths[i], int(value) + 1)
        return lengths


def _encode_dotnet_string(value: str) -> bytes:
    raw = str(value).encode("utf-16-le")
    n = len(raw)
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    out += raw
    return bytes(out)


def _encode_packed_int(value: int) -> bytes:
    n = int(value)
    if 0 <= n <= B.BYTE:
        return bytes([n])
    if -0x8000 <= n <= 0x7FFF:
        return bytes([B.INT16]) + struct.pack("<h", n)
    if -0x80000000 <= n <= 0x7FFFFFFF:
        return bytes([B.INT32]) + struct.pack("<i", n)
    return bytes([B.INT64]) + struct.pack("<q", n)


def _normalise_native_table(
    table: dict[tuple[int, ...], int | str],
    *,
    is_string: bool,
) -> tuple[dict[tuple[int, ...], int | str], int | str | None]:
    has_array = any(idx for idx in table)
    default: int | str = "" if is_string else 0
    scalar = table.get((), table.get((0,), default))
    if not has_array or set(table).issubset({(), (0,)}):
        return {}, scalar
    entries: dict[tuple[int, ...], int | str] = {}
    for idx, value in table.items():
        out_idx = (0,) if idx == () else tuple(int(i) for i in idx)
        if out_idx == (0,) and (0,) in entries:
            continue
        entries[out_idx] = value
    return entries, scalar


def _encode_sparse_array_body(
    entries: dict[tuple[int, ...], int | str],
    dim: int,
    *,
    is_string: bool,
) -> bytes:
    body = bytearray()
    cursor = [0, 0, 0]
    for idx in sorted(entries):
        target = list(idx) + [0] * (3 - len(idx))
        if dim >= 3 and target[0] > cursor[0]:
            body.append(B.ZERO_A2)
            body += _encode_packed_int(target[0] - cursor[0])
            cursor[0], cursor[1], cursor[2] = target[0], 0, 0
        if dim >= 2:
            axis = 0 if dim == 2 else 1
            if target[axis] > cursor[axis]:
                body.append(B.ZERO_A1)
                body += _encode_packed_int(target[axis] - cursor[axis])
                cursor[axis] = target[axis]
                cursor[axis + 1] = 0
        last = dim - 1
        if target[last] > cursor[last]:
            body.append(B.ZERO)
            body += _encode_packed_int(target[last] - cursor[last])
            cursor[last] = target[last]
        if target[:dim] != cursor[:dim]:
            # Sorted sparse records should never need to jump backwards; skip
            # duplicate aliases instead of emitting an invalid stream.
            continue
        value = entries[idx]
        if is_string:
            body.append(B.STRING)
            body += _encode_dotnet_string(str(value))
        else:
            body += _encode_packed_int(int(value))
        cursor[last] += 1
    body.append(B.EOD)
    return bytes(body)


def write_native_save(path: str | Path, save: NativeSave, program: Program | None = None) -> None:
    data = EraBinarySaveWriter(program).save(save)
    Path(path).write_bytes(data)


def native_save_from_json_obj(
    data: dict[str, Any],
    *,
    file_type: int = SaveFileType.NORMAL,
    save_text: str = "",
    script_code: int = 0,
    script_version: int = 0,
) -> NativeSave:
    def dec_map(raw: dict[str, dict[str, Any]], *, string_values: bool) -> dict[str, dict[tuple[int, ...], Any]]:
        out: dict[str, dict[tuple[int, ...], Any]] = {}
        for key, values in (raw or {}).items():
            table: dict[tuple[int, ...], Any] = {}
            for idx_text, value in (values or {}).items():
                idx = tuple(int(p) for p in str(idx_text).split("|") if p != "")
                table[idx] = str(value) if string_values else int(value)
            out[norm_name(key)] = table
        return out

    meta = data.get("_meta")
    meta_text = str(meta.get("text", "")) if isinstance(meta, dict) else ""
    save = NativeSave(
        file_type=file_type,
        script_code=script_code,
        script_version=script_version,
        save_text=save_text or meta_text,
        numeric=dec_map(data.get("numeric", {}), string_values=False),
        strings=dec_map(data.get("strings", {}), string_values=True),
    )
    if file_type == SaveFileType.NORMAL:
        for raw_ch in data.get("characters", []) or []:
            ch = CharacterState(template_no=int(raw_ch.get("template_no", -1)))
            ch.numeric = dec_map(raw_ch.get("numeric", {}), string_values=False)
            ch.strings = dec_map(raw_ch.get("strings", {}), string_values=True)
            save.characters.append(ch)
    return save


def native_save_from_memory(
    memory: Any,
    *,
    file_type: int = SaveFileType.NORMAL,
    save_text: str = "",
    script_code: int = 0,
    script_version: int = 0,
) -> NativeSave:
    data = memory.to_global_json_obj() if file_type == SaveFileType.GLOBAL else memory.to_json_obj()
    return native_save_from_json_obj(
        data,
        file_type=file_type,
        save_text=save_text,
        script_code=script_code,
        script_version=script_version,
    )


def read_native_save(path: str | Path, program: Program) -> NativeSave:
    reader = EraBinarySaveReader.from_path(path)
    file_type = reader.byte()
    if file_type not in {SaveFileType.NORMAL, SaveFileType.GLOBAL, SaveFileType.VAR, SaveFileType.CHARVAR}:
        raise SaveFormatError(f"bad file type: {file_type}")
    save = NativeSave(file_type=file_type)
    save.script_code = reader.i64()
    save.script_version = reader.i64()
    save.save_text = reader.dotnet_string()
    if file_type == SaveFileType.GLOBAL:
        _read_variable_records(reader, program, save.numeric, save.strings)
        return save
    if file_type != SaveFileType.NORMAL:
        raise SaveFormatError(f"unsupported file type: {file_type}")
    chara_count = reader.i64()
    for _ in range(max(0, int(chara_count))):
        save.characters.append(_read_character(reader, program))
    _read_variable_records(reader, program, save.numeric, save.strings)
    return save


def is_native_binary_save(path: str | Path) -> bool:
    try:
        with Path(path).open("rb") as f:
            return f.read(8) == struct.pack("<Q", MAGIC)
    except OSError:
        return False


def read_legacy_text_save(path: str | Path, program: Program) -> NativeSave:
    """Read enough of Emuera's old line-oriented text save format to migrate.

    eraMegaten still ships at least one pre-binary save slot.  The historical
    text format stores the fixed Era variable tables positionally and is not as
    self-describing as the 1808 binary format, but the leading character records
    are stable enough to recover the save metadata, roster, names and the most
    important character numeric tables.  The Emuera 1808 extended tail is also
    parsed by record name for character/user SAVEDATA variables that the current
    program declares.
    """

    lines = _read_legacy_text_lines(path)
    if len(lines) < 4:
        raise SaveFormatError("truncated legacy text save")
    save = NativeSave(
        file_type=SaveFileType.NORMAL,
        script_code=_legacy_int(lines[0]),
        script_version=_legacy_int(lines[1]),
        save_text=lines[2],
    )
    chara_count = max(0, _legacy_int(lines[3]))
    named_start = _legacy_named_start(lines)
    starts = _legacy_character_starts(lines, chara_count)
    for idx, start in enumerate(starts[:chara_count]):
        end = starts[idx + 1] if idx + 1 < len(starts) else named_start
        try:
            save.characters.append(_read_legacy_character(lines, start, end, program))
        except Exception:
            # Preserve slot order even when an old customized character record
            # is too malformed for partial migration.
            save.characters.append(CharacterState(template_no=-1))
    global_start = 4
    if starts:
        global_start = _legacy_after_blocks(
            lines,
            starts[min(len(starts), chara_count) - 1] + 4,
            named_start,
            len(LEGACY_CHARACTER_INT_ARRAYS),
        )
    _read_legacy_global_records(lines, global_start, named_start, save)
    _read_legacy_named_records(lines, program, save)
    return save


def read_legacy_text_global_save(path: str | Path, program: Program) -> NativeSave:
    """Read Emuera's old line-oriented global.sav format.

    A legacy global save contains the fixed GLOBAL/GLOBALS arrays first and,
    for newer text saves, an Emuera extended section with user-defined GLOBAL
    variables.  It has no script metadata or character roster.
    """

    lines = _read_legacy_text_lines(path)
    named_start = _legacy_named_start(lines)
    save = NativeSave(file_type=SaveFileType.GLOBAL)
    blocks = _legacy_split_blocks(lines[:named_start], max_blocks=2)
    if blocks:
        table = {(i,): _legacy_int(v, 0) for i, v in enumerate(blocks[0]) if _legacy_int(v, 0) != 0}
        if table:
            save.numeric["GLOBAL"] = table
    if len(blocks) >= 2:
        table = {(i,): v for i, v in enumerate(blocks[1]) if v}
        if table:
            save.strings["GLOBALS"] = table
    _read_legacy_named_records(lines, program, save)
    return save


def _read_legacy_text_lines(path: str | Path) -> list[str]:
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp932", "shift_jis", "utf-16"):
        try:
            return data.decode(enc).splitlines()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").splitlines()


def _legacy_int(text: str, default: int = 0) -> int:
    try:
        return int(str(text).strip(), 0)
    except Exception:
        try:
            return int(str(text).strip(), 10)
        except Exception:
            return default


def _legacy_is_int(text: str) -> bool:
    s = str(text).strip()
    if not s:
        return False
    try:
        int(s, 0)
        return True
    except Exception:
        return False


def _legacy_character_starts(lines: list[str], chara_count: int) -> list[int]:
    if chara_count <= 0 or len(lines) < 8:
        return []
    starts = [4]
    i = 8
    while i + 3 < len(lines) and len(starts) < chara_count:
        if (
            lines[i] != "__FINISHED"
            and not lines[i].startswith("__EMU_")
            and not _legacy_is_int(lines[i])
            and not _legacy_is_int(lines[i + 1])
            and _legacy_is_int(lines[i + 3])
        ):
            # Old text character records are separated by several
            # __FINISHED markers.  Requiring a short marker run before a
            # non-numeric name avoids false positives in CSTR prose.
            marker_run = 0
            j = i - 1
            while j >= 0 and lines[j] == "__FINISHED":
                marker_run += 1
                j -= 1
            if marker_run >= 3:
                starts.append(i)
        i += 1
    return starts


def _legacy_named_start(lines: list[str]) -> int:
    for i, line in enumerate(lines):
        if line in LEGACY_EMUERA_START_MARKERS:
            return i
    return len(lines)


def _legacy_after_blocks(lines: list[str], start: int, end: int, count: int) -> int:
    pos = max(0, start)
    for _ in range(max(0, count)):
        while pos < end and lines[pos] != "__FINISHED":
            if lines[pos].startswith("__EMUERA_"):
                return pos
            pos += 1
        if pos < end and lines[pos] == "__FINISHED":
            pos += 1
        else:
            return pos
    return pos


LEGACY_CHARACTER_INT_ARRAYS = (
    # Emuera's pre-binary CharacterData.SaveToStream writes two strings
    # (NAME/CALLNAME), two scalar ints (ISASSI/NO), then these saved
    # one-dimensional integer arrays in VariableCode order.
    "BASE",
    "MAXBASE",
    "ABL",
    "TALENT",
    "EXP",
    "MARK",
    "PALAM",
    "SOURCE",
    "EX",
    "CFLAG",
    "JUEL",
    "RELATION",
    "EQUIP",
    "TEQUIP",
    "STAIN",
    "GOTJUEL",
    "NOWEX",
)


LEGACY_GLOBAL_INT_ARRAYS = (
    # VariableData.SaveToStream writes legacy non-character saved integer
    # arrays by VariableCode index before the Emuera extended named section.
    "DAY",
    "MONEY",
    "ITEM",
    "FLAG",
    "TFLAG",
    "UP",
    "PALAMLV",
    "EXPLV",
    "EJAC",
    "DOWN",
    "RESULT",
    "COUNT",
    "TARGET",
    "ASSI",
    "MASTER",
    "NOITEM",
    "LOSEBASE",
    "SELECTCOM",
    "ASSIPLAY",
    "PREVCOM",
    "NOTUSE_14",
    "NOTUSE_15",
    "TIME",
    "ITEMSALES",
    "PLAYER",
    "NEXTCOM",
    "PBAND",
    "BOUGHT",
    "NOTUSE_1C",
    "NOTUSE_1D",
    "A",
    "B",
    "C",
    "D",
    "E",
    "F",
    "G",
    "H",
    "I",
    "J",
    "K",
    "L",
    "M",
    "N",
    "O",
    "P",
    "Q",
    "R",
    "S",
    "T",
    "U",
    "V",
    "W",
    "X",
    "Y",
    "Z",
    "NOTUSE_38",
    "NOTUSE_39",
    "NOTUSE_3A",
    "NOTUSE_3B",
)

LEGACY_GLOBAL_STRING_ARRAYS = ("SAVESTR",)


def _read_legacy_character(lines: list[str], start: int, end: int, program: Program) -> CharacterState:
    name = lines[start] if start < len(lines) else ""
    callname = lines[start + 1] if start + 1 < len(lines) else name
    is_assi = _legacy_int(lines[start + 2], 0) if start + 2 < len(lines) else 0
    no = _legacy_int(lines[start + 3], -1) if start + 3 < len(lines) else -1
    tmpl = None
    if program.csv:
        tmpl = program.csv.characters.get(no) or program.csv.sp_characters.get(no)
    ch = CharacterState.from_template(tmpl) if tmpl is not None else CharacterState(template_no=no)
    ch.template_no = no
    ch.numeric.setdefault("ISASSI", {})[()] = is_assi
    ch.numeric.setdefault("ISASSI", {})[(0,)] = is_assi
    ch.numeric.setdefault("NO", {})[()] = no
    ch.numeric.setdefault("NO", {})[(0,)] = no
    if name:
        ch.strings.setdefault("NAME", {})[()] = name
        ch.strings.setdefault("NAME", {})[(0,)] = name
    if callname:
        ch.strings.setdefault("CALLNAME", {})[()] = callname
        ch.strings.setdefault("CALLNAME", {})[(0,)] = callname
    blocks = _legacy_split_blocks(lines[start + 4 : end], max_blocks=len(LEGACY_CHARACTER_INT_ARRAYS))
    for var, block in zip(LEGACY_CHARACTER_INT_ARRAYS, blocks):
        values = [_legacy_int(v, 0) for v in block]
        table = {(i,): value for i, value in enumerate(values) if value}
        if table:
            ch.numeric[norm_name(var)] = table
    return ch


def _read_legacy_global_records(lines: list[str], start: int, end: int, save: NativeSave) -> None:
    if start >= end:
        return
    blocks = _legacy_split_blocks(
        lines[start:end],
        max_blocks=len(LEGACY_GLOBAL_INT_ARRAYS) + len(LEGACY_GLOBAL_STRING_ARRAYS),
    )
    for var, block in zip(LEGACY_GLOBAL_INT_ARRAYS, blocks):
        table = {(i,): _legacy_int(v, 0) for i, v in enumerate(block) if _legacy_int(v, 0) != 0}
        if table:
            save.numeric[norm_name(var)] = table
    offset = len(LEGACY_GLOBAL_INT_ARRAYS)
    for var, block in zip(LEGACY_GLOBAL_STRING_ARRAYS, blocks[offset:]):
        table = {(i,): str(v) for i, v in enumerate(block) if v}
        if table:
            save.strings[norm_name(var)] = table


def _legacy_split_blocks(segment: list[str], *, max_blocks: int) -> list[list[str]]:
    out: list[list[str]] = []
    cur: list[str] = []
    for line in segment:
        if line in LEGACY_EMUERA_START_MARKERS:
            break
        if line == "__FINISHED":
            out.append(cur)
            cur = []
            if len(out) >= max_blocks:
                break
        else:
            cur.append(line)
    if cur and len(out) < max_blocks:
        out.append(cur)
    return out


def _read_legacy_named_records(lines: list[str], program: Program, save: NativeSave) -> None:
    start = _legacy_named_start(lines)
    if start >= len(lines):
        return
    pos = start + 1
    chara_seen: dict[str, int] = {}
    while pos < len(lines):
        while pos < len(lines) and lines[pos] == "__EMU_SEPARATOR__":
            pos += 1
        if pos >= len(lines):
            break
        name = lines[pos]
        pos += 1
        values: list[str] = []
        while pos < len(lines) and lines[pos] not in {"__FINISHED", "__EMU_SEPARATOR__"}:
            values.append(lines[pos])
            pos += 1
        if pos < len(lines) and lines[pos] == "__FINISHED":
            pos += 1
        if not name:
            continue
        key = norm_name(name)
        decl = program.var_decls.get(key)
        if key in CHARA_STRING_ARRAYS or key in CHARA_NUMERIC_ARRAYS or (decl and decl.charadata):
            index = chara_seen.get(key, 0)
            chara_seen[key] = index + 1
            if 0 <= index < len(save.characters):
                is_string = key in CHARA_STRING_ARRAYS or bool(decl and decl.is_string)
                dims = decl.dims if decl and decl.dims else (len(values),)
                table = _legacy_values_to_table(values, dims=dims, is_string=is_string, keep_scalar_alias=False)
                if table:
                    dest = save.characters[index].strings if is_string else save.characters[index].numeric
                    dest[key] = table
            continue
        if decl is None:
            continue
        is_string = decl.is_string
        table = _legacy_values_to_table(values, dims=decl.dims, is_string=is_string, keep_scalar_alias=True)
        if not table:
            continue
        dest = save.strings if is_string else save.numeric
        dest[key] = table


def _legacy_values_to_table(
    values: list[str],
    *,
    dims: tuple[int, ...],
    is_string: bool,
    keep_scalar_alias: bool,
) -> dict[tuple[int, ...], Any]:
    table: dict[tuple[int, ...], Any] = {}
    if not dims:
        first = values[0] if values else ""
        if is_string:
            if first:
                table[()] = first
                if keep_scalar_alias:
                    table[(0,)] = first
        else:
            value = _legacy_int(first, 0)
            table[()] = value
            if keep_scalar_alias:
                table[(0,)] = value
        return table
    safe_dims = tuple(max(0, int(d)) for d in dims)
    if len(safe_dims) == 2 and any("," in raw for raw in values):
        return _legacy_2d_rows_to_table(values, safe_dims, is_string=is_string)
    if len(safe_dims) == 3 and any(raw.endswith("{") or raw == "}" for raw in values):
        return _legacy_3d_groups_to_table(values, safe_dims, is_string=is_string)
    for linear, raw in enumerate(values):
        if linear >= _legacy_dim_total(safe_dims):
            break
        if is_string:
            if raw == "":
                continue
            value: Any = raw
        else:
            value = _legacy_int(raw, 0)
            if value == 0:
                continue
        table[_legacy_unflatten_index(linear, safe_dims)] = value
    return table


def _legacy_parse_cell(raw: str, *, is_string: bool) -> Any | None:
    if is_string:
        return raw if raw != "" else None
    value = _legacy_int(raw, 0)
    return value if value != 0 else None


def _legacy_2d_rows_to_table(
    rows: list[str],
    dims: tuple[int, ...],
    *,
    is_string: bool,
) -> dict[tuple[int, ...], Any]:
    table: dict[tuple[int, ...], Any] = {}
    max_x = dims[0] if len(dims) >= 1 else 0
    max_y = dims[1] if len(dims) >= 2 else 0
    for x, row in enumerate(rows[:max_x]):
        if row == "":
            continue
        cells = row.split(",")
        for y, raw in enumerate(cells[:max_y]):
            value = _legacy_parse_cell(raw, is_string=is_string)
            if value is not None:
                table[(x, y)] = value
    return table


def _legacy_3d_groups_to_table(
    lines: list[str],
    dims: tuple[int, ...],
    *,
    is_string: bool,
) -> dict[tuple[int, ...], Any]:
    table: dict[tuple[int, ...], Any] = {}
    max_x = dims[0] if len(dims) >= 1 else 0
    max_y = dims[1] if len(dims) >= 2 else 0
    max_z = dims[2] if len(dims) >= 3 else 0
    x = -1
    y = 0
    implicit_x = 0
    in_group = False
    for raw in lines:
        line = raw.strip()
        if line.endswith("{"):
            prefix = line[:-1].strip()
            parsed_x = _legacy_int(prefix, implicit_x) if prefix else implicit_x
            x = parsed_x
            implicit_x = parsed_x + 1
            y = 0
            in_group = True
            continue
        if line == "}":
            in_group = False
            continue
        if not in_group or x < 0 or x >= max_x:
            continue
        if y >= max_y:
            y += 1
            continue
        if raw != "":
            cells = raw.split(",")
            for z, cell in enumerate(cells[:max_z]):
                value = _legacy_parse_cell(cell, is_string=is_string)
                if value is not None:
                    table[(x, y, z)] = value
        y += 1
    return table


def _legacy_dim_total(dims: tuple[int, ...]) -> int:
    total = 1
    for dim in dims:
        total *= max(0, int(dim))
    return total


def _legacy_unflatten_index(linear: int, dims: tuple[int, ...]) -> tuple[int, ...]:
    if not dims:
        return ()
    idx = [0] * len(dims)
    n = max(0, int(linear))
    for axis in range(len(dims) - 1, -1, -1):
        dim = max(1, int(dims[axis]))
        idx[axis] = n % dim
        n //= dim
    return tuple(idx)


def _read_variable_records(
    reader: EraBinarySaveReader,
    program: Program,
    numeric: dict[str, dict[tuple[int, ...], int]],
    strings: dict[str, dict[tuple[int, ...], str]],
) -> None:
    while True:
        typ, name = reader.variable_code()
        if typ == SaveDataType.EOF:
            return
        if typ in {SaveDataType.EOC, SaveDataType.SEPARATOR}:
            continue
        value = reader.read_value(typ)
        if not name:
            continue
        _store_value(program, norm_name(name), typ, value, numeric, strings)


def _read_character(reader: EraBinarySaveReader, program: Program) -> CharacterState:
    ch = CharacterState()
    user_defined = False
    while True:
        typ, name = reader.variable_code()
        if typ in {SaveDataType.EOF, SaveDataType.EOC}:
            break
        if typ == SaveDataType.SEPARATOR:
            user_defined = True
            continue
        value = reader.read_value(typ)
        if not name:
            continue
        key = norm_name(name)
        decl = program.var_decls.get(key)
        is_string = typ >= SaveDataType.STR
        if user_defined and not (decl and decl.charadata):
            continue
        if is_string:
            _store_chara_value(ch.strings, key, value)
        else:
            _store_chara_value(ch.numeric, key, value)
    ch.template_no = int(ch.numeric.get("NO", {}).get((), ch.numeric.get("NO", {}).get((0,), -1)))
    return ch


def _store_chara_value(table: dict[str, dict[tuple[int, ...], Any]], key: str, value: Any) -> None:
    dest = table.setdefault(key, {})
    if isinstance(value, dict):
        dest.update(value)
    else:
        dest[()] = value
        dest[(0,)] = value


def _store_value(
    program: Program,
    key: str,
    typ: int,
    value: dict[tuple[int, ...], int | str] | int | str,
    numeric: dict[str, dict[tuple[int, ...], int]],
    strings: dict[str, dict[tuple[int, ...], str]],
) -> None:
    is_string = typ >= SaveDataType.STR
    table = strings.setdefault(key, {}) if is_string else numeric.setdefault(key, {})
    if isinstance(value, dict):
        table.update(value)  # type: ignore[arg-type]
    else:
        table[()] = value  # type: ignore[assignment]
        table[(0,)] = value  # type: ignore[assignment]

import tempfile
import struct
import unittest
import contextlib
import io
from pathlib import Path

from eramegaten_engine.cli import main as cli_main
from eramegaten_engine.loader import load_program
from eramegaten_engine.runtime import EraRuntime
from eramegaten_engine.runtime import split_call_syntax
from eramegaten_engine.expr import eval_expr
from eramegaten_engine.memory import CharacterState
from eramegaten_engine.native_save import NativeSave, SaveDataType, SaveFileType, native_save_from_memory, read_legacy_text_global_save, read_legacy_text_save, read_native_save, write_native_save


class EngineSmokeTests(unittest.TestCase):
    def make_game(self, body: str):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text(body, encoding="utf-8")
        return td, load_program(root)

    def native_string(self, text: str) -> bytes:
        raw = text.encode("utf-16-le")
        n = len(raw)
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            if n:
                out.append(b | 0x80)
            else:
                out.append(b)
                break
        return bytes(out) + raw

    def native_int(self, value: int) -> bytes:
        if 0 <= value <= 0xCF:
            return bytes([value])
        if -0x8000 <= value <= 0x7FFF:
            return b"\xD0" + struct.pack("<h", value)
        if -0x80000000 <= value <= 0x7FFFFFFF:
            return b"\xD1" + struct.pack("<i", value)
        return b"\xD2" + struct.pack("<q", value)

    def native_record(self, typ: int, key: str, body: bytes) -> bytes:
        return bytes([typ]) + self.native_string(key) + body

    def native_int_array(self, values: list[int]) -> bytes:
        return struct.pack("<i", len(values)) + b"".join(self.native_int(v) for v in values) + b"\xFF"

    def native_str_array(self, values: list[str]) -> bytes:
        body = bytearray(struct.pack("<i", len(values)))
        for value in values:
            if value:
                body += b"\xD8" + self.native_string(value)
            else:
                body += b"\xF0\x01"
        body += b"\xFF"
        return bytes(body)

    def native_header(self, file_type: int, text: str = "") -> bytes:
        return (
            b"\x89ERA\r\n\x1a\n"
            + struct.pack("<I", 1808)
            + struct.pack("<I", 0)
            + bytes([file_type])
            + struct.pack("<q", 666)
            + struct.pack("<q", 309145)
            + self.native_string(text)
        )

    def test_expression_ops(self):
        td, program = self.make_game("@MAIN\nRETURN 0\n")
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        self.assertEqual(eval_expr(rt, "1 + 2 * 3"), 7)
        self.assertEqual(eval_expr(rt, "1 !| 0"), 0)  # NOR
        self.assertEqual(eval_expr(rt, "1 !& 0"), 1)  # NAND
        self.assertEqual(eval_expr(rt, '"x" * 3'), "xxx")
        self.assertEqual(eval_expr(rt, "1 ? 20 # 30"), 20)
        self.assertEqual(eval_expr(rt, "00309145"), 309145)

    def test_nested_quotes_inside_form_string_expression(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML {TOINT(AUTO_SPLIT(@"{COLOR("AQUA")}_{COLOR("WHITE")}_{COLOR("RED")}" , "_" , 1))}
RETURN

@AUTO_SPLIT(ARGS, ARGS:1, ARG)
#FUNCTIONS
SPLIT ARGS, ARGS:1, LOCALS
RETURNF LOCALS:ARG
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(len(rt.warnings), 0)
        self.assertRegex("".join(rt.output), r"\d+")

    def test_nested_quotes_inside_percent_form_string_expression(self):
        td, program = self.make_game('''@SYSTEM_TITLE
FLAG:現ダンジョン = 1
LOCALS = @"ダンジョン%TOSTR(FLAG:現ダンジョン , "00")%"
PRINTFORML %LOCALS%
CALL ECHO, @"ダンジョン%TOSTR(FLAG:現ダンジョン,"00")%"
RETURN

@ECHO(ARGS)
PRINTFORML /%ARGS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.warnings, [])
        self.assertIn("ダンジョン01\n/ダンジョン01", "".join(rt.output))

    def test_returnf_renders_bare_form_condition_without_warning(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL SETCALL, "あなた"
PRINTFORML [%MYNAME("貴方", "様")%]
CALL SETCALL, "太郎"
PRINTFORML [%MYNAME("貴方", "様")%]
PRINTFORML M={MODVAL(17, 5)}
RETURN

@SETCALL(ARGS)
CALLNAME:MASTER = %ARGS%
RETURN

@MYNAME(ARGS, ARGS:1)
#FUNCTIONS
RETURNF \\@CALLNAME:MASTER == "あなた" ? %ARGS% # %CALLNAME:MASTER%%ARGS:1%\\@

@MODVAL(ARG, ARG:1)
#FUNCTION
RETURNF ARG % ARG:1
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "[貴方]\n[太郎様]\nM=2\n")

    def test_percent_wrapped_form_condition_outputs_literal_branch(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS S
S = "ok"
FLAG:0 = 1
PRINTFORML A%\\@GETBIT(FLAG:0, 0) ?(予約中)#\\@%B
PRINTFORML C%\\@0 ?bad#%S%\\@%D
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "A(予約中)B\nCokD\n")

    def test_bit_commands_and_functions_accept_multiple_bit_arguments(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM A
SETBIT A, 1, 3, 5
PRINTFORML S={A}:{GETBIT(A,1)}:{GETBIT(A,3)}:{GETBIT(A,5)}
CLEARBIT A, 1, 5
PRINTFORML C={A}
INVERTBIT A, 0, 3
PRINTFORML I={A}
PRINTFORML F={SETBIT(0, 2, 4)}:{CLEARBIT(31, 1, 3)}:{INVERTBIT(0, 0, 2)}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "S=42:1:1:1\nC=8\nI=1\nF=20:21:5\n")
        self.assertEqual(rt.warnings, [])

    def test_bare_variable_and_zero_index_are_aliases(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM A, 5
#DIMS S, 5
A:0 = 7
PRINTFORML A={A}
A = 9
PRINTFORML A0={A:0}
S:0 = "x"
PRINTFORML S=%S%
S = "y"
PRINTFORML S0=%S:0%
ADDVOIDCHARA
CFLAG:0:0 = 42
PRINTFORML C={CFLAG:0}
CSTR:0:0 = "n"
PRINTFORML CS=%CSTR:0%
ENCODETOUNI "AZ"
PRINTFORML U={RESULT}:{RESULT:0}:{RESULT:1}:{RESULT:2}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "A=7\nA0=9\nS=x\nS0=y\nC=42\nCS=n\nU=2:2:65:90\n")
        self.assertEqual(rt.warnings, [])

    def test_strlen_locale_and_unicode_variants(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML L={STRLEN("A中あ")}:{STRLENS("A中あ")}:{STRLENSU("A中あ")}
PRINTFORML LF={STRLENFORM("A{1}中")}:{STRLENFORMU("A{1}中")}
STRLENFORM A{1}中
PRINTFORML F={RESULT}
STRLENFORMU A{1}中
PRINTFORML FU={RESULT}
RETURN
''')
        (program.root / "emuera.config").write_text("内部で使用する東アジア言語:CHINESE_HANS\n", encoding="utf-8")
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "L=5:5:3\nLF=4:3\nF=4\nFU=3\n")
        self.assertEqual(rt.warnings, [])

    def test_expression_function_omitted_arguments(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML {F(7, , , , 9)}:{F(, 3)}
RETURN

@F(ARG, ARG:1, ARG:2, ARG:3, ARG:4)
#FUNCTION
RETURNF ARG + ARG:1 + ARG:2 + ARG:3 + ARG:4
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("16:3", "".join(rt.output))

    def test_omitted_string_arguments_bind_as_blank(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL ECHO, , 5
PRINTFORML F={IS_EMPTY(, 5)}
RETURN

@ECHO(ARGS, ARG)
PRINTFORML A="%ARGS%":{ARG}
RETURN

@IS_EMPTY(ARGS, ARG)
#FUNCTION
RETURNF ARGS == "" && ARG == 5
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn('A="":5', out)
        self.assertIn("F=1", out)

    def test_omitted_arguments_use_header_defaults_when_declared(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL MESSAGE_LIKE, "speaker", "a/b", , , , , 4
PRINTFORML EF={NUM_DEFAULT(, 3)}:{NUM_DEFAULT(7,)}:%STR_DEFAULT(, "B")%:%STR_DEFAULT("A",)%
RETURN

@MESSAGE_LIKE(ARGS, ARGS:1, SEP = "/" , OPTIONS = "default/options" , POS, WIDTH = 72, ROWS = -1)
#DIMS PARTS, 4
SPLIT ARGS:1, SEP, PARTS
PRINTFORML %ARGS%|%SEP%|%OPTIONS%|%POS%|{WIDTH}|{ROWS}|{RESULT}|%PARTS:0%:%PARTS:1%
RETURN

@NUM_DEFAULT(ARG = 5, ARG:1 = 9)
#FUNCTION
RETURNF ARG * 10 + ARG:1

@STR_DEFAULT(ARGS = "A", ARGS:1 = "Z")
#FUNCTIONS
RETURNF ARGS + ARGS:1
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "speaker|/|default/options||72|4|2|a:b\nEF=53:79:AB:AZ\n")

    def test_arg_and_args_header_scalars_alias_index_zero(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL ECHO, "first", 7
RETURN

@ECHO(ARGS, ARG)
PRINTFORML %ARGS%/%ARGS:0%:{ARG}/{ARG:0}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "first/first:7/7\n")

    def test_dpoint_shorthand_uses_current_dungeon_name(self):
        td, program = self.make_game('''@SYSTEM_TITLE
FLAG:現ダンジョン = 1
CALLF DPOINT("=" , 2 , 5 , 7)
PRINTFORML %GLOBALS:0%
RETURN

@DPOINT(ARGS = "GET" , ARG = -1 , ARG:1 = -1 , ARG:2 = -1 , ARG:3 = -1 , ARGS:1 = "")
#FUNCTION
GLOBALS:0 = %ARGS:1%
RETURNF 0
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("ダンジョン01", "".join(rt.output))

    def test_assignment_variable_name_can_start_with_print(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS PRINT_MODE
PRINT_MODE '= "silent"
PRINTFORML %PRINT_MODE%
PRINTFORML A={1 == 1}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "silent\nA=1\n")

    def test_debugprint_is_recognized_and_kept_out_of_game_output(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM X = 7
DEBUGPRINTL hidden plain
DEBUGPRINTFORML hidden form {X + 1}
PRINTL visible
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "visible\n")
        self.assertEqual(rt.debug_output, [])
        self.assertEqual(rt.warnings, [])

        td2, program2 = self.make_game('''@SYSTEM_TITLE
#DIM X = 7
DEBUGPRINTFORM plain-%X%
DEBUGPRINTFORML /form-{X + 1}
PRINTL visible
RETURN
''')
        self.addCleanup(td2.cleanup)
        (Path(td2.name) / "emuera.config").write_text("デバッグコマンドを使用する:YES\n", encoding="utf-8")
        rt2 = EraRuntime(program2, echo=False, interactive=False)
        rt2.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt2.output), "visible\n")
        self.assertEqual("".join(rt2.debug_output), "plain-7/form-8\n")
        self.assertEqual(rt2.warnings, [])

    def test_string_assignments_keep_parenthetical_text_labels_literal(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS RESULTS
#DIMS TSTR, 10
RESULTS = 生命消耗(小)
TSTR:0 = 戶外PLAY(帰宅)
RESULTS:1 = 要触媒：英雄亚瑟(陥落済)　未所持
PRINTFORML %RESULTS%|%TSTR:0%|%RESULTS:1%
RESULTS = SUBSTRING("abcdef", 1, 3)
PRINTFORML %RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(
            "".join(rt.output),
            "生命消耗(小)|戶外PLAY(帰宅)|要触媒：英雄亚瑟(陥落済)　未所持\nbcd\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_string_assignment_parenthesized_form_text_renders_literal_parens(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS LOCALS
#DIM LOCAL, 4
LOCAL:3 = 24
LOCALS = ({LOCAL:3}/12)
PRINTFORML %LOCALS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "(24/12)\n")
        self.assertEqual(rt.warnings, [])

    def test_string_assignment_parenthesized_lvalue_rhs_evaluates(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS TEMPS, 10
#DIMS GDS, 10, 10
#DIM LCOUNT
TEMPS:0 = 魔貨
TEMPS:1 = 100
LCOUNT = 0
GDS:LCOUNT:0 '= TEMPS:(LCOUNT * 2)
PRINTFORML %GDS:0:0%/{TOINT(TEMPS:(LCOUNT * 2 + 1))}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "魔貨/100\n")
        self.assertEqual(rt.warnings, [])

    def test_call_arguments_render_bare_form_conditionals(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL F, "TALENT", \\@1 ? 討厭男人 # 討厭女人 \\@, \\@0 ? 7 # 9 \\@
RETURN

@F(ARGS, ARGS:1, ARG)
PRINTFORML %ARGS%/%ARGS:1%:{ARG}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "TALENT/討厭男人:9\n")
        self.assertEqual(rt.warnings, [])

    def test_callform_conditional_target_trims_syntax_padding(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM U
#DIM MASTER
U = 0
MASTER = 0
CALLFORM PRINT_%\\@ U == MASTER ? MASTER # SLAVE \\@%_STATUS
RETURN

@PRINT_MASTER_STATUS
PRINTL master
RETURN

@PRINT_SLAVE_STATUS
PRINTL slave
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "master\n")
        self.assertEqual(rt.warnings, [])

    def test_return_multiple_values_populate_result_arrays(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL NUM
PRINTFORML N={RESULT}:{RESULT:0}:{RESULT:1}:{RESULT:2}
CALL STRS
PRINTFORML S=%RESULTS%/%RESULTS:0%/%RESULTS:1%/%RESULTS:2%
CALL SINGLE
PRINTFORML A={RESULT}:{RESULT:0}
RETURN

@NUM
RETURN 7, 8 + 1, 11

@STRS
RETURNFORM A{1}, \\@0 ? bad # ok\\@, %TOSTR(3)%

@SINGLE
RETURN 5
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "N=7:7:9:11\nS=A1/A1/ok/3\nA=5:5\n")
        self.assertEqual(rt.warnings, [])

    def test_html_print_evaluates_expression_variables_and_printlc(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS SHOW_LINE
SHOW_LINE = "html"
HTML_PRINT SHOW_LINE
HTML_PRINT GETLINESTR("-") + "<br>"
HTML_PRINT "[7] html button"
PRINTLC done
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.line_width = 3
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("html\n---<br>\n[7] html button\ndone", out)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_html_print_choices_do_not_autoselect(self):
        td, program = self.make_game('''@SYSTEM_TITLE
HTML_PRINT "[7] html choice"
INPUT
PRINTL bad
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "[7] html choice\n")

    def test_noninteractive_string_printbutton_waits_for_explicit_input(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTBUTTON "[loop]", "loop"
PRINTBUTTON "[exit]", "exit"
INPUTS
PRINTL bad
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        steps = rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertLess(steps, 100)
        self.assertEqual("".join(rt.output), "[loop][exit]")
        self.assertEqual(rt.warnings, [])

        rt2 = EraRuntime(program, echo=False, interactive=False, inputs=["loop"])
        rt2.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt2.output), "[loop][exit]bad\n")
        self.assertEqual(rt2.warnings, [])

    def test_html_print_records_button_values_without_keyboard_autoselect(self):
        td, program = self.make_game('''@SYSTEM_TITLE
HTML_PRINT "<button value='buy/7' title='Buy &amp; Use'><font color='red'>[Buy]</font></button>"
HTML_PRINT "<button value=100>Plain</button>"
INPUTS
PRINTFORML picked=%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.html_buttons, [
            {"value": "buy/7", "title": "Buy & Use", "pos": "", "label": "[Buy]"},
            {"value": "100", "title": "", "pos": "", "label": "Plain"},
        ])
        self.assertEqual(rt.pending_buttons, [])
        self.assertEqual("".join(rt.output), "<button value='buy/7' title='Buy &amp; Use'><font color='red'>[Buy]</font></button>\n<button value=100>Plain</button>\n")

    def test_html_layout_suppresses_raw_html_transcript_text_drawables(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTS "P:"
HTML_PRINT "<button value='go'>Go</button><font color='#00ff00'>Tail</font>"
HTML_PRINT "<button value='solo'>Solo</button>"
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "P:<button value='go'>Go</button><font color='#00ff00'>Tail</font>\n<button value='solo'>Solo</button>\n")
        page = rt.html_page_model()
        self.assertEqual([(span["display_line"], span["col"], span["text"]) for span in page["style_spans"]], [(1, 0, "P:")])
        self.assertEqual([(run["display_line"], run["col"], run["text"]) for run in page["html_text"]], [(1, 4, "Tail")])
        layout = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual([(text["text"], text["x"]) for text in layout["texts"]], [("P:", 0)])
        self.assertEqual([(run["text"], run["x"], run["color"]) for run in layout["html_text"]], [("Tail", 32, 0x00FF00)])
        self.assertEqual([(button["value"], button["x"], button["width"]) for button in layout["buttons"]], [("go", 16, 16), ("solo", 0, 32)])
        self.assertFalse(any("<button" in str(item.get("text", "")) for item in layout["drawables"]))
        self.assertLess(layout["canvas"]["width"], len("P:<button value='go'>Go</button><font color='#00ff00'>Tail</font>") * 8)
        self.assertEqual(rt.html_click_value(17, 1, char_width=8, line_height=20), "go")
        self.assertEqual(rt.html_click_value(1, 21, char_width=8, line_height=20), "solo")
        self.assertEqual(rt.warnings, [])

    def test_html_buttons_without_value_are_drawable_but_not_clickable(self):
        td, program = self.make_game('''@SYSTEM_TITLE
HTML_PRINT "<nobr><button title='tip'>Head</button><button>Plain</button><s><button>[Off]</button></s><button value='go'>Go</button>"
INPUTS
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        page = rt.html_page_model()
        self.assertEqual([(button["value"], button["title"], button["label"]) for button in page["buttons"]], [
            ("", "tip", "Head"),
            ("", "", "Plain"),
            ("", "", "[Off]"),
            ("go", "", "Go"),
        ])
        self.assertEqual(page["html_text"], [])
        self.assertEqual(page["buttons"][2]["font_style"], 8)
        layout = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual([(button["value"], button["x"], button["width"]) for button in layout["buttons"]], [
            ("", 0, 32),
            ("", 32, 40),
            ("", 72, 40),
            ("go", 112, 16),
        ])
        hit = rt.html_hit_test(73, 1, char_width=8, line_height=20)
        self.assertEqual((hit["type"], hit["value"], hit["button_value"]), ("button", "", ""))
        self.assertIsNone(rt.html_click_value(73, 1, char_width=8, line_height=20))
        self.assertEqual(rt.html_click_value(113, 1, char_width=8, line_height=20), "go")
        self.assertEqual(rt.pending_buttons, [])
        self.assertEqual(rt.warnings, [])

    def test_html_font_and_style_tags_inherit_to_gui_button_drawables(self):
        td, program = self.make_game('''@SYSTEM_TITLE
HTML_PRINT "<font color='#00ff00'><button value='outer'>Outer</button></font>"
HTML_PRINT "<button value='inner'><font color='0xFF0000'><b>Inner</b></font></button><nonbutton><i>Info</i></nonbutton>"
INPUTS
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.html_buttons, [
            {"value": "outer", "title": "", "pos": "", "label": "Outer"},
            {"value": "inner", "title": "", "pos": "", "label": "Inner"},
        ])
        page = rt.html_page_model()
        self.assertEqual([button["color"] for button in page["buttons"]], [0x00FF00, 0xFF0000])
        self.assertEqual([button["font_style"] for button in page["buttons"]], [0, 1])
        self.assertEqual(page["nonbuttons"][0]["font_style"], 2)
        layout = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual([(button["value"], button["color"], button["font_style"]) for button in layout["buttons"]], [
            ("outer", 0x00FF00, 0),
            ("inner", 0xFF0000, 1),
        ])
        self.assertEqual(layout["nonbuttons"][0]["font_style"], 2)
        self.assertEqual(rt.warnings, [])

    def test_html_styled_text_runs_are_exposed_outside_controls(self):
        td, program = self.make_game('''@SYSTEM_TITLE
HTML_PRINT "<font color='#00ff00'>■</font>NORMAL<br><b>Bold</b><button value='x'>Skip</button><nonbutton>Info</nonbutton><font color='#ff0000'>Tail</font>"
INPUTS
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        page = rt.html_page_model()
        self.assertEqual([(run["display_line"], run["col"], run["text"]) for run in page["html_text"]], [
            (1, 0, "■"),
            (1, 1, "NORMAL"),
            (2, 0, "Bold"),
            (2, 12, "Tail"),
        ])
        self.assertEqual([run["color"] for run in page["html_text"]], [0x00FF00, rt.default_color, rt.default_color, 0xFF0000])
        self.assertEqual([run["font_style"] for run in page["html_text"]], [0, 0, 1, 0])
        self.assertEqual(page["buttons"][0]["col"], 4)
        self.assertEqual(page["nonbuttons"][0]["col"], 8)
        layout = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual([(run["text"], run["x"], run["y"], run["color"], run["font_style"]) for run in layout["html_text"]], [
            ("■", 0, 0, 0x00FF00, 0),
            ("NORMAL", 8, 0, rt.default_color, 0),
            ("Bold", 0, 20, rt.default_color, 1),
            ("Tail", 96, 20, 0xFF0000, 0),
        ])
        self.assertEqual(layout["buttons"][0]["x"], 32)
        self.assertEqual(layout["nonbuttons"][0]["x"], 64)
        self.assertNotIn("Skip", [run["text"] for run in page["html_text"]])
        self.assertNotIn("Info", [run["text"] for run in page["html_text"]])
        self.assertEqual(rt.warnings, [])

    def test_inline_html_images_advance_following_text_and_controls(self):
        td, program = self.make_game('''@SYSTEM_TITLE
HTML_PRINT "A<img src='Pic' width='16' height='8'>B<button value='go'>Go</button>"
INPUTS
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        page = rt.html_page_model()
        self.assertEqual([(run["col"], run["text"]) for run in page["html_text"]], [(0, "A"), (3, "B")])
        self.assertEqual(page["images"][0]["col"], "1")
        self.assertEqual(page["buttons"][0]["col"], 4)
        layout = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual((layout["images"][0]["x"], layout["images"][0]["width"]), (8, 16))
        self.assertEqual([(run["text"], run["x"]) for run in layout["html_text"]], [("A", 0), ("B", 24)])
        self.assertEqual((layout["buttons"][0]["x"], layout["buttons"][0]["value"]), (32, "go"))
        self.assertEqual(rt.html_click_value(33, 1, char_width=8, line_height=20), "go")
        self.assertEqual(rt.warnings, [])

    def test_image_only_html_controls_expand_to_child_image_bounds(self):
        td, program = self.make_game('''@SYSTEM_TITLE
HTML_PRINT "<button value='pic' title='Face'><img src='Pic' width='16' height='8'></button><button value='next'>N</button><nonbutton title='Still'><img src='Pic' width='8' height='8'></nonbutton>"
INPUTS
RETURN
''')
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "resources").mkdir()
        (root / "resources" / "画像.csv").write_text("Pic,img.png,0,0,16,8\n", encoding="utf-8")
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        page = rt.html_page_model()
        self.assertEqual([(img["parent"], img["parent_title"], img["col"]) for img in page["images"]], [("button", "Face", "0"), ("nonbutton", "Still", "3")])
        layout = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual([(button["value"], button["x"], button["width"], button["height"]) for button in layout["buttons"]], [
            ("pic", 0, 16, 20),
            ("next", 16, 8, 20),
        ])
        self.assertEqual([(non["title"], non["x"], non["width"], non["height"]) for non in layout["nonbuttons"]], [("Still", 24, 8, 20)])
        self.assertEqual(rt.html_click_value(1, 1, char_width=8, line_height=20), "pic")
        self.assertEqual(rt.html_click_value(17, 1, char_width=8, line_height=20), "next")
        self.assertIsNone(rt.html_click_value(25, 1, char_width=8, line_height=20))
        self.assertEqual(rt.warnings, [])

    def test_scaled_inline_html_images_advance_following_elements_by_scaled_width(self):
        td, program = self.make_game('''@SYSTEM_TITLE
HTML_PRINT "A<img src='Pic' width='150' height='100'>B<button value='go'>Go</button>"
HTML_PRINT "<img src='Pic' height='100'>T"
INPUTS
RETURN
''')
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "resources").mkdir()
        (root / "resources" / "画像.csv").write_text("Pic,img.png,0,0,64,16\n", encoding="utf-8")
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        page = rt.html_page_model()
        self.assertEqual([(run["display_line"], run["col"], run["text"]) for run in page["html_text"]], [(1, 0, "A"), (1, 20, "B"), (2, 8, "T")])
        self.assertEqual([img["col"] for img in page["images"]], ["1", "0"])
        raw = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual([(run["text"], run["x"]) for run in raw["html_text"]], [("A", 0), ("B", 158), ("T", 400)])
        scaled = rt.html_layout_model(char_width=8, line_height=20, html_unit_scale=0.2)
        self.assertEqual((scaled["images"][0]["x"], scaled["images"][0]["width"]), (8, 30))
        self.assertEqual((scaled["images"][1]["x"], scaled["images"][1]["width"]), (0, 80))
        self.assertEqual([(run["text"], run["x"]) for run in scaled["html_text"]], [("A", 0), ("B", 38), ("T", 80)])
        self.assertEqual((scaled["buttons"][0]["x"], scaled["buttons"][0]["value"]), (46, "go"))
        self.assertEqual(rt.html_click_value(47, 1, char_width=8, line_height=20, html_unit_scale=0.2), "go")
        self.assertIsNone(rt.html_click_value(47, 1, char_width=8, line_height=20))
        self.assertEqual(rt.warnings, [])

    def test_html_align_shifts_unpositioned_inline_drawables_with_viewport(self):
        td, program = self.make_game('''@SYSTEM_TITLE
HTML_PRINT "<p align='center'><nobr><img src='Pic' width='16' height='8'>T<button value='go'>Go</button>"
HTML_PRINT "<p align='right'>R"
INPUTS
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        page = rt.html_page_model()
        self.assertEqual(page["images"][0]["alignment"], "CENTER")
        self.assertEqual(page["html_text"][0]["alignment"], "CENTER")
        self.assertEqual(page["buttons"][0]["alignment"], "CENTER")
        self.assertEqual(page["html_text"][1]["alignment"], "RIGHT")
        raw_layout = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual((raw_layout["images"][0]["x"], raw_layout["buttons"][0]["x"]), (0, 24))
        layout = rt.html_layout_model(char_width=8, line_height=20, viewport_width=100)
        self.assertEqual((layout["images"][0]["x"], layout["html_text"][0]["x"], layout["buttons"][0]["x"]), (30, 46, 54))
        self.assertEqual((layout["html_text"][1]["x"], layout["html_text"][1]["text"]), (92, "R"))
        self.assertEqual(rt.html_click_value(55, 1, char_width=8, line_height=20, viewport_width=100), "go")
        self.assertEqual(rt.warnings, [])

    def test_html_print_records_image_metadata_for_gui_frontends(self):
        td, program = self.make_game('''@SYSTEM_TITLE
HTML_PRINT "<img src='A1_0_1' title='Face &amp; Body' pos=0 width=180 height='45'><br>"
HTML_PRINT "<button value='ok' pos='200'><img src=\\"B2\\" width=16 height=12></button><nonbutton pos='201' title='Still'><img src='A1_1_1' width=50 height=25 ypos='-10'>Label</nonbutton>"
INPUTS
RETURN
''')
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "resources").mkdir()
        (root / "resources" / "画像.csv").write_text("A1_0_1,img.png,0,0,180,45\nA1_1_1,img.png,0,45,180,30\n", encoding="utf-8")
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.html_images, [
            {
                "src": "A1_0_1", "title": "Face & Body", "pos": "0", "width": "180", "height": "45", "ypos": "",
                "col": "0",
                "natural_width": "180", "natural_height": "45", "parent": "", "parent_pos": "", "parent_title": "", "parent_value": "",
            },
            {
                "src": "B2", "title": "", "pos": "", "width": "16", "height": "12", "ypos": "",
                "col": "",
                "natural_width": "", "natural_height": "", "parent": "button", "parent_pos": "200", "parent_title": "", "parent_value": "ok",
            },
            {
                "src": "A1_1_1", "title": "", "pos": "", "width": "50", "height": "25", "ypos": "-10",
                "col": "",
                "natural_width": "180", "natural_height": "30", "parent": "nonbutton", "parent_pos": "201", "parent_title": "Still", "parent_value": "",
            },
        ])
        self.assertEqual(rt.html_buttons, [{"value": "ok", "title": "", "pos": "200", "label": ""}])
        self.assertEqual(rt.html_nonbuttons, [{"title": "Still", "pos": "201", "label": "Label"}])
        self.assertEqual(rt.pending_buttons, [])
        page = rt.html_page_model()
        self.assertEqual(len(page["lines"]), 3)
        self.assertEqual(page["lines"][0]["images"][0]["src"], "A1_0_1")
        self.assertEqual(page["lines"][2]["buttons"][0]["value"], "ok")
        self.assertEqual([img["src"] for img in page["lines"][2]["images"]], ["B2", "A1_1_1"])
        self.assertEqual(page["images"][2]["display_line"], 3)
        self.assertEqual(page["html"][0]["display_line"], 1)
        self.assertEqual(page["html"][0]["display_end_line"], 2)
        layout = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual(layout["rows"][1]["y"], 20)
        self.assertEqual(layout["rows"][2]["y"], 40)
        self.assertEqual(
            [(img["src"], img["x"], img["y"], img["width"], img["height"]) for img in layout["images"]],
            [("A1_0_1", 0, 0, 180, 45), ("B2", 200, 40, 16, 12), ("A1_1_1", 201, 30, 50, 25)],
        )
        self.assertEqual(layout["buttons"][0]["x"], 200)
        self.assertGreaterEqual(layout["canvas"]["height"], 45)
        self.assertEqual(rt.html_click_value(200, 45, char_width=8, line_height=20), "ok")
        self.assertEqual(rt.html_hit_test(220, 35, char_width=8, line_height=20)["parent"], "nonbutton")
        self.assertIsNone(rt.html_click_value(220, 35, char_width=8, line_height=20))
        self.assertEqual(rt.html_hit_test(210, 45, char_width=8, line_height=20)["parent"], "nonbutton")
        self.assertEqual(rt.html_click_value(210, 45, char_width=8, line_height=20), "ok")

    def test_html_layout_can_scale_explicit_emuera_html_units(self):
        td, program = self.make_game('''@SYSTEM_TITLE
HTML_PRINT "<nobr><nonbutton pos='200'><img src='Pic' width='150' height='100' ypos='-50'></nonbutton><button value='go' pos='400'>Go</button>"
HTML_PRINT "<img src='Pic' height='100'>"
INPUTS
RETURN
''')
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "resources").mkdir()
        (root / "resources" / "画像.csv").write_text("Pic,img.png,0,0,64,16\n", encoding="utf-8")
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        raw = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual((raw["images"][0]["x"], raw["images"][0]["y"], raw["images"][0]["width"], raw["images"][0]["height"]), (200, -50, 150, 100))
        scaled = rt.html_layout_model(char_width=8, line_height=20, html_unit_scale=0.2)
        self.assertEqual((scaled["images"][0]["x"], scaled["images"][0]["y"], scaled["images"][0]["width"], scaled["images"][0]["height"]), (40, -10, 30, 20))
        self.assertEqual((scaled["images"][1]["x"], scaled["images"][1]["y"], scaled["images"][1]["width"], scaled["images"][1]["height"]), (0, 20, 80, 20))
        self.assertEqual(scaled["nonbuttons"][0]["x"], 40)
        self.assertEqual(scaled["buttons"][0]["x"], 80)
        self.assertEqual(rt.html_hit_test(45, -5, char_width=8, line_height=20, html_unit_scale=0.2)["parent"], "nonbutton")
        self.assertEqual(rt.html_click_value(81, 1, char_width=8, line_height=20, html_unit_scale=0.2), "go")
        self.assertIsNone(rt.html_click_value(81, 1, char_width=8, line_height=20))
        self.assertEqual(rt.warnings, [])

    def test_html_print_br_tags_advance_visual_layout_lines(self):
        td, program = self.make_game('''@SYSTEM_TITLE
HTML_PRINT "top<br><button value='down'>Down</button><br><nonbutton pos='10'>Info</nonbutton>"
PRINTL after
INPUTS
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        page = rt.html_page_model()
        self.assertEqual(page["html"][0]["display_line"], 1)
        self.assertEqual(page["html"][0]["display_end_line"], 3)
        self.assertEqual(page["buttons"][0]["display_line"], 2)
        self.assertEqual(page["nonbuttons"][0]["display_line"], 3)
        self.assertEqual(page["style_spans"][0]["display_line"], 4)
        layout = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual(layout["buttons"][0]["y"], 20)
        self.assertEqual(layout["nonbuttons"][0]["y"], 40)
        self.assertEqual([d["text"] for d in layout["texts"][-1:]], ["after"])
        self.assertEqual(layout["texts"][-1]["y"], 60)
        self.assertEqual(rt.html_click_value(4, 25, char_width=8, line_height=20), "down")

    def test_clearline_trims_html_output_and_visible_html_metadata(self):
        td, program = self.make_game('''@SYSTEM_TITLE
HTML_PRINT "<button value='keep'>Keep</button>"
HTML_PRINT "<button value='gone'><img src='Gone'></button><nonbutton>Gone</nonbutton>"
CLEARLINE 1
INPUTS
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "<button value='keep'>Keep</button>\n")
        self.assertEqual(rt.html_output, ["<button value='keep'>Keep</button>"])
        self.assertEqual(rt.html_buttons, [{"value": "keep", "title": "", "pos": "", "label": "Keep"}])
        self.assertEqual(rt.html_images, [])
        self.assertEqual(rt.html_nonbuttons, [])
        page = rt.html_page_model()
        self.assertEqual(len(page["lines"]), 1)
        self.assertEqual(page["lines"][0]["buttons"][0]["value"], "keep")
        self.assertEqual(page["lines"][0]["buttons"][0]["label"], "Keep")
        self.assertEqual(page["lines"][0]["buttons"][0]["display_line"], 1)
        self.assertEqual(rt.html_layout_model(char_width=8, line_height=20)["buttons"][0]["width"], 32)
        self.assertEqual(rt.html_click_value(16, 10, char_width=8, line_height=20), "keep")
        self.assertIsNone(rt.html_click_value(80, 10, char_width=8, line_height=20))
        self.assertEqual(rt.pending_buttons, [])

    def test_html_click_can_queue_input_and_resume_paused_runtime(self):
        td, program = self.make_game('''@SYSTEM_TITLE
HTML_PRINT "<button value='go'>Go</button>"
INPUTS
PRINTFORML picked=%RESULTS% mouse=%MOUSEB()% x={MOUSEX()} y={MOUSEY()}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        steps = rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertTrue(rt.waiting_for_input)
        self.assertGreater(steps, 0)
        self.assertEqual(rt.queue_html_click(4, 10, char_width=8, line_height=20), "go")
        self.assertEqual(rt.inputs, ["go"])
        more = rt.continue_run(max_steps=100)
        self.assertGreater(more, 0)
        self.assertFalse(rt.waiting_for_input)
        self.assertIn("picked=go mouse=go x=4 y=10", "".join(rt.output))
        self.assertEqual(rt.warnings, [])

    def test_page_model_keeps_text_style_spans_for_gui_frontends(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTS plain
SETCOLOR 255,0,0
PRINTS red
SETBGCOLOR 0,0,255
FONTBOLD
PRINTSL bold
ALIGNMENT CENTER
PRINTSL removed
CLEARLINE 1
RESETCOLOR
RESETBGCOLOR
FONTREGULAR
ALIGNMENT RIGHT
PRINTSL tail
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "plainredbold\ntail\n")
        page = rt.html_page_model()
        spans = page["style_spans"]
        self.assertEqual([(s["display_line"], s["col"], s["text"]) for s in spans], [
            (1, 0, "plain"),
            (1, 5, "red"),
            (1, 8, "bold"),
            (2, 0, "tail"),
        ])
        self.assertEqual(spans[0]["color"], rt.default_color)
        self.assertEqual(spans[1]["color"], 0xFF0000)
        self.assertEqual(spans[2]["color"], 0xFF0000)
        self.assertEqual(spans[2]["bgcolor"], 0x0000FF)
        self.assertEqual(spans[2]["font_style"], 1)
        self.assertEqual(spans[3]["color"], rt.default_color)
        self.assertEqual(spans[3]["bgcolor"], rt.default_bgcolor)
        self.assertEqual(spans[3]["font_style"], 0)
        self.assertEqual(spans[3]["alignment"], "RIGHT")
        layout = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual([(d["text"], d["x"], d["color"], d["font_style"]) for d in layout["texts"]], [
            ("plain", 0, rt.default_color, 0),
            ("red", 40, 0xFF0000, 0),
            ("bold", 64, 0xFF0000, 1),
            ("tail", 0, rt.default_color, 0),
        ])
        self.assertEqual(rt.warnings, [])

    def test_printplain_does_not_harvest_numeric_button_text(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTPLAIN [18] Search
PRINTPLAINFORM [{1+1}] Form
PRINTPLAINFORMC [{20}]
PRINT [7] Real
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("[18] Search[2] Form", "".join(rt.output))
        self.assertEqual(rt.pending_buttons, ["7"])
        self.assertEqual(rt.warnings, [])

    def test_printbutton_metadata_is_exposed_for_gui_layout_and_clicks(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "emuera.config").write_text("PRINTCを並べる数:3\nPRINTCの文字数:5\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
SETCOLOR 0x123456
PRINTBUTTON "[A]", "alpha"
PRINTBUTTONC "Cell", "cell"
PRINTBUTTONLC "Line", "line"
INPUTS
RETURN
''', encoding="utf-8")
        self.addCleanup(td.cleanup)
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        page = rt.html_page_model()
        self.assertEqual([(b["display_line"], b["col"], b["label"], b["value"]) for b in page["print_buttons"]], [
            (1, 0, "[A]", "alpha"),
            (1, 3, "Cell ", "cell"),
            (1, 8, "Line ", "line"),
        ])
        self.assertEqual([b["color"] for b in page["print_buttons"]], [0x123456, 0x123456, 0x123456])
        layout = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual([(b["label"], b["x"], b["width"], b["value"]) for b in layout["print_buttons"]], [
            ("[A]", 0, 24, "alpha"),
            ("Cell ", 24, 40, "cell"),
            ("Line ", 64, 40, "line"),
        ])
        self.assertEqual(rt.html_click_value(1, 1, char_width=8, line_height=20), "alpha")
        self.assertEqual(rt.html_click_value(25, 1, char_width=8, line_height=20), "cell")
        self.assertEqual(rt.html_click_value(65, 1, char_width=8, line_height=20), "line")
        self.assertEqual(rt.html_hit_test(65, 1, char_width=8, line_height=20)["type"], "print_button")
        self.assertEqual(rt.pending_buttons, ["alpha", "cell", "line"])
        self.assertEqual(rt.warnings, [])

    def test_print_rect_metadata_is_exposed_for_gui_layout(self):
        td, program = self.make_game('''@SYSTEM_TITLE
SETCOLOR 0x445566
PRINTS "X"
PRINT_RECT 250
PRINTL
PRINT_RECT 600
PRINTL
CLEARLINE 1
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "X▭▭\n")
        page = rt.html_page_model()
        self.assertEqual([(r["display_line"], r["col"], r["width"]) for r in page["print_rects"]], [(1, 1, 250)])
        self.assertEqual(page["lines"][0]["print_rects"][0]["color"], 0x445566)
        layout = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual([(r["x"], r["y"], r["width"], r["height"], r["color"]) for r in layout["print_rects"]], [(8, 0, 250, 20, 0x445566)])
        hit = rt.html_hit_test(10, 1, char_width=8, line_height=20)
        self.assertEqual((hit["type"], hit["button_value"]), ("print_rect", ""))
        self.assertIsNone(rt.html_click_value(10, 1, char_width=8, line_height=20))
        self.assertEqual(rt.warnings, [])

    def test_printdata_variants_printd_and_print_space(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM PICK
PRINTD A
PRINT_SPACE 300
PRINTDL B
PRINTDATAW PICK
  DATAFORM C{1+1}
  DATAFORM C3
  DATALIST
    DATAFORM E{1}
    DATA E2
  ENDLIST
ENDDATA
PRINTFORML pick={PICK}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn(
            "".join(rt.output),
            {
                "A   B\nC2\npick=0\n",
                "A   B\nC3\npick=1\n",
                "A   B\nE1\nE2\npick=2\n",
            },
        )
        self.assertEqual(rt.warnings, [])

    def test_print_space_metadata_preserves_emuera_width_for_gui_layout(self):
        td, program = self.make_game('''@SYSTEM_TITLE
SETCOLOR 0x112233
PRINTS "A"
PRINT_SPACE 300
PRINTBUTTON "B", "go"
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "A   B")
        page = rt.html_page_model()
        self.assertEqual(
            [(s["display_line"], s["col"], s["width"], s["cells"], s["color"]) for s in page["print_spaces"]],
            [(1, 1, 300, 3, 0x112233)],
        )
        self.assertEqual(page["lines"][0]["print_spaces"][0]["width"], 300)
        raw = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual([(s["x"], s["width"], s["cells"], s["raw_width"]) for s in raw["print_spaces"]], [(8, 300, 3, 300)])
        self.assertEqual(raw["print_buttons"][0]["x"], 308)
        scaled = rt.html_layout_model(char_width=8, line_height=20, html_unit_scale=0.2)
        self.assertEqual((scaled["print_spaces"][0]["x"], scaled["print_spaces"][0]["width"]), (8, 60))
        self.assertEqual(scaled["print_buttons"][0]["x"], 68)
        self.assertIsNone(rt.html_hit_test(40, 1, char_width=8, line_height=20, html_unit_scale=0.2))
        self.assertEqual(rt.html_click_value(69, 1, char_width=8, line_height=20, html_unit_scale=0.2), "go")
        self.assertIsNone(rt.html_click_value(69, 1, char_width=8, line_height=20))
        self.assertEqual(rt.warnings, [])

    def test_data_records_outside_printdata_are_inert_markers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM PICK = 7
PRINTDATA PICK
ENDDATA
PRINTFORML empty={PICK}
GOTO AFTER
DATALIST
  DATA should-not-print
  DATAFORM should-not-render{1+1}
ENDLIST
DATA also-ignored
DATAFORM also-not-rendered{2+2}
ENDDATA
$AFTER
DATALIST
  DATA skipped-too
ENDLIST
ENDDATA
PRINTL ok
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "empty=7\nok\n")
        self.assertEqual(rt.warnings, [])

    def test_oneinput_string_consumes_single_character(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ONEINPUTS
PRINTFORML first=%RESULTS%
TONEINPUTS 1, "ZZ", 0, ""
PRINTFORML default=%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["abc"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "first=a\ndefault=Z\n")

    def test_input_default_arguments_and_toneinput_numeric(self):
        td, program = self.make_game('''@SYSTEM_TITLE
INPUT 42
PRINTFORML I={RESULT}:%RESULTS%
ONEINPUT 98
PRINTFORML O={RESULT}:%RESULTS%
INPUTS "fallback"
PRINTFORML S=%RESULTS%
ONEINPUTS "xy"
PRINTFORML OS=%RESULTS%
TINPUT 1, -7, 0
PRINTFORML T={RESULT}:%RESULTS%
TONEINPUT 1, 83, 0
PRINTFORML TO={RESULT}:%RESULTS%
ONEINPUT -1
PRINTL bad
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        steps = rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertLess(steps, 100)
        self.assertEqual(
            "".join(rt.output),
            "I=42:42\nO=9:9\nS=fallback\nOS=x\nT=-7:-7\nTO=8:8\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_tostr_emura_numeric_formats(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML %TOSTR(1234567, "#,###")%|%TOSTR(0, "#,##0")%|%TOSTR(7, "000")%
PRINTFORML %TOSTR(255, "x")%|%TOSTR(255, "X8")%|%TOSTR(125, "0'.'00")%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "1,234,567|0|007\nff|000000FF|1.25\n")

    def test_printv_evaluates_expression_and_suffixes(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS S
A = 12
S = "xy"
PRINTV A + 5
PRINTL
PRINTVL S + "z"
PRINTVW 3
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "17\nxyz\n3\n")
        self.assertEqual(rt.warnings, [])

    def test_prints_evaluates_string_expressions_and_form_conditions(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS S
A = 1
S = "xy"
PRINTS S
PRINTS "-"
PRINTS GETLINESTR("=", 3)
PRINTSL \\@A?yes#no\\@
PRINTSW @"%S%!"
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "xy-===yes\nxy!\n")
        self.assertEqual(rt.warnings, [])

    def test_plain_print_preserves_literal_braces_but_renders_percent_forms(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS S
S = "xy"
PRINTL {::/::::ｒﾄr}
PRINTL %S%\\@1 ?ok#bad\\@{literal}
PRINTSL {1+1}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "{::/::::ｒﾄr}\nxyok{literal}\n2\n")

    def test_form_string_unescapes_literal_percent_parentheses_and_newline(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS S
S = "朝"
DAY = 3
PRINTFORML rate={25}\\%
LOCALS = {DAY}日目\\(%S%\\)
PRINTFORML %LOCALS%
PRINTFORML \\%%S%
PRINTFORM \\@1?\\n#bad\\@
PRINTL tail
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "rate=25%\n3日目(朝)\n%朝\n\ntail\n")

    def test_lvalue_edge_cases_fullwidth_digits_qident_and_trailing_comma(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CFLAG:0:１moreフラグ += 1
LOCAL = 2
[[店舗:種類]]:(LOCAL) = 77
FLAG:触手能量 = 100
FLAG:触手能量,-= 20
ADDVOIDCHARA
PRINTFORML C={CFLAG:0:１moreフラグ}|DE={DE:0:2}|F={FLAG:触手能量}|N={CHARANUM}:{RESULT}:{NO:RESULT}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "CFlag.csv").write_text("1,１moreフラグ\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text("1,触手能量\n", encoding="utf-8")
        (root / "CSV" / "_Rename.csv").write_text("DE:0,店舗:種類\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("C=1|DE=77|F=80|N=2:1:-1", "".join(rt.output))
        self.assertEqual(rt.warnings, [])

    def test_string_append_renders_form_conditionals_for_local_dims(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS SHOW_LINE
ARG:1 = 0
SHOW_LINE = "A"
SHOW_LINE += \\@ARG:1?yes#no\\@
ARG:1 = 1
SHOW_LINE += \\@ARG:1?yes#no\\@
SHOW_LINE += @"\\@ARG:1?Y#N\\@"
PRINTFORML %SHOW_LINE%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "AnoyesY\n")

    def test_expression_function_preserves_result_register(self):
        td, program = self.make_game('''@SYSTEM_TITLE
RESULT = 1
IF RESULT == 2
PRINTL bad
ELSEIF F() == 20
PRINTFORML kept={RESULT}
ENDIF
RETURN

@F
#FUNCTION
RETURNF 20
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("kept=1", "".join(rt.output))

    def test_parenthesized_csv_name_index_is_not_function_call(self):
        td, program = self.make_game('''@SYSTEM_TITLE
FLAG:Foo(1) = 123
PRINTFORML {FLAG:1}:{Foo(1)}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Flag.csv").write_text("1,Foo(1)\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("123:1", "".join(rt.output))
        self.assertNotIn("unknown expression function: Foo", "\n".join(rt.warnings))

    def test_itemname_and_trainname_csv_string_arrays_are_loaded(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML T=%TRAINNAME:0%/%TRAINNAME:21%:{GETNUM(TRAINNAME, "休憩")}
PRINTFORML I=%ITEMNAME:5%:{GETNUM(ITEM, "Potion")}:{GETNUM(ITEMNAME, "Potion")}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Train.csv").write_text("0,愛撫\n21,休憩\n", encoding="utf-8")
        (root / "CSV" / "Item.csv").write_text("5,Potion\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "T=愛撫/休憩:21\nI=Potion:5:5\n")
        self.assertEqual(rt.warnings, [])

    def test_bracket_rename_alias_can_be_array_lvalue_base(self):
        td, program = self.make_game('''@SYSTEM_TITLE
DE:0:2 = 77
LOCAL = 1
PRINTFORML {[[店舗:種類]]:(LOCAL + 1)}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "_Rename.csv").write_text("DE:0,店舗:種類\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("77", "".join(rt.output))

    def test_analyze_count_check_native_hotpath(self):
        td, program = self.make_game('''@SYSTEM_TITLE
FLAG:20001 = 500
FLAG:20002 = 1000
FLAG:20003 = 1000
FLAG:20004 = 1000
PRINTFORML A={ANALYZE_COUNT_CHECK(1, 0, 100, 1, 999, -1)}
PRINTFORML B={ANALYZE_COUNT_CHECK(0, 100, 100, 1, 999, -1)}
PRINTFORML C={ANALYZE_COUNT_CHECK(0, 0, 100, 1, 999, 2)}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Base.csv").write_text("0,LV\n", encoding="utf-8")
        (root / "CSV" / "Abl.csv").write_text("0,種族\n", encoding="utf-8")
        (root / "CSV" / "CFlag.csv").write_text("0,EXTRA出典\n", encoding="utf-8")
        (root / "CSV" / "Chara1.csv").write_text("番号,1\n名前,A\n呼び名,A\n基礎,LV,5\n能力,種族,1\nＣフラグ,EXTRA出典,0\n", encoding="utf-8")
        (root / "CSV" / "Chara2.csv").write_text("番号,2\n名前,B\n呼び名,B\n基礎,LV,6\n能力,種族,2\nＣフラグ,EXTRA出典,1\n", encoding="utf-8")
        (root / "CSV" / "Chara3.csv").write_text("番号,3\n名前,C\n呼び名,C\n基礎,LV,7\n能力,種族,0\n", encoding="utf-8")
        (root / "CSV" / "Chara4.csv").write_text("番号,4\n名前,D\n呼び名,D\n基礎,LV,1000\n能力,種族,2\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("A=1", out)
        self.assertIn("B=1", out)
        self.assertIn("C=1", out)

    def test_remodel_equipment_native_hotpaths(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML A={改造装備(2390)}:{改造装備(2389)}:{改造装備(4949)}
PRINTFORML B={改造装備番号(2390)}:{改造装備番号(2399)}:{改造装備番号(2940)}:{改造装備番号(4949)}:{改造装備番号(0)}
PRINTFORML C={改造装備物品ナンバー(0)}:{改造装備物品ナンバー(9)}:{改造装備物品ナンバー(10)}:{改造装備物品ナンバー(59)}:{改造装備物品ナンバー(60)}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("A=1:0:1", out)
        self.assertIn("B=0:9:10:59:0", out)
        self.assertIn("C=2390:2399:2940:4949:0", out)

    def test_array_index_function_call_segment(self):
        td, program = self.make_game('''@MAIN
RETURN 0

@GET_STATE(ARG)
#FUNCTION
RETURNF ARG + 2
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.memory.set_var("ARG", [], 3)
        rt.memory.set_var("LCOUNT", [], 4)
        rt.memory.set_var("MAXBASE", [3, 6], -80)
        self.assertEqual(eval_expr(rt, "MAX(MAXBASE:ARG:GET_STATE(LCOUNT), -100)"), -80)
        self.assertEqual(rt.warnings, [])

    def test_era_megaten_numbering_lookup_helpers_use_csv_offsets(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML B=%GET_BASESTATUS(1)%:{GET_BASESTATUS_NUM("力")}
PRINTFORML T=%GET_TYPE(2)%:{GET_TYPE_NUM("火炎")}
PRINTFORML S=%GET_STATE(1)%:{GET_STATE_NUM("毒")}
PRINTFORML E=%GET_EQUIP(1)%:{GET_EQUIPNUM("銃")}
PRINTFORML C=%GET_SUCCESSION(1)%:{GET_SUCCESSION_NUM("火炎")}
PRINTFORML L=%GET_ALI1(1)%/%GET_ALI2(3)%/%GET_RANGE(2)%/%GET_SPHERE(3)%/%GET_GUNTYPE(4)%
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Base.csv").write_text(
            "0,LV\n1,力\n10,攻撃\n20,剣撃\n21,打撃\n22,火炎\n40,良好\n41,毒\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Equip.csv").write_text("0,剣\n1,銃\n", encoding="utf-8")
        (root / "CSV" / "Talent.csv").write_text("0,剣撃\n1,火炎\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(
            "".join(rt.output),
            "B=力:1\n"
            "T=火炎:2\n"
            "S=毒:1\n"
            "E=銃:1\n"
            "C=火炎:1\n"
            "L=Light/Chaos/Ｍ/全体/ライフル\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_era_megaten_simple_native_helpers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 10
ADDCHARA 11
TARGET = 0
STAIN:0:6 = 4
CFLAG:0:ステート = GET_STATE_NUM("良好")
CFLAG:1:ステート = GET_STATE_NUM("瀕死")
TALENT:0:召喚師 = 3
TALENT:1:召喚師 = 9
FLAG:ポジション1 = 0
FLAG:ポジション2 = 1
FLAG:ポジション3 = -1
FLAG:ポジション4 = -1
FLAG:ポジション5 = -1
FLAG:ポジション6 = -1
PRINTFORML D={GET_DEVIL(0)}:{GET_DEVIL(1)}:{GET_DEVIL(12,0)}
PRINTFORML ST={GET_STAIN("膣内","精液")}:{GET_STAIN("口","精液",0)}
PRINTFORML EXP={GET_NEXT_EXP(2,0)}:{GET_NEXT_EXP(2,1)}
PRINTFORML MLV={GET_SUMMONER_MLV()}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Base.csv").write_text("60,良好\n75,瀕死\n", encoding="utf-8")
        (root / "CSV" / "Abl.csv").write_text("0,種族\n", encoding="utf-8")
        (root / "CSV" / "CFlag.csv").write_text("0,ステート\n", encoding="utf-8")
        (root / "CSV" / "Talent.csv").write_text("0,召喚師\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text(
            "0,ポジション1\n1,ポジション2\n2,ポジション3\n3,ポジション4\n4,ポジション5\n5,ポジション6\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Chara10.csv").write_text("番号,10\n名前,A\n呼び名,A\n能力,種族,1\n", encoding="utf-8")
        (root / "CSV" / "Chara11.csv").write_text("番号,11\n名前,B\n呼び名,B\n能力,種族,0\n", encoding="utf-8")
        (root / "CSV" / "Chara12.csv").write_text("番号,12\n名前,C\n呼び名,C\n能力,種族,45\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(
            "".join(rt.output),
            "D=1:0:0\n"
            "ST=1:0\n"
            "EXP=40:50\n"
            "MLV=3\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_era_megaten_talent_status_and_text_native_helpers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
TALENT:0:恋慕 = 1
PRINTFORML F1={陥落(0)}:%RESULTS%
TALENT:0:恋慕 = 0
TALENT:0:親愛 = 1
PRINTFORML F2={陥落(0)}:%RESULTS%
TALENT:0:親愛 = 0
TALENT:0:妻 = 1
PRINTFORML F3={陥落(0)}:%RESULTS%
TALENT:0:妻 = 0
TALENT:0:恋慕 = 1
TALENT:0:ＮＴＲ = 1
CFLAG:0:陥落キャラ = 77
CFLAG:1:キャラ固有の番号 = 77
PRINTFORML R={恋慕(0,1)}:{親愛(0,1)}:{陥落(0,1)}:%RESULTS%
TALENT:0:ＮＴＲ = 0
TALENT:0:男性 = 1
PRINTFORML M={IS_MALE(0)}:{IS_LOOKSLIKE_MALE(0)}:{HAVE_PENIS(0)}
TALENT:0:偽娘 = 1
PRINTFORML M2={IS_MALE(0)}:{IS_LOOKSLIKE_MALE(0)}:{HAVE_PENIS(0)}
TALENT:0:男性 = 0
TALENT:0:偽娘 = 0
TALENT:0:可以発情 = 0
ABL:0:種族 = 0
CFLAG:0:発情妊娠 = 0
CFLAG:0:ダンジョン内発情 = 0
CFLAG:0:危険日 = 5
FLAG:月齢 = 5
FLAG:月齢ベクトル = 0
PRINTFORML D1={危険日(0)}
TALENT:0:可以発情 = 1
PRINTFORML D2={危険日(0)}
TALENT:0:男性 = 1
FLAG:月齢 = 2
PRINTFORML D3={危険日(0)}
ABL:0:種族 = 1
FLAG:月齢 = 8
PRINTFORML D4={危険日(0)}
BASE:0:ＨＰ = 25
MAXBASE:0:ＨＰ = 100
PRINTFORML HP={現HP割合(0)}:{傷害割合(0,30)}
PRINTFORML C={COMTYPE("道具系")}:{COMTYPE("未知")}
PRINTFORML H=%ハート(2)%/%ハートＢ(1)%
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Talent.csv").write_text(
            "\n".join(
                f"{i},{name}"
                for i, name in enumerate(
                    [
                        "妻", "夫", "親愛", "恋慕",
                        "淫魔", "娼婦", "淫乱",
                        "玩具", "隷属", "服従",
                        "盟友", "相棒", "信頼", "ＮＴＲ",
                        "男性", "偽娘", "FUTA", "可以発情",
                    ]
                )
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "CSV" / "CFlag.csv").write_text(
            "0,陥落キャラ\n1,キャラ固有の番号\n2,発情妊娠\n3,ダンジョン内発情\n4,危険日\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Flag.csv").write_text("0,月齢\n1,月齢ベクトル\n", encoding="utf-8")
        (root / "CSV" / "Abl.csv").write_text("0,種族\n", encoding="utf-8")
        (root / "CSV" / "Base.csv").write_text("0,ＨＰ\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=300)
        self.assertEqual(
            "".join(rt.output),
            "F1=1:恋慕\n"
            "F2=2:恋慕\n"
            "F3=3:恋慕\n"
            "R=1:0:1:恋慕\n"
            "M=1:1:1\n"
            "M2=1:0:1\n"
            "D1=1\n"
            "D2=2\n"
            "D3=-1\n"
            "D4=-2\n"
            "HP=25:30\n"
            "C=2:-1\n"
            "H=♡♡/♥\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_era_megaten_skill_and_skillgage_native_helpers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
FLAG:技能数 = 3
FLAG:異能者技能数 = 5
TALENT:1:Aion式召喚術 = 1
CFLAG:1:リンク悪魔 = 1
CFLAG:2:ボスフラグ = 1
TALENT:3:異能者 = 1
CFLAG:3:悪魔変身 = 1
PRINTFORML C={CHARA_SKILLCOUNT(0)}:{CHARA_SKILLCOUNT_技能操作用(0)}:{CHARA_SKILLCOUNT(1)}:{CHARA_SKILLCOUNT_技能操作用(1)}:{CHARA_SKILLCOUNT(2)}:{CHARA_SKILLCOUNT(3)}:{CHARA_SKILLCOUNT(3,0)}
ABL:0:技能1 = 101
ABL:0:技能2 = 102
ABL:0:技能3 = 101
ABL:0:初期変身悪魔技能1 = 301
ABL:0:装備技能1 = 201
CFLAG:0:技能ゲージH1 = 7
CFLAG:0:技能ゲージD1 = 8
CFLAG:0:技能ゲージF1 = 4
CFLAG:0:技能ゲージH10 = 11
CFLAG:0:技能ゲージH14 = 9
CFLAG:0:技能ゲージH30 = 16
PRINTFORML H={HAVE_SKILL(0,101)}:{HAVE_SKILL(0,101,1)}:{HAVE_SKILL(0,201,1)}:{HAVE_SKILL(0,999)}
PRINTFORML O={HAVE_SKILL_OVERLAP(0,101)}:{CHECK_SKILL(0,201)}:{CHECK_SKILL(0,102)}:{CHECK_SKILL_OVERLAP(0,101)}:{HAVE_SKILL_C(0,301)}
PRINTFORML G={SKILLGAGE_NUM(0,101)}:{SKILLGAGE_H_GET(0,101)}:{SKILLGAGE_D_GET(0,101)}:{SKILLGAGE_F_GET(0,101)}:{SKILLGAGE_H_GETBIT(0,101,0)}:{SKILLGAGE_H_GETBIT(0,101,3)}
PRINTFORML GE={SKILLGAGE_NUM(0,201)}:{SKILLGAGE_H_GET(0,201)}:{SKILLGAGE_H_GETBIT(0,201,4)}
CFLAG:0:悪魔変身 = 1
PRINTFORML GA={SKILLGAGE_NUM(0,102)}:{SKILLGAGE_H_GET(0,102)}
CFLAG:0:悪魔変身 = 0
TALENT:0:Persona使 = 1
EQUIP:0:装備Persona = 5
EQUIP:0:所持Persona2 = 5
PRINTFORML GP={SKILLGAGE_NUM(0,102)}:{SKILLGAGE_H_GET(0,102)}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Flag.csv").write_text("0,技能数\n1,異能者技能数\n", encoding="utf-8")
        (root / "CSV" / "CFlag.csv").write_text(
            "0,PTフラグ\n1,ボスフラグ\n2,リンク悪魔\n3,悪魔変身\n"
            "10,技能ゲージH1\n11,技能ゲージD1\n12,技能ゲージF1\n"
            "13,技能ゲージH10\n14,技能ゲージH14\n15,技能ゲージH30\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Talent.csv").write_text(
            "0,Aion式召喚術\n1,Persona使\n2,異能者\n3,達人\n4,人修羅\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Equip.csv").write_text("0,装備Persona\n1,所持Persona2\n2,所持Persona3\n", encoding="utf-8")
        (root / "CSV" / "Abl.csv").write_text(
            "10,技能1\n11,技能2\n12,技能3\n20,初期変身悪魔技能1\n30,装備技能1\n",
            encoding="utf-8",
        )
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=300)
        self.assertEqual(
            "".join(rt.output),
            "C=3:3:7:4:20:3:5\n"
            "H=1:1:21:0\n"
            "O=2:0:1:2:1\n"
            "G=1:7:8:4:1:0\n"
            "GE=30:16:1\n"
            "GA=14:9\n"
            "GP=10:11\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_era_megaten_private_setting_and_skillgage_write_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text(
            "0,技能数\n1,異能者技能数\n2,其他設定スイッチ\n3,戦闘難易度関連設定开关\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Talent.csv").write_text(
            "0,Aion式召喚術\n1,Persona使\n2,異能者\n3,達人\n4,人修羅\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Equip.csv").write_text("0,装備Persona\n1,所持Persona2\n2,所持Persona3\n", encoding="utf-8")
        (root / "CSV" / "Abl.csv").write_text("0,技能1\n1,技能2\n30,装備技能1\n", encoding="utf-8")
        cflags = ["PTフラグ", "ボスフラグ", "リンク悪魔", "悪魔変身"]
        for n in [1, 2, 30, 31]:
            cflags.extend([f"技能ゲージH{n}", f"技能ゲージD{n}", f"技能ゲージF{n}"])
        (root / "CSV" / "CFlag.csv").write_text("".join(f"{i},{name}\n" for i, name in enumerate(cflags)), encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDVOIDCHARA
FLAG:技能数 = 2
FLAG:異能者技能数 = 5
ABL:0:技能1 = 101
ABL:0:技能2 = 102
ABL:0:装備技能1 = 201
CALL SETTING_SET_3SIZE, 1
CALL SETTING_INVERT_MAKKA_RATE
CALL SETTING_SET_VELVET_STATUS_UP, 0
PRINTFORML S1={SETTING_IS_3SIZE()}:{SETTING_IS_MAKKA_RATE()}:{SETTING_IS_VELVET_STATUS_UP()}:{FLAG:其他設定スイッチ}
CALL SETTING_INVERT_3SIZE
CALL BATTLE_SETTING_SET_TALENT, 1
CALL BATTLE_SETTING_INVERT_1MORE
CALL BATTLE_SETTING_SET_ITEM_HITRATE, 1
CALL BATTLE_SETTING_SET_ITEM_HITRATE, 0
PRINTFORML S2={SETTING_IS_3SIZE()}:{BATTLE_SETTING_IS_TALENT()}:{BATTLE_SETTING_IS_1MORE()}:{BATTLE_SETTING_IS_ITEM_HITRATE()}:{FLAG:戦闘難易度関連設定开关}
CALL SKILLGAGE_H_SET, 0, 101, 5
CALL SKILLGAGE_D_SET, 0, 101, 6
CALL SKILLGAGE_F_SET, 0, 101, 7
CALL SKILLGAGE_H_ADD, 0, 101, 3
CALL SKILLGAGE_D_CALCULATION, 0, 101, 3, "*"
CALL SKILLGAGE_F_CALCULATION, 0, 101, 2, "+"
CALL SKILLGAGE_H_SETBIT, 0, 101, 4
CALL SKILLGAGE_H_CLEARBIT, 0, 101, 3
CALL SKILLGAGE_H_INVERTBIT, 0, 101, 1
PRINTFORML G1={SKILLGAGE_H_GET(0,101)}:{SKILLGAGE_D_GET(0,101)}:{SKILLGAGE_F_GET(0,101)}:{SKILLGAGE_H_GETBIT(0,101,1)}:{SKILLGAGE_H_GETBIT(0,101,9)}
CALL SKILLGAGE_H_SET, 0, 102, 50
CALL SKILLGAGE_D_SET, 0, 102, 60
CALL SKILLGAGE_F_SET, 0, 102, 70
CALL SKILLGAGE_SWAP, 0, 101, 102
PRINTFORML G2={CFLAG:0:技能ゲージH1}:{CFLAG:0:技能ゲージH2}:{CFLAG:0:技能ゲージD1}:{CFLAG:0:技能ゲージD2}:{CFLAG:0:技能ゲージF1}:{CFLAG:0:技能ゲージF2}
CALL SKILLGAGE_CLEAR, 0, 102
PRINTFORML G3={CFLAG:0:技能ゲージH2}:{CFLAG:0:技能ゲージD2}:{CFLAG:0:技能ゲージF2}
CFLAG:0:技能ゲージH30 = 300
CFLAG:0:技能ゲージD30 = 310
CFLAG:0:技能ゲージF30 = 320
CFLAG:0:技能ゲージH31 = 400
CFLAG:0:技能ゲージD31 = 410
CFLAG:0:技能ゲージF31 = 420
CALL SKILLGAGE_DIRECT_SWAP, 0, 30, 31
CALL SKILLGAGE_DIRECT_CLEAR, 0, 31
PRINTFORML G4={CFLAG:0:技能ゲージH30}:{CFLAG:0:技能ゲージD30}:{CFLAG:0:技能ゲージF30}:{CFLAG:0:技能ゲージH31}:{CFLAG:0:技能ゲージD31}:{CFLAG:0:技能ゲージF31}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=1000)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "S1=1:1:0:3\n"
            "S2=0:1:1:0:5\n"
            "G1=18:18:9:1:0\n"
            "G2=50:18:60:18:70:9\n"
            "G3=0:0:0\n"
            "G4=400:410:420:0:0:0\n",
        )

    def test_era_megaten_private_badend_display_misc_and_equiptheory_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "_Rename.csv").write_text(
            "1000,技能:装備知識Lv0\n1001,技能:装備知識Lv1\n1002,技能:装備知識Lv2\n"
            "1003,技能:装備知識Lv3\n1004,技能:装備知識Lv4\n1005,技能:装備知識Lv5\n"
            "2001,衣装:專属奴隸項圈\n2002,衣装:背徳戒指\n30,キャラ:你的女兒\n31,キャラ:奴隸的女兒\n32,キャラ:造魔\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Flag.csv").write_text(
            "0,DEBUG\n1,ポジション7\n2,ポジション8\n3,ポジション9\n4,人間戦闘ステ設定\n"
            "5,技能数\n6,異能者技能数\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Base.csv").write_text("0,LV\n10,良好\n25,瀕死\n", encoding="utf-8")
        (root / "CSV" / "Abl.csv").write_text(
            "0,種族\n1,技能1\n2,技能2\n3,人間時技能1\n4,人間時技能2\n5,人間時技能3\n6,人間時技能4\n30,装備技能1\n",
            encoding="utf-8",
        )
        (root / "CSV" / "CFlag.csv").write_text(
            "0,ステート\n1,悪魔変身\n2,子宮最大容量\n3,PTフラグ\n4,所属ＣＯＭＰ\n5,リンク悪魔\n6,ボスフラグ\n"
            "7,帽子\n8,内衣（下）\n9,下衣\n10,手\n11,其他\n12,其他2\n13,其他3\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Flag.csv").write_text(
            "0,DEBUG\n1,技能数\n2,異能者技能数\n3,人間戦闘ステ設定\n"
            "17,ポジション7\n18,ポジション8\n19,ポジション9\n20,ポジション10\n21,ポジション11\n"
            "22,ポジション12\n23,ポジション13\n24,ポジション14\n25,ポジション15\n26,ポジション16\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Talent.csv").write_text(
            "0,Aion式召喚術\n1,Persona使\n2,異能者\n3,達人\n4,人修羅\n5,小人体型\n6,体型嬌小\n7,高大\n8,巨人\n9,容易懷孕\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Equip.csv").write_text("0,剣\n1,装備Persona\n2,所持Persona2\n3,所持Persona3\n", encoding="utf-8")
        (root / "CSV" / "Chara10.csv").write_text("番号,10\n名前,LongName\n呼び名,LN\n能力,種族,36\n能力,技能1,1005\nフラグ,PTフラグ,1\nフラグ,所属ＣＯＭＰ,0\n", encoding="utf-8")
        (root / "CSV" / "Chara30.csv").write_text("番号,30\n名前,D\n呼び名,D\nフラグ,1165,1\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDCHARA 10
ADDVOIDCHARA
ADDVOIDCHARA
NAME:1 = ＡＢＣＤＥＦ
CALLNAME:1 = 呼称
FLAG:技能数 = 2
FLAG:異能者技能数 = 5
ABL:0:技能1 = 1005
ABL:0:種族 = 36
BASE:0:LV = 20
ABL:1:種族 = 1
ABL:1:技能1 = 1002
TALENT:1:高大 = 1
TALENT:1:容易懷孕 = 1
CFLAG:1:ステート = GET_STATE_NUM("瀕死")
ABL:2:種族 = 0
TALENT:2:Aion式召喚術 = 1
ABL:2:技能1 = 111
ABL:2:技能2 = 222
EQUIP:0:剣 = 777
TALENT:0:253 = 2
CFLAG:0:其他 = 2001
CFLAG:1:其他 = 2002
FLAG:ポジション7 = 0
FLAG:ポジション8 = 1
FLAG:ポジション9 = -1
FLAG:ポジション10 = -1
FLAG:ポジション11 = -1
FLAG:ポジション12 = -1
FLAG:ポジション13 = -1
FLAG:ポジション14 = -1
FLAG:ポジション15 = -1
FLAG:ポジション16 = -1
CALL GLOBAL_BADEND_INIT
CALL SHOPCOMABLE_700
PRINTFORML S700={RESULT}
FLAG:DEBUG = 1
CALL SHOPCOMABLE_700
PRINTFORML S700D={RESULT}:%RESULTS%
FLAG:DEBUG = 0
CALL SHOP_COM_700
PRINTFORML BR={GLOBAL:バッドエンド記録1}:{GLOBAL:バッドエンド記録2}
CALL GLOBAL_BADEND_SET, 1
CALL GLOBAL_BADEND_SET, 65
PRINTFORML B={GLOBAL_BADEND_GET(1)}:{GLOBAL_BADEND_GET(2)}:{GLOBAL_BADEND_GET(65)}:{GLOBAL:バッドエンド記録1}:{GLOBAL:バッドエンド記録2}
CALL GLOBAL_BADEND_DISP_BADENDLIST
PRINTFORML M=%GET_STATE_KANJI(15)%:%GET_STATE_KANJI(15,1)%:{STATE_COLOR(15)}:%CHANGE_MS_TO_HHMISS(60123000)%:%耐性一文字(-1)%:%耐性一文字(50)%:%耐性一文字(150)%:%耐性一文字(999)%:%耐性一文字(250)%
PRINTFORML E={ENEMY_COUNT(0)}:{ENEMY_COUNT(1)}
PRINTFORML K='%ADD_KMGT(1234567,6)%':'%ADD_KMGT(-987654321,7)%'
PRINTFORML N=%S_NAME(1,4,0,"X")%:%S_NAME(9,4,0,"X")%
PRINTFORML A={Aion式召喚術_技能枠判定(2,1)}:{Aion式召喚術_技能枠判定(0,1)}:{Aion式召喚術_技能枠判定(0,5)}
CALL Aion式召喚術_人間時技能反映, 2
PRINTFORML AH={ABL:2:人間時技能1}:{ABL:2:人間時技能2}
PRINTFORML EQ={SKILL_EQUIPTHEORY_IS_HAVE_SKILL(0)}:{SKILL_EQUIPTHEORY_IS_SKILL_EQUIPTHEORY(1005)}:{SKILL_EQUIPTHEORY_EQUIP_STATUS(0,100)}:{SKILL_EQUIPTHEORY_EQUIP_HIT(1,100)}
PRINTFORML W={MATCHING_WEAPON_CHECK(0)}:{WEAPON_STYLE_CHECK(0,4)}:{WEAPON_CHECK_MIX(0)}
PRINTFORML P={GET_CHARAPARAM(10,"種族")}:{GET_CHARAPARAM(10,"LV","",-100,"BASE")}
CALL 子宮最大容量初期設定, 1
PRINTFORML U={CFLAG:1:子宮最大容量}:{IS_ANTI_NTR_CLOTHES(0)}:{IS_ANTI_NTR_CLOTHES(1)}
NO:1 = 4901
NO:2 = 32
PRINTFORML R={IS_RANDOMCHARA(1)}:{IS_RANDOMCHARA(2)}:{IS_RANDOMCHARA(0)}
CALL LIFTING_A_BAN, 30
PRINTFORML L={FLAG:10030}
RETURN

@剣タイプ_777
#FUNCTION
RETURNF 4
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=2000)
        self.assertEqual(rt.warnings, [])
        out = "".join(rt.output)
        self.assertIn("S700=-1\n", out)
        self.assertIn("S700D=1:バッドエンド記録リセット\n", out)
        self.assertIn("BR=0:0\n", out)
        self.assertIn("B=1:0:65:2:2\n", out)
        self.assertIn("発見済みのBADエンド", out)
        self.assertIn("M=死亡:死:10027008:16:42:03:吸:50:弱:反:倍\n", out)
        self.assertIn("E=1:2\n", out)
        self.assertIn("K=' 1.23M':'  -987M'\n", out)
        self.assertIn("N=呼称:X\n", out)
        self.assertIn("A=-1:1:0\n", out)
        self.assertIn("AH=111:222\n", out)
        self.assertIn("EQ=1:1:125:50\n", out)
        self.assertIn("W=4:2:204\n", out)
        self.assertIn("P=36:20\n", out)
        self.assertIn("U=115:1:-1\n", out)
        self.assertIn("R=1:1:0\n", out)
        self.assertIn("Dの合体を解禁しました。", out)
        self.assertIn("L=1\n", out)

    def test_era_megaten_private_skill_timing_and_regen_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text(
            "0,技能数\n1,異能者技能数\n2,ポジション1\n3,ポジション2\n4,ポジション3\n5,ポジション4\n"
            "6,ポジション5\n7,ポジション6\n8,ポジション7\n9,ポジション8\n10,ポジション9\n"
            "11,ポジション10\n12,ポジション11\n13,ポジション12\n14,ポジション13\n15,ポジション14\n"
            "16,ポジション15\n17,ポジション16\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Base.csv").write_text("0,ＨＰ\n1,ＭＡＧ\n", encoding="utf-8")
        (root / "CSV" / "Abl.csv").write_text(
            "0,種族\n1,技能1\n2,技能2\n3,技能3\n4,技能4\n5,技能5\n30,装備技能1\n31,装備技能2\n",
            encoding="utf-8",
        )
        (root / "CSV" / "CFlag.csv").write_text(
            "0,PTフラグ\n1,ボスフラグ\n2,悪魔変身\n3,リンク悪魔\n4,ＭＡＧ自己消費\n"
            "10,攻撃強化\n11,命中強化\n12,防御強化\n13,回避強化\n14,魔法威力強化\n"
            "15,魔法効果強化\n16,クリティカル強化\n17,BS強化\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Talent.csv").write_text(
            "0,Aion式召喚術\n1,Persona使\n2,異能者\n3,達人\n4,人修羅\n5,悪魔変身\n",
            encoding="utf-8",
        )
        (root / "CSV" / "CStr.csv").write_text(
            "0,専用技1\n1,専用技2\n2,専用技3\n3,専用技4\n4,専用技5\n5,専用技6\n"
            "6,専用技7\n7,専用技8\n8,専用技9\n9,専用技10\n10,専用技11\n11,専用技12\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Item.csv").write_text("13904,技能牌【専用技1】\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDVOIDCHARA
ADDVOIDCHARA
ADDVOIDCHARA
MASTER = 0
ASSI = 2
CALLNAME:0 = C0
CALLNAME:1 = C1
CALLNAME:2 = C2
FLAG:技能数 = 3
FLAG:異能者技能数 = 5
ABL:0:技能1 = 101
ABL:0:技能2 = 101
ABL:0:技能3 = 3904
ABL:0:装備技能1 = 201
CSTR:0:専用技1 = Alpha
ABL:1:技能1 = 101
ABL:1:技能2 = 102
ABL:1:種族 = 1
ABL:2:技能1 = 301
CFLAG:0:PTフラグ = 1
CFLAG:1:PTフラグ = 1
CFLAG:2:PTフラグ = 1
BASE:0:ＭＡＧ = 40
BASE:1:ＭＡＧ = 5
BASE:1:ＨＰ = 10
FLAG:ポジション1 = 0
FLAG:ポジション2 = 1
FLAG:ポジション3 = 2
FLAG:ポジション4 = -1
FLAG:ポジション5 = -1
FLAG:ポジション6 = -1
FLAG:ポジション7 = -1
FLAG:ポジション8 = -1
FLAG:ポジション9 = -1
FLAG:ポジション10 = -1
FLAG:ポジション11 = -1
FLAG:ポジション12 = -1
FLAG:ポジション13 = -1
FLAG:ポジション14 = -1
FLAG:ポジション15 = -1
FLAG:ポジション16 = -1
CALL SEARCH_SKILL_FUNCTION, 0, "TURNSTART", 0
PRINTFORML SEARCH={GLOBAL:10}:{GLOBAL:13}:{GLOBAL:12}:{GLOBAL:11}:{GLOBAL:14}
CALL SKILL_TIMING, "TURNSTART"
PRINTFORML TIMING={GLOBAL:10}:{GLOBAL:13}:{GLOBAL:12}:{GLOBAL:11}:{GLOBAL:14}
CALL VAR_REGENABLE_CHECK, 1, 101, "HP"
PRINTFORML RK1={RESULT}
CALL VAR_REGENABLE_CHECK, 1, 102, "HP"
PRINTFORML RK2={RESULT}
CALL VAR_REGEN, 1, 101, "ＨＰ", 15, 7
PRINTFORML REGEN={BASE:0:ＭＡＧ}:{BASE:1:ＨＰ}
CFLAG:0:攻撃強化 = 5
CALL VAR_KAJA, 0, 0, 4, 6, 1, 3
PRINTFORML KAJA={CFLAG:0:攻撃強化}:{CFLAG:1:攻撃強化}:{POS(1)}:{POS(2)}
RETURN

@SKILL_TURNSTART_101(ARG)
GLOBAL:10 = GLOBAL:10 + 1
RETURN

@SKILL_TURNSTART_102(ARG)
GLOBAL:11 = GLOBAL:11 + 1
RETURN

@SKILL_TURNSTART_201(ARG)
GLOBAL:12 = GLOBAL:12 + 1
RETURN

@SKILL_TURNSTART_Alpha(ARG)
GLOBAL:13 = GLOBAL:13 + 1
RETURN

@SKILL_TURNSTART_301(ARG)
GLOBAL:14 = GLOBAL:14 + 1
RETURN

@SKILL_HP_REGEN_RANK_101(ARG)
RETURN 5

@SKILL_HP_REGEN_RANK_102(ARG)
RETURN 3

@SKILL_NAME_101(ARG)
RESULTS = Regen
RETURN

@CONTROL_MAG(ARG, ARG:1)
BASE:ARG:ＭＡＧ = BASE:ARG:ＭＡＧ + ARG:1
RETURN

@VAR_HP(ARG, ARG:1, ARG:2)
BASE:ARG:ＨＰ = BASE:ARG:ＨＰ + ARG:1
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=2000)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "SEARCH=1:1:1:0:0\n"
            "TIMING=3:2:2:1:1\n"
            "RK1=1\n"
            "RK2=0\n"
            "Regen C1 >>>>> 15回復 MAG主人消費\n"
            "REGEN=33:25\n"
            "KAJA=6:4:0:1\n",
        )

    def test_era_megaten_favorite_and_skill_change_native_helpers(self):
        td, program = self.make_game('''#DIM CONST MAX_NTR_CHARA = 3
#DIM CONST MAX_PLAYER_CHARA = 5
@SYSTEM_TITLE
ADDCHARA 10
ADDCHARA 20
ADDCHARA 30
CFLAG:0:キャラ固有の番号 = 0
CFLAG:1:キャラ固有の番号 = 1
CFLAG:2:キャラ固有の番号 = 2
CDFLAG:0:キャラ間好感度:99 = -120
CDFLAG:0:キャラ間好感度:101 = 100
CDFLAG:0:キャラ間好感度:102 = 60
PRINTFORML N={FAVORITE(0,-1)}:{FAVORITE_ID(0,-1)}
ABL:0:百合属性 = 2
ABL:0:百合中毒 = 1
TALENT:0:兩面通吃 = 1
PRINTFORML L={IS_LESBIAN(0,1)}:{FAVORITE(0,1)}
TALENT:0:討厭女人 = 1
PRINTFORML LH={FAVORITE(0,1)}
TALENT:0:討厭女人 = 0
TALENT:0:兩面通吃 = 0
ABL:0:百合属性 = 0
ABL:0:百合中毒 = 0
TALENT:0:男性 = 1
TALENT:1:男性 = 1
TALENT:2:男性 = 1
ABL:0:ＢＬ属性 = 3
ABL:0:ＢＬ中毒 = 1
PRINTFORML G={IS_GAY(0,2)}:{FAVORITE(0,2)}
PRINTFORML TOP={FAVORITE_1(0)}:{FAVORITE_1(0,1)}:{FAVORITE_1_ID(0)}:{FAVORITE_1_ID(0,1)}
CFLAG:0:被リンクフラグ = 2
PRINTFORML SC={SKILL_CHANGE(0,10)}:{SKILL_CHANGE(0,30)}:{SKILL_CHANGE(0,99)}:{SKILL_CHANGE(-1,10)}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "CFlag.csv").write_text("0,EXTRA出典\n1,キャラ固有の番号\n2,被リンクフラグ\n", encoding="utf-8")
        (root / "CSV" / "Cdflag1.csv").write_text("0,キャラ間好感度\n", encoding="utf-8")
        (root / "CSV" / "Abl.csv").write_text("0,百合属性\n1,百合中毒\n2,ＢＬ属性\n3,ＢＬ中毒\n", encoding="utf-8")
        (root / "CSV" / "Talent.csv").write_text(
            "0,男性\n1,偽娘\n2,兩面通吃\n3,討厭女人\n4,討厭男人\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Flag.csv").write_text("0,偽娘ＢＬ設定\n", encoding="utf-8")
        for no in (10, 20, 30):
            (root / "CSV" / f"Chara{no}.csv").write_text(f"番号,{no}\n名前,C{no}\n呼び名,C{no}\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=300)
        self.assertEqual(
            "".join(rt.output),
            "N=-120:-120\n"
            "L=1:90\n"
            "LH=40\n"
            "G=1:30\n"
            "TOP=-1:1:-1:1\n"
            "SC=1:1:0:0\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_era_megaten_personal_skill_and_equipment_native_helpers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 10
ADDCHARA 20
ADDCHARA 30
FLAG:技能数 = 3
FLAG:異能者技能数 = 5
CSTR:0:専用技1 = Alpha
CSTR:0:専用技2 = Beta
ABL:0:技能1 = SKILL_NUM_F("専用技1")
CSTR:0:専用装備 = Blade_剣4_Coat_胴
PRINTFORML S={PU_NUM()}:{SKILL_NUM_F("専用技1")}:%SKILL_NAME_F(SKILL_NUM_F("専用技1"))%
PRINTFORML P=%GET_PU_SKILL_CSTR(0,1)%:{PU_SKILLNUM_GET(0,"Alpha")}:{PU_SKILL_CHECK(0,"Beta",1)}:{IS_PU_SKILL(0,SKILL_NUM_F("専用技2"),"Beta")}:{HAVE_PU_SKILL(0,"Alpha",1)}
CFLAG:2:キャラ固有の番号 = 7
CSTR:2:専用技1 = Linked
TALENT:1:Aion式召喚術 = 1
CFLAG:1:リンク悪魔 = 7
ABL:1:技能5 = SKILL_NUM_F("専用技1") + 1
PRINTFORML L=%GET_PU_SKILL_CSTR(1,1)%
PRINTFORML W=%GET_WEAPON_TYPE(4)%:{GET_WEAPON_TYPE_NUM("刀")}:{PUEQ_NUM_CHECK(3439)}:{PUEQ_NUM_CHECK(2000)}:{PUEQ_NUM_GET("剣4")}:{PUEQ_NUM_GET("胴")}
PRINTFORML EQ=%PUEQ_NAME_GET(0,PUEQ_NUM_GET("剣4"))%:%PUEQ_NAME_GETS(0,"胴")%
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Flag.csv").write_text("0,技能数\n1,異能者技能数\n", encoding="utf-8")
        (root / "CSV" / "CFlag.csv").write_text(
            "0,PTフラグ\n1,ボスフラグ\n2,リンク悪魔\n3,悪魔変身\n4,キャラ固有の番号\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Talent.csv").write_text("0,Aion式召喚術\n1,Persona使\n2,異能者\n3,達人\n4,人修羅\n5,悪魔変身\n", encoding="utf-8")
        (root / "CSV" / "Abl.csv").write_text("0,技能1\n1,技能2\n2,技能3\n4,技能5\n30,装備技能1\n", encoding="utf-8")
        (root / "CSV" / "CStr.csv").write_text(
            "0,専用技1\n1,専用技2\n2,専用技3\n3,専用技4\n4,専用技5\n5,専用技6\n"
            "6,専用技7\n7,専用技8\n8,専用技9\n9,専用技10\n10,専用技11\n11,専用技12\n20,専用装備\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Item.csv").write_text("13904,技能牌【専用技1】\n13905,技能牌【専用技2】\n", encoding="utf-8")
        for no in (10, 20, 30):
            (root / "CSV" / f"Chara{no}.csv").write_text(f"番号,{no}\n名前,C{no}\n呼び名,C{no}\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=300)
        self.assertEqual(
            "".join(rt.output),
            "S=12:3904:専用技1\n"
            "P=Alpha:3904:1:1:1\n"
            "L=Linked\n"
            "W=剣:1:1:0:2382:3939\n"
            "EQ=Blade:Coat\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_run_small_script(self):
        td, program = self.make_game('''@SYSTEM_TITLE\n#DIM I\nFOR I,0,3\nPRINTFORM {I}\nNEXT\nA = 2\nCALL ADD, 5\nPRINTFORML ={RESULT}\nRETURN\n\n@ADD(ARG)\n#FUNCTION\nRETURNF ARG + A\n''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("012=7", "".join(rt.output))

    def test_chara_csv(self):
        td, program = self.make_game('''@SYSTEM_TITLE\nADDCHARA 0\nPRINTFORML %CALLNAME:0%:{BASE:0:LV}\nRETURN\n''')
        root = Path(td.name)
        (root / "CSV" / "Base.csv").write_text("30,LV\n", encoding="utf-8")
        (root / "CSV" / "Chara0.csv").write_text("番号,0\n名前,あなた\n呼び名,あなた\n基礎,LV,5\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("あなた:5", "".join(rt.output))

    def test_csvcharanum_returns_sparse_upper_bound_for_csv_numbers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM COUNT
#DIM I
FOR I, 1, CSVCHARANUM()
  SIF EXISTCSV(I)
    COUNT++
NEXT
CSVCHARANUM
PRINTFORML N={CSVCHARANUM()}:{RESULT}:{COUNT}:{EXISTCSV(10)}:{EXISTCSV(11)}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Chara0.csv").write_text("番号,0\n名前,A\n呼び名,A\n", encoding="utf-8")
        (root / "CSV" / "Chara10.csv").write_text("番号,10\n名前,B\n呼び名,B\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "N=11:11:1:1:0\n")
        self.assertEqual(rt.warnings, [])

    def test_adddefchara_uses_csv_file_numbers_and_gamebase_initial_chara(self):
        td, program = self.make_game('''@SYSTEM_TITLE
RESETDATA
ADDDEFCHARA
PRINTFORML N={CHARANUM}:M={MASTER}:T={TARGET}:NO={NO:0},{NO:1}:NAME=%NAME:0%/%NAME:1%
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "GameBase.csv").write_text(
            "称号,Test\nバージョン,1\n最初からいるキャラ,12\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Chara0_master.csv").write_text("番号,99\n名前,FileZero\n呼び名,F0\n", encoding="utf-8")
        (root / "CSV" / "Chara12_target.csv").write_text("番号,34\n名前,Initial\n呼び名,Init\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "N=2:M=0:T=1:NO=99,34:NAME=FileZero/Initial\n")
        self.assertEqual(rt.warnings, [])

    def test_adddefchara_missing_csv_creates_empty_character(self):
        td, program = self.make_game('''@SYSTEM_TITLE
RESETDATA
ADDDEFCHARA
PRINTFORML {CHARANUM}:{NO:0}:%NAME:0%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "1:-1:\n")
        self.assertEqual(rt.warnings, [])

    def test_multi_add_delete_delall_and_pickupchara_remap_character_indices(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 0, 1, 2, 3
MASTER = 0
PLAYER = 1
TARGET = 2
ASSI = 3
DELCHARA 1, 3
PRINTFORML D={CHARANUM}:{NO:0},{NO:1}:M{MASTER}:P{PLAYER}:T{TARGET}:A{ASSI}
ADDCHARA 1, 3, 4
MASTER = 0
PLAYER = 2
TARGET = 3
ASSI = 4
PICKUPCHARA MASTER, TARGET
PRINTFORML P={CHARANUM}:{NO:0},{NO:1}:M{MASTER}:P{PLAYER}:T{TARGET}:A{ASSI}
DELALLCHARA
PRINTFORML A={CHARANUM}:M{MASTER}:T{TARGET}
RETURN
''')
        root = Path(td.name)
        for no in range(5):
            (root / "CSV" / f"Chara{no}.csv").write_text(f"番号,{no}\n名前,C{no}\n呼び名,C{no}\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(
            "".join(rt.output),
            "D=2:0,2:M0:P-1:T1:A-1\nP=2:0,3:M0:P-1:T1:A-1\nA=0:M-1:T-1\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_addvoidchara_creates_empty_character(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDVOIDCHARA
PRINTFORML {CHARANUM}:{RESULT}:{NO:0}:[%NAME:0%]:[%CALLNAME:0%]
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "1:0:-1:[]:[]\n")
        self.assertEqual(rt.warnings, [])

    def test_sp_chara_addition_csv_functions_existcsv_and_getchara_flag(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML E={EXISTCSV(10)}:{EXISTCSV(10,1)}:{EXISTCSV(11,1)}
PRINTFORML C=%CSVNAME(10)%/%CSVNAME(10,1)%:%CSVCALLNAME(10,1)%:%CSVNICKNAME(10,1)%:%CSVCSTR(10,5,1)%:{CSVBASE(10,30,0)}:{CSVBASE(10,30,1)}:{CSVCFLAG(10,0,1)}
ADDSPCHARA 10
PRINTFORML S={CHARANUM}:{GETCHARA(10)}:{GETCHARA(10,1)}:{GETSPCHARA(10)}:{CFLAG:0:0}:%NAME:0%
ADDCHARA 10
PRINTFORML A={CHARANUM}:{NO:0}:{CFLAG:0:0}:{NO:1}:{CFLAG:1:0}:{GETCHARA(10)}:{GETCHARA(10,1)}:{GETSPCHARA(10)}
DELCHARA 1
PRINTFORML G={CHARANUM}:{GETCHARA(10)}:{GETCHARA(10,1)}:{GETSPCHARA(10)}:{CFLAG:0:0}:%NAME:0%
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Base.csv").write_text("30,LV\n", encoding="utf-8")
        (root / "CSV" / "Chara10_normal.csv").write_text(
            "番号,10\n名前,Normal\n呼び名,N\nニックネーム,NN\n基礎,LV,5\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Chara110_sp.csv").write_text(
            "番号,10\n名前,Special\n呼び名,SPC\nニックネーム,SPN\n基礎,LV,9\nフラグ,0,1\nＣ文字列,5,SPSTR\n",
            encoding="utf-8",
        )
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(
            "".join(rt.output),
            "E=1:1:0\n"
            "C=Normal/Special:SPC:SPN:SPSTR:5:9:1\n"
            "S=1:-1:0:0:1:Special\n"
            "A=2:10:1:10:0:1:1:0\n"
            "G=1:-1:0:0:1:Special\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_findchara_id_b_and_m_follow_erb_helpers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 1
ADDCHARA 2
ADDCHARA 3
CFLAG:0:キャラ固有の番号 = 111
CFLAG:1:キャラ固有の番号 = 222
CFLAG:0:PTフラグ = 1
CFLAG:0:所属ＣＯＭＰ = -1
CFLAG:1:PTフラグ = 1
CFLAG:1:所属ＣＯＭＰ = 0
CFLAG:2:PTフラグ = 2
CFLAG:2:事件加入 = 7
PRINTFORML I={FINDCHARA_ID(222)}:{FINDCHARA_ID(999)}
PRINTFORML M={FINDCHARA_M(1,2,3)}:{FINDCHARA_M(9,1,2)}
PRINTFORML B1={FINDCHARA_B(1)}:{RESULT:1}
PRINTFORML B2={FINDCHARA_B(2)}:{RESULT:1}
PRINTFORML B3={FINDCHARA_B(3,7)}:{RESULT:1}
PRINTFORML B4={FINDCHARA_B(3,8)}:{RESULT:1}
RETURN

@INPUTABLEF_CHARA(ARG)
#FUNCTION
SIF ARG == 1
    RETURNF 0
RETURNF 1
''')
        root = Path(td.name)
        (root / "CSV" / "CFLAG.csv").write_text(
            "0,SP\n1,事件加入\n2,PTフラグ\n3,所属ＣＯＭＰ\n4,キャラ固有の番号\n",
            encoding="utf-8",
        )
        for no in range(1, 4):
            (root / "CSV" / f"Chara{no}.csv").write_text(f"番号,{no}\n名前,C{no}\n呼び名,C{no}\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=300)
        self.assertEqual(
            "".join(rt.output),
            "I=1:-1\n"
            "M=1:0\n"
            "B1=0:0\n"
            "B2=11:1\n"
            "B3=2:2\n"
            "B4=-1:-1\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_string_assignment_keeps_bare_csv_constants_literal(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS S, 7
LOCAL = 探索
A = 8
RESULTS = 探索
S:0 = RESULTS
S:1 = LOCAL
S:2 = 探索
S:3 = 名称/愛称変更
S:4 = 遊戲選項
S:5 = A / 2
S:6 = 労役/売却/経営
PRINTFORML {LOCAL}:%RESULTS%:%S:0%:%S:1%:%S:2%:%S:3%:%S:4%:%S:5%:%S:6%
RETURN

@UNRELATED_LOCAL_DECL
#DIM 遊戲選項
#DIMS CONST 労役 = "x"
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "_Rename.csv").write_text("101,ショップ:探索\n206,ショップ:労役/売却/経営\n210,ショップ:名称/愛称変更\n400,ショップ:遊戲選項\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("101:探索:探索:101:探索:名称/愛称変更:遊戲選項:4:労役/売却/経営", "".join(rt.output))

    def test_mixed_form_string_assignment_renders_literals_not_expression(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS S
#DIMS T
#DIMS U
#DIM A
LOCALS '= HP
A = 7
S =  %LOCALS,4%：{A,4}({A,4})
T = _Enter_確定_%A == 7 ? "" # "0x404040"%
U = F() + @"%TOSTR(7)%"
PRINTFORML [%S%]|[%T%]|[%U%]
RETURN

@F
#FUNCTIONS
RETURNF "y"
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "[  HP：   7(   7)]|[_Enter_確定_]|[y7]\n")

    def test_string_assignment_allows_literal_arrow_prefix_before_form_marker(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS TARGETS
#DIMS RESULTS
#DIM TARGET_LENS
#DIM N
RESULTS = Enemy
TARGET_LENS = 8
TARGETS = >>> %RESULTS, TARGET_LENS, LEFT%
N = 8 >> 1
PRINTFORML [%TARGETS%]|{N}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "[>>> Enemy   ]|4\n")

    def test_tstr_is_builtin_string_array_and_renders_battle_form_labels(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM L_威力
#DIM TEMP
#DIMS L_攻撃タイプ
#DIMS L_傷害タイプ
#DIMS L_RESULTS
L_威力 = 80
L_攻撃タイプ = "剣撃"
L_傷害タイプ = "物理"
TSTR:傷害解析1 = 【威力_{L_威力}】【%L_攻撃タイプ%/%L_傷害タイプ%】
TSTR:技能メッセージ矢印 = 　>>>>>>　
TEMP = 3
L_RESULTS = AUTO_SPLIT("攻/命", "/", 0) + \\@TEMP >= 0 ?+#\\@ + TOSTR(TEMP)
TEMP = -2
L_RESULTS += "/" + AUTO_SPLIT("攻/命", "/", 1) + \\@TEMP >= 0 ?+#\\@ + TOSTR(TEMP)
PRINTFORML %TSTR:傷害解析1%|%TSTR:技能メッセージ矢印%
PRINTFORML %L_RESULTS%
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "TSTR.csv").write_text("52,傷害解析1\n64,技能メッセージ矢印\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "【威力_80】【剣撃/物理】|　>>>>>>\n攻+3/命-2\n")

    def test_string_assignment_evaluates_top_level_ternary_expression(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS S, 3
A = 4
S:0 = A < 5 ? "small" # "large"
S:1 = A > 5 ? "large" # "small"
S:2 = A ? "yes" # "no"
PRINTFORML %S:0%/%S:1%/%S:2%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "small/small/yes\n")

    def test_string_assignment_renders_html_tag_form_literal(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS L_TEXT
#DIMS L_COLORS
L_COLORS '= #ff00aa
L_TEXT = <font color = '%L_COLORS%'>
PRINTFORML %L_TEXT%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "<font color = '#ff00aa'>\n")

    def test_named_args_postfix_and_trycatch(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM K
#DIMS KSTR, 10
CALL NAMED, 5, "ok"
KSTR:(K++) = RESULTS
TRYCCALL MISSING
CATCH
KSTR:(K++) = "caught"
ENDCATCH
TRYCCALL EXISTS
KSTR:(K++) = "after"
CATCH
KSTR:(K++) = "bad"
ENDCATCH
PRINTFORML %KSTR:0%/%KSTR:1%/%KSTR:2%/%KSTR:3%
RETURN

@NAMED(VALUE, TEXT)
#FUNCTIONS
RETURNF TEXT + TOSTR(VALUE)

@EXISTS
RETURN 1
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertIn("ok5/caught/after/", "".join(rt.output))

    def test_throw_stops_execution_without_falling_into_catch(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL before
CALL BOOM
PRINTL after
RETURN

@BOOM
THROW fatal {1 + 2}
PRINTL bad
CATCH
PRINTL caught
ENDCATCH
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        steps = rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(steps, 3)
        self.assertEqual("".join(rt.output), "before\n")
        self.assertEqual(rt.warnings, ["THROW: fatal 3"])
        self.assertEqual(rt.stack, [])
        self.assertEqual(rt.memory.frames, [])

    def test_tryjumpform_transfers_when_target_exists(self):
        td, program = self.make_game('''@SYSTEM_TITLE
TARGET = 7
TRYJUMPFORM DEST_{TARGET}, 3
PRINTL bad
RETURN

@DEST_7(ARG)
PRINTFORML jumped={ARG}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("jumped=3", out)
        self.assertNotIn("bad", out)

    def test_trycalllist_tryjumplist_trygotolist_choose_first_existing_target(self):
        td, program = self.make_game('''@SYSTEM_TITLE
TRYCALLLIST
FUNC MISSING
FUNC LIST_HIT, 7
FUNC LIST_BAD
ENDFUNC
PRINTL after-call
CALL GOTO_CASE
TRYJUMPLIST
FUNC MISSING
FUNC JUMP_HIT
FUNC LIST_BAD
ENDFUNC
PRINTL bad-after-jump
RETURN

@LIST_HIT(ARG)
PRINTFORML call={ARG}
RETURN

@LIST_BAD
PRINTL bad-target
RETURN

@GOTO_CASE
TRYGOTOLIST
FUNC MISSING_LABEL
FUNC $FOUND_LABEL
ENDFUNC
PRINTL bad-goto
$FOUND_LABEL
PRINTL goto
RETURN

@JUMP_HIT
PRINTL jump
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        out = "".join(rt.output)
        self.assertEqual(out, "call=7\nafter-call\ngoto\njump\n")
        self.assertEqual(rt.warnings, [])

    def test_gotoform_and_dotrain(self):
        td, program = self.make_game('''@SYSTEM_TITLE
TARGET = 2
GOTOFORM LABEL_{TARGET}
PRINTL bad
$LABEL_2
DOTRAIN 1
PRINTL done
RETURN

@COM1
PRINTL trained
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("trained\ndone", out)
        self.assertNotIn("bad", out)

    def test_function_style_call_and_array_builtins(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM I
#DIM ARR, 10
FOR I,0,5
ARR:I = I + 1
NEXT
CALLF SETVAL(7, 8)
PRINTFORML {RESULT}:{SUMARRAY(ARR)}:{MAXARRAY(ARR)}:{MINARRAY(ARR)}:{FINDELEMENT(ARR, 4)}:{INRANGEARRAY(ARR, 2, 5, 0, 5)}:{INRANGEARRAY(ARR, 0, 2)}
CALL INPUTINT(3, 4, 5)
PRINTFORML input={RESULT}
RETURN

@SETVAL(A, B)
#FUNCTION
RETURNF A + B
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["4"])
        rt.run("SYSTEM_TITLE", max_steps=300)
        out = "".join(rt.output)
        self.assertIn("15:15:5:1:3:3:1", out)
        self.assertIn("input=4", out)

    def test_tinputint_uses_timeout_default_without_blocking(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL TINPUTINT(5000, -1, 1, 0, 1, 2, 3, 9)
PRINTFORML first={RESULT}:%RESULTS%
CALL TINPUTINT(5000, -1, 1, 0, 1, 2, 3, 9)
PRINTFORML second={RESULT}:%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["2"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "first=2:2\nsecond=-1:-1\n")

    def test_tinputint_retries_invalid_explicit_input_before_timeout_default(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL TINPUTINT(5000, -1, 1, 0, 1, 2, 3, 6)
PRINTFORML picked={RESULT}:%RESULTS%
CALL TINPUTINT(5000, -1, 1, 0, 1, 2, 3, 6)
PRINTFORML timeout={RESULT}:%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["9", "6"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "picked=6:6\ntimeout=-1:-1\n")
        self.assertEqual(rt.warnings, [])

    def test_expression_inputint_and_tinputint_consume_valid_after_many_invalid_inputs(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML exprI={INPUTINT(3,4,5)}
PRINTFORML exprT={TINPUTINT(1000,-1,0,5,6)}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=(["9"] * 8) + ["4"] + (["8"] * 8) + ["6"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "exprI=4\nexprT=6\n")
        self.assertEqual(rt.warnings, [])

    def test_expression_inputint_pauses_when_explicit_inputs_exhaust(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL before
PRINTFORML exprI={INPUTINT(3,4,5)}
PRINTL after
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["9"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "before\n")
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("4"), "4")
        rt.continue_run(max_steps=100)
        self.assertEqual("".join(rt.output), "before\nexprI=4\nafter\n")
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_expression_tinputint_and_input_char_pause_when_explicit_inputs_exhaust(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL before
PRINTFORML exprT={TINPUTINT(1000,-1,0,5,6)}
PRINTL mid
PRINTFORML exprC={INPUT_CHAR("abc",0)}:%RESULTS%
PRINTL after
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["8"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "before\n")
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("6"), "6")
        rt.continue_run(max_steps=100)
        self.assertEqual("".join(rt.output), "before\nexprT=6\nmid\n")
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("z"), "z")
        rt.continue_run(max_steps=100)
        self.assertEqual("".join(rt.output), "before\nexprT=6\nmid\n")
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("b"), "b")
        rt.continue_run(max_steps=100)
        self.assertEqual("".join(rt.output), "before\nexprT=6\nmid\nexprC=0:b\nafter\n")
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_expression_input_select_and_yn_pause_without_rerendering_prompt(self):
        td, program = self.make_game('''@SYSTEM_TITLE
FLAG:双选输入设定 = 2
PRINTL before
PRINTFORML exprS={INPUT_SELECT(1,"One",2,"Two",3,"Three")}
PRINTL mid
PRINTFORML exprY={INPUT_YN("Yes","No",1)}
PRINTL after
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["9"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("before\n", out)
        self.assertIn("[1] One", out)
        self.assertNotIn("exprS=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("2"), "2")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[1] One"), 1)
        self.assertIn("exprS=2\nmid\n", out)
        self.assertEqual(out.count("[0] Yes\n"), 1)
        self.assertEqual(out.count("[1] No\n"), 1)
        self.assertNotIn("exprY=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("x"), "x")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[0] Yes\n"), 1)
        self.assertEqual(out.count("[1] No\n"), 1)
        self.assertNotIn("exprY=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("1"), "1")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[1] One"), 1)
        self.assertEqual(out.count("[0] Yes\n"), 1)
        self.assertEqual(out.count("[1] No\n"), 1)
        self.assertIn("exprY=1\nafter\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_expression_input_helpers_consume_valid_after_many_invalid_inputs(self):
        td, program = self.make_game('''@SYSTEM_TITLE
FLAG:双选输入设定 = 2
PRINTFORML exprC={INPUT_CHAR("abc",0)}:%RESULTS%
PRINTFORML exprM={INPUT_MANY(2,9)}
PRINTFORML exprS={INPUT_SELECT(1,"One",2,"Two",3,"Three")}
PRINTFORML exprP={INPUT_SPLIT("Pick","Alpha/Beta/Gamma","/","Cancel",2,0,10,1001,0,1003)}:{RESULT:1}:%RESULTS%
PRINTFORML exprY={INPUT_YN("Yes","No",2)}
RETURN
''')
        self.addCleanup(td.cleanup)
        inputs = (
            (["z"] * 8) + ["b"]
            + (["bad"] * 8) + ["7"]
            + (["9"] * 8) + ["2"]
            + (["999"] * 8) + ["11"]
            + (["x"] * 8) + ["n"]
        )
        rt = EraRuntime(program, echo=False, interactive=False, inputs=inputs)
        rt.run("SYSTEM_TITLE", max_steps=200)
        out = "".join(rt.output)
        self.assertIn("exprC=0:b\n", out)
        self.assertIn("exprM=7\n", out)
        self.assertIn("exprS=2\n", out)
        self.assertIn("exprP=11:0:Beta\n", out)
        self.assertIn("exprY=1\n", out)
        self.assertEqual(rt.warnings, [])

    def test_expression_input_split_does_not_rerender_same_page_on_invalid_inputs(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML picked={INPUT_SPLIT("Pick","Alpha/Beta/Gamma","/","Cancel",2,0,10,1001,0,1003)}:{RESULT:1}:%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["999"] * 8 + ["11"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("Pick\n"), 1)
        self.assertIn("[10]Alpha", out)
        self.assertIn("picked=11:0:Beta\n", out)
        self.assertEqual(rt.warnings, [])

    def test_expression_input_split_pauses_without_rerendering_prompt(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL before
PRINTFORML picked={INPUT_SPLIT("Pick","Alpha/Beta/Gamma","/","Cancel",2,0,10,1001,0,1003)}:{RESULT:1}:%RESULTS%
PRINTL after
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["999"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("before\n", out)
        self.assertEqual(out.count("Pick\n"), 1)
        self.assertNotIn("picked=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("999"), "999")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("Pick\n"), 1)
        self.assertNotIn("picked=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("11"), "11")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("Pick\n"), 1)
        self.assertIn("picked=11:0:Beta\n", out)
        self.assertIn("after\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_expression_input_many_uses_calculator_button_sequence(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML fw={INPUT_MANY(1,99)}
PRINTFORML neg={INPUT_MANY(-9,9)}
PRINTFORML direct={INPUT_MANY(-9,9)}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["１", "２", "ENTER", "-", "５", "ENTER", "-5", "5"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("fw=12\n", out)
        self.assertIn("neg=-5\n", out)
        self.assertIn("direct=5\n", out)
        self.assertEqual(rt.inputs, [])
        self.assertEqual(rt.warnings, [])

    def test_expression_input_many_pauses_and_preserves_calculator_state(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL before
PRINTFORML many={INPUT_MANY(1,99)}
PRINTL mid
PRINTFORML neg={INPUT_MANY(-9,9)}
PRINTL after
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["１"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("before\n", out)
        self.assertEqual(out.count("《【1】 - 【99】》"), 1)
        self.assertNotIn("many=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("２"), "２")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("《【1】 - 【99】》"), 1)
        self.assertNotIn("many=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("ENTER"), "ENTER")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("《【1】 - 【99】》"), 1)
        self.assertIn("many=12\nmid\n", out)
        self.assertEqual(out.count("《【-9】 - 【9】》"), 1)
        self.assertNotIn("neg=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        for value in ["-", "５", "ENTER"]:
            self.assertEqual(rt.queue_input(value), value)
            rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("《【-9】 - 【9】》"), 1)
        self.assertIn("neg=-5\nafter\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_expression_window_input_helpers_consume_valid_after_many_invalid_inputs(self):
        td, program = self.make_game('''@SYSTEM_TITLE
FLAG:双选输入设定 = 2
PRINTFORML exprSM={INPUT_SELECT_M("[1] One/[2] Two","/","ログを残す/ボタンを利用する",2,1,"LEFT",20)}
PRINTFORML exprYM={INPUT_YN_M("Yes","No","/")}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=(["9"] * 8) + ["2"] + (["x"] * 8) + ["n"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("exprSM=2\n", out)
        self.assertIn("exprYM=1\n", out)
        self.assertEqual(rt.warnings, [])

    def test_expression_window_input_helpers_pause_without_rerendering_prompt(self):
        td, program = self.make_game('''@SYSTEM_TITLE
FLAG:双选输入设定 = 2
PRINTL before
PRINTFORML exprSM={INPUT_SELECT_M("[1] One/[2] Two","/","ログを残す/ボタンを利用する",2,1,"LEFT",20)}
PRINTL mid
PRINTFORML exprYM={INPUT_YN_M("Yes","No","/")}
PRINTL after
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["9"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("before\n", out)
        self.assertEqual(out.count("[1] One　[2] Two\n"), 1)
        self.assertNotIn("exprSM=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("9"), "9")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[1] One　[2] Two\n"), 1)
        self.assertNotIn("exprSM=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("2"), "2")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[1] One　[2] Two\n"), 1)
        self.assertIn("exprSM=2\nmid\n", out)
        self.assertEqual(out.count("[0] Yes/[1] No\n"), 1)
        self.assertNotIn("exprYM=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("x"), "x")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[0] Yes/[1] No\n"), 1)
        self.assertNotIn("exprYM=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("n"), "n")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[1] One　[2] Two\n"), 1)
        self.assertEqual(out.count("[0] Yes/[1] No\n"), 1)
        self.assertIn("exprYM=1\nafter\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_expression_window_dungeon_input_helpers_pause_without_rerendering_prompt(self):
        td, program = self.make_game('''@SYSTEM_TITLE
FLAG:双选输入设定 = 2
PRINTL before
PRINTFORML exprSD={INPUT_SELECT_D("[7] Seven/[8] Eight")}
PRINTL mid
PRINTFORML exprYD={INPUT_YN_D("はい","いいえ","/")}
PRINTL after
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["9"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("before\n", out)
        self.assertEqual(out.count("[7] Seven\n"), 1)
        self.assertEqual(out.count("[8] Eight\n"), 1)
        self.assertNotIn("exprSD=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("8"), "8")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[7] Seven\n"), 1)
        self.assertEqual(out.count("[8] Eight\n"), 1)
        self.assertIn("exprSD=8\nmid\n", out)
        self.assertEqual(out.count("[0] はい/[1] いいえ\n"), 1)
        self.assertNotIn("exprYD=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("x"), "x")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[0] はい/[1] いいえ\n"), 1)
        self.assertNotIn("exprYD=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

        self.assertEqual(rt.queue_input("1"), "1")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[7] Seven\n"), 1)
        self.assertEqual(out.count("[8] Eight\n"), 1)
        self.assertEqual(out.count("[0] はい/[1] いいえ\n"), 1)
        self.assertIn("exprYD=1\nafter\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_expression_window_dungeon_input_helpers_resume_log_and_config_controls(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text(
            "1,オート送り\n2,ウィンドウメッセージスキップ\n",
            encoding="utf-8",
        )
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
CALL MESSAGE_WINDOW_LOG, "", "BeforeD", "/", 1, 20
FLAG:オート送り = 0
FLAG:ウィンドウメッセージスキップ = 0
PRINTFORML yd={INPUT_YN_D("Yes","No","/")}:{FLAG:オート送り}:{FLAG:ウィンドウメッセージスキップ}
FLAG:オート送り = 0
FLAG:ウィンドウメッセージスキップ = 0
PRINTFORML sd={INPUT_SELECT_D("[1] One/[2] Two","/","ログを残す/ボタンを利用する",2,1,"LEFT",20)}:{FLAG:オート送り}:{FLAG:ウィンドウメッセージスキップ}:{GLOBAL:メッセージ速度}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False, inputs=["+"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("BeforeD", out)
        self.assertEqual(out.count("[0] Yes/[1] No\n"), 1)
        self.assertNotIn("yd=", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("close"), "close")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[0] Yes/[1] No\n"), 1)
        self.assertNotIn("yd=", out)
        self.assertTrue(rt.waiting_for_input)

        for value in ["-", "*", "1"]:
            self.assertEqual(rt.queue_input(value), value)
            rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertIn("yd=1:1:1\n", out)
        self.assertEqual(out.count("[1] One　[2] Two\n"), 1)
        self.assertNotIn("sd=", out)
        self.assertTrue(rt.waiting_for_input)

        for value in ["/", "0", "5"]:
            self.assertEqual(rt.queue_input(value), value)
            rt.continue_run(max_steps=100)
            self.assertNotIn("sd=", "".join(rt.output))
            self.assertTrue(rt.waiting_for_input)
        out = "".join(rt.output)
        self.assertIn("[0] メッセージ速度\n", out)
        self.assertEqual(out.count("[1] One　[2] Two\n"), 1)

        self.assertEqual(rt.queue_input("9"), "9")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[1] One　[2] Two\n"), 1)
        self.assertNotIn("sd=", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("*"), "*")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertNotIn("sd=", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("-"), "-")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertNotIn("sd=", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("2"), "2")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertIn("sd=2:1:0:5\n", out)
        self.assertEqual(out.count("[0] Yes/[1] No\n"), 1)
        self.assertEqual(out.count("[1] One　[2] Two\n"), 1)
        self.assertEqual(rt.inputs, [])
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_expression_window_input_helpers_process_control_buttons(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text(
            "1,オート送り\n2,ウィンドウメッセージスキップ\n",
            encoding="utf-8",
        )
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
CALL MESSAGE_WINDOW_LOG, "", "Before", "/", 1, 20
FLAG:オート送り = 0
FLAG:ウィンドウメッセージスキップ = 0
PRINTFORML y={INPUT_YN_M("Yes","No","/")}:{FLAG:オート送り}:{FLAG:ウィンドウメッセージスキップ}
FLAG:オート送り = 0
FLAG:ウィンドウメッセージスキップ = 0
PRINTFORML s={INPUT_SELECT_M("[1] One/[2] Two","/","ログを残す/ボタンを利用する",2,1,"LEFT",20)}:{FLAG:オート送り}:{FLAG:ウィンドウメッセージスキップ}
PRINTFORML speed={GLOBAL:メッセージ速度}
RETURN
''', encoding="utf-8")
        program = load_program(root)
        rt = EraRuntime(
            program,
            echo=False,
            interactive=False,
            inputs=["+", "close", "-", "*", "1", "/", "0", "5", "9", "-", "*", "2"],
        )
        rt.run("SYSTEM_TITLE", max_steps=200)
        out = "".join(rt.output)
        self.assertNotIn("Before", out)
        self.assertIn("y=1:1:1\n", out)
        self.assertIn("s=2:1:0\n", out)
        self.assertIn("speed=5\n", out)
        self.assertEqual(rt.inputs, [])
        self.assertEqual(rt.warnings, [])

    def test_entry_equipment_compendium_fast_path_marks_sparse_item_flags(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ITEM:999 = 5
ITEM:1000 = 2
ITEM:1002 = 0
ITEM:9998 = 3
ITEM:9999 = 4
CALL ENTRY_EQUIPMENT_COMPENDIUM
PRINTFORML {FLAG:40999}:{FLAG:41000}:{FLAG:41002}:{FLAG:49998}:{FLAG:49999}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "0:1:0:1:0\n")

    def test_match_counts_array_slices_and_groupmatch_counts_scalars(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM A, 5
#DIM B, 2, 4
#DIM Q, 5
#DIMS S, 4
A:0 = 1
A:1 = 2
A:2 = 1
A:3 = 3
A:4 = 1
B:1:0 = 5
B:1:1 = 7
B:1:2 = 5
Q:0 = 7
Q:3 = 7
RESULT = 7
S:0 = "x"
S:1 = "y"
S:2 = "x"
PRINTFORML M={MATCH(A,1)}:{MATCH(A,1,1,4)}:{MATCH(A,9)}
PRINTFORML S={MATCH(S,"x")}:{MATCH(S,"x",1,4)}
PRINTFORML P={MATCH(B:1:0,5,0,4)}:{MATCH(Q, RESULT, 0)}
PRINTFORML G={GROUPMATCH(2,1,2,3,2)}:{GROUPMATCH(1,1,1,0)}
PRINTFORML E={EQUALCHECK(0,0,0)}:{EQUALCHECK(0,0,5,0,0)}:{EQUALCHECK(3,1,2,3)}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        out = "".join(rt.output)
        self.assertIn("M=3:1:0", out)
        self.assertIn("S=2:1", out)
        self.assertIn("P=2:2", out)
        self.assertIn("G=2:2", out)
        self.assertIn("E=0:1:1", out)

    def test_callform_target_with_parenthesized_form_expression(self):
        parsed = split_call_syntax("基本能力修正_{EQUIP:ARG:(GET_EQUIP(LCOUNT:1))}, LCOUNT, ARG")
        self.assertEqual(parsed, ("基本能力修正_{EQUIP:ARG:(GET_EQUIP(LCOUNT:1))}", ["LCOUNT", "ARG"]))
        td, program = self.make_game('''@SYSTEM_TITLE
TARGET_ID = 2500
TARGET_ID:1 = 42
CALLFORM BASIC_{TARGET_ID:(GET_SLOT(0))}, 2
PRINTFORML result={RESULT}
RETURN

@BASIC_42(ARG)
#FUNCTION
RETURNF ARG + 10

@GET_SLOT(ARG)
#FUNCTION
RETURNF 1
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("result=12", "".join(rt.output))

    def test_varset_range(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM A, 5
VARSET A, 1, 0, 4
PRINTFORML {A:0}{A:1}{A:2}{A:3}{A:4}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("11110", "".join(rt.output))

    def test_varset_fills_declared_and_standard_arrays(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM L, 4
#DIM N
#DIMS S, 3
VARSET L, -1
VARSET N, -2
VARSET S, "x"
VARSET Q, -3
PRINTFORML L={L:0},{L:1},{L:3},{L:4}|N={N},{N:1}|S=%S:0%,%S:2%,%S:3%|Q={Q:0},{Q:12}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("L=-1,-1,-1,0|N=-2,0|S=x,x,|Q=-3,-3", "".join(rt.output))

    def test_varset_indexed_target_fills_or_clears_suffix_only(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM A, 3, 4
#DIMS S, 2, 3
A:0:2 = 4
A:1:0 = 5
A:2:2 = 9
VARSET A:1:1, 7
VARSET A:1:2
S:1:0 = "a"
VARSET S:1:1, "z"
PRINTFORML A={A:0:2},{A:1:0},{A:1:1},{A:1:2},{A:1:3},{A:2:2}|S=%S:1:0%/%S:1:1%/%S:1:2%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("A=4,5,7,0,0,9|S=a/z/z", "".join(rt.output))

    def test_varset_prefix_and_array_commands(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM A, 8
#DIM B, 8
#DIMS S, 4
A:0 = 3
A:1 = 1
A:2 = 2
B:0 = 30
B:1 = 10
B:2 = 20
ARRAYSORT A, FORWARD, 0, 3
ARRAYMSORT B, A
ARRAYREMOVE A, 1, 1
ARRAYSHIFT A, 1, 9, 1, 3
S:0 = "a"
S:1 = "b"
ARRAYREMOVE S, 0, 1
VARSET B:0, 7, 1, 3
PRINTFORML A={A:0},{A:1},{A:2},{A:3}|B={B:0},{B:1},{B:2}|S=%S:0%/%S:1%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertIn("A=2,9,1,0|B=10,7,7|S=b/", "".join(rt.output))

    def test_varset_fills_local_arrays_and_plain_print_renders_form_markers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
VARSET LOCAL, -1
LOCAL:2 = 5
PRINTS \\@ LOCAL:2 == 5 ? ok # bad \\@
PRINTFORML :{LOCAL:1}:{LOCAL:2}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output).replace(" ", "")
        self.assertIn("ok:-1:5", out)

    def test_ref_parameters_alias_arrays_and_varsize(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS S, 5, 2
CALL FILL(S)
PRINTFORML %S:0:0%/%S:1:0%/{RESULT}
RETURN

@FILL(ARR)
#DIMS REF ARR, 0, 0
ARR:0:0 '= outer
CALL INNER(ARR)
VARSIZE ARR
RETURN

@INNER(BUF)
#DIMS REF BUF, 0, 0
BUF:1:0 '= inner
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("outer/inner/5", "".join(rt.output))

    def test_ref_parameters_with_index_alias_offset_slices(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM A, 6
#DIMS S, 4
ADDCHARA 0
ADDCHARA 0
TARGET = 1
A:0 = 10, 11, 12, 13, 14, 15
S:0 '= keep
S:1 '= bee
S:2 '= see
S:3 '= dee
CALL TOUCH(A:2, S:1)
CALL TOUCHC(CFLAG:TARGET)
PRINTFORML A={A:0}:{A:1}:{A:2}:{A:3}:{A:4}:{A:5}
PRINTFORML S=%S:0%:%S:1%:%S:2%:%S:3%
PRINTFORML C={CFLAG:0:親愛}:{CFLAG:1:親愛}
RETURN

@TOUCH(R, RS)
#DIM REF R, 0
#DIMS REF RS, 0
PRINTFORML size={VARSIZE(R)}:{VARSIZE(RS)}
R:0 = 20
R:2 = 22
RS:0 '= yy
VARSET R, 7
VARSET RS
RETURN

@TOUCHC(R)
#DIM REF R, 0
R:親愛 = 99
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "CFlag.csv").write_text("0,恋慕\n1,親愛\n", encoding="utf-8")
        (root / "CSV" / "VariableSize.csv").write_text("CFLAG,2\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(
            "".join(rt.output),
            "size=4:3\n"
            "A=10:11:7:7:7:7\n"
            "S=keep:::\n"
            "C=0:99\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_array_commands_operate_on_ref_index_alias_slices(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM A, 8
#DIM B, 8
A:0 = 10, 11, 5, 3, 4, 1, 16, 17
CALL ARRAYS(A:2, B:1)
PRINTFORML A={A:0}:{A:1}:{A:2}:{A:3}:{A:4}:{A:5}:{A:6}
PRINTFORML B={B:0}:{B:1}:{B:2}:{B:3}:{B:4}:{B:5}:{RESULT}
RETURN

@ARRAYS(R, DST)
#DIM REF R, 0
#DIM REF DST, 0
ARRAYCOPY "R", "DST"
ARRAYSORT R, FORWARD, 0, 4
ARRAYREMOVE DST, 1, 1
ARRAYSHIFT DST, 1, 9, 1, 3
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(
            "".join(rt.output),
            "A=10:11:1:3:4:5:16\n"
            "B=0:5:9:4:1:16:6\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_array_query_builtins_scan_ref_index_alias_slices(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM A, 8
#DIMS S, 5
A:0 = 10, 11, 5, 3, 4, 1, 16, 17
S:0 '= keep
S:1 '= bee
S:2 '= see
S:3 '= dee
S:4 '= see
CALL QUERY(A:2, S:1)
RETURN

@QUERY(R, RS)
#DIM REF R, 0
#DIMS REF RS, 0
PRINTFORML nums={SUMARRAY(R)}:{MAXARRAY(R)}:{MINARRAY(R)}:{INRANGEARRAY(R,3,6)}:{MATCH(R,4)}:{FINDELEMENT(R,4)}:{FINDLASTELEMENT(R,4)}
PRINTFORML range={SUMARRAY(R,1,4)}:{MAXARRAY(R,1,4)}:{MINARRAY(R,1,4)}:{INRANGEARRAY(R,1,5,1,4)}
PRINTFORML strs={MATCH(RS,"see")}:{FINDELEMENT(RS,"see",0,4,1)}:{FINDLASTELEMENT(RS,"see",0,4,1)}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(
            "".join(rt.output),
            "nums=46:17:1:3:1:2:2\n"
            "range=8:4:1:3\n"
            "strs=2:1:3\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_findelement_uses_multidimensional_ref_prefix_dimension(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM A, 3, 4
A:1:0 = 5
A:1:1 = 6
A:1:2 = 7
A:1:3 = 8
A:2:0 = 10
A:2:1 = 11
A:2:2 = 12
A:2:3 = 13
CALL CHECK2D(A:1)
RETURN

@CHECK2D(R)
#DIM REF R, 0, 0
PRINTFORML d={VARSIZE(R)}:{VARSIZE(R,1)}
PRINTFORML row={MATCH(R:1:0,13)}:{FINDELEMENT(R:1:0,13)}:{FINDLASTELEMENT(R:1:0,13)}:{SUMARRAY(R:1:0,0,4)}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(
            "".join(rt.output),
            "d=2:4\n"
            "row=1:3:3:46\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_varsize_command_dimensions_and_multi_assignment(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM A, 2, 3
#DIM B, 2
CALL CHECK(A)
B:0 = RESULT:0, RESULT:1
PRINTFORML {B:0}:{B:1}:{RESULT}:{RESULT:0}:{RESULT:1}
RETURN

@CHECK(R)
#DIM REF R, 0, 0
VARSIZE R
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("2:3:2:2:3", "".join(rt.output))

    def test_varsize_dimension_argument_for_expression_and_command(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM A, 2, 3, 4
VARSIZE A, 1
PRINTFORML cmd={RESULT}:{RESULT:0}:{RESULT:1}:{RESULT:2}
PRINTFORML expr={VARSIZE(A)}:{VARSIZE(A,0)}:{VARSIZE(A,1)}:{VARSIZE(A,2)}:{VARSIZE(A,3)}
CALL CHECK(A)
RETURN

@CHECK(R)
#DIM REF R, 0, 0, 0
PRINTFORML ref={VARSIZE(R,2)}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("cmd=3:2:3:4", out)
        self.assertIn("expr=2:2:3:4:0", out)
        self.assertIn("ref=4", out)
        self.assertEqual(rt.warnings, [])

    def test_decl_dimensions_resolve_const_expressions(self):
        td, program = self.make_game('''#DIM CONST MAX_SLOT = 4
#DIM SAVEDATA FLAGS, MAX_SLOT, 3
@SYSTEM_TITLE
#DIM LOCALARR, MAX_SLOT
VARSET LOCALARR, -1
VARSIZE FLAGS
PRINTFORML g={RESULT}:{RESULT:0}:{RESULT:1}:{VARSIZE("FLAGS", 1)}
PRINTFORML l={LOCALARR:0}:{LOCALARR:3}:{LOCALARR:4}:{VARSIZE("LOCALARR")}
RETURN
''')
        self.addCleanup(td.cleanup)
        self.assertEqual(program.var_decls["FLAGS"].dims, (4, 3))
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("g=4:4:3:3", out)
        self.assertIn("l=-1:-1:0:4", out)
        self.assertEqual(rt.warnings, [])

    def test_local_size_directives_drive_varsize_and_varset_fill(self):
        td, program = self.make_game('''#DIM CONST N = 3
@SYSTEM_TITLE
#LOCALSIZE N + 1
#LOCALSSIZE 2
VARSET LOCAL, -1
VARSET LOCALS, "x"
PRINTFORML n={VARSIZE("LOCAL")}:{VARSIZE("LOCALS")}
PRINTFORML l={LOCAL:0}:{LOCAL:3}:{LOCAL:4}|s=%LOCALS:0%:%LOCALS:1%:%LOCALS:2%
RETURN
''')
        self.addCleanup(td.cleanup)
        fn = program.get_function("SYSTEM_TITLE")
        self.assertIsNotNone(fn)
        self.assertEqual(fn.local_size_expr, "N + 1")
        self.assertEqual(fn.locals_size_expr, "2")
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("n=4:2", out)
        self.assertIn("l=-1:-1:0|s=x:x:", out)
        self.assertEqual(rt.warnings, [])

    def test_define_aliases_expand_lvalues_and_constants(self):
        td, program = self.make_game('''#DEFINE XPOS FLAG:10
#DEFINE MAP DA
#DEFINE NUM 12
#DEFINE EXPR NUM + 5
@SYSTEM_TITLE
XPOS = 7
MAP:1:2 = 9
PRINTFORML a={FLAG:10}:{XPOS}:{DA:1:2}:{MAP:1:2}
PRINTFORML n={NUM}:{EXPR}:{NUM + EXPR}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("a=7:7:9:9", out)
        self.assertIn("n=12:17:29", out)
        self.assertEqual(rt.warnings, [])

    def test_findelement_string_arrays_and_string_concat_expressions(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS NAMES, 3 = "", "YEN", "JP"
#DIMS S
FINDELEMENT NAMES, "", , , 1
S '= "x"
S += F() + @"%TOSTR(7)%"
PRINTFORML {RESULT}:%S%
RETURN

@F()
#FUNCTIONS
RETURNF "y"
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("0:xy7", "".join(rt.output))

    def test_find_ranges_and_findlastelement_regex_matching(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS WORDS, 5 = "alpha", "alphabet", "beta", "", "alpha"
#DIM NUMS, 5 = 1, 2, 1, 3, 1
PRINTFORML E={FINDELEMENT(WORDS, "alpha")}:{FINDELEMENT(WORDS, "alpha", 1)}:{FINDELEMENT(WORDS, "alpha", 0, 5, 1)}:{FINDELEMENT(WORDS, "", 0, 5, 1)}
PRINTFORML L={FINDLASTELEMENT(WORDS, "alpha")}:{FINDLASTELEMENT(WORDS, "alpha", 0, 4, 1)}:{FINDLASTELEMENT(WORDS, "alpha", 1, 4)}:{FINDLASTELEMENT(WORDS, "^alpha$", 0, 5, 1)}
PRINTFORML N={FINDLASTELEMENT(NUMS, 1)}:{FINDLASTELEMENT(NUMS, 1, 0, 4)}:{FINDLASTELEMENT(NUMS, 2, 2, 5)}
RESETDATA
ADDVOIDCHARA
ADDVOIDCHARA
ADDVOIDCHARA
ADDVOIDCHARA
NO:0 = 0
NO:1 = 1
NO:2 = 1
NO:3 = 2
PRINTFORML C={FINDCHARA(NO, 1, 1, 3)}:{FINDCHARA(NO, 1, 2, 3)}:{FINDCHARA(NO, 1, 3)}:{FINDLASTCHARA(NO, 1, 0, 3)}:{FINDLASTCHARA(NO, 1, 0, 2)}:{FINDLASTCHARA(NO, 1, 3)}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=300)
        self.assertEqual(
            "".join(rt.output),
            "E=0:1:0:3\n"
            "L=4:0:1:4\n"
            "N=4:2:-1\n"
            "C=1:2:-1:2:1:-1\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_private_scalar_time_money_and_printc_helpers(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "_Replace.csv").write_text("お金の単位,$\n単位の位置,前\n", encoding="utf-8")
        (root / "emuera.config").write_text("PRINTCの文字数:7\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
MONEY = 77
#DIMS TS
TS = GETTIMES()
GETTIMES
PRINTFORML S={SIGN(-5)}:{SIGN(0)}:{SIGN(9)}:{NOSAMES(1,2,"2")}:{NOSAMES(1,2,1)}:{ALLSAMES("7",7,7)}:{ALLSAMES(7,7,8)}
PRINTFORML M=%MONEYSTR(1234,"#,###")%:%MONEYSTR()%
PRINTFORML P={PRINTCLENGTH()} T={STRLEN(TS)}:%SUBSTRING(TS,4,1)%:%SUBSTRING(TS,10,1)% R={STRLEN(RESULTS)}
RETURN
''', encoding="utf-8")
        self.addCleanup(td.cleanup)
        program = load_program(root)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(
            "".join(rt.output),
            "S=-1:0:1:0:0:1:0\n"
            "M=$1,234:$77\n"
            "P=7 T=19:/:  R=19\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_emuedir_file_enumeration_and_window_memory_helpers(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV" / "Sub").mkdir(parents=True)
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "A.csv").write_text("a\n", encoding="utf-8")
        (root / "CSV" / "Sub" / "B.csv").write_text("b\n", encoding="utf-8")
        (root / "CSV" / "notes.txt").write_text("n\n", encoding="utf-8")
        (root / "emuera.config").write_text("ウィンドウ幅:1234\nウィンドウ高さ:567\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
PRINTFORML client={CLIENTWIDTH()}x{CLIENTHEIGHT()}
PRINTFORML exists={EXISTFILE("CSV/GameBase.csv")}:{EXISTFILE("../outside.txt")}:{EXISTFILE("CSV")}
A = ENUMFILES("CSV", "*.csv", 0)
PRINTFORML direct={A}:{RESULTS:0}:{RESULTS:1}
ENUMFILES "CSV", "*.csv", 1
PRINTFORML rec={RESULT}:{RESULTS:0}:{RESULTS:1}:{RESULTS:2}
PRINTFORML mem={GETMEMORYUSAGE() > 0}:{CLEARMEMORY() >= 0}
UPDATECHECK
RETURN
''', encoding="utf-8")
        self.addCleanup(td.cleanup)
        program = load_program(root)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(
            "".join(rt.output),
            "client=1234x567\n"
            "exists=1:0:0\n"
            "direct=2:CSV\\A.csv:CSV\\GameBase.csv\n"
            "rec=3:CSV\\A.csv:CSV\\GameBase.csv:CSV\\Sub\\B.csv\n"
            "mem=1:1\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_await_keyboard_mouse_polling_helpers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
AWAIT 12
PRINTFORML tick={ISACTIVE()}:{MOUSEX()}:{MOUSEY()}:%MOUSEB()%:{GETKEY(65)}:{GETKEY(66)}
PRINTFORML trig={GETKEYTRIGGERED(65)}:{GETKEYTRIGGERED(65)}
GETKEY 65
PRINTFORML keycmd={RESULT}:{RESULTS}
GETKEYTRIGGERED 67
PRINTFORML trigcmd={RESULT}:{GETKEYTRIGGERED(67)}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.key_state = {65}
        rt.key_triggered = {65, 67}
        rt.mouse_x = 123
        rt.mouse_y = -45
        rt.mouse_button = "buy/7"
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(
            "".join(rt.output),
            "tick=1:123:-45:buy/7:1:0\n"
            "trig=1:0\n"
            "keycmd=1:1\n"
            "trigcmd=1:0\n",
        )
        self.assertEqual(rt.await_count, 1)
        self.assertEqual(rt.last_await_millis, 12)
        self.assertEqual(rt.warnings, [])

        rt_inactive = EraRuntime(program, echo=False, interactive=False)
        rt_inactive.is_active = False
        rt_inactive.key_state = {65}
        rt_inactive.key_triggered = {65}
        rt_inactive.mouse_x = 9
        rt_inactive.mouse_y = -8
        rt_inactive.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("tick=0:9:-8::0:0\ntrig=0:0\nkeycmd=0:0\n", "".join(rt_inactive.output))
        self.assertEqual(rt_inactive.warnings, [])

    def test_inputany_binput_and_flowinput_defaults(self):
        td, program = self.make_game('''@SYSTEM_TITLE
INPUTANY
PRINTFORML any1={RESULT}:%RESULTS%
INPUTANY
PRINTFORML any2={RESULT}:%RESULTS%
PRINTBUTTON "[A]", "A"
BINPUTS "fallback"
PRINTFORML bs=%RESULTS%
BINPUT 7
PRINTFORML bd={RESULT}:%RESULTS%
PRINTBUTTON "[5]", 5
BINPUT
PRINTFORML ba={RESULT}:%RESULTS%
FLOWINPUT 42, 1, 1, 1
__SHOPINPUT
PRINTFORML flow={RESULT}:%RESULTS%
FLOWINPUTS 1, "left"
__TRAININPUT
PRINTFORML flows={RESULT}:%RESULTS%
FLOWINPUTS 0
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["123", "word", "A"])
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(
            "".join(rt.output),
            "any1=123:\n"
            "any2=0:word\n"
            "[A]bs=A\n"
            "bd=7:7\n"
            "[5]ba=5:5\n"
            "flow=42:42\n"
            "flows=1:left\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_sound_commands_and_existsound_helper(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "sound").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "sound" / "Se.WAV").write_bytes(b"RIFF")
        (root / "sound" / "BGM.ogg").write_bytes(b"OggS")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
PRINTFORML ex={EXISTSOUND("se.wav")}:{EXISTSOUND("missing.wav")}:{EXISTSOUND("../CSV/GameBase.csv")}
PLAYSOUND "Se.WAV"
PLAYSOUND "missing.wav"
PLAYBGM "BGM.ogg"
SETSOUNDVOLUME 77
SETBGMVOLUME 33
EXISTSOUND "BGM.ogg"
PRINTFORML cmd={RESULT}:%RESULTS%
STOPSOUND
STOPBGM
RETURN
''', encoding="utf-8")
        self.addCleanup(td.cleanup)
        program = load_program(root)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "ex=1:0:0\ncmd=1:1\n")
        self.assertEqual(rt.sound_effects, [])
        self.assertEqual(rt.current_bgm, "")
        self.assertEqual(rt.sound_volume, 77)
        self.assertEqual(rt.bgm_volume, 33)
        self.assertEqual(
            [event["action"] for event in rt.sound_events],
            ["playsound", "playsound", "playbgm", "setsoundvolume", "setbgmvolume", "stopsound", "stopbgm"],
        )
        self.assertTrue(rt.sound_events[0]["exists"])
        self.assertFalse(rt.sound_events[1]["exists"])
        self.assertTrue(rt.sound_events[2]["exists"])
        self.assertEqual(rt.warnings, [])

    def test_expression_function_ref_arguments_alias_arrays(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS S, 2 = "A", "B"
PRINTFORML %JOIN(S)%
RETURN

@JOIN(R)
#FUNCTIONS
#DIMS REF R, 0
RETURNF R:0 + R:1
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "AB\n")

    def test_variable_size_csv_and_function_fallthrough_default(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "VariableSize.csv").write_text("ITEM,1234\nDA,100,200\nTA,2500,20,20\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
RESULT = 99
PRINTFORML {FALLS_THROUGH()}:{RESULT}
VARSIZE ITEM
PRINTFORML size={RESULT}
PRINTFORML expr={VARSIZE("ITEM")}
VARSIZE DA, 1
PRINTFORML da={RESULT}:{RESULT:0}:{RESULT:1}:{VARSIZE("DA")}:{VARSIZE("DA", 0)}:{VARSIZE("DA", 1)}:{VARSIZE("DA", 2)}
PRINTFORML ta={VARSIZE("TA")}:{VARSIZE("TA", 1)}:{VARSIZE("TA", 2)}:{VARSIZE("TA", 3)}
RETURN

@FALLS_THROUGH()
#FUNCTION
SIF 0
RETURNF 1
''', encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("0:99", out)
        self.assertIn("size=1234", out)
        self.assertIn("expr=1234", out)
        self.assertIn("da=200:100:200:100:100:200:0", out)
        self.assertIn("ta=2500:20:20:0", out)

    def test_variable_size_system_names_shadow_csv_constants(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML init={R}:{SELECTCOM}:{VARSIZE("R")}:{VARSIZE("SELECTCOM")}:{VARSIZE("DA", 1)}
A:R = 5
R = 2
SELECTCOM = 3
A:R = 9
PRINTFORML vals={A:0}:{A:2}:{A:99}:{R}:{SELECTCOM}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "_Rename.csv").write_text("99,R\n77,SELECTCOM\n", encoding="utf-8")
        (root / "CSV" / "VariableSize.csv").write_text("R,5\nSELECTCOM,7\nDA,2,3\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "init=0:0:5:7:3\nvals=5:9:0:2:3\n")
        self.assertEqual(rt.warnings, [])

    def test_fixed_character_arrays_use_character_namespace(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 0
ADDCHARA 0
SOURCE:0:1 = 11
SOURCE:1:1 = 22
RELATION:0:2 = 33
STAIN:0:3 = 44
GOTJUEL:0:4 = 55
NOWEX:0:5 = 66
PRINTFORML c0={SOURCE:0:1}:{RELATION:0:2}:{STAIN:0:3}:{GOTJUEL:0:4}:{NOWEX:0:5}
PRINTFORML c1={SOURCE:1:1}:{RELATION:1:2}:{STAIN:1:3}:{GOTJUEL:1:4}:{NOWEX:1:5}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "c0=11:33:44:55:66\nc1=22:0:0:0:0\n")
        self.assertEqual(rt.warnings, [])
        self.assertEqual(rt.memory.characters[0].numeric["SOURCE"][(1,)], 11)
        self.assertEqual(rt.memory.characters[1].numeric["SOURCE"][(1,)], 22)
        self.assertNotIn("SOURCE", rt.memory.numeric)

    def test_character_arrays_omit_character_index_to_target(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 0
ADDCHARA 0
TARGET = 1
TALENT:恋慕 = 7
TALENT:親愛 = 8
BASE:LV = 12
CSTR:一人称 = "私"
CALLNAME = "二番目"
TARGET = 0
TALENT:恋慕 = 5
CSTR:一人称 = "僕"
CALLNAME = "一番目"
PRINTFORML ch0={TALENT:0:恋慕}:{TALENT:0:親愛}:{BASE:0:LV}:%CSTR:0:一人称%:%CALLNAME:0%
PRINTFORML ch1={TALENT:1:恋慕}:{TALENT:1:親愛}:{BASE:1:LV}:%CSTR:1:一人称%:%CALLNAME:1%
TARGET = 1
PRINTFORML omitted={TALENT:恋慕}:{TALENT:親愛}:{BASE:LV}:%CSTR:一人称%:%CALLNAME%
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Talent.csv").write_text("0,恋慕\n1,親愛\n", encoding="utf-8")
        (root / "CSV" / "Base.csv").write_text("0,LV\n", encoding="utf-8")
        (root / "CSV" / "CStr.csv").write_text("0,一人称\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=120)
        self.assertEqual(
            "".join(rt.output),
            "ch0=5:0:0:僕:一番目\n"
            "ch1=7:8:12:私:二番目\n"
            "omitted=7:8:12:私:二番目\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_varset_and_arraycopy_resolve_character_array_prefixes(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 0
ADDCHARA 0
TARGET = 1
CFLAG:0:恋慕 = 101
CFLAG:0:親愛 = 102
CFLAG:1:恋慕 = 201
CFLAG:1:親愛 = 202
CFLAG:1:友好度 = 203
CFLAG:1:火炎 = 204
VARSET CFLAG:親愛, 0
PRINTFORML clear={CFLAG:0:恋慕}:{CFLAG:0:親愛}:{CFLAG:1:恋慕}:{CFLAG:1:親愛}:{CFLAG:1:友好度}:{CFLAG:1:火炎}
VARSET CFLAG:友好度, 9
VARSET CFLAG:TARGET:0, 7, 3, 4
PRINTFORML fill={CFLAG:1:恋慕}:{CFLAG:1:親愛}:{CFLAG:1:友好度}:{CFLAG:1:剣撃}:{CFLAG:1:火炎}
ARRAYCOPY "CFLAG:TARGET", "A"
ARRAYCOPY "CFLAG:友好度", "B"
PRINTFORML copy={A:0}:{A:1}:{A:2}:{A:3}:{A:4}:{B}
ARRAYREMOVE CFLAG:TARGET, 1, 1
PRINTFORML remove={CFLAG:1:恋慕}:{CFLAG:1:親愛}:{CFLAG:1:友好度}:{CFLAG:1:剣撃}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "CFlag.csv").write_text(
            "0,恋慕\n1,親愛\n2,友好度\n3,剣撃\n4,火炎\n",
            encoding="utf-8",
        )
        (root / "CSV" / "VariableSize.csv").write_text("CFLAG,5\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(
            "".join(rt.output),
            "clear=101:102:201:0:0:0\n"
            "fill=201:0:9:7:9\n"
            "copy=201:0:9:7:9:9\n"
            "remove=201:9:7:9\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_declared_charadata_arrays_are_per_character_and_string_varset_clears(self):
        td, program = self.make_game('''#DIM CHARADATA RPG_CFLAG_DUNGEON, 4
#DIMS CHARADATA RPG_CSTR_DUNGEON, 4
@SYSTEM_TITLE
ADDCHARA 0
ADDCHARA 0
TARGET = 1
RPG_CFLAG_DUNGEON:0:0 = 11
RPG_CFLAG_DUNGEON:1:0 = 22
RPG_CFLAG_DUNGEON:2 = 33
RPG_CSTR_DUNGEON:0:0 '= keep
RPG_CSTR_DUNGEON:1:0 '= old
RPG_CSTR_DUNGEON:1:1 '= tail
VARSET RPG_CSTR_DUNGEON:TARGET:0
PRINTFORML n={RPG_CFLAG_DUNGEON:0:0}:{RPG_CFLAG_DUNGEON:1:0}:{RPG_CFLAG_DUNGEON:1:2}:{RPG_CFLAG_DUNGEON:2}
PRINTFORML s=%RPG_CSTR_DUNGEON:0:0%/%RPG_CSTR_DUNGEON:1:0%/%RPG_CSTR_DUNGEON:1:1%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(
            "".join(rt.output),
            "n=11:22:33:33\n"
            "s=keep//\n",
        )
        self.assertEqual(rt.warnings, [])
        self.assertEqual(rt.memory.characters[1].numeric["RPG_CFLAG_DUNGEON"][(2,)], 33)
        self.assertEqual(rt.memory.characters[1].strings["RPG_CSTR_DUNGEON"][(0,)], "")
        self.assertNotIn("RPG_CFLAG_DUNGEON", rt.memory.numeric)
        self.assertNotIn("RPG_CSTR_DUNGEON", rt.memory.strings)
        save = native_save_from_memory(rt.memory, file_type=SaveFileType.NORMAL)
        self.assertIn("RPG_CFLAG_DUNGEON", save.characters[1].numeric)
        self.assertIn("RPG_CSTR_DUNGEON", save.characters[1].strings)
        save_path = Path(td.name) / "charadata.sav"
        write_native_save(save_path, save, program)
        self.assertIn(bytes([SaveDataType.SEPARATOR]), save_path.read_bytes())
        roundtrip = read_native_save(save_path, program)
        self.assertEqual(roundtrip.characters[1].numeric["RPG_CFLAG_DUNGEON"][(2,)], 33)
        self.assertEqual(roundtrip.characters[0].strings["RPG_CSTR_DUNGEON"][(0,)], "keep")

    def test_era_megaten_native_hot_helpers_stay_sparse(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM DYNAMIC A, 20000
CALL MouseUIStore_Set_Value()
CALL MouseUIStore_Yen_OnSales(A)
PRINTFORML {魔晶装備(2450)}:{魔晶装備(2500)}:{RESULT}:{A:1}
RETURN

@MouseUIStore_Set_Value()
FOR LOCAL, 0, 100000
NEXT
RESULT = 9
RETURN

@MouseUIStore_Yen_OnSales(rOnSales)
#DIM REF rOnSales, 0
FOR LOCAL, 0, 100000
NEXT
rOnSales:1 = 1
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "1:0:0:0\n")

    def test_cvarset_applies_to_all_characters(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 0
ADDCHARA 0
CVARSET CFLAG, 5, 12
PRINTFORML {CFLAG:0:5}/{CFLAG:1:5}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Chara0.csv").write_text("番号,0\n名前,A\n呼び名,A\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("12/12", "".join(rt.output))

    def test_sidecar_savedata_and_global_persistence(self):
        td, program = self.make_game('''@SYSTEM_TITLE
GLOBAL:2 = 77
FLAG:1 = 11
ADDCHARA 0
CFLAG:0:3 = 44
SAVEGLOBAL
SAVEDATA 7, "slot"
GLOBAL:2 = 0
FLAG:1 = 0
CFLAG:0:3 = 0
RESETDATA
LOADGLOBAL
CHKDATA 7
LOCAL = RESULT
LOADDATA 7
PRINTL after-load
RETURN

@EVENTLOAD
PRINTFORML G={GLOBAL:2}|F={FLAG:1}|C={CFLAG:0:3}|N={CHARANUM}|CHK={LOCAL}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Chara0.csv").write_text("番号,0\n名前,A\n呼び名,A\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertIn("G=77|F=11|C=44|N=1|CHK=0", "".join(rt.output))
        self.assertNotIn("after-load", "".join(rt.output))
        self.assertTrue((root / ".eramegaten_engine_saves" / "global.engine.json").exists())
        self.assertTrue((root / ".eramegaten_engine_saves" / "save007.engine.json").exists())

    def test_loaddata_flow_calls_system_loadend_eventload_show_shop(self):
        td, program = self.make_game('''@SYSTEM_TITLE
FLAG:1 = 42
SAVEDATA 0, "slot"
FLAG:1 = 0
LOADDATA 0
PRINTL bad-after-load
RETURN

@SYSTEM_LOADEND
PRINTFORML SYS={FLAG:1}
RETURN

@EVENTLOAD
PRINTFORML EVT={FLAG:1}
RETURN

@SHOW_SHOP
PRINTFORML SHOP={FLAG:1}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("SYS=42\nEVT=42\nSHOP=42\n", out)
        self.assertNotIn("bad-after-load", out)

    def test_times_gettime_encode_to_uni_commands(self):
        td, program = self.make_game('''@SYSTEM_TITLE
A = 8
TIMES A, 1.25
GETTIME
T = RESULT
ENCODETOUNI "Az"
PRINTFORML {A}:{RESULT}:{RESULT:1}:{RESULT:2}:{T > 0}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("10:2:65:122:1", "".join(rt.output))

    def test_swap_incdec_builtin_commands_and_brace_join(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS S
A = 2
B = 5
SWAP A, B
A++
B --
C = 0
D = ++(C) % 2
VARSIZE A
LOCAL = RESULT > 0
SUBSTRING "abcdef", 1, 3
{
S '= "x" + RESULTS
 + "z"
}
SUBSTRING "abcdef", 2, -1
S '= S + ":" + RESULTS
PRINTFORML {A}:{B}:{C}:{D}:{LOCAL}:%S%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("6:1:1:1:1:xbcdz:cdef", "".join(rt.output))

    def test_private_math_functions_and_command_form(self):
        td, program = self.make_game('''@SYSTEM_TITLE
LOG 100
A = RESULT
CBRT -64
B = RESULT
EXPONENT 2
C = RESULT
PRINTFORML {A}:{B}:{C}:{LOG(100)}:{LOG10(999)}:{CBRT(27)}:{EXPONENT(0)}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "4:-4:7:4:2:3:1\n")
        self.assertEqual(rt.warnings, [])

    def test_split_form_condition_and_onekey_auto(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS KEY, 4
#DIM 消費済み
ARG:1 = 2
FLAG:迷宫内操作设定 = 0
SPLIT \\@ FLAG:迷宫内操作设定 == 0 ? 8_4_6_2 # w_a_s_d \\@, "_", KEY
CALL INPUT_ONEKEY_TAP_RESULTS
RESULTS:1 = RESULTS
消費済み = 2
CALL INPUT_ONEKEY_TAP_RESULTS
PRINTFORML %KEY:0%/%KEY:1%/%RESULTS:1%/%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("8/4/6/", "".join(rt.output))

    def test_split_updates_result_without_count_argument(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS PARTS, 4
RESULT = 99
SPLIT "a,b", ",", PARTS
PRINTFORML {RESULT}:%RESULTS%:%PARTS:0%/%PARTS:1%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "2::a/b\n")

    def test_display_config_string_builtins_and_redraw_state(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM NOS
PRINTFORM \\@ LINEISEMPTY() ? empty # bad \\@
PRINTL
PRINTFORM \\@ LINEISEMPTY() ? yes # no \\@
PRINTFORM %/%
SAVESTR:0 = /
REDRAW 2
SETFONT "Arial"
ALIGNMENT CENTER
PRINTFORML |%SAVESTR:0%|{CURRENTREDRAW()}|%CURRENTALIGN()%|%GETFONT()%|{CHKFONT("Arial")}|%GETCONFIGS("描画インターフェース")%|{GETCONFIG("フォントサイズ")}|{GETFOCUSCOLOR()}|{GETDEFCOLOR()}|{GETBGCOLOR()}
ALIGNMENT RIGHT
PRINTFORML |%CURRENTALIGN()%
SETFONT
GETFONT
PRINTFORML |%RESULTS%
SAVENOS NOS
PRINTFORML |save={NOS}:{SAVENOS()}
RETURN
''')
        root = Path(td.name)
        (root / "emuera.config").write_text("描画インターフェース:TEXTRENDERER\nフォントサイズ:20\n一行の高さ:20\n文字色:5,6,7\n背景色:8,9,10\n選択中文字色:1,2,3\n表示するセーブデータ数:42\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        self.assertEqual(eval_expr(rt, 'TOFULL("A 12")'), "Ａ　１２")
        self.assertEqual(eval_expr(rt, 'TOHALF("ＡＢ１２")'), "AB12")
        self.assertEqual(eval_expr(rt, 'CHARATU("abc", 1)'), "b")
        self.assertEqual(eval_expr(rt, 'STRFINDU("ababa", "ba", 2)'), 3)
        self.assertEqual(eval_expr(rt, r'STRCOUNT("a1 b22 c333", "\d+")'), 3)
        self.assertEqual(eval_expr(rt, r'REPLACE("a//b///c", "\/+", "/")'), "a/b/c")
        self.assertEqual(eval_expr(rt, r'REPLACE("ab12", "([a-z]+)(\d+)", "$2-$1")'), "12-ab")
        self.assertEqual(eval_expr(rt, 'GETLINESTR("=", 5)'), "=====")
        self.assertEqual(eval_expr(rt, "GETPALAMLV(2999, 17)"), 2)
        self.assertEqual(eval_expr(rt, "GETPALAMLV(3000, 17)"), 3)
        self.assertEqual(eval_expr(rt, "GETEXPLV(499, 10)"), 5)
        self.assertEqual(eval_expr(rt, "GETEXPLV(500, 10)"), 6)
        rt.memory.set_var("EXPLV", [6], 777)
        self.assertEqual(eval_expr(rt, "GETEXPLV(776, 10)"), 5)
        self.assertEqual(eval_expr(rt, "GETEXPLV(777, 10)"), 6)
        self.assertEqual(eval_expr(rt, "GETFOCUSCOLOR()"), 0x010203)
        self.assertEqual(eval_expr(rt, "GETDEFCOLOR()"), 0x050607)
        self.assertEqual(eval_expr(rt, "GETBGCOLOR()"), 0x08090A)
        self.assertEqual(eval_expr(rt, "SAVENOS()"), 42)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn(" empty \n yes /|/|0|CENTER|Arial|1|TEXTRENDERER|20|66051|329223|526602\n|RIGHT\n|ＭＳ ゴシック", "".join(rt.output))
        self.assertIn("|save=42:42", "".join(rt.output))

    def test_isdefined_and_existvar_scope_type_bits(self):
        td, program = self.make_game('''#DEFINE 体力 0
#DIM CONST BIT = 0, 1, 2, 4, 8, 16
@SYSTEM_TITLE
#DIM キャラデータ, 2, 2
#DIMS 名前
PRINTFORML def={ISDEFINED("体力")}:{ISDEFINED("不明")}
PRINTFORML ex={EXISTVAR("キャラデータ")}:{EXISTVAR("BIT")}:{EXISTVAR("名前")}:{EXISTVAR("性別")}:{EXISTVAR("RESULTS")}
EXISTVAR "名前"
PRINTFORML cmd={RESULT}:{RESULTS}
CALL Foo
PRINTFORML foo=%RESULTS%
RETURN

@Foo
#DIMS 性別
RESULTS = TOSTR(EXISTVAR("性別")) + "/" + TOSTR(EXISTVAR("キャラデータ"))
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(
            "".join(rt.output),
            "def=1:0\n"
            "ex=9:5:2:0:2\n"
            "cmd=2:2\n"
            "foo=2/0\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_existfunction_and_enum_introspection_helpers(self):
        td, program = self.make_game('''#DEFINE Foo2 "Test"
#DEFINE Foo3
#DEFINE MyFoo 1 + 1
#DIM CONST Foo1 = 1
#DIMS CONST Foo3 = "3"
#DIM MyFoo
@SYSTEM_TITLE
#DIMS Local3DFoo, 2, 2, 2
PRINTFORML exists={EXISTFUNCTION("NormalFoo")}:{EXISTFUNCTION("NumberFoo")}:{EXISTFUNCTION("StringFoo")}:{EXISTFUNCTION("ABS")}
ENUMFUNCBEGINSWITH FooTarget
PRINTFORML fb={RESULT}:{RESULTS:0}:{RESULTS:1}
ENUMFUNCENDSWITH Foo
PRINTFORML fe={RESULT}:{RESULTS:0}:{RESULTS:1}:{RESULTS:2}
ENUMMACROWITH Foo
PRINTFORML mw={RESULT}:{RESULTS:0}:{RESULTS:1}:{RESULTS:2}
ENUMVARENDSWITH Foo
PRINTFORML ve={RESULT}:{RESULTS:0}
ENUMVARWITH Foo
PRINTFORML vw={RESULT}:{RESULTS:0}:{RESULTS:1}:{RESULTS:2}
RETURN

@NormalFoo
RETURN

@NumberFoo
#FUNCTION
RETURNF 7

@StringFoo
#FUNCTIONS
RETURNF "S"

@FooTargetA
RETURN

@FooTargetB
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(
            "".join(rt.output),
            "exists=1:2:3:0\n"
            "fb=2:FooTargetA:FooTargetB\n"
            "fe=3:NormalFoo:NumberFoo:StringFoo\n"
            "mw=3:Foo2:Foo3:MyFoo\n"
            "ve=1:MyFoo\n"
            "vw=3:Foo1:Foo3:MyFoo\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_getsetvar_and_resetglobal_helpers(self):
        td, program = self.make_game('''#DIM GLOBAL G, 3 = 1, 2, 3
#DIMS GLOBAL GS, 2 = "a", "b"
#DIM N = 5
@SYSTEM_TITLE
#DIMS L = "local"
SETVAR "N", 7
SETVAR "L", "apple"
SETVAR "G:1", 22
SETVAR "GS:0", "zed"
PRINTFORML dyn={GETVAR("N")}:{GETVAR("G:1")}:%GETVARS("L")%:%GETVARS("GS:0")%:{RESULT}
GETVARS "L"
PRINTFORML cmd=%RESULTS%
SETVAR "N", 9
RESETGLOBAL
PRINTFORML reset={G:0}:{G:1}:%GS:0%:%L%:{N}:{GETVAR("N")}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(
            "".join(rt.output),
            "dyn=7:22:apple:zed:1\n"
            "cmd=apple\n"
            "reset=0:0::apple:9:9\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_varsetex_dynamic_array_fill_modes(self):
        td, program = self.make_game('''#DIM ARR = 1, 2, 3, 4, 5, 6
#DIM ARR2, 3, 4
@SYSTEM_TITLE
#DIMS L, 3 = "Cat1", "Cat2", "Cat3"
VARSETEX "L", "dog"
PRINTFORML s=%L:0%/%L:1%/%L:2%:{RESULT}
VARSETEX "ARR", -1, 0, 3, 5
PRINTFORML a={ARR:0}{ARR:1}{ARR:2}{ARR:3}{ARR:4}{ARR:5}:{RESULT}
VARSETEX "ARR2:1:2", 9
VARSETEX "ARR2:1:2", 7, 0, 1
PRINTFORML m={ARR2:0:1}:{ARR2:0:2}:{ARR2:1:1}:{ARR2:1:2}:{ARR2:2:3}
PRINTFORML expr={VARSETEX("ARR:0", 8, 0, 0, 1)}:{ARR:0}:{ARR:1}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(
            "".join(rt.output),
            "s=dog/dog/dog:1\n"
            "a=123-1-16:1\n"
            "m=0:9:0:7:9\n"
            "expr=1:8:2\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_arraymsort_multidim_and_arraymsortex_helpers(self):
        td, program = self.make_game('''#DIM A1, 4
#DIM A2, 4
#DIM A3, 4, 3
#DIM IDX, 4
#DIM AA, 4
#DIM BB, 4
#DIMS ARRAYS, 3
#DIMS ARRAYS2, 2
@SYSTEM_TITLE
A1:0 = 3
A1:1 = 1
A1:2 = 2
A1:3 = 0
A2:0 = 1001
A2:1 = 1002
A2:2 = 1003
A2:3 = 0
A3:0:0 = 1
A3:0:1 = 101
A3:0:2 = 2763
A3:1:0 = 2
A3:1:1 = 102
A3:1:2 = 9615
A3:2:0 = 3
A3:2:1 = 103
A3:2:2 = 7035
ARRAYMSORT A1, A2, A3
PRINTFORML ms={RESULT}:{A1:0},{A1:1},{A1:2},{A1:3}:{A2:0},{A2:1},{A2:2}:{A3:0:0}/{A3:0:1}/{A3:0:2}:{A3:1:0}/{A3:1:1}/{A3:1:2}:{A3:2:0}/{A3:2:1}/{A3:2:2}
IDX:0 = 4
IDX:1 = 2
IDX:2 = 3
IDX:3 = 1
AA:0 = 1
AA:1 = 2
AA:2 = 3
AA:3 = 4
BB:0 = 5
BB:1 = 3
BB:2 = 1
BB:3 = 2
ARRAYS:0 = "IDX"
ARRAYS:1 = "AA"
ARRAYS:2 = "BB"
PRINTFORML ex={ARRAYMSORTEX(IDX, ARRAYS)}:{IDX:0},{IDX:1},{IDX:2},{IDX:3}:{AA:0},{AA:1},{AA:2},{AA:3}:{BB:0},{BB:1},{BB:2},{BB:3}
IDX:0 = 4
IDX:1 = 2
IDX:2 = 3
IDX:3 = 1
AA:0 = 1
AA:1 = 2
AA:2 = 3
AA:3 = 4
BB:0 = 5
BB:1 = 3
BB:2 = 1
BB:3 = 2
ARRAYMSORTEX "IDX", ARRAYS, 0, 4
PRINTFORML desc={RESULT}:{IDX:0},{IDX:1},{IDX:2},{IDX:3}:{AA:0},{AA:1},{AA:2},{AA:3}:{BB:0},{BB:1},{BB:2},{BB:3}
IDX:0 = 4
IDX:1 = 2
IDX:2 = 3
IDX:3 = 1
AA:0 = 1
AA:1 = 2
AA:2 = 3
AA:3 = 4
BB:0 = 5
BB:1 = 3
BB:2 = 1
BB:3 = 2
ARRAYS2:0 = "AA"
ARRAYS2:1 = "BB"
ARRAYMSORTEX IDX, ARRAYS2
PRINTFORML nosort={RESULT}:{IDX:0},{IDX:1},{IDX:2},{IDX:3}:{AA:0},{AA:1},{AA:2},{AA:3}:{BB:0},{BB:1},{BB:2},{BB:3}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=300)
        self.assertEqual(
            "".join(rt.output),
            "ms=1:1,2,3,0:1002,1003,1001:2/102/9615:3/103/7035:1/101/2763\n"
            "ex=1:1,2,3,4:4,2,3,1:2,3,1,5\n"
            "desc=1:4,3,2,1:1,3,2,4:5,1,3,2\n"
            "nosort=1:4,2,3,1:4,2,3,1:2,3,1,5\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_strform_strjoin_and_regexpmatch_helpers(self):
        td, program = self.make_game(r'''@SYSTEM_TITLE
#DIMS PARTS, 5
SPLIT "a,b,,d", ",", PARTS
PRINTFORML join=%STRJOIN(PARTS, "|", 1, 3)%/%STRJOIN(PARTS, "", 0, 4)%
NAME:0 = "Alice"
PRINTFORML form=%STRFORM("%NAME:0%:{1+2}")%
A = REGEXPMATCH("Apple Banana Car", ".(.{2})\b", 1)
PRINTFORML regex={A}:{RESULT:1}:%RESULTS:0%/%RESULTS:1%/%RESULTS:2%/%RESULTS:3%/%RESULTS:4%/%RESULTS:5%
REGEXPMATCH "xx yy", "([a-z])([a-z])", 1
PRINTFORML regcmd={RESULT}:{RESULT:1}:%RESULTS:0%/%RESULTS:1%/%RESULTS:2%/%RESULTS:3%/%RESULTS:4%/%RESULTS:5%
#DIM GC
#DIMS MATCHES, 8
B = REGEXPMATCH("up down", "([a-z]+)", GC, MATCHES)
PRINTFORML ref={B}:{GC}:%MATCHES:0%/%MATCHES:1%/%MATCHES:2%/%MATCHES:3%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(
            "".join(rt.output),
            "join=b||d/abd\n"
            "form=Alice:3\n"
            "regex=3:2:ple/le/ana/na/Car/ar\n"
            "regcmd=2:3:xx/x/x/yy/y/y\n"
            "ref=2:2:up/up/down/down\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_randomize_dumprand_and_initrand_restore_rand_state(self):
        td, program = self.make_game('''@SYSTEM_TITLE
RANDOMIZE 12345
A = RAND:100000
B = RAND(100000)
RANDOMIZE 12345
C = RAND:100000
D = RAND(100000)
RANDOMIZE 67890
DUMPRAND
E = RAND:100000
F = RAND(100000)
G = RAND:100000
INITRAND
H = RAND:100000
I = RAND(100000)
J = RAND:100000
INITRAND
K = RAND:100000
L = RAND(100000)
M = RAND:100000
PRINTFORML rand={A == C}:{B == D}:{E == H}:{F == I}:{G == J}:{E == K}:{F == L}:{G == M}:{RANDDATA:624 >= 0}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "rand=1:1:1:1:1:1:1:1:1\n")
        self.assertEqual(rt.warnings, [])

    def test_display_line_log_and_outputlog_helpers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL AAA
PRINTL BBB
PRINTFORML gl=%GETDISPLAYLINE(0)%/%GETDISPLAYLINE(1)%/{LINECOUNT}
GETDISPLAYLINE 1
BITMAP_CACHE_ENABLE 1
PRINTFORML cmd=%RESULTS%:bmp={BITMAP_CACHE_ENABLE(0)}:{BITMAP_CACHE_ENABLE(1)}
SKIPLOG 1
PUTFORM save={1+2}/%GAMEBASE_TITLE%
OUTPUTLOG "logs/out.txt"
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        expected = (
            "AAA\n"
            "BBB\n"
            "gl=AAA/BBB/2\n"
            "cmd=BBB:bmp=0:1\n"
        )
        self.assertEqual("".join(rt.output), expected)
        self.assertEqual(rt.warnings, [])
        self.assertTrue(rt.log_skip)
        self.assertTrue(rt.bitmap_cache_enabled)
        self.assertEqual(rt.save_info_lines, ["save=3/Test"])
        self.assertEqual((program.root / "logs" / "out.txt").read_text(encoding="utf-16"), expected)
        self.assertEqual(len(rt.output_log_files), 1)

    def test_erdname_and_dynamic_vari_vars_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "HOGE3D@1.ERD").write_text("0,AAA\n1,BBB\n2,CCC\n", encoding="utf-8")
        (root / "HOGE3D@2.ERD").write_text("0,DDD\n1,EEE\n2,FFF\n", encoding="utf-8")
        (root / "HOGE3D@3.ERD").write_text("0,GGG\n1,HHH\n2,III\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@DYN_LOCAL
VARI ANSWER = 42
VARS QUESTION = "life"
VARI INTEGER, 3
VARS LABELS, 2
INTEGER:3 = 84
LABELS:1 = "tail"
PRINTFORML dyn={ANSWER}:%QUESTION%:{VARSIZE(INTEGER)}:{INTEGER:0}:{INTEGER:3}:%LABELS:1%:{EXISTVAR("ANSWER")}:{EXISTVAR("QUESTION")}
RETURN
@SYSTEM_TITLE
PRINTFORML erd=%ERDNAME(HOGE3D, 0, 1)%/%ERDNAME(HOGE3D, 1, 2)%/%ERDNAME(HOGE3D, 2, 3)%/{GETNUM(HOGE3D, "BBB")}
ERDNAME HOGE3D, 1, 2
PRINTFORML cmd=%RESULTS%
CALL DYN_LOCAL
PRINTFORML outer={EXISTVAR("ANSWER")}:{EXISTVAR("QUESTION")}
RETURN
''', encoding="utf-8")
        program = load_program(root)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(
            "".join(rt.output),
            "erd=AAA/EEE/III/1\n"
            "cmd=EEE\n"
            "dyn=42:life:3:0:84:tail:1:2\n"
            "outer=0:0\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_persona_slot_function_fast_path_in_dynamic_indices(self):
        td, program = self.make_game('''@Persona(ARGS)
SIF ARGS == "NO"
    RETURNF 1
SIF ARGS == "LV" || ARGS == "ＬＶ"
    RETURNF 2
SIF ARGS == "習得技能1"
    RETURNF 40
THROW "ARGSが異常です"
@SYSTEM_TITLE
DITEMTYPE:0:Persona("NO") = 99
DITEMTYPE:0:Persona("LV") = 15
LOCALS = "LV"
DITEMTYPE:0:Persona(LOCALS) += 1
DITEMTYPE:0:(Persona("習得技能1") + 1) = 123
PRINTFORML p={Persona("NO")}:{GET_DITEMTYPE_NUM("LV")}:%GET_DITEMTYPE(2)%:{DITEMTYPE:0:1}:{DITEMTYPE:0:2}:{DITEMTYPE:0:Persona("ＬＶ")}:{DITEMTYPE:0:41}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "p=1:2:LV:99:16:16:123\n")
        self.assertEqual(rt.warnings, [])

    def test_mantra_mapname_and_num_fast_path_in_dynamic_indices(self):
        td, program = self.make_game('''#DIM MANTRA_ABLE, 200
#DIM 真言座標, 200
@SYSTEM_TITLE
STR:701 = "真言／喰奴"
STR:815 = "真言／熾天使"
PRINTFORML m=%GET_MANTRA_MAPNAME(7,7)%:{GET_MANTRA_NUM(GET_MANTRA_MAPNAME(7,7))}:%GET_MANTRA(1)%
A = GET_MANTRA_MAPNAME(-1,7)
PRINTFORML bad=%A%:{RESULT:1}:{GET_MANTRA_NUM("NONE")}
MANTRA_ABLE:GET_MANTRA_NUM(GET_MANTRA_MAPNAME(7,7)) = 5
真言座標:GET_MANTRA_NUM(GET_MANTRA_MAPNAME(0,7)) = 7000
PRINTFORML arr={MANTRA_ABLE:1}:{真言座標:115}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual("".join(rt.output), "m=喰奴:1:喰奴\nbad=NONE:-1:0\narr=5:7000\n")
        self.assertEqual(rt.warnings, [])

    def test_currentredraw_reports_persistent_redraw_state_bits(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML r0={CURRENTREDRAW()}
REDRAW 0
PRINTFORML r1={CURRENTREDRAW()}
REDRAW 1
PRINTFORML r2={CURRENTREDRAW()}
REDRAW 2
PRINTFORML r3={CURRENTREDRAW()}
REDRAW 3
PRINTFORML r4={CURRENTREDRAW()}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "r0=1\nr1=0\nr2=1\nr3=0\nr4=1\n")
        rt.current_redraw = 2
        self.assertEqual(eval_expr(rt, "CURRENTREDRAW()"), 0)
        rt.current_redraw = 3
        self.assertEqual(eval_expr(rt, "CURRENTREDRAW()"), 1)
        self.assertEqual(rt.warnings, [])

    def test_fontstyle_fontbold_getstyle_state(self):
        td, program = self.make_game('''@SYSTEM_TITLE
FONTSTYLE 4
PRINTFORM {GETSTYLE()}
FONTBOLD
GETSTYLE
PRINTFORML /{RESULT}/{GETSTYLE()}
FONTREGULAR
PRINTFORML {GETSTYLE()}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "4/5/5\n0\n")
        self.assertEqual(rt.warnings, [])

    def test_font_style_helper_commands_accumulate_dotnet_bits(self):
        td, program = self.make_game('''@SYSTEM_TITLE
FONTITALIC
PRINTFORM {GETSTYLE()}
FONTUNDERLINE
PRINTFORM /{GETSTYLE()}
FONTSTRIKEOUT
PRINTFORM /{GETSTYLE()}
FONTBOLD
PRINTFORM /{GETSTYLE()}
FONTREGULAR
PRINTFORML /{GETSTYLE()}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "2/6/14/15/0\n")
        self.assertEqual(rt.warnings, [])

    def test_begin_and_resetdata_reset_display_style(self):
        td, program = self.make_game('''@SYSTEM_TITLE
SETCOLOR 1, 2, 3
SETBGCOLOR 4, 5, 6
SETFONT "Arial"
FONTSTYLE 5
RESETDATA
PRINTFORML R={GETCOLOR()}:{GETBGCOLOR()}:%GETFONT()%:{GETSTYLE()}
SETCOLOR 9, 8, 7
SETBGCOLOR 6, 5, 4
SETFONT "Courier New"
FONTBOLD
BEGIN SHOP
PRINTL bad
RETURN

@SHOW_SHOP
PRINTFORML B={GETCOLOR()}:{GETBGCOLOR()}:%GETFONT()%:{GETSTYLE()}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "R=12632256:0:ＭＳ ゴシック:0\nB=12632256:0:ＭＳ ゴシック:0\n")
        self.assertEqual(rt.warnings, [])

    def test_begin_aftertrain_and_ablup_state_flow(self):
        td, program = self.make_game('''@SYSTEM_TITLE
BEGIN AFTERTRAIN
PRINTL unreachable

@EVENTEND
PRINTL end
BEGIN ABLUP

@EVENTTURNEND
PRINTL turn
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "end\nturn\n")
        self.assertEqual(rt.warnings, [])

    def test_begin_ablup_runs_manual_menu_when_input_is_queued(self):
        td, program = self.make_game('''@SYSTEM_TITLE
TARGET = 3
BEGIN ABLUP

@ABL_MANUAL_MAIN(ARG)
PRINTFORML abl={ARG}:{TARGET}
RETURN

@EVENTTURNEND
PRINTL done
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["100"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "abl=1:3\ndone\n")
        self.assertEqual(rt.warnings, [])

    def test_begin_discards_stale_buttons_before_next_state_input(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTBUTTON "[1] stale", 1
PRINTL
BEGIN TRAIN

@EVENTTRAIN
PRINTL train
INPUT
PRINTFORML picked={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["999"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "[1] stale\ntrain\npicked=999\n")
        self.assertEqual(rt.warnings, [])

    def test_begin_train_runs_status_usercom_loop_until_aftertrain(self):
        td, program = self.make_game('''@SYSTEM_TITLE
BEGIN TRAIN

@EVENTTRAIN
PRINTL start
RETURN

@SHOW_STATUS
PRINTL status
RETURN

@SHOW_USERCOM
PRINTL menu[999]
RETURN

@USERCOM
PRINTFORML user={RESULT}
IF RESULT == 999
    BEGIN AFTERTRAIN
ENDIF
RETURN

@EVENTEND
PRINTL end
BEGIN ABLUP

@EVENTTURNEND
PRINTL turn
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["999"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "start\nstatus\nmenu[999]\nuser=999\nend\nturn\n")
        self.assertEqual(rt.warnings, [])

    def test_dotrain_runs_eventcom_com_and_eventcomend(self):
        td, program = self.make_game('''@SYSTEM_TITLE
DOTRAIN 7
RETURN

@EVENTCOM
PRINTFORML pre={SELECTCOM}
RETURN

@COM7
PRINTL com
RETURN

@SOURCE_CHECK
PRINTL source
RETURN

@EVENTCOMEND
PRINTL post
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "pre=7\ncom\nsource\npost\n")
        self.assertEqual(rt.warnings, [])

    def test_dotrain_prefers_com_common_and_uses_rewritten_selectcom(self):
        td, program = self.make_game('''@SYSTEM_TITLE
DOTRAIN 7
RETURN

@EVENTCOM
PRINTFORML pre={SELECTCOM}
SELECTCOM = 21
RETURN

@COM_COMMON(ARG)
PRINTFORML common={ARG}:{SELECTCOM}
RETURN

@SOURCE_CHECK
PRINTFORML source={SELECTCOM}
RETURN

@ACT_COM7
PRINTL wrong
RETURN

@EVENTCOMEND
PRINTFORML post={SELECTCOM}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "pre=7\ncommon=21:21\nsource=21\npost=21\n")
        self.assertEqual(rt.warnings, [])

    def test_tooltip_state_and_mouseskip_function(self):
        td, program = self.make_game('''@SYSTEM_TITLE
TOOLTIP_SETDELAY 250
TOOLTIP_SETCOLOR 1, 2, 3
PRINTFORML {MOUSESKIP()}
HTML_PRINT "<button value='go' title='Tip'><img src='Pic' height='8'></button><nonbutton title='Info'>I</nonbutton>"
PRINTBUTTON "[P]", "plain"
RETURN
''')
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "resources").mkdir()
        (root / "resources" / "画像.csv").write_text("Pic,img.png,0,0,16,8\n", encoding="utf-8")
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "0\n<button value='go' title='Tip'><img src='Pic' height='8'></button><nonbutton title='Info'>I</nonbutton>\n[P]")
        self.assertEqual(rt.current_tooltip_delay, 250)
        self.assertEqual(rt.current_tooltip_color, 0x010203)
        page = rt.html_page_model()
        self.assertEqual((page["style_spans"][0]["tooltip_delay"], page["style_spans"][0]["tooltip_color"]), (250, 0x010203))
        self.assertEqual((page["buttons"][0]["title"], page["buttons"][0]["tooltip_delay"], page["buttons"][0]["tooltip_color"]), ("Tip", 250, 0x010203))
        self.assertEqual((page["images"][0]["col"], page["images"][0]["parent_title"], page["images"][0]["tooltip_delay"], page["images"][0]["tooltip_color"]), ("0", "Tip", 250, 0x010203))
        self.assertEqual((page["nonbuttons"][0]["title"], page["nonbuttons"][0]["tooltip_delay"], page["nonbuttons"][0]["tooltip_color"]), ("Info", 250, 0x010203))
        self.assertEqual((page["print_buttons"][0]["value"], page["print_buttons"][0]["tooltip_delay"], page["print_buttons"][0]["tooltip_color"]), ("plain", 250, 0x010203))
        layout = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual((layout["buttons"][0]["tooltip_delay"], layout["buttons"][0]["tooltip_color"]), (250, 0x010203))
        self.assertEqual((layout["images"][0]["x"], layout["images"][0]["parent_title"], layout["images"][0]["tooltip_delay"], layout["images"][0]["tooltip_color"]), (0, "Tip", 250, 0x010203))
        self.assertEqual(layout["nonbuttons"][0]["x"], 16)
        self.assertEqual((layout["print_buttons"][0]["tooltip_delay"], layout["print_buttons"][0]["tooltip_color"]), (250, 0x010203))
        hit = rt.html_hit_test(1, 21, char_width=8, line_height=20)
        self.assertEqual((hit["type"], hit["parent_title"], hit["button_value"], hit["tooltip_delay"], hit["tooltip_color"]), ("image", "Tip", "go", 250, 0x010203))
        self.assertEqual(rt.html_click_value(1, 21, char_width=8, line_height=20), "go")
        self.assertEqual(rt.warnings, [])

    def test_messkip_reads_runtime_skip_state_and_mouse_fallback(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML A={MESSKIP()}:{MOUSESKIP()}
IF MESSKIP()
    PRINTL skip
ELSE
    PRINTL wait
ENDIF
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "A=0:0\nwait\n")
        self.assertEqual(rt.warnings, [])

        rt = EraRuntime(program, echo=False, interactive=False)
        rt.message_skip = True
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "A=1:0\nskip\n")
        self.assertEqual(rt.warnings, [])

        rt = EraRuntime(program, echo=False, interactive=False)
        rt.mouse_skip = True
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "A=1:1\nskip\n")
        self.assertEqual(rt.warnings, [])

    def test_html_getprintedstr_returns_current_display_line(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTS "<b>keep</b>"
PRINTL
RESULTS = %HTML_TOPLAINTEXT(HTML_GETPRINTEDSTR())%
CLEARLINE 1
PRINTS RESULTS
PRINTFORML |empty={LINEISEMPTY()}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "keep|empty=0\n")

    def test_html_related_split_escape_pop_and_indexed_getprintedstr(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS PARTS, 10
#DIM COUNT
PRINTS "draft"
RESULTS = %HTML_POPPRINTINGSTR()%
PRINTFORML pop=%RESULTS%|line={LINECOUNT}
PRINTL first
HTML_PRINT "<b>second</b>"
HTML_TAGSPLIT "<p align='right'>あ<!--comment-->い<font color='red'>う</font></p>", COUNT, PARTS
PRINTFORML count={COUNT}|{PARTS:0}|{PARTS:1}|{PARTS:2}|{PARTS:7}
HTML_TAGSPLIT "<i>x</i>"
PRINTFORML def={RESULT}:{RESULTS:0}:{RESULTS:1}:{RESULTS:2}
PRINTFORML lines=%HTML_TOPLAINTEXT(HTML_GETPRINTEDSTR(2))%/%HTML_TOPLAINTEXT(HTML_GETPRINTEDSTR())%|esc=%HTML_ESCAPE("<&\\"'")%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(
            "".join(rt.output),
            "pop=draft|line=0\n"
            "first\n"
            "<b>second</b>\n"
            "count=8|<p align='right'>|あ|<!--comment-->|</p>\n"
            "def=3:<i>:x:</i>\n"
            "lines=second/def=3::x:|esc=&lt;&amp;&quot;&#x27;\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_html_string_measurement_and_substring_helpers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML len={HTML_STRINGLEN("<b>B</b>")} pix={HTML_STRINGLEN("<b>B</b>", 1)} lines={HTML_STRINGLINES("AB<b>CD</b>",4)}
PRINTSL HTML_SUBSTRING("AB<b>CD</b>EFG",4)
PRINTFORML rest=%RESULTS:1%
HTML_SUBSTRING "AB<b>CD</b>EFG", 4
PRINTFORML cmd=%RESULTS%/%RESULTS:0%/%RESULTS:1%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(
            "".join(rt.output),
            "len=2 pix=10 lines=2\n"
            "AB<b>C</b>\n"
            "rest=<b>D</b>EFG\n"
            "cmd=AB<b>C</b>/AB<b>C</b>/<b>D</b>EFG\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_color_commands_update_getcolor_state(self):
        td, program = self.make_game('''@SYSTEM_TITLE
SETCOLOR 1, 2, 3
GETCOLOR
A = RESULT
SETCOLORBYNAME "RED"
B = GETCOLOR()
SETCOLORBYNAME Gold
F = GETCOLOR()
RESETCOLOR
C = GETCOLOR()
SETBGCOLOR COLOR("AQUA")
GETBGCOLOR
D = RESULT
SETBGCOLORBYNAME Blue
G = GETBGCOLOR()
RESETBGCOLOR
E = GETBGCOLOR()
H = GETDEFBGCOLOR()
PRINTFORML {A}:{B}:{F}:{C}:{D}:{G}:{E}:{H}:{COLOR("RED")}:{COLOR("gray")}:{COLOR("P-GREEN")}:{COLOR("デフォルト")}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(
            "".join(rt.output),
            "66051:16711680:16766720:12632256:6750207:255:0:0:10027008:7829367:7389296:12632256\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_color_helper_matches_eramegaten_dynamic_and_battle_colors(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CFLAG:0:PTフラグ = 1
CFLAG:1:PTフラグ = 0
FLAG:通常カ拉 = 255000128
PRINTFORML {COLOR("BATTLE", 0)}:{COLOR("BATTLE", 0, 1)}:{COLOR("BATTLE", 1)}:{COLOR("通常カ拉")}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "3407820:16711731:16711731:16711808\n")
        self.assertEqual(rt.warnings, [])

    def test_csv_equip_exp_and_raw_chara_search(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 0
ADDCHARA 1
PRINTFORML {FINDCHARA(NO, 1)}:{FINDCHARA(CSTR:101, "TALULAH")}:{FINDLASTCHARA(CSTR:101, "TALULAH")}:{CSVEQUIP(1, 1, 0)}:{CSVEXP(1, 2, 0)}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Chara0.csv").write_text("番号,0\n名前,A\n呼び名,A\nＣ文字列,101,TALULAH\n", encoding="utf-8")
        (root / "CSV" / "Chara1.csv").write_text("番号,1\n名前,B\n呼び名,B\nＣ文字列,101,TALULAH\n装備,1,99\n経験,2,7\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("1:0:1:99:7", "".join(rt.output))

    def test_csv_nickname_mastername_and_character_string_slots(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 0
PRINTFORML %CSVNAME(0)%:%CSVCALLNAME(0)%:%CSVNICKNAME(0)%:%CSVMASTERNAME(0)%:%NAME:0%:%CALLNAME:0%:%NICKNAME:0%:%MASTERNAME:0%
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Chara0.csv").write_text(
            "番号,0\n名前,正式名\n呼び名,呼称\nニックネーム,愛称\n主人名,主呼称\n",
            encoding="utf-8",
        )
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "正式名:呼称:愛称:主呼称:正式名:呼称:愛称:主呼称\n")
        self.assertEqual(rt.warnings, [])

    def test_csvrelation_and_flag_relation_rows(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML {CSVCFLAG(2, GETNUM(CFLAG, "善悪値"), 0)}:{CSVCFLAG(2, GETNUM(CFLAG, "キャラ相性1"), 0)}:{CSVCFLAG(2, GETNUM(CFLAG, "キャラ相性値1"), 0)}:{CSVCFLAG(2, GETNUM(CFLAG, "相性値1"), 0)}:{CSVCFLAG(2, GETNUM(CFLAG, "キャラ相性2"), 0)}:{CSVCFLAG(2, GETNUM(CFLAG, "キャラ相性値2"), 0)}:{CSVRELATION(2, 7, 0)}:{CSVRELATION(2, 9, 0)}
ADDCHARA 2
PRINTFORML {CFLAG:0:善悪値}:{CFLAG:0:キャラ相性1}:{CFLAG:0:キャラ相性値1}:{CFLAG:0:キャラ相性2}:{CFLAG:0:キャラ相性値2}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "CFlag.csv").write_text(
            "\n".join(
                [
                    "10,善悪値",
                    "1600,キャラ相性1",
                    "1601,キャラ相性値1",
                    "1602,キャラ相性2",
                    "1603,キャラ相性値2",
                    "1640,相性1",
                    "1641,相性値1",
                    "1642,相性2",
                    "1643,相性値2",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Chara2.csv").write_text(
            "番号,2\n名前,R\n呼び名,R\nフラグ,善悪値,128\nフラグ,相性1,7\nフラグ,相性値1,120\nフラグ,相性値2,9,50\n",
            encoding="utf-8",
        )
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "128:7:120:120:9:50:120:50\n128:7:120:9:50\n")
        self.assertEqual(rt.warnings, [])

    def test_remaining_emura_command_shims(self):
        td, program = self.make_game('''@SYSTEM_TITLE
A:0 = 5
A:1 = 8
ARRAYCOPY "A", "B"
TRYGOTO OK
PRINTL bad
$OK
ADDVOIDCHARA
CFLAG:RESULT:5 = 42
ADDCOPYCHARA 0
LOCAL = RESULT
STRLENFORMU %CALLNAME:0%
LOCAL:1 = RESULT
SAVEDATA 3
DELDATA 3
CHKDATA 3
PRINTFORML B={B:0},{B:1}|COPY={LOCAL}:{CFLAG:LOCAL:5}|LEN={LOCAL:1}|CHK={RESULT}|CM={CMATCH(CFLAG:5,42)}|RGB={COLOR_FROMRGB(1,2,3)}|C={CONVERT(255,16)}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        self.assertEqual(eval_expr(rt, 'HTML_TOPLAINTEXT("a<br><b>b</b>")'), "a\nb")
        self.assertEqual(eval_expr(rt, "LOG10(999)"), 2)
        rt.run("SYSTEM_TITLE", max_steps=200)
        out = "".join(rt.output)
        self.assertIn("B=5,8|COPY=1:42|LEN=0|CHK=1|CM=2|RGB=66051|C=FF", out)
        self.assertNotIn("bad", out)

    def test_carray_functions_scan_character_axis(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 0
ADDCHARA 1
ADDCHARA 2
CFLAG:0:5 = 42
CFLAG:1:5 = 42
CFLAG:2:5 = 7
BASE:0:LV = 10
BASE:1:LV = 30
BASE:2:LV = 20
NAME:0 = Alice
NAME:1 = Bob
NAME:2 = Alice
PRINTFORML C={CMATCH(CFLAG:5,42)}:{CMATCH(CFLAG:0:5,42)}:{CMATCH(CFLAG:5,42,1,3)}:{CMATCH(NAME,"Alice")}
PRINTFORML S={SUMCARRAY(CFLAG:5)}:{SUMCARRAY(CFLAG:0:5,1,3)}
PRINTFORML M={MAXCARRAY(BASE:0:LV)}:{MINCARRAY(BASE:0:LV)}
PRINTFORML R={INRANGECARRAY(BASE:0:LV,15,31)}:{INRANGECARRAY(CFLAG:5,0,10,1,3)}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Base.csv").write_text("0,HP\n1,MP\n2,LV\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(
            "".join(rt.output),
            "C=2:2:1:2\n"
            "S=91:49\n"
            "M=30:10\n"
            "R=2:1\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_split_sets_result_count_count_var_and_scalar_alias(self):
        td, program = self.make_game('''@SYSTEM_TITLE
SPLIT "a_b_c", "_", LOCALS, LOCAL
PRINTFORML L={RESULT}:{LOCAL}:%LOCALS%:%LOCALS:0%:%LOCALS:2%
RESULTS = keep
SPLIT "x/y", "/", RESULTS, LOADED
PRINTFORML R={RESULT}:{LOADED}:%RESULTS%:%RESULTS:0%:%RESULTS:1%
SPLIT "", ",", STR, COUNT
PRINTFORML E={RESULT}:{COUNT}:[%STR%]:[%STR:0%]
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(
            "".join(rt.output),
            "L=3:3:a:a:c\n"
            "R=2:2:x:x:y\n"
            "E=1:1:[]:[]\n",
        )
        self.assertEqual(rt.warnings, [])

    def test_chkdata_results_for_sidecar_and_native_headers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
SAVEDATA 4, "sidecar caption"
CHKDATA 4
PRINTFORML S={RESULT}:%RESULTS%
CHKDATA 5
PRINTFORML N={RESULT}:%RESULTS%
CHKDATA 6
PRINTFORML T={RESULT}:%RESULTS%
CHKDATA 7
PRINTFORML M={RESULT}:%RESULTS%
LOCAL = CHKDATA(5)
PRINTFORML F={LOCAL}:%RESULTS%
RETURN
''')
        root = Path(td.name)
        caption = "native caption"
        encoded = caption.encode("utf-16-le")
        n = len(encoded)
        varint = []
        while True:
            b = n & 0x7F
            n >>= 7
            if n:
                varint.append(b | 0x80)
            else:
                varint.append(b)
                break
        header = b"\x89ERA\r\n\x1a\n" + b"\x10\x07\x00\x00\x00\x00\x00\x00" + b"\x00" * 16
        (root / "save05.sav").write_bytes(header + b"\x00" + bytes(varint) + encoded + b"\x00\x00\x00\x00")
        (root / "save06.sav").write_text("666\n309140\n2022/12/27 22:34:39 legacy caption\n", encoding="utf-8-sig")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("S=0:sidecar caption", out)
        self.assertIn("N=0:native caption", out)
        self.assertIn("T=0:2022/12/27 22:34:39 legacy caption", out)
        self.assertIn("M=1:", out)
        self.assertIn("F=0:native caption", out)

    def test_loadglobal_and_loaddata_from_native_binary_save(self):
        td, program = self.make_game('''@SYSTEM_TITLE
LOADGLOBAL
LOADDATA 0
PRINTL after-load
RETURN

@EVENTLOAD
PRINTFORML G={GLOBAL:0}/{GLOBAL:2}:%GLOBALS:0%/%GLOBALS:1%
PRINTFORML L={DAY:0}/{DAY:1}|C={CHARANUM}:{NO:0}:%NAME:0%|V={LASTLOAD_VERSION}:{LASTLOAD_NO}
RETURN
''')
        root = Path(td.name)
        (root / "global.sav").write_bytes(
            self.native_header(1, "")
            + self.native_record(0x01, "GLOBAL", self.native_int_array([9, 0, 8]))
            + self.native_record(0x11, "GLOBALS", self.native_str_array(["g0", "g1"]))
            + b"\xFF"
        )
        chara = (
            self.native_record(0x00, "NO", self.native_int(123))
            + self.native_record(0x10, "NAME", self.native_string("Loaded"))
            + b"\xFE"
        )
        (root / "save00.sav").write_bytes(
            self.native_header(0, "native slot")
            + struct.pack("<q", 1)
            + chara
            + self.native_record(0x01, "DAY", self.native_int_array([4, 5]))
            + b"\xFF"
        )
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("G=9/8:g0/g1", out)
        self.assertIn("L=4/5|C=1:123:Loaded|V=309145:0", out)
        self.assertNotIn("after-load", out)

    def test_loadglobal_from_legacy_text_global_save(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM GLOBAL LEGACY_G, 3
#DIMS GLOBAL LEGACY_GS, 2
#DIM GLOBAL LEGACY_G2, 3, 4
#DIM GLOBAL LEGACY_G3, 2, 2, 3
LOADGLOBAL
PRINTFORML G={GLOBAL:2}:%GLOBALS:1%:{LEGACY_G:1}:%LEGACY_GS:1%:{LEGACY_G2:1:2}:{LEGACY_G2:2:3}:{LEGACY_G3:1:0:2}:{LEGACY_G3:1:1:0}
RETURN
''')
        root = Path(td.name)
        (root / "global.sav").write_text(
            "\n".join([
                "0",
                "",
                "44",
                "__FINISHED",
                "",
                "global text",
                "__FINISHED",
                "__EMUERA_1808_STRAT__",
                "LEGACY_GS",
                "",
                "named str",
                "__FINISHED",
                "__EMU_SEPARATOR__",
                "LEGACY_G",
                "",
                "77",
                "__FINISHED",
                "__EMU_SEPARATOR__",
                "LEGACY_G2",
                "0,5",
                "0,0,6",
                "0,0,0,7",
                "__FINISHED",
                "__EMU_SEPARATOR__",
                "LEGACY_G3",
                "0{",
                "1,0,2",
                "",
                "}",
                "1{",
                "0,0,3",
                "4",
                "}",
                "__FINISHED",
                "__EMU_SEPARATOR__",
            ]),
            encoding="utf-8-sig",
        )
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "G=44:global text:77:named str:6:7:3:4\n")
        self.assertEqual(rt.warnings, [])
        legacy = read_legacy_text_global_save(root / "global.sav", program)
        self.assertEqual(legacy.file_type, SaveFileType.GLOBAL)
        self.assertEqual(legacy.numeric["GLOBAL"][(2,)], 44)
        self.assertEqual(legacy.strings["GLOBALS"][(1,)], "global text")
        self.assertEqual(legacy.numeric["LEGACY_G"][(1,)], 77)
        self.assertEqual(legacy.strings["LEGACY_GS"][(1,)], "named str")
        self.assertEqual(legacy.numeric["LEGACY_G2"][(1, 2)], 6)
        self.assertEqual(legacy.numeric["LEGACY_G2"][(2, 3)], 7)
        self.assertEqual(legacy.numeric["LEGACY_G3"][(1, 0, 2)], 3)
        self.assertEqual(legacy.numeric["LEGACY_G3"][(1, 1, 0)], 4)

    def test_loaddata_from_legacy_text_save_migrates_roster_without_warning(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM SAVEDATA LEGACY_FLAG, 2, 3
#DIMS SAVEDATA LEGACY_STR, 4
#DIM SAVEDATA LEGACY_SCALAR
LOADDATA 2
PRINTL after-load
RETURN

@EVENTLOAD
PRINTFORML C={CHARANUM}:{NO:0}:{ISASSI:0}:%NAME:0%/%CALLNAME:0%:{BASE:0:0}:{MAXBASE:0:0}:{ABL:0:1}
PRINTFORML Y={CFLAG:0:2}:{JUEL:0:1}:{EQUIP:0:1}:{TEQUIP:0:0}
PRINTFORML G={DAY:1}:{MONEY:2}:{ITEM:3}:{FLAG:4}:{PALAMLV:3}:{EXPLV:4}:{TARGET:0}:%SAVESTR:1%
PRINTFORML X=%CSTR:0:2%:{LEGACY_FLAG:0:1}:{LEGACY_FLAG:1:1}:%LEGACY_STR:3%:{LEGACY_SCALAR}
PRINTFORML V={LASTLOAD_VERSION}:{LASTLOAD_NO}:%LASTLOAD_TEXT%
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Chara42.csv").write_text("番号,42\n名前,Template\n呼び名,T\n", encoding="utf-8")
        legacy_lines = [
                "666",
                "309140",
                "legacy caption",
                "1",
                "Hero",
                "HeroCall",
                "1",
                "42",
                "10",
                "0",
                "__FINISHED",
                "20",
                "__FINISHED",
                "0",
                "30",
                "__FINISHED",
                "__FINISHED",
                "__FINISHED",
                "__FINISHED",
                "__FINISHED",
                "0",
                "11",
                "__FINISHED",
                "__FINISHED",
                "0",
                "0",
                "88",
                "__FINISHED",
                "0",
                "4",
                "__FINISHED",
                "9",
                "__FINISHED",
                "0",
                "66",
                "__FINISHED",
                "7",
                "__FINISHED",
                "0",
                "12",
                "__FINISHED",
                "0",
                "13",
                "__FINISHED",
                "0",
                "14",
                "__FINISHED",
            ]
        global_blocks = [[] for _ in range(60)]
        global_blocks[0] = ["0", "12"]  # DAY
        global_blocks[1] = ["0", "0", "345"]  # MONEY
        global_blocks[2] = ["0", "0", "0", "7"]  # ITEM
        global_blocks[3] = ["0", "0", "0", "0", "9"]  # FLAG
        global_blocks[6] = ["0", "100", "500", "3333"]  # PALAMLV
        global_blocks[7] = ["0", "1", "4", "20", "444"]  # EXPLV
        global_blocks[12] = ["8"]  # TARGET
        for block in global_blocks:
            legacy_lines.extend(block)
            legacy_lines.append("__FINISHED")
        legacy_lines.extend([
                "",
                "savetail",
                "__FINISHED",
                "__EMUERA_1808_STRAT__",
                "__EMU_SEPARATOR__",
                "CSTR",
                "",
                "",
                "bio",
                "__FINISHED",
                "LEGACY_FLAG",
                "",
                "5",
                "",
                "",
                "9",
                "__FINISHED",
                "LEGACY_STR",
                "",
                "",
                "",
                "tail",
                "__FINISHED",
                "LEGACY_SCALAR",
                "77",
                "__FINISHED",
            ])
        (root / "save02.sav").write_text(
            "\n".join(legacy_lines),
            encoding="utf-8-sig",
        )
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("C=1:42:1:Hero/HeroCall:10:20:30", out)
        self.assertIn("Y=88:4:66:7", out)
        self.assertIn("G=12:345:7:9:3333:444:8:savetail", out)
        self.assertIn("X=bio:5:9:tail:77", out)
        self.assertIn("V=309140:2:legacy caption", out)
        self.assertNotIn("after-load", out)
        self.assertEqual(rt.warnings, [])
        legacy = read_legacy_text_save(root / "save02.sav", program)
        ch = legacy.characters[0]
        self.assertEqual(ch.numeric["SOURCE"][(1,)], 11)
        self.assertEqual(ch.numeric["CFLAG"][(2,)], 88)
        self.assertEqual(ch.numeric["JUEL"][(1,)], 4)
        self.assertEqual(ch.numeric["RELATION"][(0,)], 9)
        self.assertEqual(ch.numeric["EQUIP"][(1,)], 66)
        self.assertEqual(ch.numeric["TEQUIP"][(0,)], 7)
        self.assertEqual(ch.numeric["STAIN"][(1,)], 12)
        self.assertEqual(ch.numeric["GOTJUEL"][(1,)], 13)
        self.assertEqual(ch.numeric["NOWEX"][(1,)], 14)
        self.assertEqual(legacy.numeric["DAY"][(1,)], 12)
        self.assertEqual(legacy.numeric["MONEY"][(2,)], 345)
        self.assertEqual(legacy.numeric["ITEM"][(3,)], 7)
        self.assertEqual(legacy.numeric["FLAG"][(4,)], 9)
        self.assertEqual(legacy.numeric["PALAMLV"][(3,)], 3333)
        self.assertEqual(legacy.numeric["EXPLV"][(4,)], 444)
        self.assertEqual(legacy.numeric["TARGET"][(0,)], 8)
        self.assertEqual(legacy.strings["SAVESTR"][(1,)], "savetail")

    def test_write_native_binary_save_roundtrips_sparse_arrays_and_characters(self):
        td, program = self.make_game('''@SYSTEM_TITLE
RETURN
''')
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        ch = CharacterState(template_no=123)
        ch.numeric = {
            "NO": {(): 123, (0,): 123},
            "CFLAG": {(2,): -7, (5,): 99999},
        }
        ch.strings = {"NAME": {(): "Writer", (0,): "Writer"}, "CSTR": {(3,): "tag"}}
        save = NativeSave(
            file_type=SaveFileType.NORMAL,
            script_code=666,
            script_version=309145,
            save_text="writer slot",
            numeric={
                "FLAG": {(): 11, (0,): 11, (5,): 22},
                "DA": {(2, 3): 99},
                "TA": {(1, 2, 3): -123456},
            },
            strings={"STR": {(): "s0", (0,): "s0", (4,): "s4"}},
            characters=[ch],
        )
        path = root / "save00.sav"
        write_native_save(path, save, program)
        loaded = read_native_save(path, program)
        self.assertEqual(loaded.file_type, SaveFileType.NORMAL)
        self.assertEqual(loaded.script_code, 666)
        self.assertEqual(loaded.script_version, 309145)
        self.assertEqual(loaded.save_text, "writer slot")
        self.assertEqual(loaded.numeric["FLAG"][(0,)], 11)
        self.assertEqual(loaded.numeric["FLAG"][(5,)], 22)
        self.assertEqual(loaded.numeric["DA"][(2, 3)], 99)
        self.assertEqual(loaded.numeric["TA"][(1, 2, 3)], -123456)
        self.assertEqual(loaded.strings["STR"][(0,)], "s0")
        self.assertEqual(loaded.strings["STR"][(4,)], "s4")
        self.assertEqual(len(loaded.characters), 1)
        self.assertEqual(loaded.characters[0].template_no, 123)
        self.assertEqual(loaded.characters[0].numeric["CFLAG"][(2,)], -7)
        self.assertEqual(loaded.characters[0].strings["NAME"][()], "Writer")
        self.assertEqual(loaded.characters[0].strings["CSTR"][(3,)], "tag")

    def test_native_save_from_memory_writes_global_only_snapshot(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM GLOBAL GFLAG, 4
#DIMS GLOBAL GSTR, 3
GLOBAL:1 = 7
GLOBALS:2 = "persist"
GFLAG:3 = 33
GSTR:1 = "g"
FLAG:9 = 99
RETURN
''')
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        save = native_save_from_memory(
            rt.memory,
            file_type=SaveFileType.GLOBAL,
            save_text="global writer",
            script_code=666,
            script_version=309145,
        )
        path = root / "global.sav"
        write_native_save(path, save, program)
        loaded = read_native_save(path, program)
        self.assertEqual(loaded.file_type, SaveFileType.GLOBAL)
        self.assertEqual(loaded.save_text, "global writer")
        self.assertEqual(loaded.numeric["GLOBAL"][(1,)], 7)
        self.assertEqual(loaded.numeric["GFLAG"][(3,)], 33)
        self.assertEqual(loaded.strings["GLOBALS"][(2,)], "persist")
        self.assertEqual(loaded.strings["GSTR"][(1,)], "g")
        self.assertNotIn("FLAG", loaded.numeric)

    def test_cli_can_export_native_save_and_global_without_touching_root_saves(self):
        td, _ = self.make_game('''@SYSTEM_TITLE
#DIM GLOBAL GFLAG, 4
ADDCHARA 0
FLAG:5 = 55
GLOBAL:1 = 7
GFLAG:2 = 8
SAVESTR:0 = "/"
RETURN
''')
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "CSV" / "GameBase.csv").write_text(
            "称号,Test\nコード,777\nバージョン,123456\n", encoding="utf-8"
        )
        (root / "CSV" / "Chara0.csv").write_text("番号,0\n名前,Hero\n呼び名,Hero\n", encoding="utf-8")
        save_path = root / "exports" / "slot.sav"
        global_path = root / "exports" / "global.sav"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli_main([
                "run",
                str(root),
                "--entry",
                "SYSTEM_TITLE",
                "--max-steps",
                "100",
                "--non-interactive",
                "--quiet",
                "--export-native-save",
                str(save_path),
                "--export-native-global",
                str(global_path),
                "--native-save-text",
                "cli slot",
            ])
        self.assertEqual(rc, 0)
        program = load_program(root)
        normal = read_native_save(save_path, program)
        glob = read_native_save(global_path, program)
        self.assertEqual(normal.file_type, SaveFileType.NORMAL)
        self.assertEqual(normal.script_code, 777)
        self.assertEqual(normal.script_version, 123456)
        self.assertEqual(normal.save_text, "cli slot")
        self.assertEqual(normal.numeric["FLAG"][(5,)], 55)
        self.assertEqual(normal.strings["SAVESTR"][(0,)], "/")
        self.assertEqual(normal.characters[0].strings["NAME"][()], "Hero")
        self.assertEqual(glob.file_type, SaveFileType.GLOBAL)
        self.assertEqual(glob.numeric["GLOBAL"][(1,)], 7)
        self.assertEqual(glob.numeric["GFLAG"][(2,)], 8)
        self.assertNotIn("FLAG", glob.numeric)
        self.assertFalse((root / "save00.sav").exists())
        self.assertFalse((root / "global.sav").exists())

    def test_sortchara_orders_characters_and_remaps_targets(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 0
ADDCHARA 1
ADDCHARA 2
BASE:0:LV = 5
BASE:1:LV = 1
BASE:2:LV = 3
TARGET = 2
SORTCHARA BASE:LV
PRINTFORML A={NO:0},{NO:1},{NO:2}|T={TARGET}
SORTCHARA NO:U,BACK
PRINTFORML B={NO:0},{NO:1},{NO:2}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Base.csv").write_text("30,LV\n", encoding="utf-8")
        for i in range(3):
            (root / "CSV" / f"Chara{i}.csv").write_text(f"番号,{i}\n名前,C{i}\n呼び名,C{i}\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("A=1,2,0|T=1", out)
        self.assertIn("B=2,1,0", out)

    def test_begin_runs_duplicate_event_functions_by_priority(self):
        td, program = self.make_game('''@SYSTEM_TITLE
BEGIN SHOP

@EVENTSHOP
#PRI 0
PRINTL low
RETURN

@EVENTSHOP
#PRI 1
PRINTL high
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "high\nlow\n")

    def test_later_duplicate_event_functions_run_after_same_priority(self):
        td, program = self.make_game('''@SYSTEM_TITLE
BEGIN SHOP

@EVENTSHOP
PRINTL first
RETURN

@EVENTSHOP
#LATER
PRINTL later
RETURN

@EVENTSHOP
PRINTL second
RETURN
''')
        self.addCleanup(td.cleanup)
        funcs = program.get_functions("EVENTSHOP")
        self.assertEqual([fn.later for fn in funcs], [False, True, False])
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "first\nsecond\nlater\n")

    def test_begin_shop_loop_calls_show_usershop_event_and_waits(self):
        td, program = self.make_game('''@SYSTEM_TITLE
BEGIN SHOP
PRINTL bad

@SHOW_SHOP
PRINTL menu
RETURN

@USERSHOP
PRINTFORML user={RESULT}
FLAG:商店指令 = RESULT
RETURN

@EVENTSHOP
PRINTFORML event={FLAG:商店指令}
FLAG:商店指令 = 0
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["7"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "menu\nuser=7\nevent=7\nmenu\n")

    def test_noninteractive_input_uses_first_printbutton(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTBUTTON "[7] choose", 7
INPUT
PRINTFORML picked={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertIn("picked=7", "".join(rt.output))

    def test_noninteractive_input_uses_printed_numeric_choice(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL [3] visible choice
INPUT
PRINTFORML picked={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertIn("picked=3", "".join(rt.output))

    def test_noninteractive_stops_when_explicit_inputs_are_exhausted(self):
        td, program = self.make_game('''@SYSTEM_TITLE
INPUT
PRINTL [3] visible choice
INPUT
PRINTL bad
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["1"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "[3] visible choice\n")

    def test_noninteractive_stops_when_explicit_inputs_exhaust_before_native_inputint(self):
        td, program = self.make_game('''@SYSTEM_TITLE
INPUT
PRINT [0] rest
PRINTL [1] sleep
CALL INPUTINT(0, 1)
PRINTFORML picked={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["7"])
        steps = rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertLess(steps, 50)
        self.assertEqual("".join(rt.output), "[0] rest[1] sleep\n")
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.queue_input("1"), "1")
        rt.continue_run(max_steps=50)
        self.assertIn("picked=1\n", "".join(rt.output))
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_inputint_does_not_fallback_after_repeated_invalid_inputs(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL INPUTINT(3, 4, 5)
PRINTFORML picked={RESULT}
CALL INPUTINT(1, 2, 3)
PRINTFORML one={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["9"] * 8)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "")
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)

        self.assertEqual(rt.queue_input("4"), "4")
        rt.continue_run(max_steps=50)
        self.assertIn("picked=4\n", "".join(rt.output))
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)

        self.assertEqual(rt.queue_input("23"), "23")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertIn("one=2\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_preserves_stack_when_explicit_inputs_exhaust_before_native_input_yn(self):
        td, program = self.make_game('''@SYSTEM_TITLE
INPUT
CALL INPUT_YN, "Yes", "No"
PRINTFORML yn={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["9"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "[0] Yes\n[1] No\n")
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.queue_input("1"), "1")
        rt.continue_run(max_steps=50)
        self.assertIn("yn=1\n", "".join(rt.output))
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_input_yn_window_helpers_render_and_resume(self):
        td, program = self.make_game('''@SYSTEM_TITLE
INPUT
CALL INPUT_YN_M, "Yes", "No", "/"
PRINTFORML ym={RESULT}
CALL INPUT_YN_D, "はい", "いいえ", "/"
PRINTFORML yd={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["seed"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("[0] Yes/[1] No\n"), 1)
        self.assertNotIn("ym=", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("1"), "1")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertIn("ym=1\n", out)
        self.assertEqual(out.count("[0] はい/[1] いいえ\n"), 1)
        self.assertNotIn("yd=", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("0"), "0")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertIn("yd=0\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_input_yn_does_not_fallback_after_repeated_invalid_inputs(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL INPUT_YN, "Yes", "No"
PRINTFORML yn={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["9"] * 8)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[0] Yes\n[1] No\n"), 1)
        self.assertNotIn("yn=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)

        self.assertEqual(rt.queue_input("1"), "1")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("[0] Yes\n[1] No\n"), 1)
        self.assertIn("yn=1\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_input_select_menu_helpers_render_and_resume(self):
        td, program = self.make_game('''@SYSTEM_TITLE
INPUT
CALL INPUT_SELECT_M, "[1] One/[22] Two", "/", "ログを残す/ボタンを利用する", 2, 1, "LEFT", 20
PRINTFORML sm={RESULT}
CALL INPUT_SELECT_D, "[7] Seven/[8] Eight"
PRINTFORML sd={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["seed"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("[1] One　[22] Two\n"), 1)
        self.assertNotIn("sm=", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("22"), "22")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertIn("sm=22\n", out)
        self.assertEqual(out.count("[7] Seven\n[8] Eight\n\n\n"), 1)
        self.assertNotIn("sd=", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("8"), "8")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertIn("sd=8\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_input_select_menu_preserves_mismatched_explicit_input(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL INPUT_SELECT_M, "[0] Zero/[1] One", "/", "ログを残す/ボタンを利用する", 2, 1, "LEFT", 20
PRINTFORML sm={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["9"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.inputs, ["9"])
        self.assertEqual("".join(rt.output).count("[0] Zero　[1] One\n"), 1)
        self.assertNotIn("sm=", "".join(rt.output))
        rt.inputs.clear()
        self.assertEqual(rt.queue_input("1"), "1")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("[0] Zero　[1] One\n"), 1)
        self.assertIn("sm=1\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_input_select_menu_does_not_fallback_after_repeated_controls(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text("1,オート送り\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
FLAG:オート送り = 0
CALL INPUT_SELECT_M, "[1] One/[2] Two", "/", "ログを残す/ボタンを利用する", 2, 1, "LEFT", 20
PRINTFORML sm={FLAG:オート送り}:{RESULT}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False, inputs=["-"] * 8)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[1] One　[2] Two\n"), 1)
        self.assertNotIn("sm=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)

        self.assertEqual(rt.queue_input("2"), "2")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("[1] One　[2] Two\n"), 1)
        self.assertIn("sm=0:2\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_window_controls_toggle_auto_and_skip_flags(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text(
            "1,オート送り\n2,ウィンドウメッセージスキップ\n",
            encoding="utf-8",
        )
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
FLAG:オート送り = 0
FLAG:ウィンドウメッセージスキップ = 0
CALL INPUT_YN_M, "Yes", "No", "/"
PRINTFORML y={FLAG:オート送り}:{FLAG:ウィンドウメッセージスキップ}:{RESULT}
FLAG:オート送り = 0
FLAG:ウィンドウメッセージスキップ = 0
CALL INPUT_SELECT_M, "[1] One", "/", "ログを残す/ボタンを利用する", 1, 1, "LEFT", 20
PRINTFORML s={FLAG:オート送り}:{FLAG:ウィンドウメッセージスキップ}:{RESULT}
RETURN
''', encoding="utf-8")
        program = load_program(root)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["-", "*", "1", "-", "*", "1"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("y=1:1:1\n", out)
        self.assertIn("s=1:0:1\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_window_log_and_config_controls_resume(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL MESSAGE_WINDOW_LOG, "", "Log body", "/", 1, 72
CALL INPUT_YN_M, "Yes", "No", "/"
PRINTFORML y={RESULT}
CALL INPUT_SELECT_M, "[1] One", "/", "ログを残す/ボタンを利用する", 1, 1, "LEFT", 20
PRINTFORML s={GLOBAL:メッセージ速度}:{RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["+", "close", "1", "/", "0"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertNotIn("Log body", out)
        self.assertIn("y=1\n", out)
        self.assertNotIn("s=", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("5"), "5")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertNotIn("s=", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("9"), "9")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertNotIn("s=", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("1"), "1")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[0] Yes/[1] No\n"), 1)
        self.assertEqual(out.count("[1] One\n"), 1)
        self.assertNotIn("Log body", out)
        self.assertIn("s=5:1\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_window_log_control_includes_current_menu(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL INPUT_YN_M, "Yes", "No", "/"
PRINTFORML y={RESULT}
CALL INPUT_SELECT_M, "[1] One", "/", "ログを残す/ボタンを利用する", 1, 1, "LEFT", 20
PRINTFORML s={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["+"])
        rt.run("SYSTEM_TITLE", max_steps=150)
        out = "".join(rt.output)
        self.assertEqual(out.count("[0] Yes/[1] No\n"), 1)
        self.assertIn("│[0] Yes", out)
        self.assertIn("│[1] No", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("close-yn"), "close-yn")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[0] Yes/[1] No\n"), 1)
        self.assertNotIn("│[0] Yes", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("1"), "1")
        rt.continue_run(max_steps=100)
        self.assertEqual(rt.queue_input("+"), "+")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[1] One\n"), 1)
        self.assertIn("│[1] One", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("close-select"), "close-select")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[1] One\n"), 1)
        self.assertNotIn("│[1] One", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("1"), "1")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertIn("y=1\n", out)
        self.assertIn("s=1\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_preserves_stack_when_explicit_inputs_exhaust_before_native_input_many(self):
        td, program = self.make_game('''@SYSTEM_TITLE
INPUT
CALL INPUT_MANY(1, 10)
PRINTFORML many={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["9"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        out = "".join(rt.output)
        self.assertIn("【0】　《【1】 - 【10】》\n", out)
        self.assertIn("[7]　[8]　[9]　[ AC]\n", out)
        self.assertIn("[0]　[-]　[ENTER]\n", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.queue_input("7"), "7")
        rt.continue_run(max_steps=50)
        self.assertEqual("".join(rt.output).count("【0】　《【1】 - 【10】》"), 1)
        self.assertIn("many=7\n", "".join(rt.output))
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_input_many_renders_calculator_button_metadata(self):
        td, program = self.make_game('''@SYSTEM_TITLE
INPUT
CALL INPUT_MANY(1, 10)
PRINTFORML many={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["seed"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        out = "".join(rt.output)
        self.assertIn("[7]　[8]　[9]　[ AC]\n", out)
        self.assertIn("[4]　[5]　[6]　[Max]\n", out)
        self.assertIn("[1]　[2]　[3]　[Min]\n", out)
        self.assertIn("[0]　[-]　[ENTER]\n", out)
        page = rt.html_page_model()
        values = [button["value"] for button in page["print_buttons"]]
        self.assertEqual(values, ["７", "８", "９", "AC", "４", "５", "６", "MAX", "１", "２", "３", "MIN", "０", "-", "ENTER"])
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_input_many_renders_money_exchange_shortcut(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text("2,商店指令\n", encoding="utf-8")
        (root / "CSV" / "_Rename.csv").write_text("114,ショップ:魔貨交換\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
FLAG:商店指令 = [[ショップ:魔貨交換]]
INPUT
CALL INPUT_MANY(0, 30000)
PRINTFORML many={RESULT}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False, inputs=["seed"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        out = "".join(rt.output)
        self.assertIn("【0】　《【0】 - 【30000】》　【￥0】\n", out)
        self.assertIn("[￥1,000,000]\n", out)
        page = rt.html_page_model()
        self.assertTrue(
            any(
                button["value"] == "20000" and button["label"] == "[￥1,000,000]" and button["col"] == 0
                for button in page["print_buttons"]
            )
        )
        self.assertNotIn("many=", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("20000"), "20000")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertIn("many=20000\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_input_many_clears_full_menu_when_log_disabled(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL before
CALL INPUT_MANY, 1, 9, "ログを残さない"
PRINTFORML many={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["7"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out, "before\nmany=7\n")
        self.assertFalse(rt.html_page_model()["print_buttons"])
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_input_many_calculator_buttons_resume(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL INPUT_MANY(1, 99)
PRINTFORML many={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["１"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        out = "".join(rt.output)
        self.assertIn("【0】　《【1】 - 【99】》\n", out)
        self.assertIn("【1】　《【1】 - 【99】》\n", out)
        self.assertNotIn("many=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)

        self.assertEqual(rt.queue_input("２"), "２")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertIn("【12】　《【1】 - 【99】》\n", out)
        self.assertNotIn("many=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)

        self.assertEqual(rt.queue_input("ENTER"), "ENTER")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertIn("many=12\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_input_many_rejects_signed_direct_input_but_accepts_sign_button(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL INPUT_MANY(-9, 9)
PRINTFORML many={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["-5"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("【0】　《【-9】 - 【9】》\n"), 2)
        self.assertNotIn("many=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)

        self.assertEqual(rt.queue_input("-"), "-")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("【0】　《【-9】 - 【9】》\n"), 3)
        self.assertNotIn("many=", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("５"), "５")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertIn("【-5】　《【-9】 - 【9】》\n", out)
        self.assertNotIn("many=", out)
        self.assertTrue(rt.waiting_for_input)

        self.assertEqual(rt.queue_input("ENTER"), "ENTER")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertIn("many=-5\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_preserves_stack_when_explicit_inputs_exhaust_before_native_input_select(self):
        td, program = self.make_game('''@SYSTEM_TITLE
INPUT
CALL INPUT_SELECT, 1, "One", 2, "Two", 3, "Three"
PRINTFORML selected={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["9"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        out = "".join(rt.output)
        self.assertIn("[1] One", out)
        self.assertIn("[2] Two", out)
        self.assertIn("[3] Three", out)
        self.assertNotIn("selected=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.queue_input("2"), "2")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("[1] One"), 1)
        self.assertIn("selected=2\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_input_select_does_not_fallback_after_repeated_invalid_inputs(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL INPUT_SELECT, 1, "One", 2, "Two", 3, "Three"
PRINTFORML selected={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["9"] * 8)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("[1] One", out)
        self.assertNotIn("selected=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)

        self.assertEqual(rt.queue_input("2"), "2")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("[1] One"), 1)
        self.assertIn("selected=2\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_preserves_stack_when_explicit_inputs_exhaust_before_native_input_split(self):
        td, program = self.make_game('''@SYSTEM_TITLE
INPUT
CALL INPUT_SPLIT, "Pick", "Alpha/Beta/Gamma", "/", "Cancel", 2, 0, 10, 1001, 0, 1003
PRINTFORML split={RESULT}:{RESULT:1}:%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["99"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        out = "".join(rt.output)
        self.assertIn("Pick\n", out)
        self.assertIn("[10]Alpha", out)
        self.assertIn("[11]Beta", out)
        self.assertIn("[0]Cancel", out)
        self.assertNotIn("split=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.queue_input("11"), "11")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("Pick\n"), 1)
        self.assertIn("split=11:0:Beta\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_input_split_does_not_fallback_after_repeated_invalid_inputs(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL INPUT_SPLIT, "Pick", "Alpha/Beta/Gamma", "/", "Cancel", 2, 0, 10, 1001, 0, 1003
PRINTFORML split={RESULT}:{RESULT:1}:%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["999"] * 8)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("Pick\n", out)
        self.assertIn("[10]Alpha", out)
        self.assertNotIn("split=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)

        self.assertEqual(rt.queue_input("11"), "11")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("Pick\n"), 1)
        self.assertIn("split=11:0:Beta\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_preserves_stack_when_explicit_inputs_exhaust_before_native_input_char(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL INPUT_CHAR, "abc", 0
PRINTFORML char=%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["z"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "")
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.queue_input("b"), "b")
        rt.continue_run(max_steps=50)
        self.assertIn("char=b\n", "".join(rt.output))
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_input_char_does_not_fallback_after_repeated_invalid_inputs(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL INPUT_CHAR, "abc", 0
PRINTFORML char=%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["z"] * 8)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "")
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)

        self.assertEqual(rt.queue_input("b"), "b")
        rt.continue_run(max_steps=50)
        self.assertIn("char=b\n", "".join(rt.output))
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_preserves_stack_when_explicit_inputs_exhaust_before_native_input_onekey_tap(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL INPUT_ONEKEY_TAP, 0, "-", "_", "x_[X]_extra"
PRINTFORML tap=%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["q"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        out = "".join(rt.output)
        self.assertIn("[8]", out)
        self.assertIn("[[X]]extra", out)
        self.assertNotIn("tap=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.queue_input("x"), "x")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("[[X]]extra"), 1)
        self.assertIn("tap=x\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_native_input_onekey_tap_does_not_fallback_after_repeated_invalid_inputs(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL INPUT_ONEKEY_TAP, 0, "-", "_", "x_[X]_extra"
PRINTFORML tap=%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["q"] * 8)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("[8]", out)
        self.assertIn("[[X]]extra", out)
        self.assertNotIn("tap=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)

        self.assertEqual(rt.queue_input("x"), "x")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("[[X]]extra"), 1)
        self.assertIn("tap=x\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_preserves_stack_when_explicit_inputs_exhaust_inside_message_window_config(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL MESSAGE_WINDOW_CONFIG
PRINTFORML speed={GLOBAL:メッセージ速度}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["0"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        out = "".join(rt.output)
        self.assertIn("[0] メッセージ速度\n", out)
        self.assertNotIn("speed=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.queue_input("4"), "4")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("[0] メッセージ速度"), 1)
        self.assertNotIn("タイプ方式のメッセージ速度を設定します", out)
        self.assertNotIn("speed=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.queue_input("9"), "9")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertIn("speed=4\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_message_window_config_repeats_until_exit(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL MESSAGE_WINDOW_CONFIG
PRINTFORML speed={GLOBAL:メッセージ速度}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["0", "4", "0", "6", "9"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertNotIn("[0] メッセージ速度", out)
        self.assertIn("speed=6\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_message_window_config_clears_menu_on_exit_preserving_prior_lines(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL before
CALL MESSAGE_WINDOW_CONFIG
PRINTL after
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["9"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out, "before\nafter\n")
        self.assertFalse(rt.html_page_model()["print_buttons"])
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_message_window_config_numeric_subchoice_uses_input_many_state(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL MESSAGE_WINDOW_CONFIG
PRINTFORML speed={GLOBAL:メッセージ速度}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["0", "１"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[0] メッセージ速度"), 1)
        self.assertEqual(out.count("タイプ方式のメッセージ速度を設定します"), 1)
        self.assertEqual(out.count("【0】　《【0】 - 【9】》"), 1)
        self.assertEqual(out.count("【1】　《【0】 - 【9】》"), 1)
        self.assertNotIn("speed=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)

        self.assertEqual(rt.queue_input("ENTER"), "ENTER")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[0] メッセージ速度"), 1)
        self.assertNotIn("タイプ方式のメッセージ速度を設定します", out)
        self.assertNotIn("【0】　《【0】 - 【9】》", out)
        self.assertEqual(rt.memory.get_var("GLOBAL", ["メッセージ速度"]), 1)
        self.assertNotIn("speed=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)

        self.assertEqual(rt.queue_input("9"), "9")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertIn("speed=1\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_message_window_config_yn_subchoice_pauses_without_defaulting(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL MESSAGE_WINDOW_CONFIG
PRINTFORML anim={GLOBAL:メッセージアニメ利用}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["3", "x"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[3] メッセージアニメ利用"), 1)
        self.assertEqual(out.count("メッセージのアニメーションを行うかどうか設定します"), 1)
        self.assertEqual(out.count("[0] 利用しない\n[1] 利用する\n"), 1)
        self.assertNotIn("anim=", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)

        self.assertEqual(rt.queue_input("1"), "1")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out.count("[3] メッセージアニメ利用"), 1)
        self.assertNotIn("メッセージのアニメーションを行うかどうか設定します", out)
        self.assertNotIn("[0] 利用しない\n[1] 利用する\n", out)
        self.assertNotIn("anim=", out)
        self.assertEqual(rt.memory.get_var("GLOBAL", ["メッセージアニメ利用"]), 1)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)

        self.assertEqual(rt.queue_input("9"), "9")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertIn("anim=1\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_message_window_log_viewer_preserves_stack_when_explicit_inputs_exhaust(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL MESSAGE_WINDOW_LOG, "", "Log body", "/", 1, 72
INPUT
CALL MESSAGE_WINDOW_LOG, "", "", "", 0, 0, 1
PRINTL done
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["seed"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("Log body", out)
        self.assertNotIn("done\n", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.queue_input("close"), "close")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertNotIn("Log body", out)
        self.assertIn("done\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_stops_when_next_input_misses_visible_menu(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL [0] yes
PRINTL [1] no
INPUT
$LOOP
IF RESULT == 0
PRINTL yes
ELSEIF RESULT == 1
PRINTL no
ELSE
GOTO LOOP
ENDIF
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["5"])
        steps = rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertLess(steps, 50)
        self.assertEqual(rt.inputs, ["5"])
        self.assertEqual("".join(rt.output), "[0] yes\n[1] no\n")
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_string_input_stops_when_next_input_misses_visible_numeric_menu(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL [0] zero
PRINTL [1] one
ONEINPUTS
PRINTFORML picked=%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["9"])
        steps = rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertLess(steps, 50)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.inputs, ["9"])
        self.assertEqual("".join(rt.output), "[0] zero\n[1] one\n")
        self.assertEqual(rt.warnings, [])
        rt.inputs.clear()
        self.assertEqual(rt.queue_input("1"), "1")
        rt.continue_run(max_steps=50)
        self.assertIn("picked=1\n", "".join(rt.output))
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_waitanykey_and_forcewait_preserve_stack_when_explicit_inputs_exhaust(self):
        td, program = self.make_game('''@SYSTEM_TITLE
INPUT
PRINTL before
WAITANYKEY
PRINTL mid
FORCEWAIT
PRINTL after
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["seed"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "before\n")
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.queue_input("advance"), "advance")
        rt.continue_run(max_steps=50)
        self.assertEqual("".join(rt.output), "before\nmid\n")
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.queue_input("finish"), "finish")
        rt.continue_run(max_steps=50)
        self.assertEqual("".join(rt.output), "before\nmid\nafter\n")
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_printw_preserves_stack_without_duplicate_output(self):
        td, program = self.make_game('''@SYSTEM_TITLE
INPUT
PRINTFORMW before{1}
PRINTL after
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["seed"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual("".join(rt.output), "before1\n")
        self.assertEqual(rt.queue_input("advance"), "advance")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("before1\n"), 1)
        self.assertIn("after\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_printdataw_preserves_stack_without_duplicate_output(self):
        td, program = self.make_game('''#DIM PICK
@SYSTEM_TITLE
INPUT
PRINTDATAW PICK
  DATA only
ENDDATA
PRINTFORML pick={PICK}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["seed"])
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual("".join(rt.output), "only\n")
        self.assertEqual(rt.queue_input("advance"), "advance")
        rt.continue_run(max_steps=50)
        out = "".join(rt.output)
        self.assertEqual(out.count("only\n"), 1)
        self.assertIn("pick=0\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_bare_input_stops_at_prompt(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL prompt
INPUT
PRINTL bad
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        steps = rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertLess(steps, 50)
        self.assertEqual("".join(rt.output), "prompt\n")
        self.assertEqual(rt.warnings, [])

    def test_linecount_and_clearline(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL a
PRINTL b
LOCAL = LINECOUNT
CLEARLINE 1
PRINTFORML count={LOCAL}->{LINECOUNT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "a\ncount=2->1\n")

    def test_clearline_discards_stale_noninteractive_buttons(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTBUTTON "[9] stale", 9
PRINTL
CLEARLINE 1
PRINTL [3] visible choice
INPUT
PRINTFORML picked={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "[3] visible choice\npicked=3\n")

    def test_skipdisp_suppresses_output_and_hidden_buttons_only(self):
        td, program = self.make_game('''@SYSTEM_TITLE
SKIPDISP 1
PRINTBUTTON "[9] hidden", 9
PRINTL hidden text
LOCAL = 7
SKIPDISP 0
PRINTL [3] visible choice
INPUT
PRINTFORML picked={RESULT},local={LOCAL}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "[3] visible choice\npicked=3,local=7\n")
        self.assertEqual(rt.warnings, [])

    def test_skipdisp_preserves_buttons_that_were_already_visible(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTBUTTON "[4] visible", 4
PRINTL
SKIPDISP 1
PRINTBUTTON "[9] hidden", 9
SKIPDISP 0
INPUT
PRINTFORML picked={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "[4] visible\npicked=4\n")
        self.assertEqual(rt.warnings, [])

    def test_skipdisp_suppresses_printbuttonc_variants_from_input_candidates(self):
        td, program = self.make_game('''@SYSTEM_TITLE
SKIPDISP 1
PRINTBUTTONC "hidden-c", "hidden-c"
PRINTBUTTONLC "hidden-lc", "hidden-lc"
SKIPDISP 0
PRINTBUTTON "[V] visible", "visible"
PRINTL
INPUTS
PRINTFORML picked=%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "[V] visible\npicked=visible\n")
        self.assertEqual(rt.warnings, [])

    def test_noskip_temporarily_reenables_output_without_clearing_isskip(self):
        td, program = self.make_game('''@SYSTEM_TITLE
SKIPDISP 1
PRINTL hidden-before
NOSKIP
PRINTFORML visible={ISSKIP()}
ENDNOSKIP
PRINTL hidden-after
SKIPDISP 0
PRINTFORML final={ISSKIP()}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "visible=1\nfinal=0\n")
        self.assertEqual(rt.warnings, [])

    def test_reset_stain_clears_one_character_slots(self):
        td, program = self.make_game('''@SYSTEM_TITLE
STAIN:0:0 = 7
STAIN:0:1 = 3
STAIN:1:0 = 5
RESET_STAIN 0
PRINTFORML {STAIN:0:0}:{STAIN:0:1}:{STAIN:1:0}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "0:0:5\n")
        self.assertEqual(rt.warnings, [])

    def test_cupcheck_applies_cup_cdown_to_character_palam(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 0
ADDCHARA 0
PALAM:0:4 = 100
PALAM:1:4 = 200
CUP:0:4 = 30
CDOWN:0:4 = 50
CUP:1:4 = 999
CUP:5 = 7
CUPCHECK 0
PRINTFORML {PALAM:0:4}:{PALAM:0:5}:{PALAM:1:4}:{CUP:0:4}:{CDOWN:0:4}:{CUP:1:4}:{CUP:5}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Chara0.csv").write_text("番号,0\n名前,A\n呼び名,A\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "80:7:200:0:0:999:0\n")
        self.assertEqual(rt.warnings, [])

    def test_upcheck_applies_up_down_to_character_palam(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 0
ADDCHARA 0
PALAM:0:4 = 100
PALAM:1:4 = 200
UP:4 = 30
DOWN:4 = 50
UP:5 = 7
UPCHECK 0
PRINTFORML {PALAM:0:4}:{PALAM:0:5}:{PALAM:1:4}:{UP:4}:{DOWN:4}:{UP:5}
RETURN
''')
        root = Path(td.name)
        (root / "CSV" / "Chara0.csv").write_text("番号,0\n名前,A\n呼び名,A\n", encoding="utf-8")
        program = load_program(root)
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "80:7:200:0:0:0\n")
        self.assertEqual(rt.warnings, [])

    def test_bar_command_and_barstr_render_progress(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORM pre
BAR 2, 4, 5
PRINTFORML |%BARSTR(3, 6, 4)%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "pre[**...]|[**..]\n")
        self.assertEqual(rt.warnings, [])

    def test_customdrawline_and_getsecond_command_compatibility(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CUSTOMDRAWLINE =
CUSTOMDRAWLINE ･
DRAWLINEFORM {1+1}
CUSTOMDRAWLINE
#DIM A
A = GETSECOND()
GETSECOND
PRINTFORML {A > 60000000000}:{RESULT >= A}:{RESULT - A <= 2}:%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        lines = "".join(rt.output).splitlines()
        self.assertEqual(lines[:4], ["=" * 72, "･" * 72, "2" * 72, "─" * 72])
        self.assertRegex(lines[4], r"^1:1:1:\d+$")
        self.assertEqual(rt.warnings, [])

    def test_forcekana_printk_family_converts_only_kana_prints(self):
        td, program = self.make_game('''@SYSTEM_TITLE
FORCEKANA 1
PRINTKL あいうカナ
PRINTL あいう
FORCEKANA 2
PRINTFORMKL カタ{1}
PRINTKL ｶﾅ
FORCEKANA 3
PRINTSKL "ｶﾞｷﾞ"
PRINTDATAK
DATA かなカナ
ENDDATA
FORCEKANA 0
PRINTKL あ
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "アイウカナ\nあいう\nかた1\nｶﾅ\nがぎ\nかなかな\nあ\n")
        self.assertEqual(rt.warnings, [])

    def test_reuselastline_replaces_previous_display_line(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL menu
PRINTFORM partial
REUSELASTLINE replaced {1+2}
PRINTL tail
REUSELASTLINE
PRINTL done
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual("".join(rt.output), "menu\nreplaced 3\ntail\ndone\n")

    def test_parenthesized_lvalue_for_and_printc_columns(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM I
FOR (I), 1, 4
PRINTFORMC {I}
NEXT
PRINTL end
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertIn("1".ljust(25) + "2".ljust(25) + "3".ljust(25) + "\nend\n", "".join(rt.output))

    def test_printcperline_reads_config_and_zero_disables_autowrap(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "emuera.config").write_text("PRINTCを並べる数:2\nPRINTCの文字数:4\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
PRINTFORML per={PRINTCPERLINE()}
PRINTC A
PRINTC B
PRINTC C
PRINTL done
RETURN
''', encoding="utf-8")
        self.addCleanup(td.cleanup)
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "per=2\nA   B   \nC   done\n")
        self.assertEqual(rt.warnings, [])

        td_zero = tempfile.TemporaryDirectory()
        root_zero = Path(td_zero.name)
        (root_zero / "ERB").mkdir()
        (root_zero / "CSV").mkdir()
        (root_zero / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root_zero / "emuera.config").write_text("PRINTCを並べる数:0\nPRINTCの文字数:3\n", encoding="utf-8")
        (root_zero / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
PRINTFORM {PRINTCPERLINE()}:
PRINTC A
PRINTC B
PRINTC C
PRINTL end
RETURN
''', encoding="utf-8")
        self.addCleanup(td_zero.cleanup)
        rt_zero = EraRuntime(load_program(root_zero), echo=False, interactive=False)
        rt_zero.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt_zero.output), "0:A  B  C  end\n")
        self.assertEqual(rt_zero.warnings, [])

    def test_printc_padding_and_truncation_use_east_asian_display_width(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIMS S
S = "漢字A"
PRINTC 漢A
PRINTC B
PRINTL |
PRINTC 漢字A
PRINTC C
PRINTL |
PRINTFORMLC %S%
PRINTC D
PRINTL |
RETURN
''')
        self.addCleanup(td.cleanup)
        (program.root / "emuera.config").write_text(
            "PRINTCを並べる数:0\nPRINTCの文字数:4\n内部で使用する東アジア言語:CHINESE_HANS\n",
            encoding="utf-8",
        )
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "漢A B   |\n漢字C   |\n漢字\nD   |\n")
        self.assertEqual(rt.warnings, [])

    def test_page_layout_uses_east_asian_display_width_for_cjk_print_buttons(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTS 漢
PRINTBUTTON "字", "go"
PRINTL
PRINTBUTTONC "漢A", "cell"
PRINTBUTTONLC "B", "next"
RETURN
''')
        self.addCleanup(td.cleanup)
        (program.root / "emuera.config").write_text(
            "PRINTCを並べる数:0\nPRINTCの文字数:4\n内部で使用する東アジア言語:CHINESE_HANS\n",
            encoding="utf-8",
        )
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        page = rt.html_page_model()
        self.assertEqual([(span["display_line"], span["col"], span["text"]) for span in page["style_spans"]], [
            (1, 0, "漢"),
            (1, 2, "字"),
            (2, 0, "漢A "),
            (2, 4, "B   "),
        ])
        self.assertEqual([(b["display_line"], b["col"], b["label"], b["value"]) for b in page["print_buttons"]], [
            (1, 2, "字", "go"),
            (2, 0, "漢A ", "cell"),
            (2, 4, "B   ", "next"),
        ])
        layout = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual([(b["label"], b["x"], b["width"], b["value"]) for b in layout["print_buttons"]], [
            ("字", 16, 16, "go"),
            ("漢A ", 0, 32, "cell"),
            ("B   ", 32, 32, "next"),
        ])
        self.assertEqual(rt.html_click_value(17, 1, char_width=8, line_height=20), "go")
        self.assertEqual(rt.html_click_value(31, 21, char_width=8, line_height=20), "cell")
        self.assertEqual(rt.html_click_value(33, 21, char_width=8, line_height=20), "next")
        self.assertEqual(rt.warnings, [])

    def test_printbuttonc_and_printbuttonlc_use_printc_layout(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "emuera.config").write_text("PRINTCを並べる数:3\nPRINTCの文字数:5\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
PRINTBUTTONC "A", 10
PRINTBUTTONLC "B", "two"
PRINTL done
RETURN
''', encoding="utf-8")
        self.addCleanup(td.cleanup)
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "A    B    \ndone\n")
        self.assertEqual(rt.pending_buttons, ["10", "two"])
        self.assertEqual(rt.warnings, [])

    def test_printd_family_uses_default_color_and_preserves_c_newline_semantics(self):
        td, program = self.make_game('''@SYSTEM_TITLE
SETCOLOR 0x112233
PRINTD plain
PRINTDL -line
PRINTFORMD F{1+1}
PRINTFORMDL -form
PRINTCD C
PRINTLCD D
PRINTDATAD
DATA row
ENDDATA
PRINTL colored
RETURN
''')
        self.addCleanup(td.cleanup)
        (program.root / "emuera.config").write_text(
            "PRINTCを並べる数:0\nPRINTCの文字数:4\n",
            encoding="utf-8",
        )
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "plain-line\nF2-form\nC   D   \nrow\ncolored\n")
        page = rt.html_page_model()
        spans = [(span["display_line"], span["text"], span["color"]) for span in page["style_spans"]]
        self.assertEqual(spans, [
            (1, "plain", rt.default_color),
            (1, "-line", rt.default_color),
            (2, "F2", rt.default_color),
            (2, "-form", rt.default_color),
            (3, "C   ", rt.default_color),
            (3, "D   ", rt.default_color),
            (4, "row", rt.default_color),
            (5, "colored", 0x112233),
        ])
        self.assertEqual(rt.warnings, [])

    def test_goto_out_of_inner_loop_prunes_loop_context(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM I
#DIM J
FOR I, 0, 2
  FOR J, 0, 4
    SIF J == 1
      GOTO NEXT_I
  NEXT
  PRINTL bad
$NEXT_I
NEXT
PRINTFORML I={I},J={J}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "I=2,J=1\n")

    def test_trycgoto_and_trygotoform_are_silent_label_jumps(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM LOCAL
LOCAL = 1
TRYCGOTO MISSING
PRINTFORM A|
TRYCGOTO HIT
PRINTFORM bad|
$HIT
PRINTFORM B|
TRYGOTOFORM FORM_%LOCAL%
PRINTFORM bad2|
$FORM_1
PRINTFORM C|
TRYCGOTOFORM NO_%LOCAL%
PRINTL D
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "A|B|C|D\n")

    def test_break_out_of_inner_loop_prunes_loop_context(self):
        td, program = self.make_game('''@SYSTEM_TITLE
#DIM I
#DIM J
FOR I, 0, 2
  FOR J, 0, 4
    BREAK
  NEXT
  PRINTFORM {I}:{J}|
NEXT
PRINTL done
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "0:0|1:0|done\n")

    def test_restart_rewinds_current_function_without_clearing_caller(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL title
CALL SUB
PRINTL after
RETURN

@SUB
LOCAL += 1
PRINTFORML sub={LOCAL}
SIF LOCAL < 2
RESTART
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "title\nsub=1\nsub=2\nafter\n")

    def test_dim_const_and_initializers(self):
        td, program = self.make_game('''#DIM CONST MAX_PLAYER_CHARA = 500
#DIMS CONST GLOBAL_LABELS = "A", "B"
@SYSTEM_TITLE
#DIM L_HIDE, 3 = 1,2,3
#DIMS CONST FUNCNAME = "ENHANCE_BASE", "MAG_SKILL_RANKUP"
#DIMS L_TEXT, 2 = "x","y"
PRINTFORML G={MAX_PLAYER_CHARA}:%GLOBAL_LABELS:0%/%GLOBAL_LABELS:1%
PRINTFORML L={L_HIDE:0}{L_HIDE:1}{L_HIDE:2}:%L_TEXT:0%/%L_TEXT:1%:%FUNCNAME:0%/%FUNCNAME:1%
RETURN
''')
        self.addCleanup(td.cleanup)
        self.assertTrue(program.var_decls["MAX_PLAYER_CHARA"].const)
        self.assertTrue(program.var_decls["MAX_PLAYER_CHARA"].module_scope)
        self.assertEqual(program.var_decls["MAX_PLAYER_CHARA"].initial, ("500",))
        self.assertTrue(program.var_decls["FUNCNAME"].const)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        out = "".join(rt.output)
        self.assertIn("G=500:A/B", out)
        self.assertIn("L=123:x/y:ENHANCE_BASE/MAG_SKILL_RANKUP", out)

    def test_graphics_and_resource_sprite_registry(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML preset={SPRITECREATED("preset")}:{SPRITEWIDTH("preset")}x{SPRITEHEIGHT("preset")}
GCREATE 7, 80, 40
SPRITECREATE "dyn", 7, 1, 2, 30, 12
PRINTFORML dyn={GCREATED(7)}:{GWIDTH(7)}x{GHEIGHT(7)}:{SPRITECREATED("dyn")}:{SPRITEWIDTH("dyn")}x{SPRITEHEIGHT("dyn")}
GCREATE 9, 100, 100
GDRAWSPRITE 9, "dyn"
GDRAWSPRITE 9, "dyn", 5, 6, 7, 8, 0x112233
GCLEAR 9, 0xFF
GDRAWSPRITE 9, "dyn", 9, 10
GCREATE 10, 100, 100
GDRAWSPRITE 10, "dyn", 5, 6, 7, 8, 0x112233
GCREATEFROMFILE 8, "resources\\img.png"
PRINTFORML file={RESULT}:{GCREATED(8)}:{GWIDTH(8)}x{GHEIGHT(8)}
PRINT_IMG "preset"
PRINTL
GDISPOSE 7
SPRITEDISPOSE "dyn"
PRINTFORML post={GCREATED(7)}:{SPRITECREATED("dyn")}
RETURN
''')
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "resources").mkdir()
        (root / "resources" / "画像.csv").write_text("preset,img.png,0,0,32,16\n", encoding="utf-8")
        (root / "resources" / "img.png").write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", 2, 3)
            + b"\x08\x06\x00\x00\x00"
        )
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(
            "".join(rt.output),
            "preset=1:32x16\n"
            "dyn=1:80x40:1:30x12\n"
            "file=1:1:2x3\n"
            "[IMG:preset]\n"
            "post=0:0\n",
        )
        self.assertEqual(rt.graphics[9]["clear"], 0xFF)
        self.assertEqual(rt.graphics[9]["draws"], ["dyn"])
        self.assertEqual(
            rt.graphics[9]["draw_ops"],
            [{"sprite": "dyn", "x": 9, "y": 10, "width": 30, "height": 12, "color_matrix": None, "color_matrix_arg": ""}],
        )
        self.assertEqual(
            rt.graphics[10]["draw_ops"],
            [{"sprite": "dyn", "x": 5, "y": 6, "width": 7, "height": 8, "color_matrix": 0x112233, "color_matrix_arg": "0x112233"}],
        )
        self.assertEqual(rt.print_images, [{"src": "preset", "width": "32", "height": "16", "col": 0}])

    def test_graphic_registry_can_render_and_cli_export_png(self):
        td, program = self.make_game('''#DIM MAT, 1, 5, 5
@SYSTEM_TITLE
GCREATEFROMFILE 1, "resources\\img.png"
SPRITECREATE "left", 1, 0, 0, 2, 2
SPRITECREATE "right", 1, 2, 0, 2, 2
GCREATE 2, 5, 3
GCLEAR 2, 0x00000000
GDRAWSPRITE 2, "left", 1, 1
GDRAWSPRITE 2, "right", 3, 1
MAT:0:0:0 = 0, 256, 0, 0, 0
MAT:0:1:0 = 256, 0, 0, 0, 0
MAT:0:2:0 = 0, 0, 256, 0, 0
MAT:0:3:0 = 0, 0, 0, 256, 0
MAT:0:4:0 = 0, 0, 0, 0, 256
GCREATE 3, 2, 2
GCLEAR 3, 0x00000000
GDRAWSPRITE 3, "left", 0, 0, 2, 2, MAT:0:0:0
PRINT_IMG "preset"
PRINTL
RETURN
''')
        self.addCleanup(td.cleanup)
        from PIL import Image

        root = Path(td.name)
        (root / "resources").mkdir()
        (root / "resources" / "画像.csv").write_text("preset,img.png,2,0,2,2\n", encoding="utf-8")
        source = Image.new("RGBA", (4, 2), (0, 0, 0, 0))
        for x in range(2):
            for y in range(2):
                source.putpixel((x, y), (255, 0, 0, 255))
                source.putpixel((x + 2, y), (0, 255, 0, 255))
        source.save(root / "resources" / "img.png")

        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        image = rt.render_graphic_image(2)
        self.assertEqual(image.size, (5, 3))
        self.assertEqual(image.getpixel((0, 0)), (0, 0, 0, 0))
        self.assertEqual(image.getpixel((1, 1)), (255, 0, 0, 255))
        self.assertEqual(image.getpixel((3, 1)), (0, 255, 0, 255))
        recolored = rt.render_graphic_image(3)
        self.assertEqual(recolored.getpixel((0, 0)), (0, 255, 0, 255))
        self.assertEqual(rt.print_images, [{"src": "preset", "width": "2", "height": "2", "col": 0}])
        sprite = rt.render_sprite_image("preset")
        self.assertEqual(sprite.size, (2, 2))
        self.assertEqual(sprite.getpixel((0, 0)), (0, 255, 0, 255))

        export_path = root / "exports" / "canvas.png"
        rt.export_graphic_png(2, export_path)
        self.assertEqual(Image.open(export_path).convert("RGBA").getpixel((4, 2)), (0, 255, 0, 255))
        sprite_export_path = root / "exports" / "sprite.png"
        rt.export_sprite_png("preset", sprite_export_path)
        self.assertEqual(Image.open(sprite_export_path).convert("RGBA").getpixel((0, 0)), (0, 255, 0, 255))

        cli_path = root / "exports" / "cli_canvas.png"
        rc = cli_main([
            "run",
            str(root),
            "--entry",
            "SYSTEM_TITLE",
            "--max-steps",
            "100",
            "--non-interactive",
            "--quiet",
            "--export-graphic",
            f"2={cli_path}",
            "--export-sprite",
            f"preset={root / 'exports' / 'cli_sprite.png'}",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(Image.open(cli_path).convert("RGBA").getpixel((1, 1)), (255, 0, 0, 255))
        self.assertEqual(Image.open(root / "exports" / "cli_sprite.png").convert("RGBA").getpixel((0, 0)), (0, 255, 0, 255))

    def test_page_snapshot_can_render_rects_and_sprites_to_png(self):
        td, program = self.make_game('''@SYSTEM_TITLE
SETCOLOR 0xFF0000
PRINT_RECT 4
PRINTL
PRINT_IMG "preset"
PRINTL
HTML_PRINT "<img src='preset' width='2' height='2'>"
RETURN
''')
        self.addCleanup(td.cleanup)
        from PIL import Image

        root = Path(td.name)
        (root / "resources").mkdir()
        (root / "resources" / "画像.csv").write_text("preset,img.png,0,0,2,2\n", encoding="utf-8")
        sprite = Image.new("RGBA", (2, 2), (0, 255, 0, 255))
        sprite.save(root / "resources" / "img.png")

        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        page = rt.render_page_image(char_width=8, line_height=10)
        self.assertGreaterEqual(page.size[0], 8)
        self.assertGreaterEqual(page.size[1], 30)
        self.assertEqual(page.getpixel((0, 0)), (255, 0, 0, 255))
        self.assertEqual(page.getpixel((0, 10)), (0, 255, 0, 255))
        self.assertEqual(page.getpixel((0, 20)), (0, 255, 0, 255))
        export_path = root / "exports" / "page.png"
        rt.export_page_png(export_path, char_width=8, line_height=10)
        self.assertEqual(Image.open(export_path).convert("RGBA").getpixel((0, 10)), (0, 255, 0, 255))

    def test_page_snapshot_renders_fontstyle_underline_and_strikeout(self):
        td, program = self.make_game('''@SYSTEM_TITLE
SETCOLOR 0xFF0000
FONTSTYLE 12
PRINTSL "    "
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(rt.html_page_model()["style_spans"][0]["font_style"], 12)
        rendered = rt.render_page_image(char_width=8, line_height=20)
        self.assertEqual(rendered.getpixel((0, 10)), (255, 0, 0, 255))
        self.assertEqual(rendered.getpixel((31, 18)), (255, 0, 0, 255))
        self.assertEqual(rt.warnings, [])

    def test_page_snapshot_renders_fontstyle_italic_differently(self):
        td, normal_program = self.make_game('''@SYSTEM_TITLE
SETCOLOR 0xFF0000
PRINTSL "IIII"
RETURN
''')
        self.addCleanup(td.cleanup)
        td2, italic_program = self.make_game('''@SYSTEM_TITLE
SETCOLOR 0xFF0000
FONTSTYLE 2
PRINTSL "IIII"
RETURN
''')
        self.addCleanup(td2.cleanup)
        normal = EraRuntime(normal_program, echo=False, interactive=False)
        italic = EraRuntime(italic_program, echo=False, interactive=False)
        normal.run("SYSTEM_TITLE", max_steps=100)
        italic.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual(italic.html_page_model()["style_spans"][0]["font_style"], 2)
        normal_image = normal.render_page_image(char_width=8, line_height=20)
        italic_image = italic.render_page_image(char_width=8, line_height=20)
        self.assertNotEqual(normal_image.tobytes(), italic_image.tobytes())
        self.assertGreaterEqual(italic_image.size[0], normal_image.size[0])
        self.assertEqual(italic.warnings, [])

    def test_print_img_preserves_current_column_for_layout_and_page_png(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTS "AB"
PRINT_IMG "preset"
PRINTL
RETURN
''')
        self.addCleanup(td.cleanup)
        from PIL import Image

        root = Path(td.name)
        (root / "resources").mkdir()
        (root / "resources" / "画像.csv").write_text("preset,img.png,0,0,2,2\n", encoding="utf-8")
        sprite = Image.new("RGBA", (2, 2), (0, 255, 0, 255))
        sprite.save(root / "resources" / "img.png")

        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        page = rt.html_page_model()
        self.assertEqual([(img["display_line"], img["col"], img["src"]) for img in page["print_images"]], [(1, 2, "preset")])
        layout = rt.html_layout_model(char_width=8, line_height=10)
        self.assertEqual([(img["x"], img["y"], img["width"], img["height"]) for img in layout["print_images"]], [(16, 0, 2, 2)])
        rendered = rt.render_page_image(char_width=8, line_height=10)
        self.assertEqual(rendered.getpixel((16, 0)), (0, 255, 0, 255))
        self.assertNotEqual(rendered.getpixel((0, 0)), (0, 255, 0, 255))

    def test_write_img_and_get_img_type_native_helpers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 10
CFLAG:0:顔グラ = 2
CFLAG:0:ショップ顔グラ = 3
PRINTFORML T={GET_IMG_TYPE(0,0,-100)}:{GET_IMG_TYPE(0,1,0)}
CALL WRITE_IMG, 0, 301, 2, "キャラ番号指定/ア禮服取得"
PRINTFORML ADDR=%RESULTS%:{RESULT}
CALL WRITE_IMG, 0, 301, 2, "キャラ番号指定"
PRINTL
CALL WRITE_IMG, 10, 0, 1
PRINTL
CALL WRITE_IMG, 0, 0, 1, "キャラ番号指定"
PRINTFORML MISS=%RESULTS%:{RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "resources").mkdir()
        (root / "resources" / "画像.csv").write_text("A10_301_2,img.png,0,0,32,16\n", encoding="utf-8")
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "T=300:301\n"
            "ADDR=A10_301_2:1\n"
            "[IMG:A10_301_2]\n"
            "▭▭▭▭\n"
            "MISS=NO_IMG:0\n",
        )
        page = rt.html_page_model()
        self.assertEqual([(img["display_line"], img["col"], img["src"], img["width"], img["height"]) for img in page["print_images"]], [(3, 0, "A10_301_2", "32", "16")])
        self.assertEqual([(rect["display_line"], rect["col"], rect["width"]) for rect in page["print_rects"]], [(4, 0, 400)])
        layout = rt.html_layout_model(char_width=8, line_height=20)
        self.assertEqual([(rect["x"], rect["y"], rect["width"], rect["height"]) for rect in layout["print_rects"]], [(0, 60, 400, 20)])

    def test_write_img_special_face_hook(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 12
CALL WRITE_IMG, 0, 0, 1, "キャラ番号指定"
PRINTL
PRINTFORML R={RESULT}:%RESULTS%
RETURN

@PRINT_SPECIAL_FACE_CHARA_12(ARG, ARG:1, ARG:2, ARG:3)
RESULT = 1
RESULTS = "special_face"
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "[IMG:special_face]\nR=1:1\n")

    def test_face_graphic_add_creates_sprites_from_image_file(self):
        td, program = self.make_game('''@SYSTEM_TITLE
CALL 顔グラ追加, 10
PRINTFORML S={EXIST_PICTURE(10,0,1)}:{SPRITEWIDTH("A10_0_1")}x{SPRITEHEIGHT("A10_0_1")}:{EXIST_PICTURE(10,1,6)}
RETURN
''')
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "resources" / "画像_自家製").mkdir(parents=True)
        (root / "resources" / "画像_自家製" / "A10_0.png").write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", 24, 24)
            + b"\x08\x06\x00\x00\x00"
        )
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=500)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "S=1:24x6:1\n")

    def test_equip_detail_item_list_native_selection(self):
        td, program = self.make_game('''#DIM 物品リスト, 5
@SYSTEM_TITLE
物品リスト:0 = 42
物品リスト:1 = 77
物品リスト:2 = -1
CALL EQUIP_DETAIL_ITEM_LIST, 0, -1
PRINTFORML SEL={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["77"])
        rt.run("SYSTEM_TITLE", max_steps=300)
        self.assertEqual(rt.warnings, [])
        self.assertIn("SEL=77\n", "".join(rt.output))

    def test_noninteractive_equip_detail_item_list_waits_when_explicit_inputs_exhaust(self):
        td, program = self.make_game('''#DIM 物品リスト, 5
@SYSTEM_TITLE
物品リスト:0 = 42
物品リスト:1 = -1
INPUT
CALL EQUIP_DETAIL_ITEM_LIST, 0, -1
PRINTFORML SEL={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["seed"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertNotIn("SEL=", "".join(rt.output))
        self.assertEqual(rt.queue_input("42"), "42")
        rt.continue_run(max_steps=200)
        out = "".join(rt.output)
        self.assertIn("SEL=42\n", out)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])

    def test_fix_add_master_701_debug_menu_native_fallback(self):
        td, program = self.make_game('''@SYSTEM_TITLE
FLAG:DEBUG = 1
CALL SHOPCOMABLE_701
PRINTFORML C={RESULT}:%RESULTS%
CALL SHOP_COM_701
PRINTFORML R={RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["1"])
        rt.run("SYSTEM_TITLE", max_steps=300)
        out = "".join(rt.output)
        self.assertEqual(rt.warnings, [])
        self.assertIn("C=1:旧あなた加入問題修正\n", out)
        self.assertIn("修正を実行してもよろしいですか？", out)
        self.assertIn("R=1\n", out)

    def test_image_edit_list_and_dictionary_helpers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
LOCALS = LIST_ADD("red,blue,", "green")
PRINTFORML L=%LOCALS%:%LIST_GET(LOCALS,1)%:{LIST_COUNT(LOCALS)}:{LIST_INDEXOF(LOCALS,"green")}
PRINTFORML LS=%LIST_SORT_R("b,a,c,")%
LOCALS:1 = DIC_SET("[hair:short][eye:blue]", "hair", "long")
LOCALS:1 = DIC_SET(LOCALS:1, "skin", "pale")
PRINTFORML D=%DIC_GET(LOCALS:1,"hair")%:{DIC_CONTAINSKEY(LOCALS:1,"skin")}:{DIC_COUNT(LOCALS:1)}:%DIC_REMOVE(LOCALS:1,"eye")%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "L=red,blue,green,:blue:3:2\n"
            "LS=c,b,a,\n"
            "D=long:1:3:[hair:long][skin:pale]\n",
        )

    def test_attack_skill_special_target_default_native_predicate(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ARG = 0
LOCAL = 5
CALLFORM SKILL_SPECIAL_TARGET_{ARG}, LOCAL
PRINTFORML R={RESULT}:%RESULTS%
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=50)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "R=1:1\n")

    def test_temp_status_reset_fast_path_resets_modifiers_and_syncs_touched_party(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 0
ADDCHARA 0
CFLAG:0:PTフラグ = 2
CFLAG:1:PTフラグ = 0
CFLAG:0:ＨＰ補正 = 5
CFLAG:0:物理被傷害補正 = 11
CFLAG:1:速度補正 = 7
CFLAG:1:剣撃与傷害補正 = 22
CALL TEMP_STATUS_RESET
PRINTFORML %FLAG:77%:{CFLAG:0:ＨＰ補正}:{CFLAG:1:速度補正}:{CFLAG:0:物理被傷害補正}:{CFLAG:1:剣撃与傷害補正}
RETURN

@SYNC_STATUS(ARG)
FLAG:77 += 1
RETURN

@TEMP_STATUS_RESET
PRINTL slow-path
RETURN
''')
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "CSV" / "CFLAG.csv").write_text(
            "\n".join(
                [
                    "1,物理被傷害補正",
                    "4,物理与傷害補正",
                    "10,剣撃被傷害補正",
                    "40,剣撃与傷害補正",
                    "100,ＨＰ補正",
                    "101,ＭＰ補正",
                    "102,速度補正",
                    "200,PTフラグ",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        program = load_program(root)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "1:0:0:0:0\n")

    def test_equipment_enhance_expand_fast_path_preserves_common_defaults(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 0
RESULT = 77
CALL 装備強化_展開, 0, 0, "戦闘能力修正", "攻撃", -1
PRINTFORML A={RESULT}
CALL 装備強化_展開, 0, 123, "戦闘能力修正", "攻撃", 12
PRINTFORML B={RESULT}
CALL 装備強化_展開, 0, 0, "防御相性", "火炎", -1
PRINTFORML C={RESULT}
RESULT = 88
CALL 装備強化_展開, 0, 123, "追加効果", "ステート", -1
PRINTFORML D={RESULT}
EQUIP:0:魔装術 = 1
CALL 装備強化_展開, 0, 123, "追加効果", "ステート", -1
PRINTFORML E={RESULT}
CALL 装備強化_展開, 0, 123, "攻撃相性", "付与相性"
PRINTFORML F={RESULT}
RETURN

@装備強化_展開(ARG, ARG:1, ARGS:1, ARGS:2, ARG:2 = -1, ARG:3 = -1)
PRINTL slow-path
RETURN 999
''')
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "CSV" / "CSTR.csv").write_text("60,装備強化\n", encoding="utf-8")
        (root / "CSV" / "EQUIP.csv").write_text("10,魔装術\n", encoding="utf-8")
        program = load_program(root)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(rt.warnings, [])
        self.assertEqual("".join(rt.output), "A=77\nB=12\nC=100\nD=88\nE=0\nF=-1\n")

    def test_formation_face_fast_path_returns_resource_name(self):
        td, program = self.make_game('''@SYSTEM_TITLE
ADDCHARA 0
CALL PRINT_FORMATION_FACE_P, 0, 1, 0, "ア禮服取得"
PRINTFORML %RESULTS%:{RESULT}
CALL PRINT_FORMATION_FACE_P, 0, 2, 0, "ア禮服取得"
PRINTFORML %RESULTS%:{RESULT}
RETURN
''')
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "CSV" / "Chara0.csv").write_text("番号,0\n名前,A\n呼び名,A\n", encoding="utf-8")
        (root / "resources").mkdir()
        (root / "resources" / "画像.csv").write_text("A0_0_1,img.png,0,0,32,16\n", encoding="utf-8")
        program = load_program(root)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "A0_0_1:1\nNO_IMG:0\n")

    def test_era_megaten_strflag_native_helpers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTFORML B=%ADD_STRFLAG("","A")%:%DEL_STRFLAG("/A/B/","A")%:%SWAP_STRFLAG("/A/B/","A","C")%
FLAG:現ダンジョン = 2
PRINTFORML D0={STRFLAG_D("旗",0)}
PRINTFORML D1={STRFLAG_D("旗",1)}
PRINTFORML D2={STRFLAG_D("旗",0)}:%SAVESTR:102%
PRINTFORML D3={STRFLAG_D("旗",-1)}:%SAVESTR:102%
PRINTFORML N0={STRFLAG_NUM_D("進","=",0,7,2)}:%SAVESTR:102%
PRINTFORML N1={STRFLAG_NUM_D("進")}
PRINTFORML N2={STRFLAG_NUM_D("進","==",7,9,2)}:%SAVESTR:102%
PRINTFORML N3={STRFLAG_NUM_D("進","!=",7,3,2)}:%SAVESTR:102%
PRINTFORML EV={STRFLAG_EV("事件",1,3)}:%SAVESTR:203%
PRINTFORML CLO={STRFLAG_CLO("闘",1,2)}:%SAVESTR:302%
PRINTFORML REQS={STRFLAG_REQ("依頼旗",1,2)}:%SAVESTR:402%
PRINTFORML COL={STRFLAG_NUM_COL("勝","=",0,4,1)}:%SAVESTR:301%
PRINTFORML REQ={STRFLAG_NUM_REQ("依","=",0,5,1)}:%SAVESTR:401%
PRINTFORML C={CSTRFLAG_NUM("C","=",0,6,1)}:%CSTR:1%:{CSTRFLAG_NUM("C")}
PRINTFORML T={TSTRFLAG_NUM("T","=",0,8,1)}:%TSTR:1%:{TSTRFLAG_NUM("T")}
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "B=/A/:/B/:/A/B/C/\n"
            "D0=0\n"
            "D1=0\n"
            "D2=1:/旗/\n"
            "D3=1:/\n"
            "N0=1:/|進|7|/\n"
            "N1=7\n"
            "N2=1:/|進|9|/\n"
            "N3=1:/|進|3|/\n"
            "EV=0:/事件/\n"
            "CLO=0:/闘/\n"
            "REQS=0:/依頼旗/\n"
            "COL=1:/|勝|4|/\n"
            "REQ=1:/|依|5|/\n"
            "C=1:/|C|6|/:6\n"
            "T=1:/|T|8|/:8\n",
        )

    def test_era_megaten_cpd_registration_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        base_names = ["LV", "力", "知恵", "魔力", "耐力", "速度", "運", "ＥＸＰ"]
        cflag_names = [
            "力強化回数", "知恵強化回数", "魔力強化回数", "耐力強化回数",
            "速度強化回数", "運強化回数", "能力強化回数", "変更相性1",
            "変更相性値1", "攻撃相性", "射程", "攻撃範囲", "全書召喚不可", "合体条件有り",
        ]
        abl_names = ["種族", "変異", "変異等級"]
        abl_names += [f"技能{i}" for i in range(1, 9)]
        abl_names += [f"習得技能{i}" for i in range(1, 21)]
        abl_names += [f"習得LV{i}" for i in range(1, 21)]
        (root / "CSV" / "BASE.csv").write_text(
            "\n".join(f"{30 + i},{name}" for i, name in enumerate(base_names)) + "\n",
            encoding="utf-8",
        )
        (root / "CSV" / "CFLAG.csv").write_text(
            "\n".join(f"{100 + i},{name}" for i, name in enumerate(cflag_names)) + "\n",
            encoding="utf-8",
        )
        (root / "CSV" / "ABL.csv").write_text(
            "\n".join(f"{80 + i},{name}" for i, name in enumerate(abl_names)) + "\n",
            encoding="utf-8",
        )
        (root / "CSV" / "TALENT.csv").write_text("0,処女\n2,恋慕\n10,再生処女\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDCHARA 0
NAME:0 = "Alpha"
NO:0 = 42
BASE:0:30 = 10
BASE:0:31 = 11
BASE:0:37 = 123
CFLAG:0:100 = 2
ABL:0:80 = 1
ABL:0:83 = 301
TALENT:0:処女 = 1
TALENT:0:恋慕 = 1
PRINTFORML F=%GET_CPD_STRFLAG(0)%:%GET_CPD_STRFLAG(2)%:{GET_CPD_STRFLAG_NUM("LV")}:{GET_CPD_STRFLAG_NUM("")}
CALL STRFLAG_NUM_CPD, 0, "ADD"
PRINTFORML FIND={STRFLAG_NUM_CPD_FIND("NO",42)}:{STRFLAG_NUM_CPD_FIND("",-1)}
PRINTFORML G=%GET_CPD_SAVESTR_NUM(2000,"NO")%:%GET_CPD_SAVESTR_NUM(2000,"LV")%:%GET_CPD_SAVESTR_NUM(2000,"力強化回数")%
BASE:0:30 = 1
BASE:0:31 = 1
CFLAG:0:100 = 0
ABL:0:83 = 0
TALENT:0:処女 = 0
TALENT:0:恋慕 = 0
CALL STRFLAG_NUM_CPD, 0, "REFER"
PRINTFORML R={BASE:0:30}:{BASE:0:31}:{CFLAG:0:100}:{ABL:0:83}:{TALENT:0:処女}:{TALENT:0:恋慕}
CALL STRFLAG_NUM_CPD, 0, "DIFF"
PRINTFORML D={文字色変更}
BASE:0:37 = 1
CALL STRFLAG_NUM_CPD, 0, "DIFF"
PRINTFORML D2={文字色変更}
CALL STRFLAG_NUM_CPD, 0, "CLEAR"
PRINTFORML C=%SAVESTR:2000%:%SAVESTR:2001%:{STRFLAG_NUM_CPD_FIND("NO",42)}
RETURN
''', encoding="utf-8")
        program = load_program(root)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=500)
        self.assertEqual(rt.warnings, [])
        out = "".join(rt.output)
        self.assertIn("F=NAME:LV:2:72\n", out)
        self.assertIn("FIND=2000:2001\n", out)
        self.assertIn("G=42:10:2\n", out)
        self.assertIn("R=10:11:2:301:1:1\n", out)
        self.assertIn("D=0\n", out)
        self.assertIn("D2=14423100\n", out)
        self.assertIn("C=::-1\n", out)

    def test_era_megaten_character_search_count_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "BASE.csv").write_text("0,良好\n1,瀕死\n", encoding="utf-8")
        (root / "CSV" / "ABL.csv").write_text(
            "\n".join(
                [
                    "10,種族",
                    "20,技能1",
                    "40,装備技能1",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "CSV" / "CFLAG.csv").write_text(
            "\n".join(
                [
                    "10,PTフラグ",
                    "11,所属ＣＯＭＰ",
                    "12,容量未使用",
                    "13,合体不可",
                    "14,この場に居ないフラグ",
                    "15,ステート",
                    "16,事件加入",
                    "17,ボスフラグ",
                    "18,リンク悪魔",
                    "19,悪魔変身",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "CSV" / "TALENT.csv").write_text(
            "\n".join(
                [
                    "1,召喚師",
                    "2,妻",
                    "3,夫",
                    "4,淫魔",
                    "5,玩具",
                    "6,盟友",
                    "7,Aion式召喚術",
                    "8,Persona使",
                    "9,異能者",
                    "10,達人",
                    "11,人修羅",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "CSV" / "EQUIP.csv").write_text("1,恶魔会议室\n", encoding="utf-8")
        (root / "CSV" / "FLAG.csv").write_text(
            "\n".join(
                [
                    "1,ポジション1",
                    "2,ポジション2",
                    "3,ポジション3",
                    "4,ポジション4",
                    "5,ポジション5",
                    "6,ポジション6",
                    "10,COMP使用不能",
                    "11,技能数",
                    "12,異能者技能数",
                    "13,ＣＯＭＰ容量",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDCHARA 0
ADDCHARA 100
ADDCHARA 101
ADDCHARA 102
ADDCHARA 103
ADDCHARA 100
ADDCHARA 104
ADDCHARA 900
ADDCHARA 901
MASTER = 0
FLAG:技能数 = 1
FLAG:異能者技能数 = 1
FLAG:ＣＯＭＰ容量 = 5
FLAG:ポジション1 = 1
FLAG:ポジション2 = 2
FLAG:ポジション3 = 3
FLAG:ポジション4 = -1
FLAG:ポジション5 = -1
FLAG:ポジション6 = -1
CFLAG:1:PTフラグ = 1
CFLAG:2:PTフラグ = 1
CFLAG:3:PTフラグ = 1
CFLAG:4:PTフラグ = 1
CFLAG:5:PTフラグ = 1
CFLAG:6:PTフラグ = 1
CFLAG:1:所属ＣＯＭＰ = 0
CFLAG:2:所属ＣＯＭＰ = 0
CFLAG:3:所属ＣＯＭＰ = 0
CFLAG:4:所属ＣＯＭＰ = 0
CFLAG:5:所属ＣＯＭＰ = -1
CFLAG:6:所属ＣＯＭＰ = 0
ABL:1:種族 = 10
ABL:2:種族 = 45
ABL:3:種族 = 20
ABL:4:種族 = 5
ABL:5:種族 = 5
ABL:6:種族 = 5
CFLAG:4:容量未使用 = 1
ABL:1:技能1 = 77
ABL:3:技能1 = 77
TALENT:1:召喚師 = 3
TALENT:2:召喚師 = 5
TALENT:3:召喚師 = 10
CFLAG:3:ステート = 1
TALENT:6:妻 = 1
CFLAG:7:ステート = 0
CFLAG:8:ステート = 1
PRINTFORML S={GET_SUMMONER_LV()}:{GET_SUMMONER_MLV()}:{NUM_SUMMONER(3)}:{NUM_HAVESKILL(77)}
PRINTFORML N={NUM_NAKAMA()}:{NUM_NAKAMA_HEADCOUNT()}:{NUM_FUSIONABLE()}:{NUM_ZOUMA()}:{ＣＯＭＰ空き容量()}
EQUIP:MASTER:恶魔会议室 = 1
PRINTFORML M={NUM_NAKAMA()}:{NUM_NAKAMA_HEADCOUNT()}:{ＣＯＭＰ空き容量()}
PRINTFORML F={FINDCHARA_NO_C(100)}:{契約(6)}:{CHARANUM_DIGIT()}
PRINTFORML E={FINDCHARA_ENEMY(900)}:{RESULT:1}:{FINDCHARA_ENEMY(901)}:{RESULT:1}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=500)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "S=8:5:2:1\n"
            "N=4:4:5:1:1\n"
            "M=0:4:1\n"
            "F=1:1:1\n"
            "E=1:7:3:8\n",
        )

    def test_era_megaten_battle_system_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "BASE.csv").write_text(
            "\n".join(
                [
                    "0,良好",
                    "1,瀕死",
                    "2,麻痺",
                    "3,休克",
                    "4,休眠",
                    "5,跌倒",
                    "6,灼熱",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "CSV" / "CFLAG.csv").write_text(
            "\n".join(
                [
                    "10,PTフラグ",
                    "11,ステート",
                    "12,悪魔変身",
                    "13,リンク悪魔",
                    "14,初期LINK悪魔",
                    "15,キャラ固有の番号",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "CSV" / "TALENT.csv").write_text(
            "\n".join(
                [
                    "1,悪魔変身",
                    "2,喰奴",
                    "3,Aion式召喚術",
                    "4,Persona使",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "CSV" / "EQUIP.csv").write_text("1,装備Persona\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDCHARA 100
ADDCHARA 101
ADDCHARA 102
ADDCHARA 103
ADDCHARA 104
ADDCHARA 105
NO:0 = 100
NO:1 = 101
NO:2 = 202
NO:3 = 303
NO:4 = 404
CFLAG:1:PTフラグ = 2
CFLAG:4:PTフラグ = 2
CFLAG:2:PTフラグ = 0
CFLAG:3:PTフラグ = 0
CFLAG:2:ステート = 1
CFLAG:3:ステート = 2
CFLAG:5:ステート = 5
PRINTFORML A={ACTIONABLE_CHARA_F(1)}:{ACTIONABLE_CHARA_F(3)}:{ACTIONABLE_CHARA_F(5)}
ACTIONABLE_CHARA 3
PRINTFORML ACMD={RESULT}:{RESULTS}
PRINTFORML C=%CONVERT_BADSTATE_NAME("睡眠")%:%CONVERT_BADSTATE_NAME("感電")%:%CONVERT_BADSTATE_NAME("炎上")%:%CONVERT_BADSTATE_NAME("不明")%
PRINTFORML B={IS_BADSTATE(3,"麻痺")}:{IS_BADSTATE(3,"感電")}:{IS_TARGET_ABLE(-1)}:{IS_TARGET_ABLE(0)}:{IS_TARGET_ABLE(1)}:{IS_TARGET_ABLE(2)}
PRINTFORML F={IS_FRIEND(1,4)}:{IS_FRIEND(1,2)}:{IS_FRIEND(2,3)}:{IS_FRIEND(-1,2)}
PRINTFORML R={IS_FRONT(1)}:{IS_FRONT(4)}:{IS_FRONT(7)}:{GET_BTL_RANGE(1,4)}:{GET_BTL_RANGE(1,2)}:{GET_BTL_RANGE(4,2)}
PRINTFORML P={GET_POS_MIN(18)}:{GET_POS_MAX(18)}:{GET_POS_MIN(22)}:{GET_POS_MAX(22)}
PRINTFORML W=%GET_WEAKNESS(999)%:%GET_WEAKNESS(-1)%:%GET_WEAKNESS(0)%:%GET_WEAKNESS(50)%:%GET_WEAKNESS(100)%:%GET_WEAKNESS(150)%
TALENT:1:Persona使 = 1
EQUIP:1:装備Persona = 7
DITEMTYPE:7:Persona("NO") = 555
TALENT:2:悪魔変身 = 1
CFLAG:2:悪魔変身 = 1
CFLAG:2:初期LINK悪魔 = 777
TALENT:3:Aion式召喚術 = 1
CFLAG:3:リンク悪魔 = 9001
CFLAG:4:キャラ固有の番号 = 9001
PRINTFORML N={BTL_NO(1)}:{BTL_NO(2)}:{BTL_NO(3)}
EQUIP:1:装備Persona = 0
PRINTFORML N2={BTL_NO(1)}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=500)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "A=1:0:0\n"
            "ACMD=0:0\n"
            "C=休眠:休克:燃烧:不明\n"
            "B=1:0:0:0:1:0\n"
            "F=1:0:1:0\n"
            "R=1:0:1:0:1:2\n"
            "P=4:6:7:16\n"
            "W=反射:吸収:無効:耐性:通常:弱点\n"
            "N=555:777:404\n"
            "N2=101\n",
        )

    def test_era_megaten_ai_judgement_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text("".join(f"{i},ポジション{i}\n" for i in range(1, 17)), encoding="utf-8")
        (root / "CSV" / "CFlag.csv").write_text("0,ステート\n1,ターゲット\n", encoding="utf-8")
        (root / "CSV" / "CStr.csv").write_text("50,弱点記憶\n", encoding="utf-8")
        (root / "CSV" / "Base.csv").write_text("0,ＨＰ\n1,火炎\n10,良好\n11,瀕死\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDVOIDCHARA
ADDVOIDCHARA
ADDVOIDCHARA
ADDVOIDCHARA
ADDVOIDCHARA
FLAG:ポジション1 = 0
FLAG:ポジション2 = 1
FLAG:ポジション3 = 2
FLAG:ポジション7 = 3
FLAG:ポジション8 = 4
BASE:0:ＨＰ = 50
BASE:1:ＨＰ = 20
BASE:2:ＨＰ = 5
CFLAG:2:ステート = 1
BASE:3:ＨＰ = 8
BASE:4:ＨＰ = 3
CALL ATTACK_MIN_HP, 0, 0
PRINTFORML T1={CFLAG:0:ターゲット}:{RESULT}
CALL ATTACK_MIN_HP, 0, 1
PRINTFORML T2={CFLAG:0:ターゲット}:{RESULT}
MAXBASE:0:火炎 = 150
MAXBASE:1:火炎 = 80
MAXBASE:2:火炎 = 120
CALL MEMORIZE_WEAKNESS, 4, 20, "火炎", 0
PRINTFORML M0={RESULT}:{RESULT:1}:{RESULT:2}:{RESULT:3}:{RESULT:4}:%CSTR:4:50%
CALL MEMORIZE_WEAKNESS, 4, 20, "火炎", 1
PRINTFORML M1={RESULT}:{RESULT:1}:{RESULT:2}:{RESULT:3}:{RESULT:4}:%CSTR:4:50%
PRINTFORML W={CHECK_WEAKNESS(999)}:{CHECK_WEAKNESS(-1)}:{CHECK_WEAKNESS(0)}:{CHECK_WEAKNESS(50)}:{CHECK_WEAKNESS(100)}:{CHECK_WEAKNESS(150)}:{CHECK_WEAKNESS(1000)}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=1000)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "T1=2:2\n"
            "T2=8:8\n"
            "M0=1000:1000:-1:1000:-1:0_火炎_150/1_火炎_80/2_火炎_120\n"
            "M1=150:150:1:80:2:0_火炎_150/1_火炎_80/2_火炎_120\n"
            "W=-4:-3:-2:-1:0:1:100\n",
        )

    def test_era_megaten_persona_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Talent.csv").write_text("0,Persona使\n", encoding="utf-8")
        (root / "CSV" / "Equip.csv").write_text("0,装備Persona\n", encoding="utf-8")
        (root / "CSV" / "CFlag.csv").write_text("0,初期Personaナンバー\n", encoding="utf-8")
        (root / "CSV" / "Abl.csv").write_text("0,初期Persona\n", encoding="utf-8")
        (root / "CSV" / "Chara10.csv").write_text("番号,10\n名前,Initial\n呼び名,InitialCall\n", encoding="utf-8")
        (root / "CSV" / "Chara20.csv").write_text("番号,20\n名前,Equipped\n呼び名,EquippedCall\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''#DIM GLOBAL DITEMTYPE, 100, 20
@SYSTEM_TITLE
ADDVOIDCHARA
ADDVOIDCHARA
STR:0 = Persona資料／装備状態
STR:1 = Persona資料／NO
STR:2 = Persona資料／LV
TALENT:0:Persona使 = 1
CFLAG:0:初期Personaナンバー = 7
ABL:0:初期Persona = 10
EQUIP:0:装備Persona = 5
DITEMTYPE:5:GET_DITEMTYPE_NUM("NO") = 20
DITEMTYPE:5:GET_DITEMTYPE_NUM("LV") = 44
DITEMTYPE:7:GET_DITEMTYPE_NUM("LV") = 55
PRINTFORML P={現在のPersona(0)}:%GET_PERSONA_NAME(0)%:{Persona資料(5,"LV")}:{装備Persona資料(0,"LV")}
CALL Persona編集, 5, "LV", 99
PRINTFORML E={Persona資料(5,"LV")}
EQUIP:0:装備Persona = 0
PRINTFORML I={現在のPersona(0)}:%GET_PERSONA_NAME(0)%:{装備Persona資料(0,"LV")}
PRINTFORML N={現在のPersona(1)}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=1000)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "P=5:EquippedCall:44:44\n"
            "E=99\n"
            "I=7:InitialCall:55\n"
            "N=0\n",
        )

    def test_era_megaten_kojo_and_animation_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text(
            "0,指令表示行数\n1,現ダンジョン\n2,ＣＯＭＰ容量\n3,月齢\n4,月齢ベクトル\n5,現X\n6,現Y\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Base.csv").write_text("0,ＭＡＧ\n", encoding="utf-8")
        (root / "CSV" / "Talent.csv").write_text(
            "0,男性\n1,偽娘\n2,小人体型\n3,体型嬌小\n4,高大\n5,巨人\n6,精力超群\n7,Ｃ敏感\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Exp.csv").write_text("0,射精経験\n1,性交経験\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDVOIDCHARA
ADDVOIDCHARA
BASE:0:ＭＡＧ = 321
MONEY = 1234
FLAG:ＣＯＭＰ容量 = 12
FLAG:月齢 = 3
FLAG:月齢ベクトル = 0
TALENT:0:男性 = 1
EXP:0:射精経験 = 1
EXP:0:性交経験 = 0
PRINTFORML K=%卑語_おちん()%|%卑語_陰茎()%|%卑語_精液(-1)%|%卑語_精液(0)%|%卑語_精液(1)%
CALL SHOW_PICTURE, "NONFLOORD", "F1", "A/B", "/", "CENTER"
PRINTFORML AFTER1={RESULT}:{FLAG:指令表示行数}:{CURRENTALIGN()}
CALL SHOW_PICTURE, "再利用"
PRINTFORML AFTER2={RESULT}:{FLAG:指令表示行数}
CALL SHOW_PICTURE, "blank", "F2", "C|D", "|", "RIGHT"
PRINTFORML AFTER3={RESULT}:{FLAG:指令表示行数}:{CURRENTALIGN()}
FLAG:現X = 10
FLAG:現Y = 20
CALL SHOW_FORCEMOVE, "UR<3>D", "D", "", "", "EMPTY"
PRINTFORML MOVE={RESULT}:{FLAG:現X}:{FLAG:現Y}:{GETBIT(FLAG:233,0)}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=1000)
        self.assertEqual(rt.warnings, [])
        out = "".join(rt.output)
        first = out.splitlines()[0]
        self.assertTrue(first.startswith("K="))
        parts = first[2:].split("|")
        self.assertEqual(len(parts), 5)
        self.assertTrue(parts[0].endswith("おちんちん"))
        self.assertTrue(parts[1].endswith("鴆ポ"))
        self.assertNotEqual(parts[2], "")
        self.assertTrue(parts[3].startswith("童貞"))
        self.assertTrue(parts[4].startswith("初物"))
        self.assertIn("F1\n", out)
        self.assertIn("A\nB\n", out)
        self.assertIn("AFTER1=1:", out)
        self.assertIn("AFTER2=1:", out)
        self.assertIn("F2\nC\nD\nAFTER3=1:", out)
        self.assertIn("MOVE=6:13:20:1\n", out)

    def test_era_megaten_stain_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
TARGET = 2
CALL SET_STAIN("口","精液",0)
CALL SET_STAIN("口","陰茎",0)
PRINTFORML S={GET_STAIN("口","精液",0)}:{GET_STAIN("口","陰茎",0)}:{STAIN:0:0}
PRINTFORML D={DIRTY("口",0)}:{RESULT}:{RESULT:1}
CALL SET_STAIN("陰茎","陰茎",0)
PRINTFORML X={DIRTY("陰茎",0)}:{RESULT:1}
CALL SET_STAIN("肛門","肛門",1)
PRINTFORML A={DIRTY("肛門",1)}:{RESULT:1}
CALL SET_STAIN("手","粘液",1)
CALL MOVE_STAIN("口",0,"手",1)
PRINTFORML M={GET_STAIN("口","粘液",0)}:{GET_STAIN("手","精液",1)}:{STAIN:0:0}:{STAIN:1:1}
CALL SET_STAIN("胸","破瓜の血")
PRINTFORML T={DIRTY("胸")}:{RESULT:1}:{GET_STAIN("胸","破瓜の血",2)}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=500)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "S=1:1:6\n"
            "D=2:2:6\n"
            "X=0:0\n"
            "A=0:0\n"
            "M=1:1:38:38\n"
            "T=1:64:1\n",
        )

    def test_era_megaten_coefficient_numbering_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
STR:0 = "人間"
STR:1 = "机器"
STR:2 = "造魔"
STR:10 = "待機中、――"
STR:11 = "採掘、矿山"
STR:20 = "Persona資料／装備状態"
STR:21 = "Persona資料／NO"
STR:22 = "Persona資料／LV"
STR:30 = "真言／喰奴"
STR:31 = "真言／魔獣"
PALAM:0:7 = 30000
PRINTFORML C={COEFFICIENT_EXP(42)}:{COEFFICIENT_EXP(2)}:{COEFFICIENT_EXP(13)}:{COEFFICIENT_EXP(99)}:{COEFFICIENT_MAG(11)}:{COEFFICIENT_MAG(99)}:{COEFFICIENT_MONEY(43)}:{COEFFICIENT_MONEY(99)}
PRINTFORML D={DIVERGENCE(1,11,22,33)}:{DIVERGENCE(2,11,22,33)}:{DIVERGENCE(0,11)}:{DIVERGENCE(20,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20)}:{EQUIPSKILLNUM()}
PRINTFORML P=%GET_DITEMTYPE(1)%:{GET_DITEMTYPE_NUM("LV")}:%GET_JOB(1)%:%GET_JOB_OMIT(1)%:%GET_MANTRA(2)%:{GET_MANTRA_NUM("魔獣")}
PRINTFORML R=%GET_RACE(2)%:{GET_RACE_NUM("造魔")}:%GET_傷害タイプ(1)%:%GET_傷害タイプ(9)%:{GET_傷害タイプ_NUM("魔法")}:{GET_傷害タイプ_NUM("不明")}
PRINTFORML A=%GET_攻撃タイプ(3)%:%GET_攻撃タイプ(7)%:{GET_攻撃タイプ_NUM("GUN")}:{GET_攻撃タイプ_NUM("道具")}:{GET_攻撃タイプ_NUM("割合傷害")}:{GET_攻撃タイプ_NUM("不明")}:{PALAMLV_F(0,7)}
NOWEX:0 = 16
NOWEX:1 = 8
NOWEX:2 = 4
NOWEX:3 = 0
PRINTFORML X={GET_EX("3重")}:{GET_EX("2重")}:{GET_EX("C")}:{GET_EX("V")}:{GET_EX("A")}:{GET_EX("B")}
NOWEX:3 = 8
PRINTFORML X4={GET_EX("四重")}:{GET_EX("三重")}:{GET_EX("C")}:{GET_EX("B")}
NOWEX:0 = 16
NOWEX:1 = 8
NOWEX:2 = 0
NOWEX:3 = 0
PRINTFORML X2={GET_EX("ニ重")}:{GET_EX("三重")}:{GET_EX("C")}:{GET_EX("V")}:{GET_EX("A")}
NOWEX:0 = 0
NOWEX:1 = 8
NOWEX:2 = 4
NOWEX:3 = 8
PRINTFORML X3={GET_EX("３重")}:{GET_EX("２重")}:{GET_EX("V")}:{GET_EX("A")}:{GET_EX("B")}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=500)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "C=30:20:6:14:220:128:14:8\n"
            "D=11:22:0:0:21\n"
            "P=NO:2:採掘:矿山:魔獣:2\n"
            "R=造魔:2:物理:不正な引数が与えられました:2:-1\n"
            "A=銃:不正な引数が与えられました:3:4:5:-1:5\n"
            "X=1:0:4:2:1:0\n"
            "X4=1:0:2:1\n"
            "X2=1:0:8:4:0\n"
            "X3=1:0:2:1:2\n",
        )

    def test_era_megaten_species_mp_plugin_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "ABL.csv").write_text("10,種族\n", encoding="utf-8")
        (root / "CSV" / "CSTR.csv").write_text("20,種族名\n", encoding="utf-8")
        (root / "CSV" / "BASE.csv").write_text("1,ＭＰ\n", encoding="utf-8")
        (root / "CSV" / "Item.csv").write_text("8000,プラグイン／銀之手\n", encoding="utf-8")
        (root / "CSV" / "Chara10.csv").write_text(
            "番号,10\n名前,A\n呼び名,A\n能力,種族,1\nＣ文字列,20,独自種族\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Chara11.csv").write_text(
            "番号,11\n名前,B\n呼び名,B\n能力,種族,2\n",
            encoding="utf-8",
        )
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
STR:1 = "人間"
STR:2 = "悪魔"
ADDCHARA 10
ADDCHARA 11
BASE:0:ＭＰ = 30
MAXBASE:0:ＭＰ = 120
MAXBASE:1:ＭＰ = 0
PRINTFORML R=%種族名(0)%:%種族名(1)%:%CSV種族名(10,0)%:%CSV種族名(11,0)%
PRINTFORML M={現MP割合(0)}:{現MP割合(1)}
PRINTFORML P=%PLUGINNAME(8000)%
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=500)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "R=独自種族:悪魔:独自種族:悪魔\n"
            "M=25:0\n"
            "P=銀之手\n",
        )

    def test_era_megaten_flag_com_event_and_oncerand_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "CFlag.csv").write_text("5,KOJO_FUNCTION使用\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text(
            "1,ダンジョン出現1\n"
            "2,ダンジョン出現2\n"
            "3,闘技場出現1\n"
            "4,闘技場出現2\n"
            "5,事件出現1\n"
            "6,事件出現2\n"
            "7,依頼出現1\n"
            "8,依頼出現2\n"
            "20,行動順1\n"
            "21,行動順2\n",
            encoding="utf-8",
        )
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDVOIDCHARA
TARGET = 0
CFLAG:0:KOJO_FUNCTION使用 = 200
FLAG:行動順1 = 77
FLAG:行動順2 = 88
PRINTFORML I={INI(1)}:{INI(2)}
PRINTFORML C0={GET_COMFLAG(5,0,0)}:{GET_COMFLAG(5,1,0)}:{GET_COMFLAG(5,0,0)}:{GET_COMFLAG(5,-1,0)}:{GET_COMFLAG(5,0,0)}
CALL SET_COMFLAG(66,0,0)
PRINTFORML C1={GET_COMFLAG(66,0,0)}:{CFLAG:0:201}
PRINTFORML E0={GET_EVENTFLAG(5,0,0)}:{GET_EVENTFLAG(5,1,0)}:{GET_EVENTFLAG(5,0,0)}:{GET_EVENTFLAG(5,-1,0)}:{GET_EVENTFLAG(5,0,0)}
FLAG:ダンジョン出現1 = SETBIT(0,5)
FLAG:ダンジョン出現2 = SETBIT(0,6)
FLAG:闘技場出現1 = SETBIT(0,7)
FLAG:事件出現2 = SETBIT(0,8)
FLAG:依頼出現1 = SETBIT(0,9)
CALL FLAG_RESET, 5, 0
CALL FLAG_RESET, 70, 0
CALL FLAG_RESET, 7, 1
CALL FLAG_RESET, 72, 2
CALL FLAG_RESET, 9, 3
PRINTFORML F={GETBIT(FLAG:ダンジョン出現1,5)}:{GETBIT(FLAG:ダンジョン出現2,6)}:{GETBIT(FLAG:闘技場出現1,7)}:{GETBIT(FLAG:事件出現2,8)}:{GETBIT(FLAG:依頼出現1,9)}
SELECTCOM:1 = 111
SELECTCOM:2 = 222
CALL SET_NEXTTRAIN, 33, 2
PRINTFORML N={SELECTCOM:1}:{SELECTCOM:2}:{SELECTCOM:3}
LOCAL:9 = ONCERAND(4,0,2)
LOCAL = 0
LOCAL:1 = ONCERAND(4,0)
LOCAL = SETBIT(LOCAL, LOCAL:1 - 1)
LOCAL:2 = ONCERAND(4,0)
LOCAL = SETBIT(LOCAL, LOCAL:2 - 1)
LOCAL:3 = ONCERAND(4,0)
LOCAL = SETBIT(LOCAL, LOCAL:3 - 1)
LOCAL:4 = ONCERAND(4,0)
LOCAL = SETBIT(LOCAL, LOCAL:4 - 1)
PRINTFORML O={LOCAL:9}:{GETBIT(LOCAL,0)}:{GETBIT(LOCAL,1)}:{GETBIT(LOCAL,2)}:{GETBIT(LOCAL,3)}:{ONCERAND(4,0)}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=800)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "I=77:88\n"
            "C0=0:0:1:1:0\n"
            "C1=1:8\n"
            "E0=0:0:1:1:0\n"
            "F=0:0:0:0:0\n"
            "N=20033:111:222\n"
            "O=0:1:1:1:1:0\n",
        )

    def test_era_megaten_event_pair_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Base.csv").write_text("0,忠誠度\n", encoding="utf-8")
        (root / "CSV" / "Abl.csv").write_text(
            "0,百合属性\n1,百合中毒\n2,ＢＬ属性\n3,ＢＬ中毒\n",
            encoding="utf-8",
        )
        (root / "CSV" / "CFlag.csv").write_text(
            "10,キャラ固有の番号\n"
            "11,戦闘参加不可能\n"
            "12,この場に居ないフラグ\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Cdflag.csv").write_text("0,キャラ間好感度\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text("0,偽娘ＢＬ設定\n", encoding="utf-8")
        (root / "CSV" / "Talent.csv").write_text(
            "0,男性\n1,偽娘\n2,兩面通吃\n3,討厭女人\n4,討厭男人\n"
            "5,非戦闘員\n6,妻\n7,夫\n8,淫魔\n9,玩具\n10,盟友\n"
            "11,親愛\n12,娼婦\n13,隷属\n14,相棒\n"
            "15,恋慕\n16,淫乱\n17,服従\n18,信頼\n19,ＮＴＲ\n",
            encoding="utf-8",
        )
        for no in (10, 20, 30):
            (root / "CSV" / f"Chara{no}.csv").write_text(f"番号,{no}\n名前,C{no}\n呼び名,C{no}\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDCHARA 10
ADDCHARA 20
ADDCHARA 30
CFLAG:0:キャラ固有の番号 = 10
CFLAG:1:キャラ固有の番号 = 20
CFLAG:2:キャラ固有の番号 = 30
TALENT:0:男性 = 1
TALENT:2:非戦闘員 = 1
BASE:0:忠誠度 = 7
BASE:1:忠誠度 = 3
CDFLAG:0:キャラ間好感度:120 = 5
CDFLAG:1:キャラ間好感度:110 = -2
PRINTFORML E={キャラ存在確かめ(10)}:{キャラ存在確かめ(20)}:{キャラ存在確かめ(30)}:{キャラ存在確かめ(99)}
PRINTFORML B={キャラ絆確かめ(10,20)}:{EVENT_10_2人掛け合いチェック(10,20,80)}:{EVENT_10_2人掛け合いチェック(10,20,81)}
CFLAG:1:この場に居ないフラグ = 1
PRINTFORML E2={EVENT_10_2人掛け合いチェック(10,20,1)}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=500)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "E=1:1:0:0\n"
            "B=80:1:0\n"
            "E2=0\n",
        )

    def test_era_megaten_datetime_autosplit_and_truth_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "CStr.csv").write_text("50,配偶者\n", encoding="utf-8")
        (root / "CSV" / "Chara10.csv").write_text("番号,10\n名前,Alice\n呼び名,A\n", encoding="utf-8")
        (root / "CSV" / "Chara20.csv").write_text("番号,20\n名前,Bob\n呼び名,B\n", encoding="utf-8")
        (root / "CSV" / "Chara30.csv").write_text("番号,30\n名前,Carol\n呼び名,C\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
PRINTFORML W=%WEEKDAY(0)%%WEEKDAY(1)%%WEEKDAY(2)%%WEEKDAY(3)%%WEEKDAY(4)%%WEEKDAY(5)%%WEEKDAY(6)%%WEEKDAY(7)%
PRINTFORML A=%AUTO_SPLIT("A_B_C","_",1)%:%AUTO_SPLIT("再利用","_",2)%:%AUTO_SPLIT("A_B_C","_",0,"B")%:%AUTO_SPLIT("再利用","_",1,"B")%:{AUTO_SPLIT_NUM("A_B_C","_","B")}:{AUTO_SPLIT_INT("4_5","_",1)}
PRINTFORML D={ONCEDAY("EV",0,0)}:{ONCEDAY("EV",0,0)}:{ONCEDAY("EV2",1,0)}:%SAVESTR:0%
PRINTFORML D2={ONCEDAY("EV",0,1)}:%SAVESTR:0%
PRINTFORML T={ONCETURN("TR",0,0)}:{ONCETURN("TR",0,0)}:{ONCETURN("TR2",1,0)}:%SAVESTR:10%
TIME = 1
CALL EVENTTURNEND
PRINTFORML R1=%SAVESTR:10%:%SAVESTR:0%
TIME = 0
CALL EVENTTURNEND
PRINTFORML R2=%SAVESTR:10%:%SAVESTR:0%
ADDVOIDCHARA
ADDVOIDCHARA
ADDVOIDCHARA
TARGET = 1
MASTER = 0
ASSI = 2
PRINTFORML P={ONCEPLAY(3,0,0,0,2)}:{ONCEPLAY(3,0,0,0,0)}:{ONCEPLAY(3,0,0,0,0)}:{ONCEPLAY(3,0,0,1,0)}
PRINTFORML P2={ONCEPLAY(3,0,0,0,1)}:{ONCEPLAY(3,0,0,1,0)}:{ONCEPLAY(3,0,0,0,0)}:{ONCEPLAY(4,1,0,0,0)}:{ONCEPLAY(4,1,0,0,0)}:{ONCEPLAY(5,2,0,0,0)}:{ONCEPLAY(64,0,0,0,0)}:{ONCEPLAY(1,3,99,0,0)}
NOITEM = 0
PRINTFORML I={EXIST_ITEM(10)}
ITEM:10 = 2
PRINTFORML I2={EXIST_ITEM(10)}
ITEM:10 = 0
NOITEM = 1
PRINTFORML I3={EXIST_ITEM(10)}
DELALLCHARA
ADDCHARA 10
ADDCHARA 20
ADDCHARA 30
CSTR:0:配偶者 = "Bob_X"
PRINTFORML S={CSV配偶者(0,1)}:{CSV配偶者(0,2)}:{CSV配偶者(0,-1)}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=1000)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "W=日月火水木金土？\n"
            "A=B:C:B:C:1:5\n"
            "D=1:0:1:/EV/\n"
            "D2=0:/\n"
            "T=1:0:1:/TR/\n"
            "R1=/:/\n"
            "R2=/:/\n"
            "P=0:1:0:0\n"
            "P2=0:1:1:1:0:1:0:0\n"
            "I=0\n"
            "I2=1\n"
            "I3=1\n"
            "S=1:0:0\n",
        )

    def test_era_megaten_koujou_event_and_train_bit_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDVOIDCHARA
ADDVOIDCHARA
TARGET = 0
PRINTFORML K={EVENT_KEYWORD("調教中事件")}:{EVENT_KEYWORD("射精")}:{EVENT_KEYWORD("探索中セックス")}:{EVENT_KEYWORD("不明")}
CALL EVENT_SETBIT, "調教中事件", "射精"
PRINTFORML E1={EVENT_GETBIT("調教中事件","射精")}:{EVENT_GETBIT("調教中事件","噴乳")}:{EVENT_GETBIT("調教中事件")}:{CFLAG:0:245}
CALL EVENT_SETBIT, 0, "調教中事件", "噴乳"
PRINTFORML E2={EVENT_GETBIT(0,"調教中事件","噴乳")}:{EVENT_GETBIT(0,"調教中事件")}
CALL EVENT_CLEARBIT, "調教中事件", "射精"
CALL EVENT_INVERTBIT, "調教中事件", "放尿"
PRINTFORML E3={EVENT_GETBIT("調教中事件","射精")}:{EVENT_GETBIT("調教中事件","噴乳")}:{EVENT_GETBIT("調教中事件","放尿")}:{EVENT_GETBIT("調教中事件")}
SELECTCOM = 70
CALL TRAIN_SETBIT, 5
PRINTFORML T1={TRAIN_GETBIT()}:{TRAIN_GETBIT(70)}:{GETBIT(CFLAG:0:303,6)}:{GETBIT(CFLAG:0:304,6)}:{GETBIT(CFLAG:0:305,6)}
CALL TRAIN_SETBIT, 1, 130, 3
PRINTFORML T2={TRAIN_GETBIT(1,130)}:{GETBIT(CFLAG:1:306,2)}:{GETBIT(CFLAG:1:307,2)}:{GETBIT(CFLAG:1:308,2)}
CALL TRAIN_SETBIT, 130, 0
PRINTFORML T3={TRAIN_GETBIT(130)}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=500)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "K=245:1:4:-1\n"
            "E1=1:0:2:2\n"
            "E2=1:6\n"
            "E3=0:1:1:12\n"
            "T1=5:5:1:0:1\n"
            "T2=3:1:1:0\n"
            "T3=0\n",
        )

    def test_era_megaten_abl_bust_and_skill_search_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Base.csv").write_text("0,剣撃\n1,火炎\n2,氷結\n50,良好\n51,麻痺\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text("0,技能数\n1,異能者技能数\n", encoding="utf-8")
        (root / "CSV" / "CFlag.csv").write_text("0,PTフラグ\n1,ボスフラグ\n2,リンク悪魔\n3,悪魔変身\n", encoding="utf-8")
        (root / "CSV" / "Talent.csv").write_text(
            "0,Aion式召喚術\n1,Persona使\n2,異能者\n3,達人\n4,人修羅\n"
            "10,絶壁\n11,貧乳\n12,巨乳\n13,爆乳\n14,魔乳\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Abl.csv").write_text(
            "0,技能1\n1,技能2\n2,技能3\n30,装備技能1\n31,装備技能2\n",
            encoding="utf-8",
        )
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDVOIDCHARA
ADDVOIDCHARA
PRINTFORML B={BUST(0)}
TALENT:0:絶壁 = 1
TALENT:1:巨乳 = 1
PRINTFORML B2={BUST(0)}:{BUST(1)}
FLAG:技能数 = 3
FLAG:異能者技能数 = 1
ABL:0:技能1 = 101
ABL:0:技能2 = 102
ABL:0:技能3 = 103
ABL:0:装備技能1 = 201
PRINTFORML S1={CHECK_SKILL_SEARCH(0,"火炎","全体","攻撃","MAGIC")}:{CHECK_SKILL_SEARCH(0,"火炎","単体","攻撃","MAGIC")}:{CHECK_SKILL_SEARCH(0,"氷結","単体","回復","EXTRA")}:{HAVE_SKILL_SEARCH(0,"氷結","単体","回復","EXTRA")}
PRINTFORML S2={CHECK_SKILL_SEARCH2(0,"火炎","全体","攻撃","MAGIC","麻痺",50)}:{CHECK_SKILL_SEARCH2(0,"火炎","全体","攻撃","MAGIC","麻痺",51)}:{CHECK_SKILL_SEARCH2(0,"火炎","全体","攻撃","MAGIC","麻痺",-51)}
PRINTFORML S3={HAVE_SKILL_SEARCH2(0,"氷結","単体","回復","EXTRA","良好",10,4001)}:{HAVE_SKILL_SEARCH2(0,"氷結","単体","回復","EXTRA","良好",10,4002)}:{_SKILL_CHECK(0,"火炎","全体","攻撃","MAGIC",-1,101)}:{_SKILL_CHECK2(0,"火炎","全体","攻撃","MAGIC","麻痺",50,-1,101)}
RETURN

@CHECK_ACTIONABLE(ARG, ARG:1)
#FUNCTION
SIF ARG:1 == 103
  RETURNF 0
RETURNF 1

@SKILL_DECIDE_TYPE_101()
#FUNCTION
RETURNF 2
@SKILL_TYPE_101(ARG)
#FUNCTION
RETURNF 1
@SKILL_SPHERE_101()
#FUNCTION
RETURNF 3
@SKILL_EFECT_101()
#FUNCTION
RETURNF 1
@SKILL_ADDTIONAL_STATE_101(ARG)
#FUNCTION
RETURNF 1
@SKILL_POWER_101(ARG)
#FUNCTION
RETURNF 50
@SKILL_MAXATTACKNUMBER_101(ARG)
#FUNCTION
RETURNF 2
@SKILL_MAXATK_PER_101(ARG)
#FUNCTION
RETURNF -1
@SKILL_MINATTACKNUMBER_101(ARG)
#FUNCTION
RETURNF 0

@SKILL_DECIDE_TYPE_102()
#FUNCTION
RETURNF 2
@SKILL_TYPE_102(ARG)
#FUNCTION
RETURNF 2
@SKILL_SPHERE_102()
#FUNCTION
RETURNF 1
@SKILL_EFECT_102()
#FUNCTION
RETURNF 2

@SKILL_DECIDE_TYPE_201()
#FUNCTION
RETURNF 1
@SKILL_TYPE_201(ARG)
#FUNCTION
RETURNF 2
@SKILL_SPHERE_201()
#FUNCTION
RETURNF 1
@SKILL_EFECT_201()
#FUNCTION
RETURNF 2
@SKILL_ADDTIONAL_STATE_201(ARG)
#FUNCTION
RETURNF 0
@SKILL_POWER_201(ARG)
#FUNCTION
RETURNF 12
@SKILL_MAXATTACKNUMBER_201(ARG)
#FUNCTION
RETURNF 1
@SKILL_MAXATK_PER_201(ARG)
#FUNCTION
RETURNF -1
@SKILL_MINATTACKNUMBER_201(ARG)
#FUNCTION
RETURNF 0
@SKILL_SPECIAL_ACTIONABLE_4001(ARG)
#FUNCTION
RETURNF 1
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=1200)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "B=3\n"
            "B2=1:4\n"
            "S1=1:0:0:1\n"
            "S2=1:0:1\n"
            "S3=1:0:1:1\n",
        )

    def test_era_megaten_talent_and_training_use_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Talent.csv").write_text(
            "3,恋慕\n4,淫乱\n5,服従\n6,親愛\n7,娼婦\n8,隷属\n"
            "20,淫魔\n21,玩具\n22,盟友\n23,相棒\n24,信頼\n25,ＮＴＲ\n"
            "30,貞操観念\n31,不在乎貞操\n"
            "82,討厭男人\n88,討厭女人\n139,FUTA\n157,偽娘\n170,妻\n171,夫\n"
            "185,男性\n186,女性\n187,中性\n188,雄性\n189,雌性\n190,双性\n191,無性\n"
            "200,陥落履歴(親愛)\n213,獣\n214,鳥\n215,爬虫類\n216,不定形\n217,魚\n"
            "222,異能者\n223,Persona使\n224,喰奴\n225,悪魔変身\n229,達人\n230,悪魔憑依\n242,Aion式召喚術\n250,召喚師\n"
            "100,体型嬌小\n113,高大\n114,巨人\n142,小人体型\n"
            "301,頭\n302,目\n303,口\n304,腕\n305,鉤爪\n306,羽\n307,足\n308,鉤足\n309,尾\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Abl.csv").write_text("80,種族\n", encoding="utf-8")
        (root / "CSV" / "Base.csv").write_text("60,良好\n71,麻痺\n75,瀕死\n", encoding="utf-8")
        (root / "CSV" / "CFlag.csv").write_text("0,陥落キャラ\n1,キャラ固有の番号\n2,ステート\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text(
            "1,ポジション1\n2,ポジション2\n3,ポジション3\n4,ポジション4\n5,ポジション5\n6,ポジション6\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Exp.csv").write_text("5,愛情経験\n6,ＴＳ経験\n", encoding="utf-8")
        (root / "CSV" / "Item.csv").write_text("12,可穿戴式陽具\n", encoding="utf-8")
        (root / "CSV" / "Tequip.csv").write_text(
            "95,乳房露出\n96,乳首露出\n98,陰唇露出\n99,臀部露出\n"
            "102,Ｃ触覚\n103,Ｖ触覚\n104,Ａ触覚\n105,乳房触覚\n106,乳首触覚\n"
            "108,胸構造\n112,Ｖ不可\n113,Ａ不可\n134,Vずらし中\n",
            encoding="utf-8",
        )
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDVOIDCHARA
ADDVOIDCHARA
ADDVOIDCHARA
MASTER = 0
PLAYER = 0
TARGET = 1
TALENT:0:男性 = 1
TALENT:0:恋慕 = 1
TALENT:0:貞操観念 = 1
TALENT:0:小人体型 = 1
TALENT:0:異能者 = 1
TALENT:0:討厭女人 = 1
TALENT:0:頭 = 1
TALENT:0:目 = 1
TALENT:0:口 = 1
TALENT:0:腕 = 1
TALENT:0:足 = 1
TALENT:0:羽 = 1
TALENT:0:尾 = 1
TALENT:1:女性 = 1
TALENT:1:淫乱 = 1
TALENT:1:服従 = 1
TALENT:1:妻 = 1
TALENT:1:高大 = 1
TALENT:1:召喚師 = 5
TALENT:1:討厭男人 = 1
TALENT:1:獣 = 1
TALENT:1:頭 = 1
TALENT:1:目 = 1
TALENT:1:口 = 1
TALENT:1:腕 = 1
TALENT:1:足 = 1
TALENT:1:羽 = 1
TALENT:1:尾 = 1
TALENT:2:達人 = 1
CFLAG:0:キャラ固有の番号 = 10
CFLAG:1:キャラ固有の番号 = 11
FLAG:ポジション1 = 1
FLAG:ポジション2 = -1
FLAG:ポジション3 = -1
FLAG:ポジション4 = -1
FLAG:ポジション5 = -1
FLAG:ポジション6 = -1
EXP:1:ＴＳ経験 = 3
TEQUIP:0:乳房露出 = -1
TEQUIP:0:乳首露出 = -1
TEQUIP:0:陰唇露出 = -1
TEQUIP:0:臀部露出 = -1
TEQUIP:1:乳房露出 = -1
TEQUIP:1:乳首露出 = -1
TEQUIP:1:陰唇露出 = -1
TEQUIP:1:臀部露出 = -1
ITEM:可穿戴式陽具 = 1
TCVAR:1:5 = 77
PRINTFORML A={HAVE_PENIS(0)}:{HAVE_VAGINA(1)}:{HAVE_CLITORIS(1)}:{HAVE_TIT(1)}:{IS_HUMAN(0)}:{IS_BEAST(1)}:{IS_BITCHY(1)}:{IS_SLAVERY(1)}:{IS_ENGAGE(1)}:{XGENDER(1)}
PRINTFORML H={HATE(1,0)}:{RESULT:1}:{HATE(0,1)}:{RESULT:1}:{IS_LOVER(0)}
PRINTFORML U1={USE_MOUTH(0)}:{USE_HAND(0)}:{USE_FOOT(0)}:{USE_TAIL(0)}:{USE_EYE(0)}:{USE_HEAD(0)}:{USE_WING(0)}
PRINTFORML U2={USE_VAGINA(1)}:{USE_CLI(1)}:{USE_NIPLE(1)}:{USE_BREAST(1)}:{USE_ANUS(1)}:{USE_PBAND(1)}:{USE_PENIS(0)}
PRINTFORML T={GET_调和者出力()}:{体格(0)}:{体格(1)}:{体格差(0,1)}:{初期性別参照(1)}:{純異能者チェック(0)}:{純達人チェック(2)}:{貞操(0,0)}
TEQUIP:1:13 = 1
TEQUIP:0:20 = 1
PRINTFORML B={ITEM_VAGINA(1)}:{USE_VAGINA(1)}:{USE_MOUTH(0)}
PRINTFORML E={GET_ADD_EXP(5,0)}:{GETS_ADD_EXP("愛情経験",0,1)}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=500)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "A=1:1:1:1:1:1:1:1:1:1\n"
            "H=1:82:1:88:1\n"
            "U1=1:1:1:1:1:1:1\n"
            "U2=1:1:1:1:1:1:1\n"
            "T=30:-10:1:11:1:1:1:2\n"
            "B=1:0:0\n"
            "E=77:77\n",
        )

    def test_era_megaten_cflag_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "CFlag.csv").write_text(
            "0,行動順\n1,ポジション\n2,ボスフラグ\n3,この場に居ないフラグ\n"
            "4,労役フラグ\n5,売却可能\n6,売却不可フラグ\n7,KOJO_FUNCTION使用\n"
            "8,キャラ固有の番号\n100,キャラ相性1\n101,キャラ相性値1\n",
            encoding="utf-8",
        )
        (root / "CSV" / "CStr.csv").write_text(
            "0,相性グループ\n1,相性_最高\n2,相性_抜群\n3,相性_良好\n4,相性_不良\n5,相性_最悪\n6,配偶者\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Abl.csv").write_text("0,属性LD\n1,属性LC\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text("0,出産機能ONOFF\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDVOIDCHARA
ADDVOIDCHARA
ADDVOIDCHARA
MASTER = 0
PLAYER = 0
TARGET = 1
CFLAG:0:キャラ固有の番号 = 10
CFLAG:1:キャラ固有の番号 = 11
CFLAG:1:行動順 = 12
CFLAG:1:ポジション = 7
CFLAG:1:売却可能 = 1
CFLAG:2:売却可能 = 1
CFLAG:2:ボスフラグ = 1
CSTR:0:相性グループ = "犬_猫"
CSTR:1:相性グループ = "猫"
CSTR:1:相性_最高 = "犬"
ABL:1:属性LD = 1
ABL:0:属性LD = 3
ABL:1:属性LC = 2
ABL:0:属性LC = 2
PRINTFORML C={CINI(1)}:{CPOS(1)}
PRINTFORML S={GET_CHARASELLABLE(1)}:{GET_CHARASELLABLE(2)}:{GET_CHARASELLABLE(0)}
PRINTFORML G={IS_RELATION_GROUP(0,"犬")}:{GET_RELATION_GROUP(1,0,"猫")}
PRINTFORML R={GET_RELATION(1,0,1)}:{GET_RELATION(1,0)}
CFLAG:1:キャラ相性1 = 10
CFLAG:1:キャラ相性値1 = 77
PRINTFORML R2={GET_RELATION(1,0,1)}
TFLAG:24 = 23
PRINTFORML M={GET_MARK_WAY(0,1)}
CFLAG:1:1501 = 9
TCVAR:1:111 = 3
PRINTFORML V={VIDEO_COM_INCLUDE_CFLAG(1,999)}:{VIDEO_COM_INCLUDE_TCVAR(1,999)}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=800)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "C=12:7\n"
            "S=1:0:0\n"
            "G=1:150\n"
            "R=200:185\n"
            "R2=77\n"
            "M=23\n"
            "V=2:2\n",
        )

    def test_era_megaten_boolean_play_requirement_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Talent.csv").write_text(
            "0,処女\n82,討厭男人\n83,抖Ｓ\n88,討厭女人\n139,FUTA\n142,小人体型\n144,禁忌的知識\n"
            "185,男性\n186,女性\n200,汚臭無視\n201,汚臭敏感\n"
            "222,異能者\n223,Persona使\n229,達人\n240,人修羅\n242,Aion式召喚術\n"
            "301,頭\n302,目\n303,口\n304,腕\n306,羽\n307,足\n309,尾\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Abl.csv").write_text(
            "0,従順\n2,技巧\n11,百合属性\n60,技能1\n141,装備技能1\n",
            encoding="utf-8",
        )
        (root / "CSV" / "CFlag.csv").write_text(
            "0,PTフラグ\n1,ボスフラグ\n2,リンク悪魔\n3,悪魔変身\n4,物品使用能力\n"
            "5,キャラ固有の番号\n6,娘の父親の固有番号娘\n7,娘の産みの親の固有番号娘\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Flag.csv").write_text("0,技能数\n1,異能者技能数\n", encoding="utf-8")
        (root / "CSV" / "Exp.csv").write_text("10,Ａ拡張経験\n11,Ｖ拡張経験\n", encoding="utf-8")
        (root / "CSV" / "Equip.csv").write_text("0,胴\n", encoding="utf-8")
        (root / "CSV" / "Item.csv").write_text(
            "12,可穿戴式陽具\n100,高叉甲冑\n101,娼婦之服\n102,戦闘短装\n103,無敵納米裙\n"
            "104,尖刺文胸\n105,战斗吊带衫\n106,高开衩内衣\n200,消費物品\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Tequip.csv").write_text(
            "95,乳房露出\n96,乳首露出\n98,陰唇露出\n99,臀部露出\n"
            "102,Ｃ触覚\n103,Ｖ触覚\n104,Ａ触覚\n105,乳房触覚\n106,乳首触覚\n"
            "108,胸構造\n112,Ｖ不可\n113,Ａ不可\n134,Vずらし中\n",
            encoding="utf-8",
        )
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDVOIDCHARA
ADDVOIDCHARA
MASTER = 0
PLAYER = 0
ASSI = 1
TALENT:0:男性 = 1
TALENT:1:女性 = 1
TALENT:0:口 = 1
TALENT:1:口 = 1
TALENT:0:腕 = 1
TALENT:0:足 = 1
TALENT:0:頭 = 1
TALENT:0:目 = 1
TALENT:0:尾 = 1
TALENT:0:羽 = 1
TALENT:1:腕 = 1
TALENT:1:足 = 1
TALENT:1:頭 = 1
TALENT:1:目 = 1
TALENT:1:尾 = 1
TALENT:1:羽 = 1
TEQUIP:0:乳房露出 = -1
TEQUIP:0:乳首露出 = -1
TEQUIP:0:陰唇露出 = -1
TEQUIP:0:臀部露出 = -1
TEQUIP:1:乳房露出 = -1
TEQUIP:1:乳首露出 = -1
TEQUIP:1:陰唇露出 = -1
TEQUIP:1:臀部露出 = -1
EQUIP:0:胴 = 100
PRINTFORML ERO={IS_EROEQUIP_F(0)}:{IS_EROEQUIP_F(1)}
CFLAG:0:PTフラグ = 0
PRINTFORML I0={ITEM_USE_REQUIREMENT(0,999,200,5,2)}
CFLAG:0:PTフラグ = 1
FLAG:技能数 = 1
FLAG:異能者技能数 = 1
CFLAG:0:物品使用能力 = 5
ITEM:200 = 2
PRINTFORML I1={ITEM_USE_REQUIREMENT(0,999,200,5,2)}:{ITEM_USE_REQUIREMENT(0,999,200,6,2)}:{ITEM_USE_REQUIREMENT(0,999,200,5,3)}
PRINTFORML P={PLAY_KISS(0,1)}:{PLAY_FELLA(1,0)}:{PLAY_CUNNI(0,1)}:{PLAY_SEX(0,1)}:{PLAY_ANALSEX(0,1)}
TEQUIP:0:20 = 1
PRINTFORML P2={PLAY_KISS(0,1)}
CFLAG:0:キャラ固有の番号 = 10
CFLAG:1:キャラ固有の番号 = 11
CFLAG:0:娘の父親の固有番号娘 = 11
PRINTFORML K={近親チェック(0,1)}:%RESULTS%:%RESULTS:1%
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=1000)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "ERO=1:0\n"
            "I0=1\n"
            "I1=1:0:0\n"
            "P=1:1:1:1:1\n"
            "P2=0\n"
            "K=1:父:息子\n",
        )

    def test_era_megaten_input_function_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text("0,双选输入设定\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
FLAG:双选输入设定 = 2
PRINTFORML SM={INPUT_SELECT_M("[1] One/[22] Two","/","ログを残す/ボタンを利用する",2,1,"LEFT",20)}
PRINTFORML YM={INPUT_YN_M("Yes","No","/")}
PRINTFORML SD={INPUT_SELECT_D("[7] Seven")}
PRINTFORML YD={INPUT_YN_D()}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False, inputs=["22", "y", "7", "n"])
        rt.run("SYSTEM_TITLE", max_steps=500)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "[1] One　[22] Two\n"
            "SM=22\n"
            "[0] Yes/[1] No\n"
            "YM=0\n"
            "[7] Seven\n"
            "\n"
            "\n"
            "\n"
            "SD=7\n"
            "[0] はい/[1] いいえ\n"
            "YD=1\n",
        )

    def test_era_megaten_generic_input_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text("0,双选输入设定\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
FLAG:双选输入设定 = 2
PRINTFORML I={INPUTINT(1,2,3)}
PRINTFORML T={TINPUTINT(1000,-1,0,5,6)}
CALL INPUT_CHAR, "abc", 0
PRINTFORML C=%RESULTS%
CALL INPUT_MANY, 2, 9, "ログを残す", "99"
PRINTFORML M={RESULT}
CALL INPUT_SELECT, 11, "Eleven", 22, "TwentyTwo", 33, "Thirty"
PRINTFORML S={RESULT}
CALL INPUT_SPLIT, "Pick", "Alpha/Beta/Gamma", "/", "Cancel", 2, 0, 10, 1001, 0, 1003
PRINTFORML P={RESULT}:{RESULT:1}:%RESULTS%
PRINTFORML Y={INPUT_YN("Yes","No",2)}
CALL INPUT_ONEKEY_TAP, 0, "-", "_", "x_[X]_extra"
PRINTFORML K=%RESULTS%
RESULTS:0 = "z_[Z]_fromResults"
CALL INPUT_ONEKEY_TAP_RESULTS, 0, "-", "_"
PRINTFORML KR=%RESULTS%
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False, inputs=["2", "6", "b", "7", "22", "12", "n", "x", "z"])
        rt.run("SYSTEM_TITLE", max_steps=1000)
        self.assertEqual(rt.warnings, [])
        out = "".join(rt.output)
        self.assertIn("I=2\n", out)
        self.assertIn("T=6\n", out)
        self.assertIn("C=b\n", out)
        self.assertIn("M=7\n", out)
        self.assertIn("S=22\n", out)
        self.assertIn("P=12:0:Gamma\n", out)
        self.assertIn("Y=1\n", out)
        self.assertIn("K=x\n", out)
        self.assertIn("KR=z\n", out)

    def test_era_megaten_generic_split_string_and_truth_helpers(self):
        td, program = self.make_game('''@SYSTEM_TITLE
MASTER = 7
ASSI = -1
PRINTFORML S0={SUBPLAYER()}
ASSI = 3
ASSIPLAY = 0
PRINTFORML S1={SUBPLAYER()}
ASSIPLAY = 1
ABL:0:技能1 = 123
PRINTFORML S2={SUBPLAYER()}
PRINTFORML N=%INIS(2)%/%POSS(3)%/{SKILLNUM(0,1)}
PRINTFORML E={EQUALCHECK_TURN(0,0,5)}:{EQUALCHECK_TURN(7,1,7,7)}:{EQUALCHECK_STR("B","A","B","B")}:{EQUALCHECK_STR("","")}:{TRUECHECK(1,-1,2,0,3)}
PRINTFORML P=%ADD_SPLIT("A//C","/","B")%|%CHANGE_SPLIT("A/B/C","/",1,"X")%|%CHANGE_SPLIT("A/B/C","/",1,"B","X")%
PRINTFORML Q=%CALC_SPLIT("A/2/C","/",1,"+=","3")%|%CALC_SPLIT("LV/1/HP/5","/",1,"HP","+=","7")%|%SHIFT_SPLIT("A/B/C","/",1,"Z",0,3)%
RETURN
''')
        self.addCleanup(td.cleanup)
        root = program.root
        (root / "CSV" / "Abl.csv").write_text("10,技能1\n", encoding="utf-8")
        program = load_program(root)
        rt = EraRuntime(program, echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=300)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "S0=-2\n"
            "S1=3\n"
            "S2=7\n"
            "N=行動順2/ポジション3/123\n"
            "E=1:2:2:0:3\n"
            "P=A/B/C|A/X/C|A/B/X\n"
            "Q=A/+5/C|LV/1/HP/+12|Z/A/B/C\n",
        )

    def test_era_megaten_tequip_clothes_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        tequip_names = [
            (80, "帽子"), (81, "服"), (82, "下衣"), (83, "全身服"), (84, "手"),
            (85, "内衣（上）"), (86, "内衣（下）"), (87, "全身内衣"), (88, "襪子"),
            (89, "靴"), (90, "外衣"), (91, "其他"), (92, "腕露出"), (93, "足露出"),
            (94, "脚露出"), (95, "乳房露出"), (96, "乳首露出"), (97, "臍露出"),
            (98, "陰唇露出"), (99, "臀部露出"), (100, "陰唇可視"), (101, "臀部可視"),
            (102, "Ｃ触覚"), (103, "Ｖ触覚"), (104, "Ａ触覚"), (105, "乳房触覚"),
            (106, "乳首触覚"), (107, "打開胸前"), (108, "胸構造"), (109, "可以打開股間前"),
            (110, "打開股間前"), (111, "股間構造"), (112, "Ｖ不可"), (113, "Ａ不可"),
            (114, "被覆愛撫Ｃ"), (115, "服内部愛撫Ｃ"), (117, "被覆愛撫Ｖ"),
            (118, "服内部愛撫Ｖ"), (120, "被覆愛撫Ａ"), (121, "服内部愛撫Ａ"),
            (123, "被覆愛撫乳房"), (124, "服内部愛撫乳房"), (126, "被覆愛撫乳首"),
            (127, "服内部愛撫乳首"), (129, "裙子被向上巻起"), (132, "其他2"),
            (133, "其他3"), (134, "Vずらし中"),
        ]
        cflag_names = [(23, "着衣フラグ")]
        clothes = ["帽子", "服", "下衣", "全身服", "手", "内衣（上）", "内衣（下）", "全身内衣", "襪子", "靴", "外衣", "其他", "其他2", "其他3"]
        cflag_names += [(40 + i, name) for i, name in enumerate(clothes)]
        cflag_names += [(60 + i, "初期" + name) for i, name in enumerate(clothes)]
        (root / "CSV" / "Tequip.csv").write_text("".join(f"{i},{name}\n" for i, name in tequip_names), encoding="utf-8")
        (root / "CSV" / "Cflag.csv").write_text("".join(f"{i},{name}\n" for i, name in cflag_names), encoding="utf-8")
        (root / "CSV" / "Item.csv").write_text("6101,上衣\n6102,袴子\n6103,内衣上\n6104,内衣下\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
MASTER = 0
NO:0 = 0
CFLAG:0:61 = 101
CFLAG:0:62 = 102
CALL CLOTHES_INITIALIZE, 0
CALL SET_CLOTHES_EQUIP_ALL, 0
PRINTFORML N=%GET_CLOTHESNAME(1)%:{GET_CLOTHES("下衣")}:%NAME_EXPOSE(3)%:%CLOTHESNAMEF(0,1)%
CALL CHECK_EXPOSE, 0
PRINTFORML E={TEQUIP:0:乳房露出}:{TEQUIP:0:陰唇露出}
CALL おっぱいオープンチェック, 0
CALL 股間構造チェック, 0
CALL ずらしチェック, 0
CALL 触覚チェック, 0
PRINTFORML S={TEQUIP:0:胸構造}:{TEQUIP:0:打開胸前}:{TEQUIP:0:股間構造}:{TEQUIP:0:Ｖ不可}:{TEQUIP:0:Ｃ触覚}:{TEQUIP:0:服内部愛撫Ｃ}
CALL SET_CLOTHES_DROP_TOPS, 0
PRINTFORML D={TEQUIP:0:服}:{TEQUIP:0:下衣}
TEQUIP:0:内衣（上） = 103
TEQUIP:0:内衣（下） = 104
TEQUIP:0:全身内衣 = 105
CALL SET_CLOTHES_DROP_INNER, 0, 2
PRINTFORML I={TEQUIP:0:内衣（上）}:{TEQUIP:0:内衣（下）}:{TEQUIP:0:全身内衣}
RETURN

@CLOTHES_EXPOSE_0(ARG)
RETURN 1

@CLOTHES_BREAST_0
RETURN 0

@CLOTHES_CROTCH_0
RETURN 0

@CLOTHES_触覚_0(ARG)
RETURN 1

@CLOTHES_EXPOSE_101(ARG)
SELECTCASE ARG
CASE 4,5
RETURN 0
CASEELSE
RETURN 1
ENDSELECT

@CLOTHES_BREAST_101
RETURN 6

@CLOTHES_CROTCH_101
RETURN 0

@CLOTHES_触覚_101(ARG)
RETURN 1

@CLOTHES_EXPOSE_102(ARG)
SELECTCASE ARG
CASE 7,8
RETURN 0
CASEELSE
RETURN 1
ENDSELECT

@CLOTHES_BREAST_102
RETURN 0

@CLOTHES_CROTCH_102
RETURN 5

@CLOTHES_触覚_102(ARG)
SELECTCASE ARG
CASE 0 TO 2
RETURN 6
CASEELSE
RETURN 1
ENDSELECT
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=2000)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "N=服:2:乳房露出:上衣\n"
            "E=-1:2\n"
            "S=6:1:8:0:6:1\n"
            "D=0:102\n"
            "I=103:0:0\n",
        )

    def test_era_megaten_message_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text(
            "0,ＣＯＭＰ容量\n1,弱点カ拉\n2,通常カ拉\n3,耐性カ拉\n4,無効カ拉\n5,吸収カ拉\n6,反射カ拉\n",
            encoding="utf-8",
        )
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
FLAG:弱点カ拉 = 255000000
FLAG:通常カ拉 = 192192192
FLAG:耐性カ拉 = 100200030
FLAG:無効カ拉 = 0
FLAG:吸収カ拉 = 123456789
FLAG:反射カ拉 = 255255255
PRINTFORML C={GETCOLOR_9(123456789)}:{RESULT:1}:{RESULT:2}:{GETCOLOR_9(1,2,3)}:{TOSTR1000(1234567)}
CALL MESSAGE_BL, 2, "alpha", "beta"
CALL MESSAGE_B2, "Name", 1, "line"
CALL SET_AISYOU_COLOR, 200
PRINTFORML COLOR={GETCOLOR()}
CALL SHOW_AISYOU_COLOR_LIST, 5
PRINTL
FLAG:ＣＯＭＰ容量 = -1
CALL MESSAGE_COMP_OVER
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=500)
        self.assertEqual(rt.warnings, [])
        out = "".join(rt.output)
        self.assertIn("C=123:456:789:1002003:1,234,567\n", out)
        self.assertIn("┃alpha", out)
        self.assertIn("┃beta", out)
        self.assertIn("┓＠Name┏", out)
        self.assertIn("┃line", out)
        self.assertIn("COLOR=16711680\n", out)
        self.assertIn("■REFLECT", out)
        self.assertIn("＞一時的にCOMP容量が最大値をオーバーしました。\n", out)

    def test_era_megaten_generic_message_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Cstr.csv").write_text("11,一人称\n12,二人称\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
MASTER = 0
PLAYER = 1
TARGET = 0
ASSI = 2
CALLNAME:0 = "あなた"
CALLNAME:1 = "Player"
CALLNAME:2 = "Assistant"
NAME:0 = "TargName"
CSTR:0:一人称 = "私"
CSTR:0:二人称 = "君"
PRINTFORML A=%ANATANAME("貴方","様")%:%TOALIGNMENT("A",4,"CENTER")%:%TOSTR_HTML(255)%:{BTL_COLOR_TABLE_NUM()}
CALLNAME:0 = "Master"
PRINTFORML A2=%ANATANAME("貴方","様")%
PRINTFORML B={BARCOLORSET("赤")}:{RESULT:1}
CALL PRINT_COLOR, "red", COLOR("赤"), "L"
CALL COLORDRAWLINE, "=", COLOR("青")
CALL PRINT_COLORBAR, 3, 10, 5, "#", ".", COLOR("赤"), COLOR("黒")
PRINTL
CALL PRINT_EIGHT_BAR, 9, 4
PRINTL
CALL PRINTFORM_LF, "x" + UNICODE(13) + "y", "L"
PRINTFORML F=%PRINT_STR_F("CALLNAME:TARGET_一人称_H_WH_BH_名前")%
CALL PRINT_STRL, "CALLNAME:TARGET_/_一人称_/_H_/_BUTTON_7_/_NOBUTTON_[9]_-_strike"
PRINTFORML R={RESULT}
CALL HEARTMARK
CALL WHITE_HEARTMARK
CALL BIG_HEARTMARK
PRINTL
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=500)
        self.assertEqual(rt.warnings, [])
        out = "".join(rt.output)
        self.assertIn("A=貴方: A  :#000000FF:17\n", out)
        self.assertIn("A2=Master様\n", out)
        self.assertIn("B=12611696:5251104\n", out)
        self.assertIn("red\n", out)
        self.assertIn("=" * 72 + "\n", out)
        self.assertIn("#....\n", out)
        self.assertIn("█▏  \n", out)
        self.assertIn("x\ry\n\n", out)
        self.assertIn("F=Master私♥♡❤TargName\n", out)
        self.assertIn("Master/私/♥/7/[9]strike\n", out)
        self.assertIn("R=0\n♥♡❤\n", out)

    def test_era_megaten_heart_mark_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
PRINTFORML F=[%ハート(2)%][%ハートＢ(3)%]
RESULT = 42
CALL HEART, 2
CALL HEARTB, 2
CALL HEARTW, 1, "", "!", 0
PRINTFORML R={RESULT}
SETCOLOR 1
CALL HEARTD, 1
PRINTFORML C={GETCOLOR()}
CALL HEARTDW, 1, "", "@", 1
CALL HEARTDBW, 1, "", "", 0
PRINTFORML END={RESULT}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False)
        rt.run("SYSTEM_TITLE", max_steps=500)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "F=[♡♡][♥♥♥]\n"
            "♡♡♥♥♡!\n"
            "R=42\n"
            "♡C=1\n"
            "♡@\n"
            "♥\n"
            "END=42\n",
        )

    def test_era_megaten_message_window_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
PRINTFORML A=%NOWALIGNMENT()%:%PREVALIGNMENT()%
CALL SET_ALIGNMENT, "CENTER"
PRINTFORML B=%NOWALIGNMENT()%:%PREVALIGNMENT()%
CALL MESSAGE_WINDOW, "Alice", "hello/world", "/", "ログを残す/ボタンを利用しない", "LEFT", 20, -1, "TYPE", 10, -1, "CENTER"
PRINTFORML R={RESULT}
CALL MESSAGE_WINDOW_LOG, "", "", "/", 0, 20, 1
CALL MESSAGE_WINDOW, "", "again", "/", "ログを残す/ボタンを利用しない/再利用する", "", 22, 0
CALL MESSAGE_WINDOW_D, "D", "x/y", "/", "デフォルト", "CENTER", 20, 1
CALL MESSAGE_WINDOW_CONFIG
PRINTFORML G={GLOBAL:メッセージ速度}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(
            load_program(root),
            echo=False,
            interactive=False,
            inputs=["advance", "close-log", "advance", "advance", "0", "5", "9"],
        )
        rt.run("SYSTEM_TITLE", max_steps=1000)
        self.assertEqual(rt.warnings, [])
        out = "".join(rt.output)
        self.assertIn("A=LEFT:LEFT\n", out)
        self.assertIn("B=CENTER:LEFT\n", out)
        self.assertIn("┌┤Alice ├", out)
        self.assertIn("│       hello        │\n", out)
        self.assertIn("│       world        │\n", out)
        self.assertIn("R=1\n", out)
        self.assertIn("│hello", out)
        self.assertIn("│world", out)
        self.assertIn("│again", out)
        self.assertNotIn("┌┤D ├", out)
        self.assertNotIn("[0] メッセージ速度\n", out)
        self.assertIn("G=5\n", out)

    def test_era_megaten_message_window_default_clears_visible_box_but_keeps_log(self):
        td, program = self.make_game('''@SYSTEM_TITLE
PRINTL before
CALL MESSAGE_WINDOW, "", "body", "/", "ログを残さない/ボタンを利用しない", "LEFT", 20
PRINTL after
INPUT
CALL MESSAGE_WINDOW_LOG, "", "", "/", 0, 20, 1
PRINTL done
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["advance", "seed"])
        rt.run("SYSTEM_TITLE", max_steps=200)
        out = "".join(rt.output)
        self.assertTrue(out.startswith("before\nafter\n"), out)
        self.assertNotIn("body", out.split("after\n", 1)[0])
        self.assertIn("│body", out)
        self.assertNotIn("done\n", out)
        self.assertTrue(rt.waiting_for_input)
        self.assertEqual(rt.queue_input("close"), "close")
        rt.continue_run(max_steps=100)
        out = "".join(rt.output)
        self.assertEqual(out, "before\nafter\ndone\n")
        self.assertIn("done\n", out)
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_message_window_waits_on_explicit_exhaustion_and_clears_on_resume(self):
        td, program = self.make_game('''@SYSTEM_TITLE
INPUT
PRINTL before
CALL MESSAGE_WINDOW, "", "body", "/", "ログを残さない/ボタンを利用しない", "LEFT", 20
PRINTL after
RETURN
''')
        self.addCleanup(td.cleanup)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["seed"])
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertTrue(rt.waiting_for_input)
        out = "".join(rt.output)
        self.assertIn("before\n", out)
        self.assertIn("│body", out)
        self.assertNotIn("after", out)

        rt.queue_input("advance")
        rt.continue_run(max_steps=100)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual("".join(rt.output), "before\nafter\n")
        self.assertEqual(rt.warnings, [])

    def test_noninteractive_message_window_controls_resume_log_config_and_auto(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Flag.csv").write_text(
            "0,オート送り\n1,ウィンドウメッセージスキップ\n",
            encoding="utf-8",
        )
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
CALL MESSAGE_WINDOW, "", "body", "/", "ログを残さない/ボタンを利用する", "LEFT", 20
PRINTFORML after1:{FLAG:オート送り}
CALL MESSAGE_WINDOW, "", "cfg", "/", "ログを残さない/ボタンを利用する", "LEFT", 20
PRINTFORML after2:{GLOBAL:メッセージ速度}
CALL MESSAGE_WINDOW, "", "auto", "/", "ログを残さない/ボタンを利用する", "LEFT", 20
PRINTFORML after3:{FLAG:オート送り}
RETURN
''', encoding="utf-8")
        program = load_program(root)
        rt = EraRuntime(program, echo=False, interactive=False, inputs=["+"])
        rt.run("SYSTEM_TITLE", max_steps=200)
        self.assertTrue(rt.waiting_for_input)
        self.assertGreaterEqual("".join(rt.output).count("│body"), 2)
        self.assertNotIn("after1", "".join(rt.output))

        rt.queue_input("close-log")
        rt.continue_run(max_steps=100)
        self.assertTrue(rt.waiting_for_input)
        self.assertIn("│body", "".join(rt.output))
        self.assertNotIn("after1", "".join(rt.output))

        rt.queue_input("advance")
        rt.continue_run(max_steps=100)
        self.assertTrue(rt.waiting_for_input)
        out = "".join(rt.output)
        self.assertIn("after1:0", out)
        self.assertIn("│cfg", out)

        rt.queue_input("/")
        rt.continue_run(max_steps=100)
        self.assertTrue(rt.waiting_for_input)
        self.assertIn("[0] メッセージ速度", "".join(rt.output))

        rt.queue_input("0")
        rt.queue_input("7")
        rt.queue_input("9")
        rt.continue_run(max_steps=500)
        self.assertTrue(rt.waiting_for_input)
        out = "".join(rt.output)
        self.assertIn("│cfg", out)
        self.assertNotIn("after2", out)

        rt.queue_input("advance")
        rt.continue_run(max_steps=100)
        self.assertTrue(rt.waiting_for_input)
        out = "".join(rt.output)
        self.assertIn("after2:7", out)
        self.assertIn("│auto", out)

        rt.queue_input("-")
        rt.continue_run(max_steps=100)
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual("".join(rt.output), "after1:0\nafter2:7\nafter3:1\n")
        self.assertEqual(rt.warnings, [])

    def test_era_megaten_character_operation_native_helpers(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Exp.csv").write_text("0,膣射経験\n1,調教経験\n2,ＴＳ経験\n", encoding="utf-8")
        (root / "CSV" / "Base.csv").write_text("0,LV\n1,力\n2,体力\n3,剣撃\n4,火炎\n5,瀕死\n", encoding="utf-8")
        (root / "CSV" / "Abl.csv").write_text("0,従順\n1,種族\n2,会話類型\n", encoding="utf-8")
        (root / "CSV" / "Talent.csv").write_text(
            "0,男性\n1,処女\n2,FUTA\n3,偽娘\n4,絶壁\n5,貧乳\n6,巨乳\n7,爆乳\n8,魔乳\n9,蓬莱人\n10,人修羅\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Cflag.csv").write_text(
            "20,イベント槽\n21,イベント槽2\n30,KOJO_FUNCTION使用\n40,力補正\n50,キャラ相性値1\n51,相手1\n52,相性値20\n"
            "60,体力回復停止フラグ\n61,圧力値\n62,労役フラグ\n63,ポジション\n64,ゲスト加入フラグ\n65,この場に居ないフラグ\n66,忠誠度\n"
            "70,元処女\n71,元FUTA\n72,元偽娘\n73,元胸サイズ\n74,現胸サイズ\n75,ＴＳ時会話類型\n76,人化時会話類型\n77,ＴＳ人化時会話類型\n78,元Ｖ感覚\n"
            "80,能力強化回数\n81,力強化回数\n",
            encoding="utf-8",
        )
        (root / "CSV" / "Equip.csv").write_text("0,剣\n1,飾品\n6,装備6\n", encoding="utf-8")
        (root / "CSV" / "Item.csv").write_text("1001,力之源\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDCHARA 100
NO:0 = 100
TARGET = 0
CALL ADD_EXP, 0, 5, 0
CALL ADDS_EXP, "調教経験", 3, 0
MAXBASE:0:力 = 30
CALL SET_BATTLE_STATUS, 0, 50, "力"
CALL SET_EVENTFLAG, 10, 0, 0
CALL SET_RELATION, 0
CALL SET_SEX, 0, 1
BASE:0:体力 = 0
ABL:0:従順 = 2
MARK:0:苦痛刻印 = 1
CALL 気絶処理, 0
PRINTFORML A={TCVAR:0:0}:{TCVAR:0:101}:{TCVAR:0:1}:{MAXBASE:0:力}:{CFLAG:0:力補正}:{GETBIT(CFLAG:0:20,10)}:{CFLAG:0:50}:{CFLAG:0:49}:{TALENT:0:男性}:{BASE:0:体力}:{ABL:0:従順}:{CFLAG:0:圧力値}:{RESULT}
CSTR:0:11 = "I"
CSTR:0:12 = "YOU"
ABL:0:会話類型 = 9
CALL 初ＴＳ処理, 0
CALL ＴＳ処理, 0, 5
PRINTFORML T={TALENT:0:男性}:{TALENT:0:処女}:{TALENT:0:爆乳}:%CSTR:0:11%/%CSTR:0:16%:{CFLAG:0:ＴＳ時会話類型}:{CFLAG:0:元胸サイズ}
FLAG:ポジション1 = -1
CALL ADD_GUEST_COMPANION, 200, 88, 0
PRINTFORML G={RESULT}:{RESULT:1}:{RESULT:2}:{FLAG:ポジション1}:{CFLAG:(FLAG:ポジション1):ゲスト加入フラグ}:{CFLAG:(FLAG:ポジション1):忠誠度}
EQUIP:0:飾品 = GETNUM(ITEM,"力之源")
CALL LVUP_BOOSTER, 0, 1
TALENT:0:人修羅 = 1
EQUIP:0:装備6 = 8200
BASE:0:瀕死 = -10
CALL LVUP_BOOSTER_MAGATAMA, 0, 2
PRINTFORML L={MAXBASE:0:力}:{CFLAG:0:能力強化回数}:{BASE:0:瀕死}
ITEM:1010 = 1
CALL BASE_INCENSE, 0
PRINTFORML I={BASE:0:力}:{ITEM:1010}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False, inputs=["1"])
        rt.run("SYSTEM_TITLE", max_steps=1000)
        self.assertEqual(rt.warnings, [])
        self.assertEqual(
            "".join(rt.output),
            "A=5:5:3:50:20:1:100:-1:1:1:1:24:2\n"
            "T=0:1:1:I/I:9:3\n"
            "G=1:-1:1:1:1:88\n"
            "L=51:1:-20\n"
            "I=1:0\n",
        )

    def test_noninteractive_preserves_stack_when_explicit_inputs_exhaust_before_native_base_incense(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Test\nバージョン,1\n", encoding="utf-8")
        (root / "CSV" / "Base.csv").write_text("0,LV\n1,力\n", encoding="utf-8")
        (root / "CSV" / "Item.csv").write_text("1010,力の香\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text('''@SYSTEM_TITLE
ADDCHARA 100
NO:0 = 100
ITEM:1010 = 1
INPUT
CALL BASE_INCENSE, 0
PRINTFORML incense={BASE:0:力}:{ITEM:1010}
RETURN
''', encoding="utf-8")
        rt = EraRuntime(load_program(root), echo=False, interactive=False, inputs=["seed"])
        rt.run("SYSTEM_TITLE", max_steps=100)
        self.assertEqual("".join(rt.output), "")
        self.assertTrue(rt.waiting_for_input)
        self.assertTrue(rt.stack)
        self.assertEqual(rt.queue_input("1"), "1")
        rt.continue_run(max_steps=100)
        self.assertEqual("".join(rt.output), "incense=1:0\n")
        self.assertFalse(rt.waiting_for_input)
        self.assertEqual(rt.warnings, [])


if __name__ == "__main__":
    unittest.main()

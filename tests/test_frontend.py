import tempfile
import unittest
from pathlib import Path

from eramegaten_engine.cli import build_parser as build_cli_parser
from eramegaten_engine.frontend import FrontendSession
from eramegaten_engine.gui import build_parser as build_gui_parser


class FrontendSessionTests(unittest.TestCase):
    def make_game(self, body: str) -> tuple[tempfile.TemporaryDirectory, Path]:
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        (root / "ERB").mkdir()
        (root / "CSV").mkdir()
        (root / "CSV" / "GameBase.csv").write_text("称号,Frontend Test\nバージョン,1\n", encoding="utf-8")
        (root / "ERB" / "SYSTEM.ERB").write_text(body, encoding="utf-8")
        return td, root

    def test_session_pauses_before_first_frontend_input_and_resumes(self):
        td, root = self.make_game(
            """@SYSTEM_TITLE
PRINTL ready
INPUT
PRINTFORML selected={RESULT}
RETURN
"""
        )
        self.addCleanup(td.cleanup)
        session = FrontendSession(max_steps=100)

        session.load(root)
        paused = session.status()
        self.assertTrue(paused["waiting"])
        self.assertFalse(paused["finished"])
        self.assertIn("ready\n", "".join(session.runtime.output))

        session.submit("7")
        finished = session.status()
        self.assertFalse(finished["waiting"])
        self.assertTrue(finished["finished"])
        self.assertIn("selected=7\n", "".join(session.runtime.output))
        self.assertEqual(finished["warnings"], [])

    def test_session_click_uses_runtime_page_hit_testing(self):
        td, root = self.make_game(
            """@SYSTEM_TITLE
PRINTBUTTON "continue", 5
PRINTL
INPUT
PRINTFORML clicked={RESULT}
RETURN
"""
        )
        self.addCleanup(td.cleanup)
        session = FrontendSession(max_steps=100)
        session.load(root)
        layout = session.layout(char_width=8, line_height=20, viewport_width=800)
        button = layout["print_buttons"][0]

        value, _steps = session.click(
            button["x"] + 1,
            button["y"] + 1,
            char_width=8,
            line_height=20,
            viewport_width=800,
            advance_if_empty=False,
        )

        self.assertEqual(value, "5")
        self.assertTrue(session.status()["finished"])
        self.assertIn("clicked=5\n", "".join(session.runtime.output))

    def test_session_clicks_plain_numeric_title_menu(self):
        td, root = self.make_game(
            """@SYSTEM_TITLE
ALIGNMENT CENTER
PRINTL [0]  NEW GAME
PRINTL [1] LOAD GAME
ALIGNMENT LEFT
INPUT
PRINTFORML clicked={RESULT}
RETURN
"""
        )
        self.addCleanup(td.cleanup)
        session = FrontendSession(max_steps=100)
        session.load(root)
        layout = session.layout(char_width=8, line_height=20, viewport_width=800)
        button = next(item for item in layout["implicit_buttons"] if item["value"] == "0")

        value, _steps = session.click(
            button["x"] + 1,
            button["y"] + 1,
            char_width=8,
            line_height=20,
            viewport_width=800,
            advance_if_empty=False,
        )

        self.assertEqual(value, "0")
        self.assertTrue(session.status()["finished"])
        self.assertIn("clicked=0\n", "".join(session.runtime.output))

    def test_session_clicks_real_input_yn_plain_text_rows(self):
        td, root = self.make_game(
            """@SYSTEM_TITLE
PRINTL 是否対与性相関的経験部分進行詳細設置？
CALL INPUT_YN,"Yes","No"
PRINTFORML selected={RESULT}
RETURN
"""
        )
        self.addCleanup(td.cleanup)
        session = FrontendSession(max_steps=100)
        session.load(root)
        layout = session.layout(char_width=9, line_height=18, viewport_width=900, html_unit_scale=0.18)
        no_row = next(item for item in layout["implicit_buttons"] if item["value"] == "1")

        value, _steps = session.activate_pointer(
            700,
            no_row["y"] + no_row["height"] // 2,
            "1",
            advance_if_empty=False,
        )

        self.assertEqual(value, "1")
        self.assertTrue(session.status()["finished"])
        self.assertIn("selected=1\n", "".join(session.runtime.output))

    def test_session_clicks_onekey_enter_button_with_empty_value(self):
        td, root = self.make_game(
            '''@SYSTEM_TITLE
RESULTS:0 = "_Enter_確定_"
CALL INPUT_ONEKEY_TAP_RESULTS, 0, "-", "_"
PRINTFORML confirmed=<%RESULTS%>
RETURN
'''
        )
        self.addCleanup(td.cleanup)
        session = FrontendSession(max_steps=100)
        session.load(root)
        layout = session.layout(char_width=8, line_height=20, viewport_width=800)
        enter = next(
            item
            for item in layout["print_buttons"]
            if item.get("activate_empty") and "Enter" in item["label"]
        )

        value, _steps = session.click(
            enter["x"] + 1,
            enter["y"] + 1,
            char_width=8,
            line_height=20,
            viewport_width=800,
            advance_if_empty=False,
        )

        self.assertEqual(value, "")
        self.assertTrue(session.status()["finished"])
        self.assertIn("confirmed=<>\n", "".join(session.runtime.output))

    def test_old_plain_numeric_menu_rows_do_not_reactivate(self):
        td, root = self.make_game(
            """@SYSTEM_TITLE
PRINTL [0] old menu
INPUT
PRINTL [0] current menu
INPUT
PRINTFORML selected={RESULT}
RETURN
"""
        )
        self.addCleanup(td.cleanup)
        session = FrontendSession(max_steps=100)
        session.load(root)
        first = session.layout(char_width=8, line_height=20, viewport_width=800)["implicit_buttons"]
        session.click(first[0]["x"] + 1, first[0]["y"] + 1, char_width=8, line_height=20, viewport_width=800)

        current = session.layout(char_width=8, line_height=20, viewport_width=800)["implicit_buttons"]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["display_line"], 2)
        self.assertEqual(current[0]["value"], "0")

    def test_frontend_cli_entries_accept_optional_root(self):
        cli_args = build_cli_parser().parse_args(["gui", "--no-auto-run"])
        gui_args = build_gui_parser().parse_args(["--no-auto-run"])
        self.assertEqual(cli_args.command, "gui")
        self.assertEqual(cli_args.root, "")
        self.assertEqual(gui_args.root, "")

    def test_step_limit_is_a_frontend_pause_not_a_warning_or_fake_input(self):
        td, root = self.make_game(
            """@SYSTEM_TITLE
WHILE 1
LOCAL += 1
WEND
"""
        )
        self.addCleanup(td.cleanup)
        session = FrontendSession(max_steps=10)
        session.load(root)

        first = session.status()
        self.assertTrue(first["step_limited"])
        self.assertEqual(first["warnings"], [])
        self.assertEqual(session.runtime.inputs, [])

        session.advance()
        second = session.status()
        self.assertTrue(second["step_limited"])
        self.assertEqual(session.runtime.inputs, [])

        self.assertTrue(session.request_stop())
        stopped = session.status()
        self.assertTrue(stopped["stopped"])
        self.assertTrue(stopped["finished"])

    def test_live_pointer_and_key_state_are_available_to_polling_builtins(self):
        td, root = self.make_game("@SYSTEM_TITLE\nINPUT\nRETURN\n")
        self.addCleanup(td.cleanup)
        session = FrontendSession(max_steps=20)
        session.load(root)

        session.update_pointer(321, 654, "button-7")
        session.update_key(65, pressed=True, triggered=True)
        self.assertEqual(session.runtime.mouse_x, 321)
        self.assertEqual(session.runtime.mouse_y, 654)
        self.assertEqual(session.runtime.mouse_button, "button-7")
        self.assertIn(65, session.runtime.key_state)
        self.assertIn(65, session.runtime.key_triggered)

        session.update_key(65, pressed=False)
        session.set_active(False)
        self.assertNotIn(65, session.runtime.key_state)
        self.assertFalse(session.runtime.is_active)

    def test_batch_message_skip_stops_before_menu_input(self):
        td, root = self.make_game(
            """@SYSTEM_TITLE
PRINTW first message
PRINTW second message
PRINTL [0] choose me
INPUT
PRINTFORML selected={RESULT}
RETURN
"""
        )
        self.addCleanup(td.cleanup)
        session = FrontendSession(max_steps=100)
        session.load(root)

        self.assertEqual(session.input_boundary()["kind"], "message")
        result = session.skip_messages(20)

        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["stopped_at"], "input")
        self.assertTrue(session.status()["waiting"])
        self.assertEqual(session.runtime.inputs, [])
        self.assertIn("first message\nsecond message\n[0] choose me\n", "".join(session.runtime.output))
        session.submit("0")
        self.assertTrue(session.status()["finished"])
        self.assertIn("selected=0\n", "".join(session.runtime.output))

    def test_batch_message_skip_respects_limit(self):
        td, root = self.make_game(
            """@SYSTEM_TITLE
PRINTW one
PRINTW two
PRINTW three
INPUT
RETURN
"""
        )
        self.addCleanup(td.cleanup)
        session = FrontendSession(max_steps=100)
        session.load(root)

        result = session.skip_messages(2)

        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["stopped_at"], "message")
        self.assertEqual(session.input_boundary()["source"], "PRINTW three")


if __name__ == "__main__":
    unittest.main()

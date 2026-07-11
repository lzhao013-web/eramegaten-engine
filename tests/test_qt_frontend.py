import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from eramegaten_engine.qt_gui import EraMegatenQtWindow, GameSceneView


class QtFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def runtime_stub(self):
        return SimpleNamespace(
            default_bgcolor=0x090E17,
            current_color=0xE7EDF8,
            render_sprite_image=lambda _name: Image.new("RGBA", (20, 20), (120, 140, 255, 255)),
        )

    def layout(self, *, height=1800, button_y=700):
        return {
            "drawables": [
                {
                    "type": "print_button",
                    "x": 120,
                    "y": button_y,
                    "width": 120,
                    "height": 28,
                    "value": "7",
                    "label": "choose seven",
                    "color": 0xE7EDF8,
                    "bgcolor": 0x090E17,
                }
            ],
            "canvas": {"width": 900, "height": height},
        }

    def test_scene_click_stays_on_same_logical_position_after_scroll_and_zoom(self):
        view = GameSceneView()
        view.resize(520, 320)
        view.show()
        self.app.processEvents()
        view.set_layout(self.layout(), self.runtime_stub(), follow_output=False)
        view.set_zoom(1.5)
        view.centerOn(QPointF(180, 714))
        self.app.processEvents()

        activated = []
        pointers = []
        view.activated.connect(lambda x, y, value: activated.append((x, y, value)))
        view.pointerMoved.connect(lambda x, y, value: pointers.append((x, y, value)))
        viewport_pos = view.mapFromScene(QPointF(140, 710))
        QTest.mouseMove(view.viewport(), QPointF(2, 2).toPoint())
        QTest.mouseMove(view.viewport(), viewport_pos)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=viewport_pos)
        self.app.processEvents()

        self.assertTrue(pointers)
        self.assertAlmostEqual(pointers[-1][0], 140, delta=1)
        self.assertAlmostEqual(pointers[-1][1], 710, delta=1)
        self.assertEqual(pointers[-1][2], "7")
        self.assertEqual(activated[-1][2], "7")
        self.assertAlmostEqual(activated[-1][0], 140, delta=1)
        self.assertAlmostEqual(activated[-1][1], 710, delta=1)
        view.close()

    def test_scene_rebuild_preserves_manual_scroll_but_follow_mode_focuses_latest_line(self):
        view = GameSceneView()
        view.resize(500, 300)
        view.show()
        self.app.processEvents()
        view.set_layout(self.layout(height=2200), self.runtime_stub(), follow_output=False)
        self.app.processEvents()
        view.verticalScrollBar().setValue(680)
        before = view.verticalScrollBar().value()

        view.set_layout(self.layout(height=2600), self.runtime_stub(), follow_output=False)
        self.app.processEvents()
        self.assertEqual(view.verticalScrollBar().value(), before)

        view.verticalScrollBar().setValue(420)
        view.set_layout(
            self.layout(height=3000, button_y=2940),
            self.runtime_stub(),
            follow_output=True,
        )
        self.app.processEvents()
        self.assertEqual(view.verticalScrollBar().value(), view.verticalScrollBar().maximum())
        latest = view.mapFromScene(QPointF(150, 2954))
        self.assertTrue(view.viewport().rect().contains(latest))
        self.assertEqual(view.clickable_value_at(latest), "7")
        view.close()

    def test_follow_mode_anchors_to_latest_control_before_large_trailing_blank_area(self):
        view = GameSceneView()
        view.resize(500, 300)
        view.show()
        self.app.processEvents()
        view.set_layout(
            self.layout(height=6000, button_y=3200),
            self.runtime_stub(),
            follow_output=True,
        )
        self.app.processEvents()

        self.assertLess(view.verticalScrollBar().value(), view.verticalScrollBar().maximum())
        latest = view.mapFromScene(QPointF(150, 3214))
        self.assertTrue(view.viewport().rect().contains(latest))
        self.assertEqual(view.clickable_value_at(latest), "7")
        view.close()

    def test_restored_window_geometry_is_clamped_to_current_screen(self):
        window = EraMegatenQtWindow(auto_run=False, persist_settings=False)
        window.setMinimumSize(100, 100)
        available = self.app.primaryScreen().availableGeometry()
        width = min(500, available.width())
        height = min(400, available.height())
        window.setGeometry(available.right() + 300, available.bottom() + 300, width, height)
        window.show()
        self.app.processEvents()

        window._ensure_window_on_screen()
        self.app.processEvents()

        self.assertTrue(available.contains(window.frameGeometry()))
        window.close()

    def test_clicking_game_row_resumes_output_following(self):
        window = EraMegatenQtWindow(auto_run=False, persist_settings=False)
        window.session.runtime = SimpleNamespace()
        window.follow_check.setChecked(False)
        calls = []
        window._read_max_steps = lambda: 100
        window._run_async = lambda label, action, **kwargs: calls.append((label, action, kwargs))

        window._scene_activated(900, 2057, "1")

        self.assertTrue(window.follow_check.isChecked())
        self.assertEqual(len(calls), 1)
        self.assertIn("点击 [1]", calls[0][0])
        window.close()

    def test_main_window_builds_polished_qt_frontend_without_loading_game(self):
        window = EraMegatenQtWindow(auto_run=False, persist_settings=False)
        window.show()
        self.app.processEvents()
        self.assertEqual(window.objectName(), "mainWindow")
        self.assertEqual(window.game_view.objectName(), "gameView")
        self.assertEqual(window.game_view.renderer_name, "offscreen-tile-raster")
        self.assertEqual(window.renderer_badge.text(), "原版离屏")
        self.assertTrue(window.follow_check.isChecked())
        self.assertEqual(window.tabs.count(), 4)
        self.assertEqual(window.tabs.tabText(1), "操作")
        self.assertGreaterEqual(window.width(), 1050)
        window.close()

    def test_narrow_window_keeps_inspector_visible_and_hides_optional_header_metrics(self):
        window = EraMegatenQtWindow(auto_run=False, persist_settings=False)
        window.resize(1050, 680)
        window.show()
        self.app.processEvents()

        self.assertTrue(window.inspector.isVisible())
        self.assertGreaterEqual(window.inspector.width(), 250)
        self.assertGreater(window.game_panel.width(), 500)
        self.assertFalse(window.pointer_label.isVisible())
        self.assertFalse(window.renderer_badge.isVisible())
        self.assertFalse(window.zoom_reset.isVisible())
        window.close()

    def test_clean_mode_maximizes_canvas_and_restores_convenience_panels(self):
        window = EraMegatenQtWindow(auto_run=False, persist_settings=False)
        window.show()
        self.app.processEvents()
        self.assertTrue(window.project_panel.isVisible())
        self.assertTrue(window.inspector.isVisible())
        self.assertTrue(window.input_card.isVisible())

        window._toggle_clean_mode(True)
        self.app.processEvents()
        self.assertFalse(window.project_panel.isVisible())
        self.assertFalse(window.inspector.isVisible())
        self.assertFalse(window.input_card.isVisible())
        self.assertTrue(window.clean_toggle.isChecked())
        self.assertTrue(window.game_view.isVisible())

        window._toggle_clean_mode(False)
        self.app.processEvents()
        self.assertTrue(window.project_panel.isVisible())
        self.assertTrue(window.inspector.isVisible())
        self.assertTrue(window.input_card.isVisible())
        window.close()

    def test_image_button_keeps_click_value_and_tooltip_in_scene(self):
        view = GameSceneView()
        view.resize(400, 240)
        view.show()
        self.app.processEvents()
        layout = {
            "drawables": [
                {
                    "type": "image",
                    "x": 80,
                    "y": 90,
                    "width": 60,
                    "height": 40,
                    "src": "portrait",
                    "parent": "button",
                    "parent_value": "42",
                    "parent_title": "选择角色",
                }
            ],
            "canvas": {"width": 400, "height": 240},
        }
        view.set_layout(layout, self.runtime_stub(), follow_output=False)
        self.app.processEvents()
        self.assertEqual(view.renderer_name, "offscreen-tile-raster")
        viewport_pos = view.mapFromScene(QPointF(100, 105))
        self.assertEqual(view.clickable_value_at(viewport_pos), "42")
        self.assertEqual(view.tooltip_at(viewport_pos), "选择角色")
        view.close()

    def test_plain_numeric_menu_stays_visible_on_hover_and_clicks(self):
        view = GameSceneView()
        view.resize(500, 260)
        view.show()
        self.app.processEvents()
        layout = {
            "drawables": [
                {
                    "type": "text",
                    "x": 120,
                    "y": 90,
                    "width": 180,
                    "height": 28,
                    "text": "[0]  NEW GAME",
                    "color": 0xE7EDF8,
                    "bgcolor": 0x090E17,
                },
                {
                    "type": "implicit_button",
                    "x": 120,
                    "y": 90,
                    "width": 180,
                    "height": 28,
                    "value": "0",
                    "label": "[0]  NEW GAME",
                    "color": 0xE7EDF8,
                    "bgcolor": 0x090E17,
                },
                {
                    "type": "text",
                    "x": 120,
                    "y": 126,
                    "width": 180,
                    "height": 28,
                    "text": "[1]  LOAD GAME",
                    "color": 0xE7EDF8,
                    "bgcolor": 0x090E17,
                },
                {
                    "type": "implicit_button",
                    "x": 120,
                    "y": 126,
                    "width": 180,
                    "height": 28,
                    "value": "1",
                    "label": "[1]  LOAD GAME",
                    "color": 0xE7EDF8,
                    "bgcolor": 0x090E17,
                },
            ],
            "canvas": {"width": 500, "height": 260},
        }
        view.set_layout(layout, self.runtime_stub(), follow_output=False)
        self.app.processEvents()
        self.assertEqual(view.renderer_name, "offscreen-tile-raster")
        self.assertEqual([region["value"] for region in view.hit_regions], ["0", "1"])
        self.assertGreaterEqual(view.hit_regions[0]["rect"].width(), 500)
        self.assertGreaterEqual(view.hit_regions[1]["rect"].width(), 500)
        activated = []
        view.activated.connect(lambda x, y, value: activated.append((x, y, value)))

        def bright_text_pixels(y: int) -> int:
            pixmap = view.viewport().grab()
            image = pixmap.toImage()
            ratio = pixmap.devicePixelRatio()
            top_left = view.mapFromScene(QPointF(128, y + 4))
            bottom_right = view.mapFromScene(QPointF(292, y + 24))
            left = max(0, round(top_left.x() * ratio))
            top = max(0, round(top_left.y() * ratio))
            right = min(image.width(), round(bottom_right.x() * ratio))
            bottom = min(image.height(), round(bottom_right.y() * ratio))
            return sum(
                1
                for py in range(top, bottom)
                for px in range(left, right)
                if max(
                    image.pixelColor(px, py).red(),
                    image.pixelColor(px, py).green(),
                    image.pixelColor(px, py).blue(),
                )
                >= 150
            )

        QTest.mouseMove(view.viewport(), view.mapFromScene(QPointF(40, 40)))
        self.app.processEvents()
        baseline_pixels = [bright_text_pixels(90), bright_text_pixels(126)]
        self.assertTrue(all(count > 40 for count in baseline_pixels))

        # Exercise a continuous path: enter from outside, cross both labels,
        # leave the rows, and return to NEW GAME before clicking it.
        trajectory = (
            [QPointF(60 + step * 8, 104) for step in range(31)]
            + [QPointF(308 + step * 5, 104 + step * 5) for step in range(9)]
            + [QPointF(340 - step * 8, 140) for step in range(29)]
            + [QPointF(108 + step * 4, 140 - step * 2) for step in range(11)]
        )
        for scene_pos in trajectory:
            viewport_pos = view.mapFromScene(scene_pos)
            QTest.mouseMove(view.viewport(), viewport_pos, delay=1)
            self.app.processEvents()
            if 125 <= scene_pos.x() <= 295 and 94 <= scene_pos.y() <= 114:
                self.assertEqual(view.clickable_value_at(viewport_pos), "0")
            elif 125 <= scene_pos.x() <= 295 and 130 <= scene_pos.y() <= 150:
                self.assertEqual(view.clickable_value_at(viewport_pos), "1")
            for row_y, baseline in zip((90, 126), baseline_pixels):
                self.assertEqual(bright_text_pixels(row_y), baseline)

        viewport_pos = view.mapFromScene(QPointF(150, 104))
        QTest.mouseMove(view.viewport(), viewport_pos)
        self.app.processEvents()
        self.assertEqual(view.clickable_value_at(viewport_pos), "0")
        self.assertEqual(
            view.clickable_value_at(view.mapFromScene(QPointF(460, 104))),
            "0",
        )

        # Deliberately click far to the right of the short menu label; the
        # complete active row must submit its value.
        wide_row_pos = view.mapFromScene(QPointF(460, 104))
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=wide_row_pos)
        self.app.processEvents()
        self.assertEqual(activated[-1][2], "0")
        view.close()

    def test_original_printbutton_hover_is_text_only_yellow_overlay(self):
        view = GameSceneView()
        view.resize(420, 180)
        view.show()
        self.app.processEvents()
        layout = {
            "drawables": [
                {
                    "type": "text",
                    "x": 80,
                    "y": 60,
                    "width": 150,
                    "height": 18,
                    "text": "[0] NEW GAME",
                    "color": 0xC0C0C0,
                    "bgcolor": 0x000000,
                    "font": "ＭＳ ゴシック",
                },
                {
                    "type": "print_button",
                    "x": 80,
                    "y": 60,
                    "width": 150,
                    "height": 18,
                    "value": "0",
                    "label": "[0] NEW GAME",
                    "color": 0xC0C0C0,
                    "bgcolor": 0x000000,
                    "font": "ＭＳ ゴシック",
                },
            ],
            "canvas": {"width": 400, "height": 160},
        }
        runtime = SimpleNamespace(
            default_bgcolor=0x000000,
            current_color=0xC0C0C0,
            render_sprite_image=lambda _name: Image.new("RGBA", (1, 1)),
        )
        view.set_layout(layout, runtime, follow_output=False)
        self.app.processEvents()

        def count_pixels(image, predicate):
            return sum(
                1
                for y in range(image.height())
                for x in range(image.width())
                if predicate(image.pixelColor(x, y))
            )

        baseline = view.viewport().grab().toImage()
        # The modern purple rounded frame must be completely absent.
        self.assertEqual(
            count_pixels(
                baseline,
                lambda c: (c.red(), c.green(), c.blue()) == (124, 140, 255),
            ),
            0,
        )
        self.assertEqual(
            count_pixels(baseline, lambda c: c.red() > 180 and c.green() > 180 and c.blue() < 80),
            0,
        )

        QTest.mouseMove(view.viewport(), view.mapFromScene(QPointF(10, 10)))
        QTest.mouseMove(view.viewport(), view.mapFromScene(QPointF(105, 69)))
        self.app.processEvents()
        hovered = view.viewport().grab().toImage()
        self.assertGreater(
            count_pixels(hovered, lambda c: c.red() > 180 and c.green() > 180 and c.blue() < 80),
            20,
        )
        # The row is still populated; only its text color changed.
        self.assertGreater(
            count_pixels(hovered, lambda c: max(c.red(), c.green(), c.blue()) > 120),
            40,
        )
        view.close()

    def test_following_new_output_restores_left_edge(self):
        view = GameSceneView()
        view.resize(420, 180)
        view.show()
        self.app.processEvents()
        wide = self.layout(height=400, button_y=300)
        wide["canvas"]["width"] = 1800
        view.set_layout(wide, self.runtime_stub(), follow_output=False)
        self.app.processEvents()
        view.horizontalScrollBar().setValue(340)
        self.assertGreater(view.horizontalScrollBar().value(), 0)

        view.set_layout(wide, self.runtime_stub(), follow_output=True)
        self.app.processEvents()
        self.assertEqual(view.horizontalScrollBar().value(), 0)
        view.close()

    def test_authoritative_original_layout_uses_eighteen_pixel_rows(self):
        window = EraMegatenQtWindow(auto_run=False, persist_settings=False)
        char_width, line_height, _viewport, html_scale = window._layout_metrics()
        self.assertGreaterEqual(char_width, 7)
        self.assertEqual(line_height, 18)
        self.assertEqual(window.game_view.font_pixel_size, 18)
        self.assertAlmostEqual(html_scale, 0.18)
        window.close()

    def test_loaded_emuera_profile_controls_fixed_canvas_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "CSV").mkdir()
            (root / "ERB").mkdir()
            (root / "CSV" / "GameBase.csv").write_text("称号,Profile\nバージョン,1\n", encoding="utf-8")
            (root / "emuera.config").write_text(
                "フォント名:MS Gothic\nフォントサイズ:20\n一行の高さ:24\n"
                "ウィンドウ幅:1280\nウィンドウ高さ:720\n",
                encoding="utf-8",
            )
            (root / "ERB" / "SYSTEM.ERB").write_text("@SYSTEM_TITLE\nPRINTL ready\nINPUT\n", encoding="utf-8")
            window = EraMegatenQtWindow(auto_run=False, persist_settings=False)
            window.session.load(root)
            window._render_runtime()

            _char_width, line_height, viewport, html_scale = window._layout_metrics()
            self.assertEqual(window.session.runtime.default_font, "MS Gothic")
            self.assertEqual((line_height, viewport), (24, 1280))
            self.assertAlmostEqual(html_scale, 0.20)
            self.assertEqual(window.game_view.font_pixel_size, 20)
            self.assertEqual(window.game_view.line_height, 24)
            self.assertEqual(window.game_view.reference_size, (1280, 720))
            window.close()

    def test_short_page_is_bottom_anchored_to_original_1600_by_950_canvas(self):
        view = GameSceneView()
        view.configure_rendering(
            font_pixel_size=18,
            line_height=18,
            reference_width=1600,
            reference_height=950,
        )
        runtime = self.runtime_stub()
        runtime.output = ["title\n"]
        view.set_layout(
            {
                "drawables": [
                    {
                        "type": "print_button",
                        "x": 741,
                        "y": 180,
                        "width": 117,
                        "height": 18,
                        "value": "0",
                        "label": "[0] NEW GAME",
                        "color": 0xC0C0C0,
                        "bgcolor": 0,
                    }
                ],
                "canvas": {"width": 1602, "height": 198},
            },
            runtime,
            follow_output=True,
        )

        # 198 px of output plus Emuera's fresh 18 px cursor row and final
        # client-edge cursor pixel leave 733 px above the title.
        self.assertEqual(view.content_origin_y, 733)
        self.assertEqual(view.canvas_size, (1600, 950))
        self.assertEqual(round(view.hit_regions[0]["rect"].top()), 913)
        view.close()

    def test_original_pixel_zoom_avoids_fractional_dpi_bitmap_enlargement(self):
        view = GameSceneView()
        view._native_device_pixel_ratio = lambda: 1.5
        view.configure_rendering(
            font_pixel_size=18,
            line_height=18,
            reference_width=1600,
            reference_height=950,
        )
        runtime = self.runtime_stub()
        runtime.default_bgcolor = 0
        runtime.output = ["text"]
        view.set_layout(
            {
                "drawables": [
                    {
                        "type": "text",
                        "x": 0,
                        "y": 0,
                        "width": 120,
                        "height": 18,
                        "text": "NEW GAME",
                        "color": 0xC0C0C0,
                        "bgcolor": 0,
                        "font": "MS Gothic",
                    }
                ],
                "canvas": {"width": 1600, "height": 950},
            },
            runtime,
            follow_output=False,
        )

        self.assertAlmostEqual(view._scene_scale(), 2 / 3)
        tile_100 = view._raster_tile(0, 0)
        self.assertIsNotNone(tile_100)
        self.assertAlmostEqual(tile_100.devicePixelRatioF(), 1.0)
        view.set_zoom(1.5)
        tile_150 = view._raster_tile(0, 0)
        self.assertIsNotNone(tile_150)
        self.assertAlmostEqual(view._scene_scale(), 1.0)
        self.assertAlmostEqual(tile_150.devicePixelRatioF(), 1.5)
        self.assertEqual(tile_150.width(), 1536)
        self.assertEqual(tile_150.height(), 384)
        view.close()

    def test_empty_value_enter_control_is_still_clickable(self):
        view = GameSceneView()
        view.resize(400, 220)
        view.show()
        self.app.processEvents()
        layout = {
            "drawables": [
                {
                    "type": "print_button",
                    "x": 80,
                    "y": 70,
                    "width": 150,
                    "height": 28,
                    "value": "",
                    "activate_empty": True,
                    "label": "[Enter] 確定",
                    "color": 0xE7EDF8,
                    "bgcolor": 0x090E17,
                }
            ],
            "canvas": {"width": 400, "height": 220},
        }
        view.set_layout(layout, self.runtime_stub(), follow_output=False)
        self.app.processEvents()
        viewport_pos = view.mapFromScene(QPointF(110, 84))
        activated = []
        view.activated.connect(lambda x, y, value: activated.append((x, y, value)))

        self.assertEqual(view.clickable_value_at(viewport_pos), "")
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=viewport_pos)
        self.app.processEvents()
        self.assertEqual(activated[-1][2], "")
        view.close()

    def test_right_click_requests_safe_batch_skip_without_activating_row(self):
        view = GameSceneView()
        view.resize(400, 220)
        view.show()
        self.app.processEvents()
        view.set_layout(self.layout(height=240, button_y=90), self.runtime_stub(), follow_output=False)
        self.app.processEvents()
        skipped = []
        activated = []
        view.skipRequested.connect(lambda: skipped.append(True))
        view.activated.connect(lambda x, y, value: activated.append((x, y, value)))

        position = view.mapFromScene(QPointF(150, 104))
        QTest.mouseClick(view.viewport(), Qt.MouseButton.RightButton, pos=position)
        self.app.processEvents()

        self.assertEqual(skipped, [True])
        self.assertEqual(activated, [])
        view.close()

    def test_actual_font_metrics_expand_canvas_and_button_hit_bounds(self):
        view = GameSceneView()
        view.resize(360, 200)
        view.show()
        self.app.processEvents()
        layout = {
            "drawables": [
                {
                    "type": "print_button",
                    "x": 10,
                    "y": 20,
                    "width": 20,
                    "height": 28,
                    "value": "9",
                    "label": "W" * 30,
                    "color": 0xE7EDF8,
                    "bgcolor": 0x090E17,
                }
            ],
            "canvas": {"width": 40, "height": 80},
        }
        view.set_layout(layout, self.runtime_stub(), follow_output=False)
        self.app.processEvents()

        self.assertGreater(view.hit_regions[0]["rect"].width(), 20)
        self.assertGreater(view.canvas_size[0], view.hit_regions[0]["rect"].right())
        view.close()

    def test_static_sprite_pixmap_cache_survives_layout_refresh(self):
        calls = []
        runtime = SimpleNamespace(
            default_bgcolor=0x090E17,
            current_color=0xE7EDF8,
            render_sprite_image=lambda name: (
                calls.append(name) or Image.new("RGBA", (20, 20), (120, 140, 255, 255))
            ),
        )
        layout = {
            "drawables": [
                {"type": "image", "x": 20, "y": 20, "width": 40, "height": 40, "src": "face"}
            ],
            "canvas": {"width": 120, "height": 100},
        }
        view = GameSceneView()
        view.resize(240, 160)
        view.show()
        self.app.processEvents()
        view.set_layout(layout, runtime, follow_output=False)
        self.app.processEvents()
        view.viewport().grab()
        view.set_layout(layout, runtime, follow_output=False)
        self.app.processEvents()
        view.viewport().grab()

        self.assertEqual(calls, ["face"])
        self.assertEqual(view.render_failures, ())
        view.close()

    def test_legacy_separators_and_block_bars_match_original_pixel_geometry(self):
        runtime = SimpleNamespace(
            default_bgcolor=0,
            current_color=0xC0C0C0,
            output=["content"],
            render_sprite_image=lambda _name: Image.new("RGBA", (1, 1)),
        )
        view = GameSceneView()
        view.configure_rendering(
            font_pixel_size=18,
            line_height=18,
            reference_width=1600,
            reference_height=100,
        )
        view.set_layout(
            {
                "drawables": [
                    {"type": "text", "x": 0, "y": 0, "width": 1602, "height": 18, "text": "=" * 178, "color": 0xC0C0C0, "bgcolor": 0},
                    {"type": "text", "x": 45, "y": 20, "width": 414, "height": 18, "text": "▅" * 46, "color": 0xC07070, "bgcolor": 0},
                    {"type": "text", "x": 72, "y": 40, "width": 72, "height": 18, "text": "▄" * 8, "color": 0x202050, "bgcolor": 0},
                    {"type": "text", "x": 81, "y": 60, "width": 18, "height": 18, "text": "▋" * 2, "color": 0x502020, "bgcolor": 0},
                ],
                "canvas": {"width": 1602, "height": 100},
            },
            runtime,
            follow_output=False,
        )
        tile = view._raster_tile(0, 0)
        self.assertIsNotNone(tile)

        self.assertEqual(tile.pixelColor(3, 6).red(), 115)
        self.assertEqual(tile.pixelColor(10, 6).red(), 0)
        self.assertEqual(tile.pixelColor(12, 6).red(), 115)
        self.assertEqual(tile.pixelColor(49, 27).getRgb()[:3], (192, 112, 112))
        self.assertEqual(tile.pixelColor(461, 36).getRgb()[:3], (192, 112, 112))
        self.assertEqual(tile.pixelColor(48, 27).getRgb()[:3], (0, 0, 0))
        self.assertEqual(tile.pixelColor(76, 53).getRgb()[:3], (32, 32, 80))
        self.assertEqual(tile.pixelColor(146, 57).getRgb()[:3], (32, 32, 80))
        self.assertEqual(tile.pixelColor(85, 61).getRgb()[:3], (80, 32, 32))
        self.assertEqual(tile.pixelColor(90, 61).getRgb()[:3], (0, 0, 0))
        view.close()

    def test_print_image_uses_winapi_bearing_and_bilinear_scaling(self):
        source = Image.new("RGBA", (2, 1), (0, 0, 0, 255))
        source.putpixel((0, 0), (255, 0, 0, 255))
        source.putpixel((1, 0), (0, 0, 255, 255))
        runtime = SimpleNamespace(
            default_bgcolor=0,
            current_color=0xC0C0C0,
            output=["content"],
            render_sprite_image=lambda _name: source,
        )
        view = GameSceneView()
        view.set_layout(
            {
                "drawables": [
                    {"type": "print_image", "x": 10, "y": 10, "width": 4, "height": 2, "src": "face"},
                    {"type": "image", "x": 30, "y": 10, "width": 4, "height": 2, "src": "face"},
                ],
                "canvas": {"width": 80, "height": 40},
            },
            runtime,
            follow_output=False,
        )
        tile = view._raster_tile(0, 0)
        self.assertIsNotNone(tile)

        self.assertEqual(tile.pixelColor(10, 10).getRgb()[:3], (0, 0, 0))
        self.assertGreater(tile.pixelColor(13, 10).red(), tile.pixelColor(13, 10).blue())
        self.assertGreater(tile.pixelColor(16, 10).blue(), tile.pixelColor(16, 10).red())
        mixed = tile.pixelColor(14, 10)
        self.assertGreater(mixed.red(), 0)
        self.assertGreater(mixed.blue(), 0)
        self.assertIn(tile.pixelColor(31, 10).getRgb()[:3], {(255, 0, 0), (0, 0, 255)})
        view.close()

    def test_missing_sprite_is_reported_and_painted_as_visible_fallback(self):
        runtime = SimpleNamespace(
            default_bgcolor=0x090E17,
            current_color=0xE7EDF8,
            render_sprite_image=lambda name: (_ for _ in ()).throw(KeyError(name)),
        )
        layout = {
            "drawables": [
                {
                    "type": "print_image",
                    "x": 20,
                    "y": 20,
                    "width": 180,
                    "height": 28,
                    "src": "missing",
                    "asset_missing": True,
                }
            ],
            "canvas": {"width": 240, "height": 100},
        }
        view = GameSceneView()
        view.resize(260, 140)
        view.show()
        self.app.processEvents()
        view.set_layout(layout, runtime, follow_output=False)
        self.app.processEvents()
        rendered = view.viewport().grab().toImage()

        self.assertEqual(view.render_failures, ("missing",))
        self.assertTrue(
            any(
                rendered.pixelColor(x, y).red() > 40
                for y in range(20, 50)
                for x in range(20, 205)
            )
        )
        view.close()

    def test_compact_action_tab_extracts_and_filters_current_choices(self):
        window = EraMegatenQtWindow(auto_run=False, persist_settings=False)
        window.session.runtime = SimpleNamespace()
        submitted = []
        window._submit = lambda value=None: submitted.append(value)
        layout = {
            "rows": [
                {"index": 4, "text": "[0] NEW GAME"},
                {"index": 5, "text": "[1] LOAD GAME"},
            ],
            "drawables": [
                {"type": "implicit_button", "line": 4, "x": 80, "value": "0", "label": "[0]"},
                {"type": "implicit_button", "line": 5, "x": 80, "value": "1", "label": "[1]"},
            ],
        }

        window._update_actions(layout)
        self.assertEqual(window.action_list.count(), 2)
        self.assertIn("NEW GAME", window.action_list.item(0).text())
        window._filter_actions("load")
        self.assertTrue(window.action_list.item(0).isHidden())
        self.assertFalse(window.action_list.item(1).isHidden())
        window._activate_first_filtered_action()
        self.assertEqual(submitted, ["1"])
        window.close()

    def test_canvas_y_and_n_hotkeys_select_visible_yes_no_actions(self):
        window = EraMegatenQtWindow(auto_run=False, persist_settings=False)
        window.session.runtime = SimpleNamespace(waiting_for_input=True)
        submitted = []
        window._submit = lambda value=None: submitted.append(value)
        window._update_actions(
            {
                "rows": [
                    {"index": 4, "text": "[0] Yes"},
                    {"index": 5, "text": "[1] No"},
                ],
                "drawables": [
                    {"type": "implicit_button", "line": 4, "x": 0, "value": "0", "label": "[0]"},
                    {"type": "implicit_button", "line": 5, "x": 0, "value": "1", "label": "[1]"},
                ],
            }
        )

        window._quick_input("y")
        window._quick_input("n")
        self.assertEqual(submitted, ["0", "1"])
        window.close()

    def test_cpu_raster_paints_only_visible_tiles_in_long_transcript(self):
        view = GameSceneView()
        view.resize(640, 320)
        view.show()
        self.app.processEvents()
        drawables = [
            {
                "type": "text",
                "x": 20,
                "y": row * 22,
                "width": 180,
                "height": 22,
                "text": f"row {row}",
                "color": 0xE7EDF8,
                "bgcolor": 0x090E17,
            }
            for row in range(20_000)
        ]
        view.set_layout(
            {
                "drawables": drawables,
                "canvas": {"width": 640, "height": 20_000 * 22},
            },
            self.runtime_stub(),
            follow_output=False,
        )
        self.app.processEvents()
        view.viewport().grab()

        self.assertEqual(view.drawable_count, 20_000)
        self.assertLess(view.last_paint_candidate_count, 40)
        self.assertLessEqual(view.last_painted_count, view.last_paint_candidate_count)
        view.close()


if __name__ == "__main__":
    unittest.main()

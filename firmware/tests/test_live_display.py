from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_st7735_has_live_pages_face_animation_and_server_payload_consumer() -> None:
    display = (ROOT / "main" / "device_display.cpp").read_text()
    header = (ROOT / "include" / "device_display.h").read_text()
    network = (ROOT / "main" / "device_network.cpp").read_text()
    assert "esp_err_t set_dashboard(" in header
    assert "esp_err_t set_speaking_for(uint32_t duration_ms);" in header
    assert 'xTaskCreate(display_task, task_name' in display
    assert "display_model.animation_frame % 3 == 0" in display
    assert "flush_tft_region(size_t x, size_t y, size_t width, size_t height)" in display
    assert "flush_tft_region(112, 0, 48, 44)" in display
    assert "!full_refresh && !display_model.game_active" in display
    assert "display_model.blink = true;" in display
    assert 'std::strcmp(display_model.page.data(), "home") == 0' in display
    assert "draw_text(display_model.title.data()" in display
    assert "draw_text(display_model.items[index].data()" in display
    assert 'std::strcmp(type->valuestring, "display.state") == 0' in network
    assert "DeviceDisplay::set_dashboard(" in network
    assert 'std::strcmp(type->valuestring, "game.state") == 0' in network
    assert 'std::strcmp(type->valuestring, "calendar.notification") == 0' in network
    assert 'std::strcmp(type->valuestring, "sound.effect") == 0' in network
    assert 'std::strcmp(type->valuestring, "animation.state") == 0' in network
    assert 'std::strcmp(emoji, "🎮") == 0' in display
    assert "esp_err_t queue_sound_effect(const char* effect)" in (
        ROOT / "main" / "audio_i2s.cpp"
    ).read_text()
    assert 'std::strcmp(type->valuestring, "tts.start") == 0' in network
    assert "DeviceDisplay::set_speaking_for(" in network
    assert 'HardwareConfig::TFT::MOSI' in display
    assert 'HardwareConfig::TFT::SCK' in display


def test_display_automatically_prefers_ssd1306_and_falls_back_to_st7735() -> None:
    display = (ROOT / "main" / "device_display.cpp").read_text()
    hardware = (ROOT / "include" / "hardware_config.h").read_text()
    build = (ROOT / "main" / "CMakeLists.txt").read_text()
    assert "i2c_master_probe(i2c_bus, address, 100)" in display
    assert "OLED_ADDRESS_PRIMARY = 0x3C" in display
    assert "OLED_ADDRESS_SECONDARY = 0x3D" in display
    assert "if (oled_result == ESP_OK)" in display
    assert "trying ST7735 TFT" in display
    assert "active_panel = Panel::Tft" in display
    assert "I2C_PORT = 0" in hardware
    assert "SDA = GPIO_NUM_1" in hardware
    assert "SCL = GPIO_NUM_2" in hardware
    assert "esp_driver_i2c" in build


def test_tft_dashboard_uses_lvgl_widgets_with_game_and_oled_fallbacks() -> None:
    display = (ROOT / "main" / "device_display.cpp").read_text()
    assert "lv_disp_drv_register(&lv_display_driver)" in display
    assert "lv_display_driver.flush_cb = lv_flush" in display
    assert "lv_label_create(screen)" in display
    assert "lv_label_set_text(lv_items[index]" in display
    assert "lv_refr_now(lv_display)" in display
    assert "const uint8_t orientation = 0x60;" in display
    for emotion in ("excited", "curious", "surprised", "happy", "sad", "listening", "thinking", "speaking"):
        assert f'std::strcmp(state, "{emotion}")' in display
    assert "if (display_model.game_active)" in display
    assert "draw_screen();" in display


def test_tft_layout_bounds_and_pagination(tmp_path: Path) -> None:
    source = tmp_path / "layout_test.cpp"
    source.write_text(
        '#include "dashboard_layout.h"\n'
        '#include <cassert>\n'
        'int main() {\n'
        '  using namespace DashboardLayout;\n'
        '  assert(first_item(0, 0) == 0);\n'
        '  assert(first_item(4, 50) == 0);\n'
        '  assert(first_item(8, 49) == 0);\n'
        '  assert(first_item(8, 50) == 4);\n'
        '  assert(first_item(8, 100) == 0);\n'
        '  assert(items_y >= 43 && items_y + visible_items * row_height <= footer_y);\n'
        '  assert(footer_y + font_height <= height);\n'
        '  assert(width == 160 && height == 128 && left + header_width <= 119);\n'
        '}\n'
    )
    executable = tmp_path / "layout_test"
    subprocess.run(["c++", "-std=c++17", "-I", str(ROOT / "include"),
                    str(source), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)


def test_face_dashboard_lifecycle_and_tick_wrap(tmp_path: Path) -> None:
    source = tmp_path / "lifecycle_test.cpp"
    source.write_text(
        '#include "display_lifecycle.h"\n'
        '#include <cassert>\n'
        'int main() {\n'
        '  DisplayLifecycle view;\n'
        '  assert(!view.dashboard());\n'
        '  view.show_page(false, 10, 4);\n'
        '  assert(view.dashboard() && !view.update(100));\n'
        '  view.show_page(true, 100, 4);\n'
        '  assert(!view.update(103) && view.dashboard());\n'
        '  assert(view.update(104) && !view.dashboard());\n'
        '  view.show_page(true, 0xfffffffeu, 4);\n'
        '  assert(!view.update(1) && view.dashboard());\n'
        '  assert(view.update(2) && !view.dashboard());\n'
        '  view.show_page(true, 20, 4);\n'
        '  view.show_page(false, 21, 4);\n'
        '  assert(!view.update(25) && view.dashboard());\n'
        '}\n'
    )
    executable = tmp_path / "lifecycle_test"
    subprocess.run(["c++", "-std=c++17", "-I", str(ROOT / "include"),
                    str(source), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)


def test_lvgl_face_geometry_fits_screen_and_partial_refresh(tmp_path: Path) -> None:
    source = tmp_path / "face_test.cpp"
    source.write_text(
        '#include "face_layout.h"\n'
        '#include <cassert>\n'
        'int main() {\n'
        '  for (bool dashboard : {false, true}) {\n'
        '    for (bool bob : {false, true}) {\n'
        '      auto face = FaceLayout::bounds(dashboard, bob);\n'
        '      assert(face.x >= 0 && face.y >= 0);\n'
        '      assert(face.x + face.size <= 160 && face.y + face.size <= 128);\n'
        '      assert(FaceLayout::eye_width(dashboard) < face.size / 4);\n'
        '      assert(FaceLayout::mouth_half_width(dashboard) < face.size / 2);\n'
        '      if (dashboard) assert(face.x >= 112 && face.y + face.size <= 44);\n'
        '    }\n'
        '  }\n'
        '}\n'
    )
    executable = tmp_path / "face_test"
    subprocess.run(["c++", "-std=c++17", "-include", "initializer_list", "-I", str(ROOT / "include"),
                    str(source), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
    display = (ROOT / "main" / "device_display.cpp").read_text()
    assert "lv_face = make_face_shape(screen" in display
    assert "lv_line_create(lv_face)" in display
    assert "render_lv_face(false)" in display and "render_lv_face(true)" in display

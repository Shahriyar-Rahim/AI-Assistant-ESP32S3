from pathlib import Path


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
    assert "flush_tft_region(80, 8, 48, 48)" in display
    assert "active_panel == Panel::Tft && !full_refresh" in display
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

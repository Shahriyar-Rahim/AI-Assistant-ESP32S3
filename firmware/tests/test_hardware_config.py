from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_hardware_pin_mapping_and_independent_i2s_ports() -> None:
    header = (ROOT / "include" / "hardware_config.h").read_text()
    expected = {
        "Microphone": {"I2S_PORT": "I2S_NUM_0", "WS": "GPIO_NUM_17", "BCLK": "GPIO_NUM_18", "DATA_IN": "GPIO_NUM_15"},
        "Amplifier": {"I2S_PORT": "I2S_NUM_1", "BCLK": "GPIO_NUM_3", "LRC": "GPIO_NUM_8", "DATA_OUT": "GPIO_NUM_16", "SHUTDOWN": "GPIO_NUM_46"},
        "OLED": {"I2C_PORT": "0", "SDA": "GPIO_NUM_1", "SCL": "GPIO_NUM_2"},
        "TFT": {"SPI_HOST": "SPI2_HOST", "CS": "GPIO_NUM_10", "DC": "GPIO_NUM_9", "RST": "GPIO_NUM_14", "MOSI": "GPIO_NUM_11", "SCK": "GPIO_NUM_12", "BACKLIGHT": "GPIO_NUM_13"},
    }
    for namespace, constants in expected.items():
        block = re.search(rf"namespace {namespace}\s*\{{(.*?)\n\}}", header, re.S)
        assert block, namespace
        for name, value in constants.items():
            assert re.search(rf"\b{name}\s*=\s*{value}\b", block.group(1)), f"{namespace}.{name}"
    assert "GPIO_NUM_5" not in header


def test_firmware_creates_distinct_input_and_output_channels() -> None:
    source = (ROOT / "main" / "audio_i2s.cpp").read_text()
    assert "HardwareConfig::Microphone::I2S_PORT" in source
    assert "HardwareConfig::Amplifier::I2S_PORT" in source
    assert "i2s_new_channel(&rx_config, nullptr, &rx_channel)" in source
    assert "i2s_new_channel(&tx_config, &tx_channel, nullptr)" in source


def test_display_automatically_selects_oled_or_st7735() -> None:
    config = (ROOT / "include" / "hardware_config.h").read_text()
    display = (ROOT / "main" / "device_display.cpp").read_text()
    main = (ROOT / "main" / "main.cpp").read_text()
    assert "constexpr bool ENABLE_TFT = true;" in config
    assert "constexpr bool ENABLE_OLED = true;" in config
    assert "DeviceDisplay::initialize()" in main
    assert "0x2A" in display and "0x2B" in display and "0x2C" in display
    assert "SSD1306 OLED detected at I2C address" in display
    assert "Automatic display selection: ST7735 TFT" in display
    assert 'std::strcmp(state, "listening") == 0' in display

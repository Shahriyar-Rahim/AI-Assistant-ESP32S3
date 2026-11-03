#include "device_display.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdio>
#include <cstdint>
#include <cstring>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "hardware_config.h"

namespace {

constexpr char TAG[] = "device_display";
constexpr char OLED_TAG[] = "device_display";
constexpr size_t WIDTH = 128;
constexpr size_t HEIGHT = 160;
constexpr size_t PIXELS_PER_ROW = WIDTH;
constexpr uint16_t COLOR_BACKGROUND = 0x0861;
constexpr uint16_t COLOR_FACE = 0xFFE0;
constexpr uint16_t COLOR_FEATURE = 0x1082;
constexpr uint16_t COLOR_HEADER = 0x18E3;
constexpr uint16_t COLOR_ACCENT = 0x07FF;
constexpr uint16_t COLOR_TEXT = 0xFFFF;
constexpr uint16_t COLOR_MUTED = 0xBDF7;
constexpr size_t MAX_DISPLAY_ITEMS = 8;
constexpr size_t DISPLAY_TEXT_SIZE = 96;
constexpr uint16_t OLED_ADDRESS_PRIMARY = 0x3C;
constexpr uint16_t OLED_ADDRESS_SECONDARY = 0x3D;
constexpr size_t OLED_WIDTH = 128;
constexpr size_t OLED_HEIGHT = 64;

enum class Panel { None, Oled, Tft };
Panel active_panel = Panel::None;
spi_device_handle_t display_device = nullptr;
i2c_master_bus_handle_t i2c_bus = nullptr;
i2c_master_dev_handle_t oled_device = nullptr;
std::array<uint16_t, WIDTH * HEIGHT> frame_buffer{};
std::array<uint8_t, PIXELS_PER_ROW * 2> row_buffer{};
std::array<uint8_t, OLED_WIDTH * OLED_HEIGHT / 8> oled_frame_buffer{};
std::array<uint8_t, 17> oled_transfer_buffer{};
SemaphoreHandle_t display_mutex = nullptr;

enum class FaceState {
    Idle,
    Listening,
    Thinking,
    Speaking,
    Happy,
    Sad,
    Error,
    Sleeping,
};

struct DisplayModel {
    FaceState face = FaceState::Idle;
    std::array<char, 16> page{"home"};
    std::array<char, 40> title{"ASSISTANT"};
    std::array<char, 48> detail{"LIVE OVERVIEW"};
    std::array<char, 16> emoji{};
    std::array<std::array<char, DISPLAY_TEXT_SIZE>, MAX_DISPLAY_ITEMS> items{};
    size_t item_count = 0;
    uint16_t todos = 0;
    uint16_t reminders = 0;
    uint16_t notes = 0;
    uint16_t notifications = 0;
    uint16_t alarms = 0;
    TickType_t speaking_until = 0;
    bool dirty = true;
    bool blink = false;
    uint32_t animation_frame = 0;
    bool game_active = false;
    DeviceDisplay::GameView game;
} display_model;

esp_err_t write_bytes(const uint8_t* data, size_t length, bool is_data) {
    if (display_device == nullptr || data == nullptr || length == 0) return ESP_ERR_INVALID_STATE;
    ESP_RETURN_ON_ERROR(gpio_set_level(HardwareConfig::TFT::DC, is_data ? 1 : 0),
        TAG, "Cannot set display data/command pin");
    spi_transaction_t transaction{};
    transaction.length = length * 8;
    transaction.tx_buffer = data;
    return spi_device_transmit(display_device, &transaction);
}

esp_err_t command(uint8_t value) {
    return write_bytes(&value, sizeof(value), false);
}

esp_err_t data(const uint8_t* values, size_t length) {
    return write_bytes(values, length, true);
}

esp_err_t command_data(uint8_t opcode, const uint8_t* values, size_t length) {
    ESP_RETURN_ON_ERROR(command(opcode), TAG, "Cannot send ST7735 command");
    if (length != 0) ESP_RETURN_ON_ERROR(data(values, length), TAG, "Cannot send ST7735 data");
    return ESP_OK;
}

void set_pixel(int x, int y, uint16_t color) {
    if (active_panel == Panel::Oled) {
        if (x < 0 || x >= static_cast<int>(OLED_WIDTH) || y < 0 ||
            y >= static_cast<int>(OLED_HEIGHT)) return;
        const size_t index = static_cast<size_t>(x) +
            (static_cast<size_t>(y) / 8) * OLED_WIDTH;
        if (color == COLOR_BACKGROUND) {
            oled_frame_buffer[index] &= static_cast<uint8_t>(~(1U << (y % 8)));
        } else {
            oled_frame_buffer[index] |= static_cast<uint8_t>(1U << (y % 8));
        }
    } else if (active_panel == Panel::Tft &&
        x >= 0 && x < static_cast<int>(WIDTH) && y >= 0 && y < static_cast<int>(HEIGHT)) {
        frame_buffer[static_cast<size_t>(y) * WIDTH + static_cast<size_t>(x)] = color;
    }
}

void fill_rect(int x, int y, int width, int height, uint16_t color) {
    for (int row = y; row < y + height; ++row) {
        for (int column = x; column < x + width; ++column) set_pixel(column, row, color);
    }
}

void fill_circle(int center_x, int center_y, int radius, uint16_t color) {
    const int radius_squared = radius * radius;
    for (int y = -radius; y <= radius; ++y) {
        for (int x = -radius; x <= radius; ++x) {
            if (x * x + y * y <= radius_squared) set_pixel(center_x + x, center_y + y, color);
        }
    }
}

void draw_line(int x0, int y0, int x1, int y1, uint16_t color, int thickness = 1) {
    const int dx = x1 >= x0 ? x1 - x0 : x0 - x1;
    const int sx = x0 < x1 ? 1 : -1;
    const int dy = -(y1 >= y0 ? y1 - y0 : y0 - y1);
    const int sy = y0 < y1 ? 1 : -1;
    int error = dx + dy;
    while (true) {
        for (int offset = 0; offset < thickness; ++offset) {
            set_pixel(x0, y0 + offset, color);
        }
        if (x0 == x1 && y0 == y1) break;
        const int doubled = 2 * error;
        if (doubled >= dy) {
            error += dy;
            x0 += sx;
        }
        if (doubled <= dx) {
            error += dx;
            y0 += sy;
        }
    }
}

esp_err_t flush_tft_region(size_t x, size_t y, size_t width, size_t height) {
    if (active_panel != Panel::Tft || width == 0 || height == 0 ||
        x + width > WIDTH || y + height > HEIGHT) {
        return ESP_ERR_INVALID_ARG;
    }
    const uint8_t columns[] = {
        0, static_cast<uint8_t>(x),
        0, static_cast<uint8_t>(x + width - 1),
    };
    const uint8_t rows[] = {
        0, static_cast<uint8_t>(y),
        0, static_cast<uint8_t>(y + height - 1),
    };
    ESP_RETURN_ON_ERROR(command_data(0x2A, columns, sizeof(columns)), TAG, "Cannot set display columns");
    ESP_RETURN_ON_ERROR(command_data(0x2B, rows, sizeof(rows)), TAG, "Cannot set display rows");
    ESP_RETURN_ON_ERROR(command(0x2C), TAG, "Cannot start display memory write");
    for (size_t row = y; row < y + height; ++row) {
        for (size_t column = 0; column < width; ++column) {
            const uint16_t pixel = frame_buffer[row * WIDTH + x + column];
            row_buffer[column * 2] = static_cast<uint8_t>(pixel >> 8);
            row_buffer[column * 2 + 1] = static_cast<uint8_t>(pixel);
        }
        ESP_RETURN_ON_ERROR(data(row_buffer.data(), width * 2), TAG, "Cannot transfer display row");
    }
    return ESP_OK;
}

esp_err_t flush_frame() {
    if (active_panel == Panel::Oled) {
        for (uint8_t page = 0; page < OLED_HEIGHT / 8; ++page) {
            const uint8_t commands[] = {
                static_cast<uint8_t>(0xB0 | page), 0x00, 0x10,
            };
            for (const uint8_t value : commands) {
                const uint8_t packet[] = {0x00, value};
                ESP_RETURN_ON_ERROR(
                    i2c_master_transmit(oled_device, packet, sizeof(packet), 100),
                    TAG, "Cannot set SSD1306 page address");
            }
            for (size_t offset = 0; offset < OLED_WIDTH; offset += 16) {
                oled_transfer_buffer[0] = 0x40;
                std::memcpy(
                    oled_transfer_buffer.data() + 1,
                    oled_frame_buffer.data() + static_cast<size_t>(page) * OLED_WIDTH + offset,
                    16);
                ESP_RETURN_ON_ERROR(
                    i2c_master_transmit(
                        oled_device, oled_transfer_buffer.data(), oled_transfer_buffer.size(), 100),
                    TAG, "Cannot transfer SSD1306 framebuffer");
            }
        }
        return ESP_OK;
    }
    return flush_tft_region(0, 0, WIDTH, HEIGHT);
}

std::array<uint8_t, 5> glyph(char value) {
    switch (static_cast<char>(std::toupper(static_cast<unsigned char>(value)))) {
        case 'A': return {2, 5, 7, 5, 5};
        case 'B': return {6, 5, 6, 5, 6};
        case 'C': return {3, 4, 4, 4, 3};
        case 'D': return {6, 5, 5, 5, 6};
        case 'E': return {7, 4, 6, 4, 7};
        case 'F': return {7, 4, 6, 4, 4};
        case 'G': return {3, 4, 5, 5, 3};
        case 'H': return {5, 5, 7, 5, 5};
        case 'I': return {7, 2, 2, 2, 7};
        case 'J': return {1, 1, 1, 5, 2};
        case 'K': return {5, 5, 6, 5, 5};
        case 'L': return {4, 4, 4, 4, 7};
        case 'M': return {5, 7, 7, 5, 5};
        case 'N': return {5, 7, 7, 7, 5};
        case 'O': return {2, 5, 5, 5, 2};
        case 'P': return {6, 5, 6, 4, 4};
        case 'Q': return {2, 5, 5, 7, 3};
        case 'R': return {6, 5, 6, 5, 5};
        case 'S': return {3, 4, 2, 1, 6};
        case 'T': return {7, 2, 2, 2, 2};
        case 'U': return {5, 5, 5, 5, 7};
        case 'V': return {5, 5, 5, 5, 2};
        case 'W': return {5, 5, 7, 7, 5};
        case 'X': return {5, 5, 2, 5, 5};
        case 'Y': return {5, 5, 2, 2, 2};
        case 'Z': return {7, 1, 2, 4, 7};
        case '0': return {7, 5, 5, 5, 7};
        case '1': return {2, 6, 2, 2, 7};
        case '2': return {6, 1, 2, 4, 7};
        case '3': return {6, 1, 2, 1, 6};
        case '4': return {5, 5, 7, 1, 1};
        case '5': return {7, 4, 6, 1, 6};
        case '6': return {3, 4, 7, 5, 7};
        case '7': return {7, 1, 2, 2, 2};
        case '8': return {7, 5, 2, 5, 7};
        case '9': return {7, 5, 7, 1, 6};
        case ':': return {0, 2, 0, 2, 0};
        case '.': return {0, 0, 0, 0, 2};
        case '-': return {0, 0, 7, 0, 0};
        case '/': return {1, 1, 2, 4, 4};
        case '?': return {6, 1, 2, 0, 2};
        case '!': return {2, 2, 2, 0, 2};
        case '_': return {0, 0, 0, 0, 7};
        case ',': return {0, 0, 0, 2, 4};
        case '>': return {4, 2, 1, 2, 4};
        case '<': return {1, 2, 4, 2, 1};
        case '=': return {0, 7, 0, 7, 0};
        case '+': return {0, 2, 7, 2, 0};
        case '\'': return {2, 2, 0, 0, 0};
        default: return {0, 0, 0, 0, 0};
    }
}

void draw_text(const char* text, int x, int y, int scale, uint16_t color, size_t max_chars) {
    if (text == nullptr) return;
    size_t rendered = 0;
    for (const char* cursor = text; *cursor != '\0' && rendered < max_chars; ++cursor, ++rendered) {
        const auto rows = glyph(*cursor);
        for (int row = 0; row < 5; ++row) {
            for (int column = 0; column < 3; ++column) {
                if ((rows[row] & (1U << (2 - column))) == 0) continue;
                fill_rect(x + column * scale, y + row * scale, scale, scale, color);
            }
        }
        x += 4 * scale;
    }
}

void draw_face(FaceState state, bool blink, uint32_t frame) {
    if (active_panel == Panel::Oled) {
        const uint16_t face_color = COLOR_TEXT;
        const int bob = (state == FaceState::Listening || state == FaceState::Speaking) &&
            (frame / 6) % 2 == 0 ? 1 : 0;
        fill_circle(113, 22 + bob, 10, face_color);
        if (blink || state == FaceState::Sleeping) {
            draw_line(109, 20 + bob, 111, 20 + bob, COLOR_BACKGROUND, 1);
            draw_line(115, 20 + bob, 117, 20 + bob, COLOR_BACKGROUND, 1);
        } else {
            fill_circle(110, 20 + bob, 1, COLOR_BACKGROUND);
            fill_circle(116, 20 + bob, 1, COLOR_BACKGROUND);
        }
        if (state == FaceState::Speaking) {
            fill_circle(113, 25 + bob, 1 + static_cast<int>((frame / 3) % 2), COLOR_BACKGROUND);
        } else if (state == FaceState::Sad || state == FaceState::Error) {
            draw_line(111, 27 + bob, 115, 27 + bob, COLOR_BACKGROUND, 1);
        } else {
            draw_line(110, 25 + bob, 112, 27 + bob, COLOR_BACKGROUND, 1);
            draw_line(112, 27 + bob, 115, 27 + bob, COLOR_BACKGROUND, 1);
            draw_line(115, 27 + bob, 117, 25 + bob, COLOR_BACKGROUND, 1);
        }
        return;
    }
    const uint16_t face_color = state == FaceState::Error ? 0xF800 : COLOR_FACE;
    const int bob = (state == FaceState::Listening || state == FaceState::Speaking) &&
        (frame / 6) % 2 == 0 ? 1 : 0;
    fill_circle(105, 31 + bob, 19, face_color);
    const int eye_offset = state == FaceState::Thinking ? 2 :
        state == FaceState::Listening ? static_cast<int>((frame / 8) % 3) - 1 : 0;
    if (blink || state == FaceState::Sleeping) {
        draw_line(98, 29 + bob, 102, 29 + bob, COLOR_FEATURE, 2);
        draw_line(108, 29 + bob, 112, 29 + bob, COLOR_FEATURE, 2);
    } else {
        fill_circle(100 + eye_offset, 29 + bob, 2, COLOR_FEATURE);
        fill_circle(110 + eye_offset, 29 + bob, 2, COLOR_FEATURE);
    }
    switch (state) {
        case FaceState::Happy:
        case FaceState::Idle:
            draw_line(99, 38 + bob, 103, 41 + bob, COLOR_FEATURE, 2);
            draw_line(103, 41 + bob, 108, 41 + bob, COLOR_FEATURE, 2);
            draw_line(108, 41 + bob, 112, 38 + bob, COLOR_FEATURE, 2);
            break;
        case FaceState::Sad:
        case FaceState::Error:
            draw_line(99, 41 + bob, 103, 38 + bob, COLOR_FEATURE, 2);
            draw_line(103, 38 + bob, 108, 38 + bob, COLOR_FEATURE, 2);
            draw_line(108, 38 + bob, 112, 41 + bob, COLOR_FEATURE, 2);
            break;
        case FaceState::Speaking:
            fill_circle(105, 40 + bob, 2 + static_cast<int>((frame / 3) % 4), COLOR_FEATURE);
            break;
        case FaceState::Listening:
            fill_circle(105, 40 + bob, 3, COLOR_FEATURE);
            fill_circle(105, 40 + bob, 1, COLOR_ACCENT);
            break;
        case FaceState::Thinking:
            if ((frame / 5) % 2 == 0) fill_circle(116, 17, 2, COLOR_ACCENT);
            break;
        case FaceState::Sleeping:
            draw_line(101, 39 + bob, 109, 39 + bob, COLOR_FEATURE, 2);
            break;
    }
}

void draw_emoji(const char* emoji, int x, int y, int size) {
    if (emoji == nullptr || emoji[0] == '\0') return;
    const int center_x = x + size / 2;
    const int center_y = y + size / 2;
    const int scale = std::max(1, size / 16);
    if (std::strcmp(emoji, "☀️") == 0) {
        fill_circle(center_x, center_y, 4 * scale, COLOR_FACE);
        for (int direction = 0; direction < 8; ++direction) {
            const int dx = (direction % 2 == 0) ? (direction < 4 ? 1 : -1) : 0;
            const int dy = (direction % 2 == 1) ? (direction < 4 ? 1 : -1) : 0;
            draw_line(center_x + dx * 6 * scale, center_y + dy * 6 * scale,
                center_x + dx * 7 * scale, center_y + dy * 7 * scale, COLOR_ACCENT, scale);
        }
        return;
    }
    if (std::strcmp(emoji, "✅") == 0) {
        draw_line(x + 3 * scale, y + 8 * scale, x + 6 * scale, y + 11 * scale, COLOR_ACCENT, 2 * scale);
        draw_line(x + 6 * scale, y + 11 * scale, x + 13 * scale, y + 4 * scale, COLOR_ACCENT, 2 * scale);
        return;
    }
    if (std::strcmp(emoji, "📝") == 0) {
        fill_rect(x + 4 * scale, y + 2 * scale, 9 * scale, 12 * scale, COLOR_TEXT);
        for (int row = 0; row < 3; ++row) {
            fill_rect(x + 6 * scale, y + (6 + row * 2) * scale, 5 * scale, scale, COLOR_ACCENT);
        }
        return;
    }
    if (std::strcmp(emoji, "⏰") == 0) {
        fill_circle(center_x, center_y + scale, 6 * scale, COLOR_ACCENT);
        fill_circle(center_x, center_y + scale, 4 * scale, COLOR_BACKGROUND);
        draw_line(center_x, center_y + scale, center_x, center_y - 2 * scale, COLOR_TEXT, scale);
        draw_line(center_x, center_y + scale, center_x + 2 * scale, center_y + 2 * scale, COLOR_TEXT, scale);
        return;
    }
    if (std::strcmp(emoji, "🎮") == 0) {
        fill_rect(x + 2 * scale, y + 5 * scale, 12 * scale, 7 * scale, COLOR_ACCENT);
        fill_rect(x + 5 * scale, y + 7 * scale, 2 * scale, 2 * scale, COLOR_BACKGROUND);
        fill_rect(x + 4 * scale, y + 6 * scale, 4 * scale, 1 * scale, COLOR_BACKGROUND);
        fill_circle(x + 11 * scale, y + 8 * scale, scale, COLOR_FEATURE);
        return;
    }
    if (std::strcmp(emoji, "📅") == 0 || std::strcmp(emoji, "🔔") == 0 ||
        std::strcmp(emoji, "⚠️") == 0) {
        if (std::strcmp(emoji, "📅") == 0) {
            fill_rect(x + 3 * scale, y + 3 * scale, 10 * scale, 11 * scale, COLOR_TEXT);
            fill_rect(x + 3 * scale, y + 5 * scale, 10 * scale, 2 * scale, COLOR_ACCENT);
            fill_rect(x + 5 * scale, y + 8 * scale, 2 * scale, 2 * scale, COLOR_FEATURE);
            fill_rect(x + 9 * scale, y + 8 * scale, 2 * scale, 2 * scale, COLOR_FEATURE);
        } else if (std::strcmp(emoji, "🔔") == 0) {
            fill_circle(center_x, y + 6 * scale, 4 * scale, COLOR_ACCENT);
            fill_rect(x + 5 * scale, y + 7 * scale, 8 * scale, 4 * scale, COLOR_ACCENT);
            fill_rect(x + 4 * scale, y + 11 * scale, 10 * scale, 2 * scale, COLOR_ACCENT);
            fill_circle(center_x, y + 14 * scale, scale, COLOR_FEATURE);
        } else {
            draw_line(center_x, y + 2 * scale, x + 14 * scale, y + 13 * scale, COLOR_ACCENT, scale);
            draw_line(x + 14 * scale, y + 13 * scale, x + 2 * scale, y + 13 * scale, COLOR_ACCENT, scale);
            draw_line(x + 2 * scale, y + 13 * scale, center_x, y + 2 * scale, COLOR_ACCENT, scale);
            fill_rect(center_x - scale / 2, y + 6 * scale, scale, 4 * scale, COLOR_FEATURE);
            fill_circle(center_x, y + 12 * scale, scale / 2 + 1, COLOR_FEATURE);
        }
        return;
    }
    if (std::strcmp(emoji, "🎙️") == 0) {
        fill_rect(center_x - 2 * scale, y + 2 * scale, 4 * scale, 8 * scale, COLOR_ACCENT);
        draw_line(center_x - 5 * scale, y + 8 * scale, center_x - 5 * scale,
            y + 10 * scale, COLOR_TEXT, scale);
        draw_line(center_x + 5 * scale, y + 8 * scale, center_x + 5 * scale,
            y + 10 * scale, COLOR_TEXT, scale);
        draw_line(center_x - 5 * scale, y + 10 * scale, center_x + 5 * scale,
            y + 10 * scale, COLOR_TEXT, scale);
        draw_line(center_x, y + 10 * scale, center_x, y + 14 * scale, COLOR_TEXT, scale);
        return;
    }
    const bool sad = std::strcmp(emoji, "😢") == 0;
    const bool sleeping = std::strcmp(emoji, "😴") == 0;
    const uint16_t face_color = COLOR_FACE;
    fill_circle(center_x, center_y, size / 2 - scale, face_color);
    if (sleeping) {
        draw_line(x + 4 * scale, y + 7 * scale, x + 6 * scale, y + 7 * scale, COLOR_FEATURE, scale);
        draw_line(x + 10 * scale, y + 7 * scale, x + 12 * scale, y + 7 * scale, COLOR_FEATURE, scale);
        draw_text("Z", x + 11 * scale, y, scale, COLOR_ACCENT, 1);
    } else if (std::strcmp(emoji, "🙂") == 0 || std::strcmp(emoji, "😊") == 0 ||
        std::strcmp(emoji, "😢") == 0) {
        fill_circle(x + 5 * scale, y + 6 * scale, scale, COLOR_FEATURE);
        fill_circle(x + 11 * scale, y + 6 * scale, scale, COLOR_FEATURE);
    } else {
        draw_text("?", x + 5 * scale, y + 5 * scale, scale, COLOR_TEXT, 1);
        return;
    }
    if (sad) {
        draw_line(x + 5 * scale, y + 12 * scale, center_x, y + 10 * scale, COLOR_FEATURE, scale);
        draw_line(center_x, y + 10 * scale, x + 11 * scale, y + 12 * scale, COLOR_FEATURE, scale);
    } else {
        draw_line(x + 5 * scale, y + 10 * scale, center_x, y + 13 * scale, COLOR_FEATURE, scale);
        draw_line(center_x, y + 13 * scale, x + 11 * scale, y + 10 * scale, COLOR_FEATURE, scale);
    }
}


void draw_game_indicator(int x, int y, FaceState face, uint32_t frame, uint16_t color) {
    if (face == FaceState::Listening) {
        for (int dot = 0; dot < 3; ++dot) {
            const int radius = ((frame / 4 + dot) % 3) == 0 ? 3 : 2;
            fill_circle(x + dot * 8, y + 4, radius, color);
        }
    } else if (face == FaceState::Speaking) {
        for (int bar = 0; bar < 4; ++bar) {
            const int height = 3 + static_cast<int>((frame * 3 + static_cast<uint32_t>(bar) * 5) % 9);
            fill_rect(x + bar * 6, y + 8 - height, 3, height, color);
        }
    } else if (face == FaceState::Thinking) {
        const int active = static_cast<int>(frame / 3 % 3);
        for (int dot = 0; dot < 3; ++dot) {
            fill_circle(x + dot * 8, y + 4, dot == active ? 3 : 1, color);
        }
    } else {
        fill_circle(x + 4, y + 4, 3, color);
    }
}

void draw_ring(int center_x, int center_y, int radius, uint16_t color, uint16_t background) {
    fill_circle(center_x, center_y, radius, color);
    fill_circle(center_x, center_y, radius - 3, background);
}

void draw_tic_tac_toe(const DeviceDisplay::GameView& view, int left, int top, int cell) {
    for (int line = 1; line < 3; ++line) {
        fill_rect(left + line * cell - 1, top, 2, cell * 3, COLOR_ACCENT);
        fill_rect(left, top + line * cell - 1, cell * 3, 2, COLOR_ACCENT);
    }
    for (int index = 0; index < 9; ++index) {
        const int cx = left + (index % 3) * cell + cell / 2;
        const int cy = top + (index / 3) * cell + cell / 2;
        const int arm = cell / 2 - 5;
        if (view.board[index] == 'X') {
            draw_line(cx - arm, cy - arm, cx + arm, cy + arm, COLOR_FACE, 3);
            draw_line(cx - arm, cy + arm, cx + arm, cy - arm, COLOR_FACE, 3);
        } else if (view.board[index] == 'O') {
            draw_ring(cx, cy, arm + 1, COLOR_TEXT, COLOR_BACKGROUND);
        } else {
            char number[2] = {static_cast<char>('1' + index), '\0'};
            draw_text(number, cx - 1, cy - 2, 1, COLOR_MUTED, 1);
        }
    }
}

void draw_hangman(const DeviceDisplay::GameView& view, int left, int top) {
    const uint16_t wood = COLOR_MUTED;
    fill_rect(left, top + 70, 50, 3, wood);
    fill_rect(left + 10, top, 3, 70, wood);
    fill_rect(left + 10, top, 30, 3, wood);
    fill_rect(left + 38, top, 2, 10, wood);
    const int m = view.mistakes;
    if (m >= 1) draw_ring(left + 39, top + 16, 6, COLOR_FACE, COLOR_BACKGROUND);
    if (m >= 2) fill_rect(left + 38, top + 22, 2, 22, COLOR_FACE);
    if (m >= 3) draw_line(left + 39, top + 26, left + 30, top + 36, COLOR_FACE, 2);
    if (m >= 4) draw_line(left + 39, top + 26, left + 48, top + 36, COLOR_FACE, 2);
    if (m >= 5) draw_line(left + 39, top + 44, left + 31, top + 58, COLOR_FACE, 2);
    if (m >= 6) draw_line(left + 39, top + 44, left + 47, top + 58, COLOR_FACE, 2);
}

void draw_game_screen() {
    const auto& view = display_model.game;
    const FaceState face = display_model.face;
    const uint32_t frame = display_model.animation_frame;
    const char* live = face == FaceState::Listening ? "LISTENING"
        : face == FaceState::Speaking ? "SPEAKING"
        : face == FaceState::Thinking ? "THINKING" : "YOUR MOVE";
    const uint16_t live_color = face == FaceState::Listening ? COLOR_FACE : COLOR_ACCENT;
    if (active_panel == Panel::Oled) {
        oled_frame_buffer.fill(0);
        fill_rect(0, 0, OLED_WIDTH, 9, COLOR_TEXT);
        draw_text(view.title, 3, 2, 1, COLOR_BACKGROUND, 18);
        draw_game_indicator(100, 1, face, frame, COLOR_BACKGROUND);
        if (view.layout == DeviceDisplay::GameLayout::TicTacToe) {
            draw_tic_tac_toe(view, 3, 11, 17);
            draw_text(view.status, 62, 14, 1, COLOR_TEXT, 16);
            draw_text(view.line1, 62, 24, 1, COLOR_TEXT, 16);
            draw_text(live, 62, 54, 1, COLOR_TEXT, 12);
        } else {
            draw_text(view.layout == DeviceDisplay::GameLayout::Hangman ? view.masked : view.line1,
                3, 14, 1, COLOR_TEXT, 30);
            draw_text(view.status, 3, 26, 1, COLOR_TEXT, 30);
            draw_text(view.line2, 3, 36, 1, COLOR_TEXT, 30);
            draw_text(live, 3, 55, 1, COLOR_TEXT, 30);
        }
        return;
    }
    frame_buffer.fill(COLOR_BACKGROUND);
    fill_rect(0, 0, WIDTH, 18, COLOR_HEADER);
    draw_text(view.title, 5, 5, 1, COLOR_ACCENT, 18);
    draw_game_indicator(98, 5, face, frame, live_color);
    int y = 24;
    if (view.layout == DeviceDisplay::GameLayout::TicTacToe) {
        draw_tic_tac_toe(view, 19, y, 30);
        y += 96;
    } else if (view.layout == DeviceDisplay::GameLayout::Hangman) {
        draw_hangman(view, 36, y);
        draw_text(view.masked, 5, y + 78, 2, COLOR_TEXT, 15);
        y += 98;
    } else if (view.layout == DeviceDisplay::GameLayout::Quiz) {
        for (uint8_t row = 0; row < view.row_count && row < 8; ++row) {
            const bool choice = view.rows[row][1] == ' ' && view.rows[row][0] >= 'A' && view.rows[row][0] <= 'D';
            draw_text(view.rows[row], 5, y + 2, 1, choice ? COLOR_ACCENT : COLOR_TEXT, 30);
            y += 11;
        }
        y += 4;
    } else {
        draw_text(view.line1, 5, y + 4, 2, COLOR_TEXT, 15);
        draw_text(view.line2, 5, y + 22, 2, COLOR_ACCENT, 15);
        y += 44;
    }
    draw_text(view.status, 5, y, 1, COLOR_FACE, 30);
    y += 10;
    if (view.layout == DeviceDisplay::GameLayout::Quiz) {
        // question and choices already fill the screen
    } else if (view.layout == DeviceDisplay::GameLayout::Text) {
        draw_text(view.line3, 5, y, 1, COLOR_TEXT, 30);
        y += 10;
    } else {
        draw_text(view.line1, 5, y, 1, COLOR_TEXT, 30);
        y += 10;
        draw_text(view.line2, 5, y, 1, COLOR_MUTED, 30);
        y += 10;
    }
    if (view.show_scores) {
        char score[32];
        std::snprintf(score, sizeof(score), "YOU %u  BUDDY %u", view.score_you, view.score_ai);
        draw_text(score, 5, 148, 1, COLOR_ACCENT, 30);
    } else {
        draw_text(live, 5, 148, 1, COLOR_MUTED, 30);
    }
}

void draw_screen() {
    if (display_model.game_active) {
        draw_game_screen();
        return;
    }
    if (active_panel == Panel::Oled) {
        oled_frame_buffer.fill(0);
        fill_rect(0, 0, OLED_WIDTH, 9, COLOR_TEXT);
        draw_text("BUDDY", 3, 2, 1, COLOR_BACKGROUND, 8);
        draw_face(display_model.face, display_model.blink, display_model.animation_frame);
        draw_emoji(display_model.emoji.data(), 88, 1, 16);
        draw_text(display_model.title.data(), 3, 12, 1, COLOR_TEXT, 25);
        draw_text(display_model.detail.data(), 3, 21, 1, COLOR_TEXT, 28);
        draw_line(3, 28, 124, 28, COLOR_TEXT, 1);
        int y = 31;
        for (size_t index = 0; index < display_model.item_count && index < 3; ++index) {
            draw_text(display_model.items[index].data(), 3, y, 1, COLOR_TEXT, 29);
            y += 8;
        }
        char counts[40];
        std::snprintf(
            counts, sizeof(counts), "T:%u R:%u N:%u A:%u",
            display_model.todos, display_model.reminders,
            display_model.notes, display_model.alarms);
        draw_text(counts, 3, 57, 1, COLOR_TEXT, 29);
        return;
    }
    frame_buffer.fill(COLOR_BACKGROUND);
    fill_rect(0, 0, WIDTH, 18, COLOR_HEADER);
    draw_text("BUDDY", 5, 5, 1, COLOR_ACCENT, 8);
    draw_emoji(display_model.emoji.data(), 108, 1, 16);
    draw_face(display_model.face, display_model.blink, display_model.animation_frame);
    draw_text(display_model.title.data(), 5, 29, 2, COLOR_TEXT, 15);
    draw_text(display_model.detail.data(), 5, 44, 1, COLOR_ACCENT, 29);
    draw_line(5, 54, 122, 54, COLOR_HEADER, 1);

    int y = 62;
    // Compact line spacing keeps long lists and wrapped news headlines on a 128x160 panel.
    const bool compact = display_model.item_count > 4;
    for (size_t index = 0; index < display_model.item_count && index < MAX_DISPLAY_ITEMS; ++index) {
        draw_text(display_model.items[index].data(), 4, y, 1, COLOR_TEXT, 30);
        y += compact ? 11 : 16;
    }
    if (compact) {
        return;
    }
    if (std::strcmp(display_model.page.data(), "home") == 0) {
        char counts[40];
        std::snprintf(
            counts, sizeof(counts), "T:%u R:%u N:%u A:%u",
            display_model.todos, display_model.reminders,
            display_model.notes, display_model.notifications);
        draw_text(counts, 5, 127, 1, COLOR_MUTED, 29);
        char alarm_count[20];
        std::snprintf(alarm_count, sizeof(alarm_count), "ALARMS %u", display_model.alarms);
        draw_text(alarm_count, 5, 141, 1, COLOR_ACCENT, 28);
    } else {
        char footer[40];
        std::snprintf(
            footer, sizeof(footer), "T:%u R:%u N:%u A:%u",
            display_model.todos, display_model.reminders,
            display_model.notes, display_model.alarms);
        draw_text(footer, 5, 141, 1, COLOR_MUTED, 29);
    }
}

esp_err_t initialize_oled() {
    i2c_master_bus_config_t bus_config{};
    bus_config.i2c_port = HardwareConfig::OLED::I2C_PORT;
    bus_config.sda_io_num = HardwareConfig::OLED::SDA;
    bus_config.scl_io_num = HardwareConfig::OLED::SCL;
    bus_config.clk_source = I2C_CLK_SRC_DEFAULT;
    bus_config.glitch_ignore_cnt = 7;
    bus_config.flags.enable_internal_pullup = true;
    ESP_RETURN_ON_ERROR(
        i2c_new_master_bus(&bus_config, &i2c_bus),         OLED_TAG, "Cannot initialize OLED I2C bus");

    uint16_t address = OLED_ADDRESS_PRIMARY;
    esp_err_t probe_result = i2c_master_probe(i2c_bus, address, 100);
    if (probe_result != ESP_OK) {
        address = OLED_ADDRESS_SECONDARY;
        probe_result = i2c_master_probe(i2c_bus, address, 100);
    }
    if (probe_result != ESP_OK) {
        ESP_LOGI(OLED_TAG, "No SSD1306 OLED found at I2C addresses 0x3C or 0x3D");
        ESP_RETURN_ON_ERROR(i2c_del_master_bus(i2c_bus), OLED_TAG, "Cannot release unused OLED I2C bus");
        i2c_bus = nullptr;
        return ESP_ERR_NOT_FOUND;
    }

    i2c_device_config_t device_config{};
    device_config.dev_addr_length = I2C_ADDR_BIT_LEN_7;
    device_config.device_address = address;
    device_config.scl_speed_hz = 400000;
    const esp_err_t add_device_result =
        i2c_master_bus_add_device(i2c_bus, &device_config, &oled_device);
    if (add_device_result != ESP_OK) {
        ESP_LOGE(OLED_TAG, "Cannot register SSD1306 OLED: %s", esp_err_to_name(add_device_result));
        const esp_err_t cleanup_result = i2c_del_master_bus(i2c_bus);
        i2c_bus = nullptr;
        if (cleanup_result != ESP_OK) {
            ESP_LOGE(OLED_TAG, "Cannot release I2C bus after OLED registration failure: %s",
                esp_err_to_name(cleanup_result));
        }
        return add_device_result;
    }

    const uint8_t initialization[] = {
        0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40,
        0x8D, 0x14, 0x20, 0x00, 0xA1, 0xC8, 0xDA, 0x12,
        0x81, 0x7F, 0xD9, 0xF1, 0xDB, 0x40, 0xA4, 0xA6, 0xAF,
    };
    for (const uint8_t value : initialization) {
        const uint8_t packet[] = {0x00, value};
        const esp_err_t result = i2c_master_transmit(oled_device, packet, sizeof(packet), 100);
        if (result != ESP_OK) {
            ESP_LOGE(OLED_TAG, "SSD1306 initialization failed: %s", esp_err_to_name(result));
            const esp_err_t remove_result = i2c_master_bus_rm_device(oled_device);
            oled_device = nullptr;
            if (remove_result != ESP_OK) {
                ESP_LOGE(OLED_TAG, "Cannot remove failed OLED device: %s", esp_err_to_name(remove_result));
            }
            const esp_err_t cleanup_result = i2c_del_master_bus(i2c_bus);
            i2c_bus = nullptr;
            if (cleanup_result != ESP_OK) {
                ESP_LOGE(OLED_TAG, "Cannot release I2C bus after OLED init failure: %s",
                    esp_err_to_name(cleanup_result));
            }
            return result;
        }
    }
    active_panel = Panel::Oled;
    ESP_LOGI(OLED_TAG, "SSD1306 OLED detected at I2C address 0x%02X (128x64)", address);
    return ESP_OK;
}

void display_task(void*);

esp_err_t start_renderer() {
    display_mutex = xSemaphoreCreateMutex();
    if (display_mutex == nullptr) return ESP_ERR_NO_MEM;
    draw_screen();
    ESP_RETURN_ON_ERROR(flush_frame(), TAG, "Cannot draw initial display state");
    const char* task_name = active_panel == Panel::Oled ? "oled_animation" : "tft_animation";
    if (xTaskCreate(display_task, task_name, 4096, nullptr, 3, nullptr) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void display_task(void*) {
    TickType_t last_blink = xTaskGetTickCount();
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(100));
        if (xSemaphoreTake(display_mutex, pdMS_TO_TICKS(20)) != pdTRUE) continue;
        const TickType_t now = xTaskGetTickCount();
        bool update = display_model.dirty;
        display_model.dirty = false;
        if (display_model.face == FaceState::Speaking && display_model.speaking_until != 0 &&
            static_cast<int32_t>(now - display_model.speaking_until) >= 0) {
            display_model.face = FaceState::Idle;
            display_model.speaking_until = 0;
            display_model.dirty = true;
            update = true;
        }
        const bool full_refresh = update;
        ++display_model.animation_frame;
        if (display_model.game_active) {
            update = update || display_model.animation_frame % 3 == 0;
        } else if (display_model.face == FaceState::Speaking) {
            update = update || display_model.animation_frame % 3 == 0;
        } else if (display_model.face == FaceState::Listening ||
            display_model.face == FaceState::Thinking) {
            update = update || display_model.animation_frame % 8 == 0;
        } else if (now - last_blink >= pdMS_TO_TICKS(2800)) {
            display_model.blink = true;
            update = true;
            last_blink = now;
        } else if (display_model.blink) {
            display_model.blink = false;
            update = true;
        }
        if (update) {
            draw_screen();
            const esp_err_t result = active_panel == Panel::Tft && !full_refresh && !display_model.game_active
                ? flush_tft_region(80, 8, 48, 48)
                : flush_frame();
            if (result != ESP_OK) {
                ESP_LOGE(TAG, "Cannot update animated display: %s", esp_err_to_name(result));
            }
        }
        xSemaphoreGive(display_mutex);
    }
}

template <size_t N>
void copy_text(std::array<char, N>& destination, const char* source) {
    destination.fill('\0');
    if (source == nullptr) return;
    std::strncpy(destination.data(), source, destination.size() - 1);
}

esp_err_t set_addressing() {
    const uint8_t color_mode = 0x05;
    const uint8_t orientation = 0xC8;
    ESP_RETURN_ON_ERROR(command_data(0x3A, &color_mode, sizeof(color_mode)), TAG, "Cannot set RGB565 mode");
    ESP_RETURN_ON_ERROR(command_data(0x36, &orientation, sizeof(orientation)), TAG, "Cannot set display orientation");
    return ESP_OK;
}

}

namespace DeviceDisplay {

esp_err_t initialize() {
    if constexpr (HardwareConfig::Features::ENABLE_OLED) {
        const esp_err_t oled_result = initialize_oled();
        if (oled_result == ESP_OK) {
            ESP_RETURN_ON_ERROR(start_renderer(), OLED_TAG, "Cannot start OLED renderer");
            ESP_LOGI(OLED_TAG, "Automatic display selection: SSD1306 OLED");
            return ESP_OK;
        }
        if (oled_result != ESP_ERR_NOT_FOUND) {
            ESP_LOGW(OLED_TAG, "OLED initialization failed; trying ST7735 TFT: %s",
                esp_err_to_name(oled_result));
        }
    }

    if constexpr (!HardwareConfig::Features::ENABLE_TFT) return ESP_ERR_NOT_FOUND;
    active_panel = Panel::Tft;
    gpio_config_t control_pins{};
    control_pins.pin_bit_mask =
        (1ULL << HardwareConfig::TFT::DC) | (1ULL << HardwareConfig::TFT::RST);
    control_pins.mode = GPIO_MODE_OUTPUT;
    control_pins.pull_up_en = GPIO_PULLUP_DISABLE;
    control_pins.pull_down_en = GPIO_PULLDOWN_DISABLE;
    control_pins.intr_type = GPIO_INTR_DISABLE;
    ESP_RETURN_ON_ERROR(gpio_config(&control_pins), TAG, "Cannot configure display control pins");

    spi_bus_config_t bus_config{};
    bus_config.mosi_io_num = HardwareConfig::TFT::MOSI;
    bus_config.miso_io_num = -1;
    bus_config.sclk_io_num = HardwareConfig::TFT::SCK;
    bus_config.quadwp_io_num = -1;
    bus_config.quadhd_io_num = -1;
    bus_config.max_transfer_sz = PIXELS_PER_ROW * 2;
    esp_err_t result = spi_bus_initialize(HardwareConfig::TFT::SPI_HOST, &bus_config, SPI_DMA_CH_AUTO);
    if (result != ESP_OK) return result;

    spi_device_interface_config_t device_config{};
    device_config.clock_speed_hz = 20 * 1000 * 1000;
    device_config.mode = 0;
    device_config.spics_io_num = HardwareConfig::TFT::CS;
    device_config.queue_size = 1;
    result = spi_bus_add_device(HardwareConfig::TFT::SPI_HOST, &device_config, &display_device);
    if (result != ESP_OK) return result;

    ESP_RETURN_ON_ERROR(gpio_set_level(HardwareConfig::TFT::RST, 0), TAG, "Cannot reset display");
    vTaskDelay(pdMS_TO_TICKS(20));
    ESP_RETURN_ON_ERROR(gpio_set_level(HardwareConfig::TFT::RST, 1), TAG, "Cannot release display reset");
    vTaskDelay(pdMS_TO_TICKS(120));

    ESP_RETURN_ON_ERROR(command(0x01), TAG, "Cannot reset ST7735");
    vTaskDelay(pdMS_TO_TICKS(150));
    ESP_RETURN_ON_ERROR(command(0x11), TAG, "Cannot wake ST7735");
    vTaskDelay(pdMS_TO_TICKS(120));

    const uint8_t frame_rate[] = {0x01, 0x2C, 0x2D};
    const uint8_t frame_rate_idle[] = {0x01, 0x2C, 0x2D};
    const uint8_t frame_rate_partial[] = {0x01, 0x2C, 0x2D, 0x01, 0x2C, 0x2D};
    const uint8_t inversion_control = 0x07;
    const uint8_t power_1[] = {0xA2, 0x02, 0x84};
    const uint8_t power_2 = 0xC5;
    const uint8_t power_3[] = {0x0A, 0x00};
    const uint8_t power_4[] = {0x8A, 0x2A};
    const uint8_t power_5[] = {0x8A, 0xEE};
    const uint8_t vcom = 0x0E;
    const uint8_t gamma_positive[] = {
        0x02, 0x1C, 0x07, 0x12, 0x37, 0x32, 0x29, 0x2D,
        0x29, 0x25, 0x2B, 0x39, 0x00, 0x01, 0x03, 0x10,
    };
    const uint8_t gamma_negative[] = {
        0x03, 0x1D, 0x07, 0x06, 0x2E, 0x2C, 0x29, 0x2D,
        0x2E, 0x2E, 0x37, 0x3F, 0x00, 0x00, 0x02, 0x10,
    };
    ESP_RETURN_ON_ERROR(command_data(0xB1, frame_rate, sizeof(frame_rate)), TAG, "Cannot set frame rate");
    ESP_RETURN_ON_ERROR(command_data(0xB2, frame_rate_idle, sizeof(frame_rate_idle)), TAG, "Cannot set idle frame rate");
    ESP_RETURN_ON_ERROR(command_data(0xB3, frame_rate_partial, sizeof(frame_rate_partial)), TAG, "Cannot set partial frame rate");
    ESP_RETURN_ON_ERROR(command_data(0xB4, &inversion_control, sizeof(inversion_control)), TAG, "Cannot set inversion control");
    ESP_RETURN_ON_ERROR(command_data(0xC0, power_1, sizeof(power_1)), TAG, "Cannot set power control");
    ESP_RETURN_ON_ERROR(command_data(0xC1, &power_2, sizeof(power_2)), TAG, "Cannot set power control");
    ESP_RETURN_ON_ERROR(command_data(0xC2, power_3, sizeof(power_3)), TAG, "Cannot set power control");
    ESP_RETURN_ON_ERROR(command_data(0xC3, power_4, sizeof(power_4)), TAG, "Cannot set power control");
    ESP_RETURN_ON_ERROR(command_data(0xC4, power_5, sizeof(power_5)), TAG, "Cannot set power control");
    ESP_RETURN_ON_ERROR(command_data(0xC5, &vcom, sizeof(vcom)), TAG, "Cannot set VCOM");
    ESP_RETURN_ON_ERROR(command(0x20), TAG, "Cannot disable display inversion");
    ESP_RETURN_ON_ERROR(set_addressing(), TAG, "Cannot configure display addressing");
    ESP_RETURN_ON_ERROR(command_data(0xE0, gamma_positive, sizeof(gamma_positive)), TAG, "Cannot set positive gamma");
    ESP_RETURN_ON_ERROR(command_data(0xE1, gamma_negative, sizeof(gamma_negative)), TAG, "Cannot set negative gamma");
    ESP_RETURN_ON_ERROR(command(0x13), TAG, "Cannot enter normal display mode");
    vTaskDelay(pdMS_TO_TICKS(10));
    ESP_RETURN_ON_ERROR(command(0x29), TAG, "Cannot turn display on");
    vTaskDelay(pdMS_TO_TICKS(100));

    ESP_RETURN_ON_ERROR(start_renderer(), TAG, "Cannot start TFT renderer");
    ESP_LOGI(TAG, "Automatic display selection: ST7735 TFT, 128x160 on SPI2");
    return ESP_OK;
}

esp_err_t set_state(const char* state) {
    if (active_panel == Panel::None || state == nullptr) return ESP_ERR_INVALID_STATE;
    FaceState face = FaceState::Idle;
    if (std::strcmp(state, "listening") == 0) face = FaceState::Listening;
    else if (std::strcmp(state, "thinking") == 0) face = FaceState::Thinking;
    else if (std::strcmp(state, "speaking") == 0) face = FaceState::Speaking;
    else if (std::strcmp(state, "happy") == 0) face = FaceState::Happy;
    else if (std::strcmp(state, "sad") == 0) face = FaceState::Sad;
    else if (std::strcmp(state, "error") == 0) face = FaceState::Error;
    else if (std::strcmp(state, "sleeping") == 0) face = FaceState::Sleeping;
    else if (std::strcmp(state, "idle") != 0) return ESP_ERR_INVALID_ARG;
    if (display_mutex == nullptr || xSemaphoreTake(display_mutex, pdMS_TO_TICKS(100)) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }
    display_model.face = face;
    display_model.speaking_until = 0;
    display_model.dirty = true;
    xSemaphoreGive(display_mutex);
    return ESP_OK;
}

esp_err_t set_game_view(const GameView& view) {
    if (display_mutex == nullptr || active_panel == Panel::None) return ESP_ERR_INVALID_STATE;
    if (xSemaphoreTake(display_mutex, pdMS_TO_TICKS(100)) != pdTRUE) return ESP_ERR_TIMEOUT;
    display_model.game = view;
    display_model.game_active = true;
    display_model.dirty = true;
    xSemaphoreGive(display_mutex);
    return ESP_OK;
}

esp_err_t clear_game_view() {
    if (display_mutex == nullptr || active_panel == Panel::None) return ESP_ERR_INVALID_STATE;
    if (xSemaphoreTake(display_mutex, pdMS_TO_TICKS(100)) != pdTRUE) return ESP_ERR_TIMEOUT;
    display_model.game_active = false;
    display_model.dirty = true;
    xSemaphoreGive(display_mutex);
    return ESP_OK;
}

esp_err_t set_speaking_for(uint32_t duration_ms) {
    if (display_mutex == nullptr || duration_ms == 0) return ESP_ERR_INVALID_ARG;
    if (xSemaphoreTake(display_mutex, pdMS_TO_TICKS(100)) != pdTRUE) return ESP_ERR_TIMEOUT;
    display_model.face = FaceState::Speaking;
    display_model.speaking_until = xTaskGetTickCount() + pdMS_TO_TICKS(duration_ms);
    display_model.dirty = true;
    xSemaphoreGive(display_mutex);
    return ESP_OK;
}

esp_err_t set_dashboard(
    const char* page,
    const char* title,
    const char* detail,
    const char* const* items,
    size_t item_count,
    uint16_t todos,
    uint16_t reminders,
    uint16_t notes,
    uint16_t notifications,
    uint16_t alarms,
    const char* emoji) {
    if (display_mutex == nullptr || page == nullptr || title == nullptr || detail == nullptr ||
        item_count > MAX_DISPLAY_ITEMS || (item_count > 0 && items == nullptr)) {
        return ESP_ERR_INVALID_ARG;
    }
    if (xSemaphoreTake(display_mutex, pdMS_TO_TICKS(100)) != pdTRUE) return ESP_ERR_TIMEOUT;
    copy_text(display_model.page, page);
    copy_text(display_model.title, title);
    copy_text(display_model.detail, detail);
    copy_text(display_model.emoji, emoji);
    for (auto& item : display_model.items) item.fill('\0');
    display_model.item_count = item_count;
    for (size_t index = 0; index < item_count; ++index) copy_text(display_model.items[index], items[index]);
    display_model.todos = todos;
    display_model.reminders = reminders;
    display_model.notes = notes;
    display_model.notifications = notifications;
    display_model.alarms = alarms;
    display_model.dirty = true;
    xSemaphoreGive(display_mutex);
    return ESP_OK;
}

}

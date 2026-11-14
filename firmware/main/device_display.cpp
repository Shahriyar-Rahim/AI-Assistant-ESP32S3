#include "device_display.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstring>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "hardware_config.h"
#include "dashboard_layout.h"
#include "display_lifecycle.h"
#include "face_layout.h"
#include "lvgl.h"

namespace
{

    constexpr char TAG[] = "device_display";

    constexpr size_t WIDTH = DashboardLayout::width;
    constexpr size_t HEIGHT = DashboardLayout::height;
    constexpr size_t PIXELS_PER_ROW = WIDTH;

    // Correct hardware offsets for 1.8" ST7735 128x160 TFT modules
    constexpr uint8_t ST7735_X_OFFSET = 0;
    constexpr uint8_t ST7735_Y_OFFSET = 0;

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

    enum class Panel
    {
        None,
        Oled,
        Tft
    };
    Panel active_panel = Panel::None;
    spi_device_handle_t display_device = nullptr;
    i2c_master_bus_handle_t i2c_bus = nullptr;
    i2c_master_dev_handle_t oled_device = nullptr;

    std::array<uint16_t, WIDTH * HEIGHT> frame_buffer{};

    // Heap-allocated DMA safe memory buffer for SPI transfers
    uint8_t *dma_row_buffer = nullptr;

    std::array<uint8_t, OLED_WIDTH * OLED_HEIGHT / 8> oled_frame_buffer{};
    std::array<uint8_t, 17> oled_transfer_buffer{};
    SemaphoreHandle_t display_mutex = nullptr;
    lv_disp_draw_buf_t lv_draw_buffer;
    lv_disp_drv_t lv_display_driver;
    lv_disp_t *lv_display = nullptr;
    std::array<lv_color_t, WIDTH * 8> lv_pixels{};
    lv_obj_t *lv_title = nullptr;
    lv_obj_t *lv_detail = nullptr;
    lv_obj_t *lv_items[MAX_DISPLAY_ITEMS]{};
    lv_obj_t *lv_footer = nullptr;
    lv_obj_t *lv_face = nullptr;
    lv_obj_t *lv_eyes[2]{};
    lv_obj_t *lv_mouth = nullptr;
    lv_obj_t *lv_mouth_line = nullptr;
    lv_point_t lv_mouth_points[3]{};
    DisplayLifecycle display_lifecycle;

    enum class FaceState
    {
        Idle,
        Listening,
        Thinking,
        Speaking,
        Happy,
        Excited,
        Curious,
        Surprised,
        Sad,
        Error,
        Sleeping,
    };

    struct DisplayModel
    {
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

    esp_err_t write_bytes(const uint8_t *data, size_t length, bool is_data)
    {
        if (display_device == nullptr || data == nullptr || length == 0)
            return ESP_ERR_INVALID_STATE;
        ESP_RETURN_ON_ERROR(gpio_set_level(HardwareConfig::TFT::DC, is_data ? 1 : 0),
                            TAG, "Cannot set display data/command pin");
        spi_transaction_t transaction{};
        transaction.length = length * 8;
        transaction.tx_buffer = data;
        return spi_device_transmit(display_device, &transaction);
    }

    esp_err_t command(uint8_t value)
    {
        return write_bytes(&value, sizeof(value), false);
    }

    esp_err_t data(const uint8_t *values, size_t length)
    {
        return write_bytes(values, length, true);
    }

    esp_err_t command_data(uint8_t opcode, const uint8_t *values, size_t length)
    {
        ESP_RETURN_ON_ERROR(command(opcode), TAG, "Cannot send ST7735 command");
        if (length != 0)
            ESP_RETURN_ON_ERROR(data(values, length), TAG, "Cannot send ST7735 data");
        return ESP_OK;
    }

    void set_pixel(int x, int y, uint16_t color)
    {
        if (active_panel == Panel::Oled)
        {
            if (x < 0 || x >= static_cast<int>(OLED_WIDTH) || y < 0 ||
                y >= static_cast<int>(OLED_HEIGHT))
                return;
            const size_t index = static_cast<size_t>(x) +
                                 (static_cast<size_t>(y) / 8) * OLED_WIDTH;
            if (color == COLOR_BACKGROUND)
            {
                oled_frame_buffer[index] &= static_cast<uint8_t>(~(1U << (y % 8)));
            }
            else
            {
                oled_frame_buffer[index] |= static_cast<uint8_t>(1U << (y % 8));
            }
        }
        else if (active_panel == Panel::Tft &&
                 x >= 0 && x < static_cast<int>(WIDTH) && y >= 0 && y < static_cast<int>(HEIGHT))
        {
            frame_buffer[static_cast<size_t>(y) * WIDTH + static_cast<size_t>(x)] = color;
        }
    }

    void fill_rect(int x, int y, int width, int height, uint16_t color)
    {
        for (int row = y; row < y + height; ++row)
        {
            for (int column = x; column < x + width; ++column)
                set_pixel(column, row, color);
        }
    }

    void fill_circle(int center_x, int center_y, int radius, uint16_t color)
    {
        const int radius_squared = radius * radius;
        for (int y = -radius; y <= radius; ++y)
        {
            for (int x = -radius; x <= radius; ++x)
            {
                if (x * x + y * y <= radius_squared)
                    set_pixel(center_x + x, center_y + y, color);
            }
        }
    }

    void draw_line(int x0, int y0, int x1, int y1, uint16_t color, int thickness = 1)
    {
        const int dx = x1 >= x0 ? x1 - x0 : x0 - x1;
        const int sx = x0 < x1 ? 1 : -1;
        const int dy = -(y1 >= y0 ? y1 - y0 : y0 - y1);
        const int sy = y0 < y1 ? 1 : -1;
        int error = dx + dy;
        while (true)
        {
            for (int offset = 0; offset < thickness; ++offset)
            {
                set_pixel(x0, y0 + offset, color);
            }
            if (x0 == x1 && y0 == y1)
                break;
            const int doubled = 2 * error;
            if (doubled >= dy)
            {
                error += dy;
                x0 += sx;
            }
            if (doubled <= dx)
            {
                error += dx;
                y0 += sy;
            }
        }
    }

    esp_err_t flush_tft_region(size_t x, size_t y, size_t width, size_t height)
    {
        if (active_panel != Panel::Tft || width == 0 || height == 0 ||
            x + width > WIDTH || y + height > HEIGHT || dma_row_buffer == nullptr)
        {
            return ESP_ERR_INVALID_ARG;
        }

        const uint16_t x_start = static_cast<uint16_t>(x + ST7735_X_OFFSET);
        const uint16_t x_end = static_cast<uint16_t>(x + width - 1 + ST7735_X_OFFSET);
        const uint16_t y_start = static_cast<uint16_t>(y + ST7735_Y_OFFSET);
        const uint16_t y_end = static_cast<uint16_t>(y + height - 1 + ST7735_Y_OFFSET);

        const uint8_t columns[] = {
            static_cast<uint8_t>(x_start >> 8), static_cast<uint8_t>(x_start & 0xFF),
            static_cast<uint8_t>(x_end >> 8), static_cast<uint8_t>(x_end & 0xFF)};
        const uint8_t rows[] = {
            static_cast<uint8_t>(y_start >> 8), static_cast<uint8_t>(y_start & 0xFF),
            static_cast<uint8_t>(y_end >> 8), static_cast<uint8_t>(y_end & 0xFF)};

        ESP_RETURN_ON_ERROR(command_data(0x2A, columns, sizeof(columns)), TAG, "Cannot set display columns");
        ESP_RETURN_ON_ERROR(command_data(0x2B, rows, sizeof(rows)), TAG, "Cannot set display rows");
        ESP_RETURN_ON_ERROR(command(0x2C), TAG, "Cannot start display memory write");

        for (size_t row = y; row < y + height; ++row)
        {
            for (size_t column = 0; column < width; ++column)
            {
                const uint16_t pixel = frame_buffer[row * WIDTH + x + column];
                dma_row_buffer[column * 2] = static_cast<uint8_t>(pixel >> 8);
                dma_row_buffer[column * 2 + 1] = static_cast<uint8_t>(pixel & 0xFF);
            }
            ESP_RETURN_ON_ERROR(data(dma_row_buffer, width * 2), TAG, "Cannot transfer display row");
        }
        return ESP_OK;
    }

    esp_err_t flush_frame()
    {
        if (active_panel == Panel::Oled)
        {
            for (uint8_t page = 0; page < OLED_HEIGHT / 8; ++page)
            {
                const uint8_t setup_pkt[] = {0x00, static_cast<uint8_t>(0xB0 | page), 0x00, 0x10};
                ESP_RETURN_ON_ERROR(
                    i2c_master_transmit(oled_device, setup_pkt, sizeof(setup_pkt), 100),
                    TAG, "Cannot set SSD1306 page address");

                for (size_t offset = 0; offset < OLED_WIDTH; offset += 16)
                {
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

    void draw_face(FaceState state, bool blink, uint32_t frame);
    void draw_emoji(const char *emoji, int x, int y, int size);

    void lv_flush(lv_disp_drv_t *driver, const lv_area_t *area, lv_color_t *pixels)
    {
        for (lv_coord_t y = area->y1; y <= area->y2; ++y)
        {
            for (lv_coord_t x = area->x1; x <= area->x2; ++x)
            {
                const lv_color_t color = *pixels++;
                const uint16_t rgb565 = static_cast<uint16_t>(
                    (LV_COLOR_GET_R(color) << 11) | (LV_COLOR_GET_G(color) << 5) | LV_COLOR_GET_B(color));
                const uint16_t target = active_panel == Panel::Oled
                                            ? (rgb565 == COLOR_BACKGROUND || rgb565 == 0 ? COLOR_BACKGROUND : COLOR_TEXT)
                                            : rgb565;
                set_pixel(x, y, target);
            }
        }
        lv_disp_flush_ready(driver);
    }

    lv_obj_t *make_label(lv_obj_t *screen, int x, int y, int width, lv_color_t color)
    {
        lv_obj_t *label = lv_label_create(screen);
        lv_obj_set_pos(label, x, y);
        lv_obj_set_width(label, width);
        lv_obj_set_height(label, DashboardLayout::font_height);
        lv_label_set_long_mode(label, LV_LABEL_LONG_DOT);
        lv_obj_set_style_text_color(label, color, 0);
        return label;
    }

    lv_obj_t *make_face_shape(lv_obj_t *parent, uint32_t color)
    {
        lv_obj_t *object = lv_obj_create(parent);
        lv_obj_remove_style_all(object);
        lv_obj_clear_flag(object, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_style_bg_color(object, lv_color_hex(color), 0);
        lv_obj_set_style_bg_opa(object, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(object, LV_RADIUS_CIRCLE, 0);
        return object;
    }

    esp_err_t initialize_lv_widgets()
    {
        lv_init();
        if (lv_font_get_line_height(LV_FONT_DEFAULT) > DashboardLayout::font_height)
        {
            ESP_LOGE(TAG, "LVGL default font exceeds dashboard row height");
            return ESP_ERR_INVALID_SIZE;
        }
        lv_disp_draw_buf_init(&lv_draw_buffer, lv_pixels.data(), nullptr, lv_pixels.size());
        lv_disp_drv_init(&lv_display_driver);
        lv_display_driver.hor_res = WIDTH;
        lv_display_driver.ver_res = active_panel == Panel::Oled ? OLED_HEIGHT : HEIGHT;
        lv_display_driver.flush_cb = lv_flush;
        lv_display_driver.draw_buf = &lv_draw_buffer;
        lv_display = lv_disp_drv_register(&lv_display_driver);
        if (lv_display == nullptr)
            return ESP_ERR_NO_MEM;
        lv_obj_t *screen = lv_disp_get_scr_act(lv_display);
        lv_obj_set_style_bg_color(screen, lv_color_hex(active_panel == Panel::Oled ? 0x000000 : 0x080c08), 0);
        lv_obj_clear_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
        const bool oled = active_panel == Panel::Oled;
        lv_title = make_label(screen, 3, oled ? 0 : DashboardLayout::title_y,
                              oled ? 96 : DashboardLayout::header_width, lv_color_hex(0xffffff));
        lv_detail = make_label(screen, 3, oled ? 15 : DashboardLayout::detail_y,
                               oled ? 96 : DashboardLayout::header_width, lv_color_hex(0x00ffff));
        if (!oled)
        {
            lv_obj_set_style_bg_color(lv_title, lv_color_hex(0x203b55), 0);
            lv_obj_set_style_bg_opa(lv_title, LV_OPA_COVER, 0);
            lv_obj_set_style_radius(lv_title, 3, 0);
        }
        for (size_t index = 0; index < MAX_DISPLAY_ITEMS; ++index)
        {
            const size_t row = index < DashboardLayout::visible_items ? index : 0;
            lv_items[index] = make_label(screen, 3, oled ? 30 :
                                         DashboardLayout::items_y + static_cast<int>(row) * DashboardLayout::row_height,
                                         oled ? 121 : DashboardLayout::item_width, lv_color_hex(0xffffff));
            if (!oled && index < DashboardLayout::visible_items)
            {
                lv_obj_set_style_bg_color(lv_items[index], lv_color_hex(index % 2 ? 0x142a37 : 0x1b3341), 0);
                lv_obj_set_style_bg_opa(lv_items[index], LV_OPA_COVER, 0);
                lv_obj_set_style_radius(lv_items[index], 3, 0);
            }
        }
        lv_footer = make_label(screen, 3, oled ? 50 : DashboardLayout::footer_y,
                               oled ? 122 : DashboardLayout::item_width, lv_color_hex(0xbdbdbd));
        if (!oled)
        {
            lv_face = make_face_shape(screen, 0xffe000);
            for (auto &eye : lv_eyes)
                eye = make_face_shape(lv_face, 0x101010);
            lv_mouth = make_face_shape(lv_face, 0x101010);
            lv_mouth_line = lv_line_create(lv_face);
            lv_obj_set_style_line_color(lv_mouth_line, lv_color_hex(0x101010), 0);
            lv_obj_set_style_line_width(lv_mouth_line, 3, 0);
            lv_obj_set_style_line_rounded(lv_mouth_line, true, 0);
        }
        return ESP_OK;
    }

    void render_lv_face(bool dashboard)
    {
        const bool bob = (display_model.face == FaceState::Listening || display_model.face == FaceState::Speaking) &&
                         (display_model.animation_frame / 6) % 2 == 0;
        const auto bounds = FaceLayout::bounds(dashboard, bob);
        const int size = bounds.size;
        lv_obj_set_size(lv_face, size, size);
        lv_obj_set_pos(lv_face, bounds.x, bounds.y);
        lv_obj_set_style_bg_color(lv_face,
                                  lv_color_hex(display_model.face == FaceState::Error ? 0xff3030 : 0xffe000), 0);
        const int eye_width = FaceLayout::eye_width(dashboard);
        const int eye_height = (display_model.blink || display_model.face == FaceState::Sleeping) ? 2 : eye_width;
        const int look = display_model.face == FaceState::Thinking || display_model.face == FaceState::Curious ? 2 : 0;
        for (int index = 0; index < 2; ++index)
        {
            lv_obj_set_size(lv_eyes[index], eye_width, eye_height);
            lv_obj_set_pos(lv_eyes[index], (index == 0 ? size / 4 : size * 3 / 4) - eye_width / 2 + look,
                           size * 2 / 5 - eye_height / 2);
        }
        const bool open = display_model.face == FaceState::Speaking ||
                          display_model.face == FaceState::Surprised ||
                          display_model.face == FaceState::Listening;
        if (open)
        {
            const int diameter = (dashboard ? 6 : 12) + static_cast<int>((display_model.animation_frame / 3) % 3);
            lv_obj_set_size(lv_mouth, diameter, diameter);
            lv_obj_set_pos(lv_mouth, (size - diameter) / 2, size * 7 / 10 - diameter / 2);
            lv_obj_clear_flag(lv_mouth, LV_OBJ_FLAG_HIDDEN);
            lv_obj_add_flag(lv_mouth_line, LV_OBJ_FLAG_HIDDEN);
        }
        else
        {
            const int half = FaceLayout::mouth_half_width(dashboard);
            const int baseline = size * 7 / 10;
            int curvature = dashboard ? 3 : 7;
            if (display_model.face == FaceState::Sad || display_model.face == FaceState::Error)
                curvature = -curvature;
            else if (display_model.face == FaceState::Thinking || display_model.face == FaceState::Curious ||
                     display_model.face == FaceState::Sleeping)
                curvature = 0;
            lv_mouth_points[0] = {static_cast<lv_coord_t>(size / 2 - half), static_cast<lv_coord_t>(baseline)};
            lv_mouth_points[1] = {static_cast<lv_coord_t>(size / 2), static_cast<lv_coord_t>(baseline + curvature)};
            lv_mouth_points[2] = {static_cast<lv_coord_t>(size / 2 + half), static_cast<lv_coord_t>(baseline)};
            lv_line_set_points(lv_mouth_line, lv_mouth_points, 3);
            lv_obj_add_flag(lv_mouth, LV_OBJ_FLAG_HIDDEN);
            lv_obj_clear_flag(lv_mouth_line, LV_OBJ_FLAG_HIDDEN);
        }
    }

    void render_lv_idle()
    {
        lv_obj_add_flag(lv_title, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(lv_detail, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(lv_footer, LV_OBJ_FLAG_HIDDEN);
        for (auto *item : lv_items)
            lv_obj_add_flag(item, LV_OBJ_FLAG_HIDDEN);
        render_lv_face(false);
        lv_obj_invalidate(lv_disp_get_scr_act(lv_display));
        lv_refr_now(lv_display);
    }

    void render_lv_dashboard()
    {
        lv_obj_clear_flag(lv_title, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(lv_detail, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(lv_footer, LV_OBJ_FLAG_HIDDEN);
        lv_label_set_text(lv_title, display_model.title.data());
        lv_label_set_text(lv_detail, display_model.detail.data());
        const size_t visible = active_panel == Panel::Oled ? 1 : DashboardLayout::visible_items;
        const size_t first = active_panel == Panel::Oled ? 0 :
            DashboardLayout::first_item(display_model.item_count, display_model.animation_frame);
        for (size_t index = 0; index < MAX_DISPLAY_ITEMS; ++index)
        {
            lv_obj_clear_flag(lv_items[index], LV_OBJ_FLAG_HIDDEN);
            const size_t item = first + index;
            lv_label_set_text(lv_items[index], index < visible && item < display_model.item_count
                                                   ? display_model.items[item].data() : "");
        }
        char counts[48];
        std::snprintf(counts, sizeof(counts), "T:%u R:%u N:%u A:%u", display_model.todos,
                      display_model.reminders, display_model.notes, display_model.alarms);
        lv_label_set_text(lv_footer, counts);
        render_lv_face(true);
        lv_obj_invalidate(lv_disp_get_scr_act(lv_display));
        lv_refr_now(lv_display);
    }

    std::array<uint8_t, 5> glyph(char value)
    {
        switch (static_cast<char>(std::toupper(static_cast<unsigned char>(value))))
        {
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

    void draw_text(const char *text, int x, int y, int scale, uint16_t color, size_t max_chars)
    {
        if (text == nullptr)
            return;
        size_t rendered = 0;
        for (const char *cursor = text; *cursor != '\0' && rendered < max_chars; ++cursor, ++rendered)
        {
            const auto rows = glyph(*cursor);
            for (int row = 0; row < 5; ++row)
            {
                for (int column = 0; column < 3; ++column)
                {
                    if ((rows[row] & (1U << (2 - column))) == 0)
                        continue;
                    fill_rect(x + column * scale, y + row * scale, scale, scale, color);
                }
            }
            x += 4 * scale;
        }
    }

    void draw_face(FaceState state, bool blink, uint32_t frame)
    {
        if (active_panel == Panel::Oled)
        {
            const uint16_t face_color = COLOR_TEXT;
            const int bob = (state == FaceState::Listening || state == FaceState::Speaking) &&
                                    (frame / 6) % 2 == 0
                                ? 1
                                : 0;
            fill_circle(113, 22 + bob, 10, face_color);
            if (blink || state == FaceState::Sleeping)
            {
                draw_line(109, 20 + bob, 111, 20 + bob, COLOR_BACKGROUND, 1);
                draw_line(115, 20 + bob, 117, 20 + bob, COLOR_BACKGROUND, 1);
            }

            else
            {
                fill_circle(110, 20 + bob, 1, COLOR_BACKGROUND);
                fill_circle(116, 20 + bob, 1, COLOR_BACKGROUND);
            }
            if (state == FaceState::Speaking)
            {
                fill_circle(113, 25 + bob, 1 + static_cast<int>((frame / 3) % 2), COLOR_BACKGROUND);
            }
            else if (state == FaceState::Sad || state == FaceState::Error)
            {
                draw_line(111, 27 + bob, 115, 27 + bob, COLOR_BACKGROUND, 1);
            }
            else
            {
                draw_line(110, 25 + bob, 112, 27 + bob, COLOR_BACKGROUND, 1);
                draw_line(112, 27 + bob, 115, 27 + bob, COLOR_BACKGROUND, 1);
                draw_line(115, 27 + bob, 117, 25 + bob, COLOR_BACKGROUND, 1);
            }
            return;
        }
        const uint16_t face_color = state == FaceState::Error ? 0xF800 : COLOR_FACE;
        const int bob = (state == FaceState::Listening || state == FaceState::Speaking) &&
                                (frame / 6) % 2 == 0
                            ? 1
                            : 0;
        constexpr int center = 138;
        fill_circle(center, 23 + bob, 19, face_color);
        const int eye_offset = state == FaceState::Thinking ? 2 : state == FaceState::Listening ? static_cast<int>((frame / 8) % 3) - 1
                                                                                                : 0;
        if (blink || state == FaceState::Sleeping)
        {
            draw_line(center - 7, 21 + bob, center - 3, 21 + bob, COLOR_FEATURE, 2);
            draw_line(center + 3, 21 + bob, center + 7, 21 + bob, COLOR_FEATURE, 2);
        }
        else
        {
            fill_circle(center - 5 + eye_offset, 21 + bob, 2, COLOR_FEATURE);
            fill_circle(center + 5 + eye_offset, 21 + bob, 2, COLOR_FEATURE);
        }
        switch (state)
        {
        case FaceState::Happy:
        case FaceState::Excited:
        case FaceState::Idle:
            draw_line(center - 6, 30 + bob, center - 2, 33 + bob, COLOR_FEATURE, 2);
            draw_line(center - 2, 33 + bob, center + 3, 33 + bob, COLOR_FEATURE, 2);
            draw_line(center + 3, 33 + bob, center + 7, 30 + bob, COLOR_FEATURE, 2);
            break;
        case FaceState::Sad:
        case FaceState::Error:
            draw_line(center - 6, 33 + bob, center - 2, 30 + bob, COLOR_FEATURE, 2);
            draw_line(center - 2, 30 + bob, center + 3, 30 + bob, COLOR_FEATURE, 2);
            draw_line(center + 3, 30 + bob, center + 7, 33 + bob, COLOR_FEATURE, 2);
            break;
        case FaceState::Speaking:
        case FaceState::Surprised:
            fill_circle(center, 32 + bob, 2 + static_cast<int>((frame / 3) % 4), COLOR_FEATURE);
            break;
        case FaceState::Listening:
            fill_circle(center, 32 + bob, 3, COLOR_FEATURE);
            fill_circle(center, 32 + bob, 1, COLOR_ACCENT);
            break;
        case FaceState::Thinking:
        case FaceState::Curious:
            if ((frame / 5) % 2 == 0)
                fill_circle(center + 11, 9, 2, COLOR_ACCENT);
            break;
        case FaceState::Sleeping:
            draw_line(center - 4, 31 + bob, center + 4, 31 + bob, COLOR_FEATURE, 2);
            break;
        }
    }

    void draw_idle_face()
    {
        const bool oled = active_panel == Panel::Oled;
        if (oled)
            oled_frame_buffer.fill(0);
        else
            frame_buffer.fill(COLOR_BACKGROUND);
        const int cx = oled ? 64 : 80;
        const int cy = oled ? 32 : 64;
        const int radius = oled ? 27 : 50;
        const int bob = (display_model.face == FaceState::Listening ||
                         display_model.face == FaceState::Speaking) &&
                                (display_model.animation_frame / 6) % 2 == 0 ? 1 : 0;
        const int y = cy + bob;
        const uint16_t face = oled ? COLOR_TEXT :
            display_model.face == FaceState::Error ? 0xF800 : COLOR_FACE;
        const uint16_t features = oled ? COLOR_BACKGROUND : COLOR_FEATURE;
        fill_circle(cx, y, radius, face);
        const int eye_x = oled ? 10 : 20;
        const int eye_y = y - (oled ? 7 : 12);
        const int eye_radius = oled ? 3 : 6;
        if (display_model.blink || display_model.face == FaceState::Sleeping)
        {
            draw_line(cx - eye_x - eye_radius, eye_y, cx - eye_x + eye_radius, eye_y, features, 2);
            draw_line(cx + eye_x - eye_radius, eye_y, cx + eye_x + eye_radius, eye_y, features, 2);
        }
        else
        {
            const int look = (display_model.face == FaceState::Thinking ||
                              display_model.face == FaceState::Curious) ? eye_radius / 2 :
                             display_model.face == FaceState::Idle ?
                                 static_cast<int>((display_model.animation_frame / 40) % 3) - 1 : 0;
            fill_circle(cx - eye_x + look, eye_y, eye_radius, features);
            fill_circle(cx + eye_x + look, eye_y, eye_radius, features);
            if (!oled)
            {
                fill_circle(cx - eye_x + look - 2, eye_y - 2, 1, COLOR_TEXT);
                fill_circle(cx + eye_x + look - 2, eye_y - 2, 1, COLOR_TEXT);
            }
        }
        const int mouth_y = y + (oled ? 11 : 22);
        const int mouth_width = oled ? 10 : 20;
        if (display_model.face == FaceState::Speaking || display_model.face == FaceState::Surprised ||
            display_model.face == FaceState::Listening)
            fill_circle(cx, mouth_y, (oled ? 3 : 5) + (display_model.animation_frame / 3) % 3, features);
        else if (display_model.face == FaceState::Thinking || display_model.face == FaceState::Curious ||
                 display_model.face == FaceState::Sleeping)
            draw_line(cx - mouth_width / 2, mouth_y, cx + mouth_width / 2, mouth_y, features, 2);
        else if (display_model.face == FaceState::Sad || display_model.face == FaceState::Error)
        {
            draw_line(cx - mouth_width, mouth_y + 3, cx, mouth_y - 2, features, 2);
            draw_line(cx, mouth_y - 2, cx + mouth_width, mouth_y + 3, features, 2);
        }
        else
        {
            draw_line(cx - mouth_width, mouth_y - 2, cx, mouth_y + (oled ? 4 : 7), features, 2);
            draw_line(cx, mouth_y + (oled ? 4 : 7), cx + mouth_width, mouth_y - 2, features, 2);
        }
    }

    void draw_emoji(const char *emoji, int x, int y, int size)
    {
        if (emoji == nullptr || emoji[0] == '\0')
            return;
        const int center_x = x + size / 2;
        const int center_y = y + size / 2;
        const int scale = std::max(1, size / 16);
        if (std::strcmp(emoji, "☀️") == 0)
        {
            fill_circle(center_x, center_y, 4 * scale, COLOR_FACE);
            for (int direction = 0; direction < 8; ++direction)
            {
                const int dx = (direction % 2 == 0) ? (direction < 4 ? 1 : -1) : 0;
                const int dy = (direction % 2 == 1) ? (direction < 4 ? 1 : -1) : 0;
                draw_line(center_x + dx * 6 * scale, center_y + dy * 6 * scale,
                          center_x + dx * 7 * scale, center_y + dy * 7 * scale, COLOR_ACCENT, scale);
            }
            return;
        }
        if (std::strcmp(emoji, "✅") == 0)
        {
            draw_line(x + 3 * scale, y + 8 * scale, x + 6 * scale, y + 11 * scale, COLOR_ACCENT, 2 * scale);
            draw_line(x + 6 * scale, y + 11 * scale, x + 13 * scale, y + 4 * scale, COLOR_ACCENT, 2 * scale);
            return;
        }
        if (std::strcmp(emoji, "📝") == 0)
        {
            fill_rect(x + 4 * scale, y + 2 * scale, 9 * scale, 12 * scale, COLOR_TEXT);
            for (int row = 0; row < 3; ++row)
            {
                fill_rect(x + 6 * scale, y + (6 + row * 2) * scale, 5 * scale, scale, COLOR_ACCENT);
            }
            return;
        }
        if (std::strcmp(emoji, "⏰") == 0)
        {
            fill_circle(center_x, center_y + scale, 6 * scale, COLOR_ACCENT);
            fill_circle(center_x, center_y + scale, 4 * scale, COLOR_BACKGROUND);
            draw_line(center_x, center_y + scale, center_x, center_y - 2 * scale, COLOR_TEXT, scale);
            draw_line(center_x, center_y + scale, center_x + 2 * scale, center_y + 2 * scale, COLOR_TEXT, scale);
            return;
        }
        if (std::strcmp(emoji, "🎮") == 0)
        {
            fill_rect(x + 2 * scale, y + 5 * scale, 12 * scale, 7 * scale, COLOR_ACCENT);
            fill_rect(x + 5 * scale, y + 7 * scale, 2 * scale, 2 * scale, COLOR_BACKGROUND);
            fill_rect(x + 4 * scale, y + 6 * scale, 4 * scale, 1 * scale, COLOR_BACKGROUND);
            fill_circle(x + 11 * scale, y + 8 * scale, scale, COLOR_FEATURE);
            return;
        }
        if (std::strcmp(emoji, "📅") == 0 || std::strcmp(emoji, "🔔") == 0 ||
            std::strcmp(emoji, "⚠️") == 0)
        {
            if (std::strcmp(emoji, "📅") == 0)
            {
                fill_rect(x + 3 * scale, y + 3 * scale, 10 * scale, 11 * scale, COLOR_TEXT);
                fill_rect(x + 3 * scale, y + 5 * scale, 10 * scale, 2 * scale, COLOR_ACCENT);
                fill_rect(x + 5 * scale, y + 8 * scale, 2 * scale, 2 * scale, COLOR_FEATURE);
                fill_rect(x + 9 * scale, y + 8 * scale, 2 * scale, 2 * scale, COLOR_FEATURE);
            }
            else if (std::strcmp(emoji, "🔔") == 0)
            {
                fill_circle(center_x, y + 6 * scale, 4 * scale, COLOR_ACCENT);
                fill_rect(x + 5 * scale, y + 7 * scale, 8 * scale, 4 * scale, COLOR_ACCENT);
                fill_rect(x + 4 * scale, y + 11 * scale, 10 * scale, 2 * scale, COLOR_ACCENT);
                fill_circle(center_x, y + 14 * scale, scale, COLOR_FEATURE);
            }
            else
            {
                draw_line(center_x, y + 2 * scale, x + 14 * scale, y + 13 * scale, COLOR_ACCENT, scale);
                draw_line(x + 14 * scale, y + 13 * scale, x + 2 * scale, y + 13 * scale, COLOR_ACCENT, scale);
                draw_line(x + 2 * scale, y + 13 * scale, center_x, y + 2 * scale, COLOR_ACCENT, scale);
                fill_rect(center_x - scale / 2, y + 6 * scale, scale, 4 * scale, COLOR_FEATURE);
                fill_circle(center_x, y + 12 * scale, scale / 2 + 1, COLOR_FEATURE);
            }
            return;
        }
        if (std::strcmp(emoji, "🎙️") == 0)
        {
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
        if (sleeping)
        {
            draw_line(x + 4 * scale, y + 7 * scale, x + 6 * scale, y + 7 * scale, COLOR_FEATURE, scale);
            draw_line(x + 10 * scale, y + 7 * scale, x + 12 * scale, y + 7 * scale, COLOR_FEATURE, scale);
            draw_text("Z", x + 11 * scale, y, scale, COLOR_ACCENT, 1);
        }
        else if (std::strcmp(emoji, "🙂") == 0 || std::strcmp(emoji, "😊") == 0 ||
                 std::strcmp(emoji, "😢") == 0)
        {
            fill_circle(x + 5 * scale, y + 6 * scale, scale, COLOR_FEATURE);
            fill_circle(x + 11 * scale, y + 6 * scale, scale, COLOR_FEATURE);
        }
        else
        {
            draw_text("?", x + 5 * scale, y + 5 * scale, scale, COLOR_TEXT, 1);
            return;
        }
        if (sad)
        {
            draw_line(x + 5 * scale, y + 12 * scale, center_x, y + 10 * scale, COLOR_FEATURE, scale);
            draw_line(center_x, y + 10 * scale, x + 11 * scale, y + 12 * scale, COLOR_FEATURE, scale);
        }
        else
        {
            draw_line(x + 5 * scale, y + 10 * scale, center_x, y + 13 * scale, COLOR_FEATURE, scale);
            draw_line(center_x, y + 13 * scale, x + 11 * scale, y + 10 * scale, COLOR_FEATURE, scale);
        }
    }

    void draw_game_indicator(int x, int y, FaceState face, uint32_t frame, uint16_t color)
    {
        if (face == FaceState::Listening)
        {
            for (int dot = 0; dot < 3; ++dot)
            {
                const int radius = ((frame / 4 + dot) % 3) == 0 ? 3 : 2;
                fill_circle(x + dot * 8, y + 4, radius, color);
            }
        }
        else if (face == FaceState::Speaking)
        {
            for (int bar = 0; bar < 4; ++bar)
            {
                const int height = 3 + static_cast<int>((frame * 3 + static_cast<uint32_t>(bar) * 5) % 9);
                fill_rect(x + bar * 6, y + 8 - height, 3, height, color);
            }
        }
        else if (face == FaceState::Thinking)
        {
            const int active = static_cast<int>(frame / 3 % 3);
            for (int dot = 0; dot < 3; ++dot)
            {
                fill_circle(x + dot * 8, y + 4, dot == active ? 3 : 1, color);
            }
        }
        else
        {
            fill_circle(x + 4, y + 4, 3, color);
        }
    }

    void draw_ring(int center_x, int center_y, int radius, uint16_t color, uint16_t background)
    {
        fill_circle(center_x, center_y, radius, color);
        fill_circle(center_x, center_y, radius - 3, background);
    }

    void draw_tic_tac_toe(const DeviceDisplay::GameView &view, int left, int top, int cell)
    {
        for (int line = 1; line < 3; ++line)
        {
            fill_rect(left + line * cell - 1, top, 2, cell * 3, COLOR_ACCENT);
            fill_rect(left, top + line * cell - 1, cell * 3, 2, COLOR_ACCENT);
        }
        for (int index = 0; index < 9; ++index)
        {
            const int cx = left + (index % 3) * cell + cell / 2;
            const int cy = top + (index / 3) * cell + cell / 2;
            const int arm = cell / 2 - 5;
            if (view.board[index] == 'X')
            {
                draw_line(cx - arm, cy - arm, cx + arm, cy + arm, COLOR_FACE, 3);
                draw_line(cx - arm, cy + arm, cx + arm, cy - arm, COLOR_FACE, 3);
            }
            else if (view.board[index] == 'O')
            {
                draw_ring(cx, cy, arm + 1, COLOR_TEXT, COLOR_BACKGROUND);
            }
            else
            {
                char number[2] = {static_cast<char>('1' + index), '\0'};
                draw_text(number, cx - 1, cy - 2, 1, COLOR_MUTED, 1);
            }
        }
    }

    void draw_hangman(const DeviceDisplay::GameView &view, int left, int top)
    {
        const uint16_t wood = COLOR_MUTED;
        fill_rect(left, top + 70, 50, 3, wood);
        fill_rect(left + 10, top, 3, 70, wood);
        fill_rect(left + 10, top, 30, 3, wood);
        fill_rect(left + 38, top, 2, 10, wood);
        const int m = view.mistakes;
        if (m >= 1)
            draw_ring(left + 39, top + 16, 6, COLOR_FACE, COLOR_BACKGROUND);
        if (m >= 2)
            fill_rect(left + 38, top + 22, 2, 22, COLOR_FACE);
        if (m >= 3)
            draw_line(left + 39, top + 26, left + 30, top + 36, COLOR_FACE, 2);
        if (m >= 4)
            draw_line(left + 39, top + 26, left + 48, top + 36, COLOR_FACE, 2);
        if (m >= 5)
            draw_line(left + 39, top + 44, left + 31, top + 58, COLOR_FACE, 2);
        if (m >= 6)
            draw_line(left + 39, top + 44, left + 47, top + 58, COLOR_FACE, 2);
    }

    void draw_game_screen()
    {
        const auto &view = display_model.game;
        const FaceState face = display_model.face;
        const uint32_t frame = display_model.animation_frame;
        const char *live = face == FaceState::Listening  ? "LISTENING"
                           : face == FaceState::Speaking ? "SPEAKING"
                           : face == FaceState::Thinking ? "THINKING"
                                                         : "YOUR MOVE";
        const uint16_t live_color = face == FaceState::Listening ? COLOR_FACE : COLOR_ACCENT;
        if (active_panel == Panel::Oled)
        {
            oled_frame_buffer.fill(0);
            fill_rect(0, 0, OLED_WIDTH, 9, COLOR_TEXT);
            draw_text(view.title, 3, 2, 1, COLOR_BACKGROUND, 18);
            draw_game_indicator(100, 1, face, frame, COLOR_BACKGROUND);
            if (view.layout == DeviceDisplay::GameLayout::TicTacToe)
            {
                draw_tic_tac_toe(view, 3, 11, 17);
                draw_text(view.status, 62, 14, 1, COLOR_TEXT, 16);
                draw_text(view.line1, 62, 24, 1, COLOR_TEXT, 16);
                draw_text(live, 62, 54, 1, COLOR_TEXT, 12);
            }
            else
            {
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
        draw_game_indicator(132, 5, face, frame, live_color);
        if (view.layout == DeviceDisplay::GameLayout::TicTacToe)
        {
            draw_tic_tac_toe(view, 4, 22, 27);
            draw_text(view.status, 92, 26, 1, COLOR_FACE, 16);
            draw_text(view.line1, 92, 39, 1, COLOR_TEXT, 16);
            draw_text(view.line2, 92, 52, 1, COLOR_MUTED, 16);
        }
        else if (view.layout == DeviceDisplay::GameLayout::Hangman)
        {
            draw_hangman(view, 5, 23);
            draw_text(view.masked, 62, 27, 1, COLOR_TEXT, 24);
            draw_text(view.status, 62, 42, 1, COLOR_FACE, 24);
            draw_text(view.line1, 62, 57, 1, COLOR_TEXT, 24);
            draw_text(view.line2, 62, 72, 1, COLOR_MUTED, 24);
        }
        else if (view.layout == DeviceDisplay::GameLayout::Quiz)
        {
            int y = 21;
            for (uint8_t row = 0; row < view.row_count && row < 8; ++row)
            {
                const bool choice = view.rows[row][1] == ' ' && view.rows[row][0] >= 'A' && view.rows[row][0] <= 'D';
                draw_text(view.rows[row], 5, y, 1, choice ? COLOR_ACCENT : COLOR_TEXT, 38);
                y += 10;
            }
            draw_text(view.status, 5, 104, 1, COLOR_FACE, 38);
        }
        else
        {
            draw_text(view.line1, 5, 25, 2, COLOR_TEXT, 19);
            draw_text(view.line2, 5, 45, 2, COLOR_ACCENT, 19);
            draw_text(view.status, 5, 66, 1, COLOR_FACE, 38);
            draw_text(view.line3, 5, 79, 1, COLOR_TEXT, 38);
        }
        if (view.show_scores)
        {
            char score[32];
            std::snprintf(score, sizeof(score), "YOU %u  BUDDY %u", view.score_you, view.score_ai);
            draw_text(score, 5, 118, 1, COLOR_ACCENT, 38);
        }
        else
        {
            draw_text(live, 5, 118, 1, COLOR_MUTED, 38);
        }
    }

    void draw_screen()
    {
        if (display_model.game_active)
        {
            draw_game_screen();
            return;
        }
        if (active_panel == Panel::Oled)
        {
            oled_frame_buffer.fill(0);
            fill_rect(0, 0, OLED_WIDTH, 9, COLOR_TEXT);
            draw_text("BUDDY", 3, 2, 1, COLOR_BACKGROUND, 8);
            draw_face(display_model.face, display_model.blink, display_model.animation_frame);
            draw_emoji(display_model.emoji.data(), 88, 1, 16);
            draw_text(display_model.title.data(), 3, 12, 1, COLOR_TEXT, 25);
            draw_text(display_model.detail.data(), 3, 21, 1, COLOR_TEXT, 28);
            draw_line(3, 28, 124, 28, COLOR_TEXT, 1);
            int y = 31;
            for (size_t index = 0; index < display_model.item_count && index < 3; ++index)
            {
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
        const bool compact = display_model.item_count > 4;
        for (size_t index = 0; index < display_model.item_count && index < MAX_DISPLAY_ITEMS; ++index)
        {
            draw_text(display_model.items[index].data(), 4, y, 1, COLOR_TEXT, 30);
            y += compact ? 11 : 16;
        }
        if (compact)
            return;

        if (std::strcmp(display_model.page.data(), "home") == 0)
        {
            char counts[40];
            std::snprintf(
                counts, sizeof(counts), "T:%u R:%u N:%u A:%u",
                display_model.todos, display_model.reminders,
                display_model.notes, display_model.notifications);
            draw_text(counts, 5, 127, 1, COLOR_MUTED, 29);
            char alarm_count[20];
            std::snprintf(alarm_count, sizeof(alarm_count), "ALARMS %u", display_model.alarms);
            draw_text(alarm_count, 5, 141, 1, COLOR_ACCENT, 28);
        }
        else
        {
            char footer[40];
            std::snprintf(
                footer, sizeof(footer), "T:%u R:%u N:%u A:%u",
                display_model.todos, display_model.reminders,
                display_model.notes, display_model.alarms);
            draw_text(footer, 5, 141, 1, COLOR_MUTED, 29);
        }
    }

    esp_err_t initialize_oled()
    {
        i2c_master_bus_config_t bus_config{};
        bus_config.i2c_port = HardwareConfig::OLED::I2C_PORT;
        bus_config.sda_io_num = HardwareConfig::OLED::SDA;
        bus_config.scl_io_num = HardwareConfig::OLED::SCL;
        bus_config.clk_source = I2C_CLK_SRC_DEFAULT;
        bus_config.glitch_ignore_cnt = 7;
        bus_config.flags.enable_internal_pullup = true;
        ESP_RETURN_ON_ERROR(
            i2c_new_master_bus(&bus_config, &i2c_bus), TAG, "Cannot initialize OLED I2C bus");

        uint16_t address = OLED_ADDRESS_PRIMARY;
        esp_err_t probe_result = i2c_master_probe(i2c_bus, address, 100);
        if (probe_result != ESP_OK)
        {
            address = OLED_ADDRESS_SECONDARY;
            probe_result = i2c_master_probe(i2c_bus, address, 100);
        }
        if (probe_result != ESP_OK)
        {
            ESP_LOGI(TAG, "No SSD1306 OLED found at I2C addresses 0x3C or 0x3D");
            ESP_RETURN_ON_ERROR(i2c_del_master_bus(i2c_bus), TAG, "Cannot release unused OLED I2C bus");
            i2c_bus = nullptr;
            return ESP_ERR_NOT_FOUND;
        }

        i2c_device_config_t device_config{};
        device_config.dev_addr_length = I2C_ADDR_BIT_LEN_7;
        device_config.device_address = address;
        device_config.scl_speed_hz = 400000;
        const esp_err_t add_device_result =
            i2c_master_bus_add_device(i2c_bus, &device_config, &oled_device);
        if (add_device_result != ESP_OK)
        {
            ESP_LOGE(TAG, "Cannot register SSD1306 OLED: %s", esp_err_to_name(add_device_result));
            const esp_err_t cleanup_result = i2c_del_master_bus(i2c_bus);
            i2c_bus = nullptr;
            if (cleanup_result != ESP_OK)
            {
                ESP_LOGE(TAG, "Cannot release I2C bus after OLED registration failure: %s",
                         esp_err_to_name(cleanup_result));
            }
            return add_device_result;
        }

        const uint8_t initialization[] = {
            0xAE,
            0xD5,
            0x80,
            0xA8,
            0x3F,
            0xD3,
            0x00,
            0x40,
            0x8D,
            0x14,
            0x20,
            0x00,
            0xA1,
            0xC8,
            0xDA,
            0x12,
            0x81,
            0x7F,
            0xD9,
            0xF1,
            0xDB,
            0x40,
            0xA4,
            0xA6,
            0xAF,
        };
        for (const uint8_t value : initialization)
        {
            const uint8_t packet[] = {0x00, value};
            const esp_err_t result = i2c_master_transmit(oled_device, packet, sizeof(packet), 100);
            if (result != ESP_OK)
            {
                ESP_LOGE(TAG, "SSD1306 initialization failed: %s", esp_err_to_name(result));
                const esp_err_t remove_result = i2c_master_bus_rm_device(oled_device);
                oled_device = nullptr;
                if (remove_result != ESP_OK)
                {
                    ESP_LOGE(TAG, "Cannot remove failed OLED device: %s", esp_err_to_name(remove_result));
                }
                const esp_err_t cleanup_result = i2c_del_master_bus(i2c_bus);
                i2c_bus = nullptr;
                if (cleanup_result != ESP_OK)
                {
                    ESP_LOGE(TAG, "Cannot release I2C bus after OLED init failure: %s",
                             esp_err_to_name(cleanup_result));
                }
                return result;
            }
        }
        active_panel = Panel::Oled;
        ESP_LOGI(TAG, "SSD1306 OLED detected at I2C address 0x%02X (128x64)", address);
        return ESP_OK;
    }

    void display_task(void *);

    esp_err_t start_renderer()
    {
        display_mutex = xSemaphoreCreateMutex();
        if (display_mutex == nullptr)
            return ESP_ERR_NO_MEM;
        if (active_panel == Panel::Tft)
        {
            ESP_RETURN_ON_ERROR(initialize_lv_widgets(), TAG, "Cannot initialize LVGL dashboard");
        }
        if (active_panel == Panel::Tft)
            render_lv_idle();
        else
            draw_idle_face();
        ESP_RETURN_ON_ERROR(flush_frame(), TAG, "Cannot draw initial display state");
        const char *task_name = active_panel == Panel::Oled ? "oled_animation" : "tft_animation";
        if (xTaskCreate(display_task, task_name, 8192, nullptr, 3, nullptr) != pdPASS)
        {
            return ESP_ERR_NO_MEM;
        }
        return ESP_OK;
    }

    void display_task(void *)
    {
        TickType_t last_blink = xTaskGetTickCount();
        int64_t last_lv_tick = esp_timer_get_time();
        while (true)
        {
            vTaskDelay(pdMS_TO_TICKS(100));
            if (xSemaphoreTake(display_mutex, pdMS_TO_TICKS(20)) != pdTRUE)
                continue;
            const int64_t elapsed_ms = (esp_timer_get_time() - last_lv_tick) / 1000;
            if (lv_display != nullptr && elapsed_ms > 0)
            {
                lv_tick_inc(static_cast<uint32_t>(elapsed_ms));
                last_lv_tick += elapsed_ms * 1000;
            }
            const TickType_t now = xTaskGetTickCount();
            const bool lifecycle_changed = display_lifecycle.update(static_cast<uint32_t>(now));
            bool update = display_model.dirty || lifecycle_changed;
            display_model.dirty = false;
            if (display_model.face == FaceState::Speaking && display_model.speaking_until != 0 &&
                static_cast<int32_t>(now - display_model.speaking_until) >= 0)
            {
                display_model.face = FaceState::Idle;
                display_model.speaking_until = 0;
                display_model.dirty = true;
                update = true;
            }
            ++display_model.animation_frame;
            if (active_panel == Panel::Tft && display_lifecycle.dashboard() && !display_model.game_active &&
                display_model.item_count > DashboardLayout::visible_items &&
                display_model.animation_frame % DashboardLayout::page_frames == 0)
                update = true;
            const bool full_refresh = update;
            if (display_model.game_active || active_panel == Panel::Oled)
            {
                update = update || display_model.animation_frame % 3 == 0;
            }
            else if (display_model.face == FaceState::Speaking)
            {
                update = update || display_model.animation_frame % 3 == 0;
            }
            else if (display_model.face == FaceState::Listening ||
                     display_model.face == FaceState::Thinking)
            {
                update = update || display_model.animation_frame % 8 == 0;
            }
            else if (now - last_blink >= pdMS_TO_TICKS(2800))
            {
                display_model.blink = true;
                update = true;
                last_blink = now;
            }
            else if (display_model.blink)
            {
                display_model.blink = false;
                update = true;
            }
            if (update)
            {
                if (display_model.game_active)
                    draw_screen();
                else if (!display_lifecycle.dashboard())
                {
                    if (active_panel == Panel::Tft)
                        render_lv_idle();
                    else
                        draw_idle_face();
                }
                else if (active_panel == Panel::Oled)
                    draw_screen();
                else
                    render_lv_dashboard();
                const esp_err_t result = active_panel == Panel::Tft && display_lifecycle.dashboard() &&
                                                 !full_refresh && !display_model.game_active
                                             ? flush_tft_region(112, 0, 48, 44)
                                             : flush_frame();
                if (result != ESP_OK)
                {
                    ESP_LOGE(TAG, "Cannot update animated display: %s", esp_err_to_name(result));
                }
            }
            xSemaphoreGive(display_mutex);
        }
    }

    template <size_t N>
    void copy_text(std::array<char, N> &destination, const char *source)
    {
        destination.fill('\0');
        if (source == nullptr)
            return;
        std::strncpy(destination.data(), source, destination.size() - 1);
    }

    esp_err_t set_addressing()
    {
        const uint8_t color_mode = 0x05;  // 16-bit RGB565
        const uint8_t orientation = 0x60;
        ESP_RETURN_ON_ERROR(command_data(0x3A, &color_mode, sizeof(color_mode)), TAG, "Cannot set RGB565 mode");
        ESP_RETURN_ON_ERROR(command_data(0x36, &orientation, sizeof(orientation)), TAG, "Cannot set display orientation");
        return ESP_OK;
    }

} // namespace

namespace DeviceDisplay
{

    esp_err_t initialize()
    {
        if constexpr (HardwareConfig::Features::ENABLE_OLED)
        {
            const esp_err_t oled_result = initialize_oled();
            if (oled_result == ESP_OK)
            {
                ESP_RETURN_ON_ERROR(start_renderer(), TAG, "Cannot start OLED renderer");
                ESP_LOGI(TAG, "Automatic display selection: SSD1306 OLED");
                return ESP_OK;
            }
            if (oled_result != ESP_ERR_NOT_FOUND)
            {
                ESP_LOGW(TAG, "OLED initialization failed; trying ST7735 TFT: %s",
                         esp_err_to_name(oled_result));
            }
        }

        if constexpr (!HardwareConfig::Features::ENABLE_TFT)
            return ESP_ERR_NOT_FOUND;
        active_panel = Panel::Tft;

        // Allocate DMA-capable memory for line transfers
        dma_row_buffer = static_cast<uint8_t *>(
            heap_caps_malloc(WIDTH * 2, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL));
        if (dma_row_buffer == nullptr)
        {
            ESP_LOGE(TAG, "Failed to allocate DMA row buffer");
            return ESP_ERR_NO_MEM;
        }

        // Configure DC, RST, and BACKLIGHT Control Pins
        gpio_config_t control_pins{};
        control_pins.pin_bit_mask =
            (1ULL << HardwareConfig::TFT::DC) |
            (1ULL << HardwareConfig::TFT::RST) |
            (1ULL << HardwareConfig::TFT::BACKLIGHT);
        control_pins.mode = GPIO_MODE_OUTPUT;
        control_pins.pull_up_en = GPIO_PULLUP_DISABLE;
        control_pins.pull_down_en = GPIO_PULLDOWN_DISABLE;
        control_pins.intr_type = GPIO_INTR_DISABLE;
        ESP_RETURN_ON_ERROR(gpio_config(&control_pins), TAG, "Cannot configure display control pins");

        // Drive Backlight Pin HIGH
        ESP_RETURN_ON_ERROR(gpio_set_level(HardwareConfig::TFT::BACKLIGHT, 1), TAG, "Cannot turn backlight on");

        spi_bus_config_t bus_config{};
        bus_config.mosi_io_num = HardwareConfig::TFT::MOSI;
        bus_config.miso_io_num = -1;
        bus_config.sclk_io_num = HardwareConfig::TFT::SCK;
        bus_config.quadwp_io_num = -1;
        bus_config.quadhd_io_num = -1;
        bus_config.max_transfer_sz = WIDTH * HEIGHT * 2;
        esp_err_t result = spi_bus_initialize(HardwareConfig::TFT::SPI_HOST, &bus_config, SPI_DMA_CH_AUTO);
        if (result != ESP_OK)
            return result;

        spi_device_interface_config_t device_config{};
        device_config.clock_speed_hz = 4 * 1000 * 1000; // 4 MHz clock for stable breadboard transfers
        device_config.mode = 0;
        device_config.spics_io_num = HardwareConfig::TFT::CS;
        device_config.queue_size = 7;
        result = spi_bus_add_device(HardwareConfig::TFT::SPI_HOST, &device_config, &display_device);
        if (result != ESP_OK)
            return result;

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

        // Use 0x20 to disable inversion (INVOFF) for black background
        ESP_RETURN_ON_ERROR(command(0x20), TAG, "Cannot disable display inversion");
        ESP_RETURN_ON_ERROR(set_addressing(), TAG, "Cannot configure display addressing");
        ESP_RETURN_ON_ERROR(command_data(0xE0, gamma_positive, sizeof(gamma_positive)), TAG, "Cannot set positive gamma");
        ESP_RETURN_ON_ERROR(command_data(0xE1, gamma_negative, sizeof(gamma_negative)), TAG, "Cannot set negative gamma");
        ESP_RETURN_ON_ERROR(command(0x13), TAG, "Cannot enter normal display mode");
        vTaskDelay(pdMS_TO_TICKS(10));
        ESP_RETURN_ON_ERROR(command(0x29), TAG, "Cannot turn display on");
        vTaskDelay(pdMS_TO_TICKS(100));

        ESP_RETURN_ON_ERROR(start_renderer(), TAG, "Cannot start TFT renderer");
        ESP_LOGI(TAG, "Automatic display selection: ST7735 TFT, 160x128 landscape on SPI2");
        return ESP_OK;
    }

    esp_err_t set_state(const char *state)
    {
        if (active_panel == Panel::None || state == nullptr)
            return ESP_ERR_INVALID_STATE;
        FaceState face = FaceState::Idle;
        if (std::strcmp(state, "listening") == 0)
            face = FaceState::Listening;
        else if (std::strcmp(state, "thinking") == 0)
            face = FaceState::Thinking;
        else if (std::strcmp(state, "speaking") == 0)
            face = FaceState::Speaking;
        else if (std::strcmp(state, "happy") == 0)
            face = FaceState::Happy;
        else if (std::strcmp(state, "excited") == 0)
            face = FaceState::Excited;
        else if (std::strcmp(state, "curious") == 0)
            face = FaceState::Curious;
        else if (std::strcmp(state, "surprised") == 0)
            face = FaceState::Surprised;
        else if (std::strcmp(state, "sad") == 0)
            face = FaceState::Sad;
        else if (std::strcmp(state, "error") == 0)
            face = FaceState::Error;
        else if (std::strcmp(state, "sleeping") == 0)
            face = FaceState::Sleeping;
        else if (std::strcmp(state, "idle") != 0)
            return ESP_ERR_INVALID_ARG;
        if (display_mutex == nullptr || xSemaphoreTake(display_mutex, pdMS_TO_TICKS(100)) != pdTRUE)
        {
            return ESP_ERR_TIMEOUT;
        }
        display_model.face = face;
        display_model.speaking_until = 0;
        display_model.dirty = true;
        xSemaphoreGive(display_mutex);
        return ESP_OK;
    }

    esp_err_t set_game_view(const GameView &view)
    {
        if (display_mutex == nullptr || active_panel == Panel::None)
            return ESP_ERR_INVALID_STATE;
        if (xSemaphoreTake(display_mutex, pdMS_TO_TICKS(100)) != pdTRUE)
            return ESP_ERR_TIMEOUT;
        display_model.game = view;
        display_model.game_active = true;
        display_model.dirty = true;
        xSemaphoreGive(display_mutex);
        return ESP_OK;
    }

    esp_err_t clear_game_view()
    {
        if (display_mutex == nullptr || active_panel == Panel::None)
            return ESP_ERR_INVALID_STATE;
        if (xSemaphoreTake(display_mutex, pdMS_TO_TICKS(100)) != pdTRUE)
            return ESP_ERR_TIMEOUT;
        display_model.game_active = false;
        display_model.dirty = true;
        xSemaphoreGive(display_mutex);
        return ESP_OK;
    }

    esp_err_t set_speaking_for(uint32_t duration_ms)
    {
        if (display_mutex == nullptr || duration_ms == 0)
            return ESP_ERR_INVALID_ARG;
        if (xSemaphoreTake(display_mutex, pdMS_TO_TICKS(100)) != pdTRUE)
            return ESP_ERR_TIMEOUT;
        display_model.face = FaceState::Speaking;
        display_model.speaking_until = xTaskGetTickCount() + pdMS_TO_TICKS(duration_ms);
        display_model.dirty = true;
        xSemaphoreGive(display_mutex);
        return ESP_OK;
    }

    esp_err_t set_dashboard(
        const char *page,
        const char *title,
        const char *detail,
        const char *const *items,
        size_t item_count,
        uint16_t todos,
        uint16_t reminders,
        uint16_t notes,
        uint16_t notifications,
        uint16_t alarms,
        const char *emoji)
    {
        if (display_mutex == nullptr || page == nullptr || title == nullptr || detail == nullptr ||
            item_count > MAX_DISPLAY_ITEMS || (item_count > 0 && items == nullptr))
        {
            return ESP_ERR_INVALID_ARG;
        }
        if (xSemaphoreTake(display_mutex, pdMS_TO_TICKS(100)) != pdTRUE)
            return ESP_ERR_TIMEOUT;
        copy_text(display_model.page, page);
        copy_text(display_model.title, title);
        copy_text(display_model.detail, detail);
        copy_text(display_model.emoji, emoji);
        for (auto &item : display_model.items)
            item.fill('\0');
        display_model.item_count = item_count;
        for (size_t index = 0; index < item_count; ++index)
            copy_text(display_model.items[index], items[index]);
        display_model.todos = todos;
        display_model.reminders = reminders;
        display_model.notes = notes;
        display_model.notifications = notifications;
        display_model.alarms = alarms;
        display_lifecycle.show_page(std::strcmp(page, "home") == 0,
                                    static_cast<uint32_t>(xTaskGetTickCount()),
                                    static_cast<uint32_t>(pdMS_TO_TICKS(4000)));
        display_model.dirty = true;
        xSemaphoreGive(display_mutex);
        return ESP_OK;
    }

} // namespace DeviceDisplay
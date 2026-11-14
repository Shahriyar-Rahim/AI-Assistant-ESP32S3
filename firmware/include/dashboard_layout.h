#pragma once

#include <cstddef>
#include <cstdint>

namespace DashboardLayout
{
    constexpr int width = 160;
    constexpr int height = 128;
    constexpr int font_height = 16;
    constexpr int left = 3;
    constexpr int header_width = 112;
    constexpr int title_y = 1;
    constexpr int detail_y = 20;
    constexpr int items_y = 45;
    constexpr int row_height = 16;
    constexpr std::size_t visible_items = 4;
    constexpr int item_width = 154;
    constexpr int footer_y = 111;
    constexpr std::uint32_t page_frames = 50;

    constexpr std::size_t first_item(std::size_t count, std::uint32_t frame)
    {
        const std::size_t pages = (count + visible_items - 1) / visible_items;
        return pages == 0 ? 0 : (frame / page_frames % pages) * visible_items;
    }

    static_assert(left + header_width <= 118);
    static_assert(left + item_width <= width);
    static_assert(detail_y + font_height <= items_y);
    static_assert(items_y + visible_items * row_height <= footer_y);
    static_assert(footer_y + font_height <= height);
}

#pragma once

namespace FaceLayout
{
    struct Rect
    {
        int x;
        int y;
        int size;
    };

    constexpr Rect bounds(bool dashboard, bool bob)
    {
        return dashboard ? Rect{119, 4 + static_cast<int>(bob), 38}
                         : Rect{30, 14 + static_cast<int>(bob), 100};
    }

    constexpr int eye_width(bool dashboard) { return dashboard ? 5 : 12; }
    constexpr int mouth_half_width(bool dashboard) { return dashboard ? 7 : 20; }
}

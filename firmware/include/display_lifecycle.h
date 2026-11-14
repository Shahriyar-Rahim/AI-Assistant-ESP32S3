#pragma once

#include <cstdint>

class DisplayLifecycle
{
public:
    void show_page(bool home, uint32_t now, uint32_t home_hold_ticks)
    {
        dashboard_ = true;
        home_pending_ = home;
        deadline_ = now + home_hold_ticks;
    }

    bool update(uint32_t now)
    {
        if (home_pending_ && static_cast<int32_t>(now - deadline_) >= 0)
        {
            dashboard_ = false;
            home_pending_ = false;
            return true;
        }
        return false;
    }

    bool dashboard() const { return dashboard_; }

private:
    bool dashboard_ = false;
    bool home_pending_ = false;
    uint32_t deadline_ = 0;
};

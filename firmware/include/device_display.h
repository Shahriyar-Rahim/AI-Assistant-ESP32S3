#pragma once

#include <cstddef>
#include <cstdint>

#include "esp_err.h"

namespace DeviceDisplay {

esp_err_t initialize();
esp_err_t set_state(const char* state);
esp_err_t set_speaking_for(uint32_t duration_ms);
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
    uint16_t alarms);

}

#pragma once

#include "cJSON.h"
#include "esp_err.h"

namespace DeviceSchedule {

// Returns true when the WebSocket accepted the report; unreported items are retried every 10 seconds.
using ReportFired = bool (*)(const char* id, const char* title, const char* kind);

esp_err_t start(ReportFired report);
// Replaces the stored schedule with the backend's `items` array; the device fires them from its own clock.
void sync(const cJSON* items);

}

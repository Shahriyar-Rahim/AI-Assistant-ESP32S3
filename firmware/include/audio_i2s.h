#pragma once

#include <cstddef>
#include <cstdint>

#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

namespace AudioI2S {

struct PcmBlock {
    uint16_t sample_count;
    int16_t samples[320];
};

esp_err_t initialize();
esp_err_t play_startup_sound(uint32_t duration_ms = 1000);
esp_err_t set_capture_enabled(bool enabled);
esp_err_t set_volume(unsigned percent);
QueueHandle_t capture_queue();
esp_err_t queue_playback(const int16_t* samples, size_t sample_count);
esp_err_t write_playback(const int16_t* samples, size_t sample_count, size_t* samples_written);
esp_err_t queue_sound_effect(const char* effect);

}

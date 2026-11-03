#include "audio_i2s.h"
#include "device_camera.h"
#include "device_network.h"
#include "esp_err.h"
#include "esp_log.h"
#include "hardware_config.h"
#include "device_display.h"

extern "C" void app_main() {
    esp_err_t result = AudioI2S::initialize();
    if (result != ESP_OK) {
        ESP_LOGE("assistant", "Audio initialization failed: %s", esp_err_to_name(result));
        return;
    }
    result = AudioI2S::set_capture_enabled(false);
    if (result != ESP_OK) {
        ESP_LOGE("assistant", "Cannot pause microphone for startup sound: %s", esp_err_to_name(result));
    } else {
        result = AudioI2S::play_startup_sound();
        if (result != ESP_OK) {
            ESP_LOGE("assistant", "Startup speaker check failed: %s", esp_err_to_name(result));
        }
        result = AudioI2S::set_capture_enabled(true);
        if (result != ESP_OK) {
            ESP_LOGE("assistant", "Cannot resume microphone after startup sound: %s", esp_err_to_name(result));
            return;
        }
    }
    if constexpr (HardwareConfig::Features::ENABLE_TFT || HardwareConfig::Features::ENABLE_OLED) {
        result = DeviceDisplay::initialize();
        if (result != ESP_OK) {
            ESP_LOGE("assistant", "Display initialization failed: %s", esp_err_to_name(result));
        }
    }
    result = DeviceNetwork::start();
    if (result != ESP_OK) {
        ESP_LOGE("assistant", "Network initialization failed: %s", esp_err_to_name(result));
        return;
    }
#ifndef DEVICE_USB_CAMERA_DISABLED
    result = DeviceCamera::start();
    if (result != ESP_OK) {
        ESP_LOGE("assistant", "USB camera host failed to start: %s", esp_err_to_name(result));
    }
#endif
    ESP_LOGI("assistant", "Audio hardware and device networking initialized");
}

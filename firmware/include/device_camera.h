#pragma once

#include "esp_err.h"

namespace DeviceCamera {

// Starts the USB host on the ESP32-S3 native USB port and streams MJPEG frames from a UVC webcam
// to the backend (POST /api/v1/vision/frames/<device_id>).
esp_err_t start();

}

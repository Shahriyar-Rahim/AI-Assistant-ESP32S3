#pragma once

#include <cstddef>

#include "esp_err.h"

namespace DeviceNetwork {

esp_err_t start();

// Builds the frame-upload URL and Authorization header; false until Wi-Fi and the backend are ready.
bool camera_endpoint(char* url, size_t url_capacity, char* authorization, size_t authorization_capacity);

}

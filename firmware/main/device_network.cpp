#include "device_network.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <string>
#include <ctime>

#include "audio_i2s.h"
#include "cJSON.h"
#include "device_display.h"
#include "esp_check.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_http_server.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_websocket_client.h"
#include "esp_wifi.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "hardware_config.h"
#include "psa/crypto.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"

namespace {

constexpr char TAG[] = "device_network";
constexpr char NVS_NAMESPACE[] = "assistant";
constexpr char CONNECTED_BIT = BIT0;
constexpr size_t AUDIO_HEADER_SIZE = 25;
constexpr size_t MAX_AUDIO_FRAME_SAMPLES = HardwareConfig::Audio::CAPTURE_SAMPLES * 2;
constexpr size_t MAX_PACKET_SIZE = AUDIO_HEADER_SIZE + MAX_AUDIO_FRAME_SAMPLES * sizeof(int16_t);
constexpr uint32_t AUDIO_UPLINK_STACK_BYTES = 12 * 1024;
constexpr size_t CONTROL_BUFFER_SIZE = 2048;
constexpr size_t REPLAY_WINDOW_SIZE = 8;
constexpr size_t OTA_CHUNK_SIZE = 2048;
constexpr size_t OTA_MAX_IMAGE_SIZE = 6 * 1024 * 1024;
constexpr TickType_t AUDIO_SEND_TIMEOUT = pdMS_TO_TICKS(1000);

struct ReplayFrame {
    uint32_t sequence = 0;
    size_t length = 0;
    bool valid = false;
    std::array<uint8_t, MAX_PACKET_SIZE> bytes{};
};

struct DeviceConfig {
    std::array<char, 33> wifi_ssid{};
    std::array<char, 65> wifi_password{};
    std::array<char, 129> backend_url{};
    std::array<char, 65> device_id{};
    std::array<char, 129> device_token{};
};

struct OtaRequest {
    std::array<char, 65> release_id{};
    std::array<char, 65> version{};
    std::array<char, 65> sha256{};
    std::array<char, 256> download_path{};
    uint32_t size_bytes = 0;
};

DeviceConfig config;
bool saved_config_loaded = false;
bool station_configured = false;
char setup_ssid[32]{};
char setup_password[17]{};
EventGroupHandle_t wifi_events;
EventGroupHandle_t websocket_events;
esp_websocket_client_handle_t websocket;
alignas(int16_t) char control_buffer[CONTROL_BUFFER_SIZE];
size_t control_buffer_used;
constexpr EventBits_t WEBSOCKET_READY_BIT = BIT2;
std::atomic_bool microphone_stream_enabled{true};
std::atomic<uint32_t> next_audio_sequence{0};
std::array<ReplayFrame, REPLAY_WINDOW_SIZE> replay_window{};
QueueHandle_t replay_requests;
QueueHandle_t ota_requests;
SemaphoreHandle_t websocket_send_mutex;
portMUX_TYPE replay_lock = portMUX_INITIALIZER_UNLOCKED;
int brightness_percent = 50;
bool backlight_initialized = false;

bool copy_json_string(const cJSON* object, const char* key, char* target, size_t capacity, bool required);

int send_websocket_text(const char* payload, int length, TickType_t timeout) {
    if (websocket == nullptr || websocket_send_mutex == nullptr || payload == nullptr || length <= 0) {
        return ESP_FAIL;
    }
    if (xSemaphoreTake(websocket_send_mutex, timeout) != pdTRUE) return ESP_ERR_TIMEOUT;
    const int result = esp_websocket_client_send_text(websocket, payload, length, timeout);
    xSemaphoreGive(websocket_send_mutex);
    return result;
}

int send_websocket_binary(const uint8_t* payload, int length, TickType_t timeout) {
    if (websocket == nullptr || websocket_send_mutex == nullptr || payload == nullptr || length <= 0) {
        return ESP_FAIL;
    }
    if (xSemaphoreTake(websocket_send_mutex, timeout) != pdTRUE) return ESP_ERR_TIMEOUT;
    const int result = esp_websocket_client_send_bin(
        websocket, reinterpret_cast<const char*>(payload), length, timeout);
    xSemaphoreGive(websocket_send_mutex);
    return result;
}

void report_ota_status(const OtaRequest& update, const char* state, int progress) {
    if (websocket == nullptr || (xEventGroupGetBits(websocket_events) & WEBSOCKET_READY_BIT) == 0) return;
    char message[512];
    time_t now = time(nullptr);
    struct tm utc_time{};
    char timestamp[32] = "1970-01-01T00:00:00Z";
    if (gmtime_r(&now, &utc_time) != nullptr) {
        std::strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%SZ", &utc_time);
    }
    const uint64_t message_id = static_cast<uint64_t>(esp_timer_get_time());
    const int length = std::snprintf(
        message, sizeof(message),
        "{\"protocol_version\":1,\"message_id\":\"ota-%llu\",\"device_id\":\"%s\","
        "\"timestamp\":\"%s\",\"type\":\"ota.status\",\"payload\":{"
        "\"release_id\":\"%s\",\"state\":\"%s\",\"progress\":%d,\"version\":\"%s\"}}",
        static_cast<unsigned long long>(message_id), config.device_id.data(), timestamp,
        update.release_id.data(), state, progress, update.version.data());
    if (length > 0 && length < static_cast<int>(sizeof(message))) {
        const int sent = send_websocket_text(message, length, pdMS_TO_TICKS(1000));
        if (sent < 0) ESP_LOGW(TAG, "OTA status event could not be sent: %s", state);
    }
}

bool make_http_base_url(char* output, size_t capacity) {
    const char* source = config.backend_url.data();
    const char* replacement = nullptr;
    size_t prefix_length = 0;
    if (std::strncmp(source, "wss://", 6) == 0) {
        replacement = "https://";
        prefix_length = 6;
    } else if (std::strncmp(source, "ws://", 5) == 0) {
        replacement = "http://";
        prefix_length = 5;
    } else if (std::strncmp(source, "https://", 8) == 0 ||
               std::strncmp(source, "http://", 7) == 0) {
        const size_t length = std::strlen(source);
        if (length >= capacity) return false;
        std::memcpy(output, source, length + 1);
        while (length > 0 && output[length - 1] == '/') output[length - 1] = '\0';
        return true;
    } else {
        return false;
    }
    const size_t source_length = std::strlen(source);
    if (source_length < prefix_length || source_length - prefix_length + std::strlen(replacement) >= capacity) {
        return false;
    }
    std::snprintf(output, capacity, "%s%s", replacement, source + prefix_length);
    size_t length = std::strlen(output);
    while (length > 0 && output[length - 1] == '/') output[--length] = '\0';
    return true;
}

bool make_ota_url(const OtaRequest& update, char* output, size_t capacity) {
    char expected_path[160]{};
    const int expected_length = std::snprintf(
        expected_path, sizeof(expected_path), "/api/v1/ota/releases/%s/binary", update.release_id.data());
    if (expected_length <= 0 || expected_length >= static_cast<int>(sizeof(expected_path)) ||
        std::strcmp(update.download_path.data(), expected_path) != 0) {
        return false;
    }
    char base[192]{};
    if (!make_http_base_url(base, sizeof(base))) return false;
    const int length = std::snprintf(
        output, capacity, "%s%s", base, update.download_path.data());
    return length > 0 && length < static_cast<int>(capacity);
}

void send_ota_update(const OtaRequest& update) {
    char url[512]{};
    if (update.size_bytes == 0 || update.size_bytes > OTA_MAX_IMAGE_SIZE ||
        !make_ota_url(update, url, sizeof(url))) {
        report_ota_status(update, "failed", 0);
        return;
    }
    report_ota_status(update, "downloading", 0);
    esp_http_client_config_t http_config{};
    http_config.url = url;
    http_config.timeout_ms = 30000;
    http_config.crt_bundle_attach = esp_crt_bundle_attach;
    esp_http_client_handle_t client = esp_http_client_init(&http_config);
    if (client == nullptr) {
        report_ota_status(update, "failed", 0);
        return;
    }
    esp_err_t result = esp_http_client_set_header(client, "Authorization", config.device_token.data());
    if (result == ESP_OK) result = esp_http_client_open(client, 0);
    int64_t content_length = result == ESP_OK ? esp_http_client_fetch_headers(client) : -1;
    if (result != ESP_OK || esp_http_client_get_status_code(client) != 200 ||
        content_length <= 0 || content_length > OTA_MAX_IMAGE_SIZE ||
        static_cast<uint32_t>(content_length) != update.size_bytes) {
        ESP_LOGE(TAG, "OTA download request failed or image size mismatched");
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        report_ota_status(update, "failed", 0);
        return;
    }
    const esp_partition_t* partition = esp_ota_get_next_update_partition(nullptr);
    if (partition == nullptr || content_length > partition->size) {
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        report_ota_status(update, "failed", 0);
        return;
    }
    esp_ota_handle_t ota_handle = 0;
    result = esp_ota_begin(partition, static_cast<size_t>(content_length), &ota_handle);
    if (result != ESP_OK) {
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        report_ota_status(update, "failed", 0);
        return;
    }
    psa_hash_operation_t hash_operation = PSA_HASH_OPERATION_INIT;
    psa_status_t hash_result = psa_crypto_init();
    if (hash_result == PSA_SUCCESS) {
        hash_result = psa_hash_setup(&hash_operation, PSA_ALG_SHA_256);
    }
    if (hash_result != PSA_SUCCESS) result = ESP_FAIL;
    std::array<uint8_t, OTA_CHUNK_SIZE> buffer{};
    size_t total = 0;
    int last_reported = -1;
    bool image_magic_valid = false;
    while (result == ESP_OK && total < static_cast<size_t>(content_length)) {
        const int received = esp_http_client_read(
            client, reinterpret_cast<char*>(buffer.data()),
            std::min(buffer.size(), static_cast<size_t>(content_length) - total));
        if (received <= 0) {
            result = received == 0 ? ESP_FAIL : static_cast<esp_err_t>(received);
            break;
        }
        if (total == 0) image_magic_valid = buffer[0] == 0xE9;
        if (!image_magic_valid) {
            result = ESP_ERR_INVALID_RESPONSE;
            break;
        }
        result = esp_ota_write(ota_handle, buffer.data(), static_cast<size_t>(received));
        if (result == ESP_OK && hash_result == PSA_SUCCESS) {
            hash_result = psa_hash_update(&hash_operation, buffer.data(), static_cast<size_t>(received));
            if (hash_result != PSA_SUCCESS) result = ESP_FAIL;
        }
        total += static_cast<size_t>(received);
        const int progress = static_cast<int>((total * 100) / static_cast<size_t>(content_length));
        if (progress / 10 != last_reported / 10 || progress == 100) {
            report_ota_status(update, "downloading", progress);
            last_reported = progress;
        }
    }
    std::array<uint8_t, 32> digest{};
    size_t digest_length = 0;
    if (result == ESP_OK && hash_result == PSA_SUCCESS) {
        hash_result = psa_hash_finish(&hash_operation, digest.data(), digest.size(), &digest_length);
        if (hash_result != PSA_SUCCESS || digest_length != digest.size()) {
            psa_hash_abort(&hash_operation);
            result = ESP_FAIL;
        }
    } else {
        psa_hash_abort(&hash_operation);
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    char actual_hash[65]{};
    for (size_t index = 0; index < digest.size(); ++index) {
        std::snprintf(actual_hash + index * 2, 3, "%02x", digest[index]);
    }
    if (result == ESP_OK && std::strcmp(actual_hash, update.sha256.data()) != 0) {
        result = ESP_ERR_INVALID_CRC;
    }
    report_ota_status(update, "verifying", 100);
    if (result == ESP_OK) result = esp_ota_end(ota_handle);
    else esp_ota_abort(ota_handle);
    if (result == ESP_OK) result = esp_ota_set_boot_partition(partition);
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "OTA image verification or activation failed: %s", esp_err_to_name(result));
        report_ota_status(update, "failed", 100);
        return;
    }
    report_ota_status(update, "rebooting", 100);
    vTaskDelay(pdMS_TO_TICKS(1000));
    esp_restart();
}

void ota_task(void*) {
    OtaRequest update{};
    while (true) {
        if (xQueueReceive(ota_requests, &update, portMAX_DELAY) == pdTRUE) {
            send_ota_update(update);
        }
    }
}

bool queue_ota_release(const cJSON* payload) {
    const cJSON* release = cJSON_GetObjectItemCaseSensitive(payload, "release");
    if (!cJSON_IsObject(release)) return false;
    OtaRequest update{};
    const cJSON* size = cJSON_GetObjectItemCaseSensitive(release, "size_bytes");
    const cJSON* hardware = cJSON_GetObjectItemCaseSensitive(release, "hardware_target");
    if (!cJSON_IsNumber(size) || size->valuedouble <= 0 ||
        size->valuedouble > OTA_MAX_IMAGE_SIZE || !cJSON_IsString(hardware) ||
        std::strcmp(hardware->valuestring, "esp32-s3-n16r8") != 0 ||
        !copy_json_string(release, "id", update.release_id.data(), update.release_id.size(), true) ||
        !copy_json_string(release, "version", update.version.data(), update.version.size(), true) ||
        !copy_json_string(release, "sha256", update.sha256.data(), update.sha256.size(), true) ||
        !copy_json_string(release, "download_path", update.download_path.data(), update.download_path.size(), true) ||
        std::strlen(update.sha256.data()) != 64) {
        return false;
    }
    for (const char* digit = update.sha256.data(); *digit != '\0'; ++digit) {
        if (!((*digit >= '0' && *digit <= '9') || (*digit >= 'a' && *digit <= 'f'))) return false;
    }
    update.size_bytes = static_cast<uint32_t>(size->valuedouble);
    return xQueueSend(ota_requests, &update, 0) == pdTRUE;
}

esp_err_t apply_brightness(int percent) {
    if (percent < 0 || percent > 100) return ESP_ERR_INVALID_ARG;
    if (!backlight_initialized) {
        ledc_timer_config_t timer{};
        timer.speed_mode = HardwareConfig::TFT::PWM_MODE;
        timer.duty_resolution = LEDC_TIMER_10_BIT;
        timer.timer_num = HardwareConfig::TFT::PWM_TIMER;
        timer.freq_hz = 5000;
        timer.clk_cfg = LEDC_AUTO_CLK;
        ESP_RETURN_ON_ERROR(ledc_timer_config(&timer), TAG, "Cannot configure backlight timer");
        ledc_channel_config_t channel{};
        channel.gpio_num = HardwareConfig::TFT::BACKLIGHT;
        channel.speed_mode = HardwareConfig::TFT::PWM_MODE;
        channel.channel = HardwareConfig::TFT::PWM_CHANNEL;
        channel.timer_sel = HardwareConfig::TFT::PWM_TIMER;
        ESP_RETURN_ON_ERROR(ledc_channel_config(&channel), TAG, "Cannot configure backlight channel");
        backlight_initialized = true;
    }
    const uint32_t duty = static_cast<uint32_t>(percent) * 1023 / 100;
    ESP_RETURN_ON_ERROR(
        ledc_set_duty(HardwareConfig::TFT::PWM_MODE, HardwareConfig::TFT::PWM_CHANNEL, duty),
        TAG, "Cannot set backlight duty");
    ESP_RETURN_ON_ERROR(
        ledc_update_duty(HardwareConfig::TFT::PWM_MODE, HardwareConfig::TFT::PWM_CHANNEL),
        TAG, "Cannot update backlight duty");
    brightness_percent = percent;
    return ESP_OK;
}

esp_err_t persist_runtime_setting(const char* key, uint8_t value) {
    nvs_handle_t handle;
    esp_err_t result = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (result != ESP_OK) return result;
    result = nvs_set_u8(handle, key, value);
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
    return result;
}

void load_runtime_settings() {
    nvs_handle_t handle;
    esp_err_t result = nvs_open(NVS_NAMESPACE, NVS_READONLY, &handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) return;
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "Cannot read runtime settings: %s", esp_err_to_name(result));
        return;
    }
    uint8_t stored = 0;
    result = nvs_get_u8(handle, "brightness", &stored);
    if (result == ESP_OK && stored <= 100) {
        brightness_percent = stored;
    } else if (result == ESP_OK) {
        ESP_LOGW(TAG, "Ignoring invalid stored brightness setting");
    } else if (result != ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGE(TAG, "Cannot read stored brightness setting: %s", esp_err_to_name(result));
    }
    result = nvs_get_u8(handle, "mic_enabled", &stored);
    if (result == ESP_OK) {
        microphone_stream_enabled.store(stored != 0);
    } else if (result != ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGE(TAG, "Cannot read stored microphone setting: %s", esp_err_to_name(result));
    }
    nvs_close(handle);
}

bool copy_json_string(const cJSON* object, const char* key, char* target, size_t capacity, bool required) {
    const cJSON* item = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsString(item) || item->valuestring == nullptr) return !required;
    const size_t length = std::strlen(item->valuestring);
    if (length == 0) return !required;
    if (length >= capacity) return false;
    for (size_t i = 0; i < length; ++i) {
        if (static_cast<unsigned char>(item->valuestring[i]) < 0x20) return false;
    }
    std::memcpy(target, item->valuestring, length + 1);
    return true;
}

bool is_public_wss_origin(const char* value) {
    std::string backend(value == nullptr ? "" : value);
    if (backend.rfind("wss://", 0) != 0) return false;
    std::string host = backend.substr(6);
    if (!host.empty() && host.back() == '/') host.pop_back();
    if (host.empty() || host.find_first_of("/?#@ \r\n") != std::string::npos) return false;
    const size_t port_separator = host.find(':');
    std::string hostname = host.substr(0, port_separator);
    std::transform(hostname.begin(), hostname.end(), hostname.begin(), [](unsigned char character) {
        return static_cast<char>(std::tolower(character));
    });
    const bool numeric_address = !hostname.empty() && std::all_of(
        hostname.begin(), hostname.end(), [](unsigned char character) {
            return (character >= '0' && character <= '9') || character == '.';
        });
    const bool local_domain = hostname == "localhost" ||
        (hostname.size() >= 6 && hostname.compare(hostname.size() - 6, 6, ".local") == 0);
    return hostname.find('.') != std::string::npos && !numeric_address && !local_domain;
}

bool request_is_from_setup_ap(httpd_req_t* request) {
    sockaddr_storage peer{};
    socklen_t peer_length = sizeof(peer);
    if (getpeername(httpd_req_to_sockfd(request), reinterpret_cast<sockaddr*>(&peer), &peer_length) != 0 ||
        peer.ss_family != AF_INET) {
        return false;
    }
    const auto* ipv4 = reinterpret_cast<const sockaddr_in*>(&peer);
    return (ntohl(ipv4->sin_addr.s_addr) & 0xFFFFFF00U) == 0xC0A80400U;
}

esp_err_t persist_config(const DeviceConfig& value) {
    nvs_handle_t handle;
    esp_err_t result = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (result != ESP_OK) return result;
    result = nvs_set_str(handle, "ssid", value.wifi_ssid.data());
    if (result == ESP_OK) result = nvs_set_str(handle, "password", value.wifi_password.data());
    if (result == ESP_OK) result = nvs_set_str(handle, "backend", value.backend_url.data());
    if (result == ESP_OK) result = nvs_set_str(handle, "device_id", value.device_id.data());
    if (result == ESP_OK) result = nvs_set_str(handle, "token", value.device_token.data());
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
    return result;
}

esp_err_t read_config_value(nvs_handle_t handle, const char* key, char* value, size_t capacity) {
    size_t length = capacity;
    return nvs_get_str(handle, key, value, &length);
}

bool load_config() {
    nvs_handle_t handle;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &handle) != ESP_OK) return false;
    const bool valid =
        read_config_value(handle, "ssid", config.wifi_ssid.data(), config.wifi_ssid.size()) == ESP_OK &&
        read_config_value(handle, "password", config.wifi_password.data(), config.wifi_password.size()) == ESP_OK &&
        read_config_value(handle, "backend", config.backend_url.data(), config.backend_url.size()) == ESP_OK &&
        read_config_value(handle, "device_id", config.device_id.data(), config.device_id.size()) == ESP_OK &&
        read_config_value(handle, "token", config.device_token.data(), config.device_token.size()) == ESP_OK;
    nvs_close(handle);
    return valid;
}

esp_err_t portal_root(httpd_req_t* request) {
    if (!request_is_from_setup_ap(request)) {
        httpd_resp_send_err(request, HTTPD_403_FORBIDDEN, "Setup dashboard is available only through its WPA2 access point");
        return ESP_FAIL;
    }
    static constexpr char page[] =
        "<!doctype html><meta name=viewport content='width=device-width'><title>Buddy device dashboard</title>"
        "<style>body{font:16px system-ui;max-width:680px;margin:2rem auto;padding:0 1rem;background:#101827;color:#eef}"
        "input{box-sizing:border-box;width:100%;padding:.7rem;margin:.35rem 0 1rem}button{padding:.8rem;background:#46d3b1;border:0}"
        "label{display:block}section{border:1px solid #536078;padding:1rem;margin:1rem 0;border-radius:.7rem}</style>"
        "<h1>Buddy device dashboard</h1><section><h2>Device status</h2><pre id=status>Loading...</pre></section>"
        "<section><h2>Wi-Fi and production backend</h2>"
        "<p>This WPA2-protected local page lets you change networks while traveling. Use the production secure WebSocket URL.</p>"
        "<form id=setup-form><label>Wi-Fi name<input name=wifi_ssid required maxlength=32></label>"
        "<label>Wi-Fi password<input name=wifi_password type=password maxlength=64 autocomplete=new-password></label>"
        "<label>Production backend origin<input name=backend_url value='wss://buddyapi.no-idea.top' maxlength=128 required></label>"
        "<label>Device ID<input name=device_id required maxlength=64></label>"
        "<label>Device token (leave blank to keep the current token)<input name=device_token type=password maxlength=128 autocomplete=new-password></label>"
        "<button>Save and reconnect</button></form><pre id=result></pre></section>"
        "<script>const f=document.getElementById('setup-form'),o=document.getElementById('result');"
        "fetch('/status').then(r=>r.json()).then(s=>{document.getElementById('status').textContent="
        "JSON.stringify(s,null,2);f.wifi_ssid.value=s.wifi_ssid||'';"
        "if(s.backend_url&&s.backend_url.startsWith('wss://'))f.backend_url.value=s.backend_url;"
        "f.device_id.value=s.device_id||''}).catch(e=>{document.getElementById('status').textContent=String(e)});"
        "f.onsubmit=async e=>{e.preventDefault();let d=Object.fromEntries(new FormData(f));"
        "let r=await fetch('/configure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});"
        "o.textContent=await r.text();}</script>";
    httpd_resp_set_type(request, "text/html; charset=utf-8");
    return httpd_resp_send(request, page, HTTPD_RESP_USE_STRLEN);
}

esp_err_t portal_status(httpd_req_t* request) {
    if (!request_is_from_setup_ap(request)) {
        httpd_resp_send_err(request, HTTPD_403_FORBIDDEN, "Setup dashboard is available only through its WPA2 access point");
        return ESP_FAIL;
    }
    cJSON* status = cJSON_CreateObject();
    if (status == nullptr) {
        httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR, "Cannot create status response");
        return ESP_ERR_NO_MEM;
    }
    cJSON_AddBoolToObject(status, "configured", station_configured);
    cJSON_AddStringToObject(status, "wifi_ssid", saved_config_loaded ? config.wifi_ssid.data() : "");
    cJSON_AddStringToObject(status, "backend_url", saved_config_loaded ? config.backend_url.data() : "");
    cJSON_AddStringToObject(status, "device_id", saved_config_loaded ? config.device_id.data() : "");
    wifi_ap_record_t ap_info{};
    cJSON_AddBoolToObject(status, "station_connected", saved_config_loaded && esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK);
    char* body = cJSON_PrintUnformatted(status);
    cJSON_Delete(status);
    if (body == nullptr) {
        httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR, "Cannot encode status response");
        return ESP_FAIL;
    }
    httpd_resp_set_type(request, "application/json");
    const esp_err_t result = httpd_resp_sendstr(request, body);
    cJSON_free(body);
    return result;
}

esp_err_t portal_configure(httpd_req_t* request) {
    if (!request_is_from_setup_ap(request)) {
        httpd_resp_send_err(request, HTTPD_403_FORBIDDEN, "Setup dashboard is available only through its WPA2 access point");
        return ESP_FAIL;
    }
    if (request->content_len <= 0 || request->content_len > 1024) {
        httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Invalid configuration size");
        return ESP_FAIL;
    }
    std::array<char, 1025> body{};
    size_t received = 0;
    while (received < static_cast<size_t>(request->content_len)) {
        const int count = httpd_req_recv(
            request, body.data() + received,
            std::min(sizeof(body) - received - 1, static_cast<size_t>(request->content_len) - received));
        if (count <= 0) {
            httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Incomplete configuration");
            return ESP_FAIL;
        }
        received += static_cast<size_t>(count);
    }
    cJSON* parsed = cJSON_ParseWithLength(body.data(), received);
    if (parsed == nullptr) {
        httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Invalid JSON");
        return ESP_FAIL;
    }
    DeviceConfig submitted{};
    const bool valid =
        copy_json_string(parsed, "wifi_ssid", submitted.wifi_ssid.data(), submitted.wifi_ssid.size(), true) &&
        copy_json_string(parsed, "wifi_password", submitted.wifi_password.data(), submitted.wifi_password.size(), false) &&
        copy_json_string(parsed, "backend_url", submitted.backend_url.data(), submitted.backend_url.size(), true) &&
        copy_json_string(parsed, "device_id", submitted.device_id.data(), submitted.device_id.size(), true) &&
        copy_json_string(parsed, "device_token", submitted.device_token.data(), submitted.device_token.size(), false);
    cJSON_Delete(parsed);
    const bool backend_valid = is_public_wss_origin(submitted.backend_url.data());
    if (submitted.wifi_password[0] == '\0' && saved_config_loaded &&
        std::strcmp(submitted.wifi_ssid.data(), config.wifi_ssid.data()) == 0) {
        std::strncpy(submitted.wifi_password.data(), config.wifi_password.data(),
            submitted.wifi_password.size() - 1);
    }
    if (submitted.device_token[0] == '\0' && saved_config_loaded) {
        std::strncpy(submitted.device_token.data(), config.device_token.data(),
            submitted.device_token.size() - 1);
    }
    bool device_id_valid = submitted.device_id[0] != '\0';
    for (const char* character = submitted.device_id.data(); device_id_valid && *character != '\0'; ++character) {
        const char value = *character;
        device_id_valid = (value >= 'a' && value <= 'z') || (value >= 'A' && value <= 'Z') ||
            (value >= '0' && value <= '9') || value == '.' || value == '_' || value == '-';
    }
    if (!valid || !backend_valid || !device_id_valid) {
        httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Required fields or WebSocket backend URL are invalid");
        return ESP_FAIL;
    }
    if (submitted.device_token[0] == '\0') {
        httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "A device token is required for first-time setup");
        return ESP_ERR_INVALID_ARG;
    }
    const esp_err_t result = persist_config(submitted);
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "Cannot persist setup configuration: %s", esp_err_to_name(result));
        httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR, "Could not save configuration");
        return result;
    }
    httpd_resp_set_type(request, "text/plain");
    httpd_resp_sendstr(request, "Configuration saved. Device will restart and connect.");
    vTaskDelay(pdMS_TO_TICKS(700));
    esp_restart();
    return ESP_OK;
}

esp_err_t start_provisioning_portal() {
    uint8_t mac[6];
    ESP_RETURN_ON_ERROR(esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP), TAG, "Cannot read Wi-Fi MAC");
    std::snprintf(setup_ssid, sizeof(setup_ssid), "Buddy-Setup-%02X%02X", mac[4], mac[5]);
    std::snprintf(
        setup_password, sizeof(setup_password), "%08X%08X",
        static_cast<unsigned>(esp_random()), static_cast<unsigned>(esp_random()));
    wifi_config_t ap_config{};
    std::snprintf(reinterpret_cast<char*>(ap_config.ap.ssid), sizeof(ap_config.ap.ssid), "%s", setup_ssid);
    std::snprintf(reinterpret_cast<char*>(ap_config.ap.password), sizeof(ap_config.ap.password), "%s", setup_password);
    ap_config.ap.ssid_len = std::strlen(setup_ssid);
    ap_config.ap.channel = 1;
    ap_config.ap.max_connection = 2;
    ap_config.ap.authmode = WIFI_AUTH_WPA2_PSK;
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_AP, &ap_config), TAG, "Cannot configure setup access point");

    httpd_config_t server_config = HTTPD_DEFAULT_CONFIG();
    httpd_handle_t server = nullptr;
    ESP_RETURN_ON_ERROR(httpd_start(&server, &server_config), TAG, "Cannot start setup portal");
    httpd_uri_t root = {.uri = "/", .method = HTTP_GET, .handler = portal_root, .user_ctx = nullptr};
    httpd_uri_t status = {.uri = "/status", .method = HTTP_GET, .handler = portal_status, .user_ctx = nullptr};
    httpd_uri_t configure = {.uri = "/configure", .method = HTTP_POST, .handler = portal_configure, .user_ctx = nullptr};
    ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &root), TAG, "Cannot register portal page");
    ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &status), TAG, "Cannot register portal status");
    ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &configure), TAG, "Cannot register portal configuration");
    ESP_LOGI(TAG, "Device dashboard available at http://192.168.4.1/; Wi-Fi SSID: %s, password: %s",
        setup_ssid, setup_password);
    return ESP_OK;
}

void wifi_event_handler(void*, esp_event_base_t event_base, int32_t event_id, void*) {
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(wifi_events, CONNECTED_BIT);
        ESP_LOGW(TAG, "Wi-Fi disconnected; retrying");
        if (station_configured) esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(wifi_events, CONNECTED_BIT);
        ESP_LOGI(TAG, "Wi-Fi connected");
    }
}

void send_hello() {
    char hello[256];
    const uint64_t boot_id = static_cast<uint64_t>(esp_timer_get_time());
    const esp_app_desc_t* app_description = esp_app_get_description();
    const char* firmware_version = app_description == nullptr ? "unknown" : app_description->version;
    time_t now = time(nullptr);
    struct tm utc_time{};
    char timestamp[32] = "1970-01-01T00:00:00Z";
    if (gmtime_r(&now, &utc_time) != nullptr) {
        std::strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%SZ", &utc_time);
    }
    const int length = std::snprintf(
        hello, sizeof(hello),
        "{\"protocol_version\":1,\"message_id\":\"boot-%llu\",\"device_id\":\"%s\","
        "\"timestamp\":\"%s\",\"type\":\"device.hello\","
        "\"payload\":{\"firmware_version\":\"%s\",\"simulated\":false}}",
        static_cast<unsigned long long>(boot_id),
        config.device_id.data(), timestamp, firmware_version);
    if (length > 0 && length < static_cast<int>(sizeof(hello))) {
        const int sent = send_websocket_text(hello, length, pdMS_TO_TICKS(3000));
        if (sent < 0) ESP_LOGE(TAG, "Failed sending device hello");
    }
}

void queue_replay_range(uint32_t expected_sequence) {
    if (replay_requests == nullptr) return;
    bool available = false;
    for (size_t offset = 0; offset < REPLAY_WINDOW_SIZE; ++offset) {
        const uint32_t sequence = expected_sequence + static_cast<uint32_t>(offset);
        portENTER_CRITICAL(&replay_lock);
        const auto& cached = replay_window[sequence % REPLAY_WINDOW_SIZE];
        available = cached.valid && cached.sequence == sequence;
        portEXIT_CRITICAL(&replay_lock);
        if (available) break;
    }
    if (available && xQueueOverwrite(replay_requests, &expected_sequence) != pdPASS) {
        ESP_LOGW(TAG, "Could not queue the bounded audio replay range");
    }
}

void replay_audio_range(uint32_t requested) {
    std::array<uint8_t, MAX_PACKET_SIZE> packet{};
    for (size_t offset = 0; offset < REPLAY_WINDOW_SIZE; ++offset) {
        const uint32_t sequence = requested + static_cast<uint32_t>(offset);
        size_t length = 0;
        portENTER_CRITICAL(&replay_lock);
        const auto& cached = replay_window[sequence % REPLAY_WINDOW_SIZE];
        if (cached.valid && cached.sequence == sequence) {
            length = cached.length;
            std::memcpy(packet.data(), cached.bytes.data(), length);
        }
        portEXIT_CRITICAL(&replay_lock);
        if (length == 0) continue;
        if ((xEventGroupGetBits(websocket_events) & WEBSOCKET_READY_BIT) == 0) break;
        const int sent = send_websocket_binary(
            packet.data(), static_cast<int>(length), AUDIO_SEND_TIMEOUT);
        if (sent != static_cast<int>(length)) {
            ESP_LOGW(TAG, "Could not replay audio frame %lu", static_cast<unsigned long>(sequence));
            break;
        }
    }
}

void websocket_event_handler(void*, esp_event_base_t, int32_t event_id, void* event_data) {
    auto* event = static_cast<esp_websocket_event_data_t*>(event_data);
    if (event_id == WEBSOCKET_EVENT_CONNECTED) {
        ESP_LOGI(TAG, "Backend WebSocket connected");
        send_hello();
        return;
    }
    if (event_id == WEBSOCKET_EVENT_ERROR && event != nullptr) {
        const auto& error = event->error_handle;
        ESP_LOGE(
            TAG,
            "WebSocket transport error: %.*s; type=%d close_code=%d handshake_status=%d "
            "tls_error=%d tls_stack_error=%d tls_flags=%d socket_errno=%d",
            event->data_ptr == nullptr ? 0 : event->data_len,
            event->data_ptr == nullptr ? "" : event->data_ptr,
            static_cast<int>(error.error_type), event->close_status_code,
            error.esp_ws_handshake_status_code, error.esp_tls_last_esp_err,
            error.esp_tls_stack_err, error.esp_tls_cert_verify_flags,
            error.esp_transport_sock_errno);
        xEventGroupClearBits(websocket_events, WEBSOCKET_READY_BIT);
        return;
    }
    if (event_id == WEBSOCKET_EVENT_DISCONNECTED || event_id == WEBSOCKET_EVENT_CLOSED) {
        xEventGroupClearBits(websocket_events, WEBSOCKET_READY_BIT);
        ESP_LOGW(
            TAG, "Backend WebSocket %s (close_code=%d)",
            event_id == WEBSOCKET_EVENT_CLOSED ? "closed cleanly" : "disconnected",
            event == nullptr ? 0 : event->close_status_code);
        return;
    }
    if (event_id != WEBSOCKET_EVENT_DATA || event == nullptr) return;
    if (event->op_code == 0x9 || event->op_code == 0xA) {
        control_buffer_used = 0;
        ESP_LOGD(
            TAG, "Received WebSocket %s control frame",
            event->op_code == 0x9 ? "ping" : "pong");
        return;
    }
    if (event->payload_len <= 0 || event->payload_len > static_cast<int>(CONTROL_BUFFER_SIZE - 1)) {
        ESP_LOGW(
            TAG, "Ignoring server frame with invalid payload length "
            "(payload_len=%d data_len=%d offset=%d opcode=%u)",
            event->payload_len, event->data_len, event->payload_offset, event->op_code);
        control_buffer_used = 0;
        return;
    }
    if (event->payload_offset == 0) control_buffer_used = 0;
    if (event->payload_offset != static_cast<int>(control_buffer_used) ||
        control_buffer_used + static_cast<size_t>(event->data_len) > sizeof(control_buffer)) {
        ESP_LOGW(TAG, "Ignoring fragmented WebSocket message with invalid offsets");
        control_buffer_used = 0;
        return;
    }
    std::memcpy(control_buffer + control_buffer_used, event->data_ptr, event->data_len);
    control_buffer_used += static_cast<size_t>(event->data_len);
    if (control_buffer_used != static_cast<size_t>(event->payload_len)) return;
    if (event->op_code == 0x2) {
        if (control_buffer_used % sizeof(int16_t) != 0) {
            ESP_LOGW(TAG, "Ignoring unaligned PCM playback frame");
            control_buffer_used = 0;
            return;
        }
        const auto* samples = reinterpret_cast<const int16_t*>(control_buffer);
        const size_t count = control_buffer_used / sizeof(int16_t);
        for (size_t offset = 0; offset < count; offset += HardwareConfig::Audio::CAPTURE_SAMPLES) {
            const size_t segment = std::min(
                static_cast<size_t>(HardwareConfig::Audio::CAPTURE_SAMPLES), count - offset);
            if (AudioI2S::queue_playback(samples + offset, segment) != ESP_OK) {
                ESP_LOGW(TAG, "Playback queue full; dropping incoming audio");
                break;
            }
        }
    } else if (event->op_code == 0x1) {
        control_buffer[control_buffer_used] = '\0';
        cJSON* message = cJSON_Parse(control_buffer);
        const cJSON* type = message == nullptr ? nullptr : cJSON_GetObjectItemCaseSensitive(message, "type");
        const cJSON* payload = message == nullptr ? nullptr : cJSON_GetObjectItemCaseSensitive(message, "payload");
        if (cJSON_IsString(type)) {
            if (std::strcmp(type->valuestring, "audio.ack") == 0) {
                const cJSON* sequence_item = cJSON_GetObjectItemCaseSensitive(payload, "sequence");
                if (cJSON_IsNumber(sequence_item) && sequence_item->valuedouble >= 0 &&
                    sequence_item->valuedouble <= UINT32_MAX) {
                    const uint32_t acknowledged = static_cast<uint32_t>(sequence_item->valuedouble);
                    portENTER_CRITICAL(&replay_lock);
                    auto& cached = replay_window[acknowledged % REPLAY_WINDOW_SIZE];
                    if (cached.valid && cached.sequence == acknowledged) cached.valid = false;
                    portEXIT_CRITICAL(&replay_lock);
                }
            } else if (std::strcmp(type->valuestring, "audio.nack") == 0) {
                const cJSON* expected = cJSON_GetObjectItemCaseSensitive(payload, "expected_sequence");
                if (cJSON_IsNumber(expected) && expected->valuedouble >= 0 &&
                    expected->valuedouble <= UINT32_MAX) {
                    const uint32_t missing = static_cast<uint32_t>(expected->valuedouble);
                    queue_replay_range(missing);
                }
            } else if (std::strcmp(type->valuestring, "ping") == 0) {
                const cJSON* id = cJSON_GetObjectItemCaseSensitive(message, "message_id");
                char pong[256];
                time_t now = time(nullptr);
                struct tm utc_time{};
                char timestamp[32] = "1970-01-01T00:00:00Z";
                if (gmtime_r(&now, &utc_time) != nullptr) {
                    std::strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%SZ", &utc_time);
                }
                if (cJSON_IsString(id)) {
                    const int length = std::snprintf(
                        pong, sizeof(pong),
                        "{\"protocol_version\":1,\"message_id\":\"%s\",\"device_id\":\"%s\","
                        "\"timestamp\":\"%s\",\"type\":\"pong\",\"payload\":{}}",
                        id->valuestring, config.device_id.data(), timestamp);
                    if (length > 0 && length < static_cast<int>(sizeof(pong))) {
                        send_websocket_text(pong, length, pdMS_TO_TICKS(100));
                    }
                }
            } else if (std::strcmp(type->valuestring, "device.status") == 0) {
                xEventGroupSetBits(websocket_events, WEBSOCKET_READY_BIT);
                if (esp_ota_check_rollback_is_possible()) {
                    const esp_err_t rollback_result = esp_ota_mark_app_valid_cancel_rollback();
                    if (rollback_result != ESP_OK) {
                        ESP_LOGE(TAG, "Cannot mark OTA firmware healthy: %s", esp_err_to_name(rollback_result));
                    }
                }
                const uint32_t current = next_audio_sequence.load();
                queue_replay_range(current - static_cast<uint32_t>(REPLAY_WINDOW_SIZE));
            } else if (std::strcmp(type->valuestring, "ota.available") == 0) {
                if (!queue_ota_release(payload)) {
                    ESP_LOGE(TAG, "OTA release manifest is invalid or update queue is full");
                    char error[320];
                    const int length = std::snprintf(
                        error, sizeof(error),
                        "{\"protocol_version\":1,\"message_id\":\"ota-invalid\","
                        "\"device_id\":\"%s\",\"timestamp\":\"1970-01-01T00:00:00Z\","
                        "\"type\":\"device.error\",\"payload\":{\"code\":\"invalid_ota_manifest\"}}",
                        config.device_id.data());
                    if (length > 0 && length < static_cast<int>(sizeof(error))) {
                        send_websocket_text(error, length, pdMS_TO_TICKS(100));
                    }
                }
            } else if (std::strcmp(type->valuestring, "display.state") == 0) {
                const cJSON* display_state = cJSON_GetObjectItemCaseSensitive(payload, "state");
                const cJSON* page_item = cJSON_GetObjectItemCaseSensitive(payload, "page");
                if (cJSON_IsString(page_item) && page_item->valuestring != nullptr) {
                    char page[16]{};
                    char title[40]{};
                    char detail[48]{};
                    const bool valid_content =
                        copy_json_string(payload, "page", page, sizeof(page), true) &&
                        copy_json_string(payload, "title", title, sizeof(title), true) &&
                        copy_json_string(payload, "detail", detail, sizeof(detail), true);
                    if (!valid_content) {
                        ESP_LOGW(TAG, "Ignoring malformed display page payload");
                    } else {
                        std::array<std::array<char, 97>, 4> item_text{};
                        std::array<const char*, 4> item_pointers{};
                        size_t item_count = 0;
                        const cJSON* items = cJSON_GetObjectItemCaseSensitive(payload, "items");
                        if (cJSON_IsArray(items)) {
                            const cJSON* item = nullptr;
                            cJSON_ArrayForEach(item, items) {
                                if (item_count >= item_text.size()) break;
                                if (!cJSON_IsString(item) || item->valuestring == nullptr) continue;
                                std::strncpy(
                                    item_text[item_count].data(),
                                    item->valuestring,
                                    item_text[item_count].size() - 1);
                                item_pointers[item_count] = item_text[item_count].data();
                                ++item_count;
                            }
                        }
                        const cJSON* counts = cJSON_GetObjectItemCaseSensitive(payload, "counts");
                        const auto read_count = [counts](const char* key) -> uint16_t {
                            const cJSON* value = cJSON_GetObjectItemCaseSensitive(counts, key);
                            if (!cJSON_IsNumber(value) || value->valuedouble < 0 ||
                                value->valuedouble > UINT16_MAX) return 0;
                            return static_cast<uint16_t>(value->valuedouble);
                        };
                        const esp_err_t result = DeviceDisplay::set_dashboard(
                            page, title, detail, item_pointers.data(), item_count,
                            read_count("todos"), read_count("reminders"), read_count("notes"),
                            read_count("notifications"), read_count("alarms"));
                        if (result != ESP_OK) {
                            ESP_LOGE(TAG, "Cannot render display page '%s': %s", page, esp_err_to_name(result));
                        }
                    }
                } else if (cJSON_IsString(display_state) && display_state->valuestring != nullptr) {
                    const esp_err_t display_result = DeviceDisplay::set_state(display_state->valuestring);
                    if (display_result != ESP_OK) {
                        ESP_LOGE(TAG, "Cannot render display state '%s': %s",
                            display_state->valuestring, esp_err_to_name(display_result));
                    }
                }
            } else if (std::strcmp(type->valuestring, "audio.transcription") == 0) {
                const esp_err_t result = DeviceDisplay::set_state("thinking");
                if (result != ESP_OK) ESP_LOGE(TAG, "Cannot show thinking face: %s", esp_err_to_name(result));
            } else if (std::strcmp(type->valuestring, "tts.start") == 0) {
                const cJSON* samples = cJSON_GetObjectItemCaseSensitive(payload, "samples");
                const cJSON* sample_rate = cJSON_GetObjectItemCaseSensitive(payload, "sample_rate");
                if (cJSON_IsNumber(samples) && samples->valuedouble > 0 &&
                    cJSON_IsNumber(sample_rate) && sample_rate->valuedouble > 0) {
                    const double duration_ms = samples->valuedouble * 1000.0 / sample_rate->valuedouble;
                    if (duration_ms <= 30'000) {
                        const esp_err_t result =
                            DeviceDisplay::set_speaking_for(
                                static_cast<uint32_t>(std::max(1.0, duration_ms)));
                        if (result != ESP_OK) {
                            ESP_LOGE(TAG, "Cannot animate speaking face: %s", esp_err_to_name(result));
                        }
                    }
                }
            }
            const cJSON* values = payload;
            const cJSON* nested_config = cJSON_GetObjectItemCaseSensitive(payload, "config");
            if (cJSON_IsObject(nested_config)) values = nested_config;
            const cJSON* brightness = cJSON_GetObjectItemCaseSensitive(values, "brightness");
            const cJSON* microphone = cJSON_GetObjectItemCaseSensitive(values, "microphone_enabled");
            if (cJSON_IsNumber(brightness) && brightness->valueint >= 0 && brightness->valueint <= 100) {
                const int next = brightness->valueint;
                if (apply_brightness(next) == ESP_OK) {
                    const esp_err_t result = persist_runtime_setting("brightness", static_cast<uint8_t>(next));
                    if (result != ESP_OK) ESP_LOGE(TAG, "Cannot persist brightness: %s", esp_err_to_name(result));
                }
            }
            if (cJSON_IsBool(microphone)) {
                const bool enabled = cJSON_IsTrue(microphone);
                microphone_stream_enabled.store(enabled);
                const esp_err_t capture_result = AudioI2S::set_capture_enabled(enabled);
                if (capture_result != ESP_OK) {
                    ESP_LOGE(TAG, "Cannot apply microphone capture setting: %s", esp_err_to_name(capture_result));
                }
                const esp_err_t result = persist_runtime_setting("mic_enabled", enabled ? 1 : 0);
                if (result != ESP_OK) ESP_LOGE(TAG, "Cannot persist microphone setting: %s", esp_err_to_name(result));
            }
            ESP_LOGD(TAG, "Received server event: %s", type->valuestring);
        } else {
            ESP_LOGW(TAG, "Ignoring malformed server control message");
        }
        cJSON_Delete(message);
    }
    control_buffer_used = 0;
}

void put_be16(uint8_t* output, uint16_t value) {
    output[0] = static_cast<uint8_t>(value >> 8);
    output[1] = static_cast<uint8_t>(value);
}

void put_be32(uint8_t* output, uint32_t value) {
    output[0] = static_cast<uint8_t>(value >> 24);
    output[1] = static_cast<uint8_t>(value >> 16);
    output[2] = static_cast<uint8_t>(value >> 8);
    output[3] = static_cast<uint8_t>(value);
}

void put_be64(uint8_t* output, uint64_t value) {
    for (int i = 7; i >= 0; --i) {
        output[i] = static_cast<uint8_t>(value);
        value >>= 8;
    }
}

void audio_uplink_task(void*) {
    auto queue = AudioI2S::capture_queue();
    AudioI2S::PcmBlock block{};
    AudioI2S::PcmBlock next_block{};
    std::array<int16_t, MAX_AUDIO_FRAME_SAMPLES> samples{};
    std::array<uint8_t, MAX_PACKET_SIZE> packet{};
    uint32_t requested_replay = 0;
    while (true) {
        if ((xEventGroupGetBits(websocket_events) & WEBSOCKET_READY_BIT) != 0 &&
            xQueueReceive(replay_requests, &requested_replay, 0) == pdTRUE) {
            replay_audio_range(requested_replay);
        }
        if (xQueueReceive(queue, &block, pdMS_TO_TICKS(250)) != pdTRUE) continue;
        if (!microphone_stream_enabled.load() ||
            (xEventGroupGetBits(websocket_events) & WEBSOCKET_READY_BIT) == 0) continue;
        if (xQueueReceive(replay_requests, &requested_replay, 0) == pdTRUE) {
            replay_audio_range(requested_replay);
            continue;
        }
        if (block.sample_count == 0 || block.sample_count > HardwareConfig::Audio::CAPTURE_SAMPLES) continue;
        size_t sample_count = block.sample_count;
        std::memcpy(samples.data(), block.samples, sample_count * sizeof(int16_t));
        if (xQueueReceive(queue, &next_block, pdMS_TO_TICKS(40)) == pdTRUE &&
            next_block.sample_count > 0 &&
            next_block.sample_count <= HardwareConfig::Audio::CAPTURE_SAMPLES &&
            sample_count + next_block.sample_count <= samples.size()) {
            std::memcpy(
                samples.data() + sample_count,
                next_block.samples,
                next_block.sample_count * sizeof(int16_t));
            sample_count += next_block.sample_count;
        }
        const size_t pcm_bytes = sample_count * sizeof(int16_t);
        if (AUDIO_HEADER_SIZE + pcm_bytes > packet.size()) continue;
        std::memcpy(packet.data(), "EAIA", 4);
        packet[4] = 1;
        const uint32_t sequence = next_audio_sequence.fetch_add(1);
        put_be32(packet.data() + 5, sequence);
        put_be64(packet.data() + 9, static_cast<uint64_t>(esp_timer_get_time()));
        put_be32(packet.data() + 17, HardwareConfig::Audio::SAMPLE_RATE);
        put_be16(packet.data() + 21, static_cast<uint16_t>(sample_count));
        packet[23] = HardwareConfig::Audio::CHANNELS;
        packet[24] = 1;
        std::memcpy(packet.data() + AUDIO_HEADER_SIZE, samples.data(), pcm_bytes);
        const size_t packet_length = AUDIO_HEADER_SIZE + pcm_bytes;
        portENTER_CRITICAL(&replay_lock);
        auto& cached = replay_window[sequence % REPLAY_WINDOW_SIZE];
        cached.sequence = sequence;
        cached.length = packet_length;
        cached.valid = true;
        std::memcpy(cached.bytes.data(), packet.data(), packet_length);
        portEXIT_CRITICAL(&replay_lock);
        const int64_t send_started_at = esp_timer_get_time();
        const int sent = send_websocket_binary(
            packet.data(), static_cast<int>(packet_length), AUDIO_SEND_TIMEOUT);
        if (sent != static_cast<int>(packet_length)) {
            const int64_t elapsed_ms = (esp_timer_get_time() - send_started_at) / 1000;
            wifi_ap_record_t ap_info{};
            const int rssi = esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK ? ap_info.rssi : -127;
            ESP_LOGW(
                TAG, "Audio send failed (sequence=%lu, result=%d, bytes=%u/%u, elapsed_ms=%lld); "
                "rssi=%d dBm; capture continues",
                static_cast<unsigned long>(sequence), sent,
                sent > 0 ? static_cast<unsigned>(sent) : 0U,
                static_cast<unsigned>(packet_length), static_cast<long long>(elapsed_ms), rssi);
        }
        static uint32_t frames_since_stack_report = 0;
        if (++frames_since_stack_report % 256 == 0) {
            ESP_LOGI(TAG, "Audio uplink stack high-water mark: %u bytes",
                static_cast<unsigned>(uxTaskGetStackHighWaterMark(nullptr)));
        }
    }
}

void network_task(void*) {
    wifi_events = xEventGroupCreate();
    if (wifi_events == nullptr) {
        ESP_LOGE(TAG, "Cannot allocate Wi-Fi state");
        vTaskDelete(nullptr);
        return;
    }
    esp_netif_create_default_wifi_ap();
    if (station_configured) esp_netif_create_default_wifi_sta();
    wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_config));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, nullptr));
    if (station_configured) {
        ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, nullptr));
    }
    ESP_ERROR_CHECK(start_provisioning_portal());
    ESP_ERROR_CHECK(esp_wifi_set_mode(station_configured ? WIFI_MODE_APSTA : WIFI_MODE_AP));
    if (station_configured) {
        websocket_events = xEventGroupCreate();
        replay_requests = xQueueCreate(1, sizeof(uint32_t));
        websocket_send_mutex = xSemaphoreCreateMutex();
        ota_requests = xQueueCreate(1, sizeof(OtaRequest));
        if (websocket_events == nullptr || replay_requests == nullptr ||
            websocket_send_mutex == nullptr || ota_requests == nullptr) {
            ESP_LOGE(TAG, "Cannot allocate WebSocket, replay, or OTA state");
            vTaskDelete(nullptr);
            return;
        }
        if (xTaskCreate(audio_uplink_task, "audio_uplink", AUDIO_UPLINK_STACK_BYTES, nullptr,
                configMAX_PRIORITIES - 4, nullptr) != pdPASS ||
            xTaskCreate(ota_task, "ota_update", 10240, nullptr, configMAX_PRIORITIES - 6, nullptr) != pdPASS) {
            ESP_LOGE(TAG, "Cannot create audio or OTA task");
            vTaskDelete(nullptr);
            return;
        }
        wifi_config_t station{};
        std::memcpy(station.sta.ssid, config.wifi_ssid.data(), std::strlen(config.wifi_ssid.data()));
        std::memcpy(station.sta.password, config.wifi_password.data(), std::strlen(config.wifi_password.data()));
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &station));
    }
    ESP_ERROR_CHECK(esp_wifi_start());
    if (!station_configured) {
        ESP_LOGW(TAG, "Setup dashboard running; configure Wi-Fi and a secure production backend to continue");
        vTaskDelete(nullptr);
        return;
    }
    const esp_err_t power_save_result = esp_wifi_set_ps(WIFI_PS_NONE);
    if (power_save_result != ESP_OK) {
        ESP_LOGE(TAG, "Cannot disable Wi-Fi power save for continuous audio: %s",
            esp_err_to_name(power_save_result));
        vTaskDelete(nullptr);
        return;
    }
    ESP_LOGI(TAG, "Wi-Fi power save disabled for continuous audio streaming");
    ESP_ERROR_CHECK(esp_wifi_connect());
    xEventGroupWaitBits(wifi_events, CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);

    const esp_sntp_config_t sntp_config = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    esp_err_t time_result = esp_netif_sntp_init(&sntp_config);
    if (time_result == ESP_OK) {
        time_result = esp_netif_sntp_sync_wait(pdMS_TO_TICKS(10000));
        if (time_result != ESP_OK) ESP_LOGW(TAG, "NTP time sync unavailable; continuing with device clock");
    } else if (time_result != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "Cannot start NTP time sync: %s", esp_err_to_name(time_result));
    }

    char uri[320];
    const char* backend = config.backend_url.data();
    const size_t backend_length = std::strlen(backend);
    const int uri_length = std::snprintf(uri, sizeof(uri), "%.*s/api/v1/ws/devices/%s/audio",
        static_cast<int>(backend_length - (backend_length > 0 && backend[backend_length - 1] == '/' ? 1 : 0)),
        backend, config.device_id.data());
    if (uri_length <= 0 || uri_length >= static_cast<int>(sizeof(uri))) {
        ESP_LOGE(TAG, "Configured backend WebSocket URL is too long");
        vTaskDelete(nullptr);
        return;
    }
    char headers[180];
    std::snprintf(headers, sizeof(headers), "Authorization: Bearer %s\r\n", config.device_token.data());
    esp_websocket_client_config_t ws_config{};
    ws_config.uri = uri;
    ws_config.headers = headers;
    ws_config.buffer_size = 2048;
    ws_config.task_stack = 6144;
    ws_config.ping_interval_sec = 20;
    ws_config.network_timeout_ms = 10000;
    ws_config.reconnect_timeout_ms = 5000;
    ws_config.disable_auto_reconnect = false;
    ws_config.crt_bundle_attach = esp_crt_bundle_attach;
    websocket = esp_websocket_client_init(&ws_config);
    if (websocket == nullptr) {
        ESP_LOGE(TAG, "Cannot create WebSocket client for %s", uri);
        vTaskDelete(nullptr);
        return;
    }
    ESP_ERROR_CHECK(esp_websocket_register_events(
        websocket, WEBSOCKET_EVENT_ANY, websocket_event_handler, nullptr));
    ESP_ERROR_CHECK(esp_websocket_client_start(websocket));
    vTaskDelete(nullptr);
}

}

namespace DeviceNetwork {

esp_err_t start() {
    esp_err_t result = nvs_flash_init();
    if (result == ESP_ERR_NVS_NO_FREE_PAGES || result == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_RETURN_ON_ERROR(nvs_flash_erase(), TAG, "Cannot reset NVS storage");
        result = nvs_flash_init();
    }
    if (result != ESP_OK) return result;
    result = esp_netif_init();
    if (result != ESP_OK && result != ESP_ERR_INVALID_STATE) return result;
    result = esp_event_loop_create_default();
    if (result != ESP_OK && result != ESP_ERR_INVALID_STATE) return result;
    load_runtime_settings();
    result = apply_brightness(brightness_percent);
    if (result != ESP_OK) return result;
    result = AudioI2S::set_capture_enabled(microphone_stream_enabled.load());
    if (result != ESP_OK) return result;
    saved_config_loaded = load_config();
    station_configured = saved_config_loaded && is_public_wss_origin(config.backend_url.data());
    if (!station_configured) {
        ESP_LOGW(TAG, "No secure production backend configured; station mode is disabled until setup");
        result = AudioI2S::set_capture_enabled(false);
        if (result != ESP_OK) return result;
    }
    if (xTaskCreate(network_task, "device_network", 8192, nullptr, configMAX_PRIORITIES - 5, nullptr) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

}

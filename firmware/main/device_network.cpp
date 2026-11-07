#include "device_network.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <string>

#include "audio_i2s.h"
#include "cJSON.h"
#include "device_display.h"
#include "device_games.h"
#include "device_schedule.h"
#include "esp_app_desc.h"
#include "esp_check.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_http_server.h"
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
#include "hardware_config.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "psa/crypto.h"

namespace
{

    constexpr char TAG[] = "device_network";
    constexpr char NVS_NAMESPACE[] = "assistant";
    constexpr EventBits_t CONNECTED_BIT = BIT0;
    constexpr EventBits_t START_AP_BIT = BIT1;
    constexpr EventBits_t WEBSOCKET_READY_BIT = BIT2;

    constexpr size_t AUDIO_HEADER_SIZE = 25;
    constexpr size_t MAX_AUDIO_FRAME_SAMPLES = HardwareConfig::Audio::CAPTURE_SAMPLES * 4;
    constexpr size_t MAX_PACKET_SIZE = AUDIO_HEADER_SIZE + MAX_AUDIO_FRAME_SAMPLES * sizeof(int16_t);
    constexpr uint32_t AUDIO_UPLINK_STACK_BYTES = 12 * 1024;
    constexpr size_t CONTROL_BUFFER_SIZE = 2048;
    constexpr size_t REPLAY_WINDOW_SIZE = 8;
    constexpr size_t OTA_CHUNK_SIZE = 2048;
    constexpr size_t OTA_MAX_IMAGE_SIZE = 6 * 1024 * 1024;
    constexpr TickType_t AUDIO_SEND_TIMEOUT = pdMS_TO_TICKS(3000);
    constexpr char SETUP_AP_PASSWORD[] = "BuddySetup-ESP32S3";

    constexpr int MAX_WIFI_RETRIES = 5;
    int wifi_retry_count = 0;

    // DNS Header structure per RFC 1035 for captive portal redirection
    struct __attribute__((packed)) DnsHeader
    {
        uint16_t id;
        uint16_t flags;
        uint16_t qd_count;
        uint16_t an_count;
        uint16_t ns_count;
        uint16_t ar_count;
    };

    TaskHandle_t dns_task_handle = nullptr;

    struct ReplayFrame
    {
        uint32_t sequence = 0;
        size_t length = 0;
        bool valid = false;
        std::array<uint8_t, MAX_PACKET_SIZE> bytes{};
    };

    struct DeviceConfig
    {
        std::array<char, 33> wifi_ssid{};
        std::array<char, 65> wifi_password{};
        std::array<char, 129> backend_url{};
        std::array<char, 65> device_id{};
        std::array<char, 129> device_token{};
        std::array<char, 3> country{};
        std::array<char, 41> timezone{};
        std::array<char, 41> city{};
    };

    struct OtaRequest
    {
        std::array<char, 65> release_id{};
        std::array<char, 65> version{};
        std::array<char, 65> sha256{};
        std::array<char, 256> download_path{};
        uint32_t size_bytes = 0;
    };

    DeviceConfig config;
    SemaphoreHandle_t config_mutex = nullptr;
    bool saved_config_loaded = false;
    bool portal_session = false;
    constexpr uint32_t PORTAL_SESSION_TIMEOUT_MS = 10 * 60 * 1000;
    bool station_configured = false;
    bool setup_ap_active = false;
    char setup_ssid[32]{};
    EventGroupHandle_t wifi_events = nullptr;
    EventGroupHandle_t websocket_events = nullptr;
    esp_websocket_client_handle_t websocket = nullptr;
    alignas(int16_t) char control_buffer[CONTROL_BUFFER_SIZE];
    size_t control_buffer_used = 0;
    std::atomic_bool microphone_stream_enabled{true};
    std::atomic<uint32_t> next_audio_sequence{0};
    std::array<ReplayFrame, REPLAY_WINDOW_SIZE> replay_window{};
    QueueHandle_t replay_requests = nullptr;
    QueueHandle_t ota_requests = nullptr;
    SemaphoreHandle_t websocket_send_mutex = nullptr;
    portMUX_TYPE replay_lock = portMUX_INITIALIZER_UNLOCKED;
    int brightness_percent = 50;
    bool backlight_initialized = false;
    uint64_t boot_identifier = 0;

    bool copy_json_string(const cJSON *object, const char *key, char *target, size_t capacity, bool required);
    bool request_is_from_setup_ap(httpd_req_t *request);
    esp_err_t persist_config(const DeviceConfig &value);

    // Dynamic DNS server resolving all domain queries to 192.168.4.1
    void dns_server_task(void *)
    {
        int socket_fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (socket_fd < 0)
        {
            ESP_LOGE(TAG, "Failed to create DNS socket");
            vTaskDelete(nullptr);
            return;
        }

        int enable = 1;
        setsockopt(socket_fd, SOL_SOCKET, SO_REUSEADDR, &enable, sizeof(enable));

        sockaddr_in server_addr{};
        server_addr.sin_family = AF_INET;
        server_addr.sin_addr.s_addr = htonl(INADDR_ANY);
        server_addr.sin_port = htons(53);

        if (bind(socket_fd, reinterpret_cast<sockaddr *>(&server_addr), sizeof(server_addr)) < 0)
        {
            ESP_LOGE(TAG, "Failed to bind DNS socket on port 53");
            close(socket_fd);
            vTaskDelete(nullptr);
            return;
        }

        ESP_LOGI(TAG, "Captive Portal DNS Server running on UDP port 53");

        std::array<uint8_t, 512> rx_buffer{};
        sockaddr_in client_addr{};
        socklen_t client_addr_len = sizeof(client_addr);

        while (setup_ap_active)
        {
            int len = recvfrom(socket_fd, rx_buffer.data(), rx_buffer.size(), 0,
                               reinterpret_cast<sockaddr *>(&client_addr), &client_addr_len);

            if (len < static_cast<int>(sizeof(DnsHeader)))
                continue;

            auto *dns_hdr = reinterpret_cast<DnsHeader *>(rx_buffer.data());

            // Standard DNS response, 1 Answer
            dns_hdr->flags = htons(0x8180);
            dns_hdr->an_count = htons(1);

            int reply_len = len;

            // Name pointer to question section (0xC00C)
            rx_buffer[reply_len++] = 0xC0;
            rx_buffer[reply_len++] = 0x0C;

            // Type A (0x0001)
            rx_buffer[reply_len++] = 0x00;
            rx_buffer[reply_len++] = 0x01;

            // Class IN (0x0001)
            rx_buffer[reply_len++] = 0x00;
            rx_buffer[reply_len++] = 0x01;

            // TTL: 60 seconds
            rx_buffer[reply_len++] = 0x00;
            rx_buffer[reply_len++] = 0x00;
            rx_buffer[reply_len++] = 0x00;
            rx_buffer[reply_len++] = 0x3C;

            // Data length: 4 bytes (IPv4)
            rx_buffer[reply_len++] = 0x00;
            rx_buffer[reply_len++] = 0x04;

            // Target AP IP: 192.168.4.1
            rx_buffer[reply_len++] = 192;
            rx_buffer[reply_len++] = 168;
            rx_buffer[reply_len++] = 4;
            rx_buffer[reply_len++] = 1;

            sendto(socket_fd, rx_buffer.data(), reply_len, 0,
                   reinterpret_cast<sockaddr *>(&client_addr), client_addr_len);
        }

        close(socket_fd);
        dns_task_handle = nullptr;
        vTaskDelete(nullptr);
    }

    void start_dns_server()
    {
        if (dns_task_handle == nullptr)
        {
            xTaskCreate(dns_server_task, "dns_captive", 3072, nullptr, configMAX_PRIORITIES - 7, &dns_task_handle);
        }
    }

    // Redirects OS probe requests to root page triggering system popups
    esp_err_t captive_redirect_handler(httpd_req_t *req)
    {
        httpd_resp_set_status(req, "302 Found");
        httpd_resp_set_hdr(req, "Location", "http://192.168.4.1/");
        httpd_resp_send(req, nullptr, 0);
        return ESP_OK;
    }

    int send_websocket_text(const char *payload, int length, TickType_t timeout)
    {
        if (websocket == nullptr || websocket_send_mutex == nullptr || payload == nullptr || length <= 0)
        {
            return ESP_FAIL;
        }
        if (xSemaphoreTake(websocket_send_mutex, timeout) != pdTRUE)
            return ESP_ERR_TIMEOUT;
        int result = ESP_FAIL;
        if (websocket != nullptr)
        {
            result = esp_websocket_client_send_text(websocket, payload, length, timeout);
        }
        xSemaphoreGive(websocket_send_mutex);
        return result;
    }

    bool report_schedule_fired(const char *id, const char *title, const char *kind)
    {
        if (websocket == nullptr || !esp_websocket_client_is_connected(websocket))
            return false;
        cJSON *root = cJSON_CreateObject();
        cJSON *body = cJSON_CreateObject();
        if (root == nullptr || body == nullptr)
        {
            cJSON_Delete(root);
            cJSON_Delete(body);
            return false;
        }

        char device_id[65]{};
        if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
        {
            std::strncpy(device_id, config.device_id.data(), sizeof(device_id) - 1);
            xSemaphoreGive(config_mutex);
        }

        cJSON_AddNumberToObject(root, "protocol_version", 1);
        cJSON_AddStringToObject(root, "message_id", id);
        cJSON_AddStringToObject(root, "device_id", device_id);
        cJSON_AddStringToObject(root, "timestamp", "1970-01-01T00:00:00Z");
        cJSON_AddStringToObject(root, "type", "schedule.fired");
        cJSON_AddStringToObject(body, "id", id);
        cJSON_AddStringToObject(body, "title", title);
        cJSON_AddStringToObject(body, "kind", kind);
        cJSON_AddItemToObject(root, "payload", body);
        char *encoded = cJSON_PrintUnformatted(root);
        cJSON_Delete(root);
        if (encoded == nullptr)
            return false;
        const int sent = send_websocket_text(encoded, static_cast<int>(std::strlen(encoded)), pdMS_TO_TICKS(1000));
        cJSON_free(encoded);
        return sent >= 0;
    }

    void handle_device_setup(const cJSON *payload)
    {
        char action[16]{};
        copy_json_string(payload, "action", action, sizeof(action), false);
        const bool open_portal = std::strcmp(action, "open_portal") == 0;
        const bool erase_setup = std::strcmp(action, "erase_setup") == 0;
        if (!open_portal && !erase_setup)
            return;
        ESP_LOGW(TAG, "Backend requested %s", action);
        nvs_handle_t handle;
        if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle) != ESP_OK)
        {
            ESP_LOGE(TAG, "Cannot open NVS for %s", action);
            return;
        }
        esp_err_t result = erase_setup ? nvs_erase_all(handle) : nvs_set_u8(handle, "portal_once", 1);
        if (result == ESP_OK)
            result = nvs_commit(handle);
        nvs_close(handle);
        if (result != ESP_OK)
        {
            ESP_LOGE(TAG, "Setup request failed: %s", esp_err_to_name(result));
            return;
        }
        AudioI2S::queue_sound_effect("notification");
        vTaskDelay(pdMS_TO_TICKS(1500));
        esp_restart();
    }

    void handle_game_command(const cJSON *payload)
    {
        static DeviceGames::Reply reply;
        char action[12]{};
        char game[32]{};
        char text[160]{};
        char difficulty[12]{};
        char request_id[40]{};
        copy_json_string(payload, "action", action, sizeof(action), false);
        copy_json_string(payload, "game", game, sizeof(game), false);
        copy_json_string(payload, "text", text, sizeof(text), false);
        copy_json_string(payload, "request_id", request_id, sizeof(request_id), false);
        copy_json_string(payload, "difficulty", difficulty, sizeof(difficulty), false);
        if (std::strcmp(action, "list") == 0)
            DeviceGames::list(reply);
        else if (std::strcmp(action, "start") == 0)
            DeviceGames::start(game, difficulty, reply);
        else if (std::strcmp(action, "stop") == 0)
            DeviceGames::stop(reply);
        else
            DeviceGames::input(text, reply);
        if (reply.handled && reply.sound != nullptr)
            AudioI2S::queue_sound_effect(reply.sound);

        cJSON *root = cJSON_CreateObject();
        cJSON *body = cJSON_CreateObject();
        if (root == nullptr || body == nullptr)
        {
            cJSON_Delete(root);
            cJSON_Delete(body);
            return;
        }

        char device_id[65]{};
        if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
        {
            std::strncpy(device_id, config.device_id.data(), sizeof(device_id) - 1);
            xSemaphoreGive(config_mutex);
        }

        cJSON_AddNumberToObject(root, "protocol_version", 1);
        cJSON_AddStringToObject(root, "message_id", request_id[0] != '\0' ? request_id : "game-event");
        cJSON_AddStringToObject(root, "device_id", device_id);
        cJSON_AddStringToObject(root, "timestamp", "1970-01-01T00:00:00Z");
        cJSON_AddStringToObject(root, "type", "game.event");
        cJSON_AddStringToObject(body, "request_id", request_id);
        cJSON_AddStringToObject(body, "action", action);
        cJSON_AddBoolToObject(body, "handled", reply.handled);
        cJSON_AddStringToObject(body, "speech", reply.speech);
        cJSON_AddStringToObject(body, "game", reply.game);
        cJSON_AddStringToObject(body, "status", reply.status);
        cJSON_AddBoolToObject(body, "active", reply.active);
        cJSON_AddBoolToObject(body, "finished", reply.finished);
        if (reply.outcome[0] != '\0')
        {
            cJSON_AddStringToObject(body, "outcome", reply.outcome);
            cJSON_AddStringToObject(body, "difficulty", reply.difficulty);
            cJSON_AddNumberToObject(body, "score", reply.score);
            cJSON_AddNumberToObject(body, "max_score", reply.max_score);
            cJSON_AddNumberToObject(body, "rounds", reply.rounds);
            cJSON_AddNumberToObject(body, "duration_seconds", reply.duration_seconds);
        }
        cJSON_AddItemToObject(root, "payload", body);
        char *encoded = cJSON_PrintUnformatted(root);
        cJSON_Delete(root);
        if (encoded == nullptr)
            return;
        const int live_sent = send_websocket_text(encoded, static_cast<int>(std::strlen(encoded)), pdMS_TO_TICKS(500));
        cJSON_free(encoded);
        if (live_sent >= 0 && reply.record_id != 0)
            DeviceGames::mark_synced(reply.record_id);
    }

    void send_game_history()
    {
        static DeviceGames::LogEntry entries[8];
        const size_t count = DeviceGames::unsynced(entries, 8);
        if (count == 0)
            return;
        cJSON *root = cJSON_CreateObject();
        cJSON *body = cJSON_CreateObject();
        cJSON *records = cJSON_CreateArray();
        if (root == nullptr || body == nullptr || records == nullptr)
        {
            cJSON_Delete(root);
            cJSON_Delete(body);
            cJSON_Delete(records);
            return;
        }
        for (size_t index = 0; index < count; ++index)
        {
            cJSON *item = cJSON_CreateObject();
            if (item == nullptr)
                continue;
            cJSON_AddNumberToObject(item, "id", entries[index].id);
            cJSON_AddNumberToObject(item, "ts", entries[index].timestamp);
            cJSON_AddStringToObject(item, "game", entries[index].game);
            cJSON_AddStringToObject(item, "outcome", entries[index].outcome);
            cJSON_AddStringToObject(item, "difficulty", entries[index].difficulty);
            cJSON_AddNumberToObject(item, "score", entries[index].score);
            cJSON_AddNumberToObject(item, "max_score", entries[index].max_score);
            cJSON_AddNumberToObject(item, "rounds", entries[index].rounds);
            cJSON_AddNumberToObject(item, "duration_seconds", entries[index].duration_seconds);
            cJSON_AddItemToArray(records, item);
        }

        char device_id[65]{};
        if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
        {
            std::strncpy(device_id, config.device_id.data(), sizeof(device_id) - 1);
            xSemaphoreGive(config_mutex);
        }

        cJSON_AddItemToObject(body, "records", records);
        cJSON_AddNumberToObject(root, "protocol_version", 1);
        cJSON_AddStringToObject(root, "message_id", "game-history");
        cJSON_AddStringToObject(root, "device_id", device_id);
        cJSON_AddStringToObject(root, "timestamp", "1970-01-01T00:00:00Z");
        cJSON_AddStringToObject(root, "type", "game.history");
        cJSON_AddItemToObject(root, "payload", body);
        char *encoded = cJSON_PrintUnformatted(root);
        cJSON_Delete(root);
        if (encoded == nullptr)
            return;
        const int sent = send_websocket_text(encoded, static_cast<int>(std::strlen(encoded)), pdMS_TO_TICKS(2000));
        cJSON_free(encoded);
        if (sent < 0)
            return;
        for (size_t index = 0; index < count; ++index)
            DeviceGames::mark_synced(entries[index].id);
        ESP_LOGI(TAG, "Uploaded %u offline game results", static_cast<unsigned>(count));
    }

    int send_websocket_binary(const uint8_t *payload, int length, TickType_t timeout)
    {
        if (websocket == nullptr || websocket_send_mutex == nullptr || payload == nullptr || length <= 0)
        {
            return ESP_FAIL;
        }
        if (xSemaphoreTake(websocket_send_mutex, timeout) != pdTRUE)
            return ESP_ERR_TIMEOUT;
        int result = ESP_FAIL;
        if (websocket != nullptr)
        {
            result = esp_websocket_client_send_bin(
                websocket, reinterpret_cast<const char *>(payload), length, timeout);
        }
        xSemaphoreGive(websocket_send_mutex);
        return result;
    }

    void report_ota_status(const OtaRequest &update, const char *state, int progress)
    {
        if (websocket == nullptr || (xEventGroupGetBits(websocket_events) & WEBSOCKET_READY_BIT) == 0)
            return;
        char message[512];
        time_t now = time(nullptr);
        struct tm utc_time{};
        char timestamp[32] = "1970-01-01T00:00:00Z";
        if (gmtime_r(&now, &utc_time) != nullptr)
        {
            std::strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%SZ", &utc_time);
        }
        char device_id[65]{};
        if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
        {
            std::strncpy(device_id, config.device_id.data(), sizeof(device_id) - 1);
            xSemaphoreGive(config_mutex);
        }
        const uint64_t message_id = static_cast<uint64_t>(esp_timer_get_time());
        const int length = std::snprintf(
            message, sizeof(message),
            "{\"protocol_version\":1,\"message_id\":\"ota-%llu\",\"device_id\":\"%s\","
            "\"timestamp\":\"%s\",\"type\":\"ota.status\",\"payload\":{"
            "\"release_id\":\"%s\",\"state\":\"%s\",\"progress\":%d,\"version\":\"%s\"}}",
            static_cast<unsigned long long>(message_id), device_id, timestamp,
            update.release_id.data(), state, progress, update.version.data());
        if (length > 0 && length < static_cast<int>(sizeof(message)))
        {
            const int sent = send_websocket_text(message, length, pdMS_TO_TICKS(1000));
            if (sent < 0)
                ESP_LOGW(TAG, "OTA status event could not be sent: %s", state);
        }
    }

    bool make_http_base_url(char *output, size_t capacity)
    {
        char source[129]{};
        if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
        {
            std::strncpy(source, config.backend_url.data(), sizeof(source) - 1);
            xSemaphoreGive(config_mutex);
        }
        else
        {
            return false;
        }

        const char *replacement = nullptr;
        size_t prefix_length = 0;
        if (std::strncmp(source, "wss://", 6) == 0)
        {
            replacement = "https://";
            prefix_length = 6;
        }
        else if (std::strncmp(source, "ws://", 5) == 0)
        {
            replacement = "http://";
            prefix_length = 5;
        }
        else if (std::strncmp(source, "https://", 8) == 0 ||
                 std::strncmp(source, "http://", 7) == 0)
        {
            const size_t length = std::strlen(source);
            if (length >= capacity)
                return false;
            std::memcpy(output, source, length + 1);
            while (length > 0 && output[length - 1] == '/')
                output[length - 1] = '\0';
            return true;
        }
        else
        {
            return false;
        }
        const size_t source_length = std::strlen(source);
        if (source_length < prefix_length || source_length - prefix_length + std::strlen(replacement) >= capacity)
        {
            return false;
        }
        std::snprintf(output, capacity, "%s%s", replacement, source + prefix_length);
        size_t length = std::strlen(output);
        while (length > 0 && output[length - 1] == '/')
            output[--length] = '\0';
        return true;
    }

    bool make_ota_url(const OtaRequest &update, char *output, size_t capacity)
    {
        char expected_path[160]{};
        const int expected_length = std::snprintf(
            expected_path, sizeof(expected_path), "/api/v1/ota/releases/%s/binary", update.release_id.data());
        if (expected_length <= 0 || expected_length >= static_cast<int>(sizeof(expected_path)) ||
            std::strcmp(update.download_path.data(), expected_path) != 0)
        {
            return false;
        }
        char base[192]{};
        if (!make_http_base_url(base, sizeof(base)))
            return false;
        const int length = std::snprintf(
            output, capacity, "%s%s", base, update.download_path.data());
        return length > 0 && length < static_cast<int>(capacity);
    }

    void send_ota_update(const OtaRequest &update)
    {
        char url[512]{};
        if (update.size_bytes == 0 || update.size_bytes > OTA_MAX_IMAGE_SIZE ||
            !make_ota_url(update, url, sizeof(url)))
        {
            report_ota_status(update, "failed", 0);
            return;
        }
        report_ota_status(update, "downloading", 0);
        esp_http_client_config_t http_config{};
        http_config.url = url;
        http_config.timeout_ms = 30000;
        http_config.crt_bundle_attach = esp_crt_bundle_attach;
        esp_http_client_handle_t client = esp_http_client_init(&http_config);
        if (client == nullptr)
        {
            report_ota_status(update, "failed", 0);
            return;
        }

        char device_token[129]{};
        if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
        {
            std::strncpy(device_token, config.device_token.data(), sizeof(device_token) - 1);
            xSemaphoreGive(config_mutex);
        }

        esp_err_t result = esp_http_client_set_header(client, "Authorization", device_token);
        if (result == ESP_OK)
            result = esp_http_client_open(client, 0);
        int64_t content_length = result == ESP_OK ? esp_http_client_fetch_headers(client) : -1;
        if (result != ESP_OK || esp_http_client_get_status_code(client) != 200 ||
            content_length <= 0 || content_length > OTA_MAX_IMAGE_SIZE ||
            static_cast<uint32_t>(content_length) != update.size_bytes)
        {
            ESP_LOGE(TAG, "OTA download request failed or image size mismatched");
            esp_http_client_close(client);
            esp_http_client_cleanup(client);
            report_ota_status(update, "failed", 0);
            return;
        }
        const esp_partition_t *partition = esp_ota_get_next_update_partition(nullptr);
        if (partition == nullptr || content_length > partition->size)
        {
            esp_http_client_close(client);
            esp_http_client_cleanup(client);
            report_ota_status(update, "failed", 0);
            return;
        }
        esp_ota_handle_t ota_handle = 0;
        result = esp_ota_begin(partition, static_cast<size_t>(content_length), &ota_handle);
        if (result != ESP_OK)
        {
            esp_http_client_close(client);
            esp_http_client_cleanup(client);
            report_ota_status(update, "failed", 0);
            return;
        }
        psa_hash_operation_t hash_operation = PSA_HASH_OPERATION_INIT;
        psa_status_t hash_result = psa_crypto_init();
        if (hash_result == PSA_SUCCESS)
        {
            hash_result = psa_hash_setup(&hash_operation, PSA_ALG_SHA_256);
        }
        if (hash_result != PSA_SUCCESS)
            result = ESP_FAIL;
        std::array<uint8_t, OTA_CHUNK_SIZE> buffer{};
        size_t total = 0;
        int last_reported = -1;
        bool image_magic_valid = false;
        while (result == ESP_OK && total < static_cast<size_t>(content_length))
        {
            const int received = esp_http_client_read(
                client, reinterpret_cast<char *>(buffer.data()),
                std::min(buffer.size(), static_cast<size_t>(content_length) - total));
            if (received <= 0)
            {
                result = received == 0 ? ESP_FAIL : static_cast<esp_err_t>(received);
                break;
            }
            if (total == 0)
                image_magic_valid = buffer[0] == 0xE9;
            if (!image_magic_valid)
            {
                result = ESP_ERR_INVALID_RESPONSE;
                break;
            }
            result = esp_ota_write(ota_handle, buffer.data(), static_cast<size_t>(received));
            if (result == ESP_OK && hash_result == PSA_SUCCESS)
            {
                hash_result = psa_hash_update(&hash_operation, buffer.data(), static_cast<size_t>(received));
                if (hash_result != PSA_SUCCESS)
                    result = ESP_FAIL;
            }
            total += static_cast<size_t>(received);
            const int progress = static_cast<int>((total * 100) / static_cast<size_t>(content_length));
            if (progress / 10 != last_reported / 10 || progress == 100)
            {
                report_ota_status(update, "downloading", progress);
                last_reported = progress;
            }
        }
        std::array<uint8_t, 32> digest{};
        size_t digest_length = 0;
        if (result == ESP_OK && hash_result == PSA_SUCCESS)
        {
            hash_result = psa_hash_finish(&hash_operation, digest.data(), digest.size(), &digest_length);
            if (hash_result != PSA_SUCCESS || digest_length != digest.size())
            {
                psa_hash_abort(&hash_operation);
                result = ESP_FAIL;
            }
        }
        else
        {
            psa_hash_abort(&hash_operation);
        }
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        char actual_hash[65]{};
        for (size_t index = 0; index < digest.size(); ++index)
        {
            std::snprintf(actual_hash + index * 2, 3, "%02x", digest[index]);
        }
        if (result == ESP_OK && std::strcmp(actual_hash, update.sha256.data()) != 0)
        {
            result = ESP_ERR_INVALID_CRC;
        }
        report_ota_status(update, "verifying", 100);
        if (result == ESP_OK)
            result = esp_ota_end(ota_handle);
        else
            esp_ota_abort(ota_handle);
        if (result == ESP_OK)
            result = esp_ota_set_boot_partition(partition);
        if (result != ESP_OK)
        {
            ESP_LOGE(TAG, "OTA image verification or activation failed: %s", esp_err_to_name(result));
            report_ota_status(update, "failed", 100);
            return;
        }
        report_ota_status(update, "rebooting", 100);
        vTaskDelay(pdMS_TO_TICKS(1000));
        esp_restart();
    }

    void ota_task(void *)
    {
        OtaRequest update{};
        while (true)
        {
            if (xQueueReceive(ota_requests, &update, portMAX_DELAY) == pdTRUE)
            {
                send_ota_update(update);
            }
        }
    }

    bool queue_ota_release(const cJSON *payload)
    {
        const cJSON *release = cJSON_GetObjectItemCaseSensitive(payload, "release");
        if (!cJSON_IsObject(release))
            return false;
        OtaRequest update{};
        const cJSON *size = cJSON_GetObjectItemCaseSensitive(release, "size_bytes");
        const cJSON *hardware = cJSON_GetObjectItemCaseSensitive(release, "hardware_target");
        if (!cJSON_IsNumber(size) || size->valuedouble <= 0 ||
            size->valuedouble > OTA_MAX_IMAGE_SIZE || !cJSON_IsString(hardware) ||
            std::strcmp(hardware->valuestring, "esp32-s3-n16r8") != 0 ||
            !copy_json_string(release, "id", update.release_id.data(), update.release_id.size(), true) ||
            !copy_json_string(release, "version", update.version.data(), update.version.size(), true) ||
            !copy_json_string(release, "sha256", update.sha256.data(), update.sha256.size(), true) ||
            !copy_json_string(release, "download_path", update.download_path.data(), update.download_path.size(), true) ||
            std::strlen(update.sha256.data()) != 64)
        {
            return false;
        }
        for (const char *digit = update.sha256.data(); *digit != '\0'; ++digit)
        {
            if (!((*digit >= '0' && *digit <= '9') || (*digit >= 'a' && *digit <= 'f')))
                return false;
        }
        update.size_bytes = static_cast<uint32_t>(size->valuedouble);
        return xQueueSend(ota_requests, &update, 0) == pdTRUE;
    }

    esp_err_t apply_brightness(int percent)
    {
        if (percent < 0 || percent > 100)
            return ESP_ERR_INVALID_ARG;
        if (!backlight_initialized)
        {
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

    esp_err_t persist_runtime_setting(const char *key, uint8_t value)
    {
        nvs_handle_t handle;
        esp_err_t result = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle);
        if (result != ESP_OK)
            return result;
        result = nvs_set_u8(handle, key, value);
        if (result == ESP_OK)
            result = nvs_commit(handle);
        nvs_close(handle);
        return result;
    }

    void load_runtime_settings()
    {
        nvs_handle_t handle;
        esp_err_t result = nvs_open(NVS_NAMESPACE, NVS_READONLY, &handle);
        if (result == ESP_ERR_NVS_NOT_FOUND)
            return;
        if (result != ESP_OK)
        {
            ESP_LOGE(TAG, "Cannot read runtime settings: %s", esp_err_to_name(result));
            return;
        }
        uint8_t stored = 0;
        result = nvs_get_u8(handle, "brightness", &stored);
        if (result == ESP_OK && stored <= 100)
        {
            brightness_percent = stored;
        }
        else if (result == ESP_OK)
        {
            ESP_LOGW(TAG, "Ignoring invalid stored brightness setting");
        }
        else if (result != ESP_ERR_NVS_NOT_FOUND)
        {
            ESP_LOGE(TAG, "Cannot read stored brightness setting: %s", esp_err_to_name(result));
        }
        result = nvs_get_u8(handle, "mic_enabled", &stored);
        if (result == ESP_OK)
        {
            microphone_stream_enabled.store(stored != 0);
        }
        else if (result != ESP_ERR_NVS_NOT_FOUND)
        {
            ESP_LOGE(TAG, "Cannot read stored microphone setting: %s", esp_err_to_name(result));
        }
        nvs_close(handle);
    }

    bool copy_json_string(const cJSON *object, const char *key, char *target, size_t capacity, bool required)
    {
        const cJSON *item = cJSON_GetObjectItemCaseSensitive(object, key);
        if (!cJSON_IsString(item) || item->valuestring == nullptr)
            return !required;
        const size_t length = std::strlen(item->valuestring);
        if (length == 0)
            return !required;
        if (length >= capacity)
            return false;
        for (size_t i = 0; i < length; ++i)
        {
            if (static_cast<unsigned char>(item->valuestring[i]) < 0x20)
                return false;
        }
        std::memcpy(target, item->valuestring, length + 1);
        return true;
    }

    bool is_public_wss_origin(const char *value)
    {
        std::string backend(value == nullptr ? "" : value);
        if (backend.rfind("wss://", 0) != 0)
            return false;
        std::string host = backend.substr(6);
        if (!host.empty() && host.back() == '/')
            host.pop_back();
        if (host.empty() || host.find_first_of("/?#@ \r\n") != std::string::npos)
            return false;
        const size_t port_separator = host.find(':');
        std::string hostname = host.substr(0, port_separator);
        std::transform(hostname.begin(), hostname.end(), hostname.begin(), [](unsigned char character)
                       { return static_cast<char>(std::tolower(character)); });
        const bool numeric_address = !hostname.empty() && std::all_of(
                                                              hostname.begin(), hostname.end(), [](unsigned char character)
                                                              { return (character >= '0' && character <= '9') || character == '.'; });
        const bool local_domain = hostname == "localhost" ||
                                  (hostname.size() >= 6 && hostname.compare(hostname.size() - 6, 6, ".local") == 0);
        return hostname.find('.') != std::string::npos && !numeric_address && !local_domain;
    }

    bool normalize_backend_origin(const char *value, char *output, size_t capacity)
    {
        if (value == nullptr || output == nullptr || capacity == 0)
            return false;
        std::string backend(value);
        const size_t first = backend.find_first_not_of(" \t\r\n");
        if (first == std::string::npos)
            return false;
        const size_t last = backend.find_last_not_of(" \t\r\n");
        backend = backend.substr(first, last - first + 1);

        if (backend.rfind("https://", 0) == 0)
        {
            backend.replace(0, 8, "wss://");
        }
        else if (backend.rfind("http://", 0) == 0)
        {
            backend.replace(0, 7, "ws://");
        }
        else if (backend.find("://") == std::string::npos)
        {
            if (backend.rfind("192.168.", 0) == 0 || backend.rfind("10.", 0) == 0 ||
                backend.rfind("172.", 0) == 0 || backend.rfind("localhost", 0) == 0)
            {
                backend.insert(0, "ws://");
            }
            else
            {
                backend.insert(0, "wss://");
            }
        }

        while (backend.size() > 6 && backend.back() == '/')
            backend.pop_back();
        const bool secure = backend.rfind("wss://", 0) == 0;
        const bool local = backend.rfind("ws://", 0) == 0;
        if (!secure && !local)
            return false;

        const size_t scheme_length = secure ? 6 : 5;
        const std::string authority = backend.substr(scheme_length);
        if (authority.empty() || authority.find_first_of("/?#@ \r\n") != std::string::npos)
            return false;
        const size_t port_separator = authority.find(':');
        const std::string hostname = authority.substr(0, port_separator);
        if (hostname.empty() || (port_separator != std::string::npos &&
                                 authority.find(':', port_separator + 1) != std::string::npos))
        {
            return false;
        }

        if (port_separator != std::string::npos)
        {
            const std::string port = authority.substr(port_separator + 1);
            if (port.empty() || !std::all_of(port.begin(), port.end(), [](unsigned char digit)
                                             { return digit >= '0' && digit <= '9'; }))
            {
                return false;
            }
            const unsigned long port_number = std::strtoul(port.c_str(), nullptr, 10);
            if (port_number == 0 || port_number > 65535)
                return false;
        }

        if (secure)
        {
            if (!is_public_wss_origin(backend.c_str()))
                return false;
        }
        else
        {
            in_addr address{};
            if (inet_pton(AF_INET, hostname.c_str(), &address) != 1)
                return false;
            const uint32_t ipv4 = ntohl(address.s_addr);
            const bool private_lan_address =
                (ipv4 & 0xFF000000U) == 0x0A000000U ||
                (ipv4 & 0xFFF00000U) == 0xAC100000U ||
                (ipv4 & 0xFFFF0000U) == 0xC0A80000U;
            if (!private_lan_address)
                return false;
        }

        if (backend.size() >= capacity)
            return false;
        std::memcpy(output, backend.c_str(), backend.size() + 1);
        return true;
    }

    bool is_supported_backend_origin(const char *value)
    {
        char normalized[129]{};
        return normalize_backend_origin(value, normalized, sizeof(normalized));
    }

    bool request_is_from_setup_ap(httpd_req_t *request)
    {
        sockaddr_storage peer{};
        socklen_t peer_length = sizeof(peer);
        if (getpeername(httpd_req_to_sockfd(request), reinterpret_cast<sockaddr *>(&peer), &peer_length) != 0)
        {
            ESP_LOGW(TAG, "Cannot identify setup portal client socket: errno=%d", errno);
            return false;
        }
        uint32_t client_address = 0;
        if (peer.ss_family == AF_INET)
        {
            const auto *ipv4 = reinterpret_cast<const sockaddr_in *>(&peer);
            client_address = ntohl(ipv4->sin_addr.s_addr);
        }
        else if (peer.ss_family == AF_INET6)
        {
            const auto *ipv6 = reinterpret_cast<const sockaddr_in6 *>(&peer);
            static constexpr uint8_t IPV4_MAPPED_PREFIX[] = {
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0xFF,
                0xFF,
            };
            if (std::memcmp(ipv6->sin6_addr.s6_addr, IPV4_MAPPED_PREFIX, sizeof(IPV4_MAPPED_PREFIX)) != 0)
            {
                ESP_LOGW(TAG, "Setup portal denied non-IPv4 client (address family %d)", peer.ss_family);
                return false;
            }
            uint32_t mapped_address = 0;
            std::memcpy(&mapped_address, ipv6->sin6_addr.s6_addr + sizeof(IPV4_MAPPED_PREFIX), sizeof(mapped_address));
            client_address = ntohl(mapped_address);
        }
        else
        {
            ESP_LOGW(TAG, "Setup portal denied client with unsupported address family %d", peer.ss_family);
            return false;
        }

        const bool from_setup_ap = (client_address & 0xFFFFFF00U) == 0xC0A80400U;
        if (!from_setup_ap)
        {
            ESP_LOGW(
                TAG, "Setup portal denied non-AP client %u.%u.%u.%u",
                static_cast<unsigned>((client_address >> 24) & 0xFF),
                static_cast<unsigned>((client_address >> 16) & 0xFF),
                static_cast<unsigned>((client_address >> 8) & 0xFF),
                static_cast<unsigned>(client_address & 0xFF));
        }
        return from_setup_ap;
    }

    esp_err_t persist_config(const DeviceConfig &value)
    {
        nvs_handle_t handle;
        esp_err_t result = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle);
        if (result != ESP_OK)
            return result;
        result = nvs_set_str(handle, "ssid", value.wifi_ssid.data());
        if (result == ESP_OK)
            result = nvs_set_str(handle, "password", value.wifi_password.data());
        if (result == ESP_OK)
            result = nvs_set_str(handle, "backend", value.backend_url.data());
        if (result == ESP_OK)
            result = nvs_set_str(handle, "device_id", value.device_id.data());
        if (result == ESP_OK)
            result = nvs_set_str(handle, "token", value.device_token.data());
        if (result == ESP_OK)
            result = nvs_set_str(handle, "country", value.country.data());
        if (result == ESP_OK)
            result = nvs_set_str(handle, "tz", value.timezone.data());
        if (result == ESP_OK)
            result = nvs_set_str(handle, "city", value.city.data());
        if (result == ESP_OK)
            result = nvs_commit(handle);
        nvs_close(handle);

        if (result == ESP_OK && xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
        {
            config = value;
            xSemaphoreGive(config_mutex);
        }

        return result;
    }

    esp_err_t read_config_value(nvs_handle_t handle, const char *key, char *value, size_t capacity)
    {
        size_t length = capacity;
        return nvs_get_str(handle, key, value, &length);
    }

    bool load_config()
    {
        nvs_handle_t handle;
        if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &handle) != ESP_OK)
            return false;

        DeviceConfig temp_config{};
        const bool valid =
            read_config_value(handle, "ssid", temp_config.wifi_ssid.data(), temp_config.wifi_ssid.size()) == ESP_OK &&
            read_config_value(handle, "password", temp_config.wifi_password.data(), temp_config.wifi_password.size()) == ESP_OK &&
            read_config_value(handle, "backend", temp_config.backend_url.data(), temp_config.backend_url.size()) == ESP_OK &&
            read_config_value(handle, "device_id", temp_config.device_id.data(), temp_config.device_id.size()) == ESP_OK &&
            read_config_value(handle, "token", temp_config.device_token.data(), temp_config.device_token.size()) == ESP_OK;
        if (valid)
        {
            read_config_value(handle, "country", temp_config.country.data(), temp_config.country.size());
            read_config_value(handle, "tz", temp_config.timezone.data(), temp_config.timezone.size());
            read_config_value(handle, "city", temp_config.city.data(), temp_config.city.size());

            if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
            {
                config = temp_config;
                xSemaphoreGive(config_mutex);
            }
        }
        nvs_close(handle);
        return valid;
    }

    esp_err_t portal_root_fn(httpd_req_t *request)
    {
        if (!request_is_from_setup_ap(request))
        {
            httpd_resp_send_err(request, HTTPD_403_FORBIDDEN, "Setup dashboard is available only through its WPA2 access point");
            return ESP_FAIL;
        }
        static constexpr char page[] = R"HTML(<!doctype html><meta name=viewport content='width=device-width'><title>Buddy device dashboard</title>
<style>body{font:16px system-ui;max-width:680px;margin:2rem auto;padding:0 1rem;background:#101827;color:#eef}
input,select{box-sizing:border-box;width:100%;padding:.7rem;margin:.35rem 0 1rem}button{padding:.8rem;background:#46d3b1;border:0}
label{display:block}section{border:1px solid #536078;padding:1rem;margin:1rem 0;border-radius:.7rem}</style>
<h1>Buddy device dashboard</h1><section><h2>Device status</h2><pre id=status>Loading...</pre></section>
<section><h2>Wi-Fi, backend and location</h2>
<p>This WPA2-protected local page lets you change networks while traveling. For a deployed backend, enter its public hostname or HTTPS/WSS URL. For a trusted local network, enter ws:// followed by its private IPv4 address and port, such as ws://192.168.10.163:8000. Do not include an API path.</p>
<form id=setup-form><label>Wi-Fi name<input name=wifi_ssid required maxlength=32></label>
<label>Wi-Fi password<input name=wifi_password type=password maxlength=64 autocomplete=new-password></label>
<label>Backend URL<input name=backend_url placeholder='api.example.com or ws://192.168.10.163:8000' maxlength=128 required></label>
<label>Device ID<input name=device_id required maxlength=64></label>
<label>Device token (leave blank to keep the current token)<input name=device_token type=password maxlength=128 autocomplete=new-password></label>
<h3>Location (the device has no GPS)</h3>
<label>Country<select name=country></select></label>
<label>Time zone<select name=timezone></select></label>
<label>City (used for weather)<input name=city maxlength=40 placeholder='Dhaka'></label>
<button>Save and reconnect</button></form><pre id=result></pre></section>
<script>
const C=[['BD','Bangladesh','Asia/Dhaka'],['IN','India','Asia/Kolkata'],['PK','Pakistan','Asia/Karachi'],['LK','Sri Lanka','Asia/Colombo'],['NP','Nepal','Asia/Kathmandu'],['BT','Bhutan','Asia/Thimphu'],['MV','Maldives','Indian/Maldives'],['AF','Afghanistan','Asia/Kabul'],['AE','United Arab Emirates','Asia/Dubai'],['SA','Saudi Arabia','Asia/Riyadh'],['QA','Qatar','Asia/Qatar'],['KW','Kuwait','Asia/Kuwait'],['OM','Oman','Asia/Muscat'],['TR','Turkey','Europe/Istanbul'],['IR','Iran','Asia/Tehran'],['IQ','Iraq','Asia/Baghdad'],['IL','Israel','Asia/Jerusalem'],['EG','Egypt','Africa/Cairo'],['ZA','South Africa','Africa/Johannesburg'],['NG','Nigeria','Africa/Lagos'],['KE','Kenya','Africa/Nairobi'],['ET','Ethiopia','Africa/Addis_Ababa'],['MA','Morocco','Africa/Casablanca'],['GB','United Kingdom','Europe/London'],['IE','Ireland','Europe/Dublin'],['PT','Portugal','Europe/Lisbon'],['ES','Spain','Europe/Madrid'],['FR','France','Europe/Paris'],['DE','Germany','Europe/Berlin'],['IT','Italy','Europe/Rome'],['NL','Netherlands','Europe/Amsterdam'],['BE','Belgium','Europe/Brussels'],['CH','Switzerland','Europe/Zurich'],['AT','Austria','Europe/Vienna'],['SE','Sweden','Europe/Stockholm'],['NO','Norway','Europe/Oslo'],['DK','Denmark','Europe/Copenhagen'],['FI','Finland','Europe/Helsinki'],['PL','Poland','Europe/Warsaw'],['GR','Greece','Europe/Athens'],['UA','Ukraine','Europe/Kyiv'],['RU','Russia','Europe/Moscow'],['US','United States','America/New_York'],['CA','Canada','America/Toronto'],['MX','Mexico','America/Mexico_City'],['BR','Brazil','America/Sao_Paulo'],['AR','Argentina','America/Argentina/Buenos_Aires'],['CL','Chile','America/Santiago'],['CO','Colombia','America/Bogota'],['PE','Peru','America/Lima'],['CN','China','Asia/Shanghai'],['HK','Hong Kong','Asia/Hong_Kong'],['TW','Taiwan','Asia/Taipei'],['JP','Japan','Asia/Tokyo'],['KR','South Korea','Asia/Seoul'],['SG','Singapore','Asia/Singapore'],['MY','Malaysia','Asia/Kuala_Lumpur'],['TH','Thailand','Asia/Bangkok'],['VN','Vietnam','Asia/Ho_Chi_Minh'],['ID','Indonesia','Asia/Jakarta'],['PH','Philippines','Asia/Manila'],['AU','Australia','Australia/Sydney'],['NZ','New Zealand','Pacific/Auckland']];
const Z=['UTC','Asia/Dhaka','Asia/Kolkata','Asia/Karachi','Asia/Colombo','Asia/Kathmandu','Asia/Thimphu','Indian/Maldives','Asia/Kabul','Asia/Dubai','Asia/Riyadh','Asia/Qatar','Asia/Kuwait','Asia/Muscat','Asia/Tehran','Asia/Baghdad','Asia/Jerusalem','Europe/Istanbul','Africa/Cairo','Africa/Johannesburg','Africa/Lagos','Africa/Nairobi','Africa/Addis_Ababa','Africa/Casablanca','Europe/London','Europe/Dublin','Europe/Lisbon','Europe/Madrid','Europe/Paris','Europe/Berlin','Europe/Rome','Europe/Amsterdam','Europe/Brussels','Europe/Zurich','Europe/Vienna','Europe/Stockholm','Europe/Oslo','Europe/Copenhagen','Europe/Helsinki','Europe/Warsaw','Europe/Athens','Europe/Kyiv','Europe/Moscow','America/New_York','America/Chicago','America/Denver','America/Los_Angeles','America/Anchorage','Pacific/Honolulu','America/Toronto','America/Vancouver','America/Mexico_City','America/Bogota','America/Lima','America/Santiago','America/Sao_Paulo','America/Argentina/Buenos_Aires','Asia/Shanghai','Asia/Hong_Kong','Asia/Taipei','Asia/Tokyo','Asia/Seoul','Asia/Singapore','Asia/Kuala_Lumpur','Asia/Bangkok','Asia/Ho_Chi_Minh','Asia/Jakarta','Asia/Manila','Australia/Perth','Australia/Adelaide','Australia/Sydney','Pacific/Auckland'];
const f=document.getElementById('setup-form'),o=document.getElementById('result');
const opt=(v,t)=>{const e=document.createElement('option');e.value=v;e.textContent=t;return e};
f.country.append(opt('','Select country'));C.forEach(c=>f.country.append(opt(c[0],c[1])));
f.timezone.append(opt('','Select time zone'));Z.forEach(z=>f.timezone.append(opt(z,z)));
f.country.onchange=()=>{const c=C.find(x=>x[0]===f.country.value);if(c)f.timezone.value=c[2]};
fetch('/status').then(r=>r.json()).then(s=>{document.getElementById('status').textContent=JSON.stringify(s,null,2);f.wifi_ssid.value=s.wifi_ssid||'';
if(s.backend_url)f.backend_url.value=s.backend_url;f.device_id.value=s.device_id||'';f.country.value=s.country||'';f.timezone.value=s.timezone||'';f.city.value=s.city||''}).catch(e=>{document.getElementById('status').textContent=String(e)});
f.onsubmit=async e=>{e.preventDefault();let d=Object.fromEntries(new FormData(f));
let r=await fetch('/configure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
o.textContent=await r.text();}
</script>)HTML";
        httpd_resp_set_type(request, "text/html; charset=utf-8");
        return httpd_resp_send(request, page, HTTPD_RESP_USE_STRLEN);
    }

    esp_err_t portal_status_fn(httpd_req_t *request)
    {
        if (!request_is_from_setup_ap(request))
        {
            httpd_resp_send_err(request, HTTPD_403_FORBIDDEN, "Setup dashboard is available only through its WPA2 access point");
            return ESP_FAIL;
        }
        cJSON *status = cJSON_CreateObject();
        if (status == nullptr)
        {
            httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR, "Cannot create status response");
            return ESP_ERR_NO_MEM;
        }

        DeviceConfig current_config{};
        if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
        {
            current_config = config;
            xSemaphoreGive(config_mutex);
        }

        cJSON_AddBoolToObject(status, "configured", station_configured);
        cJSON_AddBoolToObject(status, "setup_ap_active", setup_ap_active);
        cJSON_AddStringToObject(status, "wifi_ssid", saved_config_loaded ? current_config.wifi_ssid.data() : "");
        cJSON_AddStringToObject(status, "backend_url", saved_config_loaded ? current_config.backend_url.data() : "");
        cJSON_AddStringToObject(status, "device_id", saved_config_loaded ? current_config.device_id.data() : "");
        cJSON_AddStringToObject(status, "country", saved_config_loaded ? current_config.country.data() : "");
        cJSON_AddStringToObject(status, "timezone", saved_config_loaded ? current_config.timezone.data() : "");
        cJSON_AddStringToObject(status, "city", saved_config_loaded ? current_config.city.data() : "");

        wifi_ap_record_t ap_info{};
        const bool station_associated = saved_config_loaded && esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK;
        const bool station_has_ip = wifi_events != nullptr && (xEventGroupGetBits(wifi_events) & CONNECTED_BIT) != 0;
        cJSON_AddBoolToObject(status, "station_associated", station_associated);
        cJSON_AddBoolToObject(status, "station_connected", station_has_ip);
        esp_netif_ip_info_t station_ip{};
        esp_netif_t *station_netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
        if (station_has_ip && station_netif != nullptr && esp_netif_get_ip_info(station_netif, &station_ip) == ESP_OK)
        {
            char ip_address[IP4ADDR_STRLEN_MAX]{};
            esp_ip4addr_ntoa(&station_ip.ip, ip_address, sizeof(ip_address));
            cJSON_AddStringToObject(status, "station_ip", ip_address);
        }
        else
        {
            cJSON_AddStringToObject(status, "station_ip", "");
        }
        char *body = cJSON_PrintUnformatted(status);
        cJSON_Delete(status);
        if (body == nullptr)
        {
            httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR, "Cannot encode status response");
            return ESP_FAIL;
        }
        httpd_resp_set_type(request, "application/json");
        const esp_err_t result = httpd_resp_sendstr(request, body);
        cJSON_free(body);
        return result;
    }

    esp_err_t portal_configure_fn(httpd_req_t *request)
    {
        if (!request_is_from_setup_ap(request))
        {
            httpd_resp_send_err(request, HTTPD_403_FORBIDDEN, "Setup dashboard is available only through its WPA2 access point");
            return ESP_FAIL;
        }
        if (request->content_len <= 0 || request->content_len > 1024)
        {
            httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Invalid configuration size");
            return ESP_FAIL;
        }
        std::array<char, 1025> body{};
        size_t received = 0;
        while (received < static_cast<size_t>(request->content_len))
        {
            const int count = httpd_req_recv(
                request, body.data() + received,
                std::min(sizeof(body) - received - 1, static_cast<size_t>(request->content_len) - received));
            if (count <= 0)
            {
                httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Incomplete configuration");
                return ESP_FAIL;
            }
            received += static_cast<size_t>(count);
        }
        cJSON *parsed = cJSON_ParseWithLength(body.data(), received);
        if (parsed == nullptr)
        {
            httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Invalid JSON");
            return ESP_FAIL;
        }
        DeviceConfig submitted{};
        const bool valid =
            copy_json_string(parsed, "wifi_ssid", submitted.wifi_ssid.data(), submitted.wifi_ssid.size(), true) &&
            copy_json_string(parsed, "wifi_password", submitted.wifi_password.data(), submitted.wifi_password.size(), false) &&
            copy_json_string(parsed, "backend_url", submitted.backend_url.data(), submitted.backend_url.size(), true) &&
            copy_json_string(parsed, "device_id", submitted.device_id.data(), submitted.device_id.size(), true) &&
            copy_json_string(parsed, "device_token", submitted.device_token.data(), submitted.device_token.size(), false) &&
            copy_json_string(parsed, "country", submitted.country.data(), submitted.country.size(), false) &&
            copy_json_string(parsed, "timezone", submitted.timezone.data(), submitted.timezone.size(), false) &&
            copy_json_string(parsed, "city", submitted.city.data(), submitted.city.size(), false);
        cJSON_Delete(parsed);
        if (!valid)
        {
            httpd_resp_send_err(
                request, HTTPD_400_BAD_REQUEST,
                "Wi-Fi name, backend address, and device ID are required and must fit their field limits");
            return ESP_FAIL;
        }
        if (!normalize_backend_origin(
                submitted.backend_url.data(), submitted.backend_url.data(), submitted.backend_url.size()))
        {
            httpd_resp_send_err(
                request, HTTPD_400_BAD_REQUEST,
                "Use a public DNS hostname or https:// / wss:// URL for deployed service, or ws:// with a private LAN IPv4 address and optional port; do not include a path");
            return ESP_FAIL;
        }

        DeviceConfig current_config{};
        if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
        {
            current_config = config;
            xSemaphoreGive(config_mutex);
        }

        if (submitted.wifi_password[0] == '\0' && saved_config_loaded &&
            std::strcmp(submitted.wifi_ssid.data(), current_config.wifi_ssid.data()) == 0)
        {
            std::strncpy(submitted.wifi_password.data(), current_config.wifi_password.data(),
                         submitted.wifi_password.size() - 1);
        }
        if (submitted.device_token[0] == '\0' && saved_config_loaded)
        {
            std::strncpy(submitted.device_token.data(), current_config.device_token.data(),
                         submitted.device_token.size() - 1);
        }
        bool location_valid = submitted.country[0] == '\0' ||
                              (submitted.country[0] >= 'A' && submitted.country[0] <= 'Z' && submitted.country[1] >= 'A' &&
                               submitted.country[1] <= 'Z');
        for (const char *character = submitted.timezone.data(); location_valid && *character != '\0'; ++character)
        {
            const char value = *character;
            location_valid = (value >= 'a' && value <= 'z') || (value >= 'A' && value <= 'Z') ||
                             (value >= '0' && value <= '9') || value == '_' || value == '/' || value == '-' || value == '+';
        }
        for (const char *character = submitted.city.data(); location_valid && *character != '\0'; ++character)
        {
            location_valid = static_cast<unsigned char>(*character) >= 0x20 && *character != '"' && *character != '\\';
        }
        if (!location_valid)
        {
            httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Country, time zone, or city is invalid");
            return ESP_FAIL;
        }
        bool device_id_valid = submitted.device_id[0] != '\0';
        for (const char *character = submitted.device_id.data(); device_id_valid && *character != '\0'; ++character)
        {
            const char value = *character;
            device_id_valid = (value >= 'a' && value <= 'z') || (value >= 'A' && value <= 'Z') ||
                              (value >= '0' && value <= '9') || value == '.' || value == '_' || value == '-';
        }
        if (!device_id_valid)
        {
            httpd_resp_send_err(
                request, HTTPD_400_BAD_REQUEST,
                "Device ID may contain only letters, numbers, period, underscore, and hyphen");
            return ESP_FAIL;
        }
        if (submitted.device_token[0] == '\0')
        {
            httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "A device token is required for first-time setup");
            return ESP_ERR_INVALID_ARG;
        }
        const esp_err_t result = persist_config(submitted);
        if (result != ESP_OK)
        {
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

    esp_err_t start_provisioning_portal()
    {
        if (setup_ap_active)
            return ESP_OK;

        uint8_t mac[6];
        ESP_RETURN_ON_ERROR(esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP), TAG, "Cannot read Wi-Fi MAC");
        std::snprintf(setup_ssid, sizeof(setup_ssid), "Buddy-Setup-%02X%02X", mac[4], mac[5]);
        wifi_config_t ap_config{};
        std::snprintf(reinterpret_cast<char *>(ap_config.ap.ssid), sizeof(ap_config.ap.ssid), "%s", setup_ssid);
        std::snprintf(
            reinterpret_cast<char *>(ap_config.ap.password), sizeof(ap_config.ap.password), "%s",
            SETUP_AP_PASSWORD);
        ap_config.ap.ssid_len = std::strlen(setup_ssid);
        ap_config.ap.channel = 1;
        ap_config.ap.max_connection = 4;
        ap_config.ap.ssid_hidden = 0;
        ap_config.ap.beacon_interval = 100;
        ap_config.ap.authmode = WIFI_AUTH_WPA2_PSK;
        ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_AP, &ap_config), TAG, "Cannot configure setup access point");

        httpd_config_t server_config = HTTPD_DEFAULT_CONFIG();
        server_config.max_uri_handlers = 12; // Expanded to handle all OS detection probes
        httpd_handle_t server = nullptr;
        ESP_RETURN_ON_ERROR(httpd_start(&server, &server_config), TAG, "Cannot start setup portal");

        httpd_uri_t root = {.uri = "/", .method = HTTP_GET, .handler = portal_root_fn, .user_ctx = nullptr};
        httpd_uri_t status = {.uri = "/status", .method = HTTP_GET, .handler = portal_status_fn, .user_ctx = nullptr};
        httpd_uri_t configure = {.uri = "/configure", .method = HTTP_POST, .handler = portal_configure_fn, .user_ctx = nullptr};

        // Captive Portal detection probes for iOS, macOS, Android, Windows, and Linux
        httpd_uri_t ios_probe = {.uri = "/hotspot-detect.html", .method = HTTP_GET, .handler = captive_redirect_handler, .user_ctx = nullptr};
        httpd_uri_t android_probe1 = {.uri = "/generate_204", .method = HTTP_GET, .handler = captive_redirect_handler, .user_ctx = nullptr};
        httpd_uri_t android_probe2 = {.uri = "/gen_204", .method = HTTP_GET, .handler = captive_redirect_handler, .user_ctx = nullptr};
        httpd_uri_t win_probe = {.uri = "/connecttest.txt", .method = HTTP_GET, .handler = captive_redirect_handler, .user_ctx = nullptr};
        httpd_uri_t ncsi_probe = {.uri = "/ncsi.txt", .method = HTTP_GET, .handler = captive_redirect_handler, .user_ctx = nullptr};

        ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &root), TAG, "Cannot register portal page");
        ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &status), TAG, "Cannot register portal status");
        ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &configure), TAG, "Cannot register portal configuration");

        // Register captive redirection URIs
        httpd_register_uri_handler(server, &ios_probe);
        httpd_register_uri_handler(server, &android_probe1);
        httpd_register_uri_handler(server, &android_probe2);
        httpd_register_uri_handler(server, &win_probe);
        httpd_register_uri_handler(server, &ncsi_probe);

        setup_ap_active = true;

        // Launch captive DNS server on port 53
        start_dns_server();

        return ESP_OK;
    }

    void wifi_event_handler(void *, esp_event_base_t event_base, int32_t event_id, void *event_data)
    {
        if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED)
        {
            if (wifi_events != nullptr)
                xEventGroupClearBits(wifi_events, CONNECTED_BIT);
            const auto *disconnect = static_cast<const wifi_event_sta_disconnected_t *>(event_data);
            if (disconnect != nullptr)
            {
                ESP_LOGW(
                    TAG, "Wi-Fi disconnected (reason=%u, rssi=%d dBm)",
                    static_cast<unsigned>(disconnect->reason), static_cast<int>(disconnect->rssi));
            }
            else
            {
                ESP_LOGW(TAG, "Wi-Fi disconnected (reason unavailable)");
            }

            if (station_configured)
            {
                if (wifi_retry_count < MAX_WIFI_RETRIES)
                {
                    wifi_retry_count++;
                    ESP_LOGI(TAG, "Retrying Wi-Fi connection (%d/%d)...", wifi_retry_count, MAX_WIFI_RETRIES);
                    esp_wifi_connect();
                }
                else
                {
                    ESP_LOGE(TAG, "Failed to connect to target Wi-Fi after %d attempts. Queueing Setup Portal...", MAX_WIFI_RETRIES);
                    if (wifi_events != nullptr)
                    {
                        xEventGroupSetBits(wifi_events, START_AP_BIT);
                    }
                }
            }
        }
        else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP)
        {
            wifi_retry_count = 0;
            if (wifi_events != nullptr)
                xEventGroupSetBits(wifi_events, CONNECTED_BIT);
            const auto *event = static_cast<const ip_event_got_ip_t *>(event_data);
            if (event != nullptr)
            {
                ESP_LOGI(
                    TAG, "Wi-Fi connected; station IP: " IPSTR ", gateway: " IPSTR,
                    IP2STR(&event->ip_info.ip), IP2STR(&event->ip_info.gw));
                AudioI2S::queue_sound_effect("online");
            }
            else
            {
                ESP_LOGI(TAG, "Wi-Fi connected; station IP assigned");
            }
        }
    }

    void send_hello()
    {
        const esp_app_desc_t *app_description = esp_app_get_description();
        const char *firmware_version = app_description == nullptr ? "unknown" : app_description->version;
        time_t now = time(nullptr);
        struct tm utc_time{};
        char timestamp[32] = "1970-01-01T00:00:00Z";
        if (gmtime_r(&now, &utc_time) != nullptr)
        {
            std::strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%SZ", &utc_time);
        }

        DeviceConfig current_config{};
        if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
        {
            current_config = config;
            xSemaphoreGive(config_mutex);
        }

        char message_id[32];
        std::snprintf(message_id, sizeof(message_id), "boot-%llu", static_cast<unsigned long long>(boot_identifier));
        cJSON *root = cJSON_CreateObject();
        cJSON *payload = cJSON_CreateObject();
        cJSON *location = cJSON_CreateObject();
        if (root == nullptr || payload == nullptr || location == nullptr)
        {
            cJSON_Delete(root);
            cJSON_Delete(payload);
            cJSON_Delete(location);
            return;
        }
        cJSON_AddNumberToObject(root, "protocol_version", 1);
        cJSON_AddStringToObject(root, "message_id", message_id);
        cJSON_AddStringToObject(root, "device_id", current_config.device_id.data());
        cJSON_AddStringToObject(root, "timestamp", timestamp);
        cJSON_AddStringToObject(root, "type", "device.hello");
        cJSON_AddStringToObject(payload, "firmware_version", firmware_version);
        cJSON_AddBoolToObject(payload, "simulated", false);
        cJSON_AddStringToObject(location, "country", current_config.country.data());
        cJSON_AddStringToObject(location, "timezone", current_config.timezone.data());
        cJSON_AddStringToObject(location, "city", current_config.city.data());
        cJSON_AddItemToObject(payload, "location", location);
        cJSON *capabilities = cJSON_CreateArray();
        if (capabilities != nullptr)
        {
            cJSON_AddItemToArray(capabilities, cJSON_CreateString("local_schedule"));
            cJSON_AddItemToArray(capabilities, cJSON_CreateString("local_games"));
            cJSON_AddItemToObject(payload, "capabilities", capabilities);
        }
        cJSON_AddItemToObject(root, "payload", payload);
        char *encoded = cJSON_PrintUnformatted(root);
        cJSON_Delete(root);
        if (encoded == nullptr)
            return;
        const int sent = send_websocket_text(encoded, static_cast<int>(std::strlen(encoded)), pdMS_TO_TICKS(3000));
        if (sent < 0)
            ESP_LOGE(TAG, "Failed sending device hello");
        cJSON_free(encoded);
    }

    void queue_replay_range(uint32_t expected_sequence)
    {
        if (replay_requests == nullptr)
            return;
        bool available = false;
        for (size_t offset = 0; offset < REPLAY_WINDOW_SIZE; ++offset)
        {
            const uint32_t sequence = expected_sequence + static_cast<uint32_t>(offset);
            portENTER_CRITICAL(&replay_lock);
            const auto &cached = replay_window[sequence % REPLAY_WINDOW_SIZE];
            available = cached.valid && cached.sequence == sequence;
            portEXIT_CRITICAL(&replay_lock);
            if (available)
                break;
        }
        if (available && xQueueOverwrite(replay_requests, &expected_sequence) != pdPASS)
        {
            ESP_LOGW(TAG, "Could not queue the bounded audio replay range");
        }
    }

    void replay_audio_range(uint32_t requested)
    {
        std::array<uint8_t, MAX_PACKET_SIZE> packet{};
        for (size_t offset = 0; offset < REPLAY_WINDOW_SIZE; ++offset)
        {
            const uint32_t sequence = requested + static_cast<uint32_t>(offset);
            size_t length = 0;
            portENTER_CRITICAL(&replay_lock);
            const auto &cached = replay_window[sequence % REPLAY_WINDOW_SIZE];
            if (cached.valid && cached.sequence == sequence)
            {
                length = cached.length;
                std::memcpy(packet.data(), cached.bytes.data(), length);
            }
            portEXIT_CRITICAL(&replay_lock);
            if (length == 0)
                continue;
            if (websocket == nullptr || (xEventGroupGetBits(websocket_events) & WEBSOCKET_READY_BIT) == 0)
                break;
            const int sent = send_websocket_binary(
                packet.data(), static_cast<int>(length), AUDIO_SEND_TIMEOUT);
            if (sent != static_cast<int>(length))
            {
                ESP_LOGW(TAG, "Could not replay audio frame %lu", static_cast<unsigned long>(sequence));
                break;
            }
        }
    }

    void websocket_event_handler(void *, esp_event_base_t, int32_t event_id, void *event_data)
    {
        auto *event = static_cast<esp_websocket_event_data_t *>(event_data);
        if (event_id == WEBSOCKET_EVENT_CONNECTED)
        {
            ESP_LOGI(TAG, "Backend WebSocket connected");
            xEventGroupSetBits(websocket_events, WEBSOCKET_READY_BIT);
            send_hello();
            send_game_history();
            return;
        }
        if (event_id == WEBSOCKET_EVENT_ERROR && event != nullptr)
        {
            const auto &error = event->error_handle;
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
        if (event_id == WEBSOCKET_EVENT_DISCONNECTED || event_id == WEBSOCKET_EVENT_CLOSED)
        {
            xEventGroupClearBits(websocket_events, WEBSOCKET_READY_BIT);
            ESP_LOGW(
                TAG, "Backend WebSocket %s (close_code=%d)",
                event_id == WEBSOCKET_EVENT_CLOSED ? "closed cleanly" : "disconnected",
                event == nullptr ? 0 : event->close_status_code);
            return;
        }
        if (event_id != WEBSOCKET_EVENT_DATA || event == nullptr)
            return;
        if (event->op_code == 0x9 || event->op_code == 0xA)
        {
            control_buffer_used = 0;
            ESP_LOGD(
                TAG, "Received WebSocket %s control frame",
                event->op_code == 0x9 ? "ping" : "pong");
            return;
        }
        if (event->payload_len <= 0 || event->payload_len > static_cast<int>(CONTROL_BUFFER_SIZE - 1))
        {
            ESP_LOGW(
                TAG, "Ignoring server frame with invalid payload length "
                     "(payload_len=%d data_len=%d offset=%d opcode=%u)",
                event->payload_len, event->data_len, event->payload_offset, event->op_code);
            control_buffer_used = 0;
            return;
        }
        if (event->payload_offset == 0)
            control_buffer_used = 0;
        if (event->payload_offset != static_cast<int>(control_buffer_used) ||
            control_buffer_used + static_cast<size_t>(event->data_len) > sizeof(control_buffer))
        {
            ESP_LOGW(TAG, "Ignoring fragmented WebSocket message with invalid offsets");
            control_buffer_used = 0;
            return;
        }
        std::memcpy(control_buffer + control_buffer_used, event->data_ptr, event->data_len);
        control_buffer_used += static_cast<size_t>(event->data_len);
        if (control_buffer_used != static_cast<size_t>(event->payload_len))
            return;
        if (event->op_code == 0x2)
        {
            if (control_buffer_used % sizeof(int16_t) != 0)
            {
                ESP_LOGW(TAG, "Ignoring unaligned PCM playback frame");
                control_buffer_used = 0;
                return;
            }
            const auto *samples = reinterpret_cast<const int16_t *>(control_buffer);
            const size_t count = control_buffer_used / sizeof(int16_t);
            for (size_t offset = 0; offset < count; offset += HardwareConfig::Audio::CAPTURE_SAMPLES)
            {
                const size_t segment = std::min(
                    static_cast<size_t>(HardwareConfig::Audio::CAPTURE_SAMPLES), count - offset);
                if (AudioI2S::queue_playback(samples + offset, segment) != ESP_OK)
                {
                    ESP_LOGW(TAG, "Playback queue full; dropping incoming audio");
                    break;
                }
            }
        }
        else if (event->op_code == 0x1)
        {
            control_buffer[control_buffer_used] = '\0';
            cJSON *message = cJSON_Parse(control_buffer);
            const cJSON *type = message == nullptr ? nullptr : cJSON_GetObjectItemCaseSensitive(message, "type");
            const cJSON *payload = message == nullptr ? nullptr : cJSON_GetObjectItemCaseSensitive(message, "payload");
            if (cJSON_IsString(type))
            {
                if (std::strcmp(type->valuestring, "audio.ack") == 0)
                {
                    const cJSON *sequence_item = cJSON_GetObjectItemCaseSensitive(payload, "sequence");
                    if (cJSON_IsNumber(sequence_item) && sequence_item->valuedouble >= 0 &&
                        sequence_item->valuedouble <= UINT32_MAX)
                    {
                        const uint32_t acknowledged = static_cast<uint32_t>(sequence_item->valuedouble);
                        portENTER_CRITICAL(&replay_lock);
                        auto &cached = replay_window[acknowledged % REPLAY_WINDOW_SIZE];
                        if (cached.valid && cached.sequence == acknowledged)
                            cached.valid = false;
                        portEXIT_CRITICAL(&replay_lock);
                    }
                }
                else if (std::strcmp(type->valuestring, "audio.nack") == 0)
                {
                    const cJSON *expected = cJSON_GetObjectItemCaseSensitive(payload, "expected_sequence");
                    if (cJSON_IsNumber(expected) && expected->valuedouble >= 0 &&
                        expected->valuedouble <= UINT32_MAX)
                    {
                        const uint32_t missing = static_cast<uint32_t>(expected->valuedouble);
                        queue_replay_range(missing);
                    }
                }
                else if (std::strcmp(type->valuestring, "ping") == 0)
                {
                    const cJSON *id = cJSON_GetObjectItemCaseSensitive(message, "message_id");
                    char pong[256];
                    time_t now = time(nullptr);
                    struct tm utc_time{};
                    char timestamp[32] = "1970-01-01T00:00:00Z";
                    if (gmtime_r(&now, &utc_time) != nullptr)
                    {
                        std::strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%SZ", &utc_time);
                    }
                    char device_id[65]{};
                    if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
                    {
                        std::strncpy(device_id, config.device_id.data(), sizeof(device_id) - 1);
                        xSemaphoreGive(config_mutex);
                    }
                    if (cJSON_IsString(id))
                    {
                        const int length = std::snprintf(
                            pong, sizeof(pong),
                            "{\"protocol_version\":1,\"message_id\":\"%s\",\"device_id\":\"%s\","
                            "\"timestamp\":\"%s\",\"type\":\"pong\",\"payload\":{}}",
                            id->valuestring, device_id, timestamp);
                        if (length > 0 && length < static_cast<int>(sizeof(pong)))
                        {
                            send_websocket_text(pong, length, pdMS_TO_TICKS(100));
                        }
                    }
                }
                else if (std::strcmp(type->valuestring, "device.status") == 0)
                {
                    xEventGroupSetBits(websocket_events, WEBSOCKET_READY_BIT);
                    if (esp_ota_check_rollback_is_possible())
                    {
                        const esp_err_t rollback_result = esp_ota_mark_app_valid_cancel_rollback();
                        if (rollback_result != ESP_OK)
                        {
                            ESP_LOGE(TAG, "Cannot mark OTA firmware healthy: %s", esp_err_to_name(rollback_result));
                        }
                    }
                    const uint32_t current = next_audio_sequence.load();
                    queue_replay_range(current - static_cast<uint32_t>(REPLAY_WINDOW_SIZE));
                }
                else if (std::strcmp(type->valuestring, "ota.available") == 0)
                {
                    if (!queue_ota_release(payload))
                    {
                        ESP_LOGE(TAG, "OTA release manifest is invalid or update queue is full");
                        char device_id[65]{};
                        if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
                        {
                            std::strncpy(device_id, config.device_id.data(), sizeof(device_id) - 1);
                            xSemaphoreGive(config_mutex);
                        }
                        char error[320];
                        const int length = std::snprintf(
                            error, sizeof(error),
                            "{\"protocol_version\":1,\"message_id\":\"ota-invalid\","
                            "\"device_id\":\"%s\",\"timestamp\":\"1970-01-01T00:00:00Z\","
                            "\"type\":\"device.error\",\"payload\":{\"code\":\"invalid_ota_manifest\"}}",
                            device_id);
                        if (length > 0 && length < static_cast<int>(sizeof(error)))
                        {
                            send_websocket_text(error, length, pdMS_TO_TICKS(100));
                        }
                    }
                }
                else if (std::strcmp(type->valuestring, "display.state") == 0 ||
                         std::strcmp(type->valuestring, "game.state") == 0)
                {
                    const cJSON *display_state = cJSON_GetObjectItemCaseSensitive(payload, "state");
                    const cJSON *page_item = cJSON_GetObjectItemCaseSensitive(payload, "page");
                    if (cJSON_IsString(page_item) && page_item->valuestring != nullptr)
                    {
                        char page[16]{};
                        char title[40]{};
                        char detail[48]{};
                        const bool valid_content =
                            copy_json_string(payload, "page", page, sizeof(page), true) &&
                            copy_json_string(payload, "title", title, sizeof(title), true) &&
                            copy_json_string(payload, "detail", detail, sizeof(detail), true);
                        if (!valid_content)
                        {
                            ESP_LOGW(TAG, "Ignoring malformed display page payload");
                        }
                        else
                        {
                            std::array<std::array<char, 97>, 8> item_text{};
                            std::array<const char *, 8> item_pointers{};
                            size_t item_count = 0;
                            const cJSON *items = cJSON_GetObjectItemCaseSensitive(payload, "items");
                            if (cJSON_IsArray(items))
                            {
                                const cJSON *item = nullptr;
                                cJSON_ArrayForEach(item, items)
                                {
                                    if (item_count >= item_text.size())
                                        break;
                                    if (!cJSON_IsString(item) || item->valuestring == nullptr)
                                        continue;
                                    std::strncpy(
                                        item_text[item_count].data(),
                                        item->valuestring,
                                        item_text[item_count].size() - 1);
                                    item_pointers[item_count] = item_text[item_count].data();
                                    ++item_count;
                                }
                            }
                            const cJSON *counts = cJSON_GetObjectItemCaseSensitive(payload, "counts");
                            const auto read_count = [counts](const char *key) -> uint16_t
                            {
                                const cJSON *value = cJSON_GetObjectItemCaseSensitive(counts, key);
                                if (!cJSON_IsNumber(value) || value->valuedouble < 0 ||
                                    value->valuedouble > UINT16_MAX)
                                    return 0;
                                return static_cast<uint16_t>(value->valuedouble);
                            };
                            const esp_err_t result = DeviceDisplay::set_dashboard(
                                page, title, detail, item_pointers.data(), item_count,
                                read_count("todos"), read_count("reminders"), read_count("notes"),
                                read_count("notifications"), read_count("alarms"),
                                cJSON_IsString(cJSON_GetObjectItemCaseSensitive(payload, "emoji"))
                                    ? cJSON_GetObjectItemCaseSensitive(payload, "emoji")->valuestring
                                    : nullptr);
                            if (result != ESP_OK)
                            {
                                ESP_LOGE(TAG, "Cannot render display page '%s': %s", page, esp_err_to_name(result));
                            }
                        }
                    }
                    else if (cJSON_IsString(display_state) && display_state->valuestring != nullptr)
                    {
                        const esp_err_t display_result = DeviceDisplay::set_state(display_state->valuestring);
                        if (display_result != ESP_OK)
                        {
                            ESP_LOGE(TAG, "Cannot render display state '%s': %s",
                                     display_state->valuestring, esp_err_to_name(display_result));
                        }
                    }
                }
                else if (std::strcmp(type->valuestring, "audio.wake_word") == 0)
                {
                    const esp_err_t result = AudioI2S::queue_sound_effect("wake");
                    if (result != ESP_OK)
                        ESP_LOGW(TAG, "Cannot play wake sound: %s", esp_err_to_name(result));
                    const esp_err_t display_result = DeviceDisplay::set_state("listening");
                    if (display_result != ESP_OK)
                    {
                        ESP_LOGE(TAG, "Cannot show listening face: %s", esp_err_to_name(display_result));
                    }
                }
                else if (std::strcmp(type->valuestring, "sound.effect") == 0)
                {
                    const cJSON *effect = cJSON_GetObjectItemCaseSensitive(payload, "effect");
                    if (cJSON_IsString(effect) && effect->valuestring != nullptr)
                    {
                        const esp_err_t result = AudioI2S::queue_sound_effect(effect->valuestring);
                        if (result != ESP_OK)
                        {
                            ESP_LOGW(TAG, "Cannot play requested sound effect: %s", esp_err_to_name(result));
                        }
                    }
                }
                else if (std::strcmp(type->valuestring, "schedule.sync") == 0)
                {
                    DeviceSchedule::sync(cJSON_GetObjectItemCaseSensitive(payload, "items"));
                }
                else if (std::strcmp(type->valuestring, "device.setup") == 0)
                {
                    handle_device_setup(payload);
                }
                else if (std::strcmp(type->valuestring, "game.command") == 0)
                {
                    handle_game_command(payload);
                }
                else if (std::strcmp(type->valuestring, "animation.state") == 0)
                {
                    const cJSON *animation = cJSON_GetObjectItemCaseSensitive(payload, "state");
                    if (cJSON_IsString(animation) && animation->valuestring != nullptr)
                    {
                        const esp_err_t result = DeviceDisplay::set_state(animation->valuestring);
                        if (result != ESP_OK)
                        {
                            ESP_LOGW(TAG, "Cannot apply animation state: %s", esp_err_to_name(result));
                        }
                    }
                }
                else if (std::strcmp(type->valuestring, "calendar.notification") == 0)
                {
                    char title[40]{};
                    const bool has_title = copy_json_string(payload, "title", title, sizeof(title), false);
                    const char *item = has_title ? title : "Upcoming event";
                    const char *items[] = {item};
                    const esp_err_t display_result = DeviceDisplay::set_dashboard(
                        "notifications", "REMINDER", item, items, 1, 0, 0, 0, 1, 0, "🔔");
                    if (display_result != ESP_OK)
                    {
                        ESP_LOGE(TAG, "Cannot display calendar notification: %s", esp_err_to_name(display_result));
                    }
                    const esp_err_t sound_result = AudioI2S::queue_sound_effect("notification");
                    if (sound_result != ESP_OK)
                    {
                        ESP_LOGW(TAG, "Cannot play notification sound: %s", esp_err_to_name(sound_result));
                    }
                }
                else if (std::strcmp(type->valuestring, "audio.transcription") == 0)
                {
                    const esp_err_t result = DeviceDisplay::set_state("thinking");
                    if (result != ESP_OK)
                        ESP_LOGE(TAG, "Cannot show thinking face: %s", esp_err_to_name(result));
                }
                else if (std::strcmp(type->valuestring, "tts.start") == 0)
                {
                    const cJSON *samples = cJSON_GetObjectItemCaseSensitive(payload, "samples");
                    const cJSON *sample_rate = cJSON_GetObjectItemCaseSensitive(payload, "sample_rate");
                    if (cJSON_IsNumber(samples) && samples->valuedouble > 0 &&
                        cJSON_IsNumber(sample_rate) && sample_rate->valuedouble > 0)
                    {
                        const double duration_ms = samples->valuedouble * 1000.0 / sample_rate->valuedouble;
                        if (duration_ms <= 30'000)
                        {
                            const esp_err_t result =
                                DeviceDisplay::set_speaking_for(
                                    static_cast<uint32_t>(std::max(1.0, duration_ms)));
                            if (result != ESP_OK)
                            {
                                ESP_LOGE(TAG, "Cannot animate speaking face: %s", esp_err_to_name(result));
                            }
                        }
                    }
                }
                const cJSON *values = payload;
                const cJSON *nested_config = cJSON_GetObjectItemCaseSensitive(payload, "config");
                if (cJSON_IsObject(nested_config))
                    values = nested_config;
                const cJSON *brightness = cJSON_GetObjectItemCaseSensitive(values, "brightness");
                const cJSON *microphone = cJSON_GetObjectItemCaseSensitive(values, "microphone_enabled");
                if (cJSON_IsNumber(brightness) && brightness->valueint >= 0 && brightness->valueint <= 100)
                {
                    const int next = brightness->valueint;
                    if (apply_brightness(next) == ESP_OK)
                    {
                        const esp_err_t result = persist_runtime_setting("brightness", static_cast<uint8_t>(next));
                        if (result != ESP_OK)
                            ESP_LOGE(TAG, "Cannot persist brightness: %s", esp_err_to_name(result));
                    }
                }
                if (cJSON_IsBool(microphone))
                {
                    const bool enabled = cJSON_IsTrue(microphone);
                    microphone_stream_enabled.store(enabled);
                    const esp_err_t capture_result = AudioI2S::set_capture_enabled(enabled);
                    if (capture_result != ESP_OK)
                    {
                        ESP_LOGE(TAG, "Cannot apply microphone capture setting: %s", esp_err_to_name(capture_result));
                    }
                    const esp_err_t result = persist_runtime_setting("mic_enabled", enabled ? 1 : 0);
                    if (result != ESP_OK)
                        ESP_LOGE(TAG, "Cannot persist microphone setting: %s", esp_err_to_name(result));
                }
                ESP_LOGD(TAG, "Received server event: %s", type->valuestring);
            }
            else
            {
                ESP_LOGW(TAG, "Ignoring malformed server control message");
            }
            cJSON_Delete(message);
        }
        control_buffer_used = 0;
    }

    void put_be16(uint8_t *output, uint16_t value)
    {
        output[0] = static_cast<uint8_t>(value >> 8);
        output[1] = static_cast<uint8_t>(value);
    }

    void put_be32(uint8_t *output, uint32_t value)
    {
        output[0] = static_cast<uint8_t>(value >> 24);
        output[1] = static_cast<uint8_t>(value >> 16);
        output[2] = static_cast<uint8_t>(value >> 8);
        output[3] = static_cast<uint8_t>(value);
    }

    void put_be64(uint8_t *output, uint64_t value)
    {
        for (int i = 7; i >= 0; --i)
        {
            output[i] = static_cast<uint8_t>(value);
            value >>= 8;
        }
    }

    void audio_uplink_task(void *)
    {
        auto queue = AudioI2S::capture_queue();
        AudioI2S::PcmBlock block{};
        AudioI2S::PcmBlock next_block{};
        std::array<int16_t, MAX_AUDIO_FRAME_SAMPLES> samples{};
        std::array<uint8_t, MAX_PACKET_SIZE> packet{};
        uint32_t requested_replay = 0;
        while (true)
        {
            if (websocket != nullptr && (xEventGroupGetBits(websocket_events) & WEBSOCKET_READY_BIT) != 0 &&
                xQueueReceive(replay_requests, &requested_replay, 0) == pdTRUE)
            {
                replay_audio_range(requested_replay);
            }
            if (xQueueReceive(queue, &block, pdMS_TO_TICKS(250)) != pdTRUE)
                continue;
            if (!microphone_stream_enabled.load() || websocket == nullptr ||
                (xEventGroupGetBits(websocket_events) & WEBSOCKET_READY_BIT) == 0)
                continue;
            if (xQueueReceive(replay_requests, &requested_replay, 0) == pdTRUE)
            {
                replay_audio_range(requested_replay);
                continue;
            }
            if (block.sample_count == 0 || block.sample_count > HardwareConfig::Audio::CAPTURE_SAMPLES)
                continue;
            size_t sample_count = block.sample_count;
            std::memcpy(samples.data(), block.samples, sample_count * sizeof(int16_t));
            for (int merged = 1; merged < 4; ++merged)
            {
                if (xQueueReceive(queue, &next_block, merged == 1 ? pdMS_TO_TICKS(40) : 0) != pdTRUE)
                    break;
                if (next_block.sample_count == 0 ||
                    next_block.sample_count > HardwareConfig::Audio::CAPTURE_SAMPLES ||
                    sample_count + next_block.sample_count > samples.size())
                    break;
                std::memcpy(
                    samples.data() + sample_count,
                    next_block.samples,
                    next_block.sample_count * sizeof(int16_t));
                sample_count += next_block.sample_count;
            }
            const size_t pcm_bytes = sample_count * sizeof(int16_t);
            if (AUDIO_HEADER_SIZE + pcm_bytes > packet.size())
                continue;
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
            auto &cached = replay_window[sequence % REPLAY_WINDOW_SIZE];
            cached.sequence = sequence;
            cached.length = packet_length;
            cached.valid = true;
            std::memcpy(cached.bytes.data(), packet.data(), packet_length);
            portEXIT_CRITICAL(&replay_lock);
            const int64_t send_started_at = esp_timer_get_time();
            const int sent = send_websocket_binary(
                packet.data(), static_cast<int>(packet_length), AUDIO_SEND_TIMEOUT);
            if (sent != static_cast<int>(packet_length))
            {
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
            if (++frames_since_stack_report % 256 == 0)
            {
                ESP_LOGI(TAG, "Audio uplink stack high-water mark: %u bytes",
                         static_cast<unsigned>(uxTaskGetStackHighWaterMark(nullptr)));
            }
        }
    }

    void network_task(void *)
    {
        wifi_events = xEventGroupCreate();
        if (wifi_events == nullptr)
        {
            ESP_LOGE(TAG, "Cannot allocate Wi-Fi state");
            vTaskDelete(nullptr);
            return;
        }
        esp_netif_create_default_wifi_ap();
        if (station_configured)
            esp_netif_create_default_wifi_sta();
        wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
        ESP_ERROR_CHECK(esp_wifi_init(&init_config));
        ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, nullptr));
        if (station_configured)
        {
            ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, nullptr));
        }
        ESP_ERROR_CHECK(esp_wifi_set_mode(station_configured ? WIFI_MODE_STA : WIFI_MODE_AP));
        if (!station_configured)
        {
            ESP_ERROR_CHECK(start_provisioning_portal());
        }
        if (station_configured)
        {
            websocket_events = xEventGroupCreate();
            replay_requests = xQueueCreate(1, sizeof(uint32_t));
            websocket_send_mutex = xSemaphoreCreateMutex();
            ota_requests = xQueueCreate(1, sizeof(OtaRequest));
            if (websocket_events == nullptr || replay_requests == nullptr ||
                websocket_send_mutex == nullptr || ota_requests == nullptr)
            {
                ESP_LOGE(TAG, "Cannot allocate WebSocket, replay, or OTA state");
                vTaskDelete(nullptr);
                return;
            }
            if (xTaskCreate(audio_uplink_task, "audio_uplink", AUDIO_UPLINK_STACK_BYTES, nullptr,
                            configMAX_PRIORITIES - 4, nullptr) != pdPASS ||
                xTaskCreate(ota_task, "ota_update", 10240, nullptr, configMAX_PRIORITIES - 6, nullptr) != pdPASS)
            {
                ESP_LOGE(TAG, "Cannot create audio or OTA task");
                vTaskDelete(nullptr);
                return;
            }

            DeviceConfig current_config{};
            if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
            {
                current_config = config;
                xSemaphoreGive(config_mutex);
            }
            wifi_config_t station{};
            std::memcpy(station.sta.ssid, current_config.wifi_ssid.data(), std::strlen(current_config.wifi_ssid.data()));
            std::memcpy(station.sta.password, current_config.wifi_password.data(), std::strlen(current_config.wifi_password.data()));
            ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &station));
        }
        ESP_ERROR_CHECK(esp_wifi_start());
        if (!station_configured)
        {
            ESP_LOGI(TAG, "Initial setup access point started; SSID: %s; dashboard: http://192.168.4.1/",
                     setup_ssid);
            ESP_LOGW(TAG, "Setup dashboard running; configure Wi-Fi and a supported backend to continue");
            char ssid_line[40];
            std::snprintf(ssid_line, sizeof(ssid_line), "Wi-Fi: %s", setup_ssid);
            char password_line[40];
            std::snprintf(password_line, sizeof(password_line), "Pass: %s", SETUP_AP_PASSWORD);
            const char *setup_items[] = {ssid_line, password_line, "Open 192.168.4.1"};
            DeviceDisplay::set_dashboard("notifications", "SETUP MODE", "Join Wi-Fi, open page",
                                         setup_items, 3, 0, 0, 0, 0, 0, nullptr);
            if (portal_session)
            {
                vTaskDelay(pdMS_TO_TICKS(PORTAL_SESSION_TIMEOUT_MS));
                ESP_LOGW(TAG, "Setup portal timed out; restarting with the saved configuration");
                esp_restart();
            }
            vTaskDelete(nullptr);
            return;
        }
        const esp_err_t power_save_result = esp_wifi_set_ps(WIFI_PS_NONE);
        if (power_save_result != ESP_OK)
        {
            ESP_LOGE(TAG, "Cannot disable Wi-Fi power save for continuous audio: %s",
                     esp_err_to_name(power_save_result));
            vTaskDelete(nullptr);
            return;
        }
        ESP_LOGI(TAG, "Wi-Fi power save disabled for continuous audio streaming");
        ESP_ERROR_CHECK(esp_wifi_connect());

        const EventBits_t initial_bits = xEventGroupWaitBits(
            wifi_events, CONNECTED_BIT, pdFALSE, pdTRUE, pdMS_TO_TICKS(10000));
        if ((initial_bits & CONNECTED_BIT) == 0)
        {
            ESP_LOGW(TAG, "Station has not received an IP within 10 seconds; enabling setup access point");
            ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
            ESP_ERROR_CHECK(start_provisioning_portal());
            ESP_LOGI(TAG, "Setup access point started after station timeout; SSID: %s; dashboard: http://192.168.4.1/",
                     setup_ssid);
        }

        {
            const esp_sntp_config_t sntp_config = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
            esp_err_t time_result = esp_netif_sntp_init(&sntp_config);
            if (time_result == ESP_OK)
            {
                time_result = esp_netif_sntp_sync_wait(pdMS_TO_TICKS(10000));
                if (time_result != ESP_OK)
                    ESP_LOGW(TAG, "NTP time sync unavailable; continuing with device clock");
            }
            else if (time_result != ESP_ERR_INVALID_STATE)
            {
                ESP_LOGW(TAG, "Cannot start NTP time sync: %s", esp_err_to_name(time_result));
            }
        }

        // Supervisor loop for dynamic WebSocket client creation and AP portal activation
        while (true)
        {
            const EventBits_t bits = xEventGroupWaitBits(
                wifi_events, CONNECTED_BIT | START_AP_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(2000));

            if ((bits & START_AP_BIT) != 0)
            {
                xEventGroupClearBits(wifi_events, START_AP_BIT);
                if (!setup_ap_active)
                {
                    esp_wifi_set_mode(WIFI_MODE_APSTA);
                    start_provisioning_portal();
                    char ssid_line[40];
                    std::snprintf(ssid_line, sizeof(ssid_line), "Wi-Fi: %s", setup_ssid);
                    char password_line[40];
                    std::snprintf(password_line, sizeof(password_line), "Pass: %s", SETUP_AP_PASSWORD);
                    const char *setup_items[] = {ssid_line, password_line, "Open 192.168.4.1"};
                    DeviceDisplay::set_dashboard("notifications", "SETUP MODE", "Wi-Fi lost. Join AP",
                                                 setup_items, 3, 0, 0, 0, 0, 0, nullptr);
                }
            }

            if ((bits & CONNECTED_BIT) == 0)
            {
                if (websocket != nullptr)
                {
                    ESP_LOGW(TAG, "Wi-Fi connectivity lost; destroying WebSocket client instance");
                    xSemaphoreTake(websocket_send_mutex, portMAX_DELAY);
                    esp_websocket_client_stop(websocket);
                    esp_websocket_client_destroy(websocket);
                    websocket = nullptr;
                    xSemaphoreGive(websocket_send_mutex);
                    xEventGroupClearBits(websocket_events, WEBSOCKET_READY_BIT);
                }
                vTaskDelay(pdMS_TO_TICKS(1000));
                continue;
            }

            if (websocket == nullptr)
            {
                DeviceConfig current_config{};
                if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
                {
                    current_config = config;
                    xSemaphoreGive(config_mutex);
                }

                char normalized_backend[129]{};
                if (!normalize_backend_origin(current_config.backend_url.data(), normalized_backend,
                                              sizeof(normalized_backend)))
                {
                    ESP_LOGE(TAG, "Invalid backend origin stored in NVS: '%s'", current_config.backend_url.data());
                    vTaskDelay(pdMS_TO_TICKS(5000));
                    continue;
                }

                const bool secure_backend = std::strncmp(normalized_backend, "wss://", 6) == 0;
                const size_t backend_length = std::strlen(normalized_backend);
                const size_t origin_length = backend_length > 0 && normalized_backend[backend_length - 1] == '/'
                                                 ? backend_length - 1
                                                 : backend_length;
                char uri[320]{};
                const int uri_length = std::snprintf(
                    uri, sizeof(uri), "%.*s/api/v1/ws/devices/%s/audio",
                    static_cast<int>(origin_length), normalized_backend, current_config.device_id.data());

                char headers[180]{};
                const int header_length = std::snprintf(
                    headers, sizeof(headers), "Authorization: Bearer %s\r\n", current_config.device_token.data());

                if (uri_length <= 0 || uri_length >= static_cast<int>(sizeof(uri)) ||
                    header_length <= 0 || header_length >= static_cast<int>(sizeof(headers)))
                {
                    ESP_LOGE(TAG, "WebSocket URI or headers formatted invalidly");
                    vTaskDelay(pdMS_TO_TICKS(5000));
                    continue;
                }

                ESP_LOGI(TAG, "Initializing fresh WebSocket client instance for %s", uri);
                esp_websocket_client_config_t ws_config{};
                ws_config.uri = uri;
                ws_config.headers = headers;
                ws_config.buffer_size = 4096;
                ws_config.task_stack = 10240;
                ws_config.ping_interval_sec = 20;
                ws_config.network_timeout_ms = 15000;
                ws_config.reconnect_timeout_ms = 5000;
                ws_config.disable_auto_reconnect = false;

                if (secure_backend)
                {
                    ws_config.crt_bundle_attach = esp_crt_bundle_attach;
                    ws_config.transport = WEBSOCKET_TRANSPORT_OVER_SSL;
                }
                else
                {
                    ws_config.crt_bundle_attach = nullptr;
                    ws_config.transport = WEBSOCKET_TRANSPORT_OVER_TCP;
                }

                xSemaphoreTake(websocket_send_mutex, portMAX_DELAY);
                websocket = esp_websocket_client_init(&ws_config);
                if (websocket != nullptr)
                {
                    esp_websocket_register_events(websocket, WEBSOCKET_EVENT_ANY, websocket_event_handler, nullptr);
                    esp_websocket_client_start(websocket);
                }
                else
                {
                    ESP_LOGE(TAG, "Failed to create WebSocket client");
                }
                xSemaphoreGive(websocket_send_mutex);
            }
            else if (!esp_websocket_client_is_connected(websocket))
            {
                ESP_LOGD(TAG, "WebSocket client exists but disconnected; reconnection in progress");
            }
        }
    }

}

namespace DeviceNetwork
{

    bool camera_endpoint(char *url, size_t url_capacity, char *authorization, size_t authorization_capacity)
    {
        DeviceConfig current_config{};
        if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
        {
            current_config = config;
            xSemaphoreGive(config_mutex);
        }
        else
        {
            return false;
        }

        if (!saved_config_loaded || !station_configured || current_config.device_token[0] == '\0' ||
            wifi_events == nullptr || (xEventGroupGetBits(wifi_events) & CONNECTED_BIT) == 0)
        {
            return false;
        }
        char base[192]{};
        if (!make_http_base_url(base, sizeof(base)))
            return false;
        const int url_length = std::snprintf(url, url_capacity, "%s/api/v1/vision/frames/%s", base, current_config.device_id.data());
        const int auth_length = std::snprintf(authorization, authorization_capacity, "Bearer %s", current_config.device_token.data());
        return url_length > 0 && url_length < static_cast<int>(url_capacity) &&
               auth_length > 0 && auth_length < static_cast<int>(authorization_capacity);
    }

    esp_err_t start()
    {
        if (config_mutex == nullptr)
        {
            config_mutex = xSemaphoreCreateMutex();
            if (config_mutex == nullptr)
                return ESP_ERR_NO_MEM;
        }

        esp_err_t result = nvs_flash_init();
        if (result == ESP_ERR_NVS_NO_FREE_PAGES || result == ESP_ERR_NVS_NEW_VERSION_FOUND)
        {
            ESP_RETURN_ON_ERROR(nvs_flash_erase(), TAG, "Cannot reset NVS storage");
            result = nvs_flash_init();
        }
        if (result != ESP_OK)
            return result;
        result = esp_netif_init();
        if (result != ESP_OK && result != ESP_ERR_INVALID_STATE)
            return result;
        result = esp_event_loop_create_default();
        if (result != ESP_OK && result != ESP_ERR_INVALID_STATE)
            return result;
        load_runtime_settings();
        boot_identifier = static_cast<uint64_t>(esp_timer_get_time());
        result = apply_brightness(brightness_percent);
        if (result != ESP_OK)
            return result;
        result = AudioI2S::set_capture_enabled(microphone_stream_enabled.load());
        if (result != ESP_OK)
            return result;
        saved_config_loaded = load_config();

        char backend[129]{};
        if (xSemaphoreTake(config_mutex, pdMS_TO_TICKS(100)) == pdTRUE)
        {
            std::strncpy(backend, config.backend_url.data(), sizeof(backend) - 1);
            xSemaphoreGive(config_mutex);
        }

        station_configured = saved_config_loaded && is_supported_backend_origin(backend);
        {
            nvs_handle_t flag_handle;
            if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &flag_handle) == ESP_OK)
            {
                uint8_t portal_once = 0;
                if (nvs_get_u8(flag_handle, "portal_once", &portal_once) == ESP_OK && portal_once != 0)
                {
                    nvs_erase_key(flag_handle, "portal_once");
                    nvs_commit(flag_handle);
                    portal_session = true;
                    station_configured = false;
                    ESP_LOGW(TAG, "Voice-requested setup portal; station mode is paused for this boot");
                }
                nvs_close(flag_handle);
            }
        }
        ESP_RETURN_ON_ERROR(DeviceSchedule::start(report_schedule_fired), TAG, "Cannot start local scheduler");
        if (!station_configured)
        {
            ESP_LOGW(TAG, "No supported backend configured; station mode is disabled until setup");
            result = AudioI2S::set_capture_enabled(false);
            if (result != ESP_OK)
                return result;
        }
        if (xTaskCreate(network_task, "device_network", 8192, nullptr, configMAX_PRIORITIES - 5, nullptr) != pdPASS)
        {
            return ESP_ERR_NO_MEM;
        }
        return ESP_OK;
    }

}
#include "device_schedule.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <ctime>

#include "audio_i2s.h"
#include "device_display.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"

namespace {

constexpr char TAG[] = "device_schedule";
constexpr char NVS_NAMESPACE[] = "assistant";
constexpr char NVS_KEY[] = "schedule";
constexpr size_t MAX_ITEMS = 32;
constexpr int64_t VALID_TIME_FLOOR = 1'700'000'000;
constexpr int64_t MAX_LATE_SECONDS = 300;
constexpr int ALERT_REPEATS = 3;

struct Item {
    char id[20];
    char title[40];
    char kind[12];
    int64_t at;
    uint8_t fired;
    uint8_t reported;
};

struct Store {
    uint32_t count;
    Item items[MAX_ITEMS];
};

Store store{};
SemaphoreHandle_t store_lock = nullptr;
DeviceSchedule::ReportFired report_fired = nullptr;

void persist_locked() {
    nvs_handle_t handle;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle) != ESP_OK) return;
    const size_t size = sizeof(uint32_t) + store.count * sizeof(Item);
    if (nvs_set_blob(handle, NVS_KEY, &store, size) == ESP_OK) nvs_commit(handle);
    nvs_close(handle);
}

void load() {
    nvs_handle_t handle;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &handle) != ESP_OK) return;
    static Store loaded;
    size_t size = sizeof(loaded);
    if (nvs_get_blob(handle, NVS_KEY, &loaded, &size) == ESP_OK && size >= sizeof(uint32_t) &&
        loaded.count <= MAX_ITEMS && size == sizeof(uint32_t) + loaded.count * sizeof(Item)) {
        store = loaded;
    }
    nvs_close(handle);
}

void show_alert(const Item& item) {
    const char* heading = std::strcmp(item.kind, "alarm") == 0 ? "ALARM" : "REMINDER";
    const char* items[] = {item.title};
    DeviceDisplay::set_dashboard("notifications", heading, item.title, items, 1, 0, 0, 0, 1, 0, nullptr);
    for (int repeat = 0; repeat < ALERT_REPEATS; ++repeat) {
        AudioI2S::queue_sound_effect("notification");
        vTaskDelay(pdMS_TO_TICKS(1800));
    }
}

void schedule_task(void*) {
    int64_t last_report_attempt = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        const int64_t now = static_cast<int64_t>(time(nullptr));
        if (now < VALID_TIME_FLOOR) continue;

        Item due{};
        bool has_due = false;
        xSemaphoreTake(store_lock, portMAX_DELAY);
        for (uint32_t index = 0; index < store.count; ++index) {
            Item& item = store.items[index];
            if (item.fired || item.at > now) continue;
            item.fired = 1;
            if (now - item.at > MAX_LATE_SECONDS) {
                item.reported = 1;  // missed while powered off; do not alarm for something stale
                continue;
            }
            due = item;
            has_due = true;
            break;
        }
        if (has_due) persist_locked();
        xSemaphoreGive(store_lock);

        if (has_due) {
            ESP_LOGI(TAG, "Firing local %s: %s", due.kind, due.title);
            show_alert(due);
            vTaskDelay(pdMS_TO_TICKS(12000));
            DeviceDisplay::set_state("idle");
        }

        if (report_fired != nullptr && now - last_report_attempt >= 10) {
            last_report_attempt = now;
            Item pending{};
            bool has_pending = false;
            xSemaphoreTake(store_lock, portMAX_DELAY);
            for (uint32_t index = 0; index < store.count; ++index) {
                if (store.items[index].fired && !store.items[index].reported) {
                    pending = store.items[index];
                    has_pending = true;
                    break;
                }
            }
            xSemaphoreGive(store_lock);
            if (has_pending && report_fired(pending.id, pending.title, pending.kind)) {
                xSemaphoreTake(store_lock, portMAX_DELAY);
                for (uint32_t index = 0; index < store.count; ++index) {
                    if (std::strcmp(store.items[index].id, pending.id) == 0) store.items[index].reported = 1;
                }
                persist_locked();
                xSemaphoreGive(store_lock);
            }
        }
    }
}

}

namespace DeviceSchedule {

esp_err_t start(ReportFired report) {
    report_fired = report;
    store_lock = xSemaphoreCreateMutex();
    if (store_lock == nullptr) return ESP_ERR_NO_MEM;
    load();
    ESP_LOGI(TAG, "Loaded %u stored schedule items", static_cast<unsigned>(store.count));
    if (xTaskCreate(schedule_task, "device_schedule", 4096, nullptr, tskIDLE_PRIORITY + 3, nullptr) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void sync(const cJSON* items) {
    if (!cJSON_IsArray(items) || store_lock == nullptr) return;
    static Store next;
    std::memset(&next, 0, sizeof(next));
    xSemaphoreTake(store_lock, portMAX_DELAY);
    const cJSON* entry = nullptr;
    cJSON_ArrayForEach(entry, items) {
        if (next.count >= MAX_ITEMS) break;
        const cJSON* id = cJSON_GetObjectItemCaseSensitive(entry, "id");
        const cJSON* title = cJSON_GetObjectItemCaseSensitive(entry, "title");
        const cJSON* kind = cJSON_GetObjectItemCaseSensitive(entry, "kind");
        const cJSON* at = cJSON_GetObjectItemCaseSensitive(entry, "at");
        if (!cJSON_IsString(id) || !cJSON_IsString(title) || !cJSON_IsNumber(at)) continue;
        Item& item = next.items[next.count];
        std::snprintf(item.id, sizeof(item.id), "%.19s", id->valuestring);
        std::snprintf(item.title, sizeof(item.title), "%.39s", title->valuestring);
        std::snprintf(item.kind, sizeof(item.kind), "%.11s", cJSON_IsString(kind) ? kind->valuestring : "reminder");
        item.at = static_cast<int64_t>(at->valuedouble);
        for (uint32_t old = 0; old < store.count; ++old) {
            if (std::strcmp(store.items[old].id, item.id) == 0) {
                item.fired = store.items[old].fired;
                item.reported = store.items[old].reported;
            }
        }
        ++next.count;
    }
    // Keep fired items the backend has not yet been told about.
    for (uint32_t old = 0; old < store.count && next.count < MAX_ITEMS; ++old) {
        bool present = false;
        for (uint32_t index = 0; index < next.count; ++index) {
            if (std::strcmp(next.items[index].id, store.items[old].id) == 0) present = true;
        }
        if (!present && store.items[old].fired && !store.items[old].reported) next.items[next.count++] = store.items[old];
    }
    store = next;
    persist_locked();
    xSemaphoreGive(store_lock);
    ESP_LOGI(TAG, "Schedule synced: %u items", static_cast<unsigned>(next.count));
}

}

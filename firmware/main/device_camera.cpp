#include "device_camera.h"

#include <atomic>
#include <cstring>

#include "device_network.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "usb/usb_host.h"
#include "usb/uvc_host.h"

namespace
{

    constexpr char TAG[] = "camera";
    constexpr int64_t POST_INTERVAL_US = 700 * 1000;
    constexpr size_t JPEG_COPY_CAPACITY = 256 * 1024;
    constexpr int FRAME_BUFFERS = 3;

    QueueHandle_t frame_queue;
    SemaphoreHandle_t stream_lock;
    uvc_host_stream_hdl_t stream = nullptr;
    uint8_t *jpeg_copy = nullptr;
    esp_http_client_handle_t http = nullptr;
    std::atomic<bool> device_present{false};
    std::atomic<bool> stream_active{false};

    bool on_frame(const uvc_host_frame_t *frame, void *)
    {
        if (frame->vs_format.format != UVC_VS_FORMAT_MJPEG)
            return true;
        if (xQueueSend(frame_queue, &frame, 0) != pdPASS)
            return true;
        return false;
    }

    void on_stream_event(const uvc_host_stream_event_data_t *event, void *)
    {
        switch (event->type)
        {
        case UVC_HOST_DEVICE_DISCONNECTED:
            ESP_LOGW(TAG, "USB camera disconnected");
            xSemaphoreTake(stream_lock, portMAX_DELAY);
            if (stream != nullptr)
            {
                uvc_host_stream_close(event->device_disconnected.stream_hdl);
                stream = nullptr;
                stream_active = false;
            }
            xSemaphoreGive(stream_lock);
            break;
        case UVC_HOST_TRANSFER_ERROR:
            ESP_LOGW(TAG, "USB camera transfer error: %d", static_cast<int>(event->transfer_error.error));
            break;
        default:
            break;
        }
    }

    void on_driver_event(const uvc_host_driver_event_data_t *event, void *)
    {
        if (event->type == UVC_HOST_DRIVER_EVENT_DEVICE_CONNECTED)
        {
            ESP_LOGI(TAG, "USB camera connected (%u formats)",
                     static_cast<unsigned>(event->device_connected.frame_info_num));
            device_present = true;
        }
    }

    void usb_library_task(void *)
    {
        while (true)
        {
            uint32_t flags = 0;
            usb_host_lib_handle_events(portMAX_DELAY, &flags);
            if (flags & USB_HOST_LIB_EVENT_FLAGS_NO_CLIENTS)
                usb_host_device_free_all();
        }
    }

    bool open_stream()
    {
        if (stream_active)
            return true;

        // Delay briefly to let USB enumeration fully settle before opening stream handles
        vTaskDelay(pdMS_TO_TICKS(1500));

        uvc_host_stream_config_t config{};
        config.event_cb = on_stream_event;
        config.frame_cb = on_frame;
        config.usb.uvc_stream_index = 0;

        // Force explicit resolution and MJPEG format matching Alt Setting 4 parameters
        config.vs_format.h_res = 320;
        config.vs_format.v_res = 240;
        config.vs_format.fps = 15.0f;
        config.vs_format.format = UVC_VS_FORMAT_MJPEG;

        config.advanced.number_of_frame_buffers = FRAME_BUFFERS;
        config.advanced.frame_size = 0;
        config.advanced.frame_heap_caps = MALLOC_CAP_SPIRAM;
        config.advanced.number_of_urbs = 2;
        config.advanced.urb_size = 0;

        uvc_host_stream_hdl_t handle = nullptr;
        esp_err_t err = uvc_host_stream_open(&config, pdMS_TO_TICKS(5000), &handle);
        if (err != ESP_OK)
        {
            ESP_LOGW(TAG, "uvc_host_stream_open failed: %s", esp_err_to_name(err));
            return false;
        }

        err = uvc_host_stream_start(handle);
        if (err != ESP_OK)
        {
            ESP_LOGW(TAG, "uvc_host_stream_start failed: %s", esp_err_to_name(err));
            uvc_host_stream_close(handle);
            return false;
        }

        xSemaphoreTake(stream_lock, portMAX_DELAY);
        stream = handle;
        stream_active = true;
        xSemaphoreGive(stream_lock);

        ESP_LOGI(TAG, "Successfully streaming MJPEG 320x240 @ 15.0 FPS");
        return true;
    }

    void drop_http()
    {
        if (http != nullptr)
        {
            esp_http_client_cleanup(http);
            http = nullptr;
        }
    }

    bool post_frame(size_t length)
    {
        if (http == nullptr)
        {
            char url[256]{};
            char auth[160]{};
            if (!DeviceNetwork::camera_endpoint(url, sizeof(url), auth, sizeof(auth)))
                return false;
            esp_http_client_config_t config{};
            config.url = url;
            config.method = HTTP_METHOD_POST;
            config.timeout_ms = 5000;
            config.crt_bundle_attach = esp_crt_bundle_attach;
            http = esp_http_client_init(&config);
            if (http == nullptr)
                return false;
            esp_http_client_set_header(http, "Content-Type", "image/jpeg");
            esp_http_client_set_header(http, "Authorization", auth);
        }
        esp_http_client_set_post_field(http, reinterpret_cast<const char *>(jpeg_copy), static_cast<int>(length));
        const esp_err_t result = esp_http_client_perform(http);
        const int status = result == ESP_OK ? esp_http_client_get_status_code(http) : -1;
        if (result != ESP_OK || status >= 400)
        {
            ESP_LOGW(TAG, "Frame upload failed (%s, HTTP %d)", esp_err_to_name(result), status);
            drop_http();
            return false;
        }
        return true;
    }

    void drain_frames()
    {
        const uvc_host_frame_t *discarded = nullptr;
        while (xQueueReceive(frame_queue, &discarded, 0) == pdTRUE)
        {
        }
    }

    void camera_task(void *)
    {
        int64_t last_post = 0;
        while (true)
        {
            if (!stream_active)
            {
                drain_frames();
                if (!open_stream())
                {
                    vTaskDelay(pdMS_TO_TICKS(4000));
                    continue;
                }
            }
            const uvc_host_frame_t *frame = nullptr;
            if (xQueueReceive(frame_queue, &frame, pdMS_TO_TICKS(3000)) != pdTRUE)
                continue;

            size_t length = 0;
            bool want = false;
            xSemaphoreTake(stream_lock, portMAX_DELAY);
            if (stream != nullptr && frame != nullptr)
            {
                const int64_t now = esp_timer_get_time();
                want = now - last_post >= POST_INTERVAL_US && frame->data_len > 0 && frame->data_len <= JPEG_COPY_CAPACITY;
                if (want)
                {
                    std::memcpy(jpeg_copy, frame->data, frame->data_len);
                    length = frame->data_len;
                    last_post = now;
                }
                uvc_host_frame_return(stream, const_cast<uvc_host_frame_t *>(frame));
            }
            xSemaphoreGive(stream_lock);

            if (want)
            {
                post_frame(length);
            }
        }
    }

} // namespace

namespace DeviceCamera
{

    esp_err_t start()
    {
        jpeg_copy = static_cast<uint8_t *>(heap_caps_malloc(JPEG_COPY_CAPACITY, MALLOC_CAP_SPIRAM));
        frame_queue = xQueueCreate(FRAME_BUFFERS, sizeof(uvc_host_frame_t *));
        stream_lock = xSemaphoreCreateMutex();
        if (jpeg_copy == nullptr || frame_queue == nullptr || stream_lock == nullptr)
            return ESP_ERR_NO_MEM;

        usb_host_config_t host_config{};
        host_config.skip_phy_setup = false;
        host_config.intr_flags = ESP_INTR_FLAG_LEVEL1;
        esp_err_t result = usb_host_install(&host_config);
        if (result != ESP_OK)
            return result;

        if (xTaskCreate(usb_library_task, "usb_host", 4096, nullptr, 15, nullptr) != pdPASS)
            return ESP_ERR_NO_MEM;

        uvc_host_driver_config_t driver{};
        driver.driver_task_stack_size = 4096;
        driver.driver_task_priority = 16;
        driver.xCoreID = tskNO_AFFINITY;
        driver.create_background_task = true;
        driver.event_cb = on_driver_event;
        result = uvc_host_install(&driver);
        if (result != ESP_OK)
            return result;

        if (xTaskCreate(camera_task, "camera", 8192, nullptr, 5, nullptr) != pdPASS)
            return ESP_ERR_NO_MEM;
        ESP_LOGI(TAG, "USB camera host started");
        return ESP_OK;
    }

} // namespace DeviceCamera
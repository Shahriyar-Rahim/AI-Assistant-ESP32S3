#include "audio_i2s.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>

#include "hardware_config.h"
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "freertos/task.h"

namespace {
const char* TAG = "audio_i2s";
i2s_chan_handle_t rx_channel = nullptr;
i2s_chan_handle_t tx_channel = nullptr;
QueueHandle_t pcm_queue = nullptr;
QueueHandle_t playback_queue = nullptr;
std::atomic_bool capture_enabled{true};
std::atomic_bool startup_sound_active{false};
constexpr TickType_t AMPLIFIER_IDLE_TIMEOUT = pdMS_TO_TICKS(250);
constexpr size_t STARTUP_TAIL_SAMPLES = HardwareConfig::Audio::SAMPLE_RATE / 10;

esp_err_t set_amplifier_enabled(bool enabled) {
    return gpio_set_level(HardwareConfig::Amplifier::SHUTDOWN, enabled ? 1 : 0);
}

void capture_task(void*) {
    int32_t raw[HardwareConfig::Audio::CAPTURE_SAMPLES] = {};
    TickType_t last_overflow_log = 0;
    uint32_t dropped_blocks = 0;
    while (true) {
        if (!capture_enabled.load()) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        size_t bytes_read = 0;
        const esp_err_t result = i2s_channel_read(
            rx_channel, raw, sizeof(raw), &bytes_read, portMAX_DELAY);
        if (result != ESP_OK) {
            if (!capture_enabled.load()) continue;
            ESP_LOGE(TAG, "I2S microphone read failed: %s", esp_err_to_name(result));
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        if (!capture_enabled.load()) continue;
        AudioI2S::PcmBlock block{};
        const size_t count = bytes_read / sizeof(raw[0]);
        block.sample_count = static_cast<uint16_t>(count);
        for (size_t i = 0; i < count; ++i) {
            block.samples[i] = static_cast<int16_t>(raw[i] >> 16);
        }
        if (xQueueSend(pcm_queue, &block, 0) != pdTRUE) {
            ++dropped_blocks;
            AudioI2S::PcmBlock discarded{};
            xQueueReceive(pcm_queue, &discarded, 0);
            if (xQueueSend(pcm_queue, &block, 0) != pdTRUE) {
                ESP_LOGE(TAG, "Capture queue remained full after dropping the oldest block");
            }
            const TickType_t now = xTaskGetTickCount();
            if (now - last_overflow_log >= pdMS_TO_TICKS(5000)) {
                ESP_LOGW(TAG, "Capture queue overflowed %lu times; oldest blocks were dropped",
                    static_cast<unsigned long>(dropped_blocks));
                dropped_blocks = 0;
                last_overflow_log = now;
            }
        }
        vTaskDelay(1);
    }
}

void playback_task(void*) {
    AudioI2S::PcmBlock block{};
    bool amplifier_enabled = false;
    while (true) {
        if (xQueueReceive(playback_queue, &block, AMPLIFIER_IDLE_TIMEOUT) != pdTRUE) {
            if (amplifier_enabled && !startup_sound_active.load()) {
                const esp_err_t mute_result = set_amplifier_enabled(false);
                if (mute_result != ESP_OK) {
                    ESP_LOGE(TAG, "Cannot mute idle amplifier: %s", esp_err_to_name(mute_result));
                }
                amplifier_enabled = false;
            }
            continue;
        }
        if (block.sample_count == 0 || block.sample_count > HardwareConfig::Audio::CAPTURE_SAMPLES) {
            ESP_LOGE(TAG, "Discarding invalid amplifier playback block");
            continue;
        }
        if (!amplifier_enabled) {
            const esp_err_t enable_result = set_amplifier_enabled(true);
            if (enable_result != ESP_OK) {
                ESP_LOGE(TAG, "Cannot enable amplifier for queued playback: %s", esp_err_to_name(enable_result));
                continue;
            }
            amplifier_enabled = true;
            vTaskDelay(pdMS_TO_TICKS(2));
        }
        size_t bytes_written = 0;
        const esp_err_t result = i2s_channel_write(
            tx_channel, block.samples,
            block.sample_count * sizeof(int16_t), &bytes_written, portMAX_DELAY);
        if (result != ESP_OK || bytes_written != block.sample_count * sizeof(int16_t)) {
            ESP_LOGE(TAG, "I2S amplifier write failed: %s", esp_err_to_name(result));
            const esp_err_t mute_result = set_amplifier_enabled(false);
            if (mute_result != ESP_OK) {
                ESP_LOGE(TAG, "Cannot mute amplifier after playback failure: %s", esp_err_to_name(mute_result));
            }
            amplifier_enabled = false;
        }
    }
}
}

namespace AudioI2S {

esp_err_t initialize() {
    gpio_config_t amp_enable = {
        .pin_bit_mask = 1ULL << HardwareConfig::Amplifier::SHUTDOWN,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t result = gpio_config(&amp_enable);
    if (result != ESP_OK) return result;
    result = set_amplifier_enabled(false);
    if (result != ESP_OK) return result;

    pcm_queue = xQueueCreate(8, sizeof(PcmBlock));
    playback_queue = xQueueCreate(8, sizeof(PcmBlock));
    if (pcm_queue == nullptr || playback_queue == nullptr) return ESP_ERR_NO_MEM;

    i2s_chan_config_t rx_config = I2S_CHANNEL_DEFAULT_CONFIG(
        HardwareConfig::Microphone::I2S_PORT, I2S_ROLE_MASTER);
    rx_config.dma_desc_num = HardwareConfig::Audio::DMA_DESC_COUNT;
    rx_config.dma_frame_num = HardwareConfig::Audio::DMA_FRAME_NUM;
    result = i2s_new_channel(&rx_config, nullptr, &rx_channel);
    if (result != ESP_OK) return result;

    i2s_std_config_t mic_config = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(HardwareConfig::Audio::SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = HardwareConfig::Microphone::BCLK,
            .ws = HardwareConfig::Microphone::WS,
            .dout = I2S_GPIO_UNUSED,
            .din = HardwareConfig::Microphone::DATA_IN,
            .invert_flags = {.mclk_inv = false, .bclk_inv = false, .ws_inv = false},
        },
    };
    mic_config.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;
    result = i2s_channel_init_std_mode(rx_channel, &mic_config);
    if (result != ESP_OK) return result;

    i2s_chan_config_t tx_config = I2S_CHANNEL_DEFAULT_CONFIG(
        HardwareConfig::Amplifier::I2S_PORT, I2S_ROLE_MASTER);
    tx_config.dma_desc_num = HardwareConfig::Audio::DMA_DESC_COUNT;
    tx_config.dma_frame_num = HardwareConfig::Audio::DMA_FRAME_NUM;
    result = i2s_new_channel(&tx_config, &tx_channel, nullptr);
    if (result != ESP_OK) return result;

    const i2s_std_config_t amp_config = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(HardwareConfig::Audio::SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = HardwareConfig::Amplifier::BCLK,
            .ws = HardwareConfig::Amplifier::LRC,
            .dout = HardwareConfig::Amplifier::DATA_OUT,
            .din = I2S_GPIO_UNUSED,
            .invert_flags = {.mclk_inv = false, .bclk_inv = false, .ws_inv = false},
        },
    };
    result = i2s_channel_init_std_mode(tx_channel, &amp_config);
    if (result != ESP_OK) return result;

    result = i2s_channel_enable(rx_channel);
    if (result != ESP_OK) return result;
    result = i2s_channel_enable(tx_channel);
    if (result != ESP_OK) return result;
    if (xTaskCreate(capture_task, "mic_capture", 4096, nullptr, configMAX_PRIORITIES - 3, nullptr) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreate(playback_task, "speaker_playback", 4096, nullptr, configMAX_PRIORITIES - 3, nullptr) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "Independent microphone (I2S0) and amplifier (I2S1) started");
    return ESP_OK;
}

esp_err_t play_startup_sound(uint32_t duration_ms) {
    if (tx_channel == nullptr || duration_ms == 0 || duration_ms > 10'000) {
        return ESP_ERR_INVALID_ARG;
    }
    if (startup_sound_active.exchange(true)) return ESP_ERR_INVALID_STATE;

    constexpr double PI = 3.14159265358979323846;
    constexpr double FREQUENCY_HZ = 660.0;
    constexpr int16_t AMPLITUDE = 6000;
    constexpr size_t BLOCK_SAMPLES = HardwareConfig::Audio::CAPTURE_SAMPLES;
    const uint64_t total_samples =
        static_cast<uint64_t>(HardwareConfig::Audio::SAMPLE_RATE) * duration_ms / 1000;
    std::array<int16_t, BLOCK_SAMPLES> samples{};
    uint64_t samples_sent = 0;

    esp_err_t result = set_amplifier_enabled(true);
    if (result != ESP_OK) {
        startup_sound_active.store(false);
        return result;
    }
    vTaskDelay(pdMS_TO_TICKS(2));
    while (samples_sent < total_samples) {
        const size_t count = static_cast<size_t>(
            std::min<uint64_t>(BLOCK_SAMPLES, total_samples - samples_sent));
        for (size_t index = 0; index < count; ++index) {
            const uint64_t position = samples_sent + index;
            const uint64_t fade_samples = HardwareConfig::Audio::SAMPLE_RATE / 50;
            const double fade_in = position < fade_samples
                ? static_cast<double>(position) / fade_samples
                : 1.0;
            const uint64_t samples_remaining = total_samples - position;
            const double fade_out = samples_remaining < fade_samples
                ? static_cast<double>(samples_remaining) / fade_samples
                : 1.0;
            const double envelope = std::min(fade_in, fade_out);
            const double phase = 2.0 * PI * FREQUENCY_HZ * position /
                HardwareConfig::Audio::SAMPLE_RATE;
            samples[index] = static_cast<int16_t>(AMPLITUDE * envelope * std::sin(phase));
        }

        size_t bytes_written = 0;
        result = i2s_channel_write(
            tx_channel, samples.data(), count * sizeof(samples[0]),
            &bytes_written, portMAX_DELAY);
        if (result != ESP_OK) {
            ESP_LOGE(TAG, "Startup sound playback failed: %s", esp_err_to_name(result));
            const esp_err_t mute_result = set_amplifier_enabled(false);
            if (mute_result != ESP_OK) {
                ESP_LOGE(TAG, "Cannot mute amplifier after startup playback failure: %s",
                    esp_err_to_name(mute_result));
            }
            startup_sound_active.store(false);
            return result;
        }
        if (bytes_written != count * sizeof(samples[0])) {
            ESP_LOGE(TAG, "Startup sound playback wrote %u of %u bytes",
                static_cast<unsigned>(bytes_written),
                static_cast<unsigned>(count * sizeof(samples[0])));
            const esp_err_t mute_result = set_amplifier_enabled(false);
            if (mute_result != ESP_OK) {
                ESP_LOGE(TAG, "Cannot mute amplifier after short startup playback: %s",
                    esp_err_to_name(mute_result));
            }
            startup_sound_active.store(false);
            return ESP_FAIL;
        }
        samples_sent += count;
    }
    samples.fill(0);
    for (size_t tail_offset = 0; tail_offset < STARTUP_TAIL_SAMPLES; tail_offset += BLOCK_SAMPLES) {
        size_t tail_written = 0;
        const size_t tail_count = std::min(BLOCK_SAMPLES, STARTUP_TAIL_SAMPLES - tail_offset);
        result = i2s_channel_write(
            tx_channel, samples.data(), tail_count * sizeof(samples[0]),
            &tail_written, portMAX_DELAY);
        if (result != ESP_OK || tail_written != tail_count * sizeof(samples[0])) {
            ESP_LOGE(TAG, "Could not flush startup audio tail: %s", esp_err_to_name(result));
            const esp_err_t mute_result = set_amplifier_enabled(false);
            if (mute_result != ESP_OK) {
                ESP_LOGE(TAG, "Cannot mute amplifier after startup tail failure: %s",
                    esp_err_to_name(mute_result));
            }
            startup_sound_active.store(false);
            return result == ESP_OK ? ESP_FAIL : result;
        }
    }
    result = set_amplifier_enabled(false);
    startup_sound_active.store(false);
    if (result != ESP_OK) return result;
    ESP_LOGI(TAG, "Two-second startup sound played through I2S1");
    return ESP_OK;
}

esp_err_t set_capture_enabled(bool enabled) {
    if (rx_channel == nullptr) return ESP_ERR_INVALID_STATE;
    if (capture_enabled.load() == enabled) return ESP_OK;
    const esp_err_t result = enabled ? i2s_channel_enable(rx_channel) : i2s_channel_disable(rx_channel);
    if (result == ESP_OK) capture_enabled.store(enabled);
    return result;
}

QueueHandle_t capture_queue() {
    return pcm_queue;
}

esp_err_t queue_playback(const int16_t* samples, size_t sample_count) {
    if (samples == nullptr || sample_count == 0 ||
        sample_count > HardwareConfig::Audio::CAPTURE_SAMPLES) return ESP_ERR_INVALID_ARG;
    PcmBlock block{};
    block.sample_count = static_cast<uint16_t>(sample_count);
    for (size_t i = 0; i < sample_count; ++i) block.samples[i] = samples[i];
    return xQueueSend(playback_queue, &block, 0) == pdTRUE ? ESP_OK : ESP_ERR_TIMEOUT;
}

esp_err_t write_playback(const int16_t* samples, size_t sample_count, size_t* samples_written) {
    if (samples == nullptr || samples_written == nullptr) return ESP_ERR_INVALID_ARG;
    *samples_written = 0;
    const esp_err_t result = queue_playback(samples, sample_count);
    if (result == ESP_OK) *samples_written = sample_count;
    return result;
}

}

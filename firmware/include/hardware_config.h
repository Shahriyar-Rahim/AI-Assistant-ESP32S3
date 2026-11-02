#pragma once

#include <cstdint>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"

namespace HardwareConfig {

namespace Microphone {
constexpr int I2S_PORT = I2S_NUM_0;
constexpr gpio_num_t WS = GPIO_NUM_17;
constexpr gpio_num_t BCLK = GPIO_NUM_18;
constexpr gpio_num_t DATA_IN = GPIO_NUM_15;
}

namespace Amplifier {
constexpr int I2S_PORT = I2S_NUM_1;
constexpr gpio_num_t BCLK = GPIO_NUM_3;
constexpr gpio_num_t LRC = GPIO_NUM_8;
constexpr gpio_num_t DATA_OUT = GPIO_NUM_16;
constexpr gpio_num_t SHUTDOWN = GPIO_NUM_46;
}

namespace OLED {
constexpr i2c_port_num_t I2C_PORT = 0;
constexpr gpio_num_t SDA = GPIO_NUM_1;
constexpr gpio_num_t SCL = GPIO_NUM_2;
}

namespace TFT {
constexpr spi_host_device_t SPI_HOST = SPI2_HOST;
constexpr gpio_num_t CS = GPIO_NUM_10;
constexpr gpio_num_t DC = GPIO_NUM_9;
constexpr gpio_num_t RST = GPIO_NUM_14;
constexpr gpio_num_t MOSI = GPIO_NUM_11;
constexpr gpio_num_t SCK = GPIO_NUM_12;
constexpr gpio_num_t BACKLIGHT = GPIO_NUM_13;
constexpr ledc_mode_t PWM_MODE = LEDC_LOW_SPEED_MODE;
constexpr ledc_timer_t PWM_TIMER = LEDC_TIMER_0;
constexpr ledc_channel_t PWM_CHANNEL = LEDC_CHANNEL_0;
}

namespace Audio {
constexpr uint32_t SAMPLE_RATE = 16000;
constexpr uint8_t BITS_PER_SAMPLE = 16;
constexpr uint8_t CHANNELS = 1;
constexpr size_t CAPTURE_SAMPLES = 320;
constexpr size_t DMA_DESC_COUNT = 8;
constexpr size_t DMA_FRAME_NUM = 320;
}

namespace Features {
constexpr bool ENABLE_OLED = true;
constexpr bool ENABLE_TFT = true;
constexpr bool ENABLE_HANDS_FREE_AUDIO = true;
}

}

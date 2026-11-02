idf_component_register(
    SRCS "main.cpp" "audio_i2s.cpp" "device_network.cpp" "device_display.cpp"
    INCLUDE_DIRS "../include"
    REQUIRES esp_driver_i2s esp_driver_gpio esp_driver_i2c esp_driver_spi esp_driver_ledc esp_wifi esp_netif nvs_flash esp_event esp_http_server esp_http_client app_update cjson esp_websocket_client mbedtls
)

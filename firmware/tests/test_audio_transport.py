from pathlib import Path

from app.services.audio.framing import AudioFrame, HEADER, encode_frame


ROOT = Path(__file__).resolve().parents[1]


def test_firmware_audio_header_matches_backend_wire_contract() -> None:
    source = (ROOT / "main" / "device_network.cpp").read_text()
    assert HEADER.size == 25
    assert "constexpr size_t AUDIO_HEADER_SIZE = 25;" in source
    assert "const uint32_t sequence = next_audio_sequence.fetch_add(1);" in source
    assert "put_be32(packet.data() + 5, sequence);" in source
    assert "put_be64(packet.data() + 9," in source
    assert "put_be32(packet.data() + 17, HardwareConfig::Audio::SAMPLE_RATE);" in source
    assert "put_be16(packet.data() + 21, static_cast<uint16_t>(sample_count));" in source
    assert "uint64_t boot_identifier = 0;" in source
    assert "boot_identifier = static_cast<uint64_t>(esp_timer_get_time());" in source
    assert "static_cast<unsigned long long>(boot_identifier)" in source
    assert "static_cast<unsigned long long>(esp_timer_get_time())" not in source
    assert "packet[23] = HardwareConfig::Audio::CHANNELS;" in source
    assert "packet[24] = 1;" in source
    assert "std::memcpy(packet.data() + AUDIO_HEADER_SIZE, samples.data(), pcm_bytes);" in source
    assert "constexpr size_t MAX_AUDIO_FRAME_SAMPLES = HardwareConfig::Audio::CAPTURE_SAMPLES * 4;" in source
    assert "xQueueReceive(queue, &next_block, merged == 1 ? pdMS_TO_TICKS(40) : 0)" in source
    assert "sample_count += next_block.sample_count;" in source
    assert "constexpr uint32_t AUDIO_UPLINK_STACK_BYTES = 12 * 1024;" in source
    assert "xTaskCreate(audio_uplink_task, \"audio_uplink\", AUDIO_UPLINK_STACK_BYTES" in source
    assert "uxTaskGetStackHighWaterMark(nullptr)" in source
    assert "replay_window[sequence % REPLAY_WINDOW_SIZE]" in source
    assert 'std::strcmp(type->valuestring, "audio.ack") == 0' in source
    assert 'std::strcmp(type->valuestring, "audio.nack") == 0' in source
    assert 'std::strcmp(type->valuestring, "device.status") == 0' in source
    assert "queue_replay_range(current - static_cast<uint32_t>(REPLAY_WINDOW_SIZE));" in source
    replay_scheduler = source.split("void queue_replay_range", 1)[1].split("void replay_audio_range", 1)[0]
    assert replay_scheduler.count("for (size_t offset") == 1
    assert "xQueueOverwrite(replay_requests, &expected_sequence)" in replay_scheduler
    assert "replay_requests = xQueueCreate(1, sizeof(uint32_t));" in source
    assert "void replay_task" not in source
    assert "replay_audio_range(requested_replay);" in source
    assert "xTaskCreate(replay_task" not in source
    assert source.index("replay_audio_range(requested_replay);") < source.index(
        "const int sent = send_websocket_binary(", source.index("void audio_uplink_task")
    )
    assert "WebSocket transport error: %.*s; type=%d close_code=%d handshake_status=%d" in source
    assert "Received server event: %s" in source
    assert "if (event->op_code == 0x9 || event->op_code == 0xA)" in source
    assert source.index("if (event->op_code == 0x9 || event->op_code == 0xA)") < source.index(
        'if (event->payload_len <= 0'
    )
    assert "constexpr TickType_t AUDIO_SEND_TIMEOUT = pdMS_TO_TICKS(3000);" in source
    assert "ws_config.network_timeout_ms = 10000;" in source
    assert "ws_config.reconnect_timeout_ms = 5000;" in source
    assert "Audio send failed (sequence=%lu, result=%d, bytes=%u/%u, elapsed_ms=%lld)" in source

    frame = AudioFrame(0x10203040, 0x0102030405060708, 16_000, 1, 1, b"\x34\x12")
    packet = encode_frame(frame)
    assert packet[:25] == bytes.fromhex("454149410110203040010203040506070800003e8000010101")
    assert packet[25:] == b"\x34\x12"


def test_firmware_waits_for_authenticated_server_ready_and_applies_controls() -> None:
    source = (ROOT / "main" / "device_network.cpp").read_text()
    audio = (ROOT / "main" / "audio_i2s.cpp").read_text()
    sdkconfig = (ROOT / "sdkconfig.esp32-s3-n16r8").read_text()
    network_setup = source[source.index("void network_task(void*)"):]
    assert source.count("esp_websocket_client_send_text(") == 1
    assert source.count("esp_websocket_client_send_bin(") == 1
    assert "xSemaphoreTake(websocket_send_mutex, timeout)" in source
    assert "websocket_send_mutex = xSemaphoreCreateMutex();" in source
    assert "Authorization: Bearer %s" in source
    assert "ws_config.crt_bundle_attach = secure_backend ? esp_crt_bundle_attach : nullptr;" in source
    assert "ws_config.disable_auto_reconnect = false;" in source
    assert 'std::strcmp(type->valuestring, "device.status") == 0' in source
    assert "WEBSOCKET_READY_BIT" in source
    assert "persist_runtime_setting(\"brightness\"" in source
    assert "persist_runtime_setting(\"mic_enabled\"" in source
    assert "AudioI2S::set_capture_enabled(enabled)" in source
    assert "i2s_channel_disable(rx_channel)" in audio
    assert network_setup.index("xTaskCreate(audio_uplink_task") < network_setup.index("xEventGroupWaitBits(wifi_events")
    assert "now - last_overflow_log >= pdMS_TO_TICKS(5000)" in audio
    capture_task = audio.split("void capture_task(void*)", 1)[1].split(
        "void playback_task(void*)", 1
    )[0]
    assert "i2s_channel_read(" in capture_task
    assert "CAPTURE_BLOCK_PERIOD" in capture_task
    assert "vTaskDelayUntil(&last_capture_wake, CAPTURE_BLOCK_PERIOD);" in capture_task
    loop_body = capture_task.split("while (true)", 1)[1]
    assert loop_body.index("last_capture_wake = xTaskGetTickCount();") < loop_body.index(
        "i2s_channel_read("
    )
    assert "bytes_read == 0" in capture_task
    assert "CONFIG_ESP_WS_CLIENT_SEPARATE_TX_LOCK=y" in sdkconfig
    assert "esp_wifi_set_ps(WIFI_PS_NONE)" in source
    assert network_setup.index("esp_wifi_set_ps(WIFI_PS_NONE)") < network_setup.index("esp_wifi_connect()")
    assert "esp_wifi_sta_get_ap_info(&ap_info)" in source
    assert 'cJSON_AddBoolToObject(status, "station_connected", station_has_ip);' in source
    assert 'cJSON_AddBoolToObject(status, "station_associated", station_associated);' in source
    assert 'cJSON_AddBoolToObject(status, "setup_ap_active", setup_ap_active);' in source
    assert '"station_associated"' in source
    assert '"station_connected"' in source
    assert '"station_ip"' in source
    assert '"rssi=%d dBm; capture continues"' in source
    assert '"Wi-Fi disconnected (reason=%u, rssi=%d dBm); retrying"' in source
    assert '"Wi-Fi connected; station IP: " IPSTR ", gateway: " IPSTR' in source


def test_travel_portal_stays_available_and_requires_public_tls_backend() -> None:
    source = (ROOT / "main" / "device_network.cpp").read_text()
    assert 'Buddy-Setup-%02X%02X' in source
    assert 'constexpr char SETUP_AP_PASSWORD[] = "BuddySetup-ESP32S3";' in source
    assert "SETUP_AP_PASSWORD);" in source
    assert "setup_password" not in source
    assert "ap_config.ap.ssid_hidden = 0;" in source
    assert 'http://192.168.4.1/' in source
    assert "normalize_backend_origin(" in source
    assert "placeholder='api.example.com or ws://192.168.10.163:8000'" in source
    assert "For a deployed backend, enter its public hostname or HTTPS/WSS URL." in source
    assert "if(s.backend_url)f.backend_url.value=s.backend_url;" in source
    assert 'backend.replace(0, 8, "wss://")' in source
    assert 'backend.insert(0, "wss://")' in source
    assert 'backend.replace(0, 7, "ws://")' in source
    assert "inet_pton(AF_INET, hostname.c_str(), &address)" in source
    assert "(ipv4 & 0xFF000000U) == 0x0A000000U" in source
    assert "(ipv4 & 0xFFF00000U) == 0xAC100000U" in source
    assert "(ipv4 & 0xFFFF0000U) == 0xC0A80000U" in source
    assert "station_configured = saved_config_loaded && is_supported_backend_origin" in source
    assert 'ws_config.crt_bundle_attach = secure_backend ? esp_crt_bundle_attach : nullptr;' in source
    assert "Using unencrypted local WebSocket backend; use only on a trusted LAN" in source
    assert "station_configured ? WIFI_MODE_STA : WIFI_MODE_AP" in source
    assert "esp_wifi_set_mode(WIFI_MODE_APSTA)" in source
    network_setup = source[source.index("void network_task(void*)"):]
    assert network_setup.index("esp_wifi_set_mode(") < network_setup.index("start_provisioning_portal()")
    assert "Station has not received an IP within 10 seconds; enabling setup access point" in source
    assert "pdMS_TO_TICKS(10000)" in source
    assert "Setup access point started after station timeout; SSID: %s; dashboard: http://192.168.4.1/" in source
    assert "Initial setup access point started; SSID: %s; dashboard: http://192.168.4.1/" in source
    assert '"/status"' in source
    assert '"/configure"' in source
    assert "request_is_from_setup_ap(request)" in source
    assert "getpeername(httpd_req_to_sockfd(request)" in source
    assert "peer.ss_family == AF_INET6" in source
    assert "IPV4_MAPPED_PREFIX" in source
    assert "Setup portal denied non-AP client %u.%u.%u.%u" in source
    assert "0xC0A80400U" in source
    assert 'backend.rfind("wss://", 0) != 0' in source
    assert "hostname.find('.') != std::string::npos && !numeric_address && !local_domain" in source
    assert "station_configured = saved_config_loaded && is_supported_backend_origin" in source


def test_firmware_plays_one_second_startup_tone_on_amplifier_output() -> None:
    main = (ROOT / "main" / "main.cpp").read_text()
    audio = (ROOT / "main" / "audio_i2s.cpp").read_text()
    header = (ROOT / "include" / "audio_i2s.h").read_text()
    assert "esp_err_t play_startup_sound(uint32_t duration_ms = 1000);" in header
    assert "AudioI2S::play_startup_sound();" in main
    assert "AudioI2S::set_capture_enabled(false);" in main
    assert "AudioI2S::set_capture_enabled(true);" in main
    assert "constexpr double FREQUENCY_HZ = 660.0;" in audio
    assert "total_samples =\n        static_cast<uint64_t>(HardwareConfig::Audio::SAMPLE_RATE) * duration_ms / 1000" in audio
    assert "i2s_channel_write(" in audio
    assert "tx_channel, samples.data()" in audio


def test_microphone_capture_reports_signal_level_and_queue_depth() -> None:
    audio = (ROOT / "main" / "audio_i2s.cpp").read_text()
    capture = audio.split("void capture_task(void*)", 1)[1].split(
        "void playback_task(void*)", 1
    )[0]
    assert "Microphone capture: rms=%lu peak=%u samples=%lu queued_blocks=%u" in capture
    assert "captured_square_sum" in capture
    assert "uxQueueMessagesWaiting(pcm_queue)" in capture
    assert "pdMS_TO_TICKS(5000)" in capture


def test_amplifier_stays_muted_except_during_audio_playback() -> None:
    audio = (ROOT / "main" / "audio_i2s.cpp").read_text()
    initialize = audio.split("esp_err_t initialize()", 1)[1].split(
        "esp_err_t play_startup_sound", 1
    )[0]
    playback = audio.split("void playback_task", 1)[1].split("\n}\n}", 1)[0]
    assert "set_amplifier_enabled(false)" in initialize
    assert "gpio_set_level(HardwareConfig::Amplifier::SHUTDOWN, enabled ? 1 : 0)" in audio
    assert "AMPLIFIER_IDLE_TIMEOUT = pdMS_TO_TICKS(600)" in audio
    assert "if (amplifier_enabled && !startup_sound_active.load())" in playback
    assert "set_amplifier_enabled(true)" in playback
    assert "set_amplifier_enabled(false)" in playback
    assert "startup_sound_active.store(false);" in audio
    assert "STARTUP_TAIL_SAMPLES" in audio


def test_firmware_ota_download_verifies_image_before_slot_switch() -> None:
    source = (ROOT / "main" / "device_network.cpp").read_text()
    build = (ROOT / "main" / "CMakeLists.txt").read_text()
    defaults = (ROOT / "sdkconfig.defaults").read_text()
    assert '#include "esp_http_client.h"' in source
    assert '#include "esp_ota_ops.h"' in source
    assert "PSA_ALG_SHA_256" in source
    assert "esp_http_client_set_header(client, \"Authorization\"" in source
    assert "std::strcmp(actual_hash, update.sha256.data()) != 0" in source
    assert "esp_ota_set_boot_partition(partition)" in source
    assert "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" in defaults
    assert "esp_http_client app_update" in build


def test_audio_queues_are_psram_backed_and_online_tone_plays_on_ip() -> None:
    audio = (ROOT / "main" / "audio_i2s.cpp").read_text()
    network = (ROOT / "main" / "device_network.cpp").read_text()
    assert "xQueueCreateWithCaps(CAPTURE_QUEUE_BLOCKS, sizeof(PcmBlock), MALLOC_CAP_SPIRAM)" in audio
    assert "xQueueCreateWithCaps(PLAYBACK_QUEUE_BLOCKS, sizeof(PcmBlock), MALLOC_CAP_SPIRAM)" in audio
    assert 'std::strcmp(effect, "online") == 0' in audio
    assert 'AudioI2S::queue_sound_effect("online");' in network

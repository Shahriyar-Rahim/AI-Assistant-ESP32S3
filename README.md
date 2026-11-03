# Distributed Edge AI Assistant

This workspace contains an in-progress ESP32-S3 distributed assistant. The firmware targets ESP32-S3 N16R8 and builds independent I2S peripherals for INMP441 input and MAX98357A output. It includes WPA2 setup, NVS credential storage, authenticated TLS WebSocket PCM transport, reconnect with bounded replay/ACK/NACK, playback, and persistent microphone/backlight controls. Firmware OTA downloads the authenticated image over HTTPS, verifies SHA-256 before switching dual OTA slots, and enables boot rollback. The display driver automatically selects an SSD1306 OLED at I²C address 0x3C/0x3D when detected on GPIO1/2, or falls back to the ST7735 TFT on SPI2; both render the shared live display model. The FastAPI backend includes the voice pipeline, typed actions, calendar/games/integrations, automation rules, telemetry, display configuration, and OTA management; React supplies monitoring, display, OTA, alarm, settings, and developer surfaces. The simulator remains explicitly `SIMULATED`. USB validation confirms firmware boot, microphone samples, and automatic ST7735 selection. The board connected to the backend while both hosts shared the `192.168.10.x` LAN; the host has since moved to `192.168.1.107`, leaving the device/backend unreachable until network or provisioning is corrected. Audible speech, wake recognition, sustained streaming, and physical OTA remain unverified.

## Local backend

1. Install Python 3.11 or newer.
2. `python -m venv .venv && . .venv/bin/activate`
3. `pip install -r backend/requirements.txt`; for wake-word detection install `pip install -r backend/requirements-wakeword.txt`. Install `espeak-ng` on native Linux hosts for local TTS; the backend container installs it automatically.
4. `cp .env.example .env`, set distinct random `DEVICE_TOKEN` and `SIMULATOR_TOKEN`, set a 32+ character `AUTH_SIGNING_SECRET`, and configure a strong `BOOTSTRAP_ADMIN_USERNAME` and `BOOTSTRAP_ADMIN_PASSWORD`.
5. `cd backend && ../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`
6. Visit `http://localhost:8000/docs` or `http://localhost:8000/api/v1/health`.

Starting Uvicorn from `backend/` lets relative model paths such as `./app/models/hey_buddy.onnx` resolve correctly; python-dotenv finds the root `.env`. The audio WebSocket requires the configured shared device token or a separately issued per-device token and a registered device hello message. Dashboard routes require bearer authentication. By default, device audio is processed in memory only. If the user explicitly enables Personalization in the dashboard, verified voice PCM and transcripts are encrypted with `VOICE_ENCRYPTION_KEY`, retained for at most 30 days, and removable from that page; the derived language/response-length profile is used only for that user's replies. Production integrations that store credentials require a generated `INTEGRATION_ENCRYPTION_KEY`; voice enrollment and encrypted personalization require `VOICE_ENCRYPTION_KEY`.

Run the dashboard in another terminal with `cd dashboard && npm install && npm run dev`, then open `http://localhost:5173`. The authenticated dashboard includes device, calendar/reminder/to-do/alarm, game, AI/voice, audit/export, integration, weather, endpoint-monitoring, live telemetry, display configuration, OTA release, device settings, and developer diagnostics workflows. Display Studio can send live pages and weather coordinates to a registered device; the display also accepts voice page commands. Remaining limitations are tracked in the implementation ledger.

For the local container stack, configure the environment as above, then run `docker compose up --build`. It serves the backend at `http://localhost:8000` and dashboard at `http://localhost:8080`.

## Tests

`python -m pytest -q backend/tests firmware/tests`

Dashboard validation: `cd dashboard && npm run typecheck && npm run build`.

## Firmware

Firmware uses PlatformIO with the ESP-IDF framework. From `firmware/`, run `pio run`. The custom PlatformIO board target is ESP32-S3 N16R8 with 16 MB flash and 8 MB OPI PSRAM. At boot firmware plays a two-second speaker test tone through the MAX98357A on I2S1, pausing and then resuming microphone capture. To flash a connected board, use `pio run -d firmware -t upload --upload-port /dev/ttyACM0` and replace the port as needed. The current firmware was flashed and verified over USB; logs confirm I2S initialization, microphone samples, and ST7735 selection. Audio uplink still reports queue overflow and intermittent WebSocket write timeouts, and audible speaker output/wake recognition have not been confirmed. Verify the actual board variant and attached display module before flashing. GPIO and peripheral assignments are centralized in `firmware/include/hardware_config.h`.

Physical wiring and electrical characteristics must be checked before powering attached modules. The TFT's positive `LED` backlight input may use GPIO13 PWM directly only if the module's current limiting keeps the pin within ESP32-S3 limits; see the backlight wiring notes in [`docs/hardware.md`](./docs/hardware.md).

See [`docs/hardware.md`](./docs/hardware.md) for the complete pin wiring and power-on checklist and [`docs/deployment.md`](./docs/deployment.md) for local and hosted deployment limitations.
External service setup and credential requirements are listed in [`docs/external-dependencies.md`](./docs/external-dependencies.md).

## Simulator

After starting the backend, set `BACKEND_URL`, `DEVICE_ID`, and `SIMULATOR_TOKEN` in the shell and run `python simulator/device_simulator.py`. `--scenario replay-gap` verifies server NACK/ordered-replay ACK behavior, `--scenario reconnect` exercises two simulated sessions, `--scenario observe` waits for server control events (optionally filtered by comma-separated `SIMULATOR_EXPECT`), and `--scenario pcm --pcm-file capture.pcm` streams an explicitly supplied raw 16 kHz mono PCM16 fixture. All simulator output is marked `SIMULATED`; synthetic traffic or a supplied recording is not evidence of physical hardware, speaker verification, anti-spoof model accuracy, provider availability, or OTA rollback on the ESP32.

## Remaining work

See [`IMPLEMENTATION_STATUS.md`](./IMPLEMENTATION_STATUS.md) for the feature-by-feature implementation and verification ledger, external prerequisites, and exact unfinished areas. A passing software build is not evidence that peripherals, provider credentials, OAuth approval, or physical hardware function.

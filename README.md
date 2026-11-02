# Distributed Edge AI Assistant

This workspace contains an in-progress ESP32-S3 distributed assistant. The firmware targets ESP32-S3 N16R8 and builds independent I2S peripherals for INMP441 input and MAX98357A output. It includes WPA2 setup, NVS credential storage, authenticated TLS WebSocket PCM transport, reconnect with bounded replay/ACK/NACK, playback, and persistent microphone/backlight controls. Firmware OTA downloads the authenticated image over HTTPS, verifies SHA-256 before switching dual OTA slots, and enables boot rollback. The display driver automatically selects an SSD1306 OLED at I²C address 0x3C/0x3D when detected on GPIO1/2, or falls back to the ST7735 TFT on SPI2; both render the shared live display model. The FastAPI backend includes the voice pipeline, typed actions, calendar/games/integrations, automation rules, telemetry, display configuration, and OTA management; React supplies monitoring, display, and OTA surfaces. The simulator remains explicitly `SIMULATED`. Physical audio, display, OTA, model, and provider operation is not claimed; see the feature ledger for exact status and external dependencies.

## Local backend

1. Install Python 3.11 or newer.
2. `python -m venv .venv && . .venv/bin/activate`
3. `pip install -r backend/requirements.txt`
4. `cp .env.example .env`, set distinct random `DEVICE_TOKEN` and `SIMULATOR_TOKEN`, set a 32+ character `AUTH_SIGNING_SECRET`, and configure a strong `BOOTSTRAP_ADMIN_USERNAME` and `BOOTSTRAP_ADMIN_PASSWORD`.
5. `uvicorn app.main:app --app-dir backend --reload`
6. Visit `http://localhost:8000/docs` or `http://localhost:8000/api/v1/health`.

The audio WebSocket requires the configured shared device token or a separately issued per-device token and a registered device hello message. Dashboard routes require bearer authentication. By default, device audio is processed in memory only. If the user explicitly enables Personalization in the dashboard, verified voice PCM and transcripts are encrypted with `VOICE_ENCRYPTION_KEY`, retained for at most 30 days, and removable from that page; the derived language/response-length profile is used only for that user's replies. Production integrations that store credentials require a generated `INTEGRATION_ENCRYPTION_KEY`; voice enrollment and encrypted personalization require `VOICE_ENCRYPTION_KEY`.

Run the dashboard in another terminal with `cd dashboard && npm install && npm run dev`, then open `http://localhost:5173`. The authenticated dashboard includes device, calendar/reminder/to-do, game, AI/voice, audit/export, integration, weather, endpoint-monitoring, live telemetry, display configuration, and OTA release workflows. Display Studio can send live pages and weather coordinates to a registered device; the display also accepts voice page commands. Other incomplete surfaces remain tracked in the implementation ledger.

For the local container stack, configure the environment as above, then run `docker compose up --build`. It serves the backend at `http://localhost:8000` and dashboard at `http://localhost:8080`.

## Tests

`python -m pytest -q backend/tests firmware/tests`

Dashboard validation: `cd dashboard && npm run typecheck && npm run build`.

## Firmware

Firmware uses PlatformIO with the ESP-IDF framework. From `firmware/`, run `pio run`. The custom PlatformIO board target is ESP32-S3 N16R8 with 16 MB flash and 8 MB OPI PSRAM. At boot firmware plays a two-second speaker test tone through the MAX98357A on I2S1, pausing and then resuming microphone capture; this tests physical audio output without relying on network connectivity. To flash a connected board, use `pio run -d firmware -t upload --upload-port /dev/ttyACM0` and replace the port as needed. This build increases the audio uplink task stack after a logged stack-overflow reboot; flashing and physical reconnect stability have not yet been verified. Verify the actual board variant and attached display module before flashing. GPIO and peripheral assignments are centralized in `firmware/include/hardware_config.h`.

Physical wiring and electrical characteristics must be checked before powering attached modules. The TFT's positive `LED` backlight input may use GPIO13 PWM directly only if the module's current limiting keeps the pin within ESP32-S3 limits; see the backlight wiring notes in [`docs/hardware.md`](./docs/hardware.md).

See [`docs/hardware.md`](./docs/hardware.md) for the complete pin wiring and power-on checklist and [`docs/deployment.md`](./docs/deployment.md) for local and hosted deployment limitations.
External service setup and credential requirements are listed in [`docs/external-dependencies.md`](./docs/external-dependencies.md).

## Simulator

After starting the backend, set `BACKEND_URL`, `DEVICE_ID`, and `SIMULATOR_TOKEN` in the shell and run `python simulator/device_simulator.py`. Simulator-originated records are marked `SIMULATED`; simulated traffic is not evidence of physical hardware operation.

## Remaining work

See [`IMPLEMENTATION_STATUS.md`](./IMPLEMENTATION_STATUS.md) for the feature-by-feature implementation and verification ledger, external prerequisites, and exact unfinished areas. A passing software build is not evidence that peripherals, provider credentials, OAuth approval, or physical hardware function.

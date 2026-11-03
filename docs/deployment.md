# Deployment notes

## Local Compose

Use the root `.env.example`, replace both device tokens with distinct random values, then:

```sh
docker compose up --build
```

The backend listens on port 8000 and the dashboard on port 8080. Local persistence uses a named SQLite volume and the first Alembic migration is applied by the backend container. This configuration is for local development, not a production security boundary.

## Native backend

Create the virtual environment and install the base Python dependencies plus the wake-word runtime from the repository root. Native Linux deployments also need `espeak-ng` for offline speech:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r backend/requirements.txt -r backend/requirements-wakeword.txt
sudo apt-get install espeak-ng
cd backend
../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Starting from `backend/` resolves relative model paths such as `./app/models/hey_buddy.onnx`; python-dotenv discovers the root `.env`. The backend container installs eSpeak NG and the OpenWakeWord runtime/shared assets. On a native host without eSpeak NG, choose a working cloud TTS provider or install the local engine before selecting `TTS_PROVIDER=local`.

## Render backend

Create a web service from the repository using `backend/Dockerfile` as its Dockerfile. Configure a persistent disk if using SQLite and set `DATABASE_URL` to a path on that disk. Set distinct `DEVICE_TOKEN` and `SIMULATOR_TOKEN`, `AUTH_SIGNING_SECRET` (at least 32 characters), a strong bootstrap admin username/password, and `ALLOWED_ORIGINS` as encrypted service environment variables; never commit them. Configure provider credentials only as backend secrets. Use an externally managed PostgreSQL database for production data and migrations. Integrations that store credentials require `INTEGRATION_ENCRYPTION_KEY`; biometric enrollment requires optional voice dependencies, model weights, and `VOICE_ENCRYPTION_KEY`.

Free services may sleep or have limits. Scheduled endpoint pings do not bypass provider sleep policies and must respect provider terms and quotas.

## Firmware networking

The firmware uses ESP-IDF component-manager dependencies declared in `firmware/main/idf_component.yml`; PlatformIO resolves them during `pio run`. The WPA2 setup AP is enabled on first setup and after a configured station fails to obtain an IP within 10 seconds; normal configured boots begin in station-only mode. Connect to the `Buddy-Setup-<MAC suffix>` SSID using the fixed password `BuddySetup-ESP32S3` and open `http://192.168.4.1/` directly to view status or change Wi-Fi credentials/backend. This shared password is compiled into firmware; change it before deploying devices outside a trusted environment. For production, enter a public DNS hostname, HTTPS URL, or WSS URL (hostname and optional port only, no path); the firmware uses `wss://` and certificate validation. For local development, enter `ws://<private-LAN-IPv4>:<port>` such as `ws://192.168.10.163:8000`; the backend and ESP32 must share a reachable LAN. Only private RFC1918 IPv4 addresses are permitted for unencrypted WebSockets. Local `ws://` is not encrypted and must only be used on a trusted network. Register the device in the authenticated dashboard first, then provision its device ID and one-time token. HTTPS/WSS devices need DNS plus access to `pool.ntp.org` for clock sync. Wi-Fi modem sleep is disabled in station mode to reduce latency for continuous audio, which increases power consumption; audio send failures include AP RSSI to help identify weak-signal/interference problems. The firmware reconnects to Wi-Fi/WebSocket automatically and replays retained unacknowledged PCM frames once the server-ready event arrives. OTA deployment downloads the authenticated release via HTTPS, verifies declared size and SHA-256 before switching dual OTA slots, and enables rollback; test it only with a recovery path on the physical board. Disconnected release requests are held in process memory until device reconnect, so they are not durable across backend restarts. NVS credential encryption is not enabled, so enable and validate flash/NVS encryption before production deployment.

## Vercel dashboard

Import the repository as a Vite application, set the project root to `dashboard`, build with `npm ci && npm run build`, and publish `dist`. Set `VITE_API_URL` to the public HTTPS backend origin at build time, then add that exact origin to backend `ALLOWED_ORIGINS`. Never put provider keys, device tokens, OAuth secrets, or admin credentials in `VITE_*` variables; Vite embeds those values in the browser bundle.

The dashboard has bearer-authenticated pages for devices, calendar/reminders/to-dos, notifications, games, AI, voice/security, integrations, a credential hub, weather, endpoint monitoring, audit records, live telemetry, display configuration, and OTA releases. Display Studio can send a live page to an online device or persist the selection for reconnect; voice commands can show supported pages. The credential hub exposes provider connection metadata only and removes the assistant's encrypted credential copy; it does not revoke tokens or consent grants at the provider. OTA deployment and display rendering are implemented in software but require a flashed ESP32-S3 and live backend/hardware validation.

Voice commands require a voice enrollment by default (`BIOMETRIC_MODE=enforce`). For a trusted local setup only, `BIOMETRIC_MODE=enrolled_only` allows voice commands to use the first active account until the first voice enrollment is created; after that, anti-spoofing and speaker verification are enforced. Do not use this onboarding mode on an untrusted network or production deployment.

## Cloudflare Tunnel

For private local development, run the backend and dashboard with Compose and create a Cloudflare Tunnel to the intended service. Route the hostname to the appropriate local origin, enforce Cloudflare Access or equivalent authentication, and use HTTPS externally. WebSocket forwarding must be enabled and verified through the chosen tunnel configuration. Do not expose a deployment until secrets, TLS, database backups, rate limits, and access controls have been reviewed.

## Database and secrets

SQLite is appropriate for local development. PostgreSQL service compatibility, backup/restore, retention policy validation, and encryption-key rotation still require deployment testing before storing sensitive production data. Keep API secrets on the backend, use least-privilege tokens, and rotate any credential that is exposed.
